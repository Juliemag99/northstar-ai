"""Client-scoped Log Outreach + deterministic Today's Priority Prospects queue.

Reuses activities + client_company_relationships. No email send. No appointment auto-create.
"""

from __future__ import annotations

from staff_context import resolve_staff_actor

from datetime import date, datetime
from typing import Any

from access import get_default_user, get_user_by_id, user_can_access_client
from activities_data import ACTIVITY_TYPES, create_activity
from client_workspace_data import list_relationship_statuses, update_relationship_status
from db import get_connection
from models import (
    ActivityCreateRequest,
    OutreachLogRequest,
    OutreachLogResult,
    PriorityProspectItem,
    PriorityProspectsResponse,
)

# Outcomes that never become CCR statuses (activity outcome only).
ACTIVITY_ONLY_OUTCOMES = frozenset(
    {
        "no answer",
        "spoke with contact",
    }
)

OUTREACH_TYPE_TO_ACTIVITY = {
    "call": "Call",
    "email": "Email",
    "other": "Note",
}

NEEDS_NEXT_ACTION_STATUSES = frozenset(
    {
        "left message",
        "contacted",
        "send information",
        "obtained new contact name/number",
        "qualified",
        "hot prospect",
    }
)

EXCLUDED_STATUS_EXACT = frozenset(
    {
        "closed",
        "completed",
        "current customer",
        "appointment set",
        "do not call",
        "dupe record",
        "duplicate",
        "competitor",
    }
)


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _date_only(value: Any) -> str:
    raw = _blank(value)
    if not raw:
        return ""
    return raw[:10]


def _norm_status(status: str) -> str:
    return _blank(status).lower()


def _is_excluded_status(status: str) -> bool:
    key = _norm_status(status)
    if not key:
        return False
    if key in EXCLUDED_STATUS_EXACT:
        return True
    if key.startswith("disqualified"):
        return True
    if "do not call" in key or "dupe" in key or key == "competitor":
        return True
    if "appointment set" in key:
        return True
    return False


def _is_future_nurture(status: str) -> bool:
    key = _norm_status(status)
    return "future" in key or "nurture" in key


def _is_hot(status: str, is_hot: int | bool) -> bool:
    del is_hot
    from appointments_data import is_hot_prospect_status

    return is_hot_prospect_status(status)


def _needs_next_action(status: str) -> bool:
    key = _norm_status(status)
    if key in NEEDS_NEXT_ACTION_STATUSES:
        return True
    if key.startswith("good fit"):
        return True
    return False


def _require_client_access(user_id: int, client_id: int) -> None:
    from access import require_write_client_id

    require_write_client_id(client_id, user_id=user_id)


def _valid_status_for_client(client_id: int, outcome: str) -> str | None:
    """Return canonical status string if outcome matches an existing CCR status."""
    wanted = _blank(outcome)
    if not wanted:
        return None
    if _norm_status(wanted) in ACTIVITY_ONLY_OUTCOMES:
        return None
    statuses = list_relationship_statuses(client_id=client_id)
    for s in statuses:
        if _norm_status(s) == _norm_status(wanted):
            return s
    return None


def log_outreach(
    body: OutreachLogRequest,
    *,
    user_id: int | None = None,
) -> OutreachLogResult:
    """Save client-scoped outreach activity (+ optional follow-up / status)."""
    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")
    client_id = int(body.client_id)
    _require_client_access(user.id, client_id)
    record_no = _blank(body.external_record_no)
    if not record_no:
        raise ValueError("external_record_no is required.")

    outreach_key = _blank(body.outreach_type).lower() or "call"
    activity_type = OUTREACH_TYPE_TO_ACTIVITY.get(outreach_key)
    if activity_type is None or activity_type not in ACTIVITY_TYPES:
        raise ValueError("outreach_type must be Call, Email, or Other.")

    outcome = _blank(body.outcome)
    notes = _blank(body.notes)
    from next_actions import canonicalize_next_action

    next_action = canonicalize_next_action(
        _blank(body.next_action),
        client_id=client_id,
        status=outcome,
    )
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"

    with get_connection() as conn:
        cl = conn.execute(
            "SELECT id, name, code FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not cl:
            raise LookupError("Client not found.")
        client_name = _blank(cl["name"]) or _blank(cl["code"]) or "Client"
        company = conn.execute(
            """
            SELECT co.id AS company_id, co.company_name, co.external_record_no
            FROM companies co
            JOIN client_company_relationships ccr ON ccr.company_id = co.id
            WHERE ccr.client_id = ?
              AND (TRIM(co.external_record_no) = ? OR TRIM(ccr.external_record_no) = ?)
            LIMIT 1
            """,
            (client_id, record_no, record_no),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found for this client.")
        company_id = int(company["company_id"])

    activity = create_activity(
        ActivityCreateRequest(
            client=client_name,
            client_id=client_id,
            external_record_no=record_no,
            user_id=user.id,
            contact_id=body.contact_id,
            activity_type=activity_type,
            outcome=outcome,
            notes=notes,
            assigned_user=created_by,
            created_by=created_by,
        )
    )

    follow_up_activity_id = None
    follow_date = _date_only(body.follow_up_date)
    if follow_date:
        follow_at = f"{follow_date} 09:00:00"
        follow = create_activity(
            ActivityCreateRequest(
                client=client_name,
                client_id=client_id,
                external_record_no=record_no,
                user_id=user.id,
                contact_id=body.contact_id,
                activity_type="Follow-Up",
                notes=next_action or f"Follow-up from outreach ({outcome or activity_type})",
                follow_up_at=follow_at,
                assigned_user=created_by,
                created_by=created_by,
            )
        )
        follow_up_activity_id = follow.activity_id
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET next_action = ?, follow_up_date = ?, updated_at = ?
                WHERE client_id = ? AND company_id = ?
                """,
                (
                    next_action or "Follow-Up",
                    follow_date,
                    _now(),
                    client_id,
                    company_id,
                ),
            )
            conn.commit()
    elif next_action:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET next_action = ?, updated_at = ?
                WHERE client_id = ? AND company_id = ?
                """,
                (next_action, _now(), client_id, company_id),
            )
            conn.commit()

    status_updated = False
    applied_status = ""
    matched = _valid_status_for_client(client_id, outcome)
    if matched:
        update_relationship_status(
            record_no,
            client=client_name,
            client_id=client_id,
            status=matched,
            user=created_by,
        )
        status_updated = True
        applied_status = matched

    from appointments_data import is_appointment_set_status

    open_email = _norm_status(outcome) == "send information" or (
        status_updated and _norm_status(applied_status) == "send information"
    )
    open_appt = is_appointment_set_status(outcome) or (
        status_updated and is_appointment_set_status(applied_status)
    )

    next_record = None
    if body.return_next:
        nxt = next_priority_prospect(
            client_id,
            after_record_no=record_no,
            user_id=user.id,
        )
        next_record = nxt.external_record_no if nxt else None

    return OutreachLogResult(
        ok=True,
        message="Outreach saved.",
        activity_id=activity.activity_id,
        follow_up_activity_id=follow_up_activity_id,
        client_id=client_id,
        external_record_no=record_no,
        status_updated=status_updated,
        applied_status=applied_status,
        open_email_compose=open_email,
        open_appointment_workflow=open_appt,
        next_external_record_no=next_record,
    )


def _earliest_due_for_row(
    *,
    follow_up_date: str,
    activity_due: str,
) -> str:
    dates = [d for d in (_date_only(follow_up_date), _date_only(activity_due)) if d]
    return min(dates) if dates else ""


def list_priority_prospects(
    client_id: int,
    *,
    user_id: int | None = None,
    limit: int = 50,
) -> PriorityProspectsResponse:
    """Deterministic Active Client calling queue for dashboard + Save & Next."""
    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_client_access(user.id, client_id)
    today = _today()
    lim = max(1, min(int(limit), 200))

    with get_connection() as conn:
        cl = conn.execute(
            "SELECT id, name, code FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not cl:
            raise LookupError("Client not found.")
        client_name = _blank(cl["name"]) or _blank(cl["code"])

        rows = conn.execute(
            """
            SELECT
                ccr.id AS relationship_id,
                ccr.client_id,
                ccr.company_id,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                    AS external_record_no,
                co.company_name,
                COALESCE(ccr.status, '') AS status,
                COALESCE(ccr.next_action, '') AS next_action,
                ccr.follow_up_date,
                COALESCE(ccr.is_hot, 0) AS is_hot,
                COALESCE(ccr.priority, '') AS priority,
                (
                    SELECT MIN(substr(a.follow_up_at, 1, 10))
                    FROM activities a
                    WHERE a.client_id = ccr.client_id
                      AND a.company_id = ccr.company_id
                      AND a.follow_up_at IS NOT NULL
                      AND TRIM(a.follow_up_at) != ''
                      AND COALESCE(a.follow_up_completed, 0) = 0
                ) AS activity_follow_due,
                (
                    SELECT TRIM(COALESCE(ct.first_name,'') || ' ' || COALESCE(ct.last_name,''))
                    FROM contacts ct
                    WHERE ct.company_id = co.id
                    ORDER BY ct.id ASC
                    LIMIT 1
                ) AS primary_contact
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
            """,
            (client_id,),
        ).fetchall()

    buckets: dict[int, list[PriorityProspectItem]] = {1: [], 2: [], 3: [], 4: [], 5: []}

    for r in rows:
        status = _blank(r["status"])
        if _is_excluded_status(status):
            continue
        due = _earliest_due_for_row(
            follow_up_date=_blank(r["follow_up_date"]),
            activity_due=_blank(r["activity_follow_due"]),
        )
        is_hot = _is_hot(status, r["is_hot"])
        nurture = _is_future_nurture(status)

        # Future/Nurture only when follow-up is due/overdue.
        if nurture and not (due and due <= today):
            continue

        if due and due < today:
            bucket = 1
            reason = f"Overdue follow-up ({due})"
        elif due and due == today:
            bucket = 2
            reason = f"Follow-up due today ({due})"
        elif is_hot:
            bucket = 3
            reason = "Hot prospect requiring action"
        elif _needs_next_action(status):
            # Skip if scheduled in the future
            if due and due > today:
                continue
            bucket = 4
            reason = f"Needs next action · {status}"
        elif _norm_status(status) == "new" or status == "":
            bucket = 5
            reason = "New assignment — first outreach"
        else:
            # Other active statuses without future follow-up: treat as needs attention
            if due and due > today:
                continue
            if nurture:
                continue
            bucket = 4
            reason = f"Active · {status or 'Review'}"

        buckets[bucket].append(
            PriorityProspectItem(
                client_id=client_id,
                client_name=client_name,
                company_id=int(r["company_id"]),
                relationship_id=int(r["relationship_id"]),
                external_record_no=_blank(r["external_record_no"]),
                company_name=_blank(r["company_name"]),
                status=status,
                primary_contact=_blank(r["primary_contact"]),
                next_action=_blank(r["next_action"]),
                follow_up_date=due or None,
                is_hot=is_hot,
                priority_bucket=bucket,
                priority_reason=reason,
                sort_key=f"{bucket:02d}|{due or '9999-99-99'}|{_blank(r['company_name']).lower()}|{_blank(r['external_record_no'])}",
            )
        )

    ordered: list[PriorityProspectItem] = []
    for b in (1, 2, 3, 4, 5):
        group = buckets[b]
        group.sort(
            key=lambda p: (
                p.follow_up_date or "9999-99-99",
                (p.company_name or "").lower(),
                p.external_record_no or "",
            )
        )
        ordered.extend(group)

    trimmed = ordered[:lim]
    return PriorityProspectsResponse(
        client_id=client_id,
        client_name=client_name,
        as_of_date=today,
        count=len(trimmed),
        items=trimmed,
    )


def next_priority_prospect(
    client_id: int,
    *,
    after_record_no: str,
    user_id: int | None = None,
) -> PriorityProspectItem | None:
    """Next actionable prospect after current record in the shared queue."""
    queue = list_priority_prospects(client_id, user_id=user_id, limit=200)
    after = _blank(after_record_no)
    found = False
    for item in queue.items:
        if found:
            return item
        if item.external_record_no == after:
            found = True
    # If current not in queue (e.g. just closed), return first
    if not found and queue.items:
        return queue.items[0]
    return None
