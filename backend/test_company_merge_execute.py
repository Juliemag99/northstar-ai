"""Phase 5F atomic merge executor tests.

Isolated testdb only. Does not write production northstar.db.
Does not merge live Ronson.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import unittest
from pathlib import Path

import testdb

from company_merge_execute import (
    MergeBlockedError,
    MergeExecutionError,
    MergeStalePlanError,
    assert_not_production_connection,
    execute_company_merge,
    merge_plan_fingerprint,
    stamp_merge_fingerprint,
)
from company_merges import plan_company_merge, resolve_company_id, resolve_contact_id
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema

TEST_REASON = "TEST FIXTURE — NOT JULIE'S BUSINESS DECISION"

RONSON_SOURCE = 484
RONSON_SURVIVOR = 425
PRE_RONSON_BACKUP = (
    PRODUCTION_DB_PATH.parent / "northstar.db.bak-master-data-std-phase5i-20260916-032738"
)


def _counts(conn, company_id: int) -> dict[str, int]:
    return {
        "contacts": int(
            conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE company_id = ?", (company_id,)
            ).fetchone()[0]
        ),
        "ccr": int(
            conn.execute(
                "SELECT COUNT(*) FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            ).fetchone()[0]
        ),
        "history": int(
            conn.execute(
                "SELECT COUNT(*) FROM company_shared_history_events WHERE company_id = ?",
                (company_id,),
            ).fetchone()[0]
        ),
        "legacy_notes": int(
            conn.execute(
                "SELECT COUNT(*) FROM legacy_notes WHERE company_id = ?", (company_id,)
            ).fetchone()[0]
        ),
        "identities": int(
            conn.execute(
                "SELECT COUNT(*) FROM company_source_identities WHERE company_id = ?",
                (company_id,),
            ).fetchone()[0]
        ),
        "exists": int(
            conn.execute("SELECT COUNT(*) FROM companies WHERE id = ?", (company_id,)).fetchone()[0]
        ),
    }


def _ronson_resolution(conn, **extra: object) -> tuple[dict, dict]:
    plan = plan_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR)
    resolution = {
        "source_company_id": RONSON_SOURCE,
        "survivor_company_id": RONSON_SURVIVOR,
        "plan_fingerprint": merge_plan_fingerprint(plan),
        "reason": TEST_REASON,
        "operation_id": "phase5f-isolated-ronson",
        "ccrs": {
            "2": {
                "status": "keep_survivor",
                "assigned_user_id": "keep_survivor",
                "notes": "union",
            }
        },
        "contacts": {
            "4054": {
                "action": "MERGE_DUPLICATE",
                "survivor_contact_id": 4468,
                "fields": {"first_name": "keep_survivor", "title": "keep_survivor"},
            },
            "4053": {
                "action": "MERGE_DUPLICATE",
                "survivor_contact_id": 4470,
            },
            "4051": {"action": "MOVE_UNIQUE"},
            "4049": {"action": "KEEP_SEPARATE"},
            "4050": {"action": "KEEP_SEPARATE"},
            "4052": {"action": "KEEP_SEPARATE"},
        },
    }
    resolution.update(extra)
    stamp_merge_fingerprint(plan, resolution)
    return plan, resolution


def _approved_ronson_resolution(conn, **extra: object) -> tuple[dict, dict]:
    """Julie-approved Ronson resolution from Phase 5G. Isolated tests only."""
    plan = plan_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR)
    resolution = {
        "source_company_id": RONSON_SOURCE,
        "survivor_company_id": RONSON_SURVIVOR,
        "reason": "Phase 5H isolated Ronson rehearsal — NOT LIVE",
        "operation_id": "phase5h-isolated-ronson",
        "ccrs": {
            "2": {
                "status": "keep_survivor",
                "assigned_user_id": "keep_survivor",
                "notes": "union",
            }
        },
        "contacts": {
            "4054": {
                "action": "MERGE_DUPLICATE",
                "survivor_contact_id": 4468,
                "fields": {
                    "first_name": "keep_survivor",
                    "title": "keep_survivor",
                    "email": "keep_source",
                    "alt_phone": "keep_source",
                },
            },
            "4053": {
                "action": "MERGE_DUPLICATE",
                "survivor_contact_id": 4470,
            },
            "4051": {"action": "MOVE_UNIQUE"},
            "4049": {"action": "MOVE_UNIQUE"},
            "4050": {
                "action": "MERGE_DUPLICATE",
                "survivor_contact_id": 4049,
            },
            "4052": {
                "action": "MERGE_DUPLICATE",
                "survivor_contact_id": 4049,
                "fields": {"email": "do_not_carry_from_source"},
            },
        },
        "identities": {
            "create": [
                {
                    "rn": "1326501",
                    "source_system": "crm_import",
                    "client_id": 2,
                    "source_company_name": "Ronson Manufacturing Corp",
                }
            ]
        },
        "company_fields": {
            "company_name": "keep_survivor",
            "city": "keep_survivor",
            "website": "keep_survivor",
        },
        "allow_distinct_site": False,
    }
    resolution.update(extra)
    stamp_merge_fingerprint(plan, resolution)
    return plan, resolution


def _restore_isolated_from_path(src_path: Path) -> None:
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(os.fspath(DB_PATH))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _restore_isolated_from_production() -> None:
    _restore_isolated_from_path(PRODUCTION_DB_PATH)


def _restore_isolated_pre_ronson() -> None:
    if not PRE_RONSON_BACKUP.exists():
        raise FileNotFoundError(f"Missing pre-Ronson backup: {PRE_RONSON_BACKUP}")
    _restore_isolated_from_path(PRE_RONSON_BACKUP)


class Phase5FAtomicMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        _restore_isolated_pre_ronson()
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def test_01_refuses_production_path(self) -> None:
        live = sqlite3.connect(
            f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro", uri=True
        )
        try:
            live.execute("PRAGMA query_only = ON")
            with self.assertRaises(MergeExecutionError):
                assert_not_production_connection(live)
            with self.assertRaises(MergeExecutionError) as ctx:
                execute_company_merge(
                    live,
                    RONSON_SOURCE,
                    RONSON_SURVIVOR,
                    {
                        "plan_fingerprint": "unused",
                        "source_company_id": RONSON_SOURCE,
                        "survivor_company_id": RONSON_SURVIVOR,
                    },
                )
            self.assertIn("live_approval_id", str(ctx.exception))
            with self.assertRaises(MergeExecutionError) as ctx2:
                execute_company_merge(
                    live,
                    757,
                    551,
                    {
                        "authorize_live_northstar_db": True,
                        "plan_fingerprint": "unused",
                        "source_company_id": 757,
                        "survivor_company_id": 551,
                    },
                )
            self.assertIn("live_approval_id", str(ctx2.exception))
            self.assertNotIn("Ronson", str(ctx2.exception))
        finally:
            live.close()

    def test_02_stage_a_blocked_zero_writes(self) -> None:
        with get_connection() as conn:
            before = _counts(conn, RONSON_SOURCE)
            before_s = _counts(conn, RONSON_SURVIVOR)
            plan = plan_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR)
            self.assertEqual(plan["verdict"], "MERGE BLOCKED")
            with self.assertRaises(MergeBlockedError):
                execute_company_merge(
                    conn,
                    RONSON_SOURCE,
                    RONSON_SURVIVOR,
                    {
                        "source_company_id": RONSON_SOURCE,
                        "survivor_company_id": RONSON_SURVIVOR,
                        "plan_fingerprint": merge_plan_fingerprint(plan),
                        "reason": TEST_REASON,
                    },
                )
            after = _counts(conn, RONSON_SOURCE)
            after_s = _counts(conn, RONSON_SURVIVOR)
            merges = int(conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0])
            contacts = int(conn.execute("SELECT COUNT(*) FROM contact_merge_history").fetchone()[0])
        self.assertEqual(before, after)
        self.assertEqual(before_s, after_s)
        self.assertEqual(merges, 0)
        self.assertEqual(contacts, 0)

    def test_03_other_cases_blocked(self) -> None:
        cases = (
            (757, 551, "Johnson Controls"),
            (192, 85, "Parker"),
            (383, 215, "Greenheck"),
            (337, 336, "Watlow 337"),
            (670, 336, "Watlow 670"),
        )
        with get_connection() as conn:
            for source, survivor, label in cases:
                plan = plan_company_merge(conn, source, survivor)
                self.assertEqual(plan["verdict"], "MERGE BLOCKED", label)
                before = int(
                    conn.execute("SELECT COUNT(*) FROM companies WHERE id = ?", (source,)).fetchone()[0]
                )
                with self.assertRaises(MergeBlockedError):
                    execute_company_merge(
                        conn,
                        source,
                        survivor,
                        {
                            "source_company_id": source,
                            "survivor_company_id": survivor,
                            "plan_fingerprint": merge_plan_fingerprint(plan),
                            "reason": TEST_REASON,
                        },
                    )
                after = int(
                    conn.execute("SELECT COUNT(*) FROM companies WHERE id = ?", (source,)).fetchone()[0]
                )
                self.assertEqual(before, after, label)
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]),
                0,
            )

    def test_04_stale_fingerprint(self) -> None:
        with get_connection() as conn:
            plan, resolution = _ronson_resolution(conn)
            resolution["plan_fingerprint"] = "0" * 64
            with self.assertRaises(MergeStalePlanError):
                execute_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR, resolution)
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM companies WHERE id = ?", (RONSON_SOURCE,)).fetchone()[0]),
                1,
            )

    def _assert_unmerged(self, conn) -> None:
        self.assertEqual(_counts(conn, RONSON_SOURCE)["exists"], 1)
        self.assertEqual(_counts(conn, RONSON_SURVIVOR)["exists"], 1)
        self.assertEqual(
            int(conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]),
            0,
        )
        self.assertEqual(
            int(conn.execute("SELECT COUNT(*) FROM contact_merge_history").fetchone()[0]),
            0,
        )
        self.assertEqual(
            int(conn.execute("SELECT COUNT(*) FROM contacts WHERE id = 4054").fetchone()[0]),
            1,
        )

    def test_05_forced_rollbacks(self) -> None:
        checkpoints = (
            "identities",
            "contacts",
            "ccrs",
            "campaigns",
            "history",
            "redirect",
        )
        with get_connection() as conn:
            before_src = _counts(conn, RONSON_SOURCE)
            before_dst = _counts(conn, RONSON_SURVIVOR)
            for point in checkpoints:
                _plan, resolution = _ronson_resolution(conn, test_fail_after=point)
                with self.assertRaises(MergeExecutionError):
                    execute_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR, resolution)
                self._assert_unmerged(conn)
                self.assertEqual(_counts(conn, RONSON_SOURCE), before_src)
                self.assertEqual(_counts(conn, RONSON_SURVIVOR)["contacts"], before_dst["contacts"])
                self.assertEqual(_counts(conn, RONSON_SURVIVOR)["history"], before_dst["history"])
                self.assertEqual(_counts(conn, RONSON_SURVIVOR)["legacy_notes"], before_dst["legacy_notes"])

    def test_06_stage_b_isolated_ronson_test_fixture(self) -> None:
        with get_connection() as conn:
            before_src = _counts(conn, RONSON_SOURCE)
            before_dst = _counts(conn, RONSON_SURVIVOR)
            plan, resolution = _ronson_resolution(conn)
            self.assertIn("TEST FIXTURE", resolution["reason"])
            result = execute_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR, resolution)
            after_src = _counts(conn, RONSON_SOURCE)
            after_dst = _counts(conn, RONSON_SURVIVOR)
            josh = conn.execute(
                "SELECT first_name, last_name, email, alt_phone, title FROM contacts WHERE id = 4468"
            ).fetchone()
            harold = conn.execute(
                "SELECT first_name, email FROM contacts WHERE id = 4470"
            ).fetchone()
            terry_n = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM contacts
                    WHERE company_id = ? AND last_name = 'Carver' AND first_name = 'Terry'
                    """,
                    (RONSON_SURVIVOR,),
                ).fetchone()[0]
            )
            doug = conn.execute(
                "SELECT company_id FROM contacts WHERE id = 4051"
            ).fetchone()
            kyle = conn.execute(
                "SELECT first_name FROM contacts WHERE id = 4469"
            ).fetchone()
            brown = conn.execute(
                """
                SELECT status, external_record_no FROM client_company_relationships
                WHERE client_id = 2 AND company_id = ?
                """,
                (RONSON_SURVIVOR,),
            ).fetchone()
            ident_rns = {
                r[0]
                for r in conn.execute(
                    "SELECT source_record_no FROM company_source_identities WHERE company_id = ?",
                    (RONSON_SURVIVOR,),
                ).fetchall()
            }
            note_units = after_dst["history"] + after_dst["legacy_notes"]
            master_rn = conn.execute(
                "SELECT external_record_no FROM companies WHERE id = ?",
                (RONSON_SURVIVOR,),
            ).fetchone()[0]
            resolved_company = resolve_company_id(conn, RONSON_SOURCE)
            resolved_josh = resolve_contact_id(conn, 4054)
            resolved_harold = resolve_contact_id(conn, 4053)

        self.assertTrue(result["ok"])
        self.assertEqual(after_src["exists"], 0)
        self.assertEqual(resolved_company, RONSON_SURVIVOR)
        self.assertEqual(resolved_josh, 4468)
        self.assertEqual(resolved_harold, 4470)
        # 7 survivor + Doug MOVE + 3 Terry KEEP - Joshua/Harold merged
        self.assertEqual(after_dst["contacts"], 11)
        self.assertEqual(before_dst["contacts"], 7)
        self.assertEqual(before_src["contacts"], 6)
        self.assertEqual(josh[0], "Josh")
        self.assertEqual(josh[2], "janderson@ronsonmfg.com")
        self.assertTrue(josh[3])
        self.assertEqual(harold[0], "Harold")
        self.assertEqual(int(doug[0]), RONSON_SURVIVOR)
        self.assertEqual(kyle[0], "Kyle")
        self.assertEqual(terry_n, 3)
        self.assertEqual(brown[0], "Current Customer")
        self.assertEqual(brown[1], "1219304")
        self.assertIn("99776", ident_rns)
        self.assertEqual(master_rn, "1219304")
        # 425 already had 7 shared-history rows + 1 legacy blob (= 8).
        # 484 adds 1 distinct client-scoped legacy blob. Final = 9.
        self.assertEqual(before_dst["history"], 7)
        self.assertEqual(before_dst["legacy_notes"], 1)
        self.assertEqual(before_src["legacy_notes"], 1)
        self.assertEqual(after_dst["history"], 7)
        self.assertEqual(after_dst["legacy_notes"], 2)
        self.assertEqual(note_units, 9)

    def test_07_source_side_decisions_change_fingerprint(self) -> None:
        with get_connection() as conn:
            _plan, keep = _ronson_resolution(conn)
            _plan2, approved = _approved_ronson_resolution(conn)
            self.assertNotEqual(keep["plan_fingerprint"], approved["plan_fingerprint"])
            changed = dict(approved)
            contacts = dict(changed["contacts"])
            contacts["4052"] = {
                "action": "MERGE_DUPLICATE",
                "survivor_contact_id": 4049,
                "fields": {"email": "keep_source"},
            }
            changed["contacts"] = contacts
            stamp_merge_fingerprint(_plan2, changed)
            self.assertNotEqual(approved["plan_fingerprint"], changed["plan_fingerprint"])

    def test_08_approved_source_side_ronson_isolated(self) -> None:
        with get_connection() as conn:
            before_src = _counts(conn, RONSON_SOURCE)
            before_dst = _counts(conn, RONSON_SURVIVOR)
            _plan, resolution = _approved_ronson_resolution(conn)
            result = execute_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR, resolution)
            after_src = _counts(conn, RONSON_SOURCE)
            after_dst = _counts(conn, RONSON_SURVIVOR)
            roster = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, first_name, last_name, title, email, phone, alt_phone, company_id
                    FROM contacts WHERE company_id = ?
                    ORDER BY last_name, first_name, id
                    """,
                    (RONSON_SURVIVOR,),
                ).fetchall()
            ]
            terry = conn.execute(
                "SELECT id, first_name, last_name, title, email, phone, company_id FROM contacts WHERE id = 4049"
            ).fetchone()
            kyle = conn.execute(
                "SELECT id, first_name, email, company_id FROM contacts WHERE id = 4469"
            ).fetchone()
            doug = conn.execute(
                "SELECT id, email, company_id FROM contacts WHERE id = 4051"
            ).fetchone()
            josh = conn.execute(
                "SELECT first_name, email, alt_phone FROM contacts WHERE id = 4468"
            ).fetchone()
            ident_rns = {
                r[0]
                for r in conn.execute(
                    "SELECT source_record_no FROM company_source_identities WHERE company_id = ?",
                    (RONSON_SURVIVOR,),
                ).fetchall()
            }
            brown = conn.execute(
                """
                SELECT COUNT(*), MAX(status), MAX(external_record_no)
                FROM client_company_relationships
                WHERE client_id = 2 AND company_id = ?
                """,
                (RONSON_SURVIVOR,),
            ).fetchone()
            loc_n = int(
                conn.execute(
                    "SELECT COUNT(*) FROM company_locations WHERE company_id = ?",
                    (RONSON_SURVIVOR,),
                ).fetchone()[0]
            )
            location_id = result.get("location_id")
            note_units = after_dst["history"] + after_dst["legacy_notes"]
            gone = [
                int(conn.execute("SELECT COUNT(*) FROM contacts WHERE id = ?", (cid,)).fetchone()[0])
                for cid in (4050, 4052, 4054, 4053)
            ]
            resolved_company = resolve_company_id(conn, RONSON_SOURCE)
            resolved_josh = resolve_contact_id(conn, 4054)
            resolved_harold = resolve_contact_id(conn, 4053)
            resolved_t4050 = resolve_contact_id(conn, 4050)
            resolved_t4052 = resolve_contact_id(conn, 4052)
            resolved_t4049 = resolve_contact_id(conn, 4049)
            resolved_doug = resolve_contact_id(conn, 4051)

        self.assertTrue(result["ok"])
        self.assertEqual(after_src["exists"], 0)
        self.assertEqual(resolved_company, RONSON_SURVIVOR)
        self.assertEqual(len(roster), 9)
        self.assertEqual({r["id"] for r in roster}, {4468, 4472, 4466, 4469, 4471, 4470, 4467, 4051, 4049})
        self.assertEqual(gone, [0, 0, 0, 0])
        self.assertEqual(resolved_josh, 4468)
        self.assertEqual(resolved_harold, 4470)
        self.assertEqual(resolved_t4050, 4049)
        self.assertEqual(resolved_t4052, 4049)
        self.assertEqual(resolved_t4049, 4049)
        self.assertEqual(resolved_doug, 4051)
        self.assertEqual(int(terry[0]), 4049)
        self.assertEqual(terry[1], "Terry")
        self.assertEqual(terry[2], "Carver")
        self.assertEqual(terry[3], "President")
        self.assertEqual((terry[4] or "").strip(), "")
        self.assertEqual(terry[5], "(816) 373-2720")
        self.assertEqual(int(terry[6]), RONSON_SURVIVOR)
        self.assertEqual(kyle[1], "Kyle")
        self.assertEqual(kyle[2], "kcarver@ronsonmfg.com")
        self.assertEqual(int(kyle[3]), RONSON_SURVIVOR)
        self.assertEqual(doug[1], "purchasing@ronsonmfg.com")
        self.assertEqual(int(doug[2]), RONSON_SURVIVOR)
        self.assertEqual(josh[0], "Josh")
        self.assertEqual(josh[1], "janderson@ronsonmfg.com")
        self.assertTrue(josh[2])
        self.assertIn("99776", ident_rns)
        self.assertIn("1326501", ident_rns)
        self.assertEqual(brown[0], 1)
        self.assertEqual(brown[1], "Current Customer")
        self.assertEqual(brown[2], "1219304")
        self.assertTrue(location_id, f"expected a SAME_SITE location, got {location_id!r}")
        self.assertEqual(loc_n, 1)
        self.assertEqual(before_dst["history"], 7)
        self.assertEqual(before_src["legacy_notes"], 1)
        self.assertEqual(after_dst["history"], 7)
        self.assertEqual(after_dst["legacy_notes"], 2)
        self.assertEqual(note_units, 9)

    def test_09_source_side_forced_rollbacks(self) -> None:
        checkpoints = (
            "source_merge:4050",
            "source_merge:4052",
            "contact_redirects",
            "contact_move:4049",
            "identities",
            "contacts",
            "ccrs",
            "campaigns",
            "history",
            "redirect",
        )
        with get_connection() as conn:
            before_src = _counts(conn, RONSON_SOURCE)
            before_dst = _counts(conn, RONSON_SURVIVOR)
            terry_before = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, company_id, first_name, last_name, email
                    FROM contacts WHERE id IN (4049,4050,4052) ORDER BY id
                    """
                ).fetchall()
            ]
            for point in checkpoints:
                _plan, resolution = _approved_ronson_resolution(conn, test_fail_after=point)
                with self.assertRaises(MergeExecutionError):
                    execute_company_merge(conn, RONSON_SOURCE, RONSON_SURVIVOR, resolution)
                self._assert_unmerged(conn)
                self.assertEqual(_counts(conn, RONSON_SOURCE), before_src)
                self.assertEqual(_counts(conn, RONSON_SURVIVOR)["contacts"], before_dst["contacts"])
                self.assertEqual(_counts(conn, RONSON_SURVIVOR)["legacy_notes"], before_dst["legacy_notes"])
                after_terry = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT id, company_id, first_name, last_name, email
                        FROM contacts WHERE id IN (4049,4050,4052) ORDER BY id
                        """
                    ).fetchall()
                ]
                self.assertEqual(after_terry, terry_before, point)
                self.assertEqual(
                    int(conn.execute("SELECT COUNT(*) FROM contact_merge_history").fetchone()[0]),
                    0,
                    point,
                )

    def test_10_synthetic_source_side_cluster(self) -> None:
        with get_connection() as conn:
            src = _insert_company(conn, "Source Cluster Co")
            dst = _insert_company(conn, "Survivor Cluster Co")
            unrelated = _insert_contact(conn, dst, first_name="Pat", last_name="Unrelated", email="pat@ex.test")
            a = _insert_contact(conn, src, first_name="Alex", last_name="Same", email="alex@ex.test")
            b = _insert_contact(conn, src, first_name="Alex", last_name="Same", email="")
            c = _insert_contact(conn, src, first_name="Alex", last_name="Same", email="skip@ex.test")
            client_id = _client_id(conn)
            src_ccr = _insert_ccr(conn, client_id, src, "New")
            dst_ccr = _insert_ccr(conn, client_id, dst, "Current Customer")
            campaign_id = int(
                conn.execute(
                    """
                    INSERT INTO client_campaigns (client_id, campaign_name)
                    VALUES (?, 'Synthetic Cluster')
                    """,
                    (client_id,),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO campaign_contacts (campaign_id, client_id, contact_id, company_id, notes)
                VALUES (?, ?, ?, ?, 'from A'), (?, ?, ?, ?, 'from B')
                """,
                (campaign_id, client_id, a, src, campaign_id, client_id, b, src),
            )
            conn.execute(
                """
                INSERT INTO campaign_contacts (campaign_id, client_id, contact_id, company_id, notes)
                VALUES (?, ?, ?, ?, 'from C')
                """,
                (campaign_id, client_id, c, src),
            )
            conn.execute(
                """
                INSERT INTO activities (
                    client_id, company_id, relationship_id, external_record_no,
                    contact_id, activity_type, activity_at, notes
                ) VALUES (?, ?, ?, 'SYN-A', ?, 'call', datetime('now'), 'A activity'),
                         (?, ?, ?, 'SYN-B', ?, 'call', datetime('now'), 'B activity'),
                         (?, ?, ?, 'SYN-C', ?, 'call', datetime('now'), 'C activity')
                """,
                (
                    client_id, src, src_ccr, a,
                    client_id, src, src_ccr, b,
                    client_id, src, src_ccr, c,
                ),
            )
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, 'source blob', 'notes'), (?, ?, 'survivor blob', 'notes')
                """,
                (client_id, src, client_id, dst),
            )
            conn.commit()
            plan = plan_company_merge(conn, src, dst)
            resolution = {
                "source_company_id": src,
                "survivor_company_id": dst,
                "reason": "synthetic source-side cluster",
                "allow_location_review": True,
                "ccrs": {"%s" % client_id: {"status": "keep_survivor"}},
                "contacts": {
                    str(b): {
                        "action": "MERGE_DUPLICATE",
                        "survivor_contact_id": a,
                    },
                    str(c): {
                        "action": "MERGE_DUPLICATE",
                        "survivor_contact_id": a,
                        "fields": {"email": "do_not_carry_from_source"},
                    },
                    str(a): {"action": "MOVE_UNIQUE"},
                },
            }
            stamp_merge_fingerprint(plan, resolution)
            result = execute_company_merge(conn, src, dst, resolution)
            self.assertTrue(result["ok"])
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM companies WHERE id = ?", (src,)).fetchone()[0]),
                0,
            )
            self.assertEqual(resolve_company_id(conn, src), dst)
            self.assertEqual(resolve_contact_id(conn, b), a)
            self.assertEqual(resolve_contact_id(conn, c), a)
            self.assertEqual(resolve_contact_id(conn, a), a)
            moved = conn.execute(
                "SELECT company_id, email FROM contacts WHERE id = ?", (a,)
            ).fetchone()
            self.assertEqual(int(moved[0]), dst)
            self.assertEqual(moved[1], "alex@ex.test")
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM contacts WHERE id IN (?, ?)", (b, c)).fetchone()[0]),
                0,
            )
            self.assertEqual(
                int(conn.execute("SELECT company_id FROM contacts WHERE id = ?", (unrelated,)).fetchone()[0]),
                dst,
            )
            self.assertEqual(
                conn.execute("SELECT first_name FROM contacts WHERE id = ?", (unrelated,)).fetchone()[0],
                "Pat",
            )
            camp_notes = {
                str(r[0] or "").strip()
                for r in conn.execute(
                    "SELECT notes FROM campaign_contacts WHERE contact_id = ?", (a,)
                ).fetchall()
            }
            self.assertTrue(any("from A" in n for n in camp_notes) or any("from B" in n for n in camp_notes))
            act_n = int(
                conn.execute(
                    "SELECT COUNT(*) FROM activities WHERE contact_id = ?", (a,)
                ).fetchone()[0]
            )
            self.assertEqual(act_n, 3)
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM legacy_notes WHERE company_id = ?", (dst,)).fetchone()[0]),
                2,
            )

    def test_11_redirect_chain_flatten(self) -> None:
        with get_connection() as conn:
            src = _insert_company(conn, "Chain Source")
            dst = _insert_company(conn, "Chain Survivor")
            a = _insert_contact(conn, src, first_name="Final", last_name="Person")
            mid = _insert_contact(conn, src, first_name="Mid", last_name="Person")
            old = _insert_contact(conn, src, first_name="Old", last_name="Person")
            client_id = _client_id(conn)
            _insert_ccr(conn, client_id, src, "New")
            _insert_ccr(conn, client_id, dst, "Current Customer")
            conn.commit()
            plan = plan_company_merge(conn, src, dst)
            resolution = {
                "source_company_id": src,
                "survivor_company_id": dst,
                "reason": "synthetic redirect chain",
                "allow_location_review": True,
                "ccrs": {str(client_id): {"status": "keep_survivor"}},
                "contacts": {
                    str(old): {
                        "action": "MERGE_DUPLICATE",
                        "survivor_contact_id": mid,
                        "fields": {"first_name": "keep_survivor", "last_name": "keep_survivor"},
                    },
                    str(mid): {
                        "action": "MERGE_DUPLICATE",
                        "survivor_contact_id": a,
                        "fields": {"first_name": "keep_survivor", "last_name": "keep_survivor"},
                    },
                    str(a): {"action": "MOVE_UNIQUE"},
                },
            }
            stamp_merge_fingerprint(plan, resolution)
            execute_company_merge(conn, src, dst, resolution)
            self.assertEqual(resolve_contact_id(conn, old), a)
            self.assertEqual(resolve_contact_id(conn, mid), a)
            self.assertEqual(resolve_contact_id(conn, a), a)
            self.assertEqual(
                int(conn.execute("SELECT company_id FROM contacts WHERE id = ?", (a,)).fetchone()[0]),
                dst,
            )
            inbound = [
                int(r[0])
                for r in conn.execute(
                    "SELECT source_contact_id FROM contact_merge_history WHERE survivor_contact_id = ? ORDER BY source_contact_id",
                    (a,),
                ).fetchall()
            ]
            self.assertEqual(inbound, sorted([old, mid]))


class Phase5JPostRonsonRegressionTests(unittest.TestCase):
    """Current live copy after 5I. Does not recreate company 484."""

    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        _restore_isolated_from_production()
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def test_ronson_redirects_and_roster(self) -> None:
        with get_connection() as conn:
            self.assertEqual(resolve_company_id(conn, 484), 425)
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM companies WHERE id = 484").fetchone()[0]),
                0,
            )
            name, rn = conn.execute(
                "SELECT company_name, external_record_no FROM companies WHERE id = 425"
            ).fetchone()
            self.assertEqual(name, "Ronson Manufacturing")
            self.assertEqual(rn, "1219304")
            roster = [
                (int(r[0]), r[1], r[2])
                for r in conn.execute(
                    """
                    SELECT id, first_name, last_name
                    FROM contacts WHERE company_id = 425
                    ORDER BY last_name, first_name, id
                    """
                )
            ]
            self.assertEqual(len(roster), 9)
            self.assertEqual(
                {row[0] for row in roster},
                {4468, 4472, 4466, 4469, 4471, 4470, 4467, 4051, 4049},
            )
            self.assertEqual(resolve_contact_id(conn, 4054), 4468)
            self.assertEqual(resolve_contact_id(conn, 4053), 4470)
            self.assertEqual(resolve_contact_id(conn, 4050), 4049)
            self.assertEqual(resolve_contact_id(conn, 4052), 4049)
            terry_email = conn.execute(
                "SELECT email FROM contacts WHERE id = 4049"
            ).fetchone()[0]
            self.assertEqual((terry_email or "").strip(), "")
            brown = conn.execute(
                """
                SELECT COUNT(*), MAX(status), MAX(external_record_no)
                FROM client_company_relationships
                WHERE client_id = 2 AND company_id = 425
                """
            ).fetchone()
            self.assertEqual(int(brown[0]), 1)
            self.assertEqual(brown[1], "Current Customer")
            self.assertEqual(brown[2], "1219304")
            notes = int(
                conn.execute(
                    "SELECT COUNT(*) FROM company_shared_history_events WHERE company_id = 425"
                ).fetchone()[0]
            ) + int(
                conn.execute(
                    "SELECT COUNT(*) FROM legacy_notes WHERE company_id = 425"
                ).fetchone()[0]
            )
            self.assertEqual(notes, 9)
            ident = {
                r[0]
                for r in conn.execute(
                    "SELECT source_record_no FROM company_source_identities WHERE company_id = 425"
                )
            }
            self.assertIn("99776", ident)
            self.assertIn("1326501", ident)
            loc_n = int(
                conn.execute(
                    "SELECT COUNT(*) FROM company_locations WHERE company_id = 425"
                ).fetchone()[0]
            )
            self.assertEqual(loc_n, 1)
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]),
                1,
            )
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM contact_merge_history").fetchone()[0]),
                4,
            )
            with self.assertRaises(MergeExecutionError):
                execute_company_merge(
                    conn,
                    484,
                    425,
                    {
                        "source_company_id": 484,
                        "survivor_company_id": 425,
                        "plan_fingerprint": "x",
                    },
                )


def _insert_company(conn, name: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO companies (external_record_no, company_name, created_at, last_updated_at)
        VALUES (?, ?, datetime('now'), datetime('now'))
        """,
        (f"P5H-{secrets.token_hex(6)}", name),
    )
    return int(cur.lastrowid)


def _insert_contact(conn, company_id: int, **fields: object) -> int:
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name, title,
            phone, alt_phone, email, source_row_index, created_at
        ) VALUES (?, ?, ?, ?, '', '', '', ?, 0, datetime('now'))
        """,
        (
            int(company_id),
            str(fields.get("external_record_no") or f"P5H-C-{secrets.token_hex(4)}"),
            str(fields.get("first_name") or ""),
            str(fields.get("last_name") or ""),
            str(fields.get("email") or ""),
        ),
    )
    return int(cur.lastrowid)


def _client_id(conn) -> int:
    row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise AssertionError("Isolated testdb has no clients.")
    return int(row[0])


def _insert_ccr(conn, client_id: int, company_id: int, status: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, status, created_at, updated_at, external_record_no
        ) VALUES (?, ?, ?, datetime('now'), datetime('now'), ?)
        """,
        (int(client_id), int(company_id), status, f"P5H-CCR-{company_id}"),
    )
    return int(cur.lastrowid)


if __name__ == "__main__":
    unittest.main()
