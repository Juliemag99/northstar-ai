"""One-time merge execution approvals (Phase 5J).

Pending rows authorize exactly one source/survivor/fingerprint.
Isolated test copies may execute without an approval. Live northstar.db may not.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db import PRODUCTION_DB_PATH

APPROVAL_STATUSES = (
    "pending",
    "executing",
    "executed",
    "failed",
    "expired",
    "superseded",
)
OPEN_STATUSES = ("pending", "executing")
DEFAULT_TTL_HOURS = 24


class MergeApprovalError(ValueError):
    """Approval is missing, mismatched, expired, or not executable."""


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def connection_is_production(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA database_list").fetchall():
        name = row["name"] if hasattr(row, "keys") else row[1]
        file_name = row["file"] if hasattr(row, "keys") else row[2]
        if str(name or "") != "main":
            continue
        path = _blank(file_name)
        if not path:
            return False
        return Path(path).resolve() == PRODUCTION_DB_PATH.resolve()
    return False


def ensure_merge_approval_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent empty approval table. Does not insert executable approvals."""
    stats = {"created_merge_execution_approvals": 0}
    if not _table_exists(conn, "companies"):
        return stats
    existed = _table_exists(conn, "merge_execution_approvals")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merge_execution_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_company_id INTEGER NOT NULL,
            survivor_company_id INTEGER NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            approved_resolution_json TEXT NOT NULL DEFAULT '',
            approved_by_user_id INTEGER,
            approved_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT,
            execution_status TEXT NOT NULL DEFAULT 'pending',
            executed_at TEXT,
            executed_by_user_id INTEGER,
            company_merge_history_id INTEGER,
            failure_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (source_company_id != survivor_company_id),
            CHECK (execution_status IN (
                'pending','executing','executed','failed','expired','superseded'
            )),
            FOREIGN KEY (approved_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (executed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    if not existed:
        stats["created_merge_execution_approvals"] = 1
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_merge_approvals_one_open
            ON merge_execution_approvals(source_company_id, survivor_company_id)
            WHERE execution_status IN ('pending', 'executing')
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_merge_approvals_fingerprint
            ON merge_execution_approvals(plan_fingerprint)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_merge_approvals_status
            ON merge_execution_approvals(execution_status)
        """
    )
    return stats


def _row(conn: sqlite3.Connection, approval_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM merge_execution_approvals WHERE id = ?",
        (int(approval_id),),
    ).fetchone()
    if row is None:
        return None
    return dict(row) if hasattr(row, "keys") else None


def _is_expired(row: dict[str, Any], *, now: str | None = None) -> bool:
    expires = _blank(row.get("expires_at"))
    if not expires:
        return False
    current = now or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return expires <= current


def create_merge_approval(
    conn: sqlite3.Connection,
    *,
    source_company_id: int,
    survivor_company_id: int,
    plan_fingerprint: str,
    approved_resolution: dict[str, Any] | None = None,
    approved_by_user_id: int | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    expires_at: str | None = None,
) -> int:
    """Insert a pending one-time approval. Refuses live northstar.db."""
    if connection_is_production(conn):
        raise MergeApprovalError(
            "Refusing create_merge_approval against live northstar.db."
        )
    ensure_merge_approval_schema(conn)
    src = int(source_company_id)
    dst = int(survivor_company_id)
    fingerprint = _blank(plan_fingerprint)
    if src == dst:
        raise MergeApprovalError("Source and survivor must differ.")
    if not fingerprint:
        raise MergeApprovalError("plan_fingerprint is required.")
    expiry = expires_at
    if expiry is None:
        expiry = (
            datetime.now(timezone.utc) + timedelta(hours=int(ttl_hours))
        ).strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(approved_resolution or {}, sort_keys=True, default=str)
    cur = conn.execute(
        """
        INSERT INTO merge_execution_approvals (
            source_company_id, survivor_company_id, plan_fingerprint,
            approved_resolution_json, approved_by_user_id, expires_at,
            execution_status
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """,
        (src, dst, fingerprint, payload, approved_by_user_id, expiry),
    )
    conn.commit()
    return int(cur.lastrowid)


def load_merge_approval(conn: sqlite3.Connection, approval_id: int) -> dict[str, Any]:
    ensure_merge_approval_schema(conn)
    row = _row(conn, approval_id)
    if row is None:
        raise MergeApprovalError(f"Merge approval {approval_id} does not exist.")
    return row


def assert_approval_matches(
    row: dict[str, Any],
    *,
    source_company_id: int,
    survivor_company_id: int,
    plan_fingerprint: str,
) -> None:
    if int(row["source_company_id"]) != int(source_company_id):
        raise MergeApprovalError("Approval source_company_id does not match the execute pair.")
    if int(row["survivor_company_id"]) != int(survivor_company_id):
        raise MergeApprovalError("Approval survivor_company_id does not match the execute pair.")
    if _blank(row.get("plan_fingerprint")) != _blank(plan_fingerprint):
        raise MergeApprovalError(
            "Approval fingerprint does not match the current plan. Re-plan and re-approve."
        )


def claim_merge_approval(
    conn: sqlite3.Connection,
    approval_id: int,
    *,
    source_company_id: int,
    survivor_company_id: int,
    plan_fingerprint: str,
) -> dict[str, Any]:
    """Commit pending → executing in its own transaction (replay-safe)."""
    ensure_merge_approval_schema(conn)
    if conn.in_transaction:
        conn.commit()
    row = load_merge_approval(conn, approval_id)
    if _blank(row.get("execution_status")) != "pending":
        raise MergeApprovalError(
            f"Merge approval {approval_id} is {row.get('execution_status')}, not pending."
        )
    if _is_expired(row):
        conn.execute(
            """
            UPDATE merge_execution_approvals
            SET execution_status = 'expired',
                failure_reason = 'expired before execution'
            WHERE id = ? AND execution_status = 'pending'
            """,
            (int(approval_id),),
        )
        conn.commit()
        raise MergeApprovalError(f"Merge approval {approval_id} has expired.")
    assert_approval_matches(
        row,
        source_company_id=source_company_id,
        survivor_company_id=survivor_company_id,
        plan_fingerprint=plan_fingerprint,
    )
    cur = conn.execute(
        """
        UPDATE merge_execution_approvals
        SET execution_status = 'executing'
        WHERE id = ? AND execution_status = 'pending'
        """,
        (int(approval_id),),
    )
    if int(cur.rowcount or 0) != 1:
        conn.commit()
        raise MergeApprovalError(f"Merge approval {approval_id} could not be claimed.")
    conn.commit()
    claimed = load_merge_approval(conn, approval_id)
    if _blank(claimed.get("execution_status")) != "executing":
        raise MergeApprovalError(f"Merge approval {approval_id} claim did not stick.")
    return claimed


def complete_merge_approval(
    conn: sqlite3.Connection,
    approval_id: int,
    *,
    company_merge_history_id: int | None,
    executed_by_user_id: int | None = None,
) -> None:
    """Mark executing → executed inside the caller transaction."""
    cur = conn.execute(
        """
        UPDATE merge_execution_approvals
        SET execution_status = 'executed',
            executed_at = datetime('now'),
            executed_by_user_id = ?,
            company_merge_history_id = ?
        WHERE id = ? AND execution_status = 'executing'
        """,
        (executed_by_user_id, company_merge_history_id, int(approval_id)),
    )
    if int(cur.rowcount or 0) != 1:
        raise MergeApprovalError(
            f"Merge approval {approval_id} could not be completed."
        )


def fail_merge_approval(
    conn: sqlite3.Connection,
    approval_id: int,
    reason: str,
) -> None:
    """After merge rollback, mark executing → failed in a new transaction."""
    if conn.in_transaction:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    conn.execute(
        """
        UPDATE merge_execution_approvals
        SET execution_status = 'failed',
            failure_reason = ?
        WHERE id = ? AND execution_status = 'executing'
        """,
        (_blank(reason)[:2000], int(approval_id)),
    )
    conn.commit()


def supersede_stale_approval(conn: sqlite3.Connection, approval_id: int) -> None:
    """Pending/executing approval is not reusable after a stale-plan block."""
    ensure_merge_approval_schema(conn)
    if conn.in_transaction:
        conn.commit()
    conn.execute(
        """
        UPDATE merge_execution_approvals
        SET execution_status = 'superseded',
            failure_reason = 'stale fingerprint; reapproval required'
        WHERE id = ? AND execution_status IN ('pending', 'executing')
        """,
        (int(approval_id),),
    )
    conn.commit()


def live_executable_approval_count(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "merge_execution_approvals"):
        return 0
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM merge_execution_approvals
            WHERE execution_status IN ('pending', 'executing')
            """
        ).fetchone()[0]
    )
