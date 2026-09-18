import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import {
  connectEmailAccount,
  disconnectEmailAccount,
  fetchGoogleOAuthStatus,
  listEmailAccounts,
  type ClientEmailAccount,
} from './api/carmeco'
import {
  emailConnectionLabel,
  emailConnectionUiStatus,
  type EmailConnectionUiStatus,
} from './emailConnectionStatus'
import { useAuth } from './auth/useAuth'
import { staffCanAdminister } from './auth/staffCanAdminister'
import AdministrationImport from './AdministrationImport'
import AdministrationClientDataImport from './AdministrationClientDataImport'
import AdministrationMasterDataExport from './AdministrationMasterDataExport'
import AdministrationLeadmasterRefresh from './AdministrationLeadmasterRefresh'
import AdministrationDataSteward from './AdministrationDataSteward'

type AssignedClient = {
  client_id: number
  client_name: string
  client_code?: string
}

type AdministrationProps = {
  activeClientId: number | null
  availableClients: AssignedClient[]
}

function clientNameFor(
  clientId: number,
  availableClients: AssignedClient[],
): string {
  return (
    availableClients.find((c) => c.client_id === clientId)?.client_name ||
    `Client ${clientId}`
  )
}

function oauthReturnMessage(
  flag: string,
  detail: string,
): { kind: 'success' | 'error'; text: string } | null {
  if (flag === 'connected') {
    return { kind: 'success', text: 'Gmail connected successfully. Connection status refreshed.' }
  }
  if (flag === 'mismatch') {
    return {
      kind: 'error',
      text: 'Google account did not match the configured sender address. Reconnect using the correct Gmail identity.',
    }
  }
  if (flag === 'error') {
    const extra = detail.trim()
    return {
      kind: 'error',
      text: extra
        ? `Gmail connection failed. ${extra}`
        : 'Gmail connection failed. Try Connect Gmail again.',
    }
  }
  return null
}

export default function Administration({
  activeClientId,
  availableClients,
}: AdministrationProps) {
  const { authenticated, user } = useAuth()
  const canAdminister = staffCanAdminister(authenticated, user)
  const [searchParams, setSearchParams] = useSearchParams()
  const [scopedClientId, setScopedClientId] = useState<number>(
    activeClientId != null && activeClientId > 0 ? activeClientId : 0,
  )
  const [accounts, setAccounts] = useState<ClientEmailAccount[]>([])
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [banner, setBanner] = useState<{ kind: 'success' | 'error'; text: string } | null>(
    null,
  )
  const loadGen = useRef(0)
  const [adminTab, setAdminTab] = useState<
    'email' | 'import' | 'client-data-import' | 'data-management'
  >('email')
  const [dataMgmtTab, setDataMgmtTab] = useState<'export' | 'leadmaster-refresh' | 'master-data'>('export')

  const allMyClients = activeClientId == null || activeClientId <= 0
  const connectClientId = allMyClients ? scopedClientId : activeClientId
  const canConnect = connectClientId != null && connectClientId > 0

  useEffect(() => {
    if (activeClientId != null && activeClientId > 0) {
      setScopedClientId(activeClientId)
    }
  }, [activeClientId])

  const loadAccounts = useCallback(async (clientId: number) => {
    const gen = ++loadGen.current
    if (clientId <= 0) {
      setAccounts([])
      return
    }
    setLoading(true)
    setError(null)
    try {
      const rows = await listEmailAccounts(clientId)
      if (gen !== loadGen.current) return
      setAccounts(rows.filter((row) => Number(row.client_id) === Number(clientId)))
    } catch (err) {
      if (gen !== loadGen.current) return
      setAccounts([])
      setError(err instanceof Error ? err.message : 'Could not load email connections.')
    } finally {
      if (gen === loadGen.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!canAdminister) return
    const flag = (searchParams.get('email_oauth') || '').trim().toLowerCase()
    if (!flag) return
    const detail = searchParams.get('detail') || ''
    const returnedClient = Number(searchParams.get('client_id') || 0)
    const message = oauthReturnMessage(flag, detail)
    if (message) setBanner(message)
    if (returnedClient > 0) setScopedClientId(returnedClient)
    const next = new URLSearchParams(searchParams)
    next.delete('email_oauth')
    next.delete('detail')
    next.delete('account_id')
    next.delete('client_id')
    setSearchParams(next, { replace: true })
  }, [canAdminister, searchParams, setSearchParams])

  useEffect(() => {
    if (!canAdminister) {
      setAccounts([])
      return
    }
    if (!canConnect || connectClientId == null) {
      setAccounts([])
      return
    }
    void loadAccounts(connectClientId)
  }, [canAdminister, canConnect, connectClientId, loadAccounts, banner])

  const scopedClientName = useMemo(() => {
    if (!canConnect || connectClientId == null) return ''
    return clientNameFor(connectClientId, availableClients)
  }, [availableClients, canConnect, connectClientId])

  async function startGoogleOAuth(account: ClientEmailAccount) {
    if (!canConnect) {
      setError('Choose a specific client before connecting Gmail.')
      return
    }
    if (account.client_id !== connectClientId) {
      setError('This sender belongs to a different client. Choose that client first.')
      return
    }
    setBusyId(account.account_id)
    setError(null)
    try {
      const status = await fetchGoogleOAuthStatus()
      if (!status.configured) {
        setError(status.message || 'Google OAuth is not configured.')
        return
      }
      const started = await connectEmailAccount(account.client_id, account.account_id)
      const url = (started.authorization_url || '').trim()
      if (!url) {
        setError(started.message || 'Could not start Google OAuth.')
        return
      }
      window.location.assign(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start Google OAuth.')
    } finally {
      setBusyId(null)
    }
  }

  async function onDisconnect(account: ClientEmailAccount) {
    if (account.client_id !== connectClientId) {
      setError('This sender belongs to a different client.')
      return
    }
    const ok = window.confirm(
      `Disconnect Gmail for ${account.email_address} (${clientNameFor(account.client_id, availableClients)})?`,
    )
    if (!ok) return
    setBusyId(account.account_id)
    setError(null)
    try {
      const result = await disconnectEmailAccount(account.client_id, account.account_id)
      setBanner({
        kind: 'success',
        text: result.message || `Disconnected ${account.email_address}.`,
      })
      await loadAccounts(account.client_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Disconnect failed.')
    } finally {
      setBusyId(null)
    }
  }

  function actionFor(status: EmailConnectionUiStatus): 'connect' | 'reconnect' | 'disconnect' {
    if (status === 'connected') return 'disconnect'
    if (status === 'expired' || status === 'error') return 'reconnect'
    return 'connect'
  }

  if (!canAdminister) {
    return <Navigate to="/" replace />
  }

  return (
    <div className="administration-page">
      <div className="page-heading">
        <h1>Administration</h1>
        <p>Manage NorthStar email connections. Gmail OAuth does not store passwords.</p>
      </div>

      <div className="setup-campaign-tabs" role="tablist" aria-label="Administration sections">
        <button
          type="button"
          role="tab"
          aria-selected={adminTab === 'email'}
          className={
            adminTab === 'email'
              ? 'setup-campaign-tab setup-campaign-tab--active'
              : 'setup-campaign-tab'
          }
          onClick={() => setAdminTab('email')}
        >
          Email Connections
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={adminTab === 'import'}
          className={
            adminTab === 'import'
              ? 'setup-campaign-tab setup-campaign-tab--active'
              : 'setup-campaign-tab'
          }
          onClick={() => setAdminTab('import')}
        >
          Company & contact import
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={adminTab === 'client-data-import'}
          className={
            adminTab === 'client-data-import'
              ? 'setup-campaign-tab setup-campaign-tab--active'
              : 'setup-campaign-tab'
          }
          onClick={() => setAdminTab('client-data-import')}
        >
          Client Data Import
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={adminTab === 'data-management'}
          className={
            adminTab === 'data-management'
              ? 'setup-campaign-tab setup-campaign-tab--active'
              : 'setup-campaign-tab'
          }
          onClick={() => setAdminTab('data-management')}
        >
          Data Management
        </button>
      </div>

      {adminTab === 'import' ? (
        <AdministrationImport
          allMyClients={allMyClients}
          connectClientId={canConnect ? connectClientId : 0}
          scopedClientId={scopedClientId}
          availableClients={availableClients}
          onScopedClientId={setScopedClientId}
          clientName={scopedClientName}
        />
      ) : null}

      {adminTab === 'client-data-import' ? (
        <AdministrationClientDataImport
          allMyClients={allMyClients}
          connectClientId={canConnect ? connectClientId : 0}
          scopedClientId={scopedClientId}
          availableClients={availableClients}
          onScopedClientId={setScopedClientId}
          clientName={scopedClientName}
        />
      ) : null}

      {adminTab === 'data-management' ? (
        <div>
          <div className="setup-campaign-tabs" role="tablist" aria-label="Data Management">
            <button
              type="button"
              role="tab"
              aria-selected={dataMgmtTab === 'export'}
              className={
                dataMgmtTab === 'export'
                  ? 'setup-campaign-tab setup-campaign-tab--active'
                  : 'setup-campaign-tab'
              }
              onClick={() => setDataMgmtTab('export')}
            >
              Master Data Export
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={dataMgmtTab === 'master-data'}
              className={
                dataMgmtTab === 'master-data'
                  ? 'setup-campaign-tab setup-campaign-tab--active'
                  : 'setup-campaign-tab'
              }
              onClick={() => setDataMgmtTab('master-data')}
            >
              Master Data
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={dataMgmtTab === 'leadmaster-refresh'}
              className={
                dataMgmtTab === 'leadmaster-refresh'
                  ? 'setup-campaign-tab setup-campaign-tab--active'
                  : 'setup-campaign-tab'
              }
              onClick={() => setDataMgmtTab('leadmaster-refresh')}
            >
              LeadMaster Refresh
            </button>
          </div>
          {dataMgmtTab === 'export' ? <AdministrationMasterDataExport /> : null}
          {dataMgmtTab === 'master-data' ? <AdministrationDataSteward /> : null}
          {dataMgmtTab === 'leadmaster-refresh' ? (
            <AdministrationLeadmasterRefresh
              allMyClients={allMyClients}
              connectClientId={canConnect ? connectClientId : 0}
              scopedClientId={scopedClientId}
              availableClients={availableClients}
              onScopedClientId={setScopedClientId}
              clientName={scopedClientName}
            />
          ) : null}
        </div>
      ) : null}

      {adminTab === 'email' ? (
      <section className="administration-section" aria-labelledby="email-connections-heading">
        <h2 id="email-connections-heading">Email Connections</h2>
        <p className="queue-sub">
          Connections are client-scoped. Connect and disconnect only for the selected client.
        </p>

        {allMyClients ? (
          <label className="setup-field" style={{ maxWidth: '24rem', margin: '0.85rem 0' }}>
            <span className="setup-field-label">Client</span>
            <select
              value={scopedClientId > 0 ? String(scopedClientId) : ''}
              onChange={(e) => setScopedClientId(Number(e.target.value) || 0)}
              aria-label="Client for Gmail connections"
            >
              <option value="">Select a client…</option>
              {availableClients.map((c) => (
                <option key={c.client_id} value={c.client_id}>
                  {c.client_name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <p className="queue-sub" style={{ margin: '0.85rem 0' }}>
            Active Client: <strong>{scopedClientName}</strong>
          </p>
        )}

        {allMyClients && !canConnect ? (
          <p className="data-status" role="status">
            All My Clients is selected. Choose a specific client before connecting Gmail.
          </p>
        ) : null}

        {banner ? (
          <p
            className={
              banner.kind === 'success'
                ? 'status-banner'
                : 'data-status data-status--error'
            }
            role={banner.kind === 'error' ? 'alert' : 'status'}
          >
            {banner.text}
          </p>
        ) : null}

        {error ? (
          <p className="data-status data-status--error" role="alert">
            {error}
          </p>
        ) : null}

        {canConnect && loading ? <p className="data-status">Loading email connections…</p> : null}

        {canConnect && !loading && accounts.length === 0 ? (
          <p className="data-status">
            No sender accounts for {scopedClientName}. Add a sender under Clients → Knowledge,
            then return here to connect Gmail.
          </p>
        ) : null}

        {canConnect && !loading && accounts.length > 0 ? (
          <div className="queue-table-wrap">
            <table className="queue-table">
              <thead>
                <tr>
                  <th>Sender email</th>
                  <th>Client</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => {
                  const ui = emailConnectionUiStatus(
                    account.connection_status,
                    (account.connection_status || '').toLowerCase() === 'connected',
                  )
                  const action = actionFor(ui)
                  const busy = busyId === account.account_id
                  return (
                    <tr key={account.account_id}>
                      <td>
                        <strong>{account.email_address}</strong>
                      </td>
                      <td>{clientNameFor(account.client_id, availableClients)}</td>
                      <td>{emailConnectionLabel(ui)}</td>
                      <td>
                        {action === 'disconnect' ? (
                          <button
                            type="button"
                            className="link-btn"
                            disabled={busy}
                            onClick={() => void onDisconnect(account)}
                          >
                            Disconnect
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="primary-btn"
                            disabled={busy || !canConnect}
                            onClick={() => void startGoogleOAuth(account)}
                          >
                            {action === 'reconnect' ? 'Reconnect Gmail' : 'Connect Gmail'}
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
      ) : null}
    </div>
  )
}
