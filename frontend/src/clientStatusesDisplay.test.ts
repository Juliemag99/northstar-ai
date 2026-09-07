import { describe, expect, it } from 'vitest'
import {
  formatClientStatusChip,
  prospectMatchesStatusFilter,
  sortedClientStatuses,
} from './clientStatusesDisplay'
import type { ProspectListItem } from './types/carmeco'

function baseProspect(overrides: Partial<ProspectListItem> = {}): ProspectListItem {
  return {
    id: 1,
    external_record_no: 'NS-1',
    company: 'Acme',
    city: '',
    state: '',
    status: '',
    relationship_status: '',
    client_statuses: [],
    primary_contact: '',
    phone: '',
    last_updated: '',
    website: '',
    address: '',
    zip: '',
    customer_campaign: '',
    contact_count: 0,
    is_hot: false,
    has_appointment_set: false,
    has_quote: false,
    has_purchase_order: false,
    has_weblead: false,
    client_id: 0,
    client_code: '',
    client_name: '',
    relationship_id: 0,
    ...overrides,
  }
}

describe('All My Clients labeled status display', () => {
  it('formats labeled chips and sorts by client name', () => {
    const chips = sortedClientStatuses([
      {
        client_id: 3,
        client_code: 'dawson',
        client_name: 'Dawson Fabrication',
        status: 'Appointment Set',
        relationship_id: 10,
      },
      {
        client_id: 1,
        client_code: 'carmeco',
        client_name: 'Carmeco',
        status: 'New',
        relationship_id: 11,
      },
    ])
    expect(chips.map((c) => c.client_name)).toEqual(['Carmeco', 'Dawson Fabrication'])
    expect(formatClientStatusChip(chips[0])).toBe('Carmeco — New')
    expect(formatClientStatusChip(chips[1])).toBe('Dawson Fabrication — Appointment Set')
  })

  it('shows em dash label when status is missing', () => {
    expect(
      formatClientStatusChip({
        client_id: 1,
        client_code: 'carmeco',
        client_name: 'Carmeco',
        status: '',
        relationship_id: 1,
      }),
    ).toBe('Carmeco — —')
  })

  it('filters All-Clients when any labeled relationship matches', () => {
    const prospect = baseProspect({
      client_statuses: [
        {
          client_id: 1,
          client_code: 'carmeco',
          client_name: 'Carmeco',
          status: 'New',
          relationship_id: 1,
        },
        {
          client_id: 3,
          client_code: 'dawson',
          client_name: 'Dawson Fabrication',
          status: 'Appointment Set',
          relationship_id: 2,
        },
      ],
    })
    expect(prospectMatchesStatusFilter(prospect, 'Appointment Set', true)).toBe(true)
    expect(prospectMatchesStatusFilter(prospect, 'Hot Prospect', true)).toBe(false)
    // Must not rely on unlabeled status field in All-Clients mode.
    expect(prospect.status).toBe('')
  })

  it('keeps specific-client filtering on the single status field', () => {
    const prospect = baseProspect({
      status: 'Hot Prospect',
      relationship_status: 'Hot Prospect',
      client_id: 1,
      client_name: 'Carmeco',
      client_statuses: [],
    })
    expect(prospectMatchesStatusFilter(prospect, 'Hot Prospect', false)).toBe(true)
    expect(prospectMatchesStatusFilter(prospect, 'New', false)).toBe(false)
  })

  it('does not match All-Clients filter when chips are empty', () => {
    const prospect = baseProspect({ status: 'New', client_statuses: [] })
    expect(prospectMatchesStatusFilter(prospect, 'New', true)).toBe(false)
  })
})
