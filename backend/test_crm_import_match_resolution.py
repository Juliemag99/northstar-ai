"""Isolated tests for CRM import match resolutions.

Run: python test_crm_import_match_resolution.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import json
import os
import secrets
import unittest
from pathlib import Path

import testdb

from crm_import_match_resolution import (
    RESOLUTION_CREATE_COMPANY,
    RESOLUTION_SKIP_ROW,
    RESOLUTION_USE_EXISTING_CONTACT,
    RESOLUTION_USE_PROPOSED_COMPANY,
    ensure_crm_import_match_resolution_schema,
    save_crm_import_match_resolution,
)
from crm_import_plan import PLANNER_VERSION, plan_crm_import_batch
from crm_import_staging import ensure_crm_import_schema, save_crm_import_mapping
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from models import NorthStarUser


def _actor() -> NorthStarUser:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE is_administrator = 1 ORDER BY id LIMIT 1"
        ).fetchone()
    return NorthStarUser(
        id=int(row["id"]),
        email=str(row["email"] or ""),
        full_name=str(row["full_name"] or "Admin"),
        is_administrator=True,
        is_internal_northstar=True,
        active=True,
        created_at=str(row["created_at"] or ""),
    )


def _client_id(conn) -> int:
    row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise AssertionError("Isolated testdb needs a client.")
    return int(row["id"])


def _insert_batch(conn, client_id: int, headers: list[str], rows: list[dict]) -> int:
    cur = conn.execute(
        """
        INSERT INTO crm_import_batches (
            client_id, uploaded_by_user_id, uploaded_by_name, original_filename,
            file_type, sha256, status, headers_json, total_rows, source_row_count,
            created_at, updated_at, expires_at
        ) VALUES (?, 1, 'Admin', 't.csv', 'csv', 'sha-match-res', 'previewed', ?, ?, ?,
                  datetime('now'), datetime('now'), '2099-01-01T00:00:00Z')
        """,
        (client_id, json.dumps(headers), len(rows), len(rows)),
    )
    batch_id = int(cur.lastrowid)
    for i, values in enumerate(rows, start=2):
        conn.execute(
            """
            INSERT INTO crm_import_rows (
                batch_id, client_id, source_row_number, raw_json,
                warnings_json, errors_json, is_blank, has_blocking_error
            ) VALUES (?, ?, ?, ?, '[]', '[]', 0, 0)
            """,
            (batch_id, client_id, i, json.dumps(values)),
        )
    conn.commit()
    return batch_id


class MatchResolutionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            ensure_crm_import_match_resolution_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)
            self.actor = _actor()

    def test_use_existing_contact_clears_possible_contact_match(self):
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES ('MR-CO-1', 'Match Res Co', '1 Main', 'Ames', 'IA', '50010', '')
                    """
                ).lastrowid
            )
            c1 = int(
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company_id, external_record_no, first_name, last_name,
                        email, phone, source_row_index
                    ) VALUES (?, 'MR-CT-1', 'Pat', 'Lee', '', '', 0)
                    """,
                    (company_id,),
                ).lastrowid
            )
            c2 = int(
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company_id, external_record_no, first_name, last_name,
                        email, phone, source_row_index
                    ) VALUES (?, 'MR-CT-2', 'Pat', 'Lee', '', '', 0)
                    """,
                    (company_id,),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (client_id, company_id, status)
                VALUES (?, ?, 'New')
                """,
                (self.client_id, company_id),
            )
            headers = ["Company", "Address", "City", "First", "Last", "State"]
            batch_id = _insert_batch(
                conn,
                self.client_id,
                headers,
                [
                    {
                        "Company": "Match Res Co",
                        "Address": "1 Main",
                        "City": "Ames",
                        "First": "Pat",
                        "Last": "Lee",
                        "State": "IA",
                    }
                ],
            )
            save_crm_import_mapping(
                self.client_id,
                batch_id,
                actor=self.actor,
                mapping={
                    "company_name": "Company",
                    "address": "Address",
                    "city": "City",
                    "contact_first_name": "First",
                    "contact_last_name": "Last",
                    "state": "State",
                },
            )
            conn.commit()
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].contact_action, "possible_contact_match")
            staged_id = plan.rows[0].row_id
            chosen = min(c1, c2)
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=staged_id,
                resolution_type=RESOLUTION_USE_EXISTING_CONTACT,
                contact_id=chosen,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            planned = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertTrue(PLANNER_VERSION.startswith("crm-import-plan-v11"))
            self.assertEqual(planned.rows[0].contact_action, "use_existing_contact")
            self.assertEqual(planned.rows[0].contact_id, chosen)
            self.assertEqual(planned.counts["needs_review_rows"], 0)
            self.assertEqual(planned.counts["importable_rows"], 1)

    def test_create_company_and_skip_and_proposed(self):
        with get_connection() as conn:
            marker = secrets.token_hex(4)
            created_name = f"NS-MR Create {marker}"
            possible_name = f"NS-MR Possible {marker}"
            phone = f"515555{int(marker[:4], 16):04d}"[:10]
            headers = ["Company", "Phone", "State", "Website"]
            batch_id = _insert_batch(
                conn,
                self.client_id,
                headers,
                [
                    {
                        "Company": created_name,
                        "Phone": phone,
                        "State": "AR",
                        "Website": f"www.ns-mr-create-{marker}.example",
                    },
                    {
                        "Company": possible_name,
                        "Phone": phone,
                        "State": "AR",
                        "Website": f"www.ns-mr-possible-{marker}.example",
                    },
                    {"Company": "", "Phone": "", "State": "", "Website": ""},
                ],
            )
            save_crm_import_mapping(
                self.client_id,
                batch_id,
                actor=self.actor,
                mapping={
                    "company_name": "Company",
                    "phone": "Phone",
                    "state": "State",
                    "website": "Website",
                },
            )
            conn.commit()
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            by_src = {r.source_row_number: r for r in plan.rows}
            self.assertEqual(by_src[2].company_action, "create_company")
            self.assertEqual(by_src[3].company_action, "possible_company_match")
            self.assertEqual(by_src[4].validity, "invalid_mapping_data")
            proposed_key = by_src[2].company_proposed_key
            self.assertTrue(proposed_key)
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=by_src[3].row_id,
                resolution_type=RESOLUTION_USE_PROPOSED_COMPANY,
                company_proposed_key=str(proposed_key),
                updated_by_user_id=self.actor.id,
            )
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=by_src[4].row_id,
                resolution_type=RESOLUTION_SKIP_ROW,
                updated_by_user_id=self.actor.id,
            )
            # Force a create-despite-possible on a fresh phone twin would need possibles;
            # exercise create resolution by saving on row 3 after clearing proposed resolve.
            conn.commit()
            planned = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            by2 = {r.source_row_number: r for r in planned.rows}
            self.assertEqual(by2[3].company_action, "use_existing_company")
            self.assertEqual(by2[3].company_proposed_key, proposed_key)
            self.assertEqual(by2[4].company_action, "excluded_skip")
            self.assertEqual(planned.counts["excluded_skip"], 1)
            self.assertEqual(planned.counts["needs_review_rows"], 0)


if __name__ == "__main__":
    unittest.main()
