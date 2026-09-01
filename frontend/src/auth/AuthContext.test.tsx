import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider } from './AuthContext'
import { AuthGate } from './AuthGate'
import { setUnauthorizedHandler } from '../api/http'

function jsonResponse(status: number, body: Record<string, unknown> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const optionalMe = {
  authenticated: false,
  user: {
    id: 1,
    email: 'juliem@n-star.us',
    full_name: 'Julie Magnani',
    is_administrator: true,
    is_internal_northstar: true,
    active: true,
    created_at: '2026-08-07 16:18:04',
  },
  csrf_token: '',
  auth_available: true,
  auth_enforced: false,
}

const enforcedMe = {
  ...optionalMe,
  auth_enforced: true,
}

function renderApp() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <AuthProvider>
        <AuthGate>
          <div>CRM content</div>
        </AuthGate>
      </AuthProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  setUnauthorizedHandler(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('AuthProvider session check', () => {
  it('does not mount CRM when the initial /me request fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(500)))
    renderApp()
    expect(await screen.findByRole('heading', { name: 'Unable to verify your session' })).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
  })

  it('does not mount CRM when /me returns unusable data', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, { ok: true })))
    renderApp()
    expect(await screen.findByRole('heading', { name: 'Unable to verify your session' })).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
  })

  it('preserves optional CRM after a successful unenforced /me', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, optionalMe)))
    renderApp()
    expect(await screen.findByText('CRM content')).toBeTruthy()
  })

  it('retries /me from the fail-closed screen', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(500))
      .mockResolvedValueOnce(jsonResponse(200, optionalMe))
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    expect(await screen.findByRole('heading', { name: 'Unable to verify your session' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('CRM content')).toBeTruthy()
  })

  it('stays fail-closed on later /me failure after enforcement was observed', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, enforcedMe))
      .mockResolvedValueOnce(jsonResponse(500))
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    expect(await screen.findByRole('heading', { name: 'Staff sign in' })).toBeTruthy()
    window.dispatchEvent(new Event('focus'))
    expect(await screen.findByRole('heading', { name: 'Unable to verify your session' })).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
  })

  it('treats other API 401s as session loss only after enforcement was observed', async () => {
    const authenticatedEnforced = {
      ...enforcedMe,
      authenticated: true,
      csrf_token: 'csrf-from-me',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/auth/me')) return jsonResponse(200, authenticatedEnforced)
      return jsonResponse(401)
    })
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    expect(await screen.findByText('CRM content')).toBeTruthy()
    const { apiFetch } = await import('../api/http')
    await apiFetch('/api/prospects')
    expect(await screen.findByRole('heading', { name: 'Staff sign in' })).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
  })

  it('does not treat API 401s as session loss while enforcement is off', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/auth/me')) return jsonResponse(200, optionalMe)
      return jsonResponse(401)
    })
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    expect(await screen.findByText('CRM content')).toBeTruthy()
    const { apiFetch } = await import('../api/http')
    await apiFetch('/api/prospects')
    expect(screen.getByText('CRM content')).toBeTruthy()
  })
})
