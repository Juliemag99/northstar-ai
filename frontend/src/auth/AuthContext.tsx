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
import { AuthContext, type AuthContextValue } from './useAuth'

function emptySession(): Omit<AuthContextValue, 'ready' | 'login' | 'logout'> {
  return {
    user: null,
    authenticated: false,
    authAvailable: false,
    authEnforced: false,
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const mounted = useRef(true)
  const generation = useRef(0)
  const [ready, setReady] = useState(false)
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
      setUser(snapshot.user)
      setAuthenticated(snapshot.authenticated)
      setAuthAvailable(snapshot.authAvailable)
      setAuthEnforced(snapshot.authEnforced)
      setReady(true)
      return true
    },
    [isCurrent],
  )

  const failOpen = useCallback(
    (gen: number) => {
      if (!isCurrent(gen)) return
      const fallback = emptySession()
      setUser(fallback.user)
      setAuthenticated(fallback.authenticated)
      setAuthAvailable(fallback.authAvailable)
      setAuthEnforced(fallback.authEnforced)
      setReady(true)
    },
    [isCurrent],
  )

  useEffect(() => {
    mounted.current = true
    const gen = nextGeneration()
    void fetchCurrentUser()
      .then((parsed) => {
        commitParsed(gen, parsed)
      })
      .catch(() => {
        failOpen(gen)
      })
      .finally(() => {
        if (isCurrent(gen)) setReady(true)
      })
    return () => {
      mounted.current = false
    }
  }, [commitParsed, failOpen, isCurrent, nextGeneration])

  const login = useCallback(
    async (email: string, password: string) => {
      const gen = nextGeneration()
      try {
        const parsed = await requestLogin(email, password)
        commitParsed(gen, parsed)
      } catch (error) {
        if (isCurrent(gen)) setReady(true)
        throw error
      }
    },
    [commitParsed, isCurrent, nextGeneration],
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
      } else if (logoutParsed) {
        commitParsed(gen, logoutParsed)
      } else {
        setReady(true)
      }
    }

    if (logoutError) throw logoutError
  }, [commitParsed, isCurrent, nextGeneration])

  const value = useMemo<AuthContextValue>(
    () => ({
      ready,
      user,
      authenticated,
      authAvailable,
      authEnforced,
      login,
      logout,
    }),
    [ready, user, authenticated, authAvailable, authEnforced, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
