import { describe, expect, it } from 'vitest'
import {
  mappingsEqual,
  normalizeMapping,
  suggestMapping,
  validateMapping,
} from './crmImportMapping'

describe('crmImportMapping helpers', () => {
  it('suggests conservative deterministic aliases without duplicating headers', () => {
    const suggested = suggestMapping([
      'Company Name',
      'Email',
      'First Name',
      'Last Name',
      'Full Name',
      'Website',
      'Phone',
      'Status',
      'Notes',
    ])
    expect(suggested).toEqual({
      company_name: 'Company Name',
      contact_email: 'Email',
      contact_first_name: 'First Name',
      contact_last_name: 'Last Name',
      website: 'Website',
      phone: 'Phone',
      relationship_status: 'Status',
      relationship_notes: 'Notes',
    })
    expect(suggested.contact_full_name).toBeUndefined()
    const values = Object.values(suggested)
    expect(new Set(values).size).toBe(values.length)
  })

  it('suggests Status/Notes and source date aliases without duplicating headers', () => {
    const suggested = suggestMapping([
      'Company Name',
      'Status',
      'Notes',
      'Date Entered',
      'Last Updated',
    ])
    expect(suggested.relationship_status).toBe('Status')
    expect(suggested.relationship_notes).toBe('Notes')
    expect(suggested.source_entered_at).toBe('Date Entered')
    expect(suggested.source_updated_at).toBe('Last Updated')
  })

  it('prefers first/last over a lone full-name alias and skips unknown headers', () => {
    expect(suggestMapping(['Name', 'First', 'Last'])).toEqual({
      contact_first_name: 'First',
      contact_last_name: 'Last',
    })
    expect(suggestMapping(['Completely Unknown'])).toEqual({})
  })

  it('requires company mapping and rejects conflicts/invalid headers', () => {
    const headers = ['Company', 'Name', 'First', 'Email']
    expect(validateMapping({}, headers).ok).toBe(false)
    expect(validateMapping({ company_name: 'Missing' }, headers).errors[0]).toMatch(
      /not in this spreadsheet/,
    )
    expect(
      validateMapping(
        {
          company_name: 'Company',
          contact_full_name: 'Name',
          contact_first_name: 'First',
        },
        headers,
      ).errors,
    ).toContain('Use either a full name column or first and last name columns, not both.')
    expect(
      validateMapping(
        {
          company_name: 'Company',
          contact_email: 'Company',
        },
        headers,
      ).errors,
    ).toContain('Each source column can map to only one field.')
    expect(
      validateMapping({ company_name: 'Company', contact_email: 'Email' }, headers).ok,
    ).toBe(true)
  })

  it('normalizes and compares mappings by omitting blanks', () => {
    expect(normalizeMapping({ company_name: ' Company ', contact_email: '' })).toEqual({
      company_name: 'Company',
    })
    expect(
      mappingsEqual(
        { company_name: 'Company', contact_email: 'Email' },
        { contact_email: 'Email', company_name: 'Company', website: '' },
      ),
    ).toBe(true)
    expect(
      mappingsEqual({ company_name: 'Company' }, { company_name: 'Other' }),
    ).toBe(false)
  })
})
