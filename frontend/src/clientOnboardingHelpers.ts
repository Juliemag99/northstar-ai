/** Pure helpers for client onboarding wizard validation and unsaved-change checks. */

export const ONBOARDING_STEPS = [
  { id: 1, title: 'Client basics', key: 'basics' },
  { id: 2, title: 'What the client sells', key: 'sells' },
  { id: 3, title: 'Ideal opportunities', key: 'opportunities' },
  { id: 4, title: 'CRM workflow', key: 'crm' },
  { id: 5, title: 'Review and finish', key: 'review' },
] as const

export type FieldTier = 'required' | 'recommended' | 'optional'

export function tierForPath(
  path: string,
  tiers: Record<string, string> | undefined,
): FieldTier {
  const t = (tiers?.[path] || '').toLowerCase()
  if (t === 'required' || t === 'recommended' || t === 'optional') return t
  return 'optional'
}

export function statusesFromText(text: string): string[] {
  return text
    .split(/\n|,/)
    .map((s) => s.trim())
    .filter(Boolean)
}

export function draftIsDirty(
  savedJson: string,
  currentPayload: unknown,
  currentStep: number,
  savedStep: number,
): boolean {
  if (currentStep !== savedStep) return true
  try {
    return JSON.stringify(currentPayload) !== savedJson
  } catch {
    return true
  }
}

export function validateStepBasics(basics: Record<string, string>): string[] {
  const errors: string[] = []
  if (!String(basics.client_name || '').trim()) {
    errors.push('Client name is required.')
  }
  return errors
}

export function validateStepSells(sells: Record<string, string>): string[] {
  const errors: string[] = []
  if (!String(sells.primary_service || '').trim()) {
    errors.push('Primary service is required before finishing (recommended to enter now).')
  }
  return errors
}

export function validateStepCrm(crm: {
  campaign_name?: string
  statuses?: string[]
  default_status?: string
}): string[] {
  const errors: string[] = []
  if (!String(crm.campaign_name || '').trim()) {
    errors.push('Campaign name is required.')
  }
  if (!crm.statuses || crm.statuses.length === 0) {
    errors.push('Add at least one relationship status.')
  }
  if (!String(crm.default_status || '').trim()) {
    errors.push('Choose a default status.')
  }
  return errors
}

export function summarizeMissing(paths: string[]): string[] {
  return paths.map((p) => p.replace(/\./g, ' → '))
}
