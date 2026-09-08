# Phase 0C — SQLite inventory by category (detail)

Companion to `postgresql-readiness-phase-0c.md`. Classifications:

- **R** production runtime · **M** migration/schema · **T** tests · **S** maintenance scripts  
- **Retain** while SQLite is live · **Replace** for PostgreSQL cutover

## Production runtime — Replace

| Pattern | Primary locations |
|---------|-------------------|
| `sqlite3` / `get_connection` file DB | `db.py` (central — keep as seam) |
| `PRAGMA foreign_keys` / `busy_timeout` | `db.py` |
| UDF `create_function` | `contact_phone.py`, `crm_identity_keys.py` |
| `BEGIN IMMEDIATE` | `crm_import_confirm`, `client_data_import`, `client_workspace_data`, `work_queue_data`, `appointments_data`, `manual_*`, `crm_add_data`, `zoominfo_crm_data`, `shared_note_history_import`, `client_onboarding_data`, `deep_research_data` |
| `lastrowid` / `last_insert_rowid` | Broad writers (confirm, staging, activities, milestones, campaigns, …) |
| `datetime('now')` in DML | `client_workspace_data`, `work_queue_data`, appointments, imports, … |
| `INSERT OR IGNORE` / `OR REPLACE` | seeds, phone keys, email oauth, client_setup, … |
| `sqlite_master` / `PRAGMA table_info` | feature detection across data modules |
| FTS5 `MATCH` / `bm25` / `snippet` | `search_data.py`, Ask NorthStar FTS path |
| Request-time `ensure_*` DDL | campaigns, appointments, knowledge, email, CRM staging, … |

## Migration / schema — Replace engine, keep concepts

| Artifact | Notes |
|----------|-------|
| `database/schema.sql` | AUTOINCREMENT, PRAGMA, FTS5, UDF triggers, COLLATE NOCASE |
| `db.migrate_schema` / `init_schema` | Rewrite as versioned PG migrations |
| Partial ZoomInfo / identity indexes | **Retain** SQL shape (PG supports partial indexes) |
| Phone / identity triggers | Replace with app upserts or PG functions |

## Safe to retain (SQLite dual-support)

- Partial indexes `WHERE col != ''`
- Most `ON CONFLICT … DO NOTHING/UPDATE`
- Identity-key **tables** (not FTS)
- App-side UTC timestamps already used in many writers
- Confirm path design: no DDL inside confirm transaction

## Tests only — Retain for SQLite CI

- Direct `sqlite3.connect` / `backup` in auth, identity negative tests, scale harnesses
- Fixture `INSERT OR IGNORE`, `datetime('now')`, `rowid` ordering

## Maintenance / import scripts — Replace if scripts move to PG

- `import_brown_industries.py`, `reload_carmeco_source.py`, `_phase3_*` file copies to `database/backups/`

## Filesystem — SQLite-era only

- `database/northstar.db`, `-wal`/`-shm` cleanup, `.bak-*` copies
- Future PG: `pg_dump` / managed backups

## Placeholder density (`?`)

Highest in Ask NorthStar, client knowledge, engagement import, research, workspace.  
Bridge via `db_sql.sql_q` during cutover; no wholesale rewrite in Phase 0C.
