import { Link } from 'react-router-dom'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  companyWorkspaceHref,
  downloadReportCsv,
  fetchCampaignPerformanceReport,
  fetchClientResultsReport,
  fetchReportFilters,
  fetchReportRecords,
  fetchTeamPerformanceReport,
  type ReportQueryParams,
} from './api/carmeco'
import type {
  ReportCampaignResponse,
  ReportClientResultsResponse,
  ReportFiltersResponse,
  ReportKpi,
  ReportRecordRow,
  ReportRecordsResponse,
  ReportTeamResponse,
} from './types/carmeco'

const PAGE_SIZE = 50

type ReportSection = 'client-results' | 'team-performance' | 'campaign-performance'

type DrillTarget = {
  section: ReportSection
  metric: string
  label: string
  row_client_id?: number
  row_user_id?: number
  row_campaign_id?: number
}

function currentMonthRange(): { from: string; to: string } {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return { from: `${year}-${month}-01`, to: `${year}-${month}-${day}` }
}

function display(value: string | number | null | undefined): string {
  if (value == null) return '—'
  const text = String(value).trim()
  return text || '—'
}

function formatPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value}%`
}

function formatWhen(value: string): string {
  const text = (value || '').trim()
  if (!text) return '—'
  const iso = text.includes('T') ? text : text.replace(' ', 'T')
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return text
  return dt.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function formatKpi(value: number | null | undefined, key: string): string {
  if (value == null) return '—'
  if (key.endsWith('_pct')) return formatPct(value)
  return String(value)
}

function rangeLabel(from: string, to: string): string {
  if (!from && !to) return 'Current month'
  if (from && to) return `${from} to ${to}`
  return from || to
}

function companyHref(row: ReportRecordRow): string {
  if (row.external_record_no && row.client_id > 0) {
    return companyWorkspaceHref(row.external_record_no, row.client_id)
  }
  return '#'
}

function contactHref(row: ReportRecordRow): string {
  if (row.contact_id != null && row.contact_id > 0) {
    return `/contacts/${row.contact_id}?client_id=${row.client_id}`
  }
  return companyHref(row)
}

function SortTh({
  id,
  label,
  title,
  sortBy,
  sortDir,
  onSort,
}: {
  id: string
  label: string
  title?: string
  sortBy: string
  sortDir: string
  onSort: (id: string) => void
}) {
  const active = sortBy === id
  return (
    <th aria-sort={active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button
        type="button"
        className="sort-th"
        title={title}
        onClick={() => onSort(id)}
      >
        {label}
        {active ? (sortDir === 'asc' ? ' ▲' : ' ▼') : ''}
      </button>
    </th>
  )
}

function CountButton({
  value,
  onClick,
  title,
  label,
}: {
  value: number
  onClick: () => void
  title?: string
  label: string
}) {
  if (value <= 0) return <span title={title}>0</span>
  return (
    <button
      type="button"
      className="count-btn"
      title={title || `Open ${label}`}
      aria-label={`Open ${value} ${label}`}
      onClick={(event) => {
        event.preventDefault()
        event.stopPropagation()
        onClick()
      }}
    >
      {value}
    </button>
  )
}

function KpiGrid({
  kpis,
  onSelect,
}: {
  kpis: ReportKpi[]
  onSelect: (kpi: ReportKpi) => void
}) {
  if (!kpis.length) return null
  return (
    <div className="stat-grid stat-grid--reports">
      {kpis.map((kpi) => {
        const clickable = !kpi.key.endsWith('_pct') && kpi.value != null && kpi.value > 0
        return (
          <button
            key={kpi.key}
            type="button"
            className={`stat-card${clickable ? ' stat-card--clickable' : ''}`}
            title={kpi.definition || kpi.label}
            disabled={!clickable}
            onClick={(event) => {
              event.preventDefault()
              event.stopPropagation()
              if (clickable) onSelect(kpi)
            }}
          >
            <div className="stat-card__top">
              <span className="stat-label">{kpi.label}</span>
            </div>
            <strong className="stat-value">{formatKpi(kpi.value, kpi.key)}</strong>
          </button>
        )
      })}
    </div>
  )
}

function Pager({
  total,
  page,
  pageSize,
  onPage,
}: {
  total: number
  page: number
  pageSize: number
  onPage: (page: number) => void
}) {
  if (total <= pageSize) return null
  const last = Math.max(0, Math.ceil(total / pageSize) - 1)
  const from = page * pageSize + 1
  const to = Math.min(total, (page + 1) * pageSize)
  return (
    <div className="reports-pager">
      <span className="queue-sub">
        Showing {from}–{to} of {total}
      </span>
      <button type="button" className="link-btn" disabled={page <= 0} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <button type="button" className="link-btn" disabled={page >= last} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </div>
  )
}

export default function Reports({
  activeClientId,
  activeClientName,
}: {
  activeClientId: number | null
  activeClientName: string
}) {
  const defaults = useMemo(() => currentMonthRange(), [])
  const [dateFrom, setDateFrom] = useState(defaults.from)
  const [dateTo, setDateTo] = useState(defaults.to)
  const [userId, setUserId] = useState(0)
  const [campaignId, setCampaignId] = useState(0)
  const [kindFilter, setKindFilter] = useState('')
  const [filters, setFilters] = useState<ReportFiltersResponse | null>(null)
  const [clientPage, setClientPage] = useState(0)
  const [teamPage, setTeamPage] = useState(0)
  const [campaignPage, setCampaignPage] = useState(0)
  const [clientSort, setClientSort] = useState({ by: 'client_name', dir: 'asc' })
  const [teamSort, setTeamSort] = useState({ by: 'user_name', dir: 'asc' })
  const [campaignSort, setCampaignSort] = useState({ by: 'campaign_name', dir: 'asc' })
  const [clientReport, setClientReport] = useState<ReportClientResultsResponse | null>(null)
  const [teamReport, setTeamReport] = useState<ReportTeamResponse | null>(null)
  const [campaignReport, setCampaignReport] = useState<ReportCampaignResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState<string | null>(null)
  const [drill, setDrill] = useState<DrillTarget | null>(null)
  const [drillPage, setDrillPage] = useState(0)
  const [drillRows, setDrillRows] = useState<ReportRecordsResponse | null>(null)
  const [drillLoading, setDrillLoading] = useState(false)
  const [drillError, setDrillError] = useState<string | null>(null)
  const drillRef = useRef<HTMLDivElement>(null)

  const activityType = kindFilter.startsWith('type:') ? kindFilter.slice(5) : ''
  const outcome = kindFilter.startsWith('outcome:') ? kindFilter.slice(8) : ''
  const scopedClientId = activeClientId != null && activeClientId > 0 ? activeClientId : 0

  const baseParams = useMemo<ReportQueryParams>(
    () => ({
      client_id: scopedClientId,
      date_from: dateFrom,
      date_to: dateTo,
      user_id: userId || undefined,
      campaign_id: campaignId || undefined,
      activity_type: activityType || undefined,
      outcome: outcome || undefined,
    }),
    [scopedClientId, dateFrom, dateTo, userId, campaignId, activityType, outcome],
  )

  useEffect(() => {
    setClientPage(0)
    setTeamPage(0)
    setCampaignPage(0)
    setDrill(null)
  }, [scopedClientId, dateFrom, dateTo, userId, campaignId, kindFilter])

  useEffect(() => {
    let cancelled = false
    void fetchReportFilters(scopedClientId)
      .then((data) => {
        if (!cancelled) setFilters(data)
      })
      .catch(() => {
        if (!cancelled) setFilters(null)
      })
    return () => {
      cancelled = true
    }
  }, [scopedClientId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([
      fetchClientResultsReport({
        ...baseParams,
        sort_by: clientSort.by,
        sort_dir: clientSort.dir,
        limit: PAGE_SIZE,
        offset: clientPage * PAGE_SIZE,
      }),
      fetchTeamPerformanceReport({
        ...baseParams,
        sort_by: teamSort.by,
        sort_dir: teamSort.dir,
        limit: PAGE_SIZE,
        offset: teamPage * PAGE_SIZE,
      }),
      fetchCampaignPerformanceReport({
        ...baseParams,
        sort_by: campaignSort.by,
        sort_dir: campaignSort.dir,
        limit: PAGE_SIZE,
        offset: campaignPage * PAGE_SIZE,
      }),
    ])
      .then(([clients, team, campaigns]) => {
        if (cancelled) return
        setClientReport(clients)
        setTeamReport(team)
        setCampaignReport(campaigns)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Could not load reports.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [baseParams, clientPage, teamPage, campaignPage, clientSort, teamSort, campaignSort])

  useEffect(() => {
    if (!drill) {
      setDrillRows(null)
      return
    }
    let cancelled = false
    setDrillLoading(true)
    void fetchReportRecords({
      ...baseParams,
      section: drill.section,
      metric: drill.metric,
      row_client_id: drill.row_client_id,
      row_user_id: drill.row_user_id,
      row_campaign_id: drill.row_campaign_id,
      limit: PAGE_SIZE,
      offset: drillPage * PAGE_SIZE,
    })
      .then((data) => {
        if (cancelled) return
        setDrillError(null)
        setDrillRows(data)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setDrillRows(null)
        setDrillError(err instanceof Error ? err.message : 'Could not load supporting records.')
      })
      .finally(() => {
        if (!cancelled) setDrillLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [baseParams, drill, drillPage])

  function toggleSort(
    current: { by: string; dir: string },
    set: (next: { by: string; dir: string }) => void,
    id: string,
  ) {
    if (current.by === id) {
      set({ by: id, dir: current.dir === 'asc' ? 'desc' : 'asc' })
    } else {
      set({ by: id, dir: id.includes('name') ? 'asc' : 'desc' })
    }
  }

  function openDrill(target: DrillTarget) {
    setDrill(target)
    setDrillPage(0)
    setDrillRows(null)
    setDrillError(null)
    setDrillLoading(true)
  }

  function closeDrill() {
    setDrill(null)
    setDrillRows(null)
    setDrillError(null)
    setDrillPage(0)
    setDrillLoading(false)
  }

  useEffect(() => {
    if (!drill) return
    const main = document.querySelector('main.content')
    if (main instanceof HTMLElement) {
      main.scrollTo({ top: 0, behavior: 'smooth' })
    } else {
      window.scrollTo({ top: 0, behavior: 'smooth' })
    }
    drillRef.current?.focus()
  }, [drill])

  function clearFilters() {
    const range = currentMonthRange()
    setDateFrom(range.from)
    setDateTo(range.to)
    setUserId(0)
    setCampaignId(0)
    setKindFilter('')
  }

  async function exportSection(section: ReportSection, opts?: { metric?: string; detailed?: boolean }) {
    const metric = opts?.metric
    const detailed = Boolean(opts?.detailed)
    setExporting(metric ? 'records' : detailed ? `${section}-detailed` : section)
    try {
      await downloadReportCsv({
        ...baseParams,
        section,
        metric,
        detail: detailed,
        row_client_id: drill?.row_client_id,
        row_user_id: drill?.row_user_id,
        row_campaign_id: drill?.row_campaign_id,
      })
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not export CSV.')
    } finally {
      setExporting(null)
    }
  }

  const scopeName = activeClientName || 'All My Clients'
  const definitions = filters?.definitions || {}

  function kpiTarget(section: ReportSection, kpi: ReportKpi): DrillTarget {
    const target: DrillTarget = { section, metric: kpi.key, label: kpi.label }
    if (section === 'client-results' && (clientReport?.items.length || 0) === 1) {
      target.row_client_id = clientReport!.items[0].client_id
    }
    if (section === 'team-performance' && (teamReport?.items.length || 0) === 1) {
      const uid = teamReport!.items[0].user_id
      if (uid) target.row_user_id = uid
    }
    if (section === 'campaign-performance' && (campaignReport?.items.length || 0) === 1) {
      const row = campaignReport!.items[0]
      target.row_campaign_id = row.campaign_id
      target.row_client_id = row.client_id
    }
    return target
  }

  return (
    <section className="panel panel--queue">
      <div className="page-heading page-heading--split">
        <div>
          <h1>Reports</h1>
          <p>
            Read-only results for {scopeName}. Date range {rangeLabel(dateFrom, dateTo)}.
            Counts stay on the client relationship — shared companies are not collapsed across clients.
          </p>
        </div>
      </div>

      <div className="milestone-form-grid reports-filters">
        <label className="edit-field">
          <span className="edit-field__label">Date from</span>
          <input
            className="edit-input"
            type="date"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
          />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Date to</span>
          <input
            className="edit-input"
            type="date"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
          />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Assigned rep / user</span>
          <select
            className="edit-select"
            value={userId ? String(userId) : ''}
            onChange={(e) => setUserId(Number(e.target.value) || 0)}
          >
            <option value="">All reps</option>
            {(filters?.reps || []).map((rep) => (
              <option key={rep.id} value={rep.id}>
                {rep.name}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Campaign</span>
          <select
            className="edit-select"
            value={campaignId ? String(campaignId) : ''}
            onChange={(e) => setCampaignId(Number(e.target.value) || 0)}
          >
            <option value="">All campaigns</option>
            {(filters?.campaigns || []).map((camp) => (
              <option key={camp.id} value={camp.id}>
                {camp.client_name ? `${camp.name} (${camp.client_name})` : camp.name}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Activity or outcome type</span>
          <select
            className="edit-select"
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
          >
            <option value="">All types</option>
            <optgroup label="Activity type">
              {(filters?.activity_types || []).map((item) => (
                <option key={`type:${item}`} value={`type:${item}`}>
                  {item}
                </option>
              ))}
            </optgroup>
            {(filters?.outcome_types || []).length > 0 ? (
              <optgroup label="Outcome">
                {(filters?.outcome_types || []).map((item) => (
                  <option key={`outcome:${item}`} value={`outcome:${item}`}>
                    {item}
                  </option>
                ))}
              </optgroup>
            ) : null}
          </select>
        </label>
        <div className="edit-field reports-filter-actions">
          <span className="edit-field__label">Filters</span>
          <button type="button" className="link-btn" onClick={clearFilters}>
            Clear Filters
          </button>
        </div>
      </div>

      {error ? (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      ) : null}
      {loading && !drill ? <p className="queue-sub">Loading reports…</p> : null}

      {drill ? (
        <div ref={drillRef} className="reports-section reports-drill" tabIndex={-1}>
          <div className="reports-drill__actions">
            <button type="button" className="primary-btn" onClick={closeDrill}>
              Back to Reports
            </button>
            <button
              type="button"
              className="link-btn"
              disabled={exporting != null}
              onClick={() => void exportSection(drill.section, { metric: drill.metric })}
            >
              {exporting === 'records' ? 'Exporting…' : 'Export records CSV'}
            </button>
            <button type="button" className="link-btn" onClick={closeDrill}>
              Close
            </button>
          </div>
          <div className="panel-header panel-header--sub">
            <h2>
              {drill.label}
              {drillRows ? ` (${drillRows.total})` : ''}
            </h2>
          </div>
          <p className="queue-sub">
            Supporting records for {scopeName}, {rangeLabel(dateFrom, dateTo)}. Filters stay applied.
          </p>
          {drillError ? (
            <p className="data-status data-status--error" role="alert">
              {drillError}
            </p>
          ) : null}
          {drillLoading ? <p className="queue-sub">Loading supporting records…</p> : null}
          {!drillLoading && !drillError && drillRows && drillRows.items.length === 0 ? (
            <p className="empty-state">No supporting records for this metric.</p>
          ) : null}
          {!drillLoading && drillRows && drillRows.items.length > 0 ? (
            <div className="queue-table-wrap">
              <table className="queue-table">
                <thead>
                  <tr>
                    <th>Date/time</th>
                    <th>Activity or record type</th>
                    <th>Client</th>
                    <th>Assigned rep/user</th>
                    <th>Company</th>
                    <th>Contact</th>
                    <th>Status/outcome</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {drillRows.items.map((row) => (
                    <tr key={row.record_key}>
                      <td>{formatWhen(row.occurred_at)}</td>
                      <td>{display(row.record_type)}</td>
                      <td>{display(row.client_name)}</td>
                      <td>{display(row.user_name)}</td>
                      <td>
                        {row.external_record_no ? (
                          <Link to={companyHref(row)} className="company-link">
                            {display(row.company_name)}
                          </Link>
                        ) : (
                          display(row.company_name)
                        )}
                      </td>
                      <td>
                        {row.contact_id != null && row.contact_id > 0 ? (
                          <Link to={contactHref(row)} className="company-link">
                            {display(row.contact_name)}
                          </Link>
                        ) : (
                          display(row.contact_name)
                        )}
                      </td>
                      <td>{display(row.outcome || row.status)}</td>
                      <td>
                        <span className="activity-notes">{display(row.notes)}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <Pager total={drillRows?.total || 0} page={drillPage} pageSize={PAGE_SIZE} onPage={setDrillPage} />
        </div>
      ) : (
        <>
      <div className="reports-section">
        <div className="panel-header panel-header--sub">
          <h2>Client Results</h2>
          <div className="heading-controls">
            <button
              type="button"
              className="primary-btn"
              disabled={exporting != null}
              onClick={() => void exportSection('client-results')}
            >
              {exporting === 'client-results' ? 'Exporting…' : 'Export Summary CSV'}
            </button>
            <button
              type="button"
              className="primary-btn"
              disabled={exporting != null}
              onClick={() => void exportSection('client-results', { detailed: true })}
            >
              {exporting === 'client-results-detailed' ? 'Exporting…' : 'Export Detailed CSV'}
            </button>
          </div>
        </div>
        <KpiGrid
          kpis={clientReport?.kpis || []}
          onSelect={(kpi) => openDrill(kpiTarget('client-results', kpi))}
        />
        {!loading && (clientReport?.items.length || 0) === 0 ? (
          <p className="empty-state">No client results match these filters for {scopeName}.</p>
        ) : (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <SortTh id="client_name" label="Client" sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="calls" label="Calls logged" title={definitions.calls} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="follow_ups_scheduled" label="Follow-ups scheduled" title={definitions.follow_ups_scheduled} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="follow_ups_completed" label="Follow-ups completed" title={definitions.follow_ups_completed} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="appointments_set" label="Appointments set" title={definitions.appointments_set} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="appointments_completed" label="Appointments completed" title={definitions.appointments_completed} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="appointments_cancelled" label="Appointments cancelled" title={definitions.appointments_cancelled} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="opportunities" label="Opportunities" title={definitions.opportunities} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="outcomes" label="Outcomes" title={definitions.outcomes} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                  <SortTh id="hot_prospects" label="Current Hot Prospects" title={definitions.hot_prospects} sortBy={clientSort.by} sortDir={clientSort.dir} onSort={(id) => toggleSort(clientSort, setClientSort, id)} />
                </tr>
              </thead>
              <tbody>
                {(clientReport?.items || []).map((row) => (
                  <tr key={row.client_id}>
                    <td>
                      <strong>{display(row.client_name)}</strong>
                    </td>
                    <td>
                      <CountButton value={row.calls} label="Calls logged" title={definitions.calls} onClick={() => openDrill({ section: 'client-results', metric: 'calls', label: 'Calls logged', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.follow_ups_scheduled} label="Follow-ups scheduled" title={definitions.follow_ups_scheduled} onClick={() => openDrill({ section: 'client-results', metric: 'follow_ups_scheduled', label: 'Follow-ups scheduled', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.follow_ups_completed} label="Follow-ups completed" title={definitions.follow_ups_completed} onClick={() => openDrill({ section: 'client-results', metric: 'follow_ups_completed', label: 'Follow-ups completed', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.appointments_set} label="Appointments set" title={definitions.appointments_set} onClick={() => openDrill({ section: 'client-results', metric: 'appointments_set', label: 'Appointments set', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.appointments_completed} label="Appointments completed" title={definitions.appointments_completed} onClick={() => openDrill({ section: 'client-results', metric: 'appointments_completed', label: 'Appointments completed', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.appointments_cancelled} label="Appointments cancelled" title={definitions.appointments_cancelled} onClick={() => openDrill({ section: 'client-results', metric: 'appointments_cancelled', label: 'Appointments cancelled', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.opportunities} label="Opportunities identified" title={definitions.opportunities} onClick={() => openDrill({ section: 'client-results', metric: 'opportunities', label: 'Opportunities identified', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.outcomes} label="Outcomes" title={definitions.outcomes} onClick={() => openDrill({ section: 'client-results', metric: 'outcomes', label: 'Outcomes', row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.hot_prospects} label="Current Hot Prospects" title={definitions.hot_prospects} onClick={() => openDrill({ section: 'client-results', metric: 'hot_prospects', label: 'Current Hot Prospects', row_client_id: row.client_id })} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager total={clientReport?.total || 0} page={clientPage} pageSize={PAGE_SIZE} onPage={setClientPage} />
      </div>

      <div className="reports-section">
        <div className="panel-header panel-header--sub">
          <h2>Team Performance</h2>
          <div className="heading-controls">
            <button
              type="button"
              className="primary-btn"
              disabled={exporting != null}
              onClick={() => void exportSection('team-performance')}
            >
              {exporting === 'team-performance' ? 'Exporting…' : 'Export Summary CSV'}
            </button>
            <button
              type="button"
              className="primary-btn"
              disabled={exporting != null}
              onClick={() => void exportSection('team-performance', { detailed: true })}
            >
              {exporting === 'team-performance-detailed' ? 'Exporting…' : 'Export Detailed CSV'}
            </button>
          </div>
        </div>
        <KpiGrid
          kpis={teamReport?.kpis || []}
          onSelect={(kpi) => openDrill(kpiTarget('team-performance', kpi))}
        />
        {!loading && (teamReport?.items.length || 0) === 0 ? (
          <p className="empty-state">No team performance rows match these filters for {scopeName}.</p>
        ) : (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <SortTh id="user_name" label="Revenue Development Specialist" sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="assigned_clients" label="Assigned clients" title={definitions.assigned_clients} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="calls" label="Calls" title={definitions.calls} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="notes" label="Notes" title={definitions.notes} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="follow_ups_completed" label="Follow-ups completed" title={definitions.follow_ups_completed} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="appointments_set" label="Appointments set" title={definitions.appointments_set} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="appointments_completed" label="Appointments completed" title={definitions.appointments_completed} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="outcomes" label="Outcomes" title={definitions.outcomes} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                  <SortTh id="overdue_tasks" label="Tasks currently overdue" title={definitions.overdue_tasks} sortBy={teamSort.by} sortDir={teamSort.dir} onSort={(id) => toggleSort(teamSort, setTeamSort, id)} />
                </tr>
              </thead>
              <tbody>
                {(teamReport?.items || []).map((row) => (
                  <tr key={row.user_id || row.user_name}>
                    <td>
                      <strong>{display(row.user_name)}</strong>
                    </td>
                    <td>
                      <CountButton value={row.assigned_clients} label="Assigned clients" title={definitions.assigned_clients} onClick={() => openDrill({ section: 'team-performance', metric: 'assigned_clients', label: 'Assigned clients', row_user_id: row.user_id || undefined })} />
                    </td>
                    <td>
                      <CountButton value={row.calls} label="Calls" title={definitions.calls} onClick={() => openDrill({ section: 'team-performance', metric: 'calls', label: 'Calls', row_user_id: row.user_id || undefined })} />
                    </td>
                    <td>
                      <CountButton value={row.notes} label="Notes" title={definitions.notes} onClick={() => openDrill({ section: 'team-performance', metric: 'notes', label: 'Notes', row_user_id: row.user_id || undefined })} />
                    </td>
                    <td>
                      <CountButton value={row.follow_ups_completed} label="Follow-ups completed" title={definitions.follow_ups_completed} onClick={() => openDrill({ section: 'team-performance', metric: 'follow_ups_completed', label: 'Follow-ups completed', row_user_id: row.user_id || undefined })} />
                    </td>
                    <td>
                      <CountButton value={row.appointments_set} label="Appointments set" title={definitions.appointments_set} onClick={() => openDrill({ section: 'team-performance', metric: 'appointments_set', label: 'Appointments set', row_user_id: row.user_id || undefined })} />
                    </td>
                    <td>
                      <CountButton value={row.appointments_completed} label="Appointments completed" title={definitions.appointments_completed} onClick={() => openDrill({ section: 'team-performance', metric: 'appointments_completed', label: 'Appointments completed', row_user_id: row.user_id || undefined })} />
                    </td>
                    <td>
                      <CountButton value={row.outcomes} label="Outcomes" title={definitions.outcomes} onClick={() => openDrill({ section: 'team-performance', metric: 'outcomes', label: 'Outcomes', row_user_id: row.user_id || undefined })} />
                    </td>
                    <td>
                      <CountButton value={row.overdue_tasks} label="Tasks currently overdue" title={definitions.overdue_tasks} onClick={() => openDrill({ section: 'team-performance', metric: 'overdue_tasks', label: 'Tasks currently overdue', row_user_id: row.user_id || undefined })} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager total={teamReport?.total || 0} page={teamPage} pageSize={PAGE_SIZE} onPage={setTeamPage} />
      </div>

      <div className="reports-section">
        <div className="panel-header panel-header--sub">
          <h2>Campaign Performance</h2>
          <div className="heading-controls">
            <button
              type="button"
              className="primary-btn"
              disabled={exporting != null}
              onClick={() => void exportSection('campaign-performance')}
            >
              {exporting === 'campaign-performance' ? 'Exporting…' : 'Export Summary CSV'}
            </button>
            <button
              type="button"
              className="primary-btn"
              disabled={exporting != null}
              onClick={() => void exportSection('campaign-performance', { detailed: true })}
            >
              {exporting === 'campaign-performance-detailed' ? 'Exporting…' : 'Export Detailed CSV'}
            </button>
          </div>
        </div>
        <p className="queue-sub">
          Activity is counted only after the company or contact joined the campaign. Earlier history is not credited.
        </p>
        <KpiGrid
          kpis={campaignReport?.kpis || []}
          onSelect={(kpi) => openDrill(kpiTarget('campaign-performance', kpi))}
        />
        {!loading && (campaignReport?.items.length || 0) === 0 ? (
          <p className="empty-state">No campaigns match these filters for {scopeName}.</p>
        ) : (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <SortTh id="campaign_name" label="Campaign" sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="client_name" label="Client" sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="companies" label="Companies" title={definitions.companies} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="contacts" label="Contacts" title={definitions.contacts} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="calls" label="Calls" title={definitions.calls} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="follow_ups" label="Follow-ups" title={definitions.follow_ups} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="appointments" label="Appointments" title={definitions.appointments} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="outcomes" label="Outcomes" title={definitions.outcomes} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="call_to_appointment_pct" label="Appt / companies" title={definitions.call_to_appointment_pct} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                  <SortTh id="outcome_to_call_pct" label="Outcomes / calls" title={definitions.outcome_to_call_pct} sortBy={campaignSort.by} sortDir={campaignSort.dir} onSort={(id) => toggleSort(campaignSort, setCampaignSort, id)} />
                </tr>
              </thead>
              <tbody>
                {(campaignReport?.items || []).map((row) => (
                  <tr key={row.campaign_id}>
                    <td>
                      <strong>{display(row.campaign_name)}</strong>
                    </td>
                    <td>{display(row.client_name)}</td>
                    <td>
                      <CountButton value={row.companies} label="Companies" title={definitions.companies} onClick={() => openDrill({ section: 'campaign-performance', metric: 'companies', label: 'Companies', row_campaign_id: row.campaign_id, row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.contacts} label="Contacts" title={definitions.contacts} onClick={() => openDrill({ section: 'campaign-performance', metric: 'contacts', label: 'Contacts', row_campaign_id: row.campaign_id, row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.calls} label="Calls" title={definitions.calls} onClick={() => openDrill({ section: 'campaign-performance', metric: 'calls', label: 'Calls', row_campaign_id: row.campaign_id, row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.follow_ups} label="Follow-ups" title={definitions.follow_ups} onClick={() => openDrill({ section: 'campaign-performance', metric: 'follow_ups', label: 'Follow-ups', row_campaign_id: row.campaign_id, row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.appointments} label="Appointments" title={definitions.appointments} onClick={() => openDrill({ section: 'campaign-performance', metric: 'appointments', label: 'Appointments', row_campaign_id: row.campaign_id, row_client_id: row.client_id })} />
                    </td>
                    <td>
                      <CountButton value={row.outcomes} label="Outcomes" title={definitions.outcomes} onClick={() => openDrill({ section: 'campaign-performance', metric: 'outcomes', label: 'Outcomes', row_campaign_id: row.campaign_id, row_client_id: row.client_id })} />
                    </td>
                    <td>{formatPct(row.call_to_appointment_pct)}</td>
                    <td>{formatPct(row.outcome_to_call_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager total={campaignReport?.total || 0} page={campaignPage} pageSize={PAGE_SIZE} onPage={setCampaignPage} />
      </div>
        </>
      )}
    </section>
  )
}
