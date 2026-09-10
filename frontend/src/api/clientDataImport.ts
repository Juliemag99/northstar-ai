/** Same-origin Client Data Import API helpers. */

import { apiFetch } from './http'

export const CLIENT_DATA_IMPORT_PLAN_FINGERPRINT_RE = /^[a-f0-9]{64}$/

export type ClientDataImportBatch = {
  id?: number
  batch_id?: number
  client_id: number
  status: string
  original_filename?: string
  history_filename?: string
  file_type?: string
  worksheet_name?: string
  file_size_bytes?: number
  sha256?: string
  headers?: string[]
  history_headers?: string[]
  warnings?: string[]
  error_message?: string
  total_rows?: number
  source_row_count?: number
  blank_row_count?: number
  error_row_count?: number
  history_row_count?: number
  reusable?: boolean
  expires_at?: string
  created_at?: string
  updated_at?: string
  cancelled_at?: string
  uploaded_by_user_id?: number | null
  uploaded_by_name?: string
  mapping?: Record<string, string>
  prospects_mapping?: Record<string, string>
  history_mapping?: Record<string, string>
  mapping_updated_at?: string
  mapping_updated_by_user_id?: number | null
  closed_policy?: string
  closed_policy_note?: string
  closed_policy_notes?: string
  sample_rows?: Array<Record<string, unknown>>
  history_sample_rows?: Array<Record<string, unknown>>
  staging_cleanup_status?: string
  staging_cleanup_error?: string
}

export type ClientDataImportUploadResult = {
  kind?: string
  needs_worksheet?: boolean
  visible_sheets?: string[]
  filename?: string
  file_type?: string
  message?: string
  batch?: ClientDataImportBatch | null
}

export type ClientDataImportMappingRequest = {
  prospects_mapping: Record<string, string>
  history_mapping?: Record<string, string>
}

export type ClientDataImportProspectsCounts = {
  companies_create?: number
  companies_reuse?: number
  companies_possible?: number
  contacts_create?: number
  contacts_reuse?: number
  contacts_possible?: number
  relationships_create?: number
  relationships_existing?: number
  statuses_imported?: number
  statuses_conflicting?: number
  statuses_invalid?: number
  notes_set?: number
  notes_appended?: number
  notes_duplicate?: number
  blocking?: number
  needs_review?: number
  possible_company_match?: number
  possible_contact_match?: number
  create_company?: number
  use_existing_company?: number
  possible_company_match_count?: number
  create_contact?: number
  use_existing_contact?: number
  create_client_relationship?: number
  relationship_already_exists?: number
  use_imported_status?: number
  status_conflict?: number
  invalid_status?: number
  set_imported_notes?: number
  append_imported_notes?: number
  imported_notes_already_present?: number
  blocking_error?: number
  needs_review_rows?: number
  [key: string]: number | undefined
}

export type ClientDataImportHistoryCounts = {
  insert?: number
  already_present?: number
  invalid?: number
  unresolved?: number
  [key: string]: number | undefined
}

export type ClientDataImportDryRunResponse = {
  id?: number
  batch_id?: number
  client_id?: number
  plan_fingerprint: string
  status?: string
  status_catalog?: string[]
  closed_excluded_count?: number
  closed_policy?: string
  closed_policy_note?: string
  closed_policy_notes?: string
  confirm_allowed?: boolean
  prospects?: ClientDataImportProspectsCounts
  history?: ClientDataImportHistoryCounts
  counts?: ClientDataImportProspectsCounts & ClientDataImportHistoryCounts
  rows?: Array<Record<string, unknown>>
  sample_rows?: Array<Record<string, unknown>>
  total_rows?: number
  [key: string]: unknown
}

export type ClientDataImportConfirmResponse = {
  id?: number
  batch_id?: number
  client_id?: number
  status?: string
  imported_at?: string
  imported_by_user_id?: number | null
  confirmed_plan_fingerprint?: string
  companies_created?: number
  companies_reused?: number
  contacts_created?: number
  contacts_reused?: number
  relationships_created?: number
  relationships_existing?: number
  statuses_imported?: number
  notes_set?: number
  notes_appended?: number
  notes_duplicate?: number
  history_inserted?: number
  history_already_present?: number
  closed_excluded_count?: number
  created_company_count?: number
  reused_company_count?: number
  created_contact_count?: number
  reused_contact_count?: number
  created_relationship_count?: number
  existing_relationship_count?: number
  staging_cleanup_status?: string
  staging_cleanup_error?: string
  audit?: Record<string, unknown>
  [key: string]: unknown
}

export function batchIdOf(batch: Pick<ClientDataImportBatch, 'id' | 'batch_id'>): number {
  const id = batch.id ?? batch.batch_id
  return typeof id === 'number' && Number.isFinite(id) ? id : 0
}

export function prospectsMappingOf(batch: ClientDataImportBatch): Record<string, string> {
  return { ...(batch.prospects_mapping || batch.mapping || {}) }
}

export function historyMappingOf(batch: ClientDataImportBatch): Record<string, string> {
  return { ...(batch.history_mapping || {}) }
}

export function isValidClientDataImportPlanFingerprint(fingerprint: string): boolean {
  return CLIENT_DATA_IMPORT_PLAN_FINGERPRINT_RE.test(String(fingerprint || ''))
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text()
    let message = detail || `Request failed (${response.status})`
    try {
      const parsed = JSON.parse(detail) as { detail?: unknown }
      if (typeof parsed.detail === 'string' && parsed.detail.trim()) {
        message = parsed.detail
      }
    } catch {
      /* keep raw text */
    }
    const error = new Error(message) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  return response.json() as Promise<T>
}

function requirePositiveIds(clientId: number, batchId?: number): void {
  if (!Number.isFinite(clientId) || clientId <= 0) {
    throw new Error('Choose a specific client before continuing.')
  }
  if (batchId !== undefined && (!Number.isFinite(batchId) || batchId <= 0)) {
    throw new Error('Import batch is missing or invalid.')
  }
}

function basePath(clientId: number, batchId?: number): string {
  const root = `/api/clients/${encodeURIComponent(String(clientId))}/admin/client-data-imports`
  if (batchId === undefined) return root
  return `${root}/${encodeURIComponent(String(batchId))}`
}

export async function uploadClientDataImport(
  clientId: number,
  prospectsFile?: File | null,
  historyFile?: File | null,
  worksheet = '',
): Promise<ClientDataImportUploadResult> {
  requirePositiveIds(clientId)
  if (!prospectsFile && !historyFile) {
    throw new Error('Upload a prospects spreadsheet and/or a history CSV.')
  }
  const form = new FormData()
  if (prospectsFile) form.append('prospects_file', prospectsFile)
  if (historyFile) form.append('history_file', historyFile)
  if (worksheet.trim()) form.append('worksheet', worksheet.trim())
  const response = await apiFetch(basePath(clientId), { method: 'POST', body: form })
  return parseJson<ClientDataImportUploadResult>(response)
}

export async function fetchClientDataImportBatch(
  clientId: number,
  batchId: number,
): Promise<ClientDataImportBatch> {
  requirePositiveIds(clientId, batchId)
  const response = await apiFetch(basePath(clientId, batchId))
  return parseJson<ClientDataImportBatch>(response)
}

export async function cancelClientDataImport(
  clientId: number,
  batchId: number,
): Promise<ClientDataImportBatch> {
  requirePositiveIds(clientId, batchId)
  const response = await apiFetch(basePath(clientId, batchId), { method: 'DELETE' })
  return parseJson<ClientDataImportBatch>(response)
}

export async function saveClientDataImportMapping(
  clientId: number,
  batchId: number,
  prospectsMapping: Record<string, string>,
  historyMapping?: Record<string, string>,
): Promise<ClientDataImportBatch> {
  requirePositiveIds(clientId, batchId)
  const body: ClientDataImportMappingRequest = {
    prospects_mapping: prospectsMapping,
  }
  if (historyMapping && Object.keys(historyMapping).length > 0) {
    body.history_mapping = historyMapping
  }
  const response = await apiFetch(`${basePath(clientId, batchId)}/mapping`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson<ClientDataImportBatch>(response)
}

export async function dryRunClientDataImport(
  clientId: number,
  batchId: number,
): Promise<ClientDataImportDryRunResponse> {
  requirePositiveIds(clientId, batchId)
  const response = await apiFetch(`${basePath(clientId, batchId)}/dry-run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  return parseJson<ClientDataImportDryRunResponse>(response)
}

export async function confirmClientDataImport(
  clientId: number,
  batchId: number,
  planFingerprint: string,
): Promise<ClientDataImportConfirmResponse> {
  requirePositiveIds(clientId, batchId)
  if (!isValidClientDataImportPlanFingerprint(planFingerprint)) {
    throw new Error('Dry-run fingerprint is missing or invalid.')
  }
  const response = await apiFetch(`${basePath(clientId, batchId)}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      confirm: true,
      plan_fingerprint: planFingerprint,
    }),
  })
  return parseJson<ClientDataImportConfirmResponse>(response)
}

export async function retryClientDataImportStagingCleanup(
  clientId: number,
  batchId: number,
): Promise<ClientDataImportBatch | ClientDataImportConfirmResponse> {
  requirePositiveIds(clientId, batchId)
  const response = await apiFetch(`${basePath(clientId, batchId)}/retry-staging-cleanup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  return parseJson<ClientDataImportBatch | ClientDataImportConfirmResponse>(response)
}

export function countValue(
  source: Record<string, number | undefined> | undefined,
  ...keys: string[]
): number {
  if (!source) return 0
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return 0
}

export function prospectsCountsOf(
  plan: ClientDataImportDryRunResponse | null,
): ClientDataImportProspectsCounts {
  if (!plan) return {}
  return { ...(plan.counts || {}), ...(plan.prospects || {}) }
}

export function historyCountsOf(
  plan: ClientDataImportDryRunResponse | null,
): ClientDataImportHistoryCounts {
  if (!plan) return {}
  const flat = plan.counts || {}
  return {
    insert: flat.insert,
    already_present: flat.already_present,
    invalid: flat.invalid,
    unresolved: flat.unresolved,
    ...(plan.history || {}),
  }
}

/** Confirm is allowed only when the API says so (or all conflict counters are zero). */
export function planReadyToConfirm(plan: ClientDataImportDryRunResponse | null): boolean {
  if (!plan) return false
  if (!isValidClientDataImportPlanFingerprint(plan.plan_fingerprint)) return false
  if (plan.confirm_allowed === true) return true
  if (plan.confirm_allowed === false) return false

  const prospects = prospectsCountsOf(plan)
  const history = historyCountsOf(plan)
  const blocking = countValue(prospects, 'blocking', 'blocking_error')
  const possible =
    countValue(prospects, 'companies_possible', 'possible_company_match', 'possible_company_match_count') +
    countValue(prospects, 'contacts_possible', 'possible_contact_match')
  const invalid =
    countValue(prospects, 'statuses_invalid', 'invalid_status') + countValue(history, 'invalid')
  const unresolved = countValue(history, 'unresolved')
  const statusConflict = countValue(prospects, 'statuses_conflicting', 'status_conflict')
  return blocking + possible + invalid + unresolved + statusConflict === 0
}
