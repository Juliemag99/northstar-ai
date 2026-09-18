"""Read-only Reports: client results, team performance, and campaign performance.

Counts client-scoped activity, appointment, opportunity, campaign membership,
and current-status records. Never writes. All My Clients uses only authorized
clients and does not collapse shared master companies/contacts across clients.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Literal

from access import resolve_dashboard_client_ids, user_can_access_client
from data_steward import sql_active_ccr, sql_active_company
from staff_context import resolve_staff_actor
from staff_rbac import user_has_permission
from activities_data import ACTIVITY_TIMELINE_TYPES
from campaigns_data import ensure_campaigns_schema
from db import DB_PATH, get_connection
from models import (
    ReportCampaignRow,
    ReportCampaignResponse,
    ReportClientResultsRow,
    ReportClientResultsResponse,
    ReportFilterOption,
    ReportFiltersResponse,
    ReportKpi,
    ReportRecordRow,
    ReportRecordsResponse,
    ReportTeamRow,
    ReportTeamResponse,
)
from work_queue_data import _follow_up_bucket, list_open_follow_up_tasks

PAGE_SIZE = 50

METRIC_DEFINITIONS: dict[str, str] = {
    "calls": "Call activities in the date range for this client. Each activity row is counted once.",
    "follow_ups_scheduled": "Follow-Up tasks created in the date range. Reschedules and completion notes are not extra scheduled follow-ups.",
    "follow_ups_completed": "Follow-Up task rows marked completed in the date range. Companion “Follow-up completed” notes are not counted here.",
    "appointments_set": "Appointments created in the date range, including ones later completed or cancelled.",
    "appointments_completed": "Appointments whose completed date falls in the range.",
    "appointments_cancelled": "Appointments whose cancelled date falls in the range.",
    "opportunities": "Cross-client opportunities assigned to this client in the date range.",
    "outcomes": "Activity rows in the date range that have a non-empty outcome. Completion notes can appear here.",
    "hot_prospects": "Companies whose current client status is Hot Prospect. Not limited by the date range.",
    "assigned_clients": "Clients this specialist is actively assigned to in the current report scope.",
    "notes": "Note activities in the date range. Completing a follow-up may also write a Note.",
    "overdue_tasks": "Open follow-up tasks that are currently overdue. Not limited by the date range.",
    "companies": "Companies currently in the campaign. Not limited by the date range.",
    "contacts": "Contacts currently in the campaign. Not limited by the date range.",
    "follow_ups": "Follow-Up activities after campaign membership began, in the date range.",
    "appointments": "Appointments created after campaign membership began, in the date range.",
    "call_to_appointment_pct": "Appointments ÷ companies in this campaign (current members).",
    "outcome_to_call_pct": "Outcomes ÷ calls attributed to this campaign in the date range.",
}

CLIENT_SORT = (
    "client_name",
    "calls",
    "follow_ups_scheduled",
    "follow_ups_completed",
    "appointments_set",
    "appointments_completed",
    "appointments_cancelled",
    "opportunities",
    "outcomes",
    "hot_prospects",
)
TEAM_SORT = (
    "user_name",
    "assigned_clients",
    "calls",
    "notes",
    "follow_ups_completed",
    "appointments_set",
    "appointments_completed",
    "outcomes",
    "overdue_tasks",
)
CAMPAIGN_SORT = (
    "campaign_name",
    "client_name",
    "companies",
    "contacts",
    "calls",
    "follow_ups",
    "appointments",
    "outcomes",
    "call_to_appointment_pct",
    "outcome_to_call_pct",
)

GroupBy = Literal["client", "user", "none"]


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def current_month_range() -> tuple[str, str]:
    today = date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def _date10(value: object | None, fallback: str) -> str:
    text = _blank(value)[:10]
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return fallback


def _page(limit: int | None, offset: int | None, *, cap: int = 200) -> tuple[int, int]:
    return max(1, min(int(limit or PAGE_SIZE), cap)), max(0, int(offset or 0))


def _sort_dir(value: object | None) -> str:
    return "desc" if _blank(value).lower() == "desc" else "asc"


def activity_date_sql(alias: str = "a") -> str:
    return (
        f"substr(replace(replace(trim(COALESCE(NULLIF(TRIM({alias}.activity_at), ''), "
        f"{alias}.created_at)), 'T', ' '), 'Z', ''), 1, 10)"
    )


def updated_date_sql(alias: str = "a") -> str:
    return (
        f"substr(replace(replace(trim(COALESCE(NULLIF(TRIM({alias}.updated_at), ''), "
        f"NULLIF(TRIM({alias}.activity_at), ''), {alias}.created_at)), 'T', ' '), 'Z', ''), 1, 10)"
    )


def appointment_date_sql(alias: str, column: str) -> str:
    return (
        f"substr(replace(replace(trim(COALESCE({alias}.{column}, '')), 'T', ' '), 'Z', ''), 1, 10)"
    )


def _placeholders(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def _user_match_sql(alias: str = "a") -> str:
    """Resolve the activity’s specialist: user_id, else name match on assigned_user/created_by."""
    return f"""
        COALESCE(
            {alias}.user_id,
            (
                SELECT u.id FROM users u
                WHERE lower(trim(u.full_name)) = lower(trim(COALESCE(
                    NULLIF(TRIM({alias}.assigned_user), ''),
                    {alias}.created_by
                )))
                ORDER BY u.id
                LIMIT 1
            ),
            0
        )
    """


def _campaign_appointment_sql(alias: str = "ap") -> str:
    created = (
        f"CASE WHEN replace(replace(trim(COALESCE({alias}.created_at, '')), 'T', ' '), 'Z', '') != '' "
        f"THEN replace(replace(trim(COALESCE({alias}.created_at, '')), 'T', ' '), 'Z', '') "
        f"ELSE replace(replace(trim(COALESCE({alias}.appointment_date, '')), 'T', ' '), 'Z', '') END"
    )
    cc = "replace(replace(trim(COALESCE(cc.created_at, '')), 'T', ' '), 'Z', '')"
    ct = "replace(replace(trim(COALESCE(ct.created_at, '')), 'T', ' '), 'Z', '')"
    return f"""
        (
          EXISTS (
            SELECT 1 FROM campaign_companies cc
            WHERE cc.campaign_id = ?
              AND cc.client_id = {alias}.client_id
              AND cc.company_id = {alias}.company_id
              AND {created} > {cc}
          )
          OR EXISTS (
            SELECT 1 FROM campaign_contacts ct
            WHERE ct.campaign_id = ?
              AND ct.client_id = {alias}.client_id
              AND ct.contact_id = {alias}.contact_id
              AND {created} > {ct}
          )
        )
    """


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * float(numerator) / float(denominator), 1)


@dataclass
class ReportQuery:
    selected_client_id: int
    client_ids: list[int]
    date_from: str
    date_to: str
    user_id: int | None
    user_name: str
    campaign_id: int | None
    activity_type: str
    outcome: str
    client_label: str
    campaign_label: str
    user_label: str
    campaign_ids: list[int] | None = None


def _scope_client_ids(selected_client_id: int | None) -> tuple[int, list[int]]:
    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    raw = int(selected_client_id or 0)
    cid = raw if raw > 0 else None
    if not user_has_permission(int(user.id), "reports.view", client_id=cid):
        raise PermissionError("Not authorized for this client.")
    if raw > 0:
        if not user_can_access_client(user.id, raw) and not user.is_administrator:
            raise PermissionError("Not authorized for this client.")
        return raw, resolve_dashboard_client_ids(user.id, selected_client_id=raw)
    return 0, resolve_dashboard_client_ids(user.id, selected_client_id=None)


def _resolve_user(conn, user_id: int | None) -> tuple[int | None, str]:
    if user_id is None or int(user_id) <= 0:
        return None, ""
    row = conn.execute(
        "SELECT id, full_name FROM users WHERE id = ? AND COALESCE(active, 1) = 1",
        (int(user_id),),
    ).fetchone()
    if row is None:
        raise ValueError("Assigned rep/user was not found.")
    return int(row["id"]), _blank(row["full_name"])


def _client_label(conn, selected: int, client_ids: list[int]) -> str:
    if selected > 0:
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (selected,)).fetchone()
        return _blank(row["name"]) if row is not None else f"Client {selected}"
    if not client_ids:
        return "All My Clients"
    return "All My Clients"


def _campaign_label(conn, campaign_id: int | None) -> str:
    if campaign_id is None:
        return "All campaigns"
    row = conn.execute(
        "SELECT campaign_name FROM client_campaigns WHERE id = ?",
        (int(campaign_id),),
    ).fetchone()
    return _blank(row["campaign_name"]) if row is not None else f"Campaign {campaign_id}"


def resolve_report_query(
    *,
    client_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    user_id: int | None = None,
    campaign_id: int | None = None,
    activity_type: str = "",
    outcome: str = "",
) -> ReportQuery:
    start_default, end_default = current_month_range()
    selected, client_ids = _scope_client_ids(client_id)
    start = _date10(date_from, start_default)
    end = _date10(date_to, end_default)
    if start > end:
        start, end = end, start
    if not _db_exists():
        return ReportQuery(
            selected_client_id=selected,
            client_ids=client_ids,
            date_from=start,
            date_to=end,
            user_id=None,
            user_name="",
            campaign_id=None,
            activity_type=_blank(activity_type),
            outcome=_blank(outcome),
            client_label="All My Clients" if selected <= 0 else "",
            campaign_label="All campaigns",
            user_label="All reps",
        )
    with get_connection() as conn:
        uid, uname = _resolve_user(conn, user_id)
        camp = int(campaign_id) if campaign_id and int(campaign_id) > 0 else None
        if camp is not None:
            row = conn.execute(
                "SELECT id, client_id, campaign_name FROM client_campaigns WHERE id = ?",
                (camp,),
            ).fetchone()
            if row is None:
                raise LookupError("Campaign not found.")
            if int(row["client_id"]) not in client_ids:
                raise PermissionError("Not authorized for this campaign.")
        return ReportQuery(
            selected_client_id=selected,
            client_ids=client_ids,
            date_from=start,
            date_to=end,
            user_id=uid,
            user_name=uname,
            campaign_id=camp,
            activity_type=_blank(activity_type),
            outcome=_blank(outcome),
            client_label=_client_label(conn, selected, client_ids),
            campaign_label=_campaign_label(conn, camp),
            user_label=uname or "All reps",
        )


def _activity_user_clause(alias: str, query: ReportQuery) -> tuple[str, list[Any]]:
    if query.user_id is None:
        return "", []
    name = query.user_name.lower()
    sql = (
        f"AND ({alias}.user_id = ? OR "
        f"({alias}.user_id IS NULL AND ("
        f"lower(trim({alias}.assigned_user)) = ? OR lower(trim({alias}.created_by)) = ?)))"
    )
    return sql, [query.user_id, name, name]


def _appointment_user_clause(alias: str, query: ReportQuery) -> tuple[str, list[Any]]:
    if query.user_id is None:
        return "", []
    name = query.user_name.lower()
    sql = (
        f"AND ({alias}.revenue_specialist_user_id = ? OR "
        f"({alias}.revenue_specialist_user_id IS NULL AND lower(trim({alias}.created_by)) = ?))"
    )
    return sql, [query.user_id, name]


def _campaign_ids_filter(query: ReportQuery) -> list[int] | None:
    if query.campaign_id is not None:
        return [int(query.campaign_id)]
    if query.campaign_ids is not None:
        return [int(cid) for cid in query.campaign_ids]
    return None


def _activity_in_campaigns_sql(alias: str, campaign_ids: list[int]) -> tuple[str, list[Any]]:
    if not campaign_ids:
        return "AND 1=0", []
    ph = _placeholders(campaign_ids)
    activity_ts = "replace(replace(trim(COALESCE(" + alias + ".activity_at, '')), 'T', ' '), 'Z', '')"
    created_ts = "replace(replace(trim(COALESCE(" + alias + ".created_at, '')), 'T', ' '), 'Z', '')"
    cc_ts = "replace(replace(trim(COALESCE(cc.created_at, '')), 'T', ' '), 'Z', '')"
    ct_ts = "replace(replace(trim(COALESCE(ct.created_at, '')), 'T', ' '), 'Z', '')"
    sql = f"""
        AND (
          EXISTS (
            SELECT 1 FROM campaign_companies cc
            WHERE cc.campaign_id IN ({ph})
              AND cc.client_id = {alias}.client_id
              AND cc.company_id = {alias}.company_id
              AND CASE WHEN {activity_ts} != '' THEN {activity_ts} ELSE {created_ts} END
                  > {cc_ts}
          )
          OR EXISTS (
            SELECT 1 FROM campaign_contacts ct
            WHERE ct.campaign_id IN ({ph})
              AND ct.client_id = {alias}.client_id
              AND ct.contact_id = {alias}.contact_id
              AND CASE WHEN {activity_ts} != '' THEN {activity_ts} ELSE {created_ts} END
                  > {ct_ts}
          )
        )
    """
    return sql, [*campaign_ids, *campaign_ids]


def _appointment_in_campaigns_sql(alias: str, campaign_ids: list[int]) -> tuple[str, list[Any]]:
    if not campaign_ids:
        return "AND 1=0", []
    ph = _placeholders(campaign_ids)
    created = (
        f"CASE WHEN replace(replace(trim(COALESCE({alias}.created_at, '')), 'T', ' '), 'Z', '') != '' "
        f"THEN replace(replace(trim(COALESCE({alias}.created_at, '')), 'T', ' '), 'Z', '') "
        f"ELSE replace(replace(trim(COALESCE({alias}.appointment_date, '')), 'T', ' '), 'Z', '') END"
    )
    cc = "replace(replace(trim(COALESCE(cc.created_at, '')), 'T', ' '), 'Z', '')"
    ct = "replace(replace(trim(COALESCE(ct.created_at, '')), 'T', ' '), 'Z', '')"
    sql = f"""
        AND (
          EXISTS (
            SELECT 1 FROM campaign_companies cc
            WHERE cc.campaign_id IN ({ph})
              AND cc.client_id = {alias}.client_id
              AND cc.company_id = {alias}.company_id
              AND {created} > {cc}
          )
          OR EXISTS (
            SELECT 1 FROM campaign_contacts ct
            WHERE ct.campaign_id IN ({ph})
              AND ct.client_id = {alias}.client_id
              AND ct.contact_id = {alias}.contact_id
              AND {created} > {ct}
          )
        )
    """
    return sql, [*campaign_ids, *campaign_ids]


def _campaign_activity_clause(alias: str, query: ReportQuery) -> tuple[str, list[Any]]:
    ids = _campaign_ids_filter(query)
    if ids is None:
        return "", []
    return _activity_in_campaigns_sql(alias, ids)


def _campaign_appt_clause(alias: str, query: ReportQuery) -> tuple[str, list[Any]]:
    ids = _campaign_ids_filter(query)
    if ids is None:
        return "", []
    return _appointment_in_campaigns_sql(alias, ids)


def _type_clause(alias: str, query: ReportQuery, *, kinds: Iterable[str] | None = None) -> tuple[str, list[Any]]:
    params: list[Any] = []
    parts: list[str] = []
    if kinds:
        lowered = [k.lower() for k in kinds]
        ph = ",".join("?" * len(lowered))
        parts.append(f"lower(trim({alias}.activity_type)) IN ({ph})")
        params.extend(lowered)
    if query.activity_type:
        parts.append(f"lower(trim({alias}.activity_type)) = ?")
        params.append(query.activity_type.lower())
    if query.outcome:
        parts.append(f"lower(trim(COALESCE({alias}.outcome, ''))) = ?")
        params.append(query.outcome.lower())
    if not parts:
        return "", []
    return "AND " + " AND ".join(parts), params


def _include_appointments(query: ReportQuery) -> bool:
    if query.outcome:
        return True
    kind = query.activity_type.lower()
    if not kind:
        return True
    return kind == "appointment" or kind.startswith("appointment ")


def _include_activity_metric(query: ReportQuery, type_name: str) -> bool:
    if not query.activity_type:
        return True
    return query.activity_type.lower() == type_name.lower()


def _grouped_activity_counts(
    conn,
    query: ReportQuery,
    *,
    group: GroupBy,
    kinds: Iterable[str] | None = None,
    date_kind: Literal["activity", "completed"] = "activity",
    extra_sql: str = "",
    extra_params: list[Any] | None = None,
) -> dict[int, int]:
    if not query.client_ids:
        return {}
    date_expr = updated_date_sql("a") if date_kind == "completed" else activity_date_sql("a")
    user_sql, user_params = _activity_user_clause("a", query)
    camp_sql, camp_params = _campaign_activity_clause("a", query)
    type_sql, type_params = _type_clause("a", query, kinds=kinds)
    grp = "a.client_id" if group == "client" else (_user_match_sql("a") if group == "user" else "0")
    sql = f"""
        SELECT {grp} AS grp, COUNT(*) AS n
        FROM activities a
        WHERE a.client_id IN ({_placeholders(query.client_ids)})
          AND {date_expr} >= ?
          AND {date_expr} <= ?
          {user_sql}
          {camp_sql}
          {type_sql}
          {extra_sql}
        GROUP BY grp
    """
    params: list[Any] = [
        *query.client_ids,
        query.date_from,
        query.date_to,
        *user_params,
        *camp_params,
        *type_params,
        *(extra_params or []),
    ]
    out: dict[int, int] = {}
    for row in conn.execute(sql, params).fetchall():
        out[int(row["grp"] or 0)] = int(row["n"] or 0)
    return out


def _grouped_outcome_counts(conn, query: ReportQuery, *, group: GroupBy) -> dict[int, int]:
    extra = "AND TRIM(COALESCE(a.outcome, '')) != ''"
    if query.outcome:
        extra = ""
    return _grouped_activity_counts(
        conn, query, group=group, extra_sql=extra
    )


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _grouped_appointment_counts(
    conn,
    query: ReportQuery,
    *,
    group: GroupBy,
    date_column: str,
    statuses: Iterable[str] | None = None,
) -> dict[int, int]:
    if not query.client_ids or not _table_exists(conn, "appointments"):
        return {}
    if not _include_appointments(query) and date_column == "created_at":
        if query.activity_type.lower() not in {"", "appointment", "appointment created"}:
            return {}
    if query.activity_type:
        wanted = query.activity_type.lower()
        if date_column == "completed_at" and wanted not in {"", "appointment completed", "appointment"}:
            return {}
        if date_column == "cancelled_at" and wanted not in {"", "appointment cancelled", "appointment"}:
            return {}
        if date_column == "created_at" and wanted not in {
            "",
            "appointment",
            "appointment created",
        }:
            return {}
    date_expr = appointment_date_sql("ap", date_column)
    user_sql, user_params = _appointment_user_clause("ap", query)
    camp_sql, camp_params = _campaign_appt_clause("ap", query)
    status_sql = ""
    status_params: list[Any] = []
    if statuses:
        lowered = [s.lower() for s in statuses]
        status_sql = f"AND lower(trim(ap.status)) IN ({_placeholders([1] * len(lowered))})"
        status_params = lowered
    outcome_sql = ""
    outcome_params: list[Any] = []
    if query.outcome:
        outcome_sql = "AND lower(trim(COALESCE(ap.outcome, ''))) = ?"
        outcome_params = [query.outcome.lower()]
    if group == "client":
        grp = "ap.client_id"
    elif group == "user":
        grp = """
            COALESCE(
                ap.revenue_specialist_user_id,
                (
                    SELECT u.id FROM users u
                    WHERE lower(trim(u.full_name)) = lower(trim(ap.created_by))
                    ORDER BY u.id LIMIT 1
                ),
                0
            )
        """
    else:
        grp = "0"
    sql = f"""
        SELECT {grp} AS grp, COUNT(*) AS n
        FROM appointments ap
        WHERE ap.client_id IN ({_placeholders(query.client_ids)})
          AND {date_expr} >= ?
          AND {date_expr} <= ?
          AND TRIM(COALESCE(ap.{date_column}, '')) != ''
          {user_sql}
          {camp_sql}
          {status_sql}
          {outcome_sql}
        GROUP BY grp
    """
    params: list[Any] = [
        *query.client_ids,
        query.date_from,
        query.date_to,
        *user_params,
        *camp_params,
        *status_params,
        *outcome_params,
    ]
    out: dict[int, int] = {}
    for row in conn.execute(sql, params).fetchall():
        out[int(row["grp"] or 0)] = int(row["n"] or 0)
    return out


def _grouped_opportunity_counts(conn, query: ReportQuery, *, group: GroupBy) -> dict[int, int]:
    if not query.client_ids or not _table_exists(conn, "opportunity_assignments"):
        return {}
    if query.activity_type or query.outcome:
        return {}
    date_expr = (
        "substr(replace(replace(trim(COALESCE(oa.created_at, '')), 'T', ' '), 'Z', ''), 1, 10)"
    )
    camp_sql = ""
    camp_params: list[Any] = []
    if query.campaign_id is not None:
        camp_sql = """
            AND (
                oa.target_campaign_id = ?
                OR EXISTS (
                    SELECT 1 FROM campaign_companies cc
                    WHERE cc.campaign_id = ?
                      AND cc.client_id = oa.target_client_id
                      AND cc.company_id = oa.company_id
                      AND replace(replace(trim(COALESCE(oa.created_at, '')), 'T', ' '), 'Z', '')
                          > replace(replace(trim(COALESCE(cc.created_at, '')), 'T', ' '), 'Z', '')
                )
            )
        """
        camp_params = [query.campaign_id, query.campaign_id]
    if query.user_id is not None:
        return {}
    grp = "oa.target_client_id" if group == "client" else "0"
    sql = f"""
        SELECT {grp} AS grp, COUNT(*) AS n
        FROM opportunity_assignments oa
        WHERE oa.target_client_id IN ({_placeholders(query.client_ids)})
          AND {date_expr} >= ?
          AND {date_expr} <= ?
          {camp_sql}
        GROUP BY grp
    """
    params: list[Any] = [*query.client_ids, query.date_from, query.date_to, *camp_params]
    out: dict[int, int] = {}
    for row in conn.execute(sql, params).fetchall():
        out[int(row["grp"] or 0)] = int(row["n"] or 0)
    return out


def _grouped_hot_counts(conn, query: ReportQuery) -> dict[int, int]:
    if not query.client_ids or query.activity_type or query.outcome:
        return {}
    camp_sql = ""
    camp_params: list[Any] = []
    if query.campaign_id is not None:
        camp_sql = """
            AND EXISTS (
                SELECT 1 FROM campaign_companies cc
                WHERE cc.campaign_id = ?
                  AND cc.client_id = ccr.client_id
                  AND cc.company_id = ccr.company_id
            )
        """
        camp_params = [query.campaign_id]
    user_sql = ""
    user_params: list[Any] = []
    if query.user_id is not None:
        user_sql = "AND ccr.assigned_user_id = ?"
        user_params = [query.user_id]
    sql = f"""
        SELECT ccr.client_id AS grp, COUNT(*) AS n
        FROM client_company_relationships ccr
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.client_id IN ({_placeholders(query.client_ids)})
          AND {sql_active_ccr(conn)}
          AND {sql_active_company(conn)}
          AND lower(trim(ccr.status)) = 'hot prospect'
          {camp_sql}
          {user_sql}
        GROUP BY ccr.client_id
    """
    params: list[Any] = [*query.client_ids, *camp_params, *user_params]
    out: dict[int, int] = {}
    for row in conn.execute(sql, params).fetchall():
        out[int(row["grp"] or 0)] = int(row["n"] or 0)
    return out


def _get(mapping: dict[int, int], key: int) -> int:
    return int(mapping.get(key, 0))


def _sort_rows(
    rows: list[dict[str, Any]],
    *,
    sort_by: str,
    sort_dir: str,
    allowed: tuple[str, ...],
    limit: int,
    offset: int,
) -> tuple[int, list[dict[str, Any]]]:
    key = sort_by if sort_by in allowed else allowed[0]
    reverse = sort_dir == "desc"

    def sort_key(row: dict[str, Any]):
        val = row.get(key)
        if val is None:
            return (1, 0.0, "")
        if isinstance(val, (int, float)):
            return (0, float(val), "")
        return (0, 0.0, str(val).lower())

    ordered = sorted(rows, key=sort_key, reverse=reverse)
    return len(ordered), ordered[offset : offset + limit]


def list_report_filters(
    *,
    client_id: int | None = None,
) -> ReportFiltersResponse:
    selected, client_ids = _scope_client_ids(client_id)
    start, end = current_month_range()
    reps: list[ReportFilterOption] = []
    campaigns: list[ReportFilterOption] = []
    outcomes: list[str] = []
    if not _db_exists() or not client_ids:
        return ReportFiltersResponse(
            client_id=selected,
            client_ids=client_ids,
            date_from=start,
            date_to=end,
            reps=reps,
            campaigns=campaigns,
            activity_types=list(ACTIVITY_TIMELINE_TYPES),
            outcome_types=outcomes,
            definitions=METRIC_DEFINITIONS,
        )
    ensure_campaigns_schema()
    with get_connection() as conn:
        seen_users: set[int] = set()
        ph = _placeholders(client_ids)
        for row in conn.execute(
            f"""
            SELECT DISTINCT u.id, u.full_name
            FROM users u
            LEFT JOIN user_client_assignments uca
              ON uca.user_id = u.id AND uca.active = 1 AND uca.client_id IN ({ph})
            WHERE u.active = 1
              AND (
                    uca.user_id IS NOT NULL
                 OR COALESCE(u.is_internal_northstar, 1) = 1
                 OR COALESCE(u.is_administrator, 0) = 1
              )
            ORDER BY u.full_name COLLATE NOCASE, u.id
            """,
            client_ids,
        ).fetchall():
            uid = int(row["id"])
            if uid in seen_users:
                continue
            seen_users.add(uid)
            name = _blank(row["full_name"])
            if name:
                reps.append(ReportFilterOption(id=uid, name=name))
        if _table_exists(conn, "client_campaigns"):
            for row in conn.execute(
                f"""
                SELECT c.id, c.campaign_name, c.client_id, cl.name AS client_name
                FROM client_campaigns c
                JOIN clients cl ON cl.id = c.client_id
                WHERE c.client_id IN ({ph})
                ORDER BY cl.name COLLATE NOCASE, c.campaign_name COLLATE NOCASE, c.id
                """,
                client_ids,
            ).fetchall():
                campaigns.append(
                    ReportFilterOption(
                        id=int(row["id"]),
                        name=_blank(row["campaign_name"]),
                        client_id=int(row["client_id"]),
                        client_name=_blank(row["client_name"]),
                    )
                )
        for row in conn.execute(
            f"""
            SELECT DISTINCT TRIM(a.outcome) AS outcome
            FROM activities a
            WHERE a.client_id IN ({ph})
              AND TRIM(COALESCE(a.outcome, '')) != ''
            ORDER BY outcome COLLATE NOCASE
            LIMIT 100
            """,
            client_ids,
        ).fetchall():
            text = _blank(row["outcome"])
            if text:
                outcomes.append(text)
    return ReportFiltersResponse(
        client_id=selected,
        client_ids=client_ids,
        date_from=start,
        date_to=end,
        reps=reps,
        campaigns=campaigns,
        activity_types=list(ACTIVITY_TIMELINE_TYPES),
        outcome_types=outcomes,
        definitions=METRIC_DEFINITIONS,
    )


def _client_rows(conn, query: ReportQuery) -> list[dict[str, Any]]:
    if not query.client_ids:
        return []
    clients = {
        int(r["id"]): {"name": _blank(r["name"]), "code": _blank(r["code"])}
        for r in conn.execute(
            f"SELECT id, name, code FROM clients WHERE id IN ({_placeholders(query.client_ids)})",
            query.client_ids,
        ).fetchall()
    }
    calls = (
        _grouped_activity_counts(conn, query, group="client", kinds=["Call"])
        if _include_activity_metric(query, "Call")
        else {}
    )
    scheduled = (
        _grouped_activity_counts(conn, query, group="client", kinds=["Follow-Up"])
        if _include_activity_metric(query, "Follow-Up")
        else {}
    )
    completed = (
        _grouped_activity_counts(
            conn,
            query,
            group="client",
            kinds=["Follow-Up"],
            date_kind="completed",
            extra_sql="AND COALESCE(a.follow_up_completed, 0) = 1",
        )
        if _include_activity_metric(query, "Follow-Up")
        else {}
    )
    appt_set = _grouped_appointment_counts(conn, query, group="client", date_column="created_at")
    appt_done = _grouped_appointment_counts(conn, query, group="client", date_column="completed_at")
    appt_cancel = _grouped_appointment_counts(conn, query, group="client", date_column="cancelled_at")
    opps = _grouped_opportunity_counts(conn, query, group="client")
    outcomes = _grouped_outcome_counts(conn, query, group="client")
    hot = _grouped_hot_counts(conn, query)
    rows: list[dict[str, Any]] = []
    for cid in query.client_ids:
        meta = clients.get(cid, {"name": f"Client {cid}", "code": ""})
        rows.append(
            {
                "client_id": cid,
                "client_name": meta["name"],
                "client_code": meta["code"],
                "calls": _get(calls, cid),
                "follow_ups_scheduled": _get(scheduled, cid),
                "follow_ups_completed": _get(completed, cid),
                "appointments_set": _get(appt_set, cid),
                "appointments_completed": _get(appt_done, cid),
                "appointments_cancelled": _get(appt_cancel, cid),
                "opportunities": _get(opps, cid),
                "outcomes": _get(outcomes, cid),
                "hot_prospects": _get(hot, cid),
            }
        )
    return rows


def _client_kpis(rows: list[dict[str, Any]]) -> list[ReportKpi]:
    keys = (
        "calls",
        "follow_ups_scheduled",
        "follow_ups_completed",
        "appointments_set",
        "appointments_completed",
        "appointments_cancelled",
        "opportunities",
        "outcomes",
        "hot_prospects",
    )
    labels = {
        "calls": "Calls logged",
        "follow_ups_scheduled": "Follow-ups scheduled",
        "follow_ups_completed": "Follow-ups completed",
        "appointments_set": "Appointments set",
        "appointments_completed": "Appointments completed",
        "appointments_cancelled": "Appointments cancelled",
        "opportunities": "Opportunities identified",
        "outcomes": "Outcomes",
        "hot_prospects": "Current Hot Prospects",
    }
    return [
        ReportKpi(
            key=k,
            label=labels[k],
            value=sum(int(r[k]) for r in rows),
            definition=METRIC_DEFINITIONS.get(k, ""),
        )
        for k in keys
    ]


def list_client_results(
    *,
    client_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    user_id: int | None = None,
    campaign_id: int | None = None,
    activity_type: str = "",
    outcome: str = "",
    sort_by: str = "client_name",
    sort_dir: str = "asc",
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> ReportClientResultsResponse:
    query = resolve_report_query(
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id,
        campaign_id=campaign_id,
        activity_type=activity_type,
        outcome=outcome,
    )
    page_size, page_offset = _page(limit, offset)
    direction = _sort_dir(sort_dir)
    if not _db_exists() or not query.client_ids:
        return ReportClientResultsResponse(
            client_id=query.selected_client_id,
            date_from=query.date_from,
            date_to=query.date_to,
            total=0,
            limit=page_size,
            offset=page_offset,
            sort_by=sort_by if sort_by in CLIENT_SORT else "client_name",
            sort_dir=direction,
            kpis=_client_kpis([]),
            items=[],
        )
    with get_connection() as conn:
        rows = _client_rows(conn, query)
    total, page = _sort_rows(
        rows,
        sort_by=sort_by,
        sort_dir=direction,
        allowed=CLIENT_SORT,
        limit=page_size,
        offset=page_offset,
    )
    return ReportClientResultsResponse(
        client_id=query.selected_client_id,
        date_from=query.date_from,
        date_to=query.date_to,
        total=total,
        limit=page_size,
        offset=page_offset,
        sort_by=sort_by if sort_by in CLIENT_SORT else "client_name",
        sort_dir=direction,
        kpis=_client_kpis(rows),
        items=[ReportClientResultsRow(**item) for item in page],
    )


def _team_users(conn, query: ReportQuery) -> list[tuple[int, str]]:
    ph = _placeholders(query.client_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT u.id, u.full_name
        FROM users u
        LEFT JOIN user_client_assignments uca
          ON uca.user_id = u.id AND uca.active = 1 AND uca.client_id IN ({ph})
        WHERE u.active = 1
          AND (
                uca.user_id IS NOT NULL
             OR COALESCE(u.is_internal_northstar, 1) = 1
             OR COALESCE(u.is_administrator, 0) = 1
          )
        ORDER BY u.full_name COLLATE NOCASE, u.id
        """,
        query.client_ids,
    ).fetchall()
    users = [(int(r["id"]), _blank(r["full_name"])) for r in rows if _blank(r["full_name"])]
    if query.user_id is not None:
        users = [item for item in users if item[0] == query.user_id]
        if not users:
            users = [(query.user_id, query.user_name)]
    return users


def _assigned_client_counts(conn, query: ReportQuery) -> dict[int, int]:
    if not query.client_ids:
        return {}
    out: dict[int, int] = {}
    for row in conn.execute(
        f"""
        SELECT user_id, COUNT(DISTINCT client_id) AS n
        FROM user_client_assignments
        WHERE active = 1 AND client_id IN ({_placeholders(query.client_ids)})
        GROUP BY user_id
        """,
        query.client_ids,
    ).fetchall():
        out[int(row["user_id"])] = int(row["n"] or 0)
    return out


def _overdue_counts(query: ReportQuery) -> dict[int, int]:
    out: dict[int, int] = {}
    if not query.client_ids:
        return out
    for cid in query.client_ids:
        for item in list_open_follow_up_tasks(client_id=cid):
            if _follow_up_bucket(item.due_date, item.due_time) != "overdue":
                continue
            uid = int(item.assigned_user_id or 0)
            if query.user_id is not None and uid != query.user_id:
                if not (
                    uid == 0
                    and query.user_name
                    and item.assigned_user.strip().lower() == query.user_name.lower()
                ):
                    continue
                uid = query.user_id
            out[uid] = out.get(uid, 0) + 1
    return out


def _team_rows(conn, query: ReportQuery) -> list[dict[str, Any]]:
    users = _team_users(conn, query)
    calls = (
        _grouped_activity_counts(conn, query, group="user", kinds=["Call"])
        if _include_activity_metric(query, "Call")
        else {}
    )
    notes = (
        _grouped_activity_counts(conn, query, group="user", kinds=["Note"])
        if _include_activity_metric(query, "Note")
        else {}
    )
    completed = (
        _grouped_activity_counts(
            conn,
            query,
            group="user",
            kinds=["Follow-Up"],
            date_kind="completed",
            extra_sql="AND COALESCE(a.follow_up_completed, 0) = 1",
        )
        if _include_activity_metric(query, "Follow-Up")
        else {}
    )
    appt_set = _grouped_appointment_counts(conn, query, group="user", date_column="created_at")
    appt_done = _grouped_appointment_counts(conn, query, group="user", date_column="completed_at")
    outcomes = _grouped_outcome_counts(conn, query, group="user")
    assigned = _assigned_client_counts(conn, query)
    overdue = _overdue_counts(query)
    rows: list[dict[str, Any]] = []
    for uid, name in users:
        rows.append(
            {
                "user_id": uid,
                "user_name": name,
                "assigned_clients": _get(assigned, uid),
                "calls": _get(calls, uid),
                "notes": _get(notes, uid),
                "follow_ups_completed": _get(completed, uid),
                "appointments_set": _get(appt_set, uid),
                "appointments_completed": _get(appt_done, uid),
                "outcomes": _get(outcomes, uid),
                "overdue_tasks": _get(overdue, uid),
            }
        )
    return rows


def _team_kpis(rows: list[dict[str, Any]]) -> list[ReportKpi]:
    labels = {
        "assigned_clients": "Assigned clients",
        "calls": "Calls",
        "notes": "Notes",
        "follow_ups_completed": "Follow-ups completed",
        "appointments_set": "Appointments set",
        "appointments_completed": "Appointments completed",
        "outcomes": "Outcomes",
        "overdue_tasks": "Tasks currently overdue",
    }
    return [
        ReportKpi(
            key=k,
            label=label,
            value=sum(int(r[k]) for r in rows),
            definition=METRIC_DEFINITIONS.get(k, ""),
        )
        for k, label in labels.items()
    ]


def list_team_performance(
    *,
    client_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    user_id: int | None = None,
    campaign_id: int | None = None,
    activity_type: str = "",
    outcome: str = "",
    sort_by: str = "user_name",
    sort_dir: str = "asc",
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> ReportTeamResponse:
    query = resolve_report_query(
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id,
        campaign_id=campaign_id,
        activity_type=activity_type,
        outcome=outcome,
    )
    page_size, page_offset = _page(limit, offset)
    direction = _sort_dir(sort_dir)
    empty = ReportTeamResponse(
        client_id=query.selected_client_id,
        date_from=query.date_from,
        date_to=query.date_to,
        total=0,
        limit=page_size,
        offset=page_offset,
        sort_by=sort_by if sort_by in TEAM_SORT else "user_name",
        sort_dir=direction,
        kpis=_team_kpis([]),
        items=[],
    )
    if not _db_exists() or not query.client_ids:
        return empty
    with get_connection() as conn:
        rows = _team_rows(conn, query)
    total, page = _sort_rows(
        rows,
        sort_by=sort_by,
        sort_dir=direction,
        allowed=TEAM_SORT,
        limit=page_size,
        offset=page_offset,
    )
    return ReportTeamResponse(
        client_id=query.selected_client_id,
        date_from=query.date_from,
        date_to=query.date_to,
        total=total,
        limit=page_size,
        offset=page_offset,
        sort_by=sort_by if sort_by in TEAM_SORT else "user_name",
        sort_dir=direction,
        kpis=_team_kpis(rows),
        items=[ReportTeamRow(**item) for item in page],
    )


def _campaign_ids_in_scope(conn, query: ReportQuery) -> list[dict[str, Any]]:
    if not query.client_ids or not _table_exists(conn, "client_campaigns"):
        return []
    params: list[Any] = list(query.client_ids)
    extra = ""
    if query.campaign_id is not None:
        extra = "AND c.id = ?"
        params.append(query.campaign_id)
    rows = conn.execute(
        f"""
        SELECT c.id, c.campaign_name, c.client_id, cl.name AS client_name
        FROM client_campaigns c
        JOIN clients cl ON cl.id = c.client_id
        WHERE c.client_id IN ({_placeholders(query.client_ids)})
          {extra}
        ORDER BY cl.name COLLATE NOCASE, c.campaign_name COLLATE NOCASE, c.id
        """,
        params,
    ).fetchall()
    return [
        {
            "campaign_id": int(r["id"]),
            "campaign_name": _blank(r["campaign_name"]),
            "client_id": int(r["client_id"]),
            "client_name": _blank(r["client_name"]),
        }
        for r in rows
    ]


def _campaign_member_counts(conn, campaign_ids: list[int], table: str) -> dict[int, int]:
    if not campaign_ids:
        return {}
    out: dict[int, int] = {}
    for row in conn.execute(
        f"""
        SELECT campaign_id, COUNT(*) AS n
        FROM {table}
        WHERE campaign_id IN ({_placeholders(campaign_ids)})
        GROUP BY campaign_id
        """,
        campaign_ids,
    ).fetchall():
        out[int(row["campaign_id"])] = int(row["n"] or 0)
    return out


def _campaign_attributed_activity_counts(
    conn,
    query: ReportQuery,
    campaign_ids: list[int],
    *,
    kinds: Iterable[str] | None = None,
    require_outcome: bool = False,
) -> dict[int, int]:
    if not campaign_ids or not query.client_ids:
        return {}
    date_expr = activity_date_sql("a")
    user_sql, user_params = _activity_user_clause("a", query)
    type_sql, type_params = _type_clause("a", query, kinds=kinds)
    outcome_sql = "AND TRIM(COALESCE(a.outcome, '')) != ''" if require_outcome and not query.outcome else ""
    act_ts = "replace(replace(trim(COALESCE(NULLIF(TRIM(a.activity_at), ''), a.created_at)), 'T', ' '), 'Z', '')"
    cc_ts = "replace(replace(trim(COALESCE(cc.created_at, '')), 'T', ' '), 'Z', '')"
    ct_ts = "replace(replace(trim(COALESCE(ct.created_at, '')), 'T', ' '), 'Z', '')"
    sql = f"""
        SELECT camp_id AS grp, COUNT(*) AS n
        FROM (
            SELECT a.activity_id, cc.campaign_id AS camp_id
            FROM activities a
            JOIN campaign_companies cc
              ON cc.client_id = a.client_id AND cc.company_id = a.company_id
            WHERE a.client_id IN ({_placeholders(query.client_ids)})
              AND cc.campaign_id IN ({_placeholders(campaign_ids)})
              AND {date_expr} >= ?
              AND {date_expr} <= ?
              AND {act_ts} > {cc_ts}
              {user_sql}
              {type_sql}
              {outcome_sql}
            UNION
            SELECT a.activity_id, ct.campaign_id AS camp_id
            FROM activities a
            JOIN campaign_contacts ct
              ON ct.client_id = a.client_id AND ct.contact_id = a.contact_id
            WHERE a.client_id IN ({_placeholders(query.client_ids)})
              AND ct.campaign_id IN ({_placeholders(campaign_ids)})
              AND a.contact_id IS NOT NULL
              AND {date_expr} >= ?
              AND {date_expr} <= ?
              AND {act_ts} > {ct_ts}
              {user_sql}
              {type_sql}
              {outcome_sql}
        ) AS attributed
        GROUP BY camp_id
    """
    params: list[Any] = [
        *query.client_ids,
        *campaign_ids,
        query.date_from,
        query.date_to,
        *user_params,
        *type_params,
        *query.client_ids,
        *campaign_ids,
        query.date_from,
        query.date_to,
        *user_params,
        *type_params,
    ]
    out: dict[int, int] = {}
    for row in conn.execute(sql, params).fetchall():
        out[int(row["grp"] or 0)] = int(row["n"] or 0)
    return out


def _campaign_attributed_appointment_counts(
    conn,
    query: ReportQuery,
    campaign_ids: list[int],
) -> dict[int, int]:
    if not campaign_ids or not query.client_ids or not _table_exists(conn, "appointments"):
        return {}
    if not _include_appointments(query):
        return {}
    date_expr = appointment_date_sql("ap", "created_at")
    user_sql, user_params = _appointment_user_clause("ap", query)
    created = (
        "CASE WHEN replace(replace(trim(COALESCE(ap.created_at, '')), 'T', ' '), 'Z', '') != '' "
        "THEN replace(replace(trim(COALESCE(ap.created_at, '')), 'T', ' '), 'Z', '') "
        "ELSE replace(replace(trim(COALESCE(ap.appointment_date, '')), 'T', ' '), 'Z', '') END"
    )
    cc_ts = "replace(replace(trim(COALESCE(cc.created_at, '')), 'T', ' '), 'Z', '')"
    ct_ts = "replace(replace(trim(COALESCE(ct.created_at, '')), 'T', ' '), 'Z', '')"
    outcome_sql = ""
    outcome_params: list[Any] = []
    if query.outcome:
        outcome_sql = "AND lower(trim(COALESCE(ap.outcome, ''))) = ?"
        outcome_params = [query.outcome.lower()]
    sql = f"""
        SELECT camp_id AS grp, COUNT(*) AS n
        FROM (
            SELECT ap.id AS appt_id, cc.campaign_id AS camp_id
            FROM appointments ap
            JOIN campaign_companies cc
              ON cc.client_id = ap.client_id AND cc.company_id = ap.company_id
            WHERE ap.client_id IN ({_placeholders(query.client_ids)})
              AND cc.campaign_id IN ({_placeholders(campaign_ids)})
              AND {date_expr} >= ?
              AND {date_expr} <= ?
              AND {created} > {cc_ts}
              {user_sql}
              {outcome_sql}
            UNION
            SELECT ap.id AS appt_id, ct.campaign_id AS camp_id
            FROM appointments ap
            JOIN campaign_contacts ct
              ON ct.client_id = ap.client_id AND ct.contact_id = ap.contact_id
            WHERE ap.client_id IN ({_placeholders(query.client_ids)})
              AND ct.campaign_id IN ({_placeholders(campaign_ids)})
              AND ap.contact_id IS NOT NULL
              AND {date_expr} >= ?
              AND {date_expr} <= ?
              AND {created} > {ct_ts}
              {user_sql}
              {outcome_sql}
        ) AS attributed
        GROUP BY camp_id
    """
    params: list[Any] = [
        *query.client_ids,
        *campaign_ids,
        query.date_from,
        query.date_to,
        *user_params,
        *outcome_params,
        *query.client_ids,
        *campaign_ids,
        query.date_from,
        query.date_to,
        *user_params,
        *outcome_params,
    ]
    out: dict[int, int] = {}
    for row in conn.execute(sql, params).fetchall():
        out[int(row["grp"] or 0)] = int(row["n"] or 0)
    return out


def _campaign_rows(conn, query: ReportQuery) -> list[dict[str, Any]]:
    campaigns = _campaign_ids_in_scope(conn, query)
    ids = [int(c["campaign_id"]) for c in campaigns]
    companies = _campaign_member_counts(conn, ids, "campaign_companies")
    contacts = _campaign_member_counts(conn, ids, "campaign_contacts")
    calls = (
        _campaign_attributed_activity_counts(conn, query, ids, kinds=["Call"])
        if _include_activity_metric(query, "Call")
        else {}
    )
    follow_ups = (
        _campaign_attributed_activity_counts(conn, query, ids, kinds=["Follow-Up"])
        if _include_activity_metric(query, "Follow-Up")
        else {}
    )
    outcomes = _campaign_attributed_activity_counts(
        conn, query, ids, require_outcome=True
    )
    appointments = _campaign_attributed_appointment_counts(conn, query, ids)
    rows: list[dict[str, Any]] = []
    for camp in campaigns:
        cid = int(camp["campaign_id"])
        company_n = _get(companies, cid)
        call_n = _get(calls, cid)
        appt_n = _get(appointments, cid)
        outcome_n = _get(outcomes, cid)
        rows.append(
            {
                **camp,
                "companies": company_n,
                "contacts": _get(contacts, cid),
                "calls": call_n,
                "follow_ups": _get(follow_ups, cid),
                "appointments": appt_n,
                "outcomes": outcome_n,
                "call_to_appointment_pct": _pct(appt_n, company_n),
                "outcome_to_call_pct": _pct(outcome_n, call_n),
            }
        )
    return rows


def _campaign_kpis(rows: list[dict[str, Any]]) -> list[ReportKpi]:
    labels = {
        "companies": "Companies",
        "contacts": "Contacts",
        "calls": "Calls",
        "follow_ups": "Follow-ups",
        "appointments": "Appointments",
        "outcomes": "Outcomes",
    }
    kpis = [
        ReportKpi(
            key=k,
            label=label,
            value=sum(int(r[k]) for r in rows),
            definition=METRIC_DEFINITIONS.get(k, ""),
        )
        for k, label in labels.items()
    ]
    companies = sum(int(r["companies"]) for r in rows)
    calls = sum(int(r["calls"]) for r in rows)
    appts = sum(int(r["appointments"]) for r in rows)
    outcomes = sum(int(r["outcomes"]) for r in rows)
    kpis.append(
        ReportKpi(
            key="call_to_appointment_pct",
            label="Appt / companies %",
            value=_pct(appts, companies),
            definition=METRIC_DEFINITIONS["call_to_appointment_pct"],
        )
    )
    kpis.append(
        ReportKpi(
            key="outcome_to_call_pct",
            label="Outcomes / calls %",
            value=_pct(outcomes, calls),
            definition=METRIC_DEFINITIONS["outcome_to_call_pct"],
        )
    )
    return kpis


def list_campaign_performance(
    *,
    client_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    user_id: int | None = None,
    campaign_id: int | None = None,
    activity_type: str = "",
    outcome: str = "",
    sort_by: str = "campaign_name",
    sort_dir: str = "asc",
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> ReportCampaignResponse:
    query = resolve_report_query(
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id,
        campaign_id=campaign_id,
        activity_type=activity_type,
        outcome=outcome,
    )
    page_size, page_offset = _page(limit, offset)
    direction = _sort_dir(sort_dir)
    empty = ReportCampaignResponse(
        client_id=query.selected_client_id,
        date_from=query.date_from,
        date_to=query.date_to,
        total=0,
        limit=page_size,
        offset=page_offset,
        sort_by=sort_by if sort_by in CAMPAIGN_SORT else "campaign_name",
        sort_dir=direction,
        kpis=_campaign_kpis([]),
        items=[],
    )
    if not _db_exists() or not query.client_ids:
        return empty
    ensure_campaigns_schema()
    with get_connection() as conn:
        rows = _campaign_rows(conn, query)
    total, page = _sort_rows(
        rows,
        sort_by=sort_by,
        sort_dir=direction,
        allowed=CAMPAIGN_SORT,
        limit=page_size,
        offset=page_offset,
    )
    return ReportCampaignResponse(
        client_id=query.selected_client_id,
        date_from=query.date_from,
        date_to=query.date_to,
        total=total,
        limit=page_size,
        offset=page_offset,
        sort_by=sort_by if sort_by in CAMPAIGN_SORT else "campaign_name",
        sort_dir=direction,
        kpis=_campaign_kpis(rows),
        items=[ReportCampaignRow(**item) for item in page],
    )


def _record(
    *,
    kind: str,
    record_id: int | None,
    client_id: int,
    client_name: str,
    company_id: int | None = None,
    company_name: str = "",
    external_record_no: str = "",
    contact_id: int | None = None,
    contact_name: str = "",
    occurred_at: str = "",
    record_type: str = "",
    user_name: str = "",
    outcome: str = "",
    notes: str = "",
    status: str = "",
) -> ReportRecordRow:
    return ReportRecordRow(
        record_key=f"{kind}:{record_id or 0}:{client_id}:{company_id or 0}:{contact_id or 0}:{_blank(user_name)}",
        record_kind=kind,
        record_id=record_id,
        client_id=client_id,
        client_name=client_name,
        company_id=company_id,
        company_name=company_name,
        external_record_no=external_record_no,
        contact_id=contact_id,
        contact_name=contact_name,
        occurred_at=occurred_at,
        record_type=record_type,
        user_name=user_name,
        outcome=outcome,
        notes=notes,
        status=status,
    )


def _client_name_map(conn, client_ids: list[int]) -> dict[int, str]:
    if not client_ids:
        return {}
    return {
        int(r["id"]): _blank(r["name"])
        for r in conn.execute(
            f"SELECT id, name FROM clients WHERE id IN ({_placeholders(client_ids)})",
            client_ids,
        ).fetchall()
    }


def _scoped_ids(query: ReportQuery, row_client_id: int | None) -> list[int]:
    if row_client_id and int(row_client_id) > 0:
        cid = int(row_client_id)
        if cid not in query.client_ids:
            raise PermissionError("Not authorized for this client.")
        return [cid]
    return list(query.client_ids)


def _list_activity_records(
    conn,
    query: ReportQuery,
    *,
    client_ids: list[int],
    kinds: Iterable[str] | None,
    date_kind: Literal["activity", "completed"] = "activity",
    extra_sql: str = "",
    extra_params: list[Any] | None = None,
    require_outcome: bool = False,
    limit: int,
    offset: int,
    campaign_id: int | None = None,
) -> tuple[int, list[ReportRecordRow]]:
    if not client_ids:
        return 0, []
    names = _client_name_map(conn, client_ids)
    date_expr = updated_date_sql("a") if date_kind == "completed" else activity_date_sql("a")
    scoped = ReportQuery(**{**query.__dict__, "client_ids": client_ids, "campaign_id": campaign_id if campaign_id is not None else query.campaign_id})
    user_sql, user_params = _activity_user_clause("a", scoped)
    camp_sql, camp_params = _campaign_activity_clause("a", scoped)
    type_sql, type_params = _type_clause("a", scoped, kinds=kinds)
    outcome_sql = "AND TRIM(COALESCE(a.outcome, '')) != ''" if require_outcome and not scoped.outcome else ""
    where = f"""
        a.client_id IN ({_placeholders(client_ids)})
          AND {date_expr} >= ?
          AND {date_expr} <= ?
          {user_sql}
          {camp_sql}
          {type_sql}
          {outcome_sql}
          {extra_sql}
    """
    params: list[Any] = [
        *client_ids,
        scoped.date_from,
        scoped.date_to,
        *user_params,
        *camp_params,
        *type_params,
        *(extra_params or []),
    ]
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM activities a WHERE {where}", params).fetchone()["n"]
        or 0
    )
    rows = conn.execute(
        f"""
        SELECT
            a.activity_id,
            a.client_id,
            a.company_id,
            COALESCE(co.company_name, '') AS company_name,
            COALESCE(NULLIF(TRIM(a.external_record_no), ''), co.external_record_no, '') AS external_record_no,
            a.contact_id,
            TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, '')) AS contact_name,
            COALESCE(NULLIF(TRIM(a.activity_at), ''), a.created_at, '') AS occurred_at,
            a.activity_type,
            COALESCE(NULLIF(TRIM(u.full_name), ''), NULLIF(TRIM(a.assigned_user), ''), a.created_by, '') AS user_name,
            COALESCE(a.outcome, '') AS outcome,
            COALESCE(a.notes, '') AS notes,
            COALESCE(ccr.status, '') AS status
        FROM activities a
        JOIN companies co ON co.id = a.company_id
        LEFT JOIN contacts ct ON ct.id = a.contact_id
        LEFT JOIN users u ON u.id = a.user_id
        LEFT JOIN client_company_relationships ccr
          ON ccr.id = a.relationship_id AND ccr.client_id = a.client_id
        WHERE {where}
        ORDER BY occurred_at DESC, a.activity_id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    items = [
        _record(
            kind="activity",
            record_id=int(r["activity_id"]),
            client_id=int(r["client_id"]),
            client_name=names.get(int(r["client_id"]), ""),
            company_id=int(r["company_id"]) if r["company_id"] is not None else None,
            company_name=_blank(r["company_name"]),
            external_record_no=_blank(r["external_record_no"]),
            contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
            contact_name=_blank(r["contact_name"]),
            occurred_at=_blank(r["occurred_at"]),
            record_type=_blank(r["activity_type"]),
            user_name=_blank(r["user_name"]),
            outcome=_blank(r["outcome"]),
            notes=_blank(r["notes"]),
            status=_blank(r["status"]),
        )
        for r in rows
    ]
    return total, items


def _list_appointment_records(
    conn,
    query: ReportQuery,
    *,
    client_ids: list[int],
    date_column: str,
    limit: int,
    offset: int,
    campaign_id: int | None = None,
) -> tuple[int, list[ReportRecordRow]]:
    if not client_ids or not _table_exists(conn, "appointments"):
        return 0, []
    names = _client_name_map(conn, client_ids)
    scoped = ReportQuery(**{**query.__dict__, "client_ids": client_ids, "campaign_id": campaign_id if campaign_id is not None else query.campaign_id})
    date_expr = appointment_date_sql("ap", date_column)
    user_sql, user_params = _appointment_user_clause("ap", scoped)
    camp_sql, camp_params = _campaign_appt_clause("ap", scoped)
    outcome_sql = ""
    outcome_params: list[Any] = []
    if scoped.outcome:
        outcome_sql = "AND lower(trim(COALESCE(ap.outcome, ''))) = ?"
        outcome_params = [scoped.outcome.lower()]
    where = f"""
        ap.client_id IN ({_placeholders(client_ids)})
          AND {date_expr} >= ?
          AND {date_expr} <= ?
          AND TRIM(COALESCE(ap.{date_column}, '')) != ''
          {user_sql}
          {camp_sql}
          {outcome_sql}
    """
    params: list[Any] = [
        *client_ids,
        scoped.date_from,
        scoped.date_to,
        *user_params,
        *camp_params,
        *outcome_params,
    ]
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM appointments ap WHERE {where}", params).fetchone()["n"]
        or 0
    )
    rows = conn.execute(
        f"""
        SELECT
            ap.id,
            ap.client_id,
            ap.company_id,
            COALESCE(co.company_name, '') AS company_name,
            COALESCE(co.external_record_no, '') AS external_record_no,
            ap.contact_id,
            TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, '')) AS contact_name,
            COALESCE(ap.{date_column}, ap.created_at, '') AS occurred_at,
            ap.status,
            COALESCE(u.full_name, ap.created_by, '') AS user_name,
            COALESCE(ap.outcome, '') AS outcome,
            COALESCE(ap.notes, '') AS notes
        FROM appointments ap
        JOIN companies co ON co.id = ap.company_id
        LEFT JOIN contacts ct ON ct.id = ap.contact_id
        LEFT JOIN users u ON u.id = ap.revenue_specialist_user_id
        WHERE {where}
        ORDER BY occurred_at DESC, ap.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    label = {
        "created_at": "Appointment set",
        "completed_at": "Appointment completed",
        "cancelled_at": "Appointment cancelled",
    }.get(date_column, "Appointment")
    items = [
        _record(
            kind="appointment",
            record_id=int(r["id"]),
            client_id=int(r["client_id"]),
            client_name=names.get(int(r["client_id"]), ""),
            company_id=int(r["company_id"]) if r["company_id"] is not None else None,
            company_name=_blank(r["company_name"]),
            external_record_no=_blank(r["external_record_no"]),
            contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
            contact_name=_blank(r["contact_name"]),
            occurred_at=_blank(r["occurred_at"]),
            record_type=label,
            user_name=_blank(r["user_name"]),
            outcome=_blank(r["outcome"]),
            notes=_blank(r["notes"]),
            status=_blank(r["status"]),
        )
        for r in rows
    ]
    return total, items


def list_report_records(
    *,
    section: str,
    metric: str,
    client_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    user_id: int | None = None,
    campaign_id: int | None = None,
    activity_type: str = "",
    outcome: str = "",
    row_client_id: int | None = None,
    row_user_id: int | None = None,
    row_campaign_id: int | None = None,
    limit: int = PAGE_SIZE,
    offset: int = 0,
    export_cap: int | None = None,
) -> ReportRecordsResponse:
    query = resolve_report_query(
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id if row_user_id is None else row_user_id,
        campaign_id=campaign_id if row_campaign_id is None else row_campaign_id,
        activity_type=activity_type,
        outcome=outcome,
    )
    page_size, page_offset = _page(limit, offset, cap=export_cap or 200)
    metric_key = _blank(metric).lower().replace(" ", "_")
    section_key = _blank(section).lower().replace(" ", "-")
    empty = ReportRecordsResponse(
        section=section_key,
        metric=metric_key,
        total=0,
        limit=page_size,
        offset=page_offset,
        items=[],
    )
    if not _db_exists() or not query.client_ids:
        return empty
    client_ids = _scoped_ids(query, row_client_id)
    camp = query.campaign_id
    with get_connection() as conn:
        names = _client_name_map(conn, client_ids)
        if section_key == "campaign-performance" and camp is None:
            query = ReportQuery(
                **{
                    **query.__dict__,
                    "campaign_ids": [
                        int(c["campaign_id"]) for c in _campaign_ids_in_scope(conn, query)
                    ],
                }
            )
        if metric_key in {"calls"}:
            total, items = _list_activity_records(
                conn, query, client_ids=client_ids, kinds=["Call"],
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"follow_ups_scheduled", "follow_ups"}:
            total, items = _list_activity_records(
                conn, query, client_ids=client_ids, kinds=["Follow-Up"],
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"follow_ups_completed"}:
            total, items = _list_activity_records(
                conn, query, client_ids=client_ids, kinds=["Follow-Up"],
                date_kind="completed",
                extra_sql="AND COALESCE(a.follow_up_completed, 0) = 1",
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"notes"}:
            total, items = _list_activity_records(
                conn, query, client_ids=client_ids, kinds=["Note"],
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"outcomes"}:
            total, items = _list_activity_records(
                conn, query, client_ids=client_ids, kinds=None, require_outcome=True,
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"appointments_set", "appointments"}:
            total, items = _list_appointment_records(
                conn, query, client_ids=client_ids, date_column="created_at",
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"appointments_completed"}:
            total, items = _list_appointment_records(
                conn, query, client_ids=client_ids, date_column="completed_at",
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"appointments_cancelled"}:
            total, items = _list_appointment_records(
                conn, query, client_ids=client_ids, date_column="cancelled_at",
                limit=page_size, offset=page_offset, campaign_id=camp,
            )
        elif metric_key in {"opportunities"}:
            if not _table_exists(conn, "opportunity_assignments"):
                return empty
            date_expr = "substr(replace(replace(trim(COALESCE(oa.created_at, '')), 'T', ' '), 'Z', ''), 1, 10)"
            camp_sql, camp_params = "", []
            if camp is not None:
                camp_sql = """
                    AND (
                        oa.target_campaign_id = ?
                        OR EXISTS (
                            SELECT 1 FROM campaign_companies cc
                            WHERE cc.campaign_id = ?
                              AND cc.client_id = oa.target_client_id
                              AND cc.company_id = oa.company_id
                        )
                    )
                """
                camp_params = [camp, camp]
            where = f"""
                oa.target_client_id IN ({_placeholders(client_ids)})
                  AND {date_expr} >= ? AND {date_expr} <= ?
                  {camp_sql}
            """
            params = [*client_ids, query.date_from, query.date_to, *camp_params]
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM opportunity_assignments oa WHERE {where}", params).fetchone()["n"] or 0)
            rows = conn.execute(
                f"""
                SELECT oa.id, oa.target_client_id AS client_id, oa.company_id,
                       COALESCE(co.company_name, '') AS company_name,
                       COALESCE(co.external_record_no, '') AS external_record_no,
                       oa.created_at, COALESCE(oa.originated_from, '') AS originated_from,
                       COALESCE(oa.source_summary, '') AS source_summary
                FROM opportunity_assignments oa
                JOIN companies co ON co.id = oa.company_id
                WHERE {where}
                ORDER BY oa.created_at DESC, oa.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, page_offset],
            ).fetchall()
            items = [
                _record(
                    kind="opportunity",
                    record_id=int(r["id"]) if r["id"] is not None else None,
                    client_id=int(r["client_id"]),
                    client_name=names.get(int(r["client_id"]), ""),
                    company_id=int(r["company_id"]),
                    company_name=_blank(r["company_name"]),
                    external_record_no=_blank(r["external_record_no"]),
                    occurred_at=_blank(r["created_at"]),
                    record_type="Opportunity identified",
                    notes=_blank(r["source_summary"]) or _blank(r["originated_from"]),
                )
                for r in rows
            ]
        elif metric_key in {"hot_prospects"}:
            camp_sql, camp_params = "", []
            if camp is not None:
                camp_sql = """
                    AND EXISTS (
                        SELECT 1 FROM campaign_companies cc
                        WHERE cc.campaign_id = ? AND cc.client_id = ccr.client_id AND cc.company_id = ccr.company_id
                    )
                """
                camp_params = [camp]
            user_sql, user_params = "", []
            if query.user_id is not None:
                user_sql = "AND ccr.assigned_user_id = ?"
                user_params = [query.user_id]
            where = f"""
                ccr.client_id IN ({_placeholders(client_ids)})
                  AND {sql_active_ccr(conn)}
                  AND {sql_active_company(conn)}
                  AND lower(trim(ccr.status)) = 'hot prospect'
                  {camp_sql} {user_sql}
            """
            params = [*client_ids, *camp_params, *user_params]
            # COUNT must JOIN companies: sql_active_company() aliases archived_at as co.archived_at.
            from_hot = """
                FROM client_company_relationships ccr
                JOIN companies co ON co.id = ccr.company_id
            """
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) AS n {from_hot} WHERE {where}",
                    params,
                ).fetchone()["n"]
                or 0
            )
            rows = conn.execute(
                f"""
                SELECT ccr.id, ccr.client_id, ccr.company_id, ccr.status,
                       COALESCE(co.company_name, '') AS company_name,
                       COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '') AS external_record_no,
                       COALESCE(u.full_name, '') AS user_name,
                       COALESCE(ccr.updated_at, '') AS occurred_at
                {from_hot}
                LEFT JOIN users u ON u.id = ccr.assigned_user_id
                WHERE {where}
                ORDER BY co.company_name COLLATE NOCASE, ccr.id
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, page_offset],
            ).fetchall()
            items = [
                _record(
                    kind="company",
                    record_id=int(r["id"]),
                    client_id=int(r["client_id"]),
                    client_name=names.get(int(r["client_id"]), ""),
                    company_id=int(r["company_id"]),
                    company_name=_blank(r["company_name"]),
                    external_record_no=_blank(r["external_record_no"]),
                    occurred_at=_blank(r["occurred_at"]),
                    record_type="Hot Prospect",
                    user_name=_blank(r["user_name"]),
                    status=_blank(r["status"]),
                )
                for r in rows
            ]
        elif metric_key in {"overdue_tasks"}:
            items = []
            for cid in client_ids:
                for item in list_open_follow_up_tasks(client_id=cid):
                    if _follow_up_bucket(item.due_date, item.due_time) != "overdue":
                        continue
                    uid = int(item.assigned_user_id or 0)
                    if query.user_id is not None and uid != query.user_id:
                        if not (
                            query.user_name
                            and item.assigned_user.strip().lower() == query.user_name.lower()
                        ):
                            continue
                    items.append(
                        _record(
                            kind="task",
                            record_id=item.source_id,
                            client_id=item.client_id,
                            client_name=names.get(item.client_id, ""),
                            company_id=item.company_id,
                            company_name=item.company_name,
                            external_record_no=item.external_record_no,
                            contact_id=item.contact_id,
                            occurred_at=f"{item.due_date} {item.due_time}".strip(),
                            record_type="Overdue follow-up",
                            user_name=item.assigned_user,
                            status=item.completion_status,
                        )
                    )
            items.sort(key=lambda r: (r.occurred_at, r.company_name.lower()))
            total = len(items)
            items = items[page_offset : page_offset + page_size]
        elif metric_key in {"companies", "contacts"}:
            table = "campaign_companies" if metric_key == "companies" else "campaign_contacts"
            camp_ids = _campaign_ids_filter(query)
            if camp_ids is None:
                camp_ids = [int(c["campaign_id"]) for c in _campaign_ids_in_scope(conn, query)]
            if not camp_ids:
                return empty
            join = """
                JOIN companies co ON co.id = m.company_id
                LEFT JOIN client_company_relationships ccr
                  ON ccr.id = (
                    SELECT ccr2.id FROM client_company_relationships ccr2
                    WHERE ccr2.client_id = m.client_id AND ccr2.company_id = m.company_id
                    ORDER BY ccr2.id
                    LIMIT 1
                  )
                LEFT JOIN users u ON u.id = ccr.assigned_user_id
            """
            extra_select = (
                "COALESCE(co.company_name, '') AS company_name, "
                "COALESCE(co.external_record_no, '') AS external_record_no, "
                "COALESCE(u.full_name, '') AS user_name, "
                "COALESCE(ccr.status, '') AS status, "
                "COALESCE(m.notes, '') AS notes"
            )
            contact_join = ""
            contact_select = "NULL AS contact_id, '' AS contact_name"
            if metric_key == "contacts":
                contact_join = "LEFT JOIN contacts ct ON ct.id = m.contact_id"
                contact_select = (
                    "m.contact_id AS contact_id, "
                    "TRIM(COALESCE(ct.first_name,'') || ' ' || COALESCE(ct.last_name,'')) AS contact_name"
                )
            where = f"m.campaign_id IN ({_placeholders(camp_ids)}) AND m.client_id IN ({_placeholders(client_ids)})"
            params = [*camp_ids, *client_ids]
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table} m WHERE {where}", params).fetchone()["n"] or 0)
            rows = conn.execute(
                f"""
                SELECT m.id, m.client_id, m.company_id, m.created_at,
                       {extra_select}, {contact_select}
                FROM {table} m
                {join}
                {contact_join}
                WHERE {where}
                ORDER BY company_name COLLATE NOCASE, m.id
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, page_offset],
            ).fetchall()
            items = [
                _record(
                    kind="contact" if metric_key == "contacts" else "company",
                    record_id=int(r["id"]),
                    client_id=int(r["client_id"]),
                    client_name=names.get(int(r["client_id"]), ""),
                    company_id=int(r["company_id"]) if r["company_id"] is not None else None,
                    company_name=_blank(r["company_name"]),
                    external_record_no=_blank(r["external_record_no"]),
                    contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
                    contact_name=_blank(r["contact_name"]) if "contact_name" in r.keys() else "",
                    occurred_at=_blank(r["created_at"]),
                    record_type="Campaign member",
                    user_name=_blank(r["user_name"]),
                    status=_blank(r["status"]),
                    notes=_blank(r["notes"]),
                )
                for r in rows
            ]
        elif metric_key in {"assigned_clients"}:
            user_sql = ""
            user_params: list[Any] = []
            if query.user_id is not None:
                user_sql = "AND uca.user_id = ?"
                user_params = [query.user_id]
            where = f"""
                uca.active = 1
                  AND uca.client_id IN ({_placeholders(client_ids)})
                  {user_sql}
            """
            params = [*client_ids, *user_params]
            rows = conn.execute(
                f"""
                SELECT cl.id, cl.name, uca.assigned_at, uca.user_id,
                       COALESCE(u.full_name, '') AS user_name
                FROM user_client_assignments uca
                JOIN clients cl ON cl.id = uca.client_id
                LEFT JOIN users u ON u.id = uca.user_id
                WHERE {where}
                ORDER BY COALESCE(u.full_name, '') COLLATE NOCASE, cl.name COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, page_offset],
            ).fetchall()
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS n FROM user_client_assignments uca
                    WHERE {where}
                    """,
                    params,
                ).fetchone()["n"]
                or 0
            )
            items = [
                _record(
                    kind="client",
                    record_id=int(r["id"]),
                    client_id=int(r["id"]),
                    client_name=_blank(r["name"]),
                    occurred_at=_blank(r["assigned_at"]),
                    record_type="Assigned client",
                    user_name=_blank(r["user_name"]) or query.user_name,
                )
                for r in rows
            ]
        else:
            raise ValueError(f"Unknown report metric '{metric}'.")
    return ReportRecordsResponse(
        section=section_key,
        metric=metric_key,
        total=total,
        limit=page_size,
        offset=page_offset,
        items=items,
    )


def _csv_header_lines(
    query: ReportQuery,
    section: str,
    metric: str = "",
    *,
    detailed: bool = False,
) -> list[str]:
    kind = {
        "client-results": "Client Results",
        "team-performance": "Team Performance",
        "campaign-performance": "Campaign Performance",
        "records": "Supporting records",
    }.get(section, section)
    if detailed and section != "records":
        kind = f"{kind} (Detailed)"
    lines = [
        f"# NorthStar Reports — {kind}",
        f"# Active Client: {query.client_label}",
        f"# Date range: {query.date_from} to {query.date_to}",
        f"# Assigned rep: {query.user_label}",
        f"# Campaign: {query.campaign_label}",
        f"# Activity type: {query.activity_type or 'All'}",
        f"# Outcome: {query.outcome or 'All'}",
    ]
    if metric:
        lines.append(f"# Metric: {metric}")
    lines.append("#")
    return lines


CSV_HEADINGS: dict[str, str] = {
    "client_name": "Client",
    "calls": "Calls Logged",
    "follow_ups_scheduled": "Follow-Ups Scheduled",
    "follow_ups_completed": "Follow-Ups Completed",
    "appointments_set": "Appointments Set",
    "appointments_completed": "Appointments Completed",
    "appointments_cancelled": "Appointments Cancelled",
    "opportunities": "Opportunities",
    "outcomes": "Outcomes",
    "hot_prospects": "Current Hot Prospects",
    "user_name": "Assigned Rep/User",
    "assigned_clients": "Assigned Clients",
    "notes": "Notes",
    "overdue_tasks": "Tasks Currently Overdue",
    "campaign_name": "Campaign",
    "companies": "Companies",
    "contacts": "Contacts",
    "follow_ups": "Follow-Ups",
    "appointments": "Appointments",
    "call_to_appointment_pct": "Appt / Companies",
    "outcome_to_call_pct": "Outcomes / Calls",
    "record_kind": "Record Kind",
    "record_id": "Record ID",
    "company_name": "Company",
    "external_record_no": "External Record No",
    "contact_name": "Contact",
    "occurred_at": "Date/Time",
    "record_type": "Activity Or Record Type",
    "outcome": "Outcome",
    "status": "Status",
}

TEAM_CSV_HEADINGS: dict[str, str] = {
    **CSV_HEADINGS,
    "calls": "Calls",
    "user_name": "Revenue Development Specialist",
}

CAMPAIGN_CSV_HEADINGS: dict[str, str] = {
    **CSV_HEADINGS,
    "calls": "Calls",
}

RECORDS_CSV_HEADINGS: dict[str, str] = {
    **CSV_HEADINGS,
    "user_name": "Assigned Rep/User",
}


def _csv_heading(field: str, labels: dict[str, str] | None = None) -> str:
    mapping = labels or CSV_HEADINGS
    if field in mapping:
        return mapping[field]
    return field.replace("_", " ").title()


def _write_csv(
    header_lines: list[str],
    fieldnames: list[str],
    rows: Iterable[dict[str, Any]],
    headings: dict[str, str] | None = None,
) -> str:
    buf = io.StringIO()
    for line in header_lines:
        buf.write(line + "\n")
    writer = csv.writer(buf)
    writer.writerow([_csv_heading(name, headings) for name in fieldnames])
    for row in rows:
        writer.writerow(["" if row.get(key) is None else row.get(key) for key in fieldnames])
    return buf.getvalue()


DETAIL_LIMIT = 10000

DETAILED_FIELDS = [
    "client_name",
    "company_name",
    "external_record_no",
    "contact_name",
    "user_name",
    "occurred_at",
    "record_type",
    "status_outcome",
    "campaign_name",
    "notes",
]

DETAILED_HEADINGS: dict[str, str] = {
    "client_name": "Client",
    "company_name": "Company Name",
    "external_record_no": "Company Record No.",
    "contact_name": "Contact Name",
    "user_name": "Assigned Rep/User",
    "occurred_at": "Date/Time",
    "record_type": "Activity Or Record Type",
    "status_outcome": "Status/Outcome",
    "campaign_name": "Campaign",
    "notes": "Notes",
}

SECTION_DETAIL_METRICS: dict[str, tuple[str, ...]] = {
    "client-results": (
        "calls",
        "follow_ups_scheduled",
        "follow_ups_completed",
        "appointments_set",
        "appointments_completed",
        "appointments_cancelled",
        "opportunities",
        "outcomes",
        "hot_prospects",
    ),
    "team-performance": (
        "calls",
        "notes",
        "follow_ups_completed",
        "appointments_set",
        "appointments_completed",
        "outcomes",
        "overdue_tasks",
    ),
    "campaign-performance": (
        "calls",
        "follow_ups",
        "appointments",
        "outcomes",
        "contacts",
        "companies",
    ),
}


def _record_identity(rec: ReportRecordRow) -> tuple[str, int, int]:
    return (rec.record_kind, int(rec.record_id or 0), int(rec.client_id or 0))


def _campaign_name_lookup(
    conn,
    query: ReportQuery,
) -> tuple[dict[tuple[int, int], list[str]], dict[tuple[int, int], list[str]]]:
    by_company: dict[tuple[int, int], list[str]] = {}
    by_contact: dict[tuple[int, int], list[str]] = {}
    if not query.client_ids or not _table_exists(conn, "client_campaigns"):
        return by_company, by_contact
    camp_ids = _campaign_ids_filter(query)
    extra = ""
    camp_params: list[Any] = []
    if camp_ids is not None:
        if not camp_ids:
            return by_company, by_contact
        extra = f"AND c.id IN ({_placeholders(camp_ids)})"
        camp_params = list(camp_ids)

    def _add(bucket: dict[tuple[int, int], list[str]], key: tuple[int, int], name: str) -> None:
        label = _blank(name)
        if not label:
            return
        names = bucket.setdefault(key, [])
        if label not in names:
            names.append(label)

    for row in conn.execute(
        f"""
        SELECT cc.client_id, cc.company_id, c.campaign_name
        FROM campaign_companies cc
        JOIN client_campaigns c ON c.id = cc.campaign_id
        WHERE cc.client_id IN ({_placeholders(query.client_ids)})
          {extra}
        ORDER BY c.campaign_name COLLATE NOCASE
        """,
        [*query.client_ids, *camp_params],
    ).fetchall():
        _add(by_company, (int(row["client_id"]), int(row["company_id"])), _blank(row["campaign_name"]))
    if _table_exists(conn, "campaign_contacts"):
        for row in conn.execute(
            f"""
            SELECT ct.client_id, ct.contact_id, c.campaign_name
            FROM campaign_contacts ct
            JOIN client_campaigns c ON c.id = ct.campaign_id
            WHERE ct.client_id IN ({_placeholders(query.client_ids)})
              AND ct.contact_id IS NOT NULL
              {extra}
            ORDER BY c.campaign_name COLLATE NOCASE
            """,
            [*query.client_ids, *camp_params],
        ).fetchall():
            _add(by_contact, (int(row["client_id"]), int(row["contact_id"])), _blank(row["campaign_name"]))
    return by_company, by_contact


def _campaigns_for_record(
    rec: ReportRecordRow,
    by_company: dict[tuple[int, int], list[str]],
    by_contact: dict[tuple[int, int], list[str]],
) -> str:
    names: list[str] = []
    seen: set[str] = set()
    sources: list[list[str]] = []
    if rec.company_id:
        sources.append(by_company.get((rec.client_id, rec.company_id), []))
    if rec.contact_id:
        sources.append(by_contact.get((rec.client_id, rec.contact_id), []))
    for group in sources:
        for name in group:
            if name not in seen:
                seen.add(name)
                names.append(name)
    return "; ".join(names)


def _detail_dict(rec: ReportRecordRow, campaign_name: str) -> dict[str, Any]:
    return {
        "client_name": rec.client_name,
        "company_name": rec.company_name,
        "external_record_no": rec.external_record_no,
        "contact_name": rec.contact_name,
        "user_name": rec.user_name,
        "occurred_at": rec.occurred_at,
        "record_type": rec.record_type,
        "status_outcome": _blank(rec.outcome) or _blank(rec.status),
        "campaign_name": campaign_name,
        "notes": rec.notes,
    }


def _detail_csv_rows(items: list[ReportRecordRow], query: ReportQuery) -> list[dict[str, Any]]:
    with get_connection() as conn:
        by_company, by_contact = _campaign_name_lookup(conn, query)
    return [
        _detail_dict(rec, _campaigns_for_record(rec, by_company, by_contact))
        for rec in items
    ]


def _drop_duplicate_campaign_companies(items: list[ReportRecordRow]) -> list[ReportRecordRow]:
    contact_companies = {
        (rec.client_id, rec.company_id)
        for rec in items
        if rec.record_kind == "contact" and rec.company_id
    }
    if not contact_companies:
        return items
    return [
        rec
        for rec in items
        if not (
            rec.record_type == "Campaign member"
            and rec.record_kind == "company"
            and rec.company_id
            and (rec.client_id, rec.company_id) in contact_companies
        )
    ]


def list_section_detail_records(
    *,
    section: str,
    client_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    user_id: int | None = None,
    campaign_id: int | None = None,
    activity_type: str = "",
    outcome: str = "",
    row_client_id: int | None = None,
    row_user_id: int | None = None,
    row_campaign_id: int | None = None,
) -> list[ReportRecordRow]:
    metrics = SECTION_DETAIL_METRICS.get(section)
    if not metrics:
        return []
    items: list[ReportRecordRow] = []
    seen: set[tuple[str, int, int]] = set()
    for metric in metrics:
        payload = list_report_records(
            section=section,
            metric=metric,
            client_id=client_id,
            date_from=date_from,
            date_to=date_to,
            user_id=user_id,
            campaign_id=campaign_id,
            activity_type=activity_type,
            outcome=outcome,
            row_client_id=row_client_id,
            row_user_id=row_user_id,
            row_campaign_id=row_campaign_id,
            limit=DETAIL_LIMIT,
            offset=0,
            export_cap=DETAIL_LIMIT,
        )
        for rec in payload.items:
            key = _record_identity(rec)
            if key in seen:
                continue
            seen.add(key)
            items.append(rec)
    items = _drop_duplicate_campaign_companies(items)
    items.sort(
        key=lambda rec: (
            rec.occurred_at or "",
            rec.client_name.lower(),
            rec.company_name.lower(),
            rec.record_kind,
            rec.record_id or 0,
        ),
        reverse=True,
    )
    return items


def export_report_csv(
    *,
    section: str,
    metric: str = "",
    client_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    user_id: int | None = None,
    campaign_id: int | None = None,
    activity_type: str = "",
    outcome: str = "",
    row_client_id: int | None = None,
    row_user_id: int | None = None,
    row_campaign_id: int | None = None,
    detail: bool = False,
) -> tuple[str, str]:
    """Return (filename, csv_text). Read-only."""
    query = resolve_report_query(
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id,
        campaign_id=campaign_id,
        activity_type=activity_type,
        outcome=outcome,
    )
    section_key = _blank(section).lower().replace(" ", "-") or "client-results"
    metric_key = _blank(metric).lower().replace(" ", "_")
    stamp = f"{query.date_from}_to_{query.date_to}"
    common = dict(
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        user_id=user_id,
        campaign_id=campaign_id,
        activity_type=activity_type,
        outcome=outcome,
        row_client_id=row_client_id,
        row_user_id=row_user_id,
        row_campaign_id=row_campaign_id,
    )
    if metric_key or detail:
        if metric_key:
            records = list_report_records(
                section=section_key,
                metric=metric_key,
                limit=DETAIL_LIMIT,
                offset=0,
                **common,
                export_cap=DETAIL_LIMIT,
            )
            items = list(records.items)
            filename = f"northstar-reports-{section_key}-{metric_key}-{stamp}.csv"
            header_section = "records"
        else:
            items = list_section_detail_records(section=section_key, **common)
            filename = f"northstar-reports-{section_key}-detailed-{stamp}.csv"
            header_section = section_key
        if section_key == "campaign-performance" and query.campaign_id is None:
            with get_connection() as conn:
                query = ReportQuery(
                    **{
                        **query.__dict__,
                        "campaign_ids": [
                            int(c["campaign_id"]) for c in _campaign_ids_in_scope(conn, query)
                        ],
                    }
                )
        rows = _detail_csv_rows(items, query)
        csv_text = _write_csv(
            _csv_header_lines(
                query,
                header_section,
                metric_key,
                detailed=bool(detail) and not metric_key,
            ),
            DETAILED_FIELDS,
            rows,
            DETAILED_HEADINGS,
        )
        return filename, csv_text
    if section_key == "team-performance":
        data = list_team_performance(
            client_id=client_id, date_from=date_from, date_to=date_to, user_id=user_id,
            campaign_id=campaign_id, activity_type=activity_type, outcome=outcome,
            sort_by="user_name", sort_dir="asc", limit=5000, offset=0,
        )
        fields = [
            "user_name", "assigned_clients", "calls", "notes", "follow_ups_completed",
            "appointments_set", "appointments_completed", "outcomes", "overdue_tasks",
        ]
        rows = [item.model_dump() for item in data.items]
        filename = f"northstar-reports-team-{stamp}.csv"
        return filename, _write_csv(
            _csv_header_lines(query, section_key), fields, rows, TEAM_CSV_HEADINGS
        )
    if section_key == "campaign-performance":
        data = list_campaign_performance(
            client_id=client_id, date_from=date_from, date_to=date_to, user_id=user_id,
            campaign_id=campaign_id, activity_type=activity_type, outcome=outcome,
            sort_by="campaign_name", sort_dir="asc", limit=5000, offset=0,
        )
        fields = [
            "campaign_name", "client_name", "companies", "contacts", "calls", "follow_ups",
            "appointments", "outcomes", "call_to_appointment_pct", "outcome_to_call_pct",
        ]
        rows = [item.model_dump() for item in data.items]
        filename = f"northstar-reports-campaigns-{stamp}.csv"
        return filename, _write_csv(
            _csv_header_lines(query, section_key), fields, rows, CAMPAIGN_CSV_HEADINGS
        )
    data = list_client_results(
        client_id=client_id, date_from=date_from, date_to=date_to, user_id=user_id,
        campaign_id=campaign_id, activity_type=activity_type, outcome=outcome,
        sort_by="client_name", sort_dir="asc", limit=5000, offset=0,
    )
    fields = [
        "client_name", "calls", "follow_ups_scheduled", "follow_ups_completed",
        "appointments_set", "appointments_completed", "appointments_cancelled",
        "opportunities", "outcomes", "hot_prospects",
    ]
    rows = [item.model_dump() for item in data.items]
    filename = f"northstar-reports-clients-{stamp}.csv"
    return filename, _write_csv(
        _csv_header_lines(query, "client-results"), fields, rows, CSV_HEADINGS
    )
