import { apiFetch, clearCsrfToken, setCsrfToken } from './http'

export const GENERIC_LOGIN_ERROR = 'Invalid email or password.'

export type StaffUser = {
  id: number
  email: string
  full_name: string
  is_administrator: boolean
  is_internal_northstar: boolean
  active: boolean
  created_at: string
}

export type AuthSnapshot = {
  authenticated: boolean
  user: StaffUser | null
  authAvailable: boolean
  authEnforced: boolean
}

export type ParsedAuth = AuthSnapshot & {
  csrfToken: string
}

export type LogoutAttempt =
  | { ok: true; parsed: ParsedAuth }
  | { ok: false }

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function parseUser(value: unknown): StaffUser | null {
  const raw = asRecord(value)
  const id = Number(raw.id)
  if (!Number.isFinite(id) || id <= 0) return null
  return {
    id,
    email: String(raw.email ?? ''),
    full_name: String(raw.full_name ?? ''),
    is_administrator: Boolean(raw.is_administrator),
    is_internal_northstar: Boolean(raw.is_internal_northstar),
    active: raw.active !== false,
    created_at: String(raw.created_at ?? ''),
  }
}

function parseAuthPayload(payload: Record<string, unknown>): ParsedAuth {
  if (
    typeof payload.authenticated !== 'boolean' ||
    typeof payload.auth_available !== 'boolean' ||
    typeof payload.auth_enforced !== 'boolean'
  ) {
    throw new Error('Unable to load the current session.')
  }
  const rawCsrf = typeof payload.csrf_token === 'string' ? payload.csrf_token.trim() : ''
  return {
    authenticated: payload.authenticated,
    user: parseUser(payload.user),
    authAvailable: payload.auth_available,
    authEnforced: payload.auth_enforced,
    csrfToken: payload.authenticated ? rawCsrf : '',
  }
}

export function applyParsedAuth(parsed: ParsedAuth): AuthSnapshot {
  if (parsed.authenticated) {
    if (parsed.csrfToken) setCsrfToken(parsed.csrfToken)
  } else {
    clearCsrfToken()
  }
  return {
    authenticated: parsed.authenticated,
    user: parsed.user,
    authAvailable: parsed.authAvailable,
    authEnforced: parsed.authEnforced,
  }
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return asRecord(await response.json())
  } catch {
    return {}
  }
}

export async function fetchCurrentUser(): Promise<ParsedAuth> {
  const response = await apiFetch('/api/auth/me')
  if (!response.ok) {
    throw new Error('Unable to load the current session.')
  }
  return parseAuthPayload(await readJson(response))
}

export async function login(email: string, password: string): Promise<ParsedAuth> {
  const response = await apiFetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!response.ok) {
    throw new Error(GENERIC_LOGIN_ERROR)
  }
  const parsed = parseAuthPayload(await readJson(response))
  if (!parsed.authenticated) {
    throw new Error(GENERIC_LOGIN_ERROR)
  }
  return parsed
}

export async function logout(): Promise<LogoutAttempt> {
  const response = await apiFetch('/api/auth/logout', { method: 'POST' })
  if (!response.ok) {
    return { ok: false }
  }
  return { ok: true, parsed: parseAuthPayload(await readJson(response)) }
}
