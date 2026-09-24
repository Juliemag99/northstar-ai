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
  saveWorkbenchDisposition: vi.fn(),
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

function identity(id: number, name: string, extra: Record<string, unknown> = {}) {
  return {
    company_id: id,
    company_name: name,
    record_label: `${name} — Record ${id}`,
    city: extra.city || 'Green Bay',
    state: extra.state || 'WI',
    city_state: `${extra.city || 'Green Bay'}, ${extra.state || 'WI'}`,
    address: extra.address || '1 Main',
    phone: extra.phone || '',
    website: extra.website || '',
    domain: extra.domain || '',
    master_rn: extra.master_rn ?? null,
    master_rn_label: extra.master_rn ? String(extra.master_rn) : 'None',
    client_names: extra.client_names || 'Carmeco',
    active_ccr_count: extra.active_ccr_count ?? 1,
    removed_ccr_count: extra.removed_ccr_count ?? 0,
    contact_count: extra.contact_count ?? 2,
    ...extra,
  }
}

  it('shows workbench identity, exception labels, simple survivor, and complex EDL without merge execution', async () => {
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
    const edlRow = {
      pair_key: '22:28',
      company_a_id: 22,
      company_b_id: 28,
      company_a_name: 'Edl Packaging Engineers',
      company_b_name: 'Edl Packaging Engineers',
      company_a: identity(22, 'Edl Packaging Engineers', { master_rn: '99202', phone: '(920) 347-0143' }),
      company_b: identity(28, 'Edl Packaging Engineers', { master_rn: '101620', phone: '(920) 336-7744', website: 'edlpackaging.com' }),
      source_company_id: 28,
      survivor_company_id: 22,
      plan_state: 'NEEDS_EXCEPTION_DECISION',
      plan_state_label: 'NEEDS DECISIONS',
      exception_count: 4,
      decision_needed: 'Resolve phone conflict',
      decision_needed_all: ['Resolve phone conflict', 'Resolve Carmeco status', 'Resolve Carmeco RN', 'Review possible duplicate contact'],
      workbench_mode: 'complex',
      simple_decision: false,
      complex_decision: true,
      allow_quick_survivor: false,
      same_client_ccr_conflict: true,
    }
    const keldermanRow = {
      pair_key: '45:97',
      company_a_id: 45,
      company_b_id: 97,
      company_a_name: 'Kelderman',
      company_b_name: 'Kelderman',
      company_a: identity(45, 'Kelderman', { city: 'Oskaloosa', state: 'IA', master_rn: '1001' }),
      company_b: identity(97, 'Kelderman', { city: 'Oskaloosa', state: 'IA', master_rn: '1002' }),
      plan_state: 'NEEDS_EXCEPTION_DECISION',
      plan_state_label: 'NEEDS DECISIONS',
      exception_count: 1,
      decision_needed: 'Choose survivor',
      workbench_mode: 'simple',
      simple_decision: true,
      allow_quick_survivor: true,
      same_client_ccr_conflict: false,
    }
    const medtronicRow = {
      pair_key: '2131:2132',
      company_a_id: 2131,
      company_b_id: 2132,
      company_a_name: 'Medtronic',
      company_b_name: 'Medtronic',
      company_a: identity(2131, 'Medtronic', { city: 'Minneapolis', state: 'MN', master_rn: '5001', client_names: 'Premier' }),
      company_b: identity(2132, 'Medtronic', { city: 'Fridley', state: 'MN', master_rn: '5002', client_names: 'Carmeco' }),
      plan_state: 'NEEDS_EXCEPTION_DECISION',
      plan_state_label: 'NEEDS DECISIONS',
      exception_count: 1,
      decision_needed: 'Choose survivor',
      workbench_mode: 'simple',
      simple_decision: true,
      allow_quick_survivor: true,
    }
    const bwRow = {
      pair_key: '151:212',
      company_a_id: 151,
      company_b_id: 212,
      company_a_name: 'BW Integrated Systems',
      company_b_name: 'BW Integrated Systems',
      company_a: identity(151, 'BW Integrated Systems', { city: 'Holland', state: 'MI' }),
      company_b: identity(212, 'BW Integrated Systems', { city: 'Clearwater', state: 'FL' }),
      plan_state: 'NOT_SAFE_TO_PLAN',
      plan_state_label: 'NOT SAFE',
      workbench_mode: 'not_safe',
      allow_quick_survivor: false,
      not_safe_reason: 'Possible multi-location identity.',
      decision_needed: 'Not safe to plan',
    }
    const baxterRow = {
      pair_key: '900:901',
      company_a_id: 900,
      company_b_id: 901,
      company_a_name: 'Baxter',
      company_b_name: 'Baxter',
      company_a: identity(900, 'Baxter', { city: 'Deerfield', state: 'IL', master_rn: '7001', client_names: 'Premier' }),
      company_b: identity(901, 'Baxter', { city: 'Round Lake', state: 'IL', master_rn: null, client_names: 'Carmeco' }),
      plan_state: 'NEEDS_EXCEPTION_DECISION',
      plan_state_label: 'NEEDS DECISIONS',
      decision_needed: 'Choose survivor',
      workbench_mode: 'simple',
      simple_decision: true,
      allow_quick_survivor: true,
    }
    const aldevronRow = {
      pair_key: '910:911',
      company_a_id: 910,
      company_b_id: 911,
      company_a_name: 'Aldevron',
      company_b_name: 'Aldevron',
      company_a: identity(910, 'Aldevron', { city: 'Fargo', state: 'ND', master_rn: '7101' }),
      company_b: identity(911, 'Aldevron', { city: 'Madison', state: 'WI', master_rn: '7102' }),
      plan_state: 'NEEDS_EXCEPTION_DECISION',
      plan_state_label: 'NEEDS DECISIONS',
      decision_needed: 'Choose survivor',
      workbench_mode: 'simple',
      simple_decision: true,
      allow_quick_survivor: true,
    }
    const leicaRow = {
      pair_key: '920:921',
      company_a_id: 920,
      company_b_id: 921,
      company_a_name: 'Leica',
      company_b_name: 'Leica',
      company_a: identity(920, 'Leica', { city: 'Buffalo Grove', state: 'IL', master_rn: '7201' }),
      company_b: identity(921, 'Leica', { city: 'Heerbrugg', state: 'SG', master_rn: '7202' }),
      plan_state: 'NEEDS_EXCEPTION_DECISION',
      plan_state_label: 'NEEDS DECISIONS',
      decision_needed: 'Choose survivor',
      workbench_mode: 'simple',
      simple_decision: true,
      allow_quick_survivor: true,
    }
    vi.mocked(duplicateReview.fetchMergePlans).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      approval_created: false,
      execute_merge: false,
      plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
      summary: {
        analyzed: 16,
        ready_for_review: 0,
        needs_exception_decision: 12,
        not_safe_to_plan: 2,
        stale: 0,
        simple_decisions: 8,
        complex_decisions: 4,
      },
      total: 4,
      offset: 0,
      limit: 50,
      plans: [keldermanRow, medtronicRow, baxterRow, aldevronRow, leicaRow, edlRow, bwRow],
      no_merge_button: true,
      active_client_does_not_scope: true,
    })
    vi.mocked(duplicateReview.prepareMergePlans).mockResolvedValue({
      ok: true,
      analyzed: 16,
      ready_for_review: 0,
      needs_exception_decision: 12,
      not_safe_to_plan: 2,
      stale: 0,
      merge_will_occur: false,
      approval_created: false,
      plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
    })
    const planDetail = {
      ...edlRow,
      planning_only: true,
      merge_will_occur: false,
      approval_created: false,
      plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
      survivor_source: 'HUMAN_MERGE_CANDIDATE',
      why_proposed_survivor: 'Julie already chose Record 22 as the Merge Candidate survivor.',
      human_review: {
        disposition: 'MERGE_CANDIDATE',
        proposed_survivor_company_id: 22,
        proposed_source_company_id: 28,
        actor_name: 'Julie',
        reviewed_at: '2026-04-01T00:00:00Z',
        reason: 'Same plant',
      },
      automated_assessment: {
        classification: 'HIGH_CONFIDENCE_DUPLICATE',
        confidence: 'HIGH',
        why: ['normalized name match'],
        concern_labels: ['phone conflict'],
      },
      preservation: { lines: ['2 LeadMaster/source identities', '3 contacts'], automatic_contacts: [{ survivor_name: 'Pat Exact' }] },
      survivor_comparison: {
        rows: [
          { label: 'Phone', company_a: '(920) 347-0143', company_b: '(920) 336-7744', different: true },
        ],
      },
      exceptions: [
        {
          exception_key: 'FIELD_CONFLICT_REVIEW:phone',
          code: 'FIELD_CONFLICT_REVIEW',
          decision_needed: 'Resolve phone conflict',
          why_you: 'The phone values differ. NorthStar will not overwrite a non-blank survivor value, and needs you to choose how the source value is preserved.',
          choices: [
            { value: 'KEEP_SURVIVOR', label: 'Keep survivor value' },
            { value: 'PRESERVE_SOURCE_AS_IDENTITY', label: 'Keep survivor value and preserve source as historical/source data' },
          ],
          details: { record_a_value: '(920) 347-0143', record_b_value: '(920) 336-7744' },
        },
        {
          exception_key: 'STATUS_DECISION_REQUIRED:1',
          code: 'STATUS_DECISION_REQUIRED',
          decision_needed: 'Resolve Carmeco status',
          why_you: 'Both records have a Carmeco relationship with different statuses. NorthStar will not choose which status survives.',
          choices: [
            { value: 'KEEP_SURVIVOR', label: 'Keep "Hot Prospect"' },
            { value: 'KEEP_SOURCE', label: 'Keep "Future/Nurture"' },
          ],
          details: {
            survivor_ccr: { status: 'Hot Prospect', assigned_user_name: 'Julie', hot: true, follow_up_date: '2026-05-01', next_action: 'Call', external_record_no: '99202' },
            source_ccr: { status: 'Future/Nurture', assigned_user_name: 'Todd', hot: false, follow_up_date: '2026-06-01', next_action: 'Nurture', external_record_no: '101620' },
          },
        },
        {
          exception_key: 'EXTERNAL_RN_DECISION_REQUIRED:1',
          code: 'EXTERNAL_RN_DECISION_REQUIRED',
          decision_needed: 'Resolve Carmeco RN',
          why_you: 'Both records have valid LeadMaster record numbers. NorthStar will preserve both identities, but needs you to choose the primary RN.',
          rn_prompt: 'MASTER / PRIMARY RN AFTER FUTURE MERGE',
          rn_preservation: 'The non-primary RN will be preserved as a source identity when safe.',
          choices: [
            { value: 'KEEP_SURVIVOR', label: 'Use Record 22 RN (99202) as primary' },
            { value: 'KEEP_SOURCE', label: 'Use Record 28 RN (101620) as primary' },
          ],
        },
        {
          exception_key: 'CONTACT_DECISION_REQUIRED:1:2',
          code: 'CONTACT_DECISION_REQUIRED',
          decision_needed: 'Review possible duplicate contact',
          why_you: 'These contacts may be the same person, but they are not a mechanical exact duplicate.',
          choices: [
            { value: 'CONSOLIDATE', label: 'Same person — consolidate in future merge' },
            { value: 'KEEP_SEPARATE', label: 'Keep both contacts' },
          ],
          details: {
            survivor_contact: { name: 'Pat One', title: 'Buyer', email: 'a@example.com', phone: '111', master_rn_label: '99202' },
            source_contact: { name: 'Pat Two', title: 'Purchasing', email: 'b@example.com', phone: '222', master_rn_label: '101620' },
          },
        },
      ],
      no_merge_button: true,
      no_execute_merge: true,
    }
    vi.mocked(duplicateReview.fetchMergePlan).mockImplementation(async (a: number) => {
      if (a === 151) {
        return {
          ...bwRow,
          planning_only: true,
          merge_will_occur: false,
          plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
          exceptions: [],
          preservation: { lines: [] },
          not_safe_reason: 'Possible multi-location identity.',
        }
      }
      return planDetail
    })
    vi.mocked(duplicateReview.saveMergePlanDecision).mockResolvedValue({
      ok: true,
      approval_created: false,
      merge_will_occur: false,
      ready_for_review: true,
      remaining_exception_count: 0,
      replan_invoked: true,
      plan: {
        ...planDetail,
        plan_state: 'READY_FOR_HUMAN_APPROVAL',
        plan_state_label: 'READY FOR REVIEW',
        ready_for_review: true,
        exceptions: [],
      },
      next_needs_decision_pair: { company_a_id: 45, company_b_id: 97, decision_needed: 'Choose survivor' },
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
    vi.mocked(duplicateReview.saveWorkbenchDisposition).mockResolvedValue({
      ok: true,
      approval_created: false,
      merge_will_occur: false,
      disposition: 'MULTI_LOCATION',
      plan: { ...bwRow, planning_only: true, merge_will_occur: false, plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR', exceptions: [] },
    })

    render(<AdministrationDuplicateReview />)
    fireEvent.click(screen.getByRole('button', { name: 'Merge Planning' }))
    await waitFor(() => expect(duplicateReview.fetchMergePlans).toHaveBeenCalled())
    const planCalls = vi.mocked(duplicateReview.fetchMergePlans).mock.calls.map((call) => call[0] || {})
    expect(planCalls.some((args) => 'client_id' in (args as object))).toBe(false)
    expect(screen.getByRole('heading', { name: 'Merge Planning' })).toBeTruthy()
    expect(screen.getByText('Duplicate Exception Workbench')).toBeTruthy()
    expect(screen.getByText(/Active Client selector does not scope/)).toBeTruthy()
    expect(screen.getAllByText('Ready for Review').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Needs Decisions').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Simple Decisions').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Complex Decisions').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Not Safe').length).toBeGreaterThan(0)
    expect(screen.getByText(/Edl Packaging Engineers — Record 22/)).toBeTruthy()
    expect(screen.getByText(/Edl Packaging Engineers — Record 28/)).toBeTruthy()
    expect(screen.getByText(/Baxter — Record 900/)).toBeTruthy()
    expect(screen.getByText(/Baxter — Record 901/)).toBeTruthy()
    expect(screen.getByText(/Aldevron — Record 910/)).toBeTruthy()
    expect(screen.getByText(/Leica — Record 920/)).toBeTruthy()
    expect(screen.getByText(/Leica — Record 921/)).toBeTruthy()
    expect(screen.getAllByText(/RN None/).length).toBeGreaterThan(0)
    expect(screen.getByText(/Minneapolis, MN/)).toBeTruthy()
    expect(screen.getByText(/Fridley, MN/)).toBeTruthy()
    expect(screen.getAllByText(/RN 5001/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Exact decision needed:/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('Choose survivor').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Resolve phone conflict').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Prepare Merge Plans' }))
    await waitFor(() => expect(duplicateReview.prepareMergePlans).toHaveBeenCalled())
    fireEvent.click(screen.getAllByRole('button', { name: 'KEEP A AS SURVIVOR' })[0])
    await waitFor(() => expect(duplicateReview.saveMergePlanDecision).toHaveBeenCalled())
    expect(vi.mocked(duplicateReview.saveMergePlanDecision).mock.calls[0]?.[2]).toMatchObject({
      exception_key: 'SURVIVOR_DECISION_REQUIRED',
      chosen_resolution: 'SURVIVOR:45',
    })
    fireEvent.click(screen.getByRole('button', { name: /Open exception workbench/ }))
    await waitFor(() => expect(screen.getByText('Exception Workbench')).toBeTruthy())
    expect(screen.getByText('PROPOSED FUTURE MERGE')).toBeTruthy()
    expect(screen.getByText('NorthStar Automated Assessment')).toBeTruthy()
    expect(screen.getByText('Human Decision')).toBeTruthy()
    expect(screen.getByText(/WHAT NORTHSTAR WILL PRESERVE/)).toBeTruthy()
    expect(screen.getByText(/2 LeadMaster\/source identities/)).toBeTruthy()
    expect(screen.getByText(/NorthStar will handle automatically in future merge/)).toBeTruthy()
    expect(screen.getByText(/Keep "Hot Prospect"/)).toBeTruthy()
    expect(screen.getByText(/MASTER \/ PRIMARY RN AFTER FUTURE MERGE/)).toBeTruthy()
    expect(screen.getByLabelText('Decision for STATUS_DECISION_REQUIRED:1')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Decision for STATUS_DECISION_REQUIRED:1'), {
      target: { value: 'KEEP_SURVIVOR' },
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Save decision' })[1])
    await waitFor(() => expect(screen.getByRole('heading', { name: 'READY FOR REVIEW' })).toBeTruthy())
    expect(screen.getByRole('button', { name: 'NEXT NEEDS-DECISION PAIR' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Replan' }))
    await waitFor(() => expect(duplicateReview.replanMergePlan).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Not Safe' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open Not Safe review' }))
    await waitFor(() => expect(screen.getAllByText('NOT SAFE TO PLAN').length).toBeGreaterThan(0))
    expect(screen.getAllByText(/Possible multi-location identity/).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /Execute Merge/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Merge Now/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Merge$/i })).toBeNull()
  })
})
