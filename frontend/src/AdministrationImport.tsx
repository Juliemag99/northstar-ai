import { useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelCrmImport,
  confirmCrmImport,
  CRM_IMPORT_DRY_RUN_MAX_PAGE,
  dryRunCrmImport,
  fetchCrmImportHistory,
  fetchCrmImportRows,
  isValidCrmImportPlanFingerprint,
  saveCrmImportMapping,
  saveCrmImportMatchResolution,
  saveCrmImportSourceType,
  saveCrmImportStatusResolution,
  uploadCrmImport,
  type CrmImportBatch,
  type CrmImportConfirmResponse,
  type CrmImportDryRunResponse,
  type CrmImportDryRunRow,
  type CrmImportHistoryItem,
  type CrmImportRow,
} from './api/crmImport'
import {
  companyFields,
  contactFields,
  mappingsEqual,
  normalizeMapping,
  relationshipFields,
  suggestMapping,
  validateMapping,
} from './crmImportMapping'

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
  fallbackName = '',
): string {
  const name =
    availableClients.find((c) => c.client_id === clientId)?.client_name ||
    fallbackName.trim()
  if (clientId > 0 && name) return `${name} (Client ${clientId})`
  if (name) return name
  if (clientId > 0) return `Client ${clientId}`
  return ''
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

function companyActionLabel(action: string): string {
  switch (action) {
    case 'create_company':
      return 'Create company'
    case 'use_existing_company':
      return 'Use existing company'
    case 'possible_company_match':
      return 'Possible company match'
    case 'none':
      return 'None'
    default:
      return action || '—'
  }
}

function contactActionLabel(action: string): string {
  switch (action) {
    case 'create_contact':
      return 'Create contact'
    case 'use_existing_contact':
      return 'Use existing contact'
    case 'possible_contact_match':
      return 'Possible contact match'
    case 'insufficient_contact_data':
      return 'Needs name or email'
    case 'no_contact_data':
      return 'No contact data'
    case 'deferred':
      return 'Deferred'
    case 'none':
      return 'None'
    default:
      return action || '—'
  }
}

function relationshipActionLabel(action: string): string {
  switch (action) {
    case 'create_client_relationship':
      return 'Create relationship'
    case 'relationship_already_exists':
      return 'Relationship already exists'
    case 'deferred':
      return 'Deferred'
    case 'none':
      return 'None'
    default:
      return action || '—'
  }
}

function statusActionLabel(action: string): string {
  switch (action) {
    case 'use_default_status':
      return 'Use default status (New)'
    case 'preserve_existing_status':
      return 'Preserve existing status'
    case 'use_imported_status':
      return 'Use imported status'
    case 'status_conflict':
      return 'Status conflict'
    case 'invalid_status':
      return 'Invalid status'
    case 'none':
      return 'None'
    default:
      return action || '—'
  }
}

function notesActionLabel(action: string): string {
  switch (action) {
    case 'no_notes_change':
      return 'No notes change'
    case 'set_imported_notes':
      return 'Set imported notes'
    case 'append_imported_notes':
      return 'Append imported notes'
    case 'imported_notes_already_present':
      return 'Notes already present'
    case 'none':
      return 'None'
    default:
      return action || '—'
  }
}

function validityLabel(validity: string): string {
  switch (validity) {
    case 'ok':
      return 'OK'
    case 'blocking_error':
      return 'Blocking error'
    case 'invalid_mapping_data':
      return 'Invalid mapped data'
    default:
      return validity || '—'
  }
}

function reasonLabel(code: string): string {
  const map: Record<string, string> = {
    email_exact: 'Exact email',
    phone_exact: 'Exact phone',
    phone_last7: 'Matching last 7 digits',
    name_exact: 'Exact name',
    name_exists_elsewhere: 'Name exists elsewhere',
    record_no_exact: 'Exact Record No.',
    source_identity_exact: 'LeadMaster source identity',
    merge_redirect: 'Approved company redirect',
    alias_record_no: 'Alias Record No.',
    source_identity_bound: 'LeadMaster RN already bound to another company',
    phone: 'Matching phone',
    address_city_state: 'Matching address/city/state',
  }
  return map[code] || code
}

function truncateMapped(value: string, max = 48): string {
  const text = String(value || '')
  if (text.length <= max) return text
  return `${text.slice(0, max - 1)}…`
}

function planReadyToConfirm(plan: CrmImportDryRunResponse | null): boolean {
  if (!plan) return false
  const excluded =
    Number(plan.counts.excluded_skip || 0) + Number(plan.counts.excluded_closed || 0)
  const blocked = Boolean(plan.validation_summary?.confirm_blocked)
  return (
    plan.counts.needs_review_rows === 0 &&
    plan.counts.importable_rows + excluded === plan.total_rows &&
    plan.total_rows > 0 &&
    !blocked &&
    isValidCrmImportPlanFingerprint(plan.plan_fingerprint)
  )
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
  const [historyItems, setHistoryItems] = useState<CrmImportHistoryItem[]>([])
  const [pageRows, setPageRows] = useState<CrmImportRow[]>([])
  const [pageOffset, setPageOffset] = useState(0)
  const [pageTotal, setPageTotal] = useState(0)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const [savedMapping, setSavedMapping] = useState<Record<string, string>>({})
  const [draftMapping, setDraftMapping] = useState<Record<string, string>>({})
  const [mappingErrors, setMappingErrors] = useState<string[]>([])
  const [mappingSaving, setMappingSaving] = useState(false)
  const [mappingSaveError, setMappingSaveError] = useState<string | null>(null)
  const [mappingSaveSuccess, setMappingSaveSuccess] = useState<string | null>(null)

  const [dryRun, setDryRun] = useState<CrmImportDryRunResponse | null>(null)
  const [dryRunOffset, setDryRunOffset] = useState(0)
  const [dryRunLoading, setDryRunLoading] = useState(false)
  const [dryRunError, setDryRunError] = useState<string | null>(null)
  const [useImportedStatusForExisting, setUseImportedStatusForExisting] = useState(false)
  const [statusDrafts, setStatusDrafts] = useState<
    Record<number, { mode: string; status: string }>
  >({})
  const [resolvingRowId, setResolvingRowId] = useState<number | null>(null)
  const [sourceTypeSaving, setSourceTypeSaving] = useState(false)
  const [resolveError, setResolveError] = useState<string | null>(null)
  const [terminalLocked, setTerminalLocked] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [confirmResult, setConfirmResult] = useState<CrmImportConfirmResponse | null>(null)
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null)
  const confirmDialogRef = useRef<HTMLDivElement | null>(null)
  const confirmCancelRef = useRef<HTMLButtonElement | null>(null)
  const confirmingRef = useRef(false)

  const canUpload = connectClientId != null && connectClientId > 0
  const previewHeaders = useMemo(() => batch?.headers || [], [batch?.headers])
  const heldFileName = useMemo(() => heldFile?.name || '', [heldFile])
  const activeClientDisplay = clientLabel(
    connectClientId || 0,
    availableClients,
    clientName,
  )

  const draftNormalized = useMemo(() => normalizeMapping(draftMapping), [draftMapping])
  const savedNormalized = useMemo(() => normalizeMapping(savedMapping), [savedMapping])
  const mappingDirty = !mappingsEqual(draftNormalized, savedNormalized)
  const localValidation = useMemo(
    () => validateMapping(draftNormalized, previewHeaders),
    [draftNormalized, previewHeaders],
  )
  const savedValidation = useMemo(
    () => validateMapping(savedNormalized, previewHeaders),
    [savedNormalized, previewHeaders],
  )
  const hasSavedMapping = Object.keys(savedNormalized).length > 0
  const reusable = Boolean(batch?.reusable && batch.status === 'previewed' && !terminalLocked)
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
            : dryRun.counts.needs_review_rows > 0
              ? 'Needs review'
              : planReadyToConfirm(dryRun)
                ? 'Ready to confirm'
                : 'Ready'

  const requestActive =
    busy ||
    mappingSaving ||
    dryRunLoading ||
    confirming ||
    resolvingRowId != null ||
    sourceTypeSaving

  const canSaveMapping =
    reusable &&
    mappingDirty &&
    localValidation.ok &&
    !requestActive

  const canDryRun =
    reusable &&
    hasSavedMapping &&
    !mappingDirty &&
    localValidation.ok &&
    !requestActive

  const canConfirm =
    reusable &&
    hasSavedMapping &&
    savedValidation.ok &&
    !mappingDirty &&
    planReadyToConfirm(dryRun) &&
    !requestActive

  function clearConfirmUi() {
    setConfirmOpen(false)
    setConfirmError(null)
    confirmingRef.current = false
    setConfirming(false)
  }

  function clearMappingAndPlan() {
    setSavedMapping({})
    setDraftMapping({})
    setMappingErrors([])
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    setDryRun(null)
    setDryRunOffset(0)
    setDryRunError(null)
    setUseImportedStatusForExisting(false)
    setStatusDrafts({})
    setResolvingRowId(null)
    setResolveError(null)
    setConfirmResult(null)
    clearConfirmUi()
  }

  function clearDryRunOnly() {
    setDryRun(null)
    setDryRunOffset(0)
    setDryRunError(null)
    setStatusDrafts({})
    setResolvingRowId(null)
    setResolveError(null)
    clearConfirmUi()
  }

  function applyBatchMapping(
    next: CrmImportBatch,
    options: { suggestIfEmpty: boolean },
  ) {
    const saved = normalizeMapping(next.mapping || {})
    setSavedMapping(saved)
    if (Object.keys(saved).length > 0) {
      setDraftMapping(saved)
    } else if (options.suggestIfEmpty && next.reusable) {
      const prior = normalizeMapping(next.suggested_mapping || {})
      if (Object.keys(prior).length > 0) {
        setDraftMapping(prior)
        setMessage(
          'Suggested mapping from a prior import with the same client, source type, and headers. Review the columns and Save Mapping before Dry Run.',
        )
      } else {
        setDraftMapping(suggestMapping(next.headers || []))
      }
    } else {
      setDraftMapping({})
    }
    setMappingErrors([])
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    clearDryRunOnly()
  }

  useEffect(() => {
    setBatch(null)
    setPageRows([])
    setPageOffset(0)
    setPageTotal(0)
    setVisibleSheets([])
    setWorksheet('')
    setError(null)
    setMessage(null)
    setHeldFile(null)
    setTerminalLocked(false)
    setSavedMapping({})
    setDraftMapping({})
    setMappingErrors([])
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    setDryRun(null)
    setDryRunOffset(0)
    setDryRunError(null)
    setUseImportedStatusForExisting(false)
    setConfirmResult(null)
    setConfirmOpen(false)
    setConfirmError(null)
    confirmingRef.current = false
    setConfirming(false)
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
    setTerminalLocked(false)
    clearMappingAndPlan()
  }

  function onDraftChange(field: string, header: string) {
    setDraftMapping((prev) => {
      const next = { ...prev }
      if (!header) delete next[field]
      else next[field] = header
      return next
    })
    setMappingSaveSuccess(null)
    setMappingSaveError(null)
    clearDryRunOnly()
  }

  async function loadPage(next: CrmImportBatch, offset: number) {
    if (!next.reusable || next.status !== 'previewed') {
      setPageRows([])
      setPageOffset(0)
      setPageTotal(0)
      return
    }
    if (next.client_id <= 0 || next.batch_id <= 0) return
    const page = await fetchCrmImportRows(next.client_id, next.batch_id, offset, PAGE_SIZE)
    setPageRows(page.rows)
    setPageOffset(page.offset)
    setPageTotal(page.total)
  }

  async function submitHeldFile(sheetName: string) {
    if (!canUpload || connectClientId == null || connectClientId <= 0) {
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
        clearMappingAndPlan()
        setMessage(result.message || 'Choose one visible sheet to continue.')
        return
      }
      setVisibleSheets([])
      if (result.kind === 'failed' || !result.batch) {
        setBatch(result.batch)
        setPageRows([])
        clearMappingAndPlan()
        setError(result.message || result.batch?.error_message || 'The spreadsheet could not be read.')
        return
      }
      setBatch(result.batch)
      setTerminalLocked(false)
      setMessage(result.message || 'Preview ready.')
      applyBatchMapping(result.batch, { suggestIfEmpty: true })
      await loadPage(result.batch, 0)
    } catch (err) {
      setError(errorMessage(err, 'Upload failed.'))
    } finally {
      setBusy(false)
    }
  }

  async function onCancelBatch() {
    if (!batch || !canUpload || connectClientId == null || connectClientId <= 0) return
    if (!batch.reusable) return
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
    const validation = validateMapping(draftNormalized, previewHeaders)
    setMappingErrors(validation.errors)
    if (!validation.ok || !canSaveMapping) return
    setMappingSaving(true)
    setMappingSaveError(null)
    setMappingSaveSuccess(null)
    setError(null)
    try {
      const updated = await saveCrmImportMapping(
        connectClientId,
        batch.batch_id,
        draftNormalized,
      )
      setBatch(updated)
      setSavedMapping(normalizeMapping(updated.mapping || {}))
      setDraftMapping(normalizeMapping(updated.mapping || {}))
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

  async function runDryRun(offset: number) {
    if (!batch || !canUpload || connectClientId == null || connectClientId <= 0) return
    if (!reusable) return
    if (!hasSavedMapping || mappingDirty) {
      setDryRunError('Save the current mapping before running a dry run.')
      return
    }
    setDryRunLoading(true)
    setDryRunError(null)
    setError(null)
    const previousFingerprint = dryRun?.plan_fingerprint || ''
    try {
      const plan = await dryRunCrmImport(
        connectClientId,
        batch.batch_id,
        offset,
        CRM_IMPORT_DRY_RUN_MAX_PAGE,
        useImportedStatusForExisting,
      )
      if (
        previousFingerprint &&
        offset > 0 &&
        plan.plan_fingerprint !== previousFingerprint
      ) {
        clearDryRunOnly()
        setDryRunError('The dry-run plan changed. Run Dry Run again from the first page.')
        return
      }
      setDryRun(plan)
      setDryRunOffset(plan.offset)
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

  function statusDraftFor(row: CrmImportDryRunRow): { mode: string; status: string } {
    const existing = statusDrafts[row.row_id]
    if (existing) return existing
    const isConflict = row.relationship.status_action === 'status_conflict'
    return {
      mode: isConflict ? '' : 'replace_with_status',
      status: '',
    }
  }

  async function saveStatusResolution(row: CrmImportDryRunRow) {
    if (!batch || connectClientId == null || connectClientId <= 0 || !reusable) return
    if (!row.relationship.needs_status_resolution) return
    const draft = statusDraftFor(row)
    const isConflict = row.relationship.status_action === 'status_conflict'
    let resolutionType = draft.mode
    if (!isConflict) {
      resolutionType = 'replace_with_status'
    }
    if (!resolutionType) {
      setResolveError('Choose how to resolve this status.')
      return
    }
    if (resolutionType === 'replace_with_status' && !draft.status.trim()) {
      setResolveError('Choose a valid client status.')
      return
    }
    setResolvingRowId(row.row_id)
    setResolveError(null)
    setDryRunError(null)
    try {
      await saveCrmImportStatusResolution(connectClientId, batch.batch_id, row.row_id, {
        resolution_type: resolutionType,
        resolved_status:
          resolutionType === 'replace_with_status' ? draft.status : null,
        clear: false,
      })
      setStatusDrafts((prev) => {
        const next = { ...prev }
        delete next[row.row_id]
        return next
      })
      await runDryRun(0)
    } catch (err) {
      const detail = errorMessage(err, 'Could not save status resolution.')
      setResolveError(detail)
    } finally {
      setResolvingRowId(null)
    }
  }

  async function loadHistory(clientId: number) {
    if (clientId <= 0) {
      setHistoryItems([])
      return
    }
    try {
      const page = await fetchCrmImportHistory(clientId)
      setHistoryItems(page.items || [])
    } catch {
      setHistoryItems([])
    }
  }

  useEffect(() => {
    void loadHistory(connectClientId || 0)
  }, [connectClientId])

  async function onSaveSourceType(nextType: string) {
    if (!batch || connectClientId == null || connectClientId <= 0 || !reusable) return
    if ((batch.source_type || 'CRM_IMPORT') === nextType) return
    setSourceTypeSaving(true)
    setError(null)
    try {
      const updated = await saveCrmImportSourceType(
        connectClientId,
        batch.batch_id,
        nextType,
      )
      setBatch(updated)
      applyBatchMapping(updated, { suggestIfEmpty: Object.keys(savedNormalized).length === 0 })
      setMessage(
        nextType === 'LEADMASTER'
          ? 'Source type: LeadMaster. Confirm will write LEADMASTER identities and provenance.'
          : 'Source type: Other / Generic CRM Import.',
      )
    } catch (err) {
      setError(errorMessage(err, 'Could not save source type.'))
    } finally {
      setSourceTypeSaving(false)
    }
  }

  async function saveMatchResolution(
    row: CrmImportDryRunRow,
    resolutionType: string,
    extra: { company_id?: number | null; contact_id?: number | null } = {},
  ) {
    if (!batch || connectClientId == null || connectClientId <= 0 || !reusable) return
    setResolvingRowId(row.row_id)
    setResolveError(null)
    setDryRunError(null)
    try {
      await saveCrmImportMatchResolution(connectClientId, batch.batch_id, row.row_id, {
        resolution_type: resolutionType,
        company_id: extra.company_id ?? null,
        contact_id: extra.contact_id ?? null,
        clear: false,
      })
      await runDryRun(0)
    } catch (err) {
      setResolveError(errorMessage(err, 'Could not save match resolution.'))
    } finally {
      setResolvingRowId(null)
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
    const stillEligible =
      reusable &&
      hasSavedMapping &&
      savedValidation.ok &&
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
      const result = await confirmCrmImport(
        connectClientId,
        batch.batch_id,
        dryRun.plan_fingerprint,
        useImportedStatusForExisting,
      )
      setConfirmResult(result)
      setBatch((prev) =>
        prev
          ? {
              ...prev,
              status: result.status || 'imported',
              reusable: false,
            }
          : prev,
      )
      setPageRows([])
      setPageOffset(0)
      setPageTotal(0)
      setDryRun(null)
      setDryRunOffset(0)
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
          setDryRunError(
            `${detail} Run Dry Run again before confirming.`,
          )
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

  const usedHeaders = useMemo(() => {
    const used = new Set<string>()
    for (const value of Object.values(draftNormalized)) {
      if (value) used.add(value)
    }
    return used
  }, [draftNormalized])

  const allReady = planReadyToConfirm(dryRun)

  const terminalMessage = (() => {
    if (!batch) return null
    if (confirmResult) return null
    if (batch.status === 'imported') {
      return 'This batch was imported. Mapping and dry-run are no longer available.'
    }
    if (batch.status === 'cancelled') {
      return 'This batch was cancelled. Upload a new file to continue.'
    }
    if (batch.status === 'expired') {
      return 'This batch expired. Upload a new file to continue.'
    }
    if (batch.status === 'failed') {
      return 'This batch failed during upload. Upload a new file to continue.'
    }
    if (terminalLocked || !batch.reusable) {
      return 'This import batch can no longer be previewed.'
    }
    return null
  })()

  return (
    <section className="administration-section" aria-labelledby="crm-import-heading">
      <h2 id="crm-import-heading">Company & contact import</h2>
      <p className="queue-sub">
        Upload a CSV or XLSX file, map columns, and run a read-only dry run. Nothing is
        written to companies, contacts, or relationships until a later confirmation step.
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
                {clientLabel(c.client_id, availableClients, c.client_name)}
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
          aria-label="Import status and actions"
        >
          <div className="administration-import-sticky-meta">
            <span>
              Client: <strong>{activeClientDisplay}</strong>
            </span>
            <span>
              Source:{' '}
              <strong>
                {(batch.source_type || 'CRM_IMPORT') === 'LEADMASTER'
                  ? 'LeadMaster'
                  : 'Other / Generic CRM Import'}
              </strong>
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
                  onClick={() => void runDryRun(0)}
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
          </div>
          {reusable ? (
            <label className="administration-import-option">
              <input
                type="checkbox"
                checked={useImportedStatusForExisting}
                disabled={requestActive}
                onChange={(e) => {
                  setUseImportedStatusForExisting(e.target.checked)
                  clearDryRunOnly()
                }}
              />
              Use imported nonblank statuses for existing relationships
            </label>
          ) : null}
          {reusable ? (
            <label className="setup-field" style={{ maxWidth: '28rem' }}>
              <span className="setup-field-label">Import source type</span>
              <select
                aria-label="Import source type"
                value={batch.source_type || 'CRM_IMPORT'}
                disabled={requestActive}
                onChange={(e) => void onSaveSourceType(e.target.value)}
              >
                <option value="CRM_IMPORT">Other / Generic CRM Import</option>
                <option value="LEADMASTER">LeadMaster</option>
              </select>
              <span className="queue-sub">
                Default is generic. Choose LeadMaster only for a LeadMaster-derived file.
              </span>
            </label>
          ) : null}
        </div>
      ) : null}

      {confirmResult ? (
        <div
          className="administration-import-completion"
          role="status"
          aria-labelledby="crm-import-complete-heading"
        >
          <h3 id="crm-import-complete-heading">Import completed</h3>
          <dl className="administration-import-counts" aria-label="Import completion summary">
            <div>
              <dt>Client</dt>
              <dd>{activeClientDisplay}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>Imported</dd>
            </div>
            <div>
              <dt>Imported rows</dt>
              <dd>{confirmResult.total_imported_row_count}</dd>
            </div>
            <div>
              <dt>Companies created</dt>
              <dd>{confirmResult.created_company_count}</dd>
            </div>
            <div>
              <dt>Company rows reused</dt>
              <dd>{confirmResult.reused_company_count}</dd>
            </div>
            <div>
              <dt>Contacts created</dt>
              <dd>{confirmResult.created_contact_count}</dd>
            </div>
            <div>
              <dt>Contact rows reused</dt>
              <dd>{confirmResult.reused_contact_count}</dd>
            </div>
            <div>
              <dt>Relationships created</dt>
              <dd>{confirmResult.created_relationship_count}</dd>
            </div>
            <div>
              <dt>Existing relationship rows</dt>
              <dd>{confirmResult.existing_relationship_count}</dd>
            </div>
            <div>
              <dt>Rows with no contact</dt>
              <dd>{confirmResult.no_contact_row_count}</dd>
            </div>
            <div>
              <dt>Imported statuses</dt>
              <dd>{confirmResult.imported_status_count}</dd>
            </div>
            <div>
              <dt>Default statuses</dt>
              <dd>{confirmResult.default_status_count}</dd>
            </div>
            <div>
              <dt>Preserved statuses</dt>
              <dd>{confirmResult.preserved_status_count}</dd>
            </div>
            <div>
              <dt>Notes set</dt>
              <dd>{confirmResult.notes_set_count}</dd>
            </div>
            <div>
              <dt>Notes appended</dt>
              <dd>{confirmResult.notes_appended_count}</dd>
            </div>
            <div>
              <dt>Notes duplicates skipped</dt>
              <dd>{confirmResult.notes_duplicate_count}</dd>
            </div>
            <div>
              <dt>Notes unchanged</dt>
              <dd>{confirmResult.notes_unchanged_count}</dd>
            </div>
            <div>
              <dt>Imported at</dt>
              <dd>{confirmResult.imported_at || '—'}</dd>
            </div>
          </dl>
          <details className="administration-import-audit">
            <summary>Audit details</summary>
            <p className="queue-sub">
              Confirmed plan fingerprint: {confirmResult.confirmed_plan_fingerprint}
            </p>
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
          {reusable && previewHeaders.length > 0 ? (
            <>
              <div
                className="administration-import-table-scroll"
                tabIndex={0}
                aria-label="Source preview table"
              >
                <table className="queue-table administration-import-preview-table">
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
                    {pageOffset + 1}–{Math.min(pageOffset + pageRows.length, pageTotal)} of{' '}
                    {pageTotal}
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

          {reusable ? (
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
                  {companyFields().map((field) => (
                    <label key={field.key} className="setup-field">
                      <span className="setup-field-label">
                        {field.label}
                        {field.required ? ' *' : ''}
                      </span>
                      <select
                        aria-label={`${field.label}${field.required ? ' *' : ''}`}
                        value={draftNormalized[field.key] || ''}
                        onChange={(e) => onDraftChange(field.key, e.target.value)}
                        disabled={mappingSaving || dryRunLoading || confirming}
                      >
                        <option value="">Not mapped</option>
                        {previewHeaders.map((header) => {
                          const taken =
                            usedHeaders.has(header) &&
                            draftNormalized[field.key] !== header
                          return (
                            <option key={header} value={header} disabled={taken}>
                              {header}
                            </option>
                          )
                        })}
                      </select>
                    </label>
                  ))}
                </fieldset>
                <fieldset className="administration-import-fieldset">
                  <legend>Contact</legend>
                  {contactFields().map((field) => (
                    <label key={field.key} className="setup-field">
                      <span className="setup-field-label">{field.label}</span>
                      <select
                        aria-label={field.label}
                        value={draftNormalized[field.key] || ''}
                        onChange={(e) => onDraftChange(field.key, e.target.value)}
                        disabled={mappingSaving || dryRunLoading || confirming}
                      >
                        <option value="">Not mapped</option>
                        {previewHeaders.map((header) => {
                          const taken =
                            usedHeaders.has(header) &&
                            draftNormalized[field.key] !== header
                          return (
                            <option key={header} value={header} disabled={taken}>
                              {header}
                            </option>
                          )
                        })}
                      </select>
                    </label>
                  ))}
                </fieldset>
                <fieldset className="administration-import-fieldset">
                  <legend>Relationship</legend>
                  {relationshipFields().map((field) => (
                    <label key={field.key} className="setup-field">
                      <span className="setup-field-label">{field.label}</span>
                      <select
                        aria-label={field.label}
                        value={draftNormalized[field.key] || ''}
                        onChange={(e) => onDraftChange(field.key, e.target.value)}
                        disabled={mappingSaving || dryRunLoading || confirming}
                      >
                        <option value="">Not mapped</option>
                        {previewHeaders.map((header) => {
                          const taken =
                            usedHeaders.has(header) &&
                            draftNormalized[field.key] !== header
                          return (
                            <option key={header} value={header} disabled={taken}>
                              {header}
                            </option>
                          )
                        })}
                      </select>
                    </label>
                  ))}
                </fieldset>
              </div>

              {mappingDirty && !localValidation.ok ? (
                <ul className="administration-import-warnings" role="alert">
                  {(mappingErrors.length ? mappingErrors : localValidation.errors).map(
                    (item) => (
                      <li key={item}>{item}</li>
                    ),
                  )}
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

          {resolveError ? (
            <p className="data-status data-status--error" role="alert">
              {resolveError}
            </p>
          ) : null}

          {reusable && dryRun ? (
            <div className="administration-import-dry-run">
              <h3>Dry-run review</h3>
              {dryRun.validation_summary ? (
                <dl
                  className="administration-import-counts"
                  aria-label="Import validation summary"
                >
                  <div>
                    <dt>Client</dt>
                    <dd>
                      {dryRun.validation_summary.client_name || activeClientDisplay}
                      {dryRun.validation_summary.client_id
                        ? ` (Client ${dryRun.validation_summary.client_id})`
                        : ''}
                    </dd>
                  </div>
                  <div>
                    <dt>Source type</dt>
                    <dd>
                      {dryRun.validation_summary.source_type === 'LEADMASTER'
                        ? 'LeadMaster'
                        : 'Other / Generic CRM Import'}
                    </dd>
                  </div>
                  <div>
                    <dt>Filename</dt>
                    <dd>{dryRun.validation_summary.original_filename || batch.original_filename}</dd>
                  </div>
                  <div>
                    <dt>Source rows</dt>
                    <dd>{dryRun.validation_summary.source_row_count}</dd>
                  </div>
                  <div>
                    <dt>Mapped company name</dt>
                    <dd>{dryRun.validation_summary.mapped_company_name_field || '—'}</dd>
                  </div>
                  <div>
                    <dt>Mapped source RN</dt>
                    <dd>{dryRun.validation_summary.mapped_source_rn_field || '—'}</dd>
                  </div>
                  <div>
                    <dt>Mapped status</dt>
                    <dd>{dryRun.validation_summary.mapped_status_field || '—'}</dd>
                  </div>
                  <div>
                    <dt>Mapped notes</dt>
                    <dd>{dryRun.validation_summary.mapped_notes_field || '—'}</dd>
                  </div>
                  <div>
                    <dt>Unresolved company matches</dt>
                    <dd>{dryRun.validation_summary.unresolved_company_matches}</dd>
                  </div>
                  <div>
                    <dt>Unresolved contact matches</dt>
                    <dd>{dryRun.validation_summary.unresolved_contact_matches}</dd>
                  </div>
                  <div>
                    <dt>Status conflicts</dt>
                    <dd>{dryRun.validation_summary.status_conflicts}</dd>
                  </div>
                  <div>
                    <dt>Invalid rows</dt>
                    <dd>{dryRun.validation_summary.invalid_rows}</dd>
                  </div>
                </dl>
              ) : null}
              <p className="queue-sub">
                Counts below cover the complete batch ({dryRun.total_rows} rows). The table shows
                one page of plan rows ({dryRun.limit} max per page).
              </p>

              {dryRun.counts.needs_review_rows > 0 ? (
                <p className="data-status data-status--error" role="status">
                  Review is required before this batch can be imported.
                </p>
              ) : allReady ? (
                <p className="status-banner" role="status">
                  Dry run complete. All rows are ready for the confirmation step.
                </p>
              ) : (
                <p className="data-status" role="status">
                  Review is required before this batch can be imported.
                </p>
              )}

              <dl className="administration-import-counts" aria-label="Full-batch dry-run counts">
                <div>
                  <dt>Importable rows</dt>
                  <dd>{dryRun.counts.importable_rows}</dd>
                </div>
                <div>
                  <dt>Needs review</dt>
                  <dd>{dryRun.counts.needs_review_rows}</dd>
                </div>
                <div>
                  <dt>Blocking errors</dt>
                  <dd>{dryRun.counts.blocking_error}</dd>
                </div>
                <div>
                  <dt>Invalid mapped data</dt>
                  <dd>{dryRun.counts.invalid_mapping_data}</dd>
                </div>
                <div>
                  <dt>Companies create / existing / possible</dt>
                  <dd>
                    {dryRun.counts.create_company} / {dryRun.counts.use_existing_company} /{' '}
                    {dryRun.counts.possible_company_match}
                  </dd>
                </div>
                <div>
                  <dt>Contacts create / existing / possible / insufficient / none</dt>
                  <dd>
                    {dryRun.counts.create_contact} / {dryRun.counts.use_existing_contact} /{' '}
                    {dryRun.counts.possible_contact_match} /{' '}
                    {dryRun.counts.insufficient_contact_data} / {dryRun.counts.no_contact_data}
                  </dd>
                </div>
                <div>
                  <dt>Relationships create / already exist / deferred</dt>
                  <dd>
                    {dryRun.counts.create_client_relationship} /{' '}
                    {dryRun.counts.relationship_already_exists} /{' '}
                    {dryRun.counts.relationship_deferred}
                  </dd>
                </div>
                <div>
                  <dt>Status imported / default / preserved</dt>
                  <dd>
                    {dryRun.counts.use_imported_status} / {dryRun.counts.use_default_status} /{' '}
                    {dryRun.counts.preserve_existing_status}
                  </dd>
                </div>
                <div>
                  <dt>Existing relationship status updates</dt>
                  <dd>{dryRun.counts.update_existing_status}</dd>
                </div>
                <div>
                  <dt>Status conflicts / invalid</dt>
                  <dd>
                    {dryRun.counts.status_conflict} / {dryRun.counts.invalid_status}
                  </dd>
                </div>
                <div>
                  <dt>Notes set / append / duplicate / unchanged</dt>
                  <dd>
                    {dryRun.counts.set_imported_notes} / {dryRun.counts.append_imported_notes} /{' '}
                    {dryRun.counts.imported_notes_already_present} /{' '}
                    {dryRun.counts.no_notes_change}
                  </dd>
                </div>
              </dl>

              <div
                className="administration-import-table-scroll"
                tabIndex={0}
                aria-label="Dry-run results table"
              >
                <table className="queue-table administration-import-dry-run-table">
                  <thead>
                    <tr>
                      <th>Source row</th>
                      <th>Validity</th>
                      <th>Company</th>
                      <th>Contact</th>
                      <th>Relationship</th>
                      <th>Status decision</th>
                      <th>Notes decision</th>
                      <th>Mapped summary</th>
                      <th>Possible matches</th>
                      <th>Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dryRun.rows.map((row) => {
                      const mappedCompany = truncateMapped(row.mapped.company_name || row.company.name || '')
                      const mappedContact = truncateMapped(
                        row.mapped.contact_full_name ||
                          [row.mapped.contact_first_name, row.mapped.contact_last_name]
                            .filter(Boolean)
                            .join(' ') ||
                          row.contact.display_name ||
                          row.mapped.contact_email ||
                          '',
                      )
                      const mappedStatus = truncateMapped(
                        row.relationship.resolved_status || row.mapped.relationship_status || '',
                      )
                      const mappedEntered = truncateMapped(row.mapped.source_entered_at || '')
                      const mappedUpdated = truncateMapped(row.mapped.source_updated_at || '')
                      const possibles = [
                        ...row.company.possibles.map(
                          (p) =>
                            `${p.company_name || `Company ${p.company_id}`}${
                              p.external_record_no ? ` (${p.external_record_no})` : ''
                            }: ${(p.reasons || []).map(reasonLabel).join(', ')}`,
                        ),
                        ...row.contact.possibles.map(
                          (p) =>
                            `${p.display_name || `Contact ${p.contact_id}`}: ${(
                              p.reasons || []
                            )
                              .map(reasonLabel)
                              .join(', ')}`,
                        ),
                      ]
                      const details = [
                        row.validity_detail,
                        ...row.company.reasons.map(reasonLabel),
                        ...row.contact.reasons.map(reasonLabel),
                      ].filter(Boolean)
                      return (
                        <tr key={row.row_id}>
                          <td>{row.source_row_number}</td>
                          <td>{validityLabel(row.validity)}</td>
                          <td>{companyActionLabel(row.company.action)}</td>
                          <td>{contactActionLabel(row.contact.action)}</td>
                          <td>{relationshipActionLabel(row.relationship.action)}</td>
                          <td>
                            <div>{statusActionLabel(row.relationship.status_action)}</div>
                            {mappedStatus ? <div>{mappedStatus}</div> : null}
                            {row.relationship.status_resolution_type ===
                            'keep_existing_status' ? (
                              <div>
                                Resolved: keep existing
                                {row.relationship.resolved_status
                                  ? ` (${row.relationship.resolved_status})`
                                  : ''}
                              </div>
                            ) : null}
                            {row.relationship.status_resolution_type ===
                            'replace_with_status' ? (
                              <div>
                                Resolved: replace with{' '}
                                {row.relationship.resolved_status || '—'}
                              </div>
                            ) : null}
                            {row.relationship.needs_status_resolution ? (
                              <div className="administration-import-status-resolve">
                                <label
                                  className="field-label"
                                  htmlFor={`resolve-status-${row.row_id}`}
                                >
                                  Resolve status
                                </label>
                                {row.relationship.status_action === 'status_conflict' ? (
                                  <select
                                    id={`resolve-status-${row.row_id}`}
                                    aria-label={`Resolve status for source row ${row.source_row_number}`}
                                    value={statusDraftFor(row).mode}
                                    disabled={requestActive}
                                    onChange={(event) => {
                                      const mode = event.target.value
                                      setStatusDrafts((prev) => ({
                                        ...prev,
                                        [row.row_id]: {
                                          mode,
                                          status:
                                            mode === 'replace_with_status'
                                              ? prev[row.row_id]?.status || ''
                                              : '',
                                        },
                                      }))
                                    }}
                                  >
                                    <option value="">Choose…</option>
                                    <option value="keep_existing_status">
                                      Keep existing status
                                      {row.relationship.existing_status
                                        ? ` (${row.relationship.existing_status})`
                                        : ''}
                                    </option>
                                    <option value="replace_with_status">
                                      Replace with a client status
                                    </option>
                                  </select>
                                ) : null}
                                {(row.relationship.status_action === 'invalid_status' ||
                                  statusDraftFor(row).mode === 'replace_with_status') && (
                                  <select
                                    id={
                                      row.relationship.status_action === 'invalid_status'
                                        ? `resolve-status-${row.row_id}`
                                        : `resolve-status-value-${row.row_id}`
                                    }
                                    aria-label={`Replacement status for source row ${row.source_row_number}`}
                                    value={statusDraftFor(row).status}
                                    disabled={requestActive}
                                    onChange={(event) => {
                                      setStatusDrafts((prev) => ({
                                        ...prev,
                                        [row.row_id]: {
                                          mode:
                                            prev[row.row_id]?.mode ||
                                            (row.relationship.status_action === 'invalid_status'
                                              ? 'replace_with_status'
                                              : ''),
                                          status: event.target.value,
                                        },
                                      }))
                                    }}
                                  >
                                    <option value="">Choose a status…</option>
                                    {(dryRun.status_catalog || []).map((label) => (
                                      <option key={label} value={label}>
                                        {label}
                                      </option>
                                    ))}
                                  </select>
                                )}
                                <button
                                  type="button"
                                  className="secondary-btn"
                                  disabled={requestActive}
                                  onClick={() => void saveStatusResolution(row)}
                                >
                                  {resolvingRowId === row.row_id
                                    ? 'Saving…'
                                    : 'Save Resolution'}
                                </button>
                              </div>
                            ) : null}
                          </td>
                          <td>{notesActionLabel(row.relationship.notes_action)}</td>
                          <td>
                            <div>{mappedCompany || '—'}</div>
                            <div>{mappedContact || '—'}</div>
                            {mappedEntered ? <div>Entered {mappedEntered}</div> : null}
                            {mappedUpdated ? <div>Updated {mappedUpdated}</div> : null}
                          </td>
                          <td>
                            {possibles.length
                              ? possibles.map((item) => <div key={item}>{item}</div>)
                              : '—'}
                            {row.company.action === 'possible_company_match' ? (
                              <div className="administration-import-status-resolve">
                                <div className="queue-sub">
                                  Source: RN {row.mapped.external_record_no || '—'} ·{' '}
                                  {row.mapped.company_name || row.company.name || '—'} ·{' '}
                                  {[row.mapped.address, row.mapped.city, row.mapped.state]
                                    .filter(Boolean)
                                    .join(', ') || '—'}
                                  {row.mapped.phone ? ` · ${row.mapped.phone}` : ''}
                                  {row.mapped.website ? ` · ${row.mapped.website}` : ''}
                                </div>
                                {row.company.possibles.map((p) => (
                                  <div key={`co-${p.company_id}`}>
                                    <div>
                                      Company {p.company_id}: {p.company_name}
                                      {p.external_record_no ? ` · RN ${p.external_record_no}` : ''}
                                      {p.identity_record_nos?.length
                                        ? ` · identities ${p.identity_record_nos.join(', ')}`
                                        : ''}
                                    </div>
                                    <div className="queue-sub">
                                      {[p.address, p.city, p.state].filter(Boolean).join(', ') || '—'}
                                      {p.phone ? ` · ${p.phone}` : ''}
                                      {p.website ? ` · ${p.website}` : ''}
                                      {p.reasons?.length ? ` · ${p.reasons.join(', ')}` : ''}
                                    </div>
                                    <button
                                      type="button"
                                      className="link-btn"
                                      disabled={requestActive}
                                      onClick={() =>
                                        void saveMatchResolution(row, 'use_existing_company', {
                                          company_id: p.company_id,
                                        })
                                      }
                                    >
                                      Use existing company {p.company_id}
                                    </button>
                                  </div>
                                ))}
                                {row.company.can_create_company ? (
                                  <button
                                    type="button"
                                    className="link-btn"
                                    disabled={requestActive}
                                    onClick={() =>
                                      void saveMatchResolution(row, 'create_company')
                                    }
                                  >
                                    Create new company
                                  </button>
                                ) : null}
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={requestActive}
                                  onClick={() => void saveMatchResolution(row, 'skip_row')}
                                >
                                  Skip source row
                                </button>
                              </div>
                            ) : null}
                            {row.contact.action === 'possible_contact_match' ? (
                              <div className="administration-import-status-resolve">
                                <div className="queue-sub">
                                  Source contact: {row.contact.display_name || '—'} ·{' '}
                                  {row.mapped.company_name || '—'} ·{' '}
                                  {row.mapped.contact_email || '—'} · {row.mapped.contact_phone || '—'}
                                </div>
                                {row.contact.possibles.map((p) => (
                                  <div key={`ct-${p.contact_id}`}>
                                    <div>
                                      Contact {p.contact_id}: {p.display_name}
                                      {p.company_name ? ` · ${p.company_name}` : ''}
                                      {p.email ? ` · ${p.email}` : ''}
                                      {p.phone ? ` · ${p.phone}` : ''}
                                    </div>
                                    {p.reasons?.length ? (
                                      <div className="queue-sub">{p.reasons.join(', ')}</div>
                                    ) : null}
                                    <button
                                      type="button"
                                      className="link-btn"
                                      disabled={requestActive}
                                      onClick={() =>
                                        void saveMatchResolution(row, 'use_existing_contact', {
                                          contact_id: p.contact_id,
                                        })
                                      }
                                    >
                                      Use existing contact {p.contact_id}
                                    </button>
                                  </div>
                                ))}
                                {row.contact.can_create_contact ? (
                                  <button
                                    type="button"
                                    className="link-btn"
                                    disabled={requestActive}
                                    onClick={() =>
                                      void saveMatchResolution(row, 'create_contact')
                                    }
                                  >
                                    Create new contact
                                  </button>
                                ) : null}
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={requestActive}
                                  onClick={() =>
                                    void saveMatchResolution(row, 'import_company_only')
                                  }
                                >
                                  Skip contact
                                </button>
                              </div>
                            ) : null}
                          </td>
                          <td>
                            {details.length
                              ? details.map((item) => <div key={item}>{item}</div>)
                              : '—'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              {dryRun.total_rows > dryRun.limit ? (
                <div className="setup-actions">
                  <button
                    type="button"
                    className="link-btn"
                    disabled={dryRunLoading || dryRunOffset <= 0}
                    onClick={() =>
                      void runDryRun(Math.max(0, dryRunOffset - CRM_IMPORT_DRY_RUN_MAX_PAGE))
                    }
                  >
                    Previous plan page
                  </button>
                  <span className="queue-sub">
                    {dryRunOffset + 1}–
                    {Math.min(dryRunOffset + dryRun.rows.length, dryRun.total_rows)} of{' '}
                    {dryRun.total_rows}
                  </span>
                  <button
                    type="button"
                    className="link-btn"
                    disabled={
                      dryRunLoading || dryRunOffset + dryRun.rows.length >= dryRun.total_rows
                    }
                    onClick={() =>
                      void runDryRun(dryRunOffset + CRM_IMPORT_DRY_RUN_MAX_PAGE)
                    }
                  >
                    Next plan page
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {canUpload ? (
        <div className="administration-import-history">
          <h3>Recent imports</h3>
          {historyItems.length === 0 ? (
            <p className="queue-sub">No stored import batches for {activeClientDisplay}.</p>
          ) : (
            <table className="queue-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Filename</th>
                  <th>Source</th>
                  <th>Uploaded by</th>
                  <th>State</th>
                  <th>Rows</th>
                  <th>Created / reused</th>
                </tr>
              </thead>
              <tbody>
                {historyItems.map((item) => (
                  <tr key={item.batch_id}>
                    <td>{item.imported_at || item.created_at || '—'}</td>
                    <td>{item.original_filename || '—'}</td>
                    <td>
                      {item.source_type === 'LEADMASTER'
                        ? 'LeadMaster'
                        : 'Other / Generic CRM Import'}
                    </td>
                    <td>{item.uploaded_by_name || '—'}</td>
                    <td>{item.status || '—'}</td>
                    <td>{item.source_row_count}</td>
                    <td>
                      {item.created_company_count}/{item.reused_company_count} companies ·{' '}
                      {item.created_contact_count}/{item.reused_contact_count} contacts
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
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
            ref={confirmDialogRef}
            className="administration-import-confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="crm-import-confirm-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="crm-import-confirm-title">
              Import {dryRun.total_rows} rows for {activeClientDisplay}?
            </h3>
            <p className="queue-sub">
              This writes companies, contacts, and client relationships into NorthStar for{' '}
              <strong>{activeClientDisplay}</strong>. Only rows from the current dry-run plan
              fingerprint will be accepted.
            </p>
            <dl className="administration-import-counts" aria-label="Confirm import summary">
              <div>
                <dt>Source type</dt>
                <dd>
                  {(dryRun.source_type || batch?.source_type) === 'LEADMASTER'
                    ? 'LeadMaster'
                    : 'Other / Generic CRM Import'}
                </dd>
              </div>
              <div>
                <dt>Total rows to import</dt>
                <dd>{dryRun.total_rows}</dd>
              </div>
              <div>
                <dt>Companies to create</dt>
                <dd>{dryRun.counts.create_company}</dd>
              </div>
              <div>
                <dt>Companies to reuse</dt>
                <dd>{dryRun.counts.use_existing_company}</dd>
              </div>
              <div>
                <dt>Contacts to create</dt>
                <dd>{dryRun.counts.create_contact}</dd>
              </div>
              <div>
                <dt>Contacts to reuse</dt>
                <dd>{dryRun.counts.use_existing_contact}</dd>
              </div>
              <div>
                <dt>Relationships to create</dt>
                <dd>{dryRun.counts.create_client_relationship}</dd>
              </div>
              <div>
                <dt>Existing relationships</dt>
                <dd>{dryRun.counts.relationship_already_exists}</dd>
              </div>
              <div>
                <dt>Existing relationship status updates</dt>
                <dd>{dryRun.counts.update_existing_status}</dd>
              </div>
              <div>
                <dt>Rows with no contact</dt>
                <dd>{dryRun.counts.no_contact_data}</dd>
              </div>
            </dl>
            {useImportedStatusForExisting ? (
              <p className="queue-sub">
                Imported nonblank statuses will replace existing relationship statuses for this
                client.
              </p>
            ) : null}
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
