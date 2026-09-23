import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  fetchDuplicateCandidates,
  fetchDuplicatePair,
  saveDuplicateReview,
} from './duplicateReview'

const fetchMock = vi.fn()

afterEach(() => {
  fetchMock.mockReset()
  vi.unstubAllGlobals()
})

describe('duplicateReview API', () => {
  it('loads paginated candidates with disposition and search', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        planning_only: true,
        merge_will_occur: false,
        total: 2,
        offset: 0,
        limit: 50,
        pairs: [],
      }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await fetchDuplicateCandidates({ disposition: 'unreviewed', q: 'EDL', offset: 0, limit: 50 })
    const url = String(fetchMock.mock.calls[0]?.[0] || '')
    expect(url).toContain('/api/admin/duplicate-review/candidates')
    expect(url).toContain('disposition=unreviewed')
    expect(url).toContain('q=EDL')
  })

  it('loads pair detail and saves a plan-only review', async () => {
    fetchMock
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          planning_only: true,
          merge_will_occur: false,
          pair_key: '22:28',
          no_merge_button: true,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ok: true,
          planning_only: true,
          merge_will_occur: false,
          approval_created: false,
          disposition: 'NOT_DUPLICATE',
        }),
      })
    vi.stubGlobal('fetch', fetchMock)
    const detail = await fetchDuplicatePair(28, 22)
    expect(detail.merge_will_occur).toBe(false)
    const saved = await saveDuplicateReview(22, 28, {
      disposition: 'NOT_DUPLICATE',
      reason: 'Different companies with similar names.',
    })
    expect(saved.approval_created).toBe(false)
    expect(String(fetchMock.mock.calls[1]?.[0] || '')).toContain(
      '/api/admin/duplicate-review/pairs/22/28',
    )
  })
})
