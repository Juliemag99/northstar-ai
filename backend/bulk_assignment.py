"""DS-10 governed bulk assignment of client-company relationships.

Changes client_company_relationships.assigned_user_id only.
Never assigns Master Company ownership. Never grants client access.
Campaign bulk-assign (unassigned opportunities → campaign) stays separate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from access import user_can_access_client
from appointments_data import is_hot_prospect_status
from client_workspace_data import list_matching_prospect_ccr_ids
from data_steward import (
    ACTION_BULK_ASSIGN_CLIENT_RELATIONSHIP,
    ENTITY_CLIENT_RELATIONSHIP,
    SOURCE_MANUAL_ADMIN,
    StewardError,
    _blank,
    _has_text,
    _now,
    record_provenance,
    sql_active_ccr,
    sql_active_company,
)
from pydantic import BaseModel, Field

from models import NorthStarUser

SELECTION_EXPLICIT = "explicit"
SELECTION_FILTERED = "filtered"
SOURCE_REF = "bulk_assignment"

BLOCK_CODES_400 = frozenset(
    {
        "reason_required",
        "confirmation_required",
        "preview_required",
        "selection_required",
        "client_required",
        "invalid_selection_mode",
        "target_required",
    }
)
BLOCK_CODES_409 = frozenset(
    {
        "mixed_client_selection",
        "assignee_not_authorized_for_client",
        "stale_preview",
        "assignee_not_found",
        "assignee_inactive",
        "fingerprint_mismatch",
    }
)


class BulkAssignmentError(StewardError):
    """Governed bulk-assignment refusal with optional structured payload."""

    def __init__(self, code: str, payload: dict[str, Any] | None = None):
        super().__init__(code)
        self.payload = payload or {}


class ProspectAssignmentFilter(BaseModel):
    """Server-side Prospects filter snapshot for Select All Filtered Results."""

    client_id: int
    q: str = ""
    status: str = ""
    milestone_type: str = ""
    assigned_user_id: int | None = None


class BulkAssignmentRequest(BaseModel):
    """Preview/confirm payload. Session actor wins; spoofed actor fields ignored."""

    selection_mode: str = SELECTION_EXPLICIT
    client_id: int | None = None
    ccr_ids: list[int] = Field(default_factory=list)
    filter: ProspectAssignmentFilter | None = None
    target_user_id: int = 0
    reason: str = ""
    confirm: bool = False
    preview_fingerprint: str = ""
    actor_id: int | None = Field(default=None, description="Ignored. Session user is the actor.")
    user_id: int | None = Field(default=None, description="Ignored.")
    created_by: str | None = Field(default=None, description="Ignored.")


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_mode(value: str) -> str:
    mode = _blank(value).lower()
    if mode not in {SELECTION_EXPLICIT, SELECTION_FILTERED}:
        raise BulkAssignmentError("invalid_selection_mode")
    return mode


def _filter_snapshot(spec: ProspectAssignmentFilter | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    assigned = spec.assigned_user_id
    return {
        "client_id": int(spec.client_id),
        "q": _blank(spec.q),
        "status": _blank(spec.status),
        "milestone_type": _blank(spec.milestone_type),
        "assigned_user_id": None if assigned is None else int(assigned),
    }


def assignment_fingerprint(
    *,
    selection_mode: str,
    client_id: int | None,
    target_user_id: int,
    reason: str,
    filter_spec: dict[str, Any] | None,
    membership: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "selection_mode": selection_mode,
            "client_id": client_id,
            "target_user_id": int(target_user_id),
            "reason": _blank(reason),
            "filter": filter_spec,
            "membership": membership,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def list_eligible_assignees(conn, *, client_id: int) -> list[dict[str, Any]]:
    """Active users already authorized for this client. Does not create accounts."""
    try:
        cid = int(client_id)
    except (TypeError, ValueError) as exc:
        raise BulkAssignmentError("client_required") from exc
    if cid <= 0:
        raise BulkAssignmentError("client_required")
    rows = conn.execute(
        """
        SELECT id, email, full_name, is_administrator, active
        FROM users
        WHERE active = 1
        ORDER BY full_name COLLATE NOCASE, id
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        uid = int(row["id"])
        if not user_can_access_client(uid, cid):
            continue
        out.append(
            {
                "user_id": uid,
                "email": _blank(row["email"]),
                "full_name": _blank(row["full_name"]),
                "is_administrator": bool(row["is_administrator"]),
            }
        )
    return out


def _load_target(conn, *, target_user_id: int, client_id: int) -> dict[str, Any]:
    try:
        uid = int(target_user_id)
    except (TypeError, ValueError) as exc:
        raise BulkAssignmentError("target_required") from exc
    if uid <= 0:
        raise BulkAssignmentError("target_required")
    row = conn.execute(
        "SELECT id, email, full_name, active, is_administrator FROM users WHERE id = ?",
        (uid,),
    ).fetchone()
    if row is None:
        raise BulkAssignmentError("assignee_not_found")
    if not bool(row["active"]):
        raise BulkAssignmentError("assignee_inactive")
    authorized = user_can_access_client(uid, int(client_id))
    return {
        "user_id": uid,
        "email": _blank(row["email"]),
        "full_name": _blank(row["full_name"]),
        "is_administrator": bool(row["is_administrator"]),
        "authorized_for_client": bool(authorized),
    }


def _load_client(conn, client_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, code, name FROM clients WHERE id = ?",
        (int(client_id),),
    ).fetchone()
    if row is None:
        raise BulkAssignmentError("client_required", {"message": "Client not found."})
    return {
        "client_id": int(row["id"]),
        "code": _blank(row["code"]),
        "name": _blank(row["name"]),
    }


def _membership_tuple(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ccr_id": int(row["ccr_id"]),
        "company_id": int(row.get("company_id") or 0),
        "assigned_user_id": row.get("assigned_user_id"),
        "status": _blank(row.get("status")),
        "inactive_removed": bool(row.get("inactive_removed")),
        "archived_company": bool(row.get("archived_company")),
        "missing": bool(row.get("missing")),
    }


def _classify_hot(status: str) -> bool:
    return is_hot_prospect_status(status)


def _classify_appointment(status: str, next_action: str) -> bool:
    blob = f"{status} {next_action}".strip().lower()
    return "appointment set" in blob


def _load_ccr_rows(conn, ccr_ids: list[int]) -> dict[int, dict[str, Any]]:
    ids = [int(i) for i in ccr_ids if int(i) > 0]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    ccr_active_sql = sql_active_ccr(conn, "ccr")
    co_active_sql = sql_active_company(conn, "co")
    rows = conn.execute(
        f"""
        SELECT
            ccr.id AS ccr_id,
            ccr.client_id,
            ccr.company_id,
            ccr.assigned_user_id,
            COALESCE(ccr.status, '') AS status,
            COALESCE(ccr.is_hot, 0) AS is_hot,
            COALESCE(ccr.next_action, '') AS next_action,
            ccr.follow_up_date,
            COALESCE(ccr.notes, '') AS notes,
            COALESCE(ccr.external_record_no, '') AS external_record_no,
            COALESCE(ccr.priority, '') AS priority,
            cl.code AS client_code,
            cl.name AS client_name,
            co.company_name,
            CASE WHEN {ccr_active_sql} THEN 0 ELSE 1 END AS inactive_removed,
            CASE WHEN {co_active_sql} THEN 0 ELSE 1 END AS archived_company,
            COALESCE(assigned_u.full_name, '') AS assigned_name
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        JOIN companies co ON co.id = ccr.company_id
        LEFT JOIN users assigned_u ON assigned_u.id = ccr.assigned_user_id
        WHERE ccr.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    packed: dict[int, dict[str, Any]] = {}
    for row in rows:
        assigned = _int_or_none(row["assigned_user_id"])
        packed[int(row["ccr_id"])] = {
            "ccr_id": int(row["ccr_id"]),
            "client_id": int(row["client_id"]),
            "client_code": _blank(row["client_code"]),
            "client_name": _blank(row["client_name"]),
            "company_id": int(row["company_id"]),
            "company_name": _blank(row["company_name"]),
            "assigned_user_id": assigned,
            "assigned_name": _blank(row["assigned_name"]) or "Unassigned",
            "status": _blank(row["status"]),
            "is_hot_flag": int(row["is_hot"] or 0),
            "next_action": _blank(row["next_action"]),
            "follow_up_date": _blank(row["follow_up_date"]),
            "notes": _blank(row["notes"]),
            "external_record_no": _blank(row["external_record_no"]),
            "priority": _blank(row["priority"]),
            "inactive_removed": bool(int(row["inactive_removed"] or 0)),
            "archived_company": bool(int(row["archived_company"] or 0)),
            "missing": False,
        }
    return packed


def _resolve_selection(conn, body: BulkAssignmentRequest) -> dict[str, Any]:
    mode = _normalize_mode(body.selection_mode)
    requested_ids: list[int] = []
    filter_spec = _filter_snapshot(body.filter)
    if mode == SELECTION_FILTERED:
        spec = body.filter
        if spec is None or int(spec.client_id or 0) <= 0:
            raise BulkAssignmentError("client_required")
        requested_ids = list_matching_prospect_ccr_ids(
            client_id=int(spec.client_id),
            q=spec.q,
            status=spec.status,
            milestone_type=spec.milestone_type or None,
            assigned_user_id=spec.assigned_user_id,
            conn=conn,
        )
        declared_client = int(spec.client_id)
    else:
        requested_ids = []
        seen: set[int] = set()
        for raw in body.ccr_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid <= 0 or cid in seen:
                continue
            seen.add(cid)
            requested_ids.append(cid)
        declared_client = int(body.client_id) if body.client_id else 0

    if not requested_ids:
        raise BulkAssignmentError("selection_required")

    loaded = _load_ccr_rows(conn, requested_ids)
    rows: list[dict[str, Any]] = []
    client_counts: dict[int, dict[str, Any]] = {}
    for ccr_id in requested_ids:
        row = loaded.get(ccr_id)
        if row is None:
            rows.append(
                {
                    "ccr_id": ccr_id,
                    "client_id": 0,
                    "company_id": 0,
                    "assigned_user_id": None,
                    "status": "",
                    "inactive_removed": False,
                    "archived_company": False,
                    "missing": True,
                }
            )
            continue
        rows.append(row)
        cid = int(row["client_id"])
        bucket = client_counts.setdefault(
            cid,
            {
                "client_id": cid,
                "code": row.get("client_code") or "",
                "name": row.get("client_name") or "",
                "n": 0,
            },
        )
        bucket["n"] += 1

    mixed = len(client_counts) > 1
    resolved_client_id = None
    if len(client_counts) == 1:
        resolved_client_id = next(iter(client_counts.keys()))
    if declared_client and resolved_client_id and declared_client != resolved_client_id:
        mixed = True
        if declared_client not in client_counts:
            client_counts[declared_client] = {
                "client_id": declared_client,
                "code": "",
                "name": "",
                "n": 0,
            }
    if declared_client and not resolved_client_id and not mixed:
        resolved_client_id = declared_client

    return {
        "selection_mode": mode,
        "requested_ids": requested_ids,
        "rows": rows,
        "client_counts": list(client_counts.values()),
        "mixed_client": mixed,
        "resolved_client_id": resolved_client_id,
        "filter": filter_spec,
        "filtered_result_count": len(requested_ids) if mode == SELECTION_FILTERED else None,
    }


def _owner_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("missing") or row.get("inactive_removed") or row.get("archived_company"):
            continue
        uid = row.get("assigned_user_id")
        key = "unassigned" if uid is None else str(int(uid))
        bucket = counts.setdefault(
            key,
            {
                "assigned_user_id": uid,
                "name": "Unassigned" if uid is None else (row.get("assigned_name") or f"User {uid}"),
                "n": 0,
            },
        )
        bucket["n"] += 1
    return sorted(counts.values(), key=lambda item: (-int(item["n"]), str(item["name"])))


def preview_bulk_assignment(
    conn,
    *,
    actor: NorthStarUser,
    body: BulkAssignmentRequest,
) -> dict[str, Any]:
    """Read-only preview. Performs ZERO assignment writes."""
    if actor is None or not bool(getattr(actor, "is_administrator", False)):
        raise PermissionError("Administrator access required.")
    if not _has_text(body.reason):
        raise BulkAssignmentError("reason_required")

    resolved = _resolve_selection(conn, body)
    mixed = bool(resolved["mixed_client"])
    client_id = resolved["resolved_client_id"]
    rows = resolved["rows"]
    target: dict[str, Any] | None = None
    unauthorized = False
    if client_id and not mixed:
        target = _load_target(
            conn, target_user_id=int(body.target_user_id), client_id=int(client_id)
        )
        unauthorized = not bool(target["authorized_for_client"])
    elif int(body.target_user_id or 0) <= 0:
        raise BulkAssignmentError("target_required")

    new_assignments = 0
    reassignments = 0
    already_assigned = 0
    inactive_removed = 0
    archived_company = 0
    invalid_missing = 0
    eligible_ids: list[int] = []
    change_ids: list[int] = []
    hot_count = 0
    follow_up_count = 0
    appointment_count = 0
    target_uid = int(target["user_id"]) if target else 0

    for row in rows:
        if row.get("missing"):
            invalid_missing += 1
            continue
        if row.get("inactive_removed"):
            inactive_removed += 1
            continue
        if row.get("archived_company"):
            archived_company += 1
            continue
        if mixed or unauthorized or not target:
            continue
        eligible_ids.append(int(row["ccr_id"]))
        current = row.get("assigned_user_id")
        if current is not None and int(current) == target_uid:
            already_assigned += 1
            continue
        change_ids.append(int(row["ccr_id"]))
        if current is None:
            new_assignments += 1
        else:
            reassignments += 1
        if _classify_hot(row.get("status") or ""):
            hot_count += 1
        if _blank(row.get("follow_up_date")):
            follow_up_count += 1
        if _classify_appointment(row.get("status") or "", row.get("next_action") or ""):
            appointment_count += 1

    membership = [_membership_tuple(row) for row in sorted(rows, key=lambda r: int(r["ccr_id"]))]
    client = _load_client(conn, int(client_id)) if client_id and not mixed else None
    fingerprint = assignment_fingerprint(
        selection_mode=resolved["selection_mode"],
        client_id=int(client_id) if client_id and not mixed else None,
        target_user_id=target_uid,
        reason=body.reason,
        filter_spec=resolved["filter"],
        membership=membership,
    )
    block_code = ""
    if mixed:
        block_code = "mixed_client_selection"
    elif unauthorized:
        block_code = "assignee_not_authorized_for_client"
    confirm_allowed = block_code == "" and target is not None and client is not None
    return {
        "ok": confirm_allowed,
        "writes": False,
        "selection_mode": resolved["selection_mode"],
        "client": client,
        "target": target,
        "reason": _blank(body.reason),
        "filtered_result_count": resolved["filtered_result_count"],
        "selected_count": len(resolved["requested_ids"]),
        "eligible": len(eligible_ids),
        "new_assignments": new_assignments,
        "reassignments": reassignments,
        "already_assigned": already_assigned,
        "inactive_removed": inactive_removed,
        "archived_company": archived_company,
        "invalid_missing": invalid_missing,
        "mixed_client": mixed,
        "clients": resolved["client_counts"] if mixed else [],
        "unauthorized_target": unauthorized,
        "block_code": block_code,
        "confirm_allowed": confirm_allowed,
        "current_assignment_summary": _owner_summary(rows),
        "proposed": (
            f"Assign {new_assignments + reassignments} relationship"
            f"{'' if (new_assignments + reassignments) == 1 else 's'} to "
            f"{(target or {}).get('full_name') or 'the selected user'}"
            if confirm_allowed
            else ""
        ),
        "hot_count": hot_count,
        "follow_up_count": follow_up_count,
        "appointment_set_count": appointment_count,
        "preview_fingerprint": fingerprint,
        "policy": {
            "mixed_client": "all_or_nothing_block",
            "unauthorized_target": "all_or_nothing_block",
            "inactive_or_archived": "skip_deterministically",
            "already_assigned": "no_op",
        },
    }


def confirm_bulk_assignment(
    conn,
    *,
    actor: NorthStarUser,
    body: BulkAssignmentRequest,
) -> dict[str, Any]:
    """Atomic CCR assigned_user_id updates for eligible rows only."""
    if actor is None or not bool(getattr(actor, "is_administrator", False)):
        raise PermissionError("Administrator access required.")
    if not bool(body.confirm):
        raise BulkAssignmentError("confirmation_required")
    if not _has_text(body.preview_fingerprint):
        raise BulkAssignmentError("preview_required")

    preview = preview_bulk_assignment(conn, actor=actor, body=body)
    if _blank(body.preview_fingerprint) != _blank(preview.get("preview_fingerprint")):
        raise BulkAssignmentError("stale_preview")
    if not preview.get("confirm_allowed"):
        code = _blank(preview.get("block_code")) or "mixed_client_selection"
        raise BulkAssignmentError(
            code,
            {
                "clients": preview.get("clients") or [],
                "unauthorized_target": bool(preview.get("unauthorized_target")),
            },
        )

    client = preview["client"]
    target = preview["target"]
    cid = int(client["client_id"])
    uid = int(target["user_id"])
    resolved = _resolve_selection(conn, body)
    changed = 0
    reassigned = 0
    newly_assigned = 0
    already = 0
    skipped = 0
    failed = 0
    now = _now()
    for row in resolved["rows"]:
        if (
            row.get("missing")
            or row.get("inactive_removed")
            or row.get("archived_company")
            or int(row.get("client_id") or 0) != cid
        ):
            skipped += 1
            continue
        current = row.get("assigned_user_id")
        if current is not None and int(current) == uid:
            already += 1
            continue
        conn.execute(
            """
            UPDATE client_company_relationships
            SET assigned_user_id = ?, updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (uid, now, int(row["ccr_id"]), cid),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            failed += 1
            raise BulkAssignmentError("assignment_write_failed")
        record_provenance(
            conn,
            entity_type=ENTITY_CLIENT_RELATIONSHIP,
            entity_id=int(row["ccr_id"]),
            field="assigned_user_id",
            old_value="" if current is None else str(int(current)),
            new_value=str(uid),
            source_type=SOURCE_MANUAL_ADMIN,
            source_ref=SOURCE_REF,
            action=ACTION_BULK_ASSIGN_CLIENT_RELATIONSHIP,
            reason=_blank(body.reason),
            client_id=cid,
            actor=actor,
            trusted=True,
            changed_by_user_id=int(actor.id),
        )
        changed += 1
        if current is None:
            newly_assigned += 1
        else:
            reassigned += 1

    if failed:
        raise BulkAssignmentError("assignment_write_failed")

    return {
        "ok": True,
        "writes": True,
        "selection_mode": preview["selection_mode"],
        "client": client,
        "target": target,
        "reason": _blank(body.reason),
        "selected": int(preview["selected_count"]),
        "eligible": int(preview["eligible"]),
        "changed": changed,
        "new_assignments": newly_assigned,
        "reassigned": reassigned,
        "already_assigned": already,
        "skipped": skipped,
        "failed": failed,
        "preview_fingerprint": preview["preview_fingerprint"],
        "message": (
            f"{changed} relationship{'s' if changed != 1 else ''} assigned to "
            f"{target.get('full_name') or 'the selected user'}."
            f" {already} were already assigned."
            f" {failed} failed."
        ),
    }
