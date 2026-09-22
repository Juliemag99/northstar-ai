import { describe, expect, it } from 'vitest'
import {
  digitsOnlyExtension,
  formatPhoneWithExtension,
  formatUsPhoneDisplay,
  splitPhoneExtension,
} from './phoneDisplay'

describe('phoneDisplay', () => {
  it('leaves a number without an extension unchanged', () => {
    expect(formatUsPhoneDisplay('(402) 555-1212')).toBe('(402) 555-1212')
    expect(formatPhoneWithExtension('(402) 555-1212', '')).toBe('(402) 555-1212')
  })

  it('renders a structured extension as x123', () => {
    expect(formatPhoneWithExtension('(402) 555-1212', '123')).toBe('(402) 555-1212 x123')
    expect(formatUsPhoneDisplay('(402) 555-1212 x123')).toBe('(402) 555-1212 x123')
  })

  it('keeps extension digits-only', () => {
    expect(digitsOnlyExtension('x12-3')).toBe('123')
    expect(formatPhoneWithExtension('(402) 555-1212', '12-3')).toBe('(402) 555-1212 x123')
  })

  it('splits a display string without concatenating storage fields', () => {
    expect(splitPhoneExtension('(402) 555-1212 x456')).toEqual({
      main: '(402) 555-1212',
      extension: '456',
    })
  })
})
