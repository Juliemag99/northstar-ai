"""Regression: Companies pagination/search + post-import search_fts.

Run: python test_prospects_visibility.py
Uses isolated testdb copies only.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import testdb

from client_workspace_data import list_prospects_page
from crm_import_confirm import confirm_admin_crm_import_batch
from crm_import_plan import plan_crm_import_batch
from crm_import_staging import ensure_crm_import_schema, save_crm_import_mapping
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from models import NorthStarUser
from search_data import search


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


def _brown_client_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id FROM clients
        WHERE id = 2
           OR lower(trim(code)) = 'brown'
           OR name LIKE 'Brown%'
        ORDER BY CASE WHEN id = 2 THEN 0 ELSE 1 END, id
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise AssertionError("Isolated testdb needs a client.")
    return int(row["id"])


class ProspectsVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _brown_client_id(conn)

    def test_valmont_reachable_beyond_first_100_and_by_search(self):
        actor = _actor()
        with get_connection() as conn:
            # Prefer live imported Valmont (company_id 541) when present on the copy.
            valmont = conn.execute(
                """
                SELECT co.id
                FROM companies co
                JOIN client_company_relationships ccr ON ccr.company_id = co.id
                WHERE ccr.client_id = ?
                  AND (
                    co.id = 541
                    OR lower(trim(co.company_name)) = 'valmont'
                  )
                ORDER BY CASE WHEN co.id = 541 THEN 0 ELSE 1 END, co.id
                LIMIT 1
                """,
                (self.client_id,),
            ).fetchone()
            if valmont is None:
                valmont_id = int(
                    conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, address, city, state, zip, website,
                            legacy_phone, type_of_industry, created_at, last_updated_at
                        ) VALUES (
                            'NS-VALMONT-VIS', 'Valmont', '', '', '', '', '', '', '',
                            datetime('now'), datetime('now')
                        )
                        """
                    ).lastrowid
                )
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status, assigned_user_id,
                        priority, next_action, notes, is_hot, created_at, updated_at
                    ) VALUES (?, ?, 'NS-VALMONT-VIS', 'Left Message', NULL, '', '', '', 0,
                              datetime('now'), datetime('now'))
                    """,
                    (self.client_id, valmont_id),
                )
            else:
                valmont_id = int(valmont["id"])

            # Force Valmont outside the initial 100 alphabetical rows.
            before = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM client_company_relationships ccr
                    JOIN companies co ON co.id = ccr.company_id
                    WHERE ccr.client_id = ?
                      AND co.company_name COLLATE NOCASE < 'Valmont'
                    """,
                    (self.client_id,),
                ).fetchone()["n"]
            )
            for i in range(max(0, 100 - before)):
                name = f"AAA Visibility Pad {i:04d}"
                cid = int(
                    conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, address, city, state, zip, website,
                            legacy_phone, type_of_industry, created_at, last_updated_at
                        ) VALUES (?, ?, '', '', '', '', '', '', '', datetime('now'), datetime('now'))
                        """,
                        (f"NS-VIS-PAD-{i}", name),
                    ).lastrowid
                )
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status, assigned_user_id,
                        priority, next_action, notes, is_hot, created_at, updated_at
                    ) VALUES (?, ?, ?, 'New', NULL, '', '', '', 0, datetime('now'), datetime('now'))
                    """,
                    (self.client_id, cid, f"NS-VIS-PAD-{i}"),
                )
            conn.commit()

        page0 = list_prospects_page(
            client_id=self.client_id, user_id=actor.id, limit=100, offset=0
        )
        self.assertEqual(page0["limit"], 100)
        self.assertGreaterEqual(page0["client_total"], 101)
        self.assertEqual(len(page0["prospects"]), 100)
        page0_ids = {p.id for p in page0["prospects"]}
        self.assertNotIn(
            valmont_id,
            page0_ids,
            "Valmont must fall outside the first 100 alphabetical rows for this regression.",
        )

        # Server search must find Valmont even though it is outside page 0.
        found = list_prospects_page(
            client_id=self.client_id,
            user_id=actor.id,
            q="Valmont",
            limit=50,
            offset=0,
        )
        self.assertGreaterEqual(found["total"], 1)
        self.assertTrue(
            any(p.id == valmont_id and p.company == "Valmont" for p in found["prospects"])
        )

        later = list_prospects_page(
            client_id=self.client_id, user_id=actor.id, limit=100, offset=100
        )
        later_ids = {p.id for p in later["prospects"]}
        self.assertIn(valmont_id, later_ids)

    def test_confirm_indexes_created_company_in_search_fts(self):
        actor = _actor()
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            # Seed a known status label for this client.
            seed_co = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-SEED-FTS', 'Seed Status Co', '', '', '', '', '', '', '',
                              datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, 'NS-SEED-FTS', 'New', NULL, '', '', '', 0,
                          datetime('now'), datetime('now'))
                """,
                (self.client_id, seed_co),
            )
            cur = conn.execute(
                """
                INSERT INTO crm_import_batches (
                    client_id, uploaded_by_user_id, uploaded_by_name, original_filename,
                    file_type, sha256, status, headers_json, total_rows, source_row_count,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, 'Admin', 'fts.csv', 'csv', 'sha-fts', 'previewed', ?, 1, 1,
                          datetime('now'), datetime('now'), '2099-01-01T00:00:00Z')
                """,
                (self.client_id, actor.id, json.dumps(["Company", "Status"])),
            )
            batch_id = int(cur.lastrowid)
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
                    json.dumps({"Company": "FTS Indexed Co", "Status": "New"}),
                ),
            )
            conn.commit()

        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=actor,
            mapping={"company_name": "Company", "relationship_status": "Status"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            fp = plan.plan_fingerprint
        confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fp,
            actor=actor,
        )
        with get_connection() as conn:
            company = conn.execute(
                "SELECT id FROM companies WHERE company_name = 'FTS Indexed Co'"
            ).fetchone()
            self.assertIsNotNone(company)
            company_id = int(company["id"])
            docs = conn.execute(
                """
                SELECT doc_type, company_name FROM search_fts
                WHERE client_id = ? AND company_id = ? AND doc_type = 'company'
                """,
                (self.client_id, company_id),
            ).fetchall()
            self.assertTrue(docs, "confirm must index the created company into search_fts")

        result = search("FTS Indexed", user_id=actor.id, client_id=self.client_id)
        names = [c.company_name for c in (result.companies or [])]
        self.assertIn("FTS Indexed Co", names)


if __name__ == "__main__":
    unittest.main()
