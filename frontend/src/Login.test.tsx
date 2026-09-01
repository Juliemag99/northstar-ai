import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'
import Login from './Login'
import { AuthContext, type AuthContextValue } from './auth/useAuth'

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

function renderLogin(path: string, value: AuthContextValue) {
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={[path]}>
        <LocationProbe />
        <Login />
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
})

describe('Login', () => {
  it('shows Continue without signing in only when enforcement is off', () => {
    renderLogin('/login', authValue({ authEnforced: false }))
    expect(screen.getByRole('button', { name: 'Continue without signing in' })).toBeTruthy()
    cleanup()
    renderLogin('/login', authValue({ authEnforced: true }))
    expect(screen.queryByRole('button', { name: 'Continue without signing in' })).toBeNull()
  })

  it('returns to a safe next path after successful login', async () => {
    const login = vi.fn(async () => undefined)
    renderLogin(
      `/login?next=${encodeURIComponent('/contacts/1?client_id=2')}`,
      authValue({ login }),
    )
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'juliem@n-star.us' },
    })
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'secret-password' },
    })
    fireEvent.submit(screen.getByRole('button', { name: 'Sign in' }).closest('form')!)
    await vi.waitFor(() => {
      expect(screen.getByTestId('location').textContent).toBe('/contacts/1?client_id=2')
    })
    expect(login).toHaveBeenCalledTimes(1)
  })

  it('stays on login with the generic error when login fails', async () => {
    const login = vi.fn(async () => {
      throw new Error('Invalid email or password.')
    })
    renderLogin('/login', authValue({ login }))
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'juliem@n-star.us' },
    })
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'wrong' },
    })
    fireEvent.submit(screen.getByRole('button', { name: 'Sign in' }).closest('form')!)
    expect((await screen.findByRole('alert')).textContent).toBe('Invalid email or password.')
    expect(screen.getByTestId('location').textContent).toBe('/login')
  })
})
