import type { StaffUser } from '../api/auth'

/** True only for a signed-in active administrator. Signed-out Julie fallback is not admin. */
export function staffCanAdminister(
  authenticated: boolean,
  user: StaffUser | null | undefined,
): boolean {
  return (
    authenticated === true &&
    user != null &&
    user.is_administrator === true &&
    user.active === true
  )
}
