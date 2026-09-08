"""Portable SQL / transaction helpers for SQLite today and PostgreSQL later.

Phase 0C: helpers are used by migration tooling and new code paths.
Existing modules still use sqlite3 ``?`` placeholders through ``get_connection``;
callers can migrate gradually via ``sql_q`` / ``execute`` / ``last_insert_id``.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from db_config import DatabaseEngine

_QMARK = re.compile(r"\?")


def sql_q(sql: str, engine: DatabaseEngine = DatabaseEngine.SQLITE) -> str:
    """Translate portable ``?`` placeholders to the driver dialect.

    SQLite keeps ``?``. PostgreSQL (psycopg) uses ``%s`` (pyformat).
    """
    if engine is DatabaseEngine.SQLITE:
        return sql
    if engine is DatabaseEngine.POSTGRESQL:
        return _QMARK.sub("%s", sql)
    raise ValueError(f"Unsupported engine for sql_q: {engine!r}")


def now_sql(engine: DatabaseEngine = DatabaseEngine.SQLITE) -> str:
    """SQL expression for 'current UTC timestamp' as text-compatible value.

    SQLite keeps ``datetime('now')`` (existing schema). PostgreSQL uses
    ``to_char(timezone('utc', now()), 'YYYY-MM-DD HH24:MI:SS')`` so TEXT
    columns stay comparable during dual-support. Prefer binding Python UTC
    in application code for new writes.
    """
    if engine is DatabaseEngine.SQLITE:
        return "datetime('now')"
    return "to_char(timezone('utc', now()), 'YYYY-MM-DD HH24:MI:SS')"


def insert_or_ignore_prefix(engine: DatabaseEngine = DatabaseEngine.SQLITE) -> str:
    """Prefix for idempotent insert — prefer ``ON CONFLICT DO NOTHING`` long-term."""
    if engine is DatabaseEngine.SQLITE:
        return "INSERT OR IGNORE"
    return "INSERT"


def on_conflict_do_nothing(
    conflict_target: str,
    engine: DatabaseEngine = DatabaseEngine.SQLITE,
) -> str:
    """Portable no-op conflict clause (append after INSERT … VALUES)."""
    target = conflict_target.strip()
    if not target:
        raise ValueError("conflict_target required")
    # Both engines support ON CONFLICT … DO NOTHING (SQLite 3.24+).
    return f"ON CONFLICT {target} DO NOTHING"


def begin_write(conn: Any, engine: DatabaseEngine = DatabaseEngine.SQLITE) -> None:
    """Start a writer-preferring transaction.

    SQLite: ``BEGIN IMMEDIATE`` (existing semantics).
    PostgreSQL: plain ``BEGIN`` (row locks / advisory locks used at call sites).
    """
    if engine is DatabaseEngine.SQLITE:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("BEGIN")


def last_insert_id(conn: Any, cursor: Any | None = None) -> int:
    """Return the last inserted INTEGER primary key for the connection/cursor.

    SQLite: ``cursor.lastrowid`` / ``last_insert_rowid()``.
    PostgreSQL callers should prefer ``RETURNING id`` and pass the returned
    value; this helper falls back to ``lastval()`` only when needed.
    """
    if cursor is not None:
        rowid = getattr(cursor, "lastrowid", None)
        if rowid is not None and int(rowid) > 0:
            return int(rowid)
    # sqlite3.Connection
    if isinstance(conn, sqlite3.Connection):
        row = conn.execute("SELECT last_insert_rowid()").fetchone()
        if row is None:
            raise RuntimeError("last_insert_rowid returned no row")
        return int(row[0])
    # psycopg / DB-API fallback
    cur = conn.execute("SELECT lastval()")
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("lastval() returned no row")
    return int(row[0])


def execute(
    conn: Any,
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    engine: DatabaseEngine = DatabaseEngine.SQLITE,
) -> Any:
    """Execute SQL with dialect-adjusted placeholders."""
    return conn.execute(sql_q(sql, engine), params or ())


@contextmanager
def transaction(
    conn: Any,
    *,
    engine: DatabaseEngine = DatabaseEngine.SQLITE,
    immediate: bool = True,
) -> Iterator[Any]:
    """Context manager that commits on success and rolls back on error.

    Does not nest well with SQLite's default autocommit-off behavior when a
    transaction is already open — callers must own the connection.
    """
    if immediate:
        begin_write(conn, engine)
    else:
        conn.execute("BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def table_exists_sql(engine: DatabaseEngine = DatabaseEngine.SQLITE) -> str:
    """SQL that returns one row when table named by bind param exists."""
    if engine is DatabaseEngine.SQLITE:
        return (
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1"
        )
    return (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s LIMIT 1"
    )
