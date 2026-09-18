"""LeadMaster incremental refresh — Phase 1 isolated proof.

Uses a blank NORTHSTAR_TEST_DB tempfile. Never copies or writes live northstar.db.
Does not refresh Carmeco, Brown, or Dawson.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-lm-refresh-p1-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from company_aliases import SOURCE_CRM_IMPORT, upsert_company_alias  # noqa: E402
from company_locations import upsert_company_source_identity  # noqa: E402
from company_merges import resolve_company_id  # noqa: E402
from db import PRODUCTION_DB_PATH, get_connection, init_schema, migrate_schema  # noqa: E402
from leadmaster_refresh_apply import (  # noqa: E402
    RefreshApplyError,
    apply_leadmaster_refresh,
)
from leadmaster_refresh_plan import (  # noqa: E402
    ADDITIVE_UPDATE,
    ASSIGNMENT_CHANGE,
    CAMPAIGN_ACTIVE,
    CAMPAIGN_CHANGE,
    CAMPAIGN_HISTORICAL,
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
    PLANNER_VERSION,
    POSSIBLE_DUPLICATE,
    RefreshPolicy,
    REVIEW_REQUIRED,
    SKIPPED_INVALID,
    SOURCE_ID_CONFLICT,
    SOURCE_SYSTEM,
    STATUS_CHANGE,
    UNCHANGED,
    UNKNOWN_SOURCE_REP,
    fingerprint_refresh_plan,
    plan_leadmaster_refresh,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "working" / "leadmaster-refresh"
CLIENT_ID = 1
SHA = "abc123source"


def _policy(**kwargs) -> RefreshPolicy:
    base = dict(
        client_id=CLIENT_ID,
        assignment_user_map={"Pat Rep": 10, "Sam Mapper": 11},
        campaign_class_map={
            "Q3 Active": CAMPAIGN_ACTIVE,
            "Old 2019 List": CAMPAIGN_HISTORICAL,
        },
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
        website="https://acme.example",
        company_phone="(313) 555-0100",
        first_name="Bob",
        last_name="Smith",
        title="Buyer",
        email="bob@acme.example",
        contact_phone="(313) 555-0101",
        status="New",
        notes="Existing note",
    )
    data.update(kwargs)
    return IncomingRow(**data)


def _plan(conn, rows, policy=None, sha=SHA, filename="export.csv"):
    return plan_leadmaster_refresh(
        conn,
        client_id=CLIENT_ID,
        rows=rows,
        policy=policy or _policy(),
        source_sha256=sha,
        source_filename=filename,
    )


def _classes(plan: dict, idx: int = 0) -> list[str]:
    return list(plan["rows"][idx]["classifications"])


def _actions(plan: dict, idx: int = 0) -> set[str]:
    return {p["action"] for p in plan["rows"][idx]["proposals"]}


def _insert_company(conn, *, cid: int, rn: str, name: str, city: str, state: str,
                    address: str = "1 St", website: str = "", phone: str = "") -> None:
    conn.execute(
        """
        INSERT INTO companies (
            id, external_record_no, company_name, address, city, state, zip,
            website, legacy_phone, created_at, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, '48201', ?, ?, datetime('now'), datetime('now'))
        """,
        (cid, rn, name, address, city, state, website, phone),
    )


def _insert_contact(conn, *, company_id: int, first: str, last: str, email: str = "",
                    phone: str = "", title: str = "Buyer") -> int:
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name, email, phone,
            title, source_row_index, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
        """,
        (company_id, str(company_id), first, last, email, phone, title),
    )
    return int(cur.lastrowid)


def _insert_ccr(conn, *, company_id: int, rn: str, status: str = "New",
                notes: str = "Existing note", assigned: int | None = 10) -> int:
    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, external_record_no, status, notes, assigned_user_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (CLIENT_ID, company_id, rn, status, notes, assigned),
    )
    return int(cur.lastrowid)


def _seed(conn) -> None:
    conn.execute(
        "INSERT INTO clients (id, code, name) VALUES (1, 'testpilot', 'Test Pilot')"
    )
    conn.execute(
        """
        INSERT INTO users (id, email, full_name, is_administrator, active)
        VALUES (10, 'pat@northstar.test', 'Pat Rep', 0, 1),
               (11, 'sam@northstar.test', 'Sam Mapper', 0, 1)
        """
    )
    _insert_company(conn, cid=1, rn="100", name="Acme Stamping", city="Detroit",
                    state="MI", address="100 Main St", website="https://acme.example",
                    phone="(313) 555-0100")
    _insert_contact(conn, company_id=1, first="Bob", last="Smith",
                    email="bob@acme.example", phone="(313) 555-0101")
    _insert_ccr(conn, company_id=1, rn="100")

    _insert_company(conn, cid=2, rn="200", name="Beta Additive", city="Grand Rapids",
                    state="MI")
    _insert_contact(conn, company_id=2, first="Jane", last="Doe", email="", phone="")
    _insert_ccr(conn, company_id=2, rn="200")

    _insert_company(conn, cid=3, rn="210", name="Beta Conflict", city="Grand Rapids",
                    state="MI")
    _insert_contact(conn, company_id=3, first="Jane", last="Doe",
                    email="jane@old.example")
    _insert_ccr(conn, company_id=3, rn="210")

    _insert_company(conn, cid=4, rn="300", name="Gamma Status", city="Lansing",
                    state="MI")
    _insert_contact(conn, company_id=4, first="Ned", last="Notes",
                    email="ned@gamma.example")
    _insert_ccr(conn, company_id=4, rn="300", notes="Keep me")

    _insert_company(conn, cid=5, rn="400", name="Delta History", city="Flint",
                    state="MI")
    _insert_contact(conn, company_id=5, first="Hal", last="Story",
                    email="hal@delta.example")
    _insert_ccr(conn, company_id=5, rn="400")
    conn.execute(
        """
        INSERT INTO company_shared_history_events (
            company_id, external_record_no, event_hash, note_text, event_at, author
        ) VALUES (5, '400', 'hist-dup-400', 'Already imported', '2024-01-01', 'LM')
        """
    )

    _insert_company(conn, cid=6, rn="500", name="Epsilon Alias", city="Ann Arbor",
                    state="MI")
    _insert_contact(conn, company_id=6, first="Al", last="Ias", email="al@eps.example")
    _insert_ccr(conn, company_id=6, rn="500")
    upsert_company_alias(
        conn,
        company_id=6,
        alias_name="Epsilon Alias",
        source_system=SOURCE_CRM_IMPORT,
        source_record_no="555",
        client_id=CLIENT_ID,
        source_city="Ann Arbor",
        source_state="MI",
    )

    _insert_company(conn, cid=7, rn="600", name="Zeta Identity", city="Kalamazoo",
                    state="MI")
    _insert_contact(conn, company_id=7, first="Ida", last="Entity",
                    email="ida@zeta.example")
    _insert_ccr(conn, company_id=7, rn="600")
    upsert_company_source_identity(
        conn,
        company_id=7,
        source_system=SOURCE_SYSTEM,
        source_record_no="600",
        client_id=CLIENT_ID,
        source_company_name="Zeta Identity",
        source_city="Kalamazoo",
        source_state="MI",
    )

    _insert_company(conn, cid=8, rn="700", name="OtherName", city="Detroit",
                    state="MI")
    _insert_ccr(conn, company_id=8, rn="700")
    _insert_company(conn, cid=9, rn="702", name="ConflictCo", city="Flint",
                    state="MI")
    _insert_ccr(conn, company_id=9, rn="702")
    upsert_company_source_identity(
        conn,
        company_id=8,
        source_system=SOURCE_SYSTEM,
        source_record_no="701",
        client_id=CLIENT_ID,
        source_company_name="OtherName",
        source_city="Detroit",
        source_state="MI",
    )

    conn.execute(
        """
        INSERT INTO companies (
            id, external_record_no, company_name, address, city, state, zip,
            created_at, last_updated_at
        ) VALUES (425, 'RN-425', 'Ronson', '1 Ronson', 'Detroit', 'MI', '48201',
                  datetime('now'), datetime('now'))
        """
    )
    _insert_contact(conn, company_id=425, first="Ron", last="Son",
                    email="ron@ronson.example")
    _insert_ccr(conn, company_id=425, rn="RN-425")
    conn.execute(
        """
        INSERT INTO company_merge_history (
            source_company_id, survivor_company_id, reason
        ) VALUES (484, 425, 'test ronson redirect')
        """
    )

    _insert_company(conn, cid=10, rn="910", name="Phone Co", city="Holland",
                    state="MI")
    _insert_contact(conn, company_id=10, first="Phil", last="Ring",
                    email="phil@phone.example", phone="(616) 555-1212")
    _insert_ccr(conn, company_id=10, rn="910")

    _insert_company(conn, cid=11, rn="920", name="Assign Co", city="Muskegon",
                    state="MI")
    _insert_contact(conn, company_id=11, first="Ann", last="Sign",
                    email="ann@assign.example")
    _insert_ccr(conn, company_id=11, rn="920", assigned=10)

    _insert_company(conn, cid=12, rn="930", name="Campaign Co", city="Saginaw",
                    state="MI")
    _insert_contact(conn, company_id=12, first="Cam", last="Paign",
                    email="cam@camp.example")
    _insert_ccr(conn, company_id=12, rn="930")
    conn.commit()


class LeadMasterRefreshPhase1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.test_db = Path(_TEST_DB)
        if cls.test_db.resolve() == PRODUCTION_DB_PATH.resolve():
            raise RuntimeError("Refusing to run refresh tests against production DB")
        conn = get_connection()
        try:
            init_schema(conn)
            migrate_schema(conn)
            _seed(conn)
        finally:
            conn.close()

    def conn(self):
        return get_connection()

    def test_00_isolated_from_production(self):
        self.assertTrue(os.environ.get("NORTHSTAR_TEST_DB"))
        self.assertNotEqual(Path(_TEST_DB).resolve(), PRODUCTION_DB_PATH.resolve())
        self.assertEqual(PLANNER_VERSION, "leadmaster-refresh-plan-v2")
        with self.conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        self.assertGreater(n, 0)
        self.assertLess(n, 50)

    def test_01_exact_rn_unchanged(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row()])
        self.assertEqual(_classes(plan), [UNCHANGED])
        self.assertEqual(plan["rows"][0]["match_evidence"][0], "master_rn")
        self.assertEqual(plan["counts"]["unchanged"], 1)

    def test_02_rn_existing_new_contact(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(first_name="Alice", last_name="Jones",
                                     email="alice@acme.example", contact_phone="")])
        self.assertIn(NEW_CONTACT, _classes(plan))
        self.assertNotIn(NEW_COMPANY, _classes(plan))

    def test_03_rn_existing_contact_new_email(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="200", company_name="Beta Additive", city="Grand Rapids",
                first_name="Jane", last_name="Doe", email="jane@new.example",
                contact_phone="", status="New", notes="",
            )])
        self.assertIn("UPDATED_CONTACT", _classes(plan))
        self.assertIn(ADDITIVE_UPDATE, _actions(plan))

    def test_04_rn_existing_conflicting_email(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="210", company_name="Beta Conflict", city="Grand Rapids",
                first_name="Jane", last_name="Doe", email="jane@new.example",
                contact_phone="", status="New", notes="",
            )])
        self.assertIn(CONFLICT, _actions(plan))
        self.assertTrue(plan["rows"][0]["blocking"])
        self.assertIn(REVIEW_REQUIRED, _classes(plan))

    def test_05_valid_status_change(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="300", company_name="Gamma Status", city="Lansing",
                first_name="Ned", last_name="Notes", email="ned@gamma.example",
                contact_phone="", status="Contacted", notes="Keep me",
            )])
        self.assertIn(STATUS_CHANGE, _classes(plan))
        st = [p for p in plan["rows"][0]["proposals"] if p["field"] == "status"][0]
        self.assertEqual(st["policy"], "propose_change")
        self.assertEqual(st["old_value"], "New")
        self.assertEqual(st["new_value"], "Contacted")

    def test_06_blank_status_preserves(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="300", company_name="Gamma Status", city="Lansing",
                first_name="Ned", last_name="Notes", email="ned@gamma.example",
                contact_phone="", status="", notes="Keep me",
            )])
        st = [p for p in plan["rows"][0]["proposals"] if p["field"] == "status"][0]
        self.assertEqual(st["action"], "NO_CHANGE")
        self.assertEqual(st["policy"], "blank_preserves_existing")

    def test_07_invalid_status(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="300", company_name="Gamma Status", city="Lansing",
                first_name="Ned", last_name="Notes", email="ned@gamma.example",
                contact_phone="", status="NOT_A_REAL_STATUS", notes="Keep me",
            )])
        self.assertIn(SKIPPED_INVALID, _classes(plan))
        self.assertIn(REVIEW_REQUIRED, _classes(plan))

    def test_08_new_note(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="300", company_name="Gamma Status", city="Lansing",
                first_name="Ned", last_name="Notes", email="ned@gamma.example",
                contact_phone="", notes="Brand new distinct note",
            )])
        self.assertIn(NEW_NOTE, _classes(plan))

    def test_09_duplicate_note(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="300", company_name="Gamma Status", city="Lansing",
                first_name="Ned", last_name="Notes", email="ned@gamma.example",
                contact_phone="", notes="Keep me",
            )])
        self.assertIn(DUPLICATE_NOTE, _actions(plan))
        self.assertNotIn(NEW_NOTE, _classes(plan))

    def test_09b_excluded_marketing_note(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="300", company_name="Gamma Status", city="Lansing",
                first_name="Ned", last_name="Notes", email="ned@gamma.example",
                contact_phone="", notes="Marketing email sent",
            )])
        self.assertIn(EXCLUDED_NOTE, _actions(plan))

    def test_10_new_history(self):
        hist = IncomingHistory(source_note_id="hist-new-400", note_text="Called plant",
                               event_at="2025-06-01", author="LM")
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="400", company_name="Delta History", city="Flint",
                first_name="Hal", last_name="Story", email="hal@delta.example",
                contact_phone="", notes="", history=[hist],
            )])
        self.assertIn(NEW_HISTORY, _classes(plan))

    def test_11_duplicate_history(self):
        hist = IncomingHistory(source_note_id="hist-dup-400", note_text="Already imported",
                               event_at="2024-01-01", author="LM")
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="400", company_name="Delta History", city="Flint",
                first_name="Hal", last_name="Story", email="hal@delta.example",
                contact_phone="", notes="", history=[hist],
            )])
        self.assertIn(DUPLICATE_HISTORY, _actions(plan))
        self.assertNotIn(NEW_HISTORY, _classes(plan))

    def test_12_new_company(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="999", company_name="Brand New Co", city="Traverse City",
                first_name="New", last_name="Person", email="n@new.example",
                notes="hello",
            )])
        self.assertIn(NEW_COMPANY, _classes(plan))
        self.assertIn(NEW_RELATIONSHIP, _classes(plan))
        self.assertIn(NEW_CONTACT, _classes(plan))

    def test_13_same_name_different_location(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="998", company_name="Acme Stamping", city="Kalamazoo",
                state="MI", first_name="Other", last_name="Plant",
                email="p@acme.example", notes="",
            )])
        self.assertIn(NEW_COMPANY, _classes(plan))
        self.assertNotIn(UNCHANGED, _classes(plan))

    def test_14_same_name_no_corroboration(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="", company_name="Acme Stamping", city="Detroit",
            )])
        self.assertEqual(_classes(plan), [REVIEW_REQUIRED])
        self.assertIn("no_rn_name_only_forbidden", plan["rows"][0]["proposals"][0]["review_reason"])

    def test_15_company_alias_match(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="555", company_name="Epsilon Alias", city="Ann Arbor",
                first_name="Al", last_name="Ias", email="al@eps.example",
                contact_phone="", notes="Existing note",
            )])
        self.assertIn("alias_rn", plan["rows"][0]["match_evidence"])
        self.assertEqual(plan["rows"][0]["company_id"], 6)

    def test_16_source_identity_match(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="600", company_name="Zeta Identity", city="Kalamazoo",
                first_name="Ida", last_name="Entity", email="ida@zeta.example",
                contact_phone="", notes="Existing note",
            )])
        self.assertIn("source_identity", plan["rows"][0]["match_evidence"])
        self.assertEqual(plan["rows"][0]["company_id"], 7)

    def test_17_source_identity_conflict(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="701", company_name="ConflictCo", city="Flint", state="MI",
                first_name="X", last_name="Y", email="x@c.example", notes="",
            )])
        self.assertIn(SOURCE_ID_CONFLICT, _classes(plan))
        self.assertTrue(plan["rows"][0]["blocking"])
        with self.assertRaises(RefreshApplyError) as ctx:
            apply_leadmaster_refresh(
                conn,
                incoming_rows=[_row(
                    record_no="701", company_name="ConflictCo", city="Flint",
                    first_name="X", last_name="Y", email="x@c.example", notes="",
                )],
                policy=_policy(),
                source_sha256=SHA,
                source_filename="export.csv",
                expected_fingerprint=plan["plan_fingerprint"],
            )
        self.assertIn(str(ctx.exception), {"blocking_issues", "source_id_conflict"})

    def test_18_redirected_ronson_rn(self):
        with self.conn() as conn:
            self.assertEqual(resolve_company_id(conn, 484), 425)
            plan = _plan(conn, [_row(
                record_no="484", company_name="Ronson", city="Detroit",
                first_name="Ron", last_name="Son", email="ron@ronson.example",
                contact_phone="", notes="Existing note",
            )])
        self.assertIn("merge_redirect", plan["rows"][0]["match_evidence"])
        self.assertEqual(plan["rows"][0]["company_id"], 425)
        self.assertNotIn(NEW_COMPANY, _classes(plan))

    def test_19_exact_10_digit_phone(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="910", company_name="Phone Co", city="Holland",
                first_name="Other", last_name="Name", email="",
                contact_phone="6165551212", notes="Existing note",
            )])
        self.assertIn("phone_nanp10", plan["rows"][0]["match_evidence"])
        self.assertNotIn(POSSIBLE_DUPLICATE, _classes(plan))
        self.assertNotIn(NEW_CONTACT, _classes(plan))

    def test_20_last_seven_possible_phone(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="910", company_name="Phone Co", city="Holland",
                first_name="Phil", last_name="Ring", email="",
                contact_phone="5551212", notes="Existing note",
            )])
        self.assertIn(POSSIBLE_DUPLICATE, _classes(plan))
        self.assertIn("phone_last7", plan["rows"][0]["match_evidence"])

    def test_21_assignment_change_explicit_map(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="920", company_name="Assign Co", city="Muskegon",
                first_name="Ann", last_name="Sign", email="ann@assign.example",
                contact_phone="", notes="Existing note", assigned_rep="Sam Mapper",
            )])
        self.assertIn(ASSIGNMENT_CHANGE, _classes(plan))
        asg = [p for p in plan["rows"][0]["proposals"] if p["field"] == "assigned_user_id"][0]
        self.assertEqual(asg["new_value"], "11")
        self.assertEqual(asg["policy"], "explicit_map")

    def test_22_unknown_rep(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="920", company_name="Assign Co", city="Muskegon",
                first_name="Ann", last_name="Sign", email="ann@assign.example",
                contact_phone="", notes="Existing note", assigned_rep="Mystery Rep",
            )])
        self.assertIn(UNKNOWN_SOURCE_REP, _actions(plan))
        self.assertIn(REVIEW_REQUIRED, _classes(plan))

    def test_23_active_campaign(self):
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO client_campaigns (id, client_id, campaign_name, status, is_active)
                VALUES (50, ?, 'Q3 Operational', 'Active', 1)
                """,
                (CLIENT_ID,),
            )
            conn.commit()
            policy = _policy()
            policy.campaign_target_map = {"Q3 Active": 50}
            plan = _plan(conn, [_row(
                record_no="930", company_name="Campaign Co", city="Saginaw",
                first_name="Cam", last_name="Paign", email="cam@camp.example",
                contact_phone="", notes="Existing note", campaign="Q3 Active",
            )], policy=policy)
        self.assertIn(CAMPAIGN_CHANGE, _classes(plan))

    def test_24_historical_campaign(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="930", company_name="Campaign Co", city="Saginaw",
                first_name="Cam", last_name="Paign", email="cam@camp.example",
                contact_phone="", notes="Existing note", campaign="Old 2019 List",
            )])
        self.assertIn(CAMPAIGN_HISTORICAL, _actions(plan))
        self.assertNotIn(CAMPAIGN_CHANGE, _classes(plan))

    def test_25_unknown_campaign(self):
        with self.conn() as conn:
            plan = _plan(conn, [_row(
                record_no="930", company_name="Campaign Co", city="Saginaw",
                first_name="Cam", last_name="Paign", email="cam@camp.example",
                contact_phone="", notes="Existing note", campaign="Mystery List",
            )])
        self.assertIn(CAMPAIGN_UNKNOWN, _actions(plan))
        self.assertIn(REVIEW_REQUIRED, _classes(plan))

    def test_26_duplicate_rerun_idempotent(self):
        incoming = [_row(
            source_row=1,
            record_no="1000",
            company_name="Idempotent Co",
            city="Petoskey",
            state="MI",
            first_name="Ida",
            last_name="Potent",
            email="ida@idem.example",
            contact_phone="(231) 555-2222",
            status="New",
            notes="First distinct note",
            history=[IncomingHistory(source_note_id="hist-1000", note_text="Kickoff",
                                     event_at="2026-01-01", author="LM")],
        )]
        with self.conn() as conn:
            before = {
                "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
                "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
                "ccr": conn.execute("SELECT COUNT(*) FROM client_company_relationships").fetchone()[0],
                "notes_len": conn.execute(
                    "SELECT COUNT(*) FROM client_company_relationships WHERE notes LIKE '%First distinct note%'"
                ).fetchone()[0],
                "history": conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0],
                "identities": conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0],
                "campaigns": conn.execute("SELECT COUNT(*) FROM campaign_companies").fetchone()[0],
            }
            plan1 = _plan(conn, incoming)
            self.assertIn(NEW_COMPANY, _classes(plan1))
            result = apply_leadmaster_refresh(
                conn,
                incoming_rows=incoming,
                policy=_policy(),
                source_sha256=SHA,
                source_filename="export.csv",
                expected_fingerprint=plan1["plan_fingerprint"],
            )
            self.assertTrue(result["applied"])
            plan2 = _plan(conn, incoming)
            after = {
                "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
                "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
                "ccr": conn.execute("SELECT COUNT(*) FROM client_company_relationships").fetchone()[0],
                "notes_len": conn.execute(
                    "SELECT COUNT(*) FROM client_company_relationships WHERE notes LIKE '%First distinct note%'"
                ).fetchone()[0],
                "history": conn.execute("SELECT COUNT(*) FROM company_shared_history_events").fetchone()[0],
                "identities": conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0],
                "campaigns": conn.execute("SELECT COUNT(*) FROM campaign_companies").fetchone()[0],
            }
        self.assertEqual(_classes(plan2), [UNCHANGED])
        self.assertEqual(after["companies"], before["companies"] + 1)
        self.assertEqual(after["contacts"], before["contacts"] + 1)
        self.assertEqual(after["ccr"], before["ccr"] + 1)
        self.assertEqual(after["notes_len"], before["notes_len"] + 1)
        self.assertEqual(after["history"], before["history"] + 1)
        self.assertEqual(after["identities"], before["identities"] + 1)
        self.assertEqual(after["campaigns"], before["campaigns"])
        # Re-apply of the second (UNCHANGED) plan must not create more rows.
        with self.conn() as conn:
            apply_leadmaster_refresh(
                conn,
                incoming_rows=incoming,
                policy=_policy(),
                source_sha256=SHA,
                source_filename="export.csv",
                expected_fingerprint=plan2["plan_fingerprint"],
            )
            final_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        self.assertEqual(final_companies, after["companies"])

    def test_27_stale_plan(self):
        incoming = [_row()]
        with self.conn() as conn:
            plan = _plan(conn, incoming)
            with self.assertRaises(RefreshApplyError) as ctx:
                apply_leadmaster_refresh(
                    conn,
                    incoming_rows=incoming,
                    policy=_policy(),
                    source_sha256="different-source-sha",
                    source_filename="export.csv",
                    expected_fingerprint=plan["plan_fingerprint"],
                )
            self.assertEqual(str(ctx.exception), "stale_plan")
            mutated = dict(plan)
            mutated["source_filename"] = "other.csv"
            self.assertNotEqual(fingerprint_refresh_plan(mutated), plan["plan_fingerprint"])

    def test_28_atomic_rollback(self):
        incoming = [_row(
            record_no="1001", company_name="Rollback Co", city="Marquette",
            first_name="Roll", last_name="Back", email="r@rb.example", notes="x",
        )]
        with self.conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            plan = _plan(conn, incoming)
            with self.assertRaises(RefreshApplyError):
                apply_leadmaster_refresh(
                    conn,
                    incoming_rows=incoming,
                    policy=_policy(),
                    source_sha256=SHA,
                    source_filename="export.csv",
                    expected_fingerprint=plan["plan_fingerprint"],
                    force_fail_after="company",
                )
            after = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            idents = conn.execute(
                "SELECT COUNT(*) FROM company_source_identities WHERE source_record_no='1001'"
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(idents, 0)

    def test_29_fingerprint_deterministic(self):
        with self.conn() as conn:
            a = _plan(conn, [_row()])
            b = _plan(conn, [_row()])
        self.assertEqual(a["plan_fingerprint"], b["plan_fingerprint"])
        self.assertEqual(fingerprint_refresh_plan(a), a["plan_fingerprint"])

    def test_zz_write_isolated_results(self):
        OUT.mkdir(parents=True, exist_ok=True)
        payload = {
            "planner_version": PLANNER_VERSION,
            "test_db": str(Path(_TEST_DB)),
            "production_untouched": Path(_TEST_DB).resolve() != PRODUCTION_DB_PATH.resolve(),
            "cases": [
                "01 exact RN unchanged",
                "02 RN existing + new contact",
                "03 RN existing + contact new email additive",
                "04 RN existing + conflicting email",
                "05 valid status change propose_change",
                "06 blank status preserves",
                "07 invalid status review",
                "08 new note",
                "09 duplicate note",
                "10 new history",
                "11 duplicate history",
                "12 new company",
                "13 same name different location -> NEW_COMPANY",
                "14 same name no RN -> REVIEW name-only forbidden",
                "15 alias RN match",
                "16 source identity match",
                "17 source identity conflict refused apply",
                "18 Ronson 484 -> 425 merge_redirect",
                "19 exact NANP-10 phone",
                "20 last-seven POSSIBLE_DUPLICATE",
                "21 assignment change explicit map",
                "22 unknown rep",
                "23 active campaign",
                "24 historical campaign provenance only",
                "25 unknown campaign review",
                "26 duplicate rerun UNCHANGED",
                "27 stale plan refused",
                "28 atomic rollback",
            ],
        }
        (OUT / "isolated_test_results.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        self.assertTrue((OUT / "isolated_test_results.json").exists())


if __name__ == "__main__":
    unittest.main()
