"""NorthStar Shared History — internal cross-client visibility on a master company.

Visibility and ownership are different:
- Internal NorthStar employees may VIEW notes/activity from all clients on a company.
- New notes/calls/status changes always write to the Active Client relationship only.

Legacy LeadMaster notes that are identical across clients are shown once as shared
company-level history. Distinct legacy texts remain labeled by originating client.
"""

from __future__ import annotations

from staff_context import resolve_staff_actor

from access import (
    get_default_user,
    get_user_by_id,
    resolve_visibility_client_ids,
    user_is_internal_northstar,
)
from db import DB_PATH, get_connection
from models import (
    SharedHistoryClientSummary,
    SharedHistoryItem,
    SharedHistoryResponse,
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def normalize_legacy_note_text(text: str) -> str:
    """Normalize imported LeadMaster note text for duplicate detection."""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in cleaned.split("\n")]
    return "\n".join(lines).strip()


def _sort_key(item: SharedHistoryItem) -> tuple:
    return (item.event_at or "", item.item_key)


def list_shared_history(
    record_no: str,
    *,
    user_id: int | None = None,
    working_client_id: int | None = None,
    filter_client_id: int | None = None,
) -> SharedHistoryResponse:
    """Cross-client history with legacy dedupe and newest-first NorthStar activity."""
    if not _db_exists() or not record_no.strip():
        return SharedHistoryResponse(company_id=0)

    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")

    visible_ids = resolve_visibility_client_ids(
        user.id, selected_client_id=working_client_id, purpose="shared_history"
    )
    if not visible_ids:
        return SharedHistoryResponse(company_id=0, can_view_cross_client=False)

    key = record_no.strip()
    with get_connection() as conn:
        company = None
        if working_client_id is not None:
            company = conn.execute(
                """
                SELECT co.id, co.company_name
                FROM client_company_relationships ccr
                JOIN companies co ON co.id = ccr.company_id
                WHERE ccr.client_id = ?
                  AND (
                        TRIM(ccr.external_record_no) = ?
                     OR co.external_record_no = ?
                  )
                LIMIT 1
                """,
                (int(working_client_id), key, key),
            ).fetchone()
        if company is None:
            company = conn.execute(
                """
                SELECT co.id, co.company_name
                FROM companies co
                WHERE co.external_record_no = ?
                """,
                (key,),
            ).fetchone()
        if company is None:
            company = conn.execute(
                """
                SELECT co.id, co.company_name
                FROM client_company_relationships ccr
                JOIN companies co ON co.id = ccr.company_id
                WHERE TRIM(ccr.external_record_no) = ?
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if company is None:
            raise LookupError("Company not found for Record No.")

        company_id = int(company["id"])
        placeholders = ",".join("?" * len(visible_ids))
        client_rows = list(
            conn.execute(
                f"""
                SELECT
                    cl.id AS client_id,
                    cl.code AS client_code,
                    cl.name AS client_name,
                    COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                        AS external_record_no,
                    COALESCE(ccr.status, '') AS status,
                    COALESCE(ccr.is_hot, 0) AS is_hot
                FROM client_company_relationships ccr
                JOIN clients cl ON cl.id = ccr.client_id
                JOIN companies co ON co.id = ccr.company_id
                WHERE ccr.company_id = ?
                  AND ccr.client_id IN ({placeholders})
                ORDER BY cl.name COLLATE NOCASE
                """,
                (company_id, *visible_ids),
            )
        )

        filter_ids = [int(r["client_id"]) for r in client_rows]
        if filter_client_id is not None:
            filter_ids = [cid for cid in filter_ids if cid == int(filter_client_id)]

        summaries = [
            SharedHistoryClientSummary(
                client_id=int(r["client_id"]),
                client_code=_blank(r["client_code"]),
                client_name=_blank(r["client_name"]),
                external_record_no=_blank(r["external_record_no"]),
                status=_blank(r["status"]),
                is_hot=bool(r["is_hot"]),
            )
            for r in conn.execute(
                f"""
                SELECT
                    cl.id AS client_id,
                    cl.code AS client_code,
                    cl.name AS client_name,
                    COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                        AS external_record_no,
                    COALESCE(ccr.status, '') AS status,
                    COALESCE(ccr.is_hot, 0) AS is_hot
                FROM client_company_relationships ccr
                JOIN clients cl ON cl.id = ccr.client_id
                JOIN companies co ON co.id = ccr.company_id
                WHERE ccr.company_id = ?
                  AND ccr.client_id IN ({placeholders})
                ORDER BY cl.name COLLATE NOCASE
                """,
                (company_id, *visible_ids),
            )
        ]

        if not filter_ids:
            return SharedHistoryResponse(
                company_id=company_id,
                company_name=_blank(company["company_name"]),
                can_view_cross_client=user_is_internal_northstar(user),
                filter_client_id=filter_client_id,
                clients=summaries,
                items=[],
                northstar_items=[],
                shared_legacy_items=[],
                distinct_legacy_items=[],
                shared_company_history_items=[],
                count=0,
            )

        cph = ",".join("?" * len(filter_ids))
        rn_by_client = {
            int(r["client_id"]): _blank(r["external_record_no"]) for r in client_rows
        }

        # --- NorthStar activities (client-specific, newest first) ---
        northstar_items: list[SharedHistoryItem] = []
        for a in conn.execute(
            f"""
            SELECT a.*, cl.code AS client_code, cl.name AS client_name
            FROM activities a
            JOIN clients cl ON cl.id = a.client_id
            WHERE a.company_id = ? AND a.client_id IN ({cph})
            ORDER BY a.activity_at DESC, a.created_at DESC, a.activity_id DESC
            """,
            (company_id, *filter_ids),
        ):
            cid = int(a["client_id"])
            atype = _blank(a["activity_type"]) or "Activity"
            northstar_items.append(
                SharedHistoryItem(
                    item_key=f"activity-{a['activity_id']}",
                    item_type="activity",
                    section="northstar",
                    client_id=cid,
                    client_code=_blank(a["client_code"]),
                    client_name=_blank(a["client_name"]),
                    external_record_no=_blank(a["external_record_no"])
                    or rn_by_client.get(cid, ""),
                    title=atype,
                    body=_blank(a["notes"]) or _blank(a["outcome"]),
                    event_at=_blank(a["activity_at"]) or _blank(a["created_at"]),
                    created_by=_blank(a["created_by"]) or _blank(a["assigned_user"]),
                    activity_type=atype,
                    source_table="activities",
                    source_id=int(a["activity_id"]),
                )
            )

        # --- Legacy notes: dedupe identical LeadMaster history ---
        legacy_rows = list(
            conn.execute(
                f"""
                SELECT ln.*, cl.code AS client_code, cl.name AS client_name
                FROM legacy_notes ln
                JOIN clients cl ON cl.id = ln.client_id
                WHERE ln.company_id = ? AND ln.client_id IN ({cph})
                ORDER BY cl.name COLLATE NOCASE, ln.id
                """,
                (company_id, *filter_ids),
            )
        )

        groups: dict[str, list] = {}
        for n in legacy_rows:
            norm = normalize_legacy_note_text(_blank(n["note_text"]))
            if not norm:
                continue
            groups.setdefault(norm, []).append(n)

        shared_legacy_items: list[SharedHistoryItem] = []
        distinct_legacy_items: list[SharedHistoryItem] = []

        for norm, group in groups.items():
            client_ids_in_group = sorted({int(n["client_id"]) for n in group})
            client_names = sorted(
                {_blank(n["client_name"]) for n in group if _blank(n["client_name"])}
            )
            # Identical (or same normalized) text across 2+ clients → one shared entry
            if len(client_ids_in_group) >= 2:
                primary = min(group, key=lambda r: int(r["id"]))
                shared_legacy_items.append(
                    SharedHistoryItem(
                        item_key=f"shared-legacy-{primary['id']}",
                        item_type="shared_legacy",
                        section="shared_legacy",
                        legacy_scope="shared",
                        client_id=0,
                        client_code="leadmaster",
                        client_name="LeadMaster Legacy History",
                        external_record_no="",
                        title="LeadMaster Legacy History",
                        body=_blank(primary["note_text"]),
                        event_at=_blank(primary["created_at"]),
                        created_by="Imported",
                        source_table="legacy_notes",
                        source_id=int(primary["id"]),
                        shared_client_names=client_names,
                    )
                )
            else:
                for n in group:
                    cid = int(n["client_id"])
                    distinct_legacy_items.append(
                        SharedHistoryItem(
                            item_key=f"legacy-{n['id']}",
                            item_type="legacy_note",
                            section="distinct_legacy",
                            legacy_scope="distinct",
                            client_id=cid,
                            client_code=_blank(n["client_code"]),
                            client_name=_blank(n["client_name"]),
                            external_record_no=rn_by_client.get(cid, ""),
                            title=_blank(n["source_field"])
                            or "Sales Rep Comments/Notes",
                            body=_blank(n["note_text"]),
                            event_at=_blank(n["created_at"]),
                            created_by="Imported",
                            source_table="legacy_notes",
                            source_id=int(n["id"]),
                        )
                    )

        # When filtering to one client: hide shared legacy that doesn't include them,
        # and hide other clients' distinct legacy (already filtered via SQL).
        if filter_client_id is not None:
            fname = next(
                (
                    _blank(r["client_name"])
                    for r in client_rows
                    if int(r["client_id"]) == int(filter_client_id)
                ),
                "",
            )
            shared_legacy_items = [
                it
                for it in shared_legacy_items
                if not fname or fname in it.shared_client_names
            ]

        shared_legacy_items.sort(key=_sort_key, reverse=True)
        distinct_legacy_items.sort(key=_sort_key, reverse=True)

        # --- Company-scoped shared note-history events (chronological import) ---
        shared_company_history_items: list[SharedHistoryItem] = []
        try:
            from shared_note_history_import import (
                UNATTRIBUTED_LABEL,
                ensure_shared_note_history_schema,
            )

            ensure_shared_note_history_schema(conn)
            for ev in conn.execute(
                """
                SELECT *
                FROM company_shared_history_events
                WHERE company_id = ?
                ORDER BY
                    CASE
                        WHEN TRIM(COALESCE(event_sequence, '')) GLOB '[0-9]*'
                         AND TRIM(COALESCE(event_sequence, '')) != ''
                        THEN CAST(TRIM(event_sequence) AS INTEGER)
                        ELSE 999999999
                    END ASC,
                    event_at DESC,
                    id DESC
                """,
                (company_id,),
            ):
                attribution = _blank(ev["attribution"]) or UNATTRIBUTED_LABEL
                event_type = _blank(ev["event_type"]) or "Shared history"
                title = f"{event_type} · {attribution}"
                shared_company_history_items.append(
                    SharedHistoryItem(
                        item_key=f"shared-history-{ev['id']}",
                        item_type="shared_history_event",
                        section="shared_company_history",
                        legacy_scope="shared",
                        client_id=0,
                        client_code="",
                        client_name=attribution,
                        external_record_no=_blank(ev["external_record_no"]),
                        title=title,
                        body=_blank(ev["note_text"]),
                        event_at=_blank(ev["event_at"]),
                        created_by=_blank(ev["author"]),
                        activity_type=event_type,
                        source_table="company_shared_history_events",
                        source_id=int(ev["id"]),
                        attribution=attribution,
                        attribution_evidence=_blank(ev["attribution_evidence"]),
                        source_file=_blank(ev["source_file"]),
                        event_hash=_blank(ev["event_hash"]),
                    )
                )
        except Exception:
            shared_company_history_items = []

        items = [
            *northstar_items,
            *shared_company_history_items,
            *shared_legacy_items,
            *distinct_legacy_items,
        ]

    return SharedHistoryResponse(
        company_id=company_id,
        company_name=_blank(company["company_name"]),
        can_view_cross_client=user_is_internal_northstar(user),
        filter_client_id=filter_client_id,
        clients=summaries,
        items=items,
        northstar_items=northstar_items,
        shared_legacy_items=shared_legacy_items,
        distinct_legacy_items=distinct_legacy_items,
        shared_company_history_items=shared_company_history_items,
        count=len(items),
    )
