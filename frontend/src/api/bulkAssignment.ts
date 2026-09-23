import { apiFetch } from './http'

export type BulkAssignee = {
  user_id: number
  email: string
  full_name: string
  is_administrator: boolean
}

export type ProspectAssignmentFilter = {
  client_id: number
  q?: string
  status?: string
  milestone_type?: string
  assigned_user_id?: number | null
}

export type BulkAssignmentPreview = {
  ok: boolean
  writes: boolean
  selection_mode: 'explicit' | 'filtered' | string
  client: { client_id: number; code: string; name: string } | null
  target: { user_id: number; full_name: string; email: string } | null
  reason: string
  filtered_result_count: number | null
  selected_count: number
  eligible: number
  new_assignments: number
  reassignments: number
  already_assigned: number
  inactive_removed: number
  archived_company: number
  invalid_missing: number
  mixed_client: boolean
  unauthorized_target: boolean
  confirm_allowed: boolean
  block_code: string
  hot_count: number
  follow_up_count: number
  appointment_set_count: number
  preview_fingerprint: string
  proposed: string
  current_assignment_summary: Array<{
    assigned_user_id: number | null
    name: string
    n: number
  }>
}

export type BulkAssignmentConfirm = {
  ok: boolean
  selected: number
  eligible: number
  changed: number
  new_assignments: number
  reassigned: number
  already_assigned: number
  skipped: number
  failed: number
  message: string
  target: { user_id: number; full_name: string } | null
}

async function parseBulk<T>(response: Response): Promise<T> {
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

export async function fetchBulkAssignees(clientId: number): Promise<BulkAssignee[]> {
  const response = await apiFetch(
    `/api/admin/bulk-assignment/assignees?client_id=${encodeURIComponent(String(clientId))}`,
  )
  const payload = await parseBulk<{ assignees?: BulkAssignee[] }>(response)
  return Array.isArray(payload.assignees) ? payload.assignees : []
}

export async function previewBulkAssignment(body: {
  selection_mode: 'explicit' | 'filtered'
  client_id?: number
  ccr_ids?: number[]
  filter?: ProspectAssignmentFilter
  target_user_id: number
  reason: string
}): Promise<BulkAssignmentPreview> {
  const response = await apiFetch('/api/admin/bulk-assignment/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseBulk<BulkAssignmentPreview>(response)
}

export async function confirmBulkAssignment(body: {
  selection_mode: 'explicit' | 'filtered'
  client_id?: number
  ccr_ids?: number[]
  filter?: ProspectAssignmentFilter
  target_user_id: number
  reason: string
  preview_fingerprint: string
  confirm: true
}): Promise<BulkAssignmentConfirm> {
  const response = await apiFetch('/api/admin/bulk-assignment/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseBulk<BulkAssignmentConfirm>(response)
}
