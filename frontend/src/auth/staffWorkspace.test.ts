import { describe, expect, it } from 'vitest'
import type { StaffUser } from '../api/auth'
import {
  dashboardStartHereMessage,
  isSingleAssignedClient,
  resolveActiveClientId,
  staffNavItemVisible,
  staffRoleLabel,
} from './staffWorkspace'

const specialist: StaffUser = {
  id: 9,
  email: 'robertk@n-star.us',
  full_name: 'Robert Kirsten',
  is_administrator: false,
  is_internal_northstar: true,
  active: true,
  created_at: '2026-09-18 10:00:00',
  staff_role: 'revops_specialist',
}

describe('staffRoleLabel', () => {
  it('labels a signed-in RevOps specialist', () => {
    expect(staffRoleLabel(true, specialist)).toBe('RevOps Specialist')
  })

  it('does not use Account Executive', () => {
    expect(staffRoleLabel(true, specialist)).not.toMatch(/Account Executive/i)
  })

  it('falls back for signed-out sessions', () => {
    expect(staffRoleLabel(false, specialist)).toBe('Staff')
  })
})

describe('staffNavItemVisible', () => {
  it('hides Administration and Clients from specialists', () => {
    expect(staffNavItemVisible('administration', false)).toBe(false)
    expect(staffNavItemVisible('clients', false)).toBe(false)
    expect(staffNavItemVisible('dashboard', false)).toBe(true)
    expect(staffNavItemVisible('work-queue', false)).toBe(true)
    expect(staffNavItemVisible('contacts', false)).toBe(true)
  })

  it('shows Administration and Clients to administrators', () => {
    expect(staffNavItemVisible('administration', true)).toBe(true)
    expect(staffNavItemVisible('clients', true)).toBe(true)
  })
})

describe('resolveActiveClientId', () => {
  it('pins a single assigned client even when storage is All My Clients', () => {
    expect(
      resolveActiveClientId({ availableClientIds: [2], storedOrCurrent: 0 }),
    ).toBe(2)
  })

  it('keeps All My Clients when the user has more than one client', () => {
    expect(
      resolveActiveClientId({ availableClientIds: [2, 3], storedOrCurrent: 0 }),
    ).toBe(0)
  })

  it('falls back to the first assigned client when storage is another client', () => {
    expect(
      resolveActiveClientId({ availableClientIds: [2], storedOrCurrent: 1 }),
    ).toBe(2)
  })
})

describe('dashboardStartHereMessage', () => {
  it('points to New Assignments when due work is empty', () => {
    expect(
      dashboardStartHereMessage({
        clientName: 'Brown Industries',
        newAssignments: 838,
        callsDue: 0,
        followUpsDueToday: 0,
        overdueFollowUps: 0,
      }),
    ).toContain('New Assignments (838)')
  })

  it('stays silent when due work already exists', () => {
    expect(
      dashboardStartHereMessage({
        clientName: 'Brown Industries',
        newAssignments: 838,
        callsDue: 2,
        followUpsDueToday: 0,
        overdueFollowUps: 0,
      }),
    ).toBeNull()
  })
})

describe('isSingleAssignedClient', () => {
  it('is true only for exactly one assigned client', () => {
    expect(isSingleAssignedClient(1)).toBe(true)
    expect(isSingleAssignedClient(0)).toBe(false)
    expect(isSingleAssignedClient(2)).toBe(false)
  })
})
