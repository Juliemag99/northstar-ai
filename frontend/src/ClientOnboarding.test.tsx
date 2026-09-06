import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import {
  ONBOARDING_STEPS,
  draftIsDirty,
  statusesFromText,
  validateStepBasics,
  validateStepCrm,
} from './clientOnboardingHelpers'

vi.mock('./auth/useAuth', () => ({
  useAuth: () => ({
    user: {
      id: 1,
      email: 'admin@example.test',
      full_name: 'Admin',
      is_administrator: true,
      is_internal_northstar: true,
      active: true,
    },
    authenticated: true,
  }),
}))

vi.mock('./auth/staffCanAdminister', () => ({
  staffCanAdminister: () => true,
}))

const saveMock = vi.fn()
const createMock = vi.fn()
const templatesMock = vi.fn()
const listMock = vi.fn()
const previewMock = vi.fn()

vi.mock('./api/clientOnboarding', () => ({
  fetchOnboardingTemplates: () => templatesMock(),
  createOnboardingDraft: (...args: unknown[]) => createMock(...args),
  fetchOnboardingDraft: vi.fn(),
  saveOnboardingDraft: (...args: unknown[]) => saveMock(...args),
  applyOnboardingTemplate: vi.fn(),
  previewOnboardingCopy: (...args: unknown[]) => previewMock(...args),
  applyOnboardingCopy: vi.fn(),
  finishOnboardingDraft: vi.fn(),
  listOnboardingDrafts: vi.fn(),
}))

vi.mock('./api/carmeco', () => ({
  fetchClientSetupList: () => listMock(),
}))

import ClientOnboarding from './ClientOnboarding'

function baseDraft(overrides: Record<string, unknown> = {}) {
  return {
    draft_id: 9,
    client_id: null,
    mode: 'new',
    status: 'draft',
    current_step: 1,
    payload: {
      basics: { client_name: '', website: '', main_location: '', description: '' },
      sells: { primary_service: '' },
      opportunities: { target_customer_types: '' },
      crm: {
        statuses: ['Prospect'],
        default_status: 'Prospect',
        campaign_name: 'Default',
        campaign_description: '',
        apply_assignments: false,
        assignment_user_ids: [],
      },
      field_tiers: {},
    },
    template_id: '',
    copy_source_client_id: null,
    completion_percent: 10,
    missing_required: ['basics.client_name'],
    missing_recommended: [],
    can_finish: false,
    created_at: '',
    updated_at: '',
    finished_at: '',
    ...overrides,
  }
}

describe('clientOnboardingHelpers', () => {
  it('validates basics and CRM steps', () => {
    expect(validateStepBasics({ client_name: '' })).toContain('Client name is required.')
    expect(validateStepBasics({ client_name: 'Acme' })).toEqual([])
    expect(
      validateStepCrm({ campaign_name: '', statuses: [], default_status: '' }).length,
    ).toBeGreaterThan(0)
    expect(statusesFromText('A\nB, C')).toEqual(['A', 'B', 'C'])
    expect(ONBOARDING_STEPS).toHaveLength(5)
  })

  it('detects unsaved changes', () => {
    const payload = { basics: { client_name: 'A' } }
    const snap = JSON.stringify(payload)
    expect(draftIsDirty(snap, payload, 1, 1)).toBe(false)
    expect(draftIsDirty(snap, { basics: { client_name: 'B' } }, 1, 1)).toBe(true)
    expect(draftIsDirty(snap, payload, 2, 1)).toBe(true)
  })
})

describe('ClientOnboarding UI', () => {
  it('navigates steps, saves draft, and shows template/copy controls', async () => {
    templatesMock.mockResolvedValue({
      templates: [
        { id: 'cnc_machining', name: 'CNC machining', description: 'CNC' },
        { id: 'blank_custom', name: 'Blank / custom', description: '' },
      ],
      field_tiers: { 'basics.client_name': 'required' },
      forbidden_copy_categories: ['prospects'],
    })
    listMock.mockResolvedValue([{ client_id: 2, client_name: 'Brown Industries' }])
    createMock.mockResolvedValue(baseDraft())
    saveMock.mockImplementation(async (_id: number, body: { current_step?: number; payload?: { basics?: { client_name?: string } } }) =>
      baseDraft({
        current_step: body.current_step || 1,
        payload: {
          ...baseDraft().payload,
          ...(body.payload || {}),
          basics: {
            ...baseDraft().payload.basics,
            ...(body.payload?.basics || {}),
            client_name: body.payload?.basics?.client_name || 'Acme',
          },
        },
        missing_required: [],
        can_finish: true,
        completion_percent: 80,
      }),
    )
    previewMock.mockResolvedValue({
      source_client_id: 2,
      source_client_name: 'Brown Industries',
      destination_client_id: null,
      draft_id: 9,
      fields: [
        {
          path: 'sells.primary_service',
          source_value: 'Metal stamping',
          destination_value: '',
          will_copy: true,
          blocked_reason: '',
        },
      ],
      conflict_paths: [],
      never_copied: ['prospects', 'api_credentials'],
      copyable_only: ['primary_service'],
    })

    render(
      <MemoryRouter>
        <ClientOnboarding />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'Add Client' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText(/Client name/i), {
      target: { value: 'Acme' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByText(/Draft saved/i)).toBeTruthy()
    expect(saveMock).toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Template'), {
      target: { value: 'cnc_machining' },
    })
    expect(screen.getByRole('button', { name: /Apply template/i })).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Copy source client'), {
      target: { value: '2' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview what will be copied/i }))
    expect(await screen.findByText(/Copy preview from Brown Industries/i)).toBeTruthy()
    expect(screen.getByText(/Never copied/i).textContent || '').toMatch(/prospects/)

    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText(/STEP 2/i)).toBeTruthy()
  })
})
