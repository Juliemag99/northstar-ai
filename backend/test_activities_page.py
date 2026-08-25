"""Activities timeline is Active Client scoped with search and pagination.

Run: python test_activities_page.py

Creates and deletes temporary company/contact/activity rows.
Does not change Flora Jia or other production identity values.
"""

from __future__ import annotations

import testdb
import sys
import time

from access import get_default_user
from activities_data import insert_activity_row, list_client_activities
from db import get_connection

FLORA_ID = 4631
MARKER = "NSACT"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


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


def _cleanup(company_id: int | None, contact_id: int | None) -> None:
    if company_id is None:
        return
    with get_connection() as conn:
        conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM field_audit_log WHERE external_record_no LIKE ?", (f"{MARKER}%",))
        if contact_id is not None:
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute(
                "DELETE FROM contacts WHERE id = ? AND first_name = 'ActTmp'",
                (contact_id,),
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
    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        if not flora_before:
            _fail("Flora Jia contact not found.")
        clients = {
            str(r["code"]): int(r["id"])
            for r in conn.execute("SELECT id, code FROM clients").fetchall()
        }
        carmeco_id = clients.get("carmeco")
        brown_id = clients.get("brown")
        if not carmeco_id or not brown_id:
            _fail("Need both carmeco and brown clients.")

    from db import PRODUCTION_DB_PATH

    try:
        get_connection(PRODUCTION_DB_PATH)
        _fail("Tests were allowed to open production northstar.db.")
    except RuntimeError:
        pass

    flora_page = list_client_activities(carmeco_id, contact_id=FLORA_ID, company_id=298)
    flora_types = [item.activity_type for item in flora_page.items]
    if "Appointment Created" in flora_types:
        _fail("Flora Jia cancelled appointment still labeled Appointment Created.")
    if "Appointment Cancelled" not in flora_types:
        _fail("Flora Jia cancelled appointment missing Appointment Cancelled.")

    dash = list_client_activities(carmeco_id, limit=5, offset=0)
    page = list_client_activities(carmeco_id, limit=50, offset=0)
    if [item.item_key for item in dash.items] != [item.item_key for item in page.items[:5]]:
        _fail("Dashboard recent activity order does not match the Activities page.")
    if not any(
        item.activity_type == "Appointment Cancelled" and item.contact_id == FLORA_ID
        for item in dash.items
    ):
        _fail("Carmeco dashboard recent activity missing Flora Jia Appointment Cancelled.")
    brown_dash = list_client_activities(brown_id, limit=5, offset=0)
    if any("Cancelled: this was a test" in (item.notes or "") for item in brown_dash.items):
        _fail("Brown recent activity included Carmeco cancellation notes.")
    if any(item.activity_type == "Appointment Cancelled" for item in brown_dash.items):
        _fail("Brown recent activity included Carmeco Appointment Cancelled.")
    code, http_dash = testdb.http_json("GET", f"/api/clients/{carmeco_id}/activities?limit=5")
    if code != 200:
        _fail(f"HTTP dashboard activities failed ({code}): {http_dash}")
    http_items = http_dash.get("items") or []
    if [item.item_key for item in dash.items] != [row.get("item_key") for row in http_items]:
        _fail("HTTP limit=5 items do not match list_client_activities.")

    try:
        with get_connection() as conn:
            stamp = str(int(time.time()))
            record_no = f"{MARKER}-{stamp}"
            cur = conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (record_no, f"{MARKER} Act Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'Contacted', 'Call')
                """,
                (carmeco_id, company_id, record_no),
            )
            carmeco_rel = int(
                conn.execute(
                    "SELECT id FROM client_company_relationships WHERE company_id = ? AND client_id = ?",
                    (company_id, carmeco_id),
                ).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'Left Message', 'Call back')
                """,
                (brown_id, company_id, f"{record_no}-B"),
            )
            brown_rel = int(
                conn.execute(
                    "SELECT id FROM client_company_relationships WHERE company_id = ? AND client_id = ?",
                    (company_id, brown_id),
                ).fetchone()["id"]
            )
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'ActTmp', 'Tester', 'Buyer', 1)
                """,
                (company_id, record_no),
            )
            contact_id = int(cur.lastrowid)
            if int(contact_id) == FLORA_ID:
                _fail("Refusing to use Flora Jia as the test contact.")
            insert_activity_row(
                conn,
                client_id=carmeco_id,
                company_id=company_id,
                relationship_id=carmeco_rel,
                external_record_no=record_no,
                user_id=user.id,
                contact_id=contact_id,
                activity_type="Call",
                outcome="Spoke With Contact",
                notes=f"{MARKER} carmeco-only call token",
                created_by="Julie Magnani",
                assigned_user="Julie Magnani",
            )
            insert_activity_row(
                conn,
                client_id=brown_id,
                company_id=company_id,
                relationship_id=brown_rel,
                external_record_no=f"{record_no}-B",
                user_id=user.id,
                contact_id=contact_id,
                activity_type="Note",
                notes=f"{MARKER} brown-private workflow note",
                created_by="Julie Magnani",
                assigned_user="Julie Magnani",
            )
            for idx in range(3):
                insert_activity_row(
                    conn,
                    client_id=carmeco_id,
                    company_id=company_id,
                    relationship_id=carmeco_rel,
                    external_record_no=record_no,
                    user_id=user.id,
                    contact_id=contact_id,
                    activity_type="Note",
                    notes=f"{MARKER} page-note {idx}",
                    created_by="Julie Magnani",
                    assigned_user="Julie Magnani",
                )
            conn.commit()

        carmeco_page = list_client_activities(carmeco_id, company_id=company_id, limit=50, offset=0)
        carmeco_notes = " ".join(item.notes for item in carmeco_page.items)
        if "brown-private" in carmeco_notes.lower():
            _fail("Carmeco Activities included Brown private workflow notes.")
        if not any("carmeco-only call token" in item.notes for item in carmeco_page.items):
            _fail("Carmeco Call missing from Activities.")
        if not any(item.activity_type == "Call" for item in carmeco_page.items):
            _fail("Carmeco Activities missing Call type.")
        if not any(item.contact_id == contact_id for item in carmeco_page.items):
            _fail("Carmeco Activities missing clickable contact.")

        brown_page = list_client_activities(brown_id, company_id=company_id, limit=50, offset=0)
        brown_notes = " ".join(item.notes for item in brown_page.items)
        if "carmeco-only call token" in brown_notes.lower():
            _fail("Brown Activities included Carmeco private call.")
        if not any("brown-private workflow note" in item.notes for item in brown_page.items):
            _fail("Brown Note missing from Brown Activities.")

        searched = list_client_activities(carmeco_id, q=f"{MARKER} carmeco-only call token")
        if searched.total < 1 or not any("carmeco-only call token" in item.notes for item in searched.items):
            _fail("Server-side search did not find the Carmeco call.")
        if any("brown-private" in item.notes.lower() for item in searched.items):
            _fail("Search leaked Brown notes into Carmeco results.")

        filtered = list_client_activities(carmeco_id, activity_type="Call", company_id=company_id)
        if not filtered.items or any(item.activity_type != "Call" for item in filtered.items):
            _fail("Activity type filter failed.")

        page1 = list_client_activities(carmeco_id, company_id=company_id, q=MARKER, limit=2, offset=0)
        page2 = list_client_activities(carmeco_id, company_id=company_id, q=MARKER, limit=2, offset=2)
        if page1.total < 4:
            _fail("Expected at least 4 Carmeco temp activities for pagination.")
        if len(page1.items) != 2:
            _fail("Pagination limit=2 did not return 2 rows.")
        keys1 = {item.item_key for item in page1.items}
        keys2 = {item.item_key for item in page2.items}
        if keys1 & keys2:
            _fail("Pagination pages overlapped.")
        if not any(item.company_name.startswith(MARKER) for item in carmeco_page.items):
            _fail("Company name missing from Activities rows.")
    finally:
        _cleanup(company_id, contact_id)

    with get_connection() as conn:
        if _snapshot_flora(conn) != flora_before:
            _fail("Flora Jia identity values changed.")

    print("test_activities_page: ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"test_activities_page: FAIL: {exc}", file=sys.stderr)
        raise
