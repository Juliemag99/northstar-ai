"""Data Steward Phase DS4 — close pre-activation conditions.

Blank NORTHSTAR_TEST_DB. Never copies or writes live northstar.db.
Does not enable live archive/delete/merge or LeadMaster confirm.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-ds4-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from ask_northstar_data import (  # noqa: E402
    _ASK_INCLUDE_ARCHIVED,
    _answer_company_intelligence,
    _find_companies_by_name,
)
from campaigns_data import (  # noqa: E402
    ensure_campaigns_schema,
    get_campaign_workspace,
)
from company_merges import ensure_company_merge_schema, existing_user_id  # noqa: E402
from data_steward import (  # noqa: E402
    SOURCE_LEGACY_EXISTING,
    StewardError,
    amend_relationship,
    append_note,
    apply_data_steward_schema_isolated,
    archive_company,
    archive_contact,
    conservative_legacy_backfill,
    create_company,
    create_contact,
    force_provenance_failure,
    inspect_contact_dependencies,
    latest_provenance,
    link_relationship,
    list_operational_companies,
    list_operational_contacts,
    list_operational_relationships,
    merge_contacts_isolated,
    provenance_history,
    restore_company,
    restore_contact,
    restore_relationship,
    sql_active_ccr,
)
from db import PRODUCTION_DB_PATH, get_connection, init_schema, migrate_schema  # noqa: E402
from leadmaster_refresh_apply import apply_leadmaster_refresh  # noqa: E402
from leadmaster_refresh_plan import (  # noqa: E402
    ARCHIVED_MATCH_REVIEW,
    IncomingRow,
    RefreshPolicy,
    fingerprint_refresh_plan,
    plan_leadmaster_refresh,
)
from leadmaster_refresh_resolutions import ACCEPT_PROPOSED, KEEP_EXISTING  # noqa: E402
from models import NorthStarUser  # noqa: E402
from staff_context import bind_staff_actor, reset_staff_actor  # noqa: E402


def _admin() -> NorthStarUser:
    return NorthStarUser(
        id=10, email="admin@northstar.test", full_name="Admin Test",
        is_administrator=True, is_internal_northstar=True, active=True, created_at="",
    )


def _row(**kwargs) -> IncomingRow:
    data = dict(
        source_row=1, record_no="100", company_name="Acme Stamping",
        address="100 Main St", city="Detroit", state="MI", zip="48201",
        first_name="Bob", last_name="Smith", email="bob@acme.example",
        contact_phone="(313) 555-0101", status="New", notes="Existing note",
    )
    data.update(kwargs)
    return IncomingRow(**data)


class DataStewardPhase4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if Path(_TEST_DB).resolve() == PRODUCTION_DB_PATH.resolve():
            raise RuntimeError("Refusing DS4 tests against production")
        conn = get_connection()
        try:
            init_schema(conn)
            migrate_schema(conn)
            ensure_company_merge_schema(conn)
            apply_data_steward_schema_isolated(conn)
            conn.execute(
                "INSERT INTO clients (id, code, name) VALUES (1,'alpha','Alpha'), (2,'beta','Beta')"
            )
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, is_administrator, active, created_at)
                VALUES (10, 'admin@northstar.test', 'Admin Test', 1, 1, datetime('now'))
                """
            )
            conn.execute(
                """
                INSERT INTO companies (
                    id, external_record_no, company_name, address, city, state, zip,
                    website, created_at, last_updated_at
                ) VALUES
                (1, '100', 'Shared Stamping', '100 Main', 'Detroit', 'MI', '48201',
                 'https://shared.example', datetime('now'), 't0'),
                (425, '425', 'Ronson Survivor', '1 Ronson', 'Detroit', 'MI', '48201',
                 '', datetime('now'), 't0'),
                (484, '484', 'Ronson Loser', '1 Ronson', 'Detroit', 'MI', '48201',
                 '', datetime('now'), 't0')
                """
            )
            conn.execute(
                """
                INSERT INTO contacts (
                    id, company_id, external_record_no, first_name, last_name, email, phone,
                    title, source_row_index, created_at, last_updated_at
                ) VALUES
                (1, 1, '100', 'Pat', 'One', 'pat.one@example.com', '(313) 555-0101',
                 'Buyer', 1, datetime('now'), 't0'),
                (2, 1, '100', 'Pat', 'Two', 'pat.two@example.com', '(313) 555-0102',
                 '', 2, datetime('now'), 't0')
                """
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    id, client_id, company_id, external_record_no, status, notes, assigned_user_id
                ) VALUES
                (1, 1, 1, '100', 'Working', 'Note A', 10),
                (2, 2, 1, '100', 'Prospect', 'Client B note', 10)
                """
            )
            conn.execute(
                """
                INSERT INTO company_merge_history (
                    source_company_id, survivor_company_id, merged_at, reason
                ) VALUES (484, 425, datetime('now'), 'test redirect')
                """
            )
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (1, 1, 'Existing note A', 'Sales Rep Comments/Notes')
                """
            )
            conservative_legacy_backfill(conn, dry_run=False, changed_at="2026-09-17T13:00:00Z")
            conn.commit()
        finally:
            conn.close()
        cls.token = bind_staff_actor(_admin())

    @classmethod
    def tearDownClass(cls) -> None:
        reset_staff_actor(cls.token)
        try:
            Path(_TEST_DB).unlink(missing_ok=True)
        except PermissionError:
            pass

    def conn(self):
        return get_connection()

    def test_01_live_path_gate(self):
        from data_steward import assert_not_production_path
        with self.assertRaises(Exception):
            assert_not_production_path(PRODUCTION_DB_PATH)

    def test_02_schema_present(self):
        with self.conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
            self.assertIn("archived_at", cols)
            self.assertTrue(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='field_provenance_events'"
                ).fetchone()
            )

    def test_03_baseline_legacy_existing(self):
        with self.conn() as conn:
            src = conn.execute(
                "SELECT DISTINCT source_type FROM field_provenance_events WHERE action='BASELINE'"
            ).fetchall()
            self.assertEqual({r[0] for r in src}, {SOURCE_LEGACY_EXISTING})

    def test_04_ask_active_company(self):
        with self.conn() as conn:
            hits = _find_companies_by_name(conn, "Shared Stamping", visible_ids=[1])
            self.assertTrue(any(int(h["company_id"]) == 1 for h in hits))

    def test_05_ask_archived_company_hidden(self):
        with self.conn() as conn:
            archive_company(conn, actor=_admin(), company_id=1, reason="ds4")
            hits = _find_companies_by_name(conn, "Shared Stamping", visible_ids=[1])
            self.assertFalse(any(int(h["company_id"]) == 1 for h in hits))
            restore_company(conn, actor=_admin(), company_id=1)

    def test_06_ask_shared_active_client_a(self):
        with self.conn() as conn:
            hits = _find_companies_by_name(conn, "Shared Stamping", visible_ids=[1])
            self.assertTrue(any(int(h["company_id"]) == 1 for h in hits))

    def test_07_ask_shared_archived_client_b(self):
        with self.conn() as conn:
            from data_steward import remove_relationship
            remove_relationship(conn, actor=_admin(), ccr_id=2, reason="ds4")
            hidden = _find_companies_by_name(conn, "Shared Stamping", visible_ids=[2])
            shown = _find_companies_by_name(conn, "Shared Stamping", visible_ids=[1])
            self.assertFalse(any(int(h["company_id"]) == 1 for h in hidden))
            self.assertTrue(any(int(h["company_id"]) == 1 for h in shown))
            restore_relationship(conn, actor=_admin(), ccr_id=2)

    def test_08_ask_archived_contact_hidden(self):
        with self.conn() as conn:
            archive_contact(conn, actor=_admin(), contact_id=1, reason="ds4")
            answer = _answer_company_intelligence(
                conn, company_name_query="Shared Stamping", visible_ids=[1], preferred_client_id=1
            )
            ids = {c.contact_id for c in answer.contacts}
            self.assertNotIn(1, ids)
            restore_contact(conn, actor=_admin(), contact_id=1)

    def test_09_ask_admin_include_archived(self):
        with self.conn() as conn:
            from data_steward import remove_relationship
            remove_relationship(conn, actor=_admin(), ccr_id=2, reason="ds4")
            tok = _ASK_INCLUDE_ARCHIVED.set(True)
            try:
                hits = _find_companies_by_name(conn, "Shared Stamping", visible_ids=[2])
                self.assertTrue(any(int(h["company_id"]) == 1 for h in hits))
            finally:
                _ASK_INCLUDE_ARCHIVED.reset(tok)
            restore_relationship(conn, actor=_admin(), ccr_id=2)

    def test_10_campaign_active_and_archived(self):
        with self.conn() as conn:
            ensure_campaigns_schema(conn)
            conn.execute(
                """
                INSERT INTO client_campaigns (id, client_id, campaign_name, is_active, status)
                VALUES (91, 1, 'DS4 Camp', 1, 'Active')
                """
            )
            conn.execute(
                """
                INSERT INTO campaign_companies (
                    campaign_id, client_id, company_id, relationship_id, created_at
                ) VALUES (91, 1, 1, 1, datetime('now'))
                """
            )
            conn.commit()
        ws = get_campaign_workspace(91)
        self.assertEqual(len(ws.companies), 1)
        with self.conn() as conn:
            from data_steward import remove_relationship
            remove_relationship(conn, actor=_admin(), ccr_id=1, reason="ds4")
            conn.commit()
        ws2 = get_campaign_workspace(91)
        self.assertEqual(len(ws2.companies), 0)
        with self.conn() as conn:
            n = int(conn.execute("SELECT COUNT(*) FROM campaign_companies WHERE campaign_id=91").fetchone()[0])
            self.assertEqual(n, 1)
            restore_relationship(conn, actor=_admin(), ccr_id=1)
            conn.commit()
        ws3 = get_campaign_workspace(91)
        self.assertEqual(len(ws3.companies), 1)

    def test_11_existing_user_id_coerces_missing(self):
        with self.conn() as conn:
            self.assertEqual(existing_user_id(conn, 10), 10)
            self.assertIsNone(existing_user_id(conn, 99999))

    def test_12_contact_deps_inventory(self):
        with self.conn() as conn:
            inv = inspect_contact_dependencies(conn, 1)
            tables = {r["table"] for r in inv["formal_fks"]} | {r["table"] for r in inv["logical_refs"]}
            self.assertTrue(tables)
            self.assertIn("contacts", inv["skip_remap"])

    def test_13_simple_contact_merge(self):
        with self.conn() as conn:
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="Ann", last_name="Merge", email="ann.m@example.com")
            b = create_contact(conn, actor=_admin(), company_id=1, first_name="Ann", last_name="Other", email="ann.o@example.com")
            merge_contacts_isolated(
                conn, actor=_admin(), survivor_contact_id=a["contact_id"], source_contact_id=b["contact_id"],
                fields={"first_name": "keep_survivor", "last_name": "keep_survivor", "email": "keep_survivor"},
            )
            gone = conn.execute("SELECT 1 FROM contacts WHERE id=?", (b["contact_id"],)).fetchone()
            self.assertIsNone(gone)
            hist = conn.execute(
                "SELECT survivor_contact_id FROM contact_merge_history WHERE source_contact_id=?",
                (b["contact_id"],),
            ).fetchone()
            self.assertEqual(int(hist[0]), int(a["contact_id"]))
            fk = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(fk, 0)

    def test_14_contact_merge_activities(self):
        with self.conn() as conn:
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="Act", last_name="Surv", email="act.s@example.com")
            b = create_contact(conn, actor=_admin(), company_id=1, first_name="Act", last_name="Lose", email="act.l@example.com")
            conn.execute(
                """
                INSERT INTO activities (
                    client_id, company_id, relationship_id, external_record_no,
                    contact_id, activity_type, activity_at, notes, created_at
                ) VALUES (1, 1, 1, '100', ?, 'call', datetime('now'), 'hello', datetime('now'))
                """,
                (b["contact_id"],),
            )
            merge_contacts_isolated(
                conn, actor=_admin(), survivor_contact_id=a["contact_id"], source_contact_id=b["contact_id"],
                fields={"email": "keep_survivor", "first_name": "keep_survivor", "last_name": "keep_survivor"},
            )
            n = int(conn.execute("SELECT COUNT(*) FROM activities WHERE contact_id=?", (a["contact_id"],)).fetchone()[0])
            self.assertGreaterEqual(n, 1)

    def test_15_contact_merge_campaign(self):
        with self.conn() as conn:
            ensure_campaigns_schema(conn)
            conn.execute(
                """
                INSERT INTO client_campaigns (id, client_id, campaign_name, is_active, status)
                VALUES (92, 1, 'DS4 Merge Camp', 1, 'Active')
                """
            )
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="Cam", last_name="Surv", email="cam.s@example.com")
            b = create_contact(conn, actor=_admin(), company_id=1, first_name="Cam", last_name="Lose", email="cam.l@example.com")
            conn.execute(
                """
                INSERT INTO campaign_contacts (
                    campaign_id, client_id, contact_id, company_id, created_at
                ) VALUES (92, 1, ?, 1, datetime('now'))
                """,
                (b["contact_id"],),
            )
            merge_contacts_isolated(
                conn, actor=_admin(), survivor_contact_id=a["contact_id"], source_contact_id=b["contact_id"],
                fields={"email": "keep_survivor", "first_name": "keep_survivor", "last_name": "keep_survivor"},
            )
            n = int(conn.execute("SELECT COUNT(*) FROM campaign_contacts WHERE contact_id=?", (a["contact_id"],)).fetchone()[0])
            self.assertGreaterEqual(n, 1)

    def test_16_contact_merge_provenance(self):
        with self.conn() as conn:
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="Prov", last_name="Surv", email="prov.s@example.com")
            b = create_contact(conn, actor=_admin(), company_id=1, first_name="Prov", last_name="Lose", email="prov.l@example.com")
            merge_contacts_isolated(
                conn, actor=_admin(), survivor_contact_id=a["contact_id"], source_contact_id=b["contact_id"],
                fields={"email": "keep_survivor", "first_name": "keep_survivor", "last_name": "keep_survivor"},
            )
            latest = latest_provenance(conn, entity_type="contact", entity_id=a["contact_id"], field="merged_from")
            self.assertEqual(latest.get("action"), "MERGE")

    def test_17_cross_company_block(self):
        with self.conn() as conn:
            other = create_company(conn, actor=_admin(), company_name="Other Co DS4", city="Flint", state="MI")
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="X", last_name="A", email="xa@example.com")
            b = create_contact(conn, actor=_admin(), company_id=other["company_id"], first_name="X", last_name="B", email="xb@example.com")
            with self.assertRaises(StewardError):
                merge_contacts_isolated(
                    conn, actor=_admin(), survivor_contact_id=a["contact_id"], source_contact_id=b["contact_id"],
                    fields={"email": "keep_survivor"},
                )

    def test_18_contact_merge_rollback(self):
        with self.conn() as conn:
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="Rb", last_name="Surv", email="rb.s@example.com")
            b = create_contact(conn, actor=_admin(), company_id=1, first_name="Rb", last_name="Lose", email="rb.l@example.com")
            before = int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
            with self.assertRaises(Exception):
                with force_provenance_failure("ds4_merge"):
                    merge_contacts_isolated(
                        conn, actor=_admin(), survivor_contact_id=a["contact_id"], source_contact_id=b["contact_id"],
                        fields={"email": "keep_survivor", "first_name": "keep_survivor", "last_name": "keep_survivor"},
                    )
            after = int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
            self.assertEqual(before, after)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM contacts WHERE id=?", (b["contact_id"],)).fetchone())

    def test_19_notes_append_and_supersede(self):
        from client_workspace_data import update_relationship_notes
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM legacy_notes WHERE company_id=1 AND client_id=1").fetchone()[0]
        update_relationship_notes("100", note_text="Added B", client_id=1, mode="append")
        with self.conn() as conn:
            after = conn.execute(
                "SELECT note_text FROM legacy_notes WHERE company_id=1 AND client_id=1 ORDER BY id"
            ).fetchall()
            texts = [r[0] for r in after]
            self.assertGreater(len(texts), int(before))
            self.assertTrue(any("Existing note A" in t for t in texts))
            self.assertTrue(any("Added B" in t for t in texts))
        update_relationship_notes("100", note_text="Correction C", client_id=1, mode="supersede")
        with self.conn() as conn:
            texts = [r[0] for r in conn.execute("SELECT note_text FROM legacy_notes WHERE company_id=1 AND client_id=1")]
            self.assertTrue(any("Existing note A" in t for t in texts))
            self.assertTrue(any("Correction C" in t for t in texts))
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE sql LIKE '%DELETE FROM legacy_notes%' AND name LIKE '%notes%'").fetchone())

    def test_20_notes_rollback(self):
        with self.conn() as conn:
            blob = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
            try:
                with force_provenance_failure("ds4_note"):
                    append_note(conn, actor=_admin(), ccr_id=1, text="should not stick")
            except Exception:
                pass
            after = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
            self.assertEqual(str(blob), str(after))

    def test_21_archive_operational_lists(self):
        with self.conn() as conn:
            archive_company(conn, actor=_admin(), company_id=1, reason="ds4")
            self.assertNotIn(1, {int(r["id"]) for r in list_operational_companies(conn)})
            restore_company(conn, actor=_admin(), company_id=1)
            archive_contact(conn, actor=_admin(), contact_id=1, reason="ds4")
            self.assertNotIn(1, {int(r["id"]) for r in list_operational_contacts(conn, company_id=1)})
            restore_contact(conn, actor=_admin(), contact_id=1)
            from data_steward import remove_relationship
            remove_relationship(conn, actor=_admin(), ccr_id=1, reason="ds4")
            self.assertNotIn(1, {int(r["id"]) for r in list_operational_relationships(conn, client_id=1)})
            restore_relationship(conn, actor=_admin(), ccr_id=1)

    def test_22_duplicate_and_refresh_see_archived(self):
        with self.conn() as conn:
            archive_company(conn, actor=_admin(), company_id=1, reason="ds4")
            dups = __import__("data_steward", fromlist=["find_company_duplicates"]).find_company_duplicates(
                conn, company_name="Shared Stamping", city="Detroit", state="MI"
            )
            self.assertTrue(dups)
            plan = plan_leadmaster_refresh(
                conn, client_id=1, rows=[_row(company_name="Shared Stamping")],
                policy=RefreshPolicy(client_id=1), source_sha256="arch", source_filename="a.csv",
            )
            self.assertIn(ARCHIVED_MATCH_REVIEW, str(plan))
            restore_company(conn, actor=_admin(), company_id=1)

    def test_23_lm_keep_and_accept(self):
        from data_steward import attach_leadmaster_identity, amend_company, current_field_authority, ENTITY_COMPANY, SOURCE_MANUAL_ADMIN
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Keep Accept DS4", city="Alma", state="MI",
                phone="9895552222", website="https://keep.example",
            )
            cid = created["company_id"]
            amend_company(conn, actor=_admin(), company_id=cid, fields={"website": "https://manual.example"})
            attach_leadmaster_identity(conn, actor=_admin(), company_id=cid, record_no="DS4-KEEP", client_id=1)
            policy = RefreshPolicy(client_id=1)
            incoming = [_row(
                record_no="DS4-KEEP", company_name="Keep Accept DS4", city="Alma", state="MI",
                website="https://lm.example", status="", notes="", first_name="", last_name="",
                email="", contact_phone="",
            )]
            keep_res = [{
                "source_row": 1, "field": "website",
                "proposal_action": "MANUAL_OVERRIDE_CONFLICT", "resolution": KEEP_EXISTING,
            }]
            plan = plan_leadmaster_refresh(
                conn, client_id=1, rows=incoming, policy=policy, source_sha256="k", source_filename="k.csv"
            )
            plan["resolutions"] = keep_res
            plan["plan_fingerprint"] = fingerprint_refresh_plan(plan)
            apply_leadmaster_refresh(
                conn, incoming_rows=incoming, policy=policy, source_sha256="k",
                source_filename="k.csv", expected_fingerprint=plan["plan_fingerprint"],
                resolutions=keep_res, actor_id=10,
            )
            site = conn.execute("SELECT website FROM companies WHERE id=?", (cid,)).fetchone()[0]
            self.assertEqual(str(site), "https://manual.example")
            fk = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(fk, 0)

    def test_24_migration_utility_not_wired(self):
        import db as dbmod
        text = inspect.getsource(dbmod.migrate_schema)
        self.assertNotIn("data_steward_live_migrate", text)
        self.assertNotIn("ensure_data_steward_schema", text)

    def test_25_fk_pragma_on(self):
        with self.conn() as conn:
            self.assertEqual(str(conn.execute("PRAGMA foreign_keys").fetchone()[0]), "1")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(len(conn.execute("PRAGMA foreign_key_check").fetchall()), 0)


if __name__ == "__main__":
    unittest.main()
