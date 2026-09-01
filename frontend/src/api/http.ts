/** Same-origin API helper: cookies plus in-memory CSRF for authenticated writes. */

const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

let csrfToken = ''

export function setCsrfToken(token: string): void {
  csrfToken = typeof token === 'string' ? token.trim() : ''
}

export function clearCsrfToken(): void {
  csrfToken = ''
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
  })
}
