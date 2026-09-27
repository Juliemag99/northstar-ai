"""Administrator view of staff users, plus audited non-admin creation.

Create does not assign CRM records. assigned_user_id remains workflow
ownership, not client authorization.

Future UM-2C last-administrator protection must not treat every active
is_administrator row as a person who can sign in. An administrator row can
have no password_hash (login_status no_password). This module does not
demote or deactivate existing administrators.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from auth_http import _is_locked
from auth_passwords import hash_password
from db import get_connection
from staff_provisioning import StaffProvisionError, canonicalize_email
from staff_rbac import (
    APPOINTMENT_SETTER,
    OPERATIONS_ADMIN,
    READ_ONLY,
    REVOPS_MANAGER,
    REVOPS_SPECIALIST,
    SYSTEM_ADMINISTRATOR,
    normalize_role,
)

# Creation cannot grant administrator-wide roles. Stored value is canonical.
CREATABLE_STAFF_ROLES = frozenset(
    {
        REVOPS_SPECIALIST,
        REVOPS_MANAGER,
        APPOINTMENT_SETTER,
        READ_ONLY,
    }
)
STAFF_EMAIL_SUFFIX = "@n-star.us"
EVENT_USER_CREATED = "user_created"

STAFF_ADMIN_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS staff_admin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    target_user_id INTEGER,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    CHECK (event_type IN (
        'user_created',
        'name_changed',
        'email_changed',
        'role_changed',
        'administrator_changed',
        'active_changed',
        'client_granted',
        'client_removed',
        'password_reset',
        'sessions_revoked'
    )),
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (target_user_id) REFERENCES users(id) ON DELETE SET NULL
)
"""

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


def _read_admin_user(conn, user_id: int) -> dict[str, object]:
    if int(user_id) <= 0:
        raise LookupError("User not found.")
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


def get_admin_user(user_id: int) -> dict[str, object]:
    with get_connection() as conn:
        return _read_admin_user(conn, user_id)


class StaffAdminError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class CreateStaffUserRequest(BaseModel):
    full_name: str = ""
    email: str = ""
    staff_role: str = ""
    client_ids: list[int] = Field(default_factory=list)
    active: bool = True
    password: str = ""


def ensure_staff_admin_events_schema(conn) -> None:
    """Create the audit table only. Does not insert events or edit users."""
    conn.execute(STAFF_ADMIN_EVENTS_DDL)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_admin_events_target
            ON staff_admin_events(target_user_id, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_admin_events_created
            ON staff_admin_events(created_at)
        """
    )


def _stamp(now: datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_name(value: object) -> str:
    return " ".join(str(value or "").split())


def _creation_role(value: object) -> str:
    role = normalize_role(value)
    if role in {SYSTEM_ADMINISTRATOR, OPERATIONS_ADMIN}:
        raise StaffAdminError("Administrator access cannot be granted when creating a user.")
    if role not in CREATABLE_STAFF_ROLES:
        raise StaffAdminError("Staff role is not valid for a new user.")
    return role


def _staff_email(value: object) -> str:
    try:
        email = canonicalize_email(str(value or "").strip())
    except StaffProvisionError as exc:
        raise StaffAdminError(str(exc)) from exc
    if not email.endswith(STAFF_EMAIL_SUFFIX):
        raise StaffAdminError("Email must use @n-star.us.")
    return email


def _client_ids(conn, raw_ids: list[int]) -> list[int]:
    chosen: list[int] = []
    seen: set[int] = set()
    for raw in raw_ids:
        try:
            client_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise StaffAdminError("A selected client does not exist.") from exc
        if client_id <= 0:
            raise StaffAdminError("A selected client does not exist.")
        if client_id in seen:
            continue
        seen.add(client_id)
        chosen.append(client_id)
    if not chosen:
        raise StaffAdminError("At least one client is required.")
    found = {
        int(row["id"])
        for row in conn.execute(
            f"SELECT id FROM clients WHERE id IN ({','.join('?' for _ in chosen)})",
            chosen,
        ).fetchall()
    }
    if found != set(chosen):
        raise StaffAdminError("A selected client does not exist.")
    return chosen


def _audit_detail(detail: dict[str, object]) -> str:
    for key in detail:
        lowered = str(key).lower()
        if "password" in lowered or "csrf" in lowered or "token" in lowered or "hash" in lowered:
            raise StaffAdminError("Audit detail cannot include a credential.")
    encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"))
    if "$argon2" in encoded.lower():
        raise StaffAdminError("Audit detail cannot include a credential.")
    return encoded


def _record_user_created(
    conn,
    *,
    actor_user_id: int,
    target_user_id: int,
    detail: dict[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO staff_admin_events (
            actor_user_id, target_user_id, event_type, created_at, detail_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(actor_user_id),
            int(target_user_id),
            EVENT_USER_CREATED,
            _stamp(),
            _audit_detail(detail),
        ),
    )


def _insert_assignments(conn, user_id: int, client_ids: list[int], role: str) -> None:
    for client_id in client_ids:
        conn.execute(
            """
            INSERT INTO user_client_assignments (
                user_id, client_id, role, active, assigned_at
            ) VALUES (?, ?, ?, 1, ?)
            """,
            (int(user_id), int(client_id), role, _stamp()),
        )


def create_staff_user(
    *,
    actor_user_id: int,
    full_name: str,
    email: str,
    staff_role: str,
    client_ids: list[int],
    password: str,
    active: bool = True,
) -> dict[str, object]:
    """Create one non-administrator and the user_created audit row together.

    The request cannot set is_administrator. CRM assigned_user_id is not written.
    locked_until stays the empty string: that column is NOT NULL, and login
    treats blank as not locked.
    """
    name = _clean_name(full_name)
    if not name:
        raise StaffAdminError("Full name is required.")
    email_norm = _staff_email(email)
    role = _creation_role(staff_role)
    try:
        digest = hash_password(password, email=email_norm)
    except ValueError as exc:
        raise StaffAdminError(str(exc)) from exc
    is_active = bool(active)

    conn = get_connection()
    try:
        ensure_staff_admin_events_schema(conn)
        conn.commit()
        chosen = _client_ids(conn, list(client_ids))
        existing = conn.execute(
            "SELECT id FROM users WHERE lower(email) = ?",
            (email_norm,),
        ).fetchone()
        if existing is not None:
            raise StaffAdminError(
                "An account with that email already exists.",
                status_code=409,
            )
        detail = {
            "full_name": name,
            "email": email_norm,
            "staff_role": role,
            "active": is_active,
            "client_ids": chosen,
            "is_administrator": False,
            "is_internal_northstar": True,
        }
        try:
            conn.execute(
                """
                INSERT INTO users (
                    email, full_name, is_administrator, is_internal_northstar, active,
                    password_hash, password_updated_at, failed_login_count, locked_until,
                    staff_role
                ) VALUES (?, ?, 0, 1, ?, ?, ?, 0, '', ?)
                """,
                (
                    email_norm,
                    name,
                    1 if is_active else 0,
                    digest,
                    _stamp(),
                    role,
                ),
            )
            user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            _insert_assignments(conn, user_id, chosen, role)
            _record_user_created(
                conn,
                actor_user_id=int(actor_user_id),
                target_user_id=user_id,
                detail=detail,
            )
            payload = _read_admin_user(conn, user_id)
            payload["has_password"] = True
            conn.commit()
            return payload
        except StaffAdminError:
            conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if "email" in str(exc).lower():
                raise StaffAdminError(
                    "An account with that email already exists.",
                    status_code=409,
                ) from exc
            raise
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
