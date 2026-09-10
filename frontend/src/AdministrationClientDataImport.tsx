import { useEffect, useMemo, useRef, useState } from 'react'
import {
  batchIdOf,
  cancelClientDataImport,
  confirmClientDataImport,
  countValue,
  dryRunClientDataImport,
  historyCountsOf,
  historyMappingOf,
  planReadyToConfirm,
  prospectsCountsOf,
  prospectsMappingOf,
  retryClientDataImportStagingCleanup,
  saveClientDataImportMapping,
  uploadClientDataImport,
  type ClientDataImportBatch,
  type ClientDataImportConfirmResponse,
  type ClientDataImportDryRunResponse,
} from './api/clientDataImport'
import {
  companyFields,
  contactFields,
  historyFields,
  mappingsEqual,
  normalizeHistoryMapping,
  normalizeProspectsMapping,
  relationshipFields,
  suggestMapping,
  validateHistoryMapping,
  validateProspectsMapping,
} from './clientDataImportMapping'

type AssignedClient = {
  client_id: number
  client_name: string
  client_code?: string
}

type AdministrationClientDataImportProps = {
  allMyClients: boolean
  connectClientId: number | null
  scopedClientId: number
  availableClients: AssignedClient[]
  onScopedClientId: (clientId: number) => void
  clientName: string
}

const CLOSED_POLICY_DEFAULT = [
  'Closed means closed/not-fit for this selected client.',
  'Those records are excluded from this client import by default.',
  'Closed is not treated as deletion from the shared company master.',
].join(' ')

function clientLabel(
  clientId: number,
  availableClients: AssignedClient[],
): string {
  return (
    availableClients.find((c) => c.client_id === clientId)?.client_name ||
    `Client ${clientId}`
  )
}

function errorStatus(err: unknown): number | undefined {
  if (err && typeof err === 'object' && 'status' in err) {
    const status = (err as { status?: unknown }).status
    return typeof status === 'number' ? status : undefined
  }
  return undefined
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message.trim() ? err.message : fallback
}

function isTerminalConflictDetail(detail: string): boolean {
  const text = detail.toLowerCase()
  return (
    text.includes('imported') ||
    text.includes('cancelled') ||
    text.includes('canceled') ||
    text.includes('expired') ||
    text.includes('can no longer')
  )
}

function closedPolicyText(batch: ClientDataImportBatch | null, plan: ClientDataImportDryRunResponse | null): string {
  return (
    plan?.closed_policy_notes ||
    plan?.closed_policy_note ||
    plan?.closed_policy ||
    batch?.closed_policy_notes ||
    batch?.closed_policy_note ||
    batch?.closed_policy ||
    CLOSED_POLICY_DEFAULT
  )
}

function needsStagingCleanupRetry(
  batch: ClientDataImportBatch | null,
  result: ClientDataImportConfirmResponse | null,
): boolean {
  const status = String(
    result?.staging_cleanup_status || batch?.staging_cleanup_status || '',
  ).toLowerCase()
  const error = String(result?.staging_cleanup_error || batch?.staging_cleanup_error || '')
  return Boolean(error) || status.includes('fail') || status.includes('pending') || status.includes('retry')
}

export default function AdministrationClientDataImport({
  allMyClients,
  connectClientId,
  scopedClientId,
  availableClients,
  onScopedClientId,
  clientName,
}: AdministrationClientDataImportProps) {
  const [heldProspectsFile, setHeldProspectsFile] = useState<File | null>(null)
  const [heldHistoryFile, setHeldHistoryFile] = useState<File | null>(null)
  const [visibleSheets, setVisibleSheets] = useState<string[]>([])
  const [worksheet, setWorksheet] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [batch, setBatch] = useState<ClientDataImportBatch | null>(null)

  const [savedProspectsMapping, setSavedProspectsMapping] = useState<Record<string, string>>({})
  const [draftProspectsMapping, setDraftProspectsMapping] = useState<Record<string, string>>({})
  const [savedHistoryMapping, setSavedHistoryMapping] = useState<Record<string, string>>({})
  const [draftHistoryMapping, setDraftHistoryMapping] = useState<Record<string, string>>({})
  const [mappingErrors, setMappingErrors] = useState<string[]>([])
  const [mappingSaving, setMappingSaving] = useState(false)
  const [mappingSaveError, setMappingSaveError] = useState<string | null>(null)
  const [mappingSaveSuccess, setMappingSaveSuccess] = useState<string | null>(null)

  const [dryRun, setDryRun] = useState<ClientDataImportDryRunResponse | null>(null)
  const [dryRunLoading, setDryRunLoading] = useState(false)
  const [dryRunError, setDryRunError] = useState<string | null>(null)
  const [terminalLocked, setTerminalLocked] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [confirmResult, setConfirmResult] = useState<ClientDataImportConfirmResponse | null>(null)
  const [cleanupBusy, setCleanupBusy] = useState(false)
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null)
  const confirmCancelRef = useRef<HTMLButtonElement | null>(null)
  const confirmingRef = useRef(false)

  const canUpload = connectClientId != null && connectClientId > 0
  const prospectsHeaders = useMemo(() => batch?.headers || [], [batch?.headers])
  const historyHeaders = useMemo(() => batch?.history_headers || [], [batch?.history_headers])
  const hasHistory = historyHeaders.length > 0 || Boolean(batch?.history_filename)
  const activeClientDisplay =
    clientName || clientLabel(connectClientId || 0, availableClients)

  const draftProspectsNormalized = useMemo(
    () => normalizeProspectsMapping(draftProspectsMapping),
    [draftProspectsMapping],
  )
  const savedProspectsNormalized = useMemo(
    () => normalizeProspectsMapping(savedProspectsMapping),
    [savedProspectsMapping],
  )
  const draftHistoryNormalized = useMemo(
    () => normalizeHistoryMapping(draftHistoryMapping),
    [draftHistoryMapping],
  )
  const savedHistoryNormalized = useMemo(
    () => normalizeHistoryMapping(savedHistoryMapping),
    [savedHistoryMapping],
  )

  const prospectsDirty = !mappingsEqual(
    draftProspectsNormalized,
    savedProspectsNormalized,
    'prospects',
  )
  const historyDirty = !mappingsEqual(draftHistoryNormalized, savedHistoryNormalized, 'history')
  const mappingDirty = prospectsDirty || historyDirty

  const localProspectsValidation = useMemo(
    () => validateProspectsMapping(draftProspectsNormalized, prospectsHeaders),
    [draftProspectsNormalized, prospectsHeaders],
  )
  const localHistoryValidation = useMemo(
    () => validateHistoryMapping(draftHistoryNormalized, historyHeaders),
    [draftHistoryNormalized, historyHeaders],
  )
  const localValidationOk = localProspectsValidation.ok && localHistoryValidation.ok
  const localValidationErrors = [
    ...localProspectsValidation.errors,
    ...localHistoryValidation.errors,
  ]

  const savedProspectsValidation = useMemo(
    () => validateProspectsMapping(savedProspectsNormalized, prospectsHeaders),
    [savedProspectsNormalized, prospectsHeaders],
  )
  const savedHistoryValidation = useMemo(
    () => validateHistoryMapping(savedHistoryNormalized, historyHeaders),
    [savedHistoryNormalized, historyHeaders],
  )
  const savedValidationOk = savedProspectsValidation.ok && savedHistoryValidation.ok

  const hasSavedMapping = Object.keys(savedProspectsNormalized).length > 0
  const reusable = Boolean(batch?.reusable !== false && batch?.status === 'previewed' && !terminalLocked)
  const importedComplete = Boolean(confirmResult) || batch?.status === 'imported'

  const mappingStatusLabel = !batch
    ? 'Not started'
    : importedComplete
      ? 'Saved'
      : !hasSavedMapping
        ? 'Not saved'
        : mappingDirty
          ? 'Unsaved changes'
          : 'Saved'

  const dryRunStatusLabel = confirming
    ? 'Importing…'
    : importedComplete
      ? 'Imported'
      : dryRunLoading
        ? 'Running'
        : dryRunError
          ? 'Error'
          : !dryRun
            ? 'Not run'
            : planReadyToConfirm(dryRun)
              ? 'Ready to confirm'
              : 'Needs review'

  const requestActive = busy || mappingSaving || dryRunLoading || confirming || cleanupBusy

  const canSaveMapping =
    reusable && mappingDirty && localValidationOk && !requestActive

  const canDryRun =
    reusable && hasSavedMapping && !mappingDirty && localValidationOk && !requestActive

  const canConfirm =
    reusable &&
    hasSavedMapping &&
    savedValidationOk &&
    !mappingDirty &&
    planReadyToConfirm(dryRun) &&
    !requestActive

  const prospectsCounts = prospectsCountsOf(dryRun)
  const historyCounts = historyCountsOf(dryRun)

  function clearConfirmUi() {
    setConfirmOpen(false)
    setConfirmError(null)
    confirmingRef.current = false
    setConfirming(false)
  }

  function clearDryRunOnly() {
    setDryRun(null)
    setDryRunError(null)
    clearConfirmUi()
  }

  function clearMappingAndPlan() {
    setSavedProspectsMapping({})
    setDraftProspectsMapping({})
    setSavedHistoryMapping({})
    setDraftHistoryMapping({})
    setMappingErrors([])
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    setConfirmResult(null)
    clearDryRunOnly()
  }

  function applyBatchMapping(next: ClientDataImportBatch, options: { suggestIfEmpty: boolean }) {
    const savedProspects = normalizeProspectsMapping(prospectsMappingOf(next))
    const savedHistory = normalizeHistoryMapping(historyMappingOf(next))
    setSavedProspectsMapping(savedProspects)
    setSavedHistoryMapping(savedHistory)

    if (Object.keys(savedProspects).length > 0 || Object.keys(savedHistory).length > 0) {
      setDraftProspectsMapping(savedProspects)
      setDraftHistoryMapping(savedHistory)
    } else if (options.suggestIfEmpty && next.reusable !== false) {
      const suggested = suggestMapping(next.headers || [], next.history_headers || [])
      setDraftProspectsMapping(suggested.prospects)
      setDraftHistoryMapping(suggested.history)
    } else {
      setDraftProspectsMapping({})
      setDraftHistoryMapping({})
    }
    setMappingErrors([])
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    clearDryRunOnly()
  }

  useEffect(() => {
    setBatch(null)
    setVisibleSheets([])
    setWorksheet('')
    setError(null)
    setMessage(null)
    setHeldProspectsFile(null)
    setHeldHistoryFile(null)
    setTerminalLocked(false)
    setSavedProspectsMapping({})
    setDraftProspectsMapping({})
    setSavedHistoryMapping({})
    setDraftHistoryMapping({})
    setMappingErrors([])
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    setDryRun(null)
    setDryRunError(null)
    setConfirmResult(null)
    setConfirmOpen(false)
    setConfirmError(null)
    confirmingRef.current = false
    setConfirming(false)
  }, [connectClientId])

  function onPickProspectsFile(file: File | null) {
    setHeldProspectsFile(file)
    setVisibleSheets([])
    setWorksheet('')
    setBatch(null)
    setError(null)
    setMessage(null)
    setTerminalLocked(false)
    clearMappingAndPlan()
  }

  function onPickHistoryFile(file: File | null) {
    setHeldHistoryFile(file)
    setBatch(null)
    setError(null)
    setMessage(null)
    setTerminalLocked(false)
    clearMappingAndPlan()
  }

  function onProspectsDraftChange(field: string, header: string) {
    setDraftProspectsMapping((prev) => {
      const next = { ...prev }
      if (!header) delete next[field]
      else next[field] = header
      return next
    })
    setMappingSaveSuccess(null)
    setMappingSaveError(null)
    clearDryRunOnly()
  }

  function onHistoryDraftChange(field: string, header: string) {
    setDraftHistoryMapping((prev) => {
      const next = { ...prev }
      if (!header) delete next[field]
      else next[field] = header
      return next
    })
    setMappingSaveSuccess(null)
    setMappingSaveError(null)
    clearDryRunOnly()
  }

  async function submitHeldFiles(sheetName: string) {
    if (!canUpload || connectClientId == null || connectClientId <= 0) {
      setError('Choose a specific client before uploading.')
      return
    }
    if (!heldProspectsFile) {
      setError('Choose a prospects CSV or XLSX file to upload.')
      return
    }
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      const result = await uploadClientDataImport(
        connectClientId,
        heldProspectsFile,
        heldHistoryFile,
        sheetName,
      )
      if (result.needs_worksheet) {
        setVisibleSheets(result.visible_sheets || [])
        setWorksheet(result.visible_sheets?.[0] || '')
        setBatch(null)
        clearMappingAndPlan()
        setMessage(result.message || 'Choose one visible sheet to continue.')
        return
      }
      setVisibleSheets([])
      if (result.kind === 'failed' || !result.batch) {
        setBatch(result.batch || null)
        clearMappingAndPlan()
        setError(
          result.message ||
            result.batch?.error_message ||
            'The spreadsheet could not be read.',
        )
        return
      }
      setBatch(result.batch)
      setTerminalLocked(false)
      setMessage(result.message || 'Preview ready.')
      applyBatchMapping(result.batch, { suggestIfEmpty: true })
    } catch (err) {
      setError(errorMessage(err, 'Upload failed.'))
    } finally {
      setBusy(false)
    }
  }

  async function onCancelBatch() {
    if (!batch || !canUpload || connectClientId == null || connectClientId <= 0) return
    if (!reusable) return
    const id = batchIdOf(batch)
    if (id <= 0) return
    const ok = window.confirm(
      `Cancel this staged client data import for ${
        heldProspectsFile?.name || batch.original_filename || 'this batch'
      }? Staged rows will be removed. The audit record is kept.`,
    )
    if (!ok) return
    setBusy(true)
    setError(null)
    try {
      const cancelled = await cancelClientDataImport(connectClientId, id)
      setBatch(cancelled)
      clearMappingAndPlan()
      setMessage('Import cancelled. Staged rows were removed. The batch record was kept for audit.')
    } catch (err) {
      const status = errorStatus(err)
      if (status === 409) {
        setTerminalLocked(true)
        clearDryRunOnly()
      }
      setError(errorMessage(err, 'Cancel failed.'))
    } finally {
      setBusy(false)
    }
  }

  async function onSaveMapping() {
    if (!batch || !canUpload || connectClientId == null || connectClientId <= 0) return
    if (!reusable) return
    const id = batchIdOf(batch)
    if (id <= 0) return
    const prospectsValidation = validateProspectsMapping(
      draftProspectsNormalized,
      prospectsHeaders,
    )
    const historyValidation = validateHistoryMapping(draftHistoryNormalized, historyHeaders)
    const errors = [...prospectsValidation.errors, ...historyValidation.errors]
    setMappingErrors(errors)
    if (!prospectsValidation.ok || !historyValidation.ok || !canSaveMapping) return
    setMappingSaving(true)
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    setError(null)
    try {
      const updated = await saveClientDataImportMapping(
        connectClientId,
        id,
        draftProspectsNormalized,
        historyHeaders.length ? draftHistoryNormalized : undefined,
      )
      setBatch(updated)
      setSavedProspectsMapping(normalizeProspectsMapping(prospectsMappingOf(updated)))
      setDraftProspectsMapping(normalizeProspectsMapping(prospectsMappingOf(updated)))
      setSavedHistoryMapping(normalizeHistoryMapping(historyMappingOf(updated)))
      setDraftHistoryMapping(normalizeHistoryMapping(historyMappingOf(updated)))
      setMappingErrors([])
      clearDryRunOnly()
      setMappingSaveSuccess('Mapping saved.')
      setMessage(null)
    } catch (err) {
      const status = errorStatus(err)
      const detail = errorMessage(err, 'Could not save mapping.')
      if (status === 409) {
        setTerminalLocked(true)
        clearDryRunOnly()
        setMappingSaveError(detail)
      } else if (status === 404) {
        setTerminalLocked(true)
        setMappingSaveError('Import batch not found for this client.')
      } else if (status === 403) {
        setMappingSaveError('You do not have permission to save this mapping.')
      } else {
        setMappingSaveError(detail)
      }
    } finally {
      setMappingSaving(false)
    }
  }

  async function runDryRun() {
    if (!batch || !canUpload || connectClientId == null || connectClientId <= 0) return
    if (!reusable) return
    const id = batchIdOf(batch)
    if (id <= 0) return
    if (!hasSavedMapping || mappingDirty) {
      setDryRunError('Save the current mapping before running a dry run.')
      return
    }
    setDryRunLoading(true)
    setDryRunError(null)
    setError(null)
    try {
      const plan = await dryRunClientDataImport(connectClientId, id)
      setDryRun(plan)
    } catch (err) {
      const status = errorStatus(err)
      const detail = errorMessage(err, 'Dry run failed.')
      if (status === 409) {
        clearDryRunOnly()
        setTerminalLocked(batch.status !== 'previewed')
        setDryRunError(detail)
      } else if (status === 404) {
        setTerminalLocked(true)
        clearDryRunOnly()
        setDryRunError('Import batch not found for this client.')
      } else if (status === 403) {
        setDryRunError('You do not have permission to run this dry run.')
      } else if (status === 400) {
        setDryRunError(detail)
      } else if (status === 500) {
        setDryRunError('Unexpected failure. Staged rows were not changed.')
      } else {
        setDryRunError(detail)
      }
    } finally {
      setDryRunLoading(false)
    }
  }

  function closeConfirmDialog() {
    if (confirmingRef.current) return
    setConfirmOpen(false)
    setConfirmError(null)
  }

  function openConfirmDialog() {
    if (!canConfirm || !dryRun) return
    setConfirmError(null)
    setConfirmOpen(true)
  }

  useEffect(() => {
    if (!confirmOpen) return
    confirmCancelRef.current?.focus()
    function onKey(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      if (confirmingRef.current) return
      event.preventDefault()
      setConfirmOpen(false)
      setConfirmError(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [confirmOpen])

  const wasConfirmOpenRef = useRef(false)
  useEffect(() => {
    if (confirmOpen) {
      wasConfirmOpenRef.current = true
      return
    }
    if (wasConfirmOpenRef.current) {
      wasConfirmOpenRef.current = false
      confirmButtonRef.current?.focus()
    }
  }, [confirmOpen])

  async function onConfirmImport() {
    if (
      !batch ||
      !dryRun ||
      !canUpload ||
      connectClientId == null ||
      connectClientId <= 0
    ) {
      return
    }
    if (confirmingRef.current) return
    const id = batchIdOf(batch)
    if (id <= 0) return
    const stillEligible =
      reusable &&
      hasSavedMapping &&
      savedValidationOk &&
      !mappingDirty &&
      planReadyToConfirm(dryRun) &&
      !busy &&
      !mappingSaving &&
      !dryRunLoading
    if (!stillEligible) {
      setConfirmError('This batch is no longer ready to confirm. Run Dry Run again.')
      return
    }
    confirmingRef.current = true
    setConfirming(true)
    setConfirmError(null)
    setError(null)
    try {
      const result = await confirmClientDataImport(
        connectClientId,
        id,
        dryRun.plan_fingerprint,
      )
      setConfirmResult(result)
      setBatch((prev) =>
        prev
          ? {
              ...prev,
              status: result.status || 'imported',
              reusable: false,
              staging_cleanup_status: result.staging_cleanup_status,
              staging_cleanup_error: result.staging_cleanup_error,
            }
          : prev,
      )
      setDryRun(null)
      setDryRunError(null)
      setConfirmOpen(false)
      setMessage('Import completed.')
    } catch (err) {
      const status = errorStatus(err)
      const detail = errorMessage(err, 'Import confirmation failed.')
      if (status === 409) {
        clearDryRunOnly()
        if (isTerminalConflictDetail(detail)) {
          setTerminalLocked(true)
          setError(detail)
        } else {
          setDryRunError(`${detail} Run Dry Run again before confirming.`)
          setError(null)
        }
        setConfirmOpen(false)
      } else if (status === 404) {
        setTerminalLocked(true)
        clearDryRunOnly()
        setConfirmOpen(false)
        setError('Import batch not found for this client.')
      } else if (status === 403) {
        setConfirmError('You do not have permission to confirm this import.')
      } else if (status === 400) {
        setConfirmError(detail)
      } else if (status === 500) {
        setConfirmError('Unexpected failure. Nothing was imported. You can safely retry.')
      } else {
        setConfirmError(detail)
      }
    } finally {
      confirmingRef.current = false
      setConfirming(false)
    }
  }

  async function onRetryCleanup() {
    if (!batch || !canUpload || connectClientId == null || connectClientId <= 0) return
    const id = batchIdOf(batch)
    if (id <= 0) return
    setCleanupBusy(true)
    setError(null)
    try {
      const updated = await retryClientDataImportStagingCleanup(connectClientId, id)
      if ('status' in updated) {
        setBatch((prev) =>
          prev
            ? {
                ...prev,
                ...updated,
                status: String(updated.status || prev.status),
              }
            : (updated as ClientDataImportBatch),
        )
      }
      if ('staging_cleanup_status' in updated || 'staging_cleanup_error' in updated) {
        setConfirmResult((prev) =>
          prev
            ? {
                ...prev,
                staging_cleanup_status: updated.staging_cleanup_status,
                staging_cleanup_error: updated.staging_cleanup_error,
              }
            : prev,
        )
      }
      setMessage('Staging cleanup retry completed.')
    } catch (err) {
      setError(errorMessage(err, 'Staging cleanup retry failed.'))
    } finally {
      setCleanupBusy(false)
    }
  }

  const usedProspectsHeaders = useMemo(() => {
    const used = new Set<string>()
    for (const value of Object.values(draftProspectsNormalized)) {
      if (value) used.add(value)
    }
    return used
  }, [draftProspectsNormalized])

  const usedHistoryHeaders = useMemo(() => {
    const used = new Set<string>()
    for (const value of Object.values(draftHistoryNormalized)) {
      if (value) used.add(value)
    }
    return used
  }, [draftHistoryNormalized])

  const allReady = planReadyToConfirm(dryRun)
  const closedExcluded = dryRun?.closed_excluded_count ?? 0

  const terminalMessage = (() => {
    if (!batch) return null
    if (confirmResult) return null
    if (batch.status === 'imported') {
      return 'This batch was imported. Mapping and dry-run are no longer available.'
    }
    if (batch.status === 'cancelled') {
      return 'This batch was cancelled. Upload new files to continue.'
    }
    if (batch.status === 'expired') {
      return 'This batch expired. Upload new files to continue.'
    }
    if (batch.status === 'failed') {
      return 'This batch failed during upload. Upload new files to continue.'
    }
    if (terminalLocked || batch.reusable === false) {
      return 'This import batch can no longer be previewed.'
    }
    return null
  })()

  function renderMappingSelect(
    fieldKey: string,
    label: string,
    required: boolean | undefined,
    kind: 'prospects' | 'history',
  ) {
    const draft =
      kind === 'prospects' ? draftProspectsNormalized : draftHistoryNormalized
    const used = kind === 'prospects' ? usedProspectsHeaders : usedHistoryHeaders
    const headers = kind === 'prospects' ? prospectsHeaders : historyHeaders
    const onChange = kind === 'prospects' ? onProspectsDraftChange : onHistoryDraftChange
    const aria = `${label}${required ? ' *' : ''}`
    return (
      <label key={fieldKey} className="setup-field">
        <span className="setup-field-label">
          {label}
          {required ? ' *' : ''}
        </span>
        <select
          aria-label={aria}
          value={draft[fieldKey] || ''}
          onChange={(e) => onChange(fieldKey, e.target.value)}
          disabled={mappingSaving || dryRunLoading || confirming}
        >
          <option value="">Not mapped</option>
          {headers.map((header) => {
            const taken = used.has(header) && draft[fieldKey] !== header
            return (
              <option key={header} value={header} disabled={taken}>
                {header}
              </option>
            )
          })}
        </select>
      </label>
    )
  }

  return (
    <section className="administration-section" aria-labelledby="client-data-import-heading">
      <h2 id="client-data-import-heading">Client Data Import</h2>
      <p className="queue-sub">
        Upload prospects (and optional history), map columns, review Closed exclusions, and run a
        read-only dry run. Nothing is written until confirmation.
      </p>

      {allMyClients ? (
        <label className="setup-field" style={{ maxWidth: '24rem', margin: '0.85rem 0' }}>
          <span className="setup-field-label">Client</span>
          <select
            value={scopedClientId > 0 ? String(scopedClientId) : ''}
            onChange={(e) => onScopedClientId(Number(e.target.value) || 0)}
            aria-label="Client for client data import"
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
          Active Client: <strong>{activeClientDisplay}</strong>
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
            <span className="setup-field-label">Prospects spreadsheet</span>
            <input
              type="file"
              accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(e) => onPickProspectsFile(e.target.files?.[0] || null)}
              aria-label="Prospects spreadsheet"
            />
          </label>
          {heldProspectsFile ? (
            <p className="queue-sub">Selected prospects file: {heldProspectsFile.name}</p>
          ) : null}
          <label className="setup-field">
            <span className="setup-field-label">History CSV (optional)</span>
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => onPickHistoryFile(e.target.files?.[0] || null)}
              aria-label="History spreadsheet"
            />
          </label>
          {heldHistoryFile ? (
            <p className="queue-sub">Selected history file: {heldHistoryFile.name}</p>
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
              disabled={busy || !heldProspectsFile}
              onClick={() => void submitHeldFiles(visibleSheets.length > 0 ? worksheet : '')}
            >
              {visibleSheets.length > 0 ? 'Continue with selected sheet' : 'Upload for preview'}
            </button>
            {reusable ? (
              <button
                type="button"
                className="link-btn"
                disabled={requestActive}
                onClick={() => void onCancelBatch()}
              >
                Cancel staged import
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {batch ? (
        <div
          className="administration-import-sticky"
          role="region"
          aria-label="Client data import status and actions"
        >
          <div className="administration-import-sticky-meta">
            <span>
              Client: <strong>{activeClientDisplay}</strong>
            </span>
            <span>Mapping: {mappingStatusLabel}</span>
            <span>Dry run: {dryRunStatusLabel}</span>
          </div>
          <div className="setup-actions">
            {reusable ? (
              <>
                <button
                  type="button"
                  className="primary-btn"
                  disabled={!canSaveMapping}
                  onClick={() => void onSaveMapping()}
                >
                  {mappingSaving ? 'Saving mapping…' : 'Save Mapping'}
                </button>
                <button
                  type="button"
                  className="primary-btn"
                  disabled={!canDryRun}
                  onClick={() => void runDryRun()}
                >
                  {dryRunLoading ? 'Running dry run…' : 'Run Dry Run'}
                </button>
                {canConfirm ? (
                  <button
                    ref={confirmButtonRef}
                    type="button"
                    className="primary-btn"
                    disabled={!canConfirm}
                    onClick={openConfirmDialog}
                  >
                    Confirm Import
                  </button>
                ) : null}
              </>
            ) : null}
            {needsStagingCleanupRetry(batch, confirmResult) ? (
              <button
                type="button"
                className="link-btn"
                disabled={requestActive}
                onClick={() => void onRetryCleanup()}
              >
                {cleanupBusy ? 'Retrying cleanup…' : 'Retry staging cleanup'}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {confirmResult ? (
        <div
          className="administration-import-completion"
          role="status"
          aria-labelledby="client-data-import-complete-heading"
        >
          <h3 id="client-data-import-complete-heading">Import completed</h3>
          <dl className="administration-import-counts" aria-label="Import completion summary">
            <div>
              <dt>Client</dt>
              <dd>{activeClientDisplay}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{confirmResult.status || 'Imported'}</dd>
            </div>
            <div>
              <dt>Companies created</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'companies_created',
                  'created_company_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Companies reused</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'companies_reused',
                  'reused_company_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Contacts created</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'contacts_created',
                  'created_contact_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Contacts reused</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'contacts_reused',
                  'reused_contact_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Relationships created</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'relationships_created',
                  'created_relationship_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Existing relationships</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'relationships_existing',
                  'existing_relationship_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Statuses imported</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'statuses_imported',
                  'imported_status_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Notes set / appended / duplicate</dt>
              <dd>
                {countValue(confirmResult as Record<string, number | undefined>, 'notes_set', 'notes_set_count')}{' '}
                /{' '}
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'notes_appended',
                  'notes_appended_count',
                )}{' '}
                /{' '}
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'notes_duplicate',
                  'notes_duplicate_count',
                )}
              </dd>
            </div>
            <div>
              <dt>History inserted / already present / within-batch dup</dt>
              <dd>
                {countValue(confirmResult as Record<string, number | undefined>, 'history_inserted')}{' '}
                /{' '}
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'history_already_present',
                )}{' '}
                /{' '}
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'history_duplicate_within_batch',
                )}
              </dd>
            </div>
            <div>
              <dt>Closed excluded</dt>
              <dd>
                {countValue(
                  confirmResult as Record<string, number | undefined>,
                  'closed_excluded_count',
                )}
              </dd>
            </div>
            <div>
              <dt>Imported at</dt>
              <dd>{confirmResult.imported_at || '—'}</dd>
            </div>
          </dl>
          <details className="administration-import-audit">
            <summary>Audit details</summary>
            <p className="queue-sub">
              Confirmed plan fingerprint:{' '}
              {confirmResult.confirmed_plan_fingerprint || '—'}
            </p>
            {confirmResult.audit ? (
              <pre className="administration-import-audit-json">
                {JSON.stringify(confirmResult.audit, null, 2)}
              </pre>
            ) : null}
          </details>
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
      {terminalMessage ? (
        <p className="data-status" role="status">
          {terminalMessage}
        </p>
      ) : null}

      {batch ? (
        <div className="administration-import-preview">
          <h3>Staging preview</h3>
          <dl className="administration-import-meta">
            <div>
              <dt>Prospects file</dt>
              <dd>{batch.original_filename || '—'}</dd>
            </div>
            <div>
              <dt>History file</dt>
              <dd>{batch.history_filename || '—'}</dd>
            </div>
            <div>
              <dt>Worksheet</dt>
              <dd>{batch.worksheet_name || '—'}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{batch.status}</dd>
            </div>
            <div>
              <dt>Prospect rows</dt>
              <dd>
                {batch.source_row_count ?? batch.total_rows ?? '—'}
                {batch.error_row_count ? ` / ${batch.error_row_count} with errors` : ''}
              </dd>
            </div>
            <div>
              <dt>History rows</dt>
              <dd>{batch.history_row_count ?? '—'}</dd>
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
          {(batch.warnings || []).length > 0 ? (
            <ul className="administration-import-warnings">
              {(batch.warnings || []).map((warning, index) => (
                <li key={`${index}-${warning}`}>{warning}</li>
              ))}
            </ul>
          ) : null}

          <div
            className="administration-import-closed-policy"
            role="region"
            aria-labelledby="client-data-import-closed-policy-heading"
          >
            <h3 id="client-data-import-closed-policy-heading">Closed policy</h3>
            <p className="queue-sub">{closedPolicyText(batch, dryRun)}</p>
            {dryRun ? (
              <p className="queue-sub" role="status">
                Closed records excluded from this import:{' '}
                <strong>{closedExcluded}</strong>
              </p>
            ) : (
              <p className="queue-sub">
                Excluded Closed count appears after a dry run.
              </p>
            )}
          </div>

          {reusable && prospectsHeaders.length > 0 ? (
            <div className="administration-import-mapping">
              <h3>Column mapping</h3>
              <p className="queue-sub">
                Map spreadsheet columns to NorthStar fields. Save Mapping before running a dry
                run. Suggested matches are draft only.
              </p>
              <p className="queue-sub" role="status">
                Mapping status: <strong>{mappingStatusLabel}</strong>
                {hasSavedMapping && batch.mapping_updated_at
                  ? ` · Saved ${batch.mapping_updated_at}`
                  : null}
              </p>

              <div className="administration-import-mapping-groups">
                <fieldset className="administration-import-fieldset">
                  <legend>Company</legend>
                  {companyFields().map((field) =>
                    renderMappingSelect(field.key, field.label, field.required, 'prospects'),
                  )}
                </fieldset>
                <fieldset className="administration-import-fieldset">
                  <legend>Contact</legend>
                  {contactFields().map((field) =>
                    renderMappingSelect(field.key, field.label, field.required, 'prospects'),
                  )}
                </fieldset>
                <fieldset className="administration-import-fieldset">
                  <legend>Relationship</legend>
                  {relationshipFields().map((field) =>
                    renderMappingSelect(field.key, field.label, field.required, 'prospects'),
                  )}
                </fieldset>
                {hasHistory ? (
                  <fieldset className="administration-import-fieldset">
                    <legend>History</legend>
                    {historyFields().map((field) =>
                      renderMappingSelect(field.key, field.label, field.required, 'history'),
                    )}
                  </fieldset>
                ) : null}
              </div>

              {mappingDirty && !localValidationOk ? (
                <ul className="administration-import-warnings" role="alert">
                  {(mappingErrors.length ? mappingErrors : localValidationErrors).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              ) : null}
              {mappingSaveError ? (
                <p className="data-status data-status--error" role="alert">
                  {mappingSaveError}
                </p>
              ) : null}
              {mappingSaveSuccess ? (
                <p className="status-banner" role="status">
                  {mappingSaveSuccess}
                </p>
              ) : null}
            </div>
          ) : null}

          {reusable && dryRunError ? (
            <p className="data-status data-status--error" role="alert">
              {dryRunError}
            </p>
          ) : null}

          {reusable && dryRun ? (
            <div className="administration-import-dry-run">
              <h3>Dry-run review</h3>
              <p className="queue-sub">
                Counts below cover the complete batch. Confirm stays disabled until all blocking,
                possible-match, invalid, unresolved, and status-conflict counts are zero.
              </p>

              {allReady ? (
                <p className="status-banner" role="status">
                  Dry run complete. Ready for confirmation.
                </p>
              ) : (
                <p className="data-status data-status--error" role="status">
                  Review is required before this batch can be imported.
                </p>
              )}

              <dl className="administration-import-counts" aria-label="Full-batch dry-run counts">
                <div>
                  <dt>Companies create / reuse / possible</dt>
                  <dd>
                    {countValue(prospectsCounts, 'companies_create', 'create_company')} /{' '}
                    {countValue(prospectsCounts, 'companies_reuse', 'use_existing_company')} /{' '}
                    {countValue(
                      prospectsCounts,
                      'companies_possible',
                      'possible_company_match',
                      'possible_company_match_count',
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Contacts create / reuse / possible</dt>
                  <dd>
                    {countValue(prospectsCounts, 'contacts_create', 'create_contact')} /{' '}
                    {countValue(prospectsCounts, 'contacts_reuse', 'use_existing_contact')} /{' '}
                    {countValue(prospectsCounts, 'contacts_possible', 'possible_contact_match')}
                  </dd>
                </div>
                <div>
                  <dt>Relationships create / existing</dt>
                  <dd>
                    {countValue(
                      prospectsCounts,
                      'relationships_create',
                      'create_client_relationship',
                    )}{' '}
                    /{' '}
                    {countValue(
                      prospectsCounts,
                      'relationships_existing',
                      'relationship_already_exists',
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Statuses imported / conflicting / invalid</dt>
                  <dd>
                    {countValue(prospectsCounts, 'statuses_imported', 'use_imported_status')} /{' '}
                    {countValue(prospectsCounts, 'statuses_conflicting', 'status_conflict')} /{' '}
                    {countValue(prospectsCounts, 'statuses_invalid', 'invalid_status')}
                  </dd>
                </div>
                <div>
                  <dt>Notes set / appended / duplicate</dt>
                  <dd>
                    {countValue(prospectsCounts, 'notes_set', 'set_imported_notes')} /{' '}
                    {countValue(prospectsCounts, 'notes_appended', 'append_imported_notes')} /{' '}
                    {countValue(
                      prospectsCounts,
                      'notes_duplicate',
                      'imported_notes_already_present',
                    )}
                  </dd>
                </div>
                <div>
                  <dt>History insert / already present / within-batch dup / invalid / unresolved</dt>
                  <dd>
                    {countValue(historyCounts, 'insert')} /{' '}
                    {countValue(historyCounts, 'already_present')} /{' '}
                    {countValue(historyCounts, 'duplicate_within_batch')} /{' '}
                    {countValue(historyCounts, 'invalid')} /{' '}
                    {countValue(historyCounts, 'unresolved')}
                  </dd>
                </div>
                <div>
                  <dt>Closed excluded</dt>
                  <dd>{closedExcluded}</dd>
                </div>
                <div>
                  <dt>Blocking / needs review</dt>
                  <dd>
                    {countValue(prospectsCounts, 'blocking', 'blocking_error')} /{' '}
                    {countValue(prospectsCounts, 'needs_review', 'needs_review_rows')}
                  </dd>
                </div>
                <div>
                  <dt>Confirm allowed</dt>
                  <dd>{allReady ? 'Yes' : 'No'}</dd>
                </div>
              </dl>

              {(dryRun.rows || dryRun.sample_rows || []).length > 0 ? (
                <div
                  className="administration-import-table-scroll"
                  tabIndex={0}
                  aria-label="Dry-run sample rows"
                >
                  <table className="queue-table administration-import-dry-run-table">
                    <thead>
                      <tr>
                        <th>Sample</th>
                        <th>Detail</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(dryRun.rows || dryRun.sample_rows || []).slice(0, 25).map((row, index) => (
                        <tr key={index}>
                          <td>{index + 1}</td>
                          <td>
                            <pre className="administration-import-audit-json">
                              {JSON.stringify(row, null, 2)}
                            </pre>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {confirmOpen && dryRun ? (
        <div
          className="administration-import-confirm-backdrop"
          onClick={() => {
            if (!confirming) closeConfirmDialog()
          }}
        >
          <div
            className="administration-import-confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="client-data-import-confirm-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="client-data-import-confirm-title">
              Confirm client data import for {activeClientDisplay}?
            </h3>
            <p className="queue-sub">
              This writes prospects, relationships, notes, and optional shared history into
              NorthStar for <strong>{activeClientDisplay}</strong>. Only the current dry-run plan
              fingerprint will be accepted.
            </p>
            <dl className="administration-import-counts" aria-label="Confirm import summary">
              <div>
                <dt>Companies to create</dt>
                <dd>{countValue(prospectsCounts, 'companies_create', 'create_company')}</dd>
              </div>
              <div>
                <dt>Companies to reuse</dt>
                <dd>{countValue(prospectsCounts, 'companies_reuse', 'use_existing_company')}</dd>
              </div>
              <div>
                <dt>Contacts to create</dt>
                <dd>{countValue(prospectsCounts, 'contacts_create', 'create_contact')}</dd>
              </div>
              <div>
                <dt>Contacts to reuse</dt>
                <dd>{countValue(prospectsCounts, 'contacts_reuse', 'use_existing_contact')}</dd>
              </div>
              <div>
                <dt>Relationships to create</dt>
                <dd>
                  {countValue(
                    prospectsCounts,
                    'relationships_create',
                    'create_client_relationship',
                  )}
                </dd>
              </div>
              <div>
                <dt>Existing relationships</dt>
                <dd>
                  {countValue(
                    prospectsCounts,
                    'relationships_existing',
                    'relationship_already_exists',
                  )}
                </dd>
              </div>
              <div>
                <dt>Notes set / appended</dt>
                <dd>
                  {countValue(prospectsCounts, 'notes_set', 'set_imported_notes')} /{' '}
                  {countValue(prospectsCounts, 'notes_appended', 'append_imported_notes')}
                </dd>
              </div>
              <div>
                <dt>History inserts</dt>
                <dd>{countValue(historyCounts, 'insert')}</dd>
              </div>
              <div>
                <dt>Closed excluded</dt>
                <dd>{closedExcluded}</dd>
              </div>
            </dl>
            {confirmError ? (
              <p className="data-status data-status--error" role="alert">
                {confirmError}
              </p>
            ) : null}
            <div className="setup-actions">
              <button
                ref={confirmCancelRef}
                type="button"
                className="link-btn"
                disabled={confirming}
                onClick={closeConfirmDialog}
              >
                Cancel
              </button>
              <button
                type="button"
                className="primary-btn"
                disabled={confirming}
                onClick={() => void onConfirmImport()}
              >
                {confirming ? 'Importing…' : 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  )
}
