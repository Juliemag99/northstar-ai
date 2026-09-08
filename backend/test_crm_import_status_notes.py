"""Isolated tests for CRM import status/notes helpers and planner/confirm behavior.

Run: python test_crm_import_status_notes.py
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
from crm_import_staging import ensure_crm_import_schema, save_crm_import_mapping
from crm_import_status_notes import (
    NOTES_ALREADY_PRESENT,
    NOTES_APPEND,
    NOTES_NO_CHANGE,
    NOTES_SET,
    STATUS_CONFLICT,
    STATUS_INVALID,
    STATUS_PRESERVE,
    STATUS_USE_DEFAULT,
    STATUS_USE_IMPORTED,
    StatusCatalog,
    append_imported_notes,
    notes_block_already_present,
    plan_status_and_notes,
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
        ) VALUES (?, 1, 'Admin', 't.csv', 'csv', 'sha', 'previewed', ?, ?, ?,
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


class StatusNotesHelperTests(unittest.TestCase):
    def test_blank_status_defaults_to_new(self):
        catalog = StatusCatalog.from_labels(["New", "Active"])
        plan = plan_status_and_notes(
            relationship_is_new=True,
            existing_status="",
            existing_notes="",
            imported_status="",
            imported_notes="",
            catalog=catalog,
        )
        self.assertEqual(plan.status_action, STATUS_USE_DEFAULT)
        self.assertEqual(plan.resolved_status, "New")
        self.assertEqual(plan.notes_action, NOTES_NO_CHANGE)

    def test_case_insensitive_canonical_status(self):
        catalog = StatusCatalog.from_labels(["Active", "New"])
        plan = plan_status_and_notes(
            relationship_is_new=True,
            existing_status="",
            existing_notes="",
            imported_status="  ACTIVE ",
            imported_notes="Hello",
            catalog=catalog,
        )
        self.assertEqual(plan.status_action, STATUS_USE_IMPORTED)
        self.assertEqual(plan.resolved_status, "Active")
        self.assertEqual(plan.notes_action, NOTES_SET)
        self.assertEqual(plan.planned_notes, "Hello")

    def test_unknown_and_conflict(self):
        catalog = StatusCatalog.from_labels(["New", "Active"])
        unknown = plan_status_and_notes(
            relationship_is_new=True,
            existing_status="",
            existing_notes="",
            imported_status="Mystery",
            imported_notes="",
            catalog=catalog,
        )
        self.assertEqual(unknown.status_action, STATUS_INVALID)
        self.assertTrue(unknown.needs_review)

        conflict = plan_status_and_notes(
            relationship_is_new=False,
            existing_status="Active",
            existing_notes="Keep",
            imported_status="New",
            imported_notes="",
            catalog=catalog,
        )
        self.assertEqual(conflict.status_action, STATUS_CONFLICT)
        self.assertTrue(conflict.needs_review)
        self.assertEqual(conflict.notes_action, NOTES_NO_CHANGE)

    def test_same_status_preserved(self):
        catalog = StatusCatalog.from_labels(["Active"])
        plan = plan_status_and_notes(
            relationship_is_new=False,
            existing_status="Active",
            existing_notes="A",
            imported_status="active",
            imported_notes="",
            catalog=catalog,
        )
        self.assertEqual(plan.status_action, STATUS_PRESERVE)
        self.assertFalse(plan.needs_review)

    def test_notes_append_and_duplicate(self):
        catalog = StatusCatalog.from_labels(["New"])
        appended = plan_status_and_notes(
            relationship_is_new=False,
            existing_status="New",
            existing_notes="Line one",
            imported_status="",
            imported_notes="Line two",
            catalog=catalog,
        )
        self.assertEqual(appended.notes_action, NOTES_APPEND)
        self.assertEqual(appended.planned_notes, "Line one\n\nLine two")

        dup = plan_status_and_notes(
            relationship_is_new=False,
            existing_status="New",
            existing_notes="Intro\r\nLine two\nEnd",
            imported_status="",
            imported_notes="Line two",
            catalog=catalog,
        )
        self.assertEqual(dup.notes_action, NOTES_ALREADY_PRESENT)
        self.assertTrue(notes_block_already_present("a\r\nb", "a\nb"))
        self.assertEqual(append_imported_notes("A", "B"), "A\n\nB")


class StatusNotesPlanConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)

    def test_new_relationship_status_notes_and_fingerprint(self):
        with get_connection() as conn:
            # Ensure Active is a known client status.
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-SEED-SN', 'Seed Co SN', '', '', '', '', '', '', '',
                              datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, 'NS-SEED-SN', 'Active', NULL, '', '', '', 0,
                          datetime('now'), datetime('now'))
                """,
                (self.client_id, company_id),
            )
            conn.commit()
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status", "Notes"],
                [{"Company": "Acme SN", "Status": "active", "Notes": "First note"}],
            )

        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={
                "company_name": "Company",
                "relationship_status": "Status",
                "relationship_notes": "Notes",
            },
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(PLANNER_VERSION, "crm-import-plan-v5")
            row = plan.rows[0]
            self.assertEqual(row.status_action, STATUS_USE_IMPORTED)
            self.assertEqual(row.resolved_status, "Active")
            self.assertEqual(row.notes_action, NOTES_SET)
            self.assertEqual(plan.counts["needs_review_rows"], 0)
            fp1 = plan.plan_fingerprint
            conn.execute(
                "UPDATE crm_import_rows SET raw_json = ? WHERE batch_id = ?",
                (
                    json.dumps({"Company": "Acme SN", "Status": "active", "Notes": "Changed"}),
                    batch_id,
                ),
            )
            conn.commit()
            plan2 = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertNotEqual(fp1, plan2.plan_fingerprint)
            fingerprint = plan2.plan_fingerprint

        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fingerprint,
            actor=_actor(),
        )
        self.assertEqual(result["imported_status_count"], 1)
        self.assertEqual(result["notes_set_count"], 1)
        with get_connection() as conn:
            rel = conn.execute(
                """
                SELECT status, notes, assigned_user_id FROM client_company_relationships
                WHERE client_id = ? AND company_id = (
                    SELECT id FROM companies WHERE company_name = 'Acme SN'
                )
                """,
                (self.client_id,),
            ).fetchone()
            self.assertEqual(rel["status"], "Active")
            self.assertEqual(rel["notes"], "Changed")
            self.assertIsNone(rel["assigned_user_id"])
            audit = conn.execute(
                "SELECT status_action, notes_action FROM crm_import_results WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            self.assertEqual(audit["status_action"], STATUS_USE_IMPORTED)
            self.assertEqual(audit["notes_action"], NOTES_SET)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(crm_import_results)")}
            self.assertNotIn("notes", cols)
            self.assertNotIn("note_text", cols)

    def test_existing_conflict_blocks_confirm_and_append_updates(self):
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-EX-SN', 'Existing Co SN', '', '', '', '', '', '', '',
                              datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, 'NS-EX-SN', 'Active', NULL, '', '', 'Old note', 0,
                          datetime('now'), '2020-01-01 00:00:00')
                """,
                (self.client_id, company_id),
            )
            other_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-EX2-SN', 'Other SN', '', '', '', '', '', '', '',
                              datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, 'NS-EX2-SN', 'New', NULL, '', '', '', 0,
                          datetime('now'), datetime('now'))
                """,
                (self.client_id, other_id),
            )
            conn.commit()
            conflict_batch = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status"],
                [{"Company": "Existing Co SN", "Status": "New"}],
            )

        save_crm_import_mapping(
            self.client_id,
            conflict_batch,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_status": "Status"},
        )
        with get_connection() as conn:
            conflict_plan = plan_crm_import_batch(
                conn, client_id=self.client_id, batch_id=conflict_batch
            )
            self.assertEqual(conflict_plan.rows[0].status_action, STATUS_CONFLICT)
            self.assertGreater(conflict_plan.counts["needs_review_rows"], 0)
            fingerprint = conflict_plan.plan_fingerprint
        with self.assertRaises(Exception):
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=conflict_batch,
                plan_fingerprint=fingerprint,
                actor=_actor(),
            )

        with get_connection() as conn:
            ok_batch = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Notes"],
                [{"Company": "Existing Co SN", "Notes": "Fresh note"}],
            )
        save_crm_import_mapping(
            self.client_id,
            ok_batch,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_notes": "Notes"},
        )
        with get_connection() as conn:
            ok_plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=ok_batch)
            self.assertEqual(ok_plan.rows[0].notes_action, NOTES_APPEND)
            before = conn.execute(
                """
                SELECT notes, updated_at FROM client_company_relationships
                WHERE company_id = (SELECT id FROM companies WHERE company_name = 'Existing Co SN')
                """
            ).fetchone()
            fingerprint = ok_plan.plan_fingerprint
            before_notes = before["notes"]
            before_updated = before["updated_at"]
        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=ok_batch,
            plan_fingerprint=fingerprint,
            actor=_actor(),
        )
        self.assertEqual(result["notes_appended_count"], 1)
        with get_connection() as conn:
            after = conn.execute(
                """
                SELECT notes, updated_at, status FROM client_company_relationships
                WHERE company_id = (SELECT id FROM companies WHERE company_name = 'Existing Co SN')
                """
            ).fetchone()
            self.assertEqual(after["status"], "Active")
            self.assertEqual(after["notes"], "Old note\n\nFresh note")
            self.assertNotEqual(before_updated, after["updated_at"])
            self.assertEqual(before_notes, "Old note")

    def test_duplicate_notes_skip_and_rollback(self):
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-DUP-SN', 'Dup Co SN', '', '', '', '', '', '', '',
                              datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, 'NS-DUP-SN', 'New', NULL, '', '', 'Keep me', 0,
                          datetime('now'), '2020-01-01 00:00:00')
                """,
                (self.client_id, company_id),
            )
            conn.commit()
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Notes"],
                [{"Company": "Dup Co SN", "Notes": "Keep me"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_notes": "Notes"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].notes_action, NOTES_ALREADY_PRESENT)
            before = conn.execute(
                """
                SELECT notes, updated_at FROM client_company_relationships
                WHERE company_id = (SELECT id FROM companies WHERE company_name = 'Dup Co SN')
                """
            ).fetchone()
            fingerprint = plan.plan_fingerprint
            before_updated = before["updated_at"]
        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fingerprint,
            actor=_actor(),
        )
        self.assertEqual(result["notes_duplicate_count"], 1)
        with get_connection() as conn:
            after = conn.execute(
                """
                SELECT notes, updated_at FROM client_company_relationships
                WHERE company_id = (SELECT id FROM companies WHERE company_name = 'Dup Co SN')
                """
            ).fetchone()
            self.assertEqual(after["notes"], "Keep me")
            self.assertEqual(after["updated_at"], before_updated)

            batch2 = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Notes"],
                [{"Company": "Dup Co SN", "Notes": "Extra"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch2,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_notes": "Notes"},
        )
        with get_connection() as conn:
            plan2 = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch2)
            fingerprint2 = plan2.plan_fingerprint
        with self.assertRaises(RuntimeError):
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=batch2,
                plan_fingerprint=fingerprint2,
                actor=_actor(),
                fail_after="before_commit",
            )
        with get_connection() as conn:
            restored = conn.execute(
                """
                SELECT notes FROM client_company_relationships
                WHERE company_id = (SELECT id FROM companies WHERE company_name = 'Dup Co SN')
                """
            ).fetchone()
            self.assertEqual(restored["notes"], "Keep me")

    def test_long_notes_plan_confirm_and_fingerprint_beyond_preview(self):
        from crm_import_plan import CELL_PREVIEW_CHARS, dry_run_crm_import
        from crm_import_staging import PREVIEW_DISPLAY_CHARS

        prefix = "P" * (PREVIEW_DISPLAY_CHARS + 50)
        body_a = prefix + "TAIL-A-" + ("a" * 1800)
        body_b = prefix + "TAIL-B-" + ("b" * 1800)
        self.assertGreater(len(body_a), 2000)
        self.assertNotEqual(body_a, body_b)
        self.assertEqual(body_a[:PREVIEW_DISPLAY_CHARS], body_b[:PREVIEW_DISPLAY_CHARS])

        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Notes"],
                [{"Company": "Long Notes Co", "Notes": body_a}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_notes": "Notes"},
        )

        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].notes_action, NOTES_SET)
            self.assertEqual(plan.rows[0].planned_notes, body_a)
            fp1 = plan.plan_fingerprint

            dry = dry_run_crm_import(self.client_id, batch_id, offset=0, limit=25)
            mapped_notes = dry.rows[0].mapped.get("relationship_notes") or ""
            self.assertLessEqual(len(mapped_notes), CELL_PREVIEW_CHARS)
            self.assertNotIn("TAIL-A-", mapped_notes)
            self.assertNotEqual(mapped_notes, body_a)

            conn.execute(
                "UPDATE crm_import_rows SET raw_json = ? WHERE batch_id = ?",
                (json.dumps({"Company": "Long Notes Co", "Notes": body_b}), batch_id),
            )
            conn.commit()
            plan2 = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertNotEqual(fp1, plan2.plan_fingerprint)
            self.assertEqual(plan2.rows[0].planned_notes, body_b)
            fingerprint = plan2.plan_fingerprint

        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fingerprint,
            actor=_actor(),
        )
        self.assertEqual(result["notes_set_count"], 1)
        with get_connection() as conn:
            notes = conn.execute(
                """
                SELECT notes FROM client_company_relationships
                WHERE client_id = ? AND company_id = (
                    SELECT id FROM companies WHERE company_name = 'Long Notes Co'
                )
                """,
                (self.client_id,),
            ).fetchone()["notes"]
            self.assertEqual(notes, body_b)
            self.assertIn("TAIL-B-", notes)


if __name__ == "__main__":
    unittest.main()
