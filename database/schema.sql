-- NorthStar AI local relational schema (SQLite)
-- Carmeco status and notes live on client_company_relationships / legacy_notes,
-- not as universal company fields.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- NorthStar employees / revenue development specialists
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    is_administrator INTEGER NOT NULL DEFAULT 0,
    -- Internal NorthStar employees may view cross-client shared history/search.
    -- Future external/client-facing users should be set to 0 (own-client only).
    is_internal_northstar INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Staff auth (Phase 0). Empty password_hash means the user cannot log in.
    password_hash TEXT NOT NULL DEFAULT '',
    password_updated_at TEXT NOT NULL DEFAULT '',
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NOT NULL DEFAULT ''
);

-- Many-to-many: a user may work for many clients; a client may have many users
CREATE TABLE IF NOT EXISTS user_client_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, client_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_record_no TEXT NOT NULL UNIQUE,
    company_name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    -- Legacy primary-contact fields from the company import row (preserved as-is)
    legacy_first_name TEXT NOT NULL DEFAULT '',
    legacy_last_name TEXT NOT NULL DEFAULT '',
    legacy_title TEXT NOT NULL DEFAULT '',
    legacy_phone TEXT NOT NULL DEFAULT '',
    legacy_alt_phone TEXT NOT NULL DEFAULT '',
    legacy_mobile TEXT NOT NULL DEFAULT '',
    legacy_email TEXT NOT NULL DEFAULT '',
    sales_volume_range TEXT NOT NULL DEFAULT '',
    location_sales_volume_range TEXT NOT NULL DEFAULT '',
    employee_size_range TEXT NOT NULL DEFAULT '',
    primary_sic_code TEXT NOT NULL DEFAULT '',
    primary_sic_description TEXT NOT NULL DEFAULT '',
    primary_naics_code TEXT NOT NULL DEFAULT '',
    primary_naics_description TEXT NOT NULL DEFAULT '',
    sic_8_digit TEXT NOT NULL DEFAULT '',
    sic_8_digit_description TEXT NOT NULL DEFAULT '',
    type_of_industry TEXT NOT NULL DEFAULT '',
    customer_campaign TEXT NOT NULL DEFAULT '',
    entered_at TEXT NOT NULL DEFAULT '',
    last_updated_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    zoominfo_company_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    external_record_no TEXT NOT NULL,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    alt_phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    source_row_index INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    linkedin_url TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    zoominfo_contact_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS client_company_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    -- Legacy Record No. belongs to the CLIENT-COMPANY RELATIONSHIP
    -- (not assumed to be a universal master-company identifier).
    external_record_no TEXT NOT NULL DEFAULT '',
    -- Client-specific CRM fields (never universal company fields)
    status TEXT NOT NULL DEFAULT '',
    assigned_user_id INTEGER,
    priority TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    follow_up_date TEXT,
    notes TEXT NOT NULL DEFAULT '',
    is_hot INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (client_id, company_id),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (assigned_user_id) REFERENCES users(id) ON DELETE SET NULL
);

-- Per-client operational workflow for a shared master contact.
-- Never stores status on contacts; never shared across clients.
CREATE TABLE IF NOT EXISTS contact_client_workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    relationship_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    assigned_user_id INTEGER,
    next_action TEXT NOT NULL DEFAULT '',
    follow_up_date TEXT,
    follow_up_time TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (contact_id, client_id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE,
    FOREIGN KEY (assigned_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_contact_workflows_client
    ON contact_client_workflows(client_id, contact_id);

-- Explicit per-client assignment of a shared master contact (does not copy the person).
CREATE TABLE IF NOT EXISTS contact_client_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    relationship_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (contact_id, client_id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contact_client_rel_client
    ON contact_client_relationships(client_id, contact_id);

CREATE TABLE IF NOT EXISTS legacy_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    note_text TEXT NOT NULL,
    source_field TEXT NOT NULL DEFAULT 'Sales Rep Comments/Notes',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_companies_external_record_no
    ON companies(external_record_no);

-- Maintained identity keys for indexed CRM / Client Data Import matching.
-- PostgreSQL: same tables with btree indexes on the signal columns (partial
-- WHERE col <> '' is supported). Prefer triggers or app upserts over
-- SQLite-only generated columns.
CREATE TABLE IF NOT EXISTS company_identity_keys (
    company_id INTEGER PRIMARY KEY,
    record_no TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    norm_name TEXT NOT NULL DEFAULT '',
    phone_digits TEXT NOT NULL DEFAULT '',
    phone_last7 TEXT NOT NULL DEFAULT '',
    addr_norm TEXT NOT NULL DEFAULT '',
    city_norm TEXT NOT NULL DEFAULT '',
    state_norm TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_company_identity_record_no
    ON company_identity_keys(record_no)
    WHERE record_no != '';
CREATE INDEX IF NOT EXISTS idx_company_identity_domain
    ON company_identity_keys(domain)
    WHERE domain != '';
CREATE INDEX IF NOT EXISTS idx_company_identity_norm_name
    ON company_identity_keys(norm_name)
    WHERE norm_name != '';
CREATE INDEX IF NOT EXISTS idx_company_identity_phone
    ON company_identity_keys(phone_digits)
    WHERE phone_digits != '';
CREATE INDEX IF NOT EXISTS idx_company_identity_phone_last7
    ON company_identity_keys(phone_last7)
    WHERE phone_last7 != '';
CREATE INDEX IF NOT EXISTS idx_company_identity_addr
    ON company_identity_keys(addr_norm, city_norm, state_norm)
    WHERE addr_norm != '';

CREATE TABLE IF NOT EXISTS contact_person_keys (
    contact_id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    person_norm TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contact_person_keys_norm
    ON contact_person_keys(person_norm)
    WHERE person_norm != '';
CREATE INDEX IF NOT EXISTS idx_contact_person_keys_company
    ON contact_person_keys(company_id);

CREATE INDEX IF NOT EXISTS idx_contacts_company_id
    ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_last_first_nocase
    ON contacts(last_name COLLATE NOCASE, first_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_contacts_external_record_no
    ON contacts(external_record_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_zoominfo_id
    ON companies(zoominfo_company_id) WHERE TRIM(zoominfo_company_id) != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_zoominfo_id
    ON contacts(zoominfo_contact_id) WHERE TRIM(zoominfo_contact_id) != '';

-- Derived NANP keys for Add Contact phone matching. Display phones stay on contacts.
CREATE TABLE IF NOT EXISTS contact_phone_keys (
    contact_id INTEGER NOT NULL,
    slot TEXT NOT NULL CHECK (slot IN ('phone', 'alt_phone')),
    nanp10 TEXT NOT NULL DEFAULT '',
    last7 TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (contact_id, slot),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contact_phone_keys_nanp10
    ON contact_phone_keys(nanp10);
CREATE INDEX IF NOT EXISTS idx_contact_phone_keys_last7
    ON contact_phone_keys(last7);
CREATE TRIGGER IF NOT EXISTS trg_contact_phone_keys_ai
AFTER INSERT ON contacts
BEGIN
    INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
    SELECT NEW.id, 'phone',
           northstar_phone_nanp10(NEW.phone),
           northstar_phone_last7(NEW.phone)
    WHERE northstar_phone_nanp10(NEW.phone) != ''
       OR northstar_phone_last7(NEW.phone) != '';
    INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
    SELECT NEW.id, 'alt_phone',
           northstar_phone_nanp10(NEW.alt_phone),
           northstar_phone_last7(NEW.alt_phone)
    WHERE northstar_phone_nanp10(NEW.alt_phone) != ''
       OR northstar_phone_last7(NEW.alt_phone) != '';
END;
CREATE TRIGGER IF NOT EXISTS trg_contact_phone_keys_au
AFTER UPDATE OF phone, alt_phone ON contacts
BEGIN
    DELETE FROM contact_phone_keys WHERE contact_id = NEW.id;
    INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
    SELECT NEW.id, 'phone',
           northstar_phone_nanp10(NEW.phone),
           northstar_phone_last7(NEW.phone)
    WHERE northstar_phone_nanp10(NEW.phone) != ''
       OR northstar_phone_last7(NEW.phone) != '';
    INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
    SELECT NEW.id, 'alt_phone',
           northstar_phone_nanp10(NEW.alt_phone),
           northstar_phone_last7(NEW.alt_phone)
    WHERE northstar_phone_nanp10(NEW.alt_phone) != ''
       OR northstar_phone_last7(NEW.alt_phone) != '';
END;
CREATE INDEX IF NOT EXISTS idx_ccr_client_company
    ON client_company_relationships(client_id, company_id);
CREATE INDEX IF NOT EXISTS idx_ccr_client_record_no
    ON client_company_relationships(client_id, external_record_no);
CREATE INDEX IF NOT EXISTS idx_legacy_notes_company
    ON legacy_notes(company_id);

-- Company-scoped shared LeadMaster note-history events (chronological import).
-- Idempotent on (company_id, event_hash). Visible across linked clients.
CREATE TABLE IF NOT EXISTS company_shared_history_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    external_record_no TEXT NOT NULL DEFAULT '',
    source_company_name TEXT NOT NULL DEFAULT '',
    event_at TEXT NOT NULL DEFAULT '',
    event_sequence TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    attribution TEXT NOT NULL DEFAULT '',
    attribution_evidence TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    note_text TEXT NOT NULL DEFAULT '',
    event_hash TEXT NOT NULL,
    import_batch_id INTEGER,
    imported_at TEXT NOT NULL DEFAULT '',
    imported_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_company_shared_history_idempotency
    ON company_shared_history_events(company_id, event_hash);
CREATE INDEX IF NOT EXISTS idx_company_shared_history_company
    ON company_shared_history_events(company_id, event_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_company_shared_history_record_no
    ON company_shared_history_events(external_record_no);

CREATE TABLE IF NOT EXISTS shared_note_history_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'previewed',
    prospects_filename TEXT NOT NULL DEFAULT '',
    history_filename TEXT NOT NULL DEFAULT '',
    prospects_sha256 TEXT NOT NULL DEFAULT '',
    history_sha256 TEXT NOT NULL DEFAULT '',
    prospects_path TEXT NOT NULL DEFAULT '',
    history_path TEXT NOT NULL DEFAULT '',
    preview_json TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '',
    plan_fingerprint TEXT NOT NULL DEFAULT '',
    staging_cleanup_status TEXT NOT NULL DEFAULT '',
    staging_cleanup_error TEXT NOT NULL DEFAULT '',
    staging_cleanup_at TEXT NOT NULL DEFAULT '',
    created_by_user_id INTEGER,
    created_by_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL DEFAULT '',
    cancelled_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_snh_import_batches_client
    ON shared_note_history_import_batches(client_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_user_client_user
    ON user_client_assignments(user_id);
CREATE INDEX IF NOT EXISTS idx_user_client_client
    ON user_client_assignments(client_id);

-- Opaque staff sessions. Cookie stores the raw token; this table stores SHA-256.
CREATE TABLE IF NOT EXISTS staff_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    csrf_secret TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_staff_sessions_user
    ON staff_sessions(user_id, revoked_at);

CREATE INDEX IF NOT EXISTS idx_staff_sessions_expires
    ON staff_sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_staff_sessions_token_hash
    ON staff_sessions(token_hash);

-- Field-level audit for Carmeco status / notes edits (client-scoped)
CREATE TABLE IF NOT EXISTS field_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_code TEXT NOT NULL DEFAULT 'carmeco',
    external_record_no TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    changed_by TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_record_no
    ON field_audit_log(external_record_no);
CREATE INDEX IF NOT EXISTS idx_audit_changed_at
    ON field_audit_log(changed_at);

-- Client-scoped CRM activities / follow-ups (tied to client_company_relationships)
CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    relationship_id INTEGER NOT NULL,
    external_record_no TEXT NOT NULL,
    user_id INTEGER,
    contact_id INTEGER,
    activity_type TEXT NOT NULL,
    activity_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    follow_up_at TEXT,
    assigned_user TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    follow_up_completed INTEGER NOT NULL DEFAULT 0,
    completion_status TEXT NOT NULL DEFAULT 'open',
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_activities_relationship
    ON activities(relationship_id);
CREATE INDEX IF NOT EXISTS idx_activities_client_record
    ON activities(client_id, external_record_no);
CREATE INDEX IF NOT EXISTS idx_activities_follow_up
    ON activities(follow_up_at);
CREATE INDEX IF NOT EXISTS idx_activities_activity_at
    ON activities(activity_at);
CREATE INDEX IF NOT EXISTS idx_activities_type
    ON activities(activity_type);

-- Client-scoped revenue milestones (history preserved; never overwrite)
CREATE TABLE IF NOT EXISTS revenue_milestones (
    milestone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    relationship_id INTEGER NOT NULL,
    external_record_no TEXT NOT NULL,
    contact_id INTEGER,
    milestone_type TEXT NOT NULL,
    milestone_date TEXT NOT NULL,
    amount REAL,
    reference_number TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source_activity_id INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
    FOREIGN KEY (source_activity_id) REFERENCES activities(activity_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_milestones_client_company
    ON revenue_milestones(client_id, company_id);
CREATE INDEX IF NOT EXISTS idx_milestones_type
    ON revenue_milestones(milestone_type);
CREATE INDEX IF NOT EXISTS idx_milestones_record
    ON revenue_milestones(client_id, external_record_no);
CREATE INDEX IF NOT EXISTS idx_milestones_date
    ON revenue_milestones(milestone_date);
CREATE INDEX IF NOT EXISTS idx_ccr_is_hot
    ON client_company_relationships(is_hot);

-- Scheduled work queue (Action Type / Due Date / Completion) — not inferred from status
CREATE TABLE IF NOT EXISTS work_queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    relationship_id INTEGER NOT NULL,
    contact_id INTEGER,
    action_type TEXT NOT NULL,
    due_date TEXT NOT NULL,
    due_time TEXT NOT NULL DEFAULT '',
    assigned_user_id INTEGER,
    assigned_user TEXT NOT NULL DEFAULT '',
    completion_status TEXT NOT NULL DEFAULT 'open',
    priority TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    source_activity_id INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
    FOREIGN KEY (assigned_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (source_activity_id) REFERENCES activities(activity_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_work_queue_client_due
    ON work_queue_items(client_id, due_date, completion_status);
CREATE INDEX IF NOT EXISTS idx_work_queue_action
    ON work_queue_items(action_type, completion_status);
CREATE INDEX IF NOT EXISTS idx_work_queue_company
    ON work_queue_items(company_id);

-- Cross-client opportunity dismissals (per target client; does not delete history)
CREATE TABLE IF NOT EXISTS opportunity_dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    dismissed_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (target_client_id, company_id),
    FOREIGN KEY (target_client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS opportunity_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    relationship_id INTEGER NOT NULL,
    originated_from TEXT NOT NULL DEFAULT 'Cross-Client Opportunity',
    opportunity_score INTEGER NOT NULL DEFAULT 0,
    source_summary TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (target_client_id, company_id),
    FOREIGN KEY (target_client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE
);

-- Per-target-client review state (does not modify source-client data)
CREATE TABLE IF NOT EXISTS opportunity_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    reviewed_by TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (target_client_id, company_id),
    FOREIGN KEY (target_client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_opp_dismiss_target
    ON opportunity_dismissals(target_client_id);
CREATE INDEX IF NOT EXISTS idx_opp_assign_target
    ON opportunity_assignments(target_client_id);
CREATE INDEX IF NOT EXISTS idx_opp_review_target
    ON opportunity_reviews(target_client_id);

-- Ask NorthStar per-user question history (Phase 1)
CREATE TABLE IF NOT EXISTS ask_northstar_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'all',
    active_client_id INTEGER,
    intent TEXT NOT NULL DEFAULT '',
    answer_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ask_history_user_created
    ON ask_northstar_history(user_id, created_at DESC);

-- ============================================================
-- Research Company (Phase 2A) — public web research + proposed updates
-- ============================================================

-- Client ICP / target profile used for FIT FOR [CLIENT] analysis
CREATE TABLE IF NOT EXISTS client_target_profiles (
    client_id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    target_industries TEXT NOT NULL DEFAULT '',
    target_capabilities TEXT NOT NULL DEFAULT '',
    target_products TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    primary_service TEXT NOT NULL DEFAULT '',
    secondary_services TEXT NOT NULL DEFAULT '',
    ideal_customer_types TEXT NOT NULL DEFAULT '',
    manufacturing_processes_sought TEXT NOT NULL DEFAULT '',
    production_preference TEXT NOT NULL DEFAULT '',
    stamping_capability TEXT NOT NULL DEFAULT '',
    tooling_notes TEXT NOT NULL DEFAULT '',
    geographic_preferences TEXT NOT NULL DEFAULT '',
    company_size_preferences TEXT NOT NULL DEFAULT '',
    positive_fit_signals TEXT NOT NULL DEFAULT '',
    negative_fit_signals TEXT NOT NULL DEFAULT '',
    fit_weighting_notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- One research run for a company evaluated for a Working For client
CREATE TABLE IF NOT EXISTS company_research_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    working_for_client_id INTEGER NOT NULL,
    initiated_by_user_id INTEGER,
    initiated_by_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed',
    summary TEXT NOT NULL DEFAULT '',
    providers_used TEXT NOT NULL DEFAULT '',
    sources_checked TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    campaign_id INTEGER,
    research_depth TEXT NOT NULL DEFAULT 'quick',
    openai_response_id TEXT NOT NULL DEFAULT '',
    usage_json TEXT NOT NULL DEFAULT '',
    citations_json TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (working_for_client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (initiated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_research_runs_company
    ON company_research_runs(company_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_runs_client
    ON company_research_runs(working_for_client_id, completed_at DESC);

-- Shared master-company intelligence (facts, not client fit)
CREATE TABLE IF NOT EXISTS company_intelligence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    field_key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    finding_type TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    last_verified_at TEXT,
    research_run_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, field_key),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (research_run_id) REFERENCES company_research_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_company_intel_company
    ON company_intelligence(company_id);

-- Individual research findings with full provenance
CREATE TABLE IF NOT EXISTS company_research_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    research_run_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    finding_type TEXT NOT NULL,
    field_key TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    researched_at TEXT NOT NULL DEFAULT (datetime('now')),
    confidence TEXT NOT NULL DEFAULT 'medium',
    evidence_level TEXT NOT NULL DEFAULT 'verified',
    page_title TEXT NOT NULL DEFAULT '',
    is_public_contact INTEGER NOT NULL DEFAULT 0,
    contact_name TEXT NOT NULL DEFAULT '',
    contact_title TEXT NOT NULL DEFAULT '',
    provider_id TEXT NOT NULL DEFAULT 'public_web',
    FOREIGN KEY (research_run_id) REFERENCES company_research_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_findings_run
    ON company_research_findings(research_run_id);

-- Proposed CRM master-field updates (human approval required)
CREATE TABLE IF NOT EXISTS research_proposed_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    research_run_id INTEGER,
    field_key TEXT NOT NULL,
    current_value TEXT NOT NULL DEFAULT '',
    proposed_value TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    research_date TEXT NOT NULL DEFAULT (datetime('now')),
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'Pending Review',
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (research_run_id) REFERENCES company_research_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_proposed_updates_company_status
    ON research_proposed_updates(company_id, status);

-- Audit trail for approved research writebacks to master company
CREATE TABLE IF NOT EXISTS research_update_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_update_id INTEGER,
    company_id INTEGER NOT NULL,
    field_key TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (proposed_update_id) REFERENCES research_proposed_updates(id) ON DELETE SET NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

-- Client-scoped fit analysis (separate from shared company facts)
CREATE TABLE IF NOT EXISTS company_client_fit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    research_run_id INTEGER,
    fit_result TEXT NOT NULL DEFAULT 'Insufficient Information',
    why TEXT NOT NULL DEFAULT '',
    supporting_evidence TEXT NOT NULL DEFAULT '',
    potential_opportunity TEXT NOT NULL DEFAULT '',
    concerns TEXT NOT NULL DEFAULT '',
    missing_information TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, client_id),
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (research_run_id) REFERENCES company_research_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_client_fit_company
    ON company_client_fit(company_id, client_id);

-- Deep Research durable jobs (OpenAI Responses + web_search). Quick Research
-- continues to use company_research_runs alone with research_depth='quick'.
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
);

CREATE INDEX IF NOT EXISTS idx_research_jobs_active
    ON company_research_jobs(company_id, working_for_client_id, status);
CREATE INDEX IF NOT EXISTS idx_research_jobs_company
    ON company_research_jobs(company_id, created_at DESC);

-- ============================================================
-- Client Setup / Target Profiles / Campaigns
-- Source of truth for Research Fit, Ask NorthStar, Cross-Client,
-- and future prospect-list imports (client + campaign context).
-- ============================================================

-- Client-level overview + what the client sells (not campaign-specific)
CREATE TABLE IF NOT EXISTS client_profiles (
    client_id INTEGER PRIMARY KEY,
    website TEXT NOT NULL DEFAULT '',
    main_location TEXT NOT NULL DEFAULT '',
    main_phone TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    primary_owner_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    primary_service TEXT NOT NULL DEFAULT '',
    secondary_services TEXT NOT NULL DEFAULT '',
    products_services TEXT NOT NULL DEFAULT '',
    differentiators TEXT NOT NULL DEFAULT '',
    certifications TEXT NOT NULL DEFAULT '',
    equipment_capacity TEXT NOT NULL DEFAULT '',
    materials TEXT NOT NULL DEFAULT '',
    value_proposition TEXT NOT NULL DEFAULT '',
    default_campaign_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- Multiple target profiles / campaigns per client
CREATE TABLE IF NOT EXISTS client_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    campaign_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    primary_service TEXT NOT NULL DEFAULT '',
    secondary_services TEXT NOT NULL DEFAULT '',
    target_industries TEXT NOT NULL DEFAULT '',
    target_customer_types TEXT NOT NULL DEFAULT '',
    target_products TEXT NOT NULL DEFAULT '',
    manufacturing_processes_sought TEXT NOT NULL DEFAULT '',
    production_preference TEXT NOT NULL DEFAULT '',
    stamping_capability TEXT NOT NULL DEFAULT '',
    tooling_notes TEXT NOT NULL DEFAULT '',
    geographic_preferences TEXT NOT NULL DEFAULT '',
    geography_mode TEXT NOT NULL DEFAULT '',
    geography_required INTEGER NOT NULL DEFAULT 0,
    company_size_preferences TEXT NOT NULL DEFAULT '',
    positive_signals TEXT NOT NULL DEFAULT '',
    negative_signals TEXT NOT NULL DEFAULT '',
    exclusions TEXT NOT NULL DEFAULT '',
    target_titles TEXT NOT NULL DEFAULT '',
    fit_weighting_notes TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    -- Future prospect-list import metadata (retained when lists are imported)
    list_source TEXT NOT NULL DEFAULT '',
    import_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Active',
    owner_user_id INTEGER,
    owner_name TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_client_campaigns_client
    ON client_campaigns(client_id, is_active, is_default);

CREATE TABLE IF NOT EXISTS campaign_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    relationship_id INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (campaign_id, company_id),
    FOREIGN KEY (campaign_id) REFERENCES client_campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_campaign_companies_client
    ON campaign_companies(client_id, campaign_id);

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (campaign_id, contact_id),
    FOREIGN KEY (campaign_id) REFERENCES client_campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_campaign_contacts_client
    ON campaign_contacts(client_id, campaign_id);

-- Audit history for client profile + campaign edits
CREATE TABLE IF NOT EXISTS client_setup_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    campaign_id INTEGER,
    entity_type TEXT NOT NULL DEFAULT 'client_profile',
    field_name TEXT NOT NULL,
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    changed_by_user_id INTEGER,
    changed_by_name TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (campaign_id) REFERENCES client_campaigns(id) ON DELETE SET NULL,
    FOREIGN KEY (changed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_client_setup_audit_client
    ON client_setup_audit(client_id, changed_at DESC);

-- Client onboarding wizard drafts (administrator). Blank draft fields never erase live setup.
CREATE TABLE IF NOT EXISTS client_onboarding_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    mode TEXT NOT NULL DEFAULT 'new',
    status TEXT NOT NULL DEFAULT 'draft',
    current_step INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL DEFAULT '{}',
    template_id TEXT NOT NULL DEFAULT '',
    copy_source_client_id INTEGER,
    completion_percent INTEGER NOT NULL DEFAULT 0,
    created_by_user_id INTEGER,
    updated_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL,
    FOREIGN KEY (copy_source_client_id) REFERENCES clients(id) ON DELETE SET NULL,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_onboarding_drafts_client
    ON client_onboarding_drafts(client_id, status, updated_at DESC);

-- Relationship status labels seeded by onboarding (not CRM relationship rows).
CREATE TABLE IF NOT EXISTS client_status_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    status_label TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (client_id, status_label),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_client_status_catalog_client
    ON client_status_catalog(client_id, sort_order, status_label);


-- Controlled Next Action choices. client_id = 0 is the shared default catalog.
-- A client may later insert its own rows to override the defaults.
CREATE TABLE IF NOT EXISTS client_next_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL DEFAULT 0,
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    requires_detail INTEGER NOT NULL DEFAULT 0,
    aliases TEXT NOT NULL DEFAULT '[]',
    UNIQUE (client_id, code)
);

CREATE INDEX IF NOT EXISTS idx_client_next_actions_client
    ON client_next_actions(client_id, sort_order, id);

-- Live CRM appointments created from Log Call / Appointment Set (not imported events).
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    relationship_id INTEGER NOT NULL,
    contact_id INTEGER,
    activity_id INTEGER,
    appointment_date TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'America/Chicago',
    datetime_tbd INTEGER NOT NULL DEFAULT 0,
    appointment_type TEXT NOT NULL DEFAULT 'Phone',
    location_or_link TEXT NOT NULL DEFAULT '',
    revenue_specialist_user_id INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'Phone Call',
    status TEXT NOT NULL DEFAULT 'scheduled',
    grade TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    follow_up_notes TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled_at TEXT,
    completed_at TEXT,
    cancellation_reason TEXT NOT NULL DEFAULT '',
    cancelled_by TEXT NOT NULL DEFAULT '',
    completed_by TEXT NOT NULL DEFAULT '',
    cancel_activity_id INTEGER,
    complete_activity_id INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES client_company_relationships(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
    FOREIGN KEY (activity_id) REFERENCES activities(activity_id) ON DELETE SET NULL,
    FOREIGN KEY (revenue_specialist_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_appointments_client_status_date
    ON appointments(client_id, status, appointment_date, start_time, id);
CREATE INDEX IF NOT EXISTS idx_appointments_company
    ON appointments(client_id, company_id);
CREATE INDEX IF NOT EXISTS idx_appointments_contact
    ON appointments(client_id, contact_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_idempotency
    ON appointments(client_id, idempotency_key)
    WHERE TRIM(idempotency_key) != '';

-- Administrator company/contact import staging (Checkpoint B). Preview only.
CREATE TABLE IF NOT EXISTS crm_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    uploaded_by_user_id INTEGER,
    uploaded_by_name TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT '',
    worksheet_name TEXT NOT NULL DEFAULT '',
    file_size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'previewed',
    error_message TEXT NOT NULL DEFAULT '',
    headers_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    total_rows INTEGER NOT NULL DEFAULT 0,
    source_row_count INTEGER NOT NULL DEFAULT 0,
    blank_row_count INTEGER NOT NULL DEFAULT 0,
    error_row_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    cancelled_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    mapping_json TEXT NOT NULL DEFAULT '{}',
    mapping_updated_at TEXT NOT NULL DEFAULT '',
    mapping_updated_by_user_id INTEGER,
    imported_at TEXT NOT NULL DEFAULT '',
    imported_by_user_id INTEGER,
    confirmed_plan_fingerprint TEXT NOT NULL DEFAULT '',
    created_company_count INTEGER NOT NULL DEFAULT 0,
    reused_company_count INTEGER NOT NULL DEFAULT 0,
    created_contact_count INTEGER NOT NULL DEFAULT 0,
    reused_contact_count INTEGER NOT NULL DEFAULT 0,
    created_relationship_count INTEGER NOT NULL DEFAULT 0,
    existing_relationship_count INTEGER NOT NULL DEFAULT 0,
    no_contact_row_count INTEGER NOT NULL DEFAULT 0,
    total_imported_row_count INTEGER NOT NULL DEFAULT 0,
    imported_status_count INTEGER NOT NULL DEFAULT 0,
    default_status_count INTEGER NOT NULL DEFAULT 0,
    preserved_status_count INTEGER NOT NULL DEFAULT 0,
    notes_set_count INTEGER NOT NULL DEFAULT 0,
    notes_appended_count INTEGER NOT NULL DEFAULT 0,
    notes_duplicate_count INTEGER NOT NULL DEFAULT 0,
    notes_unchanged_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (uploaded_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (mapping_updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (imported_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS crm_import_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    source_row_number INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]',
    is_blank INTEGER NOT NULL DEFAULT 0,
    has_blocking_error INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (batch_id) REFERENCES crm_import_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_crm_import_rows_batch
    ON crm_import_rows(batch_id, source_row_number);
CREATE INDEX IF NOT EXISTS idx_crm_import_batches_client
    ON crm_import_batches(client_id, status, expires_at);

-- Row-level reconciliation for fully confirmed CRM imports (Checkpoint C3).
-- Stores resolved IDs only; never stores raw CSV / mapped cell content.
CREATE TABLE IF NOT EXISTS crm_import_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    source_row_number INTEGER NOT NULL,
    staged_row_id INTEGER NOT NULL,
    company_action TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    contact_action TEXT NOT NULL,
    contact_id INTEGER,
    relationship_action TEXT NOT NULL,
    relationship_id INTEGER NOT NULL,
    status_action TEXT NOT NULL DEFAULT '',
    notes_action TEXT NOT NULL DEFAULT '',
    previous_status TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (batch_id) REFERENCES crm_import_batches(id) ON DELETE CASCADE,
    UNIQUE (batch_id, staged_row_id)
);

CREATE INDEX IF NOT EXISTS idx_crm_import_results_batch_source
    ON crm_import_results(batch_id, source_row_number);
CREATE INDEX IF NOT EXISTS idx_crm_import_results_batch
    ON crm_import_results(batch_id);

-- Client Data Import: orchestrates CRM prospects staging + optional history CSV.
CREATE TABLE IF NOT EXISTS client_data_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    crm_batch_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'previewed',
    history_original_filename TEXT NOT NULL DEFAULT '',
    history_sha256 TEXT NOT NULL DEFAULT '',
    history_file_size_bytes INTEGER NOT NULL DEFAULT 0,
    history_staging_path TEXT NOT NULL DEFAULT '',
    history_headers_json TEXT NOT NULL DEFAULT '[]',
    history_mapping_json TEXT NOT NULL DEFAULT '{}',
    history_row_count INTEGER NOT NULL DEFAULT 0,
    plan_fingerprint TEXT NOT NULL DEFAULT '',
    preview_json TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '',
    staging_cleanup_status TEXT NOT NULL DEFAULT '',
    staging_cleanup_error TEXT NOT NULL DEFAULT '',
    staging_cleanup_at TEXT NOT NULL DEFAULT '',
    uploaded_by_user_id INTEGER,
    uploaded_by_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT NOT NULL DEFAULT '',
    confirmed_by_user_id INTEGER,
    closed_excluded_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (crm_batch_id) REFERENCES crm_import_batches(id) ON DELETE CASCADE,
    FOREIGN KEY (uploaded_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (confirmed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_client_data_import_batches_client
    ON client_data_import_batches(client_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_client_data_import_batches_crm
    ON client_data_import_batches(crm_batch_id);

-- Full-text search index for companies, contacts, legacy notes, and activities
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    doc_type,
    client_id UNINDEXED,
    company_id UNINDEXED,
    external_record_no UNINDEXED,
    contact_id UNINDEXED,
    source_table UNINDEXED,
    source_id UNINDEXED,
    company_name UNINDEXED,
    client_name UNINDEXED,
    client_code UNINDEXED,
    contact_name UNINDEXED,
    note_type UNINDEXED,
    created_by UNINDEXED,
    event_at UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
