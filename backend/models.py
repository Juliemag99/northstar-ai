"""Pydantic models for NorthStar Carmeco API responses."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

# Writes must send an explicit client. 0 (All My Clients) is never valid.
PositiveClientId = Annotated[int, Field(gt=0)]


class ActiveClient(BaseModel):
    id: str  # client code, or "all" for All My Clients
    name: str
    seed_file: str = ""
    prospect_count: int = 0
    company_count: int = 0
    contact_count: int = 0
    client_id: int = 0
    code: str = ""
    mode: str = "selected_client"  # selected_client | all_my_clients


class ContactSummary(BaseModel):
    id: int
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    phone: str = ""
    alt_phone: str = ""
    email: str = ""
    external_record_no: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class ContactListItem(BaseModel):
    """CRM prospect contact row for the Contacts list page."""

    id: int
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    phone: str = ""
    alt_phone: str = ""
    email: str = ""
    company_id: int | None = None
    company_name: str = ""
    company_record_no: str = ""
    city: str = ""
    state: str = ""
    client_id: int = 0
    client_name: str = ""
    client_code: str = ""
    relationship_id: int = 0
    status: str = ""


class ContactsResponse(BaseModel):
    client: ActiveClient
    contacts: list[ContactListItem]
    total: int = 0
    client_total: int = 0
    offset: int = 0
    limit: int = 50


class ClientRelationshipStatusChip(BaseModel):
    """One accessible client-company relationship status for All My Clients rows."""

    client_id: int
    client_code: str = ""
    client_name: str = ""
    status: str = ""
    relationship_id: int = 0


class ProspectListItem(BaseModel):
    """Prospects table / dashboard row for a client-company relationship."""

    id: int
    external_record_no: str
    company: str
    city: str = ""
    state: str = ""
    status: str = ""
    # Client-neutral alias of status (CCR status for the Active/Working client).
    relationship_status: str = ""
    # All My Clients: labeled statuses for every accessible client relationship.
    # Empty in single-client mode (use status / relationship_status instead).
    client_statuses: list[ClientRelationshipStatusChip] = Field(default_factory=list)
    primary_contact: str = ""
    phone: str = ""
    last_updated: str = ""
    website: str = ""
    address: str = ""
    zip: str = ""
    customer_campaign: str = ""
    contact_count: int = 0
    is_hot: bool = False
    has_appointment_set: bool = False
    has_quote: bool = False
    has_purchase_order: bool = False
    has_weblead: bool = False
    next_action: str = ""
    follow_up_date: str | None = None
    call_due: bool = False
    follow_up_due: bool = False
    client_id: int = 0
    client_code: str = ""
    client_name: str = ""
    relationship_id: int = 0


class LegacyNote(BaseModel):
    id: int
    note_text: str
    source_field: str = "Sales Rep Comments/Notes"
    created_at: str = ""


class CompanyWorkspace(BaseModel):
    id: int
    external_record_no: str
    company_name: str
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    website: str = ""
    sales_volume_range: str = ""
    location_sales_volume_range: str = ""
    employee_size_range: str = ""
    primary_sic_code: str = ""
    primary_sic_description: str = ""
    primary_naics_code: str = ""
    primary_naics_description: str = ""
    sic_8_digit: str = ""
    sic_8_digit_description: str = ""
    type_of_industry: str = ""
    customer_campaign: str = ""
    entered_at: str = ""
    last_updated_at: str = ""
    status: str = ""
    # Client-neutral alias of status (CCR status for the Active/Working client).
    relationship_status: str = ""
    is_hot: bool = False
    legacy_phone: str = ""
    legacy_email: str = ""
    contacts: list[ContactSummary] = Field(default_factory=list)
    legacy_notes: list[LegacyNote] = Field(default_factory=list)
    milestones: list["RevenueMilestone"] = Field(default_factory=list)
    client_history: list["ClientMilestoneHistory"] = Field(default_factory=list)
    sales_events: list["SalesEventView"] = Field(default_factory=list)
    recommendation: "NorthStarRecommendation | None" = None
    client_id: int = 0
    client_code: str = ""
    client_name: str = ""
    relationship_id: int = 0
    master_external_record_no: str = ""


class ProspectsResponse(BaseModel):
    client: ActiveClient
    prospects: list[ProspectListItem]
    total: int = 0
    client_total: int = 0
    offset: int = 0
    limit: int = 50
    has_previous: bool = False
    has_next: bool = False


class ImportStats(BaseModel):
    companies_imported: int
    contacts_imported: int
    notes_imported: int
    company_count: int
    contact_count: int
    orphan_contacts_in_db: int
    unmatched_record_nos: int
    db_path: str


class ClientStatusList(BaseModel):
    """Distinct relationship statuses for one client (or All My Clients union)."""

    client: str = ""
    client_id: int = 0
    statuses: list[str] = Field(default_factory=list)


class NextActionChoice(BaseModel):
    code: str
    label: str
    aliases: list[str] = Field(default_factory=list)
    requires_detail: bool = False


class NextActionCatalog(BaseModel):
    """Client-configurable Next Action choices. Global defaults when a client has none."""

    client_id: int = 0
    choices: list[NextActionChoice] = Field(default_factory=list)
    default_for_closed_code: str = "no_next_action"


# Backward-compatible alias
CarmecoStatusList = ClientStatusList


class StatusUpdateRequest(BaseModel):
    client: str = ""
    client_id: PositiveClientId
    status: str
    user: str = "Julie Magnani"


class NotesUpdateRequest(BaseModel):
    client: str = ""
    client_id: PositiveClientId
    note_text: str = ""
    user: str = "Julie Magnani"


class FieldUpdateResponse(BaseModel):
    external_record_no: str
    client: str = ""
    client_id: int = 0
    field_name: str
    old_value: str = ""
    new_value: str = ""
    changed_by: str = ""
    changed_at: str = ""
    workspace: CompanyWorkspace | None = None


class Activity(BaseModel):
    """Client-scoped CRM activity tied to a client-company relationship."""

    activity_id: int
    client_id: int
    external_record_no: str
    company_id: int
    relationship_id: int
    user_id: int | None = None
    contact_id: int | None = None
    activity_type: str
    activity_at: str
    outcome: str = ""
    notes: str = ""
    follow_up_at: str | None = None
    assigned_user: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


class ActivityCreateRequest(BaseModel):
    client: str = ""
    client_id: PositiveClientId
    external_record_no: str
    user_id: int | None = None
    contact_id: int | None = None
    activity_type: str
    activity_at: str = ""
    outcome: str = ""
    notes: str = ""
    follow_up_at: str | None = None
    assigned_user: str = "Julie Magnani"
    created_by: str = "Julie Magnani"


class ActivityListResponse(BaseModel):
    client: str = ""
    count: int = 0
    activities: list[Activity] = Field(default_factory=list)


class ActivityTimelineRow(BaseModel):
    """One Activities-page timeline row for the Active Client only."""

    item_key: str
    source: str = "activity"
    activity_id: int | None = None
    client_id: int
    company_id: int
    relationship_id: int = 0
    external_record_no: str = ""
    company_name: str = ""
    contact_id: int | None = None
    contact_name: str = ""
    activity_type: str
    activity_at: str
    user_name: str = ""
    user_id: int | None = None
    outcome: str = ""
    notes: str = ""
    status: str = ""


class ActivityTimelineResponse(BaseModel):
    client_id: int
    count: int = 0
    total: int = 0
    limit: int = 50
    offset: int = 0
    activity_types: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    items: list[ActivityTimelineRow] = Field(default_factory=list)


class NorthStarUser(BaseModel):
    id: int
    email: str
    full_name: str
    is_administrator: bool = False
    is_internal_northstar: bool = True
    active: bool = True
    created_at: str = ""


class ClientAssignment(BaseModel):
    client_id: int
    client_code: str
    client_name: str
    role: str = ""
    active: bool = True
    assigned_at: str = ""


class WorkQueueItem(BaseModel):
    """One scheduled work-queue action (foundation for future NorthStar Work Queue)."""

    id: int | None = None
    source: str = ""
    source_id: int | None = None
    client_id: int
    company_id: int
    relationship_id: int
    external_record_no: str = ""
    company_name: str = ""
    contact_id: int | None = None
    action_type: str
    due_date: str
    due_time: str = ""
    assigned_user: str = ""
    assigned_user_id: int | None = None
    completion_status: str = "open"
    priority: str = ""


class WorkQueueSummary(BaseModel):
    client: str = ""
    calls_due_today: int = 0
    follow_ups_due: int = 0
    new_assignments: int = 0
    calls: list[WorkQueueItem] = Field(default_factory=list)
    follow_ups: list[WorkQueueItem] = Field(default_factory=list)


class WorkQueueNorthStarInsight(BaseModel):
    """Observation-only advisory overlay for a Work Queue row. Never drives ranking."""

    evaluated: bool = False
    fit: str = "Not Evaluated"
    engagement: str = "Not Evaluated"
    recommendation: str = "Not Evaluated"
    why: str = ""
    evidence_used: list[str] = Field(default_factory=list)
    still_need_to_know: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    alignment: str = "Insufficient AI Data"  # Aligned | Review | Insufficient AI Data
    review_reason: str = ""
    review_flag: str = ""
    advisory_note: str = (
        "Observation only — NorthStar Insight does not change Work Queue ranking, "
        "priority scores, or next actions."
    )


class WorkQueueInsightSummary(BaseModel):
    """Observation counts across the ranked queue (before AI display filters)."""

    aligned: int = 0
    review: int = 0
    insufficient_ai_data: int = 0
    evaluated: int = 0
    not_evaluated: int = 0


class WorkQueueRow(BaseModel):
    """One actionable Work Queue row for Rev Development Specialists."""

    queue_item_id: str
    work_type: str
    work_priority: int = 0
    priority_label: str = "Low"
    client_id: int
    client_code: str = ""
    client_name: str = ""
    company_id: int
    relationship_id: int
    external_record_no: str = ""
    company_name: str = ""
    city: str = ""
    state: str = ""
    contact_id: int | None = None
    contact_name: str = ""
    status: str = ""
    due_date: str | None = None
    due_time: str = ""
    is_overdue: bool = False
    last_activity_at: str | None = None
    last_activity_summary: str = ""
    next_action: str = ""
    why_in_queue: str = ""
    is_hot: bool = False
    is_weblead: bool = False
    weblead_age_label: str | None = None
    is_new_weblead: bool = False
    is_cross_client: bool = False
    opportunity_score: int | None = None
    source_client_summary: str = ""
    assigned_user: str = ""
    assigned_user_id: int | None = None
    source: str = ""
    source_id: int | None = None
    completion_status: str = "open"
    northstar_insight: WorkQueueNorthStarInsight | None = None


class WorkQueueSummaryV2(BaseModel):
    calls_due: int = 0
    follow_ups_due: int = 0
    appointments: int = 0
    hot: int = 0
    webleads: int = 0
    new_assignments: int = 0
    needs_next_action: int = 0
    cross_client_opportunities: int = 0
    overdue: int = 0


class WorkQueueListResponse(BaseModel):
    user_id: int = 0
    mode: str = "all_my_clients"
    client_ids: list[int] = Field(default_factory=list)
    count: int = 0
    total: int = 0
    offset: int = 0
    limit: int = 50
    has_previous: bool = False
    has_next: bool = False
    summary: WorkQueueSummaryV2 = Field(default_factory=WorkQueueSummaryV2)
    items: list[WorkQueueRow] = Field(default_factory=list)
    northstar_insight_summary: WorkQueueInsightSummary = Field(
        default_factory=WorkQueueInsightSummary
    )


class WorkQueueNextResponse(BaseModel):
    """Next eligible Work Queue item after a completed/current queue_item_id."""

    user_id: int = 0
    mode: str = "all_my_clients"
    client_ids: list[int] = Field(default_factory=list)
    after_queue_item_id: str = ""
    has_next: bool = False
    end_of_results: bool = True
    total: int = 0
    position: int | None = None
    item: WorkQueueRow | None = None
    message: str = ""


class WorkQueueCompleteRequest(BaseModel):
    client_id: PositiveClientId
    source: str
    source_id: int | None = None
    company_id: int | None = None


class FollowUpTaskCompleteRequest(BaseModel):
    """Complete one open follow-up task (contact or company-level)."""

    client_id: PositiveClientId
    source: str
    source_id: int
    company_id: int
    contact_id: int | None = None
    notes: str = ""
    created_by: str = "Julie Magnani"


class FollowUpTaskRescheduleRequest(BaseModel):
    """Update the same open follow-up task — never create a duplicate."""

    client_id: PositiveClientId
    source: str
    source_id: int
    company_id: int
    contact_id: int | None = None
    follow_up_date: str
    follow_up_time: str
    assigned_user_id: int | None = None
    next_action: str = ""
    notes: str = ""
    created_by: str = "Julie Magnani"


class FollowUpTaskActionResult(BaseModel):
    ok: bool = True
    message: str = ""
    activity_id: int | None = None
    follow_up_activity_id: int | None = None
    client_id: int = 0
    source: str = ""
    source_id: int | None = None


class WorkQueueLogCallRequest(BaseModel):
    """Log a call from Work Queue Company Workspace (client-scoped)."""

    client_id: PositiveClientId
    external_record_no: str
    contact_id: int | None = None
    outcome: str = ""
    notes: str = ""
    status: str | None = None
    next_action: str = ""
    follow_up_date: str | None = None
    follow_up_time: str = ""
    queue_source: str = ""
    queue_source_id: int | None = None
    complete_current: bool = True
    created_by: str = "Julie Magnani"
    appointment: AppointmentDetailsPayload | None = None


class WorkQueueLogCallResult(BaseModel):
    ok: bool = True
    message: str = ""
    activity_id: int | None = None
    follow_up_activity_id: int | None = None
    client_id: int = 0
    external_record_no: str = ""
    completed_queue_item: bool = False
    appointment_id: int | None = None


class OutreachLogRequest(BaseModel):
    """Log outreach from Company Workspace (client-scoped)."""

    client_id: PositiveClientId
    external_record_no: str
    contact_id: int | None = None
    outreach_type: str = "Call"  # Call | Email | Other
    outcome: str = ""
    notes: str = ""
    next_action: str = ""
    follow_up_date: str | None = None
    return_next: bool = False
    created_by: str = "Julie Magnani"


class OutreachLogResult(BaseModel):
    ok: bool = True
    message: str = ""
    activity_id: int | None = None
    follow_up_activity_id: int | None = None
    client_id: int = 0
    external_record_no: str = ""
    status_updated: bool = False
    applied_status: str = ""
    open_email_compose: bool = False
    open_appointment_workflow: bool = False
    next_external_record_no: str | None = None


class PriorityProspectItem(BaseModel):
    client_id: int
    client_name: str = ""
    company_id: int
    relationship_id: int
    external_record_no: str
    company_name: str = ""
    status: str = ""
    primary_contact: str = ""
    next_action: str = ""
    follow_up_date: str | None = None
    is_hot: bool = False
    priority_bucket: int = 0
    priority_reason: str = ""
    sort_key: str = ""


class PriorityProspectsResponse(BaseModel):
    client_id: int
    client_name: str = ""
    as_of_date: str = ""
    count: int = 0
    items: list[PriorityProspectItem] = Field(default_factory=list)


class ClientCompanyWorkItem(BaseModel):
    """One client-company relationship row for multi-client work queues."""

    relationship_id: int
    client_id: int
    client_code: str
    client_name: str
    company_id: int
    external_record_no: str
    company_name: str
    status: str = ""
    assigned_user_id: int | None = None
    priority: str = ""
    next_action: str = ""
    follow_up_date: str | None = None
    notes: str = ""


class DashboardScopeSummary(BaseModel):
    mode: str
    user_id: int
    client_ids: list[int] = Field(default_factory=list)
    relationship_count: int = 0
    calls_due_today: int = 0
    follow_ups_due: int = 0
    appointments_today: int = 0
    hot_prospects: int = 0
    new_assignments: int = 0


class DashboardFollowUpItem(BaseModel):
    """One open follow-up task for the Active Client dashboard."""

    client_id: int
    company_id: int
    contact_id: int | None = None
    contact_name: str = ""
    company_name: str = ""
    external_record_no: str = ""
    due_date: str
    due_time: str = ""
    assigned_user: str = ""
    assigned_user_id: int | None = None
    status: str = ""
    source: str = ""
    source_id: int | None = None
    bucket: str = "upcoming"
    next_action: str = ""


class DashboardFollowUpsResponse(BaseModel):
    client_id: int
    overdue: list[DashboardFollowUpItem] = Field(default_factory=list)
    due_today: list[DashboardFollowUpItem] = Field(default_factory=list)
    upcoming: list[DashboardFollowUpItem] = Field(default_factory=list)
    overdue_count: int = 0
    due_today_count: int = 0
    upcoming_count: int = 0


class UserClientsResponse(BaseModel):
    user: NorthStarUser
    clients: list[ClientAssignment] = Field(default_factory=list)


class SearchHit(BaseModel):
    doc_type: str
    client_id: int
    client_name: str = ""
    client_code: str = ""
    company_id: int | None = None
    company_name: str = ""
    external_record_no: str = ""
    contact_id: int | None = None
    contact_name: str | None = None
    source_table: str = ""
    source_id: int | None = None
    note_type: str | None = None
    title: str = ""
    snippet: str = ""
    event_at: str | None = None
    created_by: str | None = None
    workspace_path: str | None = None
    focus_target: str | None = None


class SearchResponse(BaseModel):
    query: str
    mode: str = "all_my_clients"
    client_ids: list[int] = Field(default_factory=list)
    total: int = 0
    companies: list[SearchHit] = Field(default_factory=list)
    contacts: list[SearchHit] = Field(default_factory=list)
    notes: list[SearchHit] = Field(default_factory=list)


class RevenueMilestone(BaseModel):
    milestone_id: int
    client_id: int
    client_name: str = ""
    client_code: str = ""
    company_id: int
    relationship_id: int
    external_record_no: str
    contact_id: int | None = None
    contact_name: str | None = None
    milestone_type: str
    milestone_date: str
    amount: float | None = None
    reference_number: str = ""
    source: str = ""
    notes: str = ""
    created_by: str = ""
    created_at: str = ""
    source_activity_id: int | None = None


class MilestoneCreateRequest(BaseModel):
    client: str = ""
    client_id: PositiveClientId
    external_record_no: str
    milestone_type: str
    milestone_date: str = ""
    contact_id: int | None = None
    amount: float | None = None
    reference_number: str = ""
    source: str = ""
    notes: str = ""
    created_by: str = "Julie Magnani"
    source_activity_id: int | None = None


class SetHotRequest(BaseModel):
    client: str = ""
    client_id: PositiveClientId
    external_record_no: str
    is_hot: bool = True
    notes: str = ""
    created_by: str = "Julie Magnani"


class MilestoneSummary(BaseModel):
    mode: str
    user_id: int
    client_ids: list[int] = Field(default_factory=list)
    appointments_set: int = 0
    quotes: int = 0
    purchase_orders: int = 0
    webleads: int = 0
    hot: int = 0


class ClientMilestoneHistory(BaseModel):
    client_id: int
    client_code: str
    client_name: str
    is_hot: bool = False
    milestones: list[RevenueMilestone] = Field(default_factory=list)


class SharedHistoryClientSummary(BaseModel):
    client_id: int
    client_code: str
    client_name: str
    external_record_no: str = ""
    status: str = ""
    is_hot: bool = False


class SharedHistoryItem(BaseModel):
    """One history entry — NorthStar activity or legacy note (shared or distinct)."""

    item_key: str
    item_type: str  # activity | shared_legacy | legacy_note | milestone | shared_history_event
    section: str = ""  # northstar | shared_legacy | distinct_legacy | shared_company_history
    legacy_scope: str = ""  # shared | distinct | ""
    client_id: int
    client_code: str = ""
    client_name: str = ""
    external_record_no: str = ""
    title: str = ""
    body: str = ""
    event_at: str = ""
    created_by: str = ""
    activity_type: str = ""
    milestone_type: str = ""
    source_table: str = ""
    source_id: int | None = None
    shared_client_names: list[str] = Field(default_factory=list)
    attribution: str = ""
    attribution_evidence: str = ""
    source_file: str = ""
    event_hash: str = ""


class SharedHistoryResponse(BaseModel):
    company_id: int
    company_name: str = ""
    can_view_cross_client: bool = False
    filter_client_id: int | None = None
    clients: list[SharedHistoryClientSummary] = Field(default_factory=list)
    items: list[SharedHistoryItem] = Field(default_factory=list)
    northstar_items: list[SharedHistoryItem] = Field(default_factory=list)
    shared_legacy_items: list[SharedHistoryItem] = Field(default_factory=list)
    distinct_legacy_items: list[SharedHistoryItem] = Field(default_factory=list)
    shared_company_history_items: list[SharedHistoryItem] = Field(default_factory=list)
    count: int = 0


class CrossClientOpportunity(BaseModel):
    company_id: int
    external_record_no: str
    company_name: str
    city: str = ""
    state: str = ""
    target_client_id: int = 0
    target_client_code: str = ""
    target_client_name: str = ""
    other_clients: list[dict] = Field(default_factory=list)
    strongest_signal: str = ""
    signal_history: list["OpportunitySignal"] = Field(default_factory=list)
    signal_types: list[str] = Field(default_factory=list)
    target_client_status: str = ""
    target_client_activity: str = ""
    last_target_activity: str | None = None
    opportunity_score: int = 0
    score_label: str = "Low"
    proven_buyer: bool = False
    engaged_elsewhere: bool = False
    has_target_relationship: bool = False
    workspace_path: str = ""
    review_status: str = "New Opportunity"
    recommended_action: str = ""
    why_summary: str = ""
    source_client_names: list[str] = Field(default_factory=list)
    # Legacy / foundation fields
    focus_client_id: int = 0
    other_client_id: int = 0
    other_client_code: str = ""
    other_client_name: str = ""
    other_milestone_types: list[str] = Field(default_factory=list)
    reason: str = ""


class OpportunitySignal(BaseModel):
    """Privacy-safe cross-client signal — no notes, amounts, or references."""

    client_id: int
    client_code: str = ""
    client_name: str = ""
    milestone_type: str
    milestone_date: str = ""


class ScoreConfig(BaseModel):
    purchase_order: int = 40
    quote: int = 28
    appointment_set: int = 18
    weblead: int = 10
    hot: int = 10
    multi_client_bonus: int = 6
    recency_90_days: int = 10
    recency_180_days: int = 5
    recency_365_days: int = 2
    never_worked_bonus: int = 15
    has_target_contacts_bonus: int = 4


class CrossClientOpportunityList(BaseModel):
    target_client_id: int
    target_client_code: str = ""
    target_client_name: str = ""
    count: int = 0
    opportunities: list[CrossClientOpportunity] = Field(default_factory=list)
    score_config: ScoreConfig = Field(default_factory=ScoreConfig)
    data_note: str = ""


class OpportunityDismissRequest(BaseModel):
    target_client_id: PositiveClientId
    company_id: int
    reason: str
    notes: str = ""
    dismissed_by: str = "Julie Magnani"


class OpportunityAddRequest(BaseModel):
    target_client_id: PositiveClientId
    company_id: int
    status: str = "New"
    created_by: str = "Julie Magnani"
    opportunity_score: int | None = None
    source_summary: str = ""
    # Future: evaluate against Target Client + Target Campaign (not every campaign)
    target_campaign_id: int | None = None


class OpportunityReviewRequest(BaseModel):
    target_client_id: PositiveClientId
    company_id: int
    notes: str = ""
    reviewed_by: str = "Julie Magnani"


class OpportunityActionResult(BaseModel):
    action: str
    company_id: int
    external_record_no: str = ""
    target_client_id: int
    relationship_id: int | None = None
    created_new_relationship: bool = False
    message: str = ""
    workspace_path: str = ""


# --- Ask NorthStar (Phase 1 read-only retrieval) ---


class AskNorthStarRequest(BaseModel):
    question: str
    scope: str = "all"  # all | active_client
    active_client_id: int | None = None


class AskSource(BaseModel):
    label: str
    detail: str = ""


class AskLink(BaseModel):
    label: str
    href: str


class AskResearchOption(BaseModel):
    id: str
    label: str
    status: str = "Coming Next"
    enabled: bool = False
    description: str = ""


class AskSection(BaseModel):
    id: str
    title: str
    body: str = ""
    items: list[dict] = Field(default_factory=list)


class AskCompanyCard(BaseModel):
    company_id: int
    company_name: str
    external_record_no: str = ""
    client_id: int | None = None
    client_name: str = ""
    status: str = ""
    city: str = ""
    state: str = ""
    badges: list[str] = Field(default_factory=list)
    why: str = ""
    opportunity_score: int | None = None
    workspace_path: str = ""
    rank: int | None = None
    next_action: str = ""
    work_priority: int | None = None
    work_type: str = ""
    # Observation-only supplemental fields (do not affect Work Next ranking)
    northstar_recommendation: str = ""
    northstar_fit: str = ""
    northstar_engagement: str = ""
    northstar_alignment: str = ""
    northstar_recommendation_why: str = ""
    northstar_insight_note: str = (
        "Observation only — does not change Work Queue Next Action."
    )


class AskContactCard(BaseModel):
    contact_id: int | None = None
    name: str = ""
    title: str = ""
    phone: str = ""
    email: str = ""
    client_id: int | None = None
    client_name: str = ""
    source: str = "NorthStar CRM"


class AskNoteHit(BaseModel):
    company_id: int | None = None
    company_name: str = ""
    external_record_no: str = ""
    client_id: int | None = None
    client_name: str = ""
    excerpt: str = ""
    event_at: str = ""
    source: str = "NorthStar CRM"
    doc_type: str = "note"
    workspace_path: str = ""


class AskMilestoneBadge(BaseModel):
    milestone_type: str
    label: str = ""
    milestone_date: str = ""
    client_id: int | None = None
    client_name: str = ""
    source: str = "NorthStar CRM"


class AskNorthStarResponse(BaseModel):
    question: str
    intent: str = ""
    summary: str = ""
    scope: str = "all"
    scope_label: str = ""
    active_client_id: int | None = None
    active_client_name: str = ""
    no_data: bool = False
    read_only: bool = True
    result_count: int = 0
    sections: list[AskSection] = Field(default_factory=list)
    companies: list[AskCompanyCard] = Field(default_factory=list)
    contacts: list[AskContactCard] = Field(default_factory=list)
    notes: list[AskNoteHit] = Field(default_factory=list)
    milestones: list[AskMilestoneBadge] = Field(default_factory=list)
    sources: list[AskSource] = Field(default_factory=list)
    recommended_links: list[AskLink] = Field(default_factory=list)
    research_options: list[AskResearchOption] = Field(default_factory=list)
    research_available: bool = False
    history_id: int | None = None


class AskHistoryItem(BaseModel):
    id: int
    question: str
    scope: str = ""
    active_client_id: int | None = None
    active_client_name: str = ""
    intent: str = ""
    answer_summary: str = ""
    created_at: str = ""


class AskHistoryResponse(BaseModel):
    items: list[AskHistoryItem] = Field(default_factory=list)
    count: int = 0
    filter_scope: str = ""
    filter_client_id: int | None = None
    filter_client_name: str = ""


# --- Research Company (Phase 2A) ---


class ResearchStartRequest(BaseModel):
    company_id: int | None = None
    external_record_no: str | None = None
    working_for_client_id: int | None = None
    campaign_id: int | None = None
    force_refresh: bool = False
    # Required for a paid Deep Research re-run inside the cache window.
    confirm_paid_refresh: bool = False
    # quick = deterministic public web; deep = OpenAI Responses + web_search job
    research_depth: str = "quick"


class ResearchCitation(BaseModel):
    url: str = ""
    title: str = ""


class ResearchJobView(BaseModel):
    job_id: int
    company_id: int
    working_for_client_id: int
    campaign_id: int | None = None
    research_run_id: int | None = None
    research_depth: str = "deep"
    status: str = "queued"  # queued|running|completed|failed|cancelled
    progress: int = 0
    progress_message: str = ""
    cancel_requested: bool = False
    attempt_count: int = 0
    max_attempts: int = 2
    openai_response_id: str = ""
    openai_model: str = ""
    usage: dict = Field(default_factory=dict)
    citations: list[dict] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    error_message: str = ""
    started_at: str = ""
    completed_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    deep_research_configured: bool = False
    from_cache: bool = False
    paid_refresh_required: bool = False
    limited_by: str = ""


class ResearchFindingView(BaseModel):
    id: int | None = None
    finding_type: str
    field_key: str = ""
    value: str = ""
    source_name: str = ""
    source_url: str = ""
    researched_at: str = ""
    confidence: str = "medium"
    evidence_level: str = "verified"  # verified | supported_inference | not_verified
    page_title: str = ""
    is_public_contact: bool = False
    contact_name: str = ""
    contact_title: str = ""
    provider_id: str = "public_web"
    why_relevant: str = ""
    matched_persona: str = ""
    crm_match_status: str = ""  # existing | possible_match | new
    crm_match_label: str = ""  # Already in NorthStar | Possible Match | New Contact
    crm_matched_contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    linkedin_url: str = ""
    source_labels: list[str] = Field(default_factory=list)
    linkedin_derived: bool = False
    relevance_tier: str = ""  # HIGH | MEDIUM | LOW | NOT_TARGET
    crm_matched_contact_id: int | None = None
    crm_match_reasons: list[str] = Field(default_factory=list)


class ResearchProposedUpdateView(BaseModel):
    id: int
    company_id: int
    field_key: str
    current_value: str = ""
    proposed_value: str = ""
    source_name: str = ""
    source_url: str = ""
    research_date: str = ""
    confidence: str = "medium"
    status: str = "Pending Review"


class ResearchFitView(BaseModel):
    client_id: int
    client_name: str
    campaign_id: int | None = None
    campaign_name: str = ""
    fit_result: str = "Insufficient Information"
    why: str = ""
    supporting_evidence: list[str] = Field(default_factory=list)
    # Explainable chains: "fact → why it matters"
    evidence_chains: list[dict] = Field(default_factory=list)
    potential_opportunity: str = ""
    concerns: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    profile_fields_used: list[str] = Field(default_factory=list)
    profile_incomplete: bool = False
    profile_gaps: list[str] = Field(default_factory=list)


class ResearchTargetingGuidance(BaseModel):
    """Advisory targeting notes from Client Knowledge — never used for Campaign Fit."""

    prospecting_guidance: str = ""
    note: str = (
        "Prospecting Guidance informs targeting/research context only. "
        "It does not independently determine Campaign Fit."
    )


class ResearchDecisionSummary(BaseModel):
    """Sales-facing Campaign Fit decision block — presentation only; does not change fit logic."""

    client_name: str = ""
    campaign_name: str = ""
    fit_result: str = ""
    why: str = ""
    still_need: str = ""


class ResearchEngagementAssessment(BaseModel):
    """
    Opportunity / Engagement — separate from Campaign Fit.
    Commercial milestones (PO, Quote, Appointment Set, WebLead, Hot) raise engagement
    confidence but must never be converted into a Campaign Fit rating.
    """

    level: str = "No Stored Engagement"
    label: str = ""
    signals: list[str] = Field(default_factory=list)
    attributed_to: str = ""
    why: str = ""
    note: str = (
        "Engagement is separate from Campaign Fit. Commercial history can increase "
        "confidence to pursue the account but does not establish manufacturing fit."
    )
    cross_client_signals: list[dict] = Field(default_factory=list)


class NorthStarRecommendation(BaseModel):
    """
    Advisory next-action layer — independent from Campaign Fit and Engagement.
    Never writes CRM data or changes Work Queue ranking.
    """

    action: str = "Review Account"
    why: str = ""
    evidence_used: list[str] = Field(default_factory=list)
    still_need_to_know: list[str] = Field(default_factory=list)
    fit_context: str = ""
    engagement_context: str = ""
    confidence: str = "medium"  # high | medium | low
    advisory_note: str = (
        "Advisory only - does not change CRM status, create tasks, or alter Work Queue ranking."
    )


class ResearchNorthStarKnown(BaseModel):
    company_name: str = ""
    master_record_no: str = ""
    website: str = ""
    city: str = ""
    state: str = ""
    address: str = ""
    working_for_client_id: int | None = None
    working_for_client_name: str = ""
    working_for_record_no: str = ""
    working_for_status: str = ""
    has_relationship: bool = False
    contacts: list[dict] = Field(default_factory=list)
    contacts_total: int = 0
    milestones: list[dict] = Field(default_factory=list)
    notes: list[dict] = Field(default_factory=list)
    activities: list[dict] = Field(default_factory=list)
    sales_events: list[dict] = Field(default_factory=list)
    cross_client: list[dict] = Field(default_factory=list)
    previous_research: list[dict] = Field(default_factory=list)
    opportunity_score: int | None = None


class ResearchVerificationItem(BaseModel):
    field_key: str
    label: str
    northstar_value: str = ""
    research_value: str = ""
    source_name: str = ""
    source_url: str = ""
    match: bool = False
    proposed_update_id: int | None = None


class ResearchCompanyResponse(BaseModel):
    research_run_id: int | None = None
    company_id: int
    company_name: str
    working_for_client_id: int | None = None
    working_for_client_name: str = ""
    working_for_record_no: str = ""
    working_for_status: str = ""
    campaign_id: int | None = None
    campaign_name: str = ""
    campaign_choices: list[dict] = Field(default_factory=list)
    needs_working_for: bool = False
    working_for_choices: list[dict] = Field(default_factory=list)
    summary: str = ""
    decision_summary: ResearchDecisionSummary | None = None
    engagement: ResearchEngagementAssessment | None = None
    recommendation: NorthStarRecommendation | None = None
    last_researched_at: str = ""
    initiated_by: str = ""
    northstar_known: ResearchNorthStarKnown | None = None
    verified_matches: list[ResearchVerificationItem] = Field(default_factory=list)
    possible_changes: list[ResearchVerificationItem] = Field(default_factory=list)
    overview: list[ResearchFindingView] = Field(default_factory=list)
    locations: list[ResearchFindingView] = Field(default_factory=list)
    capabilities: list[ResearchFindingView] = Field(default_factory=list)
    products: list[ResearchFindingView] = Field(default_factory=list)
    materials: list[ResearchFindingView] = Field(default_factory=list)
    industries: list[ResearchFindingView] = Field(default_factory=list)
    recent_developments: list[ResearchFindingView] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    northstar_contacts: list[dict] = Field(default_factory=list)
    northstar_contacts_total: int = 0
    public_contacts: list[ResearchFindingView] = Field(default_factory=list)
    people_discovery: dict = Field(default_factory=dict)
    cross_client_experience: list[dict] = Field(default_factory=list)
    fit: ResearchFitView | None = None
    targeting_guidance: ResearchTargetingGuidance | None = None
    proposed_updates: list[ResearchProposedUpdateView] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    pages_researched: list[dict] = Field(default_factory=list)
    data_provider: dict = Field(default_factory=dict)
    research_history: list[dict] = Field(default_factory=list)
    read_only_crm: bool = True
    research_depth: str = "quick"
    job: ResearchJobView | None = None
    citations: list[dict] = Field(default_factory=list)
    deep_research_usage: dict = Field(default_factory=dict)
    deep_research_from_cache: bool = False
    deep_research_paid_refresh_required: bool = False


class ResearchApproveRequest(BaseModel):
    proposed_update_id: int
    approved_by: str | None = None


class ResearchRejectRequest(BaseModel):
    proposed_update_id: int
    rejected_by: str | None = None


# --- Client Setup / Campaigns / Target Profiles ---


class ClientSetupCompleteness(BaseModel):
    """Separated completeness + AI Fit readiness (not an AI quality score)."""

    # Backward-compatible aggregate (prefer client_information + target_profile)
    percent: int = 0
    filled_fields: int = 0
    total_fields: int = 0
    missing_sections: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    # Deprecated alias — use ai_fit_ready
    strong_fit_ready: bool = False

    client_information_percent: int = 0
    client_information_missing: list[str] = Field(default_factory=list)
    target_profile_percent: int = 0
    target_profile_missing: list[str] = Field(default_factory=list)
    ai_fit_ready: bool = False
    ai_fit_missing: list[str] = Field(default_factory=list)
    # Deliberately blank criteria (not errors / not Strong Fit blockers)
    intentionally_unspecified: list[str] = Field(default_factory=list)


class ClientCampaignView(BaseModel):
    campaign_id: int
    client_id: int
    campaign_name: str
    description: str = ""
    active: bool = True
    is_default: bool = False
    primary_service: str = ""
    secondary_services: str = ""
    target_industries: str = ""
    target_customer_types: str = ""
    target_products: str = ""
    manufacturing_processes_sought: str = ""
    production_preference: str = ""
    stamping_capability: str = ""
    tooling_notes: str = ""
    geographic_preferences: str = ""
    geography_mode: str = ""
    geography_required: bool = False
    company_size_preferences: str = ""
    positive_signals: str = ""
    negative_signals: str = ""
    exclusions: str = ""
    target_titles: str = ""
    fit_weighting_notes: str = ""
    notes: str = ""
    list_source: str = ""
    import_date: str = ""
    created_at: str = ""
    updated_at: str = ""


class ClientSetupAuditItem(BaseModel):
    id: int
    client_id: int
    campaign_id: int | None = None
    entity_type: str = ""
    field_name: str = ""
    old_value: str = ""
    new_value: str = ""
    changed_by_name: str = ""
    changed_at: str = ""
    change_source: str = ""


class ClientSetupResponse(BaseModel):
    client_id: int
    client_code: str = ""
    client_name: str = ""
    website: str = ""
    main_location: str = ""
    main_phone: str = ""
    description: str = ""
    primary_owner_name: str = ""
    is_active: bool = True
    primary_service: str = ""
    secondary_services: str = ""
    products_services: str = ""
    differentiators: str = ""
    certifications: str = ""
    equipment_capacity: str = ""
    value_proposition: str = ""
    default_campaign_id: int | None = None
    campaigns: list[ClientCampaignView] = Field(default_factory=list)
    completeness: ClientSetupCompleteness = Field(
        default_factory=ClientSetupCompleteness
    )
    # Soft-display provenance for auto-filled blanks (does not overwrite DB)
    field_sources: dict[str, str] = Field(default_factory=dict)
    can_edit: bool = False
    audit_history: list[ClientSetupAuditItem] = Field(default_factory=list)
    updated_at: str = ""


class ClientListSetupItem(BaseModel):
    client_id: int
    client_code: str = ""
    client_name: str = ""
    is_active: bool = True
    completeness_percent: int = 0
    client_information_percent: int = 0
    target_profile_percent: int = 0
    ai_fit_ready: bool = False
    missing_sections: list[str] = Field(default_factory=list)
    campaign_count: int = 0
    default_campaign_name: str = ""
    can_edit: bool = False


class ClientOverviewUpdate(BaseModel):
    client_name: str = ""
    website: str = ""
    main_location: str = ""
    main_phone: str = ""
    description: str = ""
    primary_owner_name: str = ""
    is_active: bool = True


class ClientSellsUpdate(BaseModel):
    primary_service: str = ""
    secondary_services: str = ""
    products_services: str = ""
    differentiators: str = ""
    certifications: str = ""
    equipment_capacity: str = ""
    value_proposition: str = ""


class ClientCampaignUpdate(BaseModel):
    campaign_name: str = ""
    description: str = ""
    active: bool = True
    is_default: bool = False
    primary_service: str = ""
    secondary_services: str = ""
    target_industries: str = ""
    target_customer_types: str = ""
    target_products: str = ""
    manufacturing_processes_sought: str = ""
    production_preference: str = ""
    stamping_capability: str = ""
    tooling_notes: str = ""
    geographic_preferences: str = ""
    geography_mode: str = ""
    geography_required: bool = False
    company_size_preferences: str = ""
    positive_signals: str = ""
    negative_signals: str = ""
    exclusions: str = ""
    target_titles: str = ""
    fit_weighting_notes: str = ""
    notes: str = ""


# --- Client Knowledge Hub ---


class ClientDocumentView(BaseModel):
    document_id: int
    client_id: int
    filename: str = ""
    document_type: str = "Other"
    uploaded_at: str = ""
    uploaded_by: str = ""
    processing_status: str = "uploaded"
    sensitive_flag: bool = False
    sensitive_note: str = ""
    archived: bool = False
    file_size: int = 0
    replaced_by_document_id: int | None = None


class ClientExtractionProposalView(BaseModel):
    proposal_id: int
    client_id: int
    document_id: int | None = None
    document_filename: str = ""
    document_type: str = ""
    section: str = ""
    field_name: str = ""
    field_label: str = ""
    existing_value: str = ""
    proposed_value: str = ""
    source_reference: str = ""
    raw_source_text: str = ""
    source_locator: str = ""
    confidence: str = "MEDIUM"
    classification: str = ""
    status: str = "Pending"
    status_label: str = ""
    reviewed_by: str = ""
    reviewed_at: str = ""
    approved_value: str = ""
    extracted_value: str = ""
    apply_target: str = ""
    applied_at: str = ""
    resolved_at: str = ""
    resolved_by: str = ""
    resolution_reason: str = ""
    resolved_by_proposal_ids: list[int] = Field(default_factory=list)
    resolved_client_contact_ids: list[int] = Field(default_factory=list)


class ClientExtractionProposalUpdate(BaseModel):
    status: str = "Pending"
    edited_value: str | None = None
    # Optional remapping before approve (e.g. Unmapped → Client Operations)
    section: str | None = None
    field_name: str | None = None
    item_title: str | None = None


class ClientExtractionResolveRequest(BaseModel):
    resolution_reason: str = ""
    resolved_by_proposal_ids: list[int] = Field(default_factory=list)
    resolved_client_contact_ids: list[int] = Field(default_factory=list)


class ContactFieldIncorporation(BaseModel):
    field: str
    value: str = ""
    status: str  # already_incorporated | unresolved | not_present
    matched_contact_id: int | None = None
    note: str = ""


class ContactPersonIncorporation(BaseModel):
    name: str = ""
    email: str = ""
    fields: list[ContactFieldIncorporation] = Field(default_factory=list)
    overall: str = ""  # fully_incorporated | partially_incorporated | not_incorporated


class ProposalIncorporationAnalysis(BaseModel):
    proposal_id: int
    client_id: int
    status: str = "Pending"
    document_filename: str = ""
    people: list[ContactPersonIncorporation] = Field(default_factory=list)
    fully_incorporated: bool = False
    unresolved_summary: list[str] = Field(default_factory=list)
    resolution_suggestion: str = ""
    can_suggest_resolve: bool = False
    suggested_resolved_by_proposal_ids: list[int] = Field(default_factory=list)
    suggested_resolved_client_contact_ids: list[int] = Field(default_factory=list)
    note: str = (
        "Suggestion only — resolving requires human confirmation. "
        "Unresolved titles/phones/roles are not treated as Client Contact facts."
    )


class ContactEnrichmentProposalCreate(BaseModel):
    contact_id: int
    field_name: str  # title | phone | role_type | notes
    proposed_value: str = ""
    source_proposal_id: int | None = None
    source_document_id: int | None = None
    evidence: str = ""


class ClientContactView(BaseModel):
    contact_id: int
    client_id: int
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    role_type: str = ""
    notes: str = ""
    active: bool = True
    source_document_id: int | None = None
    source_proposal_id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""
    updated_by: str = ""


class ClientContactCreate(BaseModel):
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    role_type: str = ""
    notes: str = ""
    source_document_id: int | None = None
    source_proposal_id: int | None = None


class ClientContactUpdate(BaseModel):
    name: str | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    role_type: str | None = None
    notes: str | None = None
    active: bool | None = None


class ClientContactSplitPerson(BaseModel):
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    role_type: str = ""
    notes: str = ""
    raw: str = ""
    possible_duplicate_id: int | None = None
    possible_duplicate_name: str = ""
    needs_review: bool = False


class ClientContactSplitPreview(BaseModel):
    proposal_id: int
    client_id: int
    source_text: str = ""
    people: list[ClientContactSplitPerson] = Field(default_factory=list)
    person_count: int = 0


class ClientContactSplitCreateRequest(BaseModel):
    people: list[ClientContactSplitPerson] | None = None


class KnowledgeSectionCatalogItem(BaseModel):
    section_key: str
    display_name: str
    fields: list[dict[str, str]] = Field(default_factory=list)


class ClientExtractionBulkReviewRequest(BaseModel):
    proposal_ids: list[int] = Field(default_factory=list)
    mode: str = "selected"  # selected | matches
    status: str = "Approved"


class ClientExtractionBulkResolveRequest(BaseModel):
    proposal_ids: list[int] = Field(default_factory=list)


class ClientExtractionBulkRejectRequest(BaseModel):
    proposal_ids: list[int] = Field(default_factory=list)
    rejection_category: str = ""
    rejection_note: str = ""


class ClientDocumentProcessResult(BaseModel):
    document: ClientDocumentView
    proposals_created: int = 0
    sensitive_excluded: int = 0
    type_hint: str = ""
    appointment_batch_id: int | None = None
    message: str = ""


class ClientKnowledgeSectionView(BaseModel):
    section_key: str
    payload: dict = Field(default_factory=dict)
    updated_at: str = ""
    updated_by: str = ""


class ClientKnowledgeSectionUpdate(BaseModel):
    payload: dict = Field(default_factory=dict)


class ClientKnowledgeFieldUpdate(BaseModel):
    field_name: str = ""
    value: str = ""


class ClientEmailTemplateView(BaseModel):
    template_id: int
    client_id: int
    template_name: str = ""
    template_type: str = ""
    subject: str = ""
    body: str = ""
    is_active: bool = True
    source_document_id: int | None = None
    source_proposal_ids: list[int] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    created_by_name: str = ""
    approved_by_name: str = ""


class ClientEmailTemplateUpdate(BaseModel):
    template_name: str = ""
    template_type: str = ""
    subject: str = ""
    body: str = ""
    is_active: bool = True
    source_document_id: int | None = None
    source_proposal_ids: list[int] = Field(default_factory=list)


class ClientEmailAccountAssignmentView(BaseModel):
    assignment_id: int
    client_email_account_id: int
    user_id: int | None = None
    source_rep_name: str = ""
    active: bool = True
    is_default_for_rep: bool = False
    created_at: str = ""
    updated_at: str = ""


class ClientEmailAccountAssignmentUpdate(BaseModel):
    user_id: int | None = None
    source_rep_name: str = ""
    active: bool = True
    is_default_for_rep: bool = True


class ClientEmailAccountView(BaseModel):
    account_id: int
    client_id: int
    email_address: str = ""
    display_name: str = ""
    provider: str = "Other"
    connection_status: str = "not_connected"
    active: bool = True
    is_default: bool = False
    provider_connection_ref: str = ""
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""
    updated_by: str = ""
    assignments: list[ClientEmailAccountAssignmentView] = Field(default_factory=list)


class ClientEmailAccountUpdate(BaseModel):
    email_address: str = ""
    display_name: str = ""
    provider: str = "Other"
    connection_status: str = "not_connected"
    active: bool = True
    is_default: bool = False
    provider_connection_ref: str = ""


class ClientEmailAccountSuggestion(BaseModel):
    client_id: int
    suggested_email_address: str = ""
    suggested_display_name: str = ""
    suggested_provider: str = "Other"
    suggested_source_rep_name: str = ""
    suggested_user_id: int | None = None
    email_suggestion_source: str = ""
    rep_suggestion_source: str = ""
    already_configured: bool = False
    auto_created: bool = False
    notes: list[str] = Field(default_factory=list)


class ClientEmailSignatureView(BaseModel):
    signature_id: int
    client_id: int
    email_account_id: int | None = None
    rep_user_id: int | None = None
    source_rep_name: str = ""
    signature_name: str = ""
    signature_body: str = ""
    active: bool = True
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""


class ClientEmailSignatureUpdate(BaseModel):
    email_account_id: int | None = None
    rep_user_id: int | None = None
    source_rep_name: str = ""
    signature_name: str = ""
    signature_body: str = ""
    active: bool = True
    is_default: bool = False


class PlaceholderResolution(BaseModel):
    placeholder: str = ""
    resolved: bool = False
    value: str = ""
    note: str = ""


class ClientEmailPreviewRequest(BaseModel):
    account_id: int
    contact_id: int
    template_id: int | None = None  # None / omitted = Blank Email
    appointment_date: str = ""
    appointment_time: str = ""
    appointment_contact: str = ""
    appointment_event_id: int | None = None  # client_appointment_events.id
    sales_event_id: int | None = None  # client_sales_events.id (appointment family)
    meeting_with: str = ""  # optional explicit override; otherwise from appointment


class EmailPreviewAppointmentOption(BaseModel):
    """Unified appointment row for Preview Email selector (read-only)."""

    source: str  # "sales_event" | "appointment_event"
    event_id: int
    label: str = ""
    appointment_date: str = ""  # readable for placeholders, e.g. Jul 14, 2026
    appointment_time: str = ""  # readable for placeholders, e.g. 9:00 AM CDT
    appointment_date_raw: str = ""
    appointment_time_raw: str = ""
    company_name: str = ""
    contact_name: str = ""
    meeting_with: str = ""
    event_type: str = ""
    meeting_type: str = ""
    timezone: str = ""
    source_date_time_text: str = ""
    company_id: int | None = None
    contact_id: int | None = None


class ClientEmailPreviewResult(BaseModel):
    client_id: int
    client_name: str = ""
    account_id: int
    template_id: int | None = None
    template_name: str = ""
    is_blank: bool = False
    contact_id: int
    company_id: int | None = None
    from_address: str = ""
    from_display: str = ""
    to_address: str = ""
    to_contact_name: str = ""
    subject: str = ""
    body: str = ""  # final message including signature exactly once
    message_body: str = ""  # template/message content only (no auto-appended signature)
    signature: str = ""
    signature_placement: str = ""  # appended | placeholder | none
    signature_source: str = ""
    revenue_specialist: str = ""
    meeting_with: str = ""
    connection_status: str = "not_connected"
    connection_connected: bool = False
    send_enabled: bool = False
    unresolved_placeholders: list[PlaceholderResolution] = Field(default_factory=list)
    resolved_placeholders: list[PlaceholderResolution] = Field(default_factory=list)
    legacy_token_notes: list[str] = Field(default_factory=list)
    message: str = ""
    supported_placeholders: list[str] = Field(default_factory=list)
    # Future send payload fields (not sent in this phase)
    appointment_event_id: int | None = None
    sales_event_id: int | None = None
    external_record_no: str = ""
    campaign_id: int | None = None


class ClientEmailSendRequest(BaseModel):
    account_id: int
    contact_id: int | None = None
    company_id: int | None = None
    template_id: int | None = None
    to_address: str = ""
    subject: str = ""
    body: str = ""
    signature: str = ""
    appointment_event_id: int | None = None
    confirm_send: bool = False
    confirm_recipient: str = ""  # must match to_address when confirm_send


class ClientEmailSendResult(BaseModel):
    ok: bool = False
    client_id: int
    account_id: int
    company_id: int | None = None
    contact_id: int | None = None
    to_address: str = ""
    subject: str = ""
    provider: str = "Google"
    provider_message_id: str = ""
    sales_event_id: int | None = None
    event_type: str = "Email Sent"
    message: str = ""


class EmailTemplateNameConflict(BaseModel):
    kind: str = "POTENTIAL CONTACT NAME CONFLICT"
    source_name: str = ""
    approved_contact_name: str = ""
    approved_contact_id: int | None = None
    note: str = ""


class EmailTemplateGroupPreview(BaseModel):
    client_id: int
    document_id: int | None = None
    document_filename: str = ""
    source_proposal_ids: list[int] = Field(default_factory=list)
    component_statuses: dict[str, str] = Field(default_factory=dict)
    template_name: str = ""
    template_type: str = ""
    subject: str = ""
    body: str = ""
    is_active: bool = True
    evidence: list[str] = Field(default_factory=list)
    name_conflicts: list[EmailTemplateNameConflict] = Field(default_factory=list)
    requires_human_review: bool = False
    message: str = ""


class EmailTemplateGroupApproveRequest(BaseModel):
    proposal_ids: list[int] = Field(default_factory=list)
    template_name: str = ""
    template_type: str = ""
    subject: str = ""
    body: str = ""
    is_active: bool = True
    acknowledge_name_conflicts: bool = False


class ClientAppointmentImportBatchView(BaseModel):
    batch_id: int
    client_id: int
    document_id: int | None = None
    filename: str = ""
    status: str = ""
    headers: list[str] = Field(default_factory=list)
    column_mapping: dict[str, str] = Field(default_factory=dict)
    row_count: int = 0
    uploaded_at: str = ""
    uploaded_by: str = ""
    target_fields: list[str] = Field(default_factory=list)


class ClientAppointmentImportMapRequest(BaseModel):
    column_mapping: dict[str, str] = Field(default_factory=dict)


class ClientAppointmentImportRowView(BaseModel):
    row_id: int
    row_index: int = 0
    mapped: dict[str, str] = Field(default_factory=dict)
    company_match_status: str = "NEW"
    company_id: int | None = None
    company_name: str = ""
    company_record_no: str = ""
    contact_match_status: str = "NEW"
    contact_id: int | None = None
    contact_name: str = ""
    import_status: str = "staged"


class ClientAppointmentImportPreview(BaseModel):
    batch: ClientAppointmentImportBatchView
    rows: list[ClientAppointmentImportRowView] = Field(default_factory=list)


class ClientAppointmentImportConfirmRequest(BaseModel):
    confirm: bool = False


class ClientKnowledgeHubResponse(BaseModel):
    client_id: int
    client_name: str = ""
    client_code: str = ""
    can_edit: bool = False
    documents: list[ClientDocumentView] = Field(default_factory=list)
    sections: list[ClientKnowledgeSectionView] = Field(default_factory=list)
    email_templates: list[ClientEmailTemplateView] = Field(default_factory=list)
    email_accounts: list[ClientEmailAccountView] = Field(default_factory=list)
    extraction_proposals: list[ClientExtractionProposalView] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    appointment_events: list[dict] = Field(default_factory=list)
    client_contacts: list[ClientContactView] = Field(default_factory=list)
    client_contact_role_types: list[str] = Field(default_factory=list)

# --- Phase 3 Engagement / Appointment workbook import ---


class SheetClassificationUpdate(BaseModel):
    sheet_id: int
    user_type: str = "Appointments"


class EngagementImportSheetView(BaseModel):
    sheet_id: int
    sheet_name: str = ""
    detected_type: str = ""
    user_type: str = ""
    headers: list[str] = Field(default_factory=list)
    column_mapping: dict[str, str] = Field(default_factory=dict)
    row_count: int = 0
    is_empty: bool = False


class EngagementImportBatchView(BaseModel):
    batch_id: int
    client_id: int
    document_id: int | None = None
    filename: str = ""
    status: str = ""
    uploaded_at: str = ""
    uploaded_by: str = ""
    sheets: list[EngagementImportSheetView] = Field(default_factory=list)
    target_fields: list[str] = Field(default_factory=list)
    sheet_type_options: list[str] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class EngagementImportRowView(BaseModel):
    row_id: int
    sheet_name: str = ""
    sheet_type: str = ""
    source_row_number: int = 0
    mapped: dict[str, str] = Field(default_factory=dict)
    event_type: str = ""
    source_date_time_text: str = ""
    event_date: str = ""
    event_time: str = ""
    timezone: str = ""
    meeting_type: str = ""
    datetime_needs_review: bool = False
    company_match_status: str = "NEW"
    company_id: int | None = None
    relationship_id: int | None = None
    company_name: str = ""
    company_record_no: str = ""
    contact_match_status: str = "NEW"
    contact_id: int | None = None
    contact_name: str = ""
    client_validation: str = "OK"
    source_rev_spec_text: str = ""
    rev_spec_user_id: int | None = None
    rev_spec_needs_review: bool = False
    source_appointment_grade: str = ""
    quoted_amount: float | None = None
    source_quoted_value: str = ""
    potential_quote_signal: bool = False
    outcome_normalized: str = ""
    source_outcome: str = ""
    thread_key: str = ""
    source_row_fingerprint: str = ""
    duplicate_status: str = ""
    import_status: str = "staged"
    selected: bool = True
    needs_review: bool = False
    review_flags: list[str] = Field(default_factory=list)


class EngagementImportSummary(BaseModel):
    rows_detected: int = 0
    rows_selected: int = 0
    matched_companies: int = 0
    possible_company_matches: int = 0
    new_companies: int = 0
    company_conflicts: int = 0
    matched_contacts: int = 0
    possible_contact_matches: int = 0
    new_contacts: int = 0
    duplicates_skipped: int = 0
    rows_needing_review: int = 0
    appointment_events: int = 0
    send_information_events: int = 0
    datetime_parse_success: int = 0
    datetime_needs_review: int = 0
    client_mismatches: int = 0


class EngagementImportPreview(BaseModel):
    batch: EngagementImportBatchView
    rows: list[EngagementImportRowView] = Field(default_factory=list)
    summary: EngagementImportSummary = Field(default_factory=EngagementImportSummary)


class EngagementImportMapRequest(BaseModel):
    sheet_mappings: dict[str, dict[str, str]] = Field(default_factory=dict)


class EngagementImportClassifyRequest(BaseModel):
    sheets: list[SheetClassificationUpdate] = Field(default_factory=list)


class EngagementImportConfirmRequest(BaseModel):
    confirm: bool = False
    row_ids: list[int] = Field(default_factory=list)
    create_new_ids: list[int] = Field(default_factory=list)


class SalesEventView(BaseModel):
    event_id: int
    client_id: int
    relationship_id: int | None = None
    company_id: int | None = None
    contact_id: int | None = None
    thread_key: str = ""
    parent_event_id: int | None = None
    event_type: str = ""
    event_family: str = ""
    source_date_time_text: str = ""
    event_date: str = ""
    event_time: str = ""
    timezone: str = ""
    meeting_type: str = ""
    company_name: str = ""
    contact_name: str = ""
    contact_title: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    city_state_zip: str = ""
    caller_notes: str = ""
    sales_notes: str = ""
    source_appointment_grade: str = ""
    source_rev_spec_text: str = ""
    rev_spec_user_id: int | None = None
    quoted_amount: float | None = None
    source_quoted_value: str = ""
    potential_quote_signal: bool = False
    outcome_normalized: str = ""
    source_outcome: str = ""
    source_document_id: int | None = None
    source_batch_id: int | None = None
    source_sheet: str = ""
    source_row: int = 0
    source_file_name: str = ""
    source_record_number: str = ""
    source_row_fingerprint: str = ""
    imported_at: str = ""
    imported_by: str = ""


class ContactRepOption(BaseModel):
    user_id: int
    full_name: str = ""


class ClientRepsResponse(BaseModel):
    client_id: int
    reps: list[ContactRepOption] = Field(default_factory=list)


class ContactTimelineItem(BaseModel):
    item_type: str
    id: int
    title: str
    body: str = ""
    at: str = ""
    created_by: str = ""
    outcome: str = ""
    follow_up_at: str | None = None


class ContactWorkspace(BaseModel):
    contact_id: int
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    phone: str = ""
    alt_phone: str = ""
    email: str = ""
    company_id: int | None = None
    company_name: str = ""
    company_record_no: str = ""
    client_id: int = 0
    client_name: str = ""
    client_code: str = ""
    relationship_id: int | None = None
    assigned_to_client: bool = True
    company_assigned_to_client: bool = True
    status: str = ""
    assigned_user_id: int | None = None
    assigned_user: str = ""
    next_action: str = ""
    follow_up_date: str = ""
    follow_up_time: str = ""
    open_follow_up: "ContactOpenFollowUp | None" = None
    sales_events: list[SalesEventView] = Field(default_factory=list)
    activities: list[dict] = Field(default_factory=list)
    notes: list[dict] = Field(default_factory=list)
    timeline: list[ContactTimelineItem] = Field(default_factory=list)
    company_timeline: list[ContactTimelineItem] = Field(default_factory=list)
    reps: list[ContactRepOption] = Field(default_factory=list)
    linkedin_url: str = ""
    location: str = ""
    zoominfo_contact_id: str = ""
    source: str = ""
    source_updated_at: str = ""


class ContactWorkflowUpdate(BaseModel):
    client_id: PositiveClientId
    status: str | None = None
    assigned_user_id: int | None = None
    next_action: str | None = None
    follow_up_date: str | None = None
    follow_up_time: str | None = None
    user: str = "Julie Magnani"


class AppointmentDetailsPayload(BaseModel):
    appointment_date: str = ""
    start_time: str = ""
    timezone: str = "America/Chicago"
    datetime_tbd: bool = False
    appointment_type: str = "Phone"
    location_or_link: str = ""
    revenue_specialist_user_id: int | None = None
    notes: str = ""
    source: str = "Phone Call"
    idempotency_key: str = ""


WorkQueueLogCallRequest.model_rebuild()


class ContactActivityCreate(BaseModel):
    client_id: PositiveClientId
    activity_type: str
    notes: str = ""
    outcome: str = ""
    status: str | None = None
    next_action: str | None = None
    follow_up_date: str | None = None
    follow_up_time: str | None = None
    assigned_user_id: int | None = None
    schedule_follow_up: bool = False
    created_by: str = "Julie Magnani"
    appointment: AppointmentDetailsPayload | None = None


class ContactAssignRequest(BaseModel):
    client_id: PositiveClientId
    add_company: bool = False
    user: str = "Julie Magnani"


class ContactOpenFollowUp(BaseModel):
    source: str = "work_queue"
    source_id: int | None = None
    activity_id: int | None = None
    due_date: str = ""
    due_time: str = ""
    assigned_user: str = ""
    notes: str = ""


class ContactFollowUpCompleteRequest(BaseModel):
    client_id: PositiveClientId
    notes: str = ""
    status: str | None = None
    created_by: str = "Julie Magnani"


class ContactFollowUpRescheduleRequest(BaseModel):
    client_id: PositiveClientId
    follow_up_date: str
    follow_up_time: str
    notes: str = ""
    created_by: str = "Julie Magnani"


class ContactAssignResult(BaseModel):
    ok: bool = True
    message: str = ""
    needs_company_confirmation: bool = False
    company_assigned: bool = True
    already_assigned: bool = False
    company_created: bool = False
    workspace: ContactWorkspace
    activity_id: int | None = None


class ContactWorkflowResult(BaseModel):
    ok: bool = True
    message: str = ""
    workspace: ContactWorkspace
    activity_id: int | None = None
    follow_up_activity_id: int | None = None
    appointment_id: int | None = None


class AppointmentRecord(BaseModel):
    id: int
    client_id: int
    company_id: int
    relationship_id: int
    contact_id: int | None = None
    activity_id: int | None = None
    company_name: str = ""
    company_record_no: str = ""
    contact_name: str = ""
    appointment_date: str = ""
    start_time: str = ""
    timezone: str = "America/Chicago"
    datetime_tbd: bool = False
    appointment_type: str = "Phone"
    location_or_link: str = ""
    revenue_specialist_user_id: int | None = None
    revenue_specialist_name: str = ""
    notes: str = ""
    source: str = "Phone Call"
    status: str = "scheduled"
    grade: str = ""
    outcome: str = ""
    follow_up_notes: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    cancellation_reason: str = ""
    cancelled_by: str = ""
    cancelled_at: str = ""
    completed_by: str = ""
    completed_at: str = ""
    bucket: str = "upcoming"


class AppointmentListResponse(BaseModel):
    client_id: int
    bucket: str = ""
    items: list[AppointmentRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    set_count: int = 0


class AppointmentSummary(BaseModel):
    client_id: int
    set_count: int = 0
    scheduled_count: int = 0
    today_count: int = 0
    upcoming_count: int = 0
    past_count: int = 0
    cancelled_count: int = 0
    upcoming: list[AppointmentRecord] = Field(default_factory=list)
    today: list[AppointmentRecord] = Field(default_factory=list)


class AppointmentActionResult(BaseModel):
    ok: bool = True
    message: str = ""
    appointment: AppointmentRecord


class AppointmentRescheduleRequest(BaseModel):
    client_id: PositiveClientId
    appointment_date: str = ""
    start_time: str = ""
    timezone: str = "America/Chicago"
    datetime_tbd: bool = False
    appointment_type: str = ""
    location_or_link: str = ""
    revenue_specialist_user_id: int | None = None
    notes: str = ""
    source: str = ""


class AppointmentCancelRequest(BaseModel):
    client_id: PositiveClientId
    reason: str = ""
    notes: str = ""
    new_status: str = ""
    next_action: str = ""
    follow_up_date: str | None = None
    follow_up_time: str = ""
    created_by: str = "Julie Magnani"


class AppointmentCompleteRequest(BaseModel):
    client_id: PositiveClientId
    grade: str = ""
    outcome: str = ""
    follow_up_notes: str = ""
    created_by: str = "Julie Magnani"


# --- Shared CRM Add / Link (AI Research + future Add Company) ---


class CrmAddCompanyInput(BaseModel):
    company_name: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    industry: str = ""


class CrmAddContactInput(BaseModel):
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    source_url: str = ""


class CrmAddFieldDiff(BaseModel):
    field: str
    existing_value: str = ""
    proposed_value: str = ""
    action: str = ""


class CrmAddContactPreview(BaseModel):
    index: int = 0
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    source_url: str = ""
    match_status: str = "new"  # existing | possible_match | new
    matched_contact_id: int | None = None
    matched_contact_name: str = ""
    match_reasons: list[str] = Field(default_factory=list)
    confidence: str = "medium"


class CrmAddRelationshipPreview(BaseModel):
    exists: bool = False
    relationship_id: int | None = None
    current_status: str = ""
    would_create: bool = False
    status_if_create: str = ""
    message: str = ""


class CrmAddPreviewRequest(BaseModel):
    client_id: PositiveClientId
    company: CrmAddCompanyInput
    contacts: list[CrmAddContactInput] = Field(default_factory=list)
    research_run_id: int | None = None
    source: str = ""
    provider: str = ""
    known_company_id: int | None = None
    trusted_external_record_no: str | None = None


class CrmAddPreviewResponse(BaseModel):
    client_id: int
    client_name: str = ""
    company_match_type: str = "new_company"
    company_confidence: str = "high"
    company_match_reasons: list[str] = Field(default_factory=list)
    matched_company_id: int | None = None
    matched_external_record_no: str = ""
    matched_company_name: str = ""
    proposed_company: CrmAddCompanyInput
    company_field_diffs: list[CrmAddFieldDiff] = Field(default_factory=list)
    possible_company_matches: list[dict] = Field(default_factory=list)
    contacts: list[CrmAddContactPreview] = Field(default_factory=list)
    relationship: CrmAddRelationshipPreview = Field(
        default_factory=CrmAddRelationshipPreview
    )
    warnings: list[str] = Field(default_factory=list)
    research_run_id: int | None = None
    source: str = ""
    provider: str = ""


class CrmAddContactConfirmItem(BaseModel):
    contact: CrmAddContactInput
    selected: bool = True
    decision: str = "auto"  # auto | create_new | use_existing | skip
    matched_contact_id: int | None = None


class CrmAddConfirmRequest(BaseModel):
    client_id: PositiveClientId
    company: CrmAddCompanyInput
    contacts: list[CrmAddContactConfirmItem] = Field(default_factory=list)
    company_decision: str = "auto"  # auto | create_new | use_existing
    existing_company_id: int | None = None
    known_company_id: int | None = None
    trusted_external_record_no: str | None = None
    research_run_id: int | None = None
    source: str = ""
    provider: str = ""


class CrmAddConfirmResult(BaseModel):
    ok: bool = True
    message: str = ""
    client_id: int = 0
    company_id: int = 0
    external_record_no: str = ""
    company_name: str = ""
    company_created: bool = False
    relationship_id: int | None = None
    relationship_created: bool = False
    relationship_status: str = ""
    contacts: list[dict] = Field(default_factory=list)
    activity_id: int | None = None
    workspace_path: str = ""


CAMPAIGN_STATUSES = ("Draft", "Active", "Paused", "Completed", "Archived")


class CampaignSummary(BaseModel):
    campaign_id: int
    client_id: int
    client_name: str = ""
    client_code: str = ""
    campaign_name: str = ""
    description: str = ""
    category: str = ""
    owner_name: str = ""
    owner_user_id: int | None = None
    status: str = "Active"
    start_date: str = ""
    end_date: str = ""
    company_count: int = 0
    contact_count: int = 0
    call_count: int = 0
    follow_up_count: int = 0
    appointment_count: int = 0
    outcome_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class CampaignListResponse(BaseModel):
    client_id: int = 0
    count: int = 0
    total: int = 0
    limit: int = 50
    offset: int = 0
    statuses: list[str] = Field(default_factory=list)
    owners: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    items: list[CampaignSummary] = Field(default_factory=list)


class CampaignCompanyRow(BaseModel):
    company_id: int
    company_name: str = ""
    external_record_no: str = ""
    city: str = ""
    state: str = ""
    status: str = ""
    client_id: int = 0


class CampaignContactRow(BaseModel):
    contact_id: int
    company_id: int
    company_name: str = ""
    contact_name: str = ""
    title: str = ""
    external_record_no: str = ""
    client_id: int = 0
    is_primary_routed_contact: bool = False


class CampaignWorkspace(BaseModel):
    campaign: CampaignSummary
    notes: str = ""
    companies: list[CampaignCompanyRow] = Field(default_factory=list)
    contacts: list[CampaignContactRow] = Field(default_factory=list)


class CampaignCreateRequest(BaseModel):
    client_id: PositiveClientId
    campaign_name: str
    description: str = ""
    category: str = ""
    owner_name: str = ""
    status: str = "Draft"
    start_date: str = ""
    end_date: str = ""
    notes: str = ""


class CampaignUpdateRequest(BaseModel):
    campaign_name: str = ""
    description: str | None = None
    category: str | None = None
    owner_name: str | None = None
    status: str = ""
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None


class CampaignMemberAddRequest(BaseModel):
    company_id: int | None = None
    contact_id: int | None = None
    notes: str = ""


class CampaignActionResult(BaseModel):
    ok: bool = True
    message: str = ""
    campaign: CampaignSummary | None = None


class CampaignMemberSearchResponse(BaseModel):
    companies: list[CampaignCompanyRow] = Field(default_factory=list)
    contacts: list[CampaignContactRow] = Field(default_factory=list)


class CampaignChoice(BaseModel):
    campaign_id: int
    campaign_name: str
    status: str = "Active"
    is_default: bool = False
    client_id: int = 0
    recommended: bool = False


class CampaignRouteSuggestion(BaseModel):
    client_id: int
    company_id: int
    contact_id: int | None = None
    company_name: str = ""
    should_prompt: bool = False
    already_assigned: bool = False
    assigned_campaign_ids: list[int] = Field(default_factory=list)
    assigned_campaign_names: list[str] = Field(default_factory=list)
    recommended_campaign_id: int | None = None
    auto_unassigned: bool = False
    message: str = ""
    campaigns: list[CampaignChoice] = Field(default_factory=list)


class CampaignRouteConfirmRequest(BaseModel):
    client_id: PositiveClientId
    company_id: int
    contact_id: int | None = None
    campaign_id: int
    source: str = ""


class CampaignRouteDeferRequest(BaseModel):
    client_id: PositiveClientId
    company_id: int
    contact_id: int | None = None
    source: str = ""


class UnassignedOpportunity(BaseModel):
    unassigned_id: int
    client_id: int
    client_name: str = ""
    company_id: int
    company_name: str = ""
    external_record_no: str = ""
    contact_id: int | None = None
    contact_name: str = ""
    source: str = ""
    source_type: str = ""
    status: str = ""
    identified_at: str = ""
    assigned_rep: str = ""
    campaign_id: int | None = None
    recommended_campaign_id: int | None = None
    recommended_campaign_name: str = ""


class UnassignedOpportunityList(BaseModel):
    client_id: int = 0
    total: int = 0
    limit: int = 50
    offset: int = 0
    q: str = ""
    items: list[UnassignedOpportunity] = Field(default_factory=list)
    campaigns: list[CampaignChoice] = Field(default_factory=list)


class UnassignedBulkAssignRequest(BaseModel):
    client_id: PositiveClientId
    campaign_id: int
    company_ids: list[int] = Field(default_factory=list)
    select_all_matching: bool = False
    exclude_company_ids: list[int] = Field(default_factory=list)
    q: str = ""


class UnassignedBulkAssignResult(BaseModel):
    client_id: int = 0
    client_name: str = ""
    campaign_id: int = 0
    campaign_name: str = ""
    assigned: int = 0
    skipped: int = 0
    failed: int = 0
    total_requested: int = 0
    message: str = ""


class ReportKpi(BaseModel):
    key: str
    label: str
    value: int | float | None = 0
    definition: str = ""


class ReportFilterOption(BaseModel):
    id: int = 0
    name: str = ""
    client_id: int | None = None
    client_name: str = ""


class ReportFiltersResponse(BaseModel):
    client_id: int = 0
    client_ids: list[int] = Field(default_factory=list)
    date_from: str = ""
    date_to: str = ""
    reps: list[ReportFilterOption] = Field(default_factory=list)
    campaigns: list[ReportFilterOption] = Field(default_factory=list)
    activity_types: list[str] = Field(default_factory=list)
    outcome_types: list[str] = Field(default_factory=list)
    definitions: dict[str, str] = Field(default_factory=dict)


class ReportClientResultsRow(BaseModel):
    client_id: int
    client_name: str
    client_code: str = ""
    calls: int = 0
    follow_ups_scheduled: int = 0
    follow_ups_completed: int = 0
    appointments_set: int = 0
    appointments_completed: int = 0
    appointments_cancelled: int = 0
    opportunities: int = 0
    outcomes: int = 0
    hot_prospects: int = 0


class ReportClientResultsResponse(BaseModel):
    client_id: int = 0
    date_from: str = ""
    date_to: str = ""
    total: int = 0
    limit: int = 50
    offset: int = 0
    sort_by: str = "client_name"
    sort_dir: str = "asc"
    kpis: list[ReportKpi] = Field(default_factory=list)
    items: list[ReportClientResultsRow] = Field(default_factory=list)


class ReportTeamRow(BaseModel):
    user_id: int | None = None
    user_name: str = ""
    assigned_clients: int = 0
    calls: int = 0
    notes: int = 0
    follow_ups_completed: int = 0
    appointments_set: int = 0
    appointments_completed: int = 0
    outcomes: int = 0
    overdue_tasks: int = 0


class ReportTeamResponse(BaseModel):
    client_id: int = 0
    date_from: str = ""
    date_to: str = ""
    total: int = 0
    limit: int = 50
    offset: int = 0
    sort_by: str = "user_name"
    sort_dir: str = "asc"
    kpis: list[ReportKpi] = Field(default_factory=list)
    items: list[ReportTeamRow] = Field(default_factory=list)


class ReportCampaignRow(BaseModel):
    campaign_id: int
    campaign_name: str
    client_id: int
    client_name: str
    companies: int = 0
    contacts: int = 0
    calls: int = 0
    follow_ups: int = 0
    appointments: int = 0
    outcomes: int = 0
    call_to_appointment_pct: float | None = None
    outcome_to_call_pct: float | None = None


class ReportCampaignResponse(BaseModel):
    client_id: int = 0
    date_from: str = ""
    date_to: str = ""
    total: int = 0
    limit: int = 50
    offset: int = 0
    sort_by: str = "campaign_name"
    sort_dir: str = "asc"
    kpis: list[ReportKpi] = Field(default_factory=list)
    items: list[ReportCampaignRow] = Field(default_factory=list)


class ReportRecordRow(BaseModel):
    record_key: str
    record_kind: str
    record_id: int | None = None
    client_id: int = 0
    client_name: str = ""
    company_id: int | None = None
    company_name: str = ""
    external_record_no: str = ""
    contact_id: int | None = None
    contact_name: str = ""
    occurred_at: str = ""
    record_type: str = ""
    user_name: str = ""
    outcome: str = ""
    notes: str = ""
    status: str = ""


class ReportRecordsResponse(BaseModel):
    section: str
    metric: str
    total: int = 0
    limit: int = 50
    offset: int = 0
    items: list[ReportRecordRow] = Field(default_factory=list)


class CompanyLookupItem(BaseModel):
    id: int
    company_name: str
    external_record_no: str = ""


class CompanyLookupResponse(BaseModel):
    companies: list[CompanyLookupItem] = Field(default_factory=list)


class ManualContactPreviewRequest(BaseModel):
    client_id: PositiveClientId
    company_id: int = Field(gt=0)
    first_name: str
    last_name: str
    title: str = ""
    email: str = ""
    phone: str = ""
    alt_phone: str = ""


class ManualContactMatch(BaseModel):
    contact_id: int
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    alt_phone: str = ""
    company_id: int
    company_name: str = ""
    reasons: list[str] = Field(default_factory=list)
    confidence: str = "high"
    already_assigned: bool = False
    same_company: bool = True


class ManualContactPreviewResponse(BaseModel):
    matches: list[ManualContactMatch] = Field(default_factory=list)
    can_create: bool = True
    requires_contact_info_confirmation: bool = False
    message: str = ""


class ManualContactSaveRequest(BaseModel):
    client_id: PositiveClientId
    company_id: int = Field(gt=0)
    action: str
    existing_contact_id: int | None = None
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    alt_phone: str = ""
    confirm_without_contact_info: bool = False
    created_by: str = "Julie Magnani"
    source: str = "manual"
    zoominfo_contact_id: str = ""
    linkedin_url: str = ""
    location: str = ""


class ManualContactSaveResult(BaseModel):
    ok: bool = True
    action: str = ""
    message: str = ""
    contact_id: int
    company_id: int
    client_id: int
    activity_id: int | None = None
    already_assigned: bool = False


class ManualCompanyClientRel(BaseModel):
    client_id: int
    client_name: str = ""
    status: str = ""


class ManualCompanyMatch(BaseModel):
    company_id: int
    company_name: str = ""
    external_record_no: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    reasons: list[str] = Field(default_factory=list)
    confidence: str = "high"
    already_assigned: bool = False
    client_relationships: list[ManualCompanyClientRel] = Field(default_factory=list)
    score: int = 0
    ai_assessment: str = ""
    ai_explanation: str = ""
    ai_confidence: str = ""
    ai_available: bool = False


class ManualCompanyPreviewRequest(BaseModel):
    client_id: PositiveClientId
    company_name: str
    website: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    industry: str = ""
    employee_size: str = ""
    sales_volume: str = ""
    external_record_no: str = ""
    notes: str = ""


class ManualCompanyPreviewResponse(BaseModel):
    matches: list[ManualCompanyMatch] = Field(default_factory=list)
    can_create: bool = True
    requires_create_confirmation: bool = False
    message: str = ""
    ai_available: bool = False


class ManualCompanySaveRequest(BaseModel):
    client_id: PositiveClientId
    action: str
    existing_company_id: int | None = None
    company_name: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    industry: str = ""
    employee_size: str = ""
    sales_volume: str = ""
    external_record_no: str = ""
    notes: str = ""
    confirm_create_despite_match: bool = False
    created_by: str = "Julie Magnani"
    source: str = "manual"
    zoominfo_company_id: str = ""


class ManualCompanySaveResult(BaseModel):
    ok: bool = True
    action: str = ""
    message: str = ""
    company_id: int
    client_id: int
    external_record_no: str = ""
    activity_id: int | None = None
    already_assigned: bool = False


class ZoomInfoSnapshot(BaseModel):
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    alt_phone: str = ""
    mobile_phone: str = ""
    company_name: str = ""
    zoominfo_company_id: str = ""
    zoominfo_contact_id: str = ""
    linkedin_url: str = ""
    location: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    industry: str = ""
    employee_size: str = ""
    sales_volume: str = ""


class ZoomInfoFieldChoice(BaseModel):
    field: str
    label: str = ""
    northstar_value: str = ""
    zoominfo_value: str = ""
    keep_northstar: bool = True
    blank_zoominfo: bool = False
    different_company: bool = False
    applyable: bool = True


class ZoomInfoContactPreviewRequest(BaseModel):
    client_id: PositiveClientId
    zoominfo: ZoomInfoSnapshot | None = None


class ZoomInfoContactPreviewResponse(BaseModel):
    available: bool = True
    status: str = ""
    contact_id: int
    client_id: int
    company_id: int | None = None
    company_name: str = ""
    fields: list[ZoomInfoFieldChoice] = Field(default_factory=list)
    different_company: bool = False
    zoominfo_company_name: str = ""
    matches: list[ManualContactMatch] = Field(default_factory=list)
    retrieved_at: str = ""


class ZoomInfoContactApplyRequest(BaseModel):
    client_id: PositiveClientId
    apply_fields: list[str] = Field(default_factory=list)
    zoominfo: ZoomInfoSnapshot
    confirm_company_relink: bool = False
    target_company_id: int | None = None
    created_by: str = "Julie Magnani"


class ZoomInfoContactApplyResult(BaseModel):
    ok: bool = True
    message: str = ""
    contact_id: int
    client_id: int
    company_id: int | None = None
    changed_fields: list[str] = Field(default_factory=list)
    activity_id: int | None = None
    company_relinked: bool = False


class ZoomInfoAddPreviewRequest(BaseModel):
    client_id: PositiveClientId
    company_id: int | None = None
    zoominfo: ZoomInfoSnapshot
    kind: str = "contact"


class ZoomInfoAddPreviewResponse(BaseModel):
    kind: str
    client_id: int
    company_id: int | None = None
    matches: list[dict] = Field(default_factory=list)
    can_create: bool = True
    requires_create_confirmation: bool = False
    message: str = ""


class ZoomInfoAddSaveRequest(BaseModel):
    client_id: PositiveClientId
    company_id: int | None = None
    action: str
    existing_company_id: int | None = None
    existing_contact_id: int | None = None
    zoominfo: ZoomInfoSnapshot
    kind: str = "contact"
    confirm_create_despite_match: bool = False
    created_by: str = "Julie Magnani"


class ZoomInfoAddResult(BaseModel):
    ok: bool = True
    action: str = ""
    message: str = ""
    kind: str = ""
    company_id: int | None = None
    contact_id: int | None = None
    client_id: int
    external_record_no: str = ""
    activity_id: int | None = None


class CrmImportRowView(BaseModel):
    row_id: int = 0
    source_row_number: int
    values: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    has_blocking_error: bool = False


class CrmImportBatchView(BaseModel):
    batch_id: int
    client_id: int
    status: str
    original_filename: str = ""
    file_type: str = ""
    worksheet_name: str = ""
    file_size_bytes: int = 0
    sha256: str = ""
    headers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_message: str = ""
    total_rows: int = 0
    source_row_count: int = 0
    blank_row_count: int = 0
    error_row_count: int = 0
    reusable: bool = False
    expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    cancelled_at: str = ""
    uploaded_by_user_id: int | None = None
    uploaded_by_name: str = ""
    mapping: dict[str, str] = Field(default_factory=dict)
    mapping_updated_at: str = ""
    mapping_updated_by_user_id: int | None = None
    sample_rows: list[CrmImportRowView] = Field(default_factory=list)


class CrmImportMappingRequest(BaseModel):
    mapping: dict[str, str] = Field(default_factory=dict)


class CrmImportUploadResult(BaseModel):
    kind: str
    needs_worksheet: bool = False
    visible_sheets: list[str] = Field(default_factory=list)
    filename: str = ""
    file_type: str = ""
    message: str = ""
    batch: CrmImportBatchView | None = None


class CrmImportRowsPage(BaseModel):
    batch_id: int
    client_id: int
    offset: int
    limit: int
    total: int
    rows: list[CrmImportRowView] = Field(default_factory=list)


class CrmImportDryRunRequest(BaseModel):
    offset: int = 0
    limit: int = 100
    use_imported_status_for_existing: bool = False


class CrmImportDryRunCompanyPossible(BaseModel):
    company_id: int
    company_name: str = ""
    external_record_no: str = ""
    reasons: list[str] = Field(default_factory=list)


class CrmImportDryRunContactPossible(BaseModel):
    contact_id: int
    display_name: str = ""
    reasons: list[str] = Field(default_factory=list)


class CrmImportDryRunCompanyPlan(BaseModel):
    action: str = "none"
    reasons: list[str] = Field(default_factory=list)
    company_id: int | None = None
    proposed_key: str | None = None
    created_at_source_row: int | None = None
    name: str = ""
    possibles: list[CrmImportDryRunCompanyPossible] = Field(default_factory=list)


class CrmImportDryRunContactPlan(BaseModel):
    action: str = "none"
    reasons: list[str] = Field(default_factory=list)
    contact_id: int | None = None
    proposed_key: str | None = None
    created_at_source_row: int | None = None
    display_name: str = ""
    possibles: list[CrmImportDryRunContactPossible] = Field(default_factory=list)


class CrmImportDryRunRelationshipPlan(BaseModel):
    action: str = "none"
    relationship_id: int | None = None
    proposed_key: str | None = None
    status_action: str = "none"
    notes_action: str = "none"
    resolved_status: str = ""
    original_status_action: str = "none"
    status_resolution_type: str = ""
    existing_status: str = ""
    needs_status_resolution: bool = False


class CrmImportDryRunRow(BaseModel):
    row_id: int
    source_row_number: int
    validity: str
    validity_detail: str = ""
    mapped: dict[str, str] = Field(default_factory=dict)
    company: CrmImportDryRunCompanyPlan = Field(default_factory=CrmImportDryRunCompanyPlan)
    contact: CrmImportDryRunContactPlan = Field(default_factory=CrmImportDryRunContactPlan)
    relationship: CrmImportDryRunRelationshipPlan = Field(
        default_factory=CrmImportDryRunRelationshipPlan
    )


class CrmImportDryRunCounts(BaseModel):
    blocking_error: int = 0
    invalid_mapping_data: int = 0
    ok: int = 0
    create_company: int = 0
    use_existing_company: int = 0
    possible_company_match: int = 0
    create_contact: int = 0
    use_existing_contact: int = 0
    possible_contact_match: int = 0
    insufficient_contact_data: int = 0
    no_contact_data: int = 0
    contact_deferred: int = 0
    create_client_relationship: int = 0
    relationship_already_exists: int = 0
    relationship_deferred: int = 0
    use_default_status: int = 0
    preserve_existing_status: int = 0
    use_imported_status: int = 0
    status_conflict: int = 0
    invalid_status: int = 0
    update_existing_status: int = 0
    no_notes_change: int = 0
    set_imported_notes: int = 0
    append_imported_notes: int = 0
    imported_notes_already_present: int = 0
    importable_rows: int = 0
    needs_review_rows: int = 0


class CrmImportDryRunResponse(BaseModel):
    batch_id: int
    client_id: int
    planner_version: str
    plan_fingerprint: str
    mapping_updated_at: str = ""
    total_rows: int
    offset: int
    limit: int
    use_imported_status_for_existing: bool = False
    counts: CrmImportDryRunCounts = Field(default_factory=CrmImportDryRunCounts)
    rows: list[CrmImportDryRunRow] = Field(default_factory=list)
    status_catalog: list[str] = Field(default_factory=list)


class CrmImportStatusResolutionRequest(BaseModel):
    resolution_type: str | None = None
    resolved_status: str | None = None
    clear: bool = False


class CrmImportStatusResolutionResponse(BaseModel):
    client_id: int
    batch_id: int
    staged_row_id: int
    cleared: bool = False
    resolution: dict[str, object] | None = None


class CrmImportConfirmRequest(BaseModel):
    confirm: bool = False
    plan_fingerprint: str = ""
    use_imported_status_for_existing: bool = False


class CrmImportConfirmResponse(BaseModel):
    batch_id: int
    client_id: int
    status: str = "imported"
    imported_at: str = ""
    imported_by_user_id: int | None = None
    confirmed_plan_fingerprint: str = ""
    created_company_count: int = 0
    reused_company_count: int = 0
    created_contact_count: int = 0
    reused_contact_count: int = 0
    created_relationship_count: int = 0
    existing_relationship_count: int = 0
    no_contact_row_count: int = 0
    total_imported_row_count: int = 0
    imported_status_count: int = 0
    default_status_count: int = 0
    preserved_status_count: int = 0
    notes_set_count: int = 0
    notes_appended_count: int = 0
    notes_duplicate_count: int = 0
    notes_unchanged_count: int = 0


class SharedNoteHistoryPreviewCounts(BaseModel):
    companies_included: int = 0
    companies_excluded_closed: int = 0
    contacts_included: int = 0
    contacts_excluded_closed: int = 0
    history_events_total: int = 0
    history_events_excluded_closed: int = 0
    history_long_notes: int = 0
    history_unattributed: int = 0
    companies_to_create: int = 0
    companies_to_reuse: int = 0
    contacts_to_create: int = 0
    contacts_to_reuse: int = 0
    relationships_to_create: int = 0
    relationships_existing: int = 0
    status_preserved: int = 0
    status_set_on_new_relationships: int = 0
    history_to_insert: int = 0
    history_already_present: int = 0


class SharedNoteHistoryPreviewResponse(BaseModel):
    client_id: int
    ok: bool = True
    errors: list[str] = Field(default_factory=list)
    counts: SharedNoteHistoryPreviewCounts = Field(
        default_factory=SharedNoteHistoryPreviewCounts
    )
    included_record_nos: list[str] = Field(default_factory=list)


class SharedNoteHistoryUploadResult(BaseModel):
    batch_id: int
    client_id: int
    status: str = "previewed"
    preview: SharedNoteHistoryPreviewResponse


class SharedNoteHistoryImportBatchView(BaseModel):
    batch_id: int
    client_id: int
    status: str = ""
    prospects_filename: str = ""
    history_filename: str = ""
    created_at: str = ""
    confirmed_at: str = ""
    plan_fingerprint: str = ""
    staging_cleanup_status: str = ""
    staging_cleanup_error: str = ""
    staging_cleanup_at: str = ""
    preview: SharedNoteHistoryPreviewResponse | None = None


class SharedNoteHistoryConfirmResponse(BaseModel):
    client_id: int
    batch_id: int = 0
    ok: bool = True
    companies_created: int = 0
    companies_reused: int = 0
    contacts_created: int = 0
    contacts_reused: int = 0
    relationships_created: int = 0
    relationships_existing: int = 0
    status_preserved: int = 0
    status_set_on_new_relationships: int = 0
    history_inserted: int = 0
    history_skipped_duplicates: int = 0
    plan_fingerprint: str = ""
    staging_cleanup_status: str = ""
    staging_cleanup_error: str = ""


class ClientDataImportProspectsCounts(BaseModel):
    companies_create: int = 0
    companies_reuse: int = 0
    companies_possible: int = 0
    contacts_create: int = 0
    contacts_reuse: int = 0
    contacts_possible: int = 0
    relationships_create: int = 0
    relationships_existing: int = 0
    statuses_imported: int = 0
    statuses_conflicting: int = 0
    statuses_invalid: int = 0
    notes_set: int = 0
    notes_appended: int = 0
    notes_duplicate: int = 0
    blocking: int = 0
    needs_review: int = 0
    possible_company_match: int = 0
    possible_contact_match: int = 0
    create_company: int = 0
    use_existing_company: int = 0
    possible_company_match_count: int = 0
    create_contact: int = 0
    use_existing_contact: int = 0
    create_client_relationship: int = 0
    relationship_already_exists: int = 0
    use_imported_status: int = 0
    status_conflict: int = 0
    invalid_status: int = 0
    set_imported_notes: int = 0
    append_imported_notes: int = 0
    imported_notes_already_present: int = 0
    blocking_error: int = 0
    needs_review_rows: int = 0
    excluded_closed: int = 0


class ClientDataImportHistoryCounts(BaseModel):
    insert: int = 0
    already_present: int = 0
    duplicate_within_batch: int = 0
    invalid: int = 0
    unresolved: int = 0
    excluded_closed: int = 0
    excluded_marketing: int = 0
    blank_rows: int = 0
    blank_history: int = 0
    unmatched_companies: int = 0
    ambiguous_companies: int = 0
    matched_by_record_no: int = 0
    matched_by_name: int = 0
    needs_review: int = 0


class ClientDataImportBatchView(BaseModel):
    id: int = 0
    batch_id: int = 0
    client_id: int
    crm_batch_id: int | None = 0
    import_mode: str = "full"
    status: str = ""
    original_filename: str = ""
    history_filename: str = ""
    history_original_filename: str = ""
    file_type: str = ""
    worksheet_name: str = ""
    file_size_bytes: int = 0
    sha256: str = ""
    headers: list[str] = Field(default_factory=list)
    history_headers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_message: str = ""
    total_rows: int = 0
    source_row_count: int = 0
    blank_row_count: int = 0
    error_row_count: int = 0
    history_row_count: int = 0
    reusable: bool = False
    expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    cancelled_at: str = ""
    uploaded_by_user_id: int | None = None
    uploaded_by_name: str = ""
    mapping: dict[str, str] = Field(default_factory=dict)
    prospects_mapping: dict[str, str] = Field(default_factory=dict)
    history_mapping: dict[str, str] = Field(default_factory=dict)
    mapping_updated_at: str = ""
    mapping_updated_by_user_id: int | None = None
    closed_policy: str = ""
    closed_policy_note: str = ""
    closed_policy_notes: str = ""
    sample_rows: list[CrmImportRowView] = Field(default_factory=list)
    history_sample_rows: list[dict[str, str]] = Field(default_factory=list)
    staging_cleanup_status: str = ""
    staging_cleanup_error: str = ""
    staging_cleanup_at: str = ""
    plan_fingerprint: str = ""
    closed_excluded_count: int = 0


class ClientDataImportUploadResult(BaseModel):
    kind: str = ""
    needs_worksheet: bool = False
    visible_sheets: list[str] = Field(default_factory=list)
    filename: str = ""
    file_type: str = ""
    message: str = ""
    batch: ClientDataImportBatchView | None = None


class ClientDataImportMappingRequest(BaseModel):
    prospects_mapping: dict[str, str] = Field(default_factory=dict)
    history_mapping: dict[str, str] | None = None


class ClientDataImportDryRunRequest(BaseModel):
    pass


class ClientDataImportDryRunResponse(BaseModel):
    id: int = 0
    batch_id: int = 0
    client_id: int = 0
    import_mode: str = "full"
    plan_fingerprint: str = ""
    status: str = ""
    status_catalog: list[str] = Field(default_factory=list)
    closed_excluded_count: int = 0
    closed_policy: str = ""
    closed_policy_note: str = ""
    closed_policy_notes: str = ""
    confirm_allowed: bool = False
    prospects: ClientDataImportProspectsCounts = Field(
        default_factory=ClientDataImportProspectsCounts
    )
    history: ClientDataImportHistoryCounts = Field(
        default_factory=ClientDataImportHistoryCounts
    )
    counts: dict[str, int] = Field(default_factory=dict)
    rows: list[dict[str, object]] = Field(default_factory=list)
    sample_rows: list[dict[str, object]] = Field(default_factory=list)
    total_rows: int = 0
    crm_plan_fingerprint: str = ""
    companies_excluded_closed: int = 0
    contacts_excluded_closed: int = 0
    history_events_excluded_closed: int = 0


class ClientDataImportConfirmRequest(BaseModel):
    confirm: bool = False
    plan_fingerprint: str = ""


class ClientDataImportConfirmResponse(BaseModel):
    id: int = 0
    batch_id: int = 0
    client_id: int = 0
    status: str = "confirmed"
    imported_at: str = ""
    imported_by_user_id: int | None = None
    confirmed_plan_fingerprint: str = ""
    companies_created: int = 0
    companies_reused: int = 0
    contacts_created: int = 0
    contacts_reused: int = 0
    relationships_created: int = 0
    relationships_existing: int = 0
    statuses_imported: int = 0
    notes_set: int = 0
    notes_appended: int = 0
    notes_duplicate: int = 0
    history_inserted: int = 0
    history_already_present: int = 0
    history_duplicate_within_batch: int = 0
    closed_excluded_count: int = 0
    created_company_count: int = 0
    reused_company_count: int = 0
    created_contact_count: int = 0
    reused_contact_count: int = 0
    created_relationship_count: int = 0
    existing_relationship_count: int = 0
    staging_cleanup_status: str = ""
    staging_cleanup_error: str = ""
    audit: dict[str, object] = Field(default_factory=dict)
