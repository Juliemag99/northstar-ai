import { describe, expect, it } from 'vitest'
import {
  dueWorkCount,
  NEW_ASSIGNMENTS_EMPTY,
  newAssignmentsEmptyCopy,
} from './workQueueLanes'
import type { WorkQueueSummaryV2 } from './types/carmeco'

const summary = (overrides: Partial<WorkQueueSummaryV2> = {}): WorkQueueSummaryV2 => ({
  calls_due: 0,
  follow_ups_due: 0,
  appointments: 0,
  hot: 0,
  webleads: 0,
  new_assignments: 0,
  needs_next_action: 0,
  cross_client_opportunities: 0,
  overdue: 0,
  ...overrides,
})

describe('dueWorkCount', () => {
  it('stays separate from New Assignments when appointments exist', () => {
    expect(
      dueWorkCount(
        summary({
          appointments: 4,
          new_assignments: 838,
        }),
      ),
    ).toBe(4)
  })

  it('does not hide the New Assignments count in due work', () => {
    const due = dueWorkCount(summary({ calls_due: 2, new_assignments: 126 }))
    expect(due).toBe(2)
    expect(due).not.toBe(126)
  })
})

describe('newAssignmentsEmptyCopy', () => {
  it('is assignment-aware when the specialist has no assigned New rows', () => {
    expect(newAssignmentsEmptyCopy(0)).toBe(NEW_ASSIGNMENTS_EMPTY)
    expect(newAssignmentsEmptyCopy(0)).not.toMatch(/838|1243|client/i)
  })
})
