import { useEffect, useMemo, useState } from 'react'
import {
  analyzeDuplicateCandidates,
  confirmDuplicateBatchReview,
  fetchDuplicateCandidates,
  fetchDuplicatePair,
  fetchDuplicateReviewHistory,
  fetchMergePlan,
  fetchMergePlans,
  prepareMergePlans,
  previewDuplicateBatchReview,
  replanMergePlan,
  saveDuplicateReview,
  saveMergePlanDecision,
  saveWorkbenchDisposition,
  type DuplicateAutomatedAssessment,
  type DuplicateBatchPreview,
  type DuplicateCandidate,
  type DuplicateCompanyDetail,
  type DuplicateDisposition,
  type DuplicatePairDetail,
  type DuplicateSummary,
  type MergePlanDetail,
  type MergePlanListRow,
  type MergePlanSummary,
} from './api/duplicateReview'
import MergePlanningPanel from './AdministrationDuplicateWorkbench'

const QUEUES: Array<{ id: string; label: string }> = [
  { id: 'review_needed', label: 'Review Needed' },
  { id: 'high_confidence', label: 'High Confidence Duplicates' },
  { id: 'likely_duplicate_auto', label: 'Likely Duplicate (auto)' },
  { id: 'likely_multi_location', label: 'Likely Multi-Location' },
  { id: 'likely_not_duplicate', label: 'Likely Not Duplicate' },
  { id: 'human_review_required', label: 'Human Review Required' },
  { id: 'insufficient_evidence', label: 'Insufficient Evidence' },
  { id: 'unreviewed', label: 'Unreviewed (human)' },
  { id: 'likely_duplicate', label: 'Likely Duplicate (Julie)' },
  { id: 'not_duplicate', label: 'Not Duplicate (Julie)' },
  { id: 'multi_location', label: 'Multi-Location (Julie)' },
  { id: 'needs_research', label: 'Needs Research' },
  { id: 'merge_candidate', label: 'Merge Candidate' },
  { id: 'all', label: 'All' },
]

const BATCH_ACTIONS: Array<{ id: string; label: string }> = [
  { id: 'ACCEPT_LIKELY_DUPLICATE', label: 'Accept as Likely Duplicate' },
  { id: 'ACCEPT_MULTI_LOCATION', label: 'Accept as Multi-Location' },
  { id: 'ACCEPT_NOT_DUPLICATE', label: 'Accept as Not Duplicate' },
  { id: 'SEND_NEEDS_RESEARCH', label: 'Send to Needs Research' },
]

const DISPOSITIONS: Array<{ id: DuplicateDisposition; label: string }> = [
  { id: 'LIKELY_DUPLICATE', label: 'Likely Duplicate' },
  { id: 'NOT_DUPLICATE', label: 'Not Duplicate' },
  { id: 'MULTI_LOCATION', label: 'Multi-Location' },
  { id: 'NEEDS_RESEARCH', label: 'Needs Research' },
  { id: 'MERGE_CANDIDATE', label: 'Merge Candidate' },
]

const EMPTY_SUMMARY: DuplicateSummary = {
  candidates: 0,
  HIGH_CONFIDENCE_DUPLICATE: 0,
  LIKELY_DUPLICATE: 0,
  LIKELY_MULTI_LOCATION: 0,
  LIKELY_NOT_DUPLICATE: 0,
  HUMAN_REVIEW_REQUIRED: 0,
  INSUFFICIENT_EVIDENCE: 0,
  review_needed: 0,
}

const EMPTY_PLAN_SUMMARY: MergePlanSummary = {
  analyzed: 0,
  ready_for_review: 0,
  needs_exception_decision: 0,
  not_safe_to_plan: 0,
  stale: 0,
  simple_decisions: 0,
  complex_decisions: 0,
}

function cityState(company: { city?: string; state?: string }) {
  return [company.city, company.state].filter(Boolean).join(', ')
}

function classLabel(value?: string) {
  return (value || 'Unclassified').replace(/_/g, ' ')
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

function AssessmentPanel({
  assessment,
  companyA,
  companyB,
}: {
  assessment?: DuplicateAutomatedAssessment
  companyA: DuplicateCompanyDetail
  companyB: DuplicateCompanyDetail
}) {
  if (!assessment) {
    return (
      <section className="dup-assessment" aria-label="NorthStar Automated Assessment">
        <h3>NorthStar Automated Assessment</h3>
        <p>Run Analyze Duplicate Candidates to persist classifications. A local rules assessment is shown once a pair is opened.</p>
      </section>
    )
  }
  const survivorId = assessment.proposed_survivor_company_id
  const survivor =
    survivorId === companyA.company_id
      ? companyA
      : survivorId === companyB.company_id
        ? companyB
        : null
  return (
    <section className="dup-assessment" aria-label="NorthStar Automated Assessment">
      <h3>NorthStar Automated Assessment</h3>
      <p className="queue-sub">
        Deterministic duplicate analysis. This is not an external AI model and is not merge authorization.
      </p>
      <FieldRow label="Classification" value={classLabel(assessment.classification)} />
      <FieldRow label="Confidence" value={assessment.confidence || '—'} />
      {assessment.stale ? <p className="dup-stale">Automated classification is stale and should be recalculated.</p> : null}
      <h4>Why</h4>
      <ul>
        {(assessment.why || []).map((row) => (
          <li key={row}>✓ {row}</li>
        ))}
      </ul>
      {(assessment.concern_labels || []).length ? (
        <>
          <h4>Concerns</h4>
          <ul>
            {(assessment.concern_labels || []).map((row) => (
              <li key={row}>⚠ {row}</li>
            ))}
          </ul>
        </>
      ) : null}
      <FieldRow
        label="Proposed survivor"
        value={
          assessment.survivor_decision_required
            ? 'NONE · SURVIVOR DECISION REQUIRED'
            : survivor
              ? `Company ${survivor.company_id} · ${survivor.company_name}`
              : 'NONE'
        }
      />
      {assessment.survivor_reason ? <p>Why survivor: {assessment.survivor_reason}</p> : null}
    </section>
  )
}

export default function AdministrationDuplicateReview() {
  const [queue, setQueue] = useState('review_needed')
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<{ total: number; pairs: DuplicateCandidate[]; summary: DuplicateSummary }>({
    total: 0,
    pairs: [],
    summary: EMPTY_SUMMARY,
  })
  const [selected, setSelected] = useState<DuplicatePairDetail | null>(null)
  const [history, setHistory] = useState<Array<Record<string, unknown>>>([])
  const [formDisposition, setFormDisposition] = useState<DuplicateDisposition>('LIKELY_DUPLICATE')
  const [reason, setReason] = useState('')
  const [survivorId, setSurvivorId] = useState<number | ''>('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [checked, setChecked] = useState<Record<string, boolean>>({})
  const [selectFiltered, setSelectFiltered] = useState(false)
  const [batchAction, setBatchAction] = useState('ACCEPT_LIKELY_DUPLICATE')
  const [batchReason, setBatchReason] = useState('')
  const [preview, setPreview] = useState<DuplicateBatchPreview | null>(null)
  const [workspace, setWorkspace] = useState<'review' | 'planning'>('review')
  const [planState, setPlanState] = useState('NEEDS_EXCEPTION_DECISION')
  const [planQuery, setPlanQuery] = useState('')
  const [planDecisionType, setPlanDecisionType] = useState('')
  const [planSameClient, setPlanSameClient] = useState('')
  const [planOffset, setPlanOffset] = useState(0)
  const [planPage, setPlanPage] = useState<{
    total: number
    plans: MergePlanListRow[]
    summary: MergePlanSummary
  }>({ total: 0, plans: [], summary: EMPTY_PLAN_SUMMARY })
  const [selectedPlan, setSelectedPlan] = useState<MergePlanDetail | null>(null)
  const [focusedExceptionKey, setFocusedExceptionKey] = useState('')
  const [readyNextPair, setReadyNextPair] = useState<MergePlanListRow | null>(null)
  const [decisionChoices, setDecisionChoices] = useState<Record<string, string>>({})
  const [decisionReason, setDecisionReason] = useState('')
  const limit = 50

  async function loadList(nextOffset = offset) {
    setBusy(true)
    setError('')
    try {
      const result = await fetchDuplicateCandidates({
        queue,
        q: query,
        offset: nextOffset,
        limit,
      })
      setPage({ total: result.total, pairs: result.pairs, summary: result.summary || EMPTY_SUMMARY })
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
  }, [queue])

  useEffect(() => {
    if (workspace === 'planning') void loadPlans(0, planState)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace, planState, planDecisionType, planSameClient])

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

  const selectedKeys = useMemo(
    () => Object.entries(checked).filter(([, on]) => on).map(([key]) => key),
    [checked],
  )

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

  async function analyze() {
    setBusy(true)
    setError('')
    try {
      const result = await analyzeDuplicateCandidates()
      setMessage(
        `Candidates analyzed: ${result.candidates_analyzed}. High-confidence duplicate: ${result.buckets.HIGH_CONFIDENCE_DUPLICATE || 0}. Likely duplicate: ${result.buckets.LIKELY_DUPLICATE || 0}. Likely multi-location: ${result.buckets.LIKELY_MULTI_LOCATION || 0}. Likely not duplicate: ${result.buckets.LIKELY_NOT_DUPLICATE || 0}. Human review required: ${result.buckets.HUMAN_REVIEW_REQUIRED || 0}. Insufficient evidence: ${result.buckets.INSUFFICIENT_EVIDENCE || 0}.`,
      )
      await loadList(0)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not analyze candidates.')
    } finally {
      setBusy(false)
    }
  }

  async function runPreview() {
    if (batchReason.trim().length < 3) {
      setError('A short reason is required.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await previewDuplicateBatchReview({
        action: batchAction,
        reason: batchReason,
        selection_mode: selectFiltered ? 'filtered' : 'explicit',
        pair_keys: selectFiltered ? [] : selectedKeys,
        queue,
        q: query,
      })
      setPreview(result)
      setMessage(
        `Preview: ${result.selected} selected · ${result.eligible} eligible · ${result.excluded} require individual review.`,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not preview batch review.')
    } finally {
      setBusy(false)
    }
  }

  async function runConfirm() {
    if (!preview) return
    setBusy(true)
    setError('')
    try {
      const result = await confirmDuplicateBatchReview({
        action: batchAction,
        reason: batchReason,
        selection_mode: selectFiltered ? 'filtered' : 'explicit',
        pair_keys: selectFiltered ? [] : selectedKeys,
        queue,
        q: query,
        preview_fingerprint: preview.preview_fingerprint,
        confirm: true,
      })
      setMessage(
        `Batch review saved ${result.saved} human dispositions as ${result.new_disposition.replace(/_/g, ' ')}. No merge occurred.`,
      )
      setPreview(null)
      setChecked({})
      setSelectFiltered(false)
      await loadList(offset)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not confirm batch review.')
    } finally {
      setBusy(false)
    }
  }

  async function loadPlans(nextOffset = planOffset, nextState = planState) {
    setBusy(true)
    setError('')
    try {
      const result = await fetchMergePlans({
        state: nextState,
        q: planQuery,
        decision_type: planDecisionType,
        same_client: planSameClient,
        offset: nextOffset,
        limit,
      })
      setPlanPage({
        total: result.total,
        plans: result.plans,
        summary: result.summary || EMPTY_PLAN_SUMMARY,
      })
      setPlanOffset(nextOffset)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load merge plans.')
    } finally {
      setBusy(false)
    }
  }

  async function openPlan(row: MergePlanListRow) {
    setBusy(true)
    setError('')
    try {
      const detail = await fetchMergePlan(row.company_a_id, row.company_b_id)
      setSelectedPlan(detail)
      const next: Record<string, string> = {}
      ;(detail.exceptions || []).forEach((item) => {
        next[item.exception_key] = ''
      })
      setDecisionChoices(next)
      setFocusedExceptionKey(detail.next_unresolved_exception?.exception_key || detail.exceptions?.[0]?.exception_key || '')
      setReadyNextPair(null)
      setMessage('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load merge plan.')
    } finally {
      setBusy(false)
    }
  }

  async function runPreparePlans() {
    setBusy(true)
    setError('')
    try {
      const result = await prepareMergePlans()
      setMessage(
        `Analyzed ${result.analyzed}. Ready for Review: ${result.ready_for_review || 0}. Needs Decisions: ${result.needs_exception_decision || 0}. Not Safe: ${result.not_safe_to_plan || 0}. Stale: ${result.stale || 0}. ${result.plan_only_warning || 'PLANNING ONLY — NO MERGE WILL OCCUR'}`,
      )
      await loadPlans(0, planState)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not prepare merge plans.')
    } finally {
      setBusy(false)
    }
  }

  async function applyPlanResult(saved: {
    plan: MergePlanDetail
    ready_for_review?: boolean
    next_unresolved_exception?: { exception_key: string } | null
    next_needs_decision_pair?: { company_a_id: number; company_b_id: number } | null
    remaining_exception_count?: number
  }, prefix: string) {
    setSelectedPlan(saved.plan)
    const remaining = saved.plan.exceptions || []
    setFocusedExceptionKey(saved.next_unresolved_exception?.exception_key || remaining[0]?.exception_key || '')
    if (saved.ready_for_review || saved.plan.plan_state === 'READY_FOR_HUMAN_APPROVAL') {
      setMessage(`${prefix} READY FOR REVIEW. ${saved.plan.plan_only_warning} No merge approval was created.`)
      const next = saved.next_needs_decision_pair
      setReadyNextPair(
        next
          ? {
              pair_key: `${next.company_a_id}:${next.company_b_id}`,
              company_a_id: next.company_a_id,
              company_b_id: next.company_b_id,
              plan_state: 'NEEDS_EXCEPTION_DECISION',
            }
          : planPage.plans.find((row) => row.pair_key !== saved.plan.pair_key && row.plan_state === 'NEEDS_EXCEPTION_DECISION') || null,
      )
    } else {
      setMessage(`${prefix} ${saved.plan.plan_only_warning} Remaining exceptions: ${remaining.length}.`)
      setReadyNextPair(null)
    }
    await loadPlans(planOffset, planState)
  }

  async function saveDecision(row: MergePlanListRow, exceptionKey: string, resolution?: string) {
    const chosen = resolution || decisionChoices[exceptionKey]
    if (!chosen) {
      setError('Choose a resolution before saving the plan decision.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const saved = await saveMergePlanDecision(row.company_a_id, row.company_b_id, {
        exception_key: exceptionKey,
        chosen_resolution: chosen,
        reason: decisionReason,
      })
      await applyPlanResult(saved, 'Plan decision saved.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save plan decision.')
    } finally {
      setBusy(false)
    }
  }

  async function saveDisposition(
    row: MergePlanListRow,
    disposition: 'MULTI_LOCATION' | 'NOT_DUPLICATE' | 'NEEDS_RESEARCH',
    reason: string,
  ) {
    setBusy(true)
    setError('')
    try {
      const saved = await saveWorkbenchDisposition(row.company_a_id, row.company_b_id, {
        disposition,
        reason,
        confirm: true,
      })
      setSelectedPlan(saved.plan)
      setMessage(
        `Human ${disposition.replace(/_/g, ' ')} saved. Pair left merge-planning eligibility. ${saved.plan.plan_only_warning}`,
      )
      const next = saved.next_needs_decision_pair
      setReadyNextPair(
        next
          ? {
              pair_key: `${next.company_a_id}:${next.company_b_id}`,
              company_a_id: next.company_a_id,
              company_b_id: next.company_b_id,
              plan_state: 'NEEDS_EXCEPTION_DECISION',
            }
          : null,
      )
      await loadPlans(planOffset, planState)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save human disposition.')
    } finally {
      setBusy(false)
    }
  }

  async function runReplan() {
    if (!selectedPlan) return
    setBusy(true)
    setError('')
    try {
      const result = await replanMergePlan(selectedPlan.company_a_id, selectedPlan.company_b_id)
      setSelectedPlan(result.plan)
      setMessage(`Plan recalculated. ${result.plan.plan_only_warning}`)
      await loadPlans(planOffset, planState)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not replan.')
    } finally {
      setBusy(false)
    }
  }

  const summary = page.summary
  const human = selected?.review
  const assessment = selected?.automated_assessment

  return (
    <section className="dup-review" aria-labelledby="dup-review-heading">
      <h2 id="dup-review-heading">Duplicate Review</h2>
      <p className="dup-plan-banner" role="status">
        REVIEW / PLAN ONLY. NO MERGE WILL OCCUR.
      </p>
      <p className="queue-sub">
        NorthStar Automated Assessment sorts candidates with explainable evidence. Julie&apos;s human
        disposition remains authoritative. This is not an external AI model.
      </p>
      <div className="dup-toolbar" role="tablist" aria-label="Duplicate workspace">
        <button type="button" aria-selected={workspace === 'review'} onClick={() => setWorkspace('review')}>
          Duplicate Review
        </button>
        <button
          type="button"
          aria-selected={workspace === 'planning'}
          onClick={() => setWorkspace('planning')}
        >
          Merge Planning
        </button>
      </div>
      {workspace === 'planning' ? (
        <MergePlanningPanel
          busy={busy}
          error={error}
          message={message}
          planState={planState}
          planQuery={planQuery}
          planDecisionType={planDecisionType}
          planSameClient={planSameClient}
          planOffset={planOffset}
          planPage={planPage}
          selectedPlan={selectedPlan}
          focusedExceptionKey={focusedExceptionKey}
          decisionChoices={decisionChoices}
          decisionReason={decisionReason}
          readyNextPair={readyNextPair}
          limit={limit}
          onPlanState={setPlanState}
          onPlanQuery={setPlanQuery}
          onDecisionType={setPlanDecisionType}
          onSameClient={setPlanSameClient}
          onLoad={(offset, state) => void loadPlans(offset, state)}
          onPrepare={() => void runPreparePlans()}
          onOpen={(row) => void openPlan(row)}
          onClose={() => {
            setSelectedPlan(null)
            setFocusedExceptionKey('')
            setReadyNextPair(null)
          }}
          onChoice={(key, value) => setDecisionChoices((current) => ({ ...current, [key]: value }))}
          onReason={setDecisionReason}
          onSaveDecision={(row, key, resolution) => void saveDecision(row, key, resolution)}
          onSaveDisposition={(row, disposition, reason) => void saveDisposition(row, disposition, reason)}
          onReplan={() => void runReplan()}
          onNextPair={() => readyNextPair && void openPlan(readyNextPair)}
        />
      ) : null}
      {workspace === 'review' ? (
      <>
      <div className="dup-cards" role="group" aria-label="Classification summary">
        <button type="button" className="dup-card" onClick={() => setQueue('high_confidence')}>
          High Confidence Duplicate
          <strong>{summary.HIGH_CONFIDENCE_DUPLICATE || 0}</strong>
        </button>
        <button type="button" className="dup-card" onClick={() => setQueue('likely_duplicate_auto')}>
          Likely Duplicate
          <strong>{summary.LIKELY_DUPLICATE || 0}</strong>
        </button>
        <button type="button" className="dup-card" onClick={() => setQueue('likely_multi_location')}>
          Likely Multi-Location
          <strong>{summary.LIKELY_MULTI_LOCATION || 0}</strong>
        </button>
        <button type="button" className="dup-card" onClick={() => setQueue('likely_not_duplicate')}>
          Likely Not Duplicate
          <strong>{summary.LIKELY_NOT_DUPLICATE || 0}</strong>
        </button>
        <button type="button" className="dup-card" onClick={() => setQueue('human_review_required')}>
          Human Review Required
          <strong>{summary.HUMAN_REVIEW_REQUIRED || 0}</strong>
        </button>
        <button type="button" className="dup-card" onClick={() => setQueue('insufficient_evidence')}>
          Insufficient Evidence
          <strong>{summary.INSUFFICIENT_EVIDENCE || 0}</strong>
        </button>
        <button type="button" className="dup-card dup-card-primary" onClick={() => setQueue('review_needed')}>
          Review Needed
          <strong>{summary.review_needed || 0}</strong>
        </button>
      </div>
      <div className="dup-toolbar">
        <button type="button" onClick={() => void analyze()} disabled={busy}>
          Analyze Duplicate Candidates
        </button>
      </div>
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
          Queue
          <select value={queue} onChange={(event) => setQueue(event.target.value)}>
            {QUEUES.map((row) => (
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
      <div className="dup-toolbar">
        <button
          type="button"
          onClick={() => {
            const next: Record<string, boolean> = {}
            page.pairs.forEach((row) => {
              next[row.pair_key] = true
            })
            setChecked(next)
            setSelectFiltered(false)
          }}
        >
          Select Current Page
        </button>
        <label className="dup-radio">
          <input
            type="checkbox"
            checked={selectFiltered}
            onChange={(event) => setSelectFiltered(event.target.checked)}
          />
          Select All Filtered
        </label>
        <label>
          Batch action
          <select value={batchAction} onChange={(event) => setBatchAction(event.target.value)}>
            {BATCH_ACTIONS.map((row) => (
              <option key={row.id} value={row.id}>
                {row.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Batch reason
          <input value={batchReason} onChange={(event) => setBatchReason(event.target.value)} />
        </label>
        <button type="button" onClick={() => void runPreview()} disabled={busy}>
          Preview batch review
        </button>
      </div>
      {preview ? (
        <div className="dup-preview" role="status">
          <p>
            {preview.selected} selected · {preview.eligible} eligible · {preview.excluded} require
            individual review because evidence became stale or unclassified.
          </p>
          <p>No silent partial mutation. Confirm applies only the eligible set shown here.</p>
          <button type="button" onClick={() => void runConfirm()} disabled={busy || !preview.confirm_allowed}>
            Confirm batch review
          </button>
        </div>
      ) : null}
      <table className="dup-table">
        <thead>
          <tr>
            <th>Select</th>
            <th>Company A</th>
            <th>Company B</th>
            <th>City / state</th>
            <th>Assessment</th>
            <th>Confidence</th>
            <th>Proposed survivor</th>
            <th>Human disposition</th>
          </tr>
        </thead>
        <tbody>
          {page.pairs.map((row) => (
            <tr key={row.pair_key}>
              <td>
                <input
                  type="checkbox"
                  aria-label={`Select ${row.pair_key}`}
                  checked={Boolean(checked[row.pair_key]) || selectFiltered}
                  onChange={(event) =>
                    setChecked((current) => ({ ...current, [row.pair_key]: event.target.checked }))
                  }
                />
              </td>
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
              <td>{classLabel(row.classification)}</td>
              <td>{row.confidence || '—'}</td>
              <td>
                {row.survivor_decision_required
                  ? 'NONE · SURVIVOR DECISION REQUIRED'
                  : row.auto_proposed_survivor_company_id || '—'}
              </td>
              <td>
                {row.disposition}
                {row.stale ? ' · RE-REVIEW NEEDED' : ''}
                {row.disagreement ? ' · DISAGREES' : ''}
              </td>
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
          {assessment?.disagreement ? (
            <p className="dup-conflict" role="status">
              HUMAN DECISION DIFFERS FROM AUTOMATED ASSESSMENT. Julie&apos;s disposition remains
              authoritative.
            </p>
          ) : null}
          <AssessmentPanel
            assessment={assessment}
            companyA={selected.company_a}
            companyB={selected.company_b}
          />
          <section className="dup-human" aria-label="Human review">
            <h3>Human Review</h3>
            <p>
              Julie disposition:{' '}
              {human?.disposition === 'UNREVIEWED' || !human?.disposition
                ? 'Unreviewed'
                : classLabel(human.disposition)}
            </p>
          </section>
          <p>{selected.evidence_summary}</p>
          <p>
            Dependency: {selected.dependency.overall}
            {selected.same_client_conflicts.length ? ' · SAME-CLIENT CCR CONFLICT' : ''}
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
                Proposed source: {sourceId || '—'} · Automated survivor is a proposal only. Julie
                selects the survivor for merge planning.
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
          <p className="queue-sub">There is no Merge, Execute, or batch Merge Candidate action in DS-12.</p>
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
      </>
      ) : null}
    </section>
  )
}
