import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchSharedHistory } from './api/carmeco'

function jsonResponse(body: Record<string, unknown>) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('fetchSharedHistory', () => {
  it('preserves backend shared_company_history_items in the returned object', async () => {
    const sharedEvent = {
      item_key: 'shared-history-18829',
      item_type: 'shared_history_event',
      section: 'shared_company_history',
      legacy_scope: 'shared',
      client_id: 0,
      client_code: '',
      client_name: 'Unattributed shared history',
      external_record_no: '1325886',
      title: 'Shared history · Unattributed shared history',
      body: 'Called about quote',
      event_at: '9-Mar-2026 1:54 PM CDT',
      created_by: 'Julie',
      activity_type: 'Shared history',
      milestone_type: '',
      source_table: 'company_shared_history_events',
      source_id: 18829,
      shared_client_names: [],
    }

    const fetchMock = vi.fn(async () =>
      jsonResponse({
        company_id: 751,
        company_name: 'Altec Industries Inc',
        can_view_cross_client: true,
        filter_client_id: null,
        clients: [],
        items: [sharedEvent],
        northstar_items: [],
        shared_legacy_items: [],
        distinct_legacy_items: [],
        shared_company_history_items: [sharedEvent],
        count: 1,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchSharedHistory('1325886', { client_id: 2 })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/companies/by-record/1325886/shared-history?client_id=2',
      expect.objectContaining({ credentials: 'include' }),
    )
    expect(result.company_id).toBe(751)
    expect(result.shared_company_history_items).toHaveLength(1)
    expect(result.shared_company_history_items?.[0]?.item_key).toBe('shared-history-18829')
    expect(result.shared_company_history_items?.[0]?.section).toBe('shared_company_history')
    expect(result.shared_company_history_items?.[0]?.source_id).toBe(18829)
    expect(result.shared_company_history_items?.[0]?.body).toBe('Called about quote')
  })

  it('falls back to items filtered by shared_company_history section when field missing', async () => {
    const sharedEvent = {
      item_key: 'shared-history-1',
      item_type: 'shared_history_event',
      section: 'shared_company_history',
      legacy_scope: 'shared',
      client_id: 0,
      client_code: '',
      client_name: '',
      external_record_no: '1325886',
      title: 'Note',
      body: 'Fallback body',
      event_at: '',
      created_by: '',
      activity_type: '',
      milestone_type: '',
      source_table: 'company_shared_history_events',
      source_id: 1,
      shared_client_names: [],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          company_id: 751,
          company_name: 'Altec Industries Inc',
          can_view_cross_client: true,
          filter_client_id: null,
          clients: [],
          items: [sharedEvent],
          northstar_items: [],
          shared_legacy_items: [],
          distinct_legacy_items: [],
          count: 1,
        }),
      ),
    )

    const result = await fetchSharedHistory('1325886', { client_id: 2 })
    expect(result.shared_company_history_items).toHaveLength(1)
    expect(result.shared_company_history_items?.[0]?.body).toBe('Fallback body')
  })
})
