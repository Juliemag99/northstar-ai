/** Client Data Import mapping helpers — extends CRM import canonical fields. */

import {
  CANONICAL_MAPPING_FIELDS,
  type CanonicalMappingField,
  normalizeHeaderKey as crmNormalizeHeaderKey,
} from './crmImportMapping'

export type ProspectsMappingField = CanonicalMappingField

export type HistoryMappingField = {
  key: string
  label: string
  group: 'history'
  required?: boolean
}

export function normalizeHeaderKey(raw: string): string {
  return crmNormalizeHeaderKey(raw).replace(/[^a-z0-9]/g, '')
}

/** Prospects fields = CRM import fields (includes LeadMaster Record No.). */
export const PROSPECTS_MAPPING_FIELDS: ProspectsMappingField[] = [
  ...CANONICAL_MAPPING_FIELDS,
]

export const HISTORY_MAPPING_FIELDS: HistoryMappingField[] = [
  { key: 'history_record_no', label: 'History record No.', group: 'history', required: true },
  { key: 'history_event_at', label: 'History timestamp', group: 'history' },
  { key: 'history_author', label: 'History author', group: 'history' },
  { key: 'history_event_type', label: 'History event type', group: 'history' },
  { key: 'history_attribution', label: 'History attribution', group: 'history' },
  {
    key: 'history_attribution_evidence',
    label: 'History attribution evidence',
    group: 'history',
  },
  { key: 'history_note_text', label: 'History note text', group: 'history', required: true },
  { key: 'history_event_hash', label: 'History event hash', group: 'history' },
  { key: 'history_source_note_id', label: 'History source note/activity ID', group: 'history' },
  { key: 'history_contact_no', label: 'History contact No.', group: 'history' },
  { key: 'history_company_name', label: 'History company name', group: 'history' },
  { key: 'history_status', label: 'History status', group: 'history' },
]

const PROSPECTS_FIELD_KEYS = new Set(PROSPECTS_MAPPING_FIELDS.map((f) => f.key))
const HISTORY_FIELD_KEYS = new Set(HISTORY_MAPPING_FIELDS.map((f) => f.key))

const PROSPECTS_HEADER_ALIASES: Record<string, string> = {
  company: 'company_name',
  companyname: 'company_name',
  account: 'company_name',
  accountname: 'company_name',
  organization: 'company_name',
  org: 'company_name',
  website: 'website',
  url: 'website',
  web: 'website',
  companyphone: 'phone',
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
  contactfirst: 'contact_first_name',
  contactfirstname: 'contact_first_name',
  last: 'contact_last_name',
  lastname: 'contact_last_name',
  contactlast: 'contact_last_name',
  contactlastname: 'contact_last_name',
  fullname: 'contact_full_name',
  contactname: 'contact_full_name',
  name: 'contact_full_name',
  title: 'contact_title',
  jobtitle: 'contact_title',
  contacttitle: 'contact_title',
  email: 'contact_email',
  emailaddress: 'contact_email',
  contactemail: 'contact_email',
  contactphone: 'contact_phone',
  mobile: 'contact_phone',
  cellphone: 'contact_phone',
  status: 'relationship_status',
  relationshipstatus: 'relationship_status',
  companystatus: 'relationship_status',
  currentstatus: 'relationship_status',
  currentdawsonstatus: 'relationship_status',
  notes: 'relationship_notes',
  note: 'relationship_notes',
  relationshipnotes: 'relationship_notes',
  comments: 'relationship_notes',
  sourceenteredat: 'source_entered_at',
  enteredat: 'source_entered_at',
  entereddate: 'source_entered_at',
  dateentered: 'source_entered_at',
  sourceupdatedat: 'source_updated_at',
  updatedat: 'source_updated_at',
  updateddate: 'source_updated_at',
  dateupdated: 'source_updated_at',
  lastupdated: 'source_updated_at',
  externalrecordno: 'external_record_no',
  leadmasterrecordno: 'external_record_no',
  recordno: 'external_record_no',
  recordnumber: 'external_record_no',
  record: 'external_record_no',
}

const HISTORY_HEADER_ALIASES: Record<string, string> = {
  historyrecordno: 'history_record_no',
  recordno: 'history_record_no',
  recordnumber: 'history_record_no',
  leadmasterrecordno: 'history_record_no',
  externalrecordno: 'history_record_no',
  historyeventat: 'history_event_at',
  eventtimestamp: 'history_event_at',
  timestamp: 'history_event_at',
  eventat: 'history_event_at',
  datetime: 'history_event_at',
  historyauthor: 'history_author',
  author: 'history_author',
  createdby: 'history_author',
  user: 'history_author',
  historyeventtype: 'history_event_type',
  eventtype: 'history_event_type',
  type: 'history_event_type',
  historyattribution: 'history_attribution',
  attribution: 'history_attribution',
  attributedclientcampaign: 'history_attribution',
  attributedclient: 'history_attribution',
  clientattribution: 'history_attribution',
  campaignattribution: 'history_attribution',
  historyattributionevidence: 'history_attribution_evidence',
  attributionevidence: 'history_attribution_evidence',
  evidence: 'history_attribution_evidence',
  historynotetext: 'history_note_text',
  notetext: 'history_note_text',
  fullnotetext: 'history_note_text',
  notes: 'history_note_text',
  note: 'history_note_text',
  body: 'history_note_text',
  historyeventhash: 'history_event_hash',
  eventhash: 'history_event_hash',
  hash: 'history_event_hash',
  historysourcenoteid: 'history_source_note_id',
  sourcenoteid: 'history_source_note_id',
  noteid: 'history_source_note_id',
  activityid: 'history_source_note_id',
  historycontactno: 'history_contact_no',
  contactno: 'history_contact_no',
  contactnumber: 'history_contact_no',
  historycompanyname: 'history_company_name',
  company: 'history_company_name',
  companyname: 'history_company_name',
  historystatus: 'history_status',
  status: 'history_status',
  currentstatus: 'history_status',
}

export function normalizeProspectsMapping(raw: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [dest, header] of Object.entries(raw || {})) {
    const key = String(dest || '').trim()
    const value = String(header || '').trim()
    if (!key || !value) continue
    if (!PROSPECTS_FIELD_KEYS.has(key)) continue
    out[key] = value
  }
  return out
}

export function normalizeHistoryMapping(raw: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [dest, header] of Object.entries(raw || {})) {
    const key = String(dest || '').trim()
    const value = String(header || '').trim()
    if (!key || !value) continue
    if (!HISTORY_FIELD_KEYS.has(key)) continue
    out[key] = value
  }
  return out
}

export type MappingValidation = {
  ok: boolean
  errors: string[]
}

function validateAgainstHeaders(
  mapping: Record<string, string>,
  headers: string[],
  options: { requireCompanyName: boolean; requireHistoryCore: boolean },
): MappingValidation {
  const errors: string[] = []
  const headerSet = new Set(headers)
  const used = new Map<string, string>()

  if (options.requireCompanyName && !mapping.company_name) {
    errors.push('Map a Company name column before continuing.')
  }
  if (options.requireHistoryCore) {
    if (!mapping.history_record_no) {
      errors.push('Map a History record No. column before continuing.')
    }
    if (!mapping.history_note_text) {
      errors.push('Map a History note text column before continuing.')
    }
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

export function validateProspectsMapping(
  draft: Record<string, string>,
  headers: string[],
): MappingValidation {
  return validateAgainstHeaders(normalizeProspectsMapping(draft), headers, {
    requireCompanyName: true,
    requireHistoryCore: false,
  })
}

export function validateHistoryMapping(
  draft: Record<string, string>,
  headers: string[],
): MappingValidation {
  if (headers.length === 0) {
    return { ok: true, errors: [] }
  }
  return validateAgainstHeaders(normalizeHistoryMapping(draft), headers, {
    requireCompanyName: false,
    requireHistoryCore: true,
  })
}

export function mappingsEqual(
  a: Record<string, string>,
  b: Record<string, string>,
  kind: 'prospects' | 'history' = 'prospects',
): boolean {
  const normalize = kind === 'history' ? normalizeHistoryMapping : normalizeProspectsMapping
  const left = normalize(a)
  const right = normalize(b)
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  for (const key of keys) {
    if ((left[key] || '') !== (right[key] || '')) return false
  }
  return true
}

function suggestFromAliases(
  headers: string[],
  aliases: Record<string, string>,
  fieldKeys: Set<string>,
  options: { preferFirstLast: boolean },
): Record<string, string> {
  const suggested: Record<string, string> = {}
  const usedHeaders = new Set<string>()
  const candidates: { header: string; field: string; priority: number }[] = []

  for (const header of headers) {
    const field = aliases[normalizeHeaderKey(header)]
    if (!field || !fieldKeys.has(field)) continue
    let priority = 10
    if (field === 'contact_first_name' || field === 'contact_last_name') priority = 1
    if (field === 'contact_full_name') priority = 5
    if (field === 'company_name' || field === 'history_company_name') priority = 0
    if (field === 'external_record_no' || field === 'history_record_no') priority = 0
    candidates.push({ header, field, priority })
  }

  candidates.sort((a, b) => a.priority - b.priority || a.header.localeCompare(b.header))

  const hasFirstOrLast = options.preferFirstLast
    ? candidates.some(
        (c) => c.field === 'contact_first_name' || c.field === 'contact_last_name',
      )
    : false

  for (const item of candidates) {
    if (suggested[item.field]) continue
    if (usedHeaders.has(item.header)) continue
    if (options.preferFirstLast && item.field === 'contact_full_name' && hasFirstOrLast) {
      continue
    }
    if (
      options.preferFirstLast &&
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

export function suggestProspectsMapping(headers: string[]): Record<string, string> {
  return suggestFromAliases(headers, PROSPECTS_HEADER_ALIASES, PROSPECTS_FIELD_KEYS, {
    preferFirstLast: true,
  })
}

export function suggestHistoryMapping(headers: string[]): Record<string, string> {
  return suggestFromAliases(headers, HISTORY_HEADER_ALIASES, HISTORY_FIELD_KEYS, {
    preferFirstLast: false,
  })
}

/** Suggest mappings for prospects headers and optional history headers. */
export function suggestMapping(
  prospectsHeaders: string[],
  historyHeaders: string[] = [],
): { prospects: Record<string, string>; history: Record<string, string> } {
  return {
    prospects: suggestProspectsMapping(prospectsHeaders),
    history: historyHeaders.length ? suggestHistoryMapping(historyHeaders) : {},
  }
}

export function companyFields(): ProspectsMappingField[] {
  return PROSPECTS_MAPPING_FIELDS.filter((f) => f.group === 'company')
}

export function contactFields(): ProspectsMappingField[] {
  return PROSPECTS_MAPPING_FIELDS.filter((f) => f.group === 'contact')
}

export function relationshipFields(): ProspectsMappingField[] {
  return PROSPECTS_MAPPING_FIELDS.filter((f) => f.group === 'relationship')
}

export function historyFields(): HistoryMappingField[] {
  return HISTORY_MAPPING_FIELDS
}
