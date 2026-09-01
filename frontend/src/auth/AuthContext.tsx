import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  applyParsedAuth,
  fetchCurrentUser,
  login as requestLogin,
  logout as requestLogout,
  type ParsedAuth,
  type StaffUser,
} from '../api/auth'
import { clearCsrfToken, setUnauthorizedHandler } from '../api/http'
import { AuthContext, type AuthContextValue } from './useAuth'

export function AuthProvider({ children }: { children: ReactNode }) {
  const mounted = useRef(true)
  const generation = useRef(0)
  const readyRef = useRef(false)
  const sawEnforcedRef = useRef(false)
  const [ready, setReady] = useState(false)
  const [sessionError, setSessionError] = useState(false)
  const [user, setUser] = useState<StaffUser | null>(null)
  const [authenticated, setAuthenticated] = useState(false)
  const [authAvailable, setAuthAvailable] = useState(false)
  const [authEnforced, setAuthEnforced] = useState(false)

  const isCurrent = useCallback((gen: number) => {
    return mounted.current && gen === generation.current
  }, [])

  const nextGeneration = useCallback(() => {
    generation.current += 1
    return generation.current
  }, [])

  const commitParsed = useCallback(
    (gen: number, parsed: ParsedAuth) => {
      if (!isCurrent(gen)) return false
      const snapshot = applyParsedAuth(parsed)
      if (snapshot.authEnforced) sawEnforcedRef.current = true
      readyRef.current = true
      setUser(snapshot.user)
      setAuthenticated(snapshot.authenticated)
      setAuthAvailable(snapshot.authAvailable)
      setAuthEnforced(snapshot.authEnforced)
      setSessionError(false)
      setReady(true)
      return true
    },
    [isCurrent],
  )

  const failClosed = useCallback(
    (gen: number) => {
      if (!isCurrent(gen)) return
      if (readyRef.current && !sawEnforcedRef.current) return
      readyRef.current = false
      setSessionError(true)
      setReady(false)
    },
    [isCurrent],
  )

  const loadSession = useCallback(
    async (gen: number) => {
      try {
        const parsed = await fetchCurrentUser()
        commitParsed(gen, parsed)
      } catch {
        failClosed(gen)
      }
    },
    [commitParsed, failClosed],
  )

  useEffect(() => {
    mounted.current = true
    const gen = nextGeneration()
    void loadSession(gen)
    return () => {
      mounted.current = false
    }
  }, [loadSession, nextGeneration])

  useEffect(() => {
    function onFocus() {
      void loadSession(generation.current)
    }
    window.addEventListener('focus', onFocus)
    return () => {
      window.removeEventListener('focus', onFocus)
    }
  }, [loadSession])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (!sawEnforcedRef.current) return
      nextGeneration()
      clearCsrfToken()
      if (!mounted.current) return
      readyRef.current = true
      setUser(null)
      setAuthenticated(false)
      setAuthEnforced(true)
      setSessionError(false)
      setReady(true)
    })
    return () => {
      setUnauthorizedHandler(null)
    }
  }, [nextGeneration])

  const retrySession = useCallback(async () => {
    const gen = nextGeneration()
    setSessionError(false)
    await loadSession(gen)
  }, [loadSession, nextGeneration])

  const login = useCallback(
    async (email: string, password: string) => {
      const gen = nextGeneration()
      const parsed = await requestLogin(email, password)
      commitParsed(gen, parsed)
    },
    [commitParsed, nextGeneration],
  )

  const logout = useCallback(async () => {
    const gen = nextGeneration()
    let logoutError: unknown = null
    let logoutParsed: ParsedAuth | undefined
    try {
      const attempt = await requestLogout()
      if (attempt.ok) logoutParsed = attempt.parsed
    } catch (error) {
      logoutError = error
    }

    let meParsed: ParsedAuth | undefined
    try {
      meParsed = await fetchCurrentUser()
    } catch {
      meParsed = undefined
    }

    if (isCurrent(gen)) {
      if (meParsed) {
        commitParsed(gen, meParsed)
      } else if (sawEnforcedRef.current) {
        failClosed(gen)
      } else if (logoutParsed) {
        commitParsed(gen, logoutParsed)
      }
    }

    if (logoutError) throw logoutError
  }, [commitParsed, failClosed, isCurrent, nextGeneration])

  const value = useMemo<AuthContextValue>(
    () => ({
      ready,
      sessionError,
      user,
      authenticated,
      authAvailable,
      authEnforced,
      login,
      logout,
      retrySession,
    }),
    [
      ready,
      sessionError,
      user,
      authenticated,
      authAvailable,
      authEnforced,
      login,
      logout,
      retrySession,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
