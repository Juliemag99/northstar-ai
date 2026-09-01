/** Same-app return paths only. Never honor external or login targets. */

const APP_ORIGIN = 'http://northstar.local'

export function safeReturnPath(raw: string | null | undefined): string | null {
  if (raw == null) return null
  const value = raw.trim()
  if (!value.startsWith('/') || value.startsWith('//') || value.includes('\\')) {
    return null
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(value)) return null

  try {
    const url = new URL(value, APP_ORIGIN)
    if (url.origin !== APP_ORIGIN) return null
    if (url.username || url.password) return null
    const pathname = url.pathname
    if (pathname === '/login' || pathname === '/login/' || pathname.startsWith('/login/')) {
      return null
    }
    if (!pathname.startsWith('/')) return null
    return `${pathname}${url.search}`
  } catch {
    return null
  }
}

export function loginRedirectPath(currentPathAndSearch: string): string {
  const safe = safeReturnPath(currentPathAndSearch)
  if (!safe) return '/login'
  return `/login?next=${encodeURIComponent(safe)}`
}
