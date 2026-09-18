"""Tests for CRM Add / Link foundation. Creates then deletes test rows.

Run: python test_crm_add.py
No OAuth/email. Restores counts.
"""

from __future__ import annotations

import testdb
import sys

from crm_add_data import (
    allocate_ns_record_no,
    confirm_company_contact_add,
    preview_company_contact_add,
)
from db import get_connection
from models import (
    CrmAddCompanyInput,
    CrmAddConfirmRequest,
    CrmAddContactConfirmItem,
    CrmAddContactInput,
    CrmAddPreviewRequest,
)


def _counts(conn) -> dict[str, int]:
    return {
        "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
        "ccr": conn.execute(
            "SELECT COUNT(*) FROM client_company_relationships"
        ).fetchone()[0],
        "sales_events": conn.execute(
            "SELECT COUNT(*) FROM client_sales_events"
        ).fetchone()[0],
    }


def _cleanup(company_ids: list[int], activity_ids: list[int]) -> None:
    with get_connection() as conn:
        for aid in activity_ids:
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
        for cid in company_ids:
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (cid,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        # also wipe any leftover NS test companies by name marker
        rows = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE 'NSCRMTEST %'"
        ).fetchall()
        for r in rows:
            cid = int(r["id"])
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?", (cid,)
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.execute(
            "DELETE FROM activities WHERE notes LIKE 'Created from NorthStar AI Research%' "
            "AND external_record_no LIKE 'NS-%'"
        )
        conn.commit()


def main() -> int:
    created_companies: list[int] = []
    created_activities: list[int] = []

    with get_connection() as conn:
        before = _counts(conn)
        # Pick an existing Carmeco company with website for domain match
        existing = conn.execute(
            """
            SELECT co.id, co.company_name, co.external_record_no, co.website,
                   co.legacy_phone, ccr.status
            FROM companies co
            JOIN client_company_relationships ccr ON ccr.company_id = co.id
            WHERE ccr.client_id = 1
              AND trim(coalesce(co.website,'')) != ''
              AND lower(trim(coalesce(ccr.status,''))) NOT IN ('closed')
            ORDER BY co.id
            LIMIT 1
            """
        ).fetchone()
        assert existing is not None
        existing_status = existing["status"]
        brown_ccr = conn.execute(
            """
            SELECT id, status FROM client_company_relationships
            WHERE client_id = 2 AND company_id = ?
            """,
            (int(existing["id"]),),
        ).fetchone()
        brown_before = dict(brown_ccr) if brown_ccr else None

        # Contact with email on some company for email dedupe
        email_contact = conn.execute(
            """
            SELECT id, company_id, email, first_name, last_name, phone
            FROM contacts
            WHERE trim(coalesce(email,'')) != '' AND company_id = ?
            LIMIT 1
            """,
            (int(existing["id"]),),
        ).fetchone()

    # Domain match → existing
    from import_brown_industries import domain

    dom = domain(existing["website"])
    prev = preview_company_contact_add(
        CrmAddPreviewRequest(
            client_id=1,
            company=CrmAddCompanyInput(
                company_name="Totally Different Name XYZ",
                website=f"https://www.{dom}/about",
            ),
            contacts=[],
        )
    )
    assert prev.company_match_type == "existing_company", prev
    assert prev.matched_company_id == int(existing["id"])
    assert "domain_exact" in prev.company_match_reasons

    # Strong name match
    prev_name = preview_company_contact_add(
        CrmAddPreviewRequest(
            client_id=1,
            company=CrmAddCompanyInput(company_name=existing["company_name"]),
            contacts=[],
        )
    )
    assert prev_name.company_match_type == "possible_match", prev_name
    assert prev_name.matched_company_id == int(existing["id"]) or any(
        p.get("company_id") == int(existing["id"])
        for p in prev_name.possible_company_matches
    )

    # New company + NS record + CCR New + contact
    unique = "NSCRMTEST Alpha Manufacturing LLC"
    result = confirm_company_contact_add(
        CrmAddConfirmRequest(
            client_id=1,
            company=CrmAddCompanyInput(
                company_name=unique,
                website="https://nscrmtest-alpha.example.com",
                phone="5551234567",
                city="Springfield",
                state="MO",
            ),
            contacts=[
                CrmAddContactConfirmItem(
                    contact=CrmAddContactInput(
                        full_name="Pat Example",
                        title="Buyer",
                        email="pat.example@nscrmtest-alpha.example.com",
                    ),
                    selected=True,
                    decision="create_new",
                )
            ],
            company_decision="create_new",
            research_run_id=999001,
            source="unit-test",
            provider="test",
        )
    )
    created_companies.append(result.company_id)
    if result.activity_id:
        created_activities.append(result.activity_id)
    assert result.company_created is True
    assert result.external_record_no.startswith("NS-")
    assert result.relationship_created is True
    assert result.relationship_status == "New"
    assert any(c.get("created") for c in result.contacts)

    # Second NS id increments
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rn1 = allocate_ns_record_no(conn)
        # simulate insert collision path by inserting rn1 then allocating again
        conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, created_at, last_updated_at)
            VALUES (?, 'NSCRMTEST Seq Holder', datetime('now'), datetime('now'))
            """,
            (rn1,),
        )
        cid_holder = int(
            conn.execute(
                "SELECT id FROM companies WHERE external_record_no = ?", (rn1,)
            ).fetchone()["id"]
        )
        created_companies.append(cid_holder)
        rn2 = allocate_ns_record_no(conn)
        conn.commit()
    assert rn1 != rn2
    assert rn1.startswith("NS-") and rn2.startswith("NS-")
    assert int(rn2[3:]) == int(rn1[3:]) + 1

    # Existing company not duplicated; fields not overwritten; CCR preserved
    with get_connection() as conn:
        before_website = conn.execute(
            "SELECT website FROM companies WHERE id = ?", (int(existing["id"]),)
        ).fetchone()["website"]

    link = confirm_company_contact_add(
        CrmAddConfirmRequest(
            client_id=1,
            company=CrmAddCompanyInput(
                company_name=existing["company_name"],
                website="https://should-not-overwrite.example.com",
            ),
            contacts=[],
            company_decision="use_existing",
            existing_company_id=int(existing["id"]),
            known_company_id=int(existing["id"]),
            source="unit-test-link",
        )
    )
    if link.activity_id:
        created_activities.append(link.activity_id)
    assert link.company_created is False
    assert link.company_id == int(existing["id"])
    assert link.relationship_created is False
    with get_connection() as conn:
        after_website = conn.execute(
            "SELECT website FROM companies WHERE id = ?", (int(existing["id"]),)
        ).fetchone()["website"]
        after_status = conn.execute(
            """
            SELECT status FROM client_company_relationships
            WHERE client_id = 1 AND company_id = ?
            """,
            (int(existing["id"]),),
        ).fetchone()["status"]
    assert after_website == before_website
    assert after_status == existing_status

    # Ambiguous company requires decision
    try:
        # Force possible_match path by confirming with empty decision on possible
        # Use preview possibles if any; otherwise skip assert with synthetic ValueError path
        from crm_add_data import match_company
        from db import get_connection as gc

        with gc() as conn:
            m = {
                "match_type": "possible_match",
                "company_id": int(existing["id"]),
                "reasons": ["name_exact"],
            }
            from crm_add_data import create_or_link_company

            try:
                create_or_link_company(
                    conn,
                    company=CrmAddCompanyInput(company_name="X"),
                    decision="auto",
                    existing_company_id=None,
                    match=m,
                )
                raise AssertionError("expected ValueError for ambiguous match")
            except ValueError:
                pass

    except Exception:
        raise

    # Contact email dedupe
    if email_contact is not None:
        prev_c = preview_company_contact_add(
            CrmAddPreviewRequest(
                client_id=1,
                company=CrmAddCompanyInput(company_name=existing["company_name"]),
                contacts=[
                    CrmAddContactInput(
                        email=email_contact["email"],
                        full_name="Someone Else",
                    )
                ],
                known_company_id=int(existing["id"]),
            )
        )
        assert prev_c.contacts
        assert prev_c.contacts[0].match_status == "existing"

        dup = confirm_company_contact_add(
            CrmAddConfirmRequest(
                client_id=1,
                company=CrmAddCompanyInput(company_name=existing["company_name"]),
                contacts=[
                    CrmAddContactConfirmItem(
                        contact=CrmAddContactInput(
                            email=email_contact["email"],
                            full_name="Someone Else",
                        ),
                        selected=True,
                        decision="use_existing",
                        matched_contact_id=int(email_contact["id"]),
                    )
                ],
                company_decision="use_existing",
                known_company_id=int(existing["id"]),
                existing_company_id=int(existing["id"]),
            )
        )
        if dup.activity_id:
            created_activities.append(dup.activity_id)
        assert dup.contacts[0]["created"] is False
        assert dup.contacts[0]["contact_id"] == int(email_contact["id"])

    # Other client untouched
    with get_connection() as conn:
        brown_after = conn.execute(
            """
            SELECT id, status FROM client_company_relationships
            WHERE client_id = 2 AND company_id = ?
            """,
            (int(existing["id"]),),
        ).fetchone()
        if brown_before is None:
            assert brown_after is None
        else:
            assert int(brown_after["id"]) == int(brown_before["id"])
            assert brown_after["status"] == brown_before["status"]

    # Cancel = zero writes already covered by preview-only calls
    # Provenance on new company CCR
    with get_connection() as conn:
        notes = conn.execute(
            """
            SELECT notes FROM client_company_relationships
            WHERE client_id = 1 AND company_id = ?
            """,
            (created_companies[0],),
        ).fetchone()["notes"]
    assert "Created from NorthStar AI Research" in notes
    assert "999001" in notes

    _cleanup(created_companies, created_activities)

    with get_connection() as conn:
        after = _counts(conn)
    assert after == before, f"{before} -> {after}"

    print("PASS: crm add preview/confirm/dedupe/NS/CCR/provenance")
    print("DATA COUNTS:", after)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL:", exc, file=sys.stderr)
        try:
            _cleanup([], [])
        except Exception:
            pass
        raise
