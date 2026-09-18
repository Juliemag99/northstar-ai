-- Data Steward Phase DS1 isolated schema.
-- DO NOT execute from db.migrate_schema / live northstar.db.
-- Applied only via data_steward.ensure_data_steward_schema() on isolated DBs.

-- Archive columns (ALTER on existing tables in isolated helper):
-- companies.archived_at / archived_by_user_id / archive_reason
-- contacts.archived_at / archived_by_user_id / archive_reason / last_updated_at
-- company_locations.archived_at / archived_by_user_id / archive_reason
-- client_company_relationships.archived_at / archived_by_user_id / archive_reason

CREATE TABLE IF NOT EXISTS field_provenance_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    changed_by_user_id INTEGER,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    action TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    client_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_field_prov_entity
    ON field_provenance_events(entity_type, entity_id, field, id);
CREATE INDEX IF NOT EXISTS idx_field_prov_changed
    ON field_provenance_events(changed_at);
