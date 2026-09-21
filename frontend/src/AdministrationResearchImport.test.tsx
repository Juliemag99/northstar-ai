import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdministrationResearchImport from './AdministrationResearchImport'
import * as researchImport from './api/researchImport'

vi.mock('./api/researchImport', async () => {
  const actual = await vi.importActual<typeof import('./api/researchImport')>('./api/researchImport')
  return {
    ...actual,
    uploadResearchImport: vi.fn(),
    saveResearchImportMapping: vi.fn(),
    dryRunResearchImport: vi.fn(),
    saveResearchMatchResolution: vi.fn(),
  }
})

const clients = [{ client_id: 4, client_name: 'Premier Manufacturing' }]

function batch(overrides: Partial<ReturnType<typeof jancoBatch>> = {}) {
  return { ...jancoBatch(), ...overrides }
}

function jancoBatch() {
  return {
    batch_id: 9,
    client_id: 4,
    batch_name: 'Janco preview',
    status: 'previewed',
    original_filename: 'janco.csv',
    file_type: 'csv',
    worksheet_name: 'CSV',
    sha256: 'abc123sha',
    research_method: 'CHATGPT_DEEP_RESEARCH',
    research_date: '2026-09-18',
    source_type: 'CHATGPT_DEEP_RESEARCH',
    headers: ['Company Name', 'Why Janco Fits', 'Priority'],
    warnings: [],
    source_row_count: 1,
    mapping: { company_name: 'Company Name', why_client_fits: 'Why Janco Fits', research_priority: 'Priority' },
    suggested_mapping: { company_name: 'Company Name' },
    sample_rows: [{ 'Company Name': 'Harvest International', 'Why Janco Fits': 'Makes planters', Priority: 'A - Iowa' }],
    column_map: [
      {
        header: 'Company Name',
        samples: ['Harvest International'],
        suggested_field: 'company_name',
        suggested_category: 'MASTER_COMPANY',
        selected_field: 'company_name',
        selected_category: 'MASTER_COMPANY',
        custom_key: '',
        custom_label: '',
        value_type: 'TEXT',
        ignored: false,
        workflow_sensitive: false,
        warning: '',
      },
      {
        header: 'Why Janco Fits',
        samples: ['Makes planters'],
        suggested_field: 'why_client_fits',
        suggested_category: 'CLIENT_PROSPECT',
        selected_field: 'why_client_fits',
        selected_category: 'CLIENT_PROSPECT',
        custom_key: '',
        custom_label: '',
        value_type: 'TEXT',
        ignored: false,
        workflow_sensitive: false,
        warning: '',
      },
      {
        header: 'Priority',
        samples: ['A - Iowa'],
        suggested_field: 'research_priority',
        suggested_category: 'CLIENT_PROSPECT',
        selected_field: 'research_priority',
        selected_category: 'CLIENT_PROSPECT',
        custom_key: '',
        custom_label: '',
        value_type: 'TEXT',
        ignored: false,
        workflow_sensitive: false,
        warning: '',
      },
    ],
    production_confirm_enabled: false,
    product_name: 'Research & Custom Prospect Import',
  }
}

function preview(overrides: Record<string, unknown> = {}) {
  return {
    batch: batch(),
    plan_fingerprint: 'a'.repeat(64),
    counts: {
      source_rows: 1,
      valid_rows: 1,
      existing_companies: 0,
      strong_matches: 0,
      new_companies: 1,
      new_locations: 0,
      possible_matches: 0,
      ambiguous: 1,
      invalid: 0,
      existing_ccrs: 0,
      new_ccrs: 1,
      research_rows_to_add: 1,
      research_attributes: 2,
      sources_captured: 1,
      master_fills: 0,
      master_proposed_updates: 1,
      manual_authority_conflicts: 0,
      blocking_rows: 1,
      contacts_present: 0,
      custom_attribute_values: 0,
      workflow_fields: 0,
      ignored_fields: 0,
    },
    batch_caveat: 'Confirm volumes from Sully, Iowa.',
    confirm_allowed: false,
    production_confirm_enabled: false,
    workflow_fields_will_write: 0,
    forecast: {
      companies_created: 0,
      companies_reused: 0,
      locations_created: 0,
      contacts_created: 0,
      contacts_reused: 0,
      contacts_skipped: 0,
      notes_append: 0,
      notes_dedupe: 0,
      research_created: 1,
      attributes_created: 2,
      master_fill_blank: 0,
      master_accept_incoming: 0,
      master_keep_existing: 1,
    },
    blocking: true,
    blocking_reasons: ['AMBIGUOUS'],
    default_status: 'New',
    offset: 0,
    limit: 25,
    total_rows: 1,
    ignored_headers: [],
    rows: [
      {
        row_id: 1,
        source_row_number: 2,
        ri_class: 'AMBIGUOUS',
        matcher_reasons: ['domain_exact'],
        company_name: 'Greenheck',
        address: '1 Main',
        city: 'Schofield',
        state: 'WI',
        zip: '54476',
        phone: '(715) 359-6171',
        website: 'https://www.greenheck.com/',
        matched_company_id: 215,
        matched_company_name: 'Greenheck Fan',
        matched_city: 'Schofield',
        matched_state: 'WI',
        matched_address: '1 Main',
        matched_phone: '(715) 359-6171',
        matched_website: 'www.greenheck.com',
        possibles: [
          {
            company_id: 215,
            name: 'Greenheck Fan',
            city: 'Schofield',
            state: 'WI',
            address: '1 Main',
            phone: '',
            website: 'www.greenheck.com',
            reasons: ['domain_exact'],
          },
        ],
        conflicts: [
          { field: 'website', incoming: 'https://www.greenheck.com/', current: '', class: 'FILL_BLANK' },
        ],
        existing_clients: ['carmeco'],
        relationship_action: 'deferred',
        planned_status: '',
        status_action: '',
        research_priority_code: 'A',
        research_priority_label: 'A - Iowa',
        why_client_fits: 'Makes fans; Janco could supply panels.',
        qualification_notes: 'Confirm volumes from Sully, Iowa.',
        attributes: {
          target_market: ['Agriculture / grain equipment'],
          equipment_product: ['fans'],
          potential_component: ['welded brackets'],
          target_department: ['Purchasing / Supply Chain', 'Engineering / Operations'],
        },
        sources: [{ source_role: 'WEBSITE', source_url: 'https://www.greenheck.com/', field_supported: 'website' }],
        allowed_resolutions: ['USE_EXISTING', 'CREATE_NEW', 'SKIP'],
        blocking: true,
        blocking_reasons: ['AMBIGUOUS'],
      },
    ],
    ...overrides,
  }
}

function renderImport() {
  return render(
    <AdministrationResearchImport
      allMyClients={false}
      connectClientId={4}
      scopedClientId={4}
      availableClients={clients}
      onScopedClientId={() => undefined}
      clientName="Premier Manufacturing"
    />,
  )
}

afterEach(() => {
  cleanup()
})

describe('Administration Research & Custom Prospect Import', () => {
  it('uploads, maps, previews structured research, and keeps confirm disabled', async () => {
    vi.mocked(researchImport.uploadResearchImport).mockResolvedValue({
      kind: 'ok',
      needs_worksheet: false,
      visible_sheets: ['CSV'],
      filename: 'janco.csv',
      file_type: 'csv',
      message: '',
      batch: batch(),
    })
    vi.mocked(researchImport.saveResearchImportMapping).mockResolvedValue(batch())
    vi.mocked(researchImport.dryRunResearchImport).mockResolvedValue(preview())

    renderImport()
    expect(screen.getByRole('heading', { name: 'Research & Custom Prospect Import' })).toBeTruthy()
    expect(screen.getByLabelText('Source type')).toBeTruthy()
    const file = new File(['Company Name\nGreenheck\n'], 'janco.csv', { type: 'text/csv' })
    fireEvent.change(screen.getByLabelText('Research workbook'), {
      target: { files: [file] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Upload' }))
    await waitFor(() => expect(screen.getByText(/Map Fields/)).toBeTruthy())
    expect(screen.getByText('Harvest International')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Validate and preview' }))
    await waitFor(() => expect(screen.getByText(/Ready \/ Blocking Summary/)).toBeTruthy())
    expect(screen.getByText(/Ambiguous: 1/)).toBeTruthy()
    expect(screen.getByText(/AMBIGUOUS — Greenheck/)).toBeTruthy()
    expect(screen.getByText(/Why Client Fits: Makes fans; Janco could supply panels./)).toBeTruthy()
    expect(screen.getByText(/Target Department: Purchasing \/ Supply Chain; Engineering \/ Operations/)).toBeTruthy()
    expect(screen.getByText(/FILL_BLANK/)).toBeTruthy()
    expect(screen.getByText(/Live confirmation not enabled/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Confirm (live confirmation not enabled)' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/Workflow fields will NOT be written/)).toBeTruthy()
    expect(screen.getByText(/Same-batch research conflicts/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'USE_EXISTING: Greenheck Fan' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'CREATE_NEW' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^USE_EXISTING$/ })).toBeNull()
  })

  it('maps arbitrary headers, ignore, custom attributes, contacts, and workflow warnings', async () => {
    const customBatch = batch({
      original_filename: 'custom.csv',
      source_type: 'CUSTOM_REPORT',
      headers: ['Org Name', 'First Name', 'Status', 'Press Tonnage', 'Internal Score'],
      mapping: { company_name: 'Org Name' },
      suggested_mapping: { company_name: 'Org Name', contact_first_name: 'First Name' },
      sample_rows: [
        {
          'Org Name': 'Synthetic Press Co',
          'First Name': 'Riley',
          Status: 'Hot',
          'Press Tonnage': '600',
          'Internal Score': '99',
        },
      ],
      column_map: [
        {
          header: 'Org Name',
          samples: ['Synthetic Press Co'],
          suggested_field: 'company_name',
          suggested_category: 'MASTER_COMPANY',
          selected_field: 'company_name',
          selected_category: 'MASTER_COMPANY',
          custom_key: '',
          custom_label: '',
          value_type: 'TEXT',
          ignored: false,
          workflow_sensitive: false,
          warning: '',
        },
        {
          header: 'First Name',
          samples: ['Riley'],
          suggested_field: 'contact_first_name',
          suggested_category: 'CONTACT',
          selected_field: 'contact_first_name',
          selected_category: 'CONTACT',
          custom_key: '',
          custom_label: '',
          value_type: 'TEXT',
          ignored: false,
          workflow_sensitive: false,
          warning: '',
        },
        {
          header: 'Status',
          samples: ['Hot'],
          suggested_field: '',
          suggested_category: 'CRM_WORKFLOW',
          selected_field: '',
          selected_category: '',
          custom_key: '',
          custom_label: '',
          value_type: 'TEXT',
          ignored: false,
          workflow_sensitive: true,
          warning: 'Workflow-sensitive. Will not change CRM status, assignment, or follow-up in this phase.',
        },
        {
          header: 'Press Tonnage',
          samples: ['600'],
          suggested_field: '',
          suggested_category: '',
          selected_field: '',
          selected_category: 'CUSTOM_ATTRIBUTE',
          custom_key: 'press_tonnage',
          custom_label: 'Press Tonnage',
          value_type: 'NUMBER',
          ignored: false,
          workflow_sensitive: false,
          warning: '',
        },
        {
          header: 'Internal Score',
          samples: ['99'],
          suggested_field: '',
          suggested_category: '',
          selected_field: '',
          selected_category: 'IGNORE',
          custom_key: '',
          custom_label: '',
          value_type: 'TEXT',
          ignored: true,
          workflow_sensitive: false,
          warning: '',
        },
      ],
      mapping_template_suggestion: {
        template_name: 'Janco Monthly Prospect Report',
        safety_class: 'safe_reusable',
        review_required: true,
        header_compatibility: {
          confidence: 'low',
          review_required: true,
          missing_headers: ['Why Janco Fits'],
          new_headers: ['Press Tonnage'],
        },
      },
    })
    vi.mocked(researchImport.uploadResearchImport).mockResolvedValue({
      kind: 'ok',
      needs_worksheet: false,
      visible_sheets: ['CSV'],
      filename: 'custom.csv',
      file_type: 'csv',
      message: '',
      batch: customBatch,
    })
    vi.mocked(researchImport.saveResearchImportMapping).mockResolvedValue(customBatch)
    vi.mocked(researchImport.dryRunResearchImport).mockResolvedValue(
      preview({
        batch: customBatch,
        ignored_headers: ['Internal Score'],
        counts: {
          source_rows: 1,
          new_companies: 1,
          contacts_present: 1,
          custom_attribute_values: 1,
          workflow_fields: 1,
          ignored_fields: 1,
          blocking_rows: 0,
          ambiguous: 0,
        },
        blocking: false,
        rows: [
          {
            ...preview().rows[0],
            ri_class: 'NEW_COMPANY',
            company_name: 'Synthetic Press Co',
            blocking: false,
            blocking_reasons: [],
            contacts: [
              {
                incoming: { full_name: 'Riley Synthetic', first_name: 'Riley', last_name: 'Synthetic' },
                contact_class: 'NEW_CONTACT',
                full_name: 'Riley Synthetic',
                first_name: 'Riley',
                last_name: 'Synthetic',
                reasons: [],
                allowed_resolutions: ['CREATE_NEW_CONTACT', 'SKIP_CONTACT'],
                resolution: 'CREATE_NEW_CONTACT',
              },
            ],
            custom_attributes: [{ key: 'press_tonnage', label: 'Press Tonnage', original_value: '600' }],
            workflow_fields: { workflow_status: 'Hot' },
            ignored_fields: ['Internal Score'],
          },
        ],
      }),
    )

    renderImport()
    fireEvent.change(screen.getByLabelText('Source type'), { target: { value: 'CUSTOM_REPORT' } })
    const file = new File(['Org Name\nSynthetic Press Co\n'], 'custom.csv', { type: 'text/csv' })
    fireEvent.change(screen.getByLabelText('Research workbook'), { target: { files: [file] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload' }))
    await waitFor(() => expect(screen.getByText(/Map Fields/)).toBeTruthy())
    expect(screen.getByText('Synthetic Press Co')).toBeTruthy()
    expect(screen.getByText('Riley')).toBeTruthy()
    expect(screen.getByLabelText('Ignore Internal Score')).toBeTruthy()
    expect((screen.getByLabelText('Ignore Internal Score') as HTMLInputElement).checked).toBe(true)
    expect(screen.getByLabelText('Custom attribute label for Press Tonnage')).toBeTruthy()
    expect(screen.getByText(/Workflow-sensitive/)).toBeTruthy()
    expect(screen.getByText(/Mapping template suggested: Janco Monthly Prospect Report/)).toBeTruthy()
    expect(screen.getByText(/Changed \/ missing columns: Why Janco Fits/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Validate and preview' }))
    await waitFor(() => expect(screen.getByText(/Ready \/ Blocking Summary/)).toBeTruthy())
    expect(screen.getByText(/Contacts present: 1/)).toBeTruthy()
    expect(screen.getAllByText(/Custom attributes: 1/).length).toBeGreaterThan(0)
    expect(screen.getByText(/Contact Review/)).toBeTruthy()
    expect(screen.getAllByText(/Riley Synthetic/).length).toBeGreaterThan(0)
    expect(screen.getByText(/NEW CONTACT/)).toBeTruthy()
    expect(screen.getByText(/press_tonnage=600|Press Tonnage=600/)).toBeTruthy()
    expect(screen.getByText(/Workflow fields \(not applied\)/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Confirm (live confirmation not enabled)' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
