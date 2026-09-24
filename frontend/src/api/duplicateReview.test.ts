import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  fetchDuplicateCandidates,
  fetchDuplicatePair,
  saveDuplicateReview,
  analyzeDuplicateCandidates,
  fetchMergePlans,
  prepareMergePlans,
  saveMergePlanDecision,
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
    expect(url).toContain('queue=unreviewed')
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

  it('analyzes candidates without implying merge', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        ok: true,
        candidates_analyzed: 12,
        merge_will_occur: false,
        approval_created: false,
        buckets: { HIGH_CONFIDENCE_DUPLICATE: 3 },
      }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const result = await analyzeDuplicateCandidates()
    expect(result.merge_will_occur).toBe(false)
    expect(String(fetchMock.mock.calls[0]?.[0] || '')).toContain(
      '/api/admin/duplicate-review/analyze',
    )
  })

  it('prepares and loads merge plans without creating approvals', async () => {
    fetchMock
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ok: true,
          analyzed: 16,
          merge_will_occur: false,
          approval_created: false,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          planning_only: true,
          merge_will_occur: false,
          approval_created: false,
          plans: [],
          summary: { ready_for_review: 0 },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ok: true,
          approval_created: false,
          merge_will_occur: false,
          plan: { plan_state: 'READY_FOR_HUMAN_APPROVAL' },
        }),
      })
    vi.stubGlobal('fetch', fetchMock)
    const prepared = await prepareMergePlans()
    expect(prepared.approval_created).toBe(false)
    expect(String(fetchMock.mock.calls[0]?.[0] || '')).toContain(
      '/api/admin/duplicate-review/merge-plans/prepare',
    )
    await fetchMergePlans({ state: 'NEEDS_EXCEPTION_DECISION', decision_type: 'survivor', same_client: 'yes' })
    expect(String(fetchMock.mock.calls[1]?.[0] || '')).toContain(
      '/api/admin/duplicate-review/merge-plans',
    )
    expect(String(fetchMock.mock.calls[1]?.[0] || '')).toContain('decision_type=survivor')
    expect(String(fetchMock.mock.calls[1]?.[0] || '')).not.toContain('client_id=')
    const saved = await saveMergePlanDecision(22, 28, {
      exception_key: 'STATUS_DECISION_REQUIRED:1',
      chosen_resolution: 'KEEP_SURVIVOR',
    })
    expect(saved.approval_created).toBe(false)
    expect(String(fetchMock.mock.calls[2]?.[0] || '')).toContain(
      '/api/admin/duplicate-review/pairs/22/28/merge-plan/decisions',
    )
  })
})
