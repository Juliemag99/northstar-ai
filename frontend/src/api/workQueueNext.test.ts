import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchWorkQueueNext } from './carmeco'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('fetchWorkQueueNext', () => {
  it('requests /api/work-queue/next with after id, cursor, and filters (no limit/offset workaround)', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          user_id: 1,
          mode: 'selected_client',
          client_ids: [2],
          after_queue_item_id: 'hot:hot:10:2',
          has_next: true,
          end_of_results: false,
          total: 70,
          position: 51,
          item: {
            queue_item_id: 'hot:hot:99:2',
            work_type: 'Hot',
            work_priority: 80,
            priority_label: 'High',
            client_id: 2,
            company_id: 500,
            relationship_id: 99,
            company_name: 'Beyond Page Co',
            external_record_no: 'NS-500',
          },
          message: '',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchWorkQueueNext({
      after_queue_item_id: 'hot:hot:10:2',
      after_work_priority: 80,
      after_company_name: 'Page Boundary Co',
      after_company_id: 400,
      client_id: 2,
      type: 'hot',
      due: 'today',
      ai_alignment: 'Aligned',
    })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('/api/work-queue/next?')
    expect(url).toContain('after_queue_item_id=hot%3Ahot%3A10%3A2')
    expect(url).toContain('after_work_priority=80')
    expect(url).toContain('after_company_id=400')
    expect(url).toContain('client_id=2')
    expect(url).toContain('type=hot')
    expect(url).toContain('due=today')
    expect(url).toContain('ai_alignment=Aligned')
    expect(url).not.toContain('limit=')
    expect(url).not.toContain('offset=')

    expect(result.has_next).toBe(true)
    expect(result.end_of_results).toBe(false)
    expect(result.position).toBe(51)
    expect(result.item?.company_name).toBe('Beyond Page Co')
  })

  it('normalizes end-of-results with null item', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            after_queue_item_id: 'hot:hot:1:2',
            has_next: false,
            end_of_results: true,
            total: 1,
            position: null,
            item: null,
            message: 'No further matching Work Queue items.',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    const result = await fetchWorkQueueNext({
      after_queue_item_id: 'hot:hot:1:2',
      client_id: 2,
      type: 'hot',
    })
    expect(result.has_next).toBe(false)
    expect(result.end_of_results).toBe(true)
    expect(result.item).toBeNull()
    expect(result.message).toContain('No further')
  })

  it('passes quick filters hot/weblead/cross_client/overdue with list-API parity', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          has_next: false,
          end_of_results: true,
          total: 0,
          item: null,
          message: 'No further matching Work Queue items.',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    await fetchWorkQueueNext({
      after_queue_item_id: 'hot:hot:1:2',
      client_id: 2,
      hot: true,
      weblead: true,
      cross_client: true,
      overdue: true,
      due: 'overdue',
    })
    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('hot=true')
    expect(url).toContain('weblead=true')
    expect(url).toContain('cross_client=true')
    expect(url).toContain('overdue=true')
    expect(url).toContain('due=overdue')
  })

  it('passes every ai_* filter', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          has_next: false,
          end_of_results: true,
          total: 0,
          item: null,
          message: 'No further matching Work Queue items.',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    await fetchWorkQueueNext({
      after_queue_item_id: 'hot:hot:1:2',
      client_id: 2,
      ai_alignment: 'Review',
      ai_recommendation: 'Hold',
      ai_fit: 'Weak Fit',
      ai_engagement: 'Cold',
    })
    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('ai_alignment=Review')
    expect(url).toContain('ai_recommendation=Hold')
    expect(url).toContain('ai_fit=Weak+Fit')
    expect(url).toContain('ai_engagement=Cold')
  })
})
