"""All My Clients company status aggregation for Companies page.

Run: python test_all_clients_company_statuses.py
Isolated testdb only — never writes production.
"""

from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

import testdb
from access import get_default_user
from client_workspace_data import list_prospects_page
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema


def _uid() -> int:
    user = get_default_user()
    assert user is not None
    return int(user.id)


class AllClientsCompanyStatusesTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            clients = conn.execute(
                "SELECT id, code, name FROM clients ORDER BY id"
            ).fetchall()
            if len(clients) < 2:
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES ('ac_a', 'Alpha Client')"
                )
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES ('ac_b', 'Beta Client')"
                )
                conn.commit()
                clients = conn.execute(
                    "SELECT id, code, name FROM clients ORDER BY id"
                ).fetchall()
            self.client_a = int(clients[0]["id"])
            self.client_b = int(clients[1]["id"])
            # Ensure actor can access both (admin default user usually can).
            uid = _uid()
            for cid in (self.client_a, self.client_b):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_client_assignments
                        (user_id, client_id, role, active, assigned_at)
                    VALUES (?, ?, 'staff', 1, datetime('now'))
                    """,
                    (uid, cid),
                )
            # Third inaccessible client for negative auth proof.
            conn.execute(
                "INSERT INTO clients (code, name) VALUES (?, ?)",
                (f"hidden_{secrets.token_hex(3)}", "Hidden Client ZZ"),
            )
            self.hidden_client = int(
                conn.execute(
                    "SELECT id FROM clients WHERE name = 'Hidden Client ZZ' ORDER BY id DESC LIMIT 1"
                ).fetchone()["id"]
            )
            # Non-admin user without hidden client access.
            conn.execute(
                """
                INSERT INTO users (
                    email, full_name, is_administrator, is_internal_northstar, active
                ) VALUES (?, 'Limited Staff', 0, 1, 1)
                """,
                (f"limited.{secrets.token_hex(4)}@example.test",),
            )
            self.limited_uid = int(
                conn.execute(
                    "SELECT id FROM users WHERE email LIKE 'limited.%' ORDER BY id DESC LIMIT 1"
                ).fetchone()["id"]
            )
            for cid in (self.client_a, self.client_b):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_client_assignments
                        (user_id, client_id, role, active, assigned_at)
                    VALUES (?, ?, 'staff', 1, datetime('now'))
                    """,
                    (self.limited_uid, cid),
                )
            conn.commit()

    def _insert_company(self, name: str) -> int:
        rn = f"AC-{secrets.token_hex(4)}"
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES (?, ?, datetime('now'), datetime('now'))
                """,
                (rn, name),
            )
            company_id = int(cur.lastrowid)
            conn.commit()
            return company_id

    def _link(
        self,
        *,
        client_id: int,
        company_id: int,
        status: str,
        record_no: str | None = None,
    ) -> int:
        rn = record_no or f"R-{client_id}-{company_id}"
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, status, external_record_no,
                    assigned_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                (client_id, company_id, status, rn, _uid()),
            )
            rel_id = int(cur.lastrowid)
            conn.commit()
            return rel_id

    def test_multi_client_one_row_labeled_statuses_sorted(self) -> None:
        company_id = self._insert_company("Multi Status Co")
        # Insert out of name order: Beta then Alpha → response must sort by client name.
        self._link(client_id=self.client_b, company_id=company_id, status="New")
        self._link(
            client_id=self.client_a, company_id=company_id, status="Appointment Set"
        )
        # Inaccessible relationship must not appear.
        self._link(
            client_id=self.hidden_client,
            company_id=company_id,
            status="Secret Hot",
        )

        page = list_prospects_page(
            all_clients=True, user_id=self.limited_uid, q="Multi Status Co"
        )
        rows = [p for p in page["prospects"] if p.id == company_id]
        self.assertEqual(len(rows), 1, "one row per shared company")
        row = rows[0]
        self.assertEqual(row.status, "")
        self.assertEqual(row.relationship_status, "")
        self.assertEqual(len(row.client_statuses), 2)
        names = [c.client_name for c in row.client_statuses]
        self.assertEqual(names, sorted(names, key=lambda n: n.casefold()))
        labels = {(c.client_name, c.status) for c in row.client_statuses}
        # Resolve actual client names from DB for assertion flexibility.
        with get_connection() as conn:
            a_name = conn.execute(
                "SELECT name FROM clients WHERE id = ?", (self.client_a,)
            ).fetchone()["name"]
            b_name = conn.execute(
                "SELECT name FROM clients WHERE id = ?", (self.client_b,)
            ).fetchone()["name"]
        self.assertIn((a_name, "Appointment Set"), labels)
        self.assertIn((b_name, "New"), labels)
        self.assertTrue(all(c.status != "Secret Hot" for c in row.client_statuses))
        self.assertTrue(
            all(c.client_id != self.hidden_client for c in row.client_statuses)
        )

    def test_one_client_company_and_missing_status(self) -> None:
        company_id = self._insert_company("Single Client Co")
        self._link(client_id=self.client_a, company_id=company_id, status="")
        page = list_prospects_page(
            all_clients=True, user_id=self.limited_uid, q="Single Client Co"
        )
        rows = [p for p in page["prospects"] if p.id == company_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0].client_statuses), 1)
        self.assertEqual(rows[0].client_statuses[0].status, "")
        self.assertEqual(rows[0].status, "")

    def test_specific_client_mode_unchanged(self) -> None:
        company_id = self._insert_company("Specific Mode Co")
        self._link(client_id=self.client_a, company_id=company_id, status="Hot Prospect")
        self._link(client_id=self.client_b, company_id=company_id, status="New")
        page = list_prospects_page(
            client_id=self.client_a, all_clients=False, user_id=self.limited_uid
        )
        rows = [p for p in page["prospects"] if p.id == company_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "Hot Prospect")
        self.assertEqual(rows[0].relationship_status, "Hot Prospect")
        self.assertEqual(rows[0].client_statuses, [])
        self.assertEqual(rows[0].client_id, self.client_a)

    def test_no_duplicate_company_rows_in_all_clients(self) -> None:
        company_id = self._insert_company("No Dup Co")
        self._link(client_id=self.client_a, company_id=company_id, status="A")
        self._link(client_id=self.client_b, company_id=company_id, status="B")
        page = list_prospects_page(all_clients=True, user_id=self.limited_uid)
        ids = [p.id for p in page["prospects"] if p.id == company_id]
        self.assertEqual(ids, [company_id])


if __name__ == "__main__":
    unittest.main()
