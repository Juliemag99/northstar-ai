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
  createAdminUser: vi.fn(),
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

function renderAdministration(
  value: AuthContextValue,
  availableClients: Array<{ client_id: number; client_name: string; client_code: string }> = [],
) {
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={['/administration']}>
        <Routes>
          <Route path="/" element={<div>Dashboard home</div>} />
          <Route
            path="/administration"
            element={
              <Administration activeClientId={1} availableClients={availableClients} />
            }
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
  vi.mocked(userManagement.createAdminUser).mockReset()
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
    expect(userManagement.createAdminUser).not.toHaveBeenCalled()
  })

  it('creates a non-administrator from the Administration user directory', async () => {
    const secret = 'NsPilot9secret'
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.createAdminUser).mockResolvedValue({
      ...directory[1],
      id: 77,
      full_name: 'Ada Lovelace',
      email: 'ada@n-star.us',
      staff_role: 'revops_specialist',
      is_administrator: false,
      active: true,
      login_status: 'can_sign_in',
      access_scope: 'assigned',
      clients: [{ client_id: 2, client_code: 'brown', client_name: 'Brown' }],
      assignments: [{ client_id: 2, client_code: 'brown', client_name: 'Brown', active: true }],
      crm_ownership: {
        client_company_relationships: 0,
        contact_client_workflows: 0,
        open_work_queue_items: 0,
      },
    })

    renderAdministration(authValue({ authenticated: true, user: adminUser }), [
      { client_id: 2, client_name: 'Brown', client_code: 'brown' },
      { client_id: 3, client_name: 'Dawson', client_code: 'dawson' },
    ])
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Create user' }))

    const roleOptions = screen.getAllByRole('option').map((option) => option.textContent)
    expect(roleOptions).toEqual([
      'Select a role…',
      'RevOps specialist',
      'RevOps manager',
      'Appointment setter',
      'Read only',
    ])
    expect(screen.queryByRole('option', { name: 'System administrator' })).toBeNull()
    expect(screen.queryByRole('button', { name: /administrator/i })).toBeNull()

    fireEvent.change(screen.getByLabelText('Full name'), { target: { value: 'Ada Lovelace' } })
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'Ada@n-star.us' } })
    fireEvent.change(screen.getByLabelText('Staff role'), { target: { value: 'revops_specialist' } })
    fireEvent.change(screen.getByLabelText('Initial password'), { target: { value: secret } })
    fireEvent.change(screen.getByLabelText('Confirm initial password'), { target: { value: secret } })
    fireEvent.click(screen.getByRole('button', { name: 'Review user' }))
    expect(screen.getByText('Select at least one client.')).toBeTruthy()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Brown' }))
    fireEvent.change(screen.getByLabelText('Confirm initial password'), {
      target: { value: `${secret}-mismatch` },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Review user' }))
    expect(screen.getByText('Password confirmation does not match.')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Confirm initial password'), { target: { value: secret } })
    fireEvent.click(screen.getByRole('button', { name: 'Review user' }))
    expect(screen.getByRole('heading', { name: 'Review new user' })).toBeTruthy()
    expect(screen.getByText('ada@n-star.us')).toBeTruthy()
    expect(screen.getByText(/The password is not shown/)).toBeTruthy()
    expect(screen.queryByText(secret)).toBeNull()
    expect(screen.queryByDisplayValue(secret)).toBeNull()
    expect(screen.queryByLabelText('Initial password')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm create user' }))
    expect(await screen.findByRole('heading', { name: 'Ada Lovelace' })).toBeTruthy()
    expect(screen.getByText('User created. The initial password is not shown again.')).toBeTruthy()
    expect(screen.queryByText(secret)).toBeNull()
    expect(screen.queryByLabelText('Initial password')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset password' })).toBeNull()
    expect(userManagement.createAdminUser).toHaveBeenCalledWith({
      full_name: 'Ada Lovelace',
      email: 'ada@n-star.us',
      staff_role: 'revops_specialist',
      client_ids: [2],
      active: true,
      password: secret,
    })
  })
})
