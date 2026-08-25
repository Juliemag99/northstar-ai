"""Operational Campaigns page: client scoping, membership, search, and actions.

Run: python test_campaigns.py

Creates and deletes temporary companies, contacts, campaigns, and memberships.
Does not change Flora Jia, Whirlpool, or other production identity values.
"""

from __future__ import annotations

import testdb
import sys
import time

from access import get_default_user
from campaigns_data import (
    add_campaign_company,
    add_campaign_contact,
    create_operational_campaign,
    ensure_campaigns_schema,
    get_campaign_workspace,
    list_campaigns,
    set_campaign_status,
)
from db import get_connection
from models import CampaignCreateRequest, CampaignMemberAddRequest

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSCAM"
CARMECO_ID = 1
BROWN_ID = 2


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _snapshot_flora(conn) -> dict:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, email
        FROM contacts
        WHERE id = ?
        """,
        (FLORA_ID,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _snapshot_whirlpool(conn) -> dict:
    row = conn.execute(
        "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
        (WHIRLPOOL_ID,),
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
        conn.execute("DELETE FROM campaign_companies WHERE notes LIKE ?", (f"{MARKER}%",))
        conn.execute("DELETE FROM campaign_contacts WHERE notes LIKE ?", (f"{MARKER}%",))
        if company_id is not None:
            conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM campaign_companies WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM campaign_contacts WHERE company_id = ?", (company_id,))
            conn.execute(
                "DELETE FROM contacts WHERE company_id = ? AND first_name = 'CampTmp'",
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
    contact_id: int | None = None
    campaign_ids: list[int] = []
    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        whirlpool_before = _snapshot_whirlpool(conn)
        if not flora_before:
            _fail("Flora Jia contact 4631 is missing.")
        companies_before = int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        contacts_before = int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
        stamp = str(int(time.time()))
        record_no = f"{MARKER}-{stamp}"
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, city, state)
            VALUES (?, ?, 'Campville', 'OH')
            """,
            (record_no, f"{MARKER} Shared Co {stamp}"),
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
                company_id, external_record_no, first_name, last_name, title,
                source_row_index
            ) VALUES (?, ?, 'CampTmp', 'Buyer', 'Buyer', 1)
            """,
            (company_id, record_no),
        )
        contact_id = int(cur.lastrowid)
        if int(contact_id) == FLORA_ID:
            _fail("Refusing to use Flora Jia as the test contact.")
        conn.execute(
            """
            INSERT INTO activities (
                client_id, company_id, relationship_id, external_record_no,
                contact_id, activity_type, activity_at, outcome, notes, created_by
            )
            SELECT ?, ?, id, ?, ?, 'Call', datetime('now'), 'Connected', ?, 'NSCAM'
            FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (
                CARMECO_ID,
                company_id,
                record_no,
                contact_id,
                f"{MARKER} carmeco call",
                CARMECO_ID,
                company_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO activities (
                client_id, company_id, relationship_id, external_record_no,
                contact_id, activity_type, activity_at, outcome, notes, created_by
            )
            SELECT ?, ?, id, ?, ?, 'Call', datetime('now'), 'Left message', ?, 'NSCAM'
            FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (
                BROWN_ID,
                company_id,
                f"{record_no}-B",
                contact_id,
                f"{MARKER} brown call",
                BROWN_ID,
                company_id,
            ),
        )
        conn.commit()

    try:
        ensure_campaigns_schema()
        carmeco = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} DC Misc. {stamp}",
                description="Carmeco-only test campaign",
                category="DC Misc.",
                owner_name="Julie Magnani",
                status="Active",
            )
        )
        brown = create_operational_campaign(
            CampaignCreateRequest(
                client_id=BROWN_ID,
                campaign_name=f"{MARKER} Brown Only {stamp}",
                description="Brown-only test campaign",
                category="Industrial",
                owner_name="Julie Magnani",
                status="Active",
            )
        )
        campaign_ids = [carmeco.campaign_id, brown.campaign_id]

        add_campaign_company(
            carmeco.campaign_id,
            CampaignMemberAddRequest(company_id=company_id, notes=f"{MARKER} ccr"),
        )
        add_campaign_contact(
            carmeco.campaign_id,
            CampaignMemberAddRequest(contact_id=contact_id, notes=f"{MARKER} cct"),
        )
        add_campaign_company(
            brown.campaign_id,
            CampaignMemberAddRequest(company_id=company_id, notes=f"{MARKER} bcr"),
        )
        add_campaign_contact(
            brown.campaign_id,
            CampaignMemberAddRequest(contact_id=contact_id, notes=f"{MARKER} bct"),
        )

        with get_connection() as conn:
            companies_after_add = int(
                conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            )
            contacts_after_add = int(
                conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            )
        if companies_after_add != companies_before + 1:
            _fail("Adding a company to two campaigns created a duplicate master company.")
        if contacts_after_add != contacts_before + 1:
            _fail("Adding a contact to two campaigns created a duplicate master contact.")

        carmeco_list = list_campaigns(client_id=CARMECO_ID, q=MARKER, limit=50, offset=0)
        brown_list = list_campaigns(client_id=BROWN_ID, q=MARKER, limit=50, offset=0)
        all_list = list_campaigns(client_id=0, q=MARKER, limit=50, offset=0)

        carmeco_ids = {item.campaign_id for item in carmeco_list.items}
        brown_ids = {item.campaign_id for item in brown_list.items}
        all_ids = {item.campaign_id for item in all_list.items}

        if carmeco.campaign_id not in carmeco_ids:
            _fail("Carmeco view is missing the Carmeco campaign.")
        if brown.campaign_id in carmeco_ids:
            _fail("Carmeco view leaked a Brown Industries campaign.")
        if brown.campaign_id not in brown_ids:
            _fail("Brown view is missing the Brown campaign.")
        if carmeco.campaign_id in brown_ids:
            _fail("Brown view leaked a Carmeco campaign.")
        if carmeco.campaign_id not in all_ids or brown.campaign_id not in all_ids:
            _fail("All My Clients did not include authorized campaigns from both clients.")
        if any(item.client_id not in {CARMECO_ID, BROWN_ID} for item in all_list.items if MARKER in item.campaign_name):
            _fail("All My Clients included a campaign outside Carmeco/Brown test set.")

        tagged = list_campaigns(client_id=CARMECO_ID, q="DC Misc.", limit=50, offset=0)
        if carmeco.campaign_id not in {item.campaign_id for item in tagged.items}:
            _fail("Search did not match campaign name/tag 'DC Misc.'")
        by_tag = list_campaigns(client_id=CARMECO_ID, category="DC Misc.", limit=50, offset=0)
        if carmeco.campaign_id not in {item.campaign_id for item in by_tag.items}:
            _fail("Category filter did not match 'DC Misc.'")

        carmeco_ws = get_campaign_workspace(carmeco.campaign_id)
        brown_ws = get_campaign_workspace(brown.campaign_id)
        if carmeco_ws.campaign.call_count != 0:
            _fail(
                f"Carmeco campaign counted {carmeco_ws.campaign.call_count} pre-assignment calls; expected 0."
            )
        if brown_ws.campaign.call_count != 0:
            _fail(
                f"Brown campaign counted {brown_ws.campaign.call_count} pre-assignment calls; expected 0."
            )
        if any(c.client_id != CARMECO_ID for c in carmeco_ws.companies):
            _fail("Carmeco workspace listed a company membership for another client.")
        if any(c.client_id != BROWN_ID for c in brown_ws.companies):
            _fail("Brown workspace listed a company membership for another client.")

        paused = set_campaign_status(carmeco.campaign_id, "Paused")
        if paused.campaign is None or paused.campaign.status != "Paused":
            _fail("Pause did not set status to Paused.")
        resumed = set_campaign_status(carmeco.campaign_id, "Active")
        if resumed.campaign is None or resumed.campaign.status != "Active":
            _fail("Resume did not set status to Active.")
        completed = set_campaign_status(carmeco.campaign_id, "Completed")
        if completed.campaign is None or completed.campaign.status != "Completed":
            _fail("Complete did not set status to Completed.")
        archived = set_campaign_status(carmeco.campaign_id, "Archived")
        if archived.campaign is None or archived.campaign.status != "Archived":
            _fail("Archive did not set status to Archived.")

        status, payload = testdb.http_json("GET", f"/api/campaigns?client_id={CARMECO_ID}&q={MARKER}")
        if status != 200:
            _fail(f"GET /api/campaigns Carmeco failed ({status}): {payload}")
        http_ids = {int(item["campaign_id"]) for item in payload.get("items") or []}
        if carmeco.campaign_id not in http_ids or brown.campaign_id in http_ids:
            _fail("HTTP Carmeco list did not stay client-scoped.")

        status, payload = testdb.http_json("GET", f"/api/campaigns?client_id={BROWN_ID}&q={MARKER}")
        if status != 200:
            _fail(f"GET /api/campaigns Brown failed ({status}): {payload}")
        http_ids = {int(item["campaign_id"]) for item in payload.get("items") or []}
        if brown.campaign_id not in http_ids or carmeco.campaign_id in http_ids:
            _fail("HTTP Brown list did not stay client-scoped.")

        status, payload = testdb.http_json("GET", f"/api/campaigns?client_id=0&q={MARKER}")
        if status != 200:
            _fail(f"GET /api/campaigns all clients failed ({status}): {payload}")
        http_ids = {int(item["campaign_id"]) for item in payload.get("items") or []}
        if carmeco.campaign_id not in http_ids or brown.campaign_id not in http_ids:
            _fail("HTTP All My Clients list missed an authorized campaign.")
        clients_seen = {int(item["client_id"]) for item in payload.get("items") or []}
        if CARMECO_ID not in clients_seen or BROWN_ID not in clients_seen:
            _fail("HTTP All My Clients did not include a Client column source for both clients.")

        status, payload = testdb.http_json("GET", f"/api/campaigns/{carmeco.campaign_id}")
        if status != 200:
            _fail(f"GET campaign workspace failed ({status}): {payload}")
        companies = payload.get("companies") or []
        contacts = payload.get("contacts") or []
        if not companies or int(companies[0]["company_id"]) != company_id:
            _fail("Campaign workspace did not include the assigned company.")
        if not contacts or int(contacts[0]["contact_id"]) != contact_id:
            _fail("Campaign workspace did not include the assigned contact.")

        paged = list_campaigns(client_id=0, q=MARKER, limit=1, offset=0)
        if paged.limit != 1 or paged.total < 2 or len(paged.items) != 1:
            _fail("Server-side pagination did not limit campaign results.")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            whirlpool_after = _snapshot_whirlpool(conn)
        if flora_after != flora_before:
            _fail("Flora Jia production contact was modified.")
        if whirlpool_after != whirlpool_before:
            _fail("Whirlpool production company was modified.")
    finally:
        _cleanup(company_id, campaign_ids)

    print("test_campaigns: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
