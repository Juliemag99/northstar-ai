import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  approveResearchUpdate,
  cancelResearchJob,
  fetchCompanyResearch,
  fetchDeepResearchStatus,
  fetchResearchJob,
  rejectResearchUpdate,
  startCompanyResearch,
} from './api/carmeco'
import type { DeepResearchStatus, ResearchCompanyResponse, ResearchFindingView } from './types/carmeco'
import AddToNorthStar from './AddToNorthStar'
import ZoomInfoAddModal from './ZoomInfoAddModal'
import type { CrmAddContactInput } from './api/carmeco'
import { ASK_NORTHSTAR_PATH, isFromAskNorthStar, withAskReturnParam } from './askNorthStarReturn'
import { useAuth } from './auth/useAuth'

function display(value: string | null | undefined): string {
  return value?.trim() || '—'
}

function relevanceLabel(tier: string | undefined): string {
  const key = (tier || '').toUpperCase()
  if (key === 'HIGH') return 'High'
  if (key === 'MEDIUM') return 'Medium'
  if (key === 'LOW') return 'Low'
  if (key === 'NOT_TARGET') return 'Not Target'
  return tier || '—'
}

function publicCrmMatchKind(
  status: string | undefined,
): 'new' | 'existing' | 'possible' {
  if (status === 'existing') return 'existing'
  if (status === 'possible_match') return 'possible'
  return 'new'
}

function publicCrmBadgeLabel(kind: 'new' | 'existing' | 'possible'): string {
  if (kind === 'existing') return 'Already in NorthStar'
  if (kind === 'possible') return 'Possible Match'
  return 'New Contact'
}

function toAddContactInput(
  c: ResearchFindingView,
): CrmAddContactInput {
  return {
    full_name: c.contact_name || c.value || '',
    title: c.contact_title || '',
    email: c.contact_email || '',
    phone: c.contact_phone || '',
    linkedin: c.linkedin_url || '',
    source_url: c.linkedin_url || c.source_url || '',
  }
}

function evidenceLabel(level: string | undefined): string {
  const key = (level || '').toLowerCase()
  if (key === 'supported_inference') return 'Supported Inference'
  if (key === 'not_verified') return 'Not Verified'
  return 'Verified'
}

function FindingList({
  title,
  items,
}: {
  title: string
  items: Array<{
    value: string
    source_name: string
    source_url: string
    researched_at: string
    finding_type?: string
    evidence_level?: string
    page_title?: string
  }>
}) {
  const usable = items.filter(
    (item) =>
      (item.evidence_level || '').toLowerCase() !== 'not_verified' &&
      !/not verified in current public research/i.test(item.value || ''),
  )
  if (!usable.length) {
    return (
      <div className="ask-section">
        <h3>{title}</h3>
        <p className="queue-sub">Not verified in current public research.</p>
      </div>
    )
  }
  return (
    <div className="ask-section">
      <h3>{title}</h3>
      <ul className="ask-history-list">
        {usable.map((item, index) => (
          <li key={`${title}-${index}-${item.value}`}>
            <div className="ask-work-next-clients__row">
              <strong>{display(item.value)}</strong>
              <span
                className={`research-evidence research-evidence--${(
                  item.evidence_level || 'verified'
                ).split('_').join('-')}`}
              >
                {evidenceLabel(item.evidence_level)}
              </span>
            </div>
            <div className="queue-sub">
              Source: {display(item.source_name)}
              {item.page_title ? ` · ${item.page_title}` : ''}
              {item.source_url ? (
                <>
                  {' · '}
                  <a href={item.source_url} target="_blank" rel="noreferrer">
                    Open source
                  </a>
                </>
              ) : null}
              {item.researched_at ? ` · Researched: ${item.researched_at}` : ''}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

const RESEARCH_PROGRESS_STEPS = [
  'Checking NorthStar history',
  'Verifying official website',
  'Reviewing products and capabilities',
  'Reviewing locations',
  'Evaluating client fit',
  'Preparing findings',
]

export default function ResearchCompany({
  recordNo,
  defaultClientId = null,
  defaultClientName = '',
}: {
  recordNo: string
  defaultClientId?: number | null
  defaultClientName?: string
}) {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const fromAskNorthStar = isFromAskNorthStar(searchParams.get('from'))
  const clientIdParam = searchParams.get('client_id')
  const campaignIdParam = searchParams.get('campaign_id')
  const workingForClientId = clientIdParam
    ? Number(clientIdParam)
    : defaultClientId != null && defaultClientId > 0
      ? defaultClientId
      : null
  const campaignIdFromUrl =
    campaignIdParam && Number(campaignIdParam) > 0 ? Number(campaignIdParam) : null

  const { user } = useAuth()
  const isAdmin = Boolean(user?.is_administrator)

  const [data, setData] = useState<ResearchCompanyResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actionMsg, setActionMsg] = useState<string | null>(null)
  const [progressStep, setProgressStep] = useState(0)
  const [deepConfigured, setDeepConfigured] = useState(false)
  const [deepStatus, setDeepStatus] = useState<DeepResearchStatus | null>(null)
  const [deepConfirmOpen, setDeepConfirmOpen] = useState(false)
  const [deepPaidRefresh, setDeepPaidRefresh] = useState(false)
  const [activeJobId, setActiveJobId] = useState<number | null>(null)
  const inFlightRef = useRef(false)
  const skipClientReloadRef = useRef(true)
  const [selectedPublicIdx, setSelectedPublicIdx] = useState<Set<number>>(new Set())
  const [addOpen, setAddOpen] = useState(false)
  const [addContacts, setAddContacts] = useState<CrmAddContactInput[]>([])
  const [detailContact, setDetailContact] = useState<ResearchFindingView | null>(null)
  const [zoomInfoAddKind, setZoomInfoAddKind] = useState<'company' | 'contact' | null>(null)

  const load = useCallback(
    async (opts?: {
      run?: boolean
      force?: boolean
      clientId?: number | null
      campaignId?: number | null
      depth?: 'quick' | 'deep'
      confirmPaidRefresh?: boolean
    }) => {
      const run = opts?.run ?? false
      const force = opts?.force ?? false
      const depth = opts?.depth ?? 'quick'
      const confirmPaidRefresh = opts?.confirmPaidRefresh ?? false
      const cid = opts?.clientId !== undefined ? opts.clientId : workingForClientId
      const campId =
        opts?.campaignId !== undefined ? opts.campaignId : campaignIdFromUrl
      if (run && inFlightRef.current) return
      if (run) inFlightRef.current = true
      setError(null)
      setActionMsg(null)
      if (run) setRunning(true)
      else setLoading(true)
      try {
        const result = run
          ? await startCompanyResearch({
              external_record_no: recordNo,
              working_for_client_id: cid,
              campaign_id: campId,
              force_refresh: force,
              confirm_paid_refresh: confirmPaidRefresh,
              research_depth: depth,
            })
          : await fetchCompanyResearch({
              external_record_no: recordNo,
              working_for_client_id: cid,
              campaign_id: campId,
              run: false,
            })
        setData(result)
        setSelectedPublicIdx(new Set())
        if (result.deep_research_paid_refresh_required) {
          setDeepPaidRefresh(true)
          setDeepConfirmOpen(true)
          setRunning(false)
          inFlightRef.current = false
          setActiveJobId(null)
          return
        }
        if (result.job?.job_id && ['queued', 'running'].includes(result.job.status)) {
          setActiveJobId(result.job.job_id)
        } else {
          setActiveJobId(null)
        }
        if (result.working_for_client_id && !clientIdParam) {
          setSearchParams(
            (prev) => {
              const next = new URLSearchParams(prev)
              next.set('client_id', String(result.working_for_client_id))
              return next
            },
            { replace: true },
          )
        }
        if (result.campaign_id && !campaignIdParam) {
          setSearchParams(
            (prev) => {
              const next = new URLSearchParams(prev)
              next.set('campaign_id', String(result.campaign_id))
              return next
            },
            { replace: true },
          )
        }
      } catch (err) {
        console.error('NorthStar Research request failed', err)
        setData(null)
        const raw = err instanceof Error ? err.message : String(err)
        const unreachable =
          /failed to fetch|networkerror|load failed|econnrefused|api_base/i.test(raw)
        setError(
          unreachable
            ? 'NorthStar Research could not connect to the research service. Please try again.'
            : 'NorthStar Research could not load results for this company. Please try again.',
        )
      } finally {
        setLoading(false)
        setRunning(false)
        if (run) inFlightRef.current = false
      }
    },
    [recordNo, workingForClientId, campaignIdFromUrl, clientIdParam, campaignIdParam, setSearchParams],
  )

  useEffect(() => {
    let cancelled = false
    void fetchDeepResearchStatus()
      .then((s) => {
        if (!cancelled) {
          setDeepConfigured(Boolean(s.deep_research_configured))
          setDeepStatus(s)
        }
      })
      .catch(() => {
        if (!cancelled) setDeepConfigured(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (activeJobId == null) return
    let cancelled = false
    const timer = window.setInterval(() => {
      void fetchResearchJob(activeJobId)
        .then((result) => {
          if (cancelled) return
          setData(result)
          const status = result.job?.status || ''
          if (['completed', 'failed', 'cancelled'].includes(status)) {
            setActiveJobId(null)
            setRunning(false)
            inFlightRef.current = false
          }
        })
        .catch(() => {
          /* keep polling until user cancels or leaves */
        })
    }, 2000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [activeJobId])

  useEffect(() => {
    skipClientReloadRef.current = true
    setData(null)
    setError(null)
    setDetailContact(null)
    setAddOpen(false)
    setAddContacts([])
    void load({ run: true, force: false })
    // Auto-run research when the company record changes, not when Active Client switches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordNo])

  useEffect(() => {
    if (skipClientReloadRef.current) {
      skipClientReloadRef.current = false
      return
    }
    if (!workingForClientId) return
    setData(null)
    setError(null)
    void load({ run: false })
    // Reload stored research for the newly selected Active Client without writing a new run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workingForClientId, campaignIdFromUrl])

  useEffect(() => {
    if (!running && !loading) return
    setProgressStep(0)
    const timer = window.setInterval(() => {
      setProgressStep((prev) => (prev + 1) % RESEARCH_PROGRESS_STEPS.length)
    }, 1800)
    return () => window.clearInterval(timer)
  }, [running, loading])

  async function onApprove(id: number) {
    setActionMsg(null)
    try {
      const result = await approveResearchUpdate(id)
      setActionMsg(
        `Approved ${result.field_key}: “${result.old_value || '(empty)'}” → “${result.new_value}”.`,
      )
      await load({ run: false })
    } catch (err) {
      console.error('Research approve failed', err)
      setActionMsg('Could not approve that update. Please try again.')
    }
  }

  async function onReject(id: number) {
    setActionMsg(null)
    try {
      const result = await rejectResearchUpdate(id)
      setActionMsg(
        result.master_value_unchanged
          ? `Rejected ${result.field_key}. Master record unchanged.`
          : `Rejected ${result.field_key}.`,
      )
      await load({ run: false })
    } catch (err) {
      console.error('Research reject failed', err)
      setActionMsg('Could not reject that update. Please try again.')
    }
  }

  function chooseClient(clientId: number) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('client_id', String(clientId))
      return next
    })
  }

  const known = data?.northstar_known
  const fit = data?.fit
  const headerCompanyName =
    data?.company_name || known?.company_name || defaultClientName || ''
  const headerRecordNo =
    data?.working_for_record_no ||
    known?.working_for_record_no ||
    known?.master_record_no ||
    recordNo
  const headerStatus = data?.working_for_status || known?.working_for_status || ''
  const headerWorkingFor =
    data?.working_for_client_name || known?.working_for_client_name || defaultClientName

  function researchContactHref(contactId: number): string {
    const q =
      workingForClientId != null && workingForClientId > 0
        ? `?client_id=${workingForClientId}`
        : ''
    const href = `/contacts/${contactId}${q}`
    return fromAskNorthStar ? withAskReturnParam(href) : href
  }

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <button
            type="button"
            className="link-btn back-link"
            onClick={() =>
              fromAskNorthStar ? navigate(ASK_NORTHSTAR_PATH) : navigate(-1)
            }
          >
            {fromAskNorthStar ? '← Back to Ask NorthStar' : '← Back'}
          </button>
          <h1>Research Company</h1>
          <p className="research-company-name">
            {display(headerCompanyName || recordNo)}
          </p>
          {data && !data.needs_working_for && (
            <>
              <p className="workspace-working-for">
                <strong>Working For:</strong> {display(headerWorkingFor)}
              </p>
              {(data.campaign_name || (data.campaign_choices?.length ?? 0) > 0) && (
                <p className="workspace-working-for">
                  <strong>Campaign:</strong>{' '}
                  {(data.campaign_choices?.length ?? 0) > 1 ? (
                    <select
                      value={String(data.campaign_id || '')}
                      onChange={(e) => {
                        const nextId = Number(e.target.value)
                        if (!nextId) return
                        setSearchParams(
                          (prev) => {
                            const next = new URLSearchParams(prev)
                            next.set('campaign_id', String(nextId))
                            return next
                          },
                          { replace: true },
                        )
                        void load({
                          run: true,
                          force: true,
                          clientId: data.working_for_client_id,
                          campaignId: nextId,
                        })
                      }}
                    >
                      {data.campaign_choices.map((c) => (
                        <option key={c.campaign_id} value={c.campaign_id}>
                          {c.campaign_name}
                          {c.is_default ? ' (Default)' : ''}
                        </option>
                      ))}
                    </select>
                  ) : (
                    display(data.campaign_name)
                  )}
                </p>
              )}
              <p>
                Record No. {display(headerRecordNo)}
                {' · '}
                Status: {display(headerStatus)}
              </p>
            </>
          )}
          {!data && !error && defaultClientName ? (
            <p className="workspace-working-for">
              <strong>Working For:</strong> {defaultClientName}
            </p>
          ) : null}
        </div>
        <div className="heading-controls">
          <button
            type="button"
            className="primary-btn"
            disabled={running || loading || Boolean(data?.needs_working_for) || activeJobId != null}
            onClick={() => {
              if (running || loading || activeJobId != null) return
              void load({ run: true, force: true, depth: 'quick' })
            }}
          >
            {running && !activeJobId ? 'Researching…' : 'Quick Research'}
          </button>
          <button
            type="button"
            className="primary-btn"
            disabled={
              running ||
              loading ||
              Boolean(data?.needs_working_for) ||
              activeJobId != null ||
              !deepConfigured ||
              !isAdmin ||
              inFlightRef.current
            }
            title={
              !isAdmin
                ? 'Deep Research is administrator-only during the pilot'
                : deepConfigured
                  ? 'Deep Research with web search and citations'
                  : 'Deep Research is not configured on the server'
            }
            onClick={() => {
              if (
                running ||
                loading ||
                activeJobId != null ||
                !deepConfigured ||
                !isAdmin ||
                inFlightRef.current
              ) {
                return
              }
              setDeepPaidRefresh(false)
              setDeepConfirmOpen(true)
            }}
          >
            Deep Research
          </button>
          {activeJobId != null && isAdmin ? (
            <button
              type="button"
              className="link-btn"
              onClick={() => {
                if (inFlightRef.current) return
                void cancelResearchJob(activeJobId).then((result) => {
                  setData(result)
                  if (!['queued', 'running'].includes(result.job?.status || '')) {
                    setActiveJobId(null)
                    setRunning(false)
                    inFlightRef.current = false
                  }
                })
              }}
            >
              Cancel Deep Research
            </button>
          ) : null}
        </div>
      </div>

      {(loading || running || activeJobId != null) && (
        <p className="data-status" aria-live="polite">
          {data?.job && ['queued', 'running'].includes(data.job.status)
            ? `Deep Research ${data.job.status} — ${data.job.progress}% ${data.job.progress_message || ''}`.trim()
            : `${RESEARCH_PROGRESS_STEPS[progressStep]}…`}
        </p>
      )}
      {data?.job?.error_message ? (
        <p className="data-status data-status--error" role="alert">
          {display(data.job.error_message)}
        </p>
      ) : null}
      {error && (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      )}
      {actionMsg && <p className="data-status">{actionMsg}</p>}

      {(() => {
        const usage =
          (data?.deep_research_usage && Object.keys(data.deep_research_usage).length
            ? data.deep_research_usage
            : data?.job?.usage) || null
        if (!usage || !Object.keys(usage).length) return null
        if (data?.research_depth !== 'deep' && !data?.job) return null
        const costStatus = String(usage.cost_estimate_status || 'unavailable')
        const cost =
          costStatus === 'complete' && usage.estimated_cost_usd != null
            ? `$${Number(usage.estimated_cost_usd).toFixed(4)}`
            : 'Exact cost unavailable'
        return (
          <p className="data-status" data-testid="deep-research-usage">
            Deep Research usage: model {display(String(usage.model || data?.job?.openai_model || ''))}
            {' · '}
            searches {display(String(usage.web_search_call_count ?? '—'))}
            {' · '}
            tokens in/out/total{' '}
            {display(String(usage.input_tokens ?? '—'))}/
            {display(String(usage.output_tokens ?? '—'))}/
            {display(String(usage.total_tokens ?? '—'))}
            {usage.reasoning_tokens != null
              ? ` (reasoning ${String(usage.reasoning_tokens)})`
              : ''}
            {' · '}
            est. cost {cost}
            {data?.deep_research_from_cache ? ' · cached' : ''}
          </p>
        )
      })()}

      {data?.needs_working_for && (
        <section className="panel" aria-label="Select Working For client">
          <div className="panel-header">
            <h2>SELECT WORKING FOR CLIENT</h2>
          </div>
          <p>{data.summary}</p>
          <ul className="ask-work-next-clients">
            {data.working_for_choices.map((choice) => (
              <li key={String(choice.client_id)}>
                <strong>{display(String(choice.client_name || ''))}</strong>
                <div className="queue-sub">
                  Record No. {display(String(choice.external_record_no || ''))}
                  {choice.status ? ` · ${String(choice.status)}` : ''}
                </div>
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => chooseClient(Number(choice.client_id))}
                >
                  Research for {String(choice.client_name || 'this client')}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {data && !data.needs_working_for && (
        <div className="research-layout">
          <section className="panel ask-answer research-decision" aria-label="Research summary">
            <div className="panel-header">
              <h2>RESEARCH SUMMARY</h2>
              <span className="ask-answer-scope">
                {data.working_for_client_name} · Read only until approve
              </span>
            </div>

            {(() => {
              const decision = data.decision_summary
              const fitResult =
                decision?.fit_result || data.fit?.fit_result || 'Insufficient Information'
              const clientLabel =
                decision?.client_name || data.working_for_client_name || 'Client'
              const campaignLabel =
                decision?.campaign_name || data.campaign_name || data.fit?.campaign_name || 'Default'
              const overviewItems = (data.overview || []).filter(
                (item) =>
                  (item.evidence_level || '').toLowerCase() !== 'not_verified' &&
                  !/not verified in current public research/i.test(item.value || ''),
              )
              const locationItems = (data.locations || []).filter(
                (item) =>
                  (item.evidence_level || '').toLowerCase() !== 'not_verified' &&
                  !/not verified in current public research/i.test(item.value || ''),
              )
              const industryItems = (data.industries || []).filter(
                (item) =>
                  (item.evidence_level || '').toLowerCase() !== 'not_verified' &&
                  !/not verified in current public research/i.test(item.value || ''),
              )
              const productItems = (data.products || []).filter(
                (item) =>
                  (item.evidence_level || '').toLowerCase() !== 'not_verified' &&
                  !/not verified in current public research/i.test(item.value || ''),
              )
              const websiteItem = overviewItems.find((item) => item.finding_type === 'website')
              const nameItem = overviewItems.find((item) => item.finding_type === 'company_name')
              const overviewText = overviewItems.find((item) => item.finding_type === 'overview')
              return (
                <>
                  <div className="research-decision__fit" aria-label="Company profile">
                    <div className="research-decision__kicker">Company Profile</div>
                    <div className="research-decision__client">
                      {display(String(nameItem?.value || data.company_name || ''))}
                      {websiteItem?.value ? ` · ${display(String(websiteItem.value))}` : ''}
                    </div>
                    {overviewText?.value ? (
                      <p style={{ marginTop: '0.45rem' }}>{display(String(overviewText.value))}</p>
                    ) : null}
                    {locationItems.length > 0 ? (
                      <p className="queue-sub" style={{ marginTop: '0.35rem' }}>
                        Locations:{' '}
                        {locationItems
                          .map((item) => String(item.value || ''))
                          .filter(Boolean)
                          .slice(0, 4)
                          .join(' · ')}
                      </p>
                    ) : null}
                    {industryItems.length > 0 ? (
                      <p className="queue-sub">
                        Industries:{' '}
                        {Array.from(
                          new Set(industryItems.map((item) => String(item.value || ''))),
                        )
                          .filter(Boolean)
                          .slice(0, 6)
                          .join(', ')}
                      </p>
                    ) : null}
                    {productItems.length > 0 ? (
                      <p className="queue-sub">
                        Products / services:{' '}
                        {productItems
                          .map((item) => String(item.value || ''))
                          .filter(Boolean)
                          .slice(0, 6)
                          .join(', ')}
                      </p>
                    ) : (
                      <p className="queue-sub">
                        Products / manufacturing processes: see Research Evidence below when
                        available.
                      </p>
                    )}
                  </div>

                  <div className="research-decision__fit">
                    <div className="research-decision__kicker">Campaign Fit</div>
                    <div className="research-decision__client">
                      {clientLabel} · {campaignLabel}
                    </div>
                    <div className="research-decision__rating">{fitResult}</div>
                    <div className="research-decision__block">
                      <h3>Why</h3>
                      <p>
                        {display(
                          decision?.why ||
                            data.fit?.why ||
                            'Not enough verified evidence to explain this rating yet.',
                        )}
                      </p>
                    </div>
                    <div className="research-decision__block">
                      <h3>What we still need to know</h3>
                      <p>
                        {display(
                          decision?.still_need ||
                            (data.fit?.missing_information?.[0] as string) ||
                            'Confirm the remaining campaign-specific gaps listed under Missing Information.',
                        )}
                      </p>
                    </div>
                  </div>
                </>
              )
            })()}

            {data.engagement && (
              <div className="research-decision__engagement" aria-label="Opportunity engagement">
                <div className="research-decision__kicker">Opportunity / Engagement</div>
                <div className="research-decision__rating research-decision__rating--engagement">
                  {display(data.engagement.label || data.engagement.level)}
                </div>
                {data.engagement.signals.length > 0 ? (
                  <div className="signal-history" style={{ marginTop: '0.45rem' }}>
                    {data.engagement.signals.map((s) => (
                      <span key={s} className="opportunity-badge">
                        {s}
                      </span>
                    ))}
                  </div>
                ) : null}
                <p className="research-decision__engagement-why">{display(data.engagement.why)}</p>
                <p className="queue-sub">{display(data.engagement.note)}</p>
                {data.engagement.cross_client_signals.length > 0 ? (
                  <ul className="ask-bullet-list" style={{ marginTop: '0.5rem' }}>
                    {data.engagement.cross_client_signals.map((row, i) => (
                      <li key={`eng-cc-${i}`}>
                        {display(String(row.client_name || 'Other client'))}:{' '}
                        {(row.signals || []).join(', ')}
                        {row.attribution ? (
                          <span className="queue-sub"> — {String(row.attribution)}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            )}

            {data.recommendation && (
              <div
                className="research-decision__engagement"
                aria-label="NorthStar recommendation"
                style={{ marginTop: '0.85rem', borderTop: '1px solid rgba(0,0,0,0.08)', paddingTop: '0.85rem' }}
              >
                <div className="research-decision__kicker">NorthStar Recommendation</div>
                <div className="research-decision__rating">{display(data.recommendation.action)}</div>
                <div className="research-decision__block">
                  <h3>Why</h3>
                  <p>{display(data.recommendation.why)}</p>
                </div>
                {data.recommendation.evidence_used.length > 0 ? (
                  <div className="signal-history" style={{ marginTop: '0.45rem' }}>
                    {data.recommendation.evidence_used.map((s) => (
                      <span key={s} className="opportunity-badge">
                        {s}
                      </span>
                    ))}
                  </div>
                ) : null}
                {data.recommendation.still_need_to_know.length > 0 ? (
                  <div className="research-decision__block">
                    <h3>What we still need to know</h3>
                    <ul className="ask-bullet-list">
                      {data.recommendation.still_need_to_know.map((g) => (
                        <li key={g}>{g}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                <p className="queue-sub">{display(data.recommendation.advisory_note)}</p>
              </div>
            )}

            <p className="queue-sub">
              Last researched: {display(data.last_researched_at)}
              {data.initiated_by ? ` · By ${data.initiated_by}` : ''}
            </p>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>WHAT NORTHSTAR ALREADY KNOWS</h2>
            </div>
            {known && (
              <dl className="ask-fact-grid">
                <div>
                  <dt>Company</dt>
                  <dd>{display(known.company_name)}</dd>
                </div>
                <div>
                  <dt>Website</dt>
                  <dd>{display(known.website)}</dd>
                </div>
                <div>
                  <dt>Location</dt>
                  <dd>
                    {[known.city, known.state].filter(Boolean).join(', ') || '—'}
                  </dd>
                </div>
                <div>
                  <dt>Working For status</dt>
                  <dd>{display(known.working_for_status)}</dd>
                </div>
                {known.opportunity_score != null && (
                  <div>
                    <dt>Opportunity score</dt>
                    <dd>{known.opportunity_score}</dd>
                  </div>
                )}
              </dl>
            )}
            {known?.milestones && known.milestones.length > 0 && (
              <div className="signal-history" style={{ marginTop: '0.75rem' }}>
                {known.milestones.map((m, i) => (
                  <span
                    key={`${m.milestone_type}-${m.client_name}-${i}`}
                    className="opportunity-badge"
                  >
                    {String(m.client_name || '')}: {String(m.milestone_type || '')}
                  </span>
                ))}
              </div>
            )}
            {known?.notes && known.notes.length > 0 && (
              <div className="ask-section">
                <h3>Recent notes</h3>
                <ul className="ask-history-list">
                  {known.notes.slice(0, 4).map((n, i) => (
                    <li key={`note-${i}`}>
                      <div className="queue-sub">
                        {String(n.client_name || '')} · {String(n.created_at || '')}
                      </div>
                      <p>{display(String(n.excerpt || ''))}</p>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>VERIFIED MATCHES</h2>
            </div>
            {data.verified_matches.length === 0 ? (
              <p className="queue-sub">No verified field matches in this run.</p>
            ) : (
              <ul className="ask-history-list">
                {data.verified_matches.map((item) => (
                  <li key={`vm-${item.field_key}`}>
                    <strong>{item.label}</strong>
                    <div>NorthStar: {display(item.northstar_value)}</div>
                    <div>Research: {display(item.research_value)}</div>
                    {item.source_url ? (
                      <a href={item.source_url} target="_blank" rel="noreferrer">
                        {item.source_name || 'Source'}
                      </a>
                    ) : (
                      <span className="queue-sub">{display(item.source_name)}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>POSSIBLE CHANGES / SUGGESTED CRM UPDATES</h2>
            </div>
            {data.possible_changes.length === 0 && data.proposed_updates.filter((p) => p.status === 'Pending Review').length === 0 ? (
              <p className="queue-sub">No proposed master-field updates.</p>
            ) : (
              <ul className="ask-history-list">
                {(data.possible_changes.length
                  ? data.possible_changes
                  : data.proposed_updates
                      .filter((p) => p.status === 'Pending Review')
                      .map((p) => ({
                        field_key: p.field_key,
                        label: p.field_key,
                        northstar_value: p.current_value,
                        research_value: p.proposed_value,
                        source_name: p.source_name,
                        source_url: p.source_url,
                        match: false,
                        proposed_update_id: p.id,
                      }))
                ).map((item) => (
                  <li key={`pc-${item.field_key}-${item.proposed_update_id}`}>
                    <strong>{item.label}</strong>
                    <div>NorthStar: {display(item.northstar_value)}</div>
                    <div>Research: {display(item.research_value)}</div>
                    <div className="queue-sub">
                      Source: {display(item.source_name)}
                      {item.source_url ? (
                        <>
                          {' · '}
                          <a href={item.source_url} target="_blank" rel="noreferrer">
                            Open source
                          </a>
                        </>
                      ) : null}
                    </div>
                    {item.proposed_update_id ? (
                      <div className="opportunity-actions" style={{ marginTop: '0.5rem' }}>
                        <button
                          type="button"
                          className="primary-btn"
                          onClick={() => void onApprove(item.proposed_update_id as number)}
                        >
                          Approve Update
                        </button>
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() => void onReject(item.proposed_update_id as number)}
                        >
                          Reject
                        </button>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
            <p className="queue-sub">
              Approvals write only the selected master company field and create an audit trail.
              CRM status is never changed by research.
            </p>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>RESEARCH EVIDENCE</h2>
            </div>
            <FindingList title="COMPANY OVERVIEW" items={data.overview} />
            <FindingList title="LOCATIONS" items={data.locations} />
            <FindingList title="PRODUCTS / SERVICES" items={data.products || []} />
            <FindingList title="CAPABILITIES" items={data.capabilities} />
            <FindingList title="MATERIALS" items={data.materials || []} />
            <FindingList title="INDUSTRIES / MARKETS" items={data.industries} />
            <FindingList title="RECENT DEVELOPMENTS" items={data.recent_developments} />
            {(() => {
              const cites: Array<{ url: string; title?: string }> = [
                ...(data.citations || []),
                ...((data.job?.citations || []) as Array<{ url?: string; title?: string }>),
                ...((data.sources || []) as Array<{ source_url?: string; url?: string; source_name?: string; title?: string; page_title?: string }>).map(
                  (s) => ({
                    url: String(s.url || s.source_url || ''),
                    title: String(s.title || s.page_title || s.source_name || ''),
                  }),
                ),
              ]
                .map((c) => ({
                  url: String(c.url || '').trim(),
                  title: c.title,
                }))
                .filter((c) => Boolean(c.url) && /^https?:\/\//i.test(c.url))
              const seen = new Set<string>()
              const unique = cites.filter((c) => {
                if (seen.has(c.url)) return false
                seen.add(c.url)
                return true
              })
              if (!unique.length) return null
              return (
                <div className="ask-section">
                  <h3>SOURCE CITATIONS</h3>
                  <ul className="ask-bullet-list">
                    {unique.map((c) => (
                      <li key={c.url}>
                        <a href={c.url} target="_blank" rel="noreferrer">
                          {display(c.title || c.url)}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              )
            })()}
            {data.summary ? (
              <div className="ask-section">
                <h3>DETAILED RESEARCH NOTES</h3>
                <p className="queue-sub">{data.summary}</p>
              </div>
            ) : null}
            {data.pages_researched && data.pages_researched.length > 0 ? (
              <div className="ask-section">
                <h3>PAGES RESEARCHED</h3>
                <ul className="ask-bullet-list">
                  {data.pages_researched.map((p, i) => (
                    <li key={`page-${i}`}>
                      {String(p.label || p.title || 'Page')}
                      {p.url ? (
                        <>
                          {' · '}
                          <a href={String(p.url)} target="_blank" rel="noreferrer">
                            Open
                          </a>
                        </>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>NORTHSTAR CROSS-CLIENT EXPERIENCE</h2>
            </div>
            {data.cross_client_experience.length === 0 ? (
              <p className="queue-sub">No other authorized client history for this company.</p>
            ) : (
              <ul className="ask-history-list">
                {data.cross_client_experience.map((row, i) => (
                  <li key={`cc-${i}-${String(row.client_name || '')}`}>
                    <strong>{display(String(row.client_name || row.role || ''))}</strong>
                    {row.status ? <div>Status: {String(row.status)}</div> : null}
                    {row.external_record_no ? (
                      <div>Record No. {String(row.external_record_no)}</div>
                    ) : null}
                    {row.opportunity_score != null ? (
                      <div>Opportunity score: {String(row.opportunity_score)}</div>
                    ) : null}
                    {row.attribution ? (
                      <div className="queue-sub">{String(row.attribution)}</div>
                    ) : null}
                    {Array.isArray(row.source_clients) && row.source_clients.length > 0 ? (
                      <div className="queue-sub">
                        Source client(s): {(row.source_clients as string[]).join(', ')}
                      </div>
                    ) : null}
                    {Array.isArray(row.signals) && row.signals.length > 0 && (
                      <div className="signal-history" style={{ marginTop: '0.35rem' }}>
                        {(row.signals as string[]).map((s) => (
                          <span key={s} className="opportunity-badge">
                            {s}
                          </span>
                        ))}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {fit && (
            <section className="panel">
              <div className="panel-header">
                <h2>CAMPAIGN FIT EVIDENCE</h2>
                {fit.campaign_name ? (
                  <span className="ask-answer-scope">
                    {fit.client_name} · {fit.campaign_name}
                  </span>
                ) : (
                  <span className="ask-answer-scope">{fit.client_name}</span>
                )}
              </div>
              <p>
                Rating: <strong>{fit.fit_result}</strong>
              </p>
              <p>{fit.why}</p>
              {fit.profile_incomplete ? (
                <p className="queue-sub">
                  Target profile incomplete for Strong Fit. Fields used:{' '}
                  {(fit.profile_fields_used || []).join(', ') || 'none'}.
                  Missing richer ICP fields have not been invented.
                </p>
              ) : null}
              {fit.evidence_chains && fit.evidence_chains.length > 0 && (
                <>
                  <h3>Evidence chain</h3>
                  <ul className="ask-history-list">
                    {fit.evidence_chains.map((chain, i) => (
                      <li key={`chain-${i}`}>
                        <strong>{display(String(chain.fact || ''))}</strong>
                        <div className="queue-sub">→ {display(String(chain.why || ''))}</div>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {fit.supporting_evidence.length > 0 && (
                <>
                  <h3>Supporting evidence</h3>
                  <ul className="ask-bullet-list">
                    {fit.supporting_evidence.map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                </>
              )}
              {fit.potential_opportunity ? (
                <>
                  <h3>Potential opportunity notes</h3>
                  <p>{fit.potential_opportunity}</p>
                </>
              ) : null}
              {fit.concerns.length > 0 && (
                <>
                  <h3>Concerns / gaps</h3>
                  <ul className="ask-bullet-list">
                    {fit.concerns.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                </>
              )}
            </section>
          )}

          <section className="panel">
            <div className="panel-header">
              <h2>MISSING INFORMATION</h2>
            </div>
            {data.missing_information.length === 0 ? (
              <p className="queue-sub">No explicit gaps listed for this run.</p>
            ) : (
              <ul className="ask-bullet-list">
                {data.missing_information.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>
                NorthStar Contacts (
                {data.northstar_contacts_total ||
                  data.northstar_known?.contacts_total ||
                  data.northstar_contacts.length}
                )
              </h2>
            </div>
            {data.northstar_contacts.length === 0 ? (
              <p className="queue-sub">No NorthStar contacts on file.</p>
            ) : (
              <>
                <p className="queue-sub">
                  Showing campaign-relevant contacts first. Full master contact set is used for
                  CRM matching.
                </p>
                <ul className="ask-contact-list">
                  {data.northstar_contacts.map((c, i) => (
                    <li key={`ns-c-${i}`}>
                      <strong>{display(String(c.name || ''))}</strong>
                      {c.title ? <span> · {String(c.title)}</span> : null}
                      <div className="queue-sub">NorthStar Contact</div>
                    </li>
                  ))}
                </ul>
                {(data.northstar_contacts_total ||
                  data.northstar_known?.contacts_total ||
                  0) > data.northstar_contacts.length ? (
                  <p style={{ marginTop: '0.65rem' }}>
                    <Link
                      className="link-btn"
                      to={`/companies/${encodeURIComponent(
                        data.working_for_record_no ||
                          known?.master_record_no ||
                          recordNo,
                      )}?client_id=${data.working_for_client_id || workingForClientId || ''}`}
                    >
                      View All Contacts
                    </Link>
                  </p>
                ) : null}
              </>
            )}
            <div className="panel-header" style={{ marginTop: '1rem' }}>
              <h2>Publicly Found Contacts</h2>
            </div>
            {data.public_contacts.length === 0 ? (
              <p className="queue-sub">No publicly listed contacts identified in this run.</p>
            ) : (
              <>
                <p className="queue-sub" style={{ marginBottom: '0.65rem' }}>
                  Select contacts to add in bulk, or open an individual contact to review first.
                </p>
                <ul className="ask-contact-list">
                  {data.public_contacts.map((c, i) => {
                    const matchStatus = c.crm_match_status || 'new'
                    const matchKind = publicCrmMatchKind(matchStatus)
                    const isExisting = matchKind === 'existing'
                    const isPossible = matchKind === 'possible'
                    const isNew = matchKind === 'new'
                    const isNotTarget =
                      (c.relevance_tier || '').toUpperCase() === 'NOT_TARGET'
                    const canAdd =
                      workingForClientId != null && workingForClientId > 0
                    const sourceBits =
                      c.source_labels && c.source_labels.length
                        ? c.source_labels
                        : [c.source_name || 'Public web']

                    const openExistingRecord = () => {
                      if (!c.crm_matched_contact_id) return
                      navigate(researchContactHref(c.crm_matched_contact_id))
                    }
                    const openDetail = () => setDetailContact(c)
                    const addOne = () => {
                      setAddContacts([toAddContactInput(c)])
                      setDetailContact(null)
                      setAddOpen(true)
                    }
                    const openFromName = () => {
                      if (isExisting) openExistingRecord()
                      else openDetail()
                    }

                    return (
                      <li
                        key={`pub-c-${i}`}
                        style={isNotTarget ? { opacity: 0.72 } : undefined}
                      >
                        <div className="public-contact-card">
                          <input
                            type="checkbox"
                            disabled={isExisting}
                            checked={selectedPublicIdx.has(i)}
                            aria-label={`Select ${c.contact_name || 'contact'} for bulk add`}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) => {
                              setSelectedPublicIdx((prev) => {
                                const next = new Set(prev)
                                if (e.target.checked) next.add(i)
                                else next.delete(i)
                                return next
                              })
                            }}
                          />
                          <div className="public-contact-card__body">
                            <div className="public-contact-card__header">
                              <button
                                type="button"
                                className="link-btn"
                                data-testid={`public-contact-name-${i}`}
                                style={{
                                  fontWeight: 700,
                                  padding: 0,
                                  fontSize: 'inherit',
                                  textAlign: 'left',
                                  cursor: 'pointer',
                                }}
                                onClick={openFromName}
                              >
                                {display(c.contact_name || c.value)}
                              </button>
                              <span
                                className={`public-contact-badge public-contact-badge--${matchKind}`}
                                data-testid={`public-contact-badge-${i}`}
                              >
                                {publicCrmBadgeLabel(matchKind)}
                              </span>
                            </div>
                            {c.contact_title ? (
                              <div className="public-contact-card__title">
                                {c.contact_title}
                              </div>
                            ) : null}

                            {isExisting ? (
                              c.crm_matched_contact_name ? (
                                <div className="queue-sub">
                                  Matched CRM contact: {c.crm_matched_contact_name}
                                </div>
                              ) : null
                            ) : (
                              <>
                                <div className="queue-sub">
                                  Relevance: {relevanceLabel(c.relevance_tier)}
                                  {c.matched_persona
                                    ? ` · Matched Persona: ${c.matched_persona}`
                                    : ''}
                                </div>
                                {c.why_relevant && !isPossible ? (
                                  <div className="queue-sub">
                                    Why Relevant: {c.why_relevant}
                                  </div>
                                ) : null}
                                {isPossible &&
                                (c.crm_match_reasons || []).length > 0 ? (
                                  <div className="queue-sub">
                                    Match reasons: {(c.crm_match_reasons || []).join(', ')}
                                  </div>
                                ) : null}
                                <div className="queue-sub">
                                  Sources: {sourceBits.join(' + ')}
                                  {c.linkedin_url ? (
                                    <>
                                      {' · '}
                                      <a
                                        href={c.linkedin_url}
                                        target="_blank"
                                        rel="noreferrer"
                                      >
                                        LinkedIn
                                      </a>
                                    </>
                                  ) : null}
                                  {!c.linkedin_url && c.source_url ? (
                                    <>
                                      {' · '}
                                      <a
                                        href={c.source_url}
                                        target="_blank"
                                        rel="noreferrer"
                                      >
                                        Open
                                      </a>
                                    </>
                                  ) : null}
                                  {c.linkedin_url &&
                                  c.source_url &&
                                  c.source_url !== c.linkedin_url ? (
                                    <>
                                      {' · '}
                                      <a
                                        href={c.source_url}
                                        target="_blank"
                                        rel="noreferrer"
                                      >
                                        Other source
                                      </a>
                                    </>
                                  ) : null}
                                  {' · '}
                                  Confidence: {display(c.confidence || 'medium')}
                                </div>
                              </>
                            )}

                            <div className="public-contact-card__actions">
                              {isNew ? (
                                <>
                                  <button
                                    type="button"
                                    className="secondary-btn"
                                    data-testid={`public-contact-view-${i}`}
                                    onClick={openDetail}
                                  >
                                    View Contact
                                  </button>
                                  <button
                                    type="button"
                                    className="primary-btn"
                                    data-testid={`public-contact-add-${i}`}
                                    disabled={!canAdd}
                                    title={
                                      canAdd
                                        ? undefined
                                        : 'Select Working For client first'
                                    }
                                    onClick={addOne}
                                  >
                                    Add to NorthStar
                                  </button>
                                </>
                              ) : null}
                              {isExisting ? (
                                <button
                                  type="button"
                                  className="primary-btn"
                                  data-testid={`public-contact-open-${i}`}
                                  disabled={!c.crm_matched_contact_id}
                                  onClick={openExistingRecord}
                                >
                                  Open Contact Record
                                </button>
                              ) : null}
                              {isPossible ? (
                                <button
                                  type="button"
                                  className="primary-btn"
                                  data-testid={`public-contact-review-${i}`}
                                  onClick={openDetail}
                                >
                                  Review Match
                                </button>
                              ) : null}
                            </div>
                          </div>
                        </div>
                      </li>
                    )
                  })}
                </ul>
                <div className="setup-actions" style={{ marginTop: '0.65rem' }}>
                  <button
                    type="button"
                    className="primary-btn"
                    disabled={
                      selectedPublicIdx.size === 0 ||
                      !(workingForClientId != null && workingForClientId > 0)
                    }
                    onClick={() => {
                      const selected = data.public_contacts
                        .map((c, i) => ({ c, i }))
                        .filter(({ i }) => selectedPublicIdx.has(i))
                        .filter(({ c }) => (c.crm_match_status || 'new') !== 'existing')
                        .map(({ c }) => toAddContactInput(c))
                      setAddContacts(selected)
                      setAddOpen(true)
                    }}
                  >
                    Add Selected to NorthStar ({selectedPublicIdx.size})
                  </button>
                  {!(workingForClientId != null && workingForClientId > 0) ? (
                    <span className="queue-sub">Select Working For client first.</span>
                  ) : null}
                </div>
                <p className="queue-sub">
                  Checkboxes are for bulk add only. Use View Contact, Add to NorthStar, Open
                  Contact Record, or Review Match for an individual contact. Existing matches
                  cannot be re-added. Not Target contacts are de-emphasized but remain
                  reviewable. Email/phone stay blank unless a public source provided them.
                </p>
              </>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>SOURCES REVIEWED</h2>
            </div>
            <ul className="ask-history-list">
              {data.sources.map((s, i) => (
                <li key={`src-${i}`}>
                  <strong>{display(String(s.source_name || s.finding_type || 'Source'))}</strong>
                  <div className="queue-sub">
                    {s.researched_at ? `Researched: ${String(s.researched_at)}` : ''}
                    {s.source_url ? (
                      <>
                        {' · '}
                        <a href={String(s.source_url)} target="_blank" rel="noreferrer">
                          Open source
                        </a>
                      </>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section className="panel ask-research-panel">
            <div className="panel-header">
              <h2>DATA PROVIDER ENRICHMENT</h2>
            </div>
            <p>
              <strong>{display(String(data.data_provider?.status || 'ZoomInfo not connected'))}</strong>
            </p>
            <p>
              When ZoomInfo finds a person or company that is not already in NorthStar, use Add to
              NorthStar. Nothing is created automatically from search results.
            </p>
            <div className="heading-controls" style={{ margin: '0.75rem 0' }}>
              <button
                type="button"
                className="primary-btn"
                onClick={() => setZoomInfoAddKind('company')}
              >
                Add to NorthStar
              </button>
            </div>
            <p className="queue-sub">Future capabilities may include:</p>
            <ul className="ask-bullet-list">
              {(
                (data.data_provider?.future_capabilities as string[] | undefined) || [
                  'Company firmographics',
                  'Employee count',
                  'Revenue',
                  'Decision makers',
                  'Titles',
                  'Business emails',
                  'Direct phones',
                  'Mobile phones',
                ]
              ).map((cap) => (
                <li key={cap}>{cap}</li>
              ))}
            </ul>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2>RESEARCH HISTORY</h2>
            </div>
            {data.research_history.length === 0 ? (
              <p className="queue-sub">No prior research runs.</p>
            ) : (
              <ul className="ask-history-list">
                {data.research_history.map((h) => (
                  <li key={String(h.research_run_id)}>
                    <strong>{display(String(h.completed_at || ''))}</strong>
                    <div className="queue-sub">
                      {display(String(h.initiated_by || ''))}
                      {h.providers_used ? ` · ${String(h.providers_used)}` : ''}
                    </div>
                    <p>{display(String(h.summary || ''))}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {data.working_for_record_no || known?.master_record_no ? (
            <p>
              <Link
                className="link-btn"
                to={`/companies/${encodeURIComponent(
                  data.working_for_record_no || known?.master_record_no || recordNo,
                )}?client_id=${data.working_for_client_id || ''}`}
              >
                Open Company Workspace
              </Link>
            </p>
          ) : null}
        </div>
      )}

      {deepConfirmOpen
        ? createPortal(
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby="deep-research-confirm-title"
              data-testid="deep-research-confirm-modal"
              style={{
                position: 'fixed',
                inset: 0,
                background: 'rgba(15, 23, 42, 0.45)',
                zIndex: 210,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: 16,
              }}
            >
              <div
                className="panel"
                style={{ maxWidth: 520, width: '100%', background: '#fff', padding: 20 }}
              >
                <h2 id="deep-research-confirm-title">Confirm Deep Research</h2>
                <p>
                  <strong>Deep Research uses paid OpenAI API services.</strong>
                </p>
                <ul className="ask-bullet-list">
                  <li>
                    Maximum {deepStatus?.limits?.max_web_search_calls ?? 10} web searches
                  </li>
                  <li>
                    Maximum {deepStatus?.limits?.max_output_tokens ?? 6000} generated tokens
                  </li>
                  <li>
                    Estimated cost:{' '}
                    {deepStatus?.pricing?.estimate_available &&
                    deepStatus?.limits?.estimated_max_run_usd != null
                      ? `up to $${Number(deepStatus.limits.estimated_max_run_usd).toFixed(4)} (output+search ceiling; input not included)`
                      : 'Exact cost unavailable'}
                  </li>
                  <li>
                    Remaining monthly NorthStar allowance:{' '}
                    {deepStatus?.monthly?.remaining_usd != null &&
                    deepStatus.monthly.status !== 'unavailable'
                      ? `$${Number(deepStatus.monthly.remaining_usd).toFixed(2)}`
                      : 'Not calculable until pricing rates are configured'}
                  </li>
                  {deepPaidRefresh ? (
                    <li>
                      A completed Deep Research result is still within the{' '}
                      {deepStatus?.limits?.cache_days ?? 30}-day cache. This confirms a paid
                      refresh.
                    </li>
                  ) : null}
                </ul>
                <div className="heading-controls" style={{ marginTop: 16 }}>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => {
                      setDeepConfirmOpen(false)
                      setDeepPaidRefresh(false)
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="primary-btn"
                    disabled={running || inFlightRef.current}
                    onClick={() => {
                      if (running || inFlightRef.current) return
                      setDeepConfirmOpen(false)
                      void load({
                        run: true,
                        force: true,
                        depth: 'deep',
                        confirmPaidRefresh: deepPaidRefresh,
                      })
                      setDeepPaidRefresh(false)
                    }}
                  >
                    {deepPaidRefresh ? 'Confirm paid refresh' : 'Start Deep Research'}
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}

      {detailContact && data
        ? createPortal(
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby="public-contact-detail-title"
              data-testid="public-contact-detail-modal"
              style={{
                position: 'fixed',
                inset: 0,
                background: 'rgba(15, 23, 42, 0.45)',
                zIndex: 200,
                overflow: 'auto',
                padding: '1.5rem',
              }}
              onClick={() => setDetailContact(null)}
            >
              <section
                className="panel add-note-card"
                style={{ maxWidth: '36rem', margin: '2rem auto' }}
                onClick={(e) => e.stopPropagation()}
              >
                {(() => {
                  const c = detailContact
                  const matchStatus = c.crm_match_status || 'new'
                  const isPossible = matchStatus === 'possible_match'
                  const sources = c.source_labels?.length
                    ? c.source_labels
                    : [c.source_name || 'Public web'].filter(Boolean)
                  const canAdd =
                    workingForClientId != null && workingForClientId > 0
                  return (
                    <>
                      <h2
                        id="public-contact-detail-title"
                        className="add-note-card__title"
                      >
                        Contact Research Detail
                      </h2>
                      <p>
                        <strong>{display(c.contact_name || c.value)}</strong>
                        {c.contact_title ? ` — ${c.contact_title}` : ''}
                      </p>
                      <ul className="ask-history-list">
                        <li>
                          <div className="queue-sub">Company</div>
                          <div>{display(data.company_name)}</div>
                        </li>
                        <li>
                          <div className="queue-sub">Matched Persona</div>
                          <div>{display(c.matched_persona)}</div>
                        </li>
                        <li>
                          <div className="queue-sub">Relevance</div>
                          <div>{relevanceLabel(c.relevance_tier)}</div>
                        </li>
                        <li>
                          <div className="queue-sub">Why Relevant</div>
                          <div>{display(c.why_relevant)}</div>
                        </li>
                        <li>
                          <div className="queue-sub">Confidence</div>
                          <div>{display(c.confidence)}</div>
                        </li>
                        <li>
                          <div className="queue-sub">CRM Match</div>
                          <div>
                            {display(c.crm_match_label || matchStatus)}
                            {c.crm_matched_contact_name
                              ? ` — ${c.crm_matched_contact_name}`
                              : ''}
                          </div>
                        </li>
                        {isPossible ? (
                          <li>
                            <div className="queue-sub">Match Reasons</div>
                            <div>
                              {(c.crm_match_reasons || []).join(', ') ||
                                'Name/title similarity'}
                            </div>
                          </li>
                        ) : null}
                        <li>
                          <div className="queue-sub">Sources</div>
                          <div>
                            {sources.join(' + ')}
                            {c.linkedin_url ? (
                              <>
                                {' · '}
                                <a
                                  href={c.linkedin_url}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  LinkedIn profile
                                </a>
                              </>
                            ) : null}
                            {c.source_url && c.source_url !== c.linkedin_url ? (
                              <>
                                {' · '}
                                <a
                                  href={c.source_url}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  Public source
                                </a>
                              </>
                            ) : null}
                          </div>
                        </li>
                        <li>
                          <div className="queue-sub">Evidence summary</div>
                          <div>
                            Public research found {display(c.contact_name)} as{' '}
                            {display(c.contact_title)}
                            {c.linkedin_derived
                              ? ' via LinkedIn search evidence'
                              : ''}
                            {sources.length > 1
                              ? ' with corroborating public sources'
                              : ''}
                            . Email and phone were not invented.
                          </div>
                        </li>
                      </ul>
                      {!canAdd ? (
                        <p className="queue-sub">
                          Select Working For client before adding this contact.
                        </p>
                      ) : null}
                      <div className="setup-actions" style={{ marginTop: '1rem' }}>
                        {isPossible ? (
                          <>
                            <button
                              type="button"
                              className="primary-btn"
                              disabled={!c.crm_matched_contact_id}
                              onClick={() => {
                                if (c.crm_matched_contact_id) {
                                  navigate(researchContactHref(c.crm_matched_contact_id))
                                }
                                setDetailContact(null)
                              }}
                            >
                              Use Existing
                            </button>
                            <button
                              type="button"
                              className="primary-btn"
                              disabled={!canAdd}
                              onClick={() => {
                                setAddContacts([toAddContactInput(c)])
                                setDetailContact(null)
                                setAddOpen(true)
                              }}
                            >
                              Create New
                            </button>
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => setDetailContact(null)}
                            >
                              Cancel
                            </button>
                          </>
                        ) : matchStatus === 'existing' ? (
                          <>
                            <button
                              type="button"
                              className="primary-btn"
                              disabled={!c.crm_matched_contact_id}
                              onClick={() => {
                                if (c.crm_matched_contact_id) {
                                  navigate(researchContactHref(c.crm_matched_contact_id))
                                }
                                setDetailContact(null)
                              }}
                            >
                              Open NorthStar Contact
                            </button>
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => setDetailContact(null)}
                            >
                              Close
                            </button>
                          </>
                        ) : (
                          <>
                            <button
                              type="button"
                              className="primary-btn"
                              data-testid="detail-add-to-northstar"
                              disabled={!canAdd}
                              onClick={() => {
                                setAddContacts([toAddContactInput(c)])
                                setDetailContact(null)
                                setAddOpen(true)
                              }}
                            >
                              Add to NorthStar
                            </button>
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => setDetailContact(null)}
                            >
                              Close
                            </button>
                          </>
                        )}
                      </div>
                    </>
                  )
                })()}
              </section>
            </div>,
            document.body,
          )
        : null}

      {zoomInfoAddKind && workingForClientId != null && workingForClientId > 0 && data ? (
        <ZoomInfoAddModal
          open
          onClose={() => setZoomInfoAddKind(null)}
          clientId={workingForClientId}
          clientName={data.working_for_client_name || defaultClientName}
          kind={zoomInfoAddKind}
          lockedCompanyId={zoomInfoAddKind === 'contact' ? data.company_id : null}
          lockedCompanyName={data.company_name}
          zoominfo={{
            company_name: data.company_name || '',
            website: data.northstar_known?.website || '',
            address: data.northstar_known?.address || '',
            city: data.northstar_known?.city || '',
            state: data.northstar_known?.state || '',
          }}
        />
      ) : null}
      {addOpen && workingForClientId != null && workingForClientId > 0 && data ? (
        <AddToNorthStar
          open={addOpen}
          onClose={() => {
            setAddOpen(false)
            setAddContacts([])
          }}
          clientId={workingForClientId}
          clientName={data.working_for_client_name || defaultClientName}
          company={{
            company_name: data.company_name || '',
            website: data.northstar_known?.website || '',
            address: data.northstar_known?.address || '',
            city: data.northstar_known?.city || '',
            state: data.northstar_known?.state || '',
            phone: '',
            industry: '',
          }}
          contacts={addContacts}
          knownCompanyId={data.company_id}
          trustedExternalRecordNo={
            data.northstar_known?.master_record_no ||
            recordNo ||
            data.working_for_record_no ||
            ''
          }
          researchRunId={data.research_run_id}
          source="AI Research public contacts"
          provider={String(data.data_provider?.status || 'public_web')}
          mode="contacts_only"
        />
      ) : null}
    </>
  )
}
