"""Operational Campaigns — client-scoped lists, membership, and results.

Uses client_campaigns (also the Client Setup ICP table) plus membership rows.
Shared companies/contacts can belong to many campaigns; master records are not copied.
"""

from __future__ import annotations

from datetime import datetime, timezone

from access import resolve_dashboard_client_ids, user_can_access_client
from data_steward import sql_active_ccr, sql_active_company, sql_active_contact
from staff_context import resolve_staff_actor
from staff_rbac import user_has_permission
from db import get_connection
from models import (
    CampaignActionResult,
    CampaignChoice,
    CampaignCompanyRow,
    CampaignContactRow,
    CampaignCreateRequest,
    CampaignListResponse,
    CampaignMemberAddRequest,
    CampaignMemberSearchResponse,
    CampaignRouteConfirmRequest,
    CampaignRouteDeferRequest,
    CampaignRouteSuggestion,
    CampaignSummary,
    CampaignUpdateRequest,
    CampaignWorkspace,
    UnassignedBulkAssignRequest,
    UnassignedBulkAssignResult,
    UnassignedOpportunity,
    UnassignedOpportunityList,
)

CAMPAIGN_STATUSES = ("Draft", "Active", "Paused", "Completed", "Archived")
PAGE_MAX = 200


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status_from_row(row) -> str:
    raw = ""
    if "status" in row.keys():
        raw = _blank(row["status"])
    if raw in CAMPAIGN_STATUSES:
        return raw
    if "is_active" in row.keys() and not int(row["is_active"] or 0):
        return "Paused"
    return "Active"


def _is_active_flag(status: str) -> int:
    return 0 if status in {"Completed", "Archived"} else 1


def ensure_campaigns_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaign_companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                company_id INTEGER NOT NULL,
                relationship_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (campaign_id, company_id),
                FOREIGN KEY (campaign_id) REFERENCES client_campaigns(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_campaign_companies_client
                ON campaign_companies(client_id, campaign_id);
            CREATE TABLE IF NOT EXISTS campaign_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                company_id INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (campaign_id, contact_id),
                FOREIGN KEY (campaign_id) REFERENCES client_campaigns(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_campaign_contacts_client
                ON campaign_contacts(client_id, campaign_id);
            CREATE TABLE IF NOT EXISTS campaign_unassigned (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                company_id INTEGER NOT NULL,
                contact_id INTEGER,
                relationship_id INTEGER,
                source TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (client_id, company_id),
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_campaign_unassigned_client
                ON campaign_unassigned(client_id, created_at DESC);
            """
        )
        existing = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(client_campaigns)").fetchall()
        }
        for name, declaration in (
            ("status", "TEXT NOT NULL DEFAULT 'Active'"),
            ("owner_user_id", "INTEGER"),
            ("owner_name", "TEXT NOT NULL DEFAULT ''"),
            ("start_date", "TEXT NOT NULL DEFAULT ''"),
            ("end_date", "TEXT NOT NULL DEFAULT ''"),
            ("category", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in existing:
                conn.execute(f"ALTER TABLE client_campaigns ADD COLUMN {name} {declaration}")
        oa_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
        ).fetchone()
        if oa_exists:
            oa_cols = {
                str(r[1]) for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
            }
            if "target_campaign_id" not in oa_cols:
                conn.execute(
                    "ALTER TABLE opportunity_assignments ADD COLUMN target_campaign_id INTEGER"
                )
        conn.execute(
            """
            UPDATE client_campaigns
            SET status = CASE
                  WHEN COALESCE(TRIM(status), '') = '' AND COALESCE(is_active, 1) = 0 THEN 'Paused'
                  WHEN COALESCE(TRIM(status), '') = '' THEN 'Active'
                  ELSE status
                END
            WHERE COALESCE(TRIM(status), '') = ''
            """
        )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _authorized_client_ids(
    selected_client_id: int | None,
    *,
    write: bool = False,
) -> list[int]:
    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    perm = "campaigns.manage" if write else "campaigns.view"
    cid = int(selected_client_id) if selected_client_id is not None and int(selected_client_id) > 0 else None
    if not user_has_permission(int(user.id), perm, client_id=cid):
        raise PermissionError("Not authorized for this client.")
    if cid is not None:
        if not user_can_access_client(user.id, cid) and not user.is_administrator:
            raise PermissionError("Not authorized for this client.")
        return resolve_dashboard_client_ids(user.id, selected_client_id=cid)
    return resolve_dashboard_client_ids(user.id, selected_client_id=None)


def _require_campaign(conn, campaign_id: int, allowed: list[int]):
    row = conn.execute(
        """
        SELECT c.*, cl.name AS client_name, cl.code AS client_code
        FROM client_campaigns c
        JOIN clients cl ON cl.id = c.client_id
        WHERE c.id = ?
        """,
        (int(campaign_id),),
    ).fetchone()
    if row is None:
        raise LookupError("Campaign not found.")
    if int(row["client_id"]) not in allowed:
        raise PermissionError("Not authorized for this campaign.")
    return row


def _ts(alias_col: str) -> str:
    return f"replace(replace(trim(COALESCE({alias_col}, '')), 'T', ' '), 'Z', '')"


def _member_activity_exists_sql(alias: str) -> str:
    """Count only activity that occurred after this client’s campaign assignment."""
    activity_ts = _ts(f"{alias}.activity_at")
    created_ts = _ts(f"{alias}.created_at")
    cc_ts = _ts("cc.created_at")
    ct_ts = _ts("ct.created_at")
    return f"""
        (
          EXISTS (
            SELECT 1 FROM campaign_companies cc
            WHERE cc.campaign_id = ?
              AND cc.client_id = {alias}.client_id
              AND cc.company_id = {alias}.company_id
              AND CASE WHEN {activity_ts} != '' THEN {activity_ts} ELSE {created_ts} END
                  > {cc_ts}
          )
          OR EXISTS (
            SELECT 1 FROM campaign_contacts ct
            WHERE ct.campaign_id = ?
              AND ct.client_id = {alias}.client_id
              AND ct.contact_id = {alias}.contact_id
              AND CASE WHEN {activity_ts} != '' THEN {activity_ts} ELSE {created_ts} END
                  > {ct_ts}
          )
        )
    """


def _stats_for_campaign(conn, campaign_id: int, client_id: int) -> dict[str, int]:
    company_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM campaign_companies cc
            JOIN companies co ON co.id = cc.company_id
            JOIN client_company_relationships ccr
              ON ccr.id = cc.relationship_id AND ccr.client_id = cc.client_id
             AND {sql_active_ccr(conn, "ccr")}
            WHERE cc.campaign_id = ? AND cc.client_id = ?
              AND {sql_active_company(conn, "co")}
            """,
            (campaign_id, client_id),
        ).fetchone()["n"]
    )
    contact_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM campaign_contacts m
            JOIN contacts ct ON ct.id = m.contact_id
            JOIN client_company_relationships ccr
              ON ccr.company_id = m.company_id AND ccr.client_id = m.client_id
             AND {sql_active_ccr(conn, "ccr")}
            WHERE m.campaign_id = ? AND m.client_id = ?
              AND {sql_active_contact(conn, "ct")}
            """,
            (campaign_id, client_id),
        ).fetchone()["n"]
    )
    calls = follow_ups = appointments = outcomes = 0
    if company_count or contact_count:
        member_sql = _member_activity_exists_sql("a")
        params = [client_id, campaign_id, campaign_id]
        calls = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM activities a
                WHERE a.client_id = ? AND {member_sql}
                  AND lower(trim(a.activity_type)) = 'call'
                """,
                params,
            ).fetchone()["n"]
        )
        follow_ups = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM activities a
                WHERE a.client_id = ? AND {member_sql}
                  AND lower(trim(a.activity_type)) LIKE '%follow%'
                """,
                params,
            ).fetchone()["n"]
        )
        outcomes = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM activities a
                WHERE a.client_id = ? AND {member_sql}
                  AND TRIM(COALESCE(a.outcome, '')) != ''
                """,
                params,
            ).fetchone()["n"]
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appointments'"
        ).fetchone():
            appointments = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM appointments ap
                    WHERE ap.client_id = ?
                      AND (
                        EXISTS (
                          SELECT 1 FROM campaign_companies cc
                          WHERE cc.campaign_id = ?
                            AND cc.client_id = ap.client_id
                            AND cc.company_id = ap.company_id
                            AND CASE
                                  WHEN replace(replace(trim(COALESCE(ap.created_at, '')), 'T', ' '), 'Z', '') != ''
                                  THEN replace(replace(trim(COALESCE(ap.created_at, '')), 'T', ' '), 'Z', '')
                                  ELSE replace(replace(trim(COALESCE(ap.appointment_date, '')), 'T', ' '), 'Z', '')
                                END
                                > replace(replace(trim(COALESCE(cc.created_at, '')), 'T', ' '), 'Z', '')
                        )
                        OR EXISTS (
                          SELECT 1 FROM campaign_contacts ct
                          WHERE ct.campaign_id = ?
                            AND ct.client_id = ap.client_id
                            AND ct.contact_id = ap.contact_id
                            AND CASE
                                  WHEN replace(replace(trim(COALESCE(ap.created_at, '')), 'T', ' '), 'Z', '') != ''
                                  THEN replace(replace(trim(COALESCE(ap.created_at, '')), 'T', ' '), 'Z', '')
                                  ELSE replace(replace(trim(COALESCE(ap.appointment_date, '')), 'T', ' '), 'Z', '')
                                END
                                > replace(replace(trim(COALESCE(ct.created_at, '')), 'T', ' '), 'Z', '')
                        )
                      )
                    """,
                    (client_id, campaign_id, campaign_id),
                ).fetchone()["n"]
            )
    return {
        "company_count": company_count,
        "contact_count": contact_count,
        "call_count": calls,
        "follow_up_count": follow_ups,
        "appointment_count": appointments,
        "outcome_count": outcomes,
    }


def _row_to_summary(conn, row) -> CampaignSummary:
    cid = int(row["id"])
    client_id = int(row["client_id"])
    stats = _stats_for_campaign(conn, cid, client_id)
    owner = _blank(row["owner_name"]) if "owner_name" in row.keys() else ""
    return CampaignSummary(
        campaign_id=cid,
        client_id=client_id,
        client_name=_blank(row["client_name"]),
        client_code=_blank(row["client_code"]),
        campaign_name=_blank(row["campaign_name"]),
        description=_blank(row["description"]),
        category=_blank(row["category"]) if "category" in row.keys() else "",
        owner_name=owner,
        owner_user_id=int(row["owner_user_id"])
        if "owner_user_id" in row.keys() and row["owner_user_id"] is not None
        else None,
        status=_status_from_row(row),
        start_date=_blank(row["start_date"]) if "start_date" in row.keys() else "",
        end_date=_blank(row["end_date"]) if "end_date" in row.keys() else "",
        company_count=stats["company_count"],
        contact_count=stats["contact_count"],
        call_count=stats["call_count"],
        follow_up_count=stats["follow_up_count"],
        appointment_count=stats["appointment_count"],
        outcome_count=stats["outcome_count"],
        created_at=_blank(row["created_at"]),
        updated_at=_blank(row["updated_at"]),
    )


def list_campaigns(
    *,
    client_id: int | None = None,
    status: str = "",
    owner: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> CampaignListResponse:
    selected = int(client_id) if client_id is not None else 0
    allowed = _authorized_client_ids(selected if selected > 0 else None)
    page_size = max(1, min(int(limit or 50), PAGE_MAX))
    page_offset = max(0, int(offset or 0))
    if not allowed:
        return CampaignListResponse(client_id=selected, limit=page_size, offset=page_offset)

    ensure_campaigns_schema()
    placeholders = ",".join("?" * len(allowed))
    where = [f"c.client_id IN ({placeholders})"]
    params: list[object] = list(allowed)
    wanted_status = _blank(status)
    if wanted_status:
        if wanted_status not in CAMPAIGN_STATUSES:
            raise ValueError(f"Unknown campaign status '{wanted_status}'.")
        where.append("COALESCE(NULLIF(TRIM(c.status), ''), CASE WHEN c.is_active = 0 THEN 'Paused' ELSE 'Active' END) = ?")
        params.append(wanted_status)
    owner_q = _blank(owner)
    if owner_q:
        where.append("lower(TRIM(c.owner_name)) = lower(?)")
        params.append(owner_q)
    category_q = _blank(category)
    if category_q:
        where.append("lower(TRIM(c.category)) = lower(?)")
        params.append(category_q)
    start = _blank(date_from)[:10]
    end = _blank(date_to)[:10]
    if start:
        where.append("substr(COALESCE(NULLIF(TRIM(c.start_date), ''), c.created_at), 1, 10) >= ?")
        params.append(start)
    if end:
        where.append("substr(COALESCE(NULLIF(TRIM(c.start_date), ''), c.created_at), 1, 10) <= ?")
        params.append(end)
    needle = _blank(q).lower()
    if needle:
        where.append(
            """
            (
                lower(c.campaign_name) LIKE ?
             OR lower(c.description) LIKE ?
             OR lower(c.category) LIKE ?
             OR lower(c.owner_name) LIKE ?
             OR lower(cl.name) LIKE ?
            )
            """
        )
        like = f"%{needle}%"
        params.extend([like] * 5)

    where_sql = " AND ".join(where)
    with get_connection() as conn:
        total = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM client_campaigns c
                JOIN clients cl ON cl.id = c.client_id
                WHERE {where_sql}
                """,
                params,
            ).fetchone()["n"]
        )
        rows = conn.execute(
            f"""
            SELECT c.*, cl.name AS client_name, cl.code AS client_code
            FROM client_campaigns c
            JOIN clients cl ON cl.id = c.client_id
            WHERE {where_sql}
            ORDER BY datetime(c.updated_at) DESC, c.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, page_offset],
        ).fetchall()
        items = [_row_to_summary(conn, r) for r in rows]
        status_rows = conn.execute(
            f"""
            SELECT DISTINCT COALESCE(NULLIF(TRIM(c.status), ''),
                   CASE WHEN c.is_active = 0 THEN 'Paused' ELSE 'Active' END) AS status
            FROM client_campaigns c
            WHERE c.client_id IN ({placeholders})
            """,
            allowed,
        ).fetchall()
        owner_rows = conn.execute(
            f"""
            SELECT DISTINCT TRIM(c.owner_name) AS owner_name
            FROM client_campaigns c
            WHERE c.client_id IN ({placeholders}) AND TRIM(c.owner_name) != ''
            ORDER BY owner_name COLLATE NOCASE
            """,
            allowed,
        ).fetchall()
        category_rows = conn.execute(
            f"""
            SELECT DISTINCT TRIM(c.category) AS category
            FROM client_campaigns c
            WHERE c.client_id IN ({placeholders}) AND TRIM(c.category) != ''
            ORDER BY category COLLATE NOCASE
            """,
            allowed,
        ).fetchall()

    seen_status = {_blank(r["status"]) for r in status_rows if _blank(r["status"])}
    statuses = [s for s in CAMPAIGN_STATUSES if s in seen_status]
    for s in CAMPAIGN_STATUSES:
        if s not in statuses:
            statuses.append(s)
    return CampaignListResponse(
        client_id=selected,
        count=len(items),
        total=total,
        limit=page_size,
        offset=page_offset,
        statuses=statuses,
        owners=[_blank(r["owner_name"]) for r in owner_rows],
        categories=[_blank(r["category"]) for r in category_rows],
        items=items,
    )


def get_campaign_workspace(campaign_id: int) -> CampaignWorkspace:
    allowed = _authorized_client_ids(None)
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        summary = _row_to_summary(conn, row)
        companies = [
            CampaignCompanyRow(
                company_id=int(r["company_id"]),
                company_name=_blank(r["company_name"]),
                external_record_no=_blank(r["external_record_no"]),
                city=_blank(r["city"]),
                state=_blank(r["state"]),
                status=_blank(r["status"]),
                client_id=int(r["client_id"]),
            )
            for r in conn.execute(
                f"""
                SELECT
                    cc.company_id, cc.client_id,
                    co.company_name, co.city, co.state,
                    COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                        AS external_record_no,
                    COALESCE(ccr.status, '') AS status
                FROM campaign_companies cc
                JOIN companies co ON co.id = cc.company_id
                JOIN client_company_relationships ccr
                  ON ccr.id = cc.relationship_id AND ccr.client_id = cc.client_id
                 AND {sql_active_ccr(conn, "ccr")}
                WHERE cc.campaign_id = ? AND cc.client_id = ?
                  AND {sql_active_company(conn, "co")}
                ORDER BY co.company_name COLLATE NOCASE
                """,
                (int(campaign_id), int(row["client_id"])),
            )
        ]
        contact_rows = conn.execute(
            f"""
            SELECT
                m.id AS membership_id,
                m.contact_id, m.company_id, m.client_id,
                TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, ''))
                    AS contact_name,
                COALESCE(ct.title, '') AS title,
                co.company_name,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                    AS external_record_no
            FROM campaign_contacts m
            JOIN contacts ct ON ct.id = m.contact_id
            JOIN companies co ON co.id = m.company_id
            JOIN client_company_relationships ccr
              ON ccr.company_id = m.company_id AND ccr.client_id = m.client_id
             AND {sql_active_ccr(conn, "ccr")}
            WHERE m.campaign_id = ? AND m.client_id = ?
              AND {sql_active_contact(conn, "ct")}
              AND {sql_active_company(conn, "co")}
            ORDER BY m.id ASC
            """,
            (int(campaign_id), int(row["client_id"])),
        ).fetchall()
        primary_by_company: dict[int, int] = {}
        for r in contact_rows:
            company_key = int(r["company_id"])
            if company_key not in primary_by_company:
                primary_by_company[company_key] = int(r["contact_id"])
        contacts = [
            CampaignContactRow(
                contact_id=int(r["contact_id"]),
                company_id=int(r["company_id"]),
                company_name=_blank(r["company_name"]),
                contact_name=_blank(r["contact_name"]),
                title=_blank(r["title"]),
                external_record_no=_blank(r["external_record_no"]),
                client_id=int(r["client_id"]),
                is_primary_routed_contact=int(r["contact_id"])
                == primary_by_company.get(int(r["company_id"])),
            )
            for r in sorted(
                contact_rows,
                key=lambda item: (
                    _blank(item["contact_name"]).lower(),
                    int(item["contact_id"]),
                ),
            )
        ]
        notes = _blank(row["notes"])
    return CampaignWorkspace(
        campaign=summary,
        notes=notes,
        companies=companies,
        contacts=contacts,
    )


def create_operational_campaign(body: CampaignCreateRequest) -> CampaignSummary:
    from access import require_write_client_id

    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    if not user_has_permission(int(user.id), "campaigns.manage", client_id=int(body.client_id or 0) or None):
        raise PermissionError("Not authorized for this client.")
    client_id = require_write_client_id(body.client_id, user_id=user.id)
    name = _blank(body.campaign_name)
    if not name:
        raise ValueError("Campaign name is required.")
    status = _blank(body.status) or "Draft"
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(f"Unknown campaign status '{status}'.")
    owner = _blank(body.owner_name) or _blank(user.full_name)
    ensure_campaigns_schema()
    now = _now()
    with get_connection() as conn:
        if not conn.execute("SELECT id FROM clients WHERE id = ?", (client_id,)).fetchone():
            raise LookupError("Client not found.")
        cur = conn.execute(
            """
            INSERT INTO client_campaigns (
                client_id, campaign_name, description, is_active, is_default,
                status, owner_user_id, owner_name, start_date, end_date, category,
                notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                name,
                _blank(body.description),
                _is_active_flag(status),
                status,
                user.id,
                owner,
                _blank(body.start_date)[:10],
                _blank(body.end_date)[:10],
                _blank(body.category),
                _blank(body.notes),
                now,
                now,
            ),
        )
        campaign_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute(
            """
            SELECT c.*, cl.name AS client_name, cl.code AS client_code
            FROM client_campaigns c
            JOIN clients cl ON cl.id = c.client_id
            WHERE c.id = ?
            """,
            (campaign_id,),
        ).fetchone()
        return _row_to_summary(conn, row)


def update_operational_campaign(campaign_id: int, body: CampaignUpdateRequest) -> CampaignSummary:
    allowed = _authorized_client_ids(None, write=True)
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        name = _blank(body.campaign_name) or _blank(row["campaign_name"])
        if not name:
            raise ValueError("Campaign name is required.")
        status = _blank(body.status) or _status_from_row(row)
        if status not in CAMPAIGN_STATUSES:
            raise ValueError(f"Unknown campaign status '{status}'.")
        conn.execute(
            """
            UPDATE client_campaigns
            SET campaign_name = ?, description = ?, notes = ?,
                status = ?, is_active = ?, owner_name = ?,
                start_date = ?, end_date = ?, category = ?, updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (
                name,
                body.description if body.description is not None else _blank(row["description"]),
                body.notes if body.notes is not None else _blank(row["notes"]),
                status,
                _is_active_flag(status),
                body.owner_name if body.owner_name is not None else _blank(row["owner_name"]),
                (body.start_date if body.start_date is not None else _blank(row["start_date"]))[:10],
                (body.end_date if body.end_date is not None else _blank(row["end_date"]))[:10],
                body.category if body.category is not None else _blank(row["category"]),
                _now(),
                int(campaign_id),
                int(row["client_id"]),
            ),
        )
        conn.commit()
        updated = conn.execute(
            """
            SELECT c.*, cl.name AS client_name, cl.code AS client_code
            FROM client_campaigns c
            JOIN clients cl ON cl.id = c.client_id
            WHERE c.id = ?
            """,
            (int(campaign_id),),
        ).fetchone()
        return _row_to_summary(conn, updated)


def set_campaign_status(campaign_id: int, status: str) -> CampaignActionResult:
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(f"Unknown campaign status '{status}'.")
    allowed = _authorized_client_ids(None, write=True)
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        conn.execute(
            """
            UPDATE client_campaigns
            SET status = ?, is_active = ?, updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (status, _is_active_flag(status), _now(), int(campaign_id), int(row["client_id"])),
        )
        conn.commit()
        updated = conn.execute(
            """
            SELECT c.*, cl.name AS client_name, cl.code AS client_code
            FROM client_campaigns c
            JOIN clients cl ON cl.id = c.client_id
            WHERE c.id = ?
            """,
            (int(campaign_id),),
        ).fetchone()
        campaign = _row_to_summary(conn, updated)
    return CampaignActionResult(
        ok=True,
        message=f"Campaign marked {status}.",
        campaign=campaign,
    )


def _company_record_no(conn, client_id: int, company_id: int) -> tuple[int | None, str]:
    rel = conn.execute(
        f"""
        SELECT ccr.id AS relationship_id,
               COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                   AS external_record_no
        FROM client_company_relationships ccr
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.client_id = ? AND ccr.company_id = ?
          AND {sql_active_ccr(conn, "ccr")}
        """,
        (int(client_id), int(company_id)),
    ).fetchone()
    if rel is None:
        return None, ""
    return int(rel["relationship_id"]), _blank(rel["external_record_no"])


def _previous_oa_campaign_id(conn, client_id: int, company_id: int) -> int | None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
    ).fetchone():
        return None
    oa_cols = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
    }
    if "target_campaign_id" not in oa_cols:
        return None
    row = conn.execute(
        """
        SELECT target_campaign_id FROM opportunity_assignments
        WHERE target_client_id = ? AND company_id = ?
        """,
        (int(client_id), int(company_id)),
    ).fetchone()
    if row is None or row["target_campaign_id"] in (None, 0, ""):
        return None
    return int(row["target_campaign_id"])


def _log_campaign_membership_activity(
    conn,
    *,
    kind: str,
    client_id: int,
    campaign_id: int,
    campaign_name: str,
    company_id: int,
    contact_id: int | None = None,
    created_by: str = "",
    details: str = "",
    company_names: list[str] | None = None,
    assigned_count: int = 1,
    previous_campaign_name: str = "",
    company_name: str = "",
    client_name: str = "",
    contact_names: list[str] | None = None,
    source: str = "",
) -> None:
    from activities_data import insert_activity_row

    relationship_id, record_no = _company_record_no(conn, client_id, company_id)
    if relationship_id is None:
        if kind == "removal":
            raise ValueError("Company is not assigned to this campaign's client.")
        return
    user = resolve_staff_actor()
    user_id = int(user.id) if user is not None else None
    actor = created_by or (_blank(user.full_name) if user is not None else "")
    types = {
        "assignment": "Campaign Assignment",
        "bulk": "Campaign Bulk Assignment",
        "reassignment": "Campaign Reassignment",
        "removal": "Campaign Removal",
    }
    activity_type = types.get(kind, "Campaign Assignment")
    name = _blank(campaign_name)
    if kind == "bulk":
        names = [n for n in (company_names or []) if _blank(n)]
        listed = "\n".join(f"• {item}" for item in names[:80])
        extra = f"\n+{len(names) - 80} more" if len(names) > 80 else ""
        notes = (
            f"Assigned {int(assigned_count)} "
            f"{'opportunity' if int(assigned_count) == 1 else 'opportunities'} "
            f"to {name}."
        )
        if listed:
            notes = f"{notes}\n\n{listed}{extra}"
        if details:
            notes = f"{notes}\n{details}"
        outcome = f"{int(assigned_count)} to {name}"
    elif kind == "reassignment":
        notes = f"Reassigned to {name}."
        if previous_campaign_name:
            notes = f"Moved from {previous_campaign_name} to {name}."
        if details:
            notes = f"{notes} {details}".strip()
        outcome = name
    elif kind == "removal":
        subject = _blank(company_name) or "Company"
        notes = f"Removed {subject} from {name}."
        contacts = [n for n in (contact_names or []) if _blank(n)]
        if contacts:
            listed = "\n".join(f"• {item}" for item in contacts[:80])
            extra = f"\n+{len(contacts) - 80} more" if len(contacts) > 80 else ""
            notes = f"{notes}\n\nContacts removed:\n{listed}{extra}"
        else:
            notes = f"{notes}\n\nContacts removed: none"
        if _blank(client_name):
            notes = f"{notes}\nClient: {_blank(client_name)}"
        if details:
            notes = f"{notes}\n{details}"
        outcome = name
    else:
        subject = _blank(company_name) or "Company"
        notes = f"Assigned {subject} to {name}."
        contacts = [n for n in (contact_names or []) if _blank(n)]
        if contacts:
            label = "Contact" if len(contacts) == 1 else "Contacts"
            listed = "\n".join(f"• {item}" for item in contacts[:80])
            extra = f"\n+{len(contacts) - 80} more" if len(contacts) > 80 else ""
            notes = f"{notes}\n\n{label}:\n{listed}{extra}"
        if _blank(client_name):
            notes = f"{notes}\nClient: {_blank(client_name)}"
        if _blank(source):
            notes = f"{notes}\nSource: {_blank(source)}"
        if details:
            notes = f"{notes}\n{details}"
        outcome = name
    insert_activity_row(
        conn,
        client_id=int(client_id),
        company_id=int(company_id),
        relationship_id=relationship_id,
        external_record_no=record_no,
        user_id=user_id,
        contact_id=int(contact_id) if contact_id else None,
        activity_type=activity_type,
        outcome=outcome,
        notes=notes,
        assigned_user=actor,
        created_by=actor,
    )


def _party_labels(
    conn,
    client_id: int,
    company_id: int,
    contact_id: int | None = None,
) -> tuple[str, str, str]:
    company = conn.execute(
        "SELECT company_name FROM companies WHERE id = ?",
        (int(company_id),),
    ).fetchone()
    client = conn.execute("SELECT name FROM clients WHERE id = ?", (int(client_id),)).fetchone()
    contact_name = ""
    if contact_id:
        contact = conn.execute(
            """
            SELECT TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) AS contact_name
            FROM contacts WHERE id = ?
            """,
            (int(contact_id),),
        ).fetchone()
        contact_name = _blank(contact["contact_name"]) if contact is not None else ""
    return (
        _blank(company["company_name"]) if company is not None else "",
        _blank(client["name"]) if client is not None else "",
        contact_name,
    )


def _insert_campaign_company_membership(
    conn,
    *,
    campaign_id: int,
    client_id: int,
    company_id: int,
    notes: str,
    created_by: str,
) -> bool:
    rel = conn.execute(
        f"""
        SELECT id FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
          AND {sql_active_ccr(conn, "client_company_relationships")}
        """,
        (int(client_id), int(company_id)),
    ).fetchone()
    if rel is None:
        raise ValueError("Company is not assigned to this campaign's client.")
    conn.execute(
        """
        INSERT INTO campaign_companies (
            campaign_id, client_id, company_id, relationship_id, notes, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(campaign_id, company_id) DO NOTHING
        """,
        (
            int(campaign_id),
            int(client_id),
            int(company_id),
            int(rel["id"]),
            _blank(notes),
            created_by,
            _now(),
        ),
    )
    return int(conn.execute("SELECT changes() AS n").fetchone()["n"]) > 0


def _insert_campaign_contact_membership(
    conn,
    *,
    campaign_id: int,
    client_id: int,
    contact_id: int,
    notes: str,
    created_by: str,
) -> bool:
    contact = conn.execute(
        f"""
        SELECT ct.id, ct.company_id
        FROM contacts ct
        JOIN client_company_relationships ccr
          ON ccr.company_id = ct.company_id AND ccr.client_id = ?
         AND {sql_active_ccr(conn, "ccr")}
        WHERE ct.id = ?
          AND {sql_active_contact(conn, "ct")}
        """,
        (int(client_id), int(contact_id)),
    ).fetchone()
    if contact is None:
        raise ValueError("Contact is not available for this campaign's client.")
    conn.execute(
        """
        INSERT INTO campaign_contacts (
            campaign_id, client_id, contact_id, company_id, notes, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(campaign_id, contact_id) DO NOTHING
        """,
        (
            int(campaign_id),
            int(client_id),
            int(contact_id),
            int(contact["company_id"]),
            _blank(notes),
            created_by,
            _now(),
        ),
    )
    return int(conn.execute("SELECT changes() AS n").fetchone()["n"]) > 0


def add_campaign_company(
    campaign_id: int,
    body: CampaignMemberAddRequest,
    *,
    log_activity: bool = True,
) -> CampaignWorkspace:
    allowed = _authorized_client_ids(None, write=True)
    company_id = int(body.company_id or 0)
    if company_id <= 0:
        raise ValueError("company_id is required.")
    user = resolve_staff_actor()
    created_by = _blank(user.full_name) if user is not None else ""
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        client_id = int(row["client_id"])
        prior_campaigns = [
            _blank(r["campaign_name"])
            for r in conn.execute(
                """
                SELECT c.campaign_name
                FROM campaign_companies cc
                JOIN client_campaigns c ON c.id = cc.campaign_id
                WHERE cc.client_id = ? AND cc.company_id = ? AND cc.campaign_id != ?
                ORDER BY c.campaign_name COLLATE NOCASE
                """,
                (client_id, company_id, int(campaign_id)),
            ).fetchall()
        ]
        previous_oa = _previous_oa_campaign_id(conn, client_id, company_id)
        inserted = _insert_campaign_company_membership(
            conn,
            campaign_id=int(campaign_id),
            client_id=client_id,
            company_id=company_id,
            notes=_blank(body.notes),
            created_by=created_by,
        )
        _stamp_campaign_assignment(conn, client_id, company_id, int(campaign_id))
        if inserted and log_activity:
            previous_name = ""
            if previous_oa and previous_oa != int(campaign_id):
                prev = conn.execute(
                    "SELECT campaign_name FROM client_campaigns WHERE id = ?",
                    (previous_oa,),
                ).fetchone()
                previous_name = _blank(prev["campaign_name"]) if prev is not None else ""
            kind = "reassignment" if previous_name or prior_campaigns else "assignment"
            extra = ""
            if prior_campaigns and kind == "assignment":
                extra = f"Already in {', '.join(prior_campaigns)}."
            company_name, client_name, _contact_name = _party_labels(conn, client_id, company_id)
            _log_campaign_membership_activity(
                conn,
                kind=kind,
                client_id=client_id,
                campaign_id=int(campaign_id),
                campaign_name=_blank(row["campaign_name"]),
                company_id=company_id,
                created_by=created_by,
                details=extra,
                previous_campaign_name=previous_name,
                company_name=company_name,
                client_name=client_name,
                source=_blank(body.notes),
            )
        conn.commit()
    return get_campaign_workspace(campaign_id)


def add_campaign_contact(
    campaign_id: int,
    body: CampaignMemberAddRequest,
    *,
    log_activity: bool = True,
) -> CampaignWorkspace:
    allowed = _authorized_client_ids(None, write=True)
    contact_id = int(body.contact_id or 0)
    if contact_id <= 0:
        raise ValueError("contact_id is required.")
    user = resolve_staff_actor()
    created_by = _blank(user.full_name) if user is not None else ""
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        client_id = int(row["client_id"])
        inserted = _insert_campaign_contact_membership(
            conn,
            campaign_id=int(campaign_id),
            client_id=client_id,
            contact_id=contact_id,
            notes=_blank(body.notes),
            created_by=created_by,
        )
        if inserted and log_activity:
            contact = conn.execute(
                "SELECT company_id FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            company_id = int(contact["company_id"]) if contact is not None else 0
            company_name, client_name, contact_name = _party_labels(
                conn, client_id, company_id, contact_id
            )
            _log_campaign_membership_activity(
                conn,
                kind="assignment",
                client_id=client_id,
                campaign_id=int(campaign_id),
                campaign_name=_blank(row["campaign_name"]),
                company_id=company_id,
                contact_id=contact_id,
                created_by=created_by,
                company_name=company_name,
                client_name=client_name,
                contact_names=[contact_name] if contact_name else [],
                source=_blank(body.notes),
                details="Contact added to campaign.",
            )
        conn.commit()
    return get_campaign_workspace(campaign_id)


def _primary_routed_contact_id(conn, campaign_id: int, client_id: int, company_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT contact_id FROM campaign_contacts
        WHERE campaign_id = ? AND client_id = ? AND company_id = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (int(campaign_id), int(client_id), int(company_id)),
    ).fetchone()
    if row is None:
        return None
    return int(row["contact_id"])


def _clear_opportunity_campaign(conn, client_id: int, company_id: int, campaign_id: int) -> None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
    ).fetchone():
        return
    oa_cols = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
    }
    if "target_campaign_id" not in oa_cols:
        return
    conn.execute(
        """
        UPDATE opportunity_assignments
        SET target_campaign_id = NULL
        WHERE target_client_id = ? AND company_id = ? AND target_campaign_id = ?
        """,
        (int(client_id), int(company_id), int(campaign_id)),
    )


def _unassign_company_from_campaign(
    conn,
    *,
    campaign_id: int,
    campaign_name: str,
    client_id: int,
    client_name: str,
    company_id: int,
    created_by: str,
) -> None:
    membership = conn.execute(
        """
        SELECT notes FROM campaign_companies
        WHERE campaign_id = ? AND client_id = ? AND company_id = ?
        """,
        (int(campaign_id), int(client_id), int(company_id)),
    ).fetchone()
    if membership is None:
        raise LookupError("Company is not a member of this campaign.")
    relationship_id, _record_no = _company_record_no(conn, client_id, company_id)
    if relationship_id is None:
        raise ValueError("Company is not assigned to this campaign's client.")
    company = conn.execute(
        "SELECT company_name FROM companies WHERE id = ?",
        (int(company_id),),
    ).fetchone()
    company_name = _blank(company["company_name"]) if company is not None else ""
    contact_rows = conn.execute(
        """
        SELECT
            m.contact_id,
            TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, '')) AS contact_name
        FROM campaign_contacts m
        JOIN contacts ct ON ct.id = m.contact_id
        WHERE m.campaign_id = ? AND m.client_id = ? AND m.company_id = ?
        ORDER BY m.id ASC
        """,
        (int(campaign_id), int(client_id), int(company_id)),
    ).fetchall()
    contact_names = [_blank(r["contact_name"]) for r in contact_rows if _blank(r["contact_name"])]
    routed_contact_id = int(contact_rows[0]["contact_id"]) if contact_rows else None
    source = _blank(membership["notes"])
    if not source:
        oa = conn.execute(
            """
            SELECT originated_from FROM opportunity_assignments
            WHERE target_client_id = ? AND company_id = ?
            """,
            (int(client_id), int(company_id)),
        ).fetchone() if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
        ).fetchone() else None
        source = _blank(oa["originated_from"]) if oa is not None else ""
    conn.execute(
        """
        DELETE FROM campaign_contacts
        WHERE campaign_id = ? AND client_id = ? AND company_id = ?
        """,
        (int(campaign_id), int(client_id), int(company_id)),
    )
    deleted_contacts = int(conn.execute("SELECT changes() AS n").fetchone()["n"])
    conn.execute(
        """
        DELETE FROM campaign_companies
        WHERE campaign_id = ? AND client_id = ? AND company_id = ?
        """,
        (int(campaign_id), int(client_id), int(company_id)),
    )
    deleted_company = int(conn.execute("SELECT changes() AS n").fetchone()["n"])
    if deleted_company != 1:
        raise RuntimeError("Campaign company membership could not be removed.")
    if deleted_contacts != len(contact_rows):
        raise RuntimeError("Campaign contact memberships could not be removed.")
    remaining = conn.execute(
        """
        SELECT 1 FROM campaign_companies
        WHERE client_id = ? AND company_id = ?
        LIMIT 1
        """,
        (int(client_id), int(company_id)),
    ).fetchone()
    if remaining is None:
        _clear_opportunity_campaign(conn, client_id, company_id, campaign_id)
        _place_unassigned(
            conn,
            client_id=int(client_id),
            company_id=int(company_id),
            contact_id=routed_contact_id,
            source=source or "unassigned",
            created_by=created_by,
        )
    _log_campaign_membership_activity(
        conn,
        kind="removal",
        client_id=int(client_id),
        campaign_id=int(campaign_id),
        campaign_name=campaign_name,
        company_id=int(company_id),
        contact_id=routed_contact_id,
        created_by=created_by,
        company_name=company_name,
        client_name=client_name,
        contact_names=contact_names,
    )


def remove_campaign_company(campaign_id: int, company_id: int) -> CampaignWorkspace:
    allowed = _authorized_client_ids(None, write=True)
    if int(company_id) <= 0:
        raise ValueError("company_id is required.")
    user = resolve_staff_actor()
    created_by = _blank(user.full_name) if user is not None else ""
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        client_id = int(row["client_id"])
        client = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        _unassign_company_from_campaign(
            conn,
            campaign_id=int(campaign_id),
            campaign_name=_blank(row["campaign_name"]),
            client_id=client_id,
            client_name=_blank(client["name"]) if client is not None else "",
            company_id=int(company_id),
            created_by=created_by,
        )
        conn.commit()
    return get_campaign_workspace(campaign_id)


def remove_campaign_contact(campaign_id: int, contact_id: int) -> CampaignWorkspace:
    allowed = _authorized_client_ids(None, write=True)
    if int(contact_id) <= 0:
        raise ValueError("contact_id is required.")
    user = resolve_staff_actor()
    created_by = _blank(user.full_name) if user is not None else ""
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        client_id = int(row["client_id"])
        existing = conn.execute(
            """
            SELECT company_id FROM campaign_contacts
            WHERE campaign_id = ? AND client_id = ? AND contact_id = ?
            """,
            (int(campaign_id), client_id, int(contact_id)),
        ).fetchone()
        if existing is None:
            raise LookupError("Contact is not a member of this campaign.")
        company_id = int(existing["company_id"])
        primary_id = _primary_routed_contact_id(conn, int(campaign_id), client_id, company_id)
        if primary_id is not None and int(contact_id) == primary_id:
            client = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
            _unassign_company_from_campaign(
                conn,
                campaign_id=int(campaign_id),
                campaign_name=_blank(row["campaign_name"]),
                client_id=client_id,
                client_name=_blank(client["name"]) if client is not None else "",
                company_id=company_id,
                created_by=created_by,
            )
            conn.commit()
            return get_campaign_workspace(campaign_id)
        contact = conn.execute(
            """
            SELECT TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) AS contact_name
            FROM contacts WHERE id = ?
            """,
            (int(contact_id),),
        ).fetchone()
        company = conn.execute(
            "SELECT company_name FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        client = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        conn.execute(
            """
            DELETE FROM campaign_contacts
            WHERE campaign_id = ? AND client_id = ? AND contact_id = ?
            """,
            (int(campaign_id), client_id, int(contact_id)),
        )
        if int(conn.execute("SELECT changes() AS n").fetchone()["n"]) != 1:
            raise RuntimeError("Campaign contact membership could not be removed.")
        _log_campaign_membership_activity(
            conn,
            kind="removal",
            client_id=client_id,
            campaign_id=int(campaign_id),
            campaign_name=_blank(row["campaign_name"]),
            company_id=company_id,
            contact_id=int(contact_id),
            created_by=created_by,
            company_name=_blank(company["company_name"]) if company is not None else "",
            client_name=_blank(client["name"]) if client is not None else "",
            contact_names=[_blank(contact["contact_name"])] if contact is not None else [],
            details="Contact removed from campaign. Company membership unchanged.",
        )
        conn.commit()
    return get_campaign_workspace(campaign_id)


def search_campaign_members(campaign_id: int, q: str = "") -> CampaignMemberSearchResponse:
    allowed = _authorized_client_ids(None)
    needle = _blank(q).lower()
    like = f"%{needle}%"
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        client_id = int(row["client_id"])
        company_sql = f"""
            SELECT
                co.id AS company_id,
                co.company_name,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                    AS external_record_no,
                COALESCE(co.city, '') AS city,
                COALESCE(co.state, '') AS state,
                COALESCE(ccr.status, '') AS status,
                ccr.client_id
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
              AND {sql_active_ccr(conn)}
              AND {sql_active_company(conn)}
        """
        company_params: list[object] = [client_id]
        if needle:
            company_sql += """
              AND (
                lower(co.company_name) LIKE ?
             OR lower(COALESCE(ccr.external_record_no, '')) LIKE ?
              )
            """
            company_params.extend([like, like])
        company_sql += " ORDER BY co.company_name COLLATE NOCASE LIMIT 12"
        companies = [
            CampaignCompanyRow(
                company_id=int(r["company_id"]),
                company_name=_blank(r["company_name"]),
                external_record_no=_blank(r["external_record_no"]),
                city=_blank(r["city"]),
                state=_blank(r["state"]),
                status=_blank(r["status"]),
                client_id=int(r["client_id"]),
            )
            for r in conn.execute(company_sql, company_params)
        ]
        contact_sql = f"""
            SELECT
                ct.id AS contact_id,
                ct.company_id,
                co.company_name,
                TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, ''))
                    AS contact_name,
                COALESCE(ct.title, '') AS title,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                    AS external_record_no,
                ccr.client_id
            FROM contacts ct
            JOIN companies co ON co.id = ct.company_id
            JOIN client_company_relationships ccr
              ON ccr.company_id = ct.company_id AND ccr.client_id = ?
            WHERE 1 = 1
              AND {sql_active_ccr(conn)}
              AND {sql_active_company(conn)}
              AND {sql_active_contact(conn)}
        """
        contact_params: list[object] = [client_id]
        if needle:
            contact_sql += """
              AND (
                lower(TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, ''))) LIKE ?
             OR lower(COALESCE(ct.email, '')) LIKE ?
             OR lower(co.company_name) LIKE ?
              )
            """
            contact_params.extend([like, like, like])
        contact_sql += """
            ORDER BY ct.last_name COLLATE NOCASE, ct.first_name COLLATE NOCASE
            LIMIT 12
        """
        contacts = [
            CampaignContactRow(
                contact_id=int(r["contact_id"]),
                company_id=int(r["company_id"]),
                company_name=_blank(r["company_name"]),
                contact_name=_blank(r["contact_name"]),
                title=_blank(r["title"]),
                external_record_no=_blank(r["external_record_no"]),
                client_id=int(r["client_id"]),
            )
            for r in conn.execute(contact_sql, contact_params)
        ]
    return CampaignMemberSearchResponse(companies=companies, contacts=contacts)


SELECTABLE_STATUSES = ("Active", "Draft", "Paused")


def is_campaign_route_status(status: str) -> bool:
    key = _blank(status).lower()
    if key == "hot prospect" or key.startswith("hot prospect"):
        return True
    if key == "qualified" or key.startswith("qualified "):
        return True
    return False


def _campaign_status_sql() -> str:
    return (
        "COALESCE(NULLIF(TRIM(c.status), ''), "
        "CASE WHEN c.is_active = 0 THEN 'Paused' ELSE 'Active' END)"
    )


def _memberships(conn, client_id: int, company_id: int) -> list[tuple[int, str]]:
    return [
        (int(r["campaign_id"]), _blank(r["campaign_name"]))
        for r in conn.execute(
            """
            SELECT cc.campaign_id, c.campaign_name
            FROM campaign_companies cc
            JOIN client_campaigns c ON c.id = cc.campaign_id
            WHERE cc.client_id = ? AND cc.company_id = ?
            ORDER BY c.campaign_name COLLATE NOCASE
            """,
            (client_id, company_id),
        )
    ]


def _selectable_campaigns(conn, client_id: int) -> list:
    status_sql = _campaign_status_sql()
    placeholders = ",".join("?" * len(SELECTABLE_STATUSES))
    return conn.execute(
        f"""
        SELECT c.*, cl.name AS client_name, cl.code AS client_code,
               {status_sql} AS resolved_status
        FROM client_campaigns c
        JOIN clients cl ON cl.id = c.client_id
        WHERE c.client_id = ? AND {status_sql} IN ({placeholders})
        ORDER BY c.is_default DESC, c.campaign_name COLLATE NOCASE
        """,
        (client_id, *SELECTABLE_STATUSES),
    ).fetchall()


def _hint_campaign_id(
    conn,
    client_id: int,
    company_id: int,
    research_run_id: int | None,
) -> int | None:
    if research_run_id:
        row = conn.execute(
            """
            SELECT campaign_id, working_for_client_id
            FROM company_research_runs
            WHERE id = ?
            """,
            (int(research_run_id),),
        ).fetchone()
        if row is not None and row["campaign_id"] and int(row["working_for_client_id"]) == client_id:
            return int(row["campaign_id"])
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_research_runs'"
    ).fetchone():
        row = conn.execute(
            """
            SELECT campaign_id FROM company_research_runs
            WHERE company_id = ? AND working_for_client_id = ?
              AND campaign_id IS NOT NULL
            ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
            LIMIT 1
            """,
            (company_id, client_id),
        ).fetchone()
        if row is not None and row["campaign_id"]:
            return int(row["campaign_id"])
    return None


def _score_campaign(row, company, hint_id: int | None) -> int:
    cid = int(row["id"])
    score = 0
    if hint_id and cid == hint_id:
        score += 50
    if int(row["is_default"] or 0):
        score += 5
    if _status_from_row(row) == "Active":
        score += 8
    hay = " ".join(
        [
            _blank(row["campaign_name"]),
            _blank(row["description"]),
            _blank(row["category"]) if "category" in row.keys() else "",
            _blank(row["target_industries"]) if "target_industries" in row.keys() else "",
            _blank(row["target_products"]) if "target_products" in row.keys() else "",
            _blank(row["target_customer_types"]) if "target_customer_types" in row.keys() else "",
        ]
    ).lower()
    bits = [
        _blank(company["company_name"]) if company is not None else "",
        _blank(company["type_of_industry"]) if company is not None and "type_of_industry" in company.keys() else "",
        _blank(company["primary_naics_description"])
        if company is not None and "primary_naics_description" in company.keys()
        else "",
        _blank(company["primary_sic_description"])
        if company is not None and "primary_sic_description" in company.keys()
        else "",
    ]
    for bit in bits:
        token = bit.lower().strip()
        if len(token) >= 4 and token in hay:
            score += 4
        for part in token.replace("/", " ").replace(",", " ").split():
            if len(part) >= 4 and part in hay:
                score += 1
    return score


def _place_unassigned(
    conn,
    *,
    client_id: int,
    company_id: int,
    contact_id: int | None,
    source: str,
    created_by: str,
) -> None:
    rel = conn.execute(
        f"""
        SELECT id FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
          AND {sql_active_ccr(conn, "client_company_relationships")}
        """,
        (client_id, company_id),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO campaign_unassigned (
            client_id, company_id, contact_id, relationship_id, source, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_id, company_id) DO UPDATE SET
            contact_id = COALESCE(excluded.contact_id, campaign_unassigned.contact_id),
            source = excluded.source,
            created_by = excluded.created_by,
            created_at = excluded.created_at
        """,
        (
            client_id,
            company_id,
            contact_id,
            int(rel["id"]) if rel is not None else None,
            _blank(source) or "opportunity",
            created_by,
            _now(),
        ),
    )


def suggest_campaign_route(
    *,
    client_id: int,
    company_id: int,
    contact_id: int | None = None,
    research_run_id: int | None = None,
    source: str = "",
    force: bool = False,
) -> CampaignRouteSuggestion:
    allowed = _authorized_client_ids(client_id)
    if int(client_id) not in allowed:
        raise PermissionError("Not authorized for this client.")
    ensure_campaigns_schema()
    with get_connection() as conn:
        company = conn.execute(
            "SELECT * FROM companies WHERE id = ?",
            (int(company_id),),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found.")
        rel = conn.execute(
            f"""
            SELECT id FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
              AND {sql_active_ccr(conn, "client_company_relationships")}
            """,
            (int(client_id), int(company_id)),
        ).fetchone()
        if rel is None:
            raise LookupError("Company is not assigned to this client.")
        assigned = _memberships(conn, int(client_id), int(company_id))
        assigned_ids = [item[0] for item in assigned]
        rows = _selectable_campaigns(conn, int(client_id))
        hint = _hint_campaign_id(conn, int(client_id), int(company_id), research_run_id)
        scored: list[tuple[int, object]] = [(_score_campaign(r, company, hint), r) for r in rows]
        scored.sort(key=lambda pair: (-pair[0], 0 if int(pair[1]["is_default"] or 0) else 1))
        recommended_id = int(scored[0][1]["id"]) if scored else None
        choices = [
            CampaignChoice(
                campaign_id=int(r["id"]),
                campaign_name=_blank(r["campaign_name"]),
                status=_status_from_row(r),
                is_default=bool(r["is_default"]),
                client_id=int(r["client_id"]),
                recommended=recommended_id is not None and int(r["id"]) == recommended_id,
            )
            for r in rows
        ]
        already = bool(assigned_ids)
        if already and not force:
            return CampaignRouteSuggestion(
                client_id=int(client_id),
                company_id=int(company_id),
                contact_id=contact_id,
                company_name=_blank(company["company_name"]),
                should_prompt=False,
                already_assigned=True,
                assigned_campaign_ids=assigned_ids,
                assigned_campaign_names=[name for _, name in assigned],
                recommended_campaign_id=assigned_ids[0],
                message="Already assigned to a campaign for this client.",
                campaigns=choices,
            )
        if not choices:
            user = resolve_staff_actor()
            created_by = _blank(user.full_name) if user is not None else ""
            _place_unassigned(
                conn,
                client_id=int(client_id),
                company_id=int(company_id),
                contact_id=contact_id,
                source=source or "no_matching_campaign",
                created_by=created_by,
            )
            conn.commit()
            return CampaignRouteSuggestion(
                client_id=int(client_id),
                company_id=int(company_id),
                contact_id=contact_id,
                company_name=_blank(company["company_name"]),
                should_prompt=False,
                auto_unassigned=True,
                message="No active campaign matched. Added to Unassigned Opportunities.",
            )
        return CampaignRouteSuggestion(
            client_id=int(client_id),
            company_id=int(company_id),
            contact_id=contact_id,
            company_name=_blank(company["company_name"]),
            should_prompt=True,
            already_assigned=already,
            assigned_campaign_ids=assigned_ids,
            assigned_campaign_names=[name for _, name in assigned],
            recommended_campaign_id=recommended_id,
            message="Add this opportunity to a campaign?",
            campaigns=choices,
        )


def confirm_campaign_route(body: CampaignRouteConfirmRequest) -> CampaignRouteSuggestion:
    from access import require_write_client_id

    require_write_client_id(body.client_id)
    allowed = _authorized_client_ids(body.client_id, write=True)
    if int(body.client_id) not in allowed:
        raise PermissionError("Not authorized for this client.")
    user = resolve_staff_actor()
    created_by = _blank(user.full_name) if user is not None else ""
    campaign_id = int(body.campaign_id)
    company_id = int(body.company_id)
    contact_id = int(body.contact_id) if body.contact_id else None
    source = _blank(body.source)
    ensure_campaigns_schema()
    with get_connection() as conn:
        row = _require_campaign(conn, campaign_id, allowed)
        client_id = int(row["client_id"])
        if client_id != int(body.client_id):
            raise ValueError("Campaign does not belong to the selected client.")
        prior_campaigns = [
            _blank(r["campaign_name"])
            for r in conn.execute(
                """
                SELECT c.campaign_name
                FROM campaign_companies cc
                JOIN client_campaigns c ON c.id = cc.campaign_id
                WHERE cc.client_id = ? AND cc.company_id = ? AND cc.campaign_id != ?
                ORDER BY c.campaign_name COLLATE NOCASE
                """,
                (client_id, company_id, campaign_id),
            ).fetchall()
        ]
        previous_oa = _previous_oa_campaign_id(conn, client_id, company_id)
        company_inserted = _insert_campaign_company_membership(
            conn,
            campaign_id=campaign_id,
            client_id=client_id,
            company_id=company_id,
            notes=source,
            created_by=created_by,
        )
        contact_inserted = False
        if contact_id:
            try:
                contact_inserted = _insert_campaign_contact_membership(
                    conn,
                    campaign_id=campaign_id,
                    client_id=client_id,
                    contact_id=contact_id,
                    notes=source,
                    created_by=created_by,
                )
            except ValueError:
                contact_id = None
        _stamp_campaign_assignment(conn, client_id, company_id, campaign_id)
        if company_inserted or contact_inserted:
            previous_name = ""
            if previous_oa and previous_oa != campaign_id:
                prev = conn.execute(
                    "SELECT campaign_name FROM client_campaigns WHERE id = ?",
                    (previous_oa,),
                ).fetchone()
                previous_name = _blank(prev["campaign_name"]) if prev is not None else ""
            kind = "reassignment" if company_inserted and (previous_name or prior_campaigns) else "assignment"
            extra = ""
            if prior_campaigns and kind == "assignment":
                extra = f"Already in {', '.join(prior_campaigns)}."
            if contact_inserted and not company_inserted:
                extra = "Contact added to campaign."
            routed_contact_id = contact_id if contact_inserted else None
            company_name, client_name, contact_name = _party_labels(
                conn, client_id, company_id, routed_contact_id
            )
            _log_campaign_membership_activity(
                conn,
                kind=kind,
                client_id=client_id,
                campaign_id=campaign_id,
                campaign_name=_blank(row["campaign_name"]),
                company_id=company_id,
                contact_id=routed_contact_id,
                created_by=created_by,
                details=extra,
                previous_campaign_name=previous_name,
                company_name=company_name,
                client_name=client_name,
                contact_names=[contact_name] if contact_name else [],
                source=source,
            )
        conn.commit()
    return suggest_campaign_route(
        client_id=int(body.client_id),
        company_id=int(body.company_id),
        contact_id=body.contact_id,
        source=body.source,
        force=True,
    )


def defer_campaign_route(body: CampaignRouteDeferRequest) -> CampaignRouteSuggestion:
    from access import require_write_client_id

    require_write_client_id(body.client_id)
    allowed = _authorized_client_ids(body.client_id, write=True)
    if int(body.client_id) not in allowed:
        raise PermissionError("Not authorized for this client.")
    user = resolve_staff_actor()
    created_by = _blank(user.full_name) if user is not None else ""
    ensure_campaigns_schema()
    with get_connection() as conn:
        if not conn.execute(
            f"""
            SELECT id FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
              AND {sql_active_ccr(conn, "client_company_relationships")}
            """,
            (int(body.client_id), int(body.company_id)),
        ).fetchone():
            raise LookupError("Company is not assigned to this client.")
        _place_unassigned(
            conn,
            client_id=int(body.client_id),
            company_id=int(body.company_id),
            contact_id=body.contact_id,
            source=body.source or "not_now",
            created_by=created_by,
        )
        conn.commit()
    return CampaignRouteSuggestion(
        client_id=int(body.client_id),
        company_id=int(body.company_id),
        contact_id=body.contact_id,
        should_prompt=False,
        auto_unassigned=True,
        message="Saved to Unassigned Opportunities.",
    )


def _source_type_label(source: str) -> str:
    key = _blank(source).lower()
    if key in {"not_now", "unassigned"}:
        return "Not now"
    if key in {"research", "research_add"}:
        return "Research"
    if "cross" in key:
        return "Cross-Client Opportunity"
    if key in {"appointment", "appointment set"}:
        return "Appointment Set"
    if key in {"hot", "status"}:
        return "Hot Prospect / Qualified"
    if key in {"no_matching_campaign"}:
        return "No matching campaign"
    return _blank(source) or "Opportunity"


def _matching_unassigned_rows(conn, allowed: list[int], q: str = "") -> list[dict]:
    if not allowed:
        return []
    placeholders = ",".join("?" * len(allowed))
    oa_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
    ).fetchone()
    oa_cols = set()
    if oa_exists:
        oa_cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
        }
    campaign_filter = ""
    if "target_campaign_id" in oa_cols:
        campaign_filter = "AND (oa.target_campaign_id IS NULL OR oa.target_campaign_id = 0)"
    union_sql = f"""
        SELECT
            u.client_id,
            u.company_id,
            u.contact_id,
            u.source AS source,
            u.created_at AS identified_at,
            u.id AS sort_id
        FROM campaign_unassigned u
        WHERE u.client_id IN ({placeholders})
          AND NOT EXISTS (
            SELECT 1 FROM campaign_companies cc
            WHERE cc.client_id = u.client_id AND cc.company_id = u.company_id
          )
    """
    union_params: list[object] = list(allowed)
    if oa_exists:
        union_sql += f"""
        UNION
        SELECT
            oa.target_client_id AS client_id,
            oa.company_id,
            NULL AS contact_id,
            COALESCE(oa.originated_from, '') AS source,
            oa.created_at AS identified_at,
            -oa.id AS sort_id
        FROM opportunity_assignments oa
        WHERE oa.target_client_id IN ({placeholders})
          {campaign_filter}
          AND NOT EXISTS (
            SELECT 1 FROM campaign_unassigned u2
            WHERE u2.client_id = oa.target_client_id AND u2.company_id = oa.company_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM campaign_companies cc
            WHERE cc.client_id = oa.target_client_id AND cc.company_id = oa.company_id
          )
        """
        union_params.extend(allowed)
    rows = conn.execute(
        f"""
        SELECT
            src.client_id,
            src.company_id,
            src.source,
            src.identified_at,
            src.sort_id,
            cl.name AS client_name,
            co.company_name,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                AS external_record_no,
            COALESCE(ccr.status, '') AS status,
            COALESCE(NULLIF(TRIM(usr.full_name), ''), '') AS assigned_rep,
            ct.id AS resolved_contact_id,
            TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, '')) AS contact_name
        FROM ({union_sql}) src
        JOIN clients cl ON cl.id = src.client_id
        JOIN companies co ON co.id = src.company_id
         AND {sql_active_company(conn, "co")}
        JOIN client_company_relationships ccr
          ON ccr.client_id = src.client_id AND ccr.company_id = src.company_id
         AND {sql_active_ccr(conn, "ccr")}
        LEFT JOIN users usr ON usr.id = ccr.assigned_user_id
        LEFT JOIN contacts ct ON ct.id = COALESCE(
            src.contact_id,
            (
                SELECT ct2.id FROM contacts ct2
                WHERE ct2.company_id = src.company_id
                ORDER BY ct2.id
                LIMIT 1
            )
        )
        ORDER BY datetime(src.identified_at) DESC, src.sort_id DESC
        """,
        union_params,
    ).fetchall()
    needle = _blank(q).lower()
    seen: set[tuple[int, int]] = set()
    matched: list[dict] = []
    for r in rows:
        key = (int(r["client_id"]), int(r["company_id"]))
        if key in seen:
            continue
        seen.add(key)
        source = _blank(r["source"])
        source_type = _source_type_label(source)
        blob = " ".join(
            [
                _blank(r["client_name"]),
                _blank(r["company_name"]),
                _blank(r["external_record_no"]),
                _blank(r["contact_name"]),
                source,
                source_type,
                _blank(r["status"]),
                _blank(r["assigned_rep"]),
            ]
        ).lower()
        if needle and needle not in blob:
            continue
        matched.append(
            {
                "sort_id": int(r["sort_id"]),
                "client_id": int(r["client_id"]),
                "client_name": _blank(r["client_name"]),
                "company_id": int(r["company_id"]),
                "company_name": _blank(r["company_name"]),
                "external_record_no": _blank(r["external_record_no"]),
                "contact_id": int(r["resolved_contact_id"]) if r["resolved_contact_id"] is not None else None,
                "contact_name": _blank(r["contact_name"]),
                "source": source,
                "source_type": source_type,
                "status": _blank(r["status"]),
                "identified_at": _blank(r["identified_at"]),
                "assigned_rep": _blank(r["assigned_rep"]),
            }
        )
    return matched


def list_unassigned_opportunities(
    client_id: int,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> UnassignedOpportunityList:
    selected = int(client_id)
    allowed = _authorized_client_ids(selected if selected > 0 else None)
    ensure_campaigns_schema()
    page_size = max(1, min(int(limit or 50), 500))
    page_offset = max(0, int(offset or 0))
    query = _blank(q)
    if not allowed:
        return UnassignedOpportunityList(
            client_id=selected, limit=page_size, offset=page_offset, q=query
        )
    with get_connection() as conn:
        matched = _matching_unassigned_rows(conn, allowed, query)
        campaigns_by_client: dict[int, list] = {}
        for cid in allowed:
            campaigns_by_client[int(cid)] = [
                row
                for row in _selectable_campaigns(conn, int(cid))
                if _status_from_row(row) == "Active"
            ]
        page = matched[page_offset : page_offset + page_size]
        items: list[UnassignedOpportunity] = []
        for r in page:
            company = conn.execute(
                "SELECT * FROM companies WHERE id = ?",
                (int(r["company_id"]),),
            ).fetchone()
            camp_rows = campaigns_by_client.get(int(r["client_id"]), [])
            hint = _hint_campaign_id(conn, int(r["client_id"]), int(r["company_id"]), None)
            scored = sorted(
                ((_score_campaign(cr, company, hint), cr) for cr in camp_rows),
                key=lambda pair: (-pair[0], 0 if int(pair[1]["is_default"] or 0) else 1),
            )
            rec = scored[0][1] if scored else None
            items.append(
                UnassignedOpportunity(
                    unassigned_id=int(r["sort_id"]),
                    client_id=int(r["client_id"]),
                    client_name=_blank(r["client_name"]),
                    company_id=int(r["company_id"]),
                    company_name=_blank(r["company_name"]),
                    external_record_no=_blank(r["external_record_no"]),
                    contact_id=r["contact_id"],
                    contact_name=_blank(r["contact_name"]),
                    source=_blank(r["source"]),
                    source_type=_blank(r["source_type"]),
                    status=_blank(r["status"]),
                    identified_at=_blank(r["identified_at"]),
                    assigned_rep=_blank(r["assigned_rep"]),
                    campaign_id=None,
                    recommended_campaign_id=int(rec["id"]) if rec is not None else None,
                    recommended_campaign_name=_blank(rec["campaign_name"]) if rec is not None else "",
                )
            )
        campaign_choices = _active_campaign_choices(conn, allowed)
    return UnassignedOpportunityList(
        client_id=selected,
        total=len(matched),
        limit=page_size,
        offset=page_offset,
        q=query,
        items=items,
        campaigns=campaign_choices,
    )


def _active_campaign_choices(conn, allowed: list[int]) -> list[CampaignChoice]:
    choices: list[CampaignChoice] = []
    for cid in allowed:
        for row in _selectable_campaigns(conn, int(cid)):
            if _status_from_row(row) != "Active":
                continue
            choices.append(
                CampaignChoice(
                    campaign_id=int(row["id"]),
                    campaign_name=_blank(row["campaign_name"]),
                    status=_status_from_row(row),
                    is_default=bool(row["is_default"]),
                    client_id=int(row["client_id"]),
                    recommended=False,
                )
            )
    return choices


def _stamp_campaign_assignment(conn, client_id: int, company_id: int, campaign_id: int) -> None:
    conn.execute(
        "DELETE FROM campaign_unassigned WHERE client_id = ? AND company_id = ?",
        (int(client_id), int(company_id)),
    )
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
    ).fetchone():
        oa_cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
        }
        if "target_campaign_id" in oa_cols:
            conn.execute(
                """
                UPDATE opportunity_assignments
                SET target_campaign_id = ?
                WHERE target_client_id = ? AND company_id = ?
                """,
                (int(campaign_id), int(client_id), int(company_id)),
            )


def _assign_unassigned_row(
    conn,
    *,
    campaign_id: int,
    client_id: int,
    company_id: int,
    contact_id: int | None,
    source: str,
    created_by: str,
) -> str:
    if conn.execute(
        """
        SELECT 1 FROM campaign_companies
        WHERE client_id = ? AND company_id = ?
        LIMIT 1
        """,
        (int(client_id), int(company_id)),
    ).fetchone():
        return "skipped"
    rel = conn.execute(
        f"""
        SELECT id FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
          AND {sql_active_ccr(conn, "client_company_relationships")}
        """,
        (int(client_id), int(company_id)),
    ).fetchone()
    if rel is None:
        return "failed"
    conn.execute(
        """
        INSERT INTO campaign_companies (
            campaign_id, client_id, company_id, relationship_id, notes, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(campaign_id, company_id) DO NOTHING
        """,
        (
            int(campaign_id),
            int(client_id),
            int(company_id),
            int(rel["id"]),
            _blank(source),
            created_by,
            _now(),
        ),
    )
    if contact_id:
        contact = conn.execute(
            f"""
            SELECT ct.id, ct.company_id
            FROM contacts ct
            JOIN client_company_relationships ccr
              ON ccr.company_id = ct.company_id AND ccr.client_id = ?
             AND {sql_active_ccr(conn, "ccr")}
            WHERE ct.id = ?
              AND {sql_active_contact(conn, "ct")}
            """,
            (int(client_id), int(contact_id)),
        ).fetchone()
        if contact is not None:
            conn.execute(
                """
                INSERT INTO campaign_contacts (
                    campaign_id, client_id, contact_id, company_id, notes, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id, contact_id) DO NOTHING
                """,
                (
                    int(campaign_id),
                    int(client_id),
                    int(contact_id),
                    int(contact["company_id"]),
                    _blank(source),
                    created_by,
                    _now(),
                ),
            )
    _stamp_campaign_assignment(conn, int(client_id), int(company_id), int(campaign_id))
    return "assigned"


def bulk_assign_unassigned(body: UnassignedBulkAssignRequest) -> UnassignedBulkAssignResult:
    from access import require_write_client_id

    client_id = require_write_client_id(body.client_id)
    campaign_id = int(body.campaign_id)
    if campaign_id <= 0:
        raise ValueError("Select a campaign.")
    allowed = _authorized_client_ids(client_id, write=True)
    if client_id not in allowed:
        raise PermissionError("Not authorized for this client.")
    user = resolve_staff_actor()
    created_by = _blank(user.full_name) if user is not None else ""
    query = _blank(body.q)
    exclude = {int(cid) for cid in body.exclude_company_ids if int(cid) > 0}
    requested_ids = [int(cid) for cid in body.company_ids if int(cid) > 0]
    ensure_campaigns_schema()
    with get_connection() as conn:
        campaign = _require_campaign(conn, campaign_id, allowed)
        if int(campaign["client_id"]) != client_id:
            raise ValueError("Campaign does not belong to the selected client.")
        if _status_from_row(campaign) != "Active":
            raise ValueError("Select an Active campaign.")
        matched = _matching_unassigned_rows(conn, [client_id], query)
        by_company = {int(row["company_id"]): row for row in matched}
        assigned = 0
        skipped = 0
        failed = 0
        assigned_rows: list[dict] = []
        if body.select_all_matching:
            targets = [row for row in matched if int(row["company_id"]) not in exclude]
        else:
            if not requested_ids:
                raise ValueError("Select at least one opportunity.")
            targets = []
            for cid in requested_ids:
                if cid in exclude:
                    continue
                row = by_company.get(cid)
                if row is None:
                    skipped += 1
                    continue
                targets.append(row)
        seen_companies: set[int] = set()
        for row in targets:
            company_id = int(row["company_id"])
            if company_id in seen_companies:
                skipped += 1
                continue
            seen_companies.add(company_id)
            try:
                result = _assign_unassigned_row(
                    conn,
                    campaign_id=campaign_id,
                    client_id=client_id,
                    company_id=company_id,
                    contact_id=row["contact_id"],
                    source=_blank(row["source"]) or "bulk",
                    created_by=created_by,
                )
            except Exception:
                failed += 1
                continue
            if result == "assigned":
                assigned += 1
                assigned_rows.append(row)
            elif result == "skipped":
                skipped += 1
            else:
                failed += 1
        if assigned_rows:
            first = assigned_rows[0]
            _log_campaign_membership_activity(
                conn,
                kind="bulk",
                client_id=client_id,
                campaign_id=campaign_id,
                campaign_name=_blank(campaign["campaign_name"]),
                company_id=int(first["company_id"]),
                contact_id=first.get("contact_id"),
                created_by=created_by,
                company_names=[_blank(item["company_name"]) for item in assigned_rows],
                assigned_count=assigned,
            )
        conn.commit()
        client_name = _blank(campaign["client_name"])
        campaign_name = _blank(campaign["campaign_name"])
    message = f"Assigned {assigned}. Skipped {skipped} already assigned. Failed {failed}."
    return UnassignedBulkAssignResult(
        client_id=client_id,
        client_name=client_name,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        assigned=assigned,
        skipped=skipped,
        failed=failed,
        total_requested=len(targets),
        message=message,
    )
