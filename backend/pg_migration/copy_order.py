"""FK-safe table copy order for SQLite → PostgreSQL migration."""

from __future__ import annotations

# Parents before children. FTS virtual tables are excluded (replaced, not copied).
# Tables created only by ensure_* (not in schema.sql) are listed after core CRM.
MIGRATION_TABLE_ORDER: tuple[str, ...] = (
    "clients",
    "users",
    "companies",
    "user_client_assignments",
    "contacts",
    "client_company_relationships",
    "company_identity_keys",
    "contact_person_keys",
    "contact_phone_keys",
    "contact_client_workflows",
    "contact_client_relationships",
    "legacy_notes",
    "company_shared_history_events",
    "shared_note_history_import_batches",
    "staff_sessions",
    "field_audit_log",
    "activities",
    "revenue_milestones",
    "work_queue_items",
    "opportunity_dismissals",
    "opportunity_assignments",
    "opportunity_reviews",
    "ask_northstar_history",
    "client_target_profiles",
    "company_research_runs",
    "company_intelligence",
    "company_research_findings",
    "research_proposed_updates",
    "research_update_audit",
    "company_client_fit",
    "company_research_jobs",
    "client_profiles",
    "client_campaigns",
    "campaign_companies",
    "campaign_contacts",
    "client_setup_audit",
    "client_onboarding_drafts",
    "client_status_catalog",
    "client_next_actions",
    "appointments",
    "crm_import_batches",
    "crm_import_rows",
    "crm_import_results",
    "client_data_import_batches",
    # Runtime-ensure tables (may be absent from older fixtures)
    "campaign_unassigned",
    "client_contacts",
    "client_documents",
    "client_extraction_runs",
    "client_extraction_items",
    "client_email_templates",
    "client_appointment_types",
    "client_appointment_outcomes",
    "client_email_accounts",
    "client_email_signatures",
    "email_oauth_states",
    "email_oauth_credentials",
    "client_engagement_import_batches",
    "client_engagement_import_rows",
    "client_sales_events",
    "crm_import_row_resolutions",
)

# Tables that must never be bulk-copied as-is (SQLite-only / rebuilt).
EXCLUDED_FROM_ROW_COPY: frozenset[str] = frozenset(
    {
        "search_fts",
        "search_fts_data",
        "search_fts_idx",
        "search_fts_content",
        "search_fts_docsize",
        "search_fts_config",
        "sqlite_sequence",
    }
)

# INTEGER PK column name overrides (not always "id").
PRIMARY_KEY_COLUMNS: dict[str, str] = {
    "activities": "activity_id",
    "revenue_milestones": "milestone_id",
}


def table_copy_order(present: set[str] | frozenset[str] | None = None) -> list[str]:
    """Return dependency order filtered to tables that exist in the source."""
    if present is None:
        return [t for t in MIGRATION_TABLE_ORDER if t not in EXCLUDED_FROM_ROW_COPY]
    return [
        t
        for t in MIGRATION_TABLE_ORDER
        if t in present and t not in EXCLUDED_FROM_ROW_COPY
    ]


def primary_key_column(table: str) -> str:
    return PRIMARY_KEY_COLUMNS.get(table, "id")
