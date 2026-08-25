import { Link } from 'react-router-dom'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent, ReactNode } from 'react'
import { askNorthStar, fetchAskHistory, fetchCrossClientOpportunities, fetchProspects } from './api/carmeco'
import type {
  AskHistoryItem,
  AskNorthStarResponse,
  AskScope,
} from './types/carmeco'
import {
  askContactHref,
  askReturnSnapshotMatchesPage,
  captureAskScrollTop,
  isAskRecordHref,
  readAskReturnSnapshot,
  restoreAskScrollTop,
  saveAskReturnSnapshot,
  withAskReturnParam,
} from './askNorthStarReturn'

const ASK_SCOPE_STORAGE_KEY = 'northstar_ask_search_scope'
const ASK_PICKED_CLIENT_STORAGE_KEY = 'northstar_ask_picked_client_id'

function scoreProspectForExample(p: {
  has_purchase_order: boolean
  has_quote: boolean
  has_weblead: boolean
  is_hot: boolean
  has_appointment_set: boolean
  contact_count: number
  status: string
}): number {
  let score = 0
  if (p.has_purchase_order) score += 50
  if (p.has_quote) score += 35
  if (p.has_weblead) score += 20
  if (p.is_hot) score += 15
  if (p.has_appointment_set) score += 10
  score += Math.min(p.contact_count, 5)
  const st = (p.status || '').toLowerCase()
  if (st && st !== 'new') score += 5
  return score
}

/** Pick a company with meaningful history for the Active Client call-prep example. */
async function pickExampleCompany(clientId: number): Promise<string> {
  try {
    const opps = await fetchCrossClientOpportunities({
      target_client_id: clientId,
      include_dismissed: true,
    })
    const ranked = [...(opps.opportunities || [])].sort(
      (a, b) => (b.opportunity_score || 0) - (a.opportunity_score || 0),
    )
    const name = ranked[0]?.company_name?.trim()
    if (name) return name
  } catch {
    /* fall through to prospects */
  }
  try {
    const { prospects } = await fetchProspects({ client_id: clientId })
    const ranked = [...prospects].sort(
      (a, b) => scoreProspectForExample(b) - scoreProspectForExample(a),
    )
    const name = ranked[0]?.company?.trim()
    if (name) return name
  } catch {
    /* ignore */
  }
  return 'Whirlpool'
}

/** Max 5 examples from current scope + Active Client name (+ example company). */
function buildExampleQuestions(
  scope: AskScope,
  clientName: string,
  exampleCompany: string,
): string[] {
  const name = clientName.trim()
  const company = exampleCompany.trim() || 'Whirlpool'

  if (scope === 'active_client' && name) {
    return [
      'What should I work next?',
      `What should I know before I call ${company}?`,
      `Which ${name} prospects need a next action?`,
      'Which companies have received a quote?',
      'Find notes mentioning stamping.',
    ]
  }

  if (scope === 'active_client') {
    return []
  }

  return [
    'What do we know about Whirlpool?',
    'Which companies have received a quote?',
    'Find notes mentioning stamping.',
    'Show me cross-client opportunities.',
    'Which companies have appointments but no quote?',
  ]
}

function readStoredAskScope(fallback: AskScope): AskScope {
  try {
    const raw = window.localStorage.getItem(ASK_SCOPE_STORAGE_KEY)
    if (raw === 'all' || raw === 'active_client') return raw
  } catch {
    /* ignore */
  }
  return fallback
}

function writeStoredAskScope(scope: AskScope) {
  try {
    window.localStorage.setItem(ASK_SCOPE_STORAGE_KEY, scope)
  } catch {
    /* ignore */
  }
}

function readStoredPickedClientId(): number | null {
  try {
    const raw = window.localStorage.getItem(ASK_PICKED_CLIENT_STORAGE_KEY)
    if (!raw) return null
    const id = Number(raw)
    return Number.isFinite(id) && id > 0 ? id : null
  } catch {
    return null
  }
}

function writeStoredPickedClientId(clientId: number | null) {
  try {
    if (clientId == null || clientId <= 0) {
      window.localStorage.removeItem(ASK_PICKED_CLIENT_STORAGE_KEY)
    } else {
      window.localStorage.setItem(ASK_PICKED_CLIENT_STORAGE_KEY, String(clientId))
    }
  } catch {
    /* ignore */
  }
}

function badgeClass(label: string): string {
  const key = label.toLowerCase()
  if (key.includes('purchase')) return 'purchase-order'
  if (key.includes('quote')) return 'quote'
  if (key.includes('appointment')) return 'appointment'
  if (key.includes('web')) return 'weblead'
  if (key.includes('hot')) return 'hot'
  if (key.includes('needs')) return 'needs-next'
  return 'default'
}

function display(value: string | null | undefined): string {
  return value?.trim() || '—'
}

export default function AskNorthStar({
  activeClientId = null,
  activeClientName = '',
  assignedClients = [],
}: {
  activeClientId?: number | null
  activeClientName?: string
  assignedClients?: Array<{ client_id: number; client_name: string; client_code: string }>
}) {
  const sidebarHasSpecificClient = activeClientId != null && activeClientId > 0
  const [scope, setScope] = useState<AskScope>(() => {
    const snap = readAskReturnSnapshot()
    if (snap?.scope === 'all' || snap?.scope === 'active_client') return snap.scope
    return readStoredAskScope(sidebarHasSpecificClient ? 'active_client' : 'all')
  })
  // When sidebar is All My Clients, user may pick a client for Active Client scope.
  const [pickedClientId, setPickedClientId] = useState<number | null>(() => {
    const snap = readAskReturnSnapshot()
    if (snap) return snap.pickedClientId
    return readStoredPickedClientId()
  })
  const [question, setQuestion] = useState(() => readAskReturnSnapshot()?.question ?? '')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [answer, setAnswer] = useState<AskNorthStarResponse | null>(
    () => readAskReturnSnapshot()?.answer ?? null,
  )
  const [history, setHistory] = useState<AskHistoryItem[]>([])
  const [exampleCompany, setExampleCompany] = useState('Whirlpool')
  const didRestoreScroll = useRef(false)

  // Persist Search Scope across navigation / refresh / return visits.
  useEffect(() => {
    writeStoredAskScope(scope)
  }, [scope])

  // Persist picker choice only when sidebar is All My Clients.
  useEffect(() => {
    if (sidebarHasSpecificClient) {
      writeStoredPickedClientId(null)
      return
    }
    writeStoredPickedClientId(pickedClientId)
  }, [pickedClientId, sidebarHasSpecificClient])

  // Drop stored picker if that client is no longer assigned.
  useEffect(() => {
    if (pickedClientId == null || assignedClients.length === 0) return
    if (!assignedClients.some((c) => c.client_id === pickedClientId)) {
      setPickedClientId(null)
    }
  }, [assignedClients, pickedClientId])

  // Sidebar specific client always wins for Active Client scope.
  const effectiveClientId = useMemo(() => {
    if (scope !== 'active_client') return null
    if (sidebarHasSpecificClient) return activeClientId
    return pickedClientId
  }, [scope, sidebarHasSpecificClient, activeClientId, pickedClientId])

  const effectiveClientName = useMemo(() => {
    if (effectiveClientId == null) return ''
    if (sidebarHasSpecificClient && activeClientId === effectiveClientId) {
      return activeClientName.trim()
    }
    return (
      assignedClients.find((c) => c.client_id === effectiveClientId)?.client_name ||
      ''
    )
  }, [
    effectiveClientId,
    sidebarHasSpecificClient,
    activeClientId,
    activeClientName,
    assignedClients,
  ])

  // Sidebar client changes apply immediately for Active Client scope (picker unused).
  useEffect(() => {
    if (sidebarHasSpecificClient) {
      setPickedClientId(null)
    }
  }, [activeClientId, sidebarHasSpecificClient])

  // Never keep a previous answer's Working For when scope / Active Client changes.
  // Skip when restoring a saved Ask NorthStar visit so Back can keep the prior results.
  useEffect(() => {
    const snap = readAskReturnSnapshot()
    if (askReturnSnapshotMatchesPage(snap, scope, effectiveClientId)) return
    setAnswer(null)
    setError(null)
    if (snap) {
      saveAskReturnSnapshot({
        question: snap.question,
        scope,
        pickedClientId,
        effectiveClientId,
        answer: null,
        scrollTop: 0,
      })
    }
  }, [effectiveClientId, scope, pickedClientId])

  // Refresh call-prep example company when Active Client changes (not hard-coded).
  useEffect(() => {
    let cancelled = false
    if (scope !== 'active_client' || effectiveClientId == null || effectiveClientId <= 0) {
      setExampleCompany('Whirlpool')
      return
    }
    setExampleCompany('Whirlpool')
    void pickExampleCompany(effectiveClientId).then((name) => {
      if (!cancelled && name) setExampleCompany(name)
    })
    return () => {
      cancelled = true
    }
  }, [scope, effectiveClientId])

  const loadHistory = async () => {
    try {
      const result = await fetchAskHistory(12, {
        scope,
        active_client_id:
          scope === 'active_client' ? effectiveClientId : null,
      })
      setHistory(result.items)
    } catch {
      setHistory([])
    }
  }

  useEffect(() => {
    void loadHistory()
  }, [scope, effectiveClientId]) // eslint-disable-line react-hooks/exhaustive-deps

  const scopeDisplay = useMemo(() => {
    if (scope === 'active_client') {
      return effectiveClientName
        ? `Search Scope: Active Client · ${effectiveClientName}`
        : 'Search Scope: Active Client · select a client'
    }
    return 'Search Scope: All NorthStar'
  }, [scope, effectiveClientName])

  const exampleQuestions = useMemo(
    () => buildExampleQuestions(scope, effectiveClientName, exampleCompany),
    [scope, effectiveClientName, exampleCompany],
  )

  async function runAsk(nextQuestion: string) {
    const q = nextQuestion.trim()
    if (!q || loading) return
    if (scope === 'active_client' && (effectiveClientId == null || effectiveClientId <= 0)) {
      setError('Select a specific client for Active Client scope (sidebar or the picker below).')
      return
    }
    setLoading(true)
    setError(null)
    // Drop prior answer immediately so Working For cannot appear stale mid-request.
    setAnswer(null)
    try {
      const result = await askNorthStar({
        question: q,
        scope,
        active_client_id: scope === 'active_client' ? effectiveClientId : null,
      })
      setAnswer(result)
      setQuestion(q)
      saveAskReturnSnapshot({
        question: q,
        scope,
        pickedClientId,
        effectiveClientId,
        answer: result,
        scrollTop: captureAskScrollTop(),
      })
      await loadHistory()
    } catch (err) {
      setAnswer(null)
      setError(err instanceof Error ? err.message : 'Ask NorthStar failed.')
    } finally {
      setLoading(false)
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void runAsk(question)
  }

  function onQuestionKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Enter' || event.shiftKey) return
    event.preventDefault()
    if (loading || !question.trim()) return
    event.currentTarget.form?.requestSubmit()
  }

  useLayoutEffect(() => {
    if (didRestoreScroll.current || !answer) return
    const snap = readAskReturnSnapshot()
    if (!snap || snap.scrollTop <= 0) return
    didRestoreScroll.current = true
    const top = snap.scrollTop
    restoreAskScrollTop(top)
    const frame = window.requestAnimationFrame(() => restoreAskScrollTop(top))
    const timer = window.setTimeout(() => restoreAskScrollTop(top), 50)
    return () => {
      window.cancelAnimationFrame(frame)
      window.clearTimeout(timer)
    }
  }, [answer])

  function rememberAskReturn() {
    saveAskReturnSnapshot({
      question,
      scope,
      pickedClientId,
      effectiveClientId,
      answer,
      scrollTop: captureAskScrollTop(),
    })
  }

  function AskResultLink({
    to,
    className,
    title,
    children,
  }: {
    to: string
    className?: string
    title?: string
    children: ReactNode
  }) {
    return (
      <Link
        className={className}
        title={title}
        to={withAskReturnParam(to)}
        onClick={() => {
          if (isAskRecordHref(to)) rememberAskReturn()
        }}
      >
        {children}
      </Link>
    )
  }

  function contactResultHref(contactId: unknown, clientId?: unknown): string | null {
    const id = Number(contactId)
    if (!Number.isFinite(id) || id <= 0) return null
    const cid = Number(clientId)
    return askContactHref(id, Number.isFinite(cid) && cid > 0 ? cid : null)
  }

  const answerScopeLabel = useMemo(() => {
    if (!answer) return ''
    const isAll = answer.scope === 'all' || answer.scope_label === 'All NorthStar'
    if (isAll) return 'All NorthStar · Read only'
    const label = answer.scope_label || answer.active_client_name || 'Active Client'
    return `${label} · Read only`
  }, [answer])

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Ask NorthStar</h1>
          <p>
            Search NorthStar&apos;s company intelligence, contacts, notes, activity, and client
            history.
          </p>
        </div>
        <div className="heading-controls">
          <span className="date-chip">Phase 1 · Read only</span>
        </div>
      </div>

      <section className="panel ask-question-panel" aria-label="Ask NorthStar question">
        <div className="ask-scope-bar ask-scope-bar--inline" aria-label="Search scope">
          <div className="ask-scope-row">
            <span className="edit-field__label">SEARCH SCOPE</span>
            <div className="ask-scope-toggles" role="group" aria-label="Ask NorthStar scope">
              <button
                type="button"
                className={`ask-scope-btn${scope === 'active_client' ? ' ask-scope-btn--active' : ''}`}
                onClick={() => setScope('active_client')}
                aria-pressed={scope === 'active_client'}
              >
                Active Client
              </button>
              <button
                type="button"
                className={`ask-scope-btn${scope === 'all' ? ' ask-scope-btn--active' : ''}`}
                onClick={() => setScope('all')}
                aria-pressed={scope === 'all'}
              >
                All NorthStar
              </button>
            </div>
            {scope === 'active_client' && sidebarHasSpecificClient && (
              <span className="ask-scope-client">{activeClientName.trim() || 'Active Client'}</span>
            )}
            {scope === 'active_client' && !sidebarHasSpecificClient && (
              <label className="ask-scope-pick">
                <span className="sr-only">Choose client</span>
                <select
                  className="edit-select"
                  value={pickedClientId ?? ''}
                  onChange={(event) =>
                    setPickedClientId(event.target.value ? Number(event.target.value) : null)
                  }
                >
                  <option value="">Select client…</option>
                  {assignedClients.map((c) => (
                    <option key={c.client_id} value={c.client_id}>
                      {c.client_name}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <p className="queue-sub ask-scope-status">{scopeDisplay}</p>
        </div>

        <form className="ask-form" onSubmit={onSubmit}>
          <label className="sr-only" htmlFor="ask-northstar-input">
            Ask NorthStar anything
          </label>
          <textarea
            id="ask-northstar-input"
            className="ask-input"
            rows={3}
            placeholder="Ask NorthStar anything..."
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={onQuestionKeyDown}
            aria-keyshortcuts="Enter"
          />
          <div className="ask-form-actions">
            <button
              type="submit"
              className="primary-btn ask-submit-btn"
              disabled={
                loading ||
                !question.trim() ||
                (scope === 'active_client' && (effectiveClientId == null || effectiveClientId <= 0))
              }
            >
              {loading ? 'Searching NorthStar…' : 'Ask NorthStar'}
            </button>
            <p className="queue-sub">
              Press Enter to ask · Shift+Enter for a new line. Answers use stored NorthStar data
              only. No web research in Phase 1.
            </p>
          </div>
        </form>
      </section>

      <section className="panel ask-examples" aria-label="Example questions">
        <div className="panel-header">
          <h2>Example questions</h2>
        </div>
        <div className="ask-example-list">
          {exampleQuestions.length === 0 ? (
            <p className="queue-sub">
              Select a specific client for Active Client scope to see matching example questions.
            </p>
          ) : (
            exampleQuestions.map((example) => (
              <button
                key={example}
                type="button"
                className="ask-example-chip"
                onClick={() => void runAsk(example)}
              >
                {example}
              </button>
            ))
          )}
        </div>
      </section>

      {error && (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      )}

      {answer && (
        <section className="panel ask-answer" aria-label="Ask NorthStar answer">
          <div className="panel-header">
            <h2>Answer</h2>
            <span className="ask-answer-scope">{answerScopeLabel}</span>
          </div>

          {answer.intent === 'call_prep' &&
            (() => {
              const hdr = answer.sections.find((s) => s.id === 'call_brief_header')
              const working = hdr?.items?.[0]
                ? String((hdr.items[0] as Record<string, unknown>).working_for || '')
                : ''
              return working ? (
                <p className="ask-working-for">
                  Working For: <strong>{working}</strong>
                </p>
              ) : null
            })()}

          <p className={`ask-summary${answer.no_data ? ' ask-summary--empty' : ''}`}>
            {answer.summary}
          </p>

          {answer.intent === 'work_next' && answer.companies.length > 0 && (
            <div className="ask-section ask-work-next" aria-label="Top Work Next priorities">
              <h3>TOP PRIORITIES</h3>
              <ol className="ask-work-next-list">
                {answer.companies.map((company) => (
                  <li
                    key={`${company.rank}-${company.company_id}-${company.client_id}-${company.external_record_no}`}
                    className="ask-work-next-item"
                  >
                    <div className="ask-work-next-item__header">
                      <span className="ask-work-next-rank">{company.rank ?? ''}</span>
                      <div>
                        {company.workspace_path ? (
                          <AskResultLink className="company-link ask-work-next-company" to={company.workspace_path}>
                            {display(company.company_name)}
                          </AskResultLink>
                        ) : (
                          <strong>{display(company.company_name)}</strong>
                        )}
                        <div className="ask-work-next-meta">
                          {company.client_name ? (
                            <span className="client-badge-pill">{company.client_name}</span>
                          ) : null}
                          <span>Record No. {display(company.external_record_no)}</span>
                          {company.status ? <span>Status: {company.status}</span> : null}
                          {(company.work_type || company.badges[0]) && (
                            <span
                              className={`opportunity-badge opportunity-badge--${badgeClass(
                                company.work_type || company.badges[0],
                              )}`}
                            >
                              {company.work_type || company.badges[0]}
                            </span>
                          )}
                          {company.work_priority != null ? (
                            <span>Priority: {company.work_priority}</span>
                          ) : company.opportunity_score != null ? (
                            <span>Score: {company.opportunity_score}</span>
                          ) : null}
                        </div>
                      </div>
                    </div>
                    {company.why ? (
                      <p className="ask-work-next-why">
                        <span className="ask-work-next-label">Why:</span> {company.why}
                      </p>
                    ) : null}
                    {company.next_action ? (
                      <p className="ask-work-next-next">
                        <span className="ask-work-next-label">Work Queue Next Action:</span>{' '}
                        {company.next_action}
                      </p>
                    ) : null}
                    {company.northstar_recommendation ? (
                      <div className="ask-work-next-ai">
                        <p className="ask-work-next-next">
                          <span className="ask-work-next-label">NorthStar Recommendation:</span>{' '}
                          {company.northstar_recommendation}
                          {company.northstar_alignment
                            ? ` · ${company.northstar_alignment}`
                            : ''}
                        </p>
                        {(company.northstar_fit || company.northstar_engagement) && (
                          <p className="queue-sub">
                            Fit: {display(company.northstar_fit)} · Engagement:{' '}
                            {display(company.northstar_engagement)}
                          </p>
                        )}
                        {company.northstar_recommendation_why ? (
                          <p className="queue-sub">{company.northstar_recommendation_why}</p>
                        ) : null}
                        <p className="queue-sub">
                          {company.northstar_insight_note ||
                            'Observation only — does not change Work Queue Next Action.'}
                        </p>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ol>
              {answer.recommended_links.length > 0 && (
                <div className="ask-work-next-queue-link opportunity-actions">
                  {answer.recommended_links.map((link) => (
                    <AskResultLink key={link.href} className="link-btn" to={link.href}>
                      {link.label}
                    </AskResultLink>
                  ))}
                </div>
              )}
            </div>
          )}

          {answer.sources.length > 0 && (
            <div className="ask-sources" aria-label="Sources">
              <strong>Sources</strong>
              <ul>
                {answer.sources.map((source) => (
                  <li key={`${source.label}-${source.detail}`}>
                    <span className="ask-source-label">{source.label}</span>
                    {source.detail ? <span className="queue-sub"> — {source.detail}</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {answer.sections.map((section) => {
            if (section.id === 'work_next_top') return null
            const isCallBriefSection = [
              'call_brief_header',
              'why_matters',
              'campaign_fit',
              'opportunity_engagement',
              'northstar_recommendation',
              'people',
              'company_knowledge',
              'northstar_history',
              'sales_appointment_history',
              'commercial_history',
              'cross_client_intel',
              'fit_for_client',
              'talking_points',
              'missing_info',
              'next_step',
              'research_preview',
            ].includes(section.id)
            return (
            <div
              key={section.id}
              className={`ask-section${isCallBriefSection ? ' ask-section--brief' : ''}`}
            >
              <h3>{section.title}</h3>
              {section.body &&
                !(section.id === 'campaign_fit' && section.items.length > 0) &&
                !(section.id === 'northstar_recommendation' && section.items.length > 0) &&
                !(section.id === 'opportunity_engagement' && section.items.length > 0) && (
                  <p className="ask-section__body">{section.body}</p>
                )}

              {section.id === 'work_next_by_client' && section.items.length > 0 && (
                <ul className="ask-work-next-clients">
                  {section.items.map((item) => {
                    const row = item as Record<string, unknown>
                    const href = String(row.work_queue_href || '')
                    const count = Number(row.actionable_count || 0)
                    const name = String(row.client_name || '')
                    return (
                      <li key={String(row.client_id)}>
                        <div className="ask-work-next-clients__row">
                          <strong>{display(name)}</strong>
                          <span>
                            {count} actionable item{count === 1 ? '' : 's'}
                          </span>
                        </div>
                        {row.top_company ? (
                          <p className="queue-sub">
                            Top now: {String(row.top_company)}
                            {row.top_work_type ? ` · ${String(row.top_work_type)}` : ''}
                            {row.top_status ? ` · ${String(row.top_status)}` : ''}
                          </p>
                        ) : null}
                        {href ? (
                          <AskResultLink className="link-btn" to={href}>
                            View Work Queue for {name}
                          </AskResultLink>
                        ) : null}
                      </li>
                    )
                  })}
                </ul>
              )}

              {section.id === 'call_brief_header' && section.items[0] && (
                <dl className="ask-fact-grid ask-call-brief-header">
                  {(() => {
                    const item = section.items[0] as Record<string, unknown>
                    return (
                      <>
                        <div className="ask-working-for-banner">
                          <dt>Working For</dt>
                          <dd>
                            <strong>{display(String(item.working_for || ''))}</strong>
                          </dd>
                        </div>
                        <div>
                          <dt>Company</dt>
                          <dd>{display(String(item.company || ''))}</dd>
                        </div>
                        <div>
                          <dt>Record No.</dt>
                          <dd>{display(String(item.external_record_no || ''))}</dd>
                        </div>
                        <div>
                          <dt>Current Status</dt>
                          <dd>{display(String(item.status || ''))}</dd>
                        </div>
                        {item.last_researched ? (
                          <div>
                            <dt>Last Researched</dt>
                            <dd>{display(String(item.last_researched))}</dd>
                          </div>
                        ) : null}
                        {item.opportunity_score != null && item.opportunity_score !== '' ? (
                          <div>
                            <dt>Opportunity Score</dt>
                            <dd>{String(item.opportunity_score)}</dd>
                          </div>
                        ) : null}
                      </>
                    )
                  })()}
                </dl>
              )}

              {section.id === 'campaign_fit' && (
                <div className="ask-campaign-fit">
                  {section.items.length === 0 ? (
                    <p className="queue-sub">{section.body}</p>
                  ) : (
                    <>
                      {section.items.map((item, index) => {
                        const row = item as Record<string, unknown>
                        const kind = String(row.section || '')
                        if (kind === 'header') {
                          return (
                            <dl key={`cf-h-${index}`} className="ask-fact-grid ask-campaign-fit-header">
                              <div>
                                <dt>Campaign</dt>
                                <dd>
                                  <strong>{display(String(row.campaign || ''))}</strong>
                                </dd>
                              </div>
                              <div>
                                <dt>Fit Rating</dt>
                                <dd>
                                  <strong>{display(String(row.fit_rating || ''))}</strong>
                                </dd>
                              </div>
                              <div>
                                <dt>Confidence</dt>
                                <dd>{display(String(row.confidence || ''))}</dd>
                              </div>
                            </dl>
                          )
                        }
                        if (kind === 'why') {
                          return (
                            <div key={`cf-w-${index}`} className="ask-campaign-fit-block">
                              <h4>WHY</h4>
                              <p>{display(String(row.text || ''))}</p>
                            </div>
                          )
                        }
                        const bullets = Array.isArray(row.bullets)
                          ? (row.bullets as string[])
                          : []
                        return (
                          <div key={`cf-${kind}-${index}`} className="ask-campaign-fit-block">
                            <h4>{display(String(row.label || kind.toUpperCase()))}</h4>
                            {bullets.length === 0 ? (
                              <p className="queue-sub">None recorded in stored fit/research.</p>
                            ) : (
                              <ul className="ask-bullet-list">
                                {bullets.map((b) => (
                                  <li key={b}>{b}</li>
                                ))}
                              </ul>
                            )}
                            {row.note ? (
                              <p className="queue-sub">{String(row.note)}</p>
                            ) : null}
                          </div>
                        )
                      })}
                    </>
                  )}
                </div>
              )}

              {section.id === 'opportunity_engagement' && section.items[0] && (
                <div className="ask-campaign-fit">
                  {(() => {
                    const row = section.items[0] as Record<string, unknown>
                    const signals = Array.isArray(row.signals)
                      ? (row.signals as string[])
                      : []
                    return (
                      <>
                        <dl className="ask-fact-grid ask-campaign-fit-header">
                          <div>
                            <dt>Engagement</dt>
                            <dd>
                              <strong>{display(String(row.level || ''))}</strong>
                            </dd>
                          </div>
                        </dl>
                        {signals.length > 0 ? (
                          <div className="signal-history" style={{ marginTop: '0.45rem' }}>
                            {signals.map((s) => (
                              <span key={s} className="opportunity-badge">
                                {s}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        {row.note ? (
                          <p className="queue-sub">{String(row.note)}</p>
                        ) : null}
                      </>
                    )
                  })()}
                </div>
              )}

              {section.id === 'northstar_recommendation' && section.items[0] && (
                <div className="ask-campaign-fit">
                  {(() => {
                    const row = section.items[0] as Record<string, unknown>
                    const evidence = Array.isArray(row.evidence_used)
                      ? (row.evidence_used as string[])
                      : []
                    const need = Array.isArray(row.still_need_to_know)
                      ? (row.still_need_to_know as string[])
                      : []
                    return (
                      <>
                        <dl className="ask-fact-grid ask-campaign-fit-header">
                          <div>
                            <dt>Recommended Action</dt>
                            <dd>
                              <strong>{display(String(row.recommended_action || ''))}</strong>
                            </dd>
                          </div>
                          <div>
                            <dt>Fit Context</dt>
                            <dd>{display(String(row.fit_context || ''))}</dd>
                          </div>
                          <div>
                            <dt>Engagement Context</dt>
                            <dd>{display(String(row.engagement_context || ''))}</dd>
                          </div>
                        </dl>
                        <div className="ask-campaign-fit-block">
                          <h4>WHY</h4>
                          <p>{display(String(row.why || ''))}</p>
                        </div>
                        {evidence.length > 0 ? (
                          <div className="ask-campaign-fit-block">
                            <h4>EVIDENCE USED</h4>
                            <ul className="ask-bullet-list">
                              {evidence.map((b) => (
                                <li key={b}>{b}</li>
                              ))}
                            </ul>
                          </div>
                        ) : null}
                        {need.length > 0 ? (
                          <div className="ask-campaign-fit-block">
                            <h4>WHAT WE STILL NEED TO KNOW</h4>
                            <ul className="ask-bullet-list">
                              {need.map((b) => (
                                <li key={b}>{b}</li>
                              ))}
                            </ul>
                          </div>
                        ) : null}
                        {row.advisory_note ? (
                          <p className="queue-sub">{String(row.advisory_note)}</p>
                        ) : null}
                      </>
                    )
                  })()}
                </div>
              )}

              {section.id === 'company_knowledge' && (
                <div className="ask-company-knowledge">
                  {section.items.length === 0 ? (
                    <p className="queue-sub">NorthStar has not researched this company yet.</p>
                  ) : (
                    <ul className="ask-knowledge-list">
                      {section.items.map((item, index) => {
                        const row = item as Record<string, unknown>
                        const level = String(row.evidence_level || 'verified')
                        return (
                          <li key={`ck-${index}`} className="ask-knowledge-list__item">
                            <div className="ask-knowledge-list__head">
                              <strong>
                                {display(String(row.label || row.finding_type || ''))}
                              </strong>
                              <span
                                className={`research-evidence research-evidence--${level.replace(/_/g, '-')}`}
                              >
                                {level === 'supported_inference'
                                  ? 'Supported Inference'
                                  : level === 'not_verified'
                                    ? 'Not Verified'
                                    : 'Verified'}
                              </span>
                            </div>
                            <div>{display(String(row.value || ''))}</div>
                            <div className="queue-sub">
                              {row.source_name ? String(row.source_name) : ''}
                              {row.page_title ? ` · ${String(row.page_title)}` : ''}
                              {row.provenance ? ` · ${String(row.provenance)}` : ''}
                            </div>
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </div>
              )}

              {section.id === 'fit_for_client' && (
                <div className="ask-fit-block">
                  {section.items.length === 0 ? (
                    <p className="queue-sub">{section.body}</p>
                  ) : (
                    section.items.map((item, index) => {
                      const row = item as Record<string, unknown>
                      const concerns = Array.isArray(row.concerns)
                        ? (row.concerns as string[])
                        : []
                      const gaps = Array.isArray(row.missing_information)
                        ? (row.missing_information as string[])
                        : []
                      return (
                        <div key={`fit-${index}`}>
                          <p>
                            Rating: <strong>{display(String(row.fit_result || ''))}</strong>
                          </p>
                          <p>{display(String(row.why || ''))}</p>
                          {row.potential_opportunity ? (
                            <p className="queue-sub">{String(row.potential_opportunity)}</p>
                          ) : null}
                          {concerns.length > 0 ? (
                            <ul className="ask-bullet-list">
                              {concerns.slice(0, 4).map((c) => (
                                <li key={c}>{c}</li>
                              ))}
                            </ul>
                          ) : null}
                          {gaps.length > 0 ? (
                            <>
                              <p className="queue-sub">Important unknowns</p>
                              <ul className="ask-bullet-list">
                                {gaps.slice(0, 6).map((g) => (
                                  <li key={g}>{g}</li>
                                ))}
                              </ul>
                            </>
                          ) : null}
                          <p className="queue-sub">
                            {display(String(row.provenance || ''))}
                          </p>
                        </div>
                      )
                    })
                  )}
                </div>
              )}

              {section.id === 'need_working_for' && section.items.length > 0 && (
                <ul className="ask-history-list">
                  {section.items.map((item) => {
                    const row = item as Record<string, unknown>
                    return (
                      <li key={String(row.client_id)}>
                        <strong>{display(String(row.client_name || ''))}</strong>
                        <div>Record No. {display(String(row.external_record_no || ''))}</div>
                        <div>Status: {display(String(row.status || ''))}</div>
                      </li>
                    )
                  })}
                </ul>
              )}

              {section.id === 'people' && section.items.length > 0 && (
                <ul className="ask-contact-list">
                  {section.items.map((item, index) => {
                    const row = item as Record<string, unknown>
                    const href = contactResultHref(
                      row.contact_id,
                      row.client_id ?? answer.active_client_id,
                    )
                    return (
                      <li key={`${String(row.contact_id ?? index)}-${String(row.name || '')}`}>
                        {href ? (
                          <AskResultLink className="company-link" to={href}>
                            <strong>{display(String(row.name || ''))}</strong>
                          </AskResultLink>
                        ) : (
                          <strong>{display(String(row.name || ''))}</strong>
                        )}
                        {row.title ? <span> · {String(row.title)}</span> : null}
                        <div className="queue-sub">
                          {row.client_name ? (
                            <span className="client-badge-pill">{String(row.client_name)}</span>
                          ) : null}
                          {row.phone ? ` · ${String(row.phone)}` : ''}
                          {row.alt_phone ? ` · Alt ${String(row.alt_phone)}` : ''}
                          {row.email ? ` · ${String(row.email)}` : ''}
                          {row.source ? ` · ${String(row.source)}` : ''}
                        </div>
                      </li>
                    )
                  })}
                </ul>
              )}

              {section.id === 'northstar_history' && section.items.length > 0 && (
                <ul className="ask-history-list">
                  {section.items.map((item, index) => {
                    const row = item as Record<string, unknown>
                    return (
                      <li key={`hist-${index}-${String(row.activity_at || '')}`}>
                        <strong>
                          {display(String(row.activity_type || 'Note'))}
                          {row.client_name ? ` · ${String(row.client_name)}` : ''}
                        </strong>
                        <div className="queue-sub">
                          {row.activity_at ? String(row.activity_at) : '—'}
                          {row.source ? ` · ${String(row.source)}` : ''}
                          {row.is_working_for ? ' · Working For' : ' · Other client'}
                        </div>
                        <p>{display(String(row.excerpt || ''))}</p>
                      </li>
                    )
                  })}
                </ul>
              )}

              {section.id === 'cross_client_intel' && section.items.length > 0 && (
                <ul className="ask-history-list">
                  {section.items.map((item, index) => {
                    const row = item as Record<string, unknown>
                    const signals = Array.isArray(row.signals) ? (row.signals as string[]) : []
                    const sources = Array.isArray(row.source_clients)
                      ? (row.source_clients as string[])
                      : []
                    return (
                      <li key={`cc-${index}-${String(row.client_name || '')}`}>
                        <strong>{display(String(row.role || 'Client'))}</strong>
                        {row.client_name ? <div>{String(row.client_name)}</div> : null}
                        {row.external_record_no ? (
                          <div>Record No. {String(row.external_record_no)}</div>
                        ) : null}
                        {row.status ? <div>Status: {String(row.status)}</div> : null}
                        {sources.length > 0 ? (
                          <div>Source Client: {sources.join(', ')}</div>
                        ) : null}
                        {row.opportunity_score != null && row.opportunity_score !== '' ? (
                          <div>Opportunity Score: {String(row.opportunity_score)}</div>
                        ) : null}
                        {signals.length > 0 && (
                          <div className="signal-history" style={{ marginTop: '0.35rem' }}>
                            {signals.map((signal) => (
                              <span
                                key={signal}
                                className={`opportunity-badge opportunity-badge--${badgeClass(signal)}`}
                              >
                                {signal}
                              </span>
                            ))}
                          </div>
                        )}
                        {row.note ? <p className="queue-sub">{String(row.note)}</p> : null}
                      </li>
                    )
                  })}
                </ul>
              )}

              {(section.id === 'commercial_signals' ||
                section.id === 'commercial_history') &&
                section.items.length > 0 && (
                <div className="signal-history">
                  {section.items.map((item, index) => {
                    const row = item as Record<string, unknown>
                    const label = String(row.display || row.signal || '')
                    return (
                      <span
                        key={`${label}-${index}`}
                        className={`opportunity-badge opportunity-badge--${badgeClass(String(row.signal || ''))}`}
                        title={String(row.source || '')}
                      >
                        {label}
                      </span>
                    )
                  })}
                </div>
              )}

              {(section.id === 'client_operations' ||
                section.id === 'client_operations_relevant') &&
                section.items.length > 0 && (
                <ul className="ask-history-list">
                  {section.items.map((item, index) => {
                    const row = item as Record<string, unknown>
                    return (
                      <li key={`ops-${index}`}>
                        <strong>{display(String(row.title || 'Operational note'))}</strong>
                        <div>{display(String(row.content || ''))}</div>
                        {row.provenance ? (
                          <div className="queue-sub">{String(row.provenance)}</div>
                        ) : null}
                        {row.source_document ? (
                          <div className="queue-sub">Source: {String(row.source_document)}</div>
                        ) : null}
                      </li>
                    )
                  })}
                </ul>
              )}

              {(section.id === 'client_contacts' ||
                section.id === 'client_contacts_linked') &&
                section.body && (
                <div style={{ whiteSpace: 'pre-wrap' }}>{section.body}</div>
              )}

              {section.id === 'talking_points' && section.items.length > 0 && (
                <ul className="ask-bullet-list">
                  {section.items.map((item, index) => {
                    const row = item as Record<string, unknown>
                    return <li key={`tp-${index}`}>{display(String(row.point || ''))}</li>
                  })}
                </ul>
              )}

              {section.id === 'missing_info' && section.items.length > 0 && (
                <ul className="ask-bullet-list">
                  {section.items.map((item, index) => {
                    const row = item as Record<string, unknown>
                    return <li key={`miss-${index}`}>{display(String(row.gap || ''))}</li>
                  })}
                </ul>
              )}

              {section.id === 'next_step' && section.items[0] && (
                <p className="ask-next-step">
                  {display(String((section.items[0] as Record<string, unknown>).next_step || section.body))}
                  <span className="queue-sub"> · Advisory only — nothing was created or changed.</span>
                </p>
              )}

              {section.id === 'research_preview' && section.items[0] && (
                <div className="ask-research-preview">
                  <p className="queue-sub">
                    {display(String((section.items[0] as Record<string, unknown>).status || ''))}
                    {(section.items[0] as Record<string, unknown>).last_researched
                      ? ` · Last researched ${String(
                          (section.items[0] as Record<string, unknown>).last_researched,
                        )}`
                      : ''}
                  </p>
                  <ul className="ask-bullet-list">
                    {(
                      Array.isArray((section.items[0] as Record<string, unknown>).could_add)
                        ? ((section.items[0] as Record<string, unknown>).could_add as string[])
                        : []
                    ).map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}

              {section.id === 'client_history' && section.items.length > 0 && (
                <ul className="ask-history-list">
                  {section.items.map((item) => {
                    const row = item as Record<string, unknown>
                    const clientName = String(row.client_name || '')
                    const rn = String(row.external_record_no || '')
                    const status = String(row.status || '')
                    const clientId = Number(row.client_id)
                    return (
                      <li key={`${clientId}-${rn}`}>
                        <strong>{clientName}</strong>
                        <div>
                          Record No. {rn || '—'}
                          {rn && Number.isFinite(clientId) ? (
                            <>
                              {' · '}
                              <AskResultLink to={`/companies/${encodeURIComponent(rn)}?client_id=${clientId}`}>
                                Open workspace
                              </AskResultLink>
                            </>
                          ) : null}
                        </div>
                        <div>Status: {status || '—'}</div>
                        <div className="queue-sub">{String(row.source || '')}</div>
                      </li>
                    )
                  })}
                </ul>
              )}

              {section.id === 'cross_client' && section.items[0] && (
                <dl className="ask-fact-grid">
                  {(() => {
                    const item = section.items[0] as Record<string, unknown>
                    const signals = Array.isArray(item.signals)
                      ? (item.signals as string[])
                      : []
                    const sources = Array.isArray(item.source_clients)
                      ? (item.source_clients as string[])
                      : []
                    return (
                      <>
                        <div>
                          <dt>Target Client</dt>
                          <dd>{display(String(item.target_client || ''))}</dd>
                        </div>
                        <div>
                          <dt>Current Target Status</dt>
                          <dd>{display(String(item.target_status || ''))}</dd>
                        </div>
                        <div>
                          <dt>Source Client</dt>
                          <dd>{sources.join(', ') || '—'}</dd>
                        </div>
                        <div>
                          <dt>NorthStar Signals</dt>
                          <dd>
                            <div className="signal-history">
                              {signals.map((signal) => (
                                <span
                                  key={signal}
                                  className={`opportunity-badge opportunity-badge--${badgeClass(signal)}`}
                                >
                                  {signal}
                                </span>
                              ))}
                            </div>
                          </dd>
                        </div>
                        <div>
                          <dt>Opportunity Score</dt>
                          <dd>{String(item.opportunity_score ?? '—')}</dd>
                        </div>
                      </>
                    )
                  })()}
                </dl>
              )}
            </div>
            )
          })}

          {answer.milestones.length > 0 && (
            <div className="ask-section">
              <h3>MILESTONE BADGES</h3>
              <div className="signal-history">
                {answer.milestones.map((m, index) => (
                  <span
                    key={`${m.milestone_type}-${m.client_name}-${index}`}
                    className={`opportunity-badge opportunity-badge--${badgeClass(m.label || m.milestone_type)}`}
                    title={`${m.source}${m.milestone_date ? ` · ${m.milestone_date}` : ''}`}
                  >
                    {m.label || m.milestone_type}
                    {m.client_name ? ` · ${m.client_name}` : ''}
                  </span>
                ))}
              </div>
            </div>
          )}

          {answer.contacts.length > 0 && (
            <div className="ask-section">
              <h3>CONTACTS</h3>
              <ul className="ask-contact-list">
                {answer.contacts.map((contact, index) => {
                  const href = contactResultHref(
                    contact.contact_id,
                    contact.client_id ?? answer.active_client_id,
                  )
                  return (
                    <li key={`${contact.contact_id ?? index}-${contact.name}`}>
                      {href ? (
                        <AskResultLink className="company-link" to={href}>
                          <strong>{display(contact.name)}</strong>
                        </AskResultLink>
                      ) : (
                        <strong>{display(contact.name)}</strong>
                      )}
                      {contact.title ? <span> · {contact.title}</span> : null}
                    <div className="queue-sub">
                      {contact.client_name ? (
                        <span className="client-badge-pill">{contact.client_name}</span>
                      ) : null}
                      {contact.phone ? ` · ${contact.phone}` : ''}
                      {contact.email ? ` · ${contact.email}` : ''}
                      {contact.source ? ` · ${contact.source}` : ''}
                    </div>
                    </li>
                  )
                })}
              </ul>
            </div>
          )}

          {answer.notes.length > 0 && (
            <div className="ask-section">
              <h3>NOTE MATCHES</h3>
              <div className="ask-note-list">
                {answer.notes.map((note, index) => (
                  <article key={`${note.external_record_no}-${index}`} className="ask-note-card">
                    {note.workspace_path ? (
                      <AskResultLink className="company-link" to={note.workspace_path}>
                        {display(note.company_name)}
                      </AskResultLink>
                    ) : (
                      <strong>{display(note.company_name)}</strong>
                    )}
                    <div className="ask-note-meta">
                      {note.client_name ? (
                        <span className="client-badge-pill">{note.client_name}</span>
                      ) : null}
                      <span>Record No. {display(note.external_record_no)}</span>
                      {note.event_at ? <span>{note.event_at}</span> : null}
                      <span>{note.source}</span>
                    </div>
                    <p>{display(note.excerpt)}</p>
                  </article>
                ))}
              </div>
            </div>
          )}

          {answer.intent !== 'work_next' && answer.companies.length > 0 && (
            <div className="ask-section">
              <h3>COMPANIES</h3>
              <div className="opportunity-card-list">
                {answer.companies.map((company) => (
                  <article
                    key={`${company.company_id}-${company.client_id}-${company.external_record_no}`}
                    className="opportunity-card ask-company-card"
                  >
                    <div className="opportunity-card__header">
                      <div>
                        {company.workspace_path ? (
                          <AskResultLink className="company-link opportunity-card__company" to={company.workspace_path}>
                            {display(company.company_name)}
                          </AskResultLink>
                        ) : (
                          <strong>{display(company.company_name)}</strong>
                        )}
                        <div className="opportunity-card__meta">
                          {company.client_name ? (
                            <span className="client-badge-pill">{company.client_name}</span>
                          ) : null}
                          <span>Record No. {display(company.external_record_no)}</span>
                          {company.status ? <span>Status: {company.status}</span> : null}
                          {company.opportunity_score != null ? (
                            <span>Score: {company.opportunity_score}</span>
                          ) : null}
                        </div>
                      </div>
                    </div>
                    {company.badges.length > 0 && (
                      <div className="signal-history">
                        {company.badges.map((badge) => (
                          <span
                            key={badge}
                            className={`opportunity-badge opportunity-badge--${badgeClass(badge)}`}
                          >
                            {badge}
                          </span>
                        ))}
                      </div>
                    )}
                    {company.why ? <p className="ask-section__body">{company.why}</p> : null}
                  </article>
                ))}
              </div>
            </div>
          )}

          {answer.recommended_links.length > 0 &&
            (answer.intent !== 'work_next' || answer.companies.length === 0) && (
            <div className="ask-section">
              <h3>RECOMMENDED NAVIGATION</h3>
              <div className="opportunity-actions">
                {answer.recommended_links.map((link) => (
                  <AskResultLink key={link.href} className="link-btn" to={link.href}>
                    {link.label}
                  </AskResultLink>
                ))}
              </div>
            </div>
          )}

          <div className="ask-research panel ask-research-panel" aria-label="Research this company">
            <h3>Research This Company</h3>
            <p className="queue-sub">
              Opens Research Company: NorthStar intelligence first, then public web research and fit
              for the Working For client. No automatic CRM changes.
            </p>
            {(() => {
              const hdr = answer.sections.find((s) => s.id === 'call_brief_header')
              const hdrItem = hdr?.items?.[0] as Record<string, unknown> | undefined
              const companyCard =
                answer.companies.find((c) => c.external_record_no) || answer.companies[0]
              // Prefer THIS answer's resolved Working For — never a prior answer's client.
              const workingForClientId =
                (hdrItem?.working_for_client_id != null &&
                Number(hdrItem.working_for_client_id) > 0
                  ? Number(hdrItem.working_for_client_id)
                  : null) ??
                (answer.active_client_id != null && answer.active_client_id > 0
                  ? answer.active_client_id
                  : null) ??
                (companyCard?.client_id != null && companyCard.client_id > 0
                  ? companyCard.client_id
                  : null)
              const recordNo =
                String(hdrItem?.research_record_no || '').trim() ||
                String(hdrItem?.external_record_no || '').trim() ||
                (companyCard?.external_record_no || '').trim() ||
                String(hdrItem?.master_record_no || '').trim()
              const companyName =
                companyCard?.company_name ||
                String(hdrItem?.company || '') ||
                'this company'
              const workingForName =
                String(hdrItem?.working_for || '').trim() ||
                answer.active_client_name ||
                ''
              const needClientChoices =
                answer.sections.find((s) => s.id === 'need_working_for')?.items || []
              const webOption = answer.research_options.find((o) => o.id === 'web')
              const providerOption = answer.research_options.find((o) => o.id === 'data_provider')

              if (!recordNo) {
                return (
                  <p className="queue-sub">
                    Ask about a specific company first, or open Research from Company Workspace.
                  </p>
                )
              }

              if (workingForClientId == null || workingForClientId <= 0) {
                const choices =
                  needClientChoices.length > 0
                    ? needClientChoices
                    : assignedClients.map((c) => ({
                        client_id: c.client_id,
                        client_name: c.client_name,
                      }))
                return (
                  <div>
                    <p className="queue-sub">
                      Select a Working For client to research {display(companyName)}.
                    </p>
                    <div className="ask-research-options">
                      {choices.map((raw) => {
                        const row = raw as Record<string, unknown>
                        const cid = Number(row.client_id)
                        const name = String(row.client_name || '')
                        if (!Number.isFinite(cid) || cid <= 0) return null
                        return (
                          <AskResultLink
                            key={cid}
                            className="ask-research-btn ask-research-btn--active"
                            to={`/companies/${encodeURIComponent(recordNo)}/research?client_id=${cid}`}
                          >
                            Research for {name}
                            <span>Working For</span>
                          </AskResultLink>
                        )
                      })}
                    </div>
                  </div>
                )
              }

              const href = `/companies/${encodeURIComponent(recordNo)}/research?client_id=${workingForClientId}`
              const hasStored =
                Boolean(hdrItem?.has_stored_research) ||
                Boolean(hdrItem?.last_researched) ||
                Boolean(
                  (answer.sections.find((s) => s.id === 'research_preview')?.items?.[0] as
                    | Record<string, unknown>
                    | undefined)?.has_stored_research,
                )
              const actionLabel = hasStored ? 'Refresh Research' : 'Research This Company'
              return (
                <div className="ask-research-options">
                  <p className="queue-sub" style={{ width: '100%', marginBottom: '0.35rem' }}>
                    Working For: <strong>{display(workingForName)}</strong>
                    {hdrItem?.last_researched
                      ? ` · Last researched ${String(hdrItem.last_researched)}`
                      : ''}
                  </p>
                  <AskResultLink
                    className="ask-research-btn ask-research-btn--active"
                    to={href}
                    title={webOption?.description || 'Open Research Company'}
                  >
                    {actionLabel}
                    <span>{webOption?.status || 'Available'}</span>
                  </AskResultLink>
                  <button
                    type="button"
                    className="ask-research-btn ask-research-btn--disabled"
                    disabled
                    title={providerOption?.description || 'ZoomInfo not connected'}
                  >
                    {providerOption?.label || 'Research Data Provider'}
                    <span>{providerOption?.status || 'ZoomInfo not connected'}</span>
                  </button>
                </div>
              )
            })()}
          </div>
        </section>
      )}

      <section className="panel ask-history" aria-label="Recent Ask NorthStar questions">
        <div className="panel-header">
          <h2>Recent Questions</h2>
          <span className="queue-sub">
            {scope === 'active_client' && effectiveClientName
              ? `${effectiveClientName} · Your questions only`
              : scope === 'active_client'
                ? 'Select a client · Your questions only'
                : 'All NorthStar · Your questions only'}
          </span>
        </div>
        {history.length === 0 ? (
          <p className="empty-state">
            {scope === 'active_client' && effectiveClientName
              ? `No recent ${effectiveClientName} Ask NorthStar questions yet.`
              : 'No recent Ask NorthStar questions yet.'}
          </p>
        ) : (
          <ul className="ask-history-questions">
            {history.map((item) => {
              const badge =
                item.active_client_name?.trim() ||
                (item.scope === 'all' || item.active_client_id == null
                  ? 'All NorthStar'
                  : '')
              return (
                <li key={item.id}>
                  <button
                    type="button"
                    className="ask-history-btn"
                    onClick={() => void runAsk(item.question)}
                  >
                    {item.question}
                  </button>
                  <span className="queue-sub ask-history-meta">
                    {scope === 'all' && badge ? (
                      <span className="ask-history-client-badge">{badge}</span>
                    ) : null}
                    {item.created_at}
                    {item.answer_summary ? ` · ${item.answer_summary.slice(0, 90)}` : ''}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </>
  )
}
