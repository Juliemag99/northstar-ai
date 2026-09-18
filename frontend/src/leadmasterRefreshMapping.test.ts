import { describe, expect, it } from 'vitest'
import {
  NOT_MAPPED,
  normalizeRefreshMapping,
  validateRefreshMapping,
} from './leadmasterRefreshMapping'

describe('LeadMaster refresh mapping', () => {
  it('allows optional fields to be NOT MAPPED', () => {
    const mapping = normalizeRefreshMapping({
      company_name: 'Company',
      campaign: NOT_MAPPED,
      assigned_rep: '',
      phone: 'Phone',
    })
    expect(mapping).toEqual({ company_name: 'Company', phone: 'Phone' })
    const check = validateRefreshMapping(mapping, ['Company', 'Phone'])
    expect(check.ok).toBe(true)
  })

  it('requires company name', () => {
    const check = validateRefreshMapping({ email: 'Email' }, ['Email'])
    expect(check.ok).toBe(false)
    expect(check.errors[0]).toMatch(/Company name/)
  })
})
