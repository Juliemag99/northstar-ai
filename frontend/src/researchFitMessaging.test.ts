import { describe, expect, it } from 'vitest'
import {
  isCampaignCriteriaMissing,
  researchFitPresentation,
} from './researchFitMessaging'

describe('researchFitMessaging — completed research, criteria missing', () => {
  it('never presents Valmont-style criteria gap as Research Company or not-a-fit', () => {
    const stored = 'Campaign Criteria Not Configured'
    expect(isCampaignCriteriaMissing(stored)).toBe(true)

    const presentation = researchFitPresentation({
      fitResult: stored,
      clientName: 'Brown Industries',
      workingForClientId: 2,
    })

    expect(presentation.state).toBe('not_evaluated')
    expect(presentation.heading).toBe('Fit Not Yet Evaluated')
    expect(presentation.why.toLowerCase()).toContain(
      'no positive or negative fit determination has been made',
    )
    expect(presentation.why.toLowerCase()).toContain('company research is complete')
    expect(presentation.nextAction).toBe(
      "Configure Brown Industries' capabilities and target criteria to evaluate fit.",
    )
    expect(presentation.configureHref).toBe('/clients/2/setup')

    const blob = `${presentation.heading}\n${presentation.why}\n${presentation.nextAction}`.toLowerCase()
    for (const banned of [
      'research company',
      'little verified fit evidence',
      'not a fit',
      'weak fit',
    ]) {
      expect(blob).not.toContain(banned)
    }
  })

  it('keeps evaluated ratings distinct from criteria-missing', () => {
    expect(researchFitPresentation({ fitResult: 'Strong Fit' }).state).toBe('strong')
    expect(researchFitPresentation({ fitResult: 'Possible Fit' }).state).toBe('possible')
    expect(researchFitPresentation({ fitResult: 'Weak Fit' }).state).toBe('weak')
    expect(
      researchFitPresentation({ fitResult: 'Insufficient Information' }).state,
    ).toBe('insufficient')
    expect(
      researchFitPresentation({
        fitResult: 'Campaign Criteria Not Configured',
        researchFailed: true,
      }).state,
    ).toBe('research_unavailable')
  })
})
