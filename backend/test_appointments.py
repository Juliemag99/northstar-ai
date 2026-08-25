"""Live Appointment Set workflow — Log Call through dashboard/list/actions.

Run: python test_appointments.py

Creates and deletes temporary company/contact/CCR/appointment rows.
Does not change Flora Jia or any other existing production identity values.
"""

from __future__ import annotations

import testdb
import sys
import time
import uuid
from datetime import date, timedelta

from appointments_data import (
    appointment_summary,
    cancel_appointment,
    complete_appointment,
    ensure_appointments_schema,
    list_appointments,
    reschedule_appointment,
)
from activities_data import list_client_activities
from client_workspace_data import (
    create_contact_activity,
    ensure_contact_workflow_schema,
    get_contact_workspace,
)
from db import get_connection
from models import (
    AppointmentCancelRequest,
    AppointmentCompleteRequest,
    AppointmentDetailsPayload,
    AppointmentRescheduleRequest,
    ContactActivityCreate,
)

FLORA_ID = 4631
MARKER = "NSAPPT"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _timeline_types(
    client_id: int, *, contact_id: int | None = None, company_id: int | None = None
) -> list[str]:
    page = list_client_activities(
        client_id, contact_id=contact_id, company_id=company_id, limit=200, offset=0
    )
    return [item.activity_type for item in page.items]


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
            ORDER BY client_id
            """,
            (company_id,),
        ).fetchall()
    ]


def _cleanup(company_id: int | None, contact_id: int | None) -> None:
    if company_id is None and contact_id is None:
        return
    with get_connection() as conn:
        if contact_id is not None:
            conn.execute("DELETE FROM appointments WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM activities WHERE contact_id = ?", (contact_id,))
            conn.execute(
                "DELETE FROM work_queue_items WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        if company_id is not None:
            conn.execute("DELETE FROM appointments WHERE company_id = ?", (company_id,))
            conn.execute(
                "DELETE FROM revenue_milestones WHERE company_id = ?",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM activities WHERE company_id = ?",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM work_queue_items WHERE company_id = ?",
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


def _log_call(
    contact_id: int,
    client_id: int,
    *,
    status: str,
    notes: str,
    appointment: AppointmentDetailsPayload | None,
    schedule_follow_up: bool = False,
    follow_date: str = "",
    follow_time: str = "",
    assigned_user_id: int | None = None,
) -> dict:
    return create_contact_activity(
        contact_id,
        ContactActivityCreate(
            client_id=client_id,
            activity_type="Call",
            status=status,
            outcome=status,
            notes=notes,
            schedule_follow_up=schedule_follow_up,
            follow_up_date=follow_date,
            follow_up_time=follow_time,
            assigned_user_id=assigned_user_id,
            created_by="Julie Magnani",
            appointment=appointment,
        ),
    )


def main() -> int:
    ensure_contact_workflow_schema()
    ensure_appointments_schema()
    company_id: int | None = None
    contact_id: int | None = None
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
                (record_no, f"{MARKER} Appt Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action, is_hot
                ) VALUES (?, ?, ?, 'Hot Prospect', 'Call', 1)
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
                ) VALUES (?, ?, 'ApptAct', 'Tester', 'Buyer', 1)
                """,
                (company_id, record_no),
            )
            contact_id = int(cur.lastrowid)
            if int(contact_id) == FLORA_ID:
                _fail("Refusing to use Flora Jia as the test contact.")
            conn.commit()

        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        try:
            _log_call(
                contact_id,
                brown_id,
                status="Appointment Set",
                notes="missing datetime",
                appointment=AppointmentDetailsPayload(
                    datetime_tbd=False,
                    idempotency_key=str(uuid.uuid4()),
                ),
            )
            _fail("Expected date/time validation error.")
        except ValueError as exc:
            if "date and time" not in str(exc).lower() and "tbd" not in str(exc).lower():
                raise

        key = str(uuid.uuid4())
        first = _log_call(
            contact_id,
            brown_id,
            status="Appointment Set",
            notes="Set plant intro",
            assigned_user_id=julie_id,
            appointment=AppointmentDetailsPayload(
                appointment_date=tomorrow,
                start_time="10:30",
                timezone="America/Chicago",
                appointment_type="Video Meeting",
                location_or_link="https://meet.example.test/nsappt",
                revenue_specialist_user_id=julie_id,
                notes="Bring capability deck",
                source="Phone Call",
                idempotency_key=key,
            ),
        )
        if not first.get("appointment_id"):
            _fail("Log Call Appointment Set did not create an appointment.")
        appt_id = int(first["appointment_id"])
        ws = get_contact_workspace(contact_id, client_id=brown_id)
        if _blank(ws.status) != "Appointment Set":
            _fail(f"Brown status not updated; got {ws.status!r}.")
        with get_connection() as conn:
            brown_hot = int(
                conn.execute(
                    """
                    SELECT COALESCE(is_hot, 0) AS is_hot
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id = ?
                    """,
                    (company_id, brown_id),
                ).fetchone()["is_hot"]
            )
        if brown_hot != 0:
            _fail("Appointment Set left a stale Hot flag on the Brown relationship.")
        from access import get_default_user
        from work_queue_data import list_work_queue

        hot_queue = list_work_queue(get_default_user().id, client_id=brown_id, hot=True)
        if any(item.company_id == company_id for item in hot_queue.items):
            _fail("Appointment Set company still appears under Hot on the Work Queue.")

        with get_connection() as conn:
            calls = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE contact_id = ? AND client_id = ? AND activity_type = 'Call'
                    """,
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
            appts = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM appointments
                    WHERE contact_id = ? AND client_id = ?
                    """,
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
            milestones = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM revenue_milestones
                    WHERE company_id = ? AND client_id = ?
                      AND milestone_type = 'Appointment Set'
                    """,
                    (company_id, brown_id),
                ).fetchone()["n"]
            )
            carmeco_status = _blank(
                conn.execute(
                    """
                    SELECT status FROM client_company_relationships
                    WHERE company_id = ? AND client_id = ?
                    """,
                    (company_id, carmeco_id),
                ).fetchone()["status"]
            )
        if calls != 1:
            _fail(f"Expected 1 call activity, got {calls}.")
        if appts != 1:
            _fail(f"Expected 1 appointment, got {appts}.")
        if milestones < 1:
            _fail("Appointment Set milestone was not created.")
        if carmeco_status != "Existing Carmeco Status":
            _fail("Carmeco relationship was modified.")
        types = _timeline_types(brown_id, contact_id=contact_id, company_id=company_id)
        if "Appointment Created" not in types:
            _fail("Appointment Created missing from Activities after booking.")

        dup = _log_call(
            contact_id,
            brown_id,
            status="Appointment Set",
            notes="double click",
            assigned_user_id=julie_id,
            appointment=AppointmentDetailsPayload(
                appointment_date=tomorrow,
                start_time="10:30",
                timezone="America/Chicago",
                appointment_type="Video Meeting",
                idempotency_key=key,
            ),
        )
        if int(dup.get("appointment_id") or 0) != appt_id:
            _fail("Duplicate save created a different appointment.")
        with get_connection() as conn:
            calls = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE contact_id = ? AND client_id = ? AND activity_type = 'Call'
                    """,
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
            appts = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM appointments WHERE contact_id = ? AND client_id = ?",
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
            created = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE contact_id = ? AND client_id = ?
                      AND activity_type = 'Appointment Created'
                    """,
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
        if calls != 1 or appts != 1:
            _fail(f"Duplicate save created extra rows (calls={calls}, appts={appts}).")
        if created != 1:
            _fail(f"Duplicate save created extra Appointment Created rows ({created}).")

        listing = list_appointments(brown_id, bucket="upcoming")
        if not any(item.id == appt_id for item in listing.items):
            _fail("Appointment missing from Upcoming list.")
        brown_summary = appointment_summary(brown_id)
        if brown_summary.set_count < 1 or brown_summary.upcoming_count < 1:
            _fail("Dashboard appointment count did not increase for Brown.")
        carmeco_summary = appointment_summary(carmeco_id)
        if any(item.id == appt_id for item in carmeco_summary.upcoming + carmeco_summary.today):
            _fail("Brown appointment leaked into Carmeco summary.")

        rescheduled = reschedule_appointment(
            appt_id,
            AppointmentRescheduleRequest(
                client_id=brown_id,
                appointment_date=today,
                start_time="14:00",
                timezone="America/Chicago",
                appointment_type="Video Meeting",
                location_or_link="https://meet.example.test/nsappt",
                source="Phone Call",
            ),
        )
        if rescheduled.appointment.bucket != "today":
            _fail(f"Rescheduled appointment should be Today, got {rescheduled.appointment.bucket}.")
        today_list = list_appointments(brown_id, bucket="today")
        if not any(item.id == appt_id for item in today_list.items):
            _fail("Rescheduled appointment missing from Today.")
        if appointment_summary(brown_id).today_count < 1:
            _fail("Dashboard Appointments Today count did not update.")
        types = _timeline_types(brown_id, contact_id=contact_id, company_id=company_id)
        if "Appointment Rescheduled" not in types:
            _fail("Appointment Rescheduled missing from Activities.")
        if "Appointment Created" not in types:
            _fail("Appointment Created missing after reschedule.")

        completed = complete_appointment(
            appt_id,
            AppointmentCompleteRequest(
                client_id=brown_id,
                grade="A",
                outcome="Proceeding",
                follow_up_notes="Send quote packet.",
            ),
        )
        if completed.appointment.status != "completed":
            _fail("Mark Complete did not set status=completed.")
        if completed.appointment.grade != "A" or completed.appointment.outcome != "Proceeding":
            _fail("Grade/outcome were not saved.")
        with get_connection() as conn:
            complete_acts = conn.execute(
                """
                SELECT notes, created_by FROM activities
                WHERE contact_id = ? AND client_id = ?
                  AND activity_type = 'Appointment Completed'
                ORDER BY activity_id DESC
                """,
                (contact_id, brown_id),
            ).fetchall()
        if not complete_acts:
            _fail("Mark Complete did not create an Appointment Completed activity.")
        complete_notes = _blank(complete_acts[0]["notes"]).lower()
        if "grade: a" not in complete_notes or "proceeding" not in complete_notes:
            _fail("Appointment Completed activity is missing grade/outcome.")
        if "julie magnani" not in _blank(complete_acts[0]["created_by"]).lower():
            _fail("Appointment Completed activity is missing the user.")
        past_list = list_appointments(brown_id, bucket="past")
        if not any(item.id == appt_id for item in past_list.items):
            _fail("Completed appointment missing from Past.")
        types = _timeline_types(brown_id, contact_id=contact_id, company_id=company_id)
        if "Appointment Completed" not in types:
            _fail("Appointment Completed missing from Activities.")
        if "Appointment Created" not in types:
            _fail("Appointment Created missing after completion.")

        tbd_key = str(uuid.uuid4())
        tbd = _log_call(
            contact_id,
            brown_id,
            status="Appt Set Email Marketing",
            notes="email campaign set",
            assigned_user_id=julie_id,
            appointment=AppointmentDetailsPayload(
                datetime_tbd=True,
                appointment_type="Phone",
                source="Email Marketing",
                idempotency_key=tbd_key,
            ),
        )
        tbd_id = int(tbd.get("appointment_id") or 0)
        if tbd_id <= 0:
            _fail("TBD appointment was not created.")
        tbd_row = next(item for item in list_appointments(brown_id).items if item.id == tbd_id)
        if tbd_row.source != "Email Marketing":
            _fail(f"Expected Email Marketing source, got {tbd_row.source!r}.")
        if tbd_row.bucket != "upcoming":
            _fail("TBD appointment should be Upcoming.")
        try:
            cancel_appointment(
                tbd_id,
                AppointmentCancelRequest(client_id=brown_id, new_status="New"),
            )
            _fail("Expected cancellation reason to be required.")
        except ValueError as exc:
            if "reason" not in str(exc).lower():
                raise
        try:
            cancel_appointment(
                tbd_id,
                AppointmentCancelRequest(client_id=brown_id, reason="Prospect asked to wait."),
            )
            _fail("Expected new status to be required.")
        except ValueError as exc:
            if "status" not in str(exc).lower():
                raise
        cancelled = cancel_appointment(
            tbd_id,
            AppointmentCancelRequest(
                client_id=brown_id,
                reason="Prospect asked to wait.",
                new_status="New",
                next_action="",
            ),
        )
        if cancelled.appointment.status != "cancelled":
            _fail("Cancel did not set status=cancelled.")
        if cancelled.appointment.cancellation_reason != "Prospect asked to wait.":
            _fail("Cancelled appointment is missing the reason.")
        cancelled_list = list_appointments(brown_id, bucket="cancelled")
        if not any(item.id == tbd_id for item in cancelled_list.items):
            _fail("Cancelled appointment missing from Cancelled.")
        if any(item.id == tbd_id for item in list_appointments(brown_id, bucket="upcoming").items):
            _fail("Cancelled appointment remained in Upcoming.")
        if any(item.id == tbd_id for item in list_appointments(brown_id, bucket="today").items):
            _fail("Cancelled appointment remained in Today.")
        cancel_ws = get_contact_workspace(contact_id, client_id=brown_id)
        if _blank(cancel_ws.status) == "Hot Prospect":
            _fail("Cancellation automatically returned the contact to Hot Prospect.")
        if _blank(cancel_ws.status) != "New":
            _fail(f"Cancellation did not apply the selected status; got {cancel_ws.status!r}.")
        timeline_titles = [item.title for item in (cancel_ws.timeline or [])]
        company_titles = [item.title for item in (cancel_ws.company_timeline or [])]
        if "Appointment Cancelled" not in timeline_titles:
            _fail("Appointment Cancelled activity missing from Contact Activity.")
        if "Appointment Cancelled" not in company_titles:
            _fail("Appointment Cancelled activity missing from Company Activity.")
        cancel_item = next(
            item for item in cancel_ws.timeline if item.title == "Appointment Cancelled"
        )
        body = (cancel_item.body or "").lower()
        if "prospect asked to wait" not in body or "previous appointment" not in body:
            _fail("Cancellation activity is missing reason or previous appointment date.")
        types = _timeline_types(brown_id, contact_id=contact_id, company_id=company_id)
        if "Appointment Cancelled" not in types:
            _fail("Appointment Cancelled missing from Activities.")
        if "new status: new" not in body:
            _fail("Cancellation activity is missing the new status.")
        with get_connection() as conn:
            after_cancel_hot = int(
                conn.execute(
                    """
                    SELECT COALESCE(is_hot, 0) AS is_hot
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id = ?
                    """,
                    (company_id, brown_id),
                ).fetchone()["is_hot"]
            )
        if after_cancel_hot != 0:
            _fail("Cancellation left a Hot flag while status is not Hot Prospect.")

        past_key = str(uuid.uuid4())
        past = _log_call(
            contact_id,
            brown_id,
            status="Appointment Set",
            notes="already occurred",
            assigned_user_id=julie_id,
            appointment=AppointmentDetailsPayload(
                appointment_date=yesterday,
                start_time="09:00",
                appointment_type="In Person",
                location_or_link="Plant lobby",
                idempotency_key=past_key,
            ),
        )
        past_id = int(past.get("appointment_id") or 0)
        past_after = list_appointments(brown_id, bucket="past")
        if not any(item.id == past_id for item in past_after.items):
            _fail("Past-dated appointment missing from Past.")

        from models import WorkQueueLogCallRequest
        from work_queue_data import log_work_queue_call

        wq_key = str(uuid.uuid4())
        wq_first = log_work_queue_call(
            WorkQueueLogCallRequest(
                client_id=brown_id,
                external_record_no=record_no,
                contact_id=contact_id,
                outcome="Connected",
                notes="WQ appointment set",
                status="Appointment Set",
                complete_current=False,
                created_by="Julie Magnani",
                appointment=AppointmentDetailsPayload(
                    appointment_date=tomorrow,
                    start_time="11:15",
                    timezone="America/Chicago",
                    appointment_type="Phone",
                    location_or_link="",
                    revenue_specialist_user_id=julie_id,
                    notes="Work Queue path",
                    source="Phone Call",
                    idempotency_key=wq_key,
                ),
            )
        )
        wq_appt_id = int(wq_first.appointment_id or 0)
        if wq_appt_id <= 0:
            _fail("Work Queue Log Call did not create an appointment.")
        wq_dup = log_work_queue_call(
            WorkQueueLogCallRequest(
                client_id=brown_id,
                external_record_no=record_no,
                contact_id=contact_id,
                outcome="Connected",
                notes="WQ double click",
                status="Appointment Set",
                complete_current=False,
                created_by="Julie Magnani",
                appointment=AppointmentDetailsPayload(
                    appointment_date=tomorrow,
                    start_time="11:15",
                    timezone="America/Chicago",
                    appointment_type="Phone",
                    idempotency_key=wq_key,
                ),
            )
        )
        if int(wq_dup.appointment_id or 0) != wq_appt_id:
            _fail("Work Queue duplicate save created a different appointment.")
        with get_connection() as conn:
            wq_appts = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM appointments
                    WHERE company_id = ? AND client_id = ? AND idempotency_key = ?
                    """,
                    (company_id, brown_id, wq_key),
                ).fetchone()["n"]
            )
            wq_calls = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM activities
                    WHERE contact_id = ? AND client_id = ?
                      AND activity_type = 'Call' AND notes = 'WQ appointment set'
                    """,
                    (contact_id, brown_id),
                ).fetchone()["n"]
            )
        if wq_appts != 1:
            _fail(f"Work Queue duplicate save created extra appointments ({wq_appts}).")
        if wq_calls != 1:
            _fail(f"Work Queue duplicate save created extra calls ({wq_calls}).")
        if not any(
            item.id == wq_appt_id for item in list_appointments(brown_id, bucket="upcoming").items
        ):
            _fail("Work Queue appointment missing from Upcoming.")

        if flora_before is not None:
            with get_connection() as conn:
                flora_after = _snapshot_flora(conn)
                if flora_after != flora_before:
                    _fail("Flora Jia identity values changed.")
                if flora_company_id is not None:
                    if _ccr_snapshot(conn, flora_company_id) != flora_ccr_before:
                        _fail("Flora Jia company relationships changed.")

        print("test_appointments: ok")
        return 0
    finally:
        _cleanup(company_id, contact_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"test_appointments: FAIL: {exc}", file=sys.stderr)
        raise
