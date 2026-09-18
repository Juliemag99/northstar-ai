/** Compact persistent marker that this is the NorthStar pilot, not LeadMaster. */
export function showNorthStarPilotIndicator(): boolean {
  return import.meta.env.VITE_NORTHSTAR_PILOT === 'true'
}

export function northStarPilotLabel(): string {
  return 'NORTHSTAR PILOT'
}
