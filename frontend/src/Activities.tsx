import { Link } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { companyWorkspaceHref, fetchClientActivities } from './api/carmeco'
import type { ActivityTimelineRow } from './types/carmeco'

const PAGE_SIZE = 50

function display(value: string | null | undefined): string {
  return (value || '').trim() || '—'
}

function formatWhen(value: string): string {
  const text = (value || '').trim()
  if (!text) return '—'
  const iso = text.includes('T') ? text : text.replace(' ', 'T')
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return text
  return dt.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function companyHref(row: ActivityTimelineRow): string {
  if (row.external_record_no) {
    return companyWorkspaceHref(row.external_record_no, row.client_id)
  }
  return '#'
}

function contactHref(row: ActivityTimelineRow): string {
  if (row.contact_id != null && row.contact_id > 0) {
    return `/contacts/${row.contact_id}?client_id=${row.client_id}`
  }
  return companyHref(row)
}

export default function Activities({
  activeClientId,
  activeClientName,
}: {
  activeClientId: number | null
  activeClientName: string
}) {
  const scoped = activeClientId != null && activeClientId > 0
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [activityType, setActivityType] = useState('')
  const [assignedUser, setAssignedUser] = useState('')
  const [company, setCompany] = useState('')
  const [contact, setContact] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [debouncedCompany, setDebouncedCompany] = useState('')
  const [debouncedContact, setDebouncedContact] = useState('')
  const [page, setPage] = useState(0)
  const [items, setItems] = useState<ActivityTimelineRow[]>([])
  const [total, setTotal] = useState(0)
  const [types, setTypes] = useState<string[]>([])
  const [users, setUsers] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [query])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedCompany(company.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [company])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedContact(contact.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [contact])

  useEffect(() => {
    setPage(0)
  }, [
    debouncedQuery,
    activityType,
    assignedUser,
    debouncedCompany,
    debouncedContact,
    dateFrom,
    dateTo,
    activeClientId,
  ])

  useEffect(() => {
    if (!scoped || activeClientId == null) {
      setItems([])
      setTotal(0)
      setLoading(false)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    void fetchClientActivities(activeClientId, {
      activity_type: activityType || undefined,
      assigned_user: assignedUser || undefined,
      company: debouncedCompany || undefined,
      contact: debouncedContact || undefined,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      q: debouncedQuery || undefined,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    })
      .then((data) => {
        if (cancelled) return
        setItems(data.items)
        setTotal(data.total)
        setTypes(data.activity_types)
        setUsers(data.users)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setItems([])
        setTotal(0)
        setError(err instanceof Error ? err.message : 'Failed to load activities.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [
    scoped,
    activeClientId,
    activityType,
    assignedUser,
    debouncedCompany,
    debouncedContact,
    dateFrom,
    dateTo,
    debouncedQuery,
    page,
  ])

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const typeOptions = useMemo(() => types, [types])

  if (!scoped) {
    return (
      <div className="page-heading">
        <h1>Activities</h1>
        <p>Select an Active Client to view that client’s activity timeline.</p>
      </div>
    )
  }

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Activities</h1>
          <p>
            {activeClientName} timeline — calls, notes, status changes, follow-ups, tasks, and
            appointments for this Working For client only.
          </p>
        </div>
      </div>

      <section className="panel panel--queue" aria-label="Activities">
        <div className="panel-header">
          <h2>
            {total} activit{total === 1 ? 'y' : 'ies'}
          </h2>
          <span className="queue-source">Active Client · newest first</span>
        </div>
        <div className="milestone-form-grid" style={{ margin: '0.75rem 0' }}>
          <label className="edit-field">
            <span className="edit-field__label">Search</span>
            <input
              className="edit-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Notes, company, contact, user…"
            />
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Activity type</span>
            <select
              className="edit-select"
              value={activityType}
              onChange={(e) => setActivityType(e.target.value)}
            >
              <option value="">All types</option>
              {typeOptions.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Assigned rep / user</span>
            <select
              className="edit-select"
              value={assignedUser}
              onChange={(e) => setAssignedUser(e.target.value)}
            >
              <option value="">All users</option>
              {users.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Company</span>
            <input
              className="edit-input"
              value={company}
              onChange={(e) => setCompany(e.target.value)}
              placeholder="Company name or Record No."
            />
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Contact</span>
            <input
              className="edit-input"
              value={contact}
              onChange={(e) => setContact(e.target.value)}
              placeholder="Contact name"
            />
          </label>
          <label className="edit-field">
            <span className="edit-field__label">From</span>
            <input
              className="edit-input"
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
            />
          </label>
          <label className="edit-field">
            <span className="edit-field__label">To</span>
            <input
              className="edit-input"
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
            />
          </label>
        </div>
        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : null}
        {loading ? <p className="queue-sub">Loading activities…</p> : null}
        {!loading && items.length === 0 ? (
          <p className="empty-state">No {activeClientName} activities match these filters.</p>
        ) : (
          <>
            <div className="queue-table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>Date / time</th>
                    <th>Type</th>
                    <th>User</th>
                    <th>Company</th>
                    <th>Contact</th>
                    <th>Status / outcome</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <tr key={row.item_key}>
                      <td>{formatWhen(row.activity_at)}</td>
                      <td>{display(row.activity_type)}</td>
                      <td>{display(row.user_name)}</td>
                      <td>
                        {row.external_record_no ? (
                          <Link to={companyHref(row)} className="company-link">
                            {display(row.company_name)}
                          </Link>
                        ) : (
                          display(row.company_name)
                        )}
                      </td>
                      <td>
                        {row.contact_id != null && row.contact_id > 0 ? (
                          <Link to={contactHref(row)} className="company-link">
                            {display(row.contact_name)}
                          </Link>
                        ) : (
                          display(row.contact_name)
                        )}
                      </td>
                      <td>
                        {display(row.outcome || row.status)}
                      </td>
                      <td className="activity-notes">{display(row.notes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {total > PAGE_SIZE ? (
              <div className="heading-controls" style={{ marginTop: '0.85rem' }}>
                <button
                  type="button"
                  className="link-btn"
                  disabled={safePage <= 0 || loading}
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                >
                  Previous
                </button>
                <span className="queue-source">
                  Page {safePage + 1} of {pageCount}
                </span>
                <button
                  type="button"
                  className="link-btn"
                  disabled={safePage + 1 >= pageCount || loading}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next
                </button>
              </div>
            ) : null}
          </>
        )}
      </section>
    </>
  )
}
