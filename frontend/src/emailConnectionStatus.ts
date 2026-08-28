/** Display-only Gmail connection status. Never surface tokens or secrets. */

export type EmailConnectionUiStatus = 'connected' | 'not_connected' | 'expired' | 'error'

export function emailConnectionUiStatus(
  rawStatus: string | null | undefined,
  connected?: boolean,
): EmailConnectionUiStatus {
  const status = (rawStatus || '').trim().toLowerCase().replace(/\s+/g, '_')
  if (connected || status === 'connected') return 'connected'
  if (status === 'expired' || status === 'revoked') return 'expired'
  if (status === 'error') return 'error'
  return 'not_connected'
}

export function emailConnectionLabel(status: EmailConnectionUiStatus): string {
  if (status === 'connected') return 'Connected'
  if (status === 'expired') return 'Expired'
  if (status === 'error') return 'Error'
  return 'Not Connected'
}

export function sendEmailConnectionCaption(
  rawStatus: string | null | undefined,
  connected: boolean,
): string {
  const ui = emailConnectionUiStatus(rawStatus, connected)
  if (ui === 'connected') return 'Connected'
  if (ui === 'expired') return 'Not Connected — Preview Only (Expired)'
  if (ui === 'error') return 'Not Connected — Preview Only (Error)'
  return 'Not Connected — Preview Only'
}
