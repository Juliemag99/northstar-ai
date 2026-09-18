import { useEffect, useMemo, useState } from 'react'
import {
  previewLeadMasterRefresh,
  recalculateLeadMasterRefresh,
  reviewLeadMasterRefresh,
  saveLeadMasterRefreshMapping,
  saveLeadMasterRefreshPolicy,
  uploadLeadMasterRefresh,
  type LeadMasterRefreshBatch,
  type LeadMasterRefreshPlan,
} from './api/leadmasterRefresh'
import {
  NOT_MAPPED,
  REFRESH_MAPPING_FIELDS,
  normalizeRefreshMapping,
  validateRefreshMapping,
} from './leadmasterRefreshMapping'

type AssignedClient = {
  client_id: number
  client_name: string
  client_code?: string
}

type Props = {
  allMyClients: boolean
  connectClientId: number | null
  scopedClientId: number
  availableClients: AssignedClient[]
  onScopedClientId: (clientId: number) => void
  clientName: string
}

type Step = 'upload' | 'mapping' | 'policy' | 'preview' | 'review'

const STATUS_MODES = [
  { id: 'PRESERVE_EXISTING', label: 'Preserve existing CCR status' },
  { id: 'PROPOSE_CHANGES', label: 'Propose status changes (default)' },
  { id: 'AUTHORITATIVE_NONBLANK', label: 'Authoritative nonblank after confirm' },
]
const CAMPAIGN_CLASSES = [
  'ACTIVE_OPERATIONAL',
  'HISTORICAL_PROVENANCE',
  'SYSTEM_TEST_ADMIN',
  'UNKNOWN_REVIEW',
  'IGNORED',
]
const ASSIGN_STATES = ['MAPPED', 'UNMAPPED', 'INACTIVE_TARGET', 'AMBIGUOUS', 'IGNORED']

export default function AdministrationLeadmasterRefresh({
  allMyClients,
  connectClientId,
  scopedClientId,
  availableClients,
  onScopedClientId,
  clientName,
}: Props) {
  const clientId = connectClientId && connectClientId > 0 ? connectClientId : 0
  const [step, setStep] = useState<Step>('upload')
  const [file, setFile] = useState<File | null>(null)
  const [batch, setBatch] = useState<LeadMasterRefreshBatch | null>(null)
  const [draftMapping, setDraftMapping] = useState<Record<string, string>>({})
  const [statusMode, setStatusMode] = useState('PROPOSE_CHANGES')
  const [companyMode, setCompanyMode] = useState('FILL_BLANK_SAFE_FIELDS')
  const [contactMode, setContactMode] = useState('FILL_BLANK_SAFE_FIELDS')
  const [plan, setPlan] = useState<LeadMasterRefreshPlan | null>(null)
  const [reviewRows, setReviewRows] = useState<Array<Record<string, unknown>>>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const mappingErrors = useMemo(
    () => validateRefreshMapping(draftMapping, batch?.headers || []).errors,
    [draftMapping, batch],
  )

  useEffect(() => {
    if (!batch) return
    setDraftMapping({ ...batch.suggested_mapping, ...batch.mapping })
    const policy = batch.policy as {
      status_mode?: string
      company_field_mode?: string
      contact_field_mode?: string
    }
    if (policy?.status_mode) setStatusMode(String(policy.status_mode))
    if (policy?.company_field_mode) setCompanyMode(String(policy.company_field_mode))
    if (policy?.contact_field_mode) setContactMode(String(policy.contact_field_mode))
  }, [batch])

  async function onUpload() {
    if (!clientId || !file) {
      setError('Choose a client and a LeadMaster file.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await uploadLeadMasterRefresh(clientId, file)
      if (!result.batch) {
        setError(result.message || 'Upload did not create a refresh batch.')
        return
      }
      setBatch(result.batch)
      setStep('mapping')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed.')
    } finally {
      setBusy(false)
    }
  }

  async function onSaveMapping() {
    if (!batch || !clientId) return
    const check = validateRefreshMapping(draftMapping, batch.headers)
    if (!check.ok) {
      setError(check.errors[0] || 'Fix mapping errors.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const saved = await saveLeadMasterRefreshMapping(
        clientId,
        batch.batch_id,
        normalizeRefreshMapping(draftMapping),
      )
      setBatch(saved)
      setStep('policy')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save mapping.')
    } finally {
      setBusy(false)
    }
  }

  async function onSavePolicy() {
    if (!batch || !clientId) return
    setBusy(true)
    setError(null)
    try {
      const saved = await saveLeadMasterRefreshPolicy(clientId, batch.batch_id, {
        ...(batch.policy || {}),
        status_mode: statusMode,
        company_field_mode: companyMode,
        contact_field_mode: contactMode,
        notes_mode: 'APPEND_DISTINCT',
        history_mode: 'APPEND_DISTINCT_BY_SOURCE_ID_OR_FINGERPRINT',
      })
      setBatch(saved)
      const preview = await previewLeadMasterRefresh(clientId, batch.batch_id)
      setPlan(preview)
      setStep('preview')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save policy.')
    } finally {
      setBusy(false)
    }
  }

  async function onReview() {
    if (!batch || !clientId) return
    setBusy(true)
    setError(null)
    try {
      const review = await reviewLeadMasterRefresh(clientId, batch.batch_id)
      setReviewRows(review.review_rows || [])
      setStep('review')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load review rows.')
    } finally {
      setBusy(false)
    }
  }

  async function onRecalculate() {
    if (!batch || !clientId) return
    setBusy(true)
    setError(null)
    try {
      const next = await recalculateLeadMasterRefresh(clientId, batch.batch_id)
      setPlan(next)
      setReviewRows(next.review_rows || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not recalculate.')
    } finally {
      setBusy(false)
    }
  }

  const counts = plan?.counts || {}

  return (
    <section className="administration-section" aria-labelledby="lm-refresh-heading">
      <h2 id="lm-refresh-heading">LeadMaster Refresh</h2>
      <p className="queue-sub">
        Preview a newer LeadMaster export against an existing NorthStar client. Nothing is applied
        in this phase. Source system is LeadMaster only.
      </p>

      {allMyClients ? (
        <label className="setup-field" style={{ maxWidth: '24rem', margin: '0.85rem 0' }}>
          <span className="setup-field-label">Client</span>
          <select
            value={scopedClientId > 0 ? String(scopedClientId) : ''}
            onChange={(e) => onScopedClientId(Number(e.target.value) || 0)}
            aria-label="Client for LeadMaster refresh"
          >
            <option value="">Select a client…</option>
            {availableClients.map((c) => (
              <option key={c.client_id} value={c.client_id}>
                {c.client_name}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <p className="queue-sub">Active Client: <strong>{clientName}</strong></p>
      )}

      <p className="queue-sub">Source system: <strong>LEADMASTER</strong></p>

      {error ? (
        <p className="data-status data-status--error" role="alert">{error}</p>
      ) : null}

      {step === 'upload' ? (
        <fieldset className="administration-import-fieldset">
          <legend>Upload</legend>
          <input
            type="file"
            accept=".csv,.xlsx"
            aria-label="LeadMaster export file"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
          <button type="button" onClick={() => void onUpload()} disabled={busy || !clientId}>
            {busy ? 'Uploading…' : 'Upload / Continue to Mapping'}
          </button>
          <p className="queue-sub">
            Live production uploads are refused in Phase 2. Isolated/test databases may stage a
            preview. Confirm/apply is disabled.
          </p>
        </fieldset>
      ) : null}

      {step === 'mapping' && batch ? (
        <fieldset className="administration-import-fieldset">
          <legend>Mapping</legend>
          <p className="queue-sub">
            File <strong>{batch.original_filename}</strong> · SHA256 {batch.sha256.slice(0, 12)}…
            Optional fields may be {NOT_MAPPED}.
          </p>
          {REFRESH_MAPPING_FIELDS.map((field) => (
            <label key={field.key} className="setup-field">
              <span className="setup-field-label">
                {field.label}
                {field.required ? ' (required)' : ''}
              </span>
              <select
                value={draftMapping[field.key] || ''}
                aria-label={field.label}
                onChange={(e) =>
                  setDraftMapping((prev) => {
                    const next = { ...prev }
                    if (!e.target.value) delete next[field.key]
                    else next[field.key] = e.target.value
                    return next
                  })
                }
              >
                <option value="">{NOT_MAPPED}</option>
                {batch.headers.map((header) => (
                  <option key={header} value={header}>{header}</option>
                ))}
              </select>
            </label>
          ))}
          {mappingErrors.length ? (
            <ul>{mappingErrors.map((err) => <li key={err}>{err}</li>)}</ul>
          ) : null}
          <button type="button" onClick={() => void onSaveMapping()} disabled={busy}>
            Save mapping
          </button>
        </fieldset>
      ) : null}

      {step === 'policy' && batch ? (
        <fieldset className="administration-import-fieldset">
          <legend>Refresh policy (frozen into this batch)</legend>
          <label className="setup-field">
            <span className="setup-field-label">Status policy</span>
            <select
              aria-label="Status policy"
              value={statusMode}
              onChange={(e) => setStatusMode(e.target.value)}
            >
              {STATUS_MODES.map((mode) => (
                <option key={mode.id} value={mode.id}>{mode.label}</option>
              ))}
            </select>
          </label>
          <label className="setup-field">
            <span className="setup-field-label">Company field policy</span>
            <select
              aria-label="Company field policy"
              value={companyMode}
              onChange={(e) => setCompanyMode(e.target.value)}
            >
              <option value="FILL_BLANK_SAFE_FIELDS">Fill blank safe fields (default)</option>
              <option value="PRESERVE_POPULATED_MASTER">Preserve populated master</option>
              <option value="REVIEW_CONFLICTS">Review conflicts</option>
            </select>
          </label>
          <label className="setup-field">
            <span className="setup-field-label">Contact field policy</span>
            <select
              aria-label="Contact field policy"
              value={contactMode}
              onChange={(e) => setContactMode(e.target.value)}
            >
              <option value="FILL_BLANK_SAFE_FIELDS">Fill blank safe fields (default)</option>
              <option value="REVIEW_POPULATED_CONFLICTS">Review populated conflicts</option>
            </select>
          </label>
          <p className="queue-sub">
            Notes: append distinct (no replace). History: append by source id/fingerprint.
            Assignments: explicit map only, never fuzzy. Campaigns: explicit class; no auto-create.
            Blank incoming status always preserves. Invalid status never silently overwrites.
          </p>
          <p className="queue-sub">
            Assignment states: {ASSIGN_STATES.join(', ')}. Campaign classes:{' '}
            {CAMPAIGN_CLASSES.join(', ')}.
          </p>
          <button type="button" onClick={() => void onSavePolicy()} disabled={busy}>
            Save policy and preview
          </button>
        </fieldset>
      ) : null}

      {step === 'preview' && plan ? (
        <fieldset className="administration-import-fieldset">
          <legend>Preview</legend>
          <p className="queue-sub">Fingerprint: {plan.plan_fingerprint}</p>
          <p className="queue-sub">
            Status: {statusMode} · Company: {companyMode} · Contact: {contactMode} ·
            Notes: APPEND_DISTINCT · History: APPEND_DISTINCT_BY_SOURCE_ID_OR_FINGERPRINT
          </p>
          <dl className="administration-import-meta">
            {Object.entries(counts).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          <button type="button" onClick={() => void onReview()} disabled={busy}>
            Review required
          </button>
          <button type="button" onClick={() => void onRecalculate()} disabled={busy}>
            Recalculate
          </button>
        </fieldset>
      ) : null}

      {step === 'review' ? (
        <fieldset className="administration-import-fieldset">
          <legend>Review required</legend>
          <p className="queue-sub">{reviewRows.length} rows need review or are blocking.</p>
          <div className="administration-import-table-scroll">
            <table className="administration-import-preview-table">
              <thead>
                <tr>
                  <th>Row</th>
                  <th>RN</th>
                  <th>Classes</th>
                  <th>Evidence</th>
                  <th>Field</th>
                  <th>Old / current NS</th>
                  <th>New / proposed</th>
                  <th>Action</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {reviewRows.flatMap((row) => {
                  const proposals = (row.proposals as Array<Record<string, string>>) || [{}]
                  return proposals.slice(0, 8).map((prop, idx) => (
                    <tr key={`${row.source_row}-${idx}`}>
                      <td>{String(row.source_row)}</td>
                      <td>{String(row.record_no || '')}</td>
                      <td>{((row.classifications as string[]) || []).join(', ')}</td>
                      <td>{((row.match_evidence as string[]) || []).join(', ')}</td>
                      <td>{prop.field || ''}</td>
                      <td>{prop.old_value || ''}</td>
                      <td>{prop.new_value || ''}</td>
                      <td>{prop.action || ''}</td>
                      <td>
                        {prop.review_reason
                          || (typeof prop.evidence === 'string' ? prop.evidence : '')}
                      </td>
                    </tr>
                  ))
                })}
              </tbody>
            </table>
          </div>
          <p className="queue-sub">
            Recalculate after mapping, policy, or review-resolution changes. Confirm stays
            unavailable against live Carmeco, Brown, and Dawson.
          </p>
          <button type="button" onClick={() => void onRecalculate()} disabled={busy}>
            Recalculate
          </button>
          <p className="queue-sub" role="status">Live confirmation not enabled</p>
          <button type="button" disabled>
            Confirm (live confirmation not enabled)
          </button>
        </fieldset>
      ) : null}
    </section>
  )
}
