"""Checkpoint B — administrator CRM import staging preview.

Run: python test_crm_import_staging.py

Uses isolated testdb copies only — never writes production northstar.db,
never talks to live :8007, and never stores a real staff password in source.
"""

from __future__ import annotations

import testdb
import csv
import io
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from auth_http import (
    ADMIN_REQUIRED_DETAIL,
    AUTH_REQUIRED_DETAIL,
    CSRF_HEADER,
)
from auth_passwords import hash_password
from crm_import_staging import (
    BATCH_NOT_REUSABLE,
    CELL_PREVIEW_CHARS,
    CLIENT_REQUIRED,
    EXTRA_SOURCE_COLUMNS,
    FILE_TOO_LARGE,
    MAPPING_COMPANY_REQUIRED,
    MAPPING_DUPLICATE_HEADER,
    MAPPING_EMPTY_HEADER,
    MAPPING_HEADER_MISSING,
    MAPPING_NAME_CONFLICT,
    MAPPING_NO_ROWS,
    MAPPING_UNKNOWN_FIELD,
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    NEED_WORKSHEET,
    RETENTION_DAYS,
    TOO_MANY_COLUMNS,
    TOO_MANY_ROWS,
    UNSUPPORTED_TYPE,
    ensure_crm_import_schema,
)
from db import get_connection
from main import app

UPLOAD = "/api/clients/{client_id}/admin/imports"
BATCH = "/api/clients/{client_id}/admin/imports/{batch_id}"
ROWS = "/api/clients/{client_id}/admin/imports/{batch_id}/rows"
MAPPING = "/api/clients/{client_id}/admin/imports/{batch_id}/mapping"

CRM_TABLES = (
    "companies",
    "contacts",
    "client_company_relationships",
    "activities",
    "legacy_notes",
    "contact_client_workflows",
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret_password() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client() -> TestClient:
    return TestClient(app)


def _dump(payload: object) -> str:
    return json.dumps(payload, default=str)


def _assert_no_secrets(payload: object, *secrets_out: str) -> None:
    dumped = _dump(payload).lower()
    if "password_hash" in dumped:
        _fail("JSON leaked password_hash.")
    if "$argon2id$" in dumped:
        _fail("JSON leaked an Argon2 hash.")
    if "csrf_secret" in dumped:
        _fail("JSON leaked csrf_secret.")
    for needle in (
        "access_token",
        "refresh_token",
        "bearer ",
        "oauth_token",
        "northstar_oauth_token_key",
    ):
        if needle in dumped:
            _fail("JSON leaked an OAuth token or secret field.")
    for item in secrets_out:
        if item and str(item).lower() in dumped:
            _fail("JSON leaked a raw secret.")


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _create_user(*, email: str, password: str, administrator: int = 0, active: int = 1) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, ?, ?, 1, ?, ?, 0, '')
            """,
            (email, "CRM Import Test User", int(administrator), int(active), digest),
        )
        user_id = int(
            conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        )
        conn.commit()
    return user_id


def _assign(user_id: int, client_id: int, *, role: str = "staff") -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, ?, 1, datetime('now'))
            """,
            (user_id, client_id, role),
        )
        conn.commit()


def _delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def _login(http: TestClient, email: str, password: str):
    return http.post("/api/auth/login", json={"email": email, "password": password})


def _csrf(response) -> str:
    return str(response.json().get("csrf_token") or "")


def _crm_counts():
    with get_connection() as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in CRM_TABLES
        }


def _assert_crm_frozen(before: dict, label: str) -> None:
    after = _crm_counts()
    if after != before:
        _fail(f"{label} changed CRM tables: {before} -> {after}")


def _staging_counts():
    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        batches = int(conn.execute("SELECT COUNT(*) AS n FROM crm_import_batches").fetchone()["n"])
        rows = int(conn.execute("SELECT COUNT(*) AS n FROM crm_import_rows").fetchone()["n"])
        conn.commit()
    return batches, rows


def _batch_row_count(batch_id: int) -> int:
    with get_connection() as conn:
        n = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM crm_import_rows WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()["n"]
        )
    return n


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _xlsx_bytes(
    sheets: dict[str, list[list[object]]],
    *,
    hidden: tuple[str, ...] = (),
) -> bytes:
    wb = Workbook()
    first = True
    for name, grid in sheets.items():
        if first:
            ws = wb.active
            ws.title = name
            first = False
        else:
            ws = wb.create_sheet(title=name)
        for row in grid:
            ws.append(row)
        if name in hidden:
            ws.sheet_state = "hidden"
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _row_fingerprint(batch_id: int) -> tuple[int, int, int]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(length(raw_json)), 0) AS bytes,
                   COALESCE(SUM(source_row_number), 0) AS nums
            FROM crm_import_rows WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
    return int(row["n"]), int(row["bytes"]), int(row["nums"])


def _mapping_columns() -> set[str]:
    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        names = {str(r["name"]) for r in conn.execute("PRAGMA table_info(crm_import_batches)")}
        conn.commit()
    return names


def _upload(http: TestClient, client_id: int, csrf: str, filename: str, content: bytes, worksheet: str = ""):
    data = {}
    if worksheet:
        data["worksheet"] = worksheet
    return http.post(
        UPLOAD.format(client_id=client_id),
        headers={CSRF_HEADER: csrf},
        files={"file": (filename, content, "application/octet-stream")},
        data=data,
    )


class _Session:
    def __init__(self):
        assigned_id, other_id = _client_ids()
        self.assigned_id = assigned_id
        self.other_id = other_id
        self.password = _secret_password()
        self.email = f"import.admin.{secrets.token_hex(4)}@example.test"
        self.user_id = _create_user(email=self.email, password=self.password, administrator=1)
        _assign(self.user_id, assigned_id)
        self.http = _client()
        login = _login(self.http, self.email, self.password)
        if login.status_code != 200:
            _fail(f"Admin login failed: {login.status_code}")
        self.csrf = _csrf(login)
        self.crm_before = _crm_counts()
        self.staging_before = _staging_counts()

    def close(self) -> None:
        _delete_user(self.user_id)

    def assert_frozen(self, label: str) -> None:
        _assert_crm_frozen(self.crm_before, label)


def test_unauthenticated_and_non_admin_are_denied() -> None:
    assigned_id, _other = _client_ids()
    crm_before = _crm_counts()
    staging_before = _staging_counts()
    http = _client()
    csv_file = _csv_bytes(["Company", "Email"], [["Acme", "a@example.test"]])
    anon = http.post(
        UPLOAD.format(client_id=assigned_id),
        files={"file": ("contacts.csv", csv_file, "text/csv")},
    )
    if anon.status_code != 401:
        _fail(f"Anonymous upload must be 401, got {anon.status_code}.")
    if anon.json().get("detail") != AUTH_REQUIRED_DETAIL:
        _fail("Anonymous upload 401 detail was not generic.")
    _assert_no_secrets(anon.json())

    password = _secret_password()
    email = f"import.staff.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=0)
    _assign(user_id, assigned_id)
    try:
        login = _login(http, email, password)
        if login.status_code != 200:
            _fail("Staff login failed.")
        csrf = _csrf(login)
        denied = http.post(
            UPLOAD.format(client_id=assigned_id),
            headers={CSRF_HEADER: csrf},
            files={"file": ("contacts.csv", csv_file, "text/csv")},
        )
        if denied.status_code != 403:
            _fail(f"Non-admin upload must be 403, got {denied.status_code}.")
        if denied.json().get("detail") != ADMIN_REQUIRED_DETAIL:
            _fail("Non-admin 403 detail was not generic.")
        _assert_no_secrets(denied.json())
    finally:
        _delete_user(user_id)

    if _staging_counts() != staging_before:
        _fail("Denied uploads must not create staging batches or rows.")
    _assert_crm_frozen(crm_before, "Denied uploads")


def test_zero_client_and_unassigned_client() -> None:
    session = _Session()
    try:
        zero = session.http.post(
            UPLOAD.format(client_id=0),
            headers={CSRF_HEADER: session.csrf},
            files={"file": ("contacts.csv", _csv_bytes(["A"], [["1"]]), "text/csv")},
        )
        if zero.status_code != 400:
            _fail(f"client_id 0 must be 400, got {zero.status_code}.")
        if CLIENT_REQUIRED not in str(zero.json().get("detail")):
            _fail("client_id 0 must require an explicit client.")
        if _staging_counts() != session.staging_before:
            _fail("client_id 0 must not create staging.")
        other = _upload(
            session.http,
            session.other_id,
            session.csrf,
            "contacts.csv",
            _csv_bytes(["A"], [["1"]]),
        )
        if other.status_code != 200 or other.json().get("kind") != "previewed":
            _fail("Administrators may stage a preview for any existing client.")
        batch_id = other.json()["batch"]["batch_id"]
        missing = session.http.get(BATCH.format(client_id=session.assigned_id, batch_id=batch_id))
        if missing.status_code != 404:
            _fail("Batch detail must be scoped to the path client_id.")
        session.assert_frozen("Client scoping")
    finally:
        session.close()


def test_csv_preview_does_not_write_crm() -> None:
    session = _Session()
    try:
        content = _csv_bytes(
            ["Company", "First", "Email"],
            [["Acme", "Ada", "ada@example.test"], ["", "", ""]],
        )
        resp = _upload(session.http, session.assigned_id, session.csrf, "contacts.csv", content)
        if resp.status_code != 200:
            _fail(f"CSV upload must be 200, got {resp.status_code}: {resp.text}")
        payload = resp.json()
        _assert_no_secrets(payload, session.password, session.csrf)
        if payload.get("kind") != "previewed" or not payload.get("batch"):
            _fail("CSV upload must return a previewed batch.")
        batch = payload["batch"]
        if batch["status"] != "previewed" or not batch["reusable"]:
            _fail("CSV batch must be reusable previewed.")
        if batch["source_row_count"] != 1 or batch["blank_row_count"] != 1:
            _fail("CSV preview must keep source counts and skip blank rows.")
        if batch["sample_rows"][0]["source_row_number"] != 2:
            _fail("Preview must preserve the original source row number.")
        if "ada@example.test" not in _dump(batch["sample_rows"]):
            _fail("Preview must show staged cell values.")
        detail = session.http.get(BATCH.format(client_id=session.assigned_id, batch_id=batch["batch_id"]))
        if detail.status_code != 200:
            _fail(f"Batch detail must be 200, got {detail.status_code}.")
        rows = session.http.get(
            ROWS.format(client_id=session.assigned_id, batch_id=batch["batch_id"]),
            params={"offset": 0, "limit": 25},
        )
        if rows.status_code != 200:
            _fail(f"Staged rows must be 200, got {rows.status_code}.")
        if rows.json()["total"] != 1:
            _fail("Paged rows must match staged count.")
        session.assert_frozen("CSV preview")
    finally:
        session.close()


def test_xlsx_formula_is_literal_text() -> None:
    session = _Session()
    try:
        content = _xlsx_bytes({"Sheet1": [["Name", "Amount"], ["Ada", "=1+1"]]})
        resp = _upload(session.http, session.assigned_id, session.csrf, "book.xlsx", content)
        if resp.status_code != 200:
            _fail(f"XLSX upload must be 200, got {resp.status_code}: {resp.text}")
        row = resp.json()["batch"]["sample_rows"][0]
        if row["values"].get("Amount") != "=1+1":
            _fail(f"Formula cells must stay literal text, got {row['values']!r}.")
        if row["has_blocking_error"]:
            _fail("Formula cells must warn, not block.")
        if not any("Formula" in w for w in row["warnings"]):
            _fail("Formula cells must receive a warning.")
        session.assert_frozen("Formula preview")
    finally:
        session.close()


def test_ordinary_long_cell_is_bounded_and_blocking() -> None:
    session = _Session()
    try:
        huge = "x" * (MAX_CELL_CHARS + 5)
        content = _csv_bytes(["Name", "Title"], [["Ada", huge]])
        resp = _upload(session.http, session.assigned_id, session.csrf, "long.csv", content)
        if resp.status_code != 200:
            _fail(f"Long-cell upload must be 200, got {resp.status_code}.")
        batch = resp.json()["batch"]
        if batch["error_row_count"] != 1:
            _fail("Overlong ordinary cells must count as blocking row errors.")
        row = batch["sample_rows"][0]
        stored = row["values"]["Title"]
        if stored != ("x" * CELL_PREVIEW_CHARS) + "…":
            _fail("Overlong ordinary cells must keep only a bounded preview.")
        if huge in _dump(row):
            _fail("Overlong ordinary source text must not be retained in full.")
        if not row["has_blocking_error"] or not row["errors"]:
            _fail("Overlong ordinary cells must add a blocking row error.")
        session.assert_frozen("Ordinary long-cell preview")
    finally:
        session.close()


def test_notes_long_text_is_preserved_with_display_cap() -> None:
    from crm_import_staging import (
        MAX_LONG_TEXT_CHARS,
        PREVIEW_DISPLAY_CHARS,
        is_long_text_header,
        list_crm_import_rows,
    )

    if not is_long_text_header("Notes"):
        _fail("Notes must be recognized as a long-text header.")
    if not is_long_text_header("Comments:"):
        _fail("Comments: must be recognized as a long-text header.")
    if is_long_text_header("Customer Notes") or is_long_text_header("Notepad"):
        _fail("Unrelated headers must not be treated as long-text.")

    session = _Session()
    try:
        body = ("line-one\n" + ("n" * 1990))  # >2000, multiline, well under 100k
        if len(body) <= MAX_CELL_CHARS:
            body = body + ("n" * (MAX_CELL_CHARS + 1 - len(body)))
        content = _csv_bytes(["Company", "Notes"], [["Acme Notes Co", body]])
        resp = _upload(session.http, session.assigned_id, session.csrf, "notes.csv", content)
        if resp.status_code != 200:
            _fail(f"Long Notes upload must be 200, got {resp.status_code}.")
        batch = resp.json()["batch"]
        if batch["error_row_count"] != 0:
            _fail("Accepted long Notes must not count as blocking errors.")
        sample = batch["sample_rows"][0]
        preview = sample["values"]["Notes"]
        if len(preview) != PREVIEW_DISPLAY_CHARS + 1 or not preview.endswith("…"):
            _fail("Preview API must truncate long Notes for display.")
        if body in _dump(sample):
            _fail("Preview payload must not include the complete Notes body.")

        batch_id = int(batch["batch_id"])
        with get_connection() as conn:
            raw = conn.execute(
                "SELECT raw_json, has_blocking_error FROM crm_import_rows WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            stored = json.loads(raw["raw_json"])["Notes"]
            if stored != body:
                _fail("Accepted Notes must be preserved fully in raw_json.")
            if int(raw["has_blocking_error"] or 0) != 0:
                _fail("Accepted Notes must not set has_blocking_error.")

        page = list_crm_import_rows(session.assigned_id, batch_id, offset=0, limit=25)
        listed = page.rows[0].values["Notes"]
        if listed != preview:
            _fail("Row listing must use the same display truncation as sample_rows.")
        with get_connection() as conn:
            again = json.loads(
                conn.execute(
                    "SELECT raw_json FROM crm_import_rows WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()["raw_json"]
            )["Notes"]
            if again != body:
                _fail("Listing must not modify stored raw_json Notes.")

        # Comments header (no trailing colon) is also long-text.
        comments_body = "c" * (MAX_CELL_CHARS + 50)
        resp_c = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "comments.csv",
            _csv_bytes(["Company", "Comments"], [["Comments Co", comments_body]]),
        )
        if resp_c.status_code != 200 or resp_c.json()["batch"]["error_row_count"] != 0:
            _fail("Comments header must accept values over 2000 characters.")
        with get_connection() as conn:
            c_batch = int(resp_c.json()["batch"]["batch_id"])
            c_stored = json.loads(
                conn.execute(
                    "SELECT raw_json FROM crm_import_rows WHERE batch_id = ?",
                    (c_batch,),
                ).fetchone()["raw_json"]
            )["Comments"]
            if c_stored != comments_body:
                _fail("Comments must be preserved fully in raw_json.")

        # Near 100k accepted; HTML-like text stored literally (no execution path).
        near = ("<" + "s" * (MAX_LONG_TEXT_CHARS - 20) + "script>")
        if len(near) > MAX_LONG_TEXT_CHARS:
            near = near[:MAX_LONG_TEXT_CHARS]
        resp_near = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "notes-near.csv",
            _csv_bytes(["Company", "Notes"], [["Near Co", near]]),
        )
        if resp_near.status_code != 200 or resp_near.json()["batch"]["error_row_count"] != 0:
            _fail("Notes near 100000 characters must be accepted.")
        with get_connection() as conn:
            near_id = int(resp_near.json()["batch"]["batch_id"])
            near_stored = json.loads(
                conn.execute(
                    "SELECT raw_json FROM crm_import_rows WHERE batch_id = ?",
                    (near_id,),
                ).fetchone()["raw_json"]
            )["Notes"]
            if near_stored != near:
                _fail("Near-limit Notes must be preserved fully in raw_json.")
        near_preview = resp_near.json()["batch"]["sample_rows"][0]["values"]["Notes"]
        if near in near_preview or len(near_preview) > PREVIEW_DISPLAY_CHARS + 1:
            _fail("Near-limit Notes must still be display-truncated in preview.")

        # Over 100k still blocks.
        too_big = "z" * (MAX_LONG_TEXT_CHARS + 3)
        resp2 = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "notes-too-big.csv",
            _csv_bytes(["Company", "Notes"], [["Huge Co", too_big]]),
        )
        if resp2.status_code != 200:
            _fail("Overlong Notes upload must still return 200 with blocking row.")
        batch2 = resp2.json()["batch"]
        if batch2["error_row_count"] != 1:
            _fail("Notes over 100000 must block.")
        preview2 = batch2["sample_rows"][0]["values"]["Notes"]
        if preview2 != ("z" * CELL_PREVIEW_CHARS) + "…":
            _fail("Blocked overlong Notes must store only the safety preview.")
        with get_connection() as conn:
            blocked = json.loads(
                conn.execute(
                    "SELECT raw_json FROM crm_import_rows WHERE batch_id = ?",
                    (int(batch2["batch_id"]),),
                ).fetchone()["raw_json"]
            )["Notes"]
            if blocked != ("z" * CELL_PREVIEW_CHARS) + "…" or too_big in blocked:
                _fail("Blocked overlong Notes must not retain the full value in raw_json.")
        session.assert_frozen("Long Notes preservation")
    finally:
        session.close()


def test_too_many_columns_fails_without_partial_rows() -> None:
    session = _Session()
    try:
        headers = [f"C{i}" for i in range(MAX_COLUMNS + 1)]
        content = _csv_bytes(headers, [["v"] * (MAX_COLUMNS + 1)])
        staging_before = _staging_counts()
        resp = _upload(session.http, session.assigned_id, session.csrf, "wide.csv", content)
        if resp.status_code != 200:
            _fail(f"Over-column upload must be 200 failed batch, got {resp.status_code}.")
        payload = resp.json()
        if payload.get("kind") != "failed":
            _fail("More than 40 columns must fail the batch.")
        if TOO_MANY_COLUMNS not in str(payload.get("message")):
            _fail("Over-column failure must explain the column limit.")
        batch = payload["batch"]
        if batch["status"] != "failed" or batch["source_row_count"] != 0:
            _fail("Failed over-column batch must stage zero rows.")
        if _batch_row_count(batch["batch_id"]) != 0:
            _fail("Over-column failure left partial staging rows.")
        extra = _csv_bytes(["A", "B"], [["1", "2", "3"]])
        extra_resp = _upload(session.http, session.assigned_id, session.csrf, "extra.csv", extra)
        if extra_resp.json().get("kind") != "failed":
            _fail("Values beyond headers must fail instead of dropping columns.")
        if EXTRA_SOURCE_COLUMNS not in str(extra_resp.json().get("message")):
            _fail("Extra headerless columns must be reported.")
        if _staging_counts()[1] != staging_before[1]:
            _fail("Column overflow must not stage rows.")
        session.assert_frozen("Column overflow")
    finally:
        session.close()


def test_row_limit_and_file_size_leave_no_partial_rows() -> None:
    session = _Session()
    try:
        staging_before = _staging_counts()
        content = _csv_bytes(["Name"], [["a"], ["b"], ["c"]])
        with patch("crm_import_staging.MAX_SOURCE_ROWS", 2):
            resp = _upload(session.http, session.assigned_id, session.csrf, "rows.csv", content)
        if resp.status_code != 200:
            _fail(f"Row-limit upload must be 200 failed batch, got {resp.status_code}.")
        payload = resp.json()
        if payload.get("kind") != "failed" or TOO_MANY_ROWS not in str(payload.get("message")):
            _fail("Row-limit exceedance must fail the batch.")
        if _batch_row_count(payload["batch"]["batch_id"]) != 0:
            _fail("Row-limit failure left partial staging rows.")
        oversized = b"n,email\n" + b"a,b@example.test\n"
        with patch("crm_import_staging.MAX_FILE_BYTES", 8), patch("main.MAX_FILE_BYTES", 8):
            big = _upload(session.http, session.assigned_id, session.csrf, "big.csv", oversized)
        if big.status_code != 400:
            _fail(f"Oversized upload must be 400 with no batch, got {big.status_code}.")
        if FILE_TOO_LARGE not in str(big.json().get("detail")):
            _fail("Oversized upload must use the file-size message.")
        xls = _upload(session.http, session.assigned_id, session.csrf, "legacy.xls", b"not-xlsx")
        if xls.status_code != 400 or UNSUPPORTED_TYPE not in str(xls.json().get("detail")):
            _fail("Unsupported types must 400 with no batch.")
        if _staging_counts()[1] != staging_before[1]:
            _fail("Limit and type failures left staging rows.")
        if _staging_counts()[0] != staging_before[0] + 1:
            _fail("Only the row-limit parse failure may create a failed batch.")
        session.assert_frozen("Limit failures")
    finally:
        session.close()


def test_malformed_xlsx_creates_failed_batch_without_rows() -> None:
    session = _Session()
    try:
        staging_before = _staging_counts()
        resp = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "broken.xlsx",
            b"this is not a zip spreadsheet",
        )
        if resp.status_code != 200:
            _fail(f"Malformed XLSX must return 200 failed batch, got {resp.status_code}.")
        payload = resp.json()
        if payload.get("kind") != "failed":
            _fail("Malformed XLSX must create a failed batch.")
        if payload["batch"]["source_row_count"] != 0:
            _fail("Malformed XLSX must stage zero rows.")
        if _batch_row_count(payload["batch"]["batch_id"]) != 0:
            _fail("Malformed XLSX left partial staging rows.")
        if _staging_counts()[0] != staging_before[0] + 1:
            _fail("Malformed XLSX should create exactly one failed batch.")
        session.assert_frozen("Malformed XLSX")
    finally:
        session.close()


def test_multi_sheet_selection_does_not_write_until_chosen() -> None:
    session = _Session()
    try:
        content = _xlsx_bytes(
            {
                "VisibleA": [["Name"], ["Ada"]],
                "Hidden": [["Secret"], ["nope"]],
                "VisibleB": [["Name"], ["Bea"]],
            },
            hidden=("Hidden",),
        )
        staging_before = _staging_counts()
        first = _upload(session.http, session.assigned_id, session.csrf, "multi.xlsx", content)
        if first.status_code != 200:
            _fail(f"Multi-sheet upload must be 200, got {first.status_code}.")
        payload = first.json()
        if not payload.get("needs_worksheet"):
            _fail("Multiple visible sheets must return a selection response.")
        if payload.get("kind") != "needs_worksheet":
            _fail("Selection response kind must be needs_worksheet.")
        if payload.get("batch") is not None:
            _fail("Sheet selection must not create a batch.")
        if _staging_counts() != staging_before:
            _fail("Sheet selection must not write staging rows or batches.")
        sheets = payload.get("visible_sheets") or []
        if sheets != ["VisibleA", "VisibleB"]:
            _fail(f"Only visible sheets may be offered, got {sheets!r}.")
        if "Hidden" in sheets:
            _fail("Hidden sheets cannot be selected.")
        if NEED_WORKSHEET not in str(payload.get("message")):
            _fail("Selection response must explain that a sheet is required.")
        hidden_try = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "multi.xlsx",
            content,
            worksheet="Hidden",
        )
        if hidden_try.status_code != 200 or hidden_try.json().get("kind") != "failed":
            _fail("Choosing a hidden sheet must fail without staging rows.")
        chosen = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "multi.xlsx",
            content,
            worksheet="VisibleB",
        )
        if chosen.status_code != 200 or chosen.json().get("kind") != "previewed":
            _fail("Selecting one visible sheet must stage a preview.")
        batch = chosen.json()["batch"]
        if batch["worksheet_name"] != "VisibleB":
            _fail("Preview must record the selected visible sheet.")
        if batch["sample_rows"][0]["values"].get("Name") != "Bea":
            _fail("Selected sheet rows must be staged.")
        session.assert_frozen("Multi-sheet selection")
    finally:
        session.close()


def test_single_visible_sheet_autoselects() -> None:
    session = _Session()
    try:
        content = _xlsx_bytes(
            {"Only": [["Name"], ["Ada"]], "Hidden": [["X"], ["nope"]]},
            hidden=("Hidden",),
        )
        resp = _upload(session.http, session.assigned_id, session.csrf, "one.xlsx", content)
        if resp.status_code != 200 or resp.json().get("kind") != "previewed":
            _fail("One visible sheet must autoselect and preview.")
        batch = resp.json()["batch"]
        if batch["worksheet_name"] != "Only":
            _fail("Autoselect must use the visible sheet.")
        if any("Hidden" in w for w in batch["warnings"]) is False:
            _fail("Hidden sheets should be mentioned in warnings.")
        session.assert_frozen("Single visible sheet")
    finally:
        session.close()


def test_cancel_keeps_audit_and_cannot_be_reused() -> None:
    session = _Session()
    try:
        content = _csv_bytes(["Company", "Email"], [["Acme", "a@example.test"]])
        uploaded = _upload(session.http, session.assigned_id, session.csrf, "keep.csv", content)
        batch = uploaded.json()["batch"]
        batch_id = batch["batch_id"]
        checksum = batch["sha256"]
        cancelled = session.http.delete(
            BATCH.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
        )
        if cancelled.status_code != 200:
            _fail(f"Cancel must be 200, got {cancelled.status_code}.")
        view = cancelled.json()
        _assert_no_secrets(view, session.password, session.csrf)
        if view["status"] != "cancelled" or view["reusable"]:
            _fail("Cancelled batch must remain with status=cancelled.")
        if not view["cancelled_at"]:
            _fail("Cancelled batch must record cancelled_at.")
        if view["sha256"] != checksum:
            _fail("Cancelled batch must retain checksum.")
        if view["original_filename"] != "keep.csv":
            _fail("Cancelled batch must retain filename metadata.")
        if view["headers"] or view["sample_rows"] or view["warnings"]:
            _fail("Cancelled batch must not keep source headers, warnings, or sample rows.")
        if _batch_row_count(batch_id) != 0:
            _fail("Cancel must delete staged rows.")
        with get_connection() as conn:
            exists = conn.execute(
                "SELECT id, status FROM crm_import_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        if exists is None:
            _fail("Cancel must retain the batch record for audit.")
        if str(exists["status"]) != "cancelled":
            _fail("Retained audit batch must stay cancelled.")
        rows = session.http.get(ROWS.format(client_id=session.assigned_id, batch_id=batch_id))
        if rows.status_code != 409:
            _fail(f"Cancelled rows must be 409, got {rows.status_code}.")
        if BATCH_NOT_REUSABLE not in str(rows.json().get("detail")):
            _fail("Cancelled batch cannot be reused.")
        again = session.http.delete(
            BATCH.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
        )
        if again.status_code != 200 or again.json()["status"] != "cancelled":
            _fail("A second cancel must keep the same audit batch.")
        session.assert_frozen("Cancel")
    finally:
        session.close()


def test_expired_batch_purges_rows_and_cannot_be_reused() -> None:
    session = _Session()
    try:
        content = _csv_bytes(["Company"], [["Acme"], ["Beta"]])
        uploaded = _upload(session.http, session.assigned_id, session.csrf, "expire.csv", content)
        batch = uploaded.json()["batch"]
        batch_id = batch["batch_id"]
        if not batch["expires_at"]:
            _fail("Previewed batches must have expires_at.")
        created = datetime.strptime(batch["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        expires = datetime.strptime(batch["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        if expires - created != timedelta(days=RETENTION_DAYS):
            _fail("Default staging retention must be 7 days.")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_connection() as conn:
            conn.execute(
                "UPDATE crm_import_batches SET expires_at = ? WHERE id = ?",
                (past, batch_id),
            )
            conn.commit()
        detail = session.http.get(BATCH.format(client_id=session.assigned_id, batch_id=batch_id))
        if detail.status_code != 200:
            _fail("Expired batch audit metadata must still be readable.")
        view = detail.json()
        if view["status"] != "expired" or view["reusable"]:
            _fail("Expired batch must be marked expired and not reusable.")
        if view["headers"] or view["sample_rows"] or view["warnings"]:
            _fail("Expired batch must not keep source headers or sample data.")
        if view["original_filename"] != "expire.csv" or not view["sha256"]:
            _fail("Expired batch must retain filename and checksum audit fields.")
        if _batch_row_count(batch_id) != 0:
            _fail("Expired staging rows must be purged.")
        rows = session.http.get(ROWS.format(client_id=session.assigned_id, batch_id=batch_id))
        if rows.status_code != 409:
            _fail(f"Expired rows must be 409, got {rows.status_code}.")
        cancel = session.http.delete(
            BATCH.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
        )
        if cancel.status_code != 409:
            _fail("Expired batches cannot be reused via cancel.")
        session.assert_frozen("Expiration")
    finally:
        session.close()


def _put_mapping(session: _Session, batch_id: int, mapping: dict[str, str], client_id: int | None = None):
    return session.http.put(
        MAPPING.format(
            client_id=session.assigned_id if client_id is None else client_id,
            batch_id=batch_id,
        ),
        headers={CSRF_HEADER: session.csrf},
        json={"mapping": mapping},
    )


def test_mapping_schema_is_idempotent() -> None:
    from db import migrate_schema

    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        ensure_crm_import_schema(conn)
        migrate_schema(conn)
        conn.commit()
        names = {str(r["name"]) for r in conn.execute("PRAGMA table_info(crm_import_batches)")}
    for column in ("mapping_json", "mapping_updated_at", "mapping_updated_by_user_id"):
        if column not in names:
            _fail(f"crm_import_batches missing {column}.")


def test_mapping_auth_and_scope() -> None:
    assigned_id, _other = _client_ids()
    http = _client()
    anon = http.put(
        MAPPING.format(client_id=assigned_id, batch_id=1),
        json={"mapping": {"company_name": "Company"}},
    )
    if anon.status_code != 401:
        _fail(f"Anonymous mapping must be 401, got {anon.status_code}.")
    if anon.json().get("detail") != AUTH_REQUIRED_DETAIL:
        _fail("Anonymous mapping 401 detail was not generic.")
    password = _secret_password()
    email = f"import.staff.map.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=0)
    _assign(user_id, assigned_id)
    try:
        login = _login(http, email, password)
        csrf = _csrf(login)
        denied = http.put(
            MAPPING.format(client_id=assigned_id, batch_id=1),
            headers={CSRF_HEADER: csrf},
            json={"mapping": {"company_name": "Company"}},
        )
        if denied.status_code != 403:
            _fail(f"Non-admin mapping must be 403, got {denied.status_code}.")
        if denied.json().get("detail") != ADMIN_REQUIRED_DETAIL:
            _fail("Non-admin mapping 403 detail was not generic.")
    finally:
        _delete_user(user_id)


def test_mapping_persists_and_does_not_write_crm_or_rows() -> None:
    session = _Session()
    try:
        content = _csv_bytes(
            ["Company", "Email", "First", "Last"],
            [["Acme", "a@example.test", "Ada", "Lovelace"]],
        )
        uploaded = _upload(session.http, session.assigned_id, session.csrf, "map.csv", content)
        batch = uploaded.json()["batch"]
        batch_id = batch["batch_id"]
        before_rows = _row_fingerprint(batch_id)
        if before_rows[0] != 1:
            _fail("Preview must stage one row before mapping.")
        resp = _put_mapping(
            session,
            batch_id,
            {
                "company_name": "  Company  ",
                "contact_email": "Email",
                "contact_first_name": "First",
                "contact_last_name": "Last",
            },
        )
        if resp.status_code != 200:
            _fail(f"Valid mapping must be 200, got {resp.status_code}: {resp.text}")
        view = resp.json()
        _assert_no_secrets(view, session.password, session.csrf)
        mapping = view.get("mapping") or {}
        if mapping.get("company_name") != "Company":
            _fail("Trimmed mapping must persist the exact headers_json value.")
        if mapping.get("contact_email") != "Email":
            _fail("contact_email must persist the Email header.")
        if view.get("mapping_updated_by_user_id") != session.user_id:
            _fail("Mapping must record the session administrator.")
        if not view.get("mapping_updated_at"):
            _fail("mapping_updated_at must be set.")
        if _row_fingerprint(batch_id) != before_rows:
            _fail("Saving a mapping must not alter crm_import_rows.")
        detail = session.http.get(BATCH.format(client_id=session.assigned_id, batch_id=batch_id))
        if (detail.json().get("mapping") or {}).get("company_name") != "Company":
            _fail("GET batch must return the persisted mapping.")
        session.assert_frozen("Mapping save")
    finally:
        session.close()


def test_mapping_validation_rules() -> None:
    session = _Session()
    try:
        content = _csv_bytes(
            ["Company", "Email", "Full Name", "First"],
            [["Acme", "a@example.test", "Ada Lovelace", "Ada"]],
        )
        batch_id = _upload(
            session.http, session.assigned_id, session.csrf, "rules.csv", content
        ).json()["batch"]["batch_id"]
        before_rows = _row_fingerprint(batch_id)
        cases = (
            ({}, MAPPING_COMPANY_REQUIRED),
            ({"contact_email": "Email"}, MAPPING_COMPANY_REQUIRED),
            ({"company_name": "Company", "nickname": "Email"}, MAPPING_UNKNOWN_FIELD),
            ({"company_name": "NotAHeader"}, MAPPING_HEADER_MISSING),
            ({"company_name": "   "}, MAPPING_EMPTY_HEADER),
            ({"company_name": "Company", "website": "Company"}, MAPPING_DUPLICATE_HEADER),
            (
                {
                    "company_name": "Company",
                    "contact_full_name": "Full Name",
                    "contact_first_name": "First",
                },
                MAPPING_NAME_CONFLICT,
            ),
        )
        for mapping, expected in cases:
            resp = _put_mapping(session, batch_id, mapping)
            if resp.status_code != 400:
                _fail(f"Invalid mapping {mapping!r} must be 400, got {resp.status_code}.")
            if expected not in str(resp.json().get("detail")):
                _fail(f"Invalid mapping {mapping!r} must explain {expected}.")
        ok_first = _put_mapping(
            session,
            batch_id,
            {"company_name": "Company", "contact_first_name": "First"},
        )
        if ok_first.status_code != 200:
            _fail("First name without last name must be allowed.")
        ok_last = _put_mapping(
            session,
            batch_id,
            {"company_name": "Company", "contact_last_name": "First"},
        )
        if ok_last.status_code != 200:
            _fail("Last name without first name must be allowed.")
        if _row_fingerprint(batch_id) != before_rows:
            _fail("Rejected mappings must not alter staged rows.")
        other = _put_mapping(
            session,
            batch_id,
            {"company_name": "Company"},
            client_id=session.other_id,
        )
        if other.status_code != 404:
            _fail(f"Mapping must be client-scoped, got {other.status_code}.")
        session.assert_frozen("Mapping validation")
    finally:
        session.close()


def test_mapping_refuses_cancelled_expired_and_empty_batches() -> None:
    session = _Session()
    try:
        content = _csv_bytes(["Company"], [["Acme"]])
        batch_id = _upload(
            session.http, session.assigned_id, session.csrf, "refuse.csv", content
        ).json()["batch"]["batch_id"]
        session.http.delete(
            BATCH.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
        )
        cancelled = _put_mapping(session, batch_id, {"company_name": "Company"})
        if cancelled.status_code != 409:
            _fail(f"Cancelled mapping must be 409, got {cancelled.status_code}.")
        if BATCH_NOT_REUSABLE not in str(cancelled.json().get("detail")):
            _fail("Cancelled mapping must not be reusable.")

        empty_id = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "empty.csv",
            _csv_bytes(["Company"], []),
        ).json()["batch"]["batch_id"]
        empty = _put_mapping(session, empty_id, {"company_name": "Company"})
        if empty.status_code != 409:
            _fail(f"Zero-row mapping must be 409, got {empty.status_code}.")
        if MAPPING_NO_ROWS not in str(empty.json().get("detail")):
            _fail("Zero-row mapping must explain that there are no staged rows.")

        live_id = _upload(
            session.http, session.assigned_id, session.csrf, "expire-map.csv", content
        ).json()["batch"]["batch_id"]
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_connection() as conn:
            conn.execute(
                "UPDATE crm_import_batches SET expires_at = ? WHERE id = ?",
                (past, live_id),
            )
            conn.commit()
        expired = _put_mapping(session, live_id, {"company_name": "Company"})
        if expired.status_code != 409:
            _fail(f"Expired mapping must be 409, got {expired.status_code}.")
        session.assert_frozen("Mapping refusal")
    finally:
        session.close()


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    test_unauthenticated_and_non_admin_are_denied()
    test_zero_client_and_unassigned_client()
    test_csv_preview_does_not_write_crm()
    test_xlsx_formula_is_literal_text()
    test_ordinary_long_cell_is_bounded_and_blocking()
    test_notes_long_text_is_preserved_with_display_cap()
    test_too_many_columns_fails_without_partial_rows()
    test_row_limit_and_file_size_leave_no_partial_rows()
    test_malformed_xlsx_creates_failed_batch_without_rows()
    test_multi_sheet_selection_does_not_write_until_chosen()
    test_single_visible_sheet_autoselects()
    test_cancel_keeps_audit_and_cannot_be_reused()
    test_expired_batch_purges_rows_and_cannot_be_reused()
    test_mapping_schema_is_idempotent()
    test_mapping_auth_and_scope()
    test_mapping_persists_and_does_not_write_crm_or_rows()
    test_mapping_validation_rules()
    test_mapping_refuses_cancelled_expired_and_empty_batches()
    print("test_crm_import_staging: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
