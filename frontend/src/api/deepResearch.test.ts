import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  cancelResearchJob,
  fetchDeepResearchStatus,
  fetchResearchJob,
  startCompanyResearch,
} from './carmeco'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Deep Research API client', () => {
  it('posts research_depth=deep with confirm_paid_refresh and never includes API keys', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          company_id: 541,
          company_name: 'Valmont',
          research_depth: 'deep',
          deep_research_usage: {
            model: 'gpt-5.5',
            web_search_call_count: 3,
            input_tokens: 10,
            output_tokens: 20,
            total_tokens: 30,
            estimated_cost_usd: null,
            cost_estimate_status: 'unavailable',
          },
          job: {
            job_id: 9,
            company_id: 541,
            working_for_client_id: 2,
            status: 'queued',
            progress: 0,
            progress_message: 'Queued',
            citations: [],
            sources: [],
            deep_research_configured: true,
            usage: {},
          },
          read_only_crm: true,
          overview: [],
          locations: [],
          products: [],
          capabilities: [],
          industries: [],
          sources: [],
          citations: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await startCompanyResearch({
      company_id: 541,
      working_for_client_id: 2,
      force_refresh: true,
      confirm_paid_refresh: true,
      research_depth: 'deep',
    })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const body = JSON.parse(String(init.body || '{}')) as Record<string, unknown>
    expect(body.research_depth).toBe('deep')
    expect(body.confirm_paid_refresh).toBe(true)
    expect(JSON.stringify(body)).not.toMatch(/api[_-]?key/i)
    expect(result.research_depth).toBe('deep')
    expect(result.job?.job_id).toBe(9)
    expect(result.deep_research_usage?.cost_estimate_status).toBe('unavailable')
  })

  it('polls and cancels deep research jobs by id', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            company_id: 541,
            company_name: 'Valmont',
            research_depth: 'deep',
            job: { job_id: 9, status: 'running', progress: 40, citations: [] },
            read_only_crm: true,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            company_id: 541,
            company_name: 'Valmont',
            research_depth: 'deep',
            job: { job_id: 9, status: 'cancelled', progress: 40, citations: [] },
            read_only_crm: true,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    vi.stubGlobal('fetch', fetchMock)

    const polled = await fetchResearchJob(9)
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('/api/research/jobs/9')
    expect(polled.job?.status).toBe('running')

    const cancelled = await cancelResearchJob(9)
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain('/api/research/jobs/9/cancel')
    expect(cancelled.job?.status).toBe('cancelled')
  })

  it('reports deep research status with limits and no secrets', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          deep_research_configured: true,
          limits: {
            max_web_search_calls: 10,
            max_output_tokens: 6000,
            max_attempts: 2,
            dollar_limits_enforceable: false,
            estimated_max_run_usd: null,
            estimated_max_run_status: 'unavailable',
            pilot_admin_only: true,
          },
          pricing: { estimate_available: false, note: 'Exact cost unavailable' },
          monthly: {
            spent_usd: null,
            remaining_usd: null,
            monthly_limit_usd: 20,
            status: 'unavailable',
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const status = await fetchDeepResearchStatus()
    expect(status.deep_research_configured).toBe(true)
    expect(status.limits?.max_web_search_calls).toBe(10)
    expect(status.limits?.max_output_tokens).toBe(6000)
    expect(status.pricing?.estimate_available).toBe(false)
    expect(JSON.stringify(status)).not.toMatch(/sk-|Bearer|api_key/i)
  })
})
