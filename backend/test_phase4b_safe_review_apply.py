"""Phase 4B safe-review apply tests.

Isolated testdb only. Does not write production northstar.db.
"""

from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

import testdb

from _phase4b_safe_review_apply import (
    SOURCE_SYSTEM,
    apply_once,
    is_valid_nanp_main,
    parse_extension_only,
    revalidate_company_extension,
    revalidate_contact_extension,
    revalidate_rename,
)
from contact_phone import canonical_contact_phone
from crm_identity_keys import upsert_company_identity_key
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from import_brown_industries import digits_phone


def _insert_company(
    conn,
    *,
    name: str,
    record_no: str | None = None,
    phone: str = "(515) 555-1212",
    alt_phone: str = "",
    phone_ext: str = "",
    website: str = "",
    address: str = "100 Main St",
    city: str = "Des Moines",
    state: str = "IA",
) -> int:
    if not record_no:
        record_no = f"P4B-{secrets.token_hex(4)}"
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, address, city, state, zip, website,
            legacy_phone, legacy_alt_phone, legacy_phone_extension, created_at, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, '50309', ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (record_no, name, address, city, state, website, phone, alt_phone, phone_ext or None),
    )
    cid = int(cur.lastrowid)
    upsert_company_identity_key(
        conn,
        cid,
        external_record_no=record_no,
        company_name=name,
        website=website,
        legacy_phone=phone,
        address=address,
        city=city,
        state=state,
    )
    return cid


def _insert_contact(
    conn,
    company_id: int,
    *,
    first: str = "Pat",
    last: str = "Test",
    phone: str = "(515) 555-1212",
    alt: str = "x99",
    phone_ext: str = "",
) -> int:
    marker = secrets.token_hex(4)
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name,
            phone, alt_phone, phone_extension, source_row_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (company_id, f"CT-{marker}", first, last, phone, alt, phone_ext or None),
    )
    return int(cur.lastrowid)


class Phase4BSafeReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()

    def test_alias_preserved_before_rename_and_identity_refresh(self) -> None:
        marker = secrets.token_hex(3)
        old = f"Crosby Group (The) {marker}"
        new = f"The Crosby Group {marker}"
        with get_connection() as conn:
            cid = _insert_company(conn, name=old, record_no=f"RN-{marker}", website="thecrosbygroup.com")
            conn.commit()
            candidate = {
                "entity_type": "company",
                "entity_id": cid,
                "kind": "rename",
                "field": "company_name",
                "current": old,
                "recommended": new,
                "disposition": "A. SAFE RENAME AFTER REVIEW",
                "reason": "article invert",
                "evidence": "test",
            }
            audit: list[dict] = []
            stats = apply_once(conn, [candidate], audit)
            conn.commit()
            self.assertEqual(stats["renames_applied"], 1)
            self.assertEqual(stats["aliases_created"], 1)
            company = conn.execute(
                "SELECT company_name FROM companies WHERE id=?", (cid,)
            ).fetchone()
            self.assertEqual(company["company_name"], new)
            alias = conn.execute(
                """
                SELECT alias_name, source_system FROM company_aliases
                WHERE company_id=? AND alias_name=?
                """,
                (cid, old),
            ).fetchone()
            self.assertIsNotNone(alias)
            self.assertEqual(alias["source_system"], SOURCE_SYSTEM)
            ident = conn.execute(
                "SELECT norm_name, phone_digits FROM company_identity_keys WHERE company_id=?",
                (cid,),
            ).fetchone()
            self.assertEqual(ident["phone_digits"], "5155551212")
            audit2: list[dict] = []
            second = apply_once(conn, [candidate], audit2)
            self.assertEqual(second["renames_applied"], 0)
            self.assertEqual(second["aliases_created"], 0)
            self.assertEqual(second["renames_already"], 1)

    def test_rename_defers_on_collision(self) -> None:
        marker = secrets.token_hex(3)
        with get_connection() as conn:
            cid = _insert_company(conn, name=f"Old Co {marker}")
            other = _insert_company(conn, name=f"New Co {marker}")
            conn.commit()
            decision = revalidate_rename(
                conn,
                {
                    "entity_id": cid,
                    "current": f"Old Co {marker}",
                    "recommended": f"New Co {marker}",
                    "reason": "test",
                },
            )
            self.assertEqual(decision["status"], "deferred")
            self.assertIn("collision", decision["reason"].lower())
            _ = other

    def test_extension_only_sibling_migration_company_and_contact(self) -> None:
        with get_connection() as conn:
            cid = _insert_company(conn, name="Ext Co", phone="(231) 798-1483", alt_phone="ext.161")
            tid = _insert_contact(conn, cid, phone="(507) 427-3133", alt="x126")
            conn.commit()
            candidates = [
                {
                    "entity_type": "company",
                    "entity_id": cid,
                    "kind": "extension_only",
                    "field": "legacy_alt_phone",
                    "current": "ext.161",
                    "recommended": "(231) 798-1483 + extension 161",
                    "disposition": "B. EXTENSION-ONLY — POSSIBLE SIBLING MAIN",
                    "reason": "test",
                    "evidence": "test",
                },
                {
                    "entity_type": "contact",
                    "entity_id": tid,
                    "kind": "extension_only",
                    "field": "alt_phone",
                    "current": "x126",
                    "recommended": "(507) 427-3133 + extension 126",
                    "disposition": "B. EXTENSION-ONLY — POSSIBLE SIBLING MAIN",
                    "reason": "test",
                    "evidence": "test",
                },
            ]
            stats = apply_once(conn, candidates, [])
            conn.commit()
            self.assertEqual(stats["company_ext_applied"], 1)
            self.assertEqual(stats["contact_ext_applied"], 1)
            company = conn.execute(
                "SELECT legacy_phone, legacy_phone_extension, legacy_alt_phone FROM companies WHERE id=?",
                (cid,),
            ).fetchone()
            self.assertEqual(company["legacy_phone"], "(231) 798-1483")
            self.assertEqual(str(company["legacy_phone_extension"]), "161")
            self.assertEqual(company["legacy_alt_phone"], "")
            contact = conn.execute(
                "SELECT phone, phone_extension, alt_phone FROM contacts WHERE id=?",
                (tid,),
            ).fetchone()
            self.assertEqual(contact["phone"], "(507) 427-3133")
            self.assertEqual(str(contact["phone_extension"]), "126")
            self.assertEqual(contact["alt_phone"], "")
            ident = conn.execute(
                "SELECT phone_digits, phone_last7 FROM company_identity_keys WHERE company_id=?",
                (cid,),
            ).fetchone()
            self.assertEqual(ident["phone_digits"], "2317981483")
            self.assertEqual(ident["phone_last7"], "7981483")
            self.assertNotIn("161", ident["phone_digits"])
            keys = list(
                conn.execute(
                    "SELECT slot, nanp10, last7 FROM contact_phone_keys WHERE contact_id=?",
                    (tid,),
                )
            )
            self.assertEqual(len(keys), 1)
            self.assertEqual(keys[0]["slot"], "phone")
            self.assertEqual(keys[0]["nanp10"], "5074273133")
            self.assertEqual(keys[0]["last7"], "4273133")
            second = apply_once(conn, candidates, [])
            self.assertEqual(second["company_ext_applied"], 0)
            self.assertEqual(second["contact_ext_applied"], 0)
            self.assertEqual(second["fields_cleared"], 0)

    def test_ambiguous_sibling_must_defer(self) -> None:
        with get_connection() as conn:
            cid = _insert_company(conn, name="No Main", phone="", alt_phone="ext.161")
            tid = _insert_contact(conn, cid, phone="", alt="x126")
            conn.commit()
            company = revalidate_company_extension(
                conn,
                {
                    "entity_id": cid,
                    "field": "legacy_alt_phone",
                    "current": "ext.161",
                },
            )
            contact = revalidate_contact_extension(
                conn,
                {
                    "entity_id": tid,
                    "field": "alt_phone",
                    "current": "x126",
                },
            )
            self.assertEqual(company["status"], "deferred")
            self.assertEqual(contact["status"], "deferred")
            self.assertIn("sibling", contact["reason"].lower())

    def test_existing_extension_conflict_must_defer(self) -> None:
        with get_connection() as conn:
            cid = _insert_company(
                conn, name="Conflict Co", phone="(231) 798-1483", alt_phone="ext.161", phone_ext="999"
            )
            tid = _insert_contact(conn, cid, phone="(507) 427-3133", alt="x126", phone_ext="999")
            conn.commit()
            company = revalidate_company_extension(
                conn,
                {"entity_id": cid, "field": "legacy_alt_phone", "current": "ext.161"},
            )
            contact = revalidate_contact_extension(
                conn,
                {"entity_id": tid, "field": "alt_phone", "current": "x126"},
            )
            self.assertEqual(company["status"], "deferred")
            self.assertIn("conflicts", company["reason"])
            self.assertEqual(contact["status"], "deferred")
            self.assertIn("conflicts", contact["reason"])

    def test_unmarked_digits_and_reverse_slots_defer(self) -> None:
        self.assertFalse(parse_extension_only("122", field="alt_phone")["extension_ok"])
        self.assertTrue(parse_extension_only("x126", field="alt_phone")["extension_ok"])
        self.assertTrue(is_valid_nanp_main("(231) 798-1483"))
        self.assertFalse(is_valid_nanp_main("+49 871430700"))
        with get_connection() as conn:
            cid = _insert_company(conn, name="Reverse", phone="x161", alt_phone="(231) 798-1483")
            tid = _insert_contact(conn, cid, phone="x126", alt="(507) 427-3133")
            unmarked = _insert_contact(conn, cid, phone="(712) 722-1488", alt="122")
            conn.commit()
            self.assertEqual(
                revalidate_company_extension(
                    conn, {"entity_id": cid, "field": "legacy_phone", "current": "x161"}
                )["status"],
                "deferred",
            )
            self.assertEqual(
                revalidate_contact_extension(
                    conn, {"entity_id": tid, "field": "phone", "current": "x126"}
                )["status"],
                "deferred",
            )
            self.assertEqual(
                revalidate_contact_extension(
                    conn, {"entity_id": unmarked, "field": "alt_phone", "current": "122"}
                )["status"],
                "deferred",
            )

    def test_identity_keys_main_only_helpers(self) -> None:
        self.assertEqual(digits_phone("(231) 798-1483"), "2317981483")
        nanp, last7 = canonical_contact_phone("(507) 427-3133")
        self.assertEqual(nanp, "5074273133")
        self.assertEqual(last7, "4273133")
        with get_connection() as conn:
            cid = _insert_company(conn, name="Key Co", phone="(231) 798-1483", alt_phone="ext.161")
            conn.commit()
            apply_once(
                conn,
                [
                    {
                        "entity_type": "company",
                        "entity_id": cid,
                        "kind": "extension_only",
                        "field": "legacy_alt_phone",
                        "current": "ext.161",
                        "recommended": "",
                        "disposition": "B",
                        "reason": "test",
                        "evidence": "test",
                    }
                ],
                [],
            )
            ident = conn.execute(
                "SELECT phone_digits FROM company_identity_keys WHERE company_id=?",
                (cid,),
            ).fetchone()
            self.assertEqual(ident["phone_digits"], "2317981483")


if __name__ == "__main__":
    unittest.main()
