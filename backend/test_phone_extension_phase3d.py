"""Phase 3D phone-extension schema, parser, and write-path tests.

Isolated testdb only. Does not write production northstar.db.
"""

from __future__ import annotations

import testdb

from contact_phone import canonical_contact_phone, store_phone_parts, upsert_contact_phone_keys
from crm_add_data import confirm_company_contact_add, preview_company_contact_add
from crm_identity_keys import company_identity_tuple
from db import ensure_phone_extension_columns, get_connection, migrate_schema
from import_brown_industries import digits_phone
from models import (
    CrmAddCompanyInput,
    CrmAddConfirmRequest,
    CrmAddContactConfirmItem,
    CrmAddContactInput,
    CrmAddPreviewRequest,
)


def test_schema_migration_idempotent() -> None:
    with get_connection() as conn:
        first = ensure_phone_extension_columns(conn)
        second = ensure_phone_extension_columns(conn)
        migrate_schema(conn)
        cols_c = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
        cols_t = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    assert "legacy_phone_extension" in cols_c
    assert "legacy_alt_phone_extension" in cols_c
    assert "phone_extension" in cols_t
    assert "alt_phone_extension" in cols_t
    assert "legacy_mobile_extension" not in cols_c
    assert second == []
    _ = first


def test_digits_phone_uses_main_only() -> None:
    assert digits_phone("(712) 725-2311 x153") == "7127252311"
    assert digits_phone("(262) 569-1960 10130") == "9196010130"
    assert company_identity_tuple(legacy_phone="(712) 725-2311 x153")["phone_digits"] == "7127252311"
    assert company_identity_tuple(legacy_phone="(712) 725-2311 x153")["phone_last7"] == "7252311"


def test_contact_keys_main_only() -> None:
    nanp, last7 = canonical_contact_phone("(847) 437-3900 x330")
    assert nanp == "8474373900"
    assert last7 == "4373900"


def test_add_company_contact_splits_extension() -> None:
    with get_connection() as conn:
        ensure_phone_extension_columns(conn)
        conn.commit()
    preview = preview_company_contact_add(
        CrmAddPreviewRequest(
            client_id=1,
            company=CrmAddCompanyInput(
                company_name="NS3D Ext Co",
                phone="(515) 555-1212 ext 44",
            ),
            contacts=[
                CrmAddContactInput(
                    first_name="Pat",
                    last_name="Ext",
                    phone="(515) 555-9999 x12",
                )
            ],
        )
    )
    body = CrmAddConfirmRequest(
        client_id=1,
        company=CrmAddCompanyInput(
            company_name="NS3D Ext Co",
            phone="(515) 555-1212 ext 44",
        ),
        contacts=[
            CrmAddContactConfirmItem(
                contact=CrmAddContactInput(
                    first_name="Pat",
                    last_name="Ext",
                    phone="(515) 555-9999 x12",
                ),
                selected=True,
                decision="create_new",
            )
        ],
        company_decision="create_new",
    )
    result = confirm_company_contact_add(body)
    cid = int(result.company_id)
    with get_connection() as conn:
        company = conn.execute(
            "SELECT legacy_phone, legacy_phone_extension FROM companies WHERE id=?",
            (cid,),
        ).fetchone()
        contact = conn.execute(
            """
            SELECT id, phone, phone_extension FROM contacts
            WHERE company_id=? ORDER BY id DESC LIMIT 1
            """,
            (cid,),
        ).fetchone()
        key = conn.execute(
            "SELECT phone_digits FROM company_identity_keys WHERE company_id=?",
            (cid,),
        ).fetchone()
        conn.execute(
            "DELETE FROM contact_phone_keys WHERE contact_id IN (SELECT id FROM contacts WHERE company_id=?)",
            (cid,),
        )
        conn.execute(
            "DELETE FROM contact_client_workflows WHERE contact_id IN (SELECT id FROM contacts WHERE company_id=?)",
            (cid,),
        )
        conn.execute(
            "DELETE FROM contact_client_relationships WHERE contact_id IN (SELECT id FROM contacts WHERE company_id=?)",
            (cid,),
        )
        conn.execute("DELETE FROM activities WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM contacts WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM company_identity_keys WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM client_company_relationships WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM companies WHERE id=?", (cid,))
        conn.commit()
    assert company["legacy_phone"] == "(515) 555-1212"
    assert str(company["legacy_phone_extension"] or "") == "44"
    assert contact["phone"] == "(515) 555-9999"
    assert str(contact["phone_extension"] or "") == "12"
    assert key["phone_digits"] == "5155551212"
    _ = preview
    _ = store_phone_parts
    _ = upsert_contact_phone_keys


def test_display_and_export_keep_extension_separate() -> None:
    from contact_phone import format_phone_with_extension
    from db import SCHEMA_PATH
    from master_data_export import COMPANY_HEADERS, CONTACT_HEADERS
    from pg_migration.schema_pg import render_postgresql_ddl

    assert format_phone_with_extension("(515) 555-1212", "44") == "(515) 555-1212 x44"
    assert format_phone_with_extension("(515) 555-1212", "") == "(515) 555-1212"
    assert COMPANY_HEADERS[COMPANY_HEADERS.index("Phone") + 1] == "Phone Extension"
    assert COMPANY_HEADERS[COMPANY_HEADERS.index("Alt Phone") + 1] == "Alt Phone Extension"
    assert CONTACT_HEADERS[CONTACT_HEADERS.index("Phone") + 1] == "Phone Extension"
    assert CONTACT_HEADERS[CONTACT_HEADERS.index("Alt Phone") + 1] == "Alt Phone Extension"
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    for col in (
        "legacy_phone_extension",
        "legacy_alt_phone_extension",
        "phone_extension",
        "alt_phone_extension",
    ):
        assert col in schema
    ddl = render_postgresql_ddl()
    assert "legacy_phone_extension" in ddl
    assert "phone_extension" in ddl
    assert "legacy_mobile_extension" not in ddl
    assert "mobile_extension" not in ddl


if __name__ == "__main__":
    testdb  # imported for isolation side effect
    test_schema_migration_idempotent()
    test_digits_phone_uses_main_only()
    test_contact_keys_main_only()
    test_add_company_contact_splits_extension()
    test_display_and_export_keep_extension_separate()
    print("ALL PASSED (5)")
