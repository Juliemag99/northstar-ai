import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchProspects } from './carmeco'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Companies page prospects visibility API', () => {
  it('requests server-side q/limit/offset so Valmont beyond the first 100 is searchable', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          client: {
            name: 'Brown Industries',
            client_id: 2,
            code: 'brown',
            prospect_count: 106,
            company_count: 106,
            contact_count: 0,
            seed_file: 'database/northstar.db',
            mode: 'selected_client',
          },
          prospects: [
            {
              id: 541,
              external_record_no: 'NS-541',
              company: 'Valmont',
              city: '',
              state: '',
              status: 'Left Message',
              relationship_status: 'Left Message',
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
              next_action: '',
              follow_up_date: null,
              call_due: false,
              follow_up_due: false,
              client_id: 2,
              client_code: 'brown',
              client_name: 'Brown Industries',
              relationship_id: 999,
            },
          ],
          total: 1,
          client_total: 106,
          offset: 0,
          limit: 50,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const page = await fetchProspects({
      client_id: 2,
      q: 'Valmont',
      limit: 50,
      offset: 0,
    })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('/api/prospects?')
    expect(url).toContain('client_id=2')
    expect(url).toContain('q=Valmont')
    expect(url).toContain('limit=50')
    // offset=0 is omitted by design (API default)
    expect(url).not.toContain('offset=')

    expect(page.client_total).toBe(106)
    expect(page.total).toBe(1)
    expect(page.limit).toBe(50)
    expect(page.prospects).toHaveLength(1)
    expect(page.prospects[0]?.id).toBe(541)
    expect(page.prospects[0]?.company).toBe('Valmont')
  })

  it('passes milestone_type so Quotes/PO/WebLead filters are server-side', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          client: { name: 'Brown', client_id: 2, code: 'brown', mode: 'selected_client' },
          prospects: [{ id: 282, company: 'Colamark', client_id: 2, has_quote: true }],
          total: 26,
          client_total: 106,
          offset: 0,
          limit: 50,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const page = await fetchProspects({
      client_id: 2,
      milestone_type: 'Quote',
      limit: 50,
      offset: 0,
    })

    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('milestone_type=Quote')
    expect(url).toContain('client_id=2')
    expect(page.total).toBe(26)
  })

  it('passes a non-zero offset so rows past the first page are reachable', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          client: { name: 'Brown', client_id: 2, code: 'brown', mode: 'selected_client' },
          prospects: [{ id: 541, company: 'Valmont', client_id: 2 }],
          total: 106,
          client_total: 106,
          offset: 100,
          limit: 100,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const page = await fetchProspects({
      client_id: 2,
      limit: 100,
      offset: 100,
    })

    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('limit=100')
    expect(url).toContain('offset=100')
    expect(page.offset).toBe(100)
    expect(page.prospects.some((p) => p.id === 541 && p.company === 'Valmont')).toBe(true)
  })

  it('preserves client_statuses chips for All My Clients responses', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          client: {
            name: 'All My Clients',
            client_id: 0,
            code: '',
            mode: 'all_my_clients',
          },
          prospects: [
            {
              id: 100,
              company: 'Multi Status Co',
              status: 'New',
              client_statuses: [
                {
                  client_id: 1,
                  client_code: 'carmeco',
                  client_name: 'Carmeco',
                  status: 'Qualified',
                  relationship_id: 10,
                },
                {
                  client_id: 3,
                  client_code: 'dawson',
                  client_name: 'Dawson Fabrication',
                  status: 'New',
                  relationship_id: 11,
                },
              ],
            },
          ],
          total: 697,
          client_total: 697,
          offset: 0,
          limit: 50,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const page = await fetchProspects({ all_clients: true, limit: 50 })
    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('all_clients=true')
    expect(url).not.toContain('client_id=')
    expect(page.total).toBe(697)
    expect(page.prospects[0]?.client_statuses).toEqual([
      {
        client_id: 1,
        client_code: 'carmeco',
        client_name: 'Carmeco',
        status: 'Qualified',
        relationship_id: 10,
      },
      {
        client_id: 3,
        client_code: 'dawson',
        client_name: 'Dawson Fabrication',
        status: 'New',
        relationship_id: 11,
      },
    ])
  })

  it('always sends a bounded limit even when callers omit limit', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          client: { name: 'Brown', client_id: 2, code: 'brown', mode: 'selected_client' },
          prospects: [],
          total: 0,
          client_total: 0,
          offset: 0,
          limit: 50,
          has_previous: false,
          has_next: false,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    await fetchProspects({ client_id: 2 })
    const url = String(fetchMock.mock.calls[0]?.[0] ?? '')
    expect(url).toContain('limit=50')
    expect(url).not.toMatch(/limit=500|limit=1000/)
  })
})
