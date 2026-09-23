import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ProspectBulkAssign, {
  emptySelection,
  isRowSelected,
  pageAllSelected,
  prospectFilterKey,
  selectAllFiltered,
  selectCurrentPage,
  selectedCount,
  toggleRow,
  visibleCcrIds,
} from './ProspectBulkAssign'
import * as bulkAssignment from './api/bulkAssignment'
import type { ProspectListItem } from './types/carmeco'

vi.mock('./api/bulkAssignment', () => ({
  fetchBulkAssignees: vi.fn(),
  previewBulkAssignment: vi.fn(),
  confirmBulkAssignment: vi.fn(),
}))

function row(id: number, extras: Partial<ProspectListItem> = {}): ProspectListItem {
  return {
    id,
    external_record_no: `RN-${id}`,
    company: `Co ${id}`,
    city: '',
    state: '',
    status: 'New',
    relationship_status: 'New',
    primary_contact: '',
    phone: '',
    last_updated: '',
    website: '',
    address: '',
    zip: '',
    customer_campaign: '',
    contact_count: 0,
    is_hot: false,
    has_appointment_set: false,
    has_quote: false,
    has_purchase_order: false,
    has_weblead: false,
    client_id: 4,
    client_code: 'premier',
    client_name: 'Premier',
    relationship_id: id,
    ...extras,
  }
}

const filter = {
  clientId: 4,
  q: '',
  status: 'New',
  milestoneType: '',
  assignedUserId: null as number | null,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Prospect bulk selection helpers', () => {
  it('selects the current page, all filtered results, and clears', () => {
    const rows = [row(1), row(2)]
    const page = selectCurrentPage(rows, 127)
    expect(visibleCcrIds(rows)).toEqual([1, 2])
    expect(selectedCount(page)).toBe(2)
    expect(pageAllSelected(page, rows)).toBe(true)
    const all = selectAllFiltered(127)
    expect(selectedCount(all)).toBe(127)
    expect(all.mode).toBe('filtered')
    expect(isRowSelected(all, 99)).toBe(true)
    const cleared = emptySelection(127)
    expect(selectedCount(cleared)).toBe(0)
  })

  it('invalidates filtered selection when the filter key changes', () => {
    const a = prospectFilterKey(filter)
    const b = prospectFilterKey({ ...filter, q: "O'Reilly" })
    const c = prospectFilterKey({ ...filter, assignedUserId: 0 })
    expect(a).not.toBe(b)
    expect(a).not.toBe(c)
  })

  it('keeps explicit ids across pages and unchecking a filtered row downgrades to the page', () => {
    const page1 = selectCurrentPage([row(1), row(2)], 127)
    const withPage2 = {
      ...page1,
      ccrIds: [...page1.ccrIds, 51],
    }
    expect(selectedCount(withPage2)).toBe(3)
    const filtered = selectAllFiltered(127)
    const afterUncheck = toggleRow(filtered, 2, [1, 2])
    expect(afterUncheck.mode).toBe('explicit')
    expect(afterUncheck.ccrIds).toEqual([1])
  })
})

describe('Prospect Assign to Rep UI', () => {
  it('previews reassignment warning and confirms assignment', async () => {
    vi.mocked(bulkAssignment.fetchBulkAssignees).mockResolvedValue([
      {
        user_id: 9,
        email: 'robert@example.test',
        full_name: 'Robert Isolated',
        is_administrator: false,
      },
    ])
    vi.mocked(bulkAssignment.previewBulkAssignment).mockResolvedValue({
      ok: true,
      writes: false,
      selection_mode: 'filtered',
      client: { client_id: 4, code: 'premier', name: 'Premier' },
      target: { user_id: 9, full_name: 'Robert Isolated', email: 'robert@example.test' },
      reason: 'Premier pilot book assignment',
      filtered_result_count: 675,
      selected_count: 675,
      eligible: 675,
      new_assignments: 70,
      reassignments: 20,
      already_assigned: 10,
      inactive_removed: 0,
      archived_company: 0,
      invalid_missing: 0,
      mixed_client: false,
      unauthorized_target: false,
      confirm_allowed: true,
      block_code: '',
      hot_count: 4,
      follow_up_count: 12,
      appointment_set_count: 2,
      preview_fingerprint: 'abc',
      proposed: 'Assign 90 relationships to Robert Isolated',
      current_assignment_summary: [],
    })
    vi.mocked(bulkAssignment.confirmBulkAssignment).mockResolvedValue({
      ok: true,
      selected: 675,
      eligible: 675,
      changed: 90,
      new_assignments: 70,
      reassigned: 20,
      already_assigned: 10,
      skipped: 0,
      failed: 0,
      message: '90 relationships assigned to Robert Isolated. 10 were already assigned. 0 failed.',
      target: { user_id: 9, full_name: 'Robert Isolated' },
    })
    const onAssigned = vi.fn()
    const onSelectionChange = vi.fn()
    render(
      <ProspectBulkAssign
        enabled
        clientId={4}
        clientName="Premier"
        rows={[row(1), row(2)]}
        filteredTotal={675}
        filter={filter}
        selection={selectAllFiltered(675)}
        onSelectionChange={onSelectionChange}
        onAssigned={onAssigned}
      />,
    )
    expect(screen.getByText(/All 675 filtered records selected/i)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Bulk Actions'), { target: { value: 'assign' } })
    const assignee = await screen.findByLabelText('Assignee')
    fireEvent.change(assignee, { target: { value: '9' } })
    fireEvent.change(screen.getByLabelText('Assignment reason'), {
      target: { value: 'Premier pilot book assignment' },
    })
    await waitFor(() => {
      expect((screen.getByLabelText('Assignee') as HTMLSelectElement).value).toBe('9')
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => {
      expect(screen.getByText(/New assignments:\s*70/i)).toBeTruthy()
    })
    expect(screen.getByRole('alert').textContent || '').toMatch(/Reassignments: 20/)
    expect(screen.getByText(/Already assigned: 10/i)).toBeTruthy()
    expect(screen.getByText(/Hot: 4/i)).toBeTruthy()
    expect(screen.getByText(/Follow-ups: 12/i)).toBeTruthy()
    expect(screen.getByText(/Appointment Set: 2/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Assignment' }))
    await waitFor(() => {
      expect(onAssigned).toHaveBeenCalled()
    })
    expect(onSelectionChange).toHaveBeenCalledWith(emptySelection(675))
    expect(screen.getByText(/90 relationships assigned to Robert Isolated/i)).toBeTruthy()
  })

  it('hides bulk assign for specialists', () => {
    render(
      <ProspectBulkAssign
        enabled={false}
        clientId={4}
        clientName="Premier"
        rows={[row(1)]}
        filteredTotal={1}
        filter={filter}
        selection={emptySelection(1)}
        onSelectionChange={vi.fn()}
        onAssigned={vi.fn()}
      />,
    )
    expect(screen.queryByLabelText('Bulk Actions')).toBeNull()
  })
})
