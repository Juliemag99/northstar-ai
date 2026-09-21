/** Research & Custom Prospect Import mapping helpers. */

export type ResearchMappingField = {
  key: string
  label: string
  required?: boolean
  category: string
}

export const RESEARCH_SOURCE_TYPES: Array<{ code: string; label: string }> = [
  { code: 'CHATGPT_DEEP_RESEARCH', label: 'ChatGPT Deep Research' },
  { code: 'AI_RESEARCH_OTHER', label: 'Other AI research' },
  { code: 'CLIENT_PROVIDED', label: 'Client-provided list' },
  { code: 'INTERNAL_RESEARCH', label: 'Internal NorthStar research' },
  { code: 'TRADE_SHOW', label: 'Trade show list' },
  { code: 'ASSOCIATION_DIRECTORY', label: 'Association / directory list' },
  { code: 'PURCHASED_LIST', label: 'Purchased prospect list' },
  { code: 'SALESPERSON_PROVIDED', label: 'Salesperson-created spreadsheet' },
  { code: 'CUSTOM_REPORT', label: 'Legacy / custom report' },
  { code: 'OTHER', label: 'Other' },
]

export const RESEARCH_CATEGORIES: Array<{ code: string; label: string }> = [
  { code: 'MASTER_COMPANY', label: 'Master company' },
  { code: 'LOCATION', label: 'Location' },
  { code: 'CONTACT', label: 'Contact' },
  { code: 'CLIENT_PROSPECT', label: 'Client prospect' },
  { code: 'RESEARCH_INTELLIGENCE', label: 'Research intelligence' },
  { code: 'TARGETING', label: 'Targeting' },
  { code: 'SOURCE_PROVENANCE', label: 'Source / provenance' },
  { code: 'BATCH_LINEAGE', label: 'Batch / lineage' },
  { code: 'CRM_WORKFLOW', label: 'CRM workflow' },
  { code: 'CUSTOM_ATTRIBUTE', label: 'Custom attribute' },
  { code: 'NOTES', label: 'Notes' },
  { code: 'IGNORE', label: 'Ignore' },
]

export const RESEARCH_MAPPING_FIELDS: ResearchMappingField[] = [
  { key: 'company_name', label: 'Company Name', required: true, category: 'MASTER_COMPANY' },
  { key: 'website', label: 'Website', category: 'MASTER_COMPANY' },
  { key: 'phone', label: 'Company Phone', category: 'MASTER_COMPANY' },
  { key: 'address', label: 'Address 1', category: 'LOCATION' },
  { key: 'address2', label: 'Address 2', category: 'LOCATION' },
  { key: 'city', label: 'City', category: 'LOCATION' },
  { key: 'state', label: 'State/Province', category: 'LOCATION' },
  { key: 'zip', label: 'Postal Code', category: 'LOCATION' },
  { key: 'country', label: 'Country', category: 'LOCATION' },
  { key: 'contact_first_name', label: 'Contact First Name', category: 'CONTACT' },
  { key: 'contact_last_name', label: 'Contact Last Name', category: 'CONTACT' },
  { key: 'contact_full_name', label: 'Contact Full Name', category: 'CONTACT' },
  { key: 'contact_title', label: 'Contact Title', category: 'CONTACT' },
  { key: 'contact_email', label: 'Contact Email', category: 'CONTACT' },
  { key: 'contact_phone', label: 'Contact Phone', category: 'CONTACT' },
  { key: 'contact_phone_extension', label: 'Contact Phone Extension', category: 'CONTACT' },
  { key: 'research_priority', label: 'Research Priority', category: 'CLIENT_PROSPECT' },
  { key: 'why_client_fits', label: 'Why Client Fits', category: 'CLIENT_PROSPECT' },
  { key: 'target_market', label: 'Target Market / Industry', category: 'RESEARCH_INTELLIGENCE' },
  { key: 'equipment_product', label: 'Equipment / Products', category: 'RESEARCH_INTELLIGENCE' },
  { key: 'potential_components', label: 'Potential Components', category: 'RESEARCH_INTELLIGENCE' },
  { key: 'qualification_notes', label: 'Qualification Notes', category: 'RESEARCH_INTELLIGENCE' },
  { key: 'target_department', label: 'Target Department', category: 'TARGETING' },
  { key: 'product_source', label: 'Product Source', category: 'SOURCE_PROVENANCE' },
  { key: 'address_source', label: 'Address Source', category: 'SOURCE_PROVENANCE' },
  { key: 'phone_source', label: 'Phone Source', category: 'SOURCE_PROVENANCE' },
  { key: 'website_source', label: 'Website Source', category: 'SOURCE_PROVENANCE' },
  { key: 'general_source', label: 'General Research Source', category: 'SOURCE_PROVENANCE' },
  { key: 'research_date', label: 'Research Date', category: 'BATCH_LINEAGE' },
  { key: 'research_method', label: 'Research Method', category: 'BATCH_LINEAGE' },
  { key: 'research_batch_name', label: 'Research Batch', category: 'BATCH_LINEAGE' },
  { key: 'original_research_file', label: 'Original Research File', category: 'BATCH_LINEAGE' },
  { key: 'prior_research_file', label: 'Prior Research File', category: 'BATCH_LINEAGE' },
  { key: 'imported_notes', label: 'Notes (explicit)', category: 'NOTES' },
  { key: 'workflow_status', label: 'CRM Status (review)', category: 'CRM_WORKFLOW' },
  { key: 'workflow_assigned_rep', label: 'Assigned Rep (review)', category: 'CRM_WORKFLOW' },
  { key: 'workflow_follow_up', label: 'Next Follow-Up (review)', category: 'CRM_WORKFLOW' },
  { key: 'workflow_next_action', label: 'Next Action (review)', category: 'CRM_WORKFLOW' },
  { key: 'workflow_hot', label: 'Hot (review)', category: 'CRM_WORKFLOW' },
  { key: 'workflow_campaign', label: 'Campaign (review)', category: 'CRM_WORKFLOW' },
]

export const VALUE_TYPES = ['TEXT', 'NUMBER', 'DATE', 'BOOLEAN', 'URL'] as const

const FIELD_KEYS = new Set(RESEARCH_MAPPING_FIELDS.map((f) => f.key))
const FIELD_CATEGORY: Record<string, string> = Object.fromEntries(
  RESEARCH_MAPPING_FIELDS.map((f) => [f.key, f.category]),
)
const WORKFLOW_FIELDS = new Set(
  RESEARCH_MAPPING_FIELDS.filter((f) => f.category === 'CRM_WORKFLOW').map((f) => f.key),
)

const AMBIGUOUS_AUTOMAP = new Set([
  'status',
  'owner',
  'type',
  'rating',
  'score',
  'category',
  'comments',
  'comment',
  'rep',
  'assignedrep',
  'salesperson',
  'nextfollowup',
  'nextaction',
  'hot',
  'campaign',
  'notes',
  'note',
  'remarks',
  'remark',
])

const ALIASES: Record<string, string> = {
  company: 'company_name',
  companyname: 'company_name',
  account: 'company_name',
  accountname: 'company_name',
  organization: 'company_name',
  org: 'company_name',
  orgname: 'company_name',
  firm: 'company_name',
  businessname: 'company_name',
  business: 'company_name',
  street: 'address',
  streetaddress: 'address',
  address1: 'address',
  addressline1: 'address',
  url: 'website',
  web: 'website',
  website: 'website',
  homepage: 'website',
  mainphone: 'phone',
  hqphone: 'phone',
  companyphone: 'phone',
  jancotargetmarket: 'target_market',
  targetmarket: 'target_market',
  equipmentproduct: 'equipment_product',
  whyjancofits: 'why_client_fits',
  whyclientfits: 'why_client_fits',
  potentialcomponents: 'potential_components',
  priority: 'research_priority',
  researchpriority: 'research_priority',
  targetdepartment: 'target_department',
  qualificationnotes: 'qualification_notes',
  productsource: 'product_source',
  addresssource: 'address_source',
  phonesource: 'phone_source',
  websitesource: 'website_source',
  generalsource: 'general_source',
  generalresearchsource: 'general_source',
  firstname: 'contact_first_name',
  lastname: 'contact_last_name',
  fullname: 'contact_full_name',
  contactname: 'contact_full_name',
  jobtitle: 'contact_title',
  email: 'contact_email',
  emailaddress: 'contact_email',
  contactphone: 'contact_phone',
  phoneextension: 'contact_phone_extension',
}

export type ColumnDraft = {
  header: string
  samples: string[]
  suggested_field: string
  suggested_category: string
  selected_field: string
  selected_category: string
  custom_key: string
  custom_label: string
  value_type: string
  ignored: boolean
  workflow_sensitive: boolean
  warning: string
}

export type ResearchMappingV3 = {
  fields: Record<string, string>
  columns: Array<{
    header: string
    category: string
    field: string
    ignored: boolean
    custom_key?: string
    custom_label?: string
    value_type?: string
    scope?: string
  }>
}

export function normalizeHeaderKey(header: string): string {
  return header.toLowerCase().replace(/&/g, ' and ').replace(/[^a-z0-9]+/g, '')
}

export function normalizeAttributeKey(label: string): string {
  return label
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
}

export function suggestResearchMapping(headers: string[]): Record<string, string> {
  const mapping: Record<string, string> = {}
  const used = new Set<string>()
  for (const header of headers) {
    const norm = normalizeHeaderKey(header)
    if (AMBIGUOUS_AUTOMAP.has(norm)) continue
    const key = ALIASES[norm] || (FIELD_KEYS.has(norm) ? norm : '')
    if (!key || used.has(key) || !FIELD_KEYS.has(key) || WORKFLOW_FIELDS.has(key)) continue
    mapping[key] = header
    used.add(key)
  }
  if (!mapping.website_source && mapping.website) mapping.website_source = mapping.website
  return mapping
}

export function normalizeResearchMapping(
  mapping: Record<string, string>,
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, header] of Object.entries(mapping)) {
    if (!FIELD_KEYS.has(key) || !header.trim()) continue
    out[key] = header.trim()
  }
  return out
}

export function fieldsFromMapping(
  mapping: Record<string, string> | ResearchMappingV3 | null | undefined,
): Record<string, string> {
  if (!mapping) return {}
  if ('fields' in mapping || 'columns' in mapping) {
    const fields = normalizeResearchMapping((mapping as ResearchMappingV3).fields || {})
    for (const column of (mapping as ResearchMappingV3).columns || []) {
      if (column.ignored || column.category === 'IGNORE') continue
      if (column.field && FIELD_KEYS.has(column.field) && column.header) {
        fields[column.field] = column.header
      }
    }
    return fields
  }
  return normalizeResearchMapping(mapping)
}

export function mappingFromColumns(columns: ColumnDraft[]): ResearchMappingV3 {
  const fields: Record<string, string> = {}
  const out = columns.map((col) => {
    if (col.ignored || col.selected_category === 'IGNORE') {
      return {
        header: col.header,
        category: 'IGNORE',
        field: '',
        ignored: true,
        custom_key: '',
        custom_label: '',
        value_type: 'TEXT',
      }
    }
    if (col.selected_category === 'CUSTOM_ATTRIBUTE') {
      return {
        header: col.header,
        category: 'CUSTOM_ATTRIBUTE',
        field: '',
        ignored: false,
        custom_key: col.custom_key || normalizeAttributeKey(col.custom_label || col.header),
        custom_label: col.custom_label || col.header,
        value_type: col.value_type || 'TEXT',
        scope: 'client',
      }
    }
    if (col.selected_field) fields[col.selected_field] = col.header
    return {
      header: col.header,
      category: col.selected_category || FIELD_CATEGORY[col.selected_field] || '',
      field: col.selected_field,
      ignored: false,
      custom_key: '',
      custom_label: '',
      value_type: 'TEXT',
    }
  })
  return { fields, columns: out }
}

export function columnsFromBatch(input: {
  headers: string[]
  mapping?: Record<string, string> | ResearchMappingV3
  suggested_mapping?: Record<string, string>
  sample_rows?: Record<string, string>[]
  column_map?: ColumnDraft[]
}): ColumnDraft[] {
  if (input.column_map?.length) {
    return input.column_map.map((col) => ({
      header: col.header,
      samples: col.samples || [],
      suggested_field: col.suggested_field || '',
      suggested_category: col.suggested_category || '',
      selected_field: col.selected_field || '',
      selected_category: col.selected_category || (col.ignored ? 'IGNORE' : ''),
      custom_key: col.custom_key || '',
      custom_label: col.custom_label || '',
      value_type: col.value_type || 'TEXT',
      ignored: Boolean(col.ignored),
      workflow_sensitive: Boolean(col.workflow_sensitive),
      warning: col.warning || '',
    }))
  }
  const suggested = input.suggested_mapping || suggestResearchMapping(input.headers)
  const selected = fieldsFromMapping(input.mapping)
  const selectedByHeader = Object.fromEntries(Object.entries(selected).map(([k, v]) => [v, k]))
  const suggestedByHeader = Object.fromEntries(Object.entries(suggested).map(([k, v]) => [v, k]))
  return input.headers.map((header) => {
    const selectedField = selectedByHeader[header] || ''
    const suggestedField = suggestedByHeader[header] || ''
    const norm = normalizeHeaderKey(header)
    const workflowSensitive = AMBIGUOUS_AUTOMAP.has(norm) || WORKFLOW_FIELDS.has(selectedField)
    const samples: string[] = []
    for (const row of input.sample_rows || []) {
      const value = (row[header] || '').trim()
      if (value && !samples.includes(value)) samples.push(value)
      if (samples.length >= 3) break
    }
    return {
      header,
      samples,
      suggested_field: suggestedField,
      suggested_category: FIELD_CATEGORY[suggestedField] || '',
      selected_field: selectedField,
      selected_category: selectedField ? FIELD_CATEGORY[selectedField] : '',
      custom_key: '',
      custom_label: '',
      value_type: 'TEXT',
      ignored: false,
      workflow_sensitive: workflowSensitive,
      warning: workflowSensitive
        ? 'Workflow-sensitive. Will not change CRM status, assignment, or follow-up in this phase.'
        : '',
    }
  })
}

export function validateResearchMapping(
  mapping: Record<string, string> | ResearchMappingV3,
  headers: string[],
): { ok: boolean; errors: string[] } {
  const errors: string[] = []
  const headerSet = new Set(headers)
  const mapped = fieldsFromMapping(mapping)
  if (!mapped.company_name || !headerSet.has(mapped.company_name)) {
    errors.push('Map a Company Name column before continuing.')
  }
  return { ok: errors.length === 0, errors }
}

export function mappingsEqual(
  a: Record<string, string> | ResearchMappingV3,
  b: Record<string, string> | ResearchMappingV3,
): boolean {
  return JSON.stringify(mappingFromComparable(a)) === JSON.stringify(mappingFromComparable(b))
}

function mappingFromComparable(mapping: Record<string, string> | ResearchMappingV3) {
  if (mapping && ('fields' in mapping || 'columns' in mapping)) return mapping
  return { fields: normalizeResearchMapping(mapping), columns: [] }
}

export function fieldsForCategory(category: string): ResearchMappingField[] {
  if (!category || category === 'IGNORE' || category === 'CUSTOM_ATTRIBUTE') return []
  return RESEARCH_MAPPING_FIELDS.filter((field) => field.category === category)
}
