import { describe, expect, it } from 'vitest'
import {
  APPOINTMENTS_SET_SUBTITLE,
  revenueOpportunityCountsFromSummary,
} from './revenueOpportunityKpis'

describe('revenueOpportunityKpis', () => {
  it('maps full-client milestone summary into card counts', () => {
    const counts = revenueOpportunityCountsFromSummary({
      appointments_set: 53,
      quotes: 26,
      purchase_orders: 0,
      webleads: 0,
    })
    expect(counts).toEqual({
      appointments_set: 53,
      quotes: 26,
      purchase_orders: 0,
      webleads: 0,
    })
  })

  it('returns null when summary is missing so cards can fall back', () => {
    expect(revenueOpportunityCountsFromSummary(null)).toBeNull()
    expect(revenueOpportunityCountsFromSummary(undefined)).toBeNull()
  })

  it('clarifies Appointments Set is not native-table-only', () => {
    expect(APPOINTMENTS_SET_SUBTITLE.toLowerCase()).toContain('imported')
    expect(APPOINTMENTS_SET_SUBTITLE.toLowerCase()).not.toContain('live appointment records')
  })
})
