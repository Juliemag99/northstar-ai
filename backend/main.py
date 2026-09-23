import os

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from access import (
    WriteClientIdError,
    dashboard_scope_summary,
    get_user_by_id,
    list_clients_for_user,
)
from staff_context import resolve_staff_actor
from staff_rbac import user_has_permission
from auth_http import (
    add_staff_csrf_middleware,
    auth_enforcement_active,
    require_administrator,
    require_authenticated_staff,
    require_client_access,
    require_client_setup_editor,
    staff_auth_router,
)
from activities_data import (
    create_activity,
    list_activities_due_today,
    list_activities_for_company,
    list_client_activities,
    list_overdue_follow_ups,
    list_upcoming_follow_ups,
)
from outreach_data import list_priority_prospects, log_outreach, next_priority_prospect
from crm_add_data import confirm_company_contact_add, preview_company_contact_add
from carmeco_data import (
    get_active_client,
    get_company_by_record_no,
    get_company_workspace,
    list_contacts,
    list_prospects_page,
    list_relationship_statuses,
    update_relationship_notes,
    update_relationship_status,
)
from db import ensure_schema, get_connection
from milestones_data import (
    create_milestone,
    list_milestones_for_company,
    list_northstar_client_history,
    milestone_summary,
    set_company_hot,
)
from shared_history_data import list_shared_history
from models import (
    ActiveClient,
    Activity,
    ActivityCreateRequest,
    ActivityListResponse,
    ActivityTimelineResponse,
    AskHistoryResponse,
    AskNorthStarRequest,
    AskNorthStarResponse,
    ClientAppointmentImportConfirmRequest,
    ClientAppointmentImportMapRequest,
    ClientAppointmentImportPreview,
    ClientAppointmentImportBatchView,
    ClientCampaignUpdate,
    ClientContactCreate,
    ClientContactSplitCreateRequest,
    ClientContactSplitPreview,
    ClientContactUpdate,
    ClientContactView,
    ClientDocumentProcessResult,
    ContactsResponse,
    ContactActivityCreate,
    ContactAssignRequest,
    ContactAssignResult,
    ContactPhoneUpdate,
    CompanyLookupResponse,
    ManualContactPreviewRequest,
    ManualContactPreviewResponse,
    ManualContactSaveRequest,
    ManualContactSaveResult,
    ManualCompanyPreviewRequest,
    ManualCompanyPreviewResponse,
    ManualCompanySaveRequest,
    ManualCompanySaveResult,
    ZoomInfoAddPreviewRequest,
    ZoomInfoAddPreviewResponse,
    ZoomInfoAddResult,
    ZoomInfoAddSaveRequest,
    ZoomInfoContactApplyRequest,
    ZoomInfoContactApplyResult,
    ZoomInfoContactPreviewRequest,
    ZoomInfoContactPreviewResponse,
    ContactFollowUpCompleteRequest,
    ContactFollowUpRescheduleRequest,
    ContactWorkflowResult,
    ContactWorkflowUpdate,
    ContactWorkspace,
    AppointmentActionResult,
    AppointmentCancelRequest,
    AppointmentCompleteRequest,
    AppointmentListResponse,
    AppointmentRescheduleRequest,
    AppointmentSummary,
    EngagementImportBatchView,
    EngagementImportClassifyRequest,
    EngagementImportConfirmRequest,
    EngagementImportMapRequest,
    EngagementImportPreview,
    SalesEventView,
    ClientDocumentView,
    ClientEmailAccountAssignmentUpdate,
    ClientEmailAccountAssignmentView,
    ClientEmailAccountSuggestion,
    ClientEmailAccountUpdate,
    ClientEmailAccountView,
    ClientEmailPreviewRequest,
    ClientEmailPreviewResult,
    EmailPreviewAppointmentOption,
    ClientEmailSendRequest,
    ClientEmailSendResult,
    ClientEmailSignatureUpdate,
    ClientEmailSignatureView,
    ClientEmailTemplateUpdate,
    CrmImportBatchView,
    CrmImportDryRunRequest,
    CrmImportDryRunResponse,
    CrmImportConfirmRequest,
    CrmImportConfirmResponse,
    CrmImportHistoryPage,
    CrmImportMappingRequest,
    CrmImportMatchResolutionRequest,
    CrmImportMatchResolutionResponse,
    CrmImportRowsPage,
    CrmImportSourceTypeRequest,
    CrmImportStatusResolutionRequest,
    CrmImportStatusResolutionResponse,
    CrmImportUploadResult,
    ClientDataImportBatchView,
    ClientDataImportConfirmRequest,
    ClientDataImportConfirmResponse,
    ClientDataImportDryRunRequest,
    ClientDataImportDryRunResponse,
    ClientDataImportMappingRequest,
    ClientDataImportUploadResult,
    ClientEmailTemplateView,
    ClientExtractionBulkRejectRequest,
    ClientExtractionBulkResolveRequest,
    ClientExtractionBulkReviewRequest,
    ClientExtractionProposalUpdate,
    ClientExtractionProposalView,
    ClientExtractionResolveRequest,
    ContactEnrichmentProposalCreate,
    ProposalIncorporationAnalysis,
    ClientKnowledgeFieldUpdate,
    ClientKnowledgeHubResponse,
    ClientKnowledgeSectionUpdate,
    ClientKnowledgeSectionView,
    EmailTemplateGroupApproveRequest,
    EmailTemplateGroupPreview,
    ClientListSetupItem,
    ClientOverviewUpdate,
    ClientSetupResponse,
    ClientSellsUpdate,
    ClientStatusList,
    NextActionCatalog,
    ClientMilestoneHistory,
    CompanyWorkspace,
    CrossClientOpportunityList,
    DashboardScopeSummary,
    DashboardFollowUpsResponse,
    ClientRepsResponse,
    FollowUpTaskActionResult,
    FollowUpTaskCompleteRequest,
    FollowUpTaskRescheduleRequest,
    FieldUpdateResponse,
    MilestoneCreateRequest,
    MilestoneSummary,
    NotesUpdateRequest,
    OpportunityActionResult,
    OpportunityAddRequest,
    OpportunityDismissRequest,
    OpportunityReviewRequest,
    ProspectsResponse,
    ResearchApproveRequest,
    ResearchCompanyResponse,
    ResearchRejectRequest,
    ResearchStartRequest,
    RevenueMilestone,
    SearchResponse,
    SetHotRequest,
    SharedHistoryResponse,
    SharedNoteHistoryConfirmResponse,
    SharedNoteHistoryImportBatchView,
    SharedNoteHistoryUploadResult,
    StatusUpdateRequest,
    UserClientsResponse,
    WorkQueueCompleteRequest,
    WorkQueueListResponse,
    WorkQueueLogCallRequest,
    WorkQueueLogCallResult,
    WorkQueueNextResponse,
    OutreachLogRequest,
    OutreachLogResult,
    PriorityProspectsResponse,
    CrmAddPreviewRequest,
    CrmAddPreviewResponse,
    CrmAddConfirmRequest,
    CrmAddConfirmResult,
    WorkQueueSummary,
    CampaignActionResult,
    CampaignCreateRequest,
    CampaignListResponse,
    CampaignMemberAddRequest,
    CampaignMemberSearchResponse,
    CampaignRouteConfirmRequest,
    CampaignRouteDeferRequest,
    CampaignRouteSuggestion,
    CampaignSummary,
    CampaignUpdateRequest,
    ReportCampaignResponse,
    ReportClientResultsResponse,
    ReportFiltersResponse,
    ReportRecordsResponse,
    ReportTeamResponse,
    CampaignWorkspace,
    UnassignedBulkAssignRequest,
    UnassignedBulkAssignResult,
    UnassignedOpportunityList,
)
from opportunities_data import (
    add_opportunity_to_target,
    dismiss_opportunity,
    list_cross_client_opportunities,
    mark_opportunity_reviewed,
    opportunity_count_for_dashboard,
    workspace_cross_client_banner,
)
from client_setup_data import (
    create_campaign,
    delete_campaign,
    get_client_setup,
    list_client_setup_summaries,
    set_default_campaign,
    update_campaign,
    update_client_overview,
    update_client_sells,
)
from client_engagement_import import (
    confirm_engagement_import,
    get_engagement_import_batch,
    get_engagement_import_preview,
    list_sales_events,
    map_engagement_import,
    start_engagement_import,
    update_sheet_classifications,
)
from manual_contact_data import (
    lookup_companies_for_client,
    preview_manual_contact,
    save_manual_contact,
    update_contact_phones,
)
from manual_company_data import preview_manual_company, save_manual_company
from zoominfo_crm_data import (
    apply_zoominfo_contact_update,
    preview_zoominfo_add,
    preview_zoominfo_contact_update,
    save_zoominfo_add,
)
from client_workspace_data import (
    assign_shared_contact,
    complete_contact_follow_up,
    create_contact_activity,
    get_contact_workspace,
    list_client_reps,
    reschedule_contact_follow_up,
    update_contact_workflow,
)
from appointments_data import (
    appointment_summary,
    cancel_appointment,
    complete_appointment,
    list_appointments,
    reschedule_appointment,
)
from client_contacts_data import (
    analyze_proposal_incorporation,
    create_client_contact,
    create_contact_enrichment_proposal,
    create_split_contact_proposals,
    deactivate_client_contact,
    ensure_title_enrichment_proposals_from_source,
    list_client_contacts,
    preview_split_contact_proposal,
    update_client_contact,
)
from client_knowledge_data import (
    archive_document,
    bulk_review_extraction_proposals,
    confirm_appointment_import,
    ensure_client_knowledge_schema,
    get_appointment_import_batch,
    get_client_knowledge_hub,
    get_document_file,
    get_knowledge_section_catalog,
    list_documents,
    list_email_templates,
    list_extraction_proposals,
    map_appointment_import_columns,
    preview_email_template_group,
    process_document,
    approve_email_template_group,
    resolve_extraction_proposal,
    review_extraction_proposal,
    start_appointment_import,
    update_knowledge_field,
    upload_document,
    upsert_email_template,
    upsert_knowledge_section,
)
from client_email_accounts_data import (
    connect_email_account_stub,
    deactivate_email_account,
    deactivate_email_signature,
    list_email_accounts,
    list_email_signatures,
    list_preview_appointments,
    list_preview_contacts,
    preview_client_email,
    suggest_email_account_setup,
    upsert_email_account,
    upsert_email_account_assignment,
    upsert_email_signature,
)
from gmail_oauth import (
    begin_google_connect,
    complete_google_callback,
    disconnect_google_account,
    oauth_status,
)
from crm_import_staging import (
    BatchNotReusable,
    CLIENT_REQUIRED,
    MAX_FILE_BYTES,
    cancel_crm_import,
    get_crm_import_batch,
    list_crm_import_batches,
    list_crm_import_rows,
    save_crm_import_mapping,
    save_crm_import_source_type,
    upload_crm_import,
)
from crm_import_plan import dry_run_crm_import
from crm_identity_keys import IdentityKeysNotReady
from crm_import_confirm import confirm_admin_crm_import_batch
from crm_import_status_resolution import save_crm_import_status_resolution
from crm_import_match_resolution import save_crm_import_match_resolution_http
from research_import import confirm_research_import, dry_run_research_import
from research_import_plan import CLIENT_MISSING, CLIENT_REQUIRED as RESEARCH_CLIENT_REQUIRED
from research_import_staging import (
    BatchNotReusable as ResearchBatchNotReusable,
    save_contact_resolution as save_research_contact_resolution,
    save_mapping as save_research_import_mapping,
    save_master_resolution as save_research_master_resolution,
    save_match_resolution as save_research_match_resolution,
    upload_research_import,
)
from leadmaster_refresh_http import (
    confirm_refresh,
    preview_refresh,
    readiness_refresh,
    refresh_meta,
    upload_refresh,
)
from leadmaster_refresh_staging import (
    RefreshApplyDisabled,
    RefreshLiveWriteError,
    refresh_batch_view,
    save_refresh_mapping,
    save_refresh_policy,
    save_refresh_resolutions,
)
from shared_note_history_import import (
    MAX_FILE_BYTES as SNH_MAX_FILE_BYTES,
    confirm_shared_note_history_import,
    get_shared_note_history_batch,
    upload_shared_note_history_import,
)
from client_data_import import (
    cancel_client_data_import,
    confirm_client_data_import,
    dry_run_client_data_import,
    get_client_data_import_batch,
    retry_client_data_import_staging_cleanup,
    save_client_data_import_mapping,
    upload_client_data_import,
)
from gmail_send import send_client_email
from fastapi.responses import RedirectResponse
from extraction_review_bulk import (
    bulk_reject_proposals,
    bulk_resolve_eligible_proposals,
    classify_pending_proposals_for_review,
    preview_bulk_resolve,
)
from ask_northstar_data import ask_northstar, list_ask_history
from research_data import (
    approve_proposed_update,
    get_latest_research,
    get_research_run,
    reject_proposed_update,
    start_company_research,
)
from deep_research_data import (
    cancel_deep_research_job,
    deep_research_status_payload,
    get_deep_research_job,
)
from deep_research_config import deep_research_is_configured
from client_onboarding_data import (
    apply_copy_to_draft,
    apply_template_to_draft,
    create_draft,
    finish_draft,
    get_draft,
    list_drafts,
    onboarding_templates_payload,
    preview_copy_from_client,
    save_draft,
)
from work_queue_data import (
    clients_for_work_queue,
    complete_follow_up_task,
    complete_work_queue_item,
    get_work_queue_next,
    list_dashboard_follow_ups,
    list_work_queue,
    log_work_queue_call,
    reschedule_follow_up_task,
    work_queue_summary,
)
from search_data import search as run_search
from campaigns_data import (
    add_campaign_company,
    add_campaign_contact,
    bulk_assign_unassigned,
    confirm_campaign_route,
    create_operational_campaign,
    defer_campaign_route,
    get_campaign_workspace,
    list_campaigns,
    list_unassigned_opportunities,
    remove_campaign_company,
    remove_campaign_contact,
    set_campaign_status,
    search_campaign_members,
    suggest_campaign_route,
    update_operational_campaign,
)
from reports_data import (
    export_report_csv,
    list_campaign_performance,
    list_client_results,
    list_report_filters,
    list_report_records,
    list_team_performance,
)
from master_data_export import build_master_data_export

def _cors_allow_origins() -> list[str]:
    origins = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ]
    extra = os.environ.get("NORTHSTAR_CORS_ORIGINS", "")
    for part in extra.split(","):
        origin = part.strip()
        if origin and origin not in origins:
            origins.append(origin)
    return origins


app = FastAPI(
    title="NorthStar AI",
    version="1.9.1",
    description="NorthStar AI Revenue Development Platform — multi-client",
)

# CORS is outermost. CSRF then staff-session enforcement wrap routes.
# CSRF stays inside CORS so 403s keep CORS labels.
add_staff_csrf_middleware(app)
app.include_router(staff_auth_router)

if os.environ.get("NORTHSTAR_TRUST_PROXY", "").strip() == "1":
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=["127.0.0.1", "localhost", "::1"],
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(WriteClientIdError)
async def write_client_id_error_handler(_request, exc: WriteClientIdError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(PermissionError)
async def permission_error_handler(_request, exc: PermissionError):
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc) or "Not authorized."},
    )


@app.on_event("startup")
def startup() -> None:
    ensure_schema()


@app.get("/")
def home():
    client = get_active_client()
    return {
        "application": "NorthStar AI",
        "status": "Running",
        "version": "1.9.1",
        "active_client": client.name,
        "companies": client.company_count,
        "contacts": client.contact_count,
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/api/ready")
def ready():
    """Readiness for reverse-proxy checks. No paths, tokens, or user data."""
    database = "error"
    try:
        conn = get_connection()
        try:
            conn.execute("SELECT 1")
            database = "ok"
        finally:
            conn.close()
    except Exception:
        database = "error"
    return {
        "status": "ready" if database == "ok" else "degraded",
        "database": database,
        "auth_enforced": auth_enforcement_active(),
    }


@app.get("/api/client", response_model=ActiveClient)
def active_client(
    client_id: int | None = Query(default=None),
    all_clients: bool = Query(default=False),
):
    """Identify the Active Client (or All My Clients summary)."""
    return get_active_client(client_id=client_id, all_clients=all_clients)


@app.get("/api/prospects", response_model=ProspectsResponse)
def list_prospects_api(
    client_id: int | None = Query(default=None),
    all_clients: bool = Query(default=False),
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    milestone_type: str | None = Query(
        default=None,
        description="Optional: Quote | Purchase Order | WebLead | Appointment Set",
    ),
    assigned_user_id: int | None = Query(
        default=None,
        description="Filter by assigned rep. 0 = unassigned.",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Return a bounded page of prospects for the Active Client (or all assigned)."""
    try:
        page = list_prospects_page(
            client_id=client_id,
            all_clients=all_clients,
            q=q,
            status=status,
            milestone_type=milestone_type,
            assigned_user_id=assigned_user_id,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    client = get_active_client(client_id=client_id, all_clients=all_clients)
    return ProspectsResponse(
        client=client,
        prospects=page["prospects"],
        total=int(page["total"]),
        client_total=int(page["client_total"]),
        offset=int(page["offset"]),
        limit=int(page["limit"]),
        has_previous=bool(page["has_previous"]),
        has_next=bool(page["has_next"]),
    )


@app.get("/api/carmeco/statuses", response_model=ClientStatusList)
def carmeco_statuses(
    client_id: int | None = Query(default=None),
    all_clients: bool = Query(default=False),
):
    """Distinct relationship statuses (legacy path — prefer /api/clients/{id}/statuses)."""
    if client_id is not None and not all_clients:
        client = get_active_client(client_id=client_id)
        statuses = list_relationship_statuses(client_id=client_id)
    else:
        client = get_active_client(all_clients=True)
        statuses = list_relationship_statuses(all_clients=True)
    return ClientStatusList(
        client=client.name,
        client_id=client.client_id,
        statuses=statuses,
    )


@app.get("/api/clients/{client_id}/statuses", response_model=ClientStatusList)
def client_statuses(client_id: int):
    """Distinct relationship status values for the given client."""
    client = get_active_client(client_id=client_id)
    return ClientStatusList(
        client=client.name,
        client_id=client.client_id,
        statuses=list_relationship_statuses(client_id=client_id),
    )


@app.get("/api/clients/{client_id}/next-actions", response_model=NextActionCatalog)
def client_next_actions(client_id: int):
    """Controlled Next Action choices for the Active Client (global defaults if unset)."""
    from next_actions import get_next_action_catalog

    catalog = get_next_action_catalog(client_id=client_id)
    return NextActionCatalog(**catalog)


@app.get("/api/clients/{client_id}/reps", response_model=ClientRepsResponse)
def client_reps(client_id: int):
    """Assigned-rep choices for the Active Client."""
    client = get_active_client(client_id=client_id)
    return ClientRepsResponse(client_id=client.client_id, reps=list_client_reps(client_id))


@app.get("/api/next-actions", response_model=NextActionCatalog)
def global_next_actions(client_id: int | None = Query(default=None)):
    """Next Action catalog for a client, or the shared defaults when client_id is omitted."""
    from next_actions import get_next_action_catalog

    catalog = get_next_action_catalog(client_id=client_id)
    return NextActionCatalog(**catalog)


@app.get("/api/companies/by-record/{record_no}", response_model=CompanyWorkspace)
def company_workspace_by_record(
    record_no: str,
    client_id: int | None = Query(default=None),
    client: str | None = Query(default=None),
):
    """Company Workspace for a client-specific Record No. + client context."""
    workspace = get_company_by_record_no(
        record_no, client_id=client_id, client=client
    )
    if workspace is None:
        raise HTTPException(
            status_code=404,
            detail="Company not found for Record No. (client context may be required).",
        )
    return workspace


@app.patch(
    "/api/companies/by-record/{record_no}/status",
    response_model=FieldUpdateResponse,
)
def patch_company_status(record_no: str, body: StatusUpdateRequest):
    """Save client-company relationship status (not a universal company field)."""
    try:
        result = update_relationship_status(
            record_no,
            client=body.client,
            client_id=body.client_id,
            status=body.status,
            user=body.user,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FieldUpdateResponse(**result)


@app.patch(
    "/api/companies/by-record/{record_no}/notes",
    response_model=FieldUpdateResponse,
)
def patch_company_notes(record_no: str, body: NotesUpdateRequest):
    """Save Sales Rep Comments/Notes for the Active Client relationship."""
    try:
        result = update_relationship_notes(
            record_no,
            client=body.client,
            client_id=body.client_id,
            note_text=body.note_text,
            user=body.user,
            mode=body.mode,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FieldUpdateResponse(**result)


@app.get("/api/companies/lookup", response_model=CompanyLookupResponse)
def lookup_companies_api(
    client_id: int = Query(..., description="Active Client id — required, must be > 0."),
    q: str = Query(default="", description="Company name or Record No."),
):
    """Search companies assigned to the Active Client for Add Contact."""
    try:
        return CompanyLookupResponse(**lookup_companies_for_client(client_id, q))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/companies/{company_id}", response_model=CompanyWorkspace)
def company_workspace(
    company_id: int,
    client_id: int | None = Query(default=None),
    client: str | None = Query(default=None),
):
    """Company Workspace: profile, client status, contacts, legacy notes."""
    workspace = get_company_workspace(
        company_id, client_id=client_id, client=client
    )
    if workspace is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    return workspace


@app.post("/api/activities", response_model=Activity, status_code=201)
def create_activity_api(body: ActivityCreateRequest):
    """Create a client-scoped activity on a client-company relationship."""
    try:
        return create_activity(body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/outreach",
    response_model=OutreachLogResult,
    status_code=201,
)
def log_outreach_api(client_id: int, body: OutreachLogRequest):
    """Log outreach for Working For client. Does not send email or auto-create appointments."""
    try:
        payload = body.model_copy(update={"client_id": client_id})
        return log_outreach(payload)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/priority-prospects",
    response_model=PriorityProspectsResponse,
)
def priority_prospects_api(client_id: int, limit: int = Query(default=50)):
    """Deterministic Today's Priority Prospects queue for the active client."""
    try:
        return list_priority_prospects(client_id, limit=limit)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/priority-prospects/next")
def priority_prospects_next_api(
    client_id: int,
    after: str = Query(..., description="Current external_record_no"),
):
    """Next actionable prospect after the current record (same queue order)."""
    try:
        item = next_priority_prospect(client_id, after_record_no=after)
        if item is None:
            return {"ok": True, "item": None, "message": "No further actionable prospects."}
        return {"ok": True, "item": item}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/crm/add/preview",
    response_model=CrmAddPreviewResponse,
)
def crm_add_preview_api(body: CrmAddPreviewRequest):
    """Read-only company/contact dedupe preview for Add to NorthStar."""
    try:
        return preview_company_contact_add(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/crm/add/confirm",
    response_model=CrmAddConfirmResult,
    status_code=201,
)
def crm_add_confirm_api(body: CrmAddConfirmRequest):
    """Explicit create/link after human review. No automatic field overwrite."""
    try:
        return confirm_company_contact_add(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/campaigns", response_model=CampaignListResponse)
def list_campaigns_api(
    client_id: int = Query(default=0),
    status: str | None = Query(default=None),
    owner: str | None = Query(default=None),
    category: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Operational campaigns for the Active Client (0 = all authorized clients)."""
    try:
        return list_campaigns(
            client_id=client_id,
            status=status or "",
            owner=owner or "",
            category=category or "",
            date_from=date_from or "",
            date_to=date_to or "",
            q=q or "",
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _report_query_params(
    client_id: int,
    date_from: str | None,
    date_to: str | None,
    user_id: int | None,
    campaign_id: int | None,
    activity_type: str | None,
    outcome: str | None,
) -> dict:
    return {
        "client_id": client_id if client_id > 0 else None,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "user_id": user_id if user_id and user_id > 0 else None,
        "campaign_id": campaign_id if campaign_id and campaign_id > 0 else None,
        "activity_type": (activity_type or "").strip(),
        "outcome": (outcome or "").strip(),
    }


@app.get("/api/reports/filters", response_model=ReportFiltersResponse)
def reports_filters_api(client_id: int = Query(default=0)):
    """Filter options for Reports. client_id=0 is All My Clients."""
    try:
        return list_report_filters(client_id=client_id if client_id > 0 else None)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/reports/client-results", response_model=ReportClientResultsResponse)
def reports_client_results_api(
    client_id: int = Query(default=0),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    campaign_id: int | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    sort_by: str = Query(default="client_name"),
    sort_dir: str = Query(default="asc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        return list_client_results(
            **_report_query_params(
                client_id, date_from, date_to, user_id, campaign_id, activity_type, outcome
            ),
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/reports/team-performance", response_model=ReportTeamResponse)
def reports_team_performance_api(
    client_id: int = Query(default=0),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    campaign_id: int | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    sort_by: str = Query(default="user_name"),
    sort_dir: str = Query(default="asc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        return list_team_performance(
            **_report_query_params(
                client_id, date_from, date_to, user_id, campaign_id, activity_type, outcome
            ),
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/reports/campaign-performance", response_model=ReportCampaignResponse)
def reports_campaign_performance_api(
    client_id: int = Query(default=0),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    campaign_id: int | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    sort_by: str = Query(default="campaign_name"),
    sort_dir: str = Query(default="asc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        return list_campaign_performance(
            **_report_query_params(
                client_id, date_from, date_to, user_id, campaign_id, activity_type, outcome
            ),
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/reports/records", response_model=ReportRecordsResponse)
def reports_records_api(
    section: str = Query(default="client-results"),
    metric: str = Query(...),
    client_id: int = Query(default=0),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    campaign_id: int | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    row_client_id: int | None = Query(default=None),
    row_user_id: int | None = Query(default=None),
    row_campaign_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        return list_report_records(
            section=section,
            metric=metric,
            **_report_query_params(
                client_id, date_from, date_to, user_id, campaign_id, activity_type, outcome
            ),
            row_client_id=row_client_id if row_client_id and row_client_id > 0 else None,
            row_user_id=row_user_id if row_user_id and row_user_id > 0 else None,
            row_campaign_id=row_campaign_id if row_campaign_id and row_campaign_id > 0 else None,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/reports/export")
def reports_export_api(
    section: str = Query(default="client-results"),
    metric: str | None = Query(default=None),
    client_id: int = Query(default=0),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    campaign_id: int | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    row_client_id: int | None = Query(default=None),
    row_user_id: int | None = Query(default=None),
    row_campaign_id: int | None = Query(default=None),
    detail: bool = Query(default=False),
):
    try:
        filename, csv_text = export_report_csv(
            section=section,
            metric=metric or "",
            **_report_query_params(
                client_id, date_from, date_to, user_id, campaign_id, activity_type, outcome
            ),
            row_client_id=row_client_id if row_client_id and row_client_id > 0 else None,
            row_user_id=row_user_id if row_user_id and row_user_id > 0 else None,
            row_campaign_id=row_campaign_id if row_campaign_id and row_campaign_id > 0 else None,
            detail=detail,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=csv_text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/campaigns", response_model=CampaignSummary)
def create_campaign_ops_api(body: CampaignCreateRequest):
    try:
        return create_operational_campaign(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/campaigns/route", response_model=CampaignRouteSuggestion)
def suggest_campaign_route_api(
    client_id: int = Query(...),
    company_id: int = Query(...),
    contact_id: int | None = Query(default=None),
    research_run_id: int | None = Query(default=None),
    source: str | None = Query(default=None),
    force: bool = Query(default=False),
):
    try:
        return suggest_campaign_route(
            client_id=client_id,
            company_id=company_id,
            contact_id=contact_id,
            research_run_id=research_run_id,
            source=source or "",
            force=force,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/route/confirm", response_model=CampaignRouteSuggestion)
def confirm_campaign_route_api(body: CampaignRouteConfirmRequest):
    try:
        return confirm_campaign_route(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/route/defer", response_model=CampaignRouteSuggestion)
def defer_campaign_route_api(body: CampaignRouteDeferRequest):
    try:
        return defer_campaign_route(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/campaigns/unassigned", response_model=UnassignedOpportunityList)
def list_unassigned_opportunities_api(
    client_id: int = Query(default=0),
    q: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    try:
        return list_unassigned_opportunities(client_id, q=q, limit=limit, offset=offset)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/campaigns/unassigned/assign", response_model=UnassignedBulkAssignResult)
def bulk_assign_unassigned_api(body: UnassignedBulkAssignRequest):
    try:
        return bulk_assign_unassigned(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/campaigns/{campaign_id}", response_model=CampaignWorkspace)
def get_campaign_workspace_api(campaign_id: int):
    try:
        return get_campaign_workspace(campaign_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/campaigns/{campaign_id}", response_model=CampaignSummary)
def update_campaign_ops_api(campaign_id: int, body: CampaignUpdateRequest):
    try:
        return update_operational_campaign(campaign_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/{campaign_id}/pause", response_model=CampaignActionResult)
def pause_campaign_api(campaign_id: int):
    try:
        return set_campaign_status(campaign_id, "Paused")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/{campaign_id}/resume", response_model=CampaignActionResult)
def resume_campaign_api(campaign_id: int):
    try:
        return set_campaign_status(campaign_id, "Active")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/{campaign_id}/complete", response_model=CampaignActionResult)
def complete_campaign_api(campaign_id: int):
    try:
        return set_campaign_status(campaign_id, "Completed")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/{campaign_id}/archive", response_model=CampaignActionResult)
def archive_campaign_api(campaign_id: int):
    try:
        return set_campaign_status(campaign_id, "Archived")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/{campaign_id}/companies", response_model=CampaignWorkspace)
def add_campaign_company_api(campaign_id: int, body: CampaignMemberAddRequest):
    try:
        return add_campaign_company(campaign_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/campaigns/{campaign_id}/contacts", response_model=CampaignWorkspace)
def add_campaign_contact_api(campaign_id: int, body: CampaignMemberAddRequest):
    try:
        return add_campaign_contact(campaign_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/campaigns/{campaign_id}/companies/{company_id}", response_model=CampaignWorkspace)
def remove_campaign_company_api(campaign_id: int, company_id: int):
    try:
        return remove_campaign_company(campaign_id, company_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/campaigns/{campaign_id}/contacts/{contact_id}", response_model=CampaignWorkspace)
def remove_campaign_contact_api(campaign_id: int, contact_id: int):
    try:
        return remove_campaign_contact(campaign_id, contact_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/campaigns/{campaign_id}/members/search", response_model=CampaignMemberSearchResponse)
def search_campaign_members_api(campaign_id: int, q: str | None = Query(default=None)):
    try:
        return search_campaign_members(campaign_id, q or "")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/activities",
    response_model=ActivityTimelineResponse,
)
def client_activities_timeline(
    client_id: int,
    activity_type: str | None = Query(default=None),
    assigned_user: str | None = Query(default=None),
    user_id: int | None = Query(default=None),
    company_id: int | None = Query(default=None),
    contact_id: int | None = Query(default=None),
    company: str | None = Query(default=None),
    contact: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Active-client activity timeline with server-side search, filters, and pagination."""
    try:
        return list_client_activities(
            client_id,
            activity_type=activity_type or "",
            assigned_user=assigned_user or "",
            user_id=user_id,
            company_id=company_id,
            contact_id=contact_id,
            company=company or "",
            contact=contact or "",
            date_from=date_from or "",
            date_to=date_to or "",
            q=q or "",
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/companies/by-record/{record_no}/activities",
    response_model=ActivityListResponse,
)
def company_activities(
    record_no: str,
    client: str = Query(default="Carmeco"),
    client_id: int | None = Query(default=None),
):
    """Retrieve activities for a company Record No., scoped to the client."""
    activities = list_activities_for_company(
        record_no, client=client, client_id=client_id
    )
    return ActivityListResponse(client=client, count=len(activities), activities=activities)


@app.get("/api/activities/due-today", response_model=ActivityListResponse)
def activities_due_today(client: str = Query(default="Carmeco")):
    """Retrieve activities with a follow-up due today for the client."""
    activities = list_activities_due_today(client=client)
    return ActivityListResponse(client=client, count=len(activities), activities=activities)


@app.get("/api/activities/overdue", response_model=ActivityListResponse)
def activities_overdue(client: str = Query(default="Carmeco")):
    """Retrieve overdue follow-ups for the client."""
    activities = list_overdue_follow_ups(client=client)
    return ActivityListResponse(client=client, count=len(activities), activities=activities)


@app.get("/api/activities/upcoming", response_model=ActivityListResponse)
def activities_upcoming(client: str = Query(default="Carmeco")):
    """Retrieve upcoming follow-ups (after today) for the client."""
    activities = list_upcoming_follow_ups(client=client)
    return ActivityListResponse(client=client, count=len(activities), activities=activities)


@app.get("/api/work-queue/summary", response_model=WorkQueueSummary)
def work_queue_summary_api(
    client: str = Query(default="Carmeco"),
    client_id: int | None = Query(default=None),
):
    """Legacy summary: Calls Due / Follow-Ups / New — scheduled work, not status labels."""
    return work_queue_summary(client=client, client_id=client_id)


@app.get("/api/work-queue", response_model=WorkQueueListResponse)
def work_queue_list_api(
    user_id: int | None = Query(default=None),
    client_id: int | None = Query(default=None),
    work_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    due: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    hot: bool = Query(default=False),
    weblead: bool = Query(default=False),
    cross_client: bool = Query(default=False),
    overdue: bool = Query(default=False),
    assigned_user_id: int | None = Query(default=None),
    q: str | None = Query(default=None),
    ai_alignment: str | None = Query(default=None),
    ai_recommendation: str | None = Query(default=None),
    ai_fit: str | None = Query(default=None),
    ai_engagement: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Rev Development Specialist Work Queue across authorized clients."""
    try:
        return list_work_queue(
            user_id,
            client_id=client_id,
            work_type=work_type,
            status=status,
            due=due,
            priority=priority,
            hot=hot,
            weblead=weblead,
            cross_client=cross_client,
            overdue_only=overdue,
            assigned_user_id=assigned_user_id,
            q=q,
            ai_alignment=ai_alignment,
            ai_recommendation=ai_recommendation,
            ai_fit=ai_fit,
            ai_engagement=ai_engagement,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/work-queue/next", response_model=WorkQueueNextResponse)
def work_queue_next_api(
    after_queue_item_id: str = Query(...),
    after_work_priority: int | None = Query(default=None),
    after_due_date: str | None = Query(default=None),
    after_due_time: str | None = Query(default=None),
    after_company_name: str | None = Query(default=None),
    after_company_id: int | None = Query(default=None),
    user_id: int | None = Query(default=None),
    client_id: int | None = Query(default=None),
    work_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    due: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    hot: bool = Query(default=False),
    weblead: bool = Query(default=False),
    cross_client: bool = Query(default=False),
    overdue: bool = Query(default=False),
    assigned_user_id: int | None = Query(default=None),
    q: str | None = Query(default=None),
    ai_alignment: str | None = Query(default=None),
    ai_recommendation: str | None = Query(default=None),
    ai_fit: str | None = Query(default=None),
    ai_engagement: str | None = Query(default=None),
):
    """Next eligible Work Queue item after the given queue_item_id (Save & Next)."""
    try:
        return get_work_queue_next(
            user_id,
            after_queue_item_id=after_queue_item_id,
            after_work_priority=after_work_priority,
            after_due_date=after_due_date,
            after_due_time=after_due_time,
            after_company_name=after_company_name,
            after_company_id=after_company_id,
            client_id=client_id,
            work_type=work_type,
            status=status,
            due=due,
            priority=priority,
            hot=hot,
            weblead=weblead,
            cross_client=cross_client,
            overdue_only=overdue,
            assigned_user_id=assigned_user_id,
            q=q,
            ai_alignment=ai_alignment,
            ai_recommendation=ai_recommendation,
            ai_fit=ai_fit,
            ai_engagement=ai_engagement,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/work-queue/complete")
def work_queue_complete_api(body: WorkQueueCompleteRequest):
    try:
        return complete_work_queue_item(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/work-queue/log-call", response_model=WorkQueueLogCallResult)
def work_queue_log_call_api(body: WorkQueueLogCallRequest):
    """Log a call from Work Queue workspace — client-scoped, optional follow-up + complete."""
    try:
        return log_work_queue_call(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/follow-up-tasks/complete", response_model=FollowUpTaskActionResult)
def follow_up_task_complete_api(body: FollowUpTaskCompleteRequest):
    """Complete one open follow-up task for the Active Client (contact or company-level)."""
    try:
        return complete_follow_up_task(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/follow-up-tasks/reschedule", response_model=FollowUpTaskActionResult)
def follow_up_task_reschedule_api(body: FollowUpTaskRescheduleRequest):
    """Update the same open follow-up task without creating a duplicate."""
    try:
        return reschedule_follow_up_task(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/work-queue/clients")
def work_queue_clients_api(user_id: int | None = Query(default=None)):
    del user_id
    actor = resolve_staff_actor()
    return {"clients": clients_for_work_queue(actor.id if actor is not None else None)}


@app.get("/api/users/default", response_model=UserClientsResponse)
def default_user_with_clients():
    """Authenticated session user and allowed clients."""
    user = resolve_staff_actor()
    if user is None:
        raise HTTPException(status_code=404, detail="Default user not found.")
    clients = list_clients_for_user(user.id, active_only=True)
    return UserClientsResponse(user=user, clients=clients)


@app.get("/api/users/{user_id}/clients", response_model=UserClientsResponse)
def user_clients(user_id: int, request: Request):
    """List clients for a user. Self or administrator only."""
    actor = require_authenticated_staff(request)
    if int(actor.id) != int(user_id) and not bool(actor.is_administrator):
        raise HTTPException(status_code=403, detail="Not authorized.")
    user = get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    clients = list_clients_for_user(user_id, active_only=True)
    return UserClientsResponse(user=user, clients=clients)


@app.get("/api/dashboard/scope", response_model=DashboardScopeSummary)
def dashboard_scope(
    user_id: int | None = Query(default=None),
    client_id: int | None = Query(
        default=None,
        description="Omit for All My Clients; set for Selected Client mode.",
    ),
):
    """
    Dashboard query foundation:
    - no client_id → aggregates across every assigned client
    - client_id set → aggregates for that client only (assignment required)
    Query user_id is ignored as actor identity.
    """
    user = resolve_staff_actor()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    try:
        return dashboard_scope_summary(user.id, selected_client_id=client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/dashboard/follow-ups", response_model=DashboardFollowUpsResponse)
def dashboard_follow_ups(
    client_id: int = Query(..., description="Active Client id — follow-ups are client-scoped."),
):
    """Open follow-up tasks for the Active Client, split overdue / due today / upcoming."""
    user = resolve_staff_actor()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    try:
        from access import user_can_access_client

        if not user_can_access_client(user.id, client_id) and not user.is_administrator:
            raise PermissionError("Not authorized for this client.")
        return list_dashboard_follow_ups(client_id=client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/search", response_model=SearchResponse)
def global_search(
    q: str = Query(default="", description="Search text"),
    client_id: int | None = Query(default=None),
    company_id: int | None = Query(default=None),
    user_id: int | None = Query(
        default=None,
        description="Optional filter: only activities created by this user_id",
    ),
    acting_user_id: int | None = Query(
        default=None,
        description="Acting user for client-assignment security (defaults to Julie).",
    ),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    all_clients: bool = Query(default=False),
    had_appointment: bool = Query(default=False),
    had_quote: bool = Query(default=False),
    had_purchase_order: bool = Query(default=False),
    had_weblead: bool = Query(default=False),
    is_hot: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
):
    """
    Global search across companies, contacts, notes, activities, and milestones.
    Milestone filters can be combined. all_clients requires administrator.
    """
    try:
        return run_search(
            q,
            user_id=acting_user_id,
            client_id=client_id,
            company_id=company_id,
            filter_user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            activity_type=activity_type,
            status=status,
            all_clients=all_clients,
            had_appointment=had_appointment,
            had_quote=had_quote,
            had_purchase_order=had_purchase_order,
            had_weblead=had_weblead,
            is_hot=is_hot,
            limit=limit,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/milestones/summary", response_model=MilestoneSummary)
def milestones_summary_api(
    user_id: int | None = Query(default=None),
    client_id: int | None = Query(default=None),
    all_clients: bool = Query(default=False),
):
    try:
        return milestone_summary(user_id, client_id=client_id, all_clients=all_clients)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/milestones", response_model=RevenueMilestone, status_code=201)
def create_milestone_api(body: MilestoneCreateRequest):
    try:
        return create_milestone(body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/milestones/hot")
def set_hot_api(body: SetHotRequest):
    try:
        return set_company_hot(body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/companies/by-record/{record_no}/milestones",
    response_model=list[RevenueMilestone],
)
def company_milestones(record_no: str, client: str = Query(default="Carmeco")):
    return list_milestones_for_company(record_no, client=client)


@app.get(
    "/api/companies/by-record/{record_no}/client-history",
    response_model=list[ClientMilestoneHistory],
)
def company_client_history(record_no: str):
    return list_northstar_client_history(record_no)


@app.get(
    "/api/companies/by-record/{record_no}/shared-history",
    response_model=SharedHistoryResponse,
)
def company_shared_history(
    record_no: str,
    client_id: int | None = Query(
        default=None,
        description="Working/Active Client context used to resolve the master company",
    ),
    filter_client_id: int | None = Query(
        default=None,
        description="Optional filter: only show history for this originating client",
    ),
    user_id: int | None = Query(default=None),
):
    """
    NorthStar Shared History — internal cross-client visibility.

    Viewing other clients' notes/activity never changes write ownership.
    """
    try:
        return list_shared_history(
            record_no,
            user_id=user_id,
            working_client_id=client_id,
            filter_client_id=filter_client_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/opportunities/cross-client", response_model=CrossClientOpportunityList)
def cross_client_opportunities_api(
    user_id: int | None = Query(default=None),
    target_client_id: int | None = Query(default=None),
    client_id: int | None = Query(
        default=None,
        description="Alias for target_client_id",
    ),
    signal_type: str | None = Query(default=None),
    other_client_id: int | None = Query(default=None),
    state: str | None = Query(default=None),
    strength: str | None = Query(default=None),
    target_status: str | None = Query(default=None),
    target_activity: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    min_score: int | None = Query(default=None),
    proven_buyer: bool = Query(default=False),
    engaged_elsewhere: bool = Query(default=False),
    multiple_signals: bool = Query(default=False),
    include_active_target: bool = Query(default=False),
    include_dismissed: bool = Query(default=False),
):
    try:
        return list_cross_client_opportunities(
            user_id,
            target_client_id=target_client_id if target_client_id is not None else client_id,
            signal_type=signal_type,
            other_client_id=other_client_id,
            state=state,
            strength=strength,
            target_status=target_status,
            target_activity=target_activity,
            review_status=review_status,
            min_score=min_score,
            proven_buyer=proven_buyer,
            engaged_elsewhere=engaged_elsewhere,
            multiple_signals=multiple_signals,
            include_active_target=include_active_target,
            include_dismissed=include_dismissed,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/opportunities/cross-client/count")
def cross_client_opportunity_count(
    user_id: int | None = Query(default=None),
    target_client_id: int | None = Query(default=None),
    client_id: int | None = Query(default=None),
):
    try:
        count = opportunity_count_for_dashboard(
            user_id,
            target_client_id=target_client_id if target_client_id is not None else client_id,
        )
        return {"count": count}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/opportunities/cross-client/dismiss", response_model=OpportunityActionResult)
def dismiss_opportunity_api(body: OpportunityDismissRequest):
    try:
        return dismiss_opportunity(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/opportunities/cross-client/review", response_model=OpportunityActionResult)
def review_opportunity_api(body: OpportunityReviewRequest):
    try:
        return mark_opportunity_reviewed(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/opportunities/cross-client/add", response_model=OpportunityActionResult)
def add_opportunity_api(body: OpportunityAddRequest):
    try:
        return add_opportunity_to_target(
            target_client_id=body.target_client_id,
            company_id=body.company_id,
            status=body.status,
            created_by=body.created_by,
            opportunity_score=body.opportunity_score,
            source_summary=body.source_summary,
            target_campaign_id=body.target_campaign_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/companies/by-record/{record_no}/cross-client-opportunity")
def company_cross_client_banner(
    record_no: str,
    target_client_id: int | None = Query(default=None),
    user_id: int | None = Query(default=None),
):
    # Treat missing/invalid ids as "use default assigned client" — never 500 the workspace.
    focus = target_client_id if target_client_id is not None and target_client_id > 0 else None
    try:
        return workspace_cross_client_banner(
            record_no,
            target_client_id=focus,
            user_id=user_id,
        )
    except (PermissionError, LookupError):
        return {
            "applicable": False,
            "target_client_name": "",
            "evidence": [],
        }


@app.post("/api/ask-northstar", response_model=AskNorthStarResponse)
def ask_northstar_api(body: AskNorthStarRequest):
    """Phase 1 Ask NorthStar — read-only retrieval from authorized NorthStar data."""
    try:
        return ask_northstar(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- Client Setup / Target Profiles / Campaigns ---


@app.get("/api/clients/setup", response_model=list[ClientListSetupItem])
def list_clients_setup_api():
    """List authorized clients with profile completeness for Client Setup hub."""
    try:
        return list_client_setup_summaries()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/setup", response_model=ClientSetupResponse)
def get_client_setup_api(client_id: int):
    try:
        return get_client_setup(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/clients/{client_id}/setup/overview", response_model=ClientSetupResponse)
def update_client_overview_api(client_id: int, body: ClientOverviewUpdate):
    try:
        return update_client_overview(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/clients/{client_id}/setup/sells", response_model=ClientSetupResponse)
def update_client_sells_api(client_id: int, body: ClientSellsUpdate):
    try:
        return update_client_sells(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/setup/campaigns", response_model=ClientSetupResponse)
def create_campaign_api(client_id: int, body: ClientCampaignUpdate):
    try:
        return create_campaign(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/setup/campaigns/{campaign_id}",
    response_model=ClientSetupResponse,
)
def update_campaign_api(client_id: int, campaign_id: int, body: ClientCampaignUpdate):
    try:
        return update_campaign(client_id, campaign_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/setup/campaigns/{campaign_id}/default",
    response_model=ClientSetupResponse,
)
def set_default_campaign_api(client_id: int, campaign_id: int):
    try:
        return set_default_campaign(client_id, campaign_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete(
    "/api/clients/{client_id}/setup/campaigns/{campaign_id}",
    response_model=ClientSetupResponse,
)
def delete_campaign_api(client_id: int, campaign_id: int):
    try:
        return delete_campaign(client_id, campaign_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Administrator Master Data Export ---


@app.get("/api/admin/master-data-export")
def admin_master_data_export_api(
    request: Request,
    mode: str = Query(default="companies"),
    file_format: str = Query(default="xlsx", alias="format"),
):
    require_administrator(request)
    try:
        filename, content, media_type = build_master_data_export(
            mode=mode, file_format=file_format
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Client Onboarding Wizard (administrator) ---


@app.get("/api/admin/onboarding/templates")
def onboarding_templates_api(request: Request):
    require_administrator(request)
    return onboarding_templates_payload()


@app.get("/api/admin/onboarding/drafts")
def onboarding_list_drafts_api(request: Request):
    actor = require_administrator(request)
    return list_drafts(user=actor)


@app.post("/api/admin/onboarding/drafts")
def onboarding_create_draft_api(request: Request, body: dict | None = None):
    actor = require_administrator(request)
    data = body or {}
    client_id = data.get("client_id")
    try:
        cid = int(client_id) if client_id is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid client_id.") from None
    try:
        return create_draft(
            user=actor,
            client_id=cid,
            template_id=str(data.get("template_id") or "") or None,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/onboarding/drafts/{draft_id}")
def onboarding_get_draft_api(draft_id: int, request: Request):
    actor = require_administrator(request)
    try:
        return get_draft(draft_id, user=actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/admin/onboarding/drafts/{draft_id}")
def onboarding_save_draft_api(draft_id: int, request: Request, body: dict | None = None):
    actor = require_administrator(request)
    data = body or {}
    try:
        return save_draft(
            draft_id,
            user=actor,
            current_step=data.get("current_step"),
            payload_patch=data.get("payload") if isinstance(data.get("payload"), dict) else data,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/onboarding/drafts/{draft_id}/apply-template")
def onboarding_apply_template_api(draft_id: int, request: Request, body: dict | None = None):
    actor = require_administrator(request)
    data = body or {}
    try:
        return apply_template_to_draft(
            draft_id,
            user=actor,
            template_id=str(data.get("template_id") or ""),
            overwrite_populated=bool(data.get("overwrite_populated")),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/onboarding/copy-preview")
def onboarding_copy_preview_api(request: Request, body: dict | None = None):
    actor = require_administrator(request)
    data = body or {}
    try:
        source_id = int(data.get("source_client_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="source_client_id required.") from None
    draft_id = data.get("draft_id")
    dest_id = data.get("destination_client_id")
    try:
        return preview_copy_from_client(
            user=actor,
            source_client_id=source_id,
            draft_id=int(draft_id) if draft_id is not None else None,
            destination_client_id=int(dest_id) if dest_id is not None else None,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/onboarding/drafts/{draft_id}/apply-copy")
def onboarding_apply_copy_api(draft_id: int, request: Request, body: dict | None = None):
    actor = require_administrator(request)
    data = body or {}
    try:
        source_id = int(data.get("source_client_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="source_client_id required.") from None
    overwrite = data.get("overwrite_fields") if isinstance(data.get("overwrite_fields"), list) else []
    try:
        return apply_copy_to_draft(
            draft_id,
            user=actor,
            source_client_id=source_id,
            overwrite_fields=[str(x) for x in overwrite],
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/onboarding/drafts/{draft_id}/finish")
def onboarding_finish_api(draft_id: int, request: Request):
    actor = require_administrator(request)
    try:
        return finish_draft(draft_id, user=actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Client Knowledge Hub ---


@app.get(
    "/api/clients/{client_id}/knowledge",
    response_model=ClientKnowledgeHubResponse,
)
def get_client_knowledge_api(client_id: int):
    try:
        ensure_client_knowledge_schema()
        return get_client_knowledge_hub(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/documents",
    response_model=list[ClientDocumentView],
)
def list_client_documents_api(
    client_id: int, include_archived: bool = Query(default=False)
):
    try:
        return list_documents(client_id, include_archived=include_archived)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/documents",
    response_model=ClientDocumentView,
)
async def upload_client_document_api(
    client_id: int,
    file: UploadFile = File(...),
    document_type: str = Form(default="Other"),
    replace_document_id: int | None = Form(default=None),
):
    try:
        content = await file.read()
        return upload_document(
            client_id,
            filename=file.filename or "upload.bin",
            content=content,
            document_type=document_type,
            replace_document_id=replace_document_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/knowledge/documents/{document_id}/file")
def download_client_document_api(client_id: int, document_id: int):
    try:
        path, filename = get_document_file(client_id, document_id)
        return FileResponse(path, filename=filename)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/documents/{document_id}/process",
    response_model=ClientDocumentProcessResult,
)
def process_client_document_api(client_id: int, document_id: int):
    try:
        return process_document(client_id, document_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/extractions",
    response_model=list[ClientExtractionProposalView],
)
def list_extractions_api(
    client_id: int,
    status: str | None = Query(default="Pending"),
    document_id: int | None = Query(default=None),
):
    try:
        return list_extraction_proposals(
            client_id, status=status, document_id=document_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/extractions/{proposal_id}/resolve",
    response_model=ClientExtractionProposalView,
)
def resolve_extraction_api(
    client_id: int, proposal_id: int, body: ClientExtractionResolveRequest
):
    try:
        return resolve_extraction_proposal(client_id, proposal_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/extractions/{proposal_id}/incorporation-analysis",
    response_model=ProposalIncorporationAnalysis,
)
def incorporation_analysis_api(client_id: int, proposal_id: int):
    try:
        return analyze_proposal_incorporation(client_id, proposal_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/contacts/enrichment-proposals",
)
def create_enrichment_proposal_api(
    client_id: int, body: ContactEnrichmentProposalCreate
):
    try:
        return create_contact_enrichment_proposal(
            client_id,
            contact_id=body.contact_id,
            field_name=body.field_name,
            proposed_value=body.proposed_value,
            source_proposal_id=body.source_proposal_id,
            source_document_id=body.source_document_id,
            evidence=body.evidence,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/extractions/{proposal_id}/suggest-title-enrichments",
)
def suggest_title_enrichments_api(client_id: int, proposal_id: int):
    """Create Pending title enrichment proposals from a source proposal. No approvals."""
    try:
        created = ensure_title_enrichment_proposals_from_source(
            client_id, proposal_id
        )
        analysis = analyze_proposal_incorporation(client_id, proposal_id)
        return {
            "source_proposal_id": proposal_id,
            "enrichment_proposals": created,
            "analysis": analysis,
            "message": (
                f"Created/found {len(created)} Pending enrichment proposal(s). "
                "Nothing was approved or resolved."
            ),
        }
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/extractions/bulk-resolve/preview",
)
def preview_bulk_resolve_api(
    client_id: int, body: ClientExtractionBulkResolveRequest
):
    try:
        return preview_bulk_resolve(client_id, body.proposal_ids or [])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/extractions/bulk-resolve",
)
def bulk_resolve_api(client_id: int, body: ClientExtractionBulkResolveRequest):
    try:
        return bulk_resolve_eligible_proposals(client_id, body.proposal_ids or [])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/extractions/bulk-reject",
)
def bulk_reject_api(client_id: int, body: ClientExtractionBulkRejectRequest):
    try:
        return bulk_reject_proposals(
            client_id,
            body.proposal_ids or [],
            rejection_category=body.rejection_category,
            rejection_note=body.rejection_note or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/extractions/review-classifications",
)
def review_classifications_api(client_id: int):
    try:
        return classify_pending_proposals_for_review(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/extractions/bulk-review",
    response_model=list[ClientExtractionProposalView],
)
def bulk_review_extractions_api(
    client_id: int, body: ClientExtractionBulkReviewRequest
):
    try:
        return bulk_review_extraction_proposals(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/documents/{document_id}/archive",
    response_model=ClientDocumentView,
)
def archive_client_document_api(client_id: int, document_id: int):
    try:
        return archive_document(client_id, document_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/knowledge/section-catalog")
def knowledge_section_catalog_api():
    """Section/field catalog for extraction remapping (read-only)."""
    return get_knowledge_section_catalog()


@app.put(
    "/api/clients/{client_id}/knowledge/sections/{section_key}",
    response_model=ClientKnowledgeSectionView,
)
def upsert_knowledge_section_api(
    client_id: int, section_key: str, body: ClientKnowledgeSectionUpdate
):
    try:
        return upsert_knowledge_section(client_id, section_key, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch(
    "/api/clients/{client_id}/knowledge/sections/{section_key}/fields",
    response_model=ClientKnowledgeSectionView,
)
def update_knowledge_field_api(
    client_id: int, section_key: str, body: ClientKnowledgeFieldUpdate
):
    try:
        return update_knowledge_field(
            client_id, section_key, body.field_name, body.value or ""
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/email-templates",
    response_model=list[ClientEmailTemplateView],
)
def list_email_templates_api(client_id: int):
    try:
        return list_email_templates(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/email-templates/preview-group",
    response_model=EmailTemplateGroupPreview,
)
def preview_email_template_group_api(
    client_id: int, body: EmailTemplateGroupApproveRequest
):
    """Preview combined template from extraction proposals. No writes."""
    try:
        return preview_email_template_group(client_id, body.proposal_ids or [])
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/email-templates/approve-group",
)
def approve_email_template_group_api(
    client_id: int, body: EmailTemplateGroupApproveRequest
):
    try:
        return approve_email_template_group(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/email-templates",
    response_model=ClientEmailTemplateView,
)
def create_email_template_api(client_id: int, body: ClientEmailTemplateUpdate):
    try:
        return upsert_email_template(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/knowledge/email-templates/{template_id}",
    response_model=ClientEmailTemplateView,
)
def update_email_template_api(
    client_id: int, template_id: int, body: ClientEmailTemplateUpdate
):
    try:
        return upsert_email_template(client_id, body, template_id=template_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Client email accounts / sender identity (config + preview only; no send) ---


@app.get(
    "/api/clients/{client_id}/email-accounts",
    response_model=list[ClientEmailAccountView],
)
def list_email_accounts_api(client_id: int, request: Request):
    actor = require_client_access(request, client_id)
    try:
        return list_email_accounts(client_id, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/email-accounts/suggestions",
    response_model=ClientEmailAccountSuggestion,
)
def suggest_email_accounts_api(client_id: int, request: Request):
    """Suggest from approved Client Operations — never auto-creates."""
    actor = require_client_access(request, client_id)
    try:
        return suggest_email_account_setup(client_id, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-accounts",
    response_model=ClientEmailAccountView,
)
def create_email_account_api(client_id: int, body: ClientEmailAccountUpdate, request: Request):
    actor = require_client_setup_editor(request, client_id)
    try:
        return upsert_email_account(client_id, body, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/email-accounts/{account_id}",
    response_model=ClientEmailAccountView,
)
def update_email_account_api(
    client_id: int, account_id: int, body: ClientEmailAccountUpdate, request: Request
):
    actor = require_client_setup_editor(request, client_id)
    try:
        return upsert_email_account(
            client_id, body, account_id=account_id, user_id=int(actor.id)
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-accounts/{account_id}/deactivate",
    response_model=ClientEmailAccountView,
)
def deactivate_email_account_api(client_id: int, account_id: int, request: Request):
    actor = require_client_setup_editor(request, client_id)
    try:
        return deactivate_email_account(client_id, account_id, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/email-accounts/{account_id}/connect")
def connect_email_account_api(client_id: int, account_id: int, request: Request):
    """Start provider connect. Google → OAuth URL; other providers remain stubbed."""
    actor = require_administrator(request)
    try:
        from client_email_accounts_data import get_email_account

        account = get_email_account(client_id, account_id, user_id=int(actor.id))
        provider = (account.provider or "").strip().lower()
        if provider in {"google", "gmail"}:
            started = begin_google_connect(account_id, actor=actor)
            return {
                "account_id": account_id,
                "client_id": client_id,
                "email_address": started.get("email_address"),
                "provider": "Google",
                "connection_status": "connecting",
                "connected": False,
                "available": True,
                "authorization_url": started.get("authorization_url"),
                "oauth_path": f"/api/email/google/connect/{account_id}",
                "message": "Redirect to Google to connect this mailbox.",
            }
        return connect_email_account_stub(client_id, account_id, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/email/google/status")
def google_oauth_status_api(request: Request):
    require_administrator(request)
    return oauth_status()


@app.get("/api/email/google/connect/{account_id}")
def google_connect_start_api(account_id: int, request: Request, redirect: int = 1):
    """Start Google OAuth. redirect=1 returns RedirectResponse to Google."""
    actor = require_administrator(request)
    try:
        started = begin_google_connect(account_id, actor=actor)
        if redirect:
            return RedirectResponse(url=started["authorization_url"], status_code=302)
        return started
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/email/google/callback")
def google_oauth_callback_api(code: str = "", state: str = "", error: str = ""):
    """Google redirects here. Exchange code and bounce user to frontend."""
    from google_oauth_config import frontend_after_connect_base
    from urllib.parse import quote

    if error:
        dest = (
            f"{frontend_after_connect_base()}/administration"
            f"?email_oauth=error&detail={quote(error[:180])}"
        )
        return RedirectResponse(url=dest, status_code=302)
    try:
        if not code or not state:
            raise ValueError("Missing code or state from Google.")
        result = complete_google_callback(code=code, state=state)
        return RedirectResponse(url=result["frontend_redirect"], status_code=302)
    except Exception as exc:
        dest = (
            f"{frontend_after_connect_base()}/administration"
            f"?email_oauth=error&detail={quote(str(exc)[:180])}"
        )
        return RedirectResponse(url=dest, status_code=302)


@app.post("/api/clients/{client_id}/email-accounts/{account_id}/disconnect")
def disconnect_google_account_api(client_id: int, account_id: int, request: Request):
    actor = require_administrator(request)
    try:
        return disconnect_google_account(client_id, account_id, actor=actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-send",
    response_model=ClientEmailSendResult,
)
def send_client_email_api(client_id: int, body: ClientEmailSendRequest, request: Request):
    """Send via Gmail after human confirm_send. Creates Email Sent only on success."""
    actor = require_client_access(request, client_id)
    try:
        if body.confirm_send and body.confirm_recipient:
            if body.confirm_recipient.strip().lower() != body.to_address.strip().lower():
                raise ValueError("confirm_recipient must match to_address.")
        return send_client_email(client_id, body, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-accounts/{account_id}/assignments",
    response_model=ClientEmailAccountAssignmentView,
)
def create_email_account_assignment_api(
    client_id: int, account_id: int, body: ClientEmailAccountAssignmentUpdate, request: Request
):
    actor = require_client_setup_editor(request, client_id)
    try:
        return upsert_email_account_assignment(
            client_id, account_id, body, user_id=int(actor.id)
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/email-signatures",
    response_model=list[ClientEmailSignatureView],
)
def list_email_signatures_api(client_id: int, request: Request):
    actor = require_client_access(request, client_id)
    try:
        return list_email_signatures(client_id, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-signatures",
    response_model=ClientEmailSignatureView,
)
def create_email_signature_api(
    client_id: int, body: ClientEmailSignatureUpdate, request: Request
):
    actor = require_client_setup_editor(request, client_id)
    try:
        return upsert_email_signature(client_id, body, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/email-signatures/{signature_id}",
    response_model=ClientEmailSignatureView,
)
def update_email_signature_api(
    client_id: int, signature_id: int, body: ClientEmailSignatureUpdate, request: Request
):
    actor = require_client_setup_editor(request, client_id)
    try:
        return upsert_email_signature(
            client_id, body, signature_id=signature_id, user_id=int(actor.id)
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-signatures/{signature_id}/deactivate",
    response_model=ClientEmailSignatureView,
)
def deactivate_email_signature_api(client_id: int, signature_id: int, request: Request):
    actor = require_client_setup_editor(request, client_id)
    try:
        return deactivate_email_signature(
            client_id, signature_id, user_id=int(actor.id)
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/email-preview/contacts")
def list_email_preview_contacts_api(client_id: int, request: Request, limit: int = 100):
    actor = require_client_access(request, client_id)
    try:
        return list_preview_contacts(client_id, user_id=int(actor.id), limit=limit)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/email-preview/appointments",
    response_model=list[EmailPreviewAppointmentOption],
)
def list_email_preview_appointments_api(
    client_id: int,
    request: Request,
    contact_id: int = Query(...),
    limit: int = 50,
):
    """Read-only appointments for Preview Email, scoped to a CRM contact."""
    actor = require_client_access(request, client_id)
    try:
        return list_preview_appointments(
            client_id, contact_id, user_id=int(actor.id), limit=limit
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-preview",
    response_model=ClientEmailPreviewResult,
)
def preview_client_email_api(
    client_id: int, body: ClientEmailPreviewRequest, request: Request
):
    """Read-only From/To/Subject/Body/Signature render. Does not send."""
    actor = require_client_access(request, client_id)
    try:
        return preview_client_email(client_id, body, user_id=int(actor.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_admin_client(request: Request, client_id: int):
    actor = require_administrator(request)
    if client_id <= 0:
        raise HTTPException(status_code=400, detail=CLIENT_REQUIRED)
    require_client_access(request, client_id)
    return actor


@app.post(
    "/api/clients/{client_id}/admin/imports",
    response_model=CrmImportUploadResult,
)
async def upload_admin_crm_import_api(
    client_id: int,
    request: Request,
    file: UploadFile = File(...),
    worksheet: str = Form(default=""),
):
    actor = _require_admin_client(request, client_id)
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="This file is too large to upload.")
    try:
        return upload_crm_import(
            client_id=client_id,
            actor=actor,
            filename=file.filename or "upload.csv",
            content=content,
            worksheet=worksheet or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/admin/imports",
    response_model=CrmImportHistoryPage,
)
def list_admin_crm_import_history_api(client_id: int, request: Request, limit: int = 20):
    _require_admin_client(request, client_id)
    items = list_crm_import_batches(client_id, limit=limit)
    return CrmImportHistoryPage(client_id=int(client_id), items=items)


@app.get("/api/clients/{client_id}/admin/imports/{batch_id}")
def get_admin_crm_import_api(client_id: int, batch_id: int, request: Request):
    _require_admin_client(request, client_id)
    try:
        return get_crm_import_batch(client_id, batch_id, include_sample=True)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/admin/imports/{batch_id}/rows",
    response_model=CrmImportRowsPage,
)
def list_admin_crm_import_rows_api(
    client_id: int,
    batch_id: int,
    request: Request,
    offset: int = 0,
    limit: int = 25,
):
    _require_admin_client(request, client_id)
    try:
        return list_crm_import_rows(client_id, batch_id, offset=offset, limit=limit)
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/admin/leadmaster-refresh/meta")
def leadmaster_refresh_meta_api(request: Request):
    require_administrator(request)
    return refresh_meta()


@app.get("/api/admin/data-steward/meta")
def data_steward_meta_api(request: Request):
    require_administrator(request)
    from data_steward import (
        live_ccr_lifecycle_enabled,
        live_company_amend_enabled,
        live_destructive_enabled,
        live_master_archive_enabled,
    )

    ccr_on = live_ccr_lifecycle_enabled()
    archive_on = live_master_archive_enabled()
    return {
        "live_mutations_enabled": live_destructive_enabled(),
        "company_amend_enabled": live_company_amend_enabled(),
        "archive_enabled": archive_on,
        "company_restore_enabled": archive_on,
        "delete_enabled": False,
        "merge_enabled": False,
        "remove_relationship_enabled": ccr_on,
        "restore_enabled": ccr_on,
        "message": (
            "Governed Master Company amend, archive/restore, and client relationship "
            "remove/restore are enabled. Live merge and hard delete remain disabled."
        ),
    }


@app.get("/api/admin/data-steward/provenance")
def data_steward_provenance_api(
    request: Request,
    entity_type: str = "",
    entity_id: int = 0,
    field: str = "",
    source_type: str = "",
    actor_id: int = 0,
    since: str = "",
    until: str = "",
    limit: int = 100,
):
    require_administrator(request)
    from data_steward import current_field_authority, provenance_history, provenance_table_ready

    with get_connection() as conn:
        if not provenance_table_ready(conn):
            return {
                "schema_ready": False,
                "events": [],
                "current": None,
                "message": "Provenance table is not available on this database copy.",
            }
        events = provenance_history(
            conn,
            entity_type=entity_type or None,
            entity_id=entity_id or None,
            field=field,
            source_type=source_type,
            actor_id=actor_id or None,
            since=since,
            until=until,
            limit=min(max(int(limit or 100), 1), 500),
        )
        current = None
        if entity_type and entity_id and field:
            current = current_field_authority(
                conn, entity_type=entity_type, entity_id=int(entity_id), field=field
            )
        return {
            "schema_ready": True,
            "current": current,
            "events": events,
            "newest_first": True,
        }


def _steward_http_error(exc: Exception) -> HTTPException:
    from bulk_assignment import BLOCK_CODES_400, BulkAssignmentError
    from data_steward import StewardError, StewardLiveWriteError, StewardPermissionError
    from data_steward_amend import DuplicateCompanyError

    if isinstance(exc, DuplicateCompanyError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_company",
                "message": "Proposed values collide with another master company. Save is blocked.",
                "candidates": exc.candidates,
            },
        )
    if isinstance(exc, BulkAssignmentError):
        code = str(exc)
        status = 400 if code in BLOCK_CODES_400 else 409
        detail = {"code": code, "message": code, **(exc.payload or {})}
        return HTTPException(status_code=status, detail=detail)
    if isinstance(exc, StewardPermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, StewardLiveWriteError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, StewardError):
        code = str(exc)
        status = 400 if code in {"reason_required", "confirmation_required"} else 409
        return HTTPException(status_code=status, detail={"code": code, "message": code})
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    raise exc


@app.get("/api/admin/data-steward/companies")
def data_steward_search_companies_api(
    request: Request,
    q: str = Query(default=""),
    visibility: str = Query(default="all"),
):
    require_administrator(request)
    from data_steward_amend import search_master_companies

    with get_connection() as conn:
        return {"companies": search_master_companies(conn, q, visibility=visibility)}


@app.get("/api/admin/data-steward/companies/{company_id}")
def data_steward_get_company_api(company_id: int, request: Request):
    require_administrator(request)
    from data_steward_amend import load_master_company

    try:
        with get_connection() as conn:
            return load_master_company(conn, company_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/admin/data-steward/companies/{company_id}/amend/preview")
def data_steward_preview_company_amend_api(
    company_id: int, body: dict, request: Request
):
    actor = require_administrator(request)
    from data_steward_amend import CompanyAmendRequest, fields_from_request, preview_company_amend

    parsed = CompanyAmendRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return preview_company_amend(
                conn,
                actor=actor,
                company_id=company_id,
                fields=fields_from_request(parsed),
                reason=parsed.reason,
            )
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/companies/{company_id}/amend")
def data_steward_save_company_amend_api(
    company_id: int, body: dict, request: Request
):
    actor = require_administrator(request)
    from data_steward_amend import (
        CompanyAmendRequest,
        fields_from_request,
        save_company_amend,
    )

    parsed = CompanyAmendRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return save_company_amend(
                conn,
                actor=actor,
                company_id=company_id,
                fields=fields_from_request(parsed),
                reason=parsed.reason,
                expected_updated_at=parsed.expected_updated_at,
                preview_fingerprint_value=parsed.preview_fingerprint,
            )
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.get("/api/admin/data-steward/companies/{company_id}/relationship-events")
def data_steward_company_relationship_events_api(company_id: int, request: Request):
    require_administrator(request)
    from data_steward_relationship import list_company_relationship_events

    with get_connection() as conn:
        return {"events": list_company_relationship_events(conn, company_id), "newest_first": True}


@app.post("/api/admin/data-steward/relationships/{ccr_id}/remove/preview")
def data_steward_preview_remove_api(ccr_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_relationship import RelationshipActionRequest, preview_remove_relationship

    parsed = RelationshipActionRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return preview_remove_relationship(
                conn, actor=actor, ccr_id=ccr_id, reason=parsed.reason
            )
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/relationships/{ccr_id}/remove")
def data_steward_confirm_remove_api(ccr_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_relationship import RelationshipActionRequest, confirm_remove_relationship

    parsed = RelationshipActionRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return confirm_remove_relationship(conn, actor=actor, ccr_id=ccr_id, body=parsed)
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/relationships/{ccr_id}/restore/preview")
def data_steward_preview_restore_api(ccr_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_relationship import RelationshipActionRequest, preview_restore_relationship

    parsed = RelationshipActionRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return preview_restore_relationship(
                conn, actor=actor, ccr_id=ccr_id, reason=parsed.reason
            )
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/relationships/{ccr_id}/restore")
def data_steward_confirm_restore_api(ccr_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_relationship import RelationshipActionRequest, confirm_restore_relationship

    parsed = RelationshipActionRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return confirm_restore_relationship(conn, actor=actor, ccr_id=ccr_id, body=parsed)
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.get("/api/admin/data-steward/companies/{company_id}/lifecycle-events")
def data_steward_company_lifecycle_events_api(company_id: int, request: Request):
    require_administrator(request)
    from data_steward_archive import list_company_lifecycle_events

    with get_connection() as conn:
        return {"events": list_company_lifecycle_events(conn, company_id), "newest_first": True}


@app.get("/api/admin/bulk-assignment/assignees")
def bulk_assignment_assignees_api(request: Request, client_id: int = Query(...)):
    require_administrator(request)
    from bulk_assignment import list_eligible_assignees

    try:
        with get_connection() as conn:
            return {"client_id": int(client_id), "assignees": list_eligible_assignees(conn, client_id=client_id)}
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/bulk-assignment/preview")
def bulk_assignment_preview_api(body: dict, request: Request):
    actor = require_administrator(request)
    from bulk_assignment import BulkAssignmentRequest, preview_bulk_assignment

    parsed = BulkAssignmentRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return preview_bulk_assignment(conn, actor=actor, body=parsed)
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/bulk-assignment/confirm")
def bulk_assignment_confirm_api(body: dict, request: Request):
    actor = require_administrator(request)
    from bulk_assignment import BulkAssignmentRequest, confirm_bulk_assignment

    parsed = BulkAssignmentRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return confirm_bulk_assignment(conn, actor=actor, body=parsed)
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/companies/{company_id}/archive/preview")
def data_steward_preview_archive_company_api(company_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_archive import MasterArchiveRequest, preview_archive_company

    parsed = MasterArchiveRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return preview_archive_company(
                conn, actor=actor, company_id=company_id, reason=parsed.reason
            )
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/companies/{company_id}/archive")
def data_steward_confirm_archive_company_api(company_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_archive import MasterArchiveRequest, confirm_archive_company

    parsed = MasterArchiveRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return confirm_archive_company(conn, actor=actor, company_id=company_id, body=parsed)
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/companies/{company_id}/restore/preview")
def data_steward_preview_restore_company_api(company_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_archive import MasterArchiveRequest, preview_restore_company

    parsed = MasterArchiveRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return preview_restore_company(
                conn, actor=actor, company_id=company_id, reason=parsed.reason
            )
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/admin/data-steward/companies/{company_id}/restore")
def data_steward_confirm_restore_company_api(company_id: int, body: dict, request: Request):
    actor = require_administrator(request)
    from data_steward_archive import MasterArchiveRequest, confirm_restore_company

    parsed = MasterArchiveRequest.model_validate(body or {})
    try:
        with get_connection() as conn:
            return confirm_restore_company(conn, actor=actor, company_id=company_id, body=parsed)
    except Exception as exc:
        mapped = _steward_http_error(exc)
        if mapped:
            raise mapped from exc
        raise


@app.post("/api/clients/{client_id}/admin/leadmaster-refresh")
async def upload_leadmaster_refresh_api(
    client_id: int,
    request: Request,
    file: UploadFile = File(...),
    worksheet: str = Form(default=""),
):
    actor = _require_admin_client(request, client_id)
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="This file is too large to upload.")
    try:
        return upload_refresh(
            client_id=client_id,
            actor=actor,
            filename=file.filename or "upload.csv",
            content=content,
            worksheet=worksheet or "",
        )
    except RefreshLiveWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}")
def get_leadmaster_refresh_batch_api(client_id: int, batch_id: int, request: Request):
    _require_admin_client(request, client_id)
    try:
        return refresh_batch_view(client_id, batch_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/mapping")
def save_leadmaster_refresh_mapping_api(
    client_id: int, batch_id: int, body: dict, request: Request
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_refresh_mapping(
            client_id=client_id,
            batch_id=batch_id,
            actor=actor,
            mapping=dict(body.get("mapping") or {}),
        )
    except RefreshLiveWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/policy")
def save_leadmaster_refresh_policy_api(
    client_id: int, batch_id: int, body: dict, request: Request
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_refresh_policy(
            client_id=client_id,
            batch_id=batch_id,
            actor=actor,
            policy_payload=dict(body.get("policy") or body),
        )
    except RefreshLiveWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/preview")
def preview_leadmaster_refresh_api(client_id: int, batch_id: int, request: Request):
    _require_admin_client(request, client_id)
    try:
        return preview_refresh(client_id=client_id, batch_id=batch_id, persist=False)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/review")
def review_leadmaster_refresh_api(client_id: int, batch_id: int, request: Request):
    _require_admin_client(request, client_id)
    try:
        plan = preview_refresh(client_id=client_id, batch_id=batch_id, persist=False)
        return {
            "plan_fingerprint": plan.get("plan_fingerprint"),
            "policy": plan.get("policy_visible") or plan.get("policy"),
            "counts": plan.get("counts"),
            "review_rows": plan.get("review_rows") or [],
        }
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/recalculate")
def recalculate_leadmaster_refresh_api(client_id: int, batch_id: int, request: Request):
    _require_admin_client(request, client_id)
    try:
        return preview_refresh(client_id=client_id, batch_id=batch_id, persist=True)
    except RefreshLiveWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/resolutions")
def save_leadmaster_refresh_resolutions_api(
    client_id: int, batch_id: int, body: dict, request: Request
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_refresh_resolutions(
            client_id=client_id,
            batch_id=batch_id,
            actor=actor,
            resolutions=list(body.get("resolutions") or []),
        )
    except RefreshLiveWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/readiness")
def leadmaster_refresh_readiness_api(client_id: int, batch_id: int, request: Request):
    actor = _require_admin_client(request, client_id)
    try:
        return readiness_refresh(client_id=client_id, batch_id=batch_id, actor=actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/leadmaster-refresh/{batch_id}/confirm")
def confirm_leadmaster_refresh_api(client_id: int, batch_id: int, request: Request, body: dict | None = None):
    actor = _require_admin_client(request, client_id)
    payload = body or {}
    expected = str(payload.get("plan_fingerprint") or "")
    try:
        return confirm_refresh(
            client_id=client_id,
            batch_id=batch_id,
            actor=actor,
            expected_fingerprint=expected,
        )
    except RefreshLiveWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefreshApplyDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/clients/{client_id}/admin/imports/{batch_id}")
def cancel_admin_crm_import_api(client_id: int, batch_id: int, request: Request):
    actor = _require_admin_client(request, client_id)
    try:
        return cancel_crm_import(client_id, batch_id, actor=actor)
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/admin/imports/{batch_id}/mapping",
    response_model=CrmImportBatchView,
)
def save_admin_crm_import_mapping_api(
    client_id: int,
    batch_id: int,
    body: CrmImportMappingRequest,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_crm_import_mapping(
            client_id,
            batch_id,
            actor=actor,
            mapping=body.mapping,
        )
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/admin/imports/{batch_id}/source-type",
    response_model=CrmImportBatchView,
)
def save_admin_crm_import_source_type_api(
    client_id: int,
    batch_id: int,
    body: CrmImportSourceTypeRequest,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_crm_import_source_type(
            client_id,
            batch_id,
            actor=actor,
            source_type=body.source_type,
        )
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/admin/imports/{batch_id}/rows/{row_id}/status-resolution",
    response_model=CrmImportStatusResolutionResponse,
)
def save_admin_crm_import_status_resolution_api(
    client_id: int,
    batch_id: int,
    row_id: int,
    body: CrmImportStatusResolutionRequest,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        result = save_crm_import_status_resolution(
            client_id,
            batch_id,
            row_id,
            actor=actor,
            resolution_type=body.resolution_type,
            resolved_status=body.resolved_status,
            clear=bool(body.clear),
        )
        return CrmImportStatusResolutionResponse(**result)
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/admin/imports/{batch_id}/rows/{row_id}/match-resolution",
    response_model=CrmImportMatchResolutionResponse,
)
def save_admin_crm_import_match_resolution_api(
    client_id: int,
    batch_id: int,
    row_id: int,
    body: CrmImportMatchResolutionRequest,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        result = save_crm_import_match_resolution_http(
            client_id,
            batch_id,
            row_id,
            actor=actor,
            resolution_type=body.resolution_type,
            company_id=body.company_id,
            contact_id=body.contact_id,
            clear=bool(body.clear),
        )
        return CrmImportMatchResolutionResponse(**result)
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/admin/imports/{batch_id}/dry-run",
    response_model=CrmImportDryRunResponse,
)
def dry_run_admin_crm_import_api(
    client_id: int,
    batch_id: int,
    request: Request,
    body: CrmImportDryRunRequest | None = None,
):
    _require_admin_client(request, client_id)
    req = body or CrmImportDryRunRequest()
    try:
        return dry_run_crm_import(
            client_id,
            batch_id,
            offset=req.offset,
            limit=req.limit,
            use_imported_status_for_existing=bool(
                req.use_imported_status_for_existing
            ),
        )
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdentityKeysNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/admin/imports/{batch_id}/confirm",
    response_model=CrmImportConfirmResponse,
)
def confirm_admin_crm_import_api(
    client_id: int,
    batch_id: int,
    body: CrmImportConfirmRequest,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    if body.confirm is not True:
        raise HTTPException(status_code=400, detail="confirm must be true.")
    try:
        return confirm_admin_crm_import_batch(
            client_id=client_id,
            batch_id=batch_id,
            plan_fingerprint=body.plan_fingerprint,
            actor=actor,
            use_imported_status_for_existing=bool(
                body.use_imported_status_for_existing
            ),
        )
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdentityKeysNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unexpected failure.") from exc


@app.post("/api/clients/{client_id}/admin/research-imports")
async def upload_admin_research_import_api(
    client_id: int,
    request: Request,
    file: UploadFile = File(...),
    worksheet: str = Form(default=""),
    research_method: str = Form(default=""),
    research_date: str = Form(default=""),
    batch_name: str = Form(default=""),
    source_type: str = Form(default="CHATGPT_DEEP_RESEARCH"),
    source_label: str = Form(default=""),
    source_supplied_by: str = Form(default=""),
):
    actor = _require_admin_client(request, client_id)
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="This file is too large to upload.")
    try:
        return upload_research_import(
            client_id=client_id,
            actor=actor,
            filename=file.filename or "upload.csv",
            content=content,
            worksheet=worksheet or "",
            research_method=research_method or "",
            research_date=research_date or "",
            batch_name=batch_name or "",
            source_type=source_type or "",
            source_label=source_label or "",
            source_supplied_by=source_supplied_by or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/research-imports/{batch_id}/mapping")
def save_admin_research_import_mapping_api(
    client_id: int,
    batch_id: int,
    body: dict,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_research_import_mapping(
            client_id,
            batch_id,
            actor=actor,
            mapping=body.get("mapping") or {},
            research_method=str(body.get("research_method") or ""),
            research_date=str(body.get("research_date") or ""),
            batch_name=str(body.get("batch_name") or ""),
            source_type=str(body.get("source_type") or ""),
            source_label=str(body.get("source_label") or ""),
            source_supplied_by=str(body.get("source_supplied_by") or ""),
            mapping_template_id=(int(body["mapping_template_id"]) if body.get("mapping_template_id") else None),
            save_as_template=str(body.get("save_as_template") or ""),
        )
    except ResearchBatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/research-imports/{batch_id}/match-resolution")
def save_admin_research_match_resolution_api(
    client_id: int,
    batch_id: int,
    body: dict,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_research_match_resolution(
            client_id,
            batch_id,
            actor=actor,
            staged_row_id=int(body.get("staged_row_id") or 0),
            resolution_type=str(body.get("resolution_type") or ""),
            company_id=(int(body["company_id"]) if body.get("company_id") else None),
        )
    except ResearchBatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/research-imports/{batch_id}/master-resolution")
def save_admin_research_master_resolution_api(
    client_id: int,
    batch_id: int,
    body: dict,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_research_master_resolution(
            client_id,
            batch_id,
            actor=actor,
            staged_row_id=int(body.get("staged_row_id") or 0),
            field=str(body.get("field") or ""),
            resolution_type=str(body.get("resolution_type") or ""),
        )
    except ResearchBatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/research-imports/{batch_id}/contact-resolution")
def save_admin_research_contact_resolution_api(
    client_id: int,
    batch_id: int,
    body: dict,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_research_contact_resolution(
            client_id,
            batch_id,
            actor=actor,
            staged_row_id=int(body.get("staged_row_id") or 0),
            resolution_type=str(body.get("resolution_type") or ""),
            contact_id=(int(body["contact_id"]) if body.get("contact_id") else None),
        )
    except ResearchBatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/research-imports/{batch_id}/dry-run")
def dry_run_admin_research_import_api(
    client_id: int,
    batch_id: int,
    request: Request,
    offset: int = 0,
    limit: int = 25,
):
    _require_admin_client(request, client_id)
    try:
        return dry_run_research_import(client_id, batch_id, offset=offset, limit=limit)
    except ResearchBatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/admin/research-imports/{batch_id}/confirm")
def confirm_admin_research_import_api(
    client_id: int,
    batch_id: int,
    body: dict,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return confirm_research_import(
            client_id=client_id,
            batch_id=batch_id,
            plan_fingerprint=str(body.get("plan_fingerprint") or ""),
            actor=actor,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ResearchBatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/admin/shared-note-history-imports",
    response_model=SharedNoteHistoryUploadResult,
)
async def upload_shared_note_history_import_api(
    client_id: int,
    request: Request,
    prospects_file: UploadFile = File(...),
    history_file: UploadFile = File(...),
):
    """Upload prospects + shared history CSVs and return dry-run preview counts."""
    actor = _require_admin_client(request, client_id)
    prospects_content = await prospects_file.read(SNH_MAX_FILE_BYTES + 1)
    history_content = await history_file.read(SNH_MAX_FILE_BYTES + 1)
    if len(prospects_content) > SNH_MAX_FILE_BYTES or len(history_content) > SNH_MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="This file is too large to upload.")
    try:
        return upload_shared_note_history_import(
            client_id=client_id,
            actor=actor,
            prospects_filename=prospects_file.filename or "prospects.csv",
            prospects_content=prospects_content,
            history_filename=history_file.filename or "history.csv",
            history_content=history_content,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/admin/shared-note-history-imports/{batch_id}",
    response_model=SharedNoteHistoryImportBatchView,
)
def get_shared_note_history_import_api(client_id: int, batch_id: int, request: Request):
    _require_admin_client(request, client_id)
    try:
        return get_shared_note_history_batch(client_id, batch_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/admin/shared-note-history-imports/{batch_id}/confirm",
    response_model=SharedNoteHistoryConfirmResponse,
)
def confirm_shared_note_history_import_api(
    client_id: int,
    batch_id: int,
    request: Request,
    confirm: bool = True,
):
    actor = _require_admin_client(request, client_id)
    if confirm is not True:
        raise HTTPException(status_code=400, detail="confirm must be true.")
    try:
        return confirm_shared_note_history_import(
            client_id=client_id,
            batch_id=batch_id,
            actor=actor,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unexpected failure.") from exc


@app.post(
    "/api/clients/{client_id}/admin/client-data-imports",
    response_model=ClientDataImportUploadResult,
)
async def upload_admin_client_data_import_api(
    client_id: int,
    request: Request,
    prospects_file: UploadFile | None = File(default=None),
    history_file: UploadFile | None = File(default=None),
    worksheet: str = Form(default=""),
):
    actor = _require_admin_client(request, client_id)
    prospects_content = None
    prospects_name = None
    if prospects_file is not None and _blank_filename(prospects_file.filename):
        prospects_content = await prospects_file.read(MAX_FILE_BYTES + 1)
        if len(prospects_content) > MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail="This file is too large to upload.")
        prospects_name = prospects_file.filename or "prospects.csv"
    history_content = None
    history_name = None
    if history_file is not None and _blank_filename(history_file.filename):
        history_content = await history_file.read(SNH_MAX_FILE_BYTES + 1)
        if len(history_content) > SNH_MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail="This file is too large to upload.")
        history_name = history_file.filename or "history.csv"
    try:
        return upload_client_data_import(
            client_id=client_id,
            actor=actor,
            prospects_filename=prospects_name,
            prospects_content=prospects_content,
            history_filename=history_name,
            history_content=history_content,
            worksheet=worksheet or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _blank_filename(name: str | None) -> bool:
    return bool(str(name or "").strip())


@app.get(
    "/api/clients/{client_id}/admin/client-data-imports/{batch_id}",
    response_model=ClientDataImportBatchView,
)
def get_admin_client_data_import_api(client_id: int, batch_id: int, request: Request):
    _require_admin_client(request, client_id)
    try:
        return get_client_data_import_batch(client_id, batch_id, include_sample=True)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete(
    "/api/clients/{client_id}/admin/client-data-imports/{batch_id}",
    response_model=ClientDataImportBatchView,
)
def cancel_admin_client_data_import_api(client_id: int, batch_id: int, request: Request):
    actor = _require_admin_client(request, client_id)
    try:
        return cancel_client_data_import(client_id, batch_id, actor=actor)
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/mapping",
    response_model=ClientDataImportBatchView,
)
def save_admin_client_data_import_mapping_api(
    client_id: int,
    batch_id: int,
    body: ClientDataImportMappingRequest,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return save_client_data_import_mapping(
            client_id,
            batch_id,
            actor=actor,
            prospects_mapping=body.prospects_mapping,
            history_mapping=body.history_mapping,
        )
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/dry-run",
    response_model=ClientDataImportDryRunResponse,
)
def dry_run_admin_client_data_import_api(
    client_id: int,
    batch_id: int,
    request: Request,
    body: ClientDataImportDryRunRequest | None = None,
):
    actor = _require_admin_client(request, client_id)
    _ = body
    try:
        return dry_run_client_data_import(client_id, batch_id, actor=actor)
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdentityKeysNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/confirm",
    response_model=ClientDataImportConfirmResponse,
)
def confirm_admin_client_data_import_api(
    client_id: int,
    batch_id: int,
    body: ClientDataImportConfirmRequest,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    if body.confirm is not True:
        raise HTTPException(status_code=400, detail="confirm must be true.")
    try:
        return confirm_client_data_import(
            client_id=client_id,
            batch_id=batch_id,
            plan_fingerprint=body.plan_fingerprint,
            actor=actor,
            confirm=True,
        )
    except BatchNotReusable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdentityKeysNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unexpected failure.") from exc


@app.post(
    "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/retry-staging-cleanup",
    response_model=ClientDataImportConfirmResponse,
)
def retry_admin_client_data_import_staging_cleanup_api(
    client_id: int,
    batch_id: int,
    request: Request,
):
    actor = _require_admin_client(request, client_id)
    try:
        return retry_client_data_import_staging_cleanup(
            client_id=client_id,
            batch_id=batch_id,
            actor=actor,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/knowledge/extractions/{proposal_id}",
    response_model=ClientExtractionProposalView,
)
def review_extraction_api(
    client_id: int, proposal_id: int, body: ClientExtractionProposalUpdate
):
    try:
        return review_extraction_proposal(client_id, proposal_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/contacts",
    response_model=list[ClientContactView],
)
def list_client_contacts_api(
    client_id: int,
    include_inactive: bool = Query(False),
):
    try:
        return list_client_contacts(
            client_id, include_inactive=include_inactive
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/contacts",
    response_model=ClientContactView,
)
def create_client_contact_api(client_id: int, body: ClientContactCreate):
    try:
        return create_client_contact(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/knowledge/contacts/{contact_id}",
    response_model=ClientContactView,
)
def update_client_contact_api(
    client_id: int, contact_id: int, body: ClientContactUpdate
):
    try:
        return update_client_contact(client_id, contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/contacts/{contact_id}/deactivate",
    response_model=ClientContactView,
)
def deactivate_client_contact_api(client_id: int, contact_id: int):
    try:
        return deactivate_client_contact(client_id, contact_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/extractions/{proposal_id}/split-contacts",
    response_model=ClientContactSplitPreview,
)
def preview_split_contacts_api(client_id: int, proposal_id: int):
    try:
        return preview_split_contact_proposal(client_id, proposal_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/extractions/{proposal_id}/split-contacts",
)
def create_split_contacts_api(
    client_id: int,
    proposal_id: int,
    body: ClientContactSplitCreateRequest | None = None,
):
    try:
        people = body.people if body is not None else None
        created = create_split_contact_proposals(
            client_id, proposal_id, people=people
        )
        return {
            "parent_proposal_id": proposal_id,
            "created_count": len(created),
            "proposals": created,
            "message": (
                f"Created {len(created)} individual Pending Client Contact proposals. "
                "Parent proposal remains Pending — approve each person separately."
            ),
        }
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/appointment-imports",
    response_model=ClientAppointmentImportBatchView,
)
async def start_appointment_import_api(
    client_id: int,
    file: UploadFile = File(...),
):
    try:
        content = await file.read()
        return start_appointment_import(
            client_id, filename=file.filename or "grid.csv", content=content
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/appointment-imports/{batch_id}",
    response_model=ClientAppointmentImportBatchView,
)
def get_appointment_import_api(client_id: int, batch_id: int):
    try:
        return get_appointment_import_batch(client_id, batch_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/appointment-imports/{batch_id}/map",
    response_model=ClientAppointmentImportPreview,
)
def map_appointment_import_api(
    client_id: int, batch_id: int, body: ClientAppointmentImportMapRequest
):
    try:
        return map_appointment_import_columns(client_id, batch_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/appointment-imports/{batch_id}/confirm",
    response_model=ClientAppointmentImportBatchView,
)
def confirm_appointment_import_api(
    client_id: int, batch_id: int, body: ClientAppointmentImportConfirmRequest
):
    try:
        return confirm_appointment_import(client_id, batch_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc




# --- Phase 3 Engagement workbook import ---


@app.post(
    "/api/clients/{client_id}/knowledge/engagement-imports",
    response_model=EngagementImportBatchView,
)
async def start_engagement_import_api(client_id: int, file: UploadFile = File(...)):
    try:
        content = await file.read()
        return start_engagement_import(
            client_id, filename=file.filename or "workbook.xlsx", content=content
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/engagement-imports/{batch_id}",
    response_model=EngagementImportBatchView,
)
def get_engagement_import_api(client_id: int, batch_id: int):
    try:
        return get_engagement_import_batch(client_id, batch_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/engagement-imports/{batch_id}/classify",
    response_model=EngagementImportBatchView,
)
def classify_engagement_sheets_api(
    client_id: int, batch_id: int, body: EngagementImportClassifyRequest
):
    try:
        return update_sheet_classifications(client_id, batch_id, body.sheets)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/engagement-imports/{batch_id}/map",
    response_model=EngagementImportPreview,
)
def map_engagement_import_api(
    client_id: int, batch_id: int, body: EngagementImportMapRequest
):
    try:
        sheet_mappings = {int(k): v for k, v in (body.sheet_mappings or {}).items()}
        return map_engagement_import(client_id, batch_id, sheet_mappings or None)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/knowledge/engagement-imports/{batch_id}/preview",
    response_model=EngagementImportPreview,
)
def preview_engagement_import_api(
    client_id: int,
    batch_id: int,
    filter_status: str | None = Query(default=None),
):
    try:
        return get_engagement_import_preview(
            client_id, batch_id, filter_status=filter_status
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/knowledge/engagement-imports/{batch_id}/confirm",
    response_model=EngagementImportBatchView,
)
def confirm_engagement_import_api(
    client_id: int, batch_id: int, body: EngagementImportConfirmRequest
):
    try:
        return confirm_engagement_import(client_id, batch_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/sales-events",
    response_model=list[SalesEventView],
)
def list_sales_events_api(
    client_id: int,
    company_id: int | None = Query(default=None),
    contact_id: int | None = Query(default=None),
    event_family: str | None = Query(default=None),
    rev_spec: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    appointment_grade: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    company_name: str | None = Query(default=None),
    contact_name: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    try:
        return list_sales_events(
            client_id,
            company_id=company_id,
            contact_id=contact_id,
            event_family=event_family,
            rev_spec=rev_spec,
            outcome=outcome,
            appointment_grade=appointment_grade,
            date_from=date_from,
            date_to=date_to,
            company_name=company_name,
            contact_name=contact_name,
            limit=limit,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/contacts", response_model=ContactsResponse)
def list_contacts_api(
    client_id: int | None = Query(default=None),
    all_clients: bool = Query(default=False),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """CRM contact list for the Active Client (or all assigned clients). Read-only."""
    try:
        result = list_contacts(
            client_id=client_id,
            all_clients=all_clients,
            q=q,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    client = get_active_client(client_id=client_id, all_clients=all_clients)
    return ContactsResponse(
        client=client,
        contacts=result["contacts"],
        total=int(result["total"]),
        client_total=int(result["client_total"]),
        offset=int(result["offset"]),
        limit=int(result["limit"]),
    )


@app.post("/api/contacts/manual/preview", response_model=ManualContactPreviewResponse)
def preview_manual_contact_api(body: ManualContactPreviewRequest):
    """Read-only duplicate check before creating or linking a CRM contact."""
    try:
        return preview_manual_contact(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/contacts/manual", response_model=ManualContactSaveResult)
def save_manual_contact_api(body: ManualContactSaveRequest):
    """Create one shared master contact or link an existing one to this client/company."""
    try:
        return save_manual_contact(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/companies/manual/preview", response_model=ManualCompanyPreviewResponse)
def preview_manual_company_api(body: ManualCompanyPreviewRequest):
    """Read-only duplicate check before creating or linking a CRM company."""
    try:
        return preview_manual_company(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/companies/manual", response_model=ManualCompanySaveResult)
def save_manual_company_api(body: ManualCompanySaveRequest):
    """Create one shared master company or link it to this client."""
    try:
        return save_manual_company(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/contacts/{contact_id}/zoominfo/preview",
    response_model=ZoomInfoContactPreviewResponse,
)
def preview_zoominfo_contact_api(contact_id: int, body: ZoomInfoContactPreviewRequest):
    """Compare NorthStar vs ZoomInfo values. Does not write."""
    try:
        return preview_zoominfo_contact_update(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/contacts/{contact_id}/zoominfo/apply",
    response_model=ZoomInfoContactApplyResult,
)
def apply_zoominfo_contact_api(contact_id: int, body: ZoomInfoContactApplyRequest):
    """Apply only the ZoomInfo fields the user selected. Cancel is a no-op (do not call)."""
    try:
        return apply_zoominfo_contact_update(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/zoominfo/add/preview", response_model=ZoomInfoAddPreviewResponse)
def preview_zoominfo_add_api(body: ZoomInfoAddPreviewRequest):
    """Duplicate check before adding a ZoomInfo company or contact. Never auto-creates."""
    try:
        return ZoomInfoAddPreviewResponse(**preview_zoominfo_add(body))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/zoominfo/add", response_model=ZoomInfoAddResult)
def save_zoominfo_add_api(body: ZoomInfoAddSaveRequest):
    """Create or link a ZoomInfo company/contact after an explicit user decision."""
    try:
        return save_zoominfo_add(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/contacts/{contact_id}", response_model=ContactWorkspace)
def get_contact_workspace_api(
    contact_id: int, client_id: int | None = Query(default=None)
):
    try:
        return get_contact_workspace(contact_id, client_id=client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/contacts/{contact_id}/phone")
def patch_contact_phone_api(contact_id: int, body: ContactPhoneUpdate):
    """Update structured phone / extension. Does not concatenate into stored main phone."""
    try:
        return update_contact_phones(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/contacts/{contact_id}/assign", response_model=ContactAssignResult)
def post_contact_assign(contact_id: int, body: ContactAssignRequest):
    """Add an existing shared contact to the Active Client. Never duplicates the person."""
    try:
        result = assign_shared_contact(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ContactAssignResult(**result)


@app.patch("/api/contacts/{contact_id}/workflow", response_model=ContactWorkflowResult)
def patch_contact_workflow(contact_id: int, body: ContactWorkflowUpdate):
    """Save client-scoped operational workflow for a shared master contact."""
    try:
        result = update_contact_workflow(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ContactWorkflowResult(**result)


@app.post("/api/contacts/{contact_id}/activities", response_model=ContactWorkflowResult)
def post_contact_activity(contact_id: int, body: ContactActivityCreate):
    """Log Call, Add Note, or Schedule Follow-Up for the active client relationship."""
    try:
        result = create_contact_activity(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ContactWorkflowResult(**result)


@app.post("/api/contacts/{contact_id}/follow-up/complete", response_model=ContactWorkflowResult)
def post_contact_follow_up_complete(contact_id: int, body: ContactFollowUpCompleteRequest):
    """Complete the open follow-up task for this contact and Active Client."""
    try:
        result = complete_contact_follow_up(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ContactWorkflowResult(**result)


@app.post("/api/contacts/{contact_id}/follow-up/reschedule", response_model=ContactWorkflowResult)
def post_contact_follow_up_reschedule(contact_id: int, body: ContactFollowUpRescheduleRequest):
    """Update the existing open follow-up date/time without creating a duplicate."""
    try:
        result = reschedule_contact_follow_up(contact_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ContactWorkflowResult(**result)


@app.get("/api/clients/{client_id}/appointments", response_model=AppointmentListResponse)
def get_client_appointments(
    client_id: int,
    bucket: str = Query(default=""),
    q: str = Query(default=""),
    appointment_type: str = Query(default=""),
    source: str = Query(default=""),
    revenue_specialist_user_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    """List live CRM appointments for the Active Client."""
    try:
        return list_appointments(
            client_id,
            bucket=bucket,
            q=q,
            appointment_type=appointment_type,
            source=source,
            revenue_specialist_user_id=revenue_specialist_user_id,
            limit=limit,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/appointments/summary", response_model=AppointmentSummary)
def get_client_appointment_summary(client_id: int):
    """Dashboard counts and upcoming preview for live appointments."""
    try:
        return appointment_summary(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/appointments/{appointment_id}/reschedule", response_model=AppointmentActionResult)
def post_reschedule_appointment(appointment_id: int, body: AppointmentRescheduleRequest):
    try:
        return reschedule_appointment(appointment_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/appointments/{appointment_id}/cancel", response_model=AppointmentActionResult)
def post_cancel_appointment(appointment_id: int, body: AppointmentCancelRequest):
    try:
        return cancel_appointment(appointment_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/appointments/{appointment_id}/complete", response_model=AppointmentActionResult)
def post_complete_appointment(appointment_id: int, body: AppointmentCompleteRequest):
    try:
        return complete_appointment(appointment_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ask-northstar/history", response_model=AskHistoryResponse)
def ask_northstar_history(
    limit: int = Query(default=12, ge=1, le=50),
    scope: str | None = Query(default=None),
    active_client_id: int | None = Query(default=None),
):
    """Recent Ask NorthStar questions for the current user — optionally filtered by scope/client."""
    try:
        return list_ask_history(
            limit=limit,
            scope=scope,
            active_client_id=active_client_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/research/company", response_model=ResearchCompanyResponse)
def research_company_api(body: ResearchStartRequest, request: Request):
    """Research This Company — Quick (deterministic) or Deep (background job)."""
    try:
        depth = (body.research_depth or "quick").strip().lower()
        actor = resolve_staff_actor()
        if actor is None:
            raise PermissionError("Not authorized.")
        cid = body.working_for_client_id
        if depth in {"deep", "deep_research"}:
            actor = require_administrator(request)
            return start_company_research(body, user_id=actor.id)
        if not user_has_permission(int(actor.id), "research.run", client_id=cid):
            raise PermissionError("Not authorized.")
        return start_company_research(body, user_id=actor.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/research/company", response_model=ResearchCompanyResponse)
def research_company_get(
    request: Request,
    company_id: int | None = Query(default=None),
    external_record_no: str | None = Query(default=None),
    working_for_client_id: int | None = Query(default=None),
    campaign_id: int | None = Query(default=None),
    run: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
    research_depth: str = Query(default="quick"),
    confirm_paid_refresh: bool = Query(default=False),
):
    """Load latest research or start a run when run=true."""
    try:
        if run:
            depth = (research_depth or "quick").strip().lower()
            body = ResearchStartRequest(
                company_id=company_id,
                external_record_no=external_record_no,
                working_for_client_id=working_for_client_id,
                campaign_id=campaign_id,
                force_refresh=force_refresh,
                confirm_paid_refresh=confirm_paid_refresh,
                research_depth=research_depth,
            )
            if depth in {"deep", "deep_research"}:
                actor = require_administrator(request)
                return start_company_research(body, user_id=actor.id)
            actor = resolve_staff_actor()
            if actor is None:
                raise PermissionError("Not authorized.")
            if not user_has_permission(
                int(actor.id), "research.run", client_id=working_for_client_id
            ):
                raise PermissionError("Not authorized.")
            return start_company_research(body, user_id=actor.id)
        return get_latest_research(
            company_id=company_id,
            external_record_no=external_record_no,
            working_for_client_id=working_for_client_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/research/jobs/{job_id}", response_model=ResearchCompanyResponse)
def research_job_get(job_id: int):
    """Poll a Deep Research job (progress, result, errors)."""
    try:
        return get_deep_research_job(job_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/research/jobs/{job_id}/cancel", response_model=ResearchCompanyResponse)
def research_job_cancel(job_id: int, request: Request):
    """Request cancellation of an active Deep Research job (administrator-only)."""
    try:
        actor = require_administrator(request)
        return cancel_deep_research_job(job_id, user_id=actor.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/research/deep/status")
def research_deep_status():
    """Limits + monthly usage presence (never returns secrets)."""
    return deep_research_status_payload()


@app.get("/api/research/runs/{run_id}", response_model=ResearchCompanyResponse)
def research_run_get(run_id: int):
    try:
        return get_research_run(run_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/research/proposed-updates/approve")
def research_approve_api(body: ResearchApproveRequest):
    try:
        return approve_proposed_update(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/research/proposed-updates/reject")
def research_reject_api(body: ResearchRejectRequest):
    try:
        return reject_proposed_update(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
