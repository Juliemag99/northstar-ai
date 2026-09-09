"""Isolated tests for CRM import per-row status resolution.

Run: python test_crm_import_status_resolution.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import testdb
from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from crm_import_confirm import confirm_admin_crm_import_batch
from crm_import_plan import PLANNER_VERSION, plan_crm_import_batch
from crm_import_staging import (
    BatchNotReusable,
    ensure_crm_import_schema,
    save_crm_import_mapping,
)
from crm_import_status_notes import (
    STATUS_CONFLICT,
    STATUS_INVALID,
    STATUS_PRESERVE,
    STATUS_USE_IMPORTED,
)
from crm_import_status_resolution import (
    RESOLUTION_KEEP_EXISTING,
    RESOLUTION_REPLACE,
    save_crm_import_status_resolution,
)
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from models import NorthStarUser


BROWN_STATUSES = [
    "Contacted",
    "Disqualified-Not a good fit-No relevant work",
    "Disqualified-Production/Packed Outside US",
    "Good Fit-But no projects at this Time",
    "Left Message",
    "New",
    "Send Information",
]


def _actor(user_id: int = 1, *, admin: bool = True, active: bool = True) -> NorthStarUser:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM users WHERE is_administrator = 1 ORDER BY id LIMIT 1"
            ).fetchone()
    return NorthStarUser(
        id=int(row["id"]),
        email=str(row["email"] or ""),
        full_name=str(row["full_name"] or "Admin"),
        is_administrator=admin,
        is_internal_northstar=True,
        active=active,
        created_at=str(row["created_at"] or ""),
    )


def _client_id(conn) -> int:
    row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise AssertionError("Isolated testdb needs a client.")
    return int(row["id"])


def _seed_brown_statuses(conn, client_id: int) -> None:
    import secrets

    token = secrets.token_hex(4)
    for i, status in enumerate(BROWN_STATUSES, start=1):
        record = f"NS-ST-{token}-{i}"
        company_id = int(
            conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, address, city, state, zip, website,
                    legacy_phone, type_of_industry, created_at, last_updated_at
                ) VALUES (?, ?, '', '', '', '', '', '', '', datetime('now'), datetime('now'))
                """,
                (record, f"Status Seed {token} {i}"),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, assigned_user_id,
                priority, next_action, notes, is_hot, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, '', '', '', 0, datetime('now'), datetime('now'))
            """,
            (client_id, company_id, record, status),
        )
    conn.commit()


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


class StatusResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = _client_id(conn)
            _seed_brown_statuses(conn, self.client_id)

    def test_invalid_status_catalog_replace_and_fingerprint(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status"],
                [{"Company": "Resolve Co", "Status": "Disqualified"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_status": "Status"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(PLANNER_VERSION, "crm-import-plan-v7")
            self.assertTrue(set(BROWN_STATUSES).issubset(set(plan.status_catalog)))
            for label in BROWN_STATUSES:
                self.assertIn(label, plan.status_catalog)
            row = plan.rows[0]
            self.assertEqual(row.status_action, STATUS_INVALID)
            self.assertTrue(row.status_action)
            fp1 = plan.plan_fingerprint
            staged_id = row.row_id

        saved = save_crm_import_status_resolution(
            self.client_id,
            batch_id,
            staged_id,
            actor=_actor(),
            resolution_type=RESOLUTION_REPLACE,
            resolved_status="Disqualified-Not a good fit-No relevant work",
        )
        self.assertFalse(saved["cleared"])
        self.assertEqual(
            saved["resolution"]["resolved_status"],
            "Disqualified-Not a good fit-No relevant work",
        )

        with get_connection() as conn:
            plan2 = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertNotEqual(fp1, plan2.plan_fingerprint)
            row2 = plan2.rows[0]
            self.assertEqual(row2.status_action, STATUS_USE_IMPORTED)
            self.assertEqual(
                row2.resolved_status, "Disqualified-Not a good fit-No relevant work"
            )
            self.assertEqual(row2.original_status_action, STATUS_INVALID)
            self.assertEqual(row2.status_resolution_type, RESOLUTION_REPLACE)
            self.assertEqual(plan2.counts["needs_review_rows"], 0)
            self.assertEqual(plan2.counts["importable_rows"], 1)
            fingerprint = plan2.plan_fingerprint

        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=fingerprint,
            actor=_actor(),
        )
        self.assertEqual(result["imported_status_count"], 1)
        with get_connection() as conn:
            status = conn.execute(
                """
                SELECT status FROM client_company_relationships
                WHERE client_id = ? AND company_id = (
                    SELECT id FROM companies WHERE company_name = 'Resolve Co'
                )
                """,
                (self.client_id,),
            ).fetchone()["status"]
            self.assertEqual(status, "Disqualified-Not a good fit-No relevant work")
            audit = conn.execute(
                """
                SELECT status_action, original_status_action, status_resolution_action, final_status
                FROM crm_import_results WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            self.assertEqual(audit["status_action"], STATUS_USE_IMPORTED)
            self.assertEqual(audit["original_status_action"], STATUS_INVALID)
            self.assertEqual(audit["status_resolution_action"], RESOLUTION_REPLACE)
            self.assertEqual(
                audit["final_status"], "Disqualified-Not a good fit-No relevant work"
            )

    def test_conflict_keep_and_replace(self):
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-CONF', 'Conflict Co', '', '', '', '', '', '', '',
                              datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, 'NS-CONF', 'New', NULL, '', '', '', 0,
                          datetime('now'), datetime('now'))
                """,
                (self.client_id, company_id),
            )
            keep_batch = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status"],
                [{"Company": "Conflict Co", "Status": "Contacted"}],
            )
            replace_batch = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status"],
                [{"Company": "Conflict Co", "Status": "Contacted"}],
            )

        for batch_id in (keep_batch, replace_batch):
            save_crm_import_mapping(
                self.client_id,
                batch_id,
                actor=_actor(),
                mapping={"company_name": "Company", "relationship_status": "Status"},
            )

        with get_connection() as conn:
            keep_plan = plan_crm_import_batch(
                conn, client_id=self.client_id, batch_id=keep_batch
            )
            self.assertEqual(keep_plan.rows[0].status_action, STATUS_CONFLICT)
            keep_row = keep_plan.rows[0].row_id

        save_crm_import_status_resolution(
            self.client_id,
            keep_batch,
            keep_row,
            actor=_actor(),
            resolution_type=RESOLUTION_KEEP_EXISTING,
        )
        with get_connection() as conn:
            keep_plan2 = plan_crm_import_batch(
                conn, client_id=self.client_id, batch_id=keep_batch
            )
            self.assertEqual(keep_plan2.rows[0].status_action, STATUS_PRESERVE)
            self.assertEqual(keep_plan2.rows[0].resolved_status, "New")
            self.assertEqual(keep_plan2.counts["needs_review_rows"], 0)
            keep_fp = keep_plan2.plan_fingerprint
        confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=keep_batch,
            plan_fingerprint=keep_fp,
            actor=_actor(),
        )
        with get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            ).fetchone()["status"]
            self.assertEqual(status, "New")

        with get_connection() as conn:
            replace_plan = plan_crm_import_batch(
                conn, client_id=self.client_id, batch_id=replace_batch
            )
            # After keep confirm, status is still New so Contacted still conflicts.
            self.assertEqual(replace_plan.rows[0].status_action, STATUS_CONFLICT)
            replace_row = replace_plan.rows[0].row_id
        save_crm_import_status_resolution(
            self.client_id,
            replace_batch,
            replace_row,
            actor=_actor(),
            resolution_type=RESOLUTION_REPLACE,
            resolved_status="Left Message",
        )
        with get_connection() as conn:
            replace_plan2 = plan_crm_import_batch(
                conn, client_id=self.client_id, batch_id=replace_batch
            )
            self.assertEqual(replace_plan2.rows[0].status_action, STATUS_USE_IMPORTED)
            self.assertEqual(replace_plan2.rows[0].resolved_status, "Left Message")
            replace_fp = replace_plan2.plan_fingerprint
        confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=replace_batch,
            plan_fingerprint=replace_fp,
            actor=_actor(),
        )
        with get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            ).fetchone()["status"]
            self.assertEqual(status, "Left Message")
            audit = conn.execute(
                """
                SELECT original_status_action, status_resolution_action, final_status
                FROM crm_import_results WHERE batch_id = ?
                """,
                (replace_batch,),
            ).fetchone()
            self.assertEqual(audit["original_status_action"], STATUS_CONFLICT)
            self.assertEqual(audit["status_resolution_action"], RESOLUTION_REPLACE)
            self.assertEqual(audit["final_status"], "Left Message")

    def test_mapping_change_clears_resolutions_and_blocks_partial_confirm(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status", "Alt"],
                [
                    {"Company": "A1", "Status": "Disqualified", "Alt": "New"},
                    {"Company": "A2", "Status": "Nope", "Alt": "Contacted"},
                ],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_status": "Status"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.counts["needs_review_rows"], 2)
            row_ids = [r.row_id for r in plan.rows]
        save_crm_import_status_resolution(
            self.client_id,
            batch_id,
            row_ids[0],
            actor=_actor(),
            resolution_type=RESOLUTION_REPLACE,
            resolved_status="New",
        )
        with get_connection() as conn:
            plan2 = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan2.counts["needs_review_rows"], 1)
            with self.assertRaises(Exception):
                confirm_admin_crm_import_batch(
                    client_id=self.client_id,
                    batch_id=batch_id,
                    plan_fingerprint=plan2.plan_fingerprint,
                    actor=_actor(),
                )
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM crm_import_row_resolutions WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()["n"]
            self.assertEqual(int(n), 1)

        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_status": "Alt"},
        )
        with get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM crm_import_row_resolutions WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()["n"]
            self.assertEqual(int(n), 0)

    def test_terminal_batches_reject_resolution(self):
        with get_connection() as conn:
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status"],
                [{"Company": "Term Co", "Status": "Disqualified"}],
            )
            row_id = int(
                conn.execute(
                    "SELECT id FROM crm_import_rows WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()["id"]
            )
            save_crm_import_mapping(
                self.client_id,
                batch_id,
                actor=_actor(),
                mapping={"company_name": "Company", "relationship_status": "Status"},
            )
            for status in ("cancelled", "imported", "expired"):
                conn.execute(
                    "UPDATE crm_import_batches SET status = ? WHERE id = ?",
                    (status, batch_id),
                )
                conn.commit()
                with self.assertRaises(BatchNotReusable):
                    save_crm_import_status_resolution(
                        self.client_id,
                        batch_id,
                        row_id,
                        actor=_actor(),
                        resolution_type=RESOLUTION_REPLACE,
                        resolved_status="New",
                    )

    def test_rollback_on_injected_failure_after_status_update(self):
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, type_of_industry, created_at, last_updated_at
                    ) VALUES ('NS-ROLL', 'Rollback Co', '', '', '', '', '', '', '',
                              datetime('now'), datetime('now'))
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at
                ) VALUES (?, ?, 'NS-ROLL', 'New', NULL, '', '', 'Keep', 0,
                          datetime('now'), '2020-01-01 00:00:00')
                """,
                (self.client_id, company_id),
            )
            batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status"],
                [{"Company": "Rollback Co", "Status": "Contacted"}],
            )
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=_actor(),
            mapping={"company_name": "Company", "relationship_status": "Status"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            row_id = plan.rows[0].row_id
        save_crm_import_status_resolution(
            self.client_id,
            batch_id,
            row_id,
            actor=_actor(),
            resolution_type=RESOLUTION_REPLACE,
            resolved_status="Contacted",
        )
        with get_connection() as conn:
            fp = plan_crm_import_batch(
                conn, client_id=self.client_id, batch_id=batch_id
            ).plan_fingerprint
        with self.assertRaises(RuntimeError):
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint=fp,
                actor=_actor(),
                fail_after="during_result_audit",
            )
        with get_connection() as conn:
            rel = conn.execute(
                "SELECT status, notes FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            ).fetchone()
            self.assertEqual(rel["status"], "New")
            self.assertEqual(rel["notes"], "Keep")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM crm_import_results WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()[0],
                0,
            )


class StatusResolutionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            migrate_schema(conn)
            self.client_id = _client_id(conn)
            _seed_brown_statuses(conn, self.client_id)
            admin = conn.execute(
                "SELECT id FROM users WHERE is_administrator = 1 ORDER BY id LIMIT 1"
            ).fetchone()
            self.admin_id = int(admin["id"])
            pwd = "ResolveTestPass!12"
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(pwd), self.admin_id),
            )
            email = conn.execute(
                "SELECT email FROM users WHERE id = ?", (self.admin_id,)
            ).fetchone()["email"]
            self.admin_email = str(email)
            self.admin_password = pwd
            # Ensure assignment
            exists = conn.execute(
                """
                SELECT 1 FROM user_client_assignments
                WHERE user_id = ? AND client_id = ?
                """,
                (self.admin_id, self.client_id),
            ).fetchone()
            if exists is None:
                conn.execute(
                    """
                    INSERT INTO user_client_assignments (user_id, client_id, role, active, assigned_at)
                    VALUES (?, ?, 'admin', 1, datetime('now'))
                    """,
                    (self.admin_id, self.client_id),
                )
            non = conn.execute(
                "SELECT id FROM users WHERE is_administrator = 0 ORDER BY id LIMIT 1"
            ).fetchone()
            if non is None:
                cur = conn.execute(
                    """
                    INSERT INTO users (email, full_name, password_hash, is_administrator, active, created_at)
                    VALUES ('staff@example.test', 'Staff', ?, 0, 1, datetime('now'))
                    """,
                    (hash_password("StaffTestPass!12"),),
                )
                self.staff_id = int(cur.lastrowid)
                self.staff_email = "staff@example.test"
                self.staff_password = "StaffTestPass!12"
            else:
                self.staff_id = int(non["id"])
                conn.execute(
                    "UPDATE users SET password_hash = ?, active = 1 WHERE id = ?",
                    (hash_password("StaffTestPass!12"), self.staff_id),
                )
                self.staff_email = conn.execute(
                    "SELECT email FROM users WHERE id = ?", (self.staff_id,)
                ).fetchone()["email"]
                self.staff_password = "StaffTestPass!12"
            conn.commit()
            self.batch_id = _insert_batch(
                conn,
                self.client_id,
                ["Company", "Status"],
                [{"Company": "Http Co", "Status": "Disqualified"}],
            )
        save_crm_import_mapping(
            self.client_id,
            self.batch_id,
            actor=_actor(self.admin_id),
            mapping={"company_name": "Company", "relationship_status": "Status"},
        )
        with get_connection() as conn:
            self.row_id = int(
                conn.execute(
                    "SELECT id FROM crm_import_rows WHERE batch_id = ?",
                    (self.batch_id,),
                ).fetchone()["id"]
            )
        os.environ["NORTHSTAR_AUTH_ENFORCE"] = "1"
        from main import app

        self.http = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)

    def _login(self, email: str, password: str) -> str:
        resp = self.http.post(
            "/api/auth/login",
            json={"email": email, "password": password},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        csrf = resp.json().get("csrf_token") or resp.cookies.get("northstar_csrf")
        self.assertTrue(csrf)
        return str(csrf)

    def test_auth_csrf_and_validation(self):
        url = (
            f"/api/clients/{self.client_id}/admin/imports/{self.batch_id}"
            f"/rows/{self.row_id}/status-resolution"
        )
        body = {
            "resolution_type": RESOLUTION_REPLACE,
            "resolved_status": "New",
        }
        # No auth
        bare = TestClient(self.http.app)
        denied = bare.put(url, json=body)
        self.assertIn(denied.status_code, {401, 403})

        staff_csrf = self._login(self.staff_email, self.staff_password)
        staff = self.http.put(
            url, json=body, headers={CSRF_HEADER: staff_csrf}
        )
        self.assertEqual(staff.status_code, 403)

        csrf = self._login(self.admin_email, self.admin_password)
        no_csrf = self.http.put(url, json=body)
        self.assertEqual(no_csrf.status_code, 403)

        wrong_client = self.http.put(
            f"/api/clients/999999/admin/imports/{self.batch_id}/rows/{self.row_id}/status-resolution",
            json=body,
            headers={CSRF_HEADER: csrf},
        )
        self.assertIn(wrong_client.status_code, {403, 404})

        bad_status = self.http.put(
            url,
            json={"resolution_type": RESOLUTION_REPLACE, "resolved_status": "NotARealStatus"},
            headers={CSRF_HEADER: csrf},
        )
        self.assertEqual(bad_status.status_code, 400)

        bad_row = self.http.put(
            f"/api/clients/{self.client_id}/admin/imports/{self.batch_id}/rows/999999/status-resolution",
            json=body,
            headers={CSRF_HEADER: csrf},
        )
        self.assertEqual(bad_row.status_code, 404)

        ok = self.http.put(url, json=body, headers={CSRF_HEADER: csrf})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["resolution"]["resolved_status"], "New")


if __name__ == "__main__":
    unittest.main()
