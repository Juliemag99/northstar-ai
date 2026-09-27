import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Administration from './Administration'
import { AuthContext, type AuthContextValue } from './auth/useAuth'
import type { StaffUser } from './api/auth'
import * as userManagement from './api/userManagement'
import type { AdminUserDetail, AdminUserSummary } from './api/userManagement'

vi.mock('./api/userManagement', () => ({
  fetchAdminUsers: vi.fn(),
  fetchAdminUser: vi.fn(),
}))

const adminUser: StaffUser = {
  id: 1,
  email: 'juliem@n-star.us',
  full_name: 'Julie Magnani',
  is_administrator: true,
  is_internal_northstar: true,
  active: true,
  created_at: '2026-08-07 16:18:04',
}

const directory: AdminUserSummary[] = [
  {
    id: 1,
    full_name: 'Julie Magnani',
    email: 'juliem@n-star.us',
    staff_role: 'system_administrator',
    is_administrator: true,
    active: true,
    is_internal_northstar: true,
    created_at: '2026-08-07 16:18:04',
    password_updated_at: '2026-09-01 12:00:00',
    login_status: 'can_sign_in',
    access_scope: 'all_clients',
    clients: [],
  },
  {
    id: 42,
    full_name: 'Robert Kirsten',
    email: 'robertk@n-star.us',
    staff_role: 'revops_specialist',
    is_administrator: false,
    active: true,
    is_internal_northstar: true,
    created_at: '2026-09-27 15:00:00',
    password_updated_at: '',
    login_status: 'can_sign_in',
    access_scope: 'assigned',
    clients: [{ client_id: 2, client_code: 'brown', client_name: 'Brown' }],
  },
]

const robertDetail: AdminUserDetail = {
  ...directory[1],
  assignments: [
    { client_id: 2, client_code: 'brown', client_name: 'Brown', active: true },
    { client_id: 4, client_code: 'dawson', client_name: 'Dawson', active: false },
  ],
  crm_ownership: {
    client_company_relationships: 3,
    contact_client_workflows: 2,
    open_work_queue_items: 1,
  },
}

function authValue(overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  return {
    ready: true,
    sessionError: false,
    user: null,
    authenticated: false,
    authAvailable: true,
    authEnforced: true,
    login: vi.fn(async () => undefined),
    logout: vi.fn(async () => undefined),
    retrySession: vi.fn(async () => undefined),
    ...overrides,
  }
}

function renderAdministration(value: AuthContextValue) {
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
  vi.mocked(userManagement.fetchAdminUsers).mockReset()
  vi.mocked(userManagement.fetchAdminUser).mockReset()
})

describe('Administration User Management', () => {
  it('follows Administration to the read-only user list and detail', async () => {
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(robertDetail)

    renderAdministration(authValue({ authenticated: true, user: adminUser }))

    expect(screen.getByRole('heading', { name: 'Administration' })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))

    expect(await screen.findByRole('heading', { name: 'User Management' })).toBeTruthy()
    expect(screen.getByText('Julie Magnani')).toBeTruthy()
    expect(screen.getByText('robertk@n-star.us')).toBeTruthy()
    expect(screen.getByText('RevOps specialist')).toBeTruthy()
    expect(screen.getByText('All clients')).toBeTruthy()
    expect(screen.getByText('Brown')).toBeTruthy()
    expect(screen.getAllByText('Can sign in').length).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole('button', { name: 'View Robert Kirsten' }))

    expect(await screen.findByRole('heading', { name: 'Robert Kirsten' })).toBeTruthy()
    expect(screen.getByText('CRM ownership is separate from client access. These counts show work still assigned to this person. This screen does not change assigned reps, company relationships, or activity history.')).toBeTruthy()
    expect(screen.getByText('Company relationships assigned: 3')).toBeTruthy()
    expect(screen.getByText('Contact workflows assigned: 2')).toBeTruthy()
    expect(screen.getByText('Open work queue items assigned: 1')).toBeTruthy()
    expect(screen.getByText('Dawson')).toBeTruthy()
    expect(screen.getByText('Not set')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Create user' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset password' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Deactivate' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.queryByLabelText(/password/i)).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('does not let an ordinary specialist reach Administration or User Management', () => {
    renderAdministration(
      authValue({
        authenticated: true,
        user: {
          ...adminUser,
          id: 42,
          full_name: 'Robert Kirsten',
          email: 'robertk@n-star.us',
          is_administrator: false,
          staff_role: 'revops_specialist',
        },
      }),
    )

    expect(screen.queryByRole('heading', { name: 'Administration' })).toBeNull()
    expect(screen.queryByRole('tab', { name: 'User Management' })).toBeNull()
    expect(screen.getByText('Dashboard home')).toBeTruthy()
    expect(userManagement.fetchAdminUsers).not.toHaveBeenCalled()
  })
})
