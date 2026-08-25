"""Bulk assign unassigned opportunities and campaign membership activity.

Run: python test_campaign_bulk_assign.py

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
    add_campaign_company,
    bulk_assign_unassigned,
    create_operational_campaign,
    ensure_campaigns_schema,
    get_campaign_workspace,
    list_unassigned_opportunities,
    remove_campaign_company,
)
from db import get_connection
from models import (
    CampaignCreateRequest,
    CampaignMemberAddRequest,
    UnassignedBulkAssignRequest,
)

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSCBULK"
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


def _cleanup(company_ids: list[int], campaign_ids: list[int]) -> None:
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
        conn.execute("DELETE FROM activities WHERE notes LIKE ?", (f"%{MARKER}%",))
        conn.execute("DELETE FROM activities WHERE outcome LIKE ?", (f"%{MARKER}%",))
        if company_ids:
            placeholders = ",".join("?" * len(company_ids))
            conn.execute(f"DELETE FROM campaign_unassigned WHERE company_id IN ({placeholders})", company_ids)
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
            ).fetchone():
                conn.execute(
                    f"DELETE FROM opportunity_assignments WHERE company_id IN ({placeholders})",
                    company_ids,
                )
            conn.execute(f"DELETE FROM campaign_companies WHERE company_id IN ({placeholders})", company_ids)
            conn.execute(f"DELETE FROM campaign_contacts WHERE company_id IN ({placeholders})", company_ids)
            conn.execute(f"DELETE FROM activities WHERE company_id IN ({placeholders})", company_ids)
            conn.execute(
                f"DELETE FROM contacts WHERE company_id IN ({placeholders}) AND first_name = 'BulkTmp'",
                company_ids,
            )
            conn.execute(
                f"DELETE FROM client_company_relationships WHERE company_id IN ({placeholders})",
                company_ids,
            )
            conn.execute(
                f"DELETE FROM companies WHERE id IN ({placeholders}) AND company_name LIKE ?",
                [*company_ids, f"{MARKER}%"],
            )
        conn.commit()


def _insert_company(conn, *, client_id: int, stamp: str, suffix: str) -> tuple[int, int]:
    record_no = f"{MARKER}-{stamp}-{suffix}"
    cur = conn.execute(
        """
        INSERT INTO companies (external_record_no, company_name, city, state)
        VALUES (?, ?, 'Bulkville', 'OH')
        """,
        (record_no, f"{MARKER} {suffix} {stamp}"),
    )
    company_id = int(cur.lastrowid)
    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, external_record_no, status, next_action
        ) VALUES (?, ?, ?, 'Hot Prospect', 'Call')
        """,
        (client_id, company_id, record_no),
    )
    relationship_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name, title, source_row_index
        ) VALUES (?, ?, 'BulkTmp', ?, 'Buyer', 1)
        """,
        (company_id, record_no, suffix),
    )
    return company_id, relationship_id


def _insert_oa(conn, client_id: int, company_id: int, relationship_id: int) -> None:
    oa_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
    ).fetchone()
    if not oa_exists:
        return
    oa_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()}
    if "target_campaign_id" not in oa_cols:
        return
    conn.execute(
        """
        INSERT INTO opportunity_assignments (
            target_client_id, company_id, relationship_id,
            originated_from, opportunity_score, source_summary,
            created_by, created_at, target_campaign_id
        ) VALUES (?, ?, ?, 'Cross-Client Opportunity', 0, ?, 'NSCBULK', datetime('now'), NULL)
        ON CONFLICT(target_client_id, company_id) DO UPDATE SET
            target_campaign_id = NULL
        """,
        (client_id, company_id, relationship_id, f"{MARKER} oa"),
    )


def main() -> int:
    user = get_default_user()
    if user is None:
        _fail("Default user not found.")

    company_ids: list[int] = []
    campaign_ids: list[int] = []
    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        companies_before = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        stamp = str(int(time.time()))
        for suffix, client_id in (("A", CARMECO_ID), ("B", CARMECO_ID), ("C", CARMECO_ID), ("Br", BROWN_ID)):
            company_id, rel_id = _insert_company(conn, client_id=client_id, stamp=stamp, suffix=suffix)
            if company_id == WHIRLPOOL_ID:
                _fail("Refusing to reuse Whirlpool as a test company.")
            company_ids.append(company_id)
            _insert_oa(conn, client_id, company_id, rel_id)
        conn.commit()

    carmeco_ids = company_ids[:3]
    brown_id = company_ids[3]
    try:
        ensure_campaigns_schema()
        camp = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Stamping {stamp}",
                status="Active",
            )
        )
        other = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Other {stamp}",
                status="Active",
            )
        )
        brown_camp = create_operational_campaign(
            CampaignCreateRequest(
                client_id=BROWN_ID,
                campaign_name=f"{MARKER} Brown {stamp}",
                status="Active",
            )
        )
        campaign_ids = [camp.campaign_id, other.campaign_id, brown_camp.campaign_id]

        listed = list_unassigned_opportunities(CARMECO_ID, q=MARKER, limit=50, offset=0)
        if listed.total < 3:
            _fail(f"Expected at least 3 Carmeco unassigned rows, got {listed.total}.")
        page1 = list_unassigned_opportunities(CARMECO_ID, q=MARKER, limit=1, offset=0)
        if page1.total < 3 or len(page1.items) != 1:
            _fail("Unassigned pagination did not return one row on page 1.")

        mixed = testdb.http_json(
            "POST",
            "/api/campaigns/unassigned/assign",
            {
                "client_id": 0,
                "campaign_id": camp.campaign_id,
                "company_ids": carmeco_ids[:2],
            },
        )
        if mixed[0] != 400:
            _fail(f"All My Clients bulk assign should be 400, got {mixed[0]}: {mixed[1]}")

        wrong_client = testdb.http_json(
            "POST",
            "/api/campaigns/unassigned/assign",
            {
                "client_id": CARMECO_ID,
                "campaign_id": brown_camp.campaign_id,
                "company_ids": carmeco_ids[:1],
            },
        )
        if wrong_client[0] not in {400, 403}:
            _fail(
                f"Carmeco bulk assign to a Brown campaign should be rejected, got {wrong_client[0]}: {wrong_client[1]}"
            )

        result = bulk_assign_unassigned(
            UnassignedBulkAssignRequest(
                client_id=CARMECO_ID,
                campaign_id=camp.campaign_id,
                company_ids=[carmeco_ids[0], carmeco_ids[1], brown_id],
                q=MARKER,
            )
        )
        if result.assigned != 2:
            _fail(f"Expected 2 assigned, got {result.assigned}.")
        if result.skipped < 1:
            _fail("Brown company should have been skipped for a Carmeco bulk assign.")
        leftover = list_unassigned_opportunities(CARMECO_ID, q=MARKER, limit=50)
        leftover_ids = {item.company_id for item in leftover.items}
        if carmeco_ids[0] in leftover_ids or carmeco_ids[1] in leftover_ids:
            _fail("Assigned Carmeco companies remained unassigned.")
        if carmeco_ids[2] not in leftover_ids:
            _fail("Unselected Carmeco company disappeared from unassigned.")

        first_bulk = list_client_activities(CARMECO_ID, activity_type="Campaign Bulk Assignment", q=MARKER, limit=20)
        if len(first_bulk.items) != 1:
            _fail(f"Expected one bulk assignment activity after the first save, got {len(first_bulk.items)}.")
        if str(first_bulk.items[0].notes).count("•") < 2:
            _fail("Bulk assignment activity is missing accessible company details.")

        again = bulk_assign_unassigned(
            UnassignedBulkAssignRequest(
                client_id=CARMECO_ID,
                campaign_id=camp.campaign_id,
                company_ids=[carmeco_ids[0]],
                q=MARKER,
            )
        )
        if again.assigned != 0 or again.skipped < 1:
            _fail("Already-assigned company should be skipped, not duplicated.")

        matching = bulk_assign_unassigned(
            UnassignedBulkAssignRequest(
                client_id=CARMECO_ID,
                campaign_id=camp.campaign_id,
                select_all_matching=True,
                q=MARKER,
            )
        )
        if matching.assigned < 1:
            _fail("Select-all matching did not assign the remaining Carmeco opportunity.")

        ws = get_campaign_workspace(camp.campaign_id)
        member_ids = {row.company_id for row in ws.companies}
        if set(carmeco_ids) - member_ids:
            _fail("Campaign membership missing assigned Carmeco companies.")
        if brown_id in member_ids:
            _fail("Brown company was added to a Carmeco campaign.")
        if ws.campaign.company_count != 3:
            _fail(f"Campaign totals expected 3 companies, got {ws.campaign.company_count}.")

        activities = list_client_activities(CARMECO_ID, q=MARKER, limit=50)
        bulk_rows = [item for item in activities.items if item.activity_type == "Campaign Bulk Assignment"]
        if len(bulk_rows) != 2:
            _fail(f"Expected two bulk assignment summaries, got {len(bulk_rows)}.")
        per_company = [
            item
            for item in activities.items
            if item.activity_type == "Campaign Assignment" and item.company_id in set(carmeco_ids)
        ]
        if per_company:
            _fail("Bulk assign created per-company timeline entries.")

        add_campaign_company(
            other.campaign_id,
            CampaignMemberAddRequest(company_id=carmeco_ids[0], notes=MARKER),
        )
        moved = list_client_activities(CARMECO_ID, activity_type="Campaign Reassignment", q=MARKER, limit=20)
        if not any(item.company_id == carmeco_ids[0] for item in moved.items):
            _fail("Adding a company already in another campaign did not log reassignment.")

        remove_campaign_company(camp.campaign_id, carmeco_ids[2])
        removed = list_client_activities(CARMECO_ID, activity_type="Campaign Removal", q=MARKER, limit=20)
        if not any(item.company_id == carmeco_ids[2] for item in removed.items):
            _fail("Campaign removal was not logged.")

        brown_acts = list_client_activities(BROWN_ID, q=MARKER, limit=20)
        if any(item.company_id in set(carmeco_ids) for item in brown_acts.items):
            _fail("Carmeco campaign activity leaked onto Brown.")

        with get_connection() as conn:
            companies_after = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
            flora_after = _snapshot_flora(conn)
            extra_contacts = conn.execute(
                "SELECT COUNT(*) AS n FROM contacts WHERE company_id IN (?, ?, ?, ?) AND first_name = 'BulkTmp'",
                company_ids,
            ).fetchone()
        if companies_after != companies_before + 4:
            _fail("Bulk assign created or deleted master companies.")
        if flora_after != flora_before:
            _fail("Flora Jia production contact was modified.")
        if int(extra_contacts["n"]) != 4:
            _fail("Bulk assign duplicated or lost temporary contacts.")
    finally:
        _cleanup(company_ids, campaign_ids)

    print("test_campaign_bulk_assign: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
