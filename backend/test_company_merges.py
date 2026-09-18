"""Phase 5E company redirect / merge-planner tests.

Isolated testdb only. Does not write production northstar.db.
Does not execute a live company or contact merge.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import unittest
from pathlib import Path

import testdb

from company_merges import (
    CONTACT_REDIRECT_REQUIRED,
    RETIREMENT_POLICY_CODE,
    MergeRedirectError,
    classify_contact_pair,
    discover_company_id_refs,
    ensure_company_merge_schema,
    plan_company_merge,
    record_company_merge_redirect,
    record_contact_merge_redirect,
    resolve_company_id,
    resolve_contact_id,
)
from db import DB_PATH, PRODUCTION_DB_PATH, SCHEMA_PATH, get_connection, migrate_schema
from pg_migration.copy_order import MIGRATION_TABLE_ORDER
from pg_migration.schema_pg import render_postgresql_ddl


def _insert_company(conn, name: str, rn: str | None = None) -> int:
    token = rn or f"P5E-{secrets.token_hex(4)}"
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, created_at, last_updated_at
        ) VALUES (?, ?, datetime('now'), datetime('now'))
        """,
        (token, name),
    )
    return int(cur.lastrowid)


def _insert_contact(conn, company_id: int, **fields: object) -> int:
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name, title,
            phone, alt_phone, phone_extension, alt_phone_extension, email,
            source_row_index, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
        """,
        (
            int(company_id),
            str(fields.get("external_record_no") or f"C-{secrets.token_hex(3)}"),
            str(fields.get("first_name") or ""),
            str(fields.get("last_name") or ""),
            str(fields.get("title") or ""),
            str(fields.get("phone") or ""),
            str(fields.get("alt_phone") or ""),
            fields.get("phone_extension"),
            fields.get("alt_phone_extension"),
            str(fields.get("email") or ""),
        ),
    )
    return int(cur.lastrowid)


def _client_id(conn) -> int:
    row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise AssertionError("Isolated testdb has no clients.")
    return int(row[0])


def _insert_ccr(conn, client_id: int, company_id: int, status: str, notes: str = "", rn: str = "") -> int:
    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, external_record_no, status, notes,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (client_id, company_id, rn, status, notes),
    )
    return int(cur.lastrowid)


class Phase5ECompanyMergeFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def test_schema_exists_and_pg_order(self) -> None:
        with get_connection() as conn:
            first = ensure_company_merge_schema(conn)
            second = ensure_company_merge_schema(conn)
            migrate_schema(conn)
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            company_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(company_merge_history)")
            }
            contact_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(contact_merge_history)")
            }
            company_fks = list(conn.execute("PRAGMA foreign_key_list(company_merge_history)"))
            contact_fks = list(conn.execute("PRAGMA foreign_key_list(contact_merge_history)"))
            company_before = int(
                conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]
            )
            contact_before = int(
                conn.execute("SELECT COUNT(*) FROM contact_merge_history").fetchone()[0]
            )
            third = ensure_company_merge_schema(conn)
            company_after = int(
                conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]
            )
            contact_after = int(
                conn.execute("SELECT COUNT(*) FROM contact_merge_history").fetchone()[0]
            )
        self.assertIn("company_merge_history", tables)
        self.assertIn("contact_merge_history", tables)
        self.assertIn("merge_execution_approvals", tables)
        self.assertIn("source_company_id", company_cols)
        self.assertIn("survivor_company_id", company_cols)
        self.assertIn("source_contact_id", contact_cols)
        self.assertEqual(first["created_company_merge_history"], 0)
        self.assertEqual(second["created_company_merge_history"], 0)
        self.assertEqual(third["created_company_merge_history"], 0)
        self.assertEqual(company_after, company_before)
        self.assertEqual(contact_after, contact_before)
        company_fk_from = {str(fk[3]) for fk in company_fks}
        self.assertIn("survivor_company_id", company_fk_from)
        self.assertNotIn("source_company_id", company_fk_from)
        contact_fk_from = {str(fk[3]) for fk in contact_fks}
        self.assertIn("survivor_contact_id", contact_fk_from)
        self.assertNotIn("source_contact_id", contact_fk_from)
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS company_merge_history", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS contact_merge_history", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS merge_execution_approvals", schema)
        ddl = render_postgresql_ddl()
        self.assertIn("company_merge_history", ddl)
        self.assertIn("contact_merge_history", ddl)
        self.assertIn("merge_execution_approvals", ddl)
        self.assertLess(
            MIGRATION_TABLE_ORDER.index("companies"),
            MIGRATION_TABLE_ORDER.index("company_merge_history"),
        )
        self.assertLess(
            MIGRATION_TABLE_ORDER.index("company_merge_history"),
            MIGRATION_TABLE_ORDER.index("merge_execution_approvals"),
        )
        self.assertLess(
            MIGRATION_TABLE_ORDER.index("contacts"),
            MIGRATION_TABLE_ORDER.index("contact_merge_history"),
        )
        self.assertTrue(CONTACT_REDIRECT_REQUIRED)
        self.assertEqual(RETIREMENT_POLICY_CODE, "D")

    def test_resolve_identity_chain_cycle_and_self(self) -> None:
        with get_connection() as conn:
            a = _insert_company(conn, "Chain A")
            b = _insert_company(conn, "Chain B")
            c = _insert_company(conn, "Chain C")
            conn.commit()
            self.assertEqual(resolve_company_id(conn, a), a)
            record_company_merge_redirect(
                conn, source_company_id=a, survivor_company_id=b, reason="test-chain"
            )
            record_company_merge_redirect(
                conn, source_company_id=b, survivor_company_id=c, reason="test-chain"
            )
            conn.commit()
            self.assertEqual(resolve_company_id(conn, a), c)
            self.assertEqual(resolve_company_id(conn, b), c)
            self.assertEqual(resolve_company_id(conn, c), c)
            with self.assertRaises(MergeRedirectError):
                record_company_merge_redirect(
                    conn, source_company_id=c, survivor_company_id=a, reason="cycle"
                )
            with self.assertRaises(MergeRedirectError):
                record_company_merge_redirect(
                    conn, source_company_id=c, survivor_company_id=c, reason="self"
                )
            missing = 9_999_999
            with self.assertRaises(MergeRedirectError):
                record_company_merge_redirect(
                    conn, source_company_id=c, survivor_company_id=missing, reason="gone"
                )
            with self.assertRaises(MergeRedirectError):
                record_company_merge_redirect(
                    conn, source_company_id=a, survivor_company_id=c, reason="dup-source"
                )

    def test_redirect_survives_source_delete(self) -> None:
        with get_connection() as conn:
            src = _insert_company(conn, "Loser Co")
            dst = _insert_company(conn, "Survivor Co")
            record_company_merge_redirect(
                conn, source_company_id=src, survivor_company_id=dst, reason="delete-ok"
            )
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM companies WHERE id = ?", (src,))
            conn.commit()
            gone = conn.execute("SELECT 1 FROM companies WHERE id = ?", (src,)).fetchone()
            self.assertIsNone(gone)
            row = conn.execute(
                """
                SELECT source_company_id, survivor_company_id
                FROM company_merge_history WHERE source_company_id = ?
                """,
                (src,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row[1]), dst)
            self.assertEqual(resolve_company_id(conn, src), dst)

    def test_contact_redirect_and_resolve(self) -> None:
        with get_connection() as conn:
            company = _insert_company(conn, "Contact Org")
            src = _insert_contact(conn, company, first_name="Pat", last_name="Lee")
            dst = _insert_contact(conn, company, first_name="Patricia", last_name="Lee")
            conn.commit()
            self.assertEqual(resolve_contact_id(conn, src), src)
            record_contact_merge_redirect(
                conn, source_contact_id=src, survivor_contact_id=dst, reason="dup"
            )
            conn.commit()
            self.assertEqual(resolve_contact_id(conn, src), dst)
            with self.assertRaises(MergeRedirectError):
                record_contact_merge_redirect(
                    conn, source_contact_id=src, survivor_contact_id=dst + 50, reason="dup2"
                )

    def test_contact_classification_rules(self) -> None:
        email_a = {"first_name": "Ann", "last_name": "Wu", "email": "Ann@Ex.com"}
        email_b = {"first_name": "A", "last_name": "Wu", "email": "ann@ex.com"}
        klass, reasons = classify_contact_pair(email_a, email_b)
        self.assertEqual(klass, "MERGE")
        self.assertIn("exact_normalized_email", reasons)

        phone_a = {"first_name": "Bo", "last_name": "Ng", "phone": "(816) 555-0100"}
        phone_b = {"first_name": "Robert", "last_name": "Ng", "phone": "8165550100"}
        klass, reasons = classify_contact_pair(phone_a, phone_b)
        self.assertEqual(klass, "MERGE")
        self.assertIn("exact_normalized_nanp10", reasons)

        name_title_a = {"first_name": "Cara", "last_name": "Diaz", "title": "Buyer"}
        name_title_b = {"first_name": "Cara", "last_name": "Diaz", "title": "buyer"}
        klass, _ = classify_contact_pair(name_title_a, name_title_b)
        self.assertEqual(klass, "MERGE")

        possible_a = {"first_name": "Drew", "last_name": "Kim"}
        possible_b = {"first_name": "Drew", "last_name": "Kim", "title": "VP"}
        klass, _ = classify_contact_pair(possible_a, possible_b)
        self.assertEqual(klass, "POSSIBLE")

        keep_a = {
            "first_name": "Evan",
            "last_name": "Park",
            "email": "evan.a@ex.com",
            "phone": "8165550101",
        }
        keep_b = {
            "first_name": "Evan",
            "last_name": "Park",
            "email": "evan.b@ex.com",
            "phone": "8165550102",
        }
        klass, _ = classify_contact_pair(keep_a, keep_b)
        self.assertEqual(klass, "KEEP_SEPARATE")

        unique_a = {"first_name": "Fay", "last_name": "Ortiz", "email": "fay@ex.com"}
        unique_b = {"first_name": "Gus", "last_name": "Ortiz", "phone": "8165550199"}
        klass, _ = classify_contact_pair(unique_a, unique_b)
        self.assertEqual(klass, "MOVE")

        switch_a = {
            "first_name": "Terry",
            "last_name": "Carver",
            "phone": "(816) 373-2720",
        }
        switch_b = {
            "first_name": "Josh",
            "last_name": "Anderson",
            "phone": "(816) 373-2720",
        }
        klass, _ = classify_contact_pair(
            switch_a, switch_b, company_nanp={"8163732720"}
        )
        self.assertEqual(klass, "MOVE")

        nick_a = {
            "first_name": "Joshua",
            "last_name": "Anderson",
            "title": "Manager, Quality Assurance",
        }
        nick_b = {
            "first_name": "Josh",
            "last_name": "Anderson",
            "title": "Quality Assurance Manager",
        }
        klass, reasons = classify_contact_pair(nick_a, nick_b)
        self.assertEqual(klass, "MERGE")
        self.assertIn("first_stem_last_plus_company", reasons)

    def test_planner_contacts_notes_and_ccr_block(self) -> None:
        with get_connection() as conn:
            client_id = _client_id(conn)
            survivor = _insert_company(conn, "Acme Survivor")
            source = _insert_company(conn, "Acme Duplicate")
            move_id = _insert_contact(
                conn,
                source,
                first_name="Unique",
                last_name="Person",
                email="unique@acme.test",
            )
            merge_src = _insert_contact(
                conn,
                source,
                first_name="Sam",
                last_name="Lee",
                email="sam@acme.test",
                title="Buyer",
            )
            merge_dst = _insert_contact(
                conn,
                survivor,
                first_name="Samuel",
                last_name="Lee",
                email="sam@acme.test",
                title="Purchasing",
            )
            possible_src = _insert_contact(
                conn, source, first_name="Jordan", last_name="Blake"
            )
            possible_dst = _insert_contact(
                conn, survivor, first_name="Jordan", last_name="Blake", title="Plant Mgr"
            )
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, ?, 'test'), (?, ?, ?, 'test'), (?, ?, ?, 'test')
                """,
                (
                    client_id,
                    source,
                    "Called the plant Tuesday.",
                    client_id,
                    survivor,
                    "called the plant tuesday.",
                    client_id,
                    source,
                    "Quoted 12 hoppers.",
                ),
            )
            _insert_ccr(conn, client_id, survivor, "Current Customer", "Keep me", "RN-S")
            _insert_ccr(conn, client_id, source, "New", "Quoted 12 hoppers.", "RN-L")
            conn.commit()
            plan = plan_company_merge(conn, source, survivor)
            refs = {r["table"] for r in discover_company_id_refs(conn)}

        self.assertEqual(plan["verdict"], "MERGE BLOCKED")
        self.assertTrue(plan["blockers"])
        self.assertFalse(plan["live_merge_executed"])
        contacts = plan["contacts"]
        actions = {c["source_contact_id"]: c["action"] for c in contacts["classifications"]}
        self.assertEqual(actions[move_id], "MOVE")
        self.assertEqual(actions[merge_src], "MERGE")
        self.assertEqual(actions[possible_src], "POSSIBLE")
        merge_row = next(c for c in contacts["classifications"] if c["source_contact_id"] == merge_src)
        self.assertEqual(merge_row["survivor_contact_id"], merge_dst)
        self.assertTrue(
            any(f["field"] in {"title", "first_name"} for f in merge_row["field_conflicts"])
        )
        self.assertEqual(contacts["unique_contacts_to_move"], 1)
        self.assertEqual(contacts["high_confidence_duplicates_to_consolidate"], 1)
        self.assertEqual(contacts["possible_duplicates_requiring_review"], 1)
        self.assertEqual(contacts["projected_survivor_contact_count"], 3)
        notes = plan["notes"]
        self.assertGreaterEqual(notes["exact_normalized_duplicates"], 1)
        self.assertGreaterEqual(notes["distinct_notes_to_preserve"], 1)
        texts = {n["norm"] for n in notes["distinct_note_rows"]}
        self.assertIn("quoted 12 hoppers.", texts)
        ccr_pair = next(p for p in plan["ccrs"]["pairs"] if p.get("same_client"))
        self.assertEqual(ccr_pair["decision"], "HUMAN REVIEW")
        self.assertTrue(ccr_pair["status_conflict"])
        self.assertFalse(ccr_pair["auto_resolve_status"])
        self.assertEqual(ccr_pair["rn_plan"], "preserve_both_via_source_identities")
        self.assertIn("contacts", refs)
        self.assertIn("client_company_relationships", refs)
        self.assertNotIn("company_merge_history", refs)

    def test_planner_ready_when_same_status_and_does_not_write(self) -> None:
        with get_connection() as conn:
            client_id = _client_id(conn)
            survivor = _insert_company(conn, "Ready Survivor")
            source = _insert_company(conn, "Ready Source")
            _insert_contact(conn, source, first_name="Only", last_name="New")
            _insert_ccr(conn, client_id, survivor, "Current Customer")
            _insert_ccr(conn, client_id, source, "Current Customer")
            before_companies = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
            before_redirects = int(
                conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]
            )
            plan = plan_company_merge(conn, source, survivor)
            after_companies = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
            after_redirects = int(
                conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]
            )
            after_contacts = int(
                conn.execute(
                    "SELECT COUNT(*) FROM contacts WHERE company_id IN (?, ?)",
                    (source, survivor),
                ).fetchone()[0]
            )
        self.assertEqual(plan["verdict"], "MERGE READY")
        self.assertEqual(before_companies, after_companies)
        self.assertEqual(before_redirects, after_redirects)
        self.assertEqual(after_contacts, 1)
        self.assertEqual(plan["contacts"]["unique_contacts_to_move"], 1)
        self.assertEqual(plan["contacts"]["projected_survivor_contact_count"], 1)

    def test_isolated_from_production(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        live = sqlite3.connect(
            f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro", uri=True
        )
        try:
            live.execute("PRAGMA query_only = ON")
            live_companies = int(live.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        finally:
            live.close()
        with get_connection() as conn:
            extra = _insert_company(conn, "Test-Only Merge Co")
            conn.commit()
            test_companies = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        self.assertGreater(test_companies, live_companies)
        self.assertGreater(extra, 0)


if __name__ == "__main__":
    unittest.main()
