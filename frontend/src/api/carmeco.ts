import { type NextActionCatalog } from '../nextAction'
import { requireWriteClientId } from '../writeClient'
import { apiFetch as fetch } from './http'
import type {
  ActiveClient,
  ActivitySummary,
  ActivityTimelineResponse,
  AskHistoryResponse,
  AskNorthStarResponse,
  AskScope,
  ClientStatusList,
  CompanyWorkspace,
  ContactListItem,
  ContactSummary,
  ContactsResponse,
  FieldUpdateResult,
  LegacyNote,
  MilestoneSummary,
  ProspectListItem,
  ProspectsResponse,
  RevenueMilestone,
  RevenueMilestoneType,
  SearchHit,
  SearchResponse,
  SharedHistoryResponse,
  SharedHistoryItem,
  ClientMilestoneHistory,
  CrossClientBanner,
  CrossClientOpportunityList,
  OpportunityActionResult,
  WorkQueueListResponse,
  WorkQueueRow,
  WorkQueueSummaryV2,
  DashboardFollowUpItem,
  DashboardFollowUpsResponse,
  CampaignListResponse,
  CampaignMemberSearchResponse,
  CampaignRouteSuggestion,
  CampaignSummary,
  CampaignWorkspace,
  UnassignedBulkAssignResult,
  UnassignedOpportunityList,
  ReportCampaignResponse,
  ReportClientResultsResponse,
  ReportFiltersResponse,
  ReportRecordsResponse,
  ReportTeamResponse,
} from '../types/carmeco'

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text()
    let message = detail || `Request failed (${response.status})`
    try {
      const parsed = JSON.parse(detail) as { detail?: unknown }
      if (typeof parsed.detail === 'string' && parsed.detail.trim()) {
        message = parsed.detail
      } else if (Array.isArray(parsed.detail)) {
        message = parsed.detail
          .map((item) => {
            if (typeof item === 'string') return item
            if (item && typeof item === 'object' && 'msg' in item) {
              return String((item as { msg: unknown }).msg)
            }
            return JSON.stringify(item)
          })
          .join('; ')
      }
    } catch {
      /* keep raw text */
    }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

function pick(record: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = record[key]
    if (value == null) continue
    const text = String(value).trim()
    if (text) return text
  }
  return ''
}

function asNumber(value: unknown, fallback = 0): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function normalizeContact(raw: unknown): ContactSummary {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    id: asNumber(record.id),
    first_name: pick(record, 'first_name'),
    last_name: pick(record, 'last_name'),
    title: pick(record, 'title'),
    phone: pick(record, 'phone'),
    alt_phone: pick(record, 'alt_phone'),
    email: pick(record, 'email'),
    external_record_no: pick(record, 'external_record_no'),
  }
}

function normalizeNote(raw: unknown): LegacyNote {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    id: asNumber(record.id),
    note_text: pick(record, 'note_text'),
    source_field: pick(record, 'source_field') || 'Sales Rep Comments/Notes',
    created_at: pick(record, 'created_at'),
  }
}

function asBoolean(value: unknown): boolean {
  return value === true || value === 1 || value === '1' || String(value).toLowerCase() === 'true'
}

function normalizeMilestone(raw: unknown): RevenueMilestone {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    id: asNumber(record.id, asNumber(record.milestone_id)),
    client: pick(record, 'client') || '',
    external_record_no: pick(record, 'external_record_no'),
    milestone_type: (pick(record, 'milestone_type') || 'Appointment Set') as RevenueMilestoneType,
    milestone_date: pick(record, 'milestone_date'),
    contact_id: record.contact_id == null ? null : asNumber(record.contact_id),
    amount: record.amount == null || record.amount === '' ? null : asNumber(record.amount),
    reference_number: pick(record, 'reference_number'),
    source: pick(record, 'source'),
    notes: pick(record, 'notes'),
    created_by: pick(record, 'created_by'),
    created_at: pick(record, 'created_at'),
  }
}

function normalizeClientHistory(raw: unknown): ClientMilestoneHistory {
  const record = (raw ?? {}) as Record<string, unknown>
  const milestones = Array.isArray(record.milestones)
    ? record.milestones.map(normalizeMilestone)
    : []
  return {
    client_id: asNumber(record.client_id),
    client_code: pick(record, 'client_code'),
    client_name: pick(record, 'client_name', 'client'),
    client: pick(record, 'client_name', 'client'),
    is_hot: asBoolean(record.is_hot),
    milestones,
  }
}

function normalizeClientStatusChip(raw: unknown): {
  client_id: number
  client_code: string
  client_name: string
  status: string
  relationship_id: number
} {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    client_id: asNumber(record.client_id),
    client_code: pick(record, 'client_code'),
    client_name: pick(record, 'client_name'),
    status: pick(record, 'status'),
    relationship_id: asNumber(record.relationship_id),
  }
}

function normalizeProspect(raw: unknown): ProspectListItem {
  const record = (raw ?? {}) as Record<string, unknown>
  // Canonical: CCR status for the Active/Working client (never a Carmeco-owned field).
  const status = pick(record, 'status', 'relationship_status')
  const clientStatusesRaw = record.client_statuses
  const client_statuses = Array.isArray(clientStatusesRaw)
    ? clientStatusesRaw.map(normalizeClientStatusChip)
    : []
  return {
    id: asNumber(record.id),
    external_record_no: pick(record, 'external_record_no', 'master_id', 'Master ID'),
    company: pick(record, 'company', 'Company', 'company_name'),
    city: pick(record, 'city', 'City'),
    state: pick(record, 'state', 'State'),
    status,
    relationship_status: status,
    client_statuses,
    primary_contact: pick(
      record,
      'primary_contact',
      'likely_decision_maker',
      'Likely Decision Maker',
    ),
    phone: pick(record, 'phone', 'main_phone', 'Main Phone'),
    last_updated: pick(record, 'last_updated', 'last_updated_at'),
    website: pick(record, 'website', 'official_website'),
    address: pick(record, 'address', 'manufacturing_address'),
    zip: pick(record, 'zip'),
    customer_campaign: pick(record, 'customer_campaign'),
    contact_count: asNumber(record.contact_count),
    is_hot: asBoolean(record.is_hot),
    has_appointment_set: asBoolean(record.has_appointment_set),
    has_quote: asBoolean(record.has_quote),
    has_purchase_order: asBoolean(record.has_purchase_order),
    has_weblead: asBoolean(record.has_weblead),
    next_action: pick(record, 'next_action'),
    follow_up_date: pick(record, 'follow_up_date') || null,
    call_due: asBoolean(record.call_due),
    follow_up_due: asBoolean(record.follow_up_due),
    client_id: asNumber(record.client_id),
    client_code: pick(record, 'client_code'),
    client_name: pick(record, 'client_name'),
    relationship_id: asNumber(record.relationship_id),
  }
}

function normalizeActiveClient(raw: unknown, prospectCountFallback = 0): ActiveClient {
  const record = (raw ?? {}) as Record<string, unknown>
  const code = pick(record, 'code', 'id')
  const clientId = asNumber(record.client_id)
  return {
    id: pick(record, 'id') || code || String(clientId || ''),
    name: pick(record, 'name', 'client_name') || code || 'Client',
    seed_file: pick(record, 'seed_file') || 'database/northstar.db',
    prospect_count: asNumber(record.prospect_count, prospectCountFallback),
    company_count: asNumber(record.company_count, prospectCountFallback),
    contact_count: asNumber(record.contact_count),
    client_id: clientId,
    code,
    mode:
      pick(record, 'mode') === 'all_my_clients' ? 'all_my_clients' : 'selected_client',
  }
}

function normalizeProspectsResponse(raw: unknown): ProspectsResponse {
  const payload = (raw ?? {}) as {
    client?: unknown
    prospects?: unknown[]
    total?: unknown
    client_total?: unknown
    offset?: unknown
    limit?: unknown
  }
  const prospects = (payload.prospects ?? []).map(normalizeProspect)
  const total = asNumber(payload.total, prospects.length)
  const clientTotal = asNumber(payload.client_total, total)
  const limitRaw = payload.limit
  const limit =
    limitRaw == null || limitRaw === ''
      ? null
      : asNumber(limitRaw, prospects.length)
  return {
    client: normalizeActiveClient(payload.client, clientTotal),
    prospects,
    total,
    client_total: clientTotal,
    offset: asNumber(payload.offset, 0),
    limit,
  }
}

function normalizeWorkspace(raw: unknown): CompanyWorkspace {
  const record = (raw ?? {}) as Record<string, unknown>
  const contacts = Array.isArray(record.contacts)
    ? record.contacts.map(normalizeContact)
    : []
  const legacyNotes = Array.isArray(record.legacy_notes)
    ? record.legacy_notes.map(normalizeNote)
    : []
  const milestones = Array.isArray(record.milestones)
    ? record.milestones.map(normalizeMilestone)
    : []
  const clientHistory = Array.isArray(record.client_history)
    ? record.client_history.map(normalizeClientHistory)
    : []
  return {
    id: asNumber(record.id),
    external_record_no: pick(record, 'external_record_no'),
    company_name: pick(record, 'company_name', 'company'),
    address: pick(record, 'address'),
    city: pick(record, 'city'),
    state: pick(record, 'state'),
    zip: pick(record, 'zip'),
    website: pick(record, 'website'),
    sales_volume_range: pick(record, 'sales_volume_range'),
    location_sales_volume_range: pick(record, 'location_sales_volume_range'),
    employee_size_range: pick(record, 'employee_size_range'),
    primary_sic_code: pick(record, 'primary_sic_code'),
    primary_sic_description: pick(record, 'primary_sic_description'),
    primary_naics_code: pick(record, 'primary_naics_code'),
    primary_naics_description: pick(record, 'primary_naics_description'),
    sic_8_digit: pick(record, 'sic_8_digit'),
    sic_8_digit_description: pick(record, 'sic_8_digit_description'),
    type_of_industry: pick(record, 'type_of_industry'),
    customer_campaign: pick(record, 'customer_campaign'),
    entered_at: pick(record, 'entered_at'),
    last_updated_at: pick(record, 'last_updated_at'),
    status: (() => {
      const status = pick(record, 'status', 'relationship_status')
      return status
    })(),
    relationship_status: pick(record, 'status', 'relationship_status'),
    legacy_phone: pick(record, 'legacy_phone'),
    legacy_email: pick(record, 'legacy_email'),
    contacts,
    legacy_notes: legacyNotes,
    is_hot: asBoolean(record.is_hot),
    client_id: asNumber(record.client_id),
    client_code: pick(record, 'client_code'),
    client_name: pick(record, 'client_name'),
    relationship_id: asNumber(record.relationship_id),
    master_external_record_no: pick(record, 'master_external_record_no'),
    milestones,
    client_history: clientHistory,
    sales_events: Array.isArray(record.sales_events)
      ? (record.sales_events as CompanyWorkspace['sales_events'])
      : [],
    recommendation: (() => {
      const r = (record.recommendation || null) as Record<string, unknown> | null
      if (!r) return null
      return {
        action: pick(r, 'action'),
        why: pick(r, 'why'),
        evidence_used: Array.isArray(r.evidence_used) ? r.evidence_used.map(String) : [],
        still_need_to_know: Array.isArray(r.still_need_to_know)
          ? r.still_need_to_know.map(String)
          : [],
        fit_context: pick(r, 'fit_context'),
        engagement_context: pick(r, 'engagement_context'),
        confidence: pick(r, 'confidence'),
        advisory_note: pick(r, 'advisory_note'),
      }
    })(),
  }
}

export async function fetchActiveClient(): Promise<ActiveClient> {
  const response = await fetch('/api/client')
  return parseJson<ActiveClient>(response)
}

export async function fetchProspects(params?: {
  client_id?: number | null
  all_clients?: boolean
  q?: string
  limit?: number | null
  offset?: number
}): Promise<ProspectsResponse> {
  const query = new URLSearchParams()
  if (params?.all_clients) {
    query.set('all_clients', 'true')
  } else if (params?.client_id != null && Number(params.client_id) > 0) {
    query.set('client_id', String(params.client_id))
  }
  if (params?.q != null && String(params.q).trim()) {
    query.set('q', String(params.q).trim())
  }
  if (params?.limit != null && Number.isFinite(params.limit) && params.limit > 0) {
    query.set('limit', String(params.limit))
  }
  if (params?.offset != null && Number.isFinite(params.offset) && params.offset > 0) {
    query.set('offset', String(params.offset))
  }
  const qs = query.toString()
  const response = await fetch(`/api/prospects${qs ? `?${qs}` : ''}`)
  const raw = await parseJson<unknown>(response)
  return normalizeProspectsResponse(raw)
}

export async function fetchMilestoneSummary(): Promise<MilestoneSummary> {
  const response = await fetch('/api/milestones/summary')
  const raw = await parseJson<Record<string, unknown>>(response)
  return {
    appointments_set: asNumber(raw.appointments_set),
    quotes: asNumber(raw.quotes),
    purchase_orders: asNumber(raw.purchase_orders),
    webleads: asNumber(raw.webleads),
    hot: asNumber(raw.hot),
  }
}

export async function createMilestone(params: {
  client?: string
  client_id: number
  external_record_no: string
  milestone_type: RevenueMilestoneType
  milestone_date: string
  contact_id?: number | null
  amount?: number | null
  reference_number?: string
  source?: string
  notes?: string
  created_by?: string
}): Promise<RevenueMilestone> {
  const writeClientId = requireWriteClientId(params.client_id)
  const response = await fetch('/api/milestones', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...params, client_id: writeClientId, client: params.client || '' }),
  })
  return normalizeMilestone(await parseJson<unknown>(response))
}

export async function setCompanyHot(params: {
  client?: string
  client_id: number
  external_record_no: string
  is_hot: boolean
  notes?: string
  created_by?: string
}): Promise<CompanyWorkspace | null> {
  const writeClientId = requireWriteClientId(params.client_id)
  const response = await fetch('/api/milestones/hot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...params, client_id: writeClientId, client: params.client || '' }),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return raw.workspace ? normalizeWorkspace(raw.workspace) : null
}

export async function fetchCompanyWorkspace(companyId: number): Promise<CompanyWorkspace> {
  const response = await fetch(`/api/companies/${companyId}`)
  const raw = await parseJson<unknown>(response)
  return normalizeWorkspace(raw)
}

export async function fetchCompanyByRecordNo(
  recordNo: string,
  clientId?: number | null,
): Promise<CompanyWorkspace> {
  const query = new URLSearchParams()
  if (clientId != null) query.set('client_id', String(clientId))
  const qs = query.toString()
  const response = await fetch(
    `/api/companies/by-record/${encodeURIComponent(recordNo.trim())}${qs ? `?${qs}` : ''}`,
  )
  const raw = await parseJson<unknown>(response)
  return normalizeWorkspace(raw)
}

export async function fetchSharedHistory(
  recordNo: string,
  params?: {
    client_id?: number | null
    filter_client_id?: number | null
  },
): Promise<SharedHistoryResponse> {
  const query = new URLSearchParams()
  if (params?.client_id != null) query.set('client_id', String(params.client_id))
  if (params?.filter_client_id != null) {
    query.set('filter_client_id', String(params.filter_client_id))
  }
  const qs = query.toString()
  const response = await fetch(
    `/api/companies/by-record/${encodeURIComponent(recordNo.trim())}/shared-history${
      qs ? `?${qs}` : ''
    }`,
  )
  const raw = await parseJson<Record<string, unknown>>(response)
  const clients = Array.isArray(raw.clients)
    ? raw.clients.map((c) => {
        const row = (c ?? {}) as Record<string, unknown>
        return {
          client_id: asNumber(row.client_id),
          client_code: pick(row, 'client_code'),
          client_name: pick(row, 'client_name'),
          external_record_no: pick(row, 'external_record_no'),
          status: pick(row, 'status'),
          is_hot: asBoolean(row.is_hot),
        }
      })
    : []
  const mapItem = (item: unknown): SharedHistoryItem => {
    const row = (item ?? {}) as Record<string, unknown>
    const sharedNames = Array.isArray(row.shared_client_names)
      ? row.shared_client_names.map((v) => String(v))
      : []
    return {
      item_key: pick(row, 'item_key'),
      item_type: pick(row, 'item_type'),
      section: pick(row, 'section'),
      legacy_scope: pick(row, 'legacy_scope'),
      client_id: asNumber(row.client_id),
      client_code: pick(row, 'client_code'),
      client_name: pick(row, 'client_name'),
      external_record_no: pick(row, 'external_record_no'),
      title: pick(row, 'title'),
      body: pick(row, 'body'),
      event_at: pick(row, 'event_at'),
      created_by: pick(row, 'created_by'),
      activity_type: pick(row, 'activity_type'),
      milestone_type: pick(row, 'milestone_type'),
      source_table: pick(row, 'source_table'),
      source_id: row.source_id == null ? null : asNumber(row.source_id),
      shared_client_names: sharedNames,
    }
  }
  const items = Array.isArray(raw.items) ? raw.items.map(mapItem) : []
  const northstarItems = Array.isArray(raw.northstar_items)
    ? raw.northstar_items.map(mapItem)
    : items.filter((i) => i.section === 'northstar' || i.item_type === 'activity')
  const sharedLegacyItems = Array.isArray(raw.shared_legacy_items)
    ? raw.shared_legacy_items.map(mapItem)
    : items.filter((i) => i.section === 'shared_legacy' || i.item_type === 'shared_legacy')
  const distinctLegacyItems = Array.isArray(raw.distinct_legacy_items)
    ? raw.distinct_legacy_items.map(mapItem)
    : items.filter((i) => i.section === 'distinct_legacy' || i.item_type === 'legacy_note')
  return {
    company_id: asNumber(raw.company_id),
    company_name: pick(raw, 'company_name'),
    can_view_cross_client: asBoolean(raw.can_view_cross_client),
    filter_client_id:
      raw.filter_client_id == null || raw.filter_client_id === ''
        ? null
        : asNumber(raw.filter_client_id),
    clients,
    items,
    northstar_items: northstarItems,
    shared_legacy_items: sharedLegacyItems,
    distinct_legacy_items: distinctLegacyItems,
    count: asNumber(raw.count, items.length),
  }
}

export async function fetchClientStatuses(clientId?: number | null): Promise<string[]> {
  const url =
    clientId != null && clientId > 0
      ? `/api/clients/${encodeURIComponent(String(clientId))}/statuses`
      : '/api/carmeco/statuses?all_clients=true'
  const response = await fetch(url)
  const raw = await parseJson<ClientStatusList>(response)
  return Array.isArray(raw.statuses) ? raw.statuses.map((s) => String(s).trim()).filter(Boolean) : []
}

/** @deprecated Prefer fetchClientStatuses — name kept for existing imports. */
export async function fetchCarmecoStatuses(clientId?: number | null): Promise<string[]> {
  return fetchClientStatuses(clientId)
}

export async function fetchNextActions(clientId?: number | null): Promise<NextActionCatalog> {
  const url =
    clientId != null && clientId > 0
      ? `/api/clients/${encodeURIComponent(String(clientId))}/next-actions`
      : '/api/next-actions'
  const response = await fetch(url)
  const raw = await parseJson<Record<string, unknown>>(response)
  const choices = Array.isArray(raw.choices)
    ? raw.choices.map((item) => {
        const record = (item ?? {}) as Record<string, unknown>
        return {
          code: pick(record, 'code'),
          label: pick(record, 'label'),
          aliases: Array.isArray(record.aliases)
            ? record.aliases.map((alias) => String(alias || '').trim()).filter(Boolean)
            : [],
          requires_detail: asBoolean(record.requires_detail),
        }
      }).filter((choice) => choice.code && choice.label)
    : []
  return {
    client_id: asNumber(raw.client_id) || (clientId != null && clientId > 0 ? clientId : 0),
    choices,
    default_for_closed_code: pick(raw, 'default_for_closed_code') || 'no_next_action',
  }
}

function normalizeFieldUpdate(raw: unknown): FieldUpdateResult {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    external_record_no: pick(record, 'external_record_no'),
    client: pick(record, 'client') || '',
    field_name: pick(record, 'field_name'),
    old_value: pick(record, 'old_value'),
    new_value: pick(record, 'new_value'),
    changed_by: pick(record, 'changed_by'),
    changed_at: pick(record, 'changed_at'),
    workspace: record.workspace ? normalizeWorkspace(record.workspace) : null,
  }
}

export async function updateCompanyStatus(
  recordNo: string,
  status: string,
  user = 'Julie Magnani',
  clientId?: number | null,
  clientName = '',
): Promise<FieldUpdateResult> {
  const writeClientId = requireWriteClientId(clientId)
  const response = await fetch(
    `/api/companies/by-record/${encodeURIComponent(recordNo.trim())}/status`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client: clientName,
        client_id: writeClientId,
        status,
        user,
      }),
    },
  )
  const raw = await parseJson<unknown>(response)
  return normalizeFieldUpdate(raw)
}

export async function updateCompanyNotes(
  recordNo: string,
  noteText: string,
  user = 'Julie Magnani',
  clientId?: number | null,
  clientName = '',
): Promise<FieldUpdateResult> {
  const writeClientId = requireWriteClientId(clientId)
  const response = await fetch(
    `/api/companies/by-record/${encodeURIComponent(recordNo.trim())}/notes`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client: clientName,
        client_id: writeClientId,
        note_text: noteText,
        user,
      }),
    },
  )
  const raw = await parseJson<unknown>(response)
  return normalizeFieldUpdate(raw)
}

export interface GlobalSearchParams {
  q: string
  client_id?: number | null
  company_id?: number | null
  user_id?: number | null
  filter_user_id?: number | null
  date_from?: string | null
  date_to?: string | null
  activity_type?: string | null
  status?: string | null
  all_clients?: boolean
  had_appointment?: boolean
  had_quote?: boolean
  had_purchase_order?: boolean
  had_weblead?: boolean
  is_hot?: boolean
  limit?: number
}

function normalizeSearchHit(raw: unknown): SearchHit {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    doc_type: pick(record, 'doc_type'),
    client_id: asNumber(record.client_id),
    client_name: pick(record, 'client_name'),
    client_code: pick(record, 'client_code'),
    company_id: record.company_id == null ? null : asNumber(record.company_id),
    company_name: pick(record, 'company_name'),
    external_record_no: pick(record, 'external_record_no'),
    contact_id: record.contact_id == null ? null : asNumber(record.contact_id),
    contact_name: pick(record, 'contact_name') || null,
    source_table: pick(record, 'source_table'),
    source_id: record.source_id == null ? null : asNumber(record.source_id),
    note_type: pick(record, 'note_type') || null,
    title: pick(record, 'title'),
    snippet: pick(record, 'snippet'),
    event_at: pick(record, 'event_at') || null,
    created_by: pick(record, 'created_by') || null,
    workspace_path: pick(record, 'workspace_path') || null,
    focus_target: pick(record, 'focus_target') || null,
  }
}

export async function globalSearch(params: GlobalSearchParams): Promise<SearchResponse> {
  const query = new URLSearchParams()
  query.set('q', params.q)
  if (params.client_id != null) query.set('client_id', String(params.client_id))
  if (params.company_id != null) query.set('company_id', String(params.company_id))
  if (params.user_id != null) query.set('user_id', String(params.user_id))
  if (params.filter_user_id != null) query.set('user_id', String(params.filter_user_id))
  if (params.date_from) query.set('date_from', params.date_from)
  if (params.date_to) query.set('date_to', params.date_to)
  if (params.activity_type) query.set('activity_type', params.activity_type)
  if (params.status) query.set('status', params.status)
  if (params.all_clients) query.set('all_clients', 'true')
  if (params.had_appointment) query.set('had_appointment', 'true')
  if (params.had_quote) query.set('had_quote', 'true')
  if (params.had_purchase_order) query.set('had_purchase_order', 'true')
  if (params.had_weblead) query.set('had_weblead', 'true')
  if (params.is_hot) query.set('is_hot', 'true')
  if (params.limit != null) query.set('limit', String(params.limit))

  const response = await fetch(`/api/search?${query.toString()}`)
  const raw = await parseJson<Record<string, unknown>>(response)
  const companies = Array.isArray(raw.companies) ? raw.companies.map(normalizeSearchHit) : []
  const contacts = Array.isArray(raw.contacts) ? raw.contacts.map(normalizeSearchHit) : []
  const notes = Array.isArray(raw.notes) ? raw.notes.map(normalizeSearchHit) : []
  return {
    query: pick(raw, 'query'),
    mode: pick(raw, 'mode') || 'all_my_clients',
    client_ids: Array.isArray(raw.client_ids) ? raw.client_ids.map((v) => asNumber(v)) : [],
    total: asNumber(raw.total),
    companies,
    contacts,
    notes,
  }
}

export async function fetchCompanyActivities(
  recordNo: string,
  client = '',
  clientId?: number | null,
): Promise<ActivitySummary[]> {
  const query = new URLSearchParams({ client })
  if (clientId != null) query.set('client_id', String(clientId))
  const response = await fetch(
    `/api/companies/by-record/${encodeURIComponent(recordNo.trim())}/activities?${query.toString()}`,
  )
  const raw = await parseJson<{ activities?: unknown[] }>(response)
  return (raw.activities ?? []).map((item) => {
    const record = (item ?? {}) as Record<string, unknown>
    return {
      activity_id: asNumber(record.activity_id),
      client_id: asNumber(record.client_id),
      external_record_no: pick(record, 'external_record_no'),
      company_id: asNumber(record.company_id),
      relationship_id: asNumber(record.relationship_id),
      user_id: record.user_id == null ? null : asNumber(record.user_id),
      contact_id: record.contact_id == null ? null : asNumber(record.contact_id),
      activity_type: pick(record, 'activity_type'),
      activity_at: pick(record, 'activity_at'),
      outcome: pick(record, 'outcome'),
      notes: pick(record, 'notes'),
      follow_up_at: pick(record, 'follow_up_at') || null,
      assigned_user: pick(record, 'assigned_user'),
      created_by: pick(record, 'created_by'),
      created_at: pick(record, 'created_at'),
      updated_at: pick(record, 'updated_at'),
    }
  })
}

export async function createCompanyNote(params: {
  external_record_no: string
  notes: string
  client?: string
  client_id: number
  created_by?: string
  contact_id?: number | null
}): Promise<ActivitySummary> {
  const writeClientId = requireWriteClientId(params.client_id)
  const response = await fetch('/api/activities', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client: params.client || '',
      client_id: writeClientId,
      external_record_no: params.external_record_no,
      activity_type: 'Note',
      notes: params.notes,
      created_by: params.created_by ?? 'Julie Magnani',
      assigned_user: params.created_by ?? 'Julie Magnani',
      contact_id: params.contact_id ?? null,
    }),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return {
    activity_id: asNumber(raw.activity_id),
    client_id: asNumber(raw.client_id),
    external_record_no: pick(raw, 'external_record_no'),
    company_id: asNumber(raw.company_id),
    relationship_id: asNumber(raw.relationship_id),
    user_id: raw.user_id == null ? null : asNumber(raw.user_id),
    contact_id: raw.contact_id == null ? null : asNumber(raw.contact_id),
    activity_type: pick(raw, 'activity_type'),
    activity_at: pick(raw, 'activity_at'),
    outcome: pick(raw, 'outcome'),
    notes: pick(raw, 'notes'),
    follow_up_at: pick(raw, 'follow_up_at') || null,
    assigned_user: pick(raw, 'assigned_user'),
    created_by: pick(raw, 'created_by'),
    created_at: pick(raw, 'created_at'),
    updated_at: pick(raw, 'updated_at'),
  }
}

export interface CrossClientOpportunityFilters {
  target_client_id?: number | null
  signal_type?: string
  other_client_id?: number | null
  state?: string
  strength?: string
  target_status?: string
  target_activity?: string
  review_status?: string
  min_score?: number | null
  proven_buyer?: boolean
  engaged_elsewhere?: boolean
  multiple_signals?: boolean
  include_dismissed?: boolean
}

function crossClientQuery(params: CrossClientOpportunityFilters): string {
  const query = new URLSearchParams()
  if (params.target_client_id != null) query.set('target_client_id', String(params.target_client_id))
  if (params.signal_type) query.set('signal_type', params.signal_type)
  if (params.other_client_id != null) query.set('other_client_id', String(params.other_client_id))
  if (params.state) query.set('state', params.state)
  if (params.strength) query.set('strength', params.strength)
  if (params.target_status) query.set('target_status', params.target_status)
  if (params.target_activity) query.set('target_activity', params.target_activity)
  if (params.review_status) query.set('review_status', params.review_status)
  if (params.min_score != null) query.set('min_score', String(params.min_score))
  if (params.proven_buyer) query.set('proven_buyer', 'true')
  if (params.engaged_elsewhere) query.set('engaged_elsewhere', 'true')
  if (params.multiple_signals) query.set('multiple_signals', 'true')
  if (params.include_dismissed) query.set('include_dismissed', 'true')
  return query.toString()
}

export async function fetchCrossClientOpportunities(
  params: CrossClientOpportunityFilters = {},
): Promise<CrossClientOpportunityList> {
  const query = crossClientQuery(params)
  const response = await fetch(`/api/opportunities/cross-client${query ? `?${query}` : ''}`)
  return parseJson<CrossClientOpportunityList>(response)
}

export async function fetchOpportunityCount(targetClientId?: number | null): Promise<number> {
  const query = targetClientId != null ? `?target_client_id=${encodeURIComponent(String(targetClientId))}` : ''
  const response = await fetch(`/api/opportunities/cross-client/count${query}`)
  const raw = await parseJson<Record<string, unknown>>(response)
  return asNumber(raw.count)
}

export async function dismissOpportunity(params: {
  target_client_id: number
  company_id: number
  reason: string
  notes?: string
}): Promise<OpportunityActionResult> {
  const response = await fetch('/api/opportunities/cross-client/dismiss', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return parseJson<OpportunityActionResult>(response)
}

export async function addOpportunityToTarget(params: {
  target_client_id: number
  company_id: number
  status?: 'New'
  opportunity_score?: number
  source_summary?: string
}): Promise<OpportunityActionResult> {
  const response = await fetch('/api/opportunities/cross-client/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return parseJson<OpportunityActionResult>(response)
}

export async function markOpportunityReviewed(params: {
  target_client_id: number
  company_id: number
  notes?: string
}): Promise<OpportunityActionResult> {
  const response = await fetch('/api/opportunities/cross-client/review', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return parseJson<OpportunityActionResult>(response)
}

export async function fetchCrossClientBanner(
  recordNo: string,
  targetClientId?: number | null,
): Promise<CrossClientBanner | null> {
  const query =
    targetClientId != null && Number.isFinite(targetClientId) && targetClientId > 0
      ? `?target_client_id=${encodeURIComponent(String(targetClientId))}`
      : ''
  const response = await fetch(
    `/api/companies/by-record/${encodeURIComponent(recordNo.trim())}/cross-client-opportunity${query}`,
  )
  if (response.status === 404) return null
  // Banner is optional context — never block Company Workspace on banner failures.
  if (!response.ok) return null
  const raw = await parseJson<Record<string, unknown>>(response)
  const applicable = Boolean(raw.applicable)
  if (!applicable) return null
  const evidenceRaw = Array.isArray(raw.evidence) ? raw.evidence : []
  return {
    applicable: true,
    target_client_name: pick(raw, 'target_client_name'),
    target_client_activity: pick(raw, 'target_client_activity') || undefined,
    target_client_status: pick(raw, 'target_client_status') || undefined,
    opportunity_score: raw.opportunity_score == null ? undefined : asNumber(raw.opportunity_score),
    score_label: pick(raw, 'score_label') || undefined,
    evidence: evidenceRaw.map((item) => {
      const record = (item ?? {}) as Record<string, unknown>
      return {
        client_name: pick(record, 'client_name'),
        milestone_type: pick(record, 'milestone_type'),
        milestone_date: pick(record, 'milestone_date'),
      }
    }),
  }
}

export interface WorkQueueFilters {
  client_id?: number | null
  type?: string
  status?: string
  due?: string
  priority?: string
  hot?: boolean
  weblead?: boolean
  cross_client?: boolean
  overdue?: boolean
  assigned_user_id?: number | null
  q?: string
  ai_alignment?: string
  ai_recommendation?: string
  ai_fit?: string
  ai_engagement?: string
}

function workQueueQuery(params: WorkQueueFilters): string {
  const query = new URLSearchParams()
  if (params.client_id != null) query.set('client_id', String(params.client_id))
  if (params.type) query.set('type', params.type)
  if (params.status) query.set('status', params.status)
  if (params.due) query.set('due', params.due)
  if (params.priority) query.set('priority', params.priority)
  if (params.hot) query.set('hot', 'true')
  if (params.weblead) query.set('weblead', 'true')
  if (params.cross_client) query.set('cross_client', 'true')
  if (params.overdue) query.set('overdue', 'true')
  if (params.assigned_user_id != null) query.set('assigned_user_id', String(params.assigned_user_id))
  if (params.q) query.set('q', params.q)
  if (params.ai_alignment) query.set('ai_alignment', params.ai_alignment)
  if (params.ai_recommendation) query.set('ai_recommendation', params.ai_recommendation)
  if (params.ai_fit) query.set('ai_fit', params.ai_fit)
  if (params.ai_engagement) query.set('ai_engagement', params.ai_engagement)
  return query.toString()
}

function normalizeWorkQueueInsight(raw: unknown): WorkQueueRow['northstar_insight'] {
  const record = (raw ?? null) as Record<string, unknown> | null
  if (!record) return null
  return {
    evaluated: asBoolean(record.evaluated),
    fit: pick(record, 'fit') || 'Not Evaluated',
    engagement: pick(record, 'engagement') || 'Not Evaluated',
    recommendation: pick(record, 'recommendation') || 'Not Evaluated',
    why: pick(record, 'why'),
    evidence_used: Array.isArray(record.evidence_used)
      ? record.evidence_used.map(String)
      : [],
    still_need_to_know: Array.isArray(record.still_need_to_know)
      ? record.still_need_to_know.map(String)
      : [],
    signals: Array.isArray(record.signals) ? record.signals.map(String) : [],
    alignment: pick(record, 'alignment') || 'Insufficient AI Data',
    review_reason: pick(record, 'review_reason'),
    review_flag: pick(record, 'review_flag'),
    advisory_note: pick(record, 'advisory_note'),
  }
}

function normalizeWorkQueueRow(raw: unknown): WorkQueueRow {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    queue_item_id: pick(record, 'queue_item_id'),
    work_type: pick(record, 'work_type'),
    work_priority: asNumber(record.work_priority),
    priority_label: pick(record, 'priority_label') || 'Low',
    client_id: asNumber(record.client_id),
    client_code: pick(record, 'client_code'),
    client_name: pick(record, 'client_name'),
    company_id: asNumber(record.company_id),
    relationship_id: asNumber(record.relationship_id),
    external_record_no: pick(record, 'external_record_no'),
    company_name: pick(record, 'company_name'),
    city: pick(record, 'city'),
    state: pick(record, 'state'),
    contact_id: record.contact_id == null || record.contact_id === '' ? null : asNumber(record.contact_id),
    contact_name: pick(record, 'contact_name'),
    status: pick(record, 'status'),
    due_date: pick(record, 'due_date') || null,
    due_time: pick(record, 'due_time'),
    is_overdue: asBoolean(record.is_overdue),
    last_activity_at: pick(record, 'last_activity_at') || null,
    last_activity_summary: pick(record, 'last_activity_summary'),
    next_action: pick(record, 'next_action'),
    why_in_queue: pick(record, 'why_in_queue'),
    is_hot: asBoolean(record.is_hot),
    is_weblead: asBoolean(record.is_weblead),
    weblead_age_label: pick(record, 'weblead_age_label') || null,
    is_new_weblead: asBoolean(record.is_new_weblead),
    is_cross_client: asBoolean(record.is_cross_client),
    opportunity_score: record.opportunity_score == null ? null : asNumber(record.opportunity_score),
    source_client_summary: pick(record, 'source_client_summary'),
    assigned_user: pick(record, 'assigned_user'),
    assigned_user_id:
      record.assigned_user_id == null || record.assigned_user_id === ''
        ? null
        : asNumber(record.assigned_user_id),
    source: pick(record, 'source'),
    source_id: record.source_id == null || record.source_id === '' ? null : asNumber(record.source_id),
    completion_status: pick(record, 'completion_status') || 'open',
    northstar_insight: normalizeWorkQueueInsight(record.northstar_insight),
  }
}

export async function fetchWorkQueue(params: WorkQueueFilters = {}): Promise<WorkQueueListResponse> {
  const query = workQueueQuery(params)
  const response = await fetch(`/api/work-queue${query ? `?${query}` : ''}`)
  const raw = await parseJson<Record<string, unknown>>(response)
  const summaryRaw = (raw.summary ?? {}) as Record<string, unknown>
  const summary: WorkQueueSummaryV2 = {
    calls_due: asNumber(summaryRaw.calls_due),
    follow_ups_due: asNumber(summaryRaw.follow_ups_due),
    appointments: asNumber(summaryRaw.appointments),
    hot: asNumber(summaryRaw.hot),
    webleads: asNumber(summaryRaw.webleads),
    new_assignments: asNumber(summaryRaw.new_assignments),
    needs_next_action: asNumber(summaryRaw.needs_next_action),
    cross_client_opportunities: asNumber(summaryRaw.cross_client_opportunities),
    overdue: asNumber(summaryRaw.overdue),
  }
  const items = Array.isArray(raw.items) ? raw.items.map(normalizeWorkQueueRow) : []
  const insightRaw = (raw.northstar_insight_summary ?? {}) as Record<string, unknown>
  return {
    user_id: asNumber(raw.user_id),
    mode: pick(raw, 'mode') || 'all_my_clients',
    client_ids: Array.isArray(raw.client_ids) ? raw.client_ids.map((v) => asNumber(v)) : [],
    count: asNumber(raw.count),
    summary,
    items,
    northstar_insight_summary: {
      aligned: asNumber(insightRaw.aligned),
      review: asNumber(insightRaw.review),
      insufficient_ai_data: asNumber(insightRaw.insufficient_ai_data),
      evaluated: asNumber(insightRaw.evaluated),
      not_evaluated: asNumber(insightRaw.not_evaluated),
    },
  }
}

function normalizeDashboardFollowUp(record: Record<string, unknown>): DashboardFollowUpItem {
  return {
    client_id: asNumber(record.client_id),
    company_id: asNumber(record.company_id),
    contact_id:
      record.contact_id == null || record.contact_id === '' ? null : asNumber(record.contact_id),
    contact_name: pick(record, 'contact_name'),
    company_name: pick(record, 'company_name'),
    external_record_no: pick(record, 'external_record_no'),
    due_date: pick(record, 'due_date'),
    due_time: pick(record, 'due_time'),
    assigned_user: pick(record, 'assigned_user'),
    assigned_user_id:
      record.assigned_user_id == null || record.assigned_user_id === ''
        ? null
        : asNumber(record.assigned_user_id),
    status: pick(record, 'status'),
    source: pick(record, 'source'),
    source_id: record.source_id == null || record.source_id === '' ? null : asNumber(record.source_id),
    bucket: pick(record, 'bucket') || 'upcoming',
    next_action: pick(record, 'next_action'),
  }
}

export async function fetchDashboardFollowUps(
  clientId: number,
): Promise<DashboardFollowUpsResponse> {
  const response = await fetch(
    `/api/dashboard/follow-ups?client_id=${encodeURIComponent(String(clientId))}`,
  )
  const raw = await parseJson<Record<string, unknown>>(response)
  const overdue = Array.isArray(raw.overdue)
    ? raw.overdue.map((item) => normalizeDashboardFollowUp((item ?? {}) as Record<string, unknown>))
    : []
  const dueToday = Array.isArray(raw.due_today)
    ? raw.due_today.map((item) => normalizeDashboardFollowUp((item ?? {}) as Record<string, unknown>))
    : []
  const upcoming = Array.isArray(raw.upcoming)
    ? raw.upcoming.map((item) => normalizeDashboardFollowUp((item ?? {}) as Record<string, unknown>))
    : []
  return {
    client_id: asNumber(raw.client_id) || clientId,
    overdue,
    due_today: dueToday,
    upcoming,
    overdue_count: asNumber(raw.overdue_count) || overdue.length,
    due_today_count: asNumber(raw.due_today_count) || dueToday.length,
    upcoming_count: asNumber(raw.upcoming_count) || upcoming.length,
  }
}

export type AssignedClientOption = {
  client_id: number
  client_name: string
  client_code: string
}

/** Clients assigned to the logged-in NorthStar user (from DB assignments). */
export async function fetchAssignedClients(): Promise<AssignedClientOption[]> {
  const response = await fetch('/api/users/default')
  const raw = await parseJson<Record<string, unknown>>(response)
  const items = Array.isArray(raw.clients) ? raw.clients : []
  return items
    .map((item) => {
      const record = (item ?? {}) as Record<string, unknown>
      return {
        client_id: asNumber(record.client_id),
        client_name: pick(record, 'client_name', 'name'),
        client_code: pick(record, 'client_code', 'code'),
      }
    })
    .filter((c) => c.client_id > 0 && Boolean(c.client_name || c.client_code))
}

export async function fetchWorkQueueClients(): Promise<AssignedClientOption[]> {
  // Prefer user assignments endpoint; fall back to work-queue clients list.
  try {
    const assigned = await fetchAssignedClients()
    if (assigned.length > 0) return assigned
  } catch {
    /* fall through */
  }
  const response = await fetch('/api/work-queue/clients')
  const raw = await parseJson<Record<string, unknown>>(response)
  const items = Array.isArray(raw.clients) ? raw.clients : []
  return items.map((item) => {
    const record = (item ?? {}) as Record<string, unknown>
    return {
      client_id: asNumber(record.client_id),
      client_name: pick(record, 'client_name'),
      client_code: pick(record, 'client_code'),
    }
  })
}

export async function completeWorkQueueItem(params: {
  client_id: number
  source: string
  source_id?: number | null
  company_id?: number | null
}): Promise<{ ok: boolean; message: string }> {
  const response = await fetch('/api/work-queue/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return parseJson(response)
}

export async function fetchClientReps(clientId: number): Promise<ContactRepOption[]> {
  const response = await fetch(`/api/clients/${encodeURIComponent(String(clientId))}/reps`)
  const raw = await parseJson<Record<string, unknown>>(response)
  const items = Array.isArray(raw.reps) ? raw.reps : []
  return items
    .map((item) => {
      const record = (item ?? {}) as Record<string, unknown>
      return {
        user_id: asNumber(record.user_id),
        full_name: pick(record, 'full_name'),
      }
    })
    .filter((rep) => rep.user_id > 0 && Boolean(rep.full_name))
}

export async function completeFollowUpTask(params: {
  client_id: number
  source: string
  source_id: number
  company_id: number
  contact_id?: number | null
  notes?: string
  created_by?: string
}): Promise<{
  ok: boolean
  message: string
  activity_id: number | null
  source: string
  source_id: number | null
}> {
  const writeClientId = requireWriteClientId(params.client_id)
  const response = await fetch('/api/follow-up-tasks/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...params, client_id: writeClientId }),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return {
    ok: Boolean(raw.ok),
    message: pick(raw, 'message'),
    activity_id: raw.activity_id == null ? null : asNumber(raw.activity_id),
    source: pick(raw, 'source'),
    source_id: raw.source_id == null || raw.source_id === '' ? null : asNumber(raw.source_id),
  }
}

export async function rescheduleFollowUpTask(params: {
  client_id: number
  source: string
  source_id: number
  company_id: number
  contact_id?: number | null
  follow_up_date: string
  follow_up_time: string
  assigned_user_id?: number | null
  next_action?: string
  notes?: string
  created_by?: string
}): Promise<{
  ok: boolean
  message: string
  follow_up_activity_id: number | null
  source: string
  source_id: number | null
}> {
  const response = await fetch('/api/follow-up-tasks/reschedule', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return {
    ok: Boolean(raw.ok),
    message: pick(raw, 'message'),
    follow_up_activity_id:
      raw.follow_up_activity_id == null ? null : asNumber(raw.follow_up_activity_id),
    source: pick(raw, 'source'),
    source_id: raw.source_id == null || raw.source_id === '' ? null : asNumber(raw.source_id),
  }
}

export async function logWorkQueueCall(params: {
  client_id: number
  external_record_no: string
  contact_id?: number | null
  outcome?: string
  notes?: string
  status?: string | null
  next_action?: string
  follow_up_date?: string | null
  follow_up_time?: string
  queue_source?: string
  queue_source_id?: number | null
  complete_current?: boolean
  created_by?: string
  appointment?: AppointmentDetailsPayload | null
}): Promise<{
  ok: boolean
  message: string
  activity_id: number | null
  follow_up_activity_id: number | null
  completed_queue_item: boolean
  appointment_id: number | null
}> {
  const writeClientId = requireWriteClientId(params.client_id)
  const response = await fetch('/api/work-queue/log-call', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...params, client_id: writeClientId }),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return {
    ok: Boolean(raw.ok),
    message: pick(raw, 'message'),
    activity_id: raw.activity_id == null ? null : asNumber(raw.activity_id),
    follow_up_activity_id:
      raw.follow_up_activity_id == null ? null : asNumber(raw.follow_up_activity_id),
    completed_queue_item: asBoolean(raw.completed_queue_item),
    appointment_id: raw.appointment_id == null ? null : asNumber(raw.appointment_id),
  }
}

export type PriorityProspectItem = {
  client_id: number
  client_name: string
  company_id: number
  relationship_id: number
  external_record_no: string
  company_name: string
  status: string
  primary_contact: string
  next_action: string
  follow_up_date: string | null
  is_hot: boolean
  priority_bucket: number
  priority_reason: string
  sort_key: string
}

export type OutreachLogResult = {
  ok: boolean
  message: string
  activity_id: number | null
  follow_up_activity_id: number | null
  client_id: number
  external_record_no: string
  status_updated: boolean
  applied_status: string
  open_email_compose: boolean
  open_appointment_workflow: boolean
  next_external_record_no: string | null
}

export async function fetchPriorityProspects(
  clientId: number,
  limit = 50,
): Promise<PriorityProspectItem[]> {
  const query = new URLSearchParams({ limit: String(limit) })
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/priority-prospects?${query}`,
  )
  const raw = await parseJson<Record<string, unknown>>(response)
  const items = Array.isArray(raw.items) ? raw.items : []
  return items.map((row) => {
    const r = row as Record<string, unknown>
    return {
      client_id: asNumber(r.client_id),
      client_name: pick(r, 'client_name'),
      company_id: asNumber(r.company_id),
      relationship_id: asNumber(r.relationship_id),
      external_record_no: pick(r, 'external_record_no'),
      company_name: pick(r, 'company_name'),
      status: pick(r, 'status'),
      primary_contact: pick(r, 'primary_contact'),
      next_action: pick(r, 'next_action'),
      follow_up_date: pick(r, 'follow_up_date') || null,
      is_hot: asBoolean(r.is_hot),
      priority_bucket: asNumber(r.priority_bucket),
      priority_reason: pick(r, 'priority_reason'),
      sort_key: pick(r, 'sort_key'),
    }
  })
}

export async function logOutreach(params: {
  client_id: number
  external_record_no: string
  contact_id?: number | null
  outreach_type?: string
  outcome?: string
  notes?: string
  next_action?: string
  follow_up_date?: string | null
  return_next?: boolean
  created_by?: string
}): Promise<OutreachLogResult> {
  const writeClientId = requireWriteClientId(params.client_id)
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(writeClientId))}/outreach`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: writeClientId,
        external_record_no: params.external_record_no,
        contact_id: params.contact_id ?? null,
        outreach_type: params.outreach_type || 'Call',
        outcome: params.outcome || '',
        notes: params.notes || '',
        next_action: params.next_action || '',
        follow_up_date: params.follow_up_date || null,
        return_next: Boolean(params.return_next),
        created_by: params.created_by || 'Julie Magnani',
      }),
    },
  )
  const raw = await parseJson<Record<string, unknown>>(response)
  return {
    ok: Boolean(raw.ok),
    message: pick(raw, 'message'),
    activity_id: raw.activity_id == null ? null : asNumber(raw.activity_id),
    follow_up_activity_id:
      raw.follow_up_activity_id == null ? null : asNumber(raw.follow_up_activity_id),
    client_id: asNumber(raw.client_id),
    external_record_no: pick(raw, 'external_record_no'),
    status_updated: asBoolean(raw.status_updated),
    applied_status: pick(raw, 'applied_status'),
    open_email_compose: asBoolean(raw.open_email_compose),
    open_appointment_workflow: asBoolean(raw.open_appointment_workflow),
    next_external_record_no:
      raw.next_external_record_no == null || raw.next_external_record_no === ''
        ? null
        : String(raw.next_external_record_no),
  }
}

export async function createCompanyActivity(params: {
  client?: string
  client_id: number
  external_record_no: string
  activity_type: string
  activity_at?: string
  outcome?: string
  notes?: string
  follow_up_at?: string | null
  contact_id?: number | null
  assigned_user?: string
  created_by?: string
}): Promise<ActivitySummary> {
  const writeClientId = requireWriteClientId(params.client_id)
  const response = await fetch('/api/activities', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...params,
      client: params.client || '',
      client_id: writeClientId,
    }),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return {
    activity_id: asNumber(raw.activity_id),
    client_id: asNumber(raw.client_id),
    external_record_no: pick(raw, 'external_record_no'),
    company_id: asNumber(raw.company_id),
    relationship_id: asNumber(raw.relationship_id),
    user_id: raw.user_id == null ? null : asNumber(raw.user_id),
    contact_id: raw.contact_id == null ? null : asNumber(raw.contact_id),
    activity_type: pick(raw, 'activity_type'),
    activity_at: pick(raw, 'activity_at'),
    outcome: pick(raw, 'outcome'),
    notes: pick(raw, 'notes'),
    follow_up_at: pick(raw, 'follow_up_at') || null,
    assigned_user: pick(raw, 'assigned_user'),
    created_by: pick(raw, 'created_by'),
    created_at: pick(raw, 'created_at'),
    updated_at: pick(raw, 'updated_at'),
  }
}

export async function fetchClientActivities(
  clientId: number,
  params?: {
    activity_type?: string
    assigned_user?: string
    user_id?: number | null
    company_id?: number | null
    contact_id?: number | null
    company?: string
    contact?: string
    date_from?: string
    date_to?: string
    q?: string
    limit?: number
    offset?: number
  },
): Promise<ActivityTimelineResponse> {
  const query = new URLSearchParams()
  if (params?.activity_type) query.set('activity_type', params.activity_type)
  if (params?.assigned_user) query.set('assigned_user', params.assigned_user)
  if (params?.user_id != null && params.user_id > 0) query.set('user_id', String(params.user_id))
  if (params?.company_id != null && params.company_id > 0) {
    query.set('company_id', String(params.company_id))
  }
  if (params?.contact_id != null && params.contact_id > 0) {
    query.set('contact_id', String(params.contact_id))
  }
  if (params?.company) query.set('company', params.company)
  if (params?.contact) query.set('contact', params.contact)
  if (params?.date_from) query.set('date_from', params.date_from)
  if (params?.date_to) query.set('date_to', params.date_to)
  if (params?.q) query.set('q', params.q)
  if (params?.limit != null) query.set('limit', String(params.limit))
  if (params?.offset != null) query.set('offset', String(params.offset))
  const qs = query.toString()
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/activities${qs ? `?${qs}` : ''}`,
  )
  const raw = (await parseJson(response)) as Record<string, unknown>
  const items = Array.isArray(raw.items) ? raw.items : []
  return {
    client_id: asNumber(raw.client_id, clientId),
    count: asNumber(raw.count, items.length),
    total: asNumber(raw.total, items.length),
    limit: asNumber(raw.limit, 50),
    offset: asNumber(raw.offset),
    activity_types: Array.isArray(raw.activity_types)
      ? raw.activity_types.map((v) => String(v))
      : [],
    users: Array.isArray(raw.users) ? raw.users.map((v) => String(v)) : [],
    items: items.map((item) => {
      const row = (item ?? {}) as Record<string, unknown>
      return {
        item_key: pick(row, 'item_key'),
        source: pick(row, 'source') || 'activity',
        activity_id: row.activity_id == null || row.activity_id === '' ? null : asNumber(row.activity_id),
        client_id: asNumber(row.client_id, clientId),
        company_id: asNumber(row.company_id),
        relationship_id: asNumber(row.relationship_id),
        external_record_no: pick(row, 'external_record_no'),
        company_name: pick(row, 'company_name'),
        contact_id: row.contact_id == null || row.contact_id === '' ? null : asNumber(row.contact_id),
        contact_name: pick(row, 'contact_name'),
        activity_type: pick(row, 'activity_type'),
        activity_at: pick(row, 'activity_at'),
        user_name: pick(row, 'user_name'),
        user_id: row.user_id == null || row.user_id === '' ? null : asNumber(row.user_id),
        outcome: pick(row, 'outcome'),
        notes: pick(row, 'notes'),
        status: pick(row, 'status'),
      }
    }),
  }
}

export function companyWorkspaceHref(recordNo: string, clientId?: number | null): string {
  const base = `/companies/${encodeURIComponent(recordNo)}`
  if (clientId == null) return base
  return `${base}?client_id=${encodeURIComponent(String(clientId))}`
}

export async function askNorthStar(params: {
  question: string
  scope?: AskScope
  active_client_id?: number | null
}): Promise<AskNorthStarResponse> {
  const response = await fetch('/api/ask-northstar', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      question: params.question,
      scope: params.scope || 'all',
      active_client_id: params.active_client_id ?? null,
    }),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return normalizeAskResponse(raw)
}

export async function fetchAskHistory(
  limit = 12,
  opts?: { scope?: AskScope | string | null; active_client_id?: number | null },
): Promise<AskHistoryResponse> {
  const query = new URLSearchParams()
  query.set('limit', String(limit))
  if (opts?.scope) query.set('scope', String(opts.scope))
  if (
    opts?.scope === 'active_client' &&
    opts.active_client_id != null &&
    opts.active_client_id > 0
  ) {
    query.set('active_client_id', String(opts.active_client_id))
  }
  const response = await fetch(`/api/ask-northstar/history?${query.toString()}`)
  const raw = await parseJson<Record<string, unknown>>(response)
  const itemsRaw = Array.isArray(raw.items) ? raw.items : []
  return {
    count: asNumber(raw.count),
    filter_scope: pick(raw, 'filter_scope'),
    filter_client_id:
      raw.filter_client_id == null ? null : asNumber(raw.filter_client_id),
    filter_client_name: pick(raw, 'filter_client_name'),
    items: itemsRaw.map((item) => {
      const row = item as Record<string, unknown>
      return {
        id: asNumber(row.id),
        question: pick(row, 'question'),
        scope: pick(row, 'scope'),
        active_client_id:
          row.active_client_id == null ? null : asNumber(row.active_client_id),
        active_client_name: pick(row, 'active_client_name'),
        intent: pick(row, 'intent'),
        answer_summary: pick(row, 'answer_summary'),
        created_at: pick(row, 'created_at'),
      }
    }),
  }
}

function normalizeAskResponse(raw: Record<string, unknown>): AskNorthStarResponse {
  const companies = Array.isArray(raw.companies) ? raw.companies : []
  const contacts = Array.isArray(raw.contacts) ? raw.contacts : []
  const notes = Array.isArray(raw.notes) ? raw.notes : []
  const milestones = Array.isArray(raw.milestones) ? raw.milestones : []
  const sources = Array.isArray(raw.sources) ? raw.sources : []
  const links = Array.isArray(raw.recommended_links) ? raw.recommended_links : []
  const research = Array.isArray(raw.research_options) ? raw.research_options : []
  const sections = Array.isArray(raw.sections) ? raw.sections : []
  return {
    question: pick(raw, 'question'),
    intent: pick(raw, 'intent'),
    summary: pick(raw, 'summary'),
    scope: pick(raw, 'scope') || 'all',
    scope_label: pick(raw, 'scope_label'),
    active_client_id:
      raw.active_client_id == null ? null : asNumber(raw.active_client_id),
    active_client_name: pick(raw, 'active_client_name'),
    no_data: asBoolean(raw.no_data),
    read_only: raw.read_only == null ? true : asBoolean(raw.read_only),
    result_count: asNumber(raw.result_count),
    sections: sections.map((item) => {
      const row = item as Record<string, unknown>
      return {
        id: pick(row, 'id'),
        title: pick(row, 'title'),
        body: pick(row, 'body'),
        items: Array.isArray(row.items) ? (row.items as Record<string, unknown>[]) : [],
      }
    }),
    companies: companies.map((item) => {
      const row = item as Record<string, unknown>
      return {
        company_id: asNumber(row.company_id),
        company_name: pick(row, 'company_name'),
        external_record_no: pick(row, 'external_record_no'),
        client_id: row.client_id == null ? null : asNumber(row.client_id),
        client_name: pick(row, 'client_name'),
        status: pick(row, 'status'),
        city: pick(row, 'city'),
        state: pick(row, 'state'),
        badges: Array.isArray(row.badges) ? row.badges.map(String) : [],
        why: pick(row, 'why'),
        opportunity_score:
          row.opportunity_score == null ? null : asNumber(row.opportunity_score),
        workspace_path: pick(row, 'workspace_path'),
        rank: row.rank == null ? null : asNumber(row.rank),
        next_action: pick(row, 'next_action'),
        work_priority: row.work_priority == null ? null : asNumber(row.work_priority),
        work_type: pick(row, 'work_type'),
        northstar_recommendation: pick(row, 'northstar_recommendation'),
        northstar_fit: pick(row, 'northstar_fit'),
        northstar_engagement: pick(row, 'northstar_engagement'),
        northstar_alignment: pick(row, 'northstar_alignment'),
        northstar_recommendation_why: pick(row, 'northstar_recommendation_why'),
        northstar_insight_note: pick(row, 'northstar_insight_note'),
      }
    }),
    contacts: contacts.map((item) => {
      const row = item as Record<string, unknown>
      return {
        contact_id: row.contact_id == null ? null : asNumber(row.contact_id),
        name: pick(row, 'name'),
        title: pick(row, 'title'),
        phone: pick(row, 'phone'),
        email: pick(row, 'email'),
        client_id: row.client_id == null ? null : asNumber(row.client_id),
        client_name: pick(row, 'client_name'),
        source: pick(row, 'source') || 'NorthStar CRM',
      }
    }),
    notes: notes.map((item) => {
      const row = item as Record<string, unknown>
      return {
        company_id: row.company_id == null ? null : asNumber(row.company_id),
        company_name: pick(row, 'company_name'),
        external_record_no: pick(row, 'external_record_no'),
        client_id: row.client_id == null ? null : asNumber(row.client_id),
        client_name: pick(row, 'client_name'),
        excerpt: pick(row, 'excerpt'),
        event_at: pick(row, 'event_at'),
        source: pick(row, 'source') || 'NorthStar CRM',
        doc_type: pick(row, 'doc_type') || 'note',
        workspace_path: pick(row, 'workspace_path'),
      }
    }),
    milestones: milestones.map((item) => {
      const row = item as Record<string, unknown>
      return {
        milestone_type: pick(row, 'milestone_type'),
        label: pick(row, 'label'),
        milestone_date: pick(row, 'milestone_date'),
        client_id: row.client_id == null ? null : asNumber(row.client_id),
        client_name: pick(row, 'client_name'),
        source: pick(row, 'source') || 'NorthStar CRM',
      }
    }),
    sources: sources.map((item) => {
      const row = item as Record<string, unknown>
      return { label: pick(row, 'label'), detail: pick(row, 'detail') }
    }),
    recommended_links: links.map((item) => {
      const row = item as Record<string, unknown>
      return { label: pick(row, 'label'), href: pick(row, 'href') }
    }),
    research_options: research.map((item) => {
      const row = item as Record<string, unknown>
      return {
        id: pick(row, 'id'),
        label: pick(row, 'label'),
        status: pick(row, 'status') || 'Coming Next',
        enabled: asBoolean(row.enabled),
        description: pick(row, 'description'),
      }
    }),
    research_available: asBoolean(raw.research_available),
    history_id: raw.history_id == null ? null : asNumber(raw.history_id),
  }
}

function asResearchFinding(row: Record<string, unknown>): import('../types/carmeco').ResearchFindingView {
  return {
    id: row.id == null ? null : asNumber(row.id),
    finding_type: pick(row, 'finding_type'),
    field_key: pick(row, 'field_key'),
    value: pick(row, 'value'),
    source_name: pick(row, 'source_name'),
    source_url: pick(row, 'source_url'),
    researched_at: pick(row, 'researched_at'),
    confidence: pick(row, 'confidence') || 'medium',
    evidence_level: pick(row, 'evidence_level') || 'verified',
    page_title: pick(row, 'page_title'),
    is_public_contact: asBoolean(row.is_public_contact),
    contact_name: pick(row, 'contact_name'),
    contact_title: pick(row, 'contact_title'),
    provider_id: pick(row, 'provider_id') || 'public_web',
    why_relevant: pick(row, 'why_relevant'),
    matched_persona: pick(row, 'matched_persona'),
    crm_match_status: pick(row, 'crm_match_status'),
    crm_match_label: pick(row, 'crm_match_label'),
    crm_matched_contact_name: pick(row, 'crm_matched_contact_name'),
    contact_email: pick(row, 'contact_email'),
    contact_phone: pick(row, 'contact_phone'),
    linkedin_url: pick(row, 'linkedin_url'),
    source_labels: Array.isArray(row.source_labels)
      ? row.source_labels.map(String)
      : [],
    linkedin_derived: asBoolean(row.linkedin_derived),
    relevance_tier: pick(row, 'relevance_tier'),
    crm_matched_contact_id:
      row.crm_matched_contact_id == null ? null : asNumber(row.crm_matched_contact_id),
    crm_match_reasons: Array.isArray(row.crm_match_reasons)
      ? row.crm_match_reasons.map(String)
      : [],
  }
}

function normalizeResearchResponse(raw: Record<string, unknown>): import('../types/carmeco').ResearchCompanyResponse {
  const knownRaw = (raw.northstar_known || null) as Record<string, unknown> | null
  const fitRaw = (raw.fit || null) as Record<string, unknown> | null
  const list = (key: string) =>
    Array.isArray(raw[key]) ? (raw[key] as Record<string, unknown>[]) : []

  return {
    research_run_id: raw.research_run_id == null ? null : asNumber(raw.research_run_id),
    company_id: asNumber(raw.company_id),
    company_name: pick(raw, 'company_name'),
    working_for_client_id:
      raw.working_for_client_id == null ? null : asNumber(raw.working_for_client_id),
    working_for_client_name: pick(raw, 'working_for_client_name'),
    working_for_record_no: pick(raw, 'working_for_record_no'),
    working_for_status: pick(raw, 'working_for_status'),
    campaign_id: raw.campaign_id == null ? null : asNumber(raw.campaign_id),
    campaign_name: pick(raw, 'campaign_name'),
    campaign_choices: list('campaign_choices').map((row) => ({
      campaign_id: asNumber(row.campaign_id),
      campaign_name: pick(row, 'campaign_name'),
      is_default: asBoolean(row.is_default),
    })),
    needs_working_for: asBoolean(raw.needs_working_for),
    working_for_choices: list('working_for_choices'),
    summary: pick(raw, 'summary'),
    decision_summary: (() => {
      const d = (raw.decision_summary || null) as Record<string, unknown> | null
      if (!d) return null
      return {
        client_name: pick(d, 'client_name'),
        campaign_name: pick(d, 'campaign_name'),
        fit_result: pick(d, 'fit_result'),
        why: pick(d, 'why'),
        still_need: pick(d, 'still_need'),
      }
    })(),
    engagement: (() => {
      const e = (raw.engagement || null) as Record<string, unknown> | null
      if (!e) return null
      return {
        level: pick(e, 'level'),
        label: pick(e, 'label'),
        signals: Array.isArray(e.signals) ? e.signals.map(String) : [],
        attributed_to: pick(e, 'attributed_to'),
        why: pick(e, 'why'),
        note: pick(e, 'note'),
        cross_client_signals: Array.isArray(e.cross_client_signals)
          ? (e.cross_client_signals as Record<string, unknown>[]).map((row) => ({
              client_name: pick(row, 'client_name'),
              signals: Array.isArray(row.signals) ? row.signals.map(String) : [],
              attribution: pick(row, 'attribution'),
            }))
          : [],
      }
    })(),
    recommendation: (() => {
      const r = (raw.recommendation || null) as Record<string, unknown> | null
      if (!r) return null
      return {
        action: pick(r, 'action'),
        why: pick(r, 'why'),
        evidence_used: Array.isArray(r.evidence_used) ? r.evidence_used.map(String) : [],
        still_need_to_know: Array.isArray(r.still_need_to_know)
          ? r.still_need_to_know.map(String)
          : [],
        fit_context: pick(r, 'fit_context'),
        engagement_context: pick(r, 'engagement_context'),
        confidence: pick(r, 'confidence'),
        advisory_note: pick(r, 'advisory_note'),
      }
    })(),
    last_researched_at: pick(raw, 'last_researched_at'),
    initiated_by: pick(raw, 'initiated_by'),
    northstar_known: knownRaw
      ? {
          company_name: pick(knownRaw, 'company_name'),
          master_record_no: pick(knownRaw, 'master_record_no'),
          website: pick(knownRaw, 'website'),
          city: pick(knownRaw, 'city'),
          state: pick(knownRaw, 'state'),
          address: pick(knownRaw, 'address'),
          working_for_client_id:
            knownRaw.working_for_client_id == null
              ? null
              : asNumber(knownRaw.working_for_client_id),
          working_for_client_name: pick(knownRaw, 'working_for_client_name'),
          working_for_record_no: pick(knownRaw, 'working_for_record_no'),
          working_for_status: pick(knownRaw, 'working_for_status'),
          has_relationship: asBoolean(knownRaw.has_relationship),
          contacts: Array.isArray(knownRaw.contacts)
            ? (knownRaw.contacts as Record<string, unknown>[])
            : [],
          contacts_total: asNumber(knownRaw.contacts_total),
          milestones: Array.isArray(knownRaw.milestones)
            ? (knownRaw.milestones as Record<string, unknown>[])
            : [],
          notes: Array.isArray(knownRaw.notes)
            ? (knownRaw.notes as Record<string, unknown>[])
            : [],
          activities: Array.isArray(knownRaw.activities)
            ? (knownRaw.activities as Record<string, unknown>[])
            : [],
          cross_client: Array.isArray(knownRaw.cross_client)
            ? (knownRaw.cross_client as Record<string, unknown>[])
            : [],
          previous_research: Array.isArray(knownRaw.previous_research)
            ? (knownRaw.previous_research as Record<string, unknown>[])
            : [],
          opportunity_score:
            knownRaw.opportunity_score == null
              ? null
              : asNumber(knownRaw.opportunity_score),
        }
      : null,
    verified_matches: list('verified_matches').map((row) => ({
      field_key: pick(row, 'field_key'),
      label: pick(row, 'label'),
      northstar_value: pick(row, 'northstar_value'),
      research_value: pick(row, 'research_value'),
      source_name: pick(row, 'source_name'),
      source_url: pick(row, 'source_url'),
      match: asBoolean(row.match),
      proposed_update_id:
        row.proposed_update_id == null ? null : asNumber(row.proposed_update_id),
    })),
    possible_changes: list('possible_changes').map((row) => ({
      field_key: pick(row, 'field_key'),
      label: pick(row, 'label'),
      northstar_value: pick(row, 'northstar_value'),
      research_value: pick(row, 'research_value'),
      source_name: pick(row, 'source_name'),
      source_url: pick(row, 'source_url'),
      match: asBoolean(row.match),
      proposed_update_id:
        row.proposed_update_id == null ? null : asNumber(row.proposed_update_id),
    })),
    overview: list('overview').map(asResearchFinding),
    locations: list('locations').map(asResearchFinding),
    capabilities: list('capabilities').map(asResearchFinding),
    products: list('products').map(asResearchFinding),
    materials: list('materials').map(asResearchFinding),
    industries: list('industries').map(asResearchFinding),
    recent_developments: list('recent_developments').map(asResearchFinding),
    missing_information: Array.isArray(raw.missing_information)
      ? raw.missing_information.map(String)
      : [],
    northstar_contacts: list('northstar_contacts'),
    northstar_contacts_total: asNumber(raw.northstar_contacts_total),
    public_contacts: list('public_contacts').map(asResearchFinding),
    people_discovery:
      raw.people_discovery && typeof raw.people_discovery === 'object'
        ? (raw.people_discovery as Record<string, unknown>)
        : {},
    cross_client_experience: list('cross_client_experience'),
    fit: fitRaw
      ? {
          client_id: asNumber(fitRaw.client_id),
          client_name: pick(fitRaw, 'client_name'),
          campaign_id:
            fitRaw.campaign_id == null ? null : asNumber(fitRaw.campaign_id),
          campaign_name: pick(fitRaw, 'campaign_name'),
          fit_result: pick(fitRaw, 'fit_result'),
          why: pick(fitRaw, 'why'),
          supporting_evidence: Array.isArray(fitRaw.supporting_evidence)
            ? fitRaw.supporting_evidence.map(String)
            : [],
          evidence_chains: Array.isArray(fitRaw.evidence_chains)
            ? (fitRaw.evidence_chains as Array<{ fact?: string; why?: string; kind?: string }>)
            : [],
          potential_opportunity: pick(fitRaw, 'potential_opportunity'),
          concerns: Array.isArray(fitRaw.concerns) ? fitRaw.concerns.map(String) : [],
          missing_information: Array.isArray(fitRaw.missing_information)
            ? fitRaw.missing_information.map(String)
            : [],
          profile_fields_used: Array.isArray(fitRaw.profile_fields_used)
            ? fitRaw.profile_fields_used.map(String)
            : [],
          profile_incomplete: asBoolean(fitRaw.profile_incomplete),
          profile_gaps: Array.isArray(fitRaw.profile_gaps)
            ? fitRaw.profile_gaps.map(String)
            : [],
        }
      : null,
    proposed_updates: list('proposed_updates').map((row) => ({
      id: asNumber(row.id),
      company_id: asNumber(row.company_id),
      field_key: pick(row, 'field_key'),
      current_value: pick(row, 'current_value'),
      proposed_value: pick(row, 'proposed_value'),
      source_name: pick(row, 'source_name'),
      source_url: pick(row, 'source_url'),
      research_date: pick(row, 'research_date'),
      confidence: pick(row, 'confidence'),
      status: pick(row, 'status'),
    })),
    sources: list('sources'),
    pages_researched: list('pages_researched'),
    data_provider:
      raw.data_provider && typeof raw.data_provider === 'object'
        ? (raw.data_provider as Record<string, unknown>)
        : {},
    research_history: list('research_history'),
    read_only_crm: raw.read_only_crm == null ? true : asBoolean(raw.read_only_crm),
    research_depth: pick(raw, 'research_depth') || 'quick',
    job: (() => {
      const j = (raw.job || null) as Record<string, unknown> | null
      if (!j) return null
      return {
        job_id: asNumber(j.job_id),
        company_id: asNumber(j.company_id),
        working_for_client_id: asNumber(j.working_for_client_id),
        campaign_id: j.campaign_id == null ? null : asNumber(j.campaign_id),
        research_run_id: j.research_run_id == null ? null : asNumber(j.research_run_id),
        research_depth: pick(j, 'research_depth') || 'deep',
        status: pick(j, 'status') || 'queued',
        progress: asNumber(j.progress, 0),
        progress_message: pick(j, 'progress_message'),
        cancel_requested: asBoolean(j.cancel_requested),
        attempt_count: asNumber(j.attempt_count, 0),
        max_attempts: asNumber(j.max_attempts, 2),
        openai_response_id: pick(j, 'openai_response_id'),
        openai_model: pick(j, 'openai_model'),
        usage:
          j.usage && typeof j.usage === 'object' ? (j.usage as Record<string, unknown>) : {},
        citations: Array.isArray(j.citations)
          ? (j.citations as Record<string, unknown>[]).map((c) => ({
              url: pick(c, 'url'),
              title: pick(c, 'title'),
            }))
          : [],
        sources: Array.isArray(j.sources)
          ? (j.sources as Record<string, unknown>[]).map((c) => ({
              url: pick(c, 'url'),
              title: pick(c, 'title'),
            }))
          : [],
        error_message: pick(j, 'error_message'),
        started_at: pick(j, 'started_at'),
        completed_at: pick(j, 'completed_at'),
        created_at: pick(j, 'created_at'),
        updated_at: pick(j, 'updated_at'),
        deep_research_configured: asBoolean(j.deep_research_configured),
        from_cache: asBoolean(j.from_cache),
        paid_refresh_required: asBoolean(j.paid_refresh_required),
        limited_by: pick(j, 'limited_by'),
      }
    })(),
    citations: list('citations').map((c) => ({
      url: pick(c, 'url'),
      title: pick(c, 'title'),
    })),
    deep_research_usage:
      raw.deep_research_usage && typeof raw.deep_research_usage === 'object'
        ? (raw.deep_research_usage as Record<string, unknown>)
        : {},
    deep_research_from_cache: asBoolean(raw.deep_research_from_cache),
    deep_research_paid_refresh_required: asBoolean(
      raw.deep_research_paid_refresh_required,
    ),
  }
}

export async function startCompanyResearch(params: {
  company_id?: number | null
  external_record_no?: string | null
  working_for_client_id?: number | null
  campaign_id?: number | null
  force_refresh?: boolean
  confirm_paid_refresh?: boolean
  research_depth?: 'quick' | 'deep'
}): Promise<import('../types/carmeco').ResearchCompanyResponse> {
  const response = await fetch('/api/research/company', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      company_id: params.company_id ?? null,
      external_record_no: params.external_record_no ?? null,
      working_for_client_id: params.working_for_client_id ?? null,
      campaign_id: params.campaign_id ?? null,
      force_refresh: Boolean(params.force_refresh),
      confirm_paid_refresh: Boolean(params.confirm_paid_refresh),
      research_depth: params.research_depth || 'quick',
    }),
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return normalizeResearchResponse(raw)
}

export async function fetchResearchJob(
  jobId: number,
): Promise<import('../types/carmeco').ResearchCompanyResponse> {
  const response = await fetch(`/api/research/jobs/${jobId}`)
  const raw = await parseJson<Record<string, unknown>>(response)
  return normalizeResearchResponse(raw)
}

export async function cancelResearchJob(
  jobId: number,
): Promise<import('../types/carmeco').ResearchCompanyResponse> {
  const response = await fetch(`/api/research/jobs/${jobId}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  const raw = await parseJson<Record<string, unknown>>(response)
  return normalizeResearchResponse(raw)
}

export async function fetchDeepResearchStatus(): Promise<
  import('../types/carmeco').DeepResearchStatus
> {
  const response = await fetch('/api/research/deep/status')
  const raw = await parseJson<Record<string, unknown>>(response)
  const limits =
    raw.limits && typeof raw.limits === 'object'
      ? (raw.limits as Record<string, unknown>)
      : {}
  const pricing =
    raw.pricing && typeof raw.pricing === 'object'
      ? (raw.pricing as Record<string, unknown>)
      : {}
  const monthly =
    raw.monthly && typeof raw.monthly === 'object'
      ? (raw.monthly as Record<string, unknown>)
      : {}
  return {
    deep_research_configured: asBoolean(raw.deep_research_configured),
    limits: {
      max_web_search_calls: asNumber(limits.max_web_search_calls, 10),
      max_output_tokens: asNumber(limits.max_output_tokens, 6000),
      max_attempts: asNumber(limits.max_attempts, 2),
      max_run_usd: asNumber(limits.max_run_usd, 1),
      monthly_limit_usd: asNumber(limits.monthly_limit_usd, 20),
      cache_days: asNumber(limits.cache_days, 30),
      dollar_limits_enforceable: asBoolean(limits.dollar_limits_enforceable),
      estimated_max_run_usd:
        limits.estimated_max_run_usd == null
          ? null
          : asNumber(limits.estimated_max_run_usd),
      estimated_max_run_status: pick(limits, 'estimated_max_run_status') || 'unavailable',
      estimated_max_run_note: pick(limits, 'estimated_max_run_note'),
      pilot_admin_only: asBoolean(limits.pilot_admin_only ?? true),
    },
    pricing: {
      estimate_available: asBoolean(pricing.estimate_available),
      note: pick(pricing, 'note'),
    },
    monthly: {
      month_start_utc: pick(monthly, 'month_start_utc'),
      spent_usd: monthly.spent_usd == null ? null : asNumber(monthly.spent_usd),
      remaining_usd:
        monthly.remaining_usd == null ? null : asNumber(monthly.remaining_usd),
      monthly_limit_usd: asNumber(monthly.monthly_limit_usd, 20),
      status: pick(monthly, 'status') || 'unavailable',
      dollar_limits_enforceable: asBoolean(monthly.dollar_limits_enforceable),
    },
  }
}

export async function fetchCompanyResearch(params: {
  company_id?: number | null
  external_record_no?: string | null
  working_for_client_id?: number | null
  campaign_id?: number | null
  run?: boolean
  force_refresh?: boolean
}): Promise<import('../types/carmeco').ResearchCompanyResponse> {
  const qs = new URLSearchParams()
  if (params.company_id) qs.set('company_id', String(params.company_id))
  if (params.external_record_no) qs.set('external_record_no', params.external_record_no)
  if (params.working_for_client_id)
    qs.set('working_for_client_id', String(params.working_for_client_id))
  if (params.campaign_id) qs.set('campaign_id', String(params.campaign_id))
  if (params.run) qs.set('run', 'true')
  if (params.force_refresh) qs.set('force_refresh', 'true')
  const response = await fetch(`/api/research/company?${qs.toString()}`)
  const raw = await parseJson<Record<string, unknown>>(response)
  return normalizeResearchResponse(raw)
}

export async function approveResearchUpdate(proposedUpdateId: number, approvedBy?: string) {
  const response = await fetch('/api/research/proposed-updates/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      proposed_update_id: proposedUpdateId,
      approved_by: approvedBy || null,
    }),
  })
  const payload = await parseJson<Record<string, unknown>>(response)
  return {
    status: pick(payload, 'status'),
    field_key: pick(payload, 'field_key'),
    old_value: pick(payload, 'old_value'),
    new_value: pick(payload, 'new_value'),
    approved_by: pick(payload, 'approved_by'),
    approved_at: pick(payload, 'approved_at'),
  }
}

export async function rejectResearchUpdate(proposedUpdateId: number, rejectedBy?: string) {
  const response = await fetch('/api/research/proposed-updates/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      proposed_update_id: proposedUpdateId,
      rejected_by: rejectedBy || null,
    }),
  })
  const payload = await parseJson<Record<string, unknown>>(response)
  return {
    status: pick(payload, 'status'),
    field_key: pick(payload, 'field_key'),
    master_value_unchanged: asBoolean(payload.master_value_unchanged),
    master_value: pick(payload, 'master_value'),
    rejected_by: pick(payload, 'rejected_by'),
    rejected_at: pick(payload, 'rejected_at'),
  }
}

// --- Client Setup ---

export type ClientSetupListItem = {
  client_id: number
  client_code: string
  client_name: string
  is_active: boolean
  completeness_percent: number
  client_information_percent: number
  target_profile_percent: number
  ai_fit_ready: boolean
  missing_sections: string[]
  campaign_count: number
  default_campaign_name: string
  can_edit: boolean
}

export type ClientCampaign = {
  campaign_id: number
  client_id: number
  campaign_name: string
  description: string
  active: boolean
  is_default: boolean
  primary_service: string
  secondary_services: string
  target_industries: string
  target_customer_types: string
  target_products: string
  manufacturing_processes_sought: string
  production_preference: string
  stamping_capability: string
  tooling_notes: string
  geographic_preferences: string
  geography_mode: string
  geography_required: boolean
  company_size_preferences: string
  positive_signals: string
  negative_signals: string
  exclusions: string
  target_titles: string
  fit_weighting_notes: string
  notes: string
  list_source: string
  import_date: string
  created_at: string
  updated_at: string
}

export type ClientSetup = {
  client_id: number
  client_code: string
  client_name: string
  website: string
  main_location: string
  main_phone: string
  description: string
  primary_owner_name: string
  is_active: boolean
  primary_service: string
  secondary_services: string
  products_services: string
  differentiators: string
  certifications: string
  equipment_capacity: string
  value_proposition: string
  default_campaign_id: number | null
  campaigns: ClientCampaign[]
  completeness: {
    percent: number
    filled_fields: number
    total_fields: number
    missing_sections: string[]
    missing_fields: string[]
    strong_fit_ready: boolean
    client_information_percent: number
    client_information_missing: string[]
    target_profile_percent: number
    target_profile_missing: string[]
    ai_fit_ready: boolean
    ai_fit_missing: string[]
    intentionally_unspecified?: string[]
  }
  field_sources: Record<string, string>
  can_edit: boolean
  audit_history: Array<{
    id: number
    client_id: number
    campaign_id: number | null
    entity_type: string
    field_name: string
    old_value: string
    new_value: string
    changed_by_name: string
    changed_at: string
    change_source?: string
  }>
  updated_at: string
}

export async function fetchClientSetupList(): Promise<ClientSetupListItem[]> {
  const response = await fetch('/api/clients/setup')
  const raw = await parseJson<unknown[]>(response)
  if (!Array.isArray(raw)) return []
  return raw.map((item) => {
    const row = item as Record<string, unknown>
    return {
      client_id: asNumber(row.client_id),
      client_code: pick(row, 'client_code'),
      client_name: pick(row, 'client_name'),
      is_active: asBoolean(row.is_active),
      completeness_percent: asNumber(row.completeness_percent),
      client_information_percent: asNumber(row.client_information_percent),
      target_profile_percent: asNumber(row.target_profile_percent),
      ai_fit_ready: asBoolean(row.ai_fit_ready),
      missing_sections: Array.isArray(row.missing_sections)
        ? row.missing_sections.map(String)
        : [],
      campaign_count: asNumber(row.campaign_count),
      default_campaign_name: pick(row, 'default_campaign_name'),
      can_edit: asBoolean(row.can_edit),
    }
  })
}

export async function fetchClientSetup(clientId: number): Promise<ClientSetup> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/setup`,
  )
  return (await parseJson(response)) as ClientSetup
}

export async function saveClientOverview(
  clientId: number,
  body: Record<string, unknown>,
): Promise<ClientSetup> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/setup/overview`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientSetup
}

export async function saveClientSells(
  clientId: number,
  body: Record<string, unknown>,
): Promise<ClientSetup> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/setup/sells`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientSetup
}

export async function saveClientCampaign(
  clientId: number,
  campaignId: number | null,
  body: Record<string, unknown>,
): Promise<ClientSetup> {
  const path =
    campaignId == null
      ? `/api/clients/${encodeURIComponent(String(clientId))}/setup/campaigns`
      : `/api/clients/${encodeURIComponent(String(clientId))}/setup/campaigns/${encodeURIComponent(String(campaignId))}`
  const response = await fetch(path, {
    method: campaignId == null ? 'POST' : 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return (await parseJson(response)) as ClientSetup
}

export async function setClientDefaultCampaign(
  clientId: number,
  campaignId: number,
): Promise<ClientSetup> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/setup/campaigns/${encodeURIComponent(String(campaignId))}/default`,
    { method: 'POST' },
  )
  return (await parseJson(response)) as ClientSetup
}

export async function deleteClientCampaign(
  clientId: number,
  campaignId: number,
): Promise<ClientSetup> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/setup/campaigns/${encodeURIComponent(String(campaignId))}`,
    { method: 'DELETE' },
  )
  return (await parseJson(response)) as ClientSetup
}

export type ClientDocument = {
  document_id: number
  client_id: number
  filename: string
  document_type: string
  uploaded_at: string
  uploaded_by: string
  processing_status: string
  sensitive_flag: boolean
  sensitive_note: string
  archived: boolean
  file_size: number
  replaced_by_document_id: number | null
}

export type ClientExtractionProposal = {
  proposal_id: number
  client_id: number
  document_id: number | null
  document_filename: string
  document_type: string
  section: string
  field_name: string
  field_label: string
  existing_value: string
  proposed_value: string
  source_reference: string
  raw_source_text: string
  source_locator: string
  confidence: string
  classification: string
  status: string
  status_label?: string
  reviewed_by: string
  reviewed_at: string
  approved_value: string
  extracted_value: string
  apply_target: string
  applied_at: string
  resolved_at?: string
  resolved_by?: string
  resolution_reason?: string
  resolved_by_proposal_ids?: number[]
  resolved_client_contact_ids?: number[]
}

export type ProposalIncorporationAnalysis = {
  proposal_id: number
  client_id: number
  status: string
  document_filename: string
  people: Array<{
    name: string
    email: string
    overall: string
    fields: Array<{
      field: string
      value: string
      status: string
      matched_contact_id: number | null
      note: string
    }>
  }>
  fully_incorporated: boolean
  unresolved_summary: string[]
  resolution_suggestion: string
  can_suggest_resolve: boolean
  suggested_resolved_by_proposal_ids: number[]
  suggested_resolved_client_contact_ids: number[]
  note: string
}

export type ClientDocumentProcessResult = {
  document: ClientDocument
  proposals_created: number
  sensitive_excluded: number
  type_hint: string
  appointment_batch_id: number | null
  message: string
}

export type ClientKnowledgeHub = {
  client_id: number
  client_name: string
  client_code: string
  can_edit: boolean
  documents: ClientDocument[]
  sections: Array<{
    section_key: string
    payload: Record<string, unknown>
    updated_at: string
    updated_by: string
  }>
  email_templates: Array<{
    template_id: number
    client_id: number
    template_name: string
    template_type: string
    subject: string
    body: string
    is_active: boolean
    updated_at: string
  }>
  email_accounts?: unknown[]
  extraction_proposals: ClientExtractionProposal[]
  document_types: string[]
  appointment_events: Record<string, unknown>[]
  client_contacts?: ClientContact[]
  client_contact_role_types?: string[]
}

export type ClientContact = {
  contact_id: number
  client_id: number
  name: string
  title: string
  email: string
  phone: string
  role_type: string
  notes: string
  active: boolean
  source_document_id: number | null
  source_proposal_id: number | null
  created_at: string
  updated_at: string
  created_by: string
  updated_by: string
}

export type ClientContactSplitPreview = {
  proposal_id: number
  client_id: number
  source_text: string
  people: Array<{
    name: string
    title: string
    email: string
    phone: string
    role_type: string
    notes: string
    raw: string
    possible_duplicate_id: number | null
    possible_duplicate_name: string
    needs_review: boolean
  }>
  person_count: number
}

export type ClientAppointmentImportBatch = {
  batch_id: number
  client_id: number
  document_id: number | null
  filename: string
  status: string
  headers: string[]
  column_mapping: Record<string, string>
  row_count: number
  uploaded_at: string
  uploaded_by: string
  target_fields: string[]
}

export type ClientAppointmentImportPreview = {
  batch: ClientAppointmentImportBatch
  rows: Array<{
    row_id: number
    row_index: number
    mapped: Record<string, string>
    company_match_status: string
    company_id: number | null
    company_name: string
    company_record_no: string
    contact_match_status: string
    contact_id: number | null
    contact_name: string
    import_status: string
  }>
}

export async function fetchClientKnowledge(clientId: number): Promise<ClientKnowledgeHub> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge`,
  )
  return (await parseJson(response)) as ClientKnowledgeHub
}

export async function uploadClientDocument(
  clientId: number,
  file: File,
  documentType: string,
  replaceDocumentId?: number,
): Promise<ClientDocument> {
  const form = new FormData()
  form.append('file', file)
  form.append('document_type', documentType)
  if (replaceDocumentId != null) {
    form.append('replace_document_id', String(replaceDocumentId))
  }
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/documents`,
    { method: 'POST', body: form },
  )
  return (await parseJson(response)) as ClientDocument
}

export async function processClientDocument(
  clientId: number,
  documentId: number,
): Promise<ClientDocumentProcessResult> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/documents/${encodeURIComponent(String(documentId))}/process`,
    { method: 'POST' },
  )
  return (await parseJson(response)) as ClientDocumentProcessResult
}

export async function reviewExtractionProposal(
  clientId: number,
  proposalId: number,
  status: 'Approved' | 'Rejected' | 'Pending',
  editedValue?: string,
  remap?: {
    section?: string
    field_name?: string
    item_title?: string
  },
): Promise<ClientExtractionProposal> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/${encodeURIComponent(String(proposalId))}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status,
        edited_value: editedValue ?? null,
        section: remap?.section ?? null,
        field_name: remap?.field_name ?? null,
        item_title: remap?.item_title ?? null,
      }),
    },
  )
  return (await parseJson(response)) as ClientExtractionProposal
}

export async function listExtractionProposals(
  clientId: number,
  status: 'Pending' | 'Approved' | 'Rejected' | 'Resolved' | 'All' = 'Pending',
): Promise<ClientExtractionProposal[]> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions?status=${encodeURIComponent(status)}`,
  )
  return (await parseJson(response)) as ClientExtractionProposal[]
}

export async function resolveExtractionProposal(
  clientId: number,
  proposalId: number,
  body: {
    resolution_reason: string
    resolved_by_proposal_ids?: number[]
    resolved_client_contact_ids?: number[]
  },
): Promise<ClientExtractionProposal> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/${encodeURIComponent(String(proposalId))}/resolve`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientExtractionProposal
}

export async function fetchIncorporationAnalysis(
  clientId: number,
  proposalId: number,
): Promise<ProposalIncorporationAnalysis> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/${encodeURIComponent(String(proposalId))}/incorporation-analysis`,
  )
  return (await parseJson(response)) as ProposalIncorporationAnalysis
}

export async function createClientContact(
  clientId: number,
  body: {
    name: string
    title?: string
    email?: string
    phone?: string
    role_type?: string
    notes?: string
  },
): Promise<ClientContact> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/contacts`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientContact
}

export async function updateClientContact(
  clientId: number,
  contactId: number,
  body: {
    name?: string
    title?: string
    email?: string
    phone?: string
    role_type?: string
    notes?: string
    active?: boolean
  },
): Promise<ClientContact> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/contacts/${encodeURIComponent(String(contactId))}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientContact
}

export async function deactivateClientContact(
  clientId: number,
  contactId: number,
): Promise<ClientContact> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/contacts/${encodeURIComponent(String(contactId))}/deactivate`,
    { method: 'POST' },
  )
  return (await parseJson(response)) as ClientContact
}

export async function previewSplitClientContacts(
  clientId: number,
  proposalId: number,
): Promise<ClientContactSplitPreview> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/${encodeURIComponent(String(proposalId))}/split-contacts`,
  )
  return (await parseJson(response)) as ClientContactSplitPreview
}

export async function createSplitClientContactProposals(
  clientId: number,
  proposalId: number,
  people?: ClientContactSplitPreview['people'],
): Promise<{
  parent_proposal_id: number
  created_count: number
  proposals: Array<Record<string, unknown>>
  message: string
}> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/${encodeURIComponent(String(proposalId))}/split-contacts`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(people ? { people } : {}),
    },
  )
  return (await parseJson(response)) as {
    parent_proposal_id: number
    created_count: number
    proposals: Array<Record<string, unknown>>
    message: string
  }
}

export async function fetchKnowledgeSectionCatalog(): Promise<
  Array<{
    section_key: string
    display_name: string
    fields: Array<{ field_name: string; field_label: string }>
  }>
> {
  const response = await fetch('/api/knowledge/section-catalog')
  const raw = await parseJson<unknown>(response)
  return Array.isArray(raw) ? (raw as Array<{
    section_key: string
    display_name: string
    fields: Array<{ field_name: string; field_label: string }>
  }>) : []
}

export async function updateKnowledgeField(
  clientId: number,
  sectionKey: string,
  fieldName: string,
  value: string,
): Promise<{ section_key: string; payload: Record<string, unknown> }> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/sections/${encodeURIComponent(sectionKey)}/fields`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field_name: fieldName, value }),
    },
  )
  return (await parseJson(response)) as {
    section_key: string
    payload: Record<string, unknown>
  }
}

export type ClientEmailTemplate = {
  template_id: number
  client_id: number
  template_name: string
  template_type: string
  subject: string
  body: string
  is_active: boolean
  source_document_id?: number | null
  source_proposal_ids?: number[]
  created_at?: string
  updated_at?: string
  created_by_name?: string
  approved_by_name?: string
}

export type EmailTemplateNameConflict = {
  kind: string
  source_name: string
  approved_contact_name: string
  approved_contact_id: number | null
  note: string
}

export type EmailTemplateGroupPreview = {
  client_id: number
  document_id: number | null
  document_filename: string
  source_proposal_ids: number[]
  component_statuses: Record<string, string>
  template_name: string
  template_type: string
  subject: string
  body: string
  is_active: boolean
  evidence: string[]
  name_conflicts: EmailTemplateNameConflict[]
  requires_human_review: boolean
  message: string
}

export async function listEmailTemplates(clientId: number): Promise<ClientEmailTemplate[]> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/email-templates`,
  )
  const raw = await parseJson<unknown>(response)
  return Array.isArray(raw) ? (raw as ClientEmailTemplate[]) : []
}

export async function previewEmailTemplateGroup(
  clientId: number,
  proposalIds: number[],
): Promise<EmailTemplateGroupPreview> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/email-templates/preview-group`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_ids: proposalIds }),
    },
  )
  return (await parseJson(response)) as EmailTemplateGroupPreview
}

export async function approveEmailTemplateGroup(
  clientId: number,
  body: {
    proposal_ids: number[]
    template_name: string
    template_type: string
    subject: string
    body: string
    is_active: boolean
    acknowledge_name_conflicts?: boolean
  },
): Promise<{
  template: ClientEmailTemplate
  resolved_proposal_ids: number[]
  skipped: Array<{ proposal_id: number; reason: string }>
  message: string
}> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/email-templates/approve-group`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as {
    template: ClientEmailTemplate
    resolved_proposal_ids: number[]
    skipped: Array<{ proposal_id: number; reason: string }>
    message: string
  }
}

export async function updateEmailTemplate(
  clientId: number,
  templateId: number,
  body: {
    template_name: string
    template_type: string
    subject: string
    body: string
    is_active: boolean
  },
): Promise<ClientEmailTemplate> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/email-templates/${encodeURIComponent(String(templateId))}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientEmailTemplate
}

export type ClientEmailAccountAssignment = {
  assignment_id: number
  client_email_account_id: number
  user_id: number | null
  source_rep_name: string
  active: boolean
  is_default_for_rep: boolean
  created_at?: string
  updated_at?: string
}

export type ClientEmailAccount = {
  account_id: number
  client_id: number
  email_address: string
  display_name: string
  provider: string
  connection_status: string
  active: boolean
  is_default: boolean
  provider_connection_ref: string
  created_at?: string
  updated_at?: string
  created_by?: string
  updated_by?: string
  assignments: ClientEmailAccountAssignment[]
}

export type ClientEmailAccountSuggestion = {
  client_id: number
  suggested_email_address: string
  suggested_display_name: string
  suggested_provider: string
  suggested_source_rep_name: string
  suggested_user_id: number | null
  email_suggestion_source: string
  rep_suggestion_source: string
  already_configured: boolean
  auto_created: boolean
  notes: string[]
}

export type ClientEmailSignature = {
  signature_id: number
  client_id: number
  email_account_id: number | null
  rep_user_id: number | null
  source_rep_name: string
  signature_name: string
  signature_body: string
  active: boolean
  is_default: boolean
  created_at?: string
  updated_at?: string
}

export type PlaceholderResolution = {
  placeholder: string
  resolved: boolean
  value: string
  note: string
}

export type ClientEmailPreviewResult = {
  client_id: number
  client_name: string
  account_id: number
  template_id: number | null
  template_name: string
  is_blank?: boolean
  contact_id: number
  company_id: number | null
  from_address: string
  from_display: string
  to_address: string
  to_contact_name: string
  subject: string
  body: string
  message_body?: string
  signature: string
  signature_placement?: string
  signature_source?: string
  revenue_specialist?: string
  meeting_with?: string
  connection_status: string
  connection_connected: boolean
  send_enabled: boolean
  unresolved_placeholders: PlaceholderResolution[]
  resolved_placeholders: PlaceholderResolution[]
  legacy_token_notes?: string[]
  message: string
  supported_placeholders: string[]
  appointment_event_id?: number | null
  sales_event_id?: number | null
  external_record_no?: string
  campaign_id?: number | null
}

export type EmailPreviewContact = {
  contact_id: number
  contact_name: string
  first_name?: string
  email: string
  company_id: number
  company_name: string
}

export type EmailPreviewAppointment = {
  source: 'sales_event' | 'appointment_event' | string
  event_id: number
  label: string
  appointment_date: string
  appointment_time: string
  appointment_date_raw?: string
  appointment_time_raw?: string
  company_name: string
  contact_name: string
  meeting_with: string
  event_type: string
  meeting_type?: string
  timezone?: string
  source_date_time_text?: string
  company_id?: number | null
  contact_id?: number | null
}

export type EmailComposeContactOption = {
  contact_id: number
  contact_name: string
  email: string
  title?: string
}

/** Future Send Email payload shape — not submitted until provider send exists. */
export type EmailComposeSendPayload = {
  client_id: number
  company_id: number | null
  contact_id: number
  sender_account_id: number
  template_id: number | null
  subject: string
  body: string
  rep: string
  appointment_event_id?: number | null
}

export async function listEmailAccounts(clientId: number): Promise<ClientEmailAccount[]> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts`,
  )
  const raw = await parseJson<unknown>(response)
  return Array.isArray(raw) ? (raw as ClientEmailAccount[]) : []
}

export async function fetchEmailAccountSuggestions(
  clientId: number,
): Promise<ClientEmailAccountSuggestion> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts/suggestions`,
  )
  return (await parseJson(response)) as ClientEmailAccountSuggestion
}

export async function createEmailAccount(
  clientId: number,
  body: {
    email_address: string
    display_name?: string
    provider?: string
    active?: boolean
    is_default?: boolean
  },
): Promise<ClientEmailAccount> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientEmailAccount
}

export async function updateEmailAccount(
  clientId: number,
  accountId: number,
  body: {
    email_address: string
    display_name?: string
    provider?: string
    connection_status?: string
    active?: boolean
    is_default?: boolean
  },
): Promise<ClientEmailAccount> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts/${encodeURIComponent(String(accountId))}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientEmailAccount
}

export async function deactivateEmailAccount(
  clientId: number,
  accountId: number,
): Promise<ClientEmailAccount> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts/${encodeURIComponent(String(accountId))}/deactivate`,
    { method: 'POST' },
  )
  return (await parseJson(response)) as ClientEmailAccount
}

export async function connectEmailAccount(
  clientId: number,
  accountId: number,
): Promise<{
  available: boolean
  message: string
  connection_status: string
  authorization_url?: string
}> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts/${encodeURIComponent(String(accountId))}/connect`,
    { method: 'POST' },
  )
  return (await parseJson(response)) as {
    available: boolean
    message: string
    connection_status: string
    authorization_url?: string
  }
}

export async function disconnectEmailAccount(
  clientId: number,
  accountId: number,
): Promise<{ connection_status: string; message: string }> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts/${encodeURIComponent(String(accountId))}/disconnect`,
    { method: 'POST' },
  )
  return (await parseJson(response)) as { connection_status: string; message: string }
}

export async function fetchGoogleOAuthStatus(): Promise<{
  configured: boolean
  redirect_uri: string
  scopes: string[]
  message: string
}> {
  const response = await fetch('/api/email/google/status')
  return (await parseJson(response)) as {
    configured: boolean
    redirect_uri: string
    scopes: string[]
    message: string
  }
}

export async function sendClientEmail(
  clientId: number,
  body: {
    account_id: number
    contact_id?: number | null
    company_id?: number | null
    template_id?: number | null
    to_address: string
    subject: string
    body: string
    signature?: string
    appointment_event_id?: number | null
    confirm_send: boolean
    confirm_recipient: string
  },
): Promise<{
  ok: boolean
  provider_message_id: string
  sales_event_id: number | null
  message: string
}> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-send`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as {
    ok: boolean
    provider_message_id: string
    sales_event_id: number | null
    message: string
  }
}

export async function createEmailAccountAssignment(
  clientId: number,
  accountId: number,
  body: {
    user_id?: number | null
    source_rep_name?: string
    active?: boolean
    is_default_for_rep?: boolean
  },
): Promise<ClientEmailAccountAssignment> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-accounts/${encodeURIComponent(String(accountId))}/assignments`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientEmailAccountAssignment
}

export async function listEmailSignatures(clientId: number): Promise<ClientEmailSignature[]> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-signatures`,
  )
  const raw = await parseJson<unknown>(response)
  return Array.isArray(raw) ? (raw as ClientEmailSignature[]) : []
}

export async function createEmailSignature(
  clientId: number,
  body: {
    email_account_id?: number | null
    rep_user_id?: number | null
    source_rep_name?: string
    signature_name?: string
    signature_body?: string
    active?: boolean
    is_default?: boolean
  },
): Promise<ClientEmailSignature> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-signatures`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientEmailSignature
}

export async function updateEmailSignature(
  clientId: number,
  signatureId: number,
  body: {
    email_account_id?: number | null
    rep_user_id?: number | null
    source_rep_name?: string
    signature_name?: string
    signature_body?: string
    active?: boolean
    is_default?: boolean
  },
): Promise<ClientEmailSignature> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-signatures/${encodeURIComponent(String(signatureId))}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ClientEmailSignature
}

export async function listEmailPreviewContacts(
  clientId: number,
): Promise<EmailPreviewContact[]> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-preview/contacts?limit=500`,
  )
  const raw = await parseJson<unknown>(response)
  return Array.isArray(raw) ? (raw as EmailPreviewContact[]) : []
}

export async function listEmailPreviewAppointments(
  clientId: number,
  contactId: number,
): Promise<EmailPreviewAppointment[]> {
  const query = new URLSearchParams({ contact_id: String(contactId) })
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-preview/appointments?${query}`,
  )
  const raw = await parseJson<unknown>(response)
  return Array.isArray(raw) ? (raw as EmailPreviewAppointment[]) : []
}

export async function previewClientEmail(
  clientId: number,
  body: {
    account_id: number
    contact_id: number
    template_id?: number | null
    appointment_date?: string
    appointment_time?: string
    appointment_contact?: string
    appointment_event_id?: number | null
    sales_event_id?: number | null
    meeting_with?: string
  },
): Promise<ClientEmailPreviewResult> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/email-preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        account_id: body.account_id,
        contact_id: body.contact_id,
        template_id: body.template_id ?? null,
        appointment_date: body.appointment_date || '',
        appointment_time: body.appointment_time || '',
        appointment_contact: body.appointment_contact || '',
        appointment_event_id: body.appointment_event_id ?? null,
        sales_event_id: body.sales_event_id ?? null,
        meeting_with: body.meeting_with || '',
      }),
    },
  )
  return (await parseJson(response)) as ClientEmailPreviewResult
}

export async function bulkReviewExtractions(
  clientId: number,
  body: {
    mode: 'selected' | 'matches'
    proposal_ids?: number[]
    status?: 'Approved' | 'Rejected'
  },
): Promise<ClientExtractionProposal[]> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/bulk-review`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: body.mode,
        proposal_ids: body.proposal_ids || [],
        status: body.status || 'Approved',
      }),
    },
  )
  return (await parseJson(response)) as ClientExtractionProposal[]
}

export type ExtractionReviewClassification = {
  proposal_id: number
  section?: string
  field_name?: string
  field_label?: string
  status?: string
  ready: boolean
  review_class: string
  reason: string
  resolution_reason?: string
}

export type BulkResolvePreview = {
  selected_count: number
  eligible_count: number
  not_eligible_count: number
  eligible: ExtractionReviewClassification[]
  not_eligible: ExtractionReviewClassification[]
  message: string
}

export type BulkResolveResult = {
  resolved_count: number
  skipped_count: number
  error_count: number
  resolved: Array<{ proposal_id: number; status?: string; resolution_reason?: string }>
  skipped: ExtractionReviewClassification[]
  errors: Array<{ proposal_id: number; error: string }>
  message: string
}

export type BulkRejectResult = {
  rejected_count: number
  skipped_count: number
  rejected_ids: number[]
  skipped: Array<{ proposal_id: number; reason: string }>
  rejection_category: string
  message: string
}

export async function fetchReviewClassifications(
  clientId: number,
): Promise<ExtractionReviewClassification[]> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/review-classifications`,
  )
  const raw = await parseJson<unknown>(response)
  return Array.isArray(raw) ? (raw as ExtractionReviewClassification[]) : []
}

export async function previewBulkResolve(
  clientId: number,
  proposalIds: number[],
): Promise<BulkResolvePreview> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/bulk-resolve/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_ids: proposalIds }),
    },
  )
  return (await parseJson(response)) as BulkResolvePreview
}

export async function bulkResolveExtractions(
  clientId: number,
  proposalIds: number[],
): Promise<BulkResolveResult> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/bulk-resolve`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_ids: proposalIds }),
    },
  )
  return (await parseJson(response)) as BulkResolveResult
}

export async function bulkRejectExtractions(
  clientId: number,
  body: {
    proposal_ids: number[]
    rejection_category: string
    rejection_note?: string
  },
): Promise<BulkRejectResult> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/extractions/bulk-reject`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        proposal_ids: body.proposal_ids,
        rejection_category: body.rejection_category,
        rejection_note: body.rejection_note || '',
      }),
    },
  )
  return (await parseJson(response)) as BulkRejectResult
}

export async function archiveClientDocument(
  clientId: number,
  documentId: number,
): Promise<ClientDocument> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/documents/${encodeURIComponent(String(documentId))}/archive`,
    { method: 'POST' },
  )
  return (await parseJson(response)) as ClientDocument
}

export async function startAppointmentImport(
  clientId: number,
  file: File,
): Promise<ClientAppointmentImportBatch> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/appointment-imports`,
    { method: 'POST', body: form },
  )
  return (await parseJson(response)) as ClientAppointmentImportBatch
}

export async function mapAppointmentImport(
  clientId: number,
  batchId: number,
  columnMapping: Record<string, string>,
): Promise<ClientAppointmentImportPreview> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/appointment-imports/${encodeURIComponent(String(batchId))}/map`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ column_mapping: columnMapping }),
    },
  )
  return (await parseJson(response)) as ClientAppointmentImportPreview
}

export async function confirmAppointmentImport(
  clientId: number,
  batchId: number,
): Promise<ClientAppointmentImportBatch> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/appointment-imports/${encodeURIComponent(String(batchId))}/confirm`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    },
  )
  return (await parseJson(response)) as ClientAppointmentImportBatch
}

export type EngagementImportBatch = {
  batch_id: number
  client_id: number
  document_id: number | null
  filename: string
  status: string
  uploaded_at: string
  uploaded_by: string
  sheets: Array<{
    sheet_id: number
    sheet_name: string
    detected_type: string
    user_type: string
    headers: string[]
    column_mapping: Record<string, string>
    row_count: number
    is_empty: boolean
  }>
  target_fields: string[]
  sheet_type_options: string[]
  summary: Record<string, number>
}

export type EngagementImportRow = {
  row_id: number
  sheet_name: string
  sheet_type: string
  source_row_number: number
  mapped: Record<string, string>
  event_type: string
  source_date_time_text: string
  event_date: string
  event_time: string
  company_match_status: string
  company_id: number | null
  company_name: string
  company_record_no: string
  contact_match_status: string
  contact_id: number | null
  contact_name: string
  source_rev_spec_text: string
  source_appointment_grade: string
  duplicate_status: string
  needs_review: boolean
  selected: boolean
  review_flags: string[]
  datetime_needs_review: boolean
  client_validation: string
}

export type EngagementImportPreview = {
  batch: EngagementImportBatch
  rows: EngagementImportRow[]
  summary: Record<string, number>
}

export type ContactWorkspace = {
  contact_id: number
  first_name: string
  last_name: string
  title: string
  phone: string
  alt_phone: string
  email: string
  company_id: number | null
  company_name: string
  company_record_no: string
  client_id: number
  client_name: string
  client_code: string
  relationship_id: number | null
  assigned_to_client?: boolean
  company_assigned_to_client?: boolean
  status: string
  assigned_user_id: number | null
  assigned_user: string
  next_action: string
  follow_up_date: string
  follow_up_time: string
  open_follow_up?: ContactOpenFollowUp | null
  sales_events: Array<{
    event_id: number
    event_type: string
    event_date: string
    source_date_time_text: string
    source_appointment_grade: string
    source_rev_spec_text: string
    rev_spec_user_id?: number | null
    caller_notes: string
    sales_notes: string
    quoted_amount?: number | null
    outcome_normalized?: string
    source_outcome?: string
    source_file_name: string
    source_sheet: string
    source_row: number
  }>
  activities: Record<string, unknown>[]
  notes: Record<string, unknown>[]
  timeline: ContactTimelineItem[]
  company_timeline: ContactTimelineItem[]
  reps: ContactRepOption[]
  linkedin_url?: string
  location?: string
  zoominfo_contact_id?: string
  source?: string
  source_updated_at?: string
}

export type ContactRepOption = {
  user_id: number
  full_name: string
}

export type ContactOpenFollowUp = {
  source: string
  source_id: number | null
  activity_id: number | null
  due_date: string
  due_time: string
  assigned_user: string
  notes: string
}

export type ContactTimelineItem = {
  item_type: string
  id: number
  title: string
  body: string
  at: string
  created_by: string
  outcome: string
  follow_up_at: string | null
}

export type ContactWorkflowUpdate = {
  client_id: number
  status?: string | null
  assigned_user_id?: number | null
  next_action?: string | null
  follow_up_date?: string | null
  follow_up_time?: string | null
  user?: string
}

export type ContactActivityCreate = {
  client_id: number
  activity_type: string
  notes?: string
  outcome?: string
  status?: string | null
  next_action?: string | null
  follow_up_date?: string | null
  follow_up_time?: string | null
  assigned_user_id?: number | null
  schedule_follow_up?: boolean
  created_by?: string
  appointment?: AppointmentDetailsPayload | null
}

export type AppointmentDetailsPayload = {
  appointment_date?: string
  start_time?: string
  timezone?: string
  datetime_tbd?: boolean
  appointment_type?: string
  location_or_link?: string
  revenue_specialist_user_id?: number | null
  notes?: string
  source?: string
  idempotency_key?: string
}

export type ContactWorkflowResult = {
  ok: boolean
  message: string
  workspace: ContactWorkspace
  activity_id: number | null
  follow_up_activity_id: number | null
  appointment_id?: number | null
}

export type AppointmentRecord = {
  id: number
  client_id: number
  company_id: number
  relationship_id: number
  contact_id: number | null
  activity_id: number | null
  company_name: string
  company_record_no: string
  contact_name: string
  appointment_date: string
  start_time: string
  timezone: string
  datetime_tbd: boolean
  appointment_type: string
  location_or_link: string
  revenue_specialist_user_id: number | null
  revenue_specialist_name: string
  notes: string
  source: string
  status: string
  grade: string
  outcome: string
  follow_up_notes: string
  created_by: string
  created_at: string
  updated_at: string
  cancellation_reason: string
  cancelled_by: string
  cancelled_at: string
  completed_by: string
  completed_at: string
  bucket: string
}

export type AppointmentListResponse = {
  client_id: number
  bucket: string
  items: AppointmentRecord[]
  counts: Record<string, number>
  set_count: number
}

export type AppointmentSummary = {
  client_id: number
  set_count: number
  scheduled_count: number
  today_count: number
  upcoming_count: number
  past_count: number
  cancelled_count: number
  upcoming: AppointmentRecord[]
  today: AppointmentRecord[]
}

export type AppointmentActionResult = {
  ok: boolean
  message: string
  appointment: AppointmentRecord
}

export type ContactAssignRequest = {
  client_id: number
  add_company?: boolean
  user?: string
}

export type ContactAssignResult = {
  ok: boolean
  message: string
  needs_company_confirmation: boolean
  company_assigned: boolean
  already_assigned: boolean
  company_created: boolean
  workspace: ContactWorkspace
  activity_id: number | null
}

export async function startEngagementImport(
  clientId: number,
  file: File,
): Promise<EngagementImportBatch> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/engagement-imports`,
    { method: 'POST', body: form },
  )
  return (await parseJson(response)) as EngagementImportBatch
}

export async function classifyEngagementSheets(
  clientId: number,
  batchId: number,
  sheets: Array<{ sheet_id: number; user_type: string }>,
): Promise<EngagementImportBatch> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/engagement-imports/${encodeURIComponent(String(batchId))}/classify`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheets }),
    },
  )
  return (await parseJson(response)) as EngagementImportBatch
}

export async function mapEngagementImport(
  clientId: number,
  batchId: number,
  sheetMappings: Record<string, Record<string, string>> = {},
): Promise<EngagementImportPreview> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/engagement-imports/${encodeURIComponent(String(batchId))}/map`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet_mappings: sheetMappings }),
    },
  )
  return (await parseJson(response)) as EngagementImportPreview
}

export async function fetchEngagementPreview(
  clientId: number,
  batchId: number,
  filterStatus?: string,
): Promise<EngagementImportPreview> {
  const qs = filterStatus ? `?filter_status=${encodeURIComponent(filterStatus)}` : ''
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/knowledge/engagement-imports/${encodeURIComponent(String(batchId))}/preview${qs}`,
  )
  return (await parseJson(response)) as EngagementImportPreview
}

export async function fetchContacts(params?: {
  client_id?: number | null
  all_clients?: boolean
  q?: string
  limit?: number
  offset?: number
}): Promise<ContactsResponse> {
  const query = new URLSearchParams()
  if (params?.all_clients) {
    query.set('all_clients', 'true')
  } else if (params?.client_id != null && Number(params.client_id) > 0) {
    query.set('client_id', String(params.client_id))
  }
  if (params?.q?.trim()) query.set('q', params.q.trim())
  if (params?.limit != null) query.set('limit', String(params.limit))
  if (params?.offset != null) query.set('offset', String(params.offset))
  const qs = query.toString()
  const response = await fetch(`/api/contacts${qs ? `?${qs}` : ''}`)
  const raw = (await parseJson<Record<string, unknown>>(response)) as Record<string, unknown>
  const rows = Array.isArray(raw.contacts) ? raw.contacts : []
  return {
    client: (raw.client || {}) as ContactsResponse['client'],
    total: asNumber(raw.total),
    client_total: asNumber(raw.client_total, asNumber(raw.total)),
    offset: asNumber(raw.offset),
    limit: asNumber(raw.limit, 50),
    contacts: rows.map((item) => {
      const record = (item ?? {}) as Record<string, unknown>
      const firstName = pick(record, 'first_name', 'FirstName', 'firstname')
      const lastName = pick(record, 'last_name', 'LastName', 'lastname')
      const fullName =
        pick(record, 'full_name', 'contact_name', 'name') ||
        `${firstName} ${lastName}`.trim()
      return {
        id: asNumber(record.id, asNumber(record.contact_id)),
        first_name: firstName,
        last_name: lastName,
        full_name: fullName,
        title: pick(record, 'title'),
        phone: pick(record, 'phone'),
        alt_phone: pick(record, 'alt_phone'),
        email: pick(record, 'email'),
        company_id: record.company_id == null ? null : asNumber(record.company_id),
        company_name: pick(record, 'company_name'),
        company_record_no: pick(record, 'company_record_no', 'external_record_no'),
        city: pick(record, 'city'),
        state: pick(record, 'state'),
        client_id: asNumber(record.client_id),
        client_name: pick(record, 'client_name'),
        client_code: pick(record, 'client_code'),
        relationship_id: asNumber(record.relationship_id),
        status: pick(record, 'status'),
      } satisfies ContactListItem
    }),
  }
}

export async function fetchContactWorkspace(
  contactId: number,
  clientId?: number | null,
): Promise<ContactWorkspace> {
  const qs =
    clientId != null && clientId > 0 ? `?client_id=${encodeURIComponent(String(clientId))}` : ''
  const response = await fetch(`/api/contacts/${encodeURIComponent(String(contactId))}${qs}`)
  return (await parseJson(response)) as ContactWorkspace
}

export async function updateContactWorkflow(
  contactId: number,
  body: ContactWorkflowUpdate,
): Promise<ContactWorkflowResult> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch(`/api/contacts/${encodeURIComponent(String(contactId))}/workflow`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ContactWorkflowResult
}

export async function createContactActivity(
  contactId: number,
  body: ContactActivityCreate,
): Promise<ContactWorkflowResult> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch(`/api/contacts/${encodeURIComponent(String(contactId))}/activities`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ContactWorkflowResult
}

function asAppointmentRecord(raw: Record<string, unknown>): AppointmentRecord {
  return {
    id: asNumber(raw.id),
    client_id: asNumber(raw.client_id),
    company_id: asNumber(raw.company_id),
    relationship_id: asNumber(raw.relationship_id),
    contact_id: raw.contact_id == null ? null : asNumber(raw.contact_id),
    activity_id: raw.activity_id == null ? null : asNumber(raw.activity_id),
    company_name: pick(raw, 'company_name'),
    company_record_no: pick(raw, 'company_record_no', 'external_record_no'),
    contact_name: pick(raw, 'contact_name'),
    appointment_date: pick(raw, 'appointment_date'),
    start_time: pick(raw, 'start_time'),
    timezone: pick(raw, 'timezone') || 'America/Chicago',
    datetime_tbd: asBoolean(raw.datetime_tbd),
    appointment_type: pick(raw, 'appointment_type') || 'Phone',
    location_or_link: pick(raw, 'location_or_link'),
    revenue_specialist_user_id:
      raw.revenue_specialist_user_id == null ? null : asNumber(raw.revenue_specialist_user_id),
    revenue_specialist_name: pick(raw, 'revenue_specialist_name'),
    notes: pick(raw, 'notes'),
    source: pick(raw, 'source') || 'Phone Call',
    status: pick(raw, 'status') || 'scheduled',
    grade: pick(raw, 'grade'),
    outcome: pick(raw, 'outcome'),
    follow_up_notes: pick(raw, 'follow_up_notes'),
    created_by: pick(raw, 'created_by'),
    created_at: pick(raw, 'created_at'),
    updated_at: pick(raw, 'updated_at'),
    cancellation_reason: pick(raw, 'cancellation_reason'),
    cancelled_by: pick(raw, 'cancelled_by'),
    cancelled_at: pick(raw, 'cancelled_at'),
    completed_by: pick(raw, 'completed_by'),
    completed_at: pick(raw, 'completed_at'),
    bucket: pick(raw, 'bucket') || 'upcoming',
  }
}

export async function fetchAppointments(
  clientId: number,
  params?: {
    bucket?: string
    q?: string
    appointment_type?: string
    source?: string
    revenue_specialist_user_id?: number
    limit?: number
  },
): Promise<AppointmentListResponse> {
  const query = new URLSearchParams()
  if (params?.bucket) query.set('bucket', params.bucket)
  if (params?.q) query.set('q', params.q)
  if (params?.appointment_type) query.set('appointment_type', params.appointment_type)
  if (params?.source) query.set('source', params.source)
  if (params?.revenue_specialist_user_id != null) {
    query.set('revenue_specialist_user_id', String(params.revenue_specialist_user_id))
  }
  if (params?.limit != null) query.set('limit', String(params.limit))
  const qs = query.toString()
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/appointments${qs ? `?${qs}` : ''}`,
  )
  const raw = (await parseJson(response)) as Record<string, unknown>
  const items = Array.isArray(raw.items) ? raw.items : []
  const counts =
    raw.counts && typeof raw.counts === 'object' && !Array.isArray(raw.counts)
      ? (raw.counts as Record<string, number>)
      : {}
  return {
    client_id: asNumber(raw.client_id, clientId),
    bucket: pick(raw, 'bucket'),
    items: items.map((row) => asAppointmentRecord(row as Record<string, unknown>)),
    counts,
    set_count: asNumber(raw.set_count),
  }
}

export async function fetchAppointmentSummary(clientId: number): Promise<AppointmentSummary> {
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/appointments/summary`,
  )
  const raw = (await parseJson(response)) as Record<string, unknown>
  const upcoming = Array.isArray(raw.upcoming) ? raw.upcoming : []
  const today = Array.isArray(raw.today) ? raw.today : []
  return {
    client_id: asNumber(raw.client_id, clientId),
    set_count: asNumber(raw.set_count),
    scheduled_count: asNumber(raw.scheduled_count),
    today_count: asNumber(raw.today_count),
    upcoming_count: asNumber(raw.upcoming_count),
    past_count: asNumber(raw.past_count),
    cancelled_count: asNumber(raw.cancelled_count),
    upcoming: upcoming.map((row) => asAppointmentRecord(row as Record<string, unknown>)),
    today: today.map((row) => asAppointmentRecord(row as Record<string, unknown>)),
  }
}

export async function rescheduleAppointment(
  appointmentId: number,
  body: {
    client_id: number
    appointment_date?: string
    start_time?: string
    timezone?: string
    datetime_tbd?: boolean
    appointment_type?: string
    location_or_link?: string
    revenue_specialist_user_id?: number | null
    notes?: string
    source?: string
  },
): Promise<AppointmentActionResult> {
  const response = await fetch(
    `/api/appointments/${encodeURIComponent(String(appointmentId))}/reschedule`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  const raw = (await parseJson(response)) as Record<string, unknown>
  return {
    ok: asBoolean(raw.ok) || true,
    message: pick(raw, 'message'),
    appointment: asAppointmentRecord((raw.appointment || {}) as Record<string, unknown>),
  }
}

export async function cancelAppointment(
  appointmentId: number,
  body: {
    client_id: number
    reason?: string
    notes?: string
    new_status?: string
    next_action?: string
    follow_up_date?: string | null
    follow_up_time?: string
    created_by?: string
  },
): Promise<AppointmentActionResult> {
  const response = await fetch(
    `/api/appointments/${encodeURIComponent(String(appointmentId))}/cancel`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  const raw = (await parseJson(response)) as Record<string, unknown>
  return {
    ok: asBoolean(raw.ok) || true,
    message: pick(raw, 'message'),
    appointment: asAppointmentRecord((raw.appointment || {}) as Record<string, unknown>),
  }
}

export async function completeAppointment(
  appointmentId: number,
  body: {
    client_id: number
    grade?: string
    outcome?: string
    follow_up_notes?: string
    created_by?: string
  },
): Promise<AppointmentActionResult> {
  const response = await fetch(
    `/api/appointments/${encodeURIComponent(String(appointmentId))}/complete`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  const raw = (await parseJson(response)) as Record<string, unknown>
  return {
    ok: asBoolean(raw.ok) || true,
    message: pick(raw, 'message'),
    appointment: asAppointmentRecord((raw.appointment || {}) as Record<string, unknown>),
  }
}

export async function completeContactFollowUp(
  contactId: number,
  body: { client_id: number; notes?: string; status?: string | null; created_by?: string },
): Promise<ContactWorkflowResult> {
  const response = await fetch(
    `/api/contacts/${encodeURIComponent(String(contactId))}/follow-up/complete`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ContactWorkflowResult
}

export async function rescheduleContactFollowUp(
  contactId: number,
  body: {
    client_id: number
    follow_up_date: string
    follow_up_time: string
    notes?: string
    created_by?: string
  },
): Promise<ContactWorkflowResult> {
  const response = await fetch(
    `/api/contacts/${encodeURIComponent(String(contactId))}/follow-up/reschedule`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return (await parseJson(response)) as ContactWorkflowResult
}

export async function assignSharedContact(
  contactId: number,
  body: ContactAssignRequest,
): Promise<ContactAssignResult> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch(`/api/contacts/${encodeURIComponent(String(contactId))}/assign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ContactAssignResult
}

export type CompanyLookupItem = {
  id: number
  company_name: string
  external_record_no: string
}

export type ManualContactMatch = {
  contact_id: number
  first_name: string
  last_name: string
  title: string
  email: string
  phone: string
  alt_phone: string
  company_id: number
  company_name: string
  reasons: string[]
  confidence: string
  already_assigned: boolean
  same_company: boolean
}

export type ManualContactPreviewResponse = {
  matches: ManualContactMatch[]
  can_create: boolean
  requires_contact_info_confirmation: boolean
  message: string
}

export type ManualContactSaveResult = {
  ok: boolean
  action: string
  message: string
  contact_id: number
  company_id: number
  client_id: number
  activity_id: number | null
  already_assigned: boolean
}

export async function lookupCompaniesForClient(
  clientId: number,
  q = '',
): Promise<{ companies: CompanyLookupItem[] }> {
  const writeClientId = requireWriteClientId(clientId)
  const query = new URLSearchParams({
    client_id: String(writeClientId),
    q,
  })
  const response = await fetch(`/api/companies/lookup?${query.toString()}`)
  return (await parseJson(response)) as { companies: CompanyLookupItem[] }
}

export async function previewManualContact(body: {
  client_id: number
  company_id: number
  first_name: string
  last_name: string
  title?: string
  email?: string
  phone?: string
  alt_phone?: string
}): Promise<ManualContactPreviewResponse> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/contacts/manual/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ManualContactPreviewResponse
}

export async function saveManualContact(body: {
  client_id: number
  company_id: number
  action: 'create' | 'link'
  existing_contact_id?: number | null
  first_name?: string
  last_name?: string
  title?: string
  email?: string
  phone?: string
  alt_phone?: string
  confirm_without_contact_info?: boolean
  created_by?: string
}): Promise<ManualContactSaveResult> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/contacts/manual', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ManualContactSaveResult
}

export type ManualCompanyClientRel = {
  client_id: number
  client_name: string
  status: string
}

export type ManualCompanyMatch = {
  company_id: number
  company_name: string
  external_record_no: string
  website: string
  address: string
  city: string
  state: string
  zip: string
  phone: string
  reasons: string[]
  confidence: string
  already_assigned: boolean
  client_relationships: ManualCompanyClientRel[]
  score: number
  ai_assessment: string
  ai_explanation: string
  ai_confidence: string
  ai_available: boolean
}

export type ManualCompanyPreviewResponse = {
  matches: ManualCompanyMatch[]
  can_create: boolean
  requires_create_confirmation: boolean
  message: string
  ai_available: boolean
}

export type ManualCompanySaveResult = {
  ok: boolean
  action: string
  message: string
  company_id: number
  client_id: number
  external_record_no: string
  activity_id: number | null
  already_assigned: boolean
}

export type ZoomInfoSnapshot = {
  first_name?: string
  last_name?: string
  title?: string
  email?: string
  phone?: string
  alt_phone?: string
  mobile_phone?: string
  company_name?: string
  zoominfo_company_id?: string
  zoominfo_contact_id?: string
  linkedin_url?: string
  location?: string
  website?: string
  address?: string
  city?: string
  state?: string
  zip?: string
  industry?: string
  employee_size?: string
  sales_volume?: string
}

export type ZoomInfoFieldChoice = {
  field: string
  label: string
  northstar_value: string
  zoominfo_value: string
  keep_northstar: boolean
  blank_zoominfo: boolean
  different_company: boolean
  applyable: boolean
}

export type ZoomInfoContactPreviewResponse = {
  available: boolean
  status: string
  contact_id: number
  client_id: number
  company_id: number | null
  company_name: string
  fields: ZoomInfoFieldChoice[]
  different_company: boolean
  zoominfo_company_name: string
  matches: ManualContactMatch[]
  retrieved_at: string
}

export type ZoomInfoContactApplyResult = {
  ok: boolean
  message: string
  contact_id: number
  client_id: number
  company_id: number | null
  changed_fields: string[]
  activity_id: number | null
  company_relinked: boolean
}

export type ZoomInfoAddPreviewResponse = {
  kind: string
  client_id: number
  company_id: number | null
  matches: Array<Record<string, unknown>>
  can_create: boolean
  requires_create_confirmation: boolean
  message: string
}

export type ZoomInfoAddResult = {
  ok: boolean
  action: string
  message: string
  kind: string
  company_id: number | null
  contact_id: number | null
  client_id: number
  external_record_no: string
  activity_id: number | null
}

export async function previewManualCompany(body: {
  client_id: number
  company_name: string
  website?: string
  address?: string
  city?: string
  state?: string
  zip?: string
  phone?: string
  industry?: string
  employee_size?: string
  sales_volume?: string
  external_record_no?: string
  notes?: string
}): Promise<ManualCompanyPreviewResponse> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/companies/manual/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ManualCompanyPreviewResponse
}

export async function saveManualCompany(body: {
  client_id: number
  action: 'create' | 'link'
  existing_company_id?: number | null
  company_name?: string
  website?: string
  address?: string
  city?: string
  state?: string
  zip?: string
  phone?: string
  industry?: string
  employee_size?: string
  sales_volume?: string
  external_record_no?: string
  notes?: string
  confirm_create_despite_match?: boolean
  created_by?: string
}): Promise<ManualCompanySaveResult> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/companies/manual', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ManualCompanySaveResult
}

export async function previewZoomInfoContact(
  contactId: number,
  body: { client_id: number; zoominfo?: ZoomInfoSnapshot | null },
): Promise<ZoomInfoContactPreviewResponse> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch(
    `/api/contacts/${encodeURIComponent(String(contactId))}/zoominfo/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, client_id: writeClientId }),
    },
  )
  return (await parseJson(response)) as ZoomInfoContactPreviewResponse
}

export async function applyZoomInfoContact(
  contactId: number,
  body: {
    client_id: number
    apply_fields: string[]
    zoominfo: ZoomInfoSnapshot
    confirm_company_relink?: boolean
    target_company_id?: number | null
    created_by?: string
  },
): Promise<ZoomInfoContactApplyResult> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch(
    `/api/contacts/${encodeURIComponent(String(contactId))}/zoominfo/apply`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, client_id: writeClientId }),
    },
  )
  return (await parseJson(response)) as ZoomInfoContactApplyResult
}

export async function previewZoomInfoAdd(body: {
  client_id: number
  company_id?: number | null
  zoominfo: ZoomInfoSnapshot
  kind?: 'company' | 'contact'
}): Promise<ZoomInfoAddPreviewResponse> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/zoominfo/add/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ZoomInfoAddPreviewResponse
}

export async function saveZoomInfoAdd(body: {
  client_id: number
  company_id?: number | null
  action: 'create' | 'link'
  existing_company_id?: number | null
  existing_contact_id?: number | null
  zoominfo: ZoomInfoSnapshot
  kind?: 'company' | 'contact'
  confirm_create_despite_match?: boolean
  created_by?: string
}): Promise<ZoomInfoAddResult> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/zoominfo/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return (await parseJson(response)) as ZoomInfoAddResult
}

export async function fetchClientSalesEvents(
  clientId: number,
  params?: {
    company_id?: number
    contact_id?: number
    event_family?: string
    rev_spec?: string
    outcome?: string
    appointment_grade?: string
    date_from?: string
    date_to?: string
    company_name?: string
    contact_name?: string
    limit?: number
  },
): Promise<Array<Record<string, unknown>>> {
  const query = new URLSearchParams()
  if (params?.company_id != null) query.set('company_id', String(params.company_id))
  if (params?.contact_id != null) query.set('contact_id', String(params.contact_id))
  if (params?.event_family) query.set('event_family', params.event_family)
  if (params?.rev_spec) query.set('rev_spec', params.rev_spec)
  if (params?.outcome) query.set('outcome', params.outcome)
  if (params?.appointment_grade) query.set('appointment_grade', params.appointment_grade)
  if (params?.date_from) query.set('date_from', params.date_from)
  if (params?.date_to) query.set('date_to', params.date_to)
  if (params?.company_name) query.set('company_name', params.company_name)
  if (params?.contact_name) query.set('contact_name', params.contact_name)
  if (params?.limit != null) query.set('limit', String(params.limit))
  const qs = query.toString()
  const response = await fetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/sales-events${qs ? `?${qs}` : ''}`,
  )
  return (await parseJson(response)) as Array<Record<string, unknown>>
}

// --- Shared CRM Add / Link (AI Research) ---

export type CrmAddCompanyInput = {
  company_name: string
  website?: string
  address?: string
  city?: string
  state?: string
  zip?: string
  phone?: string
  industry?: string
}

export type CrmAddContactInput = {
  first_name?: string
  last_name?: string
  full_name?: string
  title?: string
  email?: string
  phone?: string
  linkedin?: string
  source_url?: string
}

export type CrmAddPreviewResponse = {
  client_id: number
  client_name: string
  company_match_type: string
  company_confidence: string
  company_match_reasons: string[]
  matched_company_id: number | null
  matched_external_record_no: string
  matched_company_name: string
  proposed_company: CrmAddCompanyInput
  company_field_diffs: Array<{
    field: string
    existing_value: string
    proposed_value: string
    action: string
  }>
  possible_company_matches: Array<Record<string, unknown>>
  contacts: Array<{
    index: number
    first_name: string
    last_name: string
    full_name: string
    title: string
    email: string
    phone: string
    linkedin: string
    source_url: string
    match_status: string
    matched_contact_id: number | null
    matched_contact_name: string
    match_reasons: string[]
    confidence: string
  }>
  relationship: {
    exists: boolean
    relationship_id: number | null
    current_status: string
    would_create: boolean
    status_if_create: string
    message: string
  }
  warnings: string[]
  research_run_id: number | null
  source: string
  provider: string
}

export type CrmAddConfirmResult = {
  ok: boolean
  message: string
  client_id: number
  company_id: number
  external_record_no: string
  company_name: string
  company_created: boolean
  relationship_id: number | null
  relationship_created: boolean
  relationship_status: string
  contacts: Array<Record<string, unknown>>
  activity_id: number | null
  workspace_path: string
}

export async function previewCrmAdd(body: {
  client_id: number
  company: CrmAddCompanyInput
  contacts?: CrmAddContactInput[]
  research_run_id?: number | null
  source?: string
  provider?: string
  known_company_id?: number | null
  trusted_external_record_no?: string | null
}): Promise<CrmAddPreviewResponse> {
  const response = await fetch('/api/crm/add/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return (await parseJson(response)) as CrmAddPreviewResponse
}

export async function confirmCrmAdd(body: {
  client_id: number
  company: CrmAddCompanyInput
  contacts?: Array<{
    contact: CrmAddContactInput
    selected?: boolean
    decision?: string
    matched_contact_id?: number | null
  }>
  company_decision?: string
  existing_company_id?: number | null
  known_company_id?: number | null
  trusted_external_record_no?: string | null
  research_run_id?: number | null
  source?: string
  provider?: string
}): Promise<CrmAddConfirmResult> {
  const response = await fetch('/api/crm/add/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return (await parseJson(response)) as CrmAddConfirmResult
}

export async function fetchCampaigns(params: {
  client_id?: number | null
  status?: string
  owner?: string
  category?: string
  date_from?: string
  date_to?: string
  q?: string
  limit?: number
  offset?: number
}): Promise<CampaignListResponse> {
  const query = new URLSearchParams()
  query.set('client_id', String(params.client_id ?? 0))
  if (params.status?.trim()) query.set('status', params.status.trim())
  if (params.owner?.trim()) query.set('owner', params.owner.trim())
  if (params.category?.trim()) query.set('category', params.category.trim())
  if (params.date_from?.trim()) query.set('date_from', params.date_from.trim())
  if (params.date_to?.trim()) query.set('date_to', params.date_to.trim())
  if (params.q?.trim()) query.set('q', params.q.trim())
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset != null) query.set('offset', String(params.offset))
  const response = await fetch(`/api/campaigns?${query.toString()}`)
  return parseJson<CampaignListResponse>(response)
}

export async function fetchCampaignWorkspace(campaignId: number): Promise<CampaignWorkspace> {
  const response = await fetch(`/api/campaigns/${encodeURIComponent(String(campaignId))}`)
  return parseJson<CampaignWorkspace>(response)
}

export async function createCampaign(body: {
  client_id: number
  campaign_name: string
  description?: string
  category?: string
  owner_name?: string
  status?: string
  start_date?: string
  end_date?: string
  notes?: string
}): Promise<CampaignSummary> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/campaigns', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return parseJson<CampaignSummary>(response)
}

export async function updateCampaign(
  campaignId: number,
  body: {
    campaign_name?: string
    description?: string
    category?: string
    owner_name?: string
    status?: string
    start_date?: string
    end_date?: string
    notes?: string
  },
): Promise<CampaignSummary> {
  const response = await fetch(`/api/campaigns/${encodeURIComponent(String(campaignId))}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson<CampaignSummary>(response)
}

export async function pauseCampaign(campaignId: number): Promise<CampaignSummary> {
  const response = await fetch(`/api/campaigns/${encodeURIComponent(String(campaignId))}/pause`, {
    method: 'POST',
  })
  const raw = await parseJson<{ campaign: CampaignSummary }>(response)
  return raw.campaign
}

export async function resumeCampaign(campaignId: number): Promise<CampaignSummary> {
  const response = await fetch(`/api/campaigns/${encodeURIComponent(String(campaignId))}/resume`, {
    method: 'POST',
  })
  const raw = await parseJson<{ campaign: CampaignSummary }>(response)
  return raw.campaign
}

export async function completeCampaign(campaignId: number): Promise<CampaignSummary> {
  const response = await fetch(
    `/api/campaigns/${encodeURIComponent(String(campaignId))}/complete`,
    { method: 'POST' },
  )
  const raw = await parseJson<{ campaign: CampaignSummary }>(response)
  return raw.campaign
}

export async function archiveCampaign(campaignId: number): Promise<CampaignSummary> {
  const response = await fetch(`/api/campaigns/${encodeURIComponent(String(campaignId))}/archive`, {
    method: 'POST',
  })
  const raw = await parseJson<{ campaign: CampaignSummary }>(response)
  return raw.campaign
}

export async function addCampaignCompany(
  campaignId: number,
  companyId: number,
): Promise<CampaignWorkspace> {
  const response = await fetch(
    `/api/campaigns/${encodeURIComponent(String(campaignId))}/companies`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ company_id: companyId }),
    },
  )
  return parseJson<CampaignWorkspace>(response)
}

export async function addCampaignContact(
  campaignId: number,
  contactId: number,
): Promise<CampaignWorkspace> {
  const response = await fetch(
    `/api/campaigns/${encodeURIComponent(String(campaignId))}/contacts`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ contact_id: contactId }),
    },
  )
  return parseJson<CampaignWorkspace>(response)
}

export async function searchCampaignMembers(
  campaignId: number,
  q: string,
): Promise<CampaignMemberSearchResponse> {
  const query = new URLSearchParams()
  if (q.trim()) query.set('q', q.trim())
  const qs = query.toString()
  const response = await fetch(
    `/api/campaigns/${encodeURIComponent(String(campaignId))}/members/search${qs ? `?${qs}` : ''}`,
  )
  return parseJson<CampaignMemberSearchResponse>(response)
}

export function isCampaignRouteStatus(status: string): boolean {
  const key = (status || '').trim().toLowerCase()
  if (key === 'hot prospect' || key.startsWith('hot prospect')) return true
  if (key === 'qualified' || key.startsWith('qualified ')) return true
  return false
}

export async function fetchCampaignRoute(params: {
  client_id: number
  company_id: number
  contact_id?: number | null
  research_run_id?: number | null
  source?: string
  force?: boolean
}): Promise<CampaignRouteSuggestion> {
  const query = new URLSearchParams()
  query.set('client_id', String(params.client_id))
  query.set('company_id', String(params.company_id))
  if (params.contact_id != null && params.contact_id > 0) {
    query.set('contact_id', String(params.contact_id))
  }
  if (params.research_run_id != null && params.research_run_id > 0) {
    query.set('research_run_id', String(params.research_run_id))
  }
  if (params.source?.trim()) query.set('source', params.source.trim())
  if (params.force) query.set('force', 'true')
  const response = await fetch(`/api/campaigns/route?${query.toString()}`)
  return parseJson<CampaignRouteSuggestion>(response)
}

export async function confirmCampaignRoute(body: {
  client_id: number
  company_id: number
  contact_id?: number | null
  campaign_id: number
  source?: string
}): Promise<CampaignRouteSuggestion> {
  const writeClientId = requireWriteClientId(body.client_id)
  const response = await fetch('/api/campaigns/route/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, client_id: writeClientId }),
  })
  return parseJson<CampaignRouteSuggestion>(response)
}

export async function deferCampaignRoute(body: {
  client_id: number
  company_id: number
  contact_id?: number | null
  source?: string
}): Promise<CampaignRouteSuggestion> {
  const response = await fetch('/api/campaigns/route/defer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson<CampaignRouteSuggestion>(response)
}

export async function fetchUnassignedOpportunities(
  clientId: number | null,
  params?: { q?: string; limit?: number; offset?: number },
): Promise<UnassignedOpportunityList> {
  const query = new URLSearchParams()
  query.set('client_id', String(clientId ?? 0))
  if (params?.q?.trim()) query.set('q', params.q.trim())
  query.set('limit', String(params?.limit ?? 50))
  query.set('offset', String(params?.offset ?? 0))
  const response = await fetch(`/api/campaigns/unassigned?${query.toString()}`)
  return parseJson<UnassignedOpportunityList>(response).then((data) => ({
    ...data,
    items: data.items || [],
    campaigns: data.campaigns || [],
    total: data.total ?? (data.items || []).length,
    limit: data.limit ?? 50,
    offset: data.offset ?? 0,
    q: data.q || '',
  }))
}

export async function bulkAssignUnassigned(body: {
  client_id: number
  campaign_id: number
  company_ids?: number[]
  select_all_matching?: boolean
  exclude_company_ids?: number[]
  q?: string
}): Promise<UnassignedBulkAssignResult> {
  const response = await fetch('/api/campaigns/unassigned/assign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson<UnassignedBulkAssignResult>(response)
}

export async function removeCampaignCompany(
  campaignId: number,
  companyId: number,
): Promise<CampaignWorkspace> {
  const response = await fetch(
    `/api/campaigns/${encodeURIComponent(String(campaignId))}/companies/${encodeURIComponent(String(companyId))}`,
    { method: 'DELETE' },
  )
  return parseJson<CampaignWorkspace>(response)
}

export async function removeCampaignContact(
  campaignId: number,
  contactId: number,
): Promise<CampaignWorkspace> {
  const response = await fetch(
    `/api/campaigns/${encodeURIComponent(String(campaignId))}/contacts/${encodeURIComponent(String(contactId))}`,
    { method: 'DELETE' },
  )
  return parseJson<CampaignWorkspace>(response)
}

export interface ReportQueryParams {
  client_id?: number
  date_from?: string
  date_to?: string
  user_id?: number
  campaign_id?: number
  activity_type?: string
  outcome?: string
  sort_by?: string
  sort_dir?: string
  limit?: number
  offset?: number
  section?: string
  metric?: string
  row_client_id?: number
  row_user_id?: number
  row_campaign_id?: number
  detail?: boolean
}

function reportQueryString(params: ReportQueryParams): string {
  const query = new URLSearchParams()
  if (params.client_id != null && params.client_id > 0) {
    query.set('client_id', String(params.client_id))
  } else {
    query.set('client_id', '0')
  }
  if (params.date_from) query.set('date_from', params.date_from)
  if (params.date_to) query.set('date_to', params.date_to)
  if (params.user_id != null && params.user_id > 0) query.set('user_id', String(params.user_id))
  if (params.campaign_id != null && params.campaign_id > 0) {
    query.set('campaign_id', String(params.campaign_id))
  }
  if (params.activity_type) query.set('activity_type', params.activity_type)
  if (params.outcome) query.set('outcome', params.outcome)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_dir) query.set('sort_dir', params.sort_dir)
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset != null) query.set('offset', String(params.offset))
  if (params.section) query.set('section', params.section)
  if (params.metric) query.set('metric', params.metric)
  if (params.row_client_id != null && params.row_client_id > 0) {
    query.set('row_client_id', String(params.row_client_id))
  }
  if (params.row_user_id != null && params.row_user_id > 0) {
    query.set('row_user_id', String(params.row_user_id))
  }
  if (params.row_campaign_id != null && params.row_campaign_id > 0) {
    query.set('row_campaign_id', String(params.row_campaign_id))
  }
  if (params.detail) query.set('detail', 'true')
  return query.toString()
}

export async function fetchReportFilters(clientId: number): Promise<ReportFiltersResponse> {
  const query = new URLSearchParams()
  query.set('client_id', String(clientId > 0 ? clientId : 0))
  const response = await fetch(`/api/reports/filters?${query.toString()}`)
  return parseJson<ReportFiltersResponse>(response)
}

export async function fetchClientResultsReport(
  params: ReportQueryParams,
): Promise<ReportClientResultsResponse> {
  const response = await fetch(`/api/reports/client-results?${reportQueryString(params)}`)
  return parseJson<ReportClientResultsResponse>(response)
}

export async function fetchTeamPerformanceReport(
  params: ReportQueryParams,
): Promise<ReportTeamResponse> {
  const response = await fetch(`/api/reports/team-performance?${reportQueryString(params)}`)
  return parseJson<ReportTeamResponse>(response)
}

export async function fetchCampaignPerformanceReport(
  params: ReportQueryParams,
): Promise<ReportCampaignResponse> {
  const response = await fetch(`/api/reports/campaign-performance?${reportQueryString(params)}`)
  return parseJson<ReportCampaignResponse>(response)
}

export async function fetchReportRecords(
  params: ReportQueryParams,
): Promise<ReportRecordsResponse> {
  const response = await fetch(`/api/reports/records?${reportQueryString(params)}`)
  return parseJson<ReportRecordsResponse>(response)
}

export async function downloadReportCsv(params: ReportQueryParams): Promise<void> {
  const response = await fetch(`/api/reports/export?${reportQueryString(params)}`)
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `Export failed (${response.status})`)
  }
  const blob = await response.blob()
  const header = response.headers.get('Content-Disposition') || ''
  const match = header.match(/filename="([^"]+)"/)
  const filename = match?.[1] || 'northstar-reports.csv'
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
