import { describe, expect, it } from 'vitest'
import type { StaffUser } from '../api/auth'
import { staffCanAdminister } from './staffCanAdminister'

const julieFallback: StaffUser = {
  id: 1,
  email: 'juliem@n-star.us',
  full_name: 'Julie Magnani',
  is_administrator: true,
  is_internal_northstar: true,
  active: true,
  created_at: '2026-08-07 16:18:04',
}

describe('staffCanAdminister', () => {
  it('does not treat the signed-out Julie fallback payload as an administrator', () => {
    expect(staffCanAdminister(false, julieFallback)).toBe(false)
    expect(staffCanAdminister(false, null)).toBe(false)
  })

  it('rejects an authenticated non-administrator', () => {
    expect(
      staffCanAdminister(true, {
        ...julieFallback,
        id: 9,
        email: 'rep@example.test',
        is_administrator: false,
      }),
    ).toBe(false)
  })

  it('rejects an inactive administrator session user', () => {
    expect(staffCanAdminister(true, { ...julieFallback, active: false })).toBe(false)
  })

  it('allows an authenticated active administrator', () => {
    expect(staffCanAdminister(true, julieFallback)).toBe(true)
  })
})
