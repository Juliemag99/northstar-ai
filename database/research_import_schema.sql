-- Research Prospect Import schema.
-- DO NOT execute from db.migrate_schema.
-- Isolated/test: research_import_schema.ensure_research_import_schema().
-- Live northstar.db first install: NORTHSTAR_ALLOW_RESEARCH_IMPORT_SCHEMA=1 only.
-- That flag does not authorize production confirm. Additive CREATE IF NOT EXISTS only.

CREATE TABLE IF NOT EXISTS research_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    batch_name TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT '',
    worksheet_name TEXT NOT NULL DEFAULT '',
    file_size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    research_method TEXT NOT NULL DEFAULT '',
    research_date TEXT NOT NULL DEFAULT '',
    prior_research_file TEXT NOT NULL DEFAULT '',
    original_research_file TEXT NOT NULL DEFAULT '',
    batch_caveat TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'previewed',
    error_message TEXT NOT NULL DEFAULT '',
    headers_json TEXT NOT NULL DEFAULT '[]',
    mapping_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    source_row_count INTEGER NOT NULL DEFAULT 0,
    uploaded_by_user_id INTEGER,
    uploaded_by_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    cancelled_at TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL DEFAULT '',
    confirmed_plan_fingerprint TEXT NOT NULL DEFAULT '',
    confirm_result_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (uploaded_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_research_import_batches_client
    ON research_import_batches(client_id, id DESC);

CREATE TABLE IF NOT EXISTS research_import_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    source_row_number INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    mapped_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]',
    is_blank INTEGER NOT NULL DEFAULT 0,
    has_blocking_error INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (batch_id) REFERENCES research_import_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_research_import_rows_batch
    ON research_import_rows(batch_id, source_row_number);

CREATE TABLE IF NOT EXISTS research_import_match_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    staged_row_id INTEGER NOT NULL,
    resolution_type TEXT NOT NULL,
    company_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by_user_id INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (batch_id) REFERENCES research_import_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (staged_row_id) REFERENCES research_import_rows(id) ON DELETE CASCADE,
    UNIQUE (client_id, batch_id, staged_row_id)
);

CREATE TABLE IF NOT EXISTS research_import_contact_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    staged_row_id INTEGER NOT NULL,
    resolution_type TEXT NOT NULL,
    contact_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by_user_id INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (batch_id) REFERENCES research_import_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (staged_row_id) REFERENCES research_import_rows(id) ON DELETE CASCADE,
    UNIQUE (client_id, batch_id, staged_row_id)
);

CREATE TABLE IF NOT EXISTS research_import_master_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    staged_row_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    resolution_type TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by_user_id INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (batch_id) REFERENCES research_import_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (staged_row_id) REFERENCES research_import_rows(id) ON DELETE CASCADE,
    UNIQUE (client_id, batch_id, staged_row_id, field)
);

CREATE TABLE IF NOT EXISTS client_company_research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    relationship_id INTEGER,
    batch_id INTEGER,
    research_priority_code TEXT NOT NULL DEFAULT '',
    research_priority_label TEXT NOT NULL DEFAULT '',
    why_client_fits TEXT NOT NULL DEFAULT '',
    qualification_notes TEXT NOT NULL DEFAULT '',
    is_current INTEGER NOT NULL DEFAULT 1,
    researched_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by_user_id INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE SET NULL,
    FOREIGN KEY (batch_id) REFERENCES research_import_batches(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_client_company_research_current
    ON client_company_research(client_id, company_id, is_current);
CREATE UNIQUE INDEX IF NOT EXISTS idx_client_company_research_one_current
    ON client_company_research(client_id, company_id)
    WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS research_attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    company_id INTEGER NOT NULL,
    research_id INTEGER,
    batch_id INTEGER,
    attribute_type TEXT NOT NULL,
    attribute_value TEXT NOT NULL,
    value_norm TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'client',
    is_current INTEGER NOT NULL DEFAULT 1,
    source_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (research_id) REFERENCES client_company_research(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_research_attributes_lookup
    ON research_attributes(client_id, attribute_type, value_norm)
    WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_research_attributes_company
    ON research_attributes(company_id, attribute_type, is_current);

CREATE TABLE IF NOT EXISTS research_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER,
    research_id INTEGER,
    company_id INTEGER,
    client_id INTEGER,
    source_role TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_text TEXT NOT NULL DEFAULT '',
    source_domain TEXT NOT NULL DEFAULT '',
    field_supported TEXT NOT NULL DEFAULT '',
    original_value TEXT NOT NULL DEFAULT '',
    researched_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (research_id) REFERENCES client_company_research(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_research_sources_research
    ON research_sources(research_id, source_role);
CREATE INDEX IF NOT EXISTS idx_research_sources_domain
    ON research_sources(source_domain)
    WHERE TRIM(source_domain) != '';

-- RI-3: governed custom attributes + reusable mapping templates (isolated only).
CREATE TABLE IF NOT EXISTS research_attribute_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute_key TEXT NOT NULL UNIQUE,
    display_label TEXT NOT NULL DEFAULT '',
    value_type TEXT NOT NULL DEFAULT 'TEXT',
    scope_default TEXT NOT NULL DEFAULT 'client',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS research_import_mapping_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_name TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    client_id INTEGER,
    header_signature TEXT NOT NULL DEFAULT '',
    headers_json TEXT NOT NULL DEFAULT '[]',
    mapping_json TEXT NOT NULL DEFAULT '{}',
    safety_class TEXT NOT NULL DEFAULT 'safe_reusable',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by_user_id INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_research_mapping_templates_sig
    ON research_import_mapping_templates(source_type, header_signature);
