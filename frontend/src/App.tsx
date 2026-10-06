import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Link,
  matchPath,
  Navigate,
  useLocation,
  useNavigate,
  useSearchParams,
} from 'react-router-dom'
import {
  companyWorkspaceHref,
  createCompanyNote,
  createMilestone,
  fetchClientStatuses,
  fetchNextActions,
  fetchCompanyActivities,
  fetchCompanyByRecordNo,
  fetchCrossClientBanner,
  fetchOpportunityCount,
  fetchPriorityProspects,
  fetchProspects,
  fetchSharedHistory,
  fetchWorkQueue,
  fetchWorkQueueNext,
  fetchDashboardFollowUps,
  fetchAssignedClients,
  fetchAppointmentSummary,
  fetchMilestoneSummary,
  fetchClientActivities,
  globalSearch,
  isCampaignRouteStatus,
  logOutreach,
  logWorkQueueCall,
  setCompanyHot,
  updateCompanyStatus,
} from './api/carmeco'
import { pathAfterActiveClientChange } from './activeClientNavigation'
import { isFromAskNorthStar, withAskReturnParam } from './askNorthStarReturn'
import {
  appendWorkspaceOrigin,
  copyWorkspaceReturn,
  SEARCH_FROM,
  DASHBOARD_FROM,
  PROSPECTS_FROM,
  workspaceReturnLabel,
  workspaceReturnPath,
} from './workspaceReturn'
import {
  APPOINTMENTS_SET_SUBTITLE,
  revenueOpportunityCountsFromSummary,
  type RevenueOpportunityCounts,
} from './revenueOpportunityKpis'
import {
  formatClientStatusChip,
  sortedClientStatuses,
} from './clientStatusesDisplay'
import { formatDisplayDateTime } from './formatDisplayDateTime'
import { SELECT_CLIENT_FOR_WRITE } from './writeClient'
import type {
  ActiveClient,
  ActivitySummary,
  CompanyWorkspace,
  ProspectListItem,
  RevenueMilestoneType,
  SearchHit,
  SearchResponse,
  SharedHistoryResponse,
  CrossClientBanner,
  WorkQueueRow,
  WorkQueueSummaryV2,
  ActivityTimelineRow,
  DashboardFollowUpItem,
  DashboardFollowUpsResponse,
} from './types/carmeco'
import type { AppointmentSummary } from './api/carmeco'
import type { PriorityProspectItem } from './api/carmeco'
import CrossClientOpportunities from './CrossClientOpportunities'
import WorkQueue from './WorkQueue'
import ClientSetup from './ClientSetup'
import ClientOnboarding from './ClientOnboarding'
import ClientKnowledge from './ClientKnowledge'
import ContactWorkspacePage from './ContactWorkspace'
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
import Contacts from './Contacts'
import AddContactModal from './AddContactModal'
import AddCompanyModal from './AddCompanyModal'
import ProspectBulkAssign, {
  emptySelection,
  isRowSelected,
  pageAllSelected,
  prospectFilterKey,
  selectCurrentPage,
  toggleRow,
  type ProspectSelection,
} from './ProspectBulkAssign'
import { fetchBulkAssignees, type BulkAssignee } from './api/bulkAssignment'
import Tasks from './Tasks'
import Appointments from './Appointments'
import Activities from './Activities'
import Reports from './Reports'
import Administration from './Administration'
import Campaigns from './Campaigns'
import CampaignWorkspacePage from './CampaignWorkspace'
import CampaignRoutePrompt, { type CampaignRouteOffer } from './CampaignRoutePrompt'
import AskNorthStar from './AskNorthStar'
import Research from './Research'
import ResearchCompany from './ResearchCompany'
import SendEmailCompose from './SendEmailCompose'
import AppointmentDetailsFields from './AppointmentDetailsFields'
import { useAuth } from './auth/useAuth'
import { staffCanAdminister } from './auth/staffCanAdminister'
import {
  shouldStayOnCompanyAfterCallSave,
  staffCanManageCampaigns,
  workQueueCallShouldRoute,
} from './auth/campaignAccess'
import FeedbackButton from './FeedbackButton'
import {
  dashboardStartHereMessage,
  isSingleAssignedClient,
  resolveActiveClientId,
  staffCanViewClientKnowledge,
  staffNavItemVisible,
  staffRoleLabel,
} from './auth/staffWorkspace'
import {
  northStarPilotLabel,
  showNorthStarPilotIndicator,
} from './auth/pilotIndicator'
import {
  APPOINTMENT_SET_STATUSES,
  appointmentDetailsAreValid,
  appointmentSourceForStatus,
  emptyAppointmentDetails,
  formatAppointmentWhen,
  isAppointmentSetStatus,
  type AppointmentDetails,
} from './appointmentSet'
import './App.css'

const WORKSPACE_USER = 'Julie Magnani'
const WORKSPACE_INITIALS = 'JR'
const ACTIVE_CLIENT_STORAGE_KEY = 'northstar_active_client_id'
const OUTREACH_ACTIVITY_ONLY_OUTCOMES = ['No Answer', 'Spoke With Contact', 'Follow-Up'] as const
const OUTREACH_TYPES = ['Call', 'Email', 'Other'] as const

function readStoredActiveClientId(): number | null {
  try {
    const raw = window.localStorage.getItem(ACTIVE_CLIENT_STORAGE_KEY)
    if (raw == null || raw === '') return null
    if (raw === 'all' || raw === '0') return 0
    const n = Number(raw)
    return Number.isFinite(n) && n >= 0 ? n : null
  } catch {
    return null
  }
}

function writeStoredActiveClientId(clientId: number | null) {
  try {
    if (clientId == null) window.localStorage.removeItem(ACTIVE_CLIENT_STORAGE_KEY)
    else if (clientId === 0) window.localStorage.setItem(ACTIVE_CLIENT_STORAGE_KEY, 'all')
    else window.localStorage.setItem(ACTIVE_CLIENT_STORAGE_KEY, String(clientId))
  } catch {
    /* ignore */
  }
}

type ProspectFilterKey =
  | 'calls-due-today'
  | 'follow-ups-due'
  | 'appointments-set'
  | 'quotes'
  | 'purchase-orders'
  | 'webleads'
  | 'hot'
  | 'new'
  | null

const PROSPECT_MILESTONE_FILTER_TYPES: Partial<
  Record<Exclude<ProspectFilterKey, null>, string>
> = {
  quotes: 'Quote',
  'purchase-orders': 'Purchase Order',
  webleads: 'WebLead',
  'appointments-set': 'Appointment Set',
}

const PROSPECT_FILTER_META: Record<
  Exclude<ProspectFilterKey, null>,
  { heading: string; empty: string; description: string }
> = {
  'calls-due-today': {
    heading: 'Calls Due Today',
    empty: 'No calls are due today.',
    description:
      'Prospects with an open Call task due today or overdue — not inferred from status.',
  },
  'follow-ups-due': {
    heading: 'Follow-Ups Due',
    empty: 'No follow-ups are due.',
    description:
      'Prospects with an explicit follow-up scheduled for today or overdue.',
  },
  'appointments-set': {
    heading: 'Appointments Set',
    empty: 'No appointments set.',
    description: 'Prospects with an Appointment Set revenue milestone.',
  },
  quotes: {
    heading: 'Quotes',
    empty: 'No quotes recorded.',
    description: 'Prospects with a Quote revenue milestone.',
  },
  'purchase-orders': {
    heading: 'Purchase Orders',
    empty: 'No purchase orders recorded.',
    description: 'Prospects with a Purchase Order revenue milestone.',
  },
  webleads: {
    heading: 'WebLeads',
    empty: 'No WebLeads recorded.',
    description: 'Prospects with a WebLead revenue milestone.',
  },
  hot: {
    heading: 'Hot Prospects',
    empty: 'No hot prospects.',
    description: 'Records marked hot for the Active Client.',
  },
  new: {
    heading: 'New Assignments',
    empty: 'No New Assignments are currently assigned to you.',
    description: 'Fresh calling book: assigned prospects with status New.',
  },
}

function contactNameFromWorkspace(
  workspace: CompanyWorkspace | null,
  contactId: number | null,
): string | null {
  if (!workspace || contactId == null) return null
  const contact = workspace.contacts.find((c) => c.id === contactId)
  if (!contact) return null
  const name = `${asText(contact.first_name)} ${asText(contact.last_name)}`.trim()
  return name || null
}

type WorkspaceHistoryEntry = {
  key: string
  focusId: string
  kind: 'legacy' | 'activity'
  text: string
  at: string
  user: string
  activityType: string
  contactName: string | null
  sourceLabel: string
}

function buildWorkspaceHistory(
  workspace: CompanyWorkspace | null,
  activities: ActivitySummary[],
): WorkspaceHistoryEntry[] {
  if (!workspace) return []
  // Current-client NorthStar activity only (newest first). Legacy is shown once
  // in Shared / Distinct LeadMaster sections below.
  const entries: WorkspaceHistoryEntry[] = activities.map((activity) => ({
    key: `activity-${activity.activity_id}`,
    focusId: `activity-${activity.activity_id}`,
    kind: 'activity' as const,
    text: asText(activity.notes) || asText(activity.outcome),
    at: asText(activity.activity_at) || asText(activity.created_at),
    user: asText(activity.created_by) || asText(activity.assigned_user),
    activityType: asText(activity.activity_type) || 'Activity',
    contactName: contactNameFromWorkspace(workspace, activity.contact_id),
    sourceLabel:
      asText(activity.activity_type).toLowerCase() === 'note'
        ? 'NorthStar Note'
        : 'NorthStar Activity',
  }))
  return entries.sort((a, b) => {
    const byDate = asText(b.at).localeCompare(asText(a.at))
    if (byDate !== 0) return byDate
    return b.key.localeCompare(a.key)
  })
}

function statusEquals(status: unknown, expected: string): boolean {
  return asText(status).toLowerCase() === expected.toLowerCase()
}

/** Calls due only when an actual Call task/date is scheduled (never from status alone). */
function isCallsDueToday(prospect: ProspectListItem): boolean {
  return prospect.call_due === true
}

function followUpContactHref(item: DashboardFollowUpItem): string {
  if (item.contact_id != null && item.contact_id > 0) {
    return `/contacts/${item.contact_id}?client_id=${item.client_id}`
  }
  if (item.external_record_no) {
    return `/companies/${encodeURIComponent(item.external_record_no)}?client_id=${item.client_id}`
  }
  return '#'
}

function followUpCountHref(items: DashboardFollowUpItem[], sectionId: string): string {
  if (items.length === 1) return followUpContactHref(items[0])
  return `#${sectionId}`
}

function followUpWhen(item: DashboardFollowUpItem): string {
  const datePart = (item.due_date || '').trim()
  const timePart = (item.due_time || '').trim().slice(0, 5)
  if (datePart && timePart) return `${datePart} ${timePart}`
  return datePart || timePart || '—'
}

function DashboardFollowUpList({
  id,
  title,
  empty,
  items,
  onNavigate,
}: {
  id: string
  title: string
  empty: string
  items: DashboardFollowUpItem[]
  onNavigate: () => void
}) {
  return (
    <div id={id} className="dashboard-follow-up-bucket">
      <h3>{title}</h3>
      {items.length === 0 ? (
        <p className="empty-state">{empty}</p>
      ) : (
        <ul className="appointment-list">
          {items.map((item) => (
            <li
              key={`${item.source}-${item.source_id}-${item.contact_id ?? item.company_id}`}
              className="appointment-item"
            >
              <Link
                to={followUpContactHref(item)}
                className="dashboard-follow-up-link"
                onClick={onNavigate}
              >
                <strong>{item.contact_name || item.company_name}</strong>
                <span>{item.contact_name ? item.company_name : item.external_record_no}</span>
                <time>{followUpWhen(item)}</time>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function isNewAssignment(prospect: ProspectListItem): boolean {
  return statusEquals(prospectStatus(prospect), 'New')
}

function isAppointmentToday(prospect: ProspectListItem): boolean {
  return /appointment/i.test(prospectStatus(prospect))
}

function parseProspectFilter(value: string | null): ProspectFilterKey {
  if (
    value === 'calls-due-today' ||
    value === 'follow-ups-due' ||
    value === 'appointments-set' ||
    value === 'quotes' ||
    value === 'purchase-orders' ||
    value === 'webleads' ||
    value === 'hot' ||
    value === 'new'
  ) {
    return value
  }
  return null
}

const navItems = [
  { id: 'dashboard', label: 'Dashboard', icon: '▣' },
  { id: 'work-queue', label: 'Work Queue', icon: '☰' },
  { id: 'prospects', label: 'Prospects', icon: '◎' },
  { id: 'companies', label: 'Companies', icon: '⌂' },
  { id: 'contacts', label: 'Contacts', icon: '☺' },
  { id: 'activities', label: 'Activities', icon: '▹' },
  { id: 'appointments', label: 'Appointments', icon: '◷' },
  { id: 'campaigns', label: 'Campaigns', icon: '◈' },
  { id: 'client-knowledge', label: 'Client Knowledge', icon: '◉' },
  { id: 'tasks', label: 'Tasks', icon: '✓' },
  { id: 'reports', label: 'Reports', icon: '▤' },
  { id: 'research', label: 'Research', icon: '⌕' },
  { id: 'ask-northstar', label: 'Ask NorthStar', icon: '✦' },
  { id: 'cross-client-opportunities', label: 'Cross-Client Opportunities', icon: '⇄' },
  { id: 'clients', label: 'Clients', icon: '◇' },
  { id: 'administration', label: 'Administration', icon: '⚙' },
]

function asText(value: unknown): string {
  if (value == null) return ''
  return String(value).trim()
}

function prospectStatus(prospect: {
  status?: string
  relationship_status?: string
}): string {
  // Active/Working client CCR status only — never a Carmeco-owned alias.
  return asText(prospect.status) || asText(prospect.relationship_status)
}

function withRelationshipStatus<T extends { status?: string; relationship_status?: string }>(
  row: T,
  status: string,
): T {
  const next = asText(status)
  return { ...row, status: next, relationship_status: next }
}

function displayOrDash(value: unknown): string {
  const text = asText(value)
  return text || '—'
}

function shortenAction(label: string): string {
  return label.length > 48 ? `${label.slice(0, 48)}…` : label
}

/** UI-only labels for backend priority_bucket (ordering unchanged). */
function priorityBucketLabel(bucket: number): string {
  switch (bucket) {
    case 1:
      return 'OVERDUE'
    case 2:
      return 'DUE TODAY'
    case 3:
      return 'HOT'
    case 4:
      return 'NEXT ACTION'
    case 5:
      return 'NEW'
    default:
      return '—'
  }
}

function priorityBucketTone(bucket: number): 'a' | 'b' | 'c' {
  if (bucket <= 2) return 'a'
  if (bucket <= 4) return 'b'
  return 'c'
}

function greetingForNow(date = new Date()): string {
  const hour = date.getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

function staffInitials(authenticated: boolean, fullName: string): string {
  if (!authenticated) return WORKSPACE_INITIALS
  const parts = fullName.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return WORKSPACE_INITIALS
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return `${parts[0][0] ?? ''}${parts[parts.length - 1][0] ?? ''}`.toUpperCase()
}

function greetingFirstName(authenticated: boolean, fullName: string): string {
  if (!authenticated) return 'Julie'
  return fullName.trim().split(/\s+/).filter(Boolean)[0] || 'Julie'
}

function formatToday(date = new Date()): string {
  return date.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function buildWorkQueueCards(
  prospects: ProspectListItem[],
  activeClientId: number | null,
  needsNextActionCount: number | null,
  opportunityCount: number | null,
  followUps: DashboardFollowUpsResponse | null,
  appointmentTodayCount: number | null,
  queueSummary: WorkQueueSummaryV2 | null = null,
) {
  const scopeQuery =
    activeClientId != null && activeClientId > 0
      ? `client_id=${activeClientId}&`
      : ''
  const oppTarget =
    activeClientId != null && activeClientId > 0
      ? `?target_client_id=${activeClientId}`
      : ''
  const overdueItems = followUps?.overdue ?? []
  const dueTodayItems = followUps?.due_today ?? []
  const upcomingItems = followUps?.upcoming ?? []
  return [
    {
      id: 'calls-due-today',
      label: 'Calls Due Today',
      value:
        queueSummary != null
          ? String(queueSummary.calls_due)
          : String(prospects.filter(isCallsDueToday).length),
      change: 'Scheduled call tasks',
      tone: 'work' as const,
      icon: '☎',
      lane: 'due' as const,
      to: `/work-queue?${scopeQuery}type=call&due=today`,
    },
    {
      id: 'follow-ups-overdue',
      label: 'Overdue Follow-Ups',
      value: followUps ? String(followUps.overdue_count) : '—',
      change: 'Past due open tasks',
      tone: 'work' as const,
      icon: '⚠',
      lane: 'due' as const,
      to: followUpCountHref(overdueItems, 'dashboard-follow-ups-overdue'),
    },
    {
      id: 'follow-ups-due',
      label: 'Follow-Ups Due Today',
      value: followUps ? String(followUps.due_today_count) : '—',
      change: 'Open tasks due today',
      tone: 'work' as const,
      icon: '↻',
      lane: 'due' as const,
      to: followUpCountHref(dueTodayItems, 'dashboard-follow-ups-today'),
    },
    {
      id: 'follow-ups-upcoming',
      label: 'Upcoming Follow-Ups',
      value: followUps ? String(followUps.upcoming_count) : '—',
      change: 'Next 7+ days',
      tone: 'work' as const,
      icon: '◷',
      lane: 'due' as const,
      to: followUpCountHref(upcomingItems, 'dashboard-follow-ups-upcoming'),
    },
    {
      id: 'new',
      label: 'New Assignments',
      value:
        queueSummary != null
          ? String(queueSummary.new_assignments)
          : String(prospects.filter(isNewAssignment).length),
      change: 'Fresh calling book — assigned New records',
      tone: 'work' as const,
      icon: '＋',
      lane: 'fresh' as const,
      to: `/work-queue?${scopeQuery}type=new`,
    },
    {
      id: 'needs-next-action',
      label: 'Needs Next Action',
      value: needsNextActionCount != null ? String(needsNextActionCount) : '—',
      change: 'Working status, no follow-up',
      tone: 'work' as const,
      icon: '▹',
      lane: 'due' as const,
      to: `/work-queue?${scopeQuery}type=needs-next-action`,
    },
    {
      id: 'appointments-today',
      label: 'Appointments Today',
      value:
        appointmentTodayCount != null
          ? String(appointmentTodayCount)
          : String(prospects.filter(isAppointmentToday).length),
      change: 'Scheduled appointments',
      tone: 'work' as const,
      icon: '◷',
      lane: 'due' as const,
      to: '/appointments?filter=today',
    },
    {
      id: 'cross-client',
      label: 'Cross-Client Opportunities',
      value: opportunityCount != null ? String(opportunityCount) : '—',
      change: 'Actionable for Active Client',
      tone: 'work' as const,
      icon: '⇄',
      lane: 'due' as const,
      to: `/cross-client-opportunities${oppTarget}`,
    },
  ]
}

function buildKpiCards(
  prospects: ProspectListItem[],
  revenueCounts: RevenueOpportunityCounts | null,
  hotCount: number | null,
) {
  return [
    {
      id: 'appointments',
      label: 'Appointments Set',
      value:
        revenueCounts != null
          ? String(revenueCounts.appointments_set)
          : String(prospects.filter((prospect) => prospect.has_appointment_set).length),
      change: APPOINTMENTS_SET_SUBTITLE,
      tone: 'appointments' as const,
      icon: '◷',
      to: '/appointments',
    },
    {
      id: 'quotes',
      label: 'Quotes',
      value:
        revenueCounts != null
          ? String(revenueCounts.quotes)
          : String(prospects.filter((prospect) => prospect.has_quote).length),
      change: 'Quote milestones',
      tone: 'quotes' as const,
      icon: '▤',
      to: '/prospects?filter=quotes',
    },
    {
      id: 'purchase-orders',
      label: 'Purchase Orders',
      value:
        revenueCounts != null
          ? String(revenueCounts.purchase_orders)
          : String(prospects.filter((prospect) => prospect.has_purchase_order).length),
      change: 'Revenue milestone',
      tone: 'purchase-orders' as const,
      icon: '✓',
      to: '/prospects?filter=purchase-orders',
    },
    {
      id: 'webleads',
      label: 'WebLeads',
      value:
        revenueCounts != null
          ? String(revenueCounts.webleads)
          : String(prospects.filter((prospect) => prospect.has_weblead).length),
      change: 'Revenue milestone',
      tone: 'webleads' as const,
      icon: '⌕',
      to: '/prospects?filter=webleads',
    },
    {
      id: 'hot',
      label: 'Hot',
      value: hotCount != null ? String(hotCount) : '—',
      change: 'Current status = Hot Prospect',
      tone: 'hot' as const,
      icon: '▲',
      to: '/work-queue?type=hot',
    },
  ]
}

function parseActivityDate(value: string): Date | null {
  const text = (value || '').trim()
  if (!text) return null
  const iso = text.includes('T') ? text : text.replace(' ', 'T')
  const dt = new Date(iso)
  return Number.isNaN(dt.getTime()) ? null : dt
}

function formatExactActivityWhen(value: string): string {
  const dt = parseActivityDate(value)
  if (!dt) return (value || '').trim()
  return dt.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function formatRelativeActivityWhen(value: string): string {
  const dt = parseActivityDate(value)
  if (!dt) return (value || '').trim() || '—'
  const diffMs = Date.now() - dt.getTime()
  const abs = Math.abs(diffMs)
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (abs < minute) return 'just now'
  const suffix = diffMs >= 0 ? 'ago' : 'from now'
  if (abs < hour) {
    const n = Math.max(1, Math.round(abs / minute))
    return `${n} min ${suffix}`
  }
  if (abs < day) {
    const n = Math.max(1, Math.round(abs / hour))
    return n === 1 ? `1 hr ${suffix}` : `${n} hr ${suffix}`
  }
  if (abs < 7 * day) {
    const n = Math.max(1, Math.round(abs / day))
    if (diffMs >= 0) return n === 1 ? 'Yesterday' : `${n} days ago`
    return n === 1 ? 'Tomorrow' : `in ${n} days`
  }
  return formatExactActivityWhen(value)
}

function activityTone(activityType: string): string {
  const t = activityType.toLowerCase()
  if (t.includes('appointment')) return 'meeting'
  if (t.includes('email')) return 'email'
  if (t.includes('call') || t.includes('voicemail')) return 'lead'
  if (t.includes('follow')) return 'pipeline'
  if (t.includes('campaign')) return 'pipeline'
  return 'view'
}

function recentActivityHref(row: ActivityTimelineRow): string {
  if ((row.activity_type || '').toLowerCase().includes('campaign')) {
    return `/campaigns?client_id=${row.client_id}`
  }
  if (row.contact_id != null && row.contact_id > 0) {
    return `/contacts/${row.contact_id}?client_id=${row.client_id}`
  }
  if (row.external_record_no) {
    return companyWorkspaceHref(row.external_record_no, row.client_id)
  }
  return '#'
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="info-row">
      <dt>{label}</dt>
      <dd>{displayOrDash(value)}</dd>
    </div>
  )
}

function navIdFromPath(pathname: string): string {
  if (pathname === '/login') return 'login'
  if (matchPath({ path: '/companies/:recordNo/research', end: true }, pathname)) {
    return 'research'
  }
  if (pathname === '/contacts' || matchPath({ path: '/contacts/:contactId', end: true }, pathname)) {
    return 'contacts'
  }
  if (matchPath({ path: '/companies/:recordNo', end: true }, pathname)) {
    return 'prospects'
  }
  if (
    matchPath({ path: '/clients/:clientId/setup', end: true }, pathname) ||
    matchPath({ path: '/clients/:clientId/knowledge', end: true }, pathname) ||
    pathname === '/clients' ||
    pathname === '/clients/onboarding' ||
    matchPath({ path: '/clients/onboarding', end: true }, pathname)
  ) {
    return 'clients'
  }
  if (pathname === '/client-knowledge') return 'client-knowledge'
  if (pathname === '/prospects') return 'prospects'
  if (pathname === '/appointments') return 'appointments'
  if (pathname === '/campaigns' || matchPath({ path: '/campaigns/:campaignId', end: true }, pathname)) {
    return 'campaigns'
  }
  if (pathname === '/' || pathname === '/dashboard') return 'dashboard'
  const segment = pathname.replace(/^\//, '').split('/')[0]
  return segment || 'dashboard'
}

function pathForNav(navId: string, activeClientId?: number | null): string {
  if (navId === 'dashboard') return '/'
  if (navId === 'work-queue' && activeClientId != null && activeClientId > 0) {
    return `/work-queue?client_id=${activeClientId}`
  }
  if (navId === 'campaigns' && activeClientId != null && activeClientId > 0) {
    return `/campaigns?client_id=${activeClientId}`
  }
  if (navId === 'client-knowledge') return '/client-knowledge'
  if (navId === 'reports' && activeClientId != null && activeClientId > 0) {
    return `/reports?client_id=${activeClientId}`
  }
  if (navId === 'cross-client-opportunities' && activeClientId != null && activeClientId > 0) {
    return `/cross-client-opportunities?target_client_id=${activeClientId}`
  }
  return `/${navId}`
}

function App() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { authenticated, user, authAvailable, authEnforced, logout } = useAuth()
  const canAdminister = staffCanAdminister(authenticated, user)
  const canViewClientKnowledge = staffCanViewClientKnowledge(user)
  const canManageCampaigns = staffCanManageCampaigns(user)
  const visibleNavItems = navItems.filter((item) =>
    staffNavItemVisible(item.id, canAdminister, canViewClientKnowledge),
  )
  const roleLabel = staffRoleLabel(authenticated, user)
  const displayName =
    authenticated && user?.full_name.trim() ? user.full_name.trim() : WORKSPACE_USER
  const displayInitials = staffInitials(authenticated, displayName)
  const greetingName = greetingFirstName(authenticated, displayName)
  const activeNav = navIdFromPath(location.pathname)
  const companyMatch = matchPath(
    { path: '/companies/:recordNo', end: true },
    location.pathname,
  )
  const researchMatch = matchPath(
    { path: '/companies/:recordNo/research', end: true },
    location.pathname,
  )
  const clientSetupMatch = matchPath(
    { path: '/clients/:clientId/setup', end: true },
    location.pathname,
  )
  const clientKnowledgeMatch = matchPath(
    { path: '/clients/:clientId/knowledge', end: true },
    location.pathname,
  )
  const showClientOnboarding =
    location.pathname === '/clients/onboarding' ||
    Boolean(matchPath({ path: '/clients/onboarding', end: true }, location.pathname))
  const contactMatch = matchPath(
    { path: '/contacts/:contactId', end: true },
    location.pathname,
  )
  const campaignMatch = matchPath(
    { path: '/campaigns/:campaignId', end: true },
    location.pathname,
  )
  const researchRecordNo = researchMatch?.params.recordNo
    ? decodeURIComponent(researchMatch.params.recordNo)
    : null
  const clientSetupId = clientSetupMatch?.params.clientId
    ? Number(clientSetupMatch.params.clientId)
    : null
  const clientKnowledgeId = clientKnowledgeMatch?.params.clientId
    ? Number(clientKnowledgeMatch.params.clientId)
    : null
  const contactWorkspaceId = contactMatch?.params.contactId
    ? Number(contactMatch.params.contactId)
    : null
  const campaignWorkspaceId = campaignMatch?.params.campaignId
    ? Number(campaignMatch.params.campaignId)
    : null
  const showClientSetup =
    activeNav === 'clients' &&
    (location.pathname === '/clients' ||
      Boolean(clientSetupId) ||
      Boolean(clientKnowledgeId) ||
      showClientOnboarding)
  const selectedRecordNo = companyMatch?.params.recordNo
    ? decodeURIComponent(companyMatch.params.recordNo)
    : null
  const prospectFilter = parseProspectFilter(searchParams.get('filter'))
  const prospectStatusFilter = asText(searchParams.get('status'))
  const prospectAssignedFilter = asText(searchParams.get('assigned'))
  const assignedUserIdForApi =
    !prospectAssignedFilter
      ? undefined
      : prospectAssignedFilter === 'unassigned'
        ? 0
        : Number.isFinite(Number(prospectAssignedFilter))
          ? Number(prospectAssignedFilter)
          : undefined
  const fromWorkQueue = searchParams.get('from') === 'work-queue'
  const fromAskNorthStar = isFromAskNorthStar(searchParams.get('from'))
  const workQueueClientId = searchParams.get('client_id')
  const workQueueItemId = searchParams.get('queue_item')
  const workQueueType = searchParams.get('work_type')
  const workQueueReturn = searchParams.get('queue') || ''
  const workQueueReason = searchParams.get('reason') || ''
  const workQueuePriority = searchParams.get('priority') || ''
  const workQueuePosition = searchParams.get('position') || ''
  const workQueueTotal = searchParams.get('total') || ''
  const focusTarget = searchParams.get('focus')
  const selectedClientIdParam = searchParams.get('client_id')
  const selectedClientId = selectedClientIdParam ? Number(selectedClientIdParam) : null

  const [search, setSearch] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchResults, setSearchResults] = useState<SearchResponse | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [workspaceActivities, setWorkspaceActivities] = useState<ActivitySummary[]>([])
  const [focusFlashId, setFocusFlashId] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [client, setClient] = useState<ActiveClient | null>(null)
  const [availableClients, setAvailableClients] = useState<
    Array<{ client_id: number; client_name: string; client_code: string }>
  >([])
  const singleAssignedClient = isSingleAssignedClient(availableClients.length)
  const [activeClientId, setActiveClientId] = useState<number | null>(() => readStoredActiveClientId())
  const [prospects, setProspects] = useState<ProspectListItem[]>([])
  const [companyListQuery, setCompanyListQuery] = useState('')
  const [debouncedCompanyListQuery, setDebouncedCompanyListQuery] = useState('')
  const [companyListPage, setCompanyListPage] = useState(0)
  const [companyListRows, setCompanyListRows] = useState<ProspectListItem[]>([])
  const [companyListTotal, setCompanyListTotal] = useState(0)
  const [companyListClientTotal, setCompanyListClientTotal] = useState(0)
  const [companyListLoading, setCompanyListLoading] = useState(false)
  const [prospectSelection, setProspectSelection] = useState<ProspectSelection>(emptySelection())
  const [bulkAssignees, setBulkAssignees] = useState<BulkAssignee[]>([])
  const COMPANY_PAGE_SIZE = 50
  const companyListRequestSeq = useRef(0)
  const workQueueSaveSeq = useRef(0)
  const [queueRefreshKey, setQueueRefreshKey] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [workspace, setWorkspace] = useState<CompanyWorkspace | null>(null)
  const [workspaceLoading, setWorkspaceLoading] = useState(false)
  const [workspaceError, setWorkspaceError] = useState<string | null>(null)
  const [addContactOpen, setAddContactOpen] = useState(false)
  const [addCompanyOpen, setAddCompanyOpen] = useState(false)
  const [sendEmailOpen, setSendEmailOpen] = useState(false)
  const [campaignRouteOffer, setCampaignRouteOffer] = useState<CampaignRouteOffer | null>(null)
  const [contactFeedbackContext, setContactFeedbackContext] = useState<{
    companyId: number | null
    contactId: number | null
  }>({ companyId: null, contactId: null })
  const reportContactFeedbackContext = useCallback(
    (next: { companyId: number | null; contactId: number | null }) => {
      setContactFeedbackContext((prev) =>
        prev.companyId === next.companyId && prev.contactId === next.contactId ? prev : next,
      )
    },
    [],
  )
  const [sendEmailPreferredContactId, setSendEmailPreferredContactId] = useState<
    number | null
  >(null)
  const [sendEmailPreferredTemplateName, setSendEmailPreferredTemplateName] =
    useState<string | null>(null)
  /** Navigate here after SendEmailCompose closes (deferred Save & Next). */
  const pendingNextAfterEmailRef = useRef<string | null>(null)
  const [statusOptions, setStatusOptions] = useState<string[]>([])
  const [nextActionCatalog, setNextActionCatalog] = useState<NextActionCatalog>(emptyNextActionCatalog())
  const [draftStatus, setDraftStatus] = useState('')
  const [newNoteText, setNewNoteText] = useState('')
  const [statusSaving, setStatusSaving] = useState(false)
  const [noteSaving, setNoteSaving] = useState(false)
  const [statusSaveMsg, setStatusSaveMsg] = useState<string | null>(null)
  const [noteSaveMsg, setNoteSaveMsg] = useState<string | null>(null)
  const [statusSaveError, setStatusSaveError] = useState<string | null>(null)
  const [noteSaveError, setNoteSaveError] = useState<string | null>(null)
  const [milestoneType, setMilestoneType] = useState<RevenueMilestoneType>('Quote')
  const [milestoneDate, setMilestoneDate] = useState('')
  const [milestoneReference, setMilestoneReference] = useState('')
  const [milestoneAmount, setMilestoneAmount] = useState('')
  const [milestoneSource, setMilestoneSource] = useState('')
  const [milestoneContactId, setMilestoneContactId] = useState('')
  const [milestoneNotes, setMilestoneNotes] = useState('')
  const [milestoneSaving, setMilestoneSaving] = useState(false)
  const [milestoneSaveMsg, setMilestoneSaveMsg] = useState<string | null>(null)
  const [milestoneSaveError, setMilestoneSaveError] = useState<string | null>(null)
  const [hotSaving, setHotSaving] = useState(false)
  const [opportunityCount, setOpportunityCount] = useState<number | null>(null)
  const [needsNextActionCount, setNeedsNextActionCount] = useState<number | null>(null)
  const [hotCount, setHotCount] = useState<number | null>(null)
  const [workQueueSummary, setWorkQueueSummary] = useState<WorkQueueSummaryV2 | null>(null)
  const [dashboardFollowUps, setDashboardFollowUps] = useState<DashboardFollowUpsResponse | null>(
    null,
  )
  const [crossClientBanner, setCrossClientBanner] = useState<CrossClientBanner | null>(null)
  const [sharedHistory, setSharedHistory] = useState<SharedHistoryResponse | null>(null)
  const [sharedHistoryFilter, setSharedHistoryFilter] = useState<number | 0>(0)
  const [sharedHistoryLoading, setSharedHistoryLoading] = useState(false)
  const [workQueueContext, setWorkQueueContext] = useState<WorkQueueRow | null>(null)
  const [callContactId, setCallContactId] = useState('')
  const [callOutcome, setCallOutcome] = useState('')
  const [callNotes, setCallNotes] = useState('')
  const [callStatus, setCallStatus] = useState('')
  const [callNextSel, setCallNextSel] = useState<NextActionSelection>({ code: '', custom: '' })
  const [callFollowUpDate, setCallFollowUpDate] = useState('')
  const [callFollowUpTime, setCallFollowUpTime] = useState('')
  const [callSaving, setCallSaving] = useState(false)
  const [callSaveMsg, setCallSaveMsg] = useState<string | null>(null)
  const [callSaveError, setCallSaveError] = useState<string | null>(null)
  const [callAppointmentDetails, setCallAppointmentDetails] = useState<AppointmentDetails>(
    emptyAppointmentDetails,
  )
  const [queuePositionLabel, setQueuePositionLabel] = useState('')
  const [outreachContactId, setOutreachContactId] = useState('')
  const [outreachType, setOutreachType] = useState<(typeof OUTREACH_TYPES)[number]>('Call')
  const [outreachOutcome, setOutreachOutcome] = useState('')
  const [outreachNotes, setOutreachNotes] = useState('')
  const [outreachNextSel, setOutreachNextSel] = useState<NextActionSelection>({ code: '', custom: '' })
  const [outreachFollowUpDate, setOutreachFollowUpDate] = useState('')
  const [outreachSaving, setOutreachSaving] = useState(false)
  const [outreachMsg, setOutreachMsg] = useState<string | null>(null)
  const [outreachError, setOutreachError] = useState<string | null>(null)
  const [priorityQueue, setPriorityQueue] = useState<PriorityProspectItem[]>([])
  const [appointmentSummary, setAppointmentSummary] = useState<AppointmentSummary | null>(null)
  const [milestoneSummary, setMilestoneSummary] = useState<RevenueOpportunityCounts | null>(null)
  const [recentActivities, setRecentActivities] = useState<ActivityTimelineRow[]>([])
  const [salesEventFilter, setSalesEventFilter] = useState<
    'all' | 'appointments' | 'rfq' | 'send'
  >('all')

  // Load assigned clients once — never rewrite Active Client from this list fetch.
  useEffect(() => {
    let cancelled = false
    void fetchAssignedClients()
      .then((clients) => {
        if (cancelled) return
        setAvailableClients(clients)
      })
      .catch(() => {
        if (!cancelled) setAvailableClients([])
      })
    return () => {
      cancelled = true
    }
  }, [authenticated, user?.id])

  // Hydrate Active Client from localStorage or first assignment (no Carmeco hard-code).
  // One-client specialists are pinned to that client so leftover "All My Clients" is not shown.
  useEffect(() => {
    if (availableClients.length === 0) {
      return
    }
    const next = resolveActiveClientId({
      availableClientIds: availableClients.map((c) => c.client_id),
      storedOrCurrent: activeClientId,
    })
    if (next === activeClientId) return
    setActiveClientId(next)
    if (next != null) writeStoredActiveClientId(next)
  }, [activeClientId, availableClients])

  // Prospects / statuses follow Active Client only — never reset activeClientId here.
  useEffect(() => {
    if (activeClientId == null) return
    let cancelled = false

    async function loadScopedData() {
      setLoading(true)
      setError(null)
      try {
        const allClients = activeClientId === 0
        const [data, statuses, nextActions] = await Promise.all([
          fetchProspects({
            client_id: allClients ? null : activeClientId,
            all_clients: allClients,
            limit: COMPANY_PAGE_SIZE,
            offset: 0,
            assigned_user_id: assignedUserIdForApi,
          }),
          fetchClientStatuses(allClients ? null : activeClientId),
          fetchNextActions(allClients ? null : activeClientId).catch(() =>
            emptyNextActionCatalog(),
          ),
        ])
        if (cancelled) return
        setClient(data.client)
        setProspects(data.prospects)
        setCompanyListRows(data.prospects)
        setCompanyListTotal(data.total)
        setCompanyListClientTotal(data.client_total)
        setStatusOptions(statuses)
        setNextActionCatalog(nextActions)
        if (!allClients && activeClientId != null && activeClientId > 0) {
          try {
            const queue = await fetchPriorityProspects(activeClientId, 50)
            if (!cancelled) setPriorityQueue(queue)
          } catch {
            if (!cancelled) setPriorityQueue([])
          }
        } else if (!cancelled) {
          setPriorityQueue([])
        }
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Failed to load prospects.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void loadScopedData()
    return () => {
      cancelled = true
    }
  }, [activeClientId])

  useEffect(() => {
    const timer = window.setTimeout(
      () => setDebouncedCompanyListQuery(companyListQuery.trim()),
      250,
    )
    return () => window.clearTimeout(timer)
  }, [companyListQuery])

  useEffect(() => {
    setCompanyListPage(0)
  }, [debouncedCompanyListQuery, activeClientId, activeNav, prospectFilter, prospectStatusFilter, prospectAssignedFilter])

  useEffect(() => {
    if (activeClientId == null) {
      setCompanyListRows([])
      setCompanyListTotal(0)
      setCompanyListClientTotal(0)
      return
    }
    if (activeNav !== 'companies' && activeNav !== 'prospects') return

    const statusForApi =
      prospectStatusFilter ||
      (prospectFilter === 'hot'
        ? 'Hot Prospect'
        : prospectFilter === 'new'
          ? 'New'
          : '')

    const milestoneTypeForApi =
      prospectFilter && PROSPECT_MILESTONE_FILTER_TYPES[prospectFilter]
        ? PROSPECT_MILESTONE_FILTER_TYPES[prospectFilter]
        : undefined

    // Non-milestone shortcuts (calls/follow-ups) still belong on Work Queue.
    if (
      prospectFilter &&
      !milestoneTypeForApi &&
      prospectFilter !== 'hot' &&
      prospectFilter !== 'new' &&
      !prospectStatusFilter
    ) {
      setCompanyListRows([])
      setCompanyListTotal(0)
      setCompanyListClientTotal(0)
      setCompanyListLoading(false)
      return
    }

    let cancelled = false
    const requestId = ++companyListRequestSeq.current
    setCompanyListLoading(true)
    const allClients = activeClientId === 0
    void fetchProspects({
      client_id: allClients ? null : activeClientId,
      all_clients: allClients,
      q: debouncedCompanyListQuery,
      status: statusForApi || undefined,
      milestone_type: milestoneTypeForApi,
      assigned_user_id: assignedUserIdForApi,
      limit: COMPANY_PAGE_SIZE,
      offset: companyListPage * COMPANY_PAGE_SIZE,
    })
      .then((data) => {
        if (cancelled || requestId !== companyListRequestSeq.current) return
        setCompanyListRows(data.prospects)
        setCompanyListTotal(data.total)
        setCompanyListClientTotal(data.client_total)
        setProspects(data.prospects)
        if (data.client) setClient(data.client)
        // Empty last page after deletes: step back one page.
        if (
          data.prospects.length === 0 &&
          data.total > 0 &&
          companyListPage > 0 &&
          companyListPage * COMPANY_PAGE_SIZE >= data.total
        ) {
          setCompanyListPage(Math.max(0, Math.ceil(data.total / COMPANY_PAGE_SIZE) - 1))
        }
      })
      .catch(() => {
        if (cancelled || requestId !== companyListRequestSeq.current) return
        setCompanyListRows([])
        setCompanyListTotal(0)
      })
      .finally(() => {
        if (!cancelled && requestId === companyListRequestSeq.current) {
          setCompanyListLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [
    activeClientId,
    activeNav,
    debouncedCompanyListQuery,
    companyListPage,
    prospectFilter,
    prospectStatusFilter,
    prospectAssignedFilter,
    assignedUserIdForApi,
  ])

  const prospectBulkFilter = {
    clientId: activeClientId && activeClientId > 0 ? activeClientId : 0,
    q: debouncedCompanyListQuery,
    status:
      prospectStatusFilter ||
      (prospectFilter === 'hot' ? 'Hot Prospect' : prospectFilter === 'new' ? 'New' : ''),
    milestoneType:
      prospectFilter && PROSPECT_MILESTONE_FILTER_TYPES[prospectFilter]
        ? String(PROSPECT_MILESTONE_FILTER_TYPES[prospectFilter])
        : '',
    assignedUserId: assignedUserIdForApi,
  }
  const prospectBulkFilterKey = prospectFilterKey(prospectBulkFilter)
  const showProspectBulk =
    canAdminister &&
    activeNav === 'prospects' &&
    activeClientId != null &&
    activeClientId > 0

  useEffect(() => {
    setProspectSelection(emptySelection(companyListTotal))
  }, [prospectBulkFilterKey, activeClientId, activeNav])

  useEffect(() => {
    if (!showProspectBulk) {
      setBulkAssignees([])
      return
    }
    let cancelled = false
    void fetchBulkAssignees(activeClientId as number)
      .then((rows) => {
        if (!cancelled) setBulkAssignees(rows)
      })
      .catch(() => {
        if (!cancelled) setBulkAssignees([])
      })
    return () => {
      cancelled = true
    }
  }, [showProspectBulk, activeClientId])

  useEffect(() => {
    let cancelled = false
    const scopedId = activeClientId != null && activeClientId > 0 ? activeClientId : null
    setDashboardFollowUps(null)
    setAppointmentSummary(null)
    setMilestoneSummary(null)
    const followUpsPromise =
      scopedId != null ? fetchDashboardFollowUps(scopedId) : Promise.resolve(null)
    const appointmentPromise =
      scopedId != null ? fetchAppointmentSummary(scopedId).catch(() => null) : Promise.resolve(null)
    const milestonePromise =
      activeClientId != null
        ? fetchMilestoneSummary(scopedId).catch(() => null)
        : Promise.resolve(null)
    void Promise.all([
      fetchOpportunityCount(scopedId),
      fetchWorkQueue({ client_id: scopedId, limit: 1, offset: 0 }),
      followUpsPromise,
      appointmentPromise,
      milestonePromise,
    ])
      .then(([count, queue, followUps, appointments, milestones]) => {
        if (cancelled) return
        setOpportunityCount(count)
        setWorkQueueSummary(queue.summary)
        setNeedsNextActionCount(queue.summary.needs_next_action ?? 0)
        setHotCount(queue.summary.hot ?? 0)
        setDashboardFollowUps(followUps)
        setAppointmentSummary(appointments)
        setMilestoneSummary(revenueOpportunityCountsFromSummary(milestones))
      })
      .catch(() => {
        if (cancelled) return
        setOpportunityCount(null)
        setWorkQueueSummary(null)
        setNeedsNextActionCount(null)
        setHotCount(null)
        setDashboardFollowUps(null)
        setAppointmentSummary(null)
        setMilestoneSummary(null)
      })
    return () => {
      cancelled = true
    }
  }, [activeClientId, queueRefreshKey])

  useEffect(() => {
    if (!selectedRecordNo) {
      setWorkspace(null)
      setWorkspaceError(null)
      setWorkspaceLoading(false)
      setSendEmailOpen(false)
      setWorkspaceActivities([])
      setDraftStatus('')
      setNewNoteText('')
      setStatusSaveMsg(null)
      setNoteSaveMsg(null)
      setStatusSaveError(null)
      setNoteSaveError(null)
      setMilestoneSaveMsg(null)
      setMilestoneSaveError(null)
      setMilestoneDate('')
      setMilestoneReference('')
      setMilestoneAmount('')
      setMilestoneSource('')
      setMilestoneContactId('')
      setMilestoneNotes('')
      setFocusFlashId(null)
      setCrossClientBanner(null)
      setWorkQueueContext(null)
      setCallContactId('')
      setCallOutcome('')
      setCallNotes('')
      setCallStatus('')
      setCallAppointmentDetails(emptyAppointmentDetails())
      setCallNextSel({ code: '', custom: '' })
      setCallFollowUpDate('')
      setCallFollowUpTime('')
      setCallSaveMsg(null)
      setCallSaveError(null)
      setQueuePositionLabel('')
      return
    }

    let cancelled = false

    async function loadWorkspace(recordNo: string) {
      setWorkspaceLoading(true)
      setWorkspaceError(null)
      setStatusSaveMsg(null)
      setNoteSaveMsg(null)
      setStatusSaveError(null)
      setNoteSaveError(null)
      setNewNoteText('')
      setMilestoneSaveMsg(null)
      setMilestoneSaveError(null)
      try {
        const workspaceClientId = (() => {
          if (workQueueClientId != null && workQueueClientId !== '') {
            const n = Number(workQueueClientId)
            if (Number.isFinite(n) && n > 0) return n
          }
          if (selectedClientId != null && Number.isFinite(selectedClientId) && selectedClientId > 0) {
            return selectedClientId
          }
          if (activeClientId != null && activeClientId > 0) return activeClientId
          if (client?.client_id && client.client_id > 0) return client.client_id
          return null
        })()
        setWorkspace(null)
        setDraftStatus('')
        const activityClient =
          availableClients.find((c) => c.client_id === workspaceClientId)?.client_name ||
          client?.name ||
          ''
        const [data, activities, banner, shared] = await Promise.all([
          fetchCompanyByRecordNo(recordNo, workspaceClientId),
          fetchCompanyActivities(recordNo, activityClient, workspaceClientId),
          fetchCrossClientBanner(recordNo, workspaceClientId),
          fetchSharedHistory(recordNo, {
            client_id: workspaceClientId,
            filter_client_id: null,
          }),
        ])
        if (cancelled) return
        setWorkspace(data)
        setWorkspaceActivities(activities)
        setCrossClientBanner(banner)
        setSharedHistory(shared)
        setSharedHistoryFilter(0)
        setDraftStatus(prospectStatus(data))
        if (fromWorkQueue) {
          try {
            const queueParams = new URLSearchParams(workQueueReturn)
            const scopeClient = queueParams.get('client_id')
            const result = await fetchWorkQueue({
              client_id: scopeClient ? Number(scopeClient) : null,
              type: queueParams.get('type') || workQueueType || undefined,
              due: queueParams.get('due') || undefined,
              status: queueParams.get('status') || undefined,
              priority: queueParams.get('priority') || undefined,
              hot: queueParams.get('hot') === '1',
              weblead: queueParams.get('weblead') === '1',
              cross_client: queueParams.get('cross_client') === '1',
              overdue: queueParams.get('due') === 'overdue',
              q: queueParams.get('q') || undefined,
              limit: 50,
              offset: 0,
            })
            if (cancelled) return
            const currentIndex = result.items.findIndex(
              (item) =>
                item.queue_item_id === workQueueItemId ||
                (item.external_record_no === recordNo &&
                  (!workQueueType || item.work_type === workQueueType)),
            )
            const current = currentIndex >= 0 ? result.items[currentIndex] : null
            setWorkQueueContext(current)
            if (current) {
              setCallContactId(current.contact_id != null ? String(current.contact_id) : '')
              setCallNextSel(matchNextAction(current.next_action, nextActionCatalog))
              setCallStatus('')
              setCallAppointmentDetails(emptyAppointmentDetails())
              const pos = workQueuePosition || String(currentIndex >= 0 ? currentIndex + 1 : 1)
              const total = workQueueTotal || String(result.items.length || 1)
              setQueuePositionLabel(`${pos} of ${total}`)
            } else if (workQueuePosition && workQueueTotal) {
              setQueuePositionLabel(`${workQueuePosition} of ${workQueueTotal}`)
            }
          } catch {
            if (!cancelled) setWorkQueueContext(null)
          }
        } else {
          setWorkQueueContext(null)
        }
      } catch (err) {
        if (cancelled) return
        setWorkspace(null)
        setWorkspaceActivities([])
        setSharedHistory(null)
        setWorkspaceError(
          err instanceof Error ? err.message : 'Failed to load company workspace.',
        )
      } finally {
        if (!cancelled) setWorkspaceLoading(false)
      }
    }

    void loadWorkspace(selectedRecordNo)
    return () => {
      cancelled = true
    }
  }, [selectedRecordNo, client?.client_id, client?.name, activeClientId, availableClients, selectedClientId, fromWorkQueue, workQueueClientId, workQueueItemId, workQueueType, workQueueReturn, workQueuePosition, workQueueTotal])

  useEffect(() => {
    const q = search.trim()
    if (q.length < 2) {
      setSearchResults(null)
      setSearchError(null)
      setSearchLoading(false)
      return
    }

    let cancelled = false
    const handle = window.setTimeout(() => {
      void (async () => {
        setSearchLoading(true)
        setSearchError(null)
        try {
          const data = await globalSearch({
            q,
            // Global search is a results dropdown (not a page filter).
            // Search assigned clients so Active Client = Brown can still find
            // companies like 3M that appear as Cross-Client Opportunities.
            client_id: null,
            all_clients: true,
            limit: 40,
          })
          if (cancelled) return
          setSearchResults(data)
          setSearchOpen(true)
        } catch (err) {
          if (cancelled) return
          setSearchResults(null)
          setSearchError(err instanceof Error ? err.message : 'Search failed.')
          setSearchOpen(true)
        } finally {
          if (!cancelled) setSearchLoading(false)
        }
      })()
    }, 250)

    return () => {
      cancelled = true
      window.clearTimeout(handle)
    }
  }, [search, activeClientId])

  async function reloadSharedHistory(filterClientId: number | 0) {
    if (!selectedRecordNo) return
    setSharedHistoryLoading(true)
    try {
      const shared = await fetchSharedHistory(selectedRecordNo, {
        client_id: workspaceClientId,
        filter_client_id: filterClientId === 0 ? null : filterClientId,
      })
      setSharedHistory(shared)
      setSharedHistoryFilter(filterClientId)
    } catch {
      /* keep prior shared history on filter error */
    } finally {
      setSharedHistoryLoading(false)
    }
  }

  useEffect(() => {
    if (!focusTarget || workspaceLoading || !workspace) return
    const timer = window.setTimeout(() => {
      const el =
        document.getElementById(focusTarget) ||
        document.getElementById('workspace-notes')
      if (!el) return
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setFocusFlashId(focusTarget)
      if (el instanceof HTMLElement && 'focus' in el) {
        try {
          el.focus({ preventScroll: true })
        } catch {
          /* ignore */
        }
      }
    }, 120)
    return () => window.clearTimeout(timer)
  }, [focusTarget, workspaceLoading, workspace, workspaceActivities])

  function openSearchHit(hit: SearchHit) {
    if (!hit.external_record_no) return
    // Prefer Active Client workspace so Cross-Client finds (e.g. 3M under Brown)
    // open Working For the selected client, not only the source-signal client.
    const openClientId =
      activeClientId != null && activeClientId > 0
        ? activeClientId
        : hit.client_id || null
    const href = appendWorkspaceOrigin(
      companyWorkspaceHref(hit.external_record_no, openClientId),
      { from: SEARCH_FROM },
    )
    const focus = hit.focus_target
      ? `${href.includes('?') ? '&' : '?'}focus=${encodeURIComponent(hit.focus_target)}`
      : ''
    navigate(`${href}${focus}`)
    setSearch('')
    setSearchOpen(false)
    setSearchResults(null)
    setSidebarOpen(false)
  }

  useEffect(() => {
    if (!searchOpen) return
    function onPointerDown(event: MouseEvent) {
      const target = event.target as HTMLElement | null
      if (!target) return
      if (target.closest('.search-wrap')) return
      setSearchOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [searchOpen])

  const priorityProspects = useMemo(() => priorityQueue.slice(0, 8), [priorityQueue])

  const outreachOutcomeOptions = useMemo(() => {
    const merged = [...OUTREACH_ACTIVITY_ONLY_OUTCOMES, ...statusOptions]
    return Array.from(new Set(merged.filter(Boolean)))
  }, [statusOptions])

  const workQueueCards = useMemo(
    () =>
      buildWorkQueueCards(
        companyListRows,
        activeClientId,
        needsNextActionCount,
        opportunityCount,
        dashboardFollowUps,
        appointmentSummary?.today_count ?? null,
        workQueueSummary,
      ),
    [
      companyListRows,
      activeClientId,
      needsNextActionCount,
      opportunityCount,
      dashboardFollowUps,
      appointmentSummary,
      workQueueSummary,
    ],
  )
  const kpiCards = useMemo(
    () => buildKpiCards(companyListRows, milestoneSummary, hotCount),
    [companyListRows, milestoneSummary, hotCount],
  )
  const upcomingAppointments = appointmentSummary?.upcoming ?? []

  const clientName =
    (activeClientId === 0
      ? 'All My Clients'
      : availableClients.find((c) => c.client_id === activeClientId)?.client_name) ||
    client?.name ||
    'Select client'
  const startHereMessage = dashboardStartHereMessage({
    clientName,
    newAssignments: workQueueSummary?.new_assignments ?? 0,
    callsDue: workQueueSummary?.calls_due ?? 0,
    followUpsDueToday: dashboardFollowUps?.due_today_count ?? 0,
    overdueFollowUps: dashboardFollowUps?.overdue_count ?? 0,
  })
  const workspaceClientId =
    (selectedClientId != null && selectedClientId > 0 && Number.isFinite(selectedClientId)
      ? selectedClientId
      : null) ??
    (activeClientId != null && activeClientId > 0 ? activeClientId : null)
  const workspaceClientName =
    availableClients.find((c) => c.client_id === workspaceClientId)?.client_name ||
    clientName

  const filteredSalesEvents = useMemo(() => {
    const events = workspace?.sales_events || []
    if (salesEventFilter === 'appointments') {
      return events.filter((e) => String(e.event_type || '').startsWith('Appointment'))
    }
    if (salesEventFilter === 'rfq') {
      return events.filter((e) => e.event_type === 'RFQ')
    }
    if (salesEventFilter === 'send') {
      return events.filter((e) => e.event_type === 'Send Information')
    }
    return events
  }, [workspace?.sales_events, salesEventFilter])

  function changeActiveClient(nextId: number) {
    if (!Number.isFinite(nextId) || nextId < 0) return
    // Persist first so refresh mid-navigation cannot snap back to Carmeco.
    writeStoredActiveClientId(nextId)
    setActiveClientId(nextId)
    // Drop client-specific status filter when switching Active Client.
    if (searchParams.get('status')) {
      const next = new URLSearchParams(searchParams)
      next.delete('status')
      setSearchParams(next, { replace: true })
    }
    // Optimistically reflect the selected client name before prospects return.
    const selectedName =
      nextId === 0
        ? 'All My Clients'
        : availableClients.find((c) => c.client_id === nextId)?.client_name
    if (selectedName) {
      setClient((prev) =>
        prev
          ? {
              ...prev,
              id:
                nextId === 0
                  ? 'all'
                  : availableClients.find((c) => c.client_id === nextId)?.client_code ||
                    prev.id,
              name: selectedName,
              client_id: nextId,
              code:
                nextId === 0
                  ? 'all'
                  : availableClients.find((c) => c.client_id === nextId)?.client_code ||
                    prev.code,
              mode: nextId === 0 ? 'all_my_clients' : 'selected_client',
            }
          : {
              id:
                nextId === 0
                  ? 'all'
                  : availableClients.find((c) => c.client_id === nextId)?.client_code ||
                    String(nextId),
              name: selectedName,
              seed_file: 'database/northstar.db',
              prospect_count: 0,
              client_id: nextId,
              code:
                nextId === 0
                  ? 'all'
                  : availableClients.find((c) => c.client_id === nextId)?.client_code ||
                    String(nextId),
              mode: nextId === 0 ? 'all_my_clients' : 'selected_client',
            },
      )
    }
    const rewritten = pathAfterActiveClientChange(
      location.pathname,
      location.search,
      nextId,
    )
    if (rewritten) {
      navigate(rewritten)
      return
    }
    if (activeNav === 'work-queue') {
      const params = new URLSearchParams(location.search)
      if (nextId > 0) params.set('client_id', String(nextId))
      else params.delete('client_id')
      const qs = params.toString()
      navigate(qs ? `/work-queue?${qs}` : '/work-queue')
    }
  }

  useEffect(() => {
    if (activeClientId == null || activeClientId <= 0) return
    const rewritten = pathAfterActiveClientChange(
      location.pathname,
      location.search,
      activeClientId,
      { resetCampaignWorkspace: false },
    )
    if (!rewritten) return
    const wantPath = rewritten.split('?')[0]
    const want = new URLSearchParams(rewritten.includes('?') ? rewritten.split('?')[1] : '')
    const have = new URLSearchParams(location.search)
    if (
      wantPath === location.pathname &&
      (want.get('client_id') || '') === (have.get('client_id') || '') &&
      (want.get('target_client_id') || '') === (have.get('target_client_id') || '')
    ) {
      return
    }
    navigate(rewritten, { replace: true })
  }, [activeClientId, location.pathname, location.search, navigate])

  const greeting = greetingForNow()
  const todayLabel = formatToday()
  const showContactWorkspace = Boolean(contactWorkspaceId && contactWorkspaceId > 0)
  const showCampaignWorkspace = Boolean(campaignWorkspaceId && campaignWorkspaceId > 0)
  const showCompanyWorkspace = Boolean(selectedRecordNo) && !researchMatch && !showContactWorkspace
  const showResearchCompany = Boolean(researchRecordNo)
  const showProspects =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    !showCampaignWorkspace &&
    (activeNav === 'prospects' || activeNav === 'companies')
  const showAppointments =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'appointments'
  const showActivities =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'activities'

  const showWorkQueue =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'work-queue'
  const showResearch =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'research'
  const showAskNorthStar =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'ask-northstar'
  const showContacts =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'contacts'
  const showTasks =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'tasks'
  const showReports =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'reports'
  const showAdministration =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'administration'
  const showCrossClientOpportunities =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav === 'cross-client-opportunities'
  const showClientSetupPage =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    showClientSetup
  const showDashboard =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    activeNav !== 'prospects' &&
    activeNav !== 'companies' &&
    activeNav !== 'appointments' &&
    activeNav !== 'activities' &&
    activeNav !== 'work-queue' &&
    activeNav !== 'ask-northstar' &&
    activeNav !== 'cross-client-opportunities' &&
    activeNav !== 'research' &&
    activeNav !== 'contacts' &&
    activeNav !== 'tasks' &&
    activeNav !== 'reports' &&
    activeNav !== 'clients' &&
    activeNav !== 'campaigns' &&
    activeNav !== 'client-knowledge' &&
    activeNav !== 'administration' &&
    activeNav !== 'login'
  const showCampaigns =
    !showCompanyWorkspace &&
    !showResearchCompany &&
    !showContactWorkspace &&
    !showCampaignWorkspace &&
    activeNav === 'campaigns'
  const prospectFilterMeta = prospectFilter ? PROSPECT_FILTER_META[prospectFilter] : null

  useEffect(() => {
    if (!showDashboard) return
    if (activeClientId == null || activeClientId <= 0) {
      setRecentActivities([])
      return
    }
    let cancelled = false
    void fetchClientActivities(activeClientId, { limit: 5, offset: 0 })
      .then((data) => {
        if (cancelled) return
        setRecentActivities(data.items.slice(0, 5))
      })
      .catch(() => {
        if (!cancelled) setRecentActivities([])
      })
    return () => {
      cancelled = true
    }
  }, [activeClientId, queueRefreshKey, showDashboard])

  const statusSelectOptions = useMemo(() => {
    const set = new Set(statusOptions)
    const current = asText(draftStatus)
    if (current) set.add(current)
    for (const item of APPOINTMENT_SET_STATUSES) set.add(item)
    return Array.from(set).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }))
  }, [statusOptions, draftStatus])

  const statusDirty =
    Boolean(workspace) && asText(draftStatus) !== prospectStatus(workspace || {})

  const noteDraft = newNoteText.trim()
  const noteDirty = noteDraft.length > 0

  const workspaceHistory = useMemo(
    () => buildWorkspaceHistory(workspace, workspaceActivities),
    [workspace, workspaceActivities],
  )

  function applyContactWorkflowToClientViews(update: {
    client_id: number
    company_record_no: string
    status: string
    next_action: string
    follow_up_date: string
  }) {
    const recordNo = (update.company_record_no || '').trim()
    const followDate = (update.follow_up_date || '').trim()
    const today = new Date().toISOString().slice(0, 10)
    const due = Boolean(followDate && followDate.slice(0, 10) <= today)
    const nextAction = (update.next_action || '').toLowerCase()
    setProspects((prev) =>
      prev.map((p) =>
        p.client_id === update.client_id && p.external_record_no === recordNo
          ? withRelationshipStatus(
              {
                ...p,
                next_action: update.next_action,
                follow_up_date: followDate || null,
                follow_up_due: due && nextAction.includes('follow'),
                call_due: due && (nextAction.includes('call') || nextAction.includes('outreach')),
              },
              update.status,
            )
          : p,
      ),
    )
    setQueueRefreshKey((key) => key + 1)
  }

  function applyWorkspaceUpdate(next: CompanyWorkspace) {
    setWorkspace(next)
    setDraftStatus(prospectStatus(next))
    setProspects((prev) =>
      prev.map((p) =>
        p.external_record_no === next.external_record_no &&
        (p.client_id === next.client_id || !next.client_id)
          ? withRelationshipStatus(
              { ...p, is_hot: next.is_hot },
              prospectStatus(next),
            )
          : p,
      ),
    )
  }

  async function saveStatus() {
    if (!selectedRecordNo || !statusDirty || statusSaving) return
    if (workspaceClientId == null || workspaceClientId <= 0) {
      setStatusSaveError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    setStatusSaving(true)
    setStatusSaveMsg(null)
    setStatusSaveError(null)
    try {
      const result = await updateCompanyStatus(
        selectedRecordNo,
        draftStatus,
        displayName,
        workspaceClientId,
        workspaceClientName,
      )
      if (result.workspace) {
        applyWorkspaceUpdate(result.workspace)
      } else {
        setWorkspace((prev) =>
          prev ? withRelationshipStatus(prev, result.new_value) : prev,
        )
        setProspects((prev) =>
          prev.map((p) =>
            p.external_record_no === result.external_record_no
              ? withRelationshipStatus(p, result.new_value)
              : p,
          ),
        )
        setDraftStatus(result.new_value)
      }
      setStatusSaveMsg('Saved')
      setQueueRefreshKey((key) => key + 1)
      if (
        canManageCampaigns &&
        workspace?.id &&
        workspaceClientId != null &&
        workspaceClientId > 0 &&
        isCampaignRouteStatus(result.new_value || draftStatus)
      ) {
        setCampaignRouteOffer({
          clientId: workspaceClientId,
          companyId: workspace.id,
          source: 'status',
        })
      }
    } catch (err) {
      setStatusSaveError(err instanceof Error ? err.message : 'Failed to save status.')
    } finally {
      setStatusSaving(false)
    }
  }

  function workQueueScopeClientId(): number | null {
    const queueParams = new URLSearchParams(workQueueReturn)
    const scope = queueParams.get('client_id')
    if (scope) return Number(scope)
    return null
  }

  function workQueueFetchFilters() {
    const queueParams = new URLSearchParams(workQueueReturn)
    const dueParam = queueParams.get('due') || undefined
    const hot =
      queueParams.get('hot') === '1' || queueParams.get('hot') === 'true'
    const weblead =
      queueParams.get('weblead') === '1' || queueParams.get('weblead') === 'true'
    const crossClient =
      queueParams.get('cross_client') === '1' ||
      queueParams.get('cross_client') === 'true'
    const overdue =
      queueParams.get('overdue') === '1' ||
      queueParams.get('overdue') === 'true' ||
      dueParam === 'overdue'
    return {
      client_id: workQueueScopeClientId(),
      type: queueParams.get('type') || workQueueType || undefined,
      due: dueParam,
      status: queueParams.get('status') || undefined,
      priority: queueParams.get('priority') || undefined,
      hot,
      weblead,
      cross_client: crossClient,
      overdue,
      q: queueParams.get('q') || undefined,
      ai_alignment: queueParams.get('ai_alignment') || undefined,
      ai_recommendation: queueParams.get('ai_recommendation') || undefined,
      ai_fit: queueParams.get('ai_fit') || undefined,
      ai_engagement: queueParams.get('ai_engagement') || undefined,
      limit: 50,
      offset: 0,
    }
  }

  function navigateToWorkQueueItem(next: WorkQueueRow, position: number, total: number) {
    const params = new URLSearchParams()
    params.set('from', 'work-queue')
    params.set('client_id', String(next.client_id))
    params.set('queue_item', next.queue_item_id)
    params.set('work_type', next.work_type)
    params.set('reason', next.why_in_queue || next.work_type)
    params.set('priority', String(next.work_priority))
    params.set('position', String(position))
    params.set('total', String(total))
    if (workQueueReturn) params.set('queue', workQueueReturn)
    navigate(`/companies/${encodeURIComponent(next.external_record_no)}?${params.toString()}`)
  }

  async function refreshProspectsQuietly() {
    try {
      const allClients = activeClientId === 0
      const data = await fetchProspects({
        client_id: allClients ? null : activeClientId,
        all_clients: allClients,
        q: debouncedCompanyListQuery || undefined,
        status: prospectStatusFilter || undefined,
        assigned_user_id: assignedUserIdForApi,
        limit: COMPANY_PAGE_SIZE,
        offset: companyListPage * COMPANY_PAGE_SIZE,
      })
      setProspects(data.prospects)
      setCompanyListRows(data.prospects)
      setCompanyListTotal(data.total)
      setCompanyListClientTotal(data.client_total)
      setClient(data.client)
    } catch {
      /* keep existing dashboard data */
    }
  }

  async function saveWorkQueueCall(andNext: boolean) {
    if (!fromWorkQueue || !selectedRecordNo || !workQueueContext || callSaving) return
    if (!nextActionIsValid(callNextSel, nextActionCatalog)) return
    if (isAppointmentSetStatus(callStatus) && !appointmentDetailsAreValid(callAppointmentDetails)) {
      return
    }
    const saveSeq = ++workQueueSaveSeq.current
    setCallSaving(true)
    setCallSaveMsg(null)
    setCallSaveError(null)
    try {
      const result = await logWorkQueueCall({
        client_id: workQueueContext.client_id,
        external_record_no: selectedRecordNo,
        contact_id: callContactId ? Number(callContactId) : null,
        outcome: callOutcome,
        notes: callNotes,
        status: callStatus || null,
        next_action: storedNextAction(callNextSel, nextActionCatalog),
        follow_up_date: callFollowUpDate || null,
        follow_up_time: callFollowUpTime,
        queue_source: workQueueContext.source,
        queue_source_id: workQueueContext.source_id,
        complete_current: true,
        created_by: displayName,
        appointment: isAppointmentSetStatus(callStatus)
          ? {
              appointment_date: callAppointmentDetails.appointment_date,
              start_time: callAppointmentDetails.start_time,
              timezone: callAppointmentDetails.timezone,
              datetime_tbd: callAppointmentDetails.datetime_tbd,
              appointment_type: callAppointmentDetails.appointment_type,
              location_or_link: callAppointmentDetails.location_or_link,
              revenue_specialist_user_id: callAppointmentDetails.revenue_specialist_user_id,
              notes: callAppointmentDetails.notes,
              source: callAppointmentDetails.source,
              idempotency_key: callAppointmentDetails.idempotency_key,
            }
          : undefined,
      })
      if (saveSeq !== workQueueSaveSeq.current) return
      const activities = await fetchCompanyActivities(
        selectedRecordNo,
        workQueueContext.client_name || workQueueContext.client_code || '',
        workQueueContext.client_id,
      )
      if (saveSeq !== workQueueSaveSeq.current) return
      setWorkspaceActivities(activities)
      if (callStatus) {
        setDraftStatus(callStatus)
        setWorkspace((prev) =>
          prev ? withRelationshipStatus(prev, callStatus) : prev,
        )
      }
      await refreshProspectsQuietly()
      setQueueRefreshKey((key) => key + 1)
      setCallSaveMsg(result.message || 'Call saved.')
      setCallNotes('')
      setCallOutcome('')
      setCallAppointmentDetails(emptyAppointmentDetails())

      const shouldRoute = workQueueCallShouldRoute(
        user,
        callStatus,
        Boolean(workspace?.id),
        workQueueContext.client_id,
      )
      if (shouldRoute && workspace) {
        setCampaignRouteOffer({
          clientId: workQueueContext.client_id,
          companyId: workspace.id,
          contactId: callContactId ? Number(callContactId) : null,
          source: isAppointmentSetStatus(callStatus) ? 'appointment' : 'status',
        })
      }

      if (shouldStayOnCompanyAfterCallSave(andNext, shouldRoute)) {
        // Remain on Company Workspace; refresh queue context counts/position.
        const refreshed = await fetchWorkQueue(workQueueFetchFilters())
        if (saveSeq !== workQueueSaveSeq.current) return
        const stillHere = refreshed.items.find((item) => item.queue_item_id === workQueueItemId)
        if (!stillHere) {
          setWorkQueueContext((prev) =>
            prev
              ? {
                  ...prev,
                  why_in_queue: 'Completed for this queue pass',
                  completion_status: 'completed',
                }
              : prev,
          )
        }
        return
      }

      // Save & Next: ask the server for the next eligible item across the full
      // filtered ranking (not page 0 of limit=50).
      try {
        const filters = workQueueFetchFilters()
        const {
          limit: _limit,
          offset: _offset,
          ...nextFilters
        } = filters
        const nextResult = await fetchWorkQueueNext({
          ...nextFilters,
          after_queue_item_id:
            workQueueItemId || workQueueContext.queue_item_id,
          after_work_priority: workQueueContext.work_priority,
          after_due_date: workQueueContext.due_date,
          after_due_time: workQueueContext.due_time || undefined,
          after_company_name: workQueueContext.company_name,
          after_company_id: workQueueContext.company_id,
        })
        if (saveSeq !== workQueueSaveSeq.current) return
        if (!nextResult.has_next || !nextResult.item) {
          setCallSaveMsg(
            (result.message || 'Call saved.') +
              ' ' +
              (nextResult.message || 'No further matching Work Queue items.'),
          )
          setWorkQueueContext((prev) =>
            prev
              ? {
                  ...prev,
                  why_in_queue: 'Completed for this queue pass',
                  completion_status: 'completed',
                }
              : prev,
          )
          return
        }
        navigateToWorkQueueItem(
          nextResult.item,
          nextResult.position ?? 1,
          nextResult.total || 1,
        )
      } catch (nextErr) {
        if (saveSeq !== workQueueSaveSeq.current) return
        setCallSaveError(
          nextErr instanceof Error
            ? `Call saved, but the next Work Queue item could not be loaded: ${nextErr.message}`
            : 'Call saved, but the next Work Queue item could not be loaded.',
        )
      }
    } catch (err) {
      if (saveSeq !== workQueueSaveSeq.current) return
      setCallSaveError(err instanceof Error ? err.message : 'Failed to save call.')
    } finally {
      if (saveSeq === workQueueSaveSeq.current) setCallSaving(false)
    }
  }

  async function saveOutreach(andNext: boolean) {
    if (workspaceClientId == null || workspaceClientId <= 0) {
      setOutreachError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (!selectedRecordNo || outreachSaving) {
      return
    }
    setOutreachSaving(true)
    setOutreachMsg(null)
    setOutreachError(null)
    try {
      const result = await logOutreach({
        client_id: workspaceClientId,
        external_record_no: selectedRecordNo,
        contact_id: outreachContactId ? Number(outreachContactId) : null,
        outreach_type: outreachType,
        outcome: outreachOutcome,
        notes: outreachNotes,
        next_action: storedNextAction(outreachNextSel, nextActionCatalog),
        follow_up_date: outreachFollowUpDate || null,
        return_next: andNext,
        created_by: displayName,
      })
      const activities = await fetchCompanyActivities(
        selectedRecordNo,
        workspaceClientName,
        workspaceClientId,
      )
      setWorkspaceActivities(activities)
      if (result.status_updated && result.applied_status) {
        setDraftStatus(result.applied_status)
        setWorkspace((prev) =>
          prev ? withRelationshipStatus(prev, result.applied_status) : prev,
        )
      }
      await refreshProspectsQuietly()
      setQueueRefreshKey((key) => key + 1)
      try {
        const queue = await fetchPriorityProspects(workspaceClientId, 50)
        setPriorityQueue(queue)
      } catch {
        /* keep prior queue */
      }
      const contactForEmail = outreachContactId
      setOutreachMsg(result.message || 'Outreach saved.')
      setOutreachNotes('')
      setOutreachOutcome('')
      setOutreachNextSel({ code: '', custom: '' })
      setOutreachFollowUpDate('')

      if (result.open_email_compose) {
        setSendEmailPreferredContactId(
          contactForEmail ? Number(contactForEmail) : null,
        )
        setSendEmailPreferredTemplateName('Carmeco Send Information Template')
        setSendEmailOpen(true)
        if (andNext) {
          const nextPending = result.next_external_record_no || null
          pendingNextAfterEmailRef.current = nextPending
          if (!result.next_external_record_no) {
            setOutreachMsg(
              (result.message || 'Outreach saved.') +
                ' Finish or cancel the email, then no further actionable prospects.',
            )
          } else {
            setOutreachMsg(
              (result.message || 'Outreach saved.') +
                ' Finish or cancel the email to continue to the next prospect.',
            )
          }
        }
        window.setTimeout(() => {
          const el = document.getElementById('send-email-compose')
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'start' })
            setFocusFlashId('send-email-compose')
          }
        }, 80)
      } else if (andNext) {
        const nextNo = result.next_external_record_no
        if (nextNo) {
          navigate(copyWorkspaceReturn(companyWorkspaceHref(nextNo, workspaceClientId), searchParams))
        } else {
          setOutreachMsg(
            (result.message || 'Outreach saved.') + ' No further actionable prospects.',
          )
        }
      }

      if (result.open_appointment_workflow) {
        window.setTimeout(() => {
          const el = document.getElementById('milestones-heading')
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' })
            setFocusFlashId('milestones-heading')
          }
        }, 80)
      }
    } catch (err) {
      setOutreachError(err instanceof Error ? err.message : 'Failed to save outreach.')
    } finally {
      setOutreachSaving(false)
    }
  }

  async function saveNewNote() {
    if (!selectedRecordNo || !noteDirty || noteSaving) return
    if (workspaceClientId == null || workspaceClientId <= 0) {
      setNoteSaveError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    setNoteSaving(true)
    setNoteSaveMsg(null)
    setNoteSaveError(null)
    try {
      const created = await createCompanyNote({
        external_record_no: selectedRecordNo,
        notes: noteDraft,
        created_by: displayName,
        client: workspaceClientName,
        client_id: workspaceClientId,
      })
      setWorkspaceActivities((prev) => [created, ...prev.filter((a) => a.activity_id !== created.activity_id)])
      setNewNoteText('')
      setNoteSaveMsg('Note added')
      setFocusFlashId(`activity-${created.activity_id}`)
      setQueueRefreshKey((key) => key + 1)
    } catch (err) {
      setNoteSaveError(err instanceof Error ? err.message : 'Failed to add note.')
    } finally {
      setNoteSaving(false)
    }
  }

  function clearNewNote() {
    setNewNoteText('')
    setNoteSaveMsg(null)
    setNoteSaveError(null)
  }

  function clearMilestoneForm() {
    setMilestoneDate('')
    setMilestoneReference('')
    setMilestoneAmount('')
    setMilestoneSource('')
    setMilestoneContactId('')
    setMilestoneNotes('')
  }

  async function saveMilestone() {
    if (!selectedRecordNo || !milestoneDate || milestoneSaving) return
    if (workspaceClientId == null || workspaceClientId <= 0) {
      setMilestoneSaveError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    if (milestoneType === 'WebLead' && workspace?.contacts.length && !milestoneContactId) {
      setMilestoneSaveError('Select a contact for this WebLead.')
      return
    }
    setMilestoneSaving(true)
    setMilestoneSaveMsg(null)
    setMilestoneSaveError(null)
    try {
      await createMilestone({
        client: workspaceClientName,
        client_id: workspaceClientId,
        external_record_no: selectedRecordNo,
        milestone_type: milestoneType,
        milestone_date: milestoneDate,
        contact_id: milestoneContactId ? Number(milestoneContactId) : null,
        amount: milestoneAmount ? Number(milestoneAmount) : null,
        reference_number: milestoneReference || undefined,
        source: milestoneSource || undefined,
        notes: milestoneNotes || undefined,
        created_by: displayName,
      })
      const [nextWorkspace, activities] = await Promise.all([
        fetchCompanyByRecordNo(selectedRecordNo, workspaceClientId),
        fetchCompanyActivities(selectedRecordNo, workspaceClientName, workspaceClientId),
      ])
      applyWorkspaceUpdate(nextWorkspace)
      setWorkspaceActivities(activities)
      setProspects((prev) =>
        prev.map((prospect) => {
          if (prospect.external_record_no !== selectedRecordNo) return prospect
          if (milestoneType === 'Quote') return { ...prospect, has_quote: true }
          if (milestoneType === 'Purchase Order') return { ...prospect, has_purchase_order: true }
          if (milestoneType === 'WebLead') return { ...prospect, has_weblead: true }
          if (milestoneType === 'Appointment Set') return { ...prospect, has_appointment_set: true }
          return prospect
        }),
      )
      clearMilestoneForm()
      setMilestoneSaveMsg(`${milestoneType} added`)
      if (
        canManageCampaigns &&
        milestoneType === 'Appointment Set' &&
        workspace?.id &&
        workspaceClientId != null &&
        workspaceClientId > 0
      ) {
        setCampaignRouteOffer({
          clientId: workspaceClientId,
          companyId: workspace.id,
          contactId: milestoneContactId ? Number(milestoneContactId) : null,
          source: 'appointment',
        })
      }
    } catch (err) {
      setMilestoneSaveError(err instanceof Error ? err.message : 'Failed to add milestone.')
    } finally {
      setMilestoneSaving(false)
    }
  }

  async function toggleHot() {
    if (!selectedRecordNo || !workspace || hotSaving) return
    if (workspaceClientId == null || workspaceClientId <= 0) {
      setMilestoneSaveError(SELECT_CLIENT_FOR_WRITE)
      return
    }
    setHotSaving(true)
    setMilestoneSaveMsg(null)
    setMilestoneSaveError(null)
    try {
      const next = await setCompanyHot({
        client: workspaceClientName,
        client_id: workspaceClientId,
        external_record_no: selectedRecordNo,
        is_hot: !workspace.is_hot,
        created_by: displayName,
      })
      if (next) {
        applyWorkspaceUpdate(next)
      } else {
        const refreshed = await fetchCompanyByRecordNo(selectedRecordNo, workspaceClientId)
        applyWorkspaceUpdate(refreshed)
      }
      setMilestoneSaveMsg(workspace.is_hot ? 'Hot flag removed' : 'Marked hot')
      if (
        canManageCampaigns &&
        !workspace.is_hot &&
        workspace.id &&
        workspaceClientId != null &&
        workspaceClientId > 0
      ) {
        setCampaignRouteOffer({
          clientId: workspaceClientId,
          companyId: workspace.id,
          source: 'hot',
        })
      }
    } catch (err) {
      setMilestoneSaveError(err instanceof Error ? err.message : 'Failed to update hot flag.')
    } finally {
      setHotSaving(false)
    }
  }

  function closeCompanyWorkspace() {
    setAddContactOpen(false)
    navigate(workspaceReturnPath(searchParams))
  }

  function workspaceContactHref(contactId: number) {
    const params = new URLSearchParams()
    if (workspaceClientId) params.set('client_id', String(workspaceClientId))
    for (const key of ['from', 'queue', 'list'] as const) {
      const value = searchParams.get(key)
      if (value) params.set(key, value)
    }
    const q = params.toString()
    const base = `/contacts/${contactId}${q ? `?${q}` : ''}`
    return fromAskNorthStar ? withAskReturnParam(base) : base
  }

  function goToNav(navId: string) {
    navigate(pathForNav(navId, activeClientId))
    setSidebarOpen(false)
  }

  function goToSignIn() {
    navigate('/login')
    setSidebarOpen(false)
  }

  async function onSignOut() {
    try {
      await logout()
    } catch {
      /* Session is cleared locally even if the request fails. */
    }
    if (authEnforced) {
      navigate('/login')
    }
  }

  return (
    <div className={`dashboard ${sidebarCollapsed ? 'dashboard--collapsed' : ''}`}>
      {sidebarOpen && (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="Close menu"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={`sidebar ${sidebarOpen ? 'sidebar--open' : ''} ${
          sidebarCollapsed ? 'sidebar--collapsed' : ''
        }`}
      >
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            NS
          </span>
          {!sidebarCollapsed && (
            <div className="brand-text">
              <strong>NorthStar Group</strong>
              <span>NorthStar AI</span>
              {showNorthStarPilotIndicator() ? (
                <span className="pilot-indicator">{northStarPilotLabel()}</span>
              ) : null}
            </div>
          )}
          <button
            type="button"
            className="sidebar-collapse"
            aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-pressed={sidebarCollapsed}
            onClick={() => setSidebarCollapsed((v) => !v)}
          >
            {sidebarCollapsed ? '»' : '«'}
          </button>
        </div>

        {!sidebarCollapsed && (
          <div className="client-badge" aria-label="Active client">
            <span className="client-badge__label">Active client</span>
            {singleAssignedClient ? (
              <strong className="client-badge__name">{availableClients[0].client_name}</strong>
            ) : (
              <label className="client-badge__select-label" htmlFor="active-client-select">
                <span className="sr-only">Active client</span>
                <select
                  id="active-client-select"
                  className="client-badge__select"
                  value={
                    activeClientId == null
                      ? ''
                      : activeClientId === 0
                        ? 'all'
                        : String(activeClientId)
                  }
                  onChange={(e) => {
                    const v = e.target.value
                    if (v === '') return
                    if (v === 'all') {
                      changeActiveClient(0)
                      return
                    }
                    const nextId = Number(v)
                    if (!Number.isFinite(nextId) || nextId <= 0) return
                    changeActiveClient(nextId)
                  }}
                >
                  {availableClients.length === 0 && (
                    <option value="" disabled>
                      Loading clients…
                    </option>
                  )}
                  {availableClients.map((c) => (
                    <option key={c.client_id} value={String(c.client_id)}>
                      {c.client_name}
                    </option>
                  ))}
                  {availableClients.length > 1 && <option value="all">All My Clients</option>}
                </select>
              </label>
            )}
          </div>
        )}

        <nav className="sidebar-nav" aria-label="Primary">
          {visibleNavItems.map((item) => (
            <button
              key={item.id}
              type="button"
              title={item.label}
              className={`nav-item ${
                !showCompanyWorkspace && activeNav === item.id ? 'nav-item--active' : ''
              }`}
              onClick={() => goToNav(item.id)}
            >
              <span className="nav-icon" aria-hidden="true">
                {item.icon}
              </span>
              {!sidebarCollapsed && <span className="nav-label">{item.label}</span>}
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="user-chip">
            <span className="user-avatar" aria-hidden="true">
              {displayInitials}
            </span>
            {!sidebarCollapsed && (
              <div>
                <strong>{displayName}</strong>
                <span>{roleLabel}</span>
              </div>
            )}
          </div>
          {authenticated ? (
            <button type="button" className="link-btn user-chip-action" onClick={() => void onSignOut()}>
              Sign out
            </button>
          ) : authAvailable ? (
            <button type="button" className="link-btn user-chip-action" onClick={goToSignIn}>
              Sign in
            </button>
          ) : null}
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            type="button"
            className="menu-toggle"
            aria-label="Open menu"
            onClick={() => setSidebarOpen(true)}
          >
            ☰
          </button>

          <div className="search-wrap">
            <span className="search-icon" aria-hidden="true">
              ⌕
            </span>
            <input
              type="search"
              className="search-input"
              placeholder="Search companies, contacts, notes…"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setSearchOpen(true)
              }}
              onFocus={() => {
                if (search.trim().length >= 2) setSearchOpen(true)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Escape') setSearchOpen(false)
              }}
              aria-label="Global search"
              aria-expanded={searchOpen}
              aria-controls="global-search-results"
            />
            {searchOpen && search.trim().length >= 2 && (
              <div
                id="global-search-results"
                className="search-results"
                role="listbox"
                aria-label="Search results"
              >
                {searchLoading && <p className="search-results__status">Searching…</p>}
                {searchError && (
                  <p className="search-results__status search-results__status--error">
                    {searchError}
                  </p>
                )}
                {!searchLoading && !searchError && searchResults && searchResults.total === 0 && (
                  <p className="search-results__status">No matches for “{search.trim()}”.</p>
                )}
                {!searchLoading && searchResults && searchResults.total > 0 && (
                  <p className="search-results__status">
                    Search results
                    {activeClientId != null && activeClientId > 0
                      ? ` · open under ${
                          availableClients.find((c) => c.client_id === activeClientId)
                            ?.client_name || 'Active Client'
                        }`
                      : ' · across assigned clients'}
                  </p>
                )}
                {!searchLoading && searchResults && searchResults.companies.length > 0 && (
                  <div className="search-group">
                    <h3>Companies</h3>
                    {searchResults.companies.map((hit) => (
                      <button
                        key={`co-${hit.client_id}-${hit.company_id}`}
                        type="button"
                        className="search-hit"
                        onClick={() => openSearchHit(hit)}
                      >
                        <strong>{hit.company_name || hit.title}</strong>
                        <span>
                          <span className="client-badge-pill">{hit.client_name}</span>
                          {' · Record No. '}
                          {hit.external_record_no || '—'}
                        </span>
                        <span className="search-hit__meta">Company</span>
                        <span
                          className="search-hit__snippet"
                          dangerouslySetInnerHTML={{ __html: hit.snippet || hit.title }}
                        />
                      </button>
                    ))}
                  </div>
                )}
                {!searchLoading && searchResults && searchResults.contacts.length > 0 && (
                  <div className="search-group">
                    <h3>Contacts</h3>
                    {searchResults.contacts.map((hit) => (
                      <button
                        key={`ct-${hit.client_id}-${hit.contact_id}`}
                        type="button"
                        className="search-hit"
                        onClick={() => openSearchHit(hit)}
                      >
                        <strong>{hit.contact_name || hit.title}</strong>
                        <span>
                          {hit.company_name || '—'} ·{' '}
                          <span className="client-badge-pill">{hit.client_name}</span>
                          {hit.external_record_no
                            ? ` · Record No. ${hit.external_record_no}`
                            : ''}
                        </span>
                        <span className="search-hit__meta">Contact</span>
                        <span
                          className="search-hit__snippet"
                          dangerouslySetInnerHTML={{ __html: hit.snippet || hit.title }}
                        />
                      </button>
                    ))}
                  </div>
                )}
                {!searchLoading && searchResults && searchResults.notes.length > 0 && (
                  <div className="search-group">
                    <h3>Notes / Activities</h3>
                    {searchResults.notes.map((hit) => (
                      <button
                        key={`nt-${hit.source_table}-${hit.source_id}`}
                        type="button"
                        className="search-hit"
                        onClick={() => openSearchHit(hit)}
                      >
                        <strong>{hit.company_name || 'Company'}</strong>
                        <span>
                          <span className="client-badge-pill">{hit.client_name}</span>
                          {' · Record No. '}
                          {hit.external_record_no || '—'}
                          {hit.contact_name ? ` · ${hit.contact_name}` : ''}
                        </span>
                        <span className="search-hit__meta">
                          {hit.note_type || hit.title || 'Note'}
                          {hit.event_at ? ` · ${hit.event_at}` : ''}
                          {hit.created_by ? ` · ${hit.created_by}` : ''}
                        </span>
                        <span
                          className="search-hit__snippet"
                          dangerouslySetInnerHTML={{ __html: hit.snippet }}
                        />
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="topbar-actions">
            <FeedbackButton
              clientId={
                workspaceClientId != null && workspaceClientId > 0 ? workspaceClientId : null
              }
              companyId={
                showCompanyWorkspace && workspace?.id
                  ? workspace.id
                  : showContactWorkspace
                    ? contactFeedbackContext.companyId
                    : null
              }
              contactId={
                showContactWorkspace && contactFeedbackContext.companyId
                  ? contactFeedbackContext.contactId
                  : null
              }
            />
            <button type="button" className="icon-btn" aria-label="Notifications" title="Notifications">
              <span className="icon-bell" aria-hidden="true" />
            </button>
            <button type="button" className="icon-btn" aria-label="Help" title="Help">
              <span aria-hidden="true">?</span>
            </button>
            <div className="topbar-user" title={displayName}>
              <span className="user-avatar user-avatar--sm" aria-hidden="true">
                {displayInitials}
              </span>
              <div className="topbar-user__text">
                <strong>{displayName}</strong>
                <span>{clientName}</span>
              </div>
              {authenticated ? (
                <button type="button" className="link-btn" onClick={() => void onSignOut()}>
                  Sign out
                </button>
              ) : authAvailable ? (
                <button type="button" className="link-btn" onClick={goToSignIn}>
                  Sign in
                </button>
              ) : null}
            </div>
          </div>
        </header>

        <main className="content">
          <CampaignRoutePrompt
            offer={campaignRouteOffer}
            onClose={() => setCampaignRouteOffer(null)}
          />
          {loading &&
            activeNav !== 'client-knowledge' &&
            !showResearchCompany &&
            !showResearch &&
            !showContacts &&
            !showTasks &&
            !showReports &&
            !showAdministration &&
            !showAppointments &&
            !showContactWorkspace &&
            !showCompanyWorkspace && (
            <p className="data-status">Loading {clientName} prospects…</p>
          )}
          {error &&
            activeNav !== 'client-knowledge' &&
            !showResearchCompany &&
            !showResearch &&
            !showContacts &&
            !showTasks &&
            !showReports &&
            !showAdministration &&
            !showAppointments &&
            !showContactWorkspace &&
            !showCompanyWorkspace && (
            <p className="data-status data-status--error" role="alert">
              Could not load client data. Start the backend API, then refresh. ({error})
            </p>
          )}

          {showContactWorkspace && contactWorkspaceId && (
            <ContactWorkspacePage
              contactId={contactWorkspaceId}
              activeClientId={activeClientId}
              activeClientName={clientName}
              onWorkflowSaved={applyContactWorkflowToClientViews}
              onFeedbackContext={reportContactFeedbackContext}
            />
          )}

          {!error && showCompanyWorkspace && (
            <>
              {workspace ? (
                <AddContactModal
                  open={addContactOpen}
                  onClose={() => setAddContactOpen(false)}
                  clientId={workspaceClientId}
                  clientName={workspaceClientName}
                  lockedCompanyId={workspace.id}
                  lockedCompanyName={workspace.company_name}
                />
              ) : null}
              <div className="page-heading page-heading--split">
                <div>
                  <button type="button" className="link-btn back-link" onClick={closeCompanyWorkspace}>
                    {workspaceReturnLabel(searchParams)}
                  </button>
                  <h1>{workspace?.company_name || 'Company Workspace'}</h1>
                  <p className="workspace-working-for">
                    <strong>Working For:</strong> {workspaceClientName}
                  </p>
                  {workspaceClientId == null || workspaceClientId <= 0 ? (
                    <p className="data-status" role="status">
                      {SELECT_CLIENT_FOR_WRITE}
                    </p>
                  ) : null}
                  {(location.state as { companySaveNotice?: string } | null)?.companySaveNotice ? (
                    <p className="save-confirm" role="status">
                      {(location.state as { companySaveNotice?: string }).companySaveNotice}
                    </p>
                  ) : null}
                  <p>
                    Record No.: {workspace?.external_record_no || '—'}
                    {' · '}
                    Status:{' '}
                    {displayOrDash(
                      prospectStatus(workspace || {}) || draftStatus,
                    )}
                  </p>
                </div>
                <div className="heading-controls">
                  {workspace?.external_record_no &&
                  workspaceClientId != null &&
                  workspaceClientId > 0 ? (
                    <>
                      <button
                        type="button"
                        className="primary-btn"
                        onClick={() => {
                          setSendEmailPreferredContactId(null)
                          setSendEmailPreferredTemplateName(null)
                          pendingNextAfterEmailRef.current = null
                          setSendEmailOpen(true)
                        }}
                      >
                        Send Email
                      </button>
                      <Link
                        className="primary-btn"
                        to={`/companies/${encodeURIComponent(workspace.external_record_no)}/research?client_id=${workspaceClientId}`}
                      >
                        Research This Company
                      </Link>
                      {canManageCampaigns && workspace.id ? (
                        <button
                          type="button"
                          className="ghost-btn"
                          onClick={() =>
                            setCampaignRouteOffer({
                              clientId: workspaceClientId,
                              companyId: workspace.id,
                              source: 'manual',
                              mode: 'manual',
                            })
                          }
                        >
                          Add to Campaign
                        </button>
                      ) : null}
                    </>
                  ) : workspace?.external_record_no ? (
                    <p className="queue-sub" style={{ margin: 0, maxWidth: '14rem' }}>
                      Select a Working For client to research this company.
                    </p>
                  ) : null}
                </div>
              </div>

              {sendEmailOpen &&
              workspace &&
              workspaceClientId != null &&
              workspaceClientId > 0 ? (
                <SendEmailCompose
                  open={sendEmailOpen}
                  onClose={() => {
                    setSendEmailOpen(false)
                    setSendEmailPreferredContactId(null)
                    setSendEmailPreferredTemplateName(null)
                    const nextNo = pendingNextAfterEmailRef.current
                    pendingNextAfterEmailRef.current = null
                    if (nextNo && workspaceClientId > 0) {
                      navigate(copyWorkspaceReturn(companyWorkspaceHref(nextNo, workspaceClientId), searchParams))
                    }
                  }}
                  clientId={workspaceClientId}
                  clientName={workspaceClientName}
                  companyId={workspace.id}
                  companyName={workspace.company_name}
                  externalRecordNo={workspace.external_record_no}
                  preferredContactId={sendEmailPreferredContactId}
                  preferredTemplateName={sendEmailPreferredTemplateName}
                  allowContactsWithoutEmail
                  contacts={(workspace.contacts || []).map((c) => ({
                    contact_id: c.id,
                    contact_name: `${c.first_name || ''} ${c.last_name || ''}`.trim() || 'Contact',
                    email: c.email || '',
                    title: c.title || '',
                  }))}
                />
              ) : null}

              {workspaceLoading && <p className="data-status">Loading company workspace…</p>}
              {workspaceError && (
                <p className="data-status data-status--error" role="alert">
                  {workspaceError}
                </p>
              )}

              {!workspaceLoading && workspace && (
                <div className="workspace-grid">
                  <div className="workspace-section-banner panel--wide" role="presentation">
                    <strong>Working For: {workspaceClientName}</strong>
                    <span>
                      Record No. {displayOrDash(workspace.external_record_no)} · Status:{' '}
                      {displayOrDash(prospectStatus(workspace))} — new
                      notes, calls, follow-ups, milestones, Hot, and status changes save to this
                      client only.
                    </span>
                  </div>

                  {workspace.recommendation?.action ? (
                    <section
                      className="panel panel--wide"
                      aria-labelledby="northstar-recommendation-heading"
                    >
                      <div className="panel-header">
                        <h2 id="northstar-recommendation-heading">NorthStar Recommendation</h2>
                        <span className="queue-source">Advisory only</span>
                      </div>
                      <p>
                        <strong>{workspace.recommendation.action}</strong>
                      </p>
                      <p className="queue-sub" style={{ marginTop: '0.35rem' }}>
                        {displayOrDash(workspace.recommendation.why)}
                      </p>
                      {workspace.recommendation.evidence_used?.length ? (
                        <div className="signal-history" style={{ marginTop: '0.5rem' }}>
                          {workspace.recommendation.evidence_used.slice(0, 5).map((s) => (
                            <span key={s} className="opportunity-badge">
                              {s}
                            </span>
                          ))}
                        </div>
                      ) : null}
                      <div className="heading-controls" style={{ marginTop: '0.75rem', gap: '0.75rem' }}>
                        {workspace.external_record_no &&
                        workspaceClientId != null &&
                        workspaceClientId > 0 ? (
                          <Link
                            className="link-btn"
                            to={`/companies/${encodeURIComponent(workspace.external_record_no)}/research?client_id=${workspaceClientId}`}
                          >
                            Research This Company
                          </Link>
                        ) : null}
                        <Link className="link-btn" to="/ask-northstar">
                          Ask NorthStar Call Brief
                        </Link>
                      </div>
                    </section>
                  ) : null}

                  <section className="panel" aria-labelledby="company-info-heading">
                    <div className="panel-header">
                      <h2 id="company-info-heading">Company Information</h2>
                    </div>
                    <dl className="info-list">
                      <InfoRow label="Client" value={workspaceClientName} />
                      <InfoRow label="Company" value={workspace.company_name} />
                      <InfoRow label="Record No." value={workspace.external_record_no} />
                      <InfoRow label="Address" value={workspace.address} />
                      <InfoRow label="City" value={workspace.city} />
                      <InfoRow label="State" value={workspace.state} />
                      <InfoRow label="Zip" value={workspace.zip} />
                      <InfoRow label="Website" value={workspace.website} />
                      <InfoRow label="Phone" value={workspace.legacy_phone} />
                      <InfoRow
                        label="Last Updated"
                        value={formatDisplayDateTime(workspace.last_updated_at)}
                      />
                      <InfoRow label="Sales Volume Range" value={workspace.sales_volume_range} />
                      <InfoRow
                        label="Location Sales Volume"
                        value={workspace.location_sales_volume_range}
                      />
                      <InfoRow label="Employee Size Range" value={workspace.employee_size_range} />
                      <InfoRow
                        label="Primary SIC"
                        value={[workspace.primary_sic_code, workspace.primary_sic_description]
                          .filter(Boolean)
                          .join(' — ')}
                      />
                      <InfoRow
                        label="Primary NAICS"
                        value={[workspace.primary_naics_code, workspace.primary_naics_description]
                          .filter(Boolean)
                          .join(' — ')}
                      />
                      <InfoRow
                        label="8 Digit SIC"
                        value={[workspace.sic_8_digit, workspace.sic_8_digit_description]
                          .filter(Boolean)
                          .join(' — ')}
                      />
                      <InfoRow label="Type of Industry" value={workspace.type_of_industry} />
                      <InfoRow label="Customer Campaign" value={workspace.customer_campaign} />
                      <InfoRow
                        label="Entered"
                        value={formatDisplayDateTime(workspace.entered_at)}
                      />
                    </dl>
                  </section>

                  <section className="panel" aria-labelledby="client-status-heading">
                    <div className="panel-header">
                      <h2 id="client-status-heading">Status</h2>
                    </div>
                    <div className="edit-field">
                      <label className="edit-field__label" htmlFor="client-status-select">
                        Status
                      </label>
                      <select
                        id="client-status-select"
                        className="edit-select"
                        value={draftStatus}
                        onChange={(e) => {
                          setDraftStatus(e.target.value)
                          setStatusSaveMsg(null)
                          setStatusSaveError(null)
                        }}
                      >
                        {!draftStatus && <option value="">Select status…</option>}
                        {statusSelectOptions.map((status) => (
                          <option key={status} value={status}>
                            {status}
                          </option>
                        ))}
                      </select>
                      <div className="edit-actions">
                        <button
                          type="button"
                          className="primary-btn"
                          disabled={!statusDirty || statusSaving || !draftStatus}
                          onClick={() => void saveStatus()}
                        >
                          {statusSaving ? 'Saving…' : 'Save'}
                        </button>
                        {statusSaveMsg && (
                          <span className="save-confirm" role="status">
                            {statusSaveMsg}
                          </span>
                        )}
                      </div>
                      {statusSaveError && (
                        <p className="data-status data-status--error" role="alert">
                          {statusSaveError}
                        </p>
                      )}
                    </div>
                    <p className="muted-note">
                      Client-specific status for {workspaceClientName} — not a universal company
                      field.
                    </p>
                  </section>

                  {fromWorkQueue && (
                    <section className="panel work-queue-context" aria-labelledby="wq-context-heading">
                      <div className="panel-header">
                        <h2 id="wq-context-heading">WORK QUEUE</h2>
                        <Link
                          className="link-btn"
                          to={workQueueReturn ? `/work-queue?${workQueueReturn}` : '/work-queue'}
                        >
                          Back to Work Queue
                        </Link>
                      </div>
                      <dl className="info-list work-queue-banner">
                        <div className="info-row">
                          <dt>Reason</dt>
                          <dd>
                            {displayOrDash(
                              workQueueReason ||
                                workQueueContext?.why_in_queue ||
                                workQueueType ||
                                'Queue item',
                            )}
                          </dd>
                        </div>
                        <div className="info-row">
                          <dt>Priority</dt>
                          <dd>
                            {displayOrDash(
                              workQueuePriority ||
                                (workQueueContext ? String(workQueueContext.work_priority) : ''),
                            )}
                          </dd>
                        </div>
                        <div className="info-row">
                          <dt>Client</dt>
                          <dd>
                            <strong>
                              {displayOrDash(workQueueContext?.client_name || workspaceClientName || clientName)}
                            </strong>
                          </dd>
                        </div>
                        <div className="info-row">
                          <dt>Position</dt>
                          <dd>{displayOrDash(queuePositionLabel || (workQueuePosition && workQueueTotal ? `${workQueuePosition} of ${workQueueTotal}` : ''))}</dd>
                        </div>
                      </dl>

                      {workQueueType === 'New Assignment' && (
                        <p className="muted-note">
                          Start Working opened this New Assignment. No Call activity is created until you log an
                          actual action below.
                        </p>
                      )}

                      <div
                        id="log-call"
                        className={`add-note-card work-queue-log-call${focusTarget === 'log-call' ? ' focus-flash' : ''}`}
                      >
                        <h3 className="add-note-card__title">Log Call</h3>
                        <div className="milestone-form-grid">
                          <label className="edit-field">
                            <span className="edit-field__label">Contact</span>
                            <select
                              className="edit-select"
                              value={callContactId}
                              onChange={(e) => setCallContactId(e.target.value)}
                            >
                              <option value="">No contact selected</option>
                              {(workspace?.contacts ?? []).map((contact) => (
                                <option key={contact.id} value={contact.id}>
                                  {`${asText(contact.first_name)} ${asText(contact.last_name)}`.trim() ||
                                    `Contact #${contact.id}`}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label className="edit-field">
                            <span className="edit-field__label">Outcome</span>
                            <select
                              className="edit-select"
                              value={callOutcome}
                              onChange={(e) => setCallOutcome(e.target.value)}
                            >
                              <option value="">Select outcome…</option>
                              <option value="Connected">Connected</option>
                              <option value="Left Message">Left Message</option>
                              <option value="No Answer">No Answer</option>
                              <option value="Busy">Busy</option>
                              <option value="Wrong Number">Wrong Number</option>
                              <option value="Not Interested">Not Interested</option>
                            </select>
                          </label>
                          <label className="edit-field">
                            <span className="edit-field__label">Optional Status Change</span>
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
                                  setCallAppointmentDetails((current) => ({
                                    ...current,
                                    source: appointmentSourceForStatus(nextStatus),
                                    revenue_specialist_user_id:
                                      current.revenue_specialist_user_id ??
                                      workQueueContext?.assigned_user_id ??
                                      null,
                                  }))
                                }
                              }}
                            >
                              <option value="">Keep current status</option>
                              {statusSelectOptions.map((status) => (
                                <option key={status} value={status}>
                                  {status}
                                </option>
                              ))}
                            </select>
                          </label>
                          <NextActionFields
                            catalog={nextActionCatalog}
                            value={callNextSel}
                            onChange={setCallNextSel}
                            idPrefix="wq-call"
                          />
                          <label className="edit-field">
                            <span className="edit-field__label">Follow-Up Date</span>
                            <input
                              className="edit-input"
                              type="date"
                              value={callFollowUpDate}
                              onChange={(e) => setCallFollowUpDate(e.target.value)}
                            />
                          </label>
                          <label className="edit-field">
                            <span className="edit-field__label">Follow-Up Time</span>
                            <input
                              className="edit-input"
                              type="time"
                              value={callFollowUpTime}
                              onChange={(e) => setCallFollowUpTime(e.target.value)}
                            />
                          </label>
                        </div>
                        <label className="edit-field__label" htmlFor="wq-call-notes">
                          Notes
                        </label>
                        <textarea
                          id="wq-call-notes"
                          className="edit-textarea add-note-card__textarea"
                          rows={4}
                          value={callNotes}
                          onChange={(e) => setCallNotes(e.target.value)}
                          placeholder="Call notes…"
                        />
                        {isAppointmentSetStatus(callStatus) ? (
                          <AppointmentDetailsFields
                            value={callAppointmentDetails}
                            onChange={setCallAppointmentDetails}
                            reps={
                              workQueueContext?.assigned_user_id
                                ? [
                                    {
                                      user_id: workQueueContext.assigned_user_id,
                                      full_name:
                                        workQueueContext.assigned_user || WORKSPACE_USER,
                                    },
                                  ]
                                : [{ user_id: 1, full_name: WORKSPACE_USER }]
                            }
                            companyName={asText(workspace?.company_name)}
                            contactName={
                              (() => {
                                const selected = (workspace?.contacts ?? []).find(
                                  (contact) => String(contact.id) === callContactId,
                                )
                                if (!selected) {
                                  return asText(workQueueContext?.contact_name)
                                }
                                return (
                                  `${asText(selected.first_name)} ${asText(selected.last_name)}`.trim() ||
                                  `Contact #${selected.id}`
                                )
                              })()
                            }
                            idPrefix="wq-appt"
                          />
                        ) : null}
                        <div className="edit-actions">
                          <button
                            type="button"
                            className="primary-btn"
                            disabled={
                              callSaving ||
                              !workQueueContext ||
                              !nextActionIsValid(callNextSel, nextActionCatalog) ||
                              (isAppointmentSetStatus(callStatus) &&
                                !appointmentDetailsAreValid(callAppointmentDetails))
                            }
                            onClick={() => void saveWorkQueueCall(false)}
                          >
                            {callSaving ? 'Saving…' : 'Save Call'}
                          </button>
                          <button
                            type="button"
                            className="primary-btn"
                            disabled={
                              callSaving ||
                              !workQueueContext ||
                              !nextActionIsValid(callNextSel, nextActionCatalog) ||
                              (isAppointmentSetStatus(callStatus) &&
                                !appointmentDetailsAreValid(callAppointmentDetails))
                            }
                            onClick={() => void saveWorkQueueCall(true)}
                          >
                            {callSaving ? 'Saving…' : 'SAVE & NEXT'}
                          </button>
                        </div>
                        {callSaveMsg && (
                          <span className="save-confirm" role="status">
                            {callSaveMsg}
                          </span>
                        )}
                        {callSaveError && (
                          <p className="data-status data-status--error" role="alert">
                            {callSaveError}
                          </p>
                        )}
                      </div>
                    </section>
                  )}

                  {crossClientBanner?.applicable && (
                    <section className="panel cross-client-banner" aria-labelledby="cross-client-banner-heading">
                      <div className="panel-header">
                        <h2 id="cross-client-banner-heading">Cross-Client Opportunity</h2>
                      </div>
                      <p className="cross-client-banner__target">
                        Target Client: <strong>{crossClientBanner.target_client_name}</strong>
                      </p>
                      <div className="cross-client-banner__signals">
                        <p className="muted-note">Evidence Elsewhere:</p>
                        {crossClientBanner.evidence.map((signal, index) => (
                          <span key={`${signal.client_name}-${signal.milestone_date}-${index}`}>
                            {signal.client_name} — {signal.milestone_type}
                            {signal.milestone_date ? ` · ${signal.milestone_date}` : ''}
                          </span>
                        ))}
                      </div>
                      <p className="muted-note">
                        {crossClientBanner.target_client_name}:{' '}
                        {displayOrDash(crossClientBanner.target_client_activity)}
                      </p>
                    </section>
                  )}

                  <section className="panel panel--wide" aria-labelledby="milestones-heading">
                    <div className="panel-header">
                      <h2 id="milestones-heading">Revenue Milestones</h2>
                      <span className="queue-source">{workspace.milestones.length} recorded</span>
                    </div>
                    <div className="milestone-badges" aria-label="Current revenue milestones">
                      {(['Appointment Set', 'Quote', 'Purchase Order', 'WebLead'] as RevenueMilestoneType[])
                        .filter((type) => workspace.milestones.some((milestone) => milestone.milestone_type === type))
                        .map((type) => (
                          <span key={type} className="milestone-badge">
                            {type === 'Appointment Set' ? 'Appointment' : type}
                          </span>
                        ))}
                      {(workspace.is_hot ||
                        workspace.milestones.some((milestone) => milestone.milestone_type === 'Hot')) && (
                        <span className="milestone-badge milestone-badge--hot">Hot</span>
                      )}
                      {!workspace.is_hot && workspace.milestones.length === 0 && (
                        <span className="muted-note">No revenue milestones yet.</span>
                      )}
                    </div>
                    <label className="hot-toggle">
                      <input
                        type="checkbox"
                        checked={workspace.is_hot}
                        disabled={hotSaving}
                        onChange={() => void toggleHot()}
                      />
                      <span>{hotSaving ? 'Updating hot flag…' : 'Mark this company hot'}</span>
                    </label>
                    <p className="muted-note">
                      {workspaceClientName} relationship status remains{' '}
                      {displayOrDash(prospectStatus(workspace))}.
                    </p>

                    <div className="milestone-form-card">
                      <h3 className="add-note-card__title">Add Revenue Milestone</h3>
                      <div className="milestone-form-grid">
                        <label className="edit-field">
                          <span className="edit-field__label">Type</span>
                          <select
                            className="edit-select"
                            value={milestoneType}
                            onChange={(e) => {
                              setMilestoneType(e.target.value as RevenueMilestoneType)
                              setMilestoneSaveError(null)
                            }}
                          >
                            <option value="Quote">Quote</option>
                            <option value="Purchase Order">Purchase Order</option>
                            <option value="WebLead">WebLead</option>
                          </select>
                        </label>
                        <label className="edit-field">
                          <span className="edit-field__label">Date</span>
                          <input
                            className="edit-input"
                            type="date"
                            value={milestoneDate}
                            onChange={(e) => setMilestoneDate(e.target.value)}
                          />
                        </label>
                        {milestoneType !== 'WebLead' ? (
                          <label className="edit-field">
                            <span className="edit-field__label">
                              {milestoneType === 'Quote' ? 'Quote Number' : 'PO Number'}
                            </span>
                            <input
                              className="edit-input"
                              value={milestoneReference}
                              onChange={(e) => setMilestoneReference(e.target.value)}
                            />
                          </label>
                        ) : (
                          <label className="edit-field">
                            <span className="edit-field__label">Source</span>
                            <input
                              className="edit-input"
                              value={milestoneSource}
                              onChange={(e) => setMilestoneSource(e.target.value)}
                            />
                          </label>
                        )}
                        {milestoneType !== 'WebLead' && (
                          <label className="edit-field">
                            <span className="edit-field__label">Amount</span>
                            <input
                              className="edit-input"
                              type="number"
                              min="0"
                              step="0.01"
                              value={milestoneAmount}
                              onChange={(e) => setMilestoneAmount(e.target.value)}
                            />
                          </label>
                        )}
                        <label className="edit-field">
                          <span className="edit-field__label">
                            Contact{milestoneType === 'WebLead' && workspace.contacts.length ? ' (required)' : ''}
                          </span>
                          <select
                            className="edit-select"
                            value={milestoneContactId}
                            onChange={(e) => setMilestoneContactId(e.target.value)}
                          >
                            <option value="">No contact selected</option>
                            {workspace.contacts.map((contact) => (
                              <option key={contact.id} value={contact.id}>
                                {`${contact.first_name} ${contact.last_name}`.trim() || `Contact ${contact.id}`}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <label className="edit-field">
                        <span className="edit-field__label">Notes</span>
                        <textarea
                          className="edit-textarea milestone-form-card__notes"
                          rows={3}
                          value={milestoneNotes}
                          onChange={(e) => setMilestoneNotes(e.target.value)}
                        />
                      </label>
                      <div className="edit-actions">
                        <button
                          type="button"
                          className="primary-btn"
                          disabled={!milestoneDate || milestoneSaving}
                          onClick={() => void saveMilestone()}
                        >
                          {milestoneSaving ? 'Saving…' : 'Add Milestone'}
                        </button>
                        {milestoneSaveMsg && <span className="save-confirm" role="status">{milestoneSaveMsg}</span>}
                      </div>
                      {milestoneSaveError && (
                        <p className="data-status data-status--error" role="alert">{milestoneSaveError}</p>
                      )}
                    </div>

                    <details className="milestone-history">
                      <summary>Milestone history ({workspace.milestones.length})</summary>
                      {workspace.milestones.length === 0 ? (
                        <p className="empty-state">No milestones recorded for this client.</p>
                      ) : (
                        <ul className="milestone-history__list">
                          {[...workspace.milestones]
                            .sort((a, b) => b.milestone_date.localeCompare(a.milestone_date))
                            .map((milestone) => (
                              <li key={milestone.id || `${milestone.milestone_type}-${milestone.milestone_date}`}>
                                <strong>{milestone.milestone_type}</strong>
                                <span>{milestone.milestone_date}</span>
                                {milestone.reference_number && <span>· {milestone.reference_number}</span>}
                                {milestone.amount != null && <span>· ${milestone.amount.toLocaleString()}</span>}
                                {milestone.source && <span>· {milestone.source}</span>}
                              </li>
                            ))}
                        </ul>
                      )}
                    </details>
                  </section>

                  <section className="panel panel--wide" aria-labelledby="engagement-history-heading">
                    <div className="panel-header">
                      <h2 id="engagement-history-heading">Appointment / Engagement History</h2>
                      <span className="queue-source">
                        {(workspace.sales_events || []).length} event
                        {(workspace.sales_events || []).length === 1 ? '' : 's'} · newest first
                      </span>
                    </div>
                    <div className="setup-actions" style={{ marginBottom: '0.75rem' }}>
                      {(
                        [
                          ['all', 'All Events'],
                          ['appointments', 'Appointments'],
                          ['rfq', 'RFQs'],
                          ['send', 'Send Information'],
                        ] as const
                      ).map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          className="link-btn"
                          onClick={() => setSalesEventFilter(key)}
                        >
                          {label}
                          {salesEventFilter === key ? ' ·' : ''}
                        </button>
                      ))}
                    </div>
                    {filteredSalesEvents.length === 0 ? (
                      <p className="empty-state">
                        No imported appointment/engagement history for this company yet.
                      </p>
                    ) : (
                      <ul className="ask-history-list">
                        {filteredSalesEvents.map((ev) => (
                          <li key={ev.event_id}>
                            <details>
                              <summary>
                                <strong>{ev.event_type}</strong>
                                <span className="queue-sub">
                                  {' '}
                                  · {displayOrDash(ev.event_date || ev.source_date_time_text)}
                                  {ev.source_rev_spec_text
                                    ? ` · ${ev.source_rev_spec_text}`
                                    : ''}
                                  {ev.source_appointment_grade
                                    ? ` · Grade ${ev.source_appointment_grade}`
                                    : ''}
                                </span>
                              </summary>
                              <div>
                                Contact:{' '}
                                {ev.contact_id ? (
                                  <Link to={workspaceContactHref(ev.contact_id)}>
                                    {displayOrDash(ev.contact_name)}
                                  </Link>
                                ) : (
                                  displayOrDash(ev.contact_name)
                                )}
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
                              {(ev.quoted_amount != null ||
                                ev.outcome_normalized ||
                                ev.source_outcome) && (
                                <div className="queue-sub">
                                  {ev.quoted_amount != null
                                    ? `Quoted $${ev.quoted_amount.toLocaleString()} · `
                                    : ''}
                                  {displayOrDash(ev.outcome_normalized || ev.source_outcome)}
                                </div>
                              )}
                              <div className="queue-sub">
                                Source: {displayOrDash(ev.source_file_name)} ·{' '}
                                {displayOrDash(ev.source_sheet)} row {ev.source_row || '—'} ·
                                Record {displayOrDash(ev.source_record_number)}
                              </div>
                            </details>
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>

                  <section className="panel panel--wide" aria-labelledby="contacts-heading">
                    <div className="panel-header">
                      <h2 id="contacts-heading">
                        Contacts ({workspace.contacts.length})
                      </h2>
                      <div className="heading-controls">
                        <span className="queue-source">
                          Linked by Record No. {workspace.external_record_no}
                        </span>
                        <button
                          type="button"
                          className="primary-btn"
                          onClick={() => {
                            if (workspaceClientId == null || workspaceClientId <= 0) {
                              return
                            }
                            setAddContactOpen(true)
                          }}
                        >
                          Add Contact
                        </button>
                      </div>
                    </div>
                    {workspace.contacts.length === 0 ? (
                      <p className="empty-state">No contacts linked to this Record No.</p>
                    ) : (
                      <div className="queue-table-wrap">
                        <table className="queue-table">
                          <thead>
                            <tr>
                              <th>First Name</th>
                              <th>Last Name</th>
                              <th>Title</th>
                              <th>Phone</th>
                              <th>Alt Phone</th>
                              <th>Email</th>
                            </tr>
                          </thead>
                          <tbody>
                            {workspace.contacts.map((contact) => (
                              <tr key={contact.id} id={`contact-${contact.id}`} tabIndex={-1}>
                                <td>
                                  <Link to={workspaceContactHref(contact.id)}>
                                    {displayOrDash(contact.first_name)}
                                  </Link>
                                </td>
                                <td>
                                  <Link to={workspaceContactHref(contact.id)}>
                                    {displayOrDash(contact.last_name)}
                                  </Link>
                                </td>
                                <td>{displayOrDash(contact.title)}</td>
                                <td>{displayOrDash(contact.phone)}</td>
                                <td>{displayOrDash(contact.alt_phone)}</td>
                                <td>{displayOrDash(contact.email)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </section>

                  <section
                    id="workspace-notes"
                    className={`panel panel--wide${focusFlashId === 'workspace-notes' ? ' focus-flash' : ''}`}
                    aria-labelledby="notes-heading"
                    tabIndex={-1}
                  >
                    <div className="panel-header">
                      <h2 id="notes-heading">
                        Current Client Notes &amp; Activity ({workspaceClientName})
                      </h2>
                    </div>

                    <div className="add-note-card" style={{ marginBottom: '1rem' }}>
                      <h3 className="add-note-card__title">Log Outreach</h3>
                      <p className="muted-note">
                        Saves to Working For ({workspaceClientName}) only. Does not send email or
                        auto-create appointments.
                      </p>
                      <div className="setup-grid" style={{ marginBottom: '0.5rem' }}>
                        <label className="edit-field">
                          <span className="edit-field__label">Contact</span>
                          <select
                            value={outreachContactId}
                            onChange={(e) => setOutreachContactId(e.target.value)}
                          >
                            <option value="">No contact selected</option>
                            {(workspace.contacts || []).map((c) => (
                              <option key={c.id} value={c.id}>
                                {`${c.first_name || ''} ${c.last_name || ''}`.trim() || 'Contact'}
                                {c.email ? ` · ${c.email}` : ''}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="edit-field">
                          <span className="edit-field__label">Outreach Type</span>
                          <select
                            value={outreachType}
                            onChange={(e) =>
                              setOutreachType(e.target.value as (typeof OUTREACH_TYPES)[number])
                            }
                          >
                            {OUTREACH_TYPES.map((t) => (
                              <option key={t} value={t}>
                                {t}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="edit-field">
                          <span className="edit-field__label">Outcome</span>
                          <select
                            value={outreachOutcome}
                            onChange={(e) => setOutreachOutcome(e.target.value)}
                          >
                            <option value="">Select…</option>
                            {outreachOutcomeOptions.map((o) => (
                              <option key={o} value={o}>
                                {o}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="edit-field">
                          <span className="edit-field__label">Follow-Up Date</span>
                          <input
                            type="date"
                            className="edit-input"
                            value={outreachFollowUpDate}
                            onChange={(e) => setOutreachFollowUpDate(e.target.value)}
                          />
                        </label>
                      </div>
                      <NextActionFields
                        catalog={nextActionCatalog}
                        value={outreachNextSel}
                        onChange={setOutreachNextSel}
                        idPrefix="outreach"
                      />
                      <label className="edit-field__label" htmlFor="outreach-notes">
                        Notes
                      </label>
                      <textarea
                        id="outreach-notes"
                        className="edit-textarea add-note-card__textarea"
                        rows={4}
                        value={outreachNotes}
                        onChange={(e) => {
                          setOutreachNotes(e.target.value)
                          setOutreachMsg(null)
                          setOutreachError(null)
                        }}
                        placeholder="What happened on this outreach…"
                      />
                      <div className="edit-actions">
                        <button
                          type="button"
                          className="primary-btn"
                          disabled={outreachSaving || !outreachOutcome}
                          onClick={() => void saveOutreach(false)}
                        >
                          {outreachSaving ? 'Saving…' : 'Save Outreach'}
                        </button>
                        <button
                          type="button"
                          className="primary-btn"
                          disabled={outreachSaving || !outreachOutcome}
                          onClick={() => void saveOutreach(true)}
                        >
                          Save &amp; Next Prospect
                        </button>
                        {outreachMsg && (
                          <span className="save-confirm" role="status">
                            {outreachMsg}
                          </span>
                        )}
                      </div>
                      {outreachError && (
                        <p className="data-status data-status--error" role="alert">
                          {outreachError}
                        </p>
                      )}
                    </div>

                    <div className="add-note-card">
                      <h3 className="add-note-card__title">Add Note</h3>
                      <label className="edit-field__label" htmlFor="add-company-note">
                        New note
                      </label>
                      <textarea
                        id="add-company-note"
                        className="edit-textarea add-note-card__textarea"
                        rows={5}
                        value={newNoteText}
                        onChange={(e) => {
                          setNewNoteText(e.target.value)
                          setNoteSaveMsg(null)
                          setNoteSaveError(null)
                        }}
                        placeholder="Add a note about this company..."
                      />
                      <div className="edit-actions">
                        <button
                          type="button"
                          className="primary-btn"
                          disabled={!noteDirty || noteSaving}
                          onClick={() => void saveNewNote()}
                        >
                          {noteSaving ? 'Saving…' : 'Add Note'}
                        </button>
                        <button
                          type="button"
                          className="link-btn"
                          disabled={noteSaving || (!noteDirty && !newNoteText)}
                          onClick={clearNewNote}
                        >
                          Clear
                        </button>
                        {noteSaveMsg && (
                          <span className="save-confirm" role="status">
                            {noteSaveMsg}
                          </span>
                        )}
                      </div>
                      {noteSaveError && (
                        <p className="data-status data-status--error" role="alert">
                          {noteSaveError}
                        </p>
                      )}
                    </div>

                    <div className="panel-header panel-header--sub">
                      <h3 id="notes-history-heading">
                        {workspaceClientName} NorthStar Activity
                      </h3>
                      <span className="queue-source">
                        Newest first · {workspaceHistory.length} entr
                        {workspaceHistory.length === 1 ? 'y' : 'ies'}
                      </span>
                    </div>
                    <p className="muted-note">
                      Notes and activities saved while working {workspaceClientName}. Cross-client
                      NorthStar activity and LeadMaster history appear in the sections below.
                    </p>
                    {workspaceHistory.length === 0 ? (
                      <p className="empty-state">No NorthStar notes or activities for this client yet.</p>
                    ) : (
                      <ul className="activity-history" aria-labelledby="notes-history-heading">
                        {workspaceHistory.map((entry) => (
                          <li
                            key={entry.key}
                            id={entry.focusId}
                            className={`activity-history__item${
                              focusFlashId === entry.focusId ? ' focus-flash' : ''
                            }`}
                            tabIndex={-1}
                          >
                            <div className="activity-history__meta">
                              <span className="client-badge-pill">{workspaceClientName}</span>
                              <strong>{entry.activityType}</strong>
                              <span className="history-source-badge">{entry.sourceLabel}</span>
                              {entry.at ? <span>{entry.at}</span> : null}
                              {entry.user ? <span>{entry.user}</span> : null}
                              {entry.contactName ? (
                                <span>Contact: {entry.contactName}</span>
                              ) : null}
                            </div>
                            {entry.text ? (
                              <pre className="note-history__body">{entry.text}</pre>
                            ) : (
                              <p className="muted-note">No note text</p>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>

                  <section className="panel panel--wide" aria-labelledby="shared-history-heading">
                    <div className="panel-header">
                      <h2 id="shared-history-heading">NorthStar Shared History</h2>
                      <span className="queue-source">
                        Internal visibility · writes stay on {workspaceClientName}
                      </span>
                    </div>
                    <p className="muted-note">
                      View NorthStar activity across clients on this company. Identical LeadMaster
                      imports appear once. Status and milestones stay client-specific in their
                      panels above.
                    </p>
                    {sharedHistory?.clients && sharedHistory.clients.length > 0 && (
                      <div className="shared-history-clients" aria-label="Clients on this company">
                        {sharedHistory.clients.map((cl) => (
                          <div key={cl.client_id} className="shared-history-client-card">
                            <strong>{cl.client_name}</strong>
                            <span>Record No. {displayOrDash(cl.external_record_no)}</span>
                            <span>Status: {displayOrDash(cl.status)}</span>
                            {cl.is_hot ? <span className="client-badge-pill">Hot</span> : null}
                          </div>
                        ))}
                      </div>
                    )}
                    <div
                      className="shared-history-filters"
                      role="group"
                      aria-label="Shared history client filter"
                    >
                      <button
                        type="button"
                        className={`chip-btn${sharedHistoryFilter === 0 ? ' chip-btn--active' : ''}`}
                        onClick={() => void reloadSharedHistory(0)}
                      >
                        All Clients
                      </button>
                      {(sharedHistory?.clients ?? []).map((cl) => (
                        <button
                          key={cl.client_id}
                          type="button"
                          className={`chip-btn${
                            sharedHistoryFilter === cl.client_id ? ' chip-btn--active' : ''
                          }`}
                          onClick={() => void reloadSharedHistory(cl.client_id)}
                        >
                          {cl.client_name}
                        </button>
                      ))}
                    </div>
                    {sharedHistoryLoading && (
                      <p className="data-status">Loading shared history…</p>
                    )}

                    {!sharedHistoryLoading && (
                      <>
                        <div className="panel-header panel-header--sub">
                          <h3 id="recent-ns-activity-heading">Recent NorthStar Activity</h3>
                          <span className="queue-source">
                            Newest first · {(sharedHistory?.northstar_items ?? []).length}
                          </span>
                        </div>
                        {(sharedHistory?.northstar_items ?? []).length === 0 ? (
                          <p className="empty-state">No NorthStar activity across clients yet.</p>
                        ) : (
                          <ul
                            className="activity-history shared-history-list"
                            aria-labelledby="recent-ns-activity-heading"
                          >
                            {(sharedHistory?.northstar_items ?? []).map((item) => (
                              <li key={item.item_key} className="activity-history__item">
                                <div className="activity-history__meta">
                                  <span className="client-badge-pill">{item.client_name}</span>
                                  <strong>
                                    {item.activity_type || item.title || item.item_type}
                                  </strong>
                                  {item.event_at ? <span>{item.event_at}</span> : null}
                                  {item.created_by ? <span>{item.created_by}</span> : null}
                                </div>
                                {item.body ? (
                                  <pre className="note-history__body">{item.body}</pre>
                                ) : (
                                  <p className="muted-note">No detail text</p>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}

                        <div className="panel-header panel-header--sub">
                          <h3 id="shared-company-history-heading">Shared Company History</h3>
                          <span className="queue-source">
                            Chronological LeadMaster events · attribution preserved ·{' '}
                            {(sharedHistory?.shared_company_history_items ?? []).length}
                          </span>
                        </div>
                        {(sharedHistory?.shared_company_history_items ?? []).length === 0 ? (
                          <p className="empty-state">
                            No shared company note-history events for this company.
                          </p>
                        ) : (
                          <ul
                            className="activity-history shared-history-list"
                            aria-labelledby="shared-company-history-heading"
                          >
                            {(sharedHistory?.shared_company_history_items ?? []).map((item) => (
                              <li
                                key={item.item_key}
                                id={
                                  item.source_id != null
                                    ? `shared-history-${item.source_id}`
                                    : undefined
                                }
                                className="activity-history__item activity-history__item--legacy"
                              >
                                <div className="activity-history__meta">
                                  <span className="client-badge-pill">
                                    {item.attribution || item.client_name || 'Shared history'}
                                  </span>
                                  <strong>{item.title || item.activity_type || 'Shared history'}</strong>
                                  {item.attribution_evidence ? (
                                    <span>{item.attribution_evidence}</span>
                                  ) : null}
                                  {item.event_at ? <span>{item.event_at}</span> : null}
                                  {item.created_by ? <span>{item.created_by}</span> : null}
                                  {item.source_file ? (
                                    <span className="history-source-badge">{item.source_file}</span>
                                  ) : null}
                                </div>
                                {item.body ? (
                                  <pre className="note-history__body">{item.body}</pre>
                                ) : (
                                  <p className="muted-note">No detail text</p>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}

                        <div className="panel-header panel-header--sub">
                          <h3 id="shared-legacy-heading">LeadMaster Legacy History</h3>
                          <span className="queue-source">
                            Shown once when identical across clients
                          </span>
                        </div>
                        {(sharedHistory?.shared_legacy_items ?? []).length === 0 ? (
                          <p className="empty-state">
                            No shared LeadMaster history for this company.
                          </p>
                        ) : (
                          <ul
                            className="activity-history shared-history-list"
                            aria-labelledby="shared-legacy-heading"
                          >
                            {(sharedHistory?.shared_legacy_items ?? []).map((item) => (
                              <li
                                key={item.item_key}
                                id={
                                  item.source_id != null
                                    ? `legacy-note-${item.source_id}`
                                    : undefined
                                }
                                className="activity-history__item activity-history__item--legacy"
                              >
                                <div className="activity-history__meta">
                                  <span className="client-badge-pill">Shared Legacy</span>
                                  <strong>{item.title || 'LeadMaster Legacy History'}</strong>
                                  {item.shared_client_names.length > 0 ? (
                                    <span>
                                      From: {item.shared_client_names.join(', ')}
                                    </span>
                                  ) : null}
                                  {item.event_at ? <span>{item.event_at}</span> : null}
                                </div>
                                {item.body ? (
                                  <pre className="note-history__body">{item.body}</pre>
                                ) : (
                                  <p className="muted-note">No detail text</p>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}

                        <div className="panel-header panel-header--sub">
                          <h3 id="distinct-legacy-heading">Client Legacy History</h3>
                          <span className="queue-source">
                            Only when LeadMaster text differs by client
                          </span>
                        </div>
                        {(sharedHistory?.distinct_legacy_items ?? []).length === 0 ? (
                          <p className="empty-state">
                            No distinct client-specific LeadMaster history.
                          </p>
                        ) : (
                          <ul
                            className="activity-history shared-history-list"
                            aria-labelledby="distinct-legacy-heading"
                          >
                            {(sharedHistory?.distinct_legacy_items ?? []).map((item) => (
                              <li
                                key={item.item_key}
                                id={
                                  item.source_id != null
                                    ? `legacy-note-${item.source_id}`
                                    : undefined
                                }
                                className="activity-history__item activity-history__item--legacy"
                              >
                                <div className="activity-history__meta">
                                  <span className="client-badge-pill">{item.client_name}</span>
                                  <strong>{item.title || 'Legacy Note'}</strong>
                                  {item.external_record_no ? (
                                    <span>RN {item.external_record_no}</span>
                                  ) : null}
                                  {item.event_at ? <span>{item.event_at}</span> : null}
                                </div>
                                {item.body ? (
                                  <pre className="note-history__body">{item.body}</pre>
                                ) : (
                                  <p className="muted-note">No detail text</p>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </>
                    )}
                  </section>
                </div>
              )}
            </>
          )}

          {!loading && !error && showProspects && (
            <>
              <AddCompanyModal
                open={addCompanyOpen}
                onClose={() => setAddCompanyOpen(false)}
                clientId={activeClientId}
                clientName={clientName}
              />
              <div className="page-heading page-heading--split">
                <div>
                  <h1>
                    {activeNav === 'companies'
                      ? 'Companies'
                      : prospectFilterMeta?.heading ?? 'Prospects'}
                  </h1>
                  <p>
                    {activeNav === 'companies'
                      ? `${clientName} companies assigned to this client. Add a company to create or link a shared master record.`
                      : prospectFilterMeta?.description ??
                        `${clientName} companies from the production import — status, primary contact, and last update.`}
                  </p>
                  {activeClientId == null || activeClientId <= 0 ? (
                    <p className="data-status" role="status">
                      {SELECT_CLIENT_FOR_WRITE}
                    </p>
                  ) : null}
                </div>
                <div className="heading-controls">
                  <button
                    type="button"
                    className="primary-btn"
                    onClick={() => {
                      if (activeClientId == null || activeClientId <= 0) return
                      setAddCompanyOpen(true)
                    }}
                  >
                    Add Company
                  </button>
                  {prospectFilterMeta && (
                    <button
                      type="button"
                      className="link-btn clear-filter-btn"
                      onClick={() => navigate('/prospects')}
                    >
                      Clear Filter
                    </button>
                  )}
                </div>
              </div>

              <section
                className="panel panel--queue"
                aria-label={prospectFilterMeta?.heading ?? `${clientName} prospect queue`}
              >
                <div className="panel-header">
                  <h2>
                    {companyListLoading
                      ? 'Loading…'
                      : debouncedCompanyListQuery || prospectStatusFilter || prospectFilter
                        ? `${companyListTotal} match${companyListTotal === 1 ? '' : 'es'}`
                        : `${companyListClientTotal || companyListTotal} prospect${
                            (companyListClientTotal || companyListTotal) === 1 ? '' : 's'
                          }`}
                  </h2>
                  <span className="queue-source">
                    {companyListTotal === 0
                      ? `${companyListClientTotal} total`
                      : `${companyListPage * COMPANY_PAGE_SIZE + 1}–${Math.min(
                          companyListTotal,
                          (companyListPage + 1) * COMPANY_PAGE_SIZE,
                        )} of ${companyListTotal}${
                          debouncedCompanyListQuery || prospectStatusFilter
                            ? ` · ${companyListClientTotal} total`
                            : ''
                        }`}
                  </span>
                </div>

                <div className="opportunity-filter-grid" style={{ marginBottom: '0.85rem' }}>
                  <label className="edit-field">
                    <span className="edit-field__label">Search companies</span>
                    <input
                      type="search"
                      className="edit-input"
                      value={companyListQuery}
                      placeholder={`Search all ${clientName} companies`}
                      aria-label={`Search all ${clientName} companies`}
                      onChange={(event) => setCompanyListQuery(event.target.value)}
                    />
                  </label>
                  <label className="edit-field">
                    <span className="edit-field__label">Status</span>
                    <select
                      className="edit-select"
                      value={prospectStatusFilter}
                      onChange={(event) => {
                        const next = new URLSearchParams(searchParams)
                        const value = event.target.value
                        if (value) next.set('status', value)
                        else next.delete('status')
                        setSearchParams(next, { replace: true })
                      }}
                    >
                      <option value="">Any status</option>
                      {statusOptions.map((status) => (
                        <option key={status} value={status}>
                          {status}
                        </option>
                      ))}
                    </select>
                  </label>
                  {activeClientId != null && activeClientId > 0 ? (
                    <label className="edit-field">
                      <span className="edit-field__label">Assigned rep</span>
                      <select
                        className="edit-select"
                        value={prospectAssignedFilter}
                        aria-label="Assigned rep"
                        onChange={(event) => {
                          const next = new URLSearchParams(searchParams)
                          const value = event.target.value
                          if (value) next.set('assigned', value)
                          else next.delete('assigned')
                          setSearchParams(next, { replace: true })
                        }}
                      >
                        <option value="">All reps</option>
                        <option value="unassigned">Unassigned</option>
                        {user?.id ? (
                          <option value={String(user.id)}>
                            {user.full_name || user.email || 'Me'}
                          </option>
                        ) : null}
                        {bulkAssignees
                          .filter((assignee) => assignee.user_id !== user?.id)
                          .map((assignee) => (
                            <option key={assignee.user_id} value={String(assignee.user_id)}>
                              {assignee.full_name || assignee.email}
                            </option>
                          ))}
                      </select>
                    </label>
                  ) : null}
                </div>

                {showProspectBulk ? (
                  <ProspectBulkAssign
                    enabled
                    clientId={activeClientId as number}
                    clientName={clientName}
                    rows={companyListRows}
                    filteredTotal={companyListTotal}
                    filter={prospectBulkFilter}
                    selection={prospectSelection}
                    onSelectionChange={setProspectSelection}
                    onAssigned={() => {
                      void refreshProspectsQuietly()
                    }}
                  />
                ) : null}

                {(() => {
                  const rows = companyListRows
                  if (companyListLoading) {
                    return <p className="data-status">Loading {clientName} companies…</p>
                  }
                  if (rows.length === 0) {
                    return (
                      <p className="empty-state">
                        {prospectFilterMeta?.empty ??
                          (debouncedCompanyListQuery
                            ? `No ${clientName} companies match that search.`
                            : `No ${clientName} prospects match this filter.`)}
                      </p>
                    )
                  }
                  const pageCount = Math.max(
                    1,
                    Math.ceil(companyListTotal / COMPANY_PAGE_SIZE),
                  )
                  return (
                    <>
                      <div className="queue-table-wrap">
                        <table className="queue-table">
                          <thead>
                            <tr>
                              {showProspectBulk ? (
                                <th className="select-col">
                                  <input
                                    type="checkbox"
                                    aria-label="Select current page"
                                    checked={pageAllSelected(prospectSelection, rows)}
                                    onChange={() =>
                                      setProspectSelection(
                                        selectCurrentPage(rows, companyListTotal),
                                      )
                                    }
                                  />
                                </th>
                              ) : null}
                              <th>Company</th>
                              <th>City</th>
                              <th>State</th>
                              <th>
                                {activeClientId === 0 ? 'Client Statuses' : 'Status'}
                              </th>
                              <th>Primary Contact</th>
                              <th>Phone</th>
                              <th>Assigned</th>
                              <th>Last Updated</th>
                            </tr>
                          </thead>
                          <tbody>
                            {rows.map((prospect) => (
                              <tr
                                key={
                                  activeClientId === 0
                                    ? `company-${prospect.id}`
                                    : `${prospect.client_id}-${prospect.relationship_id || prospect.id}`
                                }
                              >
                                {showProspectBulk ? (
                                  <td className="select-col">
                                    <input
                                      type="checkbox"
                                      aria-label={`Select ${prospect.company || 'company'}`}
                                      checked={isRowSelected(
                                        prospectSelection,
                                        prospect.relationship_id,
                                      )}
                                      onChange={() =>
                                        setProspectSelection((current) =>
                                          toggleRow(
                                            current,
                                            prospect.relationship_id,
                                            rows.map((item) => item.relationship_id),
                                          ),
                                        )
                                      }
                                    />
                                  </td>
                                ) : null}
                                <td>
                                  <Link
                                    to={appendWorkspaceOrigin(
                                      companyWorkspaceHref(
                                        prospect.external_record_no,
                                        prospect.client_id || workspaceClientId,
                                      ),
                                      {
                                        from: PROSPECTS_FROM,
                                        listQuery: searchParams.toString(),
                                      },
                                    )}
                                    className="company-link"
                                    onClick={() => setSidebarOpen(false)}
                                  >
                                    {prospect.company || '—'}
                                  </Link>
                                  <span className="queue-sub">
                                    Record No. {prospect.external_record_no}
                                  </span>
                                </td>
                                <td>{displayOrDash(prospect.city)}</td>
                                <td>{displayOrDash(prospect.state)}</td>
                                <td>
                                  {activeClientId === 0 ? (
                                    (() => {
                                      const chips = sortedClientStatuses(
                                        prospect.client_statuses,
                                      )
                                      if (chips.length === 0) return '—'
                                      return (
                                        <div className="client-status-chips">
                                          {chips.map((chip) => (
                                            <span
                                              key={`${chip.client_id}-${chip.relationship_id}`}
                                              className="client-status-chip"
                                            >
                                              {formatClientStatusChip(chip)}
                                            </span>
                                          ))}
                                        </div>
                                      )
                                    })()
                                  ) : (
                                    displayOrDash(prospectStatus(prospect))
                                  )}
                                </td>
                                <td>{displayOrDash(prospect.primary_contact)}</td>
                                <td>{displayOrDash(prospect.phone)}</td>
                                <td>
                                  {displayOrDash(
                                    prospect.assigned_user_name ||
                                      (prospect.assigned_user_id ? String(prospect.assigned_user_id) : ''),
                                  )}
                                </td>
                                <td>{formatDisplayDateTime(prospect.last_updated)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      {companyListTotal > COMPANY_PAGE_SIZE ? (
                        <div className="setup-actions" style={{ marginTop: '0.75rem' }}>
                          <button
                            type="button"
                            className="link-btn"
                            disabled={companyListPage <= 0 || companyListLoading}
                            onClick={() => setCompanyListPage((p) => Math.max(0, p - 1))}
                          >
                            Previous
                          </button>
                          <span className="queue-source">
                            Page {companyListPage + 1} of {pageCount}
                          </span>
                          <button
                            type="button"
                            className="link-btn"
                            disabled={
                              companyListPage + 1 >= pageCount || companyListLoading
                            }
                            onClick={() => setCompanyListPage((p) => p + 1)}
                          >
                            Next
                          </button>
                        </div>
                      ) : null}
                    </>
                  )
                })()}
              </section>
            </>
          )}

          {showAppointments && (
            <Appointments
              activeClientId={activeClientId}
              activeClientName={clientName}
              onAppointmentsChanged={() => setQueueRefreshKey((key) => key + 1)}
            />
          )}

          {showActivities && (
            <Activities
              activeClientId={activeClientId}
              activeClientName={clientName}
            />
          )}

          {showCampaigns && (
            <Campaigns
              activeClientId={activeClientId}
              activeClientName={clientName}
              assignedClients={availableClients}
            />
          )}

          {showCampaignWorkspace && campaignWorkspaceId && (
            <CampaignWorkspacePage
              campaignId={campaignWorkspaceId}
              activeClientId={activeClientId}
            />
          )}

          {showContacts && (
            <Contacts
              activeClientId={activeClientId}
              activeClientName={clientName}
            />
          )}

          {showTasks && (
            <Tasks
              activeClientId={activeClientId}
              activeClientName={clientName}
              onTasksChanged={() => setQueueRefreshKey((key) => key + 1)}
            />
          )}

          {showReports && (
            <Reports
              activeClientId={activeClientId}
              activeClientName={clientName}
              currentUser={user}
            />
          )}

          {showAdministration &&
            (canAdminister ? (
            <Administration
              activeClientId={activeClientId}
              availableClients={availableClients}
            />
            ) : (
              <Navigate to="/" replace />
            ))}

          {showClientSetupPage && !canAdminister ? <Navigate to="/" replace /> : null}

          {!loading && !error && showWorkQueue && (
            <WorkQueue
              activeClientId={activeClientId}
              assignedClients={availableClients}
              onQueueChanged={() => setQueueRefreshKey((key) => key + 1)}
            />
          )}

          {showResearch && (
            <Research
              activeClientId={activeClientId}
              activeClientName={clientName}
              prospects={prospects}
              loading={loading}
            />
          )}

          {!error && showResearchCompany && researchRecordNo && (
            <ResearchCompany
              recordNo={researchRecordNo}
              defaultClientId={
                selectedClientId != null && selectedClientId > 0
                  ? selectedClientId
                  : activeClientId != null && activeClientId > 0
                    ? activeClientId
                    : null
              }
              defaultClientName={clientName}
            />
          )}

          {!loading && !error && showAskNorthStar && (
            <AskNorthStar
              activeClientId={activeClientId}
              activeClientName={clientName}
              assignedClients={availableClients}
            />
          )}

          {!loading && !error && showCrossClientOpportunities && (
            canAdminister ? (
              <CrossClientOpportunities client={client} />
            ) : (
              <Navigate to="/" replace />
            )
          )}

          {!loading && !error && showClientSetupPage && canAdminister && showClientOnboarding && (
            <ClientOnboarding />
          )}

          {activeNav === 'client-knowledge' && activeClientId != null && activeClientId > 0 && (
            <ClientKnowledge clientId={activeClientId} />
          )}
          {activeNav === 'client-knowledge' && !(activeClientId != null && activeClientId > 0) && (
            <p className="data-status">No assigned client is available for Client Knowledge.</p>
          )}

          {!loading &&
            !error &&
            showClientSetupPage &&
            canAdminister &&
            !showClientOnboarding &&
            clientKnowledgeId &&
            clientKnowledgeId > 0 && (
            <ClientKnowledge clientId={clientKnowledgeId} />
          )}

          {!loading &&
            !error &&
            showClientSetupPage &&
            canAdminister &&
            !showClientOnboarding &&
            !clientKnowledgeId && (
            <ClientSetup clientId={clientSetupId && clientSetupId > 0 ? clientSetupId : null} />
          )}

          {!loading && !error && showDashboard && (
            <>
              <div className="page-heading page-heading--split">
                <div>
                  <h1>
                    {greeting}, {greetingName}
                  </h1>
                  <p>
                    Today&apos;s {clientName} work — priority outreach, follow-ups, and plant intro
                    appointments.
                  </p>
                </div>
                <div className="heading-controls">
                  <span className="date-chip">{todayLabel}</span>
                </div>
              </div>

              <div className="kpi-section">
                <h2 className="kpi-section__label">Revenue &amp; Opportunities</h2>
                <section className="stat-grid" aria-label="Revenue and opportunities">
                  {kpiCards.map((card) => (
                    <Link
                      key={card.id}
                      to={card.to}
                      className={`stat-card stat-card--clickable stat-card--${card.tone}`}
                      aria-label={`${card.label}: ${card.value}. Open filtered list.`}
                    >
                      <div className="stat-card__top">
                        <p className="stat-label">{card.label}</p>
                        <span className="stat-icon" aria-hidden="true">
                          {card.icon}
                        </span>
                      </div>
                      <p className="stat-value">{card.value}</p>
                      <p className={`stat-change stat-change--${card.tone}`}>{card.change}</p>
                    </Link>
                  ))}
                </section>
              </div>

              <div className="kpi-section">
                <h2 className="kpi-section__label">Due Work</h2>
                {startHereMessage ? (
                  <p className="dashboard-start-here" role="status">
                    {startHereMessage}
                  </p>
                ) : null}
                <section className="stat-grid stat-grid--work-queue" aria-label="Due work">
                  {workQueueCards
                    .filter((card) => card.lane === 'due')
                    .filter((card) => canAdminister || card.id !== 'cross-client')
                    .map((card) => (
                    <Link
                      key={card.id}
                      to={card.to}
                      className={`stat-card stat-card--clickable stat-card--${card.tone}`}
                      aria-label={`${card.label}: ${card.value}. ${
                        card.id.startsWith('follow-ups')
                          ? 'Open follow-up contact.'
                          : 'Open queue.'
                      }`}
                    >
                      <div className="stat-card__top">
                        <p className="stat-label">{card.label}</p>
                        <span className="stat-icon" aria-hidden="true">
                          {card.icon}
                        </span>
                      </div>
                      <p className="stat-value">{card.value}</p>
                      <p className={`stat-change stat-change--${card.tone}`}>{card.change}</p>
                    </Link>
                  ))}
                </section>
              </div>

              <div className="kpi-section">
                <h2 className="kpi-section__label">Fresh Calling</h2>
                <section className="stat-grid stat-grid--work-queue" aria-label="Fresh calling">
                  {workQueueCards
                    .filter((card) => card.lane === 'fresh')
                    .map((card) => (
                    <Link
                      key={card.id}
                      to={card.to}
                      className={`stat-card stat-card--clickable stat-card--${card.tone}`}
                      aria-label={`${card.label}: ${card.value}. Open New Assignments.`}
                    >
                      <div className="stat-card__top">
                        <p className="stat-label">{card.label}</p>
                        <span className="stat-icon" aria-hidden="true">
                          {card.icon}
                        </span>
                      </div>
                      <p className="stat-value">{card.value}</p>
                      <p className={`stat-change stat-change--${card.tone}`}>{card.change}</p>
                    </Link>
                  ))}
                </section>
              </div>

              {activeClientId != null && activeClientId > 0 ? (
                <section
                  className="panel panel--wide dashboard-follow-ups"
                  id="dashboard-follow-ups"
                  aria-labelledby="follow-ups-heading"
                >
                  <div className="panel-header">
                    <h2 id="follow-ups-heading">{clientName} Follow-Ups</h2>
                  </div>
                  <div className="dashboard-follow-up-grid">
                    <DashboardFollowUpList
                      id="dashboard-follow-ups-overdue"
                      title="Overdue"
                      empty="No overdue follow-ups."
                      items={dashboardFollowUps?.overdue ?? []}
                      onNavigate={() => setSidebarOpen(false)}
                    />
                    <DashboardFollowUpList
                      id="dashboard-follow-ups-today"
                      title="Due Today"
                      empty="No follow-ups due today."
                      items={dashboardFollowUps?.due_today ?? []}
                      onNavigate={() => setSidebarOpen(false)}
                    />
                    <DashboardFollowUpList
                      id="dashboard-follow-ups-upcoming"
                      title="Upcoming Follow-Ups"
                      empty="No upcoming follow-ups in the next 7 days."
                      items={dashboardFollowUps?.upcoming ?? []}
                      onNavigate={() => setSidebarOpen(false)}
                    />
                  </div>
                </section>
              ) : null}

              {canAdminister ? (
              <Link
                to={
                  activeClientId != null && activeClientId > 0
                    ? `/cross-client-opportunities?target_client_id=${activeClientId}`
                    : '/cross-client-opportunities'
                }
                className="opportunity-indicator"
                aria-label={`Cross-Client Opportunities: ${opportunityCount ?? 0}. Open opportunities.`}
              >
                <span>Cross-Client Opportunities</span>
                <strong>{opportunityCount ?? '—'}</strong>
                <small>Find companies with proven engagement elsewhere</small>
              </Link>
              ) : null}

              <div className="panels">
                <section className="panel panel--wide" aria-labelledby="priority-heading">
                  <div className="panel-header">
                    <h2 id="priority-heading">Today&apos;s Priority Prospects</h2>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => goToNav('prospects')}
                    >
                      View all
                    </button>
                  </div>
                  {priorityProspects.length === 0 ? (
                    <p className="empty-state">
                      {activeClientId != null && activeClientId > 0
                        ? `No actionable ${clientName} prospects in today's calling queue.`
                        : "Select a Working For client to see today's priority prospects."}
                    </p>
                  ) : (
                    <div className="priority-table-wrap">
                      <table className="priority-table">
                        <thead>
                          <tr>
                            <th>Company</th>
                            <th>Contact</th>
                            <th>Priority</th>
                            <th>Why / Next</th>
                            <th>Follow-Up</th>
                            <th>Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {priorityProspects.map((item) => (
                            <tr key={`${item.client_id}-${item.external_record_no}`}>
                              <td>
                                <Link
                                  to={appendWorkspaceOrigin(
                                    companyWorkspaceHref(
                                      item.external_record_no,
                                      item.client_id,
                                    ),
                                    { from: DASHBOARD_FROM },
                                  )}
                                  className="company-link"
                                  onClick={() => setSidebarOpen(false)}
                                >
                                  {item.company_name}
                                </Link>
                                <span className="queue-sub">
                                  Record No. {item.external_record_no}
                                </span>
                              </td>
                              <td>{displayOrDash(item.primary_contact)}</td>
                              <td>
                                <span
                                  className={`priority-pill priority-pill--${priorityBucketTone(
                                    item.priority_bucket,
                                  )}`}
                                >
                                  {priorityBucketLabel(item.priority_bucket)}
                                </span>
                              </td>
                              <td>
                                {shortenAction(
                                  displayNextAction(item.next_action, nextActionCatalog) ||
                                    item.next_action ||
                                    item.priority_reason ||
                                    'Review',
                                )}
                              </td>
                              <td>{displayOrDash(item.follow_up_date)}</td>
                              <td>
                                <span className="status-text">{displayOrDash(item.status)}</span>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>

                <div className="side-panels">
                  <section className="panel" aria-labelledby="activity-heading">
                    <div className="panel-header">
                      <h2 id="activity-heading">Recent Activity</h2>
                    </div>
                    <ul className="activity-list">
                      {activeClientId == null || activeClientId <= 0 ? (
                        <li className="activity-item">
                          <div>
                            <p>Select an Active Client to see recent activity.</p>
                          </div>
                        </li>
                      ) : recentActivities.length === 0 ? (
                        <li className="activity-item">
                          <div>
                            <p>No recent {clientName} activity.</p>
                          </div>
                        </li>
                      ) : (
                        recentActivities.map((row) => {
                          const href = recentActivityHref(row)
                          const exact = formatExactActivityWhen(row.activity_at)
                          const detail = [row.notes, row.outcome].filter((part) => part.trim())[0] || ''
                          const clickable = href !== '#'
                          const body = (
                            <>
                              <span
                                className={`activity-dot activity-dot--${activityTone(row.activity_type)}`}
                                aria-hidden="true"
                              />
                              <div>
                                <p>
                                  <strong>{displayOrDash(row.activity_type)}</strong>
                                  {row.company_name ? ` · ${row.company_name}` : ''}
                                  {row.contact_name ? ` · ${row.contact_name}` : ''}
                                </p>
                                <p className="activity-sub">
                                  {displayOrDash(row.user_name)}
                                  {detail ? ` · ${detail}` : ''}
                                </p>
                                <time dateTime={row.activity_at || undefined} title={exact}>
                                  {formatRelativeActivityWhen(row.activity_at)}
                                </time>
                              </div>
                            </>
                          )
                          return (
                            <li key={row.item_key} className="activity-item">
                              {clickable ? (
                                <Link
                                  to={href}
                                  className="activity-item-link"
                                  title={exact}
                                  onClick={() => setSidebarOpen(false)}
                                >
                                  {body}
                                </Link>
                              ) : (
                                <div className="activity-item-link" title={exact}>
                                  {body}
                                </div>
                              )}
                            </li>
                          )
                        })
                      )}
                    </ul>
                  </section>

                  <section className="panel" aria-labelledby="appointments-heading">
                    <div className="panel-header">
                      <h2 id="appointments-heading">Upcoming Appointments</h2>
                    </div>
                    <ul className="appointment-list">
                      {upcomingAppointments.length === 0 ? (
                        <li className="appointment-item">
                          <strong>No upcoming appointments</strong>
                          <span>Set one from Log Call when the outcome is Appointment Set.</span>
                        </li>
                      ) : (
                        upcomingAppointments.map((item) => (
                          <li key={item.id} className="appointment-item">
                            <strong>{item.company_name}</strong>
                            <span>{item.contact_name || 'Contact TBD'}</span>
                            <time>{formatAppointmentWhen(item)}</time>
                          </li>
                        ))
                      )}
                    </ul>
                  </section>
                </div>
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

export default App
