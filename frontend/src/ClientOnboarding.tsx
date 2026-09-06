import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import {
  applyOnboardingCopy,
  applyOnboardingTemplate,
  createOnboardingDraft,
  fetchOnboardingDraft,
  fetchOnboardingTemplates,
  finishOnboardingDraft,
  previewOnboardingCopy,
  saveOnboardingDraft,
  type CopyPreview,
  type OnboardingDraft,
  type OnboardingTemplateListItem,
} from './api/clientOnboarding'
import { fetchClientSetupList } from './api/carmeco'
import {
  ONBOARDING_STEPS,
  draftIsDirty,
  statusesFromText,
  summarizeMissing,
  tierForPath,
  validateStepBasics,
  validateStepCrm,
} from './clientOnboardingHelpers'
import { useAuth } from './auth/useAuth'
import { staffCanAdminister } from './auth/staffCanAdminister'

function Field({
  label,
  path,
  tiers,
  value,
  onChange,
  multiline,
  hint,
}: {
  label: string
  path: string
  tiers?: Record<string, string>
  value: string
  onChange: (v: string) => void
  multiline?: boolean
  hint?: string
}) {
  const tier = tierForPath(path, tiers)
  const id = `onboard-${path.replace(/\./g, '-')}`
  return (
    <label className="setup-field" htmlFor={id}>
      <span className="setup-field-label">
        {label}{' '}
        <span className="queue-sub">
          ({tier === 'required' ? 'required' : tier === 'recommended' ? 'recommended' : 'optional'})
        </span>
      </span>
      {multiline ? (
        <textarea
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={3}
        />
      ) : (
        <input id={id} value={value} onChange={(e) => onChange(e.target.value)} />
      )}
      {hint ? <span className="queue-sub">{hint}</span> : null}
    </label>
  )
}

export default function ClientOnboarding() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const { user, authenticated } = useAuth()
  const isAdmin = staffCanAdminister(Boolean(authenticated), user)

  const existingClientId = Number(params.get('client_id') || 0) || null
  const draftParam = Number(params.get('draft_id') || 0) || null

  const [draft, setDraft] = useState<OnboardingDraft | null>(null)
  const [templates, setTemplates] = useState<OnboardingTemplateListItem[]>([])
  const [tiers, setTiers] = useState<Record<string, string>>({})
  const [clients, setClients] = useState<Array<{ client_id: number; client_name: string }>>(
    [],
  )
  const [step, setStep] = useState(1)
  const [error, setError] = useState<string | null>(null)
  const [errors, setErrors] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [savedSnapshot, setSavedSnapshot] = useState('')
  const [savedStep, setSavedStep] = useState(1)
  const [templateId, setTemplateId] = useState('')
  const [copySourceId, setCopySourceId] = useState('')
  const [copyPreview, setCopyPreview] = useState<CopyPreview | null>(null)
  const [overwritePaths, setOverwritePaths] = useState<string[]>([])
  const [statusText, setStatusText] = useState('')

  const dirty = useMemo(() => {
    if (!draft) return false
    return draftIsDirty(savedSnapshot, draft.payload, step, savedStep)
  }, [draft, savedSnapshot, step, savedStep])

  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (!dirty) return
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  useEffect(() => {
    if (!isAdmin) return
    let cancelled = false
    ;(async () => {
      try {
        const tmpl = await fetchOnboardingTemplates()
        if (cancelled) return
        setTemplates(tmpl.templates || [])
        setTiers(tmpl.field_tiers || {})
        const list = await fetchClientSetupList()
        if (cancelled) return
        setClients(
          list.map((c) => ({
            client_id: c.client_id,
            client_name: c.client_name,
          })),
        )
        let loaded: OnboardingDraft
        if (draftParam) {
          loaded = await fetchOnboardingDraft(draftParam)
        } else {
          loaded = await createOnboardingDraft({
            client_id: existingClientId,
          })
        }
        if (cancelled) return
        applyLoaded(loaded)
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to start onboarding.')
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [isAdmin, draftParam, existingClientId])

  function applyLoaded(loaded: OnboardingDraft) {
    setDraft(loaded)
    setStep(loaded.current_step || 1)
    setSavedStep(loaded.current_step || 1)
    setSavedSnapshot(JSON.stringify(loaded.payload))
    setStatusText((loaded.payload.crm.statuses || []).join('\n'))
    setTemplateId(loaded.template_id || '')
    setMessage(null)
    setError(null)
  }

  function patchSection(
    section: 'basics' | 'sells' | 'opportunities' | 'crm',
    key: string,
    value: unknown,
  ) {
    setDraft((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        payload: {
          ...prev.payload,
          [section]: {
            ...prev.payload[section],
            [key]: value,
          },
        },
      }
    })
  }

  async function persist(nextStep = step): Promise<OnboardingDraft | null> {
    if (!draft) return null
    setSaving(true)
    setError(null)
    try {
      const statuses = statusesFromText(statusText)
      const payload = {
        ...draft.payload,
        crm: {
          ...draft.payload.crm,
          statuses,
          default_status:
            draft.payload.crm.default_status || (statuses[0] || ''),
        },
      }
      const saved = await saveOnboardingDraft(draft.draft_id, {
        current_step: nextStep,
        payload,
      })
      applyLoaded(saved)
      setMessage('Draft saved.')
      return saved
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed.')
      return null
    } finally {
      setSaving(false)
    }
  }

  async function goNext() {
    if (!draft) return
    const stepErrors: string[] = []
    if (step === 1) stepErrors.push(...validateStepBasics(draft.payload.basics))
    if (step === 4) {
      stepErrors.push(
        ...validateStepCrm({
          ...draft.payload.crm,
          statuses: statusesFromText(statusText),
        }),
      )
    }
    setErrors(stepErrors)
    if (stepErrors.length) return
    const saved = await persist(Math.min(5, step + 1))
    if (saved) setStep(Math.min(5, step + 1))
  }

  async function goBack() {
    const next = Math.max(1, step - 1)
    const saved = await persist(next)
    if (saved) setStep(next)
  }

  async function onApplyTemplate() {
    if (!draft || !templateId) return
    setSaving(true)
    try {
      const updated = await applyOnboardingTemplate(draft.draft_id, templateId, false)
      applyLoaded(updated)
      setMessage(`Applied template “${templateId}” (blank destination fields only).`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Template apply failed.')
    } finally {
      setSaving(false)
    }
  }

  async function onPreviewCopy() {
    if (!draft || !copySourceId) return
    setSaving(true)
    try {
      const preview = await previewOnboardingCopy({
        source_client_id: Number(copySourceId),
        draft_id: draft.draft_id,
      })
      setCopyPreview(preview)
      setOverwritePaths([])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Copy preview failed.')
    } finally {
      setSaving(false)
    }
  }

  async function onApplyCopy() {
    if (!draft || !copySourceId) return
    setSaving(true)
    try {
      const updated = await applyOnboardingCopy(
        draft.draft_id,
        Number(copySourceId),
        overwritePaths,
      )
      applyLoaded(updated)
      setCopyPreview(null)
      setMessage('Copied selected capability/status/campaign fields.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Copy apply failed.')
    } finally {
      setSaving(false)
    }
  }

  async function onFinish() {
    if (!draft) return
    if (dirty) {
      const saved = await persist(5)
      if (!saved) return
    }
    if (!draft.can_finish && (await persist(5))?.can_finish === false) {
      setErrors(
        summarizeMissing(
          (await fetchOnboardingDraft(draft.draft_id)).missing_required,
        ).map((m) => `Missing required: ${m}`),
      )
      return
    }
    setSaving(true)
    try {
      const result = await finishOnboardingDraft(draft.draft_id)
      setMessage(
        result.created_new_client
          ? `Created ${result.client_name}. Setup saved.`
          : `Updated ${result.client_name}. Setup saved.`,
      )
      navigate(result.setup_href)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Finish failed.')
    } finally {
      setSaving(false)
    }
  }

  function leave() {
    if (dirty && !window.confirm('You have unsaved changes. Leave without saving?')) {
      return
    }
    navigate('/clients')
  }

  if (!isAdmin) {
    return (
      <div className="client-setup-page">
        <p className="data-status data-status--error" role="alert">
          Client onboarding is administrator-only.
        </p>
        <Link className="link-btn" to="/clients">
          Back to Clients
        </Link>
      </div>
    )
  }

  if (!draft) {
    return (
      <div className="client-setup-page">
        <p className="data-status">{error || 'Loading onboarding…'}</p>
      </div>
    )
  }

  const p = draft.payload

  return (
    <div className="client-setup-page">
      <div className="page-heading page-heading--split">
        <div>
          <button type="button" className="link-btn back-link" onClick={leave}>
            ← Back to Clients
          </button>
          <h1>
            {draft.mode === 'existing' ? 'Complete Client Setup' : 'Add Client'}
          </h1>
          <p>
            Guided setup for NorthStar client capabilities and target criteria. Do not invent
            capabilities — leave unknowns blank.
          </p>
        </div>
        <div className="heading-controls">
          <div aria-live="polite">
            Setup {draft.completion_percent}% complete
            {dirty ? ' · unsaved changes' : ''}
          </div>
          <button type="button" className="link-btn" disabled={saving} onClick={() => void persist()}>
            Save draft
          </button>
        </div>
      </div>

      <nav className="setup-campaign-tabs" aria-label="Onboarding steps">
        {ONBOARDING_STEPS.map((s) => (
          <button
            key={s.id}
            type="button"
            className={
              step === s.id ? 'setup-campaign-tab setup-campaign-tab--active' : 'setup-campaign-tab'
            }
            onClick={() => {
              void persist(s.id).then((saved) => {
                if (saved) setStep(s.id)
              })
            }}
          >
            {s.id}. {s.title}
          </button>
        ))}
      </nav>

      {error ? (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      ) : null}
      {message ? <p className="data-status">{message}</p> : null}
      {errors.length > 0 ? (
        <div className="panel" role="alert" aria-label="Validation errors">
          <ul className="setup-missing-list">
            {errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <section className="panel">
        <div className="panel-header">
          <h2>
            STEP {step}: {ONBOARDING_STEPS[step - 1]?.title.toUpperCase()}
          </h2>
        </div>

        {step === 1 ? (
          <div className="setup-grid">
            <Field
              label="Client name"
              path="basics.client_name"
              tiers={tiers}
              value={p.basics.client_name || ''}
              onChange={(v) => patchSection('basics', 'client_name', v)}
            />
            <Field
              label="Website"
              path="basics.website"
              tiers={tiers}
              value={p.basics.website || ''}
              onChange={(v) => patchSection('basics', 'website', v)}
            />
            <Field
              label="Headquarters / location"
              path="basics.main_location"
              tiers={tiers}
              value={p.basics.main_location || ''}
              onChange={(v) => patchSection('basics', 'main_location', v)}
            />
            <Field
              label="Description"
              path="basics.description"
              tiers={tiers}
              value={p.basics.description || ''}
              onChange={(v) => patchSection('basics', 'description', v)}
              multiline
            />
            <div className="setup-field">
              <span className="setup-field-label">Start from a template (optional)</span>
              <select
                value={templateId}
                onChange={(e) => setTemplateId(e.target.value)}
                aria-label="Template"
              >
                <option value="">Choose a template…</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="link-btn"
                disabled={!templateId || saving}
                onClick={() => void onApplyTemplate()}
              >
                Apply template to blank fields
              </button>
            </div>
            <div className="setup-field">
              <span className="setup-field-label">Copy from a similar client (optional)</span>
              <select
                value={copySourceId}
                onChange={(e) => setCopySourceId(e.target.value)}
                aria-label="Copy source client"
              >
                <option value="">Choose a client…</option>
                {clients.map((c) => (
                  <option key={c.client_id} value={c.client_id}>
                    {c.client_name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="link-btn"
                disabled={!copySourceId || saving}
                onClick={() => void onPreviewCopy()}
              >
                Preview what will be copied
              </button>
            </div>
            {copyPreview ? (
              <div className="ask-section" aria-label="Copy preview">
                <h3>
                  Copy preview from {copyPreview.source_client_name} (capabilities / statuses /
                  campaign criteria only)
                </h3>
                <p className="queue-sub">
                  Never copied: {(copyPreview.never_copied || []).join(', ')}
                </p>
                <ul className="ask-history-list">
                  {copyPreview.fields
                    .filter((f) => f.will_copy || f.blocked_reason.includes('overwrite'))
                    .slice(0, 40)
                    .map((f) => (
                      <li key={f.path}>
                        <strong>{f.path}</strong>
                        <div className="queue-sub">
                          {f.will_copy
                            ? 'Will copy'
                            : f.blocked_reason || 'Skipped'}
                        </div>
                        {!f.will_copy && f.blocked_reason.includes('overwrite') ? (
                          <label>
                            <input
                              type="checkbox"
                              checked={overwritePaths.includes(f.path)}
                              onChange={(e) => {
                                setOverwritePaths((prev) =>
                                  e.target.checked
                                    ? [...prev, f.path]
                                    : prev.filter((x) => x !== f.path),
                                )
                              }}
                            />{' '}
                            Overwrite populated destination field
                          </label>
                        ) : null}
                      </li>
                    ))}
                </ul>
                <button
                  type="button"
                  className="primary-btn"
                  disabled={saving}
                  onClick={() => void onApplyCopy()}
                >
                  Apply copy
                </button>
              </div>
            ) : null}
          </div>
        ) : null}

        {step === 2 ? (
          <div className="setup-grid">
            <Field
              label="Primary services"
              path="sells.primary_service"
              tiers={tiers}
              value={p.sells.primary_service || ''}
              onChange={(v) => patchSection('sells', 'primary_service', v)}
            />
            <Field
              label="Products"
              path="sells.products_services"
              tiers={tiers}
              value={p.sells.products_services || ''}
              onChange={(v) => patchSection('sells', 'products_services', v)}
              multiline
            />
            <Field
              label="Industries served"
              path="sells.industries_served"
              tiers={tiers}
              value={p.sells.industries_served || ''}
              onChange={(v) => patchSection('sells', 'industries_served', v)}
              multiline
            />
            <Field
              label="Capabilities / processes"
              path="sells.secondary_services"
              tiers={tiers}
              value={p.sells.secondary_services || ''}
              onChange={(v) => patchSection('sells', 'secondary_services', v)}
              multiline
            />
            <Field
              label="Equipment"
              path="sells.equipment_capacity"
              tiers={tiers}
              value={p.sells.equipment_capacity || ''}
              onChange={(v) => patchSection('sells', 'equipment_capacity', v)}
              multiline
            />
            <Field
              label="Materials"
              path="sells.materials"
              tiers={tiers}
              value={p.sells.materials || ''}
              onChange={(v) => patchSection('sells', 'materials', v)}
              multiline
            />
            <Field
              label="Certifications"
              path="sells.certifications"
              tiers={tiers}
              value={p.sells.certifications || ''}
              onChange={(v) => patchSection('sells', 'certifications', v)}
            />
          </div>
        ) : null}

        {step === 3 ? (
          <div className="setup-grid">
            <Field
              label="Ideal customer types"
              path="opportunities.target_customer_types"
              tiers={tiers}
              value={p.opportunities.target_customer_types || ''}
              onChange={(v) => patchSection('opportunities', 'target_customer_types', v)}
              multiline
            />
            <Field
              label="Desired work / target products"
              path="opportunities.target_products"
              tiers={tiers}
              value={p.opportunities.target_products || ''}
              onChange={(v) => patchSection('opportunities', 'target_products', v)}
              multiline
            />
            <Field
              label="Preferred part / project characteristics"
              path="opportunities.manufacturing_processes_sought"
              tiers={tiers}
              value={p.opportunities.manufacturing_processes_sought || ''}
              onChange={(v) =>
                patchSection('opportunities', 'manufacturing_processes_sought', v)
              }
              multiline
            />
            <Field
              label="Production-volume preferences"
              path="opportunities.production_preference"
              tiers={tiers}
              value={p.opportunities.production_preference || ''}
              onChange={(v) => patchSection('opportunities', 'production_preference', v)}
              multiline
            />
            <Field
              label="Geography"
              path="opportunities.geographic_preferences"
              tiers={tiers}
              value={p.opportunities.geographic_preferences || ''}
              onChange={(v) => patchSection('opportunities', 'geographic_preferences', v)}
              multiline
            />
            <Field
              label="Positive fit signals"
              path="opportunities.positive_signals"
              tiers={tiers}
              value={p.opportunities.positive_signals || ''}
              onChange={(v) => patchSection('opportunities', 'positive_signals', v)}
              multiline
            />
            <Field
              label="Exclusions / negative fit signals"
              path="opportunities.negative_signals"
              tiers={tiers}
              value={p.opportunities.negative_signals || ''}
              onChange={(v) => patchSection('opportunities', 'negative_signals', v)}
              multiline
            />
            <Field
              label="Additional exclusions"
              path="opportunities.exclusions"
              tiers={tiers}
              value={p.opportunities.exclusions || ''}
              onChange={(v) => patchSection('opportunities', 'exclusions', v)}
              multiline
            />
          </div>
        ) : null}

        {step === 4 ? (
          <div className="setup-grid">
            <label className="setup-field" htmlFor="onboard-statuses">
              <span className="setup-field-label">
                Relationship status catalog{' '}
                <span className="queue-sub">(required)</span>
              </span>
              <textarea
                id="onboard-statuses"
                rows={5}
                value={statusText}
                onChange={(e) => setStatusText(e.target.value)}
                placeholder={'Prospect\nWorking\nCustomer\nNot a Fit'}
              />
              <span className="queue-sub">One status per line (or comma-separated).</span>
            </label>
            <label className="setup-field" htmlFor="onboard-default-status">
              <span className="setup-field-label">Default status (required)</span>
              <select
                id="onboard-default-status"
                value={p.crm.default_status || ''}
                onChange={(e) => patchSection('crm', 'default_status', e.target.value)}
              >
                <option value="">Select…</option>
                {statusesFromText(statusText).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <Field
              label="Campaign name"
              path="crm.campaign_name"
              tiers={tiers}
              value={p.crm.campaign_name || ''}
              onChange={(v) => patchSection('crm', 'campaign_name', v)}
            />
            <Field
              label="Campaign description"
              path="crm.campaign_description"
              tiers={tiers}
              value={p.crm.campaign_description || ''}
              onChange={(v) => patchSection('crm', 'campaign_description', v)}
              multiline
            />
            <label className="setup-field">
              <span className="setup-field-label">
                Assignment defaults{' '}
                <span className="queue-sub">(optional — off unless explicitly selected)</span>
              </span>
              <label>
                <input
                  type="checkbox"
                  checked={Boolean(p.crm.apply_assignments)}
                  onChange={(e) => patchSection('crm', 'apply_assignments', e.target.checked)}
                />{' '}
                Apply staff assignments on finish (only when checked)
              </label>
              <p className="queue-sub">
                Leave unchecked to avoid creating user assignments. Assign people later in
                administration if needed.
              </p>
            </label>
          </div>
        ) : null}

        {step === 5 ? (
          <div className="ask-section">
            <h3>Summary</h3>
            <dl className="ask-fact-grid">
              <div>
                <dt>Client</dt>
                <dd>{p.basics.client_name || '—'}</dd>
              </div>
              <div>
                <dt>Primary service</dt>
                <dd>{p.sells.primary_service || '—'}</dd>
              </div>
              <div>
                <dt>Campaign</dt>
                <dd>{p.crm.campaign_name || '—'}</dd>
              </div>
              <div>
                <dt>Completion</dt>
                <dd>{draft.completion_percent}%</dd>
              </div>
            </dl>
            {draft.missing_required.length > 0 ? (
              <>
                <h3>Missing required</h3>
                <ul className="setup-missing-list">
                  {summarizeMissing(draft.missing_required).map((m) => (
                    <li key={m}>{m}</li>
                  ))}
                </ul>
              </>
            ) : (
              <p>Required fields are complete. Optional gaps can be filled later in Client Setup.</p>
            )}
            {draft.missing_recommended.length > 0 ? (
              <>
                <h3>Recommended (optional to finish)</h3>
                <ul className="setup-missing-list">
                  {summarizeMissing(draft.missing_recommended).slice(0, 12).map((m) => (
                    <li key={m}>{m}</li>
                  ))}
                </ul>
              </>
            ) : null}
            <p className="queue-sub">
              Saving will update only filled onboarding fields. Blank draft fields will not erase
              existing client data. You can return and edit later from Clients → Complete Setup.
            </p>
          </div>
        ) : null}

        <div className="heading-controls" style={{ marginTop: '1rem' }}>
          <button type="button" className="link-btn" disabled={step <= 1 || saving} onClick={() => void goBack()}>
            Back
          </button>
          {step < 5 ? (
            <button type="button" className="primary-btn" disabled={saving} onClick={() => void goNext()}>
              Next
            </button>
          ) : (
            <button
              type="button"
              className="primary-btn"
              disabled={saving || !draft.can_finish}
              onClick={() => void onFinish()}
            >
              Save and finish
            </button>
          )}
        </div>
      </section>
    </div>
  )
}
