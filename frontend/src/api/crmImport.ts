/** Same-origin CRM import API helpers (Checkpoint B / C4). */

import { apiFetch } from './http'

export const CRM_IMPORT_PLAN_FINGERPRINT_RE = /^[a-f0-9]{64}$/

export type CrmImportRow = {
  row_id: number
  source_row_number: number
  values: Record<string, string>
  warnings: string[]
  errors: string[]
  has_blocking_error: boolean
}

export type CrmImportBatch = {
  batch_id: number
  client_id: number
  status: string
  original_filename: string
  file_type: string
  worksheet_name: string
  file_size_bytes: number
  sha256: string
  headers: string[]
  warnings: string[]
  error_message: string
  total_rows: number
  source_row_count: number
  blank_row_count: number
  error_row_count: number
  reusable: boolean
  expires_at: string
  created_at: string
  updated_at: string
  cancelled_at: string
  uploaded_by_user_id: number | null
  uploaded_by_name: string
  mapping: Record<string, string>
  mapping_updated_at: string
  mapping_updated_by_user_id: number | null
  sample_rows: CrmImportRow[]
}

export type CrmImportUploadResult = {
  kind: string
  needs_worksheet: boolean
  visible_sheets: string[]
  filename: string
  file_type: string
  message: string
  batch: CrmImportBatch | null
}

export type CrmImportRowsPage = {
  batch_id: number
  client_id: number
  offset: number
  limit: number
  total: number
  rows: CrmImportRow[]
}

export type CrmImportMappingRequest = {
  mapping: Record<string, string>
}

export type CrmImportDryRunRequest = {
  offset?: number
  limit?: number
}

export type CrmImportValidity =
  | 'ok'
  | 'blocking_error'
  | 'invalid_mapping_data'

export type CrmImportCompanyAction =
  | 'none'
  | 'create_company'
  | 'use_existing_company'
  | 'possible_company_match'

export type CrmImportContactAction =
  | 'none'
  | 'create_contact'
  | 'use_existing_contact'
  | 'possible_contact_match'
  | 'insufficient_contact_data'
  | 'no_contact_data'
  | 'deferred'

export type CrmImportRelationshipAction =
  | 'none'
  | 'create_client_relationship'
  | 'relationship_already_exists'
  | 'deferred'

export type CrmImportStatusAction =
  | 'none'
  | 'use_default_status'
  | 'preserve_existing_status'
  | 'use_imported_status'
  | 'status_conflict'
  | 'invalid_status'

export type CrmImportNotesAction =
  | 'none'
  | 'no_notes_change'
  | 'set_imported_notes'
  | 'append_imported_notes'
  | 'imported_notes_already_present'

export type CrmImportDryRunCompanyPossible = {
  company_id: number
  company_name: string
  external_record_no: string
  reasons: string[]
}

export type CrmImportDryRunContactPossible = {
  contact_id: number
  display_name: string
  reasons: string[]
}

export type CrmImportDryRunCompanyPlan = {
  action: CrmImportCompanyAction
  reasons: string[]
  company_id: number | null
  proposed_key: string | null
  created_at_source_row: number | null
  name: string
  possibles: CrmImportDryRunCompanyPossible[]
}

export type CrmImportDryRunContactPlan = {
  action: CrmImportContactAction
  reasons: string[]
  contact_id: number | null
  proposed_key: string | null
  created_at_source_row: number | null
  display_name: string
  possibles: CrmImportDryRunContactPossible[]
}

export type CrmImportDryRunRelationshipPlan = {
  action: CrmImportRelationshipAction
  relationship_id: number | null
  proposed_key: string | null
  status_action: CrmImportStatusAction
  notes_action: CrmImportNotesAction
  resolved_status: string
  original_status_action: CrmImportStatusAction | string
  status_resolution_type: string
  existing_status: string
  needs_status_resolution: boolean
}

export type CrmImportDryRunRow = {
  row_id: number
  source_row_number: number
  validity: CrmImportValidity
  validity_detail: string
  mapped: Record<string, string>
  company: CrmImportDryRunCompanyPlan
  contact: CrmImportDryRunContactPlan
  relationship: CrmImportDryRunRelationshipPlan
}

export type CrmImportDryRunCounts = {
  blocking_error: number
  invalid_mapping_data: number
  ok: number
  create_company: number
  use_existing_company: number
  possible_company_match: number
  create_contact: number
  use_existing_contact: number
  possible_contact_match: number
  insufficient_contact_data: number
  no_contact_data: number
  contact_deferred: number
  create_client_relationship: number
  relationship_already_exists: number
  relationship_deferred: number
  use_default_status: number
  preserve_existing_status: number
  use_imported_status: number
  status_conflict: number
  invalid_status: number
  no_notes_change: number
  set_imported_notes: number
  append_imported_notes: number
  imported_notes_already_present: number
  importable_rows: number
  needs_review_rows: number
}

export type CrmImportDryRunResponse = {
  batch_id: number
  client_id: number
  planner_version: string
  plan_fingerprint: string
  mapping_updated_at: string
  total_rows: number
  offset: number
  limit: number
  counts: CrmImportDryRunCounts
  rows: CrmImportDryRunRow[]
  status_catalog: string[]
}

export type CrmImportStatusResolutionRequest = {
  resolution_type?: string | null
  resolved_status?: string | null
  clear?: boolean
}

export type CrmImportStatusResolutionResponse = {
  client_id: number
  batch_id: number
  staged_row_id: number
  cleared: boolean
  resolution: {
    resolution_type: string
    resolved_status: string
    updated_at: string
    updated_by_user_id: number
  } | null
}

export type CrmImportConfirmRequest = {
  confirm: true
  plan_fingerprint: string
}

export type CrmImportConfirmResponse = {
  batch_id: number
  client_id: number
  status: string
  imported_at: string
  imported_by_user_id: number | null
  confirmed_plan_fingerprint: string
  created_company_count: number
  reused_company_count: number
  created_contact_count: number
  reused_contact_count: number
  created_relationship_count: number
  existing_relationship_count: number
  no_contact_row_count: number
  total_imported_row_count: number
  imported_status_count: number
  default_status_count: number
  preserved_status_count: number
  notes_set_count: number
  notes_appended_count: number
  notes_duplicate_count: number
  notes_unchanged_count: number
}

export const CRM_IMPORT_DRY_RUN_MAX_PAGE = 100

export function isValidCrmImportPlanFingerprint(fingerprint: string): boolean {
  return CRM_IMPORT_PLAN_FINGERPRINT_RE.test(String(fingerprint || ''))
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

export async function uploadCrmImport(
  clientId: number,
  file: File,
  worksheet = '',
): Promise<CrmImportUploadResult> {
  requirePositiveIds(clientId)
  const form = new FormData()
  form.append('file', file)
  if (worksheet.trim()) form.append('worksheet', worksheet.trim())
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports`,
    { method: 'POST', body: form },
  )
  return parseJson<CrmImportUploadResult>(response)
}

export async function fetchCrmImportBatch(
  clientId: number,
  batchId: number,
): Promise<CrmImportBatch> {
  requirePositiveIds(clientId, batchId)
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}`,
  )
  return parseJson<CrmImportBatch>(response)
}

export async function fetchCrmImportRows(
  clientId: number,
  batchId: number,
  offset = 0,
  limit = 25,
): Promise<CrmImportRowsPage> {
  requirePositiveIds(clientId, batchId)
  const params = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  })
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}/rows?${params}`,
  )
  return parseJson<CrmImportRowsPage>(response)
}

export async function cancelCrmImport(
  clientId: number,
  batchId: number,
): Promise<CrmImportBatch> {
  requirePositiveIds(clientId, batchId)
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}`,
    { method: 'DELETE' },
  )
  return parseJson<CrmImportBatch>(response)
}

export async function saveCrmImportMapping(
  clientId: number,
  batchId: number,
  mapping: Record<string, string>,
): Promise<CrmImportBatch> {
  requirePositiveIds(clientId, batchId)
  const body: CrmImportMappingRequest = { mapping }
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}/mapping`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return parseJson<CrmImportBatch>(response)
}

export async function dryRunCrmImport(
  clientId: number,
  batchId: number,
  offset = 0,
  limit = CRM_IMPORT_DRY_RUN_MAX_PAGE,
): Promise<CrmImportDryRunResponse> {
  requirePositiveIds(clientId, batchId)
  if (!Number.isFinite(offset) || offset < 0) {
    throw new Error('Invalid paging.')
  }
  if (!Number.isFinite(limit) || limit < 1 || limit > CRM_IMPORT_DRY_RUN_MAX_PAGE) {
    throw new Error('Invalid paging.')
  }
  const body: CrmImportDryRunRequest = { offset, limit }
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}/dry-run`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return parseJson<CrmImportDryRunResponse>(response)
}

export async function confirmCrmImport(
  clientId: number,
  batchId: number,
  planFingerprint: string,
): Promise<CrmImportConfirmResponse> {
  requirePositiveIds(clientId, batchId)
  if (!isValidCrmImportPlanFingerprint(planFingerprint)) {
    throw new Error('Dry-run fingerprint is missing or invalid.')
  }
  const body: CrmImportConfirmRequest = {
    confirm: true,
    plan_fingerprint: planFingerprint,
  }
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}/confirm`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return parseJson<CrmImportConfirmResponse>(response)
}

export async function saveCrmImportStatusResolution(
  clientId: number,
  batchId: number,
  rowId: number,
  body: CrmImportStatusResolutionRequest,
): Promise<CrmImportStatusResolutionResponse> {
  requirePositiveIds(clientId, batchId)
  if (!Number.isFinite(rowId) || rowId <= 0) {
    throw new Error('Import row is missing or invalid.')
  }
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}/rows/${encodeURIComponent(String(rowId))}/status-resolution`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
  return parseJson<CrmImportStatusResolutionResponse>(response)
}
