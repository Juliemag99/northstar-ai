import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  completeContactFollowUp,
  completeWorkQueueItem,
  createCompanyActivity,
  createContactActivity,
  fetchNextActions,
  fetchOpportunityCount,
  fetchWorkQueue,
  fetchWorkQueueClients,
} from './api/carmeco'
import { requireWriteClientId } from './writeClient'
import NextActionFields from './NextActionFields'
import {
  emptyNextActionCatalog,
  displayNextAction,
  matchNextAction,
  nextActionIsValid,
  storedNextAction,
  type NextActionCatalog,
  type NextActionSelection,
} from './nextAction'
import type { WorkQueueInsightSummary, WorkQueueRow, WorkQueueSummaryV2 } from './types/carmeco'

type ClientOption = { client_id: number; client_name: string; client_code: string }

const PAGE_SIZE = 50

const EMPTY_SUMMARY: WorkQueueSummaryV2 = {
  calls_due: 0,
  follow_ups_due: 0,
  appointments: 0,
  hot: 0,
  webleads: 0,
  new_assignments: 0,
  needs_next_action: 0,
  cross_client_opportunities: 0,
  overdue: 0,
}

const EMPTY_INSIGHT_SUMMARY: WorkQueueInsightSummary = {
  aligned: 0,
  review: 0,
  insufficient_ai_data: 0,
  evaluated: 0,
  not_evaluated: 0,
}

function display(value: string | null | undefined): string {
  return value?.trim() || '—'
}

function workTypeClass(workType: string): string {
  const key = workType.toLowerCase()
  if (key.includes('call')) return 'call'
  if (key.includes('follow')) return 'follow-up'
  if (key.includes('appointment')) return 'appointment'
  if (key.includes('weblead')) return 'weblead'
  if (key === 'hot') return 'hot'
  if (key.includes('new')) return 'new'
  if (key.includes('needs next')) return 'needs-next'
  if (key.includes('cross')) return 'cross-client'
  return 'default'
}

function workspaceHref(
  item: WorkQueueRow,
  queueQuery: string,
  position: number,
  total: number,
): string {
  const params = new URLSearchParams()
  params.set('from', 'work-queue')
  params.set('client_id', String(item.client_id))
  params.set('queue_item', item.queue_item_id)
  params.set('work_type', item.work_type)
  params.set('reason', item.why_in_queue || item.work_type)
  params.set('priority', String(item.work_priority))
  params.set('position', String(position))
  params.set('total', String(total))
  if (queueQuery) params.set('queue', queueQuery)
  if (item.work_type === 'Follow-Up' && item.contact_id != null) {
    return `/contacts/${item.contact_id}?${params.toString()}`
  }
  return `/companies/${encodeURIComponent(item.external_record_no)}?${params.toString()}`
}

export default function WorkQueue({
  activeClientId = null,
  assignedClients,
  onQueueChanged,
}: {
  activeClientId?: number | null
  assignedClients?: ClientOption[]
  onQueueChanged?: () => void
}) {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [clients, setClients] = useState<ClientOption[]>(assignedClients ?? [])
  const [items, setItems] = useState<WorkQueueRow[]>([])
  const [total, setTotal] = useState(0)
  const [hasPrevious, setHasPrevious] = useState(false)
  const [hasNext, setHasNext] = useState(false)
  const [page, setPage] = useState(0)
  const [summary, setSummary] = useState<WorkQueueSummaryV2>(EMPTY_SUMMARY)
  const [insightSummary, setInsightSummary] =
    useState<WorkQueueInsightSummary>(EMPTY_INSIGHT_SUMMARY)
  const [expandedInsightId, setExpandedInsightId] = useState<string | null>(null)
  const [opportunityCount, setOpportunityCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [searchDraft, setSearchDraft] = useState(searchParams.get('q') || '')
  const [debouncedQ, setDebouncedQ] = useState(searchParams.get('q') || '')
  const requestSeq = useRef(0)
  const [nextActionCatalog, setNextActionCatalog] = useState<NextActionCatalog>(emptyNextActionCatalog())
  const [schedulingKey, setSchedulingKey] = useState<string | null>(null)
  const [scheduleDate, setScheduleDate] = useState('')
  const [scheduleTime, setScheduleTime] = useState('09:00')
  const [scheduleNextSel, setScheduleNextSel] = useState<NextActionSelection>({
    code: 'follow_up',
    custom: '',
  })
  const [scheduleSaving, setScheduleSaving] = useState(false)

  const clientId = searchParams.get('client_id')
  const workType = searchParams.get('type') || ''
  const due = searchParams.get('due') || ''
  const status = searchParams.get('status') || ''
  const priority = searchParams.get('priority') || ''
  const hot = searchParams.get('hot') === '1' || searchParams.get('hot') === 'true'
  const weblead = searchParams.get('weblead') === '1' || searchParams.get('weblead') === 'true'
  const crossClient =
    searchParams.get('cross_client') === '1' || searchParams.get('cross_client') === 'true'
  const overdue =
    searchParams.get('overdue') === '1' ||
    searchParams.get('overdue') === 'true' ||
    due === 'overdue'
  const q = debouncedQ
  const aiAlignment = searchParams.get('ai_alignment') || ''
  const aiRecommendation = searchParams.get('ai_recommendation') || ''
  const aiFit = searchParams.get('ai_fit') || ''
  const aiEngagement = searchParams.get('ai_engagement') || ''

  const queueQuery = searchParams.toString()

  const selectedClientId = clientId
    ? Number(clientId)
    : activeClientId != null && activeClientId > 0
      ? activeClientId
      : null

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = searchDraft.trim()
      setDebouncedQ(next)
      setSearchParams(
        (prev) => {
          const current = prev.get('q') || ''
          if (next === current) return prev
          const params = new URLSearchParams(prev)
          if (!next) params.delete('q')
          else params.set('q', next)
          return params
        },
        { replace: true },
      )
    }, 250)
    return () => window.clearTimeout(timer)
  }, [searchDraft, setSearchParams])

  useEffect(() => {
    setPage(0)
  }, [
    selectedClientId,
    workType,
    status,
    due,
    priority,
    hot,
    weblead,
    crossClient,
    overdue,
    q,
    aiAlignment,
    aiRecommendation,
    aiFit,
    aiEngagement,
  ])

  useEffect(() => {
    if (assignedClients && assignedClients.length > 0) {
      setClients(assignedClients)
      return
    }
    let cancelled = false
    void fetchWorkQueueClients()
      .then((list) => {
        if (!cancelled) setClients(list)
      })
      .catch(() => {
        if (!cancelled) setClients([])
      })
    return () => {
      cancelled = true
    }
  }, [assignedClients])

  useEffect(() => {
    const scopedId = selectedClientId != null && selectedClientId > 0 ? selectedClientId : 0
    let cancelled = false
    void fetchNextActions(scopedId || null)
      .then((catalog) => {
        if (!cancelled) setNextActionCatalog(catalog)
      })
      .catch(() => {
        if (!cancelled) setNextActionCatalog(emptyNextActionCatalog())
      })
    return () => {
      cancelled = true
    }
  }, [selectedClientId])

  useEffect(() => {
    if (activeClientId == null || activeClientId < 0) return
    const current = searchParams.get('client_id')
    const desired = activeClientId > 0 ? String(activeClientId) : ''
    if (activeClientId > 0) {
      if (current === desired) return
      const next = new URLSearchParams(searchParams)
      next.set('client_id', desired)
      setSearchParams(next, { replace: true })
      return
    }
    // All My Clients — drop client_id scope from the queue URL.
    if (!current) return
    const next = new URLSearchParams(searchParams)
    next.delete('client_id')
    setSearchParams(next, { replace: true })
  }, [activeClientId, searchParams, setSearchParams])

  const loadQueue = useCallback(async () => {
    const seq = ++requestSeq.current
    setLoading(true)
    setError(null)
    try {
      const scoped = Number.isFinite(selectedClientId) ? selectedClientId : null
      const [result, oppCount] = await Promise.all([
        fetchWorkQueue({
          client_id: scoped,
          type: workType || undefined,
          status: status || undefined,
          due: due || undefined,
          priority: priority || undefined,
          hot,
          weblead,
          cross_client: crossClient,
          overdue,
          q: q || undefined,
          ai_alignment: aiAlignment || undefined,
          ai_recommendation: aiRecommendation || undefined,
          ai_fit: aiFit || undefined,
          ai_engagement: aiEngagement || undefined,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        }),
        fetchOpportunityCount(scoped),
      ])
      if (seq !== requestSeq.current) return
      setItems(result.items)
      setTotal(result.total)
      setHasPrevious(result.has_previous)
      setHasNext(result.has_next)
      setSummary(result.summary)
      setInsightSummary(result.northstar_insight_summary || EMPTY_INSIGHT_SUMMARY)
      setOpportunityCount(oppCount)
    } catch (err) {
      if (seq !== requestSeq.current) return
      setError(err instanceof Error ? err.message : 'Failed to load Work Queue.')
      setItems([])
      setTotal(0)
      setHasPrevious(false)
      setHasNext(false)
      setSummary(EMPTY_SUMMARY)
      setInsightSummary(EMPTY_INSIGHT_SUMMARY)
      setOpportunityCount(0)
    } finally {
      if (seq === requestSeq.current) setLoading(false)
    }
  }, [
    selectedClientId,
    workType,
    status,
    due,
    priority,
    hot,
    weblead,
    crossClient,
    overdue,
    q,
    aiAlignment,
    aiRecommendation,
    aiFit,
    aiEngagement,
    page,
  ])

  useEffect(() => {
    void loadQueue()
  }, [loadQueue])

  function updateParams(mutator: (params: URLSearchParams) => void) {
    const next = new URLSearchParams(searchParams)
    mutator(next)
    setSearchParams(next)
  }

  function setFilter(key: string, value: string) {
    updateParams((params) => {
      if (!value) params.delete(key)
      else params.set(key, value)
      if (key === 'due' && value === 'overdue') params.delete('overdue')
    })
  }

  function toggleBoolFilter(key: string, active: boolean) {
    updateParams((params) => {
      if (active) params.set(key, '1')
      else params.delete(key)
    })
  }

  /** Clear summary/queue filters in one write so React Router does not drop params. */
  function clearQueueFilters(params: URLSearchParams) {
    for (const key of [
      'type',
      'due',
      'status',
      'priority',
      'hot',
      'weblead',
      'cross_client',
      'overdue',
      'q',
      'ai_alignment',
      'ai_recommendation',
      'ai_fit',
      'ai_engagement',
    ]) {
      params.delete(key)
    }
  }

  function applySummaryFilter(filterId: string) {
    setMessage(null)
    setPage(0)
    // Rebuild params from scratch so card filters never stack/overwrite oddly.
    const next = new URLSearchParams()
    if (clientId) next.set('client_id', clientId)
    clearQueueFilters(next)
    setSearchDraft('')
    setDebouncedQ('')

    switch (filterId) {
      case 'all':
        break
      case 'work-next':
        // Prioritized actionable queue (excludes bulk New Assignments when other work exists).
        next.set('type', 'work-next')
        break
      case 'call':
        next.set('type', 'call')
        next.set('due', 'today')
        break
      case 'follow-up':
        next.set('type', 'follow-up')
        break
      case 'needs-next-action':
        next.set('type', 'needs-next-action')
        break
      case 'appointment':
        next.set('type', 'appointment')
        break
      case 'cross-client':
        next.set('type', 'cross-client')
        break
      case 'hot':
        next.set('type', 'hot')
        break
      case 'weblead':
        next.set('type', 'weblead')
        break
      case 'new':
        next.set('type', 'new')
        break
      case 'overdue':
        next.set('due', 'overdue')
        break
      default:
        break
    }
    setSearchParams(next)
  }

  function clearFilters() {
    applySummaryFilter('all')
  }

  const activeSummaryId = (() => {
    if (workType === 'work-next') return 'work-next'
    if (workType === 'call' && (due === 'today' || due === '')) return 'call'
    if (workType === 'follow-up' && (due === 'today' || due === '')) return 'follow-up'
    if (workType === 'needs-next-action') return 'needs-next-action'
    if (workType === 'appointment') return 'appointment'
    if (workType === 'cross-client') return 'cross-client'
    if (workType === 'hot' || (hot && !workType)) return 'hot'
    if (workType === 'weblead') return 'weblead'
    if (workType === 'new') return 'new'
    if ((due === 'overdue' || (overdue && due === '')) && !workType) return 'overdue'
    if (!workType && !due && !hot && !weblead && !crossClient && !overdue && !status && !priority && !q) {
      return 'all'
    }
    return null
  })()

  function openItem(item: WorkQueueRow, index = 0) {
    navigate(workspaceHref(item, queueQuery, page * PAGE_SIZE + index + 1, total || 1))
  }

  async function workNext() {
    // Always pull an unfiltered prioritized queue so New Assignments cannot hide
    // higher-value work behind the current card filter.
    try {
      const result = await fetchWorkQueue({
        client_id: clientId ? Number(clientId) : null,
        type: 'work-next',
        limit: PAGE_SIZE,
        offset: 0,
      })
      if (result.items.length === 0) {
        setMessage("You're caught up.")
        return
      }
      const item = result.items[0]
      navigate(workspaceHref(item, 'type=work-next', 1, result.total || result.items.length))
    } catch {
      if (items.length === 0) {
        setMessage("You're caught up.")
        return
      }
      openItem(items[0], 0)
    }
  }

  async function quickLogCall(item: WorkQueueRow, index: number) {
    navigate(
      `${workspaceHref(item, queueQuery, page * PAGE_SIZE + index + 1, total || 1)}&focus=log-call`,
    )
  }

  async function quickAddNote(item: WorkQueueRow) {
    const notes = window.prompt('Note text')
    if (!notes?.trim()) return
    try {
      const clientId = requireWriteClientId(item.client_id)
      await createCompanyActivity({
        client: item.client_name || item.client_code || '',
        client_id: clientId,
        external_record_no: item.external_record_no,
        activity_type: 'Note',
        notes: notes.trim(),
        contact_id: item.contact_id,
      })
      setMessage(`Note added for ${item.company_name}.`)
      await loadQueue()
      onQueueChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add note.')
    }
  }

  async function quickCompleteFollowUp(item: WorkQueueRow) {
    if (!item.contact_id) return
    const notes = window.prompt('Completion note (optional). Leave blank to complete without a note.', '')
    if (notes === null) return
    try {
      await completeContactFollowUp(item.contact_id, {
        client_id: item.client_id,
        notes: notes.trim(),
        created_by: 'Julie Magnani',
      })
      setMessage(`Follow-up completed for ${item.contact_name || item.company_name}.`)
      await loadQueue()
      onQueueChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not complete the follow-up.')
    }
  }

  async function quickScheduleFollowUp(item: WorkQueueRow) {
    setSchedulingKey(item.queue_item_id)
    setScheduleDate((item.due_date || new Date().toISOString().slice(0, 10)).slice(0, 10))
    setScheduleTime((item.due_time || '09:00').slice(0, 5) || '09:00')
    const matched = matchNextAction(item.next_action || 'Follow-Up', nextActionCatalog)
    setScheduleNextSel(matched.code ? matched : { code: 'follow_up', custom: '' })
    setMessage(null)
    setError(null)
  }

  async function saveScheduledFollowUp(item: WorkQueueRow) {
    if (scheduleSaving || !scheduleDate) return
    if (!nextActionIsValid(scheduleNextSel, nextActionCatalog)) return
    const nextAction = storedNextAction(scheduleNextSel, nextActionCatalog) || 'Follow-Up'
    setScheduleSaving(true)
    setError(null)
    try {
      if (item.contact_id) {
        await createContactActivity(item.contact_id, {
          client_id: item.client_id,
          activity_type: 'Follow-Up',
          notes: 'Scheduled from Work Queue',
          next_action: nextAction,
          follow_up_date: scheduleDate,
          follow_up_time: scheduleTime,
          created_by: 'Julie Magnani',
        })
        if (item.work_type === 'Call') {
          await completeWorkQueueItem({
            client_id: item.client_id,
            source: item.source,
            source_id: item.source_id,
            company_id: item.company_id,
          })
        }
      } else {
        await createCompanyActivity({
          client: item.client_name || item.client_code || '',
          client_id: requireWriteClientId(item.client_id),
          external_record_no: item.external_record_no,
          activity_type: 'Follow-Up',
          notes: nextAction,
          follow_up_at: `${scheduleDate} ${scheduleTime}`,
          contact_id: item.contact_id,
        })
        if (item.work_type === 'Call' || item.work_type === 'Follow-Up') {
          await completeWorkQueueItem({
            client_id: item.client_id,
            source: item.source,
            source_id: item.source_id,
            company_id: item.company_id,
          })
        }
      }
      setMessage(`Follow-up scheduled for ${item.company_name}.`)
      setSchedulingKey(null)
      await loadQueue()
      onQueueChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not schedule follow-up.')
    } finally {
      setScheduleSaving(false)
    }
  }

  const actionableCount = useMemo(() => {
    const nonNew =
      summary.calls_due +
      summary.follow_ups_due +
      summary.appointments +
      summary.hot +
      summary.webleads +
      summary.needs_next_action +
      summary.cross_client_opportunities +
      summary.overdue
    return nonNew > 0 ? nonNew : summary.new_assignments
  }, [summary])

  const summaryCards = [
    {
      id: 'work-next',
      label: 'Work Next',
      value: actionableCount as number | null,
      tone: 'work-next',
      emptyLabel: 'Work Next',
    },
    {
      id: 'call',
      label: 'Calls Due',
      value: summary.calls_due,
      tone: 'call',
      emptyLabel: 'Calls Due',
    },
    {
      id: 'follow-up',
      label: 'Follow-Ups Due',
      value: summary.follow_ups_due,
      tone: 'follow-up',
      emptyLabel: 'Follow-Ups Due',
    },
    {
      id: 'needs-next-action',
      label: 'Needs Next Action',
      value: summary.needs_next_action,
      tone: 'needs-next',
      emptyLabel: 'Needs Next Action',
    },
    {
      id: 'appointment',
      label: 'Appointments',
      value: summary.appointments,
      tone: 'appointment',
      emptyLabel: 'Appointments',
    },
    {
      id: 'cross-client',
      label: 'Cross-Client Opportunities',
      value: opportunityCount,
      tone: 'cross-client',
      emptyLabel: 'Cross-Client Opportunities',
    },
    {
      id: 'hot',
      label: 'Hot',
      value: summary.hot,
      tone: 'hot',
      emptyLabel: 'Hot',
    },
    {
      id: 'new',
      label: 'New Assignments',
      value: summary.new_assignments,
      tone: 'new',
      emptyLabel: 'New Assignments',
    },
    {
      id: 'all',
      label: 'All Work',
      value: null as number | null,
      tone: 'all',
      emptyLabel: 'work items',
    },
  ]

  const activeCard = summaryCards.find((card) => card.id === activeSummaryId) || null
  const filteredEmptyLabel =
    activeCard && activeCard.id !== 'all'
      ? activeCard.emptyLabel
      : workType || due || hot || weblead || overdue
        ? 'matching'
        : null

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Work Queue</h1>
          <p>Daily prospecting work across your assigned NorthStar clients.</p>
        </div>
        <div className="heading-controls">
          <button type="button" className="clear-filter-btn" onClick={clearFilters}>
            Clear Filter
          </button>
          <button type="button" className="primary-btn work-next-btn" onClick={() => void workNext()}>
            WORK NEXT
          </button>
        </div>
      </div>

      <section className="stat-grid stat-grid--work-summary" aria-label="Work Queue summary">
        {summaryCards.map((card) => {
          const selected = activeSummaryId === card.id
          return (
            <button
              key={card.id}
              type="button"
              className={`stat-card stat-card--clickable stat-card--wq-${card.tone}${selected ? ' stat-card--wq-selected' : ''}`}
              onClick={() => {
                if (card.id === 'cross-client') {
                  const target =
                    selectedClientId != null && selectedClientId > 0
                      ? `/cross-client-opportunities?target_client_id=${selectedClientId}`
                      : '/cross-client-opportunities'
                  navigate(target)
                  return
                }
                applySummaryFilter(card.id)
              }}
              aria-pressed={selected}
            >
              <div className="stat-card__top">
                <p className="stat-label">{card.label}</p>
              </div>
              {card.value != null ? (
                <p className="stat-value">{card.value}</p>
              ) : (
                <p className="stat-value stat-value--muted">View all</p>
              )}
            </button>
          )
        })}
      </section>

      <section className="panel work-queue-filters" aria-label="Work Queue filters">
        <div className="opportunity-filter-grid">
          <label className="edit-field">
            <span className="edit-field__label">Client</span>
            <select
              className="edit-select"
              value={selectedClientId ?? ''}
              onChange={(event) => setFilter('client_id', event.target.value)}
            >
              <option value="">All My Clients</option>
              {clients.map((client) => (
                <option key={client.client_id} value={client.client_id}>
                  {client.client_name}
                </option>
              ))}
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Work Type</span>
            <select className="edit-select" value={workType} onChange={(event) => setFilter('type', event.target.value)}>
              <option value="">Any</option>
              <option value="call">Call</option>
              <option value="follow-up">Follow-Up</option>
              <option value="appointment">Appointment</option>
              <option value="weblead">WebLead</option>
              <option value="hot">Hot</option>
              <option value="new">New Assignment</option>
              <option value="needs-next-action">Needs Next Action</option>
              <option value="research">Research</option>
              <option value="cross-client">Cross-Client Opportunity</option>
              <option value="other">Other</option>
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Due</span>
            <select className="edit-select" value={due} onChange={(event) => setFilter('due', event.target.value)}>
              <option value="">Any</option>
              <option value="today">Today</option>
              <option value="overdue">Overdue</option>
              <option value="upcoming">Upcoming</option>
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Priority</span>
            <select className="edit-select" value={priority} onChange={(event) => setFilter('priority', event.target.value)}>
              <option value="">Any</option>
              <option value="Critical">Critical</option>
              <option value="High">High</option>
              <option value="Medium">Medium</option>
              <option value="Low">Low</option>
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Status</span>
            <input
              className="edit-input"
              value={status}
              onChange={(event) => setFilter('status', event.target.value)}
              placeholder="Any"
            />
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Search</span>
            <input
              className="edit-input"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  const next = searchDraft.trim()
                  setDebouncedQ(next)
                  setFilter('q', next)
                }
              }}
              placeholder="Company, contact, notes…"
            />
          </label>
          <label className="edit-field">
            <span className="edit-field__label">AI Alignment</span>
            <select
              className="edit-select"
              value={aiAlignment}
              onChange={(event) => setFilter('ai_alignment', event.target.value)}
            >
              <option value="">All</option>
              <option value="Aligned">Aligned</option>
              <option value="Review">Review</option>
              <option value="Insufficient AI Data">Insufficient AI Data</option>
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Recommendation</span>
            <select
              className="edit-select"
              value={aiRecommendation}
              onChange={(event) => setFilter('ai_recommendation', event.target.value)}
            >
              <option value="">All</option>
              <option value="Follow Up Now">Follow Up Now</option>
              <option value="Prepare for Appointment">Prepare for Appointment</option>
              <option value="Review RFQ">Review RFQ</option>
              <option value="Confirm Opportunity Fit">Confirm Opportunity Fit</option>
              <option value="Identify Decision Maker">Identify Decision Maker</option>
              <option value="Research Company">Research Company</option>
              <option value="Nurture">Nurture</option>
              <option value="No Immediate Action">No Immediate Action</option>
              <option value="Do Not Pursue">Do Not Pursue</option>
              <option value="Review Account">Review Account</option>
              <option value="Not Evaluated">Not Evaluated</option>
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Fit</span>
            <select
              className="edit-select"
              value={aiFit}
              onChange={(event) => setFilter('ai_fit', event.target.value)}
            >
              <option value="">All</option>
              <option value="Strong">Strong</option>
              <option value="Possible">Possible</option>
              <option value="Weak">Weak</option>
              <option value="Insufficient Information">Insufficient Information</option>
              <option value="Campaign Criteria Not Configured">
                Campaign Criteria Not Configured
              </option>
              <option value="Not Evaluated">Not Evaluated</option>
            </select>
          </label>
          <label className="edit-field">
            <span className="edit-field__label">Engagement</span>
            <select
              className="edit-select"
              value={aiEngagement}
              onChange={(event) => setFilter('ai_engagement', event.target.value)}
            >
              <option value="">All</option>
              <option value="Active Opportunity">Active Opportunity</option>
              <option value="Engaged">Engaged</option>
              <option value="Early Engagement">Early Engagement</option>
              <option value="Nurture">Nurture</option>
              <option value="No Current Engagement">No Current Engagement</option>
              <option value="Closed / Not Pursuing">Closed / Not Pursuing</option>
              <option value="Insufficient Information">Insufficient Information</option>
              <option value="Campaign Criteria Not Configured">
                Campaign Criteria Not Configured
              </option>
              <option value="Not Evaluated">Not Evaluated</option>
            </select>
          </label>
        </div>
        <div className="quick-filter-row">
          <button type="button" className={`quick-filter-chip ${hot ? 'quick-filter-chip--active' : ''}`} onClick={() => toggleBoolFilter('hot', !hot)}>Hot</button>
          <button type="button" className={`quick-filter-chip ${weblead ? 'quick-filter-chip--active' : ''}`} onClick={() => toggleBoolFilter('weblead', !weblead)}>WebLead</button>
          <button type="button" className={`quick-filter-chip ${crossClient ? 'quick-filter-chip--active' : ''}`} onClick={() => toggleBoolFilter('cross_client', !crossClient)}>Cross-Client Opportunity</button>
          <button type="button" className="link-btn" onClick={() => {
            const next = searchDraft.trim()
            setDebouncedQ(next)
            setFilter('q', next)
          }}>Search</button>
          <button type="button" className="clear-filter-btn" onClick={clearFilters}>Clear Filters</button>
        </div>
      </section>

      {message && <p className="data-status opportunity-message" role="status">{message}</p>}
      {error && <p className="data-status data-status--error" role="alert">{error}</p>}

      <section className="panel panel--queue" aria-label="Work Queue table">
        <div className="panel-header">
          <h2>
            {loading
              ? 'Loading work…'
              : `${total} item${total === 1 ? '' : 's'}${
                  total > PAGE_SIZE
                    ? ` · showing ${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + items.length}`
                    : ''
                }`}
          </h2>
          <div className="queue-header-meta">
            {activeSummaryId && activeSummaryId !== 'all' && (
              <button type="button" className="clear-filter-btn" onClick={clearFilters}>
                Clear Filter
              </button>
            )}
            <span className="queue-source">Work Priority · rules-based (not AI)</span>
            <span className="queue-source">
              NorthStar Insight · observation only · Aligned {insightSummary.aligned} · Review{' '}
              {insightSummary.review} · Insufficient {insightSummary.insufficient_ai_data}
            </span>
          </div>
        </div>
        {!loading && items.length === 0 ? (
          <div className="empty-state work-queue-caught-up">
            {filteredEmptyLabel ? (
              <>
                <p>No {filteredEmptyLabel} items in this queue.</p>
                <div className="edit-actions">
                  <button type="button" className="primary-btn" onClick={clearFilters}>
                    All Work
                  </button>
                  <button type="button" className="link-btn" onClick={() => applySummaryFilter('new')}>
                    View New Assignments
                  </button>
                  <Link className="link-btn" to="/">
                    Return to Dashboard
                  </Link>
                </div>
              </>
            ) : (
              <>
                <p>You&apos;re caught up.</p>
                <div className="edit-actions">
                  <button type="button" className="primary-btn" onClick={() => applySummaryFilter('new')}>
                    View New Assignments
                  </button>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => {
                      setSearchDraft('')
                      setSearchParams(new URLSearchParams())
                    }}
                  >
                    Change Client
                  </button>
                  <Link className="link-btn" to="/">
                    Return to Dashboard
                  </Link>
                </div>
              </>
            )}
          </div>
        ) : (
          <>
          <div className="queue-table-wrap">
            <table className="queue-table work-queue-table">
              <thead>
                <tr>
                  <th>Priority</th>
                  <th>Company</th>
                  <th>Client</th>
                  <th>Contact</th>
                  <th>Work Type</th>
                  <th>Status</th>
                  <th>Due Date</th>
                  <th>Due Time</th>
                  <th>Last Activity</th>
                  <th>Next Action</th>
                  <th>NorthStar Insight</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, index) => {
                  const insight = item.northstar_insight
                  const expanded = expandedInsightId === item.queue_item_id
                  const alignmentClass =
                    insight?.alignment === 'Review'
                      ? 'wq-insight--review'
                      : insight?.alignment === 'Aligned'
                        ? 'wq-insight--aligned'
                        : 'wq-insight--insufficient'
                  return (
                  <tr key={item.queue_item_id} className={item.is_overdue ? 'work-queue-row--overdue' : undefined}>
                    <td>
                      <span className={`priority-pill priority-pill--${item.priority_label.toLowerCase()}`}>
                        {item.work_priority}
                      </span>
                      <span className="queue-sub">{item.priority_label}</span>
                    </td>
                    <td>
                      <Link
                        className="company-link"
                        to={workspaceHref(
                          item,
                          queueQuery,
                          page * PAGE_SIZE + index + 1,
                          total || 1,
                        )}
                      >
                        {display(item.company_name)}
                      </Link>
                      <span className="queue-sub">Record No. {item.external_record_no}</span>
                      {item.is_new_weblead && (
                        <span className="wq-badge wq-badge--weblead">
                          NEW WEBLEAD{item.weblead_age_label ? ` · ${item.weblead_age_label}` : ''}
                        </span>
                      )}
                      {item.is_cross_client && (
                        <span className="wq-badge wq-badge--cross">
                          Cross-Client Opportunity
                          {item.opportunity_score != null ? ` · Score ${item.opportunity_score}` : ''}
                          {item.source_client_summary ? ` · Source: ${item.source_client_summary}` : ''}
                        </span>
                      )}
                    </td>
                    <td>
                      <strong>{display(item.client_name)}</strong>
                    </td>
                    <td>
                      {item.contact_id != null && item.contact_name ? (
                        <Link
                          className="company-link"
                          to={workspaceHref(
                            item,
                            queueQuery,
                            page * PAGE_SIZE + index + 1,
                            total || 1,
                          )}
                        >
                          {item.contact_name}
                        </Link>
                      ) : (
                        display(item.contact_name)
                      )}
                    </td>
                    <td>
                      <span className={`opportunity-badge opportunity-badge--${workTypeClass(item.work_type)}`}>
                        {item.work_type}
                      </span>
                    </td>
                    <td>{display(item.status)}</td>
                    <td>{display(item.due_date)}</td>
                    <td>{display(item.due_time)}</td>
                    <td>
                      {display(item.last_activity_summary)}
                      {item.last_activity_at && <span className="queue-sub">{item.last_activity_at}</span>}
                    </td>
                    <td>{display(displayNextAction(item.next_action, nextActionCatalog) || item.next_action)}</td>
                    <td className={`wq-insight ${alignmentClass}`}>
                      {!insight || !insight.evaluated ? (
                        <div className="wq-insight__compact">
                          <span className="wq-insight__kicker">NorthStar Insight</span>
                          <span>Not Evaluated</span>
                          {item.external_record_no ? (
                            <Link
                              className="link-btn"
                              to={`/companies/${encodeURIComponent(item.external_record_no)}/research?client_id=${item.client_id}`}
                            >
                              Research Company
                            </Link>
                          ) : null}
                        </div>
                      ) : (
                        <div className="wq-insight__compact">
                          <span className="wq-insight__kicker">NorthStar Insight</span>
                          {insight.review_flag ? (
                            <span className="wq-insight__flag">{insight.review_flag}</span>
                          ) : (
                            <span className="wq-insight__alignment">{insight.alignment}</span>
                          )}
                          <span>Fit: {insight.fit}</span>
                          <span>Engagement: {insight.engagement}</span>
                          <span>Recommendation: {insight.recommendation}</span>
                          {insight.signals.length > 0 ? (
                            <span className="queue-sub">Signals: {insight.signals.slice(0, 3).join(', ')}</span>
                          ) : null}
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() =>
                              setExpandedInsightId(expanded ? null : item.queue_item_id)
                            }
                          >
                            {expanded ? 'Hide detail' : 'Why / Evidence'}
                          </button>
                          {expanded ? (
                            <div className="wq-insight__detail">
                              {insight.why ? (
                                <p>
                                  <strong>Why:</strong> {insight.why}
                                </p>
                              ) : null}
                              {insight.review_reason ? (
                                <p>
                                  <strong>Review reason:</strong> {insight.review_reason}
                                </p>
                              ) : null}
                              {insight.evidence_used.length > 0 ? (
                                <p>
                                  <strong>Evidence:</strong> {insight.evidence_used.join('; ')}
                                </p>
                              ) : null}
                              {insight.still_need_to_know.length > 0 ? (
                                <p>
                                  <strong>Missing:</strong>{' '}
                                  {insight.still_need_to_know.slice(0, 3).join('; ')}
                                </p>
                              ) : null}
                              <p className="queue-sub">{insight.advisory_note}</p>
                            </div>
                          ) : null}
                        </div>
                      )}
                    </td>
                    <td>
                      <div className="opportunity-actions">
                        <button type="button" className="link-btn" onClick={() => openItem(item, index)}>
                          Open Company
                        </button>
                        <button type="button" className="link-btn" onClick={() => void quickLogCall(item, index)}>
                          Log Call
                        </button>
                        <button type="button" className="link-btn" onClick={() => void quickAddNote(item)}>
                          Add Note
                        </button>
                        {schedulingKey === item.queue_item_id ? (
                          <div className="task-complete-inline">
                            <label className="edit-field">
                              <span className="edit-field__label">Follow-Up Date</span>
                              <input
                                className="edit-input"
                                type="date"
                                value={scheduleDate}
                                onChange={(e) => setScheduleDate(e.target.value)}
                              />
                            </label>
                            <label className="edit-field">
                              <span className="edit-field__label">Follow-Up Time</span>
                              <input
                                className="edit-input"
                                type="time"
                                value={scheduleTime}
                                onChange={(e) => setScheduleTime(e.target.value)}
                              />
                            </label>
                            <NextActionFields
                              catalog={nextActionCatalog}
                              value={scheduleNextSel}
                              onChange={setScheduleNextSel}
                              idPrefix={`wq-schedule-${item.queue_item_id}`}
                            />
                            <div className="edit-actions">
                              <button
                                type="button"
                                className="primary-btn"
                                disabled={
                                  scheduleSaving ||
                                  !scheduleDate ||
                                  !nextActionIsValid(scheduleNextSel, nextActionCatalog)
                                }
                                onClick={() => void saveScheduledFollowUp(item)}
                              >
                                {scheduleSaving ? 'Saving…' : 'Save Follow-Up'}
                              </button>
                              <button
                                type="button"
                                className="link-btn"
                                disabled={scheduleSaving}
                                onClick={() => setSchedulingKey(null)}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <button type="button" className="link-btn" onClick={() => void quickScheduleFollowUp(item)}>
                            Schedule Follow-Up
                          </button>
                        )}
                        {item.work_type === 'Follow-Up' && item.contact_id ? (
                          <button type="button" className="link-btn" onClick={() => void quickCompleteFollowUp(item)}>
                            Complete
                          </button>
                        ) : null}
                        {item.work_type === 'New Assignment' && (
                          <button type="button" className="link-btn" onClick={() => openItem(item, index)}>
                            Start Working
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {total > PAGE_SIZE ? (
            <div className="queue-pagination" style={{ display: 'flex', gap: '1rem', alignItems: 'center', marginTop: '0.75rem' }}>
              <button
                type="button"
                className="link-btn"
                disabled={!hasPrevious || loading || page <= 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                Previous
              </button>
              <span className="queue-source">
                Page {page + 1} of {Math.max(1, Math.ceil(total / PAGE_SIZE))}
              </span>
              <button
                type="button"
                className="link-btn"
                disabled={!hasNext || loading}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          ) : null}
          </>
        )}
      </section>
    </>
  )
}
