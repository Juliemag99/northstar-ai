"""PostgreSQL migration foundation (Phase 0C).

Isolated tooling only — does not touch live northstar.db or paid cloud DBs.
"""

from __future__ import annotations

from pg_migration.copy_order import MIGRATION_TABLE_ORDER, table_copy_order
from pg_migration.migrate import (
    MigrationReport,
    generate_postgresql_schema_sql,
    migrate_sqlite_fixture_to_plan,
    validate_migration_plan,
)
from pg_migration.schema_pg import render_postgresql_ddl

__all__ = [
    "MIGRATION_TABLE_ORDER",
    "MigrationReport",
    "generate_postgresql_schema_sql",
    "migrate_sqlite_fixture_to_plan",
    "render_postgresql_ddl",
    "table_copy_order",
    "validate_migration_plan",
]
