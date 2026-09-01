import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { AuthGate } from './AuthGate'
import { AuthContext, type AuthContextValue } from './useAuth'

function LocationProbe() {
  const location = useLocation()
  return (
    <div data-testid="location">
      {location.pathname}
      {location.search}
    </div>
  )
}

function authValue(overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  return {
    ready: true,
    sessionError: false,
    user: null,
    authenticated: false,
    authAvailable: true,
    authEnforced: false,
    login: vi.fn(async () => undefined),
    logout: vi.fn(async () => undefined),
    retrySession: vi.fn(async () => undefined),
    ...overrides,
  }
}

function renderGate(path: string, value: AuthContextValue) {
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={[path]}>
        <LocationProbe />
        <AuthGate>
          <div>CRM content</div>
        </AuthGate>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
})

describe('AuthGate', () => {
  it('shows a splash and does not mount CRM until ready', () => {
    renderGate('/', authValue({ ready: false, sessionError: false }))
    expect(screen.getByText('Checking your session.')).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Staff sign in' })).toBeNull()
  })

  it('shows a fail-closed retry screen instead of CRM when the session check failed', () => {
    const retrySession = vi.fn(async () => undefined)
    renderGate('/', authValue({ ready: false, sessionError: true, retrySession }))
    expect(screen.getByRole('heading', { name: 'Unable to verify your session' })).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(retrySession).toHaveBeenCalledTimes(1)
  })

  it('preserves optional CRM when enforcement is off', () => {
    renderGate('/prospects', authValue({ authEnforced: false, authenticated: false }))
    expect(screen.getByText('CRM content')).toBeTruthy()
  })

  it('keeps /login accessible without mounting CRM in optional mode', () => {
    renderGate('/login', authValue({ authEnforced: false, authenticated: false }))
    expect(screen.getByRole('heading', { name: 'Staff sign in' })).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
  })

  it('redirects unauthenticated users to login with a safe next path when enforced', () => {
    renderGate(
      '/prospects?client_id=2',
      authValue({ authEnforced: true, authenticated: false }),
    )
    expect(screen.queryByText('CRM content')).toBeNull()
    expect(screen.getByTestId('location').textContent).toBe(
      `/login?next=${encodeURIComponent('/prospects?client_id=2')}`,
    )
    expect(screen.getByRole('heading', { name: 'Staff sign in' })).toBeTruthy()
  })

  it('keeps /login mounted when enforced and unauthenticated', () => {
    renderGate('/login', authValue({ authEnforced: true, authenticated: false }))
    expect(screen.getByRole('heading', { name: 'Staff sign in' })).toBeTruthy()
    expect(screen.queryByText('CRM content')).toBeNull()
    expect(screen.getByTestId('location').textContent).toBe('/login')
  })

  it('sends authenticated users away from /login to a safe next path', () => {
    renderGate(
      `/login?next=${encodeURIComponent('/contacts/9?client_id=1')}`,
      authValue({ authEnforced: true, authenticated: true }),
    )
    expect(screen.getByTestId('location').textContent).toBe('/contacts/9?client_id=1')
    expect(screen.getByText('CRM content')).toBeTruthy()
  })
})
