import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdministrationImport from './AdministrationImport'
import * as crmImport from './api/crmImport'

vi.mock('./api/crmImport', () => ({
  uploadCrmImport: vi.fn(),
  fetchCrmImportRows: vi.fn(),
  cancelCrmImport: vi.fn(),
}))

const clients = [{ client_id: 7, client_name: 'Carmeco' }]

afterEach(() => {
  cleanup()
  vi.mocked(crmImport.uploadCrmImport).mockReset()
  vi.mocked(crmImport.fetchCrmImportRows).mockReset()
  vi.mocked(crmImport.cancelCrmImport).mockReset()
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
        batch: {
          batch_id: 11,
          client_id: 7,
          status: 'previewed',
          original_filename: 'workbook.xlsx',
          file_type: 'xlsx',
          worksheet_name: 'People',
          file_size_bytes: 12,
          sha256: 'abc',
          headers: ['a', 'b'],
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
          sample_rows: [],
        },
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
    expect(await screen.findByText('Preview ready.')).toBeTruthy()
    expect(crmImport.uploadCrmImport).toHaveBeenCalledTimes(2)
    expect(vi.mocked(crmImport.uploadCrmImport).mock.calls[0][1]).toBe(file)
    expect(vi.mocked(crmImport.uploadCrmImport).mock.calls[0][2]).toBe('')
    expect(vi.mocked(crmImport.uploadCrmImport).mock.calls[1][1]).toBe(file)
    expect(vi.mocked(crmImport.uploadCrmImport).mock.calls[1][2]).toBe('People')
    expect(screen.getByText('Selected file: workbook.xlsx')).toBeTruthy()
    expect(screen.getByText('People')).toBeTruthy()
  })

  it('blocks upload when All My Clients is selected', () => {
    render(
      <AdministrationImport
        allMyClients
        connectClientId={0}
        scopedClientId={0}
        availableClients={clients}
        onScopedClientId={() => undefined}
        clientName=""
      />,
    )
    expect(
      screen.getByText('All My Clients is selected. Choose a specific client before uploading.'),
    ).toBeTruthy()
    expect(screen.queryByLabelText('Company and contact spreadsheet')).toBeNull()
  })
})
