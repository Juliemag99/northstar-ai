/** Appointment Set status helpers shared by Log Call and the Appointments page. */

export const APPOINTMENT_SET_STATUSES = ['Appointment Set', 'Appt Set Email Marketing'] as const

export const APPOINTMENT_TYPES = ['Phone', 'Video Meeting', 'In Person', 'Other'] as const

export const APPOINTMENT_SOURCES = ['Phone Call', 'Email Marketing'] as const

export const APPOINTMENT_TIMEZONES = [
  { value: 'America/New_York', label: 'Eastern (America/New_York)' },
  { value: 'America/Chicago', label: 'Central (America/Chicago)' },
  { value: 'America/Denver', label: 'Mountain (America/Denver)' },
  { value: 'America/Los_Angeles', label: 'Pacific (America/Los_Angeles)' },
  { value: 'America/Phoenix', label: 'Arizona (America/Phoenix)' },
  { value: 'UTC', label: 'UTC' },
] as const

export const APPOINTMENT_GRADES = ['A', 'B', 'C', 'D', 'No Show'] as const

export const APPOINTMENT_OUTCOMES = [
  'Proceeding',
  'Needs Follow-Up',
  'Not a Fit',
  'Reschedule',
  'No Decision',
] as const

export type AppointmentDetails = {
  appointment_date: string
  start_time: string
  timezone: string
  datetime_tbd: boolean
  appointment_type: string
  location_or_link: string
  revenue_specialist_user_id: number | null
  notes: string
  source: string
  idempotency_key: string
}

export function isAppointmentSetStatus(status: string | null | undefined): boolean {
  const key = (status || '').trim().toLowerCase()
  if (!key) return false
  if (key === 'appointment set' || key === 'appt set' || key === 'appt set email marketing') {
    return true
  }
  if (key.includes('appointment set')) return true
  if (key.startsWith('appt set')) return true
  return false
}

export function appointmentSourceForStatus(status: string | null | undefined): string {
  const key = (status || '').trim().toLowerCase()
  if (key.includes('email marketing')) return 'Email Marketing'
  return 'Phone Call'
}

export function newAppointmentIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `appt-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

export function emptyAppointmentDetails(overrides?: Partial<AppointmentDetails>): AppointmentDetails {
  return {
    appointment_date: '',
    start_time: '',
    timezone: 'America/Chicago',
    datetime_tbd: false,
    appointment_type: 'Phone',
    location_or_link: '',
    revenue_specialist_user_id: null,
    notes: '',
    source: 'Phone Call',
    idempotency_key: newAppointmentIdempotencyKey(),
    ...overrides,
  }
}

export function appointmentDetailsAreValid(details: AppointmentDetails): boolean {
  if (details.datetime_tbd) return true
  return Boolean(details.appointment_date.trim()) && Boolean(details.start_time.trim())
}

export function formatAppointmentWhen(item: {
  datetime_tbd?: boolean
  appointment_date?: string
  start_time?: string
  timezone?: string
}): string {
  if (item.datetime_tbd || !(item.appointment_date || '').trim()) return 'Date/Time TBD'
  const datePart = (item.appointment_date || '').trim()
  const timePart = (item.start_time || '').trim().slice(0, 5)
  const tz = (item.timezone || '').trim()
  if (datePart && timePart) return tz ? `${datePart} ${timePart} ${tz}` : `${datePart} ${timePart}`
  return datePart || timePart || '—'
}
