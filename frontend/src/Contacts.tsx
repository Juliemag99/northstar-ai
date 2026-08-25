import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { companyWorkspaceHref, fetchContacts } from './api/carmeco'
import type { ContactListItem } from './types/carmeco'

const PAGE_SIZE = 50

function display(value: string | null | undefined): string {
  return value?.trim() || '—'
}

function contactName(row: ContactListItem): string {
  const name = (row.full_name || `${row.first_name} ${row.last_name}`).trim()
  if (name) return name
  const email = (row.email || '').trim()
  return email || '—'
}

function contactHref(row: ContactListItem, activeClientId: number | null): string {
  const clientId = row.client_id || activeClientId
  const qs = clientId != null && clientId > 0 ? `?client_id=${clientId}` : ''
  return `/contacts/${row.id}${qs}`
}

export default function Contacts({
  activeClientId,
  activeClientName,
}: {
  activeClientId: number | null
  activeClientName: string
}) {
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [page, setPage] = useState(0)
  const [contacts, setContacts] = useState<ContactListItem[]>([])
  const [total, setTotal] = useState(0)
  const [clientTotal, setClientTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const showClientColumn = activeClientId === 0

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [query])

  useEffect(() => {
    setPage(0)
  }, [debouncedQuery, activeClientId])

  useEffect(() => {
    if (activeClientId == null) {
      setContacts([])
      setTotal(0)
      setClientTotal(0)
      setLoading(false)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    const allClients = activeClientId === 0
    void fetchContacts({
      client_id: allClients ? null : activeClientId,
      all_clients: allClients,
      q: debouncedQuery,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    })
      .then((data) => {
        if (cancelled) return
        setContacts(data.contacts)
        setTotal(data.total)
        setClientTotal(data.client_total)
      })
      .catch((err) => {
        if (cancelled) return
        setContacts([])
        setTotal(0)
        setClientTotal(0)
        setError(err instanceof Error ? err.message : 'Failed to load contacts.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [activeClientId, debouncedQuery, page])

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const from = total === 0 ? 0 : safePage * PAGE_SIZE + 1
  const to = Math.min(total, (safePage + 1) * PAGE_SIZE)
  const searching = Boolean(debouncedQuery)

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Contacts</h1>
          <p>
            Search {activeClientName} CRM contacts by name, company, title, phone, or email. Open a
            contact to view the Contact Workspace.
          </p>
        </div>
      </div>

      <section className="panel panel--queue" aria-label="Contacts">
        <div className="panel-header">
          <h2>
            {loading && contacts.length === 0
              ? 'Contacts'
              : searching
                ? `${total} match${total === 1 ? '' : 'es'}`
                : `${clientTotal} contact${clientTotal === 1 ? '' : 's'}`}
          </h2>
          <span className="queue-source">
            {loading
              ? 'Loading contacts…'
              : searching
                ? `${from}–${to} of ${total} · ${clientTotal} total`
                : total === 0
                  ? activeClientName
                  : `${from}–${to} of ${clientTotal}`}
          </span>
        </div>

        <label className="edit-field" style={{ marginBottom: '0.85rem', maxWidth: '28rem' }}>
          <span className="edit-field__label">Search contacts</span>
          <input
            className="edit-input"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Name, company, title, phone, email…"
            aria-label="Search contacts"
          />
        </label>

        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : loading && contacts.length === 0 ? (
          <p className="data-status">Loading contacts…</p>
        ) : contacts.length === 0 ? (
          <p className="empty-state">
            {searching
              ? `No ${activeClientName} contacts match that search.`
              : `No ${activeClientName} contacts available.`}
          </p>
        ) : (
          <>
            <div className="queue-table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Title</th>
                    <th>Company</th>
                    {showClientColumn ? <th>Working For</th> : null}
                    <th>Phone</th>
                    <th>Email</th>
                  </tr>
                </thead>
                <tbody>
                  {contacts.map((row) => {
                    const clientId = row.client_id || activeClientId
                    return (
                      <tr key={`${row.client_id}-${row.id}`}>
                        <td>
                          <Link
                            to={contactHref(row, activeClientId)}
                            className="company-link"
                          >
                            {contactName(row)}
                          </Link>
                        </td>
                        <td>{display(row.title)}</td>
                        <td>
                          {row.company_record_no ? (
                            <>
                              <Link
                                to={companyWorkspaceHref(row.company_record_no, clientId)}
                                className="company-link"
                              >
                                {display(row.company_name)}
                              </Link>
                              <span className="queue-sub">
                                Record No. {row.company_record_no}
                              </span>
                            </>
                          ) : (
                            display(row.company_name)
                          )}
                        </td>
                        {showClientColumn ? <td>{display(row.client_name)}</td> : null}
                        <td>{display(row.phone || row.alt_phone)}</td>
                        <td>{display(row.email)}</td>
                      </tr>
                    )
                  })}
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
