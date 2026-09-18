"""Phase 5B company location / source-identity schema tests.

Isolated testdb only. Does not write production northstar.db.
Does not backfill live locations or source identities.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import unittest
from pathlib import Path

import testdb

from company_locations import (
    LOCATION_TYPES,
    create_company_location,
    delete_company_location,
    ensure_company_location_schema,
    normalize_location_flags,
    upsert_company_source_identity,
)
from db import DB_PATH, PRODUCTION_DB_PATH, SCHEMA_PATH, get_connection, migrate_schema
from pg_migration.copy_order import MIGRATION_TABLE_ORDER
from pg_migration.schema_pg import render_postgresql_ddl


def _insert_company(conn, name: str = "Loc Co") -> int:
    rn = f"P5B-{secrets.token_hex(4)}"
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, created_at, last_updated_at
        ) VALUES (?, ?, datetime('now'), datetime('now'))
        """,
        (rn, name),
    )
    return int(cur.lastrowid)


class Phase5BLocationSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def test_schema_exists_and_migration_idempotent(self) -> None:
        with get_connection() as conn:
            first = ensure_company_location_schema(conn)
            second = ensure_company_location_schema(conn)
            migrate_schema(conn)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            loc_cols = {r[1] for r in conn.execute("PRAGMA table_info(company_locations)")}
            ident_cols = {r[1] for r in conn.execute("PRAGMA table_info(company_source_identities)")}
            contact_cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
        self.assertIn("company_locations", tables)
        self.assertIn("company_source_identities", tables)
        self.assertIn("location_type", loc_cols)
        self.assertNotIn("external_record_no", loc_cols)
        self.assertIn("source_record_no", ident_cols)
        self.assertIn("location_id", ident_cols)
        self.assertIn("location_id", contact_cols)
        self.assertEqual(first["created_locations_table"], 0)
        self.assertEqual(second["created_locations_table"], 0)
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS company_locations", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS company_source_identities", schema)
        self.assertIn("idx_company_source_identities_idempotent", schema)
        ddl = render_postgresql_ddl()
        self.assertIn("company_locations", ddl)
        self.assertIn("company_source_identities", ddl)
        self.assertIn("COALESCE(client_id, 0)", ddl)
        self.assertLess(MIGRATION_TABLE_ORDER.index("companies"), MIGRATION_TABLE_ORDER.index("company_locations"))
        self.assertLess(MIGRATION_TABLE_ORDER.index("company_locations"), MIGRATION_TABLE_ORDER.index("contacts"))
        for kind in LOCATION_TYPES:
            self.assertIn(f"'{kind}'", schema)

    def test_valid_location_and_org_and_location_identities(self) -> None:
        with get_connection() as conn:
            cid = _insert_company(conn, "Org Loc")
            lid = create_company_location(
                conn,
                company_id=cid,
                location_name="York Plant",
                location_type="plant",
                city="York",
                state="PA",
            )
            org_id, org_status = upsert_company_source_identity(
                conn,
                company_id=cid,
                source_system="leadmaster",
                source_record_no="102072",
            )
            loc_id, loc_status = upsert_company_source_identity(
                conn,
                company_id=cid,
                location_id=lid,
                source_system="leadmaster",
                source_record_no="1326674",
                source_company_name="Johnson Controls-York",
            )
            conn.commit()
            loc = conn.execute("SELECT * FROM company_locations WHERE id=?", (lid,)).fetchone()
            ident = conn.execute(
                "SELECT location_id FROM company_source_identities WHERE id=?", (loc_id,)
            ).fetchone()
        self.assertEqual(org_status, "created")
        self.assertEqual(loc_status, "created")
        self.assertEqual(loc["location_type"], "plant")
        self.assertEqual(int(loc["is_headquarters"]), 0)
        self.assertEqual(int(ident["location_id"]), lid)
        self.assertIsNotNone(org_id)

    def test_duplicate_identity_and_null_client_scope(self) -> None:
        with get_connection() as conn:
            cid = _insert_company(conn)
            first, status1 = upsert_company_source_identity(
                conn, company_id=cid, source_system="crm_import", source_record_no="RN-DUP"
            )
            second, status2 = upsert_company_source_identity(
                conn, company_id=cid, source_system="crm_import", source_record_no="RN-DUP"
            )
            third, status3 = upsert_company_source_identity(
                conn,
                company_id=cid,
                source_system="crm_import",
                source_record_no="RN-DUP",
                client_id=None,
            )
            conn.commit()
            n = conn.execute(
                """
                SELECT COUNT(*) FROM company_source_identities
                WHERE source_system='crm_import' AND source_record_no='RN-DUP'
                """
            ).fetchone()[0]
        self.assertEqual(status1, "created")
        self.assertEqual(status2, "existing")
        self.assertEqual(status3, "existing")
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(n, 1)

    def test_same_rn_allowed_across_source_systems(self) -> None:
        with get_connection() as conn:
            cid = _insert_company(conn)
            a, sa = upsert_company_source_identity(
                conn, company_id=cid, source_system="leadmaster", source_record_no="SHARED-RN"
            )
            b, sb = upsert_company_source_identity(
                conn, company_id=cid, source_system="crm_import", source_record_no="SHARED-RN"
            )
            conn.commit()
        self.assertEqual(sa, "created")
        self.assertEqual(sb, "created")
        self.assertNotEqual(a, b)

    def test_conflicting_company_or_location_does_not_overwrite(self) -> None:
        with get_connection() as conn:
            a = _insert_company(conn, "A")
            b = _insert_company(conn, "B")
            loc_a = create_company_location(conn, company_id=a, location_name="A Plant", location_type="plant")
            loc_b = create_company_location(conn, company_id=a, location_name="A Office", location_type="office")
            ident, created = upsert_company_source_identity(
                conn,
                company_id=a,
                location_id=loc_a,
                source_system="leadmaster",
                source_record_no="CONFLICT-RN",
            )
            other_co, status_co = upsert_company_source_identity(
                conn,
                company_id=b,
                source_system="leadmaster",
                source_record_no="CONFLICT-RN",
            )
            other_loc, status_loc = upsert_company_source_identity(
                conn,
                company_id=a,
                location_id=loc_b,
                source_system="leadmaster",
                source_record_no="CONFLICT-RN",
            )
            live = conn.execute(
                "SELECT company_id, location_id FROM company_source_identities WHERE id=?",
                (ident,),
            ).fetchone()
            conn.commit()
        self.assertEqual(created, "created")
        self.assertEqual(status_co, "conflict")
        self.assertEqual(status_loc, "conflict")
        self.assertEqual(other_co, ident)
        self.assertEqual(other_loc, ident)
        self.assertEqual(int(live["company_id"]), a)
        self.assertEqual(int(live["location_id"]), loc_a)

    def test_fk_and_safe_location_delete(self) -> None:
        with get_connection() as conn:
            cid = _insert_company(conn)
            lid = create_company_location(conn, company_id=cid, location_name="Site", location_type="office")
            ident, _ = upsert_company_source_identity(
                conn,
                company_id=cid,
                location_id=lid,
                source_system="cdi",
                source_record_no="KEEP-ME",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO company_locations (company_id, location_name) VALUES (999999999, 'orphan')"
                )
            delete_company_location(conn, lid)
            conn.commit()
            gone = conn.execute("SELECT 1 FROM company_locations WHERE id=?", (lid,)).fetchone()
            kept = conn.execute(
                "SELECT location_id FROM company_source_identities WHERE id=?", (ident,)
            ).fetchone()
        self.assertIsNone(gone)
        self.assertIsNone(kept["location_id"])

    def test_headquarters_flags_and_invalid_type(self) -> None:
        kind, hq = normalize_location_flags("headquarters", 0)
        self.assertEqual(kind, "headquarters")
        self.assertEqual(hq, 1)
        kind, hq = normalize_location_flags("plant", 1)
        self.assertEqual(kind, "headquarters")
        self.assertEqual(hq, 1)
        with self.assertRaises(ValueError):
            normalize_location_flags("campus", 0)
        with get_connection() as conn:
            cid = _insert_company(conn)
            create_company_location(conn, company_id=cid, location_type="headquarters", is_headquarters=1)
            with self.assertRaises(sqlite3.IntegrityError):
                create_company_location(conn, company_id=cid, location_type="headquarters", is_headquarters=1)
            conn.rollback()

    def test_site_dedupe_reuses_location_and_allows_two_identities(self) -> None:
        from company_locations import ensure_company_location, find_company_location_by_site

        with get_connection() as conn:
            cid = _insert_company(conn, "Dedupe Co")
            first, st1 = ensure_company_location(
                conn,
                company_id=cid,
                location_name="Birmingham",
                location_type="unknown",
                address="1730 Vanderbilt Rd",
                city="Birmingham",
                state="AL",
                zip_code="35234",
            )
            second, st2 = ensure_company_location(
                conn,
                company_id=cid,
                location_name="Birmingham Plant",
                location_type="plant",
                address="1730 Vanderbilt Rd",
                city="Birmingham",
                state="AL",
                zip_code="35234",
            )
            found = find_company_location_by_site(
                conn,
                company_id=cid,
                address="1730 Vanderbilt Rd",
                city="Birmingham",
                state="AL",
                zip_code="35234",
            )
            a, sa = upsert_company_source_identity(
                conn,
                company_id=cid,
                location_id=first,
                source_system="crm_import",
                source_record_no="RN-A",
            )
            b, sb = upsert_company_source_identity(
                conn,
                company_id=cid,
                location_id=first,
                source_system="shared_history",
                source_record_no="RN-B",
            )
            n = conn.execute(
                "SELECT COUNT(*) FROM company_locations WHERE company_id=?", (cid,)
            ).fetchone()[0]
            conn.commit()
        self.assertEqual(st1, "created")
        self.assertEqual(st2, "existing")
        self.assertEqual(first, second)
        self.assertEqual(found, first)
        self.assertEqual(sa, "created")
        self.assertEqual(sb, "created")
        self.assertNotEqual(a, b)
        self.assertEqual(n, 1)
        live = None
        with get_connection() as conn:
            live = conn.execute(
                "SELECT location_name, is_headquarters, is_primary FROM company_locations WHERE id=?",
                (first,),
            ).fetchone()
        self.assertEqual(live["location_name"], "Birmingham")
        self.assertEqual(int(live["is_headquarters"]), 0)
        self.assertEqual(int(live["is_primary"]), 0)


if __name__ == "__main__":
    unittest.main()
