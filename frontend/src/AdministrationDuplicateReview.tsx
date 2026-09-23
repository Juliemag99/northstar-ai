import { useEffect, useMemo, useState } from 'react'
import {
  fetchDuplicateCandidates,
  fetchDuplicatePair,
  fetchDuplicateReviewHistory,
  saveDuplicateReview,
  type DuplicateCandidate,
  type DuplicateCompanyDetail,
  type DuplicateDisposition,
  type DuplicatePairDetail,
} from './api/duplicateReview'

const FILTERS: Array<{ id: string; label: string }> = [
  { id: 'unreviewed', label: 'Unreviewed' },
  { id: 'likely_duplicate', label: 'Likely Duplicate' },
  { id: 'not_duplicate', label: 'Not Duplicate' },
  { id: 'multi_location', label: 'Multi-Location' },
  { id: 'needs_research', label: 'Needs Research' },
  { id: 'merge_candidate', label: 'Merge Candidate' },
  { id: 'all', label: 'All' },
]

const DISPOSITIONS: Array<{ id: DuplicateDisposition; label: string }> = [
  { id: 'LIKELY_DUPLICATE', label: 'Likely Duplicate' },
  { id: 'NOT_DUPLICATE', label: 'Not Duplicate' },
  { id: 'MULTI_LOCATION', label: 'Multi-Location' },
  { id: 'NEEDS_RESEARCH', label: 'Needs Research' },
  { id: 'MERGE_CANDIDATE', label: 'Merge Candidate' },
]

function cityState(company: { city?: string; state?: string }) {
  return [company.city, company.state].filter(Boolean).join(', ')
}

function FieldRow({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="dup-field">
      <span className="dup-field-label">{label}</span>
      <span>{value === null || value === undefined || value === '' ? '—' : String(value)}</span>
    </div>
  )
}

function CompanyPane({ title, company }: { title: string; company: DuplicateCompanyDetail }) {
  const [open, setOpen] = useState<'ccrs' | 'contacts' | 'locations' | 'aliases' | 'identities' | 'campaigns' | 'history' | null>('ccrs')
  return (
    <article className="dup-pane" aria-label={title}>
      <h3>{title}</h3>
      {company.archived ? <p className="dup-archive">Archived</p> : null}
      <FieldRow label="Company ID" value={company.company_id} />
      <FieldRow label="Canonical name" value={company.company_name} />
      <FieldRow label="Address" value={company.address} />
      <FieldRow label="City" value={company.city} />
      <FieldRow label="State" value={company.state} />
      <FieldRow label="ZIP" value={company.zip} />
      <FieldRow label="Phone" value={company.phone} />
      <FieldRow label="Extension" value={company.phone_extension} />
      <FieldRow label="Website" value={company.website} />
      <FieldRow label="LeadMaster RN" value={company.external_record_no} />
      <FieldRow label="Created" value={company.created_at} />
      <FieldRow label="Updated" value={company.updated_at} />
      <button type="button" onClick={() => setOpen(open === 'aliases' ? null : 'aliases')}>
        Aliases ({company.aliases.length})
      </button>
      {open === 'aliases' ? (
        <ul>
          {company.aliases.map((row, index) => (
            <li key={index}>{String(row.alias_name || '')}</li>
          ))}
        </ul>
      ) : null}
      <button type="button" onClick={() => setOpen(open === 'identities' ? null : 'identities')}>
        Identities ({company.identities.length})
      </button>
      {open === 'identities' ? (
        <ul>
          {company.identities.map((row, index) => (
            <li key={index}>
              {String(row.source_system || '')} {String(row.source_record_no || '')}{' '}
              {String(row.source_company_name || '')}
            </li>
          ))}
        </ul>
      ) : null}
      <button type="button" onClick={() => setOpen(open === 'locations' ? null : 'locations')}>
        Locations ({company.locations.length})
      </button>
      {open === 'locations' ? (
        <ul>
          {company.locations.map((row, index) => (
            <li key={index}>
              {String(row.location_name || row.address || '')} {String(row.city || '')}{' '}
              {String(row.state || '')}
            </li>
          ))}
        </ul>
      ) : null}
      <button type="button" onClick={() => setOpen(open === 'ccrs' ? null : 'ccrs')}>
        Client relationships ({company.ccrs.length})
      </button>
      {open === 'ccrs' ? (
        <ul>
          {company.ccrs.map((row) => (
            <li key={row.ccr_id}>
              {row.client_name} — {row.active ? 'active' : 'removed'} — {row.status || '—'} —{' '}
              {row.assigned_user_name || 'Unassigned'}
              {row.hot ? ' — Hot' : ''}
              {row.follow_up_date ? ` — follow-up ${row.follow_up_date}` : ''}
              {row.external_record_no ? ` — RN ${row.external_record_no}` : ''}
            </li>
          ))}
        </ul>
      ) : null}
      <button type="button" onClick={() => setOpen(open === 'contacts' ? null : 'contacts')}>
        Contacts ({company.contacts.length})
      </button>
      {open === 'contacts' ? (
        <ul>
          {company.contacts.map((row, index) => (
            <li key={index}>
              {String(row.first_name || '')} {String(row.last_name || '')} {String(row.title || '')}{' '}
              {String(row.email || '')} {String(row.phone || '')} {String(row.external_record_no || '')}
            </li>
          ))}
        </ul>
      ) : null}
      <button type="button" onClick={() => setOpen(open === 'campaigns' ? null : 'campaigns')}>
        Campaigns ({company.campaigns.length})
      </button>
      {open === 'campaigns' ? (
        <ul>
          {company.campaigns.map((row, index) => (
            <li key={index}>
              {String(row.client_name || '')} — {String(row.campaign_name || '')}
            </li>
          ))}
        </ul>
      ) : null}
      <button type="button" onClick={() => setOpen(open === 'history' ? null : 'history')}>
        History summary
      </button>
      {open === 'history' ? (
        <ul>
          {Object.entries(company.history || {}).map(([key, value]) => (
            <li key={key}>
              {key}: {value}
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  )
}

export default function AdministrationDuplicateReview() {
  const [disposition, setDisposition] = useState('unreviewed')
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<{ total: number; pairs: DuplicateCandidate[] }>({
    total: 0,
    pairs: [],
  })
  const [selected, setSelected] = useState<DuplicatePairDetail | null>(null)
  const [history, setHistory] = useState<Array<Record<string, unknown>>>([])
  const [formDisposition, setFormDisposition] = useState<DuplicateDisposition>('LIKELY_DUPLICATE')
  const [reason, setReason] = useState('')
  const [survivorId, setSurvivorId] = useState<number | ''>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const limit = 50

  async function loadList(nextOffset = offset) {
    setBusy(true)
    setError('')
    try {
      const result = await fetchDuplicateCandidates({
        disposition,
        q: query,
        offset: nextOffset,
        limit,
      })
      setPage({ total: result.total, pairs: result.pairs })
      setOffset(nextOffset)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load duplicate candidates.')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    void loadList(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [disposition])

  async function openPair(row: DuplicateCandidate) {
    setBusy(true)
    setError('')
    try {
      const detail = await fetchDuplicatePair(row.company_a_id, row.company_b_id)
      const events = await fetchDuplicateReviewHistory(row.company_a_id, row.company_b_id)
      setSelected(detail)
      setHistory(events.events || [])
      setFormDisposition(
        detail.review.disposition === 'UNREVIEWED' ? 'LIKELY_DUPLICATE' : detail.review.disposition,
      )
      setReason(detail.review.reason || '')
      setSurvivorId(detail.review.proposed_survivor_company_id || '')
      setMessage('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load pair detail.')
    } finally {
      setBusy(false)
    }
  }

  const sourceId = useMemo(() => {
    if (!selected || survivorId === '') return ''
    const ids = [selected.company_a.company_id, selected.company_b.company_id]
    return ids.find((id) => id !== Number(survivorId)) || ''
  }, [selected, survivorId])

  async function save() {
    if (!selected) return
    if (reason.trim().length < 3) {
      setError('A short reason is required.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const saved = await saveDuplicateReview(
        selected.company_a.company_id,
        selected.company_b.company_id,
        {
          disposition: formDisposition,
          reason,
          proposed_survivor_company_id:
            formDisposition === 'MERGE_CANDIDATE' && survivorId !== '' ? Number(survivorId) : null,
          proposed_source_company_id:
            formDisposition === 'MERGE_CANDIDATE' && sourceId !== '' ? Number(sourceId) : null,
        },
      )
      setMessage(
        `${saved.disposition.replace(/_/g, ' ')} saved. ${saved.plan_only_warning} No merge approval was created.`,
      )
      await loadList(offset)
      await openPair({
        company_a_id: selected.company_a.company_id,
        company_b_id: selected.company_b.company_id,
      } as DuplicateCandidate)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save review.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="dup-review" aria-labelledby="dup-review-heading">
      <h2 id="dup-review-heading">Duplicate Review</h2>
      <p className="dup-plan-banner" role="status">
        REVIEW / PLAN ONLY. NO MERGE WILL OCCUR.
      </p>
      <p className="queue-sub">
        NorthStar can suggest possible duplicates from normalized evidence. It does not declare
        records identical and never chooses a survivor automatically.
      </p>
      <form
        className="dup-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          void loadList(0)
        }}
      >
        <label>
          Search
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Company name or RN"
          />
        </label>
        <label>
          Disposition
          <select value={disposition} onChange={(event) => setDisposition(event.target.value)}>
            {FILTERS.map((row) => (
              <option key={row.id} value={row.id}>
                {row.label}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">Search</button>
      </form>
      {error ? <p className="data-status data-status--error">{error}</p> : null}
      {message ? <p className="data-status">{message}</p> : null}
      <p className="queue-sub">
        {page.total} candidate pairs · showing {page.pairs.length} · {busy ? 'Loading…' : ''}
      </p>
      <table className="dup-table">
        <thead>
          <tr>
            <th>Company A</th>
            <th>Company B</th>
            <th>City / state</th>
            <th>Phone</th>
            <th>Website</th>
            <th>Evidence</th>
            <th>Disposition</th>
            <th>Reviewed</th>
          </tr>
        </thead>
        <tbody>
          {page.pairs.map((row) => (
            <tr key={row.pair_key}>
              <td>
                <button type="button" onClick={() => void openPair(row)}>
                  {row.company_a.company_name}
                  {row.company_a.archived ? ' (archived)' : ''}
                </button>
              </td>
              <td>
                <button type="button" onClick={() => void openPair(row)}>
                  {row.company_b.company_name}
                  {row.company_b.archived ? ' (archived)' : ''}
                </button>
              </td>
              <td>
                {cityState(row.company_a)} / {cityState(row.company_b)}
              </td>
              <td>
                {row.company_a.phone || '—'} / {row.company_b.phone || '—'}
              </td>
              <td>
                {row.company_a.website || '—'} / {row.company_b.website || '—'}
              </td>
              <td>{(row.categories || []).join(', ')}</td>
              <td>
                {row.disposition}
                {row.stale ? ' · RE-REVIEW NEEDED' : ''}
              </td>
              <td>{row.reviewed_at || 'Unreviewed'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="dup-pager">
        <button type="button" disabled={offset <= 0} onClick={() => void loadList(Math.max(0, offset - limit))}>
          Previous
        </button>
        <button
          type="button"
          disabled={offset + limit >= page.total}
          onClick={() => void loadList(offset + limit)}
        >
          Next
        </button>
      </div>

      {selected ? (
        <div className="dup-detail">
          <h3>Side-by-side review</h3>
          {selected.review.stale ? (
            <p className="dup-stale" role="status">
              REVIEW STALE · RE-REVIEW NEEDED. Identifying evidence changed after the last decision.
            </p>
          ) : null}
          <p>{selected.evidence_summary}</p>
          <p>
            Dependency: {selected.dependency.overall}
            {selected.same_client_conflicts.length
              ? ' · SAME-CLIENT CCR CONFLICT'
              : ''}
          </p>
          <div className="dup-grid">
            <CompanyPane title="MASTER COMPANY A" company={selected.company_a} />
            <CompanyPane title="MASTER COMPANY B" company={selected.company_b} />
          </div>
          {selected.contact_overlaps.length ? (
            <section>
              <h4>Contact overlaps</h4>
              <ul>
                {selected.contact_overlaps.map((row, index) => (
                  <li key={index}>
                    {String(row.kind || '')}: {String(row.name_a || '')} / {String(row.name_b || '')}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          {selected.same_client_conflicts.map((row, index) => (
            <p key={index} className="dup-conflict">
              SAME-CLIENT CCR CONFLICT · {String(row.client_name || row.client_code || '')}
            </p>
          ))}
          <fieldset>
            <legend>Review disposition</legend>
            {DISPOSITIONS.map((row) => (
              <label key={row.id} className="dup-radio">
                <input
                  type="radio"
                  name="dup-disposition"
                  value={row.id}
                  checked={formDisposition === row.id}
                  onChange={() => setFormDisposition(row.id)}
                />
                {row.label}
              </label>
            ))}
          </fieldset>
          {formDisposition === 'MERGE_CANDIDATE' ? (
            <div>
              <p className="dup-plan-banner">{selected.plan_only_warning}</p>
              <label>
                Proposed survivor
                <select
                  value={survivorId}
                  onChange={(event) =>
                    setSurvivorId(event.target.value ? Number(event.target.value) : '')
                  }
                >
                  <option value="">Select survivor</option>
                  <option value={selected.company_a.company_id}>
                    A · {selected.company_a.company_name} ({selected.company_a.company_id})
                  </option>
                  <option value={selected.company_b.company_id}>
                    B · {selected.company_b.company_name} ({selected.company_b.company_id})
                  </option>
                </select>
              </label>
              <p className="queue-sub">
                Proposed source: {sourceId || '—'} · These counts are informational. Julie selects
                the survivor.
              </p>
            </div>
          ) : null}
          <label>
            Reason
            <textarea value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <button type="button" onClick={() => void save()} disabled={busy}>
            Save review
          </button>
          <p className="queue-sub">There is no Merge or Execute action in DS-11.</p>
          {history.length ? (
            <section>
              <h4>Review history</h4>
              <ul>
                {history.map((row, index) => (
                  <li key={index}>
                    {String(row.created_at || '')} {String(row.actor_name || '')}:{' '}
                    {String(row.old_disposition || 'UNREVIEWED')} → {String(row.new_disposition || '')}{' '}
                    {String(row.reason || '')}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
