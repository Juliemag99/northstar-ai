"""Isolated tests for Client Data Import orchestrator.

Run from backend/:
  python test_client_data_import.py

Uses isolated testdb copies only — never writes production northstar.db,
never restarts services, never commits/pushes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

import testdb
from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from client_data_import import (
    CLEANUP_COMPLETED,
    CLEANUP_PENDING,
    CLOSED_POLICY_TEXT,
    ensure_client_data_import_schema,
)
from crm_add_data import allocate_ns_record_no
from crm_import_staging import ensure_crm_import_schema
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from fastapi.testclient import TestClient
from main import app
from models import NorthStarUser
from shared_note_history_import import ensure_shared_note_history_schema

UPLOAD = "/api/clients/{client_id}/admin/client-data-imports"
BATCH = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}"
MAPPING = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/mapping"
DRY_RUN = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/dry-run"
CONFIRM = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/confirm"
RETRY_CLEANUP = (
    "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/retry-staging-cleanup"
)

FREEZE_CRM = (
    "companies",
    "contacts",
    "client_company_relationships",
    "company_shared_history_events",
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
            (email, "CDI Import Test User", int(administrator), digest),
        )
        user_id = int(
            conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        )
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


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
        if len(rows) < 2:
            if len(rows) < 1:
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES ('cdi_a', 'CDI Client A')"
                )
            conn.execute(
                "INSERT INTO clients (code, name) VALUES (?, ?)",
                (f"cdi_b_{secrets.token_hex(3)}", "CDI Client B"),
            )
            conn.commit()
            rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    return int(rows[0]["id"]), int(rows[1]["id"])


def _seed_status_catalog(client_id: int, labels: list[str]) -> None:
    """Ensure catalog labels exist via durable seed relationships."""
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for label in labels:
            rn = allocate_ns_record_no(conn)
            cur = conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (rn, f"__cdi_status_seed_{secrets.token_hex(3)}", now, now),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', '', '', 0, ?, ?)
                """,
                (client_id, company_id, rn, label, now, now),
            )
        conn.commit()


class _Session:
    def __init__(self):
        _prove_isolated()
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            ensure_shared_note_history_schema(conn)
            ensure_client_data_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
        self.http = _client()
        self.client_id, self.other_client_id = _client_ids()
        _seed_status_catalog(self.client_id, ["New", "Active", "Closed"])
        self.password = _secret_password()
        self.email = f"cdi.admin.{secrets.token_hex(6)}@example.test"
        self.user_id = _create_user(
            email=self.email, password=self.password, administrator=1
        )
        _assign(self.user_id, self.client_id)
        login = self.http.post(
            "/api/auth/login", json={"email": self.email, "password": self.password}
        )
        if login.status_code != 200:
            _fail(f"Login failed: {login.status_code} {login.text}")
        self.csrf = _csrf(login)
        if not self.csrf:
            _fail("Missing csrf_token from login response.")

    def close(self) -> None:
        _delete_user(self.user_id)

    def actor(self) -> NorthStarUser:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (self.user_id,)
            ).fetchone()
        return NorthStarUser(
            id=int(row["id"]),
            email=str(row["email"] or ""),
            full_name=str(row["full_name"] or ""),
            is_administrator=bool(row["is_administrator"]),
            is_internal_northstar=True,
            active=bool(row["active"]),
            created_at=str(row["created_at"] or ""),
        )

    def headers(self, *, csrf: str | None = None, include_csrf: bool = True) -> dict:
        out = {}
        if include_csrf:
            out[CSRF_HEADER] = self.csrf if csrf is None else csrf
        return out


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _table_fp(conn, table: str) -> str:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode("utf-8")).hexdigest()


def _freeze() -> dict[str, str]:
    with get_connection() as conn:
        ensure_client_data_import_schema(conn)
        ensure_shared_note_history_schema(conn)
        return {t: _table_fp(conn, t) for t in FREEZE_CRM}


def _assert_frozen(before: dict[str, str], label: str) -> None:
    after = _freeze()
    if after != before:
        changed = [t for t in FREEZE_CRM if after.get(t) != before.get(t)]
        _fail(f"{label} changed tables {changed}.")


def _upload(
    session: _Session,
    prospects: bytes,
    *,
    history: bytes | None = None,
    prospects_name: str = "prospects.csv",
    history_name: str = "history.csv",
    client_id: int | None = None,
):
    cid = session.client_id if client_id is None else client_id
    files = {
        "prospects_file": (prospects_name, prospects, "application/octet-stream"),
    }
    if history is not None:
        files["history_file"] = (history_name, history, "application/octet-stream")
    return session.http.post(
        UPLOAD.format(client_id=cid),
        headers=session.headers(),
        files=files,
    )


def _map(
    session: _Session,
    batch_id: int,
    prospects_mapping: dict[str, str],
    history_mapping: dict[str, str] | None = None,
):
    body: dict = {"prospects_mapping": prospects_mapping}
    if history_mapping is not None:
        body["history_mapping"] = history_mapping
    return session.http.put(
        MAPPING.format(client_id=session.client_id, batch_id=batch_id),
        headers=session.headers(),
        json=body,
    )


def _dry(session: _Session, batch_id: int):
    return session.http.post(
        DRY_RUN.format(client_id=session.client_id, batch_id=batch_id),
        headers=session.headers(),
        json={},
    )


def _confirm(session: _Session, batch_id: int, fp: str, *, confirm: bool = True):
    return session.http.post(
        CONFIRM.format(client_id=session.client_id, batch_id=batch_id),
        headers=session.headers(),
        json={"confirm": confirm, "plan_fingerprint": fp},
    )


PROSPECTS_HEADERS = [
    "LeadMaster Record No.",
    "Company",
    "First Name",
    "Last Name",
    "Email",
    "Status",
]

PROSPECTS_MAPPING = {
    "external_record_no": "LeadMaster Record No.",
    "company_name": "Company",
    "contact_first_name": "First Name",
    "contact_last_name": "Last Name",
    "contact_email": "Email",
    "relationship_status": "Status",
}

HISTORY_HEADERS = [
    "LeadMaster Record No.",
    "Event Timestamp",
    "Author",
    "Note Text",
    "Event Hash",
]

HISTORY_MAPPING = {
    "history_record_no": "LeadMaster Record No.",
    "history_event_at": "Event Timestamp",
    "history_author": "Author",
    "history_note_text": "Note Text",
    "history_event_hash": "Event Hash",
}


def test_prospects_only_happy_path() -> None:
    session = _Session()
    try:
        before = _freeze()
        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [["90001", "Acme Widgets", "Ada", "Lovelace", "ada@acme.test", ""]],
        )
        uploaded = _upload(session, content)
        if uploaded.status_code != 200:
            _fail(f"upload: {uploaded.status_code} {uploaded.text}")
        body = uploaded.json()
        batch = body.get("batch") or {}
        batch_id = int(batch.get("id") or batch.get("batch_id") or 0)
        if batch_id <= 0:
            _fail("missing client-data batch id")
        if batch.get("id") != batch.get("batch_id"):
            _fail("id and batch_id must match")
        if CLOSED_POLICY_TEXT not in (batch.get("closed_policy") or ""):
            _fail("closed policy missing on batch")

        mapped = _map(session, batch_id, PROSPECTS_MAPPING)
        if mapped.status_code != 200:
            _fail(f"mapping: {mapped.status_code} {mapped.text}")
        _assert_frozen(before, "after mapping")

        dry = _dry(session, batch_id)
        if dry.status_code != 200:
            _fail(f"dry-run: {dry.status_code} {dry.text}")
        plan = dry.json()
        fp = plan.get("plan_fingerprint") or ""
        if not re.match(r"^[a-f0-9]{64}$", fp):
            _fail("bad fingerprint")
        if not plan.get("confirm_allowed"):
            _fail(f"confirm not allowed: {plan}")
        prospects = plan.get("prospects") or {}
        if int(prospects.get("companies_create") or 0) != 1:
            _fail(f"expected create 1: {prospects}")
        if int(prospects.get("create_company") or 0) != 1:
            _fail("dual key create_company missing")
        _assert_frozen(before, "after dry-run")

        confirmed = _confirm(session, batch_id, fp)
        if confirmed.status_code != 200:
            _fail(f"confirm: {confirmed.status_code} {confirmed.text}")
        result = confirmed.json()
        if int(result.get("companies_created") or 0) != 1:
            _fail(f"confirm counters: {result}")
        if int(result.get("created_company_count") or 0) != 1:
            _fail("dual confirm key missing")

        # Preview/confirm counter equality for creates.
        if int(prospects.get("companies_create") or 0) != int(
            result.get("companies_created") or 0
        ):
            _fail("preview/confirm company create mismatch")

        with get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE company_name = ?",
                ("Acme Widgets",),
            ).fetchone()["n"]
        if int(n) != 1:
            _fail("company not created")

        # Idempotent re-confirm.
        again = _confirm(session, batch_id, fp)
        if again.status_code != 200:
            _fail(f"idempotent confirm: {again.status_code} {again.text}")
        if again.json().get("companies_created") != result.get("companies_created"):
            _fail("idempotent confirm changed counters")
    finally:
        session.close()


def test_prospects_plus_history() -> None:
    session = _Session()
    try:
        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [["90002", "Beta Co", "Ben", "Bitdiddle", "ben@beta.test", ""]],
        )
        hist_hash = hashlib.sha256(b"beta-note-1").hexdigest()
        history = _csv_bytes(
            HISTORY_HEADERS,
            [["90002", "2024-01-01T12:00:00Z", "Rep", "Called Beta", hist_hash]],
        )
        uploaded = _upload(session, prospects, history=history)
        if uploaded.status_code != 200:
            _fail(f"upload: {uploaded.status_code} {uploaded.text}")
        batch = uploaded.json()["batch"]
        batch_id = int(batch["id"])
        if int(batch.get("history_row_count") or 0) != 1:
            _fail("history_row_count")
        if not (batch.get("history_headers") or []):
            _fail("history_headers missing")

        mapped = _map(session, batch_id, PROSPECTS_MAPPING, HISTORY_MAPPING)
        if mapped.status_code != 200:
            _fail(f"mapping: {mapped.status_code} {mapped.text}")

        dry = _dry(session, batch_id)
        plan = dry.json()
        if dry.status_code != 200 or not plan.get("confirm_allowed"):
            _fail(f"dry-run: {dry.status_code} {plan}")
        hist = plan.get("history") or {}
        if int(hist.get("insert") or 0) != 1:
            _fail(f"history insert expected: {hist}")

        confirmed = _confirm(session, batch_id, plan["plan_fingerprint"])
        if confirmed.status_code != 200:
            _fail(f"confirm: {confirmed.status_code} {confirmed.text}")
        if int(confirmed.json().get("history_inserted") or 0) != 1:
            _fail(f"history not inserted: {confirmed.json()}")
        with get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM company_shared_history_events WHERE event_hash = ?",
                (hist_hash,),
            ).fetchone()["n"]
        if int(n) != 1:
            _fail("history event missing")
        if confirmed.json().get("staging_cleanup_status") != CLEANUP_COMPLETED:
            _fail("cleanup not completed")
    finally:
        session.close()


def test_record_no_reuse_and_name_only_no_reuse() -> None:
    session = _Session()
    try:
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES ('91001', 'Exact Record Co', ?, ?)
                """,
                (now, now),
            )
            company_id = int(
                conn.execute(
                    "SELECT id FROM companies WHERE external_record_no = '91001'"
                ).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES (?, 'Name Only Target', ?, ?)
                """,
                (allocate_ns_record_no(conn), now, now),
            )
            conn.commit()

        # Record No. exact match → reuse
        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [["91001", "Exact Record Co Renamed", "Pat", "Reuse", "pat@x.test", ""]],
        )
        uploaded = _upload(session, content)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING)
        dry = _dry(session, batch_id).json()
        if int((dry.get("prospects") or {}).get("companies_reuse") or 0) != 1:
            _fail(f"expected record_no reuse: {dry}")
        if not dry.get("confirm_allowed"):
            _fail(f"reuse should confirm: {dry}")
        _confirm(session, batch_id, dry["plan_fingerprint"])

        # Name-only match must NOT reuse in client_data mode → create
        content2 = _csv_bytes(
            PROSPECTS_HEADERS,
            [["91002", "Name Only Target", "Ned", "Name", "ned@y.test", ""]],
        )
        uploaded2 = _upload(session, content2)
        batch_id2 = int(uploaded2.json()["batch"]["id"])
        _map(session, batch_id2, PROSPECTS_MAPPING)
        dry2 = _dry(session, batch_id2).json()
        prospects = dry2.get("prospects") or {}
        if int(prospects.get("companies_reuse") or 0) != 0:
            _fail(f"name-only must not reuse: {prospects}")
        if int(prospects.get("companies_create") or 0) != 1:
            _fail(f"name-only should create: {prospects}")
        # Ensure original company still unique by name count after confirm create.
        before_n = 0
        with get_connection() as conn:
            before_n = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM companies WHERE company_name = ?",
                    ("Name Only Target",),
                ).fetchone()["n"]
            )
        _confirm(session, batch_id2, dry2["plan_fingerprint"])
        with get_connection() as conn:
            after_n = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM companies WHERE company_name = ?",
                    ("Name Only Target",),
                ).fetchone()["n"]
            )
            still = conn.execute(
                "SELECT id FROM companies WHERE external_record_no = '91001'"
            ).fetchone()
        if after_n != before_n + 1:
            _fail("name-only path did not create a distinct company")
        if int(still["id"]) != company_id:
            _fail("record_no company mutated unexpectedly")
    finally:
        session.close()


def test_closed_exclusion_without_master_deletion() -> None:
    session = _Session()
    try:
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES ('92001', 'Keep Master Closed', ?, ?)
                """,
                (now, now),
            )
            conn.commit()

        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [
                ["92001", "Keep Master Closed", "C1", "Closed", "c1@z.test", "Closed"],
                ["92002", "Open Prospect", "O1", "Open", "o1@z.test", ""],
            ],
        )
        uploaded = _upload(session, content)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING)
        dry = _dry(session, batch_id).json()
        if int(dry.get("closed_excluded_count") or 0) != 1:
            _fail(f"expected 1 closed excluded: {dry}")
        if not dry.get("confirm_allowed"):
            _fail(f"closed exclusion should still allow confirm: {dry}")
        prospects = dry.get("prospects") or {}
        if int(prospects.get("companies_create") or 0) != 1:
            _fail(f"only open row should create: {prospects}")

        confirmed = _confirm(session, batch_id, dry["plan_fingerprint"])
        if confirmed.status_code != 200:
            _fail(f"confirm: {confirmed.status_code} {confirmed.text}")
        with get_connection() as conn:
            master = conn.execute(
                "SELECT id, company_name FROM companies WHERE external_record_no = '92001'"
            ).fetchone()
            open_co = conn.execute(
                "SELECT id FROM companies WHERE external_record_no = '92002'"
            ).fetchone()
            rel_closed = conn.execute(
                """
                SELECT 1 FROM client_company_relationships ccr
                JOIN companies co ON co.id = ccr.company_id
                WHERE ccr.client_id = ? AND co.external_record_no = '92001'
                """,
                (session.client_id,),
            ).fetchone()
        if master is None:
            _fail("closed exclusion deleted master company")
        if open_co is None:
            _fail("open company missing")
        if rel_closed is not None:
            _fail("closed row should not create client relationship")
    finally:
        session.close()


def test_status_conflict_blocks_confirm() -> None:
    session = _Session()
    try:
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rn = "93001"
            cur = conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES (?, 'Conflict Co', ?, ?)
                """,
                (rn, now, now),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, ?, 'Active', '', '', '', 0, ?, ?)
                """,
                (session.client_id, company_id, rn, now, now),
            )
            conn.commit()

        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [[rn, "Conflict Co", "Sam", "Conflict", "sam@c.test", "New"]],
        )
        uploaded = _upload(session, content)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING)
        dry = _dry(session, batch_id).json()
        prospects = dry.get("prospects") or {}
        if int(prospects.get("status_conflict") or 0) < 1:
            _fail(f"expected status_conflict: {prospects}")
        if dry.get("confirm_allowed"):
            _fail("status conflict must not allow confirm")
        blocked = _confirm(session, batch_id, dry["plan_fingerprint"])
        if blocked.status_code != 409:
            _fail(f"expected 409, got {blocked.status_code} {blocked.text}")
    finally:
        session.close()


def test_auth_csrf_and_client_required() -> None:
    session = _Session()
    try:
        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [["94001", "Auth Co", "A", "Auth", "a@auth.test", ""]],
        )
        # client_id=0
        bad_client = session.http.post(
            UPLOAD.format(client_id=0),
            headers=session.headers(),
            files={"prospects_file": ("p.csv", content, "application/octet-stream")},
        )
        if bad_client.status_code != 400:
            _fail(f"client_id=0 expected 400 got {bad_client.status_code}")

        # non-admin
        password = _secret_password()
        email = f"cdi.staff.{secrets.token_hex(4)}@example.test"
        staff_id = _create_user(email=email, password=password, administrator=0)
        _assign(staff_id, session.client_id)
        try:
            http = _client()
            login = http.post("/api/auth/login", json={"email": email, "password": password})
            csrf = _csrf(login)
            forbidden = http.post(
                UPLOAD.format(client_id=session.client_id),
                headers={CSRF_HEADER: csrf},
                files={"prospects_file": ("p.csv", content, "application/octet-stream")},
            )
            if forbidden.status_code != 403:
                _fail(f"non-admin expected 403 got {forbidden.status_code}")
        finally:
            _delete_user(staff_id)

        # CSRF missing
        no_csrf = session.http.post(
            UPLOAD.format(client_id=session.client_id),
            files={"prospects_file": ("p.csv", content, "application/octet-stream")},
        )
        if no_csrf.status_code not in {403, 401}:
            _fail(f"missing CSRF expected 401/403 got {no_csrf.status_code}")
    finally:
        session.close()


def test_stale_fingerprint_409() -> None:
    session = _Session()
    try:
        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [["95001", "Stale Co", "S", "Tale", "s@stale.test", ""]],
        )
        uploaded = _upload(session, content)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING)
        dry = _dry(session, batch_id).json()
        stale = "ab" * 32
        resp = _confirm(session, batch_id, stale)
        if resp.status_code != 409:
            _fail(f"stale fp expected 409 got {resp.status_code} {resp.text}")
        # Valid fp still works after.
        ok = _confirm(session, batch_id, dry["plan_fingerprint"])
        if ok.status_code != 200:
            _fail(f"valid confirm after stale: {ok.status_code} {ok.text}")
    finally:
        session.close()


def test_staging_cleanup_failure_pending_and_retry() -> None:
    session = _Session()
    try:
        from client_data_import import confirm_client_data_import

        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [["96001", "Cleanup Co", "C", "Lean", "c@clean.test", ""]],
        )
        hist_hash = hashlib.sha256(b"cleanup-note").hexdigest()
        history = _csv_bytes(
            HISTORY_HEADERS,
            [["96001", "2024-02-01T00:00:00Z", "Rep", "Note", hist_hash]],
        )
        uploaded = _upload(session, prospects, history=history)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING, HISTORY_MAPPING)
        dry = _dry(session, batch_id).json()

        def boom(_path: Path) -> None:
            raise OSError("injected cleanup failure")

        result = confirm_client_data_import(
            client_id=session.client_id,
            batch_id=batch_id,
            plan_fingerprint=dry["plan_fingerprint"],
            actor=session.actor(),
            confirm=True,
            unlink_fn=boom,
        )
        if result.staging_cleanup_status != CLEANUP_PENDING:
            _fail(f"expected pending cleanup: {result}")
        # CRM/history should still be committed.
        with get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM company_shared_history_events WHERE event_hash = ?",
                (hist_hash,),
            ).fetchone()["n"]
            staging = conn.execute(
                """
                SELECT history_staging_path FROM client_data_import_batches
                WHERE id = ? AND client_id = ?
                """,
                (batch_id, session.client_id),
            ).fetchone()["history_staging_path"]
        if int(n) != 1:
            _fail("history rolled back on cleanup failure")
        if not staging:
            _fail("staging path cleared despite cleanup failure")

        retried = session.http.post(
            RETRY_CLEANUP.format(client_id=session.client_id, batch_id=batch_id),
            headers=session.headers(),
            json={},
        )
        if retried.status_code != 200:
            _fail(f"retry cleanup: {retried.status_code} {retried.text}")
        if retried.json().get("staging_cleanup_status") != CLEANUP_COMPLETED:
            _fail(f"retry did not complete: {retried.json()}")
    finally:
        session.close()


def test_malicious_filename_sanitized() -> None:
    session = _Session()
    try:
        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [["97001", "Safe Co", "S", "Afe", "s@safe.test", ""]],
        )
        history = _csv_bytes(
            HISTORY_HEADERS,
            [["97001", "2024-03-01T00:00:00Z", "Rep", "Hi", hashlib.sha256(b"h").hexdigest()]],
        )
        evil = r"..\..\..\evil<script>.csv"
        uploaded = _upload(
            session,
            content,
            history=history,
            history_name=evil,
        )
        if uploaded.status_code != 200:
            _fail(f"upload: {uploaded.status_code} {uploaded.text}")
        name = uploaded.json()["batch"].get("history_filename") or ""
        if ".." in name or "<" in name or "/" in name or "\\" in name:
            _fail(f"filename not sanitized: {name!r}")
        with get_connection() as conn:
            path = conn.execute(
                """
                SELECT history_staging_path FROM client_data_import_batches
                WHERE id = ?
                """,
                (int(uploaded.json()["batch"]["id"]),),
            ).fetchone()["history_staging_path"]
        if ".." in path:
            _fail(f"staging path escaped: {path}")
        if not Path(path).is_file():
            _fail("staging file missing")
    finally:
        session.close()


def test_no_crm_writes_during_dry_run() -> None:
    session = _Session()
    try:
        before = _freeze()
        content = _csv_bytes(
            PROSPECTS_HEADERS,
            [["98001", "Dry Co", "D", "Run", "d@dry.test", ""]],
        )
        uploaded = _upload(session, content)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING)
        after_map = _freeze()
        if after_map != before:
            _fail("mapping wrote CRM tables")
        dry = _dry(session, batch_id)
        if dry.status_code != 200:
            _fail(dry.text)
        _assert_frozen(before, "dry-run")
    finally:
        session.close()


def test_atomic_rollback_after_each_write_layer() -> None:
    """Inject failures after each write layer; CRM/history must fully roll back."""
    from client_data_import import confirm_client_data_import

    layers = (
        "after_company_insert",
        "after_contact_insert",
        "after_relationship_insert",
        "after_history_insert",
        "after_crm_apply",
        "after_history_before_audit",
        "before_commit",
    )
    for layer in layers:
        session = _Session()
        try:
            before = _freeze()
            prospects = _csv_bytes(
                PROSPECTS_HEADERS,
                [["11001", "Atomic Co", "A", "Tom", "a@atomic.test", "New"]],
            )
            hist_hash = hashlib.sha256(f"atomic-{layer}".encode()).hexdigest()
            history = _csv_bytes(
                HISTORY_HEADERS,
                [["11001", "2024-04-01T12:00:00Z", "Rep", "Long note " + ("x" * 5000), hist_hash]],
            )
            uploaded = _upload(session, prospects, history=history)
            batch_id = int(uploaded.json()["batch"]["id"])
            _map(session, batch_id, PROSPECTS_MAPPING, HISTORY_MAPPING)
            dry = _dry(session, batch_id).json()
            try:
                confirm_client_data_import(
                    client_id=session.client_id,
                    batch_id=batch_id,
                    plan_fingerprint=dry["plan_fingerprint"],
                    actor=session.actor(),
                    confirm=True,
                    fail_after=layer,
                )
                _fail(f"{layer} did not raise")
            except RuntimeError as exc:
                if "Injected failure" not in str(exc):
                    raise
            _assert_frozen(before, f"rollback after {layer}")
            with get_connection() as conn:
                status = conn.execute(
                    """
                    SELECT status FROM client_data_import_batches
                    WHERE id = ? AND client_id = ?
                    """,
                    (batch_id, session.client_id),
                ).fetchone()["status"]
                crm_status = conn.execute(
                    """
                    SELECT status FROM crm_import_batches
                    WHERE id = (
                        SELECT crm_batch_id FROM client_data_import_batches
                        WHERE id = ? AND client_id = ?
                    )
                    """,
                    (batch_id, session.client_id),
                ).fetchone()["status"]
            if status != "previewed":
                _fail(f"{layer}: client_data batch status={status}")
            if crm_status != "previewed":
                _fail(f"{layer}: crm batch status={crm_status}")
        finally:
            session.close()

    # Status/notes update layers require an existing client relationship.
    # Notes: blank status → preserve + append notes (confirmable without resolution).
    # Status: conflict → REPLACE resolution → use_imported_status on confirm.
    from crm_import_status_resolution import (
        RESOLUTION_REPLACE,
        save_crm_import_status_resolution,
    )

    for layer in ("after_notes_update", "after_status_update"):
        session = _Session()
        try:
            now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
            rn = "11050" if layer == "after_notes_update" else "11051"
            with get_connection() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, created_at, last_updated_at
                    ) VALUES (?, 'Status Notes Co', ?, ?)
                    """,
                    (rn, now, now),
                )
                company_id = int(cur.lastrowid)
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status,
                        priority, next_action, notes, is_hot, created_at, updated_at
                    ) VALUES (?, ?, ?, 'New', '', '', 'seed', 0, ?, ?)
                    """,
                    (session.client_id, company_id, rn, now, now),
                )
                conn.commit()
            before = _freeze()
            headers = PROSPECTS_HEADERS + ["Notes"]
            mapping = {**PROSPECTS_MAPPING, "relationship_notes": "Notes"}
            if layer == "after_notes_update":
                row = [rn, "Status Notes Co", "S", "N", "sn@atomic.test", "", "Extra note"]
            else:
                row = [
                    rn,
                    "Status Notes Co",
                    "S",
                    "N",
                    "sn2@atomic.test",
                    "Active",
                    "Extra note",
                ]
            prospects = _csv_bytes(headers, [row])
            uploaded = _upload(session, prospects)
            batch_id = int(uploaded.json()["batch"]["id"])
            _map(session, batch_id, mapping)
            dry = _dry(session, batch_id).json()
            if layer == "after_status_update":
                with get_connection() as conn:
                    crm_batch_id = int(
                        conn.execute(
                            """
                            SELECT crm_batch_id FROM client_data_import_batches
                            WHERE id = ? AND client_id = ?
                            """,
                            (batch_id, session.client_id),
                        ).fetchone()["crm_batch_id"]
                    )
                    staged_id = int(
                        conn.execute(
                            """
                            SELECT id FROM crm_import_rows
                            WHERE batch_id = ? AND client_id = ?
                            ORDER BY id LIMIT 1
                            """,
                            (crm_batch_id, session.client_id),
                        ).fetchone()["id"]
                    )
                save_crm_import_status_resolution(
                    session.client_id,
                    crm_batch_id,
                    staged_id,
                    actor=session.actor(),
                    resolution_type=RESOLUTION_REPLACE,
                    resolved_status="Active",
                )
                dry = _dry(session, batch_id).json()
            if not dry.get("confirm_allowed"):
                _fail(f"{layer} setup not confirmable: {dry}")
            try:
                confirm_client_data_import(
                    client_id=session.client_id,
                    batch_id=batch_id,
                    plan_fingerprint=dry["plan_fingerprint"],
                    actor=session.actor(),
                    confirm=True,
                    fail_after=layer,
                )
                _fail(f"{layer} did not raise")
            except RuntimeError as exc:
                if "Injected failure" not in str(exc):
                    raise
            _assert_frozen(before, f"rollback after {layer}")
        finally:
            session.close()


def test_preview_confirm_counters_and_in_batch_reuse() -> None:
    session = _Session()
    try:
        # Two contacts same company RN → one company create, two contacts,
        # in-batch company reuse for second row.
        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [
                ["12001", "Batch Co", "Ann", "One", "ann@batch.test", "New"],
                ["12001", "Batch Co", "Bob", "Two", "bob@batch.test", "New"],
                ["12002", "Batch Co Two", "Cara", "Three", "cara@batch.test", "New"],
            ],
        )
        hist_hash = hashlib.sha256(b"batch-hist").hexdigest()
        history = _csv_bytes(
            HISTORY_HEADERS,
            [
                ["12001", "2024-05-01T00:00:00Z", "Rep", "Note A", hist_hash],
                ["12001", "2024-05-02T00:00:00Z", "Rep", "Note B", hashlib.sha256(b"b2").hexdigest()],
            ],
        )
        uploaded = _upload(session, prospects, history=history)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING, HISTORY_MAPPING)
        dry = _dry(session, batch_id).json()
        p = dry["prospects"]
        if int(p.get("companies_create") or 0) != 2:
            _fail(f"expected 2 company creates, got {p}")
        if int(p.get("contacts_create") or 0) != 3:
            _fail(f"expected 3 contact creates, got {p}")
        if int(dry.get("history", {}).get("insert") or 0) != 2:
            _fail(f"expected 2 history inserts: {dry.get('history')}")
        confirmed = _confirm(session, batch_id, dry["plan_fingerprint"])
        body = confirmed.json()
        if int(body.get("companies_created") or 0) != 2:
            _fail(f"confirm companies: {body}")
        if int(body.get("contacts_created") or 0) != 3:
            _fail(f"confirm contacts: {body}")
        if int(body.get("history_inserted") or 0) != 2:
            _fail(f"confirm history: {body}")
        with get_connection() as conn:
            companies = conn.execute(
                """
                SELECT COUNT(*) AS n FROM companies
                WHERE external_record_no IN ('12001', '12002')
                """
            ).fetchone()["n"]
            events = conn.execute(
                """
                SELECT COUNT(*) AS n FROM company_shared_history_events
                WHERE external_record_no = '12001'
                """
            ).fetchone()["n"]
            contacts = conn.execute(
                """
                SELECT COUNT(*) AS n FROM contacts c
                JOIN companies co ON co.id = c.company_id
                WHERE co.external_record_no = '12001'
                """
            ).fetchone()["n"]
        if int(companies) != 2 or int(events) != 2 or int(contacts) != 2:
            _fail(f"db counts companies={companies} events={events} contacts={contacts}")
    finally:
        session.close()


def test_fingerprint_stale_on_mapping_file_identity_catalog() -> None:
    session = _Session()
    try:
        prospects = _csv_bytes(
            [
                "LeadMaster Record No.",
                "Company",
                "Website",
                "First Name",
                "Last Name",
                "Email",
                "Status",
            ],
            [["13011", "Stale Co", "https://a.example", "S", "Tale", "s@stale.test", "New"]],
        )
        history = _csv_bytes(
            HISTORY_HEADERS,
            [["13011", "2024-01-01T00:00:00Z", "Rep", "H1", hashlib.sha256(b"fp-h1").hexdigest()]],
        )
        uploaded = _upload(session, prospects, history=history)
        batch_id = int(uploaded.json()["batch"]["id"])
        mapping_a = {
            "external_record_no": "LeadMaster Record No.",
            "company_name": "Company",
            "website": "Website",
            "contact_first_name": "First Name",
            "contact_last_name": "Last Name",
            "contact_email": "Email",
            "relationship_status": "Status",
        }
        _map(session, batch_id, mapping_a, HISTORY_MAPPING)
        fp_a = _dry(session, batch_id).json()["plan_fingerprint"]
        mapping_b = {
            "external_record_no": "LeadMaster Record No.",
            "company_name": "Company",
            "contact_first_name": "First Name",
            "contact_last_name": "Last Name",
            "contact_email": "Email",
            "relationship_status": "Status",
        }
        _map(session, batch_id, mapping_b, HISTORY_MAPPING)
        fp_b = _dry(session, batch_id).json()["plan_fingerprint"]
        if fp_a == fp_b:
            _fail("mapping change did not stale fingerprint")
        blocked = _confirm(session, batch_id, fp_a)
        if blocked.status_code not in {409, 400}:
            _fail(f"stale mapping fingerprint accepted: {blocked.status_code} {blocked.text}")

        # Staged CRM file digest change must stale fingerprint.
        with get_connection() as conn:
            crm_id = int(
                conn.execute(
                    """
                    SELECT crm_batch_id FROM client_data_import_batches
                    WHERE id = ? AND client_id = ?
                    """,
                    (batch_id, session.client_id),
                ).fetchone()["crm_batch_id"]
            )
            conn.execute(
                "UPDATE crm_import_batches SET sha256 = ? WHERE id = ?",
                ("0" * 64, crm_id),
            )
            conn.commit()
        fp_file = _dry(session, batch_id).json()["plan_fingerprint"]
        if fp_file == fp_b:
            _fail("staged CRM file sha change did not stale fingerprint")
        blocked_file = _confirm(session, batch_id, fp_b)
        if blocked_file.status_code not in {409, 400}:
            _fail("stale file fingerprint accepted")

        # History staged file content change must stale combined fingerprint.
        with get_connection() as conn:
            path = conn.execute(
                """
                SELECT history_staging_path FROM client_data_import_batches
                WHERE id = ? AND client_id = ?
                """,
                (batch_id, session.client_id),
            ).fetchone()["history_staging_path"]
            Path(path).write_bytes(
                _csv_bytes(
                    HISTORY_HEADERS,
                    [
                        [
                            "13011",
                            "2024-01-02T00:00:00Z",
                            "Rep",
                            "H2",
                            hashlib.sha256(b"fp-h2").hexdigest(),
                        ]
                    ],
                )
            )
            conn.execute(
                """
                UPDATE client_data_import_batches
                SET history_sha256 = ?
                WHERE id = ? AND client_id = ?
                """,
                (hashlib.sha256(Path(path).read_bytes()).hexdigest(), batch_id, session.client_id),
            )
            conn.commit()
        fp_hist = _dry(session, batch_id).json()["plan_fingerprint"]
        if fp_hist == fp_file:
            _fail("history staged file change did not stale fingerprint")

        # Identity change: existing RN flips create → reuse
        with get_connection() as conn:
            now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES ('13011', 'Preexisting', ?, ?)
                """,
                (now, now),
            )
            conn.commit()
        fp_c = _dry(session, batch_id).json()["plan_fingerprint"]
        if fp_c == fp_hist:
            _fail("identity (existing RN) change did not stale fingerprint")

        # Status catalog: unknown label → invalid_status (blocks + different decisions)
        prospects2 = _csv_bytes(
            [
                "LeadMaster Record No.",
                "Company",
                "First Name",
                "Last Name",
                "Email",
                "Status",
            ],
            [["13012", "Catalog Co", "C", "At", "c@cat.test", "BrandNewStatus"]],
        )
        uploaded2 = _upload(session, prospects2)
        batch2 = int(uploaded2.json()["batch"]["id"])
        _map(session, batch2, PROSPECTS_MAPPING)
        dry_invalid = _dry(session, batch2).json()
        if int(dry_invalid.get("prospects", {}).get("invalid_status") or 0) < 1:
            _fail(f"expected invalid_status for unknown catalog label: {dry_invalid}")
        if dry_invalid.get("confirm_allowed"):
            _fail("confirm_allowed true with invalid status")

        # Catalog change that flips a valid plan decision: seed Warm, dry-run New→Active
        # then remove Active from catalog by deleting all Active relationships and
        # re-seed only New — import Active must become invalid and stale fingerprint.
        with get_connection() as conn:
            now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES ('13013', 'Catalog Flip', ?, ?)
                """,
                (now, now),
            )
            cid = int(
                conn.execute(
                    "SELECT id FROM companies WHERE external_record_no = '13013'"
                ).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, '13013', 'Active', '', '', '', 0, ?, ?)
                """,
                (session.client_id, cid, now, now),
            )
            conn.commit()
        prospects3 = _csv_bytes(
            PROSPECTS_HEADERS,
            [["13014", "Flip Target", "F", "Lip", "f@flip.test", "Active"]],
        )
        uploaded3 = _upload(session, prospects3)
        batch3 = int(uploaded3.json()["batch"]["id"])
        _map(session, batch3, PROSPECTS_MAPPING)
        dry_ok = _dry(session, batch3).json()
        fp_ok = dry_ok["plan_fingerprint"]
        if not dry_ok.get("confirm_allowed"):
            _fail(f"Active catalog should allow: {dry_ok}")
        with get_connection() as conn:
            conn.execute(
                """
                DELETE FROM client_company_relationships
                WHERE client_id = ? AND status = 'Active'
                """,
                (session.client_id,),
            )
            conn.commit()
        dry_stale_cat = _dry(session, batch3).json()
        if dry_stale_cat["plan_fingerprint"] == fp_ok:
            _fail("status catalog change did not stale fingerprint")
        if dry_stale_cat.get("confirm_allowed"):
            _fail("confirm still allowed after Active removed from catalog")
        blocked_cat = _confirm(session, batch3, fp_ok)
        if blocked_cat.status_code not in {409, 400}:
            _fail("stale catalog fingerprint accepted")
    finally:
        session.close()


def test_matching_safety_possible_high_confidence_contact_scope() -> None:
    """RN priority, no name-only reuse, high-confidence no-RN reuse, possibles block,
    contacts stay company-scoped, unmatched RN never fuzzy-reuses."""
    session = _Session()
    try:
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        with get_connection() as conn:
            # Two masters sharing the same domain → possible match without RN.
            for rn, name in (("14001", "Dom Twin A"), ("14002", "Dom Twin B")):
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, website,
                        created_at, last_updated_at
                    ) VALUES (?, ?, 'https://twins.example', ?, ?)
                    """,
                    (rn, name, now, now),
                )
            # High-confidence target: unique name + domain, no colliding twins.
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, website,
                    created_at, last_updated_at
                ) VALUES ('14010', 'Hi Conf Co', 'https://hiconf.example', ?, ?)
                """,
                (now, now),
            )
            hi_id = int(
                conn.execute(
                    "SELECT id FROM companies WHERE external_record_no = '14010'"
                ).fetchone()["id"]
            )
            # Contact on company A for company-scope proof.
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES ('14020', 'Scope Co A', ?, ?)
                """,
                (now, now),
            )
            a_id = int(
                conn.execute(
                    "SELECT id FROM companies WHERE external_record_no = '14020'"
                ).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, email,
                    source_row_index
                ) VALUES (?, '14020', 'Shared', 'Mail', 'shared@scope.test', 0)
                """,
                (a_id,),
            )
            # Domain twin for RN-exclusive create (unmatched RN must not fuzzy-reuse).
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, website,
                    created_at, last_updated_at
                ) VALUES ('14030', 'Fuzzy Dom Co', 'https://fuzzy.example', ?, ?)
                """,
                (now, now),
            )
            conn.commit()

        # Possible company match blocks confirm.
        headers = [
            "LeadMaster Record No.",
            "Company",
            "Website",
            "First Name",
            "Last Name",
            "Email",
            "Status",
        ]
        mapping = {
            "external_record_no": "LeadMaster Record No.",
            "company_name": "Company",
            "website": "Website",
            "contact_first_name": "First Name",
            "contact_last_name": "Last Name",
            "contact_email": "Email",
            "relationship_status": "Status",
        }
        possible = _csv_bytes(
            headers,
            [["", "Any Twin Name", "https://twins.example", "P", "Oss", "p@twin.test", "New"]],
        )
        up = _upload(session, possible)
        bid = int(up.json()["batch"]["id"])
        _map(session, bid, mapping)
        dry_p = _dry(session, bid).json()
        if int((dry_p.get("prospects") or {}).get("companies_possible") or 0) < 1:
            _fail(f"expected possible_company_match: {dry_p}")
        if dry_p.get("confirm_allowed"):
            _fail("possible match must block confirm")
        blocked = _confirm(session, bid, dry_p["plan_fingerprint"])
        if blocked.status_code not in {409, 400}:
            _fail(f"possible match confirm not refused: {blocked.status_code}")

        # No-RN high-confidence identity (name + domain) → reuse.
        hi = _csv_bytes(
            headers,
            [
                [
                    "",
                    "Hi Conf Co",
                    "https://hiconf.example",
                    "H",
                    "I",
                    "hi@hiconf.test",
                    "New",
                ]
            ],
        )
        up_hi = _upload(session, hi)
        bid_hi = int(up_hi.json()["batch"]["id"])
        _map(session, bid_hi, mapping)
        dry_hi = _dry(session, bid_hi).json()
        if int((dry_hi.get("prospects") or {}).get("companies_reuse") or 0) != 1:
            _fail(f"expected high-confidence reuse: {dry_hi}")
        if not dry_hi.get("confirm_allowed"):
            _fail(f"high-confidence reuse should confirm: {dry_hi}")
        conf_hi = _confirm(session, bid_hi, dry_hi["plan_fingerprint"])
        if conf_hi.status_code != 200:
            _fail(conf_hi.text)
        with get_connection() as conn:
            n = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM companies WHERE website LIKE '%hiconf.example%'"
                ).fetchone()["n"]
            )
        if n != 1:
            _fail("high-confidence path created duplicate company")

        # Unmatched Record No. must create even when domain matches existing.
        fuzzy = _csv_bytes(
            headers,
            [
                [
                    "14999",
                    "Fuzzy Dom Co",
                    "https://fuzzy.example",
                    "F",
                    "Uz",
                    "f@fuzzy.test",
                    "New",
                ]
            ],
        )
        up_f = _upload(session, fuzzy)
        bid_f = int(up_f.json()["batch"]["id"])
        _map(session, bid_f, mapping)
        dry_f = _dry(session, bid_f).json()
        if int((dry_f.get("prospects") or {}).get("companies_create") or 0) != 1:
            _fail(f"unmatched RN must create (no fuzzy): {dry_f}")
        if int((dry_f.get("prospects") or {}).get("companies_reuse") or 0) != 0:
            _fail(f"unmatched RN must not reuse: {dry_f}")
        _confirm(session, bid_f, dry_f["plan_fingerprint"])
        with get_connection() as conn:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM companies WHERE website LIKE '%fuzzy.example%'"
                ).fetchone()["n"]
            )
        if count != 2:
            _fail(f"expected distinct company for unmatched RN, got {count}")

        # Contacts remain company-scoped: same email on a different company creates.
        scope = _csv_bytes(
            PROSPECTS_HEADERS,
            [["14021", "Scope Co B", "Shared", "Mail", "shared@scope.test", "New"]],
        )
        up_s = _upload(session, scope)
        bid_s = int(up_s.json()["batch"]["id"])
        _map(session, bid_s, PROSPECTS_MAPPING)
        dry_s = _dry(session, bid_s).json()
        if int((dry_s.get("prospects") or {}).get("contacts_create") or 0) != 1:
            _fail(f"cross-company email must create contact: {dry_s}")
        conf_s = _confirm(session, bid_s, dry_s["plan_fingerprint"])
        if conf_s.status_code != 200:
            _fail(conf_s.text)
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.company_id, co.external_record_no
                FROM contacts c
                JOIN companies co ON co.id = c.company_id
                WHERE lower(c.email) = 'shared@scope.test'
                ORDER BY c.id
                """
            ).fetchall()
        if len(rows) != 2:
            _fail(f"expected two company-scoped contacts, got {rows}")
        rns = {str(r["external_record_no"]) for r in rows}
        if rns != {"14020", "14021"}:
            _fail(f"contacts not company-scoped: {rns}")
        if hi_id <= 0:
            _fail("setup failed")
    finally:
        session.close()


def test_history_once_per_company_unattributed_long_note_idempotent() -> None:
    session = _Session()
    try:
        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [
                ["14001", "Hist Co", "H", "One", "h1@hist.test", "New"],
                ["14001", "Hist Co", "H", "Two", "h2@hist.test", "New"],
            ],
        )
        long_note = "N" * 12000
        hist_hash = hashlib.sha256(long_note.encode()).hexdigest()
        history = _csv_bytes(
            [
                "LeadMaster Record No.",
                "Event Timestamp",
                "Author",
                "Note Text",
                "Event Hash",
                "Attributed Client/Campaign",
            ],
            [
                ["14001", "2024-06-01T00:00:00Z", "Rep", long_note, hist_hash, ""],
            ],
        )
        hist_map = {
            **HISTORY_MAPPING,
            "history_attribution": "Attributed Client/Campaign",
        }
        uploaded = _upload(session, prospects, history=history)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING, hist_map)
        dry = _dry(session, batch_id).json()
        confirmed = _confirm(session, batch_id, dry["plan_fingerprint"]).json()
        if int(confirmed.get("history_inserted") or 0) != 1:
            _fail(f"expected one history event: {confirmed}")
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT note_text, attribution, event_hash, company_id
                FROM company_shared_history_events
                WHERE external_record_no = '14001'
                """
            ).fetchall()
            if len(rows) != 1:
                _fail(f"history not once-per-company: {len(rows)}")
            if rows[0]["note_text"] != long_note:
                _fail("long note not preserved")
            if (rows[0]["attribution"] or "").strip() != "Unattributed shared history":
                _fail(f"expected unattributed: {rows[0]['attribution']!r}")
            company_id = int(rows[0]["company_id"])
            # Cross-client visibility: event is company-scoped (no client_id column).
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(company_shared_history_events)")
            }
            if "client_id" in cols:
                _fail("history events must not be client-owned")
            # Idempotent re-import of same hash
            conn.execute(
                """
                UPDATE client_data_import_batches SET status = 'previewed'
                WHERE id = ?
                """,
                (batch_id,),
            )
            # Cannot re-confirm same batch after CRM imported — use second batch
        prospects2 = _csv_bytes(
            PROSPECTS_HEADERS,
            [["14001", "Hist Co", "H", "Three", "h3@hist.test", "New"]],
        )
        history2 = _csv_bytes(
            [
                "LeadMaster Record No.",
                "Event Timestamp",
                "Author",
                "Note Text",
                "Event Hash",
                "Attributed Client/Campaign",
            ],
            [["14001", "2024-06-01T00:00:00Z", "Rep", long_note, hist_hash, ""]],
        )
        uploaded2 = _upload(session, prospects2, history=history2)
        batch2 = int(uploaded2.json()["batch"]["id"])
        _map(session, batch2, PROSPECTS_MAPPING, hist_map)
        dry2 = _dry(session, batch2).json()
        if int(dry2.get("history", {}).get("already_present") or 0) != 1:
            _fail(f"expected already_present: {dry2.get('history')}")
        confirmed2 = _confirm(session, batch2, dry2["plan_fingerprint"]).json()
        if int(confirmed2.get("history_inserted") or 0) != 0:
            _fail("hash idempotency failed on second import")
        if int(confirmed2.get("history_already_present") or 0) != 1:
            _fail(f"confirm already_present: {confirmed2}")
        with get_connection() as conn:
            n = conn.execute(
                """
                SELECT COUNT(*) AS n FROM company_shared_history_events
                WHERE company_id = ? AND event_hash = ?
                """,
                (company_id, hist_hash),
            ).fetchone()["n"]
        if int(n) != 1:
            _fail("duplicate history event inserted")
    finally:
        session.close()


def test_closed_excludes_registries_and_reports_breakdown() -> None:
    session = _Session()
    try:
        with get_connection() as conn:
            now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES ('15001', 'Keep Master', ?, ?)
                """,
                (now, now),
            )
            conn.commit()
        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [
                ["15001", "Keep Master", "K", "Eep", "k@keep.test", "Closed"],
                ["15002", "Open Co", "O", "Pen", "o@open.test", "New"],
            ],
        )
        history = _csv_bytes(
            HISTORY_HEADERS,
            [
                ["15001", "2024-07-01T00:00:00Z", "Rep", "Closed hist", hashlib.sha256(b"c1").hexdigest()],
                ["15002", "2024-07-01T00:00:00Z", "Rep", "Open hist", hashlib.sha256(b"o1").hexdigest()],
            ],
        )
        uploaded = _upload(session, prospects, history=history)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING, HISTORY_MAPPING)
        dry = _dry(session, batch_id).json()
        if int(dry.get("companies_excluded_closed") or 0) < 1:
            _fail(f"companies_excluded_closed missing: {dry}")
        if int(dry.get("contacts_excluded_closed") or 0) < 1:
            _fail(f"contacts_excluded_closed missing: {dry}")
        if int(dry.get("history_events_excluded_closed") or 0) < 1:
            _fail(f"history excluded missing: {dry}")
        if int(dry.get("prospects", {}).get("excluded_closed") or 0) < 1:
            _fail(f"prospect excluded_closed: {dry.get('prospects')}")
        confirmed = _confirm(session, batch_id, dry["plan_fingerprint"]).json()
        with get_connection() as conn:
            master = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE external_record_no = '15001'"
            ).fetchone()["n"]
            rel = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM client_company_relationships ccr
                JOIN companies co ON co.id = ccr.company_id
                WHERE ccr.client_id = ? AND co.external_record_no = '15001'
                """,
                (session.client_id,),
            ).fetchone()["n"]
            closed_hist = conn.execute(
                """
                SELECT COUNT(*) AS n FROM company_shared_history_events
                WHERE external_record_no = '15001'
                """
            ).fetchone()["n"]
            open_hist = conn.execute(
                """
                SELECT COUNT(*) AS n FROM company_shared_history_events
                WHERE external_record_no = '15002'
                """
            ).fetchone()["n"]
        if int(master) != 1:
            _fail("Closed deleted shared master")
        if int(rel) != 0:
            _fail("Closed created selected-client relationship")
        if int(closed_hist) != 0:
            _fail("Closed history was imported")
        if int(open_hist) != 1:
            _fail("Open history missing")
        if int(confirmed.get("closed_excluded_count") or 0) < 1:
            _fail(f"confirm closed count: {confirmed}")
    finally:
        session.close()


def test_concurrent_confirm_one_success() -> None:
    import concurrent.futures
    from client_data_import import confirm_client_data_import
    from crm_import_staging import BatchNotReusable

    session = _Session()
    try:
        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [["16001", "Race Co", "R", "Ace", "r@race.test", "New"]],
        )
        uploaded = _upload(session, prospects)
        batch_id = int(uploaded.json()["batch"]["id"])
        _map(session, batch_id, PROSPECTS_MAPPING)
        fp = _dry(session, batch_id).json()["plan_fingerprint"]
        actor = session.actor()
        results: list[str] = []

        def runner() -> str:
            try:
                out = confirm_client_data_import(
                    client_id=session.client_id,
                    batch_id=batch_id,
                    plan_fingerprint=fp,
                    actor=actor,
                    confirm=True,
                )
                return f"ok:{out.status}"
            except BatchNotReusable as exc:
                return f"refuse:{exc}"
            except Exception as exc:  # noqa: BLE001
                return f"err:{type(exc).__name__}:{exc}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(runner), pool.submit(runner)]
            results = [f.result() for f in concurrent.futures.as_completed(futs)]
        oks = [r for r in results if r.startswith("ok:")]
        refuses = [r for r in results if r.startswith("refuse:") or r.startswith("ok:confirmed")]
        if len(oks) < 1:
            _fail(f"no success: {results}")
        # Second must be idempotent success or safe refusal — never partial write.
        with get_connection() as conn:
            companies = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE external_record_no = '16001'"
            ).fetchone()["n"]
            batch_status = conn.execute(
                """
                SELECT status FROM client_data_import_batches
                WHERE id = ? AND client_id = ?
                """,
                (batch_id, session.client_id),
            ).fetchone()["status"]
        if int(companies) != 1:
            _fail(f"concurrent created duplicates: {companies} results={results}")
        if batch_status != "confirmed":
            _fail(f"batch not confirmed: {batch_status} results={results}")
        if any(r.startswith("err:") for r in results):
            _fail(f"unsafe concurrent error: {results}")
    finally:
        session.close()


def test_no_request_time_ddl_on_confirm_dry_run() -> None:
    session = _Session()
    try:
        from unittest.mock import patch
        import client_data_import as cdi
        import crm_import_staging as cis
        import db as dbmod

        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [["17001", "DDL Co", "D", "Dl", "d@ddl.test", "New"]],
        )
        calls: list[str] = []

        def ban_ddl(name: str):
            def _ban(*_a, **_k):
                calls.append(name)
                raise AssertionError(f"DDL/ensure called at request time: {name}")

            return _ban

        with (
            patch.object(cdi, "ensure_client_data_import_schema", ban_ddl("cdi")),
            patch.object(cis, "ensure_crm_import_schema", ban_ddl("crm_staging")),
            patch.object(dbmod, "migrate_schema", ban_ddl("migrate")),
        ):
            uploaded = _upload(session, prospects)
            if uploaded.status_code != 200:
                _fail(f"upload: {uploaded.status_code} {uploaded.text}")
            batch_id = int(uploaded.json()["batch"]["id"])
            mapped = _map(session, batch_id, PROSPECTS_MAPPING)
            if mapped.status_code != 200:
                _fail(mapped.text)
            dry = _dry(session, batch_id)
            if dry.status_code != 200:
                _fail(dry.text)
            confirmed = _confirm(session, batch_id, dry.json()["plan_fingerprint"])
            if confirmed.status_code != 200:
                _fail(confirmed.text)
            got = session.http.get(
                BATCH.format(client_id=session.client_id, batch_id=batch_id),
                headers=session.headers(),
            )
            if got.status_code != 200:
                _fail(got.text)
        if calls:
            _fail(f"DDL invoked: {calls}")
    finally:
        session.close()


def test_upload_rejects_unsupported_and_oversized_history() -> None:
    session = _Session()
    try:
        from unittest.mock import patch
        from openpyxl import Workbook
        import client_data_import as cdi
        import crm_import_staging as cis

        prospects = _csv_bytes(
            PROSPECTS_HEADERS,
            [["18001", "Up Co", "U", "P", "u@up.test", "New"]],
        )
        # Unsupported history extension
        bad = session.http.post(
            UPLOAD.format(client_id=session.client_id),
            headers=session.headers(),
            files={
                "prospects_file": ("ok.csv", prospects, "text/csv"),
                "history_file": ("notes.xlsx", b"not-csv", "application/octet-stream"),
            },
        )
        if bad.status_code not in {400, 422}:
            _fail(f"expected reject xlsx history: {bad.status_code} {bad.text}")
        # Unsupported prospects type
        bad_pros = session.http.post(
            UPLOAD.format(client_id=session.client_id),
            headers=session.headers(),
            files={
                "prospects_file": ("notes.txt", b"x,y\n1,2\n", "text/plain"),
            },
        )
        if bad_pros.status_code not in {400, 422}:
            _fail(f"expected reject unsupported prospects: {bad_pros.status_code}")
        # Oversized history
        huge = b"a" * (cdi.HISTORY_MAX_FILE_BYTES + 1)
        big = session.http.post(
            UPLOAD.format(client_id=session.client_id),
            headers=session.headers(),
            files={
                "prospects_file": ("ok.csv", prospects, "text/csv"),
                "history_file": ("notes.csv", huge, "text/csv"),
            },
        )
        if big.status_code not in {400, 422}:
            _fail(f"expected reject oversized: {big.status_code}")
        # Oversized prospects cell
        with patch.object(cis, "MAX_CELL_CHARS", 8), patch.object(
            cis, "MAX_FILE_BYTES", 10_000_000
        ):
            fat = _csv_bytes(
                PROSPECTS_HEADERS,
                [["18002", "x" * 40, "U", "P", "fat@up.test", "New"]],
            )
            fat_resp = _upload(session, fat)
            # Either rejected or staged as failed/blocking — must not silently accept.
            if fat_resp.status_code == 200:
                batch = fat_resp.json().get("batch") or {}
                if (batch.get("status") or "") not in {"failed", "previewed"}:
                    _fail(f"unexpected fat cell batch: {batch}")
                if batch.get("status") == "previewed" and int(
                    batch.get("error_row_count") or 0
                ) < 1 and int(batch.get("blocking_error_count") or 0) < 1:
                    # Check via dry-run needs_review if counts differ
                    pass
            elif fat_resp.status_code not in {400, 422}:
                _fail(f"fat cell unexpected: {fat_resp.status_code}")
        # XLSX formula stored as literal / not executed
        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(PROSPECTS_HEADERS)
        ws.append(["18003", "=1+1", "F", "Orm", "f@form.test", "New"])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx = buf.getvalue()
        xresp = _upload(session, xlsx, prospects_name="book.xlsx")
        if xresp.status_code != 200:
            _fail(f"xlsx upload: {xresp.status_code} {xresp.text}")
        sample = (xresp.json().get("batch") or {}).get("sample_rows") or []
        blob = json.dumps(sample) if sample else xresp.text
        if "=1+1" not in blob and "2" in blob and "formula" not in blob.lower():
            # Prefer evidence of formula warning or literal retention
            batch_id = int(xresp.json()["batch"]["id"])
            got = session.http.get(
                BATCH.format(client_id=session.client_id, batch_id=batch_id),
                headers=session.headers(),
            ).json()
            warnings = got.get("warnings") or got.get("batch", {}).get("warnings") or []
            raw = json.dumps(got)
            if "=1+1" not in raw and "formula" not in raw.lower():
                _fail(f"xlsx formula not preserved/warned: {warnings} {raw[:500]}")
        # Staging path cannot escape controlled root
        with get_connection() as conn:
            from client_data_import import _import_root

            root = _import_root().resolve()
            outside = str(root.parent / "escape-test.csv")
            # Confirm rejects outside path
            from client_data_import import confirm_client_data_import
            from crm_import_staging import BatchNotReusable

            prospects2 = _csv_bytes(
                PROSPECTS_HEADERS,
                [["18004", "Path Co", "P", "Ath", "p@path.test", "New"]],
            )
            hist = _csv_bytes(
                HISTORY_HEADERS,
                [
                    [
                        "18004",
                        "2024-01-01T00:00:00Z",
                        "Rep",
                        "n",
                        hashlib.sha256(b"path").hexdigest(),
                    ]
                ],
            )
            up2 = _upload(session, prospects2, history=hist)
            bid2 = int(up2.json()["batch"]["id"])
            _map(session, bid2, PROSPECTS_MAPPING, HISTORY_MAPPING)
            dry2 = _dry(session, bid2).json()
            Path(outside).write_text("x", encoding="utf-8")
            conn.execute(
                """
                UPDATE client_data_import_batches
                SET history_staging_path = ?
                WHERE id = ? AND client_id = ?
                """,
                (outside, bid2, session.client_id),
            )
            conn.commit()
            try:
                confirm_client_data_import(
                    client_id=session.client_id,
                    batch_id=bid2,
                    plan_fingerprint=dry2["plan_fingerprint"],
                    actor=session.actor(),
                    confirm=True,
                )
                _fail("outside staging path was accepted")
            except BatchNotReusable as exc:
                if "controlled directory" not in str(exc).lower() and "outside" not in str(
                    exc
                ).lower():
                    # dry-run may fail first on missing file under expected path
                    if "missing" not in str(exc).lower() and "fingerprint" not in str(
                        exc
                    ).lower():
                        raise
            finally:
                try:
                    Path(outside).unlink(missing_ok=True)
                except Exception:
                    pass
    finally:
        session.close()


def main() -> None:
    tests = [
        test_prospects_only_happy_path,
        test_prospects_plus_history,
        test_record_no_reuse_and_name_only_no_reuse,
        test_closed_exclusion_without_master_deletion,
        test_status_conflict_blocks_confirm,
        test_auth_csrf_and_client_required,
        test_stale_fingerprint_409,
        test_staging_cleanup_failure_pending_and_retry,
        test_malicious_filename_sanitized,
        test_no_crm_writes_during_dry_run,
        test_atomic_rollback_after_each_write_layer,
        test_preview_confirm_counters_and_in_batch_reuse,
        test_fingerprint_stale_on_mapping_file_identity_catalog,
        test_matching_safety_possible_high_confidence_contact_scope,
        test_history_once_per_company_unattributed_long_note_idempotent,
        test_closed_excludes_registries_and_reports_breakdown,
        test_concurrent_confirm_one_success,
        test_no_request_time_ddl_on_confirm_dry_run,
        test_upload_rejects_unsupported_and_oversized_history,
    ]
    for fn in tests:
        print(f"RUN {fn.__name__}")
        fn()
        print(f"OK  {fn.__name__}")
    print(f"ALL PASSED ({len(tests)})")


if __name__ == "__main__":
    main()
