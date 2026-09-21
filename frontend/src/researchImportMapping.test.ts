import { describe, expect, it } from 'vitest'
import {
  mappingFromColumns,
  normalizeAttributeKey,
  suggestResearchMapping,
  validateResearchMapping,
  type ColumnDraft,
} from './researchImportMapping'

function col(header: string, patch: Partial<ColumnDraft> = {}): ColumnDraft {
  return {
    header,
    samples: [],
    suggested_field: '',
    suggested_category: '',
    selected_field: '',
    selected_category: '',
    custom_key: '',
    custom_label: '',
    value_type: 'TEXT',
    ignored: false,
    workflow_sensitive: false,
    warning: '',
    ...patch,
  }
}

describe('Research & Custom Prospect Import mapping', () => {
  it('safely automaps obvious company headers and skips ambiguous ones', () => {
    const suggested = suggestResearchMapping([
      'Business Name',
      'URL',
      'Main Phone',
      'Street Address',
      'Status',
      'Owner',
      'Type',
      'Rating',
      'Score',
      'Category',
      'Comments',
      'Rep',
    ])
    expect(suggested.company_name).toBe('Business Name')
    expect(suggested.website).toBe('URL')
    expect(suggested.phone).toBe('Main Phone')
    expect(suggested.address).toBe('Street Address')
    expect(suggested.workflow_status).toBeUndefined()
    expect(suggested.imported_notes).toBeUndefined()
    expect(Object.values(suggested)).not.toContain('Status')
    expect(Object.values(suggested)).not.toContain('Comments')
  })

  it('normalizes custom attribute keys', () => {
    expect(normalizeAttributeKey('Press Tonnage')).toBe('press_tonnage')
    expect(normalizeAttributeKey('Press tonnage')).toBe('press_tonnage')
    expect(normalizeAttributeKey('press_tonnage')).toBe('press_tonnage')
  })

  it('keeps ignored columns out of destination fields', () => {
    const mapping = mappingFromColumns([
      col('Org Name', { selected_field: 'company_name', selected_category: 'MASTER_COMPANY' }),
      col('Internal Score', { ignored: true, selected_category: 'IGNORE' }),
    ])
    expect(mapping.fields.company_name).toBe('Org Name')
    expect(mapping.columns.find((item) => item.header === 'Internal Score')?.ignored).toBe(true)
    expect(Object.values(mapping.fields)).not.toContain('Internal Score')
    const check = validateResearchMapping(mapping, ['Org Name', 'Internal Score'])
    expect(check.ok).toBe(true)
  })
})
