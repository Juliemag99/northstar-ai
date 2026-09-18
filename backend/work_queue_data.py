"""NorthStar Rev Development Specialist Work Queue.

Status and work queue are separate:
- Status = New never becomes Call Due by itself.
- Calls / Follow-Ups require explicit scheduled/open actions.
- Overdue requires an incomplete task past its due date/time.
"""

from __future__ import annotations

from staff_context import resolve_staff_actor

from datetime import date, datetime
from typing import Literal

from access import (
    get_default_user,
    get_user_by_id,
    list_clients_for_user,
    require_write_client_id,
    resolve_dashboard_client_ids,
    user_can_access_client,
)
from appointments_data import count_hot_prospects, is_hot_prospect_status
from data_steward import sql_active_ccr, sql_active_company
from db import DB_PATH, get_connection
from models import (
    DashboardFollowUpItem,
    DashboardFollowUpsResponse,
    FollowUpTaskActionResult,
    FollowUpTaskCompleteRequest,
    FollowUpTaskRescheduleRequest,
    WorkQueueCompleteRequest,
    WorkQueueInsightSummary,
    WorkQueueItem,
    WorkQueueListResponse,
    WorkQueueLogCallRequest,
    WorkQueueLogCallResult,
    WorkQueueNextResponse,
    WorkQueueRow,
    WorkQueueSummary,
    WorkQueueSummaryV2,
)

NEEDS_NEXT_ACTION_STATUSES = frozenset(
    {
        "left message",
        "contacted",
        "send information",
    }
)

TERMINAL_STATUS_EXACT = frozenset(
    {
        "current customer",
        "closed",
        "completed",
    }
)


def _is_terminal_relationship_status(status: str) -> bool:
    key = status.strip().lower()
    if not key:
        return False
    if key in TERMINAL_STATUS_EXACT:
        return True
    if key.startswith("disqualified"):
        return True
    return False


def _needs_next_action_status(status: str) -> bool:
    return status.strip().lower() in NEEDS_NEXT_ACTION_STATUSES


QueueKind = Literal["call", "follow_up"]

CALL_ACTION_TYPES = frozenset({"call"})
FOLLOW_UP_ACTION_TYPES = frozenset({"follow-up", "follow_up", "followup"})

WORK_TYPES = (
    "Call",
    "Follow-Up",
    "Appointment",
    "WebLead",
    "Hot",
    "New Assignment",
    "Needs Next Action",
    "Research",
    "Other",
    "Cross-Client Opportunity",
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def _today() -> str:
    return date.today().isoformat()


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _date_only(value: object | None) -> str | None:
    text = _blank(value)
    if not text:
        return None
    return text[:10]


def _time_only(value: object | None) -> str:
    text = _blank(value)
    if len(text) >= 16 and text[10] in {" ", "T"}:
        return text[11:16]
    if len(text) == 5 and ":" in text:
        return text
    return ""


def _is_open_status(status: str) -> bool:
    key = status.strip().lower()
    return key in {"", "open", "due", "incomplete", "pending"}


def _classify_action(action: str) -> QueueKind | None:
    key = action.strip().lower()
    if not key:
        return None
    if "follow" in key:
        return "follow_up"
    if (
        key == "call"
        or key.startswith("call ")
        or key.endswith(" call")
        or "call back" in key
        or "outreach call" in key
        or key.startswith("initial outreach")
    ):
        return "call"
    if key in CALL_ACTION_TYPES:
        return "call"
    if key.replace(" ", "-") in FOLLOW_UP_ACTION_TYPES:
        return "follow_up"
    return None


def _resolve_client_id(conn, client: str | None = None, client_id: int | None = None) -> int | None:
    """Read helper. Writes must call require_write_client_id instead."""
    if client_id is not None:
        row = conn.execute("SELECT id FROM clients WHERE id = ?", (client_id,)).fetchone()
        return int(row["id"]) if row else None
    if not client:
        return None
    name = client.strip()
    code = name.lower()
    if code in {"carmeco", "carmeco metal"}:
        code = "carmeco"
    row = conn.execute(
        "SELECT id FROM clients WHERE code = ? OR lower(name) = lower(?)",
        (code, name),
    ).fetchone()
    return int(row["id"]) if row else None


def _item(
    *,
    source: str,
    source_id: int | None,
    client_id: int,
    company_id: int,
    relationship_id: int,
    external_record_no: str,
    company_name: str,
    contact_id: int | None,
    action_type: str,
    due_date: str,
    due_time: str,
    assigned_user: str,
    assigned_user_id: int | None,
    completion_status: str,
    priority: str,
) -> WorkQueueItem:
    return WorkQueueItem(
        id=source_id,
        source=source,
        source_id=source_id,
        client_id=client_id,
        company_id=company_id,
        relationship_id=relationship_id,
        external_record_no=external_record_no,
        company_name=company_name,
        contact_id=contact_id,
        action_type=action_type,
        due_date=due_date,
        due_time=due_time,
        assigned_user=assigned_user,
        assigned_user_id=assigned_user_id,
        completion_status=completion_status,
        priority=priority,
    )


def _age_label(from_iso: str | None) -> str | None:
    if not from_iso:
        return None
    try:
        raw = from_iso.replace("T", " ")[:19]
        start = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            start = datetime.strptime(from_iso[:10], "%Y-%m-%d")
        except ValueError:
            return None
    delta = _now() - start
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def _priority_label(score: int) -> str:
    if score >= 90:
        return "Critical"
    if score >= 75:
        return "High"
    if score >= 55:
        return "Medium"
    return "Low"


def _compute_priority(
    *,
    work_type: str,
    is_overdue: bool,
    is_hot: bool,
    is_weblead: bool,
    is_cross_client: bool,
    opportunity_score: int | None,
    due_date: str | None,
    due_time: str,
) -> int:
    """
    Work Next ordering (high → low):
    Overdue call/follow-up → due-today call/follow-up → Appointment →
    high-value Cross-Client → Hot → Needs Next Action → New Assignment.
    """
    score = 30
    wt = work_type.lower()
    today = _today()
    due_today = bool(due_date and due_date == today)
    opp = int(opportunity_score or 0)

    if is_overdue or (due_date and due_date < today):
        score = 96
    elif wt == "call" and due_today:
        score = 92
    elif wt == "follow-up" and due_today:
        score = 90
    elif "appointment" in wt:
        score = 88
    elif (is_cross_client or "cross-client" in wt) and opp >= 75:
        score = 86
    elif is_hot or wt == "hot":
        score = 82
    elif is_weblead or wt == "weblead":
        score = 78
    elif wt == "call":
        score = 74
    elif wt == "follow-up":
        score = 70
    elif is_cross_client or "cross-client" in wt:
        score = 62 + min(18, opp // 5)
    elif wt == "needs next action":
        score = 58
    elif wt == "new assignment":
        score = 32
    elif wt == "research":
        score = 35
    else:
        score = 40

    if due_date:
        if due_date < today:
            score = max(score, 94)
        elif due_date == today and wt not in {"call", "follow-up"}:
            score += 3
            if due_time:
                score += 1
    if is_hot and wt not in {"hot"}:
        score += 2
    return max(0, min(100, score))


def list_due_work_items(
    *,
    kind: QueueKind,
    client: str = "Carmeco",
    client_id: int | None = None,
    include_completed: bool = False,
) -> list[WorkQueueItem]:
    """Open scheduled work due today or overdue for the given action kind."""
    if not _db_exists():
        return []
    today = _today()
    items: list[WorkQueueItem] = []

    with get_connection() as conn:
        focus_id = _resolve_client_id(conn, client=client, client_id=client_id)
        if focus_id is None:
            return []

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_queue_items'"
        ).fetchone():
            type_filter = (
                ("Call",)
                if kind == "call"
                else ("Follow-Up", "Follow Up", "Followup")
            )
            placeholders = ",".join("?" * len(type_filter))
            rows = conn.execute(
                f"""
                SELECT
                    w.*,
                    co.company_name,
                    co.external_record_no,
                    COALESCE(u.full_name, w.assigned_user, '') AS assignee_name
                FROM work_queue_items w
                JOIN companies co ON co.id = w.company_id
                LEFT JOIN users u ON u.id = w.assigned_user_id
                WHERE w.client_id = ?
                  AND lower(w.action_type) IN ({placeholders})
                  AND substr(w.due_date, 1, 10) <= ?
                  AND (
                    ? = 1
                    OR lower(COALESCE(w.completion_status, 'open')) IN ('open', 'due', 'incomplete', 'pending', '')
                  )
                ORDER BY w.due_date ASC, w.id ASC
                """,
                (
                    focus_id,
                    *[t.lower() for t in type_filter],
                    today,
                    1 if include_completed else 0,
                ),
            ).fetchall()
            for r in rows:
                due = _date_only(r["due_date"]) or today
                items.append(
                    _item(
                        source="work_queue",
                        source_id=int(r["id"]),
                        client_id=int(r["client_id"]),
                        company_id=int(r["company_id"]),
                        relationship_id=int(r["relationship_id"]),
                        external_record_no=_blank(r["external_record_no"]),
                        company_name=_blank(r["company_name"]),
                        contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
                        action_type=_blank(r["action_type"]) or ("Call" if kind == "call" else "Follow-Up"),
                        due_date=due,
                        due_time=_blank(r["due_time"]),
                        assigned_user=_blank(r["assignee_name"]),
                        assigned_user_id=(
                            int(r["assigned_user_id"]) if r["assigned_user_id"] is not None else None
                        ),
                        completion_status=_blank(r["completion_status"]) or "open",
                        priority=_blank(r["priority"]),
                    )
                )

        ccr_rows = conn.execute(
            f"""
            SELECT
                ccr.id AS relationship_id,
                ccr.client_id,
                ccr.company_id,
                ccr.assigned_user_id,
                COALESCE(ccr.next_action, '') AS next_action,
                ccr.follow_up_date,
                COALESCE(ccr.priority, '') AS priority,
                co.external_record_no,
                co.company_name,
                COALESCE(u.full_name, '') AS assignee_name
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            LEFT JOIN users u ON u.id = ccr.assigned_user_id
            WHERE ccr.client_id = ?
              AND {sql_active_ccr(conn)}
              AND {sql_active_company(conn)}
              AND ccr.follow_up_date IS NOT NULL
              AND TRIM(ccr.follow_up_date) != ''
              AND substr(ccr.follow_up_date, 1, 10) <= ?
              AND TRIM(COALESCE(ccr.next_action, '')) != ''
            """,
            (focus_id, today),
        ).fetchall()
        for r in ccr_rows:
            classified = _classify_action(_blank(r["next_action"]))
            if classified != kind:
                continue
            due = _date_only(r["follow_up_date"])
            if not due:
                continue
            items.append(
                _item(
                    source="ccr",
                    source_id=int(r["relationship_id"]),
                    client_id=int(r["client_id"]),
                    company_id=int(r["company_id"]),
                    relationship_id=int(r["relationship_id"]),
                    external_record_no=_blank(r["external_record_no"]),
                    company_name=_blank(r["company_name"]),
                    contact_id=None,
                    action_type=_blank(r["next_action"]),
                    due_date=due,
                    due_time="",
                    assigned_user=_blank(r["assignee_name"]),
                    assigned_user_id=(
                        int(r["assigned_user_id"]) if r["assigned_user_id"] is not None else None
                    ),
                    completion_status="open",
                    priority=_blank(r["priority"]),
                )
            )

        if kind == "follow_up":
            act_rows = conn.execute(
                """
                SELECT
                    a.activity_id,
                    a.client_id,
                    a.company_id,
                    a.relationship_id,
                    a.contact_id,
                    a.follow_up_at,
                    a.assigned_user,
                    a.user_id,
                    COALESCE(a.follow_up_completed, 0) AS follow_up_completed,
                    co.external_record_no,
                    co.company_name
                FROM activities a
                JOIN companies co ON co.id = a.company_id
                WHERE a.client_id = ?
                  AND lower(TRIM(a.activity_type)) = 'follow-up'
                  AND a.follow_up_at IS NOT NULL
                  AND TRIM(a.follow_up_at) != ''
                  AND substr(a.follow_up_at, 1, 10) <= ?
                  AND (? = 1 OR COALESCE(a.follow_up_completed, 0) = 0)
                  AND lower(COALESCE(a.completion_status, 'open')) NOT IN
                      ('completed', 'cancelled', 'done', 'closed')
                ORDER BY a.follow_up_at ASC, a.activity_id ASC
                """,
                (focus_id, today, 1 if include_completed else 0),
            ).fetchall()
            for r in act_rows:
                due = _date_only(r["follow_up_at"])
                if not due:
                    continue
                completed = int(r["follow_up_completed"] or 0) == 1
                items.append(
                    _item(
                        source="activity_follow_up",
                        source_id=int(r["activity_id"]),
                        client_id=int(r["client_id"]),
                        company_id=int(r["company_id"]),
                        relationship_id=int(r["relationship_id"]),
                        external_record_no=_blank(r["external_record_no"]),
                        company_name=_blank(r["company_name"]),
                        contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
                        action_type="Follow-Up",
                        due_date=due,
                        due_time=_time_only(r["follow_up_at"]),
                        assigned_user=_blank(r["assigned_user"]),
                        assigned_user_id=int(r["user_id"]) if r["user_id"] is not None else None,
                        completion_status="completed" if completed else "open",
                        priority="",
                    )
                )

    seen: set[int] = set()
    unique: list[WorkQueueItem] = []
    for item in items:
        if not include_completed and not _is_open_status(item.completion_status):
            continue
        if item.company_id in seen:
            continue
        seen.add(item.company_id)
        unique.append(item)
    unique.sort(key=lambda i: (i.due_date, i.company_name.lower()))
    return unique


def due_record_nos(
    *,
    kind: QueueKind,
    client: str = "Carmeco",
    client_id: int | None = None,
) -> set[str]:
    return {
        item.external_record_no
        for item in list_due_work_items(kind=kind, client=client, client_id=client_id)
    }


def _follow_up_bucket(due_date: str | None, due_time: str) -> str:
    due = _date_only(due_date) or ""
    today = _today()
    if due and due < today:
        return "overdue"
    if due and due > today:
        return "upcoming"
    if due == today and _is_overdue(due, due_time):
        return "overdue"
    if due == today:
        return "due_today"
    return "upcoming"


def list_open_follow_up_tasks(*, client_id: int) -> list[WorkQueueItem]:
    """Open follow-up task records for one client (Work Queue / Tasks / Dashboard)."""
    if not _db_exists() or client_id <= 0:
        return []
    items: list[WorkQueueItem] = []
    seen_activity_ids: set[int] = set()
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_queue_items'"
        ).fetchone():
            rows = conn.execute(
                """
                SELECT
                    w.*,
                    co.company_name,
                    co.external_record_no,
                    COALESCE(u.full_name, w.assigned_user, '') AS assignee_name
                FROM work_queue_items w
                JOIN companies co ON co.id = w.company_id
                LEFT JOIN users u ON u.id = w.assigned_user_id
                WHERE w.client_id = ?
                  AND lower(w.action_type) IN ('follow-up', 'follow up', 'followup')
                  AND lower(COALESCE(w.completion_status, 'open')) IN
                      ('open', 'due', 'incomplete', 'pending', '')
                ORDER BY w.due_date ASC, w.due_time ASC, w.id ASC
                """,
                (int(client_id),),
            ).fetchall()
            for r in rows:
                due = _date_only(r["due_date"])
                if not due:
                    continue
                queue_id = int(r["id"])
                source_activity = r["source_activity_id"]
                if source_activity is not None:
                    seen_activity_ids.add(int(source_activity))
                items.append(
                    _item(
                        source="work_queue",
                        source_id=queue_id,
                        client_id=int(r["client_id"]),
                        company_id=int(r["company_id"]),
                        relationship_id=int(r["relationship_id"]),
                        external_record_no=_blank(r["external_record_no"]),
                        company_name=_blank(r["company_name"]),
                        contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
                        action_type=_blank(r["action_type"]) or "Follow-Up",
                        due_date=due,
                        due_time=_blank(r["due_time"])[:5],
                        assigned_user=_blank(r["assignee_name"]),
                        assigned_user_id=(
                            int(r["assigned_user_id"]) if r["assigned_user_id"] is not None else None
                        ),
                        completion_status=_blank(r["completion_status"]) or "open",
                        priority=_blank(r["priority"]),
                    )
                )
        act_rows = conn.execute(
            """
            SELECT
                a.activity_id,
                a.client_id,
                a.company_id,
                a.relationship_id,
                a.contact_id,
                a.follow_up_at,
                a.assigned_user,
                a.user_id,
                co.external_record_no,
                co.company_name
            FROM activities a
            JOIN companies co ON co.id = a.company_id
            WHERE a.client_id = ?
              AND lower(TRIM(a.activity_type)) = 'follow-up'
              AND a.follow_up_at IS NOT NULL
              AND TRIM(a.follow_up_at) != ''
              AND COALESCE(a.follow_up_completed, 0) = 0
              AND lower(COALESCE(a.completion_status, 'open')) NOT IN
                  ('completed', 'cancelled', 'done', 'closed')
            ORDER BY a.follow_up_at ASC, a.activity_id ASC
            """,
            (int(client_id),),
        ).fetchall()
        for r in act_rows:
            activity_id = int(r["activity_id"])
            if activity_id in seen_activity_ids:
                continue
            due = _date_only(r["follow_up_at"])
            if not due:
                continue
            seen_activity_ids.add(activity_id)
            items.append(
                _item(
                    source="activity_follow_up",
                    source_id=activity_id,
                    client_id=int(r["client_id"]),
                    company_id=int(r["company_id"]),
                    relationship_id=int(r["relationship_id"]),
                    external_record_no=_blank(r["external_record_no"]),
                    company_name=_blank(r["company_name"]),
                    contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
                    action_type="Follow-Up",
                    due_date=due,
                    due_time=_time_only(r["follow_up_at"]),
                    assigned_user=_blank(r["assigned_user"]),
                    assigned_user_id=int(r["user_id"]) if r["user_id"] is not None else None,
                    completion_status="open",
                    priority="",
                )
            )
    items.sort(key=lambda i: (i.due_date, i.due_time or "99:99", i.company_name.lower()))
    return items


def list_dashboard_follow_ups(*, client_id: int) -> DashboardFollowUpsResponse:
    """Active Client follow-ups from the same open task records as Work Queue."""
    overdue: list[DashboardFollowUpItem] = []
    due_today: list[DashboardFollowUpItem] = []
    upcoming: list[DashboardFollowUpItem] = []
    if client_id <= 0:
        return DashboardFollowUpsResponse(client_id=client_id)

    with get_connection() as conn:
        for item in list_open_follow_up_tasks(client_id=client_id):
            resolved_contact_id, contact_name = _contact_name(
                conn, item.company_id, item.contact_id
            )
            bucket = _follow_up_bucket(item.due_date, item.due_time)
            next_action = ""
            status = ""
            if resolved_contact_id:
                wf = conn.execute(
                    """
                    SELECT next_action FROM contact_client_workflows
                    WHERE contact_id = ? AND client_id = ?
                    """,
                    (int(resolved_contact_id), int(client_id)),
                ).fetchone()
                if wf is not None:
                    next_action = _blank(wf["next_action"])
            ccr = conn.execute(
                """
                SELECT next_action, status
                FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (int(client_id), int(item.company_id)),
            ).fetchone()
            if ccr is not None:
                status = _blank(ccr["status"])
                if not next_action:
                    next_action = _blank(ccr["next_action"])
            row = DashboardFollowUpItem(
                client_id=item.client_id,
                company_id=item.company_id,
                contact_id=resolved_contact_id,
                contact_name=contact_name,
                company_name=item.company_name,
                external_record_no=item.external_record_no,
                due_date=item.due_date,
                due_time=item.due_time,
                assigned_user=item.assigned_user,
                assigned_user_id=item.assigned_user_id,
                status=status,
                source=item.source,
                source_id=item.source_id,
                bucket=bucket,
                next_action=next_action,
            )
            if bucket == "overdue":
                overdue.append(row)
            elif bucket == "due_today":
                due_today.append(row)
            else:
                upcoming.append(row)

    return DashboardFollowUpsResponse(
        client_id=client_id,
        overdue=overdue,
        due_today=due_today,
        upcoming=upcoming,
        overdue_count=len(overdue),
        due_today_count=len(due_today),
        upcoming_count=len(upcoming),
    )


def work_queue_summary(*, client: str = "Carmeco", client_id: int | None = None) -> WorkQueueSummary:
    from access import user_can_access_client

    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    calls = list_due_work_items(kind="call", client=client, client_id=client_id)
    follow_ups = list_due_work_items(kind="follow_up", client=client, client_id=client_id)
    new_count = 0
    if _db_exists():
        with get_connection() as conn:
            focus_id = _resolve_client_id(conn, client=client, client_id=client_id)
            if focus_id is not None:
                if not user_can_access_client(user.id, int(focus_id)) and not user.is_administrator:
                    raise PermissionError("Not authorized for this client.")
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM client_company_relationships
                    WHERE client_id = ?
                      AND lower(TRIM(COALESCE(status, ''))) = 'new'
                    """,
                    (focus_id,),
                ).fetchone()
                new_count = int(row["n"]) if row else 0
    return WorkQueueSummary(
        client=client,
        calls_due_today=len(calls),
        follow_ups_due=len(follow_ups),
        new_assignments=new_count,
        calls=calls,
        follow_ups=follow_ups,
    )


def _contact_name(conn, company_id: int, contact_id: int | None = None) -> tuple[int | None, str]:
    """Return a named contact at this company, or (None, "").

    Open Contact requires a real person at this company (non-blank first or last
    name). Nameless stubs, deleted ids, and contacts on another company are not
    exposed. A missing contact_id recovers the first named contact at the
    company when one exists (so Tasks can still Open Contact for people like
    James Logsdon).
    """

    def _name(row) -> str:
        return f"{_blank(row['first_name'])} {_blank(row['last_name'])}".strip()

    def _is_named(row) -> bool:
        return bool(_blank(row["first_name"]) or _blank(row["last_name"]))

    if contact_id is not None:
        row = conn.execute(
            "SELECT id, company_id, first_name, last_name FROM contacts WHERE id = ?",
            (int(contact_id),),
        ).fetchone()
        if (
            row is not None
            and _is_named(row)
        ):
            row_company = int(row["company_id"]) if row["company_id"] is not None else None
            if row_company is None or row_company == int(company_id):
                return int(row["id"]), _name(row)
        return None, ""
    row = conn.execute(
        """
        SELECT id, first_name, last_name FROM contacts
        WHERE company_id = ?
          AND (
                TRIM(COALESCE(first_name, '')) != ''
             OR TRIM(COALESCE(last_name, '')) != ''
          )
        ORDER BY source_row_index ASC, id ASC
        LIMIT 1
        """,
        (int(company_id),),
    ).fetchone()
    if row is None:
        return None, ""
    return int(row["id"]), _name(row)


def _last_activity(conn, client_id: int, company_id: int) -> tuple[str | None, str]:
    row = conn.execute(
        """
        SELECT activity_at, activity_type, outcome, notes
        FROM activities
        WHERE client_id = ? AND company_id = ?
        ORDER BY activity_at DESC, activity_id DESC
        LIMIT 1
        """,
        (client_id, company_id),
    ).fetchone()
    if row is None:
        return None, ""
    summary = _blank(row["activity_type"])
    outcome = _blank(row["outcome"])
    notes = _blank(row["notes"])
    if outcome:
        summary = f"{summary}: {outcome}"
    elif notes:
        summary = f"{summary}: {notes[:80]}"
    return _blank(row["activity_at"]) or None, summary


def _is_overdue(due_date: str | None, due_time: str) -> bool:
    if not due_date:
        return False
    today = _today()
    if due_date < today:
        return True
    if due_date > today:
        return False
    if not due_time:
        return False
    now_hm = _now().strftime("%H:%M")
    return due_time < now_hm


WORK_QUEUE_DEFAULT_LIMIT = 50
WORK_QUEUE_MAX_LIMIT = 100
_CCR_KEY_CHUNK = 400


def _work_queue_sort_key(row: WorkQueueRow) -> tuple:
    """Deterministic Work Queue order (priority desc, due, name, ids)."""
    return (
        -int(row.work_priority or 0),
        row.due_date or "9999-99-99",
        row.due_time or "99:99",
        (row.company_name or "").lower(),
        int(row.company_id or 0),
        row.queue_item_id or "",
    )


def _cursor_sort_key(
    *,
    work_priority: int,
    due_date: str | None,
    due_time: str,
    company_name: str,
    company_id: int,
    queue_item_id: str,
) -> tuple:
    return (
        -int(work_priority or 0),
        due_date or "9999-99-99",
        due_time or "99:99",
        (company_name or "").lower(),
        int(company_id or 0),
        queue_item_id or "",
    )

_CCR_CONTEXT_SELECT = """
    SELECT
        ccr.id AS relationship_id,
        ccr.client_id,
        cl.code AS client_code,
        cl.name AS client_name,
        ccr.company_id,
        COALESCE(
            NULLIF(TRIM(ccr.external_record_no), ''),
            co.external_record_no
        ) AS external_record_no,
        co.company_name,
        co.city,
        co.state,
        COALESCE(ccr.status, '') AS status,
        COALESCE(ccr.next_action, '') AS next_action,
        ccr.follow_up_date,
        COALESCE(ccr.priority, '') AS ccr_priority,
        ccr.assigned_user_id,
        COALESCE(u.full_name, '') AS assignee_name,
        COALESCE(ccr.is_hot, 0) AS is_hot,
        COALESCE(ccr.notes, '') AS notes
    FROM client_company_relationships ccr
    JOIN clients cl ON cl.id = ccr.client_id
    JOIN companies co ON co.id = ccr.company_id
    LEFT JOIN users u ON u.id = ccr.assigned_user_id
"""


def _normalize_work_queue_page(
    limit: int | None = None,
    offset: int | None = None,
) -> tuple[int, int]:
    """Bounded paging. Rejects invalid / negative values with ValueError."""
    if limit is None:
        limit_i = WORK_QUEUE_DEFAULT_LIMIT
    else:
        try:
            limit_i = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
    if offset is None:
        offset_i = 0
    else:
        try:
            offset_i = int(offset)
        except (TypeError, ValueError) as exc:
            raise ValueError("offset must be an integer") from exc
    if limit_i < 1 or limit_i > WORK_QUEUE_MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {WORK_QUEUE_MAX_LIMIT}")
    if offset_i < 0:
        raise ValueError("offset must be >= 0")
    return limit_i, offset_i


def _load_ccr_context_for_keys(conn, keys: set[tuple[int, int]]) -> dict[tuple[int, int], object]:
    """Hydrate CCR + company + client context only for candidate (client_id, company_id) keys."""
    out: dict[tuple[int, int], object] = {}
    if not keys:
        return out
    key_list = list(keys)
    for i in range(0, len(key_list), _CCR_KEY_CHUNK):
        chunk = key_list[i : i + _CCR_KEY_CHUNK]
        clauses = " OR ".join(
            ["(ccr.client_id = ? AND ccr.company_id = ?)"] * len(chunk)
        )
        params: list[object] = []
        for cid, company_id in chunk:
            params.extend([int(cid), int(company_id)])
        for r in conn.execute(
            f"{_CCR_CONTEXT_SELECT} WHERE {clauses}",
            params,
        ):
            out[(int(r["client_id"]), int(r["company_id"]))] = r
    return out


def _ccr_keys_matching(
    conn,
    client_ids: list[int],
    *,
    status_sql: str,
    status_params: tuple[object, ...] = (),
) -> set[tuple[int, int]]:
    """Status-scoped CCR candidate keys — never an unqualified client book scan."""
    if not client_ids:
        return set()
    placeholders = ",".join("?" * len(client_ids))
    rows = conn.execute(
        f"""
        SELECT ccr.client_id, ccr.company_id
        FROM client_company_relationships ccr
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.client_id IN ({placeholders})
          AND {sql_active_ccr(conn)}
          AND {sql_active_company(conn)}
          AND ({status_sql})
        """,
        [*client_ids, *status_params],
    ).fetchall()
    return {(int(r["client_id"]), int(r["company_id"])) for r in rows}


def list_work_queue(
    user_id: int | None = None,
    *,
    client_id: int | None = None,
    work_type: str | None = None,
    status: str | None = None,
    due: str | None = None,
    priority: str | None = None,
    hot: bool = False,
    weblead: bool = False,
    cross_client: bool = False,
    overdue_only: bool = False,
    assigned_user_id: int | None = None,
    q: str | None = None,
    ai_alignment: str | None = None,
    ai_recommendation: str | None = None,
    ai_fit: str | None = None,
    ai_engagement: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    enrich_contacts: bool = True,
    apply_insight_overlay: bool = True,
    page_results: bool = True,
) -> WorkQueueListResponse:
    """Full specialist work queue across authorized clients.

    Builds candidates from source queries (due work, appointments, webleads,
    cross-client, status-scoped Hot/New/Needs Next Action/Appointment), then
    hydrates CCR context only for those (client_id, company_id) keys. Applies
    filters before pagination.

    Internal flags (navigation / next-item):
    - enrich_contacts: load contact + last-activity per row (expensive).
    - apply_insight_overlay: attach NorthStar insight + AI filters.
    - page_results: when False, return the full ranked filtered set (no LIMIT);
      used only by get_work_queue_next — not exposed on the public list API.
    """
    if page_results:
        page_limit, page_offset = _normalize_work_queue_page(limit, offset)
    else:
        page_limit, page_offset = 0, 0
    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")

    empty = WorkQueueListResponse(
        user_id=user.id,
        mode="selected_client" if client_id is not None else "all_my_clients",
        client_ids=[],
        count=0,
        total=0,
        offset=page_offset,
        limit=page_limit if page_results else 0,
        has_previous=False,
        has_next=False,
        summary=WorkQueueSummaryV2(),
        items=[],
    )

    client_ids = resolve_dashboard_client_ids(user.id, selected_client_id=client_id)
    if not client_ids:
        return empty.model_copy(update={"user_id": user.id})

    today = _today()
    rows_out: list[WorkQueueRow] = []

    with get_connection() as conn:
        placeholders = ",".join("?" * len(client_ids))
        candidate_keys: set[tuple[int, int]] = set()

        # --- Source payloads (keys first; CCR hydrate later) ---
        due_calls: list = []
        due_follow_ups: list = []
        for cid in client_ids:
            for item in list_due_work_items(kind="call", client_id=cid):
                due_calls.append(item)
                candidate_keys.add((item.client_id, item.company_id))
            for item in list_open_follow_up_tasks(client_id=cid):
                due_follow_ups.append(item)
                candidate_keys.add((item.client_id, item.company_id))

        appointment_rows: list = []
        scheduled_companies: set[tuple[int, int]] = set()
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appointments'"
        ).fetchone():
            appointment_rows = list(
                conn.execute(
                    f"""
                    SELECT *
                    FROM appointments
                    WHERE client_id IN ({placeholders})
                      AND lower(status) = 'scheduled'
                    """,
                    client_ids,
                )
            )
            for r in appointment_rows:
                key = (int(r["client_id"]), int(r["company_id"]))
                scheduled_companies.add(key)
                candidate_keys.add(key)

        activity_appointment_rows = list(
            conn.execute(
                f"""
                SELECT a.*, co.company_name, co.external_record_no
                FROM activities a
                JOIN companies co ON co.id = a.company_id
                WHERE a.client_id IN ({placeholders})
                  AND lower(TRIM(a.activity_type)) = 'appointment'
                  AND substr(a.activity_at, 1, 10) <= ?
                  AND lower(COALESCE(a.completion_status, 'open')) IN
                      ('open', 'due', 'incomplete', 'pending', '')
                """,
                [*client_ids, today],
            )
        )
        for r in activity_appointment_rows:
            candidate_keys.add((int(r["client_id"]), int(r["company_id"])))

        # Status-scoped CCR candidates (not the full client book)
        appt_status_keys = _ccr_keys_matching(
            conn,
            client_ids,
            status_sql=(
                "lower(ccr.status) LIKE '%appointment%' "
                "OR lower(trim(ccr.status)) LIKE 'appt set%'"
            ),
        )
        candidate_keys |= appt_status_keys

        hot_keys = _ccr_keys_matching(
            conn,
            client_ids,
            status_sql="lower(trim(ccr.status)) = 'hot prospect'",
        )
        candidate_keys |= hot_keys

        new_keys = _ccr_keys_matching(
            conn,
            client_ids,
            status_sql="lower(trim(ccr.status)) = 'new'",
        )
        candidate_keys |= new_keys

        needs_keys = _ccr_keys_matching(
            conn,
            client_ids,
            status_sql=(
                "lower(trim(ccr.status)) IN "
                "('left message', 'contacted', 'send information')"
            ),
        )
        candidate_keys |= needs_keys

        # Milestone flags
        weblead_dates: dict[tuple[int, int], str] = {}
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_milestones'"
        ).fetchone():
            for r in conn.execute(
                f"""
                SELECT client_id, company_id, MIN(milestone_date) AS first_date
                FROM revenue_milestones
                WHERE client_id IN ({placeholders})
                  AND milestone_type = 'WebLead'
                GROUP BY client_id, company_id
                """,
                client_ids,
            ):
                key = (int(r["client_id"]), int(r["company_id"]))
                weblead_dates[key] = _blank(r["first_date"])
                candidate_keys.add(key)

        # Cross-client assignments
        cross_meta: dict[tuple[int, int], tuple[int, str]] = {}
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
        ).fetchone():
            oa_cols = {
                str(c["name"])
                for c in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
            }
            score_expr = (
                "COALESCE(oa.opportunity_score, 0)"
                if "opportunity_score" in oa_cols
                else "0"
            )
            source_expr = (
                "COALESCE(oa.source_summary, '')"
                if "source_summary" in oa_cols
                else "''"
            )
            for r in conn.execute(
                f"""
                SELECT oa.target_client_id, oa.company_id,
                       {score_expr} AS opportunity_score,
                       {source_expr} AS source_summary
                FROM opportunity_assignments oa
                WHERE oa.target_client_id IN ({placeholders})
                """,
                client_ids,
            ):
                key = (int(r["target_client_id"]), int(r["company_id"]))
                cross_meta[key] = (
                    int(r["opportunity_score"] or 0),
                    _blank(r["source_summary"]),
                )
                candidate_keys.add(key)

        ccr_by_key = _load_ccr_context_for_keys(conn, candidate_keys)

        def add_row(
            *,
            work_type_name: str,
            source: str,
            source_id: int | None,
            ccr,
            due_date: str | None,
            due_time: str,
            next_action: str,
            why: str,
            contact_id: int | None = None,
            opportunity_score: int | None = None,
            force_weblead: bool = False,
            force_cross: bool = False,
            source_client_summary: str = "",
        ) -> None:
            cid = int(ccr["client_id"])
            company_id = int(ccr["company_id"])
            if enrich_contacts:
                ct_id, ct_name = _contact_name(conn, company_id, contact_id)
                last_at, last_summary = _last_activity(conn, cid, company_id)
            else:
                ct_id = contact_id
                ct_name = ""
                last_at, last_summary = None, ""
            is_hot = is_hot_prospect_status(_blank(ccr["status"]))
            wl_date = weblead_dates.get((cid, company_id))
            is_weblead = force_weblead or wl_date is not None
            is_cross = force_cross or (cid, company_id) in cross_meta
            overdue = _is_overdue(due_date, due_time)
            score = _compute_priority(
                work_type=work_type_name,
                is_overdue=overdue,
                is_hot=is_hot,
                is_weblead=is_weblead,
                is_cross_client=is_cross,
                opportunity_score=opportunity_score,
                due_date=due_date,
                due_time=due_time,
            )
            age = _age_label(wl_date) if is_weblead else None
            is_new_weblead = bool(is_weblead and work_type_name == "WebLead")
            queue_item_id = (
                f"{work_type_name.lower().replace(' ', '-')}:{source}:"
                f"{source_id or company_id}:{cid}"
            )
            source_summary = source_client_summary
            if not source_summary and is_cross:
                source_summary = cross_meta.get((cid, company_id), (0, ""))[1]
            rows_out.append(
                WorkQueueRow(
                    queue_item_id=queue_item_id,
                    work_type=work_type_name,
                    work_priority=score,
                    priority_label=_priority_label(score),
                    client_id=cid,
                    client_code=_blank(ccr["client_code"]),
                    client_name=_blank(ccr["client_name"]),
                    company_id=company_id,
                    relationship_id=int(ccr["relationship_id"]),
                    external_record_no=_blank(ccr["external_record_no"]),
                    company_name=_blank(ccr["company_name"]),
                    city=_blank(ccr["city"]),
                    state=_blank(ccr["state"]),
                    contact_id=ct_id,
                    contact_name=ct_name,
                    status=_blank(ccr["status"]),
                    due_date=due_date,
                    due_time=due_time,
                    is_overdue=overdue,
                    last_activity_at=last_at,
                    last_activity_summary=last_summary,
                    next_action=next_action or _blank(ccr["next_action"]) or work_type_name,
                    why_in_queue=why,
                    is_hot=is_hot,
                    is_weblead=is_weblead,
                    weblead_age_label=age,
                    is_new_weblead=is_new_weblead,
                    is_cross_client=is_cross,
                    opportunity_score=opportunity_score,
                    source_client_summary=source_summary,
                    assigned_user=_blank(ccr["assignee_name"]),
                    assigned_user_id=(
                        int(ccr["assigned_user_id"])
                        if ccr["assigned_user_id"] is not None
                        else None
                    ),
                    source=source,
                    source_id=source_id,
                    completion_status="open",
                )
            )

        for item in due_calls:
            ccr = ccr_by_key.get((item.client_id, item.company_id))
            if ccr is None:
                continue
            why = (
                f"Call due {item.due_date}"
                + (f" at {item.due_time}" if item.due_time else "")
            )
            if item.due_date < today:
                why = f"Overdue call ({item.due_date})"
            add_row(
                work_type_name="Call",
                source=item.source,
                source_id=item.source_id,
                ccr=ccr,
                due_date=item.due_date,
                due_time=item.due_time,
                next_action=item.action_type or "Call",
                why=why,
                contact_id=item.contact_id,
            )

        for item in due_follow_ups:
            ccr = ccr_by_key.get((item.client_id, item.company_id))
            if ccr is None:
                continue
            why = (
                f"Follow-Up due {item.due_date}"
                + (f" at {item.due_time}" if item.due_time else "")
            )
            if item.due_date < today:
                why = f"Overdue follow-up ({item.due_date})"
            elif item.due_date > today:
                why = (
                    f"Upcoming follow-up {item.due_date}"
                    + (f" at {item.due_time}" if item.due_time else "")
                )
            add_row(
                work_type_name="Follow-Up",
                source=item.source,
                source_id=item.source_id,
                ccr=ccr,
                due_date=item.due_date,
                due_time=item.due_time,
                next_action=item.action_type or "Follow-Up",
                why=why,
                contact_id=item.contact_id,
            )

        for r in appointment_rows:
            ccr = ccr_by_key.get((int(r["client_id"]), int(r["company_id"])))
            if ccr is None:
                continue
            due = _blank(r["appointment_date"])[:10] or today
            add_row(
                work_type_name="Appointment",
                source="appointment",
                source_id=int(r["id"]),
                ccr=ccr,
                due_date=due,
                due_time=_blank(r["start_time"])[:5],
                next_action="Appointment",
                why=(
                    "Appointment date/time TBD"
                    if int(r["datetime_tbd"] or 0)
                    else f"Scheduled appointment on {due}"
                ),
                contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
            )

        for key in appt_status_keys:
            if key in scheduled_companies:
                continue
            ccr = ccr_by_key.get(key)
            if ccr is None:
                continue
            status_l = _blank(ccr["status"]).lower()
            if "appointment" not in status_l and not status_l.startswith("appt set"):
                continue
            add_row(
                work_type_name="Appointment",
                source="status",
                source_id=int(ccr["relationship_id"]),
                ccr=ccr,
                due_date=today,
                due_time="",
                next_action="Confirm appointment",
                why="Appointment Set status requires attention",
            )

        for r in activity_appointment_rows:
            ccr = ccr_by_key.get((int(r["client_id"]), int(r["company_id"])))
            if ccr is None:
                continue
            due = _date_only(r["activity_at"]) or today
            add_row(
                work_type_name="Appointment",
                source="activity_appointment",
                source_id=int(r["activity_id"]),
                ccr=ccr,
                due_date=due,
                due_time=_time_only(r["activity_at"]),
                next_action="Appointment",
                why=f"Scheduled appointment on {due}",
                contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
            )

        for key in hot_keys:
            ccr = ccr_by_key.get(key)
            if ccr is None or not is_hot_prospect_status(_blank(ccr["status"])):
                continue
            add_row(
                work_type_name="Hot",
                source="hot",
                source_id=int(ccr["relationship_id"]),
                ccr=ccr,
                due_date=_date_only(ccr["follow_up_date"]),
                due_time="",
                next_action=_blank(ccr["next_action"]) or "Work hot prospect",
                why="Current status is Hot Prospect",
            )

        for (cid, company_id), first_date in weblead_dates.items():
            ccr = ccr_by_key.get((cid, company_id))
            if ccr is None:
                continue
            add_row(
                work_type_name="WebLead",
                source="weblead",
                source_id=int(ccr["relationship_id"]),
                ccr=ccr,
                due_date=today,
                due_time="",
                next_action="Respond to WebLead",
                why=f"NEW WEBLEAD · {_age_label(first_date) or first_date}",
                force_weblead=True,
            )

        for key in new_keys:
            ccr = ccr_by_key.get(key)
            if ccr is None or _blank(ccr["status"]).lower() != "new":
                continue
            add_row(
                work_type_name="New Assignment",
                source="new",
                source_id=int(ccr["relationship_id"]),
                ccr=ccr,
                due_date=None,
                due_time="",
                next_action="Start working / initial outreach",
                why="Newly assigned prospect (Status = New)",
            )

        for (cid, company_id), (score, source_summary) in cross_meta.items():
            ccr = ccr_by_key.get((cid, company_id))
            if ccr is None:
                continue
            why = "Added from Cross-Client Opportunities"
            if source_summary:
                why = f"Source intelligence: {source_summary}"
            add_row(
                work_type_name="Cross-Client Opportunity",
                source="cross_client",
                source_id=int(ccr["relationship_id"]),
                ccr=ccr,
                due_date=None,
                due_time="",
                next_action=_blank(ccr["next_action"]) or "Review cross-client opportunity",
                why=why,
                opportunity_score=score or None,
                force_cross=True,
                source_client_summary=source_summary,
            )

        scheduled_keys = {
            (r.client_id, r.company_id)
            for r in rows_out
            if r.work_type in {"Call", "Follow-Up"}
        }
        for key in needs_keys:
            ccr = ccr_by_key.get(key)
            if ccr is None:
                continue
            rel_status = _blank(ccr["status"])
            if _is_terminal_relationship_status(rel_status):
                continue
            if not _needs_next_action_status(rel_status):
                continue
            if key in scheduled_keys:
                continue
            if _date_only(ccr["follow_up_date"]):
                continue
            add_row(
                work_type_name="Needs Next Action",
                source="needs_next_action",
                source_id=int(ccr["relationship_id"]),
                ccr=ccr,
                due_date=None,
                due_time="",
                next_action=_blank(ccr["next_action"]) or "Choose next action",
                why=f"Status = {rel_status} with no open scheduled next action",
            )

    # Filters (applied before pagination)
    filtered = rows_out
    if work_type:
        wt = work_type.strip().lower().replace("_", "-")
        aliases = {
            "call": "call",
            "follow-up": "follow-up",
            "followup": "follow-up",
            "appointment": "appointment",
            "weblead": "weblead",
            "hot": "hot",
            "new": "new assignment",
            "new-assignment": "new assignment",
            "needs-next-action": "needs next action",
            "needs_next_action": "needs next action",
            "research": "research",
            "other": "other",
            "cross-client": "cross-client opportunity",
            "cross-client-opportunity": "cross-client opportunity",
            "work-next": "__work_next__",
        }
        target = aliases.get(wt, wt)
        if target == "__work_next__":
            actionable = [r for r in filtered if r.work_type != "New Assignment"]
            filtered = actionable if actionable else filtered
        else:
            filtered = [
                r
                for r in filtered
                if r.work_type.lower() == target
                and (target != "hot" or is_hot_prospect_status(r.status))
            ]

    if status:
        filtered = [
            r for r in filtered if r.status.lower() == status.strip().lower()
        ]

    if due:
        due_key = due.strip().lower()
        if due_key == "today":
            filtered = [r for r in filtered if r.due_date and r.due_date <= today]
        elif due_key == "overdue":
            filtered = [r for r in filtered if r.is_overdue]
        elif due_key == "upcoming":
            filtered = [r for r in filtered if r.due_date and r.due_date > today]

    if overdue_only or (due and due.strip().lower() == "overdue"):
        filtered = [r for r in filtered if r.is_overdue]

    if hot:
        filtered = [
            r
            for r in filtered
            if r.work_type == "Hot" and is_hot_prospect_status(r.status)
        ]

    if weblead:
        filtered = [r for r in filtered if r.is_weblead or r.work_type == "WebLead"]

    if cross_client:
        filtered = [r for r in filtered if r.is_cross_client]

    if assigned_user_id is not None:
        filtered = [r for r in filtered if r.assigned_user_id == assigned_user_id]

    if priority:
        pl = priority.strip().lower()
        filtered = [r for r in filtered if r.priority_label.lower() == pl]

    if q:
        needle = q.strip().lower()
        if needle:
            filtered = [
                r
                for r in filtered
                if needle in r.company_name.lower()
                or needle in r.contact_name.lower()
                or needle in r.last_activity_summary.lower()
                or needle in r.why_in_queue.lower()
                or needle in r.next_action.lower()
                or needle in r.external_record_no.lower()
            ]

    seen_ids: set[str] = set()
    unique_rows: list[WorkQueueRow] = []
    for r in filtered:
        if r.queue_item_id in seen_ids:
            continue
        seen_ids.add(r.queue_item_id)
        unique_rows.append(r)

    unique_rows.sort(key=_work_queue_sort_key)

    from work_queue_intel_overlay import (
        attach_northstar_insight_overlay,
        filter_rows_by_insight,
    )

    insight_summary = WorkQueueInsightSummary()
    if apply_insight_overlay:
        unique_rows, insight_summary = attach_northstar_insight_overlay(unique_rows)
        unique_rows = filter_rows_by_insight(
            unique_rows,
            ai_alignment=ai_alignment,
            ai_recommendation=ai_recommendation,
            ai_fit=ai_fit,
            ai_engagement=ai_engagement,
        )
    elif any(
        _blank(x)
        for x in (ai_alignment, ai_recommendation, ai_fit, ai_engagement)
    ):
        # AI filters require overlay; apply when requested even if overlay flag was off.
        unique_rows, insight_summary = attach_northstar_insight_overlay(unique_rows)
        unique_rows = filter_rows_by_insight(
            unique_rows,
            ai_alignment=ai_alignment,
            ai_recommendation=ai_recommendation,
            ai_fit=ai_fit,
            ai_engagement=ai_engagement,
        )

    total = len(unique_rows)
    if page_results:
        page_items = unique_rows[page_offset : page_offset + page_limit]
    else:
        page_items = unique_rows
        page_limit = total
        page_offset = 0

    all_for_summary = rows_out
    summary = WorkQueueSummaryV2(
        calls_due=len({(r.client_id, r.company_id) for r in all_for_summary if r.work_type == "Call"}),
        follow_ups_due=len(
            {(r.client_id, r.company_id) for r in all_for_summary if r.work_type == "Follow-Up"}
        ),
        appointments=len(
            {(r.client_id, r.company_id) for r in all_for_summary if r.work_type == "Appointment"}
        ),
        hot=count_hot_prospects(client_ids),
        webleads=len(
            {(r.client_id, r.company_id) for r in all_for_summary if r.work_type == "WebLead"}
        ),
        new_assignments=len(
            {(r.client_id, r.company_id) for r in all_for_summary if r.work_type == "New Assignment"}
        ),
        needs_next_action=len(
            {
                (r.client_id, r.company_id)
                for r in all_for_summary
                if r.work_type == "Needs Next Action"
            }
        ),
        cross_client_opportunities=len(
            {
                (r.client_id, r.company_id)
                for r in all_for_summary
                if r.work_type == "Cross-Client Opportunity"
            }
        ),
        overdue=len({(r.client_id, r.company_id) for r in all_for_summary if r.is_overdue}),
    )

    return WorkQueueListResponse(
        user_id=user.id,
        mode="selected_client" if client_id is not None else "all_my_clients",
        client_ids=client_ids,
        count=total,
        total=total,
        offset=page_offset,
        limit=page_limit if page_results else total,
        has_previous=page_results and page_offset > 0,
        has_next=page_results and (page_offset + page_limit) < total,
        summary=summary,
        items=page_items,
        northstar_insight_summary=insight_summary,
    )


def _enrich_work_queue_row(row: WorkQueueRow) -> WorkQueueRow:
    """Fill contact + last-activity for a single navigation target row."""
    with get_connection() as conn:
        ct_id, ct_name = _contact_name(conn, row.company_id, row.contact_id)
        last_at, last_summary = _last_activity(conn, row.client_id, row.company_id)
    return row.model_copy(
        update={
            "contact_id": ct_id,
            "contact_name": ct_name,
            "last_activity_at": last_at,
            "last_activity_summary": last_summary,
        }
    )


def get_work_queue_next(
    user_id: int | None = None,
    *,
    after_queue_item_id: str,
    after_work_priority: int | None = None,
    after_due_date: str | None = None,
    after_due_time: str | None = None,
    after_company_name: str | None = None,
    after_company_id: int | None = None,
    client_id: int | None = None,
    work_type: str | None = None,
    status: str | None = None,
    due: str | None = None,
    priority: str | None = None,
    hot: bool = False,
    weblead: bool = False,
    cross_client: bool = False,
    overdue_only: bool = False,
    assigned_user_id: int | None = None,
    q: str | None = None,
    ai_alignment: str | None = None,
    ai_recommendation: str | None = None,
    ai_fit: str | None = None,
    ai_engagement: str | None = None,
) -> WorkQueueNextResponse:
    """Return the next eligible Work Queue item after ``after_queue_item_id``.

    Uses the same filters and deterministic order as ``list_work_queue``. Ranking
    skips per-row contact/activity hydration unless search ``q`` is set (search
    can match those fields). Only the chosen next row is enriched + insight-
    overlaid for the response payload.
    """
    after_id = _blank(after_queue_item_id)
    if not after_id:
        raise ValueError("after_queue_item_id is required")

    needs_search_enrich = bool(_blank(q))
    needs_ai = any(
        _blank(x)
        for x in (ai_alignment, ai_recommendation, ai_fit, ai_engagement)
    )

    ranked = list_work_queue(
        user_id,
        client_id=client_id,
        work_type=work_type,
        status=status,
        due=due,
        priority=priority,
        hot=hot,
        weblead=weblead,
        cross_client=cross_client,
        overdue_only=overdue_only,
        assigned_user_id=assigned_user_id,
        q=q,
        ai_alignment=ai_alignment,
        ai_recommendation=ai_recommendation,
        ai_fit=ai_fit,
        ai_engagement=ai_engagement,
        enrich_contacts=needs_search_enrich,
        apply_insight_overlay=needs_ai,
        page_results=False,
    )

    items = list(ranked.items)
    total = len(items)
    next_index: int | None = None

    for i, row in enumerate(items):
        if row.queue_item_id == after_id:
            next_index = i + 1
            break

    if next_index is None:
        # Current item already left the queue (e.g. completed). Use cursor.
        if after_work_priority is not None and after_company_id is not None:
            cursor = _cursor_sort_key(
                work_priority=int(after_work_priority),
                due_date=after_due_date,
                due_time=_blank(after_due_time),
                company_name=_blank(after_company_name),
                company_id=int(after_company_id),
                queue_item_id=after_id,
            )
            for i, row in enumerate(items):
                if _work_queue_sort_key(row) > cursor:
                    next_index = i
                    break
        else:
            next_index = 0 if items else None

    if next_index is None or next_index >= total:
        return WorkQueueNextResponse(
            user_id=ranked.user_id,
            mode=ranked.mode,
            client_ids=ranked.client_ids,
            after_queue_item_id=after_id,
            has_next=False,
            end_of_results=True,
            total=total,
            position=None,
            item=None,
            message="No further matching Work Queue items.",
        )

    next_row = items[next_index]
    # Never return the same queue_item_id the caller just finished.
    if next_row.queue_item_id == after_id:
        return WorkQueueNextResponse(
            user_id=ranked.user_id,
            mode=ranked.mode,
            client_ids=ranked.client_ids,
            after_queue_item_id=after_id,
            has_next=False,
            end_of_results=True,
            total=total,
            position=None,
            item=None,
            message="No further matching Work Queue items.",
        )
    if not needs_search_enrich:
        next_row = _enrich_work_queue_row(next_row)
    if not needs_ai:
        from work_queue_intel_overlay import attach_northstar_insight_overlay

        enriched_list, _ = attach_northstar_insight_overlay([next_row])
        next_row = enriched_list[0] if enriched_list else next_row

    return WorkQueueNextResponse(
        user_id=ranked.user_id,
        mode=ranked.mode,
        client_ids=ranked.client_ids,
        after_queue_item_id=after_id,
        has_next=True,
        end_of_results=False,
        total=total,
        position=next_index + 1,
        item=next_row,
        message="",
    )


def work_queue_summary_v2(
    user_id: int | None = None,
    *,
    client_id: int | None = None,
) -> WorkQueueSummaryV2:
    return list_work_queue(user_id, client_id=client_id, limit=1, offset=0).summary


def _mark_follow_up_activity_completed(conn, *, client_id: int, activity_id: int) -> None:
    now = _now().isoformat(sep=" ")
    conn.execute(
        """
        UPDATE activities
        SET follow_up_completed = 1,
            completion_status = 'completed',
            updated_at = ?
        WHERE activity_id = ? AND client_id = ?
          AND COALESCE(follow_up_completed, 0) = 0
          AND lower(COALESCE(completion_status, 'open')) NOT IN
              ('completed', 'cancelled', 'done', 'closed')
        """,
        (now, int(activity_id), int(client_id)),
    )


def _mark_work_queue_follow_up_completed(conn, *, client_id: int, queue_id: int) -> None:
    now = _now().isoformat(sep=" ")
    conn.execute(
        """
        UPDATE work_queue_items
        SET completion_status = 'completed',
            completed_at = ?,
            updated_at = ?
        WHERE id = ? AND client_id = ?
          AND lower(COALESCE(completion_status, 'open')) IN
              ('open', 'due', 'incomplete', 'pending', '')
        """,
        (now, now, int(queue_id), int(client_id)),
    )


def _complete_linked_follow_up_source(conn, *, client_id: int, source: str, source_id: int | None):
    """Complete one follow-up source and its linked activity/queue row for this client."""
    if source_id is None:
        return None
    kind = _blank(source).lower()
    if kind == "work_queue":
        row = conn.execute(
            """
            SELECT id, source_activity_id, company_id, contact_id, relationship_id,
                   due_date, due_time, notes
            FROM work_queue_items
            WHERE id = ? AND client_id = ?
            """,
            (int(source_id), int(client_id)),
        ).fetchone()
        if row is None:
            raise LookupError("Follow-up task not found.")
        _mark_work_queue_follow_up_completed(
            conn, client_id=client_id, queue_id=int(row["id"])
        )
        if row["source_activity_id"] is not None:
            _mark_follow_up_activity_completed(
                conn, client_id=client_id, activity_id=int(row["source_activity_id"])
            )
        return row
    if kind == "activity_follow_up":
        row = conn.execute(
            """
            SELECT activity_id, company_id, contact_id, relationship_id, follow_up_at, notes
            FROM activities
            WHERE activity_id = ? AND client_id = ?
            """,
            (int(source_id), int(client_id)),
        ).fetchone()
        if row is None:
            raise LookupError("Follow-up task not found.")
        _mark_follow_up_activity_completed(
            conn, client_id=client_id, activity_id=int(row["activity_id"])
        )
        now = _now().isoformat(sep=" ")
        conn.execute(
            """
            UPDATE work_queue_items
            SET completion_status = 'completed',
                completed_at = ?,
                updated_at = ?
            WHERE client_id = ? AND source_activity_id = ?
              AND lower(COALESCE(completion_status, 'open')) IN
                  ('open', 'due', 'incomplete', 'pending', '')
            """,
            (now, now, int(client_id), int(row["activity_id"])),
        )
        return row
    return None


def complete_work_queue_item(body: WorkQueueCompleteRequest) -> dict:
    """Mark a queue source complete without deleting company history."""
    from access import require_write_client_id

    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(body.client_id, user_id=user.id)

    with get_connection() as conn:
        kind = _blank(body.source).lower()
        if kind in {"activity_follow_up", "work_queue"}:
            _complete_linked_follow_up_source(
                conn,
                client_id=body.client_id,
                source=kind,
                source_id=body.source_id,
            )
        elif body.source == "ccr" and body.source_id is not None:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET follow_up_date = NULL,
                    next_action = '',
                    updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (_now().isoformat(sep=" "), body.source_id, body.client_id),
            )
        elif body.source == "activity_appointment" and body.source_id is not None:
            conn.execute(
                """
                UPDATE activities
                SET completion_status = 'completed', updated_at = ?
                WHERE activity_id = ? AND client_id = ?
                """,
                (_now().isoformat(sep=" "), body.source_id, body.client_id),
            )
        conn.commit()

    return {
        "ok": True,
        "client_id": body.client_id,
        "source": body.source,
        "source_id": body.source_id,
        "message": "Work item marked complete.",
    }


def _follow_up_at_stamp(follow_date: str, follow_time: str) -> str | None:
    date_part = _blank(follow_date)[:10]
    if not date_part:
        return None
    time_part = _blank(follow_time)[:5]
    if len(time_part) == 5:
        return f"{date_part} {time_part}:00"
    return date_part


def _ccr_snapshot_other_clients(conn, *, company_id: int, client_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
            FROM client_company_relationships
            WHERE company_id = ? AND client_id != ?
            ORDER BY client_id, id
            """,
            (int(company_id), int(client_id)),
        ).fetchall()
    ]


def _load_task_relationship(conn, *, client_id: int, company_id: int):
    company = conn.execute(
        """
        SELECT id, external_record_no, company_name
        FROM companies WHERE id = ?
        """,
        (int(company_id),),
    ).fetchone()
    if company is None:
        raise LookupError("Company not found.")
    rel = conn.execute(
        """
        SELECT id, client_id, external_record_no, status, assigned_user_id, next_action,
               follow_up_date
        FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (int(client_id), int(company_id)),
    ).fetchone()
    if rel is None:
        raise LookupError("No client-company relationship for this company.")
    return company, rel


def complete_follow_up_task(body: FollowUpTaskCompleteRequest) -> FollowUpTaskActionResult:
    """Complete the current open follow-up (contact or company-level) for one client."""
    from activities_data import insert_activity_row
    from client_workspace_data import _clear_workflow_follow_up_if_match

    if not isinstance(body, FollowUpTaskCompleteRequest):
        body = FollowUpTaskCompleteRequest.model_validate(body)
    from access import require_write_client_id

    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    client_id = require_write_client_id(body.client_id, user_id=user.id)
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    notes = _blank(body.notes)
    activity_id: int | None = None

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            company, rel = _load_task_relationship(
                conn, client_id=client_id, company_id=int(body.company_id)
            )
            company_id = int(company["id"])
            relationship_id = int(rel["id"])
            record_no = _blank(rel["external_record_no"]) or _blank(company["external_record_no"])
            other_before = _ccr_snapshot_other_clients(
                conn, company_id=company_id, client_id=client_id
            )
            row = _complete_linked_follow_up_source(
                conn,
                client_id=client_id,
                source=body.source,
                source_id=int(body.source_id),
            )
            if row is None:
                raise ValueError("Unsupported follow-up source.")
            if int(row["company_id"]) != company_id:
                raise LookupError("Follow-up task does not belong to this company.")
            stored_contact_id = (
                int(row["contact_id"]) if row["contact_id"] is not None else None
            )
            due_date = _date_only(row["due_date"] if "due_date" in row.keys() else None) or ""
            due_time = _blank(row["due_time"] if "due_time" in row.keys() else "")[:5]
            if not due_date and "follow_up_at" in row.keys():
                due_date = _date_only(row["follow_up_at"]) or ""
                due_time = _time_only(row["follow_up_at"])
            stamp = f"{due_date} {due_time}".strip() if due_time else due_date
            completion_notes = notes or (
                f"Completed follow-up{f' scheduled for {stamp}' if stamp else ''}.".strip()
            )
            activity_id = insert_activity_row(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=relationship_id,
                external_record_no=record_no,
                user_id=user.id,
                contact_id=stored_contact_id,
                activity_type="Note",
                outcome="Follow-up completed",
                notes=completion_notes,
                follow_up_at=None,
                assigned_user=created_by,
                created_by=created_by,
            )
            rel_date = _date_only(rel["follow_up_date"]) or ""
            if due_date and rel_date == due_date:
                conn.execute(
                    """
                    UPDATE client_company_relationships
                    SET follow_up_date = NULL, updated_at = datetime('now')
                    WHERE id = ? AND client_id = ?
                    """,
                    (relationship_id, client_id),
                )
            if stored_contact_id:
                _clear_workflow_follow_up_if_match(
                    conn,
                    contact_id=stored_contact_id,
                    client_id=client_id,
                    relationship_id=relationship_id,
                    task_date=due_date,
                    task_time=due_time,
                )
            other_after = _ccr_snapshot_other_clients(
                conn, company_id=company_id, client_id=client_id
            )
            if other_after != other_before:
                raise RuntimeError("Refusing to overwrite another client's relationship.")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    if activity_id:
        from search_data import index_activity

        try:
            index_activity(int(activity_id))
        except Exception:
            pass
    return FollowUpTaskActionResult(
        ok=True,
        message="Follow-up task completed.",
        activity_id=activity_id,
        client_id=client_id,
        source=_blank(body.source),
        source_id=int(body.source_id),
    )


def reschedule_follow_up_task(body: FollowUpTaskRescheduleRequest) -> FollowUpTaskActionResult:
    """Update the same open follow-up task; do not create a duplicate."""
    from activities_data import insert_activity_row
    from next_actions import canonicalize_next_action

    if not isinstance(body, FollowUpTaskRescheduleRequest):
        body = FollowUpTaskRescheduleRequest.model_validate(body)
    from access import require_write_client_id

    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    client_id = require_write_client_id(body.client_id, user_id=user.id)
    follow_date = _blank(body.follow_up_date)[:10]
    follow_time = _blank(body.follow_up_time)[:5]
    if not follow_date or not follow_time:
        raise ValueError("Follow-up date and time are required to reschedule.")
    follow_at = _follow_up_at_stamp(follow_date, follow_time)
    if not follow_at:
        raise ValueError("Follow-up date and time are required to reschedule.")
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    notes = _blank(body.notes)
    follow_up_activity_id: int | None = None

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            company, rel = _load_task_relationship(
                conn, client_id=client_id, company_id=int(body.company_id)
            )
            company_id = int(company["id"])
            relationship_id = int(rel["id"])
            record_no = _blank(rel["external_record_no"]) or _blank(company["external_record_no"])
            other_before = _ccr_snapshot_other_clients(
                conn, company_id=company_id, client_id=client_id
            )
            kind = _blank(body.source).lower()
            stored_contact_id: int | None = None
            queue_id: int | None = None
            activity_id: int | None = None
            existing_assigned_id = (
                int(rel["assigned_user_id"]) if rel["assigned_user_id"] is not None else user.id
            )
            existing_assigned_name = created_by
            existing_notes = ""

            if kind == "work_queue":
                row = conn.execute(
                    """
                    SELECT id, source_activity_id, company_id, contact_id, relationship_id,
                           assigned_user_id, assigned_user, notes
                    FROM work_queue_items
                    WHERE id = ? AND client_id = ?
                    """,
                    (int(body.source_id), int(client_id)),
                ).fetchone()
                if row is None:
                    raise LookupError("Follow-up task not found.")
                if int(row["company_id"]) != company_id:
                    raise LookupError("Follow-up task does not belong to this company.")
                queue_id = int(row["id"])
                stored_contact_id = (
                    int(row["contact_id"]) if row["contact_id"] is not None else None
                )
                activity_id = (
                    int(row["source_activity_id"])
                    if row["source_activity_id"] is not None
                    else None
                )
                if row["assigned_user_id"] is not None:
                    existing_assigned_id = int(row["assigned_user_id"])
                existing_assigned_name = _blank(row["assigned_user"]) or existing_assigned_name
                existing_notes = _blank(row["notes"])
            elif kind == "activity_follow_up":
                row = conn.execute(
                    """
                    SELECT activity_id, company_id, contact_id, relationship_id,
                           user_id, assigned_user, notes
                    FROM activities
                    WHERE activity_id = ? AND client_id = ?
                    """,
                    (int(body.source_id), int(client_id)),
                ).fetchone()
                if row is None:
                    raise LookupError("Follow-up task not found.")
                if int(row["company_id"]) != company_id:
                    raise LookupError("Follow-up task does not belong to this company.")
                activity_id = int(row["activity_id"])
                stored_contact_id = (
                    int(row["contact_id"]) if row["contact_id"] is not None else None
                )
                if row["user_id"] is not None:
                    existing_assigned_id = int(row["user_id"])
                existing_assigned_name = _blank(row["assigned_user"]) or existing_assigned_name
                existing_notes = _blank(row["notes"])
                linked = conn.execute(
                    """
                    SELECT id FROM work_queue_items
                    WHERE client_id = ? AND source_activity_id = ?
                      AND lower(COALESCE(completion_status, 'open')) IN
                          ('open', 'due', 'incomplete', 'pending', '')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(client_id), activity_id),
                ).fetchone()
                if linked is not None:
                    queue_id = int(linked["id"])
            else:
                raise ValueError("Unsupported follow-up source.")

            assignee_id = (
                int(body.assigned_user_id)
                if body.assigned_user_id is not None and int(body.assigned_user_id) > 0
                else existing_assigned_id
            )
            assignee_name = existing_assigned_name
            if assignee_id is not None:
                uname = conn.execute(
                    "SELECT full_name FROM users WHERE id = ?",
                    (int(assignee_id),),
                ).fetchone()
                if uname is not None:
                    assignee_name = _blank(uname["full_name"]) or assignee_name
            next_action = canonicalize_next_action(
                _blank(body.next_action),
                client_id=client_id,
                status=_blank(rel["status"]),
                conn=conn,
            ) or _blank(rel["next_action"]) or "Follow-Up"
            task_notes = notes or existing_notes or "Rescheduled follow-up"

            if activity_id is not None:
                conn.execute(
                    """
                    UPDATE activities
                    SET follow_up_at = ?, assigned_user = ?, user_id = ?,
                        notes = CASE WHEN TRIM(?) = '' THEN notes ELSE ? END,
                        updated_at = datetime('now')
                    WHERE activity_id = ? AND client_id = ?
                    """,
                    (
                        follow_at,
                        assignee_name,
                        assignee_id,
                        notes,
                        task_notes,
                        activity_id,
                        client_id,
                    ),
                )
                follow_up_activity_id = activity_id
            if queue_id is not None:
                conn.execute(
                    """
                    UPDATE work_queue_items
                    SET due_date = ?, due_time = ?, assigned_user_id = ?, assigned_user = ?,
                        notes = ?, source_activity_id = COALESCE(source_activity_id, ?),
                        updated_at = datetime('now')
                    WHERE id = ? AND client_id = ?
                    """,
                    (
                        follow_date,
                        follow_time,
                        assignee_id,
                        assignee_name,
                        task_notes,
                        activity_id,
                        queue_id,
                        client_id,
                    ),
                )

            conn.execute(
                """
                UPDATE client_company_relationships
                SET follow_up_date = ?,
                    next_action = ?,
                    assigned_user_id = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND client_id = ?
                """,
                (
                    follow_at,
                    next_action,
                    assignee_id,
                    relationship_id,
                    client_id,
                ),
            )
            if stored_contact_id:
                from client_workspace_data import ensure_contact_workflow_schema

                ensure_contact_workflow_schema(conn)
                conn.execute(
                    """
                    INSERT INTO contact_client_workflows (
                        contact_id, client_id, relationship_id, status, assigned_user_id,
                        next_action, follow_up_date, follow_up_time, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(contact_id, client_id) DO UPDATE SET
                        relationship_id = excluded.relationship_id,
                        assigned_user_id = excluded.assigned_user_id,
                        next_action = excluded.next_action,
                        follow_up_date = excluded.follow_up_date,
                        follow_up_time = excluded.follow_up_time,
                        updated_at = datetime('now')
                    """,
                    (
                        stored_contact_id,
                        client_id,
                        relationship_id,
                        _blank(rel["status"]),
                        assignee_id,
                        next_action,
                        follow_date,
                        follow_time,
                    ),
                )

            insert_activity_row(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=relationship_id,
                external_record_no=record_no,
                user_id=user.id,
                contact_id=stored_contact_id,
                activity_type="Follow-Up Rescheduled",
                outcome=f"{follow_date} {follow_time}".strip(),
                notes=notes or f"Follow-up rescheduled to {follow_date} {follow_time}.".strip(),
                follow_up_at=follow_at,
                assigned_user=assignee_name,
                created_by=created_by,
            )
            other_after = _ccr_snapshot_other_clients(
                conn, company_id=company_id, client_id=client_id
            )
            if other_after != other_before:
                raise RuntimeError("Refusing to overwrite another client's relationship.")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    if follow_up_activity_id:
        from search_data import index_activity

        try:
            index_activity(int(follow_up_activity_id))
        except Exception:
            pass
    return FollowUpTaskActionResult(
        ok=True,
        message="Follow-up rescheduled.",
        activity_id=follow_up_activity_id,
        follow_up_activity_id=follow_up_activity_id,
        client_id=client_id,
        source=_blank(body.source),
        source_id=int(body.source_id),
    )


def clients_for_work_queue(user_id: int | None = None) -> list[dict]:
    user = resolve_staff_actor(user_id)
    if user is None:
        return []
    return [
        {
            "client_id": c.client_id,
            "client_code": c.client_code,
            "client_name": c.client_name,
        }
        for c in list_clients_for_user(user.id, active_only=True)
    ]


def log_work_queue_call(body: WorkQueueLogCallRequest) -> WorkQueueLogCallResult:
    """
    Create a Call activity for the selected client/company, optionally update
    status / next action, schedule a follow-up, and complete the current queue task.
    Never creates a Call merely by opening a New Assignment.
    """
    from activities_data import create_activity
    from appointments_data import (
        ensure_appointment_set_milestone,
        insert_appointment_row,
        is_appointment_set_status,
        normalize_appointment_payload,
    )
    from client_workspace_data import update_relationship_status
    from models import ActivityCreateRequest

    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    from access import require_write_client_id

    require_write_client_id(body.client_id, user_id=user.id)

    record_no = body.external_record_no.strip()
    if not record_no:
        raise ValueError("external_record_no is required.")
    appointment_payload = None
    if is_appointment_set_status(_blank(body.status)):
        appointment_payload = normalize_appointment_payload(
            body.appointment,
            status=_blank(body.status),
        )

    with get_connection() as conn:
        client = conn.execute(
            "SELECT id, code, name FROM clients WHERE id = ?",
            (body.client_id,),
        ).fetchone()
        if client is None:
            raise LookupError("Client not found.")
        company = conn.execute(
            "SELECT id, external_record_no FROM companies WHERE external_record_no = ?",
            (record_no,),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found.")
        rel = conn.execute(
            """
            SELECT id FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (body.client_id, int(company["id"])),
        ).fetchone()
        if rel is None:
            raise LookupError("No client-company relationship for this company.")
        company_id = int(company["id"])
        relationship_id = int(rel["id"])
        client_name = _blank(client["name"]) or _blank(client["code"]) or "Client"
        if appointment_payload and appointment_payload.get("idempotency_key"):
            existing = conn.execute(
                """
                SELECT id, activity_id FROM appointments
                WHERE client_id = ? AND idempotency_key = ?
                """,
                (int(body.client_id), appointment_payload["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                return WorkQueueLogCallResult(
                    ok=True,
                    message="Appointment already saved.",
                    activity_id=int(existing["activity_id"]) if existing["activity_id"] is not None else None,
                    follow_up_activity_id=None,
                    client_id=body.client_id,
                    external_record_no=record_no,
                    completed_queue_item=False,
                    appointment_id=int(existing["id"]),
                )

    created_by = body.created_by.strip() or user.full_name
    from next_actions import canonicalize_next_action

    next_action = canonicalize_next_action(
        body.next_action.strip(),
        client_id=body.client_id,
        status=_blank(body.status),
    )

    activity = create_activity(
        ActivityCreateRequest(
            client=client_name,
            client_id=int(body.client_id),
            external_record_no=record_no,
            user_id=user.id,
            contact_id=body.contact_id,
            activity_type="Call",
            outcome=body.outcome.strip(),
            notes=body.notes,
            assigned_user=created_by,
            created_by=created_by,
        )
    )

    follow_up_activity_id = None
    follow_date = _date_only(body.follow_up_date)
    follow_time = (body.follow_up_time or "").strip()
    if follow_date:
        if len(follow_time) == 5:
            follow_at = f"{follow_date} {follow_time}:00"
        else:
            follow_at = f"{follow_date} 09:00:00"
        follow = create_activity(
            ActivityCreateRequest(
                client=client_name,
                client_id=int(body.client_id),
                external_record_no=record_no,
                user_id=user.id,
                contact_id=body.contact_id,
                activity_type="Follow-Up",
                notes=next_action or "Scheduled from Work Queue call",
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
                SET next_action = ?,
                    follow_up_date = ?,
                    updated_at = ?
                WHERE client_id = ? AND company_id = ?
                """,
                (
                    next_action or "Follow-Up",
                    follow_date,
                    _now().isoformat(sep=" "),
                    body.client_id,
                    company_id,
                ),
            )
            conn.commit()
    elif next_action:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET next_action = ?,
                    updated_at = ?
                WHERE client_id = ? AND company_id = ?
                """,
                (
                    next_action,
                    _now().isoformat(sep=" "),
                    body.client_id,
                    company_id,
                ),
            )
            conn.commit()

    if body.status and body.status.strip():
        update_relationship_status(
            record_no,
            client=client_name,
            client_id=body.client_id,
            status=body.status.strip(),
            user=created_by,
        )

    completed = False
    completable = {
        "activity_follow_up",
        "work_queue",
        "ccr",
        "activity_appointment",
        "activity_call",
    }
    if body.complete_current and body.queue_source in completable:
        complete_work_queue_item(
            WorkQueueCompleteRequest(
                client_id=body.client_id,
                source=body.queue_source,
                source_id=body.queue_source_id,
                company_id=company_id,
            )
        )
        completed = True

    appointment_id = None
    if appointment_payload and activity.activity_id:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if appointment_payload["revenue_specialist_user_id"] is None:
                    appointment_payload["revenue_specialist_user_id"] = user.id
                appointment_id, _created = insert_appointment_row(
                    conn,
                    client_id=body.client_id,
                    company_id=company_id,
                    relationship_id=relationship_id,
                    contact_id=body.contact_id,
                    activity_id=activity.activity_id,
                    created_by=created_by,
                    payload=appointment_payload,
                )
                ensure_appointment_set_milestone(
                    conn, activity_id=activity.activity_id, created_by=created_by
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    return WorkQueueLogCallResult(
        ok=True,
        message="Call saved.",
        activity_id=activity.activity_id,
        follow_up_activity_id=follow_up_activity_id,
        client_id=body.client_id,
        external_record_no=record_no,
        completed_queue_item=completed,
        appointment_id=appointment_id,
    )
