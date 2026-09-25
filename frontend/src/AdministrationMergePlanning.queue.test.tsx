import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Administration from './Administration'
import { AuthContext, type AuthContextValue } from './auth/useAuth'
import type { StaffUser } from './api/auth'
import * as duplicateReview from './api/duplicateReview'

vi.mock('./api/carmeco', () => ({
  listEmailAccounts: vi.fn(async () => []),
  fetchGoogleOAuthStatus: vi.fn(async () => ({ configured: false })),
  connectEmailAccount: vi.fn(),
  disconnectEmailAccount: vi.fn(),
}))

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

const adminUser: StaffUser = {
  id: 1,
  email: 'juliem@n-star.us',
  full_name: 'Julie Magnani',
  is_administrator: true,
  is_internal_northstar: true,
  active: true,
  created_at: '2026-08-07 16:18:04',
}

function authValue(): AuthContextValue {
  return {
    ready: true,
    sessionError: false,
    user: adminUser,
    authenticated: true,
    authAvailable: true,
    authEnforced: true,
    login: vi.fn(async () => undefined),
    logout: vi.fn(async () => undefined),
    retrySession: vi.fn(async () => undefined),
  }
}

function rel(client: string, status: string, assigned: string, extra: Record<string, unknown> = {}) {
  return {
    client_name: client,
    client_code: String(extra.client_code || client.split(' ')[0].toLowerCase()),
    status: status || null,
    status_label: status || 'No status',
    display: `${client} — ${status || 'No status'}`,
    assigned_user_name: assigned || 'Unassigned',
    active: extra.active !== false,
    state_label: extra.active === false ? 'Removed' : 'Active',
    hot: Boolean(extra.hot),
    follow_up_date: extra.follow_up_date ?? null,
    next_action: extra.next_action || '',
    external_record_no: extra.external_record_no ?? null,
    campaigns: extra.campaigns || [],
    ...extra,
  }
}

function identity(id: number, name: string, extra: Record<string, unknown> = {}) {
  const relationships =
    (extra.active_relationships as unknown[] | undefined) ||
    (extra.relationships as unknown[] | undefined) ||
    [rel('Carmeco', String(extra.status || 'New'), extra.assigned === undefined ? 'Julie Magnani' : String(extra.assigned || 'Unassigned'))]
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
    active_relationships: relationships.filter((row) => (row as { active?: boolean }).active !== false),
    relationships,
    contacts: extra.contacts || [],
    aliases: extra.aliases || [],
    locations: extra.locations || [],
    identities: extra.identities || [],
    ...extra,
  }
}

function keldermanPreservation() {
  return {
    preview_version: 'DS14B_PREVIEW_V1',
    planning_only: true,
    table: [
      { data_type: 'LeadMaster IDs', before: 2, planned_action: 'Preserve both', expected_after: 2 },
      { data_type: 'Contacts', before: 3, planned_action: '1 exact contact consolidation · 1 unique preserved', expected_after: 2 },
      { data_type: 'Client relationships', before: 2, planned_action: 'Preserve unique clients; consolidate same-client CCR', expected_after: 1 },
      { data_type: 'Notes', before: 2, planned_action: 'Preserve all', expected_after: 2 },
      { data_type: 'Activities/history', before: 0, planned_action: 'Preserve all', expected_after: 0 },
      { data_type: 'Campaigns', before: 0, planned_action: 'Preserve all', expected_after: 0 },
      { data_type: 'Aliases', before: 0, planned_action: 'Preserve all', expected_after: 0 },
      { data_type: 'Locations', before: 1, planned_action: 'Preserve unique locations', expected_after: 1 },
    ],
    leadmaster: {
      title: 'LEADMASTER / SOURCE IDENTITIES',
      record_a: { company_id: 45, primary_rn: '138431' },
      record_b: { company_id: 97, primary_rn: '253832' },
      planned_result_count: 2,
      planned_rns: ['138431', '253832'],
      future_plan: 'Preserve both LeadMaster RNs as source identities',
    },
    contacts: {
      before: { record_a: 2, record_b: 1, total_source_rows: 3 },
      planned: { exact_consolidations: 1, unique_preserved: 1, decision_required: 0 },
      expected_after: 2,
      rows: [
        {
          name: 'Gary L Kelderman',
          record_ids: [45, 97],
          treatment: 'EXACT / SAME PERSON — FUTURE CONSOLIDATION',
        },
        { name: 'Debbie Unknown', record_ids: [45], treatment: 'UNIQUE — PRESERVE' },
      ],
    },
    ccrs: {
      title: 'CLIENT RELATIONSHIPS',
      rows: [{ client_name: 'Carmeco', planned_result: 'same values → preserve / consolidate to one relationship' }],
      expected_after: 1,
    },
    notes: { before: 2, expected_after: 2 },
    warnings: [],
    blocks_ready: false,
    lines: ['LeadMaster IDs: 2 → 2', 'Contacts: 3 → 2', 'Notes: 2 → 2'],
  }
}

function edlPreservation() {
  return {
    preview_version: 'DS14B_PREVIEW_V1',
    planning_only: true,
    table: [
      { data_type: 'LeadMaster IDs', before: 2, planned_action: 'Preserve both', expected_after: 2 },
      { data_type: 'Contacts', before: 3, planned_action: 'contact decision required', expected_after: 'Pending decision' },
      { data_type: 'Client relationships', before: 2, planned_action: 'Pending client-field decisions', expected_after: 'Pending decision' },
    ],
    leadmaster: {
      title: 'LEADMASTER / SOURCE IDENTITIES',
      record_a: { company_id: 22, primary_rn: '99202' },
      record_b: { company_id: 28, primary_rn: '101620' },
      planned_result_count: 2,
      planned_rns: ['99202', '101620'],
    },
    contacts: {
      before: { record_a: 2, record_b: 1, total_source_rows: 3 },
      planned: { decision_required: 1 },
      expected_after: 'Pending decision',
      rows: [{ name: 'Pat One', record_ids: [22, 28], treatment: 'DECISION REQUIRED' }],
    },
    warnings: [],
    blocks_ready: false,
    lines: ['Contacts: 3 → Pending decision'],
  }
}

const keldermanRow = {
  pair_key: '45:97',
  company_a_id: 45,
  company_b_id: 97,
  company_a_name: 'Kelderman',
  company_b_name: 'Kelderman',
  company_a: identity(45, 'Kelderman', {
    city: 'Oskaloosa',
    state: 'IA',
    address: '2686 Hwy 92',
    phone: '(641) 673-0469',
    master_rn: '138431',
    contact_count: 2,
    active_relationships: [rel('Carmeco', 'New', 'Julie Magnani', { external_record_no: '138431' })],
  }),
  company_b: identity(97, 'Kelderman', {
    city: 'Oskaloosa',
    state: 'IA',
    master_rn: '253832',
    contact_count: 1,
    active_relationships: [rel('Carmeco', 'New', 'Julie Magnani', { external_record_no: '253832' })],
  }),
  plan_state: 'NEEDS_EXCEPTION_DECISION',
  plan_state_label: 'NEEDS DECISIONS',
  exception_count: 1,
  decision_needed: 'Choose survivor',
  workbench_mode: 'simple',
  simple_decision: true,
  allow_quick_survivor: true,
  same_client_ccr_conflict: false,
}

const kuhnRow = {
  pair_key: '229:358',
  company_a_id: 229,
  company_b_id: 358,
  company_a_name: 'Kuhn',
  company_b_name: 'Kuhn',
  company_a: identity(229, 'Kuhn', {
    master_rn: '111111',
    contact_count: 1,
    active_relationships: [rel('Carmeco', 'New', 'Julie Magnani')],
  }),
  company_b: identity(358, 'Kuhn', {
    master_rn: '222222',
    contact_count: 1,
    active_relationships: [rel('Carmeco', 'New', 'Julie Magnani')],
  }),
  plan_state: 'NEEDS_EXCEPTION_DECISION',
  plan_state_label: 'NEEDS DECISIONS',
  exception_count: 1,
  decision_needed: 'Choose survivor',
  workbench_mode: 'simple',
  simple_decision: true,
  allow_quick_survivor: true,
  same_client_ccr_conflict: false,
}

const edlRow = {
  pair_key: '22:28',
  company_a_id: 22,
  company_b_id: 28,
  company_a_name: 'Edl Packaging Engineers',
  company_b_name: 'Edl Packaging Engineers',
  company_a: identity(22, 'Edl Packaging Engineers', {
    master_rn: '99202',
    phone: '(920) 347-0143',
    contact_count: 2,
    active_relationships: [rel('Carmeco', 'Left Message', 'Julie Magnani', { external_record_no: '99202' })],
  }),
  company_b: identity(28, 'Edl Packaging Engineers', {
    master_rn: '101620',
    phone: '(920) 336-7744',
    contact_count: 1,
    active_relationships: [rel('Carmeco', 'New', 'Julie Magnani', { external_record_no: '101620' })],
  }),
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

function planDetail(row: typeof keldermanRow, extra: Record<string, unknown> = {}) {
  return {
    ...row,
    planning_only: true,
    merge_will_occur: false,
    approval_created: false,
    plan_only_warning: 'PLANNING ONLY — NO MERGE WILL OCCUR',
    exceptions: extra.exceptions || [
      {
        exception_key: 'SURVIVOR_DECISION_REQUIRED',
        code: 'SURVIVOR_DECISION_REQUIRED',
        decision_needed: 'Choose survivor',
        why_you: 'choose',
        choices: [{ value: `SURVIVOR:${row.company_a_id}`, label: `Make Record ${row.company_a_id} the survivor` }],
        details: {},
      },
    ],
    preservation: extra.preservation || keldermanPreservation(),
    no_merge_button: true,
    no_execute_merge: true,
    ...extra,
  }
}

function renderAdministration() {
  return render(
    <AuthContext.Provider value={authValue()}>
      <MemoryRouter initialEntries={['/administration']}>
        <Routes>
          <Route
            path="/administration"
            element={
              <Administration
                activeClientId={1}
                availableClients={[{ client_id: 1, client_name: 'Carmeco' }]}
              />
            }
          />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('Administration → Data Management → Duplicate Review → Merge Planning queue', () => {
  it('renders the production queue path with DS-14A controls and DS-14B preservation', async () => {
    vi.mocked(duplicateReview.fetchDuplicateCandidates).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      automatic_verdict: false,
      writes: false,
      total: 0,
      offset: 0,
      limit: 50,
      pairs: [],
      summary: {
        candidates: 0,
        HIGH_CONFIDENCE_DUPLICATE: 0,
        LIKELY_DUPLICATE: 0,
        LIKELY_MULTI_LOCATION: 0,
        LIKELY_NOT_DUPLICATE: 0,
        HUMAN_REVIEW_REQUIRED: 0,
        INSUFFICIENT_EVIDENCE: 0,
        review_needed: 0,
      },
    })
    vi.mocked(duplicateReview.fetchMergePlans).mockResolvedValue({
      planning_only: true,
      merge_will_occur: false,
      total: 3,
      offset: 0,
      limit: 50,
      plans: [keldermanRow, kuhnRow, edlRow],
      summary: {
        analyzed: 3,
        ready_for_review: 0,
        needs_exception_decision: 3,
        not_safe_to_plan: 0,
        stale: 0,
        simple_decisions: 2,
        complex_decisions: 1,
      },
      no_merge_button: true,
      active_client_does_not_scope: true,
    })
    vi.mocked(duplicateReview.fetchMergePlan).mockImplementation(async (a: number) => {
      if (a === 45) return planDetail(keldermanRow)
      if (a === 229) return planDetail(kuhnRow, { preservation: { ...keldermanPreservation(), leadmaster: { title: 'LEADMASTER / SOURCE IDENTITIES', record_a: { company_id: 229 }, record_b: { company_id: 358 } } } })
      return planDetail(edlRow, {
        preservation: edlPreservation(),
        exceptions: [
          { exception_key: 'FIELD_CONFLICT_REVIEW:phone', code: 'FIELD_CONFLICT_REVIEW', decision_needed: 'Resolve phone conflict', why_you: 'phones differ', choices: [], details: {} },
          { exception_key: 'STATUS_DECISION_REQUIRED:1', code: 'STATUS_DECISION_REQUIRED', decision_needed: 'Resolve Carmeco status', why_you: 'status', choices: [], details: {} },
          { exception_key: 'EXTERNAL_RN_DECISION_REQUIRED:1', code: 'EXTERNAL_RN_DECISION_REQUIRED', decision_needed: 'Resolve Carmeco RN', why_you: 'rn', choices: [], details: {} },
          { exception_key: 'CONTACT_DECISION_REQUIRED:1:2', code: 'CONTACT_DECISION_REQUIRED', decision_needed: 'Review possible duplicate contact', why_you: 'contact', choices: [], details: {} },
        ],
      })
    })

    renderAdministration()
    fireEvent.click(screen.getByRole('tab', { name: 'Data Management' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Duplicate Review' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Duplicate Review' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Merge Planning' }))
    await waitFor(() => expect(duplicateReview.fetchMergePlans).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByRole('article', { name: /Record 45 vs .*Record 97/ })).toBeTruthy())

    const keldermanCard = screen.getByRole('article', { name: /Record 45 vs .*Record 97/ })
    expect(within(keldermanCard).getByRole('button', { name: 'REVIEW DETAILS' })).toBeTruthy()
    expect(within(keldermanCard).getByRole('button', { name: 'KEEP RECORD 45' })).toBeTruthy()
    expect(within(keldermanCard).getByRole('button', { name: 'KEEP RECORD 97' })).toBeTruthy()
    expect(within(keldermanCard).getByRole('button', { name: 'THESE ARE SEPARATE RECORDS / LOCATIONS' })).toBeTruthy()
    expect(within(keldermanCard).getByRole('button', { name: 'NEEDS RESEARCH' })).toBeTruthy()
    expect(within(keldermanCard).queryByRole('button', { name: 'KEEP A AS SURVIVOR' })).toBeNull()
    expect(within(keldermanCard).queryByRole('button', { name: 'KEEP B AS SURVIVOR' })).toBeNull()
    expect(within(keldermanCard).getAllByText('Carmeco — New').length).toBe(2)
    expect(within(keldermanCard).getAllByText('Assigned: Julie Magnani').length).toBeGreaterThan(0)
    expect(within(keldermanCard).getByText('RN 138431')).toBeTruthy()
    expect(within(keldermanCard).getByText('RN 253832')).toBeTruthy()
    expect(within(keldermanCard).getByText(/Contacts:\s*2/)).toBeTruthy()
    expect(within(keldermanCard).getByText(/Contacts:\s*1/)).toBeTruthy()

    const kuhnCard = screen.getByRole('article', { name: /Record 229 vs .*Record 358/ })
    expect(within(kuhnCard).getByRole('button', { name: 'REVIEW DETAILS' })).toBeTruthy()
    expect(within(kuhnCard).getByRole('button', { name: 'KEEP RECORD 229' })).toBeTruthy()
    expect(within(kuhnCard).getByRole('button', { name: 'KEEP RECORD 358' })).toBeTruthy()
    expect(within(kuhnCard).getAllByText('Carmeco — New').length).toBeGreaterThan(0)
    expect(within(kuhnCard).getAllByText('Assigned: Julie Magnani').length).toBeGreaterThan(0)

    const edlCard = screen.getByRole('article', { name: /Record 22 vs .*Record 28/ })
    expect(within(edlCard).getByRole('button', { name: 'REVIEW DETAILS' })).toBeTruthy()
    expect(within(edlCard).queryByRole('button', { name: 'KEEP RECORD 22' })).toBeNull()
    expect(within(edlCard).getByText('Carmeco — Left Message')).toBeTruthy()
    expect(within(edlCard).getByText('Carmeco — New')).toBeTruthy()

    fireEvent.click(keldermanCard)
    await waitFor(() => expect(screen.getByRole('article', { name: 'Exception workbench' })).toBeTruthy())
    expect(duplicateReview.saveMergePlanDecision).not.toHaveBeenCalled()
    const details = screen.getByRole('article', { name: 'Exception workbench' })
    expect(within(details).getByText('DATA TYPE')).toBeTruthy()
    expect(within(details).getByText('BEFORE')).toBeTruthy()
    expect(within(details).getByText('PLANNED ACTION')).toBeTruthy()
    expect(within(details).getByText('EXPECTED AFTER')).toBeTruthy()
    expect(within(details).getByText(/WHAT NORTHSTAR WILL PRESERVE/)).toBeTruthy()
    fireEvent.click(within(details).getAllByRole('button', { name: 'CLOSE DETAILS' })[0])
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Exception workbench' })).toBeNull())
    expect(screen.getByRole('article', { name: /Record 45 vs .*Record 97/ })).toBeTruthy()

    fireEvent.click(within(screen.getByRole('article', { name: /Record 45 vs .*Record 97/ })).getByRole('button', { name: 'REVIEW DETAILS' }))
    await waitFor(() => expect(screen.getByRole('article', { name: 'Exception workbench' })).toBeTruthy())
    expect(duplicateReview.saveMergePlanDecision).not.toHaveBeenCalled()
    expect(screen.getByText('DATA TYPE')).toBeTruthy()
    expect(screen.getByText('EXPECTED AFTER')).toBeTruthy()
    expect(screen.getByText(/Record 45 Primary RN:\s*138431/)).toBeTruthy()
    expect(screen.getByText(/Record 97 Primary RN:\s*253832/)).toBeTruthy()
    expect(screen.getByText('Gary L Kelderman')).toBeTruthy()
    expect(screen.getByText(/EXACT \/ SAME PERSON/)).toBeTruthy()
    expect(screen.getByText('Debbie Unknown')).toBeTruthy()
    expect(screen.getByText(/UNIQUE — PRESERVE/)).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: 'CLOSE DETAILS' })[0])
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Exception workbench' })).toBeNull())

    fireEvent.click(within(screen.getByRole('article', { name: /Record 229 vs .*Record 358/ })).getByRole('button', { name: 'REVIEW DETAILS' }))
    await waitFor(() => expect(screen.getByRole('article', { name: 'Exception workbench' })).toBeTruthy())
    expect(duplicateReview.saveMergePlanDecision).not.toHaveBeenCalled()
    fireEvent.click(screen.getAllByRole('button', { name: 'CLOSE DETAILS' })[0])
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Exception workbench' })).toBeNull())

    fireEvent.click(within(screen.getByRole('article', { name: /Record 22 vs .*Record 28/ })).getByRole('button', { name: 'REVIEW DETAILS' }))
    await waitFor(() => expect(screen.getByRole('article', { name: 'Exception workbench' })).toBeTruthy())
    expect(duplicateReview.saveMergePlanDecision).not.toHaveBeenCalled()
    const edlDetails = screen.getByRole('article', { name: 'Exception workbench' })
    expect(within(edlDetails).getByText('DATA TYPE')).toBeTruthy()
    expect(within(edlDetails).getAllByText('Resolve phone conflict').length).toBeGreaterThan(0)
    expect(within(edlDetails).getAllByText('Resolve Carmeco status').length).toBeGreaterThan(0)
    expect(within(edlDetails).getAllByText('Resolve Carmeco RN').length).toBeGreaterThan(0)
    expect(within(edlDetails).getAllByText('Review possible duplicate contact').length).toBeGreaterThan(0)
    fireEvent.click(screen.getAllByRole('button', { name: 'CLOSE DETAILS' })[0])
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Exception workbench' })).toBeNull())
    expect(duplicateReview.saveMergePlanDecision).not.toHaveBeenCalled()
    expect(duplicateReview.saveWorkbenchDisposition).not.toHaveBeenCalled()
  })
})
