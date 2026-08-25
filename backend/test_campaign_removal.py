"""Campaign membership removal: atomic company unassign and contact rules.

Run: python test_campaign_removal.py

Creates and deletes temporary companies, campaigns, memberships, and activities.
Does not change Flora Jia, Whirlpool, AGCO, Trailerman, or other production rows.
"""

from __future__ import annotations

import testdb
import sys
import time

from access import get_default_user
from activities_data import list_client_activities
from campaigns_data import (
    add_campaign_company,
    add_campaign_contact,
    confirm_campaign_route,
    create_operational_campaign,
    ensure_campaigns_schema,
    get_campaign_workspace,
    list_unassigned_opportunities,
    remove_campaign_company,
    remove_campaign_contact,
)
from db import get_connection
from models import (
    CampaignCreateRequest,
    CampaignMemberAddRequest,
    CampaignRouteConfirmRequest,
)

FLORA_ID = 4631
WHIRLPOOL_ID = 298
AGCO_ID = 205
TRAILERMAN_ID = 18
MARKER = "NSCREM"
CARMECO_ID = 1


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _snapshot_flora(conn) -> dict:
    row = conn.execute(
        "SELECT id, company_id, first_name, last_name, title, phone, email FROM contacts WHERE id = ?",
        (FLORA_ID,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _snapshot_protected(conn) -> dict:
    names = conn.execute(
        """
        SELECT co.id, co.company_name,
               (SELECT COUNT(*) FROM campaign_companies cc WHERE cc.company_id = co.id) AS camp_n
        FROM companies co
        WHERE co.id IN (?, ?, ?, ?)
        ORDER BY co.id
        """,
        (TRAILERMAN_ID, AGCO_ID, WHIRLPOOL_ID, FLORA_ID),
    ).fetchall()
    flora = _snapshot_flora(conn)
    return {"rows": [dict(r) for r in names], "flora": flora}


def _cleanup(company_id: int | None, campaign_ids: list[int], extra_contact_ids: list[int]) -> None:
    with get_connection() as conn:
        if campaign_ids:
            placeholders = ",".join("?" * len(campaign_ids))
            conn.execute(
                f"DELETE FROM campaign_companies WHERE campaign_id IN ({placeholders})",
                campaign_ids,
            )
            conn.execute(
                f"DELETE FROM campaign_contacts WHERE campaign_id IN ({placeholders})",
                campaign_ids,
            )
            conn.execute(
                f"DELETE FROM client_campaigns WHERE id IN ({placeholders}) AND campaign_name LIKE ?",
                [*campaign_ids, f"{MARKER}%"],
            )
        conn.execute("DELETE FROM client_campaigns WHERE campaign_name LIKE ?", (f"{MARKER}%",))
        if extra_contact_ids:
            cph = ",".join("?" * len(extra_contact_ids))
            conn.execute(f"DELETE FROM campaign_contacts WHERE contact_id IN ({cph})", extra_contact_ids)
            conn.execute(
                f"DELETE FROM contacts WHERE id IN ({cph}) AND first_name = 'RemTmp'",
                extra_contact_ids,
            )
        if company_id is not None:
            conn.execute("DELETE FROM campaign_unassigned WHERE company_id = ?", (company_id,))
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
            ).fetchone():
                conn.execute("DELETE FROM opportunity_assignments WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM campaign_companies WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM campaign_contacts WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
            conn.execute(
                "DELETE FROM contacts WHERE company_id = ? AND first_name = 'RemTmp'",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM companies WHERE id = ? AND company_name LIKE ?",
                (company_id, f"{MARKER}%"),
            )
        conn.commit()


def _unassigned_ids(client_id: int) -> set[int]:
    listed = list_unassigned_opportunities(client_id, q=MARKER, limit=50)
    return {item.company_id for item in listed.items}


def main() -> int:
    user = get_default_user()
    if user is None:
        _fail("Default user not found.")

    company_id: int | None = None
    campaign_ids: list[int] = []
    extra_contact_ids: list[int] = []
    with get_connection() as conn:
        protected_before = _snapshot_protected(conn)
        companies_before = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        stamp = str(int(time.time()))
        record_no = f"{MARKER}-{stamp}"
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, city, state)
            VALUES (?, ?, 'Removeville', 'OH')
            """,
            (record_no, f"{MARKER} Co {stamp}"),
        )
        company_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action
            ) VALUES (?, ?, ?, 'Hot Prospect', 'Call')
            """,
            (CARMECO_ID, company_id, record_no),
        )
        relationship_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title, source_row_index
            ) VALUES (?, ?, 'RemTmp', 'Primary', 'Buyer', 1)
            """,
            (company_id, record_no),
        )
        primary_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title, source_row_index
            ) VALUES (?, ?, 'RemTmp', 'Extra', 'Engineer', 2)
            """,
            (company_id, f"{record_no}-x"),
        )
        extra_id = int(cur.lastrowid)
        extra_contact_ids = [primary_id, extra_id]
        if FLORA_ID in extra_contact_ids:
            _fail("Refusing to use Flora Jia as the test contact.")
        oa_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
        ).fetchone()
        if oa_exists:
            oa_cols = {
                str(r[1]) for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
            }
            if "target_campaign_id" in oa_cols:
                conn.execute(
                    """
                    INSERT INTO opportunity_assignments (
                        target_client_id, company_id, relationship_id,
                        originated_from, opportunity_score, source_summary,
                        created_by, created_at, target_campaign_id
                    ) VALUES (?, ?, ?, 'Cross-Client Opportunity', 0, ?, 'NSCREM', datetime('now'), NULL)
                    """,
                    (CARMECO_ID, company_id, relationship_id, f"{MARKER} oa"),
                )
        conn.commit()

    try:
        ensure_campaigns_schema()
        camp = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Stamping {stamp}",
                description="Removal tests",
                category="Stamping",
                status="Active",
            )
        )
        other = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Other {stamp}",
                description="Second campaign",
                status="Active",
            )
        )
        campaign_ids = [camp.campaign_id, other.campaign_id]

        confirm_campaign_route(
            CampaignRouteConfirmRequest(
                client_id=CARMECO_ID,
                company_id=company_id,
                contact_id=primary_id,
                campaign_id=camp.campaign_id,
                source="Cross-Client Opportunity",
            )
        )
        add_campaign_contact(
            camp.campaign_id,
            CampaignMemberAddRequest(contact_id=extra_id, notes=MARKER),
        )
        assigns = list_client_activities(
            CARMECO_ID, activity_type="Campaign Assignment", q=MARKER, limit=20
        )
        company_assigns = [item for item in assigns.items if item.company_id == company_id]
        if len(company_assigns) != 2:
            _fail(
                f"Expected one routed assignment plus one extra-contact assignment, got {len(company_assigns)}."
            )
        if sum(1 for item in company_assigns if item.contact_id == primary_id) != 1:
            _fail("Routed company+contact assign did not log a single combined activity.")
        if sum(1 for item in company_assigns if item.contact_id == extra_id) != 1:
            _fail("Independent extra-contact add did not log its own activity.")
        ws = get_campaign_workspace(camp.campaign_id)
        by_contact = {row.contact_id: row for row in ws.contacts}
        if not by_contact[primary_id].is_primary_routed_contact:
            _fail("Routed contact was not marked primary.")
        if by_contact[extra_id].is_primary_routed_contact:
            _fail("Secondary contact was marked primary.")

        status, before_remove = testdb.http_json(
            "GET", f"/api/campaigns/{camp.campaign_id}"
        )
        if status != 200:
            _fail(f"Workspace GET failed: {status} {before_remove}")

        extra_ws = remove_campaign_contact(camp.campaign_id, extra_id)
        extra_ids = {row.contact_id for row in extra_ws.contacts}
        if extra_id in extra_ids:
            _fail("Non-primary contact membership was not removed.")
        if primary_id not in extra_ids:
            _fail("Primary contact was removed with a secondary contact.")
        if company_id not in {row.company_id for row in extra_ws.companies}:
            _fail("Company membership was removed when deleting a secondary contact.")
        if company_id in _unassigned_ids(CARMECO_ID):
            _fail("Company returned to unassigned after a secondary-contact removal.")

        status, payload = testdb.http_json(
            "DELETE",
            f"/api/campaigns/{camp.campaign_id}/companies/{company_id}",
        )
        if status != 200:
            _fail(f"Company DELETE failed: {status} {payload}")
        gone = get_campaign_workspace(camp.campaign_id)
        if any(row.company_id == company_id for row in gone.companies):
            _fail("Company membership remained after confirmed removal.")
        if any(row.company_id == company_id for row in gone.contacts):
            _fail("Company campaign contacts remained after company removal.")
        if company_id not in _unassigned_ids(CARMECO_ID):
            _fail("Removed opportunity did not return to Unassigned Opportunities.")

        with get_connection() as conn:
            oa = conn.execute(
                """
                SELECT target_campaign_id FROM opportunity_assignments
                WHERE target_client_id = ? AND company_id = ?
                """,
                (CARMECO_ID, company_id),
            ).fetchone()
            unassigned = conn.execute(
                """
                SELECT contact_id, source FROM campaign_unassigned
                WHERE client_id = ? AND company_id = ?
                """,
                (CARMECO_ID, company_id),
            ).fetchone()
        if oa is not None and oa["target_campaign_id"] not in (None, 0, ""):
            _fail("Opportunity assignment still pointed at the campaign.")
        if unassigned is None:
            _fail("campaign_unassigned row was not created.")
        if int(unassigned["contact_id"] or 0) != primary_id:
            _fail("Unassigned row did not keep the routed contact.")

        removed = list_client_activities(
            CARMECO_ID, activity_type="Campaign Removal", q=MARKER, limit=20
        )
        company_removals = [
            item
            for item in removed.items
            if item.company_id == company_id
            and "Company membership unchanged" not in str(item.notes)
        ]
        if len(company_removals) != 1:
            _fail(f"Expected one company Campaign Removal activity, got {len(company_removals)}.")
        notes = str(company_removals[0].notes)
        if "RemTmp Primary" not in notes or camp.campaign_name not in notes or "Carmeco" not in notes:
            _fail("Campaign Removal activity is missing company, contact, campaign, or client.")
        if not company_removals[0].user_name or not company_removals[0].activity_at:
            _fail("Campaign Removal activity is missing user or timestamp.")

        confirm_campaign_route(
            CampaignRouteConfirmRequest(
                client_id=CARMECO_ID,
                company_id=company_id,
                contact_id=primary_id,
                campaign_id=camp.campaign_id,
                source="Cross-Client Opportunity",
            )
        )
        add_campaign_contact(
            camp.campaign_id,
            CampaignMemberAddRequest(contact_id=extra_id, notes=MARKER),
        )
        remove_campaign_contact(camp.campaign_id, extra_id)
        primary_ws = remove_campaign_contact(camp.campaign_id, primary_id)
        if any(row.company_id == company_id for row in primary_ws.companies):
            _fail("Removing the routed contact left the company in the campaign.")
        if company_id not in _unassigned_ids(CARMECO_ID):
            _fail("Removing the routed contact did not return the opportunity to unassigned.")

        confirm_campaign_route(
            CampaignRouteConfirmRequest(
                client_id=CARMECO_ID,
                company_id=company_id,
                contact_id=primary_id,
                campaign_id=camp.campaign_id,
                source="Cross-Client Opportunity",
            )
        )
        add_campaign_company(
            other.campaign_id,
            CampaignMemberAddRequest(company_id=company_id, notes=MARKER),
        )
        remove_campaign_company(camp.campaign_id, company_id)
        still = get_campaign_workspace(other.campaign_id)
        if company_id not in {row.company_id for row in still.companies}:
            _fail("Removing from one campaign dropped membership in another campaign.")
        if company_id in _unassigned_ids(CARMECO_ID):
            _fail("Company was marked unassigned while still in another campaign.")

        with get_connection() as conn:
            protected_after = _snapshot_protected(conn)
            companies_after = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        if protected_after != protected_before:
            _fail("Protected production companies or Flora Jia changed.")
        if companies_after != companies_before + 1:
            _fail("Removal tests created or deleted master companies unexpectedly.")
        print("test_campaign_removal: ok")
        return 0
    finally:
        _cleanup(company_id, campaign_ids, extra_contact_ids)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"test_campaign_removal: FAIL {exc}", file=sys.stderr)
        raise
