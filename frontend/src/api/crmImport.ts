import { apiFetch } from './http'

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
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export async function uploadCrmImport(
  clientId: number,
  file: File,
  worksheet = '',
): Promise<CrmImportUploadResult> {
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
  const response = await apiFetch(
    `/api/clients/${encodeURIComponent(String(clientId))}/admin/imports/${encodeURIComponent(String(batchId))}`,
    { method: 'DELETE' },
  )
  return parseJson<CrmImportBatch>(response)
}
