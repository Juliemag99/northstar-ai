/** Keep workspace URLs aligned with the Active Client switcher. */

const QUEUE_CONTEXT_KEYS = [
  'from',
  'queue_item',
  'work_type',
  'queue',
  'reason',
  'priority',
  'position',
  'total',
  'focus',
]

function isContactWorkspacePath(pathname: string): boolean {
  return /^\/contacts\/\d+\/?$/.test(pathname)
}

function isCompanyWorkspacePath(pathname: string): boolean {
  return /^\/companies\/[^/]+\/?$/.test(pathname)
}

function isResearchCompanyPath(pathname: string): boolean {
  return /^\/companies\/[^/]+\/research\/?$/.test(pathname)
}

function isWorkQueuePath(pathname: string): boolean {
  return pathname === '/work-queue' || pathname.startsWith('/work-queue/')
}

function isCampaignsPath(pathname: string): boolean {
  return pathname === '/campaigns' || /^\/campaigns\/\d+\/?$/.test(pathname)
}

function isReportsPath(pathname: string): boolean {
  return pathname === '/reports' || pathname.startsWith('/reports/')
}

function isCrossClientPath(pathname: string): boolean {
  return (
    pathname === '/cross-client-opportunities' ||
    pathname.startsWith('/cross-client-opportunities/')
  )
}

export function pathAfterActiveClientChange(
  pathname: string,
  search: string,
  nextClientId: number,
  options?: { resetCampaignWorkspace?: boolean },
): string | null {
  const raw = search.startsWith('?') ? search.slice(1) : search
  const params = new URLSearchParams(raw)
  const onContact = isContactWorkspacePath(pathname)
  const onResearchCompany = isResearchCompanyPath(pathname)
  const onCompany = isCompanyWorkspacePath(pathname) && !onResearchCompany
  const resetCampaignWorkspace = options?.resetCampaignWorkspace !== false

  if (onContact || onCompany || onResearchCompany) {
    if (nextClientId > 0) params.set('client_id', String(nextClientId))
    else params.delete('client_id')
    if (onCompany || onResearchCompany) {
      const keepAskFrom = params.get('from') === 'ask-northstar'
      for (const key of QUEUE_CONTEXT_KEYS) params.delete(key)
      if (keepAskFrom) params.set('from', 'ask-northstar')
    }
    const qs = params.toString()
    return qs ? `${pathname}?${qs}` : pathname
  }

  if (isWorkQueuePath(pathname)) {
    if (nextClientId > 0) params.set('client_id', String(nextClientId))
    else params.delete('client_id')
    const qs = params.toString()
    return qs ? `${pathname}?${qs}` : pathname
  }

  if (isCampaignsPath(pathname)) {
    if (nextClientId > 0) params.set('client_id', String(nextClientId))
    else params.delete('client_id')
    const qs = params.toString()
    const onWorkspace = /^\/campaigns\/\d+\/?$/.test(pathname)
    // Leave the workspace when Active Client actually changes. URL-sync must keep
    // /campaigns/:campaignId so opening a campaign does not bounce back to the list.
    const path = onWorkspace && resetCampaignWorkspace ? '/campaigns' : pathname
    return qs ? `${path}?${qs}` : path
  }

  if (isReportsPath(pathname)) {
    if (nextClientId > 0) params.set('client_id', String(nextClientId))
    else params.delete('client_id')
    const qs = params.toString()
    return qs ? `${pathname}?${qs}` : pathname
  }

  if (isCrossClientPath(pathname)) {
    if (nextClientId > 0) params.set('target_client_id', String(nextClientId))
    else params.delete('target_client_id')
    const qs = params.toString()
    return qs ? `${pathname}?${qs}` : pathname
  }

  return null
}
