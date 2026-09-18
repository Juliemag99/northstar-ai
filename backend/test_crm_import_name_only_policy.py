"""Phase 1 — CRM import must not auto-reuse a stored master on name alone.

Isolated testdb only. Does not write production northstar.db.
Does not rename or merge live Johnson Controls rows.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import testdb

from crm_add_data import match_company
from crm_import_plan import (
    PLANNER_VERSION,
    CompanyRec,
    _match_company,
    _query_company,
    plan_crm_import_batch,
)
from crm_import_state import state_for_match
from crm_import_staging import ensure_crm_import_schema, save_crm_import_mapping
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from models import CrmAddCompanyInput, NorthStarUser
from shared_note_history_import import normalize_record_no


def _rec(
    *,
    name: str,
    company_id: int | None = None,
    proposed_key: str | None = None,
    website: str = "",
    phone: str = "",
    address: str = "",
    city: str = "",
    state: str = "",
    record_no: str = "",
) -> CompanyRec:
    matched_state = state_for_match(state) or ""
    return CompanyRec(
        company_id=company_id,
        proposed_key=proposed_key,
        name=name,
        norm_name=norm_name(name) if name else "",
        domain=domain(website),
        phone=digits_phone(phone),
        addr=norm_addr(address) if address else "",
        city=(city or "").strip().lower(),
        state=matched_state,
        record_no=normalize_record_no(record_no),
    )


class NameOnlyMatchPolicyTests(unittest.TestCase):
    def test_planner_version_bumped_for_name_policy(self) -> None:
        self.assertEqual(PLANNER_VERSION, "crm-import-plan-v11")

    def test_johnson_controls_york_vs_milwaukee_is_review(self) -> None:
        existing = _rec(
            company_id=551,
            name="Johnson Controls",
            address="507 E Michigan St",
            city="Milwaukee",
            state="WI",
            phone="4145244000",
            record_no="102072",
        )
        incoming = _rec(
            name="Johnson Controls",
            address="100 JCI Way",
            city="York",
            state="PA",
            phone="7172681868",
            record_no="1326674",
        )
        action, matched, reasons, extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "possible_company_match")
        self.assertNotEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 551)
        self.assertEqual(reasons, ["name_exact"])
        self.assertTrue(extras)

    def test_same_record_no_legal_suffix_and_road_spelling_reuses(self) -> None:
        existing = _rec(
            company_id=10,
            name="ABC Manufacturing Inc.",
            address="1055 Jordan Rd",
            city="Des Moines",
            state="IA",
            record_no="12345",
        )
        incoming = _rec(
            name="ABC Manufacturing",
            address="1055 Jordan Road",
            city="Des Moines",
            state="IA",
            record_no="12345",
        )
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 10)
        self.assertEqual(reasons, ["record_no_exact"])

    def test_name_plus_domain_and_compatible_address_reuses(self) -> None:
        existing = _rec(
            company_id=20,
            name="ABC Manufacturing Inc.",
            website="https://www.abcmfg.example",
            address="1055 Jordan Rd",
            city="Des Moines",
            state="IA",
            record_no="99901",
        )
        incoming = _rec(
            name="ABC Manufacturing",
            website="https://abcmfg.example/about",
            address="1055 Jordan Road",
            city="Des Moines",
            state="IA",
            record_no="99902",
        )
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 20)
        self.assertIn("domain_exact", reasons)
        self.assertIn("name_exact", reasons)
        self.assertIn("address_city_state", reasons)

    def test_same_name_different_city_address_phone_rn_is_review(self) -> None:
        existing = _rec(
            company_id=30,
            name="Acme Stampings",
            address="1 Main St",
            city="Ames",
            state="IA",
            phone="5155551212",
            record_no="1001",
        )
        incoming = _rec(
            name="Acme Stampings",
            address="9 Oak Ave",
            city="York",
            state="PA",
            phone="7175559999",
            record_no="2002",
        )
        action, matched, _reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "possible_company_match")
        self.assertNotEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 30)

    def test_legal_suffix_alone_does_not_block_corroborated_reuse(self) -> None:
        existing = _rec(
            company_id=40,
            name="Parsons Company, Inc.",
            address="1055 Jordan Rd",
            city="Des Moines",
            state="IA",
            phone="5155550100",
            record_no="777",
        )
        incoming = _rec(
            name="Parsons Co",
            address="1055 Jordan Road",
            city="Des Moines",
            state="IA",
            phone="(515) 555-0100",
            record_no="888",
        )
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 40)
        self.assertIn("name_exact", reasons)
        self.assertTrue("phone" in reasons or "address_city_state" in reasons)

    def test_jeld_wen_display_name_is_not_rewritten(self) -> None:
        mapped = _query_company(
            {
                "company_name": "JELD-WEN",
                "website": "",
                "phone": "5418823457",
                "address": "2647 Powell Blvd",
                "city": "Portland",
                "state": "OR",
                "external_record_no": "55501",
            }
        )
        self.assertEqual(mapped.name, "JELD-WEN")
        self.assertNotIn("Inc", mapped.name)
        self.assertEqual(mapped.norm_name, norm_name("JELD-WEN Inc."))
        existing = _rec(
            company_id=50,
            name="JELD-WEN",
            address="2647 Powell Blvd",
            city="Portland",
            state="OR",
            phone="5418823457",
            record_no="55501",
        )
        incoming = _rec(
            name="JELD-WEN, Inc.",
            address="2647 Powell Boulevard",
            city="Portland",
            state="OR",
            phone="541-882-3457",
            record_no="55501",
        )
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 50)
        self.assertEqual(reasons, ["record_no_exact"])
        self.assertEqual(incoming.name, "JELD-WEN, Inc.")

    def test_unique_name_only_against_stored_master_is_review(self) -> None:
        existing = _rec(company_id=60, name="Name Only Target", record_no="91001")
        incoming = _rec(name="Name Only Target")
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "possible_company_match")
        self.assertEqual(reasons, ["name_exact"])
        self.assertEqual(matched.company_id if matched else None, 60)

    def test_in_batch_proposed_unique_name_still_groups_contacts(self) -> None:
        proposed = _rec(
            proposed_key="proposed:company:2:1",
            name="Within Batch Co",
        )
        incoming = _rec(name="Within Batch Co")
        action, matched, reasons, _extras = _match_company(incoming, [], [proposed])
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.proposed_key if matched else None, "proposed:company:2:1")
        self.assertEqual(reasons, ["name_exact"])

    def test_in_batch_same_name_different_city_is_review(self) -> None:
        proposed = _rec(
            proposed_key="proposed:company:2:1",
            name="Johnson Controls",
            address="100 JCI Way",
            city="York",
            state="PA",
            record_no="1326674",
        )
        incoming = _rec(
            name="Johnson Controls",
            address="507 E Michigan St",
            city="Milwaukee",
            state="WI",
            record_no="102072",
        )
        action, _matched, _reasons, _extras = _match_company(incoming, [], [proposed])
        self.assertEqual(action, "possible_company_match")


class CrmAddNameOnlyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())

    def test_ai_add_name_only_with_different_city_is_possible(self) -> None:
        company_id: int | None = None
        with get_connection() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip,
                        legacy_phone, created_at, last_updated_at
                    ) VALUES (
                        'NS-JC-TEST-102072', 'Johnson Controls Policy',
                        '507 E Michigan St', 'Milwaukee', 'WI', '53202',
                        '(414) 524-4000', datetime('now'), datetime('now')
                    )
                    """
                )
                company_id = int(cur.lastrowid)
                conn.commit()
                match = match_company(
                    conn,
                    CrmAddCompanyInput(
                        company_name="Johnson Controls Policy",
                        address="100 JCI Way",
                        city="York",
                        state="PA",
                        phone="7172681868",
                    ),
                )
            finally:
                if company_id is not None:
                    conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
                    conn.commit()
        self.assertEqual(match["match_type"], "possible_match")
        self.assertEqual(int(match["company_id"]), company_id)
        self.assertEqual(match["reasons"], ["name_exact"])


class PlannerJohnsonControlsRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
            self.client_id = int(row["id"])
            actor_row = conn.execute(
                "SELECT * FROM users WHERE is_administrator = 1 ORDER BY id LIMIT 1"
            ).fetchone()
            self.actor = NorthStarUser(
                id=int(actor_row["id"]),
                email=str(actor_row["email"] or ""),
                full_name=str(actor_row["full_name"] or "Admin"),
                is_administrator=True,
                is_internal_northstar=True,
                active=True,
                created_at=str(actor_row["created_at"] or ""),
            )

    def test_planner_york_johnson_controls_needs_review(self) -> None:
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM companies
                WHERE external_record_no = '102072'
                  AND company_name = 'Johnson Controls'
                """
            ).fetchone()
            self.assertIsNotNone(existing)
            company_id = int(existing["id"])
            batch = conn.execute(
                """
                INSERT INTO crm_import_batches (
                    client_id, uploaded_by_user_id, uploaded_by_name, original_filename,
                    file_type, sha256, status, headers_json, total_rows, source_row_count,
                    created_at, updated_at, expires_at
                ) VALUES (?, 1, 'Admin', 'jc.csv', 'csv', 'sha-jc-policy', 'previewed', ?, 1, 1,
                          datetime('now'), datetime('now'), '2099-01-01T00:00:00Z')
                """,
                (
                    self.client_id,
                    json.dumps(
                        [
                            "Company",
                            "LeadMaster Record No.",
                            "Address",
                            "City",
                            "State",
                            "Phone",
                        ]
                    ),
                ),
            )
            batch_id = int(batch.lastrowid)
            conn.execute(
                """
                INSERT INTO crm_import_rows (
                    batch_id, client_id, source_row_number, raw_json,
                    warnings_json, errors_json, is_blank, has_blocking_error
                ) VALUES (?, ?, 2, ?, '[]', '[]', 0, 0)
                """,
                (
                    batch_id,
                    self.client_id,
                    json.dumps(
                        {
                            "Company": "Johnson Controls",
                            "LeadMaster Record No.": "1326674",
                            "Address": "100 JCI Way",
                            "City": "York",
                            "State": "PA",
                            "Phone": "(717) 268-1868",
                        }
                    ),
                ),
            )
            conn.commit()
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=self.actor,
            mapping={
                "company_name": "Company",
                "external_record_no": "LeadMaster Record No.",
                "address": "Address",
                "city": "City",
                "state": "State",
                "phone": "Phone",
            },
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            row = plan.rows[0]
            conn.execute("DELETE FROM crm_import_rows WHERE batch_id = ?", (batch_id,))
            conn.execute("DELETE FROM crm_import_batches WHERE id = ?", (batch_id,))
            conn.commit()
        self.assertEqual(row.company_action, "possible_company_match")
        self.assertNotEqual(row.company_action, "use_existing_company")
        self.assertIsNone(row.company_id)
        possible_ids = {item.company_id for item in row.company_possibles}
        self.assertIn(company_id, possible_ids)
        self.assertEqual(row.company_reasons, ["name_exact"])


if __name__ == "__main__":
    unittest.main()
