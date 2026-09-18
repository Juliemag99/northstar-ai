"""LeadMaster refresh Phase 2 — mapping, policy, isolated preview.

Blank NORTHSTAR_TEST_DB. Never copies or writes live northstar.db.
Does not refresh Carmeco, Brown, or Dawson.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-lm-refresh-p2-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from crm_import_staging import BatchNotReusable, ensure_crm_import_schema  # noqa: E402
from db import PRODUCTION_DB_PATH, get_connection, init_schema, migrate_schema  # noqa: E402
from leadmaster_refresh_http import confirm_refresh_disabled, refresh_meta  # noqa: E402
from leadmaster_refresh_mapping import (  # noqa: E402
    NOT_MAPPED,
    normalize_mapping,
    validate_refresh_mapping,
)
from leadmaster_refresh_plan import (  # noqa: E402
    CAMPAIGN_CHANGE,
    IncomingRow,
    PLANNER_VERSION,
    RefreshPolicy,
    REVIEW_REQUIRED,
    UNKNOWN_SOURCE_REP,
    fingerprint_refresh_plan,
    plan_leadmaster_refresh,
)
from leadmaster_refresh_policy import (  # noqa: E402
    ASSIGN_IGNORED,
    ASSIGN_INACTIVE,
    ASSIGN_MAPPED,
    CAMPAIGN_ACTIVE,
    CAMPAIGN_IGNORED,
    MODE_LEADMASTER_REFRESH,
    STATUS_AUTHORITATIVE_NONBLANK,
    STATUS_PRESERVE_EXISTING,
    STATUS_PROPOSE_CHANGES,
    AssignmentMapEntry,
    CampaignMapEntry,
    RefreshPolicyProfile,
    default_policy,
    planner_status_mode,
)
from leadmaster_refresh_staging import (  # noqa: E402
    REFRESH_APPLY_DISABLED,
    RefreshApplyDisabled,
    RefreshLiveWriteError,
    assert_not_production_db,
    ensure_leadmaster_refresh_schema,
    refuse_refresh_as_initial_import,
    upload_leadmaster_refresh,
)
from models import NorthStarUser  # noqa: E402

CLIENT_ID = 1


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


def _csv(headers: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


class LeadMasterRefreshPhase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if Path(_TEST_DB).resolve() == PRODUCTION_DB_PATH.resolve():
            raise RuntimeError("Refusing Phase 2 tests against production")
        conn = get_connection()
        try:
            init_schema(conn)
            migrate_schema(conn)
            ensure_crm_import_schema(conn)
            ensure_leadmaster_refresh_schema(conn)
            conn.execute(
                "INSERT INTO clients (id, code, name) VALUES (1, 'testpilot', 'Test Pilot')"
            )
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, is_administrator, active)
                VALUES (10, 'admin@northstar.test', 'Admin Test', 1, 1),
                       (11, 'inactive@northstar.test', 'Inactive', 0, 0)
                """
            )
            conn.execute(
                """
                INSERT INTO companies (
                    id, external_record_no, company_name, city, state, zip,
                    created_at, last_updated_at
                ) VALUES (1, '100', 'Acme', 'Detroit', 'MI', '48201',
                          datetime('now'), datetime('now'))
                """
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, notes, assigned_user_id
                ) VALUES (1, 1, '100', 'New', 'Keep', 10)
                """
            )
            conn.execute(
                """
                INSERT INTO client_campaigns (id, client_id, campaign_name, status, is_active)
                VALUES (7, 1, 'Ops Q3', 'Active', 1)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_01_planner_version_v2(self):
        self.assertEqual(PLANNER_VERSION, "leadmaster-refresh-plan-v2")
        self.assertEqual(planner_status_mode(STATUS_PROPOSE_CHANGES), "propose_change")
        self.assertEqual(planner_status_mode(STATUS_PRESERVE_EXISTING), "preserve")
        self.assertEqual(planner_status_mode(STATUS_AUTHORITATIVE_NONBLANK), "authoritative")

    def test_02_optional_fields_not_mapped(self):
        ok, errors, mapping = validate_refresh_mapping(
            {
                "company_name": "Company",
                "campaign": NOT_MAPPED,
                "assigned_rep": "",
                "phone": "Phone",
            },
            ["Company", "Phone", "Campaign"],
        )
        self.assertTrue(ok, errors)
        self.assertEqual(normalize_mapping(mapping), {"company_name": "Company", "phone": "Phone"})
        self.assertNotIn("campaign", mapping)

    def test_03_policy_frozen_in_fingerprint(self):
        with get_connection() as conn:
            row = IncomingRow(source_row=1, record_no="100", company_name="Acme", city="Detroit")
            a = plan_leadmaster_refresh(
                conn, client_id=1, rows=[row],
                policy=RefreshPolicy(client_id=1, status_mode="propose_change"),
                source_sha256="aaa", source_filename="a.csv",
            )
            b = plan_leadmaster_refresh(
                conn, client_id=1, rows=[row],
                policy=RefreshPolicy(client_id=1, status_mode="preserve"),
                source_sha256="aaa", source_filename="a.csv",
            )
        self.assertNotEqual(a["plan_fingerprint"], b["plan_fingerprint"])
        self.assertEqual(fingerprint_refresh_plan(a), a["plan_fingerprint"])

    def test_04_assignment_states(self):
        profile = default_policy(1)
        profile.assignment_maps = [
            AssignmentMapEntry("Pat Rep", ASSIGN_MAPPED, 10),
            AssignmentMapEntry("Old Rep", ASSIGN_INACTIVE, 11),
            AssignmentMapEntry("Skip Me", ASSIGN_IGNORED),
        ]
        from leadmaster_refresh_plan import refresh_policy_from_profile, _assignment_proposal

        pol = refresh_policy_from_profile(profile)
        ccr = {"assigned_user_id": 10}
        mapped = _assignment_proposal(ccr, IncomingRow(source_row=1, assigned_rep="Pat Rep"), pol)
        self.assertEqual(mapped.action, "UNCHANGED_ASSIGNMENT")
        inactive = _assignment_proposal(ccr, IncomingRow(source_row=1, assigned_rep="Old Rep"), pol)
        self.assertEqual(inactive.action, "INACTIVE_TARGET")
        ignored = _assignment_proposal(ccr, IncomingRow(source_row=1, assigned_rep="Skip Me"), pol)
        self.assertEqual(ignored.action, "IGNORED")
        unknown = _assignment_proposal(ccr, IncomingRow(source_row=1, assigned_rep="Mystery"), pol)
        self.assertEqual(unknown.action, UNKNOWN_SOURCE_REP)

    def test_05_campaign_no_auto_create(self):
        with get_connection() as conn:
            incoming = IncomingRow(
                source_row=1, record_no="100", company_name="Acme", city="Detroit",
                campaign="Q3 Active",
            )
            no_target = plan_leadmaster_refresh(
                conn, client_id=1, rows=[incoming],
                policy=RefreshPolicy(
                    client_id=1,
                    campaign_class_map={"Q3 Active": CAMPAIGN_ACTIVE},
                ),
            )
            self.assertIn(REVIEW_REQUIRED, no_target["rows"][0]["classifications"])
            with_target = plan_leadmaster_refresh(
                conn, client_id=1, rows=[incoming],
                policy=RefreshPolicy(
                    client_id=1,
                    campaign_class_map={"Q3 Active": CAMPAIGN_ACTIVE},
                    campaign_target_map={"Q3 Active": 7},
                ),
            )
            self.assertIn(CAMPAIGN_CHANGE, with_target["rows"][0]["classifications"])
            ignored = plan_leadmaster_refresh(
                conn, client_id=1, rows=[incoming],
                policy=RefreshPolicy(
                    client_id=1,
                    campaign_class_map={"Q3 Active": CAMPAIGN_IGNORED},
                ),
            )
            actions = {p["action"] for p in ignored["rows"][0]["proposals"]}
            self.assertIn("IGNORED", actions)

    def test_06_profile_copied_into_batch_not_defaults_only(self):
        frozen = RefreshPolicyProfile.from_json(
            {"status_mode": STATUS_AUTHORITATIVE_NONBLANK, "client_id": 1},
            client_id=1,
        )
        self.assertEqual(frozen.status_mode, STATUS_AUTHORITATIVE_NONBLANK)
        later_default = default_policy(1)
        self.assertEqual(later_default.status_mode, STATUS_PROPOSE_CHANGES)
        self.assertNotEqual(frozen.fingerprint_slice(), later_default.fingerprint_slice())

    def test_07_upload_and_preview_isolated(self):
        content = _csv(
            ["Record No.", "Company", "Status", "Campaign"],
            [["100", "Acme", "Contacted", "Old List"]],
        )
        result = upload_leadmaster_refresh(
            client_id=CLIENT_ID,
            actor=_actor(),
            filename="lm.csv",
            content=content,
        )
        self.assertEqual(result["kind"], "previewed")
        self.assertEqual(result["batch"]["import_mode"], MODE_LEADMASTER_REFRESH)
        self.assertEqual(result["batch"]["source_system"], "LEADMASTER")
        from leadmaster_refresh_staging import save_refresh_mapping, plan_refresh_batch

        save_refresh_mapping(
            client_id=CLIENT_ID,
            batch_id=int(result["batch"]["batch_id"]),
            actor=_actor(),
            mapping={"external_record_no": "Record No.", "company_name": "Company",
                     "relationship_status": "Status", "campaign": "Campaign"},
        )
        with get_connection() as conn:
            plan = plan_refresh_batch(
                conn, client_id=CLIENT_ID, batch_id=int(result["batch"]["batch_id"])
            )
            self.assertTrue(plan["plan_fingerprint"])
            self.assertEqual(plan["import_mode"], MODE_LEADMASTER_REFRESH)
            self.assertIn("status_changes", plan["counts"])
            self.assertIn("duplicate_notes", plan["counts"])
            self.assertIn("excluded_notes", plan["counts"])
            self.assertIn("duplicate_history", plan["counts"])
            self.assertIn("history_conflicts", plan["counts"])

    def test_08_initial_import_refuses_refresh_batch(self):
        class _Row(dict):
            def keys(self):
                return super().keys()
        row = _Row(import_mode=MODE_LEADMASTER_REFRESH, status="previewed")
        with self.assertRaises(BatchNotReusable) as ctx:
            refuse_refresh_as_initial_import(row)
        self.assertIn("LeadMaster refresh", str(ctx.exception))

    def test_09_live_write_refused_when_path_is_production(self):
        with patch(
            "leadmaster_refresh_staging.PRODUCTION_DB_PATH",
            Path(_TEST_DB).resolve(),
        ):
            with self.assertRaises(RefreshLiveWriteError):
                assert_not_production_db()

    def test_10_apply_http_disabled(self):
        with self.assertRaises(RefreshApplyDisabled) as ctx:
            confirm_refresh_disabled()
        self.assertEqual(str(ctx.exception), REFRESH_APPLY_DISABLED)
        meta = refresh_meta()
        self.assertFalse(meta["apply_enabled"])
        self.assertFalse(meta["live_writes_enabled"])
        self.assertEqual(meta["source_system"], "LEADMASTER")

    def test_11_not_production_path(self):
        self.assertNotEqual(Path(_TEST_DB).resolve(), PRODUCTION_DB_PATH.resolve())
        assert_not_production_db()

    def test_12_get_view_does_not_alter_schema(self):
        content = _csv(["Company"], [["Acme"]])
        result = upload_leadmaster_refresh(
            client_id=CLIENT_ID,
            actor=_actor(),
            filename="view.csv",
            content=content,
        )
        from leadmaster_refresh_staging import refresh_batch_view

        with patch("leadmaster_refresh_staging.ensure_leadmaster_refresh_schema") as mocked:
            view = refresh_batch_view(CLIENT_ID, int(result["batch"]["batch_id"]))
        mocked.assert_not_called()
        self.assertEqual(view["import_mode"], MODE_LEADMASTER_REFRESH)

    def test_13_blank_filename_refused(self):
        with self.assertRaises(ValueError):
            upload_leadmaster_refresh(
                client_id=CLIENT_ID,
                actor=_actor(),
                filename="  ",
                content=b"Company\nAcme\n",
            )


if __name__ == "__main__":
    unittest.main()
