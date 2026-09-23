"""DS-8 governed Remove From Client / Restore relationship.

Reuses data_steward.remove_relationship and restore_relationship.
Archives the SAME CCR. Does not enable master archive, delete, or merge.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from data_steward import (
    ACTION_REMOVE_FROM_CLIENT,
    ACTION_RESTORE_TO_CLIENT,
    SOURCE_MANUAL_ADMIN,
    StewardError,
    _blank,
    _has_text,
    _load,
    inspect_relationship_dependencies,
    is_archived,
    provenance_history,
    remove_relationship,
    restore_relationship,
    sql_active_ccr,
)
from pydantic import BaseModel, Field

from models import NorthStarUser

REMOVE_WARNING = (
    "This removes the company from this client's active working list. The "
    "Master Company and historical data are preserved."
)
RESTORE_WARNING = (
    "This restores the existing client relationship. It does not create a new relationship."
)
WARNING_STATUSES = frozenset(
    {
        "appointment set",
        "current customer",
        "customer",
        "closed won",
        "sold",
    }
)


class RelationshipActionRequest(BaseModel):
    """Governed CCR payload. Session actor wins; body actor fields are ignored."""

    reason: str = ""
    confirm: bool = False
    preview_fingerprint: str = ""
    expected_updated_at: str | None = None
    expected_archived_at: str | None = None
    actor_id: int | None = Field(default=None, description="Ignored. Session user is the actor.")
    user_id: int | None = Field(default=None, description="Ignored.")
    created_by: str | None = Field(default=None, description="Ignored.")


def _int_or_none(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number else None


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _user_name(conn, user_id: object) -> str:
    uid = _int_or_none(user_id)
    if not uid or not _table_exists(conn, "users"):
        return ""
    row = conn.execute("SELECT full_name FROM users WHERE id=?", (uid,)).fetchone()
    return _blank(row["full_name"] if row is not None else "")


def relationship_fingerprint(
    *,
    ccr_id: int,
    archived_at: str,
    updated_at: str,
    status: str,
    assigned_user_id: object,
    is_hot: object,
    follow_up_date: str,
    next_action: str,
    reason: str,
    action: str,
) -> str:
    payload = json.dumps(
        {
            "ccr_id": int(ccr_id),
            "archived_at": _blank(archived_at),
            "updated_at": _blank(updated_at),
            "status": _blank(status),
            "assigned_user_id": _int_or_none(assigned_user_id),
            "is_hot": int(is_hot or 0),
            "follow_up_date": _blank(follow_up_date),
            "next_action": _blank(next_action),
            "reason": _blank(reason),
            "action": _blank(action),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_relationship_dependencies(stored: dict[str, Any], counts: dict[str, Any]) -> list[dict[str, str]]:
    """Conservative: warn on operational commitments; do not block or erase them."""
    items: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        items.append({"severity": severity, "code": code, "message": message})

    assigned = _int_or_none(stored.get("assigned_user_id"))
    if assigned:
        add("warning", "assigned_rep", "An assigned rep is preserved and will leave the active working list.")
    if int(stored.get("is_hot") or 0):
        add("warning", "hot", "Hot is preserved on the relationship and will leave the active Hot queue.")
    if _blank(stored.get("follow_up_date")):
        add(
            "warning",
            "open_follow_up",
            "An open follow-up date is preserved and will leave active due-work queues.",
        )
    if _blank(stored.get("next_action")):
        add("warning", "next_action", "Next action is preserved and will leave active due-work queues.")
    if int(counts.get("campaigns") or 0) > 0:
        add(
            "warning",
            "campaign_membership",
            "Historical campaign membership is preserved; the company leaves active campaign targeting.",
        )
    status = _blank(stored.get("status")).casefold()
    if status in WARNING_STATUSES:
        add(
            "warning",
            "status_commitment",
            f"Current status is {stored.get('status')}. Removal does not change or cancel that status.",
        )
    if int(counts.get("notes") or 0) or int(counts.get("legacy_notes") or 0):
        add("info", "notes", "Client notes remain on this relationship.")
    if int(counts.get("history") or 0):
        add("info", "history", "Shared history remains on the Master Company.")
    if int(counts.get("activities") or 0):
        add("info", "activities", "Activity rows remain; they leave active specialist queues.")
    return items


def load_relationship_snapshot(conn, ccr_id: int) -> dict[str, Any]:
    stored = _load(conn, "client_company_relationships", int(ccr_id))
    company = _load(conn, "companies", int(stored["company_id"]))
    client = conn.execute(
        "SELECT id, code, name FROM clients WHERE id=?",
        (int(stored["client_id"]),),
    ).fetchone()
    if client is None:
        raise LookupError("client_not_found")
    deps = inspect_relationship_dependencies(conn, int(ccr_id))
    counts = dict(deps.get("counts") or {})
    contact_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE company_id=?",
            (int(stored["company_id"]),),
        ).fetchone()[0]
    )
    alias_count = (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM company_aliases WHERE company_id=?",
                (int(stored["company_id"]),),
            ).fetchone()[0]
        )
        if _table_exists(conn, "company_aliases")
        else 0
    )
    location_count = (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM company_locations WHERE company_id=?",
                (int(stored["company_id"]),),
            ).fetchone()[0]
        )
        if _table_exists(conn, "company_locations")
        else 0
    )
    identity_count = (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM company_source_identities WHERE company_id=?",
                (int(stored["company_id"]),),
            ).fetchone()[0]
        )
        if _table_exists(conn, "company_source_identities")
        else 0
    )
    other_active = conn.execute(
        f"""
        SELECT COUNT(*) FROM client_company_relationships ccr
        WHERE ccr.company_id = ?
          AND ccr.id != ?
          AND {sql_active_ccr(conn, "ccr")}
        """,
        (int(stored["company_id"]), int(ccr_id)),
    ).fetchone()[0]
    archived = is_archived(conn, "client_company_relationships", int(ccr_id))
    assigned_id = _int_or_none(stored.get("assigned_user_id"))
    archived_by_id = _int_or_none(stored.get("archived_by_user_id"))
    notes = _blank(stored.get("notes"))
    return {
        "ccr_id": int(ccr_id),
        "company_id": int(stored["company_id"]),
        "company_name": _blank(company.get("company_name")),
        "client_id": int(stored["client_id"]),
        "client_code": _blank(client["code"]),
        "client_name": _blank(client["name"]),
        "external_record_no": _blank(stored.get("external_record_no") or company.get("external_record_no")),
        "status": _blank(stored.get("status")),
        "assigned_user_id": assigned_id,
        "assigned_rep": _user_name(conn, assigned_id),
        "is_hot": bool(int(stored.get("is_hot") or 0)),
        "follow_up_date": _blank(stored.get("follow_up_date")),
        "next_action": _blank(stored.get("next_action")),
        "notes_preview": notes[:240],
        "has_notes": bool(notes),
        "contact_count": contact_count,
        "activity_count": int(counts.get("activities") or 0),
        "history_count": int(counts.get("history") or 0),
        "campaign_count": int(counts.get("campaigns") or 0),
        "alias_count": alias_count,
        "location_count": location_count,
        "identity_count": identity_count,
        "other_active_relationships": int(other_active),
        "archived": archived,
        "archived_at": _blank(stored.get("archived_at")),
        "archived_by_user_id": archived_by_id,
        "archived_by_name": _user_name(conn, archived_by_id),
        "archive_reason": _blank(stored.get("archive_reason")),
        "updated_at": _blank(stored.get("updated_at") or stored.get("last_updated_at")),
        "dependencies": classify_relationship_dependencies(stored, counts),
        "counts": {
            **counts,
            "contacts": contact_count,
            "aliases": alias_count,
            "locations": location_count,
            "identities": identity_count,
        },
    }


def _effects_remove() -> list[str]:
    return [
        "CCR becomes inactive/archived (same CCR id).",
        "Disappears from that client's Fresh Calling, Work Queue, active search/prospects, Hot queue, and active reports.",
        "Leaves active campaign targeting; historical campaign membership rows remain.",
        "Follow-ups/tasks are preserved and leave active specialist queues.",
        "Other client relationships remain active.",
        "Master Company, contacts, notes/history, source identities, aliases, and locations remain.",
    ]


def _effects_restore() -> list[str]:
    return [
        "The same CCR id is reactivated. No new relationship is created.",
        "Preserved status, assigned rep, Hot, notes, history, Record No., and follow-up/action return as stored.",
        "Past follow-up dates are not rewritten; they surface again under existing due-work semantics.",
        "Campaign membership is not duplicated.",
        "The company returns to that client's active working views.",
    ]


def preview_remove_relationship(
    conn,
    *,
    actor: NorthStarUser,
    ccr_id: int,
    reason: str = "",
) -> dict[str, Any]:
    _ = actor
    snap = load_relationship_snapshot(conn, int(ccr_id))
    reason_text = _blank(reason)
    already = bool(snap["archived"])
    blocking = [d for d in snap["dependencies"] if d["severity"] == "block"]
    fingerprint = relationship_fingerprint(
        ccr_id=int(ccr_id),
        archived_at=snap["archived_at"],
        updated_at=snap["updated_at"],
        status=snap["status"],
        assigned_user_id=snap["assigned_user_id"],
        is_hot=snap["is_hot"],
        follow_up_date=snap["follow_up_date"],
        next_action=snap["next_action"],
        reason=reason_text,
        action="remove",
    )
    return {
        **snap,
        "action": "remove",
        "warning": REMOVE_WARNING,
        "effects": _effects_remove(),
        "reason": reason_text,
        "reason_ok": bool(reason_text),
        "blocked": bool(blocking),
        "already_removed": already,
        "noop": already,
        "preview_fingerprint": fingerprint,
        "expected_updated_at": snap["updated_at"],
        "expected_archived_at": snap["archived_at"],
        "writes": False,
        "confirm_required": True,
    }


def preview_restore_relationship(
    conn,
    *,
    actor: NorthStarUser,
    ccr_id: int,
    reason: str = "",
) -> dict[str, Any]:
    _ = actor
    snap = load_relationship_snapshot(conn, int(ccr_id))
    reason_text = _blank(reason)
    already_active = not bool(snap["archived"])
    fingerprint = relationship_fingerprint(
        ccr_id=int(ccr_id),
        archived_at=snap["archived_at"],
        updated_at=snap["updated_at"],
        status=snap["status"],
        assigned_user_id=snap["assigned_user_id"],
        is_hot=snap["is_hot"],
        follow_up_date=snap["follow_up_date"],
        next_action=snap["next_action"],
        reason=reason_text,
        action="restore",
    )
    return {
        **snap,
        "action": "restore",
        "warning": RESTORE_WARNING,
        "effects": _effects_restore(),
        "reason": reason_text,
        "reason_ok": bool(reason_text),
        "blocked": False,
        "already_active": already_active,
        "noop": already_active,
        "preview_fingerprint": fingerprint,
        "expected_updated_at": snap["updated_at"],
        "expected_archived_at": snap["archived_at"],
        "writes": False,
        "confirm_required": True,
    }


def _require_admin_reason(actor: NorthStarUser, reason: str) -> str:
    if not actor.is_administrator:
        raise PermissionError("Administrator required.")
    reason_text = _blank(reason)
    if not reason_text:
        raise StewardError("reason_required")
    return reason_text


def _refuse_stale(preview: dict[str, Any], body: RelationshipActionRequest) -> None:
    if not _has_text(body.preview_fingerprint):
        raise StewardError("stale_preview")
    if preview["preview_fingerprint"] != _blank(body.preview_fingerprint):
        raise StewardError("stale_preview")
    if body.expected_updated_at is not None and _blank(body.expected_updated_at) != preview[
        "expected_updated_at"
    ]:
        raise StewardError("stale_edit")
    if body.expected_archived_at is not None and _blank(body.expected_archived_at) != preview[
        "expected_archived_at"
    ]:
        raise StewardError("stale_preview")


def confirm_remove_relationship(
    conn,
    *,
    actor: NorthStarUser,
    ccr_id: int,
    body: RelationshipActionRequest,
) -> dict[str, Any]:
    reason_text = _require_admin_reason(actor, body.reason)
    if not body.confirm:
        raise StewardError("confirmation_required")
    preview = preview_remove_relationship(conn, actor=actor, ccr_id=int(ccr_id), reason=reason_text)
    _refuse_stale(preview, body)
    if preview["blocked"]:
        raise StewardError("remove_blocked")
    result = remove_relationship(conn, actor=actor, ccr_id=int(ccr_id), reason=reason_text)
    history = provenance_history(
        conn,
        entity_type="client_relationship",
        entity_id=int(ccr_id),
        field="archived_at",
        limit=20,
    )
    events = [row for row in history if _blank(row.get("action")) == ACTION_REMOVE_FROM_CLIENT]
    return {
        **result,
        "reason": reason_text,
        "source_type": SOURCE_MANUAL_ADMIN,
        "action": ACTION_REMOVE_FROM_CLIENT,
        "actor_id": int(actor.id),
        "actor_name": _blank(actor.full_name),
        "provenance": events[:3],
    }


def confirm_restore_relationship(
    conn,
    *,
    actor: NorthStarUser,
    ccr_id: int,
    body: RelationshipActionRequest,
) -> dict[str, Any]:
    reason_text = _require_admin_reason(actor, body.reason)
    if not body.confirm:
        raise StewardError("confirmation_required")
    preview = preview_restore_relationship(conn, actor=actor, ccr_id=int(ccr_id), reason=reason_text)
    _refuse_stale(preview, body)
    restored_id = restore_relationship(conn, actor=actor, ccr_id=int(ccr_id), reason=reason_text)
    history = provenance_history(
        conn,
        entity_type="client_relationship",
        entity_id=int(ccr_id),
        field="archived_at",
        limit=20,
    )
    events = [row for row in history if _blank(row.get("action")) == ACTION_RESTORE_TO_CLIENT]
    snap = load_relationship_snapshot(conn, int(restored_id))
    return {
        "ccr_id": int(restored_id),
        "same_id": int(restored_id) == int(ccr_id),
        "archived": snap["archived"],
        "noop": preview["noop"],
        "code": "already_active" if preview["noop"] else "restored",
        "reason": reason_text,
        "source_type": SOURCE_MANUAL_ADMIN,
        "action": ACTION_RESTORE_TO_CLIENT,
        "actor_id": int(actor.id),
        "actor_name": _blank(actor.full_name),
        "status": snap["status"],
        "assigned_user_id": snap["assigned_user_id"],
        "is_hot": snap["is_hot"],
        "follow_up_date": snap["follow_up_date"],
        "next_action": snap["next_action"],
        "external_record_no": snap["external_record_no"],
        "provenance": events[:3],
    }


def list_company_relationship_events(conn, company_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ccr.id AS ccr_id, ccr.client_id, cl.code, cl.name
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        WHERE ccr.company_id = ?
        ORDER BY cl.name, ccr.id
        """,
        (int(company_id),),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        for item in provenance_history(
            conn,
            entity_type="client_relationship",
            entity_id=int(row["ccr_id"]),
            limit=limit,
        ):
            action = _blank(item.get("action"))
            if action not in {ACTION_REMOVE_FROM_CLIENT, ACTION_RESTORE_TO_CLIENT, "ARCHIVE", "RESTORE"}:
                continue
            payload = dict(item)
            payload["ccr_id"] = int(row["ccr_id"])
            payload["client_id"] = int(row["client_id"])
            payload["client_code"] = _blank(row["code"])
            payload["client_name"] = _blank(row["name"])
            payload["action_label"] = (
                "Removed From Client"
                if action in {ACTION_REMOVE_FROM_CLIENT, "ARCHIVE"}
                else "Restored To Client"
            )
            events.append(payload)
    events.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
    return events[: max(int(limit), 1)]
