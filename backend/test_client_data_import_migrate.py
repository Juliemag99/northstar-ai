"""Isolated proofs that migrate_schema activates Client Data Import schema.

Run from backend/:
  python test_client_data_import_migrate.py
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import testdb
from db import (
    DB_PATH,
    PRODUCTION_DB_PATH,
    SCHEMA_PATH,
    get_connection,
    migrate_schema,
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


def _schema_sql_cdi_columns() -> list[str]:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS client_data_import_batches\s*\((.*?)\);",
        text,
        flags=re.S,
    )
    if not match:
        _fail("client_data_import_batches not found in schema.sql")
    body = match.group(1)
    cols: list[str] = []
    for raw in body.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.upper().startswith("FOREIGN KEY") or line.upper().startswith(
            "PRIMARY KEY"
        ):
            continue
        name = line.split()[0].strip("`\"")
        if name.upper() in {"CONSTRAINT", "UNIQUE", "CHECK"}:
            continue
        cols.append(name)
    return cols


def _schema_sql_cdi_indexes() -> set[str]:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    return set(
        re.findall(
            r"CREATE INDEX IF NOT EXISTS (idx_client_data_import_batches_\w+)",
            text,
        )
    )


def _drop_cdi(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_client_data_import_batches_client")
    conn.execute("DROP INDEX IF EXISTS idx_client_data_import_batches_crm")
    conn.execute("DROP TABLE IF EXISTS client_data_import_batches")
    conn.commit()


def _cdi_present(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='client_data_import_batches'
            """
        ).fetchone()
        is not None
    )


def _cdi_columns(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute("PRAGMA table_info(client_data_import_batches)")]


def _cdi_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index'
          AND tbl_name='client_data_import_batches'
          AND name LIKE 'idx_client_data_import_batches_%'
        """
    ).fetchall()
    return {r["name"] if hasattr(r, "keys") else r[0] for r in rows}


def _freeze_masters(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "clients": int(conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"]),
        "companies": int(
            conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
        ),
        "contacts": int(conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]),
        "ccr": int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM client_company_relationships"
            ).fetchone()["n"]
        ),
    }


def test_migrate_schema_creates_cdi_on_older_db() -> None:
    """Older DB without CDI gains complete table/indexes via migrate_schema only."""
    _prove_isolated()
    # Fresh file copy of the isolated testdb, then strip CDI so migrate must recreate it.
    src = Path(os.fspath(DB_PATH)).resolve()
    handle, name = tempfile.mkstemp(prefix="ns-cdi-old-", suffix=".db")
    os.close(handle)
    os.unlink(name)
    shutil.copy2(src, name)
    try:
        conn = sqlite3.connect(name)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _drop_cdi(conn)
        if _cdi_present(conn):
            _fail("DROP failed to remove client_data_import_batches")
        before = _freeze_masters(conn)
        # Seed one CDI-independent row fingerprint via companies count stability.
        migrate_schema(conn)
        if not _cdi_present(conn):
            _fail("migrate_schema did not create client_data_import_batches")
        cols = _cdi_columns(conn)
        expected_cols = _schema_sql_cdi_columns()
        if cols != expected_cols:
            _fail(f"column mismatch live={cols} schema.sql={expected_cols}")
        idx = _cdi_indexes(conn)
        expected_idx = _schema_sql_cdi_indexes()
        if idx != expected_idx:
            _fail(f"index mismatch live={idx} schema.sql={expected_idx}")
        after = _freeze_masters(conn)
        if after != before:
            _fail(f"CRM masters changed: before={before} after={after}")
        # Existing CDI rows (none) + idempotent second pass.
        n1 = int(
            conn.execute("SELECT COUNT(*) AS n FROM client_data_import_batches").fetchone()[
                "n"
            ]
        )
        migrate_schema(conn)
        n2 = int(
            conn.execute("SELECT COUNT(*) AS n FROM client_data_import_batches").fetchone()[
                "n"
            ]
        )
        if n1 != n2 or n1 != 0:
            _fail(f"idempotent row drift n1={n1} n2={n2}")
        if _cdi_columns(conn) != cols or _cdi_indexes(conn) != idx:
            _fail("second migrate_schema altered CDI schema")
        if _freeze_masters(conn) != before:
            _fail("CRM masters changed on second migrate_schema")
        conn.close()
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def test_migrate_schema_agrees_with_schema_sql_on_isolated() -> None:
    _prove_isolated()
    with get_connection() as conn:
        migrate_schema(conn)
        cols = _cdi_columns(conn)
        expected_cols = _schema_sql_cdi_columns()
        if cols != expected_cols:
            _fail(f"isolated columns {cols} != schema.sql {expected_cols}")
        idx = _cdi_indexes(conn)
        expected_idx = _schema_sql_cdi_indexes()
        if idx != expected_idx:
            _fail(f"isolated indexes {idx} != schema.sql {expected_idx}")


def test_upload_dry_run_confirm_still_no_request_time_ddl() -> None:
    """Routes must not call migrate_schema / ensure_* DDL at request time."""
    _prove_isolated()
    import client_data_import as cdi
    import crm_import_staging as cis
    import db as dbmod
    from auth_passwords import hash_password
    from fastapi.testclient import TestClient
    from main import app
    from models import NorthStarUser
    import csv
    import io
    import secrets
    from datetime import datetime
    from auth_http import CSRF_HEADER

    calls: list[str] = []

    def ban(name: str):
        def _ban(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"request-time DDL: {name}")

        return _ban

    password = f"NsTest9{secrets.token_hex(8)}"
    email = f"cdi.migrate.{secrets.token_hex(4)}@example.test"
    with get_connection() as conn:
        cdi.ensure_client_data_import_schema(conn)
        cis.ensure_crm_import_schema(conn)
        migrate_schema(conn)
        digest = hash_password(password, email=email)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, 'CDI Migrate Test', 1, 1, 1, ?, 0, '')
            """,
            (email, digest),
        )
        uid = int(conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"])
        client_id = int(conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"])
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, 'staff', 1, datetime('now'))
            """,
            (uid, client_id),
        )
        # Seed New status for dry-run catalog.
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, created_at, last_updated_at)
            VALUES ('cdi-mig-seed', '__cdi_mig_seed__', ?, ?)
            """,
            (now, now),
        )
        cid = int(
            conn.execute(
                "SELECT id FROM companies WHERE external_record_no='cdi-mig-seed'"
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status,
                priority, next_action, notes, is_hot, created_at, updated_at
            ) VALUES (?, ?, 'cdi-mig-seed', 'New', '', '', '', 0, ?, ?)
            """,
            (client_id, cid, now, now),
        )
        conn.commit()

    http = TestClient(app)
    try:
        login = http.post("/api/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            _fail(f"login {login.status_code} {login.text}")
        csrf = str(login.json().get("csrf_token") or "")
        headers = {CSRF_HEADER: csrf}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "LeadMaster Record No.",
                "Company",
                "First Name",
                "Last Name",
                "Email",
                "Status",
            ]
        )
        w.writerow(
            ["991001001", "Migrate Smoke Co", "M", "Ig", "m@migrate.test", "New"]
        )
        content = buf.getvalue().encode("utf-8")

        with (
            patch.object(cdi, "ensure_client_data_import_schema", ban("cdi")),
            patch.object(cis, "ensure_crm_import_schema", ban("crm")),
            patch.object(dbmod, "migrate_schema", ban("migrate")),
        ):
            up = http.post(
                f"/api/clients/{client_id}/admin/client-data-imports",
                headers=headers,
                files={"prospects_file": ("mig.csv", content, "text/csv")},
            )
            if up.status_code != 200:
                _fail(f"upload {up.status_code} {up.text}")
            batch_id = int(up.json()["batch"]["id"])
            mapped = http.put(
                f"/api/clients/{client_id}/admin/client-data-imports/{batch_id}/mapping",
                headers=headers,
                json={
                    "prospects_mapping": {
                        "external_record_no": "LeadMaster Record No.",
                        "company_name": "Company",
                        "contact_first_name": "First Name",
                        "contact_last_name": "Last Name",
                        "contact_email": "Email",
                        "relationship_status": "Status",
                    }
                },
            )
            if mapped.status_code != 200:
                _fail(mapped.text)
            dry = http.post(
                f"/api/clients/{client_id}/admin/client-data-imports/{batch_id}/dry-run",
                headers=headers,
                json={},
            )
            if dry.status_code != 200:
                _fail(dry.text)
            fp = dry.json()["plan_fingerprint"]
            # Confirm is allowed in this synthetic path; still no DDL.
            confirmed = http.post(
                f"/api/clients/{client_id}/admin/client-data-imports/{batch_id}/confirm",
                headers=headers,
                json={"confirm": True, "plan_fingerprint": fp},
            )
            if confirmed.status_code != 200:
                _fail(confirmed.text)
        if calls:
            _fail(f"DDL invoked during request path: {calls}")
    finally:
        with get_connection() as conn:
            conn.execute("DELETE FROM staff_sessions WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM user_client_assignments WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            conn.commit()


def main() -> None:
    tests = [
        test_migrate_schema_creates_cdi_on_older_db,
        test_migrate_schema_agrees_with_schema_sql_on_isolated,
        test_upload_dry_run_confirm_still_no_request_time_ddl,
    ]
    for fn in tests:
        print(f"RUN {fn.__name__}")
        fn()
        print(f"OK  {fn.__name__}")
    print(f"ALL PASSED ({len(tests)})")


if __name__ == "__main__":
    main()
