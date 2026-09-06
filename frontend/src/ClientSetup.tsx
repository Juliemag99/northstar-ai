import { Link, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import {
  deleteClientCampaign,
  fetchClientSetup,
  fetchClientSetupList,
  saveClientCampaign,
  saveClientOverview,
  saveClientSells,
  setClientDefaultCampaign,
  type ClientCampaign,
  type ClientSetup,
  type ClientSetupListItem,
} from './api/carmeco'
import { useAuth } from './auth/useAuth'
import { staffCanAdminister } from './auth/staffCanAdminister'

function display(value: string | null | undefined): string {
  const t = (value || '').trim()
  return t || '—'
}

function Field({
  label,
  value,
  onChange,
  canEdit,
  multiline,
  source,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  canEdit: boolean
  multiline?: boolean
  source?: string
}) {
  return (
    <label className="setup-field">
      <span className="setup-field-label">{label}</span>
      {canEdit ? (
        multiline ? (
          <textarea
            rows={3}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          />
        ) : (
          <input value={value} onChange={(e) => onChange(e.target.value)} />
        )
      ) : (
        <div className="setup-readonly">{display(value)}</div>
      )}
      {source ? <div className="queue-sub">Source: {source}</div> : null}
    </label>
  )
}

function CompletenessMissing({ items }: { items: string[] }) {
  if (!items.length) return <p className="queue-sub">Nothing missing in this category.</p>
  return (
    <ul className="setup-missing-list">
      {items.map((s) => (
        <li key={s}>{s}</li>
      ))}
    </ul>
  )
}

function ClientsHub() {
  const [rows, setRows] = useState<ClientSetupListItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const { user, authenticated } = useAuth()
  const isAdmin = staffCanAdminister(Boolean(authenticated), user)

  useEffect(() => {
    setLoading(true)
    fetchClientSetupList()
      .then(setRows)
      .catch((err) =>
        setError(err instanceof Error ? err.message : 'Failed to load clients.'),
      )
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="client-setup-page">
      <div className="page-heading page-heading--split">
        <div>
          <h1>Clients</h1>
          <p>Open Client Setup to define what each client sells, who they target, and campaigns.</p>
        </div>
        {isAdmin ? (
          <div className="heading-controls">
            <Link className="primary-btn" to="/clients/onboarding">
              Add Client
            </Link>
          </div>
        ) : null}
      </div>
      {loading && <p className="data-status">Loading clients…</p>}
      {error && (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      )}
      <section className="panel">
        <div className="panel-header">
          <h2>CLIENT SETUP</h2>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Client</th>
              <th>Status</th>
              <th>Completeness</th>
              <th>Default Campaign</th>
              <th>Campaigns</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.client_id}>
                <td>
                  <strong>{r.client_name}</strong>
                  <div className="queue-sub">{r.client_code}</div>
                </td>
                <td>{r.is_active ? 'Active' : 'Inactive'}</td>
                <td>
                  <div>
                    Client info {r.client_information_percent ?? r.completeness_percent}%
                  </div>
                  <div>
                    Target profile {r.target_profile_percent ?? r.completeness_percent}%
                  </div>
                  <div className="queue-sub">
                    AI Fit: {r.ai_fit_ready ? 'Ready' : 'Not Ready'}
                  </div>
                  {r.missing_sections.length > 0 && (
                    <div className="queue-sub">
                      Gaps: {r.missing_sections.slice(0, 3).join(', ')}
                      {r.missing_sections.length > 3 ? '…' : ''}
                    </div>
                  )}
                </td>
                <td>{display(r.default_campaign_name)}</td>
                <td>{r.campaign_count}</td>
                <td>
                  <Link className="link-btn" to={`/clients/${r.client_id}/setup`}>
                    Open Setup
                  </Link>
                  {isAdmin && (r.target_profile_percent ?? r.completeness_percent) < 100 ? (
                    <>
                      {' · '}
                      <Link
                        className="link-btn"
                        to={`/clients/onboarding?client_id=${r.client_id}`}
                      >
                        Complete Setup
                      </Link>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}

function emptyCampaignDraft(clientId: number): ClientCampaign {
  return {
    campaign_id: 0,
    client_id: clientId,
    campaign_name: '',
    description: '',
    active: true,
    is_default: false,
    primary_service: '',
    secondary_services: '',
    target_industries: '',
    target_customer_types: '',
    target_products: '',
    manufacturing_processes_sought: '',
    production_preference: '',
    stamping_capability: '',
    tooling_notes: '',
    geographic_preferences: '',
    geography_mode: '',
    geography_required: false,
    company_size_preferences: '',
    positive_signals: '',
    negative_signals: '',
    exclusions: '',
    target_titles: '',
    fit_weighting_notes: '',
    notes: '',
    list_source: '',
    import_date: '',
    created_at: '',
    updated_at: '',
  }
}

function ClientSetupDetail({ clientId }: { clientId: number }) {
  const navigate = useNavigate()
  const [data, setData] = useState<ClientSetup | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [editing, setEditing] = useState(false)
  const [selectedCampaignId, setSelectedCampaignId] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)

  const [overview, setOverview] = useState({
    client_name: '',
    website: '',
    main_location: '',
    main_phone: '',
    description: '',
    primary_owner_name: '',
    is_active: true,
  })
  const [sells, setSells] = useState({
    primary_service: '',
    secondary_services: '',
    products_services: '',
    differentiators: '',
    certifications: '',
    equipment_capacity: '',
    value_proposition: '',
  })
  const [campaign, setCampaign] = useState<ClientCampaign | null>(null)

  function applySetup(next: ClientSetup, preferCampaignId?: number | null) {
    setData(next)
    setOverview({
      client_name: next.client_name,
      website: next.website,
      main_location: next.main_location,
      main_phone: next.main_phone,
      description: next.description,
      primary_owner_name: next.primary_owner_name,
      is_active: next.is_active,
    })
    setSells({
      primary_service: next.primary_service,
      secondary_services: next.secondary_services,
      products_services: next.products_services,
      differentiators: next.differentiators,
      certifications: next.certifications,
      equipment_capacity: next.equipment_capacity,
      value_proposition: next.value_proposition,
    })
    const pickId =
      preferCampaignId ??
      selectedCampaignId ??
      next.default_campaign_id ??
      next.campaigns[0]?.campaign_id ??
      null
    setSelectedCampaignId(pickId)
    const found = next.campaigns.find((c) => c.campaign_id === pickId) || null
    setCampaign(found)
    setCreating(false)
  }

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetchClientSetup(clientId)
      .then((next) => applySetup(next))
      .catch((err) =>
        setError(err instanceof Error ? err.message : 'Failed to load client setup.'),
      )
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId])

  const canEdit = Boolean(data?.can_edit)
  const fieldsEditable = canEdit && editing

  function beginEdit() {
    if (!canEdit || !data) return
    setError(null)
    setMsg(null)
    applySetup(data, selectedCampaignId)
    setEditing(true)
  }

  function cancelEdit() {
    if (!data) return
    applySetup(data, selectedCampaignId)
    setCreating(false)
    setEditing(false)
    setError(null)
    setMsg(null)
  }

  async function saveAllChanges() {
    if (!canEdit || !editing || saving) return
    setSaving(true)
    setMsg(null)
    setError(null)
    try {
      let next = await saveClientOverview(clientId, overview)
      next = await saveClientSells(clientId, sells)
      if (campaign && (creating || campaign.campaign_id > 0)) {
        const body = {
          campaign_name: campaign.campaign_name,
          description: campaign.description,
          active: campaign.active,
          is_default: campaign.is_default,
          primary_service: campaign.primary_service,
          secondary_services: campaign.secondary_services,
          target_industries: campaign.target_industries,
          target_customer_types: campaign.target_customer_types,
          target_products: campaign.target_products,
          manufacturing_processes_sought: campaign.manufacturing_processes_sought,
          production_preference: campaign.production_preference,
          stamping_capability: campaign.stamping_capability,
          tooling_notes: campaign.tooling_notes,
          geographic_preferences: campaign.geographic_preferences,
          geography_mode: campaign.geography_mode,
          geography_required: campaign.geography_required,
          company_size_preferences: campaign.company_size_preferences,
          positive_signals: campaign.positive_signals,
          negative_signals: campaign.negative_signals,
          exclusions: campaign.exclusions,
          target_titles: campaign.target_titles,
          fit_weighting_notes: campaign.fit_weighting_notes,
          notes: campaign.notes,
        }
        next = await saveClientCampaign(
          clientId,
          creating ? null : campaign.campaign_id,
          body,
        )
      }
      applySetup(
        next,
        creating
          ? next.campaigns.find((c) => c.campaign_name === campaign?.campaign_name)
              ?.campaign_id
          : selectedCampaignId,
      )
      setEditing(false)
      setMsg('Client Setup saved.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  async function saveOverview() {
    if (!fieldsEditable || saving) return
    setSaving(true)
    setMsg(null)
    setError(null)
    try {
      const next = await saveClientOverview(clientId, overview)
      applySetup(next, selectedCampaignId)
      setMsg('Overview saved.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  async function saveSellsSection() {
    if (!fieldsEditable || saving) return
    setSaving(true)
    setMsg(null)
    setError(null)
    try {
      const next = await saveClientSells(clientId, sells)
      applySetup(next, selectedCampaignId)
      setMsg('What the client sells saved.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  async function saveCampaignSection() {
    if (!fieldsEditable || saving || !campaign) return
    setSaving(true)
    setMsg(null)
    setError(null)
    try {
      const body = {
        campaign_name: campaign.campaign_name,
        description: campaign.description,
        active: campaign.active,
        is_default: campaign.is_default,
        primary_service: campaign.primary_service,
        secondary_services: campaign.secondary_services,
        target_industries: campaign.target_industries,
        target_customer_types: campaign.target_customer_types,
        target_products: campaign.target_products,
        manufacturing_processes_sought: campaign.manufacturing_processes_sought,
        production_preference: campaign.production_preference,
        stamping_capability: campaign.stamping_capability,
        tooling_notes: campaign.tooling_notes,
        geographic_preferences: campaign.geographic_preferences,
        geography_mode: campaign.geography_mode,
        geography_required: campaign.geography_required,
        company_size_preferences: campaign.company_size_preferences,
        positive_signals: campaign.positive_signals,
        negative_signals: campaign.negative_signals,
        exclusions: campaign.exclusions,
        target_titles: campaign.target_titles,
        fit_weighting_notes: campaign.fit_weighting_notes,
        notes: campaign.notes,
      }
      const next = await saveClientCampaign(
        clientId,
        creating ? null : campaign.campaign_id,
        body,
      )
      applySetup(
        next,
        creating
          ? next.campaigns.find((c) => c.campaign_name === body.campaign_name)?.campaign_id
          : campaign.campaign_id,
      )
      setMsg(creating ? 'Campaign created.' : 'Campaign saved.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  function selectCampaign(id: number) {
    if (!data) return
    if (editing && creating) {
      // discard new draft when switching away
      setCreating(false)
    }
    setSelectedCampaignId(id)
    setCampaign(data.campaigns.find((c) => c.campaign_id === id) || null)
  }

  function startNewCampaign() {
    if (!fieldsEditable) return
    setCreating(true)
    setSelectedCampaignId(null)
    setCampaign(emptyCampaignDraft(clientId))
  }

  if (loading) return <p className="data-status">Loading client setup…</p>
  if (error && !data) {
    return (
      <p className="data-status data-status--error" role="alert">
        {error}
      </p>
    )
  }
  if (!data) return null

  const c = data.completeness
  const sources = data.field_sources || {}

  return (
    <div className="client-setup-page">
      <div className="page-heading page-heading--split">
        <div>
          <button type="button" className="link-btn back-link" onClick={() => navigate('/clients')}>
            ← Clients
          </button>
          <h1>{data.client_name} — Client Setup</h1>
          <p>
            Source of truth for Research Fit, Ask NorthStar, Cross-Client Opportunities, and
            prospecting.
          </p>
        </div>
        <div className="setup-actions">
          <Link className="link-btn" to={`/clients/${clientId}/knowledge`}>
            Client Knowledge &amp; Documents
          </Link>
          {canEdit && !editing ? (
            <button type="button" className="primary-btn" onClick={beginEdit}>
              Edit Client Setup
            </button>
          ) : null}
          {canEdit && editing ? (
            <>
              <button
                type="button"
                className="primary-btn"
                disabled={saving}
                onClick={() => void saveAllChanges()}
              >
                {saving ? 'Saving…' : 'Save Changes'}
              </button>
              <button type="button" className="link-btn" disabled={saving} onClick={cancelEdit}>
                Cancel
              </button>
            </>
          ) : null}
        </div>
      </div>

      <section className="panel setup-completeness-panel" aria-label="Completeness indicators">
        <div className="setup-completeness-grid">
          <div className="setup-completeness-score">
            <div className="queue-sub">Client Information</div>
            <strong>{c.client_information_percent ?? 0}% Complete</strong>
            <CompletenessMissing items={c.client_information_missing || []} />
          </div>
          <div className="setup-completeness-score">
            <div className="queue-sub">Target Profile</div>
            <strong>{c.target_profile_percent ?? 0}% Complete</strong>
            <CompletenessMissing items={c.target_profile_missing || []} />
          </div>
          <div className="setup-completeness-score">
            <div className="queue-sub">AI Fit</div>
            <strong>{c.ai_fit_ready ? 'Ready' : 'Not Ready'}</strong>
            <CompletenessMissing items={c.ai_fit_missing || []} />
            {(c.intentionally_unspecified || []).length > 0 ? (
              <>
                <div className="queue-sub" style={{ marginTop: '0.5rem' }}>
                  Intentionally unspecified (not errors):
                </div>
                <CompletenessMissing items={c.intentionally_unspecified || []} />
              </>
            ) : null}
            <p className="queue-sub">
              Strong Fit is blocked only when target-profile critical fields are missing — not by
              admin phone/owner/location gaps, and not by intentionally blank company-size or
              absolute-exclusion preferences.
            </p>
          </div>
        </div>
      </section>

      {!canEdit && (
        <p className="data-status">
          View only — Admin / Management / Rev Ops (authorized Client Setup editors) can edit.
        </p>
      )}
      {canEdit && editing ? (
        <p className="data-status">Editing Client Setup — Save Changes or Cancel when done.</p>
      ) : null}
      {msg && <p className="data-status">{msg}</p>}
      {error && (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      )}

      <section className="panel">
        <div className="panel-header panel-header--sub">
          <h3>A. Client Overview</h3>
          {fieldsEditable && (
            <button type="button" className="link-btn" disabled={saving} onClick={() => void saveOverview()}>
              Save overview
            </button>
          )}
        </div>
        <div className="setup-grid">
          <Field
            label="Client Name"
            value={overview.client_name}
            onChange={(v) => setOverview((p) => ({ ...p, client_name: v }))}
            canEdit={fieldsEditable}
            source={sources.client_name}
          />
          <Field
            label="Website"
            value={overview.website}
            onChange={(v) => setOverview((p) => ({ ...p, website: v }))}
            canEdit={fieldsEditable}
            source={sources.website}
          />
          <Field
            label="Main Location"
            value={overview.main_location}
            onChange={(v) => setOverview((p) => ({ ...p, main_location: v }))}
            canEdit={fieldsEditable}
            source={sources.main_location}
          />
          <Field
            label="Main Phone"
            value={overview.main_phone}
            onChange={(v) => setOverview((p) => ({ ...p, main_phone: v }))}
            canEdit={fieldsEditable}
            source={sources.main_phone}
          />
          <Field
            label="Client Description"
            value={overview.description}
            onChange={(v) => setOverview((p) => ({ ...p, description: v }))}
            canEdit={fieldsEditable}
            multiline
            source={sources.description}
          />
          <Field
            label="Primary NorthStar Contact / Owner"
            value={overview.primary_owner_name}
            onChange={(v) => setOverview((p) => ({ ...p, primary_owner_name: v }))}
            canEdit={fieldsEditable}
            source={sources.primary_owner_name}
          />
          <label className="setup-field">
            <span className="setup-field-label">Active / Inactive</span>
            {fieldsEditable ? (
              <select
                value={overview.is_active ? 'active' : 'inactive'}
                onChange={(e) =>
                  setOverview((p) => ({ ...p, is_active: e.target.value === 'active' }))
                }
              >
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
              </select>
            ) : (
              <div className="setup-readonly">{overview.is_active ? 'Active' : 'Inactive'}</div>
            )}
          </label>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header panel-header--sub">
          <h3>B. What the Client Sells</h3>
          {fieldsEditable && (
            <button
              type="button"
              className="primary-btn"
              disabled={saving}
              onClick={() => void saveSellsSection()}
            >
              Save sells
            </button>
          )}
        </div>
        <div className="setup-grid">
          <Field
            label="Primary service / capability"
            value={sells.primary_service}
            onChange={(v) => setSells((p) => ({ ...p, primary_service: v }))}
            canEdit={fieldsEditable}
            source={sources.primary_service}
          />
          <Field
            label="Secondary services / capabilities"
            value={sells.secondary_services}
            onChange={(v) => setSells((p) => ({ ...p, secondary_services: v }))}
            canEdit={fieldsEditable}
            multiline
            source={sources.secondary_services}
          />
          <Field
            label="Products / services offered"
            value={sells.products_services}
            onChange={(v) => setSells((p) => ({ ...p, products_services: v }))}
            canEdit={fieldsEditable}
            multiline
            source={sources.products_services}
          />
          <Field
            label="Key differentiators"
            value={sells.differentiators}
            onChange={(v) => setSells((p) => ({ ...p, differentiators: v }))}
            canEdit={fieldsEditable}
            multiline
          />
          <Field
            label="Certifications"
            value={sells.certifications}
            onChange={(v) => setSells((p) => ({ ...p, certifications: v }))}
            canEdit={fieldsEditable}
          />
          <Field
            label="Important equipment / capacity"
            value={sells.equipment_capacity}
            onChange={(v) => setSells((p) => ({ ...p, equipment_capacity: v }))}
            canEdit={fieldsEditable}
            multiline
            source={sources.equipment_capacity}
          />
          <Field
            label="Value proposition"
            value={sells.value_proposition}
            onChange={(v) => setSells((p) => ({ ...p, value_proposition: v }))}
            canEdit={fieldsEditable}
            multiline
            source={sources.value_proposition}
          />
        </div>
      </section>

      <section className="panel">
        <div className="panel-header panel-header--sub">
          <h3>Campaigns / Target Profiles</h3>
          {fieldsEditable && (
            <button type="button" className="link-btn" onClick={startNewCampaign}>
              + New campaign
            </button>
          )}
        </div>
        <p className="queue-sub">
          A client may have multiple campaigns. Research / Fit / Ask use the selected or default
          campaign — never invent campaign names.
        </p>
        <div className="setup-campaign-tabs">
          {data.campaigns.map((c) => (
            <button
              key={c.campaign_id}
              type="button"
              className={
                !creating && selectedCampaignId === c.campaign_id
                  ? 'setup-campaign-tab setup-campaign-tab--active'
                  : 'setup-campaign-tab'
              }
              onClick={() => selectCampaign(c.campaign_id)}
            >
              {c.campaign_name}
              {c.is_default ? ' · Default' : ''}
            </button>
          ))}
          {creating && (
            <span className="setup-campaign-tab setup-campaign-tab--active">New campaign</span>
          )}
        </div>

        {campaign && (
          <>
            <div className="setup-grid" style={{ marginTop: '1rem' }}>
              <Field
                label="Campaign name"
                value={campaign.campaign_name}
                onChange={(v) => setCampaign({ ...campaign, campaign_name: v })}
                canEdit={fieldsEditable}
              />
              <Field
                label="Description"
                value={campaign.description}
                onChange={(v) => setCampaign({ ...campaign, description: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <label className="setup-field">
                <span className="setup-field-label">Active</span>
                {fieldsEditable ? (
                  <select
                    value={campaign.active ? 'yes' : 'no'}
                    onChange={(e) =>
                      setCampaign({ ...campaign, active: e.target.value === 'yes' })
                    }
                  >
                    <option value="yes">Active</option>
                    <option value="no">Inactive</option>
                  </select>
                ) : (
                  <div className="setup-readonly">{campaign.active ? 'Active' : 'Inactive'}</div>
                )}
              </label>
            </div>

            <h4 className="setup-subhead">C. Ideal Customer Profile</h4>
            <div className="setup-grid">
              <Field
                label="Ideal customer types"
                value={campaign.target_customer_types}
                onChange={(v) => setCampaign({ ...campaign, target_customer_types: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Preferred industries"
                value={campaign.target_industries}
                onChange={(v) => setCampaign({ ...campaign, target_industries: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Product / part characteristics"
                value={campaign.target_products}
                onChange={(v) => setCampaign({ ...campaign, target_products: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Manufacturing processes sought"
                value={campaign.manufacturing_processes_sought}
                onChange={(v) =>
                  setCampaign({ ...campaign, manufacturing_processes_sought: v })
                }
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Production volume / new program / recurring preference"
                value={campaign.production_preference}
                onChange={(v) => setCampaign({ ...campaign, production_preference: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Existing tooling preference"
                value={campaign.tooling_notes}
                onChange={(v) => setCampaign({ ...campaign, tooling_notes: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Company-size preference"
                value={campaign.company_size_preferences}
                onChange={(v) => setCampaign({ ...campaign, company_size_preferences: v })}
                canEdit={fieldsEditable}
              />
              <Field
                label="Primary service (campaign)"
                value={campaign.primary_service}
                onChange={(v) => setCampaign({ ...campaign, primary_service: v })}
                canEdit={fieldsEditable}
              />
              <Field
                label="Secondary services (campaign)"
                value={campaign.secondary_services}
                onChange={(v) => setCampaign({ ...campaign, secondary_services: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Stamping / capacity notes"
                value={campaign.stamping_capability}
                onChange={(v) => setCampaign({ ...campaign, stamping_capability: v })}
                canEdit={fieldsEditable}
                multiline
              />
            </div>

            <h4 className="setup-subhead">D. Target Geography</h4>
            <div className="setup-grid">
              <label className="setup-field">
                <span className="setup-field-label">Geography mode</span>
                {fieldsEditable ? (
                  <select
                    value={campaign.geography_mode}
                    onChange={(e) =>
                      setCampaign({ ...campaign, geography_mode: e.target.value })
                    }
                  >
                    <option value="">—</option>
                    <option value="nationwide">Nationwide</option>
                    <option value="radius">Radius</option>
                    <option value="states">States / regions</option>
                  </select>
                ) : (
                  <div className="setup-readonly">{display(campaign.geography_mode)}</div>
                )}
              </label>
              <Field
                label="Geographic preferences"
                value={campaign.geographic_preferences}
                onChange={(v) => setCampaign({ ...campaign, geographic_preferences: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <label className="setup-field">
                <span className="setup-field-label">Preferred vs required</span>
                {fieldsEditable ? (
                  <select
                    value={campaign.geography_required ? 'required' : 'preferred'}
                    onChange={(e) =>
                      setCampaign({
                        ...campaign,
                        geography_required: e.target.value === 'required',
                      })
                    }
                  >
                    <option value="preferred">Preferred (soft)</option>
                    <option value="required">Required</option>
                  </select>
                ) : (
                  <div className="setup-readonly">
                    {campaign.geography_required ? 'Required' : 'Preferred (soft)'}
                  </div>
                )}
              </label>
            </div>

            <h4 className="setup-subhead">E. Positive Fit Signals</h4>
            <div className="setup-grid">
              <Field
                label="Positive signals"
                value={campaign.positive_signals}
                onChange={(v) => setCampaign({ ...campaign, positive_signals: v })}
                canEdit={fieldsEditable}
                multiline
              />
            </div>

            <h4 className="setup-subhead">F. Negative / Exclusion Signals</h4>
            <div className="setup-grid">
              <Field
                label="Negative signals"
                value={campaign.negative_signals}
                onChange={(v) => setCampaign({ ...campaign, negative_signals: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Exclusions"
                value={campaign.exclusions}
                onChange={(v) => setCampaign({ ...campaign, exclusions: v })}
                canEdit={fieldsEditable}
                multiline
              />
            </div>

            <h4 className="setup-subhead">G. Target Titles</h4>
            <div className="setup-grid">
              <Field
                label="Target titles"
                value={campaign.target_titles}
                onChange={(v) => setCampaign({ ...campaign, target_titles: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Fit weighting notes"
                value={campaign.fit_weighting_notes}
                onChange={(v) => setCampaign({ ...campaign, fit_weighting_notes: v })}
                canEdit={fieldsEditable}
                multiline
              />
              <Field
                label="Campaign notes"
                value={campaign.notes}
                onChange={(v) => setCampaign({ ...campaign, notes: v })}
                canEdit={fieldsEditable}
                multiline
              />
            </div>

            {fieldsEditable && (
              <div className="setup-actions">
                <button
                  type="button"
                  className="primary-btn"
                  disabled={saving}
                  onClick={() => void saveCampaignSection()}
                >
                  {creating ? 'Create campaign' : 'Save campaign'}
                </button>
                {!creating && campaign.campaign_id > 0 && !campaign.is_default && (
                  <button
                    type="button"
                    className="link-btn"
                    disabled={saving}
                    onClick={() => {
                      void setClientDefaultCampaign(clientId, campaign.campaign_id)
                        .then((next) => {
                          applySetup(next, campaign.campaign_id)
                          setMsg('Default campaign updated.')
                        })
                        .catch((err) =>
                          setError(err instanceof Error ? err.message : 'Update failed.'),
                        )
                    }}
                  >
                    Make default
                  </button>
                )}
                {!creating && data.campaigns.length > 1 && (
                  <button
                    type="button"
                    className="link-btn"
                    disabled={saving}
                    onClick={() => {
                      if (
                        !window.confirm(
                          `Delete campaign “${campaign.campaign_name}” for this client only?`,
                        )
                      ) {
                        return
                      }
                      void deleteClientCampaign(clientId, campaign.campaign_id)
                        .then((next) => {
                          applySetup(next, next.default_campaign_id)
                          setMsg('Campaign deleted.')
                        })
                        .catch((err) =>
                          setError(err instanceof Error ? err.message : 'Delete failed.'),
                        )
                    }}
                  >
                    Delete campaign
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </section>

      {data.audit_history.length > 0 && (
        <section className="panel">
          <div className="panel-header">
            <h2>AUDIT HISTORY</h2>
          </div>
          <ul className="setup-audit-list">
            {data.audit_history.slice(0, 20).map((a) => (
              <li key={a.id}>
                <strong>{a.field_name}</strong>
                {' · '}
                {display(a.changed_by_name)} · {display(a.changed_at)}
                {a.change_source ? (
                  <span className="queue-sub"> · {a.change_source}</span>
                ) : null}
                <div className="queue-sub">
                  {display(a.old_value).slice(0, 80)} → {display(a.new_value).slice(0, 80)}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

export default function ClientSetup({ clientId }: { clientId?: number | null }) {
  if (clientId != null && clientId > 0) {
    return <ClientSetupDetail clientId={clientId} />
  }
  return <ClientsHub />
}
