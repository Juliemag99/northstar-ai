import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdministrationDuplicateReview from './AdministrationDuplicateReview'
import * as duplicateReview from './api/duplicateReview'

vi.mock('./api/duplicateReview', () => ({
  fetchDuplicateCandidates: vi.fn(),
  fetchDuplicatePair: vi.fn(),
  fetchDuplicateReviewHistory: vi.fn(),
  saveDuplicateReview: vi.fn(),
}))

const candidate = {
  company_a_id: 22,
  company_b_id: 28,
  pair_key: '22:28',
  company_a: {
    company_id: 22,
    company_name: 'Edl Packaging Engineers',
    city: 'Green Bay',
    state: 'WI',
    phone: '(920) 347-0143',
    website: '',
    archived: false,
  },
  company_b: {
    company_id: 28,
    company_name: 'Edl Packaging Engineers',
    city: 'Green Bay',
    state: 'WI',
    phone: '(920) 336-7744',
    website: 'Edlpackaging.Com',
    archived: false,
  },
  categories: ['EXACT_NAME_ADDRESS'],
  evidence_summary: 'Possible duplicate because normalized name and address match. Julie decides the disposition.',
  disposition: 'UNREVIEWED',
  review_status: 'UNREVIEWED',
  stale: false,
  planning_only: true,
  merge_will_occur: false,
}

const detail = {
  planning_only: true,
  merge_will_occur: false,
  automatic_verdict: false,
  writes: false,
  pair_key: '22:28',
  company_a: {
    company_id: 22,
    company_name: 'Edl Packaging Engineers',
    address: '1 Main',
    city: 'Green Bay',
    state: 'WI',
    zip: '54301',
    phone: '(920) 347-0143',
    phone_extension: '',
    website: '',
    external_record_no: '99202',
    archived: false,
    ccrs: [
      {
        ccr_id: 22,
        client_id: 1,
        client_name: 'Carmeco',
        status: 'Left Message',
        assigned_user_name: 'Julie Magnani',
        hot: false,
        active: true,
      },
    ],
    aliases: [{ alias_name: 'EDL' }],
    identities: [{ source_system: 'leadmaster', source_record_no: '99202' }],
    locations: [{ address: '1 Main', city: 'Green Bay', state: 'WI' }],
    contacts: [{ id: 9, first_name: 'Pat', last_name: 'Lee', email: 'pat@edl.example', phone: '9205550100' }],
    campaigns: [{ client_name: 'Carmeco', campaign_name: 'Packaging' }],
    history: { notes: 1, activities: 2, follow_ups: 0, appointments: 0, research_records: 0, campaigns: 1, sales_events: 0 },
    planning_factors: { active_ccr_count: 1, contact_count: 1 },
  },
  company_b: {
    company_id: 28,
    company_name: 'Edl Packaging Engineers',
    address: '1 Main',
    city: 'Green Bay',
    state: 'WI',
    zip: '54301',
    phone: '(920) 336-7744',
    website: 'Edlpackaging.Com',
    external_record_no: '101620',
    archived: false,
    ccrs: [
      {
        ccr_id: 28,
        client_id: 1,
        client_name: 'Carmeco',
        status: 'New',
        assigned_user_name: 'Julie Magnani',
        hot: true,
        active: true,
      },
    ],
    aliases: [],
    identities: [{ source_system: 'leadmaster', source_record_no: '101620' }],
    locations: [],
    contacts: [{ id: 10, first_name: 'Pat', last_name: 'Lee', email: 'pat@edl.example', phone: '9205550100' }],
    campaigns: [],
    history: { notes: 0, activities: 1, follow_ups: 1, appointments: 0, research_records: 0, campaigns: 0, sales_events: 0 },
    planning_factors: { active_ccr_count: 1, contact_count: 1 },
  },
  categories: ['EXACT_NAME_ADDRESS'],
  evidence_summary: 'Possible duplicate because normalized name and address match. Julie decides the disposition.',
  same_client_conflicts: [{ client_name: 'Carmeco' }],
  contact_overlaps: [{ kind: 'exact_email', name_a: 'Pat Lee', name_b: 'Pat Lee' }],
  dependency: { overall: 'CONFLICT', labels: ['CONFLICT'] },
  merge_plan: null,
  review: {
    disposition: 'UNREVIEWED',
    reason: '',
    stale: false,
    review_status: 'UNREVIEWED',
    evidence_fingerprint: 'abc',
  },
  plan_only_warning: 'This records a merge plan only. No records will be merged.',
  no_merge_button: true,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Duplicate Review workspace', () => {
  it('lists candidates, opens side-by-side detail, and saves a plan-only review', async () => {
    vi.mocked(duplicateReview.fetchDuplicateCandidates).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      automatic_verdict: false,
      writes: false,
      total: 1,
      offset: 0,
      limit: 50,
      pairs: [candidate],
    })
    vi.mocked(duplicateReview.fetchDuplicatePair).mockResolvedValue(detail)
    vi.mocked(duplicateReview.fetchDuplicateReviewHistory).mockResolvedValue({ events: [] })
    vi.mocked(duplicateReview.saveDuplicateReview).mockResolvedValue({
      ok: true,
      planning_only: true,
      merge_will_occur: false,
      approval_created: false,
      plan_only_warning: detail.plan_only_warning,
      disposition: 'NOT_DUPLICATE',
      reason: 'Different companies with similar names.',
    })

    render(<AdministrationDuplicateReview />)
    expect(screen.getByRole('heading', { name: 'Duplicate Review' })).toBeTruthy()
    expect(screen.getByText(/REVIEW \/ PLAN ONLY/)).toBeTruthy()
    await waitFor(() => expect(screen.getAllByText('Edl Packaging Engineers').length).toBeGreaterThan(0))
    expect(screen.getByText('EXACT_NAME_ADDRESS')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Merge/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Execute/i })).toBeNull()

    fireEvent.click(screen.getAllByRole('button', { name: 'Edl Packaging Engineers' })[0])
    await waitFor(() => expect(screen.getByText('MASTER COMPANY A')).toBeTruthy())
    expect(screen.getByText('MASTER COMPANY B')).toBeTruthy()
    expect(screen.getByText('22')).toBeTruthy()
    expect(screen.getByText('28')).toBeTruthy()
    expect(screen.getAllByText(/SAME-CLIENT CCR CONFLICT/).length).toBeGreaterThan(0)
    expect(screen.getByText(/exact_email/)).toBeTruthy()
    expect(screen.getByLabelText('Not Duplicate')).toBeTruthy()
    expect(screen.getByLabelText('Multi-Location')).toBeTruthy()
    expect(screen.getByLabelText('Needs Research')).toBeTruthy()
    expect(screen.getByLabelText('Likely Duplicate')).toBeTruthy()
    expect(screen.getByLabelText('Merge Candidate')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Not Duplicate'))
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))
    expect(screen.getByText('A short reason is required.')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Reason'), {
      target: { value: 'Different companies with similar names.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))
    await waitFor(() =>
      expect(duplicateReview.saveDuplicateReview).toHaveBeenCalledWith(
        22,
        28,
        expect.objectContaining({ disposition: 'NOT_DUPLICATE' }),
      ),
    )
  })

  it('requires survivor selection and shows the merge-plan-only warning', async () => {
    vi.mocked(duplicateReview.fetchDuplicateCandidates).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      automatic_verdict: false,
      writes: false,
      total: 1,
      offset: 0,
      limit: 50,
      pairs: [{ ...candidate, stale: true, review_status: 'RE_REVIEW_NEEDED' }],
    })
    vi.mocked(duplicateReview.fetchDuplicatePair).mockResolvedValue({
      ...detail,
      review: { ...detail.review, stale: true, review_status: 'RE_REVIEW_NEEDED' },
    })
    vi.mocked(duplicateReview.fetchDuplicateReviewHistory).mockResolvedValue({
      events: [{ old_disposition: 'LIKELY_DUPLICATE', new_disposition: 'NOT_DUPLICATE', reason: 'was not' }],
    })
    vi.mocked(duplicateReview.saveDuplicateReview).mockResolvedValue({
      ok: true,
      planning_only: true,
      merge_will_occur: false,
      approval_created: false,
      plan_only_warning: detail.plan_only_warning,
      disposition: 'MERGE_CANDIDATE',
      reason: 'Plan only',
    })

    render(<AdministrationDuplicateReview />)
    await waitFor(() => expect(screen.getByText(/RE-REVIEW NEEDED/)).toBeTruthy())
    fireEvent.click(screen.getAllByRole('button', { name: 'Edl Packaging Engineers' })[0])
    await waitFor(() => expect(screen.getByText(/REVIEW STALE/)).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Merge Candidate'))
    expect(screen.getAllByText(detail.plan_only_warning).length).toBeGreaterThan(0)
    expect(screen.getByLabelText('Proposed survivor')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Proposed survivor'), { target: { value: '22' } })
    fireEvent.change(screen.getByLabelText('Reason'), {
      target: { value: 'Reviewed pair; plan only for later DS-12.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))
    await waitFor(() =>
      expect(duplicateReview.saveDuplicateReview).toHaveBeenCalledWith(
        22,
        28,
        expect.objectContaining({
          disposition: 'MERGE_CANDIDATE',
          proposed_survivor_company_id: 22,
          proposed_source_company_id: 28,
        }),
      ),
    )
  })

  it('filters and paginates the candidate list', async () => {
    vi.mocked(duplicateReview.fetchDuplicateCandidates).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      automatic_verdict: false,
      writes: false,
      total: 60,
      offset: 0,
      limit: 50,
      pairs: [candidate],
    })
    render(<AdministrationDuplicateReview />)
    await waitFor(() => expect(duplicateReview.fetchDuplicateCandidates).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'EDL' } })
    fireEvent.change(screen.getByLabelText('Disposition'), { target: { value: 'multi_location' } })
    await waitFor(() =>
      expect(duplicateReview.fetchDuplicateCandidates).toHaveBeenCalledWith(
        expect.objectContaining({ disposition: 'multi_location' }),
      ),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() =>
      expect(duplicateReview.fetchDuplicateCandidates).toHaveBeenCalledWith(
        expect.objectContaining({ q: 'EDL' }),
      ),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() =>
      expect(duplicateReview.fetchDuplicateCandidates).toHaveBeenCalledWith(
        expect.objectContaining({ offset: 50 }),
      ),
    )
  })
})
