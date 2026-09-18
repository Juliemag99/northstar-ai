"""Controlled staff-user provisioning for NorthStar RevOps.

Creates a normal staff account with hashed password, staff_role, and
explicit user_client_assignments. This is not the first-admin bootstrap
and it cannot elevate to administrator.

Never logs, prints, or returns plaintext passwords or password hashes.
Live northstar.db writes require an env flag plus explicit confirmation.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth_passwords import hash_password, password_issue
from auth_sessions import ensure_staff_auth_schema
from db import PRODUCTION_DB_PATH, get_connection
from staff_rbac import (
    REVOPS_SPECIALIST,
    SYSTEM_ADMINISTRATOR,
    VALID_ROLES,
    ensure_staff_role_schema,
    normalize_role,
    permissions_for_role,
)

ALLOW_FLAG = "NORTHSTAR_ALLOW_STAFF_PROVISION"
PASSWORD_ENV = "NORTHSTAR_STAFF_PROVISION_PASSWORD"

# Roles this path may assign. system_administrator and operations_admin are
# refused so the RevOps provisioner cannot grant admin-wide capabilities.
ALLOWED_PROVISION_ROLES: frozenset[str] = frozenset(
    {
        REVOPS_SPECIALIST,
        "revops_manager",
        "appointment_setter",
        "read_only",
    }
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]*$")


class StaffProvisionError(ValueError):
    """Fail-closed provisioning error. Safe to show to an administrator."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def canonicalize_email(value: object) -> str:
    email = _blank(value).lower()
    if not email or not _EMAIL_RE.match(email):
        raise StaffProvisionError("A valid email address is required.")
    return email


def canonicalize_name(value: object, *, field: str) -> str:
    text = " ".join(_blank(value).split())
    if not text:
        raise StaffProvisionError(f"{field} is required.")
    if not _NAME_RE.match(text):
        raise StaffProvisionError(f"{field} contains unsupported characters.")
    return text


def connection_file_path(conn) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    file_name = ""
    if row is not None:
        try:
            file_name = str(row["file"] or "")
        except (KeyError, IndexError, TypeError):
            file_name = str(row[2] if len(row) > 2 else "")
    if not file_name:
        return Path(":memory:")
    return Path(file_name).resolve()


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
            os.path.realpath(right)
        )
    except OSError:
        return False


def is_live_database(path: Path) -> bool:
    if str(path) in {":memory:", ""}:
        return False
    return _same_file(path, Path(PRODUCTION_DB_PATH))


def database_kind(path: Path) -> str:
    if is_live_database(path):
        return "LIVE production database"
    return "isolated test database (not live northstar.db)"


def refuse_live_write(
    conn,
    *,
    confirm_live: bool | None,
    action: str,
    allow_flag: str = ALLOW_FLAG,
) -> Path:
    """Refuse silent writes to live northstar.db."""
    path = connection_file_path(conn)
    if not is_live_database(path):
        return path
    if os.environ.get(allow_flag, "").strip() != "1":
        raise StaffProvisionError(
            f"Refusing to {action} on the live database. Set {allow_flag}=1."
        )
    if confirm_live is True:
        return path
    raise StaffProvisionError(
        f"Refusing to {action} on the live database without explicit confirmation."
    )


def _require_clients(conn, client_ids: list[int]) -> list[dict[str, Any]]:
    if not client_ids:
        raise StaffProvisionError("At least one client assignment is required.")
    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
    for raw in client_ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError) as exc:
            raise StaffProvisionError("client_id must be a positive integer.") from exc
        if cid <= 0:
            raise StaffProvisionError("client_id must be a positive integer.")
        if cid in seen:
            continue
        seen.add(cid)
        row = conn.execute(
            "SELECT id, code, name FROM clients WHERE id = ?",
            (cid,),
        ).fetchone()
        if row is None:
            raise StaffProvisionError(f"client_id {cid} does not exist.")
        rows.append(
            {
                "client_id": int(row["id"]),
                "code": str(row["code"] or ""),
                "name": str(row["name"] or ""),
            }
        )
    return rows


def _public_user_row(row) -> dict[str, Any]:
    return {
        "user_id": int(row["id"]),
        "email": str(row["email"] or ""),
        "full_name": str(row["full_name"] or ""),
        "staff_role": str(row["staff_role"] or "") if "staff_role" in row.keys() else "",
        "active": bool(row["active"]),
        "is_administrator": bool(row["is_administrator"]),
        "is_internal_northstar": bool(row["is_internal_northstar"]),
        "has_password": bool(str(row["password_hash"] or "").strip()),
    }


def provision_staff_user(
    *,
    first_name: str,
    last_name: str,
    email: str,
    password: str,
    staff_role: str = REVOPS_SPECIALIST,
    client_ids: list[int],
    active: bool = True,
    is_internal_northstar: bool = True,
    exist_ok: bool = False,
    reset_password: bool = False,
    conn=None,
    confirm_live: bool | None = None,
) -> dict[str, Any]:
    """Create or safely reuse a non-administrator staff user.

    Password is hashed immediately and never returned. Duplicate email fails
    closed unless exist_ok and the existing row matches this identity.
    Administrator elevation is always refused on this path.
    """
    first = canonicalize_name(first_name, field="First name")
    last = canonicalize_name(last_name, field="Last name")
    full_name = f"{first} {last}"
    email_norm = canonicalize_email(email)
    role = normalize_role(staff_role) or _blank(staff_role).lower()
    if role not in VALID_ROLES:
        raise StaffProvisionError("Invalid staff role.")
    if role == SYSTEM_ADMINISTRATOR or role not in ALLOWED_PROVISION_ROLES:
        raise StaffProvisionError(
            "The RevOps provisioning path cannot grant administrator access."
        )
    if "clients.all" in permissions_for_role(role) or "users.manage" in permissions_for_role(role):
        raise StaffProvisionError(
            "The RevOps provisioning path cannot grant administrator access."
        )
    if reset_password and not exist_ok:
        raise StaffProvisionError("Password reset requires exist_ok on an existing account.")
    if not exist_ok or reset_password:
        issue = password_issue(password, email=email_norm)
        if issue:
            raise StaffProvisionError(issue)

    owns = conn is None
    if owns:
        conn = get_connection()
    created = False
    already_existed = False
    password_set = False
    try:
        target = refuse_live_write(
            conn,
            confirm_live=confirm_live,
            action="provision a staff user",
        )
        ensure_staff_auth_schema(conn)
        ensure_staff_role_schema(conn)
        clients = _require_clients(conn, client_ids)

        existing = conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)",
            (email_norm,),
        ).fetchone()
        if existing is not None:
            already_existed = True
            if not exist_ok:
                raise StaffProvisionError("An account with that email already exists.")
            stored_name = _blank(existing["full_name"])
            stored_role = ""
            if "staff_role" in existing.keys():
                stored_role = normalize_role(existing["staff_role"])
            if stored_name and stored_name.lower() != full_name.lower():
                raise StaffProvisionError(
                    "An account with that email already exists with a different name."
                )
            if stored_role and stored_role != role:
                raise StaffProvisionError(
                    "An account with that email already exists with a different role."
                )
            if bool(existing["is_administrator"]):
                raise StaffProvisionError(
                    "The RevOps provisioning path cannot modify an administrator account."
                )
            user_id = int(existing["id"])
            if reset_password:
                digest = hash_password(password, email=email_norm)
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, password_updated_at = ?,
                        failed_login_count = 0, locked_until = '', active = ?
                    WHERE id = ?
                    """,
                    (digest, _now(), 1 if active else 0, user_id),
                )
                password_set = True
        else:
            digest = hash_password(password, email=email_norm)
            conn.execute(
                """
                INSERT INTO users (
                    email, full_name, is_administrator, is_internal_northstar, active,
                    password_hash, password_updated_at, failed_login_count, locked_until,
                    staff_role
                ) VALUES (?, ?, 0, ?, ?, ?, ?, 0, '', ?)
                """,
                (
                    email_norm,
                    full_name,
                    1 if is_internal_northstar else 0,
                    1 if active else 0,
                    digest,
                    _now(),
                    role,
                ),
            )
            user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            created = True
            password_set = True

        assigned: list[dict[str, Any]] = []
        for client in clients:
            row = conn.execute(
                """
                SELECT id, active FROM user_client_assignments
                WHERE user_id = ? AND client_id = ?
                """,
                (user_id, client["client_id"]),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO user_client_assignments
                        (user_id, client_id, role, active, assigned_at)
                    VALUES (?, ?, ?, 1, datetime('now'))
                    """,
                    (user_id, client["client_id"], role),
                )
            elif not bool(row["active"]):
                conn.execute(
                    """
                    UPDATE user_client_assignments
                    SET active = 1, role = ?, assigned_at = datetime('now')
                    WHERE id = ?
                    """,
                    (role, int(row["id"])),
                )
            assigned.append(client)

        if owns:
            conn.commit()
        loaded = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        public = _public_user_row(loaded)
        public["staff_role"] = role
        return {
            "ok": True,
            "created": created,
            "already_existed": already_existed,
            "password_set": password_set,
            "user": public,
            "client_assignments": assigned,
            "permissions": sorted(permissions_for_role(role)),
            "database_path": str(target),
            "database_kind": database_kind(target),
        }
    finally:
        if owns:
            conn.close()
