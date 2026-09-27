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
