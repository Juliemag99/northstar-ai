/** Review modal for Add to NorthStar (preview → confirm). No auto-import. */
import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import {
  confirmCrmAdd,
  previewCrmAdd,
  type CrmAddCompanyInput,
  type CrmAddContactInput,
  type CrmAddPreviewResponse,
} from './api/carmeco'
import CampaignRoutePrompt, { type CampaignRouteOffer } from './CampaignRoutePrompt'

export type AddToNorthStarProps = {
  open: boolean
  onClose: () => void
  clientId: number
  clientName?: string
  company: CrmAddCompanyInput
  contacts: CrmAddContactInput[]
  /** When researching an existing company — forces link, no company create. */
  knownCompanyId?: number | null
  trustedExternalRecordNo?: string | null
  researchRunId?: number | null
  source?: string
  provider?: string
  mode?: 'full' | 'contacts_only'
}

type ContactRowState = {
  index: number
  selected: boolean
  decision: string
  contact: CrmAddContactInput
  matchStatus: string
  matchedContactId: number | null
  matchedName: string
  reasons: string[]
}

export default function AddToNorthStar({
  open,
  onClose,
  clientId,
  clientName,
  company: initialCompany,
  contacts: initialContacts,
  knownCompanyId = null,
  trustedExternalRecordNo = null,
  researchRunId = null,
  source = '',
  provider = '',
  mode = 'full',
}: AddToNorthStarProps) {
  const navigate = useNavigate()
  const [company, setCompany] = useState<CrmAddCompanyInput>(initialCompany)
  const [preview, setPreview] = useState<CrmAddPreviewResponse | null>(null)
  const [rows, setRows] = useState<ContactRowState[]>([])
  const [companyDecision, setCompanyDecision] = useState('auto')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [campaignRouteOffer, setCampaignRouteOffer] = useState<CampaignRouteOffer | null>(null)
  const [pendingWorkspacePath, setPendingWorkspacePath] = useState<string | null>(null)

  const contactsOnly = mode === 'contacts_only' || knownCompanyId != null

  async function runPreview(contactList?: CrmAddContactInput[]) {
    if (!(clientId > 0)) {
      setError('Working For client is required.')
      return
    }
    const contactsForPreview =
      contactList ??
      (rows.length ? rows.map((r) => r.contact) : initialContacts)
    setLoading(true)
    setError(null)
    setMsg(null)
    try {
      const result = await previewCrmAdd({
        client_id: clientId,
        company: initialCompany,
        contacts: contactsForPreview,
        research_run_id: researchRunId,
        source,
        provider,
        known_company_id: knownCompanyId,
        trusted_external_record_no: trustedExternalRecordNo,
      })
      setPreview(result)
      if (result.company_match_type === 'existing_company') {
        setCompanyDecision('use_existing')
      } else if (result.company_match_type === 'possible_match') {
        setCompanyDecision('') // force explicit
      } else if (!contactsOnly) {
        setCompanyDecision('create_new')
      }
      setRows((prev) => {
        const base =
          prev.length > 0
            ? prev
            : contactsForPreview.map((c, index) => ({
                index,
                selected: true,
                decision: 'auto',
                contact: c,
                matchStatus: 'new',
                matchedContactId: null as number | null,
                matchedName: '',
                reasons: [] as string[],
              }))
        return base.map((row) => {
          const hit =
            result.contacts.find((c) => c.index === row.index) ||
            result.contacts.find(
              (c) =>
                (c.full_name || '').toLowerCase() ===
                (row.contact.full_name || '').toLowerCase(),
            )
          if (!hit) return { ...row, selected: true }
          let decision = 'auto'
          if (hit.match_status === 'possible_match') decision = ''
          if (hit.match_status === 'existing') decision = 'use_existing'
          if (hit.match_status === 'new') decision = 'create_new'
          return {
            ...row,
            selected: true,
            matchStatus: hit.match_status,
            matchedContactId: hit.matched_contact_id,
            matchedName: hit.matched_contact_name,
            reasons: hit.match_reasons || [],
            decision,
          }
        })
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview failed.')
    } finally {
      setLoading(false)
    }
  }

  // Open handoff: parent already chose which contacts to add — preselect them and
  // preview immediately with THAT list (do not rely on async rows state).
  const contactsHandoffKey = initialContacts
    .map((c) => `${c.full_name}|${c.title}|${c.linkedin}|${c.source_url}`)
    .join('||')

  useEffect(() => {
    if (!open) return
    setCompany(initialCompany)
    setPreview(null)
    setError(null)
    setMsg(null)
    setCompanyDecision(contactsOnly ? 'use_existing' : 'auto')
    const nextRows = initialContacts.map((c, index) => ({
      index,
      selected: true,
      decision: 'auto',
      contact: c,
      matchStatus: 'new',
      matchedContactId: null as number | null,
      matchedName: '',
      reasons: [] as string[],
    }))
    setRows(nextRows)
    if (clientId > 0) {
      void runPreview(initialContacts)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional open handoff
  }, [open, clientId, knownCompanyId, trustedExternalRecordNo, researchRunId, contactsHandoffKey])

  const selectedCount = useMemo(
    () => rows.filter((r) => r.selected).length,
    [rows],
  )

  async function onConfirm() {
    if (!preview) {
      setError('Run preview first.')
      return
    }
    const selectedRows = rows.filter((r) => r.selected)
    if (selectedRows.length === 0) {
      setError('Select at least one contact to add.')
      return
    }
    if (!contactsOnly && !companyDecision) {
      setError('Choose Use Existing or Create New for the company match.')
      return
    }
    for (const row of selectedRows) {
      if (row.matchStatus === 'possible_match' && !row.decision) {
        setError(`Choose a decision for possible match: ${row.contact.full_name || 'contact'}`)
        return
      }
    }
    setBusy(true)
    setError(null)
    try {
      const result = await confirmCrmAdd({
        client_id: clientId,
        company,
        contacts: selectedRows.map((r) => ({
          contact: r.contact,
          selected: true,
          decision: r.decision || 'auto',
          matched_contact_id: r.matchedContactId,
        })),
        company_decision: contactsOnly ? 'use_existing' : companyDecision || 'auto',
        existing_company_id: preview.matched_company_id,
        known_company_id: knownCompanyId,
        trusted_external_record_no: trustedExternalRecordNo,
        research_run_id: researchRunId,
        source,
        provider,
      })
      setMsg(result.message)
      const firstContact = (result.contacts || []).find(
        (row) => row && typeof row === 'object' && Number((row as { contact_id?: number }).contact_id) > 0,
      ) as { contact_id?: number } | undefined
      setPendingWorkspacePath(result.workspace_path || null)
      setCampaignRouteOffer({
        clientId,
        companyId: result.company_id,
        contactId: firstContact?.contact_id ?? null,
        researchRunId: researchRunId ?? null,
        source: 'research',
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Confirm failed.')
    } finally {
      setBusy(false)
    }
  }

  function openExisting() {
    const rn =
      preview?.matched_external_record_no || trustedExternalRecordNo || ''
    if (!rn) {
      setError('No existing record to open.')
      return
    }
    onClose()
    navigate(`/companies/${encodeURIComponent(rn)}?client_id=${clientId}`)
  }

  function finishCampaignRoute() {
    const path = pendingWorkspacePath
    setCampaignRouteOffer(null)
    setPendingWorkspacePath(null)
    onClose()
    if (path) navigate(path)
  }

  if (!open) return null

  return (
    <>
      <CampaignRoutePrompt offer={campaignRouteOffer} onClose={finishCampaignRoute} />
      {createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-to-ns-title"
      data-testid="add-to-northstar-modal"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(15, 23, 42, 0.45)',
        zIndex: 220,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        padding: '2rem 1rem',
        overflow: 'auto',
      }}
    >
      <section
        className="panel panel--wide"
        style={{ maxWidth: '42rem', width: '100%', margin: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-header">
          <h2 id="add-to-ns-title">ADD TO NORTHSTAR</h2>
          <button type="button" className="link-btn" onClick={onClose}>
            Cancel
          </button>
        </div>
        <p className="queue-sub">
          Working For: {clientName || `Client #${clientId}`} · Initial Status: New
          (only when creating relationship) · Preview before write
        </p>

        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : null}
        {msg ? <p className="data-status">{msg}</p> : null}
        {loading ? <p className="data-status">Running duplicate check…</p> : null}

        <h3 className="add-note-card__title">Company</h3>
        <div className="setup-grid">
          {(
            [
              ['company_name', 'Company'],
              ['website', 'Website'],
              ['address', 'Address'],
              ['city', 'City'],
              ['state', 'State'],
              ['zip', 'Zip'],
              ['phone', 'Phone'],
              ['industry', 'Industry'],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="edit-field">
              <span className="edit-field__label">{label}</span>
              <input
                className="edit-input"
                value={(company[key] as string) || ''}
                disabled={contactsOnly && key !== 'company_name'}
                onChange={(e) =>
                  setCompany((prev) => ({ ...prev, [key]: e.target.value }))
                }
              />
            </label>
          ))}
        </div>

        <div style={{ marginTop: '0.75rem' }}>
          <strong>Duplicate Check</strong>
          {preview ? (
            <p className="queue-sub">
              {preview.company_match_type === 'new_company'
                ? 'New Company'
                : preview.company_match_type === 'existing_company'
                  ? `Existing Company: ${preview.matched_company_name} / Record No. ${preview.matched_external_record_no}`
                  : `Possible Match: ${preview.matched_company_name} / Record No. ${preview.matched_external_record_no}`}
              {preview.company_match_reasons.length
                ? ` · ${preview.company_match_reasons.join(', ')}`
                : ''}
            </p>
          ) : (
            <p className="queue-sub">Run preview to see matches.</p>
          )}
          {preview?.company_match_type === 'possible_match' ? (
            <div className="setup-actions">
              <button
                type="button"
                className="link-btn"
                onClick={() => setCompanyDecision('use_existing')}
              >
                Use Existing
              </button>
              <button
                type="button"
                className="link-btn"
                onClick={() => setCompanyDecision('create_new')}
              >
                Create New
              </button>
              <span className="queue-sub">Selected: {companyDecision || 'none'}</span>
            </div>
          ) : null}
          {(preview?.warnings || []).map((w) => (
            <p key={w} className="data-status data-status--error">
              {w}
            </p>
          ))}
          {preview?.relationship ? (
            <p className="queue-sub">{preview.relationship.message}</p>
          ) : null}
        </div>

        <h3 className="add-note-card__title" style={{ marginTop: '1rem' }}>
          Contacts
        </h3>
        <p className="queue-sub">
          Contacts passed from Research are selected. Uncheck any you do not want to add.
        </p>
        {rows.length === 0 ? (
          <p className="empty-state">No contacts in this review.</p>
        ) : (
          <ul className="ask-contact-list">
            {rows.map((row) => (
              <li key={row.index}>
                <label style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-start' }}>
                  <input
                    type="checkbox"
                    checked={row.selected}
                    onChange={(e) =>
                      setRows((prev) =>
                        prev.map((r) =>
                          r.index === row.index
                            ? { ...r, selected: e.target.checked }
                            : r,
                        ),
                      )
                    }
                  />
                  <span>
                    <strong>
                      {row.contact.full_name ||
                        `${row.contact.first_name || ''} ${row.contact.last_name || ''}`.trim() ||
                        row.contact.email ||
                        'Contact'}
                    </strong>
                    {row.contact.title ? ` — ${row.contact.title}` : ''}
                    {row.contact.email ? ` — ${row.contact.email}` : ''}
                    <div className="queue-sub">
                      {row.matchStatus === 'existing'
                        ? `Existing · ${row.matchedName}`
                        : row.matchStatus === 'possible_match'
                          ? `Possible Match · ${row.matchedName}`
                          : 'New'}
                      {row.reasons.length ? ` · ${row.reasons.join(', ')}` : ''}
                    </div>
                    {row.contact.linkedin || row.contact.source_url ? (
                      <div className="queue-sub">
                        {row.contact.linkedin ? (
                          <a href={row.contact.linkedin} target="_blank" rel="noreferrer">
                            LinkedIn profile
                          </a>
                        ) : null}
                        {row.contact.linkedin && row.contact.source_url
                          && row.contact.source_url !== row.contact.linkedin
                          ? ' · '
                          : null}
                        {row.contact.source_url &&
                        row.contact.source_url !== row.contact.linkedin ? (
                          <a href={row.contact.source_url} target="_blank" rel="noreferrer">
                            Source
                          </a>
                        ) : null}
                      </div>
                    ) : null}
                    {row.matchStatus === 'possible_match' && row.selected ? (
                      <div className="setup-actions">
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() =>
                            setRows((prev) =>
                              prev.map((r) =>
                                r.index === row.index
                                  ? { ...r, decision: 'use_existing' }
                                  : r,
                              ),
                            )
                          }
                        >
                          Use Existing
                        </button>
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() =>
                            setRows((prev) =>
                              prev.map((r) =>
                                r.index === row.index
                                  ? { ...r, decision: 'create_new' }
                                  : r,
                              ),
                            )
                          }
                        >
                          Create New
                        </button>
                        <span className="queue-sub">
                          Decision: {row.decision || 'none'}
                        </span>
                      </div>
                    ) : null}
                  </span>
                </label>
              </li>
            ))}
          </ul>
        )}

        <div className="setup-actions" style={{ marginTop: '1rem' }}>
          <button
            type="button"
            className="link-btn"
            disabled={loading || busy}
            onClick={() => void runPreview()}
          >
            Refresh Preview
          </button>
          <button
            type="button"
            className="primary-btn"
            disabled={busy || loading || !preview || selectedCount === 0}
            onClick={() => void onConfirm()}
          >
            {contactsOnly
              ? `Add Selected Contacts (${selectedCount})`
              : preview?.company_match_type === 'existing_company' ||
                  companyDecision === 'use_existing'
                ? `Add Contacts to Existing Company (${selectedCount})`
                : `Add Company + Selected Contacts (${selectedCount})`}
          </button>
          {preview?.matched_external_record_no || trustedExternalRecordNo ? (
            <button type="button" className="link-btn" onClick={openExisting}>
              Open Existing Record
            </button>
          ) : null}
          <button type="button" className="link-btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </section>
    </div>,
    document.body,
  )}
    </>
  )
}
