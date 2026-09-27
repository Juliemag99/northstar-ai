"""Administrator view of staff users, plus audited create and edit.

Create and edit do not assign CRM records. assigned_user_id remains workflow
ownership, not client authorization.

A usable administrator can sign in: is_administrator, active, and a non-empty
password_hash. Temporary lockout does not remove that status, because lockout
expires. An active administrator with no password is not a usable fallback.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth_http import _is_locked
from auth_passwords import hash_password
from auth_sessions import revoke_staff_sessions_for_user
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
EVENT_NAME_CHANGED = "name_changed"
EVENT_EMAIL_CHANGED = "email_changed"
EVENT_ROLE_CHANGED = "role_changed"
EVENT_ADMINISTRATOR_CHANGED = "administrator_changed"
EVENT_ACTIVE_CHANGED = "active_changed"
EVENT_CLIENT_GRANTED = "client_granted"
EVENT_CLIENT_REMOVED = "client_removed"
EVENT_SESSIONS_REVOKED = "sessions_revoked"
EVENT_PASSWORD_RESET = "password_reset"

LAST_USABLE_ADMIN_MESSAGE = "This would leave no administrator who can sign in."
SELF_DEACTIVATE_MESSAGE = "You cannot deactivate your own account."
SELF_DEMOTE_MESSAGE = "You cannot remove your own administrator access."

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


def _strict_bool(value: object) -> object:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError("Active and administrator must be true or false.")
    return value


class UpdateStaffUserRequest(BaseModel):
    """Account fields only. Password, clients, and CRM assignment are rejected."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    email: str | None = None
    staff_role: str | None = None
    is_administrator: bool | None = None
    active: bool | None = None

    @field_validator("is_administrator", "active", mode="before")
    @classmethod
    def _booleans(cls, value: object) -> object:
        return _strict_bool(value)


class ReplaceUserClientsRequest(BaseModel):
    """Complete desired set of active client assignments."""

    model_config = ConfigDict(extra="forbid")

    client_ids: list[int] = Field(default_factory=list)


class ResetStaffPasswordRequest(BaseModel):
    """Write-only replacement password. Extra fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    password: str

    @field_validator("password", mode="before")
    @classmethod
    def _password_text(cls, value: object) -> object:
        if type(value) is not str:
            raise ValueError("Password is required.")
        return value


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


def _client_ids(conn, raw_ids: list[int], *, allow_empty: bool = False) -> list[int]:
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
        if allow_empty:
            return []
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


_SAFE_DETAIL_KEYS = frozenset(
    {
        "old",
        "new",
        "full_name",
        "email",
        "staff_role",
        "active",
        "is_administrator",
        "is_internal_northstar",
        "client_id",
        "client_ids",
        "client_code",
        "client_name",
        "clients",
        "sessions_revoked",
        "password_updated_at",
    }
)
_SECRET_KEY_MARKERS = (
    "password_hash",
    "token_hash",
    "csrf_secret",
    "credential",
    "cookie",
    "secret",
    "csrf",
    "token",
    "password",
    "hash",
)


def _secret_detail_key(key: object) -> bool:
    lowered = str(key).strip().lower().replace("-", "_")
    if lowered == "password_updated_at":
        return False
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def _secret_detail_text(value: str) -> bool:
    lowered = value.lower()
    return "$argon2" in lowered or "password_hash" in lowered


def _sanitize_detail_value(value: object, depth: int) -> object:
    if depth > 6:
        return None
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        if _secret_detail_text(value):
            return None
        return value
    if isinstance(value, list):
        cleaned = []
        for item in value:
            safe = _sanitize_detail_value(item, depth + 1)
            if safe is not None:
                cleaned.append(safe)
        return cleaned
    if isinstance(value, dict):
        return _sanitize_detail_mapping(value, depth + 1)
    return None


def _sanitize_detail_mapping(value: dict, depth: int) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, item in value.items():
        name = str(key)
        if name not in _SAFE_DETAIL_KEYS or _secret_detail_key(name):
            continue
        safe = _sanitize_detail_value(item, depth)
        if safe is None or safe == {} or safe == []:
            continue
        cleaned[name] = safe
    return cleaned


def sanitize_staff_admin_detail(raw: object) -> tuple[dict[str, object], bool]:
    """Return a safe detail object and whether the stored JSON could be read.

    Malformed text becomes an empty detail. The original text is not returned.
    Secret-bearing keys are dropped at every nesting level.
    """
    if isinstance(raw, dict):
        return _sanitize_detail_mapping(raw, 0), True
    if not isinstance(raw, str):
        return {}, False
    text = raw.strip()
    if not text:
        return {}, True
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}, False
    if not isinstance(parsed, dict):
        return {}, False
    return _sanitize_detail_mapping(parsed, 0), True


def _audit_detail(detail: dict[str, object]) -> str:
    for key in detail:
        lowered = str(key).lower()
        if lowered == "password_updated_at":
            continue
        if "password" in lowered or "csrf" in lowered or "token" in lowered or "hash" in lowered:
            raise StaffAdminError("Audit detail cannot include a credential.")
    encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"))
    if "$argon2" in encoded.lower():
        raise StaffAdminError("Audit detail cannot include a credential.")
    return encoded


def _record_admin_event(
    conn,
    *,
    actor_user_id: int,
    target_user_id: int,
    event_type: str,
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
            event_type,
            _stamp(),
            _audit_detail(detail),
        ),
    )


def _record_user_created(
    conn,
    *,
    actor_user_id: int,
    target_user_id: int,
    detail: dict[str, object],
) -> None:
    _record_admin_event(
        conn,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        event_type=EVENT_USER_CREATED,
        detail=detail,
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


def is_usable_administrator(row) -> bool:
    """True when this row can sign in as an administrator.

    Requires is_administrator, active, and a non-empty password_hash.
    locked_until is ignored: a temporary lockout expires and must not make
    the last usable administrator look unprotected. A no-password
    administrator is not usable.
    """
    return (
        bool(row["is_administrator"])
        and bool(row["active"])
        and bool(_text(row, "password_hash").strip())
    )


def _count_other_usable_administrators(conn, user_id: int) -> int:
    rows = conn.execute(
        "SELECT id, is_administrator, active, password_hash FROM users"
    ).fetchall()
    return sum(
        1
        for row in rows
        if int(row["id"]) != int(user_id) and is_usable_administrator(row)
    )


def _non_admin_role(value: object) -> str:
    role = normalize_role(value)
    if role not in CREATABLE_STAFF_ROLES:
        raise StaffAdminError("Staff role is not valid.")
    return role


def _next_account(row, changes: dict[str, object]) -> dict[str, object]:
    """Resolve the account fields that should be stored after this edit."""
    current_admin = bool(row["is_administrator"])
    current_active = bool(row["active"])
    stored_role = _text(row, "staff_role")
    current_role = normalize_role(stored_role)
    stored_name = _text(row, "full_name")
    stored_email = _text(row, "email").strip()
    current_email = stored_email.lower()

    if "full_name" in changes:
        name = _clean_name(changes["full_name"])
        if not name:
            raise StaffAdminError("Full name is required.")
    else:
        name = stored_name

    if "email" in changes:
        candidate = str(changes["email"] or "").strip().lower()
        if candidate == current_email:
            email = stored_email
        else:
            email = _staff_email(changes["email"])
    else:
        email = stored_email

    if "active" in changes:
        if changes["active"] is None:
            raise StaffAdminError("Active must be true or false.")
        active = bool(changes["active"])
    else:
        active = current_active

    if "is_administrator" in changes:
        if changes["is_administrator"] is None:
            raise StaffAdminError("Administrator must be true or false.")
        administrator = bool(changes["is_administrator"])
    else:
        administrator = current_admin

    if administrator:
        if not current_admin:
            role = SYSTEM_ADMINISTRATOR
        elif "staff_role" in changes:
            requested = normalize_role(changes["staff_role"])
            if requested and requested != SYSTEM_ADMINISTRATOR:
                raise StaffAdminError(
                    "An administrator's role remains System administrator. "
                    "Turn administrator access off to assign another role."
                )
            role = SYSTEM_ADMINISTRATOR
        else:
            role = current_role
    else:
        demoting = current_admin and not administrator
        if demoting and "staff_role" not in changes:
            raise StaffAdminError(
                "Choose a non-administrator role when turning administrator access off."
            )
        if "staff_role" in changes:
            role = _non_admin_role(changes["staff_role"])
        elif demoting:
            raise StaffAdminError(
                "Choose a non-administrator role when turning administrator access off."
            )
        else:
            role = current_role

    role_changed = role != current_role
    if not role_changed:
        role = stored_role
    email_changed = email.lower() != current_email
    if not email_changed:
        email = stored_email

    return {
        "full_name": name,
        "email": email,
        "staff_role": role,
        "is_administrator": administrator,
        "active": active,
        "name_changed": name != stored_name,
        "email_changed": email_changed,
        "role_changed": role_changed,
        "administrator_changed": administrator != current_admin,
        "active_changed": active != current_active,
        "previous_name": stored_name,
        "previous_email": stored_email,
        "previous_role": current_role,
        "previous_administrator": current_admin,
        "previous_active": current_active,
    }


def update_staff_user(
    *,
    actor_user_id: int,
    user_id: int,
    changes: dict[str, object],
) -> dict[str, object]:
    """Apply one account edit atomically.

    Does not change passwords, client assignments except the stored role on
    active rows, or any CRM assigned_user_id. Reactivation does not invent a
    password and does not clear lockout.

    If the actor changes their own email, this revokes their current session
    along with the target's other sessions. The response still returns
    sessions_revoked. The caller must sign in again; the session is not kept
    alive to preserve the screen.
    """
    if int(user_id) <= 0:
        raise LookupError("User not found.")

    conn = get_connection()
    try:
        ensure_staff_admin_events_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if row is None:
            raise LookupError("User not found.")
        account = _next_account(row, changes)
        if account["email_changed"]:
            taken = conn.execute(
                "SELECT id FROM users WHERE lower(email) = ? AND id != ?",
                (account["email"], int(user_id)),
            ).fetchone()
            if taken is not None:
                raise StaffAdminError(
                    "An account with that email already exists.",
                    status_code=409,
                )

        if int(actor_user_id) == int(user_id):
            if account["previous_active"] and not account["active"]:
                raise StaffAdminError(SELF_DEACTIVATE_MESSAGE, status_code=409)
            if account["previous_administrator"] and not account["is_administrator"]:
                raise StaffAdminError(SELF_DEMOTE_MESSAGE, status_code=409)

        next_usable = is_usable_administrator(
            {
                "is_administrator": account["is_administrator"],
                "active": account["active"],
                "password_hash": _text(row, "password_hash"),
            }
        )
        if _count_other_usable_administrators(conn, int(user_id)) + (1 if next_usable else 0) == 0:
            raise StaffAdminError(LAST_USABLE_ADMIN_MESSAGE, status_code=409)

        changed = any(
            account[flag]
            for flag in (
                "name_changed",
                "email_changed",
                "role_changed",
                "administrator_changed",
                "active_changed",
            )
        )
        sessions_revoked = 0
        try:
            if changed:
                conn.execute(
                    """
                    UPDATE users
                    SET full_name = ?, email = ?, staff_role = ?,
                        is_administrator = ?, active = ?
                    WHERE id = ?
                    """,
                    (
                        account["full_name"],
                        account["email"],
                        account["staff_role"],
                        1 if account["is_administrator"] else 0,
                        1 if account["active"] else 0,
                        int(user_id),
                    ),
                )
                if account["role_changed"]:
                    conn.execute(
                        """
                        UPDATE user_client_assignments
                        SET role = ?
                        WHERE user_id = ? AND active = 1
                        """,
                        (account["staff_role"], int(user_id)),
                    )
                audits = (
                    (
                        account["name_changed"],
                        EVENT_NAME_CHANGED,
                        {"old": account["previous_name"], "new": account["full_name"]},
                    ),
                    (
                        account["email_changed"],
                        EVENT_EMAIL_CHANGED,
                        {"old": account["previous_email"], "new": account["email"]},
                    ),
                    (
                        account["role_changed"],
                        EVENT_ROLE_CHANGED,
                        {"old": account["previous_role"], "new": account["staff_role"]},
                    ),
                    (
                        account["administrator_changed"],
                        EVENT_ADMINISTRATOR_CHANGED,
                        {
                            "old": account["previous_administrator"],
                            "new": account["is_administrator"],
                        },
                    ),
                    (
                        account["active_changed"],
                        EVENT_ACTIVE_CHANGED,
                        {"old": account["previous_active"], "new": account["active"]},
                    ),
                )
                for happened, event_type, detail in audits:
                    if happened:
                        _record_admin_event(
                            conn,
                            actor_user_id=int(actor_user_id),
                            target_user_id=int(user_id),
                            event_type=event_type,
                            detail=detail,
                        )
                revoke = (
                    account["email_changed"]
                    or account["role_changed"]
                    or account["administrator_changed"]
                    or (account["previous_active"] and not account["active"])
                )
                if revoke:
                    sessions_revoked = int(
                        revoke_staff_sessions_for_user(conn, int(user_id)) or 0
                    )
                    if sessions_revoked:
                        _record_admin_event(
                            conn,
                            actor_user_id=int(actor_user_id),
                            target_user_id=int(user_id),
                            event_type=EVENT_SESSIONS_REVOKED,
                            detail={"sessions_revoked": sessions_revoked},
                        )
            payload = _read_admin_user(conn, int(user_id))
            payload["sessions_revoked"] = sessions_revoked
            conn.commit()
            return payload
        except StaffAdminError:
            conn.rollback()
            raise
        except LookupError:
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


def replace_user_clients(
    *,
    actor_user_id: int,
    user_id: int,
    client_ids: list[int],
) -> dict[str, object]:
    """Replace the active client-assignment set without deleting rows.

    Administrators may have an empty explicit set; their effective access
    stays all clients. A non-administrator must keep at least one active
    client. Sessions are not revoked. CRM assigned_user_id is not written.
    """
    if int(user_id) <= 0:
        raise LookupError("User not found.")

    conn = get_connection()
    try:
        ensure_staff_admin_events_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if row is None:
            raise LookupError("User not found.")
        administrator = bool(row["is_administrator"])
        chosen = _client_ids(conn, list(client_ids), allow_empty=administrator)
        if not administrator and not chosen:
            raise StaffAdminError("At least one client is required.")
        role = normalize_role(row["staff_role"] if "staff_role" in row.keys() else "")
        if not role:
            role = _canonical_role(row, has_staff_role="staff_role" in row.keys())
        try:
            existing = {
                int(item["client_id"]): item
                for item in conn.execute(
                    """
                    SELECT id, client_id, active
                    FROM user_client_assignments
                    WHERE user_id = ?
                    """,
                    (int(user_id),),
                ).fetchall()
            }
            desired = set(chosen)
            for client_id in chosen:
                current = existing.get(client_id)
                if current is None:
                    conn.execute(
                        """
                        INSERT INTO user_client_assignments (
                            user_id, client_id, role, active, assigned_at
                        ) VALUES (?, ?, ?, 1, ?)
                        """,
                        (int(user_id), client_id, role, _stamp()),
                    )
                    _record_admin_event(
                        conn,
                        actor_user_id=int(actor_user_id),
                        target_user_id=int(user_id),
                        event_type=EVENT_CLIENT_GRANTED,
                        detail={"client_id": client_id},
                    )
                elif not bool(current["active"]):
                    conn.execute(
                        """
                        UPDATE user_client_assignments
                        SET active = 1, role = ?
                        WHERE id = ?
                        """,
                        (role, int(current["id"])),
                    )
                    _record_admin_event(
                        conn,
                        actor_user_id=int(actor_user_id),
                        target_user_id=int(user_id),
                        event_type=EVENT_CLIENT_GRANTED,
                        detail={"client_id": client_id},
                    )
                else:
                    conn.execute(
                        """
                        UPDATE user_client_assignments
                        SET role = ?
                        WHERE id = ?
                        """,
                        (role, int(current["id"])),
                    )
            for client_id, current in existing.items():
                if client_id in desired or not bool(current["active"]):
                    continue
                conn.execute(
                    "UPDATE user_client_assignments SET active = 0 WHERE id = ?",
                    (int(current["id"]),),
                )
                _record_admin_event(
                    conn,
                    actor_user_id=int(actor_user_id),
                    target_user_id=int(user_id),
                    event_type=EVENT_CLIENT_REMOVED,
                    detail={"client_id": client_id},
                )
            payload = _read_admin_user(conn, int(user_id))
            conn.commit()
            return payload
        except StaffAdminError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def reset_staff_password(
    *,
    actor_user_id: int,
    user_id: int,
    password: str,
) -> dict[str, object]:
    """Replace one password and revoke that user's sessions together.

    Does not activate the account, change role or administrator status, or
    touch client access or CRM assigned_user_id. locked_until is cleared with
    the empty string, which is how the current NOT NULL column means unlocked.
    A weak password is rejected before any write. If the actor resets their
    own password, this revokes the current session and does not recreate it.
    """
    if int(user_id) <= 0:
        raise LookupError("User not found.")
    if type(password) is not str:
        raise StaffAdminError("Password is required.")

    conn = get_connection()
    try:
        ensure_staff_admin_events_schema(conn)
        conn.commit()
        row = conn.execute(
            "SELECT id, email, active FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if row is None:
            raise LookupError("User not found.")
        email = _text(row, "email")
        conn.commit()
        try:
            digest = hash_password(password, email=email)
        except ValueError as exc:
            raise StaffAdminError(str(exc)) from exc
        updated_at = _stamp()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT id, active FROM users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
            if current is None:
                raise LookupError("User not found.")
            was_active = bool(current["active"])
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    password_updated_at = ?,
                    failed_login_count = 0,
                    locked_until = ''
                WHERE id = ?
                """,
                (digest, updated_at, int(user_id)),
            )
            _record_admin_event(
                conn,
                actor_user_id=int(actor_user_id),
                target_user_id=int(user_id),
                event_type=EVENT_PASSWORD_RESET,
                detail={
                    "password_updated_at": updated_at,
                    "active": was_active,
                },
            )
            sessions_revoked = int(revoke_staff_sessions_for_user(conn, int(user_id)) or 0)
            if sessions_revoked:
                _record_admin_event(
                    conn,
                    actor_user_id=int(actor_user_id),
                    target_user_id=int(user_id),
                    event_type=EVENT_SESSIONS_REVOKED,
                    detail={"sessions_revoked": sessions_revoked},
                )
            conn.commit()
            return {
                "user_id": int(user_id),
                "has_password": True,
                "password_updated_at": updated_at,
                "sessions_revoked": sessions_revoked,
            }
        except StaffAdminError:
            conn.rollback()
            raise
        except LookupError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _detail_client_ids(detail: dict[str, object]) -> list[int]:
    found: list[int] = []
    raw_one = detail.get("client_id")
    raw_many = detail.get("client_ids")
    values: list[object] = []
    if isinstance(raw_many, list):
        values.extend(raw_many)
    if raw_one is not None:
        values.append(raw_one)
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        if value not in found:
            found.append(value)
    return found


def _with_client_names(conn, detail: dict[str, object]) -> dict[str, object]:
    client_ids = _detail_client_ids(detail)
    if not client_ids or not _table_exists(conn, "clients"):
        return detail
    rows = conn.execute(
        f"SELECT id, code, name FROM clients WHERE id IN ({','.join('?' for _ in client_ids)})",
        client_ids,
    ).fetchall()
    by_id = {
        int(row["id"]): {
            "client_id": int(row["id"]),
            "client_code": _text(row, "code"),
            "client_name": _text(row, "name"),
        }
        for row in rows
    }
    clients = [by_id[client_id] for client_id in client_ids if client_id in by_id]
    if not clients:
        return detail
    enriched = dict(detail)
    enriched["clients"] = clients
    single = detail.get("client_id")
    if isinstance(single, int) and not isinstance(single, bool) and single in by_id:
        enriched["client_code"] = by_id[single]["client_code"]
        enriched["client_name"] = by_id[single]["client_name"]
    return enriched


def list_admin_user_history(user_id: int) -> dict[str, object]:
    """Read administration events for one user. Does not write."""
    if int(user_id) <= 0:
        raise LookupError("User not found.")
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if row is None:
            raise LookupError("User not found.")
        if not _table_exists(conn, "staff_admin_events"):
            return {"user_id": int(user_id), "events": []}
        events = conn.execute(
            """
            SELECT
                e.id,
                e.event_type,
                e.created_at,
                e.actor_user_id,
                e.target_user_id,
                e.detail_json,
                actor.full_name AS actor_full_name,
                actor.email AS actor_email
            FROM staff_admin_events e
            LEFT JOIN users actor ON actor.id = e.actor_user_id
            WHERE e.target_user_id = ?
            ORDER BY e.created_at DESC, e.id DESC
            """,
            (int(user_id),),
        ).fetchall()
        payload: list[dict[str, object]] = []
        for event in events:
            detail, available = sanitize_staff_admin_detail(_text(event, "detail_json"))
            if available:
                detail = _with_client_names(conn, detail)
            actor_id = event["actor_user_id"]
            payload.append(
                {
                    "id": int(event["id"]),
                    "event_type": _text(event, "event_type"),
                    "created_at": _text(event, "created_at"),
                    "actor_user_id": None if actor_id is None else int(actor_id),
                    "actor_full_name": _text(event, "actor_full_name"),
                    "actor_email": _text(event, "actor_email"),
                    "target_user_id": int(user_id),
                    "detail_available": available,
                    "detail": detail,
                }
            )
        return {"user_id": int(user_id), "events": payload}
