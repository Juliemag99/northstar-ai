"""Client-neutral Prospects and Company Workspace data access.

Legacy Record No. belongs to the client-company relationship.
Master company identity is companies.id; client context is required for CRM fields.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from appointments_data import is_hot_prospect_status
from db import DB_PATH, get_connection
from models import (
    ActiveClient,
    CompanyWorkspace,
    ContactListItem,
    ContactSummary,
    LegacyNote,
    ProspectListItem,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _contact_display_name(first: str, last: str) -> str:
    return f"{first} {last}".strip()


_RESEARCH_NOTE_PREFIX = "created from northstar ai research"
_ADDED_CONTACTS_RE = re.compile(r"added contacts:\s*[\d,\s]+", re.IGNORECASE)


def _research_note_signature(body: str) -> str | None:
    """Fingerprint for system-generated AI Research notes (display dedupe only)."""
    text = _blank(body)
    if not text.lower().startswith(_RESEARCH_NOTE_PREFIX):
        return None
    normalized = _ADDED_CONTACTS_RE.sub("added contacts:", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _activity_timeline_item(row) -> "ContactTimelineItem":
    from models import ContactTimelineItem

    at = _blank(row["activity_at"]) or _blank(row["created_at"])
    return ContactTimelineItem(
        item_type="activity",
        id=int(row["activity_id"]),
        title=_blank(row["activity_type"]) or "Activity",
        body=_blank(row["notes"]),
        at=at,
        created_by=_blank(row["created_by"]),
        outcome=_blank(row["outcome"]),
        follow_up_at=_blank(row["follow_up_at"]) or None,
    )


def _note_timeline_item(row) -> "ContactTimelineItem":
    from models import ContactTimelineItem

    return ContactTimelineItem(
        item_type="note",
        id=int(row["id"]),
        title=_blank(row["source_field"]) or "Note",
        body=_blank(row["note_text"]),
        at=_blank(row["created_at"]),
        created_by="",
    )


def _dedupe_display_timeline(items: list) -> list:
    """Drop repeated system research notes and exact duplicate bodies. Newest kept.

    Does not delete or alter database rows.
    """
    seen_research: set[str] = set()
    seen_exact: set[tuple[str, str, str]] = set()
    out = []
    for item in items:
        research_sig = _research_note_signature(item.body)
        if research_sig:
            if research_sig in seen_research:
                continue
            seen_research.add(research_sig)
        exact = (item.item_type, item.title.strip().lower(), item.body.strip().lower())
        if exact in seen_exact:
            continue
        seen_exact.add(exact)
        out.append(item)
    return out


def db_exists() -> bool:
    return DB_PATH.exists()


def resolve_client_row(
    conn,
    *,
    client_id: int | None = None,
    client: str | None = None,
):
    """Resolve a clients row by numeric id or name/code."""
    if client_id is not None:
        return conn.execute(
            "SELECT * FROM clients WHERE id = ?", (int(client_id),)
        ).fetchone()
    if client:
        code = client.strip().lower()
        if code in {"carmeco", "carmeco metal"}:
            code = "carmeco"
        if code in {"brown", "brown industries"}:
            code = "brown"
        return conn.execute(
            "SELECT * FROM clients WHERE code = ? OR lower(name) = lower(?)",
            (code, client.strip()),
        ).fetchone()
    return None


def get_client_summary(client_id: int) -> ActiveClient | None:
    if not db_exists():
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        cid = int(row["id"])
        prospects = conn.execute(
            """
            SELECT COUNT(*) AS n FROM client_company_relationships
            WHERE client_id = ?
            """,
            (cid,),
        ).fetchone()["n"]
        contacts = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM contacts ct
            JOIN client_company_relationships ccr
              ON ccr.company_id = ct.company_id AND ccr.client_id = ?
            WHERE TRIM(ct.external_record_no) = TRIM(
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), (
                    SELECT co.external_record_no FROM companies co WHERE co.id = ccr.company_id
                ))
            )
            """,
            (cid,),
        ).fetchone()["n"]
        rel = "database/northstar.db"
        try:
            rel = str(Path(str(DB_PATH)).resolve().relative_to(REPO_ROOT)).replace(
                "\\", "/"
            )
        except ValueError:
            rel = str(DB_PATH)
        code = _blank(row["code"])
        name = _blank(row["name"])
        return ActiveClient(
            id=code,
            client_id=cid,
            code=code,
            name=name,
            seed_file=rel,
            prospect_count=int(prospects),
            company_count=int(prospects),
            contact_count=int(contacts),
            mode="selected_client",
        )


def get_all_my_clients_summary(user_id: int | None = None) -> ActiveClient:
    from access import get_default_user, list_clients_for_user, resolve_dashboard_client_ids

    user = get_default_user() if user_id is None else None
    uid = user_id
    if uid is None and user is not None:
        uid = user.id
    if uid is None:
        return ActiveClient(
            id="all",
            client_id=0,
            code="all",
            name="All My Clients",
            seed_file="",
            prospect_count=0,
            company_count=0,
            contact_count=0,
            mode="all_my_clients",
        )
    client_ids = resolve_dashboard_client_ids(uid, selected_client_id=None)
    assignments = list_clients_for_user(uid)
    prospect_count = 0
    contact_count = 0
    if db_exists() and client_ids:
        placeholders = ",".join("?" * len(client_ids))
        with get_connection() as conn:
            prospect_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS n FROM client_company_relationships
                    WHERE client_id IN ({placeholders})
                    """,
                    client_ids,
                ).fetchone()["n"]
            )
            contact_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM contacts ct
                    JOIN client_company_relationships ccr
                      ON ccr.company_id = ct.company_id
                     AND ccr.client_id IN ({placeholders})
                    WHERE TRIM(ct.external_record_no) = TRIM(
                        COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), (
                            SELECT co.external_record_no FROM companies co
                            WHERE co.id = ccr.company_id
                        ))
                    )
                    """,
                    client_ids,
                ).fetchone()["n"]
            )
    names = ", ".join(a.client_name for a in assignments) or "All My Clients"
    return ActiveClient(
        id="all",
        client_id=0,
        code="all",
        name="All My Clients",
        seed_file=names,
        prospect_count=prospect_count,
        company_count=prospect_count,
        contact_count=contact_count,
        mode="all_my_clients",
    )


def get_active_client(
    *,
    client_id: int | None = None,
    all_clients: bool = False,
) -> ActiveClient:
    """Active Client summary. Defaults to Carmeco when no selection provided."""
    if all_clients or (client_id is not None and int(client_id) == 0):
        return get_all_my_clients_summary()
    if client_id is not None:
        summary = get_client_summary(int(client_id))
        if summary is not None:
            return summary
    # Default: Carmeco if present, else first client
    if not db_exists():
        return ActiveClient(
            id="carmeco",
            client_id=0,
            code="carmeco",
            name="Carmeco",
            seed_file="",
            prospect_count=0,
        )
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE code = 'carmeco'"
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM clients ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if row is None:
            return ActiveClient(
                id="",
                client_id=0,
                code="",
                name="",
                seed_file="",
                prospect_count=0,
            )
    return get_client_summary(int(row["id"])) or ActiveClient(
        id="carmeco", client_id=0, code="carmeco", name="Carmeco", seed_file="", prospect_count=0
    )


def resolve_relationship(
    conn,
    *,
    record_no: str,
    client_id: int | None = None,
    client: str | None = None,
):
    """
    Resolve client-company relationship for a Record No.

    Prefer (client_id, CCR.external_record_no), then (client_id, master RN),
    then CCR RN alone when client is unambiguous.
    """
    key = record_no.strip()
    if not key:
        return None

    client_row = resolve_client_row(conn, client_id=client_id, client=client)

    if client_row is not None:
        cid = int(client_row["id"])
        row = conn.execute(
            """
            SELECT
                ccr.id AS relationship_id,
                ccr.client_id,
                ccr.company_id,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                    AS relationship_record_no,
                COALESCE(ccr.status, '') AS status,
                COALESCE(ccr.is_hot, 0) AS is_hot,
                COALESCE(ccr.next_action, '') AS next_action,
                ccr.follow_up_date,
                cl.code AS client_code,
                cl.name AS client_name,
                co.*
            FROM client_company_relationships ccr
            JOIN clients cl ON cl.id = ccr.client_id
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
              AND (
                    TRIM(ccr.external_record_no) = ?
                 OR co.external_record_no = ?
              )
            LIMIT 1
            """,
            (cid, key, key),
        ).fetchone()
        return row

    # No client provided: unique CCR RN match, else master RN with single CCR
    row = conn.execute(
        """
        SELECT
            ccr.id AS relationship_id,
            ccr.client_id,
            ccr.company_id,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                AS relationship_record_no,
            COALESCE(ccr.status, '') AS status,
            COALESCE(ccr.is_hot, 0) AS is_hot,
            COALESCE(ccr.next_action, '') AS next_action,
            ccr.follow_up_date,
            cl.code AS client_code,
            cl.name AS client_name,
            co.*
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        JOIN companies co ON co.id = ccr.company_id
        WHERE TRIM(ccr.external_record_no) = ?
        """,
        (key,),
    ).fetchall()
    if len(row) == 1:
        return row[0]
    if len(row) > 1:
        return None  # ambiguous — client_id required

    row = conn.execute(
        """
        SELECT
            ccr.id AS relationship_id,
            ccr.client_id,
            ccr.company_id,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                AS relationship_record_no,
            COALESCE(ccr.status, '') AS status,
            COALESCE(ccr.is_hot, 0) AS is_hot,
            COALESCE(ccr.next_action, '') AS next_action,
            ccr.follow_up_date,
            cl.code AS client_code,
            cl.name AS client_name,
            co.*
        FROM companies co
        JOIN client_company_relationships ccr ON ccr.company_id = co.id
        JOIN clients cl ON cl.id = ccr.client_id
        WHERE co.external_record_no = ?
        """,
        (key,),
    ).fetchall()
    if len(row) == 1:
        return row[0]
    return None


def list_prospects(
    *,
    client_id: int | None = None,
    all_clients: bool = False,
    user_id: int | None = None,
) -> list[ProspectListItem]:
    """Prospects for one client, or all assigned clients when all_clients=True."""
    if not db_exists():
        return []

    from access import get_default_user, resolve_dashboard_client_ids

    user = get_default_user() if user_id is None else None
    uid = user_id if user_id is not None else (user.id if user else None)
    if uid is None:
        return []

    if all_clients or client_id == 0:
        client_ids = resolve_dashboard_client_ids(uid, selected_client_id=None)
    elif client_id is not None:
        client_ids = resolve_dashboard_client_ids(
            uid, selected_client_id=int(client_id)
        )
    else:
        # Default Carmeco
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM clients WHERE code = 'carmeco'"
            ).fetchone()
            if row is None:
                return []
            client_ids = resolve_dashboard_client_ids(
                uid, selected_client_id=int(row["id"])
            )

    if not client_ids:
        return []

    placeholders = ",".join("?" * len(client_ids))
    sql = f"""
        SELECT
            co.id,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                AS external_record_no,
            co.company_name,
            co.city,
            co.state,
            co.address,
            co.zip,
            co.website,
            co.customer_campaign,
            co.last_updated_at,
            co.legacy_phone,
            co.legacy_first_name,
            co.legacy_last_name,
            COALESCE(ccr.status, '') AS status,
            COALESCE(ccr.is_hot, 0) AS is_hot,
            COALESCE(ccr.next_action, '') AS next_action,
            ccr.follow_up_date,
            ccr.id AS relationship_id,
            ccr.client_id,
            cl.code AS client_code,
            cl.name AS client_name,
            (
                SELECT COUNT(*) FROM contacts ct
                WHERE ct.company_id = co.id
            ) AS contact_count,
            (
                SELECT ct.first_name FROM contacts ct
                WHERE ct.company_id = co.id
                ORDER BY ct.source_row_index ASC, ct.id ASC
                LIMIT 1
            ) AS contact_first,
            (
                SELECT ct.last_name FROM contacts ct
                WHERE ct.company_id = co.id
                ORDER BY ct.source_row_index ASC, ct.id ASC
                LIMIT 1
            ) AS contact_last,
            (
                SELECT ct.phone FROM contacts ct
                WHERE ct.company_id = co.id
                ORDER BY ct.source_row_index ASC, ct.id ASC
                LIMIT 1
            ) AS contact_phone
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.client_id IN ({placeholders})
        ORDER BY cl.name COLLATE NOCASE ASC, co.company_name COLLATE NOCASE ASC
    """

    prospects: list[ProspectListItem] = []
    with get_connection() as conn:
        from milestones_data import companies_with_milestone_flags
        from work_queue_data import due_record_nos

        flags_by_client: dict[int, dict[int, dict[str, bool]]] = {}
        call_due: dict[int, set[str]] = {}
        follow_due: dict[int, set[str]] = {}
        for cid in client_ids:
            flags_by_client[cid] = companies_with_milestone_flags(conn, cid)
            call_due[cid] = due_record_nos(kind="call", client_id=cid)
            follow_due[cid] = due_record_nos(kind="follow_up", client_id=cid)

        for row in conn.execute(sql, client_ids):
            primary = _contact_display_name(
                _blank(row["contact_first"]), _blank(row["contact_last"])
            )
            if not primary:
                primary = _contact_display_name(
                    _blank(row["legacy_first_name"]), _blank(row["legacy_last_name"])
                )
            phone = _blank(row["contact_phone"]) or _blank(row["legacy_phone"])
            company_id = int(row["id"])
            cid = int(row["client_id"])
            flags = flags_by_client.get(cid, {}).get(company_id, {})
            status = _blank(row["status"])
            record_no = _blank(row["external_record_no"])
            prospects.append(
                ProspectListItem(
                    id=company_id,
                    external_record_no=record_no,
                    company=_blank(row["company_name"]),
                    city=_blank(row["city"]),
                    state=_blank(row["state"]),
                    status=status,
                    relationship_status=status,
                    primary_contact=primary,
                    phone=phone,
                    last_updated=_blank(row["last_updated_at"]),
                    website=_blank(row["website"]),
                    address=_blank(row["address"]),
                    zip=_blank(row["zip"]),
                    customer_campaign=_blank(row["customer_campaign"]),
                    contact_count=int(row["contact_count"] or 0),
                    is_hot=is_hot_prospect_status(status),
                    has_appointment_set=bool(flags.get("has_appointment_set")),
                    has_quote=bool(flags.get("has_quote")),
                    has_purchase_order=bool(flags.get("has_purchase_order")),
                    has_weblead=bool(flags.get("has_weblead")),
                    next_action=_blank(row["next_action"]),
                    follow_up_date=_blank(row["follow_up_date"]) or None,
                    call_due=record_no in call_due.get(cid, set()),
                    follow_up_due=record_no in follow_due.get(cid, set()),
                    client_id=cid,
                    client_code=_blank(row["client_code"]),
                    client_name=_blank(row["client_name"]),
                    relationship_id=int(row["relationship_id"]),
                )
            )
    return prospects


def _workspace_from_relationship(conn, row) -> CompanyWorkspace:
    company_id = int(row["company_id"] if "company_id" in row.keys() else row["id"])
    client_id = int(row["client_id"])
    rel_rn = _blank(row["relationship_record_no"])
    status = _blank(row["status"])

    contacts = [
        ContactSummary(
            id=int(c["id"]),
            first_name=_blank(c["first_name"]),
            last_name=_blank(c["last_name"]),
            title=_blank(c["title"]),
            phone=_blank(c["phone"]),
            alt_phone=_blank(c["alt_phone"]),
            email=_blank(c["email"]),
            external_record_no=_blank(c["external_record_no"]),
        )
        for c in conn.execute(
            """
            SELECT * FROM contacts
            WHERE company_id = ?
            ORDER BY source_row_index ASC, id ASC
            """,
            (company_id,),
        )
    ]

    notes = [
        LegacyNote(
            id=int(n["id"]),
            note_text=_blank(n["note_text"]),
            source_field=_blank(n["source_field"]) or "Sales Rep Comments/Notes",
            created_at=_blank(n["created_at"]),
        )
        for n in conn.execute(
            """
            SELECT * FROM legacy_notes
            WHERE client_id = ? AND company_id = ?
            ORDER BY id ASC
            """,
            (client_id, company_id),
        )
    ]

    from milestones_data import list_milestones_for_company, list_northstar_client_history
    from client_engagement_import import ensure_engagement_import_schema, list_sales_events

    milestones = list_milestones_for_company(
        rel_rn,
        client=_blank(row["client_name"]),
        company_id=company_id,
        client_id=client_id,
    )
    client_history = list_northstar_client_history(rel_rn, company_id=company_id)
    sales_events = []
    try:
        ensure_engagement_import_schema()
        sales_events = list_sales_events(client_id, company_id=company_id, limit=100)
    except Exception:
        sales_events = []

    recommendation = None
    try:
        from recommendation_data import build_northstar_recommendation
        from sales_events_intel import sales_event_engagement_signals

        se_dicts = [
            {
                "event_type": getattr(e, "event_type", ""),
                "event_date": getattr(e, "event_date", ""),
                "source_date_time_text": getattr(e, "source_date_time_text", ""),
            }
            for e in sales_events
        ]
        se_signals, se_level = sales_event_engagement_signals(
            se_dicts, closed_status=status
        )
        mil_signals = []
        for m in milestones or []:
            mt = _blank(getattr(m, "milestone_type", None) or (m.get("milestone_type") if isinstance(m, dict) else ""))
            if mt and mt not in mil_signals:
                mil_signals.append(mt)
        eng_signals = list(dict.fromkeys([*se_signals, *mil_signals]))
        fit_result = ""
        missing_fit: list[str] = []
        try:
            import json as _json
            from db import get_connection as _gc

            with _gc() as _conn:
                if _conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_client_fit'"
                ).fetchone():
                    row_fit = _conn.execute(
                        """
                        SELECT fit_result, missing_information
                        FROM company_client_fit
                        WHERE company_id = ? AND client_id = ?
                        LIMIT 1
                        """,
                        (company_id, client_id),
                    ).fetchone()
                    if row_fit:
                        fit_result = _blank(row_fit["fit_result"])
                        try:
                            missing_fit = [
                                _blank(x)
                                for x in _json.loads(row_fit["missing_information"] or "[]")
                                if _blank(x)
                            ]
                        except Exception:
                            missing_fit = []
        except Exception:
            fit_result = ""
            missing_fit = []

        if sales_events or milestones or fit_result or _blank(status):
            recommendation = build_northstar_recommendation(
                fit_result=fit_result or "Insufficient Information",
                engagement_level=se_level,
                engagement_signals=eng_signals,
                relationship_status=status,
                sales_events=se_dicts,
                contacts=[{"title": _blank(getattr(c, "title", ""))} for c in contacts],
                missing_information=missing_fit,
                campaign_name="Default",
                client_name=_blank(row["client_name"]),
            )
    except Exception:
        recommendation = None

    return CompanyWorkspace(
        id=company_id,
        external_record_no=rel_rn,
        company_name=_blank(row["company_name"]),
        address=_blank(row["address"]),
        city=_blank(row["city"]),
        state=_blank(row["state"]),
        zip=_blank(row["zip"]),
        website=_blank(row["website"]),
        sales_volume_range=_blank(row["sales_volume_range"]),
        location_sales_volume_range=_blank(row["location_sales_volume_range"]),
        employee_size_range=_blank(row["employee_size_range"]),
        primary_sic_code=_blank(row["primary_sic_code"]),
        primary_sic_description=_blank(row["primary_sic_description"]),
        primary_naics_code=_blank(row["primary_naics_code"]),
        primary_naics_description=_blank(row["primary_naics_description"]),
        sic_8_digit=_blank(row["sic_8_digit"]),
        sic_8_digit_description=_blank(row["sic_8_digit_description"]),
        type_of_industry=_blank(row["type_of_industry"]),
        customer_campaign=_blank(row["customer_campaign"]),
        entered_at=_blank(row["entered_at"]),
        last_updated_at=_blank(row["last_updated_at"]),
        status=status,
        relationship_status=status,
        is_hot=is_hot_prospect_status(status),
        legacy_phone=_blank(row["legacy_phone"]),
        legacy_email=_blank(row["legacy_email"]),
        contacts=contacts,
        legacy_notes=notes,
        milestones=milestones,
        client_history=client_history,
        sales_events=sales_events,
        recommendation=recommendation,
        client_id=client_id,
        client_code=_blank(row["client_code"]),
        client_name=_blank(row["client_name"]),
        relationship_id=int(row["relationship_id"]),
        master_external_record_no=_blank(row["external_record_no"]),
    )


def get_company_workspace(
    company_id: int,
    *,
    client_id: int | None = None,
    client: str | None = None,
) -> CompanyWorkspace | None:
    if not db_exists():
        return None
    with get_connection() as conn:
        client_row = resolve_client_row(conn, client_id=client_id, client=client)
        if client_row is None and client_id is None and not client:
            client_row = conn.execute(
                "SELECT * FROM clients WHERE code = 'carmeco'"
            ).fetchone()
        if client_row is None:
            return None
        row = conn.execute(
            """
            SELECT
                ccr.id AS relationship_id,
                ccr.client_id,
                ccr.company_id,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                    AS relationship_record_no,
                COALESCE(ccr.status, '') AS status,
                COALESCE(ccr.is_hot, 0) AS is_hot,
                COALESCE(ccr.next_action, '') AS next_action,
                ccr.follow_up_date,
                cl.code AS client_code,
                cl.name AS client_name,
                co.*
            FROM companies co
            JOIN client_company_relationships ccr
              ON ccr.company_id = co.id AND ccr.client_id = ?
            JOIN clients cl ON cl.id = ccr.client_id
            WHERE co.id = ?
            """,
            (int(client_row["id"]), company_id),
        ).fetchone()
        if row is None:
            return None
        return _workspace_from_relationship(conn, row)


def get_company_by_record_no(
    record_no: str,
    *,
    client_id: int | None = None,
    client: str | None = None,
) -> CompanyWorkspace | None:
    if not db_exists() or not record_no.strip():
        return None
    with get_connection() as conn:
        row = resolve_relationship(
            conn,
            record_no=record_no,
            client_id=client_id,
            client=client,
        )
        if row is not None:
            return _workspace_from_relationship(conn, row)

        # Soft open for cross-client prospecting: Working For = requested client
        # even when that client does not yet have a CCR for this company.
        client_row = resolve_client_row(conn, client_id=client_id, client=client)
        if client_row is None:
            return None

        key = record_no.strip()
        company = conn.execute(
            """
            SELECT co.*
            FROM companies co
            WHERE TRIM(co.external_record_no) = ?
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        if company is None:
            company = conn.execute(
                """
                SELECT co.*
                FROM companies co
                JOIN client_company_relationships ccr ON ccr.company_id = co.id
                WHERE TRIM(ccr.external_record_no) = ?
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if company is None:
            return None

        return _provisional_workspace_for_client(conn, company, client_row)


def _provisional_workspace_for_client(conn, company, client_row) -> CompanyWorkspace:
    """Company Workspace under a Working For client without a CCR yet."""
    company_id = int(company["id"])
    cid = int(client_row["id"])
    master_rn = _blank(company["external_record_no"])

    contacts = [
        ContactSummary(
            id=int(c["id"]),
            first_name=_blank(c["first_name"]),
            last_name=_blank(c["last_name"]),
            title=_blank(c["title"]),
            phone=_blank(c["phone"]),
            alt_phone=_blank(c["alt_phone"]),
            email=_blank(c["email"]),
            external_record_no=_blank(c["external_record_no"]),
        )
        for c in conn.execute(
            """
            SELECT * FROM contacts
            WHERE company_id = ?
            ORDER BY source_row_index ASC, id ASC
            """,
            (company_id,),
        )
    ]

    from milestones_data import list_milestones_for_company, list_northstar_client_history

    milestones = list_milestones_for_company(
        master_rn,
        client=_blank(client_row["name"]),
        company_id=company_id,
        client_id=cid,
    )
    client_history = list_northstar_client_history(master_rn, company_id=company_id)

    return CompanyWorkspace(
        id=company_id,
        external_record_no=master_rn,
        company_name=_blank(company["company_name"]),
        address=_blank(company["address"]),
        city=_blank(company["city"]),
        state=_blank(company["state"]),
        zip=_blank(company["zip"]),
        website=_blank(company["website"]),
        sales_volume_range=_blank(company["sales_volume_range"]),
        location_sales_volume_range=_blank(company["location_sales_volume_range"]),
        employee_size_range=_blank(company["employee_size_range"]),
        primary_sic_code=_blank(company["primary_sic_code"]),
        primary_sic_description=_blank(company["primary_sic_description"]),
        primary_naics_code=_blank(company["primary_naics_code"]),
        primary_naics_description=_blank(company["primary_naics_description"]),
        sic_8_digit=_blank(company["sic_8_digit"]),
        sic_8_digit_description=_blank(company["sic_8_digit_description"]),
        type_of_industry=_blank(company["type_of_industry"]),
        customer_campaign=_blank(company["customer_campaign"]),
        entered_at=_blank(company["entered_at"]),
        last_updated_at=_blank(company["last_updated_at"]),
        status="New",
        relationship_status="New",
        is_hot=False,
        legacy_phone=_blank(company["legacy_phone"]),
        legacy_email=_blank(company["legacy_email"]),
        contacts=contacts,
        legacy_notes=[],
        milestones=milestones,
        client_history=client_history,
        sales_events=[],
        client_id=cid,
        client_code=_blank(client_row["code"]),
        client_name=_blank(client_row["name"]),
        relationship_id=0,
        master_external_record_no=master_rn,
    )


def list_relationship_statuses(
    *,
    client_id: int | None = None,
    client: str | None = None,
    all_clients: bool = False,
    user_id: int | None = None,
) -> list[str]:
    """Distinct CCR statuses for one client, or the union across assigned clients."""
    if not db_exists():
        return []

    with get_connection() as conn:
        client_ids: list[int] = []
        if client_id is not None or (client and not all_clients):
            client_row = resolve_client_row(conn, client_id=client_id, client=client)
            if client_row is None:
                return []
            client_ids = [int(client_row["id"])]
        else:
            # All My Clients / no client specified → union of assigned clients.
            # Do NOT silently fall back to Carmeco-only statuses.
            from access import get_default_user, resolve_dashboard_client_ids

            user = get_default_user() if user_id is None else None
            uid = user_id if user_id is not None else (user.id if user else None)
            if uid is None:
                return []
            client_ids = resolve_dashboard_client_ids(uid, selected_client_id=None)

        if not client_ids:
            return []

        placeholders = ",".join("?" * len(client_ids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT ccr.status
            FROM client_company_relationships ccr
            WHERE ccr.client_id IN ({placeholders}) AND TRIM(ccr.status) != ''
            ORDER BY ccr.status COLLATE NOCASE ASC
            """,
            client_ids,
        ).fetchall()
    return [_blank(r["status"]) for r in rows if _blank(r["status"])]


def _write_audit(
    conn,
    *,
    client_code: str,
    record_no: str,
    field_name: str,
    old_value: str,
    new_value: str,
    changed_by: str,
) -> str:
    cur = conn.execute(
        """
        INSERT INTO field_audit_log (
            client_code, external_record_no, field_name,
            old_value, new_value, changed_by
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            client_code,
            record_no,
            field_name,
            old_value,
            new_value,
            changed_by.strip() or "Julie Magnani",
        ),
    )
    row = conn.execute(
        "SELECT changed_at FROM field_audit_log WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return _blank(row["changed_at"]) if row else ""


def update_relationship_status(
    record_no: str,
    *,
    status: str,
    client_id: int | None = None,
    client: str = "",
    user: str = "Julie Magnani",
) -> dict:
    key = record_no.strip()
    new_status = status.strip()
    if not key:
        raise ValueError("Record No. is required.")
    if not new_status:
        raise ValueError("Status is required.")

    with get_connection() as conn:
        rel = resolve_relationship(
            conn, record_no=key, client_id=client_id, client=client
        )
        if rel is None:
            raise LookupError(
                "Company relationship not found for Record No. "
                "(client context required for shared companies)."
            )
        cid = int(rel["client_id"])
        company_id = int(rel["company_id"])
        rel_rn = _blank(rel["relationship_record_no"])
        client_code = _blank(rel["client_code"])
        client_name = _blank(rel["client_name"])

        allowed = {
            _blank(r["status"])
            for r in conn.execute(
                """
                SELECT DISTINCT status FROM client_company_relationships
                WHERE client_id = ? AND TRIM(status) != ''
                """,
                (cid,),
            )
        }
        # Brown may have no statuses yet — allow any non-empty status when set is empty
        old_status = _blank(rel["status"])
        if allowed and new_status not in allowed and new_status != old_status:
            raise ValueError(
                f"Status '{new_status}' is not a known status for {client_name}."
            )

        conn.execute(
            """
            UPDATE client_company_relationships
            SET status = ?, is_hot = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                new_status,
                1 if is_hot_prospect_status(new_status) else 0,
                int(rel["relationship_id"]),
            ),
        )
        changed_at = _write_audit(
            conn,
            client_code=client_code or "unknown",
            record_no=rel_rn,
            field_name="status",
            old_value=old_status,
            new_value=new_status,
            changed_by=user,
        )
        conn.commit()

    from search_data import rebuild_search_index

    rebuild_search_index()
    workspace = get_company_by_record_no(
        rel_rn, client_id=cid
    )
    return {
        "external_record_no": rel_rn,
        "client": client_name,
        "client_id": cid,
        "field_name": "status",
        "old_value": old_status,
        "new_value": new_status,
        "changed_by": user.strip() or "Julie Magnani",
        "changed_at": changed_at,
        "workspace": workspace,
    }


def update_relationship_notes(
    record_no: str,
    *,
    note_text: str,
    client_id: int | None = None,
    client: str = "Carmeco",
    user: str = "Julie Magnani",
) -> dict:
    key = record_no.strip()
    new_text = note_text if note_text is not None else ""
    if not key:
        raise ValueError("Record No. is required.")

    with get_connection() as conn:
        rel = resolve_relationship(
            conn, record_no=key, client_id=client_id, client=client
        )
        if rel is None:
            raise LookupError(
                "Company relationship not found for Record No. "
                "(client context required for shared companies)."
            )
        cid = int(rel["client_id"])
        company_id = int(rel["company_id"])
        rel_rn = _blank(rel["relationship_record_no"])
        client_code = _blank(rel["client_code"])
        client_name = _blank(rel["client_name"])

        notes = list(
            conn.execute(
                """
                SELECT id, note_text FROM legacy_notes
                WHERE client_id = ? AND company_id = ?
                ORDER BY id ASC
                """,
                (cid, company_id),
            )
        )
        old_text = "\n\n".join(_blank(n["note_text"]) for n in notes) if notes else ""

        conn.execute(
            "DELETE FROM legacy_notes WHERE client_id = ? AND company_id = ?",
            (cid, company_id),
        )
        if new_text != "":
            conn.execute(
                """
                INSERT INTO legacy_notes (
                    client_id, company_id, note_text, source_field
                ) VALUES (?, ?, ?, ?)
                """,
                (cid, company_id, new_text, "Sales Rep Comments/Notes"),
            )

        changed_at = _write_audit(
            conn,
            client_code=client_code or "unknown",
            record_no=rel_rn,
            field_name="sales_rep_notes",
            old_value=old_text,
            new_value=new_text,
            changed_by=user,
        )
        conn.commit()

    from search_data import rebuild_search_index

    rebuild_search_index()
    workspace = get_company_by_record_no(rel_rn, client_id=cid)
    return {
        "external_record_no": rel_rn,
        "client": client_name,
        "client_id": cid,
        "field_name": "sales_rep_notes",
        "old_value": old_text,
        "new_value": new_text,
        "changed_by": user.strip() or "Julie Magnani",
        "changed_at": changed_at,
        "workspace": workspace,
    }


# Backward-compatible aliases used by older call sites
def list_carmeco_statuses() -> list[str]:
    return list_relationship_statuses(client="Carmeco")


def update_carmeco_status(
    record_no: str,
    *,
    client: str = "Carmeco",
    status: str,
    user: str = "Julie Magnani",
    client_id: int | None = None,
) -> dict:
    return update_relationship_status(
        record_no,
        status=status,
        client=client,
        client_id=client_id,
        user=user,
    )


def update_carmeco_notes(
    record_no: str,
    *,
    client: str = "Carmeco",
    note_text: str,
    user: str = "Julie Magnani",
    client_id: int | None = None,
) -> dict:
    return update_relationship_notes(
        record_no,
        note_text=note_text,
        client=client,
        client_id=client_id,
        user=user,
    )

def _contacts_client_ids(
    *,
    client_id: int | None,
    all_clients: bool,
    user_id: int | None,
) -> list[int]:
    from access import get_default_user, resolve_dashboard_client_ids

    user = get_default_user() if user_id is None else None
    uid = user_id if user_id is not None else (user.id if user else None)
    if uid is None:
        return []

    if all_clients or client_id == 0:
        return resolve_dashboard_client_ids(uid, selected_client_id=None)
    if client_id is not None:
        return resolve_dashboard_client_ids(uid, selected_client_id=int(client_id))
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE code = 'carmeco'"
        ).fetchone()
        if row is None:
            return []
        return resolve_dashboard_client_ids(uid, selected_client_id=int(row["id"]))


def _contact_record_join() -> str:
    """Scope CRM contacts to Record No. match or an explicit client-contact assignment."""
    return """
        FROM contacts ct
        JOIN companies co ON co.id = ct.company_id
        JOIN client_company_relationships ccr
          ON ccr.company_id = ct.company_id
        JOIN clients cl ON cl.id = ccr.client_id
        LEFT JOIN contact_client_relationships xref
          ON xref.contact_id = ct.id AND xref.client_id = ccr.client_id
    """


def _contact_assignment_sql() -> str:
    return """
      AND (
            TRIM(ct.external_record_no) = TRIM(
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
            )
         OR xref.id IS NOT NULL
      )
    """


def contact_is_assigned_to_client(contact_id: int, client_id: int, conn=None) -> bool:
    """True when this person is on the client's CRM contact list."""
    if contact_id <= 0 or client_id <= 0:
        return False
    owns = conn is None
    if owns:
        if not db_exists():
            return False
        conn = get_connection()
    try:
        ensure_contact_workflow_schema(conn)
        row = conn.execute(
            f"""
            SELECT 1 AS ok
            {_contact_record_join()}
            WHERE ct.id = ? AND ccr.client_id = ?
            {_contact_assignment_sql()}
            LIMIT 1
            """,
            (int(contact_id), int(client_id)),
        ).fetchone()
        return row is not None
    finally:
        if owns:
            conn.close()


def _contact_search_sql(tokens: list[str], params: list[object]) -> str:
    if not tokens:
        return ""
    clauses: list[str] = []
    for token in tokens:
        like = f"%{token}%"
        clauses.append(
            """
            (
                TRIM(ct.first_name || ' ' || ct.last_name) LIKE ?
                OR ct.first_name LIKE ?
                OR ct.last_name LIKE ?
                OR ct.title LIKE ?
                OR ct.email LIKE ?
                OR ct.phone LIKE ?
                OR ct.alt_phone LIKE ?
                OR co.company_name LIKE ?
                OR COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no) LIKE ?
            )
            """
        )
        params.extend([like] * 9)
    return " AND " + " AND ".join(clauses)


def list_contacts(
    *,
    client_id: int | None = None,
    all_clients: bool = False,
    user_id: int | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """CRM contacts for one client, or all assigned clients when all_clients=True.

    Read-only — does not create or update contact records.
    Contacts belong to a client via matching relationship Record No.
    """
    empty = {
        "contacts": [],
        "total": 0,
        "client_total": 0,
        "offset": 0,
        "limit": max(1, min(int(limit or 50), 200)),
    }
    if not db_exists():
        return empty

    client_ids = _contacts_client_ids(
        client_id=client_id, all_clients=all_clients, user_id=user_id
    )
    if not client_ids:
        return empty

    ensure_contact_workflow_schema()
    page_size = max(1, min(int(limit or 50), 200))
    page_offset = max(0, int(offset or 0))
    placeholders = ",".join("?" * len(client_ids))
    join_sql = _contact_record_join()
    tokens = [t for t in _blank(q).split() if t]
    search_params: list[object] = []
    search_sql = _contact_search_sql(tokens, search_params)
    where_sql = f"WHERE ccr.client_id IN ({placeholders}) {_contact_assignment_sql()}"

    contacts: list[ContactListItem] = []
    with get_connection() as conn:
        client_total = int(
            conn.execute(
                f"SELECT COUNT(*) AS n {join_sql} {where_sql}",
                client_ids,
            ).fetchone()["n"]
        )
        match_params = [*client_ids, *search_params]
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS n {join_sql} {where_sql} {search_sql}",
                match_params,
            ).fetchone()["n"]
        )
        rows = conn.execute(
            f"""
            SELECT
                ct.id,
                ct.first_name,
                ct.last_name,
                TRIM(ct.first_name || ' ' || ct.last_name) AS full_name,
                ct.title,
                ct.phone,
                ct.alt_phone,
                ct.email,
                ct.company_id,
                co.company_name,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                    AS company_record_no,
                co.city,
                co.state,
                ccr.client_id,
                cl.name AS client_name,
                cl.code AS client_code,
                ccr.id AS relationship_id,
                COALESCE(ccr.status, '') AS status
            {join_sql}
            {where_sql}
            {search_sql}
            ORDER BY
                CASE WHEN TRIM(ct.first_name || ' ' || ct.last_name) = '' THEN 1 ELSE 0 END,
                ct.last_name COLLATE NOCASE ASC,
                ct.first_name COLLATE NOCASE ASC,
                co.company_name COLLATE NOCASE ASC,
                ct.id ASC
            LIMIT ? OFFSET ?
            """,
            [*match_params, page_size, page_offset],
        )
        for row in rows:
            first = _blank(row["first_name"])
            last = _blank(row["last_name"])
            contacts.append(
                ContactListItem(
                    id=int(row["id"]),
                    first_name=first,
                    last_name=last,
                    full_name=_blank(row["full_name"]) or f"{first} {last}".strip(),
                    title=_blank(row["title"]),
                    phone=_blank(row["phone"]),
                    alt_phone=_blank(row["alt_phone"]),
                    email=_blank(row["email"]),
                    company_id=int(row["company_id"]) if row["company_id"] else None,
                    company_name=_blank(row["company_name"]),
                    company_record_no=_blank(row["company_record_no"]),
                    city=_blank(row["city"]),
                    state=_blank(row["state"]),
                    client_id=int(row["client_id"]),
                    client_name=_blank(row["client_name"]),
                    client_code=_blank(row["client_code"]),
                    relationship_id=int(row["relationship_id"] or 0),
                    status=_blank(row["status"]),
                )
            )
    return {
        "contacts": contacts,
        "total": total,
        "client_total": client_total,
        "offset": page_offset,
        "limit": page_size,
    }


def ensure_contact_workflow_schema(conn=None) -> None:
    """Create contact_client_workflows if missing. Does not alter contact rows."""
    owns = conn is None
    if owns:
        if not db_exists():
            return
        conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_client_workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                relationship_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT '',
                assigned_user_id INTEGER,
                next_action TEXT NOT NULL DEFAULT '',
                follow_up_date TEXT,
                follow_up_time TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (contact_id, client_id),
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE,
                FOREIGN KEY (assigned_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_contact_workflows_client
                ON contact_client_workflows(client_id, contact_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_client_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                relationship_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_by TEXT NOT NULL DEFAULT '',
                UNIQUE (contact_id, client_id),
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_contact_client_rel_client
                ON contact_client_relationships(client_id, contact_id)
            """
        )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _split_follow_up(value: object | None) -> tuple[str, str]:
    raw = _blank(value)
    if not raw:
        return "", ""
    if "T" in raw:
        raw = raw.replace("T", " ", 1)
    parts = raw.split(" ", 1)
    date_part = parts[0][:10]
    time_part = ""
    if len(parts) > 1:
        time_part = parts[1][:8]
        if len(time_part) == 5:
            time_part = f"{time_part}:00"
        if time_part.endswith(":00:00") and len(time_part) == 8:
            time_part = time_part[:5]
        elif len(time_part) >= 5:
            time_part = time_part[:5]
    return date_part, time_part


def _combine_follow_up(date_part: str, time_part: str) -> str | None:
    d = _blank(date_part)[:10]
    if not d:
        return None
    t = _blank(time_part)
    if len(t) == 5:
        t = f"{t}:00"
    if t:
        return f"{d} {t}"
    return d


def list_client_reps(client_id: int) -> list:
    from models import ContactRepOption

    if not db_exists() or client_id <= 0:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT u.id, u.full_name
            FROM users u
            LEFT JOIN user_client_assignments uca
              ON uca.user_id = u.id AND uca.client_id = ? AND uca.active = 1
            WHERE u.active = 1
              AND (
                    uca.user_id IS NOT NULL
                 OR COALESCE(u.is_internal_northstar, 1) = 1
                 OR COALESCE(u.is_administrator, 0) = 1
              )
            ORDER BY u.full_name COLLATE NOCASE, u.id
            """,
            (int(client_id),),
        ).fetchall()
    return [
        ContactRepOption(user_id=int(r["id"]), full_name=_blank(r["full_name"]))
        for r in rows
        if _blank(r["full_name"])
    ]


def _load_contact_relationship(conn, *, contact_id: int, client_id: int | None):
    row = conn.execute(
        """
        SELECT ct.*, co.company_name, co.external_record_no AS company_master_record_no
        FROM contacts ct
        LEFT JOIN companies co ON co.id = ct.company_id
        WHERE ct.id = ?
        """,
        (contact_id,),
    ).fetchone()
    if not row:
        raise LookupError("Contact not found.")
    company_id = int(row["company_id"]) if row["company_id"] else None
    rel = None
    if client_id and company_id:
        rel = conn.execute(
            """
            SELECT ccr.id, ccr.client_id, ccr.external_record_no,
                   COALESCE(ccr.status, '') AS status,
                   ccr.assigned_user_id,
                   COALESCE(ccr.next_action, '') AS next_action,
                   ccr.follow_up_date,
                   cl.name AS client_name, cl.code AS client_code,
                   COALESCE(u.full_name, '') AS assigned_user
            FROM client_company_relationships ccr
            JOIN clients cl ON cl.id = ccr.client_id
            LEFT JOIN users u ON u.id = ccr.assigned_user_id
            WHERE ccr.client_id = ? AND ccr.company_id = ?
            LIMIT 1
            """,
            (int(client_id), company_id),
        ).fetchone()
    elif company_id:
        rel = conn.execute(
            """
            SELECT ccr.id, ccr.client_id, ccr.external_record_no,
                   COALESCE(ccr.status, '') AS status,
                   ccr.assigned_user_id,
                   COALESCE(ccr.next_action, '') AS next_action,
                   ccr.follow_up_date,
                   cl.name AS client_name, cl.code AS client_code,
                   COALESCE(u.full_name, '') AS assigned_user
            FROM client_company_relationships ccr
            JOIN clients cl ON cl.id = ccr.client_id
            LEFT JOIN users u ON u.id = ccr.assigned_user_id
            WHERE ccr.company_id = ?
            ORDER BY ccr.id
            LIMIT 1
            """,
            (company_id,),
        ).fetchone()
    return row, company_id, rel


def get_contact_workspace(
    contact_id: int,
    *,
    client_id: int | None = None,
):
    """Contact Workspace — master person + client-scoped operational workflow."""
    from models import ContactOpenFollowUp, ContactTimelineItem, ContactWorkspace
    from client_engagement_import import ensure_engagement_import_schema, list_sales_events

    if not db_exists():
        raise LookupError("Database not found.")
    ensure_engagement_import_schema()
    ensure_contact_workflow_schema()
    with get_connection() as conn:
        row, company_id, rel = _load_contact_relationship(
            conn, contact_id=contact_id, client_id=client_id
        )
        requested_id = int(client_id) if client_id else 0
        requested_name = ""
        requested_code = ""
        assigned_to_client = True
        if requested_id > 0:
            assigned_to_client = contact_is_assigned_to_client(
                contact_id, requested_id, conn=conn
            )
            cl = conn.execute(
                "SELECT name, code FROM clients WHERE id = ?",
                (requested_id,),
            ).fetchone()
            if cl is not None:
                requested_name = _blank(cl["name"])
                requested_code = _blank(cl["code"])
            if not assigned_to_client:
                company_assigned = False
                if company_id:
                    company_assigned = (
                        conn.execute(
                            """
                            SELECT 1 FROM client_company_relationships
                            WHERE client_id = ? AND company_id = ?
                            LIMIT 1
                            """,
                            (requested_id, company_id),
                        ).fetchone()
                        is not None
                    )
                return ContactWorkspace(
                    contact_id=contact_id,
                    first_name=_blank(row["first_name"]),
                    last_name=_blank(row["last_name"]),
                    title=_blank(row["title"]),
                    phone=_blank(row["phone"]),
                    alt_phone=_blank(row["alt_phone"]),
                    email=_blank(row["email"]),
                    company_id=company_id,
                    company_name=_blank(row["company_name"]),
                    company_record_no=_blank(row["company_master_record_no"]),
                    client_id=requested_id,
                    client_name=requested_name,
                    client_code=requested_code,
                    relationship_id=None,
                    assigned_to_client=False,
                    company_assigned_to_client=company_assigned,
                    status="",
                    assigned_user_id=None,
                    assigned_user="",
                    next_action="",
                    follow_up_date="",
                    follow_up_time="",
                    open_follow_up=None,
                    sales_events=[],
                    activities=[],
                    notes=[],
                    timeline=[],
                    company_timeline=[],
                    reps=[],
                )
            if rel is None or int(rel["client_id"]) != requested_id:
                rel = None
        resolved_client_id = int(rel["client_id"]) if rel is not None else (requested_id or 0)
        relationship_id = int(rel["id"]) if rel is not None else None
        status = _blank(rel["status"]) if rel is not None else ""
        assigned_user_id = (
            int(rel["assigned_user_id"]) if rel is not None and rel["assigned_user_id"] is not None else None
        )
        assigned_user = _blank(rel["assigned_user"]) if rel is not None else ""
        next_action = _blank(rel["next_action"]) if rel is not None else ""
        follow_up_date, follow_up_time = _split_follow_up(
            rel["follow_up_date"] if rel is not None else None
        )
        if resolved_client_id:
            wf = conn.execute(
                """
                SELECT status, assigned_user_id, next_action, follow_up_date, follow_up_time
                FROM contact_client_workflows
                WHERE contact_id = ? AND client_id = ?
                """,
                (contact_id, resolved_client_id),
            ).fetchone()
            if wf is not None:
                status = _blank(wf["status"]) or status
                if wf["assigned_user_id"] is not None:
                    assigned_user_id = int(wf["assigned_user_id"])
                next_action = _blank(wf["next_action"]) if _blank(wf["next_action"]) else next_action
                wf_date, wf_time = _split_follow_up(wf["follow_up_date"])
                if wf_date:
                    follow_up_date = wf_date
                if _blank(wf["follow_up_time"]):
                    follow_up_time = _blank(wf["follow_up_time"])[:5]
                elif wf_time:
                    follow_up_time = wf_time
                if assigned_user_id is not None:
                    uname = conn.execute(
                        "SELECT full_name FROM users WHERE id = ?",
                        (assigned_user_id,),
                    ).fetchone()
                    if uname is not None:
                        assigned_user = _blank(uname["full_name"]) or assigned_user
        activities = []
        notes = []
        timeline: list[ContactTimelineItem] = []
        company_timeline: list[ContactTimelineItem] = []
        if resolved_client_id and company_id:
            try:
                contact_rows = conn.execute(
                    """
                    SELECT activity_id, activity_type, notes, outcome, activity_at,
                           created_at, created_by, contact_id, follow_up_at
                    FROM activities
                    WHERE client_id = ? AND company_id = ? AND contact_id = ?
                    ORDER BY COALESCE(activity_at, created_at) DESC, activity_id DESC
                    LIMIT 80
                    """,
                    (resolved_client_id, company_id, contact_id),
                ).fetchall()
                activities = [dict(a) for a in contact_rows]
                timeline = [_activity_timeline_item(a) for a in contact_rows]
            except Exception:
                activities = []
                timeline = []
            try:
                company_rows = conn.execute(
                    """
                    SELECT activity_id, activity_type, notes, outcome, activity_at,
                           created_at, created_by, contact_id, follow_up_at
                    FROM activities
                    WHERE client_id = ? AND company_id = ?
                      AND (
                        contact_id IS NULL
                        OR activity_type IN ('Appointment Cancelled', 'Appointment Completed')
                      )
                    ORDER BY COALESCE(activity_at, created_at) DESC, activity_id DESC
                    LIMIT 80
                    """,
                    (resolved_client_id, company_id),
                ).fetchall()
                company_timeline.extend(_activity_timeline_item(a) for a in company_rows)
            except Exception:
                pass
            try:
                note_rows = conn.execute(
                    """
                    SELECT id, note_text, source_field, created_at
                    FROM legacy_notes
                    WHERE client_id = ? AND company_id = ?
                    ORDER BY id DESC LIMIT 20
                    """,
                    (resolved_client_id, company_id),
                ).fetchall()
                notes = [
                    {
                        "id": int(n["id"]),
                        "note_text": _blank(n["note_text"]),
                        "source_field": _blank(n["source_field"]),
                        "created_at": _blank(n["created_at"]),
                    }
                    for n in note_rows
                ]
                company_timeline.extend(_note_timeline_item(n) for n in note_rows)
            except Exception:
                notes = []
        timeline.sort(key=lambda item: (item.at or "", item.id), reverse=True)
        timeline = _dedupe_display_timeline(timeline)[:80]
        company_timeline.sort(key=lambda item: (item.at or "", item.id), reverse=True)
        company_timeline = _dedupe_display_timeline(company_timeline)[:80]
    reps = list_client_reps(resolved_client_id) if resolved_client_id else []
    events = []
    if resolved_client_id:
        events = list_sales_events(resolved_client_id, contact_id=contact_id, limit=100)
    return ContactWorkspace(
        contact_id=contact_id,
        first_name=_blank(row["first_name"]),
        last_name=_blank(row["last_name"]),
        title=_blank(row["title"]),
        phone=_blank(row["phone"]),
        alt_phone=_blank(row["alt_phone"]),
        email=_blank(row["email"]),
        company_id=company_id,
        company_name=_blank(row["company_name"]),
        company_record_no=(
            _blank(rel["external_record_no"]) if rel else _blank(row["company_master_record_no"])
        ),
        client_id=resolved_client_id or requested_id or 0,
        client_name=(_blank(rel["client_name"]) if rel else requested_name),
        client_code=(_blank(rel["client_code"]) if rel else requested_code),
        relationship_id=relationship_id,
        assigned_to_client=assigned_to_client,
        company_assigned_to_client=relationship_id is not None,
        status=status,
        assigned_user_id=assigned_user_id,
        assigned_user=assigned_user,
        next_action=next_action,
        follow_up_date=follow_up_date,
        follow_up_time=follow_up_time,
        open_follow_up=_open_follow_up_for_contact(
            conn, client_id=resolved_client_id, contact_id=contact_id
        ),
        sales_events=events,
        activities=activities,
        notes=notes,
        timeline=timeline,
        company_timeline=company_timeline,
        reps=reps,
    )


def update_contact_workflow(contact_id: int, body) -> dict:
    """Save operational workflow for one client relationship of a shared master contact.

    Updates contact_client_workflows + the matching CCR only.
    Never inserts a contact. Never writes another client's CCR.
    """
    from access import get_default_user, user_can_access_client
    from models import ContactWorkflowUpdate

    if not isinstance(body, ContactWorkflowUpdate):
        body = ContactWorkflowUpdate.model_validate(body)
    client_id = int(body.client_id)
    if client_id <= 0:
        raise ValueError("client_id is required.")
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")

    ensure_contact_workflow_schema()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            contact_count_before = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            )
            row, company_id, rel = _load_contact_relationship(
                conn, contact_id=contact_id, client_id=client_id
            )
            if rel is None or company_id is None or int(rel["client_id"]) != client_id:
                raise LookupError(
                    "No client-company relationship for this contact and Working For client."
                )
            relationship_id = int(rel["id"])
            other_before = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]

            new_status = body.status if body.status is not None else _blank(rel["status"])
            new_status = _blank(new_status)
            new_next = body.next_action if body.next_action is not None else _blank(rel["next_action"])
            from next_actions import canonicalize_next_action

            new_next = canonicalize_next_action(
                _blank(new_next),
                client_id=client_id,
                status=new_status,
                conn=conn,
            )
            if body.follow_up_date is not None:
                new_date = _blank(body.follow_up_date)[:10]
            else:
                new_date, _ = _split_follow_up(rel["follow_up_date"])
            if body.follow_up_time is not None:
                new_time = _blank(body.follow_up_time)[:5]
            else:
                _, new_time = _split_follow_up(rel["follow_up_date"])
            if body.assigned_user_id is not None and int(body.assigned_user_id) > 0:
                new_assignee = int(body.assigned_user_id)
            elif body.assigned_user_id == 0:
                new_assignee = None
            else:
                new_assignee = (
                    int(rel["assigned_user_id"]) if rel["assigned_user_id"] is not None else None
                )

            if new_status:
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
                old_status = _blank(rel["status"])
                if allowed and new_status not in allowed and new_status != old_status:
                    raise ValueError(
                        f"Status '{new_status}' is not a known status for this client."
                    )

            follow_stored = _combine_follow_up(new_date, new_time)
            conn.execute(
                """
                INSERT INTO contact_client_workflows (
                    contact_id, client_id, relationship_id, status, assigned_user_id,
                    next_action, follow_up_date, follow_up_time, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(contact_id, client_id) DO UPDATE SET
                    relationship_id = excluded.relationship_id,
                    status = excluded.status,
                    assigned_user_id = excluded.assigned_user_id,
                    next_action = excluded.next_action,
                    follow_up_date = excluded.follow_up_date,
                    follow_up_time = excluded.follow_up_time,
                    updated_at = datetime('now')
                """,
                (
                    contact_id,
                    client_id,
                    relationship_id,
                    new_status,
                    new_assignee,
                    new_next,
                    new_date or None,
                    new_time,
                ),
            )
            conn.execute(
                """
                UPDATE client_company_relationships
                SET status = ?,
                    assigned_user_id = ?,
                    next_action = ?,
                    follow_up_date = ?,
                    is_hot = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND client_id = ?
                """,
                (
                    new_status,
                    new_assignee,
                    new_next,
                    follow_stored,
                    1 if is_hot_prospect_status(new_status) else 0,
                    relationship_id,
                    client_id,
                ),
            )
            if new_status and new_status != old_status:
                from activities_data import insert_activity_row

                record_no = _blank(rel["external_record_no"]) or _blank(
                    row["company_master_record_no"]
                )
                insert_activity_row(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=relationship_id,
                    external_record_no=record_no,
                    user_id=user.id,
                    contact_id=contact_id,
                    activity_type="Status Change",
                    outcome=f"{old_status} → {new_status}" if old_status else new_status,
                    notes=(
                        f"Status changed from {old_status or '—'} to {new_status}."
                    ),
                    assigned_user=_blank(user.full_name),
                    created_by=_blank(user.full_name) or "Julie Magnani",
                )
            other_after = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            if other_after != other_before:
                raise RuntimeError("Refusing to overwrite another client's relationship.")
            contact_count_after = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            )
            if contact_count_after != contact_count_before:
                raise RuntimeError("Contact workflow must not create or delete contacts.")
            if new_date:
                assignee_name = ""
                if new_assignee is not None:
                    uname = conn.execute(
                        "SELECT full_name FROM users WHERE id = ?",
                        (new_assignee,),
                    ).fetchone()
                    assignee_name = _blank(uname["full_name"]) if uname else ""
                _upsert_open_follow_up_task(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=relationship_id,
                    contact_id=contact_id,
                    record_no=_blank(rel["external_record_no"])
                    or _blank(row["company_master_record_no"]),
                    follow_date=new_date,
                    follow_time=new_time,
                    assigned_user_id=new_assignee,
                    assigned_user=assignee_name,
                    notes=new_next,
                    created_by=_blank(body.user) or "Julie Magnani",
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "ok": True,
        "message": "Workflow saved.",
        "workspace": get_contact_workspace(contact_id, client_id=client_id),
    }


def _ensure_work_queue_items(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            company_id INTEGER NOT NULL,
            relationship_id INTEGER NOT NULL,
            contact_id INTEGER,
            action_type TEXT NOT NULL,
            due_date TEXT NOT NULL,
            due_time TEXT NOT NULL DEFAULT '',
            assigned_user_id INTEGER,
            assigned_user TEXT NOT NULL DEFAULT '',
            completion_status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            source_activity_id INTEGER,
            notes TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT
        )
        """
    )


def _follow_up_stamp(follow_date: str, follow_time: str) -> str:
    date_part = _blank(follow_date)[:10]
    time_part = _blank(follow_time)[:5]
    if time_part and len(time_part) == 5:
        return f"{date_part} {time_part}"
    return date_part


def _upsert_open_follow_up_task(
    conn,
    *,
    client_id: int,
    company_id: int,
    relationship_id: int,
    contact_id: int,
    record_no: str,
    follow_date: str,
    follow_time: str,
    assigned_user_id: int | None,
    assigned_user: str,
    notes: str,
    created_by: str,
) -> int | None:
    """Create or update the one open follow-up task for this contact + client."""
    from activities_data import insert_activity_row

    follow_date = _blank(follow_date)[:10]
    follow_time = _blank(follow_time)[:5]
    if not follow_date:
        return None
    follow_at = _combine_follow_up(follow_date, follow_time)
    if not follow_at:
        return None
    _ensure_work_queue_items(conn)
    stamp = _follow_up_stamp(follow_date, follow_time)
    existing_act = conn.execute(
        """
        SELECT activity_id, follow_up_at FROM activities
        WHERE client_id = ? AND contact_id = ?
          AND activity_type = 'Follow-Up'
          AND COALESCE(follow_up_completed, 0) = 0
          AND lower(COALESCE(completion_status, 'open')) NOT IN
              ('completed', 'cancelled', 'done', 'closed')
        ORDER BY activity_id DESC
        LIMIT 1
        """,
        (int(client_id), int(contact_id)),
    ).fetchone()
    same_slot = conn.execute(
        """
        SELECT activity_id FROM activities
        WHERE client_id = ? AND contact_id = ?
          AND activity_type = 'Follow-Up'
          AND COALESCE(follow_up_completed, 0) = 0
          AND substr(REPLACE(COALESCE(follow_up_at, ''), 'T', ' '), 1, 16) = ?
        ORDER BY activity_id DESC
        LIMIT 1
        """,
        (int(client_id), int(contact_id), stamp),
    ).fetchone()
    activity_id: int | None = None
    if same_slot is not None:
        activity_id = int(same_slot["activity_id"])
        conn.execute(
            """
            UPDATE activities
            SET follow_up_at = ?, assigned_user = ?, user_id = ?,
                notes = CASE WHEN TRIM(?) = '' THEN notes ELSE ? END,
                updated_at = datetime('now')
            WHERE activity_id = ?
            """,
            (
                follow_at,
                assigned_user,
                assigned_user_id,
                notes,
                notes,
                activity_id,
            ),
        )
    elif existing_act is not None:
        activity_id = int(existing_act["activity_id"])
        conn.execute(
            """
            UPDATE activities
            SET follow_up_at = ?, assigned_user = ?, user_id = ?,
                notes = CASE WHEN TRIM(?) = '' THEN notes ELSE ? END,
                updated_at = datetime('now')
            WHERE activity_id = ?
            """,
            (
                follow_at,
                assigned_user,
                assigned_user_id,
                notes,
                notes,
                activity_id,
            ),
        )
    else:
        activity_id = insert_activity_row(
            conn,
            client_id=client_id,
            company_id=company_id,
            relationship_id=relationship_id,
            external_record_no=record_no,
            user_id=assigned_user_id,
            contact_id=contact_id,
            activity_type="Follow-Up",
            notes=notes or "Scheduled follow-up",
            follow_up_at=follow_at,
            assigned_user=assigned_user,
            created_by=created_by,
        )

    existing_q = conn.execute(
        """
        SELECT id, due_date, due_time FROM work_queue_items
        WHERE client_id = ? AND contact_id = ?
          AND lower(action_type) IN ('follow-up', 'follow up', 'followup')
          AND lower(COALESCE(completion_status, 'open')) IN
              ('open', 'due', 'incomplete', 'pending', '')
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(client_id), int(contact_id)),
    ).fetchone()
    same_q = conn.execute(
        """
        SELECT id FROM work_queue_items
        WHERE client_id = ? AND contact_id = ?
          AND lower(action_type) IN ('follow-up', 'follow up', 'followup')
          AND lower(COALESCE(completion_status, 'open')) IN
              ('open', 'due', 'incomplete', 'pending', '')
          AND substr(due_date, 1, 10) = ?
          AND substr(COALESCE(due_time, ''), 1, 5) = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(client_id), int(contact_id), follow_date, follow_time),
    ).fetchone()
    queue_id = None
    if same_q is not None:
        queue_id = int(same_q["id"])
    elif existing_q is not None:
        queue_id = int(existing_q["id"])
    if queue_id is not None:
        conn.execute(
            """
            UPDATE work_queue_items
            SET due_date = ?, due_time = ?, assigned_user_id = ?, assigned_user = ?,
                relationship_id = ?, source_activity_id = ?, notes = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                follow_date,
                follow_time,
                assigned_user_id,
                assigned_user,
                relationship_id,
                activity_id,
                notes,
                queue_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO work_queue_items (
                client_id, company_id, relationship_id, contact_id, action_type,
                due_date, due_time, assigned_user_id, assigned_user, completion_status,
                source, source_activity_id, notes, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Follow-Up', ?, ?, ?, ?, 'open',
                      'contact_workspace', ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                int(client_id),
                int(company_id),
                int(relationship_id),
                int(contact_id),
                follow_date,
                follow_time,
                assigned_user_id,
                assigned_user,
                activity_id,
                notes,
                created_by,
            ),
        )
    return activity_id


def _complete_open_follow_up_tasks(
    conn,
    *,
    client_id: int,
    contact_id: int,
) -> None:
    """Mark this contact's open follow-up activity and queue task completed."""
    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    conn.execute(
        """
        UPDATE activities
        SET follow_up_completed = 1,
            completion_status = 'completed',
            updated_at = ?
        WHERE client_id = ? AND contact_id = ?
          AND activity_type = 'Follow-Up'
          AND COALESCE(follow_up_completed, 0) = 0
          AND lower(COALESCE(completion_status, 'open')) NOT IN
              ('completed', 'cancelled', 'done', 'closed')
        """,
        (now, int(client_id), int(contact_id)),
    )
    _ensure_work_queue_items(conn)
    conn.execute(
        """
        UPDATE work_queue_items
        SET completion_status = 'completed',
            completed_at = ?,
            updated_at = ?
        WHERE client_id = ? AND contact_id = ?
          AND lower(action_type) IN ('follow-up', 'follow up', 'followup')
          AND lower(COALESCE(completion_status, 'open')) IN
              ('open', 'due', 'incomplete', 'pending', '')
        """,
        (now, now, int(client_id), int(contact_id)),
    )


def _open_follow_up_for_contact(conn, *, client_id: int, contact_id: int):
    from models import ContactOpenFollowUp

    if client_id <= 0 or contact_id <= 0:
        return None
    _ensure_work_queue_items(conn)
    row = conn.execute(
        """
        SELECT id, source_activity_id, due_date, due_time, assigned_user, notes
        FROM work_queue_items
        WHERE client_id = ? AND contact_id = ?
          AND lower(action_type) IN ('follow-up', 'follow up', 'followup')
          AND lower(COALESCE(completion_status, 'open')) IN
              ('open', 'due', 'incomplete', 'pending', '')
        ORDER BY due_date ASC, due_time ASC, id ASC
        LIMIT 1
        """,
        (int(client_id), int(contact_id)),
    ).fetchone()
    if row is not None:
        return ContactOpenFollowUp(
            source="work_queue",
            source_id=int(row["id"]),
            activity_id=(
                int(row["source_activity_id"]) if row["source_activity_id"] is not None else None
            ),
            due_date=_blank(row["due_date"])[:10],
            due_time=_blank(row["due_time"])[:5],
            assigned_user=_blank(row["assigned_user"]),
            notes=_blank(row["notes"]),
        )
    act = conn.execute(
        """
        SELECT activity_id, follow_up_at, assigned_user, notes
        FROM activities
        WHERE client_id = ? AND contact_id = ?
          AND activity_type = 'Follow-Up'
          AND COALESCE(follow_up_completed, 0) = 0
          AND lower(COALESCE(completion_status, 'open')) NOT IN
              ('completed', 'cancelled', 'done', 'closed')
        ORDER BY follow_up_at ASC, activity_id ASC
        LIMIT 1
        """,
        (int(client_id), int(contact_id)),
    ).fetchone()
    if act is None:
        return None
    due_date, due_time = _split_follow_up(act["follow_up_at"])
    return ContactOpenFollowUp(
        source="activity_follow_up",
        source_id=int(act["activity_id"]),
        activity_id=int(act["activity_id"]),
        due_date=due_date,
        due_time=due_time,
        assigned_user=_blank(act["assigned_user"]),
        notes=_blank(act["notes"]),
    )


def _follow_ups_match(left_date: str, left_time: str, right_date: str, right_time: str) -> bool:
    ld, lt = _blank(left_date)[:10], _blank(left_time)[:5]
    rd, rt = _blank(right_date)[:10], _blank(right_time)[:5]
    if not ld or not rd or ld != rd:
        return False
    if lt and rt:
        return lt == rt
    return True


def _clear_workflow_follow_up_if_match(
    conn,
    *,
    contact_id: int,
    client_id: int,
    relationship_id: int,
    task_date: str,
    task_time: str,
) -> None:
    wf = conn.execute(
        """
        SELECT follow_up_date, follow_up_time
        FROM contact_client_workflows
        WHERE contact_id = ? AND client_id = ?
        """,
        (int(contact_id), int(client_id)),
    ).fetchone()
    rel = conn.execute(
        "SELECT follow_up_date FROM client_company_relationships WHERE id = ? AND client_id = ?",
        (int(relationship_id), int(client_id)),
    ).fetchone()
    wf_date, wf_time = ("", "")
    if wf is not None:
        wf_date, wf_time = _split_follow_up(wf["follow_up_date"])
        if _blank(wf["follow_up_time"]):
            wf_time = _blank(wf["follow_up_time"])[:5]
    matches_wf = _follow_ups_match(wf_date, wf_time, task_date, task_time)
    rel_date, rel_time = _split_follow_up(rel["follow_up_date"] if rel is not None else None)
    matches_rel = _follow_ups_match(rel_date, rel_time, task_date, task_time)
    if matches_wf:
        conn.execute(
            """
            UPDATE contact_client_workflows
            SET follow_up_date = NULL, follow_up_time = '', updated_at = datetime('now')
            WHERE contact_id = ? AND client_id = ?
            """,
            (int(contact_id), int(client_id)),
        )
    if matches_rel:
        conn.execute(
            """
            UPDATE client_company_relationships
            SET follow_up_date = NULL, updated_at = datetime('now')
            WHERE id = ? AND client_id = ?
            """,
            (int(relationship_id), int(client_id)),
        )


def _status_never_requires_follow_up(status: str) -> bool:
    key = _blank(status).lower()
    if not key:
        return False
    if "do not call" in key or key in {"dnc"}:
        return True
    return key == "closed" or key.startswith("closed")


def _known_client_status(conn, client_id: int, candidate: str, current: str) -> bool:
    status = _blank(candidate)
    if not status:
        return False
    if status == _blank(current):
        return True
    allowed = {
        _blank(r["status"])
        for r in conn.execute(
            """
            SELECT DISTINCT status FROM client_company_relationships
            WHERE client_id = ? AND TRIM(status) != ''
            """,
            (int(client_id),),
        )
    }
    from appointments_data import is_appointment_set_status

    return (not allowed) or status in allowed or is_appointment_set_status(status)


class _ExistingAppointmentSave(Exception):
    """Idempotent Log Call: appointment already exists for this save key."""


def create_contact_activity(contact_id: int, body) -> dict:
    """Log Call / Add Note / Schedule Follow-Up for one client relationship.

    Log Call only creates a follow-up task when schedule_follow_up is true.
    Otherwise it logs the call, updates status, and completes any open
    follow-up for this contact + client. Note never changes status or follow-up.
    """
    from access import get_default_user, user_can_access_client
    from activities_data import ACTIVITY_TYPES, insert_activity_row
    from models import ContactActivityCreate

    if not isinstance(body, ContactActivityCreate):
        body = ContactActivityCreate.model_validate(body)
    client_id = int(body.client_id)
    if client_id <= 0:
        raise ValueError("client_id is required.")
    activity_type = _blank(body.activity_type)
    if activity_type not in ACTIVITY_TYPES:
        raise ValueError(
            f"Unsupported activity_type '{body.activity_type}'. "
            f"Allowed: {', '.join(sorted(ACTIVITY_TYPES))}."
        )
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")

    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    schedule_follow_up = bool(getattr(body, "schedule_follow_up", False))
    if activity_type == "Follow-Up":
        schedule_follow_up = True
    follow_date = _blank(body.follow_up_date)[:10]
    follow_time = _blank(body.follow_up_time)[:5]
    if activity_type == "Call" and not schedule_follow_up:
        follow_date = ""
        follow_time = ""
    notes = _blank(body.notes)
    next_action = _blank(body.next_action)
    if schedule_follow_up and follow_date and not next_action:
        next_action = "Follow-Up"
    outcome = _blank(body.outcome)
    activity_id: int | None = None
    follow_up_activity_id: int | None = None
    appointment_id: int | None = None
    existing_appointment_save = False
    index_ids: list[int] = []
    appointment_payload = None
    from appointments_data import (
        ensure_appointment_set_milestone,
        insert_appointment_row,
        is_appointment_set_status,
        normalize_appointment_payload,
    )

    if activity_type == "Call" and is_appointment_set_status(
        _blank(body.status) or _blank(body.outcome)
    ):
        appointment_payload = normalize_appointment_payload(
            body.appointment,
            status=_blank(body.status) or _blank(body.outcome),
        )

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row, company_id, rel = _load_contact_relationship(
                conn, contact_id=contact_id, client_id=client_id
            )
            if rel is None or company_id is None or int(rel["client_id"]) != client_id:
                raise LookupError(
                    "No client-company relationship for this contact and Working For client."
                )
            relationship_id = int(rel["id"])
            record_no = _blank(rel["external_record_no"]) or _blank(
                row["company_master_record_no"]
            )
            current_status = _blank(rel["status"])
            wf_row = conn.execute(
                """
                SELECT status, assigned_user_id, next_action, follow_up_date, follow_up_time
                FROM contact_client_workflows
                WHERE contact_id = ? AND client_id = ?
                """,
                (contact_id, client_id),
            ).fetchone()
            if wf_row is not None and _blank(wf_row["status"]):
                current_status = _blank(wf_row["status"])
            status_for_workflow = ""
            if activity_type == "Call":
                candidate = _blank(body.status) or outcome
                if candidate:
                    if not _known_client_status(conn, client_id, candidate, current_status):
                        if _blank(body.status):
                            raise ValueError(
                                f"Status '{candidate}' is not a known status for this client."
                            )
                    else:
                        status_for_workflow = candidate
            from next_actions import canonicalize_next_action

            next_action = canonicalize_next_action(
                next_action,
                client_id=client_id,
                status=status_for_workflow or current_status,
                conn=conn,
            )
            contact_count_before = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            )
            other_before = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            if activity_type == "Note" and not notes:
                raise ValueError("Note text is required.")
            if activity_type == "Follow-Up" and not follow_date:
                raise ValueError("Follow-up date is required.")
            if (
                activity_type == "Call"
                and schedule_follow_up
                and _status_never_requires_follow_up(status_for_workflow or outcome)
                and (not follow_date or not follow_time)
            ):
                schedule_follow_up = False
                follow_date = ""
                follow_time = ""
            if activity_type == "Call" and schedule_follow_up and (not follow_date or not follow_time):
                raise ValueError("Follow-up date and time are required to schedule another follow-up.")
            if activity_type == "Call" and not status_for_workflow and not outcome and not notes:
                raise ValueError("Log Call requires an outcome/status or notes.")
            if appointment_payload:
                from appointments_data import ensure_appointments_schema

                ensure_appointments_schema(conn)
            if appointment_payload and appointment_payload.get("idempotency_key"):
                existing_appt = conn.execute(
                    """
                    SELECT id, activity_id FROM appointments
                    WHERE client_id = ? AND idempotency_key = ?
                    """,
                    (client_id, appointment_payload["idempotency_key"]),
                ).fetchone()
                if existing_appt is not None:
                    appointment_id = int(existing_appt["id"])
                    if existing_appt["activity_id"] is not None:
                        activity_id = int(existing_appt["activity_id"])
                    raise _ExistingAppointmentSave()

            assignee_id = None
            if body.assigned_user_id is not None and int(body.assigned_user_id) > 0:
                assignee_id = int(body.assigned_user_id)
            elif wf_row is not None and wf_row["assigned_user_id"] is not None:
                assignee_id = int(wf_row["assigned_user_id"])
            elif rel["assigned_user_id"] is not None:
                assignee_id = int(rel["assigned_user_id"])
            else:
                assignee_id = user.id
            assignee_name = created_by
            if assignee_id is not None:
                uname = conn.execute(
                    "SELECT full_name FROM users WHERE id = ?",
                    (assignee_id,),
                ).fetchone()
                if uname is not None:
                    assignee_name = _blank(uname["full_name"]) or created_by

            if activity_type == "Follow-Up":
                follow_up_activity_id = _upsert_open_follow_up_task(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=relationship_id,
                    contact_id=contact_id,
                    record_no=record_no,
                    follow_date=follow_date,
                    follow_time=follow_time,
                    assigned_user_id=assignee_id,
                    assigned_user=assignee_name,
                    notes=notes or next_action,
                    created_by=created_by,
                )
                activity_id = follow_up_activity_id
            else:
                activity_id = insert_activity_row(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=relationship_id,
                    external_record_no=record_no,
                    user_id=user.id,
                    contact_id=contact_id,
                    activity_type=activity_type,
                    outcome=outcome or status_for_workflow,
                    notes=notes,
                    follow_up_at=None,
                    assigned_user=assignee_name,
                    created_by=created_by,
                )

            if activity_type in {"Call", "Follow-Up"}:
                wf_status = status_for_workflow if activity_type == "Call" else None
                wf_next = None
                wf_date = None
                wf_time = None
                if activity_type == "Call":
                    if body.next_action is not None:
                        wf_next = next_action
                    if schedule_follow_up:
                        wf_date = follow_date
                        wf_time = follow_time
                        if not wf_next:
                            wf_next = "Follow-Up"
                    else:
                        wf_date = ""
                        wf_time = ""
                else:
                    wf_next = next_action or "Follow-Up"
                    wf_date = follow_date
                    wf_time = follow_time
                if wf_status or wf_next is not None or wf_date is not None:
                    new_status = wf_status if wf_status else (
                        _blank(wf_row["status"]) if wf_row is not None else current_status
                    )
                    new_next = wf_next if wf_next is not None else (
                        _blank(wf_row["next_action"]) if wf_row is not None else _blank(rel["next_action"])
                    )
                    if wf_date is not None:
                        new_date = wf_date
                        new_time = wf_time or ""
                    elif wf_row is not None:
                        new_date, new_time = _split_follow_up(wf_row["follow_up_date"])
                        if _blank(wf_row["follow_up_time"]):
                            new_time = _blank(wf_row["follow_up_time"])[:5]
                    else:
                        new_date, new_time = _split_follow_up(rel["follow_up_date"])
                    follow_stored = _combine_follow_up(new_date, new_time)
                    new_assignee = assignee_id
                    if activity_type == "Call" and body.assigned_user_id is None:
                        new_assignee = (
                            int(wf_row["assigned_user_id"])
                            if wf_row is not None and wf_row["assigned_user_id"] is not None
                            else (
                                int(rel["assigned_user_id"])
                                if rel["assigned_user_id"] is not None
                                else assignee_id
                            )
                        )
                    conn.execute(
                        """
                        INSERT INTO contact_client_workflows (
                            contact_id, client_id, relationship_id, status, assigned_user_id,
                            next_action, follow_up_date, follow_up_time, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(contact_id, client_id) DO UPDATE SET
                            relationship_id = excluded.relationship_id,
                            status = excluded.status,
                            assigned_user_id = excluded.assigned_user_id,
                            next_action = excluded.next_action,
                            follow_up_date = excluded.follow_up_date,
                            follow_up_time = excluded.follow_up_time,
                            updated_at = datetime('now')
                        """,
                        (
                            contact_id,
                            client_id,
                            relationship_id,
                            new_status,
                            new_assignee,
                            new_next,
                            new_date or None,
                            new_time,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE client_company_relationships
                        SET status = ?,
                            assigned_user_id = ?,
                            next_action = ?,
                            follow_up_date = ?,
                            is_hot = ?,
                            updated_at = datetime('now')
                        WHERE id = ? AND client_id = ?
                        """,
                        (
                            new_status,
                            new_assignee,
                            new_next,
                            follow_stored,
                            1 if is_hot_prospect_status(new_status) else 0,
                            relationship_id,
                            client_id,
                        ),
                    )

            if activity_type == "Call":
                _complete_open_follow_up_tasks(
                    conn, client_id=client_id, contact_id=contact_id
                )
            if activity_type == "Call" and schedule_follow_up:
                follow_up_activity_id = _upsert_open_follow_up_task(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=relationship_id,
                    contact_id=contact_id,
                    record_no=record_no,
                    follow_date=follow_date,
                    follow_time=follow_time,
                    assigned_user_id=assignee_id,
                    assigned_user=assignee_name,
                    notes=notes or next_action,
                    created_by=created_by,
                )

            if appointment_payload and activity_id:
                if appointment_payload["revenue_specialist_user_id"] is None:
                    appointment_payload["revenue_specialist_user_id"] = assignee_id
                appointment_id, _created = insert_appointment_row(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=relationship_id,
                    contact_id=contact_id,
                    activity_id=activity_id,
                    created_by=created_by,
                    payload=appointment_payload,
                )
                ensure_appointment_set_milestone(
                    conn, activity_id=activity_id, created_by=created_by
                )

            other_after = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            if other_after != other_before:
                raise RuntimeError("Refusing to overwrite another client's relationship.")
            contact_count_after = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            )
            if contact_count_after != contact_count_before:
                raise RuntimeError("Contact activity must not create or delete contacts.")
            conn.commit()
        except _ExistingAppointmentSave:
            conn.rollback()
            existing_appointment_save = True
        except Exception:
            conn.rollback()
            raise

    if activity_id:
        index_ids.append(int(activity_id))
    if follow_up_activity_id and follow_up_activity_id != activity_id:
        index_ids.append(int(follow_up_activity_id))
    if index_ids:
        from search_data import index_activity

        for aid in index_ids:
            try:
                index_activity(aid)
            except Exception:
                pass

    return {
        "ok": True,
        "message": (
            "Appointment already saved."
            if existing_appointment_save
            else f"{activity_type} saved."
        ),
        "activity_id": activity_id,
        "follow_up_activity_id": follow_up_activity_id,
        "appointment_id": appointment_id,
        "workspace": get_contact_workspace(contact_id, client_id=client_id),
    }


def complete_contact_follow_up(contact_id: int, body) -> dict:
    """Complete this contact's open follow-up for the Active Client only."""
    from access import get_default_user, user_can_access_client
    from activities_data import insert_activity_row
    from models import ContactFollowUpCompleteRequest

    if not isinstance(body, ContactFollowUpCompleteRequest):
        body = ContactFollowUpCompleteRequest.model_validate(body)
    client_id = int(body.client_id)
    if client_id <= 0:
        raise ValueError("client_id is required.")
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    notes = _blank(body.notes)
    activity_id: int | None = None

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row, company_id, rel = _load_contact_relationship(
                conn, contact_id=contact_id, client_id=client_id
            )
            if rel is None or company_id is None or int(rel["client_id"]) != client_id:
                raise LookupError(
                    "No client-company relationship for this contact and Working For client."
                )
            relationship_id = int(rel["id"])
            record_no = _blank(rel["external_record_no"]) or _blank(
                row["company_master_record_no"]
            )
            other_before = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            open_task = _open_follow_up_for_contact(
                conn, client_id=client_id, contact_id=contact_id
            )
            if open_task is None:
                raise ValueError("No open follow-up task to complete.")
            task_date = _blank(open_task.due_date)[:10]
            task_time = _blank(open_task.due_time)[:5]
            current_status = _blank(rel["status"])
            wf_row = conn.execute(
                """
                SELECT status FROM contact_client_workflows
                WHERE contact_id = ? AND client_id = ?
                """,
                (contact_id, client_id),
            ).fetchone()
            if wf_row is not None and _blank(wf_row["status"]):
                current_status = _blank(wf_row["status"])
            new_status = current_status
            if body.status is not None and _blank(body.status):
                candidate = _blank(body.status)
                if not _known_client_status(conn, client_id, candidate, current_status):
                    raise ValueError(
                        f"Status '{candidate}' is not a known status for this client."
                    )
                new_status = candidate
            stamp = f"{task_date} {task_time}".strip() if task_time else task_date
            completion_notes = notes or f"Completed follow-up{f' scheduled for {stamp}' if stamp else ''}.".strip()
            activity_id = insert_activity_row(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=relationship_id,
                external_record_no=record_no,
                user_id=user.id,
                contact_id=contact_id,
                activity_type="Note",
                outcome="Follow-up completed",
                notes=completion_notes,
                follow_up_at=None,
                assigned_user=created_by,
                created_by=created_by,
            )
            _complete_open_follow_up_tasks(
                conn, client_id=client_id, contact_id=contact_id
            )
            _clear_workflow_follow_up_if_match(
                conn,
                contact_id=contact_id,
                client_id=client_id,
                relationship_id=relationship_id,
                task_date=task_date,
                task_time=task_time,
            )
            if new_status != current_status:
                conn.execute(
                    """
                    UPDATE contact_client_workflows
                    SET status = ?, updated_at = datetime('now')
                    WHERE contact_id = ? AND client_id = ?
                    """,
                    (new_status, contact_id, client_id),
                )
                conn.execute(
                    """
                    UPDATE client_company_relationships
                    SET status = ?, updated_at = datetime('now')
                    WHERE id = ? AND client_id = ?
                    """,
                    (new_status, relationship_id, client_id),
                )
            other_after = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
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
    return {
        "ok": True,
        "message": "Follow-up task completed.",
        "activity_id": activity_id,
        "follow_up_activity_id": None,
        "workspace": get_contact_workspace(contact_id, client_id=client_id),
    }


def reschedule_contact_follow_up(contact_id: int, body) -> dict:
    """Update the existing open follow-up date/time; do not create a duplicate."""
    from access import get_default_user, user_can_access_client
    from models import ContactFollowUpRescheduleRequest

    if not isinstance(body, ContactFollowUpRescheduleRequest):
        body = ContactFollowUpRescheduleRequest.model_validate(body)
    client_id = int(body.client_id)
    if client_id <= 0:
        raise ValueError("client_id is required.")
    follow_date = _blank(body.follow_up_date)[:10]
    follow_time = _blank(body.follow_up_time)[:5]
    if not follow_date or not follow_time:
        raise ValueError("Follow-up date and time are required to reschedule.")
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    notes = _blank(body.notes)
    follow_up_activity_id: int | None = None

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row, company_id, rel = _load_contact_relationship(
                conn, contact_id=contact_id, client_id=client_id
            )
            if rel is None or company_id is None or int(rel["client_id"]) != client_id:
                raise LookupError(
                    "No client-company relationship for this contact and Working For client."
                )
            relationship_id = int(rel["id"])
            record_no = _blank(rel["external_record_no"]) or _blank(
                row["company_master_record_no"]
            )
            other_before = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            open_task = _open_follow_up_for_contact(
                conn, client_id=client_id, contact_id=contact_id
            )
            if open_task is None:
                raise ValueError("No open follow-up task to reschedule.")
            wf_row = conn.execute(
                """
                SELECT status, assigned_user_id, next_action
                FROM contact_client_workflows
                WHERE contact_id = ? AND client_id = ?
                """,
                (contact_id, client_id),
            ).fetchone()
            assignee_id = None
            if wf_row is not None and wf_row["assigned_user_id"] is not None:
                assignee_id = int(wf_row["assigned_user_id"])
            elif rel["assigned_user_id"] is not None:
                assignee_id = int(rel["assigned_user_id"])
            else:
                assignee_id = user.id
            assignee_name = created_by
            if assignee_id is not None:
                uname = conn.execute(
                    "SELECT full_name FROM users WHERE id = ?",
                    (assignee_id,),
                ).fetchone()
                if uname is not None:
                    assignee_name = _blank(uname["full_name"]) or created_by
            follow_up_activity_id = _upsert_open_follow_up_task(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=relationship_id,
                contact_id=contact_id,
                record_no=record_no,
                follow_date=follow_date,
                follow_time=follow_time,
                assigned_user_id=assignee_id,
                assigned_user=assignee_name,
                notes=notes,
                created_by=created_by,
            )
            follow_stored = _combine_follow_up(follow_date, follow_time)
            current_status = _blank(wf_row["status"]) if wf_row is not None else _blank(rel["status"])
            current_next = (
                _blank(wf_row["next_action"]) if wf_row is not None else _blank(rel["next_action"])
            ) or "Follow-Up"
            conn.execute(
                """
                INSERT INTO contact_client_workflows (
                    contact_id, client_id, relationship_id, status, assigned_user_id,
                    next_action, follow_up_date, follow_up_time, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(contact_id, client_id) DO UPDATE SET
                    relationship_id = excluded.relationship_id,
                    follow_up_date = excluded.follow_up_date,
                    follow_up_time = excluded.follow_up_time,
                    next_action = excluded.next_action,
                    assigned_user_id = excluded.assigned_user_id,
                    updated_at = datetime('now')
                """,
                (
                    contact_id,
                    client_id,
                    relationship_id,
                    current_status,
                    assignee_id,
                    current_next,
                    follow_date,
                    follow_time,
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
                    follow_stored,
                    current_next,
                    assignee_id,
                    relationship_id,
                    client_id,
                ),
            )
            other_after = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            if other_after != other_before:
                raise RuntimeError("Refusing to overwrite another client's relationship.")
            from activities_data import insert_activity_row

            insert_activity_row(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=relationship_id,
                external_record_no=record_no,
                user_id=user.id,
                contact_id=contact_id,
                activity_type="Follow-Up Rescheduled",
                outcome=f"{follow_date} {follow_time}".strip(),
                notes=notes or f"Follow-up rescheduled to {follow_date} {follow_time}.".strip(),
                follow_up_at=_combine_follow_up(follow_date, follow_time),
                assigned_user=assignee_name,
                created_by=created_by,
            )
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
    return {
        "ok": True,
        "message": "Follow-up rescheduled.",
        "activity_id": follow_up_activity_id,
        "follow_up_activity_id": follow_up_activity_id,
        "workspace": get_contact_workspace(contact_id, client_id=client_id),
    }


def _client_default_new_status(conn, client_id: int) -> str:
    """Use this client's existing 'New' status label when present."""
    rows = conn.execute(
        """
        SELECT DISTINCT status FROM client_company_relationships
        WHERE client_id = ? AND TRIM(status) != ''
        """,
        (int(client_id),),
    ).fetchall()
    allowed = [_blank(r["status"]) for r in rows]
    for status in allowed:
        if status.lower() == "new":
            return status
    return "New"


def _allocate_relationship_record_no(conn) -> str:
    from crm_add_data import allocate_ns_record_no

    candidate = allocate_ns_record_no(conn)
    while True:
        hit = conn.execute(
            """
            SELECT 1 FROM client_company_relationships
            WHERE TRIM(external_record_no) = ?
            LIMIT 1
            """,
            (candidate,),
        ).fetchone()
        if hit is None:
            return candidate
        try:
            nxt = int(candidate[3:]) + 1
        except ValueError:
            nxt = 1
        candidate = f"NS-{nxt}"


def assign_shared_contact(contact_id: int, body) -> dict:
    """Link an existing master contact to the Active Client. Never duplicates the person."""
    from access import get_default_user, user_can_access_client
    from activities_data import create_activity
    from models import ActivityCreateRequest, ContactAssignRequest

    if not isinstance(body, ContactAssignRequest):
        body = ContactAssignRequest.model_validate(body)
    client_id = int(body.client_id)
    if client_id <= 0:
        raise ValueError("client_id is required.")
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")

    ensure_contact_workflow_schema()
    created_by = _blank(body.user) or _blank(user.full_name) or "Julie Magnani"
    company_created = False
    activity_id = None

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            contact_count_before = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            )
            row, company_id, rel = _load_contact_relationship(
                conn, contact_id=contact_id, client_id=client_id
            )
            if company_id is None:
                raise LookupError("Contact is not linked to a company.")
            other_before = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            cl = conn.execute(
                "SELECT name, code FROM clients WHERE id = ?",
                (client_id,),
            ).fetchone()
            client_name = _blank(cl["name"]) if cl else "Client"
            identity = {
                "first_name": _blank(row["first_name"]),
                "last_name": _blank(row["last_name"]),
                "title": _blank(row["title"]),
                "phone": _blank(row["phone"]),
                "email": _blank(row["email"]),
            }
            already = contact_is_assigned_to_client(contact_id, client_id, conn=conn)
            if already:
                conn.commit()
                ws = get_contact_workspace(contact_id, client_id=client_id)
                return {
                    "ok": True,
                    "message": f"{ws.first_name} {ws.last_name}".strip()
                    + f" is already assigned to {client_name}.",
                    "needs_company_confirmation": False,
                    "company_assigned": True,
                    "already_assigned": True,
                    "company_created": False,
                    "workspace": ws,
                    "activity_id": None,
                }

            company_assigned = rel is not None and int(rel["client_id"]) == client_id
            if not company_assigned:
                existing_ccr = conn.execute(
                    """
                    SELECT id, external_record_no, status FROM client_company_relationships
                    WHERE client_id = ? AND company_id = ?
                    """,
                    (client_id, company_id),
                ).fetchone()
                company_assigned = existing_ccr is not None
                if existing_ccr is not None:
                    rel = existing_ccr

            if not company_assigned and not body.add_company:
                conn.rollback()
                ws = get_contact_workspace(contact_id, client_id=client_id)
                return {
                    "ok": True,
                    "message": (
                        f"{client_name} does not have this company yet. "
                        f"Add both the company and contact?"
                    ),
                    "needs_company_confirmation": True,
                    "company_assigned": False,
                    "already_assigned": False,
                    "company_created": False,
                    "workspace": ws,
                    "activity_id": None,
                }

            new_status = _client_default_new_status(conn, client_id)
            if not company_assigned:
                record_no = _allocate_relationship_record_no(conn)
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO client_company_relationships (
                            client_id, company_id, status, assigned_user_id,
                            priority, next_action, notes, is_hot, created_at, updated_at,
                            external_record_no
                        ) VALUES (?, ?, ?, ?, '', '', ?, 0, datetime('now'), datetime('now'), ?)
                        """,
                        (
                            client_id,
                            company_id,
                            new_status,
                            user.id,
                            f"Added with shared contact {contact_id} by {created_by}.",
                            record_no,
                        ),
                    )
                    relationship_id = int(cur.lastrowid)
                    company_created = True
                except sqlite3.IntegrityError:
                    existing_ccr = conn.execute(
                        """
                        SELECT id FROM client_company_relationships
                        WHERE client_id = ? AND company_id = ?
                        """,
                        (client_id, company_id),
                    ).fetchone()
                    if existing_ccr is None:
                        raise
                    relationship_id = int(existing_ccr["id"])
                    company_created = False
            else:
                relationship_id = int(rel["id"]) if rel is not None else int(
                    conn.execute(
                        """
                        SELECT id FROM client_company_relationships
                        WHERE client_id = ? AND company_id = ?
                        """,
                        (client_id, company_id),
                    ).fetchone()["id"]
                )

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO contact_client_relationships (
                    contact_id, client_id, relationship_id, created_at, created_by
                ) VALUES (?, ?, ?, datetime('now'), ?)
                """,
                (contact_id, client_id, relationship_id, created_by),
            )
            if int(cur.rowcount or 0) <= 0:
                conn.commit()
                ws = get_contact_workspace(contact_id, client_id=client_id)
                return {
                    "ok": True,
                    "message": f"{ws.first_name} {ws.last_name}".strip()
                    + f" is already assigned to {client_name}.",
                    "needs_company_confirmation": False,
                    "company_assigned": True,
                    "already_assigned": True,
                    "company_created": False,
                    "workspace": ws,
                    "activity_id": None,
                }
            conn.execute(
                """
                INSERT INTO contact_client_workflows (
                    contact_id, client_id, relationship_id, status, assigned_user_id,
                    next_action, follow_up_date, follow_up_time, updated_at
                ) VALUES (?, ?, ?, ?, NULL, '', NULL, '', datetime('now'))
                ON CONFLICT(contact_id, client_id) DO UPDATE SET
                    relationship_id = excluded.relationship_id,
                    status = CASE
                        WHEN TRIM(contact_client_workflows.status) = ''
                        THEN excluded.status ELSE contact_client_workflows.status
                    END,
                    updated_at = datetime('now')
                """,
                (contact_id, client_id, relationship_id, new_status),
            )
            other_after = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
                    FROM client_company_relationships
                    WHERE company_id = ? AND client_id != ?
                    """,
                    (company_id, client_id),
                ).fetchall()
            ]
            if other_after != other_before:
                raise RuntimeError("Refusing to overwrite another client's relationship.")
            contact_row = conn.execute(
                "SELECT first_name, last_name, title, phone, email FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if (
                _blank(contact_row["first_name"]) != identity["first_name"]
                or _blank(contact_row["last_name"]) != identity["last_name"]
                or _blank(contact_row["title"]) != identity["title"]
                or _blank(contact_row["phone"]) != identity["phone"]
                or _blank(contact_row["email"]) != identity["email"]
            ):
                raise RuntimeError("Assign must not alter the shared master contact.")
            contact_count_after = int(
                conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
            )
            if contact_count_after != contact_count_before:
                raise RuntimeError("Assign must not create or delete contacts.")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    rel_row = None
    with get_connection() as conn:
        rel_row = conn.execute(
            """
            SELECT ccr.external_record_no, co.external_record_no AS company_master_record_no
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.id = ?
            """,
            (relationship_id,),
        ).fetchone()
        client_name = _blank(
            conn.execute(
                "SELECT name FROM clients WHERE id = ?", (client_id,)
            ).fetchone()["name"]
        )
    record_no = ""
    if rel_row is not None:
        record_no = _blank(rel_row["external_record_no"]) or _blank(
            rel_row["company_master_record_no"]
        )
    full_name = f"{identity['first_name']} {identity['last_name']}".strip() or "Contact"
    act = create_activity(
        ActivityCreateRequest(
            client=client_name,
            client_id=client_id,
            external_record_no=record_no,
            user_id=user.id,
            contact_id=contact_id,
            activity_type="Note",
            notes=(
                f"{created_by} added shared contact {full_name} "
                f"(contact_id {contact_id}) to {client_name}."
            ),
            created_by=created_by,
            assigned_user=created_by,
        )
    )
    activity_id = act.activity_id
    ws = get_contact_workspace(contact_id, client_id=client_id)
    return {
        "ok": True,
        "message": f"{full_name} added to {client_name}.",
        "needs_company_confirmation": False,
        "company_assigned": True,
        "already_assigned": False,
        "company_created": company_created,
        "workspace": ws,
        "activity_id": activity_id,
    }
