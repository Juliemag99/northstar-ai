"""Read-only administrator view of staff users.

Does not create users, change passwords, edit assignments, or write audit rows.
CRM assigned_user_id values are counted only. They are not authorization.
"""

from __future__ import annotations

from datetime import datetime, timezone

from auth_http import _is_locked
from db import get_connection
from staff_rbac import SYSTEM_ADMINISTRATOR, normalize_role

LOGIN_INACTIVE = "inactive"
LOGIN_NO_PASSWORD = "no_password"
LOGIN_LOCKED = "locked"
LOGIN_CAN_SIGN_IN = "can_sign_in"

ACCESS_ALL_CLIENTS = "all_clients"
ACCESS_ASSIGNED = "assigned"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _columns(conn, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _text(row, key: str) -> str:
    if key not in row.keys():
        return ""
    value = row[key]
    return "" if value is None else str(value)


def _canonical_role(row, *, has_staff_role: bool) -> str:
    if has_staff_role:
        stored = normalize_role(row["staff_role"] if "staff_role" in row.keys() else "")
        if stored:
            return stored
    if bool(row["is_administrator"]):
        return SYSTEM_ADMINISTRATOR
    return ""


def login_status_for_row(row, *, now: datetime | None = None) -> str:
    """Safe sign-in state. Inactive wins, then missing password, then lockout."""
    if not bool(row["active"]):
        return LOGIN_INACTIVE
    if not _text(row, "password_hash").strip():
        return LOGIN_NO_PASSWORD
    if _is_locked(row, now or _now()):
        return LOGIN_LOCKED
    return LOGIN_CAN_SIGN_IN


def _load_assignments(conn) -> dict[int, list[dict[str, object]]]:
    if not _table_exists(conn, "user_client_assignments"):
        return {}
    grouped: dict[int, list[dict[str, object]]] = {}
    rows = conn.execute(
        """
        SELECT
            uca.user_id,
            uca.client_id,
            uca.active,
            cl.code AS client_code,
            cl.name AS client_name
        FROM user_client_assignments uca
        JOIN clients cl ON cl.id = uca.client_id
        ORDER BY cl.name COLLATE NOCASE, cl.id
        """
    ).fetchall()
    for row in rows:
        grouped.setdefault(int(row["user_id"]), []).append(
            {
                "client_id": int(row["client_id"]),
                "client_code": _text(row, "client_code"),
                "client_name": _text(row, "client_name"),
                "active": bool(row["active"]),
            }
        )
    return grouped


def _summary(
    row,
    assignments: list[dict[str, object]],
    *,
    has_staff_role: bool,
    now: datetime,
) -> dict[str, object]:
    """Allowlisted public fields. Callers must not merge the raw user row."""
    administrator = bool(row["is_administrator"])
    active_clients = [item for item in assignments if item["active"]]
    return {
        "id": int(row["id"]),
        "full_name": _text(row, "full_name"),
        "email": _text(row, "email"),
        "staff_role": _canonical_role(row, has_staff_role=has_staff_role),
        "is_administrator": administrator,
        "active": bool(row["active"]),
        "is_internal_northstar": bool(row["is_internal_northstar"]),
        "created_at": _text(row, "created_at"),
        "password_updated_at": _text(row, "password_updated_at"),
        "login_status": login_status_for_row(row, now=now),
        "access_scope": ACCESS_ALL_CLIENTS if administrator else ACCESS_ASSIGNED,
        "clients": [
            {
                "client_id": item["client_id"],
                "client_code": item["client_code"],
                "client_name": item["client_name"],
            }
            for item in active_clients
        ],
    }


def _count(conn, sql: str, user_id: int) -> int:
    row = conn.execute(sql, (int(user_id),)).fetchone()
    return int(row[0] if row is not None else 0)


def _crm_ownership(conn, user_id: int) -> dict[str, int]:
    relationships = 0
    workflows = 0
    open_queue = 0
    if _table_exists(conn, "client_company_relationships"):
        relationships = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM client_company_relationships
            WHERE assigned_user_id = ?
            """,
            user_id,
        )
    if _table_exists(conn, "contact_client_workflows"):
        workflows = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM contact_client_workflows
            WHERE assigned_user_id = ?
            """,
            user_id,
        )
    if _table_exists(conn, "work_queue_items"):
        open_queue = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM work_queue_items
            WHERE assigned_user_id = ?
              AND lower(trim(COALESCE(completion_status, ''))) = 'open'
            """,
            user_id,
        )
    return {
        "client_company_relationships": relationships,
        "contact_client_workflows": workflows,
        "open_work_queue_items": open_queue,
    }


def list_admin_users() -> list[dict[str, object]]:
    with get_connection() as conn:
        has_staff_role = "staff_role" in _columns(conn, "users")
        users = conn.execute(
            "SELECT * FROM users ORDER BY full_name COLLATE NOCASE, id"
        ).fetchall()
        assignments = _load_assignments(conn)
        now = _now()
        return [
            _summary(
                row,
                assignments.get(int(row["id"]), []),
                has_staff_role=has_staff_role,
                now=now,
            )
            for row in users
        ]


def get_admin_user(user_id: int) -> dict[str, object]:
    if int(user_id) <= 0:
        raise LookupError("User not found.")
    with get_connection() as conn:
        has_staff_role = "staff_role" in _columns(conn, "users")
        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if row is None:
            raise LookupError("User not found.")
        assignments = _load_assignments(conn).get(int(row["id"]), [])
        payload = _summary(
            row,
            assignments,
            has_staff_role=has_staff_role,
            now=_now(),
        )
        payload["assignments"] = assignments
        payload["crm_ownership"] = _crm_ownership(conn, int(row["id"]))
        return payload
