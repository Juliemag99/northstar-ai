"""Isolated tests for standard status catalog + authoritative existing-status option.

Run: python test_crm_import_authoritative_status.py
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
from crm_import_staging import (
    BatchNotReusable,
    ensure_crm_import_schema,
    save_crm_import_mapping,
)
from crm_import_status_notes import (
    BATCH_RESOLUTION_USE_IMPORTED,
    STANDARD_CRM_RELATIONSHIP_STATUSES,
    STATUS_CONFLICT,
    STATUS_PRESERVE,
    STATUS_USE_IMPORTED,
    ensure_client_standard_status_catalog,
    load_client_status_catalog,
    normalize_status_key,
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


def _second_client_id(conn, primary: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM clients WHERE id != ? ORDER BY id LIMIT 1",
        (int(primary),),
    ).fetchone()
    return int(row["id"]) if row else None


def _insert_batch(conn, client_id: int, headers: list[str], rows: list[dict]) -> int:
    cur = conn.execute(
        """
        INSERT INTO crm_import_batches (
            client_id, uploaded_by_user_id, uploaded_by_name, original_filename,
            file_type, sha256, status, headers_json, total_rows, source_row_count,
            created_at, updated_at, expires_at
        ) VALUES (?, 1, 'Admin', 'auth-status.csv', 'csv', 'sha-auth', 'previewed', ?, ?, ?,
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


STATUS_ROW_HEADERS = ["Company", "Record No.", "Status"]
STATUS_ROW_MAPPING = {
    "company_name": "Company",
    "external_record_no": "Record No.",
    "relationship_status": "Status",
}


class StandardCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)

    def test_canonical_standard_list_exact_order_and_spelling(self):
        self.assertEqual(
            STANDARD_CRM_RELATIONSHIP_STATUSES,
            (
                "New",
                "Contacted",
                "Qualified",
                "Future/Nurture",
                "Closed",
                "Appointment Set",
                "Good Fit-But no projects at this Time",
                "Disqualified-Not a good fit-No relevant work",
                "Disqualified-Production/Packed Outside US",
                "Disqualified-Purchasing Done at Parent/Elsewhere",
                "Actively Calling-Not getting through yet",
                "Company Not Called Yet",
                "Other",
                "Left Message",
                "Good fit under contract with competitor",
                "Send Information",
                "Hot Prospect",
                "Need Contact Name",
                "Need Contact Phone Number",
                "Competitor",
                "Current Customer",
                "Obtained New Contact Name/Number",
                "Dupe Record",
                "Do Not Call",
                "Do Not Email",
                "Appt Set Email Marketing",
            ),
        )
        keys = [normalize_status_key(s) for s in STANDARD_CRM_RELATIONSHIP_STATUSES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_load_catalog_includes_standards_without_persist(self):
        with get_connection() as conn:
            before = {
                normalize_status_key(r["status_label"])
                for r in conn.execute(
                    """
                    SELECT status_label FROM client_status_catalog
                    WHERE client_id = ?
                    """,
                    (self.client_id,),
                ).fetchall()
            }
            catalog = load_client_status_catalog(conn, self.client_id)
            for label in STANDARD_CRM_RELATIONSHIP_STATUSES:
                canonical, err = catalog.resolve(label)
                self.assertIsNone(err, label)
                self.assertEqual(canonical, label)
            after = {
                normalize_status_key(r["status_label"])
                for r in conn.execute(
                    """
                    SELECT status_label FROM client_status_catalog
                    WHERE client_id = ?
                    """,
                    (self.client_id,),
                ).fetchall()
            }
            self.assertEqual(before, after)

    def test_ensure_standards_idempotent_no_duplicates(self):
        with get_connection() as conn:
            first = ensure_client_standard_status_catalog(conn, self.client_id)
            second = ensure_client_standard_status_catalog(conn, self.client_id)
            conn.commit()
            self.assertEqual(second, [])
            rows = conn.execute(
                """
                SELECT status_label FROM client_status_catalog
                WHERE client_id = ?
                """,
                (self.client_id,),
            ).fetchall()
            keys = [normalize_status_key(r["status_label"]) for r in rows]
            self.assertEqual(len(keys), len(set(keys)))
            for label in STANDARD_CRM_RELATIONSHIP_STATUSES:
                self.assertIn(normalize_status_key(label), keys)
            self.assertTrue(isinstance(first, list))


class AuthoritativeStatusOptionTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)
            self.other_client_id = _second_client_id(conn, self.client_id)

    def _seed_company_rel(self, conn, *, record_no: str, name: str, status: str, notes: str = ""):
        company_id = int(
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, address, city, state, zip, website,
                    legacy_phone, type_of_industry, created_at, last_updated_at
                ) VALUES (?, ?, '', '', '', '', '', '', '', datetime('now'), datetime('now'))
                """,
                (record_no, name),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, assigned_user_id,
                priority, next_action, notes, is_hot, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, '', '', ?, 0, datetime('now'), datetime('now'))
            """,
            (self.client_id, company_id, record_no, status, notes),
        )
        return company_id

    def test_option_off_keeps_conflict_option_on_updates_and_fingerprint(self):
        with get_connection() as conn:
            self._seed_company_rel(
                conn,
                record_no="NS-AUTH-1",
                name="Auth Co",
                status="New",
                notes="Keep me",
            )
            batch_id = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Auth Co", "Record No.": "NS-AUTH-1", "Status": "Hot Prospect"}],
            )

        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping=STATUS_ROW_MAPPING,
        )

        with get_connection() as conn:
            off = plan_crm_import_batch(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                use_imported_status_for_existing=False,
            )
            on = plan_crm_import_batch(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                use_imported_status_for_existing=True,
            )

        self.assertEqual(PLANNER_VERSION, "crm-import-plan-v11")
        self.assertEqual(off.rows[0].status_action, STATUS_CONFLICT)
        self.assertEqual(off.counts["status_conflict"], 1)
        self.assertEqual(off.counts["update_existing_status"], 0)
        self.assertEqual(off.counts["needs_review_rows"], 1)
        self.assertFalse(off.use_imported_status_for_existing)

        self.assertEqual(on.rows[0].status_action, STATUS_USE_IMPORTED)
        self.assertEqual(on.rows[0].resolved_status, "Hot Prospect")
        self.assertEqual(on.rows[0].status_resolution_type, BATCH_RESOLUTION_USE_IMPORTED)
        self.assertEqual(on.rows[0].original_status_action, STATUS_CONFLICT)
        self.assertEqual(on.counts["status_conflict"], 0)
        self.assertEqual(on.counts["update_existing_status"], 1)
        self.assertEqual(on.counts["importable_rows"], 1)
        self.assertEqual(on.counts["needs_review_rows"], 0)
        self.assertTrue(on.use_imported_status_for_existing)
        self.assertNotEqual(off.plan_fingerprint, on.plan_fingerprint)

    def test_blank_imported_preserves_and_confirm_updates_selected_client_only(self):
        with get_connection() as conn:
            company_id = self._seed_company_rel(
                conn,
                record_no="NS-AUTH-2",
                name="Auth Update Co",
                status="New",
                notes="Old",
            )
            blank_batch = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Auth Update Co", "Record No.": "NS-AUTH-2", "Status": ""}],
            )
            update_batch = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Auth Update Co", "Record No.": "NS-AUTH-2", "Status": "Current Customer"}],
            )
            other_status_before = None
            if self.other_client_id is not None:
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status, assigned_user_id,
                        priority, next_action, notes, is_hot, created_at, updated_at
                    ) VALUES (?, ?, 'NS-AUTH-2-OTHER', 'Working', NULL, '', '', 'Other client',
                              0, datetime('now'), datetime('now'))
                    """,
                    (self.other_client_id, company_id),
                )
                other_status_before = "Working"
            conn.commit()

        save_crm_import_mapping(
            self.client_id,
            blank_batch,
            actor=_actor(),
            mapping=STATUS_ROW_MAPPING,
        )
        with get_connection() as conn:
            blank_plan = plan_crm_import_batch(
                conn,
                client_id=self.client_id,
                batch_id=blank_batch,
                use_imported_status_for_existing=True,
            )
        self.assertEqual(blank_plan.rows[0].status_action, STATUS_PRESERVE)
        self.assertEqual(blank_plan.counts["update_existing_status"], 0)

        save_crm_import_mapping(
            self.client_id,
            update_batch,
            actor=_actor(),
            mapping=STATUS_ROW_MAPPING,
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(
                conn,
                client_id=self.client_id,
                batch_id=update_batch,
                use_imported_status_for_existing=True,
            )
            fingerprint = plan.plan_fingerprint

        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=update_batch,
            plan_fingerprint=fingerprint,
            actor=_actor(),
            use_imported_status_for_existing=True,
        )
        self.assertEqual(result["imported_status_count"], 1)

        with get_connection() as conn:
            mine = conn.execute(
                """
                SELECT status, notes FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (self.client_id, company_id),
            ).fetchone()
            self.assertEqual(mine["status"], "Current Customer")
            self.assertEqual(mine["notes"], "Old")
            audit = conn.execute(
                """
                SELECT batch_id, source_row_number, relationship_id,
                       status_action, original_status_action, status_resolution_action,
                       previous_status, final_status
                FROM crm_import_results WHERE batch_id = ?
                """,
                (update_batch,),
            ).fetchone()
            self.assertEqual(audit["batch_id"], update_batch)
            self.assertEqual(audit["status_action"], STATUS_USE_IMPORTED)
            self.assertEqual(audit["original_status_action"], STATUS_CONFLICT)
            self.assertEqual(audit["status_resolution_action"], BATCH_RESOLUTION_USE_IMPORTED)
            self.assertEqual(audit["previous_status"], "New")
            self.assertEqual(audit["final_status"], "Current Customer")
            self.assertIsNotNone(audit["relationship_id"])
            if self.other_client_id is not None and other_status_before is not None:
                other = conn.execute(
                    """
                    SELECT status, notes FROM client_company_relationships
                    WHERE client_id = ? AND company_id = ?
                    """,
                    (self.other_client_id, company_id),
                ).fetchone()
                self.assertEqual(other["status"], other_status_before)
                self.assertEqual(other["notes"], "Other client")

    def test_confirm_rejects_fingerprint_when_option_mismatches(self):
        with get_connection() as conn:
            self._seed_company_rel(
                conn,
                record_no="NS-AUTH-3",
                name="Auth FP Co",
                status="New",
            )
            batch_id = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Auth FP Co", "Record No.": "NS-AUTH-3", "Status": "Future/Nurture"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping=STATUS_ROW_MAPPING,
        )
        with get_connection() as conn:
            on = plan_crm_import_batch(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                use_imported_status_for_existing=True,
            )

        with self.assertRaises(BatchNotReusable):
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint=on.plan_fingerprint,
                actor=_actor(),
                use_imported_status_for_existing=False,
            )


class PreviousStatusAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(crm_import_results)").fetchall()
            }
            self.assertIn("previous_status", cols)

    def _seed(self, conn, *, record_no: str, name: str, status: str):
        company_id = int(
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, address, city, state, zip, website,
                    legacy_phone, type_of_industry, created_at, last_updated_at
                ) VALUES (?, ?, '', '', '', '', '', '', '', datetime('now'), datetime('now'))
                """,
                (record_no, name),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, assigned_user_id,
                priority, next_action, notes, is_hot, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, '', '', '', 0, datetime('now'), datetime('now'))
            """,
            (self.client_id, company_id, record_no, status),
        )
        return company_id

    def _map_and_confirm(self, batch_id: int, *, use_imported: bool):
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping=STATUS_ROW_MAPPING,
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                use_imported_status_for_existing=use_imported,
            )
            self.assertEqual(plan.counts["needs_review_rows"], 0)
            fingerprint = plan.plan_fingerprint
            existing_status = plan.rows[0].existing_status
        confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fingerprint,
            actor=_actor(),
            use_imported_status_for_existing=use_imported,
        )
        with get_connection() as conn:
            audit = conn.execute(
                """
                SELECT batch_id, source_row_number, relationship_id, relationship_action,
                       status_action, status_resolution_action,
                       previous_status, final_status
                FROM crm_import_results WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
        return existing_status, audit

    def test_changed_status_records_previous_and_final(self):
        with get_connection() as conn:
            self._seed(conn, record_no="NS-PREV-1", name="Prev Change Co", status="New")
            batch_id = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Prev Change Co", "Record No.": "NS-PREV-1", "Status": "Hot Prospect"}],
            )
        planned_existing, audit = self._map_and_confirm(batch_id, use_imported=True)
        self.assertEqual(planned_existing, "New")
        self.assertEqual(audit["previous_status"], "New")
        self.assertEqual(audit["final_status"], "Hot Prospect")
        self.assertEqual(audit["status_action"], STATUS_USE_IMPORTED)
        self.assertEqual(audit["status_resolution_action"], BATCH_RESOLUTION_USE_IMPORTED)
        self.assertEqual(audit["relationship_action"], "relationship_already_exists")
        self.assertEqual(audit["batch_id"], batch_id)
        self.assertIsNotNone(audit["relationship_id"])
        self.assertIsNotNone(audit["source_row_number"])

    def test_unchanged_status_not_authoritative_change(self):
        with get_connection() as conn:
            self._seed(conn, record_no="NS-PREV-2", name="Prev Same Co", status="New")
            batch_id = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Prev Same Co", "Record No.": "NS-PREV-2", "Status": "New"}],
            )
        _, audit = self._map_and_confirm(batch_id, use_imported=True)
        self.assertEqual(audit["previous_status"], "New")
        self.assertEqual(audit["final_status"], "New")
        self.assertEqual(audit["status_action"], STATUS_PRESERVE)
        self.assertEqual(audit["status_resolution_action"], "")

    def test_blank_imported_preserves_without_authoritative_marker(self):
        with get_connection() as conn:
            self._seed(
                conn,
                record_no="NS-PREV-3",
                name="Prev Blank Co",
                status="Contacted",
            )
            batch_id = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Prev Blank Co", "Record No.": "NS-PREV-3", "Status": ""}],
            )
        _, audit = self._map_and_confirm(batch_id, use_imported=True)
        self.assertEqual(audit["previous_status"], "Contacted")
        self.assertEqual(audit["final_status"], "Contacted")
        self.assertEqual(audit["status_action"], STATUS_PRESERVE)
        self.assertEqual(audit["status_resolution_action"], "")
        with get_connection() as conn:
            status = conn.execute(
                """
                SELECT status FROM client_company_relationships
                WHERE client_id = ? AND external_record_no = 'NS-PREV-3'
                """,
                (self.client_id,),
            ).fetchone()["status"]
        self.assertEqual(status, "Contacted")

    def test_new_relationship_previous_status_null(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                STATUS_ROW_HEADERS,
                [{"Company": "Brand New Audit Co", "Record No.": "", "Status": "Qualified"}],
            )
        _, audit = self._map_and_confirm(batch_id, use_imported=False)
        self.assertIsNone(audit["previous_status"])
        self.assertEqual(audit["final_status"], "Qualified")
        self.assertEqual(audit["status_action"], STATUS_USE_IMPORTED)
        self.assertEqual(audit["relationship_action"], "create_client_relationship")
        self.assertEqual(audit["status_resolution_action"], "")


if __name__ == "__main__":
    unittest.main()
