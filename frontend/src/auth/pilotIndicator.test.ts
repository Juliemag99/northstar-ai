import { describe, expect, it } from 'vitest'
import { northStarPilotLabel, showNorthStarPilotIndicator } from './pilotIndicator'

describe('pilotIndicator', () => {
  it('keeps the badge off unless VITE_NORTHSTAR_PILOT=true', () => {
    expect(showNorthStarPilotIndicator()).toBe(false)
    expect(northStarPilotLabel()).toBe('NORTHSTAR PILOT')
  })
})
