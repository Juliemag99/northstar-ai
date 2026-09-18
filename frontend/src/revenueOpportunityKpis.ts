import type { MilestoneSummary } from './types/carmeco'

/** Full-client Revenue & Opportunities counts from /api/milestones/summary. */
export type RevenueOpportunityCounts = Pick<
  MilestoneSummary,
  'appointments_set' | 'quotes' | 'purchase_orders' | 'webleads'
>

export function revenueOpportunityCountsFromSummary(
  summary: RevenueOpportunityCounts | null | undefined,
): RevenueOpportunityCounts | null {
  if (summary == null) return null
  return {
    appointments_set: Number(summary.appointments_set) || 0,
    quotes: Number(summary.quotes) || 0,
    purchase_orders: Number(summary.purchase_orders) || 0,
    webleads: Number(summary.webleads) || 0,
  }
}

export const APPOINTMENTS_SET_SUBTITLE =
  'Scheduled appointments (native + imported)'
