import { useState } from 'react'
import {
  downloadMasterDataExport,
  type MasterDataExportFormat,
  type MasterDataExportMode,
} from './api/masterDataExport'

export default function AdministrationMasterDataExport() {
  const [mode, setMode] = useState<MasterDataExportMode>('companies')
  const [fileFormat, setFileFormat] = useState<MasterDataExportFormat>('xlsx')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onDownload() {
    setBusy(true)
    setError(null)
    try {
      await downloadMasterDataExport({ mode, format: fileFormat })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not download export.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="administration-section" aria-labelledby="master-data-export-heading">
      <h2 id="master-data-export-heading">Master Data Export</h2>
      <p className="queue-sub">
        Exports the current NorthStar master database. Client-specific statuses, assignments and
        campaign memberships remain labeled by client.
      </p>

      <fieldset className="administration-import-fieldset" style={{ marginTop: '1rem' }}>
        <legend>Export</legend>
        <label className="setup-field">
          <span className="setup-field-label">
            <input
              type="radio"
              name="master-data-export-mode"
              value="companies"
              checked={mode === 'companies'}
              onChange={() => setMode('companies')}
            />{' '}
            Companies Only
          </span>
        </label>
        <label className="setup-field">
          <span className="setup-field-label">
            <input
              type="radio"
              name="master-data-export-mode"
              value="companies-contacts"
              checked={mode === 'companies-contacts'}
              onChange={() => setMode('companies-contacts')}
            />{' '}
            Companies + Contacts
          </span>
        </label>
      </fieldset>

      <fieldset className="administration-import-fieldset" style={{ marginTop: '0.85rem' }}>
        <legend>Format</legend>
        <label className="setup-field">
          <span className="setup-field-label">
            <input
              type="radio"
              name="master-data-export-format"
              value="xlsx"
              checked={fileFormat === 'xlsx'}
              onChange={() => setFileFormat('xlsx')}
            />{' '}
            Excel (.xlsx)
          </span>
        </label>
        <label className="setup-field">
          <span className="setup-field-label">
            <input
              type="radio"
              name="master-data-export-format"
              value="csv"
              checked={fileFormat === 'csv'}
              onChange={() => setFileFormat('csv')}
            />{' '}
            CSV
          </span>
        </label>
      </fieldset>

      {error ? (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      ) : null}

      <div style={{ marginTop: '1rem' }}>
        <button type="button" className="primary-btn" disabled={busy} onClick={() => void onDownload()}>
          {busy ? 'Downloading…' : 'Download Export'}
        </button>
      </div>
    </section>
  )
}
