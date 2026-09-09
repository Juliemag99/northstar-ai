import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdministrationImport from './AdministrationImport'
import * as crmImport from './api/crmImport'

vi.mock('./api/crmImport', async () => {
  const actual = await vi.importActual<typeof import('./api/crmImport')>('./api/crmImport')
  return {
    ...actual,
    uploadCrmImport: vi.fn(),
    fetchCrmImportRows: vi.fn(),
    cancelCrmImport: vi.fn(),
    saveCrmImportMapping: vi.fn(),
    dryRunCrmImport: vi.fn(),
    confirmCrmImport: vi.fn(),
    saveCrmImportStatusResolution: vi.fn(),
  }
})

const clients = [{ client_id: 7, client_name: 'Carmeco' }]

function baseBatch(overrides: Partial<crmImport.CrmImportBatch> = {}): crmImport.CrmImportBatch {
  return {
    batch_id: 11,
    client_id: 7,
    status: 'previewed',
    original_filename: 'contacts.csv',
    file_type: 'csv',
    worksheet_name: '',
    file_size_bytes: 12,
    sha256: 'abc',
    headers: ['Company', 'Email', 'First', 'Last'],
    warnings: [],
    error_message: '',
    total_rows: 1,
    source_row_count: 1,
    blank_row_count: 0,
    error_row_count: 0,
    reusable: true,
    expires_at: '2026-09-09T00:00:00Z',
    created_at: '',
    updated_at: '',
    cancelled_at: '',
    uploaded_by_user_id: 1,
    uploaded_by_name: 'Admin',
    mapping: {},
    mapping_updated_at: '',
    mapping_updated_by_user_id: null,
    sample_rows: [],
    ...overrides,
  }
}

function emptyCounts(
  overrides: Partial<crmImport.CrmImportDryRunCounts> = {},
): crmImport.CrmImportDryRunCounts {
  return {
    blocking_error: 0,
    invalid_mapping_data: 0,
    ok: 1,
    create_company: 1,
    use_existing_company: 0,
    possible_company_match: 0,
    create_contact: 1,
    use_existing_contact: 0,
    possible_contact_match: 0,
    insufficient_contact_data: 0,
    no_contact_data: 0,
    contact_deferred: 0,
    create_client_relationship: 1,
    relationship_already_exists: 0,
    relationship_deferred: 0,
    use_default_status: 1,
    preserve_existing_status: 0,
    use_imported_status: 0,
    status_conflict: 0,
    invalid_status: 0,
    update_existing_status: 0,
    no_notes_change: 1,
    set_imported_notes: 0,
    append_imported_notes: 0,
    imported_notes_already_present: 0,
    importable_rows: 1,
    needs_review_rows: 0,
    ...overrides,
  }
}

function dryRunResponse(
  overrides: Partial<crmImport.CrmImportDryRunResponse> = {},
): crmImport.CrmImportDryRunResponse {
  return {
    batch_id: 11,
    client_id: 7,
    planner_version: 'crm-import-plan-v1',
    plan_fingerprint: 'a'.repeat(64),
    mapping_updated_at: '2026-09-03T12:00:00',
    total_rows: 1,
    offset: 0,
    limit: 100,
    use_imported_status_for_existing: false,
    counts: emptyCounts(),
    rows: [
      {
        row_id: 1,
        source_row_number: 2,
        validity: 'ok',
        validity_detail: '',
        mapped: {
          company_name: 'Acme',
          contact_email: 'ada@example.test',
          contact_first_name: 'Ada',
          contact_last_name: 'Lovelace',
        },
        company: {
          action: 'create_company',
          reasons: [],
          company_id: null,
          proposed_key: 'proposed:company:2:1',
          created_at_source_row: 2,
          name: 'Acme',
          possibles: [],
        },
        contact: {
          action: 'create_contact',
          reasons: [],
          contact_id: null,
          proposed_key: 'proposed:contact:2:1',
          created_at_source_row: 2,
          display_name: 'Ada Lovelace',
          possibles: [],
        },
        relationship: {
          action: 'create_client_relationship',
          relationship_id: null,
          proposed_key: 'proposed:relationship:2:1',
          status_action: 'use_default_status',
          notes_action: 'no_notes_change',
          resolved_status: 'New',
          original_status_action: 'use_default_status',
          status_resolution_type: '',
          existing_status: '',
          needs_status_resolution: false,
        },
      },
    ],
    status_catalog: ['New', 'Active', 'Contacted'],
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.mocked(crmImport.uploadCrmImport).mockReset()
  vi.mocked(crmImport.fetchCrmImportRows).mockReset()
  vi.mocked(crmImport.cancelCrmImport).mockReset()
  vi.mocked(crmImport.saveCrmImportMapping).mockReset()
  vi.mocked(crmImport.dryRunCrmImport).mockReset()
  vi.mocked(crmImport.confirmCrmImport).mockReset()
  vi.mocked(crmImport.saveCrmImportStatusResolution).mockReset()
})

describe('Administration import tab', () => {
  it('keeps the selected File and resubmits after worksheet choice', async () => {
    const file = new File(['a,b\n1,2\n'], 'workbook.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    vi.mocked(crmImport.uploadCrmImport)
      .mockResolvedValueOnce({
        kind: 'needs_worksheet',
        needs_worksheet: true,
        visible_sheets: ['Accounts', 'People'],
        filename: 'workbook.xlsx',
        file_type: 'xlsx',
        message: 'This workbook has multiple sheets. Choose one visible sheet to continue.',
        batch: null,
      })
      .mockResolvedValueOnce({
        kind: 'previewed',
        needs_worksheet: false,
        visible_sheets: [],
        filename: 'workbook.xlsx',
        file_type: 'xlsx',
        message: 'Preview ready.',
        batch: baseBatch({
          original_filename: 'workbook.xlsx',
          file_type: 'xlsx',
          worksheet_name: 'People',
          headers: ['a', 'b'],
        }),
      })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [
        {
          row_id: 1,
          source_row_number: 2,
          values: { a: '1', b: '2' },
          warnings: [],
          errors: [],
          has_blocking_error: false,
        },
      ],
    })

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )

    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    expect(await screen.findByLabelText('Visible worksheet')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Visible worksheet'), {
      target: { value: 'People' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue with selected sheet' }))
    expect(await screen.findByText('Staging preview')).toBeTruthy()
    expect(crmImport.uploadCrmImport).toHaveBeenCalledTimes(2)
    expect(crmImport.uploadCrmImport).toHaveBeenLastCalledWith(7, file, 'People')
  })

  it('wraps source preview and dry-run tables in horizontal scroll containers', async () => {
    const file = new File(['Company\nAcme\n'], 'scroll.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'scroll.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company'],
        mapping: { company_name: 'Company' },
        mapping_updated_at: 't',
        mapping_updated_by_user_id: 1,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [
        {
          row_id: 1,
          source_row_number: 2,
          values: { Company: 'Acme' },
          warnings: [],
          errors: [],
          is_blank: false,
          has_blocking_error: false,
        },
      ],
    })
    vi.mocked(crmImport.dryRunCrmImport).mockResolvedValue(dryRunResponse())

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Staging preview')
    const previewScroll = screen.getByLabelText('Source preview table')
    expect(previewScroll.className).toContain('administration-import-table-scroll')
    expect(previewScroll.querySelector('table.administration-import-preview-table')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    await screen.findByText('Dry-run review')
    const dryScroll = screen.getByLabelText('Dry-run results table')
    expect(dryScroll.className).toContain('administration-import-table-scroll')
    expect(dryScroll.querySelector('table.administration-import-dry-run-table')).toBeTruthy()
  })

  it('suggests draft mapping for a new upload and does not overwrite saved mapping', async () => {
    const file = new File(['Company,Email\nAcme,a@x.test\n'], 'contacts.csv', {
      type: 'text/csv',
    })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'contacts.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company', 'Email'],
        mapping: {},
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    expect(await screen.findByText('Column mapping')).toBeTruthy()
    expect((screen.getByLabelText('Company name *') as HTMLSelectElement).value).toBe(
      'Company',
    )
    expect((screen.getByLabelText('Contact email') as HTMLSelectElement).value).toBe('Email')
    expect(screen.getByText(/Mapping status:/).textContent).toMatch(/Not saved/)
  })

  it('loads an existing saved mapping without replacing it with suggestions', async () => {
    const file = new File(['Company Name,Email\nx,y\n'], 'saved.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'saved.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company Name', 'Email', 'First'],
        mapping: { company_name: 'Company Name', contact_email: 'Email' },
        mapping_updated_at: '2026-09-03T10:00:00',
        mapping_updated_by_user_id: 1,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    expect(await screen.findByText(/Mapping status:/)).toBeTruthy()
    expect((screen.getByLabelText('Company name *') as HTMLSelectElement).value).toBe(
      'Company Name',
    )
    expect((screen.getByLabelText('Contact email') as HTMLSelectElement).value).toBe('Email')
    // First was not in saved mapping; suggestions must not overwrite saved baseline.
    expect((screen.getByLabelText('Contact first name') as HTMLSelectElement).value).toBe('')
    expect(screen.getByText(/Mapping status:/).textContent).toMatch(/Saved/)
  })

  it('validates mapping conflicts before enabling save', async () => {
    const file = new File(['Company,Name,First\na,b,c\n'], 'map.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'map.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({ headers: ['Company', 'Name', 'First'], mapping: {} }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')

    fireEvent.change(screen.getByLabelText('Contact full name'), {
      target: { value: 'Name' },
    })
    fireEvent.change(screen.getByLabelText('Contact first name'), {
      target: { value: 'First' },
    })
    expect(
      screen.getByText(
        'Use either a full name column or first and last name columns, not both.',
      ),
    ).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Save Mapping' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('saves mapping through the helper and clears dry-run on later edits', async () => {
    const file = new File(['Company,Email\nAcme,a@x.test\n'], 'contacts.csv', {
      type: 'text/csv',
    })
    const uploaded = baseBatch({
      headers: ['Company', 'Email'],
      mapping: {},
    })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'contacts.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: uploaded,
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })
    vi.mocked(crmImport.saveCrmImportMapping).mockResolvedValue(
      baseBatch({
        headers: ['Company', 'Email'],
        mapping: { company_name: 'Company', contact_email: 'Email' },
        mapping_updated_at: '2026-09-03T12:00:00',
        mapping_updated_by_user_id: 1,
      }),
    )
    vi.mocked(crmImport.dryRunCrmImport).mockResolvedValue(dryRunResponse())

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    fireEvent.click(screen.getByRole('button', { name: 'Save Mapping' }))
    expect(await screen.findByText('Mapping saved.')).toBeTruthy()
    expect(crmImport.saveCrmImportMapping).toHaveBeenCalledWith(7, 11, {
      company_name: 'Company',
      contact_email: 'Email',
    })

    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    expect(
      await screen.findByText('Dry run complete. All rows are ready for the confirmation step.'),
    ).toBeTruthy()
    expect(crmImport.dryRunCrmImport).toHaveBeenCalledWith(7, 11, 0, 100, false)
    expect(screen.getByRole('button', { name: 'Confirm Import' })).toBeTruthy()
    expect(screen.queryByText(/raw_json/i)).toBeNull()

    fireEvent.change(screen.getByLabelText('Contact email'), { target: { value: '' } })
    expect(screen.queryByText('Dry-run review')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
    expect(crmImport.dryRunCrmImport.mock.calls.length).toBe(1)
  })

  it('shows needs-review messaging and full-batch counts with paginated rows', async () => {
    const file = new File(['Company\nAcme\n'], 'n.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'n.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company'],
        mapping: { company_name: 'Company' },
        mapping_updated_at: 't',
        mapping_updated_by_user_id: 1,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })
    vi.mocked(crmImport.dryRunCrmImport).mockResolvedValue(
      dryRunResponse({
        total_rows: 120,
        counts: emptyCounts({
          importable_rows: 100,
          needs_review_rows: 20,
          possible_company_match: 20,
          ok: 100,
        }),
        rows: [
          {
            row_id: 1,
            source_row_number: 2,
            validity: 'ok',
            validity_detail: '',
            mapped: { company_name: 'Acme' },
            company: {
              action: 'possible_company_match',
              reasons: ['name_exact'],
              company_id: null,
              proposed_key: null,
              created_at_source_row: null,
              name: 'Acme',
              possibles: [
                {
                  company_id: 9,
                  company_name: 'Acme Inc',
                  external_record_no: 'NS-9',
                  reasons: ['name_exact'],
                },
              ],
            },
            contact: {
              action: 'deferred',
              reasons: [],
              contact_id: null,
              proposed_key: null,
              created_at_source_row: null,
              display_name: '',
              possibles: [],
            },
            relationship: {
              action: 'deferred',
              relationship_id: null,
              proposed_key: null,
              status_action: 'none',
              notes_action: 'none',
              resolved_status: '',
              original_status_action: 'none',
              status_resolution_type: '',
              existing_status: '',
              needs_status_resolution: false,
            },
          },
        ],
      }),
    )

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    expect(
      await screen.findByText('Review is required before this batch can be imported.'),
    ).toBeTruthy()
    const counts = screen.getByLabelText('Full-batch dry-run counts')
    expect(within(counts).getByText('Importable rows').parentElement?.textContent).toMatch(
      /Importable rows\s*100/,
    )
    expect(within(counts).getByText('Needs review').parentElement?.textContent).toMatch(
      /Needs review\s*20/,
    )
    expect(screen.getByText('Possible company match')).toBeTruthy()
    expect(screen.getByText(/Acme Inc \(NS-9\): Exact name/)).toBeTruthy()
    expect(screen.getByText(/complete batch \(120 rows\)/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
  })

  it('invalidates the plan when a later page returns a different fingerprint', async () => {
    const file = new File(['Company\nAcme\n'], 'n.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'n.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company'],
        mapping: { company_name: 'Company' },
        mapping_updated_at: 't',
        mapping_updated_by_user_id: 1,
        total_rows: 150,
        source_row_count: 150,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 150,
      rows: [],
    })
    const first = dryRunResponse({
      total_rows: 150,
      plan_fingerprint: 'a'.repeat(64),
      counts: emptyCounts({ importable_rows: 150 }),
    })
    const second = dryRunResponse({
      total_rows: 150,
      offset: 100,
      plan_fingerprint: 'b'.repeat(64),
      counts: emptyCounts({ importable_rows: 150 }),
    })
    vi.mocked(crmImport.dryRunCrmImport)
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second)

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    expect(await screen.findByText('Dry-run review')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Next plan page' }))
    expect(
      await screen.findByText(
        'The dry-run plan changed. Run Dry Run again from the first page.',
      ),
    ).toBeTruthy()
    expect(screen.queryByText('Dry-run review')).toBeNull()
  })

  it('does not fetch rows or dry-run for terminal imported batches', async () => {
    const file = new File(['Company\nAcme\n'], 'n.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'n.csv',
      file_type: 'csv',
      message: 'Imported already.',
      batch: baseBatch({
        status: 'imported',
        reusable: false,
        headers: [],
        mapping: {},
      }),
    })

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    expect(
      await screen.findByText(
        'This batch was imported. Mapping and dry-run are no longer available.',
      ),
    ).toBeTruthy()
    expect(crmImport.fetchCrmImportRows).not.toHaveBeenCalled()
    expect(crmImport.dryRunCrmImport).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Save Mapping' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Run Dry Run' })).toBeNull()
    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull()
  })

  it('clears state when the active client changes and never calls with client 0', async () => {
    const file = new File(['Company\nAcme\n'], 'n.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'n.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company'],
        mapping: { company_name: 'Company' },
        mapping_updated_at: 't',
        mapping_updated_by_user_id: 1,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })

    const { rerender } = render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    expect(await screen.findByText('Staging preview')).toBeTruthy()

    rerender(
      <AdministrationImport
        allMyClients
        connectClientId={0}
        scopedClientId={0}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName=""
      />,
    )
    expect(screen.queryByText('Staging preview')).toBeNull()
    expect(screen.getByText(/Choose a specific client before uploading/)).toBeTruthy()
    const callsBefore = crmImport.uploadCrmImport.mock.calls.length
    expect(screen.queryByRole('button', { name: 'Upload for preview' })).toBeNull()
    expect(crmImport.uploadCrmImport.mock.calls.length).toBe(callsBefore)
  })

  it('surfaces 409 terminal mapping errors without calling confirm', async () => {
    const file = new File(['Company\nAcme\n'], 'n.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'n.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({ headers: ['Company'], mapping: {} }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })
    const err = new Error('This import batch can no longer be previewed.') as Error & {
      status?: number
    }
    err.status = 409
    vi.mocked(crmImport.saveCrmImportMapping).mockRejectedValue(err)

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    fireEvent.click(screen.getByRole('button', { name: 'Save Mapping' }))
    expect(
      await screen.findByText('This import batch can no longer be previewed.'),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull()
  })

  async function renderReadyToConfirm(plan = dryRunResponse()) {
    const file = new File(['Company\nAcme\n'], 'n.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'n.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company'],
        mapping: { company_name: 'Company' },
        mapping_updated_at: 't',
        mapping_updated_by_user_id: 1,
        total_rows: plan.total_rows,
        source_row_count: plan.total_rows,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: plan.total_rows,
      rows: [],
    })
    vi.mocked(crmImport.dryRunCrmImport).mockResolvedValue(plan)
    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    await screen.findByRole('button', { name: 'Confirm Import' })
    return file
  }

  it('shows no confirm control before a successful all-ready dry run', async () => {
    const file = new File(['Company\nAcme\n'], 'n.csv', { type: 'text/csv' })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'n.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company'],
        mapping: { company_name: 'Company' },
        mapping_updated_at: 't',
        mapping_updated_by_user_id: 1,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })
    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
  })

  it('shows relationship status/notes mapping and blocks confirm on status conflict', async () => {
    const file = new File(['Company,Status,Notes\nAcme,Active,Hello\n'], 'sn.csv', {
      type: 'text/csv',
    })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'sn.csv',
      file_type: 'csv',
      message: 'Preview ready.',
      batch: baseBatch({
        headers: ['Company', 'Status', 'Notes'],
        mapping: {
          company_name: 'Company',
          relationship_status: 'Status',
          relationship_notes: 'Notes',
        },
        mapping_updated_at: 't',
        mapping_updated_by_user_id: 1,
      }),
    })
    vi.mocked(crmImport.fetchCrmImportRows).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      offset: 0,
      limit: 25,
      total: 1,
      rows: [],
    })
    vi.mocked(crmImport.dryRunCrmImport).mockResolvedValue(
      dryRunResponse({
        counts: emptyCounts({
          importable_rows: 0,
          needs_review_rows: 1,
          create_client_relationship: 0,
          relationship_already_exists: 1,
          use_default_status: 0,
          status_conflict: 1,
          no_notes_change: 0,
          append_imported_notes: 1,
          ok: 0,
        }),
        rows: [
          {
            row_id: 1,
            source_row_number: 2,
            validity: 'ok',
            validity_detail: '',
            mapped: {
              company_name: 'Acme',
              relationship_status: 'Active',
              relationship_notes: 'Hello world note that must not appear in full',
            },
            company: {
              action: 'use_existing_company',
              reasons: ['name_exact'],
              company_id: 9,
              proposed_key: null,
              created_at_source_row: null,
              name: 'Acme',
              possibles: [],
            },
            contact: {
              action: 'no_contact_data',
              reasons: [],
              contact_id: null,
              proposed_key: null,
              created_at_source_row: null,
              display_name: '',
              possibles: [],
            },
            relationship: {
              action: 'relationship_already_exists',
              relationship_id: 3,
              proposed_key: null,
              status_action: 'status_conflict',
              notes_action: 'append_imported_notes',
              resolved_status: '',
              original_status_action: 'status_conflict',
              status_resolution_type: '',
              existing_status: 'New',
              needs_status_resolution: true,
            },
          },
        ],
        status_catalog: [
          'Contacted',
          'Disqualified-Not a good fit-No relevant work',
          'Disqualified-Production/Packed Outside US',
          'Good Fit-But no projects at this Time',
          'Left Message',
          'New',
          'Send Information',
        ],
      }),
    )

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Carmeco"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    expect((screen.getByLabelText('Relationship status') as HTMLSelectElement).value).toBe(
      'Status',
    )
    expect((screen.getByLabelText('Relationship notes') as HTMLSelectElement).value).toBe('Notes')
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    expect(
      await screen.findByText('Review is required before this batch can be imported.'),
    ).toBeTruthy()
    const counts = screen.getByLabelText('Full-batch dry-run counts')
    expect(within(counts).getByText('Status conflicts / invalid').parentElement?.textContent).toMatch(
      /Status conflicts \/ invalid\s*1 \/ 0/,
    )
    expect(screen.getByText('Status conflict')).toBeTruthy()
    expect(screen.getByText('Append imported notes')).toBeTruthy()
    expect(screen.getByLabelText('Resolve status')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Save Resolution' })).toBeTruthy()
    const resolveSelect = screen.getByLabelText(
      'Resolve status for source row 2',
    ) as HTMLSelectElement
    const optionLabels = Array.from(resolveSelect.options).map((o) => o.textContent || '')
    expect(optionLabels.some((t) => t.includes('Keep existing status'))).toBe(true)
    expect(optionLabels.some((t) => t.includes('Replace with a client status'))).toBe(true)
    expect(screen.queryByText('Hello world note that must not appear in full')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
    expect(document.body.innerHTML).not.toContain('dangerouslySetInnerHTML')
  })

  it('saves status resolution, reruns dry run, and enables confirm when review is clear', async () => {
    const file = new File(['Company,Status\nAcme,Disqualified\n'], 'sn.csv', {
      type: 'text/csv',
    })
    vi.mocked(crmImport.uploadCrmImport).mockResolvedValue({
      kind: 'previewed',
      needs_worksheet: false,
      visible_sheets: [],
      filename: 'sn.csv',
      file_type: 'csv',
      message: '',
      batch: baseBatch({
        headers: ['Company', 'Status'],
        mapping: {
          company_name: 'Company',
          relationship_status: 'Status',
        },
        mapping_updated_at: '2026-09-04T12:00:00',
        mapping_updated_by_user_id: 1,
      }),
    })
    const needsReview = dryRunResponse({
      counts: emptyCounts({
        importable_rows: 0,
        needs_review_rows: 1,
        create_client_relationship: 1,
        use_default_status: 0,
        invalid_status: 1,
        ok: 0,
      }),
      rows: [
        {
          row_id: 44,
          source_row_number: 2,
          validity: 'ok',
          validity_detail: '',
          mapped: { company_name: 'Acme', relationship_status: 'Disqualified' },
          company: {
            action: 'create_company',
            reasons: [],
            company_id: null,
            proposed_key: 'proposed:company:2:44',
            created_at_source_row: 2,
            name: 'Acme',
            possibles: [],
          },
          contact: {
            action: 'no_contact_data',
            reasons: [],
            contact_id: null,
            proposed_key: null,
            created_at_source_row: null,
            display_name: '',
            possibles: [],
          },
          relationship: {
            action: 'create_client_relationship',
            relationship_id: null,
            proposed_key: 'proposed:relationship:2:44',
            status_action: 'invalid_status',
            notes_action: 'no_notes_change',
            resolved_status: '',
            original_status_action: 'invalid_status',
            status_resolution_type: '',
            existing_status: '',
            needs_status_resolution: true,
          },
        },
      ],
      status_catalog: [
        'Contacted',
        'Disqualified-Not a good fit-No relevant work',
        'New',
      ],
      plan_fingerprint: 'b'.repeat(64),
    })
    const resolved = dryRunResponse({
      counts: emptyCounts({
        importable_rows: 1,
        needs_review_rows: 0,
        use_imported_status: 1,
        use_default_status: 0,
      }),
      rows: [
        {
          ...needsReview.rows[0],
          relationship: {
            ...needsReview.rows[0].relationship,
            status_action: 'use_imported_status',
            resolved_status: 'Disqualified-Not a good fit-No relevant work',
            original_status_action: 'invalid_status',
            status_resolution_type: 'replace_with_status',
            needs_status_resolution: false,
          },
        },
      ],
      status_catalog: needsReview.status_catalog,
      plan_fingerprint: 'c'.repeat(64),
    })
    vi.mocked(crmImport.dryRunCrmImport)
      .mockResolvedValueOnce(needsReview)
      .mockResolvedValueOnce(resolved)
    vi.mocked(crmImport.saveCrmImportStatusResolution).mockResolvedValue({
      client_id: 7,
      batch_id: 11,
      staged_row_id: 44,
      cleared: false,
      resolution: {
        resolution_type: 'replace_with_status',
        resolved_status: 'Disqualified-Not a good fit-No relevant work',
        updated_at: '2026-09-04T12:00:00Z',
        updated_by_user_id: 1,
      },
    })

    render(
      <AdministrationImport
        allMyClients={false}
        connectClientId={7}
        scopedClientId={7}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName="Brown Industries"
      />,
    )
    fireEvent.change(screen.getByLabelText('Company and contact spreadsheet'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for preview' }))
    await screen.findByText('Column mapping')
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    expect(await screen.findByText('Invalid status')).toBeTruthy()
    const statusSelect = screen.getByLabelText(
      'Replacement status for source row 2',
    ) as HTMLSelectElement
    expect(
      Array.from(statusSelect.options).map((o) => o.value),
    ).toEqual([
      '',
      'Contacted',
      'Disqualified-Not a good fit-No relevant work',
      'New',
    ])
    fireEvent.change(statusSelect, {
      target: { value: 'Disqualified-Not a good fit-No relevant work' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save Resolution' }))
    await screen.findByText(/Resolved: replace with/)
    expect(crmImport.saveCrmImportStatusResolution).toHaveBeenCalledWith(7, 11, 44, {
      resolution_type: 'replace_with_status',
      resolved_status: 'Disqualified-Not a good fit-No relevant work',
      clear: false,
    })
    expect(crmImport.dryRunCrmImport).toHaveBeenCalledTimes(2)
    expect(crmImport.dryRunCrmImport).toHaveBeenLastCalledWith(7, 11, 0, 100, false)
    expect(screen.getByRole('button', { name: 'Confirm Import' })).toBeTruthy()
  })

  it('opens an accessible confirm dialog with client and counts, Cancel restores focus', async () => {
    await renderReadyToConfirm(
      dryRunResponse({
        total_rows: 116,
        counts: emptyCounts({
          importable_rows: 116,
          create_company: 40,
          use_existing_company: 76,
          create_contact: 90,
          use_existing_contact: 20,
          no_contact_data: 6,
          create_client_relationship: 100,
          relationship_already_exists: 16,
        }),
      }),
    )
    const openBtn = screen.getByRole('button', { name: 'Confirm Import' })
    openBtn.focus()
    fireEvent.click(openBtn)
    const dialog = await screen.findByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(screen.getByRole('heading', { name: 'Import 116 rows for Carmeco?' })).toBeTruthy()
    expect(within(dialog).getByText('40')).toBeTruthy()
    expect(within(dialog).getByLabelText('Confirm import summary')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => {
      expect(document.activeElement).toBe(
        screen.getByRole('button', { name: 'Confirm Import' }),
      )
    })
  })

  it('closes the confirm dialog on Escape and backdrop when idle', async () => {
    await renderReadyToConfirm()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
    fireEvent.click(screen.getByRole('dialog').parentElement as HTMLElement)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('confirms once, shows completion counters, and hides mapping/dry-run/confirm', async () => {
    await renderReadyToConfirm(dryRunResponse({ plan_fingerprint: 'ab'.repeat(32) }))
    let release!: (value: crmImport.CrmImportConfirmResponse) => void
    const pending = new Promise<crmImport.CrmImportConfirmResponse>((resolve) => {
      release = resolve
    })
    vi.mocked(crmImport.confirmCrmImport).mockReturnValue(pending)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }))
    const dialog = await screen.findByRole('dialog')
    const confirmAction = within(dialog).getByRole('button', { name: 'Confirm' })
    fireEvent.click(confirmAction)
    fireEvent.click(confirmAction)
    expect(crmImport.confirmCrmImport).toHaveBeenCalledTimes(1)
    expect(within(dialog).getByRole('button', { name: 'Importing…' })).toBeTruthy()
    expect(
      (within(dialog).getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled,
    ).toBe(true)

    release({
      batch_id: 11,
      client_id: 7,
      status: 'imported',
      imported_at: '2026-09-03T18:00:00Z',
      imported_by_user_id: 1,
      confirmed_plan_fingerprint: 'ab'.repeat(32),
      created_company_count: 1,
      reused_company_count: 0,
      created_contact_count: 1,
      reused_contact_count: 0,
      created_relationship_count: 1,
      existing_relationship_count: 0,
      no_contact_row_count: 0,
      total_imported_row_count: 1,
      imported_status_count: 0,
      default_status_count: 1,
      preserved_status_count: 0,
      notes_set_count: 0,
      notes_appended_count: 0,
      notes_duplicate_count: 0,
      notes_unchanged_count: 1,
    })
    expect(await screen.findByText('Import completed')).toBeTruthy()
    expect(crmImport.confirmCrmImport).toHaveBeenCalledWith(7, 11, 'ab'.repeat(32), false)
    expect(screen.getByLabelText('Import completion summary').textContent).toMatch(/Imported rows\s*1/)
    expect(screen.getByLabelText('Import completion summary').textContent).toMatch(/Default statuses\s*1/)
    expect(screen.queryByText('Column mapping')).toBeNull()
    expect(screen.queryByText('Dry-run review')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel staged import' })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    const rowFetchesAfter = vi.mocked(crmImport.fetchCrmImportRows).mock.calls.length
    expect(rowFetchesAfter).toBe(1)
    expect(crmImport.uploadCrmImport).toHaveBeenCalledTimes(1)
    expect(crmImport.confirmCrmImport).toHaveBeenCalledTimes(1)
  })

  it('clears eligibility on stale 409 and retains dry-run after 500 for retry', async () => {
    await renderReadyToConfirm()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }))
    await screen.findByRole('dialog')
    const stale = new Error('Plan fingerprint is stale.') as Error & { status?: number }
    stale.status = 409
    vi.mocked(crmImport.confirmCrmImport).mockRejectedValueOnce(stale)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(
      await screen.findByText(/Plan fingerprint is stale\. Run Dry Run again before confirming\./),
    ).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
    expect(screen.queryByText('Dry-run review')).toBeNull()

    vi.mocked(crmImport.dryRunCrmImport).mockResolvedValue(dryRunResponse())
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    await screen.findByRole('button', { name: 'Confirm Import' })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }))
    await screen.findByRole('dialog')
    const server = new Error('boom') as Error & { status?: number }
    server.status = 500
    vi.mocked(crmImport.confirmCrmImport).mockRejectedValueOnce(server)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(
      await screen.findByText('Unexpected failure. Nothing was imported. You can safely retry.'),
    ).toBeTruthy()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText('Dry-run review')).toBeTruthy()
    expect(screen.queryByText('Import completed')).toBeNull()
  })

  it('locks actions on terminal confirm 409', async () => {
    await renderReadyToConfirm()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }))
    await screen.findByRole('dialog')
    const terminal = new Error('This import batch was already imported.') as Error & {
      status?: number
    }
    terminal.status = 409
    vi.mocked(crmImport.confirmCrmImport).mockRejectedValueOnce(terminal)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByText('This import batch was already imported.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save Mapping' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()
  })

  it('defaults status overwrite off and forwards the option on dry-run and confirm', async () => {
    const plan = dryRunResponse({
      use_imported_status_for_existing: true,
      counts: emptyCounts({
        create_company: 0,
        use_existing_company: 1,
        create_client_relationship: 0,
        relationship_already_exists: 1,
        use_default_status: 0,
        use_imported_status: 1,
        update_existing_status: 1,
      }),
    })
    await renderReadyToConfirm(plan)
    const checkbox = screen.getByRole('checkbox', {
      name: /Use imported nonblank statuses for existing relationships/i,
    }) as HTMLInputElement
    expect(checkbox.checked).toBe(false)
    const updatesRow = screen.getByText('Existing relationship status updates').closest('div')
    expect(updatesRow).toBeTruthy()
    expect(within(updatesRow as HTMLElement).getByText('1')).toBeTruthy()

    fireEvent.click(checkbox)
    expect(checkbox.checked).toBe(true)
    expect(screen.queryByRole('button', { name: 'Confirm Import' })).toBeNull()

    vi.mocked(crmImport.dryRunCrmImport).mockResolvedValue(plan)
    fireEvent.click(screen.getByRole('button', { name: 'Run Dry Run' }))
    await screen.findByRole('button', { name: 'Confirm Import' })
    expect(crmImport.dryRunCrmImport).toHaveBeenLastCalledWith(7, 11, 0, 100, true)

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }))
    await screen.findByRole('dialog')
    expect(
      screen.getByText(/Imported nonblank statuses will replace existing relationship statuses/i),
    ).toBeTruthy()
    vi.mocked(crmImport.confirmCrmImport).mockResolvedValue({
      batch_id: 11,
      client_id: 7,
      status: 'imported',
      imported_at: 't',
      imported_by_user_id: 1,
      confirmed_plan_fingerprint: 'ab'.repeat(32),
      created_company_count: 0,
      reused_company_count: 1,
      created_contact_count: 0,
      reused_contact_count: 0,
      created_relationship_count: 0,
      existing_relationship_count: 1,
      no_contact_row_count: 1,
      total_imported_row_count: 1,
      imported_status_count: 1,
      default_status_count: 0,
      preserved_status_count: 0,
      notes_set_count: 0,
      notes_appended_count: 0,
      notes_duplicate_count: 0,
      notes_unchanged_count: 1,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => {
      expect(crmImport.confirmCrmImport).toHaveBeenCalledWith(7, 11, 'a'.repeat(64), true)
    })
  })
})
