/** Same-origin Research & Custom Prospect Import API helpers. Confirm stays fail-closed. */

import { apiFetch } from './http'
import type { ColumnDraft, ResearchMappingV3 } from '../researchImportMapping'

export type ResearchImportBatch = {
  batch_id: number
  client_id: number
  batch_name: string
  status: string
  original_filename: string
  file_type: string
  worksheet_name: string
  sha256: string
  research_method: string
  research_date: string
  source_type?: string
  source_label?: string
  source_supplied_by?: string
  is_ai_generated?: number
  mapping_template_id?: number | null
  mapping_template_name?: string
  headers: string[]
  warnings: string[]
  source_row_count: number
  mapping: Record<string, string> | ResearchMappingV3
  suggested_mapping: Record<string, string>
  sample_rows: Record<string, string>[]
  column_map?: ColumnDraft[]
  ignored_headers?: string[]
  custom_attributes?: Array<Record<string, string>>
  mapping_template_suggestion?: {
    template_id?: number
    template_name?: string
    safety_class?: string
    status?: string
    applied?: boolean
    review_required?: boolean
    mapping?: Record<string, string> | ResearchMappingV3
    header_compatibility?: {
      compatible?: boolean
      missing_headers?: string[]
      new_headers?: string[]
      review_required?: boolean
      confidence?: string
    }
  }
  production_confirm_enabled: boolean
  product_name?: string
}

export type ResearchImportUploadResult = {
  kind: string
  needs_worksheet: boolean
  visible_sheets: string[]
  filename: string
  file_type: string
  message: string
  batch: ResearchImportBatch | null
}

export type ResearchPlanRow = {
  row_id: number
  source_row_number: number
  ri_class: string
  matcher_reasons: string[]
  company_name: string
  address: string
  city: string
  state: string
  zip: string
  phone: string
  website: string
  matched_company_id: number | null
  matched_company_name: string
  matched_city: string
  matched_state: string
  matched_address: string
  matched_phone: string
  matched_website: string
  possibles: Array<{
    company_id: number | null
    name: string
    city: string
    state: string
    address: string
    phone: string
    website: string
    reasons: string[]
  }>
  conflicts: Array<{
    field: string
    incoming: string
    current: string
    class: string
    authority?: string
    provenance?: string[]
  }>
  existing_clients: string[]
  relationship_action: string
  planned_status: string
  status_action: string
  research_priority_code: string
  research_priority_label: string
  why_client_fits: string
  qualification_notes: string
  attributes: Record<string, string[]>
  sources: Array<{ source_role: string; source_url: string; field_supported: string }>
  allowed_resolutions: string[]
  blocking: boolean
  blocking_reasons: string[]
  contacts?: Array<{
    incoming?: {
      first_name?: string
      last_name?: string
      full_name?: string
      title?: string
      email?: string
      phone?: string
      phone_extension?: string
    }
    full_name?: string
    first_name?: string
    last_name?: string
    title?: string
    email?: string
    phone?: string
    phone_extension?: string
    contact_class?: string
    matched_contact_id?: number | null
    matched_name?: string
    matched_email?: string
    matched_phone?: string
    matched_title?: string
    matched_company_name?: string
    reasons?: string[]
    allowed_resolutions?: string[]
    resolution?: string
    blocking?: boolean
    blocking_reason?: string
    cross_company?: boolean
  }>
  custom_attributes?: Array<Record<string, string>>
  workflow_fields?: Record<string, string>
  ignored_fields?: string[]
  notes_explicit?: string
  workflow_preview_only?: boolean
  same_batch_key?: string
  same_batch_row_ids?: number[]
  same_batch_conflicts?: Array<Record<string, unknown>>
  research_anchor?: boolean
}

export type ResearchDryRun = {
  batch: ResearchImportBatch
  plan_fingerprint: string
  counts: Record<string, number>
  forecast?: Record<string, number>
  batch_caveat: string
  confirm_allowed: boolean
  production_confirm_enabled: boolean
  blocking: boolean
  ready?: boolean
  blocking_reasons: string[]
  default_status: string
  offset: number
  limit: number
  total_rows: number
  rows: ResearchPlanRow[]
  ignored_headers?: string[]
  workflow_fields_will_write?: number
  contract?: {
    workflow_writes?: number
    production_confirm?: boolean
    source_type_is_authority?: boolean
    notes_require_explicit_mapping?: boolean
  }
}

async function parseJson<T>(resp: Response): Promise<T> {
  const payload = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const detail = typeof payload?.detail === 'string' ? payload.detail : 'Request failed.'
    const err = new Error(detail) as Error & { status?: number }
    err.status = resp.status
    throw err
  }
  return payload as T
}

export async function uploadResearchImport(
  clientId: number,
  file: File,
  extras?: {
    worksheet?: string
    research_method?: string
    research_date?: string
    batch_name?: string
    source_type?: string
    source_label?: string
    source_supplied_by?: string
  },
): Promise<ResearchImportUploadResult> {
  const body = new FormData()
  body.append('file', file)
  if (extras?.worksheet) body.append('worksheet', extras.worksheet)
  if (extras?.research_method) body.append('research_method', extras.research_method)
  if (extras?.research_date) body.append('research_date', extras.research_date)
  if (extras?.batch_name) body.append('batch_name', extras.batch_name)
  if (extras?.source_type) body.append('source_type', extras.source_type)
  if (extras?.source_label) body.append('source_label', extras.source_label)
  if (extras?.source_supplied_by) body.append('source_supplied_by', extras.source_supplied_by)
  const resp = await apiFetch(`/api/clients/${clientId}/admin/research-imports`, {
    method: 'POST',
    body,
  })
  return parseJson(resp)
}

export async function saveResearchImportMapping(
  clientId: number,
  batchId: number,
  mapping: Record<string, string> | ResearchMappingV3,
  extras?: {
    research_method?: string
    research_date?: string
    batch_name?: string
    source_type?: string
    source_label?: string
    source_supplied_by?: string
    mapping_template_id?: number | null
    save_as_template?: string
  },
): Promise<ResearchImportBatch> {
  const resp = await apiFetch(`/api/clients/${clientId}/admin/research-imports/${batchId}/mapping`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mapping, ...extras }),
  })
  return parseJson(resp)
}

export async function dryRunResearchImport(
  clientId: number,
  batchId: number,
  offset = 0,
  limit = 25,
): Promise<ResearchDryRun> {
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/research-imports/${batchId}/dry-run?offset=${offset}&limit=${limit}`,
    { method: 'POST' },
  )
  return parseJson(resp)
}

export async function saveResearchMatchResolution(
  clientId: number,
  batchId: number,
  stagedRowId: number,
  resolutionType: string,
  companyId?: number | null,
): Promise<unknown> {
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/research-imports/${batchId}/match-resolution`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        staged_row_id: stagedRowId,
        resolution_type: resolutionType,
        company_id: companyId ?? null,
      }),
    },
  )
  return parseJson(resp)
}

export async function saveResearchContactResolution(
  clientId: number,
  batchId: number,
  stagedRowId: number,
  resolutionType: string,
  contactId?: number | null,
): Promise<unknown> {
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/research-imports/${batchId}/contact-resolution`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        staged_row_id: stagedRowId,
        resolution_type: resolutionType,
        contact_id: contactId ?? null,
      }),
    },
  )
  return parseJson(resp)
}
