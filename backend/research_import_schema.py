"""Research Prospect Import schema.

Never applied by db.migrate_schema. Production install is a separate
administrator-gated additive step (NORTHSTAR_ALLOW_RESEARCH_IMPORT_SCHEMA=1).
That flag does not authorize production confirm.
"""
from __future__ import annotations

import os
from pathlib import Path

from db import PRODUCTION_DB_PATH

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "database" / "research_import_schema.sql"
ALLOW_SCHEMA_ENV = "NORTHSTAR_ALLOW_RESEARCH_IMPORT_SCHEMA"


class LiveResearchSchemaForbidden(RuntimeError):
    """Raised when RI schema is requested against production without the schema gate."""


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


def schema_installed(conn) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_import_batches'"
        ).fetchone()
        is not None
    )


def live_schema_install_allowed() -> bool:
    return os.environ.get(ALLOW_SCHEMA_ENV, "").strip() == "1"


def _table_cols(conn, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn, table: str, columns: list[tuple[str, str]]) -> None:
    existing = _table_cols(conn, table)
    for name, decl in columns:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_BATCH_COLUMNS = [
    ("source_type", "TEXT NOT NULL DEFAULT ''"),
    ("source_label", "TEXT NOT NULL DEFAULT ''"),
    ("source_supplied_by", "TEXT NOT NULL DEFAULT ''"),
    ("mapping_template_id", "INTEGER"),
    ("mapping_template_name", "TEXT NOT NULL DEFAULT ''"),
    ("received_at", "TEXT NOT NULL DEFAULT ''"),
    ("is_ai_generated", "INTEGER NOT NULL DEFAULT 0"),
    ("confirm_result_json", "TEXT NOT NULL DEFAULT '{}'"),
]
_ATTRIBUTE_COLUMNS = [
    ("display_label", "TEXT NOT NULL DEFAULT ''"),
    ("value_type", "TEXT NOT NULL DEFAULT 'TEXT'"),
    ("numeric_value", "REAL"),
    ("date_value", "TEXT NOT NULL DEFAULT ''"),
    ("boolean_value", "INTEGER"),
    ("original_value", "TEXT NOT NULL DEFAULT ''"),
]


def _apply_research_import_schema(conn) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    _add_columns(conn, "research_import_batches", _BATCH_COLUMNS)
    _add_columns(conn, "research_attributes", _ATTRIBUTE_COLUMNS)


def ensure_research_import_schema(conn) -> None:
    """Create RI tables on the current connection.

    Isolated/test databases: always allowed.
    Live northstar.db: allowed only if tables already exist (additive columns)
    or NORTHSTAR_ALLOW_RESEARCH_IMPORT_SCHEMA=1 for first install.
    Does not enable production confirm.
    """
    if is_production_db(conn):
        if schema_installed(conn):
            _add_columns(conn, "research_import_batches", _BATCH_COLUMNS)
            tables = {
                str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "research_attributes" in tables:
                _add_columns(conn, "research_attributes", _ATTRIBUTE_COLUMNS)
            return
        if not live_schema_install_allowed():
            raise LiveResearchSchemaForbidden(
                "Research import schema is not applied to the live database unless "
                "NORTHSTAR_ALLOW_RESEARCH_IMPORT_SCHEMA=1."
            )
    _apply_research_import_schema(conn)
