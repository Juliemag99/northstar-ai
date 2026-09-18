"""Client-scoped bulk CCR workflow-ownership transfer.

Changes client_company_relationships.assigned_user_id only.
Does not rewrite notes, history authorship, or login ACLs.

Dry-run is the default. Live northstar.db apply is refused unless an env
flag plus confirm_live=True are both present.
"""

from __future__ import annotations

from typing import Any

from access import user_can_access_client
from db import get_connection
from staff_provisioning import (
    StaffProvisionError,
    database_kind,
    refuse_live_write,
    connection_file_path,
)
from staff_rbac import load_user_staff_role

ALLOW_FLAG = "NORTHSTAR_ALLOW_CCR_BOOK_ASSIGN"
AUDIT_ACTOR = "ccr_book_assignment"


class BookAssignmentError(StaffProvisionError):
    """Fail-closed book-assignment error."""


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _ccr_archive_sql(conn, alias: str = "") -> str:
    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(client_company_relationships)").fetchall()}
    if "archived_at" not in cols:
        return ""
    column = f"{alias}.archived_at" if alias else "archived_at"
    return f" AND TRIM(COALESCE({column}, '')) = ''"


def preview_ccr_book_assignment(
    *,
    client_id: int,
    target_user_id: int,
    conn=None,
) -> dict[str, Any]:
    """Read-only preview of a client-scoped book transfer."""
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        return _preview(conn, client_id=int(client_id), target_user_id=int(target_user_id))
    finally:
        if owns:
            conn.close()


def _load_client(conn, client_id: int) -> dict[str, Any]:
    try:
        cid = int(client_id)
    except (TypeError, ValueError) as exc:
        raise BookAssignmentError("client_id must be a positive integer.") from exc
    if cid <= 0:
        raise BookAssignmentError("client_id must be a positive integer.")
    row = conn.execute(
        "SELECT id, code, name FROM clients WHERE id = ?",
        (cid,),
    ).fetchone()
    if row is None:
        raise BookAssignmentError(f"client_id {cid} does not exist.")
    return {
        "client_id": int(row["id"]),
        "code": str(row["code"] or ""),
        "name": str(row["name"] or ""),
    }


def _load_target(conn, target_user_id: int, client_id: int) -> dict[str, Any]:
    try:
        uid = int(target_user_id)
    except (TypeError, ValueError) as exc:
        raise BookAssignmentError("target_user_id must be a positive integer.") from exc
    if uid <= 0:
        raise BookAssignmentError("target_user_id must be a positive integer.")
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        raise BookAssignmentError(f"target user {uid} does not exist.")
    if not bool(row["active"]):
        raise BookAssignmentError("Target user is inactive.")
    if not user_can_access_client(uid, int(client_id)):
        raise BookAssignmentError(
            "Target user does not have an active client assignment for this client."
        )
    return {
        "user_id": uid,
        "email": str(row["email"] or ""),
        "full_name": str(row["full_name"] or ""),
        "staff_role": load_user_staff_role(uid, conn=conn),
        "is_administrator": bool(row["is_administrator"]),
    }


def _preview(conn, *, client_id: int, target_user_id: int) -> dict[str, Any]:
    client = _load_client(conn, client_id)
    target = _load_target(conn, target_user_id, client["client_id"])
    archive = _ccr_archive_sql(conn)
    ccr_archive = _ccr_archive_sql(conn, "ccr")
    cid = client["client_id"]
    uid = target["user_id"]
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM client_company_relationships WHERE client_id = ?{archive}",
            (cid,),
        ).fetchone()[0]
    )
    already = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM client_company_relationships
            WHERE client_id = ? AND assigned_user_id = ?{archive}
            """,
            (cid, uid),
        ).fetchone()[0]
    )
    unassigned = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM client_company_relationships
            WHERE client_id = ? AND assigned_user_id IS NULL{archive}
            """,
            (cid,),
        ).fetchone()[0]
    )
    other = total - already - unassigned
    would_update = total - already
    current_owners = [
        {
            "assigned_user_id": None if r["assigned_user_id"] is None else int(r["assigned_user_id"]),
            "name": _blank(r["full_name"]) or "(unassigned)",
            "n": int(r["n"]),
        }
        for r in conn.execute(
            f"""
            SELECT ccr.assigned_user_id, u.full_name, COUNT(*) AS n
            FROM client_company_relationships ccr
            LEFT JOIN users u ON u.id = ccr.assigned_user_id
            WHERE ccr.client_id = ?{ccr_archive}
            GROUP BY ccr.assigned_user_id
            ORDER BY n DESC
            """,
            (cid,),
        ).fetchall()
    ]
    return {
        "ok": True,
        "dry_run": True,
        "applied": False,
        "client": client,
        "target": {
            "user_id": target["user_id"],
            "email": target["email"],
            "full_name": target["full_name"],
            "staff_role": target["staff_role"],
        },
        "total_active_ccr": total,
        "already_assigned_to_target": already,
        "currently_unassigned": unassigned,
        "currently_other_owner": other,
        "would_update": would_update,
        "updated": 0,
        "current_owners": current_owners,
        "notes_unchanged": True,
        "history_authorship_unchanged": True,
    }


def apply_ccr_book_assignment(
    *,
    client_id: int,
    target_user_id: int,
    dry_run: bool = True,
    conn=None,
    confirm_live: bool | None = None,
    actor_label: str = AUDIT_ACTOR,
) -> dict[str, Any]:
    """Transfer all active CCRs for one client to one user.

    dry_run=True never writes. Cross-client mutation is impossible because
    client_id is required and the UPDATE is constrained to that client_id.
    """
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        preview = _preview(conn, client_id=int(client_id), target_user_id=int(target_user_id))
        if dry_run:
            preview["database_kind"] = database_kind(connection_file_path(conn))
            return preview
        target_path = refuse_live_write(
            conn,
            confirm_live=confirm_live,
            action="apply a CCR book assignment",
            allow_flag=ALLOW_FLAG,
        )
        cid = int(preview["client"]["client_id"])
        uid = int(preview["target"]["user_id"])
        archive = _ccr_archive_sql(conn)
        changing = conn.execute(
            f"""
            SELECT id, assigned_user_id, TRIM(COALESCE(external_record_no, '')) AS rn
            FROM client_company_relationships
            WHERE client_id = ? AND (assigned_user_id IS NULL OR assigned_user_id != ?){archive}
            """,
            (cid, uid),
        ).fetchall()
        conn.execute(
            f"""
            UPDATE client_company_relationships
            SET assigned_user_id = ?, updated_at = datetime('now')
            WHERE client_id = ? AND (assigned_user_id IS NULL OR assigned_user_id != ?){archive}
            """,
            (uid, cid, uid),
        )
        updated = conn.execute("SELECT changes()").fetchone()[0]
        client_code = str(preview["client"]["code"] or "")
        changed_by = _blank(actor_label) or AUDIT_ACTOR
        for row in changing:
            old = "" if row["assigned_user_id"] is None else str(int(row["assigned_user_id"]))
            rn = _blank(row["rn"]) or f"ccr:{int(row['id'])}"
            conn.execute(
                """
                INSERT INTO field_audit_log (
                    client_code, external_record_no, field_name,
                    old_value, new_value, changed_by
                ) VALUES (?, ?, 'assigned_user_id', ?, ?, ?)
                """,
                (client_code, rn, old, str(uid), changed_by),
            )
        from data_steward import ENTITY_CLIENT_RELATIONSHIP, record_changed_fields

        for row in changing:
            record_changed_fields(
                conn,
                entity_type=ENTITY_CLIENT_RELATIONSHIP,
                entity_id=int(row["id"]),
                changes=[("assigned_user_id", row["assigned_user_id"], uid)],
                source_type="MANUAL_ADMIN",
                source_ref="ccr_book_assignment",
                action="AMEND",
                client_id=cid,
                trusted=True,
                changed_by_user_id=None,
            )
        if owns:
            conn.commit()
        after = _preview(conn, client_id=cid, target_user_id=uid)
        after["dry_run"] = False
        after["applied"] = True
        after["updated"] = int(updated)
        after["would_update"] = 0
        after["database_path"] = str(target_path)
        after["database_kind"] = database_kind(target_path)
        if int(after["already_assigned_to_target"]) != int(after["total_active_ccr"]):
            raise BookAssignmentError("Book assignment did not cover every active CCR.")
        return after
    finally:
        if owns:
            conn.close()
