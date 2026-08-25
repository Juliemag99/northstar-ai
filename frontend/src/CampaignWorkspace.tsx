import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  addCampaignCompany,
  addCampaignContact,
  companyWorkspaceHref,
  fetchCampaignWorkspace,
  removeCampaignCompany,
  removeCampaignContact,
  searchCampaignMembers,
} from './api/carmeco'
import type {
  CampaignCompanyRow,
  CampaignContactRow,
  CampaignWorkspace as CampaignWorkspaceData,
} from './types/carmeco'

function display(value: string | null | undefined): string {
  return (value || '').trim() || '—'
}

function companyHref(row: { external_record_no: string; client_id: number }): string {
  if (row.external_record_no) {
    return companyWorkspaceHref(row.external_record_no, row.client_id)
  }
  return '#'
}

function contactHref(row: CampaignContactRow): string {
  if (row.contact_id > 0) {
    return `/contacts/${row.contact_id}?client_id=${row.client_id}`
  }
  return companyHref(row)
}

type PendingRemoval =
  | { kind: 'company'; companyId: number; companyName: string }
  | {
      kind: 'contact'
      contactId: number
      contactName: string
      companyId: number
      companyName: string
      primary: boolean
    }

export default function CampaignWorkspace({
  campaignId,
  activeClientId,
}: {
  campaignId: number
  activeClientId: number | null
}) {
  const [data, setData] = useState<CampaignWorkspaceData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [companyQuery, setCompanyQuery] = useState('')
  const [contactQuery, setContactQuery] = useState('')
  const [companyHits, setCompanyHits] = useState<CampaignCompanyRow[]>([])
  const [contactHits, setContactHits] = useState<CampaignContactRow[]>([])
  const [adding, setAdding] = useState(false)
  const [pendingRemoval, setPendingRemoval] = useState<PendingRemoval | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    void fetchCampaignWorkspace(campaignId)
      .then((next) => {
        if (!cancelled) setData(next)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load campaign.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [campaignId])

  useEffect(() => {
    if (!campaignId) return
    const timer = window.setTimeout(() => {
      const q = companyQuery.trim() || contactQuery.trim()
      if (!q) {
        setCompanyHits([])
        setContactHits([])
        return
      }
      void searchCampaignMembers(campaignId, q)
        .then((hits) => {
          if (companyQuery.trim()) setCompanyHits(hits.companies)
          if (contactQuery.trim()) setContactHits(hits.contacts)
        })
        .catch(() => {
          setCompanyHits([])
          setContactHits([])
        })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [campaignId, companyQuery, contactQuery])

  const campaign = data?.campaign
  const backHref =
    activeClientId != null && activeClientId > 0
      ? `/campaigns?client_id=${activeClientId}`
      : '/campaigns'

  async function addCompany(companyId: number) {
    setAdding(true)
    setError(null)
    try {
      const next = await addCampaignCompany(campaignId, companyId)
      setData(next)
      setCompanyQuery('')
      setCompanyHits([])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add company.')
    } finally {
      setAdding(false)
    }
  }

  async function addContact(contactId: number) {
    setAdding(true)
    setError(null)
    try {
      const next = await addCampaignContact(campaignId, contactId)
      setData(next)
      setContactQuery('')
      setContactHits([])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add contact.')
    } finally {
      setAdding(false)
    }
  }

  const campaignName = campaign?.campaign_name || 'this campaign'
  const companyRemoval =
    pendingRemoval != null &&
    (pendingRemoval.kind === 'company' || pendingRemoval.primary)
  const confirmMessage = pendingRemoval
    ? companyRemoval
      ? `Remove ${pendingRemoval.companyName} and its campaign contacts from ${campaignName}?`
      : `Remove ${pendingRemoval.contactName} from ${campaignName}?`
    : ''

  async function confirmRemoval() {
    if (!pendingRemoval) return
    setAdding(true)
    setError(null)
    try {
      const next =
        pendingRemoval.kind === 'contact' && !pendingRemoval.primary
          ? await removeCampaignContact(campaignId, pendingRemoval.contactId)
          : await removeCampaignCompany(campaignId, pendingRemoval.companyId)
      setData(next)
      setPendingRemoval(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove campaign membership.')
    } finally {
      setAdding(false)
    }
  }

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <p className="queue-note">
            <Link to={backHref}>← Campaigns</Link>
          </p>
          <h1>{campaign ? campaign.campaign_name : 'Campaign Workspace'}</h1>
          <p>
            {campaign
              ? `${campaign.client_name} · ${campaign.status}${campaign.category ? ` · ${campaign.category}` : ''}`
              : 'Assigned companies and contacts for this campaign.'}
          </p>
        </div>
      </div>

      {error && <p className="queue-error">{error}</p>}
      {loading && <p className="queue-note">Loading campaign…</p>}

      {campaign && (
        <>
          <section className="stat-grid" aria-label="Campaign results">
            <div className="stat-card">
              <p className="stat-label">Companies</p>
              <p className="stat-value">{campaign.company_count}</p>
            </div>
            <div className="stat-card">
              <p className="stat-label">Contacts</p>
              <p className="stat-value">{campaign.contact_count}</p>
            </div>
            <Link className="stat-card stat-card--clickable" to={`/activities?client_id=${campaign.client_id}`}>
              <p className="stat-label">Calls</p>
              <p className="stat-value">{campaign.call_count}</p>
            </Link>
            <Link className="stat-card stat-card--clickable" to={`/work-queue?client_id=${campaign.client_id}`}>
              <p className="stat-label">Follow-ups</p>
              <p className="stat-value">{campaign.follow_up_count}</p>
            </Link>
            <Link className="stat-card stat-card--clickable" to={`/appointments?client_id=${campaign.client_id}`}>
              <p className="stat-label">Appointments</p>
              <p className="stat-value">{campaign.appointment_count}</p>
            </Link>
            <div className="stat-card">
              <p className="stat-label">Outcomes</p>
              <p className="stat-value">{campaign.outcome_count}</p>
            </div>
          </section>
          {data?.notes ? <p className="queue-note">{data.notes}</p> : null}

          <section className="queue-card" style={{ marginTop: '1rem' }}>
            <h2>Assigned companies</h2>
            <p className="queue-note">
              Existing companies can belong to multiple campaigns. Adding here does not create a duplicate
              master record.
            </p>
            <label className="edit-field">
              <span className="edit-field__label">Add company</span>
              <input
                className="edit-input"
                value={companyQuery}
                onChange={(e) => setCompanyQuery(e.target.value)}
                placeholder="Search this client’s companies"
                disabled={adding}
              />
            </label>
            {companyHits.length > 0 && (
              <ul className="campaign-suggest">
                {companyHits.map((hit) => (
                  <li key={hit.company_id}>
                    <button type="button" disabled={adding} onClick={() => void addCompany(hit.company_id)}>
                      {hit.company_name || '—'}
                      {hit.external_record_no ? ` · ${hit.external_record_no}` : ''}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {data && data.companies.length === 0 && <p className="queue-note">No companies assigned yet.</p>}
            {data && data.companies.length > 0 && (
              <div className="queue-table-wrap">
                <table className="queue-table">
                  <thead>
                    <tr>
                      <th>Company</th>
                      <th>Record</th>
                      <th>City</th>
                      <th>State</th>
                      <th>Status</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.companies.map((row) => (
                      <tr key={row.company_id}>
                        <td>
                          <Link to={companyHref(row)}>
                            <strong>{display(row.company_name)}</strong>
                          </Link>
                        </td>
                        <td>{display(row.external_record_no)}</td>
                        <td>{display(row.city)}</td>
                        <td>{display(row.state)}</td>
                        <td>{display(row.status)}</td>
                        <td>
                          <button
                            type="button"
                            className="link-btn"
                            disabled={adding}
                            onClick={() =>
                              setPendingRemoval({
                                kind: 'company',
                                companyId: row.company_id,
                                companyName: row.company_name || 'this company',
                              })
                            }
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="queue-card" style={{ marginTop: '1rem' }}>
            <h2>Assigned contacts</h2>
            <p className="queue-note">
              Shared contacts stay on the master contact record. Campaign membership is client-specific.
            </p>
            <label className="edit-field">
              <span className="edit-field__label">Add contact</span>
              <input
                className="edit-input"
                value={contactQuery}
                onChange={(e) => setContactQuery(e.target.value)}
                placeholder="Search this client’s contacts"
                disabled={adding}
              />
            </label>
            {contactHits.length > 0 && (
              <ul className="campaign-suggest">
                {contactHits.map((hit) => (
                  <li key={hit.contact_id}>
                    <button type="button" disabled={adding} onClick={() => void addContact(hit.contact_id)}>
                      {hit.contact_name || '—'}
                      {hit.company_name ? ` · ${hit.company_name}` : ''}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {data && data.contacts.length === 0 && <p className="queue-note">No contacts assigned yet.</p>}
            {data && data.contacts.length > 0 && (
              <div className="queue-table-wrap">
                <table className="queue-table">
                  <thead>
                    <tr>
                      <th>Contact</th>
                      <th>Title</th>
                      <th>Company</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.contacts.map((row) => (
                      <tr key={row.contact_id}>
                        <td>
                          <Link to={contactHref(row)}>
                            <strong>{display(row.contact_name)}</strong>
                          </Link>
                        </td>
                        <td>{display(row.title)}</td>
                        <td>
                          <Link to={companyHref(row)}>{display(row.company_name)}</Link>
                        </td>
                        <td>
                          <button
                            type="button"
                            className="link-btn"
                            disabled={adding}
                            onClick={() =>
                              setPendingRemoval({
                                kind: 'contact',
                                contactId: row.contact_id,
                                contactName: row.contact_name || 'this contact',
                                companyId: row.company_id,
                                companyName: row.company_name || 'this company',
                                primary: Boolean(row.is_primary_routed_contact),
                              })
                            }
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
      {pendingRemoval
        ? createPortal(
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby="campaign-remove-title"
              style={{
                position: 'fixed',
                inset: 0,
                background: 'rgba(15, 23, 42, 0.45)',
                zIndex: 240,
                display: 'flex',
                alignItems: 'flex-start',
                justifyContent: 'center',
                padding: '2rem 1rem',
              }}
            >
              <section className="panel" style={{ maxWidth: '28rem', width: '100%', margin: 0 }}>
                <div className="panel-header">
                  <h2 id="campaign-remove-title">Remove from campaign</h2>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => setPendingRemoval(null)}
                    disabled={adding}
                  >
                    Close
                  </button>
                </div>
                <p>{confirmMessage}</p>
                <div className="heading-controls" style={{ marginTop: '0.85rem' }}>
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={adding}
                    onClick={() => void confirmRemoval()}
                  >
                    {adding ? 'Removing…' : 'Remove'}
                  </button>
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={adding}
                    onClick={() => setPendingRemoval(null)}
                  >
                    Cancel
                  </button>
                </div>
              </section>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
