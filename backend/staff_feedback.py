"""Lightweight in-app staff feedback for the RevOps pilot.

Isolated schema only in Phase 6A. Do not create this table on live
northstar.db until the migration is approved.

Never stores passwords, session cookies, CSRF secrets, or hashes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from staff_rbac import PROPOSED_STAFF_FEEDBACK_DDL, user_has_permission

FEEDBACK_CATEGORIES = ("Bug", "Workflow", "Feature Request", "Data Issue", "Other")
MAX_BODY = 4000
MAX_ROUTE = 300


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def ensure_staff_feedback_schema(conn) -> None:
    conn.execute(PROPOSED_STAFF_FEEDBACK_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_feedback_created ON staff_feedback(created_at)"
    )


def submit_staff_feedback(
    conn,
    *,
    user_id: int | None,
    page_route: str,
    body: str,
    category: str = "Other",
    client_id: int | None = None,
    user_agent: str = "",
) -> dict[str, Any]:
    text = (body or "").strip()
    if not text:
        raise ValueError("Feedback text is required.")
    if len(text) > MAX_BODY:
        raise ValueError(f"Feedback must be at most {MAX_BODY} characters.")
    cat = (category or "Other").strip()
    if cat not in FEEDBACK_CATEGORIES:
        cat = "Other"
    route = (page_route or "").strip()[:MAX_ROUTE]
    agent = (user_agent or "").strip()[:300]
    if user_id is not None and not user_has_permission(int(user_id), "feedback.submit"):
        raise PermissionError("Not authorized to submit feedback.")
    ensure_staff_feedback_schema(conn)
    cid = int(client_id) if client_id is not None and int(client_id) > 0 else None
    uid = int(user_id) if user_id is not None else None
    stamp = _now()
    cur = conn.execute(
        """
        INSERT INTO staff_feedback (
            user_id, created_at, page_route, client_id, category, body, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, stamp, route, cid, cat, text, agent),
    )
    return {
        "id": int(cur.lastrowid),
        "user_id": uid,
        "created_at": stamp,
        "page_route": route,
        "client_id": cid,
        "category": cat,
        "ok": True,
    }
