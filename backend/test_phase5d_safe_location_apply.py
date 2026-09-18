"""Phase 5D safe location backfill helpers. Isolated testdb only."""
from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

import testdb

from _phase5d_safe_location_apply import (
    Candidate,
    apply_approved,
    evaluate_candidate,
    format_location_phone,
    load_company_sites,
    parse_place,
)
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema


def _company(conn, name: str, *, address: str, city: str, state: str, zip_code: str = "", phone: str = "") -> int:
    rn = f"P5D-{secrets.token_hex(4)}"
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, address, city, state, zip,
            legacy_phone, created_at, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (rn, name, address, city, state, zip_code, phone),
    )
    return int(cur.lastrowid)


def _cand(**kwargs) -> Candidate:
    base = dict(
        company_id=0,
        master_name="Acme",
        master_rn="1",
        master_address="100 Main St",
        master_city="Springfield",
        master_state="IL",
        master_zip="62701",
        master_phone="(217) 555-0100",
        master_phone_digits="2175550100",
        master_website="",
        alias_name="Acme",
        alias_norm="acme",
        source_system="crm_import",
        source_record_no="RN-1",
        client_id=2,
        source_address="200 Other St",
        source_city="Birmingham",
        source_state="AL",
        source_zip="35234",
        source_phone="(205) 555-0199",
        source_website="",
        source_batch_id=1,
        source_row=2,
        klass5c="SAFE LOCATION CREATE",
        why="test",
    )
    base.update(kwargs)
    return Candidate(**base)


class Phase5DSafeLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def test_parse_place_extracts_city_state_zip(self) -> None:
        city, state, zip_code = parse_place("Tulsa, OK 74107", "", "")
        self.assertEqual(city, "Tulsa")
        self.assertEqual(state, "OK")
        self.assertEqual(zip_code, "74107")

    def test_shared_master_phone_is_omitted_when_city_differs(self) -> None:
        phone, ext, omit = format_location_phone(
            "(573) 443-8817", "5734438817", city_differs=True
        )
        self.assertEqual(phone, "")
        self.assertEqual(ext, "")
        self.assertIn("master phone", omit)
        phone, ext, omit = format_location_phone(
            "(205) 555-0199", "2175550100", city_differs=True
        )
        self.assertEqual(phone, "(205) 555-0199")
        self.assertEqual(omit, "")

    def test_extension_preserved_on_trusted_phone(self) -> None:
        phone, ext, omit = format_location_phone(
            "(205) 555-0199 ext 44", "2175550100", city_differs=True
        )
        self.assertEqual(phone, "(205) 555-0199")
        self.assertEqual(ext, "44")
        self.assertEqual(omit, "")

    def test_dual_source_phone_is_omitted(self) -> None:
        phone, ext, omit = format_location_phone(
            "(816) 415-6787 / (816) 223-8268", "2175550100", city_differs=True
        )
        self.assertEqual(phone, "")
        self.assertIn("ambiguous", omit)

    def test_existing_master_gate_defers(self) -> None:
        with get_connection() as conn:
            other = _company(
                conn,
                "Other Master",
                address="1730 Vanderbilt Rd",
                city="Birmingham",
                state="AL",
                zip_code="35234",
            )
            org = _company(
                conn,
                "Org Co",
                address="100 Main St",
                city="Springfield",
                state="IL",
                zip_code="62701",
            )
            sites = load_company_sites(conn)
            cand = _cand(
                company_id=org,
                source_address="1730 Vanderbilt Rd",
                source_city="Birmingham",
                source_state="AL",
                source_zip="35234",
            )
            evaluate_candidate(cand, sites)
            self.assertEqual(cand.decision, "DEFERRED")
            self.assertEqual(cand.existing_master_id, other)
            self.assertIn("Existing master company", cand.reason)

    def test_protected_johnson_is_deferred(self) -> None:
        with get_connection() as conn:
            sites = load_company_sites(conn)
            cand = _cand(company_id=551, master_name="Johnson Controls", alias_name="Johnson Controls")
            evaluate_candidate(cand, sites)
            self.assertEqual(cand.decision, "DEFERRED")
            self.assertIn("protected", cand.reason.lower())

    def test_apply_one_location_two_identities_and_idempotent_rerun(self) -> None:
        with get_connection() as conn:
            org = _company(
                conn,
                "Unique Org P5D",
                address="1 Unique P5D Ave",
                city="Zzzville",
                state="IA",
                zip_code="50000",
            )
            sites = load_company_sites(conn)
            a = _cand(
                company_id=org,
                master_name="Unique Org P5D",
                source_system="crm_import",
                source_record_no="P5D-RN-A",
                source_address="88 Unique Plant Rd",
                source_city="Cedar Rapids",
                source_state="IA",
                source_zip="52404",
                source_phone="(319) 555-0111",
            )
            b = _cand(
                company_id=org,
                master_name="Unique Org P5D",
                source_system="shared_history",
                source_record_no="P5D-RN-B",
                source_address="88 Unique Plant Rd",
                source_city="Cedar Rapids",
                source_state="IA",
                source_zip="52404",
                source_phone="(319) 555-0111",
                alias_name="Unique Org P5D Plant",
            )
            evaluate_candidate(a, sites)
            evaluate_candidate(b, sites)
            self.assertEqual(a.decision, "APPLIED")
            self.assertEqual(b.decision, "APPLIED")
            first = apply_approved(conn, [a, b], dry_run=False)
            conn.commit()
            second = apply_approved(conn, [a, b], dry_run=False)
            conn.commit()
            n_loc = conn.execute(
                "SELECT COUNT(*) FROM company_locations WHERE company_id=?", (org,)
            ).fetchone()[0]
            n_ident = conn.execute(
                "SELECT COUNT(*) FROM company_source_identities WHERE company_id=?", (org,)
            ).fetchone()[0]
            loc = conn.execute(
                "SELECT is_headquarters, is_primary, phone, phone_extension FROM company_locations WHERE company_id=?",
                (org,),
            ).fetchone()
            contacts_located = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE location_id IS NOT NULL"
            ).fetchone()[0]
        self.assertEqual(first["locations_created"], 1)
        self.assertEqual(first["identities_created"], 2)
        self.assertEqual(first["multi_identity_sites"], 1)
        self.assertEqual(second["locations_created"], 0)
        self.assertEqual(second["identities_created"], 0)
        self.assertEqual(second["locations_reused"], 1)
        self.assertEqual(n_loc, 1)
        self.assertEqual(n_ident, 2)
        self.assertEqual(int(loc["is_headquarters"]), 0)
        self.assertEqual(int(loc["is_primary"]), 0)
        self.assertEqual(loc["phone"], "(319) 555-0111")
        self.assertEqual(contacts_located, 0)

    def test_identity_conflict_on_apply_marks_deferred(self) -> None:
        with get_connection() as conn:
            org_a = _company(conn, "A Co", address="1 A St", city="Ames", state="IA", zip_code="50010")
            org_b = _company(conn, "B Co", address="2 B St", city="Boone", state="IA", zip_code="50036")
            from company_locations import upsert_company_source_identity

            upsert_company_source_identity(
                conn,
                company_id=org_a,
                source_system="crm_import",
                source_record_no="SHARED-CONFLICT",
            )
            sites = load_company_sites(conn)
            cand = _cand(
                company_id=org_b,
                master_name="B Co",
                client_id=None,
                source_record_no="SHARED-CONFLICT",
                source_address="8800 Neverused P5D Ln",
                source_city="Dubuque",
                source_state="IA",
                source_zip="52001",
            )
            evaluate_candidate(cand, sites)
            self.assertEqual(cand.decision, "APPLIED")
            stats = apply_approved(conn, [cand], dry_run=False)
            conn.commit()
            self.assertEqual(stats["identities_conflict"], 1)
            self.assertEqual(stats["locations_created"], 0)
            self.assertEqual(cand.decision, "DEFERRED")
            self.assertIn("conflicts", cand.reason.lower())
            owner = conn.execute(
                """
                SELECT company_id FROM company_source_identities
                WHERE source_system='crm_import' AND source_record_no='SHARED-CONFLICT'
                """
            ).fetchone()
            self.assertEqual(int(owner["company_id"]), org_a)


if __name__ == "__main__":
    unittest.main()
