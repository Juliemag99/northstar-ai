import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, setCsrfToken, setUnauthorizedHandler, clearCsrfToken } from './http'

function jsonResponse(status: number, body: Record<string, unknown> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  clearCsrfToken()
  setUnauthorizedHandler(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('apiFetch', () => {
  it('does not treat login 401 as session loss', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(401, { detail: 'Invalid email or password.' })),
    )
    const response = await apiFetch('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: 'a@b.c', password: 'x' }),
    })
    expect(response.status).toBe(401)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('does not treat /api/auth/me 401 as session loss', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(401)))
    await apiFetch('/api/auth/me')
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('notifies for other API 401 responses', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(401)))
    await apiFetch('/api/prospects')
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not notify for CSRF 403 responses', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(403, { detail: 'CSRF token missing or invalid.' })),
    )
    const response = await apiFetch('/api/activities', { method: 'POST', body: '{}' })
    expect(response.status).toBe(403)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('sends the in-memory CSRF header on writes', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200))
    vi.stubGlobal('fetch', fetchMock)
    setCsrfToken('csrf-secret-token')
    await apiFetch('/api/activities', { method: 'POST', body: '{}' })
    const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers)
    expect(headers.get('X-CSRF-Token')).toBe('csrf-secret-token')
  })
})
