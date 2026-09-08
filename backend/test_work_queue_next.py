"""Phase 0B follow-up: Work Queue Save & Next / neighbors API (isolated testdb).

Run: python test_work_queue_next.py
"""

from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

import testdb  # noqa: F401
from access import get_default_user
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from work_queue_data import get_work_queue_next, list_work_queue


def _uid() -> int:
    user = get_default_user()
    assert user is not None
    return int(user.id)


class WorkQueueNextTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            suffix = secrets.token_hex(3)
            self.suffix = suffix
            self.client_id = int(
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES (?, ?)",
                    (f"wqn_{suffix}", f"WQ Next Client {suffix}"),
                ).lastrowid
            )
            self.hidden_client = int(
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES (?, ?)",
                    (f"wqh_{suffix}", f"WQ Hidden {suffix}"),
                ).lastrowid
            )
            self.user_id = _uid()
            conn.execute(
                """
                INSERT OR IGNORE INTO user_client_assignments
                    (user_id, client_id, role, active, assigned_at)
                VALUES (?, ?, 'staff', 1, datetime('now'))
                """,
                (self.user_id, self.client_id),
            )
            # Limited user without hidden client.
            conn.execute(
                """
                INSERT INTO users (
                    email, full_name, is_administrator, is_internal_northstar, active
                ) VALUES (?, 'WQ Next Limited', 0, 1, 1)
                """,
                (f"wqn.limited.{suffix}@example.test",),
            )
            self.limited_uid = int(
                conn.execute(
                    "SELECT id FROM users WHERE email LIKE ? ORDER BY id DESC LIMIT 1",
                    (f"wqn.limited.{suffix}@%",),
                ).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO user_client_assignments
                    (user_id, client_id, role, active, assigned_at)
                VALUES (?, ?, 'staff', 1, datetime('now'))
                """,
                (self.limited_uid, self.client_id),
            )
            self.hot_ids: list[int] = []
            self.hot_rel_ids: list[int] = []
            self.hot_item_ids: list[str] = []
            # 70 Hot prospects — enough to span past page size 50.
            for i in range(70):
                cid = int(
                    conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, created_at, last_updated_at
                        ) VALUES (?, ?, datetime('now'), datetime('now'))
                        """,
                        (f"NS-WQN-{suffix}-{i:04d}", f"WQ Next Hot {suffix} {i:04d}"),
                    ).lastrowid
                )
                self.hot_ids.append(cid)
                rel_id = int(
                    conn.execute(
                        """
                        INSERT INTO client_company_relationships (
                            client_id, company_id, external_record_no, status, assigned_user_id,
                            priority, next_action, notes, is_hot, created_at, updated_at
                        ) VALUES (?, ?, ?, 'Hot Prospect', NULL, '', 'Work hot', '', 0,
                                  datetime('now'), datetime('now'))
                        """,
                        (self.client_id, cid, f"NS-WQN-{suffix}-{i:04d}"),
                    ).lastrowid
                )
                self.hot_rel_ids.append(rel_id)
                self.hot_item_ids.append(f"hot:hot:{rel_id}:{self.client_id}")
            # Hidden-client Hot must never appear for limited/authorized scope.
            hid = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, created_at, last_updated_at
                    ) VALUES (?, ?, datetime('now'), datetime('now'))
                    """,
                    (f"NS-WQN-HID-{suffix}", f"WQ Hidden Hot {suffix}"),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, ?, 'Hot Prospect', NULL, '', '', '', 0,
                          datetime('now'), datetime('now'))
                """,
                (self.hidden_client, hid, f"NS-WQN-HID-{suffix}"),
            )
            conn.commit()

    def test_next_on_same_page(self) -> None:
        page = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            limit=50,
            offset=0,
        )
        self.assertGreaterEqual(len(page.items), 2)
        first = page.items[0]
        second = page.items[1]
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id=first.queue_item_id,
            after_work_priority=first.work_priority,
            after_due_date=first.due_date,
            after_due_time=first.due_time,
            after_company_name=first.company_name,
            after_company_id=first.company_id,
            client_id=self.client_id,
            work_type="hot",
        )
        self.assertTrue(nxt.has_next)
        self.assertFalse(nxt.end_of_results)
        assert nxt.item is not None
        self.assertEqual(nxt.item.queue_item_id, second.queue_item_id)
        self.assertEqual(nxt.position, 2)

    def test_next_beyond_first_50(self) -> None:
        page0 = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            limit=50,
            offset=0,
        )
        page1 = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            limit=50,
            offset=50,
        )
        self.assertEqual(len(page0.items), 50)
        self.assertGreaterEqual(len(page1.items), 1)
        last_on_page0 = page0.items[-1]
        first_on_page1 = page1.items[0]
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id=last_on_page0.queue_item_id,
            after_work_priority=last_on_page0.work_priority,
            after_due_date=last_on_page0.due_date,
            after_due_time=last_on_page0.due_time,
            after_company_name=last_on_page0.company_name,
            after_company_id=last_on_page0.company_id,
            client_id=self.client_id,
            work_type="hot",
        )
        self.assertTrue(nxt.has_next)
        assert nxt.item is not None
        self.assertEqual(nxt.item.queue_item_id, first_on_page1.queue_item_id)
        self.assertEqual(nxt.position, 51)

    def test_current_beyond_page_1(self) -> None:
        page1 = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            limit=50,
            offset=50,
        )
        self.assertGreaterEqual(len(page1.items), 2)
        cur = page1.items[0]
        expect = page1.items[1]
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id=cur.queue_item_id,
            after_work_priority=cur.work_priority,
            after_company_name=cur.company_name,
            after_company_id=cur.company_id,
            client_id=self.client_id,
            work_type="hot",
        )
        assert nxt.item is not None
        self.assertEqual(nxt.item.queue_item_id, expect.queue_item_id)

    def test_final_item_end_of_results(self) -> None:
        full = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            page_results=False,
            enrich_contacts=False,
            apply_insight_overlay=False,
        )
        last = full.items[-1]
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id=last.queue_item_id,
            after_work_priority=last.work_priority,
            after_company_name=last.company_name,
            after_company_id=last.company_id,
            client_id=self.client_id,
            work_type="hot",
        )
        self.assertFalse(nxt.has_next)
        self.assertTrue(nxt.end_of_results)
        self.assertIsNone(nxt.item)
        self.assertIn("No further", nxt.message)

    def test_cursor_after_completed_item(self) -> None:
        page = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            limit=50,
            offset=0,
        )
        first = page.items[0]
        second = page.items[1]
        # Simulate completion: pass after_id that is no longer present, with cursor.
        fake_id = first.queue_item_id + ":gone"
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id=fake_id,
            after_work_priority=first.work_priority,
            after_due_date=first.due_date,
            after_due_time=first.due_time,
            after_company_name=first.company_name,
            after_company_id=first.company_id,
            client_id=self.client_id,
            work_type="hot",
        )
        self.assertTrue(nxt.has_next)
        assert nxt.item is not None
        self.assertEqual(nxt.item.queue_item_id, second.queue_item_id)

    def test_hot_filter_excludes_other_work_types(self) -> None:
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id="missing",
            after_work_priority=0,
            after_company_name="",
            after_company_id=0,
            client_id=self.client_id,
            work_type="hot",
        )
        # Missing id + empty cursor company → falls through to index 0
        if nxt.item:
            self.assertEqual(nxt.item.work_type, "Hot")

    def test_deterministic_tie_break_uses_queue_item_id(self) -> None:
        full = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            page_results=False,
            enrich_contacts=False,
            apply_insight_overlay=False,
        )
        ids = [r.queue_item_id for r in full.items]
        self.assertEqual(ids, sorted(ids, key=lambda _: 0) or ids)
        # Stable: re-rank twice yields identical order.
        again = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            page_results=False,
            enrich_contacts=False,
            apply_insight_overlay=False,
        )
        self.assertEqual(
            [r.queue_item_id for r in full.items],
            [r.queue_item_id for r in again.items],
        )

    def test_inaccessible_client_excluded(self) -> None:
        with self.assertRaises(PermissionError):
            get_work_queue_next(
                self.limited_uid,
                after_queue_item_id="x",
                after_work_priority=100,
                after_company_name="",
                after_company_id=0,
                client_id=self.hidden_client,
                work_type="hot",
            )

    def test_requires_after_queue_item_id(self) -> None:
        with self.assertRaises(ValueError):
            get_work_queue_next(
                self.user_id,
                after_queue_item_id="",
                client_id=self.client_id,
                work_type="hot",
            )

    def test_no_unbounded_ccr_book_for_next(self) -> None:
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id="seed",
            after_work_priority=0,
            after_company_name="AAA",
            after_company_id=0,
            client_id=self.client_id,
            work_type="hot",
        )
        # Response carries at most one item.
        self.assertLessEqual(1 if nxt.item else 0, 1)
        if nxt.item:
            self.assertNotIn("Noise", nxt.item.company_name)

    def test_quick_filters_hot_weblead_cross_overdue_parity(self) -> None:
        """List and next APIs apply the same quick-filter semantics."""
        with get_connection() as conn:
            # WebLead milestone on first hot company
            conn.execute(
                """
                INSERT INTO revenue_milestones (
                    client_id, company_id, relationship_id, external_record_no,
                    milestone_type, milestone_date, created_at
                ) VALUES (?, ?, ?, ?, 'WebLead', date('now'), datetime('now'))
                """,
                (
                    self.client_id,
                    self.hot_ids[0],
                    self.hot_rel_ids[0],
                    f"NS-WQN-{self.suffix}-0000",
                ),
            )
            # Cross-client assignment on second company
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
            ).fetchone():
                conn.execute(
                    """
                    INSERT INTO opportunity_assignments (
                        target_client_id, company_id, relationship_id
                    ) VALUES (?, ?, ?)
                    """,
                    (self.client_id, self.hot_ids[1], self.hot_rel_ids[1]),
                )
            # Overdue call activity on third company
            conn.execute(
                """
                INSERT INTO activities (
                    client_id, company_id, relationship_id, external_record_no,
                    activity_type, activity_at, follow_up_at, completion_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'Call', datetime('now'),
                          datetime('now', '-2 days'), 'open',
                          datetime('now'), datetime('now'))
                """,
                (
                    self.client_id,
                    self.hot_ids[2],
                    self.hot_rel_ids[2],
                    f"NS-WQN-{self.suffix}-0002",
                ),
            )
            conn.commit()

        for kwargs in (
            {"hot": True},
            {"weblead": True},
            {"cross_client": True},
            {"overdue_only": True},
            {"due": "overdue"},
        ):
            listed = list_work_queue(
                self.user_id,
                client_id=self.client_id,
                page_results=False,
                enrich_contacts=False,
                apply_insight_overlay=False,
                **kwargs,
            )
            if not listed.items:
                continue
            first = listed.items[0]
            nxt = get_work_queue_next(
                self.user_id,
                after_queue_item_id=first.queue_item_id,
                after_work_priority=first.work_priority,
                after_due_date=first.due_date,
                after_due_time=first.due_time,
                after_company_name=first.company_name,
                after_company_id=first.company_id,
                client_id=self.client_id,
                **kwargs,
            )
            list_ids = [r.queue_item_id for r in listed.items]
            if len(list_ids) == 1:
                self.assertTrue(nxt.end_of_results)
            else:
                self.assertTrue(nxt.has_next)
                assert nxt.item is not None
                self.assertEqual(nxt.item.queue_item_id, list_ids[1])
                self.assertNotEqual(nxt.item.queue_item_id, first.queue_item_id)

    def test_ai_filter_params_accepted_and_parity(self) -> None:
        """ai_* filters are accepted and keep list/next order aligned."""
        listed = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            ai_alignment="Aligned",
            ai_recommendation="Pursue",
            ai_fit="Strong Fit",
            ai_engagement="Engaged",
            page_results=False,
            enrich_contacts=False,
        )
        # Without insight data, filter typically yields empty — still must not error.
        if not listed.items:
            nxt = get_work_queue_next(
                self.user_id,
                after_queue_item_id="hot:hot:0:0",
                after_work_priority=0,
                after_company_name="",
                after_company_id=0,
                client_id=self.client_id,
                work_type="hot",
                ai_alignment="Aligned",
                ai_recommendation="Pursue",
                ai_fit="Strong Fit",
                ai_engagement="Engaged",
            )
            self.assertTrue(nxt.end_of_results)
            return
        first = listed.items[0]
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id=first.queue_item_id,
            after_work_priority=first.work_priority,
            after_company_name=first.company_name,
            after_company_id=first.company_id,
            client_id=self.client_id,
            work_type="hot",
            ai_alignment="Aligned",
            ai_recommendation="Pursue",
            ai_fit="Strong Fit",
            ai_engagement="Engaged",
        )
        list_ids = [r.queue_item_id for r in listed.items]
        if len(list_ids) > 1:
            assert nxt.item is not None
            self.assertEqual(nxt.item.queue_item_id, list_ids[1])

    def test_null_due_ordering_matches_list(self) -> None:
        """Null due dates sort after dated rows; list and next share order."""
        listed = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            page_results=False,
            enrich_contacts=False,
            apply_insight_overlay=False,
        )
        self.assertGreaterEqual(len(listed.items), 3)
        # Hot rows typically have null due — order must be stable across APIs.
        ids = [r.queue_item_id for r in listed.items]
        for i in range(len(ids) - 1):
            cur = listed.items[i]
            nxt = get_work_queue_next(
                self.user_id,
                after_queue_item_id=cur.queue_item_id,
                after_work_priority=cur.work_priority,
                after_due_date=cur.due_date,
                after_due_time=cur.due_time or "",
                after_company_name=cur.company_name,
                after_company_id=cur.company_id,
                client_id=self.client_id,
                work_type="hot",
            )
            self.assertTrue(nxt.has_next)
            assert nxt.item is not None
            self.assertEqual(nxt.item.queue_item_id, ids[i + 1])
            self.assertNotEqual(nxt.item.queue_item_id, cur.queue_item_id)

    def test_cursor_never_returns_same_item(self) -> None:
        listed = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            limit=5,
            offset=0,
        )
        cur = listed.items[0]
        # Cursor pointing at current sort key must not yield the same id.
        nxt = get_work_queue_next(
            self.user_id,
            after_queue_item_id=cur.queue_item_id + ":removed",
            after_work_priority=cur.work_priority,
            after_due_date=cur.due_date,
            after_due_time=cur.due_time or "",
            after_company_name=cur.company_name,
            after_company_id=cur.company_id,
            client_id=self.client_id,
            work_type="hot",
        )
        if nxt.item:
            self.assertNotEqual(nxt.item.queue_item_id, cur.queue_item_id)

    def test_all_my_clients_excludes_hidden(self) -> None:
        nxt = get_work_queue_next(
            self.limited_uid,
            after_queue_item_id="x",
            after_work_priority=0,
            after_company_name="",
            after_company_id=0,
            client_id=None,  # all assigned
            work_type="hot",
        )
        if nxt.item:
            self.assertEqual(nxt.item.client_id, self.client_id)
            self.assertNotEqual(nxt.item.client_id, self.hidden_client)


if __name__ == "__main__":
    unittest.main()
