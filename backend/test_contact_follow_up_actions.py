"""Contact follow-up Complete / Reschedule / Log Call & Complete.

Run: python test_contact_follow_up_actions.py

Creates and deletes temporary company/contact/CCR rows.
Does not change Flora Jia or any other existing contact identity values.
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
    complete_contact_follow_up,
    create_contact_activity,
    ensure_contact_workflow_schema,
    get_contact_workspace,
    list_relationship_statuses,
    reschedule_contact_follow_up,
)
from db import get_connection
from models import (
    ContactActivityCreate,
    ContactFollowUpCompleteRequest,
    ContactFollowUpRescheduleRequest,
)
from work_queue_data import list_dashboard_follow_ups, list_work_queue

FLORA_ID = 4631
API = "http://127.0.0.1:8007"
MARKER = "NSFUACT"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _open_follow_counts(conn, contact_id: int, client_id: int) -> tuple[int, int]:
    open_follow = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM activities
            WHERE contact_id = ? AND client_id = ? AND activity_type = 'Follow-Up'
              AND COALESCE(follow_up_completed, 0) = 0
              AND lower(COALESCE(completion_status, 'open')) NOT IN
                  ('completed', 'cancelled', 'done', 'closed')
            """,
            (contact_id, client_id),
        ).fetchone()["n"]
    )
    open_queue = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM work_queue_items
            WHERE contact_id = ? AND client_id = ?
              AND lower(action_type) IN ('follow-up', 'follow up', 'followup')
              AND lower(COALESCE(completion_status, 'open')) IN
                  ('open', 'due', 'incomplete', 'pending', '')
            """,
            (contact_id, client_id),
        ).fetchone()["n"]
    )
    return open_follow, open_queue


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


def _dash_has(client_id: int, contact_id: int) -> bool:
    dash = list_dashboard_follow_ups(client_id=client_id)
    for bucket in (dash.overdue, dash.due_today, dash.upcoming):
        if any(item.contact_id == contact_id for item in bucket):
            return True
    return False


def _queue_has(client_id: int, contact_id: int) -> bool:
    queue = list_work_queue(client_id=client_id, work_type="follow-up")
    return any(item.contact_id == contact_id for item in queue.items)


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


def _http_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    return testdb.http_json(method, path, body)


def _schedule(
    contact_id: int,
    client_id: int,
    julie_id: int,
    status: str,
    follow_date: str,
    follow_time: str,
    notes: str,
) -> dict:
    result = create_contact_activity(
        contact_id,
        ContactActivityCreate(
            client_id=client_id,
            activity_type="Call",
            status=status,
            outcome=status,
            notes=notes,
            follow_up_date=follow_date,
            follow_up_time=follow_time,
            assigned_user_id=julie_id,
            schedule_follow_up=True,
            created_by="Julie Magnani",
        ),
    )
    return result


def main() -> int:
    ensure_contact_workflow_schema()
    company_id: int | None = None
    contact_id: int | None = None
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
                (record_no, f"{MARKER} Follow Co {stamp}"),
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
                ) VALUES (?, ?, 'FollowAct', 'Tester', 'Buyer', 1)
                """,
                (company_id, record_no),
            )
            contact_id = int(cur.lastrowid)
            conn.commit()

        if contact_id == FLORA_ID or (flora_before and contact_id == int(flora_before["id"])):
            _fail("Refusing to use Flora Jia as test contact.")

        statuses = list_relationship_statuses(client_id=brown_id)
        if not statuses:
            _fail("Client statuses API source returned no statuses — cannot hardcode.")
        status = next((s for s in statuses if s.strip()), "Left Message")
        today = date.today()
        first_date = (today + timedelta(days=3)).isoformat()
        second_date = (today + timedelta(days=10)).isoformat()
        third_date = (today + timedelta(days=14)).isoformat()

        scheduled = _schedule(
            contact_id,
            brown_id,
            julie_id,
            status,
            first_date,
            "10:45",
            f"{MARKER} schedule for complete",
        )
        activity_ids.append(int(scheduled["activity_id"]))
        if scheduled.get("follow_up_activity_id"):
            activity_ids.append(int(scheduled["follow_up_activity_id"]))
        first_follow_id = scheduled.get("follow_up_activity_id") or scheduled.get("activity_id")
        ws = scheduled["workspace"]
        if ws.open_follow_up is None:
            _fail("Workspace should display the open follow-up task.")
        assert ws.open_follow_up.due_date.startswith(first_date)
        assert (ws.open_follow_up.due_time or "")[:5] == "10:45"
        if not _dash_has(brown_id, contact_id):
            _fail("Open follow-up missing from Dashboard / Tasks.")
        if not _queue_has(brown_id, contact_id):
            _fail("Open follow-up missing from Work Queue.")
        if _dash_has(carmeco_id, contact_id):
            _fail("Brown follow-up leaked onto Carmeco Dashboard.")

        status_before = ws.status
        completed = complete_contact_follow_up(
            contact_id,
            ContactFollowUpCompleteRequest(
                client_id=brown_id,
                notes=f"{MARKER} complete note",
                created_by="Julie Magnani",
            ),
        )
        if completed.get("activity_id"):
            activity_ids.append(int(completed["activity_id"]))
        ws = completed["workspace"]
        if ws.open_follow_up is not None:
            _fail("Complete Task should remove the open follow-up from the workspace.")
        if (ws.follow_up_date or "").strip():
            _fail("Completing the matching Next Follow-Up should clear workflow date/time.")
        assert ws.status == status_before, "Complete Task must not change client status."
        if _dash_has(brown_id, contact_id):
            _fail("Completed task still on Dashboard / Tasks.")
        if _queue_has(brown_id, contact_id):
            _fail("Completed task still on Work Queue.")

        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, brown_id)
            completed_follow = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE contact_id = ? AND client_id = ? AND activity_type = 'Follow-Up'
                      AND (
                        COALESCE(follow_up_completed, 0) = 1
                        OR lower(COALESCE(completion_status, '')) IN
                           ('completed', 'cancelled', 'done', 'closed')
                      )
                    """,
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
            completion_notes = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE contact_id = ? AND client_id = ? AND activity_type = 'Note'
                      AND outcome = 'Follow-up completed'
                    """,
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
            carmeco_ccr = conn.execute(
                """
                SELECT status, next_action, follow_up_date
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (carmeco_id, company_id),
            ).fetchone()
        if open_follow != 0 or open_queue != 0:
            _fail("Complete Task left an open follow-up activity or queue item.")
        if completed_follow < 1:
            _fail("Historical Follow-Up activity was deleted instead of marked completed.")
        if completion_notes != 1:
            _fail("Complete Task should add exactly one completion activity.")
        assert _blank(carmeco_ccr["status"]) == "Existing Carmeco Status"
        assert carmeco_ccr["follow_up_date"] in (None, "")

        timeline_titles = [item.outcome for item in ws.timeline]
        if "Follow-up completed" not in timeline_titles:
            _fail("Completion activity missing from contact timeline.")

        scheduled = _schedule(
            contact_id,
            brown_id,
            julie_id,
            status,
            first_date,
            "11:15",
            f"{MARKER} schedule for reschedule",
        )
        activity_ids.append(int(scheduled["activity_id"]))
        if scheduled.get("follow_up_activity_id"):
            activity_ids.append(int(scheduled["follow_up_activity_id"]))
        ws = scheduled["workspace"]
        if ws.open_follow_up is None:
            _fail("Expected an open follow-up before reschedule.")
        before_activity_id = ws.open_follow_up.activity_id
        before_source_id = ws.open_follow_up.source_id
        moved = reschedule_contact_follow_up(
            contact_id,
            ContactFollowUpRescheduleRequest(
                client_id=brown_id,
                follow_up_date=second_date,
                follow_up_time="16:30",
                created_by="Julie Magnani",
            ),
        )
        if moved.get("follow_up_activity_id"):
            activity_ids.append(int(moved["follow_up_activity_id"]))
        ws = moved["workspace"]
        if ws.open_follow_up is None:
            _fail("Reschedule must keep one open follow-up.")
        assert ws.open_follow_up.due_date.startswith(second_date)
        assert (ws.open_follow_up.due_time or "")[:5] == "16:30"
        if before_activity_id and ws.open_follow_up.activity_id != before_activity_id:
            _fail("Reschedule created a duplicate Follow-Up activity.")
        if before_source_id and ws.open_follow_up.source_id != before_source_id:
            _fail("Reschedule created a duplicate Work Queue task.")
        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, brown_id)
        if open_follow != 1 or open_queue != 1:
            _fail("Reschedule must leave exactly one open follow-up task.")

        no_follow = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Call",
                status=status,
                outcome=status,
                notes=f"{MARKER} log call complete without next",
                assigned_user_id=julie_id,
                schedule_follow_up=False,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(no_follow["activity_id"]))
        if no_follow.get("follow_up_activity_id"):
            _fail("Log Call without Schedule another follow-up must not create a next task.")
        ws = no_follow["workspace"]
        if ws.open_follow_up is not None:
            _fail("Log Call & Complete should close the current follow-up.")
        if _dash_has(brown_id, contact_id) or _queue_has(brown_id, contact_id):
            _fail("Log Call & Complete left the task on Dashboard, Tasks, or Work Queue.")
        if not any(item.title == "Call" or item.item_type == "activity" for item in ws.timeline):
            _fail("Call activity missing from timeline after Log Call & Complete.")

        scheduled = _schedule(
            contact_id,
            brown_id,
            julie_id,
            status,
            first_date,
            "09:00",
            f"{MARKER} schedule then replace",
        )
        activity_ids.append(int(scheduled["activity_id"]))
        if scheduled.get("follow_up_activity_id"):
            activity_ids.append(int(scheduled["follow_up_activity_id"]))
        replaced = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Call",
                status=status,
                outcome=status,
                notes=f"{MARKER} log call with next follow-up",
                follow_up_date=third_date,
                follow_up_time="13:00",
                assigned_user_id=julie_id,
                schedule_follow_up=True,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(replaced["activity_id"]))
        if replaced.get("follow_up_activity_id"):
            activity_ids.append(int(replaced["follow_up_activity_id"]))
        ws = replaced["workspace"]
        if ws.open_follow_up is None:
            _fail("Scheduling another follow-up should leave one open task.")
        assert ws.open_follow_up.due_date.startswith(third_date)
        assert (ws.open_follow_up.due_time or "")[:5] == "13:00"
        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, brown_id)
        if open_follow != 1 or open_queue != 1:
            _fail("Log Call with Schedule another follow-up must leave exactly one open task.")
        if first_follow_id and ws.open_follow_up.activity_id == first_follow_id:
            _fail("Replacement follow-up should be a new open task after the previous one completed.")

        http_ok = False
        try:
            code, body = _http_json(
                "POST",
                f"/api/contacts/{contact_id}/follow-up/reschedule",
                {
                    "client_id": brown_id,
                    "follow_up_date": second_date,
                    "follow_up_time": "08:15",
                    "created_by": "Julie Magnani",
                },
            )
            if code != 200:
                _fail(f"HTTP POST reschedule failed ({code}): {body}")
            open_fu = (body.get("workspace") or {}).get("open_follow_up") or {}
            if not str(open_fu.get("due_date") or "").startswith(second_date):
                _fail("HTTP reschedule did not update the open task date.")
            if str(open_fu.get("due_time") or "")[:5] != "08:15":
                _fail("HTTP reschedule did not update the open task time.")
            code, body = _http_json(
                "POST",
                f"/api/contacts/{contact_id}/follow-up/complete",
                {
                    "client_id": brown_id,
                    "notes": f"{MARKER} http complete",
                    "created_by": "Julie Magnani",
                },
            )
            if code != 200:
                _fail(f"HTTP POST complete failed ({code}): {body}")
            if body.get("activity_id"):
                activity_ids.append(int(body["activity_id"]))
            if (body.get("workspace") or {}).get("open_follow_up"):
                _fail("HTTP complete left an open follow-up on the workspace.")
            if _dash_has(brown_id, contact_id) or _queue_has(brown_id, contact_id):
                _fail("HTTP complete left the task on Dashboard, Tasks, or Work Queue.")
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

        print("PASS: Complete Task / Reschedule / Log Call & Complete are client-scoped.")
        print("      Completed tasks leave Dashboard, Work Queue, and Tasks; history remains.")
        if http_ok:
            print("PASS: live HTTP POST complete / reschedule on :8007.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        _cleanup(company_id, contact_id, activity_ids)
        with get_connection() as conn:
            leftover = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
                (f"{MARKER} %", f"{MARKER}-%"),
            ).fetchone()["n"]
            if leftover:
                print(f"WARN: {leftover} test companies still present after cleanup.")


if __name__ == "__main__":
    sys.exit(main())
