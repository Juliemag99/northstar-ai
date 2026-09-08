"""Phase 0A identity-key integrity: sync, fail-closed, reconcile, write paths.

Run from backend/:
  python test_crm_identity_keys_integrity.py

Isolated testdb only — never opens production northstar.db.
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

import testdb
from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from crm_identity_keys import (
    IDENTITY_KEYS_NOT_READY,
    IdentityKeysNotReady,
    ensure_crm_identity_key_schema,
    normalize_record_no,
    reconcile_identity_key_counts,
    require_company_identity_ready,
)
from crm_import_plan import plan_crm_import_batch
from crm_import_staging import ensure_crm_import_schema
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from fastapi.testclient import TestClient
from main import app


UPLOAD = "/api/clients/{client_id}/admin/imports"
MAPPING = "/api/clients/{client_id}/admin/imports/{batch_id}/mapping"
DRY_RUN = "/api/clients/{client_id}/admin/imports/{batch_id}/dry-run"
CONFIRM = "/api/clients/{client_id}/admin/imports/{batch_id}/confirm"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _prove_isolated() -> None:
    opened = Path(os.fspath(DB_PATH)).resolve()
    if opened == PRODUCTION_DB_PATH.resolve():
        _fail("opened production DB")
    env = Path(os.environ.get("NORTHSTAR_TEST_DB", "")).resolve()
    if env != opened:
        _fail("NORTHSTAR_TEST_DB mismatch")
    print(f"ISOLATED {opened}")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _client_id() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"])


def _ensure() -> None:
    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        ensure_crm_identity_key_schema(conn)
        migrate_schema(conn)
        conn.commit()


def _seed_status(client_id: int) -> None:
    now = _now()
    with get_connection() as conn:
        for label in ("New", "Active"):
            if conn.execute(
                """
                SELECT 1 FROM client_company_relationships
                WHERE client_id = ? AND status = ? LIMIT 1
                """,
                (client_id, label),
            ).fetchone():
                continue
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (f"NS-SEED-{label}-{secrets.token_hex(3)}", f"__seed_{label}", now, now),
            )
            cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            rn = conn.execute(
                "SELECT external_record_no FROM companies WHERE id=?", (cid,)
            ).fetchone()[0]
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


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _admin(client_id: int):
    http = TestClient(app)
    password = f"NsTest9{secrets.token_hex(8)}"
    email = f"id.integrity.{secrets.token_hex(4)}@example.test"
    with get_connection() as conn:
        digest = hash_password(password, email=email)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, 'Identity Integrity', 1, 1, 1, ?, 0, '')
            """,
            (email, digest),
        )
        uid = int(conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"])
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
    return http, csrf, uid


def _cleanup_user(uid: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()


def _mapped_batch(http, client_id, csrf, headers, rows, mapping) -> int:
    up = http.post(
        UPLOAD.format(client_id=client_id),
        headers={CSRF_HEADER: csrf},
        files={"file": ("i.csv", _csv_bytes(headers, rows), "application/octet-stream")},
    )
    if up.status_code != 200:
        _fail(f"upload {up.status_code} {up.text[:300]}")
    batch_id = int((up.json().get("batch") or {})["batch_id"])
    mapped = http.put(
        MAPPING.format(client_id=client_id, batch_id=batch_id),
        headers={CSRF_HEADER: csrf},
        json={"mapping": mapping},
    )
    if mapped.status_code != 200:
        _fail(f"mapping {mapped.status_code} {mapped.text[:300]}")
    return batch_id


# ---------------------------------------------------------------------------
# 1. Key synchronization
# ---------------------------------------------------------------------------


def test_key_sync_insert_update_delete_rollback_duplicates() -> None:
    _prove_isolated()
    _ensure()
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, legacy_phone,
                address, city, state, created_at, last_updated_at
            ) VALUES ('00123', 'Sync Co', 'https://sync-co.example', '2145550100',
                      '10 Main St', 'Dallas', 'TX', ?, ?)
            """,
            (now, now),
        )
        cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        key = conn.execute(
            "SELECT * FROM company_identity_keys WHERE company_id=?", (cid,)
        ).fetchone()
        if key is None:
            _fail("INSERT did not populate company_identity_keys")
        if key["record_no"] != "00123":
            _fail(f"leading zeros lost: {key['record_no']!r}")
        if key["domain"] != "sync-co.example":
            _fail(f"domain key wrong: {key['domain']}")
        if not key["norm_name"] or not key["phone_digits"] or not key["addr_norm"]:
            _fail(f"incomplete insert keys: {dict(key)}")

        # Duplicate identity values across companies remain supported
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, created_at, last_updated_at
            ) VALUES ('00124', 'Other Sync', 'https://sync-co.example', ?, ?)
            """,
            (now, now),
        )
        cid2 = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        twins = conn.execute(
            "SELECT COUNT(*) AS n FROM company_identity_keys WHERE domain=?",
            ("sync-co.example",),
        ).fetchone()["n"]
        if twins < 2:
            _fail("duplicate domain keys not both stored")

        # UPDATE refresh
        conn.execute(
            """
            UPDATE companies SET company_name=?, website=?, legacy_phone=?,
                address=?, city=?, state=?, external_record_no=?
            WHERE id=?
            """,
            (
                "Sync Co Renamed",
                "https://sync-renamed.example",
                "9725550199",
                "99 Oak Ave",
                "Austin",
                "TX",
                "00999",
                cid,
            ),
        )
        key2 = conn.execute(
            "SELECT * FROM company_identity_keys WHERE company_id=?", (cid,)
        ).fetchone()
        if key2["record_no"] != "00999" or key2["domain"] != "sync-renamed.example":
            _fail(f"UPDATE did not refresh keys: {dict(key2)}")
        if "renamed" not in key2["norm_name"]:
            _fail(f"norm_name not refreshed: {key2['norm_name']}")

        # Contact INSERT + name UPDATE
        conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, email,
                source_row_index
            ) VALUES (?, '00999', 'Pat', 'Lee', 'pat@sync.example', 0)
            """,
            (cid,),
        )
        contact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        pk = conn.execute(
            "SELECT * FROM contact_person_keys WHERE contact_id=?", (contact_id,)
        ).fetchone()
        if pk is None or pk["person_norm"] != "pat lee":
            _fail(f"contact insert key missing: {pk}")
        conn.execute(
            "UPDATE contacts SET first_name=?, last_name=? WHERE id=?",
            ("Patricia", "Leeson", contact_id),
        )
        pk2 = conn.execute(
            "SELECT person_norm FROM contact_person_keys WHERE contact_id=?",
            (contact_id,),
        ).fetchone()
        if pk2["person_norm"] != "patricia leeson":
            _fail(f"contact name update key stale: {pk2['person_norm']}")

        # DELETE cascades
        conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        if conn.execute(
            "SELECT 1 FROM contact_person_keys WHERE contact_id=?", (contact_id,)
        ).fetchone():
            _fail("contact delete left person key")
        conn.execute("DELETE FROM companies WHERE id=?", (cid,))
        if conn.execute(
            "SELECT 1 FROM company_identity_keys WHERE company_id=?", (cid,)
        ).fetchone():
            _fail("company delete left identity key")
        # twin still present
        if not conn.execute(
            "SELECT 1 FROM company_identity_keys WHERE company_id=?", (cid2,)
        ).fetchone():
            _fail("unrelated company key removed")
        conn.commit()

    # Rollback leaves no key-table changes
    with get_connection() as conn:
        before = reconcile_identity_key_counts(conn)
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, created_at, last_updated_at
            ) VALUES ('RB-1', 'Rollback Co', 'https://rb.example', ?, ?)
            """,
            (now, now),
        )
        rb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if not conn.execute(
            "SELECT 1 FROM company_identity_keys WHERE company_id=?", (rb_id,)
        ).fetchone():
            _fail("trigger did not fire inside txn")
        conn.execute("ROLLBACK")
        after = reconcile_identity_key_counts(conn)
        if after != before:
            _fail(f"rollback leaked keys: {before} -> {after}")
        if conn.execute(
            "SELECT 1 FROM companies WHERE external_record_no='RB-1'"
        ).fetchone():
            _fail("rollback left company")
    print("SYNC_OK")


def test_raw_connect_fails_without_udfs_canonical_registers() -> None:
    """Triggers call Python UDFs — raw sqlite3.connect must not be used for writes."""
    _prove_isolated()
    _ensure()
    path = Path(os.fspath(DB_PATH))
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        raw.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, created_at, last_updated_at
            ) VALUES ('RAW-1', 'Raw Fail', datetime('now'), datetime('now'))
            """
        )
        _fail("raw connect INSERT should fail: no such function")
    except sqlite3.OperationalError as exc:
        if "northstar_" not in str(exc).lower() and "no such function" not in str(exc).lower():
            _fail(f"unexpected raw error: {exc}")
    finally:
        raw.close()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, created_at, last_updated_at
            ) VALUES ('CANON-1', 'Canonical Ok', datetime('now'), datetime('now'))
            """
        )
        cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if not conn.execute(
            "SELECT 1 FROM company_identity_keys WHERE company_id=?", (cid,)
        ).fetchone():
            _fail("get_connection insert missing keys")
        conn.commit()
    print("UDF_REGISTER_OK")


# ---------------------------------------------------------------------------
# 3. Fail-closed
# ---------------------------------------------------------------------------


def test_fail_closed_missing_and_incomplete_identity_schema() -> None:
    _prove_isolated()
    _ensure()
    client_id = _client_id()
    _seed_status(client_id)
    http, csrf, uid = _admin(client_id)
    try:
        batch_id = _mapped_batch(
            http,
            client_id,
            csrf,
            ["Company", "Website", "Status"],
            [["Fail Closed Co", "https://fail-closed.example", "New"]],
            {
                "company_name": "Company",
                "website": "Website",
                "relationship_status": "Status",
            },
        )

        # Incomplete: delete one key row while company remains
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM companies ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.execute(
                "DELETE FROM company_identity_keys WHERE company_id=?",
                (int(row["id"]),),
            )
            conn.commit()
            try:
                require_company_identity_ready(conn)
                _fail("incomplete keys must raise")
            except IdentityKeysNotReady as exc:
                if IDENTITY_KEYS_NOT_READY not in str(exc):
                    _fail(f"message drift: {exc}")
            try:
                plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_id)
                _fail("plan must fail closed on incomplete keys")
            except IdentityKeysNotReady:
                pass

        dry = http.post(
            DRY_RUN.format(client_id=client_id, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={"offset": 0, "limit": 50},
        )
        if dry.status_code != 503:
            _fail(f"dry-run incomplete expected 503, got {dry.status_code} {dry.text[:200]}")
        if IDENTITY_KEYS_NOT_READY not in str(dry.json().get("detail") or ""):
            _fail(f"dry-run detail wrong: {dry.text[:200]}")

        # Restore then drop table
        with get_connection() as conn:
            ensure_crm_identity_key_schema(conn)
            conn.execute("DROP TABLE company_identity_keys")
            # Dropping table may leave triggers pointing at missing table — recreate clean fail
            conn.execute("DROP TRIGGER IF EXISTS trg_company_identity_keys_ai")
            conn.execute("DROP TRIGGER IF EXISTS trg_company_identity_keys_au")
            conn.commit()
            try:
                require_company_identity_ready(conn)
                _fail("missing table must raise")
            except IdentityKeysNotReady:
                pass

        dry2 = http.post(
            DRY_RUN.format(client_id=client_id, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={"offset": 0, "limit": 50},
        )
        if dry2.status_code != 503:
            _fail(f"dry-run missing table expected 503, got {dry2.status_code}")

        # Omitting name-elsewhere when contact_person_keys gone is OK
        with get_connection() as conn:
            ensure_crm_identity_key_schema(conn)
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contact_person_keys'"
            ).fetchone():
                conn.execute("DROP TABLE contact_person_keys")
                conn.execute("DROP TRIGGER IF EXISTS trg_contact_person_keys_ai")
                conn.execute("DROP TRIGGER IF EXISTS trg_contact_person_keys_au")
            conn.commit()
            # Company keys ready — plan must succeed (name-elsewhere optional)
            plan = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_id)
            if not plan.plan_fingerprint:
                _fail("plan without person keys should still fingerprint")

        # Restore full schema for later tests in same DB
        with get_connection() as conn:
            ensure_crm_identity_key_schema(conn)
            conn.commit()
        print("FAIL_CLOSED_OK")
    finally:
        _cleanup_user(uid)


# ---------------------------------------------------------------------------
# 4. Backfill reconciliation
# ---------------------------------------------------------------------------


def test_backfill_reconciliation_idempotent_no_display_rewrite() -> None:
    _prove_isolated()
    _ensure()
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, created_at, last_updated_at
            ) VALUES ('00100', 'Zero Pad Co', 'https://zeropad.example', ?, ?)
            """,
            (now, now),
        )
        cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, email,
                source_row_index
            ) VALUES (?, '00100', 'Zed', 'Pad', 'zed@zeropad.example', 0)
            """,
            (cid,),
        )
        before_disp = conn.execute(
            "SELECT company_name, website, external_record_no FROM companies WHERE id=?",
            (cid,),
        ).fetchone()
        a = ensure_crm_identity_key_schema(conn)
        mid_disp = conn.execute(
            "SELECT company_name, website, external_record_no FROM companies WHERE id=?",
            (cid,),
        ).fetchone()
        b = ensure_crm_identity_key_schema(conn)
        migrate_schema(conn)
        after_disp = conn.execute(
            "SELECT company_name, website, external_record_no FROM companies WHERE id=?",
            (cid,),
        ).fetchone()
        if dict(before_disp) != dict(mid_disp) or dict(before_disp) != dict(after_disp):
            _fail("display values rewritten by migrate/backfill")
        recon = reconcile_identity_key_counts(conn)
        if recon["companies"] != recon["company_keys"]:
            _fail(f"company key count mismatch: {recon}")
        if recon["contacts"] != recon["contact_keys"]:
            _fail(f"contact key count mismatch: {recon}")
        if recon["company_missing_keys"] or recon["company_orphan_keys"]:
            _fail(f"company orphans/missing: {recon}")
        if recon["contact_missing_keys"] or recon["contact_orphan_keys"]:
            _fail(f"contact orphans/missing: {recon}")
        rn = conn.execute(
            "SELECT record_no FROM company_identity_keys WHERE company_id=?", (cid,)
        ).fetchone()["record_no"]
        if rn != "00100":
            _fail(f"leading zeros not preserved: {rn!r}")
        if normalize_record_no("00100") != "00100":
            _fail("normalize strips meaningful leading zeros")
        if normalize_record_no("123.0") != "123":
            _fail("excel .0 not stripped")
        conn.commit()
        print(
            f"RECON_OK companies={recon['companies']} company_keys={recon['company_keys']} "
            f"contacts={recon['contacts']} contact_keys={recon['contact_keys']} "
            f"ensure_a={a} ensure_b={b}"
        )


# ---------------------------------------------------------------------------
# 5. Query safety + key updates change matching + preview/confirm equality
# ---------------------------------------------------------------------------


def test_key_update_changes_matching_and_preview_confirm_equal() -> None:
    _prove_isolated()
    _ensure()
    client_id = _client_id()
    _seed_status(client_id)
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, website, created_at, last_updated_at
            ) VALUES ('MATCH-UPD-1', 'Match Update Co', 'https://match-old.example', ?, ?)
            """,
            (now, now),
        )
        cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()

    http, csrf, uid = _admin(client_id)
    try:
        headers = ["Company", "Website", "Status"]
        mapping = {
            "company_name": "Company",
            "website": "Website",
            "relationship_status": "Status",
        }
        batch_old = _mapped_batch(
            http,
            client_id,
            csrf,
            headers,
            [["Match Update Co", "https://match-old.example", "New"]],
            mapping,
        )
        with get_connection() as conn:
            plan_old = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_old)
        if plan_old.rows[0].company_action != "use_existing_company":
            _fail(f"pre-update should reuse: {plan_old.rows[0].company_action}")
        if plan_old.rows[0].company_id != cid:
            _fail("matched wrong company")

        with get_connection() as conn:
            conn.execute(
                "UPDATE companies SET website=? WHERE id=?",
                ("https://match-new.example", cid),
            )
            key = conn.execute(
                "SELECT domain FROM company_identity_keys WHERE company_id=?", (cid,)
            ).fetchone()
            if key["domain"] != "match-new.example":
                _fail(f"domain key not updated: {key['domain']}")
            conn.commit()

        # Same staged domain (old) should no longer high-reuse via domain
        batch_stale = _mapped_batch(
            http,
            client_id,
            csrf,
            headers,
            [["Totally Different Name", "https://match-old.example", "New"]],
            mapping,
        )
        with get_connection() as conn:
            plan_stale = plan_crm_import_batch(
                conn, client_id=client_id, batch_id=batch_stale
            )
        if plan_stale.rows[0].company_action == "use_existing_company":
            if plan_stale.rows[0].company_id == cid:
                _fail("stale domain still matched after key update")

        batch_new = _mapped_batch(
            http,
            client_id,
            csrf,
            headers,
            [["Match Update Co", "https://match-new.example", "New"]],
            mapping,
        )
        with get_connection() as conn:
            plan_new = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_new)
        if plan_new.rows[0].company_action != "use_existing_company":
            _fail(f"new domain should reuse: {plan_new.rows[0].company_action}")
        if plan_new.rows[0].company_id != cid:
            _fail("new domain matched wrong id")

        # No unqualified scans during plan
        class Tracer:
            def __init__(self, inner):
                self._inner = inner
                self.sqls: list[str] = []

            def execute(self, sql, parameters=()):
                text = " ".join(str(sql).split())
                if not text.upper().startswith("EXPLAIN") and not text.upper().startswith(
                    "PRAGMA"
                ):
                    self.sqls.append(text)
                return self._inner.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def _is_unqualified_companies(sql: str) -> bool:
            upper = " ".join(sql.upper().split())
            if "COMPANY_IDENTITY_KEYS" in upper:
                return False
            if not re.search(r"\bFROM\s+COMPANIES\b", upper):
                return False
            # Readiness COUNT / orphan probes are not matching hydrates.
            if "COUNT(*)" in upper:
                return False
            if "LEFT JOIN" in upper:
                return False
            if re.search(r"\bWHERE\b", upper) and "ID IN" in upper:
                return False
            return True

        def _is_unqualified_contacts(sql: str) -> bool:
            upper = " ".join(sql.upper().split())
            if "CONTACT_PERSON_KEYS" in upper or "CONTACT_PHONE_KEYS" in upper:
                return False
            if not re.search(r"\bFROM\s+CONTACTS\b", upper):
                return False
            if "COUNT(*)" in upper or "LEFT JOIN" in upper:
                return False
            if re.search(r"\bWHERE\b", upper) and (
                "COMPANY_ID IN" in upper or "ID IN" in upper
            ):
                return False
            return True

        with get_connection() as conn:
            tracer = Tracer(conn)
            plan_trace = plan_crm_import_batch(
                tracer, client_id=client_id, batch_id=batch_new
            )
        for sql in tracer.sqls:
            if _is_unqualified_companies(sql):
                _fail(f"unqualified companies scan: {sql}")
            if _is_unqualified_contacts(sql):
                _fail(f"unqualified contacts scan: {sql}")

        dry = http.post(
            DRY_RUN.format(client_id=client_id, batch_id=batch_new),
            headers={CSRF_HEADER: csrf},
            json={"offset": 0, "limit": 50},
        )
        if dry.status_code != 200:
            _fail(dry.text[:300])
        body = dry.json()
        if body.get("plan_fingerprint") != plan_new.plan_fingerprint:
            _fail("dry-run fingerprint != planner")
        dry_counts = dict(body.get("counts") or {})
        plan_counts = {
            k: v for k, v in plan_new.counts.items() if k in dry_counts
        }
        if dry_counts != plan_counts:
            _fail(f"dry-run counts != planner: {dry_counts} vs {plan_counts}")

        # Confirm counters / fingerprint path: confirm then compare reported counts
        # Use a fresh create-only batch to avoid colliding with existing CCR.
        create_batch = _mapped_batch(
            http,
            client_id,
            csrf,
            ["LeadMaster Record No.", "Company", "Website", "Status"],
            [
                [
                    f"INT-{secrets.token_hex(4)}",
                    f"Integrity Create {secrets.token_hex(3)}",
                    f"https://integrity-create-{secrets.token_hex(3)}.example",
                    "New",
                ]
            ],
            {
                "external_record_no": "LeadMaster Record No.",
                "company_name": "Company",
                "website": "Website",
                "relationship_status": "Status",
            },
        )
        dry_c = http.post(
            DRY_RUN.format(client_id=client_id, batch_id=create_batch),
            headers={CSRF_HEADER: csrf},
            json={"offset": 0, "limit": 50},
        )
        if dry_c.status_code != 200:
            _fail(dry_c.text[:300])
        fp = dry_c.json()["plan_fingerprint"]
        dry_counts = dict(dry_c.json()["counts"] or {})
        with get_connection() as conn:
            plan_c = plan_crm_import_batch(conn, client_id=client_id, batch_id=create_batch)
        if plan_c.plan_fingerprint != fp:
            _fail("preview planner fingerprint drifted from dry-run")
        plan_c_counts = {k: v for k, v in plan_c.counts.items() if k in dry_counts}
        if plan_c_counts != dry_counts:
            _fail(f"preview planner counters drifted: {plan_c_counts} vs {dry_counts}")
        conf = http.post(
            CONFIRM.format(client_id=client_id, batch_id=create_batch),
            headers={CSRF_HEADER: csrf},
            json={"confirm": True, "plan_fingerprint": fp},
        )
        if conf.status_code != 200:
            _fail(f"confirm {conf.status_code} {conf.text[:300]}")
        # Confirmed fingerprint must match the preview fingerprint used
        with get_connection() as conn:
            row = conn.execute(
                "SELECT confirmed_plan_fingerprint FROM crm_import_batches WHERE id=?",
                (create_batch,),
            ).fetchone()
        if row and "confirmed_plan_fingerprint" in row.keys():
            if row["confirmed_plan_fingerprint"] and row["confirmed_plan_fingerprint"] != fp:
                _fail("confirmed fingerprint != preview")
        print(f"MATCH_UPDATE_OK trace_fp={plan_trace.plan_fingerprint[:12]}")
    finally:
        _cleanup_user(uid)


def test_cli_modules_use_get_connection() -> None:
    """Static proof: production writers do not call sqlite3.connect for DB writes."""
    root = Path(__file__).resolve().parent
    for name in ("reload_carmeco_source.py", "import_brown_industries.py"):
        text = (root / name).read_text(encoding="utf-8")
        if "sqlite3.connect(" in text:
            _fail(f"{name} still uses sqlite3.connect")
        if "get_connection" not in text:
            _fail(f"{name} missing get_connection")
    # db.get_connection registers identity UDFs
    src = (root / "db.py").read_text(encoding="utf-8")
    if "register_crm_identity_functions" not in src:
        _fail("get_connection must register CRM identity UDFs")
    print("CLI_CONNECT_OK")


def main() -> None:
    for fn in (
        test_cli_modules_use_get_connection,
        test_key_sync_insert_update_delete_rollback_duplicates,
        test_raw_connect_fails_without_udfs_canonical_registers,
        test_backfill_reconciliation_idempotent_no_display_rewrite,
        test_fail_closed_missing_and_incomplete_identity_schema,
        test_key_update_changes_matching_and_preview_confirm_equal,
    ):
        print(f"RUN {fn.__name__}")
        fn()
        print(f"OK  {fn.__name__}")
    print("ALL PASSED (6)")


if __name__ == "__main__":
    main()
