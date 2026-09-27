import { apiFetch } from './http'

export type LoginStatus = 'inactive' | 'no_password' | 'locked' | 'can_sign_in'
export type AccessScope = 'all_clients' | 'assigned'

export type AdminUserClient = {
  client_id: number
  client_code: string
  client_name: string
}

export type AdminUserAssignment = AdminUserClient & {
  active: boolean
}

export type CrmOwnershipCounts = {
  client_company_relationships: number
  contact_client_workflows: number
  open_work_queue_items: number
}

export type AdminUserSummary = {
  id: number
  full_name: string
  email: string
  staff_role: string
  is_administrator: boolean
  active: boolean
  is_internal_northstar: boolean
  created_at: string
  password_updated_at: string
  login_status: LoginStatus
  access_scope: AccessScope
  clients: AdminUserClient[]
}

export type AdminUserDetail = AdminUserSummary & {
  assignments: AdminUserAssignment[]
  crm_ownership: CrmOwnershipCounts
}

const LOGIN_STATUSES = new Set<LoginStatus>([
  'inactive',
  'no_password',
  'locked',
  'can_sign_in',
])

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function text(value: unknown): string {
  return value == null ? '' : String(value)
}

function parseClient(value: unknown): AdminUserClient | null {
  const raw = asRecord(value)
  const clientId = Number(raw.client_id)
  if (!Number.isFinite(clientId) || clientId <= 0) return null
  return {
    client_id: clientId,
    client_code: text(raw.client_code),
    client_name: text(raw.client_name),
  }
}

function parseSummary(value: unknown): AdminUserSummary | null {
  const raw = asRecord(value)
  const id = Number(raw.id)
  if (!Number.isFinite(id) || id <= 0) return null
  const loginStatus = text(raw.login_status) as LoginStatus
  const accessScope = text(raw.access_scope) === 'all_clients' ? 'all_clients' : 'assigned'
  const clients = Array.isArray(raw.clients)
    ? raw.clients.map(parseClient).filter((item): item is AdminUserClient => item != null)
    : []
  return {
    id,
    full_name: text(raw.full_name),
    email: text(raw.email),
    staff_role: text(raw.staff_role),
    is_administrator: raw.is_administrator === true,
    active: raw.active === true,
    is_internal_northstar: raw.is_internal_northstar === true,
    created_at: text(raw.created_at),
    password_updated_at: text(raw.password_updated_at),
    login_status: LOGIN_STATUSES.has(loginStatus) ? loginStatus : 'inactive',
    access_scope: accessScope,
    clients,
  }
}

function parseDetail(value: unknown): AdminUserDetail {
  const summary = parseSummary(value)
  if (summary == null) {
    throw new Error('Unable to load this user.')
  }
  const raw = asRecord(value)
  const assignments = Array.isArray(raw.assignments)
    ? raw.assignments
        .map((item) => {
          const client = parseClient(item)
          if (client == null) return null
          return { ...client, active: asRecord(item).active === true }
        })
        .filter((item): item is AdminUserAssignment => item != null)
    : []
  const ownership = asRecord(raw.crm_ownership)
  return {
    ...summary,
    assignments,
    crm_ownership: {
      client_company_relationships: Number(ownership.client_company_relationships) || 0,
      contact_client_workflows: Number(ownership.contact_client_workflows) || 0,
      open_work_queue_items: Number(ownership.open_work_queue_items) || 0,
    },
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export async function fetchAdminUsers(): Promise<AdminUserSummary[]> {
  const response = await apiFetch('/api/admin/users')
  if (!response.ok) {
    throw new Error('Unable to load users.')
  }
  const payload = asRecord(await readJson(response))
  const rows = Array.isArray(payload.users) ? payload.users : []
  return rows
    .map(parseSummary)
    .filter((item): item is AdminUserSummary => item != null)
}

export type CreateStaffUserInput = {
  full_name: string
  email: string
  staff_role: string
  client_ids: number[]
  active: boolean
  password: string
}

export async function createAdminUser(input: CreateStaffUserInput): Promise<AdminUserDetail> {
  const response = await apiFetch('/api/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const payload = await readJson(response)
  if (!response.ok) {
    const detail = asRecord(payload).detail
    throw new Error(typeof detail === 'string' && detail.trim() ? detail : 'Unable to create this user.')
  }
  return parseDetail(payload)
}

export type UpdateStaffUserInput = {
  full_name: string
  email: string
  staff_role: string
  is_administrator: boolean
  active: boolean
}

export type AdminUserUpdateResult = {
  user: AdminUserDetail
  sessions_revoked: number
}

export async function updateAdminUser(
  userId: number,
  input: UpdateStaffUserInput,
): Promise<AdminUserUpdateResult> {
  const response = await apiFetch(`/api/admin/users/${userId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const payload = await readJson(response)
  if (!response.ok) {
    const detail = asRecord(payload).detail
    throw new Error(typeof detail === 'string' && detail.trim() ? detail : 'Unable to update this user.')
  }
  const sessions = Number(asRecord(payload).sessions_revoked)
  return {
    user: parseDetail(payload),
    sessions_revoked: Number.isFinite(sessions) ? sessions : 0,
  }
}

export async function replaceAdminUserClients(
  userId: number,
  clientIds: number[],
): Promise<AdminUserDetail> {
  const response = await apiFetch(`/api/admin/users/${userId}/clients`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_ids: clientIds }),
  })
  const payload = await readJson(response)
  if (!response.ok) {
    const detail = asRecord(payload).detail
    throw new Error(
      typeof detail === 'string' && detail.trim() ? detail : 'Unable to update client access.',
    )
  }
  return parseDetail(payload)
}

export type PasswordResetResult = {
  user_id: number
  has_password: boolean
  password_updated_at: string
  sessions_revoked: number
}

export async function resetAdminUserPassword(
  userId: number,
  password: string,
): Promise<PasswordResetResult> {
  const response = await apiFetch(`/api/admin/users/${userId}/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  const payload = await readJson(response)
  if (!response.ok) {
    const detail = asRecord(payload).detail
    throw new Error(typeof detail === 'string' && detail.trim() ? detail : 'Unable to update this password.')
  }
  const raw = asRecord(payload)
  const sessions = Number(raw.sessions_revoked)
  return {
    user_id: Number(raw.user_id) || userId,
    has_password: raw.has_password === true,
    password_updated_at: text(raw.password_updated_at),
    sessions_revoked: Number.isFinite(sessions) ? sessions : 0,
  }
}

export async function fetchAdminUser(userId: number): Promise<AdminUserDetail> {
  const response = await apiFetch(`/api/admin/users/${userId}`)
  if (response.status === 404) {
    throw new Error('User not found.')
  }
  if (!response.ok) {
    throw new Error('Unable to load this user.')
  }
  return parseDetail(await readJson(response))
}

const SAFE_HISTORY_KEYS = new Set([
  'old',
  'new',
  'full_name',
  'email',
  'staff_role',
  'active',
  'is_administrator',
  'is_internal_northstar',
  'client_id',
  'client_ids',
  'client_code',
  'client_name',
  'clients',
  'sessions_revoked',
  'password_updated_at',
])

function secretHistoryKey(key: string): boolean {
  const lowered = key.trim().toLowerCase().replace(/-/g, '_')
  if (lowered === 'password_updated_at') return false
  return (
    lowered.includes('password') ||
    lowered.includes('token') ||
    lowered.includes('csrf') ||
    lowered.includes('secret') ||
    lowered.includes('credential') ||
    lowered.includes('cookie') ||
    lowered.includes('hash')
  )
}

function sanitizeHistoryValue(value: unknown, depth = 0): unknown {
  if (depth > 6 || value == null) return undefined
  if (typeof value === 'boolean' || typeof value === 'number') return value
  if (typeof value === 'string') {
    if (value.toLowerCase().includes('$argon2') || value.toLowerCase().includes('password_hash')) {
      return undefined
    }
    return value
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => sanitizeHistoryValue(item, depth + 1))
      .filter((item) => item !== undefined)
  }
  if (typeof value === 'object') return sanitizeHistoryRecord(value, depth + 1)
  return undefined
}

function sanitizeHistoryRecord(value: unknown, depth = 0): Record<string, unknown> {
  const raw = asRecord(value)
  const cleaned: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(raw)) {
    if (!SAFE_HISTORY_KEYS.has(key) || secretHistoryKey(key)) continue
    const safe = sanitizeHistoryValue(item, depth)
    if (safe === undefined) continue
    if (Array.isArray(safe) && safe.length === 0) continue
    if (typeof safe === 'object' && safe && !Array.isArray(safe) && Object.keys(safe).length === 0) {
      continue
    }
    cleaned[key] = safe
  }
  return cleaned
}

export type AdminHistoryClient = {
  client_id: number
  client_code: string
  client_name: string
}

export type AdminHistoryEvent = {
  id: number
  event_type: string
  created_at: string
  actor_user_id: number | null
  actor_full_name: string
  actor_email: string
  target_user_id: number
  detail_available: boolean
  detail: Record<string, unknown>
}

export type AdminHistory = {
  user_id: number
  events: AdminHistoryEvent[]
}

function parseHistoryEvent(value: unknown): AdminHistoryEvent | null {
  const raw = asRecord(value)
  const id = Number(raw.id)
  if (!Number.isFinite(id) || id <= 0) return null
  const actorId = Number(raw.actor_user_id)
  return {
    id,
    event_type: text(raw.event_type),
    created_at: text(raw.created_at),
    actor_user_id: Number.isFinite(actorId) && actorId > 0 ? actorId : null,
    actor_full_name: text(raw.actor_full_name),
    actor_email: text(raw.actor_email),
    target_user_id: Number(raw.target_user_id) || 0,
    detail_available: raw.detail_available !== false,
    detail: sanitizeHistoryRecord(raw.detail),
  }
}

export async function fetchAdminUserHistory(userId: number): Promise<AdminHistory> {
  const response = await apiFetch(`/api/admin/users/${userId}/history`)
  if (response.status === 404) {
    throw new Error('User not found.')
  }
  if (!response.ok) {
    throw new Error('Unable to load administration history.')
  }
  const payload = asRecord(await readJson(response))
  const events = Array.isArray(payload.events)
    ? payload.events
        .map(parseHistoryEvent)
        .filter((item): item is AdminHistoryEvent => item != null)
    : []
  events.sort((left, right) => {
    if (left.created_at !== right.created_at) return left.created_at < right.created_at ? 1 : -1
    return right.id - left.id
  })
  return { user_id: Number(payload.user_id) || userId, events }
}
