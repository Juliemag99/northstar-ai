"""E2E: New public contact → preview → confirm under existing Whirlpool, then cleanup.

Simulates Research Detail → Add to NorthStar payload. Does not leave Flora in CRM.
"""

from __future__ import annotations

import testdb
from crm_add_data import confirm_company_contact_add, preview_company_contact_add
from db import get_connection
from models import (
    CrmAddCompanyInput,
    CrmAddConfirmRequest,
    CrmAddContactConfirmItem,
    CrmAddContactInput,
    CrmAddPreviewRequest,
)
from research_data import get_research_run


def _counts(conn) -> dict[str, int]:
    return {
        "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "ccr": conn.execute(
            "SELECT COUNT(*) FROM client_company_relationships"
        ).fetchone()[0],
        "whirlpool_contacts": conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE company_id = 298"
        ).fetchone()[0],
    }


def main() -> int:
    resp = get_research_run(30)
    flora = next(
        (
            c
            for c in (resp.public_contacts or [])
            if (c.contact_name or "").lower() == "flora jia"
        ),
        None,
    )
    assert flora is not None, "Flora Jia must exist in research run 30"

    ryan = next(
        (
            c
            for c in (resp.public_contacts or [])
            if (c.contact_name or "").lower() == "ryan decker"
        ),
        None,
    )
    assert ryan is not None
    assert ryan.crm_match_status == "existing"
    assert ryan.crm_matched_contact_id

    company = CrmAddCompanyInput(
        company_name=resp.company_name,
        website=(resp.northstar_known.website if resp.northstar_known else "") or "",
        city=(resp.northstar_known.city if resp.northstar_known else "") or "",
        state=(resp.northstar_known.state if resp.northstar_known else "") or "",
    )
    # Flora Jia is already in CRM. Create a unique isolated contact instead of
    # duplicating her production row.
    contact = CrmAddContactInput(
        full_name="NSE2E Public Tester",
        title=flora.contact_title or "Buyer",
        linkedin="",
        source_url=flora.source_url or flora.linkedin_url or "",
    )

    with get_connection() as conn:
        before = _counts(conn)

    # Preview — equivalent to Detail → Add to NorthStar opening AddToNorthStar
    prev = preview_company_contact_add(
        CrmAddPreviewRequest(
            client_id=1,
            company=company,
            contacts=[contact],
            research_run_id=resp.research_run_id,
            source="AI Research public contacts",
            provider="public_web",
            known_company_id=298,
            trusted_external_record_no="1218562",
        )
    )
    assert prev.company_match_type == "existing_company", prev.company_match_type
    assert prev.matched_company_id == 298
    assert prev.matched_external_record_no == "1218562"
    assert len(prev.contacts) == 1
    assert prev.contacts[0].match_status == "new"
    assert prev.relationship and prev.relationship.exists

    created_id: int | None = None
    try:
        result = confirm_company_contact_add(
            CrmAddConfirmRequest(
                client_id=1,
                company=company,
                contacts=[
                    CrmAddContactConfirmItem(
                        contact=contact,
                        selected=True,
                        decision="create_new",
                    )
                ],
                company_decision="use_existing",
                existing_company_id=298,
                known_company_id=298,
                trusted_external_record_no="1218562",
                research_run_id=resp.research_run_id,
                source="AI Research public contacts",
                provider="public_web",
            )
        )
        assert result.company_id == 298
        assert result.company_created is False
        created = [c for c in (result.contacts or []) if c.get("created") and c.get("contact_id")]
        assert created, result.contacts
        created_id = int(created[0]["contact_id"])

        with get_connection() as conn:
            row = conn.execute(
                "SELECT company_id, first_name, last_name FROM contacts WHERE id = ?",
                (created_id,),
            ).fetchone()
            assert row is not None
            assert int(row["company_id"]) == 298
            assert f"{row['first_name']} {row['last_name']}".strip().lower() == "nse2e public tester"
            ccr = conn.execute(
                """
                SELECT COUNT(*) FROM client_company_relationships
                WHERE client_id = 1 AND company_id = 298
                """
            ).fetchone()[0]
            assert ccr >= 1
            ryan_n = conn.execute(
                """
                SELECT COUNT(*) FROM contacts
                WHERE company_id = 298
                  AND lower(trim(first_name || ' ' || last_name)) = 'ryan decker'
                """
            ).fetchone()[0]
            assert ryan_n >= 1
    finally:
        if created_id:
            with get_connection() as conn:
                conn.execute("DELETE FROM contacts WHERE id = ?", (created_id,))
                conn.commit()

    with get_connection() as conn:
        after = _counts(conn)

    assert after == before, (before, after)
    print("PASS: flora detail->add->preview->confirm under existing Whirlpool + cleanup")
    print("BEFORE", before)
    print("AFTER", after)
    print("RYAN_EXISTING_ID", ryan.crm_matched_contact_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print("FAIL:", exc)
        raise SystemExit(1)
