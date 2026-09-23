"""DS-9 governed Master Company archive and restore.

Reuses companies.archived_at / archived_by_user_id / archive_reason.
Blocks archive while any CCR is active. Does not enable delete or merge.
Restore does not reactivate removed client relationships.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from data_steward import (
    ACTION_ARCHIVE_MASTER_COMPANY,
    ACTION_RESTORE_MASTER_COMPANY,
    SOURCE_MANUAL_ADMIN,
    StewardError,
    _blank,
    _has_text,
    _load,
    archive_company,
    inspect_company_dependencies,
    is_archived,
    list_active_company_relationships,
    provenance_history,
    restore_company,
)
from data_steward_amend import (
    HIGH_COLLISION_REASONS,
    find_amend_collisions,
    list_linked_clients,
)
from pydantic import BaseModel, Field

from models import NorthStarUser

ARCHIVE_INSTRUCTION = (
    "Remove this company from all active clients before archiving the Master Company."
)
RESTORE_WARNING = (
    "This restores the existing Master Company. It does not create a new company."
)
RESTORE_CCR_WARNING = (
    "Restoring the Master Company does not restore removed client relationships."
)


class MasterArchiveRequest(BaseModel):
    """Governed company archive/restore payload. Session actor wins."""

    reason: str = ""
    confirm: bool = False
    preview_fingerprint: str = ""
    expected_updated_at: str | None = None
    expected_archived_at: str | None = None
    force: bool | None = Field(default=None, description="Ignored. Active CCR cannot be bypassed.")
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


def _count_where(conn, table: str, where: str, params: tuple) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])


def master_archive_fingerprint(
    *,
    company_id: int,
    archived_at: str,
    updated_at: str,
    active_ccr_ids: list[int],
    reason: str,
    action: str,
) -> str:
    payload = json.dumps(
        {
            "company_id": int(company_id),
            "archived_at": _blank(archived_at),
            "updated_at": _blank(updated_at),
            "active_ccr_ids": [int(i) for i in active_ccr_ids],
            "reason": _blank(reason),
            "action": _blank(action),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_archive_dependencies(
    *,
    active: list[dict[str, Any]],
    linked: list[dict[str, Any]],
    counts: dict[str, Any],
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        items.append({"severity": severity, "code": code, "message": message})

    if active:
        names = ", ".join(
            f"{row.get('client_name') or row.get('client_code')} (CCR #{row['ccr_id']})"
            for row in active
        )
        add("block", "active_ccr", f"Active client relationship(s) must be removed first: {names}.")
        add("block", "active_ccr_instruction", ARCHIVE_INSTRUCTION)
    removed = [row for row in linked if row.get("archived")]
    if removed:
        add(
            "info",
            "removed_ccr",
            f"{len(removed)} removed client relationship(s) remain on this Master Company.",
        )
    if int(counts.get("campaigns") or 0):
        add(
            "warning",
            "campaign_membership",
            "Historical campaign membership is preserved; the company leaves active targeting.",
        )
    if int(counts.get("contacts") or 0):
        add("info", "contacts", "Contacts remain attached to this Master Company and are not archived.")
    if int(counts.get("aliases") or 0):
        add("info", "aliases", "Aliases remain on this Master Company.")
    if int(counts.get("locations") or 0):
        add("info", "locations", "Locations remain on this Master Company.")
    if int(counts.get("source_identities") or 0):
        add("info", "identities", "Source identities remain on this Master Company.")
    if int(counts.get("history") or 0) or int(counts.get("notes") or 0):
        add("info", "history", "Notes and shared history remain.")
    if int(counts.get("activities") or 0):
        add("info", "activities", "Activity rows remain as history.")
    if int(counts.get("sales") or 0) or int(counts.get("appointments") or 0):
        add("info", "commercial", "Sales events and appointments remain as history.")
    if int(counts.get("tasks") or 0):
        add("info", "tasks", "Tasks remain; they leave active specialist queues.")
    if int(counts.get("research") or 0):
        add("info", "research", "Research records remain.")
    if int(counts.get("merge_as_source") or 0) or int(counts.get("merge_as_survivor") or 0):
        add("info", "merge_history", "Merge history rows remain. Archive does not merge or delete.")
    return items


def load_master_archive_snapshot(conn, company_id: int) -> dict[str, Any]:
    stored = _load(conn, "companies", int(company_id))
    deps = inspect_company_dependencies(conn, int(company_id))
    counts = dict(deps.get("counts") or {})
    counts["tasks"] = _count_where(
        conn, "work_queue_items", "company_id = ?", (int(company_id),)
    ) + _count_where(conn, "activities", "company_id = ? AND lower(trim(activity_type)) = 'follow-up'", (int(company_id),))
    counts["research"] = _count_where(
        conn, "company_research", "company_id = ?", (int(company_id),)
    ) + _count_where(conn, "research_people", "company_id = ?", (int(company_id),))
    linked = list_linked_clients(conn, int(company_id), include_archived=True)
    active = list_active_company_relationships(conn, int(company_id))
    archived = is_archived(conn, "companies", int(company_id))
    archived_by_id = _int_or_none(stored.get("archived_by_user_id"))
    return {
        "company_id": int(company_id),
        "company_name": _blank(stored.get("company_name")),
        "address": _blank(stored.get("address")),
        "city": _blank(stored.get("city")),
        "state": _blank(stored.get("state")),
        "zip": _blank(stored.get("zip")),
        "phone": _blank(stored.get("legacy_phone")),
        "website": _blank(stored.get("website")),
        "external_record_no": _blank(stored.get("external_record_no")),
        "archived": archived,
        "archived_at": _blank(stored.get("archived_at")),
        "archived_by_user_id": archived_by_id,
        "archived_by_name": _user_name(conn, archived_by_id),
        "archive_reason": _blank(stored.get("archive_reason")),
        "updated_at": _blank(stored.get("last_updated_at") or stored.get("updated_at")),
        "linked_clients": linked,
        "active_relationships": active,
        "active_ccr_count": len(active),
        "removed_ccr_count": sum(1 for row in linked if row.get("archived")),
        "contact_count": int(counts.get("contacts") or 0),
        "alias_count": int(counts.get("aliases") or 0),
        "location_count": int(counts.get("locations") or 0),
        "identity_count": int(counts.get("source_identities") or 0),
        "campaign_count": int(counts.get("campaigns") or 0),
        "history_count": int(counts.get("history") or 0),
        "activity_count": int(counts.get("activities") or 0),
        "counts": counts,
        "dependencies": classify_archive_dependencies(active=active, linked=linked, counts=counts),
    }


def preview_archive_company(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    reason: str = "",
) -> dict[str, Any]:
    _ = actor
    snap = load_master_archive_snapshot(conn, int(company_id))
    reason_text = _blank(reason)
    blocking = [d for d in snap["dependencies"] if d["severity"] == "block"]
    already = bool(snap["archived"])
    eligible = not blocking and not already
    active_ids = [int(row["ccr_id"]) for row in snap["active_relationships"]]
    fingerprint = master_archive_fingerprint(
        company_id=int(company_id),
        archived_at=snap["archived_at"],
        updated_at=snap["updated_at"],
        active_ccr_ids=active_ids,
        reason=reason_text,
        action="archive",
    )
    return {
        **snap,
        "action": "archive",
        "warning": ARCHIVE_INSTRUCTION if blocking else "This archives the Master Company. It is not a delete.",
        "instruction": ARCHIVE_INSTRUCTION,
        "effects": [
            "The same Master Company id is archived.",
            "Contacts, removed CCRs, aliases, locations, identities, and history remain.",
            "The company leaves ordinary active operational views.",
            "Duplicate detection still sees this company so a second master record is not created.",
        ],
        "reason": reason_text,
        "reason_ok": bool(reason_text),
        "blocked": bool(blocking),
        "eligible": eligible,
        "already_archived": already,
        "noop": already,
        "preview_fingerprint": fingerprint,
        "expected_updated_at": snap["updated_at"],
        "expected_archived_at": snap["archived_at"],
        "writes": False,
        "confirm_required": True,
        "force_ignored": True,
    }


def restore_collisions(conn, company_id: int) -> list[dict[str, Any]]:
    stored = _load(conn, "companies", int(company_id))
    proposed = {
        "company_name": _blank(stored.get("company_name")),
        "address": _blank(stored.get("address")),
        "city": _blank(stored.get("city")),
        "state": _blank(stored.get("state")),
        "phone": _blank(stored.get("legacy_phone")),
        "website": _blank(stored.get("website")),
    }
    collisions = find_amend_collisions(conn, company_id=int(company_id), proposed=proposed)
    blocking: list[dict[str, Any]] = []
    for row in collisions:
        if is_archived(conn, "companies", int(row["company_id"])):
            continue
        if row.get("severity") != "block" and not any(
            reason in HIGH_COLLISION_REASONS for reason in (row.get("reasons") or [])
        ):
            continue
        blocking.append({**row, "severity": "block"})
    return blocking


def preview_restore_company(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    reason: str = "",
) -> dict[str, Any]:
    _ = actor
    snap = load_master_archive_snapshot(conn, int(company_id))
    reason_text = _blank(reason)
    already_active = not bool(snap["archived"])
    collisions = restore_collisions(conn, int(company_id)) if snap["archived"] else []
    blocked = bool(collisions)
    fingerprint = master_archive_fingerprint(
        company_id=int(company_id),
        archived_at=snap["archived_at"],
        updated_at=snap["updated_at"],
        active_ccr_ids=[int(row["ccr_id"]) for row in snap["active_relationships"]],
        reason=reason_text,
        action="restore",
    )
    return {
        **snap,
        "action": "restore",
        "warning": RESTORE_WARNING,
        "ccr_warning": RESTORE_CCR_WARNING,
        "effects": [
            "The same Master Company id is reactivated. No new company is created.",
            "Removed client relationships stay removed until restored separately.",
            "Contacts, aliases, locations, identities, RN, and history remain as stored.",
        ],
        "reason": reason_text,
        "reason_ok": bool(reason_text),
        "blocked": blocked,
        "eligible": bool(snap["archived"]) and not blocked,
        "already_active": already_active,
        "noop": already_active,
        "collisions": collisions,
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


def _refuse_stale(preview: dict[str, Any], body: MasterArchiveRequest) -> None:
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


def confirm_archive_company(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    body: MasterArchiveRequest,
) -> dict[str, Any]:
    _ = body.force
    reason_text = _require_admin_reason(actor, body.reason)
    if not body.confirm:
        raise StewardError("confirmation_required")
    preview = preview_archive_company(conn, actor=actor, company_id=int(company_id), reason=reason_text)
    _refuse_stale(preview, body)
    if preview["blocked"] or (not preview["eligible"] and not preview["noop"]):
        raise StewardError("active_ccr_blocks_archive")
    result = archive_company(conn, actor=actor, company_id=int(company_id), reason=reason_text)
    history = provenance_history(
        conn,
        entity_type="company",
        entity_id=int(company_id),
        field="archived_at",
        limit=20,
    )
    events = [row for row in history if _blank(row.get("action")) == ACTION_ARCHIVE_MASTER_COMPANY]
    return {
        **result,
        "reason": reason_text,
        "source_type": SOURCE_MANUAL_ADMIN,
        "action": ACTION_ARCHIVE_MASTER_COMPANY,
        "actor_id": int(actor.id),
        "actor_name": _blank(actor.full_name),
        "provenance": events[:3],
        "active_ccr_count": 0 if not result.get("noop") else preview["active_ccr_count"],
    }


def confirm_restore_company(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    body: MasterArchiveRequest,
) -> dict[str, Any]:
    reason_text = _require_admin_reason(actor, body.reason)
    if not body.confirm:
        raise StewardError("confirmation_required")
    preview = preview_restore_company(conn, actor=actor, company_id=int(company_id), reason=reason_text)
    _refuse_stale(preview, body)
    if preview["blocked"]:
        raise StewardError("restore_collision")
    restored_id = restore_company(conn, actor=actor, company_id=int(company_id), reason=reason_text)
    snap = load_master_archive_snapshot(conn, int(restored_id))
    history = provenance_history(
        conn,
        entity_type="company",
        entity_id=int(company_id),
        field="archived_at",
        limit=20,
    )
    events = [row for row in history if _blank(row.get("action")) == ACTION_RESTORE_MASTER_COMPANY]
    return {
        "company_id": int(restored_id),
        "same_id": int(restored_id) == int(company_id),
        "archived": snap["archived"],
        "noop": preview["noop"],
        "code": "already_active" if preview["noop"] else "restored",
        "reason": reason_text,
        "source_type": SOURCE_MANUAL_ADMIN,
        "action": ACTION_RESTORE_MASTER_COMPANY,
        "actor_id": int(actor.id),
        "actor_name": _blank(actor.full_name),
        "linked_clients": snap["linked_clients"],
        "active_ccr_count": snap["active_ccr_count"],
        "provenance": events[:3],
        "ccr_warning": RESTORE_CCR_WARNING,
    }


def list_company_lifecycle_events(conn, company_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in provenance_history(
        conn,
        entity_type="company",
        entity_id=int(company_id),
        field="archived_at",
        limit=limit,
    ):
        action = _blank(item.get("action"))
        if action not in {
            ACTION_ARCHIVE_MASTER_COMPANY,
            ACTION_RESTORE_MASTER_COMPANY,
            "ARCHIVE",
            "RESTORE",
        }:
            continue
        payload = dict(item)
        payload["action_label"] = (
            "Archived Master Company"
            if action in {ACTION_ARCHIVE_MASTER_COMPANY, "ARCHIVE"}
            else "Restored Master Company"
        )
        events.append(payload)
    events.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
    return events[: max(int(limit), 1)]
