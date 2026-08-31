from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from access import (
    WriteClientIdError,
    dashboard_scope_summary,
    get_default_user,
    get_user_by_id,
    list_clients_for_user,
)
from auth_http import add_staff_csrf_middleware, staff_auth_router
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
    list_prospects,
    list_relationship_statuses,
    update_relationship_notes,
    update_relationship_status,
)
from db import ensure_schema
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
    StatusUpdateRequest,
    UserClientsResponse,
    WorkQueueCompleteRequest,
    WorkQueueListResponse,
    WorkQueueLogCallRequest,
    WorkQueueLogCallResult,
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
from work_queue_data import (
    clients_for_work_queue,
    complete_follow_up_task,
    complete_work_queue_item,
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

app = FastAPI(
    title="NorthStar AI",
    version="1.9.1",
    description="NorthStar AI Revenue Development Platform — multi-client",
)

# CSRF is inner so CORS can still label 403s. No 401 route gate (Checkpoint B).
add_staff_csrf_middleware(app)
app.include_router(staff_auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(WriteClientIdError)
async def write_client_id_error_handler(_request, exc: WriteClientIdError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


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
):
    """Return prospects for the Active Client (or all assigned clients)."""
    prospects = list_prospects(client_id=client_id, all_clients=all_clients)
    client = get_active_client(client_id=client_id, all_clients=all_clients)
    return ProspectsResponse(client=client, prospects=prospects)


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
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


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
    return {"clients": clients_for_work_queue(user_id)}


@app.get("/api/users/default", response_model=UserClientsResponse)
def default_user_with_clients():
    """Default logged-in specialist (Julie) and assigned clients — architecture support."""
    user = get_default_user()
    if user is None:
        raise HTTPException(status_code=404, detail="Default user not found.")
    clients = list_clients_for_user(user.id, active_only=True)
    return UserClientsResponse(user=user, clients=clients)


@app.get("/api/users/{user_id}/clients", response_model=UserClientsResponse)
def user_clients(user_id: int):
    """List clients assigned to a NorthStar user (security boundary)."""
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
    """
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
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
    user = get_default_user()
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
def list_email_accounts_api(client_id: int):
    try:
        return list_email_accounts(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/email-accounts/suggestions",
    response_model=ClientEmailAccountSuggestion,
)
def suggest_email_accounts_api(client_id: int):
    """Suggest from approved Client Operations — never auto-creates."""
    try:
        return suggest_email_account_setup(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-accounts",
    response_model=ClientEmailAccountView,
)
def create_email_account_api(client_id: int, body: ClientEmailAccountUpdate):
    try:
        return upsert_email_account(client_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(
    "/api/clients/{client_id}/email-accounts/{account_id}",
    response_model=ClientEmailAccountView,
)
def update_email_account_api(
    client_id: int, account_id: int, body: ClientEmailAccountUpdate
):
    try:
        return upsert_email_account(client_id, body, account_id=account_id)
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
def deactivate_email_account_api(client_id: int, account_id: int):
    try:
        return deactivate_email_account(client_id, account_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/clients/{client_id}/email-accounts/{account_id}/connect")
def connect_email_account_api(client_id: int, account_id: int):
    """Start provider connect. Google → OAuth URL; other providers remain stubbed."""
    try:
        from client_email_accounts_data import get_email_account

        account = get_email_account(client_id, account_id)
        provider = (account.provider or "").strip().lower()
        if provider in {"google", "gmail"}:
            started = begin_google_connect(account_id)
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
        return connect_email_account_stub(client_id, account_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/email/google/status")
def google_oauth_status_api():
    return oauth_status()


@app.get("/api/email/google/connect/{account_id}")
def google_connect_start_api(account_id: int, redirect: int = 1):
    """Start Google OAuth. redirect=1 returns RedirectResponse to Google."""
    try:
        started = begin_google_connect(account_id)
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
def disconnect_google_account_api(client_id: int, account_id: int):
    try:
        return disconnect_google_account(client_id, account_id)
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
def send_client_email_api(client_id: int, body: ClientEmailSendRequest):
    """Send via Gmail after human confirm_send. Creates Email Sent only on success."""
    try:
        if body.confirm_send and body.confirm_recipient:
            if body.confirm_recipient.strip().lower() != body.to_address.strip().lower():
                raise ValueError("confirm_recipient must match to_address.")
        return send_client_email(client_id, body)
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
    client_id: int, account_id: int, body: ClientEmailAccountAssignmentUpdate
):
    try:
        return upsert_email_account_assignment(client_id, account_id, body)
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
def list_email_signatures_api(client_id: int):
    try:
        return list_email_signatures(client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-signatures",
    response_model=ClientEmailSignatureView,
)
def create_email_signature_api(client_id: int, body: ClientEmailSignatureUpdate):
    try:
        return upsert_email_signature(client_id, body)
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
    client_id: int, signature_id: int, body: ClientEmailSignatureUpdate
):
    try:
        return upsert_email_signature(client_id, body, signature_id=signature_id)
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
def deactivate_email_signature_api(client_id: int, signature_id: int):
    try:
        return deactivate_email_signature(client_id, signature_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/clients/{client_id}/email-preview/contacts")
def list_email_preview_contacts_api(client_id: int, limit: int = 100):
    try:
        return list_preview_contacts(client_id, limit=limit)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get(
    "/api/clients/{client_id}/email-preview/appointments",
    response_model=list[EmailPreviewAppointmentOption],
)
def list_email_preview_appointments_api(
    client_id: int,
    contact_id: int = Query(...),
    limit: int = 50,
):
    """Read-only appointments for Preview Email, scoped to a CRM contact."""
    try:
        return list_preview_appointments(
            client_id, contact_id, limit=limit
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/clients/{client_id}/email-preview",
    response_model=ClientEmailPreviewResult,
)
def preview_client_email_api(client_id: int, body: ClientEmailPreviewRequest):
    """Read-only From/To/Subject/Body/Signature render. Does not send."""
    try:
        return preview_client_email(client_id, body)
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
def research_company_api(body: ResearchStartRequest):
    """Phase 2A Research This Company — NorthStar-first + public web; no auto CRM writes."""
    try:
        return start_company_research(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/research/company", response_model=ResearchCompanyResponse)
def research_company_get(
    company_id: int | None = Query(default=None),
    external_record_no: str | None = Query(default=None),
    working_for_client_id: int | None = Query(default=None),
    campaign_id: int | None = Query(default=None),
    run: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
):
    """Load latest research or start a run when run=true."""
    try:
        if run:
            return start_company_research(
                ResearchStartRequest(
                    company_id=company_id,
                    external_record_no=external_record_no,
                    working_for_client_id=working_for_client_id,
                    campaign_id=campaign_id,
                    force_refresh=force_refresh,
                )
            )
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
