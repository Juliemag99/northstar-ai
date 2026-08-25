"""Tasks page follow-up actions: contact recovery, complete, reschedule, log-call.

Run: python test_tasks_page_actions.py

Creates and deletes temporary company/contact/CCR/work-queue rows.
Does not change Flora Jia, Whirlpool, or any other production identity values.
Uses the isolated testdb copy — never writes through live :8007.
"""

from __future__ import annotations

import testdb
import sys
import time
from datetime import date, timedelta

from client_workspace_data import (
    ensure_contact_workflow_schema,
    list_relationship_statuses,
)
from db import get_connection
from work_queue_data import list_dashboard_follow_ups, list_open_follow_up_tasks, list_work_queue

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSTASK"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


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
            ORDER BY client_id, id
            """,
            (company_id,),
        ).fetchall()
    ]


def _dash_items(client_id: int):
    dash = list_dashboard_follow_ups(client_id=client_id)
    return list(dash.overdue) + list(dash.due_today) + list(dash.upcoming)


def _dash_has_source(client_id: int, source: str, source_id: int) -> bool:
    return any(item.source == source and item.source_id == source_id for item in _dash_items(client_id))


def _queue_has_source(client_id: int, source: str, source_id: int) -> bool:
    queue = list_work_queue(client_id=client_id, work_type="follow-up")
    return any(item.source == source and item.source_id == source_id for item in queue.items)


def _cleanup(company_ids: list[int], contact_ids: list[int], activity_ids: list[int]) -> None:
    with get_connection() as conn:
        for aid in activity_ids:
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
        for contact_id in contact_ids:
            conn.execute("DELETE FROM contact_client_workflows WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM work_queue_items WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        for company_id in company_ids:
            conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
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


def _insert_company(conn, *, record_no: str, name: str, carmeco_id: int, brown_id: int) -> tuple[int, int]:
    cur = conn.execute(
        """
        INSERT INTO companies (external_record_no, company_name, city, state)
        VALUES (?, ?, 'Testville', 'OH')
        """,
        (record_no, name),
    )
    company_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, external_record_no, status, next_action, follow_up_date
        ) VALUES (?, ?, ?, 'Existing Brown Status', '', NULL)
        """,
        (brown_id, company_id, record_no),
    )
    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, external_record_no, status, next_action, follow_up_date
        ) VALUES (?, ?, ?, 'Existing Carmeco Status', 'Follow-Up', ?)
        """,
        (carmeco_id, company_id, f"{record_no}-C", "2020-01-01"),
    )
    return company_id, int(cur.lastrowid)


def _insert_open_follow_up(
    conn,
    *,
    client_id: int,
    company_id: int,
    relationship_id: int,
    record_no: str,
    julie_id: int,
    contact_id: int | None,
    due_date: str,
    due_time: str,
    notes: str,
) -> tuple[int, int]:
    from activities_data import insert_activity_row

    activity_id = insert_activity_row(
        conn,
        client_id=client_id,
        company_id=company_id,
        relationship_id=relationship_id,
        external_record_no=record_no,
        user_id=julie_id,
        contact_id=contact_id,
        activity_type="Follow-Up",
        notes=notes,
        follow_up_at=f"{due_date} {due_time}:00",
        assigned_user="Julie Magnani",
        created_by="Julie Magnani",
    )
    cur = conn.execute(
        """
        INSERT INTO work_queue_items (
            client_id, company_id, relationship_id, contact_id, action_type,
            due_date, due_time, assigned_user_id, assigned_user, completion_status,
            source, source_activity_id, notes, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'Follow-Up', ?, ?, ?, 'Julie Magnani', 'open',
                  'test', ?, ?, 'Julie Magnani', datetime('now'), datetime('now'))
        """,
        (
            client_id,
            company_id,
            relationship_id,
            contact_id,
            due_date,
            due_time,
            julie_id,
            activity_id,
            notes,
        ),
    )
    return int(cur.lastrowid), activity_id


def main() -> int:
    ensure_contact_workflow_schema()
    company_ids: list[int] = []
    contact_ids: list[int] = []
    activity_ids: list[int] = []
    flora_before: dict | None = None
    flora_company_id: int | None = None
    flora_ccr_before: list[dict] = []
    whirlpool_ccr_before: list[dict] = []

    try:
        with get_connection() as conn:
            flora_before = _snapshot_flora(conn)
            if flora_before is not None:
                flora_company_id = int(flora_before["company_id"])
                flora_ccr_before = _ccr_snapshot(conn, flora_company_id)
            whirlpool = conn.execute(
                "SELECT id FROM companies WHERE id = ?",
                (WHIRLPOOL_ID,),
            ).fetchone()
            if whirlpool is not None:
                whirlpool_ccr_before = _ccr_snapshot(conn, WHIRLPOOL_ID)
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
            named_record = f"{MARKER}-{stamp}-J"
            empty_record = f"{MARKER}-{stamp}-E"
            named_company_id, named_rel_id = _insert_company(
                conn,
                record_no=named_record,
                name=f"{MARKER} James Co {stamp}",
                carmeco_id=carmeco_id,
                brown_id=brown_id,
            )
            company_ids.append(named_company_id)
            empty_company_id, empty_rel_id = _insert_company(
                conn,
                record_no=empty_record,
                name=f"{MARKER} Empty Co {stamp}",
                carmeco_id=carmeco_id,
                brown_id=brown_id,
            )
            company_ids.append(empty_company_id)
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'TaskJames', 'Logsdon', 'Buyer', 1)
                """,
                (named_company_id, named_record),
            )
            james_id = int(cur.lastrowid)
            contact_ids.append(james_id)
            brown_named_before = conn.execute(
                """
                SELECT status, next_action, follow_up_date, assigned_user_id
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (brown_id, named_company_id),
            ).fetchone()
            brown_empty_before = conn.execute(
                """
                SELECT status, next_action, follow_up_date, assigned_user_id
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (brown_id, empty_company_id),
            ).fetchone()
            yesterday = (date.today() - timedelta(days=3)).isoformat()
            named_queue_id, named_activity_id = _insert_open_follow_up(
                conn,
                client_id=carmeco_id,
                company_id=named_company_id,
                relationship_id=named_rel_id,
                record_no=named_record,
                julie_id=julie_id,
                contact_id=None,
                due_date=yesterday,
                due_time="09:00",
                notes=f"{MARKER} missing contact_id",
            )
            activity_ids.append(named_activity_id)
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    email, source_row_index
                ) VALUES (?, ?, '', '', '', 'info@example.test', 1)
                """,
                (empty_company_id, empty_record),
            )
            stub_id = int(cur.lastrowid)
            contact_ids.append(stub_id)
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'Other', 'Person', 'Buyer', 2)
                """,
                (empty_company_id, empty_record),
            )
            other_id = int(cur.lastrowid)
            contact_ids.append(other_id)
            empty_queue_id, empty_activity_id = _insert_open_follow_up(
                conn,
                client_id=carmeco_id,
                company_id=empty_company_id,
                relationship_id=empty_rel_id,
                record_no=empty_record,
                julie_id=julie_id,
                contact_id=None,
                due_date=yesterday,
                due_time="10:00",
                notes=f"{MARKER} nameless stub contact_id",
            )
            conn.execute(
                "UPDATE work_queue_items SET contact_id = ? WHERE id = ?",
                (stub_id, empty_queue_id),
            )
            activity_ids.append(empty_activity_id)
            conn.commit()

        if james_id == FLORA_ID or (flora_before and james_id == int(flora_before["id"])):
            _fail("Refusing to use Flora Jia as test contact.")

        named_row = next(
            (
                item
                for item in _dash_items(carmeco_id)
                if item.source == "work_queue" and item.source_id == named_queue_id
            ),
            None,
        )
        if named_row is None:
            _fail("Company-level James-style task missing from Carmeco Tasks/Dashboard.")
        if named_row.contact_id != james_id:
            _fail(
                f"Missing contact_id should recover TaskJames (got {named_row.contact_id})."
            )
        if "TaskJames" not in (named_row.contact_name or ""):
            _fail("Recovered contact name should be TaskJames Logsdon.")
        if any(
            item.source == "work_queue" and item.source_id == named_queue_id
            for item in _dash_items(brown_id)
        ):
            _fail("Carmeco James-style task leaked onto Brown Tasks.")

        empty_row = next(
            (
                item
                for item in _dash_items(carmeco_id)
                if item.source == "work_queue" and item.source_id == empty_queue_id
            ),
            None,
        )
        if empty_row is None:
            _fail("Company-only task with stale contact_id missing from Carmeco Tasks.")
        if empty_row.contact_id is not None:
            _fail(
                "Nameless stub contact_id must not expose Open Contact "
                f"(contact_id={empty_row.contact_id}, name={empty_row.contact_name!r}, "
                f"other_named={other_id})."
            )
        if (empty_row.contact_name or "").strip():
            _fail(
                "Company-only task without contacts should have an empty contact name "
                f"(name={empty_row.contact_name!r}, company={empty_row.company_name!r})."
            )

        code, recovered_http = testdb.http_json(
            "GET", f"/api/dashboard/follow-ups?client_id={carmeco_id}"
        )
        if code != 200:
            _fail(f"HTTP dashboard follow-ups failed ({code}): {recovered_http}")
        http_items = (
            (recovered_http.get("overdue") or [])
            + (recovered_http.get("due_today") or [])
            + (recovered_http.get("upcoming") or [])
        )
        http_named = next(
            (
                item
                for item in http_items
                if item.get("source") == "work_queue" and int(item.get("source_id") or 0) == named_queue_id
            ),
            None,
        )
        if http_named is None or int(http_named.get("contact_id") or 0) != james_id:
            _fail("HTTP dashboard did not recover a valid contact_id for the James-style row.")
        http_empty = next(
            (
                item
                for item in http_items
                if item.get("source") == "work_queue" and int(item.get("source_id") or 0) == empty_queue_id
            ),
            None,
        )
        if http_empty is None or http_empty.get("contact_id") not in (None, "", 0):
            _fail("HTTP dashboard exposed a contact_id for a company-only task.")

        new_date = (date.today() + timedelta(days=11)).isoformat()
        open_before = [
            (item.source, item.source_id)
            for item in list_open_follow_up_tasks(client_id=carmeco_id)
            if item.source_id in {named_queue_id, empty_queue_id}
        ]
        code, rescheduled = testdb.http_json(
            "POST",
            "/api/follow-up-tasks/reschedule",
            {
                "client_id": carmeco_id,
                "source": "work_queue",
                "source_id": named_queue_id,
                "company_id": named_company_id,
                "contact_id": james_id,
                "follow_up_date": new_date,
                "follow_up_time": "14:15",
                "assigned_user_id": julie_id,
                "next_action": "Follow-Up",
                "notes": f"{MARKER} reschedule",
                "created_by": "Julie Magnani",
            },
        )
        if code != 200:
            _fail(f"HTTP reschedule failed ({code}): {rescheduled}")
        if rescheduled.get("activity_id"):
            activity_ids.append(int(rescheduled["activity_id"]))
        open_after = [
            (item.source, item.source_id, item.due_date, (item.due_time or "")[:5])
            for item in list_open_follow_up_tasks(client_id=carmeco_id)
            if item.source_id in {named_queue_id, empty_queue_id}
            or (
                item.source == "work_queue"
                and item.company_id == named_company_id
                and item.due_date == new_date
            )
        ]
        matching = [
            row for row in open_after if row[0] == "work_queue" and row[1] == named_queue_id
        ]
        if len(matching) != 1:
            _fail(f"Reschedule must update the same task, not create a duplicate: {open_after}")
        if matching[0][2] != new_date or matching[0][3] != "14:15":
            _fail("Reschedule did not update the task date/time.")
        if not _dash_has_source(carmeco_id, "work_queue", named_queue_id):
            _fail("Rescheduled task missing from Tasks/Dashboard.")
        if not _queue_has_source(carmeco_id, "work_queue", named_queue_id):
            _fail("Rescheduled task missing from Work Queue.")
        if len(open_before) != 2:
            _fail("Expected both temp tasks to be open before reschedule.")

        code, completed = testdb.http_json(
            "POST",
            "/api/follow-up-tasks/complete",
            {
                "client_id": carmeco_id,
                "source": "work_queue",
                "source_id": named_queue_id,
                "company_id": named_company_id,
                "contact_id": james_id,
                "notes": f"{MARKER} complete note",
                "created_by": "Julie Magnani",
            },
        )
        if code != 200:
            _fail(f"HTTP complete failed ({code}): {completed}")
        if completed.get("activity_id"):
            activity_ids.append(int(completed["activity_id"]))
        if _dash_has_source(carmeco_id, "work_queue", named_queue_id):
            _fail("Completed task still on Tasks/Dashboard.")
        if _queue_has_source(carmeco_id, "work_queue", named_queue_id):
            _fail("Completed task still on Work Queue.")
        if _dash_has_source(carmeco_id, "activity_follow_up", named_activity_id):
            _fail("Linked Follow-Up activity still listed as an open task.")

        statuses = list_relationship_statuses(client_id=carmeco_id)
        if not statuses:
            _fail("Client statuses API source returned no statuses — cannot hardcode.")
        status = next((s for s in statuses if s.strip() and "appointment" not in s.lower()), statuses[0])
        code, logged = testdb.http_json(
            "POST",
            "/api/work-queue/log-call",
            {
                "client_id": carmeco_id,
                "external_record_no": empty_record,
                "contact_id": None,
                "outcome": status,
                "notes": f"{MARKER} company-level call",
                "status": status,
                "next_action": "Follow-Up",
                "queue_source": "work_queue",
                "queue_source_id": empty_queue_id,
                "complete_current": True,
                "created_by": "Julie Magnani",
            },
        )
        if code != 200:
            _fail(f"HTTP log-call failed ({code}): {logged}")
        if logged.get("activity_id"):
            activity_ids.append(int(logged["activity_id"]))
        if logged.get("follow_up_activity_id"):
            activity_ids.append(int(logged["follow_up_activity_id"]))
        if not logged.get("completed_queue_item"):
            _fail("Log Call & Complete should complete the current company-level task.")
        if _dash_has_source(carmeco_id, "work_queue", empty_queue_id):
            _fail("Company-level task still on Tasks after Log Call & Complete.")
        if _queue_has_source(carmeco_id, "work_queue", empty_queue_id):
            _fail("Company-level task still on Work Queue after Log Call & Complete.")

        with get_connection() as conn:
            named_follow = conn.execute(
                """
                SELECT follow_up_completed, completion_status FROM activities
                WHERE activity_id = ?
                """,
                (named_activity_id,),
            ).fetchone()
            named_queue = conn.execute(
                "SELECT completion_status FROM work_queue_items WHERE id = ?",
                (named_queue_id,),
            ).fetchone()
            empty_follow = conn.execute(
                """
                SELECT follow_up_completed, completion_status FROM activities
                WHERE activity_id = ?
                """,
                (empty_activity_id,),
            ).fetchone()
            notes = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE company_id = ? AND client_id = ? AND activity_type = 'Note'
                      AND outcome = 'Follow-up completed'
                      AND notes LIKE ?
                    """,
                    (named_company_id, carmeco_id, f"{MARKER}%"),
                ).fetchone()["n"]
            )
            calls = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE company_id = ? AND client_id = ? AND activity_type = 'Call'
                      AND notes LIKE ?
                    """,
                    (empty_company_id, carmeco_id, f"{MARKER}%"),
                ).fetchone()["n"]
            )
            brown_named_after = conn.execute(
                """
                SELECT status, next_action, follow_up_date, assigned_user_id
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (brown_id, named_company_id),
            ).fetchone()
            brown_empty_after = conn.execute(
                """
                SELECT status, next_action, follow_up_date, assigned_user_id
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (brown_id, empty_company_id),
            ).fetchone()
            flora_after = _snapshot_flora(conn)
            flora_ccr_after = _ccr_snapshot(conn, flora_company_id) if flora_company_id else []
            whirlpool_ccr_after = (
                _ccr_snapshot(conn, WHIRLPOOL_ID) if whirlpool_ccr_before else []
            )

        if int(named_follow["follow_up_completed"] or 0) != 1:
            _fail("Completing the task deleted or failed to preserve the Follow-Up activity.")
        if _blank(named_queue["completion_status"]).lower() != "completed":
            _fail("Work queue item was not marked completed.")
        if int(empty_follow["follow_up_completed"] or 0) != 1:
            _fail("Log Call & Complete left the linked Follow-Up activity open.")
        if notes != 1:
            _fail("Complete Task should add exactly one completion Note.")
        if calls != 1:
            _fail("Company-level Log Call should create one Call activity.")
        assert dict(brown_named_after) == dict(brown_named_before)
        assert dict(brown_empty_after) == dict(brown_empty_before)
        if flora_before is not None:
            assert flora_after == flora_before, "Flora Jia contact row changed."
            assert flora_ccr_after == flora_ccr_before, "Flora Jia company CCR changed."
        if whirlpool_ccr_before:
            assert whirlpool_ccr_after == whirlpool_ccr_before, "Whirlpool CCR changed."

        print("PASS: Tasks recover a valid contact_id for missing references.")
        print("PASS: Tasks hide Open Contact when no contact exists.")
        print("PASS: Complete / Reschedule / Log Call & Complete are Active Client scoped.")
        print("      Completed tasks leave Dashboard, Work Queue, and Tasks; history remains.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        _cleanup(company_ids, contact_ids, activity_ids)


if __name__ == "__main__":
    sys.exit(main())
