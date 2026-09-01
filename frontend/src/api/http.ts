/** Same-origin API helper: cookies plus in-memory CSRF for authenticated writes. */

const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

let csrfToken = ''
let unauthorizedHandler: (() => void) | null = null

export function setCsrfToken(token: string): void {
  csrfToken = typeof token === 'string' ? token.trim() : ''
}

export function clearCsrfToken(): void {
  csrfToken = ''
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

function requestPath(input: RequestInfo | URL): string {
  try {
    if (typeof input === 'string') {
      return new URL(input, 'http://northstar.local').pathname
    }
    if (input instanceof URL) {
      return input.pathname
    }
    return new URL(input.url, 'http://northstar.local').pathname
  } catch {
    return ''
  }
}

function shouldNotifyUnauthorized(input: RequestInfo | URL, init?: RequestInit): boolean {
  const method = (init?.method || 'GET').toUpperCase()
  const path = requestPath(input)
  if (path === '/api/auth/login' && method === 'POST') return false
  if (path === '/api/auth/me') return false
  return true
}

export function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const method = (init?.method || 'GET').toUpperCase()
  const headers = new Headers(init?.headers)
  if (WRITE_METHODS.has(method) && csrfToken && !headers.has('X-CSRF-Token')) {
    headers.set('X-CSRF-Token', csrfToken)
  }
  return fetch(input, {
    ...init,
    credentials: 'include',
    headers,
  }).then((response) => {
    if (response.status === 401 && shouldNotifyUnauthorized(input, init)) {
      unauthorizedHandler?.()
    }
    return response
  })
}
