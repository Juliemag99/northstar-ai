import { Link } from 'react-router-dom'
import { useMemo, useState } from 'react'
import type { ProspectListItem } from './types/carmeco'

function display(value: string | null | undefined): string {
  return value?.trim() || '—'
}

function prospectStatus(prospect: {
  status?: string
  relationship_status?: string
}): string {
  return (prospect.status || prospect.relationship_status || '').trim()
}

export default function Research({
  activeClientId,
  activeClientName,
  prospects,
  loading = false,
}: {
  activeClientId: number | null
  activeClientName: string
  prospects: ProspectListItem[]
  loading?: boolean
}) {
  const [query, setQuery] = useState('')
  const canResearch = activeClientId != null && activeClientId > 0

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const rows = q
      ? prospects.filter((p) => {
          const hay = [
            p.company,
            p.external_record_no,
            p.city,
            p.state,
            p.primary_contact,
            prospectStatus(p),
          ]
            .join(' ')
            .toLowerCase()
          return hay.includes(q)
        })
      : prospects
    return rows.slice(0, 75)
  }, [prospects, query])

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Research</h1>
          <p>
            Research companies for {activeClientName} using NorthStar intelligence first, then
            public web sources. No automatic CRM changes — approve updates on the company
            research page.
          </p>
        </div>
      </div>

      {!canResearch ? (
        <section className="panel" aria-label="Select Working For client">
          <div className="panel-header">
            <h2>WORKING FOR CLIENT REQUIRED</h2>
          </div>
          <p>
            Select a Working For client in the sidebar to research companies in that client&apos;s
            context.
          </p>
        </section>
      ) : (
        <section className="panel panel--queue" aria-label="Research companies">
          <div className="panel-header">
            <h2>Companies</h2>
            <span className="queue-source">
              {loading ? 'Loading companies…' : `${filtered.length} shown`}
              {query.trim() ? '' : prospects.length > filtered.length ? ` of ${prospects.length}` : ''}
            </span>
          </div>

          <label className="edit-field" style={{ marginBottom: '0.85rem', maxWidth: '28rem' }}>
            <span className="edit-field__label">Find a company</span>
            <input
              className="edit-input"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Company, record no., city, contact…"
              aria-label="Find a company to research"
            />
          </label>

          {loading && prospects.length === 0 ? (
            <p className="data-status">Loading companies…</p>
          ) : filtered.length === 0 ? (
            <p className="empty-state">
              {query.trim()
                ? `No ${activeClientName} companies match that search.`
                : `No ${activeClientName} companies available to research.`}
            </p>
          ) : (
            <div className="queue-table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>Company</th>
                    <th>City</th>
                    <th>State</th>
                    <th>Status</th>
                    <th>Research</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((prospect) => {
                    const clientId = prospect.client_id || activeClientId
                    const href = `/companies/${encodeURIComponent(
                      prospect.external_record_no,
                    )}/research?client_id=${clientId}`
                    return (
                      <tr key={`${prospect.client_id}-${prospect.relationship_id || prospect.id}`}>
                        <td>
                          <Link to={href} className="company-link">
                            {prospect.company || '—'}
                          </Link>
                          <span className="queue-sub">
                            Record No. {display(prospect.external_record_no)}
                          </span>
                        </td>
                        <td>{display(prospect.city)}</td>
                        <td>{display(prospect.state)}</td>
                        <td>{display(prospectStatus(prospect))}</td>
                        <td>
                          <Link className="primary-btn" to={href}>
                            Research
                          </Link>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </>
  )
}
