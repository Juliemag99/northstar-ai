import { describe, expect, it } from 'vitest'
import {
  shouldStayOnCompanyAfterCallSave,
  staffCanManageCampaigns,
  workQueueCallShouldRoute,
} from './campaignAccess'
import type { StaffUser } from '../api/auth'

function userWith(permissions: string[] | undefined): StaffUser {
  return {
    id: 1,
    email: 'spec@example.test',
    full_name: 'Spec',
    is_administrator: false,
    is_internal_northstar: true,
    active: true,
    created_at: '',
    permissions,
  }
}

describe('staffCanManageCampaigns', () => {
  it('fails closed when permissions are missing', () => {
    expect(staffCanManageCampaigns(null)).toBe(false)
    expect(staffCanManageCampaigns(undefined)).toBe(false)
    expect(staffCanManageCampaigns(userWith(undefined))).toBe(false)
    expect(staffCanManageCampaigns(userWith([]))).toBe(false)
  })

  it('is true only for campaigns.manage', () => {
    expect(staffCanManageCampaigns(userWith(['campaigns.view', 'crm.edit']))).toBe(false)
    expect(staffCanManageCampaigns(userWith(['campaigns.view', 'campaigns.manage']))).toBe(true)
  })
})

describe('work queue campaign route', () => {
  const specialist = userWith(['campaigns.view', 'crm.edit'])
  const manager = userWith(['campaigns.view', 'campaigns.manage'])

  it('does not route a specialist for Hot Prospect, Qualified, or Appointment Set', () => {
    for (const status of ['Hot Prospect', 'Qualified', 'Appointment Set', 'Appt Set Email Marketing']) {
      expect(workQueueCallShouldRoute(specialist, status, true, 2)).toBe(false)
      expect(shouldStayOnCompanyAfterCallSave(true, false)).toBe(false)
    }
  })

  it('keeps the prompt for a user with campaigns.manage', () => {
    expect(workQueueCallShouldRoute(manager, 'Hot Prospect', true, 2)).toBe(true)
    expect(workQueueCallShouldRoute(manager, 'Qualified', true, 2)).toBe(true)
    expect(workQueueCallShouldRoute(manager, 'Appointment Set', true, 2)).toBe(true)
    expect(shouldStayOnCompanyAfterCallSave(true, true)).toBe(true)
  })

  it('SAVE & NEXT without a route requests the next record', () => {
    const shouldRoute = workQueueCallShouldRoute(specialist, 'Hot Prospect', true, 2)
    expect(shouldStayOnCompanyAfterCallSave(true, shouldRoute)).toBe(false)
  })

  it('Save Call without SAVE & NEXT stays on the company', () => {
    expect(shouldStayOnCompanyAfterCallSave(false, false)).toBe(true)
  })
})
