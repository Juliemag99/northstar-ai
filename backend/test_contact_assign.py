"""Assign a shared master contact to another Active Client.

Run: python test_contact_assign.py

Creates and deletes temporary company/contact/CCR rows.
Does not change Flora Jia or any other production relationships.
"""

from __future__ import annotations

import testdb
import json
import sys
import time
import urllib.error
import urllib.request

from client_workspace_data import (
    assign_shared_contact,
    contact_is_assigned_to_client,
    ensure_contact_workflow_schema,
    get_contact_workspace,
    list_contacts,
)
from db import get_connection
from models import ContactAssignRequest

FLORA_ID = 4631
API = "http://127.0.0.1:8007"
MARKER = "NSASNTEST"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _snapshot_flora(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, alt_phone, email,
               external_record_no, source_row_index
        FROM contacts
        WHERE id = ? OR (first_name = 'Flora' AND last_name = 'Jia')
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, id
        LIMIT 1
        """,
        (FLORA_ID, FLORA_ID),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _ccr_snapshot(conn, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date,
                   external_record_no
            FROM client_company_relationships
            WHERE company_id = ?
            ORDER BY client_id, id
            """,
            (company_id,),
        ).fetchall()
    ]


def _cleanup(company_ids: list[int], contact_ids: list[int], activity_ids: list[int]) -> None:
    with get_connection() as conn:
        for aid in activity_ids:
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
        for contact_id in contact_ids:
            conn.execute(
                "DELETE FROM contact_client_relationships WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        for company_id in company_ids:
            conn.execute(
                "DELETE FROM activities WHERE company_id = ?",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM contact_client_relationships WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        leftover = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        for row in leftover:
            cid = int(row["id"])
            conn.execute("DELETE FROM activities WHERE company_id = ?", (cid,))
            conn.execute(
                "DELETE FROM contact_client_relationships WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (cid,),
            )
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


def _http_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    return testdb.http_json(method, path, body)


def _insert_temp(
    conn,
    *,
    owner_client_id: int,
    stamp: str,
    suffix: str,
    status: str,
):
    record_no = f"{MARKER}-{stamp}-{suffix}"
    cur = conn.execute(
        """
        INSERT INTO companies (external_record_no, company_name, city, state)
        VALUES (?, ?, 'Testville', 'OH')
        """,
        (record_no, f"{MARKER} Assign Co {suffix} {stamp}"),
    )
    company_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, external_record_no, status, next_action
        ) VALUES (?, ?, ?, ?, '')
        """,
        (owner_client_id, company_id, record_no, status),
    )
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, first_name, last_name, title, phone, email,
            external_record_no, source_row_index
        ) VALUES (?, ?, ?, 'Tester', '555-0100', ?, ?, 1)
        """,
        (
            company_id,
            f"{MARKER}Pat",
            suffix.title(),
            f"pat.{suffix}.{stamp}@example.test",
            record_no,
        ),
    )
    contact_id = int(cur.lastrowid)
    return company_id, contact_id, record_no


def _assert_direction(
    *,
    source_id: int,
    target_id: int,
    source_name: str,
    target_name: str,
    source_status: str,
    stamp: str,
    suffix: str,
    activity_ids: list[int],
) -> tuple[int, int]:
    with get_connection() as conn:
        company_id, contact_id, _record_no = _insert_temp(
            conn,
            owner_client_id=source_id,
            stamp=stamp,
            suffix=suffix,
            status=source_status,
        )
        source_ccr_before = [
            r
            for r in _ccr_snapshot(conn, company_id)
            if int(r["client_id"]) == source_id
        ]
        contact_before = dict(
            conn.execute(
                """
                SELECT id, first_name, last_name, title, phone, email, external_record_no
                FROM contacts WHERE id = ?
                """,
                (contact_id,),
            ).fetchone()
        )
        contact_count_before = int(
            conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
        )
        conn.commit()

    if contact_is_assigned_to_client(contact_id, target_id):
        _fail(f"Temp contact should not already be assigned to {target_name}.")
    if not contact_is_assigned_to_client(contact_id, source_id):
        _fail(f"Temp contact should be assigned to {source_name}.")

    pending = get_contact_workspace(contact_id, client_id=target_id)
    if pending.assigned_to_client:
        _fail(f"{source_name} contact should be unassigned on {target_name}.")
    if pending.company_assigned_to_client:
        _fail(f"Temp company should not already belong to {target_name}.")

    blocked = assign_shared_contact(
        contact_id,
        ContactAssignRequest(client_id=target_id, add_company=False, user="Julie Magnani"),
    )
    if not blocked["needs_company_confirmation"]:
        _fail("Expected company confirmation before writing.")
    if blocked["workspace"].assigned_to_client:
        _fail("Blocked assign must not create the relationship.")

    with get_connection() as conn:
        target_ccr = conn.execute(
            """
            SELECT id FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (target_id, company_id),
        ).fetchone()
        xref = conn.execute(
            """
            SELECT id FROM contact_client_relationships
            WHERE contact_id = ? AND client_id = ?
            """,
            (contact_id, target_id),
        ).fetchone()
        if target_ccr is not None or xref is not None:
            _fail("Company confirmation must not write CCR or contact relationship.")
        if int(conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]) != contact_count_before:
            _fail("Blocked assign changed contacts count.")

    added = assign_shared_contact(
        contact_id,
        ContactAssignRequest(client_id=target_id, add_company=True, user="Julie Magnani"),
    )
    if added["needs_company_confirmation"]:
        _fail("add_company=True should complete the assignment.")
    if added["already_assigned"]:
        _fail("First add should not report already_assigned.")
    if not added["company_created"]:
        _fail("Missing company should be created for the Active Client.")
    if added["activity_id"]:
        activity_ids.append(int(added["activity_id"]))
    ws = added["workspace"]
    if not ws.assigned_to_client:
        _fail("Workspace should open the new Active Client assignment.")
    if ws.client_id != target_id:
        _fail("Assigned workspace client_id mismatch.")
    if (ws.status or "").strip().lower() != "new":
        _fail(f"New relationship status should be New, got {ws.status!r}.")
    if not any(
        "added shared contact" in (item.body or "").lower()
        for item in (ws.timeline or [])
    ):
        _fail("Expected one audit activity on the new client's contact timeline.")

    again = assign_shared_contact(
        contact_id,
        ContactAssignRequest(client_id=target_id, add_company=True, user="Julie Magnani"),
    )
    if not again["already_assigned"]:
        _fail("Second add must be idempotent.")
    if again["activity_id"]:
        _fail("Second add must not record another audit activity.")

    source_ws = get_contact_workspace(contact_id, client_id=source_id)
    if (source_ws.status or "").strip() != source_status:
        _fail(
            f"{source_name} status changed from {source_status!r} to {source_ws.status!r}."
        )

    with get_connection() as conn:
        contact_after = dict(
            conn.execute(
                """
                SELECT id, first_name, last_name, title, phone, email, external_record_no
                FROM contacts WHERE id = ?
                """,
                (contact_id,),
            ).fetchone()
        )
        if contact_after != contact_before:
            _fail("Shared master contact was altered.")
        if int(conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]) != contact_count_before:
            _fail("Assign duplicated or deleted a contact.")
        xref_n = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM contact_client_relationships
                WHERE contact_id = ? AND client_id = ?
                """,
                (contact_id, target_id),
            ).fetchone()["n"]
        )
        if xref_n != 1:
            _fail(f"Expected one contact-client relationship, found {xref_n}.")
        source_ccr_after = [
            r
            for r in _ccr_snapshot(conn, company_id)
            if int(r["client_id"]) == source_id
        ]
        if source_ccr_after != source_ccr_before:
            _fail(f"{source_name} company relationship was altered.")
        target_rn = conn.execute(
            """
            SELECT external_record_no FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (target_id, company_id),
        ).fetchone()
        if target_rn is None:
            _fail("Target client-company relationship missing.")
        if str(target_rn["external_record_no"]).strip() == str(
            contact_after["external_record_no"]
        ).strip():
            _fail("New client CCR must not reuse the other client's Record No.")
        audit_n = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM activities
                WHERE contact_id = ? AND client_id = ?
                  AND notes LIKE '%added shared contact%'
                """,
                (contact_id, target_id),
            ).fetchone()["n"]
        )
        if audit_n != 1:
            _fail(f"Expected one audit activity, found {audit_n}.")

    listed = list_contacts(client_id=target_id, q=f"{MARKER}Pat {suffix.title()}")
    if not any(int(item.id) == contact_id for item in listed["contacts"]):
        _fail(f"Assigned contact missing from {target_name} Contacts list.")

    return company_id, contact_id


def main() -> int:
    ensure_contact_workflow_schema()
    company_ids: list[int] = []
    contact_ids: list[int] = []
    activity_ids: list[int] = []
    flora_before: dict | None = None
    flora_company_id: int | None = None
    flora_ccr_before: list[dict] = []

    try:
        with get_connection() as conn:
            flora_before = _snapshot_flora(conn)
            if flora_before is not None:
                flora_company_id = int(flora_before["company_id"])
                flora_ccr_before = _ccr_snapshot(conn, flora_company_id)
            clients = {
                str(r["code"]): (int(r["id"]), str(r["name"]))
                for r in conn.execute("SELECT id, code, name FROM clients").fetchall()
            }
        carmeco = clients.get("carmeco")
        brown = clients.get("brown")
        if not carmeco or not brown:
            _fail("Need both carmeco and brown clients.")
        carmeco_id, carmeco_name = carmeco
        brown_id, brown_name = brown
        stamp = str(int(time.time()))

        company_a, contact_a = _assert_direction(
            source_id=carmeco_id,
            target_id=brown_id,
            source_name=carmeco_name,
            target_name=brown_name,
            source_status="Hot Prospect",
            stamp=stamp,
            suffix="c2b",
            activity_ids=activity_ids,
        )
        company_ids.append(company_a)
        contact_ids.append(contact_a)

        company_b, contact_b = _assert_direction(
            source_id=brown_id,
            target_id=carmeco_id,
            source_name=brown_name,
            target_name=carmeco_name,
            source_status="Account",
            stamp=stamp,
            suffix="b2c",
            activity_ids=activity_ids,
        )
        company_ids.append(company_b)
        contact_ids.append(contact_b)

        status, payload = _http_json(
            "GET",
            f"/api/contacts/{contact_a}?client_id={brown_id}",
        )
        if status != 200:
            _fail(f"HTTP GET assigned workspace failed: {status} {payload}")
        if payload.get("assigned_to_client") is not True:
            _fail("HTTP workspace did not open the new Brown assignment.")
        if str(payload.get("status") or "").strip().lower() != "new":
            _fail("HTTP workspace status should be New.")

        status, payload = _http_json(
            "POST",
            f"/api/contacts/{contact_a}/assign",
            {"client_id": brown_id, "add_company": True, "user": "Julie Magnani"},
        )
        if status != 200:
            _fail(f"HTTP duplicate assign failed: {status} {payload}")
        if payload.get("already_assigned") is not True:
            _fail("HTTP duplicate assign should be idempotent.")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            if flora_before != flora_after:
                _fail("Flora Jia contact row changed.")
            if flora_company_id is not None:
                flora_ccr_after = _ccr_snapshot(conn, flora_company_id)
                if flora_ccr_after != flora_ccr_before:
                    _fail("Flora Jia / production company relationships changed.")

        print("contact assign: Carmeco -> Brown Industries and reverse OK")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        _cleanup(company_ids, contact_ids, activity_ids)


if __name__ == "__main__":
    raise SystemExit(main())
