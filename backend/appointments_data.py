"""Live CRM appointment records — distinct from imported sales/appointment events."""

from __future__ import annotations

from staff_context import resolve_staff_actor, runtime_attribution_name

from datetime import date, datetime, timezone

from db import get_connection
from models import (
    AppointmentActionResult,
    AppointmentDetailsPayload,
    AppointmentListResponse,
    AppointmentRecord,
    AppointmentSummary,
)

APPOINTMENT_TYPES = ("Phone", "Video Meeting", "In Person", "Other")
APPOINTMENT_SOURCES = ("Phone Call", "Email Marketing")
APPOINTMENT_STATUSES = ("scheduled", "completed", "cancelled")
DEFAULT_TIMEZONE = "America/Chicago"
TIMEZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "UTC",
)
APPOINTMENT_SET_LABELS = ("Appointment Set", "Appt Set Email Marketing")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today() -> str:
    return date.today().isoformat()


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def is_appointment_set_status(status: str) -> bool:
    key = _blank(status).lower()
    if not key:
        return False
    if key in {"appointment set", "appt set", "appt set email marketing"}:
        return True
    if "appointment set" in key:
        return True
    if key.startswith("appt set"):
        return True
    return False


def is_excluded_from_hot_queue(status: str) -> bool:
    """Statuses that must never appear as Work Queue / Dashboard Hot."""
    key = _blank(status).lower()
    if not key:
        return True
    if "appointment set" in key or key.startswith("appt set"):
        return True
    if key == "closed" or key.startswith("closed"):
        return True
    if key in {"do not call", "dnc"} or "do not call" in key:
        return True
    if key.startswith("disqualified") or "disqualified" in key:
        return True
    if key == "current customer" or key.startswith("current customer"):
        return True
    if key == "competitor" or "competitor" in key:
        return True
    return False


def is_hot_prospect_status(status: str) -> bool:
    """True only when the current active-client status is Hot Prospect.

    Sticky is_hot flags, milestones, AI insights, and excluded working/closed
    statuses must not classify a record as Hot.
    """
    key = _blank(status).lower()
    if is_excluded_from_hot_queue(key):
        return False
    return key == "hot prospect"


def count_hot_prospects(client_ids: list[int]) -> int:
    """Count companies whose current client status is Hot Prospect.

    Shared by Dashboard and Work Queue. Ignores leftover is_hot flags.
    """
    ids = [int(cid) for cid in client_ids if cid]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM client_company_relationships
            WHERE client_id IN ({placeholders})
              AND lower(trim(status)) = 'hot prospect'
            """,
            ids,
        ).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def sync_hot_flag_with_status(conn, *, relationship_id: int, status: str | None = None) -> None:
    """Keep is_hot aligned with the current client status.

    Hot Dashboard / Work Queue classification uses current status. Appointment Set
    and other working statuses must not keep a leftover Hot flag.
    """
    current = status
    if current is None:
        row = conn.execute(
            "SELECT status FROM client_company_relationships WHERE id = ?",
            (int(relationship_id),),
        ).fetchone()
        current = _blank(row["status"]) if row is not None else ""
    conn.execute(
        """
        UPDATE client_company_relationships
        SET is_hot = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (1 if is_hot_prospect_status(current) else 0, int(relationship_id)),
    )


def _appointment_when_label(row) -> str:
    if int(row["datetime_tbd"] or 0) == 1 or not _blank(row["appointment_date"]):
        return "Date/Time TBD"
    date_part = _blank(row["appointment_date"])[:10]
    time_part = _blank(row["start_time"])[:5]
    tz = _blank(row["timezone"])
    return " ".join(part for part in (date_part, time_part, tz) if part) or "—"


def appointment_source_for_status(status: str) -> str:
    key = _blank(status).lower()
    if "email marketing" in key:
        return "Email Marketing"
    return "Phone Call"


def ensure_appointments_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                company_id INTEGER NOT NULL,
                relationship_id INTEGER NOT NULL,
                contact_id INTEGER,
                activity_id INTEGER,
                appointment_date TEXT NOT NULL DEFAULT '',
                start_time TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'America/Chicago',
                datetime_tbd INTEGER NOT NULL DEFAULT 0,
                appointment_type TEXT NOT NULL DEFAULT 'Phone',
                location_or_link TEXT NOT NULL DEFAULT '',
                revenue_specialist_user_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'Phone Call',
                status TEXT NOT NULL DEFAULT 'scheduled',
                grade TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                follow_up_notes TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                cancelled_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE,
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
                FOREIGN KEY (activity_id) REFERENCES activities(activity_id) ON DELETE SET NULL,
                FOREIGN KEY (revenue_specialist_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_appointments_client_status_date
                ON appointments(client_id, status, appointment_date, start_time, id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_appointments_company
                ON appointments(client_id, company_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_appointments_contact
                ON appointments(client_id, contact_id)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_idempotency
                ON appointments(client_id, idempotency_key)
                WHERE TRIM(idempotency_key) != ''
            """
        )
        existing_cols = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(appointments)").fetchall()
        }
        for name, declaration in (
            ("cancellation_reason", "TEXT NOT NULL DEFAULT ''"),
            ("cancelled_by", "TEXT NOT NULL DEFAULT ''"),
            ("completed_by", "TEXT NOT NULL DEFAULT ''"),
            ("cancel_activity_id", "INTEGER"),
            ("complete_activity_id", "INTEGER"),
        ):
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE appointments ADD COLUMN {name} {declaration}")
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def normalize_appointment_payload(
    payload: AppointmentDetailsPayload | dict | None,
    *,
    status: str = "",
) -> dict:
    if payload is None:
        raise ValueError("Appointment details are required for Appointment Set.")
    if isinstance(payload, AppointmentDetailsPayload):
        data = payload
    else:
        data = AppointmentDetailsPayload.model_validate(payload)
    tbd = bool(data.datetime_tbd)
    appt_date = _blank(data.appointment_date)[:10]
    start_time = _blank(data.start_time)[:5]
    if not tbd and (not appt_date or not start_time):
        raise ValueError("Appointment date and time are required, or choose Date/Time TBD.")
    if tbd:
        appt_date = ""
        start_time = ""
    appt_type = _blank(data.appointment_type) or "Phone"
    if appt_type not in APPOINTMENT_TYPES:
        raise ValueError(
            f"Unsupported appointment type '{appt_type}'. "
            f"Allowed: {', '.join(APPOINTMENT_TYPES)}."
        )
    source = _blank(data.source) or appointment_source_for_status(status)
    if source not in APPOINTMENT_SOURCES:
        raise ValueError(
            f"Unsupported appointment source '{source}'. "
            f"Allowed: {', '.join(APPOINTMENT_SOURCES)}."
        )
    tz = _blank(data.timezone) or DEFAULT_TIMEZONE
    if tz not in TIMEZONES:
        tz = DEFAULT_TIMEZONE
    rds = data.revenue_specialist_user_id
    rds_id = int(rds) if rds is not None and int(rds) > 0 else None
    return {
        "appointment_date": appt_date,
        "start_time": start_time,
        "timezone": tz,
        "datetime_tbd": 1 if tbd else 0,
        "appointment_type": appt_type,
        "location_or_link": _blank(data.location_or_link),
        "revenue_specialist_user_id": rds_id,
        "notes": _blank(data.notes),
        "source": source,
        "idempotency_key": _blank(data.idempotency_key),
    }


def _bucket_for_row(row, today: str) -> str:
    status = _blank(row["status"]).lower()
    if status == "cancelled":
        return "cancelled"
    if status == "completed":
        return "past"
    if int(row["datetime_tbd"] or 0) == 1 or not _blank(row["appointment_date"]):
        return "upcoming"
    appt_date = _blank(row["appointment_date"])[:10]
    if appt_date == today:
        return "today"
    if appt_date and appt_date < today:
        return "past"
    return "upcoming"


def _row_to_record(row, *, today: str | None = None) -> AppointmentRecord:
    today = today or _today()
    contact_name = " ".join(
        part
        for part in (_blank(row["first_name"]), _blank(row["last_name"]))
        if part
    ).strip()
    return AppointmentRecord(
        id=int(row["id"]),
        client_id=int(row["client_id"]),
        company_id=int(row["company_id"]),
        relationship_id=int(row["relationship_id"]),
        contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
        activity_id=int(row["activity_id"]) if row["activity_id"] is not None else None,
        company_name=_blank(row["company_name"]),
        company_record_no=_blank(row["external_record_no"]),
        contact_name=contact_name,
        appointment_date=_blank(row["appointment_date"]),
        start_time=_blank(row["start_time"]),
        timezone=_blank(row["timezone"]) or DEFAULT_TIMEZONE,
        datetime_tbd=bool(row["datetime_tbd"]),
        appointment_type=_blank(row["appointment_type"]) or "Phone",
        location_or_link=_blank(row["location_or_link"]),
        revenue_specialist_user_id=(
            int(row["revenue_specialist_user_id"])
            if row["revenue_specialist_user_id"] is not None
            else None
        ),
        revenue_specialist_name=_blank(row["revenue_specialist_name"]),
        notes=_blank(row["notes"]),
        source=_blank(row["source"]) or "Phone Call",
        status=_blank(row["status"]) or "scheduled",
        grade=_blank(row["grade"]),
        outcome=_blank(row["outcome"]),
        follow_up_notes=_blank(row["follow_up_notes"]),
        created_by=_blank(row["created_by"]),
        created_at=_blank(row["created_at"]),
        updated_at=_blank(row["updated_at"]),
        cancellation_reason=_blank(row["cancellation_reason"]) if "cancellation_reason" in row.keys() else "",
        cancelled_by=_blank(row["cancelled_by"]) if "cancelled_by" in row.keys() else "",
        cancelled_at=_blank(row["cancelled_at"]),
        completed_by=_blank(row["completed_by"]) if "completed_by" in row.keys() else "",
        completed_at=_blank(row["completed_at"]),
        bucket=_bucket_for_row(row, today),
    )


_SELECT_APPOINTMENT = """
    SELECT
        a.*,
        co.company_name,
        COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
            AS external_record_no,
        ct.first_name,
        ct.last_name,
        u.full_name AS revenue_specialist_name
    FROM appointments a
    JOIN companies co ON co.id = a.company_id
    JOIN client_company_relationships ccr ON ccr.id = a.relationship_id
    LEFT JOIN contacts ct ON ct.id = a.contact_id
    LEFT JOIN users u ON u.id = a.revenue_specialist_user_id
"""


def fetch_appointment_row(conn, appointment_id: int):
    return conn.execute(
        _SELECT_APPOINTMENT + " WHERE a.id = ?",
        (int(appointment_id),),
    ).fetchone()


def insert_appointment_row(
    conn,
    *,
    client_id: int,
    company_id: int,
    relationship_id: int,
    contact_id: int | None,
    activity_id: int | None,
    created_by: str,
    payload: dict,
) -> tuple[int, bool]:
    """Insert one appointment on an open transaction.

    Returns (appointment_id, created). Duplicate idempotency keys return the
    existing row without inserting another appointment.
    """
    ensure_appointments_schema(conn)
    key = _blank(payload.get("idempotency_key"))
    if key:
        existing = conn.execute(
            """
            SELECT id FROM appointments
            WHERE client_id = ? AND idempotency_key = ?
            """,
            (int(client_id), key),
        ).fetchone()
        if existing is not None:
            sync_hot_flag_with_status(conn, relationship_id=relationship_id)
            return int(existing["id"]), False
    now = _now_iso()
    try:
        cur = conn.execute(
            """
            INSERT INTO appointments (
                client_id, company_id, relationship_id, contact_id, activity_id,
                appointment_date, start_time, timezone, datetime_tbd,
                appointment_type, location_or_link, revenue_specialist_user_id,
                notes, source, status, grade, outcome, follow_up_notes,
                idempotency_key, created_by, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', '', '', '',
                ?, ?, ?, ?
            )
            """,
            (
                int(client_id),
                int(company_id),
                int(relationship_id),
                contact_id,
                activity_id,
                payload["appointment_date"],
                payload["start_time"],
                payload["timezone"],
                int(payload["datetime_tbd"]),
                payload["appointment_type"],
                payload["location_or_link"],
                payload["revenue_specialist_user_id"],
                payload["notes"],
                payload["source"],
                key,
                created_by,
                now,
                now,
            ),
        )
    except Exception as exc:
        message = str(exc).lower()
        if key and "unique" in message:
            existing = conn.execute(
                """
                SELECT id FROM appointments
                WHERE client_id = ? AND idempotency_key = ?
                """,
                (int(client_id), key),
            ).fetchone()
            if existing is not None:
                sync_hot_flag_with_status(conn, relationship_id=relationship_id)
                return int(existing["id"]), False
        raise
    appt_id = int(cur.lastrowid)
    rec = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
            AS rec
        FROM client_company_relationships ccr
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.id = ?
        """,
        (int(relationship_id),),
    ).fetchone()
    from access import get_default_user
    from activities_data import insert_activity_row

    actor = resolve_staff_actor()
    insert_activity_row(
        conn,
        client_id=int(client_id),
        company_id=int(company_id),
        relationship_id=int(relationship_id),
        external_record_no=_blank(rec["rec"]) if rec is not None else "",
        user_id=actor.id if actor is not None else None,
        contact_id=contact_id,
        activity_type="Appointment Created",
        outcome=_blank(payload.get("source")) or "Appointment Set",
        notes=_blank(payload.get("notes")) or "Appointment created.",
        assigned_user=created_by,
        created_by=created_by,
        activity_at=now,
    )
    sync_hot_flag_with_status(conn, relationship_id=relationship_id)
    return appt_id, True


def ensure_appointment_set_milestone(conn, *, activity_id: int, created_by: str) -> None:
    """Create one Appointment Set milestone for this activity if missing."""
    if not _table_exists(conn, "revenue_milestones"):
        return
    act = conn.execute(
        "SELECT * FROM activities WHERE activity_id = ?",
        (int(activity_id),),
    ).fetchone()
    if act is None:
        return
    exists = conn.execute(
        "SELECT 1 FROM revenue_milestones WHERE source_activity_id = ?",
        (int(activity_id),),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        INSERT INTO revenue_milestones (
            client_id, company_id, relationship_id, external_record_no,
            contact_id, milestone_type, milestone_date, amount,
            reference_number, source, notes, created_by, created_at,
            source_activity_id
        ) VALUES (?, ?, ?, ?, ?, 'Appointment Set', ?, NULL, '', 'appointment', ?, ?, ?, ?)
        """,
        (
            int(act["client_id"]),
            int(act["company_id"]),
            int(act["relationship_id"]),
            _blank(act["external_record_no"]),
            act["contact_id"],
            _blank(act["activity_at"]) or _now_iso(),
            _blank(act["notes"]) or _blank(act["outcome"]),
            created_by or _blank(act["created_by"]) or "System",
            _now_iso(),
            int(activity_id),
        ),
    )


def _require_client_access(client_id: int):
    from access import get_default_user, require_write_client_id

    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(client_id, user_id=user.id)
    return user


def _client_appointment_records(client_id: int) -> list[AppointmentRecord]:
    client_id = int(client_id)
    if client_id <= 0:
        raise ValueError("client_id is required.")
    _require_client_access(client_id)
    ensure_appointments_schema()
    today = _today()
    with get_connection() as conn:
        rows = conn.execute(
            _SELECT_APPOINTMENT
            + """
            WHERE a.client_id = ?
            ORDER BY
                CASE WHEN a.datetime_tbd = 1 OR TRIM(a.appointment_date) = '' THEN 1 ELSE 0 END,
                a.appointment_date ASC,
                a.start_time ASC,
                a.id ASC
            """,
            (client_id,),
        ).fetchall()
    return [_row_to_record(row, today=today) for row in rows]


def list_appointments(
    client_id: int,
    *,
    bucket: str = "",
    q: str = "",
    appointment_type: str = "",
    source: str = "",
    revenue_specialist_user_id: int | None = None,
    limit: int = 200,
) -> AppointmentListResponse:
    records = _client_appointment_records(client_id)
    wanted = _blank(bucket).lower()
    query = _blank(q).lower()
    type_filter = _blank(appointment_type)
    source_filter = _blank(source)
    counts = {"upcoming": 0, "today": 0, "past": 0, "cancelled": 0}
    for item in records:
        counts[item.bucket] = counts.get(item.bucket, 0) + 1
    filtered = records
    if wanted in counts:
        filtered = [item for item in filtered if item.bucket == wanted]
    if type_filter:
        filtered = [item for item in filtered if item.appointment_type == type_filter]
    if source_filter:
        filtered = [item for item in filtered if item.source == source_filter]
    if revenue_specialist_user_id is not None and int(revenue_specialist_user_id) > 0:
        rds = int(revenue_specialist_user_id)
        filtered = [item for item in filtered if item.revenue_specialist_user_id == rds]
    if query:
        def _matches(item: AppointmentRecord) -> bool:
            hay = " ".join(
                [
                    item.company_name,
                    item.contact_name,
                    item.notes,
                    item.location_or_link,
                    item.appointment_type,
                    item.source,
                    item.revenue_specialist_name,
                    item.grade,
                    item.outcome,
                ]
            ).lower()
            return query in hay

        filtered = [item for item in filtered if _matches(item)]
    limit = max(1, min(int(limit or 200), 500))
    return AppointmentListResponse(
        client_id=int(client_id),
        bucket=wanted,
        items=filtered[:limit],
        counts=counts,
        set_count=counts["upcoming"] + counts["today"] + counts["past"],
    )


def appointment_summary(client_id: int) -> AppointmentSummary:
    records = _client_appointment_records(client_id)
    counts = {"upcoming": 0, "today": 0, "past": 0, "cancelled": 0}
    for item in records:
        counts[item.bucket] = counts.get(item.bucket, 0) + 1
    upcoming = [item for item in records if item.bucket == "upcoming"][:5]
    today_items = [item for item in records if item.bucket == "today"][:8]
    scheduled_count = sum(
        1 for item in records if item.status == "scheduled" and item.bucket != "cancelled"
    )
    return AppointmentSummary(
        client_id=int(client_id),
        set_count=counts["upcoming"] + counts["today"] + counts["past"],
        scheduled_count=scheduled_count,
        today_count=counts["today"],
        upcoming_count=counts["upcoming"],
        past_count=counts["past"],
        cancelled_count=counts["cancelled"],
        upcoming=upcoming,
        today=today_items,
    )


def _load_owned_appointment(conn, appointment_id: int, client_id: int):
    row = fetch_appointment_row(conn, appointment_id)
    if row is None:
        raise LookupError("Appointment not found.")
    if int(row["client_id"]) != int(client_id):
        raise PermissionError("Appointment is not in the Active Client.")
    return row


def reschedule_appointment(appointment_id: int, body) -> AppointmentActionResult:
    from models import AppointmentRescheduleRequest

    if not isinstance(body, AppointmentRescheduleRequest):
        body = AppointmentRescheduleRequest.model_validate(body)
    client_id = int(body.client_id)
    _require_client_access(client_id)
    payload = normalize_appointment_payload(
        AppointmentDetailsPayload(
            appointment_date=body.appointment_date,
            start_time=body.start_time,
            timezone=body.timezone,
            datetime_tbd=body.datetime_tbd,
            appointment_type=body.appointment_type or "Phone",
            location_or_link=body.location_or_link,
            revenue_specialist_user_id=body.revenue_specialist_user_id,
            notes=body.notes,
            source=body.source or "Phone Call",
            idempotency_key="",
        )
    )
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_appointments_schema(conn)
            row = _load_owned_appointment(conn, appointment_id, client_id)
            if _blank(row["status"]).lower() == "cancelled":
                raise ValueError("Cancelled appointments cannot be rescheduled.")
            old_when = " ".join(
                part
                for part in (_blank(row["appointment_date"])[:10], _blank(row["start_time"])[:5])
                if part
            ) or "TBD"
            new_when = " ".join(
                part
                for part in (payload["appointment_date"], payload["start_time"])
                if part
            ) or "TBD"
            conn.execute(
                """
                UPDATE appointments
                SET appointment_date = ?, start_time = ?, timezone = ?, datetime_tbd = ?,
                    appointment_type = ?, location_or_link = ?,
                    revenue_specialist_user_id = ?, notes = ?, source = ?,
                    status = 'scheduled', cancelled_at = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    payload["appointment_date"],
                    payload["start_time"],
                    payload["timezone"],
                    payload["datetime_tbd"],
                    payload["appointment_type"] or _blank(row["appointment_type"]) or "Phone",
                    payload["location_or_link"]
                    if body.location_or_link is not None
                    else _blank(row["location_or_link"]),
                    payload["revenue_specialist_user_id"]
                    if body.revenue_specialist_user_id is not None
                    else row["revenue_specialist_user_id"],
                    payload["notes"] if body.notes is not None else _blank(row["notes"]),
                    payload["source"] or _blank(row["source"]) or "Phone Call",
                    _now_iso(),
                    int(appointment_id),
                    client_id,
                ),
            )
            from access import get_default_user

            actor = resolve_staff_actor()
            created_by = _blank(actor.full_name) if actor is not None else "Julie Magnani"
            _write_appointment_history_activity(
                conn,
                row=row,
                activity_type="Appointment Rescheduled",
                notes=payload["notes"]
                or f"Appointment rescheduled from {old_when} to {new_when}.",
                outcome=f"{old_when} → {new_when}",
                created_by=created_by,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        updated = fetch_appointment_row(conn, appointment_id)
    return AppointmentActionResult(
        ok=True,
        message="Appointment rescheduled.",
        appointment=_row_to_record(updated),
    )


def _write_appointment_history_activity(
    conn,
    *,
    row,
    activity_type: str,
    notes: str,
    outcome: str,
    created_by: str,
) -> int:
    from access import get_default_user
    from activities_data import insert_activity_row

    user = resolve_staff_actor()
    activity_id = insert_activity_row(
        conn,
        client_id=int(row["client_id"]),
        company_id=int(row["company_id"]),
        relationship_id=int(row["relationship_id"]),
        external_record_no=_blank(row["external_record_no"]),
        user_id=user.id if user is not None else None,
        contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
        activity_type=activity_type,
        outcome=outcome,
        notes=notes,
        assigned_user=created_by,
        created_by=created_by,
    )
    conn.execute(
        """
        UPDATE activities
        SET completion_status = 'completed', follow_up_completed = 1
        WHERE activity_id = ?
        """,
        (activity_id,),
    )
    return activity_id


def cancel_appointment(appointment_id: int, body) -> AppointmentActionResult:
    from models import AppointmentCancelRequest
    from next_actions import canonicalize_next_action

    if not isinstance(body, AppointmentCancelRequest):
        body = AppointmentCancelRequest.model_validate(body)
    client_id = int(body.client_id)
    _require_client_access(client_id)
    reason = _blank(body.reason) or _blank(body.notes)
    new_status = _blank(body.new_status)
    created_by = runtime_attribution_name(resolve_staff_actor(), getattr(body, "created_by", ""))
    if not reason:
        raise ValueError("A cancellation reason is required.")
    if not new_status:
        raise ValueError("Select the contact's new client status after cancellation.")
    next_action = canonicalize_next_action(
        _blank(body.next_action),
        client_id=client_id,
        status=new_status,
    )
    follow_date = _blank(body.follow_up_date)[:10]
    follow_time = _blank(body.follow_up_time)[:5]
    follow_stored = ""
    if follow_date:
        follow_stored = f"{follow_date} {follow_time}:00" if follow_time else follow_date
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_appointments_schema(conn)
            row = _load_owned_appointment(conn, appointment_id, client_id)
            if _blank(row["status"]).lower() == "cancelled":
                conn.commit()
                return AppointmentActionResult(
                    ok=True,
                    message="Appointment already cancelled.",
                    appointment=_row_to_record(row),
                )
            allowed = {
                _blank(r["status"])
                for r in conn.execute(
                    """
                    SELECT DISTINCT status FROM client_company_relationships
                    WHERE client_id = ? AND TRIM(status) != ''
                    """,
                    (client_id,),
                )
            }
            allowed.update(APPOINTMENT_SET_LABELS)
            if allowed and new_status not in allowed:
                raise ValueError(
                    f"Status '{new_status}' is not a known status for this client."
                )
            now = _now_iso()
            previous_when = _appointment_when_label(row)
            merged_notes = _blank(row["notes"])
            cancel_line = f"Cancelled: {reason}"
            merged_notes = f"{merged_notes}\n{cancel_line}".strip() if merged_notes else cancel_line
            history_notes = "\n".join(
                [
                    f"Appointment cancelled {now} by {created_by}.",
                    f"Reason: {reason}",
                    f"Previous appointment: {previous_when}",
                    f"New status: {new_status}",
                ]
                + ([f"Next action: {next_action}"] if next_action else [])
            )
            cancel_activity_id = _write_appointment_history_activity(
                conn,
                row=row,
                activity_type="Appointment Cancelled",
                notes=history_notes,
                outcome=reason,
                created_by=created_by,
            )
            if follow_date:
                from activities_data import insert_activity_row
                from access import get_default_user

                user = resolve_staff_actor()
                follow_at = f"{follow_date} {follow_time}:00" if follow_time else f"{follow_date} 09:00:00"
                insert_activity_row(
                    conn,
                    client_id=int(row["client_id"]),
                    company_id=int(row["company_id"]),
                    relationship_id=int(row["relationship_id"]),
                    external_record_no=_blank(row["external_record_no"]),
                    user_id=user.id if user is not None else None,
                    contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
                    activity_type="Follow-Up",
                    notes=next_action or "Follow-up after appointment cancellation",
                    follow_up_at=follow_at,
                    assigned_user=created_by,
                    created_by=created_by,
                )
            conn.execute(
                """
                UPDATE appointments
                SET status = 'cancelled', notes = ?, cancellation_reason = ?,
                    cancelled_by = ?, cancelled_at = ?, cancel_activity_id = ?,
                    updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    merged_notes,
                    reason,
                    created_by,
                    now,
                    cancel_activity_id,
                    now,
                    int(appointment_id),
                    client_id,
                ),
            )
            conn.execute(
                """
                UPDATE client_company_relationships
                SET status = ?,
                    next_action = ?,
                    follow_up_date = CASE WHEN ? != '' THEN ? ELSE follow_up_date END,
                    is_hot = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND client_id = ?
                """,
                (
                    new_status,
                    next_action,
                    follow_stored,
                    follow_stored or None,
                    1 if is_hot_prospect_status(new_status) else 0,
                    int(row["relationship_id"]),
                    client_id,
                ),
            )
            if row["contact_id"] is not None:
                conn.execute(
                    """
                    UPDATE contact_client_workflows
                    SET status = ?,
                        next_action = ?,
                        follow_up_date = CASE WHEN ? != '' THEN ? ELSE follow_up_date END,
                        follow_up_time = CASE WHEN ? != '' THEN ? ELSE follow_up_time END,
                        updated_at = datetime('now')
                    WHERE contact_id = ? AND client_id = ?
                    """,
                    (
                        new_status,
                        next_action,
                        follow_stored,
                        follow_date or None,
                        follow_time,
                        follow_time,
                        int(row["contact_id"]),
                        client_id,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        updated = fetch_appointment_row(conn, appointment_id)
    return AppointmentActionResult(
        ok=True,
        message="Appointment cancelled.",
        appointment=_row_to_record(updated),
    )


def complete_appointment(appointment_id: int, body) -> AppointmentActionResult:
    from models import AppointmentCompleteRequest

    if not isinstance(body, AppointmentCompleteRequest):
        body = AppointmentCompleteRequest.model_validate(body)
    client_id = int(body.client_id)
    _require_client_access(client_id)
    created_by = runtime_attribution_name(resolve_staff_actor(), getattr(body, "created_by", ""))
    grade = _blank(body.grade)
    outcome = _blank(body.outcome)
    follow_up_notes = _blank(body.follow_up_notes)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_appointments_schema(conn)
            row = _load_owned_appointment(conn, appointment_id, client_id)
            if _blank(row["status"]).lower() == "cancelled":
                raise ValueError("Cancelled appointments cannot be completed.")
            now = _now_iso()
            already_completed = _blank(row["status"]).lower() == "completed"
            complete_activity_id = row["complete_activity_id"] if "complete_activity_id" in row.keys() else None
            if not already_completed:
                history_notes = "\n".join(
                    part
                    for part in (
                        f"Appointment completed {now} by {created_by}.",
                        f"Grade: {grade}" if grade else "",
                        f"Outcome: {outcome}" if outcome else "",
                        f"Notes: {follow_up_notes}" if follow_up_notes else "",
                    )
                    if part
                )
                complete_activity_id = _write_appointment_history_activity(
                    conn,
                    row=row,
                    activity_type="Appointment Completed",
                    notes=history_notes,
                    outcome=outcome or grade,
                    created_by=created_by,
                )
            conn.execute(
                """
                UPDATE appointments
                SET status = 'completed', grade = ?, outcome = ?, follow_up_notes = ?,
                    completed_by = ?, completed_at = COALESCE(completed_at, ?),
                    complete_activity_id = ?, updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    grade,
                    outcome,
                    follow_up_notes,
                    created_by if not already_completed else _blank(row["completed_by"]) or created_by,
                    now,
                    complete_activity_id,
                    now,
                    int(appointment_id),
                    client_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        updated = fetch_appointment_row(conn, appointment_id)
    return AppointmentActionResult(
        ok=True,
        message="Appointment marked complete.",
        appointment=_row_to_record(updated),
    )
