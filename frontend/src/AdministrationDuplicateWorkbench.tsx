import { useMemo, useState } from 'react'
import type {
  MergePlanDetail,
  MergePlanException,
  MergePlanListRow,
  MergePlanSummary,
  WorkbenchIdentity,
} from './api/duplicateReview'

const PLAN_QUEUES: Array<{ id: string; label: string }> = [
  { id: 'NEEDS_EXCEPTION_DECISION', label: 'Needs Decisions' },
  { id: 'READY_FOR_HUMAN_APPROVAL', label: 'Ready for Review' },
  { id: 'NOT_SAFE_TO_PLAN', label: 'Not Safe' },
  { id: 'STALE', label: 'Stale' },
  { id: '', label: 'All plans' },
]

const DECISION_TYPES: Array<{ id: string; label: string }> = [
  { id: '', label: 'All decision types' },
  { id: 'survivor', label: 'Survivor' },
  { id: 'status', label: 'CCR Status' },
  { id: 'rn', label: 'RN' },
  { id: 'field', label: 'Phone/Field' },
  { id: 'contact', label: 'Contact' },
  { id: 'rep', label: 'Rep' },
  { id: 'hot', label: 'Hot' },
  { id: 'followup', label: 'Follow-Up' },
  { id: 'next_action', label: 'Next Action' },
  { id: 'campaign', label: 'Campaign' },
  { id: 'location', label: 'Location' },
  { id: 'other', label: 'Other' },
]

function FieldRow({ label, value, emphasize }: { label: string; value: unknown; emphasize?: boolean }) {
  return (
    <div className={`dup-field${emphasize ? ' dup-field-diff' : ''}`}>
      <span className="dup-field-label">{label}</span>
      <span>{value === null || value === undefined || value === '' ? '—' : String(value)}</span>
    </div>
  )
}

function IdentityBlock({
  identity,
  fallbackName,
  fallbackId,
}: {
  identity?: WorkbenchIdentity
  fallbackName?: string
  fallbackId: number
}) {
  const label = identity?.record_label || `${fallbackName || 'Company'} — Record ${fallbackId}`
  return (
    <div className="dup-identity">
      <strong>{label}</strong>
      <p>{identity?.city_state || [identity?.city, identity?.state].filter(Boolean).join(', ') || 'City/state unknown'}</p>
      {identity?.address ? <p>{identity.address}</p> : null}
      {identity?.phone ? <p>Phone {identity.phone}</p> : null}
      {identity?.website || identity?.domain ? <p>{identity.website || identity.domain}</p> : null}
      <p>RN {identity?.master_rn_label || 'None'}</p>
      <p>Client(s): {identity?.client_names || 'None'}</p>
      <p>
        CCR {identity?.active_ccr_count ?? 0} active / {identity?.removed_ccr_count ?? 0} removed · Contacts{' '}
        {identity?.contact_count ?? 0}
      </p>
    </div>
  )
}

function ExceptionChoices({
  exception,
  value,
  busy,
  onChoice,
  onSave,
}: {
  exception: MergePlanException
  value: string
  busy: boolean
  onChoice: (value: string) => void
  onSave: () => void
}) {
  const details = exception.details || {}
  const sourceContact = details.source_contact as Record<string, unknown> | undefined
  const survivorContact = details.survivor_contact as Record<string, unknown> | undefined
  const sourceCcr = details.source_ccr as Record<string, unknown> | undefined
  const survivorCcr = details.survivor_ccr as Record<string, unknown> | undefined
  return (
    <div className="dup-exception" data-exception-key={exception.exception_key}>
      <p className="dup-decision-needed">{exception.decision_needed || exception.label}</p>
      <p>{exception.why_you}</p>
      {exception.code ? <p className="queue-sub">Internal code: {exception.code}</p> : null}
      {exception.rn_prompt ? <p className="dup-rn-prompt">{exception.rn_prompt}</p> : null}
      {exception.rn_preservation ? <p>{exception.rn_preservation}</p> : null}
      {exception.code === 'FIELD_CONFLICT_REVIEW' ? (
        <div className="dup-grid">
          <p>
            Record A: {String(details.record_a_value || details.survivor || '—')}
          </p>
          <p>
            Record B: {String(details.record_b_value || details.source || '—')}
          </p>
        </div>
      ) : null}
      {sourceCcr || survivorCcr ? (
        <div className="dup-grid">
          <div>
            <h5>Record A / survivor CCR</h5>
            <FieldRow label="Status" value={survivorCcr?.status} />
            <FieldRow label="Rep" value={survivorCcr?.assigned_user_name} />
            <FieldRow label="Hot" value={survivorCcr?.hot ? 'Hot' : 'Not Hot'} />
            <FieldRow label="Follow-up" value={survivorCcr?.follow_up_date} />
            <FieldRow label="Next action" value={survivorCcr?.next_action} />
            <FieldRow label="RN" value={survivorCcr?.external_record_no || 'None'} />
          </div>
          <div>
            <h5>Record B / source CCR</h5>
            <FieldRow label="Status" value={sourceCcr?.status} />
            <FieldRow label="Rep" value={sourceCcr?.assigned_user_name} />
            <FieldRow label="Hot" value={sourceCcr?.hot ? 'Hot' : 'Not Hot'} />
            <FieldRow label="Follow-up" value={sourceCcr?.follow_up_date} />
            <FieldRow label="Next action" value={sourceCcr?.next_action} />
            <FieldRow label="RN" value={sourceCcr?.external_record_no || 'None'} />
          </div>
        </div>
      ) : null}
      {sourceContact || survivorContact ? (
        <div className="dup-grid">
          <div>
            <h5>{String(survivorContact?.name || 'Survivor contact')}</h5>
            <FieldRow label="Title" value={survivorContact?.title} />
            <FieldRow label="Email" value={survivorContact?.email} />
            <FieldRow label="Phone" value={survivorContact?.phone} />
            <FieldRow label="Extension" value={survivorContact?.phone_extension} />
            <FieldRow label="RN" value={survivorContact?.master_rn_label} />
          </div>
          <div>
            <h5>{String(sourceContact?.name || 'Source contact')}</h5>
            <FieldRow label="Title" value={sourceContact?.title} />
            <FieldRow label="Email" value={sourceContact?.email} />
            <FieldRow label="Phone" value={sourceContact?.phone} />
            <FieldRow label="Extension" value={sourceContact?.phone_extension} />
            <FieldRow label="RN" value={sourceContact?.master_rn_label} />
          </div>
        </div>
      ) : null}
      {(exception.choices || []).length ? (
        <>
          <label>
            Plan decision
            <select
              aria-label={`Decision for ${exception.exception_key}`}
              value={value}
              onChange={(event) => onChoice(event.target.value)}
            >
              <option value="">Select resolution</option>
              {(exception.choices || []).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          {(exception.choices || []).map((option) =>
            option.helper ? (
              <p key={`${option.value}-help`} className="queue-sub">
                {option.helper}
              </p>
            ) : null,
          )}
          <button type="button" onClick={onSave} disabled={busy}>
            Save decision
          </button>
        </>
      ) : (
        <p className="queue-sub">This exception cannot be resolved with a quick plan decision.</p>
      )}
    </div>
  )
}

function HumanDispositionForm({
  busy,
  title,
  onCancel,
  onSave,
}: {
  busy: boolean
  title: string
  onCancel: () => void
  onSave: (disposition: 'MULTI_LOCATION' | 'NOT_DUPLICATE' | 'NEEDS_RESEARCH', reason: string) => void
}) {
  const [kind, setKind] = useState<'MULTI_LOCATION' | 'NOT_DUPLICATE' | 'NEEDS_RESEARCH'>(
    title === 'NEEDS RESEARCH' ? 'NEEDS_RESEARCH' : 'MULTI_LOCATION',
  )
  const [reason, setReason] = useState('')
  const [confirm, setConfirm] = useState(false)
  return (
    <div className="dup-disposition">
      <h4>{title}</h4>
      {title !== 'NEEDS RESEARCH' ? (
        <fieldset>
          <legend>How are these separate?</legend>
          <label>
            <input
              type="radio"
              name="separate-kind"
              checked={kind === 'MULTI_LOCATION'}
              onChange={() => setKind('MULTI_LOCATION')}
            />
            Separate physical locations of same organization
          </label>
          <label>
            <input
              type="radio"
              name="separate-kind"
              checked={kind === 'NOT_DUPLICATE'}
              onChange={() => setKind('NOT_DUPLICATE')}
            />
            Different organizations / not duplicates
          </label>
        </fieldset>
      ) : null}
      <label>
        Reason
        <textarea value={reason} onChange={(event) => setReason(event.target.value)} required />
      </label>
      <label>
        <input type="checkbox" checked={confirm} onChange={(event) => setConfirm(event.target.checked)} /> I confirm this
        human identity decision
      </label>
      <button
        type="button"
        onClick={() => {
          if (!confirm) return
          onSave(title === 'NEEDS RESEARCH' ? 'NEEDS_RESEARCH' : kind, reason)
        }}
        disabled={busy || !confirm || reason.trim().length < 3}
      >
        Confirm human decision
      </button>
      <button type="button" onClick={onCancel} disabled={busy}>
        Cancel
      </button>
    </div>
  )
}

export default function MergePlanningPanel({
  busy,
  error,
  message,
  planState,
  planQuery,
  planDecisionType,
  planSameClient,
  planOffset,
  planPage,
  selectedPlan,
  focusedExceptionKey,
  decisionChoices,
  decisionReason,
  readyNextPair,
  limit,
  onPlanState,
  onPlanQuery,
  onDecisionType,
  onSameClient,
  onLoad,
  onPrepare,
  onOpen,
  onChoice,
  onReason,
  onSaveDecision,
  onSaveDisposition,
  onReplan,
  onNextPair,
}: {
  busy: boolean
  error: string
  message: string
  planState: string
  planQuery: string
  planDecisionType: string
  planSameClient: string
  planOffset: number
  planPage: { total: number; plans: MergePlanListRow[]; summary: MergePlanSummary }
  selectedPlan: MergePlanDetail | null
  focusedExceptionKey: string
  decisionChoices: Record<string, string>
  decisionReason: string
  readyNextPair: MergePlanListRow | null
  limit: number
  onPlanState: (value: string) => void
  onPlanQuery: (value: string) => void
  onDecisionType: (value: string) => void
  onSameClient: (value: string) => void
  onLoad: (offset: number, state: string) => void
  onPrepare: () => void
  onOpen: (row: MergePlanListRow) => void
  onChoice: (key: string, value: string) => void
  onReason: (value: string) => void
  onSaveDecision: (row: MergePlanListRow, key: string, resolution?: string) => void
  onSaveDisposition: (
    row: MergePlanListRow,
    disposition: 'MULTI_LOCATION' | 'NOT_DUPLICATE' | 'NEEDS_RESEARCH',
    reason: string,
  ) => void
  onReplan: () => void
  onNextPair: () => void
}) {
  const summary = planPage.summary
  const [hatch, setHatch] = useState<{ pairKey: string; mode: 'separate' | 'research' } | null>(null)
  const simpleCards = useMemo(
    () => planPage.plans.filter((row) => row.allow_quick_survivor),
    [planPage.plans],
  )

  function identityFor(row: MergePlanListRow, side: 'a' | 'b') {
    return side === 'a' ? row.company_a : row.company_b
  }

  return (
    <div className="dup-planning" aria-label="Merge Planning">
      <h3>Merge Planning</h3>
      <p className="dup-decision-needed">Duplicate Exception Workbench</p>
      <p className="dup-plan-banner" role="status">
        PLANNING ONLY — NO MERGE WILL OCCUR
      </p>
      <p className="queue-sub">
        Duplicate Review is MASTER DATA administration. The left-side Active Client selector does not scope these
        identity decisions. Ready for Review means planning exceptions are resolved — it is not merge approval and does
        not execute a merge.
      </p>
      <div className="dup-cards" role="group" aria-label="Merge planning summary">
        <button type="button" className="dup-card dup-card-primary" aria-label="Needs Decisions" onClick={() => onPlanState('NEEDS_EXCEPTION_DECISION')}>
          Needs Decisions
          <strong>{summary.needs_exception_decision || 0}</strong>
        </button>
        <button type="button" className="dup-card" aria-label="Simple Decisions" onClick={() => onPlanState('NEEDS_EXCEPTION_DECISION')}>
          Simple Decisions
          <strong>{summary.simple_decisions || 0}</strong>
        </button>
        <button type="button" className="dup-card" aria-label="Complex Decisions" onClick={() => onPlanState('NEEDS_EXCEPTION_DECISION')}>
          Complex Decisions
          <strong>{summary.complex_decisions || 0}</strong>
        </button>
        <button type="button" className="dup-card" aria-label="Ready for Review" onClick={() => onPlanState('READY_FOR_HUMAN_APPROVAL')}>
          Ready for Review
          <strong>{summary.ready_for_review || 0}</strong>
        </button>
        <button type="button" className="dup-card" aria-label="Not Safe" onClick={() => onPlanState('NOT_SAFE_TO_PLAN')}>
          Not Safe
          <strong>{summary.not_safe_to_plan || 0}</strong>
        </button>
        <button type="button" className="dup-card" aria-label="Stale" onClick={() => onPlanState('STALE')}>
          Stale
          <strong>{summary.stale || 0}</strong>
        </button>
      </div>
      <div className="dup-toolbar">
        <button type="button" onClick={onPrepare} disabled={busy}>
          Prepare Merge Plans
        </button>
      </div>
      <form
        className="dup-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          onLoad(0, planState)
        }}
      >
        <label>
          Search plans
          <input value={planQuery} onChange={(event) => onPlanQuery(event.target.value)} placeholder="Company, RN, city" />
        </label>
        <label>
          Plan queue
          <select value={planState} onChange={(event) => onPlanState(event.target.value)}>
            {PLAN_QUEUES.map((row) => (
              <option key={row.id || 'all'} value={row.id}>
                {row.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Decision type
          <select value={planDecisionType} onChange={(event) => onDecisionType(event.target.value)}>
            {DECISION_TYPES.map((row) => (
              <option key={row.id || 'all-types'} value={row.id}>
                {row.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Same-client CCR
          <select value={planSameClient} onChange={(event) => onSameClient(event.target.value)}>
            <option value="">All</option>
            <option value="yes">Yes</option>
            <option value="no">No</option>
          </select>
        </label>
        <button type="submit">Search</button>
      </form>
      {error ? <p className="data-status data-status--error">{error}</p> : null}
      {message ? <p className="data-status">{message}</p> : null}
      <p className="queue-sub">
        {planPage.total} merge plans · showing {planPage.plans.length} · {busy ? 'Loading…' : ''}
      </p>
      <div className="dup-workbench-queue">
        {planPage.plans.map((row, index) => {
          const hatchOpen = hatch?.pairKey === row.pair_key
          const aLabel = identityFor(row, 'a')?.record_label || `${row.company_a_name} — Record ${row.company_a_id}`
          const bLabel = identityFor(row, 'b')?.record_label || `${row.company_b_name} — Record ${row.company_b_id}`
          return (
            <article
              key={row.pair_key}
              className={`dup-workbench-card${row.simple_decision ? ' dup-workbench-simple' : ''}`}
              aria-label={`${aLabel} vs ${bLabel}`}
              tabIndex={row.allow_quick_survivor ? 0 : undefined}
              onKeyDown={(event) => {
                if (!row.allow_quick_survivor) return
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                  event.preventDefault()
                  const current = simpleCards.findIndex((item) => item.pair_key === row.pair_key)
                  const next = event.key === 'ArrowDown' ? current + 1 : current - 1
                  const target = simpleCards[next]
                  if (target) onOpen(target)
                }
              }}
            >
              <div className="dup-grid">
                <IdentityBlock identity={row.company_a} fallbackName={row.company_a_name} fallbackId={row.company_a_id} />
                <IdentityBlock identity={row.company_b} fallbackName={row.company_b_name} fallbackId={row.company_b_id} />
              </div>
              <p>
                <strong>Exact decision needed:</strong> {row.decision_needed || row.plan_state_label}
              </p>
              {(row.decision_needed_all || []).length > 1 ? (
                <ul>
                  {row.decision_needed_all?.map((label) => (
                    <li key={label}>{label}</li>
                  ))}
                </ul>
              ) : null}
              <p>
                {row.plan_state_label} · {row.exception_count || 0} exception{(row.exception_count || 0) === 1 ? '' : 's'} ·
                Same-client CCR {row.same_client_ccr_conflict ? 'Yes' : 'No'}
              </p>
              {row.workbench_mode === 'not_safe' ? (
                <>
                  <p className="dup-conflict">NOT SAFE TO PLAN</p>
                  <p>{row.not_safe_reason}</p>
                </>
              ) : null}
              {row.allow_quick_survivor ? (
                <div className="dup-simple-actions">
                  <button
                    type="button"
                    onClick={() => onSaveDecision(row, 'SURVIVOR_DECISION_REQUIRED', `SURVIVOR:${row.company_a_id}`)}
                    disabled={busy}
                  >
                    KEEP A AS SURVIVOR
                  </button>
                  <button
                    type="button"
                    onClick={() => onSaveDecision(row, 'SURVIVOR_DECISION_REQUIRED', `SURVIVOR:${row.company_b_id}`)}
                    disabled={busy}
                  >
                    KEEP B AS SURVIVOR
                  </button>
                  <button type="button" onClick={() => setHatch({ pairKey: row.pair_key, mode: 'separate' })} disabled={busy}>
                    THESE ARE SEPARATE RECORDS / LOCATIONS
                  </button>
                  <button type="button" onClick={() => setHatch({ pairKey: row.pair_key, mode: 'research' })} disabled={busy}>
                    NEEDS RESEARCH
                  </button>
                </div>
              ) : (
                <div className="dup-simple-actions">
                  <button type="button" onClick={() => onOpen(row)}>
                    {row.workbench_mode === 'not_safe' ? 'Open Not Safe review' : 'Open exception workbench'}
                  </button>
                  {row.workbench_mode === 'not_safe' ? (
                    <>
                      <button type="button" onClick={() => setHatch({ pairKey: row.pair_key, mode: 'separate' })}>
                        Mark as Multi-Location / Not Duplicate
                      </button>
                      <button type="button" onClick={() => setHatch({ pairKey: row.pair_key, mode: 'research' })}>
                        Needs Research
                      </button>
                    </>
                  ) : null}
                </div>
              )}
              {hatchOpen ? (
                <HumanDispositionForm
                  busy={busy}
                  title={hatch?.mode === 'research' ? 'NEEDS RESEARCH' : 'THESE ARE SEPARATE RECORDS / LOCATIONS'}
                  onCancel={() => setHatch(null)}
                  onSave={(disposition, reason) => {
                    onSaveDisposition(row, disposition, reason)
                    setHatch(null)
                  }}
                />
              ) : null}
              <p className="queue-sub">Card {index + 1}</p>
            </article>
          )
        })}
      </div>
      <div className="dup-pager">
        <button type="button" disabled={planOffset <= 0} onClick={() => onLoad(Math.max(0, planOffset - limit), planState)}>
          Previous
        </button>
        <button
          type="button"
          disabled={planOffset + limit >= planPage.total}
          onClick={() => onLoad(planOffset + limit, planState)}
        >
          Next
        </button>
      </div>
      {selectedPlan?.ready_for_review || selectedPlan?.plan_state === 'READY_FOR_HUMAN_APPROVAL' ? (
        <div className="dup-ready" role="status">
          <h3>READY FOR REVIEW</h3>
          <p>All planning exceptions are resolved. This is not merge approval and does not execute a merge.</p>
          {readyNextPair ? (
            <button type="button" onClick={onNextPair}>
              NEXT NEEDS-DECISION PAIR
            </button>
          ) : null}
        </div>
      ) : null}
      {selectedPlan ? (
        <article className="dup-detail" aria-label="Exception workbench">
          <h3>Exception Workbench</h3>
          <p className="dup-plan-banner">PLANNING ONLY — NO MERGE WILL OCCUR</p>
          <section>
            <h4>PAIR IDENTITY</h4>
            <div className="dup-grid">
              <IdentityBlock identity={selectedPlan.company_a} fallbackName={selectedPlan.company_a_name} fallbackId={selectedPlan.company_a_id} />
              <IdentityBlock identity={selectedPlan.company_b} fallbackName={selectedPlan.company_b_name} fallbackId={selectedPlan.company_b_id} />
            </div>
          </section>
          <section>
            <h4>NorthStar Automated Assessment</h4>
            <FieldRow label="Classification" value={selectedPlan.automated_assessment?.classification} />
            <FieldRow label="Confidence" value={selectedPlan.automated_assessment?.confidence} />
            <p>This is NorthStar&apos;s automated assessment, not Julie&apos;s decision.</p>
            <ul>
              {(((selectedPlan.automated_assessment?.why as string[] | undefined) || []) as string[]).map((row) => (
                <li key={row}>✓ {row}</li>
              ))}
            </ul>
            <ul>
              {(((selectedPlan.automated_assessment?.concern_labels as string[] | undefined) || []) as string[]).map((row) => (
                <li key={row}>⚠ {row}</li>
              ))}
            </ul>
          </section>
          <section>
            <h4>Human Decision</h4>
            <FieldRow label="Disposition" value={selectedPlan.human_review?.disposition || selectedPlan.human_disposition} />
            <FieldRow label="Survivor" value={selectedPlan.human_review?.proposed_survivor_company_id} />
            <FieldRow label="Source" value={selectedPlan.human_review?.proposed_source_company_id} />
            <FieldRow label="Actor" value={selectedPlan.human_review?.actor_name} />
            <FieldRow label="Timestamp" value={selectedPlan.human_review?.reviewed_at} />
            <FieldRow label="Reason" value={selectedPlan.human_review?.reason} />
            <p className="queue-sub">Human decision wins over automated assessment.</p>
          </section>
          <section>
            <h4>PROPOSED FUTURE MERGE</h4>
            <FieldRow label="Plan state" value={selectedPlan.plan_state_label || selectedPlan.plan_state} />
            <FieldRow label="Survivor" value={selectedPlan.survivor_company_id} />
            <FieldRow label="Source" value={selectedPlan.source_company_id} />
            <FieldRow label="Why survivor" value={selectedPlan.why_proposed_survivor || selectedPlan.survivor_source} />
          </section>
          {selectedPlan.survivor_comparison?.rows?.length ? (
            <section>
              <h4>Survivor comparison</h4>
              {(selectedPlan.survivor_comparison.rows || []).map((row) => (
                <div key={row.label} className={`dup-compare${row.different ? ' dup-field-diff' : ''}`}>
                  <strong>{row.label}</strong>
                  <span>A: {String(row.company_a)}</span>
                  <span>B: {String(row.company_b)}</span>
                </div>
              ))}
            </section>
          ) : null}
          {selectedPlan.plan_state === 'NOT_SAFE_TO_PLAN' ? (
            <section>
              <p className="dup-conflict">NOT SAFE TO PLAN</p>
              <p>{selectedPlan.not_safe_reason}</p>
              <button type="button" onClick={() => setHatch({ pairKey: selectedPlan.pair_key, mode: 'separate' })}>
                Mark as Multi-Location / Not Duplicate
              </button>
              <button type="button" onClick={() => setHatch({ pairKey: selectedPlan.pair_key, mode: 'research' })}>
                Needs Research
              </button>
            </section>
          ) : null}
          <section>
            <h4>UNRESOLVED DECISIONS</h4>
            {(selectedPlan.exceptions || []).length === 0 ? <p>No unresolved planning exceptions.</p> : null}
            {(selectedPlan.exceptions || []).map((row) => (
              <div key={row.exception_key} className={focusedExceptionKey === row.exception_key ? 'dup-exception-focus' : undefined}>
                <ExceptionChoices
                  exception={row}
                  value={decisionChoices[row.exception_key] || ''}
                  busy={busy}
                  onChoice={(value) => onChoice(row.exception_key, value)}
                  onSave={() => onSaveDecision(selectedPlan, row.exception_key)}
                />
              </div>
            ))}
          </section>
          <section>
            <h4>WHAT NORTHSTAR WILL PRESERVE</h4>
            <ul>
              {(selectedPlan.preservation?.lines || []).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            {(selectedPlan.preservation?.automatic_contacts || []).length ? (
              <>
                <h5>NorthStar will handle automatically in future merge</h5>
                <ul>
                  {(selectedPlan.preservation?.automatic_contacts || []).map((row, index) => (
                    <li key={index}>
                      {String(row.survivor_name || row.source_name || 'Exact duplicate contact')} — SAFE CONTACT
                      CONSOLIDATION
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </section>
          <section>
            <h4>Decision history</h4>
            {(selectedPlan.decision_history || []).length === 0 ? <p>No plan decisions yet.</p> : null}
            {(selectedPlan.decision_history || []).map((row, index) => (
              <p key={index}>
                {String(row.created_at || '')} · actor {String(row.actor_user_id || '')} · {String(row.exception_key)} ·{' '}
                {String(row.chosen_resolution)}
                {row.superseded_at ? ` · superseded ${String(row.superseded_at)}` : ''}
                {row.reason ? ` · ${String(row.reason)}` : ''}
              </p>
            ))}
          </section>
          <label>
            Decision reason
            <textarea value={decisionReason} onChange={(event) => onReason(event.target.value)} />
          </label>
          <button type="button" onClick={onReplan} disabled={busy}>
            Replan
          </button>
          <p className="queue-sub">
            These are plan decisions only. They are not applied to live companies, CCRs, or contacts. There is no Execute
            Merge or Merge Now action.
          </p>
        </article>
      ) : null}
    </div>
  )
}
