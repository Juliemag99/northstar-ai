import type { StaffUser } from '../api/auth'

/** Specialist reports open on the authenticated rep. Admins keep All reps. */
export function defaultReportUserId(user: StaffUser | null | undefined): number {
  if (user == null || user.id <= 0) return 0
  if (user.is_administrator) return 0
  if (String(user.staff_role || '').trim() === 'revops_specialist') return user.id
  return 0
}

export function authenticatedActorName(
  user: StaffUser | null | undefined,
): string {
  const name = String(user?.full_name || '').trim()
  if (name) return name
  return String(user?.email || '').trim()
}
