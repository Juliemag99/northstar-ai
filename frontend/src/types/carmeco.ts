export interface ContactSummary {
  id: number
  first_name: string
  last_name: string
  title: string
  phone: string
  phone_extension?: string
  alt_phone: string
  alt_phone_extension?: string
  email: string
  external_record_no: string
}

export interface ContactListItem {
  id: number
  first_name: string
  last_name: string
  full_name: string
  title: string
  phone: string
  phone_extension?: string
  alt_phone: string
  alt_phone_extension?: string
  email: string
  company_id: number | null
  company_name: string
  company_record_no: string
  city: string
  state: string
  client_id: number
  client_name: string
  client_code: string
  relationship_id: number
  status: string
}

export interface ClientRelationshipStatusChip {
  client_id: number
  client_code: string
  client_name: string
  status: string
  relationship_id: number
}

export interface ProspectListItem {
  id: number
  external_record_no: string
  company: string
  city: string
  state: string
  /** Client-company relationship status for the Active/Working client. */
  status: string
  /** Client-neutral alias of status (same CCR value). */
  relationship_status: string
  /** All My Clients: labeled statuses for every accessible client relationship. */
  client_statuses?: ClientRelationshipStatusChip[]
  primary_contact: string
  phone: string
  last_updated: string
  website: string
  address: string
  zip: string
  customer_campaign: string
  contact_count: number
  is_hot: boolean
  has_appointment_set: boolean
  has_quote: boolean
  has_purchase_order: boolean
  has_weblead: boolean
  next_action?: string
  follow_up_date?: string | null
  call_due?: boolean
  follow_up_due?: boolean
  client_id: number
  client_code: string
  client_name: string
  relationship_id: number
}

/** @deprecated Alias kept for older call sites during migration */
export type CarmecoProspect = ProspectListItem

export interface LegacyNote {
  id: number
  note_text: string
  source_field: string
  created_at: string
}

export type RevenueMilestoneType =
  | 'Appointment Set'
  | 'Quote'
  | 'Purchase Order'
  | 'WebLead'
  | 'Hot'

export interface RevenueMilestone {
  id: number
  client: string
  external_record_no: string
  milestone_type: RevenueMilestoneType
  milestone_date: string
  contact_id: number | null
  amount: number | null
  reference_number: string
  source: string
  notes: string
  created_by: string
  created_at: string
}

export interface MilestoneSummary {
  appointments_set: number
  quotes: number
  purchase_orders: number
  webleads: number
  hot: number
}

export interface ClientMilestoneHistory {
  client_id: number
  client_code: string
  client_name: string
  client?: string
  is_hot?: boolean
  milestones: RevenueMilestone[]
}

export interface CompanyWorkspace {
  id: number
  external_record_no: string
  company_name: string
  address: string
  city: string
  state: string
  zip: string
  website: string
  sales_volume_range: string
  location_sales_volume_range: string
  employee_size_range: string
  primary_sic_code: string
  primary_sic_description: string
  primary_naics_code: string
  primary_naics_description: string
  sic_8_digit: string
  sic_8_digit_description: string
  type_of_industry: string
  customer_campaign: string
  entered_at: string
  last_updated_at: string
  status: string
  /** Client-neutral alias of status (same CCR value). */
  relationship_status: string
  legacy_phone: string
  legacy_email: string
  contacts: ContactSummary[]
  legacy_notes: LegacyNote[]
  is_hot: boolean
  milestones: RevenueMilestone[]
  client_history: ClientMilestoneHistory[]
  sales_events?: Array<{
    event_id: number
    event_type: string
    event_date: string
    source_date_time_text: string
    contact_id?: number | null
    contact_name: string
    source_rev_spec_text: string
    source_appointment_grade: string
    caller_notes: string
    sales_notes: string
    quoted_amount?: number | null
    outcome_normalized: string
    source_outcome: string
    source_sheet: string
    source_file_name: string
    source_record_number: string
    source_row?: number
  }>
  recommendation?: NorthStarRecommendation | null
  client_id: number
  client_code: string
  client_name: string
  relationship_id: number
  master_external_record_no?: string
}

export interface ActiveClient {
  id: string
  name: string
  seed_file: string
  prospect_count: number
  company_count?: number
  contact_count?: number
  client_id: number
  code: string
  mode?: 'selected_client' | 'all_my_clients'
}

export interface SharedHistoryClientSummary {
  client_id: number
  client_code: string
  client_name: string
  external_record_no: string
  status: string
  is_hot: boolean
}

export interface SharedHistoryItem {
  item_key: string
  item_type: string
  section: string
  legacy_scope: string
  client_id: number
  client_code: string
  client_name: string
  external_record_no: string
  title: string
  body: string
  event_at: string
  created_by: string
  activity_type: string
  milestone_type: string
  source_table: string
  source_id: number | null
  shared_client_names: string[]
  attribution?: string
  attribution_evidence?: string
  source_file?: string
  event_hash?: string
}

export interface SharedHistoryResponse {
  company_id: number
  company_name: string
  can_view_cross_client: boolean
  filter_client_id: number | null
  clients: SharedHistoryClientSummary[]
  items: SharedHistoryItem[]
  northstar_items: SharedHistoryItem[]
  shared_legacy_items: SharedHistoryItem[]
  distinct_legacy_items: SharedHistoryItem[]
  shared_company_history_items?: SharedHistoryItem[]
  count: number
}

export interface ProspectsResponse {
  client: ActiveClient
  prospects: ProspectListItem[]
  total: number
  client_total: number
  offset: number
  limit: number
  has_previous: boolean
  has_next: boolean
}

export interface ContactsResponse {
  client: ActiveClient
  contacts: ContactListItem[]
  total: number
  client_total: number
  offset: number
  limit: number
}

export interface ClientStatusList {
  client: string
  client_id?: number
  statuses: string[]
}

/** @deprecated Prefer ClientStatusList */
export type CarmecoStatusList = ClientStatusList

export interface FieldUpdateResult {
  external_record_no: string
  client: string
  field_name: string
  old_value: string
  new_value: string
  changed_by: string
  changed_at: string
  workspace: CompanyWorkspace | null
}

export interface SearchHit {
  doc_type: string
  client_id: number
  client_name: string
  client_code: string
  company_id: number | null
  company_name: string
  external_record_no: string
  contact_id: number | null
  contact_name: string | null
  source_table: string
  source_id: number | null
  note_type: string | null
  title: string
  snippet: string
  event_at: string | null
  created_by: string | null
  workspace_path: string | null
  focus_target: string | null
}

export interface SearchResponse {
  query: string
  mode: string
  client_ids: number[]
  total: number
  companies: SearchHit[]
  contacts: SearchHit[]
  notes: SearchHit[]
}

export interface ActivitySummary {
  activity_id: number
  client_id: number
  external_record_no: string
  company_id: number
  relationship_id: number
  user_id: number | null
  contact_id: number | null
  activity_type: string
  activity_at: string
  outcome: string
  notes: string
  follow_up_at: string | null
  assigned_user: string
  created_by: string
  created_at: string
  updated_at: string
}

export interface ActivityTimelineRow {
  item_key: string
  source: string
  activity_id: number | null
  client_id: number
  company_id: number
  relationship_id: number
  external_record_no: string
  company_name: string
  contact_id: number | null
  contact_name: string
  activity_type: string
  activity_at: string
  user_name: string
  user_id: number | null
  outcome: string
  notes: string
  status: string
}

export interface ActivityTimelineResponse {
  client_id: number
  count: number
  total: number
  limit: number
  offset: number
  activity_types: string[]
  users: string[]
  items: ActivityTimelineRow[]
}

export interface OpportunitySignal {
  client_name: string
  milestone_type: RevenueMilestoneType | string
  milestone_date: string
}

export interface CrossClientOpportunity {
  company_id: number
  external_record_no: string
  company_name: string
  city: string
  state: string
  target_client_id: number
  target_client_name: string
  other_clients: Array<{ client_id?: number; client_code?: string; client_name: string }>
  strongest_signal: string
  signal_history: OpportunitySignal[]
  signal_types: string[]
  target_client_status: string
  target_client_activity: string
  last_target_activity: string | null
  opportunity_score: number
  score_label: string
  proven_buyer: boolean
  engaged_elsewhere: boolean
  has_target_relationship?: boolean
  workspace_path: string | null
  review_status: string
  recommended_action: string
  why_summary: string
  source_client_names: string[]
}

export interface CrossClientOpportunityList {
  target_client_id: number | null
  target_client_name: string
  count: number
  data_note: string | null
  opportunities: CrossClientOpportunity[]
}

export interface OpportunityActionResult {
  action?: string
  success?: boolean
  message: string
  target_client_id?: number
  company_id?: number
  created_new_relationship?: boolean
  workspace_path?: string
}

export interface CrossClientBanner {
  applicable: boolean
  target_client_name: string
  target_client_activity?: string
  target_client_status?: string
  evidence: OpportunitySignal[]
  opportunity_score?: number
  score_label?: string
}

export interface WorkQueueNorthStarInsight {
  evaluated: boolean
  fit: string
  engagement: string
  recommendation: string
  why: string
  evidence_used: string[]
  still_need_to_know: string[]
  signals: string[]
  alignment: string
  review_reason: string
  review_flag: string
  advisory_note: string
}

export interface WorkQueueInsightSummary {
  aligned: number
  review: number
  insufficient_ai_data: number
  evaluated: number
  not_evaluated: number
}

export interface WorkQueueRow {
  queue_item_id: string
  work_type: string
  work_priority: number
  priority_label: string
  client_id: number
  client_code: string
  client_name: string
  company_id: number
  relationship_id: number
  external_record_no: string
  company_name: string
  city: string
  state: string
  contact_id: number | null
  contact_name: string
  status: string
  due_date: string | null
  due_time: string
  is_overdue: boolean
  last_activity_at: string | null
  last_activity_summary: string
  next_action: string
  why_in_queue: string
  is_hot: boolean
  is_weblead: boolean
  weblead_age_label: string | null
  is_new_weblead: boolean
  is_cross_client: boolean
  opportunity_score: number | null
  source_client_summary?: string
  assigned_user: string
  assigned_user_id: number | null
  source: string
  source_id: number | null
  completion_status: string
  northstar_insight?: WorkQueueNorthStarInsight | null
}

export interface WorkQueueSummaryV2 {
  calls_due: number
  follow_ups_due: number
  appointments: number
  hot: number
  webleads: number
  new_assignments: number
  needs_next_action: number
  cross_client_opportunities: number
  overdue: number
}

export interface DashboardFollowUpItem {
  client_id: number
  company_id: number
  contact_id: number | null
  contact_name: string
  company_name: string
  external_record_no: string
  due_date: string
  due_time: string
  assigned_user: string
  assigned_user_id: number | null
  status: string
  source: string
  source_id: number | null
  bucket: string
  next_action: string
}

export interface DashboardFollowUpsResponse {
  client_id: number
  overdue: DashboardFollowUpItem[]
  due_today: DashboardFollowUpItem[]
  upcoming: DashboardFollowUpItem[]
  overdue_count: number
  due_today_count: number
  upcoming_count: number
}

export interface WorkQueueListResponse {
  user_id: number
  mode: string
  client_ids: number[]
  count: number
  total: number
  offset: number
  limit: number
  has_previous: boolean
  has_next: boolean
  summary: WorkQueueSummaryV2
  items: WorkQueueRow[]
  northstar_insight_summary?: WorkQueueInsightSummary
}

export interface WorkQueueNextResponse {
  user_id: number
  mode: string
  client_ids: number[]
  after_queue_item_id: string
  has_next: boolean
  end_of_results: boolean
  total: number
  position: number | null
  item: WorkQueueRow | null
  message: string
}

export type AskScope = 'all' | 'active_client'

export interface AskSource {
  label: string
  detail: string
}

export interface AskLink {
  label: string
  href: string
}

export interface AskResearchOption {
  id: string
  label: string
  status: string
  enabled: boolean
  description: string
}

export interface AskSection {
  id: string
  title: string
  body: string
  items: Record<string, unknown>[]
}

export interface AskCompanyCard {
  company_id: number
  company_name: string
  external_record_no: string
  client_id: number | null
  client_name: string
  status: string
  city: string
  state: string
  badges: string[]
  why: string
  opportunity_score: number | null
  workspace_path: string
  rank: number | null
  next_action: string
  work_priority: number | null
  work_type: string
  northstar_recommendation?: string
  northstar_fit?: string
  northstar_engagement?: string
  northstar_alignment?: string
  northstar_recommendation_why?: string
  northstar_insight_note?: string
}

export interface AskContactCard {
  contact_id: number | null
  name: string
  title: string
  phone: string
  email: string
  client_id: number | null
  client_name: string
  source: string
}

export interface AskNoteHit {
  company_id: number | null
  company_name: string
  external_record_no: string
  client_id: number | null
  client_name: string
  excerpt: string
  event_at: string
  source: string
  doc_type: string
  workspace_path: string
}

export interface AskMilestoneBadge {
  milestone_type: string
  label: string
  milestone_date: string
  client_id: number | null
  client_name: string
  source: string
}

export interface AskNorthStarResponse {
  question: string
  intent: string
  summary: string
  scope: string
  scope_label: string
  active_client_id: number | null
  active_client_name: string
  no_data: boolean
  read_only: boolean
  result_count: number
  sections: AskSection[]
  companies: AskCompanyCard[]
  contacts: AskContactCard[]
  notes: AskNoteHit[]
  milestones: AskMilestoneBadge[]
  sources: AskSource[]
  recommended_links: AskLink[]
  research_options: AskResearchOption[]
  research_available: boolean
  history_id: number | null
}

export interface AskHistoryItem {
  id: number
  question: string
  scope: string
  active_client_id: number | null
  active_client_name: string
  intent: string
  answer_summary: string
  created_at: string
}

export interface AskHistoryResponse {
  items: AskHistoryItem[]
  count: number
  filter_scope?: string
  filter_client_id?: number | null
  filter_client_name?: string
}

export interface ResearchFindingView {
  id?: number | null
  finding_type: string
  field_key: string
  value: string
  source_name: string
  source_url: string
  researched_at: string
  confidence: string
  evidence_level: string
  page_title: string
  is_public_contact: boolean
  contact_name: string
  contact_title: string
  provider_id: string
  why_relevant?: string
  matched_persona?: string
  crm_match_status?: string
  crm_match_label?: string
  crm_matched_contact_name?: string
  contact_email?: string
  contact_phone?: string
  linkedin_url?: string
  source_labels?: string[]
  linkedin_derived?: boolean
  relevance_tier?: string
  crm_matched_contact_id?: number | null
  crm_match_reasons?: string[]
}

export interface ResearchProposedUpdateView {
  id: number
  company_id: number
  field_key: string
  current_value: string
  proposed_value: string
  source_name: string
  source_url: string
  research_date: string
  confidence: string
  status: string
}

export interface ResearchFitView {
  client_id: number
  client_name: string
  campaign_id: number | null
  campaign_name: string
  fit_result: string
  why: string
  supporting_evidence: string[]
  evidence_chains: Array<{ fact?: string; why?: string; kind?: string }>
  potential_opportunity: string
  concerns: string[]
  missing_information: string[]
  profile_fields_used: string[]
  profile_incomplete: boolean
  profile_gaps: string[]
}

export interface ResearchDecisionSummary {
  client_name: string
  campaign_name: string
  fit_result: string
  why: string
  still_need: string
}

export interface ResearchEngagementAssessment {
  level: string
  label: string
  signals: string[]
  attributed_to: string
  why: string
  note: string
  cross_client_signals: Array<{
    client_name?: string
    signals?: string[]
    attribution?: string
  }>
}

export interface NorthStarRecommendation {
  action: string
  why: string
  evidence_used: string[]
  still_need_to_know: string[]
  fit_context: string
  engagement_context: string
  confidence: string
  advisory_note: string
}

export interface ResearchVerificationItem {
  field_key: string
  label: string
  northstar_value: string
  research_value: string
  source_name: string
  source_url: string
  match: boolean
  proposed_update_id: number | null
}

export interface ResearchNorthStarKnown {
  company_name: string
  master_record_no: string
  website: string
  city: string
  state: string
  address: string
  working_for_client_id: number | null
  working_for_client_name: string
  working_for_record_no: string
  working_for_status: string
  has_relationship: boolean
  contacts: Record<string, unknown>[]
  contacts_total?: number
  milestones: Record<string, unknown>[]
  notes: Record<string, unknown>[]
  activities: Record<string, unknown>[]
  cross_client: Record<string, unknown>[]
  previous_research: Record<string, unknown>[]
  opportunity_score: number | null
}

export interface ResearchCompanyResponse {
  research_run_id: number | null
  company_id: number
  company_name: string
  working_for_client_id: number | null
  working_for_client_name: string
  working_for_record_no: string
  working_for_status: string
  campaign_id: number | null
  campaign_name: string
  campaign_choices: Array<{
    campaign_id: number
    campaign_name: string
    is_default?: boolean
  }>
  needs_working_for: boolean
  working_for_choices: Record<string, unknown>[]
  summary: string
  decision_summary: ResearchDecisionSummary | null
  engagement: ResearchEngagementAssessment | null
  recommendation: NorthStarRecommendation | null
  last_researched_at: string
  initiated_by: string
  northstar_known: ResearchNorthStarKnown | null
  verified_matches: ResearchVerificationItem[]
  possible_changes: ResearchVerificationItem[]
  overview: ResearchFindingView[]
  locations: ResearchFindingView[]
  capabilities: ResearchFindingView[]
  products: ResearchFindingView[]
  materials: ResearchFindingView[]
  industries: ResearchFindingView[]
  recent_developments: ResearchFindingView[]
  missing_information: string[]
  northstar_contacts: Record<string, unknown>[]
  northstar_contacts_total?: number
  public_contacts: ResearchFindingView[]
  people_discovery?: Record<string, unknown>
  cross_client_experience: Record<string, unknown>[]
  fit: ResearchFitView | null
  proposed_updates: ResearchProposedUpdateView[]
  sources: Record<string, unknown>[]
  pages_researched: Record<string, unknown>[]
  data_provider: Record<string, unknown>
  research_history: Record<string, unknown>[]
  read_only_crm: boolean
  research_depth?: string
  job?: ResearchJobView | null
  citations?: Array<{ url: string; title: string }>
  deep_research_usage?: Record<string, unknown>
  deep_research_from_cache?: boolean
  deep_research_paid_refresh_required?: boolean
}

export interface ResearchJobView {
  job_id: number
  company_id: number
  working_for_client_id: number
  campaign_id: number | null
  research_run_id: number | null
  research_depth: string
  status: string
  progress: number
  progress_message: string
  cancel_requested: boolean
  attempt_count: number
  max_attempts: number
  openai_response_id: string
  openai_model: string
  usage: Record<string, unknown>
  citations: Array<{ url: string; title: string }>
  sources: Array<{ url: string; title: string }>
  error_message: string
  started_at: string
  completed_at: string
  created_at: string
  updated_at: string
  deep_research_configured: boolean
  from_cache?: boolean
  paid_refresh_required?: boolean
  limited_by?: string
}

export interface DeepResearchStatus {
  deep_research_configured: boolean
  limits?: {
    max_web_search_calls: number
    max_output_tokens: number
    max_attempts: number
    max_run_usd: number
    monthly_limit_usd: number
    cache_days: number
    dollar_limits_enforceable: boolean
    estimated_max_run_usd: number | null
    estimated_max_run_status: string
    estimated_max_run_note: string
    pilot_admin_only: boolean
  }
  pricing?: {
    estimate_available: boolean
    note: string
  }
  monthly?: {
    month_start_utc?: string
    spent_usd: number | null
    remaining_usd: number | null
    monthly_limit_usd: number
    status: string
    dollar_limits_enforceable?: boolean
  }
}

export type CampaignStatus = 'Draft' | 'Active' | 'Paused' | 'Completed' | 'Archived'

export interface CampaignSummary {
  campaign_id: number
  client_id: number
  client_name: string
  client_code: string
  campaign_name: string
  description: string
  category: string
  owner_name: string
  owner_user_id: number | null
  status: CampaignStatus | string
  start_date: string
  end_date: string
  company_count: number
  contact_count: number
  call_count: number
  follow_up_count: number
  appointment_count: number
  outcome_count: number
  created_at: string
  updated_at: string
}

export interface CampaignListResponse {
  client_id: number
  count: number
  total: number
  limit: number
  offset: number
  statuses: string[]
  owners: string[]
  categories: string[]
  items: CampaignSummary[]
}

export interface CampaignCompanyRow {
  company_id: number
  company_name: string
  external_record_no: string
  city: string
  state: string
  status: string
  client_id: number
}

export interface CampaignContactRow {
  contact_id: number
  company_id: number
  company_name: string
  contact_name: string
  title: string
  external_record_no: string
  client_id: number
  is_primary_routed_contact?: boolean
}

export interface CampaignWorkspace {
  campaign: CampaignSummary
  notes: string
  companies: CampaignCompanyRow[]
  contacts: CampaignContactRow[]
}

export interface CampaignMemberSearchResponse {
  companies: CampaignCompanyRow[]
  contacts: CampaignContactRow[]
}

export interface CampaignChoice {
  campaign_id: number
  campaign_name: string
  status: string
  is_default: boolean
  client_id: number
  recommended: boolean
}

export interface CampaignRouteSuggestion {
  client_id: number
  company_id: number
  contact_id: number | null
  company_name: string
  should_prompt: boolean
  already_assigned: boolean
  assigned_campaign_ids: number[]
  assigned_campaign_names: string[]
  recommended_campaign_id: number | null
  auto_unassigned: boolean
  message: string
  campaigns: CampaignChoice[]
}

export interface UnassignedOpportunity {
  unassigned_id: number
  client_id: number
  client_name: string
  company_id: number
  company_name: string
  external_record_no: string
  contact_id: number | null
  contact_name: string
  source: string
  source_type: string
  status: string
  identified_at: string
  assigned_rep: string
  campaign_id: number | null
  recommended_campaign_id: number | null
  recommended_campaign_name: string
}

export interface UnassignedOpportunityList {
  client_id: number
  total: number
  limit: number
  offset: number
  q: string
  items: UnassignedOpportunity[]
  campaigns: CampaignChoice[]
}

export interface UnassignedBulkAssignResult {
  client_id: number
  client_name: string
  campaign_id: number
  campaign_name: string
  assigned: number
  skipped: number
  failed: number
  total_requested: number
  message: string
}

export interface ReportKpi {
  key: string
  label: string
  value: number | null
  definition: string
}

export interface ReportFilterOption {
  id: number
  name: string
  client_id: number | null
  client_name: string
}

export interface ReportFiltersResponse {
  client_id: number
  client_ids: number[]
  date_from: string
  date_to: string
  reps: ReportFilterOption[]
  campaigns: ReportFilterOption[]
  activity_types: string[]
  outcome_types: string[]
  definitions: Record<string, string>
}

export interface ReportClientResultsRow {
  client_id: number
  client_name: string
  client_code: string
  calls: number
  follow_ups_scheduled: number
  follow_ups_completed: number
  appointments_set: number
  appointments_completed: number
  appointments_cancelled: number
  opportunities: number
  outcomes: number
  hot_prospects: number
}

export interface ReportClientResultsResponse {
  client_id: number
  date_from: string
  date_to: string
  total: number
  limit: number
  offset: number
  sort_by: string
  sort_dir: string
  kpis: ReportKpi[]
  items: ReportClientResultsRow[]
}

export interface ReportTeamRow {
  user_id: number | null
  user_name: string
  assigned_clients: number
  calls: number
  notes: number
  follow_ups_completed: number
  appointments_set: number
  appointments_completed: number
  outcomes: number
  overdue_tasks: number
}

export interface ReportTeamResponse {
  client_id: number
  date_from: string
  date_to: string
  total: number
  limit: number
  offset: number
  sort_by: string
  sort_dir: string
  kpis: ReportKpi[]
  items: ReportTeamRow[]
}

export interface ReportCampaignRow {
  campaign_id: number
  campaign_name: string
  client_id: number
  client_name: string
  companies: number
  contacts: number
  calls: number
  follow_ups: number
  appointments: number
  outcomes: number
  call_to_appointment_pct: number | null
  outcome_to_call_pct: number | null
}

export interface ReportCampaignResponse {
  client_id: number
  date_from: string
  date_to: string
  total: number
  limit: number
  offset: number
  sort_by: string
  sort_dir: string
  kpis: ReportKpi[]
  items: ReportCampaignRow[]
}

export interface ReportRecordRow {
  record_key: string
  record_kind: string
  record_id: number | null
  client_id: number
  client_name: string
  company_id: number | null
  company_name: string
  external_record_no: string
  contact_id: number | null
  contact_name: string
  occurred_at: string
  record_type: string
  user_name: string
  outcome: string
  notes: string
  status: string
}

export interface ReportRecordsResponse {
  section: string
  metric: string
  total: number
  limit: number
  offset: number
  items: ReportRecordRow[]
}
