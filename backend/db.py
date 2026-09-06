"""SQLite connection helpers for the NorthStar local database."""

from __future__ import annotations

import atexit
import os
import sqlite3
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATABASE_DIR = REPO_ROOT / "database"
PRODUCTION_DB_PATH = DATABASE_DIR / "northstar.db"
SCHEMA_PATH = DATABASE_DIR / "schema.sql"
DEFAULT_USER_EMAIL = "juliem@n-star.us"


class _DatabasePath:
    """Mutable path object so tests can isolate without rebinding imports."""

    def __init__(self, path: Path):
        object.__setattr__(self, "_path", Path(path))

    def set(self, path: Path | str) -> None:
        object.__setattr__(self, "_path", Path(path))

    def __fspath__(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return f"DB_PATH({self._path})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _DatabasePath):
            return self._path == other._path
        return self._path == other

    def __hash__(self) -> int:
        return hash(self._path)

    def __getattr__(self, name: str):
        return getattr(self._path, name)


DB_PATH = _DatabasePath(PRODUCTION_DB_PATH)
_TEST_ISOLATED = False

# Additive column migrations for existing local DBs (CREATE IF NOT EXISTS will not alter).
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "users": [
        ("is_internal_northstar", "INTEGER NOT NULL DEFAULT 1"),
        ("password_hash", "TEXT NOT NULL DEFAULT ''"),
        ("password_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ("failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
        ("locked_until", "TEXT NOT NULL DEFAULT ''"),
    ],
    "client_company_relationships": [
        ("assigned_user_id", "INTEGER"),
        ("priority", "TEXT NOT NULL DEFAULT ''"),
        ("next_action", "TEXT NOT NULL DEFAULT ''"),
        ("follow_up_date", "TEXT"),
        ("notes", "TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ("is_hot", "INTEGER NOT NULL DEFAULT 0"),
        ("external_record_no", "TEXT NOT NULL DEFAULT ''"),
    ],
    "activities": [
        ("user_id", "INTEGER"),
        ("follow_up_completed", "INTEGER NOT NULL DEFAULT 0"),
        ("completion_status", "TEXT NOT NULL DEFAULT 'open'"),
    ],
    "opportunity_assignments": [
        ("opportunity_score", "INTEGER NOT NULL DEFAULT 0"),
        ("source_summary", "TEXT NOT NULL DEFAULT ''"),
    ],
    "companies": [
        ("zoominfo_company_id", "TEXT NOT NULL DEFAULT ''"),
        ("source", "TEXT NOT NULL DEFAULT ''"),
        ("source_updated_at", "TEXT NOT NULL DEFAULT ''"),
    ],
    "contacts": [
        ("linkedin_url", "TEXT NOT NULL DEFAULT ''"),
        ("location", "TEXT NOT NULL DEFAULT ''"),
        ("zoominfo_contact_id", "TEXT NOT NULL DEFAULT ''"),
        ("source", "TEXT NOT NULL DEFAULT ''"),
        ("source_updated_at", "TEXT NOT NULL DEFAULT ''"),
    ],
    "crm_import_batches": [
        ("mapping_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("mapping_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ("mapping_updated_by_user_id", "INTEGER"),
        # Checkpoint C3 audit columns.
        ("imported_at", "TEXT NOT NULL DEFAULT ''"),
        ("imported_by_user_id", "INTEGER"),
        ("confirmed_plan_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("created_company_count", "INTEGER NOT NULL DEFAULT 0"),
        ("reused_company_count", "INTEGER NOT NULL DEFAULT 0"),
        ("created_contact_count", "INTEGER NOT NULL DEFAULT 0"),
        ("reused_contact_count", "INTEGER NOT NULL DEFAULT 0"),
        ("created_relationship_count", "INTEGER NOT NULL DEFAULT 0"),
        ("existing_relationship_count", "INTEGER NOT NULL DEFAULT 0"),
        ("no_contact_row_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_imported_row_count", "INTEGER NOT NULL DEFAULT 0"),
        ("imported_status_count", "INTEGER NOT NULL DEFAULT 0"),
        ("default_status_count", "INTEGER NOT NULL DEFAULT 0"),
        ("preserved_status_count", "INTEGER NOT NULL DEFAULT 0"),
        ("notes_set_count", "INTEGER NOT NULL DEFAULT 0"),
        ("notes_appended_count", "INTEGER NOT NULL DEFAULT 0"),
        ("notes_duplicate_count", "INTEGER NOT NULL DEFAULT 0"),
        ("notes_unchanged_count", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "crm_import_results": [
        ("status_action", "TEXT NOT NULL DEFAULT ''"),
        ("notes_action", "TEXT NOT NULL DEFAULT ''"),
    ],
    "company_research_runs": [
        ("campaign_id", "INTEGER"),
        ("research_depth", "TEXT NOT NULL DEFAULT 'quick'"),
        ("openai_response_id", "TEXT NOT NULL DEFAULT ''"),
        ("usage_json", "TEXT NOT NULL DEFAULT ''"),
        ("citations_json", "TEXT NOT NULL DEFAULT ''"),
        ("error_message", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def _resolved_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is None:
        path = Path(os.fspath(DB_PATH))
    else:
        path = Path(db_path)
    test_db = os.environ.get("NORTHSTAR_TEST_DB", "").strip()
    if test_db and path.resolve() == PRODUCTION_DB_PATH.resolve():
        raise RuntimeError(
            "Automated tests are isolated and must not open production northstar.db"
        )
    return path


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = _resolved_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    from contact_phone import register_contact_phone_functions

    register_contact_phone_functions(conn)
    return conn


def isolate_for_tests() -> Path:
    """Copy production into a throwaway SQLite file. Never writes production."""
    global _TEST_ISOLATED
    env_path = os.environ.get("NORTHSTAR_TEST_DB", "").strip()
    if env_path:
        DB_PATH.set(env_path)
        _TEST_ISOLATED = True
        return Path(env_path)
    if _TEST_ISOLATED:
        return Path(os.fspath(DB_PATH))

    handle, name = tempfile.mkstemp(prefix="northstar-test-", suffix=".db")
    os.close(handle)
    os.unlink(name)
    if PRODUCTION_DB_PATH.exists():
        src = sqlite3.connect(str(PRODUCTION_DB_PATH))
        try:
            dst = sqlite3.connect(name)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
    DB_PATH.set(name)
    os.environ["NORTHSTAR_TEST_DB"] = name
    _TEST_ISOLATED = True

    def _cleanup() -> None:
        for candidate in (name, name + "-wal", name + "-shm"):
            try:
                os.remove(candidate)
            except OSError:
                pass

    atexit.register(_cleanup)
    return Path(name)


if os.environ.get("NORTHSTAR_TEST_DB", "").strip():
    isolate_for_tests()


def init_schema(conn: sqlite3.Connection) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.commit()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r["name"]) for r in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add missing columns/indexes on existing databases without wiping data."""
    for table, columns in _COLUMN_MIGRATIONS.items():
        if not _table_exists(conn, table):
            continue
        existing = _table_columns(conn, table)
        for name, declaration in columns:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    # Indexes that CREATE INDEX IF NOT EXISTS may not have run on older DBs
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ccr_assigned_user
            ON client_company_relationships(assigned_user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ccr_follow_up_date
            ON client_company_relationships(follow_up_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_activities_user
            ON activities(user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_client_user
            ON user_client_assignments(user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_client_client
            ON user_client_assignments(client_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ccr_is_hot
            ON client_company_relationships(is_hot)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ccr_client_record_no
            ON client_company_relationships(client_id, external_record_no)
        """
    )
    # Backfill relationship Record Nos from master company when empty
    # (legacy Carmeco rows stored Record No. only on companies).
    if "external_record_no" in _table_columns(conn, "client_company_relationships"):
        conn.execute(
            """
            UPDATE client_company_relationships
            SET external_record_no = (
                SELECT co.external_record_no
                FROM companies co
                WHERE co.id = client_company_relationships.company_id
            )
            WHERE TRIM(COALESCE(external_record_no, '')) = ''
            """
        )
    if _table_exists(conn, "work_queue_items"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_work_queue_client_due
                ON work_queue_items(client_id, due_date, completion_status)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_work_queue_action
                ON work_queue_items(action_type, completion_status)
            """
        )
    if _table_exists(conn, "revenue_milestones"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_milestones_client_company
                ON revenue_milestones(client_id, company_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_milestones_type
                ON revenue_milestones(milestone_type)
            """
        )
    _ensure_zoominfo_indexes(conn)
    # Deep Research jobs (idempotent; also defined in schema.sql).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_research_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            working_for_client_id INTEGER NOT NULL,
            campaign_id INTEGER,
            research_run_id INTEGER,
            research_depth TEXT NOT NULL DEFAULT 'deep',
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            progress_message TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            openai_response_id TEXT NOT NULL DEFAULT '',
            openai_model TEXT NOT NULL DEFAULT '',
            usage_json TEXT NOT NULL DEFAULT '',
            citations_json TEXT NOT NULL DEFAULT '',
            sources_json TEXT NOT NULL DEFAULT '',
            report_json TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            initiated_by_user_id INTEGER,
            initiated_by_name TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
            FOREIGN KEY (working_for_client_id) REFERENCES clients(id) ON DELETE CASCADE,
            FOREIGN KEY (research_run_id) REFERENCES company_research_runs(id) ON DELETE SET NULL,
            FOREIGN KEY (initiated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_research_jobs_active
            ON company_research_jobs(company_id, working_for_client_id, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_research_jobs_company
            ON company_research_jobs(company_id, created_at DESC)
        """
    )
    if _table_exists(conn, "contacts"):
        # Bounded Add Contact name lookup. Does not change contact rows.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_contacts_last_first_nocase
                ON contacts(last_name COLLATE NOCASE, first_name COLLATE NOCASE)
            """
        )
        from contact_phone import ensure_contact_phone_key_schema

        # Derived phone keys only. Does not rewrite contacts.phone / alt_phone.
        ensure_contact_phone_key_schema(conn)
    from auth_sessions import ensure_staff_auth_schema

    ensure_staff_auth_schema(conn)
    from client_onboarding_data import ensure_client_onboarding_schema

    ensure_client_onboarding_schema(conn)
    conn.commit()


def _ensure_zoominfo_indexes(conn: sqlite3.Connection) -> None:
    if (
        _table_exists(conn, "companies")
        and "zoominfo_company_id" in _table_columns(conn, "companies")
    ):
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_zoominfo_id
                ON companies(zoominfo_company_id)
                WHERE TRIM(zoominfo_company_id) != ''
            """
        )
    if (
        _table_exists(conn, "contacts")
        and "zoominfo_contact_id" in _table_columns(conn, "contacts")
    ):
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_zoominfo_id
                ON contacts(zoominfo_contact_id)
                WHERE TRIM(zoominfo_contact_id) != ''
            """
        )


def ensure_zoominfo_columns(conn: sqlite3.Connection | None = None) -> None:
    """Add ZoomInfo/source columns without the full schema backfill.

    Isolated tests copy production before uvicorn startup, so TestClient would
    otherwise INSERT into a companies table that still lacks `source`.
    """
    own = conn is None
    if own:
        conn = get_connection()
    assert conn is not None
    try:
        specs = {
            "companies": [
                ("zoominfo_company_id", "TEXT NOT NULL DEFAULT ''"),
                ("source", "TEXT NOT NULL DEFAULT ''"),
                ("source_updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            "contacts": [
                ("linkedin_url", "TEXT NOT NULL DEFAULT ''"),
                ("location", "TEXT NOT NULL DEFAULT ''"),
                ("zoominfo_contact_id", "TEXT NOT NULL DEFAULT ''"),
                ("source", "TEXT NOT NULL DEFAULT ''"),
                ("source_updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
        }
        for table, columns in specs.items():
            if not _table_exists(conn, table):
                continue
            existing = _table_columns(conn, table)
            for name, declaration in columns:
                if name in existing:
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        _ensure_zoominfo_indexes(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


def seed_default_users(conn: sqlite3.Connection) -> None:
    """Ensure Julie Magnani exists and is assigned to Carmeco (idempotent)."""
    if not _table_exists(conn, "users") or not _table_exists(conn, "clients"):
        return

    # Never insert a second Julie. Live id 1 is the staff account even before
    # the email cutover; a missing id 1 is a fresh empty database.
    if conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone() is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (email, full_name, is_administrator, active)
            VALUES (?, 'Julie Magnani', 0, 1)
            """,
            (DEFAULT_USER_EMAIL,),
        )
    # NorthStar management user for All Clients / cross-client authorization tests
    conn.execute(
        """
        INSERT OR IGNORE INTO users (email, full_name, is_administrator, active)
        VALUES ('admin@northstargroup.com', 'NorthStar Admin', 1, 1)
        """
    )
    user = conn.execute("SELECT id FROM users WHERE id = 1").fetchone()
    if user is None:
        user = conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(?)",
            (DEFAULT_USER_EMAIL,),
        ).fetchone()
    if user is None:
        conn.commit()
        return

    user_id = int(user["id"])
    for client_code, role in (("carmeco", "account_executive"), ("brown", "account_executive")):
        client = conn.execute(
            "SELECT id FROM clients WHERE code = ?",
            (client_code,),
        ).fetchone()
        if client is None:
            continue
        client_id = int(client["id"])
        conn.execute(
            """
            INSERT OR IGNORE INTO user_client_assignments (
                user_id, client_id, role, active
            ) VALUES (?, ?, ?, 1)
            """,
            (user_id, client_id, role),
        )
        # Backfill relationship ownership when unset
        conn.execute(
            """
            UPDATE client_company_relationships
            SET assigned_user_id = ?
            WHERE client_id = ? AND assigned_user_id IS NULL
            """,
            (user_id, client_id),
        )
        # Backfill activity user_id when unset
        if _table_exists(conn, "activities") and "user_id" in _table_columns(
            conn, "activities"
        ):
            conn.execute(
                """
                UPDATE activities
                SET user_id = ?
                WHERE client_id = ? AND user_id IS NULL
                """,
                (user_id, client_id),
            )
    conn.commit()


def ensure_schema(db_path: Path | None = None) -> None:
    """Apply schema idempotently so new tables/columns exist on existing DBs."""
    with get_connection(db_path) as conn:
        # Columns first on existing tables, then CREATE TABLE / indexes from schema.sql
        migrate_schema(conn)
        init_schema(conn)
        migrate_schema(conn)  # indexes that depend on newly added columns
        seed_default_users(conn)
        from milestones_data import backfill_milestones_from_existing

        backfill_milestones_from_existing(conn)
        from ask_northstar_data import ensure_ask_northstar_schema

        ensure_ask_northstar_schema(conn)
        from research_data import ensure_research_schema

        ensure_research_schema(conn)
        from client_setup_data import ensure_client_setup_schema

        ensure_client_setup_schema(conn)
        from client_knowledge_data import ensure_client_knowledge_schema

        ensure_client_knowledge_schema(conn)
        from client_engagement_import import ensure_engagement_import_schema

        ensure_engagement_import_schema(conn)
        from client_workspace_data import ensure_contact_workflow_schema

        ensure_contact_workflow_schema(conn)
        from next_actions import ensure_next_action_schema

        ensure_next_action_schema(conn)
        from appointments_data import ensure_appointments_schema

        ensure_appointments_schema(conn)
        from campaigns_data import ensure_campaigns_schema

        ensure_campaigns_schema(conn)
        from crm_import_staging import ensure_crm_import_schema

        ensure_crm_import_schema(conn)

    # Build/refresh FTS index after schema is ready
    from search_data import rebuild_search_index

    rebuild_search_index()


def reset_database(db_path: Path | None = None) -> sqlite3.Connection:
    """Drop and recreate the local database file, then apply schema."""
    path = db_path or DB_PATH
    if path.exists():
        path.unlink()
    conn = get_connection(path)
    init_schema(conn)
    migrate_schema(conn)
    seed_default_users(conn)
    return conn
