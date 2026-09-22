/** Preserve the originating list when opening Company/Contact Workspace. */

export const WORK_QUEUE_FROM = 'work-queue'
export const PROSPECTS_FROM = 'prospects'
export const SEARCH_FROM = 'search'
export const DASHBOARD_FROM = 'dashboard'
export const WORK_QUEUE_SCROLL_KEY = 'northstar_work_queue_scroll'
export const WORK_QUEUE_SCROLL_QUERY_KEY = 'northstar_work_queue_query'

export function isFromWorkQueue(from: string | null | undefined): boolean {
  return (from || '').trim().toLowerCase() === WORK_QUEUE_FROM
}

export function workspaceReturnPath(search: URLSearchParams): string {
  const from = (search.get('from') || '').trim().toLowerCase()
  if (from === 'ask-northstar') return '/ask-northstar'
  if (from === WORK_QUEUE_FROM) {
    const queue = search.get('queue') || ''
    return queue ? `/work-queue?${queue}` : '/work-queue'
  }
  if (from === PROSPECTS_FROM) {
    const list = search.get('list') || ''
    return list ? `/prospects?${list}` : '/prospects'
  }
  if (from === SEARCH_FROM || from === DASHBOARD_FROM) return '/'
  return '/prospects'
}

export function workspaceReturnLabel(search: URLSearchParams): string {
  const from = (search.get('from') || '').trim().toLowerCase()
  if (from === 'ask-northstar') return '← Back to Ask NorthStar'
  if (from === WORK_QUEUE_FROM) {
    const queue = new URLSearchParams(search.get('queue') || '')
    if ((queue.get('type') || '').toLowerCase() === 'new') {
      return '← Back to New Assignments'
    }
    return '← Back to Work Queue'
  }
  if (from === PROSPECTS_FROM) return '← Back to Prospects'
  if (from === SEARCH_FROM) return '← Back to Search'
  if (from === DASHBOARD_FROM) return '← Back to Dashboard'
  return '← Back to Prospects'
}

export function copyWorkspaceReturn(href: string, current: URLSearchParams): string {
  try {
    const url = new URL(href, 'http://northstar.local')
    for (const key of ['from', 'queue', 'list'] as const) {
      const value = current.get(key)
      if (value) url.searchParams.set(key, value)
    }
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return href
  }
}

export function appendWorkspaceOrigin(
  href: string,
  origin: { from: string; listQuery?: string; queueQuery?: string },
): string {
  try {
    const url = new URL(href, 'http://northstar.local')
    url.searchParams.set('from', origin.from)
    if (origin.listQuery) url.searchParams.set('list', origin.listQuery)
    if (origin.queueQuery) url.searchParams.set('queue', origin.queueQuery)
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return href
  }
}

export function captureWorkQueueScroll(queueQuery: string) {
  try {
    const main = document.querySelector('main.content')
    const top =
      main instanceof HTMLElement
        ? main.scrollTop
        : window.scrollY || document.documentElement.scrollTop || 0
    sessionStorage.setItem(WORK_QUEUE_SCROLL_KEY, String(top))
    sessionStorage.setItem(WORK_QUEUE_SCROLL_QUERY_KEY, queueQuery)
  } catch {
    /* private mode / quota */
  }
}

export function restoreWorkQueueScroll(queueQuery: string) {
  try {
    const savedQuery = sessionStorage.getItem(WORK_QUEUE_SCROLL_QUERY_KEY)
    if (savedQuery !== queueQuery) return
    const raw = sessionStorage.getItem(WORK_QUEUE_SCROLL_KEY)
    const top = Number(raw)
    if (!Number.isFinite(top) || top <= 0) return
    const apply = () => {
      const main = document.querySelector('main.content')
      if (main instanceof HTMLElement) main.scrollTop = top
      window.scrollTo(0, top)
    }
    apply()
    window.setTimeout(apply, 50)
  } catch {
    /* ignore */
  }
}
