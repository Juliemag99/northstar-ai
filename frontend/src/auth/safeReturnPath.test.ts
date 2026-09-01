import { describe, expect, it } from 'vitest'
import { loginRedirectPath, safeReturnPath } from './safeReturnPath'

describe('safeReturnPath', () => {
  it('keeps same-app path and search', () => {
    expect(safeReturnPath('/contacts/1?client_id=2')).toBe('/contacts/1?client_id=2')
    expect(safeReturnPath('/')).toBe('/')
    expect(safeReturnPath('/administration')).toBe('/administration')
  })

  it('rejects external, protocol-relative, javascript, and login targets', () => {
    expect(safeReturnPath('https://evil.example')).toBeNull()
    expect(safeReturnPath('//evil.example/phish')).toBeNull()
    expect(safeReturnPath('javascript:alert(1)')).toBeNull()
    expect(safeReturnPath('/login')).toBeNull()
    expect(safeReturnPath('/login/')).toBeNull()
    expect(safeReturnPath('/login?next=/')).toBeNull()
    expect(safeReturnPath('\\/\\/evil.example')).toBeNull()
    expect(safeReturnPath('')).toBeNull()
    expect(safeReturnPath(null)).toBeNull()
  })

  it('drops hashes and ignores userinfo tricks', () => {
    expect(safeReturnPath('/prospects#secret')).toBe('/prospects')
    expect(safeReturnPath('http://user@northstar.local/x')).toBeNull()
  })
})

describe('loginRedirectPath', () => {
  it('encodes a safe next parameter', () => {
    expect(loginRedirectPath('/prospects?client_id=2')).toBe(
      `/login?next=${encodeURIComponent('/prospects?client_id=2')}`,
    )
  })

  it('falls back to /login when the current path is unsafe', () => {
    expect(loginRedirectPath('https://evil.example')).toBe('/login')
    expect(loginRedirectPath('/login')).toBe('/login')
  })
})
