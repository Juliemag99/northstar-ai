import { useState, type FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { useAuth } from './auth/useAuth'
import {
  FEEDBACK_CATEGORIES,
  FEEDBACK_IMPACTS,
  submitFeedback,
} from './api/feedback'

type FeedbackButtonProps = {
  clientId: number | null
  companyId: number | null
  contactId: number | null
}

export default function FeedbackButton({
  clientId,
  companyId,
  contactId,
}: FeedbackButtonProps) {
  const { authenticated } = useAuth()
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [category, setCategory] = useState<string>(FEEDBACK_CATEGORIES[0])
  const [impact, setImpact] = useState<string>(FEEDBACK_IMPACTS[1])
  const [comment, setComment] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  if (!authenticated) return null

  function resetForm() {
    setCategory(FEEDBACK_CATEGORIES[0])
    setImpact(FEEDBACK_IMPACTS[1])
    setComment('')
    setError(null)
  }

  function close() {
    setOpen(false)
    setNotice(null)
    setError(null)
    resetForm()
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (saving) return
    setSaving(true)
    setError(null)
    setNotice(null)
    const route = `${location.pathname}${location.search}`.slice(0, 300)
    const company = companyId != null && companyId > 0 ? companyId : null
    const contact = company != null && contactId != null && contactId > 0 ? contactId : null
    try {
      await submitFeedback({
        category,
        impact,
        body: comment,
        page_route: route,
        client_id: clientId != null && clientId > 0 ? clientId : null,
        company_id: company,
        contact_id: contact,
      })
      resetForm()
      setNotice('Feedback sent.')
      setOpen(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send feedback.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <button type="button" className="ghost-btn" onClick={() => setOpen(true)}>
        Feedback
      </button>
      {notice && !open ? (
        <span className="save-confirm" role="status">
          {notice}
        </span>
      ) : null}
      {open ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="staff-feedback-title"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(15, 23, 42, 0.45)',
            zIndex: 250,
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'center',
            padding: '2rem 1rem',
          }}
        >
          <form className="panel" style={{ maxWidth: '28rem', width: '100%', margin: 0 }} onSubmit={(event) => void onSubmit(event)}>
            <div className="panel-header">
              <h2 id="staff-feedback-title">Feedback</h2>
              <button type="button" className="link-btn" onClick={close} disabled={saving}>
                Close
              </button>
            </div>
            <label className="edit-field">
              <span className="edit-field__label">Type</span>
              <select className="edit-select" value={category} onChange={(event) => setCategory(event.target.value)}>
                {FEEDBACK_CATEGORIES.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <label className="edit-field">
              <span className="edit-field__label">Impact</span>
              <select className="edit-select" value={impact} onChange={(event) => setImpact(event.target.value)}>
                {FEEDBACK_IMPACTS.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <label className="edit-field">
              <span className="edit-field__label">Comment</span>
              <textarea
                className="edit-textarea"
                rows={5}
                maxLength={4000}
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                required
              />
            </label>
            {error ? (
              <p className="data-status data-status--error" role="alert">
                {error}
              </p>
            ) : null}
            <div className="edit-actions">
              <button type="submit" className="primary-btn" disabled={saving || !comment.trim()}>
                {saving ? 'Sending…' : 'Send feedback'}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </>
  )
}
