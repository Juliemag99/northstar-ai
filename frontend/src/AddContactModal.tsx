/** Manual CRM contact create/link. Does not create NorthStar users as outside contacts. */
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import {
  lookupCompaniesForClient,
  previewManualContact,
  saveManualContact,
  type CompanyLookupItem,
  type ManualContactMatch,
} from './api/carmeco'
import { SELECT_CLIENT_FOR_WRITE, requireWriteClientId } from './writeClient'

export type AddContactModalProps = {
  open: boolean
  onClose: () => void
  clientId: number | null
  clientName: string
  lockedCompanyId?: number | null
  lockedCompanyName?: string | null
}

const emptyForm = {
  first_name: '',
  last_name: '',
  title: '',
  email: '',
  phone: '',
  alt_phone: '',
}

const MATCH_FIELDS = ['first_name', 'last_name', 'title', 'email', 'phone', 'alt_phone'] as const

export function contactMatchFingerprint(form: typeof emptyForm, companyId: number | null): string {
  return [...MATCH_FIELDS.map((key) => form[key].trim().toLowerCase()), String(companyId ?? '')].join('|')
}

type DupStatusKind = 'idle' | 'checking' | 'matches' | 'none' | 'stale'

function duplicateStatus(kind: DupStatusKind): { text: string } {
  switch (kind) {
    case 'checking':
      return { text: 'Checking…' }
    case 'matches':
      return { text: 'Possible duplicate found' }
    case 'none':
      return { text: 'No likely duplicates found' }
    case 'stale':
      return { text: 'Check required again because matching fields changed' }
    default:
      return { text: 'Not checked' }
  }
}

function formatUsPhoneDisplay(raw: string): string {
  const trimmed = raw.trim()
  if (!trimmed) return ''
  const extMatch = trimmed.match(/(?:\s+extension|\s+ext\.?|\s+x|x|#)\s*([0-9][0-9 \-]*)$/i)
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

function hasContactInfo(email: string, phone: string, altPhone: string): boolean {
  return Boolean(email.trim() || phone.trim() || altPhone.trim())
}

function matchLabel(match: ManualContactMatch): string {
  const name = `${match.first_name} ${match.last_name}`.trim() || 'Existing contact'
  const bits = [name]
  if (match.title.trim()) bits.push(match.title.trim())
  if (match.company_name.trim()) bits.push(match.company_name.trim())
  return bits.join(' · ')
}

function matchMetaLines(match: ManualContactMatch, clientName: string): string[] {
  const lines: string[] = []
  if (match.email.trim()) lines.push(`Email: ${match.email.trim()}`)
  const phone = (match.phone || match.alt_phone).trim()
  if (phone) lines.push(`Phone: ${phone}`)
  if (match.reasons.length) lines.push(`Match reasons: ${match.reasons.join(', ')}`)
  if (match.confidence) lines.push(`Match confidence: ${match.confidence}`)
  if (!match.same_company && !match.already_assigned) {
    lines.push('Different company — linking will use the existing person')
  }
  if (match.already_assigned) lines.push(`Already linked to ${clientName}.`)
  return lines
}

function contactGuidance(
  matches: ManualContactMatch[],
  clientName: string,
  canCreate: boolean,
  backendMessage: string | null,
): string {
  const allLinked = matches.every((match) => match.already_assigned)
  const anyUnlinked = matches.some((match) => !match.already_assigned)
  if (allLinked) {
    return canCreate
      ? `This contact is already linked to ${clientName}. Open the existing contact, or confirm Create New Anyway.`
      : `This contact is already linked to ${clientName}. Open the existing contact.`
  }
  if (anyUnlinked && matches.some((match) => match.already_assigned)) {
    return canCreate
      ? `A possible match exists. Open an existing contact, link one that is not already on ${clientName}, or confirm Create New Anyway.`
      : `A likely match exists. Open an existing contact, or link one that is not already on ${clientName}.`
  }
  if (!canCreate) {
    return backendMessage || 'A likely match exists. Link the existing contact instead of creating a duplicate.'
  }
  return 'A possible match exists. Open or link the existing contact, or confirm Create New Anyway.'
}

export default function AddContactModal({
  open,
  onClose,
  clientId,
  clientName,
  lockedCompanyId = null,
  lockedCompanyName = null,
}: AddContactModalProps) {
  const navigate = useNavigate()
  const nameRef = useRef<HTMLInputElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const checkRowRef = useRef<HTMLDivElement>(null)
  const resultsRef = useRef<HTMLDivElement>(null)
  const locked = lockedCompanyId != null && lockedCompanyId > 0
  const [form, setForm] = useState(emptyForm)
  const [companyQuery, setCompanyQuery] = useState('')
  const [companyHits, setCompanyHits] = useState<CompanyLookupItem[]>([])
  const [selectedCompany, setSelectedCompany] = useState<CompanyLookupItem | null>(null)
  const [confirmWithoutInfo, setConfirmWithoutInfo] = useState(false)
  const [matches, setMatches] = useState<ManualContactMatch[]>([])
  const [canCreate, setCanCreate] = useState(true)
  const [needsInfoConfirm, setNeedsInfoConfirm] = useState(false)
  const [checkedFingerprint, setCheckedFingerprint] = useState<string | null>(null)
  const [lastCheckedFingerprint, setLastCheckedFingerprint] = useState<string | null>(null)
  const [checkMessage, setCheckMessage] = useState<string | null>(null)
  const [anywayArmed, setAnywayArmed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [checking, setChecking] = useState(false)
  const [searching, setSearching] = useState(false)

  const companyId = selectedCompany?.id ?? null
  const currentFingerprint = useMemo(
    () => contactMatchFingerprint(form, companyId),
    [form, companyId],
  )
  const checkIsCurrent = checkedFingerprint === currentFingerprint
  const hasMatches = checkIsCurrent && matches.length > 0
  const noMatches = checkIsCurrent && matches.length === 0 && checkedFingerprint != null
  const missingInfo = !hasContactInfo(form.email, form.phone, form.alt_phone)
  const canOrdinarySave =
    noMatches &&
    Boolean(form.first_name.trim() && form.last_name.trim() && companyId) &&
    (!missingInfo || confirmWithoutInfo)
  const alreadyLinkedToActiveClient = hasMatches && matches.every((match) => match.already_assigned)
  const guidanceMessage = hasMatches
    ? contactGuidance(matches, clientName, canCreate, checkMessage)
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
    setCompanyQuery('')
    setCompanyHits([])
    setConfirmWithoutInfo(false)
    setMatches([])
    setCanCreate(true)
    setNeedsInfoConfirm(false)
    setCheckedFingerprint(null)
    setLastCheckedFingerprint(null)
    setCheckMessage(null)
    setAnywayArmed(false)
    setError(null)
    setBusy(false)
    setChecking(false)
    if (locked && lockedCompanyId) {
      setSelectedCompany({
        id: lockedCompanyId,
        company_name: lockedCompanyName || 'This company',
        external_record_no: '',
      })
    } else {
      setSelectedCompany(null)
    }
  }, [open, locked, lockedCompanyId, lockedCompanyName])

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

  useEffect(() => {
    if (!open || locked) return
    let cancelled = false
    const timer = window.setTimeout(() => {
      let writeId: number
      try {
        writeId = requireWriteClientId(clientId)
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
        }
        return
      }
      setSearching(true)
      void lookupCompaniesForClient(writeId, companyQuery)
        .then((data) => {
          if (!cancelled) setCompanyHits(data.companies)
        })
        .catch((err) => {
          if (!cancelled) {
            setCompanyHits([])
            setError(err instanceof Error ? err.message : 'Failed to search companies.')
          }
        })
        .finally(() => {
          if (!cancelled) setSearching(false)
        })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [open, locked, clientId, companyQuery])

  if (!open) return null

  function invalidateCheck() {
    setCheckedFingerprint(null)
    setMatches([])
    setCheckMessage(null)
    setAnywayArmed(false)
    setCanCreate(true)
    setNeedsInfoConfirm(false)
  }

  function updateField(key: keyof typeof emptyForm, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }))
    if ((MATCH_FIELDS as readonly string[]).includes(key)) invalidateCheck()
  }

  function chooseCompany(hit: CompanyLookupItem | null) {
    setSelectedCompany(hit)
    invalidateCheck()
  }

  function openExisting(match: ManualContactMatch) {
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    onClose()
    navigate(`/contacts/${match.contact_id}?client_id=${writeId}`)
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
    if (companyId == null || companyId <= 0) {
      setError('Search for and select a company first.')
      return
    }
    const first = form.first_name.trim()
    const last = form.last_name.trim()
    if (!first || !last) {
      setError('First name and last name are required.')
      return
    }
    setChecking(true)
    setBusy(true)
    try {
      const preview = await previewManualContact({
        client_id: writeId,
        company_id: companyId,
        first_name: first,
        last_name: last,
        title: form.title.trim(),
        email: form.email.trim(),
        phone: form.phone.trim(),
        alt_phone: form.alt_phone.trim(),
      })
      setMatches(preview.matches)
      setCanCreate(preview.can_create)
      setNeedsInfoConfirm(preview.requires_contact_info_confirmation)
      const fingerprint = contactMatchFingerprint(
        { ...form, first_name: first, last_name: last },
        companyId,
      )
      setCheckedFingerprint(fingerprint)
      setLastCheckedFingerprint(fingerprint)
      setCheckMessage(
        preview.message || (preview.matches.length ? '' : 'No likely duplicates found'),
      )
      setAnywayArmed(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to check for duplicates.')
    } finally {
      setBusy(false)
      setChecking(false)
    }
  }

  async function runSave(action: 'create' | 'link', existingContactId?: number, confirmAnyway = false) {
    setError(null)
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (companyId == null || companyId <= 0) {
      setError('Search for and select a company first.')
      return
    }
    const first = form.first_name.trim()
    const last = form.last_name.trim()
    if (action === 'create' && (!first || !last)) {
      setError('First name and last name are required.')
      return
    }
    if (action === 'create' && missingInfo && !confirmWithoutInfo) {
      setError('Enter an email or phone, or confirm creating this contact without either.')
      setNeedsInfoConfirm(true)
      return
    }
    if (action === 'create' && !checkIsCurrent) {
      setError('Check for duplicates before saving.')
      return
    }
    if (action === 'create' && hasMatches && !confirmAnyway) {
      setError(
        alreadyLinkedToActiveClient
          ? `This contact is already linked to ${clientName}. Open the existing contact.`
          : 'A possible duplicate exists. Link the existing contact, or confirm Create New Anyway.',
      )
      return
    }
    if (action === 'create' && hasMatches && !canCreate) {
      setError(
        alreadyLinkedToActiveClient
          ? `This contact is already linked to ${clientName}. Open the existing contact.`
          : 'A likely match exists. Link the existing contact instead of creating a duplicate.',
      )
      return
    }
    const payload = {
      client_id: writeId,
      company_id: companyId,
      first_name: first,
      last_name: last,
      title: form.title.trim(),
      email: form.email.trim(),
      phone: form.phone.trim(),
      alt_phone: form.alt_phone.trim(),
      confirm_without_contact_info: confirmWithoutInfo,
      created_by: 'Julie Magnani',
    }
    setBusy(true)
    try {
      if (action === 'link') {
        if (existingContactId == null) {
          setError('Select an existing contact to link.')
          return
        }
        const result = await saveManualContact({
          ...payload,
          action: 'link',
          existing_contact_id: existingContactId,
        })
        onClose()
        navigate(`/contacts/${result.contact_id}?client_id=${writeId}`, {
          state: { manualContactNotice: result.message },
        })
        return
      }
      const result = await saveManualContact({ ...payload, action: 'create' })
      onClose()
      navigate(`/contacts/${result.contact_id}?client_id=${writeId}`, {
        state: { manualContactNotice: result.message },
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save contact.')
    } finally {
      setBusy(false)
    }
  }

  function requestCreateAnyway() {
    if (!canCreate) {
      setError(
        alreadyLinkedToActiveClient
          ? `This contact is already linked to ${clientName}. Open the existing contact.`
          : 'A likely match exists. Link the existing contact instead of creating a duplicate.',
      )
      return
    }
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
    if (event.target.type === 'checkbox') return
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
        aria-labelledby="add-contact-title"
      >
        <form className="add-company-modal__form" onSubmit={onFormSubmit} onKeyDown={onFormKeyDown}>
          <header className="add-company-modal__header">
            <h2 id="add-contact-title">Add Contact</h2>
            <p>
              Working For: <strong>{clientName}</strong>
              {locked && selectedCompany ? ` · Company: ${selectedCompany.company_name}` : null}
            </p>
            <p className="queue-source">
              Check for duplicates before saving. Save Contact stays disabled until that check is
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
              <label className="edit-field">
                <span className="edit-field__label">First name</span>
                <input
                  ref={nameRef}
                  className="edit-input"
                  value={form.first_name}
                  onChange={(e) => updateField('first_name', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Last name</span>
                <input
                  className="edit-input"
                  value={form.last_name}
                  onChange={(e) => updateField('last_name', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Title</span>
                <input
                  className="edit-input"
                  value={form.title}
                  onChange={(e) => updateField('title', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Email</span>
                <input
                  className="edit-input"
                  type="email"
                  value={form.email}
                  onChange={(e) => updateField('email', e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Phone</span>
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
              <label className="edit-field">
                <span className="edit-field__label">Alternate phone</span>
                <input
                  className="edit-input"
                  value={form.alt_phone}
                  onChange={(e) => updateField('alt_phone', e.target.value)}
                  onBlur={() => {
                    const formatted = formatUsPhoneDisplay(form.alt_phone)
                    if (formatted !== form.alt_phone) updateField('alt_phone', formatted)
                  }}
                  autoComplete="off"
                />
              </label>
            </div>

            {locked ? (
              <p className="queue-source">Company is locked to this workspace.</p>
            ) : (
              <div className="edit-field" style={{ marginTop: '0.85rem' }}>
                <span className="edit-field__label">Company</span>
                {selectedCompany ? (
                  <p>
                    {selectedCompany.company_name}
                    {selectedCompany.external_record_no
                      ? ` · ${selectedCompany.external_record_no}`
                      : ''}{' '}
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => chooseCompany(null)}
                    >
                      Change
                    </button>
                  </p>
                ) : (
                  <>
                    <input
                      className="edit-input"
                      placeholder="Search this client's companies"
                      value={companyQuery}
                      onChange={(e) => setCompanyQuery(e.target.value)}
                      autoComplete="off"
                    />
                    {searching ? <p className="queue-source">Searching…</p> : null}
                    {companyHits.length > 0 ? (
                      <ul className="add-contact-company-hits">
                        {companyHits.map((hit) => (
                          <li key={hit.id}>
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => chooseCompany(hit)}
                            >
                              {hit.company_name}
                              {hit.external_record_no ? ` · ${hit.external_record_no}` : ''}
                            </button>
                          </li>
                        ))}
                      </ul>
                    ) : companyQuery.trim() && !searching ? (
                      <p className="queue-source">No companies match that search.</p>
                    ) : null}
                  </>
                )}
              </div>
            )}

            <div className="add-company-dup-check" ref={checkRowRef}>
              <button
                type="button"
                className="secondary-btn"
                data-testid="add-contact-check-duplicates"
                disabled={busy}
                onClick={() => void runCheck()}
              >
                {checkLabel}
              </button>
              <p
                className={`add-company-dup-status add-company-dup-status--${statusKind}`}
                role="status"
                aria-live="polite"
                data-testid="add-contact-dup-status"
                data-dup-status={statusKind}
              >
                {status.text}
              </p>
            </div>

            {hasMatches && guidanceMessage ? (
              <p className="data-status" role="status" data-testid="add-contact-guidance">
                {guidanceMessage}
              </p>
            ) : null}
            {anywayArmed ? (
              <p className="data-status data-status--error" role="alert">
                A possible duplicate exists. Creating a new contact will add another master record.
                Click Confirm Create New Anyway only if you are sure this is a different person.
              </p>
            ) : null}

            {hasMatches ? (
              <div
                className="add-contact-matches"
                ref={resultsRef}
                data-testid="add-contact-dup-results"
              >
                <h3>Possible existing contacts</h3>
                <ul>
                  {matches.map((match) => (
                    <li key={match.contact_id}>
                      <div>
                        <strong>{matchLabel(match)}</strong>
                        <div className="add-company-match-meta">
                          {matchMetaLines(match, clientName).map((line) =>
                            line.startsWith('Already linked') ? (
                              <div
                                key={line}
                                className="add-company-already-linked"
                                data-testid="add-contact-already-linked"
                              >
                                {line}
                              </div>
                            ) : (
                              <div key={line}>{line}</div>
                            ),
                          )}
                        </div>
                      </div>
                      <div className="add-company-match-actions">
                        <button
                          type="button"
                          className="secondary-btn"
                          disabled={busy}
                          onClick={() => openExisting(match)}
                        >
                          Open Existing Contact
                        </button>
                        {match.already_assigned ? null : (
                          <button
                            type="button"
                            className="primary-btn"
                            disabled={busy}
                            onClick={() => void runSave('link', match.contact_id)}
                          >
                            Link Existing Contact
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {missingInfo ? (
              <label className="edit-field" style={{ marginTop: '0.85rem' }}>
                <span>
                  <input
                    type="checkbox"
                    checked={confirmWithoutInfo}
                    onChange={(e) => setConfirmWithoutInfo(e.target.checked)}
                  />{' '}
                  Create without an email or phone
                </span>
              </label>
            ) : null}
            {needsInfoConfirm && !confirmWithoutInfo ? (
              <p className="queue-source">
                Check the box above to create this contact without an email or phone.
              </p>
            ) : null}
          </div>

          <footer className="add-company-modal__footer">
            <button type="button" className="secondary-btn" disabled={saving} onClick={onClose}>
              Cancel
            </button>
            {checkRequired ? (
              <button
                type="submit"
                className="secondary-btn"
                data-testid="add-contact-check-duplicates-footer"
                disabled={busy}
              >
                {checkLabel}
              </button>
            ) : null}
            {hasMatches && canCreate ? (
              <button
                type="button"
                className="primary-btn"
                data-testid="add-contact-create-anyway"
                disabled={busy || !checkIsCurrent}
                onClick={requestCreateAnyway}
              >
                {anywayArmed ? 'Confirm Create New Anyway' : 'Create New Anyway'}
              </button>
            ) : (
              <button
                type="submit"
                className="primary-btn"
                data-testid="add-contact-save"
                disabled={busy || !canOrdinarySave}
              >
                {saving ? 'Saving…' : 'Save Contact'}
              </button>
            )}
          </footer>
        </form>
      </section>
    </div>,
    document.body,
  )
}
