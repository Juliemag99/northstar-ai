/** Pure deterministic CRM import mapping helpers (Checkpoint C4A). */

export type CanonicalMappingField = {
  key: string
  label: string
  group: 'company' | 'contact'
  required?: boolean
}

export const CANONICAL_MAPPING_FIELDS: CanonicalMappingField[] = [
  { key: 'company_name', label: 'Company name', group: 'company', required: true },
  { key: 'website', label: 'Website', group: 'company' },
  { key: 'phone', label: 'Company phone', group: 'company' },
  { key: 'address', label: 'Address', group: 'company' },
  { key: 'city', label: 'City', group: 'company' },
  { key: 'state', label: 'State', group: 'company' },
  { key: 'zip', label: 'ZIP', group: 'company' },
  { key: 'contact_first_name', label: 'Contact first name', group: 'contact' },
  { key: 'contact_last_name', label: 'Contact last name', group: 'contact' },
  { key: 'contact_full_name', label: 'Contact full name', group: 'contact' },
  { key: 'contact_title', label: 'Contact title', group: 'contact' },
  { key: 'contact_email', label: 'Contact email', group: 'contact' },
  { key: 'contact_phone', label: 'Contact phone', group: 'contact' },
]

const FIELD_KEYS = new Set(CANONICAL_MAPPING_FIELDS.map((f) => f.key))

/** Explicit aliases after normalizeHeaderKey. Prefer first/last over full name. */
const HEADER_ALIASES: Record<string, string> = {
  company: 'company_name',
  companyname: 'company_name',
  company_name: 'company_name',
  account: 'company_name',
  accountname: 'company_name',
  organization: 'company_name',
  org: 'company_name',
  website: 'website',
  url: 'website',
  web: 'website',
  companyphone: 'phone',
  company_phone: 'phone',
  businessphone: 'phone',
  phone: 'phone',
  address: 'address',
  street: 'address',
  city: 'city',
  state: 'state',
  zip: 'zip',
  zipcode: 'zip',
  postal: 'zip',
  postalcode: 'zip',
  first: 'contact_first_name',
  firstname: 'contact_first_name',
  first_name: 'contact_first_name',
  contactfirst: 'contact_first_name',
  contactfirstname: 'contact_first_name',
  last: 'contact_last_name',
  lastname: 'contact_last_name',
  last_name: 'contact_last_name',
  contactlast: 'contact_last_name',
  contactlastname: 'contact_last_name',
  fullname: 'contact_full_name',
  full_name: 'contact_full_name',
  contactname: 'contact_full_name',
  contact_full_name: 'contact_full_name',
  name: 'contact_full_name',
  title: 'contact_title',
  jobtitle: 'contact_title',
  contacttitle: 'contact_title',
  email: 'contact_email',
  emailaddress: 'contact_email',
  contactemail: 'contact_email',
  contact_email: 'contact_email',
  contactphone: 'contact_phone',
  contact_phone: 'contact_phone',
  mobile: 'contact_phone',
  cellphone: 'contact_phone',
}

export function normalizeHeaderKey(raw: string): string {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[\s_-]+/g, '')
}

export function normalizeMapping(raw: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [dest, header] of Object.entries(raw || {})) {
    const key = String(dest || '').trim()
    const value = String(header || '').trim()
    if (!key || !value) continue
    if (!FIELD_KEYS.has(key)) continue
    out[key] = value
  }
  return out
}

export type MappingValidation = {
  ok: boolean
  errors: string[]
}

export function validateMapping(
  draft: Record<string, string>,
  headers: string[],
): MappingValidation {
  const mapping = normalizeMapping(draft)
  const errors: string[] = []
  const headerSet = new Set(headers)
  const used = new Map<string, string>()

  if (!mapping.company_name) {
    errors.push('Map a Company name column before continuing.')
  }

  for (const [dest, header] of Object.entries(mapping)) {
    if (!headerSet.has(header)) {
      errors.push(`Source column “${header}” is not in this spreadsheet.`)
      continue
    }
    const prior = used.get(header)
    if (prior && prior !== dest) {
      errors.push('Each source column can map to only one field.')
    } else {
      used.set(header, dest)
    }
  }

  if (
    mapping.contact_full_name &&
    (mapping.contact_first_name || mapping.contact_last_name)
  ) {
    errors.push(
      'Use either a full name column or first and last name columns, not both.',
    )
  }

  return { ok: errors.length === 0, errors: [...new Set(errors)] }
}

export function mappingsEqual(
  a: Record<string, string>,
  b: Record<string, string>,
): boolean {
  const left = normalizeMapping(a)
  const right = normalizeMapping(b)
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  for (const key of keys) {
    if ((left[key] || '') !== (right[key] || '')) return false
  }
  return true
}

/**
 * Conservative deterministic suggestions. Never assigns the same header twice.
 * Prefers first/last over a generic full-name alias when both are available.
 */
export function suggestMapping(headers: string[]): Record<string, string> {
  const suggested: Record<string, string> = {}
  const usedHeaders = new Set<string>()

  const candidates: { header: string; field: string; priority: number }[] = []
  for (const header of headers) {
    const field = HEADER_ALIASES[normalizeHeaderKey(header)]
    if (!field || !FIELD_KEYS.has(field)) continue
    let priority = 10
    if (field === 'contact_first_name' || field === 'contact_last_name') priority = 1
    if (field === 'contact_full_name') priority = 5
    if (field === 'company_name') priority = 0
    candidates.push({ header, field, priority })
  }

  candidates.sort((a, b) => a.priority - b.priority || a.header.localeCompare(b.header))

  const hasFirstOrLast = candidates.some(
    (c) => c.field === 'contact_first_name' || c.field === 'contact_last_name',
  )

  for (const item of candidates) {
    if (suggested[item.field]) continue
    if (usedHeaders.has(item.header)) continue
    if (item.field === 'contact_full_name' && hasFirstOrLast) continue
    if (
      (item.field === 'contact_first_name' || item.field === 'contact_last_name') &&
      suggested.contact_full_name
    ) {
      continue
    }
    suggested[item.field] = item.header
    usedHeaders.add(item.header)
  }

  return suggested
}

export function companyFields(): CanonicalMappingField[] {
  return CANONICAL_MAPPING_FIELDS.filter((f) => f.group === 'company')
}

export function contactFields(): CanonicalMappingField[] {
  return CANONICAL_MAPPING_FIELDS.filter((f) => f.group === 'contact')
}
