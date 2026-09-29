import { isCampaignRouteStatus } from '../api/carmeco'
import { isAppointmentSetStatus } from '../appointmentSet'
import type { StaffUser } from '../api/auth'

type PermissionUser = Pick<StaffUser, 'permissions'> | null | undefined

/** True only when the session payload includes campaigns.manage. Missing permissions fail closed. */
export function staffCanManageCampaigns(user: PermissionUser): boolean {
  const permissions = user?.permissions
  if (!Array.isArray(permissions)) return false
  return permissions.includes('campaigns.manage')
}

/** Company Log Call route offer. False unless the user can manage campaigns. */
export function workQueueCallShouldRoute(
  user: PermissionUser,
  status: string,
  hasWorkspace: boolean,
  clientId: number,
): boolean {
  return (
    staffCanManageCampaigns(user) &&
    hasWorkspace &&
    clientId > 0 &&
    (isAppointmentSetStatus(status) || isCampaignRouteStatus(status))
  )
}

/**
 * True when a company Log Call save should stay on the company.
 * SAVE & NEXT stays only when a campaign route is actually offered.
 */
export function shouldStayOnCompanyAfterCallSave(
  andNext: boolean,
  shouldRoute: boolean,
): boolean {
  return !andNext || shouldRoute
}
