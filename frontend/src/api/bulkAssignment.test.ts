import { afterEach, describe, expect, it, vi } from 'vitest'
import { confirmBulkAssignment, previewBulkAssignment } from './bulkAssignment'
import { setCsrfToken } from './http'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('bulk assignment API', () => {
  it('posts preview and confirm payloads without fetching every page', async () => {
    setCsrfToken('csrf-ds10')
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            writes: false,
            selection_mode: 'filtered',
            selected_count: 127,
            preview_fingerprint: 'fp',
            confirm_allowed: true,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            changed: 127,
            already_assigned: 0,
            failed: 0,
            message: '127 relationships assigned to Julie Magnani. 0 were already assigned. 0 failed.',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    vi.stubGlobal('fetch', fetchMock)

    await previewBulkAssignment({
      selection_mode: 'filtered',
      filter: { client_id: 4, status: 'New', assigned_user_id: 0 },
      target_user_id: 1,
      reason: 'Unassigned Premier New book',
    })
    await confirmBulkAssignment({
      selection_mode: 'filtered',
      filter: { client_id: 4, status: 'New', assigned_user_id: 0 },
      target_user_id: 1,
      reason: 'Unassigned Premier New book',
      preview_fingerprint: 'fp',
      confirm: true,
    })

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('/api/admin/bulk-assignment/preview')
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain('/api/admin/bulk-assignment/confirm')
    const previewInit = fetchMock.mock.calls[0]?.[1] as RequestInit
    const previewBody = JSON.parse(String(previewInit.body || '{}'))
    expect(previewBody.selection_mode).toBe('filtered')
    expect(previewBody.filter.client_id).toBe(4)
    expect(previewBody.ccr_ids).toBeUndefined()
  })
})
