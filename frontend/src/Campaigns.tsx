import { Link } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  archiveCampaign,
  bulkAssignUnassigned,
  completeCampaign,
  confirmCampaignRoute,
  createCampaign,
  fetchCampaigns,
  fetchUnassignedOpportunities,
  pauseCampaign,
  resumeCampaign,
  updateCampaign,
  companyWorkspaceHref,
} from './api/carmeco'
import type {
  CampaignChoice,
  CampaignSummary,
  UnassignedOpportunity,
} from './types/carmeco'

const PAGE_SIZE = 50
const UNASSIGNED_PAGE_SIZE = 50
const STATUSES = ['Draft', 'Active', 'Paused', 'Completed', 'Archived']

type AssignedClient = { client_id: number; client_name: string; client_code: string }

function display(value: string | null | undefined): string {
  return (value || '').trim() || '—'
}

function formatDate(value: string): string {
  const text = (value || '').trim()
  if (!text) return '—'
  const iso = text.includes('T') ? text : `${text}T00:00:00`
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return text
  return dt.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function unassignedKey(row: { client_id: number; company_id: number }): string {
  return `${row.client_id}:${row.company_id}`
}

function campaignHref(row: CampaignSummary, activeClientId: number | null): string {
  const clientId =
    row.client_id > 0
      ? row.client_id
      : activeClientId != null && activeClientId > 0
        ? activeClientId
        : 0
  if (clientId > 0) {
    return `/campaigns/${row.campaign_id}?client_id=${clientId}`
  }
  return `/campaigns/${row.campaign_id}`
}

function emptyForm(clientId: number | null): {
  client_id: number
  campaign_name: string
  description: string
  category: string
  owner_name: string
  status: string
  start_date: string
  end_date: string
} {
  return {
    client_id: clientId && clientId > 0 ? clientId : 0,
    campaign_name: '',
    description: '',
    category: '',
    owner_name: '',
    status: 'Draft',
    start_date: '',
    end_date: '',
  }
}

export default function Campaigns({
  activeClientId,
  activeClientName,
  assignedClients,
}: {
  activeClientId: number | null
  activeClientName: string
  assignedClients: AssignedClient[]
}) {
  const allClients = activeClientId === 0
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [status, setStatus] = useState('')
  const [owner, setOwner] = useState('')
  const [category, setCategory] = useState('')
  const [filterClientId, setFilterClientId] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [page, setPage] = useState(0)
  const [items, setItems] = useState<CampaignSummary[]>([])
  const [total, setTotal] = useState(0)
  const [owners, setOwners] = useState<string[]>([])
  const [categories, setCategories] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [editing, setEditing] = useState<CampaignSummary | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState(() => emptyForm(activeClientId))
  const [unassigned, setUnassigned] = useState<UnassignedOpportunity[]>([])
  const [unassignedCampaigns, setUnassignedCampaigns] = useState<CampaignChoice[]>([])
  const [unassignedTotal, setUnassignedTotal] = useState(0)
  const [unassignedPage, setUnassignedPage] = useState(0)
  const [unassignedQuery, setUnassignedQuery] = useState('')
  const [unassignedDebounced, setUnassignedDebounced] = useState('')
  const [assignPick, setAssignPick] = useState<Record<string, number>>({})
  const [assigningKey, setAssigningKey] = useState<string | null>(null)
  const [selectedKeys, setSelectedKeys] = useState<Record<string, true>>({})
  const [excludedKeys, setExcludedKeys] = useState<Record<string, true>>({})
  const [selectAllMatching, setSelectAllMatching] = useState(false)
  const [bulkCampaignId, setBulkCampaignId] = useState(0)
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [reloadTick, setReloadTick] = useState(0)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [query])

  useEffect(() => {
    setPage(0)
  }, [debouncedQuery, status, owner, category, filterClientId, dateFrom, dateTo, activeClientId])

  useEffect(() => {
    if (activeClientId == null) {
      setItems([])
      setTotal(0)
      setLoading(false)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    const scopedClient =
      allClients && filterClientId ? Number(filterClientId) : activeClientId
    void fetchCampaigns({
      client_id: scopedClient,
      status,
      owner,
      category,
      date_from: dateFrom,
      date_to: dateTo,
      q: debouncedQuery,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    })
      .then((data) => {
        if (cancelled) return
        setItems(data.items)
        setTotal(data.total)
        setOwners(data.owners)
        setCategories(data.categories)
      })
      .catch((err) => {
        if (cancelled) return
        setItems([])
        setTotal(0)
        setError(err instanceof Error ? err.message : 'Failed to load campaigns.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [
    activeClientId,
    allClients,
    filterClientId,
    status,
    owner,
    category,
    dateFrom,
    dateTo,
    debouncedQuery,
    page,
    reloadTick,
  ])

  useEffect(() => {
    const timer = window.setTimeout(() => setUnassignedDebounced(unassignedQuery.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [unassignedQuery])

  const unassignedClientId =
    allClients && filterClientId ? Number(filterClientId) : activeClientId ?? 0

  useEffect(() => {
    setUnassignedPage(0)
    setSelectedKeys({})
    setExcludedKeys({})
    setSelectAllMatching(false)
    setBulkCampaignId(0)
    setBulkConfirmOpen(false)
  }, [activeClientId, filterClientId, unassignedDebounced])

  useEffect(() => {
    if (activeClientId == null) {
      setUnassigned([])
      setUnassignedCampaigns([])
      setUnassignedTotal(0)
      return
    }
    let cancelled = false
    void fetchUnassignedOpportunities(unassignedClientId, {
      q: unassignedDebounced,
      limit: UNASSIGNED_PAGE_SIZE,
      offset: unassignedPage * UNASSIGNED_PAGE_SIZE,
    })
      .then((data) => {
        if (cancelled) return
        setUnassigned(data.items)
        setUnassignedCampaigns(data.campaigns || [])
        setUnassignedTotal(data.total || 0)
        setAssignPick((prev) => {
          const next = { ...prev }
          for (const item of data.items) {
            const key = unassignedKey(item)
            if (next[key] == null && item.recommended_campaign_id) {
              next[key] = item.recommended_campaign_id
            }
          }
          return next
        })
      })
      .catch(() => {
        if (!cancelled) {
          setUnassigned([])
          setUnassignedCampaigns([])
          setUnassignedTotal(0)
        }
      })
    return () => {
      cancelled = true
    }
  }, [activeClientId, unassignedClientId, unassignedDebounced, unassignedPage, reloadTick])

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const from = total === 0 ? 0 : safePage * PAGE_SIZE + 1
  const to = Math.min(total, (safePage + 1) * PAGE_SIZE)

  const clientFilterOptions = useMemo(
    () => assignedClients.filter((c) => c.client_id > 0),
    [assignedClients],
  )

  function openCreate() {
    setEditing(null)
    setCreating(true)
    setForm(emptyForm(activeClientId))
    setMessage(null)
  }

  function openEdit(row: CampaignSummary) {
    setCreating(false)
    setEditing(row)
    setForm({
      client_id: row.client_id,
      campaign_name: row.campaign_name,
      description: row.description,
      category: row.category,
      owner_name: row.owner_name,
      status: row.status,
      start_date: (row.start_date || '').slice(0, 10),
      end_date: (row.end_date || '').slice(0, 10),
    })
    setMessage(null)
  }

  async function saveForm() {
    if (!form.campaign_name.trim()) {
      setError('Campaign name is required.')
      return
    }
    const clientId = editing ? editing.client_id : form.client_id
    if (!clientId || clientId <= 0) {
      setError('Select a client for this campaign.')
      return
    }
    setBusyId(editing?.campaign_id ?? 0)
    setError(null)
    try {
      if (editing) {
        await updateCampaign(editing.campaign_id, {
          campaign_name: form.campaign_name,
          description: form.description,
          category: form.category,
          owner_name: form.owner_name,
          status: form.status,
          start_date: form.start_date,
          end_date: form.end_date,
        })
        setMessage(`Updated ${form.campaign_name}.`)
      } else {
        const created = await createCampaign({
          client_id: clientId,
          campaign_name: form.campaign_name,
          description: form.description,
          category: form.category,
          owner_name: form.owner_name,
          status: form.status,
          start_date: form.start_date,
          end_date: form.end_date,
        })
        setMessage(`Created ${created.campaign_name}.`)
      }
      setCreating(false)
      setEditing(null)
      setPage(0)
      const data = await fetchCampaigns({
        client_id: allClients && filterClientId ? Number(filterClientId) : activeClientId,
        status,
        owner,
        category,
        date_from: dateFrom,
        date_to: dateTo,
        q: debouncedQuery,
        limit: PAGE_SIZE,
        offset: 0,
      })
      setItems(data.items)
      setTotal(data.total)
      setOwners(data.owners)
      setCategories(data.categories)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save campaign.')
    } finally {
      setBusyId(null)
    }
  }

  async function runAction(row: CampaignSummary, kind: 'pause' | 'resume' | 'complete' | 'archive') {
    setBusyId(row.campaign_id)
    setError(null)
    try {
      const next =
        kind === 'pause'
          ? await pauseCampaign(row.campaign_id)
          : kind === 'resume'
            ? await resumeCampaign(row.campaign_id)
            : kind === 'complete'
              ? await completeCampaign(row.campaign_id)
              : await archiveCampaign(row.campaign_id)
      setItems((prev) => prev.map((item) => (item.campaign_id === next.campaign_id ? next : item)))
      setMessage(`${next.campaign_name} is now ${next.status}.`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update campaign.')
    } finally {
      setBusyId(null)
    }
  }

  async function assignUnassigned(row: UnassignedOpportunity) {
    const key = unassignedKey(row)
    const campaignId = assignPick[key] ?? row.recommended_campaign_id
    if (!campaignId) {
      setError('Select an active campaign to assign this opportunity.')
      return
    }
    setAssigningKey(key)
    setError(null)
    try {
      await confirmCampaignRoute({
        client_id: row.client_id,
        company_id: row.company_id,
        contact_id: row.contact_id,
        campaign_id: campaignId,
        source: row.source || 'unassigned',
      })
      setMessage(`${row.company_name} assigned to campaign.`)
      setReloadTick((tick) => tick + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not assign to campaign.')
    } finally {
      setAssigningKey(null)
    }
  }

  const unassignedPageCount = Math.max(1, Math.ceil(unassignedTotal / UNASSIGNED_PAGE_SIZE))
  const unassignedSafePage = Math.min(unassignedPage, unassignedPageCount - 1)
  const unassignedFrom =
    unassignedTotal === 0 ? 0 : unassignedSafePage * UNASSIGNED_PAGE_SIZE + 1
  const unassignedTo = Math.min(unassignedTotal, (unassignedSafePage + 1) * UNASSIGNED_PAGE_SIZE)
  const singleClientScope = unassignedClientId > 0
  const pageKeys = unassigned.map(unassignedKey)
  const pageSelectedCount = pageKeys.filter((key) =>
    selectAllMatching ? !excludedKeys[key] : Boolean(selectedKeys[key]),
  ).length
  const allPageSelected = pageKeys.length > 0 && pageSelectedCount === pageKeys.length
  const somePageSelected = pageSelectedCount > 0 && !allPageSelected
  const selectedCount = selectAllMatching
    ? Math.max(0, unassignedTotal - Object.keys(excludedKeys).length)
    : Object.keys(selectedKeys).length
  const selectedClientIds = selectAllMatching
    ? singleClientScope
      ? [unassignedClientId]
      : []
    : Array.from(
        new Set(Object.keys(selectedKeys).map((key) => Number(key.split(':')[0] || 0))),
      ).filter((id) => id > 0)
  const mixedClients = selectedClientIds.length > 1
  const bulkClientId = selectedClientIds.length === 1 ? selectedClientIds[0] : 0
  const bulkClientName =
    unassigned.find((row) => row.client_id === bulkClientId)?.client_name ||
    assignedClients.find((c) => c.client_id === bulkClientId)?.client_name ||
    activeClientName
  const bulkCampaigns = unassignedCampaigns.filter((campaign) => campaign.client_id === bulkClientId)
  const bulkCampaignName =
    bulkCampaigns.find((campaign) => campaign.campaign_id === bulkCampaignId)?.campaign_name || ''
  const matchingAvailable = unassignedTotal > UNASSIGNED_PAGE_SIZE && singleClientScope

  function isRowSelected(row: UnassignedOpportunity): boolean {
    const key = unassignedKey(row)
    if (selectAllMatching) return !excludedKeys[key]
    return Boolean(selectedKeys[key])
  }

  function toggleRow(row: UnassignedOpportunity, checked: boolean) {
    const key = unassignedKey(row)
    if (selectAllMatching) {
      setExcludedKeys((prev) => {
        const next = { ...prev }
        if (checked) delete next[key]
        else next[key] = true
        return next
      })
      return
    }
    setSelectedKeys((prev) => {
      const next = { ...prev }
      if (checked) next[key] = true
      else delete next[key]
      return next
    })
  }

  function togglePage(checked: boolean) {
    if (selectAllMatching) {
      setExcludedKeys((prev) => {
        const next = { ...prev }
        for (const key of pageKeys) {
          if (checked) delete next[key]
          else next[key] = true
        }
        return next
      })
      return
    }
    setSelectedKeys((prev) => {
      const next = { ...prev }
      for (const key of pageKeys) {
        if (checked) next[key] = true
        else delete next[key]
      }
      return next
    })
  }

  function clearSelection() {
    setSelectedKeys({})
    setExcludedKeys({})
    setSelectAllMatching(false)
    setBulkConfirmOpen(false)
  }

  function openBulkConfirm() {
    if (!selectedCount) {
      setError('Select at least one opportunity.')
      return
    }
    if (mixedClients || !bulkClientId) {
      setError('Select opportunities from one client only.')
      return
    }
    if (!bulkCampaignId) {
      setError('Select a campaign. Recommended campaigns are not assigned automatically.')
      return
    }
    setError(null)
    setBulkConfirmOpen(true)
  }

  async function runBulkAssign() {
    if (!bulkClientId || !bulkCampaignId) return
    setBulkBusy(true)
    setError(null)
    try {
      const result = await bulkAssignUnassigned({
        client_id: bulkClientId,
        campaign_id: bulkCampaignId,
        q: unassignedDebounced,
        select_all_matching: selectAllMatching,
        exclude_company_ids: selectAllMatching
          ? Object.keys(excludedKeys).map((key) => Number(key.split(':')[1] || 0))
          : [],
        company_ids: selectAllMatching
          ? []
          : Object.keys(selectedKeys).map((key) => Number(key.split(':')[1] || 0)),
      })
      setMessage(result.message)
      clearSelection()
      setBulkCampaignId(0)
      setReloadTick((tick) => tick + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not assign selected opportunities.')
    } finally {
      setBulkBusy(false)
      setBulkConfirmOpen(false)
    }
  }

  return (
    <>
      <div className="page-heading page-heading--split">
        <div>
          <h1>Campaigns</h1>
          <p>
            {allClients
              ? 'Campaigns across all clients you are authorized to access. Membership, notes, and results stay client-specific.'
              : `${activeClientName} campaigns. Shared companies and contacts can belong to more than one campaign without duplicating master records.`}
          </p>
        </div>
        <div className="heading-controls">
          <button type="button" className="ghost-btn" onClick={openCreate}>
            Create Campaign
          </button>
        </div>
      </div>

      {error && <p className="queue-error">{error}</p>}
      {message && <p className="queue-note">{message}</p>}

      <div className="milestone-form-grid" style={{ margin: '0.75rem 0' }}>
        <label className="edit-field">
          <span className="edit-field__label">Search</span>
          <input
            className="edit-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Name, tag, owner… e.g. DC Misc."
          />
        </label>
        {allClients && (
          <label className="edit-field">
            <span className="edit-field__label">Client</span>
            <select
              className="edit-select"
              value={filterClientId}
              onChange={(e) => setFilterClientId(e.target.value)}
            >
              <option value="">All authorized clients</option>
              {clientFilterOptions.map((c) => (
                <option key={c.client_id} value={String(c.client_id)}>
                  {c.client_name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="edit-field">
          <span className="edit-field__label">Status</span>
          <select className="edit-select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {STATUSES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Owner</span>
          <select className="edit-select" value={owner} onChange={(e) => setOwner(e.target.value)}>
            <option value="">All owners</option>
            {owners.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Category / tag</span>
          <select
            className="edit-select"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            <option value="">All categories</option>
            {categories.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Start from</span>
          <input
            className="edit-input"
            type="date"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
          />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Start to</span>
          <input
            className="edit-input"
            type="date"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
          />
        </label>
      </div>

      {(creating || editing) && (
        <section className="queue-card" style={{ marginBottom: '1rem' }}>
          <h2>{editing ? 'Edit campaign' : 'Create campaign'}</h2>
          <div className="milestone-form-grid">
            {(allClients || !editing) && (
              <label className="edit-field">
                <span className="edit-field__label">Client</span>
                <select
                  className="edit-select"
                  value={form.client_id || ''}
                  disabled={Boolean(editing)}
                  onChange={(e) => setForm((prev) => ({ ...prev, client_id: Number(e.target.value) }))}
                >
                  <option value="">Select client</option>
                  {clientFilterOptions.map((c) => (
                    <option key={c.client_id} value={c.client_id}>
                      {c.client_name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="edit-field">
              <span className="edit-field__label">Campaign name</span>
              <input
                className="edit-input"
                value={form.campaign_name}
                onChange={(e) => setForm((prev) => ({ ...prev, campaign_name: e.target.value }))}
                placeholder="DC Misc."
              />
            </label>
            <label className="edit-field">
              <span className="edit-field__label">Description</span>
              <input
                className="edit-input"
                value={form.description}
                onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
              />
            </label>
            <label className="edit-field">
              <span className="edit-field__label">Category / tag</span>
              <input
                className="edit-input"
                value={form.category}
                onChange={(e) => setForm((prev) => ({ ...prev, category: e.target.value }))}
                placeholder="DC Misc."
              />
            </label>
            <label className="edit-field">
              <span className="edit-field__label">Owner</span>
              <input
                className="edit-input"
                value={form.owner_name}
                onChange={(e) => setForm((prev) => ({ ...prev, owner_name: e.target.value }))}
              />
            </label>
            <label className="edit-field">
              <span className="edit-field__label">Status</span>
              <select
                className="edit-select"
                value={form.status}
                onChange={(e) => setForm((prev) => ({ ...prev, status: e.target.value }))}
              >
                {STATUSES.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <label className="edit-field">
              <span className="edit-field__label">Start date</span>
              <input
                className="edit-input"
                type="date"
                value={form.start_date}
                onChange={(e) => setForm((prev) => ({ ...prev, start_date: e.target.value }))}
              />
            </label>
            <label className="edit-field">
              <span className="edit-field__label">End date</span>
              <input
                className="edit-input"
                type="date"
                value={form.end_date}
                onChange={(e) => setForm((prev) => ({ ...prev, end_date: e.target.value }))}
              />
            </label>
          </div>
          <div className="heading-controls" style={{ marginTop: '0.75rem' }}>
            <button type="button" className="ghost-btn" onClick={() => void saveForm()} disabled={busyId != null}>
              Save
            </button>
            <button
              type="button"
              className="ghost-btn"
              onClick={() => {
                setCreating(false)
                setEditing(null)
              }}
            >
              Cancel
            </button>
          </div>
        </section>
      )}

      <section className="queue-card">
        {loading && <p className="queue-note">Loading campaigns…</p>}
        {!loading && total === 0 && <p className="queue-note">No campaigns match these filters.</p>}
        {!loading && items.length > 0 && (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <th>Campaign</th>
                  {allClients && <th>Client</th>}
                  <th>Description / category</th>
                  <th>Owner</th>
                  <th>Status</th>
                  <th>Start</th>
                  <th>End</th>
                  <th>Companies</th>
                  <th>Contacts</th>
                  <th>Calls</th>
                  <th>Follow-ups</th>
                  <th>Appointments</th>
                  <th>Outcomes</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={`${row.client_id}-${row.campaign_id}`}>
                    <td>
                      <Link to={campaignHref(row, activeClientId)}>
                        <strong>{display(row.campaign_name)}</strong>
                      </Link>
                    </td>
                    {allClients && <td>{display(row.client_name)}</td>}
                    <td>
                      {display(row.description)}
                      {row.category ? (
                        <span className="queue-sub">{row.category}</span>
                      ) : null}
                    </td>
                    <td>{display(row.owner_name)}</td>
                    <td>
                      <span className={`campaign-status campaign-status--${row.status.toLowerCase()}`}>
                        {row.status}
                      </span>
                    </td>
                    <td>{formatDate(row.start_date)}</td>
                    <td>{formatDate(row.end_date)}</td>
                    <td>{row.company_count}</td>
                    <td>{row.contact_count}</td>
                    <td>
                      <Link to={`/activities?client_id=${row.client_id}`}>{row.call_count}</Link>
                    </td>
                    <td>
                      <Link to={`/work-queue?client_id=${row.client_id}`}>{row.follow_up_count}</Link>
                    </td>
                    <td>
                      <Link to={`/appointments?client_id=${row.client_id}`}>
                        {row.appointment_count}
                      </Link>
                    </td>
                    <td>{row.outcome_count}</td>
                    <td>
                      <div className="campaign-actions">
                        <button type="button" onClick={() => openEdit(row)} disabled={busyId === row.campaign_id}>
                          Edit
                        </button>
                        {row.status === 'Paused' || row.status === 'Draft' ? (
                          <button
                            type="button"
                            onClick={() => void runAction(row, 'resume')}
                            disabled={busyId === row.campaign_id}
                          >
                            Resume
                          </button>
                        ) : row.status === 'Active' ? (
                          <button
                            type="button"
                            onClick={() => void runAction(row, 'pause')}
                            disabled={busyId === row.campaign_id}
                          >
                            Pause
                          </button>
                        ) : null}
                        {row.status !== 'Completed' && row.status !== 'Archived' ? (
                          <button
                            type="button"
                            onClick={() => void runAction(row, 'complete')}
                            disabled={busyId === row.campaign_id}
                          >
                            Complete
                          </button>
                        ) : null}
                        {row.status !== 'Archived' ? (
                          <button
                            type="button"
                            onClick={() => void runAction(row, 'archive')}
                            disabled={busyId === row.campaign_id}
                          >
                            Archive
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="heading-controls" style={{ marginTop: '0.85rem' }}>
          <button
            type="button"
            className="link-btn"
            disabled={safePage <= 0 || loading}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            Previous
          </button>
          <span className="queue-source">
            {total === 0 ? '0 campaigns' : `${from}–${to} of ${total}`}
          </span>
          <button
            type="button"
            className="link-btn"
            disabled={safePage >= pageCount - 1 || loading}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      </section>

      <section className="queue-card" style={{ marginTop: '1rem' }}>
        <h2>Unassigned Opportunities</h2>
        <p className="queue-note">
          Opportunities with no campaign assignment for the selected Active Client. Assigning
          uses the existing company and contact records.
        </p>
        <div className="milestone-form-grid" style={{ margin: '0.75rem 0' }}>
          <label className="edit-field">
            <span className="edit-field__label">Search unassigned</span>
            <input
              className="edit-input"
              value={unassignedQuery}
              onChange={(e) => setUnassignedQuery(e.target.value)}
              placeholder="Company, contact, source, status…"
            />
          </label>
        </div>
        {unassigned.length === 0 && unassignedTotal === 0 ? (
          <p className="queue-note">No unassigned opportunities.</p>
        ) : (
          <>
            <div className="unassigned-bulk-bar">
              <label className="unassigned-bulk-check">
                <input
                  type="checkbox"
                  checked={allPageSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = somePageSelected
                  }}
                  onChange={(e) => togglePage(e.target.checked)}
                />
                Select All on This Page
              </label>
              {unassignedTotal > UNASSIGNED_PAGE_SIZE ? (
                <div className="unassigned-matching">
                  {matchingAvailable ? (
                    <button
                      type="button"
                      className={`ghost-btn${selectAllMatching ? ' unassigned-matching--on' : ''}`}
                      onClick={() => {
                        setSelectAllMatching(true)
                        setSelectedKeys({})
                        setExcludedKeys({})
                      }}
                    >
                      Select All Matching Results
                    </button>
                  ) : (
                    <p className="queue-note" style={{ margin: 0 }}>
                      Filter to one client to select all matching results.
                    </p>
                  )}
                </div>
              ) : null}
              <span className="queue-source">
                {selectedCount} {selectedCount === 1 ? 'opportunity' : 'opportunities'} selected.
              </span>
              <select
                className="edit-select"
                value={bulkCampaignId || ''}
                onChange={(e) => setBulkCampaignId(Number(e.target.value) || 0)}
                disabled={!bulkClientId}
                aria-label="Bulk campaign"
              >
                <option value="">
                  {bulkClientId ? 'Select campaign' : 'Select opportunities from one client'}
                </option>
                {bulkCampaigns.map((campaign) => (
                  <option key={campaign.campaign_id} value={campaign.campaign_id}>
                    {campaign.campaign_name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="ghost-btn"
                disabled={bulkBusy || !selectedCount}
                onClick={openBulkConfirm}
              >
                Assign Selected
              </button>
              <button type="button" className="ghost-btn" onClick={clearSelection} disabled={bulkBusy}>
                Clear Selection
              </button>
            </div>
            {mixedClients ? (
              <p className="queue-error">Selected opportunities must belong to one client.</p>
            ) : null}
            <div className="queue-table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>
                      <span className="sr-only">Select</span>
                    </th>
                    {allClients && <th>Client</th>}
                    <th>Company</th>
                    <th>Contact</th>
                    <th>Opportunity source / type</th>
                    <th>Status</th>
                    <th>Date identified</th>
                    <th>Assigned rep</th>
                    <th>Recommended campaign</th>
                    <th>Assign to Campaign</th>
                  </tr>
                </thead>
                <tbody>
                  {unassigned.map((row) => {
                    const key = unassignedKey(row)
                    const rowCampaigns = unassignedCampaigns.filter(
                      (campaign) => campaign.client_id === row.client_id,
                    )
                    const selectedId = assignPick[key] ?? row.recommended_campaign_id ?? 0
                    return (
                      <tr key={`${row.client_id}-${row.company_id}-${row.unassigned_id}`}>
                        <td>
                          <input
                            type="checkbox"
                            checked={isRowSelected(row)}
                            onChange={(e) => toggleRow(row, e.target.checked)}
                            aria-label={`Select ${row.company_name}`}
                          />
                        </td>
                        {allClients && <td>{display(row.client_name)}</td>}
                        <td>
                          {row.external_record_no ? (
                            <Link to={companyWorkspaceHref(row.external_record_no, row.client_id)}>
                              <strong>{display(row.company_name)}</strong>
                            </Link>
                          ) : (
                            display(row.company_name)
                          )}
                        </td>
                        <td>
                          {row.contact_id ? (
                            <Link to={`/contacts/${row.contact_id}?client_id=${row.client_id}`}>
                              {display(row.contact_name)}
                            </Link>
                          ) : (
                            '—'
                          )}
                        </td>
                        <td>{display(row.source_type || row.source)}</td>
                        <td>{display(row.status)}</td>
                        <td>{formatDate(row.identified_at)}</td>
                        <td>{display(row.assigned_rep)}</td>
                        <td>{display(row.recommended_campaign_name)}</td>
                        <td>
                          <div className="heading-controls" style={{ gap: '0.4rem', flexWrap: 'wrap' }}>
                            <select
                              className="search-input"
                              value={selectedId || ''}
                              onChange={(event) =>
                                setAssignPick((prev) => ({
                                  ...prev,
                                  [key]: Number(event.target.value),
                                }))
                              }
                              aria-label={`Campaign for ${row.company_name}`}
                            >
                              <option value="">Select campaign</option>
                              {rowCampaigns.map((campaign) => (
                                <option key={campaign.campaign_id} value={campaign.campaign_id}>
                                  {campaign.campaign_name}
                                  {campaign.campaign_id === row.recommended_campaign_id
                                    ? ' (recommended)'
                                    : ''}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              className="link-btn"
                              disabled={!selectedId || assigningKey === key}
                              onClick={() => void assignUnassigned(row)}
                            >
                              {assigningKey === key ? 'Assigning…' : 'Assign to Campaign'}
                            </button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div className="heading-controls" style={{ marginTop: '0.85rem' }}>
              <button
                type="button"
                className="link-btn"
                disabled={unassignedSafePage <= 0}
                onClick={() => setUnassignedPage((p) => Math.max(0, p - 1))}
              >
                Previous
              </button>
              <span className="queue-source">
                {unassignedTotal === 0
                  ? '0 unassigned'
                  : `${unassignedFrom}–${unassignedTo} of ${unassignedTotal}`}
              </span>
              <button
                type="button"
                className="link-btn"
                disabled={unassignedSafePage >= unassignedPageCount - 1}
                onClick={() => setUnassignedPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </>
        )}
      </section>
      {bulkConfirmOpen
        ? createPortal(
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby="bulk-assign-title"
              style={{
                position: 'fixed',
                inset: 0,
                background: 'rgba(15, 23, 42, 0.45)',
                zIndex: 240,
                display: 'flex',
                alignItems: 'flex-start',
                justifyContent: 'center',
                padding: '2rem 1rem',
              }}
            >
              <section className="panel" style={{ maxWidth: '28rem', width: '100%', margin: 0 }}>
                <div className="panel-header">
                  <h2 id="bulk-assign-title">Assign to campaign</h2>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => setBulkConfirmOpen(false)}
                    disabled={bulkBusy}
                  >
                    Close
                  </button>
                </div>
                <p>
                  Assign {selectedCount} {bulkClientName}{' '}
                  {selectedCount === 1 ? 'opportunity' : 'opportunities'} to {bulkCampaignName}?
                </p>
                <div className="heading-controls" style={{ marginTop: '0.85rem' }}>
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={bulkBusy}
                    onClick={() => void runBulkAssign()}
                  >
                    {bulkBusy ? 'Assigning…' : 'Assign'}
                  </button>
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={bulkBusy}
                    onClick={() => setBulkConfirmOpen(false)}
                  >
                    Cancel
                  </button>
                </div>
              </section>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
