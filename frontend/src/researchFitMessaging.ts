/** Presentation helpers for Research Company Campaign Fit messaging. */

export type ResearchFitPresentationState =
  | 'not_evaluated'
  | 'strong'
  | 'possible'
  | 'weak'
  | 'insufficient'
  | 'research_unavailable'

export type ResearchFitPresentation = {
  state: ResearchFitPresentationState
  heading: string
  why: string
  nextAction: string | null
  /** Existing Client Setup route when a client id is known. */
  configureHref: string | null
}

const CRITERIA_MISSING_RE =
  /criteria not configured|fit not yet evaluated/i

export function isCampaignCriteriaMissing(fitResult: string | null | undefined): boolean {
  return CRITERIA_MISSING_RE.test(String(fitResult || ''))
}

function clientPossessive(name: string): string {
  const n = String(name || 'this client').trim() || 'this client'
  if (n.toLowerCase().endsWith('s')) return `${n}'`
  return `${n}'s`
}

export function researchFitPresentation(params: {
  fitResult?: string | null
  decisionWhy?: string | null
  fitWhy?: string | null
  clientName?: string | null
  workingForClientId?: number | null
  researchFailed?: boolean
  researchIncomplete?: boolean
}): ResearchFitPresentation {
  const fit = String(params.fitResult || '').trim()
  const client = String(params.clientName || 'this client').trim() || 'this client'
  const clientId =
    params.workingForClientId != null && params.workingForClientId > 0
      ? params.workingForClientId
      : null

  if (params.researchFailed || params.researchIncomplete) {
    return {
      state: 'research_unavailable',
      heading: 'Research Incomplete',
      why:
        'Company evidence is unavailable or incomplete, so Campaign Fit cannot be evaluated yet.',
      nextAction: null,
      configureHref: null,
    }
  }

  if (isCampaignCriteriaMissing(fit)) {
    const poss = clientPossessive(client)
    return {
      state: 'not_evaluated',
      heading: 'Fit Not Yet Evaluated',
      why:
        'No positive or negative fit determination has been made. ' +
        'Company research is complete, but the active client’s capabilities and ' +
        'target criteria must be configured before Campaign Fit can be rated.',
      nextAction: `Configure ${poss} capabilities and target criteria to evaluate fit.`,
      configureHref: clientId != null ? `/clients/${clientId}/setup` : null,
    }
  }

  const key = fit.toLowerCase()
  if (key.includes('strong')) {
    return {
      state: 'strong',
      heading: fit || 'Strong Fit',
      why: String(params.decisionWhy || params.fitWhy || '').trim(),
      nextAction: null,
      configureHref: null,
    }
  }
  if (key.includes('possible') || key.includes('partial')) {
    return {
      state: 'possible',
      heading: fit || 'Possible Fit',
      why: String(params.decisionWhy || params.fitWhy || '').trim(),
      nextAction: null,
      configureHref: null,
    }
  }
  if (key.includes('weak') || key.includes('poor') || key.includes('not a fit')) {
    return {
      state: 'weak',
      heading: fit || 'Weak Fit',
      why: String(params.decisionWhy || params.fitWhy || '').trim(),
      nextAction: null,
      configureHref: null,
    }
  }

  return {
    state: 'insufficient',
    heading: fit || 'Insufficient Information',
    why: String(params.decisionWhy || params.fitWhy || '').trim(),
    nextAction: null,
    configureHref: null,
  }
}
