import { createContext, useContext } from 'react'
import type { StaffUser } from '../api/auth'

export type AuthContextValue = {
  ready: boolean
  sessionError: boolean
  user: StaffUser | null
  authenticated: boolean
  authAvailable: boolean
  authEnforced: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  retrySession: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (value == null) {
    throw new Error('useAuth must be used within AuthProvider.')
  }
  return value
}
