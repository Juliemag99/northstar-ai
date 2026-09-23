import { useEffect, useMemo, useState } from 'react'
import type { ProspectListItem } from './types/carmeco'
import {
  confirmBulkAssignment,
  fetchBulkAssignees,
  previewBulkAssignment,
  type BulkAssignee,
  type BulkAssignmentPreview,
  type ProspectAssignmentFilter,
} from './api/bulkAssignment'

export type ProspectFilterSnapshot = {
  clientId: number
  q: string
  status: string
  milestoneType: string
  assignedUserId: number | null | undefined
}

export function prospectFilterKey(filter: ProspectFilterSnapshot): string {
  const assigned =
    filter.assignedUserId === undefined || filter.assignedUserId === null
      ? ''
      : String(filter.assignedUserId)
  return [
    filter.clientId,
    filter.q.trim(),
    filter.status.trim(),
    filter.milestoneType.trim(),
    assigned,
  ].join('|')
}

export function visibleCcrIds(rows: ProspectListItem[]): number[] {
  const ids: number[] = []
  const seen = new Set<number>()
  for (const row of rows) {
    const id = Number(row.relationship_id || 0)
    if (id > 0 && !seen.has(id)) {
      seen.add(id)
      ids.push(id)
    }
  }
  return ids
}

export type ProspectSelection =
  | { mode: 'none'; ccrIds: number[]; filteredTotal: number }
  | { mode: 'explicit'; ccrIds: number[]; filteredTotal: number }
  | { mode: 'filtered'; ccrIds: number[]; filteredTotal: number }

export function emptySelection(filteredTotal = 0): ProspectSelection {
  return { mode: 'none', ccrIds: [], filteredTotal }
}

export function selectCurrentPage(
  rows: ProspectListItem[],
  filteredTotal: number,
): ProspectSelection {
  const ids = visibleCcrIds(rows)
  return {
    mode: ids.length ? 'explicit' : 'none',
    ccrIds: ids,
    filteredTotal,
  }
}

export function selectAllFiltered(filteredTotal: number): ProspectSelection {
  return { mode: 'filtered', ccrIds: [], filteredTotal }
}

export function toggleRow(
  selection: ProspectSelection,
  ccrId: number,
  visibleIds: number[],
): ProspectSelection {
  if (ccrId <= 0) return selection
  if (selection.mode === 'filtered') {
    const next = visibleIds.filter((id) => id !== ccrId)
    return {
      mode: next.length ? 'explicit' : 'none',
      ccrIds: next,
      filteredTotal: selection.filteredTotal,
    }
  }
  const has = selection.ccrIds.includes(ccrId)
  const ccrIds = has
    ? selection.ccrIds.filter((id) => id !== ccrId)
    : [...selection.ccrIds, ccrId]
  return {
    mode: ccrIds.length ? 'explicit' : 'none',
    ccrIds,
    filteredTotal: selection.filteredTotal,
  }
}

export function isRowSelected(selection: ProspectSelection, ccrId: number): boolean {
  if (ccrId <= 0) return false
  if (selection.mode === 'filtered') return true
  return selection.ccrIds.includes(ccrId)
}

export function selectedCount(selection: ProspectSelection): number {
  if (selection.mode === 'filtered') return selection.filteredTotal
  return selection.ccrIds.length
}

export function pageAllSelected(selection: ProspectSelection, rows: ProspectListItem[]): boolean {
  const ids = visibleCcrIds(rows)
  if (ids.length === 0) return false
  if (selection.mode === 'filtered') return true
  return ids.every((id) => selection.ccrIds.includes(id))
}

type Props = {
  enabled: boolean
  clientId: number
  clientName: string
  rows: ProspectListItem[]
  filteredTotal: number
  filter: ProspectFilterSnapshot
  selection: ProspectSelection
  onSelectionChange: (next: ProspectSelection) => void
  onAssigned: () => void
}

export default function ProspectBulkAssign({
  enabled,
  clientId,
  clientName,
  rows,
  filteredTotal,
  filter,
  selection,
  onSelectionChange,
  onAssigned,
}: Props) {
  const [assignees, setAssignees] = useState<BulkAssignee[]>([])
  const [targetUserId, setTargetUserId] = useState('')
  const [reason, setReason] = useState('')
  const [open, setOpen] = useState(false)
  const [preview, setPreview] = useState<BulkAssignmentPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const count = selectedCount(selection)
  const filterKey = prospectFilterKey(filter)

  useEffect(() => {
    if (!enabled || clientId <= 0) {
      setAssignees([])
      return
    }
    let cancelled = false
    void fetchBulkAssignees(clientId)
      .then((rows) => {
        if (!cancelled) setAssignees(rows)
      })
      .catch(() => {
        if (!cancelled) setAssignees([])
      })
    return () => {
      cancelled = true
    }
  }, [enabled, clientId])

  const payload = useMemo(() => {
    const filterBody: ProspectAssignmentFilter = {
      client_id: clientId,
      q: filter.q,
      status: filter.status,
      milestone_type: filter.milestoneType,
      ...(filter.assignedUserId === undefined || filter.assignedUserId === null
        ? {}
        : { assigned_user_id: filter.assignedUserId }),
    }
    if (selection.mode === 'filtered') {
      return {
        selection_mode: 'filtered' as const,
        filter: filterBody,
      }
    }
    return {
      selection_mode: 'explicit' as const,
      client_id: clientId,
      ccr_ids: selection.ccrIds,
    }
  }, [clientId, filter, selection])

  if (!enabled) return null

  async function runPreview() {
    setError(null)
    setSuccess(null)
    setBusy(true)
    try {
      const result = await previewBulkAssignment({
        ...payload,
        target_user_id: Number(targetUserId),
        reason,
      })
      setPreview(result)
    } catch (err) {
      setPreview(null)
      setError(err instanceof Error ? err.message : 'Preview failed.')
    } finally {
      setBusy(false)
    }
  }

  async function runConfirm() {
    if (!preview?.confirm_allowed || !preview.preview_fingerprint) return
    setError(null)
    setBusy(true)
    try {
      const result = await confirmBulkAssignment({
        ...payload,
        target_user_id: Number(targetUserId),
        reason,
        preview_fingerprint: preview.preview_fingerprint,
        confirm: true,
      })
      setSuccess(result.message)
      setOpen(false)
      setPreview(null)
      setReason('')
      onSelectionChange(emptySelection(filteredTotal))
      onAssigned()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Assignment failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bulk-assign-bar">
      <div className="bulk-assign-bar__status">
        {selection.mode === 'filtered' ? (
          <p role="status">All {count} filtered records selected</p>
        ) : (
          <p role="status">{count} selected</p>
        )}
        {selection.mode !== 'filtered' && filteredTotal > rows.length ? (
          <button
            type="button"
            className="link-btn"
            onClick={() => onSelectionChange(selectAllFiltered(filteredTotal))}
          >
            Select all {filteredTotal} records matching these filters
          </button>
        ) : null}
      </div>
      <div className="heading-controls">
        <button
          type="button"
          className="link-btn"
          onClick={() =>
            onSelectionChange(selectCurrentPage(rows, filteredTotal))
          }
        >
          Select Current Page
        </button>
        <button
          type="button"
          className="link-btn"
          onClick={() => onSelectionChange(selectAllFiltered(filteredTotal))}
        >
          Select All Filtered Results
        </button>
        <button
          type="button"
          className="link-btn"
          onClick={() => onSelectionChange(emptySelection(filteredTotal))}
        >
          Clear Selection
        </button>
        <label className="edit-field">
          <span className="edit-field__label">Bulk Actions</span>
          <select
            className="edit-select"
            aria-label="Bulk Actions"
            value=""
            disabled={count === 0}
            onChange={(event) => {
              if (event.target.value === 'assign') {
                setOpen(true)
                setPreview(null)
                setError(null)
              }
            }}
          >
            <option value="">Bulk Actions</option>
            <option value="assign">Assign to Rep</option>
          </select>
        </label>
      </div>
      {success ? <p className="data-status" role="status">{success}</p> : null}
      {open ? (
        <section
          className="panel bulk-assign-preview"
          role="dialog"
          aria-modal="true"
          aria-labelledby="assign-to-rep-title"
        >
          <div className="panel-header">
            <h2 id="assign-to-rep-title">Assign to Rep</h2>
            <button
              type="button"
              className="link-btn"
              onClick={() => setOpen(false)}
              disabled={busy}
            >
              Close
            </button>
          </div>
          <p>
            {clientName}: {count} selected
            {selection.mode === 'filtered' ? ` · filter ${filterKey}` : ''}
          </p>
          <label className="edit-field">
            <span className="edit-field__label">Assignee</span>
            <select
              className="edit-select"
              aria-label="Assignee"
              value={targetUserId}
              onChange={(event) => setTargetUserId(event.target.value)}
            >
              <option value="">Select a NorthStar user</option>
              {assignees.map((user) => (
                <option key={user.user_id} value={user.user_id}>
                  {user.full_name || user.email}
                </option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Reason</span>
            <input
              className="edit-input"
              aria-label="Assignment reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Premier pilot book assignment"
            />
          </label>
          {error ? <p className="error-text">{error}</p> : null}
          {preview ? (
            <div className="bulk-assign-summary">
              <p>Client: {preview.client?.name || clientName}</p>
              <p>Selected: {preview.selected_count}</p>
              <p>New assignments: {preview.new_assignments}</p>
              {preview.reassignments > 0 ? (
                <p className="reassign-warning" role="alert">
                  Reassignments: {preview.reassignments} records will move from another
                  rep.
                </p>
              ) : (
                <p>Reassignments: 0</p>
              )}
              <p>Already assigned: {preview.already_assigned}</p>
              <p>
                Inactive/blocked: {preview.inactive_removed + preview.archived_company + preview.invalid_missing}
              </p>
              <p>Hot: {preview.hot_count}</p>
              <p>Follow-ups: {preview.follow_up_count}</p>
              <p>Appointment Set: {preview.appointment_set_count}</p>
              {preview.mixed_client ? (
                <p className="error-text">Mixed-client selection is blocked.</p>
              ) : null}
              {preview.unauthorized_target ? (
                <p className="error-text">Assignee is not authorized for this client.</p>
              ) : null}
            </div>
          ) : null}
          <div className="heading-controls" style={{ marginTop: '0.85rem' }}>
            <button
              type="button"
              className="ghost-btn"
              disabled={busy || !targetUserId || !reason.trim()}
              onClick={() => void runPreview()}
            >
              {busy ? 'Working…' : 'Preview'}
            </button>
            <button
              type="button"
              className="primary-btn"
              disabled={busy || !preview?.confirm_allowed}
              onClick={() => void runConfirm()}
            >
              Confirm Assignment
            </button>
          </div>
        </section>
      ) : null}
    </div>
  )
}
