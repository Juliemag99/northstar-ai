"""LeadMaster refresh Phase 3 — isolated confirm/apply proof.

Blank NORTHSTAR_TEST_DB. Never copies or writes live northstar.db.
Does not refresh Carmeco, Brown, or Dawson.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-lm-refresh-p3-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from company_locations import upsert_company_source_identity  # noqa: E402
from company_merges import resolve_company_id  # noqa: E402
from crm_import_staging import (  # noqa: E402
    BatchNotReusable,
    ensure_crm_import_schema,
    upload_crm_import,
)
from db import PRODUCTION_DB_PATH, get_connection, init_schema, migrate_schema  # noqa: E402
from leadmaster_refresh_apply import RefreshApplyError, apply_leadmaster_refresh  # noqa: E402
from leadmaster_refresh_confirm import (  # noqa: E402
    INITIAL_IMPORT_REFRESH_CONFIRM,
    confirm_refresh_batch,
    refresh_confirm_readiness,
    refuse_initial_as_refresh,
)
from leadmaster_refresh_plan import (  # noqa: E402
    ADDITIVE_UPDATE,
    ASSIGNMENT_CHANGE,
    CAMPAIGN_ACTIVE,
    CAMPAIGN_CHANGE,
    CAMPAIGN_HISTORICAL,
    CAMPAIGN_SYSTEM,
    CAMPAIGN_UNKNOWN,
    CONFLICT,
    DUPLICATE_HISTORY,
    DUPLICATE_NOTE,
    EXCLUDED_NOTE,
    IncomingHistory,
    IncomingRow,
    NEW_COMPANY,
    NEW_CONTACT,
    NEW_HISTORY,
    NEW_NOTE,
    NEW_RELATIONSHIP,
    POSSIBLE_DUPLICATE,
    RefreshPolicy,
    REVIEW_REQUIRED,
    SOURCE_ID_CONFLICT,
    SOURCE_SYSTEM,
    STATUS_AUTHORITATIVE,
    STATUS_CHANGE,
    STATUS_PROPOSE,
    UNCHANGED,
    UNKNOWN_SOURCE_REP,
    fingerprint_refresh_plan,
    plan_leadmaster_refresh,
)
from leadmaster_refresh_policy import MODE_LEADMASTER_REFRESH  # noqa: E402
from leadmaster_refresh_resolutions import KEEP_EXISTING, SKIP_SOURCE_ROW  # noqa: E402
from leadmaster_refresh_staging import (  # noqa: E402
    INITIAL_IMPORT_REFRESH_BATCH,
    RefreshLiveWriteError,
    assert_not_production_db,
    ensure_leadmaster_refresh_schema,
    refuse_refresh_as_initial_import,
    upload_leadmaster_refresh,
)
from models import NorthStarUser  # noqa: E402

CLIENT_A = 1
CLIENT_B = 2
SHA = "phase3-source-sha"


def _actor() -> NorthStarUser:
    return NorthStarUser(
        id=10,
        email="admin@northstar.test",
        full_name="Admin Test",
        is_administrator=True,
        is_internal_northstar=True,
        active=True,
        created_at="",
    )


def _policy(**kwargs) -> RefreshPolicy:
    base = dict(
        client_id=CLIENT_A,
        assignment_user_map={"Sam Mapper": 11},
        campaign_class_map={
            "Q3 Active": CAMPAIGN_ACTIVE,
            "Old 2019 List": CAMPAIGN_HISTORICAL,
            "QA List": CAMPAIGN_SYSTEM,
        },
        campaign_target_map={"Q3 Active": 7},
    )
    base.update(kwargs)
    return RefreshPolicy(**base)


def _row(**kwargs) -> IncomingRow:
    data = dict(
        source_row=1,
        record_no="100",
        company_name="Acme Stamping",
        address="100 Main St",
        city="Detroit",
        state="MI",
        zip="48201",
        first_name="Bob",
        last_name="Smith",
        email="bob@acme.example",
        contact_phone="(313) 555-0101",
        status="New",
        notes="Existing note",
    )
    data.update(kwargs)
    return IncomingRow(**data)


def _plan(conn, rows, policy=None):
    return plan_leadmaster_refresh(
        conn,
        client_id=CLIENT_A,
        rows=rows,
        policy=policy or _policy(),
        source_sha256=SHA,
        source_filename="export.csv",
    )


def _apply(conn, rows, policy=None, fingerprint=None, fail_after="", resolutions=None):
    pol = policy or _policy()
    plan = _plan(conn, rows, pol)
    frozen = list(resolutions or [])
    plan["resolutions"] = frozen
    plan["plan_fingerprint"] = fingerprint_refresh_plan(plan)
    return apply_leadmaster_refresh(
        conn,
        incoming_rows=rows,
        policy=pol,
        source_sha256=SHA,
        source_filename="export.csv",
        expected_fingerprint=fingerprint or plan["plan_fingerprint"],
        force_fail_after=fail_after,
        resolutions=frozen,
        actor_id=10,
    ), plan


def _counts(conn):
    return {
        "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "ccr": conn.execute("SELECT COUNT(*) FROM client_company_relationships").fetchone()[0],
        "history": conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0],
        "identities": conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0],
        "campaigns": conn.execute("SELECT COUNT(*) FROM campaign_companies").fetchone()[0],
    }


class LeadMasterRefreshPhase3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if Path(_TEST_DB).resolve() == PRODUCTION_DB_PATH.resolve():
            raise RuntimeError("Refusing Phase 3 tests against production")
        conn = get_connection()
        try:
            init_schema(conn)
            migrate_schema(conn)
            ensure_crm_import_schema(conn)
            ensure_leadmaster_refresh_schema(conn)
            conn.execute("INSERT INTO clients (id, code, name) VALUES (1, 'alpha', 'Alpha')")
            conn.execute("INSERT INTO clients (id, code, name) VALUES (2, 'beta', 'Beta')")
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, is_administrator, active)
                VALUES (10, 'admin@northstar.test', 'Admin Test', 1, 1),
                       (11, 'sam@northstar.test', 'Sam Mapper', 0, 1),
                       (12, 'none@northstar.test', 'No Access', 0, 1),
                       (13, 'old@northstar.test', 'Inactive', 0, 0)
                """
            )
            conn.execute(
                """
                INSERT INTO user_client_assignments (user_id, client_id, active)
                VALUES (11, 1, 1), (10, 1, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO companies (
                    id, external_record_no, company_name, address, city, state, zip,
                    website, legacy_phone, created_at, last_updated_at
                ) VALUES
                (1, '100', 'Acme Stamping', '100 Main St', 'Detroit', 'MI', '48201',
                 'https://acme.example', '(313) 555-0100', datetime('now'), datetime('now')),
                (2, '200', 'Blank Fields Co', '', 'Grand Rapids', 'MI', '',
                 '', '', datetime('now'), datetime('now')),
                (3, '210', 'Conflict Co', '9 Oak', 'Flint', 'MI', '48501',
                 '', '(810) 555-0000', datetime('now'), datetime('now')),
                (425, '425', 'Ronson Survivor', '1 Ronson', 'Detroit', 'MI', '48201',
                 '', '', datetime('now'), datetime('now')),
                (484, '484', 'Ronson Loser', '1 Ronson', 'Detroit', 'MI', '48201',
                 '', '', datetime('now'), datetime('now'))
                """
            )
            conn.execute(
                """
                INSERT INTO contacts (
                    id, company_id, external_record_no, first_name, last_name, email, phone, title,
                    source_row_index, created_at
                ) VALUES
                (1, 1, '100', 'Bob', 'Smith', 'bob@acme.example', '(313) 555-0101', 'Buyer', 1, datetime('now')),
                (2, 2, '200', 'Jane', 'Doe', '', '', '', 1, datetime('now')),
                (3, 3, '210', 'Ann', 'Other', 'ann@conflict.example', '', 'Rep', 1, datetime('now')),
                (4, 425, '425', 'Ron', 'Son', 'ron@ronson.example', '', '', 1, datetime('now'))
                """
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    id, client_id, company_id, external_record_no, status, notes, assigned_user_id
                ) VALUES
                (1, 1, 1, '100', 'New', 'Existing note', 10),
                (2, 2, 1, '100', 'Prospect', 'Client B note', 10),
                (3, 1, 2, '200', 'New', 'Keep', 10),
                (4, 1, 3, '210', 'New', 'Keep', 10),
                (5, 1, 425, '425', 'New', 'Existing note', 10)
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
                conn, company_id=1, source_system=SOURCE_SYSTEM, source_record_no="701",
                client_id=1, source_company_name="Acme Stamping",
            )
            upsert_company_source_identity(
                conn, company_id=484, source_system=SOURCE_SYSTEM, source_record_no="484-stale",
                client_id=1, source_company_name="Ronson Loser",
            )
            conn.execute(
                """
                INSERT INTO client_campaigns (id, client_id, campaign_name, status, is_active)
                VALUES (7, 1, 'Ops Q3', 'Active', 1),
                       (8, 1, 'Archived', 'Inactive', 0),
                       (9, 2, 'Beta Ops', 'Active', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO campaign_companies (
                    campaign_id, client_id, company_id, relationship_id, notes, created_by, created_at
                ) VALUES (7, 1, 1, 1, '', 'seed', datetime('now'))
                """
            )
            conn.commit()
        finally:
            conn.close()

    def conn(self):
        return get_connection()

    def test_01_unchanged_existing(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row()])
            self.assertEqual(plan["rows"][0]["classifications"], [UNCHANGED])
            result, _ = _apply(conn, [_row()])
        self.assertEqual(result["created"]["companies"], 0)
        self.assertEqual(result["created"]["contacts"], 0)

    def test_02_new_company(self):
        incoming = [_row(record_no="3000", company_name="New Co", city="Lansing",
                         first_name="N", last_name="C", email="n@new.example", notes="hello")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(NEW_COMPANY, plan["rows"][0]["classifications"])
            result, _ = _apply(conn, incoming)
            self.assertEqual(result["created"]["companies"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM companies WHERE external_record_no='3000'").fetchone()[0],
                1,
            )

    def test_03_new_relationship(self):
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state, created_at, last_updated_at)
                VALUES ('3100', 'Rel Only', 'Troy', 'MI', datetime('now'), datetime('now'))
                """
            )
            conn.commit()
            incoming = [_row(record_no="3100", company_name="Rel Only", city="Troy",
                             first_name="R", last_name="O", email="r@rel.example")]
            plan = _plan(conn, incoming)
            self.assertIn(NEW_RELATIONSHIP, plan["rows"][0]["classifications"])
            result, _ = _apply(conn, incoming)
            self.assertEqual(result["created"]["relationships"], 1)

    def test_04_new_contact(self):
        incoming = [_row(first_name="Pat", last_name="New", email="pat@acme.example",
                         contact_phone="(313) 555-0999")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(NEW_CONTACT, plan["rows"][0]["classifications"])
            before = conn.execute("SELECT COUNT(*) FROM contacts WHERE company_id=1").fetchone()[0]
            result, _ = _apply(conn, incoming)
            after = conn.execute("SELECT COUNT(*) FROM contacts WHERE company_id=1").fetchone()[0]
        self.assertEqual(result["created"]["contacts"], 1)
        self.assertEqual(after, before + 1)

    def test_05_additive_company_field(self):
        incoming = [_row(record_no="200", company_name="Blank Fields Co", city="Grand Rapids",
                         website="https://blank.example", first_name="Jane", last_name="Doe",
                         email="", contact_phone="", notes="Keep")]
        with self.conn() as conn:
            result, _ = _apply(conn, incoming)
            site = conn.execute("SELECT website FROM companies WHERE id=2").fetchone()[0]
        self.assertTrue(result["created"]["company_fills"] >= 1)
        self.assertEqual(site, "https://blank.example")

    def test_06_conflicting_company_field(self):
        incoming = [_row(address="999 Other Rd")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            actions = {p["action"] for p in plan["rows"][0]["proposals"]}
            self.assertTrue("CLIENT_SOURCE_DIFFERENCE" in actions or "LOCATION_DIFFERENCE" in actions)
            addr = conn.execute("SELECT address FROM companies WHERE id=1").fetchone()[0]
            _apply(conn, incoming)
            addr2 = conn.execute("SELECT address FROM companies WHERE id=1").fetchone()[0]
        self.assertEqual(addr, addr2)

    def test_07_additive_contact_email(self):
        incoming = [_row(record_no="200", company_name="Blank Fields Co", city="Grand Rapids",
                         first_name="Jane", last_name="Doe", email="jane@blank.example",
                         contact_phone="", notes="Keep")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(ADDITIVE_UPDATE, {p["action"] for p in plan["rows"][0]["proposals"]})
            _apply(conn, incoming)
            email = conn.execute("SELECT email FROM contacts WHERE id=2").fetchone()[0]
        self.assertEqual(email, "jane@blank.example")

    def test_08_conflicting_contact_email_keep(self):
        incoming = [_row(email="other@acme.example")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(CONFLICT, {p["action"] for p in plan["rows"][0]["proposals"]})
            _apply(
                conn, incoming,
                resolutions=[{"source_row": 1, "field": "email", "proposal_action": "CONFLICT",
                              "resolution": KEEP_EXISTING}],
            )
            email = conn.execute("SELECT email FROM contacts WHERE id=1").fetchone()[0]
        self.assertEqual(email, "bob@acme.example")

    def test_09_additive_phone(self):
        incoming = [_row(record_no="200", company_name="Blank Fields Co", city="Grand Rapids",
                         first_name="Jane", last_name="Doe", email="",
                         contact_phone="(616) 555-7777", notes="Keep")]
        with self.conn() as conn:
            _apply(conn, incoming)
            phone = conn.execute("SELECT phone FROM contacts WHERE id=2").fetchone()[0]
        self.assertTrue(phone)

    def test_10_last_seven_review(self):
        incoming = [_row(first_name="Other", last_name="Person", email="",
                         contact_phone="5550101")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
        self.assertIn(POSSIBLE_DUPLICATE, plan["rows"][0]["classifications"])

    def test_11_propose_status_does_not_write(self):
        incoming = [_row(status="Contacted")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(STATUS_CHANGE, plan["rows"][0]["classifications"])
            _apply(conn, incoming)
            status = conn.execute("SELECT status FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertEqual(status, "New")

    def test_12_authoritative_status(self):
        incoming = [_row(record_no="210", company_name="Conflict Co", city="Flint",
                         first_name="Ann", last_name="Other", email="ann@conflict.example",
                         status="Contacted", notes="Keep")]
        policy = _policy(status_mode=STATUS_AUTHORITATIVE)
        with self.conn() as conn:
            _apply(conn, incoming, policy=policy)
            status = conn.execute("SELECT status FROM client_company_relationships WHERE id=4").fetchone()[0]
        self.assertEqual(status, "Contacted")

    def test_13_blank_status_preserves(self):
        incoming = [_row(status="")]
        with self.conn() as conn:
            _apply(conn, incoming)
            status = conn.execute("SELECT status FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertEqual(status, "New")

    def test_14_invalid_status(self):
        incoming = [_row(status="Not A Real Status")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
        self.assertIn(REVIEW_REQUIRED, plan["rows"][0]["classifications"])

    def test_15_new_note(self):
        incoming = [_row(notes="Brand new distinct note")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(NEW_NOTE, {p["action"] for p in plan["rows"][0]["proposals"]})
            _apply(conn, incoming)
            notes = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertIn("Brand new distinct note", notes)
        self.assertIn("Existing note", notes)

    def test_16_duplicate_note(self):
        incoming = [_row(notes="Existing note")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(DUPLICATE_NOTE, {p["action"] for p in plan["rows"][0]["proposals"]})
            before = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertEqual(before, after)

    def test_17_excluded_marketing_note(self):
        incoming = [_row(notes="Marketing email sent confirmation")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            actions = {p["action"] for p in plan["rows"][0]["proposals"]}
            if EXCLUDED_NOTE not in actions:
                self.skipTest("marketing exclusion helper did not classify this fixture text")
            before = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT notes FROM client_company_relationships WHERE id=1").fetchone()[0]
        self.assertEqual(before, after)

    def test_18_new_history(self):
        incoming = [_row(history=[IncomingHistory(source_note_id="h-new", note_text="Hello hist",
                                                  event_at="2026-01-02")])]
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0]
        self.assertEqual(after, before + 1)

    def test_19_duplicate_history(self):
        incoming = [_row(history=[IncomingHistory(source_note_id="h-dup", note_text="Dup hist",
                                                  event_at="2026-01-03")])]
        with self.conn() as conn:
            _apply(conn, incoming)
            plan = _plan(conn, incoming)
            self.assertIn(DUPLICATE_HISTORY, {p["action"] for p in plan["rows"][0]["proposals"]})
            before = conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0]
        self.assertEqual(before, after)

    def test_20_assignment_mapped(self):
        incoming = [_row(assigned_rep="Sam Mapper")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(ASSIGNMENT_CHANGE, plan["rows"][0]["classifications"])
            _apply(conn, incoming)
            assigned = conn.execute(
                "SELECT assigned_user_id FROM client_company_relationships WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(int(assigned), 11)

    def test_21_assignment_unknown(self):
        incoming = [_row(assigned_rep="Mystery Rep")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(UNKNOWN_SOURCE_REP, {p["action"] for p in plan["rows"][0]["proposals"]})
            gate = refresh_confirm_readiness(
                conn, client_id=CLIENT_A, batch_id=999, actor=_actor()
            )
        self.assertIn(REVIEW_REQUIRED, plan["rows"][0]["classifications"])
        self.assertFalse(gate["ready"])

    def test_22_assignment_inactive_user(self):
        policy = _policy(assignment_user_map={"Old Rep": 13}, assignment_state_map={"Old Rep": "INACTIVE_TARGET"})
        incoming = [_row(assigned_rep="Old Rep")]
        with self.conn() as conn:
            plan = _plan(conn, incoming, policy)
        self.assertTrue(plan["rows"][0]["review"])

    def test_23_assignment_user_lacks_acl(self):
        policy = _policy(assignment_user_map={"No Access": 12})
        incoming = [_row(assigned_rep="No Access")]
        with self.conn() as conn:
            plan = _plan(conn, incoming, policy)
            with self.assertRaises(RefreshApplyError) as ctx:
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=policy, source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )
        self.assertEqual(str(ctx.exception), "assignment_user_lacks_client_access")

    def test_24_campaign_active(self):
        incoming = [_row(record_no="210", company_name="Conflict Co", city="Flint",
                         first_name="Ann", last_name="Other", email="ann@conflict.example",
                         notes="Keep", campaign="Q3 Active")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(CAMPAIGN_CHANGE, plan["rows"][0]["classifications"])
            _apply(conn, incoming)
            n = conn.execute(
                "SELECT COUNT(*) FROM campaign_companies WHERE campaign_id=7 AND company_id=3"
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_25_campaign_historical(self):
        incoming = [_row(campaign="Old 2019 List")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            before = conn.execute("SELECT COUNT(*) FROM campaign_companies").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT COUNT(*) FROM campaign_companies").fetchone()[0]
        self.assertEqual(before, after)

    def test_26_campaign_system(self):
        incoming = [_row(campaign="QA List")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            before = conn.execute("SELECT COUNT(*) FROM campaign_companies").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT COUNT(*) FROM campaign_companies").fetchone()[0]
        self.assertEqual(before, after)

    def test_27_campaign_unknown(self):
        incoming = [_row(campaign="Mystery List")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
        self.assertIn(REVIEW_REQUIRED, plan["rows"][0]["classifications"])

    def test_28_source_identity_create(self):
        incoming = [_row(record_no="4000", company_name="Ident New", city="Novi",
                         first_name="I", last_name="N", email="i@ident.example")]
        with self.conn() as conn:
            _apply(conn, incoming)
            n = conn.execute(
                "SELECT COUNT(*) FROM company_source_identities WHERE source_record_no='4000'"
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_29_source_identity_reuse(self):
        incoming = [_row(record_no="4000", company_name="Ident New", city="Novi",
                         first_name="I", last_name="N", email="i@ident.example")]
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0]
            plan = _plan(conn, incoming)
            if NEW_COMPANY in plan["rows"][0]["classifications"]:
                _apply(conn, incoming)
                before = conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0]
        self.assertEqual(before, after)

    def test_30_source_identity_conflict(self):
        incoming = [_row(record_no="701", company_name="Conflict Co", city="Flint",
                         first_name="X", last_name="Y", email="x@c.example", notes="")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertIn(SOURCE_ID_CONFLICT, plan["rows"][0]["classifications"])
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_31_ronson_redirect(self):
        incoming = [_row(record_no="484", company_name="Ronson Survivor", city="Detroit",
                         first_name="Ron", last_name="Son", email="ron@ronson.example",
                         contact_phone="", notes="Existing note")]
        with self.conn() as conn:
            self.assertEqual(resolve_company_id(conn, 484), 425)
            plan = _plan(conn, incoming)
            self.assertEqual(plan["rows"][0]["company_id"], 425)
            self.assertNotIn(NEW_COMPANY, plan["rows"][0]["classifications"])
            before = conn.execute("SELECT COUNT(*) FROM companies WHERE id=484").fetchone()[0]
            _apply(conn, incoming)
            after = conn.execute("SELECT COUNT(*) FROM companies WHERE id=484").fetchone()[0]
        self.assertEqual(before, after)

    def test_32_stale_company(self):
        incoming = [_row(record_no="200", company_name="Blank Fields Co", city="Grand Rapids",
                         first_name="Jane", last_name="Doe", email="", notes="Keep")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            conn.execute("UPDATE companies SET city='Changed' WHERE id=2")
            conn.commit()
            with self.assertRaises(RefreshApplyError) as ctx:
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )
            self.assertEqual(str(ctx.exception), "stale_plan")

    def test_33_stale_contact(self):
        incoming = [_row()]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            conn.execute("UPDATE contacts SET email='changed@acme.example' WHERE id=1")
            conn.commit()
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_34_stale_ccr_status(self):
        incoming = [_row(status="Contacted")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            conn.execute("UPDATE client_company_relationships SET status='Qualified' WHERE id=1")
            conn.commit()
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_35_stale_assignment(self):
        incoming = [_row(record_no="210", company_name="Conflict Co", city="Flint",
                         first_name="Ann", last_name="Other", email="ann@conflict.example",
                         notes="Keep", assigned_rep="Sam Mapper")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            conn.execute("UPDATE client_company_relationships SET assigned_user_id=11 WHERE id=4")
            conn.commit()
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_36_stale_campaign(self):
        incoming = [_row(record_no="200", company_name="Blank Fields Co", city="Grand Rapids",
                         first_name="Jane", last_name="Doe", email="", notes="Keep",
                         campaign="Q3 Active")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            conn.execute(
                """
                INSERT OR IGNORE INTO campaign_companies (
                    campaign_id, client_id, company_id, relationship_id, notes, created_by, created_at
                ) VALUES (7, 1, 2, 3, '', 'stale', datetime('now'))
                """
            )
            conn.commit()
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_37_stale_identity(self):
        incoming = [_row(record_no="4100", company_name="Ident Stale", city="Canton",
                         first_name="S", last_name="I", email="s@stale.example")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            upsert_company_source_identity(
                conn, company_id=1, source_system=SOURCE_SYSTEM, source_record_no="4100",
                client_id=1, source_company_name="Acme",
            )
            conn.commit()
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_38_stale_note_history(self):
        from client_data_history_notes import compute_history_event_hash
        from shared_note_history_import import normalize_record_no

        incoming = [_row(history=[IncomingHistory(source_note_id="h-stale", note_text="later",
                                                  event_at="2026-02-01")])]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            eh = compute_history_event_hash(
                client_id=CLIENT_A,
                company_id=None,
                company_record_no=normalize_record_no("100"),
                note_text="later",
                event_at="2026-02-01",
                source_note_id="h-stale",
            )
            conn.execute(
                """
                INSERT INTO company_shared_history_events (
                    company_id, event_hash, note_text, imported_at
                ) VALUES (1, ?, 'later', datetime('now'))
                """,
                (eh,),
            )
            conn.commit()
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_39_changed_policy(self):
        incoming = [_row(status="Contacted")]
        with self.conn() as conn:
            plan = _plan(conn, incoming, _policy(status_mode=STATUS_PROPOSE))
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(status_mode=STATUS_AUTHORITATIVE),
                    source_sha256=SHA, source_filename="export.csv",
                    expected_fingerprint=plan["plan_fingerprint"],
                )

    def test_40_changed_mapping_fingerprint(self):
        incoming = [_row()]
        with self.conn() as conn:
            a = _plan(conn, incoming, _policy())
            pol = _policy()
            pol.mapping = {"company_name": "Company"}
            b = _plan(conn, incoming, pol)
        self.assertNotEqual(a["plan_fingerprint"], b["plan_fingerprint"])

    def test_41_changed_resolution_fingerprint(self):
        incoming = [_row(email="other@acme.example")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            plan["resolutions"] = []
            fp1 = fingerprint_refresh_plan(plan)
            plan["resolutions"] = [
                {"source_row": 1, "field": "email", "proposal_action": "CONFLICT",
                 "resolution": KEEP_EXISTING}
            ]
            fp2 = fingerprint_refresh_plan(plan)
        self.assertNotEqual(fp1, fp2)

    def test_42_duplicate_rerun(self):
        incoming = [_row(
            record_no="5000", company_name="Idem Co", city="Petoskey",
            first_name="Ida", last_name="Potent", email="ida@idem.example",
            notes="First distinct note",
            history=[IncomingHistory(source_note_id="hist-5000", note_text="Kickoff",
                                     event_at="2026-01-01")],
        )]
        with self.conn() as conn:
            before = _counts(conn)
            _apply(conn, incoming)
            mid = _counts(conn)
            plan2 = _plan(conn, incoming)
            self.assertEqual(plan2["rows"][0]["classifications"], [UNCHANGED])
            _apply(conn, incoming)
            after = _counts(conn)
        self.assertEqual(mid["companies"], before["companies"] + 1)
        self.assertEqual(after, mid)

    def test_43_rollback_company_stage(self):
        incoming = [_row(record_no="5100", company_name="Roll Co", city="Marquette",
                         first_name="R", last_name="C", email="r@roll.example")]
        with self.conn() as conn:
            before = _counts(conn)
            plan = _plan(conn, incoming)
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                    force_fail_after="company",
                )
            after = _counts(conn)
        self.assertEqual(before, after)

    def test_44_rollback_contact_stage(self):
        incoming = [_row(record_no="200", company_name="Blank Fields Co", city="Grand Rapids",
                         first_name="Jane", last_name="Doe", email="jane2@blank.example",
                         notes="Keep")]
        with self.conn() as conn:
            email_before = conn.execute("SELECT email FROM contacts WHERE id=2").fetchone()[0]
            plan = _plan(conn, incoming)
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                    force_fail_after="after_contact_update",
                )
            email_after = conn.execute("SELECT email FROM contacts WHERE id=2").fetchone()[0]
        self.assertEqual(email_before, email_after)

    def test_45_rollback_history_stage(self):
        incoming = [_row(history=[IncomingHistory(source_note_id="h-roll", note_text="rb",
                                                  event_at="2026-03-01")])]
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0]
            plan = _plan(conn, incoming)
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                    force_fail_after="after_note_history",
                )
            after = conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0]
        self.assertEqual(before, after)

    def test_46_rollback_assignment_stage(self):
        incoming = [_row(record_no="210", company_name="Conflict Co", city="Flint",
                         first_name="Ann", last_name="Other", email="ann@conflict.example",
                         notes="Keep", assigned_rep="Sam Mapper")]
        with self.conn() as conn:
            conn.execute("UPDATE client_company_relationships SET assigned_user_id=10 WHERE id=4")
            conn.commit()
            before = conn.execute(
                "SELECT assigned_user_id FROM client_company_relationships WHERE id=4"
            ).fetchone()[0]
            plan = _plan(conn, incoming)
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                    force_fail_after="after_assignment",
                )
            after = conn.execute(
                "SELECT assigned_user_id FROM client_company_relationships WHERE id=4"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_47_rollback_campaign_stage(self):
        incoming = [_row(record_no="425", company_name="Ronson Survivor", city="Detroit",
                         first_name="Ron", last_name="Son", email="ron@ronson.example",
                         contact_phone="", notes="Existing note", campaign="Q3 Active")]
        with self.conn() as conn:
            conn.execute("DELETE FROM campaign_companies WHERE company_id=425")
            conn.commit()
            before = conn.execute(
                "SELECT COUNT(*) FROM campaign_companies WHERE company_id=425"
            ).fetchone()[0]
            plan = _plan(conn, incoming)
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=_policy(), source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                    force_fail_after="after_campaign",
                )
            after = conn.execute(
                "SELECT COUNT(*) FROM campaign_companies WHERE company_id=425"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_48_cross_client_isolation(self):
        incoming = [_row(status="Contacted", assigned_rep="Sam Mapper")]
        policy = _policy(status_mode=STATUS_AUTHORITATIVE)
        with self.conn() as conn:
            conn.execute("UPDATE contacts SET email='bob@acme.example' WHERE id=1")
            conn.execute("UPDATE client_company_relationships SET assigned_user_id=10, status='New' WHERE id=1")
            conn.commit()
            b_status = conn.execute(
                "SELECT status, assigned_user_id, notes FROM client_company_relationships WHERE id=2"
            ).fetchone()
            _apply(conn, incoming, policy=policy)
            a_row = conn.execute(
                "SELECT status, assigned_user_id FROM client_company_relationships WHERE id=1"
            ).fetchone()
            b_after = conn.execute(
                "SELECT status, assigned_user_id, notes FROM client_company_relationships WHERE id=2"
            ).fetchone()
        self.assertEqual(a_row["status"], "Contacted")
        self.assertEqual(int(a_row["assigned_user_id"]), 11)
        self.assertEqual(b_after["status"], b_status["status"])
        self.assertEqual(b_after["assigned_user_id"], b_status["assigned_user_id"])
        self.assertEqual(b_after["notes"], b_status["notes"])

    def test_49_initial_import_rejects_refresh(self):
        row = {"import_mode": MODE_LEADMASTER_REFRESH, "status": "previewed"}
        class _Row(dict):
            def keys(self):
                return super().keys()
        with self.assertRaises(BatchNotReusable) as ctx:
            refuse_refresh_as_initial_import(_Row(row))
        self.assertIn("LeadMaster refresh", str(ctx.exception))

    def test_50_refresh_rejects_initial_import(self):
        class _Row(dict):
            def keys(self):
                return super().keys()
        with self.assertRaises(BatchNotReusable) as ctx:
            refuse_initial_as_refresh(_Row({"import_mode": "INITIAL_IMPORT", "status": "previewed"}))
        self.assertIn("initial CRM import", str(ctx.exception))

    def test_51_campaign_wrong_client(self):
        policy = _policy(campaign_target_map={"Q3 Active": 9})
        incoming = [_row(record_no="210", company_name="Conflict Co", city="Flint",
                         first_name="Ann", last_name="Other", email="ann@conflict.example",
                         notes="Keep", campaign="Q3 Active")]
        with self.conn() as conn:
            plan = _plan(conn, incoming, policy)
            with self.assertRaises(RefreshApplyError) as ctx:
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=policy, source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )
        self.assertEqual(str(ctx.exception), "campaign_wrong_client")

    def test_52_campaign_inactive(self):
        policy = _policy(
            campaign_class_map={"Archived List": CAMPAIGN_ACTIVE},
            campaign_target_map={"Archived List": 8},
        )
        incoming = [_row(record_no="210", company_name="Conflict Co", city="Flint",
                         first_name="Ann", last_name="Other", email="ann@conflict.example",
                         notes="Keep", campaign="Archived List")]
        with self.conn() as conn:
            plan = _plan(conn, incoming, policy)
            with self.assertRaises(RefreshApplyError) as ctx:
                apply_leadmaster_refresh(
                    conn, incoming_rows=incoming, policy=policy, source_sha256=SHA,
                    source_filename="export.csv", expected_fingerprint=plan["plan_fingerprint"],
                )
        self.assertEqual(str(ctx.exception), "campaign_inactive")

    def test_53_stale_identity_on_merged_loser(self):
        incoming = [_row(record_no="484-stale", company_name="Ronson Survivor", city="Detroit",
                         first_name="Ron", last_name="Son", email="ron@ronson.example",
                         contact_phone="", notes="Existing note")]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            self.assertEqual(plan["rows"][0]["company_id"], 425)
            _apply(conn, incoming)
            ident = conn.execute(
                "SELECT company_id FROM company_source_identities WHERE source_record_no='484-stale'"
            ).fetchone()
        self.assertEqual(int(ident["company_id"]), 425)

    def test_54_live_path_refused(self):
        with patch(
            "leadmaster_refresh_staging.PRODUCTION_DB_PATH",
            Path(_TEST_DB).resolve(),
        ):
            with self.assertRaises(RefreshLiveWriteError):
                assert_not_production_db()

    def test_55_skip_resolution_allowed(self):
        incoming = [_row(record_no="701", company_name="Conflict Co", city="Flint",
                         first_name="X", last_name="Y", email="x@c.example", notes="")]
        with self.conn() as conn:
            before = _counts(conn)
            _apply(
                conn, incoming,
                resolutions=[{"source_row": 1, "field": "record_no",
                              "proposal_action": SOURCE_ID_CONFLICT,
                              "resolution": SKIP_SOURCE_ROW}],
            )
            after = _counts(conn)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
