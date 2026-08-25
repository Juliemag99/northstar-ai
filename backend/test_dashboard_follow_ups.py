"""Dashboard follow-ups are Active Client scoped and use Work Queue task records.

Run: python test_dashboard_follow_ups.py

Does not change Flora Jia. Temporary rows are created and deleted.
"""

from __future__ import annotations

import testdb
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

from client_workspace_data import (
    create_contact_activity,
    ensure_contact_workflow_schema,
    list_relationship_statuses,
)
from db import get_connection
from models import ContactActivityCreate
from work_queue_data import list_dashboard_follow_ups, list_work_queue

FLORA_ID = 4631
API = "http://127.0.0.1:8007"
MARKER = "NSDASHFU"


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
    return dict(row) if row is not None else None


def _ccr_snapshot(conn, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
            FROM client_company_relationships
            WHERE company_id = ?
            ORDER BY client_id
            """,
            (company_id,),
        ).fetchall()
    ]


def _cleanup(company_id: int | None, contact_id: int | None, activity_ids: list[int]) -> None:
    with get_connection() as conn:
        for aid in activity_ids:
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
        if contact_id:
            conn.execute("DELETE FROM contact_client_workflows WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM work_queue_items WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        if company_id:
            conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (company_id,))
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (company_id,),
            )
            conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
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
            conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (cid,))
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
    ensure_contact_workflow_schema()
    company_id: int | None = None
    overdue_contact_id: int | None = None
    today_contact_id: int | None = None
    upcoming_contact_id: int | None = None
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
                str(r["code"]): int(r["id"])
                for r in conn.execute("SELECT id, code FROM clients").fetchall()
            }
            carmeco_id = clients.get("carmeco")
            brown_id = clients.get("brown")
            if not carmeco_id or not brown_id:
                _fail("Need both carmeco and brown clients.")
            julie = conn.execute(
                "SELECT id FROM users WHERE lower(full_name) = 'julie magnani' AND active = 1"
            ).fetchone()
            if julie is None:
                _fail("Julie Magnani user not found.")
            julie_id = int(julie["id"])
            stamp = str(int(time.time()))
            record_no = f"{MARKER}-{stamp}"
            cur = conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (record_no, f"{MARKER} Dash Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'New', '')
                """,
                (brown_id, company_id, record_no),
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'Existing Carmeco Status', '')
                """,
                (carmeco_id, company_id, f"{record_no}-C"),
            )
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'DashOverdue', 'Tester', 'Buyer', 1)
                """,
                (company_id, record_no),
            )
            overdue_contact_id = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'DashToday', 'Tester', 'Buyer', 2)
                """,
                (company_id, f"{record_no}-T"),
            )
            today_contact_id = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'DashUpcoming', 'Tester', 'Buyer', 3)
                """,
                (company_id, f"{record_no}-U"),
            )
            upcoming_contact_id = int(cur.lastrowid)
            conn.commit()

        ids = [overdue_contact_id, today_contact_id, upcoming_contact_id]
        if FLORA_ID in ids or (flora_before and int(flora_before["id"]) in ids):
            _fail("Refusing to use Flora Jia as test contact.")

        brown_dash = list_dashboard_follow_ups(client_id=brown_id)
        carmeco_dash = list_dashboard_follow_ups(client_id=carmeco_id)
        flora_brown_before = [
            (item.due_date, item.contact_id)
            for item in brown_dash.overdue + brown_dash.due_today + brown_dash.upcoming
            if item.contact_id == FLORA_ID
        ]
        if any(item.contact_id == FLORA_ID for item in carmeco_dash.overdue + carmeco_dash.due_today + carmeco_dash.upcoming):
            _fail("Flora Jia follow-up must not appear on the Carmeco dashboard.")

        queue_carmeco = list_work_queue(client_id=carmeco_id, work_type="follow-up")
        if any(item.contact_id == FLORA_ID for item in queue_carmeco.items):
            _fail("Flora Jia follow-up leaked into Carmeco Work Queue.")

        statuses = list_relationship_statuses(client_id=brown_id)
        status = next((s for s in statuses if s.strip()), "Left Message")
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        today_s = today.isoformat()
        next_week = (today + timedelta(days=7)).isoformat()

        overdue_call = create_contact_activity(
            overdue_contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Call",
                status=status,
                outcome=status,
                notes=f"{MARKER} overdue follow-up",
                follow_up_date=yesterday,
                follow_up_time="09:00",
                assigned_user_id=julie_id,
                schedule_follow_up=True,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(overdue_call["activity_id"]))
        if overdue_call.get("follow_up_activity_id"):
            activity_ids.append(int(overdue_call["follow_up_activity_id"]))

        today_call = create_contact_activity(
            today_contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Call",
                status=status,
                outcome=status,
                notes=f"{MARKER} due today follow-up",
                follow_up_date=today_s,
                follow_up_time="23:59",
                assigned_user_id=julie_id,
                schedule_follow_up=True,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(today_call["activity_id"]))
        if today_call.get("follow_up_activity_id"):
            activity_ids.append(int(today_call["follow_up_activity_id"]))

        upcoming_call = create_contact_activity(
            upcoming_contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Call",
                status=status,
                outcome=status,
                notes=f"{MARKER} upcoming follow-up",
                follow_up_date=next_week,
                follow_up_time="10:45",
                assigned_user_id=julie_id,
                schedule_follow_up=True,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(upcoming_call["activity_id"]))
        if upcoming_call.get("follow_up_activity_id"):
            activity_ids.append(int(upcoming_call["follow_up_activity_id"]))

        brown_after = list_dashboard_follow_ups(client_id=brown_id)
        carmeco_after = list_dashboard_follow_ups(client_id=carmeco_id)
        if not any(item.contact_id == overdue_contact_id and item.due_date == yesterday for item in brown_after.overdue):
            _fail("Temporary overdue follow-up missing from Brown Overdue.")
        if not any(item.contact_id == today_contact_id and item.due_date == today_s for item in brown_after.due_today):
            _fail("Temporary due-today follow-up missing from Brown Due Today.")
        if not any(item.contact_id == upcoming_contact_id and item.due_date == next_week for item in brown_after.upcoming):
            _fail("Temporary 7-day follow-up missing from Brown Upcoming.")
        temp_ids = {overdue_contact_id, today_contact_id, upcoming_contact_id}
        if any(item.contact_id in temp_ids for item in carmeco_after.overdue + carmeco_after.due_today + carmeco_after.upcoming):
            _fail("Brown temp follow-ups leaked onto Carmeco dashboard.")
        flora_brown_after = [
            (item.due_date, item.contact_id)
            for item in brown_after.overdue + brown_after.due_today + brown_after.upcoming
            if item.contact_id == FLORA_ID
        ]
        if flora_brown_after != flora_brown_before:
            _fail("Temporary follow-up saves changed Flora Jia's Brown dashboard items.")

        queue_up = list_work_queue(client_id=brown_id, work_type="follow-up", due="upcoming")
        if not any(item.contact_id == upcoming_contact_id and (item.due_date or "")[:10] == next_week for item in queue_up.items):
            _fail("Upcoming temp follow-up missing from Brown Work Queue.")

        http_ok = False
        try:
            code, brown_http = _http_json("GET", f"/api/dashboard/follow-ups?client_id={brown_id}")
            if code != 200:
                _fail(f"HTTP Brown dashboard follow-ups failed ({code}): {brown_http}")
            upcoming_http = brown_http.get("upcoming") or []
            if not any(int(item.get("contact_id") or 0) == upcoming_contact_id for item in upcoming_http):
                _fail("HTTP Brown Upcoming missing the newly scheduled temp follow-up.")
            code, carmeco_http = _http_json("GET", f"/api/dashboard/follow-ups?client_id={carmeco_id}")
            if code != 200:
                _fail(f"HTTP Carmeco dashboard follow-ups failed ({code}): {carmeco_http}")
            carmeco_all = (
                (carmeco_http.get("overdue") or [])
                + (carmeco_http.get("due_today") or [])
                + (carmeco_http.get("upcoming") or [])
            )
            if any(int(item.get("contact_id") or 0) == FLORA_ID for item in carmeco_all):
                _fail("HTTP Carmeco dashboard included Flora Jia.")
            if any(int(item.get("contact_id") or 0) in {overdue_contact_id, today_contact_id, upcoming_contact_id} for item in carmeco_all):
                _fail("HTTP Carmeco dashboard included Brown temp follow-ups.")
            http_ok = True
        except ConnectionError:
            print(f"API not reachable at {API}; data-layer checks still passed.")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            flora_ccr_after = (
                _ccr_snapshot(conn, flora_company_id) if flora_company_id else []
            )
        if flora_before is not None:
            assert flora_after == flora_before, "Flora Jia contact row changed."
            assert flora_ccr_after == flora_ccr_before, "Flora Jia company CCR changed."

        print("PASS: Brown dashboard follow-ups stay client-scoped; Flora Jia unchanged.")
        print("PASS: Overdue / Due Today / Upcoming split from the same task records.")
        if http_ok:
            print("PASS: in-process HTTP /api/dashboard/follow-ups on isolated test DB.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        _cleanup(company_id, overdue_contact_id, activity_ids)


if __name__ == "__main__":
    sys.exit(main())
