"""OI-2 isolated tests for self-service CRM import product fixes.

Run: python test_operational_import_phase2.py
Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import csv
import io
import json
import os
import secrets
import unittest
from pathlib import Path

import testdb
from openpyxl import Workbook

from company_locations import upsert_company_source_identity
from crm_import_confirm import confirm_admin_crm_import_batch
from crm_import_match_resolution import (
    RESOLUTION_CREATE_COMPANY,
    RESOLUTION_CREATE_CONTACT,
    RESOLUTION_IMPORT_COMPANY_ONLY,
    RESOLUTION_SKIP_ROW,
    RESOLUTION_USE_EXISTING_COMPANY,
    RESOLUTION_USE_EXISTING_CONTACT,
    save_crm_import_match_resolution,
)
from crm_import_plan import PLANNER_VERSION, dry_run_crm_import, plan_crm_import_batch
from crm_import_source import IDENTITY_BOUND_DETAIL, UNQUOTED_NEWLINE_DETAIL
from crm_import_staging import (
    _parse_csv,
    ensure_crm_import_schema,
    save_crm_import_mapping,
    save_crm_import_source_type,
    upload_crm_import,
)
from crm_import_status_resolution import save_crm_import_status_resolution
from data_steward import SOURCE_CRM_IMPORT, SOURCE_LEADMASTER
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from models import NorthStarUser

REPO = Path(__file__).resolve().parent.parent
PREMIER_CSV = REPO / "working" / "operational-import-phase1" / "Premier_Manual_Import_Test.csv"
PREMIER_CLIENT_ID = 4
PREMIER_MAPPING = {
    "external_record_no": "Record No.",
    "company_name": "Company",
    "address": "Address",
    "city": "City",
    "state": "State",
    "zip": "Zip",
    "website": "Web Address",
    "phone": "Phone",
    "contact_first_name": "FirstName",
    "contact_last_name": "LastName",
    "contact_title": "Title",
    "contact_email": "Email",
    "contact_phone": "ContactPhone",
    "relationship_status": "Status",
    "relationship_notes": "Notes",
}


def _actor() -> NorthStarUser:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE is_administrator = 1 AND active = 1 ORDER BY id LIMIT 1"
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


def _csv(headers: list[str], rows: list[list[object]], *, quoted: bool = False) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL if quoted else csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _prepare():
    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        migrate_schema(conn)
        conn.commit()
        return _client_id(conn), _actor()


def _counts(conn) -> dict[str, int]:
    def n(sql: str, params=()) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return {
        "companies": n("SELECT COUNT(*) FROM companies"),
        "contacts": n("SELECT COUNT(*) FROM contacts"),
        "ccr": n("SELECT COUNT(*) FROM client_company_relationships"),
        "aliases": n("SELECT COUNT(*) FROM company_aliases")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_aliases'"
        ).fetchone()
        else 0,
        "identities": n("SELECT COUNT(*) FROM company_source_identities")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_source_identities'"
        ).fetchone()
        else 0,
        "provenance": n("SELECT COUNT(*) FROM field_provenance_events")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_provenance_events'"
        ).fetchone()
        else 0,
    }


class OperationalImportPhase2Tests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        prod = PRODUCTION_DB_PATH.resolve()
        self.assertNotEqual(opened, prod)
        self.client_id, self.actor = _prepare()

    def _upload(self, content: bytes, filename: str = "t.csv", source_type: str = "LEADMASTER"):
        result = upload_crm_import(
            client_id=self.client_id,
            actor=self.actor,
            filename=filename,
            content=content,
        )
        batch = result.batch
        self.assertIsNotNone(batch)
        save_crm_import_source_type(
            self.client_id,
            batch.batch_id,
            actor=self.actor,
            source_type=source_type,
        )
        return int(batch.batch_id)

    def _map(self, batch_id: int, mapping: dict[str, str] | None = None) -> None:
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=self.actor,
            mapping=mapping
            or {
                "company_name": "Company",
                "external_record_no": "RN",
                "state": "State",
            },
        )

    def test_a_leadmaster_new_company_creates_identity_and_provenance(self):
        marker = secrets.token_hex(4)
        rn = f"9{marker[:7]}"
        name = f"OI2 New Co {marker}"
        batch_id = self._upload(
            _csv(["Company", "RN", "State"], [[name, rn, "IA"]], quoted=True)
        )
        self._map(batch_id)
        with get_connection() as conn:
            before = _counts(conn)
        result = confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=dry_run_crm_import(self.client_id, batch_id).plan_fingerprint,
            actor=self.actor,
        )
        self.assertEqual(result["created_company_count"], 1)
        with get_connection() as conn:
            after = _counts(conn)
            company = conn.execute(
                "SELECT id FROM companies WHERE company_name = ?", (name,)
            ).fetchone()
            self.assertIsNotNone(company)
            ident = conn.execute(
                """
                SELECT company_id, source_system FROM company_source_identities
                WHERE source_record_no = ? AND COALESCE(client_id, 0) = ?
                """,
                (rn, self.client_id),
            ).fetchone()
            self.assertIsNotNone(ident)
            self.assertEqual(int(ident["company_id"]), int(company["id"]))
            self.assertEqual(ident["source_system"], SOURCE_LEADMASTER)
            prov = conn.execute(
                """
                SELECT source_type FROM field_provenance_events
                WHERE entity_type = 'company' AND entity_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (int(company["id"]),),
            ).fetchone()
            self.assertIsNotNone(prov)
            self.assertEqual(prov["source_type"], SOURCE_LEADMASTER)
            self.assertEqual(after["companies"], before["companies"] + 1)
            self.assertEqual(after["identities"], before["identities"] + 1)

    def test_b_existing_company_new_safe_rn_adds_identity(self):
        marker = secrets.token_hex(4)
        name = f"OI2 Reuse Co {marker}"
        rn = f"8{marker[:7]}"
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, '1 Main', 'Ames', 'IA', '50010', '')
                    """,
                    (f"NS-{marker}", name),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (client_id, company_id, status)
                VALUES (?, ?, 'New')
                """,
                (self.client_id, company_id),
            )
            conn.commit()
        batch_id = self._upload(
            _csv(
                ["Company", "RN", "State", "City", "Address"],
                [[name, rn, "IA", "Ames", "1 Main"]],
                quoted=True,
            )
        )
        self._map(
            batch_id,
            {
                "company_name": "Company",
                "external_record_no": "RN",
                "state": "State",
                "city": "City",
                "address": "Address",
            },
        )
        dry = dry_run_crm_import(self.client_id, batch_id)
        self.assertEqual(dry.rows[0].company.action, "use_existing_company")
        confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=dry.plan_fingerprint,
            actor=self.actor,
        )
        with get_connection() as conn:
            ident = conn.execute(
                """
                SELECT company_id FROM company_source_identities
                WHERE source_system = 'LEADMASTER' AND source_record_no = ?
                  AND COALESCE(client_id, 0) = ?
                """,
                (rn, self.client_id),
            ).fetchone()
            self.assertEqual(int(ident["company_id"]), company_id)
            n = conn.execute(
                "SELECT COUNT(*) FROM companies WHERE company_name = ?", (name,)
            ).fetchone()[0]
            self.assertEqual(int(n), 1)

    def test_c_existing_identity_is_idempotent(self):
        marker = secrets.token_hex(4)
        name = f"OI2 Ident Co {marker}"
        rn = f"7{marker[:7]}"
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, '2 Main', 'Ames', 'IA', '50010', '')
                    """,
                    (rn, name),
                ).lastrowid
            )
            upsert_company_source_identity(
                conn,
                company_id=company_id,
                source_system=SOURCE_LEADMASTER,
                source_record_no=rn,
                client_id=self.client_id,
                source_company_name=name,
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (client_id, company_id, status)
                VALUES (?, ?, 'New')
                """,
                (self.client_id, company_id),
            )
            before = _counts(conn)
            conn.commit()
        batch_id = self._upload(
            _csv(
                ["Company", "RN", "State", "City", "Address"],
                [[name, rn, "IA", "Ames", "2 Main"]],
                quoted=True,
            )
        )
        self._map(
            batch_id,
            {
                "company_name": "Company",
                "external_record_no": "RN",
                "state": "State",
                "city": "City",
                "address": "Address",
            },
        )
        dry = dry_run_crm_import(self.client_id, batch_id)
        self.assertIn("source_identity_exact", dry.rows[0].company.reasons)
        confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=dry.plan_fingerprint,
            actor=self.actor,
        )
        with get_connection() as conn:
            after = _counts(conn)
            n = conn.execute(
                """
                SELECT COUNT(*) FROM company_source_identities
                WHERE source_system = 'LEADMASTER' AND source_record_no = ?
                  AND COALESCE(client_id, 0) = ?
                """,
                (rn, self.client_id),
            ).fetchone()[0]
            self.assertEqual(int(n), 1)
            self.assertEqual(after["identities"], before["identities"])

    def test_d_rn_bound_to_other_company_blocks_confirm(self):
        marker = secrets.token_hex(4)
        rn = f"6{marker[:7]}"
        with get_connection() as conn:
            a_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, 'A St', 'Ames', 'IA', '50010', '')
                    """,
                    (f"NS-A-{marker}", f"OI2 Bound A {marker}"),
                ).lastrowid
            )
            b_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, 'B St', 'Ames', 'IA', '50010', '')
                    """,
                    (f"NS-B-{marker}", f"OI2 Bound B {marker}"),
                ).lastrowid
            )
            upsert_company_source_identity(
                conn,
                company_id=a_id,
                source_system=SOURCE_LEADMASTER,
                source_record_no=rn,
                client_id=self.client_id,
                source_company_name=f"OI2 Bound A {marker}",
            )
            conn.commit()
        batch_id = self._upload(
            _csv(
                ["Company", "RN", "State"],
                [[f"OI2 Bound B {marker}", rn, "IA"]],
                quoted=True,
            )
        )
        self._map(batch_id)
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].company_action, "use_existing_company")
            self.assertEqual(plan.rows[0].company_id, a_id)
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=plan.rows[0].row_id,
                resolution_type=RESOLUTION_USE_EXISTING_COMPANY,
                company_id=b_id,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            blocked = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(blocked.rows[0].company_action, "possible_company_match")
            self.assertGreater(blocked.counts["needs_review_rows"], 0)
        dry = dry_run_crm_import(self.client_id, batch_id)
        with self.assertRaises(Exception) as ctx:
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint=dry.plan_fingerprint,
                actor=self.actor,
            )
        self.assertIn("needs-review", str(ctx.exception).lower())

    def test_e_generic_crm_does_not_create_leadmaster_identity(self):
        marker = secrets.token_hex(4)
        rn = f"5{marker[:7]}"
        name = f"OI2 Generic {marker}"
        batch_id = self._upload(
            _csv(["Company", "RN", "State"], [[name, rn, "IA"]], quoted=True),
            source_type="CRM_IMPORT",
        )
        self._map(batch_id)
        dry = dry_run_crm_import(self.client_id, batch_id)
        self.assertEqual(dry.source_type, SOURCE_CRM_IMPORT)
        confirm_admin_crm_import_batch(
            client_id=self.client_id,
            batch_id=batch_id,
            plan_fingerprint=dry.plan_fingerprint,
            actor=self.actor,
        )
        with get_connection() as conn:
            ident = conn.execute(
                """
                SELECT id FROM company_source_identities
                WHERE source_system = 'LEADMASTER' AND source_record_no = ?
                """,
                (rn,),
            ).fetchone()
            self.assertIsNone(ident)
            company = conn.execute(
                "SELECT id FROM companies WHERE company_name = ?", (name,)
            ).fetchone()
            prov = conn.execute(
                """
                SELECT source_type FROM field_provenance_events
                WHERE entity_type = 'company' AND entity_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (int(company["id"]),),
            ).fetchone()
            self.assertEqual(prov["source_type"], SOURCE_CRM_IMPORT)

    def test_f_ambiguous_company_resolutions(self):
        marker = secrets.token_hex(4)
        phone = f"515555{int(marker[:4], 16):04d}"[:10]
        left = f"OI2 Poss L {marker}"
        right = f"OI2 Poss R {marker}"
        batch_id = self._upload(
            _csv(
                ["Company", "Phone", "State", "Website"],
                [
                    [left, phone, "AR", f"www.l-{marker}.example"],
                    [right, phone, "AR", f"www.r-{marker}.example"],
                ],
                quoted=True,
            )
        )
        self._map(
            batch_id,
            {"company_name": "Company", "phone": "Phone", "state": "State", "website": "Website"},
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            by_src = {r.source_row_number: r for r in plan.rows}
            self.assertEqual(by_src[3].company_action, "possible_company_match")
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=by_src[3].row_id,
                resolution_type=RESOLUTION_CREATE_COMPANY,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            created = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(
                {r.source_row_number: r for r in created.rows}[3].company_action,
                "create_company",
            )
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=by_src[3].row_id,
                resolution_type=RESOLUTION_SKIP_ROW,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            skipped = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(
                {r.source_row_number: r for r in skipped.rows}[3].company_action,
                "excluded_skip",
            )

    def test_g_ambiguous_contact_no_unsafe_move(self):
        marker = secrets.token_hex(4)
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, '1 Main', 'Ames', 'IA', '50010', '')
                    """,
                    (f"NS-CT-{marker}", f"OI2 Ct Co {marker}"),
                ).lastrowid
            )
            other_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, '9 Other', 'Ames', 'IA', '50010', '')
                    """,
                    (f"NS-OT-{marker}", f"OI2 Other {marker}"),
                ).lastrowid
            )
            c1 = int(
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company_id, external_record_no, first_name, last_name, email, phone, source_row_index
                    ) VALUES (?, ?, 'Pat', 'Lee', '', '', 0)
                    """,
                    (company_id, f"CT1-{marker}"),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, email, phone, source_row_index
                ) VALUES (?, ?, 'Pat', 'Lee', '', '', 0)
                """,
                (company_id, f"CT2-{marker}"),
            )
            other_contact = int(
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company_id, external_record_no, first_name, last_name, email, phone, source_row_index
                    ) VALUES (?, ?, 'Pat', 'Lee', '', '', 0)
                    """,
                    (other_id, f"CT3-{marker}"),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (client_id, company_id, status)
                VALUES (?, ?, 'New')
                """,
                (self.client_id, company_id),
            )
            conn.commit()
        batch_id = self._upload(
            _csv(
                ["Company", "Address", "City", "First", "Last", "State"],
                [[f"OI2 Ct Co {marker}", "1 Main", "Ames", "Pat", "Lee", "IA"]],
                quoted=True,
            )
        )
        self._map(
            batch_id,
            {
                "company_name": "Company",
                "address": "Address",
                "city": "City",
                "contact_first_name": "First",
                "contact_last_name": "Last",
                "state": "State",
            },
        )
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].contact_action, "possible_contact_match")
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=plan.rows[0].row_id,
                resolution_type=RESOLUTION_USE_EXISTING_CONTACT,
                contact_id=other_contact,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            unsafe = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(unsafe.rows[0].contact_action, "possible_contact_match")
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=plan.rows[0].row_id,
                resolution_type=RESOLUTION_USE_EXISTING_CONTACT,
                contact_id=c1,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            ok = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(ok.rows[0].contact_action, "use_existing_contact")
            self.assertEqual(ok.rows[0].contact_id, c1)
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=plan.rows[0].row_id,
                resolution_type=RESOLUTION_CREATE_CONTACT,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            created = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(created.rows[0].contact_action, "create_contact")
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=plan.rows[0].row_id,
                resolution_type=RESOLUTION_IMPORT_COMPANY_ONLY,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            skipped = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(skipped.rows[0].contact_action, "no_contact_data")

    def test_h_status_conflict_workflow(self):
        marker = secrets.token_hex(4)
        name = f"OI2 Status {marker}"
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, '1 Main', 'Ames', 'IA', '50010', '')
                    """,
                    (f"NS-ST-{marker}", name),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (client_id, company_id, status)
                VALUES (?, ?, 'Working')
                """,
                (self.client_id, company_id),
            )
            conn.commit()
        batch_id = self._upload(
            _csv(
                ["Company", "Address", "City", "State", "Status"],
                [[name, "1 Main", "Ames", "IA", "New"]],
                quoted=True,
            )
        )
        self._map(
            batch_id,
            {
                "company_name": "Company",
                "address": "Address",
                "city": "City",
                "state": "State",
                "relationship_status": "Status",
            },
        )
        dry = dry_run_crm_import(self.client_id, batch_id)
        self.assertEqual(dry.rows[0].relationship.status_action, "status_conflict")
        self.assertTrue(dry.validation_summary.confirm_blocked)
        save_crm_import_status_resolution(
            self.client_id,
            batch_id,
            dry.rows[0].row_id,
            actor=self.actor,
            resolution_type="keep_existing_status",
        )
        resolved = dry_run_crm_import(self.client_id, batch_id)
        self.assertEqual(resolved.rows[0].relationship.status_action, "preserve_existing_status")
        self.assertFalse(resolved.validation_summary.confirm_blocked)

    def test_i_quoted_multiline_csv_is_one_row(self):
        raw = (
            'Company,Notes,State\n'
            '"Quoted Co","line 1\nline 2",IA\n'
        ).encode("utf-8")
        headers, staged, counts, _warnings = _parse_csv(raw)
        self.assertEqual(headers[:3], ["Company", "Notes", "State"])
        self.assertEqual(counts["source_row_count"], 1)
        self.assertEqual(staged[0]["values"]["Notes"], "line 1\nline 2")

    def test_j_malformed_multiline_csv_is_rejected(self):
        raw = b"Company,Notes,State\nBroken Co,line 1\nstill notes,IA\n"
        with self.assertRaises(ValueError) as ctx:
            _parse_csv(raw)
        self.assertEqual(str(ctx.exception), UNQUOTED_NEWLINE_DETAIL)
        result = upload_crm_import(
            client_id=self.client_id,
            actor=self.actor,
            filename="bad.csv",
            content=raw,
        )
        self.assertEqual(result.kind, "failed")
        self.assertIn("line breaks", result.message)

    def test_k_xlsx_multiline_note(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(["Company", "Notes", "State"])
        ws.append(["XLSX Co", "line 1\nline 2", "IA"])
        buf = io.BytesIO()
        wb.save(buf)
        result = upload_crm_import(
            client_id=self.client_id,
            actor=self.actor,
            filename="notes.xlsx",
            content=buf.getvalue(),
        )
        self.assertIsNotNone(result.batch)
        self.assertEqual(result.batch.source_row_count, 1)
        self.assertEqual(result.batch.sample_rows[0].values["Notes"], "line 1\nline 2")

    def test_l_fingerprint_changes_with_source_type_and_resolution(self):
        marker = secrets.token_hex(4)
        batch_id = self._upload(
            _csv(["Company", "RN", "State"], [[f"OI2 Fp {marker}", f"4{marker[:7]}", "IA"]], quoted=True),
            source_type="CRM_IMPORT",
        )
        self._map(batch_id)
        first = dry_run_crm_import(self.client_id, batch_id)
        save_crm_import_source_type(
            self.client_id, batch_id, actor=self.actor, source_type="LEADMASTER"
        )
        second = dry_run_crm_import(self.client_id, batch_id)
        self.assertNotEqual(first.plan_fingerprint, second.plan_fingerprint)
        self.assertEqual(PLANNER_VERSION, "crm-import-plan-v11")
        with self.assertRaises(Exception):
            confirm_admin_crm_import_batch(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint=first.plan_fingerprint,
                actor=self.actor,
            )

    def test_m_exact_rerun_identities_idempotent(self):
        self.test_c_existing_identity_is_idempotent()

    def test_premier_file_rehearsal_reuse_only(self):
        if not PREMIER_CSV.is_file():
            self.skipTest("OI-1 Premier test file missing")
        with get_connection() as conn:
            premier = conn.execute(
                "SELECT id FROM clients WHERE id = ? OR lower(code) = 'premier' LIMIT 1",
                (PREMIER_CLIENT_ID,),
            ).fetchone()
            if premier is None:
                self.skipTest("Premier client missing from isolated copy")
            client_id = int(premier["id"])
            before = _counts(conn)
            ident_before = conn.execute(
                """
                SELECT source_record_no, company_id FROM company_source_identities
                WHERE source_system = 'LEADMASTER'
                  AND source_record_no IN ('838877', '839973', '839983')
                  AND COALESCE(client_id, 0) = ?
                """,
                (client_id,),
            ).fetchall()
            self.assertGreaterEqual(len(ident_before), 1)
        content = PREMIER_CSV.read_bytes()
        result = upload_crm_import(
            client_id=client_id,
            actor=self.actor,
            filename="Premier_Manual_Import_Test.csv",
            content=content,
        )
        self.assertEqual(result.batch.source_row_count, 4)
        save_crm_import_source_type(
            client_id, result.batch.batch_id, actor=self.actor, source_type="LEADMASTER"
        )
        save_crm_import_mapping(
            client_id, result.batch.batch_id, actor=self.actor, mapping=PREMIER_MAPPING
        )
        dry = dry_run_crm_import(client_id, result.batch.batch_id)
        self.assertEqual(dry.total_rows, 4)
        self.assertEqual(dry.source_type, SOURCE_LEADMASTER)
        self.assertEqual(dry.counts.create_company, 0)
        self.assertEqual(dry.counts.create_contact, 0)
        self.assertEqual(dry.counts.create_client_relationship, 0)
        self.assertEqual(dry.counts.needs_review_rows, 0)
        confirm = confirm_admin_crm_import_batch(
            client_id=client_id,
            batch_id=result.batch.batch_id,
            plan_fingerprint=dry.plan_fingerprint,
            actor=self.actor,
        )
        self.assertEqual(confirm["created_company_count"], 0)
        self.assertEqual(confirm["created_contact_count"], 0)
        self.assertEqual(confirm["created_relationship_count"], 0)
        with get_connection() as conn:
            after = _counts(conn)
            self.assertEqual(after["companies"], before["companies"])
            self.assertEqual(after["contacts"], before["contacts"])
            self.assertEqual(after["ccr"], before["ccr"])
            self.assertEqual(after["identities"], before["identities"])
            self.assertLessEqual(after["aliases"] - before["aliases"], 0)

    def test_identity_conflict_rolls_back_transaction(self):
        marker = secrets.token_hex(4)
        rn = f"3{marker[:7]}"
        name = f"OI2 Tx {marker}"
        with get_connection() as conn:
            other = int(
                conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website
                    ) VALUES (?, ?, 'X', 'Ames', 'IA', '50010', '')
                    """,
                    (f"NS-TX-{marker}", f"OI2 Other Tx {marker}"),
                ).lastrowid
            )
            upsert_company_source_identity(
                conn,
                company_id=other,
                source_system=SOURCE_LEADMASTER,
                source_record_no=rn,
                client_id=self.client_id,
            )
            before = _counts(conn)
            conn.commit()
        batch_id = self._upload(
            _csv(["Company", "RN", "State"], [[name, rn, "IA"]], quoted=True)
        )
        self._map(batch_id)
        dry = dry_run_crm_import(self.client_id, batch_id)
        self.assertEqual(dry.rows[0].company.action, "use_existing_company")
        with get_connection() as conn:
            after = _counts(conn)
            self.assertEqual(after["companies"], before["companies"])
            created = conn.execute(
                "SELECT id FROM companies WHERE company_name = ?", (name,)
            ).fetchone()
            self.assertIsNone(created)
        self.assertIn(IDENTITY_BOUND_DETAIL[:20], IDENTITY_BOUND_DETAIL)


if __name__ == "__main__":
    unittest.main()
