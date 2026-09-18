/** LeadMaster refresh mapping helpers. Optional fields may be NOT MAPPED. */

export type RefreshMappingField = {
  key: string
  label: string
  group: 'company' | 'contact' | 'relationship' | 'history'
  required?: boolean
}

export const NOT_MAPPED = 'NOT MAPPED'

export const REFRESH_MAPPING_FIELDS: RefreshMappingField[] = [
  { key: 'external_record_no', label: 'LeadMaster company RN', group: 'company' },
  { key: 'company_name', label: 'Company name', group: 'company', required: true },
  { key: 'address', label: 'Address', group: 'company' },
  { key: 'address2', label: 'Address 2 / unit', group: 'company' },
  { key: 'city', label: 'City', group: 'company' },
  { key: 'state', label: 'State / province', group: 'company' },
  { key: 'zip', label: 'Postal code', group: 'company' },
  { key: 'country', label: 'Country', group: 'company' },
  { key: 'phone', label: 'Company phone', group: 'company' },
  { key: 'phone_extension', label: 'Company phone extension', group: 'company' },
  { key: 'website', label: 'Website / domain', group: 'company' },
  { key: 'contact_first_name', label: 'First name', group: 'contact' },
  { key: 'contact_last_name', label: 'Last name', group: 'contact' },
  { key: 'contact_full_name', label: 'Contact full name', group: 'contact' },
  { key: 'contact_title', label: 'Title', group: 'contact' },
  { key: 'contact_email', label: 'Email', group: 'contact' },
  { key: 'contact_phone', label: 'Phone', group: 'contact' },
  { key: 'contact_phone_extension', label: 'Phone extension', group: 'contact' },
  { key: 'contact_alt_phone', label: 'Alternate phone', group: 'contact' },
  { key: 'contact_alt_extension', label: 'Alternate extension', group: 'contact' },
  { key: 'relationship_status', label: 'Status', group: 'relationship' },
  { key: 'relationship_notes', label: 'Notes', group: 'relationship' },
  { key: 'assigned_rep', label: 'Assigned / source rep', group: 'relationship' },
  { key: 'campaign', label: 'Campaign / list', group: 'relationship' },
  { key: 'history_source_id', label: 'History source event/note ID', group: 'history' },
  { key: 'history_event_at', label: 'History event date', group: 'history' },
  { key: 'history_event_type', label: 'History event type', group: 'history' },
  { key: 'history_note_text', label: 'History event text', group: 'history' },
  { key: 'history_author', label: 'History source user/rep', group: 'history' },
]

const FIELD_KEYS = new Set(REFRESH_MAPPING_FIELDS.map((f) => f.key))

export function normalizeRefreshMapping(raw: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [dest, header] of Object.entries(raw || {})) {
    const key = String(dest || '').trim()
    const value = String(header || '').trim()
    if (!key || !FIELD_KEYS.has(key)) continue
    if (!value || value.toUpperCase() === NOT_MAPPED) continue
    out[key] = value
  }
  return out
}

export function validateRefreshMapping(
  draft: Record<string, string>,
  headers: string[],
): { ok: boolean; errors: string[] } {
  const mapping = normalizeRefreshMapping(draft)
  const errors: string[] = []
  const headerSet = new Set(headers)
  const used = new Map<string, string>()
  if (!mapping.company_name) errors.push('Map a Company name column before continuing.')
  for (const [dest, header] of Object.entries(mapping)) {
    if (!headerSet.has(header)) {
      errors.push(`Source column “${header}” is not in this spreadsheet.`)
      continue
    }
    const prior = used.get(header)
    if (prior && prior !== dest) errors.push('Each source column can map to only one field.')
    else used.set(header, dest)
  }
  if (mapping.contact_full_name && (mapping.contact_first_name || mapping.contact_last_name)) {
    errors.push('Use either a full name column or first and last name columns, not both.')
  }
  return { ok: errors.length === 0, errors: [...new Set(errors)] }
}
