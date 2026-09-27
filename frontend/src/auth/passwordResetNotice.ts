export const PASSWORD_RESET_NOTICE_KEY = 'northstar.passwordResetNotice'

export const PASSWORD_RESET_NOTICE =
  'Your password was updated. Sign in with the new password.'

export function stagePasswordResetNotice(): void {
  sessionStorage.setItem(PASSWORD_RESET_NOTICE_KEY, PASSWORD_RESET_NOTICE)
}

export function takePasswordResetNotice(): string | null {
  const value = sessionStorage.getItem(PASSWORD_RESET_NOTICE_KEY)
  if (value) sessionStorage.removeItem(PASSWORD_RESET_NOTICE_KEY)
  return value
}
