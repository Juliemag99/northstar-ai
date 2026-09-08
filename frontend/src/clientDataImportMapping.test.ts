import { describe, expect, it } from 'vitest'
import {
  mappingsEqual,
  normalizeHistoryMapping,
  normalizeProspectsMapping,
  suggestHistoryMapping,
  suggestMapping,
  suggestProspectsMapping,
  validateHistoryMapping,
  validateProspectsMapping,
} from './clientDataImportMapping'

describe('clientDataImportMapping helpers', () => {
  it('suggests LeadMaster Record No. and company/contact aliases for prospects', () => {
    const suggested = suggestProspectsMapping([
      'LeadMaster Record No.',
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
    expect(suggested.external_record_no).toBe('LeadMaster Record No.')
    expect(suggested.company_name).toBe('Company Name')
    expect(suggested.contact_email).toBe('Email')
    expect(suggested.contact_first_name).toBe('First Name')
    expect(suggested.contact_last_name).toBe('Last Name')
    expect(suggested.website).toBe('Website')
    expect(suggested.phone).toBe('Phone')
    expect(suggested.relationship_status).toBe('Status')
    expect(suggested.relationship_notes).toBe('Notes')
    expect(suggested.contact_full_name).toBeUndefined()
    const values = Object.values(suggested)
    expect(new Set(values).size).toBe(values.length)
  })

  it('suggests history field aliases including attribution and event hash', () => {
    const suggested = suggestHistoryMapping([
      'Record No.',
      'Event Timestamp',
      'Author',
      'Event Type',
      'Attributed Client/Campaign',
      'Attribution Evidence',
      'Full Note Text',
      'Event Hash',
      'Company Name',
      'Status',
    ])
    expect(suggested).toEqual({
      history_record_no: 'Record No.',
      history_event_at: 'Event Timestamp',
      history_author: 'Author',
      history_event_type: 'Event Type',
      history_attribution: 'Attributed Client/Campaign',
      history_attribution_evidence: 'Attribution Evidence',
      history_note_text: 'Full Note Text',
      history_event_hash: 'Event Hash',
      history_company_name: 'Company Name',
      history_status: 'Status',
    })
  })

  it('suggestMapping returns separate prospects and history maps', () => {
    const result = suggestMapping(
      ['LeadMaster Record No.', 'Company'],
      ['Record Number', 'Note Text'],
    )
    expect(result.prospects.external_record_no).toBe('LeadMaster Record No.')
    expect(result.prospects.company_name).toBe('Company')
    expect(result.history.history_record_no).toBe('Record Number')
    expect(result.history.history_note_text).toBe('Note Text')
  })

  it('requires company name for prospects and history core fields when history present', () => {
    expect(validateProspectsMapping({}, ['Company']).ok).toBe(false)
    expect(
      validateProspectsMapping({ company_name: 'Company' }, ['Company']).ok,
    ).toBe(true)
    expect(validateHistoryMapping({}, []).ok).toBe(true)
    expect(
      validateHistoryMapping({}, ['Record No.', 'Note Text']).errors,
    ).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/History record No/),
        expect.stringMatching(/History note text/),
      ]),
    )
    expect(
      validateHistoryMapping(
        {
          history_record_no: 'Record No.',
          history_note_text: 'Note Text',
        },
        ['Record No.', 'Note Text'],
      ).ok,
    ).toBe(true)
  })

  it('normalizes and compares mappings by omitting blanks and unknown keys', () => {
    expect(
      normalizeProspectsMapping({
        company_name: ' Company ',
        external_record_no: 'RN',
        bogus: 'x',
        contact_email: '',
      }),
    ).toEqual({
      company_name: 'Company',
      external_record_no: 'RN',
    })
    expect(
      normalizeHistoryMapping({
        history_record_no: ' RN ',
        history_note_text: 'Note',
        unknown: 'x',
      }),
    ).toEqual({
      history_record_no: 'RN',
      history_note_text: 'Note',
    })
    expect(
      mappingsEqual(
        { company_name: 'Company', external_record_no: '1' },
        { external_record_no: '1', company_name: 'Company', website: '' },
      ),
    ).toBe(true)
    expect(
      mappingsEqual(
        { history_record_no: '1' },
        { history_record_no: '2' },
        'history',
      ),
    ).toBe(false)
  })
})
