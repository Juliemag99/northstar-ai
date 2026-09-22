import { Link } from 'react-router-dom'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  companyWorkspaceHref,
  completeFollowUpTask,
  fetchClientReps,
  fetchClientStatuses,
  fetchDashboardFollowUps,
  fetchNextActions,
  logWorkQueueCall,
  rescheduleFollowUpTask,
  type ContactRepOption,
} from './api/carmeco'
import AppointmentDetailsFields from './AppointmentDetailsFields'
import NextActionFields from './NextActionFields'
import {
  appointmentDetailsAreValid,
  appointmentSourceForStatus,
  emptyAppointmentDetails,
  isAppointmentSetStatus,
  type AppointmentDetails,
} from './appointmentSet'
import {
  defaultNextActionForStatus,
  displayNextAction,
  emptyNextActionCatalog,
  matchNextAction,
  nextActionIsValid,
  storedNextAction,
  type NextActionCatalog,
  type NextActionSelection,
} from './nextAction'
import type { DashboardFollowUpItem } from './types/carmeco'
import { useAuth } from './auth/useAuth'
import { authenticatedActorName } from './auth/reportDefaults'

type TaskActionKind = 'call' | 'complete' | 'reschedule' | null

function display(value: string | null | undefined): string {
  return (value || '').trim() || '—'
}

function followUpWhen(item: DashboardFollowUpItem): string {
  const datePart = (item.due_date || '').trim()
  const timePart = (item.due_time || '').trim().slice(0, 5)
  if (datePart && timePart) return `${datePart} ${timePart}`
  return datePart || timePart || '—'
}

function hasValidContact(item: DashboardFollowUpItem): boolean {
  return item.contact_id != null && item.contact_id > 0
}

function contactHref(item: DashboardFollowUpItem): string {
  if (!hasValidContact(item) || item.contact_id == null) return '#'
  return `/contacts/${item.contact_id}?client_id=${item.client_id}`
}

function taskKey(item: DashboardFollowUpItem): string {
  return `${item.source}-${item.source_id}-${item.company_id}`
}

function bucketLabel(bucket: string): string {
  const key = (bucket || '').trim().toLowerCase()
  if (key === 'overdue') return 'Overdue'
  if (key === 'due_today' || key === 'due today') return 'Due today'
  return 'Upcoming'
}

function statusNeverRequiresFollowUp(status: string): boolean {
  const key = status.trim().toLowerCase()
  if (!key) return false
  if (key.includes('do not call') || key === 'dnc') return true
  return key === 'closed' || key.startsWith('closed')
}

export default function Tasks({
  activeClientId,
  activeClientName,
  onTasksChanged,
}: {
  activeClientId: number | null
  activeClientName: string
  onTasksChanged?: () => void
}) {
  const { user } = useAuth()
  const actorName = authenticatedActorName(user)
  const [items, setItems] = useState<DashboardFollowUpItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [actionKey, setActionKey] = useState<string | null>(null)
  const [actionKind, setActionKind] = useState<TaskActionKind>(null)
  const [completeNote, setCompleteNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [catalog, setCatalog] = useState<NextActionCatalog>(emptyNextActionCatalog())
  const [statusOptions, setStatusOptions] = useState<string[]>([])
  const [reps, setReps] = useState<ContactRepOption[]>([])
  const [callStatus, setCallStatus] = useState('')
  const [callNotes, setCallNotes] = useState('')
  const [callNextSel, setCallNextSel] = useState<NextActionSelection>({ code: '', custom: '' })
  const [callFollowDate, setCallFollowDate] = useState('')
  const [callFollowTime, setCallFollowTime] = useState('')
  const [scheduleFollowUp, setScheduleFollowUp] = useState(false)
  const [callAssignedUserId, setCallAssignedUserId] = useState('')
  const [appointmentDetails, setAppointmentDetails] = useState<AppointmentDetails>(
    emptyAppointmentDetails(),
  )
  const appointmentHeadingRef = useRef<HTMLHeadingElement>(null)
  const [rescheduleDate, setRescheduleDate] = useState('')
  const [rescheduleTime, setRescheduleTime] = useState('')
  const [rescheduleAssignedUserId, setRescheduleAssignedUserId] = useState('')
  const [rescheduleNextSel, setRescheduleNextSel] = useState<NextActionSelection>({
    code: '',
    custom: '',
  })
  const [rescheduleNotes, setRescheduleNotes] = useState('')

  const scopedClient = activeClientId != null && activeClientId > 0

  async function reloadTasks() {
    if (activeClientId == null || activeClientId <= 0) return
    const data = await fetchDashboardFollowUps(activeClientId)
    setItems([...data.overdue, ...data.due_today, ...data.upcoming])
    onTasksChanged?.()
  }

  useEffect(() => {
    if (!scopedClient || activeClientId == null) {
      setItems([])
      setLoading(false)
      setError(null)
      setCatalog(emptyNextActionCatalog())
      setStatusOptions([])
      setReps([])
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    void Promise.all([
      fetchNextActions(activeClientId),
      fetchClientStatuses(activeClientId),
      fetchClientReps(activeClientId),
    ])
      .then(([nextCatalog, statuses, clientReps]) => {
        if (cancelled) return
        setCatalog(nextCatalog)
        setStatusOptions(statuses)
        setReps(clientReps)
      })
      .catch(() => {
        if (cancelled) return
        setCatalog(emptyNextActionCatalog())
        setStatusOptions([])
        setReps([])
      })
    void fetchDashboardFollowUps(activeClientId)
      .then((data) => {
        if (cancelled) return
        setItems([...data.overdue, ...data.due_today, ...data.upcoming])
      })
      .catch((err) => {
        if (cancelled) return
        setItems([])
        setError(err instanceof Error ? err.message : 'Failed to load tasks.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [activeClientId, scopedClient])

  useEffect(() => {
    if (actionKind !== 'call') return
    if (!isAppointmentSetStatus(callStatus)) return
    const frame = window.requestAnimationFrame(() => {
      appointmentHeadingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [actionKind, callStatus])

  const counts = useMemo(() => {
    return {
      overdue: items.filter((item) => item.bucket === 'overdue').length,
      dueToday: items.filter((item) => item.bucket === 'due_today').length,
      upcoming: items.filter((item) => item.bucket === 'upcoming').length,
    }
  }, [items])

  function closeAction() {
    setActionKey(null)
    setActionKind(null)
    setCompleteNote('')
    setCallNotes('')
    setScheduleFollowUp(false)
    setCallFollowDate('')
    setCallFollowTime('')
    setRescheduleNotes('')
  }

  function beginCall(item: DashboardFollowUpItem) {
    const key = taskKey(item)
    const startingStatus = (item.status || '').trim()
    setActionKey(key)
    setActionKind('call')
    setMessage(null)
    setError(null)
    setCallStatus(startingStatus)
    setCallNextSel(
      defaultNextActionForStatus(
        startingStatus,
        catalog,
        matchNextAction(item.next_action, catalog),
      ),
    )
    setCallFollowDate('')
    setCallFollowTime('')
    setScheduleFollowUp(false)
    setCallAssignedUserId(item.assigned_user_id ? String(item.assigned_user_id) : '')
    setCallNotes('')
    setAppointmentDetails(
      emptyAppointmentDetails({
        revenue_specialist_user_id: item.assigned_user_id,
        source: appointmentSourceForStatus(startingStatus),
      }),
    )
  }

  function beginComplete(item: DashboardFollowUpItem) {
    setActionKey(taskKey(item))
    setActionKind('complete')
    setCompleteNote('')
    setMessage(null)
    setError(null)
  }

  function beginReschedule(item: DashboardFollowUpItem) {
    setActionKey(taskKey(item))
    setActionKind('reschedule')
    setRescheduleDate((item.due_date || '').slice(0, 10))
    setRescheduleTime((item.due_time || '').slice(0, 5))
    setRescheduleAssignedUserId(item.assigned_user_id ? String(item.assigned_user_id) : '')
    setRescheduleNextSel(matchNextAction(item.next_action, catalog))
    setRescheduleNotes('')
    setMessage(null)
    setError(null)
  }

  const callNeedsFollowUp = scheduleFollowUp && !statusNeverRequiresFollowUp(callStatus)
  const canSaveCall =
    Boolean(callStatus.trim()) &&
    nextActionIsValid(callNextSel, catalog) &&
    (!callNeedsFollowUp || (Boolean(callFollowDate.trim()) && Boolean(callFollowTime.trim()))) &&
    (!isAppointmentSetStatus(callStatus) || appointmentDetailsAreValid(appointmentDetails))
  const canSaveReschedule =
    Boolean(rescheduleDate.trim()) &&
    Boolean(rescheduleTime.trim()) &&
    nextActionIsValid(rescheduleNextSel, catalog)

  async function saveComplete(item: DashboardFollowUpItem) {
    if (!item.source_id || saving) return
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await completeFollowUpTask({
        client_id: item.client_id,
        source: item.source,
        source_id: item.source_id,
        company_id: item.company_id,
        contact_id: hasValidContact(item) ? item.contact_id : null,
        notes: completeNote,
        created_by: actorName,
      })
      closeAction()
      setMessage(`Completed follow-up for ${display(item.contact_name || item.company_name)}.`)
      await reloadTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not complete the task.')
    } finally {
      setSaving(false)
    }
  }

  async function saveCall(item: DashboardFollowUpItem) {
    if (!canSaveCall || saving || !item.external_record_no) return
    const willSchedule =
      scheduleFollowUp && Boolean(callFollowDate.trim()) && Boolean(callFollowTime.trim())
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await logWorkQueueCall({
        client_id: item.client_id,
        external_record_no: item.external_record_no,
        contact_id: hasValidContact(item) ? item.contact_id : null,
        outcome: callStatus,
        notes: callNotes,
        status: callStatus,
        next_action: storedNextAction(callNextSel, catalog),
        follow_up_date: willSchedule ? callFollowDate : null,
        follow_up_time: willSchedule ? callFollowTime : '',
        queue_source: item.source,
        queue_source_id: item.source_id,
        complete_current: true,
        created_by: actorName,
        appointment: isAppointmentSetStatus(callStatus)
          ? {
              appointment_date: appointmentDetails.appointment_date,
              start_time: appointmentDetails.start_time,
              timezone: appointmentDetails.timezone,
              datetime_tbd: appointmentDetails.datetime_tbd,
              appointment_type: appointmentDetails.appointment_type,
              location_or_link: appointmentDetails.location_or_link,
              revenue_specialist_user_id: appointmentDetails.revenue_specialist_user_id,
              notes: appointmentDetails.notes,
              source: appointmentDetails.source,
              idempotency_key: appointmentDetails.idempotency_key,
            }
          : undefined,
      })
      closeAction()
      setMessage(`Call logged and follow-up completed for ${display(item.company_name)}.`)
      await reloadTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not log the call.')
    } finally {
      setSaving(false)
    }
  }

  async function saveReschedule(item: DashboardFollowUpItem) {
    if (!canSaveReschedule || saving || !item.source_id) return
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      await rescheduleFollowUpTask({
        client_id: item.client_id,
        source: item.source,
        source_id: item.source_id,
        company_id: item.company_id,
        contact_id: hasValidContact(item) ? item.contact_id : null,
        follow_up_date: rescheduleDate,
        follow_up_time: rescheduleTime,
        assigned_user_id: rescheduleAssignedUserId ? Number(rescheduleAssignedUserId) : null,
        next_action: storedNextAction(rescheduleNextSel, catalog),
        notes: rescheduleNotes,
        created_by: actorName,
      })
      closeAction()
      setMessage(`Rescheduled follow-up for ${display(item.contact_name || item.company_name)}.`)
      await reloadTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reschedule the task.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Tasks</h1>
          <p>
            Open follow-up tasks for {activeClientName || 'the Active Client'}. Completing a task
            removes it from Tasks, Work Queue, and Dashboard while keeping the history.
          </p>
        </div>
      </div>

      <section className="panel panel--queue" aria-label="Tasks">
        <div className="panel-header">
          <h2>
            {loading && items.length === 0
              ? 'Open follow-ups'
              : `${items.length} open follow-up${items.length === 1 ? '' : 's'}`}
          </h2>
          <span className="queue-source">
            {scopedClient
              ? `${counts.overdue} overdue · ${counts.dueToday} due today · ${counts.upcoming} upcoming`
              : 'Select a Working For client'}
          </span>
        </div>

        {message ? (
          <p className="save-confirm" role="status">
            {message}
          </p>
        ) : null}
        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : null}

        {!scopedClient ? (
          <p className="empty-state">Select a Working For client to see open follow-up tasks.</p>
        ) : loading && items.length === 0 ? (
          <p className="data-status">Loading tasks…</p>
        ) : items.length === 0 ? (
          <p className="empty-state">No open follow-up tasks for {activeClientName}.</p>
        ) : (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <th>Due</th>
                  <th>Bucket</th>
                  <th>Contact</th>
                  <th>Company</th>
                  <th>Next Action</th>
                  <th>Assigned</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const key = taskKey(item)
                  const openContact = hasValidContact(item)
                  const panelOpen = actionKey === key
                  return (
                    <tr key={key}>
                      <td>{followUpWhen(item)}</td>
                      <td>{bucketLabel(item.bucket)}</td>
                      <td>
                        {openContact ? (
                          <Link className="company-link" to={contactHref(item)}>
                            {display(item.contact_name)}
                          </Link>
                        ) : (
                          display(item.contact_name)
                        )}
                      </td>
                      <td>
                        {item.external_record_no ? (
                          <Link
                            className="company-link"
                            to={companyWorkspaceHref(item.external_record_no, item.client_id)}
                          >
                            {display(item.company_name)}
                          </Link>
                        ) : (
                          display(item.company_name)
                        )}
                        {item.external_record_no ? (
                          <span className="queue-sub">Record No. {item.external_record_no}</span>
                        ) : null}
                      </td>
                      <td>
                        {display(
                          displayNextAction(item.next_action, catalog) || item.next_action,
                        )}
                      </td>
                      <td>{display(item.assigned_user)}</td>
                      <td>
                        <div className="opportunity-actions">
                          {item.external_record_no ? (
                            <Link
                              className="link-btn"
                              to={companyWorkspaceHref(item.external_record_no, item.client_id)}
                            >
                              Open Company
                            </Link>
                          ) : null}
                          {openContact ? (
                            <Link className="link-btn" to={contactHref(item)}>
                              Open Contact
                            </Link>
                          ) : null}
                          {panelOpen && actionKind === 'call' ? (
                            <div className="task-complete-inline task-action-panel add-note-card work-queue-log-call">
                              <h3 className="add-note-card__title">Log Call & Complete</h3>
                              <p className="muted-note">
                                Saving this call logs the conversation, updates this client’s
                                status, and completes the current open follow-up. Check “Schedule
                                another follow-up” only when you want a new follow-up task.
                              </p>
                              <div className="milestone-form-grid">
                                <label className="edit-field">
                                  <span className="edit-field__label">Call outcome / status</span>
                                  <select
                                    className="edit-select"
                                    value={callStatus}
                                    onChange={(e) => {
                                      const nextStatus = e.target.value
                                      setCallStatus(nextStatus)
                                      setCallNextSel((current) =>
                                        defaultNextActionForStatus(nextStatus, catalog, current),
                                      )
                                      if (isAppointmentSetStatus(nextStatus)) {
                                        setAppointmentDetails((current) => ({
                                          ...current,
                                          source: appointmentSourceForStatus(nextStatus),
                                          revenue_specialist_user_id:
                                            current.revenue_specialist_user_id ??
                                            (callAssignedUserId ? Number(callAssignedUserId) : null),
                                        }))
                                      }
                                    }}
                                  >
                                    {!callStatus && <option value="">Select status…</option>}
                                    {statusOptions.map((option) => (
                                      <option key={option} value={option}>
                                        {option}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                <label className="edit-field">
                                  <span className="edit-field__label">Assigned Rep</span>
                                  <select
                                    className="edit-select"
                                    value={callAssignedUserId}
                                    onChange={(e) => setCallAssignedUserId(e.target.value)}
                                  >
                                    <option value="">Unassigned</option>
                                    {reps.map((rep) => (
                                      <option key={rep.user_id} value={rep.user_id}>
                                        {rep.full_name}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                <NextActionFields
                                  catalog={catalog}
                                  value={callNextSel}
                                  onChange={setCallNextSel}
                                  idPrefix={`task-call-${key}`}
                                />
                              </div>
                              {isAppointmentSetStatus(callStatus) ? (
                                <AppointmentDetailsFields
                                  value={appointmentDetails}
                                  onChange={setAppointmentDetails}
                                  reps={reps}
                                  companyName={item.company_name || ''}
                                  contactName={item.contact_name || ''}
                                  idPrefix={`task-call-appt-${key}`}
                                  headingRef={appointmentHeadingRef}
                                />
                              ) : null}
                              <label className="schedule-follow-toggle">
                                <input
                                  type="checkbox"
                                  checked={scheduleFollowUp}
                                  onChange={(e) => {
                                    const checked = e.target.checked
                                    setScheduleFollowUp(checked)
                                    if (!checked) {
                                      setCallFollowDate('')
                                      setCallFollowTime('')
                                    } else if (
                                      !callNextSel.code ||
                                      callNextSel.code === catalog.default_for_closed_code
                                    ) {
                                      setCallNextSel({ code: 'follow_up', custom: '' })
                                    }
                                  }}
                                />
                                Schedule another follow-up.
                              </label>
                              {scheduleFollowUp ? (
                                <div className="milestone-form-grid">
                                  <label className="edit-field">
                                    <span className="edit-field__label">Follow-Up Date</span>
                                    <input
                                      className="edit-input"
                                      type="date"
                                      value={callFollowDate}
                                      onChange={(e) => setCallFollowDate(e.target.value)}
                                      required
                                    />
                                  </label>
                                  <label className="edit-field">
                                    <span className="edit-field__label">Follow-Up Time</span>
                                    <input
                                      className="edit-input"
                                      type="time"
                                      value={callFollowTime}
                                      onChange={(e) => setCallFollowTime(e.target.value)}
                                      required
                                    />
                                  </label>
                                </div>
                              ) : null}
                              <label className="edit-field__label" htmlFor={`task-call-notes-${key}`}>
                                Notes
                              </label>
                              <textarea
                                id={`task-call-notes-${key}`}
                                className="edit-textarea add-note-card__textarea"
                                rows={3}
                                value={callNotes}
                                onChange={(e) => setCallNotes(e.target.value)}
                                placeholder="Call notes (optional)…"
                              />
                              <div className="edit-actions">
                                <button
                                  type="button"
                                  className="primary-btn"
                                  disabled={saving || !canSaveCall}
                                  onClick={() => void saveCall(item)}
                                >
                                  {saving ? 'Saving…' : 'Log Call & Complete'}
                                </button>
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={saving}
                                  onClick={closeAction}
                                >
                                  Cancel
                                </button>
                              </div>
                            </div>
                          ) : !panelOpen ? (
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => beginCall(item)}
                            >
                              Log Call & Complete
                            </button>
                          ) : null}
                          {panelOpen && actionKind === 'complete' ? (
                            <div className="task-complete-inline">
                              <p className="muted-note">
                                Marks this follow-up completed and keeps a completion activity in
                                history.
                              </p>
                              <textarea
                                className="edit-textarea"
                                rows={2}
                                value={completeNote}
                                onChange={(e) => setCompleteNote(e.target.value)}
                                placeholder="Completion note (optional)…"
                              />
                              <div className="edit-actions">
                                <button
                                  type="button"
                                  className="primary-btn"
                                  disabled={saving}
                                  onClick={() => void saveComplete(item)}
                                >
                                  {saving ? 'Saving…' : 'Confirm Complete'}
                                </button>
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={saving}
                                  onClick={closeAction}
                                >
                                  Cancel
                                </button>
                              </div>
                            </div>
                          ) : !panelOpen ? (
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => beginComplete(item)}
                            >
                              Complete Task
                            </button>
                          ) : null}
                          {panelOpen && actionKind === 'reschedule' ? (
                            <div className="task-complete-inline task-action-panel">
                              <p className="muted-note">
                                Updates this same follow-up. It does not create a duplicate task.
                              </p>
                              <div className="milestone-form-grid">
                                <label className="edit-field">
                                  <span className="edit-field__label">Follow-Up Date</span>
                                  <input
                                    className="edit-input"
                                    type="date"
                                    value={rescheduleDate}
                                    onChange={(e) => setRescheduleDate(e.target.value)}
                                    required
                                  />
                                </label>
                                <label className="edit-field">
                                  <span className="edit-field__label">Follow-Up Time</span>
                                  <input
                                    className="edit-input"
                                    type="time"
                                    value={rescheduleTime}
                                    onChange={(e) => setRescheduleTime(e.target.value)}
                                    required
                                  />
                                </label>
                                <label className="edit-field">
                                  <span className="edit-field__label">Assigned Rep</span>
                                  <select
                                    className="edit-select"
                                    value={rescheduleAssignedUserId}
                                    onChange={(e) => setRescheduleAssignedUserId(e.target.value)}
                                  >
                                    <option value="">Keep current</option>
                                    {reps.map((rep) => (
                                      <option key={rep.user_id} value={rep.user_id}>
                                        {rep.full_name}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                <NextActionFields
                                  catalog={catalog}
                                  value={rescheduleNextSel}
                                  onChange={setRescheduleNextSel}
                                  idPrefix={`task-reschedule-${key}`}
                                />
                              </div>
                              <textarea
                                className="edit-textarea"
                                rows={2}
                                value={rescheduleNotes}
                                onChange={(e) => setRescheduleNotes(e.target.value)}
                                placeholder="Reschedule notes (optional)…"
                              />
                              <div className="edit-actions">
                                <button
                                  type="button"
                                  className="primary-btn"
                                  disabled={saving || !canSaveReschedule}
                                  onClick={() => void saveReschedule(item)}
                                >
                                  {saving ? 'Saving…' : 'Save Reschedule'}
                                </button>
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={saving}
                                  onClick={closeAction}
                                >
                                  Cancel
                                </button>
                              </div>
                            </div>
                          ) : !panelOpen ? (
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => beginReschedule(item)}
                            >
                              Reschedule
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}
