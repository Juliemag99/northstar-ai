import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdministrationClientDataImport from './AdministrationClientDataImport'
import * as clientDataImport from './api/clientDataImport'

vi.mock('./api/clientDataImport', async () => {
  const actual = await vi.importActual<typeof import('./api/clientDataImport')>(
    './api/clientDataImport',
  )
  return {
    ...actual,
    uploadClientDataImport: vi.fn(),
    cancelClientDataImport: vi.fn(),
    saveClientDataImportMapping: vi.fn(),
    dryRunClientDataImport: vi.fn(),
    confirmClientDataImport: vi.fn(),
    retryClientDataImportStagingCleanup: vi.fn(),
  }
})

const clients = [{ client_id: 7, client_name: 'Carmeco' }]

function baseBatch(
  overrides: Partial<clientDataImport.ClientDataImportBatch> = {},
): clientDataImport.ClientDataImportBatch {
  return {
    id: 11,
    batch_id: 11,
    client_id: 7,
    status: 'previewed',
    original_filename: 'prospects.csv',
    history_filename: '',
    headers: ['LeadMaster Record No.', 'Company', 'Email', 'Status'],
    history_headers: [],
    warnings: [],
    error_message: '',
    total_rows: 1,
    source_row_count: 1,
    reusable: true,
    expires_at: '2026-09-09T00:00:00Z',
    uploaded_by_name: 'Admin',
    mapping: {},
    prospects_mapping: {},
    history_mapping: {},
    closed_policy:
      'Closed means closed/not-fit for this selected client. Those records are excluded from this client import by default.',
    ...overrides,
  }
}

function dryRunResponse(
  overrides: Partial<clientDataImport.ClientDataImportDryRunResponse> = {},
): clientDataImport.ClientDataImportDryRunResponse {
  return {
    batch_id: 11,
    client_id: 7,
    plan_fingerprint: 'a'.repeat(64),
    status_catalog: ['New', 'Active', 'Closed'],
    closed_excluded_count: 2,
    closed_policy_notes:
      'Closed means closed/not-fit for this selected client. Those records are excluded from this client import by default. Closed is not treated as deletion from the shared company master.',
    confirm_allowed: true,
    prospects: {
      companies_create: 1,
      companies_reuse: 0,
      companies_possible: 0,
      contacts_create: 1,
      contacts_reuse: 0,
      contacts_possible: 0,
      relationships_create: 1,
      relationships_existing: 0,
      statuses_imported: 1,
      statuses_conflicting: 0,
      statuses_invalid: 0,
      notes_set: 0,
      notes_appended: 0,
      notes_duplicate: 0,
      blocking: 0,
      needs_review: 0,
    },
    history: {
      insert: 0,
      already_present: 0,
      invalid: 0,
      unresolved: 0,
    },
    rows: [],
    ...overrides,
  }
}

async function uploadReadyBatch(
  batchOverrides: Partial<clientDataImport.ClientDataImportBatch> = {},
) {
  const file = new File(['Company\nAcme\n'], 'prospects.csv', { type: 'text/csv' })
  vi.mocked(clientDataImport.uploadClientDataImport).mockResolvedValue({
    kind: 'previewed',
    needs_worksheet: false,
    visible_sheets: [],
    filename: 'prospects.csv',
    file_type: 'csv',
    message: 'Preview ready.',
    batch: baseBatch(batchOverrides),
  })
  render(
    <AdministrationClientDataImport
      allMyClients={false}
      connectClientId={7}
      scopedClientId={7}
      availableClients={clients}
      onScopedClientId={() => undefined}
      clientName="Carmeco"
    />,
  )
  fireEvent.change(screen.getByLabelText('Prospects spreadsheet'), {
    target: { files: [file] },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
  expect(await screen.findByText('Staging preview')).toBeTruthy()
  return file
}

afterEach(() => {
  cleanup()
  vi.mocked(clientDataImport.uploadClientDataImport).mockReset()
  vi.mocked(clientDataImport.cancelClientDataImport).mockReset()
  vi.mocked(clientDataImport.saveClientDataImportMapping).mockReset()
  vi.mocked(clientDataImport.dryRunClientDataImport).mockReset()
  vi.mocked(clientDataImport.confirmClientDataImport).mockReset()
  vi.mocked(clientDataImport.retryClientDataImportStagingCleanup).mockReset()
})

describe('Administration Client Data Import', () => {
  it('blocks All My Clients until a specific client is chosen', () => {
    render(
      <AdministrationClientDataImport
        allMyClients
        connectClientId={0}
        scopedClientId={0}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName=""
      />,
    )
    expect(screen.getByText(/All My Clients is selected/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Upload for preview' })).toBeNull()
    expect(screen.getByLabelText('Client for client data import')).toBeTruthy()
  })

  it('shows Closed policy text and excluded count after dry-run', async () => {
    await uploadReadyBatch({
      mapping: { company_name: 'Company' },
      prospects_mapping: { company_name: 'Company' },
      mapping_updated_at: '2026-09-08T12:00:00Z',
    })
    expect(screen.getByRole('heading', { name: 'Closed policy' })).toBeTruthy()
    expect(screen.getByText(/Closed means closed\/not-fit/)).toBeTruthy()
    expect(screen.getByText(/Excluded Closed count appears after a dry run/)).toBeTruthy()

    vi.mocked(clientDataImport.dryRunClientDataImport).mockResolvedValue(dryRunResponse())
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    expect(await screen.findByText(/Closed records excluded from this import:/)).toBeTruthy()
    expect(
      screen.getByText(/Closed records excluded from this import:/).textContent,
    ).toMatch(/:\s*2/)
  })

  it('keeps Confirm disabled when conflicts remain', async () => {
    await uploadReadyBatch({
      mapping: { company_name: 'Company' },
      prospects_mapping: { company_name: 'Company' },
      mapping_updated_at: 't',
    })
    vi.mocked(clientDataImport.dryRunClientDataImport).mockResolvedValue(
      dryRunResponse({
        confirm_allowed: false,
        prospects: {
          companies_create: 0,
          companies_reuse: 0,
          companies_possible: 1,
          contacts_create: 0,
          contacts_reuse: 0,
          contacts_possible: 0,
          relationships_create: 0,
          relationships_existing: 0,
          statuses_imported: 0,
          statuses_conflicting: 1,
          statuses_invalid: 0,
          notes_set: 0,
          notes_appended: 0,
          notes_duplicate: 0,
          blocking: 0,
          needs_review: 1,
        },
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    expect(await screen.findByText('Dry-run review')).toBeTruthy()
    expect(screen.getByText(/Review is required/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
  })

  it('treats mapping suggestions as draft until Save Mapping', async () => {
    await uploadReadyBatch({
      headers: ['LeadMaster Record No.', 'Company', 'Email'],
      mapping: {},
      prospects_mapping: {},
    })
    expect(await screen.findByText('Column mapping')).toBeTruthy()
    expect((screen.getByLabelText('LeadMaster Record No.') as HTMLSelectElement).value).toBe(
      'LeadMaster Record No.',
    )
    expect((screen.getByLabelText('Company name *') as HTMLSelectElement).value).toBe('Company')
    expect(screen.getByText(/Mapping status:/).textContent).toMatch(/Not saved/)
    expect(
      (screen.getByRole('button', { name: 'Run Dry Run' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(
      (screen.getByRole('button', { name: 'Save Mapping' }) as HTMLButtonElement).disabled,
    ).toBe(false)

    vi.mocked(clientDataImport.saveClientDataImportMapping).mockResolvedValue(
      baseBatch({
        headers: ['LeadMaster Record No.', 'Company', 'Email'],
        mapping: {
          external_record_no: 'LeadMaster Record No.',
          company_name: 'Company',
          contact_email: 'Email',
        },
        prospects_mapping: {
          external_record_no: 'LeadMaster Record No.',
          company_name: 'Company',
          contact_email: 'Email',
        },
        mapping_updated_at: '2026-09-08T12:00:00Z',
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save Mapping' }))
    await waitFor(() => {
      expect(clientDataImport.saveClientDataImportMapping).toHaveBeenCalledWith(
        7,
        11,
        {
          external_record_no: 'LeadMaster Record No.',
          company_name: 'Company',
          contact_email: 'Email',
        },
        undefined,
      )
    })
    expect(await screen.findByText('Mapping saved.')).toBeTruthy()
    expect(screen.getByText(/Mapping status:/).textContent).toMatch(/Saved/)
    expect(
      (screen.getByRole('button', { name: 'Run Dry Run' }) as HTMLButtonElement).disabled,
    ).toBe(false)
    expect(
      (screen.getByRole('button', { name: 'Save Mapping' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })
})
