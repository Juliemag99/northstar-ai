"""Campaign routing: recommend, confirm, defer, unassigned, post-assignment stats.

Run: python test_campaign_routing.py

Creates and deletes temporary companies, campaigns, memberships, and activities.
Does not change Flora Jia, Whirlpool, or other production identity values.
"""

from __future__ import annotations

import testdb
import sys
import time

from access import get_default_user
from activities_data import list_client_activities
from campaigns_data import (
    confirm_campaign_route,
    create_operational_campaign,
    defer_campaign_route,
    ensure_campaigns_schema,
    get_campaign_workspace,
    list_unassigned_opportunities,
    suggest_campaign_route,
)
from db import get_connection
from models import (
    CampaignCreateRequest,
    CampaignRouteConfirmRequest,
    CampaignRouteDeferRequest,
)

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSCROUT"
CARMECO_ID = 1
BROWN_ID = 2


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _snapshot_flora(conn) -> dict:
    row = conn.execute(
        "SELECT id, company_id, first_name, last_name, title, phone, email FROM contacts WHERE id = ?",
        (FLORA_ID,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _cleanup(company_id: int | None, campaign_ids: list[int]) -> None:
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
                "DELETE FROM contacts WHERE company_id = ? AND first_name = 'RoutTmp'",
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


def main() -> int:
    user = get_default_user()
    if user is None:
        _fail("Default user not found.")

    company_id: int | None = None
    campaign_ids: list[int] = []
    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        companies_before = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        stamp = str(int(time.time()))
        record_no = f"{MARKER}-{stamp}"
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, city, state)
            VALUES (?, ?, 'Routeville', 'OH')
            """,
            (record_no, f"{MARKER} Co {stamp}"),
        )
        company_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action
            ) VALUES (?, ?, ?, 'Hot Prospect', 'Call')
            """,
            (CARMECO_ID, company_id, record_no),
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action
            ) VALUES (?, ?, ?, 'Brown Status', '')
            """,
            (BROWN_ID, company_id, f"{record_no}-B"),
        )
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title, source_row_index
            ) VALUES (?, ?, 'RoutTmp', 'Buyer', 'Buyer', 1)
            """,
            (company_id, record_no),
        )
        contact_id = int(cur.lastrowid)
        if contact_id == FLORA_ID:
            _fail("Refusing to use Flora Jia as the test contact.")
        rel = conn.execute(
            "SELECT id FROM client_company_relationships WHERE client_id = ? AND company_id = ?",
            (CARMECO_ID, company_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO activities (
                client_id, company_id, relationship_id, external_record_no,
                contact_id, activity_type, activity_at, outcome, notes, created_by
            ) VALUES (?, ?, ?, ?, ?, 'Call', datetime('now', '-1 day'), 'Old', ?, 'NSCROUT')
            """,
            (CARMECO_ID, company_id, int(rel["id"]), record_no, contact_id, f"{MARKER} before"),
        )
        conn.commit()

    try:
        ensure_campaigns_schema()
        camp = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Stamping {stamp}",
                description="Metal stamping",
                category="Stamping",
                status="Active",
            )
        )
        brown_camp = create_operational_campaign(
            CampaignCreateRequest(
                client_id=BROWN_ID,
                campaign_name=f"{MARKER} Brown {stamp}",
                description="Brown-only",
                status="Active",
            )
        )
        campaign_ids = [camp.campaign_id, brown_camp.campaign_id]

        with get_connection() as conn:
            rel = conn.execute(
                "SELECT id FROM client_company_relationships WHERE client_id = ? AND company_id = ?",
                (CARMECO_ID, company_id),
            ).fetchone()
            oa_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
            ).fetchone()
            if oa_exists and rel is not None:
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
                        ) VALUES (?, ?, ?, 'Cross-Client Opportunity', 0, ?, 'NSCROUT', datetime('now'), NULL)
                        ON CONFLICT(target_client_id, company_id) DO UPDATE SET
                            target_campaign_id = NULL
                        """,
                        (CARMECO_ID, company_id, int(rel["id"]), f"{MARKER} oa"),
                    )
                    conn.commit()
        null_campaign = list_unassigned_opportunities(CARMECO_ID)
        if not any(item.company_id == company_id and item.campaign_id is None for item in null_campaign.items):
            _fail("Opportunity with campaign_id null was missing from Unassigned Opportunities.")
        if sum(1 for item in null_campaign.items if item.company_id == company_id) != 1:
            _fail("Unassigned list duplicated a null-campaign opportunity.")

        suggestion = suggest_campaign_route(
            client_id=CARMECO_ID,
            company_id=company_id,
            contact_id=contact_id,
            source="status",
        )
        if not suggestion.should_prompt:
            _fail("Expected a campaign prompt for an unassigned Carmeco opportunity.")
        choice_ids = {c.campaign_id for c in suggestion.campaigns}
        if camp.campaign_id not in choice_ids:
            _fail("Carmeco prompt did not include the Active test campaign.")
        if suggestion.recommended_campaign_id not in choice_ids:
            _fail("Recommended campaign was not a selectable Carmeco campaign.")
        if any(c.campaign_id == brown_camp.campaign_id for c in suggestion.campaigns):
            _fail("Carmeco prompt leaked a Brown campaign.")

        again = suggest_campaign_route(
            client_id=CARMECO_ID,
            company_id=company_id,
            source="status",
        )
        if not again.should_prompt:
            _fail("Prompt should repeat until assigned or deferred.")

        deferred = defer_campaign_route(
            CampaignRouteDeferRequest(
                client_id=CARMECO_ID,
                company_id=company_id,
                contact_id=contact_id,
                source="not_now",
            )
        )
        if not deferred.auto_unassigned:
            _fail("Not now did not place the opportunity in Unassigned.")
        unassigned = list_unassigned_opportunities(CARMECO_ID)
        row = next((item for item in unassigned.items if item.company_id == company_id), None)
        if row is None:
            _fail("Carmeco Unassigned list missing the deferred company.")
        if row.campaign_id is not None:
            _fail("Unassigned opportunity still had a campaign_id.")
        if row.contact_id != contact_id:
            _fail("Unassigned list did not keep the existing contact.")
        if (row.status or "").strip() != "Hot Prospect":
            _fail("Unassigned list missing company status.")
        if not row.recommended_campaign_id:
            _fail("Unassigned list missing a recommended Active campaign.")
        if row.recommended_campaign_id != camp.campaign_id and row.recommended_campaign_id not in {
            c.campaign_id for c in unassigned.campaigns if c.client_id == CARMECO_ID
        }:
            _fail("Recommended campaign was not an Active Carmeco campaign.")
        if any(c.client_id == BROWN_ID for c in unassigned.campaigns):
            _fail("Carmeco Unassigned campaign selector leaked Brown campaigns.")
        if sum(1 for item in unassigned.items if item.company_id == company_id) != 1:
            _fail("Unassigned list duplicated the same opportunity.")
        brown_unassigned = list_unassigned_opportunities(BROWN_ID)
        if any(item.company_id == company_id for item in brown_unassigned.items):
            _fail("Brown Unassigned list leaked a Carmeco-only deferral.")
        all_unassigned = list_unassigned_opportunities(0)
        all_row = next((item for item in all_unassigned.items if item.company_id == company_id), None)
        if all_row is None or (all_row.client_name or "").strip() == "":
            _fail("All My Clients Unassigned list missing the client name.")

        confirmed = confirm_campaign_route(
            CampaignRouteConfirmRequest(
                client_id=CARMECO_ID,
                company_id=company_id,
                contact_id=contact_id,
                campaign_id=camp.campaign_id,
                source="confirm",
            )
        )
        if camp.campaign_id not in confirmed.assigned_campaign_ids:
            _fail("Confirm did not assign the company to the campaign.")
        after_prompt = suggest_campaign_route(
            client_id=CARMECO_ID,
            company_id=company_id,
            source="status",
        )
        if after_prompt.should_prompt:
            _fail("Prompted again after the company was already assigned.")
        leftover = list_unassigned_opportunities(CARMECO_ID)
        if any(item.company_id == company_id for item in leftover.items):
            _fail("Assigned company remained on Unassigned Opportunities.")
        assigned_acts = list_client_activities(
            CARMECO_ID, activity_type="Campaign Assignment", q=MARKER, limit=20
        )
        route_assigns = [item for item in assigned_acts.items if item.company_id == company_id]
        if len(route_assigns) != 1:
            _fail(f"Routed assign created {len(route_assigns)} Campaign Assignment rows; expected 1.")
        if route_assigns[0].contact_id != contact_id:
            _fail("Combined Campaign Assignment is missing the routed contact.")
        if "Source:" not in str(route_assigns[0].notes) or "Carmeco" not in str(route_assigns[0].notes):
            _fail("Combined Campaign Assignment is missing client or source.")

        status, payload = testdb.http_json(
            "GET",
            f"/api/campaigns/unassigned?client_id={CARMECO_ID}",
        )
        if status != 200:
            _fail(f"Unassigned HTTP list failed ({status}): {payload}")
        if any(int(item.get("company_id") or 0) == company_id for item in payload.get("items") or []):
            _fail("HTTP Unassigned list still included the assigned company.")

        ws = get_campaign_workspace(camp.campaign_id)
        if ws.campaign.call_count != 0:
            _fail("Campaign totals counted activity from before assignment.")

        with get_connection() as conn:
            rel = conn.execute(
                "SELECT id FROM client_company_relationships WHERE client_id = ? AND company_id = ?",
                (CARMECO_ID, company_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO activities (
                    client_id, company_id, relationship_id, external_record_no,
                    contact_id, activity_type, activity_at, outcome, notes, created_by
                ) VALUES (?, ?, ?, ?, ?, 'Call', datetime('now', '+1 hour'), 'New', ?, 'NSCROUT')
                """,
                (CARMECO_ID, company_id, int(rel["id"]), record_no, contact_id, f"{MARKER} after"),
            )
            conn.commit()
        ws_after = get_campaign_workspace(camp.campaign_id)
        if ws_after.campaign.call_count != 1:
            _fail(
                f"Campaign totals expected 1 post-assignment call, got {ws_after.campaign.call_count}."
            )

        with get_connection() as conn:
            companies_after = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
            flora_after = _snapshot_flora(conn)
        if companies_after != companies_before + 1:
            _fail("Routing created a duplicate master company.")
        if flora_after != flora_before:
            _fail("Flora Jia production contact was modified.")

        status, payload = testdb.http_json(
            "GET",
            f"/api/campaigns/route?client_id={BROWN_ID}&company_id={company_id}",
        )
        if status != 200:
            _fail(f"Brown route suggest failed ({status}): {payload}")
        rec = payload.get("recommended_campaign_id")
        if rec == camp.campaign_id:
            _fail("Brown routing recommended a Carmeco campaign.")
        names = [c.get("campaign_name") for c in payload.get("campaigns") or []]
        if any(MARKER in str(n) and "Brown" not in str(n) for n in names):
            _fail("Brown campaign selector included a Carmeco test campaign.")
    finally:
        _cleanup(company_id, campaign_ids)

    print("test_campaign_routing: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
