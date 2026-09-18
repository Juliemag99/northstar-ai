"""PR-2D — detailed Reports CSV: archived_at COUNT join, isolation, empty, archive.

Isolated testdb only. Never writes production northstar.db.
Never creates live Robert/Tyler/Todd accounts.
"""

from __future__ import annotations

import csv
import io
import os
import secrets
import time
from datetime import datetime, timezone

import testdb

from fastapi.testclient import TestClient

from auth_http import ENFORCE_FLAG
from auth_passwords import hash_password
from activities_data import insert_activity_row
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from staff_rbac import REVOPS_SPECIALIST, ensure_staff_rbac_schema

os.environ.pop(ENFORCE_FLAG, None)

MARKER = "NSPR2D"
DETAIL_COLUMNS = [
    "Client",
    "Company Name",
    "Company Record No.",
    "Contact Name",
    "Assigned Rep/User",
    "Date/Time",
    "Activity Or Record Type",
    "Status/Outcome",
    "Campaign",
    "Notes",
]
FORBIDDEN_HEADER_BITS = (
    "password",
    "csrf",
    "session",
    "token",
    "secret",
    "hash",
    "cookie",
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _clients() -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, code, name FROM clients").fetchall()
    out = {
        str(r["code"]).strip().lower(): {
            "id": int(r["id"]),
            "name": str(r["name"] or "").strip(),
        }
        for r in rows
    }
    for code in ("brown", "dawson", "premier", "carmeco"):
        if code not in out:
            _fail(f"isolated testdb missing client {code}")
    return out


def _create_specialist(email: str, password: str) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        ensure_staff_rbac_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, 0, 1, 1, ?, 0, '', ?)
            """,
            (email, f"PR2D {email.split('@')[0]}", digest, REVOPS_SPECIALIST),
        )
        user_id = int(conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"])
        conn.commit()
    return user_id


def _assign(user_id: int, client_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, 'staff', 1, datetime('now'))
            """,
            (int(user_id), int(client_id)),
        )
        conn.commit()


def _login(http: TestClient, email: str, password: str) -> str:
    resp = http.post("/api/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        _fail(f"login failed {resp.status_code} {resp.text}")
    return str(resp.json().get("csrf_token") or "")


def _data_rows(csv_text: str) -> tuple[list[str], list[list[str]]]:
    lines = csv_text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("Client,") or line == "Client":
            start = i
            break
    else:
        _fail("CSV missing Client header row")
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    rows = list(reader)
    if not rows:
        _fail("CSV header row missing after metadata")
    return rows[0], rows[1:]


def _client_column_values(csv_text: str) -> set[str]:
    header, rows = _data_rows(csv_text)
    if "Client" not in header:
        _fail(f"Client column missing: {header}")
    idx = header.index("Client")
    return {row[idx].strip() for row in rows if len(row) > idx and row[idx].strip()}


def test_production_path_untouched() -> None:
    live = str(PRODUCTION_DB_PATH).replace("\\", "/").lower()
    with get_connection() as conn:
        path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if path.replace("\\", "/").lower() == live:
        _fail(f"test connection is live production db: {path}")


def test_detailed_export_hot_count_join() -> None:
    """The original failure: detailed CSV walks hot_prospects COUNT without companies JOIN."""
    ids = _clients()
    brown = ids["brown"]["id"]
    http = testdb.test_client()
    resp = http.get(
        f"/api/reports/export?section=client-results&detail=true&client_id={brown}"
        "&date_from=2026-01-01&date_to=2026-12-31"
    )
    if resp.status_code != 200:
        _fail(f"Brown detailed export {resp.status_code} {resp.text[:400]}")
    text = resp.text
    if "no such column" in text.lower() or "archived_at" in text and "OperationalError" in text:
        _fail("detailed export still raises archived_at error")
    header, _rows = _data_rows(text)
    if header != DETAIL_COLUMNS:
        _fail(f"unexpected headers {header}")


def test_specialist_isolation_and_rbac() -> None:
    ids = _clients()
    password = _secret()
    pilots = [
        ("robert.pr2d@northstar.example.test", "brown", "Brown Industries"),
        ("tyler.pr2d@northstar.example.test", "dawson", "Dawson Fabrication"),
        ("todd.pr2d@northstar.example.test", "premier", "Premier Manufacturing"),
    ]
    other_names = {
        "Carmeco",
        "Brown Industries",
        "Dawson Fabrication",
        "Premier Manufacturing",
    }
    http = TestClient(app)
    qs = "date_from=2026-01-01&date_to=2026-12-31"
    for email, code, expected_name in pilots:
        uid = _create_specialist(email, password)
        _assign(uid, ids[code]["id"])
        _login(http, email, password)
        own = http.get(
            f"/api/reports/export?section=client-results&detail=true"
            f"&client_id={ids[code]['id']}&{qs}"
        )
        if own.status_code != 200:
            _fail(f"{email} own detailed export {own.status_code} {own.text[:300]}")
        clients_in_csv = _client_column_values(own.text)
        unexpected = clients_in_csv - {expected_name, ""}
        leaked = unexpected & (other_names - {expected_name})
        if leaked:
            _fail(f"{email} detailed CSV leaked other clients {leaked}")
        for other_code, other in ids.items():
            if other_code == code:
                continue
            denied = http.get(
                f"/api/reports/export?section=client-results&detail=true"
                f"&client_id={other['id']}&{qs}"
            )
            if denied.status_code != 403:
                _fail(
                    f"{email} other-client {other_code} expected 403, got {denied.status_code}"
                )
        http.post("/api/auth/logout")

    admin = testdb.test_client()
    admin_resp = admin.get(
        f"/api/reports/export?section=client-results&detail=true"
        f"&client_id={ids['brown']['id']}&{qs}"
    )
    if admin_resp.status_code != 200:
        _fail(f"administrator detailed export {admin_resp.status_code}")


def test_csv_content_and_empty_result() -> None:
    ids = _clients()
    brown = ids["brown"]["id"]
    http = testdb.test_client()
    filled = http.get(
        f"/api/reports/export?section=client-results&detail=true&client_id={brown}"
        "&date_from=2026-01-01&date_to=2026-12-31"
    )
    if filled.status_code != 200:
        _fail(f"content export {filled.status_code}")
    header, rows = _data_rows(filled.text)
    if header != DETAIL_COLUMNS:
        _fail(f"headers {header}")
    joined_header = ",".join(header).lower()
    for bit in FORBIDDEN_HEADER_BITS:
        if bit in joined_header:
            _fail(f"security field leaked in headers: {bit}")
    for row in rows[:50]:
        dumped = " | ".join(row)
        if dumped.startswith("<") or "sqlite3." in dumped or "ReportRecord" in dumped:
            _fail(f"python/sql object leaked into CSV: {dumped[:200]}")
        if len(row) != len(header):
            _fail(f"row width {len(row)} != header {len(header)}")

    empty = http.get(
        f"/api/reports/export?section=client-results&metric=calls&client_id={brown}"
        "&date_from=1990-01-01&date_to=1990-01-02"
    )
    if empty.status_code != 200:
        _fail(f"empty calls export {empty.status_code} {empty.text[:300]}")
    empty_header, empty_rows = _data_rows(empty.text)
    if empty_header != DETAIL_COLUMNS:
        _fail(f"empty CSV headers {empty_header}")
    if empty_rows:
        _fail(f"1990 calls export should have zero data rows, got {len(empty_rows)}")
    if "Client" not in empty.text:
        _fail("empty CSV must still include headers")


def test_archived_relationship_excluded_from_hot_detail() -> None:
    ids = _clients()
    brown = ids["brown"]["id"]
    stamp = str(int(time.time()))
    record_no = f"{MARKER}-{stamp}"
    company_id = ccr_id = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, city, state)
            VALUES (?, ?, 'Testville', 'OH')
            """,
            (record_no, f"{MARKER} Hot Co {stamp}"),
        )
        company_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action
            ) VALUES (?, ?, ?, 'Hot Prospect', '')
            """,
            (brown, company_id, record_no),
        )
        ccr_id = int(cur.lastrowid)
        conn.commit()
    http = testdb.test_client()
    try:
        before = http.get(
            f"/api/reports/export?section=client-results&metric=hot_prospects&client_id={brown}"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        if before.status_code != 200:
            _fail(f"hot export before archive {before.status_code} {before.text[:400]}")
        if record_no not in before.text and f"{MARKER} Hot Co {stamp}" not in before.text:
            _fail("active Hot Prospect fixture missing from detailed hot export")
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET archived_at = ?
                WHERE id = ?
                """,
                (now, ccr_id),
            )
            conn.commit()
        after = http.get(
            f"/api/reports/export?section=client-results&metric=hot_prospects&client_id={brown}"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        if after.status_code != 200:
            _fail(f"hot export after archive {after.status_code} {after.text[:400]}")
        if record_no in after.text or f"{MARKER} Hot Co {stamp}" in after.text:
            _fail("archived CCR still present in Hot Prospect detailed export")
    finally:
        with get_connection() as conn:
            conn.execute("DELETE FROM client_company_relationships WHERE id = ?", (ccr_id,))
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
            conn.commit()


def test_quoted_notes_round_trip() -> None:
    ids = _clients()
    brown = ids["brown"]["id"]
    stamp = str(int(time.time()))
    record_no = f"{MARKER}-NOTE-{stamp}"
    note = f"{MARKER} line1\nline2, with comma and \"quotes\""
    company_id = contact_id = activity_id = relationship_id = 0
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, city, state)
            VALUES (?, ?, 'Testville', 'OH')
            """,
            (record_no, f"{MARKER} Note Co {stamp}"),
        )
        company_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action
            ) VALUES (?, ?, ?, 'Contacted', '')
            """,
            (brown, company_id, record_no),
        )
        relationship_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title, source_row_index
            ) VALUES (?, ?, 'Pat', 'Note', 'Buyer', 1)
            """,
            (company_id, record_no),
        )
        contact_id = int(cur.lastrowid)
        activity_id = insert_activity_row(
            conn,
            client_id=brown,
            company_id=company_id,
            relationship_id=relationship_id,
            external_record_no=record_no,
            user_id=1,
            contact_id=contact_id,
            activity_type="Call",
            notes=note,
            assigned_user="PR2D",
            created_by="PR2D",
            activity_at="2026-09-18 09:00:00",
        )
        conn.commit()
    http = testdb.test_client()
    try:
        resp = http.get(
            f"/api/reports/export?section=client-results&metric=calls&client_id={brown}"
            "&date_from=2026-09-18&date_to=2026-09-18"
        )
        if resp.status_code != 200:
            _fail(f"notes export {resp.status_code} {resp.text[:300]}")
        header, rows = _data_rows(resp.text)
        notes_idx = header.index("Notes")
        matched = [row for row in rows if MARKER in " ".join(row)]
        if not matched:
            _fail("marker call missing from calls CSV")
        notes_cell = matched[0][notes_idx]
        if "line1" not in notes_cell or "line2" not in notes_cell:
            _fail("newline note was not preserved in CSV parse")
        if "quotes" not in notes_cell:
            _fail("quoted note lost quotes")
    finally:
        with get_connection() as conn:
            if activity_id:
                conn.execute("DELETE FROM activities WHERE activity_id = ?", (activity_id,))
            conn.execute("DELETE FROM client_company_relationships WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
            conn.commit()


def main() -> None:
    tests = [
        test_production_path_untouched,
        test_detailed_export_hot_count_join,
        test_specialist_isolation_and_rbac,
        test_csv_content_and_empty_result,
        test_archived_relationship_excluded_from_hot_detail,
        test_quoted_notes_round_trip,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} tests ok")


if __name__ == "__main__":
    main()
