"""Permanent NorthStar staff roles and capability permissions.

Phase 6A catalog + helpers. Phase 6B wires capabilities into CRM via
resolve_staff_actor(), user_has_permission(), and user_client_assignments.

Live 6B applies users.staff_role only — not staff_feedback / staff_auth_events.
Do not call ensure_staff_rbac_schema() from db.py (that helper also creates
optional feedback tables). Live uses ensure_staff_role_schema / _COLUMN_MIGRATIONS.

Client access is separate from role:
- role grants capabilities
- user_client_assignments (active) grants which clients those capabilities apply to
- clients.all is an explicit capability (system administrator), not inferred
  from the word "admin" on an assignment row

Assigned CCR rep is workflow ownership, not security authorization.
"""

from __future__ import annotations

from typing import Any

from access import get_user_by_id, user_can_access_client
from db import get_connection
from models import NorthStarUser

SYSTEM_ADMINISTRATOR = "system_administrator"
OPERATIONS_ADMIN = "operations_admin"
REVOPS_MANAGER = "revops_manager"
REVOPS_SPECIALIST = "revops_specialist"
APPOINTMENT_SETTER = "appointment_setter"
READ_ONLY = "read_only"

VALID_ROLES: tuple[str, ...] = (
    SYSTEM_ADMINISTRATOR,
    OPERATIONS_ADMIN,
    REVOPS_MANAGER,
    REVOPS_SPECIALIST,
    APPOINTMENT_SETTER,
    READ_ONLY,
)

# Capabilities actually justified by current NorthStar surfaces.
PERMISSIONS: tuple[str, ...] = (
    "users.manage",
    "clients.manage",
    "clients.all",
    "client.view",
    "crm.edit",
    "notes.edit",
    "tasks.edit",
    "campaigns.view",
    "campaigns.manage",
    "reports.view",
    "imports.manage",
    "exports.master_data",
    "duplicates.review",
    "duplicates.approve",
    "duplicates.execute",
    "master_data.view",
    "master_data.edit",
    "master_data.archive",
    "master_data.restore",
    "client_relationships.edit",
    "admin.view",
    "system.manage",
    "client_setup.edit",
    "research.run",
    "feedback.submit",
)

CLIENT_SCOPED = frozenset(
    {
        "client.view",
        "crm.edit",
        "notes.edit",
        "tasks.edit",
        "campaigns.view",
        "campaigns.manage",
        "reports.view",
        "client_setup.edit",
        "research.run",
        "client_relationships.edit",
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    SYSTEM_ADMINISTRATOR: frozenset(PERMISSIONS),
    OPERATIONS_ADMIN: frozenset(
        {
            "clients.manage",
            "client.view",
            "crm.edit",
            "notes.edit",
            "tasks.edit",
            "campaigns.view",
            "campaigns.manage",
            "reports.view",
            "admin.view",
            "client_setup.edit",
            "research.run",
            "feedback.submit",
            "master_data.view",
            "master_data.edit",
            "master_data.archive",
            "master_data.restore",
            "client_relationships.edit",
            "duplicates.review",
            # imports.manage / duplicates.execute / users.manage / system.manage
            # require explicit additional grant — not default for Operations.
        }
    ),
    REVOPS_MANAGER: frozenset(
        {
            "client.view",
            "crm.edit",
            "notes.edit",
            "tasks.edit",
            "campaigns.view",
            "campaigns.manage",
            "reports.view",
            "research.run",
            "feedback.submit",
            "master_data.view",
            "client_relationships.edit",
        }
    ),
    REVOPS_SPECIALIST: frozenset(
        {
            "client.view",
            "crm.edit",
            "notes.edit",
            "tasks.edit",
            "campaigns.view",
            "reports.view",
            "research.run",
            "feedback.submit",
        }
    ),
    APPOINTMENT_SETTER: frozenset(
        {
            "client.view",
            "crm.edit",
            "notes.edit",
            "tasks.edit",
            "campaigns.view",
            "feedback.submit",
        }
    ),
    READ_ONLY: frozenset(
        {
            "client.view",
            "campaigns.view",
            "reports.view",
            "feedback.submit",
        }
    ),
}

_ROLE_ALIASES = {
    "administrator": SYSTEM_ADMINISTRATOR,
    "admin": SYSTEM_ADMINISTRATOR,
    "system_admin": SYSTEM_ADMINISTRATOR,
    "system administrator": SYSTEM_ADMINISTRATOR,
    "operations": OPERATIONS_ADMIN,
    "operations_admin": OPERATIONS_ADMIN,
    "ops": OPERATIONS_ADMIN,
    "management": OPERATIONS_ADMIN,
    "revops_manager": REVOPS_MANAGER,
    "rev_ops_manager": REVOPS_MANAGER,
    "revenue_ops_manager": REVOPS_MANAGER,
    "revops": REVOPS_SPECIALIST,
    "rev_ops": REVOPS_SPECIALIST,
    "revenue_ops": REVOPS_SPECIALIST,
    "revops_specialist": REVOPS_SPECIALIST,
    "account_executive": REVOPS_SPECIALIST,
    "appointment_setter": APPOINTMENT_SETTER,
    "setter": APPOINTMENT_SETTER,
    "read_only": READ_ONLY,
    "readonly": READ_ONLY,
    "viewer": READ_ONLY,
}

PROPOSED_USERS_STAFF_ROLE_DDL = (
    "ALTER TABLE users ADD COLUMN staff_role TEXT NOT NULL DEFAULT ''"
)

PROPOSED_STAFF_FEEDBACK_DDL = """
CREATE TABLE IF NOT EXISTS staff_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    created_at TEXT NOT NULL,
    page_route TEXT NOT NULL DEFAULT '',
    client_id INTEGER,
    category TEXT NOT NULL DEFAULT 'Other',
    body TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'New',
    company_id INTEGER,
    contact_id INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
)
"""

PROPOSED_STAFF_AUTH_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS staff_auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
)
"""


def normalize_role(value: object) -> str:
    text = "" if value is None else str(value).strip().lower().replace("-", "_")
    text = "_".join(text.split())
    if text in VALID_ROLES:
        return text
    return _ROLE_ALIASES.get(text, "")


def permissions_for_role(role: str) -> frozenset[str]:
    key = normalize_role(role)
    if not key:
        return frozenset()
    return ROLE_PERMISSIONS.get(key, frozenset())


def _staff_role_column(conn) -> bool:
    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    return "staff_role" in cols


def ensure_staff_role_schema(conn) -> dict[str, bool]:
    """Add users.staff_role only. Does not create feedback/auth-event tables."""
    created_role = False
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone():
        if not _staff_role_column(conn):
            conn.execute(PROPOSED_USERS_STAFF_ROLE_DDL)
            created_role = True
    return {"users_staff_role": created_role}


def ensure_staff_rbac_schema(conn) -> dict[str, bool]:
    """Isolated helper including optional feedback tables. Live 6B uses staff_role only."""
    created = ensure_staff_role_schema(conn)
    conn.execute(PROPOSED_STAFF_FEEDBACK_DDL)
    conn.execute(PROPOSED_STAFF_AUTH_EVENTS_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_feedback_created ON staff_feedback(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_auth_events_created ON staff_auth_events(created_at)"
    )
    return {
        "users_staff_role": created["users_staff_role"],
        "staff_feedback": True,
        "staff_auth_events": True,
    }


def load_user_staff_role(user_id: int, conn=None) -> str:
    """Canonical role for a user. Empty staff_role falls back to is_administrator."""
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if row is None:
            return ""
        if _staff_role_column(conn):
            stored = normalize_role(row["staff_role"] if "staff_role" in row.keys() else "")
            if stored:
                return stored
        if bool(row["is_administrator"]):
            return SYSTEM_ADMINISTRATOR
        return ""
    finally:
        if owns:
            conn.close()


def set_user_staff_role(conn, user_id: int, role: str) -> str:
    canonical = normalize_role(role)
    if role and not canonical:
        raise ValueError(f"Unknown staff role: {role}")
    ensure_staff_role_schema(conn)
    conn.execute(
        "UPDATE users SET staff_role = ? WHERE id = ?",
        (canonical, int(user_id)),
    )
    return canonical


def user_has_permission(
    user_id: int,
    permission: str,
    *,
    client_id: int | None = None,
    conn=None,
) -> bool:
    """True when the user's role grants the capability and client scope allows it."""
    perm = str(permission or "").strip()
    if perm not in PERMISSIONS:
        return False
    user = get_user_by_id(int(user_id))
    if user is None or not user.active:
        return False
    role = load_user_staff_role(int(user_id), conn=conn)
    granted = permissions_for_role(role)
    if perm not in granted:
        return False
    if perm == "clients.all":
        return True
    if perm not in CLIENT_SCOPED:
        return True
    if client_id is None:
        return True
    if perm in granted and "clients.all" in granted:
        return user_can_access_client(int(user_id), int(client_id))
    return user_can_access_client(int(user_id), int(client_id))


def authorized_client_ids(user_id: int) -> list[int]:
    """Client ids the user may access. Administrators today see all clients.

    Proposed 6B: only users with clients.all (or explicit assignments) see all.
    This helper currently matches live access.py so callers do not silently
    narrow Julie's existing access.
    """
    from access import list_clients_for_user

    return [a.client_id for a in list_clients_for_user(int(user_id), active_only=True)]


def public_role_payload(user: NorthStarUser) -> dict[str, Any]:
    role = load_user_staff_role(int(user.id))
    return {
        "staff_role": role,
        "permissions": sorted(permissions_for_role(role)),
        "is_administrator": bool(user.is_administrator),
    }
