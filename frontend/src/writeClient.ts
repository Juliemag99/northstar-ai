/** Client-scoped writes must name an explicit client. Never infer Carmeco. */

export const SELECT_CLIENT_FOR_WRITE =
  'Choose a specific client before saving. All My Clients cannot be used for writes.'

export function requireWriteClientId(clientId: number | null | undefined): number {
  if (clientId == null || !Number.isFinite(clientId) || clientId <= 0) {
    throw new Error(SELECT_CLIENT_FOR_WRITE)
  }
  return clientId
}
