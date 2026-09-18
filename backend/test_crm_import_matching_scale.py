"""Phase 0A — indexed CRM import matching: scale + safety proofs.

Run from backend/:
  python test_crm_import_matching_scale.py

Uses isolated testdb only — never opens production northstar.db,
never talks to live ports, never migrates live Dawson data.
"""

from __future__ import annotations

import csv
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import testdb
from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from contact_phone import upsert_contact_phone_keys
from crm_add_data import allocate_ns_record_no
from crm_identity_keys import ensure_crm_identity_key_schema
from crm_import_plan import (
    IDENTITY_CLIENT_DATA,
    IN_CHUNK,
    PLANNER_VERSION,
    StagedPlanRow,
    plan_crm_import_batch,
    _load_matching_companies,
    _load_name_elsewhere,
)
from crm_import_staging import ensure_crm_import_schema
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from fastapi.testclient import TestClient
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from main import app
from shared_note_history_import import normalize_record_no


SCALE_FILL = 2500

UPLOAD = "/api/clients/{client_id}/admin/imports"
MAPPING = "/api/clients/{client_id}/admin/imports/{batch_id}/mapping"
DRY_RUN = "/api/clients/{client_id}/admin/imports/{batch_id}/dry-run"
CONFIRM = "/api/clients/{client_id}/admin/imports/{batch_id}/confirm"

FREEZE_TABLES = (
    "companies",
    "contacts",
    "client_company_relationships",
    "company_identity_keys",
    "contact_person_keys",
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _prove_isolated() -> None:
    opened = Path(os.fspath(DB_PATH)).resolve()
    if opened == PRODUCTION_DB_PATH.resolve():
        _fail("opened production DB")
    env = Path(os.environ.get("NORTHSTAR_TEST_DB", "")).resolve()
    if env != opened:
        _fail("NORTHSTAR_TEST_DB does not match opened DB")
    print(f"ISOLATED {opened}")


class TracingConnection:
    """Records executed SQL (skips EXPLAIN/PRAGMA)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql, parameters=()):
        text = " ".join(str(sql).split())
        upper = text.upper()
        if not upper.startswith("EXPLAIN") and not upper.startswith("PRAGMA"):
            self.statements.append(text)
        return self._conn.execute(sql, parameters)

    def executemany(self, sql, seq):
        self.statements.append(" ".join(str(sql).split()))
        return self._conn.executemany(sql, seq)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _unqualified_company_scan(sql: str) -> bool:
    upper = " ".join(sql.upper().split())
    if "COMPANY_IDENTITY_KEYS" in upper:
        return False
    if not re.search(r"\bFROM\s+COMPANIES\b", upper):
        return False
    # Readiness COUNT / orphan probes are not matching hydrates.
    if "COUNT(*)" in upper or "LEFT JOIN" in upper:
        return False
    # JOIN companies ... WHERE c.id IN (...) is the allowed hydrate path.
    if re.search(r"\bWHERE\b", upper) and re.search(r"\bID\s+IN\s*\(", upper):
        return False
    return True


def _unqualified_contact_scan(sql: str) -> bool:
    upper = " ".join(sql.upper().split())
    if "CONTACT_PERSON_KEYS" in upper or "CONTACT_PHONE_KEYS" in upper:
        return False
    if not re.search(r"\bFROM\s+CONTACTS\b", upper):
        return False
    if "COUNT(*)" in upper or "LEFT JOIN" in upper:
        return False
    if re.search(r"\bWHERE\b", upper) and (
        "COMPANY_ID IN" in upper or re.search(r"\bID\s+IN\s*\(", upper)
    ):
        return False
    return True


def _client_id() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
        return int(row["id"])


def _seed_status(client_id: int) -> None:
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with get_connection() as conn:
        for label in ("New", "Active"):
            exists = conn.execute(
                """
                SELECT 1 FROM client_company_relationships
                WHERE client_id = ? AND status = ? LIMIT 1
                """,
                (client_id, label),
            ).fetchone()
            if exists:
                continue
            rn = allocate_ns_record_no(conn)
            cur = conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (rn, f"__match_seed_{label}", now, now),
            )
            cid = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', '', '', 0, ?, ?)
                """,
                (client_id, cid, rn, label, now, now),
            )
        conn.commit()


def _counts() -> dict[str, int]:
    with get_connection() as conn:
        out: dict[str, int] = {}
        for table in FREEZE_TABLES:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                out[table] = int(
                    conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                )
        return out


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _admin_session(client_id: int):
    http = TestClient(app)
    password = f"NsTest9{secrets.token_hex(8)}"
    email = f"match.scale.{secrets.token_hex(4)}@example.test"
    with get_connection() as conn:
        digest = hash_password(password, email=email)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, 'Match Scale', 1, 1, 1, ?, 0, '')
            """,
            (email, digest),
        )
        uid = int(
            conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, 'staff', 1, datetime('now'))
            """,
            (uid, client_id),
        )
        conn.commit()
    login = http.post("/api/auth/login", json={"email": email, "password": password})
    if login.status_code != 200:
        _fail(login.text)
    csrf = str(login.json().get("csrf_token") or "")
    return http, csrf, uid, email, password


def _cleanup_user(uid: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()


def _mapped_batch(
    http: TestClient,
    client_id: int,
    csrf: str,
    headers: list[str],
    rows: list[list[object]],
    mapping: dict[str, str],
) -> int:
    up = http.post(
        UPLOAD.format(client_id=client_id),
        headers={CSRF_HEADER: csrf},
        files={
            "file": (
                "scale.csv",
                _csv_bytes(headers, rows),
                "application/octet-stream",
            )
        },
    )
    if up.status_code != 200:
        _fail(f"upload {up.status_code} {up.text[:400]}")
    batch_id = int((up.json().get("batch") or {})["batch_id"])
    mapped = http.put(
        MAPPING.format(client_id=client_id, batch_id=batch_id),
        headers={CSRF_HEADER: csrf},
        json={"mapping": mapping},
    )
    if mapped.status_code != 200:
        _fail(f"mapping {mapped.status_code} {mapped.text[:400]}")
    return batch_id


def _staged(
    *,
    rn: str = "",
    company: str,
    website: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    state: str = "",
    email: str = "",
    first: str = "A",
    last: str = "B",
    contact_phone: str = "",
    row_id: int = 1,
    source: int = 1,
) -> StagedPlanRow:
    mapped = {
        "external_record_no": rn,
        "company_name": company,
        "website": website,
        "phone": phone,
        "address": address,
        "city": city,
        "state": state,
        "contact_first_name": first,
        "contact_last_name": last,
        "contact_email": email,
        "contact_phone": contact_phone,
        "relationship_status": "New",
    }
    phone_d = digits_phone(phone)
    cphone = digits_phone(contact_phone)
    return StagedPlanRow(
        row_id=row_id,
        source_row_number=source,
        raw_sha256="",
        has_blocking_error=False,
        mapped=mapped,
        company_name=company,
        website=website,
        phone=phone,
        address=address,
        city=city,
        state=state,
        state_invalid=False,
        date_invalid=False,
        zip="",
        contact_first=first,
        contact_last=last,
        contact_full=f"{first} {last}".strip(),
        contact_title="",
        contact_email=email,
        contact_phone=contact_phone,
        relationship_status="New",
        relationship_notes="",
        norm_company=norm_name(company) if company else "",
        company_domain=domain(website),
        company_phone_digits=phone_d,
        company_addr=norm_addr(address) if address else "",
        email_norm=email.lower().strip(),
        nanp10=cphone if len(cphone) == 10 else "",
        last7=cphone[-7:] if len(cphone) >= 7 else "",
        person_norm=f"{first} {last}".strip().lower(),
        has_any_contact_field=bool(first or last or email or contact_phone),
        can_create_contact=True,
    )


def _ensure_schema() -> None:
    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        ensure_crm_identity_key_schema(conn)
        migrate_schema(conn)
        conn.commit()


# ---------------------------------------------------------------------------
# Scale proof
# ---------------------------------------------------------------------------


def test_scale_indexed_matching_no_full_scans() -> None:
    _prove_isolated()
    _ensure_schema()
    client_id = _client_id()
    _seed_status(client_id)
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")

    target_rn = "SCALE-RN-42"
    target_domain = "scale-target.example"

    with get_connection() as conn:
        # Noise companies/contacts — triggers maintain identity keys.
        for i in range(SCALE_FILL):
            rn = f"SCALE-FILL-{i:05d}"
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, website, legacy_phone,
                    address, city, state, created_at, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'TX', ?, ?)
                """,
                (
                    rn,
                    f"Fill Co {i}",
                    f"https://fill-{i}.example.test",
                    f"214555{i:04d}"[-10:],
                    f"{i} Main St",
                    "Dallas",
                    now,
                    now,
                ),
            )
            company_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, email,
                    phone, source_row_index
                ) VALUES (?, ?, 'Fill', ?, ?, '', 0)
                """,
                (company_id, rn, f"Person{i}", f"fill{i}@example.test"),
            )
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, legacy_phone,
                address, city, state, created_at, last_updated_at
            ) VALUES (?, 'Scale Target Co', ?, '2145550042',
                      '42 Target Rd', 'Dallas', 'TX', ?, ?)
            """,
            (target_rn, f"https://{target_domain}", now, now),
        )
        target_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, email,
                phone, source_row_index
            ) VALUES (?, ?, 'Pat', 'Target', 'pat@scale-target.example',
                      '2145550199', 0)
            """,
            (target_id, target_rn),
        )
        upsert_contact_phone_keys(conn, int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]), "2145550199", "")
        other_rn = allocate_ns_record_no(conn)
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, created_at, last_updated_at
            ) VALUES (?, 'Elsewhere Co', ?, ?)
            """,
            (other_rn, now, now),
        )
        other_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, email,
                source_row_index
            ) VALUES (?, ?, 'Pat', 'Target', 'other@elsewhere.test', 0)
            """,
            (other_id, other_rn),
        )
        conn.commit()

        key_n = int(
            conn.execute("SELECT COUNT(*) AS n FROM company_identity_keys").fetchone()["n"]
        )
        if key_n < SCALE_FILL:
            _fail(f"identity keys not maintained on insert: {key_n}")

    http, csrf, uid, _email, _pw = _admin_session(client_id)
    try:
        batch_id = _mapped_batch(
            http,
            client_id,
            csrf,
            [
                "LeadMaster Record No.",
                "Company",
                "Website",
                "Phone",
                "First Name",
                "Last Name",
                "Email",
                "Status",
            ],
            [
                [
                    target_rn,
                    "Scale Target Co Renamed",
                    f"https://{target_domain}",
                    "2145550042",
                    "Pat",
                    "Target",
                    "pat@scale-target.example",
                    "New",
                ],
                [
                    "SCALE-NEW-ZZ",
                    "Brand New Scale Co",
                    "https://brand-new-scale-zz.example",
                    "6467771234",
                    "New",
                    "Person",
                    "new@brand-new-scale-zz.example",
                    "New",
                ],
            ],
            {
                "external_record_no": "LeadMaster Record No.",
                "company_name": "Company",
                "website": "Website",
                "phone": "Phone",
                "contact_first_name": "First Name",
                "contact_last_name": "Last Name",
                "contact_email": "Email",
                "relationship_status": "Status",
            },
        )

        before = _counts()
        dry = http.post(
            DRY_RUN.format(client_id=client_id, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={"offset": 0, "limit": 100},
        )
        if dry.status_code != 200:
            _fail(f"dry-run {dry.status_code} {dry.text[:400]}")
        after = _counts()
        if after != before:
            _fail(f"dry-run wrote master tables: {before} -> {after}")

        with get_connection() as real:
            tracer = TracingConnection(real)
            plan = plan_crm_import_batch(tracer, client_id=client_id, batch_id=batch_id)
            sqls = list(tracer.statements)

        for sql in sqls:
            if _unqualified_company_scan(sql):
                _fail(f"unqualified companies scan: {sql}")
            if _unqualified_contact_scan(sql):
                _fail(f"unqualified contacts scan: {sql}")

        identity_queries = [
            s
            for s in sqls
            if "COMPANY_IDENTITY_KEYS" in s.upper()
            or "CONTACT_PERSON_KEYS" in s.upper()
        ]
        if len(identity_queries) < 1:
            _fail("expected identity-key probes")
        if len(identity_queries) > 40:
            _fail(f"too many identity queries: {len(identity_queries)}")

        # Chunk growth vs staged-row count
        staged_many = [
            _staged(
                rn=f"NOSUCH-{i}",
                company=f"Unique{i}",
                website=f"https://unique-{i}.example",
                row_id=i,
                source=i,
            )
            for i in range(50)
        ]
        with get_connection() as conn:
            tracer50 = TracingConnection(conn)
            _load_matching_companies(tracer50, staged_many)
            probes50 = [
                s
                for s in tracer50.statements
                if "COMPANY_IDENTITY_KEYS" in s.upper()
            ]
        if len(probes50) > 20:
            _fail(f"query count scales with rows not chunks: {len(probes50)}")

        with get_connection() as conn:
            plan_rows = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT company_id FROM company_identity_keys
                WHERE record_no != '' AND record_no IN (?)
                """,
                (normalize_record_no(target_rn),),
            ).fetchall()
            plan_text = " ".join(str(dict(r)) for r in plan_rows).upper()
            if "IDX_COMPANY_IDENTITY_RECORD_NO" not in plan_text:
                _fail(f"expected record_no index plan: {plan_text}")

            plan_rows2 = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT company_id, person_norm FROM contact_person_keys
                WHERE person_norm != '' AND person_norm IN (?)
                """,
                ("pat target",),
            ).fetchall()
            plan_text2 = " ".join(str(dict(r)) for r in plan_rows2).upper()
            if "IDX_CONTACT_PERSON_KEYS_NORM" not in plan_text2:
                _fail(f"expected person_norm index plan: {plan_text2}")

        retained = int((plan.stats or {}).get("retained_companies") or 0)
        if retained > 50:
            _fail(f"retained_companies too large: {retained}")
        if retained < 1:
            _fail("expected needle retained")

        reuse = next(
            (r for r in plan.rows if r.company_action == "use_existing_company"),
            None,
        )
        create = next(
            (r for r in plan.rows if r.company_action == "create_company"),
            None,
        )
        if reuse is None:
            _fail(f"RN reuse missing: {[r.company_action for r in plan.rows]}")
        if create is None:
            _fail(f"create missing: {[r.company_action for r in plan.rows]}")
        if plan.planner_version != PLANNER_VERSION:
            _fail("planner version drifted")

        with get_connection() as conn:
            plan2 = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_id)
        if plan2.plan_fingerprint != plan.plan_fingerprint:
            _fail("fingerprint not deterministic")
        dry_fp = dry.json().get("plan_fingerprint")
        if dry_fp != plan.plan_fingerprint:
            _fail("HTTP dry-run fingerprint != planner fingerprint")

        print(
            f"SCALE_OK fill={SCALE_FILL} identity_queries={len(identity_queries)} "
            f"probes_50_rows={len(probes50)} retained={retained} "
            f"chunk={IN_CHUNK} fp={plan.plan_fingerprint[:12]}"
        )
        print(f"EXPLAIN_RECORD_NO {plan_text}")
        print(f"EXPLAIN_PERSON {plan_text2}")
    finally:
        _cleanup_user(uid)


# ---------------------------------------------------------------------------
# Safety cases
# ---------------------------------------------------------------------------


def test_matching_policy_safety_cases() -> None:
    _prove_isolated()
    _ensure_schema()
    client_id = _client_id()
    _seed_status(client_id)
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, created_at, last_updated_at
            ) VALUES ('SAFE-RN-1', 'Original Name Co', 'https://safe-rn.example', ?, ?)
            """,
            (now, now),
        )
        rn_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for rn, name in (("SAFE-DOM-A", "Dom Twin A"), ("SAFE-DOM-B", "Dom Twin B")):
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, website, created_at, last_updated_at
                ) VALUES (?, ?, 'https://twin-safe.example', ?, ?)
                """,
                (rn, name, now, now),
            )
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, legacy_phone,
                created_at, last_updated_at
            ) VALUES ('SAFE-HC-1', 'Hi Conf Co', 'https://hiconf-safe.example',
                      '2145550888', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, created_at, last_updated_at
            ) VALUES ('SAFE-NAME-1', 'Name Only Safe', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, legacy_phone, created_at, last_updated_at
            ) VALUES ('SAFE-PH-1', 'Phone Only Co', '9725550111', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, address, city, state,
                created_at, last_updated_at
            ) VALUES ('SAFE-ADDR-1', 'Addr Only Co', '9 Oak Ave', 'Austin', 'TX', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, email,
                phone, source_row_index
            ) VALUES (?, 'SAFE-RN-1', 'Exact', 'Mail', 'exact@safe.test',
                      '2145550100', 0)
            """,
            (rn_id,),
        )
        contact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        upsert_contact_phone_keys(conn, contact_id, "2145550100", "")
        wrong_rn = allocate_ns_record_no(conn)
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, created_at, last_updated_at
            ) VALUES (?, 'Wrong Co', ?, ?)
            """,
            (wrong_rn, now, now),
        )
        wrong_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, email,
                phone, source_row_index
            ) VALUES (?, ?, 'Wrong', 'Place', 'exact@safe.test', '2145550100', 0)
            """,
            (wrong_id, wrong_rn),
        )
        conn.commit()

    # Loader: RN candidate despite name change
    kept = None
    with get_connection() as conn:
        kept = _load_matching_companies(
            conn,
            [
                _staged(
                    rn="SAFE-RN-1",
                    company="Renamed Entirely",
                    website="https://safe-rn.example",
                )
            ],
        )
        if not any(c.record_no == normalize_record_no("SAFE-RN-1") for c in kept):
            _fail(f"RN candidate missing: {kept}")

        kept_dom = _load_matching_companies(
            conn,
            [_staged(rn="", company="Anything", website="https://twin-safe.example")],
        )
        if len([c for c in kept_dom if c.domain == "twin-safe.example"]) < 2:
            _fail(f"domain twins not both loaded: {kept_dom}")

        elsewhere = _load_name_elsewhere(
            conn, {"exact mail", "no such person"}
        )
        if elsewhere.get("no such person"):
            _fail(f"empty name bucket polluted: {elsewhere}")
        if rn_id not in elsewhere.get("exact mail", set()):
            # may also include wrong company
            _fail(f"name elsewhere missed: {elsewhere}")

        tracer = TracingConnection(conn)
        _load_matching_companies(
            tracer,
            [
                _staged(rn="SAFE-RN-1", company="X", website="https://safe-rn.example"),
                _staged(
                    rn="",
                    company="Hi Conf Co",
                    website="https://hiconf-safe.example",
                    phone="2145550888",
                ),
            ],
        )
        _load_name_elsewhere(tracer, {"exact mail"})
        for sql in tracer.statements:
            if _unqualified_company_scan(sql) or _unqualified_contact_scan(sql):
                _fail(f"loader full scan: {sql}")

    http, csrf, uid, _e, _p = _admin_session(client_id)
    try:
        headers = [
            "LeadMaster Record No.",
            "Company",
            "Website",
            "Phone",
            "Address",
            "City",
            "State",
            "First Name",
            "Last Name",
            "Email",
            "Contact Phone",
            "Status",
        ]
        mapping = {
            "external_record_no": "LeadMaster Record No.",
            "company_name": "Company",
            "website": "Website",
            "phone": "Phone",
            "address": "Address",
            "city": "City",
            "state": "State",
            "contact_first_name": "First Name",
            "contact_last_name": "Last Name",
            "contact_email": "Email",
            "contact_phone": "Contact Phone",
            "relationship_status": "Status",
        }
        rows = [
            # exact RN, different name → reuse
            [
                "SAFE-RN-1",
                "Renamed Entirely",
                "https://safe-rn.example",
                "",
                "",
                "",
                "",
                "Exact",
                "Mail",
                "exact@safe.test",
                "2145550100",
                "New",
            ],
            # ambiguous domain twins → possible
            [
                "",
                "Dom Twin X",
                "https://twin-safe.example",
                "",
                "",
                "",
                "",
                "Dom",
                "Twin",
                "dom@twin-safe.example",
                "",
                "New",
            ],
            # name + phone supporting → high reuse
            [
                "",
                "Hi Conf Co",
                "https://hiconf-safe.example",
                "2145550888",
                "",
                "",
                "",
                "Hi",
                "Conf",
                "hi@hiconf-safe.example",
                "",
                "New",
            ],
            # phone-only → possible
            [
                "SAFE-PH-NEW",
                "Unrelated Phone Name",
                "",
                "9725550111",
                "",
                "",
                "",
                "P",
                "Only",
                "p@phone.example",
                "",
                "New",
            ],
            # address-only → possible
            [
                "SAFE-ADDR-NEW",
                "Unrelated Addr Name",
                "",
                "",
                "9 Oak Ave",
                "Austin",
                "TX",
                "A",
                "Only",
                "a@addr.example",
                "",
                "New",
            ],
            # within-batch proposed reuse of same new RN
            [
                "SAFE-BATCH-1",
                "Within Batch Co",
                "https://within-batch.example",
                "",
                "",
                "",
                "",
                "Batch",
                "One",
                "one@within-batch.example",
                "",
                "New",
            ],
            [
                "SAFE-BATCH-1",
                "Within Batch Co",
                "https://within-batch.example",
                "",
                "",
                "",
                "",
                "Batch",
                "Two",
                "two@within-batch.example",
                "",
                "New",
            ],
            # last7 / name-only contact possible on RN company row already covered
            # contact at wrong company: email exists elsewhere but RN company has match
            # create new company with email that exists at wrong company — should create contact
            # under new company (not steal wrong company's contact via global email)
            [
                "SAFE-SCOPE-1",
                "Scope New Co",
                "https://scope-new.example",
                "",
                "",
                "",
                "",
                "Wrong",
                "Place",
                "exact@safe.test",
                "2145550100",
                "New",
            ],
        ]
        batch_id = _mapped_batch(http, client_id, csrf, headers, rows, mapping)
        before = _counts()
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_id)
        after = _counts()
        if after != before:
            _fail(f"plan wrote masters: {before} -> {after}")

        actions = [(r.company_action, r.contact_action, r.company_name) for r in plan.rows]
        by_name = {r.company_name: r for r in plan.rows}

        rn_row = by_name.get("Renamed Entirely")
        if rn_row is None or rn_row.company_action != "use_existing_company":
            _fail(f"exact RN reuse failed: {rn_row}")
        if rn_row.company_id != rn_id:
            _fail(f"RN matched wrong company: {rn_row.company_id}")
        if rn_row.contact_action != "use_existing_contact":
            _fail(f"exact email/NANP10 contact reuse failed: {rn_row.contact_action}")

        twin = by_name.get("Dom Twin X")
        if twin is None or twin.company_action != "possible_company_match":
            _fail(f"ambiguous domain possible failed: {twin}")

        hi = by_name.get("Hi Conf Co")
        if hi is None or hi.company_action != "use_existing_company":
            _fail(f"name+domain/phone high match failed: {hi}")

        phone_only = by_name.get("Unrelated Phone Name")
        if phone_only is None or phone_only.company_action != "possible_company_match":
            _fail(f"phone-only possible failed: {phone_only}")

        addr_only = by_name.get("Unrelated Addr Name")
        if addr_only is None or addr_only.company_action != "possible_company_match":
            _fail(f"address-only possible failed: {addr_only}")

        batch_rows = [r for r in plan.rows if r.company_name == "Within Batch Co"]
        if len(batch_rows) != 2:
            _fail(f"within-batch rows missing: {batch_rows}")
        if batch_rows[0].company_action != "create_company":
            _fail(f"first within-batch should create: {batch_rows[0].company_action}")
        if batch_rows[1].company_action != "use_existing_company":
            _fail(f"second within-batch should reuse proposed: {batch_rows[1]}")
        if (
            batch_rows[0].company_proposed_key
            != batch_rows[1].company_proposed_key
        ):
            _fail(
                f"within-batch keys diverge: "
                f"{batch_rows[0].company_proposed_key} vs "
                f"{batch_rows[1].company_proposed_key}"
            )

        scope = by_name.get("Scope New Co")
        if scope is None or scope.company_action != "create_company":
            _fail(f"scoped create failed: {scope}")
        if scope.contact_action == "use_existing_contact":
            _fail("must not reuse contact from wrong company")

        # CRM and CDI: unique name-only must not auto-reuse a stored master
        name_batch = _mapped_batch(
            http,
            client_id,
            csrf,
            ["Company", "Status"],
            [["Name Only Safe", "New"]],
            {"company_name": "Company", "relationship_status": "Status"},
        )
        with get_connection() as conn:
            crm_name = plan_crm_import_batch(
                conn, client_id=client_id, batch_id=name_batch
            )
            cdi_name = plan_crm_import_batch(
                conn,
                client_id=client_id,
                batch_id=name_batch,
                identity_mode=IDENTITY_CLIENT_DATA,
            )
        crm_act = crm_name.rows[0].company_action
        cdi_act = cdi_name.rows[0].company_action
        if crm_act != "possible_company_match":
            _fail(f"CRM name-only must be possible not reuse: {crm_act}")
        if cdi_act != "possible_company_match":
            _fail(f"CDI name-only must be possible not reuse: {cdi_act}")

        # Stale fingerprint / dry-run freeze already covered; quick confirm rollback path
        # is owned by confirm suite — here assert dry-run + plan counters match HTTP.
        dry = http.post(
            DRY_RUN.format(client_id=client_id, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={"offset": 0, "limit": 100},
        )
        if dry.status_code != 200:
            _fail(dry.text[:300])
        body = dry.json()
        if body.get("plan_fingerprint") != plan.plan_fingerprint:
            # plan was computed before dry; recompute
            with get_connection() as conn:
                plan_now = plan_crm_import_batch(
                    conn, client_id=client_id, batch_id=batch_id
                )
            if body.get("plan_fingerprint") != plan_now.plan_fingerprint:
                _fail("dry-run fingerprint != planner")
            if body.get("counts") != plan_now.counts:
                _fail(
                    f"dry-run counts != planner: {body.get('counts')} vs {plan_now.counts}"
                )

        print(f"SAFETY_OK actions={actions[:4]}… planner={PLANNER_VERSION}")
    finally:
        _cleanup_user(uid)


def test_idempotent_backfill_no_display_rewrite() -> None:
    _prove_isolated()
    _ensure_schema()
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, created_at, last_updated_at
            ) VALUES ('SAFE-BF-1', 'Backfill Name', 'https://bf.example', ?, ?)
            """,
            (now, now),
        )
        cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        before_name = conn.execute(
            "SELECT company_name, website FROM companies WHERE id=?", (cid,)
        ).fetchone()
        from crm_identity_keys import (
            backfill_company_identity_keys,
            backfill_contact_person_keys,
        )

        a = backfill_company_identity_keys(conn)
        b = backfill_company_identity_keys(conn)
        after_name = conn.execute(
            "SELECT company_name, website FROM companies WHERE id=?", (cid,)
        ).fetchone()
        if dict(before_name) != dict(after_name):
            _fail("backfill rewrote display values")
        if a["display_rewritten"] != 0 or b["display_rewritten"] != 0:
            _fail("display_rewritten flag set")
        keys = conn.execute(
            "SELECT * FROM company_identity_keys WHERE company_id=?", (cid,)
        ).fetchone()
        if keys is None or keys["domain"] != "bf.example":
            _fail(f"key missing/wrong: {keys}")
        conn.commit()
    print("BACKFILL_OK")


def main() -> None:
    for fn in (
        test_idempotent_backfill_no_display_rewrite,
        test_matching_policy_safety_cases,
        test_scale_indexed_matching_no_full_scans,
    ):
        print(f"RUN {fn.__name__}")
        fn()
        print(f"OK  {fn.__name__}")
    print("ALL PASSED (3)")


if __name__ == "__main__":
    main()
