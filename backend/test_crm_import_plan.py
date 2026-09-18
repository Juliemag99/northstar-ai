"""Checkpoint C2 — read-only CRM import dry-run.

Run: python test_crm_import_plan.py

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
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from auth_http import (
    ADMIN_REQUIRED_DETAIL,
    AUTH_REQUIRED_DETAIL,
    CSRF_FAILED_DETAIL,
    CSRF_HEADER,
)
from auth_passwords import hash_password
from contact_phone import upsert_contact_phone_keys
from crm_import_plan import (
    INSUFFICIENT_CONTACT_DATA,
    INVALID_PAGING,
    INVALID_SAVED_MAPPING,
    MAX_DRY_RUN_PAGE,
    PLANNER_VERSION,
    dry_run_crm_import,
    plan_crm_import_batch,
)
from crm_import_staging import (
    BATCH_NOT_REUSABLE,
    CELL_PREVIEW_CHARS,
    MAPPING_COMPANY_REQUIRED,
    MAPPING_DUPLICATE_HEADER,
    MAPPING_HEADER_MISSING,
    MAPPING_NAME_CONFLICT,
    MAPPING_NO_ROWS,
    MAPPING_UNKNOWN_FIELD,
)
from db import PRODUCTION_DB_PATH, get_connection
from main import app

UPLOAD = "/api/clients/{client_id}/admin/imports"
BATCH = "/api/clients/{client_id}/admin/imports/{batch_id}"
MAPPING = "/api/clients/{client_id}/admin/imports/{batch_id}/mapping"
DRY_RUN = "/api/clients/{client_id}/admin/imports/{batch_id}/dry-run"

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


def _prove_isolated() -> None:
    from db import DB_PATH

    opened = Path(os.fspath(DB_PATH)).resolve()
    prod = PRODUCTION_DB_PATH.resolve()
    if opened == prod:
        _fail("Testdb isolation is pointing at live northstar.db.")
    env = Path(os.environ.get("NORTHSTAR_TEST_DB", "")).resolve()
    if env != opened:
        _fail("NORTHSTAR_TEST_DB does not match the opened database.")
    print(f"ISOLATED {opened}")


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
    if "raw_json" in dumped:
        _fail("Dry-run leaked raw_json.")
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
            (email, "CRM Import Plan Test User", int(administrator), int(active), digest),
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


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


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


def _batch_image(batch_id: int) -> tuple:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM crm_import_batches WHERE id = ?", (batch_id,)
        ).fetchone()
    if row is None:
        _fail(f"Batch {batch_id} missing.")
    return tuple(row)


def _schema_sql() -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'crm_import_batches'"
        ).fetchone()
        info = tuple(
            (r["name"], r["type"], r["notnull"], r["dflt_value"])
            for r in conn.execute("PRAGMA table_info(crm_import_batches)")
        )
    return json.dumps({"sql": None if row is None else row["sql"], "info": info})


def _upload(http: TestClient, client_id: int, csrf: str, filename: str, content: bytes):
    return http.post(
        UPLOAD.format(client_id=client_id),
        headers={CSRF_HEADER: csrf},
        files={"file": (filename, content, "application/octet-stream")},
    )


def _put_mapping(http: TestClient, client_id: int, csrf: str, batch_id: int, mapping: dict[str, str]):
    return http.put(
        MAPPING.format(client_id=client_id, batch_id=batch_id),
        headers={CSRF_HEADER: csrf},
        json={"mapping": mapping},
    )


def _dry_run(
    http: TestClient,
    client_id: int,
    csrf: str,
    batch_id: int,
    *,
    offset: int = 0,
    limit: int = 100,
):
    return http.post(
        DRY_RUN.format(client_id=client_id, batch_id=batch_id),
        headers={CSRF_HEADER: csrf},
        json={"offset": offset, "limit": limit},
    )


def _insert_company(name: str, **fields) -> int:
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, address, city, state, zip,
                legacy_phone, created_at, last_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields.get("record_no") or f"NS-C2-{secrets.token_hex(4)}",
                name,
                fields.get("website") or "",
                fields.get("address") or "",
                fields.get("city") or "",
                fields.get("state") or "",
                fields.get("zip") or "",
                fields.get("phone") or "",
                now,
                now,
            ),
        )
        company_id = int(cur.lastrowid)
        conn.commit()
    return company_id


def _insert_contact(company_id: int, **fields) -> int:
    with get_connection() as conn:
        record = conn.execute(
            "SELECT external_record_no FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title, phone, alt_phone, email,
                source_row_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                company_id,
                "" if record is None else str(record["external_record_no"] or ""),
                fields.get("first") or "",
                fields.get("last") or "",
                fields.get("title") or "",
                fields.get("phone") or "",
                fields.get("alt_phone") or "",
                fields.get("email") or "",
            ),
        )
        contact_id = int(cur.lastrowid)
        upsert_contact_phone_keys(
            conn,
            contact_id,
            fields.get("phone") or "",
            fields.get("alt_phone") or "",
        )
        conn.commit()
    return contact_id


def _insert_ccr(client_id: int, company_id: int) -> int:
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with get_connection() as conn:
        record = conn.execute(
            "SELECT external_record_no FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        cur = conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, status, assigned_user_id, notes, created_at, updated_at,
                external_record_no
            ) VALUES (?, ?, 'New', NULL, '', ?, ?, ?)
            """,
            (
                client_id,
                company_id,
                now,
                now,
                "" if record is None else str(record["external_record_no"] or ""),
            ),
        )
        rel_id = int(cur.lastrowid)
        conn.commit()
    return rel_id


def _delete_company(company_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM client_company_relationships WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        conn.commit()


class _Session:
    def __init__(self):
        assigned_id, other_id = _client_ids()
        self.assigned_id = assigned_id
        self.other_id = other_id
        self.password = _secret_password()
        self.email = f"import.plan.{secrets.token_hex(4)}@example.test"
        self.user_id = _create_user(email=self.email, password=self.password, administrator=1)
        _assign(self.user_id, assigned_id)
        self.http = _client()
        login = _login(self.http, self.email, self.password)
        if login.status_code != 200:
            _fail(f"Admin login failed: {login.status_code}")
        self.csrf = _csrf(login)
        self.crm_before = _crm_counts()
        self.cleanup_companies: list[int] = []

    def close(self) -> None:
        for company_id in self.cleanup_companies:
            _delete_company(company_id)
        _delete_user(self.user_id)

    def track_company(self, company_id: int) -> int:
        self.cleanup_companies.append(company_id)
        return company_id

    def snapshot_crm(self) -> None:
        self.crm_before = _crm_counts()

    def assert_frozen(self, label: str) -> None:
        _assert_crm_frozen(self.crm_before, label)


def _mapped_batch(session: _Session, headers: list[str], rows: list[list[object]], mapping: dict[str, str]):
    uploaded = _upload(
        session.http,
        session.assigned_id,
        session.csrf,
        "plan.csv",
        _csv_bytes(headers, rows),
    )
    if uploaded.status_code != 200:
        _fail(f"Upload failed: {uploaded.status_code} {uploaded.text}")
    batch = uploaded.json().get("batch") or {}
    batch_id = int(batch["batch_id"])
    mapped = _put_mapping(session.http, session.assigned_id, session.csrf, batch_id, mapping)
    if mapped.status_code != 200:
        _fail(f"Mapping failed: {mapped.status_code} {mapped.text}")
    return batch_id


def _assert_plan_meta(payload: dict, batch_id: int, session: _Session) -> None:
    if payload.get("batch_id") != batch_id:
        _fail("Dry-run batch_id mismatch.")
    if payload.get("client_id") != session.assigned_id:
        _fail("Dry-run client_id mismatch.")
    if payload.get("planner_version") != PLANNER_VERSION:
        _fail("planner_version mismatch.")
    if not payload.get("plan_fingerprint"):
        _fail("plan_fingerprint missing.")
    counts = payload.get("counts") or {}
    total = int(payload.get("total_rows") or 0)
    if int(counts.get("importable_rows") or 0) + int(counts.get("needs_review_rows") or 0) != total:
        _fail("importable_rows and needs_review_rows must partition total_rows.")
    _assert_no_secrets(payload, session.password, session.csrf)


def test_auth_and_state_gates() -> None:
    session = _Session()
    try:
        batch_id = _mapped_batch(
            session,
            ["Company", "Email"],
            [["AuthCo", "a@example.test"]],
            {"company_name": "Company", "contact_email": "Email"},
        )
        anon = _client().post(
            DRY_RUN.format(client_id=session.assigned_id, batch_id=batch_id),
            json={"offset": 0, "limit": 100},
        )
        if anon.status_code != 401:
            _fail(f"Anonymous dry-run must be 401, got {anon.status_code}.")
        if anon.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail("Anonymous 401 detail was not generic.")

        fresh = _client()
        login = _login(fresh, session.email, session.password)
        csrf = _csrf(login)
        no_csrf = fresh.post(
            DRY_RUN.format(client_id=session.assigned_id, batch_id=batch_id),
            json={"offset": 0, "limit": 100},
        )
        if no_csrf.status_code != 403:
            _fail(f"Missing CSRF must be 403, got {no_csrf.status_code}.")
        if no_csrf.json().get("detail") != CSRF_FAILED_DETAIL:
            _fail("Missing CSRF detail drifted.")
        wrong = fresh.post(
            DRY_RUN.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: "x" * max(16, len(csrf))},
            json={"offset": 0, "limit": 100},
        )
        if wrong.status_code != 403:
            _fail(f"Wrong CSRF must be 403, got {wrong.status_code}.")

        staff_http = _client()
        staff_pass = _secret_password()
        staff_email = f"import.plan.staff.{secrets.token_hex(4)}@example.test"
        staff_id = _create_user(email=staff_email, password=staff_pass, administrator=0)
        _assign(staff_id, session.assigned_id)
        try:
            staff_login = _login(staff_http, staff_email, staff_pass)
            staff_csrf = _csrf(staff_login)
            denied = staff_http.post(
                DRY_RUN.format(client_id=session.assigned_id, batch_id=batch_id),
                headers={CSRF_HEADER: staff_csrf},
                json={"offset": 0, "limit": 100},
            )
            if denied.status_code != 403:
                _fail(f"Non-admin dry-run must be 403, got {denied.status_code}.")
            if denied.json().get("detail") != ADMIN_REQUIRED_DETAIL:
                _fail("Non-admin 403 detail was not generic.")
        finally:
            _delete_user(staff_id)

        other = _dry_run(session.http, session.other_id, session.csrf, batch_id)
        if other.status_code != 404:
            _fail(f"Client mismatch must be 404, got {other.status_code}.")

        session.http.delete(
            BATCH.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
        )
        cancelled = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if cancelled.status_code != 409:
            _fail(f"Cancelled dry-run must be 409, got {cancelled.status_code}.")
        if BATCH_NOT_REUSABLE not in str(cancelled.json().get("detail")):
            _fail("Cancelled dry-run must not be reusable.")

        failed_id = _mapped_batch(
            session,
            ["Company"],
            [["FailCo"]],
            {"company_name": "Company"},
        )
        with get_connection() as conn:
            conn.execute("UPDATE crm_import_batches SET status = 'failed' WHERE id = ?", (failed_id,))
            conn.commit()
        failed = _dry_run(session.http, session.assigned_id, session.csrf, failed_id)
        if failed.status_code != 409:
            _fail(f"Failed dry-run must be 409, got {failed.status_code}.")

        empty_id = int(
            _upload(
                session.http,
                session.assigned_id,
                session.csrf,
                "empty.csv",
                _csv_bytes(["Company"], []),
            ).json()["batch"]["batch_id"]
        )
        _put_mapping(
            session.http,
            session.assigned_id,
            session.csrf,
            empty_id,
            {"company_name": "Company"},
        )
        # Mapping on zero rows is 409; set mapping via SQL so dry-run can see a mapped empty batch.
        with get_connection() as conn:
            conn.execute(
                "UPDATE crm_import_batches SET mapping_json = ? WHERE id = ?",
                ('{"company_name":"Company"}', empty_id),
            )
            conn.commit()
        empty = _dry_run(session.http, session.assigned_id, session.csrf, empty_id)
        if empty.status_code != 409:
            _fail(f"Zero-row dry-run must be 409, got {empty.status_code}.")
        if MAPPING_NO_ROWS not in str(empty.json().get("detail")):
            _fail("Zero-row dry-run must explain there are no staged rows.")

        expired_id = _mapped_batch(
            session,
            ["Company"],
            [["ExpireCo"]],
            {"company_name": "Company"},
        )
        before_rows = _row_fingerprint(expired_id)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_connection() as conn:
            conn.execute(
                "UPDATE crm_import_batches SET expires_at = ? WHERE id = ?",
                (past, expired_id),
            )
            conn.commit()
        expired = _dry_run(session.http, session.assigned_id, session.csrf, expired_id)
        if expired.status_code != 409:
            _fail(f"Expired dry-run must be 409, got {expired.status_code}.")
        if _row_fingerprint(expired_id) != before_rows:
            _fail("Expired dry-run must not purge staged rows.")
        session.assert_frozen("Auth/state gates")
    finally:
        session.close()


def test_mapping_gates() -> None:
    session = _Session()
    try:
        uploaded = _upload(
            session.http,
            session.assigned_id,
            session.csrf,
            "map.csv",
            _csv_bytes(["Company", "Email", "First", "Full"], [["Acme", "a@x.test", "Ada", "Ada Lovelace"]]),
        )
        batch_id = int(uploaded.json()["batch"]["batch_id"])
        empty = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if empty.status_code != 400:
            _fail(f"Empty mapping must be 400, got {empty.status_code}.")
        if MAPPING_COMPANY_REQUIRED not in str(empty.json().get("detail")):
            _fail("Empty mapping must require company_name.")

        with get_connection() as conn:
            conn.execute(
                "UPDATE crm_import_batches SET mapping_json = ? WHERE id = ?",
                ("{not-json", batch_id),
            )
            conn.commit()
        malformed = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if malformed.status_code != 400:
            _fail(f"Malformed mapping must be 400, got {malformed.status_code}.")
        if INVALID_SAVED_MAPPING not in str(malformed.json().get("detail")):
            _fail("Malformed mapping must fail safely.")

        cases = (
            ('{"website":"Email"}', MAPPING_COMPANY_REQUIRED),
            ('{"nickname":"Company"}', MAPPING_UNKNOWN_FIELD),
            ('{"company_name":"MissingHeader"}', MAPPING_HEADER_MISSING),
            ('{"company_name":"Company","website":"Company"}', MAPPING_DUPLICATE_HEADER),
            (
                '{"company_name":"Company","contact_full_name":"Full","contact_first_name":"First"}',
                MAPPING_NAME_CONFLICT,
            ),
        )
        for raw, expected in cases:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE crm_import_batches SET mapping_json = ? WHERE id = ?",
                    (raw, batch_id),
                )
                conn.commit()
            resp = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
            if resp.status_code != 400:
                _fail(f"Invalid stored mapping {raw} must be 400, got {resp.status_code}.")
            if expected not in str(resp.json().get("detail")):
                _fail(f"Invalid stored mapping {raw} must explain {expected}.")

        paging = session.http.post(
            DRY_RUN.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
            json={"offset": -1, "limit": 100},
        )
        if paging.status_code != 400 or INVALID_PAGING not in str(paging.json().get("detail")):
            _fail("Negative offset must be 400 invalid paging.")
        big = session.http.post(
            DRY_RUN.format(client_id=session.assigned_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
            json={"offset": 0, "limit": MAX_DRY_RUN_PAGE + 1},
        )
        if big.status_code != 400:
            _fail("Limit over 100 must be 400.")
        session.assert_frozen("Mapping gates")
    finally:
        session.close()


def test_planning_classifications() -> None:
    session = _Session()
    try:
        marker = secrets.token_hex(6)
        existing = session.track_company(
            _insert_company(
                f"C2 Exist {marker}",
                website="https://exist-{0}.example.test".format(marker),
                address="100 Main St",
                city="Dallas",
                state="TX",
                phone="2145550100",
            )
        )
        other = session.track_company(_insert_company(f"C2 Other {marker}"))
        twin_a = session.track_company(_insert_company(f"C2 Twin {marker}", city="Austin", state="TX"))
        twin_b = session.track_company(_insert_company(f"C2 Twin {marker}", city="Houston", state="TX"))
        related = session.track_company(
            _insert_company(
                f"C2 Related {marker}",
                website="https://related-{0}.example.test".format(marker),
            )
        )
        _insert_ccr(session.assigned_id, related)
        contact_email = _insert_contact(
            existing, first="Ada", last="Lovelace", email=f"ada.{marker}@exist.test"
        )
        contact_phone = _insert_contact(
            existing, first="Grace", last="Hopper", phone="(214) 555-0199"
        )
        contact_name = _insert_contact(existing, first="Alan", last="Turing")
        _insert_contact(other, first="Alan", last="Turing", email=f"alan.{marker}@other.test")
        last7 = _insert_contact(existing, first="Last", last="Seven", phone="4695550199")
        session.snapshot_crm()

        long_cell = "Z" * (CELL_PREVIEW_CHARS + 40)
        batch_id = _mapped_batch(
            session,
            ["Company", "Website", "Phone", "Address", "City", "State", "Email", "First", "Last", "CPhone"],
            [
                [f"C2 Block {marker}", "", "", "", "", "", "", "", "", ""],
                ["", "", "", "", "", "", "blank.row@example.test", "", "", ""],
                [
                    f"C2 Exist {marker}",
                    f"https://exist-{marker}.example.test",
                    "",
                    "",
                    "",
                    "",
                    f"ada.{marker}@exist.test",
                    "",
                    "",
                    "",
                ],
                [
                    f"C2 Exist {marker}",
                    f"https://exist-{marker}.example.test",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "2145550199",
                ],
                [
                    f"C2 Exist {marker}",
                    f"https://exist-{marker}.example.test",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Alan",
                    "Turing",
                    "",
                ],
                [
                    f"C2 Exist {marker}",
                    f"https://exist-{marker}.example.test",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Alan",
                    "Turing",
                    "",
                ],
                [
                    f"C2 Exist {marker}",
                    f"https://exist-{marker}.example.test",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Last",
                    "Seven",
                    "5550199",
                ],
                [f"C2 New {marker}", "", "", "", "", "", "", "", "", ""],
                [f"C2 New {marker}", "", "", "", "", "", f"same.{marker}@new.test", "Sam", "Same", ""],
                [f"C2 New {marker}", "", "", "", "", "", f"same.{marker}@new.test", "Other", "Person", ""],
                [f"C2 New {marker}", "", "", "", "", "", "", "Sam", "Same", ""],
                [f"C2 NewPhone {marker}", "", "", "", "", "", "", "Pat", "Phone", "5125550101"],
                [f"C2 NewPhone {marker}", "", "", "", "", "", "", "Pat", "Clone", "5125550101"],
                [f"C2 Twin {marker}", "", "", "", "", "", "", "", "", ""],
                [
                    f"C2 Related {marker}",
                    f"https://related-{marker}.example.test",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ],
                [f"C2 Unique {marker}", "https://unique-{0}.example.test".format(marker), "", "", "", "", "", "", "", ""],
                [long_cell, "", "", "", "", "", "", "", "", ""],
            ],
            {
                "company_name": "Company",
                "website": "Website",
                "phone": "Phone",
                "address": "Address",
                "city": "City",
                "state": "State",
                "contact_email": "Email",
                "contact_first_name": "First",
                "contact_last_name": "Last",
                "contact_phone": "CPhone",
            },
        )
        # Row 1 blank company; add a blocking error on a dedicated extra upload row via SQL.
        with get_connection() as conn:
            extra = conn.execute(
                """
                SELECT id FROM crm_import_rows
                WHERE batch_id = ? ORDER BY source_row_number, id LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE crm_import_rows
                SET has_blocking_error = 1
                WHERE id = ?
                """,
                (int(extra["id"]),),
            )
            conn.commit()

        image = _batch_image(batch_id)
        rows_fp = _row_fingerprint(batch_id)
        schema_before = _schema_sql()
        resp = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if resp.status_code != 200:
            _fail(f"Planning dry-run must be 200, got {resp.status_code}: {resp.text}")
        payload = resp.json()
        _assert_plan_meta(payload, batch_id, session)
        if _batch_image(batch_id) != image:
            _fail("Dry-run changed crm_import_batches.")
        if _row_fingerprint(batch_id) != rows_fp:
            _fail("Dry-run changed crm_import_rows.")
        if _schema_sql() != schema_before:
            _fail("Dry-run migrated schema.")
        session.assert_frozen("Planning classifications")

        rows = payload["rows"]
        if payload["total_rows"] != 17:
            _fail(f"Expected 17 staged rows, got {payload['total_rows']}.")
        if len(rows) != 17:
            _fail("Default page should include all 17 rows.")

        blocked = next(r for r in rows if r["validity"] == "blocking_error")
        if blocked["company"]["action"] != "none":
            _fail("Blocking rows must not plan a company.")

        # Blank company_name is the second staged row.
        blank = next(r for r in rows if r["validity"] == "invalid_mapping_data")
        if blank["company"]["action"] != "none":
            _fail("Blank company_name must not plan a company.")

        email_row = next(
            r
            for r in rows
            if r["contact"]["action"] == "use_existing_contact"
            and "email_exact" in r["contact"]["reasons"]
        )
        if email_row["contact"]["contact_id"] != contact_email:
            _fail("Exact email must use the existing contact.")
        if email_row["company"]["company_id"] != existing:
            _fail("Exact email row must use the existing company.")

        phone_row = next(
            r
            for r in rows
            if r["contact"]["action"] == "use_existing_contact"
            and "phone_exact" in r["contact"]["reasons"]
        )
        if phone_row["contact"]["contact_id"] != contact_phone:
            _fail("Exact NANP10 must use the existing contact.")

        name_rows = [
            r
            for r in rows
            if r["company"].get("company_id") == existing
            and r["contact"]["action"] == "possible_contact_match"
            and (
                (
                    "name_exact" in r["contact"]["reasons"]
                    and r["contact"].get("contact_id") == contact_name
                )
                or any(
                    p.get("contact_id") == contact_name
                    and "name_exact" in (p.get("reasons") or [])
                    for p in r["contact"]["possibles"]
                )
            )
        ]
        if not name_rows:
            _fail("Name-only existing contact must be possible_contact_match.")
        leaked = _dump(name_rows)
        if f"alan.{marker}@other.test" in leaked.lower():
            _fail("Possible contact must not expose another person's email.")

        last7_row = next(
            r
            for r in rows
            if "phone_last7" in r["contact"]["reasons"]
            or any("phone_last7" in (p.get("reasons") or []) for p in r["contact"]["possibles"])
        )
        if last7_row["contact"]["action"] != "possible_contact_match":
            _fail("Last-seven phone must be possible_contact_match.")
        if not any(p.get("contact_id") == last7 for p in last7_row["contact"]["possibles"]):
            _fail("Last-seven possible list must include the DB contact id.")

        new_company_creates = [
            r for r in rows if r["company"]["action"] == "create_company" and f"C2 New {marker}" in (r["company"].get("name") or r["mapped"].get("company_name") or "")
        ]
        if not new_company_creates:
            _fail("First unique new company row must create_company.")
        reused = [
            r
            for r in rows
            if r["company"]["action"] == "use_existing_company"
            and r["company"].get("proposed_key")
            and f"C2 New {marker}" in (r["mapped"].get("company_name") or "")
        ]
        if len(reused) < 3:
            _fail("Later rows must reuse the proposed company.")
        email_reuse = next(
            r
            for r in reused
            if r["contact"]["action"] == "use_existing_contact"
            and "email_exact" in r["contact"]["reasons"]
        )
        if not email_reuse["contact"].get("proposed_key"):
            _fail("Within-batch exact email must reuse the proposed contact.")
        name_proposed = next(
            r
            for r in reused
            if r["contact"]["action"] == "possible_contact_match"
            and "name_exact" in r["contact"]["reasons"]
        )
        if name_proposed["contact"].get("contact_id"):
            _fail("Name-only proposed contact must not auto-link a database id.")

        phone_create = next(
            r
            for r in rows
            if r["mapped"].get("company_name") == f"C2 NewPhone {marker}"
            and r["company"]["action"] == "create_company"
        )
        phone_reuse = next(
            r
            for r in rows
            if r["mapped"].get("company_name") == f"C2 NewPhone {marker}"
            and r["contact"]["action"] == "use_existing_contact"
            and "phone_exact" in r["contact"]["reasons"]
        )
        if not phone_reuse["contact"].get("proposed_key"):
            _fail("Within-batch exact NANP10 must reuse the proposed contact.")
        if phone_create["relationship"]["action"] != "create_client_relationship":
            _fail("First resolved company must create_client_relationship.")
        if phone_reuse["relationship"]["action"] != "relationship_already_exists":
            _fail("Later row must see the earlier proposed relationship.")

        twin = next(r for r in rows if r["mapped"].get("company_name") == f"C2 Twin {marker}")
        if twin["company"]["action"] != "possible_company_match":
            _fail("Multiple normalized-name companies must be possible_company_match.")
        if twin["contact"]["action"] != "deferred" or twin["relationship"]["action"] != "deferred":
            _fail("Possible company must defer contact and relationship.")
        twin_ids = {p["company_id"] for p in twin["company"]["possibles"]}
        if twin_a not in twin_ids or twin_b not in twin_ids:
            _fail("Possible company list must include both database ids.")

        related_row = next(
            r for r in rows if r["mapped"].get("company_name") == f"C2 Related {marker}"
        )
        if related_row["relationship"]["action"] != "relationship_already_exists":
            _fail("Existing CCR must be relationship_already_exists.")
        if related_row["contact"]["action"] != "no_contact_data":
            _fail("Company-only row must be no_contact_data.")

        unique = next(
            r for r in rows if r["mapped"].get("company_name") == f"C2 Unique {marker}"
        )
        if unique["company"]["action"] != "create_company":
            _fail("Unique new company with website must create_company.")
        create_key = unique["company"].get("proposed_key") or ""
        if create_key != f"proposed:company:{unique['source_row_number']}:{unique['row_id']}":
            _fail(f"Proposed company key must include source_row_number and row_id, got {create_key!r}.")

        capped = next(r for r in rows if (r["mapped"].get("company_name") or "").startswith("Z"))
        if len(capped["mapped"].get("company_name") or "") > CELL_PREVIEW_CHARS:
            _fail("Mapped values must be capped at 200 characters.")

        counts = payload["counts"]
        if counts["possible_company_match"] < 1:
            _fail("Counts must include possible_company_match.")
        if counts["use_existing_contact"] < 1:
            _fail("Counts must include use_existing_contact.")
    finally:
        session.close()


def test_fingerprint_and_pagination() -> None:
    session = _Session()
    try:
        marker = secrets.token_hex(6)
        existing = session.track_company(_insert_company(f"C2 Finger {marker}"))
        session.snapshot_crm()
        batch_id = _mapped_batch(
            session,
            ["Company", "Email"],
            [
                [f"C2 Finger {marker}", f"one.{marker}@finger.test"],
                [f"C2 Finger New {marker}", f"two.{marker}@finger.test"],
                [f"C2 Finger New {marker}", f"two.{marker}@finger.test"],
            ],
            {"company_name": "Company", "contact_email": "Email"},
        )
        first = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        second = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        page = _dry_run(session.http, session.assigned_id, session.csrf, batch_id, offset=1, limit=1)
        if first.status_code != 200 or second.status_code != 200 or page.status_code != 200:
            _fail("Fingerprint dry-runs must be 200.")
        fp = first.json()["plan_fingerprint"]
        if fp != second.json()["plan_fingerprint"]:
            _fail("Repeated unchanged dry-runs must have the same fingerprint.")
        if fp != page.json()["plan_fingerprint"]:
            _fail("Pagination must not change the fingerprint.")
        if page.json()["offset"] != 1 or len(page.json()["rows"]) != 1:
            _fail("Pagination must slice rows only.")
        if page.json()["counts"] != first.json()["counts"]:
            _fail("Pagination must keep full-batch counts.")

        extra = session.track_company(_insert_company(f"C2 Finger {marker}"))
        session.snapshot_crm()
        crm_changed = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if crm_changed.json()["plan_fingerprint"] == fp:
            _fail("A CRM change that adds a name twin must change the fingerprint.")
        twin_row = next(
            r
            for r in crm_changed.json()["rows"]
            if r["mapped"].get("company_name") == f"C2 Finger {marker}"
        )
        if twin_row["company"]["action"] != "possible_company_match":
            _fail("Adding a second same-name company must make the row possible.")

        fp2 = crm_changed.json()["plan_fingerprint"]
        irrelevant = session.track_company(_insert_company(f"C2 Irrelevant {marker}"))
        session.snapshot_crm()
        same = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if same.json()["plan_fingerprint"] != fp2:
            _fail("Irrelevant CRM company must not change the fingerprint.")

        _put_mapping(
            session.http,
            session.assigned_id,
            session.csrf,
            batch_id,
            {"company_name": "Company"},
        )
        mapped = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if mapped.json()["plan_fingerprint"] == fp2:
            _fail("Mapping change must change the fingerprint.")

        fp3 = mapped.json()["plan_fingerprint"]
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, raw_json FROM crm_import_rows
                WHERE batch_id = ?
                ORDER BY source_row_number, id
                LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
            payload = json.loads(row["raw_json"])
            payload["Company"] = f"C2 Finger Changed {marker}"
            conn.execute(
                "UPDATE crm_import_rows SET raw_json = ? WHERE id = ?",
                (json.dumps(payload), int(row["id"])),
            )
            conn.commit()
        changed_row = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if changed_row.json()["plan_fingerprint"] == fp3:
            _fail("Staged-row content change must change the fingerprint.")
        _ = (existing, extra, irrelevant)
        session.assert_frozen("Fingerprint")
    finally:
        session.close()


def test_query_only_and_query_count() -> None:
    session = _Session()
    try:
        marker = secrets.token_hex(6)
        rows = [[f"C2 Perf {marker} {i}", f"p{i}.{marker}@perf.test"] for i in range(200)]
        batch_id = _mapped_batch(
            session,
            ["Company", "Email"],
            rows,
            {"company_name": "Company", "contact_email": "Email"},
        )
        schema_before = _schema_sql()
        image = _batch_image(batch_id)
        row_fp = _row_fingerprint(batch_id)
        session.snapshot_crm()

        statements: list[str] = []

        class _CountingConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                statements.append(" ".join(str(sql).split()))
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        with get_connection() as conn:
            wrapped = _CountingConn(conn)
            plan = plan_crm_import_batch(wrapped, client_id=session.assigned_id, batch_id=batch_id)
            if plan.total_rows != 200:
                _fail("200-row plan must include every staged row.")
            if plan.stats.get("retained_companies", 1) != 0:
                _fail("Unique new names should retain no CRM companies.")
            if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 0:
                _fail("plan_crm_import_batch must not set PRAGMA query_only.")

        selects = [s for s in statements if s.upper().startswith("SELECT") or " FROM " in s.upper()]
        if len(statements) > 30:
            _fail(f"Too many SQL statements for a 200-row plan: {len(statements)}")
        for table in ("companies", "contacts", "crm_import_rows", "client_company_relationships"):
            hits = [s for s in statements if table in s.lower()]
            if len(hits) >= 200:
                _fail(f"Per-row SQL detected for {table}: {len(hits)} statements")
        http = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if http.status_code != 200:
            _fail(f"200-row HTTP dry-run failed: {http.status_code}")
        if http.json()["total_rows"] != 200:
            _fail("HTTP dry-run must plan all 200 rows.")
        if _schema_sql() != schema_before or _batch_image(batch_id) != image:
            _fail("Performance dry-run mutated schema or batch.")
        if _row_fingerprint(batch_id) != row_fp:
            _fail("Performance dry-run mutated staged rows.")
        session.assert_frozen("Query count")
        _ = selects
    finally:
        session.close()


def test_insufficient_contact_and_begin_immediate() -> None:
    session = _Session()
    try:
        marker = secrets.token_hex(6)
        batch_id = _mapped_batch(
            session,
            ["Company", "Title", "CPhone"],
            [
                [f"C2 Title {marker}", "Director", ""],
                [f"C2 PhoneOnly {marker}", "", "5125550999"],
            ],
            {
                "company_name": "Company",
                "contact_title": "Title",
                "contact_phone": "CPhone",
            },
        )
        image = _batch_image(batch_id)
        http = _dry_run(session.http, session.assigned_id, session.csrf, batch_id)
        if http.status_code != 200:
            _fail(f"Insufficient-contact dry-run must be 200, got {http.status_code}: {http.text}")
        payload = http.json()
        title_row = next(
            r for r in payload["rows"] if r["mapped"].get("company_name") == f"C2 Title {marker}"
        )
        phone_row = next(
            r for r in payload["rows"] if r["mapped"].get("company_name") == f"C2 PhoneOnly {marker}"
        )
        if title_row["contact"]["action"] != INSUFFICIENT_CONTACT_DATA:
            _fail("Title-only contact must be insufficient_contact_data.")
        if phone_row["contact"]["action"] != INSUFFICIENT_CONTACT_DATA:
            _fail("Unmatched phone-only contact must be insufficient_contact_data.")
        if int((payload.get("counts") or {}).get("insufficient_contact_data") or 0) != 2:
            _fail("Counts must include insufficient_contact_data.")
        if title_row["validity"] != "ok" or phone_row["validity"] != "ok":
            _fail("Insufficient contact data is a contact action, not a validity failure.")
        needs = int(payload["counts"]["needs_review_rows"])
        if needs < 2:
            _fail("Title-only and unmatched-phone-only rows must be needs-review.")
        for row in (title_row, phone_row):
            importable = (
                row["validity"] == "ok"
                and row["company"]["action"] in {"create_company", "use_existing_company"}
                and row["contact"]["action"]
                in {"create_contact", "use_existing_contact", "no_contact_data"}
                and row["relationship"]["action"]
                in {"create_client_relationship", "relationship_already_exists"}
            )
            if importable:
                _fail("Insufficient-contact rows must not be importable.")

        seen = {}
        real_plan = plan_crm_import_batch

        def _wrapped_plan(conn, *, client_id, batch_id, **kwargs):
            seen["query_only"] = int(conn.execute("PRAGMA query_only").fetchone()[0])
            return real_plan(conn, client_id=client_id, batch_id=batch_id, **kwargs)

        import crm_import_plan as plan_mod

        with patch.object(plan_mod, "plan_crm_import_batch", _wrapped_plan):
            plan_mod.dry_run_crm_import(session.assigned_id, batch_id, offset=0, limit=100)
        if seen.get("query_only") != 1:
            _fail("dry_run_crm_import must set PRAGMA query_only=ON before planning.")

        marker_write = f"c2-rollback-{secrets.token_hex(4)}"
        with get_connection() as conn:
            before_pragma = int(conn.execute("PRAGMA query_only").fetchone()[0])
            if before_pragma != 0:
                _fail("Fresh connection must not inherit query_only.")
            conn.execute("BEGIN IMMEDIATE")
            plan = plan_crm_import_batch(conn, client_id=session.assigned_id, batch_id=batch_id)
            after_pragma = int(conn.execute("PRAGMA query_only").fetchone()[0])
            if after_pragma != 0:
                _fail("plan_crm_import_batch must not set PRAGMA query_only.")
            conn.execute(
                "UPDATE crm_import_batches SET error_message = ? WHERE id = ?",
                (marker_write, batch_id),
            )
            written = conn.execute(
                "SELECT error_message FROM crm_import_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if str(written["error_message"]) != marker_write:
                _fail("Write after planner inside BEGIN IMMEDIATE must succeed.")
            conn.execute("ROLLBACK")
        if _batch_image(batch_id) != image:
            _fail("Rolled-back write after planner must not persist.")
        if plan.plan_fingerprint != payload["plan_fingerprint"]:
            _fail("BEGIN IMMEDIATE plan fingerprint must match dry-run fingerprint.")
        session.assert_frozen("Insufficient contact / IMMEDIATE")
    finally:
        session.close()


def test_direct_dry_run_wrapper_paging() -> None:
    session = _Session()
    try:
        batch_id = _mapped_batch(
            session,
            ["Company"],
            [["DirectCo"]],
            {"company_name": "Company"},
        )
        try:
            dry_run_crm_import(session.assigned_id, batch_id, offset=-1, limit=100)
            _fail("dry_run_crm_import must reject negative offset.")
        except ValueError as exc:
            if INVALID_PAGING not in str(exc):
                _fail("Paging ValueError must use INVALID_PAGING.")
        session.assert_frozen("Direct paging")
    finally:
        session.close()


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    _prove_isolated()
    test_auth_and_state_gates()
    test_mapping_gates()
    test_planning_classifications()
    test_fingerprint_and_pagination()
    test_query_only_and_query_count()
    test_insufficient_contact_and_begin_immediate()
    test_direct_dry_run_wrapper_paging()
    print("test_crm_import_plan: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
