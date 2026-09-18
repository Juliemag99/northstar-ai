"""Data Steward Phase DS1 — isolated maintenance and provenance proof.

Blank NORTHSTAR_TEST_DB. Never copies or writes live northstar.db.
Does not refresh Carmeco, Brown, or Dawson. Does not enable live archive/delete.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-ds1-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from company_locations import upsert_company_source_identity  # noqa: E402
from company_merges import ensure_company_merge_schema, resolve_company_id  # noqa: E402
from data_steward import (  # noqa: E402
    ARCHIVE_ONLY,
    BLOCKED,
    CAP_ARCHIVE,
    DELETE_ALLOWED,
    SOURCE_MANUAL_ADMIN,
    STALE_EDIT,
    StewardError,
    StewardLiveWriteError,
    StewardPermissionError,
    amend_company,
    amend_contact,
    amend_location,
    amend_relationship,
    append_note,
    archive_company,
    archive_contact,
    archive_location,
    assert_not_production_db,
    attach_leadmaster_identity,
    audit_history,
    create_company,
    create_contact,
    create_location,
    delete_company_permanent,
    ensure_data_steward_schema,
    find_company_duplicates,
    inspect_company_dependencies,
    is_archived,
    latest_provenance,
    link_relationship,
    list_operational_companies,
    live_destructive_enabled,
    merge_contacts_isolated,
    remove_relationship,
    require_capability,
    restore_company,
    restore_contact,
)
from db import PRODUCTION_DB_PATH, get_connection, init_schema, migrate_schema  # noqa: E402
from leadmaster_refresh_plan import (  # noqa: E402
    ARCHIVED_MATCH_REVIEW,
    IncomingRow,
    MANUAL_OVERRIDE_CONFLICT,
    RefreshPolicy,
    REVIEW_REQUIRED,
    STATUS_AUTHORITATIVE,
    STATUS_PROPOSE,
    plan_leadmaster_refresh,
)
from models import NorthStarUser  # noqa: E402
from staff_rbac import READ_ONLY, REVOPS_SPECIALIST, permissions_for_role  # noqa: E402

CLIENT_A = 1
CLIENT_B = 2


def _admin() -> NorthStarUser:
    return NorthStarUser(
        id=10, email="admin@northstar.test", full_name="Admin Test",
        is_administrator=True, is_internal_northstar=True, active=True, created_at="",
    )


def _specialist() -> NorthStarUser:
    return NorthStarUser(
        id=14, email="spec@northstar.test", full_name="Spec Test",
        is_administrator=False, is_internal_northstar=True, active=True, created_at="",
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


class DataStewardPhase1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if Path(_TEST_DB).resolve() == PRODUCTION_DB_PATH.resolve():
            raise RuntimeError("Refusing DS1 tests against production")
        conn = get_connection()
        try:
            init_schema(conn)
            migrate_schema(conn)
            ensure_company_merge_schema(conn)
            ensure_data_steward_schema(conn)
            conn.execute(
                "INSERT INTO clients (id, code, name) VALUES (1,'alpha','Alpha'), (2,'beta','Beta')"
            )
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, is_administrator, active, created_at)
                VALUES
                (10, 'admin@northstar.test', 'Admin Test', 1, 1, datetime('now')),
                (11, 'acl@northstar.test', 'ACL User', 0, 1, datetime('now')),
                (12, 'noacl@northstar.test', 'No ACL', 0, 1, datetime('now')),
                (14, 'spec@northstar.test', 'Spec Test', 0, 1, datetime('now')),
                (15, 'ro@northstar.test', 'RO Test', 0, 1, datetime('now'))
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            if "staff_role" in cols:
                conn.execute("UPDATE users SET staff_role='revops_specialist' WHERE id=14")
                conn.execute("UPDATE users SET staff_role='read_only' WHERE id=15")
                conn.execute("UPDATE users SET staff_role='system_administrator' WHERE id=10")
            conn.execute(
                """
                INSERT INTO user_client_assignments (user_id, client_id, active)
                VALUES (11, 1, 1), (14, 1, 1)
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
                    id, client_id, company_id, external_record_no, status, notes, assigned_user_id, updated_at
                ) VALUES
                (1, 1, 1, '100', 'New', 'Existing note', 10, 't0'),
                (2, 2, 1, '100', 'Prospect', 'Client B note', 10, 't0')
                """
            )
            conn.execute(
                """
                INSERT INTO company_merge_history (
                    source_company_id, survivor_company_id, merged_at, merged_by_user_id, reason
                ) VALUES (484, 425, datetime('now'), 10, 'test redirect')
                """
            )
            upsert_company_source_identity(
                conn, company_id=484, source_system="leadmaster", source_record_no="484-stale",
                client_id=1, source_company_name="Ronson Loser",
            )
            conn.commit()
        finally:
            conn.close()

    def conn(self):
        return get_connection()

    def test_01_manual_company_create(self):
        with self.conn() as conn:
            result = create_company(
                conn, actor=_admin(), company_name="Steward Co", city="Ann Arbor", state="MI",
            )
        self.assertGreater(result["company_id"], 0)

    def test_02_duplicate_company_create_warning(self):
        with self.conn() as conn:
            with self.assertRaises(StewardError) as ctx:
                create_company(conn, actor=_admin(), company_name="Acme Stamping", city="Detroit", state="MI")
        self.assertEqual(str(ctx.exception), "duplicate_company")

    def test_03_manual_company_name_edit(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Name Edit Co", city="Lansing", state="MI")
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"company_name": "Name Edit Co LLC"}, expected_updated_at=None,
            )
            name = conn.execute(
                "SELECT company_name FROM companies WHERE id=?", (created["company_id"],)
            ).fetchone()[0]
        self.assertEqual(name, "Name Edit Co LLC")

    def test_04_company_phone_edit_format(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Phone Co", city="Flint", state="MI")
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"phone": "3135559999"},
            )
            phone = conn.execute(
                "SELECT legacy_phone FROM companies WHERE id=?", (created["company_id"],)
            ).fetchone()[0]
        self.assertTrue(phone)

    def test_05_company_phone_extension(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Ext Co", city="Saginaw", state="MI")
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"phone": "3135558888", "phone_extension": "123"},
            )
            ext = conn.execute(
                "SELECT legacy_phone_extension FROM companies WHERE id=?",
                (created["company_id"],),
            ).fetchone()[0]
        self.assertEqual(str(ext), "123")

    def test_06_stale_company_edit_refusal(self):
        with self.conn() as conn:
            with self.assertRaises(StewardError) as ctx:
                amend_company(
                    conn, actor=_admin(), company_id=1,
                    fields={"website": "https://stale.example"},
                    expected_updated_at="not-the-token",
                )
        self.assertEqual(str(ctx.exception), STALE_EDIT)

    def test_07_company_archive(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Archive Co", city="Troy", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"], reason="closed")
            self.assertTrue(is_archived(conn, "companies", created["company_id"]))
            self._archived_id = created["company_id"]

    def test_08_archived_company_hidden_operationally(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Hidden Co", city="Novi", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            visible = {int(r["id"]) for r in list_operational_companies(conn)}
        self.assertNotIn(created["company_id"], visible)

    def test_09_archived_company_found_by_duplicate_check(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="DupArch Co", city="Canton", state="MI")
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            hits = find_company_duplicates(conn, company_name="DupArch Co", city="Canton", state="MI")
        self.assertTrue(any(h["archived"] for h in hits))

    def test_10_company_restore_same_id(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Restore Co", city="Livonia", state="MI")
            cid = created["company_id"]
            archive_company(conn, actor=_admin(), company_id=cid)
            restored = restore_company(conn, actor=_admin(), company_id=cid)
            self.assertEqual(restored, cid)
            self.assertFalse(is_archived(conn, "companies", cid))

    def test_11_company_delete_dependency_blocked(self):
        with self.conn() as conn:
            gate = inspect_company_dependencies(conn, 1)
            self.assertIn(gate["classification"], {ARCHIVE_ONLY, BLOCKED, "MERGE_RECOMMENDED"})
            with self.assertRaises(StewardError):
                delete_company_permanent(conn, actor=_admin(), company_id=1)

    def test_12_company_delete_allowed_orphan(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Orphan Co", city="Monroe", state="MI")
            gate = inspect_company_dependencies(conn, created["company_id"])
            self.assertEqual(gate["classification"], DELETE_ALLOWED)
            delete_company_permanent(conn, actor=_admin(), company_id=created["company_id"])
            gone = conn.execute(
                "SELECT 1 FROM companies WHERE id=?", (created["company_id"],)
            ).fetchone()
        self.assertIsNone(gone)

    def test_13_location_create(self):
        with self.conn() as conn:
            loc_id = create_location(
                conn, actor=_admin(), company_id=1, address="2 Plant Rd",
                city="Detroit", state="MI", location_type="plant",
            )
        self.assertGreater(loc_id, 0)

    def test_14_location_edit(self):
        with self.conn() as conn:
            loc_id = create_location(
                conn, actor=_admin(), company_id=1, address="3 Plant Rd", city="Detroit", state="MI"
            )
            amend_location(conn, actor=_admin(), location_id=loc_id, fields={"address": "3 Plant Road"})
            addr = conn.execute("SELECT address FROM company_locations WHERE id=?", (loc_id,)).fetchone()[0]
        self.assertEqual(addr, "3 Plant Road")

    def test_15_location_archive(self):
        with self.conn() as conn:
            loc_id = create_location(
                conn, actor=_admin(), company_id=1, address="4 Plant Rd", city="Detroit", state="MI"
            )
            archive_location(conn, actor=_admin(), location_id=loc_id)
            self.assertTrue(is_archived(conn, "company_locations", loc_id))

    def test_16_contact_create(self):
        with self.conn() as conn:
            result = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="New", last_name="Person", email="new@acme.example",
            )
        self.assertGreater(result["contact_id"], 0)

    def test_17_contact_duplicate_warning(self):
        with self.conn() as conn:
            with self.assertRaises(StewardError) as ctx:
                create_contact(
                    conn, actor=_admin(), company_id=1,
                    first_name="Bob", last_name="Smith", email="bob@acme.example",
                )
        self.assertEqual(str(ctx.exception), "duplicate_contact")

    def test_18_contact_email_edit(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Email", last_name="Edit", email="email.edit@acme.example",
            )
            amend_contact(
                conn, actor=_admin(), contact_id=created["contact_id"],
                fields={"email": "email.edit2@acme.example"},
            )
            email = conn.execute(
                "SELECT email FROM contacts WHERE id=?", (created["contact_id"],)
            ).fetchone()[0]
        self.assertEqual(email, "email.edit2@acme.example")

    def test_19_contact_phone_edit(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Ph", last_name="One", email="ph.one@acme.example",
            )
            amend_contact(
                conn, actor=_admin(), contact_id=created["contact_id"],
                fields={"phone": "3135557777"},
            )
            phone = conn.execute(
                "SELECT phone FROM contacts WHERE id=?", (created["contact_id"],)
            ).fetchone()[0]
        self.assertTrue(phone)

    def test_20_contact_extension(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Ex", last_name="T", email="ex.t@acme.example",
            )
            amend_contact(
                conn, actor=_admin(), contact_id=created["contact_id"],
                fields={"phone": "3135556666", "phone_extension": "44"},
            )
            ext = conn.execute(
                "SELECT phone_extension FROM contacts WHERE id=?", (created["contact_id"],)
            ).fetchone()[0]
        self.assertEqual(str(ext), "44")

    def test_21_stale_contact_edit_refusal(self):
        with self.conn() as conn:
            with self.assertRaises(StewardError) as ctx:
                amend_contact(
                    conn, actor=_admin(), contact_id=1,
                    fields={"title": "Stale"}, expected_updated_at="wrong",
                )
        self.assertEqual(str(ctx.exception), STALE_EDIT)

    def test_22_contact_archive(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Arch", last_name="Ct", email="arch.ct@acme.example",
            )
            archive_contact(conn, actor=_admin(), contact_id=created["contact_id"])
            self.assertTrue(is_archived(conn, "contacts", created["contact_id"]))
            self._contact_arch = created["contact_id"]

    def test_23_contact_restore(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Rst", last_name="Ct", email="rst.ct@acme.example",
            )
            cid = created["contact_id"]
            archive_contact(conn, actor=_admin(), contact_id=cid)
            self.assertEqual(restore_contact(conn, actor=_admin(), contact_id=cid), cid)
            self.assertFalse(is_archived(conn, "contacts", cid))

    def test_24_contact_merge_isolated(self):
        with self.conn() as conn:
            a = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Merge", last_name="A", email="merge.a@acme.example",
            )
            b = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Merge", last_name="B", email="merge.b@acme.example",
            )
            merge_contacts_isolated(
                conn, actor=_admin(),
                source_contact_id=a["contact_id"], survivor_contact_id=b["contact_id"],
                fields={
                    "first_name": "keep_survivor",
                    "last_name": "keep_survivor",
                    "email": "keep_survivor",
                    "title": "keep_survivor",
                },
            )
            self.assertEqual(resolve_company_id(conn, 1), 1)
            gone = conn.execute("SELECT 1 FROM contacts WHERE id=?", (a["contact_id"],)).fetchone()
            self.assertIsNone(gone)
            prov = latest_provenance(
                conn, entity_type="contact", entity_id=b["contact_id"], field="merged_from"
            )
        self.assertIsNotNone(prov)

    def test_25_relationship_add(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Rel Co", city="Jackson", state="MI")
            ccr_id = link_relationship(
                conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"]
            )
        self.assertGreater(ccr_id, 0)

    def test_26_relationship_remove_does_not_delete_company(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Keep Co", city="Battle Creek", state="MI")
            ccr_id = link_relationship(
                conn, actor=_admin(), client_id=CLIENT_A, company_id=created["company_id"]
            )
            result = remove_relationship(conn, actor=_admin(), ccr_id=ccr_id)
            self.assertTrue(result["company_still_exists"])
            self.assertTrue(result["archived"])
            self.assertTrue(result["same_id"])
            row = conn.execute(
                "SELECT archived_at FROM client_company_relationships WHERE id=?",
                (ccr_id,),
            ).fetchone()
        self.assertTrue(str(row["archived_at"] or "").strip())

    def test_27_client_a_edit_does_not_alter_client_b(self):
        with self.conn() as conn:
            before = conn.execute(
                "SELECT status, notes FROM client_company_relationships WHERE id=2"
            ).fetchone()
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Contacted"})
            after = conn.execute(
                "SELECT status, notes FROM client_company_relationships WHERE id=2"
            ).fetchone()
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["notes"], before["notes"])

    def test_28_status_manual_edit(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Qualified"})
            status = conn.execute(
                "SELECT status FROM client_company_relationships WHERE id=1"
            ).fetchone()[0]
            prov = latest_provenance(conn, entity_type="client_relationship", entity_id=1, field="status")
        self.assertEqual(status, "Qualified")
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)

    def test_29_assignment_permission_check(self):
        with self.conn() as conn:
            with self.assertRaises(StewardError):
                amend_relationship(
                    conn, actor=_admin(), ccr_id=1, fields={"assigned_user_id": 12}
                )

    def test_30_note_append(self):
        with self.conn() as conn:
            before = conn.execute(
                "SELECT notes FROM client_company_relationships WHERE id=1"
            ).fetchone()[0]
            append_note(conn, actor=_admin(), ccr_id=1, text="Steward note")
            after = conn.execute(
                "SELECT notes FROM client_company_relationships WHERE id=1"
            ).fetchone()[0]
        self.assertIn("Steward note", after)
        self.assertIn(before, after)

    def test_31_note_provenance(self):
        with self.conn() as conn:
            append_note(conn, actor=_admin(), ccr_id=1, text="Provenance note")
            prov = latest_provenance(conn, entity_type="client_relationship", entity_id=1, field="notes")
        self.assertEqual(prov["action"], "NOTE")
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)

    def test_32_manual_company_field_provenance(self):
        with self.conn() as conn:
            created = create_company(conn, actor=_admin(), company_name="Prov Co", city="Warren", state="MI")
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"phone": "2485551212"}, reason="verified",
            )
            prov = latest_provenance(
                conn, entity_type="company", entity_id=created["company_id"], field="phone"
            )
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)
        self.assertEqual(int(prov["changed_by_user_id"]), 10)

    def test_33_manual_contact_field_provenance(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Prov", last_name="Ct", email="prov.ct@acme.example",
            )
            amend_contact(
                conn, actor=_admin(), contact_id=created["contact_id"],
                fields={"email": "prov.ct2@acme.example"},
            )
            prov = latest_provenance(
                conn, entity_type="contact", entity_id=created["contact_id"], field="email"
            )
        self.assertEqual(prov["source_type"], SOURCE_MANUAL_ADMIN)

    def test_34_refresh_conflict_manual_company_phone(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="LmPhone Co", city="Kalamazoo", state="MI",
                phone="2695550000",
            )
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"phone": "2695551111"},
            )
            rn = conn.execute(
                "SELECT external_record_no FROM companies WHERE id=?", (created["company_id"],)
            ).fetchone()[0]
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(record_no=rn, company_name="LmPhone Co", city="Kalamazoo",
                         company_phone="2695550000", first_name="A", last_name="B",
                         email="a@lmphone.example", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        actions = [p["action"] for p in plan["rows"][0]["proposals"]]
        self.assertIn(MANUAL_OVERRIDE_CONFLICT, actions)

    def test_35_refresh_conflict_manual_contact_email(self):
        with self.conn() as conn:
            created = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="Lm", last_name="Mail", email="lm.mail@acme.example",
            )
            amend_contact(
                conn, actor=_admin(), contact_id=created["contact_id"],
                fields={"email": "lm.mail.fixed@acme.example"},
            )
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(email="lm.mail@acme.example", first_name="Lm", last_name="Mail",
                         contact_phone="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        actions = [p["action"] for p in plan["rows"][0]["proposals"] if p["field"] == "email"]
        self.assertTrue(actions)
        self.assertIn(MANUAL_OVERRIDE_CONFLICT, actions)

    def test_36_refresh_old_company_name_after_manual(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="OldName Co", city="Holland", state="MI"
            )
            amend_company(
                conn, actor=_admin(), company_id=created["company_id"],
                fields={"company_name": "NewName Co"},
            )
            rn = conn.execute(
                "SELECT external_record_no FROM companies WHERE id=?", (created["company_id"],)
            ).fetchone()[0]
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(record_no=rn, company_name="OldName Co", city="Holland",
                         first_name="N", last_name="M", email="nm@newname.example", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        actions = [p["action"] for p in plan["rows"][0]["proposals"] if p["field"] == "company_name"]
        self.assertIn(MANUAL_OVERRIDE_CONFLICT, actions)

    def test_37_propose_status_after_manual_status(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Working"})
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[_row(status="New")],
                policy=RefreshPolicy(client_id=CLIENT_A, status_mode=STATUS_PROPOSE),
            )
        status_props = [p for p in plan["rows"][0]["proposals"] if p["field"] == "status"]
        self.assertTrue(status_props)
        self.assertEqual(status_props[0]["action"], MANUAL_OVERRIDE_CONFLICT)

    def test_38_authoritative_status_after_manual_status(self):
        with self.conn() as conn:
            amend_relationship(conn, actor=_admin(), ccr_id=1, fields={"status": "Working"})
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[_row(status="New")],
                policy=RefreshPolicy(client_id=CLIENT_A, status_mode=STATUS_AUTHORITATIVE),
            )
        status_props = [p for p in plan["rows"][0]["proposals"] if p["field"] == "status"]
        self.assertEqual(status_props[0]["action"], MANUAL_OVERRIDE_CONFLICT)
        self.assertTrue(status_props[0]["blocking"])

    def test_39_archived_entity_refresh_match(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="ArchMatch Co", city="Midland", state="MI"
            )
            rn = conn.execute(
                "SELECT external_record_no FROM companies WHERE id=?", (created["company_id"],)
            ).fetchone()[0]
            archive_company(conn, actor=_admin(), company_id=created["company_id"])
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(record_no=rn, company_name="ArchMatch Co", city="Midland",
                         first_name="X", last_name="Y", email="xy@archmatch.example", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertIn(ARCHIVED_MATCH_REVIEW, plan["rows"][0]["classifications"])
        self.assertNotIn("NEW_COMPANY", plan["rows"][0]["classifications"])

    def test_40_manual_entity_later_receives_leadmaster_identity(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Later Lm Co", city="Portage", state="MI"
            )
            attach_leadmaster_identity(
                conn, actor=_admin(), company_id=created["company_id"],
                record_no="888001", client_id=CLIENT_A,
            )
            plan = plan_leadmaster_refresh(
                conn, client_id=CLIENT_A, rows=[
                    _row(record_no="888001", company_name="Later Lm Co", city="Portage",
                         first_name="L", last_name="M", email="lm@later.example", notes="")
                ],
                policy=RefreshPolicy(client_id=CLIENT_A),
            )
        self.assertEqual(plan["rows"][0]["company_id"], created["company_id"])
        self.assertNotIn("NEW_COMPANY", plan["rows"][0]["classifications"])

    def test_41_merge_preserves_provenance(self):
        with self.conn() as conn:
            a = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="PvA", last_name="M", email="pva@acme.example",
            )
            b = create_contact(
                conn, actor=_admin(), company_id=1,
                first_name="PvB", last_name="M", email="pvb@acme.example",
            )
            amend_contact(
                conn, actor=_admin(), contact_id=b["contact_id"], fields={"title": "Kept"}
            )
            merge_contacts_isolated(
                conn, actor=_admin(),
                source_contact_id=a["contact_id"], survivor_contact_id=b["contact_id"],
                fields={
                    "first_name": "keep_survivor",
                    "last_name": "keep_survivor",
                    "email": "keep_survivor",
                    "title": "keep_survivor",
                },
            )
            history = audit_history(conn, entity_type="contact", entity_id=b["contact_id"])
        self.assertTrue(any(h["action"] == "MERGE" for h in history))
        self.assertTrue(any(h["field"] == "title" for h in history))

    def test_42_restore_preserves_provenance(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="ProvRest Co", city="Okemos", state="MI"
            )
            cid = created["company_id"]
            amend_company(conn, actor=_admin(), company_id=cid, fields={"website": "https://okemos.example"})
            archive_company(conn, actor=_admin(), company_id=cid)
            restore_company(conn, actor=_admin(), company_id=cid)
            history = audit_history(conn, entity_type="company", entity_id=cid)
            website = latest_provenance(conn, entity_type="company", entity_id=cid, field="website")
        self.assertTrue(any(h["action"] == "RESTORE" for h in history))
        self.assertEqual(website["new_value"], "https://okemos.example")

    def test_43_read_only_user_blocked(self):
        self.assertNotIn(CAP_ARCHIVE, permissions_for_role(READ_ONLY))
        with self.conn() as conn:
            with self.assertRaises(StewardPermissionError):
                require_capability(_readonly(), CAP_ARCHIVE, conn=conn)

    def test_44_specialist_destructive_blocked(self):
        self.assertNotIn(CAP_ARCHIVE, permissions_for_role(REVOPS_SPECIALIST))
        with self.conn() as conn:
            with self.assertRaises(StewardPermissionError):
                require_capability(_specialist(), CAP_ARCHIVE, conn=conn)

    def test_45_admin_allowed(self):
        with self.conn() as conn:
            require_capability(_admin(), CAP_ARCHIVE, conn=conn)
            created = create_company(
                conn, actor=_admin(), company_name="Admin Ok Co", city="Ada", state="MI"
            )
        self.assertGreater(created["company_id"], 0)

    def test_46_stale_source_identity_safety(self):
        with self.conn() as conn:
            self.assertEqual(resolve_company_id(conn, 484), 425)
            ident = conn.execute(
                "SELECT company_id FROM company_source_identities WHERE source_record_no='484-stale'"
            ).fetchone()
            walked = resolve_company_id(conn, int(ident["company_id"]))
        self.assertEqual(walked, 425)

    def test_47_ronson_redirect_safety(self):
        with self.conn() as conn:
            self.assertEqual(resolve_company_id(conn, 484), 425)
            before = conn.execute("SELECT COUNT(*) FROM companies WHERE id=484").fetchone()[0]
            amend_company(conn, actor=_admin(), company_id=484, fields={"city": "Detroit"})
            after = conn.execute("SELECT COUNT(*) FROM companies WHERE id=484").fetchone()[0]
            survivor_city = conn.execute("SELECT city FROM companies WHERE id=425").fetchone()[0]
            recreated = conn.execute(
                "SELECT COUNT(*) FROM companies WHERE id != 484 AND external_record_no='484'"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(survivor_city, "Detroit")
        self.assertEqual(recreated, 0)

    def test_48_cross_client_canonical_safety(self):
        with self.conn() as conn:
            b_before = conn.execute(
                "SELECT status FROM client_company_relationships WHERE id=2"
            ).fetchone()[0]
            amend_company(conn, actor=_admin(), company_id=1, fields={"website": "https://acme-canonical.example"})
            b_after = conn.execute(
                "SELECT status FROM client_company_relationships WHERE id=2"
            ).fetchone()[0]
            site = conn.execute("SELECT website FROM companies WHERE id=1").fetchone()[0]
        self.assertEqual(b_before, b_after)
        self.assertEqual(site, "https://acme-canonical.example")

    def test_49_dependency_inspector(self):
        with self.conn() as conn:
            gate = inspect_company_dependencies(conn, 1)
        self.assertIn("counts", gate)
        self.assertGreater(gate["counts"]["relationships"], 0)
        self.assertNotEqual(gate["classification"], DELETE_ALLOWED)

    def test_50_atomic_rollback_multi_field_manual_edit(self):
        with self.conn() as conn:
            created = create_company(
                conn, actor=_admin(), company_name="Atomic Co", city="Escanaba", state="MI"
            )
            cid = created["company_id"]
            before = conn.execute(
                "SELECT website, city FROM companies WHERE id=?", (cid,)
            ).fetchone()
            with self.assertRaises(StewardError):
                amend_company(
                    conn, actor=_admin(), company_id=cid,
                    fields={"website": "https://atomic.example", "city": "Changed"},
                    force_fail_after="after_city",
                )
            after = conn.execute(
                "SELECT website, city FROM companies WHERE id=?", (cid,)
            ).fetchone()
        self.assertEqual(after["website"], before["website"])
        self.assertEqual(after["city"], before["city"])

    def test_51_live_path_refused(self):
        self.assertFalse(live_destructive_enabled())
        with patch("data_steward.PRODUCTION_DB_PATH", Path(_TEST_DB).resolve()):
            with self.assertRaises(StewardLiveWriteError):
                assert_not_production_db()

    def test_52_duplicate_contact_at_destination_company(self):
        with self.conn() as conn:
            other = create_company(
                conn, actor=_admin(), company_name="Dest Co", city="Marquette", state="MI"
            )
            create_contact(
                conn, actor=_admin(), company_id=other["company_id"],
                first_name="Bob", last_name="Smith", email="bob@acme.example",
            )
            with self.assertRaises(StewardError) as ctx:
                amend_contact(
                    conn, actor=_admin(), contact_id=1,
                    fields={"company_id": other["company_id"]},
                )
        self.assertEqual(str(ctx.exception), "duplicate_contact")


if __name__ == "__main__":
    unittest.main()
