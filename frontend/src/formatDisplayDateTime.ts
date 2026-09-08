/**
 * Shared Companies-page (and related) date/time display.
 * Output: M/D/YYYY h:mm AM/PM in the user's local timezone.
 */

function asText(value: unknown): string {
  if (value == null) return ''
  return String(value).trim()
}

/** Format a valid Date as M/D/YYYY h:mm AM/PM in local time. */
export function formatLocalDateTime(dt: Date): string {
  const month = dt.getMonth() + 1
  const day = dt.getDate()
  const year = dt.getFullYear()
  let hours = dt.getHours()
  const minutes = dt.getMinutes()
  const ampm = hours >= 12 ? 'PM' : 'AM'
  hours = hours % 12
  if (hours === 0) hours = 12
  const mm = String(minutes).padStart(2, '0')
  return `${month}/${day}/${year} ${hours}:${mm} ${ampm}`
}

/**
 * Parse common CRM timestamp strings into a Date.
 * ISO values with Z / offset are absolute instants (UTC when Z).
 * Naive US-style or space-separated values are treated as local wall time.
 */
export function parseDisplayDateTime(value: unknown): Date | null {
  const text = asText(value)
  if (!text) return null

  // ISO-8601 with T (including Z or numeric offset) — Date parses as absolute.
  if (/^\d{4}-\d{2}-\d{2}T/.test(text)) {
    const dt = new Date(text)
    return Number.isNaN(dt.getTime()) ? null : dt
  }

  // ISO date with space: 2026-09-07 20:42:06[.sss][Z|±hh:mm]
  const isoSpace = text.match(
    /^(\d{4}-\d{2}-\d{2})[ ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)$/i,
  )
  if (isoSpace) {
    const dt = new Date(`${isoSpace[1]}T${isoSpace[2]}`)
    return Number.isNaN(dt.getTime()) ? null : dt
  }

  // Existing DB / UI style: M/D/YYYY[ H:mm[:ss][ AM|PM]]
  const us = text.match(
    /^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?)?$/i,
  )
  if (us) {
    const month = Number(us[1])
    const day = Number(us[2])
    const year = Number(us[3])
    let hours = us[4] != null ? Number(us[4]) : 0
    const minutes = us[5] != null ? Number(us[5]) : 0
    const seconds = us[6] != null ? Number(us[6]) : 0
    const ap = us[7]?.toUpperCase()
    if (ap === 'PM' && hours < 12) hours += 12
    if (ap === 'AM' && hours === 12) hours = 0
    if (
      month < 1 ||
      month > 12 ||
      day < 1 ||
      day > 31 ||
      hours < 0 ||
      hours > 23 ||
      minutes > 59 ||
      seconds > 59
    ) {
      return null
    }
    const dt = new Date(year, month - 1, day, hours, minutes, seconds)
    if (Number.isNaN(dt.getTime())) return null
    // Reject overflow (e.g. 2/31/2026 → March)
    if (
      dt.getFullYear() !== year ||
      dt.getMonth() !== month - 1 ||
      dt.getDate() !== day
    ) {
      return null
    }
    return dt
  }

  const fallback = new Date(text)
  return Number.isNaN(fallback.getTime()) ? null : fallback
}

/** Display helper: blank/invalid → —; otherwise local M/D/YYYY h:mm AM/PM. */
export function formatDisplayDateTime(value: unknown): string {
  const text = asText(value)
  if (!text) return '—'
  const dt = parseDisplayDateTime(text)
  if (!dt) return '—'
  return formatLocalDateTime(dt)
}
