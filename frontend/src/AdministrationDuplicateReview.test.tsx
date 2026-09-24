import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdministrationDuplicateReview from './AdministrationDuplicateReview'
import * as duplicateReview from './api/duplicateReview'

vi.mock('./api/duplicateReview', () => ({
  fetchDuplicateCandidates: vi.fn(),
  fetchDuplicatePair: vi.fn(),
  fetchDuplicateReviewHistory: vi.fn(),
  saveDuplicateReview: vi.fn(),
  analyzeDuplicateCandidates: vi.fn(),
  previewDuplicateBatchReview: vi.fn(),
  confirmDuplicateBatchReview: vi.fn(),
  fetchMergePlans: vi.fn(),
  fetchMergePlan: vi.fn(),
  prepareMergePlans: vi.fn(),
  saveMergePlanDecision: vi.fn(),
  replanMergePlan: vi.fn(),
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
  classification: 'HIGH_CONFIDENCE_DUPLICATE',
  confidence: 'HIGH',
  auto_proposed_survivor_company_id: 22,
  survivor_decision_required: false,
  disagreement: false,
  review_needed: false,
  same_client_ccr_conflict: true,
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
  automated_assessment: {
    assessment_title: 'NorthStar Automated Assessment',
    classifier_kind: 'deterministic_rules',
    external_ai_used: false,
    classification: 'HIGH_CONFIDENCE_DUPLICATE',
    confidence: 'HIGH',
    why: ['Canonical names normalize identically', 'Street address matches', 'Main phone matches'],
    concern_labels: ['Same client has a relationship on both records (merge complexity, not identity proof)'],
    proposed_survivor_company_id: 22,
    survivor_reason: 'established LeadMaster RN; more client relationships',
    disagreement: false,
    stale: false,
  },
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
    expect(screen.getByText('HIGH CONFIDENCE DUPLICATE')).toBeTruthy()
    expect(screen.getByText('High Confidence Duplicate')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Analyze Duplicate Candidates' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Merge$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Execute Merge/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Merge Now/i })).toBeNull()

    fireEvent.click(screen.getAllByRole('button', { name: 'Edl Packaging Engineers' })[0])
    await waitFor(() => expect(screen.getByText('MASTER COMPANY A')).toBeTruthy())
    expect(screen.getByText('MASTER COMPANY B')).toBeTruthy()
    expect(screen.getAllByText('22').length).toBeGreaterThan(0)
    expect(screen.getAllByText('28').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/SAME-CLIENT CCR CONFLICT/).length).toBeGreaterThan(0)
    expect(screen.getByText('NorthStar Automated Assessment')).toBeTruthy()
    expect(screen.getByText(/Canonical names normalize identically/)).toBeTruthy()
    expect(screen.getByText(/Julie disposition/)).toBeTruthy()
    expect(screen.getAllByText(/Unreviewed/).length).toBeGreaterThan(0)
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
    fireEvent.change(screen.getByLabelText('Queue'), { target: { value: 'likely_multi_location' } })
    await waitFor(() =>
      expect(duplicateReview.fetchDuplicateCandidates).toHaveBeenCalledWith(
        expect.objectContaining({ queue: 'likely_multi_location' }),
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

  it('analyzes candidates and batch-reviews eligible pairs without merge candidate', async () => {
    vi.mocked(duplicateReview.fetchDuplicateCandidates).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      automatic_verdict: false,
      writes: false,
      total: 1,
      offset: 0,
      limit: 50,
      pairs: [candidate],
      summary: { HIGH_CONFIDENCE_DUPLICATE: 42, review_needed: 23 },
    })
    vi.mocked(duplicateReview.analyzeDuplicateCandidates).mockResolvedValue({
      ok: true,
      candidates_analyzed: 137,
      buckets: {
        HIGH_CONFIDENCE_DUPLICATE: 42,
        LIKELY_DUPLICATE: 21,
        LIKELY_MULTI_LOCATION: 18,
        LIKELY_NOT_DUPLICATE: 25,
        HUMAN_REVIEW_REQUIRED: 23,
        INSUFFICIENT_EVIDENCE: 8,
      },
      external_ai_used: false,
    })
    vi.mocked(duplicateReview.previewDuplicateBatchReview).mockResolvedValue({
      ok: true,
      writes: false,
      action: 'ACCEPT_LIKELY_DUPLICATE',
      new_disposition: 'LIKELY_DUPLICATE',
      selected: 20,
      eligible: 18,
      excluded: 2,
      excluded_rows: [{ pair_key: '1:2', reason: 'stale_classification' }],
      eligible_rows: [],
      confirm_allowed: true,
      reason_ok: true,
      preview_fingerprint: 'abc123',
      batch_merge_candidate: false,
    })
    vi.mocked(duplicateReview.confirmDuplicateBatchReview).mockResolvedValue({
      ok: true,
      saved: 18,
      selected: 20,
      eligible: 18,
      excluded: 2,
      new_disposition: 'LIKELY_DUPLICATE',
      merge_will_occur: false,
      approval_created: false,
    })

    render(<AdministrationDuplicateReview />)
    await waitFor(() => expect(screen.getByText('High Confidence Duplicate')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Analyze Duplicate Candidates' }))
    await waitFor(() => expect(duplicateReview.analyzeDuplicateCandidates).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText(/Candidates analyzed: 137/)).toBeTruthy())
    expect(screen.getByLabelText('Select 22:28')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Select 22:28'))
    fireEvent.click(screen.getByRole('button', { name: 'Select Current Page' }))
    expect(screen.getByLabelText('Select All Filtered')).toBeTruthy()
    const batchSelect = screen.getByLabelText('Batch action') as HTMLSelectElement
    expect(Array.from(batchSelect.options).map((row) => row.value)).not.toContain('MERGE_CANDIDATE')
    fireEvent.change(screen.getByLabelText('Batch reason'), {
      target: { value: 'Accept high-confidence matches after evidence review.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview batch review' }))
    await waitFor(() => expect(screen.getAllByText(/18 eligible/).length).toBeGreaterThan(0))
    expect(screen.getAllByText(/require individual review/).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm batch review' }))
    await waitFor(() => expect(duplicateReview.confirmDuplicateBatchReview).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /^Merge$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Execute/i })).toBeNull()
  })

  it('shows disagreement separately from the human disposition', async () => {
    vi.mocked(duplicateReview.fetchDuplicateCandidates).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      automatic_verdict: false,
      writes: false,
      total: 1,
      offset: 0,
      limit: 50,
      pairs: [{ ...candidate, disagreement: true, disposition: 'MULTI_LOCATION' }],
    })
    vi.mocked(duplicateReview.fetchDuplicatePair).mockResolvedValue({
      ...detail,
      review: { ...detail.review, disposition: 'MULTI_LOCATION', reason: 'Two plants' },
      automated_assessment: {
        ...detail.automated_assessment,
        disagreement: true,
        classification: 'HIGH_CONFIDENCE_DUPLICATE',
      },
    })
    vi.mocked(duplicateReview.fetchDuplicateReviewHistory).mockResolvedValue({ events: [] })
    render(<AdministrationDuplicateReview />)
    await waitFor(() => expect(screen.getByText(/DISAGREES/)).toBeTruthy())
    fireEvent.click(screen.getAllByRole('button', { name: 'Edl Packaging Engineers' })[0])
    await waitFor(() =>
      expect(screen.getByText(/HUMAN DECISION DIFFERS FROM AUTOMATED ASSESSMENT/)).toBeTruthy(),
    )
    expect(screen.getByText(/Julie disposition/)).toBeTruthy()
    expect(screen.getByText(/MULTI LOCATION/)).toBeTruthy()
  })

  it('prepares merge plans, opens needs-decisions detail, and saves a plan-only exception decision', async () => {
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
    vi.mocked(duplicateReview.fetchMergePlans).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      approval_created: false,
      execute_merge: false,
      plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
      summary: {
        analyzed: 16,
        ready_for_review: 3,
        needs_exception_decision: 12,
        not_safe_to_plan: 1,
        stale: 0,
      },
      total: 1,
      offset: 0,
      limit: 50,
      plans: [
        {
          pair_key: '22:28',
          company_a_id: 22,
          company_b_id: 28,
          company_a_name: 'Edl Packaging Engineers',
          company_b_name: 'Edl Packaging Engineers',
          source_company_id: 28,
          survivor_company_id: 22,
          plan_state: 'NEEDS_EXCEPTION_DECISION',
          plan_state_label: 'NEEDS DECISIONS',
          exception_count: 2,
          same_client_ccr_conflict: true,
        },
      ],
      no_merge_button: true,
    })
    vi.mocked(duplicateReview.prepareMergePlans).mockResolvedValue({
      ok: true,
      analyzed: 16,
      ready_for_review: 3,
      needs_exception_decision: 12,
      not_safe_to_plan: 1,
      stale: 0,
      merge_will_occur: false,
      approval_created: false,
      plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
    })
    const planDetail = {
      planning_only: true,
      merge_will_occur: false,
      approval_created: false,
      plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
      pair_key: '22:28',
      company_a_id: 22,
      company_b_id: 28,
      source_company_id: 28,
      survivor_company_id: 22,
      survivor_source: 'HUMAN_MERGE_CANDIDATE',
      plan_state: 'NEEDS_EXCEPTION_DECISION',
      plan_state_label: 'NEEDS DECISIONS',
      master_fields: [
        { field: 'company_name', action: 'KEEP_SURVIVOR', survivor_value: 'Edl Packaging Engineers', source_value: 'Edl Packaging Engineers' },
        { field: 'external_record_no', action: 'PRESERVE_SOURCE_AS_IDENTITY', survivor_value: '99202', source_value: '101620' },
      ],
      ccrs: [{ client_name: 'Carmeco', classification: 'CCR_DECISION_REQUIRED', action: 'HUMAN_CCR_DECISION' }],
      contacts: { unique_to_preserve: 1, exact_duplicates: 1, possible_duplicates: 0, conflicts: 0 },
      aliases: { unique_to_preserve: 1 },
      locations: { strong_multi_location_concern: false },
      identities: { never_drop_source_rn: true },
      campaigns: { unique_memberships_to_preserve: 1, same_campaign_already_on_survivor: 0 },
      history: { source: { notes: 1 }, survivor: { notes: 2 } },
      exceptions: [
        {
          exception_key: 'STATUS_DECISION_REQUIRED:1',
          code: 'STATUS_DECISION_REQUIRED',
          label: 'Carmeco CCR Status Decision Required requires a human choice.',
          options: ['KEEP_SURVIVOR', 'KEEP_SOURCE'],
        },
      ],
      location_concern: false,
      identity_concern: false,
      no_merge_button: true,
      no_execute_merge: true,
    }
    vi.mocked(duplicateReview.fetchMergePlan).mockResolvedValue(planDetail)
    vi.mocked(duplicateReview.saveMergePlanDecision).mockResolvedValue({
      ok: true,
      approval_created: false,
      merge_will_occur: false,
      plan: {
        ...planDetail,
        plan_state: 'READY_FOR_HUMAN_APPROVAL',
        plan_state_label: 'READY FOR REVIEW',
        exceptions: [],
      },
    })
    vi.mocked(duplicateReview.replanMergePlan).mockResolvedValue({
      ok: true,
      approval_created: false,
      merge_will_occur: false,
      plan: {
        ...planDetail,
        plan_state: 'READY_FOR_HUMAN_APPROVAL',
        plan_state_label: 'READY FOR REVIEW',
        exceptions: [],
      },
    })

    render(<AdministrationDuplicateReview />)
    fireEvent.click(screen.getByRole('button', { name: 'Merge Planning' }))
    await waitFor(() => expect(duplicateReview.fetchMergePlans).toHaveBeenCalled())
    expect(screen.getByRole('heading', { name: 'Merge Planning' })).toBeTruthy()
    expect(screen.getAllByText('Ready for Review').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Needs Decisions').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Not Safe').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Stale').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Prepare Merge Plans' }))
    await waitFor(() => expect(duplicateReview.prepareMergePlans).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText(/Analyzed 16/)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Needs Decisions' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edl Packaging Engineers / Edl Packaging Engineers' }))
    await waitFor(() => expect(screen.getByText('PROPOSED FUTURE MERGE')).toBeTruthy())
    expect(screen.getAllByText(/PLANNING ONLY — NO MERGE WILL OCCUR/).length).toBeGreaterThan(0)
    expect(screen.getByText(/Master field result/)).toBeTruthy()
    expect(screen.getByText(/PRESERVE_SOURCE_AS_IDENTITY/)).toBeTruthy()
    expect(screen.getByText(/CCR result by client/)).toBeTruthy()
    expect(screen.getAllByText(/Carmeco/).length).toBeGreaterThan(0)
    expect(screen.getByText(/Unique 1/)).toBeTruthy()
    expect(screen.getByText(/Location concern No/)).toBeTruthy()
    expect(screen.getByLabelText('Decision for STATUS_DECISION_REQUIRED:1')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Decision for STATUS_DECISION_REQUIRED:1'), {
      target: { value: 'KEEP_SURVIVOR' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save decision' }))
    await waitFor(() => expect(duplicateReview.saveMergePlanDecision).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Replan' }))
    await waitFor(() => expect(duplicateReview.replanMergePlan).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /Execute Merge/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Merge Now/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Merge$/i })).toBeNull()
  })
})
