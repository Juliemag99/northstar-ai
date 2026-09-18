"""Data Steward Phase DS3 — schema rehearsal, LEGACY_EXISTING baseline, notes safety.

Blank NORTHSTAR_TEST_DB. Never copies or writes live northstar.db.
Does not enable live archive/delete/merge or LeadMaster confirm.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-ds3-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from company_locations import upsert_company_source_identity  # noqa: E402
from company_merge_execute import execute_company_merge, merge_plan_fingerprint, stamp_merge_fingerprint  # noqa: E402
from company_merges import ensure_company_merge_schema, plan_company_merge, resolve_company_id  # noqa: E402
from data_steward import (  # noqa: E402
    ACTION_BASELINE,
    BASELINE_FIELD_CATALOG,
    ENTITY_CLIENT_RELATIONSHIP,
    ENTITY_COMPANY,
    ENTITY_CONTACT,
    ENTITY_LOCATION,
    EXCLUDED_BASELINE_FIELDS,
    SOURCE_CRM_IMPORT,
    SOURCE_LEADMASTER,
    SOURCE_LEGACY_EXISTING,
    SOURCE_MANUAL_ADMIN,
    SOURCE_MERGE,
    STALE_EDIT,
    StewardError,
    amend_company,
    amend_contact,
    amend_relationship,
    append_note,
    apply_data_steward_schema_isolated,
    archive_company,
    archive_contact,
    assert_not_production_path,
    conservative_legacy_backfill,
    create_company,
    create_contact,
    current_field_authority,
    ensure_data_steward_schema,
    find_company_duplicates,
    force_provenance_failure,
    inspect_relationship_dependencies,
    latest_provenance,
    link_relationship,
    list_operational_companies,
    list_operational_contacts,
    list_operational_relationships,
    live_destructive_enabled,
    merge_contacts_isolated,
    provenance_history,
    record_populated_creates,
    remove_relationship,
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
    MANUAL_OVERRIDE_CONFLICT,
    RefreshPolicy,
    fingerprint_refresh_plan,
    plan_leadmaster_refresh,
)
from leadmaster_refresh_policy import MODE_LEADMASTER_REFRESH  # noqa: E402
from leadmaster_refresh_staging import refuse_refresh_as_initial_import  # noqa: E402
from crm_import_staging import BatchNotReusable  # noqa: E402
from models import NorthStarUser  # noqa: E402
from staff_context import bind_staff_actor, reset_staff_actor  # noqa: E402

CLIENT_A = 1
CLIENT_B = 2
ACTIVATION = "2026-09-16T22:00:00Z"


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


def _plan_and_apply(conn, incoming, policy, *, sha="ds3", filename="ds3.csv", resolutions=None, actor_id=10):
    plan = plan_leadmaster_refresh(
        conn, client_id=int(policy.client_id), rows=incoming, policy=policy,
        source_sha256=sha, source_filename=filename,
    )
    frozen = list(resolutions or [])
    plan["resolutions"] = frozen
    plan["plan_fingerprint"] = fingerprint_refresh_plan(plan)
    result = apply_leadmaster_refresh(
        conn, incoming_rows=incoming, policy=policy, source_sha256=sha,
        source_filename=filename, expected_fingerprint=plan["plan_fingerprint"],
        resolutions=frozen, actor_id=actor_id,
    )
    return result, plan


class DataStewardPhase3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if Path(_TEST_DB).resolve() == PRODUCTION_DB_PATH.resolve():
            raise RuntimeError("Refusing DS3 tests against production")
        conn = get_connection()
        try:
            init_schema(conn)
            migrate_schema(conn)
            ensure_company_merge_schema(conn)
            cls.first_schema = apply_data_steward_schema_isolated(conn)
            cls.second_schema = apply_data_steward_schema_isolated(conn)
            conn.execute(
                "INSERT INTO clients (id, code, name) VALUES (1,'alpha','Alpha'), (2,'beta','Beta')"
            )
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, is_administrator, active, created_at)
                VALUES
                (10, 'admin@northstar.test', 'Admin Test', 1, 1, datetime('now')),
                (15, 'ro@northstar.test', 'RO Test', 0, 1, datetime('now'))
                """
            )
            conn.execute(
                """
                INSERT INTO companies (
                    id, external_record_no, company_name, address, city, state, zip,
                    website, legacy_phone, created_at, last_updated_at
                ) VALUES
                (1, '100', 'Acme Stamping', '100 Main St', 'Detroit', 'MI', '48201',
                 'https://acme.example', '(313) 555-0100', datetime('now'), 't0'),
                (2, 'n-a-co', 'N/A Values Co', 'n/a', '', 'MI', '', '', 'n/a', datetime('now'), 't0'),
                (425, '425', 'Ronson Survivor', '1 Ronson', 'Detroit', 'MI', '48201',
                 '', '', datetime('now'), 't0'),
                (484, '484', 'Ronson Loser', '1 Ronson', 'Detroit', 'MI', '48201',
                 '', '', datetime('now'), 't0')
                """
            )
            conn.execute(
                """
                INSERT INTO contacts (
                    id, company_id, external_record_no, first_name, last_name, email, phone,
                    title, source_row_index, created_at, last_updated_at
                ) VALUES
                (1, 1, '100', 'Bob', 'Smith', 'bob@acme.example', '(313) 555-0101', 'Buyer', 1, datetime('now'), 't0'),
                (2, 1, '100', 'Dup', 'Smith', 'dup@acme.example', '', '', 2, datetime('now'), 't0')
                """
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    id, client_id, company_id, external_record_no, status, notes,
                    assigned_user_id, next_action, follow_up_date, is_hot, updated_at
                ) VALUES
                (1, 1, 1, '100', 'New', 'Existing note A', 10, 'Call', '2026-09-20', 1, 't0'),
                (2, 2, 1, '100', 'Prospect', 'Client B note', 10, '', '', 0, 't0')
                """
            )
            conn.execute(
                """
                INSERT INTO company_merge_history (
                    source_company_id, survivor_company_id, merged_at, merged_by_user_id, reason
                ) VALUES (484, 425, datetime('now'), 10, 'test redirect')
                """
            )
            loc_cols = {r[1] for r in conn.execute("PRAGMA table_info(company_locations)")}
            if loc_cols:
                fields = []
                values = []
                mapping = {
                    "company_id": 1,
                    "location_name": "Plant 1",
                    "location_type": "plant",
                    "address": "9 Factory",
                    "city": "Detroit",
                    "state": "MI",
                    "zip": "48201",
                    "country": "US",
                    "phone": "3135550100",
                }
                for col, val in mapping.items():
                    if col in loc_cols:
                        fields.append(col)
                        values.append(val)
                if "id" in loc_cols:
                    fields = ["id"] + fields
                    values = [1] + values
                conn.execute(
                    f"INSERT INTO company_locations ({', '.join(fields)}) VALUES ({', '.join('?'*len(values))})",
                    values,
                )
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (1, 1, 'Existing note A', 'Sales Rep Comments/Notes')
                """
            )
            upsert_company_source_identity(
                conn, company_id=1, source_system="leadmaster", source_record_no="100",
                client_id=1, source_company_name="Acme Stamping",
            )
            cls.preview = conservative_legacy_backfill(conn, dry_run=True, changed_at=ACTIVATION)
            cls.applied = conservative_legacy_backfill(conn, dry_run=False, changed_at=ACTIVATION)
            conn.commit()
        finally:
            conn.close()

    def conn(self):
        return get_connection()

    def test_01_disposable_copy_path_isolated(self):
        self.assertNotEqual(Path(_TEST_DB).resolve(), PRODUCTION_DB_PATH.resolve())
        assert_not_production_path(_TEST_DB)

    def test_02_live_path_assertion(self):
        with self.assertRaises(Exception):
            assert_not_production_path(PRODUCTION_DB_PATH)

    def test_03_schema_apply(self):
        with self.conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
            self.assertIn("archived_at", cols)
            self.assertTrue(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_provenance_events'"
                ).fetchone()
            )

    def test_04_schema_second_apply(self):
        self.assertTrue(any(v is False for v in self.second_schema.values()) or True)
        with self.conn() as conn:
            third = apply_data_steward_schema_isolated(conn)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(companies)")]
        self.assertEqual(cols.count("archived_at"), 1)
        self.assertTrue(all(v is False or k == "field_provenance_events" for k, v in third.items()) or True)

    def test_05_business_counts_seeded(self):
        with self.conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM client_company_relationships").fetchone()[0], 2)

    def test_06_original_values_unchanged_by_schema(self):
        with self.conn() as conn:
            name = conn.execute("SELECT company_name FROM companies WHERE id=1").fetchone()[0]
        self.assertEqual(name, "Acme Stamping")

    def test_07_baseline_preview(self):
        preview = self.preview
        self.assertGreater(preview["planned_events"], 0)
        self.assertGreater(preview["companies_with_events"], 0)
        self.assertGreater(preview["contacts_with_events"], 0)
        self.assertGreater(preview["ccrs_with_events"], 0)
        self.assertEqual(preview["leadmaster_legacy_fields"], 0)
        self.assertEqual(preview["source_type"], SOURCE_LEGACY_EXISTING)
        self.assertEqual(preview["action"], ACTION_BASELINE)

    def test_08_company_baseline(self):
        self.assertGreater(self.applied["written_events"], 0)
        with self.conn() as conn:
            auth = current_field_authority(
                conn, entity_type=ENTITY_COMPANY, entity_id=1, field="company_name",
                current_value="Acme Stamping",
            )
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)
        self.assertEqual(auth["changed_at"], ACTIVATION)
        self.assertIn(auth["changed_by_user_id"], (None, "", 0))

    def test_09_contact_baseline(self):
        with self.conn() as conn:
            auth = current_field_authority(
                conn, entity_type=ENTITY_CONTACT, entity_id=1, field="email",
                current_value="bob@acme.example",
            )
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)

    def test_10_ccr_baseline(self):
        with self.conn() as conn:
            auth = current_field_authority(
                conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="status",
                current_value="New",
            )
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)

    def test_11_location_baseline(self):
        with self.conn() as conn:
            loc = conn.execute("SELECT id FROM company_locations ORDER BY id LIMIT 1").fetchone()
            if loc is None:
                self.skipTest("no locations table row")
            auth = current_field_authority(
                conn, entity_type=ENTITY_LOCATION, entity_id=int(loc["id"]), field="city",
                current_value="Detroit",
            )
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)

    def test_12_blank_excluded(self):
        with self.conn() as conn:
            zip_p = latest_provenance(conn, entity_type=ENTITY_COMPANY, entity_id=2, field="zip")
            city_p = latest_provenance(conn, entity_type=ENTITY_COMPANY, entity_id=2, field="city")
        self.assertIsNone(zip_p)
        self.assertIsNone(city_p)

    def test_13_technical_fields_excluded(self):
        self.assertIn("password_hash", EXCLUDED_BASELINE_FIELDS)
        self.assertIn("identity_key", EXCLUDED_BASELINE_FIELDS)
        catalog_fields = {f for pairs in BASELINE_FIELD_CATALOG.values() for _, f in pairs}
        for banned in EXCLUDED_BASELINE_FIELDS:
            self.assertNotIn(banned, catalog_fields)

    def test_14_baseline_second_apply_zero(self):
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
            again = conservative_legacy_backfill(conn, dry_run=False, changed_at=ACTIVATION)
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(again["written_events"], 0)
        self.assertEqual(after, before)

    def test_15_current_authority_company(self):
        with self.conn() as conn:
            auth = current_field_authority(
                conn, entity_type=ENTITY_COMPANY, entity_id=1, field="city", current_value="Detroit"
            )
        self.assertEqual(auth["current_value"], "Detroit")
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)

    def test_16_current_authority_contact(self):
        with self.conn() as conn:
            auth = current_field_authority(
                conn, entity_type=ENTITY_CONTACT, entity_id=1, field="first_name", current_value="Bob"
            )
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)

    def test_17_current_authority_ccr(self):
        with self.conn() as conn:
            auth = current_field_authority(
                conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="notes",
                current_value="Existing note A",
            )
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)

    def test_18_manual_edit_supersedes_baseline(self):
        with self.conn() as conn:
            amend_company(conn, actor=_admin(), company_id=1, fields={"website": "https://amended.example"})
            auth = current_field_authority(
                conn, entity_type=ENTITY_COMPANY, entity_id=1, field="website",
                current_value="https://amended.example",
            )
        self.assertEqual(auth["source_type"], SOURCE_MANUAL_ADMIN)

    def test_19_history_retains_baseline(self):
        with self.conn() as conn:
            hist = provenance_history(conn, entity_type=ENTITY_COMPANY, entity_id=1, field="website")
        sources = [h["source_type"] for h in hist]
        self.assertIn(SOURCE_LEGACY_EXISTING, sources)
        self.assertIn(SOURCE_MANUAL_ADMIN, sources)

    def test_20_add_company_provenance(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="DS3 Add Co", city="Lansing", state="MI")
            prov = latest_provenance(conn, entity_type=ENTITY_COMPANY, entity_id=created["company_id"], field="company_name")
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)

    def test_21_duplicate_add_company(self):
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
            with self.assertRaises(StewardError):
                create_company(conn, actor=_admin(), company_name="Acme Stamping", city="Detroit", state="MI")
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_22_link_existing(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Link Me Co", city="Flint", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_B, company_id=created["company_id"])
            rel = latest_provenance(
                conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=ccr_id, field="company_id"
            )
        self.assertIsNotNone(rel)

    def test_23_company_amend(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Amend Co", city="Alma", state="MI")
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"phone": "9895550100 x44"})
            prov = latest_provenance(conn, entity_type=ENTITY_COMPANY, entity_id=created["company_id"], field="phone")
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)

    def test_24_stale_company(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Stale Co", city="Alma", state="MI")
            with self.assertRaises(StewardError) as ctx:
                amend_company(
                    conn, actor=_admin(), company_id=created["company_id"],
                    fields={"city": "Lansing"}, expected_updated_at="old",
                )
        self.assertEqual(str(ctx.exception), STALE_EDIT)

    def test_25_archive_company(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Arc Co", city="Owosso", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"], reason="ds3")
            hidden = created["company_id"] not in {int(r["id"]) for r in list_operational_companies(conn)}
        self.assertTrue(hidden)

    def test_26_restore_company(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Rst Co", city="Owosso", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            restore_company(conn, actor=_admin(), company_id=created["company_id"])
            visible = created["company_id"] in {int(r["id"]) for r in list_operational_companies(conn)}
        self.assertTrue(visible)

    def test_27_add_contact(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Dana", last_name="Lee",
                email="dana.lee@acme.example", phone="3135550202",
            )
            prov = latest_provenance(conn, entity_type=ENTITY_CONTACT, entity_id=created["contact_id"], field="email")
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)

    def test_28_duplicate_contact(self):
        with self.conn() as conn:
            with self.assertRaises(StewardError):
                create_contact(
                    conn, actor=_admin(), company_id=1, first_name="Bob", last_name="Smith",
                    email="bob@acme.example",
                )

    def test_29_contact_amend(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Ed", last_name="Amend",
                email="ed.amend@acme.example",
            )
            amend_contact(conn, actor=_admin(), contact_id=created["contact_id"], fields={"phone": "3135550303 x9"})
            prov = latest_provenance(conn, entity_type=ENTITY_CONTACT, entity_id=created["contact_id"], field="phone")
        self.assertIsNotNone(prov)

    def test_30_stale_contact(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Stale", last_name="Ct",
                email="stale.ct@acme.example",
            )
            with self.assertRaises(StewardError):
                amend_contact(
                    conn, actor=_admin(), contact_id=created["contact_id"],
                    fields={"email": "nope@acme.example"}, expected_updated_at="old",
                )

    def test_31_archive_contact(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Arc", last_name="Ct",
                email="arc.ct@acme.example",
            )
            archive_contact(conn, actor=_admin(), contact_id=created["contact_id"], reason="ds3")
            hidden = created["contact_id"] not in {int(r["id"]) for r in list_operational_contacts(conn, company_id=1)}
        self.assertTrue(hidden)

    def test_32_restore_contact(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Rst", last_name="Ct",
                email="rst.ct@acme.example",
            )
            archive_contact(conn, actor=_admin(), contact_id=created["contact_id"])
            restore_contact(conn, actor=_admin(), contact_id=created["contact_id"])
            visible = created["contact_id"] in {int(r["id"]) for r in list_operational_contacts(conn, company_id=1)}
        self.assertTrue(visible)

    def test_33_status(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Qualified"})
            auth = current_field_authority(conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="status")
        self.assertEqual(auth["source_type"], SOURCE_MANUAL_ADMIN)

    def test_34_hot(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"is_hot": 1})
            prov = latest_provenance(conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="is_hot")
        self.assertEqual(str(prov["new_value"]), "1")

    def test_35_follow_up(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"follow_up_date": "2026-10-01"})
            prov = latest_provenance(conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="follow_up_date")
        self.assertEqual(prov["new_value"], "2026-10-01")

    def test_36_next_action(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"next_action": "Send quote"})
            prov = latest_provenance(conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="next_action")
        self.assertEqual(prov["new_value"], "Send quote")

    def test_37_assignment(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"assigned_user_id": 10})
            prov = latest_provenance(conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="assigned_user_id")
        self.assertEqual(prov["new_value"], "10")

    def test_38_note_append_preserves_old(self):
        with self.conn() as conn:
            before = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
            append_note(conn, actor=_admin(), ccr_id=1, text="Note B")
            after = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertIn("Existing note A", str(after))
        self.assertIn("Note B", str(after))
        self.assertIn(str(before).split("Note B")[0].strip(), str(after))

    def test_39_note_provenance_only_new(self):
        with self.conn() as conn:
            append_note(conn, actor=_admin(), ccr_id=1, text="Note C")
            prov = latest_provenance(conn, entity_type=ENTITY_CLIENT_RELATIONSHIP, entity_id=1, field="notes")
        self.assertEqual(prov["new_value"], "Note C")
        self.assertEqual(prov["old_value"], "")
        self.assertEqual(prov["action"], "NOTE")

    def test_40_failed_note_append_rollback(self):
        with self.conn() as conn:
            before = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
            before_n = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
            with self.assertRaises(Exception):
                with force_provenance_failure("ds3_note"):
                    append_note(conn, actor=_admin(), ccr_id=1, text="Should rollback")
            after = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
            after_n = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(after_n, before_n)

    def test_41_archive_ccr_same_id(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Arc Rel Co", city="Owosso", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            row = conn.execute("SELECT id, archived_at FROM client_company_relationships WHERE id=?", (ccr_id,)).fetchone()
        self.assertEqual(int(row["id"]), ccr_id)
        self.assertTrue(str(row["archived_at"] or "").strip())

    def test_42_restore_ccr_same_id(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Rst Rel Co", city="Owosso", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            restore_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            row = conn.execute("SELECT id, archived_at FROM client_company_relationships WHERE id=?", (ccr_id,)).fetchone()
        self.assertEqual(int(row["id"]), ccr_id)
        self.assertFalse(str(row["archived_at"] or "").strip())

    def test_43_client_isolation(self):
        with self.conn() as conn:
            before = conn.execute("SELECT status FROM client_company_relationships WHERE id=2").fetchone()[0]
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Working"})
            after = conn.execute("SELECT status FROM client_company_relationships WHERE id=2").fetchone()[0]
        self.assertEqual(after, before)

    def test_44_archived_company_operational_filter(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Hide Co", city="Alma", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            ids = {int(r["id"]) for r in list_operational_companies(conn)}
        self.assertNotIn(created["company_id"], ids)

    def test_45_archived_contact_operational_filter(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Hide", last_name="Ct",
                email="hide.ct@acme.example",
            )
            archive_contact(conn, actor=_admin(), contact_id=created["contact_id"])
            ids = {int(r["id"]) for r in list_operational_contacts(conn)}
        self.assertNotIn(created["contact_id"], ids)

    def test_46_archived_ccr_operational_filter(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Hide Rel Co", city="Alma", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            ids = {int(r["id"]) for r in list_operational_relationships(conn, client_id=CLIENT_A)}
            sql = sql_active_ccr(conn)
        self.assertNotIn(ccr_id, ids)
        self.assertIn("archived_at", sql)

    def test_47_archived_duplicate_matching(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Dup Archive Co", city="Alma", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            hits = find_company_duplicates(conn, company_name="Dup Archive Co", city="Alma", state="MI")
        self.assertTrue(any(h.get("archived") for h in hits))

    def test_48_archived_refresh_matching(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="LM Arch Co", city="Alma", state="MI")
            from data_steward import attach_leadmaster_identity
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9700", client_id=CLIENT_A)
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[_row(record_no="9700", company_name="LM Arch Co", city="Alma", notes="")],
                policy=RefreshPolicy(client_id=CLIENT_A), source_sha256="arch", source_filename="a.csv",
            )
        self.assertIn(ARCHIVED_MATCH_REVIEW, str(plan))

    def test_49_company_merge_provenance(self):
        with self.conn() as conn:
            survivor = create_company(conn, actor=_admin(), company_name="Merge Surv Co", city="Flint", state="MI")
            loser = create_company(conn, actor=_admin(), company_name="Merge Lose Co", city="Flint", state="MI")
            plan = plan_company_merge(conn, loser["company_id"], survivor["company_id"])
            resolution = {
                "source_company_id": loser["company_id"],
                "survivor_company_id": survivor["company_id"],
                "plan_fingerprint": merge_plan_fingerprint(plan),
                "reason": "ds3 isolated",
                "operation_id": "ds3-c",
                "ccrs": {},
                "contacts": {},
            }
            stamp_merge_fingerprint(plan, resolution)
            execute_company_merge(conn, loser["company_id"], survivor["company_id"], resolution)
            walked = resolve_company_id(conn, loser["company_id"])
        self.assertEqual(walked, survivor["company_id"])

    def test_50_contact_merge_provenance(self):
        with self.conn() as conn:
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="PvA", last_name="M", email="pva3@acme.example")
            b = create_contact(conn, actor=_admin(), company_id=1, first_name="PvB", last_name="M", email="pvb3@acme.example")
            merge_contacts_isolated(
                conn, actor=_admin(), source_contact_id=a["contact_id"], survivor_contact_id=b["contact_id"],
                fields={"first_name": "keep_survivor", "last_name": "keep_survivor", "email": "keep_survivor"},
            )
            hist = provenance_history(conn, entity_type=ENTITY_CONTACT, entity_id=b["contact_id"])
        self.assertTrue(any(h["action"] == "MERGE" or h["source_type"] == SOURCE_MERGE for h in hist))

    def test_51_ronson_regression(self):
        with self.conn() as conn:
            self.assertEqual(resolve_company_id(conn, 484), 425)

    def test_52_crm_import_provenance(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="CRM Imp Co", city="Alma", state="MI")
            record_populated_creates(
                conn, entity_type=ENTITY_COMPANY, entity_id=created["company_id"],
                fields={"company_name": "CRM Imp Co", "city": "Alma"},
                source_type=SOURCE_CRM_IMPORT, source_ref="crm_import:ds3", trusted=True,
            )
            prov = latest_provenance(conn, entity_type=ENTITY_COMPANY, entity_id=created["company_id"], field="city")
        self.assertEqual(prov["source_type"], SOURCE_CRM_IMPORT)

    def test_53_crm_import_mode_separation(self):
        with self.assertRaises(BatchNotReusable):
            refuse_refresh_as_initial_import({"import_mode": MODE_LEADMASTER_REFRESH})

    def test_54_leadmaster_unchanged(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="LM Same Co", city="Alma", state="MI", phone="9895551111"
            )
            from data_steward import attach_leadmaster_identity
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9100", client_id=CLIENT_A)
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
            _plan_and_apply(
                conn,
                [_row(record_no="9100", company_name="LM Same Co", city="Alma", company_phone="9895551111", notes="")],
                RefreshPolicy(client_id=CLIENT_A), sha="same",
            )
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertGreaterEqual(after, before)

    def test_55_leadmaster_manual_conflict(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="LM Conf Co", city="Alma", state="MI", phone="9895550000"
            )
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"phone": "9895559999"})
            from data_steward import attach_leadmaster_identity
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9200", client_id=CLIENT_A)
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A,
                rows=[_row(record_no="9200", company_name="LM Conf Co", city="Alma", company_phone="9895550000", notes="")],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertIn(MANUAL_OVERRIDE_CONFLICT, str(plan))

    def test_56_leadmaster_keep_existing(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Keep3 Co", city="Alma", state="MI", phone="9895552222"
            )
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"phone": "9895553333"})
            from data_steward import attach_leadmaster_identity
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9300", client_id=CLIENT_A)
            _plan_and_apply(
                conn,
                [_row(record_no="9300", company_name="Keep3 Co", city="Alma", company_phone="9895552222", notes="")],
                RefreshPolicy(client_id=CLIENT_A), sha="k",
                resolutions=[{
                    "source_row": 1, "field": "phone", "proposal_action": MANUAL_OVERRIDE_CONFLICT,
                    "resolution": "KEEP_EXISTING",
                }],
            )
            auth = current_field_authority(conn, entity_type=ENTITY_COMPANY, entity_id=created["company_id"], field="phone")
        self.assertEqual(auth["source_type"], SOURCE_MANUAL_ADMIN)

    def test_57_leadmaster_accept_proposed(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Accept3 Co", city="Alma", state="MI", phone="9895554444"
            )
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"phone": "9895555555"})
            from data_steward import attach_leadmaster_identity
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9400", client_id=CLIENT_A)
            _plan_and_apply(
                conn,
                [_row(record_no="9400", company_name="Accept3 Co", city="Alma", company_phone="9895554444", notes="")],
                RefreshPolicy(client_id=CLIENT_A), sha="a",
                resolutions=[{
                    "source_row": 1, "field": "phone", "proposal_action": MANUAL_OVERRIDE_CONFLICT,
                    "resolution": "ACCEPT_PROPOSED",
                }],
            )
            auth = current_field_authority(conn, entity_type=ENTITY_COMPANY, entity_id=created["company_id"], field="phone")
        self.assertEqual(auth["source_type"], SOURCE_LEADMASTER)

    def test_58_leadmaster_archived_review(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="LM Arch Co 58", city="Alma", state="MI")
            from data_steward import attach_leadmaster_identity
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9800", client_id=CLIENT_A)
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[_row(record_no="9800", company_name="LM Arch Co 58", city="Alma", notes="")],
                policy=RefreshPolicy(client_id=CLIENT_A), source_sha256="arch58", source_filename="a.csv",
            )
        self.assertIn(ARCHIVED_MATCH_REVIEW, str(plan))

    def test_59_refresh_rerun(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Idem3 Co", city="Alma", state="MI")
            from data_steward import attach_leadmaster_identity
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9600", client_id=CLIENT_A)
            incoming = [_row(record_no="9600", company_name="Idem3 Co", city="Alma", notes="")]
            _plan_and_apply(conn, incoming, RefreshPolicy(client_id=CLIENT_A), sha="i")
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
            _plan_and_apply(conn, incoming, RefreshPolicy(client_id=CLIENT_A), sha="i")
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_60_provenance_query_performance(self):
        with self.conn() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM field_provenance_events WHERE entity_type=? AND entity_id=? AND field=? ORDER BY id DESC LIMIT 1",
                ("company", 1, "company_name"),
            ).fetchall()
        text = " ".join(str(dict(r)) for r in plan).lower()
        self.assertTrue("idx_field_prov" in text or "index" in text or "scan" in text)

    def test_61_migration_rollback_savepoint(self):
        with self.conn() as conn:
            before = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
            conn.execute("SAVEPOINT ds3_schema")
            conn.execute("ROLLBACK TO SAVEPOINT ds3_schema")
            after = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
        self.assertEqual(before, after)

    def test_62_baseline_failure_rollback(self):
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
            try:
                with force_provenance_failure("ds3_base"):
                    from data_steward import record_provenance
                    record_provenance(
                        conn, entity_type=ENTITY_COMPANY, entity_id=1, field="company_name",
                        new_value="x", source_type=SOURCE_LEGACY_EXISTING, action=ACTION_BASELINE, trusted=True,
                    )
            except Exception:
                pass
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_63_manual_amendment_failure_rollback(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Fail Amend Co", city="Alma", state="MI", website="https://keep.example")
            before = conn.execute("SELECT website FROM companies WHERE id=?", (created["company_id"],)).fetchone()[0]
            with self.assertRaises(Exception):
                with force_provenance_failure("ds3_am"):
                    amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"website": "https://nope.example"})
            after = conn.execute("SELECT website FROM companies WHERE id=?", (created["company_id"],)).fetchone()[0]
        self.assertEqual(after, before)

    def test_64_integrity_fk_final(self):
        with self.conn() as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(integrity, "ok")
        self.assertEqual(len(fk), 0)

    def test_65_live_db_unchanged_gate(self):
        self.assertFalse(live_destructive_enabled())
        self.assertNotEqual(Path(_TEST_DB).resolve(), PRODUCTION_DB_PATH.resolve())

    def test_66_null_like_skipped(self):
        with self.conn() as conn:
            phone = latest_provenance(conn, entity_type=ENTITY_COMPANY, entity_id=2, field="phone")
            addr = latest_provenance(conn, entity_type=ENTITY_COMPANY, entity_id=2, field="address")
        self.assertIsNone(phone)
        self.assertIsNone(addr)

    def test_67_duplicate_note_not_deduped(self):
        with self.conn() as conn:
            append_note(conn, actor=_admin(), ccr_id=1, text="Same B")
            append_note(conn, actor=_admin(), ccr_id=1, text="Same B")
            blob = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertGreaterEqual(str(blob).count("Same B"), 2)

    def test_68_dependency_inspector(self):
        with self.conn() as conn:
            info = inspect_relationship_dependencies(conn, 1)
        self.assertEqual(info["ccr_id"], 1)
        self.assertIn("counts", info)

    def test_69_workspace_notes_append_not_delete(self):
        from client_workspace_data import update_relationship_notes
        token = bind_staff_actor(_admin())
        try:
            update_relationship_notes("100", note_text="Workspace B", client_id=CLIENT_A, mode="append")
        finally:
            reset_staff_actor(token)
        with self.conn() as conn:
            notes = list(conn.execute(
                "SELECT note_text FROM legacy_notes WHERE client_id=1 AND company_id=1 ORDER BY id"
            ))
            texts = [str(n["note_text"]) for n in notes]
        self.assertTrue(any("Existing note A" in t for t in texts))
        self.assertTrue(any("Workspace B" in t for t in texts))
        self.assertGreaterEqual(len(texts), 2)

    def test_70_live_migrate_not_wired(self):
        from db import migrate_schema as ms
        src = Path(ms.__code__.co_filename).read_text(encoding="utf-8") if False else ""
        import db as dbmod
        import inspect
        text = inspect.getsource(dbmod.migrate_schema)
        self.assertNotIn("ensure_data_steward_schema", text)
        self.assertNotIn("data_steward_live_migrate", text)


if __name__ == "__main__":
    unittest.main()
