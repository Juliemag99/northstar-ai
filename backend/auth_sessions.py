"""Staff session rows for upcoming cookie auth.

Phase 0 stores and looks up sessions only. No HTTP login or route protection.
The cookie will hold the raw token; this table stores SHA-256(token).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

IDLE_HOURS = 12
ABSOLUTE_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def ensure_staff_auth_schema(conn) -> None:
    """Add password columns and staff_sessions. Idempotent. No row rewrites."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone():
        existing = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        columns = (
            ("password_hash", "TEXT NOT NULL DEFAULT ''"),
            ("password_updated_at", "TEXT NOT NULL DEFAULT ''"),
            ("failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
            ("locked_until", "TEXT NOT NULL DEFAULT ''"),
        )
        for name, declaration in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            csrf_secret TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_sessions_user
            ON staff_sessions(user_id, revoked_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_sessions_expires
            ON staff_sessions(expires_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_sessions_token_hash
            ON staff_sessions(token_hash)
        """
    )


def create_staff_session(
    conn,
    *,
    user_id: int,
    ip: str = "",
    user_agent: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    stamp = now or _now()
    raw_token = secrets.token_urlsafe(32)
    csrf_secret = secrets.token_urlsafe(32)
    expires = stamp + timedelta(hours=ABSOLUTE_HOURS)
    conn.execute(
        """
        INSERT INTO staff_sessions (
            token_hash, user_id, csrf_secret, created_at, last_seen_at,
            expires_at, revoked_at, ip, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
        """,
        (
            hash_session_token(raw_token),
            int(user_id),
            csrf_secret,
            _iso(stamp),
            _iso(stamp),
            _iso(expires),
            ip or "",
            user_agent or "",
        ),
    )
    return {
        "token": raw_token,
        "csrf_secret": csrf_secret,
        "user_id": int(user_id),
        "created_at": _iso(stamp),
        "expires_at": _iso(expires),
    }


def lookup_staff_session(
    conn,
    raw_token: str,
    *,
    now: datetime | None = None,
    touch: bool = True,
) -> dict[str, Any] | None:
    token = (raw_token or "").strip()
    if not token:
        return None
    row = conn.execute(
        """
        SELECT id, token_hash, user_id, csrf_secret, created_at, last_seen_at,
               expires_at, revoked_at, ip, user_agent
        FROM staff_sessions
        WHERE token_hash = ?
        """,
        (hash_session_token(token),),
    ).fetchone()
    if row is None:
        return None
    if str(row["revoked_at"] or "").strip():
        return None
    stamp = now or _now()
    expires = _parse(row["expires_at"])
    created = _parse(row["created_at"])
    last_seen = _parse(row["last_seen_at"]) or created
    if expires is None or stamp >= expires:
        return None
    if created is not None and stamp - created >= timedelta(hours=ABSOLUTE_HOURS):
        return None
    if last_seen is not None and stamp - last_seen >= timedelta(hours=IDLE_HOURS):
        return None
    if touch:
        conn.execute(
            "UPDATE staff_sessions SET last_seen_at = ? WHERE id = ?",
            (_iso(stamp), int(row["id"])),
        )
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "csrf_secret": str(row["csrf_secret"]),
        "created_at": str(row["created_at"]),
        "last_seen_at": _iso(stamp) if touch else str(row["last_seen_at"]),
        "expires_at": str(row["expires_at"]),
    }


def revoke_staff_session(conn, raw_token: str, *, now: datetime | None = None) -> bool:
    token = (raw_token or "").strip()
    if not token:
        return False
    stamp = _iso(now or _now())
    cur = conn.execute(
        """
        UPDATE staff_sessions
        SET revoked_at = ?
        WHERE token_hash = ? AND TRIM(COALESCE(revoked_at, '')) = ''
        """,
        (stamp, hash_session_token(token)),
    )
    return int(cur.rowcount or 0) > 0


def revoke_staff_sessions_for_user(
    conn, user_id: int, *, now: datetime | None = None
) -> int:
    stamp = _iso(now or _now())
    cur = conn.execute(
        """
        UPDATE staff_sessions
        SET revoked_at = ?
        WHERE user_id = ? AND TRIM(COALESCE(revoked_at, '')) = ''
        """,
        (stamp, int(user_id)),
    )
    return int(cur.rowcount or 0)
