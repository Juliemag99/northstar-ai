"""In-app staff feedback for the RevOps pilot.

Schema is applied only by ensure_staff_feedback_schema(). Do not call that
from startup, ensure_schema(), or migrate_schema() until a live migration
is explicitly approved.

Never stores passwords, session cookies, CSRF secrets, API keys, or hashes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from access import user_can_access_client
from data_steward import sql_active_ccr, sql_active_contact
from staff_rbac import PROPOSED_STAFF_FEEDBACK_DDL, user_has_permission

FEEDBACK_CATEGORIES = (
    "Problem",
    "Suggestion",
    "Confusing",
    "Data issue",
    "Something I like",
)
FEEDBACK_IMPACTS = (
    "Blocking me",
    "Can continue",
    "Minor",
)
FEEDBACK_STATUSES = (
    "New",
    "Reviewed",
    "Planned",
    "Resolved",
    "Won't Change",
)
MAX_BODY = 4000
MAX_ROUTE = 300
MAX_AGENT = 300

_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("impact", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'New'"),
    ("company_id", "INTEGER"),
    ("contact_id", "INTEGER"),
)


class StaffFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    impact: str
    body: str
    page_route: str = ""
    client_id: int | None = None
    company_id: int | None = None
    contact_id: int | None = None


class StaffFeedbackStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class StaffFeedbackItem(BaseModel):
    id: int
    created_at: str
    user_id: int | None = None
    user_name: str = ""
    user_email: str = ""
    client_id: int | None = None
    client_name: str = ""
    category: str
    impact: str = ""
    body: str
    page_route: str = ""
    company_id: int | None = None
    company_name: str = ""
    contact_id: int | None = None
    contact_name: str = ""
    status: str = "New"


class StaffFeedbackList(BaseModel):
    items: list[StaffFeedbackItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def ensure_staff_feedback_schema(conn) -> None:
    """Create or upgrade staff_feedback. Does not create staff_auth_events."""
    conn.execute(PROPOSED_STAFF_FEEDBACK_DDL)
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(staff_feedback)").fetchall()
    }
    for name, column_type in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE staff_feedback ADD COLUMN {name} {column_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_feedback_created ON staff_feedback(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_feedback_status ON staff_feedback(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_feedback_client ON staff_feedback(client_id, created_at)"
    )


def _optional_positive(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    number = int(value)
    if number <= 0:
        raise ValueError(f"{label} is not valid.")
    return number


def _validate_context(
    conn,
    *,
    user_id: int,
    client_id: int | None,
    company_id: int | None,
    contact_id: int | None,
) -> tuple[int | None, int | None, int | None]:
    cid = _optional_positive(client_id, "client_id")
    company = _optional_positive(company_id, "company_id")
    contact = _optional_positive(contact_id, "contact_id")
    if cid is not None and not user_can_access_client(int(user_id), cid):
        raise PermissionError("Not authorized for this client.")
    if company is None and contact is None:
        return cid, None, None
    if cid is None:
        raise ValueError("A client is required when company or contact is supplied.")
    if contact is not None and company is None:
        raise ValueError("company_id is required when contact_id is supplied.")
    rel = conn.execute(
        f"""
        SELECT id FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
          AND {sql_active_ccr(conn, "client_company_relationships")}
        """,
        (cid, company),
    ).fetchone()
    if rel is None:
        raise ValueError("Company is not assigned to this client.")
    if contact is not None:
        row = conn.execute(
            f"""
            SELECT id, company_id FROM contacts
            WHERE id = ? AND {sql_active_contact(conn, "contacts")}
            """,
            (contact,),
        ).fetchone()
        if row is None or int(row["company_id"]) != int(company):
            raise ValueError("Contact does not belong to this company.")
    return cid, company, contact


def submit_staff_feedback(
    conn,
    *,
    user_id: int,
    page_route: str,
    body: str,
    category: str,
    impact: str,
    client_id: int | None = None,
    company_id: int | None = None,
    contact_id: int | None = None,
    user_agent: str = "",
) -> dict[str, Any]:
    """Insert one feedback row. Does not create the table and does not commit."""
    if user_id is None or int(user_id) <= 0:
        raise PermissionError("Authentication required.")
    uid = int(user_id)
    if not user_has_permission(uid, "feedback.submit"):
        raise PermissionError("Not authorized to submit feedback.")
    text = (body or "").strip()
    if not text:
        raise ValueError("Feedback text is required.")
    if len(text) > MAX_BODY:
        raise ValueError(f"Feedback must be at most {MAX_BODY} characters.")
    cat = (category or "").strip()
    if cat not in FEEDBACK_CATEGORIES:
        raise ValueError("Invalid feedback category.")
    level = (impact or "").strip()
    if level not in FEEDBACK_IMPACTS:
        raise ValueError("Invalid feedback impact.")
    route = (page_route or "").strip()
    if len(route) > MAX_ROUTE:
        raise ValueError(f"page_route must be at most {MAX_ROUTE} characters.")
    agent = (user_agent or "").strip()[:MAX_AGENT]
    cid, company, contact = _validate_context(
        conn,
        user_id=uid,
        client_id=client_id,
        company_id=company_id,
        contact_id=contact_id,
    )
    stamp = _now()
    cur = conn.execute(
        """
        INSERT INTO staff_feedback (
            user_id, created_at, page_route, client_id, category, body, user_agent,
            impact, status, company_id, contact_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'New', ?, ?)
        """,
        (uid, stamp, route, cid, cat, text, agent, level, company, contact),
    )
    return {
        "id": int(cur.lastrowid),
        "user_id": uid,
        "created_at": stamp,
        "page_route": route,
        "client_id": cid,
        "category": cat,
        "impact": level,
        "body": text,
        "status": "New",
        "company_id": company,
        "contact_id": contact,
        "ok": True,
    }


def list_staff_feedback(conn, *, limit: int = 50, offset: int = 0) -> StaffFeedbackList:
    safe_limit = min(max(int(limit), 1), 200)
    safe_offset = max(int(offset), 0)
    total = int(conn.execute("SELECT COUNT(*) AS n FROM staff_feedback").fetchone()["n"])
    rows = conn.execute(
        """
        SELECT
            f.id, f.created_at, f.user_id, f.client_id, f.category, f.impact, f.body,
            f.page_route, f.company_id, f.contact_id, f.status,
            COALESCE(u.full_name, '') AS user_name,
            COALESCE(u.email, '') AS user_email,
            COALESCE(cl.name, '') AS client_name,
            COALESCE(co.company_name, '') AS company_name,
            TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, '')) AS contact_name
        FROM staff_feedback f
        LEFT JOIN users u ON u.id = f.user_id
        LEFT JOIN clients cl ON cl.id = f.client_id
        LEFT JOIN companies co ON co.id = f.company_id
        LEFT JOIN contacts ct ON ct.id = f.contact_id
        ORDER BY f.created_at DESC, f.id DESC
        LIMIT ? OFFSET ?
        """,
        (safe_limit, safe_offset),
    ).fetchall()
    items = [
        StaffFeedbackItem(
            id=int(row["id"]),
            created_at=str(row["created_at"] or ""),
            user_id=int(row["user_id"]) if row["user_id"] is not None else None,
            user_name=str(row["user_name"] or ""),
            user_email=str(row["user_email"] or ""),
            client_id=int(row["client_id"]) if row["client_id"] is not None else None,
            client_name=str(row["client_name"] or ""),
            category=str(row["category"] or ""),
            impact=str(row["impact"] or ""),
            body=str(row["body"] or ""),
            page_route=str(row["page_route"] or ""),
            company_id=int(row["company_id"]) if row["company_id"] is not None else None,
            company_name=str(row["company_name"] or ""),
            contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
            contact_name=str(row["contact_name"] or "").strip(),
            status=str(row["status"] or "New"),
        )
        for row in rows
    ]
    return StaffFeedbackList(items=items, total=total, limit=safe_limit, offset=safe_offset)


def update_staff_feedback_status(conn, feedback_id: int, status: str) -> StaffFeedbackItem:
    next_status = (status or "").strip()
    if next_status not in FEEDBACK_STATUSES:
        raise ValueError("Invalid feedback status.")
    row = conn.execute(
        "SELECT id FROM staff_feedback WHERE id = ?",
        (int(feedback_id),),
    ).fetchone()
    if row is None:
        raise LookupError("Feedback not found.")
    conn.execute(
        "UPDATE staff_feedback SET status = ? WHERE id = ?",
        (next_status, int(feedback_id)),
    )
    row = conn.execute(
        """
        SELECT
            f.id, f.created_at, f.user_id, f.client_id, f.category, f.impact, f.body,
            f.page_route, f.company_id, f.contact_id, f.status,
            COALESCE(u.full_name, '') AS user_name,
            COALESCE(u.email, '') AS user_email,
            COALESCE(cl.name, '') AS client_name,
            COALESCE(co.company_name, '') AS company_name,
            TRIM(COALESCE(ct.first_name, '') || ' ' || COALESCE(ct.last_name, '')) AS contact_name
        FROM staff_feedback f
        LEFT JOIN users u ON u.id = f.user_id
        LEFT JOIN clients cl ON cl.id = f.client_id
        LEFT JOIN companies co ON co.id = f.company_id
        LEFT JOIN contacts ct ON ct.id = f.contact_id
        WHERE f.id = ?
        """,
        (int(feedback_id),),
    ).fetchone()
    if row is None:
        raise LookupError("Feedback not found.")
    return StaffFeedbackItem(
        id=int(row["id"]),
        created_at=str(row["created_at"] or ""),
        user_id=int(row["user_id"]) if row["user_id"] is not None else None,
        user_name=str(row["user_name"] or ""),
        user_email=str(row["user_email"] or ""),
        client_id=int(row["client_id"]) if row["client_id"] is not None else None,
        client_name=str(row["client_name"] or ""),
        category=str(row["category"] or ""),
        impact=str(row["impact"] or ""),
        body=str(row["body"] or ""),
        page_route=str(row["page_route"] or ""),
        company_id=int(row["company_id"]) if row["company_id"] is not None else None,
        company_name=str(row["company_name"] or ""),
        contact_id=int(row["contact_id"]) if row["contact_id"] is not None else None,
        contact_name=str(row["contact_name"] or "").strip(),
        status=str(row["status"] or "New"),
    )
