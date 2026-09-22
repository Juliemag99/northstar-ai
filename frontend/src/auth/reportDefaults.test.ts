import { describe, expect, it } from 'vitest'
import type { StaffUser } from '../api/auth'
import { authenticatedActorName, defaultReportUserId } from './reportDefaults'

const specialist: StaffUser = {
  id: 42,
  email: 'robertk@n-star.us',
  full_name: 'Robert Kirsten',
  is_administrator: false,
  is_internal_northstar: true,
  active: true,
  created_at: '2026-09-18 10:00:00',
  staff_role: 'revops_specialist',
}

const admin: StaffUser = {
  ...specialist,
  id: 1,
  email: 'juliem@n-star.us',
  full_name: 'Julie Magnani',
  is_administrator: true,
  staff_role: 'system_administrator',
}

describe('defaultReportUserId', () => {
  it('defaults a specialist report to themselves', () => {
    expect(defaultReportUserId(specialist)).toBe(42)
  })

  it('leaves administrator reports on All reps', () => {
    expect(defaultReportUserId(admin)).toBe(0)
  })
})

describe('authenticatedActorName', () => {
  it('uses the signed-in full name', () => {
    expect(authenticatedActorName(specialist)).toBe('Robert Kirsten')
  })
})
