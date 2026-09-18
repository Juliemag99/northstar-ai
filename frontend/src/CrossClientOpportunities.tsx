import { Link, useSearchParams } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { apiFetch } from './api/http'
import {
  addOpportunityToTarget,
  companyWorkspaceHref,
  dismissOpportunity,
  fetchClientStatuses,
  fetchCrossClientOpportunities,
  markOpportunityReviewed,
} from './api/carmeco'
import CampaignRoutePrompt, { type CampaignRouteOffer } from './CampaignRoutePrompt'
import type { ActiveClient, CrossClientOpportunity } from './types/carmeco'

type TargetClient = { id: number; name: string }

const SIGNAL_TYPE_OPTIONS = [
  'Purchase Order',
  'Quote',
  'Appointment Set',
  'WebLead',
  'Hot',
]

const DISMISS_REASONS = [
  'Already pursuing',
  'Not a fit for this client',
  'Wrong location/facility',
  'Duplicate',
  'Do not pursue',
  'Other',
]

const REVIEW_FILTERS = [
  { value: '', label: 'All active' },
  { value: 'New Opportunity', label: 'New Opportunity' },
  { value: 'Added to Work Queue', label: 'Added to Work Queue' },
  { value: 'Reviewed', label: 'Reviewed' },
  { value: 'Dismissed', label: 'Dismissed' },
]

const SCORE_FILTERS = [
  { value: '', label: 'Any score' },
  { value: '75', label: '75+' },
  { value: '40', label: '40+' },
  { value: '12', label: '12+' },
]

function signalClass(signal: string): string {
  const normalized = signal.toLowerCase().replace(/\s+/g, '-')
  if (normalized.includes('appointment')) return 'appointment'
  if (normalized.includes('quote')) return 'quote'
  if (normalized.includes('purchase')) return 'purchase-order'
  if (normalized.includes('web')) return 'weblead'
  if (normalized.includes('hot')) return 'hot'
  return 'default'
}

function signalLabel(signal: string): string {
  if (signal.toLowerCase().includes('appointment')) return 'Appointment'
  return signal
}

function display(value: string | null | undefined): string {
  return value?.trim() || '—'
}

function uniqueSignalTypes(opportunity: CrossClientOpportunity): string[] {
  const fromTypes = opportunity.signal_types?.length
    ? opportunity.signal_types
    : opportunity.signal_history.map((s) => s.milestone_type)
  const seen = new Set<string>()
  const out: string[] = []
  for (const type of fromTypes) {
    const key = type.trim()
    if (!key || seen.has(key)) continue
    seen.add(key)
    out.push(key)
  }
  return out
}

function signalsBySource(opportunity: CrossClientOpportunity): Array<{ client: string; types: string[] }> {
  const map = new Map<string, Set<string>>()
  for (const signal of opportunity.signal_history) {
    const client = signal.client_name?.trim() || 'NorthStar client'
    if (!map.has(client)) map.set(client, new Set())
    map.get(client)!.add(signal.milestone_type)
  }
  if (map.size === 0) {
    for (const name of opportunity.source_client_names || []) {
      map.set(name, new Set(uniqueSignalTypes(opportunity)))
    }
  }
  return [...map.entries()].map(([client, types]) => ({
    client,
    types: [...types],
  }))
}

function loadTargetClients(activeClient: ActiveClient | null): Promise<TargetClient[]> {
  return apiFetch('/api/auth/me')
    .then((response) => {
      if (!response.ok) throw new Error('Unable to load users')
      return response.json() as Promise<Record<string, unknown>>
    })
    .then((payload) => {
      const user = (payload.user ?? payload) as Record<string, unknown>
      const items = Array.isArray(user.clients)
        ? user.clients
        : Array.isArray(payload.clients)
          ? payload.clients
          : []
      return items
        .map((item) => {
          const record = item as Record<string, unknown>
          return { id: Number(record.id ?? record.client_id), name: String(record.name ?? record.client_name ?? '') }
        })
        .filter((item) => Number.isFinite(item.id) && item.name)
    })
    .catch(() => {
      const id = Number(activeClient?.client_id ?? activeClient?.id)
      return Number.isFinite(id) && id > 0
        ? [{ id, name: activeClient?.name || 'Active client' }]
        : []
    })
}

export default function CrossClientOpportunities({ client }: { client: ActiveClient | null }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [clients, setClients] = useState<TargetClient[]>([])
  const [targetClientId, setTargetClientId] = useState<number | null>(null)
  const [opportunities, setOpportunities] = useState<CrossClientOpportunity[]>([])
  const [dataNote, setDataNote] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [campaignRouteOffer, setCampaignRouteOffer] = useState<CampaignRouteOffer | null>(null)
  const [otherClientId, setOtherClientId] = useState<number | null>(null)
  const [targetStatus, setTargetStatus] = useState('')
  const [targetStatusOptions, setTargetStatusOptions] = useState<string[]>([])
  const [signalType, setSignalType] = useState('')
  const [minScore, setMinScore] = useState('')
  const [reviewStatus, setReviewStatus] = useState('')
  const [dismissingId, setDismissingId] = useState<number | null>(null)
  const [dismissReason, setDismissReason] = useState(DISMISS_REASONS[0])
  const [expandedWhy, setExpandedWhy] = useState<Record<number, boolean>>({})

  useEffect(() => {
    let cancelled = false
    void loadTargetClients(client).then((items) => {
      if (cancelled) return
      setClients(items)
      const fromUrl = Number(searchParams.get('target_client_id'))
      const activeId = Number(client?.client_id)
      const preferred =
        (Number.isFinite(fromUrl) && fromUrl > 0 && items.some((item) => item.id === fromUrl)
          ? fromUrl
          : null) ??
        items.find((item) => item.id === activeId)?.id ??
        items[0]?.id ??
        null
      setTargetClientId(preferred)
    })
    return () => {
      cancelled = true
    }
  }, [client, searchParams])

  useEffect(() => {
    let cancelled = false
    if (targetClientId == null) {
      setTargetStatusOptions([])
      return
    }
    void fetchClientStatuses(targetClientId)
      .then((items) => {
        if (!cancelled) setTargetStatusOptions(items)
      })
      .catch(() => {
        if (!cancelled) setTargetStatusOptions([])
      })
    return () => {
      cancelled = true
    }
  }, [targetClientId])

  useEffect(() => {
    let cancelled = false
    async function load() {
      if (targetClientId == null) {
        setOpportunities([])
        setLoading(false)
        return
      }
      setLoading(true)
      setError(null)
      try {
        const result = await fetchCrossClientOpportunities({
          target_client_id: targetClientId,
          signal_type: signalType || undefined,
          other_client_id: otherClientId,
          target_status: targetStatus || undefined,
          review_status: reviewStatus || undefined,
          min_score: minScore ? Number(minScore) : null,
          include_dismissed: reviewStatus === 'Dismissed',
        })
        if (cancelled) return
        setOpportunities(result.opportunities)
        setDataNote(result.data_note)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load opportunities.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [targetClientId, signalType, otherClientId, targetStatus, reviewStatus, minScore])

  const otherClientOptions = useMemo(
    () => clients.filter((item) => item.id !== targetClientId),
    [clients, targetClientId],
  )

  const sorted = useMemo(
    () => [...opportunities].sort((a, b) => {
      if (b.opportunity_score !== a.opportunity_score) {
        return b.opportunity_score - a.opportunity_score
      }
      return a.company_name.localeCompare(b.company_name, undefined, { sensitivity: 'base' })
    }),
    [opportunities],
  )

  function selectTarget(nextId: number | null) {
    setTargetClientId(nextId)
    const next = new URLSearchParams(searchParams)
    if (nextId != null && nextId > 0) next.set('target_client_id', String(nextId))
    else next.delete('target_client_id')
    setSearchParams(next, { replace: true })
  }

  async function refreshAfterAction() {
    const result = await fetchCrossClientOpportunities({
      target_client_id: targetClientId,
      signal_type: signalType || undefined,
      other_client_id: otherClientId,
      target_status: targetStatus || undefined,
      review_status: reviewStatus || undefined,
      min_score: minScore ? Number(minScore) : null,
      include_dismissed: reviewStatus === 'Dismissed',
    })
    setOpportunities(result.opportunities)
    setDataNote(result.data_note)
  }

  async function addOpportunity(opportunity: CrossClientOpportunity) {
    if (targetClientId == null) return
    try {
      const result = await addOpportunityToTarget({
        target_client_id: targetClientId,
        company_id: opportunity.company_id,
        status: 'New',
        opportunity_score: opportunity.opportunity_score,
        source_summary: (opportunity.source_client_names || []).join(', '),
      })
      setMessage(result.message || 'Added to Work Queue')
      setDismissingId(null)
      await refreshAfterAction()
      setCampaignRouteOffer({
        clientId: targetClientId,
        companyId: opportunity.company_id,
        source: 'cross-client',
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add to Work Queue.')
    }
  }

  async function markReviewed(opportunity: CrossClientOpportunity) {
    if (targetClientId == null) return
    try {
      const result = await markOpportunityReviewed({
        target_client_id: targetClientId,
        company_id: opportunity.company_id,
      })
      setMessage(result.message || 'Marked Reviewed.')
      await refreshAfterAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not mark Reviewed.')
    }
  }

  async function confirmDismiss(opportunity: CrossClientOpportunity) {
    if (targetClientId == null) return
    try {
      const result = await dismissOpportunity({
        target_client_id: targetClientId,
        company_id: opportunity.company_id,
        reason: dismissReason,
      })
      setMessage(result.message || 'Opportunity dismissed for target client.')
      setDismissingId(null)
      await refreshAfterAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not dismiss opportunity.')
    }
  }

  return (
    <>
      <CampaignRoutePrompt
        offer={campaignRouteOffer}
        onClose={() => setCampaignRouteOffer(null)}
      />
      <div className="page-heading page-heading--split">
        <div>
          <h1>Cross-Client Opportunities</h1>
          <p>Actionable prospecting from NorthStar experience at other assigned clients.</p>
        </div>
      </div>

      <section className="panel opportunity-target-bar" aria-label="Target client">
        <label className="edit-field opportunity-target-field">
          <span className="edit-field__label">Find Opportunities For:</span>
          <select
            className="edit-select"
            value={targetClientId ?? ''}
            onChange={(event) => selectTarget(event.target.value ? Number(event.target.value) : null)}
          >
            {clients.length === 0 && <option value="">Select client</option>}
            {clients.map((item) => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </select>
        </label>
        <p className="opportunity-safety-note">
          Cross-client signals inform prioritization only. They do not change target status, mark Hot,
          create calls/appointments, or copy private notes.
        </p>
      </section>

      <section className="panel opportunity-filters" aria-label="Cross-client opportunity filters">
        <div className="opportunity-filter-grid">
          <label className="edit-field">
            <span className="edit-field__label">Source Client</span>
            <select
              className="edit-select"
              value={otherClientId ?? ''}
              onChange={(event) => setOtherClientId(event.target.value ? Number(event.target.value) : null)}
            >
              <option value="">Any</option>
              {otherClientOptions.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Signal</span>
            <select className="edit-select" value={signalType} onChange={(event) => setSignalType(event.target.value)}>
              <option value="">Any</option>
              {SIGNAL_TYPE_OPTIONS.map((item) => (
                <option key={item} value={item}>{signalLabel(item)}</option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Opportunity Score</span>
            <select className="edit-select" value={minScore} onChange={(event) => setMinScore(event.target.value)}>
              {SCORE_FILTERS.map((item) => (
                <option key={item.label} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Target Status</span>
            <select className="edit-select" value={targetStatus} onChange={(event) => setTargetStatus(event.target.value)}>
              <option value="">Any</option>
              {targetStatusOptions.map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">New / Reviewed / Added / Dismissed</span>
            <select className="edit-select" value={reviewStatus} onChange={(event) => setReviewStatus(event.target.value)}>
              {REVIEW_FILTERS.map((item) => (
                <option key={item.label} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
        </div>
      </section>

      {message && <p className="data-status opportunity-message" role="status">{message}</p>}
      {error && <p className="data-status data-status--error" role="alert">{error}</p>}

      <section className="panel panel--queue" aria-label="Cross-client opportunities">
        <div className="panel-header">
          <h2>
            {loading
              ? 'Loading opportunities…'
              : `${sorted.length} opportunit${sorted.length === 1 ? 'y' : 'ies'}`}
          </h2>
          <span className="queue-sub">Sorted by Opportunity Score (high → low)</span>
        </div>

        {!loading && sorted.length === 0 ? (
          <p className="empty-state">{dataNote || 'No cross-client opportunities match these filters.'}</p>
        ) : (
          <div className="opportunity-card-list">
            {sorted.map((opportunity) => {
              const signals = uniqueSignalTypes(opportunity)
              const sources = opportunity.source_client_names?.length
                ? opportunity.source_client_names
                : opportunity.other_clients.map((c) => c.client_name).filter(Boolean)
              const whyGroups = signalsBySource(opportunity)
              const whyOpen = Boolean(expandedWhy[opportunity.company_id])
              const href = companyWorkspaceHref(
                opportunity.external_record_no,
                targetClientId,
              )
              return (
                <article
                  key={`${opportunity.company_id}-${opportunity.external_record_no}`}
                  className="opportunity-card"
                >
                  <div className="opportunity-card__header">
                    <div>
                      <Link className="company-link opportunity-card__company" to={href}>
                        {display(opportunity.company_name)}
                      </Link>
                      <div className="opportunity-card__meta">
                        <span>Target: <strong>{display(opportunity.target_client_name)}</strong></span>
                        <span>
                          {display(opportunity.target_client_name).split(' ')[0]} Status:{' '}
                          <strong>{display(opportunity.target_client_status)}</strong>
                        </span>
                        <span className={`review-status review-status--${(opportunity.review_status || 'new').toLowerCase().replace(/\s+/g, '-')}`}>
                          {display(opportunity.review_status || 'New Opportunity')}
                        </span>
                      </div>
                    </div>
                    <div className="opportunity-card__score">
                      <span className="score-pill">
                        <strong>{opportunity.opportunity_score}</strong> {display(opportunity.score_label)}
                      </span>
                    </div>
                  </div>

                  <dl className="opportunity-card__facts">
                    <div>
                      <dt>NorthStar Experience</dt>
                      <dd>{sources.join(', ') || '—'}</dd>
                    </div>
                    <div>
                      <dt>Source Client</dt>
                      <dd>{sources.join(', ') || '—'}</dd>
                    </div>
                    <div>
                      <dt>Signals</dt>
                      <dd>
                        <div className="signal-history">
                          {signals.map((type) => (
                            <span
                              key={type}
                              className={`opportunity-badge opportunity-badge--${signalClass(type)}`}
                            >
                              {signalLabel(type)}
                            </span>
                          ))}
                        </div>
                      </dd>
                    </div>
                    <div>
                      <dt>Why</dt>
                      <dd>{display(opportunity.why_summary)}</dd>
                    </div>
                    <div>
                      <dt>Recommended Action</dt>
                      <dd>{display(opportunity.recommended_action)}</dd>
                    </div>
                  </dl>

                  <details
                    className="opportunity-why"
                    open={whyOpen}
                    onToggle={(event) => {
                      const open = (event.target as HTMLDetailsElement).open
                      setExpandedWhy((current) => ({ ...current, [opportunity.company_id]: open }))
                    }}
                  >
                    <summary>Why NorthStar Flagged This</summary>
                    <div className="opportunity-why__body">
                      <p>
                        <strong>{display(opportunity.company_name)}</strong>
                        {' · '}
                        Target: <strong>{display(opportunity.target_client_name)}</strong>
                        {' · '}
                        Status: <strong>{display(opportunity.target_client_status)}</strong>
                      </p>
                      <p>
                        Source:{' '}
                        <strong>{sources.join(', ') || 'Other NorthStar client'}</strong>
                      </p>
                      <div className="signal-history" style={{ marginBottom: '0.65rem' }}>
                        {signals.map((type) => (
                          <span
                            key={`why-top-${type}`}
                            className={`opportunity-badge opportunity-badge--${signalClass(type)}`}
                          >
                            {signalLabel(type)}
                          </span>
                        ))}
                      </div>
                      <p>{display(opportunity.why_summary)}</p>
                      {whyGroups.map((group) => (
                        <div key={group.client} className="opportunity-why__group">
                          <strong>{group.client}:</strong>
                          <div className="signal-history">
                            {group.types.map((type) => (
                              <span
                                key={`${group.client}-${type}`}
                                className={`opportunity-badge opportunity-badge--${signalClass(type)}`}
                              >
                                {signalLabel(type)}
                              </span>
                            ))}
                          </div>
                        </div>
                      ))}
                      <p className="queue-sub">
                        Full authorized NorthStar Shared History remains available inside Company Workspace.
                        Private note text is never used as an automated signal.
                      </p>
                    </div>
                  </details>

                  <div className="opportunity-actions">
                    <Link className="link-btn" to={href}>Open Company Workspace</Link>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => void addOpportunity(opportunity)}
                      disabled={opportunity.review_status === 'Dismissed'}
                    >
                      Add to Work Queue
                    </button>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => void markReviewed(opportunity)}
                      disabled={opportunity.review_status === 'Dismissed'}
                    >
                      Mark Reviewed
                    </button>
                    <button
                      type="button"
                      className="link-btn link-btn--danger"
                      onClick={() => {
                        setDismissingId(opportunity.company_id)
                        setDismissReason(DISMISS_REASONS[0])
                      }}
                    >
                      Dismiss Opportunity
                    </button>
                  </div>

                  {dismissingId === opportunity.company_id && (
                    <div className="opportunity-dismiss-panel">
                      <label className="edit-field">
                        <span className="edit-field__label">Dismiss reason (for {display(opportunity.target_client_name)})</span>
                        <select
                          className="edit-select"
                          value={dismissReason}
                          onChange={(event) => setDismissReason(event.target.value)}
                        >
                          {DISMISS_REASONS.map((reason) => (
                            <option key={reason} value={reason}>{reason}</option>
                          ))}
                        </select>
                      </label>
                      <div className="opportunity-actions">
                        <button
                          type="button"
                          className="link-btn link-btn--danger"
                          onClick={() => void confirmDismiss(opportunity)}
                        >
                          Confirm Dismiss
                        </button>
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() => setDismissingId(null)}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </article>
              )
            })}
          </div>
        )}
      </section>
    </>
  )
}
