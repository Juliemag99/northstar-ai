"""Engagement Quote milestone projection — isolated tests.

Run from backend/:
  .venv\\Scripts\\python.exe test_engagement_quote_milestones.py

Uses testdb isolation only — never writes production northstar.db.
"""

from __future__ import annotations

import unittest
import uuid

import testdb  # noqa: F401

from appointments_data import ensure_appointments_schema
from client_engagement_import import ensure_engagement_import_schema
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from engagement_quote_milestones import (
    apply_quote_milestone_projection,
    plan_quote_milestone_projection,
    project_quote_milestones_for_batch,
)
from milestones_data import milestone_summary


MARKER = "NSQPROJ"


def _admin_user_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM users WHERE COALESCE(is_administrator, 0) = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise AssertionError("Isolated testdb needs an admin user.")
    return int(row["id"])


def _ensure_assignment(conn, user_id: int, client_id: int) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO user_client_assignments
            (user_id, client_id, role, active, assigned_at)
        VALUES (?, ?, 'staff', 1, datetime('now'))
        """,
        (user_id, client_id),
    )


def _create_client(conn, *, name: str) -> int:
    code = f"qproj-{uuid.uuid4().hex[:10]}"
    return int(
        conn.execute(
            "INSERT INTO clients (code, name) VALUES (?, ?)",
            (code, name),
        ).lastrowid
    )


def _create_company_with_ccr(conn, client_id: int, *, name: str) -> tuple[int, int, str]:
    rn = f"QP-{uuid.uuid4().hex[:12]}"
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
            VALUES (?, ?, 'Working')
            """,
            (client_id, company_id),
        ).lastrowid
    )
    return company_id, rel_id, rn


def _create_batch(conn, client_id: int) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO client_engagement_import_batches (
                client_id, document_id, filename, status, uploaded_at
            ) VALUES (?, NULL, 'quote-proj-test.xlsx', 'imported', datetime('now'))
            """,
            (client_id,),
        ).lastrowid
    )


def _insert_sales_event(
    conn,
    *,
    client_id: int,
    batch_id: int,
    company_id: int,
    relationship_id: int,
    event_type: str,
    quoted_amount: float | None = None,
    source_quoted_value: str = "",
    sales_notes: str = "",
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO client_sales_events (
                client_id, relationship_id, company_id, event_type, event_family,
                company_name, quoted_amount, source_quoted_value, sales_notes,
                source_batch_id, source_row_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                client_id,
                relationship_id,
                company_id,
                event_type,
                f"Co {company_id}",
                quoted_amount,
                source_quoted_value,
                sales_notes,
                batch_id,
                f"fp-{uuid.uuid4().hex}",
            ),
        ).lastrowid
    )


def _cleanup_client(conn, client_id: int) -> None:
    conn.execute("DELETE FROM revenue_milestones WHERE client_id = ?", (client_id,))
    conn.execute("DELETE FROM client_sales_events WHERE client_id = ?", (client_id,))
    conn.execute(
        "DELETE FROM client_engagement_import_batches WHERE client_id = ?", (client_id,)
    )
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


def _quote_count(conn, client_id: int, company_id: int | None = None) -> int:
    if company_id is None:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM revenue_milestones
            WHERE client_id = ? AND milestone_type = 'Quote'
            """,
            (client_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM revenue_milestones
            WHERE client_id = ? AND company_id = ? AND milestone_type = 'Quote'
            """,
            (client_id, company_id),
        ).fetchone()
    return int(row["n"])


def _ccr_status(conn, client_id: int, company_id: int) -> str:
    row = conn.execute(
        """
        SELECT status FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (client_id, company_id),
    ).fetchone()
    return str(row["status"] if row else "")


class EngagementQuoteMilestoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if DB_PATH.resolve() == PRODUCTION_DB_PATH.resolve():
            raise AssertionError("Refusing to run against production DB.")
        with get_connection() as conn:
            migrate_schema(conn)
            ensure_engagement_import_schema(conn)
            ensure_appointments_schema(conn)
            conn.commit()

    def setUp(self) -> None:
        self.client_ids: list[int] = []
        with get_connection() as conn:
            self.user_id = _admin_user_id(conn)

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

    def test_multiple_quote_events_one_milestone(self) -> None:
        client_id = self._new_client("MultiEvent")
        with get_connection() as conn:
            company_id, rel_id, _rn = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Multi"
            )
            batch_id = _create_batch(conn, client_id)
            _insert_sales_event(
                conn,
                client_id=client_id,
                batch_id=batch_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="RFQ",
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                batch_id=batch_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Send Info",
                quoted_amount=1200.0,
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                batch_id=batch_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Send Info",
                sales_notes="--- Forecast / Quoted / Won ---\nQuoted: 900",
            )
            result = project_quote_milestones_for_batch(conn, client_id, batch_id)
            conn.commit()
            self.assertEqual(result["created_count"], 1)
            self.assertEqual(_quote_count(conn, client_id, company_id), 1)
            # Amounts remain on sales events; milestone must not sum both amounts
            ms = conn.execute(
                """
                SELECT amount FROM revenue_milestones
                WHERE client_id = ? AND company_id = ? AND milestone_type = 'Quote'
                """,
                (client_id, company_id),
            ).fetchone()
            self.assertIsNone(ms["amount"])
            amounts = [
                float(r["quoted_amount"])
                for r in conn.execute(
                    """
                    SELECT quoted_amount FROM client_sales_events
                    WHERE client_id = ? AND company_id = ? AND quoted_amount IS NOT NULL
                    """,
                    (client_id, company_id),
                ).fetchall()
            ]
            self.assertIn(1200.0, amounts)
            self.assertEqual(_ccr_status(conn, client_id, company_id), "Working")

        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.quotes, 1)

    def test_same_company_two_clients_separate_milestones(self) -> None:
        client_a = self._new_client("ClientA")
        client_b = self._new_client("ClientB")
        with get_connection() as conn:
            rn = f"QP-SHARED-{uuid.uuid4().hex[:8]}"
            company_id = int(
                conn.execute(
                    "INSERT INTO companies (external_record_no, company_name) VALUES (?, ?)",
                    (rn, f"{MARKER} Shared Co"),
                ).lastrowid
            )
            rel_a = int(
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (client_id, company_id, status)
                    VALUES (?, ?, 'Working')
                    """,
                    (client_a, company_id),
                ).lastrowid
            )
            rel_b = int(
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (client_id, company_id, status)
                    VALUES (?, ?, 'Working')
                    """,
                    (client_b, company_id),
                ).lastrowid
            )
            batch_a = _create_batch(conn, client_a)
            batch_b = _create_batch(conn, client_b)
            _insert_sales_event(
                conn,
                client_id=client_a,
                batch_id=batch_a,
                company_id=company_id,
                relationship_id=rel_a,
                event_type="RFQ",
            )
            _insert_sales_event(
                conn,
                client_id=client_b,
                batch_id=batch_b,
                company_id=company_id,
                relationship_id=rel_b,
                event_type="RFQ",
            )
            project_quote_milestones_for_batch(conn, client_a, batch_a)
            project_quote_milestones_for_batch(conn, client_b, batch_b)
            conn.commit()
            self.assertEqual(_quote_count(conn, client_a, company_id), 1)
            self.assertEqual(_quote_count(conn, client_b, company_id), 1)
            self.assertEqual(_quote_count(conn, client_a), 1)
            self.assertEqual(_quote_count(conn, client_b), 1)

    def test_reimport_backfill_idempotent(self) -> None:
        client_id = self._new_client("Idempotent")
        with get_connection() as conn:
            company_id, rel_id, _rn = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Idem"
            )
            batch_id = _create_batch(conn, client_id)
            _insert_sales_event(
                conn,
                client_id=client_id,
                batch_id=batch_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="RFQ",
            )
            first = project_quote_milestones_for_batch(conn, client_id, batch_id)
            second = project_quote_milestones_for_batch(conn, client_id, batch_id)
            conn.commit()
            self.assertEqual(first["created_count"], 1)
            self.assertEqual(second["created_count"], 0)
            self.assertEqual(second["milestones_to_create_count"], 0)
            self.assertEqual(_quote_count(conn, client_id), 1)

    def test_existing_milestone_reused(self) -> None:
        client_id = self._new_client("Reuse")
        with get_connection() as conn:
            company_id, rel_id, rn = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Reuse"
            )
            existing_id = int(
                conn.execute(
                    """
                    INSERT INTO revenue_milestones (
                        client_id, company_id, relationship_id, external_record_no,
                        milestone_type, milestone_date, amount, reference_number,
                        source, notes, created_by, created_at
                    ) VALUES (?, ?, ?, ?, 'Quote', '2025-01-01', NULL, 'manual',
                              'manual', 'pre-existing', 'test', datetime('now'))
                    """,
                    (client_id, company_id, rel_id, rn),
                ).lastrowid
            )
            batch_id = _create_batch(conn, client_id)
            _insert_sales_event(
                conn,
                client_id=client_id,
                batch_id=batch_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="RFQ",
            )
            plan = plan_quote_milestone_projection(conn, client_id, batch_id)
            self.assertEqual(plan["milestones_to_create_count"], 0)
            self.assertEqual(plan["existing_quote_milestone_count"], 1)
            result = apply_quote_milestone_projection(
                conn, client_id, batch_id, dry_run=False
            )
            conn.commit()
            self.assertEqual(result["created_count"], 0)
            self.assertEqual(_quote_count(conn, client_id), 1)
            self.assertEqual(
                int(result["reuse"][0]["milestone_id"]), existing_id
            )

    def test_narrative_won_po_does_not_create_purchase_order(self) -> None:
        client_id = self._new_client("NoPO")
        with get_connection() as conn:
            company_id, rel_id, _rn = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} NoPO"
            )
            batch_id = _create_batch(conn, client_id)
            _insert_sales_event(
                conn,
                client_id=client_id,
                batch_id=batch_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Send Info",
                sales_notes="Won — purchase order coming next week",
            )
            plan = plan_quote_milestone_projection(conn, client_id, batch_id)
            conn.commit()
            self.assertEqual(plan["eligible_company_count"], 0)
            self.assertEqual(plan["purchase_orders_inferred"], 0)
            po = conn.execute(
                """
                SELECT COUNT(*) AS n FROM revenue_milestones
                WHERE client_id = ? AND milestone_type = 'Purchase Order'
                """,
                (client_id,),
            ).fetchone()
            self.assertEqual(int(po["n"]), 0)

        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.purchase_orders, 0)
        self.assertEqual(summary.quotes, 0)

    def test_dashboard_quotes_from_milestones_not_pagination(self) -> None:
        client_id = self._new_client("DashQuotes")
        with get_connection() as conn:
            batch_id = _create_batch(conn, client_id)
            for i in range(5):
                company_id, rel_id, _rn = _create_company_with_ccr(
                    conn, client_id, name=f"{MARKER} Dash {i}"
                )
                _insert_sales_event(
                    conn,
                    client_id=client_id,
                    batch_id=batch_id,
                    company_id=company_id,
                    relationship_id=rel_id,
                    event_type="RFQ",
                )
            # Before projection: Quote card is milestones-only → 0
            conn.commit()
        before = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(before.quotes, 0)

        with get_connection() as conn:
            apply_quote_milestone_projection(
                conn, client_id, batch_id, dry_run=False
            )
            conn.commit()
        after = milestone_summary(self.user_id, client_id=client_id)
        again = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(after.quotes, 5)
        self.assertEqual(again.quotes, 5)

    def test_cross_client_isolation(self) -> None:
        client_a = self._new_client("IsoA")
        client_b = self._new_client("IsoB")
        with get_connection() as conn:
            ca, ra, _ = _create_company_with_ccr(conn, client_a, name=f"{MARKER} A")
            cb, rb, _ = _create_company_with_ccr(conn, client_b, name=f"{MARKER} B")
            batch_a = _create_batch(conn, client_a)
            batch_b = _create_batch(conn, client_b)
            _insert_sales_event(
                conn,
                client_id=client_a,
                batch_id=batch_a,
                company_id=ca,
                relationship_id=ra,
                event_type="RFQ",
            )
            _insert_sales_event(
                conn,
                client_id=client_b,
                batch_id=batch_b,
                company_id=cb,
                relationship_id=rb,
                event_type="RFQ",
            )
            _insert_sales_event(
                conn,
                client_id=client_b,
                batch_id=batch_b,
                company_id=cb,
                relationship_id=rb,
                event_type="Purchase Order",
            )
            project_quote_milestones_for_batch(conn, client_a, batch_a)
            project_quote_milestones_for_batch(conn, client_b, batch_b)
            conn.commit()

        summary_a = milestone_summary(self.user_id, client_id=client_a)
        summary_b = milestone_summary(self.user_id, client_id=client_b)
        self.assertEqual(summary_a.quotes, 1)
        self.assertEqual(summary_a.purchase_orders, 0)
        self.assertEqual(summary_b.quotes, 1)
        self.assertEqual(summary_b.purchase_orders, 1)


if __name__ == "__main__":
    unittest.main()
