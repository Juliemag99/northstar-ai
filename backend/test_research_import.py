"""Isolated Research Prospect Import tests. Never writes live northstar.db."""
from __future__ import annotations

import csv
import io
import json
import os
import secrets
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

import testdb

from auth_http import ADMIN_REQUIRED_DETAIL, CSRF_HEADER
from auth_passwords import hash_password
from main import app
from staff_rbac import REVOPS_SPECIALIST, SYSTEM_ADMINISTRATOR, ensure_staff_role_schema

from contact_phone import upsert_contact_phone_keys
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection
from models import NorthStarUser
from research_import import confirm_research_import, dry_run_research_import
from research_import_mapping import (
    blank,
    build_v3_mapping,
    is_department_only_name,
    mapping_safety_class,
    normalize_attribute_key,
    parse_priority,
    parse_typed_value,
    split_multi,
    suggest_mapping,
    validate_mapping,
)
from research_import_plan import (
    CLASS_AMBIGUOUS,
    CLASS_NEW,
    CLASS_NEW_LOCATION,
    CLASS_POSSIBLE,
    CONFLICT_FILL,
    CONFLICT_MANUAL,
    CONFLICT_UPDATE,
    ISOLATED_CONFIRM_DISABLED,
    PRODUCTION_CONFIRM_DISABLED,
    SAME_BATCH_RESEARCH_CONFLICT,
    STALE_FINGERPRINT,
    confirm_enabled,
    is_production_path,
)
from research_import_queries import query_custom_attribute, query_research_by_source, query_research_prospects
from data_steward import SOURCE_MANUAL_ADMIN, record_provenance
from research_import_confirm import inject_confirm_failure
from research_import_policy import (
    CONTACT_EXACT,
    CONTACT_NEW,
    CONTACT_POSSIBLE,
    CONTACT_STRONG,
    FIELD_POLICY,
    MASTER_ACCEPT_INCOMING,
    MASTER_ADD_LOCATION,
    MASTER_FILL,
    MASTER_KEEP,
    POLICY_E,
    RES_CREATE_CONTACT,
    RES_SKIP_CONTACT,
    RES_USE_CONTACT,
    master_actions_for,
    source_is_authoritative,
    source_trust,
)
from research_import_schema import LiveResearchSchemaForbidden, ensure_research_import_schema, is_production_db
from research_import_staging import (
    save_contact_resolution,
    save_mapping,
    save_match_resolution,
    save_master_resolution,
    upload_research_import,
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


def _csv(headers: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


JANCO_HEADERS = [
    "Company Name",
    "Street Address",
    "City",
    "State",
    "ZIP",
    "Phone",
    "Website",
    "Janco Target Market",
    "Equipment / Product",
    "Why Janco Fits",
    "Potential Components",
    "Priority",
    "Target Department",
    "Qualification Notes",
    "Product Source",
    "Address Source",
    "Phone Source",
    "Prior Research File",
]


class ResearchImportTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None)
        self.actor = _actor()
        with get_connection() as conn:
            self.client_id = int(conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()[0])
            self.premier_id = int(
                conn.execute("SELECT id FROM clients WHERE lower(code)='premier'").fetchone()[0]
            )
            self.brown_id = int(
                conn.execute("SELECT id FROM clients WHERE lower(code)='brown'").fetchone()[0]
            )
            ensure_research_import_schema(conn)
            conn.commit()

    def test_schema_refuses_live_path_object(self) -> None:
        self.assertNotEqual(Path(os.fspath(DB_PATH)).resolve(), Path(PRODUCTION_DB_PATH).resolve())
        self.assertFalse(is_production_path())

    def test_parser_janco_headers_and_multiline(self) -> None:
        content = _csv(
            JANCO_HEADERS,
            [
                [
                    "Harvest International",
                    "401 W 20th",
                    "Storm Lake",
                    "IA",
                    "50588",
                    "(712) 213-5100",
                    "https://www.harvest-international.com/",
                    "Agriculture / grain equipment",
                    "Planters",
                    "Makes Planters; Janco could supply panels.",
                    "welded brackets",
                    "A - Iowa",
                    "Purchasing / Supply Chain; Engineering / Operations",
                    "Fit inferred from published products.",
                    "https://www.harvest-international.com/",
                    "https://www.harvest-international.com/contact",
                    "https://www.harvest-international.com/contact",
                    "Paragon_Precision_Metal_200_Prospects.xlsx",
                ]
            ],
        )
        uploaded = upload_research_import(
            client_id=self.client_id,
            actor=self.actor,
            filename="janco.csv",
            content=content,
            research_method="CHATGPT_DEEP_RESEARCH",
            research_date="2026-09-18",
        )
        headers = uploaded["batch"]["headers"]
        mapping = suggest_mapping(headers)
        ok, errors = validate_mapping(mapping, headers)
        self.assertTrue(ok, errors)
        self.assertEqual(mapping["company_name"], "Company Name")
        self.assertEqual(mapping["why_client_fits"], "Why Janco Fits")
        self.assertEqual(mapping["target_market"], "Janco Target Market")
        code, label = parse_priority("A - Iowa")
        self.assertEqual(code, "A")
        self.assertEqual(label, "A - Iowa")
        depts = split_multi("Purchasing / Supply Chain; Engineering / Operations")
        self.assertEqual(depts, ["Purchasing / Supply Chain", "Engineering / Operations"])

    def test_greenheck_evapco_wausau_do_not_auto_resolve(self) -> None:
        content = _csv(
            ["Company Name", "City", "State", "Website", "Phone"],
            [
                ["EVAPCO Newton", "Newton", "IL", "https://www.evapco.com/", ""],
                ["Greenheck", "Schofield", "WI", "https://www.greenheck.com/", ""],
                ["Wausau Equipment", "Wooster", "OH", "https://wausauequipment.com/", "(800) 790-0248"],
            ],
        )
        uploaded = upload_research_import(
            client_id=self.client_id, actor=self.actor, filename="ambig.csv", content=content
        )
        batch_id = uploaded["batch"]["batch_id"]
        save_mapping(
            self.client_id,
            batch_id,
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        plan = dry_run_research_import(self.client_id, batch_id, limit=10)
        classes = {row["company_name"]: row["ri_class"] for row in plan["rows"]}
        self.assertEqual(classes["EVAPCO Newton"], CLASS_AMBIGUOUS)
        self.assertEqual(classes["Greenheck"], CLASS_AMBIGUOUS)
        self.assertIn(classes["Wausau Equipment"], {CLASS_AMBIGUOUS, CLASS_POSSIBLE})
        self.assertTrue(plan["blocking"])
        self.assertFalse(plan["confirm_allowed"])
        self.assertFalse(plan["production_confirm_enabled"])

    def test_khs_phone_only_false_friend(self) -> None:
        with get_connection() as conn:
            phone = blank(
                conn.execute(
                    "SELECT legacy_phone FROM companies WHERE company_name='MGS Machine' LIMIT 1"
                ).fetchone()[0]
            )
        content = _csv(
            ["Company Name", "City", "State", "Phone", "Website"],
            [["KHS USA", "Waukesha", "WI", phone, "https://www.khs.com/en"]],
        )
        uploaded = upload_research_import(
            client_id=self.client_id, actor=self.actor, filename="khs.csv", content=content
        )
        batch_id = uploaded["batch"]["batch_id"]
        save_mapping(
            self.client_id,
            batch_id,
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        plan = dry_run_research_import(self.client_id, batch_id, limit=5)
        row = plan["rows"][0]
        self.assertEqual(row["ri_class"], CLASS_POSSIBLE)
        self.assertIn("phone", row["matcher_reasons"])
        self.assertTrue(row["blocking"])

    def test_new_company_and_existing_company_planning(self) -> None:
        content = _csv(
            ["Company Name", "City", "State", "Website", "Phone", "Street Address"],
            [
                ["Harvest International", "Storm Lake", "IA", "https://www.harvest-international.com/", "(712) 213-5100", "401 W 20th"],
                ["Sudenga Industries", "George", "IA", "https://sudenga.com/", "(888) 783-3642", "2002 Kingbird Ave"],
            ],
        )
        uploaded = upload_research_import(
            client_id=self.client_id, actor=self.actor, filename="mix.csv", content=content
        )
        batch_id = uploaded["batch"]["batch_id"]
        save_mapping(
            self.client_id,
            batch_id,
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        plan = dry_run_research_import(self.client_id, batch_id, limit=10)
        by_name = {row["company_name"]: row for row in plan["rows"]}
        self.assertEqual(by_name["Harvest International"]["ri_class"], CLASS_NEW)
        self.assertEqual(by_name["Harvest International"]["relationship_action"], "create_client_relationship")
        self.assertIn(by_name["Sudenga Industries"]["ri_class"], {"EXACT_EXISTING", "STRONG_EXISTING"})
        self.assertTrue(by_name["Sudenga Industries"]["matched_company_id"])

    def test_default_status_and_priority_separation(self) -> None:
        content = _csv(
            ["Company Name", "Priority", "Website"],
            [["Brand New Research Co", "A - Iowa", "https://brand-new-research-co.example/"]],
        )
        uploaded = upload_research_import(
            client_id=self.premier_id, actor=self.actor, filename="prio.csv", content=content
        )
        batch_id = uploaded["batch"]["batch_id"]
        save_mapping(
            self.premier_id,
            batch_id,
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        plan = dry_run_research_import(self.premier_id, batch_id, limit=5)
        row = plan["rows"][0]
        self.assertEqual(row["planned_status"], "New")
        self.assertEqual(row["research_priority_code"], "A")
        self.assertEqual(row["research_priority_label"], "A - Iowa")
        self.assertNotEqual(row["planned_status"], "Hot Prospect")

    def test_production_confirm_disabled_and_stale_fingerprint(self) -> None:
        content = _csv(["Company Name"], [["Harvest International"]])
        uploaded = upload_research_import(
            client_id=self.client_id, actor=self.actor, filename="one.csv", content=content
        )
        batch_id = uploaded["batch"]["batch_id"]
        save_mapping(
            self.client_id,
            batch_id,
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        plan = dry_run_research_import(self.client_id, batch_id, limit=5)
        with self.assertRaises(PermissionError) as raised:
            confirm_research_import(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint=plan["plan_fingerprint"],
                actor=self.actor,
            )
        self.assertIn("disabled", str(raised.exception).lower())
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        with self.assertRaises(ValueError) as stale:
            confirm_research_import(
                client_id=self.client_id,
                batch_id=batch_id,
                plan_fingerprint="0" * 64,
                actor=self.actor,
            )
        self.assertEqual(str(stale.exception), STALE_FINGERPRINT)

    def test_refresh_and_query_helpers_on_isolated_confirm(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        headers = [
            "Company Name",
            "Website",
            "City",
            "State",
            "Why Janco Fits",
            "Priority",
            "Potential Components",
            "Target Department",
            "Janco Target Market",
        ]
        content = _csv(
            headers,
            [[
                "Unique RI2 Co",
                "https://unique-ri2-co.example",
                "George",
                "IA",
                "Fits because of panels",
                "A - Iowa",
                "welded brackets",
                "Purchasing",
                "Agriculture / grain equipment",
            ]],
        )
        uploaded = upload_research_import(
            client_id=self.premier_id, actor=self.actor, filename="unique.csv", content=content
        )
        batch_id = uploaded["batch"]["batch_id"]
        save_mapping(
            self.premier_id,
            batch_id,
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        plan = dry_run_research_import(self.premier_id, batch_id, limit=5)
        self.assertFalse(plan["blocking"])
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch_id,
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["created_companies"], 1)
        with get_connection() as conn:
            brown_status = conn.execute(
                "SELECT COUNT(*) FROM client_company_relationships WHERE client_id=?",
                (self.brown_id,),
            ).fetchone()[0]
            self.assertGreaterEqual(int(brown_status), 1243)
            found = query_research_prospects(
                conn,
                self.premier_id,
                priority_code="A",
                target_market="Agriculture",
                potential_component="welded brackets",
                target_department="Purchasing",
                batch_id=batch_id,
            )
            self.assertEqual(len(found), 1)
            self.assertIn("panels", found[0]["why_client_fits"])
            first_id = found[0]["research_id"]
        content2 = _csv(
            headers,
            [[
                "Unique RI2 Co",
                "https://unique-ri2-co.example",
                "George",
                "IA",
                "Updated fit",
                "B - Neighboring state",
                "formed panels",
                "Engineering",
                "Agriculture / grain equipment",
            ]],
        )
        uploaded2 = upload_research_import(
            client_id=self.premier_id, actor=self.actor, filename="unique2.csv", content=content2
        )
        batch2 = uploaded2["batch"]["batch_id"]
        save_mapping(
            self.premier_id,
            batch2,
            actor=self.actor,
            mapping=suggest_mapping(uploaded2["batch"]["headers"]),
        )
        plan2 = dry_run_research_import(self.premier_id, batch2, limit=5)
        confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch2,
            plan_fingerprint=plan2["plan_fingerprint"],
            actor=self.actor,
        )
        with get_connection() as conn:
            versions = conn.execute(
                "SELECT id, is_current, why_client_fits FROM client_company_research WHERE company_id=(SELECT id FROM companies WHERE company_name='Unique RI2 Co') ORDER BY id"
            ).fetchall()
            self.assertEqual(len(versions), 2)
            self.assertEqual(int(versions[0]["is_current"]), 0)
            self.assertEqual(int(versions[1]["is_current"]), 1)
            self.assertIn("Updated", versions[1]["why_client_fits"])
            premier_ccr = conn.execute(
                "SELECT status FROM client_company_relationships WHERE client_id=? AND company_id=(SELECT id FROM companies WHERE company_name='Unique RI2 Co')",
                (self.premier_id,),
            ).fetchone()
            self.assertEqual(premier_ccr["status"], "New")

    def _upload_plan(self, client_id: int, filename: str, headers: list[str], rows: list[list[str]], limit: int = 25):
        uploaded = upload_research_import(
            client_id=client_id,
            actor=self.actor,
            filename=filename,
            content=_csv(headers, rows),
        )
        batch_id = uploaded["batch"]["batch_id"]
        save_mapping(
            client_id,
            batch_id,
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        plan = dry_run_research_import(client_id, batch_id, limit=limit)
        return batch_id, plan

    def test_urls_not_truncated_or_concatenated_into_notes(self) -> None:
        long_url = "https://www.example.com/products/welded-brackets?" + ("q=keep&" * 40) + "end=1"
        uploaded = upload_research_import(
            client_id=self.premier_id,
            actor=self.actor,
            filename="urls.csv",
            content=_csv(
                ["Company Name", "Website", "Product Source", "Qualification Notes"],
                [["URL Keep Co", "https://url-keep-co.example/path", long_url, "not a url dump"]],
            ),
        )
        mapping = suggest_mapping(uploaded["batch"]["headers"])
        self.assertEqual(mapping["product_source"], "Product Source")
        save_mapping(self.premier_id, uploaded["batch"]["batch_id"], actor=self.actor, mapping=mapping)
        plan = dry_run_research_import(self.premier_id, uploaded["batch"]["batch_id"], limit=5)
        row = plan["rows"][0]
        self.assertEqual(row["ri_class"], CLASS_NEW)
        self.assertEqual(row["mapped"].get("product_source"), long_url)
        self.assertTrue(
            any(s.get("source_role") == "PRODUCT" and s.get("source_url") == long_url for s in row["sources"]),
            msg=repr(row.get("sources")),
        )
        self.assertNotIn(long_url, row["qualification_notes"])

    def test_new_location_existing_ccr_and_master_proposals(self) -> None:
        with get_connection() as conn:
            rec = conn.execute(
                """
                SELECT c.id, c.company_name, c.website, c.legacy_phone, c.city, c.state, c.address,
                       ccr.id AS ccr_id, ccr.status, ccr.notes, ccr.assigned_user_id,
                       ccr.priority, ccr.next_action, ccr.follow_up_date
                FROM client_company_relationships ccr
                JOIN companies c ON c.id = ccr.company_id
                WHERE ccr.client_id=? AND TRIM(COALESCE(c.website,'')) != ''
                  AND TRIM(COALESCE(c.company_name,'')) != ''
                  AND TRIM(COALESCE(c.city,'')) != ''
                  AND lower(trim(c.city)) != 'des moines'
                ORDER BY ccr.id LIMIT 1
                """,
                (self.brown_id,),
            ).fetchone()
            self.assertIsNotNone(rec)
            company_id = int(rec["id"])
            brown_before = {
                "id": int(rec["ccr_id"]),
                "status": rec["status"],
                "notes": rec["notes"],
                "assigned_user_id": rec["assigned_user_id"],
                "priority": rec["priority"],
                "next_action": rec["next_action"],
                "follow_up_date": rec["follow_up_date"],
            }
            company_name = rec["company_name"]
            website = rec["website"]
        batch_id, plan = self._upload_plan(
            self.premier_id,
            "loc.csv",
            ["Company Name", "City", "State", "Website", "Phone", "Street Address"],
            [[
                company_name,
                "Des Moines",
                "IA",
                website,
                "(555) 010-9999",
                "100 Research Ave",
            ]],
        )
        row = plan["rows"][0]
        self.assertEqual(row["ri_class"], CLASS_NEW_LOCATION)
        self.assertTrue(row["blocking"])
        self.assertIn("TREAT_AS_NEW_LOCATION", row["allowed_resolutions"])
        phone_conflict = next(c for c in row["conflicts"] if c["field"] == "phone")
        self.assertIn(phone_conflict["class"], {CONFLICT_UPDATE, CONFLICT_FILL, CONFLICT_MANUAL})
        save_match_resolution(
            self.premier_id,
            batch_id,
            actor=self.actor,
            staged_row_id=row["row_id"],
            resolution_type="TREAT_AS_NEW_LOCATION",
            company_id=row["matched_company_id"],
        )
        resolved = dry_run_research_import(self.premier_id, batch_id, limit=5)
        self.assertNotEqual(resolved["plan_fingerprint"], plan["plan_fingerprint"])
        self.assertEqual(resolved["rows"][0]["ri_class"], CLASS_NEW_LOCATION)
        self.assertFalse(resolved["rows"][0]["blocking"])

        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch_id,
            plan_fingerprint=resolved["plan_fingerprint"],
            actor=self.actor,
        )
        with get_connection() as conn:
            brown_after = conn.execute(
                """
                SELECT status, notes, assigned_user_id, priority, next_action, follow_up_date
                FROM client_company_relationships WHERE id=?
                """,
                (int(brown_before["id"]),),
            ).fetchone()
            self.assertEqual(brown_after["status"], brown_before["status"])
            self.assertEqual(brown_after["notes"], brown_before["notes"])
            self.assertEqual(brown_after["assigned_user_id"], brown_before["assigned_user_id"])
            self.assertEqual(brown_after["priority"], brown_before["priority"])
            self.assertEqual(brown_after["next_action"], brown_before["next_action"])
            self.assertEqual(brown_after["follow_up_date"], brown_before["follow_up_date"])
            phone_now = conn.execute(
                "SELECT legacy_phone FROM companies WHERE id=?", (company_id,)
            ).fetchone()[0]
            self.assertNotEqual(blank(phone_now), "(555) 010-9999")
            contacts = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE company_id=?", (company_id,)
            ).fetchone()[0]
            self.assertGreaterEqual(int(contacts), 0)
            fake = conn.execute(
                """
                SELECT COUNT(*) FROM contacts
                WHERE company_id=? AND (
                    first_name LIKE '%Purchasing%' OR last_name LIKE '%Purchasing%'
                    OR first_name LIKE '%Engineering%' OR last_name LIKE '%Engineering%'
                )
                """,
                (company_id,),
            ).fetchone()[0]
            self.assertEqual(int(fake), 0)
            premier_ccr = conn.execute(
                """
                SELECT status FROM client_company_relationships
                WHERE client_id=? AND company_id=?
                """,
                (self.premier_id, company_id),
            ).fetchone()
            self.assertIsNotNone(premier_ccr)

    def test_existing_ccr_status_preserved_and_batch_caveat(self) -> None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT ccr.company_id, ccr.status, c.company_name, c.website, c.city, c.state, c.legacy_phone, c.address
                FROM client_company_relationships ccr
                JOIN companies c ON c.id = ccr.company_id
                WHERE ccr.client_id=? AND TRIM(COALESCE(c.website,'')) != ''
                ORDER BY ccr.id LIMIT 1
                """,
                (self.premier_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            prior_status = row["status"]
            name = row["company_name"]
        note = "Batch-level inferred fit. Confirm volumes separately."
        _batch_id, plan = self._upload_plan(
            self.premier_id,
            "caveat.csv",
            ["Company Name", "Website", "City", "State", "Phone", "Street Address", "Qualification Notes", "Priority"],
            [
                [name, row["website"], row["city"] or "", row["state"] or "", row["legacy_phone"] or "", row["address"] or "", note, "A - Iowa"],
                ["Caveat Twin Co", "https://caveat-twin.example", "Ames", "IA", "", "", note, "B - Neighboring state"],
            ],
        )
        self.assertTrue(plan["batch_caveat"])
        by_name = {r["company_name"]: r for r in plan["rows"]}
        existing = by_name[name]
        self.assertIn(existing["ri_class"], {"EXACT_EXISTING", "STRONG_EXISTING"})
        self.assertEqual(existing["relationship_action"], "relationship_already_exists")
        self.assertEqual(existing["planned_status"], prior_status)
        self.assertEqual(existing["status_action"], "preserve_existing_status")
        self.assertEqual(existing["research_priority_code"], "A")
        self.assertNotEqual(existing["research_priority_code"], existing["planned_status"])
        self.assertEqual(by_name["Caveat Twin Co"]["notes_action"], "no_notes_change")

    def test_manual_authority_and_fill_blank(self) -> None:
        from data_steward import SOURCE_MANUAL_ADMIN

        with get_connection() as conn:
            rec = conn.execute(
                """
                SELECT id, company_name, website, city, state, legacy_phone, address
                FROM companies WHERE company_name='Sudenga Industries' LIMIT 1
                """
            ).fetchone()
            cid = int(rec["id"])
            conn.execute(
                """
                INSERT INTO field_provenance_events (
                    entity_type, entity_id, field, old_value, new_value, source_type,
                    source_ref, changed_by_user_id, changed_at, action, reason
                ) VALUES ('company', ?, 'website', ?, ?, ?, '', 1, datetime('now'), 'edit', 'manual correction')
                """,
                (cid, rec["website"] or "", rec["website"] or "https://sudenga.com/", SOURCE_MANUAL_ADMIN),
            )
            conn.execute("UPDATE companies SET website='' WHERE company_name='Fill Blank RI2 Co'")
            conn.commit()
        _batch_id, plan = self._upload_plan(
            self.premier_id,
            "auth.csv",
            ["Company Name", "Website", "City", "State", "Phone", "Street Address"],
            [[
                "Sudenga Industries",
                "https://research-should-not-win.example",
                rec["city"] or "George",
                rec["state"] or "IA",
                rec["legacy_phone"] or "",
                rec["address"] or "",
            ]],
        )
        row = plan["rows"][0]
        website = next(c for c in row["conflicts"] if c["field"] == "website")
        self.assertEqual(website["class"], CONFLICT_MANUAL)
        self.assertTrue(any("MANUAL" in p for p in website.get("provenance") or []))

        uploaded = upload_research_import(
            client_id=self.premier_id,
            actor=self.actor,
            filename="fill.csv",
            content=_csv(
                ["Company Name", "Website"],
                [["Fill Blank RI2 Co", "https://fill-blank-ri2.example"]],
            ),
        )
        # First create the blank-website company via isolated confirm, then re-research.
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        save_mapping(
            self.premier_id,
            uploaded["batch"]["batch_id"],
            actor=self.actor,
            mapping=suggest_mapping(uploaded["batch"]["headers"]),
        )
        first = dry_run_research_import(self.premier_id, uploaded["batch"]["batch_id"], limit=5)
        self.assertFalse(first["blocking"])
        confirm_research_import(
            client_id=self.premier_id,
            batch_id=uploaded["batch"]["batch_id"],
            plan_fingerprint=first["plan_fingerprint"],
            actor=self.actor,
        )
        with get_connection() as conn:
            conn.execute("UPDATE companies SET website='' WHERE company_name='Fill Blank RI2 Co'")
            conn.commit()
        _bid2, fill_plan = self._upload_plan(
            self.premier_id,
            "fill2.csv",
            ["Company Name", "Website"],
            [["Fill Blank RI2 Co", "https://fill-blank-ri2.example"]],
        )
        fill_row = fill_plan["rows"][0]
        site = next(c for c in fill_row["conflicts"] if c["field"] == "website")
        self.assertEqual(site["class"], CONFLICT_FILL)

    def test_schema_refuses_live_and_is_not_in_migrate(self) -> None:
        import inspect
        import sqlite3

        from db import migrate_schema

        self.assertNotIn("research_import", inspect.getsource(migrate_schema))
        live = Path(PRODUCTION_DB_PATH).resolve()
        conn = sqlite3.connect(live.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            with self.assertRaises(LiveResearchSchemaForbidden):
                ensure_research_import_schema(conn)
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'research_%'"
                )
            }
            self.assertNotIn("research_import_batches", names)
            self.assertNotIn("client_company_research", names)
        finally:
            conn.close()

    def test_missing_client_and_production_confirm_flag(self) -> None:
        with get_connection() as conn:
            self.assertFalse(confirm_enabled(conn))
            self.assertFalse(is_production_db(conn))
        with self.assertRaises(ValueError):
            upload_research_import(
                client_id=0,
                actor=self.actor,
                filename="x.csv",
                content=_csv(["Company Name"], [["Nope"]]),
            )


def _xlsx(headers: list[str], rows: list[list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ResearchCustomImportRi3Tests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None)
        self.actor = _actor()
        with get_connection() as conn:
            self.premier_id = int(
                conn.execute("SELECT id FROM clients WHERE lower(code)='premier'").fetchone()[0]
            )
            self.brown_id = int(
                conn.execute("SELECT id FROM clients WHERE lower(code)='brown'").fetchone()[0]
            )
            ensure_research_import_schema(conn)
            conn.commit()

    def _upload(self, filename: str, headers: list[str], rows: list[list[str]], **kwargs):
        uploaded = upload_research_import(
            client_id=kwargs.pop("client_id", self.premier_id),
            actor=self.actor,
            filename=filename,
            content=_csv(headers, rows),
            **kwargs,
        )
        return uploaded["batch"]

    def test_arbitrary_csv_and_xlsx_headers(self) -> None:
        headers = ["Org Name", "Web Site"]
        rows = [["Synthetic Alloy LLC", "https://synthetic-alloy.example"]]
        batch = self._upload("unusual.csv", headers, rows, source_type="CUSTOM_REPORT")
        self.assertEqual(batch["source_type"], "CUSTOM_REPORT")
        self.assertEqual(batch["product_name"], "Research & Custom Prospect Import")
        mapping = suggest_mapping(headers)
        self.assertEqual(mapping["company_name"], "Org Name")
        self.assertEqual(mapping["website"], "Web Site")
        xlsx_up = upload_research_import(
            client_id=self.premier_id,
            actor=self.actor,
            filename="unusual.xlsx",
            content=_xlsx(headers, rows),
            source_type="TRADE_SHOW",
            source_label="Midwest Fab Expo",
        )
        self.assertEqual(xlsx_up["batch"]["headers"], headers)
        self.assertEqual(xlsx_up["batch"]["source_type"], "TRADE_SHOW")
        self.assertTrue(xlsx_up["batch"]["source_label"])

    def test_safe_automap_and_ambiguous_headers_not_risky(self) -> None:
        mapping = suggest_mapping(
            [
                "Business Name",
                "URL",
                "HQ Phone",
                "Street",
                "Status",
                "Owner",
                "Type",
                "Rating",
                "Score",
                "Category",
                "Comments",
                "Rep",
            ]
        )
        self.assertEqual(mapping["company_name"], "Business Name")
        self.assertEqual(mapping["website"], "URL")
        self.assertEqual(mapping["phone"], "HQ Phone")
        self.assertEqual(mapping["address"], "Street")
        self.assertNotIn("workflow_status", mapping)
        self.assertNotIn("imported_notes", mapping)
        self.assertNotIn("Status", mapping.values())
        self.assertNotIn("Comments", mapping.values())

    def test_ignore_does_not_block_or_store(self) -> None:
        headers = ["Org Name", "Internal Score", "Why Client Fits"]
        batch = self._upload(
            "ignore.csv",
            headers,
            [["Synthetic Ignore Co", "99", "Fits for fabricated tanks"]],
            source_type="CUSTOM_REPORT",
        )
        mapping = build_v3_mapping(
            headers,
            fields={"company_name": "Org Name", "why_client_fits": "Why Client Fits"},
            ignored=["Internal Score"],
        )
        saved = save_mapping(self.premier_id, batch["batch_id"], actor=self.actor, mapping=mapping)
        self.assertIn("Internal Score", saved["ignored_headers"])
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        row = plan["rows"][0]
        self.assertIn("Internal Score", row["ignored_fields"])
        self.assertNotIn("99", row["qualification_notes"])
        self.assertNotIn("99", json.dumps(row["attributes"]))
        self.assertFalse(plan["blocking"])

    def test_custom_attributes_governance_and_types(self) -> None:
        self.assertEqual(normalize_attribute_key("Press Tonnage"), "press_tonnage")
        self.assertEqual(normalize_attribute_key("press_tonnage"), "press_tonnage")
        typed = parse_typed_value("600", "NUMBER")
        self.assertEqual(typed["numeric_value"], 600.0)
        self.assertEqual(typed["original_value"], "600")
        headers = ["Org Name", "Press Tonnage", "Current Supplier", "Annual Steel Usage"]
        batch = self._upload(
            "attrs.csv",
            headers,
            [["Synthetic Press Co", "600", "ABC Steel", "1200"]],
            source_type="CUSTOM_REPORT",
        )
        mapping = build_v3_mapping(
            headers,
            fields={"company_name": "Org Name"},
            custom=[
                {"header": "Press Tonnage", "label": "Press Tonnage", "key": "press_tonnage", "value_type": "NUMBER"},
                {"header": "Current Supplier", "label": "Current Supplier", "key": "current_supplier", "value_type": "TEXT"},
                {"header": "Annual Steel Usage", "label": "Annual Steel Usage", "key": "annual_steel_usage", "value_type": "NUMBER"},
            ],
        )
        save_mapping(self.premier_id, batch["batch_id"], actor=self.actor, mapping=mapping)
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        keys = {item["key"] for item in plan["rows"][0]["custom_attributes"]}
        self.assertEqual(keys, {"press_tonnage", "current_supplier", "annual_steel_usage"})
        press = next(item for item in plan["rows"][0]["custom_attributes"] if item["key"] == "press_tonnage")
        self.assertEqual(press["numeric_value"], 600.0)

    def test_contact_mapping_and_department_is_not_contact(self) -> None:
        self.assertTrue(is_department_only_name("Purchasing"))
        self.assertFalse(is_department_only_name("Riley Synthetic"))
        named = self._upload(
            "contacts.csv",
            ["Org Name", "First Name", "Last Name", "Email"],
            [["Synthetic Contact Co", "Riley", "Synthetic", "riley@synthetic-contact.example"]],
            source_type="CLIENT_PROVIDED",
        )
        save_mapping(
            self.premier_id,
            named["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                named["headers"],
                fields={
                    "company_name": "Org Name",
                    "contact_first_name": "First Name",
                    "contact_last_name": "Last Name",
                    "contact_email": "Email",
                },
            ),
        )
        plan = dry_run_research_import(self.premier_id, named["batch_id"], limit=5)
        self.assertEqual(plan["counts"]["contacts_present"], 1)
        self.assertEqual(plan["rows"][0]["contacts"][0]["full_name"], "Riley Synthetic")
        dept = self._upload(
            "dept.csv",
            ["Org Name", "Contact Name"],
            [["Synthetic Dept Co", "Purchasing"]],
            source_type="CLIENT_PROVIDED",
        )
        save_mapping(
            self.premier_id,
            dept["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                dept["headers"],
                fields={"company_name": "Org Name", "contact_full_name": "Contact Name"},
            ),
        )
        dept_plan = dry_run_research_import(self.premier_id, dept["batch_id"], limit=5)
        self.assertEqual(dept_plan["counts"]["contacts_present"], 0)
        self.assertEqual(dept_plan["rows"][0]["contacts"], [])

    def test_notes_only_when_explicitly_mapped(self) -> None:
        headers = ["Org Name", "Comments"]
        batch = self._upload(
            "notes.csv",
            headers,
            [["Synthetic Notes Co", "Call after the show"]],
            source_type="SALESPERSON_PROVIDED",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(headers, fields={"company_name": "Org Name"}, ignored=["Comments"]),
        )
        ignored_plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        self.assertFalse(ignored_plan["rows"][0]["notes_explicit"])
        explicit = self._upload(
            "notes2.csv",
            headers,
            [["Synthetic Notes Co 2", "Call after the show"]],
            source_type="SALESPERSON_PROVIDED",
        )
        save_mapping(
            self.premier_id,
            explicit["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={"company_name": "Org Name", "imported_notes": "Comments"},
            ),
        )
        explicit_plan = dry_run_research_import(self.premier_id, explicit["batch_id"], limit=5)
        self.assertEqual(explicit_plan["rows"][0]["notes_explicit"], "Call after the show")
        self.assertEqual(explicit_plan["rows"][0]["notes_action"], "set_imported_notes")

    def test_source_types_and_workflow_sensitive_mapping(self) -> None:
        mapping = build_v3_mapping(
            ["Org Name", "Status"],
            fields={"company_name": "Org Name", "workflow_status": "Status"},
        )
        self.assertEqual(mapping_safety_class(mapping["fields"]), "workflow_sensitive")
        batch = self._upload(
            "wf.csv",
            ["Org Name", "Status"],
            [["Synthetic Workflow Co", "Hot"]],
            source_type="CUSTOM_REPORT",
        )
        save_mapping(self.premier_id, batch["batch_id"], actor=self.actor, mapping=mapping)
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        self.assertEqual(plan["rows"][0]["workflow_fields"]["workflow_status"], "Hot")
        self.assertTrue(plan["rows"][0]["workflow_preview_only"])

    def test_saved_mapping_reuse_and_changed_headers(self) -> None:
        headers = ["Account Name", "Home Page"]
        first = self._upload(
            "template-a.csv",
            headers,
            [["Synthetic Template Co", "https://synthetic-template.example"]],
            source_type="CUSTOM_REPORT",
            source_label="Janco Monthly Prospect Report",
        )
        mapping = suggest_mapping(headers)
        save_mapping(
            self.premier_id,
            first["batch_id"],
            actor=self.actor,
            mapping=mapping,
            save_as_template="Janco Monthly Prospect Report",
            source_type="CUSTOM_REPORT",
        )
        second = self._upload(
            "template-b.csv",
            headers,
            [["Synthetic Template Co 2", "https://synthetic-template-2.example"]],
            source_type="CUSTOM_REPORT",
        )
        hint = second["mapping_template_suggestion"]
        self.assertEqual(hint.get("template_name"), "Janco Monthly Prospect Report")
        self.assertEqual(hint.get("status"), "suggested")
        self.assertFalse(hint.get("applied"))
        self.assertFalse(hint.get("review_required"))
        changed = self._upload(
            "template-c.csv",
            ["Account Name", "Home Page", "Extra Column"],
            [["Synthetic Template Co 3", "https://synthetic-template-3.example", "x"]],
            source_type="CUSTOM_REPORT",
        )
        changed_hint = changed["mapping_template_suggestion"]
        self.assertTrue(changed_hint.get("review_required"))
        self.assertIn("Extra Column", changed_hint.get("header_compatibility", {}).get("new_headers", []))

    def test_workflow_mapping_does_not_leak_across_clients(self) -> None:
        headers = ["Firm", "Status"]
        premier = self._upload(
            "wf-premier.csv",
            headers,
            [["Synthetic Premier Firm", "Customer"]],
            source_type="CUSTOM_REPORT",
            client_id=self.premier_id,
        )
        mapping = build_v3_mapping(
            headers,
            fields={"company_name": "Firm", "workflow_status": "Status"},
        )
        save_mapping(
            self.premier_id,
            premier["batch_id"],
            actor=self.actor,
            mapping=mapping,
            save_as_template="Premier Status Report",
            source_type="CUSTOM_REPORT",
        )
        brown = self._upload(
            "wf-brown.csv",
            headers,
            [["Synthetic Brown Firm", "Prospect"]],
            source_type="CUSTOM_REPORT",
            client_id=self.brown_id,
        )
        hint = brown["mapping_template_suggestion"] or {}
        self.assertNotEqual(hint.get("template_name"), "Premier Status Report")

    def test_isolated_confirm_custom_contact_lineage_and_no_cross_client(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        headers = ["Org Name", "Website", "First Name", "Last Name", "Press Tonnage"]
        batch = self._upload(
            "confirm-custom.csv",
            headers,
            [[
                "Unique RI3 Custom Co",
                "https://unique-ri3-custom.example",
                "Riley",
                "Synthetic",
                "600",
            ]],
            source_type="CUSTOM_REPORT",
            source_label="Synthetic custom report",
            source_supplied_by="Fixture",
        )
        mapping = build_v3_mapping(
            headers,
            fields={
                "company_name": "Org Name",
                "website": "Website",
                "contact_first_name": "First Name",
                "contact_last_name": "Last Name",
            },
            custom=[{"header": "Press Tonnage", "label": "Press Tonnage", "key": "press_tonnage", "value_type": "NUMBER"}],
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=mapping,
            save_as_template="RI3 Custom Fixture",
            source_type="CUSTOM_REPORT",
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        self.assertFalse(plan["blocking"])
        self.assertFalse(plan["confirm_allowed"])
        self.assertFalse(plan["production_confirm_enabled"])
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["created_companies"], 1)
        self.assertEqual(result["created_contacts"], 1)
        self.assertEqual(result["created_custom_attributes"], 1)
        self.assertEqual(result["workflow_fields_written"], 0)
        self.assertEqual(result["master_proposals_applied"], 0)
        with get_connection() as conn:
            brown_ccr = conn.execute(
                "SELECT COUNT(*) FROM client_company_relationships WHERE client_id=?",
                (self.brown_id,),
            ).fetchone()[0]
            self.assertGreaterEqual(int(brown_ccr), 1243)
            found = query_custom_attribute(
                conn,
                self.premier_id,
                "press_tonnage",
                min_numeric=500,
                batch_id=batch["batch_id"],
            )
            self.assertEqual(len(found), 1)
            sourced = query_research_by_source(
                conn,
                self.premier_id,
                source_type="CUSTOM_REPORT",
                batch_id=batch["batch_id"],
            )
            self.assertEqual(len(sourced), 1)
            self.assertEqual(sourced[0]["original_filename"], "confirm-custom.csv")
            self.assertTrue(sourced[0]["sha256"])
            other = query_research_prospects(conn, self.brown_id, batch_id=batch["batch_id"])
            self.assertEqual(other, [])

    def test_production_confirm_still_disabled_for_custom(self) -> None:
        batch = self._upload(
            "noconfirm.csv",
            ["Org Name"],
            [["Synthetic No Confirm Co"]],
            source_type="OTHER",
            source_label="Salesperson workbook",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=suggest_mapping(batch["headers"]),
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        with self.assertRaises(PermissionError) as raised:
            confirm_research_import(
                client_id=self.premier_id,
                batch_id=batch["batch_id"],
                plan_fingerprint=plan["plan_fingerprint"],
                actor=self.actor,
            )
        self.assertIn("disabled", str(raised.exception).lower())
        self.assertFalse(plan["production_confirm_enabled"])

    def _seed_company_contact(
        self,
        *,
        name: str,
        website: str = "",
        first: str = "",
        last: str = "",
        email: str = "",
        phone: str = "",
        title: str = "",
    ) -> tuple[int, int | None]:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, website, legacy_phone, source)
                VALUES (?, ?, ?, ?, 'TEST')
                """,
                (f"RI3B-{name[:24]}", name, website, phone, ),
            )
            company_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            contact_id = None
            if first or last:
                cur = conn.execute(
                    """
                    INSERT INTO contacts (
                        company_id, external_record_no, first_name, last_name, title, email, phone, source_row_index
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (company_id, f"RI3B-C-{company_id}", first, last, title, email, phone),
                )
                contact_id = int(cur.lastrowid)
                if phone:
                    upsert_contact_phone_keys(conn, contact_id, phone, "")
            conn.commit()
        return company_id, contact_id

    def test_field_policy_matrix_and_source_trust(self) -> None:
        self.assertEqual(FIELD_POLICY["workflow_status"]["new_company"], POLICY_E)
        self.assertEqual(FIELD_POLICY["imported_notes"]["category"], "NOTES")
        self.assertIn(MASTER_KEEP, master_actions_for("MANUAL_AUTHORITY_CONFLICT"))
        self.assertNotIn("ACCEPT_INCOMING", master_actions_for("MANUAL_AUTHORITY_CONFLICT"))
        self.assertEqual(source_trust("CHATGPT_DEEP_RESEARCH"), "research_informational")
        self.assertEqual(source_trust("CLIENT_PROVIDED"), "client_provided")
        self.assertEqual(source_trust("CUSTOM_REPORT"), "legacy_custom")
        self.assertFalse(source_is_authoritative("CLIENT_PROVIDED"))
        self.assertFalse(source_is_authoritative("INTERNAL_RESEARCH"))

    def test_new_named_contact_and_extension(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        headers = ["Org Name", "Website", "Contact Name", "Title", "Email", "Phone", "Ext"]
        batch = self._upload(
            "ri3b-new-contact.csv",
            headers,
            [[
                "Unique RI3B New Contact Co",
                "https://unique-ri3b-new-contact.example",
                "Jordan Synthetic",
                "Buyer",
                "jordan@unique-ri3b-new-contact.example",
                "515-555-0101",
                "204",
            ]],
            source_type="CUSTOM_REPORT",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={
                    "company_name": "Org Name",
                    "website": "Website",
                    "contact_full_name": "Contact Name",
                    "contact_title": "Title",
                    "contact_email": "Email",
                    "contact_phone": "Phone",
                    "contact_phone_extension": "Ext",
                },
            ),
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        contact = plan["rows"][0]["contacts"][0]
        self.assertEqual(contact["contact_class"], CONTACT_NEW)
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["created_contacts"], 1)
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT first_name, last_name, email, phone, phone_extension
                FROM contacts WHERE email='jordan@unique-ri3b-new-contact.example'
                """
            ).fetchone()
            self.assertEqual(row["first_name"], "Jordan")
            self.assertEqual(row["phone_extension"], "204")

    def test_exact_email_and_phone_reuse(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        self._seed_company_contact(
            name="Unique RI3B Email Co",
            website="https://unique-ri3b-email.example",
            first="Casey",
            last="Synthetic",
            email="casey@unique-ri3b-email.example",
            phone="5155550199",
        )
        headers = ["Org Name", "Website", "Contact Name", "Email", "Phone"]
        email_batch = self._upload(
            "ri3b-email.csv",
            headers,
            [["Unique RI3B Email Co", "https://unique-ri3b-email.example", "Casey Synthetic", "casey@unique-ri3b-email.example", ""]],
            source_type="CLIENT_PROVIDED",
        )
        mapping = build_v3_mapping(
            headers,
            fields={
                "company_name": "Org Name",
                "website": "Website",
                "contact_full_name": "Contact Name",
                "contact_email": "Email",
                "contact_phone": "Phone",
            },
        )
        save_mapping(self.premier_id, email_batch["batch_id"], actor=self.actor, mapping=mapping)
        email_plan = dry_run_research_import(self.premier_id, email_batch["batch_id"], limit=5)
        self.assertEqual(email_plan["rows"][0]["contacts"][0]["contact_class"], CONTACT_EXACT)
        email_result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=email_batch["batch_id"],
            plan_fingerprint=email_plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(email_result["created_contacts"], 0)
        self.assertEqual(email_result["reused_contacts"], 1)
        phone_batch = self._upload(
            "ri3b-phone.csv",
            headers,
            [["Unique RI3B Email Co", "https://unique-ri3b-email.example", "Casey Synthetic", "", "5155550199"]],
            source_type="CLIENT_PROVIDED",
        )
        save_mapping(self.premier_id, phone_batch["batch_id"], actor=self.actor, mapping=mapping)
        phone_plan = dry_run_research_import(self.premier_id, phone_batch["batch_id"], limit=5)
        self.assertEqual(phone_plan["rows"][0]["contacts"][0]["contact_class"], CONTACT_EXACT)
        phone_result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=phone_batch["batch_id"],
            plan_fingerprint=phone_plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(phone_result["created_contacts"], 0)

    def test_last_seven_possible_and_resolution(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        self._seed_company_contact(
            name="Unique RI3B Last7 Co",
            website="https://unique-ri3b-last7.example",
            first="Morgan",
            last="Synthetic",
            phone="5155550188",
        )
        headers = ["Org Name", "Website", "Contact Name", "Phone"]
        batch = self._upload(
            "ri3b-last7.csv",
            headers,
            [["Unique RI3B Last7 Co", "https://unique-ri3b-last7.example", "Taylor Synthetic", "9995550188"]],
            source_type="CUSTOM_REPORT",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={
                    "company_name": "Org Name",
                    "website": "Website",
                    "contact_full_name": "Contact Name",
                    "contact_phone": "Phone",
                },
            ),
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        contact = plan["rows"][0]["contacts"][0]
        self.assertEqual(contact["contact_class"], CONTACT_POSSIBLE)
        self.assertTrue(contact["blocking"])
        stale = plan["plan_fingerprint"]
        save_contact_resolution(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            staged_row_id=plan["rows"][0]["row_id"],
            resolution_type=RES_CREATE_CONTACT,
        )
        with self.assertRaises(ValueError) as raised:
            confirm_research_import(
                client_id=self.premier_id,
                batch_id=batch["batch_id"],
                plan_fingerprint=stale,
                actor=self.actor,
            )
        self.assertIn("stale", str(raised.exception).lower())
        fresh = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        self.assertFalse(fresh["rows"][0]["contacts"][0]["blocking"])
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=fresh["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["created_contacts"], 1)

    def test_same_name_same_and_different_company(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        self._seed_company_contact(
            name="Unique RI3B SameName Co",
            website="https://unique-ri3b-samename.example",
            first="Alex",
            last="Synthetic",
        )
        self._seed_company_contact(
            name="Unique RI3B OtherName Co",
            website="https://unique-ri3b-othername.example",
            first="Alex",
            last="Synthetic",
        )
        headers = ["Org Name", "Website", "Contact Name"]
        same = self._upload(
            "ri3b-same-name.csv",
            headers,
            [["Unique RI3B SameName Co", "https://unique-ri3b-samename.example", "Alex Synthetic"]],
            source_type="INTERNAL_RESEARCH",
        )
        mapping = build_v3_mapping(
            headers,
            fields={"company_name": "Org Name", "website": "Website", "contact_full_name": "Contact Name"},
        )
        save_mapping(self.premier_id, same["batch_id"], actor=self.actor, mapping=mapping)
        same_plan = dry_run_research_import(self.premier_id, same["batch_id"], limit=5)
        self.assertEqual(same_plan["rows"][0]["contacts"][0]["contact_class"], CONTACT_STRONG)
        other = self._upload(
            "ri3b-other-name.csv",
            headers,
            [["Unique RI3B BrandNew Co", "https://unique-ri3b-brandnew.example", "Alex Synthetic"]],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(self.premier_id, other["batch_id"], actor=self.actor, mapping=mapping)
        other_plan = dry_run_research_import(self.premier_id, other["batch_id"], limit=5)
        other_contact = other_plan["rows"][0]["contacts"][0]
        self.assertEqual(other_contact["contact_class"], CONTACT_POSSIBLE)
        self.assertTrue(other_contact["cross_company"])
        self.assertNotIn(RES_USE_CONTACT, other_contact["allowed_resolutions"])
        save_contact_resolution(
            self.premier_id,
            other["batch_id"],
            actor=self.actor,
            staged_row_id=other_plan["rows"][0]["row_id"],
            resolution_type=RES_USE_CONTACT,
        )
        blocked = dry_run_research_import(self.premier_id, other["batch_id"], limit=5)
        self.assertTrue(blocked["rows"][0]["contacts"][0]["blocking"])
        save_contact_resolution(
            self.premier_id,
            other["batch_id"],
            actor=self.actor,
            staged_row_id=other_plan["rows"][0]["row_id"],
            resolution_type=RES_CREATE_CONTACT,
        )
        fresh = dry_run_research_import(self.premier_id, other["batch_id"], limit=5)
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=other["batch_id"],
            plan_fingerprint=fresh["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["created_contacts"], 1)

    def test_title_only_department_two_contacts_and_notes(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        headers = ["Org Name", "Website", "Contact Name", "Title", "Email", "Phone", "Comments"]
        batch = self._upload(
            "ri3b-mix.csv",
            headers,
            [
                ["Unique RI3B Mix Co", "https://unique-ri3b-mix.example", "Quinn Mixsynthetic", "Plant Manager", "", "", "Call after the show"],
                ["Unique RI3B Mix Co", "https://unique-ri3b-mix.example", "Drew Mixsynthetic", "Buyer", "drew@unique-ri3b-mix.example", "", "Call after the show"],
                ["Unique RI3B Mix Co", "https://unique-ri3b-mix.example", "Purchasing", "", "", "", ""],
            ],
            source_type="SALESPERSON_PROVIDED",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={
                    "company_name": "Org Name",
                    "website": "Website",
                    "contact_full_name": "Contact Name",
                    "contact_title": "Title",
                    "contact_email": "Email",
                    "contact_phone": "Phone",
                    "imported_notes": "Comments",
                },
            ),
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=10)
        classes = [row["contacts"][0]["contact_class"] if row["contacts"] else None for row in plan["rows"]]
        self.assertEqual(classes[0], CONTACT_NEW)
        self.assertEqual(classes[1], CONTACT_NEW)
        self.assertIsNone(classes[2])
        self.assertEqual(plan["rows"][0]["notes_action"], "set_imported_notes")
        self.assertEqual(plan["rows"][0]["notes_explicit"], "Call after the show")
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["created_companies"], 1)
        self.assertEqual(result["created_ccrs"], 1)
        self.assertEqual(result["created_contacts"], 2)
        self.assertGreaterEqual(result.get("notes_written", 0), 1, result)
        with get_connection() as conn:
            notes = conn.execute(
                """
                SELECT notes FROM client_company_relationships
                WHERE client_id=? AND company_id=(
                    SELECT id FROM companies WHERE company_name='Unique RI3B Mix Co'
                )
                """,
                (self.premier_id,),
            ).fetchone()[0]
            self.assertEqual(notes.count("Call after the show"), 1)
        second = self._upload(
            "ri3b-mix-2.csv",
            headers,
            [
                ["Unique RI3B Mix Co", "https://unique-ri3b-mix.example", "Quinn Mixsynthetic", "Plant Manager", "", "", "Call after the show"],
            ],
            source_type="SALESPERSON_PROVIDED",
        )
        save_mapping(
            self.premier_id,
            second["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={
                    "company_name": "Org Name",
                    "website": "Website",
                    "contact_full_name": "Contact Name",
                    "contact_title": "Title",
                    "contact_email": "Email",
                    "contact_phone": "Phone",
                    "imported_notes": "Comments",
                },
            ),
        )
        second_plan = dry_run_research_import(self.premier_id, second["batch_id"], limit=5)
        self.assertEqual(second_plan["rows"][0]["contacts"][0]["contact_class"], CONTACT_STRONG)
        self.assertEqual(second_plan["rows"][0]["notes_action"], "imported_notes_already_present")
        second_result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=second["batch_id"],
            plan_fingerprint=second_plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(second_result["created_companies"], 0)
        self.assertEqual(second_result["created_ccrs"], 0)
        self.assertEqual(second_result["created_contacts"], 0)

    def test_workflow_preserve_master_manual_and_notes_mapping_safety(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        company_id, _ = self._seed_company_contact(
            name="Unique RI3B Workflow Co",
            website="https://unique-ri3b-workflow.example",
            phone="5155550177",
        )
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO client_company_relationships (client_id, company_id, status, notes)
                VALUES (?, ?, 'Customer', 'Keep me')
                """,
                (self.premier_id, company_id),
            )
            conn.commit()
        headers = ["Org Name", "Website", "Status", "Comments"]
        batch = self._upload(
            "ri3b-workflow.csv",
            headers,
            [["Unique RI3B Workflow Co", "https://unique-ri3b-workflow.example", "Hot", "New note"]],
            source_type="CUSTOM_REPORT",
        )
        mapping = build_v3_mapping(
            headers,
            fields={
                "company_name": "Org Name",
                "website": "Website",
                "workflow_status": "Status",
                "imported_notes": "Comments",
            },
        )
        self.assertEqual(mapping_safety_class(mapping["fields"]), "workflow_sensitive")
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=mapping,
            save_as_template="Premier Workflow Notes",
            source_type="CUSTOM_REPORT",
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        self.assertIn(plan["rows"][0]["ri_class"], {"EXACT_EXISTING", "STRONG_EXISTING"})
        self.assertIsNotNone(plan["rows"][0]["this_client_ccr_id"])
        self.assertEqual(plan["rows"][0]["status_action"], "preserve_existing_status")
        self.assertEqual(plan["rows"][0]["notes_action"], "append_imported_notes")
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["workflow_fields_written"], 0)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT status, notes FROM client_company_relationships WHERE client_id=? AND company_id=?",
                (self.premier_id, company_id),
            ).fetchone()
            self.assertEqual(row["status"], "Customer")
            self.assertIn("Keep me", row["notes"])
            self.assertIn("New note", row["notes"])
        notes_only = build_v3_mapping(
            ["Org Name", "Comments"],
            fields={"company_name": "Org Name", "imported_notes": "Comments"},
        )
        self.assertEqual(mapping_safety_class(notes_only["fields"]), "notes_sensitive")
        brown = self._upload(
            "ri3b-wf-brown.csv",
            headers,
            [["Synthetic Brown Workflow", "https://brown-wf.example", "Prospect", "x"]],
            source_type="CUSTOM_REPORT",
            client_id=self.brown_id,
        )
        hint = brown["mapping_template_suggestion"] or {}
        self.assertNotEqual(hint.get("template_name"), "Premier Workflow Notes")

    def test_custom_attribute_versioning_and_master_resolution_fingerprint(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))
        headers = ["Org Name", "Website", "Press Tonnage"]
        first = self._upload(
            "ri3b-attr1.csv",
            headers,
            [["Unique RI3B Attr Co", "https://unique-ri3b-attr.example", "600"]],
            source_type="CUSTOM_REPORT",
        )
        mapping = build_v3_mapping(
            headers,
            fields={"company_name": "Org Name", "website": "Website"},
            custom=[{"header": "Press Tonnage", "label": "Press Tonnage", "key": "press_tonnage", "value_type": "NUMBER"}],
        )
        save_mapping(self.premier_id, first["batch_id"], actor=self.actor, mapping=mapping)
        plan1 = dry_run_research_import(self.premier_id, first["batch_id"], limit=5)
        confirm_research_import(
            client_id=self.premier_id,
            batch_id=first["batch_id"],
            plan_fingerprint=plan1["plan_fingerprint"],
            actor=self.actor,
        )
        second = self._upload(
            "ri3b-attr2.csv",
            headers,
            [["Unique RI3B Attr Co", "https://unique-ri3b-attr.example", "800"]],
            source_type="CUSTOM_REPORT",
        )
        save_mapping(self.premier_id, second["batch_id"], actor=self.actor, mapping=mapping)
        plan2 = dry_run_research_import(self.premier_id, second["batch_id"], limit=5)
        confirm_research_import(
            client_id=self.premier_id,
            batch_id=second["batch_id"],
            plan_fingerprint=plan2["plan_fingerprint"],
            actor=self.actor,
        )
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT attribute_value, is_current FROM research_attributes
                WHERE attribute_type='press_tonnage' AND company_id=(
                    SELECT id FROM companies WHERE company_name='Unique RI3B Attr Co'
                )
                ORDER BY id
                """
            ).fetchall()
            values = [(str(r["attribute_value"]), int(r["is_current"])) for r in rows]
            self.assertIn(("600", 0), values)
            self.assertIn(("800", 1), values)
        seeded_id, _ = self._seed_company_contact(
            name="Unique RI3B Master Co",
            website="https://unique-ri3b-master.example",
        )
        master_batch = self._upload(
            "ri3b-master.csv",
            ["Org Name", "Website"],
            [["Unique RI3B Master Co", "https://other-ri3b-master.example"]],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(
            self.premier_id,
            master_batch["batch_id"],
            actor=self.actor,
            mapping=suggest_mapping(master_batch["headers"]),
        )
        master_plan = dry_run_research_import(self.premier_id, master_batch["batch_id"], limit=5)
        website_conflict = next(c for c in master_plan["rows"][0]["conflicts"] if c["field"] == "website")
        self.assertIn(MASTER_KEEP, website_conflict["allowed_master_actions"])
        stale = master_plan["plan_fingerprint"]
        save_master_resolution(
            self.premier_id,
            master_batch["batch_id"],
            actor=self.actor,
            staged_row_id=master_plan["rows"][0]["row_id"],
            field="website",
            resolution_type=MASTER_KEEP,
        )
        with self.assertRaises(ValueError):
            confirm_research_import(
                client_id=self.premier_id,
                batch_id=master_batch["batch_id"],
                plan_fingerprint=stale,
                actor=self.actor,
            )
        self.assertTrue(seeded_id)


class ResearchImportRi4Tests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None)
        self.actor = _actor()
        with get_connection() as conn:
            self.premier_id = int(
                conn.execute("SELECT id FROM clients WHERE lower(code)='premier'").fetchone()[0]
            )
            self.brown_id = int(
                conn.execute("SELECT id FROM clients WHERE lower(code)='brown'").fetchone()[0]
            )
            ensure_research_import_schema(conn)
            conn.commit()

    def _enable(self) -> None:
        os.environ["NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"] = "1"
        self.addCleanup(lambda: os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None))

    def _use_existing_matches(self, batch_id: int, client_id: int | None = None) -> dict:
        cid = int(client_id or self.premier_id)
        plan = dry_run_research_import(cid, batch_id, limit=50)
        for row in plan["rows"]:
            if row["ri_class"] not in {CLASS_POSSIBLE, CLASS_AMBIGUOUS}:
                continue
            matched = row.get("matched_company_id")
            same_name = bool(matched) and blank(row.get("company_name")).casefold() == blank(
                row.get("matched_company_name")
            ).casefold()
            save_match_resolution(
                cid,
                batch_id,
                actor=self.actor,
                staged_row_id=row["row_id"],
                resolution_type="USE_EXISTING" if same_name else "CREATE_NEW",
                company_id=int(matched) if same_name else None,
            )
        return dry_run_research_import(cid, batch_id, limit=50)

    def _upload(self, filename: str, headers: list[str], rows: list[list[str]], **kwargs):
        uploaded = upload_research_import(
            client_id=kwargs.pop("client_id", self.premier_id),
            actor=self.actor,
            filename=filename,
            content=_csv(headers, rows),
            **kwargs,
        )
        return uploaded["batch"]

    def _counts(self) -> dict[str, int]:
        with get_connection() as conn:
            return {
                "companies": int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]),
                "contacts": int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]),
                "ccr": int(conn.execute("SELECT COUNT(*) FROM client_company_relationships").fetchone()[0]),
                "locations": int(conn.execute("SELECT COUNT(*) FROM company_locations").fetchone()[0]),
                "research": int(conn.execute("SELECT COUNT(*) FROM client_company_research").fetchone()[0]),
                "notes": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM client_company_relationships WHERE TRIM(COALESCE(notes,'')) != ''"
                    ).fetchone()[0]
                ),
                "provenance": int(conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0]),
            }

    def test_atomic_rollback_injection(self) -> None:
        self._enable()
        headers = ["Org Name", "Website", "Contact Name", "Comments", "Market"]
        mapping = build_v3_mapping(
            headers,
            fields={
                "company_name": "Org Name",
                "website": "Website",
                "contact_full_name": "Contact Name",
                "imported_notes": "Comments",
                "target_market": "Market",
            },
        )
        for stage in (
            "after_company",
            "after_ccr",
            "after_contact",
            "after_note",
            "after_research",
            "after_attributes",
        ):
            before = self._counts()
            batch = self._upload(
                f"ri4-rollback-{stage}.csv",
                headers,
                [[
                    f"Unique RI4 Rollback {stage} Co",
                    f"https://unique-ri4-rollback-{stage}.example",
                    "Riley Rollback",
                    "Rollback note",
                    "Ag",
                ]],
                source_type="INTERNAL_RESEARCH",
            )
            save_mapping(self.premier_id, batch["batch_id"], actor=self.actor, mapping=mapping)
            plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
            with inject_confirm_failure(stage):
                with self.assertRaises(RuntimeError):
                    confirm_research_import(
                        client_id=self.premier_id,
                        batch_id=batch["batch_id"],
                        plan_fingerprint=plan["plan_fingerprint"],
                        actor=self.actor,
                    )
            self.assertEqual(self._counts(), before, stage)

    def test_fill_blank_accept_keep_manual_and_stale(self) -> None:
        self._enable()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, website, source)
                VALUES ('RI4-FILL', 'Unique RI4 Master Co', '', 'TEST')
                """
            )
            company_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, website, source)
                VALUES ('RI4-UPD', 'Unique RI4 Update Co', 'https://old-ri4-update.example', 'TEST')
                """
            )
            update_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, website, source)
                VALUES ('RI4-MAN', 'Unique RI4 Manual Co', 'https://manual-ri4.example', 'TEST')
                """
            )
            manual_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            record_provenance(
                conn,
                entity_type="company",
                entity_id=manual_id,
                field="website",
                old_value="",
                new_value="https://manual-ri4.example",
                source_type=SOURCE_MANUAL_ADMIN,
                action="UPDATE",
                trusted=True,
            )
            conn.commit()
        headers = ["Org Name", "Website"]
        batch = self._upload(
            "ri4-master.csv",
            headers,
            [
                ["Unique RI4 Master Co", "https://filled-ri4-master.example"],
                ["Unique RI4 Update Co", "https://new-ri4-update.example"],
                ["Unique RI4 Manual Co", "https://should-not-win.example"],
            ],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(headers, fields={"company_name": "Org Name", "website": "Website"}),
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=10)
        by_name = {row["company_name"]: row for row in plan["rows"]}
        fill_row = by_name["Unique RI4 Master Co"]
        upd_row = by_name["Unique RI4 Update Co"]
        man_row = by_name["Unique RI4 Manual Co"]
        fill = next(c for c in fill_row["conflicts"] if c["field"] == "website")
        self.assertEqual(fill["class"], CONFLICT_FILL)
        upd = next(c for c in upd_row["conflicts"] if c["field"] == "website")
        self.assertEqual(upd["class"], CONFLICT_UPDATE)
        man = next(c for c in man_row["conflicts"] if c["field"] == "website")
        self.assertEqual(man["class"], CONFLICT_MANUAL)
        save_master_resolution(
            self.premier_id, batch["batch_id"], actor=self.actor,
            staged_row_id=fill_row["row_id"], field="website", resolution_type=MASTER_FILL,
        )
        save_master_resolution(
            self.premier_id, batch["batch_id"], actor=self.actor,
            staged_row_id=upd_row["row_id"], field="website", resolution_type=MASTER_ACCEPT_INCOMING,
        )
        save_master_resolution(
            self.premier_id, batch["batch_id"], actor=self.actor,
            staged_row_id=man_row["row_id"], field="website", resolution_type=MASTER_KEEP,
        )
        fresh = self._use_existing_matches(batch["batch_id"])
        with self.assertRaises(ValueError) as stale:
            confirm_research_import(
                client_id=self.premier_id,
                batch_id=batch["batch_id"],
                plan_fingerprint=plan["plan_fingerprint"],
                actor=self.actor,
            )
        self.assertEqual(str(stale.exception), STALE_FINGERPRINT)
        self.assertFalse(fresh["blocking"], fresh.get("blocking_reasons"))
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=fresh["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertGreaterEqual(result["master_fields_filled"], 1)
        self.assertGreaterEqual(result["master_fields_updated"], 1)
        self.assertEqual(result["workflow_fields_written"], 0)
        with get_connection() as conn:
            self.assertEqual(
                conn.execute("SELECT website FROM companies WHERE id=?", (company_id,)).fetchone()[0],
                "https://filled-ri4-master.example",
            )
            self.assertEqual(
                conn.execute("SELECT website FROM companies WHERE id=?", (update_id,)).fetchone()[0],
                "https://new-ri4-update.example",
            )
            self.assertEqual(
                conn.execute("SELECT website FROM companies WHERE id=?", (manual_id,)).fetchone()[0],
                "https://manual-ri4.example",
            )
            latest = conn.execute(
                """
                SELECT source_type, old_value, new_value FROM field_provenance_events
                WHERE entity_type='company' AND entity_id=? AND field='website'
                ORDER BY id DESC LIMIT 1
                """,
                (company_id,),
            ).fetchone()
            self.assertEqual(latest["source_type"], "AI_RESEARCH")
            self.assertEqual(latest["new_value"], "https://filled-ri4-master.example")
            conn.execute("UPDATE companies SET website='https://now-filled.example' WHERE id=?", (company_id,))
            conn.commit()
        stale_fill = self._upload(
            "ri4-stale-fill.csv",
            headers,
            [["Unique RI4 Master Co", "https://too-late.example"]],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(
            self.premier_id,
            stale_fill["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(headers, fields={"company_name": "Org Name", "website": "Website"}),
        )
        stale_plan = dry_run_research_import(self.premier_id, stale_fill["batch_id"], limit=5)
        website_conflict = next(c for c in stale_plan["rows"][0]["conflicts"] if c["field"] == "website")
        if website_conflict["class"] == CONFLICT_FILL:
            save_master_resolution(
                self.premier_id, stale_fill["batch_id"], actor=self.actor,
                staged_row_id=stale_plan["rows"][0]["row_id"], field="website", resolution_type=MASTER_FILL,
            )
            with get_connection() as conn:
                conn.execute("UPDATE companies SET website='https://now-filled.example' WHERE id=?", (company_id,))
                conn.commit()
            with self.assertRaises(ValueError):
                confirm_research_import(
                    client_id=self.premier_id,
                    batch_id=stale_fill["batch_id"],
                    plan_fingerprint=stale_plan["plan_fingerprint"],
                    actor=self.actor,
                )

    def test_location_equivalence_and_new_location(self) -> None:
        self._enable()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, address, city, state, zip, source)
                VALUES ('RI4-LOC', 'Unique RI4 Location Co', '100 3rd Ave', 'Des Moines', 'IA', '50309', 'TEST')
                """
            )
            company_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO company_locations (company_id, location_name, location_type, address, city, state, zip, source_system)
                VALUES (?, 'HQ', 'office', '100 3rd Ave', 'Des Moines', 'IA', '50309', 'TEST')
                """,
                (company_id,),
            )
            before_locs = int(conn.execute("SELECT COUNT(*) FROM company_locations WHERE company_id=?", (company_id,)).fetchone()[0])
            conn.commit()
        headers = ["Org Name", "Address", "City", "State", "ZIP"]
        same = self._upload(
            "ri4-loc-same.csv",
            headers,
            [["Unique RI4 Location Co", "100 Third Avenue", "Des Moines", "Iowa", "50309-1234"]],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(
            self.premier_id,
            same["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={"company_name": "Org Name", "address": "Address", "city": "City", "state": "State", "zip": "ZIP"},
            ),
        )
        plan = dry_run_research_import(self.premier_id, same["batch_id"], limit=5)
        if plan["rows"][0]["ri_class"] == CLASS_NEW_LOCATION and plan["rows"][0].get("matched_company_id"):
            save_match_resolution(
                self.premier_id, same["batch_id"], actor=self.actor,
                staged_row_id=plan["rows"][0]["row_id"],
                resolution_type="USE_EXISTING",
                company_id=plan["rows"][0]["matched_company_id"],
            )
        plan = self._use_existing_matches(same["batch_id"])
        self.assertFalse(plan["blocking"], plan.get("blocking_reasons"))
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=same["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        with get_connection() as conn:
            after_same = int(conn.execute("SELECT COUNT(*) FROM company_locations WHERE company_id=?", (company_id,)).fetchone()[0])
        self.assertEqual(after_same, before_locs)
        other = self._upload(
            "ri4-loc-new.csv",
            headers,
            [["Unique RI4 Location Co", "900 Plant Rd", "Cedar Rapids", "IA", "52401"]],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(
            self.premier_id,
            other["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={"company_name": "Org Name", "address": "Address", "city": "City", "state": "State", "zip": "ZIP"},
            ),
        )
        loc_plan = dry_run_research_import(self.premier_id, other["batch_id"], limit=5)
        loc_row = loc_plan["rows"][0]
        self.assertIsNotNone(loc_row.get("matched_company_id"))
        if loc_row["ri_class"] in {CLASS_NEW_LOCATION, CLASS_POSSIBLE, CLASS_AMBIGUOUS}:
            save_match_resolution(
                self.premier_id, other["batch_id"], actor=self.actor,
                staged_row_id=loc_row["row_id"],
                resolution_type="TREAT_AS_NEW_LOCATION",
                company_id=loc_row["matched_company_id"],
            )
            addr = next((c for c in loc_row["conflicts"] if c["field"] == "address"), None)
            if addr and addr["class"] == "POSSIBLE_NEW_LOCATION":
                save_master_resolution(
                    self.premier_id, other["batch_id"], actor=self.actor,
                    staged_row_id=loc_row["row_id"], field="address",
                    resolution_type=MASTER_ADD_LOCATION,
                )
        loc_plan = dry_run_research_import(self.premier_id, other["batch_id"], limit=5)
        self.assertFalse(loc_plan["blocking"], loc_plan.get("blocking_reasons"))
        loc_result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=other["batch_id"],
            plan_fingerprint=loc_plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertGreaterEqual(loc_result["locations_created"], 1)
        with get_connection() as conn:
            name = conn.execute("SELECT company_name FROM companies WHERE id=?", (company_id,)).fetchone()[0]
            self.assertEqual(name, "Unique RI4 Location Co")
            self.assertNotIn("Cedar Rapids", name)

    def test_full_rehearsal_idempotence_second_batch_and_isolation(self) -> None:
        self._enable()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, website, legacy_phone, source)
                VALUES ('RI4-SHARE', 'Unique RI4 Shared Co', 'https://ri4-shared-co.test', '3198880100', 'TEST')
                """
            )
            shared_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO contacts (company_id, external_record_no, first_name, last_name, email, phone, source_row_index)
                VALUES (?, 'RI4-SHARE-C', 'Alex', 'Shared', 'alex@ri4-shared-co.test', '3198880100', 0)
                """,
                (shared_id,),
            )
            contact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            upsert_contact_phone_keys(conn, contact_id, "3198880100", "")
            conn.execute(
                """
                INSERT INTO client_company_relationships
                    (client_id, company_id, status, assigned_user_id, notes, is_hot, next_action, follow_up_date)
                VALUES (?, ?, 'Customer', 1, 'Brown keeps this', 1, 'Call Brown', '2026-10-01')
                """,
                (self.brown_id, shared_id),
            )
            brown_before = conn.execute(
                """
                SELECT status, assigned_user_id, notes, is_hot, next_action, follow_up_date
                FROM client_company_relationships WHERE client_id=? AND company_id=?
                """,
                (self.brown_id, shared_id),
            ).fetchone()
            conn.commit()
        headers = [
            "Org Name", "Website", "Phone", "Contact Name", "Email", "Title", "Ext", "Comments",
            "Priority", "Why", "Market", "Equipment", "Components", "Department",
            "Source URL", "Press Tonnage", "Install Date", "OEM", "Active", "Catalog", "Status",
        ]
        rows = [
            [
                "Unique RI4 New Rehearsal Co", "https://ri4-new-rehearsal.test", "6462018888",
                "Jordan Rehearsal", "jordan@ri4-new-rehearsal.test", "Buyer", "221",
                "Line one\nLine two", "A - Iowa", "Fits tanks", "Ag", "Presses", "Brackets",
                "Purchasing", "https://source-ri4-new.example/fact", "800", "2026-01-15", "YES", "true",
                "https://catalog-ri4-new.example/press", "Hot",
            ],
            [
                "Unique RI4 Shared Co", "https://ri4-shared-co.test", "3198880100",
                "Alex Shared", "alex@ri4-shared-co.test", "Engineer", "",
                "Shared note", "B", "Fits shared", "OEM", "Fans", "Panels",
                "Engineering", "https://source-ri4-shared.example/fact", "600", "2025-06-01", "NO", "false",
                "https://catalog-ri4-shared.example", "Hot Prospect",
            ],
            [
                "Unique RI4 Shared Co", "https://ri4-shared-co.test", "3198880100",
                "Casey Possible", "", "Plant Manager", "14",
                "Shared note", "B", "Fits shared", "OEM", "Fans", "Panels",
                "Purchasing", "https://source-ri4-shared.example/fact", "600", "2025-06-01", "NO", "false",
                "https://catalog-ri4-shared.example", "Hot",
            ],
            [
                "Unique RI4 Shared Co", "https://ri4-shared-co.test", "3198880100",
                "Dana Skip", "", "Scheduler", "",
                "Shared note", "B", "Fits shared", "OEM", "Fans", "Panels",
                "Purchasing", "https://source-ri4-shared.example/fact", "600", "2025-06-01", "NO", "false",
                "https://catalog-ri4-shared.example", "Hot",
            ],
            [
                "Unique RI4 New Rehearsal Co", "https://ri4-new-rehearsal.test", "6462018888",
                "Purchasing", "", "", "",
                "Line one\nLine two", "A - Iowa", "Fits tanks", "Ag", "Presses", "Brackets",
                "Purchasing", "https://source-ri4-new.example/fact", "800", "2026-01-15", "YES", "true",
                "https://catalog-ri4-new.example/press", "Hot",
            ],
        ]
        batch = self._upload("ri4-rehearsal.csv", headers, rows, source_type="CUSTOM_REPORT")
        mapping = build_v3_mapping(
            headers,
            fields={
                "company_name": "Org Name",
                "website": "Website",
                "phone": "Phone",
                "contact_full_name": "Contact Name",
                "contact_email": "Email",
                "contact_title": "Title",
                "contact_phone_extension": "Ext",
                "imported_notes": "Comments",
                "research_priority": "Priority",
                "why_client_fits": "Why",
                "target_market": "Market",
                "equipment_product": "Equipment",
                "potential_components": "Components",
                "target_department": "Department",
                "product_source": "Source URL",
                "workflow_status": "Status",
            },
            custom=[
                {"header": "Press Tonnage", "label": "Press Tonnage", "key": "press_tonnage", "value_type": "NUMBER"},
                {"header": "Install Date", "label": "Install Date", "key": "install_date", "value_type": "DATE"},
                {"header": "OEM", "label": "OEM Flag", "key": "oem_flag", "value_type": "TEXT"},
                {"header": "Active", "label": "Active", "key": "is_active", "value_type": "BOOLEAN"},
                {"header": "Catalog", "label": "Catalog URL", "key": "catalog_url", "value_type": "URL"},
            ],
        )
        save_mapping(self.premier_id, batch["batch_id"], actor=self.actor, mapping=mapping)
        plan = self._use_existing_matches(batch["batch_id"])
        self.assertIn("forecast", plan)
        self.assertEqual(plan["workflow_fields_will_write"], 0)
        self.assertFalse(plan["production_confirm_enabled"])
        casey = next(
            row
            for row in plan["rows"]
            if row["company_name"] == "Unique RI4 Shared Co"
            and row.get("contacts")
            and "Casey" in json.dumps(row["contacts"])
        )
        save_contact_resolution(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            staged_row_id=casey["row_id"],
            resolution_type=RES_CREATE_CONTACT,
        )
        dana = next(
            row
            for row in plan["rows"]
            if row["company_name"] == "Unique RI4 Shared Co"
            and row.get("contacts")
            and "Dana" in json.dumps(row["contacts"])
        )
        save_contact_resolution(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            staged_row_id=dana["row_id"],
            resolution_type=RES_SKIP_CONTACT,
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=20)
        self.assertFalse(plan["blocking"], plan.get("blocking_reasons"))
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["companies_created"], 1)
        self.assertGreaterEqual(result["companies_reused"], 1)
        self.assertEqual(result["ccrs_created"], 2)
        self.assertGreaterEqual(result["contacts_created"], 2)
        self.assertGreaterEqual(result["contacts_skipped"], 1)
        self.assertGreaterEqual(result["notes_created"], 1)
        self.assertGreaterEqual(result["notes_deduped"], 1)
        self.assertEqual(result["workflow_fields_written"], 0)
        self.assertEqual(result["research_created"], 2)
        self.assertGreaterEqual(result["sources_created"], 1)
        replay = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertTrue(replay.get("idempotent"))
        self.assertEqual(replay["companies_created"], result["companies_created"])
        second = self._upload(
            "ri4-rehearsal-2.csv",
            headers,
            [
                [
                    "Unique RI4 New Rehearsal Co", "https://ri4-new-rehearsal.test", "6462018888",
                    "Jordan Rehearsal", "jordan@ri4-new-rehearsal.test", "Buyer", "221",
                    "Second-batch note", "A - Iowa", "Fits tanks even better", "Ag", "Presses", "Brackets",
                    "Purchasing", "https://source-ri4-new.example/updated", "900", "2026-01-15", "YES", "true",
                    "https://catalog-ri4-new.example/press", "Hot",
                ],
                [
                    "Unique RI4 New Rehearsal Co", "https://ri4-new-rehearsal.test", "6462018888",
                    "Jordan Rehearsal", "jordan@ri4-new-rehearsal.test", "Buyer", "221",
                    "Line one\nLine two", "A - Iowa", "Fits tanks even better", "Ag", "Presses", "Brackets",
                    "Purchasing", "https://source-ri4-new.example/updated", "900", "2026-01-15", "YES", "true",
                    "https://catalog-ri4-new.example/press", "Hot",
                ],
            ],
            source_type="CUSTOM_REPORT",
        )
        save_mapping(self.premier_id, second["batch_id"], actor=self.actor, mapping=mapping)
        second_plan = self._use_existing_matches(second["batch_id"])
        self.assertFalse(second_plan["blocking"], second_plan.get("blocking_reasons"))
        second_result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=second["batch_id"],
            plan_fingerprint=second_plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(second_result["companies_created"], 0)
        self.assertEqual(second_result["ccrs_created"], 0)
        self.assertEqual(second_result["workflow_fields_written"], 0)
        self.assertEqual(second_result["research_created"], 1)
        self.assertEqual(second_result["research_superseded"], 1)
        self.assertGreaterEqual(second_result["notes_created"], 1)
        self.assertGreaterEqual(second_result["notes_deduped"], 1)
        with get_connection() as conn:
            research = conn.execute(
                """
                SELECT is_current, why_client_fits FROM client_company_research
                WHERE client_id=? AND company_id=(SELECT id FROM companies WHERE company_name='Unique RI4 New Rehearsal Co')
                ORDER BY id
                """,
                (self.premier_id,),
            ).fetchall()
            current = [row for row in research if int(row["is_current"]) == 1]
            historical = [row for row in research if int(row["is_current"]) == 0]
            self.assertEqual(len(research), 2)
            self.assertEqual(len(current), 1)
            self.assertEqual(len(historical), 1)
            self.assertIn("even better", current[0]["why_client_fits"])
            brown_after = conn.execute(
                """
                SELECT status, assigned_user_id, notes, is_hot, next_action, follow_up_date
                FROM client_company_relationships WHERE client_id=? AND company_id=?
                """,
                (self.brown_id, shared_id),
            ).fetchone()
            self.assertEqual(tuple(brown_after), tuple(brown_before))
            premier_status = conn.execute(
                """
                SELECT status, assigned_user_id, is_hot FROM client_company_relationships
                WHERE client_id=? AND company_id=?
                """,
                (self.premier_id, shared_id),
            ).fetchone()
            self.assertNotEqual(premier_status["status"], "Hot Prospect")
            self.assertIsNone(premier_status["assigned_user_id"])
            self.assertEqual(int(premier_status["is_hot"] or 0), 0)
            notes = conn.execute(
                """
                SELECT notes FROM client_company_relationships
                WHERE client_id=? AND company_id=(SELECT id FROM companies WHERE company_name='Unique RI4 New Rehearsal Co')
                """,
                (self.premier_id,),
            ).fetchone()[0]
            self.assertEqual(notes.count("Line one"), 1)
            self.assertIn("Line two", notes)
            self.assertIn("Second-batch note", notes)
            jordan = conn.execute(
                """
                SELECT phone_extension FROM contacts
                WHERE company_id=(SELECT id FROM companies WHERE company_name='Unique RI4 New Rehearsal Co')
                  AND first_name='Jordan'
                """
            ).fetchone()
            self.assertEqual(blank(jordan["phone_extension"]), "221")
            self.assertEqual(
                int(conn.execute(
                    "SELECT COUNT(*) FROM contacts WHERE first_name='Dana' AND last_name='Skip'"
                ).fetchone()[0]),
                0,
            )


    def test_same_batch_three_contacts_one_research(self) -> None:
        self._enable()
        headers = [
            "Org Name", "Contact Name", "Department", "Market", "Comments", "Priority", "Source URL",
        ]
        rows = [
            ["Unique RI4B Acme Co", "Jane Smith", "Purchasing", "Ag", "Shared acme note", "A", "https://ri4b-acme.test/a"],
            ["Unique RI4B Acme Co", "John Jones", "Engineering", "Ag", "Shared acme note", "A", "https://ri4b-acme.test/a"],
            ["Unique RI4B Acme Co", "Riley Buyer", "Purchasing", "Ag", "Second acme note", "A - Iowa", "https://ri4b-acme.test/b"],
        ]
        batch = self._upload("ri4b-three.csv", headers, rows, source_type="INTERNAL_RESEARCH")
        mapping = build_v3_mapping(
            headers,
            fields={
                "company_name": "Org Name",
                "contact_full_name": "Contact Name",
                "target_department": "Department",
                "target_market": "Market",
                "imported_notes": "Comments",
                "research_priority": "Priority",
                "product_source": "Source URL",
            },
        )
        save_mapping(self.premier_id, batch["batch_id"], actor=self.actor, mapping=mapping)
        plan = self._use_existing_matches(batch["batch_id"])
        self.assertFalse(plan["blocking"], plan.get("blocking_reasons"))
        self.assertEqual(plan["forecast"]["research_created"], 1)
        self.assertEqual(plan["counts"]["research_rows_to_add"], 1)
        result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=batch["batch_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(result["companies_created"], 1)
        self.assertEqual(result["ccrs_created"], 1)
        self.assertEqual(result["research_created"], 1)
        self.assertEqual(result["research_superseded"], 0)
        self.assertEqual(result["contacts_created"], 3)
        self.assertGreaterEqual(result["notes_created"], 1)
        self.assertGreaterEqual(result["notes_deduped"], 1)
        with get_connection() as conn:
            company_id = int(
                conn.execute(
                    "SELECT id FROM companies WHERE company_name='Unique RI4B Acme Co'"
                ).fetchone()[0]
            )
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM companies WHERE company_name='Unique RI4B Acme Co'").fetchone()[0]),
                1,
            )
            self.assertEqual(
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM client_company_relationships WHERE client_id=? AND company_id=?",
                        (self.premier_id, company_id),
                    ).fetchone()[0]
                ),
                1,
            )
            research = conn.execute(
                """
                SELECT id, is_current, research_priority_code FROM client_company_research
                WHERE client_id=? AND company_id=?
                """,
                (self.premier_id, company_id),
            ).fetchall()
            self.assertEqual(len(research), 1)
            self.assertEqual(int(research[0]["is_current"]), 1)
            self.assertEqual(blank(research[0]["research_priority_code"]), "A")
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM contacts WHERE company_id=?", (company_id,)).fetchone()[0]),
                3,
            )
            depts = {
                blank(row["attribute_value"])
                for row in conn.execute(
                    """
                    SELECT attribute_value FROM research_attributes
                    WHERE research_id=? AND attribute_type='target_department'
                    """,
                    (int(research[0]["id"]),),
                )
            }
            self.assertEqual(depts, {"Purchasing", "Engineering"})
            markets = {
                blank(row["attribute_value"])
                for row in conn.execute(
                    """
                    SELECT attribute_value FROM research_attributes
                    WHERE research_id=? AND attribute_type='target_market'
                    """,
                    (int(research[0]["id"]),),
                )
            }
            self.assertEqual(markets, {"Ag"})
            urls = {
                blank(row["source_url"])
                for row in conn.execute(
                    "SELECT source_url FROM research_sources WHERE research_id=?",
                    (int(research[0]["id"]),),
                )
            }
            self.assertEqual(urls, {"https://ri4b-acme.test/a", "https://ri4b-acme.test/b"})
            notes = conn.execute(
                "SELECT notes FROM client_company_relationships WHERE client_id=? AND company_id=?",
                (self.premier_id, company_id),
            ).fetchone()[0]
            self.assertEqual(notes.count("Shared acme note"), 1)
            self.assertIn("Second acme note", notes)

        second = self._upload(
            "ri4b-three-refresh.csv",
            headers,
            [["Unique RI4B Acme Co", "Jane Smith", "Purchasing", "Ag", "Shared acme note", "A", "https://ri4b-acme.test/c"]],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(self.premier_id, second["batch_id"], actor=self.actor, mapping=mapping)
        second_plan = self._use_existing_matches(second["batch_id"])
        second_result = confirm_research_import(
            client_id=self.premier_id,
            batch_id=second["batch_id"],
            plan_fingerprint=second_plan["plan_fingerprint"],
            actor=self.actor,
        )
        self.assertEqual(second_result["research_created"], 1)
        self.assertEqual(second_result["research_superseded"], 1)
        self.assertEqual(second_result["companies_created"], 0)
        with get_connection() as conn:
            versions = conn.execute(
                """
                SELECT is_current FROM client_company_research
                WHERE client_id=? AND company_id=(SELECT id FROM companies WHERE company_name='Unique RI4B Acme Co')
                ORDER BY id
                """,
                (self.premier_id,),
            ).fetchall()
            self.assertEqual(len(versions), 2)
            self.assertEqual([int(row["is_current"]) for row in versions], [0, 1])

    def test_same_batch_priority_conflict_blocks(self) -> None:
        self._enable()
        headers = ["Org Name", "Priority"]
        batch = self._upload(
            "ri4b-conflict.csv",
            headers,
            [["Unique RI4B Conflict Co", "A"], ["Unique RI4B Conflict Co", "B"]],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(headers, fields={"company_name": "Org Name", "research_priority": "Priority"}),
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        self.assertTrue(plan["blocking"])
        self.assertTrue(any(SAME_BATCH_RESEARCH_CONFLICT in reason for reason in plan["blocking_reasons"]))
        self.assertGreaterEqual(plan["counts"].get("same_batch_conflicts", 0), 1)
        with self.assertRaises(ValueError) as raised:
            confirm_research_import(
                client_id=self.premier_id,
                batch_id=batch["batch_id"],
                plan_fingerprint=plan["plan_fingerprint"],
                actor=self.actor,
            )
        self.assertIn("blocked", str(raised.exception).lower())

    def test_same_batch_master_conflict_blocks(self) -> None:
        self._enable()
        headers = ["Org Name", "Website"]
        batch = self._upload(
            "ri4b-master.csv",
            headers,
            [
                ["Unique RI4B Master Co", "https://ri4b-master-a.test"],
                ["Unique RI4B Master Co", "https://ri4b-master-b.test"],
            ],
            source_type="INTERNAL_RESEARCH",
        )
        save_mapping(
            self.premier_id,
            batch["batch_id"],
            actor=self.actor,
            mapping=build_v3_mapping(
                headers,
                fields={"company_name": "Org Name", "website": "Website"},
            ),
        )
        plan = dry_run_research_import(self.premier_id, batch["batch_id"], limit=5)
        self.assertTrue(plan["blocking"])
        self.assertTrue(any("SAME_BATCH_MASTER_CONFLICT" in reason for reason in plan["blocking_reasons"]))
        with self.assertRaises(ValueError) as raised:
            confirm_research_import(
                client_id=self.premier_id,
                batch_id=batch["batch_id"],
                plan_fingerprint=plan["plan_fingerprint"],
                actor=self.actor,
            )
        self.assertIn("blocked", str(raised.exception).lower())


class ResearchImportAuthorizationTests(unittest.TestCase):
    """HTTP authorization for Research & Custom Prospect Import. Isolated testdb only."""

    def setUp(self) -> None:
        os.environ.pop("NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM", None)
        with get_connection() as conn:
            rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
            self.client_id = int(rows[0]["id"])
            self.other_id = int(rows[1]["id"])

    def _create_user(self, *, administrator: int, staff_role: str) -> tuple[int, str, str]:
        password = f"NsTest9{secrets.token_hex(10)}"
        email = f"ri-auth.{secrets.token_hex(4)}@example.test"
        digest = hash_password(password, email=email)
        with get_connection() as conn:
            ensure_staff_role_schema(conn)
            conn.execute(
                """
                INSERT INTO users (
                    email, full_name, is_administrator, is_internal_northstar, active,
                    password_hash, failed_login_count, locked_until, staff_role
                ) VALUES (?, ?, ?, 1, 1, ?, 0, '', ?)
                """,
                (email, "RI Auth", int(administrator), digest, staff_role),
            )
            user_id = int(
                conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO user_client_assignments
                    (user_id, client_id, role, active, assigned_at)
                VALUES (?, ?, 'staff', 1, datetime('now'))
                """,
                (user_id, self.client_id),
            )
            conn.commit()
        return user_id, email, password

    def _delete_user(self, user_id: int) -> None:
        with get_connection() as conn:
            conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()

    def test_specialist_cannot_use_research_import_admin_endpoints(self) -> None:
        user_id, email, password = self._create_user(
            administrator=0, staff_role=REVOPS_SPECIALIST
        )
        http = TestClient(app)
        try:
            login = http.post("/api/auth/login", json={"email": email, "password": password})
            self.assertEqual(login.status_code, 200)
            csrf = {CSRF_HEADER: str(login.json().get("csrf_token") or "")}
            own = http.post(
                f"/api/clients/{self.client_id}/admin/research-imports",
                headers=csrf,
                files={"file": ("x.csv", b"Company Name\nAcme", "text/csv")},
            )
            self.assertEqual(own.status_code, 403)
            self.assertEqual(own.json().get("detail"), ADMIN_REQUIRED_DETAIL)
            other = http.post(
                f"/api/clients/{self.other_id}/admin/research-imports",
                headers=csrf,
                files={"file": ("x.csv", b"Company Name\nAcme", "text/csv")},
            )
            self.assertEqual(other.status_code, 403)
            for suffix in (
                "mapping",
                "match-resolution",
                "master-resolution",
                "contact-resolution",
                "dry-run",
                "confirm",
            ):
                blocked = http.post(
                    f"/api/clients/{self.client_id}/admin/research-imports/1/{suffix}",
                    headers=csrf,
                    json={"plan_fingerprint": "x"},
                )
                self.assertEqual(blocked.status_code, 403, suffix)
                self.assertEqual(blocked.json().get("detail"), ADMIN_REQUIRED_DETAIL)
        finally:
            self._delete_user(user_id)

    def test_http_confirm_remains_disabled_for_administrator(self) -> None:
        user_id, email, password = self._create_user(
            administrator=1, staff_role=SYSTEM_ADMINISTRATOR
        )
        http = TestClient(app)
        try:
            login = http.post("/api/auth/login", json={"email": email, "password": password})
            self.assertEqual(login.status_code, 200)
            csrf = {CSRF_HEADER: str(login.json().get("csrf_token") or "")}
            confirm = http.post(
                f"/api/clients/{self.client_id}/admin/research-imports/1/confirm",
                headers=csrf,
                json={"plan_fingerprint": "x"},
            )
            self.assertEqual(confirm.status_code, 403)
            self.assertEqual(confirm.json().get("detail"), ISOLATED_CONFIRM_DISABLED)
        finally:
            self._delete_user(user_id)


if __name__ == "__main__":
    unittest.main()
