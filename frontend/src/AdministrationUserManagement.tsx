import { useEffect, useState } from 'react'
import {
  createAdminUser,
  fetchAdminUser,
  fetchAdminUsers,
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

export default function AdministrationUserManagement({
  availableClients,
}: {
  availableClients: DirectoryClient[]
}) {
  const [users, setUsers] = useState<AdminUserSummary[]>([])
  const [detail, setDetail] = useState<AdminUserDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState(blankCreateForm)

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
            <button type="button" className="link-btn" onClick={() => setDetail(null)}>
              Back to users
            </button>
          </p>
          <h3>{detail.full_name}</h3>
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
              <dt>Login status</dt>
              <dd>{LOGIN_LABELS[detail.login_status]}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{detail.created_at || 'Not recorded'}</dd>
            </div>
            <div>
              <dt>Password last updated</dt>
              <dd>{detail.password_updated_at || 'Not set'}</dd>
            </div>
          </dl>

          <h3>Client access</h3>
          {detail.access_scope === 'all_clients' ? (
            <p className="queue-sub">All clients</p>
          ) : (
            <p className="queue-sub">Assigned clients only.</p>
          )}
          {detail.assignments.length === 0 ? (
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
            CRM ownership is separate from client access. These counts show work still assigned
            to this person. This screen does not change assigned reps, company relationships, or
            activity history.
          </p>
          <ul>
            <li>Company relationships assigned: {detail.crm_ownership.client_company_relationships}</li>
            <li>Contact workflows assigned: {detail.crm_ownership.contact_client_workflows}</li>
            <li>Open work queue items assigned: {detail.crm_ownership.open_work_queue_items}</li>
          </ul>
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
