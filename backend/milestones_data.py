"""Client-scoped revenue milestones and cross-client opportunity foundation."""

from __future__ import annotations

from datetime import datetime

from access import get_default_user, get_user_by_id, resolve_dashboard_client_ids
from db import DB_PATH, get_connection
from models import (
    ClientMilestoneHistory,
    MilestoneCreateRequest,
    MilestoneSummary,
    RevenueMilestone,
    SetHotRequest,
)

MILESTONE_TYPES = frozenset(
    {"Appointment Set", "Quote", "Purchase Order", "WebLead", "Hot"}
)

# Map legacy Carmeco working statuses → milestone types (preserve history)
STATUS_MILESTONE_MAP = {
    "appointment set": "Appointment Set",
    "hot prospect": "Hot",
}


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _resolve_client_for_write(conn, client_id: object):
    from access import require_write_client_id

    cid = require_write_client_id(client_id, conn=conn)
    return conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()


def _resolve_client(conn, client_name: str = "Carmeco"):
    """Read helper. Writes must use _resolve_client_for_write."""
    code = client_name.strip().lower()
    if code in {"carmeco", "carmeco metal"}:
        code = "carmeco"
    return conn.execute(
        "SELECT * FROM clients WHERE code = ? OR lower(name) = lower(?)",
        (code, client_name.strip()),
    ).fetchone()


def _row_to_milestone(row, *, include_notes: bool = True) -> RevenueMilestone:
    contact_name = ""
    if "contact_name" in row.keys():
        contact_name = _blank(row["contact_name"])
    amount = row["amount"] if "amount" in row.keys() else None
    return RevenueMilestone(
        milestone_id=int(row["milestone_id"]),
        client_id=int(row["client_id"]),
        client_name=_blank(row["client_name"]) if "client_name" in row.keys() else "",
        client_code=_blank(row["client_code"]) if "client_code" in row.keys() else "",
        company_id=int(row["company_id"]),
        relationship_id=int(row["relationship_id"]),
        external_record_no=_blank(row["external_record_no"]),
        contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
        contact_name=contact_name or None,
        milestone_type=_blank(row["milestone_type"]),
        milestone_date=_blank(row["milestone_date"]),
        amount=float(amount) if amount is not None else None,
        reference_number=_blank(row["reference_number"]),
        source=_blank(row["source"]),
        notes=_blank(row["notes"]) if include_notes else "",
        created_by=_blank(row["created_by"]),
        created_at=_blank(row["created_at"]),
        source_activity_id=(
            int(row["source_activity_id"])
            if "source_activity_id" in row.keys() and row["source_activity_id"] is not None
            else None
        ),
    )


def backfill_milestones_from_existing(conn=None) -> dict[str, int]:
    """
    Map existing Carmeco statuses into milestone history without discarding data.
    Appointment Set / Hot Prospect statuses become milestones; Hot Prospect also sets is_hot.
    """
    owns = conn is None
    if owns:
        conn = get_connection()
    created = {"Appointment Set": 0, "Hot": 0, "is_hot": 0}
    try:
        if not _table_exists(conn, "revenue_milestones"):
            return created

        # Hot flag from Hot Prospect status (does not clear status)
        cur = conn.execute(
            """
            UPDATE client_company_relationships
            SET is_hot = 1
            WHERE lower(trim(status)) = 'hot prospect' AND COALESCE(is_hot, 0) = 0
            """
        )
        created["is_hot"] = cur.rowcount if cur.rowcount is not None else 0

        rows = conn.execute(
            """
            SELECT
                ccr.id AS relationship_id,
                ccr.client_id,
                ccr.company_id,
                ccr.status,
                ccr.updated_at,
                ccr.created_at,
                co.external_record_no,
                cl.name AS client_name
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            JOIN clients cl ON cl.id = ccr.client_id
            """
        ).fetchall()

        for row in rows:
            status_key = _blank(row["status"]).lower()
            milestone_type = STATUS_MILESTONE_MAP.get(status_key)
            if not milestone_type:
                continue
            exists = conn.execute(
                """
                SELECT 1 FROM revenue_milestones
                WHERE relationship_id = ? AND milestone_type = ?
                  AND source = 'carmeco_status_backfill'
                LIMIT 1
                """,
                (int(row["relationship_id"]), milestone_type),
            ).fetchone()
            if exists:
                continue
            milestone_date = (
                _blank(row["updated_at"])
                or _blank(row["created_at"])
                or _now_iso()
            )
            conn.execute(
                """
                INSERT INTO revenue_milestones (
                    client_id, company_id, relationship_id, external_record_no,
                    contact_id, milestone_type, milestone_date, amount,
                    reference_number, source, notes, created_by, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, '', 'carmeco_status_backfill', ?, ?, ?)
                """,
                (
                    int(row["client_id"]),
                    int(row["company_id"]),
                    int(row["relationship_id"]),
                    _blank(row["external_record_no"]),
                    milestone_type,
                    milestone_date,
                    f"Backfilled from Carmeco status: {_blank(row['status'])}",
                    "Carmeco Import",
                    _now_iso(),
                ),
            )
            created[milestone_type] = created.get(milestone_type, 0) + 1

        # Appointment activities → Appointment Set milestones (no duplicates)
        if _table_exists(conn, "activities"):
            acts = conn.execute(
                """
                SELECT a.* FROM activities a
                WHERE lower(a.activity_type) = 'appointment'
                """
            ).fetchall()
            for act in acts:
                exists = conn.execute(
                    """
                    SELECT 1 FROM revenue_milestones
                    WHERE source_activity_id = ?
                    LIMIT 1
                    """,
                    (int(act["activity_id"]),),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO revenue_milestones (
                        client_id, company_id, relationship_id, external_record_no,
                        contact_id, milestone_type, milestone_date, amount,
                        reference_number, source, notes, created_by, created_at,
                        source_activity_id
                    ) VALUES (?, ?, ?, ?, ?, 'Appointment Set', ?, NULL, '', 'activity', ?, ?, ?, ?)
                    """,
                    (
                        int(act["client_id"]),
                        int(act["company_id"]),
                        int(act["relationship_id"]),
                        _blank(act["external_record_no"]),
                        act["contact_id"],
                        _blank(act["activity_at"]) or _blank(act["created_at"]) or _now_iso(),
                        _blank(act["notes"]) or _blank(act["outcome"]),
                        _blank(act["created_by"]) or "System",
                        _now_iso(),
                        int(act["activity_id"]),
                    ),
                )
                created["Appointment Set"] = created.get("Appointment Set", 0) + 1

        conn.commit()
        return created
    finally:
        if owns:
            conn.close()


def create_milestone(body: MilestoneCreateRequest) -> RevenueMilestone:
    milestone_type = body.milestone_type.strip()
    if milestone_type not in MILESTONE_TYPES:
        raise ValueError(
            f"Unsupported milestone_type '{body.milestone_type}'. "
            f"Allowed: {', '.join(sorted(MILESTONE_TYPES))}."
        )
    record_no = body.external_record_no.strip()
    if not record_no:
        raise ValueError("external_record_no is required.")
    if not _db_exists():
        raise LookupError("Database not found.")

    with get_connection() as conn:
        client_row = _resolve_client_for_write(conn, body.client_id)
        if client_row is None:
            raise ValueError("Unknown client.")
        company = conn.execute(
            "SELECT id, external_record_no FROM companies WHERE external_record_no = ?",
            (record_no,),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found for Record No.")
        relationship = conn.execute(
            """
            SELECT id FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (int(client_row["id"]), int(company["id"])),
        ).fetchone()
        if relationship is None:
            raise LookupError("No client-company relationship for this Record No.")

        contact_id = body.contact_id
        if contact_id is not None:
            contact = conn.execute(
                "SELECT id, company_id FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if contact is None:
                raise ValueError("contact_id not found.")
            if int(contact["company_id"]) != int(company["id"]):
                raise ValueError("contact_id does not belong to this company.")

        milestone_date = body.milestone_date.strip() or _now_iso()
        created_by = body.created_by.strip() or "Julie Magnani"
        now = _now_iso()

        cur = conn.execute(
            """
            INSERT INTO revenue_milestones (
                client_id, company_id, relationship_id, external_record_no,
                contact_id, milestone_type, milestone_date, amount,
                reference_number, source, notes, created_by, created_at,
                source_activity_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(client_row["id"]),
                int(company["id"]),
                int(relationship["id"]),
                _blank(company["external_record_no"]),
                contact_id,
                milestone_type,
                milestone_date,
                body.amount,
                body.reference_number.strip(),
                body.source.strip(),
                body.notes,
                created_by,
                now,
                body.source_activity_id,
            ),
        )
        milestone_id = int(cur.lastrowid)

        # Mirror into activities timeline (does not replace milestones)
        activity_type = {
            "Appointment Set": "Appointment",
            "Quote": "Note",
            "Purchase Order": "Note",
            "WebLead": "Note",
            "Hot": "Note",
        }.get(milestone_type, "Note")
        activity_notes = body.notes
        if milestone_type in {"Quote", "Purchase Order", "WebLead", "Hot"}:
            parts = [f"{milestone_type} recorded."]
            if body.reference_number.strip():
                parts.append(f"Ref: {body.reference_number.strip()}")
            if body.amount is not None:
                parts.append(f"Amount: {body.amount}")
            if body.source.strip():
                parts.append(f"Source: {body.source.strip()}")
            if body.notes.strip():
                parts.append(body.notes.strip())
            activity_notes = " ".join(parts)

        act = conn.execute(
            """
            INSERT INTO activities (
                client_id, company_id, relationship_id, external_record_no,
                user_id, contact_id, activity_type, activity_at, outcome, notes,
                follow_up_at, assigned_user, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                int(client_row["id"]),
                int(company["id"]),
                int(relationship["id"]),
                _blank(company["external_record_no"]),
                contact_id,
                activity_type,
                milestone_date,
                milestone_type,
                activity_notes,
                created_by,
                created_by,
                now,
                now,
            ),
        )
        activity_id = int(act.lastrowid)
        conn.execute(
            "UPDATE revenue_milestones SET source_activity_id = ? WHERE milestone_id = ?",
            (activity_id, milestone_id),
        )

        if milestone_type == "Hot":
            conn.execute(
                """
                UPDATE client_company_relationships
                SET is_hot = 1, updated_at = ?
                WHERE id = ?
                """,
                (now, int(relationship["id"])),
            )

        conn.commit()

        row = conn.execute(
            """
            SELECT rm.*, cl.name AS client_name, cl.code AS client_code,
                   trim(COALESCE(ct.first_name,'') || ' ' || COALESCE(ct.last_name,'')) AS contact_name
            FROM revenue_milestones rm
            JOIN clients cl ON cl.id = rm.client_id
            LEFT JOIN contacts ct ON ct.id = rm.contact_id
            WHERE rm.milestone_id = ?
            """,
            (milestone_id,),
        ).fetchone()

    from search_data import index_activity, index_milestone

    index_activity(activity_id)
    index_milestone(milestone_id)
    return _row_to_milestone(row)


def set_company_hot(body: SetHotRequest) -> dict:
    """Toggle Hot on a client-company relationship without changing working status."""
    if not _db_exists():
        raise LookupError("Database not found.")
    record_no = body.external_record_no.strip()
    if not record_no:
        raise ValueError("external_record_no is required.")

    with get_connection() as conn:
        client_row = _resolve_client_for_write(conn, body.client_id)
        if client_row is None:
            raise ValueError("Unknown client.")
        company = conn.execute(
            "SELECT id, external_record_no FROM companies WHERE external_record_no = ?",
            (record_no,),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found.")
        relationship = conn.execute(
            """
            SELECT id, is_hot, status FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (int(client_row["id"]), int(company["id"])),
        ).fetchone()
        if relationship is None:
            raise LookupError("No client-company relationship for this Record No.")

        was_hot = bool(relationship["is_hot"])
        now = _now_iso()
        conn.execute(
            """
            UPDATE client_company_relationships
            SET is_hot = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if body.is_hot else 0, now, int(relationship["id"])),
        )

        milestone = None
        if body.is_hot and not was_hot:
            notes = body.notes.strip() or "Marked Hot"
            cur = conn.execute(
                """
                INSERT INTO revenue_milestones (
                    client_id, company_id, relationship_id, external_record_no,
                    contact_id, milestone_type, milestone_date, amount,
                    reference_number, source, notes, created_by, created_at
                ) VALUES (?, ?, ?, ?, NULL, 'Hot', ?, NULL, '', 'hot_toggle', ?, ?, ?)
                """,
                (
                    int(client_row["id"]),
                    int(company["id"]),
                    int(relationship["id"]),
                    _blank(company["external_record_no"]),
                    now,
                    notes,
                    body.created_by.strip() or "Julie Magnani",
                    now,
                ),
            )
            mid = int(cur.lastrowid)
            act = conn.execute(
                """
                INSERT INTO activities (
                    client_id, company_id, relationship_id, external_record_no,
                    user_id, contact_id, activity_type, activity_at, outcome, notes,
                    follow_up_at, assigned_user, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'Note', ?, 'Hot', ?, NULL, ?, ?, ?, ?)
                """,
                (
                    int(client_row["id"]),
                    int(company["id"]),
                    int(relationship["id"]),
                    _blank(company["external_record_no"]),
                    now,
                    notes,
                    body.created_by.strip() or "Julie Magnani",
                    body.created_by.strip() or "Julie Magnani",
                    now,
                    now,
                ),
            )
            aid = int(act.lastrowid)
            conn.execute(
                "UPDATE revenue_milestones SET source_activity_id = ? WHERE milestone_id = ?",
                (aid, mid),
            )
            conn.commit()
            from search_data import index_activity, index_milestone

            index_activity(aid)
            index_milestone(mid)
            row = conn.execute(
                """
                SELECT rm.*, cl.name AS client_name, cl.code AS client_code, '' AS contact_name
                FROM revenue_milestones rm
                JOIN clients cl ON cl.id = rm.client_id
                WHERE rm.milestone_id = ?
                """,
                (mid,),
            ).fetchone()
            milestone = _row_to_milestone(row)
        elif not body.is_hot and was_hot:
            notes = body.notes.strip() or "Hot cleared"
            act = conn.execute(
                """
                INSERT INTO activities (
                    client_id, company_id, relationship_id, external_record_no,
                    user_id, contact_id, activity_type, activity_at, outcome, notes,
                    follow_up_at, assigned_user, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'Note', ?, 'Hot cleared', ?, NULL, ?, ?, ?, ?)
                """,
                (
                    int(client_row["id"]),
                    int(company["id"]),
                    int(relationship["id"]),
                    _blank(company["external_record_no"]),
                    now,
                    notes,
                    body.created_by.strip() or "Julie Magnani",
                    body.created_by.strip() or "Julie Magnani",
                    now,
                    now,
                ),
            )
            aid = int(act.lastrowid)
            conn.commit()
            from search_data import index_activity

            index_activity(aid)
        else:
            conn.commit()

        return {
            "external_record_no": _blank(company["external_record_no"]),
            "client": _blank(client_row["name"]),
            "is_hot": bool(body.is_hot),
            "status": _blank(relationship["status"]),
            "milestone": milestone,
        }


def list_milestones_for_company(
    record_no: str,
    *,
    client: str = "Carmeco",
    company_id: int | None = None,
    client_id: int | None = None,
) -> list[RevenueMilestone]:
    if not _db_exists() or not record_no.strip():
        return []
    with get_connection() as conn:
        client_row = _resolve_client(conn, client)
        if client_row is None and client_id is not None:
            client_row = conn.execute(
                "SELECT * FROM clients WHERE id = ?", (int(client_id),)
            ).fetchone()
        if client_row is None:
            return []
        cid = int(client_row["id"])
        if company_id is not None:
            rows = conn.execute(
                """
                SELECT rm.*, cl.name AS client_name, cl.code AS client_code,
                       trim(COALESCE(ct.first_name,'') || ' ' || COALESCE(ct.last_name,'')) AS contact_name
                FROM revenue_milestones rm
                JOIN clients cl ON cl.id = rm.client_id
                LEFT JOIN contacts ct ON ct.id = rm.contact_id
                WHERE rm.client_id = ? AND rm.company_id = ?
                ORDER BY rm.milestone_date DESC, rm.milestone_id DESC
                """,
                (cid, int(company_id)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT rm.*, cl.name AS client_name, cl.code AS client_code,
                       trim(COALESCE(ct.first_name,'') || ' ' || COALESCE(ct.last_name,'')) AS contact_name
                FROM revenue_milestones rm
                JOIN clients cl ON cl.id = rm.client_id
                LEFT JOIN contacts ct ON ct.id = rm.contact_id
                WHERE rm.client_id = ? AND (
                    rm.external_record_no = ?
                    OR rm.company_id IN (
                        SELECT company_id FROM client_company_relationships
                        WHERE client_id = ? AND TRIM(external_record_no) = ?
                    )
                )
                ORDER BY rm.milestone_date DESC, rm.milestone_id DESC
                """,
                (cid, record_no.strip(), cid, record_no.strip()),
            ).fetchall()
    return [_row_to_milestone(r) for r in rows]


def list_northstar_client_history(
    record_no: str,
    *,
    company_id: int | None = None,
) -> list[ClientMilestoneHistory]:
    """
    Cross-client milestone summary for a company.
    Private notes from other clients are omitted; only milestone metadata is shown.
    """
    if not _db_exists() or (not record_no.strip() and company_id is None):
        return []
    with get_connection() as conn:
        if company_id is not None:
            company = {"id": int(company_id)}
        else:
            company = conn.execute(
                "SELECT id FROM companies WHERE external_record_no = ?",
                (record_no.strip(),),
            ).fetchone()
            if company is None:
                company = conn.execute(
                    """
                    SELECT company_id AS id
                    FROM client_company_relationships
                    WHERE TRIM(external_record_no) = ?
                    LIMIT 1
                    """,
                    (record_no.strip(),),
                ).fetchone()
        if company is None:
            return []
        clients = conn.execute(
            """
            SELECT cl.id, cl.code, cl.name
            FROM clients cl
            JOIN client_company_relationships ccr
              ON ccr.client_id = cl.id AND ccr.company_id = ?
            ORDER BY cl.name COLLATE NOCASE
            """,
            (int(company["id"]),),
        ).fetchall()
        histories: list[ClientMilestoneHistory] = []
        for cl in clients:
            rows = conn.execute(
                """
                SELECT rm.*, cl.name AS client_name, cl.code AS client_code, '' AS contact_name
                FROM revenue_milestones rm
                JOIN clients cl ON cl.id = rm.client_id
                WHERE rm.client_id = ? AND rm.company_id = ?
                ORDER BY rm.milestone_date DESC, rm.milestone_id DESC
                """,
                (int(cl["id"]), int(company["id"])),
            ).fetchall()
            # Sanitize: no private notes for cross-client view
            milestones = [_row_to_milestone(r, include_notes=False) for r in rows]
            is_hot_row = conn.execute(
                "SELECT is_hot FROM client_company_relationships WHERE client_id=? AND company_id=?",
                (int(cl["id"]), int(company["id"])),
            ).fetchone()
            histories.append(
                ClientMilestoneHistory(
                    client_id=int(cl["id"]),
                    client_code=_blank(cl["code"]),
                    client_name=_blank(cl["name"]),
                    is_hot=bool(is_hot_row["is_hot"]) if is_hot_row else False,
                    milestones=milestones,
                )
            )
    return histories


def milestone_summary(
    user_id: int | None = None,
    *,
    client_id: int | None = None,
    all_clients: bool = False,
) -> MilestoneSummary:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    client_ids = resolve_dashboard_client_ids(
        user.id, selected_client_id=client_id, all_clients=all_clients
    )
    mode = (
        "all_clients"
        if all_clients and user.is_administrator
        else ("selected_client" if client_id is not None else "all_my_clients")
    )
    if not client_ids:
        return MilestoneSummary(mode=mode, user_id=user.id, client_ids=[])

    placeholders = ",".join("?" * len(client_ids))
    with get_connection() as conn:
        def count_type(mtype: str) -> int:
            row = conn.execute(
                f"""
                SELECT COUNT(DISTINCT company_id) AS n
                FROM revenue_milestones
                WHERE client_id IN ({placeholders}) AND milestone_type = ?
                """,
                [*client_ids, mtype],
            ).fetchone()
            return int(row["n"] if row else 0)

        hot_row = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM client_company_relationships
            WHERE client_id IN ({placeholders}) AND COALESCE(is_hot, 0) = 1
            """,
            client_ids,
        ).fetchone()

    return MilestoneSummary(
        mode=mode,
        user_id=user.id,
        client_ids=client_ids,
        appointments_set=count_type("Appointment Set"),
        quotes=count_type("Quote"),
        purchase_orders=count_type("Purchase Order"),
        webleads=count_type("WebLead"),
        hot=int(hot_row["n"] if hot_row else 0),
    )


def companies_with_milestone_flags(
    conn,
    client_id: int,
) -> dict[int, dict[str, bool]]:
    """company_id → milestone existence flags for one client."""
    flags: dict[int, dict[str, bool]] = {}
    if not _table_exists(conn, "revenue_milestones"):
        return flags
    rows = conn.execute(
        """
        SELECT company_id, milestone_type
        FROM revenue_milestones
        WHERE client_id = ?
        GROUP BY company_id, milestone_type
        """,
        (client_id,),
    ).fetchall()
    for r in rows:
        cid = int(r["company_id"])
        entry = flags.setdefault(
            cid,
            {
                "has_appointment_set": False,
                "has_quote": False,
                "has_purchase_order": False,
                "has_weblead": False,
                "has_hot_milestone": False,
            },
        )
        mt = _blank(r["milestone_type"])
        if mt == "Appointment Set":
            entry["has_appointment_set"] = True
        elif mt == "Quote":
            entry["has_quote"] = True
        elif mt == "Purchase Order":
            entry["has_purchase_order"] = True
        elif mt == "WebLead":
            entry["has_weblead"] = True
        elif mt == "Hot":
            entry["has_hot_milestone"] = True
    return flags


def list_cross_client_opportunities(*args, **kwargs):
    """Compatibility wrapper — implementation lives in opportunities_data."""
    from opportunities_data import list_cross_client_opportunities as _impl

    if "selected_client_id" in kwargs and "target_client_id" not in kwargs:
        kwargs["target_client_id"] = kwargs.pop("selected_client_id")
    result = _impl(*args, **kwargs)
    if hasattr(result, "opportunities"):
        return result.opportunities
    return result


def ensure_appointment_milestone_from_activity(activity_id: int) -> None:
    """If an Appointment activity is created, feed Appointment Set milestone bucket."""
    if not _db_exists():
        return
    with get_connection() as conn:
        if not _table_exists(conn, "revenue_milestones"):
            return
        act = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()
        if act is None:
            return
        if _blank(act["activity_type"]).lower() != "appointment":
            return
        exists = conn.execute(
            "SELECT 1 FROM revenue_milestones WHERE source_activity_id = ?",
            (activity_id,),
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
            ) VALUES (?, ?, ?, ?, ?, 'Appointment Set', ?, NULL, '', 'activity', ?, ?, ?, ?)
            """,
            (
                int(act["client_id"]),
                int(act["company_id"]),
                int(act["relationship_id"]),
                _blank(act["external_record_no"]),
                act["contact_id"],
                _blank(act["activity_at"]) or _now_iso(),
                _blank(act["notes"]) or _blank(act["outcome"]),
                _blank(act["created_by"]) or "System",
                _now_iso(),
                activity_id,
            ),
        )
        mid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    from search_data import index_milestone

    index_milestone(mid)
