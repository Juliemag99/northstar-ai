"""Isolated SQLite → PostgreSQL migration planner / in-memory executor.

Never opens production northstar.db. Never connects to paid cloud PostgreSQL.
When no PG service is available, builds a structured plan + generated SQL and
validates via an in-memory row store (contract tests).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pg_migration.copy_order import (
    EXCLUDED_FROM_ROW_COPY,
    primary_key_column,
    table_copy_order,
)
from pg_migration.schema_pg import render_postgresql_ddl, sequence_reset_sql


@dataclass
class TableCopyStats:
    table: str
    source_count: int
    copied_count: int
    skipped_existing: int
    pk_column: str
    max_pk: int | None = None


@dataclass
class MigrationReport:
    source_path: str
    mode: str
    tables: list[TableCopyStats] = field(default_factory=list)
    sequence_sql: list[str] = field(default_factory=list)
    schema_sql_sha256: str = ""
    fk_violations: list[str] = field(default_factory=list)
    count_mismatches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    resumed: bool = False
    success: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


class InMemoryPostgresStore:
    """Minimal row store for contract tests (not a SQL engine)."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[Any, dict[str, Any]]] = {}
        self.checkpoint: set[str] = set()

    def ensure_table(self, name: str) -> None:
        self.tables.setdefault(name, {})

    def upsert_row(self, table: str, pk: Any, row: dict[str, Any]) -> bool:
        """Insert row if PK absent. Returns True if inserted, False if skipped."""
        self.ensure_table(table)
        if pk in self.tables[table]:
            return False
        self.tables[table][pk] = dict(row)
        return True

    def count(self, table: str) -> int:
        return len(self.tables.get(table, {}))

    def mark_done(self, table: str) -> None:
        self.checkpoint.add(table)

    def is_done(self, table: str) -> bool:
        return table in self.checkpoint


def generate_postgresql_schema_sql() -> str:
    return render_postgresql_ddl()


def _list_user_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {str(r[0]) for r in rows}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _fk_edges(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    """Return (from_col, parent_table, to_col) for foreign keys on table."""
    edges: list[tuple[str, str, str]] = []
    for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        edges.append((str(fk["from"]), str(fk["table"]), str(fk["to"])))
    return edges


def migrate_sqlite_fixture_to_plan(
    sqlite_path: Path | str,
    *,
    store: InMemoryPostgresStore | None = None,
    resume: bool = False,
    production_guard_path: Path | str | None = None,
) -> MigrationReport:
    """Copy rows from an isolated SQLite fixture into an in-memory PG store.

    Generates sequence-reset SQL and validates counts / simple FK presence.
    """
    path = Path(sqlite_path)
    prod = Path(production_guard_path) if production_guard_path else None
    if prod is not None and path.resolve() == prod.resolve():
        raise RuntimeError(
            "Refusing to migrate production northstar.db — use an isolated fixture."
        )

    schema_sql = generate_postgresql_schema_sql()
    report = MigrationReport(
        source_path=str(path),
        mode="in_memory_contract",
        schema_sql_sha256=hashlib.sha256(schema_sql.encode("utf-8")).hexdigest(),
        resumed=resume,
    )
    target = store or InMemoryPostgresStore()
    if resume and target.checkpoint:
        report.resumed = True

    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        present = _list_user_tables(conn)
        order = table_copy_order(present)
        for table in order:
            if resume and target.is_done(table):
                src_count = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                report.tables.append(
                    TableCopyStats(
                        table=table,
                        source_count=src_count,
                        copied_count=0,
                        skipped_existing=src_count,
                        pk_column=primary_key_column(table),
                    )
                )
                continue

            pk = primary_key_column(table)
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if pk not in cols:
                # Table without expected PK — copy by full-row hash key
                pk = cols[0] if cols else "id"
            src_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            copied = 0
            skipped = 0
            max_pk: int | None = None
            for row in conn.execute(f"SELECT * FROM {table}"):
                data = _row_to_dict(row)
                key = data.get(pk)
                if key is None:
                    key = hashlib.sha256(
                        json.dumps(data, sort_keys=True, default=str).encode()
                    ).hexdigest()
                inserted = target.upsert_row(table, key, data)
                if inserted:
                    copied += 1
                    if isinstance(key, int):
                        max_pk = key if max_pk is None else max(max_pk, key)
                else:
                    skipped += 1
            target.mark_done(table)
            report.tables.append(
                TableCopyStats(
                    table=table,
                    source_count=src_count,
                    copied_count=copied,
                    skipped_existing=skipped,
                    pk_column=pk,
                    max_pk=max_pk,
                )
            )
            if max_pk is not None:
                report.sequence_sql.append(sequence_reset_sql(table, pk))

        # Count reconciliation
        for stats in report.tables:
            dest = target.count(stats.table)
            expected = stats.source_count
            if dest != expected:
                report.count_mismatches.append(
                    f"{stats.table}: source={expected} dest={dest}"
                )

        # Referential integrity (presence of parent PK)
        for table in order:
            for from_col, parent, to_col in _fk_edges(conn, table):
                if parent in EXCLUDED_FROM_ROW_COPY or parent not in target.tables:
                    continue
                parent_keys = set(target.tables[parent].keys())
                for pk_val, row in target.tables.get(table, {}).items():
                    child_ref = row.get(from_col)
                    if child_ref is None or child_ref == "":
                        continue
                    if child_ref not in parent_keys:
                        report.fk_violations.append(
                            f"{table}.{from_col}={child_ref!r} → "
                            f"missing {parent}.{to_col}"
                        )

        report.success = not report.count_mismatches and not report.fk_violations
    except Exception as exc:  # noqa: BLE001 — surface in report
        report.errors.append(str(exc))
        report.success = False
    finally:
        conn.close()
    return report


def validate_migration_plan(report: MigrationReport) -> bool:
    return bool(report.success) and not report.errors


def build_insert_sql(
    table: str,
    columns: list[str],
    *,
    on_conflict_pk: str | None = None,
) -> str:
    """Generate PostgreSQL INSERT with optional idempotent conflict target."""
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
    if on_conflict_pk:
        sql += f" ON CONFLICT ({on_conflict_pk}) DO NOTHING"
    return sql
