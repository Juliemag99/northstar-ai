import { useEffect, useState } from 'react'
import {
  fetchAdminUser,
  fetchAdminUsers,
  type AdminUserDetail,
  type AdminUserSummary,
  type LoginStatus,
} from './api/userManagement'

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

export default function AdministrationUserManagement() {
  const [users, setUsers] = useState<AdminUserSummary[]>([])
  const [detail, setDetail] = useState<AdminUserDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

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

  async function openUser(userId: number) {
    setDetailLoading(true)
    setError(null)
    try {
      setDetail(await fetchAdminUser(userId))
    } catch (err: unknown) {
      setDetail(null)
      setError(err instanceof Error ? err.message : 'Unable to load this user.')
    } finally {
      setDetailLoading(false)
    }
  }

  return (
    <section className="administration-section" aria-labelledby="user-management-heading">
      <h2 id="user-management-heading">User Management</h2>
      <p className="queue-sub">
        Read-only directory of NorthStar staff. Client access is separate from who is assigned
        CRM work.
      </p>

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

      {!detail && loading ? <p className="data-status">Loading users…</p> : null}

      {!detail && !loading ? (
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
