"""Phase 0C — PostgreSQL readiness / migration foundation tests.

Run: python test_pg_readiness_phase0c.py

Uses isolated temp SQLite fixtures only — never opens or writes production
northstar.db. Does not connect to paid/cloud PostgreSQL.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Isolate before importing db.
_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-pg0c-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from db import PRODUCTION_DB_PATH, get_connection  # noqa: E402
from db_config import (  # noqa: E402
    DatabaseConfigError,
    DatabaseEngine,
    load_database_config,
    redact_mapping,
    redact_secret,
)
from db_sql import (  # noqa: E402
    begin_write,
    execute,
    last_insert_id,
    on_conflict_do_nothing,
    sql_q,
    transaction,
)
from pg_migration.copy_order import (  # noqa: E402
    MIGRATION_TABLE_ORDER,
    table_copy_order,
)
from pg_migration.migrate import (  # noqa: E402
    InMemoryPostgresStore,
    build_insert_sql,
    migrate_sqlite_fixture_to_plan,
    validate_migration_plan,
)
from pg_migration.schema_pg import render_postgresql_ddl, sequence_reset_sql  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(__file__).resolve().parent

# Production runtime modules that must not call sqlite3.connect directly.
_RUNTIME_NO_RAW_CONNECT = (
    "client_workspace_data.py",
    "work_queue_data.py",
    "crm_import_confirm.py",
    "crm_import_staging.py",
    "client_data_import.py",
    "shared_note_history_import.py",
    "deep_research_data.py",
    "activities_data.py",
    "appointments_data.py",
    "access.py",
    "auth_http.py",
    "auth_sessions.py",
)


def _fresh_fixture() -> Path:
    handle, name = tempfile.mkstemp(prefix="ns-pg0c-fix-", suffix=".db")
    os.close(handle)
    os.unlink(name)
    conn = sqlite3.connect(name)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_record_no TEXT NOT NULL UNIQUE,
            company_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            external_record_no TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            notes_long TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );
        CREATE TABLE client_company_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            company_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (client_id) REFERENCES clients(id),
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );
        CREATE TABLE company_shared_history_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            external_record_no TEXT NOT NULL DEFAULT '',
            note_text TEXT NOT NULL DEFAULT '',
            event_hash TEXT NOT NULL,
            event_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );
        CREATE TABLE field_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_code TEXT NOT NULL DEFAULT '',
            external_record_no TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            changed_by TEXT NOT NULL DEFAULT '',
            changed_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.execute(
        "INSERT INTO clients (id, code, name) VALUES (7, 'acme', 'Acme')"
    )
    conn.execute(
        "INSERT INTO companies (id, external_record_no, company_name) "
        "VALUES (100, 'RN-100', 'Shared Co')"
    )
    conn.execute(
        "INSERT INTO companies (id, external_record_no, company_name) "
        "VALUES (101, 'RN-101', 'Other Co')"
    )
    long_note = "历史-note-" + ("文" * 5000)
    conn.execute(
        """
        INSERT INTO contacts (
            id, company_id, external_record_no, first_name, last_name,
            email, notes_long, meta_json, created_at
        ) VALUES (50, 100, 'RN-100', 'Ada', 'Lovelace', 'ada@example.com',
                  ?, ?, '2024-01-02 03:04:05')
        """,
        (long_note, '{"ok": true, "n": 2}'),
    )
    # Empty / null-adjacent values preserved as empty strings (SQLite TEXT).
    conn.execute(
        """
        INSERT INTO contacts (
            id, company_id, external_record_no, first_name, last_name,
            email, notes_long, meta_json, created_at
        ) VALUES (51, 101, '', '', '', '', '', '', '')
        """
    )
    conn.execute(
        """
        INSERT INTO client_company_relationships
            (id, client_id, company_id, status, notes)
        VALUES (1, 7, 100, 'Hot Prospect', 'client-specific')
        """
    )
    conn.execute(
        """
        INSERT INTO company_shared_history_events (
            id, company_id, external_record_no, note_text, event_hash,
            event_at, created_at
        ) VALUES (9, 100, 'RN-100', ?, 'evt-hash-abc123',
                  '2023-06-01 12:00:00', '2023-06-02 08:00:00')
        """,
        (long_note,),
    )
    conn.execute(
        """
        INSERT INTO field_audit_log (
            id, client_code, external_record_no, field_name,
            old_value, new_value, changed_by, changed_at
        ) VALUES (3, 'acme', 'RN-100', 'status', '', 'Hot Prospect',
                  'tester', '2024-05-01 10:11:12')
        """
    )
    conn.commit()
    conn.close()
    return Path(name)


class TestDatabaseConfig(unittest.TestCase):
    def test_default_sqlite(self) -> None:
        cfg = load_database_config(environ={})
        self.assertEqual(cfg.engine, DatabaseEngine.SQLITE)
        self.assertTrue(str(cfg.sqlite_path).endswith("northstar.db"))
        summary = cfg.safe_summary()
        self.assertEqual(summary["engine"], "sqlite")
        self.assertIsNone(summary["database_url"])
        # Live path selection: absent DATABASE_URL → SQLite northstar.db.
        self.assertEqual(
            Path(cfg.require_sqlite_path()).resolve(),
            PRODUCTION_DB_PATH.resolve(),
        )

    def test_postgresql_requires_url(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            load_database_config(environ={"NORTHSTAR_DB_ENGINE": "postgresql"})

    def test_postgresql_from_url(self) -> None:
        cfg = load_database_config(
            environ={
                "DATABASE_URL": "postgresql://user:s3cret@localhost:5432/northstar"
            }
        )
        self.assertTrue(cfg.is_postgresql)
        safe = cfg.safe_summary()["database_url"] or ""
        self.assertNotIn("s3cret", safe)
        self.assertIn("***", safe)
        with self.assertRaises(DatabaseConfigError) as ctx:
            raise DatabaseConfigError(
                f"bad dsn postgresql://user:s3cret@localhost/db cfg={cfg.safe_summary()}"
            )
        self.assertNotIn("s3cret", str(ctx.exception))

    def test_conflicting_engine_and_url(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            load_database_config(
                environ={
                    "NORTHSTAR_DB_ENGINE": "sqlite",
                    "DATABASE_URL": "postgresql://u:p@localhost/db",
                }
            )

    def test_unsupported_engine(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            load_database_config(environ={"NORTHSTAR_DB_ENGINE": "mysql"})

    def test_redact_mapping(self) -> None:
        out = redact_mapping(
            {
                "password": "hunter2",
                "DATABASE_URL": "postgresql://a:b@h/db",
                "ok": 1,
            }
        )
        self.assertNotEqual(out["password"], "hunter2")
        self.assertNotIn("b@", str(out["DATABASE_URL"]))
        self.assertEqual(out["ok"], 1)
        self.assertEqual(redact_secret(None), "")


class TestPortableSql(unittest.TestCase):
    def test_placeholder_translation(self) -> None:
        sql = "SELECT * FROM t WHERE id = ? AND name = ?"
        self.assertEqual(sql_q(sql, DatabaseEngine.SQLITE), sql)
        self.assertEqual(
            sql_q(sql, DatabaseEngine.POSTGRESQL),
            "SELECT * FROM t WHERE id = %s AND name = %s",
        )

    def test_insert_sql_pg(self) -> None:
        sql = build_insert_sql("clients", ["id", "code"], on_conflict_pk="id")
        self.assertIn("%s", sql)
        self.assertIn("ON CONFLICT (id) DO NOTHING", sql)

    def test_on_conflict_helper(self) -> None:
        clause = on_conflict_do_nothing("(user_id, client_id)")
        self.assertIn("DO NOTHING", clause)

    def test_transaction_rollback(self) -> None:
        handle, name = tempfile.mkstemp(prefix="ns-pg0c-tx-", suffix=".db")
        os.close(handle)
        try:
            conn = sqlite3.connect(name)
            conn.execute(
                "CREATE TABLE pg0c_tx (id INTEGER PRIMARY KEY, v TEXT)"
            )
            conn.commit()
            with self.assertRaises(RuntimeError):
                with transaction(conn, engine=DatabaseEngine.SQLITE):
                    conn.execute("INSERT INTO pg0c_tx (id, v) VALUES (1, 'a')")
                    raise RuntimeError("boom")
            row = conn.execute("SELECT COUNT(*) AS n FROM pg0c_tx").fetchone()
            self.assertEqual(int(row[0]), 0)
            conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(name + suffix).unlink(missing_ok=True)

    def test_last_insert_id_sqlite(self) -> None:
        conn = get_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pg0c_ids (id INTEGER PRIMARY KEY, v TEXT)"
            )
            conn.commit()
            begin_write(conn)
            cur = execute(
                conn,
                "INSERT INTO pg0c_ids (v) VALUES (?)",
                ("x",),
                engine=DatabaseEngine.SQLITE,
            )
            new_id = last_insert_id(conn, cur)
            conn.commit()
            self.assertGreater(new_id, 0)
        finally:
            conn.close()


class TestPgSchemaGeneration(unittest.TestCase):
    def test_generated_ddl_omits_fts_and_pragma(self) -> None:
        ddl = render_postgresql_ddl()
        self.assertIn("GENERATED BY DEFAULT AS IDENTITY", ddl)
        self.assertNotIn("PRAGMA", ddl)
        self.assertNotIn("CREATE VIRTUAL TABLE", ddl.upper())
        self.assertNotIn("USING fts5", ddl.lower())
        self.assertNotIn("CREATE TRIGGER", ddl.upper())
        self.assertNotIn("northstar_phone_nanp10", ddl)
        self.assertIn("SET client_encoding", ddl)

    def test_sequence_reset_sql(self) -> None:
        sql = sequence_reset_sql("companies", "id")
        self.assertIn("setval", sql)
        self.assertIn("companies", sql)


class TestMigrationFoundation(unittest.TestCase):
    def test_dependency_order_parents_first(self) -> None:
        order = table_copy_order()
        self.assertLess(order.index("clients"), order.index("user_client_assignments"))
        self.assertLess(order.index("companies"), order.index("contacts"))
        self.assertLess(
            order.index("companies"),
            order.index("client_company_relationships"),
        )
        self.assertIn("company_locations", MIGRATION_TABLE_ORDER)
        self.assertIn("company_source_identities", MIGRATION_TABLE_ORDER)
        self.assertIn("company_merge_history", MIGRATION_TABLE_ORDER)
        self.assertIn("contact_merge_history", MIGRATION_TABLE_ORDER)
        self.assertIn("merge_execution_approvals", MIGRATION_TABLE_ORDER)
        self.assertLess(
            order.index("companies"),
            order.index("company_locations"),
        )
        self.assertLess(
            order.index("company_locations"),
            order.index("contacts"),
        )
        self.assertLess(
            order.index("companies"),
            order.index("company_merge_history"),
        )
        self.assertLess(
            order.index("contacts"),
            order.index("contact_merge_history"),
        )
        self.assertLess(
            order.index("company_locations"),
            order.index("company_source_identities"),
        )

    def test_refuses_production_path(self) -> None:
        with self.assertRaises(RuntimeError):
            migrate_sqlite_fixture_to_plan(
                PRODUCTION_DB_PATH,
                production_guard_path=PRODUCTION_DB_PATH,
            )

    def test_copy_preserves_pks_json_unicode_long_notes(self) -> None:
        fixture = _fresh_fixture()
        try:
            store = InMemoryPostgresStore()
            report = migrate_sqlite_fixture_to_plan(
                fixture,
                store=store,
                production_guard_path=PRODUCTION_DB_PATH,
            )
            self.assertTrue(validate_migration_plan(report), report.to_json())
            self.assertEqual(store.tables["companies"][100]["company_name"], "Shared Co")
            self.assertEqual(store.tables["companies"][100]["external_record_no"], "RN-100")
            contact = store.tables["contacts"][50]
            self.assertIn("文", contact["notes_long"])
            self.assertGreater(len(contact["notes_long"]), 5000)
            self.assertEqual(contact["meta_json"], '{"ok": true, "n": 2}')
            self.assertEqual(contact["created_at"], "2024-01-02 03:04:05")
            empty = store.tables["contacts"][51]
            self.assertEqual(empty["email"], "")
            self.assertEqual(empty["first_name"], "")
            self.assertEqual(empty["created_at"], "")
            rel = store.tables["client_company_relationships"][1]
            self.assertEqual(rel["status"], "Hot Prospect")
            hist = store.tables["company_shared_history_events"][9]
            self.assertEqual(hist["event_hash"], "evt-hash-abc123")
            self.assertEqual(hist["event_at"], "2023-06-01 12:00:00")
            self.assertIn("文", hist["note_text"])
            audit = store.tables["field_audit_log"][3]
            self.assertEqual(audit["old_value"], "")
            self.assertEqual(audit["new_value"], "Hot Prospect")
            self.assertEqual(audit["changed_at"], "2024-05-01 10:11:12")
            self.assertTrue(any("setval" in s for s in report.sequence_sql))
            # Explicit-ID tables must produce sequence reconciliation SQL.
            seq_tables = " ".join(report.sequence_sql)
            self.assertIn("companies", seq_tables)
            self.assertIn("contacts", seq_tables)
            self.assertIn("company_shared_history_events", seq_tables)
        finally:
            fixture.unlink(missing_ok=True)

    def test_exclusions_document_fts_and_sqlite_internal(self) -> None:
        from pg_migration.copy_order import EXCLUDED_FROM_ROW_COPY, MIGRATION_TABLE_ORDER

        self.assertIn("search_fts", EXCLUDED_FROM_ROW_COPY)
        self.assertIn("sqlite_sequence", EXCLUDED_FROM_ROW_COPY)
        order = table_copy_order({"search_fts", "companies", "sqlite_sequence"})
        self.assertEqual(order, ["companies"])
        self.assertIn("field_audit_log", MIGRATION_TABLE_ORDER)
        self.assertIn("company_shared_history_events", MIGRATION_TABLE_ORDER)

    def test_restart_idempotency_no_duplicate_rows(self) -> None:
        fixture = _fresh_fixture()
        try:
            store = InMemoryPostgresStore()
            r1 = migrate_sqlite_fixture_to_plan(
                fixture, store=store, production_guard_path=PRODUCTION_DB_PATH
            )
            self.assertTrue(r1.success)
            r2 = migrate_sqlite_fixture_to_plan(
                fixture,
                store=store,
                resume=True,
                production_guard_path=PRODUCTION_DB_PATH,
            )
            self.assertTrue(r2.success)
            self.assertEqual(store.count("companies"), 2)
            self.assertEqual(store.count("contacts"), 2)
            self.assertEqual(store.count("company_shared_history_events"), 1)
            self.assertEqual(store.count("field_audit_log"), 1)
            # Second pass should skip (resume marks done) or upsert-skip
            total_skipped = sum(t.skipped_existing for t in r2.tables)
            self.assertGreaterEqual(total_skipped, 1)
        finally:
            fixture.unlink(missing_ok=True)

    def test_fk_and_count_reconciliation(self) -> None:
        fixture = _fresh_fixture()
        try:
            report = migrate_sqlite_fixture_to_plan(
                fixture, production_guard_path=PRODUCTION_DB_PATH
            )
            self.assertEqual(report.count_mismatches, [])
            self.assertEqual(report.fk_violations, [])
            by_table = {t.table: t for t in report.tables}
            self.assertEqual(by_table["clients"].source_count, 1)
            self.assertEqual(by_table["companies"].copied_count, 2)
        finally:
            fixture.unlink(missing_ok=True)

    def test_production_sqlite_untouched_guard(self) -> None:
        """Opening the production path for migration must fail; counts stay stable."""
        before = {}
        # Read-only open of production for count snapshot only.
        uri = f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            for t in (
                "clients",
                "companies",
                "contacts",
                "client_company_relationships",
            ):
                before[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        finally:
            conn.close()

        fixture = _fresh_fixture()
        try:
            migrate_sqlite_fixture_to_plan(
                fixture, production_guard_path=PRODUCTION_DB_PATH
            )
        finally:
            fixture.unlink(missing_ok=True)

        conn = sqlite3.connect(uri, uri=True)
        try:
            for t, n in before.items():
                got = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                self.assertEqual(got, n, t)
        finally:
            conn.close()


class TestNoRawSqliteRuntimeConnect(unittest.TestCase):
    def test_runtime_modules_avoid_sqlite3_connect(self) -> None:
        offenders: list[str] = []
        for name in _RUNTIME_NO_RAW_CONNECT:
            text = (BACKEND / name).read_text(encoding="utf-8")
            if "sqlite3.connect(" in text:
                offenders.append(name)
        self.assertEqual(offenders, [], f"raw sqlite3.connect in {offenders}")

    def test_get_connection_refuses_postgresql_engine(self) -> None:
        previous = {
            k: os.environ.get(k)
            for k in (
                "NORTHSTAR_DB_ENGINE",
                "DATABASE_URL",
                "NORTHSTAR_DATABASE_URL",
            )
        }
        try:
            os.environ["NORTHSTAR_DB_ENGINE"] = "postgresql"
            os.environ["DATABASE_URL"] = "postgresql://u:p@localhost/db"
            with self.assertRaises(DatabaseConfigError):
                get_connection()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class TestHighRiskInvariantsDocumented(unittest.TestCase):
    """Contract checks that migration order preserves shared-company model."""

    def test_shared_company_one_master_row(self) -> None:
        fixture = _fresh_fixture()
        try:
            store = InMemoryPostgresStore()
            migrate_sqlite_fixture_to_plan(
                fixture, store=store, production_guard_path=PRODUCTION_DB_PATH
            )
            # One company id 100; relationship carries client-specific status.
            self.assertEqual(len(store.tables["companies"]), 2)
            self.assertEqual(
                store.tables["client_company_relationships"][1]["company_id"], 100
            )
            self.assertEqual(
                store.tables["client_company_relationships"][1]["status"],
                "Hot Prospect",
            )
        finally:
            fixture.unlink(missing_ok=True)


def main() -> None:
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
