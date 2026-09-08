import type { ClientRelationshipStatusChip, ProspectListItem } from './types/carmeco'

function asText(value: unknown): string {
  if (value == null) return ''
  return String(value).trim()
}

export function statusEquals(a: unknown, b: unknown): boolean {
  return asText(a).toLocaleLowerCase() === asText(b).toLocaleLowerCase()
}

export function prospectStatus(prospect: {
  status?: string
  relationship_status?: string
}): string {
  return asText(prospect.status) || asText(prospect.relationship_status)
}

/** Sort accessible client status chips by client name (stable). */
export function sortedClientStatuses(
  chips: ClientRelationshipStatusChip[] | undefined | null,
): ClientRelationshipStatusChip[] {
  const list = Array.isArray(chips) ? [...chips] : []
  return list.sort((a, b) => {
    const an = asText(a.client_name).toLocaleLowerCase()
    const bn = asText(b.client_name).toLocaleLowerCase()
    if (an < bn) return -1
    if (an > bn) return 1
    return (a.client_id || 0) - (b.client_id || 0)
  })
}

export function formatClientStatusChip(chip: ClientRelationshipStatusChip): string {
  const client = asText(chip.client_name) || 'Client'
  const status = asText(chip.status) || '—'
  return `${client} — ${status}`
}

/** All My Clients: never show an unlabeled single status. */
export function prospectMatchesStatusFilter(
  prospect: ProspectListItem,
  statusFilter: string,
  allClients: boolean,
): boolean {
  const wanted = asText(statusFilter)
  if (!wanted) return true
  if (allClients) {
    const chips = sortedClientStatuses(prospect.client_statuses)
    if (chips.length === 0) return false
    return chips.some((chip) => statusEquals(chip.status, wanted))
  }
  return statusEquals(prospectStatus(prospect), wanted)
}
