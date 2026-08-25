"""Backward-compatible re-exports — prefer client_workspace_data."""

from client_workspace_data import (  # noqa: F401
    db_exists,
    get_active_client,
    get_company_by_record_no,
    get_company_workspace,
    list_carmeco_statuses,
    list_contacts,
    list_prospects,
    list_relationship_statuses,
    update_carmeco_notes,
    update_carmeco_status,
    update_relationship_notes,
    update_relationship_status,
)
