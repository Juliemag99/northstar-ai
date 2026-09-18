import { apiFetch } from './http'

export type LeadMasterRefreshBatch = {
  batch_id: number
  client_id: number
  import_mode: string
  source_system: string
  status: string
  original_filename: string
  sha256: string
  headers: string[]
  mapping: Record<string, string>
  suggested_mapping: Record<string, string>
  policy: Record<string, unknown>
  planner_version: string
  total_rows: number
  warnings: string[]
  uploaded_by_name: string
  created_at: string
}

export type LeadMasterRefreshPlan = {
  plan_fingerprint: string
  counts: Record<string, number>
  rows: Array<Record<string, unknown>>
  review_rows: Array<Record<string, unknown>>
  policy?: Record<string, unknown>
  policy_visible?: Record<string, unknown>
  mapping?: Record<string, string>
  schema?: string
}

function requireClient(clientId: number) {
  if (!Number.isInteger(clientId) || clientId <= 0) {
    throw new Error('Choose a specific client before LeadMaster refresh.')
  }
}

export async function fetchLeadMasterRefreshMeta(): Promise<Record<string, unknown>> {
  const resp = await apiFetch('/api/admin/leadmaster-refresh/meta')
  if (resp.status === 403) throw Object.assign(new Error('Administrator access required.'), { status: 403 })
  if (!resp.ok) throw new Error('Could not load LeadMaster refresh metadata.')
  return resp.json()
}

export async function uploadLeadMasterRefresh(
  clientId: number,
  file: File,
): Promise<{ kind: string; message: string; batch: LeadMasterRefreshBatch | null }> {
  requireClient(clientId)
  const body = new FormData()
  body.append('file', file)
  const resp = await apiFetch(`/api/clients/${clientId}/admin/leadmaster-refresh`, {
    method: 'POST',
    body,
  })
  const payload = await resp.json().catch(() => ({}))
  if (resp.status === 403) {
    throw Object.assign(new Error(payload.detail || 'Administrator access required.'), { status: 403 })
  }
  if (resp.status === 409) {
    throw Object.assign(new Error(payload.detail || 'Live refresh writes are disabled.'), { status: 409 })
  }
  if (!resp.ok) throw new Error(payload.detail || 'Upload failed.')
  return payload
}

export async function saveLeadMasterRefreshMapping(
  clientId: number,
  batchId: number,
  mapping: Record<string, string>,
): Promise<LeadMasterRefreshBatch> {
  requireClient(clientId)
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/leadmaster-refresh/${batchId}/mapping`,
    { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mapping }) },
  )
  const payload = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(payload.detail || 'Could not save mapping.')
  return payload
}

export async function saveLeadMasterRefreshPolicy(
  clientId: number,
  batchId: number,
  policy: Record<string, unknown>,
): Promise<LeadMasterRefreshBatch> {
  requireClient(clientId)
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/leadmaster-refresh/${batchId}/policy`,
    { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ policy }) },
  )
  const payload = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(payload.detail || 'Could not save policy.')
  return payload
}

export async function previewLeadMasterRefresh(
  clientId: number,
  batchId: number,
): Promise<LeadMasterRefreshPlan> {
  requireClient(clientId)
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/leadmaster-refresh/${batchId}/preview`,
  )
  const payload = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(payload.detail || 'Could not preview refresh.')
  return payload
}

export async function reviewLeadMasterRefresh(
  clientId: number,
  batchId: number,
): Promise<{ review_rows: Array<Record<string, unknown>>; counts: Record<string, number>; plan_fingerprint: string; policy: Record<string, unknown> }> {
  requireClient(clientId)
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/leadmaster-refresh/${batchId}/review`,
  )
  const payload = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(payload.detail || 'Could not load review rows.')
  return payload
}

export async function recalculateLeadMasterRefresh(
  clientId: number,
  batchId: number,
): Promise<LeadMasterRefreshPlan> {
  requireClient(clientId)
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/leadmaster-refresh/${batchId}/recalculate`,
    { method: 'POST' },
  )
  const payload = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(payload.detail || 'Could not recalculate.')
  return payload
}

export async function confirmLeadMasterRefresh(
  clientId: number,
  batchId: number,
): Promise<never> {
  requireClient(clientId)
  const resp = await apiFetch(
    `/api/clients/${clientId}/admin/leadmaster-refresh/${batchId}/confirm`,
    { method: 'POST' },
  )
  const payload = await resp.json().catch(() => ({}))
  throw Object.assign(new Error(payload.detail || 'Refresh apply is not enabled.'), {
    status: resp.status,
  })
}
