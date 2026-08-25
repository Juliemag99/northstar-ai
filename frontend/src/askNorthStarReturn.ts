/** Preserve Ask NorthStar results when opening a company or contact from the answer. */

import type { AskNorthStarResponse, AskScope } from './types/carmeco'

export const ASK_NORTHSTAR_PATH = '/ask-northstar'
export const ASK_NORTHSTAR_FROM = 'ask-northstar'

const SNAPSHOT_KEY = 'northstar_ask_return_snapshot'

export type AskReturnSnapshot = {
  question: string
  scope: AskScope
  pickedClientId: number | null
  effectiveClientId: number | null
  answer: AskNorthStarResponse | null
  scrollTop: number
}

export function isFromAskNorthStar(from: string | null | undefined): boolean {
  return from === ASK_NORTHSTAR_FROM
}

export function isAskRecordHref(href: string): boolean {
  if (!href) return false
  try {
    const url = new URL(href, 'http://northstar.local')
    const path = url.pathname
    if (/^\/contacts\/\d+\/?$/.test(path)) return true
    if (/^\/companies\/[^/]+\/research\/?$/.test(path)) return true
    if (/^\/companies\/[^/]+\/?$/.test(path)) return true
    return false
  } catch {
    return false
  }
}

export function withAskReturnParam(href: string): string {
  if (!href || !isAskRecordHref(href)) return href
  try {
    const url = new URL(href, 'http://northstar.local')
    url.searchParams.set('from', ASK_NORTHSTAR_FROM)
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return href
  }
}

export function askContactHref(contactId: number, clientId?: number | null): string {
  const params = new URLSearchParams()
  if (clientId != null && clientId > 0) params.set('client_id', String(clientId))
  const q = params.toString()
  return `/contacts/${contactId}${q ? `?${q}` : ''}`
}

export function captureAskScrollTop(): number {
  try {
    const main = document.querySelector('main.content')
    const mainTop = main instanceof HTMLElement ? main.scrollTop : 0
    const windowTop = window.scrollY || document.documentElement.scrollTop || 0
    return Math.max(mainTop, windowTop)
  } catch {
    return 0
  }
}

export function restoreAskScrollTop(scrollTop: number) {
  if (!Number.isFinite(scrollTop) || scrollTop <= 0) return
  const apply = () => {
    try {
      const main = document.querySelector('main.content')
      if (main instanceof HTMLElement) main.scrollTop = scrollTop
      window.scrollTo(0, scrollTop)
    } catch {
      /* ignore */
    }
  }
  apply()
}

export function saveAskReturnSnapshot(snapshot: AskReturnSnapshot) {
  try {
    window.sessionStorage.setItem(SNAPSHOT_KEY, JSON.stringify(snapshot))
  } catch {
    /* quota / private mode — Back can still go to /ask-northstar */
  }
}

export function readAskReturnSnapshot(): AskReturnSnapshot | null {
  try {
    const raw = window.sessionStorage.getItem(SNAPSHOT_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<AskReturnSnapshot>
    if (!parsed || typeof parsed !== 'object') return null
    const scope: AskScope = parsed.scope === 'active_client' ? 'active_client' : 'all'
    const pickedClientId =
      typeof parsed.pickedClientId === 'number' &&
      Number.isFinite(parsed.pickedClientId) &&
      parsed.pickedClientId > 0
        ? parsed.pickedClientId
        : null
    const effectiveClientId =
      typeof parsed.effectiveClientId === 'number' &&
      Number.isFinite(parsed.effectiveClientId) &&
      parsed.effectiveClientId > 0
        ? parsed.effectiveClientId
        : null
    const scrollTop =
      typeof parsed.scrollTop === 'number' && Number.isFinite(parsed.scrollTop)
        ? parsed.scrollTop
        : 0
    const question = typeof parsed.question === 'string' ? parsed.question : ''
    const answer =
      parsed.answer && typeof parsed.answer === 'object'
        ? (parsed.answer as AskNorthStarResponse)
        : null
    return {
      question,
      scope,
      pickedClientId,
      effectiveClientId,
      answer,
      scrollTop,
    }
  } catch {
    return null
  }
}

export function askReturnSnapshotMatchesPage(
  snapshot: AskReturnSnapshot | null,
  scope: AskScope,
  effectiveClientId: number | null,
): boolean {
  if (!snapshot) return false
  if (snapshot.scope !== scope) return false
  const snapClient =
    snapshot.effectiveClientId != null && snapshot.effectiveClientId > 0
      ? snapshot.effectiveClientId
      : null
  const pageClient =
    effectiveClientId != null && effectiveClientId > 0 ? effectiveClientId : null
  return snapClient === pageClient
}
