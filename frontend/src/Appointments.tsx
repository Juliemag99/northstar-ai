import { Link, useSearchParams } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import {
  cancelAppointment,
  completeAppointment,
  companyWorkspaceHref,
  fetchAppointments,
  fetchClientSalesEvents,
  fetchClientStatuses,
  fetchNextActions,
  rescheduleAppointment,
  type AppointmentRecord,
} from './api/carmeco'
import { useAuth } from './auth/useAuth'
import { authenticatedActorName } from './auth/reportDefaults'
import AppointmentDetailsFields from './AppointmentDetailsFields'
import NextActionFields from './NextActionFields'
import {
  APPOINTMENT_GRADES,
  APPOINTMENT_OUTCOMES,
  APPOINTMENT_SOURCES,
  APPOINTMENT_TYPES,
  appointmentDetailsAreValid,
  emptyAppointmentDetails,
  formatAppointmentWhen,
  type AppointmentDetails,
} from './appointmentSet'
import {
  defaultNextActionForStatus,
  emptyNextActionCatalog,
  nextActionIsValid,
  storedNextAction,
  type NextActionCatalog,
  type NextActionSelection,
} from './nextAction'

type Bucket = 'upcoming' | 'today' | 'past' | 'cancelled'

const BUCKETS: Array<{ id: Bucket; label: string }> = [
  { id: 'upcoming', label: 'Upcoming' },
  { id: 'today', label: 'Today' },
  { id: 'past', label: 'Past' },
  { id: 'cancelled', label: 'Cancelled' },
]

function display(value: string | null | undefined): string {
  return (value || '').trim() || '—'
}

function contactHref(item: AppointmentRecord): string {
  if (item.contact_id != null && item.contact_id > 0) {
    return `/contacts/${item.contact_id}?client_id=${item.client_id}`
  }
  if (item.company_record_no) {
    return companyWorkspaceHref(item.company_record_no, item.client_id)
  }
  return '#'
}

function parseBucket(value: string | null): Bucket {
  if (value === 'today' || value === 'past' || value === 'cancelled' || value === 'upcoming') {
    return value
  }
  return 'upcoming'
}

export default function Appointments({
  activeClientId,
  activeClientName,
  onAppointmentsChanged,
}: {
  activeClientId: number | null
  activeClientName: string
  onAppointmentsChanged?: () => void
}) {
  const { user } = useAuth()
  const actorName = authenticatedActorName(user)
  const [searchParams, setSearchParams] = useSearchParams()
  const bucket = parseBucket(searchParams.get('filter'))
  const scoped = activeClientId != null && activeClientId > 0
  const [items, setItems] = useState<AppointmentRecord[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [savingId, setSavingId] = useState<number | null>(null)
  const [actionFor, setActionFor] = useState<AppointmentRecord | null>(null)
  const [actionKind, setActionKind] = useState<'reschedule' | 'cancel' | 'complete' | null>(null)
  const [reschedule, setReschedule] = useState<AppointmentDetails>(emptyAppointmentDetails())
  const [cancelNotes, setCancelNotes] = useState('')
  const [cancelStatus, setCancelStatus] = useState('')
  const [cancelNextSel, setCancelNextSel] = useState<NextActionSelection>({ code: '', custom: '' })
  const [cancelFollowDate, setCancelFollowDate] = useState('')
  const [cancelFollowTime, setCancelFollowTime] = useState('')
  const [statusOptions, setStatusOptions] = useState<string[]>([])
  const [nextActionCatalog, setNextActionCatalog] = useState<NextActionCatalog>(emptyNextActionCatalog())
  const [grade, setGrade] = useState('')
  const [outcome, setOutcome] = useState('')
  const [followUpNotes, setFollowUpNotes] = useState('')
  const [imported, setImported] = useState<Array<Record<string, unknown>>>([])
  const [importedFilters, setImportedFilters] = useState({
    rev_spec: '',
    company_name: '',
    contact_name: '',
    appointment_grade: '',
    outcome: '',
    date_from: '',
    date_to: '',
  })

  function setBucket(next: Bucket) {
    const params = new URLSearchParams(searchParams)
    if (next === 'upcoming') params.delete('filter')
    else params.set('filter', next)
    setSearchParams(params, { replace: true })
  }

  function reload() {
    if (!scoped || activeClientId == null) {
      setItems([])
      setCounts({})
      return
    }
    setLoading(true)
    setError(null)
    void fetchAppointments(activeClientId, {
      bucket,
      q: query.trim() || undefined,
      appointment_type: typeFilter || undefined,
      source: sourceFilter || undefined,
      limit: 200,
    })
      .then((data) => {
        setItems(data.items)
        setCounts(data.counts || {})
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Failed to load appointments.')
        setItems([])
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeClientId, bucket, query, typeFilter, sourceFilter])

  useEffect(() => {
    if (!scoped || activeClientId == null) {
      setStatusOptions([])
      setNextActionCatalog(emptyNextActionCatalog())
      return
    }
    let cancelled = false
    void Promise.all([fetchClientStatuses(activeClientId), fetchNextActions(activeClientId)])
      .then(([statuses, catalog]) => {
        if (cancelled) return
        setStatusOptions(statuses)
        setNextActionCatalog(catalog)
      })
      .catch(() => {
        if (!cancelled) {
          setStatusOptions([])
          setNextActionCatalog(emptyNextActionCatalog())
        }
      })
    return () => {
      cancelled = true
    }
  }, [scoped, activeClientId])

  useEffect(() => {
    if (!scoped || activeClientId == null) {
      setImported([])
      return
    }
    let cancelled = false
    void fetchClientSalesEvents(activeClientId, {
      event_family: 'appointment',
      rev_spec: importedFilters.rev_spec || undefined,
      company_name: importedFilters.company_name || undefined,
      contact_name: importedFilters.contact_name || undefined,
      appointment_grade: importedFilters.appointment_grade || undefined,
      outcome: importedFilters.outcome || undefined,
      date_from: importedFilters.date_from || undefined,
      date_to: importedFilters.date_to || undefined,
      limit: 200,
    })
      .then((rows) => {
        if (!cancelled) setImported(rows)
      })
      .catch(() => {
        if (!cancelled) setImported([])
      })
    return () => {
      cancelled = true
    }
  }, [scoped, activeClientId, importedFilters])

  const emptyLabel = useMemo(() => {
    if (bucket === 'today') return 'No appointments scheduled for today.'
    if (bucket === 'past') return 'No past appointments.'
    if (bucket === 'cancelled') return 'No cancelled appointments.'
    return 'No upcoming appointments.'
  }, [bucket])

  function beginAction(item: AppointmentRecord, kind: 'reschedule' | 'cancel' | 'complete') {
    setActionFor(item)
    setActionKind(kind)
    setMessage(null)
    setError(null)
    setCancelNotes('')
    setCancelStatus('')
    setCancelNextSel({ code: '', custom: '' })
    setCancelFollowDate('')
    setCancelFollowTime('')
    setGrade(item.grade || '')
    setOutcome(item.outcome || '')
    setFollowUpNotes(item.follow_up_notes || '')
    setReschedule(
      emptyAppointmentDetails({
        appointment_date: item.appointment_date,
        start_time: (item.start_time || '').slice(0, 5),
        timezone: item.timezone || 'America/Chicago',
        datetime_tbd: Boolean(item.datetime_tbd),
        appointment_type: item.appointment_type || 'Phone',
        location_or_link: item.location_or_link,
        revenue_specialist_user_id: item.revenue_specialist_user_id,
        notes: item.notes,
        source: item.source || 'Phone Call',
      }),
    )
  }

  async function saveAction() {
    if (!scoped || activeClientId == null || !actionFor || !actionKind || savingId) {
      if (!scoped) setError('Choose a specific client before saving. All My Clients cannot be used for writes.')
      return
    }
    if (actionKind === 'reschedule' && !appointmentDetailsAreValid(reschedule)) {
      setError('Appointment date and time are required, or choose Date/Time TBD.')
      return
    }
    if (actionKind === 'cancel') {
      if (!cancelNotes.trim()) {
        setError('A cancellation reason is required.')
        return
      }
      if (!cancelStatus.trim()) {
        setError('Select the contact’s new client status after cancellation.')
        return
      }
      if (!nextActionIsValid(cancelNextSel, nextActionCatalog)) {
        setError('Enter a valid next action, or leave it blank.')
        return
      }
    }
    setSavingId(actionFor.id)
    setError(null)
    try {
      if (actionKind === 'reschedule') {
        await rescheduleAppointment(actionFor.id, {
          client_id: activeClientId,
          appointment_date: reschedule.appointment_date,
          start_time: reschedule.start_time,
          timezone: reschedule.timezone,
          datetime_tbd: reschedule.datetime_tbd,
          appointment_type: reschedule.appointment_type,
          location_or_link: reschedule.location_or_link,
          revenue_specialist_user_id: reschedule.revenue_specialist_user_id,
          notes: reschedule.notes,
          source: reschedule.source,
        })
        setMessage('Appointment rescheduled.')
      } else if (actionKind === 'cancel') {
        await cancelAppointment(actionFor.id, {
          client_id: activeClientId,
          reason: cancelNotes.trim(),
          notes: cancelNotes.trim(),
          new_status: cancelStatus,
          next_action: storedNextAction(cancelNextSel, nextActionCatalog),
          follow_up_date: cancelFollowDate || null,
          follow_up_time: cancelFollowTime,
          created_by: actorName,
        })
        setMessage('Appointment cancelled.')
      } else {
        await completeAppointment(actionFor.id, {
          client_id: activeClientId,
          grade,
          outcome,
          follow_up_notes: followUpNotes,
        })
        setMessage('Appointment marked complete.')
      }
      setActionFor(null)
      setActionKind(null)
      reload()
      onAppointmentsChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update appointment.')
    } finally {
      setSavingId(null)
    }
  }

  if (!scoped) {
    return (
      <div className="page-heading">
        <h1>Appointments</h1>
        <p>Select an Active Client to view scheduled appointments.</p>
      </div>
    )
  }

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Appointments</h1>
          <p>{activeClientName} live appointments with dates, contacts, and outcomes.</p>
          {!scoped ? (
            <p className="data-status" role="status">
              Choose a specific client before saving. All My Clients cannot be used for writes.
            </p>
          ) : null}
        </div>
      </div>

      <section className="panel panel--queue" aria-label="Appointments">
        <div className="panel-header">
          <h2>
            {items.length} appointment{items.length === 1 ? '' : 's'}
          </h2>
          <span className="queue-source">Active Client · live appointment records</span>
        </div>
        <div className="appt-tabs" role="tablist" aria-label="Appointment buckets">
          {BUCKETS.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              className={`appt-tab${bucket === item.id ? ' is-active' : ''}`}
              aria-selected={bucket === item.id}
              onClick={() => setBucket(item.id)}
            >
              {item.label} ({counts[item.id] ?? 0})
            </button>
          ))}
        </div>
        <div className="milestone-form-grid" style={{ margin: '0.75rem 0' }}>
          <label className="edit-field">
            <span className="edit-field__label">Search</span>
            <input
              className="edit-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Company, contact, notes…"
            />
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Type</span>
            <select
              className="edit-select"
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
            >
              <option value="">All types</option>
              {APPOINTMENT_TYPES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Source</span>
            <select
              className="edit-select"
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
            >
              <option value="">All sources</option>
              {APPOINTMENT_SOURCES.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
        </div>
        {message ? <p className="muted-note">{message}</p> : null}
        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : null}
        {loading ? <p className="data-status">Loading appointments…</p> : null}
        {!loading && items.length === 0 ? <p className="empty-state">{emptyLabel}</p> : null}
        {!loading && items.length > 0 ? (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Company</th>
                  <th>Contact</th>
                  <th>Type</th>
                  <th>RDS</th>
                  <th>Status</th>
                  <th>Grade</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      {formatAppointmentWhen(item)}
                      <span className="queue-sub">{display(item.source)}</span>
                      {item.status === 'cancelled' ? (
                        <span className="queue-sub">
                          Reason: {display(item.cancellation_reason)}
                          {item.cancelled_by
                            ? ` · ${item.cancelled_by}${item.cancelled_at ? ` · ${item.cancelled_at}` : ''}`
                            : ''}
                        </span>
                      ) : null}
                    </td>
                    <td>
                      {item.company_record_no ? (
                        <Link
                          to={companyWorkspaceHref(item.company_record_no, item.client_id)}
                          className="company-link"
                        >
                          {display(item.company_name)}
                        </Link>
                      ) : (
                        display(item.company_name)
                      )}
                    </td>
                    <td>
                      {item.contact_id && item.contact_id > 0 ? (
                        <Link to={contactHref(item)}>{display(item.contact_name)}</Link>
                      ) : (
                        display(item.contact_name)
                      )}
                    </td>
                    <td>{display(item.appointment_type)}</td>
                    <td>{display(item.revenue_specialist_name)}</td>
                    <td>{display(item.status)}</td>
                    <td>{display(item.grade || item.outcome)}</td>
                    <td>
                      {item.status === 'scheduled' ? (
                        <div className="workflow-action-row">
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => beginAction(item, 'reschedule')}
                          >
                            Reschedule
                          </button>
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => beginAction(item, 'complete')}
                          >
                            Mark Complete
                          </button>
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => beginAction(item, 'cancel')}
                          >
                            Cancel
                          </button>
                        </div>
                      ) : item.status === 'completed' ? (
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() => beginAction(item, 'complete')}
                        >
                          Edit outcome
                        </button>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {actionFor && actionKind ? (
          <div className="add-note-card" style={{ marginTop: '1rem' }}>
            <h3 className="add-note-card__title">
              {actionKind === 'reschedule'
                ? 'Reschedule appointment'
                : actionKind === 'cancel'
                  ? 'Cancel appointment'
                  : 'Appointment outcome'}
            </h3>
            {actionKind === 'reschedule' ? (
              <AppointmentDetailsFields
                value={reschedule}
                onChange={setReschedule}
                reps={
                  actionFor.revenue_specialist_user_id
                    ? [
                        {
                          user_id: actionFor.revenue_specialist_user_id,
                          full_name: actionFor.revenue_specialist_name || 'Assigned RDS',
                        },
                      ]
                    : []
                }
                companyName={actionFor.company_name}
                contactName={actionFor.contact_name}
                idPrefix="appt-reschedule"
              />
            ) : null}
            {actionKind === 'cancel' ? (
              <div className="milestone-form-grid">
                <label className="edit-field" style={{ gridColumn: '1 / -1' }}>
                  <span className="edit-field__label">Cancellation reason</span>
                  <textarea
                    className="edit-textarea add-note-card__textarea"
                    rows={3}
                    value={cancelNotes}
                    onChange={(e) => setCancelNotes(e.target.value)}
                    placeholder="Why is this appointment being cancelled?"
                    required
                  />
                </label>
                <label className="edit-field">
                  <span className="edit-field__label">New client status</span>
                  <select
                    className="edit-select"
                    value={cancelStatus}
                    onChange={(e) => {
                      const nextStatus = e.target.value
                      setCancelStatus(nextStatus)
                      setCancelNextSel((current) =>
                        defaultNextActionForStatus(nextStatus, nextActionCatalog, current),
                      )
                    }}
                    required
                  >
                    <option value="">Select new status…</option>
                    {statusOptions.map((status) => (
                      <option key={status} value={status}>
                        {status}
                      </option>
                    ))}
                  </select>
                </label>
                <NextActionFields
                  catalog={nextActionCatalog}
                  value={cancelNextSel}
                  onChange={setCancelNextSel}
                  idPrefix="appt-cancel"
                />
                <label className="edit-field">
                  <span className="edit-field__label">Follow-up date (optional)</span>
                  <input
                    className="edit-input"
                    type="date"
                    value={cancelFollowDate}
                    onChange={(e) => setCancelFollowDate(e.target.value)}
                  />
                </label>
                <label className="edit-field">
                  <span className="edit-field__label">Follow-up time (optional)</span>
                  <input
                    className="edit-input"
                    type="time"
                    value={cancelFollowTime}
                    onChange={(e) => setCancelFollowTime(e.target.value)}
                  />
                </label>
                <p className="muted-note" style={{ gridColumn: '1 / -1' }}>
                  Cancelling does not return this contact to Hot Prospect unless you choose that
                  status.
                </p>
              </div>
            ) : null}
            {actionKind === 'complete' ? (
              <div className="milestone-form-grid">
                <label className="edit-field">
                  <span className="edit-field__label">Appointment Grade</span>
                  <select
                    className="edit-select"
                    value={grade}
                    onChange={(e) => setGrade(e.target.value)}
                  >
                    <option value="">Select grade…</option>
                    {APPOINTMENT_GRADES.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="edit-field">
                  <span className="edit-field__label">Outcome</span>
                  <select
                    className="edit-select"
                    value={outcome}
                    onChange={(e) => setOutcome(e.target.value)}
                  >
                    <option value="">Select outcome…</option>
                    {APPOINTMENT_OUTCOMES.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="edit-field" style={{ gridColumn: '1 / -1' }}>
                  <span className="edit-field__label">Follow-up notes</span>
                  <textarea
                    className="edit-textarea add-note-card__textarea"
                    rows={3}
                    value={followUpNotes}
                    onChange={(e) => setFollowUpNotes(e.target.value)}
                  />
                </label>
              </div>
            ) : null}
            <div className="edit-actions">
              <button
                type="button"
                className="primary-btn"
                disabled={
                  savingId != null ||
                  (actionKind === 'cancel' &&
                    (!cancelNotes.trim() ||
                      !cancelStatus.trim() ||
                      !nextActionIsValid(cancelNextSel, nextActionCatalog)))
                }
                onClick={() => void saveAction()}
              >
                {savingId != null ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                className="link-btn"
                onClick={() => {
                  setActionFor(null)
                  setActionKind(null)
                }}
              >
                Close
              </button>
            </div>
          </div>
        ) : null}
      </section>

      <section className="panel panel--queue" aria-label="Imported appointment events">
        <div className="panel-header">
          <h2>Imported Appointment Events ({imported.length})</h2>
          <span className="queue-source">From Client Knowledge Hub · excludes Send Information</span>
        </div>
        <div className="setup-grid" style={{ marginBottom: '0.75rem' }}>
          {(
            [
              ['rev_spec', 'Revenue Specialist'],
              ['company_name', 'Company'],
              ['contact_name', 'Contact'],
              ['appointment_grade', 'Appointment Grade'],
              ['outcome', 'Outcome'],
              ['date_from', 'Date from'],
              ['date_to', 'Date to'],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="setup-field">
              <span className="setup-field-label">{label}</span>
              <input
                value={importedFilters[key]}
                onChange={(e) => setImportedFilters((p) => ({ ...p, [key]: e.target.value }))}
                placeholder={label}
              />
            </label>
          ))}
        </div>
        {imported.length === 0 ? (
          <p className="empty-state">
            No imported appointment events yet. Upload and confirm an appointment workbook under
            Client Knowledge → Appointments.
          </p>
        ) : (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Event</th>
                  <th>Company</th>
                  <th>Contact</th>
                  <th>Rev Spec</th>
                  <th>Grade</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {imported.map((ev) => {
                  const recordNo = String(ev.source_record_number || '')
                  const contactId = Number(ev.contact_id || 0)
                  return (
                    <tr key={String(ev.event_id)}>
                      <td>
                        {display(String(ev.event_date || ev.source_date_time_text || ''))}
                      </td>
                      <td>{display(String(ev.event_type || ''))}</td>
                      <td>
                        {recordNo ? (
                          <Link
                            to={companyWorkspaceHref(recordNo, activeClientId)}
                            className="company-link"
                          >
                            {display(String(ev.company_name || ''))}
                          </Link>
                        ) : (
                          display(String(ev.company_name || ''))
                        )}
                      </td>
                      <td>
                        {contactId > 0 ? (
                          <Link to={`/contacts/${contactId}?client_id=${activeClientId}`}>
                            {display(String(ev.contact_name || ''))}
                          </Link>
                        ) : (
                          display(String(ev.contact_name || ''))
                        )}
                      </td>
                      <td>{display(String(ev.source_rev_spec_text || ''))}</td>
                      <td>{display(String(ev.source_appointment_grade || ''))}</td>
                      <td>
                        {display(String(ev.outcome_normalized || ev.source_outcome || ''))}
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
