"""Phase 5J merge authorization tests. Isolated testdb only."""
from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path

import testdb

from company_merge_approvals import (
    MergeApprovalError,
    claim_merge_approval,
    create_merge_approval,
    load_merge_approval,
)
from company_merge_execute import (
    MergeExecutionError,
    MergeStalePlanError,
    execute_company_merge,
    merge_plan_fingerprint,
    stamp_merge_fingerprint,
)
from company_merges import plan_company_merge, resolve_company_id, resolve_contact_id
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from test_company_merge_execute import (
    _insert_ccr,
    _insert_company,
    _insert_contact,
    _client_id,
    _restore_isolated_from_production,
)

TEST_REASON = "TEST FIXTURE — NOT JULIE'S BUSINESS DECISION"


class Phase5JMergeAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        _restore_isolated_from_production()
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def _pair(self, conn):
        src = _insert_company(conn, "Auth Source Co")
        dst = _insert_company(conn, "Auth Survivor Co")
        conn.execute(
            "UPDATE companies SET address=?, city=?, state=?, zip=? WHERE id=?",
            ("100 Main St", "Ames", "IA", "50010", dst),
        )
        conn.execute(
            "UPDATE companies SET address=?, city=?, state=?, zip=? WHERE id=?",
            ("100 Main St", "Ames", "IA", "50010", src),
        )
        a = _insert_contact(conn, src, first_name="Ann", last_name="Auth", email="ann@ex.test")
        b = _insert_contact(conn, dst, first_name="Ann", last_name="Auth", email="ann@ex.test")
        client_id = _client_id(conn)
        _insert_ccr(conn, client_id, src, "New")
        _insert_ccr(conn, client_id, dst, "Current Customer")
        conn.execute(
            "INSERT INTO legacy_notes (client_id, company_id, note_text) VALUES (?, ?, ?), (?, ?, ?)",
            (client_id, src, "source note unit", client_id, dst, "survivor note unit"),
        )
        campaign_id = int(
            conn.execute(
                "INSERT INTO client_campaigns (client_id, campaign_name) VALUES (?, 'Auth Camp')",
                (client_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO campaign_companies (campaign_id, client_id, company_id, notes)
            VALUES (?, ?, ?, 'src-member')
            """,
            (campaign_id, client_id, src),
        )
        conn.commit()
        plan = plan_company_merge(conn, src, dst)
        resolution = {
            "source_company_id": src,
            "survivor_company_id": dst,
            "reason": TEST_REASON,
            "allow_location_review": True,
            "ccrs": {str(client_id): {"status": "keep_survivor"}},
            "contacts": {
                str(a): {
                    "action": "MERGE_DUPLICATE",
                    "survivor_contact_id": b,
                }
            },
        }
        stamp_merge_fingerprint(plan, resolution)
        return src, dst, a, b, client_id, campaign_id, plan, resolution

    def test_01_refuses_create_approval_on_live(self) -> None:
        live = sqlite3.connect(
            f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro", uri=True
        )
        try:
            live.execute("PRAGMA query_only = ON")
            with self.assertRaises(MergeApprovalError):
                create_merge_approval(
                    live,
                    source_company_id=1,
                    survivor_company_id=2,
                    plan_fingerprint="abc",
                )
        finally:
            live.close()

    def test_02_boolean_does_not_authorize_live(self) -> None:
        live = sqlite3.connect(
            f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro", uri=True
        )
        try:
            live.execute("PRAGMA query_only = ON")
            with self.assertRaises(MergeExecutionError) as ctx:
                execute_company_merge(
                    live,
                    1,
                    2,
                    {
                        "authorize_live_northstar_db": True,
                        "ALLOW_LIVE_MERGE": True,
                        "plan_fingerprint": "x",
                        "source_company_id": 1,
                        "survivor_company_id": 2,
                    },
                )
            self.assertIn("live_approval_id", str(ctx.exception))
        finally:
            live.close()

    def test_03_preview_approve_execute_consume_replay(self) -> None:
        with get_connection() as conn:
            src, dst, a, b, client_id, campaign_id, plan, resolution = self._pair(conn)
            approval_id = create_merge_approval(
                conn,
                source_company_id=src,
                survivor_company_id=dst,
                plan_fingerprint=resolution["plan_fingerprint"],
                approved_resolution=resolution,
                approved_by_user_id=None,
            )
            resolution["live_approval_id"] = approval_id
            result = execute_company_merge(conn, src, dst, resolution)
            self.assertTrue(result["ok"])
            self.assertEqual(resolve_company_id(conn, src), dst)
            self.assertEqual(resolve_contact_id(conn, a), b)
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM companies WHERE id=?", (src,)).fetchone()[0]),
                0,
            )
            notes = int(
                conn.execute(
                    "SELECT COUNT(*) FROM legacy_notes WHERE company_id=?", (dst,)
                ).fetchone()[0]
            )
            self.assertEqual(notes, 2)
            ccr_n = int(
                conn.execute(
                    "SELECT COUNT(*) FROM client_company_relationships WHERE company_id=?",
                    (dst,),
                ).fetchone()[0]
            )
            self.assertEqual(ccr_n, 1)
            camp = int(
                conn.execute(
                    "SELECT COUNT(*) FROM campaign_companies WHERE company_id=?",
                    (dst,),
                ).fetchone()[0]
            )
            self.assertEqual(camp, 1)
            row = load_merge_approval(conn, approval_id)
            self.assertEqual(row["execution_status"], "executed")
            self.assertEqual(int(row["company_merge_history_id"]), int(result["redirect_id"]))
            with self.assertRaises(MergeApprovalError):
                claim_merge_approval(
                    conn,
                    approval_id,
                    source_company_id=src,
                    survivor_company_id=dst,
                    plan_fingerprint=resolution["plan_fingerprint"],
                )
            with self.assertRaises(MergeExecutionError):
                execute_company_merge(conn, src, dst, resolution)
            row2 = load_merge_approval(conn, approval_id)
            self.assertEqual(row2["execution_status"], "executed")

    def test_04_stale_fingerprint_after_approval(self) -> None:
        with get_connection() as conn:
            src, dst, a, b, client_id, campaign_id, plan, resolution = self._pair(conn)
            approval_id = create_merge_approval(
                conn,
                source_company_id=src,
                survivor_company_id=dst,
                plan_fingerprint=resolution["plan_fingerprint"],
                approved_resolution=resolution,
            )
            conn.execute(
                "UPDATE contacts SET email = 'changed@ex.test' WHERE id = ?",
                (a,),
            )
            conn.commit()
            resolution["live_approval_id"] = approval_id
            with self.assertRaises(MergeStalePlanError):
                execute_company_merge(conn, src, dst, resolution)
            row = load_merge_approval(conn, approval_id)
            self.assertEqual(row["execution_status"], "superseded")
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM companies WHERE id=?", (src,)).fetchone()[0]),
                1,
            )

    def test_05_wrong_pair_blocked(self) -> None:
        with get_connection() as conn:
            src, dst, a, b, client_id, campaign_id, plan, resolution = self._pair(conn)
            other = _insert_company(conn, "Other Co")
            conn.commit()
            approval_id = create_merge_approval(
                conn,
                source_company_id=src,
                survivor_company_id=dst,
                plan_fingerprint=resolution["plan_fingerprint"],
                approved_resolution=resolution,
            )
            other_plan = plan_company_merge(conn, src, other)
            other_res = {
                "source_company_id": src,
                "survivor_company_id": other,
                "reason": TEST_REASON,
                "allow_location_review": True,
                "live_approval_id": approval_id,
                "contacts": {str(a): {"action": "MOVE_UNIQUE"}},
            }
            stamp_merge_fingerprint(other_plan, other_res)
            with self.assertRaises(MergeExecutionError) as ctx:
                execute_company_merge(conn, src, other, other_res)
            self.assertIn("source/survivor", str(ctx.exception))
            row = load_merge_approval(conn, approval_id)
            self.assertEqual(row["execution_status"], "pending")

    def test_06_forced_failure_marks_failed_not_replayable(self) -> None:
        with get_connection() as conn:
            src, dst, a, b, client_id, campaign_id, plan, resolution = self._pair(conn)
            approval_id = create_merge_approval(
                conn,
                source_company_id=src,
                survivor_company_id=dst,
                plan_fingerprint=resolution["plan_fingerprint"],
                approved_resolution=resolution,
            )
            resolution["live_approval_id"] = approval_id
            resolution["test_fail_after"] = "contacts"
            with self.assertRaises(MergeExecutionError):
                execute_company_merge(conn, src, dst, resolution)
            row = load_merge_approval(conn, approval_id)
            self.assertEqual(row["execution_status"], "failed")
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM companies WHERE id=?", (src,)).fetchone()[0]),
                1,
            )
            del resolution["test_fail_after"]
            with self.assertRaises(MergeExecutionError):
                execute_company_merge(conn, src, dst, resolution)
            row2 = load_merge_approval(conn, approval_id)
            self.assertEqual(row2["execution_status"], "failed")

    def test_07_wrong_fingerprint_on_approval(self) -> None:
        with get_connection() as conn:
            src, dst, a, b, client_id, campaign_id, plan, resolution = self._pair(conn)
            approval_id = create_merge_approval(
                conn,
                source_company_id=src,
                survivor_company_id=dst,
                plan_fingerprint="0" * 64,
                approved_resolution=resolution,
            )
            resolution["live_approval_id"] = approval_id
            with self.assertRaises(MergeExecutionError):
                execute_company_merge(conn, src, dst, resolution)
            self.assertEqual(load_merge_approval(conn, approval_id)["execution_status"], "superseded")


if __name__ == "__main__":
    unittest.main()
