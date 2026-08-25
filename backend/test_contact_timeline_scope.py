"""Contact Workspace timeline must be scoped to the current contact_id.

Run: python test_contact_timeline_scope.py

Creates/deletes temporary rows only. Does not change Flora Jia workflow values
or delete existing research notes.
"""

from __future__ import annotations

import testdb
import json
import sys
import time
import urllib.error
import urllib.request

from activities_data import create_activity
from client_workspace_data import get_contact_workspace
from db import get_connection
from models import ActivityCreateRequest

FLORA_ID = 4631
API = "http://127.0.0.1:8007"
MARKER = "NSWFTLTEST"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _flora_workflow_snapshot(conn) -> dict:
    contact = conn.execute(
        """
        SELECT id, first_name, last_name, title, phone, email, company_id,
               external_record_no
        FROM contacts WHERE id = ?
        """,
        (FLORA_ID,),
    ).fetchone()
    if contact is None:
        _fail("Flora Jia contact 4631 not found.")
    ccrs = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
            FROM client_company_relationships
            WHERE company_id = ?
            ORDER BY client_id
            """,
            (int(contact["company_id"]),),
        ).fetchall()
    ]
    wfs = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM contact_client_workflows WHERE contact_id = ?",
            (FLORA_ID,),
        ).fetchall()
    ]
    return {"contact": dict(contact), "ccrs": ccrs, "workflows": wfs}


def _cleanup(company_id: int | None, activity_ids: list[int]) -> None:
    with get_connection() as conn:
        for aid in activity_ids:
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
        leftover = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        ids = [int(r["id"]) for r in leftover]
        if company_id:
            ids.append(int(company_id))
        for cid in set(ids):
            conn.execute("DELETE FROM activities WHERE company_id = ?", (cid,))
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (cid,),
            )
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (cid,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.commit()


def _http_json(method: str, path: str) -> tuple[int, dict]:
    return testdb.http_json(method, path)


def main() -> int:
    company_id: int | None = None
    activity_ids: list[int] = []
    flora_before: dict | None = None
    try:
        with get_connection() as conn:
            flora_before = _flora_workflow_snapshot(conn)
            carmeco = conn.execute(
                "SELECT id, name FROM clients WHERE code = 'carmeco'"
            ).fetchone()
            if carmeco is None:
                _fail("Carmeco client not found.")
            carmeco_id = int(carmeco["id"])
            client_name = str(carmeco["name"])
            stamp = str(int(time.time()))
            record_no = f"{MARKER}-{stamp}"
            cur = conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (record_no, f"{MARKER} Timeline Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status
                ) VALUES (?, ?, ?, 'New')
                """,
                (carmeco_id, company_id, record_no),
            )
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name,
                    title, source_row_index
                ) VALUES (?, ?, 'Alpha', 'Tester', 'Buyer', 1)
                """,
                (company_id, record_no),
            )
            contact_a = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name,
                    title, source_row_index
                ) VALUES (?, ?, 'Beta', 'Tester', 'Buyer', 2)
                """,
                (company_id, record_no),
            )
            contact_b = int(cur.lastrowid)
            conn.commit()

        if contact_a == FLORA_ID or contact_b == FLORA_ID:
            _fail("Refusing to use Flora Jia as a test contact.")

        research_a = (
            "Created from NorthStar AI Research. Research Run: 99. "
            "Source: AI Research public contacts. "
            f"Linked existing company {record_no}. Added contacts: 4632."
        )
        research_b = (
            "Created from NorthStar AI Research. Research Run: 99. "
            "Source: AI Research public contacts. "
            f"Linked existing company {record_no}. Added contacts: 9999."
        )
        for notes in (research_a, research_a, research_b):
            act = create_activity(
                ActivityCreateRequest(
                    client=client_name,
                    client_id=carmeco_id,
                    external_record_no=record_no,
                    activity_type="Note",
                    notes=notes,
                    created_by="Julie Magnani",
                )
            )
            activity_ids.append(act.activity_id)

        unique_company = create_activity(
            ActivityCreateRequest(
                client=client_name,
                client_id=carmeco_id,
                external_record_no=record_no,
                activity_type="Note",
                notes=f"{MARKER} unique company note",
                created_by="Julie Magnani",
            )
        )
        activity_ids.append(unique_company.activity_id)

        own_note = create_activity(
            ActivityCreateRequest(
                client=client_name,
                client_id=carmeco_id,
                external_record_no=record_no,
                contact_id=contact_a,
                activity_type="Note",
                notes=f"{MARKER} contact A note",
                created_by="Julie Magnani",
            )
        )
        activity_ids.append(own_note.activity_id)

        other_call = create_activity(
            ActivityCreateRequest(
                client=client_name,
                client_id=carmeco_id,
                external_record_no=record_no,
                contact_id=contact_b,
                activity_type="Call",
                notes=f"{MARKER} contact B call",
                outcome="Connected",
                created_by="Julie Magnani",
            )
        )
        activity_ids.append(other_call.activity_id)

        ws_a = get_contact_workspace(contact_a, client_id=carmeco_id)
        contact_bodies = [item.body for item in ws_a.timeline]
        contact_titles = [item.title for item in ws_a.timeline]
        if f"{MARKER} contact A note" not in contact_bodies:
            _fail("Contact Activity missing the note linked to this contact.")
        if any("Added contacts:" in body for body in contact_bodies):
            _fail("Contact Activity included company research notes.")
        if f"{MARKER} contact B call" in contact_bodies:
            _fail("Contact Activity included another contact's activity.")
        if f"{MARKER} unique company note" in contact_bodies:
            _fail("Contact Activity included company-level notes.")
        if "Call" in contact_titles:
            _fail("Contact Activity included a Call that belongs to another contact.")

        company_bodies = [item.body for item in ws_a.company_timeline]
        research_hits = [
            body for body in company_bodies if body.lower().startswith("created from northstar ai research")
        ]
        if len(research_hits) != 1:
            _fail(f"Expected 1 deduped research note in Company Activity, got {len(research_hits)}.")
        if f"{MARKER} unique company note" not in company_bodies:
            _fail("Company Activity missing the unique company note.")
        if f"{MARKER} contact A note" in company_bodies or f"{MARKER} contact B call" in company_bodies:
            _fail("Company Activity included contact-linked activities.")

        ws_b = get_contact_workspace(contact_b, client_id=carmeco_id)
        if f"{MARKER} contact B call" not in [item.body for item in ws_b.timeline]:
            _fail("Contact B workspace missing its own Call.")
        if f"{MARKER} contact A note" in [item.body for item in ws_b.timeline]:
            _fail("Contact B workspace showed Contact A's note.")

        with get_connection() as conn:
            remaining = conn.execute(
                "SELECT COUNT(1) AS n FROM activities WHERE activity_id IN (%s)"
                % ",".join("?" * len(activity_ids)),
                activity_ids,
            ).fetchone()["n"]
        if int(remaining) != len(activity_ids):
            _fail("Test activities were deleted from the database; display-only dedupe required.")

        flora_ws = get_contact_workspace(FLORA_ID, client_id=1)
        if any("Added contacts:" in (item.body or "") for item in flora_ws.timeline):
            _fail("Flora Jia Contact Activity still shows company research notes.")
        flora_research = [
            item.body
            for item in flora_ws.company_timeline
            if (item.body or "").lower().startswith("created from northstar ai research")
        ]
        with get_connection() as conn:
            raw_research = [
                str(r["notes"] or "").strip()
                for r in conn.execute(
                    """
                    SELECT notes FROM activities
                    WHERE company_id = (SELECT company_id FROM contacts WHERE id = ?)
                      AND client_id = 1
                      AND contact_id IS NULL
                      AND lower(notes) LIKE 'created from northstar ai research%'
                    """,
                    (FLORA_ID,),
                ).fetchall()
            ]
            flora_after = _flora_workflow_snapshot(conn)
        unique_research = {_sig(n) for n in raw_research if n}
        if len(flora_research) != len(unique_research):
            _fail(
                "Flora Jia Company Activity did not collapse identical research notes "
                f"(shown {len(flora_research)}, unique {len(unique_research)}, raw {len(raw_research)})."
            )
        if flora_after != flora_before:
            _fail("Flora Jia workflow or contact values changed.")

        try:
            code, body = _http_json("GET", f"/api/contacts/{FLORA_ID}?client_id=1")
            if code != 200:
                _fail(f"HTTP GET Flora workspace failed ({code}): {body}")
            live_timeline = body.get("timeline") or []
            if any("Added contacts:" in str(item.get("body") or "") for item in live_timeline):
                _fail("Live Contact Activity for Flora Jia still includes company research notes.")
            live_company = body.get("company_timeline") or []
            live_research = [
                item
                for item in live_company
                if str(item.get("body") or "").lower().startswith("created from northstar ai research")
            ]
            print("HTTP Flora contact timeline", len(live_timeline), "company research shown", len(live_research))
        except ConnectionError:
            print(f"API not reachable at {API}; data-layer checks still passed.")

        print("PASS: Contact Activity is contact_id-scoped; Company Activity is separate;")
        print("      research notes deduped in display only; Flora workflow unchanged.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        _cleanup(company_id, activity_ids)


def _sig(body: str) -> str:
    import re

    text = re.sub(r"added contacts:\s*[\d,\s]+", "added contacts:", body.lower())
    return re.sub(r"\s+", " ", text).strip()


if __name__ == "__main__":
    sys.exit(main())
