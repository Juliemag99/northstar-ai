import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Administration from '../Administration'
import { AuthContext, type AuthContextValue } from './useAuth'
import type { StaffUser } from '../api/auth'

const adminUser: StaffUser = {
  id: 1,
  email: 'juliem@n-star.us',
  full_name: 'Julie Magnani',
  is_administrator: true,
  is_internal_northstar: true,
  active: true,
  created_at: '2026-08-07 16:18:04',
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

function renderAdmin(value: AuthContextValue) {
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={['/administration']}>
        <Routes>
          <Route path="/" element={<div>Dashboard home</div>} />
          <Route
            path="/administration"
            element={<Administration activeClientId={1} availableClients={[]} />}
          />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
})

describe('Administration access', () => {
  it('does not render Administration for the signed-out Julie fallback', () => {
    renderAdmin(authValue({ authenticated: false, user: adminUser }))
    expect(screen.queryByRole('heading', { name: 'Administration' })).toBeNull()
    expect(screen.getByText('Dashboard home')).toBeTruthy()
  })

  it('does not render Administration for an authenticated non-administrator', () => {
    renderAdmin(
      authValue({
        authenticated: true,
        user: { ...adminUser, id: 9, is_administrator: false },
      }),
    )
    expect(screen.queryByRole('heading', { name: 'Administration' })).toBeNull()
    expect(screen.getByText('Dashboard home')).toBeTruthy()
  })

  it('renders Administration for an authenticated administrator', () => {
    renderAdmin(authValue({ authenticated: true, user: adminUser }))
    expect(screen.getByRole('heading', { name: 'Administration' })).toBeTruthy()
  })
})
