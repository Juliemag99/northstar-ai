"""Revenue & Opportunities KPI aggregate — source-only sales-event integration.

Run from backend/:
  .venv\\Scripts\\python.exe test_revenue_opportunity_kpi.py

Uses testdb isolation only — never writes production northstar.db.
"""

from __future__ import annotations

import unittest
import uuid

import testdb  # noqa: F401 — isolate before other imports

from appointments_data import ensure_appointments_schema
from client_engagement_import import ensure_engagement_import_schema
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from engagement_quote_milestones import (
    apply_quote_milestone_projection,
    plan_quote_milestone_projection,
)
from milestones_data import milestone_summary


MARKER = "NSROKPI"


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
    code = f"kpi-{uuid.uuid4().hex[:10]}"
    cur = conn.execute(
        "INSERT INTO clients (code, name) VALUES (?, ?)",
        (code, name),
    )
    return int(cur.lastrowid)


def _create_company_with_ccr(conn, client_id: int, *, name: str) -> tuple[int, int]:
    rn = f"KPI-{uuid.uuid4().hex[:12]}"
    company_id = int(
        conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name)
            VALUES (?, ?)
            """,
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
    return company_id, rel_id


def _create_batch(conn, client_id: int) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO client_engagement_import_batches (
                client_id, document_id, filename, status, uploaded_at
            ) VALUES (?, NULL, 'kpi-fixture.xlsx', 'imported', datetime('now'))
            """,
            (client_id,),
        ).lastrowid
    )


def _insert_sales_event(
    conn,
    *,
    client_id: int,
    company_id: int,
    relationship_id: int,
    event_type: str,
    fingerprint: str = "",
    quoted_amount: float | None = None,
    source_quoted_value: str = "",
    sales_notes: str = "",
    batch_id: int | None = None,
) -> int:
    fp = fingerprint or f"fp-{uuid.uuid4().hex}"
    cur = conn.execute(
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
            fp,
        ),
    )
    return int(cur.lastrowid)


def _insert_native_appointment(
    conn,
    *,
    client_id: int,
    company_id: int,
    relationship_id: int,
    idempotency_key: str = "",
    status: str = "scheduled",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO appointments (
            client_id, company_id, relationship_id, appointment_date, start_time,
            status, idempotency_key, created_at, updated_at
        ) VALUES (?, ?, ?, '2099-01-15', '10:00', ?, ?, datetime('now'), datetime('now'))
        """,
        (client_id, company_id, relationship_id, status, idempotency_key),
    )
    return int(cur.lastrowid)


def _cleanup_client(conn, client_id: int) -> None:
    conn.execute("DELETE FROM appointments WHERE client_id = ?", (client_id,))
    conn.execute("DELETE FROM client_sales_events WHERE client_id = ?", (client_id,))
    conn.execute("DELETE FROM revenue_milestones WHERE client_id = ?", (client_id,))
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


class RevenueOpportunityKpiTests(unittest.TestCase):
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

    def test_brown_fixture_appointments_and_quotes(self) -> None:
        """53 Scheduled + 7 Rescheduled → 53 Appointments; 26 Quote milestones."""
        client_id = self._new_client("BrownFixture")
        with get_connection() as conn:
            batch_id = _create_batch(conn, client_id)
            companies: list[tuple[int, int]] = []
            for i in range(53):
                companies.append(
                    _create_company_with_ccr(conn, client_id, name=f"{MARKER} Appt {i}")
                )
            for company_id, rel_id in companies:
                _insert_sales_event(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=rel_id,
                    event_type="Appointment Scheduled",
                    batch_id=batch_id,
                )
            for company_id, rel_id in companies[:7]:
                _insert_sales_event(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=rel_id,
                    event_type="Appointment Rescheduled",
                    batch_id=batch_id,
                )
            for i in range(26):
                company_id, rel_id = companies[i]
                if i < 20:
                    _insert_sales_event(
                        conn,
                        client_id=client_id,
                        company_id=company_id,
                        relationship_id=rel_id,
                        event_type="RFQ",
                        batch_id=batch_id,
                    )
                elif i < 23:
                    _insert_sales_event(
                        conn,
                        client_id=client_id,
                        company_id=company_id,
                        relationship_id=rel_id,
                        event_type="Send Info",
                        quoted_amount=1500.0,
                        batch_id=batch_id,
                    )
                else:
                    _insert_sales_event(
                        conn,
                        client_id=client_id,
                        company_id=company_id,
                        relationship_id=rel_id,
                        event_type="Send Info",
                        sales_notes="--- Forecast / Quoted / Won ---\nQuoted: 2200",
                        batch_id=batch_id,
                    )
            company_id, rel_id = companies[0]
            _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Send Info",
                sales_notes="Won the PO yesterday for $40k — purchase order coming",
                batch_id=batch_id,
            )
            plan = plan_quote_milestone_projection(conn, client_id, batch_id)
            self.assertEqual(plan["eligible_company_count"], 26)
            self.assertEqual(plan["purchase_orders_inferred"], 0)
            apply_quote_milestone_projection(
                conn, client_id, batch_id, dry_run=False
            )
            # Idempotent re-run
            again = apply_quote_milestone_projection(
                conn, client_id, batch_id, dry_run=False
            )
            self.assertEqual(again["created_count"], 0)
            conn.commit()

        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.appointments_set, 53)
        self.assertEqual(summary.quotes, 26)
        self.assertEqual(summary.purchase_orders, 0)
        self.assertEqual(summary.webleads, 0)

    def test_brown_isolated_copy_when_scheduled_fixture_present(self) -> None:
        """If isolated copy has Brown batch 4 with 53 scheduled, preview 26 Quotes."""
        with get_connection() as conn:
            brown = conn.execute(
                "SELECT id FROM clients WHERE lower(code) = 'brown' LIMIT 1"
            ).fetchone()
            if brown is None:
                self.skipTest("Brown client not in isolated copy")
            client_id = int(brown["id"])
            scheduled = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM client_sales_events
                    WHERE client_id = ? AND event_type = 'Appointment Scheduled'
                    """,
                    (client_id,),
                ).fetchone()["n"]
            )
            if scheduled != 53:
                self.skipTest(
                    f"Brown scheduled count is {scheduled}, not the 53-event fixture"
                )
            batch = conn.execute(
                """
                SELECT id FROM client_engagement_import_batches
                WHERE client_id = ? AND status = 'imported'
                ORDER BY id DESC LIMIT 1
                """,
                (client_id,),
            ).fetchone()
            if batch is None:
                self.skipTest("No imported Brown engagement batch in isolated copy")
            batch_id = int(batch["id"])
            plan = plan_quote_milestone_projection(conn, client_id, batch_id)
            self.assertEqual(plan["eligible_company_count"], 26)
            self.assertEqual(plan["purchase_orders_inferred"], 0)
            # Apply on isolated copy only (not production)
            apply_quote_milestone_projection(
                conn, client_id, batch_id, dry_run=False
            )
            replan = plan_quote_milestone_projection(conn, client_id, batch_id)
            self.assertEqual(replan["milestones_to_create_count"], 0)
            conn.commit()

        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.appointments_set, 53)
        self.assertEqual(summary.quotes, 26)
        self.assertEqual(summary.purchase_orders, 0)
        self.assertEqual(summary.webleads, 0)

    def test_reschedule_alone_does_not_count(self) -> None:
        client_id = self._new_client("RescheduleOnly")
        with get_connection() as conn:
            company_id, rel_id = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Resched"
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Appointment Rescheduled",
            )
            conn.commit()
        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.appointments_set, 0)

    def test_native_imported_duplicate_counts_once(self) -> None:
        client_id = self._new_client("Dedup")
        with get_connection() as conn:
            company_id, rel_id = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Dedup Co"
            )
            event_id = _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Appointment Scheduled",
                fingerprint="fp-shared-dedup-1",
            )
            _insert_native_appointment(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                idempotency_key=f"sales_event:{event_id}",
            )
            company2, rel2 = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Dedup Co2"
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company2,
                relationship_id=rel2,
                event_type="Appointment Scheduled",
                fingerprint="fp-shared-dedup-2",
            )
            _insert_native_appointment(
                conn,
                client_id=client_id,
                company_id=company2,
                relationship_id=rel2,
                idempotency_key="fp-shared-dedup-2",
            )
            company3, rel3 = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Dedup Co3"
            )
            _insert_native_appointment(
                conn,
                client_id=client_id,
                company_id=company3,
                relationship_id=rel3,
                idempotency_key="",
            )
            conn.commit()
        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.appointments_set, 3)

    def test_quote_signals_dedupe_by_company(self) -> None:
        client_id = self._new_client("QuoteDedupe")
        with get_connection() as conn:
            batch_id = _create_batch(conn, client_id)
            company_id, rel_id = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} Quote Co"
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="RFQ",
                batch_id=batch_id,
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Send Info",
                quoted_amount=900.0,
                batch_id=batch_id,
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Send Info",
                source_quoted_value="$1,200",
                batch_id=batch_id,
            )
            apply_quote_milestone_projection(
                conn, client_id, batch_id, dry_run=False
            )
            conn.commit()
        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.quotes, 1)

    def test_pagination_does_not_affect_totals(self) -> None:
        """KPI aggregate is full-client; unrelated to prospects page size."""
        client_id = self._new_client("Pagination")
        with get_connection() as conn:
            batch_id = _create_batch(conn, client_id)
            for i in range(5):
                company_id, rel_id = _create_company_with_ccr(
                    conn, client_id, name=f"{MARKER} Page {i}"
                )
                _insert_sales_event(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=rel_id,
                    event_type="RFQ",
                    batch_id=batch_id,
                )
                if i == 0:
                    _insert_sales_event(
                        conn,
                        client_id=client_id,
                        company_id=company_id,
                        relationship_id=rel_id,
                        event_type="Purchase Order",
                        batch_id=batch_id,
                    )
                if i == 1:
                    _insert_sales_event(
                        conn,
                        client_id=client_id,
                        company_id=company_id,
                        relationship_id=rel_id,
                        event_type="WebLead",
                        batch_id=batch_id,
                    )
            apply_quote_milestone_projection(
                conn, client_id, batch_id, dry_run=False
            )
            conn.commit()
        a = milestone_summary(self.user_id, client_id=client_id)
        b = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(a.quotes, 5)
        self.assertEqual(a.purchase_orders, 1)
        self.assertEqual(a.webleads, 1)
        self.assertEqual(
            (a.quotes, a.purchase_orders, a.webleads),
            (b.quotes, b.purchase_orders, b.webleads),
        )

    def test_free_text_won_po_does_not_create_purchase_order(self) -> None:
        client_id = self._new_client("FreeTextPO")
        with get_connection() as conn:
            company_id, rel_id = _create_company_with_ccr(
                conn, client_id, name=f"{MARKER} PO Text"
            )
            _insert_sales_event(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=rel_id,
                event_type="Send Info",
                sales_notes="Customer said they sent a purchase order / Won deal",
            )
            conn.commit()
        summary = milestone_summary(self.user_id, client_id=client_id)
        self.assertEqual(summary.purchase_orders, 0)
        self.assertEqual(summary.webleads, 0)

    def test_cross_client_events_isolated(self) -> None:
        client_a = self._new_client("ClientA")
        client_b = self._new_client("ClientB")
        with get_connection() as conn:
            ca, ra = _create_company_with_ccr(conn, client_a, name=f"{MARKER} A Co")
            cb, rb = _create_company_with_ccr(conn, client_b, name=f"{MARKER} B Co")
            batch_a = _create_batch(conn, client_a)
            batch_b = _create_batch(conn, client_b)
            for _ in range(3):
                _insert_sales_event(
                    conn,
                    client_id=client_a,
                    company_id=ca,
                    relationship_id=ra,
                    event_type="Appointment Scheduled",
                    batch_id=batch_a,
                )
            _insert_sales_event(
                conn,
                client_id=client_a,
                company_id=ca,
                relationship_id=ra,
                event_type="RFQ",
                batch_id=batch_a,
            )
            for _ in range(10):
                _insert_sales_event(
                    conn,
                    client_id=client_b,
                    company_id=cb,
                    relationship_id=rb,
                    event_type="Appointment Scheduled",
                    batch_id=batch_b,
                )
            _insert_sales_event(
                conn,
                client_id=client_b,
                company_id=cb,
                relationship_id=rb,
                event_type="Purchase Order",
                batch_id=batch_b,
            )
            apply_quote_milestone_projection(
                conn, client_a, batch_a, dry_run=False
            )
            apply_quote_milestone_projection(
                conn, client_b, batch_b, dry_run=False
            )
            conn.commit()
        summary_a = milestone_summary(self.user_id, client_id=client_a)
        summary_b = milestone_summary(self.user_id, client_id=client_b)
        self.assertEqual(summary_a.appointments_set, 3)
        self.assertEqual(summary_a.quotes, 1)
        self.assertEqual(summary_a.purchase_orders, 0)
        self.assertEqual(summary_b.appointments_set, 10)
        self.assertEqual(summary_b.purchase_orders, 1)
        self.assertEqual(summary_b.quotes, 0)


if __name__ == "__main__":
    unittest.main()
