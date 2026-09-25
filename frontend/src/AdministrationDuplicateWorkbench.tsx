import { useEffect, useMemo, useRef, useState } from 'react'
import type {
  MergePlanDetail,
  MergePlanException,
  MergePlanListRow,
  MergePlanSummary,
  PreservationPreview,
  WorkbenchIdentity,
  WorkbenchRelationship,
} from './api/duplicateReview'

function stopCardOpen(event: React.SyntheticEvent) {
  event.stopPropagation()
}

function relationshipDisplay(row: WorkbenchRelationship) {
  const client = row.client_name || row.client_code || (row.client_id ? `Client ${row.client_id}` : 'Client')
  return row.display || `${client} — ${row.status_label || row.status || 'No status'}`
}

function assignedDisplay(row: WorkbenchRelationship) {
  return `Assigned: ${row.assigned_user_name || 'Unassigned'}`
}

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

function previewCell(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function recordPhrase(ids: unknown, fallbackId?: number) {
  const list = Array.isArray(ids) ? ids.filter((id) => id !== null && id !== undefined && id !== '') : []
  if (!list.length && fallbackId) return `Record ${fallbackId}`
  return list.map((id) => `Record ${id}`).join(' + ')
}

function namedItems(rows: unknown, keys: string[]) {
  if (!Array.isArray(rows) || rows.length === 0) return 'None'
  return rows
    .map((row) => {
      if (!row || typeof row !== 'object') return String(row)
      const rec = row as Record<string, unknown>
      return keys.map((key) => rec[key]).filter((value) => value !== null && value !== undefined && value !== '').join(' ') || previewCell(rec.label)
    })
    .join('; ')
}

function PreservationPanel({
  preservation,
  companyAId,
  companyBId,
}: {
  preservation?: PreservationPreview
  companyAId: number
  companyBId: number
}) {
  const table = preservation?.table || []
  const warnings = preservation?.warnings || []
  const leadmaster = preservation?.leadmaster || {}
  const contacts = preservation?.contacts || {}
  const ccrs = preservation?.ccrs || {}
  const notes = preservation?.notes || {}
  const activities = preservation?.activities || {}
  const campaigns = preservation?.campaigns || {}
  const aliases = preservation?.aliases || {}
  const locations = preservation?.locations || {}
  return (
    <section className="dup-preview">
      <h4>WHAT NORTHSTAR WILL PRESERVE</h4>
      <p className="queue-sub">
        If this plan were eventually approved and executed, this is the data that would exist afterward.
        Planning preview only — no merge will occur.
      </p>
      {warnings.length ? (
        <div className="dup-preservation-warning" role="alert">
          <p className="dup-conflict">PRESERVATION WARNING</p>
          {warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
          <p>This pair cannot become Ready for Review until preservation warnings are resolved.</p>
        </div>
      ) : null}
      <table className="dup-preserve-table">
        <thead>
          <tr>
            <th>DATA TYPE</th>
            <th>BEFORE</th>
            <th>PLANNED ACTION</th>
            <th>EXPECTED AFTER</th>
          </tr>
        </thead>
        <tbody>
          {table.map((row) => (
            <tr key={row.data_type}>
              <th scope="row">{row.data_type}</th>
              <td>{previewCell(row.before)}</td>
              <td>{previewCell(row.planned_action)}</td>
              <td>{previewCell(row.expected_after)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h5>{String(leadmaster.title || 'LEADMASTER / SOURCE IDENTITIES')}</h5>
      <p>
        Record {previewCell(leadmaster.record_a?.company_id || companyAId)} Primary RN:{' '}
        {previewCell(leadmaster.record_a?.primary_rn)}
      </p>
      <p>
        Record {previewCell(leadmaster.record_b?.company_id || companyBId)} Primary RN:{' '}
        {previewCell(leadmaster.record_b?.primary_rn)}
      </p>
      <p>Future merge plan: {previewCell(leadmaster.future_plan)}</p>
      <p>
        Planned result: {previewCell(leadmaster.planned_result_count)} traceable LeadMaster identities
        {(leadmaster.planned_rns || []).length ? `: ${(leadmaster.planned_rns || []).join(', ')}` : ''}
      </p>
      {(leadmaster.identities || []).map((ident, index) => (
        <p key={`${String(ident.source_system || '')}-${String(ident.source_record_no || index)}`}>
          {previewCell(ident.source_system)} {previewCell(ident.source_record_no)}
          {ident.label ? ` · ${String(ident.label)}` : ''}
          {ident.treatment ? ` — ${String(ident.treatment)}` : ''}
        </p>
      ))}
      <h5>{String(contacts.title || 'CONTACTS')}</h5>
      <p>
        Before: Record {companyAId}: {previewCell(contacts.before?.record_a)} · Record {companyBId}:{' '}
        {previewCell(contacts.before?.record_b)} · Total source rows: {previewCell(contacts.before?.total_source_rows)}
      </p>
      <p>
        Planned: {previewCell(contacts.planned?.exact_consolidations)} exact contact consolidation ·{' '}
        {previewCell(contacts.planned?.unique_preserved)} unique preserved
        {Number(contacts.planned?.decision_required || 0) ? ' · contact decision required' : ''}
      </p>
      {(contacts.rows || []).map((row, index) => (
        <div key={index} className="dup-preserve-item">
          <strong>{previewCell(row.name || row.source_name || row.survivor_name)}</strong>
          <p>
            {recordPhrase(row.record_ids, typeof row.company_id === 'number' ? row.company_id : undefined)}
            {row.title ? ` · ${String(row.title)}` : ''}
            {row.phone ? ` · ${String(row.phone)}${row.phone_extension ? ` x${String(row.phone_extension)}` : ''}` : ''}
          </p>
          <p>→ {previewCell(row.treatment)}</p>
          {String(row.treatment || '').startsWith('EXACT') ? <p>→ 1 resulting contact</p> : null}
        </div>
      ))}
      <p>
        Expected future result:{' '}
        {contacts.expected_after === 'Pending decision' ? 'Pending contact decision' : previewCell(contacts.expected_after)}
      </p>
      <h5>{String(ccrs.title || 'CLIENT RELATIONSHIPS')}</h5>
      <p>
        Record {companyAId}: {namedItems(ccrs.record_a, ['display', 'assigned_user_name'])}
      </p>
      <p>
        Record {companyBId}: {namedItems(ccrs.record_b, ['display', 'assigned_user_name'])}
      </p>
      {(ccrs.rows || []).map((row, index) => (
        <div key={String(row.client_id || index)} className="dup-preserve-item">
          <strong>{previewCell(row.client_name || row.client_code)}</strong>
          {row.fields && typeof row.fields === 'object'
            ? Object.entries(row.fields as Record<string, Record<string, unknown>>).map(([field, values]) => (
                <p key={field}>
                  {field}: A/source {previewCell(values.source)} · B/survivor {previewCell(values.survivor)} ·{' '}
                  {previewCell(values.action)}
                </p>
              ))
            : null}
          <p>PLANNED RESULT: {previewCell(row.planned_result)}</p>
        </div>
      ))}
      <p>Expected future result: {previewCell(ccrs.expected_after)}</p>
      <h5>{String(notes.title || 'NOTES')}</h5>
      <p>
        Record {companyAId}: {previewCell(notes.record_a)} · Record {companyBId}: {previewCell(notes.record_b)}
      </p>
      <p>Exact/normalized duplicates to collapse: {previewCell(notes.exact_normalized_duplicates)}</p>
      <p>Unique notes to preserve: {previewCell(notes.unique_to_preserve)}</p>
      <p>Expected future result: {previewCell(notes.expected_after)}</p>
      <h5>{String(activities.title || 'ACTIVITIES / HISTORY')}</h5>
      <p>
        Record {companyAId} activities/history: {previewCell(activities.record_a)} · Record {companyBId}{' '}
        activities/history: {previewCell(activities.record_b)}
      </p>
      <p>Planned preservation: {previewCell(activities.planned_action)}</p>
      <p>Expected future result: {previewCell(activities.expected_after)}</p>
      {activities.warning ? <p className="dup-conflict">{previewCell(activities.warning)}</p> : null}
      <h5>{String(campaigns.title || 'CAMPAIGNS')}</h5>
      <p>Record {companyAId}: {namedItems(campaigns.record_a, ['campaign_name', 'campaign_id'])}</p>
      <p>Record {companyBId}: {namedItems(campaigns.record_b, ['campaign_name', 'campaign_id'])}</p>
      <p>{previewCell(campaigns.planned_action)}</p>
      <p>Expected future result: {previewCell(campaigns.expected_after)}</p>
      <h5>{String(aliases.title || 'ALIASES')}</h5>
      <p>Record {companyAId}: {namedItems(aliases.record_a, ['label', 'alias_name'])}</p>
      <p>Record {companyBId}: {namedItems(aliases.record_b, ['label', 'alias_name'])}</p>
      {aliases.source_canonical_as_alias ? (
        <p>Source canonical name preserved as alias: {previewCell(aliases.source_canonical_name)}</p>
      ) : null}
      <p>{previewCell(aliases.planned_action)}</p>
      <p>Expected future result: {previewCell(aliases.expected_after)}</p>
      <h5>{String(locations.title || 'LOCATIONS')}</h5>
      <p>Record {companyAId}: {namedItems(locations.record_a, ['label', 'location_name', 'city', 'state'])}</p>
      <p>Record {companyBId}: {namedItems(locations.record_b, ['label', 'location_name', 'city', 'state'])}</p>
      <p>{previewCell(locations.planned_action)}</p>
      <p>Expected future result: {previewCell(locations.expected_after)}</p>
    </section>
  )
}

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
  detailed,
}: {
  identity?: WorkbenchIdentity
  fallbackName?: string
  fallbackId: number
  detailed?: boolean
}) {
  const label = identity?.record_label || `${fallbackName || 'Company'} — Record ${fallbackId}`
  const activeRels = (identity?.active_relationships || []).filter((row) => row.active !== false)
  const allRels = detailed ? identity?.relationships || activeRels : activeRels
  const clientNames =
    activeRels
      .map((row) => row.client_name || row.client_code)
      .filter(Boolean)
      .join(', ') ||
    identity?.client_names ||
    'None'
  return (
    <div className="dup-identity">
      <strong>{label}</strong>
      {detailed ? <FieldRow label="Company ID" value={identity?.company_id || fallbackId} /> : null}
      {detailed ? <FieldRow label="Company name" value={identity?.company_name || fallbackName} /> : null}
      <p>
        {identity?.city_state_zip ||
          identity?.city_state ||
          [identity?.city, identity?.state, identity?.zip].filter(Boolean).join(', ') ||
          'City/state unknown'}
      </p>
      {identity?.address ? <p>{identity.address}</p> : null}
      {identity?.phone ? <p>Phone {identity.phone}{identity.phone_extension ? ` x${identity.phone_extension}` : ''}</p> : null}
      {identity?.website || identity?.domain ? <p>{identity.website || identity.domain}</p> : null}
      <p>RN {identity?.master_rn_label || 'None'}</p>
      <p>Client(s): {clientNames}</p>
      {allRels.map((row, index) => (
        <div key={`${row.ccr_id || row.client_id || index}`} className="dup-relationship">
          <p>{relationshipDisplay(row)}</p>
          <p>{assignedDisplay(row)}</p>
          {detailed ? (
            <>
              <FieldRow label="Hot" value={row.hot ? 'Hot' : 'Not Hot'} />
              <FieldRow label="Follow-up" value={row.follow_up_date} />
              <FieldRow label="Next action" value={row.next_action} />
              <FieldRow label="CCR RN" value={row.external_record_no || 'None'} />
              <FieldRow
                label="Campaigns"
                value={
                  (row.campaigns || []).map((camp) => camp.campaign_name).filter(Boolean).join(', ') || 'None'
                }
              />
              <FieldRow label="Relationship" value={row.state_label || (row.active === false ? 'Removed' : 'Active')} />
            </>
          ) : null}
        </div>
      ))}
      <p>
        CCR {identity?.active_ccr_count ?? 0} active / {identity?.removed_ccr_count ?? 0} removed · Contacts:{' '}
        {identity?.contact_count ?? identity?.contacts?.length ?? 0}
      </p>
      {detailed ? (
        <>
          <p>
            Counts: contacts {identity?.contact_count ?? 0} · notes {identity?.notes_count ?? 0} · activities/history{' '}
            {identity?.activities_count ?? 0} · aliases {identity?.alias_count ?? 0} · locations{' '}
            {identity?.location_count ?? 0} · campaigns {identity?.campaign_count ?? 0}
          </p>
          <FieldRow
            label="Source identities"
            value={
              (identity?.identities || []).map((item) => item.label).filter(Boolean).join(', ') ||
              identity?.identity_summary ||
              'None'
            }
          />
          <FieldRow
            label="Aliases"
            value={(identity?.aliases || []).map((item) => item.label || item.alias_name).filter(Boolean).join(', ') || 'None'}
          />
          <FieldRow
            label="Locations"
            value={
              (identity?.locations || [])
                .map((item) => item.label || item.location_name || item.address)
                .filter(Boolean)
                .join(', ') || 'None'
            }
          />
          {(identity?.contacts || []).length ? (
            <div>
              <p>
                <strong>Contacts</strong>
              </p>
              {(identity?.contacts || []).map((contact) => (
                <p key={contact.id || contact.name}>
                  {contact.name}
                  {contact.title ? ` · ${contact.title}` : ''}
                  {contact.email ? ` · ${contact.email}` : ''}
                  {contact.phone ? ` · ${contact.phone}` : ''}
                  {contact.phone_extension ? ` x${contact.phone_extension}` : ''}
                  {contact.master_rn_label ? ` · RN ${contact.master_rn_label}` : ''}
                  {contact.source ? ` · ${contact.source}` : ''}
                </p>
              ))}
            </div>
          ) : (
            <p>Contacts: none</p>
          )}
        </>
      ) : null}
    </div>
  )
}

function ExceptionChoices({
  exception,
  value,
  busy,
  recordAId,
  recordBId,
  survivorId,
  sourceId,
  onChoice,
  onSave,
}: {
  exception: MergePlanException
  value: string
  busy: boolean
  recordAId?: number
  recordBId?: number
  survivorId?: number | null
  sourceId?: number | null
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
            Record {recordAId ?? 'A'}: {String(details.record_a_value || details.survivor || '—')}
          </p>
          <p>
            Record {recordBId ?? 'B'}: {String(details.record_b_value || details.source || '—')}
          </p>
        </div>
      ) : null}
      {sourceCcr || survivorCcr ? (
        <div className="dup-grid">
          <div>
            <h5>Record {survivorId ?? recordAId ?? 'survivor'} CCR</h5>
            <FieldRow label="Status" value={survivorCcr?.status} />
            <FieldRow label="Rep" value={survivorCcr?.assigned_user_name} />
            <FieldRow label="Hot" value={survivorCcr?.hot ? 'Hot' : 'Not Hot'} />
            <FieldRow label="Follow-up" value={survivorCcr?.follow_up_date} />
            <FieldRow label="Next action" value={survivorCcr?.next_action} />
            <FieldRow label="RN" value={survivorCcr?.external_record_no || 'None'} />
          </div>
          <div>
            <h5>Record {sourceId ?? recordBId ?? 'source'} CCR</h5>
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
    <div className="dup-disposition" onClick={stopCardOpen} onKeyDown={stopCardOpen}>
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
  onClose,
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
  onClose: () => void
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
  const queueRef = useRef<HTMLDivElement>(null)
  const queueScrollRef = useRef(0)
  const detailRef = useRef<HTMLElement>(null)
  const simpleCards = useMemo(
    () => planPage.plans.filter((row) => row.allow_quick_survivor),
    [planPage.plans],
  )
  const selectedKey = selectedPlan?.pair_key || ''

  useEffect(() => {
    if (selectedKey && typeof detailRef.current?.scrollIntoView === 'function') {
      detailRef.current.scrollIntoView({ block: 'nearest' })
    } else if (!selectedKey && queueRef.current) {
      queueRef.current.scrollTop = queueScrollRef.current
    }
  }, [selectedKey])

  function identityFor(row: MergePlanListRow, side: 'a' | 'b') {
    return side === 'a' ? row.company_a : row.company_b
  }

  function openDetails(row: MergePlanListRow) {
    queueScrollRef.current = queueRef.current?.scrollTop || 0
    onOpen(row)
  }

  function closeDetails() {
    onClose()
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
      <div className="dup-workbench-queue" ref={queueRef}>
        {planPage.plans.map((row, index) => {
          const hatchOpen = hatch?.pairKey === row.pair_key
          const aLabel = identityFor(row, 'a')?.record_label || `${row.company_a_name} — Record ${row.company_a_id}`
          const bLabel = identityFor(row, 'b')?.record_label || `${row.company_b_name} — Record ${row.company_b_id}`
          const open = selectedPlan?.pair_key === row.pair_key
          return (
            <article
              key={row.pair_key}
              className={`dup-workbench-card${row.simple_decision ? ' dup-workbench-simple' : ''}${open ? ' dup-workbench-card-open' : ''}`}
              aria-label={`${aLabel} vs ${bLabel}`}
              tabIndex={0}
              onClick={() => openDetails(row)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  openDetails(row)
                  return
                }
                if (!row.allow_quick_survivor) return
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                  event.preventDefault()
                  const current = simpleCards.findIndex((item) => item.pair_key === row.pair_key)
                  const next = event.key === 'ArrowDown' ? current + 1 : current - 1
                  const target = simpleCards[next]
                  if (target) openDetails(target)
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
              <div className="dup-simple-actions" onClick={stopCardOpen} onKeyDown={stopCardOpen}>
                <button type="button" onClick={() => openDetails(row)} disabled={busy}>
                  REVIEW DETAILS
                </button>
                {row.allow_quick_survivor ? (
                  <>
                    <button
                      type="button"
                      onClick={() => onSaveDecision(row, 'SURVIVOR_DECISION_REQUIRED', `SURVIVOR:${row.company_a_id}`)}
                      disabled={busy}
                    >
                      KEEP RECORD {row.company_a_id}
                    </button>
                    <button
                      type="button"
                      onClick={() => onSaveDecision(row, 'SURVIVOR_DECISION_REQUIRED', `SURVIVOR:${row.company_b_id}`)}
                      disabled={busy}
                    >
                      KEEP RECORD {row.company_b_id}
                    </button>
                    <button type="button" onClick={() => setHatch({ pairKey: row.pair_key, mode: 'separate' })} disabled={busy}>
                      THESE ARE SEPARATE RECORDS / LOCATIONS
                    </button>
                    <button type="button" onClick={() => setHatch({ pairKey: row.pair_key, mode: 'research' })} disabled={busy}>
                      NEEDS RESEARCH
                    </button>
                  </>
                ) : null}
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
      {selectedPlan?.ready_for_review ? (
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
        <article className="dup-detail" aria-label="Exception workbench" ref={detailRef}>
          <h3>Exception Workbench</h3>
          <p className="dup-plan-banner">PLANNING ONLY — NO MERGE WILL OCCUR</p>
          <div className="dup-simple-actions">
            <button type="button" onClick={closeDetails}>
              CLOSE DETAILS
            </button>
          </div>
          <section>
            <h4>PAIR IDENTITY</h4>
            <div className="dup-grid">
              <IdentityBlock
                identity={selectedPlan.company_a}
                fallbackName={selectedPlan.company_a_name}
                fallbackId={selectedPlan.company_a_id}
                detailed
              />
              <IdentityBlock
                identity={selectedPlan.company_b}
                fallbackName={selectedPlan.company_b_name}
                fallbackId={selectedPlan.company_b_id}
                detailed
              />
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
                  recordAId={selectedPlan.company_a_id}
                  recordBId={selectedPlan.company_b_id}
                  survivorId={selectedPlan.survivor_company_id}
                  sourceId={selectedPlan.source_company_id}
                  onChoice={(value) => onChoice(row.exception_key, value)}
                  onSave={() => onSaveDecision(selectedPlan, row.exception_key)}
                />
              </div>
            ))}
          </section>
          <PreservationPanel
            preservation={selectedPlan.preservation}
            companyAId={selectedPlan.company_a_id}
            companyBId={selectedPlan.company_b_id}
          />
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
          <div className="dup-simple-actions">
            <button type="button" onClick={closeDetails}>
              CLOSE DETAILS
            </button>
          </div>
        </article>
      ) : null}
    </div>
  )
}
