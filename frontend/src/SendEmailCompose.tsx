/** Workspace Send Email compose panel — preview/compose only; no auto-send. */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  listEmailAccounts,
  listEmailTemplates,
  previewClientEmail,
  sendClientEmail,
  type ClientEmailAccount,
  type ClientEmailPreviewResult,
  type ClientEmailTemplate,
  type EmailComposeContactOption,
  type EmailComposeSendPayload,
} from './api/carmeco'
import { sendEmailConnectionCaption } from './emailConnectionStatus'

function display(value: string | null | undefined): string {
  const t = (value || '').trim()
  return t || '—'
}

function pickPreferredTemplate(
  templates: ClientEmailTemplate[],
  preferredName: string | null | undefined,
): ClientEmailTemplate | null {
  const wanted = (preferredName || '').trim().toLowerCase()
  if (!wanted || templates.length === 0) return null
  const exact = templates.find((t) => t.template_name.trim().toLowerCase() === wanted)
  if (exact) return exact
  // Fallback: match "send information" templates without inventing a new one
  if (wanted.includes('send information')) {
    return (
      templates.find((t) =>
        t.template_name.trim().toLowerCase().includes('send information'),
      ) || null
    )
  }
  return null
}

export type SendEmailComposeProps = {
  open: boolean
  onClose: () => void
  clientId: number
  clientName?: string
  companyId: number | null
  companyName?: string
  externalRecordNo?: string
  contacts: EmailComposeContactOption[]
  /** When set (Contact Workspace), lock To to this contact. */
  lockedContactId?: number | null
  /** Prefill To from Log Outreach (editable unless locked). */
  preferredContactId?: number | null
  /** Prefill template by existing name (e.g. Carmeco Send Information Template). */
  preferredTemplateName?: string | null
  /** Allow selecting contacts that have no email (blocks send until email exists). */
  allowContactsWithoutEmail?: boolean
  appointmentDate?: string
  appointmentTime?: string
  appointmentContact?: string
  appointmentEventId?: number | null
}

export default function SendEmailCompose({
  open,
  onClose,
  clientId,
  clientName,
  companyId,
  companyName,
  externalRecordNo,
  contacts,
  lockedContactId = null,
  preferredContactId = null,
  preferredTemplateName = null,
  allowContactsWithoutEmail = false,
  appointmentDate = '',
  appointmentTime = '',
  appointmentContact = '',
  appointmentEventId = null,
}: SendEmailComposeProps) {
  const selectableContacts = useMemo(() => {
    if (allowContactsWithoutEmail || preferredContactId != null || lockedContactId != null) {
      return contacts
    }
    return contacts.filter((c) => (c.email || '').trim())
  }, [allowContactsWithoutEmail, contacts, lockedContactId, preferredContactId])

  const [accounts, setAccounts] = useState<ClientEmailAccount[]>([])
  const [templates, setTemplates] = useState<ClientEmailTemplate[]>([])
  const [accountId, setAccountId] = useState('')
  const [contactId, setContactId] = useState('')
  const [templateKey, setTemplateKey] = useState('blank')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [signature, setSignature] = useState('')
  const [preview, setPreview] = useState<ClientEmailPreviewResult | null>(null)
  const [showPreview, setShowPreview] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const selectedAccount = useMemo(
    () => accounts.find((a) => String(a.account_id) === accountId) || null,
    [accounts, accountId],
  )
  const selectedContact = useMemo(
    () => selectableContacts.find((c) => String(c.contact_id) === contactId) || null,
    [selectableContacts, contactId],
  )
  const recipientEmail = (selectedContact?.email || '').trim()
  const recipientEmailMissing = Boolean(selectedContact) && !recipientEmail
  const connected =
    (selectedAccount?.connection_status || '').toLowerCase() === 'connected'

  const hasUnresolved =
    /\bUNRESOLVED\b/.test(`${subject}\n${body}\n${signature}`) ||
    /\[[^\]\n]{1,80}\]/.test(`${subject}\n${body}\n${signature}`)

  const canAttemptSend =
    connected &&
    Boolean(recipientEmail) &&
    Boolean(subject.trim()) &&
    !hasUnresolved &&
    showPreview

  async function onRequestSend() {
    if (!canAttemptSend || !selectedAccount || !selectedContact || !recipientEmail) {
      setError(
        !connected
          ? 'Sender is Not Connected — connect Google first.'
          : recipientEmailMissing
            ? 'Recipient email is missing — add an email on the contact before sending.'
            : hasUnresolved
              ? 'Remove UNRESOLVED placeholders before sending.'
              : !showPreview
                ? 'Preview and review the email before sending.'
                : 'Subject and recipient email are required.',
      )
      return
    }
    setConfirmOpen(true)
  }

  async function onConfirmSend() {
    if (!selectedAccount || !selectedContact || !recipientEmail) return
    const to = recipientEmail
    const ok = window.confirm(
      `Send this email from ${selectedAccount.email_address} to ${to}?`,
    )
    if (!ok) {
      setConfirmOpen(false)
      return
    }
    setBusy(true)
    setError(null)
    setConfirmOpen(false)
    try {
      const result = await sendClientEmail(clientId, {
        account_id: selectedAccount.account_id,
        contact_id: selectedContact.contact_id,
        company_id: companyId,
        template_id: templateKey === 'blank' ? null : Number(templateKey),
        to_address: to,
        subject,
        body,
        signature,
        appointment_event_id: appointmentEventId,
        confirm_send: true,
        confirm_recipient: to,
      })
      setMsg(result.message || 'Email sent.')
      setShowPreview(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Send failed.')
    } finally {
      setBusy(false)
    }
  }

  const applyRender = useCallback(
    async (opts?: { previewMode?: boolean }) => {
      const acct = Number(accountId)
      const ct = Number(contactId)
      if (!acct || !ct) {
        setError('Select From (sender) and To (contact) first.')
        return null
      }
      setBusy(true)
      setError(null)
      try {
        const templateId =
          templateKey === 'blank' || !templateKey ? null : Number(templateKey)
        const result = await previewClientEmail(clientId, {
          account_id: acct,
          contact_id: ct,
          template_id: templateId,
          appointment_date: appointmentDate,
          appointment_time: appointmentTime,
          appointment_contact: appointmentContact,
          appointment_event_id: appointmentEventId,
        })
        setSubject(result.subject || '')
        // Editable message only — master signature stays separate.
        setBody(result.message_body || result.body || '')
        setSignature(result.signature || '')
        setPreview(result)
        setMsg(result.message)
        if (opts?.previewMode) setShowPreview(true)
        return result
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Compose render failed.')
        return null
      } finally {
        setBusy(false)
      }
    },
    [
      accountId,
      appointmentContact,
      appointmentDate,
      appointmentEventId,
      appointmentTime,
      clientId,
      contactId,
      templateKey,
    ],
  )

  useEffect(() => {
    if (!open || !(clientId > 0)) return
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      setShowPreview(false)
      setPreview(null)
      setSubject('')
      setBody('')
      setSignature('')
      setMsg(null)
      try {
        const [acctList, tmplList] = await Promise.all([
          listEmailAccounts(clientId),
          listEmailTemplates(clientId),
        ])
        if (cancelled) return
        const activeAccts = acctList.filter((a) => a.active !== false)
        setAccounts(activeAccts)
        const activeTmpls = tmplList.filter((t) => t.is_active)
        setTemplates(activeTmpls)

        const def =
          activeAccts.find((a) => a.is_default) || activeAccts[0] || null
        setAccountId(def ? String(def.account_id) : '')

        const preferredId =
          lockedContactId != null && lockedContactId > 0
            ? lockedContactId
            : preferredContactId != null && preferredContactId > 0
              ? preferredContactId
              : null

        const pool =
          allowContactsWithoutEmail || preferredId != null
            ? contacts
            : contacts.filter((c) => (c.email || '').trim())

        let nextContact = ''
        if (preferredId != null) {
          const hit = pool.find((c) => c.contact_id === preferredId)
          if (hit) nextContact = String(hit.contact_id)
        } else if (pool.filter((c) => (c.email || '').trim()).length === 1) {
          const only = pool.find((c) => (c.email || '').trim())
          if (only) nextContact = String(only.contact_id)
        }
        setContactId(nextContact)

        const preferredTmpl = pickPreferredTemplate(activeTmpls, preferredTemplateName)
        setTemplateKey(preferredTmpl ? String(preferredTmpl.template_id) : 'blank')
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load email setup.')
          setAccounts([])
          setTemplates([])
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
    // contacts identity changes often from parent maps — key off open + preference ids
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    open,
    clientId,
    lockedContactId,
    preferredContactId,
    preferredTemplateName,
    allowContactsWithoutEmail,
  ])

  // Auto-render when sender + contact + template ready (populate editable draft)
  useEffect(() => {
    if (!open || loading || !accountId || !contactId) return
    void applyRender({ previewMode: false })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-render when selection changes
  }, [open, loading, accountId, contactId, templateKey])

  if (!open) return null

  const futurePayload: EmailComposeSendPayload | null =
    accountId && contactId
      ? {
          client_id: clientId,
          company_id: companyId,
          contact_id: Number(contactId),
          sender_account_id: Number(accountId),
          template_id: templateKey === 'blank' ? null : Number(templateKey),
          subject,
          body,
          rep: preview?.revenue_specialist || '',
          appointment_event_id: appointmentEventId,
        }
      : null

  const showComposeFields = !loading && accounts.length > 0 && selectableContacts.length > 0

  return (
    <section
      id="send-email-compose"
      className="panel panel--wide"
      style={{ marginTop: '1rem' }}
    >
      <div className="panel-header">
        <h2>SEND EMAIL</h2>
        <span className="queue-source">
          {clientName ? `Working For: ${clientName}` : 'Client-scoped'} · Compose only
        </span>
      </div>
      <p className="queue-sub">
        Uses this client&apos;s sender accounts and approved templates only. Edits here do not change
        stored templates. No automatic send — Preview and Send Email are explicit.
        {companyName ? ` · Company: ${companyName}` : ''}
        {externalRecordNo ? ` · Record No. ${externalRecordNo}` : ''}
      </p>

      {loading ? <p className="data-status">Loading sender accounts and templates…</p> : null}
      {error ? (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      ) : null}
      {msg ? <p className="data-status">{msg}</p> : null}

      {!loading && accounts.length === 0 ? (
        <p className="empty-state">
          No sender accounts configured for this client. Add one under Client Knowledge → Client
          Operations → EMAIL &amp; SENDING.
        </p>
      ) : null}

      {!loading && selectableContacts.length === 0 ? (
        <p className="empty-state">
          No contacts available for this company. Open a Contact Workspace to add a contact —
          NorthStar will not invent a recipient email.
        </p>
      ) : null}

      {recipientEmailMissing ? (
        <p className="data-status data-status--error" role="alert">
          Recipient email is missing for the selected contact. You can edit the draft, but Send
          Email stays disabled until a valid email is on the contact record.
        </p>
      ) : null}

      {showComposeFields ? (
        <div className="setup-grid" style={{ marginBottom: '0.75rem' }}>
          <label className="setup-field">
            <span className="setup-field-label">From</span>
            <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
              <option value="">Select sender…</option>
              {accounts.map((a) => {
                const ok = (a.connection_status || '').toLowerCase() === 'connected'
                return (
                  <option key={a.account_id} value={a.account_id}>
                    {a.email_address}
                    {a.is_default ? ' (default)' : ''} — {ok ? 'Connected' : 'Not Connected'}
                  </option>
                )
              })}
            </select>
            {selectedAccount ? (
              <span className="queue-sub" style={{ display: 'block', marginTop: '0.25rem' }}>
                {sendEmailConnectionCaption(selectedAccount.connection_status, connected)}
              </span>
            ) : null}
          </label>

          <label className="setup-field">
            <span className="setup-field-label">To</span>
            {lockedContactId != null && lockedContactId > 0 ? (
              <input
                className="edit-input"
                readOnly
                value={
                  selectedContact
                    ? `${selectedContact.contact_name}${
                        selectedContact.title ? ` — ${selectedContact.title}` : ''
                      } — ${recipientEmail || '(no email on contact)'}`
                    : 'Selected contact'
                }
              />
            ) : (
              <select value={contactId} onChange={(e) => setContactId(e.target.value)}>
                <option value="">Select contact…</option>
                {selectableContacts.map((c) => {
                  const em = (c.email || '').trim()
                  return (
                    <option key={c.contact_id} value={c.contact_id}>
                      {c.contact_name}
                      {c.title ? ` — ${c.title}` : ''} — {em || '(no email)'}
                    </option>
                  )
                })}
              </select>
            )}
          </label>

          <label className="setup-field">
            <span className="setup-field-label">Template</span>
            <select value={templateKey} onChange={(e) => setTemplateKey(e.target.value)}>
              <option value="blank">Blank Email</option>
              {templates.map((t) => (
                <option key={t.template_id} value={t.template_id}>
                  {t.template_name}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}

      {showComposeFields ? (
        <>
          <label className="edit-field" style={{ display: 'grid', gap: '0.25rem' }}>
            <span className="edit-field__label">Subject</span>
            <input
              className="edit-input"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="Enter subject if the template does not provide one"
            />
          </label>
          <label
            className="edit-field"
            style={{ display: 'grid', gap: '0.25rem', marginTop: '0.5rem' }}
          >
            <span className="edit-field__label">Body</span>
            <textarea
              rows={10}
              style={{ width: '100%' }}
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
            <span className="queue-sub">
              Sender signature is added automatically. Edit the message only — the master signature
              is not changed.
            </span>
          </label>
          <div style={{ marginTop: '0.5rem' }}>
            <div className="queue-sub">Sender signature (read-only · this Working For client)</div>
            <div className="queue-sub" style={{ whiteSpace: 'pre-wrap' }}>
              {signature.trim()
                ? signature
                : '— (no client-specific signature configured)'}
            </div>
          </div>

          {(preview?.unresolved_placeholders || []).length > 0 ? (
            <div style={{ marginTop: '0.65rem' }}>
              <strong>Unresolved placeholders</strong>
              <ul className="ask-history-list">
                {preview!.unresolved_placeholders.map((p, i) => (
                  <li key={`${p.placeholder}-${i}`}>
                    <code>{p.placeholder}</code> · UNRESOLVED
                    {p.note ? ` · ${p.note}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {(preview?.legacy_token_notes || []).length > 0 ? (
            <div className="queue-sub" style={{ marginTop: '0.4rem' }}>
              {(preview!.legacy_token_notes || []).map((n) => (
                <div key={n}>{n}</div>
              ))}
            </div>
          ) : null}

          <div className="setup-actions" style={{ marginTop: '0.85rem' }}>
            <button
              type="button"
              className="primary-btn"
              disabled={busy || !accountId || !contactId}
              onClick={() => void applyRender({ previewMode: true })}
            >
              {busy ? 'Working…' : 'Preview'}
            </button>
            <button type="button" className="link-btn" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="primary-btn"
              disabled={busy || !canAttemptSend}
              title={
                recipientEmailMissing
                  ? 'Recipient email is missing'
                  : connected
                    ? 'Requires Preview review, subject, recipient, and no UNRESOLVED placeholders'
                    : 'Connect Google Account before sending'
              }
              onClick={() => void onRequestSend()}
            >
              Send Email
            </button>
          </div>
          {confirmOpen && selectedAccount && selectedContact && recipientEmail ? (
            <div className="edit-field" style={{ marginTop: '0.75rem' }}>
              <strong>Confirm send</strong>
              <p>
                Send this email from <strong>{selectedAccount.email_address}</strong> to{' '}
                <strong>{recipientEmail}</strong>?
              </p>
              <div className="setup-actions">
                <button
                  type="button"
                  className="primary-btn"
                  disabled={busy}
                  onClick={() => void onConfirmSend()}
                >
                  Send Email
                </button>
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => setConfirmOpen(false)}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : null}

          {showPreview && preview ? (
            <div
              className="edit-field"
              style={{ marginTop: '0.85rem', display: 'grid', gap: '0.35rem' }}
            >
              <strong>Preview</strong>
              <div>
                <strong>From:</strong>{' '}
                {display(preview.from_display || selectedAccount?.email_address)}
              </div>
              <div>
                <strong>Connection:</strong>{' '}
                {sendEmailConnectionCaption(preview.connection_status, preview.connection_connected)}
              </div>
              <div>
                <strong>To:</strong>{' '}
                {recipientEmailMissing
                  ? '(no email on contact)'
                  : display(preview.to_address || recipientEmail)}
                {preview.to_contact_name ? ` (${preview.to_contact_name})` : ''}
              </div>
              <div>
                <strong>Subject:</strong>
                <div style={{ whiteSpace: 'pre-wrap' }}>{display(subject)}</div>
              </div>
              <div>
                <strong>Message</strong>
                <div
                  className="edit-field"
                  style={{ whiteSpace: 'pre-wrap', marginTop: '0.25rem' }}
                >
                  {display(preview.body || body)}
                </div>
              </div>
              {(preview.signature_source || preview.signature_placement) && (
                <p className="queue-sub" style={{ marginTop: '0.35rem' }}>
                  (Outside email) Sender signature from{' '}
                  {display(preview.signature_source) || 'client_email_signatures'} ·{' '}
                  {display(preview.signature_placement)}
                </p>
              )}
              {futurePayload ? (
                <p className="queue-sub">
                  Future send payload ready (not submitted): client={futurePayload.client_id},
                  company={futurePayload.company_id ?? '—'}, contact={futurePayload.contact_id},
                  sender={futurePayload.sender_account_id}, template=
                  {futurePayload.template_id ?? 'blank'}
                </p>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  )
}
