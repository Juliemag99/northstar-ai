# PostgreSQL Readiness — Phase 0C

**Status:** Source-only foundation. Live database remains SQLite (`database/northstar.db`).  
**Not in this phase:** production cutover, paid/cloud PostgreSQL, service restarts, live imports.

## Architecture (proposed)

```
┌─────────────────────────────────────────────────────────────┐
│ Application modules (CRM, auth, imports, Work Queue, …)     │
│  → must use db.get_connection() / db_sql helpers            │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ db_config.load_database_config()                            │
│  - default: SQLite path (NORTHSTAR_SQLITE_PATH / northstar) │
│  - future: NORTHSTAR_DB_ENGINE=postgresql + DATABASE_URL    │
│  - secrets redacted in safe_summary() / logs                │
└──────────────────────────┬──────────────────────────────────┘
                           │
          ┌────────────────┴────────────────┐
          ▼                                 ▼
   SQLite (live today)              PostgreSQL (tooling only)
   db.get_connection()              Phase 0C: refused at runtime
   + phone/identity UDFs            pg_migration.* for plans/DDL
   + PRAGMA foreign_keys            in-memory contract migrator
```

**Principles**
- Portable SQL and explicit maintained identity-key tables (no PG-only business rules).
- Transaction ownership stays at call sites via `db_sql.transaction` / existing `BEGIN IMMEDIATE` until cutover maps them.
- Search/FTS replaced later with `tsvector` (or equivalent) — not required for CRM integrity.
- Backups for PG will use `pg_dump`; SQLite file/WAL copies remain for SQLite-era ops only.

## Compatibility mapping

| Concern | SQLite today | PostgreSQL target |
|--------|--------------|-------------------|
| Connection / rows | `sqlite3` + `Row` factory via `get_connection` | `psycopg` connection pool; dict/row factory |
| Placeholders | `?` | `%s` via `db_sql.sql_q` |
| Transactions / locking | `BEGIN IMMEDIATE` | `BEGIN` + `SELECT … FOR UPDATE` / advisory locks at confirm sites |
| Upserts | `INSERT OR IGNORE` / `OR REPLACE` / `ON CONFLICT` | Prefer `ON CONFLICT DO NOTHING/UPDATE` |
| Inserted IDs | `lastrowid` / `last_insert_rowid()` | `RETURNING id` (helper fallback `lastval`) |
| Schema migration | `schema.sql` + `migrate_schema` + request-time `ensure_*` | Versioned migrations; startup-only DDL; drop request-time DDL |
| Identity keys | Side tables + SQLite UDFs/triggers | Same tables; app upserts (already present) or PG functions |
| Search / FTS | FTS5 `search_fts` | `tsvector` / dedicated search table; rebuild job |
| Case-insensitive | `COLLATE NOCASE` | `LOWER()` indexes or `citext` |
| Timestamps | TEXT + `datetime('now')` | Phase 0C keeps TEXT + UTC formatting; later `timestamptz` optional |
| Pagination | `LIMIT`/`OFFSET` | Same |
| Atomic imports | Single connection + `BEGIN IMMEDIATE` | Single connection + serializable/row locks; same fingerprint checks |
| Concurrent confirms | SQLite write lock | Advisory lock per batch + fingerprint |
| Backups | File copy / `.bak` / WAL | `pg_dump` / PITR |

## High-risk behavior (must preserve)

Encoded as design invariants for later PG implementation tests:

1. One master `companies` row shared across clients; statuses/notes on `client_company_relationships`.
2. Indexed Record No. / identity-key matching (`company_identity_keys`, `contact_person_keys`, phone keys).
3. CRM Import + Client Data Import atomicity; stale fingerprint refusal.
4. Shared-history event hash idempotency; Closed exclusion policy.
5. Work Queue / Companies server-side pagination; All My Clients authorization.
6. Deep Research job concurrency/idempotency.
7. Staff sessions + CSRF unchanged.

Phase 0C proves migration plumbing (PK preservation, sequences, JSON/Unicode/long notes, rollback/restart) on **isolated fixtures** — not a live PG cutover.

## Exclusions from row copy

Intentionally **not** copied (rebuild / SQLite-internal):

- `search_fts` and FTS5 shadow tables (`search_fts_*`)
- `sqlite_sequence`

See `EXCLUDED_FROM_ROW_COPY` in `backend/pg_migration/copy_order.py`.
All other persistent application tables from `schema.sql` plus known
runtime-ensure tables are listed in `MIGRATION_TABLE_ORDER`.


## Tooling

| Module | Role |
|--------|------|
| `db_config.py` | Engine selection + secret redaction |
| `db_sql.py` | Portable placeholders, transactions, insert-id |
| `pg_migration/schema_pg.py` | Generate PostgreSQL DDL from `schema.sql` |
| `pg_migration/migrate.py` | Isolated SQLite → in-memory plan/copy + report |
| `pg_migration/copy_order.py` | FK-safe table order |

## Remaining blockers before a test PostgreSQL migration

1. Wire a **local** (non-paid) PostgreSQL for integration tests, or continue contract mode.
2. Replace request-time `ensure_*` DDL with startup migrations.
3. Port UDF-backed triggers to app upserts (already partially done) / PG functions.
4. Replace FTS5 search path.
5. Sweep production `lastrowid` / `datetime('now')` / `INSERT OR IGNORE` / `sqlite_master` call sites.
6. Driver + pool + health checks; refuse ambiguous dual config in production.

## SQLite-specific inventory (summary by category)

Full file:line inventory was produced during Phase 0C discovery (see agent explore report). Categories:

### Production runtime — must replace for PostgreSQL
- `db.get_connection` / `sqlite3` driver, file path, PRAGMA, UDF registration
- `BEGIN IMMEDIATE` across confirm, workspace, work queue, appointments, deep research, …
- `lastrowid` / `last_insert_rowid` across writers
- `datetime('now')` in DML
- `INSERT OR IGNORE` / `OR REPLACE` (prefer `ON CONFLICT`)
- `sqlite_master` / `PRAGMA table_info` feature detection
- FTS5 in `search_data.py`
- Request-time `ensure_*` DDL

### Migration / schema — must replace engine, keep concepts
- `database/schema.sql` (AUTOINCREMENT, PRAGMA, FTS, UDF triggers, COLLATE NOCASE)
- `db.migrate_schema` / partial ZoomInfo indexes (partial indexes themselves are PG-OK)

### Safe to retain while SQLite is live
- Partial indexes `WHERE col != ''`
- Most `ON CONFLICT … DO UPDATE/NOTHING`
- Application-level UTC timestamps already used in many paths
- Identity-key **table** design (not FTS)

### Tests only — retain for SQLite CI; dual-mode later
- Direct `sqlite3.connect` in tests (isolation, negative UDF tests)
- Fixture `INSERT OR IGNORE`, `datetime('now')`

### Maintenance / import scripts
- `import_brown_industries.py`, `reload_carmeco_source.py`, `_phase3_*` file backups

### Filesystem assumptions
- `northstar.db`, `-wal`/`-shm` cleanup, `.bak-*` copies — SQLite-era only
