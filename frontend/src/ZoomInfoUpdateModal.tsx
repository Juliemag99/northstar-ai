/** Review ZoomInfo values against NorthStar. Nothing is applied until the user chooses fields. */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  applyZoomInfoContact,
  previewZoomInfoContact,
  type ZoomInfoContactPreviewResponse,
  type ZoomInfoSnapshot,
} from './api/carmeco'
import { SELECT_CLIENT_FOR_WRITE, requireWriteClientId } from './writeClient'

export default function ZoomInfoUpdateModal({
  open,
  onClose,
  contactId,
  clientId,
  zoominfo,
  onApplied,
}: {
  open: boolean
  onClose: () => void
  contactId: number
  clientId: number | null
  zoominfo?: ZoomInfoSnapshot | null
  onApplied?: (message: string) => void
}) {
  const [preview, setPreview] = useState<ZoomInfoContactPreviewResponse | null>(null)
  const [selected, setSelected] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    setPreview(null)
    setSelected({})
    setError(null)
    let cancelled = false
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    void previewZoomInfoContact(contactId, {
      client_id: writeId,
      zoominfo: zoominfo ?? undefined,
    })
      .then((data) => {
        if (cancelled) return
        setPreview(data)
        const next: Record<string, boolean> = {}
        for (const field of data.fields) next[field.field] = false
        setSelected(next)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load ZoomInfo comparison.')
        }
      })
    return () => {
      cancelled = true
    }
  }, [open, contactId, clientId, zoominfo])

  if (!open) return null

  async function apply() {
    if (!preview?.available) return
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch (err) {
      setError(err instanceof Error ? err.message : SELECT_CLIENT_FOR_WRITE)
      return
    }
    const applyFields = Object.entries(selected)
      .filter(([, on]) => on)
      .map(([field]) => field)
    setBusy(true)
    setError(null)
    try {
      const result = await applyZoomInfoContact(contactId, {
        client_id: writeId,
        apply_fields: applyFields,
        zoominfo: zoominfo || {},
      })
      onApplied?.(result.message)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply ZoomInfo values.')
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
        aria-labelledby="zoominfo-update-title"
      >
        <h2 id="zoominfo-update-title">Update from ZoomInfo</h2>
        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : null}
        {preview && !preview.available ? (
          <p>{preview.status || 'ZoomInfo is not connected. Nothing was changed.'}</p>
        ) : null}
        {preview?.available ? (
          <>
            <p className="queue-source">
              Default is keep the current NorthStar value. Check a ZoomInfo value only when you want
              to apply it. Blank ZoomInfo values cannot replace populated fields.
            </p>
            {preview.different_company ? (
              <p className="data-status" role="status">
                ZoomInfo reports a different company ({preview.zoominfo_company_name}). The contact
                will not be moved automatically. Relink is a separate confirmed action.
              </p>
            ) : null}
            {preview.matches.length > 0 ? (
              <p className="data-status" role="status">
                Duplicate detection found {preview.matches.length} possible existing contact
                {preview.matches.length === 1 ? '' : 's'}. Identity fields that match another person
                will be rejected.
              </p>
            ) : null}
            <div className="queue-table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>NorthStar</th>
                    <th>ZoomInfo</th>
                    <th>Use ZoomInfo</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.fields.map((field) => (
                    <tr key={field.field}>
                      <td>{field.label}</td>
                      <td>{field.northstar_value || '—'}</td>
                      <td>{field.zoominfo_value || '—'}</td>
                      <td>
                        <input
                          type="checkbox"
                          checked={Boolean(selected[field.field])}
                          disabled={field.blank_zoominfo || field.applyable === false}
                          onChange={(e) =>
                            setSelected((prev) => ({ ...prev, [field.field]: e.target.checked }))
                          }
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
        <div className="edit-actions">
          {preview?.available ? (
            <button type="button" className="primary-btn" disabled={busy} onClick={() => void apply()}>
              {busy ? 'Saving…' : 'Apply selected fields'}
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
