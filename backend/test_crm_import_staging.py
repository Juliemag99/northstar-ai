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


def test_long_cell_is_bounded_and_blocking() -> None:
    session = _Session()
    try:
        huge = "x" * (MAX_CELL_CHARS + 5)
        content = _csv_bytes(["Name", "Notes"], [["Ada", huge]])
        resp = _upload(session.http, session.assigned_id, session.csrf, "long.csv", content)
        if resp.status_code != 200:
            _fail(f"Long-cell upload must be 200, got {resp.status_code}.")
        batch = resp.json()["batch"]
        if batch["error_row_count"] != 1:
            _fail("Overlong cells must count as blocking row errors.")
        row = batch["sample_rows"][0]
        stored = row["values"]["Notes"]
        if stored != ("x" * CELL_PREVIEW_CHARS) + "…":
            _fail("Overlong cells must keep only a bounded preview.")
        if huge in _dump(row):
            _fail("Overlong source text must not be retained in full.")
        if not row["has_blocking_error"] or not row["errors"]:
            _fail("Overlong cells must add a blocking row error.")
        session.assert_frozen("Long-cell preview")
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


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    test_unauthenticated_and_non_admin_are_denied()
    test_zero_client_and_unassigned_client()
    test_csv_preview_does_not_write_crm()
    test_xlsx_formula_is_literal_text()
    test_long_cell_is_bounded_and_blocking()
    test_too_many_columns_fails_without_partial_rows()
    test_row_limit_and_file_size_leave_no_partial_rows()
    test_malformed_xlsx_creates_failed_batch_without_rows()
    test_multi_sheet_selection_does_not_write_until_chosen()
    test_single_visible_sheet_autoselects()
    test_cancel_keeps_audit_and_cannot_be_reused()
    test_expired_batch_purges_rows_and_cannot_be_reused()
    print("test_crm_import_staging: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
