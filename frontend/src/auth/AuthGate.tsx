import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import Login from '../Login'
import { loginRedirectPath, safeReturnPath } from './safeReturnPath'
import { useAuth } from './useAuth'

function SessionSplash() {
  return (
    <div className="login-page">
      <div className="login-card">
        <p className="login-brand">NorthStar AI</p>
        <h1>Loading…</h1>
        <p className="login-notice" role="status">
          Checking your session.
        </p>
      </div>
    </div>
  )
}

function SessionUnavailable({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="login-page">
      <div className="login-card">
        <p className="login-brand">NorthStar AI</p>
        <h1>Unable to verify your session</h1>
        <p className="login-notice" role="alert">
          NorthStar could not confirm whether you are signed in. Retry before opening CRM
          content.
        </p>
        <button type="button" className="primary-btn" onClick={onRetry}>
          Retry
        </button>
      </div>
    </div>
  )
}

export function AuthGate({ children }: { children: ReactNode }) {
  const location = useLocation()
  const { ready, sessionError, authenticated, authEnforced, retrySession } = useAuth()
  const isLogin = location.pathname === '/login'

  if (!ready) {
    if (sessionError) {
      return <SessionUnavailable onRetry={() => void retrySession()} />
    }
    return <SessionSplash />
  }

  if (authEnforced && !authenticated && !isLogin) {
    return (
      <Navigate to={loginRedirectPath(`${location.pathname}${location.search}`)} replace />
    )
  }

  if (authEnforced && authenticated && isLogin) {
    const params = new URLSearchParams(location.search)
    return <Navigate to={safeReturnPath(params.get('next')) ?? '/'} replace />
  }

  if (isLogin) {
    return <Login />
  }

  return <>{children}</>
}
