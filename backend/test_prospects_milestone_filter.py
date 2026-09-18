"""Prospects milestone_type filter — Quotes / PO / WebLead full-client pages.

Run: python test_prospects_milestone_filter.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import unittest
import uuid

import testdb  # noqa: F401

from access import get_default_user
from client_workspace_data import list_prospects_page
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema


MARKER = "NSPMF"


def _create_client(conn, *, name: str) -> int:
    code = f"pmf-{uuid.uuid4().hex[:10]}"
    return int(
        conn.execute(
            "INSERT INTO clients (code, name) VALUES (?, ?)",
            (code, name),
        ).lastrowid
    )


def _ensure_assignment(conn, user_id: int, client_id: int) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO user_client_assignments
            (user_id, client_id, role, active, assigned_at)
        VALUES (?, ?, 'staff', 1, datetime('now'))
        """,
        (user_id, client_id),
    )


def _create_company_with_ccr(
    conn, client_id: int, *, name: str, status: str = "Working"
) -> tuple[int, int, str]:
    rn = f"PMF-{uuid.uuid4().hex[:12]}"
    company_id = int(
        conn.execute(
            "INSERT INTO companies (external_record_no, company_name) VALUES (?, ?)",
            (rn, name),
        ).lastrowid
    )
    rel_id = int(
        conn.execute(
            """
            INSERT INTO client_company_relationships (client_id, company_id, status)
            VALUES (?, ?, ?)
            """,
            (client_id, company_id, status),
        ).lastrowid
    )
    return company_id, rel_id, rn


def _insert_quote(conn, client_id: int, company_id: int, rel_id: int, rn: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO revenue_milestones (
                client_id, company_id, relationship_id, external_record_no,
                milestone_type, milestone_date, source, notes, created_by, created_at
            ) VALUES (?, ?, ?, ?, 'Quote', '2025-01-01', 'test', '', 'test', datetime('now'))
            """,
            (client_id, company_id, rel_id, rn),
        ).lastrowid
    )


def _cleanup_client(conn, client_id: int) -> None:
    conn.execute("DELETE FROM revenue_milestones WHERE client_id = ?", (client_id,))
    company_ids = [
        int(r["company_id"])
        for r in conn.execute(
            "SELECT company_id FROM client_company_relationships WHERE client_id = ?",
            (client_id,),
        ).fetchall()
    ]
    conn.execute(
        "DELETE FROM client_company_relationships WHERE client_id = ?", (client_id,)
    )
    conn.execute("DELETE FROM user_client_assignments WHERE client_id = ?", (client_id,))
    for cid in company_ids:
        still = conn.execute(
            "SELECT 1 FROM client_company_relationships WHERE company_id = ?",
            (cid,),
        ).fetchone()
        if still is None:
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))


class ProspectsMilestoneFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if DB_PATH.resolve() == PRODUCTION_DB_PATH.resolve():
            raise AssertionError("Refusing to run against production DB.")
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def setUp(self) -> None:
        self.client_ids: list[int] = []
        user = get_default_user()
        assert user is not None
        self.user_id = int(user.id)

    def tearDown(self) -> None:
        with get_connection() as conn:
            for cid in self.client_ids:
                _cleanup_client(conn, cid)
            conn.commit()

    def _new_client(self, label: str) -> int:
        with get_connection() as conn:
            client_id = _create_client(conn, name=f"{MARKER} {label}")
            _ensure_assignment(conn, self.user_id, client_id)
            conn.commit()
        self.client_ids.append(client_id)
        return client_id

    def test_brown_style_26_quotes_total_ignores_page_size(self) -> None:
        client_id = self._new_client("Quotes26")
        with get_connection() as conn:
            for i in range(26):
                company_id, rel_id, rn = _create_company_with_ccr(
                    conn, client_id, name=f"{MARKER} Quote Co {i:02d}"
                )
                _insert_quote(conn, client_id, company_id, rel_id, rn)
            # Extra companies without Quote must not appear
            for i in range(10):
                _create_company_with_ccr(
                    conn, client_id, name=f"{MARKER} NoQuote {i}"
                )
            conn.commit()

        page = list_prospects_page(
            client_id=client_id,
            user_id=self.user_id,
            milestone_type="Quote",
            limit=50,
            offset=0,
        )
        self.assertEqual(page["total"], 26)
        self.assertEqual(len(page["prospects"]), 26)
        page2 = list_prospects_page(
            client_id=client_id,
            user_id=self.user_id,
            milestone_type="Quote",
            limit=10,
            offset=0,
        )
        self.assertEqual(page2["total"], 26)
        self.assertEqual(len(page2["prospects"]), 10)
        self.assertTrue(all(p.has_quote for p in page2["prospects"]))

    def test_search_and_status_within_quote_set(self) -> None:
        client_id = self._new_client("SearchStatus")
        with get_connection() as conn:
            c1, r1, rn1 = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Alpha Quoted", status="Working"
            )
            c2, r2, rn2 = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Beta Quoted", status="Hot Prospect"
            )
            _insert_quote(conn, client_id, c1, r1, rn1)
            _insert_quote(conn, client_id, c2, r2, rn2)
            conn.commit()

        by_name = list_prospects_page(
            client_id=client_id,
            user_id=self.user_id,
            milestone_type="Quote",
            q="Alpha",
            limit=50,
            offset=0,
        )
        self.assertEqual(by_name["total"], 1)
        self.assertIn("Alpha", by_name["prospects"][0].company)

        by_status = list_prospects_page(
            client_id=client_id,
            user_id=self.user_id,
            milestone_type="Quote",
            status="Hot Prospect",
            limit=50,
            offset=0,
        )
        self.assertEqual(by_status["total"], 1)
        self.assertEqual(by_status["prospects"][0].status, "Hot Prospect")

    def test_zero_quotes_returns_zero(self) -> None:
        client_id = self._new_client("ZeroQuotes")
        with get_connection() as conn:
            _create_company_with_ccr(conn, client_id, name=f"{MARKER} Plain")
            conn.commit()
        page = list_prospects_page(
            client_id=client_id,
            user_id=self.user_id,
            milestone_type="Quote",
            limit=50,
            offset=0,
        )
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["prospects"], [])

    def test_cross_client_isolation(self) -> None:
        client_a = self._new_client("IsoA")
        client_b = self._new_client("IsoB")
        with get_connection() as conn:
            ca, ra, rna = _create_company_with_ccr(
                conn, client_a, name=f"{MARKER} CarmecoLike Quote"
            )
            cb, rb, rnb = _create_company_with_ccr(
                conn, client_b, name=f"{MARKER} BrownLike Quote"
            )
            _insert_quote(conn, client_a, ca, ra, rna)
            _insert_quote(conn, client_b, cb, rb, rnb)
            conn.commit()

        page_b = list_prospects_page(
            client_id=client_b,
            user_id=self.user_id,
            milestone_type="Quote",
            limit=50,
            offset=0,
        )
        self.assertEqual(page_b["total"], 1)
        self.assertEqual(page_b["prospects"][0].client_id, client_b)
        self.assertNotEqual(page_b["prospects"][0].id, ca)

    def test_po_and_weblead_same_server_filter(self) -> None:
        client_id = self._new_client("POWeb")
        with get_connection() as conn:
            c_po, r_po, rn_po = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} PO Co"
            )
            c_w, r_w, rn_w = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Web Co"
            )
            c_q, r_q, rn_q = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Quote Only"
            )
            conn.execute(
                """
                INSERT INTO revenue_milestones (
                    client_id, company_id, relationship_id, external_record_no,
                    milestone_type, milestone_date, source, notes, created_by, created_at
                ) VALUES (?, ?, ?, ?, 'Purchase Order', '2025-01-01', 'test', '', 'test', datetime('now'))
                """,
                (client_id, c_po, r_po, rn_po),
            )
            conn.execute(
                """
                INSERT INTO revenue_milestones (
                    client_id, company_id, relationship_id, external_record_no,
                    milestone_type, milestone_date, source, notes, created_by, created_at
                ) VALUES (?, ?, ?, ?, 'WebLead', '2025-01-01', 'test', '', 'test', datetime('now'))
                """,
                (client_id, c_w, r_w, rn_w),
            )
            _insert_quote(conn, client_id, c_q, r_q, rn_q)
            conn.commit()

        po = list_prospects_page(
            client_id=client_id,
            user_id=self.user_id,
            milestone_type="Purchase Order",
            limit=50,
            offset=0,
        )
        web = list_prospects_page(
            client_id=client_id,
            user_id=self.user_id,
            milestone_type="WebLead",
            limit=50,
            offset=0,
        )
        self.assertEqual(po["total"], 1)
        self.assertTrue(po["prospects"][0].has_purchase_order)
        self.assertEqual(web["total"], 1)
        self.assertTrue(web["prospects"][0].has_weblead)


if __name__ == "__main__":
    unittest.main()
