import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  confirmCampaignRoute,
  deferCampaignRoute,
  fetchCampaignRoute,
} from './api/carmeco'
import type { CampaignRouteSuggestion } from './types/carmeco'

export type CampaignRouteOffer = {
  clientId: number
  companyId: number
  contactId?: number | null
  researchRunId?: number | null
  source: string
  mode?: 'auto' | 'manual'
}

export default function CampaignRoutePrompt({
  offer,
  onClose,
}: {
  offer: CampaignRouteOffer | null
  onClose: (result?: CampaignRouteSuggestion) => void
}) {
  const [data, setData] = useState<CampaignRouteSuggestion | null>(null)
  const [campaignId, setCampaignId] = useState<number>(0)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!offer || offer.clientId <= 0 || offer.companyId <= 0) {
      setData(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    setNotice(null)
    void fetchCampaignRoute({
      client_id: offer.clientId,
      company_id: offer.companyId,
      contact_id: offer.contactId,
      research_run_id: offer.researchRunId,
      source: offer.source,
      force: offer.mode === 'manual',
    })
      .then((next) => {
        if (cancelled) return
        setData(next)
        const recommended =
          next.recommended_campaign_id ||
          next.campaigns.find((c) => c.recommended)?.campaign_id ||
          next.campaigns[0]?.campaign_id ||
          0
        setCampaignId(recommended)
        if (offer.mode !== 'manual' && !next.should_prompt) {
          if (next.auto_unassigned) {
            setNotice(next.message || 'Added to Unassigned Opportunities.')
            return
          }
          onClose(next)
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load campaigns.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // Parent typically passes a new onClose each render; offer identity is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offer])

  if (!offer) return null

  const showForm = Boolean(data?.should_prompt || (offer.mode === 'manual' && data?.campaigns.length))

  async function confirm() {
    if (!offer || !campaignId) {
      setError('Select a campaign.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const next = await confirmCampaignRoute({
        client_id: offer.clientId,
        company_id: offer.companyId,
        contact_id: offer.contactId,
        campaign_id: campaignId,
        source: offer.source,
      })
      onClose(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add to campaign.')
    } finally {
      setSaving(false)
    }
  }

  async function notNow() {
    if (!offer) return
    setSaving(true)
    setError(null)
    try {
      const next = await deferCampaignRoute({
        client_id: offer.clientId,
        company_id: offer.companyId,
        contact_id: offer.contactId,
        source: offer.source,
      })
      onClose(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save as unassigned.')
    } finally {
      setSaving(false)
    }
  }

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="campaign-route-title"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(15, 23, 42, 0.45)',
        zIndex: 240,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        padding: '2rem 1rem',
        overflow: 'auto',
      }}
    >
      <section className="panel" style={{ maxWidth: '28rem', width: '100%', margin: 0 }}>
        <div className="panel-header">
          <h2 id="campaign-route-title">
            {offer.mode === 'manual' ? 'Add to Campaign' : 'Add this opportunity to a campaign?'}
          </h2>
          <button type="button" className="link-btn" onClick={() => {
            if (offer.mode !== 'manual' && data?.should_prompt) {
              void notNow()
              return
            }
            onClose()
          }} disabled={saving}>
            Close
          </button>
        </div>
        {loading && <p className="queue-note">Checking campaigns…</p>}
        {error && <p className="queue-error">{error}</p>}
        {notice && !showForm && (
          <>
            <p className="queue-note">{notice}</p>
            <div className="heading-controls" style={{ marginTop: '0.75rem' }}>
              <button type="button" className="primary-btn" onClick={() => onClose(data || undefined)}>
                OK
              </button>
            </div>
          </>
        )}
        {showForm && data && (
          <>
            <p>
              {data.company_name ? <strong>{data.company_name}</strong> : 'This opportunity'} will be
              added to a campaign for the Active Client. Shared company and contact records are not
              duplicated.
            </p>
            {data.assigned_campaign_names.length > 0 && (
              <p className="queue-note">
                Currently on: {data.assigned_campaign_names.join(', ')}
              </p>
            )}
            <label className="edit-field" style={{ marginTop: '0.75rem' }}>
              <span className="edit-field__label">Campaign</span>
              <select
                className="edit-select"
                value={campaignId || ''}
                onChange={(e) => setCampaignId(Number(e.target.value))}
              >
                {data.campaigns.map((item) => (
                  <option key={item.campaign_id} value={item.campaign_id}>
                    {item.campaign_name}
                    {item.recommended ? ' (recommended)' : ''}
                    {item.status !== 'Active' ? ` · ${item.status}` : ''}
                  </option>
                ))}
              </select>
            </label>
            <div className="heading-controls" style={{ marginTop: '1rem' }}>
              <button
                type="button"
                className="primary-btn"
                onClick={() => void confirm()}
                disabled={saving || !campaignId}
              >
                {saving ? 'Saving…' : 'Confirm and add'}
              </button>
              {offer.mode !== 'manual' ? (
                <button type="button" className="ghost-btn" onClick={() => void notNow()} disabled={saving}>
                  Not now
                </button>
              ) : (
                <button type="button" className="ghost-btn" onClick={() => onClose()} disabled={saving}>
                  Cancel
                </button>
              )}
            </div>
          </>
        )}
      </section>
    </div>,
    document.body,
  )
}
