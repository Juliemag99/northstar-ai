import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from './auth/useAuth'
import { stagePasswordResetNotice } from './auth/passwordResetNotice'
import {
  createAdminUser,
  fetchAdminUser,
  fetchAdminUserHistory,
  fetchAdminUsers,
  replaceAdminUserClients,
  resetAdminUserPassword,
  updateAdminUser,
  type AdminHistoryEvent,
  type AdminUserDetail,
  type AdminUserSummary,
  type LoginStatus,
} from './api/userManagement'

type DirectoryClient = {
  client_id: number
  client_name: string
  client_code?: string
}

const CREATABLE_ROLES = [
  ['revops_specialist', 'RevOps specialist'],
  ['revops_manager', 'RevOps manager'],
  ['appointment_setter', 'Appointment setter'],
  ['read_only', 'Read only'],
] as const

const ROLE_LABELS: Record<string, string> = {
  system_administrator: 'System administrator',
  operations_admin: 'Operations admin',
  revops_manager: 'RevOps manager',
  revops_specialist: 'RevOps specialist',
  appointment_setter: 'Appointment setter',
  read_only: 'Read only',
}

const LOGIN_LABELS: Record<LoginStatus, string> = {
  inactive: 'Inactive',
  no_password: 'No password',
  locked: 'Locked',
  can_sign_in: 'Can sign in',
}

function roleLabel(role: string): string {
  if (!role) return 'None'
  return ROLE_LABELS[role] || role
}

function historyScalar(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function historyClients(detail: Record<string, unknown>): string {
  if (Array.isArray(detail.clients)) {
    const names = detail.clients
      .map((item) => {
        const client = item && typeof item === 'object' ? (item as Record<string, unknown>) : {}
        return historyScalar(client.client_name) || historyScalar(client.client_code)
      })
      .filter(Boolean)
    if (names.length > 0) return names.join(', ')
  }
  const name = historyScalar(detail.client_name)
  if (name) return name
  const clientId = Number(detail.client_id)
  if (Number.isFinite(clientId) && clientId > 0) return `Client ${clientId}`
  return 'None'
}

function historyActor(event: AdminHistoryEvent): string {
  return event.actor_full_name || event.actor_email || 'an unknown administrator'
}

function historyChange(event: AdminHistoryEvent, labelFor: (value: string) => string): string[] {
  const oldValue = historyScalar(event.detail.old)
  const newValue = historyScalar(event.detail.new)
  return [`${labelFor(oldValue)} → ${labelFor(newValue)}`, `Changed by ${historyActor(event)}`]
}

function historyLines(event: AdminHistoryEvent): string[] {
  const actor = historyActor(event)
  const detail = event.detail
  if (!event.detail_available) {
    return ['ADMINISTRATION EVENT', 'Details are unavailable.']
  }
  switch (event.event_type) {
    case 'user_created':
      return [
        'USER CREATED',
        `Created by ${actor}`,
        `Role: ${roleLabel(historyScalar(detail.staff_role))}`,
        `Clients: ${historyClients(detail)}`,
        `Active: ${detail.active === true ? 'Yes' : 'No'}`,
      ]
    case 'name_changed':
      return ['NAME CHANGED', ...historyChange(event, (value) => value || 'None')]
    case 'email_changed':
      return ['EMAIL CHANGED', ...historyChange(event, (value) => value || 'None')]
    case 'role_changed':
      return ['ROLE CHANGED', ...historyChange(event, roleLabel)]
    case 'administrator_changed':
      return [
        'ADMINISTRATOR CHANGED',
        ...historyChange(event, (value) => (value === 'true' ? 'Yes' : 'No')),
      ]
    case 'active_changed':
      return [
        'ACTIVE CHANGED',
        ...historyChange(event, (value) => (value === 'true' ? 'Active' : 'Inactive')),
      ]
    case 'client_granted':
      return ['CLIENT GRANTED', historyClients(detail), `Changed by ${actor}`]
    case 'client_removed':
      return ['CLIENT REMOVED', historyClients(detail), `Changed by ${actor}`]
    case 'password_reset':
      return ['PASSWORD RESET', 'Password updated', `Changed by ${actor}`]
    case 'sessions_revoked': {
      const count = Number(detail.sessions_revoked)
      const sessions = Number.isFinite(count) ? count : 0
      return ['SESSIONS REVOKED', `${sessions} ${sessions === 1 ? 'session' : 'sessions'} revoked`]
    }
    default: {
      const labels: Array<[string, string]> = [
        ['full_name', 'Full name'],
        ['email', 'Email'],
        ['staff_role', 'Role'],
        ['client_name', 'Client'],
      ]
      const lines = ['ADMINISTRATION EVENT']
      for (const [key, label] of labels) {
        const value = historyScalar(detail[key])
        if (value) lines.push(`${label}: ${value}`)
      }
      lines.push(`Changed by ${actor}`)
      return lines
    }
  }
}

function allowedClientsLabel(user: AdminUserSummary): string {
  if (user.access_scope === 'all_clients') return 'All clients'
  if (user.clients.length === 0) return 'None'
  return user.clients.map((client) => client.client_name || client.client_code).join(', ')
}

function blankCreateForm() {
  return {
    fullName: '',
    email: '',
    staffRole: '',
    clientIds: [] as number[],
    active: true,
    password: '',
    confirmPassword: '',
  }
}

function blankAccount(user: AdminUserDetail) {
  return {
    fullName: user.full_name,
    email: user.email,
    staffRole: user.is_administrator ? '' : user.staff_role,
    isAdministrator: user.is_administrator,
    active: user.active,
    clientIds: user.assignments.filter((item) => item.active).map((item) => item.client_id),
    promotionConfirmed: false,
  }
}

export default function AdministrationUserManagement({
  availableClients,
}: {
  availableClients: DirectoryClient[]
}) {
  const auth = useAuth()
  const navigate = useNavigate()
  const signedInUserId = auth.user?.id ?? null
  const [users, setUsers] = useState<AdminUserSummary[]>([])
  const [detail, setDetail] = useState<AdminUserDetail | null>(null)
  const [history, setHistory] = useState<AdminHistoryEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState(blankCreateForm)
  const [editing, setEditing] = useState(false)
  const [resettingPassword, setResettingPassword] = useState(false)
  const [reviewingPassword, setReviewingPassword] = useState(false)
  const [newPassword, setNewPassword] = useState('')
  const [confirmNewPassword, setConfirmNewPassword] = useState('')
  const [showDeactivation, setShowDeactivation] = useState(false)
  const [account, setAccount] = useState({
    fullName: '',
    email: '',
    staffRole: '',
    isAdministrator: false,
    active: true,
    clientIds: [] as number[],
    promotionConfirmed: false,
  })

  useEffect(() => {
    if (detail == null) {
      setHistory([])
      return
    }
    const userId = detail.id
    let cancelled = false
    void fetchAdminUserHistory(userId)
      .then((payload) => {
        if (!cancelled) setHistory(payload.events)
      })
      .catch(() => {
        if (!cancelled) setHistory([])
      })
    return () => {
      cancelled = true
    }
  }, [detail?.id])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    void fetchAdminUsers()
      .then((rows) => {
        if (!cancelled) setUsers(rows)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setUsers([])
          setError(err instanceof Error ? err.message : 'Unable to load users.')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  function resetCreate() {
    setForm(blankCreateForm())
    setCreating(false)
    setReviewing(false)
    setSaving(false)
  }

  function createFormError(): string | null {
    if (!form.fullName.trim()) return 'Full name is required.'
    const email = form.email.trim().toLowerCase()
    if (!email.endsWith('@n-star.us') || !email.includes('@')) return 'Email must use @n-star.us.'
    if (!CREATABLE_ROLES.some(([value]) => value === form.staffRole)) return 'Select a staff role.'
    if (form.clientIds.length === 0) return 'Select at least one client.'
    if (!form.password) return 'Enter an initial password.'
    if (form.password !== form.confirmPassword) return 'Password confirmation does not match.'
    return null
  }

  function openReview() {
    const issue = createFormError()
    if (issue) {
      setError(issue)
      setReviewing(false)
      return
    }
    setError(null)
    setReviewing(true)
  }

  async function submitCreate() {
    const issue = createFormError()
    if (issue) {
      setError(issue)
      setReviewing(false)
      return
    }
    setSaving(true)
    setError(null)
    const password = form.password
    try {
      const created = await createAdminUser({
        full_name: form.fullName.trim(),
        email: form.email.trim().toLowerCase(),
        staff_role: form.staffRole,
        client_ids: form.clientIds,
        active: form.active,
        password,
      })
      setForm(blankCreateForm())
      setCreating(false)
      setReviewing(false)
      setDetail(created)
      setSuccess('User created. The initial password is not shown again.')
      try {
        setUsers(await fetchAdminUsers())
      } catch {
        // The new user's detail is already open. A list refresh failure is not a failed create.
      }
    } catch (err: unknown) {
      setSuccess(null)
      setError(err instanceof Error ? err.message : 'Unable to create this user.')
    } finally {
      setSaving(false)
    }
  }

  function clearPasswordReset() {
    setNewPassword('')
    setConfirmNewPassword('')
    setResettingPassword(false)
    setReviewingPassword(false)
  }

  function passwordResetError(): string | null {
    if (!newPassword) return 'Enter a new password.'
    if (newPassword !== confirmNewPassword) return 'Password confirmation does not match.'
    return null
  }

  function openPasswordReview() {
    const issue = passwordResetError()
    if (issue) {
      setError(issue)
      setReviewingPassword(false)
      return
    }
    setError(null)
    setReviewingPassword(true)
  }

  async function submitPasswordReset() {
    if (!detail) return
    const issue = passwordResetError()
    if (issue) {
      setError(issue)
      setReviewingPassword(false)
      return
    }
    const password = newPassword
    const isSelf = signedInUserId === detail.id
    const staysInactive = !detail.active
    setSaving(true)
    setError(null)
    try {
      const result = await resetAdminUserPassword(detail.id, password)
      setNewPassword('')
      setConfirmNewPassword('')
      setReviewingPassword(false)
      setResettingPassword(false)
      if (isSelf) {
        stagePasswordResetNotice()
        try {
          await auth.logout()
        } catch {
          // The current session is already revoked.
        }
        navigate('/login', { replace: true })
        return
      }
      const refreshed = await fetchAdminUser(detail.id)
      setDetail(refreshed)
      const inactiveNote = staysInactive
        ? 'Password updated, but this account remains inactive.'
        : 'Password updated.'
      setSuccess(`${inactiveNote} Existing sessions were revoked: ${result.sessions_revoked}.`)
      try {
        setUsers(await fetchAdminUsers())
      } catch {
        // Detail already shows the refreshed account.
      }
    } catch (err: unknown) {
      setSuccess(null)
      setError(err instanceof Error ? err.message : 'Unable to update this password.')
    } finally {
      setSaving(false)
    }
  }

  function startEdit(user: AdminUserDetail) {
    setAccount(blankAccount(user))
    setEditing(true)
    setShowDeactivation(false)
    setError(null)
    setSuccess(null)
  }

  function toggleAccountClient(clientId: number) {
    setAccount((current) => {
      const selected = current.clientIds.includes(clientId)
      return {
        ...current,
        clientIds: selected
          ? current.clientIds.filter((id) => id !== clientId)
          : [...current.clientIds, clientId],
      }
    })
  }

  async function saveAccount(confirmDeactivation = false) {
    if (!detail) return
    const isSelf = signedInUserId === detail.id
    const promoting = !detail.is_administrator && account.isAdministrator
    const demoting = detail.is_administrator && !account.isAdministrator && !isSelf
    const deactivating = detail.active && !account.active && !isSelf
    if (!account.fullName.trim()) {
      setError('Full name is required.')
      return
    }
    const email = account.email.trim().toLowerCase()
    const emailChanged = email !== detail.email.trim().toLowerCase()
    if (emailChanged && (!email.endsWith('@n-star.us') || !email.includes('@'))) {
      setError('Email must use @n-star.us.')
      return
    }
    if (promoting && !account.promotionConfirmed) {
      setError('Confirm that this user will become a System administrator with access to all clients.')
      return
    }
    if (demoting && !CREATABLE_ROLES.some(([value]) => value === account.staffRole)) {
      setError('Select a staff role.')
      return
    }
    if (demoting && account.clientIds.length === 0) {
      setError('Select at least one client.')
      return
    }
    if (deactivating && !confirmDeactivation) {
      setShowDeactivation(true)
      setError(null)
      return
    }
    setSaving(true)
    setError(null)
    const staffRole =
      account.isAdministrator || isSelf ? 'system_administrator' : account.staffRole
    try {
      if (demoting) {
        await replaceAdminUserClients(detail.id, account.clientIds)
      }
      const result = await updateAdminUser(detail.id, {
        full_name: account.fullName.trim(),
        email,
        staff_role: staffRole,
        is_administrator: isSelf ? true : account.isAdministrator,
        active: isSelf ? true : account.active,
      })
      if (result.sessions_revoked > 0 && isSelf) {
        setSuccess(
          'Your email changed, so this session was signed out. Sign in again with the new email.',
        )
        setEditing(false)
        setShowDeactivation(false)
        try {
          await auth.logout()
        } catch {
          // The session is already revoked. Leave the signed-out state in place.
        }
        return
      }
      setDetail(result.user)
      setAccount(blankAccount(result.user))
      setEditing(false)
      setShowDeactivation(false)
      const revoked =
        result.sessions_revoked > 0 ? ` Active sessions revoked: ${result.sessions_revoked}.` : ''
      setSuccess(
        result.user.active
          ? `User updated.${revoked}`
          : `User deactivated. Sign-in is blocked and CRM work was not reassigned.${revoked}`,
      )
      try {
        setUsers(await fetchAdminUsers())
      } catch {
        // Detail already shows the saved account.
      }
    } catch (err: unknown) {
      setSuccess(null)
      setError(err instanceof Error ? err.message : 'Unable to update this user.')
    } finally {
      setSaving(false)
    }
  }

  async function saveClients() {
    if (!detail) return
    if (!account.isAdministrator && account.clientIds.length === 0) {
      setError('Select at least one client.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const updated = await replaceAdminUserClients(detail.id, account.clientIds)
      setDetail(updated)
      setAccount(blankAccount(updated))
      setSuccess('Client access saved. CRM ownership was not changed.')
      try {
        setUsers(await fetchAdminUsers())
      } catch {
        // Detail already shows the saved client access.
      }
    } catch (err: unknown) {
      setSuccess(null)
      setError(err instanceof Error ? err.message : 'Unable to update client access.')
    } finally {
      setSaving(false)
    }
  }

  function toggleClient(clientId: number) {
    setForm((current) => {
      const selected = current.clientIds.includes(clientId)
      return {
        ...current,
        clientIds: selected
          ? current.clientIds.filter((id) => id !== clientId)
          : [...current.clientIds, clientId],
      }
    })
  }

  async function openUser(userId: number) {
    setDetailLoading(true)
    setError(null)
    setSuccess(null)
    setEditing(false)
    setShowDeactivation(false)
    clearPasswordReset()
    resetCreate()
    try {
      setDetail(await fetchAdminUser(userId))
    } catch (err: unknown) {
      setDetail(null)
      setError(err instanceof Error ? err.message : 'Unable to load this user.')
    } finally {
      setDetailLoading(false)
    }
  }

  const selectedClientNames = availableClients
    .filter((client) => form.clientIds.includes(client.client_id))
    .map((client) => client.client_name || client.client_code || `Client ${client.client_id}`)

  const editClients: DirectoryClient[] = []
  const seenClientIds = new Set<number>()
  for (const client of availableClients) {
    if (seenClientIds.has(client.client_id)) continue
    seenClientIds.add(client.client_id)
    editClients.push(client)
  }
  for (const assignment of detail?.assignments ?? []) {
    if (seenClientIds.has(assignment.client_id)) continue
    seenClientIds.add(assignment.client_id)
    editClients.push(assignment)
  }
  const isSelf = detail != null && signedInUserId === detail.id
  const promoting = detail != null && !detail.is_administrator && account.isAdministrator
  const demoting = detail != null && detail.is_administrator && !account.isAdministrator && !isSelf

  return (
    <section className="administration-section" aria-labelledby="user-management-heading">
      <h2 id="user-management-heading">User Management</h2>
      <p className="queue-sub">
        Staff directory. Creating a user sets client access only. It does not assign CRM work.
      </p>

      {success ? (
        <p className="status-banner" role="status">
          {success}
        </p>
      ) : null}

      {error ? (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      ) : null}

      {detailLoading ? <p className="data-status">Loading user…</p> : null}

      {detail ? (
        <div>
          <p>
            <button
              type="button"
              className="link-btn"
              onClick={() => {
                setDetail(null)
                setEditing(false)
                setShowDeactivation(false)
              }}
            >
              Back to users
            </button>
          </p>
          <h3>{detail.full_name}</h3>
          <p>
            <button type="button" className="primary-btn" onClick={() => startEdit(detail)}>
              Edit user
            </button>
          </p>
          <h3>Account</h3>
          <p className="queue-sub">
            Identity, role, administrator status, and whether the account is active.
          </p>
          {editing ? (
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void saveAccount(false)
              }}
            >
              <label className="setup-field">
                <span className="setup-field-label">Full name</span>
                <input
                  aria-label="Full name"
                  value={account.fullName}
                  onChange={(event) => setAccount({ ...account, fullName: event.target.value })}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Email</span>
                <input
                  type="email"
                  aria-label="Email"
                  autoComplete="off"
                  value={account.email}
                  onChange={(event) => setAccount({ ...account, email: event.target.value })}
                />
              </label>
              {account.isAdministrator ? (
                <p className="queue-sub">Role: System administrator</p>
              ) : (
                <label className="setup-field">
                  <span className="setup-field-label">Staff role</span>
                  <select
                    aria-label="Staff role"
                    value={account.staffRole}
                    onChange={(event) => setAccount({ ...account, staffRole: event.target.value })}
                  >
                    <option value="">Select a role…</option>
                    {CREATABLE_ROLES.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="setup-field">
                <span className="setup-field-label">Administrator</span>
                <input
                  type="checkbox"
                  aria-label="Administrator"
                  checked={isSelf ? true : account.isAdministrator}
                  disabled={isSelf}
                  onChange={(event) => {
                    const next = event.target.checked
                    setShowDeactivation(false)
                    setAccount({
                      ...account,
                      isAdministrator: next,
                      staffRole: next ? '' : account.staffRole === 'system_administrator' ? '' : account.staffRole,
                      promotionConfirmed: false,
                    })
                  }}
                />
              </label>
              {isSelf ? (
                <p className="queue-sub">You cannot remove your own administrator access.</p>
              ) : null}
              {promoting ? (
                <div>
                  <p className="queue-sub">
                    This user will receive access to all clients and their role will become System
                    administrator.
                  </p>
                  <label>
                    <input
                      type="checkbox"
                      aria-label="I confirm this promotion"
                      checked={account.promotionConfirmed}
                      onChange={(event) =>
                        setAccount({ ...account, promotionConfirmed: event.target.checked })
                      }
                    />{' '}
                    I confirm this promotion
                  </label>
                </div>
              ) : null}
              {demoting ? (
                <p className="queue-sub">
                  Choose a non-administrator role and at least one client before saving. Existing
                  CRM ownership is not changed.
                </p>
              ) : null}
              <label className="setup-field">
                <span className="setup-field-label">Active</span>
                <input
                  type="checkbox"
                  aria-label="Active"
                  checked={isSelf ? true : account.active}
                  disabled={isSelf}
                  onChange={(event) => {
                    setShowDeactivation(false)
                    setAccount({ ...account, active: event.target.checked })
                  }}
                />
              </label>
              {isSelf ? (
                <p className="queue-sub">You cannot deactivate your own account.</p>
              ) : null}
              {showDeactivation ? (
                <div>
                  <p className="queue-sub">
                    Deactivating this user will prevent sign-in and revoke active sessions.
                    Existing CRM work will remain assigned to this user until separately
                    reassigned.
                  </p>
                  <ul>
                    <li>
                      Company relationships assigned:{' '}
                      {detail.crm_ownership.client_company_relationships}
                    </li>
                    <li>
                      Contact workflows assigned: {detail.crm_ownership.contact_client_workflows}
                    </li>
                    <li>
                      Open work queue items assigned: {detail.crm_ownership.open_work_queue_items}
                    </li>
                  </ul>
                  <p>
                    <button
                      type="button"
                      className="primary-btn"
                      disabled={saving}
                      onClick={() => void saveAccount(true)}
                    >
                      Confirm deactivation
                    </button>
                  </p>
                </div>
              ) : (
                <p>
                  <button type="submit" className="primary-btn" disabled={saving}>
                    Save account
                  </button>
                </p>
              )}
            </form>
          ) : (
            <dl className="queue-sub">
              <div>
                <dt>Full name</dt>
                <dd>{detail.full_name}</dd>
              </div>
              <div>
                <dt>Email</dt>
                <dd>{detail.email}</dd>
              </div>
              <div>
                <dt>Role</dt>
                <dd>{roleLabel(detail.staff_role)}</dd>
              </div>
              <div>
                <dt>Administrator</dt>
                <dd>{detail.is_administrator ? 'Yes' : 'No'}</dd>
              </div>
              <div>
                <dt>Active</dt>
                <dd>{detail.active ? 'Active' : 'Inactive'}</dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{detail.created_at || 'Not recorded'}</dd>
              </div>
            </dl>
          )}

          <h3>Security</h3>
          <p className="queue-sub">
            Login state and password reset. Resetting a password does not change client access or
            CRM ownership.
          </p>
          <dl className="queue-sub">
            <div>
              <dt>Login status</dt>
              <dd>{LOGIN_LABELS[detail.login_status]}</dd>
            </div>
            <div>
              <dt>Password last updated</dt>
              <dd>{detail.password_updated_at || 'Not set'}</dd>
            </div>
          </dl>
          <p>
            <button
              type="button"
              className="primary-btn"
              onClick={() => {
                clearPasswordReset()
                setError(null)
                setSuccess(null)
                setResettingPassword(true)
              }}
            >
              Set new password
            </button>
          </p>
          {resettingPassword ? (
            <form
              onSubmit={(event) => {
                event.preventDefault()
                if (reviewingPassword) {
                  void submitPasswordReset()
                } else {
                  openPasswordReview()
                }
              }}
            >
              <h3>{reviewingPassword ? 'Review password reset' : 'Set new password'}</h3>
              {reviewingPassword ? (
                <div>
                  <p className="queue-sub">
                    This replaces the password for {detail.full_name} and revokes their active
                    sessions. The new password is not shown.
                    {detail.active
                      ? ''
                      : ' This account will remain inactive until it is activated separately.'}
                  </p>
                  <p>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => setReviewingPassword(false)}
                    >
                      Back
                    </button>{' '}
                    <button type="submit" className="primary-btn" disabled={saving}>
                      Confirm password reset
                    </button>
                  </p>
                </div>
              ) : (
                <div>
                  <p className="queue-sub">
                    Password must be at least 12 characters and include letters and numbers.
                  </p>
                  <label className="setup-field">
                    <span className="setup-field-label">New password</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      aria-label="New password"
                      value={newPassword}
                      onChange={(event) => setNewPassword(event.target.value)}
                    />
                  </label>
                  <label className="setup-field">
                    <span className="setup-field-label">Confirm new password</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      aria-label="Confirm new password"
                      value={confirmNewPassword}
                      onChange={(event) => setConfirmNewPassword(event.target.value)}
                    />
                  </label>
                  <p>
                    <button type="button" className="link-btn" onClick={clearPasswordReset}>
                      Cancel
                    </button>{' '}
                    <button type="submit" className="primary-btn">
                      Review password reset
                    </button>
                  </p>
                </div>
              )}
            </form>
          ) : null}

          <h3>Client access</h3>
          <p className="queue-sub">
            Which clients this person may access. Saving client access does not change CRM
            ownership.
          </p>
          {detail.access_scope === 'all_clients' ? (
            <p className="queue-sub">
              All clients. Explicit assignments do not limit an administrator.
            </p>
          ) : (
            <p className="queue-sub">Assigned clients only.</p>
          )}
          {editing ? (
            <fieldset>
              <legend>Client access</legend>
              {editClients.length === 0 ? (
                <p className="queue-sub">No clients are available.</p>
              ) : (
                editClients.map((client) => (
                  <label key={client.client_id}>
                    <input
                      type="checkbox"
                      checked={account.clientIds.includes(client.client_id)}
                      onChange={() => toggleAccountClient(client.client_id)}
                    />{' '}
                    {client.client_name || client.client_code}
                  </label>
                ))
              )}
              <p>
                <button
                  type="button"
                  className="primary-btn"
                  disabled={saving}
                  onClick={() => void saveClients()}
                >
                  Save client access
                </button>
              </p>
            </fieldset>
          ) : detail.assignments.length === 0 ? (
            <p className="queue-sub">No explicit client assignments.</p>
          ) : (
            <div className="queue-table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>Client</th>
                    <th>Code</th>
                    <th>Assignment</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.assignments.map((assignment) => (
                    <tr key={assignment.client_id}>
                      <td>{assignment.client_name}</td>
                      <td>{assignment.client_code}</td>
                      <td>{assignment.active ? 'Active' : 'Inactive'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3>CRM ownership</h3>
          <p className="queue-sub">
            Existing CRM work assigned to this person. These counts are read-only. This screen does
            not change assigned reps, company relationships, or activity history.
          </p>
          <ul>
            <li>Company relationships assigned: {detail.crm_ownership.client_company_relationships}</li>
            <li>Contact workflows assigned: {detail.crm_ownership.contact_client_workflows}</li>
            <li>Open work queue items assigned: {detail.crm_ownership.open_work_queue_items}</li>
          </ul>

          <h3>Administration history</h3>
          <p className="queue-sub">
            Who changed account, client access, or security settings, and when.
          </p>
          {history.length === 0 ? (
            <p className="queue-sub">No administration history yet.</p>
          ) : (
            <ol>
              {history.map((event) => (
                <li key={event.id}>
                  {historyLines(event).map((line, index) => (
                    <p key={`${event.id}-${index}`}>{line}</p>
                  ))}
                  {event.created_at ? <p className="queue-sub">{event.created_at}</p> : null}
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}

      {!detail && !creating ? (
        <p>
          <button
            type="button"
            className="primary-btn"
            onClick={() => {
              setError(null)
              setSuccess(null)
              setForm(blankCreateForm())
              setReviewing(false)
              setCreating(true)
            }}
          >
            Create user
          </button>
        </p>
      ) : null}

      {!detail && creating ? (
        <div>
          <h3>{reviewing ? 'Review new user' : 'Create user'}</h3>
          {reviewing ? (
            <div>
              <p className="queue-sub">
                Review this account before creating it. The password is not shown.
              </p>
              <dl className="queue-sub">
                <div>
                  <dt>Full name</dt>
                  <dd>{form.fullName.trim()}</dd>
                </div>
                <div>
                  <dt>Email</dt>
                  <dd>{form.email.trim().toLowerCase()}</dd>
                </div>
                <div>
                  <dt>Role</dt>
                  <dd>{roleLabel(form.staffRole)}</dd>
                </div>
                <div>
                  <dt>Clients</dt>
                  <dd>{selectedClientNames.join(', ')}</dd>
                </div>
                <div>
                  <dt>Active</dt>
                  <dd>{form.active ? 'Active' : 'Inactive'}</dd>
                </div>
              </dl>
              <p>
                <button type="button" className="link-btn" onClick={() => setReviewing(false)}>
                  Back to form
                </button>{' '}
                <button
                  type="button"
                  className="primary-btn"
                  disabled={saving}
                  onClick={() => void submitCreate()}
                >
                  Confirm create user
                </button>
              </p>
            </div>
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault()
                openReview()
              }}
            >
              <label className="setup-field">
                <span className="setup-field-label">Full name</span>
                <input
                  value={form.fullName}
                  onChange={(event) => setForm({ ...form, fullName: event.target.value })}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Email</span>
                <input
                  type="email"
                  autoComplete="off"
                  value={form.email}
                  onChange={(event) => setForm({ ...form, email: event.target.value })}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Staff role</span>
                <select
                  aria-label="Staff role"
                  value={form.staffRole}
                  onChange={(event) => setForm({ ...form, staffRole: event.target.value })}
                >
                  <option value="">Select a role…</option>
                  {CREATABLE_ROLES.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <fieldset>
                <legend>Client access</legend>
                {availableClients.length === 0 ? (
                  <p className="queue-sub">No clients are available.</p>
                ) : (
                  availableClients.map((client) => (
                    <label key={client.client_id}>
                      <input
                        type="checkbox"
                        checked={form.clientIds.includes(client.client_id)}
                        onChange={() => toggleClient(client.client_id)}
                      />{' '}
                      {client.client_name || client.client_code}
                    </label>
                  ))
                )}
              </fieldset>
              <label className="setup-field">
                <span className="setup-field-label">Active</span>
                <input
                  type="checkbox"
                  aria-label="Active"
                  checked={form.active}
                  onChange={(event) => setForm({ ...form, active: event.target.checked })}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Initial password</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  aria-label="Initial password"
                  value={form.password}
                  onChange={(event) => setForm({ ...form, password: event.target.value })}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Confirm initial password</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  aria-label="Confirm initial password"
                  value={form.confirmPassword}
                  onChange={(event) =>
                    setForm({ ...form, confirmPassword: event.target.value })
                  }
                />
              </label>
              <p>
                <button type="button" className="link-btn" onClick={resetCreate}>
                  Cancel
                </button>{' '}
                <button type="submit" className="primary-btn">
                  Review user
                </button>
              </p>
            </form>
          )}
        </div>
      ) : null}

      {!detail && !creating && loading ? <p className="data-status">Loading users…</p> : null}

      {!detail && !creating && !loading ? (
        <div className="queue-table-wrap">
          <table className="queue-table">
            <thead>
              <tr>
                <th>Full name</th>
                <th>Email</th>
                <th>Role</th>
                <th>Administrator</th>
                <th>Active</th>
                <th>Allowed clients</th>
                <th>Login status</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 ? (
                <tr>
                  <td colSpan={8}>No users.</td>
                </tr>
              ) : (
                users.map((user) => (
                  <tr key={user.id}>
                    <td>{user.full_name}</td>
                    <td>{user.email}</td>
                    <td>{roleLabel(user.staff_role)}</td>
                    <td>{user.is_administrator ? 'Yes' : 'No'}</td>
                    <td>{user.active ? 'Active' : 'Inactive'}</td>
                    <td>{allowedClientsLabel(user)}</td>
                    <td>{LOGIN_LABELS[user.login_status]}</td>
                    <td>
                      <button
                        type="button"
                        className="link-btn"
                        onClick={() => void openUser(user.id)}
                      >
                        View {user.full_name}
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  )
}
