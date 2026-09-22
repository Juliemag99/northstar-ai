import { apiFetch } from './http'

export type StewardLinkedClient = {
  client_id: number
  code: string
  name: string
  status: string
  ccr_id: number
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
  delete_enabled: boolean
  merge_enabled: boolean
  remove_relationship_enabled?: boolean
  message: string
}> {
  const response = await apiFetch('/api/admin/data-steward/meta')
  return parseSteward(response)
}

export async function searchStewardCompanies(q: string): Promise<StewardCompany[]> {
  const query = new URLSearchParams()
  query.set('q', q)
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
