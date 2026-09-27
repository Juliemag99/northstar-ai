import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Administration from './Administration'
import Login from './Login'
import { AuthContext, type AuthContextValue } from './auth/useAuth'
import type { StaffUser } from './api/auth'
import * as userManagement from './api/userManagement'
import type { AdminUserDetail, AdminUserSummary } from './api/userManagement'

vi.mock('./api/userManagement', () => ({
  fetchAdminUsers: vi.fn(),
  fetchAdminUser: vi.fn(),
  createAdminUser: vi.fn(),
  updateAdminUser: vi.fn(),
  replaceAdminUserClients: vi.fn(),
  resetAdminUserPassword: vi.fn(),
  fetchAdminUserHistory: vi.fn(async () => ({ user_id: 0, events: [] })),
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
          <Route path="/login" element={<Login />} />
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
  vi.mocked(userManagement.updateAdminUser).mockReset()
  vi.mocked(userManagement.replaceAdminUserClients).mockReset()
  vi.mocked(userManagement.resetAdminUserPassword).mockReset()
  vi.mocked(userManagement.fetchAdminUserHistory).mockReset()
  vi.mocked(userManagement.fetchAdminUserHistory).mockResolvedValue({ user_id: 0, events: [] })
  sessionStorage.clear()
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
    expect(screen.getByText('Existing CRM work assigned to this person. These counts are read-only. This screen does not change assigned reps, company relationships, or activity history.')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Administration history' })).toBeTruthy()
    expect(screen.getByText('No administration history yet.')).toBeTruthy()
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
    expect(userManagement.updateAdminUser).not.toHaveBeenCalled()
    expect(userManagement.replaceAdminUserClients).not.toHaveBeenCalled()
    expect(userManagement.resetAdminUserPassword).not.toHaveBeenCalled()
    expect(userManagement.fetchAdminUserHistory).not.toHaveBeenCalled()
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

  const clients = [
    { client_id: 2, client_name: 'Brown', client_code: 'brown' },
    { client_id: 4, client_name: 'Dawson', client_code: 'dawson' },
  ]

  const julieDetail: AdminUserDetail = {
    ...directory[0],
    assignments: [],
    crm_ownership: {
      client_company_relationships: 5,
      contact_client_workflows: 0,
      open_work_queue_items: 0,
    },
  }

  it('protects the signed-in administrator from turning off Active or Administrator', async () => {
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(julieDetail)
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Julie Magnani' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit user' }))

    expect((screen.getByRole('checkbox', { name: 'Active' }) as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByRole('checkbox', { name: 'Administrator' }) as HTMLInputElement).disabled).toBe(true)
    expect(screen.getByText('You cannot deactivate your own account.')).toBeTruthy()
    expect(screen.getByText('You cannot remove your own administrator access.')).toBeTruthy()
    expect(screen.queryByLabelText(/password/i)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset password' })).toBeNull()
    expect(screen.getByRole('heading', { name: 'CRM ownership' })).toBeTruthy()
  })

  it('requires confirmation before promoting a user to administrator', async () => {
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(robertDetail)
    vi.mocked(userManagement.updateAdminUser).mockResolvedValue({
      user: {
        ...robertDetail,
        is_administrator: true,
        staff_role: 'system_administrator',
        access_scope: 'all_clients',
      },
      sessions_revoked: 1,
    })
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Robert Kirsten' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit user' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Administrator' }))
    expect(
      screen.getByText(
        'This user will receive access to all clients and their role will become System administrator.',
      ),
    ).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    expect(
      screen.getByText(
        'Confirm that this user will become a System administrator with access to all clients.',
      ),
    ).toBeTruthy()
    expect(userManagement.updateAdminUser).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'I confirm this promotion' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    expect(await screen.findByText(/Active sessions revoked: 1/)).toBeTruthy()
    expect(userManagement.updateAdminUser).toHaveBeenCalledWith(42, {
      full_name: 'Robert Kirsten',
      email: 'robertk@n-star.us',
      staff_role: 'system_administrator',
      is_administrator: true,
      active: true,
    })
    expect(userManagement.replaceAdminUserClients).not.toHaveBeenCalled()
  })

  it('requires a role and a client before demotion and keeps CRM ownership separate', async () => {
    const otherAdmin: AdminUserDetail = {
      ...julieDetail,
      id: 8,
      full_name: 'NorthStar Admin',
      email: 'admin@northstargroup.com',
      assignments: [],
      clients: [],
    }
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue([
      ...directory,
      {
        ...directory[0],
        id: 8,
        full_name: 'NorthStar Admin',
        email: 'admin@northstargroup.com',
      },
    ])
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(otherAdmin)
    vi.mocked(userManagement.replaceAdminUserClients).mockResolvedValue({
      ...otherAdmin,
      assignments: [{ client_id: 2, client_code: 'brown', client_name: 'Brown', active: true }],
    })
    vi.mocked(userManagement.updateAdminUser).mockResolvedValue({
      user: {
        ...otherAdmin,
        is_administrator: false,
        staff_role: 'revops_specialist',
        access_scope: 'assigned',
        assignments: [{ client_id: 2, client_code: 'brown', client_name: 'Brown', active: true }],
      },
      sessions_revoked: 0,
    })
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View NorthStar Admin' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit user' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Administrator' }))
    expect(screen.getByText(/Existing CRM ownership is not changed/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    expect(screen.getByText('Select a staff role.')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Staff role'), { target: { value: 'revops_specialist' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    expect(screen.getByText('Select at least one client.')).toBeTruthy()
    expect(userManagement.updateAdminUser).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Brown' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    expect(await screen.findByText('User updated.')).toBeTruthy()
    expect(userManagement.replaceAdminUserClients).toHaveBeenCalledWith(8, [2])
    expect(userManagement.updateAdminUser).toHaveBeenCalledWith(8, {
      full_name: 'NorthStar Admin',
      email: 'admin@northstargroup.com',
      staff_role: 'revops_specialist',
      is_administrator: false,
      active: true,
    })
    expect(screen.getByRole('heading', { name: 'CRM ownership' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Reset password' })).toBeNull()
  })

  it('saves client checkboxes without treating them as CRM ownership', async () => {
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(robertDetail)
    vi.mocked(userManagement.replaceAdminUserClients).mockResolvedValue(robertDetail)
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Robert Kirsten' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit user' }))
    expect(screen.getByText('Which clients this person may access. Saving client access does not change CRM ownership.')).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Dawson' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save client access' }))
    expect(await screen.findByText('Client access saved. CRM ownership was not changed.')).toBeTruthy()
    expect(userManagement.replaceAdminUserClients).toHaveBeenCalledWith(42, [2, 4])
    expect(userManagement.updateAdminUser).not.toHaveBeenCalled()
  })

  it('requires deactivation confirmation and shows ownership counts', async () => {
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(robertDetail)
    vi.mocked(userManagement.updateAdminUser).mockResolvedValue({
      user: { ...robertDetail, active: false, login_status: 'inactive' },
      sessions_revoked: 2,
    })
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Robert Kirsten' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit user' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Active' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    expect(
      screen.getByText(
        'Deactivating this user will prevent sign-in and revoke active sessions. Existing CRM work will remain assigned to this user until separately reassigned.',
      ),
    ).toBeTruthy()
    expect(screen.getAllByText('Company relationships assigned: 3').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Contact workflows assigned: 2').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Open work queue items assigned: 1').length).toBeGreaterThan(0)
    expect(userManagement.updateAdminUser).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /reassign/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm deactivation' }))
    expect(await screen.findByText(/Active sessions revoked: 2/)).toBeTruthy()
    expect(screen.getAllByText('Inactive').length).toBeGreaterThan(0)
    expect(userManagement.updateAdminUser).toHaveBeenCalledWith(
      42,
      expect.objectContaining({ active: false }),
    )
  })

  it('resets another user password without showing it and reports revoked sessions', async () => {
    const secret = 'NsPilot9secret'
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(robertDetail)
    vi.mocked(userManagement.resetAdminUserPassword).mockResolvedValue({
      user_id: 42,
      has_password: true,
      password_updated_at: '2026-09-27T18:00:00Z',
      sessions_revoked: 3,
    })
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Robert Kirsten' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Set new password' }))

    const passwordInput = screen.getByLabelText('New password') as HTMLInputElement
    const confirmInput = screen.getByLabelText('Confirm new password') as HTMLInputElement
    expect(passwordInput.type).toBe('password')
    expect(confirmInput.type).toBe('password')
    expect(passwordInput.value).toBe('')
    expect(confirmInput.value).toBe('')

    fireEvent.click(screen.getByRole('button', { name: 'Review password reset' }))
    expect(screen.getByText('Enter a new password.')).toBeTruthy()

    fireEvent.change(passwordInput, { target: { value: secret } })
    fireEvent.change(confirmInput, { target: { value: `${secret}-no` } })
    fireEvent.click(screen.getByRole('button', { name: 'Review password reset' }))
    expect(screen.getByText('Password confirmation does not match.')).toBeTruthy()
    expect(userManagement.resetAdminUserPassword).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: secret } })
    fireEvent.click(screen.getByRole('button', { name: 'Review password reset' }))
    expect(screen.getByRole('heading', { name: 'Review password reset' })).toBeTruthy()
    expect(screen.getByText(/The new password is not shown/)).toBeTruthy()
    expect(screen.queryByText(secret)).toBeNull()
    expect(screen.queryByDisplayValue(secret)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm password reset' }))
    expect(await screen.findByText('Password updated. Existing sessions were revoked: 3.')).toBeTruthy()
    expect(screen.queryByLabelText('New password')).toBeNull()
    expect(screen.queryByText(secret)).toBeNull()
    expect(userManagement.resetAdminUserPassword).toHaveBeenCalledWith(42, secret)
  })

  it('says an inactive account remains inactive after a password reset', async () => {
    const secret = 'NsPilot9secret'
    const inactive = { ...robertDetail, active: false, login_status: 'inactive' as const }
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(inactive)
    vi.mocked(userManagement.resetAdminUserPassword).mockResolvedValue({
      user_id: 42,
      has_password: true,
      password_updated_at: '2026-09-27T18:00:00Z',
      sessions_revoked: 0,
    })
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Robert Kirsten' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Set new password' }))
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: secret } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: secret } })
    fireEvent.click(screen.getByRole('button', { name: 'Review password reset' }))
    expect(screen.getByText(/This account will remain inactive/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm password reset' }))
    expect(
      await screen.findByText(
        'Password updated, but this account remains inactive. Existing sessions were revoked: 0.',
      ),
    ).toBeTruthy()
    expect(screen.queryByText(secret)).toBeNull()
  })

  it('signs the administrator out after they reset their own password', async () => {
    const secret = 'NsPilot9secret'
    const logout = vi.fn(async () => undefined)
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(julieDetail)
    vi.mocked(userManagement.resetAdminUserPassword).mockResolvedValue({
      user_id: 1,
      has_password: true,
      password_updated_at: '2026-09-27T18:00:00Z',
      sessions_revoked: 1,
    })
    renderAdministration(
      authValue({ authenticated: true, user: adminUser, logout }),
      clients,
    )
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Julie Magnani' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Set new password' }))
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: secret } })
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: secret } })
    fireEvent.click(screen.getByRole('button', { name: 'Review password reset' }))
    expect(screen.queryByText(secret)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm password reset' }))
    expect(await screen.findByRole('heading', { name: 'Staff sign in' })).toBeTruthy()
    expect(
      screen.getByText('Your password was updated. Sign in with the new password.'),
    ).toBeTruthy()
    expect(logout).toHaveBeenCalled()
    expect(screen.queryByText(secret)).toBeNull()
    expect(screen.queryByDisplayValue(secret)).toBeNull()
  })

  it('shows administration history in readable form and hides secrets', async () => {
    vi.mocked(userManagement.fetchAdminUsers).mockResolvedValue(directory)
    vi.mocked(userManagement.fetchAdminUser).mockResolvedValue(robertDetail)
    vi.mocked(userManagement.fetchAdminUserHistory).mockResolvedValue({
      user_id: 42,
      events: [
        {
          id: 9,
          event_type: 'sessions_revoked',
          created_at: '2026-09-27T18:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: { sessions_revoked: 2, password: 'NsHidden9secret', token: 'session-token' },
        },
        {
          id: 8,
          event_type: 'password_reset',
          created_at: '2026-09-27T17:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: { password_updated_at: '2026-09-27T17:00:00Z', password_hash: '$argon2id$secret' },
        },
        {
          id: 7,
          event_type: 'client_removed',
          created_at: '2026-09-27T16:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: { client_name: 'Dawson', csrf_secret: 'csrf-value' },
        },
        {
          id: 6,
          event_type: 'client_granted',
          created_at: '2026-09-27T15:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: { client_name: 'Brown Industries' },
        },
        {
          id: 5,
          event_type: 'active_changed',
          created_at: '2026-09-27T14:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: { old: false, new: true },
        },
        {
          id: 4,
          event_type: 'administrator_changed',
          created_at: '2026-09-27T13:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: { old: false, new: true },
        },
        {
          id: 3,
          event_type: 'role_changed',
          created_at: '2026-09-27T12:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: { old: 'revops_specialist', new: 'revops_manager' },
        },
        {
          id: 2,
          event_type: 'future_event',
          created_at: '2026-09-27T11:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: false,
          detail: { raw: 'not-json credential-secret' },
        },
        {
          id: 1,
          event_type: 'user_created',
          created_at: '2026-09-27T10:00:00Z',
          actor_user_id: 1,
          actor_full_name: 'Julie Magnani',
          actor_email: 'juliem@n-star.us',
          target_user_id: 42,
          detail_available: true,
          detail: {
            staff_role: 'revops_specialist',
            active: true,
            clients: [{ client_id: 2, client_code: 'brown', client_name: 'Brown Industries' }],
          },
        },
      ],
    })
    renderAdministration(authValue({ authenticated: true, user: adminUser }), clients)
    fireEvent.click(screen.getByRole('tab', { name: 'User Management' }))
    fireEvent.click(await screen.findByRole('button', { name: 'View Robert Kirsten' }))
    expect(await screen.findByText('SESSIONS REVOKED')).toBeTruthy()
    const historyText = screen.getByRole('heading', { name: 'Administration history' }).parentElement?.textContent || ''
    expect(historyText.indexOf('SESSIONS REVOKED')).toBeLessThan(historyText.indexOf('USER CREATED'))
    expect(screen.getByText('2 sessions revoked')).toBeTruthy()
    expect(screen.getByText('PASSWORD RESET')).toBeTruthy()
    expect(screen.getByText('Password updated')).toBeTruthy()
    expect(screen.getByText('CLIENT GRANTED')).toBeTruthy()
    expect(screen.getByText('CLIENT REMOVED')).toBeTruthy()
    expect(screen.getByText('Brown Industries')).toBeTruthy()
    expect(screen.getAllByText('Dawson').length).toBeGreaterThan(0)
    expect(screen.getByText('ADMINISTRATOR CHANGED')).toBeTruthy()
    expect(screen.getByText('ACTIVE CHANGED')).toBeTruthy()
    expect(screen.getByText('Inactive → Active')).toBeTruthy()
    expect(screen.getByText('ROLE CHANGED')).toBeTruthy()
    expect(screen.getByText('RevOps specialist → RevOps manager')).toBeTruthy()
    expect(screen.getByText('USER CREATED')).toBeTruthy()
    expect(screen.getByText('Created by Julie Magnani')).toBeTruthy()
    expect(screen.getByText('Role: RevOps specialist')).toBeTruthy()
    expect(screen.getByText('Clients: Brown Industries')).toBeTruthy()
    expect(screen.getByText('Active: Yes')).toBeTruthy()
    expect(screen.getByText('ADMINISTRATION EVENT')).toBeTruthy()
    expect(screen.getByText('Details are unavailable.')).toBeTruthy()
    expect(screen.getAllByText(/Changed by Julie Magnani/).length).toBeGreaterThan(0)
    expect(screen.queryByText('NsHidden9secret')).toBeNull()
    expect(screen.queryByText('$argon2id$secret')).toBeNull()
    expect(screen.queryByText('session-token')).toBeNull()
    expect(screen.queryByText('csrf-value')).toBeNull()
    expect(screen.queryByText('credential-secret')).toBeNull()
    expect(screen.getByRole('button', { name: 'Edit user' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Set new password' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Account' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Security' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Client access' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'CRM ownership' })).toBeTruthy()
  })
})
