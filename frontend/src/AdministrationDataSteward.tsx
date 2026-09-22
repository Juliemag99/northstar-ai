import { useEffect, useMemo, useState } from 'react'
import {
  fetchStewardCompany,
  fetchStewardMeta,
  fetchStewardProvenance,
  previewStewardCompanyAmend,
  saveStewardCompanyAmend,
  searchStewardCompanies,
  type StewardCandidate,
  type StewardCompany,
  type StewardPreview,
  type StewardProvenanceEvent,
} from './api/dataSteward'

type StewardSection = 'companies' | 'audit'

const FIELD_LABELS: Record<string, string> = {
  company_name: 'Company name',
  address: 'Address',
  city: 'City',
  state: 'State',
  zip: 'ZIP',
  website: 'Website',
  phone: 'Phone',
  phone_extension: 'Extension',
}

const EMPTY_FORM = {
  company_name: '',
  address: '',
  city: '',
  state: '',
  zip: '',
  website: '',
  phone: '',
  phone_extension: '',
  reason: '',
}

function sourceClass(source: string): string {
  const key = source.toUpperCase()
  if (key.includes('MANUAL')) return 'steward-source steward-source--manual'
  if (key.includes('LEADMASTER')) return 'steward-source steward-source--leadmaster'
  if (key.includes('CRM')) return 'steward-source steward-source--crm'
  if (key.includes('AI')) return 'steward-source steward-source--ai'
  if (key.includes('MERGE')) return 'steward-source steward-source--merge'
  if (key.includes('SYSTEM') || key.includes('LEGACY')) return 'steward-source steward-source--system'
  return 'steward-source'
}

export default function AdministrationDataSteward() {
  const [section, setSection] = useState<StewardSection>('companies')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<StewardCompany[]>([])
  const [selected, setSelected] = useState<StewardCompany | null>(null)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState(EMPTY_FORM)
  const [preview, setPreview] = useState<StewardPreview | null>(null)
  const [events, setEvents] = useState<StewardProvenanceEvent[]>([])
  const [schemaReady, setSchemaReady] = useState(true)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    fetchStewardMeta().catch(() => undefined)
  }, [])

  const linkedClients = selected?.linked_clients || []

  const collisionBlock = useMemo(
    () => (preview?.collisions || []).filter((row) => row.severity === 'block'),
    [preview],
  )

  async function runSearch(event?: React.FormEvent) {
    event?.preventDefault()
    setError('')
    setBusy(true)
    try {
      const rows = await searchStewardCompanies(query)
      setHits(rows)
      if (rows.length === 0) setMessage('No master companies matched that search.')
      else setMessage('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed.')
    } finally {
      setBusy(false)
    }
  }

  async function selectCompany(companyId: number) {
    setError('')
    setBusy(true)
    try {
      const company = await fetchStewardCompany(companyId)
      setSelected(company)
      setEditing(false)
      setPreview(null)
      setForm({
        company_name: company.company_name || '',
        address: company.address || '',
        city: company.city || '',
        state: company.state || '',
        zip: company.zip || '',
        website: company.website || '',
        phone: company.phone || '',
        phone_extension: company.phone_extension || '',
        reason: '',
      })
      const history = await fetchStewardProvenance({
        entity_type: 'company',
        entity_id: company.company_id,
        limit: 100,
      })
      setSchemaReady(history.schema_ready !== false)
      setEvents(history.events || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load company.')
    } finally {
      setBusy(false)
    }
  }

  async function runPreview() {
    if (!selected) return
    setError('')
    setBusy(true)
    try {
      const result = await previewStewardCompanyAmend(selected.company_id, form)
      setPreview(result)
      if (!result.reason_ok) setError('A reason is required before saving a master-data amendment.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview failed.')
    } finally {
      setBusy(false)
    }
  }

  async function runSave() {
    if (!selected || !preview) return
    setError('')
    setBusy(true)
    try {
      const result = await saveStewardCompanyAmend(selected.company_id, {
        ...form,
        expected_updated_at: preview.expected_updated_at,
        preview_fingerprint: preview.preview_fingerprint,
      })
      setMessage(
        result.noop
          ? 'No master-data fields changed.'
          : `Saved ${result.changed.length} master-data field${result.changed.length === 1 ? '' : 's'}.`,
      )
      setEditing(false)
      setPreview(null)
      await selectCompany(selected.company_id)
    } catch (err) {
      const detail = (err as { detail?: { candidates?: StewardCandidate[]; message?: string } }).detail
      if (detail && typeof detail === 'object' && Array.isArray(detail.candidates)) {
        setPreview((current) =>
          current
            ? { ...current, blocked: true, collisions: detail.candidates as StewardCandidate[] }
            : current,
        )
        setError(detail.message || 'Save blocked because another master company looks like a duplicate.')
      } else {
        setError(err instanceof Error ? err.message : 'Save failed.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="administration-section" aria-labelledby="data-steward-heading">
      <h2 id="data-steward-heading">Master Data</h2>
      <p className="queue-sub">
        Governed Master Company amendment is available to administrators. Changing these fields
        updates the shared master organization. Live archive, restore, merge, and hard delete remain
        disabled. Provenance is stored on live <code>northstar.db</code>.
      </p>
      <div className="setup-campaign-tabs" role="tablist" aria-label="Master Data">
        <button
          type="button"
          role="tab"
          aria-selected={section === 'companies'}
          className={
            section === 'companies'
              ? 'setup-campaign-tab setup-campaign-tab--active'
              : 'setup-campaign-tab'
          }
          onClick={() => setSection('companies')}
        >
          Companies
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={section === 'audit'}
          className={
            section === 'audit' ? 'setup-campaign-tab setup-campaign-tab--active' : 'setup-campaign-tab'
          }
          onClick={() => setSection('audit')}
        >
          Audit History
        </button>
      </div>
      <p className="queue-sub" role="status">
        Live archive / delete / merge / remove-from-client not enabled
      </p>
      <div className="setup-actions" style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
        <button type="button" disabled>
          Archive (not yet enabled)
        </button>
        <button type="button" disabled>
          Restore (not yet enabled)
        </button>
        <button type="button" disabled>
          Merge (not yet enabled)
        </button>
        <button type="button" disabled>
          Delete (not yet enabled)
        </button>
      </div>

      <form className="administration-import-controls" onSubmit={runSearch}>
        <label>
          Search Master Company
          <input
            aria-label="Search Master Company"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Name, Record No., or company id"
          />
        </label>
        <button type="submit" className="primary-btn" disabled={busy}>
          Search
        </button>
      </form>

      {error ? <p className="queue-sub">{error}</p> : null}
      {message ? <p className="queue-sub">{message}</p> : null}

      {hits.length > 0 ? (
        <ul className="queue-list">
          {hits.map((hit) => (
            <li key={hit.company_id}>
              <button type="button" className="link-btn" onClick={() => selectCompany(hit.company_id)}>
                {hit.company_name || `Company ${hit.company_id}`}
              </button>
              <span className="queue-sub">
                {' '}
                {hit.city}
                {hit.city && hit.state ? ', ' : ''}
                {hit.state} · RN {hit.external_record_no || '—'}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {selected ? (
        <div className="steward-company-card">
          <h3>{selected.company_name || 'Master Company'}</h3>
          <p className="queue-sub" role="note">
            MASTER COMPANY DATA. {selected.master_data_warning}
          </p>
          <dl className="steward-current-fields">
            <div>
              <dt>Record No.</dt>
              <dd>{selected.external_record_no || '—'}</dd>
            </div>
            <div>
              <dt>Address</dt>
              <dd>
                {selected.address || '—'}
                {selected.city || selected.state ? ` · ${selected.city} ${selected.state} ${selected.zip}` : ''}
              </dd>
            </div>
            <div>
              <dt>Phone</dt>
              <dd>
                {selected.phone || '—'}
                {selected.phone_extension ? ` x${selected.phone_extension}` : ''}
              </dd>
            </div>
            <div>
              <dt>Website</dt>
              <dd>{selected.website || '—'}</dd>
            </div>
          </dl>
          <h4>Linked clients</h4>
          {linkedClients.length ? (
            <ul>
              {linkedClients.map((client) => (
                <li key={client.ccr_id}>
                  {client.name} ({client.code}) · {client.status || 'no status'}
                </li>
              ))}
            </ul>
          ) : (
            <p className="queue-sub">No active client relationships.</p>
          )}
          {!editing ? (
            <button type="button" className="primary-btn" onClick={() => setEditing(true)}>
              Edit
            </button>
          ) : (
            <div className="steward-edit-form">
              <p className="queue-sub" role="alert">
                This updates the shared Master Company record and may be visible across multiple
                clients. Client status, notes, and assignment are not edited here.
              </p>
              {Object.keys(FIELD_LABELS).map((key) => (
                <label key={key}>
                  {FIELD_LABELS[key]}
                  <input
                    aria-label={FIELD_LABELS[key]}
                    value={form[key as keyof typeof form]}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, [key]: event.target.value }))
                    }
                  />
                </label>
              ))}
              <label>
                Reason for change
                <input
                  aria-label="Reason for change"
                  value={form.reason}
                  onChange={(event) => setForm((current) => ({ ...current, reason: event.target.value }))}
                  placeholder="Corrected company name"
                  required
                />
              </label>
              <div className="setup-actions">
                <button type="button" onClick={runPreview} disabled={busy || !form.reason.trim()}>
                  Preview changes
                </button>
                <button type="button" onClick={() => setEditing(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      ) : null}

      {preview ? (
        <div className="steward-preview">
          <h3>Review changes</h3>
          {preview.noop ? <p className="queue-sub">No stored values would change.</p> : null}
          {preview.changes.length ? (
            <table className="queue-table">
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Current</th>
                  <th>Proposed</th>
                </tr>
              </thead>
              <tbody>
                {preview.changes.map((row) => (
                  <tr key={row.field}>
                    <td>{FIELD_LABELS[row.field] || row.field}</td>
                    <td>{row.current || '—'}</td>
                    <td>{row.proposed || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {collisionBlock.length ? (
            <div role="alert">
              <p>Save is blocked. Another master company looks like a duplicate:</p>
              <ul>
                {collisionBlock.map((row) => (
                  <li key={row.company_id}>
                    {row.company_name} #{row.company_id} · {row.address} {row.city} {row.state} ·{' '}
                    {row.reasons.join(', ')}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <button
            type="button"
            className="primary-btn"
            onClick={runSave}
            disabled={busy || preview.blocked || !preview.reason_ok || preview.noop}
          >
            Save
          </button>
        </div>
      ) : null}

      {section === 'audit' || selected ? (
        <div className="steward-audit">
          <h3>Audit History</h3>
          {!schemaReady ? (
            <p className="queue-sub">Provenance table is not available on this database copy.</p>
          ) : selected ? (
            events.length ? (
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Field</th>
                    <th>Old</th>
                    <th>New</th>
                    <th>Actor</th>
                    <th>Source</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => (
                    <tr key={event.id}>
                      <td>{event.changed_at}</td>
                      <td>{event.field}</td>
                      <td>{event.old_value || '—'}</td>
                      <td>{event.new_value || '—'}</td>
                      <td>{event.changed_by_name || event.changed_by_user_id || '—'}</td>
                      <td>
                        <span className={sourceClass(event.source_type)}>{event.source_type}</span>
                      </td>
                      <td>{event.reason || event.action || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="queue-sub">No provenance events for this master company yet.</p>
            )
          ) : (
            <p className="queue-sub">Search and select a master company to view audit history.</p>
          )}
        </div>
      ) : null}
    </section>
  )
}
