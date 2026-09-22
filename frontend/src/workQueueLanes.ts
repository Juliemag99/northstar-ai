import type { WorkQueueSummaryV2 } from './types/carmeco'

/** Due / follow-up work only. Never includes the New Assignments calling book. */
export function dueWorkCount(
  summary: WorkQueueSummaryV2,
  opportunityCount = 0,
): number {
  return (
    summary.calls_due +
    summary.follow_ups_due +
    summary.appointments +
    summary.hot +
    summary.webleads +
    summary.needs_next_action +
    summary.cross_client_opportunities +
    opportunityCount +
    summary.overdue
  )
}

export const NEW_ASSIGNMENTS_EMPTY =
  'No New Assignments are currently assigned to you.'

export function newAssignmentsEmptyCopy(assignedCount: number): string {
  if (assignedCount > 0) return `${assignedCount} New Assignments are assigned to you.`
  return NEW_ASSIGNMENTS_EMPTY
}
