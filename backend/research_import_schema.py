"""Research Prospect Import schema. Isolated/test DBs only.

Never applied by db.migrate_schema. Never creates tables on live northstar.db.
"""
from __future__ import annotations

from pathlib import Path

from db import PRODUCTION_DB_PATH

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "database" / "research_import_schema.sql"


class LiveResearchSchemaForbidden(RuntimeError):
    """Raised when RI-2 schema is requested against production northstar.db."""


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left).replace("\\", "/").lower() == str(right).replace("\\", "/").lower()


def is_production_db(conn) -> bool:
    try:
        path = Path(str(conn.execute("PRAGMA database_list").fetchone()["file"]))
    except Exception:
        return False
    if not str(path).strip():
        return False
    return _same_file(path, Path(PRODUCTION_DB_PATH))


def _table_cols(conn, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn, table: str, columns: list[tuple[str, str]]) -> None:
    existing = _table_cols(conn, table)
    for name, decl in columns:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def ensure_research_import_schema(conn) -> None:
    """Create RI-2/RI-3 tables on the current connection.

    Refuses production/live northstar.db even if a caller asks.
    """
    if is_production_db(conn):
        raise LiveResearchSchemaForbidden(
            "Research import schema is not applied to the live database in RI-2/RI-3."
        )
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    _add_columns(
        conn,
        "research_import_batches",
        [
            ("source_type", "TEXT NOT NULL DEFAULT ''"),
            ("source_label", "TEXT NOT NULL DEFAULT ''"),
            ("source_supplied_by", "TEXT NOT NULL DEFAULT ''"),
            ("mapping_template_id", "INTEGER"),
            ("mapping_template_name", "TEXT NOT NULL DEFAULT ''"),
            ("received_at", "TEXT NOT NULL DEFAULT ''"),
            ("is_ai_generated", "INTEGER NOT NULL DEFAULT 0"),
            ("confirm_result_json", "TEXT NOT NULL DEFAULT '{}'"),
        ],
    )
    _add_columns(
        conn,
        "research_attributes",
        [
            ("display_label", "TEXT NOT NULL DEFAULT ''"),
            ("value_type", "TEXT NOT NULL DEFAULT 'TEXT'"),
            ("numeric_value", "REAL"),
            ("date_value", "TEXT NOT NULL DEFAULT ''"),
            ("boolean_value", "INTEGER"),
            ("original_value", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
