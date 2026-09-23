import { apiFetch } from './http'

export type StewardLinkedClient = {
  client_id: number
  code: string
  name: string
  status: string
  ccr_id: number
  external_record_no?: string
  assigned_rep?: string
  assigned_user_id?: number | null
  is_hot?: boolean
  archived?: boolean
  archived_at?: string
  archive_reason?: string
}

export type StewardCompany = {
  company_id: number
  company_name: string
  address: string
  city: string
  state: string
  zip: string
  website: string
  phone: string
  phone_extension: string
  external_record_no?: string
  updated_at?: string
  archived?: boolean
  archived_at?: string
  archived_by_user_id?: number | null
  archived_by_name?: string
  archive_reason?: string
  linked_clients?: StewardLinkedClient[]
  master_data_warning?: string
}

export type StewardChange = {
  field: string
  current: string
  proposed: string
}

export type StewardCandidate = {
  company_id: number
  company_name: string
  address: string
  city: string
  state: string
  zip: string
  phone: string
  website: string
  reasons: string[]
  severity: 'block' | 'warning'
}

export type StewardPreview = {
  company_id: number
  current: Record<string, string>
  canonical: Record<string, string>
  changes: StewardChange[]
  noop: boolean
  reason: string
  reason_ok: boolean
  linked_clients: StewardLinkedClient[]
  linked_client_count: number
  master_data_warning: string
  collisions: StewardCandidate[]
  blocked: boolean
  preview_fingerprint: string
  expected_updated_at: string
  writes: boolean
}

export type StewardProvenanceEvent = {
  id: number
  entity_type: string
  entity_id: number
  field: string
  old_value: string
  new_value: string
  source_type: string
  changed_by_user_id: number | null
  changed_by_name?: string
  changed_at: string
  action: string
  reason: string
}

export type StewardAmendFields = {
  company_name?: string
  address?: string
  city?: string
  state?: string
  zip?: string
  website?: string
  phone?: string
  phone_extension?: string
  reason: string
  expected_updated_at?: string
  preview_fingerprint?: string
  actor_id?: number
  user_id?: number
  created_by?: string
}

async function parseSteward<T>(response: Response): Promise<T> {
  const text = await response.text()
  let parsed: unknown = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = null
    }
  }
  if (!response.ok) {
    const detail =
      parsed && typeof parsed === 'object' && parsed !== null && 'detail' in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed
    const error = new Error(
      typeof detail === 'string'
        ? detail
        : detail && typeof detail === 'object' && 'message' in detail
          ? String((detail as { message: unknown }).message)
          : `Request failed (${response.status})`,
    ) as Error & { status?: number; detail?: unknown }
    error.status = response.status
    error.detail = detail
    throw error
  }
  return (parsed ?? {}) as T
}

export async function fetchStewardMeta(): Promise<{
  company_amend_enabled: boolean
  archive_enabled: boolean
  company_restore_enabled?: boolean
  delete_enabled: boolean
  merge_enabled: boolean
  remove_relationship_enabled?: boolean
  restore_enabled?: boolean
  message: string
}> {
  const response = await apiFetch('/api/admin/data-steward/meta')
  return parseSteward(response)
}

export async function searchStewardCompanies(
  q: string,
  visibility: 'all' | 'active' | 'archived' = 'all',
): Promise<StewardCompany[]> {
  const query = new URLSearchParams()
  query.set('q', q)
  query.set('visibility', visibility)
  const response = await apiFetch(`/api/admin/data-steward/companies?${query}`)
  const payload = await parseSteward<{ companies?: StewardCompany[] }>(response)
  return payload.companies || []
}

export async function fetchStewardCompany(companyId: number): Promise<StewardCompany> {
  const response = await apiFetch(`/api/admin/data-steward/companies/${companyId}`)
  return parseSteward(response)
}

export async function previewStewardCompanyAmend(
  companyId: number,
  body: StewardAmendFields,
): Promise<StewardPreview> {
  const response = await apiFetch(`/api/admin/data-steward/companies/${companyId}/amend/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseSteward(response)
}

export async function saveStewardCompanyAmend(
  companyId: number,
  body: StewardAmendFields,
): Promise<{ company_id: number; changed: string[]; noop: boolean; reason: string }> {
  const response = await apiFetch(`/api/admin/data-steward/companies/${companyId}/amend`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseSteward(response)
}

export async function fetchStewardProvenance(params: {
  entity_type?: string
  entity_id?: number
  field?: string
  source_type?: string
  limit?: number
}): Promise<{ schema_ready: boolean; events: StewardProvenanceEvent[]; newest_first?: boolean }> {
  const query = new URLSearchParams()
  if (params.entity_type) query.set('entity_type', params.entity_type)
  if (params.entity_id) query.set('entity_id', String(params.entity_id))
  if (params.field) query.set('field', params.field)
  if (params.source_type) query.set('source_type', params.source_type)
  query.set('limit', String(params.limit || 100))
  const response = await apiFetch(`/api/admin/data-steward/provenance?${query}`)
  return parseSteward(response)
}

export type StewardDependency = {
  severity: 'info' | 'warning' | 'block'
  code: string
  message: string
}

export type StewardRelationshipPreview = {
  ccr_id: number
  company_id: number
  company_name: string
  client_id: number
  client_code: string
  client_name: string
  external_record_no?: string
  status: string
  assigned_rep?: string
  is_hot?: boolean
  follow_up_date?: string
  next_action?: string
  contact_count?: number
  activity_count?: number
  history_count?: number
  campaign_count?: number
  other_active_relationships?: number
  archived?: boolean
  archived_at?: string
  archived_by_name?: string
  archive_reason?: string
  warning: string
  effects: string[]
  dependencies: StewardDependency[]
  reason_ok: boolean
  blocked?: boolean
  noop?: boolean
  preview_fingerprint: string
  expected_updated_at?: string
  expected_archived_at?: string
  writes: boolean
}

export type StewardRelationshipEvent = StewardProvenanceEvent & {
  ccr_id?: number
  client_id?: number
  client_name?: string
  action_label?: string
}

export async function fetchStewardRelationshipEvents(
  companyId: number,
): Promise<StewardRelationshipEvent[]> {
  const response = await apiFetch(
    `/api/admin/data-steward/companies/${companyId}/relationship-events`,
  )
  const payload = await parseSteward<{ events?: StewardRelationshipEvent[] }>(response)
  return payload.events || []
}

export async function previewStewardRelationshipAction(
  ccrId: number,
  action: 'remove' | 'restore',
  reason: string,
): Promise<StewardRelationshipPreview> {
  const response = await apiFetch(`/api/admin/data-steward/relationships/${ccrId}/${action}/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  })
  return parseSteward(response)
}

export async function confirmStewardRelationshipAction(
  ccrId: number,
  action: 'remove' | 'restore',
  body: {
    reason: string
    confirm: boolean
    preview_fingerprint?: string
    expected_updated_at?: string
    expected_archived_at?: string
  },
): Promise<{ ccr_id: number; archived?: boolean; noop?: boolean }> {
  const response = await apiFetch(`/api/admin/data-steward/relationships/${ccrId}/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseSteward(response)
}

export type StewardArchivePreview = {
  company_id: number
  company_name: string
  address?: string
  city?: string
  state?: string
  phone?: string
  website?: string
  archived: boolean
  archived_at?: string
  archived_by_name?: string
  archive_reason?: string
  linked_clients: StewardLinkedClient[]
  active_relationships?: Array<{
    ccr_id: number
    client_id: number
    client_code?: string
    client_name?: string
    status?: string
  }>
  contact_count?: number
  alias_count?: number
  location_count?: number
  identity_count?: number
  campaign_count?: number
  history_count?: number
  activity_count?: number
  counts?: Record<string, number>
  dependencies: StewardDependency[]
  warning: string
  instruction?: string
  ccr_warning?: string
  effects: string[]
  reason: string
  reason_ok: boolean
  blocked: boolean
  eligible: boolean
  already_archived?: boolean
  already_active?: boolean
  noop?: boolean
  collisions?: StewardCandidate[]
  preview_fingerprint: string
  expected_updated_at?: string
  expected_archived_at?: string
  writes: boolean
}

export type StewardLifecycleEvent = StewardProvenanceEvent & {
  action_label?: string
}

export async function fetchStewardLifecycleEvents(
  companyId: number,
): Promise<StewardLifecycleEvent[]> {
  const response = await apiFetch(
    `/api/admin/data-steward/companies/${companyId}/lifecycle-events`,
  )
  const payload = await parseSteward<{ events?: StewardLifecycleEvent[] }>(response)
  return payload.events || []
}

export async function previewStewardMasterArchive(
  companyId: number,
  action: 'archive' | 'restore',
  reason: string,
): Promise<StewardArchivePreview> {
  const response = await apiFetch(
    `/api/admin/data-steward/companies/${companyId}/${action}/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    },
  )
  return parseSteward(response)
}

export async function confirmStewardMasterArchive(
  companyId: number,
  action: 'archive' | 'restore',
  body: {
    reason: string
    confirm: boolean
    preview_fingerprint?: string
    expected_updated_at?: string
    expected_archived_at?: string
  },
): Promise<{ company_id: number; archived?: boolean; noop?: boolean; code?: string }> {
  const response = await apiFetch(`/api/admin/data-steward/companies/${companyId}/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseSteward(response)
}
