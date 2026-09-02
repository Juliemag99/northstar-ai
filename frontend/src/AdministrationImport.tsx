import { useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelCrmImport,
  fetchCrmImportRows,
  uploadCrmImport,
  type CrmImportBatch,
  type CrmImportRow,
} from './api/crmImport'

type AssignedClient = {
  client_id: number
  client_name: string
  client_code?: string
}

type AdministrationImportProps = {
  allMyClients: boolean
  connectClientId: number | null
  scopedClientId: number
  availableClients: AssignedClient[]
  onScopedClientId: (clientId: number) => void
  clientName: string
}

const PAGE_SIZE = 25

function clientLabel(
  clientId: number,
  availableClients: AssignedClient[],
): string {
  return (
    availableClients.find((c) => c.client_id === clientId)?.client_name ||
    `Client ${clientId}`
  )
}

export default function AdministrationImport({
  allMyClients,
  connectClientId,
  scopedClientId,
  availableClients,
  onScopedClientId,
  clientName,
}: AdministrationImportProps) {
  const [heldFile, setHeldFile] = useState<File | null>(null)
  const [visibleSheets, setVisibleSheets] = useState<string[]>([])
  const [worksheet, setWorksheet] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [batch, setBatch] = useState<CrmImportBatch | null>(null)
  const [pageRows, setPageRows] = useState<CrmImportRow[]>([])
  const [pageOffset, setPageOffset] = useState(0)
  const [pageTotal, setPageTotal] = useState(0)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const canUpload = connectClientId != null && connectClientId > 0
  const previewHeaders = batch?.headers || []

  const heldFileName = useMemo(() => heldFile?.name || '', [heldFile])

  useEffect(() => {
    setBatch(null)
    setPageRows([])
    setPageOffset(0)
    setPageTotal(0)
    setVisibleSheets([])
    setWorksheet('')
    setError(null)
    setMessage(null)
  }, [connectClientId])

  function onPickFile(file: File | null) {
    setHeldFile(file)
    setVisibleSheets([])
    setWorksheet('')
    setBatch(null)
    setPageRows([])
    setPageOffset(0)
    setPageTotal(0)
    setError(null)
    setMessage(null)
  }

  async function loadPage(next: CrmImportBatch, offset: number) {
    if (!next.reusable) {
      setPageRows([])
      setPageOffset(0)
      setPageTotal(0)
      return
    }
    const page = await fetchCrmImportRows(next.client_id, next.batch_id, offset, PAGE_SIZE)
    setPageRows(page.rows)
    setPageOffset(page.offset)
    setPageTotal(page.total)
  }

  async function submitHeldFile(sheetName: string) {
    if (!canUpload || connectClientId == null) {
      setError('Choose a specific client before uploading.')
      return
    }
    if (!heldFile) {
      setError('Choose a CSV or XLSX file to upload.')
      return
    }
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      const result = await uploadCrmImport(connectClientId, heldFile, sheetName)
      if (result.needs_worksheet) {
        setVisibleSheets(result.visible_sheets)
        setWorksheet(result.visible_sheets[0] || '')
        setBatch(null)
        setPageRows([])
        setMessage(result.message || 'Choose one visible sheet to continue.')
        return
      }
      setVisibleSheets([])
      if (result.kind === 'failed' || !result.batch) {
        setBatch(result.batch)
        setPageRows([])
        setError(result.message || result.batch?.error_message || 'The spreadsheet could not be read.')
        return
      }
      setBatch(result.batch)
      setMessage(result.message || 'Preview ready.')
      await loadPage(result.batch, 0)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed.')
    } finally {
      setBusy(false)
    }
  }

  async function onCancelBatch() {
    if (!batch || !canUpload || connectClientId == null) return
    const ok = window.confirm(
      `Cancel this staged import for ${heldFileName || batch.original_filename}? Staged rows will be removed. The audit record is kept.`,
    )
    if (!ok) return
    setBusy(true)
    setError(null)
    try {
      const cancelled = await cancelCrmImport(connectClientId, batch.batch_id)
      setBatch(cancelled)
      setPageRows([])
      setPageOffset(0)
      setPageTotal(0)
      setMessage('Import cancelled. Staged rows were removed. The batch record was kept for audit.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Cancel failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="administration-section" aria-labelledby="crm-import-heading">
      <h2 id="crm-import-heading">Company & contact import</h2>
      <p className="queue-sub">
        Upload a CSV or XLSX file for a read-only staging preview. Nothing is written to
        companies, contacts, relationships, workflows, activities, or notes until a later
        import step.
      </p>

      {allMyClients ? (
        <label className="setup-field" style={{ maxWidth: '24rem', margin: '0.85rem 0' }}>
          <span className="setup-field-label">Client</span>
          <select
            value={scopedClientId > 0 ? String(scopedClientId) : ''}
            onChange={(e) => onScopedClientId(Number(e.target.value) || 0)}
            aria-label="Client for company and contact import"
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
        <p className="queue-sub" style={{ margin: '0.85rem 0' }}>
          Active Client: <strong>{clientName || clientLabel(connectClientId || 0, availableClients)}</strong>
        </p>
      )}

      {allMyClients && !canUpload ? (
        <p className="data-status" role="status">
          All My Clients is selected. Choose a specific client before uploading.
        </p>
      ) : null}

      {canUpload ? (
        <div className="administration-import-controls">
          <label className="setup-field">
            <span className="setup-field-label">Spreadsheet</span>
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(e) => onPickFile(e.target.files?.[0] || null)}
              aria-label="Company and contact spreadsheet"
            />
          </label>
          {heldFileName ? (
            <p className="queue-sub">Selected file: {heldFileName}</p>
          ) : null}
          {visibleSheets.length > 0 ? (
            <label className="setup-field" style={{ maxWidth: '24rem' }}>
              <span className="setup-field-label">Worksheet</span>
              <select
                value={worksheet}
                onChange={(e) => setWorksheet(e.target.value)}
                aria-label="Visible worksheet"
              >
                {visibleSheets.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <div className="setup-actions">
            <button
              type="button"
              className="primary-btn"
              disabled={busy || !heldFile}
              onClick={() => void submitHeldFile(visibleSheets.length > 0 ? worksheet : '')}
            >
              {visibleSheets.length > 0 ? 'Continue with selected sheet' : 'Upload for preview'}
            </button>
            {batch?.reusable ? (
              <button
                type="button"
                className="link-btn"
                disabled={busy}
                onClick={() => void onCancelBatch()}
              >
                Cancel staged import
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {message ? (
        <p className="status-banner" role="status">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      ) : null}

      {batch ? (
        <div className="administration-import-preview">
          <h3>Staging preview</h3>
          <dl className="administration-import-meta">
            <div>
              <dt>File</dt>
              <dd>{batch.original_filename}</dd>
            </div>
            <div>
              <dt>Type</dt>
              <dd>{batch.file_type}</dd>
            </div>
            <div>
              <dt>Worksheet</dt>
              <dd>{batch.worksheet_name || '—'}</dd>
            </div>
            <div>
              <dt>Size</dt>
              <dd>{batch.file_size_bytes} bytes</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{batch.status}</dd>
            </div>
            <div>
              <dt>Rows</dt>
              <dd>
                {batch.source_row_count} staged / {batch.total_rows} source
                {batch.blank_row_count ? ` / ${batch.blank_row_count} blank` : ''}
                {batch.error_row_count ? ` / ${batch.error_row_count} with errors` : ''}
              </dd>
            </div>
            <div>
              <dt>Expires</dt>
              <dd>{batch.expires_at || '—'}</dd>
            </div>
            <div>
              <dt>Uploaded by</dt>
              <dd>{batch.uploaded_by_name || '—'}</dd>
            </div>
          </dl>
          {batch.error_message ? (
            <p className="data-status data-status--error" role="alert">
              {batch.error_message}
            </p>
          ) : null}
          {batch.warnings.length > 0 ? (
            <ul className="administration-import-warnings">
              {batch.warnings.map((warning, index) => (
                <li key={`${index}-${warning}`}>{warning}</li>
              ))}
            </ul>
          ) : null}
          {batch.reusable && previewHeaders.length > 0 ? (
            <>
              <div className="queue-table-wrap administration-import-table">
                <table className="queue-table">
                  <thead>
                    <tr>
                      <th>Source row</th>
                      {previewHeaders.map((header) => (
                        <th key={header}>{header}</th>
                      ))}
                      <th>Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pageRows.map((row) => (
                      <tr key={row.row_id || row.source_row_number}>
                        <td>{row.source_row_number}</td>
                        {previewHeaders.map((header) => (
                          <td key={header}>{row.values[header] || ''}</td>
                        ))}
                        <td>
                          {row.errors.map((item) => (
                            <div key={item} className="data-status data-status--error">
                              {item}
                            </div>
                          ))}
                          {row.warnings.map((item) => (
                            <div key={item}>{item}</div>
                          ))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {pageTotal > PAGE_SIZE ? (
                <div className="setup-actions">
                  <button
                    type="button"
                    className="link-btn"
                    disabled={busy || pageOffset <= 0}
                    onClick={() => void loadPage(batch, Math.max(0, pageOffset - PAGE_SIZE))}
                  >
                    Previous
                  </button>
                  <span className="queue-sub">
                    {pageOffset + 1}–{Math.min(pageOffset + pageRows.length, pageTotal)} of {pageTotal}
                  </span>
                  <button
                    type="button"
                    className="link-btn"
                    disabled={busy || pageOffset + pageRows.length >= pageTotal}
                    onClick={() => void loadPage(batch, pageOffset + PAGE_SIZE)}
                  >
                    Next
                  </button>
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
