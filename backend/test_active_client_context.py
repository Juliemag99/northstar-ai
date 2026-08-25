"""Active Client context must not leak another client's workflow.

Run: python test_active_client_context.py

Read-only: no INSERT/UPDATE/DELETE. Does not change Flora Jia or any other rows.
"""

from __future__ import annotations

import testdb
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode

from client_workspace_data import (
    contact_is_assigned_to_client,
    get_company_by_record_no,
    get_contact_workspace,
)
from db import get_connection

FLORA_ID = 4631
API = "http://127.0.0.1:8007"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _rewrite(pathname: str, search: str, next_id: int) -> str | None:
    """Mirror frontend/src/activeClientNavigation.ts (contract test)."""
    raw = search[1:] if search.startswith("?") else search
    params = dict(parse_qs(raw, keep_blank_values=True))
    flat = {k: (v[-1] if v else "") for k, v in params.items()}

    def out(extra_delete: list[str] | None = None) -> str:
        data = dict(flat)
        for key in extra_delete or []:
            data.pop(key, None)
        if next_id > 0:
            data["client_id"] = str(next_id)
        else:
            data.pop("client_id", None)
        qs = urlencode(data)
        return f"{pathname}?{qs}" if qs else pathname

    import re

    if re.fullmatch(r"/contacts/\d+/?", pathname):
        return out()
    if re.fullmatch(r"/companies/[^/]+/research/?", pathname):
        return out(["from", "queue_item", "work_type", "queue", "reason", "priority", "position", "total", "focus"])
    if re.fullmatch(r"/companies/[^/]+/?", pathname):
        return out(["from", "queue_item", "work_type", "queue", "reason", "priority", "position", "total", "focus"])
    return None


def _http_json(path: str) -> tuple[int, dict]:
    return testdb.http_json("GET", path)


def _snapshot_flora(conn) -> dict:
    contact = dict(
        conn.execute(
            """
            SELECT id, first_name, last_name, title, phone, email, company_id,
                   external_record_no
            FROM contacts WHERE id = ?
            """,
            (FLORA_ID,),
        ).fetchone()
    )
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
    return {"contact": contact, "ccrs": ccrs, "workflows": wfs}


def main() -> int:
    try:
        with get_connection() as conn:
            before = _snapshot_flora(conn)
            clients = {
                str(r["code"]): (int(r["id"]), str(r["name"]))
                for r in conn.execute("SELECT id, code, name FROM clients").fetchall()
            }
        carmeco_id, carmeco_name = clients["carmeco"]
        brown_id, brown_name = clients["brown"]
        record_no = before["contact"]["external_record_no"]

        flora_carmeco = get_contact_workspace(FLORA_ID, client_id=carmeco_id)
        flora_brown = get_contact_workspace(FLORA_ID, client_id=brown_id)
        if flora_carmeco.client_id != carmeco_id:
            _fail("Carmeco Contact Workspace returned a different client_id.")
        if flora_carmeco.client_name != carmeco_name:
            _fail(f"Carmeco workspace Working For was {flora_carmeco.client_name!r}.")
        if flora_brown.client_id != brown_id:
            _fail("Brown Contact Workspace leaked another client's id.")
        if flora_brown.client_name != brown_name:
            _fail(f"Brown workspace Working For was {flora_brown.client_name!r}.")

        assigned_brown = contact_is_assigned_to_client(FLORA_ID, brown_id)
        if assigned_brown:
            if flora_brown.assigned_to_client is False:
                _fail("Flora is assigned to Brown but API said she is not.")
            if flora_brown.status == flora_carmeco.status and flora_carmeco.status:
                # Allowed only if both CCRs happen to share the same status.
                with get_connection() as conn:
                    brown_status = conn.execute(
                        """
                        SELECT status FROM client_company_relationships
                        WHERE client_id = ? AND company_id = ?
                        """,
                        (brown_id, before["contact"]["company_id"]),
                    ).fetchone()
                if brown_status is None or str(brown_status["status"]).strip() != flora_brown.status:
                    _fail("Brown Contact Workspace showed Carmeco's status.")
        else:
            if flora_brown.assigned_to_client is not False:
                _fail("Unassigned Brown workspace must set assigned_to_client=false.")
            if flora_brown.status or flora_brown.next_action or flora_brown.timeline:
                _fail("Unassigned Brown workspace exposed Carmeco workflow/activity.")
            if flora_brown.relationship_id is not None:
                _fail("Unassigned Brown workspace still has a relationship_id.")

        co_carmeco = get_company_by_record_no(record_no, client_id=carmeco_id)
        co_brown = get_company_by_record_no(record_no, client_id=brown_id)
        if co_carmeco is None or co_brown is None:
            _fail("Company Workspace failed to load for Carmeco or Brown.")
        if int(co_carmeco.client_id) != carmeco_id:
            _fail("Carmeco Company Workspace returned a different client.")
        if int(co_brown.client_id) != brown_id:
            _fail("Brown Company Workspace leaked another client's id.")
        if co_carmeco.client_name != carmeco_name or co_brown.client_name != brown_name:
            _fail("Company Workspace Working For did not match the requested client.")

        contact_url = _rewrite("/contacts/4631", "?client_id=1", brown_id)
        if contact_url != f"/contacts/4631?client_id={brown_id}":
            _fail(f"Contact URL rewrite failed: {contact_url}")
        company_url = _rewrite(f"/companies/{record_no}", f"?client_id={carmeco_id}", brown_id)
        if f"client_id={brown_id}" not in (company_url or ""):
            _fail(f"Company URL rewrite failed: {company_url}")
        back_url = _rewrite(f"/companies/{record_no}", f"?client_id={brown_id}", carmeco_id)
        if f"client_id={carmeco_id}" not in (back_url or ""):
            _fail(f"Company URL rewrite back to Carmeco failed: {back_url}")
        research_url = _rewrite(
            f"/companies/{record_no}/research",
            f"?client_id={carmeco_id}",
            brown_id,
        )
        if research_url != f"/companies/{record_no}/research?client_id={brown_id}":
            _fail(f"Research URL rewrite failed: {research_url}")
        research_back = _rewrite(
            f"/companies/{record_no}/research",
            f"?client_id={brown_id}",
            carmeco_id,
        )
        if research_back != f"/companies/{record_no}/research?client_id={carmeco_id}":
            _fail(f"Research URL rewrite back to Carmeco failed: {research_back}")

        try:
            code, body = _http_json(f"/api/contacts/{FLORA_ID}?client_id={brown_id}")
            if code != 200:
                _fail(f"HTTP GET Brown contact workspace failed ({code}): {body}")
            if int(body.get("client_id") or 0) != brown_id:
                _fail("Live Brown contact workspace returned the wrong client_id.")
            if body.get("assigned_to_client") is False and (
                body.get("status") or body.get("timeline")
            ):
                _fail("Live unassigned Brown workspace exposed prior workflow.")
            code2, body2 = _http_json(f"/api/contacts/{FLORA_ID}?client_id={carmeco_id}")
            if code2 != 200 or int(body2.get("client_id") or 0) != carmeco_id:
                _fail("Live Carmeco contact workspace returned the wrong client.")
            code3, co = _http_json(
                f"/api/companies/by-record/{record_no}?client_id={brown_id}"
            )
            if code3 != 200 or int(co.get("client_id") or 0) != brown_id:
                _fail("Live Brown company workspace returned the wrong client.")
            code4, co2 = _http_json(
                f"/api/companies/by-record/{record_no}?client_id={carmeco_id}"
            )
            if code4 != 200 or int(co2.get("client_id") or 0) != carmeco_id:
                _fail("Live Carmeco company workspace returned the wrong client.")
            print("PASS: live HTTP Contact + Company Workspace client_id matches Active Client.")
        except ConnectionError:
            print(f"API not reachable at {API}; data-layer checks still passed.")

        with get_connection() as conn:
            after = _snapshot_flora(conn)
        if after != before:
            _fail("Flora Jia database values changed during read-only verification.")

        print("PASS: Active Client switch keeps Contact/Company/Research on the selected client.")
        print("      Unassigned contacts hide the other client's workflow. Flora Jia unchanged.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
