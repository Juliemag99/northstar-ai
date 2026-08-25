"""Activity / follow-up data access — scoped to client_company_relationships."""

from __future__ import annotations

from datetime import date, datetime

from db import DB_PATH, get_connection
from models import (
    Activity,
    ActivityCreateRequest,
    ActivityTimelineResponse,
    ActivityTimelineRow,
)

ACTIVITY_TYPES = frozenset(
    {
        "Call",
        "Email",
        "Voicemail",
        "Follow-Up",
        "Follow-Up Completed",
        "Follow-Up Rescheduled",
        "Task Completed",
        "Appointment",
        "Appointment Created",
        "Appointment Rescheduled",
        "Appointment Cancelled",
        "Appointment Completed",
        "Note",
        "Status Change",
        "Contact Assignment",
        "Campaign Assignment",
        "Campaign Bulk Assignment",
        "Campaign Reassignment",
        "Campaign Removal",
    }
)

ACTIVITY_TIMELINE_TYPES = (
    "Call",
    "Note",
    "Email",
    "Voicemail",
    "Status Change",
    "Follow-Up",
    "Follow-Up Completed",
    "Follow-Up Rescheduled",
    "Task Completed",
    "Appointment Created",
    "Appointment Rescheduled",
    "Appointment Cancelled",
    "Appointment Completed",
    "Contact Assignment",
    "Campaign Assignment",
    "Campaign Bulk Assignment",
    "Campaign Reassignment",
    "Campaign Removal",
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def _resolve_client(conn, client_name: str = "Carmeco", client_id: int | None = None):
    if client_id is not None:
        return conn.execute(
            "SELECT * FROM clients WHERE id = ?", (int(client_id),)
        ).fetchone()
    code = client_name.strip().lower()
    if code in {"carmeco", "carmeco metal"}:
        code = "carmeco"
    if code in {"brown", "brown industries"}:
        code = "brown"
    return conn.execute(
        "SELECT * FROM clients WHERE code = ? OR lower(name) = lower(?)",
        (code, client_name.strip()),
    ).fetchone()


def _resolve_company_relationship(conn, *, client_id: int, record_no: str):
    """Resolve company + relationship using client-specific Record No."""
    key = record_no.strip()
    row = conn.execute(
        """
        SELECT
            ccr.id AS relationship_id,
            ccr.company_id,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                AS relationship_record_no,
            co.external_record_no AS master_record_no
        FROM client_company_relationships ccr
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.client_id = ?
          AND (
                TRIM(ccr.external_record_no) = ?
             OR co.external_record_no = ?
          )
        LIMIT 1
        """,
        (client_id, key, key),
    ).fetchone()
    return row


def _row_to_activity(row) -> Activity:
    user_id = row["user_id"] if "user_id" in row.keys() else None
    return Activity(
        activity_id=int(row["activity_id"]),
        client_id=int(row["client_id"]),
        external_record_no=_blank(row["external_record_no"]),
        company_id=int(row["company_id"]),
        relationship_id=int(row["relationship_id"]),
        user_id=int(user_id) if user_id is not None else None,
        contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
        activity_type=_blank(row["activity_type"]),
        activity_at=_blank(row["activity_at"]),
        outcome=_blank(row["outcome"]),
        notes=_blank(row["notes"]),
        follow_up_at=_blank(row["follow_up_at"]) or None,
        assigned_user=_blank(row["assigned_user"]),
        created_by=_blank(row["created_by"]),
        created_at=_blank(row["created_at"]),
        updated_at=_blank(row["updated_at"]),
    )


def _today_bounds() -> tuple[str, str]:
    """Return ISO date start/end strings for today (local calendar day)."""
    today = date.today().isoformat()
    return f"{today} 00:00:00", f"{today} 23:59:59"


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _resolve_user_id(conn, body: ActivityCreateRequest) -> int | None:
    if body.user_id is not None:
        row = conn.execute(
            "SELECT id, full_name FROM users WHERE id = ? AND active = 1",
            (body.user_id,),
        ).fetchone()
        if row is None:
            raise ValueError("user_id not found or inactive.")
        return int(row["id"])

    # Prefer matching created_by / assigned_user display name, else default Julie
    for name in (body.created_by, body.assigned_user, "Julie Magnani"):
        key = name.strip()
        if not key:
            continue
        row = conn.execute(
            "SELECT id FROM users WHERE lower(full_name) = lower(?) AND active = 1",
            (key,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    return None


def insert_activity_row(
    conn,
    *,
    client_id: int,
    company_id: int,
    relationship_id: int,
    external_record_no: str,
    user_id: int | None,
    contact_id: int | None,
    activity_type: str,
    outcome: str = "",
    notes: str = "",
    follow_up_at: str | None = None,
    assigned_user: str = "",
    created_by: str = "",
    activity_at: str | None = None,
) -> int:
    """Insert one activity row on an existing connection. Caller commits."""
    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO activities (
            client_id, company_id, relationship_id, external_record_no,
            user_id, contact_id, activity_type, activity_at, outcome, notes,
            follow_up_at, assigned_user, created_by, created_at, updated_at,
            follow_up_completed, completion_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'open')
        """,
        (
            int(client_id),
            int(company_id),
            int(relationship_id),
            external_record_no,
            user_id,
            contact_id,
            activity_type,
            (activity_at or "").strip() or now,
            (outcome or "").strip(),
            notes or "",
            follow_up_at,
            assigned_user or created_by or "",
            created_by or assigned_user or "",
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def create_activity(body: ActivityCreateRequest) -> Activity:
    """Create an activity owned by the named client's company relationship."""
    if not _db_exists():
        raise LookupError("Database not found.")

    activity_type = body.activity_type.strip()
    if activity_type not in ACTIVITY_TYPES:
        raise ValueError(
            f"Unsupported activity_type '{body.activity_type}'. "
            f"Allowed: {', '.join(sorted(ACTIVITY_TYPES))}."
        )

    record_no = body.external_record_no.strip()
    if not record_no:
        raise ValueError("external_record_no (company Record No.) is required.")

    activity_at = body.activity_at.strip() or _now_iso()
    follow_up_at = body.follow_up_at.strip() if body.follow_up_at else None
    if follow_up_at == "":
        follow_up_at = None

    with get_connection() as conn:
        client_row = _resolve_client(conn, body.client, body.client_id)
        if client_row is None:
            raise ValueError("Unknown client.")

        rel = _resolve_company_relationship(
            conn, client_id=int(client_row["id"]), record_no=record_no
        )
        if rel is None:
            raise LookupError(
                "No client-company relationship found for this Record No. "
                "Activity requires an existing client-company relationship."
            )
        company_id = int(rel["company_id"])
        relationship_id = int(rel["relationship_id"])
        rel_rn = _blank(rel["relationship_record_no"])

        contact_id = body.contact_id
        if contact_id is not None:
            contact = conn.execute(
                "SELECT id, company_id, external_record_no FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact is None:
                raise ValueError("contact_id not found.")
            if int(contact["company_id"]) != company_id:
                raise ValueError("contact_id does not belong to this company.")

        user_id = _resolve_user_id(conn, body)
        created_by = body.created_by.strip() or "Julie Magnani"
        assigned_user = body.assigned_user.strip() or created_by
        if user_id is not None:
            user_row = conn.execute(
                "SELECT full_name FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user_row is not None and not body.created_by.strip():
                created_by = _blank(user_row["full_name"]) or created_by
        now = _now_iso()

        cur = conn.execute(
            """
            INSERT INTO activities (
                client_id, company_id, relationship_id, external_record_no,
                user_id, contact_id, activity_type, activity_at, outcome, notes,
                follow_up_at, assigned_user, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(client_row["id"]),
                company_id,
                relationship_id,
                rel_rn,
                user_id,
                contact_id,
                activity_type,
                activity_at,
                body.outcome.strip(),
                body.notes,
                follow_up_at,
                assigned_user,
                created_by,
                now,
                now,
            ),
        )
        activity_id = int(cur.lastrowid)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()

    from search_data import index_activity

    index_activity(activity_id)

    from milestones_data import ensure_appointment_milestone_from_activity

    ensure_appointment_milestone_from_activity(activity_id)
    return _row_to_activity(row)


def list_activities_for_company(
    record_no: str,
    *,
    client: str = "Carmeco",
    client_id: int | None = None,
) -> list[Activity]:
    """Activities for one company Record No., scoped to the named client."""
    if not _db_exists() or not record_no.strip():
        return []

    with get_connection() as conn:
        client_row = _resolve_client(conn, client, client_id)
        if client_row is None:
            return []
        rel = _resolve_company_relationship(
            conn, client_id=int(client_row["id"]), record_no=record_no
        )
        if rel is None:
            return []
        rows = conn.execute(
            """
            SELECT a.* FROM activities a
            WHERE a.client_id = ? AND a.company_id = ?
            ORDER BY a.activity_at DESC, a.activity_id DESC
            """,
            (int(client_row["id"]), int(rel["company_id"])),
        ).fetchall()
    return [_row_to_activity(r) for r in rows]


def list_activities_due_today(*, client: str = "Carmeco") -> list[Activity]:
    """Follow-ups whose follow_up_at falls on today's calendar date."""
    if not _db_exists():
        return []
    start, end = _today_bounds()
    with get_connection() as conn:
        client_row = _resolve_client(conn, client)
        if client_row is None:
            return []
        rows = conn.execute(
            """
            SELECT a.* FROM activities a
            WHERE a.client_id = ?
              AND a.follow_up_at IS NOT NULL
              AND TRIM(a.follow_up_at) != ''
                  AND a.follow_up_at >= ?
              AND a.follow_up_at <= ?
              AND COALESCE(a.follow_up_completed, 0) = 0
            ORDER BY a.follow_up_at ASC, a.activity_id ASC
            """,
            (int(client_row["id"]), start, end),
        ).fetchall()
    return [_row_to_activity(r) for r in rows]


def list_overdue_follow_ups(*, client: str = "Carmeco") -> list[Activity]:
    """Follow-ups with follow_up_at before the start of today."""
    if not _db_exists():
        return []
    start, _ = _today_bounds()
    with get_connection() as conn:
        client_row = _resolve_client(conn, client)
        if client_row is None:
            return []
        rows = conn.execute(
            """
            SELECT a.* FROM activities a
            WHERE a.client_id = ?
              AND a.follow_up_at IS NOT NULL
              AND TRIM(a.follow_up_at) != ''
                  AND a.follow_up_at < ?
              AND COALESCE(a.follow_up_completed, 0) = 0
            ORDER BY a.follow_up_at ASC, a.activity_id ASC
            """,
            (int(client_row["id"]), start),
        ).fetchall()
    return [_row_to_activity(r) for r in rows]


def list_upcoming_follow_ups(*, client: str = "Carmeco") -> list[Activity]:
    """Follow-ups with follow_up_at after the end of today."""
    if not _db_exists():
        return []
    _, end = _today_bounds()
    with get_connection() as conn:
        client_row = _resolve_client(conn, client)
        if client_row is None:
            return []
        rows = conn.execute(
            """
            SELECT a.* FROM activities a
            WHERE a.client_id = ?
              AND a.follow_up_at IS NOT NULL
              AND TRIM(a.follow_up_at) != ''
              AND a.follow_up_at > ?
            ORDER BY a.follow_up_at ASC, a.activity_id ASC
            """,
            (int(client_row["id"]), end),
        ).fetchall()
    return [_row_to_activity(r) for r in rows]


def _display_type_sql(alias: str = "a") -> str:
    return f"""
        CASE
          WHEN lower(trim({alias}.activity_type)) = 'note'
               AND lower(COALESCE({alias}.outcome, '')) LIKE '%follow-up completed%'
            THEN 'Follow-Up Completed'
          WHEN lower(trim({alias}.activity_type)) = 'follow-up'
               AND lower(COALESCE({alias}.completion_status, 'open')) IN
                   ('completed', 'complete', 'done')
            THEN 'Follow-Up Completed'
          WHEN lower(trim({alias}.activity_type)) = 'note'
               AND lower(COALESCE({alias}.notes, '')) LIKE '%added shared contact%'
            THEN 'Contact Assignment'
          WHEN lower(trim({alias}.activity_type)) IN ('appointment', 'appointment created')
            THEN 'Appointment Created'
          WHEN lower(trim({alias}.activity_type)) = 'task completed'
            THEN 'Follow-Up Completed'
          ELSE trim({alias}.activity_type)
        END
    """


def list_client_activities(
    client_id: int,
    *,
    activity_type: str = "",
    assigned_user: str = "",
    user_id: int | None = None,
    company_id: int | None = None,
    contact_id: int | None = None,
    company: str = "",
    contact: str = "",
    date_from: str = "",
    date_to: str = "",
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> ActivityTimelineResponse:
    """Newest-first Active Client activity timeline. Never includes another client."""
    from access import get_default_user, user_can_access_client

    cid = int(client_id)
    if cid <= 0:
        raise ValueError("Active Client is required.")
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, cid) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")

    page_size = max(1, min(int(limit or 50), 200))
    page_offset = max(0, int(offset or 0))
    wanted_type = _blank(activity_type)
    user_filter = _blank(assigned_user)
    company_q = _blank(company)
    contact_q = _blank(contact)
    date_start = _blank(date_from)[:10]
    date_end = _blank(date_to)[:10]
    needle = _blank(q).lower()

    if not _db_exists():
        return ActivityTimelineResponse(
            client_id=cid,
            limit=page_size,
            offset=page_offset,
            activity_types=list(ACTIVITY_TIMELINE_TYPES),
        )

    display_sql = _display_type_sql("a")
    with get_connection() as conn:
        unions = [
            f"""
            SELECT
                'activity:' || a.activity_id AS item_key,
                'activity' AS source,
                a.activity_id AS activity_id,
                a.client_id AS client_id,
                a.company_id AS company_id,
                a.relationship_id AS relationship_id,
                COALESCE(NULLIF(TRIM(a.external_record_no), ''), co.external_record_no, '')
                    AS external_record_no,
                COALESCE(co.company_name, '') AS company_name,
                a.contact_id AS contact_id,
                TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, ''))
                    AS contact_name,
                {display_sql} AS display_type,
                COALESCE(NULLIF(TRIM(a.activity_at), ''), a.created_at, '') AS activity_at,
                COALESCE(
                    NULLIF(TRIM(u.full_name), ''),
                    NULLIF(TRIM(a.assigned_user), ''),
                    NULLIF(TRIM(a.created_by), ''),
                    ''
                ) AS user_name,
                a.user_id AS user_id,
                COALESCE(a.outcome, '') AS outcome,
                COALESCE(a.notes, '') AS notes,
                COALESCE(ccr.status, '') AS status
            FROM activities a
            JOIN companies co ON co.id = a.company_id
            LEFT JOIN contacts ct ON ct.id = a.contact_id
            LEFT JOIN users u ON u.id = a.user_id
            LEFT JOIN client_company_relationships ccr
              ON ccr.id = a.relationship_id AND ccr.client_id = a.client_id
            WHERE a.client_id = ?
            """
        ]
        union_params: list[object] = [cid]

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_audit_log'"
        ).fetchone():
            unions.append(
                """
                SELECT
                    'audit:' || aud.id AS item_key,
                    'audit' AS source,
                    NULL AS activity_id,
                    ccr.client_id AS client_id,
                    ccr.company_id AS company_id,
                    ccr.id AS relationship_id,
                    COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                        AS external_record_no,
                    COALESCE(co.company_name, '') AS company_name,
                    NULL AS contact_id,
                    '' AS contact_name,
                    'Status Change' AS display_type,
                    COALESCE(aud.changed_at, '') AS activity_at,
                    COALESCE(aud.changed_by, '') AS user_name,
                    NULL AS user_id,
                    TRIM(COALESCE(aud.old_value, '') || ' → ' || COALESCE(aud.new_value, ''))
                        AS outcome,
                    TRIM('Status changed from ' || COALESCE(aud.old_value, '') ||
                         ' to ' || COALESCE(aud.new_value, '')) AS notes,
                    COALESCE(aud.new_value, '') AS status
                FROM field_audit_log aud
                JOIN clients cl ON lower(cl.code) = lower(aud.client_code)
                JOIN client_company_relationships ccr ON ccr.client_id = cl.id
                JOIN companies co ON co.id = ccr.company_id
                WHERE ccr.client_id = ?
                  AND lower(TRIM(aud.field_name)) = 'status'
                  AND (
                        TRIM(ccr.external_record_no) = TRIM(aud.external_record_no)
                     OR TRIM(co.external_record_no) = TRIM(aud.external_record_no)
                  )
                """
            )
            union_params.append(cid)

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appointments'"
        ).fetchone():
            unions.append(
                """
                SELECT
                    'appointment:' || ap.id AS item_key,
                    'appointment' AS source,
                    ap.activity_id AS activity_id,
                    ap.client_id AS client_id,
                    ap.company_id AS company_id,
                    ap.relationship_id AS relationship_id,
                    COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no, '')
                        AS external_record_no,
                    COALESCE(co.company_name, '') AS company_name,
                    ap.contact_id AS contact_id,
                    TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, ''))
                        AS contact_name,
                    CASE
                      WHEN lower(trim(ap.status)) = 'cancelled'
                        THEN 'Appointment Cancelled'
                      WHEN lower(trim(ap.status)) = 'completed'
                        THEN 'Appointment Completed'
                      ELSE 'Appointment Created'
                    END AS display_type,
                    CASE
                      WHEN lower(trim(ap.status)) = 'cancelled'
                        THEN COALESCE(NULLIF(TRIM(ap.cancelled_at), ''), ap.updated_at, ap.created_at, '')
                      WHEN lower(trim(ap.status)) = 'completed'
                        THEN COALESCE(NULLIF(TRIM(ap.completed_at), ''), ap.updated_at, ap.created_at, '')
                      ELSE COALESCE(NULLIF(TRIM(ap.created_at), ''), '')
                    END AS activity_at,
                    COALESCE(
                        CASE
                          WHEN lower(trim(ap.status)) = 'cancelled'
                            THEN NULLIF(TRIM(ap.cancelled_by), '')
                          WHEN lower(trim(ap.status)) = 'completed'
                            THEN NULLIF(TRIM(ap.completed_by), '')
                          ELSE NULL
                        END,
                        NULLIF(TRIM(ap.created_by), ''),
                        ''
                    ) AS user_name,
                    ap.revenue_specialist_user_id AS user_id,
                    COALESCE(
                        CASE
                          WHEN lower(trim(ap.status)) = 'cancelled'
                            THEN ap.cancellation_reason
                          ELSE ap.outcome
                        END,
                        ap.source,
                        ''
                    ) AS outcome,
                    COALESCE(ap.notes, '') AS notes,
                    COALESCE(ap.status, '') AS status
                FROM appointments ap
                JOIN companies co ON co.id = ap.company_id
                LEFT JOIN contacts ct ON ct.id = ap.contact_id
                LEFT JOIN client_company_relationships ccr
                  ON ccr.id = ap.relationship_id AND ccr.client_id = ap.client_id
                WHERE ap.client_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM activities a2
                    WHERE a2.client_id = ap.client_id
                      AND (
                        (
                          lower(trim(ap.status)) = 'cancelled'
                          AND lower(trim(a2.activity_type)) = 'appointment cancelled'
                          AND (
                            a2.activity_id = ap.cancel_activity_id
                            OR (
                              ap.cancel_activity_id IS NULL
                              AND a2.company_id = ap.company_id
                              AND (
                                (ap.contact_id IS NOT NULL AND a2.contact_id = ap.contact_id)
                                OR (ap.contact_id IS NULL AND a2.contact_id IS NULL)
                              )
                            )
                          )
                        )
                        OR (
                          lower(trim(ap.status)) = 'completed'
                          AND lower(trim(a2.activity_type)) = 'appointment completed'
                          AND (
                            a2.activity_id = ap.complete_activity_id
                            OR (
                              ap.complete_activity_id IS NULL
                              AND a2.company_id = ap.company_id
                              AND (
                                (ap.contact_id IS NOT NULL AND a2.contact_id = ap.contact_id)
                                OR (ap.contact_id IS NULL AND a2.contact_id IS NULL)
                              )
                            )
                          )
                        )
                        OR (
                          lower(trim(ap.status)) NOT IN ('cancelled', 'completed')
                          AND lower(trim(a2.activity_type)) IN ('appointment', 'appointment created')
                          AND a2.company_id = ap.company_id
                          AND (
                            (ap.contact_id IS NOT NULL AND a2.contact_id = ap.contact_id)
                            OR (ap.contact_id IS NULL AND a2.contact_id IS NULL)
                            OR a2.activity_id = ap.activity_id
                          )
                        )
                      )
                  )
                """
            )
            union_params.append(cid)

        events_sql = " UNION ALL ".join(unions)
        where = ["1 = 1"]
        filter_params: list[object] = []

        type_key = wanted_type.lower()
        if type_key == "task completed":
            where.append("lower(e.display_type) IN ('follow-up completed', 'task completed')")
        elif type_key == "appointment created":
            where.append("lower(e.display_type) IN ('appointment created', 'appointment')")
        elif wanted_type:
            where.append("lower(e.display_type) = ?")
            filter_params.append(type_key)

        if user_filter:
            where.append("lower(e.user_name) = lower(?)")
            filter_params.append(user_filter)
        if user_id is not None and int(user_id) > 0:
            where.append("e.user_id = ?")
            filter_params.append(int(user_id))
        if company_id is not None and int(company_id) > 0:
            where.append("e.company_id = ?")
            filter_params.append(int(company_id))
        if contact_id is not None and int(contact_id) > 0:
            where.append("e.contact_id = ?")
            filter_params.append(int(contact_id))
        if company_q:
            where.append(
                "(lower(e.company_name) LIKE ? OR lower(e.external_record_no) LIKE ?)"
            )
            like = f"%{company_q.lower()}%"
            filter_params.extend([like, like])
        if contact_q:
            where.append("lower(e.contact_name) LIKE ?")
            filter_params.append(f"%{contact_q.lower()}%")
        if date_start:
            where.append("substr(e.activity_at, 1, 10) >= ?")
            filter_params.append(date_start)
        if date_end:
            where.append("substr(e.activity_at, 1, 10) <= ?")
            filter_params.append(date_end)
        if needle:
            where.append(
                """
                (
                    lower(e.company_name) LIKE ?
                 OR lower(e.contact_name) LIKE ?
                 OR lower(e.notes) LIKE ?
                 OR lower(e.outcome) LIKE ?
                 OR lower(e.display_type) LIKE ?
                 OR lower(e.external_record_no) LIKE ?
                 OR lower(e.user_name) LIKE ?
                )
                """
            )
            like = f"%{needle}%"
            filter_params.extend([like] * 7)

        where_sql = " AND ".join(where)
        filtered_sql = f"""
            SELECT * FROM (
                {events_sql}
            ) e
            WHERE {where_sql}
        """
        total_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({filtered_sql})",
            [*union_params, *filter_params],
        ).fetchone()
        total = int(total_row["n"] or 0) if total_row is not None else 0
        rows = conn.execute(
            f"""
            {filtered_sql}
            ORDER BY e.activity_at DESC, e.item_key DESC
            LIMIT ? OFFSET ?
            """,
            [*union_params, *filter_params, page_size, page_offset],
        ).fetchall()

        type_rows = conn.execute(
            f"""
            SELECT DISTINCT display_type FROM (
                {events_sql}
            ) e
            WHERE TRIM(display_type) != ''
            ORDER BY display_type COLLATE NOCASE
            """,
            union_params,
        ).fetchall()
        user_rows = conn.execute(
            f"""
            SELECT DISTINCT user_name FROM (
                {events_sql}
            ) e
            WHERE TRIM(user_name) != ''
            ORDER BY user_name COLLATE NOCASE
            """,
            union_params,
        ).fetchall()

    seen_types = {_blank(r["display_type"]) for r in type_rows if _blank(r["display_type"])}
    types = list(ACTIVITY_TIMELINE_TYPES)
    for name in sorted(seen_types, key=str.lower):
        if name not in types:
            types.append(name)
    users = [_blank(r["user_name"]) for r in user_rows if _blank(r["user_name"])]
    items = [
        ActivityTimelineRow(
            item_key=_blank(r["item_key"]),
            source=_blank(r["source"]) or "activity",
            activity_id=int(r["activity_id"]) if r["activity_id"] is not None else None,
            client_id=int(r["client_id"]),
            company_id=int(r["company_id"]),
            relationship_id=int(r["relationship_id"] or 0),
            external_record_no=_blank(r["external_record_no"]),
            company_name=_blank(r["company_name"]),
            contact_id=int(r["contact_id"]) if r["contact_id"] is not None else None,
            contact_name=_blank(r["contact_name"]),
            activity_type=_blank(r["display_type"]),
            activity_at=_blank(r["activity_at"]),
            user_name=_blank(r["user_name"]),
            user_id=int(r["user_id"]) if r["user_id"] is not None else None,
            outcome=_blank(r["outcome"]),
            notes=_blank(r["notes"]),
            status=_blank(r["status"]),
        )
        for r in rows
    ]
    return ActivityTimelineResponse(
        client_id=cid,
        count=len(items),
        total=total,
        limit=page_size,
        offset=page_offset,
        activity_types=types,
        users=users,
        items=items,
    )
