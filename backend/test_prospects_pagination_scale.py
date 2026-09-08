"""Phase 0B: bounded Companies/prospects pagination scale proofs (isolated testdb).

Run: python test_prospects_pagination_scale.py
"""

from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

import testdb  # noqa: F401 — isolates NORTHSTAR_DB_PATH before db imports
from access import get_default_user
from client_workspace_data import (
    PROSPECTS_MAX_LIMIT,
    clamp_prospects_page,
    list_prospects_page,
)
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema


def _uid() -> int:
    user = get_default_user()
    assert user is not None
    return int(user.id)


class ProspectsPaginationScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            suffix = secrets.token_hex(3)
            self.client_a = int(
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES (?, ?)",
                    (f"pg_a_{suffix}", f"Page Client A {suffix}"),
                ).lastrowid
            )
            self.client_b = int(
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES (?, ?)",
                    (f"pg_b_{suffix}", f"Page Client B {suffix}"),
                ).lastrowid
            )
            self.user_id = _uid()
            for cid in (self.client_a, self.client_b):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_client_assignments
                        (user_id, client_id, role, active, assigned_at)
                    VALUES (?, ?, 'staff', 1, datetime('now'))
                    """,
                    (self.user_id, cid),
                )
            self.company_ids: list[int] = []
            for i in range(120):
                cid = int(
                    conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, address, city, state, zip,
                            website, legacy_phone, type_of_industry, created_at, last_updated_at
                        ) VALUES (?, ?, '', 'Town', 'OH', '', '', '', '',
                                  datetime('now'), datetime('now'))
                        """,
                        (f"NS-PAGE-{suffix}-{i:04d}", f"Page Co {suffix} {i:04d}"),
                    ).lastrowid
                )
                self.company_ids.append(cid)
                status = (
                    "Hot Prospect" if i < 15 else ("New" if i < 40 else "Left Message")
                )
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status, assigned_user_id,
                        priority, next_action, notes, is_hot, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, '', '', '', 0, datetime('now'), datetime('now'))
                    """,
                    (self.client_a, cid, f"NS-PAGE-{suffix}-{i:04d}", status),
                )
                if i < 40:
                    conn.execute(
                        """
                        INSERT INTO client_company_relationships (
                            client_id, company_id, external_record_no, status, assigned_user_id,
                            priority, next_action, notes, is_hot, created_at, updated_at
                        ) VALUES (?, ?, ?, 'Appointment Set', NULL, '', '', '', 0,
                                  datetime('now'), datetime('now'))
                        """,
                        (self.client_b, cid, f"NS-PAGE-B-{suffix}-{i:04d}"),
                    )
            conn.commit()

    def test_clamp_rejects_invalid_paging(self) -> None:
        with self.assertRaises(ValueError):
            clamp_prospects_page(0, 0)
        with self.assertRaises(ValueError):
            clamp_prospects_page(-1, 0)
        with self.assertRaises(ValueError):
            clamp_prospects_page(PROSPECTS_MAX_LIMIT + 1, 0)
        with self.assertRaises(ValueError):
            clamp_prospects_page(50, -1)
        limit, offset = clamp_prospects_page(None, None)
        self.assertEqual(limit, 50)
        self.assertEqual(offset, 0)

    def test_no_unbounded_limit_none(self) -> None:
        page = list_prospects_page(
            client_id=self.client_a, user_id=self.user_id, limit=None, offset=0
        )
        self.assertEqual(page["limit"], 50)
        self.assertLessEqual(len(page["prospects"]), 50)
        self.assertEqual(page["total"], 120)

    def test_rows_never_exceed_limit(self) -> None:
        page = list_prospects_page(
            client_id=self.client_a, user_id=self.user_id, limit=25, offset=0
        )
        self.assertEqual(len(page["prospects"]), 25)
        self.assertTrue(page["has_next"])
        self.assertFalse(page["has_previous"])

    def test_status_filter_total_before_page(self) -> None:
        page = list_prospects_page(
            client_id=self.client_a,
            user_id=self.user_id,
            status="Hot Prospect",
            limit=10,
            offset=0,
        )
        self.assertEqual(page["total"], 15)
        self.assertEqual(len(page["prospects"]), 10)
        self.assertTrue(all(p.status == "Hot Prospect" for p in page["prospects"]))

    def test_all_my_clients_no_duplicate_companies(self) -> None:
        with get_connection() as conn:
            name = conn.execute(
                "SELECT company_name FROM companies WHERE id = ?",
                (self.company_ids[0],),
            ).fetchone()["company_name"]
        # "Page Co {suffix} 0000" → search "Page Co {suffix}"
        token = " ".join(str(name).split(" ")[:3])
        page = list_prospects_page(
            all_clients=True,
            user_id=self.user_id,
            q=token,
            limit=100,
            offset=0,
        )
        ids = [p.id for p in page["prospects"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(page["total"], 120)
        shared = [p for p in page["prospects"] if len(p.client_statuses) == 2]
        self.assertGreaterEqual(len(shared), 1)
        single = list_prospects_page(
            client_id=self.client_a, user_id=self.user_id, limit=1, offset=0
        )
        self.assertEqual(single["total"], 120)
    def test_all_clients_status_matches_any_relationship(self) -> None:
        page = list_prospects_page(
            client_id=self.client_b,
            user_id=self.user_id,
            status="Appointment Set",
            limit=50,
            offset=0,
        )
        self.assertEqual(page["total"], 40)
        all_clients = list_prospects_page(
            all_clients=True,
            user_id=self.user_id,
            status="Appointment Set",
            limit=50,
            offset=0,
        )
        # At least our 40; may include other testdb rows with same status.
        self.assertGreaterEqual(all_clients["total"], 40)
        for p in all_clients["prospects"]:
            self.assertTrue(
                any(
                    c.status.lower() == "appointment set" for c in p.client_statuses
                )
                or p.status.lower() == "appointment set"
            )

    def test_deterministic_paging_no_skip_or_dup(self) -> None:
        seen: list[int] = []
        offset = 0
        limit = 40
        while True:
            page = list_prospects_page(
                client_id=self.client_a,
                user_id=self.user_id,
                limit=limit,
                offset=offset,
            )
            batch = [p.id for p in page["prospects"]]
            self.assertEqual(len(batch), len(set(batch)))
            overlap = set(seen) & set(batch)
            self.assertEqual(overlap, set())
            seen.extend(batch)
            if not page["has_next"]:
                break
            offset += limit
            self.assertLess(offset, 500)
        self.assertEqual(len(seen), 120)
        self.assertEqual(len(set(seen)), 120)

    def test_returned_rows_independent_of_book_size(self) -> None:
        page25 = list_prospects_page(
            client_id=self.client_a, user_id=self.user_id, limit=25, offset=0
        )
        page50 = list_prospects_page(
            client_id=self.client_a, user_id=self.user_id, limit=50, offset=0
        )
        self.assertEqual(len(page25["prospects"]), 25)
        self.assertEqual(len(page50["prospects"]), 50)
        self.assertEqual(page25["total"], page50["total"])
        self.assertEqual(page25["total"], 120)


if __name__ == "__main__":
    unittest.main()
