import { describe, expect, it } from 'vitest'
import {
  formatDisplayDateTime,
  formatLocalDateTime,
  parseDisplayDateTime,
} from './formatDisplayDateTime'

function expectedLocalFromIsoUtc(iso: string): string {
  return formatLocalDateTime(new Date(iso))
}

describe('formatDisplayDateTime', () => {
  it('formats ISO UTC (Z) in the local timezone', () => {
    const iso = '2026-09-07T20:42:06Z'
    expect(formatDisplayDateTime(iso)).toBe(expectedLocalFromIsoUtc(iso))
  })

  it('formats existing database-style timestamps consistently with AM/PM', () => {
    // Naive local wall time — same components after round-trip through formatter.
    expect(formatDisplayDateTime('7/28/2026 13:58')).toBe('7/28/2026 1:58 PM')
    expect(formatDisplayDateTime('7/28/2026 09:05')).toBe('7/28/2026 9:05 AM')
  })

  it('preserves — for blank values', () => {
    expect(formatDisplayDateTime('')).toBe('—')
    expect(formatDisplayDateTime('   ')).toBe('—')
    expect(formatDisplayDateTime(null)).toBe('—')
    expect(formatDisplayDateTime(undefined)).toBe('—')
  })

  it('returns — for invalid values without throwing', () => {
    expect(() => formatDisplayDateTime('not-a-date')).not.toThrow()
    expect(formatDisplayDateTime('not-a-date')).toBe('—')
    expect(formatDisplayDateTime('13/40/2026 99:99')).toBe('—')
    expect(formatDisplayDateTime('2026-99-99T99:99:99Z')).toBe('—')
  })

  it('uses 12-hour AM/PM (midnight and noon)', () => {
    expect(formatDisplayDateTime('1/2/2026 0:00')).toBe('1/2/2026 12:00 AM')
    expect(formatDisplayDateTime('1/2/2026 12:00')).toBe('1/2/2026 12:00 PM')
    expect(formatDisplayDateTime('1/2/2026 12:00 AM')).toBe('1/2/2026 12:00 AM')
    expect(formatDisplayDateTime('1/2/2026 12:00 PM')).toBe('1/2/2026 12:00 PM')
  })

  it('converts UTC ISO wall-clock to local when the timezone offset is non-zero', () => {
    const iso = '2026-09-07T20:42:06Z'
    const dt = new Date(iso)
    const formatted = formatDisplayDateTime(iso)
    expect(formatted).toBe(formatLocalDateTime(dt))

    // Hour/minute in the display must match local getters, not raw UTC string.
    const match = formatted.match(/(\d{1,2}):(\d{2})\s+(AM|PM)$/)
    expect(match).not.toBeNull()
    let displayHour = Number(match![1])
    if (match![3] === 'PM' && displayHour !== 12) displayHour += 12
    if (match![3] === 'AM' && displayHour === 12) displayHour = 0
    expect(displayHour).toBe(dt.getHours())
    expect(Number(match![2])).toBe(dt.getMinutes())

    if (dt.getTimezoneOffset() !== 0) {
      expect(dt.getHours()).not.toBe(dt.getUTCHours())
    }
  })

  it('parses ISO with explicit offset as an absolute instant', () => {
    const withOffset = '2026-09-07T15:42:06-05:00'
    const asZ = '2026-09-07T20:42:06Z'
    expect(parseDisplayDateTime(withOffset)?.getTime()).toBe(
      parseDisplayDateTime(asZ)?.getTime(),
    )
    expect(formatDisplayDateTime(withOffset)).toBe(formatDisplayDateTime(asZ))
  })
})
