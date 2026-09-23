import { apiFetch } from './http'

export type DuplicateDisposition =
  | 'UNREVIEWED'
  | 'LIKELY_DUPLICATE'
  | 'NOT_DUPLICATE'
  | 'MULTI_LOCATION'
  | 'NEEDS_RESEARCH'
  | 'MERGE_CANDIDATE'
  | string

export type DuplicateListCompany = {
  company_id: number
  company_name: string
  city?: string
  state?: string
  phone?: string
  website?: string
  external_record_no?: string
  archived?: boolean
  address?: string
  zip?: string
}

export type DuplicateCandidate = {
  company_a_id: number
  company_b_id: number
  pair_key: string
  company_a: DuplicateListCompany
  company_b: DuplicateListCompany
  categories: string[]
  evidence_summary: string
  disposition: DuplicateDisposition
  review_status: string
  stale: boolean
  reviewed_at?: string
  review_reason?: string
  proposed_survivor_company_id?: number | null
  proposed_source_company_id?: number | null
  planning_only: boolean
  merge_will_occur: boolean
}

export type DuplicateCandidatePage = {
  planning_only: boolean
  merge_will_occur: boolean
  automatic_verdict: boolean
  writes: boolean
  total: number
  offset: number
  limit: number
  pairs: DuplicateCandidate[]
}

export type DuplicateCcr = {
  ccr_id: number
  client_id: number
  client_code?: string
  client_name?: string
  status?: string
  assigned_user_name?: string
  assigned_user_id?: number | null
  hot?: boolean
  follow_up_date?: string | null
  next_action?: string
  external_record_no?: string
  active?: boolean
  notes?: string
}

export type DuplicateCompanyDetail = {
  company_id: number
  company_name: string
  address?: string
  city?: string
  state?: string
  zip?: string
  phone?: string
  phone_extension?: string
  website?: string
  external_record_no?: string
  archived?: boolean
  archived_at?: string
  created_at?: string
  updated_at?: string
  ccrs: DuplicateCcr[]
  aliases: Array<Record<string, unknown>>
  identities: Array<Record<string, unknown>>
  locations: Array<Record<string, unknown>>
  contacts: Array<Record<string, unknown>>
  campaigns: Array<Record<string, unknown>>
  history: Record<string, number>
  planning_factors: Record<string, unknown>
}

export type DuplicatePairDetail = {
  planning_only: boolean
  merge_will_occur: boolean
  automatic_verdict: boolean
  writes: boolean
  pair_key: string
  company_a: DuplicateCompanyDetail
  company_b: DuplicateCompanyDetail
  categories: string[]
  evidence_summary: string
  same_client_conflicts: Array<Record<string, unknown>>
  contact_overlaps: Array<Record<string, unknown>>
  dependency: { overall: string; labels: string[] }
  merge_plan: Record<string, unknown> | null
  review: {
    disposition: DuplicateDisposition
    reason: string
    reviewed_at?: string
    stale: boolean
    review_status: string
    proposed_survivor_company_id?: number | null
    proposed_source_company_id?: number | null
    evidence_fingerprint: string
  }
  plan_only_warning: string
  no_merge_button: boolean
}

export type DuplicateReviewSave = {
  ok: boolean
  planning_only: boolean
  merge_will_occur: boolean
  approval_created: boolean
  plan_only_warning: string
  disposition: DuplicateDisposition
  reason: string
  proposed_survivor_company_id?: number | null
  proposed_source_company_id?: number | null
}

async function parseJson<T>(response: Response): Promise<T> {
  const parsed = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail =
      parsed && typeof parsed === 'object' && parsed !== null && 'detail' in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed
    const message =
      typeof detail === 'string'
        ? detail
        : detail && typeof detail === 'object' && detail !== null && 'message' in detail
          ? String((detail as { message: unknown }).message)
          : `Request failed (${response.status})`
    const error = new Error(message) as Error & { status?: number; detail?: unknown }
    error.status = response.status
    error.detail = detail
    throw error
  }
  return parsed as T
}

export async function fetchDuplicateCandidates(args: {
  disposition?: string
  q?: string
  offset?: number
  limit?: number
}): Promise<DuplicateCandidatePage> {
  const query = new URLSearchParams()
  query.set('disposition', args.disposition || 'unreviewed')
  query.set('q', args.q || '')
  query.set('offset', String(args.offset ?? 0))
  query.set('limit', String(args.limit ?? 50))
  const response = await apiFetch(`/api/admin/duplicate-review/candidates?${query}`)
  return parseJson(response)
}

export async function fetchDuplicatePair(
  companyAId: number,
  companyBId: number,
  planningSurvivorId?: number,
): Promise<DuplicatePairDetail> {
  const query = new URLSearchParams()
  if (planningSurvivorId) query.set('planning_survivor_company_id', String(planningSurvivorId))
  const suffix = query.toString() ? `?${query}` : ''
  const response = await apiFetch(
    `/api/admin/duplicate-review/pairs/${companyAId}/${companyBId}${suffix}`,
  )
  return parseJson(response)
}

export async function fetchDuplicateReviewHistory(companyAId: number, companyBId: number) {
  const response = await apiFetch(
    `/api/admin/duplicate-review/pairs/${companyAId}/${companyBId}/history`,
  )
  return parseJson<{ events: Array<Record<string, unknown>> }>(response)
}

export async function saveDuplicateReview(
  companyAId: number,
  companyBId: number,
  body: {
    disposition: string
    reason: string
    proposed_survivor_company_id?: number | null
    proposed_source_company_id?: number | null
  },
): Promise<DuplicateReviewSave> {
  const response = await apiFetch(`/api/admin/duplicate-review/pairs/${companyAId}/${companyBId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson(response)
}
