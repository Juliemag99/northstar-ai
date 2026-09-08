"""Phase 0B: bounded Work Queue pagination scale proofs (isolated testdb).

Run: python test_work_queue_pagination_scale.py
"""

from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

import testdb  # noqa: F401
from access import get_default_user
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from work_queue_data import (
    WORK_QUEUE_MAX_LIMIT,
    _normalize_work_queue_page,
    list_work_queue,
)


def _uid() -> int:
    user = get_default_user()
    assert user is not None
    return int(user.id)


class WorkQueuePaginationScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            suffix = secrets.token_hex(3)
            self.client_id = int(
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES (?, ?)",
                    (f"wq_{suffix}", f"WQ Page Client {suffix}"),
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
            self.hot_company_ids: list[int] = []
            for i in range(80):
                cid = int(
                    conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, created_at, last_updated_at
                        ) VALUES (?, ?, datetime('now'), datetime('now'))
                        """,
                        (f"NS-WQ-{suffix}-{i:04d}", f"WQ Hot {suffix} {i:04d}"),
                    ).lastrowid
                )
                self.hot_company_ids.append(cid)
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status, assigned_user_id,
                        priority, next_action, notes, is_hot, created_at, updated_at
                    ) VALUES (?, ?, ?, 'Hot Prospect', NULL, '', 'Work hot', '', 0,
                              datetime('now'), datetime('now'))
                    """,
                    (self.client_id, cid, f"NS-WQ-{suffix}-{i:04d}"),
                )
            # Noise CCRs that must not be loaded as the full book for Hot paging.
            for i in range(200):
                cid = int(
                    conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, created_at, last_updated_at
                        ) VALUES (?, ?, datetime('now'), datetime('now'))
                        """,
                        (f"NS-WQ-NOISE-{suffix}-{i:04d}", f"WQ Noise {suffix} {i:04d}"),
                    ).lastrowid
                )
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status, assigned_user_id,
                        priority, next_action, notes, is_hot, created_at, updated_at
                    ) VALUES (?, ?, ?, 'Left Message', NULL, '', '', '', 0,
                              datetime('now'), datetime('now'))
                    """,
                    (self.client_id, cid, f"NS-WQ-NOISE-{suffix}-{i:04d}"),
                )
            conn.commit()
            self.suffix = suffix

    def test_clamp_rejects_invalid_paging(self) -> None:
        with self.assertRaises(ValueError):
            _normalize_work_queue_page(0, 0)
        with self.assertRaises(ValueError):
            _normalize_work_queue_page(WORK_QUEUE_MAX_LIMIT + 1, 0)
        with self.assertRaises(ValueError):
            _normalize_work_queue_page(50, -1)
        limit, offset = _normalize_work_queue_page(None, None)
        self.assertEqual(limit, 50)
        self.assertEqual(offset, 0)

    def test_hot_filter_paged_and_bounded(self) -> None:
        page = list_work_queue(
            self.user_id,
            client_id=self.client_id,
            work_type="hot",
            limit=25,
            offset=0,
        )
        self.assertLessEqual(len(page.items), 25)
        self.assertEqual(len(page.items), 25)
        self.assertGreaterEqual(page.total, 80)
        self.assertEqual(page.count, page.total)
        self.assertTrue(page.has_next)
        self.assertFalse(page.has_previous)
        self.assertTrue(all(item.work_type == "Hot" for item in page.items))

    def test_deterministic_paging_no_skip_or_dup(self) -> None:
        seen: list[str] = []
        offset = 0
        limit = 30
        while True:
            page = list_work_queue(
                self.user_id,
                client_id=self.client_id,
                work_type="hot",
                limit=limit,
                offset=offset,
            )
            batch = [item.queue_item_id for item in page.items]
            self.assertEqual(len(batch), len(set(batch)))
            self.assertEqual(set(seen) & set(batch), set())
            seen.extend(batch)
            if not page.has_next:
                break
            offset += limit
            self.assertLess(offset, 500)
        self.assertGreaterEqual(len(seen), 80)
        self.assertEqual(len(seen), len(set(seen)))

    def test_default_limit_never_returns_full_book(self) -> None:
        page = list_work_queue(self.user_id, client_id=self.client_id)
        self.assertEqual(page.limit, 50)
        self.assertLessEqual(len(page.items), 50)
        # Client has 280 CCRs; queue items are work candidates, not full CCR dump.
        self.assertLess(len(page.items), 280)

    def test_no_unqualified_ccr_book_select(self) -> None:
        """Hot path must not SELECT every CCR for the client without a status/key predicate."""
        import re
        import sqlite3

        import work_queue_data as wqd

        class TracingConnection:
            def __init__(self, conn: sqlite3.Connection):
                self._conn = conn
                self.statements: list[str] = []

            def execute(self, sql, parameters=()):
                text = " ".join(str(sql).split())
                upper = text.upper()
                if not upper.startswith("EXPLAIN") and not upper.startswith("PRAGMA"):
                    self.statements.append(text)
                return self._conn.execute(sql, parameters)

            def executemany(self, sql, seq):
                self.statements.append(" ".join(str(sql).split()))
                return self._conn.executemany(sql, seq)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                return self._conn.__exit__(exc_type, exc, tb)

            def __getattr__(self, name: str):
                return getattr(self._conn, name)

        def unqualified_book_scan(sql: str) -> bool:
            upper = " ".join(sql.upper().split())
            if not re.search(r"\bFROM\s+CLIENT_COMPANY_RELATIONSHIPS\b", upper):
                return False
            if "COUNT(" in upper:
                return False
            if "STATUS" in upper and (
                "=" in upper or " LIKE " in upper or re.search(r"\bIN\s*\(", upper)
            ):
                return False
            if "FOLLOW_UP_DATE" in upper:
                return False
            if "COMPANY_ID = ?" in upper or "COMPANY_ID=?" in upper:
                return False
            if "COMPANY_ID IN" in upper:
                return False
            return bool(re.search(r"CLIENT_ID\s+IN\s*\(", upper))

        traced: list[TracingConnection] = []
        real_get = wqd.get_connection

        def traced_get(*args, **kwargs):
            wrapped = TracingConnection(real_get(*args, **kwargs))
            traced.append(wrapped)
            return wrapped

        wqd.get_connection = traced_get  # type: ignore[assignment]
        try:
            page = list_work_queue(
                self.user_id,
                client_id=self.client_id,
                work_type="hot",
                limit=50,
                offset=0,
            )
        finally:
            wqd.get_connection = real_get  # type: ignore[assignment]

        statements = [s for t in traced for s in t.statements]
        bad = [s for s in statements if unqualified_book_scan(s)]
        self.assertEqual(bad, [], msg=f"unqualified CCR book scan: {bad[:1]}")
        hydrate = [
            s
            for s in statements
            if "CLIENT_COMPANY_RELATIONSHIPS" in s.upper()
            and "COMPANY_ID = ?" in s.upper()
            and "CLIENT_ID = ?" in s.upper()
        ]
        self.assertTrue(hydrate, "expected pair-key CCR hydrate SQL")
        noise_hits = [
            item
            for item in page.items
            if "Noise" in item.company_name and item.work_type == "Hot"
        ]
        self.assertEqual(noise_hits, [])
        self.assertGreaterEqual(page.summary.hot, 80)


if __name__ == "__main__":
    unittest.main()
