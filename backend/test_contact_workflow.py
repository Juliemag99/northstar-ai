"""Contact Workspace operational workflow — per-client fields on a shared master contact.

Run: python test_contact_workflow.py

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
from datetime import date

from client_workspace_data import (
    create_contact_activity,
    ensure_contact_workflow_schema,
    get_contact_workspace,
    list_prospects,
    list_relationship_statuses,
    update_contact_workflow,
)
from db import get_connection
from models import ContactActivityCreate, ContactWorkflowUpdate
from work_queue_data import list_due_work_items, list_work_queue

FLORA_ID = 4631
API = "http://127.0.0.1:8007"
MARKER = "NSWFTEST"


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
              AND lower(action_type) = 'follow-up'
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
    if row is None:
        return None
    return dict(row)


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
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute(
                "DELETE FROM work_queue_items WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        if company_id:
            conn.execute(
                "DELETE FROM work_queue_items WHERE company_id = ?",
                (company_id,),
            )
        if company_id:
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
            conn.execute(
                "DELETE FROM work_queue_items WHERE company_id = ?",
                (cid,),
            )
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (cid,),
            )
            conn.execute(
                "DELETE FROM activities WHERE company_id = ?",
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


def main() -> int:
    ensure_contact_workflow_schema()
    company_id: int | None = None
    contact_id: int | None = None
    activity_ids: list[int] = []
    flora_before: dict | None = None
    flora_company_id: int | None = None
    flora_ccr_before: list[dict] = []
    contact_count_before = 0

    try:
        with get_connection() as conn:
            flora_before = _snapshot_flora(conn)
            if flora_before is not None:
                flora_company_id = int(flora_before["company_id"])
                flora_ccr_before = _ccr_snapshot(conn, flora_company_id)
            contact_count_before = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            )
            clients = {
                str(r["code"]): int(r["id"])
                for r in conn.execute("SELECT id, code FROM clients").fetchall()
            }
            carmeco_id = clients.get("carmeco")
            brown_id = clients.get("brown")
            if not carmeco_id or not brown_id:
                _fail("Need both carmeco and brown clients for isolation test.")
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
                (record_no, f"{MARKER} Workflow Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'New', '')
                """,
                (carmeco_id, company_id, record_no),
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'Existing Brown Status', '')
                """,
                (brown_id, company_id, f"{record_no}-B"),
            )
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'Workflow', 'Tester', 'Buyer', 1)
                """,
                (company_id, record_no),
            )
            contact_id = int(cur.lastrowid)
            conn.commit()

        if contact_id == FLORA_ID or (flora_before and contact_id == int(flora_before["id"])):
            _fail("Refusing to use Flora Jia as test contact.")

        statuses = list_relationship_statuses(client_id=carmeco_id)
        if not statuses:
            _fail("Client statuses API source returned no statuses — cannot hardcode.")
        target_status = next(
            (s for s in statuses if s.strip() and s.strip().lower() != "new"),
            statuses[0],
        )
        today = date.today().isoformat()

        ws = get_contact_workspace(contact_id, client_id=carmeco_id)
        assert ws.contact_id == contact_id
        assert ws.client_id == carmeco_id
        assert ws.first_name == "Workflow" and ws.last_name == "Tester"
        assert any(r.user_id == julie_id for r in ws.reps), "Assigned Rep list should include Julie"

        saved = update_contact_workflow(
            contact_id,
            ContactWorkflowUpdate(
                client_id=carmeco_id,
                status=target_status,
                assigned_user_id=julie_id,
                next_action="Follow-Up",
                follow_up_date=today,
                follow_up_time="14:30",
                user="Julie Magnani",
            ),
        )
        ws = saved["workspace"]
        assert ws.status == target_status
        assert ws.assigned_user_id == julie_id
        assert ws.next_action == "Follow-Up"
        assert ws.follow_up_date.startswith(today)
        assert ws.follow_up_time[:5] == "14:30"

        with get_connection() as conn:
            carmeco_ccr = conn.execute(
                """
                SELECT status, assigned_user_id, next_action, follow_up_date
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (carmeco_id, company_id),
            ).fetchone()
            brown_ccr = conn.execute(
                """
                SELECT status, assigned_user_id, next_action, follow_up_date
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (brown_id, company_id),
            ).fetchone()
            contact_row = conn.execute(
                "SELECT first_name, last_name, title FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            wf_rows = conn.execute(
                "SELECT client_id, status FROM contact_client_workflows WHERE contact_id = ?",
                (contact_id,),
            ).fetchall()
        assert dict(contact_row) == {
            "first_name": "Workflow",
            "last_name": "Tester",
            "title": "Buyer",
        }
        assert _blank(carmeco_ccr["status"]) == target_status
        assert int(carmeco_ccr["assigned_user_id"]) == julie_id
        assert _blank(carmeco_ccr["next_action"]) == "Follow-Up"
        assert str(carmeco_ccr["follow_up_date"]).startswith(today)
        assert _blank(brown_ccr["status"]) == "Existing Brown Status"
        assert _blank(brown_ccr["next_action"]) == ""
        assert brown_ccr["follow_up_date"] in (None, "")
        assert len(wf_rows) == 1 and int(wf_rows[0]["client_id"]) == carmeco_id

        call_status = next(
            (s for s in statuses if s.strip() and s.strip() != target_status),
            target_status,
        )
        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, carmeco_id)
        if open_follow != 1 or open_queue != 1:
            _fail("Workflow save with a follow-up date should create one open follow-up task.")

        closed_status = next(
            (
                s
                for s in statuses
                if "do not call" in s.lower()
                or s.lower() == "closed"
                or s.lower().startswith("closed")
            ),
            None,
        )

        no_follow = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=carmeco_id,
                activity_type="Call",
                status=call_status,
                outcome=call_status,
                notes=f"{MARKER} call without follow-up",
                next_action="",
                follow_up_date=today,
                follow_up_time="11:00",
                assigned_user_id=julie_id,
                schedule_follow_up=False,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(no_follow["activity_id"]))
        if no_follow.get("follow_up_activity_id"):
            _fail("Log Call without scheduling must not create a new follow-up task.")
        ws = no_follow["workspace"]
        assert ws.status == call_status
        assert not (ws.follow_up_date or "").strip(), "Unchecked Log Call must clear next follow-up."
        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, carmeco_id)
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
                    (contact_id, carmeco_id),
                ).fetchone()["n"]
            )
        if open_follow != 0 or open_queue != 0:
            _fail("Existing open follow-up must be completed when the call is not rescheduled.")
        if completed_follow < 1:
            _fail("Completing the call did not mark the previous follow-up task completed.")
        due_items = list_due_work_items(kind="follow_up", client_id=carmeco_id)
        if any(item.company_id == company_id and item.contact_id == contact_id for item in due_items):
            _fail("Completed follow-up still appears in due follow-up work items.")
        prospects = list_prospects(client_id=carmeco_id)
        match = next((p for p in prospects if p.id == company_id), None)
        if match is None:
            _fail("Dashboard prospects did not include the test company after Log Call.")
        assert match.status == call_status
        if match.follow_up_due:
            _fail("Dashboard still counted a follow-up after saving a call without scheduling one.")
        queue = list_work_queue(client_id=carmeco_id)
        if any(
            item.company_id == company_id and item.work_type == "Follow-Up"
            for item in queue.items
        ):
            _fail("Completed follow-up was not cleared from the active Work Queue.")

        if closed_status:
            closed_call = create_contact_activity(
                contact_id,
                ContactActivityCreate(
                    client_id=carmeco_id,
                    activity_type="Call",
                    status=closed_status,
                    outcome=closed_status,
                    notes=f"{MARKER} closed without follow-up",
                    schedule_follow_up=True,
                    created_by="Julie Magnani",
                ),
            )
            activity_ids.append(int(closed_call["activity_id"]))
            if closed_call.get("follow_up_activity_id"):
                _fail("Closed / Do Not Call must never require or create a follow-up.")
            assert closed_call["workspace"].status == closed_status

        call = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=carmeco_id,
                activity_type="Call",
                status=call_status,
                outcome=call_status,
                notes=f"{MARKER} logged call",
                next_action="Follow-Up",
                follow_up_date=today,
                follow_up_time="11:00",
                assigned_user_id=julie_id,
                schedule_follow_up=True,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(call["activity_id"]))
        follow_task_id = call.get("follow_up_activity_id")
        if not follow_task_id:
            _fail("Log Call with Schedule another follow-up must create a scheduled follow-up task.")
        activity_ids.append(int(follow_task_id))
        ws = call["workspace"]
        assert ws.status == call_status
        assert ws.next_action == "Follow-Up"
        assert (ws.follow_up_date or "").startswith(today)
        assert ws.follow_up_time[:5] == "11:00"
        call_titles = [item.title for item in ws.timeline]
        if "Call" not in call_titles:
            _fail(f"Log Call should create a Call activity, got {call_titles}")
        if "Follow-Up" not in call_titles:
            _fail(f"Log Call should create a Follow-Up task, got {call_titles}")

        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, carmeco_id)
            assignee = conn.execute(
                """
                SELECT assigned_user_id FROM work_queue_items
                WHERE contact_id = ? AND client_id = ?
                  AND lower(action_type) = 'follow-up'
                  AND lower(COALESCE(completion_status, 'open')) = 'open'
                """,
                (contact_id, carmeco_id),
            ).fetchone()
            if open_follow != 1:
                _fail(f"Expected one open follow-up activity, found {open_follow}.")
            if open_queue != 1:
                _fail(f"Expected one open follow-up task, found {open_queue}.")
            if int(assignee["assigned_user_id"] or 0) != julie_id:
                _fail("Follow-up task was not assigned to the selected Assigned Rep.")

        due_items = list_due_work_items(kind="follow_up", client_id=carmeco_id)
        if not any(item.company_id == company_id and item.contact_id == contact_id for item in due_items):
            _fail("Scheduled follow-up did not appear in due follow-up work items.")

        prospects = list_prospects(client_id=carmeco_id)
        match = next((p for p in prospects if p.id == company_id), None)
        if match is None:
            _fail("Dashboard prospects did not include the test company after Log Call.")
        assert match.status == call_status
        assert match.next_action == "Follow-Up"
        assert (match.follow_up_date or "").startswith(today)
        if not match.follow_up_due:
            _fail("Dashboard follow-up count did not include the new follow-up.")

        queue = list_work_queue(client_id=carmeco_id)
        queue_hit = [
            item
            for item in queue.items
            if item.company_id == company_id and item.work_type == "Follow-Up"
        ]
        if not queue_hit:
            _fail("Work Queue did not pick up the follow-up from Log Call.")
        if not any(item.assigned_user_id == julie_id for item in queue_hit):
            _fail("Work Queue follow-up is not assigned to the selected rep.")

        again = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=carmeco_id,
                activity_type="Call",
                status=call_status,
                outcome=call_status,
                notes=f"{MARKER} second call same slot",
                next_action="Follow-Up",
                follow_up_date=today,
                follow_up_time="11:00",
                assigned_user_id=julie_id,
                schedule_follow_up=True,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(again["activity_id"]))
        follow_task_id = again.get("follow_up_activity_id")
        if not follow_task_id:
            _fail("Rescheduled Log Call must create the next open follow-up task.")
        activity_ids.append(int(follow_task_id))
        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, carmeco_id)
            if open_follow != 1 or open_queue != 1:
                _fail("Rescheduled Log Call should leave exactly one open follow-up.")

        edited = update_contact_workflow(
            contact_id,
            ContactWorkflowUpdate(
                client_id=carmeco_id,
                follow_up_date=today,
                follow_up_time="16:45",
                user="Julie Magnani",
            ),
        )
        assert edited["workspace"].follow_up_time[:5] == "16:45"
        with get_connection() as conn:
            task_row = conn.execute(
                """
                SELECT follow_up_at FROM activities WHERE activity_id = ?
                """,
                (int(follow_task_id),),
            ).fetchone()
            queue_row = conn.execute(
                """
                SELECT due_time FROM work_queue_items
                WHERE contact_id = ? AND client_id = ?
                  AND lower(action_type) = 'follow-up'
                  AND lower(COALESCE(completion_status, 'open')) = 'open'
                """,
                (contact_id, carmeco_id),
            ).fetchone()
            if not str(task_row["follow_up_at"]).startswith(f"{today} 16:45"):
                _fail("Editing the follow-up date/time did not update the open follow-up activity.")
            if str(queue_row["due_time"] or "")[:5] != "16:45":
                _fail("Editing the follow-up date/time did not update the open follow-up task.")
            if int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE contact_id = ? AND client_id = ? AND activity_type = 'Follow-Up'
                      AND COALESCE(follow_up_completed, 0) = 0
                    """,
                    (contact_id, carmeco_id),
                ).fetchone()["n"]
            ) != 1:
                _fail("Editing follow-up created a duplicate open task.")

        note = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=carmeco_id,
                activity_type="Note",
                notes=f"{MARKER} added note",
                next_action="Should not apply",
                follow_up_date="1999-01-01",
                follow_up_time="08:00",
                status="Should not apply",
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(note["activity_id"]))
        ws = note["workspace"]
        assert ws.status == call_status, "Add Note must not change status."
        assert ws.next_action == "Follow-Up", "Add Note must not change next action."
        assert (ws.follow_up_date or "").startswith(today), "Add Note must not change follow-up."
        assert ws.follow_up_time[:5] == "16:45"

        follow = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=carmeco_id,
                activity_type="Follow-Up",
                notes=f"{MARKER} scheduled follow-up",
                next_action="Follow-Up",
                follow_up_date=today,
                follow_up_time="09:15",
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(follow["activity_id"]))
        if follow.get("follow_up_activity_id"):
            activity_ids.append(int(follow["follow_up_activity_id"]))

        ws = follow["workspace"]
        assert ws.status == call_status, "Schedule Follow-Up must not change status."
        assert ws.next_action == "Follow-Up"
        assert ws.follow_up_time[:5] == "09:15"
        titles = [item.title for item in ws.timeline]
        if "Follow-Up" not in titles:
            _fail(f"Schedule Follow-Up should keep a Follow-Up activity, got {titles}")
        if "Call" not in titles or "Note" not in titles:
            _fail(f"Timeline missing expected activities: {titles}")
        with get_connection() as conn:
            open_follow, open_queue = _open_follow_counts(conn, contact_id, carmeco_id)
        if open_follow != 1 or open_queue != 1:
            _fail("Schedule Follow-Up should leave exactly one open follow-up task.")
        times = [item.at for item in ws.timeline]
        assert times == sorted(times, reverse=True), "Timeline must be newest-first"

        brown_prospects = list_prospects(client_id=brown_id)
        brown_match = next((p for p in brown_prospects if p.id == company_id), None)
        if brown_match is None:
            _fail("Brown dashboard missing the shared test company.")
        assert brown_match.status == "Existing Brown Status"
        assert (brown_match.next_action or "") == ""

        queue2 = list_work_queue(client_id=carmeco_id)
        if not any(item.company_id == company_id for item in queue2.items):
            _fail("Work Queue lost the follow-up after activity save.")

        http_ok = False
        try:
            status_code, status_body = _http_json("GET", f"/api/clients/{carmeco_id}/statuses")
            if status_code != 200:
                print(f"HTTP API on {API} returned {status_code} for statuses; skipped live HTTP checks.")
            else:
                live_statuses = status_body.get("statuses") or []
                if not live_statuses:
                    _fail("Live /statuses returned no values.")
                http_status = next(
                    (s for s in live_statuses if s != target_status),
                    live_statuses[0],
                )
                code, saved_http = _http_json(
                    "PATCH",
                    f"/api/contacts/{contact_id}/workflow",
                    {
                        "client_id": carmeco_id,
                        "status": http_status,
                        "next_action": "Follow-Up",
                        "follow_up_date": today,
                        "follow_up_time": "16:00",
                        "user": "Julie Magnani",
                    },
                )
                if code != 200:
                    _fail(f"HTTP PATCH workflow failed ({code}): {saved_http}")
                http_ws = saved_http["workspace"]
                assert http_ws["status"] == http_status
                assert http_ws["client_id"] == carmeco_id
                code, call_http = _http_json(
                    "POST",
                    f"/api/contacts/{contact_id}/activities",
                    {
                        "client_id": carmeco_id,
                        "activity_type": "Call",
                        "status": http_status,
                        "outcome": http_status,
                        "notes": f"{MARKER} http call no follow-up",
                        "next_action": "Call back",
                        "follow_up_date": today,
                        "follow_up_time": "15:00",
                        "schedule_follow_up": False,
                        "created_by": "Julie Magnani",
                    },
                )
                if code != 200:
                    _fail(f"HTTP POST Log Call without follow-up failed ({code}): {call_http}")
                if call_http.get("follow_up_activity_id"):
                    _fail("HTTP Log Call without schedule_follow_up created a follow-up task.")
                if call_http.get("activity_id"):
                    activity_ids.append(int(call_http["activity_id"]))
                assert call_http["workspace"]["status"] == http_status
                assert not str(call_http["workspace"].get("follow_up_date") or "").strip()
                code, call_http = _http_json(
                    "POST",
                    f"/api/contacts/{contact_id}/activities",
                    {
                        "client_id": carmeco_id,
                        "activity_type": "Call",
                        "status": http_status,
                        "outcome": http_status,
                        "notes": f"{MARKER} http call",
                        "next_action": "Call back",
                        "follow_up_date": today,
                        "follow_up_time": "15:00",
                        "schedule_follow_up": True,
                        "created_by": "Julie Magnani",
                    },
                )
                if code != 200:
                    _fail(f"HTTP POST Log Call failed ({code}): {call_http}")
                if not call_http.get("follow_up_activity_id"):
                    _fail("HTTP Log Call did not create/update a follow-up task.")
                if call_http.get("activity_id"):
                    activity_ids.append(int(call_http["activity_id"]))
                if call_http.get("follow_up_activity_id"):
                    activity_ids.append(int(call_http["follow_up_activity_id"]))
                assert call_http["workspace"]["status"] == http_status
                assert str(call_http["workspace"].get("follow_up_time") or "")[:5] == "15:00"
                code, act = _http_json(
                    "POST",
                    f"/api/contacts/{contact_id}/activities",
                    {
                        "client_id": carmeco_id,
                        "activity_type": "Note",
                        "notes": f"{MARKER} http note",
                        "created_by": "Julie Magnani",
                    },
                )
                if code != 200:
                    _fail(f"HTTP POST activity failed ({code}): {act}")
                if act.get("activity_id"):
                    activity_ids.append(int(act["activity_id"]))
                http_ok = True
        except ConnectionError:
            print(f"API not reachable at {API}; data-layer checks still passed.")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            flora_ccr_after = (
                _ccr_snapshot(conn, flora_company_id) if flora_company_id else []
            )
            extra_contacts = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            ) - contact_count_before
        if flora_before is not None:
            assert flora_after == flora_before, "Flora Jia contact row changed."
            assert flora_ccr_after == flora_ccr_before, "Flora Jia company CCR changed."
        assert extra_contacts == 1, f"Unexpected extra contacts: {extra_contacts}"

        print("PASS: contact workflow is client-scoped, timeline newest-first,")
        print("      Dashboard/Work Queue updated, Flora Jia unchanged.")
        if http_ok:
            print("PASS: live HTTP GET statuses / PATCH workflow / POST activity on :8007.")
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


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


if __name__ == "__main__":
    sys.exit(main())
