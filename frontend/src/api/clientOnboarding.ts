/** CSRF-aware client onboarding API helpers. */

import { apiFetch } from './http'

async function parseJson<T>(response: Response): Promise<T> {
  const raw = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail =
      raw && typeof raw === 'object' && 'detail' in raw
        ? String((raw as { detail: unknown }).detail)
        : `Request failed (${response.status})`
    throw new Error(detail)
  }
  return raw as T
}

export type OnboardingTemplateListItem = {
  id: string
  name: string
  description: string
}

export type OnboardingDraft = {
  draft_id: number
  client_id: number | null
  mode: string
  status: string
  current_step: number
  payload: {
    basics: Record<string, string>
    sells: Record<string, string>
    opportunities: Record<string, string>
    crm: {
      statuses: string[]
      default_status: string
      campaign_name: string
      campaign_description: string
      apply_assignments: boolean
      assignment_user_ids: number[]
    }
    overwrite_fields?: string[]
    field_tiers?: Record<string, string>
  }
  template_id: string
  copy_source_client_id: number | null
  completion_percent: number
  missing_required: string[]
  missing_recommended: string[]
  can_finish: boolean
  created_at: string
  updated_at: string
  finished_at: string
  forbidden_copy_categories?: string[]
  client_name?: string
}

export type CopyPreview = {
  source_client_id: number
  source_client_name: string
  destination_client_id: number | null
  draft_id: number | null
  fields: Array<{
    path: string
    source_value: string | string[]
    destination_value: string | string[]
    will_copy: boolean
    blocked_reason: string
  }>
  conflict_paths: string[]
  never_copied: string[]
  copyable_only: string[]
}

export async function fetchOnboardingTemplates(): Promise<{
  templates: OnboardingTemplateListItem[]
  field_tiers: Record<string, string>
  forbidden_copy_categories: string[]
}> {
  const response = await apiFetch('/api/admin/onboarding/templates')
  return parseJson(response)
}

export async function createOnboardingDraft(body: {
  client_id?: number | null
  template_id?: string | null
}): Promise<OnboardingDraft> {
  const response = await apiFetch('/api/admin/onboarding/drafts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson(response)
}

export async function fetchOnboardingDraft(draftId: number): Promise<OnboardingDraft> {
  const response = await apiFetch(`/api/admin/onboarding/drafts/${draftId}`)
  return parseJson(response)
}

export async function listOnboardingDrafts(): Promise<OnboardingDraft[]> {
  const response = await apiFetch('/api/admin/onboarding/drafts')
  return parseJson(response)
}

export async function saveOnboardingDraft(
  draftId: number,
  body: { current_step?: number; payload?: Record<string, unknown> },
): Promise<OnboardingDraft> {
  const response = await apiFetch(`/api/admin/onboarding/drafts/${draftId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson(response)
}

export async function applyOnboardingTemplate(
  draftId: number,
  templateId: string,
  overwritePopulated = false,
): Promise<OnboardingDraft> {
  const response = await apiFetch(
    `/api/admin/onboarding/drafts/${draftId}/apply-template`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        template_id: templateId,
        overwrite_populated: overwritePopulated,
      }),
    },
  )
  return parseJson(response)
}

export async function previewOnboardingCopy(body: {
  source_client_id: number
  draft_id?: number | null
}): Promise<CopyPreview> {
  const response = await apiFetch('/api/admin/onboarding/copy-preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson(response)
}

export async function applyOnboardingCopy(
  draftId: number,
  sourceClientId: number,
  overwriteFields: string[] = [],
): Promise<OnboardingDraft> {
  const response = await apiFetch(
    `/api/admin/onboarding/drafts/${draftId}/apply-copy`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_client_id: sourceClientId,
        overwrite_fields: overwriteFields,
      }),
    },
  )
  return parseJson(response)
}

export async function finishOnboardingDraft(draftId: number): Promise<{
  client_id: number
  setup_href: string
  created_new_client: boolean
  client_name: string
}> {
  const response = await apiFetch(`/api/admin/onboarding/drafts/${draftId}/finish`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  return parseJson(response)
}
