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

export type DuplicateClassification =
  | 'HIGH_CONFIDENCE_DUPLICATE'
  | 'LIKELY_DUPLICATE'
  | 'LIKELY_MULTI_LOCATION'
  | 'LIKELY_NOT_DUPLICATE'
  | 'HUMAN_REVIEW_REQUIRED'
  | 'INSUFFICIENT_EVIDENCE'
  | string

export type DuplicateEvidenceFact = {
  code?: string
  label?: string
  polarity?: string
}

export type DuplicateAutomatedAssessment = {
  assessment_title?: string
  classifier_kind?: string
  external_ai_used?: boolean
  classification?: DuplicateClassification
  confidence?: string
  classifier_version?: string
  classified_at?: string
  stale?: boolean
  persisted?: boolean
  evidence?: DuplicateEvidenceFact[]
  concerns?: DuplicateEvidenceFact[]
  why?: string[]
  concern_labels?: string[]
  human_review_triggers?: string[]
  same_client_ccr_conflict?: boolean
  material_ccr_conflict?: boolean
  proposed_survivor_company_id?: number | null
  proposed_source_company_id?: number | null
  survivor_decision_required?: boolean
  survivor_reasons?: string[]
  survivor_reason?: string
  disagreement?: boolean
  review_needed?: boolean
  review_needed_reasons?: string[]
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
  classification?: DuplicateClassification | ''
  confidence?: string
  classification_stale?: boolean
  auto_proposed_survivor_company_id?: number | null
  survivor_decision_required?: boolean
  disagreement?: boolean
  review_needed?: boolean
  review_needed_reasons?: string[]
  same_client_ccr_conflict?: boolean
  why_summary?: string
  concern_summary?: string
}

export type DuplicateSummary = {
  candidates?: number
  classified?: number
  unclassified?: number
  review_needed?: number
  HIGH_CONFIDENCE_DUPLICATE?: number
  LIKELY_DUPLICATE?: number
  LIKELY_MULTI_LOCATION?: number
  LIKELY_NOT_DUPLICATE?: number
  HUMAN_REVIEW_REQUIRED?: number
  INSUFFICIENT_EVIDENCE?: number
  disagreement?: number
  survivor_decision_required?: number
  stale_human_review?: number
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
  summary?: DuplicateSummary
  queue?: string
  assessment_title?: string
  classifier_kind?: string
  external_ai_used?: boolean
  no_merge_button?: boolean
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
  automated_assessment?: DuplicateAutomatedAssessment
  human_review?: DuplicatePairDetail['review']
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

export type DuplicateAnalyzeResult = {
  ok: boolean
  candidates_analyzed: number
  buckets: Record<string, number>
  inserted?: number
  updated?: number
  reused?: number
  classifier_version?: string
  external_ai_used?: boolean
  human_reviews_mutated?: boolean
  merge_will_occur?: boolean
  approval_created?: boolean
}

export type DuplicateBatchPreview = {
  ok: boolean
  writes: boolean
  action: string
  new_disposition: string
  selected: number
  eligible: number
  excluded: number
  excluded_rows: Array<Record<string, unknown>>
  eligible_rows: Array<Record<string, unknown>>
  confirm_allowed: boolean
  reason_ok: boolean
  preview_fingerprint: string
  batch_merge_candidate?: boolean
}

export type DuplicateBatchConfirm = {
  ok: boolean
  saved: number
  selected: number
  eligible: number
  excluded: number
  new_disposition: string
  merge_will_occur?: boolean
  approval_created?: boolean
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
  queue?: string
  q?: string
  offset?: number
  limit?: number
}): Promise<DuplicateCandidatePage> {
  const query = new URLSearchParams()
  query.set('queue', args.queue || args.disposition || 'review_needed')
  query.set('disposition', args.disposition || args.queue || 'review_needed')
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

export async function analyzeDuplicateCandidates(): Promise<DuplicateAnalyzeResult> {
  const response = await apiFetch('/api/admin/duplicate-review/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  return parseJson(response)
}

export async function previewDuplicateBatchReview(body: {
  action: string
  reason: string
  selection_mode: 'explicit' | 'filtered' | string
  pair_keys?: string[]
  queue?: string
  q?: string
}): Promise<DuplicateBatchPreview> {
  const response = await apiFetch('/api/admin/duplicate-review/batch/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson(response)
}

export async function confirmDuplicateBatchReview(body: {
  action: string
  reason: string
  selection_mode: 'explicit' | 'filtered' | string
  pair_keys?: string[]
  queue?: string
  q?: string
  preview_fingerprint: string
  confirm: boolean
}): Promise<DuplicateBatchConfirm> {
  const response = await apiFetch('/api/admin/duplicate-review/batch/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson(response)
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
