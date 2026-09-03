"""Checkpoint C3 — atomic CRM import confirmation tests.

Run: python test_crm_import_confirm.py

Uses isolated testdb copies only — never writes production northstar.db,
never talks to live ports.
"""

from __future__ import annotations

import concurrent.futures
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import testdb
from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from contact_phone import upsert_contact_phone_keys
from crm_import_confirm import (
    _require_allowed_actions,
    confirm_admin_crm_import_batch,
)
from crm_import_plan import plan_crm_import_batch
from crm_import_staging import (
    BatchNotReusable,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    ensure_crm_import_schema,
)
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from main import app
from models import NorthStarUser


UPLOAD = "/api/clients/{client_id}/admin/imports"
MAPPING = "/api/clients/{client_id}/admin/imports/{batch_id}/mapping"
DRY_RUN = "/api/clients/{client_id}/admin/imports/{batch_id}/dry-run"
CONFIRM = "/api/clients/{client_id}/admin/imports/{batch_id}/confirm"
CANCEL = "/api/clients/{client_id}/admin/imports/{batch_id}"

FREEZE_TABLES = (
    "companies",
    "contacts",
    "contact_phone_keys",
    "client_company_relationships",
    "activities",
    "legacy_notes",
    "contact_client_workflows",
    "crm_import_batches",
    "crm_import_rows",
    "crm_import_results",
)

DDL_RE = re.compile(r"^\s*(CREATE|ALTER|DROP)\b", re.I)
SCHEMA_PRAGMA_RE = re.compile(
    r"^\s*PRAGMA\s+(table_info|index_list|index_info|foreign_key_list|database_list)\b",
    re.I,
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _prove_isolated() -> None:
    opened = Path(os.fspath(DB_PATH)).resolve()
    prod = PRODUCTION_DB_PATH.resolve()
    if opened == prod:
        _fail("Testdb isolation failed: opened production DB.")
    env = Path(os.environ.get("NORTHSTAR_TEST_DB", "")).resolve()
    if env != opened:
        _fail("NORTHSTAR_TEST_DB does not match the opened database.")
    print(f"ISOLATED {opened}")


def _client() -> TestClient:
    return TestClient(app)


def _secret_password() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _csrf(response) -> str:
    return str(response.json().get("csrf_token") or "")


def _assert_no_secrets(payload: object) -> None:
    dumped = str(payload).lower()
    for needle in ("password_hash", "csrf_secret", "$argon2id$", "raw_json"):
        if needle in dumped:
            _fail(f"JSON leaked secret field {needle!r}.")


def _create_user(*, email: str, password: str, administrator: int = 1) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, ?, ?, 1, 1, ?, 0, '')
            """,
            (email, "C3 Import Confirm Test Admin", int(administrator), digest),
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
            (user_id, client_id),
        )
        conn.commit()


def _delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def _login(http: TestClient, *, email: str, password: str):
    return http.post("/api/auth/login", json={"email": email, "password": password})


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _table_fingerprint(conn: sqlite3.Connection, table: str) -> str:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    if not rows:
        return f"{table}:0:"
    cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
    payload = []
    for row in rows:
        payload.append(tuple("" if row[c] is None else str(row[c]) for c in cols))
    digest = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
    return f"{table}:{len(rows)}:{digest}"


def _freeze_snapshot() -> dict[str, str]:
    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        migrate_schema(conn)
        conn.commit()
        return {table: _table_fingerprint(conn, table) for table in FREEZE_TABLES}


def _assert_frozen(before: dict[str, str], label: str) -> None:
    after = _freeze_snapshot()
    if after != before:
        changed = [t for t in FREEZE_TABLES if after.get(t) != before.get(t)]
        _fail(f"{label} changed tables {changed}.")


def _sqlite_master_snapshot() -> str:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            ORDER BY type, name
            """
        ).fetchall()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode("utf-8")).hexdigest()


@dataclass
class _AdminSession:
    client_id: int
    other_client_id: int
    user_id: int
    email: str
    password: str
    http: TestClient
    csrf: str

    def close(self) -> None:
        _delete_user(self.user_id)

    def actor(self) -> NorthStarUser:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (self.user_id,)).fetchone()
        return NorthStarUser(
            id=int(row["id"]),
            email=str(row["email"] or ""),
            full_name=str(row["full_name"] or ""),
            is_administrator=bool(row["is_administrator"]),
            is_internal_northstar=bool(row["is_internal_northstar"])
            if "is_internal_northstar" in row.keys()
            else True,
            active=bool(row["active"]),
            created_at=str(row["created_at"] or ""),
        )


def _session() -> _AdminSession:
    _prove_isolated()
    # Controlled migration belongs to setup, never to confirm.
    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        migrate_schema(conn)
        conn.commit()
    http = _client()
    client_id, other_id = _client_ids()
    password = _secret_password()
    email = f"c3.import.admin.{secrets.token_hex(6)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=1)
    _assign(user_id, client_id)
    login = _login(http, email=email, password=password)
    if login.status_code != 200:
        _fail(f"Login failed: {login.status_code} {login.text}")
    csrf = _csrf(login)
    if not csrf:
        _fail("Missing csrf_token from login response.")
    return _AdminSession(
        client_id=client_id,
        other_client_id=other_id,
        user_id=user_id,
        email=email,
        password=password,
        http=http,
        csrf=csrf,
    )


def _upload_batch(
    session: _AdminSession,
    headers: list[str],
    rows: list[list[object]],
    mapping: dict[str, str],
    *,
    client_id: int | None = None,
) -> int:
    cid = session.client_id if client_id is None else client_id
    content = _csv_bytes(headers, rows)
    uploaded = session.http.post(
        UPLOAD.format(client_id=cid),
        headers={CSRF_HEADER: session.csrf},
        files={"file": ("import.csv", content, "application/octet-stream")},
    )
    if uploaded.status_code != 200:
        _fail(f"Upload failed: {uploaded.status_code} {uploaded.text}")
    batch_id = int((uploaded.json().get("batch") or {})["batch_id"])
    mapped = session.http.put(
        MAPPING.format(client_id=cid, batch_id=batch_id),
        headers={CSRF_HEADER: session.csrf},
        json={"mapping": mapping},
    )
    if mapped.status_code != 200:
        _fail(f"Mapping failed: {mapped.status_code} {mapped.text}")
    return batch_id


def _dry_run(session: _AdminSession, batch_id: int, *, client_id: int | None = None):
    cid = session.client_id if client_id is None else client_id
    return session.http.post(
        DRY_RUN.format(client_id=cid, batch_id=batch_id),
        headers={CSRF_HEADER: session.csrf},
        json={"offset": 0, "limit": 100},
    )


def _dry_run_fingerprint(session: _AdminSession, batch_id: int) -> str:
    resp = _dry_run(session, batch_id)
    if resp.status_code != 200:
        _fail(f"Dry-run failed: {resp.status_code} {resp.text}")
    fp = resp.json().get("plan_fingerprint") or ""
    if not re.match(r"^[a-f0-9]{64}$", fp):
        _fail("Dry-run returned missing/malformed fingerprint.")
    return fp


def _confirm(
    session: _AdminSession,
    batch_id: int,
    *,
    fp: str,
    confirm: bool = True,
    client_id: int | None = None,
    csrf: str | None = None,
    include_csrf: bool = True,
    fingerprint_key: str = "plan_fingerprint",
    body: dict | None = None,
):
    cid = session.client_id if client_id is None else client_id
    headers = {}
    if include_csrf:
        headers[CSRF_HEADER] = session.csrf if csrf is None else csrf
    payload = body if body is not None else {"confirm": confirm, fingerprint_key: fp}
    return session.http.post(
        CONFIRM.format(client_id=cid, batch_id=batch_id),
        headers=headers,
        json=payload,
    )


def _insert_company_for_client(
    *,
    client_id: int,
    company_name: str,
    address: str = "",
    city: str = "",
    state: str = "",
    website: str = "",
    phone: str = "",
    with_ccr: bool = True,
) -> tuple[int, str, int | None]:
    from crm_add_data import allocate_ns_record_no

    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rn = allocate_ns_record_no(conn)
        cur = conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, address, city, state, zip, website,
                legacy_phone, type_of_industry, created_at, last_updated_at
            ) VALUES (?, ?, ?, ?, ?, '', ?, ?, '', ?, ?)
            """,
            (rn, company_name, address, city, state, website, phone, now, now),
        )
        company_id = int(cur.lastrowid)
        ccr_id = None
        if with_ccr:
            cur2 = conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, ?, 'New', NULL, '', '', '', 0, ?, ?)
                """,
                (client_id, company_id, rn, now, now),
            )
            ccr_id = int(cur2.lastrowid)
        conn.commit()
    return company_id, rn, ccr_id


def _insert_contact(
    company_id: int,
    external_record_no: str,
    *,
    first: str,
    last: str,
    title: str = "",
    phone: str = "",
    email: str = "",
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no,
                first_name, last_name, title,
                phone, alt_phone, email, source_row_index
            ) VALUES (?, ?, ?, ?, ?, ?, '', ?, 0)
            """,
            (int(company_id), external_record_no, first, last, title, phone, email),
        )
        contact_id = int(cur.lastrowid)
        if phone:
            upsert_contact_phone_keys(conn, contact_id, phone, "")
        conn.commit()
        return contact_id


def _set_batch_fields(batch_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE crm_import_batches SET {cols} WHERE id = ?",
            (*fields.values(), batch_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# No request-time schema migration
# ---------------------------------------------------------------------------


def test_confirm_never_calls_ensure_schema_and_performs_no_ddl() -> None:
    session = _session()
    try:
        batch_id = _upload_batch(
            session,
            ["Company"],
            [["NoDdlCo"]],
            {"company_name": "Company"},
        )
        fp = _dry_run_fingerprint(session, batch_id)
        master_before = _sqlite_master_snapshot()
        freeze_before = _freeze_snapshot()
        traced: list[str] = []

        real_get_connection = get_connection

        def traced_get_connection(*args, **kwargs):
            conn = real_get_connection(*args, **kwargs)

            def _trace(_statement: str) -> None:
                traced.append(_statement)

            conn.set_trace_callback(_trace)
            return conn

        def boom(*_a, **_k):
            raise AssertionError("ensure_crm_import_schema must not be called by confirm")

        with patch("crm_import_staging.ensure_crm_import_schema", side_effect=boom), patch(
            "crm_import_confirm.get_connection", side_effect=traced_get_connection
        ):
            resp = _confirm(session, batch_id, fp=fp)
        if resp.status_code != 200:
            _fail(f"Confirm must succeed without calling ensure: {resp.status_code} {resp.text}")
        for stmt in traced:
            if DDL_RE.search(stmt):
                _fail(f"Confirm issued DDL: {stmt}")
            if SCHEMA_PRAGMA_RE.search(stmt):
                _fail(f"Confirm issued schema-migration PRAGMA: {stmt}")
        if _sqlite_master_snapshot() != master_before:
            _fail("Confirm must not change sqlite_master.")
        # Success changes freeze tables; prove schema objects unchanged only above.
        after = _freeze_snapshot()
        if after["crm_import_batches"] == freeze_before["crm_import_batches"]:
            _fail("Successful confirm should update batch audit state.")
    finally:
        session.close()


def test_confirm_works_after_explicit_isolated_migration() -> None:
    session = _session()
    try:
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
        batch_id = _upload_batch(
            session,
            ["Company"],
            [["MigratedSetupCo"]],
            {"company_name": "Company"},
        )
        fp = _dry_run_fingerprint(session, batch_id)
        resp = _confirm(session, batch_id, fp=fp)
        if resp.status_code != 200:
            _fail(f"Confirm after explicit migration must succeed: {resp.status_code}")
        if resp.json().get("status") != "imported":
            _fail("Expected imported status.")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_confirm_authorization_complete_freeze() -> None:
    session = _session()
    try:
        batch_id = _upload_batch(
            session, ["Company"], [["AuthFreezeCo"]], {"company_name": "Company"}
        )
        fp = _dry_run_fingerprint(session, batch_id)

        # unauthenticated
        freeze = _freeze_snapshot()
        anon = _client().post(
            CONFIRM.format(client_id=session.client_id, batch_id=batch_id),
            json={"confirm": True, "plan_fingerprint": fp},
        )
        if anon.status_code != 401:
            _fail(f"Anonymous confirm must be 401, got {anon.status_code}.")
        _assert_frozen(freeze, "anonymous confirm")

        # non-admin
        freeze = _freeze_snapshot()
        http = _client()
        password = _secret_password()
        email = f"c3.nonadmin.{secrets.token_hex(6)}@example.test"
        uid = _create_user(email=email, password=password, administrator=0)
        _assign(uid, session.client_id)
        try:
            login = _login(http, email=email, password=password)
            csrf = _csrf(login)
            denied = http.post(
                CONFIRM.format(client_id=session.client_id, batch_id=batch_id),
                headers={CSRF_HEADER: csrf},
                json={"confirm": True, "plan_fingerprint": fp},
            )
            if denied.status_code != 403:
                _fail(f"Non-admin confirm must be 403, got {denied.status_code}.")
        finally:
            _delete_user(uid)
        _assert_frozen(freeze, "non-admin confirm")

        # missing CSRF
        freeze = _freeze_snapshot()
        missing = _confirm(session, batch_id, fp=fp, include_csrf=False)
        if missing.status_code != 403:
            _fail(f"Missing CSRF must be 403, got {missing.status_code}.")
        _assert_frozen(freeze, "missing CSRF")

        # wrong CSRF
        freeze = _freeze_snapshot()
        wrong = _confirm(session, batch_id, fp=fp, csrf="x" * len(session.csrf))
        if wrong.status_code != 403:
            _fail(f"Wrong CSRF must be 403, got {wrong.status_code}.")
        _assert_frozen(freeze, "wrong CSRF")

        # Administrators may access any client; a batch under a different client
        # path must still 404 (existing client-scoped lookup behavior).
        freeze = _freeze_snapshot()
        other = _confirm(session, batch_id, fp=fp, client_id=session.other_client_id)
        if other.status_code != 404:
            _fail(
                f"Admin confirm for batch under another client path must be 404, got {other.status_code}."
            )
        _assert_frozen(freeze, "admin other-client path scoping")

        # Explicit inaccessible-client 403: authenticated admin path uses
        # require_client_access; non-administrators are denied before client scope.
        # Cover client_id <= 0 (existing CLIENT_REQUIRED 400) for completeness.
        freeze = _freeze_snapshot()
        zero = _confirm(session, batch_id, fp=fp, client_id=0)
        if zero.status_code != 400:
            _fail(f"client_id 0 must be 400, got {zero.status_code}.")
        _assert_frozen(freeze, "client_id 0")

        # batch belongs to other client, actor can access requested client
        other_admin_password = _secret_password()
        other_email = f"c3.otheradmin.{secrets.token_hex(6)}@example.test"
        other_uid = _create_user(email=other_email, password=other_admin_password, administrator=1)
        _assign(other_uid, session.other_client_id)
        other_http = _client()
        other_login = _login(other_http, email=other_email, password=other_admin_password)
        other_csrf = _csrf(other_login)
        try:
            up = other_http.post(
                UPLOAD.format(client_id=session.other_client_id),
                headers={CSRF_HEADER: other_csrf},
                files={
                    "file": (
                        "other.csv",
                        _csv_bytes(["Company"], [["OtherClientBatch"]]),
                        "text/csv",
                    )
                },
            )
            if up.status_code != 200:
                _fail(f"Other-client upload failed: {up.status_code}")
            other_batch = int((up.json().get("batch") or {})["batch_id"])
            mp = other_http.put(
                MAPPING.format(client_id=session.other_client_id, batch_id=other_batch),
                headers={CSRF_HEADER: other_csrf},
                json={"mapping": {"company_name": "Company"}},
            )
            if mp.status_code != 200:
                _fail(f"Other-client mapping failed: {mp.status_code}")
        finally:
            _delete_user(other_uid)

        freeze = _freeze_snapshot()
        scoped = _confirm(session, other_batch, fp=fp, client_id=session.client_id)
        if scoped.status_code != 404:
            _fail(
                f"Batch under another client must be 404 for requested client, got {scoped.status_code}."
            )
        _assert_frozen(freeze, "cross-client batch 404")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Request + batch-state
# ---------------------------------------------------------------------------


def test_confirm_request_and_batch_state_refusals() -> None:
    session = _session()
    try:
        batch_id = _upload_batch(
            session, ["Company"], [["StateCo"]], {"company_name": "Company"}
        )
        fp = _dry_run_fingerprint(session, batch_id)

        cases = [
            ("confirm false", {"confirm": False, "plan_fingerprint": fp}, 400),
            ("missing fingerprint", {"confirm": True}, 400),
            ("uppercase fingerprint", {"confirm": True, "plan_fingerprint": fp.upper()}, 400),
            ("wrong length", {"confirm": True, "plan_fingerprint": fp[:63]}, 400),
            ("non-hex", {"confirm": True, "plan_fingerprint": "g" * 64}, 400),
        ]
        for label, body, code in cases:
            freeze = _freeze_snapshot()
            resp = _confirm(session, batch_id, fp=fp, body=body)
            if resp.status_code != code:
                _fail(f"{label}: expected {code}, got {resp.status_code}.")
            _assert_frozen(freeze, label)

        # cancelled
        cancel = session.http.delete(
            CANCEL.format(client_id=session.client_id, batch_id=batch_id),
            headers={CSRF_HEADER: session.csrf},
        )
        if cancel.status_code != 200:
            _fail("Cancel setup failed.")
        freeze = _freeze_snapshot()
        resp = _confirm(session, batch_id, fp=fp)
        if resp.status_code != 409:
            _fail(f"Cancelled batch must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "cancelled batch")

        # failed
        failed_id = _upload_batch(
            session, ["Company"], [["FailedCo"]], {"company_name": "Company"}
        )
        failed_fp = _dry_run_fingerprint(session, failed_id)
        _set_batch_fields(failed_id, status=STATUS_FAILED)
        freeze = _freeze_snapshot()
        resp = _confirm(session, failed_id, fp=failed_fp)
        if resp.status_code != 409:
            _fail(f"Failed batch must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "failed batch")

        # expired without purge/status rewrite
        expired_id = _upload_batch(
            session, ["Company"], [["ExpiredCo"]], {"company_name": "Company"}
        )
        expired_fp = _dry_run_fingerprint(session, expired_id)
        past = (datetime.now() - timedelta(days=2)).replace(microsecond=0).isoformat(sep=" ")
        _set_batch_fields(expired_id, expires_at=past)
        freeze = _freeze_snapshot()
        resp = _confirm(session, expired_id, fp=expired_fp)
        if resp.status_code != 409:
            _fail(f"Expired batch must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "expired batch no purge")
        with get_connection() as conn:
            st = conn.execute(
                "SELECT status FROM crm_import_batches WHERE id = ?", (expired_id,)
            ).fetchone()["status"]
            rows = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM crm_import_rows WHERE batch_id = ?",
                    (expired_id,),
                ).fetchone()["n"]
            )
        if st != "previewed":
            _fail("Expired confirm must not rewrite status away from previewed.")
        if rows != 1:
            _fail("Expired confirm must retain staged rows (no purge).")

        # zero staged rows
        empty_id = _upload_batch(
            session, ["Company"], [["EmptyCo"]], {"company_name": "Company"}
        )
        empty_fp = _dry_run_fingerprint(session, empty_id)
        with get_connection() as conn:
            conn.execute("DELETE FROM crm_import_rows WHERE batch_id = ?", (empty_id,))
            conn.commit()
        freeze = _freeze_snapshot()
        resp = _confirm(session, empty_id, fp=empty_fp)
        if resp.status_code != 409:
            _fail(f"Empty staged rows must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "empty staged rows")

        # malformed mapping_json
        bad_map_id = _upload_batch(
            session, ["Company"], [["BadMapCo"]], {"company_name": "Company"}
        )
        bad_fp = _dry_run_fingerprint(session, bad_map_id)
        _set_batch_fields(bad_map_id, mapping_json="{not-json")
        freeze = _freeze_snapshot()
        resp = _confirm(session, bad_map_id, fp=bad_fp)
        if resp.status_code != 400:
            _fail(f"Malformed mapping_json must be 400, got {resp.status_code}.")
        _assert_frozen(freeze, "malformed mapping")

        # invalid stored mapping (valid JSON, missing company_name)
        inv_id = _upload_batch(
            session, ["Company"], [["InvMapCo"]], {"company_name": "Company"}
        )
        inv_fp = _dry_run_fingerprint(session, inv_id)
        _set_batch_fields(inv_id, mapping_json=json.dumps({"contact_email": "Company"}))
        freeze = _freeze_snapshot()
        resp = _confirm(session, inv_id, fp=inv_fp)
        if resp.status_code != 400:
            _fail(f"Invalid stored mapping must be 400, got {resp.status_code}.")
        _assert_frozen(freeze, "invalid mapping")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Stale fingerprints
# ---------------------------------------------------------------------------


def test_confirm_stale_fingerprints() -> None:
    session = _session()
    try:
        # mapping changed
        bid = _upload_batch(
            session,
            ["Company", "Extra"],
            [["StaleMapCo", "x"]],
            {"company_name": "Company"},
        )
        old_fp = _dry_run_fingerprint(session, bid)
        remap = session.http.put(
            MAPPING.format(client_id=session.client_id, batch_id=bid),
            headers={CSRF_HEADER: session.csrf},
            json={"mapping": {"company_name": "Extra"}},
        )
        if remap.status_code != 200:
            _fail("Remap setup failed.")
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=old_fp)
        if resp.status_code != 409:
            _fail(f"Stale fingerprint after mapping change must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "stale after mapping")

        # staged raw_json changed
        bid = _upload_batch(
            session, ["Company"], [["StaleRawCo"]], {"company_name": "Company"}
        )
        old_fp = _dry_run_fingerprint(session, bid)
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE crm_import_rows
                SET raw_json = ?
                WHERE batch_id = ?
                """,
                (json.dumps({"Company": "StaleRawCo CHANGED"}), bid),
            )
            conn.commit()
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=old_fp)
        if resp.status_code != 409:
            _fail(f"Stale fingerprint after raw_json change must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "stale after raw_json")

        # staged row removed
        bid = _upload_batch(
            session,
            ["Company"],
            [["StaleDelA"], ["StaleDelB"]],
            {"company_name": "Company"},
        )
        old_fp = _dry_run_fingerprint(session, bid)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM crm_import_rows WHERE batch_id = ? ORDER BY id LIMIT 1",
                (bid,),
            ).fetchone()
            conn.execute("DELETE FROM crm_import_rows WHERE id = ?", (int(row["id"]),))
            conn.commit()
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=old_fp)
        if resp.status_code != 409:
            _fail(f"Stale fingerprint after row delete must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "stale after row delete")

        # staged row added
        bid = _upload_batch(
            session, ["Company"], [["StaleAddCo"]], {"company_name": "Company"}
        )
        old_fp = _dry_run_fingerprint(session, bid)
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO crm_import_rows (
                    batch_id, client_id, source_row_number, raw_json,
                    warnings_json, errors_json, is_blank, has_blocking_error
                ) VALUES (?, ?, 99, ?, '[]', '[]', 0, 0)
                """,
                (bid, session.client_id, json.dumps({"Company": "StaleAddExtra"})),
            )
            conn.commit()
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=old_fp)
        if resp.status_code != 409:
            _fail(f"Stale fingerprint after row add must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "stale after row add")

        # relevant company created so plan changes
        marker = secrets.token_hex(5)
        name = f"StaleCompanyHit {marker}"
        bid = _upload_batch(
            session, ["Company"], [[name]], {"company_name": "Company"}
        )
        old_fp = _dry_run_fingerprint(session, bid)
        _insert_company_for_client(client_id=session.client_id, company_name=name)
        freeze = _freeze_snapshot()  # baseline AFTER intentional CRM setup mutation
        resp = _confirm(session, bid, fp=old_fp)
        if resp.status_code != 409:
            _fail(f"Stale fingerprint after company create must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "stale after company create")

        # relevant contact created so plan changes
        marker = secrets.token_hex(5)
        cname = f"StaleContactCo {marker}"
        email = f"stale.{marker}@x.test"
        company_id, rn, _ = _insert_company_for_client(
            client_id=session.client_id, company_name=cname
        )
        bid = _upload_batch(
            session,
            ["Company", "Email"],
            [[cname, email]],
            {"company_name": "Company", "contact_email": "Email"},
        )
        old_fp = _dry_run_fingerprint(session, bid)
        _insert_contact(company_id, rn, first="New", last="Contact", email=email)
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=old_fp)
        if resp.status_code != 409:
            _fail(f"Stale fingerprint after contact create must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "stale after contact create")

        # relevant CCR created
        marker = secrets.token_hex(5)
        cname = f"StaleRelCo {marker}"
        company_id, rn, _ = _insert_company_for_client(
            client_id=session.client_id, company_name=cname, with_ccr=False
        )
        bid = _upload_batch(
            session, ["Company"], [[cname]], {"company_name": "Company"}
        )
        old_fp = _dry_run_fingerprint(session, bid)
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, ?, 'New', NULL, '', '', '', 0, ?, ?)
                """,
                (session.client_id, company_id, rn, now, now),
            )
            conn.commit()
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=old_fp)
        if resp.status_code != 409:
            _fail(f"Stale fingerprint after CCR create must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "stale after CCR create")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Needs-review refusals
# ---------------------------------------------------------------------------


def test_confirm_needs_review_refusals() -> None:
    session = _session()
    try:
        marker = secrets.token_hex(5)

        # blocking_error
        bid = _upload_batch(
            session, ["Company"], [[f"BlockCo {marker}"]], {"company_name": "Company"}
        )
        with get_connection() as conn:
            conn.execute(
                "UPDATE crm_import_rows SET has_blocking_error = 1 WHERE batch_id = ?",
                (bid,),
            )
            conn.commit()
        dry = _dry_run(session, bid)
        if dry.status_code != 200:
            _fail("blocking dry-run failed")
        if dry.json()["counts"]["blocking_error"] < 1:
            _fail("Expected blocking_error in plan counts.")
        fp = dry.json()["plan_fingerprint"]
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=fp)
        if resp.status_code != 409:
            _fail(f"blocking_error confirm must be 409, got {resp.status_code}.")
        _assert_frozen(freeze, "blocking_error")

        # invalid_mapping_data (blank company)
        bid = _upload_batch(
            session,
            ["Company", "Email"],
            [["", f"blank.{marker}@x.test"]],
            {"company_name": "Company", "contact_email": "Email"},
        )
        dry = _dry_run(session, bid)
        if dry.json()["counts"]["invalid_mapping_data"] < 1:
            _fail("Expected invalid_mapping_data.")
        fp = dry.json()["plan_fingerprint"]
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=fp)
        if resp.status_code != 409:
            _fail("invalid_mapping_data confirm must be 409.")
        _assert_frozen(freeze, "invalid_mapping_data")

        # possible_company_match (+ deferred contact/relationship)
        twin = f"TwinCo {marker}"
        _insert_company_for_client(
            client_id=session.client_id, company_name=twin, city="Austin", state="TX"
        )
        _insert_company_for_client(
            client_id=session.client_id, company_name=twin, city="Houston", state="TX"
        )
        bid = _upload_batch(
            session, ["Company"], [[twin]], {"company_name": "Company"}
        )
        dry = _dry_run(session, bid)
        row = dry.json()["rows"][0]
        if row["company"]["action"] != "possible_company_match":
            _fail("Expected possible_company_match.")
        if row["contact"]["action"] != "deferred" or row["relationship"]["action"] != "deferred":
            _fail("possible_company_match must defer contact and relationship.")
        fp = dry.json()["plan_fingerprint"]
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=fp)
        if resp.status_code != 409:
            _fail("possible_company_match confirm must be 409.")
        _assert_frozen(freeze, "possible_company_match/deferred")

        # possible_contact_match from name-only
        cname = f"NameOnlyCo {marker}"
        company_id, rn, _ = _insert_company_for_client(
            client_id=session.client_id, company_name=cname
        )
        _insert_contact(company_id, rn, first="Alan", last="Turing")
        bid = _upload_batch(
            session,
            ["Company", "First", "Last"],
            [[cname, "Alan", "Turing"]],
            {
                "company_name": "Company",
                "contact_first_name": "First",
                "contact_last_name": "Last",
            },
        )
        dry = _dry_run(session, bid)
        row = dry.json()["rows"][0]
        if row["contact"]["action"] != "possible_contact_match":
            _fail(f"Expected name-only possible_contact_match, got {row['contact']['action']}.")
        if "name_exact" not in (row["contact"].get("reasons") or []) and not any(
            "name_exact" in (p.get("reasons") or []) for p in row["contact"].get("possibles") or []
        ):
            _fail("Expected name_exact reason for possible contact.")
        fp = dry.json()["plan_fingerprint"]
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=fp)
        if resp.status_code != 409:
            _fail("name-only possible_contact_match confirm must be 409.")
        _assert_frozen(freeze, "possible_contact name-only")

        # possible_contact_match from last-seven phone
        cname = f"Last7Co {marker}"
        company_id, rn, _ = _insert_company_for_client(
            client_id=session.client_id, company_name=cname
        )
        _insert_contact(company_id, rn, first="Last", last="Seven", phone="4695550199")
        bid = _upload_batch(
            session,
            ["Company", "First", "Last", "Phone"],
            [[cname, "Last", "Seven", "5550199"]],
            {
                "company_name": "Company",
                "contact_first_name": "First",
                "contact_last_name": "Last",
                "contact_phone": "Phone",
            },
        )
        dry = _dry_run(session, bid)
        row = dry.json()["rows"][0]
        if row["contact"]["action"] != "possible_contact_match":
            _fail(f"Expected last7 possible_contact_match, got {row['contact']['action']}.")
        fp = dry.json()["plan_fingerprint"]
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=fp)
        if resp.status_code != 409:
            _fail("last7 possible_contact_match confirm must be 409.")
        _assert_frozen(freeze, "possible_contact last7")

        # insufficient_contact_data
        bid = _upload_batch(
            session,
            ["Company", "Title"],
            [[f"InsuffCo {marker}", "Director"]],
            {"company_name": "Company", "contact_title": "Title"},
        )
        dry = _dry_run(session, bid)
        if dry.json()["counts"]["insufficient_contact_data"] < 1:
            _fail("Expected insufficient_contact_data.")
        fp = dry.json()["plan_fingerprint"]
        freeze = _freeze_snapshot()
        resp = _confirm(session, bid, fp=fp)
        if resp.status_code != 409:
            _fail("insufficient_contact_data confirm must be 409.")
        _assert_frozen(freeze, "insufficient_contact_data")

        # unknown action via focused unit injection on apply validator
        try:
            _require_allowed_actions(
                SimpleNamespace(
                    total_rows=1,
                    rows=[
                        SimpleNamespace(
                            company_action="create_company",
                            contact_action="totally_unknown",
                            relationship_action="create_client_relationship",
                        )
                    ],
                )
            )
            _fail("Unknown contact action must raise.")
        except BatchNotReusable:
            pass
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Proposed-key / ownership invariants
# ---------------------------------------------------------------------------


def _confirm_with_plan_mutator(session: _AdminSession, batch_id: int, fp: str, mutator):
    real = plan_crm_import_batch

    def wrapped(conn, *, client_id, batch_id):
        plan = real(conn, client_id=client_id, batch_id=batch_id)
        mutator(plan)
        return plan

    with patch("crm_import_confirm.plan_crm_import_batch", wrapped):
        return confirm_admin_crm_import_batch(
            client_id=session.client_id,
            batch_id=batch_id,
            plan_fingerprint=fp,
            actor=session.actor(),
        )


def test_confirm_proposed_key_and_ownership_invariants() -> None:
    session = _session()
    try:
        marker = secrets.token_hex(5)

        def _base_batch(label: str) -> tuple[int, str]:
            bid = _upload_batch(
                session,
                ["Company", "Full", "Email", "Phone"],
                [[label, "Ada Lovelace", f"{label.replace(' ', '').lower()}@x.test", "2145550199"]],
                {
                    "company_name": "Company",
                    "contact_full_name": "Full",
                    "contact_email": "Email",
                    "contact_phone": "Phone",
                },
            )
            return bid, _dry_run_fingerprint(session, bid)

        cases = []

        bid, fp = _base_batch("BlankKeyCo")
        freeze = _freeze_snapshot()

        def blank_key(plan):
            plan.rows[0].company_proposed_key = ""

        try:
            _confirm_with_plan_mutator(session, bid, fp, blank_key)
            _fail("blank proposed key must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "blank proposed key")

        bid, fp = _base_batch("DupKeyCo")
        freeze = _freeze_snapshot()

        def dup_key(plan):
            # Force a second synthetic create using the same key by mutating
            # after planning: duplicate the create key onto a crafted second pass
            # by setting proposed key then cloning action through mutation of mapped
            # reuse path — instead mutate to two creates sharing one key via
            # rewriting row list is hard; set create key then force a later
            # in-loop create by converting a use path. Simpler: set key K on create,
            # and inject fail by also registering create twice via mutating first
            # row key and second synthetic — for single-row batch, convert by
            # calling apply logic through two identical create keys using a
            # custom plan.rows list with two create_company sharing one key.
            from copy import deepcopy

            first = plan.rows[0]
            key = first.company_proposed_key
            second = deepcopy(first)
            # deepcopy may not work on slots dataclass — build shallow copy fields
            second = SimpleNamespace(**{f: getattr(first, f) for f in first.__dataclass_fields__})
            second.row_id = int(first.row_id) + 1000
            second.source_row_number = int(first.source_row_number) + 1
            second.company_action = "create_company"
            second.company_proposed_key = key
            second.contact_action = "no_contact_data"
            second.contact_id = None
            second.contact_proposed_key = None
            second.relationship_action = "relationship_already_exists"
            second.relationship_proposed_key = first.relationship_proposed_key
            second.relationship_id = None
            plan.rows = [first, second]
            plan.total_rows = 2
            plan.counts["importable_rows"] = 2
            plan.counts["needs_review_rows"] = 0

        try:
            _confirm_with_plan_mutator(session, bid, fp, dup_key)
            _fail("duplicate create key must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "duplicate create key")

        bid, fp = _base_batch("MissingReuseCo")
        freeze = _freeze_snapshot()

        def missing_reuse(plan):
            plan.rows[0].company_action = "use_existing_company"
            plan.rows[0].company_id = None
            plan.rows[0].company_proposed_key = ""

        try:
            _confirm_with_plan_mutator(session, bid, fp, missing_reuse)
            _fail("missing proposed reuse key must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "missing reuse key")

        bid, fp = _base_batch("ForwardRefCo")
        freeze = _freeze_snapshot()

        def forward_ref(plan):
            row = plan.rows[0]
            key = row.company_proposed_key
            row.company_action = "use_existing_company"
            row.company_id = None
            row.company_proposed_key = key

        try:
            _confirm_with_plan_mutator(session, bid, fp, forward_ref)
            _fail("forward proposed key must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "forward proposed key")

        bid, fp = _base_batch("WrongKindCo")
        freeze = _freeze_snapshot()

        def wrong_kind(plan):
            plan.rows[0].company_action = "use_existing_company"
            plan.rows[0].company_id = None
            plan.rows[0].company_proposed_key = "proposed:contact:1:1"

        try:
            _confirm_with_plan_mutator(session, bid, fp, wrong_kind)
            _fail("wrong-kind proposed key must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "wrong-kind key")

        # conflicting proposed resolution: reuse contact key under different company
        bid = _upload_batch(
            session,
            ["Company", "Email"],
            [[f"ConflictA {marker}", f"c.{marker}@x.test"], [f"ConflictB {marker}", f"c.{marker}@x.test"]],
            {"company_name": "Company", "contact_email": "Email"},
        )
        # Force second row to reuse first contact proposed key while companies differ.
        fp = _dry_run_fingerprint(session, bid)
        freeze = _freeze_snapshot()

        def conflicting(plan):
            if len(plan.rows) < 2:
                _fail("Need two rows for conflicting resolution.")
            # Ensure row0 creates contact; row1 reuses that contact key under its own company create.
            k = plan.rows[0].contact_proposed_key
            plan.rows[1].contact_action = "use_existing_contact"
            plan.rows[1].contact_id = None
            plan.rows[1].contact_proposed_key = k

        try:
            _confirm_with_plan_mutator(session, bid, fp, conflicting)
            _fail("conflicting proposed contact/company must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "conflicting proposed resolution")

        bid, fp = _base_batch("MissingCompanyCo")
        freeze = _freeze_snapshot()

        def missing_company(plan):
            plan.rows[0].company_action = "use_existing_company"
            plan.rows[0].company_id = 999999999
            plan.rows[0].company_proposed_key = None

        try:
            _confirm_with_plan_mutator(session, bid, fp, missing_company)
            _fail("missing DB company must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "missing DB company")

        # contact belongs to another company
        other_co, other_rn, _ = _insert_company_for_client(
            client_id=session.client_id, company_name=f"OtherOwnerCo {marker}"
        )
        other_contact = _insert_contact(other_co, other_rn, first="X", last="Y", email=f"own.{marker}@x.test")
        bid, fp = _base_batch("WrongContactCo")
        freeze = _freeze_snapshot()

        def wrong_contact(plan):
            plan.rows[0].contact_action = "use_existing_contact"
            plan.rows[0].contact_id = other_contact
            plan.rows[0].contact_proposed_key = None

        try:
            _confirm_with_plan_mutator(session, bid, fp, wrong_contact)
            _fail("contact wrong company must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "contact wrong company")

        # relationship wrong client
        other_co2, other_rn2, other_ccr = _insert_company_for_client(
            client_id=session.other_client_id, company_name=f"OtherClientRel {marker}"
        )
        bid, fp = _base_batch("WrongRelClientCo")
        freeze = _freeze_snapshot()

        def wrong_rel_client(plan):
            plan.rows[0].relationship_action = "relationship_already_exists"
            plan.rows[0].relationship_id = other_ccr
            plan.rows[0].relationship_proposed_key = None

        try:
            _confirm_with_plan_mutator(session, bid, fp, wrong_rel_client)
            _fail("relationship wrong client must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "relationship wrong client")

        # relationship wrong company
        alien_co, alien_rn, alien_ccr = _insert_company_for_client(
            client_id=session.client_id, company_name=f"AlienRelCo {marker}"
        )
        bid, fp = _base_batch("WrongRelCompanyCo")
        freeze = _freeze_snapshot()

        def wrong_rel_company(plan):
            plan.rows[0].relationship_action = "relationship_already_exists"
            plan.rows[0].relationship_id = alien_ccr
            plan.rows[0].relationship_proposed_key = None

        try:
            _confirm_with_plan_mutator(session, bid, fp, wrong_rel_company)
            _fail("relationship wrong company must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "relationship wrong company")

        # batch terminal UPDATE affects zero rows
        bid, fp = _base_batch("ZeroUpdateCo")
        freeze = _freeze_snapshot()
        try:
            confirm_admin_crm_import_batch(
                client_id=session.client_id,
                batch_id=bid,
                plan_fingerprint=fp,
                actor=session.actor(),
                fail_after="force_non_previewed_before_update",
            )
            _fail("zero-row terminal UPDATE must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "zero-row terminal UPDATE")

        # staged DELETE count mismatch
        bid, fp = _base_batch("DeleteMismatchCo")
        freeze = _freeze_snapshot()
        try:
            confirm_admin_crm_import_batch(
                client_id=session.client_id,
                batch_id=bid,
                plan_fingerprint=fp,
                actor=session.actor(),
                fail_after="inflate_delete_count",
            )
            _fail("delete count mismatch must fail")
        except BatchNotReusable:
            pass
        _assert_frozen(freeze, "delete count mismatch")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Happy paths / audit / concurrency / fault injection (strengthened freezes)
# ---------------------------------------------------------------------------


def test_confirm_happy_paths_audit_and_phone() -> None:
    session = _session()
    try:
        bid = _upload_batch(
            session,
            ["Company", "CFirst", "CLast", "CPhone", "Email"],
            [["ImportNew2", "Ada", "Lovelace", "214-555-0199", "ADA@EXAMPLE.TEST"]],
            {
                "company_name": "Company",
                "contact_first_name": "CFirst",
                "contact_last_name": "CLast",
                "contact_phone": "CPhone",
                "contact_email": "Email",
            },
        )
        fp = _dry_run_fingerprint(session, bid)
        resp = _confirm(session, bid, fp=fp)
        if resp.status_code != 200:
            _fail(f"Confirm failed: {resp.status_code} {resp.text}")
        body = resp.json()
        _assert_no_secrets(body)
        if body["status"] != "imported":
            _fail("status must be imported")
        if int(body["total_imported_row_count"]) != 1:
            _fail("total_imported_row_count must be 1")
        if int(body["created_company_count"]) != 1:
            _fail("created_company_count must be 1")
        if int(body["created_contact_count"]) != 1:
            _fail("created_contact_count must be 1")
        if int(body["created_relationship_count"]) != 1:
            _fail("created_relationship_count must be 1")

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT c.id, c.phone, c.alt_phone, c.email, c.source_row_index, ccr.assigned_user_id
                FROM contacts c
                JOIN companies co ON co.id = c.company_id
                JOIN client_company_relationships ccr
                  ON ccr.company_id = co.id AND ccr.client_id = ?
                WHERE co.company_name = 'ImportNew2'
                """,
                (session.client_id,),
            ).fetchone()
            if row is None:
                _fail("Expected contact/CCR.")
            if row["phone"] != "(214) 555-0199":
                _fail(f"Expected formatted phone, got {row['phone']!r}")
            if row["alt_phone"] != "":
                _fail("alt_phone must be empty")
            if row["email"] != "ADA@EXAMPLE.TEST":
                _fail("email casing must be preserved")
            if row["assigned_user_id"] is not None:
                _fail("relationship assigned_user_id must remain NULL")
            keys = conn.execute(
                """
                SELECT slot, nanp10, last7 FROM contact_phone_keys
                WHERE contact_id = ? ORDER BY slot
                """,
                (int(row["id"]),),
            ).fetchall()
            if len(keys) != 1 or keys[0]["slot"] != "phone":
                _fail("Expected one phone key row.")
            if keys[0]["nanp10"] != "2145550199" or keys[0]["last7"] != "5550199":
                _fail("phone key nanp10/last7 mismatch")
            results = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM crm_import_results WHERE batch_id = ?",
                    (bid,),
                ).fetchone()["n"]
            )
            staged = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM crm_import_rows WHERE batch_id = ?",
                    (bid,),
                ).fetchone()["n"]
            )
            if results != 1 or staged != 0:
                _fail("Expected one result and zero staged rows.")

        # imported cannot map/dry-run/cancel/reconfirm
        freeze = _freeze_snapshot()
        if _dry_run(session, bid).status_code != 409:
            _fail("dry-run on imported must be 409")
        if (
            session.http.put(
                MAPPING.format(client_id=session.client_id, batch_id=bid),
                headers={CSRF_HEADER: session.csrf},
                json={"mapping": {"company_name": "Company"}},
            ).status_code
            != 409
        ):
            _fail("mapping on imported must be 409")
        if (
            session.http.delete(
                CANCEL.format(client_id=session.client_id, batch_id=bid),
                headers={CSRF_HEADER: session.csrf},
            ).status_code
            != 409
        ):
            _fail("cancel on imported must be 409")
        if _confirm(session, bid, fp=fp).status_code != 409:
            _fail("reconfirm must be 409")
        _assert_frozen(freeze, "imported terminal refusals")
    finally:
        session.close()


def test_confirm_atomicity_fault_injection_full_freeze() -> None:
    session = _session()
    try:
        stages = [
            "after_company_insert",
            "after_contact_insert",
            "after_relationship_insert",
            "during_result_audit",
            "during_batch_completion_update",
            "during_staged_row_deletion",
            "before_commit",
        ]
        for stage in stages:
            bid = _upload_batch(
                session,
                ["Company", "Full", "Email", "Phone"],
                [[f"Atomic-{stage}", "Alan Turing", f"{stage}@example.test", "512-555-0101"]],
                {
                    "company_name": "Company",
                    "contact_full_name": "Full",
                    "contact_email": "Email",
                    "contact_phone": "Phone",
                },
            )
            fp = _dry_run_fingerprint(session, bid)
            freeze = _freeze_snapshot()
            try:
                confirm_admin_crm_import_batch(
                    client_id=session.client_id,
                    batch_id=bid,
                    plan_fingerprint=fp,
                    actor=session.actor(),
                    fail_after=stage,
                )
                _fail(f"{stage} should raise")
            except Exception:
                pass
            _assert_frozen(freeze, f"fault {stage}")
    finally:
        session.close()


def test_confirm_idempotency_and_concurrency() -> None:
    session = _session()
    try:
        bid = _upload_batch(
            session, ["Company"], [["IdemCo"]], {"company_name": "Company"}
        )
        fp = _dry_run_fingerprint(session, bid)
        if _confirm(session, bid, fp=fp).status_code != 200:
            _fail("First confirm must succeed.")
        freeze = _freeze_snapshot()
        if _confirm(session, bid, fp=fp).status_code != 409:
            _fail("Second confirm must be 409.")
        _assert_frozen(freeze, "idempotent reconfirm")

        bid2 = _upload_batch(
            session, ["Company"], [["RaceCo"]], {"company_name": "Company"}
        )
        fp2 = _dry_run_fingerprint(session, bid2)
        actor = session.actor()
        barrier = threading.Barrier(2)
        results: list[str] = []

        def worker() -> None:
            barrier.wait()
            try:
                confirm_admin_crm_import_batch(
                    client_id=session.client_id,
                    batch_id=bid2,
                    plan_fingerprint=fp2,
                    actor=actor,
                )
                results.append("ok")
            except BatchNotReusable:
                results.append("conflict")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(worker) for _ in range(2)]
            concurrent.futures.wait(futs, timeout=30)
        if results.count("ok") != 1 or results.count("conflict") != 1:
            _fail(f"Expected 1 success + 1 conflict, got {results}.")
        with get_connection() as conn:
            n = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM companies WHERE company_name = 'RaceCo'"
                ).fetchone()["n"]
            )
            results_n = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM crm_import_results WHERE batch_id = ?",
                    (bid2,),
                ).fetchone()["n"]
            )
        if n != 1 or results_n != 1:
            _fail("Concurrency must not duplicate entities or audit results.")
    finally:
        session.close()


def test_confirm_reuse_and_ns_numbers() -> None:
    session = _session()
    try:
        marker = secrets.token_hex(5)
        bid = _upload_batch(
            session,
            ["Company"],
            [[f"ReuseCo {marker}"], [f"ReuseCo {marker}"]],
            {"company_name": "Company"},
        )
        fp = _dry_run_fingerprint(session, bid)
        if _confirm(session, bid, fp=fp).status_code != 200:
            _fail("Reuse confirm failed.")
        with get_connection() as conn:
            cos = conn.execute(
                "SELECT id FROM companies WHERE company_name = ?",
                (f"ReuseCo {marker}",),
            ).fetchall()
            if len(cos) != 1:
                _fail("Expected one reused company.")
            ccrs = conn.execute(
                """
                SELECT id FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (session.client_id, int(cos[0]["id"])),
            ).fetchall()
            if len(ccrs) != 1:
                _fail("Expected one reused relationship.")
            res = conn.execute(
                """
                SELECT company_id, relationship_id FROM crm_import_results
                WHERE batch_id = ? ORDER BY source_row_number
                """,
                (bid,),
            ).fetchall()
            if len(res) != 2:
                _fail("Expected two result rows.")
            if int(res[0]["company_id"]) != int(res[1]["company_id"]):
                _fail("Company IDs must match across reuse.")
            if int(res[0]["relationship_id"]) != int(res[1]["relationship_id"]):
                _fail("Relationship IDs must match across reuse.")

        m1, m2 = secrets.token_hex(4), secrets.token_hex(4)
        bid = _upload_batch(
            session,
            ["Company"],
            [[f"NSCo1 {m1}"], [f"NSCo2 {m2}"]],
            {"company_name": "Company"},
        )
        fp = _dry_run_fingerprint(session, bid)
        if _confirm(session, bid, fp=fp).status_code != 200:
            _fail("NS confirm failed.")
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT external_record_no FROM companies
                WHERE company_name IN (?, ?)
                ORDER BY external_record_no
                """,
                (f"NSCo1 {m1}", f"NSCo2 {m2}"),
            ).fetchall()
            nums = [int(str(r["external_record_no"])[3:]) for r in rows]
            if len(nums) != 2 or nums[1] != nums[0] + 1:
                _fail(f"Expected sequential NS numbers, got {nums}.")
    finally:
        session.close()


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    _prove_isolated()
    test_confirm_never_calls_ensure_schema_and_performs_no_ddl()
    test_confirm_works_after_explicit_isolated_migration()
    test_confirm_authorization_complete_freeze()
    test_confirm_request_and_batch_state_refusals()
    test_confirm_stale_fingerprints()
    test_confirm_needs_review_refusals()
    test_confirm_proposed_key_and_ownership_invariants()
    test_confirm_happy_paths_audit_and_phone()
    test_confirm_atomicity_fault_injection_full_freeze()
    test_confirm_idempotency_and_concurrency()
    test_confirm_reuse_and_ns_numbers()
    print("test_crm_import_confirm: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
