import { useState, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { GENERIC_LOGIN_ERROR } from './api/auth'
import { takePasswordResetNotice } from './auth/passwordResetNotice'
import { useAuth } from './auth/useAuth'
import { safeReturnPath } from './auth/safeReturnPath'

export default function Login() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { login, authAvailable, authEnforced } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [resetNotice] = useState(() => takePasswordResetNotice())

  function continueWithoutSigningIn() {
    navigate('/')
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (pending) return
    setError(null)
    setPending(true)
    try {
      await login(email, password)
      setPassword('')
      navigate(safeReturnPath(searchParams.get('next')) ?? '/')
    } catch {
      setError(GENERIC_LOGIN_ERROR)
      setPassword('')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <p className="login-brand">NorthStar AI</p>
        <h1>Staff sign in</h1>
        {resetNotice ? (
          <p className="login-notice" role="status">
            {resetNotice}
          </p>
        ) : null}
        {!authAvailable && (
          <p className="login-notice" role="status">
            Staff sign-in is not configured yet. You can continue without signing in.
          </p>
        )}
        <form className="login-form" onSubmit={onSubmit}>
          <label>
            Email
            <input
              type="email"
              name="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={pending}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={pending}
              required
            />
          </label>
          {error ? (
            <p className="login-error" role="alert">
              {error}
            </p>
          ) : null}
          <button type="submit" className="primary-btn" disabled={pending}>
            {pending ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        {!authEnforced && (
          <button
            type="button"
            className="link-btn login-continue"
            onClick={continueWithoutSigningIn}
            disabled={pending}
          >
            Continue without signing in
          </button>
        )}
      </div>
    </div>
  )
}
