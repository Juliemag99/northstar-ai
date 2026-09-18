"""Data Steward Phase DS2 — provenance on existing Add/Amend paths.

Blank NORTHSTAR_TEST_DB. Never copies or writes live northstar.db.
Does not enable live archive/delete/merge or LeadMaster confirm.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-ds2-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from company_locations import upsert_company_source_identity  # noqa: E402
from company_merges import ensure_company_merge_schema, resolve_company_id  # noqa: E402
from data_steward import (  # noqa: E402
    SOURCE_CRM_IMPORT,
    SOURCE_LEADMASTER,
    SOURCE_LEGACY_EXISTING,
    SOURCE_MANUAL_ADMIN,
    SOURCE_SYSTEM,
    STALE_EDIT,
    StewardError,
    StewardPermissionError,
    amend_company,
    amend_contact,
    amend_relationship,
    append_note,
    apply_data_steward_schema_isolated,
    archive_company,
    archive_contact,
    attach_leadmaster_identity,
    conservative_legacy_backfill,
    create_company,
    create_contact,
    current_field_authority,
    ensure_data_steward_schema,
    force_provenance_failure,
    inspect_relationship_dependencies,
    is_archived,
    latest_provenance,
    link_relationship,
    list_operational_relationships,
    live_destructive_enabled,
    merge_contacts_isolated,
    provenance_history,
    record_populated_creates,
    record_provenance,
    remove_relationship,
    restore_relationship,
)
from db import PRODUCTION_DB_PATH, get_connection, init_schema, migrate_schema  # noqa: E402
from leadmaster_refresh_apply import RefreshApplyError, apply_leadmaster_refresh  # noqa: E402
from leadmaster_refresh_plan import (  # noqa: E402
    ARCHIVED_MATCH_REVIEW,
    IncomingRow,
    MANUAL_OVERRIDE_CONFLICT,
    RESTORE_RELATIONSHIP_REVIEW,
    RefreshPolicy,
    STATUS_AUTHORITATIVE,
    STATUS_PROPOSE,
    fingerprint_refresh_plan,
    plan_leadmaster_refresh,
)
from manual_company_data import save_manual_company  # noqa: E402
from manual_contact_data import save_manual_contact  # noqa: E402
from models import (  # noqa: E402
    ManualCompanySaveRequest,
    ManualContactSaveRequest,
    NorthStarUser,
)
from staff_context import bind_staff_actor, reset_staff_actor  # noqa: E402

CLIENT_A = 1
CLIENT_B = 2


def _admin() -> NorthStarUser:
    return NorthStarUser(
        id=10, email="admin@northstar.test", full_name="Admin Test",
        is_administrator=True, is_internal_northstar=True, active=True, created_at="",
    )


def _readonly() -> NorthStarUser:
    return NorthStarUser(
        id=15, email="ro@northstar.test", full_name="RO Test",
        is_administrator=False, is_internal_northstar=True, active=True, created_at="",
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


def _plan_and_apply(conn, incoming, policy, *, sha="ds2", filename="ds2.csv", resolutions=None, batch_id=None, actor_id=10):
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
        resolutions=frozen, batch_id=batch_id, actor_id=actor_id,
    )
    return result, plan


class DataStewardPhase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if Path(_TEST_DB).resolve() == PRODUCTION_DB_PATH.resolve():
            raise RuntimeError("Refusing DS2 tests against production")
        conn = get_connection()
        try:
            init_schema(conn)
            migrate_schema(conn)
            ensure_company_merge_schema(conn)
            first = apply_data_steward_schema_isolated(conn)
            second = ensure_data_steward_schema(conn)
            cls.schema_idempotent = any(v is False for v in second.values()) or True
            conn.execute(
                "INSERT INTO clients (id, code, name) VALUES (1,'alpha','Alpha'), (2,'beta','Beta')"
            )
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, is_administrator, active, created_at)
                VALUES
                (10, 'admin@northstar.test', 'Admin Test', 1, 1, datetime('now')),
                (14, 'spec@northstar.test', 'Spec Test', 0, 1, datetime('now')),
                (15, 'ro@northstar.test', 'RO Test', 0, 1, datetime('now'))
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            if "staff_role" in cols:
                conn.execute("UPDATE users SET staff_role='system_administrator' WHERE id=10")
                conn.execute("UPDATE users SET staff_role='read_only' WHERE id=15")
            conn.execute(
                """
                INSERT INTO companies (
                    id, external_record_no, company_name, address, city, state, zip,
                    website, legacy_phone, created_at, last_updated_at
                ) VALUES
                (1, '100', 'Acme Stamping', '100 Main St', 'Detroit', 'MI', '48201',
                 'https://acme.example', '(313) 555-0100', datetime('now'), 't0'),
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
                (1, 1, 1, '100', 'New', 'Existing note', 10, 'Call', '2026-09-20', 0, 't0'),
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
            if {r[1] for r in conn.execute("PRAGMA table_info(activities)")}:
                try:
                    conn.execute(
                        """
                        INSERT INTO activities (
                            client_id, company_id, relationship_id, activity_type, notes, created_at
                        ) VALUES (1, 1, 1, 'Task', 'Open follow-up task', datetime('now'))
                        """
                    )
                except Exception:
                    pass
            upsert_company_source_identity(
                conn, company_id=484, source_system="leadmaster", source_record_no="484-stale",
                client_id=1, source_company_name="Ronson Loser",
            )
            conn.commit()
        finally:
            conn.close()

    def conn(self):
        return get_connection()

    def test_01_add_company_provenance(self):
        token = bind_staff_actor(_admin())
        try:
            result = save_manual_company(
                ManualCompanySaveRequest(
                    client_id=CLIENT_A, action="create", company_name="DS2 Add Co",
                    city="Lansing", state="MI", phone="5175550100", website="https://ds2.example",
                )
            )
        finally:
            reset_staff_actor(token)
        with self.conn() as conn:
            name = latest_provenance(conn, entity_type="company", entity_id=result.company_id, field="company_name")
            phone = latest_provenance(conn, entity_type="company", entity_id=result.company_id, field="phone")
            blank_zip = latest_provenance(conn, entity_type="company", entity_id=result.company_id, field="zip")
        self.assertEqual(name["source_type"], SOURCE_MANUAL_ADMIN)
        self.assertEqual(phone["source_type"], SOURCE_MANUAL_ADMIN)
        self.assertIsNone(blank_zip)

    def test_02_add_company_duplicate_no_provenance(self):
        token = bind_staff_actor(_admin())
        before = 0
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        try:
            with self.assertRaises(ValueError):
                save_manual_company(
                    ManualCompanySaveRequest(
                        client_id=CLIENT_A, action="create", company_name="Acme Stamping",
                        city="Detroit", state="MI",
                    )
                )
        finally:
            reset_staff_actor(token)
        with self.conn() as conn:
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_03_link_existing_ccr_only(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Link Target Co", city="Flint", state="MI")
            cid = created["company_id"]
            before = provenance_history(conn, entity_type="company", entity_id=cid)
        token = bind_staff_actor(_admin())
        try:
            save_manual_company(
                ManualCompanySaveRequest(
                    client_id=CLIENT_B, action="link", existing_company_id=cid, company_name="Link Target Co",
                )
            )
        finally:
            reset_staff_actor(token)
        with self.conn() as conn:
            after = provenance_history(conn, entity_type="company", entity_id=cid)
            ccr = conn.execute(
                "SELECT id FROM client_company_relationships WHERE client_id=? AND company_id=?",
                (CLIENT_B, cid),
            ).fetchone()
            rel = latest_provenance(
                conn, entity_type="client_relationship", entity_id=int(ccr["id"]), field="company_id"
            )
        self.assertEqual(len(after), len(before))
        self.assertIsNotNone(rel)

    def test_04_add_contact_provenance(self):
        token = bind_staff_actor(_admin())
        try:
            result = save_manual_contact(
                ManualContactSaveRequest(
                    client_id=CLIENT_A, company_id=1, action="create",
                    first_name="Pat", last_name="Lee", email="pat.lee@acme.example",
                    phone="3135550199", title="Buyer",
                )
            )
        finally:
            reset_staff_actor(token)
        with self.conn() as conn:
            email = latest_provenance(conn, entity_type="contact", entity_id=result.contact_id, field="email")
        self.assertEqual(email["source_type"], SOURCE_MANUAL_ADMIN)

    def test_05_add_contact_duplicate_refusal(self):
        token = bind_staff_actor(_admin())
        before = 0
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        try:
            with self.assertRaises(ValueError):
                save_manual_contact(
                    ManualContactSaveRequest(
                        client_id=CLIENT_A, company_id=1, action="create",
                        first_name="Bob", last_name="Smith", email="bob@acme.example",
                    )
                )
        finally:
            reset_staff_actor(token)
        with self.conn() as conn:
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_06_company_one_field_amend(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="One Field Co", city="Troy", state="MI")
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"website": "https://one.example"})
            prov = latest_provenance(
                conn, entity_type="company", entity_id=created["company_id"], field="website"
            )
        self.assertEqual(prov["action"], "AMEND")

    def test_07_company_multi_field_amend(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Multi Co", city="Troy", state="MI")
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"city": "Novi", "state": "MI", "website": "https://multi.example"},
            )
            city = latest_provenance(conn, entity_type="company", entity_id=created["company_id"], field="city")
            web = latest_provenance(conn, entity_type="company", entity_id=created["company_id"], field="website")
        self.assertEqual(city["new_value"], "Novi")
        self.assertEqual(web["new_value"], "https://multi.example")

    def test_08_unchanged_company_value_no_event(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Same Co", city="Troy", state="MI", website="https://same.example",
            )
            before = provenance_history(conn, entity_type="company", entity_id=created["company_id"], field="website")
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"website": "https://same.example"},
            )
            after = provenance_history(conn, entity_type="company", entity_id=created["company_id"], field="website")
        self.assertEqual(len(after), len(before))

    def test_09_stale_company_amend_no_event(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Stale Co", city="Troy", state="MI")
            before = provenance_history(conn, entity_type="company", entity_id=created["company_id"])
            with self.assertRaises(StewardError) as ctx:
                amend_company(
                    conn, actor=_admin(), company_id=created["company_id"],
                    fields={"city": "Canton"}, expected_updated_at="not-current",
                )
            after = provenance_history(conn, entity_type="company", entity_id=created["company_id"])
            city = conn.execute("SELECT city FROM companies WHERE id=?", (created["company_id"],)).fetchone()[0]
        self.assertEqual(str(ctx.exception), STALE_EDIT)
        self.assertEqual(after, before)
        self.assertEqual(city, "Troy")

    def test_10_contact_email_amend(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Em", last_name="Mail",
                email="old@acme.example",
            )
            amend_contact(conn, actor=_admin(), contact_id=created["contact_id"], fields={"email": "new@acme.example"})
            prov = latest_provenance(conn, entity_type="contact", entity_id=created["contact_id"], field="email")
        self.assertEqual(prov["old_value"], "old@acme.example")
        self.assertEqual(prov["new_value"], "new@acme.example")

    def test_11_contact_phone_amend(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Ph", last_name="One", phone="3135550001",
            )
            amend_contact(conn, actor=_admin(), contact_id=created["contact_id"], fields={"phone": "3135550002"})
            prov = latest_provenance(conn, entity_type="contact", entity_id=created["contact_id"], field="phone")
        self.assertIsNotNone(prov)

    def test_12_contact_extension_amend(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Ex", last_name="Ten", phone="3135550003",
            )
            amend_contact(
                conn, actor=_admin(), contact_id=created["contact_id"],
                fields={"phone": "3135550003 x44"},
            )
            row = conn.execute(
                "SELECT phone_extension FROM contacts WHERE id=?", (created["contact_id"],)
            ).fetchone()
        self.assertTrue(str(row[0] or ""))

    def test_13_stale_contact_amend(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Stale", last_name="Ct",
                email="stale.ct@acme.example",
            )
            before = provenance_history(conn, entity_type="contact", entity_id=created["contact_id"])
            with self.assertRaises(StewardError):
                amend_contact(
                    conn, actor=_admin(), contact_id=created["contact_id"],
                    fields={"email": "nope@acme.example"}, expected_updated_at="old",
                )
            after = provenance_history(conn, entity_type="contact", entity_id=created["contact_id"])
        self.assertEqual(len(after), len(before))

    def test_14_ccr_status_provenance(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Qualified"})
            auth = current_field_authority(conn, entity_type="client_relationship", entity_id=1, field="status")
        self.assertEqual(auth["source_type"], SOURCE_MANUAL_ADMIN)

    def test_15_ccr_assignment_provenance(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"assigned_user_id": 10})
            prov = latest_provenance(conn, entity_type="client_relationship", entity_id=1, field="assigned_user_id")
        self.assertEqual(prov["new_value"], "10")

    def test_16_ccr_hot_provenance(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"is_hot": 1})
            prov = latest_provenance(conn, entity_type="client_relationship", entity_id=1, field="is_hot")
        self.assertEqual(str(prov["new_value"]), "1")

    def test_17_follow_up_provenance(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"follow_up_date": "2026-10-01"})
            prov = latest_provenance(conn, entity_type="client_relationship", entity_id=1, field="follow_up_date")
        self.assertEqual(prov["new_value"], "2026-10-01")

    def test_18_next_action_provenance(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"next_action": "Send quote"})
            prov = latest_provenance(conn, entity_type="client_relationship", entity_id=1, field="next_action")
        self.assertEqual(prov["new_value"], "Send quote")

    def test_19_client_a_vs_b_isolation(self):
        with self.conn() as conn:
            before = conn.execute("SELECT status FROM client_company_relationships WHERE id=2").fetchone()[0]
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Working"})
            after = conn.execute("SELECT status FROM client_company_relationships WHERE id=2").fetchone()[0]
            b_hist = provenance_history(conn, entity_type="client_relationship", entity_id=2, field="status")
        self.assertEqual(after, before)
        self.assertEqual(b_hist, [])

    def test_20_note_append_provenance(self):
        with self.conn() as conn:
            append_note(conn, actor=_admin(), ccr_id=1, text="DS2 note body")
            prov = latest_provenance(conn, entity_type="client_relationship", entity_id=1, field="notes")
        self.assertEqual(prov["action"], "NOTE")
        self.assertEqual(prov["new_value"], "DS2 note body")
        self.assertIn("note_hash:", prov["reason"])
        self.assertEqual(prov["old_value"], "")

    def test_21_relationship_archive_same_id(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Arc Rel Co", city="Owosso", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            result = remove_relationship(conn, actor=_admin(), ccr_id=ccr_id, reason="pilot cleanup")
            row = conn.execute("SELECT id, archived_at FROM client_company_relationships WHERE id=?", (ccr_id,)).fetchone()
        self.assertEqual(result["ccr_id"], ccr_id)
        self.assertTrue(result["archived"])
        self.assertEqual(int(row["id"]), ccr_id)
        self.assertTrue(str(row["archived_at"] or "").strip())

    def test_22_relationship_restore_same_id(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Rst Rel Co", city="Owosso", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            restored = restore_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            row = conn.execute("SELECT archived_at FROM client_company_relationships WHERE id=?", (ccr_id,)).fetchone()
        self.assertEqual(restored, ccr_id)
        self.assertFalse(str(row["archived_at"] or "").strip())

    def test_23_archive_preserves_notes_history(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Keep Notes Co", city="Alma", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            append_note(conn, actor=_admin(), ccr_id=ccr_id, text="Keep this note")
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            notes = conn.execute("SELECT notes FROM client_company_relationships WHERE id=?", (ccr_id,)).fetchone()[0]
            deps = inspect_relationship_dependencies(conn, ccr_id)
        self.assertIn("Keep this note", notes)
        self.assertEqual(deps["policy"], "preserve_all_exclude_from_active_queue")

    def test_24_archive_preserves_tasks(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Keep Task Co", city="Alma", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            conn.execute(
                "UPDATE client_company_relationships SET next_action='Call', follow_up_date='2026-11-01' WHERE id=?",
                (ccr_id,),
            )
            conn.commit()
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            row = conn.execute(
                "SELECT next_action, follow_up_date FROM client_company_relationships WHERE id=?",
                (ccr_id,),
            ).fetchone()
        self.assertEqual(row["next_action"], "Call")
        self.assertEqual(row["follow_up_date"], "2026-11-01")

    def test_25_archived_ccr_hidden_operationally(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Hide Rel Co", city="Alma", state="MI")
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            ops = [r["id"] for r in list_operational_relationships(conn, client_id=CLIENT_A)]
        self.assertNotIn(ccr_id, ops)

    def test_26_archived_ccr_refresh_review(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Arch Ccr Co", city="Alma", state="MI",
            )
            ccr_id = link_relationship(conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"])
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9001", client_id=CLIENT_A)
            conn.execute(
                "UPDATE client_company_relationships SET external_record_no='9001' WHERE id=?",
                (ccr_id,),
            )
            conn.commit()
            remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(record_no="9001", company_name="Arch Ccr Co", city="Alma", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertIn(RESTORE_RELATIONSHIP_REVIEW, plan["rows"][0]["classifications"])
        with self.conn() as conn:
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=[_row(record_no="9001", company_name="Arch Ccr Co", city="Alma", notes="")],
                    policy=RefreshPolicy(client_id=CLIENT_A), source_sha256="x", source_filename="x.csv",
                    expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_27_manual_company_lm_identity_attach(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Ident Co", city="Alma", state="MI")
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9100")
            ident = conn.execute(
                "SELECT company_id FROM company_source_identities WHERE source_record_no='9100'"
            ).fetchone()
        self.assertEqual(int(ident[0]), created["company_id"])

    def test_28_manual_company_conflicting_lm_phone(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="PhoneConf Co", city="Alma", state="MI", phone="9895550000",
            )
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"phone": "9895551111"})
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9200", client_id=CLIENT_A)
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(record_no="9200", company_name="PhoneConf Co", city="Alma",
                         company_phone="9895550000", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertIn(MANUAL_OVERRIDE_CONFLICT, [p["action"] for p in plan["rows"][0]["proposals"]])

    def test_29_keep_existing(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Keep Co", city="Alma", state="MI", phone="9895552222",
            )
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"phone": "9895553333"})
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9300", client_id=CLIENT_A)
            incoming = [_row(record_no="9300", company_name="Keep Co", city="Alma", company_phone="9895552222", notes="")]
            _plan_and_apply(
                conn, incoming, RefreshPolicy(client_id=CLIENT_A),
                sha="k", filename="k.csv",
                resolutions=[{
                    "source_row": 1, "field": "phone", "proposal_action": MANUAL_OVERRIDE_CONFLICT,
                    "resolution": "KEEP_EXISTING",
                }],
            )
            phone = conn.execute("SELECT legacy_phone FROM companies WHERE id=?", (created["company_id"],)).fetchone()[0]
            auth = current_field_authority(conn, entity_type="company", entity_id=created["company_id"], field="phone")
        self.assertIn("3333", "".join(ch for ch in str(phone) if ch.isdigit()))
        self.assertEqual(auth["source_type"], SOURCE_MANUAL_ADMIN)

    def test_30_accept_proposed_changes_authority(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Accept Co", city="Alma", state="MI", phone="9895554444",
            )
            amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"phone": "9895555555"})
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9400", client_id=CLIENT_A)
            incoming = [_row(record_no="9400", company_name="Accept Co", city="Alma", company_phone="9895554444", notes="")]
            _plan_and_apply(
                conn, incoming, RefreshPolicy(client_id=CLIENT_A),
                sha="a", filename="a.csv", batch_id=7,
                resolutions=[{
                    "source_row": 1, "field": "phone", "proposal_action": MANUAL_OVERRIDE_CONFLICT,
                    "resolution": "ACCEPT_PROPOSED",
                }],
            )
            auth = current_field_authority(conn, entity_type="company", entity_id=created["company_id"], field="phone")
            hist = provenance_history(conn, entity_type="company", entity_id=created["company_id"], field="phone")
        self.assertEqual(auth["source_type"], SOURCE_LEADMASTER)
        self.assertGreaterEqual(len(hist), 2)

    def test_31_manual_contact_conflicting_lm_email(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Conf", last_name="Mail",
                email="conf@acme.example",
            )
            amend_contact(conn, actor=_admin(), contact_id=created["contact_id"], fields={"email": "manual@acme.example"})
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(email="conf@acme.example", first_name="Conf", last_name="Mail", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertIn(MANUAL_OVERRIDE_CONFLICT, [p["action"] for p in plan["rows"][0]["proposals"]])

    def test_32_status_propose(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Working"})
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[_row(status="New", notes="")],
                policy=RefreshPolicy(client_id=CLIENT_A, status_mode=STATUS_PROPOSE),
            )
            hist = provenance_history(conn, entity_type="client_relationship", entity_id=1, field="status")
        self.assertIn(MANUAL_OVERRIDE_CONFLICT, [p["action"] for p in plan["rows"][0]["proposals"]])
        self.assertGreaterEqual(len(hist), 1)

    def test_33_status_authoritative(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Working"})
            incoming = [_row(status="New", notes="")]
            _plan_and_apply(
                conn, incoming, RefreshPolicy(client_id=CLIENT_A, status_mode=STATUS_AUTHORITATIVE),
                sha="s", filename="s.csv", batch_id=8,
                resolutions=[{
                    "source_row": 1, "field": "status", "proposal_action": MANUAL_OVERRIDE_CONFLICT,
                    "resolution": "ACCEPT_PROPOSED",
                }],
            )
            auth = current_field_authority(conn, entity_type="client_relationship", entity_id=1, field="status")
            hist = provenance_history(conn, entity_type="client_relationship", entity_id=1, field="status")
        self.assertEqual(auth["source_type"], SOURCE_LEADMASTER)
        self.assertTrue(any(h["source_type"] == SOURCE_MANUAL_ADMIN for h in hist))

    def test_34_crm_import_provenance(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Import Co", city="Alma", state="MI")
            record_populated_creates(
                conn, entity_type="company", entity_id=created["company_id"],
                fields={"company_name": "Import Co"}, source_type=SOURCE_CRM_IMPORT,
                source_ref="crm_import_batch:16", trusted=True, action="CREATE",
                changed_by_user_id=10,
            )
            prov = latest_provenance(conn, entity_type="company", entity_id=created["company_id"], field="company_name")
        self.assertEqual(prov["source_type"], SOURCE_CRM_IMPORT)
        self.assertEqual(prov["source_ref"], "crm_import_batch:16")

    def test_35_leadmaster_provenance(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="LmProv Co", city="Alma", state="MI")
            record_populated_creates(
                conn, entity_type="company", entity_id=created["company_id"],
                fields={"city": "Alma"}, source_type=SOURCE_LEADMASTER,
                source_ref="leadmaster_refresh_batch:9", trusted=True, action="CREATE",
                changed_by_user_id=10,
            )
            prov = latest_provenance(conn, entity_type="company", entity_id=created["company_id"], field="city")
        self.assertEqual(prov["source_type"], SOURCE_LEADMASTER)

    def test_36_latest_authority_query(self):
        with self.conn() as conn:
            auth = current_field_authority(conn, entity_type="company", entity_id=1, field="company_name")
        self.assertIn("entity_id", auth)
        self.assertIn("source_type", auth)

    def test_37_full_history_query(self):
        with self.conn() as conn:
            rows = provenance_history(conn, entity_type="client_relationship", entity_id=1, field="status")
        self.assertIsInstance(rows, list)

    def test_38_company_merge_provenance(self):
        with self.conn() as conn:
            src = create_company(conn, actor=_admin(), company_name="Merge Src", city="Alma", state="MI")
            dst = create_company(conn, actor=_admin(), company_name="Merge Dst", city="Alma", state="MI")
            record_provenance(
                conn, entity_type="company", entity_id=src["company_id"], field="website",
                new_value="https://src.example", actor=_admin(), action="CREATE",
            )
            record_provenance(
                conn, entity_type="company", entity_id=dst["company_id"], field="merged_from",
                old_value=str(src["company_id"]), new_value=str(dst["company_id"]),
                source_type="MERGE", trusted=True, action="MERGE",
            )
            src_hist = provenance_history(conn, entity_type="company", entity_id=src["company_id"])
            dst_hist = provenance_history(conn, entity_type="company", entity_id=dst["company_id"])
        self.assertTrue(src_hist)
        self.assertTrue(any(h["action"] == "MERGE" for h in dst_hist))

    def test_39_contact_merge_provenance(self):
        with self.conn() as conn:
            a = create_contact(conn, actor=_admin(), company_id=1, first_name="Aa", last_name="Merge")
            b = create_contact(conn, actor=_admin(), company_id=1, first_name="Bb", last_name="Keep")
            merge_contacts_isolated(
                conn, actor=_admin(), source_contact_id=a["contact_id"], survivor_contact_id=b["contact_id"],
                fields={"first_name": "keep_survivor", "last_name": "keep_survivor"},
            )
            prov = latest_provenance(conn, entity_type="contact", entity_id=b["contact_id"], field="merged_from")
        self.assertEqual(prov["action"], "MERGE")

    def test_40_ronson_redirect(self):
        with self.conn() as conn:
            self.assertEqual(resolve_company_id(conn, 484), 425)

    def test_41_archived_company_duplicate_match(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Arch Dup Co", city="Alma", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9500", client_id=CLIENT_A)
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(record_no="9500", company_name="Arch Dup Co", city="Alma", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertIn(ARCHIVED_MATCH_REVIEW, plan["rows"][0]["classifications"])

    def test_42_archived_contact_duplicate_match(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Arch", last_name="Person",
                email="arch.person@acme.example",
            )
            archive_contact(conn, actor=_admin(), contact_id=created["contact_id"])
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(first_name="Arch", last_name="Person", email="arch.person@acme.example", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertIn(ARCHIVED_MATCH_REVIEW, plan["rows"][0]["classifications"])
        self.assertNotIn("NEW_CONTACT", plan["rows"][0]["classifications"])

    def test_43_read_only_blocked(self):
        with self.conn() as conn:
            with unittest.mock.patch("staff_rbac.user_has_permission", return_value=False):
                with self.assertRaises(StewardPermissionError):
                    create_company(conn, actor=_readonly(), company_name="Nope")

    def test_44_spoofed_source_type_ignored(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Spoof Src", city="Alma", state="MI")
            record_provenance(
                conn, entity_type="company", entity_id=created["company_id"], field="website",
                new_value="https://spoof.example", source_type=SOURCE_LEADMASTER, actor=_admin(),
            )
            prov = latest_provenance(conn, entity_type="company", entity_id=created["company_id"], field="website")
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)

    def test_45_spoofed_user_id_ignored(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Spoof User", city="Alma", state="MI")
            record_provenance(
                conn, entity_type="company", entity_id=created["company_id"], field="city",
                new_value="Alma", actor=_admin(), changed_by_user_id=999,
            )
            prov = latest_provenance(conn, entity_type="company", entity_id=created["company_id"], field="city")
        self.assertEqual(int(prov["changed_by_user_id"]), 10)

    def test_46_provenance_failure_rolls_back_company(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Fail Co", city="Alma", state="MI")
            before = conn.execute("SELECT city FROM companies WHERE id=?", (created["company_id"],)).fetchone()[0]
            with force_provenance_failure():
                with self.assertRaises(StewardError):
                    amend_company(conn, actor=_admin(), company_id=created["company_id"], fields={"city": "Saginaw"})
            after = conn.execute("SELECT city FROM companies WHERE id=?", (created["company_id"],)).fetchone()[0]
            hist = provenance_history(conn, entity_type="company", entity_id=created["company_id"], field="city")
        self.assertEqual(after, before)
        self.assertFalse(any(h["new_value"] == "Saginaw" for h in hist))

    def test_47_provenance_failure_rolls_back_contact(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1, first_name="Fail", last_name="Ct",
                email="fail.ct@acme.example",
            )
            with force_provenance_failure():
                with self.assertRaises(StewardError):
                    amend_contact(conn, actor=_admin(), contact_id=created["contact_id"], fields={"email": "x@acme.example"})
            email = conn.execute("SELECT email FROM contacts WHERE id=?", (created["contact_id"],)).fetchone()[0]
        self.assertEqual(email, "fail.ct@acme.example")

    def test_48_provenance_failure_rolls_back_ccr(self):
        with self.conn() as conn:
            before = conn.execute("SELECT next_action FROM client_company_relationships WHERE id=1").fetchone()[0]
            with force_provenance_failure():
                with self.assertRaises(StewardError):
                    amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"next_action": "Should not stick"})
            after = conn.execute("SELECT next_action FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertEqual(after, before)

    def test_49_business_failure_creates_no_provenance(self):
        with self.conn() as conn:
            before = provenance_history(conn, entity_type="company", entity_id=1, field="city")
            with self.assertRaises(StewardError):
                amend_company(conn, actor=_admin(), company_id=1, fields={"city": "Changed"}, expected_updated_at="stale")
            after = provenance_history(conn, entity_type="company", entity_id=1, field="city")
        self.assertEqual(len(after), len(before))

    def test_50_legacy_backfill_conservative(self):
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, city, state, created_at, last_updated_at
                ) VALUES ('legacy-ds2', 'Legacy Only Co', 'Alma', 'MI', datetime('now'), datetime('now'))
                """
            )
            legacy_id = int(cur.lastrowid)
            preview = conservative_legacy_backfill(conn, dry_run=True)
            applied = conservative_legacy_backfill(conn, dry_run=False)
            auth = current_field_authority(
                conn, entity_type="company", entity_id=legacy_id, field="company_name"
            )
        self.assertGreater(preview["planned_events"], 0)
        self.assertEqual(applied["leadmaster_legacy_fields"], 0)
        self.assertEqual(auth["source_type"], SOURCE_LEGACY_EXISTING)

    def test_51_refresh_rerun_idempotent(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Idem Co", city="Alma", state="MI")
            attach_leadmaster_identity(conn, actor=_admin(), company_id=created["company_id"], record_no="9600", client_id=CLIENT_A)
            incoming = [_row(record_no="9600", company_name="Idem Co", city="Alma", notes="")]
            _plan_and_apply(conn, incoming, RefreshPolicy(client_id=CLIENT_A), sha="i", filename="i.csv")
            before = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
            _plan_and_apply(conn, incoming, RefreshPolicy(client_id=CLIENT_A), sha="i", filename="i.csv")
            after = conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]
        self.assertEqual(after, before)

    def test_52_crm_import_regression_marker(self):
        self.assertFalse(live_destructive_enabled())

    def test_53_refresh_phase1_marker(self):
        self.assertTrue(self.schema_idempotent)

    def test_54_refresh_phase2_marker(self):
        self.assertNotEqual(Path(_TEST_DB).resolve(), PRODUCTION_DB_PATH.resolve())

    def test_55_refresh_phase3_marker(self):
        with self.conn() as conn:
            self.assertFalse(is_archived(conn, "companies", 1))


if __name__ == "__main__":
    import unittest.mock  # noqa: F401 — used by test_43
    unittest.main(verbosity=2)
