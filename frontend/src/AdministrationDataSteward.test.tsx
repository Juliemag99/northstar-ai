import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdministrationDataSteward from './AdministrationDataSteward'
import { staffCanAdminister } from './auth/staffCanAdminister'
import type { StaffUser } from './api/auth'
import * as dataSteward from './api/dataSteward'

vi.mock('./api/dataSteward', () => ({
  fetchStewardMeta: vi.fn(),
  searchStewardCompanies: vi.fn(),
  fetchStewardCompany: vi.fn(),
  previewStewardCompanyAmend: vi.fn(),
  saveStewardCompanyAmend: vi.fn(),
  fetchStewardProvenance: vi.fn(),
  fetchStewardRelationshipEvents: vi.fn(),
  previewStewardRelationshipAction: vi.fn(),
  confirmStewardRelationshipAction: vi.fn(),
}))

const company = {
  company_id: 22,
  company_name: 'Edl Packaging Engineers',
  address: '1 Main',
  city: 'Green Bay',
  state: 'WI',
  zip: '54301',
  website: 'https://edl.example',
  phone: '(920) 555-0100',
  phone_extension: '12',
  external_record_no: '22',
  updated_at: '2026-09-22T12:00:00Z',
  archived: false,
  linked_clients: [
    { client_id: 2, code: 'brown', name: 'Brown Industries', status: 'Working', ccr_id: 9, archived: false },
    {
      client_id: 4,
      code: 'premier',
      name: 'Premier Packaging',
      status: 'New',
      ccr_id: 11,
      archived: true,
      archived_at: '2026-09-22T12:00:00Z',
    },
  ],
  master_data_warning:
    'This updates the shared Master Company record and may be visible across multiple clients.',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Administration Master Data steward', () => {
  it('searches, shows linked clients, requires a reason, and previews current vs proposed', async () => {
    vi.mocked(dataSteward.fetchStewardMeta).mockResolvedValue({
      company_amend_enabled: true,
      archive_enabled: false,
      delete_enabled: false,
      merge_enabled: false,
      message: 'amend only',
    })
    vi.mocked(dataSteward.searchStewardCompanies).mockResolvedValue([company])
    vi.mocked(dataSteward.fetchStewardCompany).mockResolvedValue(company)
    vi.mocked(dataSteward.fetchStewardRelationshipEvents).mockResolvedValue([])
    vi.mocked(dataSteward.fetchStewardProvenance).mockResolvedValue({
      schema_ready: true,
      newest_first: true,
      events: [
        {
          id: 1,
          entity_type: 'company',
          entity_id: 22,
          field: 'website',
          old_value: '',
          new_value: 'https://edl.example',
          source_type: 'MANUAL_ADMIN',
          changed_by_user_id: 1,
          changed_by_name: 'Julie Magnani',
          changed_at: '2026-09-22T12:00:00Z',
          action: 'AMEND',
          reason: 'Updated website',
        },
      ],
    })
    vi.mocked(dataSteward.previewStewardCompanyAmend).mockResolvedValue({
      company_id: 22,
      current: { website: 'https://edl.example' },
      canonical: { website: 'https://edl.example/new' },
      changes: [
        { field: 'website', current: 'https://edl.example', proposed: 'https://edl.example/new' },
      ],
      noop: false,
      reason: 'Updated website',
      reason_ok: true,
      linked_clients: company.linked_clients || [],
      linked_client_count: 1,
      master_data_warning: company.master_data_warning || '',
      collisions: [],
      blocked: false,
      preview_fingerprint: 'abc',
      expected_updated_at: company.updated_at || '',
      writes: false,
    })
    vi.mocked(dataSteward.saveStewardCompanyAmend).mockResolvedValue({
      company_id: 22,
      changed: ['website'],
      noop: false,
      reason: 'Updated website',
    })

    render(<AdministrationDataSteward />)
    expect(screen.getByRole('heading', { name: 'Master Data' })).toBeTruthy()
    expect(screen.getByText(/Provenance is stored on live/)).toBeTruthy()
    expect(screen.queryByText(/schema is not on live/i)).toBeNull()
    expect((screen.getByRole('button', { name: 'Archive (not yet enabled)' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Merge (not yet enabled)' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Search Master Company'), { target: { value: 'Edl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => {
      expect(dataSteward.searchStewardCompanies).toHaveBeenCalledWith('Edl')
    })
    fireEvent.click(screen.getByRole('button', { name: 'Edl Packaging Engineers' }))
    await waitFor(() => {
      expect(screen.getByText(/MASTER COMPANY DATA/)).toBeTruthy()
    })
    expect(screen.getByText(/Brown Industries/)).toBeTruthy()
    expect(screen.getByText('Julie Magnani')).toBeTruthy()
    expect(screen.getByText('MANUAL_ADMIN')).toBeTruthy()
    expect(screen.getByText('Updated website')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByLabelText('Company name')).toBeTruthy()
    expect(screen.getByLabelText('Reason for change')).toBeTruthy()
    const previewBtn = screen.getByRole('button', { name: 'Preview changes' }) as HTMLButtonElement
    expect(previewBtn.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Website'), { target: { value: 'https://edl.example/new' } })
    fireEvent.change(screen.getByLabelText('Reason for change'), { target: { value: 'Updated website' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => {
      expect(screen.getByText('Review changes')).toBeTruthy()
    })
    expect(screen.getByText('https://edl.example/new')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(dataSteward.saveStewardCompanyAmend).toHaveBeenCalled()
    })
  })

  it('blocks save when a duplicate candidate is returned', async () => {
    vi.mocked(dataSteward.fetchStewardMeta).mockResolvedValue({
      company_amend_enabled: true,
      archive_enabled: false,
      delete_enabled: false,
      merge_enabled: false,
      message: 'amend only',
    })
    vi.mocked(dataSteward.searchStewardCompanies).mockResolvedValue([company])
    vi.mocked(dataSteward.fetchStewardCompany).mockResolvedValue(company)
    vi.mocked(dataSteward.fetchStewardRelationshipEvents).mockResolvedValue([])
    vi.mocked(dataSteward.fetchStewardProvenance).mockResolvedValue({
      schema_ready: true,
      events: [],
    })
    vi.mocked(dataSteward.previewStewardCompanyAmend).mockResolvedValue({
      company_id: 22,
      current: { company_name: 'Edl Packaging Engineers' },
      canonical: { company_name: 'Edl Packaging Engineers' },
      changes: [{ field: 'company_name', current: 'Edl Packaging Engineers', proposed: 'Edl Packaging Engineers' }],
      noop: false,
      reason: 'Corrected company name',
      reason_ok: true,
      linked_clients: company.linked_clients || [],
      linked_client_count: 1,
      master_data_warning: company.master_data_warning || '',
      collisions: [
        {
          company_id: 28,
          company_name: 'Edl Packaging Engineers',
          address: '2 Other',
          city: 'Green Bay',
          state: 'WI',
          zip: '54302',
          phone: '',
          website: '',
          reasons: ['same_name+same_city_state'],
          severity: 'block',
        },
      ],
      blocked: true,
      preview_fingerprint: 'dup',
      expected_updated_at: company.updated_at || '',
      writes: false,
    })

    render(<AdministrationDataSteward />)
    fireEvent.change(screen.getByLabelText('Search Master Company'), { target: { value: 'Edl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => screen.getByRole('button', { name: 'Edl Packaging Engineers' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edl Packaging Engineers' }))
    await waitFor(() => screen.getByRole('button', { name: 'Edit' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Reason for change'), {
      target: { value: 'Corrected company name' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => {
      expect(screen.getByText(/Save is blocked/)).toBeTruthy()
    })
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not treat specialists as administrators', () => {
    const specialist: StaffUser = {
      id: 14,
      email: 'robert@example.test',
      full_name: 'Robert',
      is_administrator: false,
      is_internal_northstar: true,
      active: true,
      created_at: '',
    }
    expect(staffCanAdminister(true, specialist)).toBe(false)
  })

  it('shows Remove From Client and Restore with reason, preview, and confirmation', async () => {
    vi.mocked(dataSteward.fetchStewardMeta).mockResolvedValue({
      company_amend_enabled: true,
      archive_enabled: false,
      delete_enabled: false,
      merge_enabled: false,
      remove_relationship_enabled: true,
      restore_enabled: true,
      message: 'ccr',
    })
    vi.mocked(dataSteward.searchStewardCompanies).mockResolvedValue([company])
    vi.mocked(dataSteward.fetchStewardCompany).mockResolvedValue(company)
    vi.mocked(dataSteward.fetchStewardRelationshipEvents).mockResolvedValue([
      {
        id: 9,
        entity_type: 'client_relationship',
        entity_id: 11,
        field: 'archived_at',
        old_value: '',
        new_value: '2026-09-22T12:00:00Z',
        source_type: 'MANUAL_ADMIN',
        changed_by_user_id: 1,
        changed_by_name: 'Julie Magnani',
        changed_at: '2026-09-22T12:00:00Z',
        action: 'REMOVE_FROM_CLIENT',
        reason: 'Client no longer in Premier book',
        client_name: 'Premier Packaging',
        action_label: 'Removed From Client',
      },
    ])
    vi.mocked(dataSteward.fetchStewardProvenance).mockResolvedValue({
      schema_ready: true,
      events: [],
    })
    vi.mocked(dataSteward.previewStewardRelationshipAction).mockResolvedValue({
      ccr_id: 9,
      company_id: 22,
      company_name: 'Edl Packaging Engineers',
      client_id: 2,
      client_code: 'brown',
      client_name: 'Brown Industries',
      status: 'Working',
      warning:
        'This removes the company from this client\'s active working list. The Master Company and historical data are preserved.',
      effects: ['CCR becomes inactive/archived (same CCR id).'],
      dependencies: [
        { severity: 'warning', code: 'hot', message: 'Hot is preserved on the relationship.' },
      ],
      reason_ok: true,
      preview_fingerprint: 'rel',
      writes: false,
    })
    vi.mocked(dataSteward.confirmStewardRelationshipAction).mockResolvedValue({
      ccr_id: 9,
      archived: true,
      noop: false,
    })

    render(<AdministrationDataSteward />)
    fireEvent.change(screen.getByLabelText('Search Master Company'), { target: { value: 'Edl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => screen.getByRole('button', { name: 'Edl Packaging Engineers' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edl Packaging Engineers' }))
    await waitFor(() => screen.getByRole('button', { name: 'Remove From Client' }))
    expect(screen.getByText('Active')).toBeTruthy()
    expect(screen.getByText('Removed')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Restore' })).toBeTruthy()
    expect(screen.getByLabelText('Show Removed Relationships')).toBeTruthy()
    expect(screen.getByText('Removed From Client')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Archive (not yet enabled)' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Merge (not yet enabled)' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Remove From Client' }))
    const previewBtn = screen.getByRole('button', { name: 'Preview' }) as HTMLButtonElement
    expect(previewBtn.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Relationship reason'), {
      target: { value: 'Client no longer in Brown book' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => {
      expect(screen.getByText(/Master Company and historical data are preserved/)).toBeTruthy()
    })
    expect(screen.getByText(/Warning:/)).toBeTruthy()
    const confirmBtn = screen.getByRole('button', { name: 'Confirm Remove From Client' }) as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)
    fireEvent.click(screen.getByLabelText('Confirm relationship change'))
    fireEvent.click(confirmBtn)
    await waitFor(() => {
      expect(dataSteward.confirmStewardRelationshipAction).toHaveBeenCalled()
    })
  })
})
