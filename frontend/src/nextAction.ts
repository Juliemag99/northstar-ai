/** Shared Next Action catalog helpers. Choices come from the backend API only. */

export type NextActionChoice = {
  code: string
  label: string
  aliases: string[]
  requires_detail: boolean
}

export type NextActionCatalog = {
  client_id: number
  choices: NextActionChoice[]
  default_for_closed_code: string
}

export type NextActionSelection = {
  code: string
  custom: string
}

function fold(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
}

export function emptyNextActionCatalog(): NextActionCatalog {
  return { client_id: 0, choices: [], default_for_closed_code: 'no_next_action' }
}

export function statusDefaultsNoNextAction(status: string): boolean {
  const key = status.trim().toLowerCase()
  if (!key) return false
  if (key.includes('do not call') || key === 'dnc') return true
  if (key === 'closed' || key.startsWith('closed')) return true
  return key.includes('disqualified')
}

export function matchNextAction(
  stored: string | null | undefined,
  catalog: NextActionCatalog | null,
): NextActionSelection {
  const value = (stored || '').trim()
  if (!catalog || catalog.choices.length === 0) {
    return { code: '', custom: value }
  }
  if (!value) return { code: '', custom: '' }
  const folded = fold(value)
  for (const choice of catalog.choices) {
    if (choice.requires_detail) continue
    const aliases = new Set((choice.aliases || []).map((alias) => fold(alias)))
    aliases.add(fold(choice.label))
    aliases.add(fold(choice.code))
    if (aliases.has(folded)) return { code: choice.code, custom: '' }
  }
  const other = catalog.choices.find((choice) => choice.requires_detail)
  return { code: other?.code || '', custom: value }
}

export function storedNextAction(
  selection: NextActionSelection,
  catalog: NextActionCatalog | null,
): string {
  const code = (selection.code || '').trim()
  const custom = (selection.custom || '').trim()
  if (!code) return custom
  const choice = catalog?.choices.find((item) => item.code === code)
  if (!choice) return custom
  if (choice.requires_detail) return custom
  return choice.label
}

export function displayNextAction(
  stored: string | null | undefined,
  catalog: NextActionCatalog | null,
): string {
  const value = (stored || '').trim()
  if (!value) return ''
  const matched = matchNextAction(value, catalog)
  if (!matched.code) return value
  const choice = catalog?.choices.find((item) => item.code === matched.code)
  if (choice?.requires_detail) return matched.custom || value
  return choice?.label || value
}

export function nextActionIsValid(
  selection: NextActionSelection,
  catalog: NextActionCatalog | null,
  { required = false }: { required?: boolean } = {},
): boolean {
  const code = (selection.code || '').trim()
  if (!code) return !required
  const choice = catalog?.choices.find((item) => item.code === code)
  if (!choice) return !required
  if (choice.requires_detail) return Boolean(selection.custom.trim())
  return true
}

export function defaultNextActionForStatus(
  status: string,
  catalog: NextActionCatalog | null,
  current: NextActionSelection,
): NextActionSelection {
  if (!statusDefaultsNoNextAction(status)) return current
  if (current.code) return current
  return { code: catalog?.default_for_closed_code || 'no_next_action', custom: '' }
}
