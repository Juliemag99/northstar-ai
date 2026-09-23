import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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
    expect(screen.getByRole('tab', { name: 'Company & contact import' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Client Data Import' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Research & Custom Prospect Import' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Data Management' })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Company & contact import' }))
    expect(screen.getByRole('heading', { name: 'Company & contact import' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Email Connections' })).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'Client Data Import' }))
    expect(screen.getByRole('heading', { name: 'Client Data Import' })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Research & Custom Prospect Import' }))
    expect(screen.getByRole('heading', { name: 'Research & Custom Prospect Import' })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Data Management' }))
    expect(screen.getByRole('tab', { name: 'LeadMaster Refresh' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Master Data' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Master Data Export' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Excel (.xlsx)' })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'LeadMaster Refresh' }))
    expect(screen.getByRole('heading', { name: 'LeadMaster Refresh' })).toBeTruthy()
    expect(screen.getByText(/Source system:/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Confirm \(live confirmation not enabled\)/i })).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'Master Data' }))
    expect(screen.getByRole('heading', { name: 'Master Data' })).toBeTruthy()
    expect(screen.getByText(/Master Company archive and restore are enabled/i)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Merge (not yet enabled)' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Delete (not yet enabled)' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
