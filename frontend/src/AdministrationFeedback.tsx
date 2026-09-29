import { useCallback, useEffect, useState } from 'react'
import {
  FEEDBACK_STATUSES,
  listFeedback,
  updateFeedbackStatus,
  type FeedbackItem,
} from './api/feedback'

function display(value: string | null | undefined): string {
  const text = (value || '').trim()
  return text || '—'
}

export default function AdministrationFeedback() {
  const [items, setItems] = useState<FeedbackItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const page = await listFeedback(50, 0)
      setItems(page.items)
      setTotal(page.total)
    } catch (err) {
      setItems([])
      setError(err instanceof Error ? err.message : 'Could not load feedback.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function onStatus(item: FeedbackItem, status: string) {
    if (status === item.status) return
    setBusyId(item.id)
    setError(null)
    try {
      const updated = await updateFeedbackStatus(item.id, status)
      setItems((current) => current.map((row) => (row.id === updated.id ? updated : row)))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update feedback status.')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="administration-section" aria-labelledby="staff-feedback-admin-heading">
      <div className="panel-header">
        <h2 id="staff-feedback-admin-heading">Feedback</h2>
        <span className="queue-source">{total} submitted</span>
      </div>
      {loading ? <p className="queue-note">Loading feedback…</p> : null}
      {error ? (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      ) : null}
      {!loading && items.length === 0 ? <p className="empty-state">No feedback yet.</p> : null}
      {items.length > 0 ? (
        <div className="queue-table-wrap">
          <table className="queue-table">
            <thead>
              <tr>
                <th>Date/time</th>
                <th>User</th>
                <th>Client</th>
                <th>Type</th>
                <th>Impact</th>
                <th>Comment</th>
                <th>Route / context</th>
                <th>Company / contact</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>{display(item.created_at)}</td>
                  <td>{display(item.user_name || item.user_email)}</td>
                  <td>{display(item.client_name)}</td>
                  <td>{display(item.category)}</td>
                  <td>{display(item.impact)}</td>
                  <td>{display(item.body)}</td>
                  <td>{display(item.page_route)}</td>
                  <td>
                    {display(item.company_name)}
                    {item.contact_name ? ` · ${item.contact_name}` : ''}
                  </td>
                  <td>
                    <select
                      className="edit-select"
                      aria-label={`Status for feedback ${item.id}`}
                      value={item.status}
                      disabled={busyId === item.id}
                      onChange={(event) => void onStatus(item, event.target.value)}
                    >
                      {FEEDBACK_STATUSES.map((status) => (
                        <option key={status} value={status}>
                          {status}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  )
}
