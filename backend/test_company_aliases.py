"""Phase 2 — durable company aliases and going-forward source RN preservation.

Isolated testdb only. Does not write production northstar.db.
Does not backfill live aliases or repair Brown Johnson Controls rows.
"""

from __future__ import annotations

import json
import os
import secrets
import unittest
from pathlib import Path

import testdb

from company_aliases import (
    SOURCE_CLIENT_DATA_IMPORT,
    SOURCE_CRM_IMPORT,
    capture_import_company_alias,
    ensure_company_alias_schema,
    list_aliases_for_companies,
    should_store_alias,
    upsert_company_alias,
)
from crm_add_data import match_company
from crm_import_confirm import apply_crm_import_plan_on_connection
from crm_import_match_resolution import (
    RESOLUTION_USE_EXISTING_COMPANY,
    ensure_crm_import_match_resolution_schema,
    save_crm_import_match_resolution,
)
from crm_import_plan import (
    PLANNER_VERSION,
    CompanyRec,
    _match_company,
    plan_crm_import_batch,
)
from crm_import_staging import ensure_crm_import_schema, save_crm_import_mapping
from crm_import_state import state_for_match
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
    alias_record_nos: tuple[str, ...] = (),
    alias_norm_names: tuple[str, ...] = (),
    alias_domains: tuple[str, ...] = (),
    alias_phones: tuple[str, ...] = (),
    alias_addrs: tuple[tuple[str, str, str], ...] = (),
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
        alias_record_nos=alias_record_nos,
        alias_norm_names=alias_norm_names,
        alias_domains=alias_domains,
        alias_phones=alias_phones,
        alias_addrs=alias_addrs,
    )


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


def _insert_batch(conn, client_id: int, headers: list[str], rows: list[dict]) -> int:
    cur = conn.execute(
        """
        INSERT INTO crm_import_batches (
            client_id, uploaded_by_user_id, uploaded_by_name, original_filename,
            file_type, sha256, status, headers_json, total_rows, source_row_count,
            created_at, updated_at, expires_at
        ) VALUES (?, 1, 'Admin', 'alias.csv', 'csv', ?, 'previewed', ?, ?, ?,
                  datetime('now'), datetime('now'), '2099-01-01T00:00:00Z')
        """,
        (
            client_id,
            f"sha-alias-{secrets.token_hex(8)}",
            json.dumps(headers),
            len(rows),
            len(rows),
        ),
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


def _insert_company(
    conn,
    *,
    record_no: str,
    name: str,
    address: str = "507 E Michigan St",
    city: str = "Milwaukee",
    state: str = "WI",
    zip_code: str = "53202",
    phone: str = "(414) 524-4000",
    website: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, address, city, state, zip, website,
            legacy_phone, created_at, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (record_no, name, address, city, state, zip_code, website, phone),
    )
    return int(cur.lastrowid)


class CompanyAliasUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            ensure_company_alias_schema(conn)
            migrate_schema(conn)
            conn.commit()

    def test_planner_version_bumped_for_aliases(self) -> None:
        self.assertEqual(PLANNER_VERSION, "crm-import-plan-v11")

    def test_upsert_is_idempotent_and_preserves_first_alias_name(self) -> None:
        marker = secrets.token_hex(4)
        with get_connection() as conn:
            cid = _insert_company(conn, record_no=f"P2-IDEM-{marker}", name="Johnson Controls")
            first, created = upsert_company_alias(
                conn,
                company_id=cid,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no="1326674",
                client_id=1,
                source_address="1 Plant Rd",
            )
            self.assertTrue(created)
            second, created_again = upsert_company_alias(
                conn,
                company_id=cid,
                alias_name=f"Johnson Controls - Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no="1326674",
                client_id=1,
                source_phone="3165550100",
            )
            conn.commit()
            rows = conn.execute(
                "SELECT alias_name, source_address, source_phone FROM company_aliases WHERE company_id=?",
                (cid,),
            ).fetchall()
        self.assertEqual(first, second)
        self.assertFalse(created_again)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["alias_name"], f"Johnson Controls-Wichita Plant {marker}")
        self.assertEqual(rows[0]["source_address"], "1 Plant Rd")
        self.assertEqual(rows[0]["source_phone"], "3165550100")

    def test_same_alias_different_clients_are_both_kept(self) -> None:
        marker = secrets.token_hex(4)
        with get_connection() as conn:
            cid = _insert_company(conn, record_no=f"P2-CLI-{marker}", name="Johnson Controls")
            upsert_company_alias(
                conn,
                company_id=cid,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no="1326674",
                client_id=1,
            )
            upsert_company_alias(
                conn,
                company_id=cid,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no="1326674",
                client_id=2,
            )
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM company_aliases WHERE company_id=?",
                (cid,),
            ).fetchone()["n"]
        self.assertEqual(int(n), 2)

    def test_same_company_different_source_rns_are_both_kept(self) -> None:
        marker = secrets.token_hex(4)
        with get_connection() as conn:
            cid = _insert_company(conn, record_no=f"P2-RN-{marker}", name="Johnson Controls")
            upsert_company_alias(
                conn,
                company_id=cid,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no="1326674",
                client_id=1,
            )
            upsert_company_alias(
                conn,
                company_id=cid,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no="1326675",
                client_id=1,
            )
            conn.commit()
            rns = {
                r["source_record_no"]
                for r in conn.execute(
                    "SELECT source_record_no FROM company_aliases WHERE company_id=?",
                    (cid,),
                )
            }
        self.assertEqual(rns, {"1326674", "1326675"})

    def test_manual_identical_name_and_rn_is_not_stored(self) -> None:
        self.assertFalse(
            should_store_alias(
                source_system="manual",
                alias_name="Johnson Controls",
                canonical_name="Johnson Controls",
                source_record_no="102072",
                master_record_no="102072",
            )
        )
        self.assertTrue(
            should_store_alias(
                source_system="manual",
                alias_name="Johnson Controls-Wichita Plant",
                canonical_name="Johnson Controls",
                source_record_no="1326674",
                master_record_no="102072",
            )
        )

    def test_export_helper_lists_alias_provenance(self) -> None:
        marker = secrets.token_hex(4)
        with get_connection() as conn:
            cid = _insert_company(conn, record_no=f"P2-EX-{marker}", name="Johnson Controls")
            upsert_company_alias(
                conn,
                company_id=cid,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CLIENT_DATA_IMPORT,
                source_record_no="1326674",
                client_id=2,
                source_city="Wichita",
                source_state="KS",
            )
            conn.commit()
            rows = list_aliases_for_companies(conn, [cid])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["alias_name"], f"Johnson Controls-Wichita Plant {marker}")
        self.assertEqual(rows[0]["source_record_no"], "1326674")
        self.assertEqual(rows[0]["client_id"], 2)
        self.assertEqual(rows[0]["source_system"], SOURCE_CLIENT_DATA_IMPORT)


class AliasMatchPolicyTests(unittest.TestCase):
    def test_alias_record_no_reuses_even_when_canonical_rn_differs(self) -> None:
        existing = _rec(
            company_id=551,
            name="Johnson Controls",
            address="507 E Michigan St",
            city="Milwaukee",
            state="WI",
            phone="4145244000",
            record_no="102072",
            alias_record_nos=("1326674",),
            alias_norm_names=(norm_name("Johnson Controls-Wichita Plant"),),
        )
        incoming = _rec(
            name="Johnson Controls-Wichita Plant",
            address="100 JCI Way",
            city="York",
            state="PA",
            record_no="1326674",
        )
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 551)
        self.assertIn("record_no_exact", reasons)
        self.assertIn("alias_record_no", reasons)

    def test_alias_name_plus_corroborating_address_reuses(self) -> None:
        existing = _rec(
            company_id=551,
            name="Johnson Controls",
            address="507 E Michigan St",
            city="Milwaukee",
            state="WI",
            phone="4145244000",
            record_no="102072",
            alias_norm_names=(norm_name("Johnson Controls-Wichita Plant"),),
            alias_addrs=(
                (
                    norm_addr("8400 E 21st St"),
                    "wichita",
                    state_for_match("KS") or "KS",
                ),
            ),
        )
        incoming = _rec(
            name="Johnson Controls-Wichita Plant",
            address="8400 E 21st Street",
            city="Wichita",
            state="KS",
            record_no="1326674",
        )
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 551)
        self.assertIn("name_exact", reasons)
        self.assertIn("address_city_state", reasons)

    def test_alias_name_only_with_conflicting_location_is_review(self) -> None:
        existing = _rec(
            company_id=551,
            name="Johnson Controls",
            address="507 E Michigan St",
            city="Milwaukee",
            state="WI",
            phone="4145244000",
            record_no="102072",
            alias_norm_names=(norm_name("Johnson Controls-Wichita Plant"),),
            alias_addrs=(
                (
                    norm_addr("8400 E 21st St"),
                    "wichita",
                    state_for_match("KS") or "KS",
                ),
            ),
        )
        incoming = _rec(
            name="Johnson Controls-Wichita Plant",
            address="100 JCI Way",
            city="York",
            state="PA",
            phone="7172681868",
            record_no="1326674",
        )
        action, matched, reasons, _extras = _match_company(incoming, [existing], [])
        self.assertEqual(action, "possible_company_match")
        self.assertNotEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 551)
        self.assertEqual(reasons, ["name_exact"])

    def test_cdi_exclusive_rn_can_reuse_via_alias(self) -> None:
        existing = _rec(
            company_id=80,
            name="Johnson Controls",
            record_no="102072",
            alias_record_nos=("1326674",),
        )
        incoming = _rec(name="Johnson Controls-Wichita Plant", record_no="1326674")
        action, matched, reasons, _extras = _match_company(
            incoming, [existing], [], record_no_exclusive=True
        )
        self.assertEqual(action, "use_existing_company")
        self.assertEqual(matched.company_id if matched else None, 80)
        self.assertIn("alias_record_no", reasons)


class AliasImportWritePathTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            ensure_crm_import_schema(conn)
            ensure_crm_import_match_resolution_schema(conn)
            ensure_company_alias_schema(conn)
            migrate_schema(conn)
            conn.commit()
            self.client_id = int(
                conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"]
            )
        self.actor = _actor()

    def _map(self, batch_id: int) -> None:
        save_crm_import_mapping(
            self.client_id,
            batch_id,
            actor=self.actor,
            mapping={
                "company_name": "Company",
                "external_record_no": "RN",
                "address": "Address",
                "city": "City",
                "state": "State",
                "zip": "ZIP",
                "phone": "Phone",
            },
        )

    def _apply(self, conn, client_id: int, batch_id: int, *, source_system: str = SOURCE_CRM_IMPORT):
        conn.execute("BEGIN IMMEDIATE")
        plan = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_id)
        result = apply_crm_import_plan_on_connection(
            conn,
            client_id=client_id,
            batch_id=batch_id,
            plan=plan,
            actor=self.actor,
            source_system=source_system,
        )
        conn.commit()
        return result

    def test_reuse_does_not_rename_master_and_preserves_client_rn(self) -> None:
        marker = secrets.token_hex(4)
        master_rn = f"P2A-102072-{marker}"
        incoming_rn = f"P2A-1326674-{marker}"
        incoming_name = f"Johnson Controls-Wichita Plant {marker}"
        with get_connection() as conn:
            company_id = _insert_company(
                conn,
                record_no=master_rn,
                name="Johnson Controls",
            )
            headers = ["Company", "RN", "Address", "City", "State", "ZIP", "Phone"]
            batch_id = _insert_batch(
                conn,
                self.client_id,
                headers,
                [
                    {
                        "Company": incoming_name,
                        "RN": incoming_rn,
                        "Address": "8400 E 21st St N",
                        "City": "Wichita",
                        "State": "KS",
                        "ZIP": "67206",
                        "Phone": "(316) 555-0100",
                    }
                ],
            )
        self._map(batch_id)
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
            self.assertEqual(plan.rows[0].company_action, "create_company")
            save_crm_import_match_resolution(
                conn,
                client_id=self.client_id,
                batch_id=batch_id,
                staged_row_id=plan.rows[0].row_id,
                resolution_type=RESOLUTION_USE_EXISTING_COMPANY,
                company_id=company_id,
                updated_by_user_id=self.actor.id,
            )
            conn.commit()
            self._apply(conn, self.client_id, batch_id)
            master = conn.execute(
                "SELECT company_name, external_record_no FROM companies WHERE id=?",
                (company_id,),
            ).fetchone()
            alias = conn.execute(
                """
                SELECT alias_name, source_record_no, source_city, source_phone, source_system
                FROM company_aliases WHERE company_id=?
                """,
                (company_id,),
            ).fetchone()
            ccr = conn.execute(
                """
                SELECT external_record_no FROM client_company_relationships
                WHERE client_id=? AND company_id=?
                """,
                (self.client_id, company_id),
            ).fetchone()
            alias_count = conn.execute(
                "SELECT COUNT(*) AS n FROM company_aliases WHERE company_id=?",
                (company_id,),
            ).fetchone()["n"]
            capture_import_company_alias(
                conn,
                company_id=company_id,
                mapped={
                    "company_name": incoming_name,
                    "external_record_no": incoming_rn,
                    "city": "Wichita",
                    "phone": "(316) 555-0100",
                },
                source_system=SOURCE_CRM_IMPORT,
                client_id=self.client_id,
            )
            conn.commit()
            alias_count_after = conn.execute(
                "SELECT COUNT(*) AS n FROM company_aliases WHERE company_id=?",
                (company_id,),
            ).fetchone()["n"]

        self.assertEqual(master["company_name"], "Johnson Controls")
        self.assertEqual(master["external_record_no"], master_rn)
        self.assertEqual(alias["alias_name"], incoming_name)
        self.assertEqual(alias["source_record_no"], incoming_rn)
        self.assertEqual(alias["source_city"], "Wichita")
        self.assertEqual(alias["source_system"], SOURCE_CRM_IMPORT)
        self.assertEqual(ccr["external_record_no"], incoming_rn)
        self.assertEqual(int(alias_count), 1)
        self.assertEqual(int(alias_count_after), 1)

    def test_canonical_create_stores_source_alias_and_rn(self) -> None:
        marker = secrets.token_hex(4)
        incoming_rn = f"P2G-{marker}"
        name = f"Phase2 Create Co {marker}"
        with get_connection() as conn:
            headers = ["Company", "RN", "Address", "City", "State", "ZIP", "Phone"]
            batch_id = _insert_batch(
                conn,
                self.client_id,
                headers,
                [
                    {
                        "Company": name,
                        "RN": incoming_rn,
                        "Address": "1 Main St",
                        "City": "Ames",
                        "State": "IA",
                        "ZIP": "50010",
                        "Phone": "(515) 555-0199",
                    }
                ],
            )
        self._map(batch_id)
        with get_connection() as conn:
            self._apply(
                conn,
                self.client_id,
                batch_id,
                source_system=SOURCE_CLIENT_DATA_IMPORT,
            )
            company = conn.execute(
                "SELECT id, company_name, external_record_no FROM companies WHERE company_name=?",
                (name,),
            ).fetchone()
            alias = conn.execute(
                """
                SELECT alias_name, source_record_no, source_system, source_city
                FROM company_aliases WHERE company_id=?
                """,
                (int(company["id"]),),
            ).fetchone()
            ccr = conn.execute(
                """
                SELECT external_record_no FROM client_company_relationships
                WHERE client_id=? AND company_id=?
                """,
                (self.client_id, int(company["id"])),
            ).fetchone()
        self.assertEqual(company["company_name"], name)
        self.assertEqual(company["external_record_no"], incoming_rn)
        self.assertEqual(alias["alias_name"], name)
        self.assertEqual(alias["source_record_no"], incoming_rn)
        self.assertEqual(alias["source_system"], SOURCE_CLIENT_DATA_IMPORT)
        self.assertEqual(alias["source_city"], "Ames")
        self.assertEqual(ccr["external_record_no"], incoming_rn)

    def test_alias_plus_address_is_loaded_from_table_and_reused(self) -> None:
        marker = secrets.token_hex(4)
        master_rn = f"P2E-102072-{marker}"
        with get_connection() as conn:
            company_id = _insert_company(
                conn,
                record_no=master_rn,
                name="Johnson Controls",
            )
            upsert_company_alias(
                conn,
                company_id=company_id,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no=f"P2E-OLD-{marker}",
                client_id=self.client_id,
                source_address="8400 E 21st St N",
                source_city="Wichita",
                source_state="KS",
            )
            headers = ["Company", "RN", "Address", "City", "State", "ZIP", "Phone"]
            batch_id = _insert_batch(
                conn,
                self.client_id,
                headers,
                [
                    {
                        "Company": f"Johnson Controls-Wichita Plant {marker}",
                        "RN": f"P2E-NEW-{marker}",
                        "Address": "8400 E 21st Street North",
                        "City": "Wichita",
                        "State": "KS",
                        "ZIP": "67206",
                        "Phone": "",
                    }
                ],
            )
            conn.commit()
        self._map(batch_id)
        with get_connection() as conn:
            plan = plan_crm_import_batch(conn, client_id=self.client_id, batch_id=batch_id)
        self.assertEqual(plan.rows[0].company_action, "use_existing_company")
        self.assertEqual(plan.rows[0].company_id, company_id)
        self.assertIn("name_exact", plan.rows[0].company_reasons)
        self.assertIn("address_city_state", plan.rows[0].company_reasons)

    def test_ai_match_uses_alias_name_and_phone_without_name_only_reuse(self) -> None:
        marker = secrets.token_hex(4)
        with get_connection() as conn:
            company_id = _insert_company(
                conn,
                record_no=f"P2AI-{marker}",
                name="Johnson Controls",
                phone="(414) 524-4000",
            )
            upsert_company_alias(
                conn,
                company_id=company_id,
                alias_name=f"Johnson Controls-Wichita Plant {marker}",
                source_system=SOURCE_CRM_IMPORT,
                source_record_no=f"P2AI-RN-{marker}",
                client_id=self.client_id,
                source_phone="(316) 555-0100",
                source_city="Wichita",
                source_state="KS",
                source_address="8400 E 21st St N",
            )
            conn.commit()
            hit = match_company(
                conn,
                CrmAddCompanyInput(
                    company_name=f"Johnson Controls-Wichita Plant {marker}",
                    address="8400 E 21st Street North",
                    city="Wichita",
                    state="KS",
                    phone="(316) 555-0100",
                ),
            )
            miss = match_company(
                conn,
                CrmAddCompanyInput(
                    company_name=f"Johnson Controls-Wichita Plant {marker}",
                    address="100 JCI Way",
                    city="York",
                    state="PA",
                    phone="(717) 268-1868",
                ),
            )
        self.assertEqual(hit["match_type"], "existing_company")
        self.assertEqual(hit["company_id"], company_id)
        self.assertEqual(miss["match_type"], "possible_match")
        self.assertNotEqual(miss["match_type"], "existing_company")


if __name__ == "__main__":
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    unittest.main()
