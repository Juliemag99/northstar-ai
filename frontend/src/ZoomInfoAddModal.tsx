/** Add a ZoomInfo company or contact only after explicit duplicate review. Never auto-creates. */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import {
  companyWorkspaceHref,
  lookupCompaniesForClient,
  previewZoomInfoAdd,
  saveZoomInfoAdd,
  type CompanyLookupItem,
  type ZoomInfoAddPreviewResponse,
  type ZoomInfoSnapshot,
} from './api/carmeco'
import { SELECT_CLIENT_FOR_WRITE, requireWriteClientId } from './writeClient'

export default function ZoomInfoAddModal({
  open,
  onClose,
  clientId,
  clientName,
  kind,
  zoominfo,
  lockedCompanyId,
  lockedCompanyName,
}: {
  open: boolean
  onClose: () => void
  clientId: number | null
  clientName: string
  kind: 'company' | 'contact'
  zoominfo: ZoomInfoSnapshot
  lockedCompanyId?: number | null
  lockedCompanyName?: string | null
}) {
  const navigate = useNavigate()
  const [preview, setPreview] = useState<ZoomInfoAddPreviewResponse | null>(null)
  const [companyHits, setCompanyHits] = useState<CompanyLookupItem[]>([])
  const [companyQuery, setCompanyQuery] = useState('')
  const [selectedCompany, setSelectedCompany] = useState<CompanyLookupItem | null>(null)
  const [confirmDespite, setConfirmDespite] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    setPreview(null)
    setConfirmDespite(false)
    setError(null)
    if (lockedCompanyId && lockedCompanyId > 0) {
      setSelectedCompany({
        id: lockedCompanyId,
        company_name: lockedCompanyName || 'This company',
        external_record_no: '',
      })
    } else {
      setSelectedCompany(null)
    }
  }, [open, lockedCompanyId, lockedCompanyName])

  useEffect(() => {
    if (!open || kind !== 'contact' || selectedCompany || lockedCompanyId) return
    let cancelled = false
    const timer = window.setTimeout(() => {
      let writeId: number
      try {
        writeId = requireWriteClientId(clientId)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
        return
      }
      void lookupCompaniesForClient(writeId, companyQuery)
        .then((data) => {
          if (!cancelled) setCompanyHits(data.companies)
        })
        .catch((err) => {
          if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to search companies.')
        })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [open, kind, clientId, companyQuery, selectedCompany, lockedCompanyId])

  if (!open) return null

  async function runPreview() {
    setError(null)
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (kind === 'contact' && (!selectedCompany || selectedCompany.id <= 0)) {
      setError('Select which company should receive this ZoomInfo contact.')
      return
    }
    setBusy(true)
    try {
      const data = await previewZoomInfoAdd({
        client_id: writeId,
        company_id: kind === 'contact' ? selectedCompany?.id : null,
        zoominfo,
        kind,
      })
      setPreview(data)
      if (data.message) setError(data.message)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to check ZoomInfo duplicates.')
    } finally {
      setBusy(false)
    }
  }

  async function save(action: 'create' | 'link', existingId?: number) {
    setError(null)
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    setBusy(true)
    try {
      const result = await saveZoomInfoAdd({
        client_id: writeId,
        company_id: kind === 'contact' ? selectedCompany?.id : null,
        action,
        existing_company_id: kind === 'company' ? existingId : null,
        existing_contact_id: kind === 'contact' ? existingId : null,
        zoominfo,
        kind,
        confirm_create_despite_match: confirmDespite,
      })
      onClose()
      if (result.contact_id) {
        navigate(`/contacts/${result.contact_id}?client_id=${writeId}`, {
          state: { manualContactNotice: result.message },
        })
      } else if (result.external_record_no) {
        navigate(companyWorkspaceHref(result.external_record_no, writeId), {
          state: { companySaveNotice: result.message },
        })
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add ZoomInfo record.')
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <div className="contact-assign-overlay" role="presentation">
      <section
        className="panel contact-assign-modal add-contact-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="zoominfo-add-title"
      >
        <h2 id="zoominfo-add-title">Add to NorthStar</h2>
        <p>
          Working For: <strong>{clientName}</strong>. ZoomInfo records are not created automatically
          from search results.
        </p>
        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : null}
        {kind === 'contact' && !lockedCompanyId ? (
          <div className="edit-field">
            <span className="edit-field__label">Company for this contact</span>
            {selectedCompany ? (
              <p>
                {selectedCompany.company_name}{' '}
                <button type="button" className="link-btn" onClick={() => setSelectedCompany(null)}>
                  Change
                </button>
              </p>
            ) : (
              <>
                <input
                  className="edit-input"
                  value={companyQuery}
                  onChange={(e) => setCompanyQuery(e.target.value)}
                  placeholder="Search this client's companies"
                />
                <ul className="add-contact-company-hits">
                  {companyHits.map((hit) => (
                    <li key={hit.id}>
                      <button type="button" className="link-btn" onClick={() => setSelectedCompany(hit)}>
                        {hit.company_name}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        ) : null}
        {preview?.requires_create_confirmation ? (
          <label className="edit-field">
            <span>
              <input
                type="checkbox"
                checked={confirmDespite}
                onChange={(e) => setConfirmDespite(e.target.checked)}
              />{' '}
              Create a new record despite a possible match
            </span>
          </label>
        ) : null}
        {preview && preview.matches.length > 0 ? (
          <div className="add-contact-matches">
            <h3>Possible existing records</h3>
            <ul>
              {preview.matches.map((match) => {
                const id = Number(match.company_id || match.contact_id || 0)
                const label = String(match.company_name || match.first_name || 'Existing record')
                return (
                  <li key={`${preview.kind}-${id}`}>
                    <strong>{label}</strong>
                    <button
                      type="button"
                      className="primary-btn"
                      disabled={busy}
                      onClick={() => void save('link', id)}
                    >
                      Link Existing
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        ) : null}
        <div className="edit-actions">
          <button type="button" className="secondary-btn" disabled={busy} onClick={() => void runPreview()}>
            Check for duplicates
          </button>
          {preview?.can_create ? (
            <button
              type="button"
              className="primary-btn"
              disabled={busy || (preview.requires_create_confirmation && !confirmDespite)}
              onClick={() => void save('create')}
            >
              {busy ? 'Saving…' : 'Add to NorthStar'}
            </button>
          ) : null}
          <button type="button" className="secondary-btn" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      </section>
    </div>,
    document.body,
  )
}
