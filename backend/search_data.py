"""Full-text search across companies, contacts, legacy notes, and activities.

Uses SQLite FTS5. Documents are always client-scoped so Carmeco notes never
leak into another client's results.
"""

from __future__ import annotations

import re

from access import get_default_user, get_user_by_id, resolve_visibility_client_ids
from db import DB_PATH, get_connection
from models import SearchHit, SearchResponse

_FTS_SPECIAL = re.compile(r'[^\w\s]+', re.UNICODE)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _fts_query(raw: str) -> str:
    """Build a safe FTS5 MATCH query from user text (AND of token prefixes)."""
    tokens = [t for t in _FTS_SPECIAL.sub(" ", raw).split() if t]
    if not tokens:
        return ""
    # Escape double quotes in FTS tokens; prefix match for partial terms.
    cleaned = []
    for t in tokens:
        t = t.replace('"', "")
        if not t:
            continue
        cleaned.append(f"{t}*" if len(t) >= 2 else t)
    return " AND ".join(cleaned)


def rebuild_search_index(conn=None) -> int:
    """Rebuild the FTS index from live tables. Returns document count."""
    owns_conn = conn is None
    if owns_conn:
        if not _db_exists():
            return 0
        conn = get_connection()

    try:
        if not _table_exists(conn, "search_fts"):
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                    doc_type,
                    client_id UNINDEXED,
                    company_id UNINDEXED,
                    external_record_no UNINDEXED,
                    contact_id UNINDEXED,
                    source_table UNINDEXED,
                    source_id UNINDEXED,
                    company_name UNINDEXED,
                    client_name UNINDEXED,
                    client_code UNINDEXED,
                    contact_name UNINDEXED,
                    note_type UNINDEXED,
                    created_by UNINDEXED,
                    event_at UNINDEXED,
                    title,
                    body,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )

        conn.execute("DELETE FROM search_fts")

        # Companies (client-scoped via relationships)
        conn.execute(
            """
            INSERT INTO search_fts (
                doc_type, client_id, company_id, external_record_no, contact_id,
                source_table, source_id, company_name, client_name, client_code,
                contact_name, note_type, created_by, event_at, title, body
            )
            SELECT
                'company',
                cl.id,
                co.id,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no),
                NULL,
                'companies',
                co.id,
                co.company_name,
                cl.name,
                cl.code,
                '',
                '',
                '',
                COALESCE(NULLIF(co.last_updated_at, ''), co.created_at),
                co.company_name,
                trim(
                    co.company_name || ' ' ||
                    COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no) || ' ' ||
                    co.address || ' ' ||
                    co.city || ' ' ||
                    co.state || ' ' ||
                    co.zip || ' ' ||
                    co.website || ' ' ||
                    co.legacy_phone || ' ' ||
                    co.legacy_email || ' ' ||
                    COALESCE(ccr.status, '')
                )
            FROM client_company_relationships ccr
            JOIN clients cl ON cl.id = ccr.client_id
            JOIN companies co ON co.id = ccr.company_id
            """
        )

        # Contacts (visible through the company's client relationships)
        conn.execute(
            """
            INSERT INTO search_fts (
                doc_type, client_id, company_id, external_record_no, contact_id,
                source_table, source_id, company_name, client_name, client_code,
                contact_name, note_type, created_by, event_at, title, body
            )
            SELECT
                'contact',
                cl.id,
                co.id,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no),
                ct.id,
                'contacts',
                ct.id,
                co.company_name,
                cl.name,
                cl.code,
                trim(ct.first_name || ' ' || ct.last_name),
                '',
                '',
                ct.created_at,
                trim(ct.first_name || ' ' || ct.last_name),
                trim(
                    ct.first_name || ' ' || ct.last_name || ' ' ||
                    ct.title || ' ' ||
                    ct.phone || ' ' ||
                    ct.alt_phone || ' ' ||
                    ct.email || ' ' ||
                    co.company_name
                )
            FROM contacts ct
            JOIN companies co ON co.id = ct.company_id
            JOIN client_company_relationships ccr ON ccr.company_id = co.id
            JOIN clients cl ON cl.id = ccr.client_id
            WHERE TRIM(ct.external_record_no) = TRIM(
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
            )
            """
        )

        # Legacy Sales Rep Comments/Notes (unaltered body text)
        conn.execute(
            """
            INSERT INTO search_fts (
                doc_type, client_id, company_id, external_record_no, contact_id,
                source_table, source_id, company_name, client_name, client_code,
                contact_name, note_type, created_by, event_at, title, body
            )
            SELECT
                'note',
                ln.client_id,
                ln.company_id,
                COALESCE(
                    (
                        SELECT NULLIF(TRIM(ccr.external_record_no), '')
                        FROM client_company_relationships ccr
                        WHERE ccr.client_id = ln.client_id AND ccr.company_id = ln.company_id
                    ),
                    co.external_record_no
                ),
                NULL,
                'legacy_notes',
                ln.id,
                co.company_name,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM legacy_notes ln2
                        WHERE ln2.company_id = ln.company_id
                          AND ln2.id != ln.id
                          AND ln2.note_text = ln.note_text
                    )
                    THEN 'LeadMaster Legacy History'
                    ELSE cl.name
                END,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM legacy_notes ln2
                        WHERE ln2.company_id = ln.company_id
                          AND ln2.id != ln.id
                          AND ln2.note_text = ln.note_text
                    )
                    THEN 'leadmaster'
                    ELSE cl.code
                END,
                '',
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM legacy_notes ln2
                        WHERE ln2.company_id = ln.company_id
                          AND ln2.id != ln.id
                          AND ln2.note_text = ln.note_text
                    )
                    THEN 'LeadMaster Legacy History'
                    ELSE COALESCE(NULLIF(ln.source_field, ''), 'Sales Rep Comments/Notes')
                END,
                '',
                ln.created_at,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM legacy_notes ln2
                        WHERE ln2.company_id = ln.company_id
                          AND ln2.id != ln.id
                          AND ln2.note_text = ln.note_text
                    )
                    THEN 'LeadMaster Legacy History'
                    ELSE COALESCE(NULLIF(ln.source_field, ''), 'Sales Rep Comments/Notes')
                END,
                ln.note_text
            FROM legacy_notes ln
            JOIN companies co ON co.id = ln.company_id
            JOIN clients cl ON cl.id = ln.client_id
            WHERE NOT EXISTS (
                SELECT 1 FROM legacy_notes ln2
                WHERE ln2.company_id = ln.company_id
                  AND ln2.note_text = ln.note_text
                  AND ln2.id < ln.id
            )
            """
        )

        # Client-company relationship notes (follow-up / CRM notes on CCR)
        conn.execute(
            """
            INSERT INTO search_fts (
                doc_type, client_id, company_id, external_record_no, contact_id,
                source_table, source_id, company_name, client_name, client_code,
                contact_name, note_type, created_by, event_at, title, body
            )
            SELECT
                'note',
                ccr.client_id,
                ccr.company_id,
                co.external_record_no,
                NULL,
                'client_company_relationships',
                ccr.id,
                co.company_name,
                cl.name,
                cl.code,
                '',
                'Follow-up notes',
                '',
                COALESCE(NULLIF(ccr.updated_at, ''), ccr.created_at),
                'Follow-up notes',
                ccr.notes
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            JOIN clients cl ON cl.id = ccr.client_id
            WHERE TRIM(COALESCE(ccr.notes, '')) != ''
            """
        )

        # Activities (notes + outcome searchable)
        if _table_exists(conn, "activities"):
            conn.execute(
                """
                INSERT INTO search_fts (
                    doc_type, client_id, company_id, external_record_no, contact_id,
                    source_table, source_id, company_name, client_name, client_code,
                    contact_name, note_type, created_by, event_at, title, body
                )
                SELECT
                    'activity',
                    a.client_id,
                    a.company_id,
                    a.external_record_no,
                    a.contact_id,
                    'activities',
                    a.activity_id,
                    co.company_name,
                    cl.name,
                    cl.code,
                    CASE
                        WHEN ct.id IS NULL THEN ''
                        ELSE trim(ct.first_name || ' ' || ct.last_name)
                    END,
                    a.activity_type,
                    COALESCE(NULLIF(a.created_by, ''), NULLIF(u.full_name, ''), ''),
                    COALESCE(NULLIF(a.activity_at, ''), a.created_at),
                    a.activity_type,
                    trim(
                        COALESCE(a.notes, '') || ' ' ||
                        COALESCE(a.outcome, '') || ' ' ||
                        COALESCE(a.activity_type, '')
                    )
                FROM activities a
                JOIN companies co ON co.id = a.company_id
                JOIN clients cl ON cl.id = a.client_id
                LEFT JOIN contacts ct ON ct.id = a.contact_id
                LEFT JOIN users u ON u.id = a.user_id
                """
            )

        # Revenue milestones
        if _table_exists(conn, "revenue_milestones"):
            conn.execute(
                """
                INSERT INTO search_fts (
                    doc_type, client_id, company_id, external_record_no, contact_id,
                    source_table, source_id, company_name, client_name, client_code,
                    contact_name, note_type, created_by, event_at, title, body
                )
                SELECT
                    'milestone',
                    rm.client_id,
                    rm.company_id,
                    rm.external_record_no,
                    rm.contact_id,
                    'revenue_milestones',
                    rm.milestone_id,
                    co.company_name,
                    cl.name,
                    cl.code,
                    CASE
                        WHEN ct.id IS NULL THEN ''
                        ELSE trim(ct.first_name || ' ' || ct.last_name)
                    END,
                    rm.milestone_type,
                    rm.created_by,
                    rm.milestone_date,
                    rm.milestone_type,
                    trim(
                        COALESCE(rm.milestone_type, '') || ' ' ||
                        COALESCE(rm.reference_number, '') || ' ' ||
                        COALESCE(rm.source, '') || ' ' ||
                        COALESCE(rm.notes, '') || ' ' ||
                        COALESCE(CAST(rm.amount AS TEXT), '')
                    )
                FROM revenue_milestones rm
                JOIN companies co ON co.id = rm.company_id
                JOIN clients cl ON cl.id = rm.client_id
                LEFT JOIN contacts ct ON ct.id = rm.contact_id
                """
            )

        conn.commit()
        row = conn.execute("SELECT COUNT(*) AS n FROM search_fts").fetchone()
        return int(row["n"] if row else 0)
    finally:
        if owns_conn:
            conn.close()


def index_activity(activity_id: int, conn=None) -> None:
    """Upsert one activity into search_fts without a full rebuild."""
    if not _db_exists() or activity_id is None:
        return

    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        if not _table_exists(conn, "search_fts"):
            rebuild_search_index(conn)
            return

        conn.execute(
            """
            DELETE FROM search_fts
            WHERE source_table = 'activities' AND source_id = ?
            """,
            (int(activity_id),),
        )
        conn.execute(
            """
            INSERT INTO search_fts (
                doc_type, client_id, company_id, external_record_no, contact_id,
                source_table, source_id, company_name, client_name, client_code,
                contact_name, note_type, created_by, event_at, title, body
            )
            SELECT
                'activity',
                a.client_id,
                a.company_id,
                a.external_record_no,
                a.contact_id,
                'activities',
                a.activity_id,
                co.company_name,
                cl.name,
                cl.code,
                CASE
                    WHEN ct.id IS NULL THEN ''
                    ELSE trim(ct.first_name || ' ' || ct.last_name)
                END,
                a.activity_type,
                COALESCE(NULLIF(a.created_by, ''), NULLIF(u.full_name, ''), ''),
                COALESCE(NULLIF(a.activity_at, ''), a.created_at),
                a.activity_type,
                trim(
                    COALESCE(a.notes, '') || ' ' ||
                    COALESCE(a.outcome, '') || ' ' ||
                    COALESCE(a.activity_type, '')
                )
            FROM activities a
            JOIN companies co ON co.id = a.company_id
            JOIN clients cl ON cl.id = a.client_id
            LEFT JOIN contacts ct ON ct.id = a.contact_id
            LEFT JOIN users u ON u.id = a.user_id
            WHERE a.activity_id = ?
            """,
            (int(activity_id),),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def index_milestone(milestone_id: int, conn=None) -> None:
    """Upsert one revenue milestone into search_fts."""
    if not _db_exists() or milestone_id is None:
        return
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        if not _table_exists(conn, "search_fts"):
            rebuild_search_index(conn)
            return
        if not _table_exists(conn, "revenue_milestones"):
            return
        conn.execute(
            """
            DELETE FROM search_fts
            WHERE source_table = 'revenue_milestones' AND source_id = ?
            """,
            (int(milestone_id),),
        )
        conn.execute(
            """
            INSERT INTO search_fts (
                doc_type, client_id, company_id, external_record_no, contact_id,
                source_table, source_id, company_name, client_name, client_code,
                contact_name, note_type, created_by, event_at, title, body
            )
            SELECT
                'milestone',
                rm.client_id,
                rm.company_id,
                rm.external_record_no,
                rm.contact_id,
                'revenue_milestones',
                rm.milestone_id,
                co.company_name,
                cl.name,
                cl.code,
                CASE
                    WHEN ct.id IS NULL THEN ''
                    ELSE trim(ct.first_name || ' ' || ct.last_name)
                END,
                rm.milestone_type,
                rm.created_by,
                rm.milestone_date,
                rm.milestone_type,
                trim(
                    COALESCE(rm.milestone_type, '') || ' ' ||
                    COALESCE(rm.reference_number, '') || ' ' ||
                    COALESCE(rm.source, '') || ' ' ||
                    COALESCE(rm.notes, '') || ' ' ||
                    COALESCE(CAST(rm.amount AS TEXT), '')
                )
            FROM revenue_milestones rm
            JOIN companies co ON co.id = rm.company_id
            JOIN clients cl ON cl.id = rm.client_id
            LEFT JOIN contacts ct ON ct.id = rm.contact_id
            WHERE rm.milestone_id = ?
            """,
            (int(milestone_id),),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def _highlight_fallback(text: str, query: str, radius: int = 90) -> str:
    """Highlight match when FTS snippet is unavailable."""
    body = text or ""
    tokens = [t for t in _FTS_SPECIAL.sub(" ", query).split() if t]
    if not tokens:
        return body[: radius * 2]
    lower = body.lower()
    idx = -1
    matched = tokens[0]
    for token in tokens:
        pos = lower.find(token.lower())
        if pos >= 0:
            idx = pos
            matched = body[pos : pos + len(token)]
            break
    if idx < 0:
        snippet = body[: radius * 2]
        return snippet
    start = max(0, idx - radius)
    end = min(len(body), idx + len(matched) + radius)
    snippet = body[start:end]
    # Escape then wrap first case-insensitive match
    pattern = re.compile(re.escape(matched), re.IGNORECASE)
    snippet = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", snippet, count=1)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return f"{prefix}{snippet}{suffix}"


def _row_to_hit(row, query: str) -> SearchHit:
    doc_type = _blank(row["doc_type"])
    snippet = _blank(row["snippet"]) if "snippet" in row.keys() else ""
    if not snippet:
        snippet = _highlight_fallback(_blank(row["body"]), query)
    # Normalize FTS ellipsis markers
    snippet = snippet.replace("\n", " ")
    return SearchHit(
        doc_type=doc_type,
        client_id=int(row["client_id"]),
        client_name=_blank(row["client_name"]),
        client_code=_blank(row["client_code"]),
        company_id=int(row["company_id"]) if row["company_id"] is not None else None,
        company_name=_blank(row["company_name"]),
        external_record_no=_blank(row["external_record_no"]),
        contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
        contact_name=_blank(row["contact_name"]) or None,
        source_table=_blank(row["source_table"]),
        source_id=int(row["source_id"]) if row["source_id"] is not None else None,
        note_type=_blank(row["note_type"]) or None,
        title=_blank(row["title"]),
        snippet=snippet,
        event_at=_blank(row["event_at"]) or None,
        created_by=_blank(row["created_by"]) or None,
        workspace_path=(
            (
                f"/companies/{_blank(row['external_record_no'])}"
                + f"?client_id={int(row['client_id'])}"
            )
            if _blank(row["external_record_no"])
            else None
        ),
        focus_target=_focus_target(
            _blank(row["source_table"]),
            int(row["source_id"]) if row["source_id"] is not None else None,
            doc_type,
        ),
    )


def _focus_target(source_table: str, source_id: int | None, doc_type: str) -> str | None:
    if source_id is None:
        return None
    if source_table == "legacy_notes":
        return f"legacy-note-{source_id}"
    if source_table == "activities":
        return f"activity-{source_id}"
    if source_table == "revenue_milestones":
        return f"milestone-{source_id}"
    if source_table == "client_company_relationships":
        return "workspace-notes"
    if doc_type == "contact":
        return f"contact-{source_id}"
    return None


def search(
    query: str,
    *,
    user_id: int | None = None,
    client_id: int | None = None,
    company_id: int | None = None,
    filter_user_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    activity_type: str | None = None,
    status: str | None = None,
    all_clients: bool = False,
    had_appointment: bool = False,
    had_quote: bool = False,
    had_purchase_order: bool = False,
    had_weblead: bool = False,
    is_hot: bool = False,
    limit: int = 50,
) -> SearchResponse:
    raw_q = (query or "").strip()
    has_milestone_filters = any(
        [had_appointment, had_quote, had_purchase_order, had_weblead, is_hot]
    )
    if not raw_q and not has_milestone_filters:
        return SearchResponse(query="", total=0)

    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")

    from access import user_is_internal_northstar

    # Visibility vs ownership: internal NorthStar employees search across all
    # clients by default so Carmeco notes remain discoverable while working Brown.
    # External/client-facing users are restricted to assigned/selected clients.
    if user_is_internal_northstar(user) and (all_clients or client_id is None):
        client_ids = resolve_visibility_client_ids(
            user.id, selected_client_id=None, purpose="search"
        )
        mode = "internal_cross_client"
    elif user_is_internal_northstar(user) and client_id is not None:
        # Explicit single-client search filter (optional)
        client_ids = resolve_visibility_client_ids(
            user.id, selected_client_id=client_id, purpose="work"
        )
        mode = "selected_client"
    else:
        from access import resolve_dashboard_client_ids

        # Client-facing users never expand to all NorthStar clients.
        client_ids = resolve_dashboard_client_ids(
            user.id,
            selected_client_id=client_id,
            all_clients=False,
        )
        mode = "selected_client" if client_id is not None else "all_my_clients"
    if not client_ids:
        return SearchResponse(query=raw_q, total=0, mode=mode)

    match = _fts_query(raw_q) if raw_q else None
    if raw_q and not match:
        return SearchResponse(query=raw_q, total=0, mode=mode)

    if not _db_exists():
        return SearchResponse(query=raw_q, total=0, mode=mode)

    with get_connection() as conn:
        if not _table_exists(conn, "search_fts"):
            rebuild_search_index(conn)
        else:
            count_row = conn.execute("SELECT COUNT(*) AS n FROM search_fts").fetchone()
            if not count_row or int(count_row["n"]) == 0:
                rebuild_search_index(conn)

        placeholders = ",".join("?" * len(client_ids))
        filters = [f"search_fts.client_id IN ({placeholders})"]
        params: list[object] = [*client_ids]
        if match:
            filters.insert(0, "search_fts MATCH ?")
            params = [match, *params]

        if company_id is not None:
            filters.append("search_fts.company_id = ?")
            params.append(company_id)
        if date_from:
            filters.append("search_fts.event_at >= ?")
            params.append(date_from)
        if date_to:
            filters.append("search_fts.event_at <= ?")
            params.append(date_to)
        if activity_type:
            filters.append(
                "(search_fts.doc_type IN ('activity', 'milestone') AND search_fts.note_type = ?)"
            )
            params.append(activity_type)
        if filter_user_id is not None:
            filters.append(
                """
                (
                  search_fts.source_table != 'activities'
                  OR EXISTS (
                    SELECT 1 FROM activities a
                    WHERE a.activity_id = search_fts.source_id
                      AND a.user_id = ?
                  )
                )
                """
            )
            params.append(filter_user_id)
        if status:
            filters.append(
                """
                EXISTS (
                  SELECT 1 FROM client_company_relationships ccr
                  WHERE ccr.client_id = search_fts.client_id
                    AND ccr.company_id = search_fts.company_id
                    AND lower(ccr.status) = lower(?)
                )
                """
            )
            params.append(status)

        for enabled, mtype in (
            (had_appointment, "Appointment Set"),
            (had_quote, "Quote"),
            (had_purchase_order, "Purchase Order"),
            (had_weblead, "WebLead"),
        ):
            if not enabled:
                continue
            filters.append(
                f"""
                EXISTS (
                  SELECT 1 FROM revenue_milestones rm
                  WHERE rm.company_id = search_fts.company_id
                    AND rm.client_id IN ({placeholders})
                    AND rm.milestone_type = ?
                )
                """
            )
            params.extend([*client_ids, mtype])
        if is_hot:
            filters.append(
                f"""
                EXISTS (
                  SELECT 1 FROM client_company_relationships ccr
                  WHERE ccr.company_id = search_fts.company_id
                    AND ccr.client_id IN ({placeholders})
                    AND lower(trim(ccr.status)) = 'hot prospect'
                )
                """
            )
            params.extend(client_ids)

        where_sql = " AND ".join(filters)
        per_group = max(8, min(40, (max(1, min(limit, 100)) + 2) // 3))
        select_cols = """
                search_fts.doc_type,
                search_fts.client_id,
                search_fts.company_id,
                search_fts.external_record_no,
                search_fts.contact_id,
                search_fts.source_table,
                search_fts.source_id,
                search_fts.company_name,
                search_fts.client_name,
                search_fts.client_code,
                search_fts.contact_name,
                search_fts.note_type,
                search_fts.created_by,
                search_fts.event_at,
                search_fts.title,
                search_fts.body,
                snippet(search_fts, 15, '<mark>', '</mark>', '…', 14) AS snippet,
                bm25(search_fts) AS rank
        """
        rows = []
        order_by = "rank" if match else "search_fts.company_name COLLATE NOCASE"
        for type_filter, type_limit in (
            ("search_fts.doc_type = 'company'", per_group),
            ("search_fts.doc_type = 'contact'", per_group),
            ("search_fts.doc_type IN ('note', 'activity', 'milestone')", max(per_group, 16)),
        ):
            sql = f"""
                SELECT {select_cols}
                FROM search_fts
                WHERE {where_sql}
                  AND {type_filter}
                ORDER BY {order_by}
                LIMIT ?
            """
            group_params = list(params) + [type_limit]
            rows.extend(conn.execute(sql, group_params).fetchall())

    hits = [_row_to_hit(r, raw_q) for r in rows]

    # Safety net: identical shared LeadMaster notes → one search hit.
    seen_shared_legacy: set[int] = set()
    deduped_hits: list[SearchHit] = []
    for h in hits:
        if (
            h.source_table == "legacy_notes"
            and h.company_id is not None
            and (
                h.note_type == "LeadMaster Legacy History"
                or h.client_code == "leadmaster"
                or h.client_name == "LeadMaster Legacy History"
            )
        ):
            if h.company_id in seen_shared_legacy:
                continue
            seen_shared_legacy.add(h.company_id)
        deduped_hits.append(h)
    hits = deduped_hits

    if not raw_q:
        seen = set()
        deduped = []
        for h in hits:
            key = (h.company_id, h.client_id, h.doc_type)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(h)
        hits = deduped

    companies = [h for h in hits if h.doc_type == "company"]
    contacts = [h for h in hits if h.doc_type == "contact"]
    notes = [h for h in hits if h.doc_type in {"note", "activity", "milestone"}]

    return SearchResponse(
        query=raw_q,
        mode=mode,
        client_ids=client_ids,
        total=len(hits),
        companies=companies,
        contacts=contacts,
        notes=notes,
    )
