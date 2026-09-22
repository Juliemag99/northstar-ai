import type { StaffUser } from '../api/auth'

const ROLE_LABELS: Record<string, string> = {
  revops_specialist: 'RevOps Specialist',
  revops_manager: 'RevOps Manager',
  system_administrator: 'Administrator',
  operations_admin: 'Operations Admin',
  appointment_setter: 'Appointment Setter',
  read_only: 'Read only',
}

/** Sidebar role under the signed-in name. Never hard-code Account Executive. */
export function staffRoleLabel(
  authenticated: boolean,
  user: StaffUser | null | undefined,
): string {
  if (!authenticated || user == null) return 'Staff'
  const role = String(user.staff_role || '').trim()
  if (role && ROLE_LABELS[role]) return ROLE_LABELS[role]
  if (user.is_administrator) return 'Administrator'
  return 'Staff'
}

/** Hide Administration, Client Setup, and Cross-Client from non-admins. Backend still enforces. */
export function staffNavItemVisible(navId: string, canAdminister: boolean): boolean {
  if (navId === 'administration' || navId === 'clients') return canAdminister
  if (navId === 'cross-client-opportunities') return canAdminister
  return true
}

export function isSingleAssignedClient(count: number): boolean {
  return count === 1
}

/**
 * Pin specialists with one ACL client onto that client.
 * Multi-client users may keep "All My Clients" (0).
 */
export function resolveActiveClientId(args: {
  availableClientIds: number[]
  storedOrCurrent: number | null
}): number | null {
  const ids = args.availableClientIds.filter((id) => Number.isFinite(id) && id > 0)
  if (ids.length === 0) return args.storedOrCurrent
  if (ids.length === 1) return ids[0]
  const current = args.storedOrCurrent
  if (current != null && current > 0 && ids.includes(current)) return current
  if (current === 0) return 0
  return ids[0]
}

/** Due work vs fresh calling stay visible even when appointments exist. */
export function dashboardStartHereMessage(args: {
  clientName: string
  newAssignments: number
  callsDue: number
  followUpsDueToday: number
  overdueFollowUps: number
}): string | null {
  const due = args.callsDue + args.followUpsDueToday + args.overdueFollowUps
  const name = args.clientName.trim() || 'this client'
  if (due > 0 && args.newAssignments > 0) {
    return `Due work is listed below. Your fresh calling book is New Assignments (${args.newAssignments}) for ${name}.`
  }
  if (due > 0) return null
  if (args.newAssignments > 0) {
    return `Nothing is scheduled due today for ${name}. Start with New Assignments (${args.newAssignments}) or Today’s Priority Prospects.`
  }
  return `Nothing is scheduled due today for ${name}. Open Work Queue or Today’s Priority Prospects to find the next company to call.`
}
