"""Isolated tests for CRM import Excel/serial datetime normalization.

Run: python test_crm_import_datetime.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from pathlib import Path

import testdb

from crm_import_confirm import confirm_admin_crm_import_batch
from crm_import_datetime import INVALID_DATE, normalize_import_datetime
from crm_import_plan import plan_crm_import_batch
from crm_import_staging import BatchNotReusable, ensure_crm_import_schema, save_crm_import_mapping
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
        ) VALUES (?, 1, 'Admin', 't.csv', 'csv', 'sha-dt', 'previewed', ?, ?, ?,
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


class NormalizeImportDatetimeHelperTests(unittest.TestCase):
    def test_excel_serial_examples(self):
        self.assertEqual(normalize_import_datetime(46192.55625), "06/19/2026 1:21 PM")
        self.assertEqual(normalize_import_datetime(46199.35208), "06/26/2026 8:27 AM")
        self.assertEqual(normalize_import_datetime("46192.55625"), "06/19/2026 1:21 PM")

    def test_blank_strings_and_round_trip(self):
        self.assertEqual(normalize_import_datetime(""), "")
        self.assertEqual(normalize_import_datetime("  "), "")
        self.assertEqual(normalize_import_datetime(None), "")
        self.assertEqual(
            normalize_import_datetime("06/19/2026 1:21 PM"),
            "06/19/2026 1:21 PM",
        )
        self.assertEqual(
            normalize_import_datetime("7/28/2026 10:30"),
            "07/28/2026 10:30 AM",
        )
        self.assertEqual(
            normalize_import_datetime(datetime(2026, 6, 19, 13, 21, 29)),
            "06/19/2026 1:21 PM",
        )
        self.assertEqual(
            normalize_import_datetime(datetime(2026, 6, 19, 13, 21, 30)),
            "06/19/2026 1:22 PM",
        )
        self.assertIsNone(normalize_import_datetime("not-a-date"))
        self.assertIsNone(normalize_import_datetime("99/99/9999 1:00 PM"))


class ImportDatetimePlanConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)

    def test_new_company_stores_source_dates_not_audit_fields(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Entered", "Updated"],
                [
                    {
                        "Company": "Date Co DT",
                        "Entered": "46192.55625",
                        "Updated": "46199.35208",
                    }
                ],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={
                "company_name": "Company",
                "source_entered_at": "Entered",
                "source_updated_at": "Updated",
            },
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.counts["needs_review_rows"], 0)
            self.assertEqual(plan.rows[0].mapped.get("source_entered_at"), "06/19/2026 1:21 PM")
            self.assertEqual(plan.rows[0].mapped.get("source_updated_at"), "06/26/2026 8:27 AM")
            fp1 = plan.plan_fingerprint
            fp2 = plan_crm_import_batch(
                conn, client_id=self.client_id, batch_id=batch_id
            ).plan_fingerprint
            self.assertEqual(fp1, fp2)
            fingerprint = fp1
        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fingerprint,
            actor=_actor(),
        )
        self.assertEqual(result["created_company_count"], 1)
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT entered_at, source_updated_at, created_at, last_updated_at
                FROM companies WHERE company_name = 'Date Co DT'
                """
            ).fetchone()
            self.assertEqual(row["entered_at"], "06/19/2026 1:21 PM")
            self.assertEqual(row["source_updated_at"], "06/26/2026 8:27 AM")
            self.assertNotEqual(row["created_at"], row["entered_at"])
            self.assertNotEqual(row["last_updated_at"], row["source_updated_at"])

    def test_invalid_date_needs_review_and_changes_fingerprint(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Entered"],
                [{"Company": "Bad Date Co", "Entered": "46192.55625"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "source_entered_at": "Entered"},
        )
        with get_connection() as conn:
            good = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            good_fp = good.plan_fingerprint
            conn.execute(
                "UPDATE crm_import_rows SET raw_json = ? WHERE batch_id = ?",
                (json.dumps({"Company": "Bad Date Co", "Entered": "tomorrow-ish"}), batch_id),
            )
            conn.commit()
            bad = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertNotEqual(good_fp, bad.plan_fingerprint)
            self.assertEqual(bad.rows[0].validity, "invalid_mapping_data")
            self.assertEqual(bad.rows[0].validity_detail, INVALID_DATE)
            self.assertGreater(bad.counts["needs_review_rows"], 0)
            fingerprint = bad.plan_fingerprint
        with self.assertRaises(BatchNotReusable):
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint=fingerprint,
                actor=_actor(),
            )


if __name__ == "__main__":
    unittest.main()
