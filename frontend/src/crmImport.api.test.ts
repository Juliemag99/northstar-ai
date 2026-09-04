import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  confirmCrmImport,
  dryRunCrmImport,
  saveCrmImportMapping,
  type CrmImportBatch,
  type CrmImportConfirmResponse,
  type CrmImportDryRunResponse,
} from './api/crmImport'
import * as http from './api/http'

vi.mock('./api/http', async () => {
  const actual = await vi.importActual<typeof import('./api/http')>('./api/http')
  return {
    ...actual,
    apiFetch: vi.fn(),
  }
})

afterEach(() => {
  vi.mocked(http.apiFetch).mockReset()
})

describe('crmImport mapping and dry-run helpers', () => {
  it('saves mapping through apiFetch with CSRF-capable path and rejects bad ids', async () => {
    const batch: CrmImportBatch = {
      batch_id: 11,
      client_id: 7,
      status: 'previewed',
      original_filename: 'contacts.csv',
      file_type: 'csv',
      worksheet_name: '',
      file_size_bytes: 12,
      sha256: 'abc',
      headers: ['Company'],
      warnings: [],
      error_message: '',
      total_rows: 1,
      source_row_count: 1,
      blank_row_count: 0,
      error_row_count: 0,
      reusable: true,
      expires_at: '',
      created_at: '',
      updated_at: '',
      cancelled_at: '',
      uploaded_by_user_id: 1,
      uploaded_by_name: 'Admin',
      mapping: { company_name: 'Company' },
      mapping_updated_at: 't',
      mapping_updated_by_user_id: 1,
      sample_rows: [],
    }
    vi.mocked(http.apiFetch).mockResolvedValue(
      new Response(JSON.stringify(batch), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const saved = await saveCrmImportMapping(7, 11, { company_name: 'Company' })
    expect(saved.mapping.company_name).toBe('Company')
    expect(http.apiFetch).toHaveBeenCalledWith(
      '/api/clients/7/admin/imports/11/mapping',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ mapping: { company_name: 'Company' } }),
      }),
    )
    await expect(saveCrmImportMapping(0, 11, { company_name: 'Company' })).rejects.toThrow(
      /specific client/i,
    )
    await expect(saveCrmImportMapping(7, 0, { company_name: 'Company' })).rejects.toThrow(
      /batch/i,
    )
  })

  it('posts dry-run through apiFetch and rejects invalid paging', async () => {
    const plan: CrmImportDryRunResponse = {
      batch_id: 11,
      client_id: 7,
      planner_version: 'crm-import-plan-v1',
      plan_fingerprint: 'a'.repeat(64),
      mapping_updated_at: 't',
      total_rows: 1,
      offset: 0,
      limit: 100,
      counts: {
        blocking_error: 0,
        invalid_mapping_data: 0,
        ok: 1,
        create_company: 1,
        use_existing_company: 0,
        possible_company_match: 0,
        create_contact: 0,
        use_existing_contact: 0,
        possible_contact_match: 0,
        insufficient_contact_data: 0,
        no_contact_data: 1,
        contact_deferred: 0,
        create_client_relationship: 0,
        relationship_already_exists: 0,
        relationship_deferred: 1,
        use_default_status: 0,
        preserve_existing_status: 0,
        use_imported_status: 0,
        status_conflict: 0,
        invalid_status: 0,
        no_notes_change: 1,
        set_imported_notes: 0,
        append_imported_notes: 0,
        imported_notes_already_present: 0,
        importable_rows: 1,
        needs_review_rows: 0,
      },
      rows: [],
    }
    vi.mocked(http.apiFetch).mockResolvedValue(
      new Response(JSON.stringify(plan), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const result = await dryRunCrmImport(7, 11, 0, 100)
    expect(result.plan_fingerprint).toHaveLength(64)
    expect(http.apiFetch).toHaveBeenCalledWith(
      '/api/clients/7/admin/imports/11/dry-run',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ offset: 0, limit: 100 }),
      }),
    )
    await expect(dryRunCrmImport(7, 11, -1, 100)).rejects.toThrow(/Invalid paging/)
    await expect(dryRunCrmImport(7, 11, 0, 101)).rejects.toThrow(/Invalid paging/)
  })

  it('posts confirm through apiFetch and rejects invalid ids/fingerprints before request', async () => {
    const result: CrmImportConfirmResponse = {
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
    }
    vi.mocked(http.apiFetch).mockResolvedValue(
      new Response(JSON.stringify(result), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const fingerprint = 'ab'.repeat(32)
    const confirmed = await confirmCrmImport(7, 11, fingerprint)
    expect(confirmed.status).toBe('imported')
    expect(http.apiFetch).toHaveBeenCalledWith(
      '/api/clients/7/admin/imports/11/confirm',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ confirm: true, plan_fingerprint: fingerprint }),
      }),
    )

    const beforeBad = vi.mocked(http.apiFetch).mock.calls.length
    await expect(confirmCrmImport(0, 11, fingerprint)).rejects.toThrow(/specific client/i)
    await expect(confirmCrmImport(7, 0, fingerprint)).rejects.toThrow(/batch/i)
    await expect(confirmCrmImport(7, 11, 'AB'.repeat(32))).rejects.toThrow(/fingerprint/i)
    await expect(confirmCrmImport(7, 11, 'short')).rejects.toThrow(/fingerprint/i)
    expect(vi.mocked(http.apiFetch).mock.calls.length).toBe(beforeBad)
  })
})
