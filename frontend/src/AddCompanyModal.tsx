/** Manual CRM company create/link. Requires a specific Active Client. */
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import {
  companyWorkspaceHref,
  previewManualCompany,
  saveManualCompany,
  type ManualCompanyMatch,
  type ManualCompanySaveResult,
} from './api/carmeco'
import { SELECT_CLIENT_FOR_WRITE, requireWriteClientId } from './writeClient'

export type AddCompanyModalProps = {
  open: boolean
  onClose: () => void
  clientId: number | null
  clientName: string
  onCreated?: (result: ManualCompanySaveResult) => void | Promise<void>
}

function formatUsPhoneDisplay(raw: string): string {
  const trimmed = raw.trim()
  if (!trimmed) return ''
  const extMatch = trimmed.match(
    /(?:(?:[\s\-./,])*(?:extension|ext\.?|xt)\s*[:.\-]?\s*|(?<![A-Za-z])x\s*[:.\-]?\s*|\s*#\s*)(\d{1,6})\)?\s*$/i,
  )
  let main = trimmed
  let ext = ''
  if (extMatch && extMatch.index != null) {
    main = trimmed.slice(0, extMatch.index).trim()
    ext = (extMatch[1] || '').replace(/\D/g, '')
  }
  let digits = main.replace(/\D/g, '')
  if (digits.length >= 11 && digits.startsWith('1')) digits = digits.slice(1)
  if (digits.length > 10) digits = digits.slice(0, 10)
  if (digits.length !== 10) return trimmed
  const formatted = `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`
  return ext ? `${formatted} x${ext}` : formatted
}

const emptyForm = {
  company_name: '',
  website: '',
  address: '',
  city: '',
  state: '',
  zip: '',
  phone: '',
  industry: '',
  employee_size: '',
  sales_volume: '',
  external_record_no: '',
  notes: '',
}

const MATCH_FIELDS = [
  'company_name',
  'website',
  'address',
  'city',
  'state',
  'zip',
  'phone',
  'external_record_no',
] as const

export function companyMatchFingerprint(form: typeof emptyForm): string {
  return MATCH_FIELDS.map((key) => form[key].trim().toLowerCase()).join('|')
}

type DupStatusKind = 'idle' | 'checking' | 'matches' | 'none' | 'stale'

function duplicateStatus(kind: DupStatusKind): { text: string; testId?: string } {
  switch (kind) {
    case 'checking':
      return { text: 'Checking…' }
    case 'matches':
      return { text: 'Possible duplicate found' }
    case 'none':
      return { text: 'No likely duplicates found', testId: 'add-company-no-matches' }
    case 'stale':
      return { text: 'Check required again because matching fields changed' }
    default:
      return { text: 'Not checked' }
  }
}

export default function AddCompanyModal({
  open,
  onClose,
  clientId,
  clientName,
  onCreated,
}: AddCompanyModalProps) {
  const navigate = useNavigate()
  const nameRef = useRef<HTMLInputElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const checkRowRef = useRef<HTMLDivElement>(null)
  const resultsRef = useRef<HTMLDivElement>(null)
  const [form, setForm] = useState(emptyForm)
  const [matches, setMatches] = useState<ManualCompanyMatch[]>([])
  const [checkedFingerprint, setCheckedFingerprint] = useState<string | null>(null)
  const [lastCheckedFingerprint, setLastCheckedFingerprint] = useState<string | null>(null)
  const [checkMessage, setCheckMessage] = useState<string | null>(null)
  const [anywayArmed, setAnywayArmed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [checking, setChecking] = useState(false)

  const currentFingerprint = useMemo(() => companyMatchFingerprint(form), [form])
  const checkIsCurrent = checkedFingerprint === currentFingerprint
  const hasMatches = checkIsCurrent && matches.length > 0
  const noMatches = checkIsCurrent && matches.length === 0 && checkedFingerprint != null
  const canOrdinarySave = noMatches && Boolean(form.company_name.trim())
  const alreadyLinkedToActiveClient = hasMatches && matches.every((match) => match.already_assigned)
  const archivedHigh = hasMatches && matches.some((match) => match.archived && match.confidence === 'high')
  const guidanceMessage = archivedHigh
    ? 'An archived Master Company already exists. Restore it in Admin → Data Management → Master Data instead of creating a duplicate.'
    : alreadyLinkedToActiveClient
      ? `This company is already linked to ${clientName}. Open the existing company, or confirm Create New Anyway.`
      : checkMessage
  const checkRequired = !checkIsCurrent
  const saving = busy && !checking

  const statusKind: DupStatusKind = checking
    ? 'checking'
    : hasMatches
      ? 'matches'
      : noMatches
        ? 'none'
        : lastCheckedFingerprint != null
          ? 'stale'
          : 'idle'
  const status = duplicateStatus(statusKind)

  useEffect(() => {
    if (!open) return
    setForm(emptyForm)
    setMatches([])
    setCheckedFingerprint(null)
    setLastCheckedFingerprint(null)
    setCheckMessage(null)
    setAnywayArmed(false)
    setError(null)
    setBusy(false)
    setChecking(false)
  }, [open])

  useEffect(() => {
    if (!open) return
    nameRef.current?.focus()
  }, [open])

  useEffect(() => {
    if (!open || saving) return
    function onKey(event: globalThis.KeyboardEvent) {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, saving, onClose])

  useEffect(() => {
    if (!hasMatches || !bodyRef.current || !resultsRef.current) return
    const body = bodyRef.current
    const results = resultsRef.current
    const bodyRect = body.getBoundingClientRect()
    const resultsRect = results.getBoundingClientRect()
    const alreadyVisible =
      resultsRect.top >= bodyRect.top && resultsRect.bottom <= bodyRect.bottom
    if (alreadyVisible) return
    const target = checkRowRef.current ?? results
    const delta = target.getBoundingClientRect().top - bodyRect.top - 8
    body.scrollTo({ top: Math.max(0, body.scrollTop + delta), behavior: 'smooth' })
  }, [hasMatches, matches])

  if (!open) return null

  function updateField(key: keyof typeof emptyForm, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }))
    if ((MATCH_FIELDS as readonly string[]).includes(key)) {
      setCheckedFingerprint(null)
      setMatches([])
      setCheckMessage(null)
      setAnywayArmed(false)
    }
  }

  async function finish(result: ManualCompanySaveResult, writeId: number) {
    if (onCreated) await onCreated(result)
    onClose()
    navigate(companyWorkspaceHref(result.external_record_no, writeId), {
      state: { companySaveNotice: result.message },
    })
  }

  async function runCheck() {
    setError(null)
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    const name = form.company_name.trim()
    if (!name) {
      setError('Company name is required.')
      return
    }
    setChecking(true)
    setBusy(true)
    try {
      const preview = await previewManualCompany({
        client_id: writeId,
        ...form,
        company_name: name,
      })
      setMatches(preview.matches)
      const fingerprint = companyMatchFingerprint({ ...form, company_name: name })
      setCheckedFingerprint(fingerprint)
      setLastCheckedFingerprint(fingerprint)
      setCheckMessage(preview.message || (preview.matches.length ? '' : 'No likely duplicates found'))
      setAnywayArmed(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to check for duplicates.')
    } finally {
      setBusy(false)
      setChecking(false)
    }
  }

  async function runSave(action: 'create' | 'link', existingId?: number, confirmAnyway = false) {
    setError(null)
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    const name = form.company_name.trim()
    if (action === 'create' && !name) {
      setError('Company name is required.')
      return
    }
    if (action === 'create' && !checkIsCurrent) {
      setError('Check for duplicates before saving.')
      return
    }
    if (action === 'create' && hasMatches && !confirmAnyway) {
      setError(
        alreadyLinkedToActiveClient
          ? `This company is already linked to ${clientName}. Open the existing company, or confirm Create New Anyway.`
          : 'A possible duplicate exists. Link the existing company, or confirm Create New Anyway.',
      )
      return
    }
    const payload = {
      client_id: writeId,
      ...form,
      company_name: name,
      confirm_create_despite_match: confirmAnyway,
    }
    setBusy(true)
    try {
      if (action === 'link') {
        if (existingId == null) {
          setError('Select an existing company to link.')
          return
        }
        const result = await saveManualCompany({
          ...payload,
          action: 'link',
          existing_company_id: existingId,
        })
        await finish(result, writeId)
        return
      }
      const result = await saveManualCompany({ ...payload, action: 'create' })
      await finish(result, writeId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save company.')
    } finally {
      setBusy(false)
    }
  }

  function openExisting(match: ManualCompanyMatch) {
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (!match.external_record_no) {
      setError('That company does not have a Record No. to open.')
      return
    }
    onClose()
    navigate(companyWorkspaceHref(match.external_record_no, writeId))
  }

  function requestCreateAnyway() {
    if (!anywayArmed) {
      setAnywayArmed(true)
      setError(null)
      return
    }
    void runSave('create', undefined, true)
  }

  function onFormSubmit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    if (checkRequired) {
      void runCheck()
      return
    }
    if (canOrdinarySave) {
      void runSave('create')
    }
  }

  function onFormKeyDown(event: ReactKeyboardEvent<HTMLFormElement>) {
    if (event.key !== 'Enter') return
    if (!(event.target instanceof HTMLInputElement)) return
    event.preventDefault()
    if (busy) return
    if (checkRequired) {
      void runCheck()
      return
    }
    if (canOrdinarySave) {
      void runSave('create')
    }
  }

  const checkLabel = checking ? 'Checking…' : 'Check for Duplicates'

  return createPortal(
    <div className="contact-assign-overlay" role="presentation">
      <section
        className="panel contact-assign-modal add-contact-modal add-company-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-company-title"
      >
        <form className="add-company-modal__form" onSubmit={onFormSubmit} onKeyDown={onFormKeyDown}>
          <header className="add-company-modal__header">
            <h2 id="add-company-title">Add Company</h2>
            <p>
              Working For: <strong>{clientName}</strong>
            </p>
            <p className="queue-source">
              Check for duplicates before saving. Save Company stays disabled until that check is
              current.
            </p>
            {error ? (
              <p className="data-status data-status--error" role="alert">
                {error}
              </p>
            ) : null}
          </header>

          <div className="add-company-modal__body" ref={bodyRef}>
            <div className="add-contact-form">
              <label className="edit-field" style={{ gridColumn: '1 / -1' }}>
                <span className="edit-field__label">Company name</span>
                <input
                  ref={nameRef}
                  className="edit-input"
                  value={form.company_name}
                  onChange={(e) => updateField('company_name', e.target.value)}
                  autoComplete="off"
                />
              </label>
            </div>

            <div className="add-company-dup-check" ref={checkRowRef}>
              <button
                type="button"
                className="secondary-btn"
                data-testid="add-company-check-duplicates"
                disabled={busy}
                onClick={() => void runCheck()}
              >
                {checkLabel}
              </button>
              <p
                className={`add-company-dup-status add-company-dup-status--${statusKind}`}
                role="status"
                aria-live="polite"
                data-testid="add-company-dup-status"
                data-dup-status={statusKind}
                id={status.testId}
              >
                {status.text}
              </p>
            </div>

            {hasMatches && guidanceMessage ? (
              <p className="data-status" role="status" data-testid="add-company-guidance">
                {guidanceMessage}
              </p>
            ) : null}
            {anywayArmed ? (
              <p className="data-status data-status--error" role="alert">
                A possible duplicate exists. Creating a new company will add another master record.
                Click Confirm Create New Anyway only if you are sure this is a different company.
              </p>
            ) : null}

            {hasMatches ? (
              <div
                className="add-contact-matches"
                ref={resultsRef}
                data-testid="add-company-dup-results"
              >
                <h3>Possible existing companies</h3>
                <ul>
                  {matches.map((match) => {
                    const place = [
                      match.address,
                      [match.city, match.state].filter(Boolean).join(', '),
                      match.zip,
                    ]
                      .filter(Boolean)
                      .join(' · ')
                    const clients = (match.client_relationships || [])
                      .map((rel) =>
                        rel.status ? `${rel.client_name} (${rel.status})` : rel.client_name,
                      )
                      .filter(Boolean)
                      .join(', ')
                    return (
                      <li key={match.company_id}>
                        <div>
                          <strong>{match.company_name || 'Existing company'}</strong>
                          <div className="add-company-match-meta">
                            <div>
                              Match confidence: {match.confidence}
                              {typeof match.score === 'number' ? ` · score ${match.score}` : ''}
                            </div>
                            {match.reasons?.length ? (
                              <div>Match reasons: {match.reasons.join(', ')}</div>
                            ) : null}
                            {match.ai_available && match.ai_assessment ? (
                              <div>
                                AI: {match.ai_assessment}
                                {match.ai_confidence ? ` ({match.ai_confidence})` : ''}
                                {match.ai_explanation ? ` — ${match.ai_explanation}` : ''}
                              </div>
                            ) : null}
                            {place ? <div>Address: {place}</div> : null}
                            {match.website ? <div>Website: {match.website}</div> : null}
                            {match.phone ? <div>Phone: {match.phone}</div> : null}
                            {match.external_record_no ? (
                              <div>Record No. {match.external_record_no}</div>
                            ) : null}
                            {clients ? <div>Clients: {clients}</div> : null}
                            {match.archived ? (
                              <div
                                className="add-company-already-linked"
                                data-testid="add-company-archived-match"
                              >
                                Archived Master Company. Restore it in Admin Master Data before linking.
                              </div>
                            ) : null}
                            {match.already_assigned ? (
                              <div
                                className="add-company-already-linked"
                                data-testid="add-company-already-linked"
                              >
                                Already linked to {clientName}.
                              </div>
                            ) : null}
                          </div>
                        </div>
                        <div className="add-company-match-actions">
                          <button
                            type="button"
                            className="secondary-btn"
                            disabled={busy}
                            onClick={() => openExisting(match)}
                          >
                            Open Existing Company
                          </button>
                          {match.already_assigned || match.archived ? null : (
                            <button
                              type="button"
                              className="primary-btn"
                              disabled={busy}
                              onClick={() => void runSave('link', match.company_id)}
                            >
                              Link Existing Company
                            </button>
                          )}
                        </div>
                      </li>
                    )
                  })}
                </ul>
              </div>
            ) : null}

            <div className="add-contact-form">
              <label className="edit-field">
                <span className="edit-field__label">Website / domain</span>
                <input
                  className="edit-input"
                  value={form.website}
                  onChange={(e) => updateField('website', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Main phone</span>
                <input
                  className="edit-input"
                  value={form.phone}
                  onChange={(e) => updateField('phone', e.target.value)}
                  onBlur={() => {
                    const formatted = formatUsPhoneDisplay(form.phone)
                    if (formatted !== form.phone) updateField('phone', formatted)
                  }}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field" style={{ gridColumn: '1 / -1' }}>
                <span className="edit-field__label">Address</span>
                <input
                  className="edit-input"
                  value={form.address}
                  onChange={(e) => updateField('address', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">City</span>
                <input
                  className="edit-input"
                  value={form.city}
                  onChange={(e) => updateField('city', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">State</span>
                <input
                  className="edit-input"
                  value={form.state}
                  onChange={(e) => updateField('state', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">ZIP</span>
                <input
                  className="edit-input"
                  value={form.zip}
                  onChange={(e) => updateField('zip', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Industry</span>
                <input
                  className="edit-input"
                  value={form.industry}
                  onChange={(e) => updateField('industry', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Employee size</span>
                <input
                  className="edit-input"
                  value={form.employee_size}
                  onChange={(e) => updateField('employee_size', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Sales volume</span>
                <input
                  className="edit-input"
                  value={form.sales_volume}
                  onChange={(e) => updateField('sales_volume', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">External Record No. (optional)</span>
                <input
                  className="edit-input"
                  value={form.external_record_no}
                  onChange={(e) => updateField('external_record_no', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field" style={{ gridColumn: '1 / -1' }}>
                <span className="edit-field__label">Notes</span>
                <textarea
                  className="edit-textarea"
                  value={form.notes}
                  onChange={(e) => updateField('notes', e.target.value)}
                  rows={3}
                />
              </label>
            </div>
          </div>

          <footer className="add-company-modal__footer">
            <button type="button" className="secondary-btn" disabled={saving} onClick={onClose}>
              Cancel
            </button>
            {checkRequired ? (
              <button
                type="submit"
                className="secondary-btn"
                data-testid="add-company-check-duplicates-footer"
                disabled={busy}
              >
                {checkLabel}
              </button>
            ) : null}
            {hasMatches ? (
              <button
                type="button"
                className="primary-btn"
                data-testid="add-company-create-anyway"
                disabled={busy || !checkIsCurrent || archivedHigh}
                onClick={requestCreateAnyway}
              >
                {anywayArmed ? 'Confirm Create New Anyway' : 'Create New Anyway'}
              </button>
            ) : (
              <button
                type="submit"
                className="primary-btn"
                data-testid="add-company-save"
                disabled={busy || !canOrdinarySave}
              >
                {saving ? 'Saving…' : 'Save Company'}
              </button>
            )}
          </footer>
        </form>
      </section>
    </div>,
    document.body,
  )
}
