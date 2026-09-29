import { apiFetch } from './http'

export const FEEDBACK_CATEGORIES = [
  'Problem',
  'Suggestion',
  'Confusing',
  'Data issue',
  'Something I like',
] as const

export const FEEDBACK_IMPACTS = ['Blocking me', 'Can continue', 'Minor'] as const

export const FEEDBACK_STATUSES = [
  'New',
  'Reviewed',
  'Planned',
  'Resolved',
  "Won't Change",
] as const

export type FeedbackItem = {
  id: number
  created_at: string
  user_id: number | null
  user_name: string
  user_email: string
  client_id: number | null
  client_name: string
  category: string
  impact: string
  body: string
  page_route: string
  company_id: number | null
  company_name: string
  contact_id: number | null
  contact_name: string
  status: string
}

export type FeedbackList = {
  items: FeedbackItem[]
  total: number
  limit: number
  offset: number
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown }
    if (typeof payload.detail === 'string' && payload.detail.trim()) return payload.detail
  } catch {
    /* response had no JSON body */
  }
  return fallback
}

export async function submitFeedback(body: {
  category: string
  impact: string
  body: string
  page_route: string
  client_id: number | null
  company_id: number | null
  contact_id: number | null
}): Promise<void> {
  const response = await apiFetch('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      category: body.category,
      impact: body.impact,
      body: body.body,
      page_route: body.page_route,
      client_id: body.client_id,
      company_id: body.company_id,
      contact_id: body.contact_id,
    }),
  })
  if (!response.ok) {
    throw new Error(await errorMessage(response, 'Could not send feedback.'))
  }
}

export async function listFeedback(limit = 50, offset = 0): Promise<FeedbackList> {
  const response = await apiFetch(`/api/feedback?limit=${limit}&offset=${offset}`)
  if (!response.ok) {
    throw new Error(await errorMessage(response, 'Could not load feedback.'))
  }
  return (await response.json()) as FeedbackList
}

export async function updateFeedbackStatus(id: number, status: string): Promise<FeedbackItem> {
  const response = await apiFetch(`/api/feedback/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  })
  if (!response.ok) {
    throw new Error(await errorMessage(response, 'Could not update feedback status.'))
  }
  return (await response.json()) as FeedbackItem
}
