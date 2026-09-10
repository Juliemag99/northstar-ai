"""Isolated tests for Brown history Record No. aliases.

Run: python test_client_data_history_aliases.py
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import testdb

from client_data_history_aliases import (
    BROWN_HISTORY_RECORD_NO_ALIASES,
    history_company_alias_id,
)
from client_data_history_match import resolve_history_company
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema


class HistoryAliasUnitTests(unittest.TestCase):
    def test_approved_and_batch16_aliases_present(self):
        expected = {
            "1325879": 751,
            "1325884": 751,
            "1325878": 751,
            "1374193": 465,
            "1325585": 465,
            "1393776": 215,
            "1326502": 467,
            "1390613": 878,
            "1395393": 147,
            "1402279": 652,
            "1326501": 425,
            "1401940": 921,
            "1326962": 79,
            "1326946": 1034,
            "1350965": 483,
            "1402335": 333,
        }
        for rn, company_id in expected.items():
            self.assertEqual(BROWN_HISTORY_RECORD_NO_ALIASES[rn], company_id)
            self.assertEqual(
                history_company_alias_id(client_id=2, record_no=rn), company_id
            )
        self.assertIsNone(history_company_alias_id(client_id=1, record_no="1325879"))
        self.assertIsNone(history_company_alias_id(client_id=2, record_no="NOPE"))


class HistoryAliasMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            # Seed Brown client 2 companies referenced by aliases when missing.
            # Testdb may already contain production-like data; ensure IDs exist.
            conn.execute(
                """
                INSERT OR IGNORE INTO clients (id, code, name)
                VALUES (2, 'brown_alias_test', 'Brown Alias Test')
                """
            )
            seeds = [
                (751, "NS-ALIAS-751", "Altec Industries Inc"),
                (465, "NS-ALIAS-465", "JE Dunn Construction Co/Form Off-Site Solutions"),
                (215, "NS-ALIAS-215", "Greenheck Fan"),
                (467, "NS-ALIAS-467", "City Electric Supply"),
                (878, "NS-ALIAS-878", "nVent"),
                (147, "NS-ALIAS-147", "Sioux Chief Mfg. Co, Inc"),
                (652, "NS-ALIAS-652", "Cummins Power Generation"),
                (425, "NS-ALIAS-425", "Ronson Manufacturing"),
                (921, "NS-ALIAS-921", "PULVERIZER"),
                (79, "NS-ALIAS-79", "Massman Automation Designs"),
                (1034, "NS-ALIAS-1034", "Pacmac"),
                (483, "NS-ALIAS-483", "Peterson Manufacturing Co"),
                (333, "NS-ALIAS-333", "HIPOWER Systems"),
            ]
            for company_id, rn, name in seeds:
                exists = conn.execute(
                    "SELECT id FROM companies WHERE id = ?", (company_id,)
                ).fetchone()
                if exists is None:
                    conn.execute(
                        """
                        INSERT INTO companies (
                            id, external_record_no, company_name, created_at, last_updated_at
                        ) VALUES (?, ?, ?, datetime('now'), datetime('now'))
                        """,
                        (company_id, rn, name),
                    )
                rel = conn.execute(
                    """
                    SELECT 1 FROM client_company_relationships
                    WHERE client_id = 2 AND company_id = ?
                    """,
                    (company_id,),
                ).fetchone()
                if rel is None:
                    conn.execute(
                        """
                        INSERT INTO client_company_relationships (
                            client_id, company_id, external_record_no, status,
                            created_at, updated_at
                        ) VALUES (2, ?, ?, 'New', datetime('now'), datetime('now'))
                        """,
                        (company_id, rn),
                    )
            conn.commit()

    def test_alias_resolves_when_source_rn_absent(self):
        cases = [
            ("1325879", "Altec Worldwide", 751, "approved_alias"),
            ("1401940", "Williams Patent Crusher", 921, "approved_alias"),
            ("1326962", "The Massman Companies", 79, "approved_alias"),
            ("1326946", "Packaging Specialties Inc.", 1034, "approved_alias"),
            ("1350965", "PM Peterson Vehicle Safety Lighting", 483, "approved_alias"),
            ("1402335", "HIMOINSA Power Systems", 333, "approved_alias"),
            ("1326501", "Ronson Manufacturing Corp", 425, "approved_alias"),
        ]
        with get_connection() as conn:
            for rn, name, company_id, method in cases:
                match = resolve_history_company(
                    conn, client_id=2, record_no=rn, company_name=name
                )
                self.assertEqual(match.status, "matched", msg=(rn, match))
                self.assertEqual(match.method, method, msg=(rn, match))
                self.assertEqual(match.company_id, company_id, msg=(rn, match))
                # Alias must not rewrite stored Record No. on the company row.
                stored = conn.execute(
                    "SELECT external_record_no FROM companies WHERE id = ?",
                    (company_id,),
                ).fetchone()["external_record_no"]
                self.assertNotEqual(str(stored).strip(), rn)

    def test_direct_record_no_still_wins_over_alias(self):
        with get_connection() as conn:
            # Give Altec Worldwide's source RN to a real company; direct match wins.
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, created_at, last_updated_at
                ) VALUES ('1325879', 'Direct RN Wins Co', datetime('now'), datetime('now'))
                """
            )
            new_id = int(
                conn.execute(
                    "SELECT id FROM companies WHERE external_record_no = '1325879'"
                ).fetchone()["id"]
            )
            conn.commit()
            match = resolve_history_company(
                conn,
                client_id=2,
                record_no="1325879",
                company_name="Altec Worldwide",
            )
            self.assertEqual(match.status, "matched")
            self.assertEqual(match.method, "record_no")
            self.assertEqual(match.company_id, new_id)


if __name__ == "__main__":
    unittest.main()
