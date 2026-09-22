/** Contact Workspace page — master person + client-scoped operational workflow. */
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  assignSharedContact,
  completeContactFollowUp,
  createContactActivity,
  fetchClientStatuses,
  fetchContactWorkspace,
  fetchNextActions,
  isCampaignRouteStatus,
  rescheduleContactFollowUp,
  updateContactWorkflow,
  type ContactTimelineItem,
  type ContactWorkspace as ContactWorkspaceData,
} from './api/carmeco'
import NextActionFields from './NextActionFields'
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
import SendEmailCompose from './SendEmailCompose'
import CampaignRoutePrompt, { type CampaignRouteOffer } from './CampaignRoutePrompt'
import AppointmentDetailsFields from './AppointmentDetailsFields'
import {
  APPOINTMENT_SET_STATUSES,
  appointmentDetailsAreValid,
  appointmentSourceForStatus,
  emptyAppointmentDetails,
  isAppointmentSetStatus,
  type AppointmentDetails,
} from './appointmentSet'
import {
  ASK_NORTHSTAR_PATH,
  isFromAskNorthStar,
} from './askNorthStarReturn'
import {
  isFromWorkQueue,
  workspaceReturnLabel,
  workspaceReturnPath,
} from './workspaceReturn'
import { SELECT_CLIENT_FOR_WRITE, requireWriteClientId } from './writeClient'
import ZoomInfoUpdateModal from './ZoomInfoUpdateModal'

const WORKSPACE_USER = 'Julie Magnani'

function display(value: string | null | undefined): string {
  const t = (value || '').trim()
  return t || '—'
}

function timeForInput(value: string | null | undefined): string {
  const t = (value || '').trim()
  if (t.length >= 5) return t.slice(0, 5)
  return ''
}

function followUpDisplay(dateValue: string | null | undefined, timeValue: string | null | undefined): string {
  const datePart = (dateValue || '').trim().slice(0, 10)
  const timePart = timeForInput(timeValue)
  if (!datePart && !timePart) return '—'
  if (datePart && timePart) return `${datePart} ${timePart}`
  return datePart || timePart
}

function statusNeverRequiresFollowUp(status: string): boolean {
  const key = status.trim().toLowerCase()
  if (!key) return false
  if (key.includes('do not call') || key === 'dnc') return true
  return key === 'closed' || key.startsWith('closed')
}

type ActionKind = 'call' | 'call-complete' | 'note' | 'follow-up' | 'complete-task' | 'reschedule' | null

export default function ContactWorkspacePage({
  contactId,
  activeClientId = null,
  activeClientName = '',
  onWorkflowSaved,
}: {
  contactId: number
  activeClientId?: number | null
  activeClientName?: string
  onWorkflowSaved?: (update: {
    client_id: number
    company_record_no: string
    status: string
    next_action: string
    follow_up_date: string
  }) => void
}) {
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const fromAskNorthStar = isFromAskNorthStar(searchParams.get('from'))
  const clientIdParam = searchParams.get('client_id')
  const clientIdFromUrl = clientIdParam ? Number(clientIdParam) : null
  const clientId =
    clientIdFromUrl != null && Number.isFinite(clientIdFromUrl) && clientIdFromUrl > 0
      ? clientIdFromUrl
      : activeClientId != null && activeClientId > 0
        ? activeClientId
        : null
  const [data, setData] = useState<ContactWorkspaceData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saveNotice] = useState<string | null>(() => {
    const state = location.state as { manualContactNotice?: string } | null
    return state?.manualContactNotice?.trim() || null
  })
  const [zoomInfoOpen, setZoomInfoOpen] = useState(false)
  const [zoomInfoMsg, setZoomInfoMsg] = useState<string | null>(null)
  const [sendEmailOpen, setSendEmailOpen] = useState(false)
  const [campaignRouteOffer, setCampaignRouteOffer] = useState<CampaignRouteOffer | null>(null)
  const [statuses, setStatuses] = useState<string[]>([])
  const [nextActionCatalog, setNextActionCatalog] = useState<NextActionCatalog>(emptyNextActionCatalog())
  const [status, setStatus] = useState('')
  const [assignedUserId, setAssignedUserId] = useState('')
  const [nextSel, setNextSel] = useState<NextActionSelection>({ code: '', custom: '' })
  const [followUpDate, setFollowUpDate] = useState('')
  const [followUpTime, setFollowUpTime] = useState('')
  const [savingWorkflow, setSavingWorkflow] = useState(false)
  const [workflowMsg, setWorkflowMsg] = useState<string | null>(null)
  const [workflowError, setWorkflowError] = useState<string | null>(null)
  const [editingWorkflow, setEditingWorkflow] = useState(false)
  const [actionKind, setActionKind] = useState<ActionKind>(null)
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
  const [noteText, setNoteText] = useState('')
  const [followNotes, setFollowNotes] = useState('')
  const [followDate, setFollowDate] = useState('')
  const [followTime, setFollowTime] = useState('')
  const [followNextSel, setFollowNextSel] = useState<NextActionSelection>({
    code: 'follow_up',
    custom: '',
  })
  const [completeNote, setCompleteNote] = useState('')
  const [rescheduleDate, setRescheduleDate] = useState('')
  const [rescheduleTime, setRescheduleTime] = useState('')
  const [actionSaving, setActionSaving] = useState(false)
  const [actionMsg, setActionMsg] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [assignSaving, setAssignSaving] = useState(false)
  const [assignError, setAssignError] = useState<string | null>(null)
  const [confirmCompany, setConfirmCompany] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      setData(null)
      setAssignError(null)
      setConfirmCompany(false)
      setEditingWorkflow(false)
      setActionKind(null)
      try {
        const next = await fetchContactWorkspace(contactId, clientId)
        if (!cancelled) applyWorkspace(next)
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load contact.')
          setData(null)
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [contactId, clientId])

  useEffect(() => {
    const workingId = data?.client_id || clientId || 0
    if (!workingId) {
      setStatuses([])
      setNextActionCatalog(emptyNextActionCatalog())
      return
    }
    let cancelled = false
    void fetchClientStatuses(workingId)
      .then((list) => {
        if (!cancelled) setStatuses(list)
      })
      .catch(() => {
        if (!cancelled) setStatuses([])
      })
    void fetchNextActions(workingId)
      .then((catalog) => {
        if (!cancelled) setNextActionCatalog(catalog)
      })
      .catch(() => {
        if (!cancelled) setNextActionCatalog(emptyNextActionCatalog())
      })
    return () => {
      cancelled = true
    }
  }, [data?.client_id, clientId])

  useEffect(() => {
    if (!data || nextActionCatalog.choices.length === 0) return
    setNextSel(
      defaultNextActionForStatus(
        data.status || '',
        nextActionCatalog,
        matchNextAction(data.next_action, nextActionCatalog),
      ),
    )
  }, [data, nextActionCatalog])

  useEffect(() => {
    if (actionKind !== 'call' && actionKind !== 'call-complete') return
    if (!isAppointmentSetStatus(callStatus)) return
    const frame = window.requestAnimationFrame(() => {
      appointmentHeadingRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [actionKind, callStatus])

  function applyWorkspace(next: ContactWorkspaceData) {
    setData(next)
    setStatus((next.status || '').trim())
    setAssignedUserId(next.assigned_user_id ? String(next.assigned_user_id) : '')
    setNextSel(
      defaultNextActionForStatus(
        next.status || '',
        nextActionCatalog,
        matchNextAction(next.next_action, nextActionCatalog),
      ),
    )
    setFollowUpDate((next.follow_up_date || '').slice(0, 10))
    setFollowUpTime(timeForInput(next.follow_up_time))
  }

  function notifyClientViews(next: ContactWorkspaceData) {
    if (!onWorkflowSaved || !next.client_id) return
    onWorkflowSaved({
      client_id: next.client_id,
      company_record_no: next.company_record_no,
      status: next.status || '',
      next_action: next.next_action || '',
      follow_up_date: next.follow_up_date || '',
    })
  }

  async function saveWorkflow() {
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch {
      setWorkflowError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (savingWorkflow) return
    setSavingWorkflow(true)
    setWorkflowMsg(null)
    setWorkflowError(null)
    try {
      const result = await updateContactWorkflow(contactId, {
        client_id: writeId,
        status,
        assigned_user_id: assignedUserId ? Number(assignedUserId) : 0,
        next_action: storedNextAction(nextSel, nextActionCatalog),
        follow_up_date: followUpDate,
        follow_up_time: followUpTime,
        user: WORKSPACE_USER,
      })
      applyWorkspace(result.workspace)
      notifyClientViews(result.workspace)
      setWorkflowMsg(result.message || 'Workflow saved.')
      setEditingWorkflow(false)
      if (isCampaignRouteStatus(status) && data?.company_id && writeId > 0) {
        setCampaignRouteOffer({
          clientId: writeId,
          companyId: data.company_id ?? 0,
          contactId: contactId,
          source: 'status',
        })
      }
    } catch (err) {
      setWorkflowError(err instanceof Error ? err.message : 'Failed to save workflow.')
    } finally {
      setSavingWorkflow(false)
    }
  }

  async function saveAction() {
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch {
      setActionError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (!actionKind || actionSaving) return
    setActionSaving(true)
    setActionMsg(null)
    setActionError(null)
    try {
      const isCall = actionKind === 'call' || actionKind === 'call-complete'
      let result
      if (actionKind === 'complete-task') {
        result = await completeContactFollowUp(contactId, {
          client_id: writeId,
          notes: completeNote,
          created_by: WORKSPACE_USER,
        })
      } else if (actionKind === 'reschedule') {
        result = await rescheduleContactFollowUp(contactId, {
          client_id: writeId,
          follow_up_date: rescheduleDate,
          follow_up_time: rescheduleTime,
          created_by: WORKSPACE_USER,
        })
      } else {
        const willSchedule =
          isCall &&
          scheduleFollowUp &&
          Boolean(callFollowDate.trim()) &&
          Boolean(callFollowTime.trim())
        const body =
          isCall
            ? {
                client_id: writeId,
                activity_type: 'Call',
                status: callStatus,
                outcome: callStatus,
                notes: callNotes,
                next_action: storedNextAction(callNextSel, nextActionCatalog),
                follow_up_date: willSchedule ? callFollowDate : '',
                follow_up_time: willSchedule ? callFollowTime : '',
                assigned_user_id: callAssignedUserId ? Number(callAssignedUserId) : 0,
                schedule_follow_up: willSchedule,
                created_by: WORKSPACE_USER,
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
              }
            : actionKind === 'note'
              ? {
                  client_id: writeId,
                  activity_type: 'Note',
                  notes: noteText,
                  created_by: WORKSPACE_USER,
                }
              : {
                  client_id: writeId,
                  activity_type: 'Follow-Up',
                  notes: followNotes,
                  next_action: storedNextAction(followNextSel, nextActionCatalog) || 'Follow-Up',
                  follow_up_date: followDate,
                  follow_up_time: followTime || null,
                  assigned_user_id: assignedUserId ? Number(assignedUserId) : 0,
                  created_by: WORKSPACE_USER,
                }
        result = await createContactActivity(contactId, body)
      }
      applyWorkspace(result.workspace)
      notifyClientViews(result.workspace)
      setActionMsg(result.message || 'Saved.')
      const routedStatus = callStatus
      const routedKind = actionKind
      setActionKind(null)
      setCallStatus('')
      setCallNotes('')
      setCallNextSel({ code: '', custom: '' })
      setCallFollowDate('')
      setCallFollowTime('')
      setScheduleFollowUp(false)
      setCallAssignedUserId('')
      setAppointmentDetails(emptyAppointmentDetails())
      setNoteText('')
      setFollowNotes('')
      setFollowDate('')
      setFollowTime('')
      setFollowNextSel({ code: 'follow_up', custom: '' })
      setCompleteNote('')
      setRescheduleDate('')
      setRescheduleTime('')
      if (
        data?.company_id &&
        writeId > 0 &&
        (routedKind === 'call' || routedKind === 'call-complete') &&
        (isAppointmentSetStatus(routedStatus) || isCampaignRouteStatus(routedStatus))
      ) {
        setCampaignRouteOffer({
          clientId: writeId,
          companyId: data.company_id ?? 0,
          contactId: contactId,
          source: isAppointmentSetStatus(routedStatus) ? 'appointment' : 'status',
        })
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to save activity.')
    } finally {
      setActionSaving(false)
    }
  }

  function cancelEditWorkflow() {
    if (data) applyWorkspace(data)
    setEditingWorkflow(false)
    setWorkflowMsg(null)
    setWorkflowError(null)
  }

  function beginCall(completeCurrent = false) {
    setEditingWorkflow(false)
    setActionKind(completeCurrent ? 'call-complete' : 'call')
    setActionMsg(null)
    setActionError(null)
    const startingStatus = (data?.status || status || '').trim()
    setCallStatus(startingStatus)
    setCallNextSel(
      defaultNextActionForStatus(
        startingStatus,
        nextActionCatalog,
        matchNextAction(data?.next_action, nextActionCatalog),
      ),
    )
    setCallFollowDate('')
    setCallFollowTime('')
    setScheduleFollowUp(false)
    setCallAssignedUserId(
      data?.assigned_user_id ? String(data.assigned_user_id) : assignedUserId,
    )
    setCallNotes('')
    setAppointmentDetails(
      emptyAppointmentDetails({
        revenue_specialist_user_id: data?.assigned_user_id ?? null,
        source: appointmentSourceForStatus(startingStatus),
      }),
    )
  }

  function beginCompleteTask() {
    setEditingWorkflow(false)
    setActionKind('complete-task')
    setActionMsg(null)
    setActionError(null)
    setCompleteNote('')
  }

  function beginReschedule() {
    const open = data?.open_follow_up
    setEditingWorkflow(false)
    setActionKind('reschedule')
    setActionMsg(null)
    setActionError(null)
    setRescheduleDate((open?.due_date || data?.follow_up_date || '').slice(0, 10))
    setRescheduleTime(timeForInput(open?.due_time || data?.follow_up_time))
  }

  function beginNote() {
    setEditingWorkflow(false)
    setActionKind('note')
    setActionMsg(null)
    setActionError(null)
  }

  function beginFollowUp() {
    setEditingWorkflow(false)
    setActionKind('follow-up')
    setActionMsg(null)
    setActionError(null)
    const matched = matchNextAction(data?.next_action || 'Follow-Up', nextActionCatalog)
    setFollowNextSel(matched.code ? matched : { code: 'follow_up', custom: '' })
    setFollowDate((data?.follow_up_date || followUpDate || '').slice(0, 10))
    setFollowTime(timeForInput(data?.follow_up_time || followUpTime))
    setFollowNotes('')
  }

  async function addToActiveClient(addCompany: boolean) {
    let writeId: number
    try {
      writeId = requireWriteClientId(clientId)
    } catch {
      setAssignError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (assignSaving) return
    setAssignSaving(true)
    setAssignError(null)
    try {
      const result = await assignSharedContact(contactId, {
        client_id: writeId,
        add_company: addCompany,
        user: WORKSPACE_USER,
      })
      if (result.needs_company_confirmation) {
        setConfirmCompany(true)
        return
      }
      applyWorkspace(result.workspace)
      notifyClientViews(result.workspace)
      setConfirmCompany(false)
    } catch (err) {
      setAssignError(err instanceof Error ? err.message : 'Failed to add this contact.')
    } finally {
      setAssignSaving(false)
    }
  }

  const statusOptions = useMemo(() => {
    const set = new Set(statuses)
    if (status.trim()) set.add(status.trim())
    if (callStatus.trim()) set.add(callStatus.trim())
    for (const item of APPOINTMENT_SET_STATUSES) set.add(item)
    return Array.from(set)
  }, [statuses, status, callStatus])

  if (loading) return <p className="data-status">Loading contact workspace…</p>
  if (error) {
    return (
      <p className="data-status data-status--error" role="alert">
        {error}
      </p>
    )
  }
  if (!data) return null

  const fullName = `${data.first_name} ${data.last_name}`.trim() || 'Contact'
  const requestedName = (data.client_name || activeClientName || '').trim()
  const assignedToClient = data.assigned_to_client !== false
  if (clientId && clientId > 0 && !assignedToClient) {
    const companyName = (data.company_name || '').trim() || 'this company'
    const clientLabel = requestedName || 'the selected client'
    return (
      <div className="client-setup-page">
        <div className="page-heading">
          {fromAskNorthStar ? (
            <button
              type="button"
              className="link-btn back-link"
              onClick={() => navigate(ASK_NORTHSTAR_PATH)}
            >
              ← Back to Ask NorthStar
            </button>
          ) : null}
          <h1>{fullName}</h1>
          <p className="workspace-working-for">
            <strong>Working For:</strong> {display(requestedName)}
          </p>
        </div>
        <div className="contact-assign-overlay" role="presentation">
          <section
            className="panel contact-assign-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="contact-assign-title"
          >
            {confirmCompany ? (
              <>
                <h2 id="contact-assign-title">Add company and contact?</h2>
                <p>
                  {companyName} is not assigned to {clientLabel}. Would you like to add both the
                  company and {fullName} to {clientLabel}?
                </p>
                {assignError ? (
                  <p className="data-status data-status--error" role="alert">
                    {assignError}
                  </p>
                ) : null}
                <div className="edit-actions">
                  <button
                    type="button"
                    className="primary-btn"
                    disabled={assignSaving}
                    onClick={() => void addToActiveClient(true)}
                  >
                    {assignSaving ? 'Adding…' : `Add ${companyName} and ${fullName}`}
                  </button>
                  <button
                    type="button"
                    className="secondary-btn"
                    disabled={assignSaving}
                    onClick={() => navigate(fromAskNorthStar ? ASK_NORTHSTAR_PATH : '/contacts')}
                  >
                    {fromAskNorthStar ? 'Cancel / Back to Ask NorthStar' : 'Cancel / Back to Contacts'}
                  </button>
                </div>
              </>
            ) : (
              <>
                <h2 id="contact-assign-title">Add this contact?</h2>
                <p>
                  {fullName} is not assigned to {clientLabel}. Would you like to add this contact
                  to {clientLabel}?
                </p>
                {assignError ? (
                  <p className="data-status data-status--error" role="alert">
                    {assignError}
                  </p>
                ) : null}
                <div className="edit-actions">
                  <button
                    type="button"
                    className="primary-btn"
                    disabled={assignSaving}
                    onClick={() => {
                      if (data.company_assigned_to_client === false) {
                        setConfirmCompany(true)
                        return
                      }
                      void addToActiveClient(false)
                    }}
                  >
                    {assignSaving ? 'Adding…' : `Add to ${clientLabel}`}
                  </button>
                  <button
                    type="button"
                    className="secondary-btn"
                    disabled={assignSaving}
                    onClick={() => navigate(fromAskNorthStar ? ASK_NORTHSTAR_PATH : '/contacts')}
                  >
                    {fromAskNorthStar ? 'Cancel / Back to Ask NorthStar' : 'Cancel / Back to Contacts'}
                  </button>
                </div>
              </>
            )}
          </section>
        </div>
      </div>
    )
  }
  const companyHref = data.company_record_no
    ? `/companies/${encodeURIComponent(data.company_record_no)}?${searchParams.toString()}`
    : null
  const returnPath = workspaceReturnPath(searchParams)
  const fromWorkQueue = isFromWorkQueue(searchParams.get('from'))
  const workingClientId = clientId != null && clientId > 0 ? clientId : 0
  const hasContactEmail = Boolean((data.email || '').trim())
  const timeline = data.timeline || []
  const reps = data.reps || []
  const canSaveWorkflow = workingClientId > 0 && Boolean(data.relationship_id)
  const callNeedsFollowUp =
    scheduleFollowUp && !statusNeverRequiresFollowUp(callStatus)
  const canSaveCall =
    Boolean(callStatus.trim()) &&
    nextActionIsValid(callNextSel, nextActionCatalog) &&
    (!callNeedsFollowUp || (Boolean(callFollowDate.trim()) && Boolean(callFollowTime.trim()))) &&
    (!isAppointmentSetStatus(callStatus) || appointmentDetailsAreValid(appointmentDetails))
  const canSaveFollowUp =
    Boolean(followDate) && nextActionIsValid(followNextSel, nextActionCatalog)
  const canSaveReschedule = Boolean(rescheduleDate.trim()) && Boolean(rescheduleTime.trim())
  const openFollowUp = data.open_follow_up || null

  return (
    <div className="client-setup-page">
      <CampaignRoutePrompt
        offer={campaignRouteOffer}
        onClose={() => setCampaignRouteOffer(null)}
      />
      <div className="page-heading page-heading--split">
        <div>
          {fromAskNorthStar ? (
            <button
              type="button"
              className="link-btn back-link"
              onClick={() => navigate(ASK_NORTHSTAR_PATH)}
            >
              ← Back to Ask NorthStar
            </button>
          ) : fromWorkQueue ? (
            <button type="button" className="link-btn back-link" onClick={() => navigate(returnPath)}>
              {workspaceReturnLabel(searchParams)}
            </button>
          ) : companyHref ? (
            <button type="button" className="link-btn back-link" onClick={() => navigate(companyHref)}>
              ← Back to Company
            </button>
          ) : (
            <button type="button" className="link-btn back-link" onClick={() => navigate(returnPath)}>
              {workspaceReturnLabel(searchParams)}
            </button>
          )}
          <h1>{fullName}</h1>
          <p className="workspace-working-for">
            <strong>Working For:</strong> {display(data.client_name)}
          </p>
          {workingClientId <= 0 ? (
            <p className="data-status" role="status">
              {SELECT_CLIENT_FOR_WRITE}
            </p>
          ) : null}
          {saveNotice ? (
            <p className="save-confirm" role="status">
              {saveNotice}
            </p>
          ) : null}
          {zoomInfoMsg ? (
            <p className="save-confirm" role="status">
              {zoomInfoMsg}
            </p>
          ) : null}
        </div>
        <div className="heading-controls">
          {workingClientId > 0 ? (
            <>
              <button type="button" className="primary-btn" onClick={() => setSendEmailOpen(true)}>
                Send Email
              </button>
              <button type="button" className="ghost-btn" onClick={() => setZoomInfoOpen(true)}>
                Update from ZoomInfo
              </button>
              {data.company_id ? (
                <button
                  type="button"
                  className="ghost-btn"
                  onClick={() =>
                    setCampaignRouteOffer({
                      clientId: workingClientId,
                      companyId: data.company_id ?? 0,
                      contactId: contactId,
                      source: 'manual',
                      mode: 'manual',
                    })
                  }
                >
                  Add to Campaign
                </button>
              ) : null}
            </>
          ) : (
            <p className="queue-sub" style={{ margin: 0, maxWidth: '12rem' }}>
              Select Working For client to send email.
            </p>
          )}
        </div>
      </div>

      <ZoomInfoUpdateModal
        open={zoomInfoOpen}
        onClose={() => setZoomInfoOpen(false)}
        contactId={contactId}
        clientId={workingClientId > 0 ? workingClientId : null}
        onApplied={(message) => setZoomInfoMsg(message)}
      />

      {sendEmailOpen && workingClientId > 0 ? (
        <SendEmailCompose
          open={sendEmailOpen}
          onClose={() => setSendEmailOpen(false)}
          clientId={workingClientId}
          clientName={data.client_name}
          companyId={data.company_id}
          companyName={data.company_name}
          externalRecordNo={data.company_record_no}
          lockedContactId={data.contact_id}
          contacts={
            hasContactEmail
              ? [
                  {
                    contact_id: data.contact_id,
                    contact_name: fullName,
                    email: data.email,
                    title: data.title || '',
                  },
                ]
              : []
          }
        />
      ) : null}

      <section className="panel">
        <div className="panel-header">
          <h2>Contact Information</h2>
        </div>
        <dl className="info-list">
          <div>
            <dt>Name</dt>
            <dd>{fullName}</dd>
          </div>
          <div>
            <dt>Title</dt>
            <dd>{display(data.title)}</dd>
          </div>
          <div>
            <dt>Company</dt>
            <dd>
              {companyHref ? <Link to={companyHref}>{display(data.company_name)}</Link> : display(data.company_name)}
            </dd>
          </div>
          <div>
            <dt>Working For</dt>
            <dd>{display(data.client_name)}</dd>
          </div>
          <div>
            <dt>Phone</dt>
            <dd>{display(data.phone)}</dd>
          </div>
          <div>
            <dt>Alternate Phone</dt>
            <dd>{display(data.alt_phone)}</dd>
          </div>
          <div>
            <dt>Email</dt>
            <dd>{display(data.email)}</dd>
          </div>
        </dl>
      </section>

      <section className="panel" aria-labelledby="contact-workflow-heading">
        <div className="panel-header">
          <h2 id="contact-workflow-heading">Operational Workflow</h2>
          <div className="workflow-summary-actions">
            <span className="queue-source">For {display(data.client_name)} only</span>
            {canSaveWorkflow && !editingWorkflow ? (
              <button
                type="button"
                className="link-btn"
                onClick={() => {
                  setActionKind(null)
                  setEditingWorkflow(true)
                  setWorkflowMsg(null)
                  setWorkflowError(null)
                }}
              >
                Edit Workflow
              </button>
            ) : null}
          </div>
        </div>
        {!canSaveWorkflow ? (
          <p className="queue-sub">Select a Working For client with a company relationship to edit workflow.</p>
        ) : editingWorkflow ? (
          <>
            <div className="milestone-form-grid">
              <label className="edit-field">
                <span className="edit-field__label">Client Status</span>
                <select
                  className="edit-select"
                  value={status}
                  onChange={(e) => {
                    const nextStatus = e.target.value
                    setStatus(nextStatus)
                    setNextSel((current) =>
                      defaultNextActionForStatus(nextStatus, nextActionCatalog, current),
                    )
                    setWorkflowMsg(null)
                    setWorkflowError(null)
                  }}
                >
                  {!status && <option value="">Select status…</option>}
                  {statusOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Assigned Rep</span>
                <select
                  className="edit-select"
                  value={assignedUserId}
                  onChange={(e) => setAssignedUserId(e.target.value)}
                >
                  <option value="">Unassigned</option>
                  {reps.map((rep) => (
                    <option key={rep.user_id} value={rep.user_id}>
                      {rep.full_name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Next Follow-Up Date</span>
                <input
                  className="edit-input"
                  type="date"
                  value={followUpDate}
                  onChange={(e) => setFollowUpDate(e.target.value)}
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Next Follow-Up Time</span>
                <input
                  className="edit-input"
                  type="time"
                  value={followUpTime}
                  onChange={(e) => setFollowUpTime(e.target.value)}
                />
              </label>
              <NextActionFields
                catalog={nextActionCatalog}
                value={nextSel}
                onChange={setNextSel}
                idPrefix="contact-workflow"
              />
            </div>
            <div className="edit-actions">
              <button
                type="button"
                className="primary-btn"
                disabled={savingWorkflow || !nextActionIsValid(nextSel, nextActionCatalog)}
                onClick={() => void saveWorkflow()}
              >
                {savingWorkflow ? 'Saving…' : 'Save Workflow'}
              </button>
              <button type="button" className="secondary-btn" disabled={savingWorkflow} onClick={cancelEditWorkflow}>
                Cancel
              </button>
              {workflowMsg ? (
                <span className="save-confirm" role="status">
                  {workflowMsg}
                </span>
              ) : null}
            </div>
            {workflowError ? (
              <p className="data-status data-status--error" role="alert">
                {workflowError}
              </p>
            ) : null}
            <p className="muted-note">
              Use this only for manual corrections. Logging a call updates status, next action, and
              follow-up in one step.
            </p>
          </>
        ) : (
          <dl className="info-list workflow-summary">
            <div>
              <dt>Status</dt>
              <dd>{display(data.status)}</dd>
            </div>
            <div>
              <dt>Assigned Rep</dt>
              <dd>{display(data.assigned_user)}</dd>
            </div>
            <div>
              <dt>Next Action</dt>
              <dd>{display(displayNextAction(data.next_action, nextActionCatalog) || data.next_action)}</dd>
            </div>
            <div>
              <dt>Next Follow-Up</dt>
              <dd>{followUpDisplay(data.follow_up_date, data.follow_up_time)}</dd>
            </div>
          </dl>
        )}

        {openFollowUp ? (
          <div className="open-follow-up-card">
            <h3 className="add-note-card__title">Open follow-up task</h3>
            <dl className="info-list workflow-summary">
              <div>
                <dt>Due</dt>
                <dd>{followUpDisplay(openFollowUp.due_date, openFollowUp.due_time)}</dd>
              </div>
              <div>
                <dt>Assigned</dt>
                <dd>{display(openFollowUp.assigned_user || data.assigned_user)}</dd>
              </div>
              {openFollowUp.notes ? (
                <div>
                  <dt>Notes</dt>
                  <dd>{openFollowUp.notes}</dd>
                </div>
              ) : null}
            </dl>
            <div className="workflow-action-row">
              <button
                type="button"
                className="primary-btn"
                disabled={!canSaveWorkflow}
                onClick={() => beginCall(true)}
              >
                Log Call &amp; Complete
              </button>
              <button
                type="button"
                className="primary-btn"
                disabled={!canSaveWorkflow}
                onClick={beginCompleteTask}
              >
                Complete Task
              </button>
              <button
                type="button"
                className="primary-btn"
                disabled={!canSaveWorkflow}
                onClick={beginReschedule}
              >
                Reschedule
              </button>
            </div>
          </div>
        ) : (
          <p className="queue-sub">No open follow-up task for this Working For client.</p>
        )}

        <div className="workflow-action-row">
          <button type="button" className="primary-btn" disabled={!canSaveWorkflow} onClick={() => beginCall(false)}>
            Log Call
          </button>
          <button type="button" className="primary-btn" disabled={!canSaveWorkflow} onClick={beginNote}>
            Add Note
          </button>
          {!openFollowUp && actionKind !== 'call' && actionKind !== 'call-complete' ? (
            <button type="button" className="primary-btn" disabled={!canSaveWorkflow} onClick={beginFollowUp}>
              Schedule Follow-Up
            </button>
          ) : null}
        </div>

        {actionKind === 'call' || actionKind === 'call-complete' ? (
          <div className="add-note-card work-queue-log-call">
            <h3 className="add-note-card__title">
              {actionKind === 'call-complete' ? 'Log Call & Complete' : 'Log Call'}
            </h3>
            <p className="muted-note">
              Saving this call logs the conversation, updates this client’s status, and completes
              the current open follow-up. Check “Schedule another follow-up” only when you want a
              new follow-up task. Closed and Do Not Call outcomes never require a follow-up.
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
                      defaultNextActionForStatus(nextStatus, nextActionCatalog, current),
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
                  {statusOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
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
                catalog={nextActionCatalog}
                value={callNextSel}
                onChange={setCallNextSel}
                idPrefix="contact-call"
              />
            </div>
            {isAppointmentSetStatus(callStatus) ? (
              <AppointmentDetailsFields
                value={appointmentDetails}
                onChange={setAppointmentDetails}
                reps={reps}
                companyName={data.company_name || ''}
                contactName={fullName}
                idPrefix="contact-call-appt"
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
                  } else if (!callNextSel.code || callNextSel.code === nextActionCatalog.default_for_closed_code) {
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
            <label className="edit-field__label" htmlFor="contact-call-notes">
              Notes
            </label>
            <textarea
              id="contact-call-notes"
              className="edit-textarea add-note-card__textarea"
              rows={4}
              value={callNotes}
              onChange={(e) => setCallNotes(e.target.value)}
              placeholder="Call notes (optional)…"
            />
            <div className="edit-actions">
              <button
                type="button"
                className="primary-btn"
                disabled={actionSaving || !canSaveCall}
                onClick={() => void saveAction()}
              >
                {actionSaving
                  ? 'Saving…'
                  : actionKind === 'call-complete'
                    ? 'Log Call & Complete'
                    : 'Save Call'}
              </button>
              <button type="button" className="link-btn" onClick={() => setActionKind(null)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {actionKind === 'note' ? (
          <div className="add-note-card">
            <h3 className="add-note-card__title">Add Note</h3>
            <textarea
              className="edit-textarea add-note-card__textarea"
              rows={4}
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
              placeholder="Note…"
            />
            <div className="edit-actions">
              <button
                type="button"
                className="primary-btn"
                disabled={actionSaving || !noteText.trim()}
                onClick={() => void saveAction()}
              >
                {actionSaving ? 'Saving…' : 'Save Note'}
              </button>
              <button type="button" className="link-btn" onClick={() => setActionKind(null)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {actionKind === 'complete-task' ? (
          <div className="add-note-card">
            <h3 className="add-note-card__title">Complete Task</h3>
            <p className="muted-note">
              Marks the open follow-up completed and adds one completion activity. Client status
              does not change unless you edit it in Operational Workflow.
            </p>
            <label className="edit-field__label" htmlFor="contact-complete-notes">
              Completion note (optional)
            </label>
            <textarea
              id="contact-complete-notes"
              className="edit-textarea add-note-card__textarea"
              rows={3}
              value={completeNote}
              onChange={(e) => setCompleteNote(e.target.value)}
              placeholder="What was completed…"
            />
            <div className="edit-actions">
              <button
                type="button"
                className="primary-btn"
                disabled={actionSaving}
                onClick={() => void saveAction()}
              >
                {actionSaving ? 'Saving…' : 'Complete Task'}
              </button>
              <button type="button" className="link-btn" onClick={() => setActionKind(null)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {actionKind === 'reschedule' ? (
          <div className="add-note-card">
            <h3 className="add-note-card__title">Reschedule Follow-Up</h3>
            <p className="muted-note">
              Updates the existing open follow-up date and time. This does not create a duplicate
              task.
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
            </div>
            <div className="edit-actions">
              <button
                type="button"
                className="primary-btn"
                disabled={actionSaving || !canSaveReschedule}
                onClick={() => void saveAction()}
              >
                {actionSaving ? 'Saving…' : 'Save Reschedule'}
              </button>
              <button type="button" className="link-btn" onClick={() => setActionKind(null)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {actionKind === 'follow-up' ? (
          <div className="add-note-card">
            <h3 className="add-note-card__title">Schedule Follow-Up</h3>
            <div className="milestone-form-grid">
              <label className="edit-field">
                <span className="edit-field__label">Follow-Up Date</span>
                <input
                  className="edit-input"
                  type="date"
                  value={followDate}
                  onChange={(e) => setFollowDate(e.target.value)}
                />
              </label>
              <label className="edit-field">
                <span className="edit-field__label">Follow-Up Time</span>
                <input
                  className="edit-input"
                  type="time"
                  value={followTime}
                  onChange={(e) => setFollowTime(e.target.value)}
                />
              </label>
              <NextActionFields
                catalog={nextActionCatalog}
                value={followNextSel}
                onChange={setFollowNextSel}
                idPrefix="contact-follow"
              />
            </div>
            <label className="edit-field__label" htmlFor="contact-follow-notes">
              Notes
            </label>
            <textarea
              id="contact-follow-notes"
              className="edit-textarea add-note-card__textarea"
              rows={3}
              value={followNotes}
              onChange={(e) => setFollowNotes(e.target.value)}
              placeholder="What to follow up on…"
            />
            <div className="edit-actions">
              <button
                type="button"
                className="primary-btn"
                disabled={actionSaving || !canSaveFollowUp}
                onClick={() => void saveAction()}
              >
                {actionSaving ? 'Saving…' : 'Save Follow-Up'}
              </button>
              <button type="button" className="link-btn" onClick={() => setActionKind(null)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {actionMsg ? (
          <span className="save-confirm" role="status">
            {actionMsg}
          </span>
        ) : null}
        {actionError ? (
          <p className="data-status data-status--error" role="alert">
            {actionError}
          </p>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Contact Activity</h2>
          <span className="queue-source">
            {timeline.length} item{timeline.length === 1 ? '' : 's'} · this contact · newest first
          </span>
        </div>
        {timeline.length === 0 ? (
          <p className="queue-sub">No notes or activities linked to this contact yet.</p>
        ) : (
          <ul className="ask-history-list contact-timeline">
            {timeline.map((item: ContactTimelineItem) => (
              <li key={`${item.item_type}-${item.id}`}>
                <strong>{display(item.title)}</strong>
                <div className="queue-sub">
                  {display(item.at)}
                  {item.created_by ? ` · ${item.created_by}` : ''}
                  {item.outcome ? ` · ${item.outcome}` : ''}
                  {item.follow_up_at ? ` · Follow-up ${item.follow_up_at}` : ''}
                </div>
                {item.body ? <div>{item.body}</div> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Company Activity</h2>
          <span className="queue-source">
            {(data.company_timeline || []).length} item
            {(data.company_timeline || []).length === 1 ? '' : 's'} · {display(data.company_name)} ·
            newest first
          </span>
        </div>
        <p className="muted-note">
          Company-level notes and activities for this Working For client. These are not linked to{' '}
          {fullName}.
        </p>
        {(data.company_timeline || []).length === 0 ? (
          <p className="queue-sub">No company-level notes or activities for this client yet.</p>
        ) : (
          <ul className="ask-history-list contact-timeline">
            {(data.company_timeline || []).map((item: ContactTimelineItem) => (
              <li key={`company-${item.item_type}-${item.id}`}>
                <strong>{display(item.title)}</strong>
                <div className="queue-sub">
                  {display(item.at)}
                  {item.created_by ? ` · ${item.created_by}` : ''}
                  {item.outcome ? ` · ${item.outcome}` : ''}
                  {item.follow_up_at ? ` · Follow-up ${item.follow_up_at}` : ''}
                </div>
                {item.body ? <div>{item.body}</div> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Appointment / Engagement History</h2>
          <span className="queue-source">
            {(data.sales_events || []).length} event
            {(data.sales_events || []).length === 1 ? '' : 's'} · newest first
          </span>
        </div>
        <p className="muted-note">
          Read-only imported Quote / appointment history. This is not a place to edit LeadMaster
          source records.
        </p>
        {(data.sales_events || []).length === 0 ? (
          <p className="queue-sub">No imported sales events for this contact yet.</p>
        ) : (
          <ul className="ask-history-list">
            {data.sales_events.map((ev) => (
              <li key={ev.event_id}>
                <strong>{display(ev.event_type)}</strong>
                <div className="queue-sub">
                  {display(ev.event_date || ev.source_date_time_text)}
                  {companyHref ? (
                    <>
                      {' · '}
                      <Link to={companyHref}>{display(data.company_name)}</Link>
                    </>
                  ) : null}
                  {' · Working For: '}
                  {display(data.client_name)}
                </div>
                <div className="queue-sub">
                  Grade {display(ev.source_appointment_grade)} · Source Rev Spec:{' '}
                  {display(ev.source_rev_spec_text)}
                  {ev.rev_spec_user_id
                    ? ''
                    : ' (historical source text — not linked to a NorthStar user)'}
                </div>
                {ev.caller_notes ? (
                  <div>
                    <em>NorthStar Caller Notes:</em> {ev.caller_notes}
                  </div>
                ) : null}
                {ev.sales_notes ? (
                  <div>
                    <em>Client / Sales Notes:</em> {ev.sales_notes}
                  </div>
                ) : null}
                {(ev.quoted_amount != null || ev.outcome_normalized || ev.source_outcome) && (
                  <div className="queue-sub">
                    {ev.quoted_amount != null
                      ? `Quoted $${Number(ev.quoted_amount).toLocaleString()} · `
                      : ''}
                    {display(ev.outcome_normalized || ev.source_outcome)}
                  </div>
                )}
                <div className="queue-sub">
                  Source: {display(ev.source_file_name)} · {display(ev.source_sheet)} row {ev.source_row}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
