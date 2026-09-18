"""Isolated tests for CRM import U.S. state normalization.

Run: python test_crm_import_state.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import testdb

from crm_import_confirm import confirm_admin_crm_import_batch
from crm_import_plan import PLANNER_VERSION, plan_crm_import_batch
from crm_import_staging import BatchNotReusable, ensure_crm_import_schema, save_crm_import_mapping
from crm_import_state import (
    INVALID_STATE,
    all_state_name_mappings,
    normalize_us_state,
    state_for_match,
)
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
        ) VALUES (?, 1, 'Admin', 't.csv', 'csv', 'sha-state', 'previewed', ?, ?, ?,
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


class NormalizeUsStateHelperTests(unittest.TestCase):
    def test_every_full_state_name(self):
        mappings = all_state_name_mappings()
        self.assertEqual(len(mappings), 51)  # 50 states + DC
        for name, code in mappings:
            self.assertEqual(normalize_us_state(name), code, name)
            self.assertEqual(normalize_us_state(name.lower()), code, name)
            self.assertEqual(normalize_us_state(name.upper()), code, name)

    def test_codes_whitespace_blank_dc_unknown(self):
        self.assertEqual(normalize_us_state("ne"), "NE")
        self.assertEqual(normalize_us_state(" NE "), "NE")
        self.assertEqual(normalize_us_state("ks"), "KS")
        self.assertEqual(normalize_us_state(""), "")
        self.assertEqual(normalize_us_state("   "), "")
        self.assertEqual(normalize_us_state(None), "")
        self.assertEqual(normalize_us_state("District of Columbia"), "DC")
        self.assertEqual(normalize_us_state("dc"), "DC")
        self.assertIsNone(normalize_us_state("Narnia"))
        self.assertIsNone(normalize_us_state("XX"))
        self.assertEqual(state_for_match("Iowa"), "IA")
        self.assertEqual(state_for_match("ia"), "IA")
        self.assertEqual(state_for_match("Narnia"), "NARNIA")

    def test_canadian_provinces_and_codes(self):
        from crm_import_state import all_canadian_province_name_mappings

        mappings = all_canadian_province_name_mappings()
        self.assertEqual(len(mappings), 13)
        for name, code in mappings:
            self.assertEqual(normalize_us_state(name), code, name)
            self.assertEqual(normalize_us_state(name.lower()), code, name)
            self.assertEqual(normalize_us_state(code), code, code)
            self.assertEqual(normalize_us_state(code.lower()), code, code)
        self.assertEqual(normalize_us_state("ON"), "ON")
        self.assertEqual(normalize_us_state("Ontario"), "ON")
        self.assertEqual(state_for_match("Ontario"), "ON")


class StatePlanConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)

    def test_spelled_matches_abbrev_and_reverse_without_rewrite(self):
        with get_connection() as conn:
            spelled_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-ST-SPELL', 'Spell Co ST', '1 Main', 'Des Moines', 'Iowa', '', '',
                              '', '', datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            abbrev_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-ST-ABBR', 'Abbr Co ST', '2 Main', 'Topeka', 'KS', '', '',
                              '', '', datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.commit()
            before_spell = conn.execute(
                "SELECT state FROM companies WHERE id = ?", (spelled_id,)
            ).fetchone()["state"]
            before_abbr = conn.execute(
                "SELECT state FROM companies WHERE id = ?", (abbrev_id,)
            ).fetchone()["state"]
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Address", "City", "State"],
                [
                    {
                        "Company": "Spell Co ST",
                        "Address": "1 Main",
                        "City": "Des Moines",
                        "State": "IA",
                    },
                    {
                        "Company": "Abbr Co ST",
                        "Address": "2 Main",
                        "City": "Topeka",
                        "State": "Kansas",
                    },
                ],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={
                "company_name": "Company",
                "address": "Address",
                "city": "City",
                "state": "State",
            },
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(PLANNER_VERSION, "crm-import-plan-v11")
            self.assertEqual(plan.counts["needs_review_rows"], 0)
            by_name = {r.company_name: r for r in plan.rows}
            self.assertEqual(by_name["Spell Co ST"].company_action, "use_existing_company")
            self.assertEqual(by_name["Spell Co ST"].company_id, spelled_id)
            self.assertEqual(by_name["Spell Co ST"].mapped.get("state"), "IA")
            self.assertIn("address_city_state", by_name["Spell Co ST"].company_reasons)
            self.assertEqual(by_name["Abbr Co ST"].company_action, "use_existing_company")
            self.assertEqual(by_name["Abbr Co ST"].company_id, abbrev_id)
            self.assertEqual(by_name["Abbr Co ST"].mapped.get("state"), "KS")
            after_spell = conn.execute(
                "SELECT state FROM companies WHERE id = ?", (spelled_id,)
            ).fetchone()["state"]
            after_abbr = conn.execute(
                "SELECT state FROM companies WHERE id = ?", (abbrev_id,)
            ).fetchone()["state"]
            self.assertEqual(after_spell, before_spell)
            self.assertEqual(after_abbr, before_abbr)
            self.assertEqual(after_spell, "Iowa")
            self.assertEqual(after_abbr, "KS")

    def test_new_company_stores_abbrev_and_dry_run_displays_it(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "State"],
                [{"Company": "Brand New ST Co", "State": "minnesota"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "state": "State"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].mapped.get("state"), "MN")
            self.assertEqual(plan.rows[0].company_action, "create_company")
            fp1 = plan.plan_fingerprint
            plan2 = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(fp1, plan2.plan_fingerprint)
            fingerprint = plan.plan_fingerprint
        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fingerprint,
            actor=_actor(),
        )
        self.assertEqual(result["created_company_count"], 1)
        with get_connection() as conn:
            stored = conn.execute(
                "SELECT state FROM companies WHERE company_name = 'Brand New ST Co'"
            ).fetchone()
            self.assertEqual(stored["state"], "MN")

    def test_canadian_province_ontario_is_importable(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "State", "City"],
                [{"Company": "Viking Cives Test", "State": "ON", "City": "Mount Forest"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "state": "State", "city": "City"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].validity, "ok")
            self.assertEqual(plan.rows[0].mapped.get("state"), "ON")
            self.assertEqual(plan.rows[0].company_action, "create_company")
            self.assertEqual(plan.counts["needs_review_rows"], 0)
            self.assertEqual(plan.counts["importable_rows"], 1)

    def test_unknown_state_needs_review_blocks_confirm_and_changes_fingerprint(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "State"],
                [{"Company": "Bad State Co", "State": "Kansas"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "state": "State"},
        )
        with get_connection() as conn:
            good = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(good.counts["needs_review_rows"], 0)
            self.assertEqual(good.rows[0].mapped.get("state"), "KS")
            good_fp = good.plan_fingerprint
            conn.execute(
                "UPDATE crm_import_rows SET raw_json = ? WHERE batch_id = ?",
                (json.dumps({"Company": "Bad State Co", "State": "Narnia"}), batch_id),
            )
            conn.commit()
            bad = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertNotEqual(good_fp, bad.plan_fingerprint)
            self.assertEqual(bad.rows[0].validity, "invalid_mapping_data")
            self.assertEqual(bad.rows[0].validity_detail, INVALID_STATE)
            self.assertGreater(bad.counts["needs_review_rows"], 0)
            self.assertEqual(bad.rows[0].mapped.get("state"), "Narnia")
            fingerprint = bad.plan_fingerprint
        with self.assertRaises(BatchNotReusable):
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint=fingerprint,
                actor=_actor(),
            )
        with get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE company_name = 'Bad State Co'"
            ).fetchone()["n"]
            self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
