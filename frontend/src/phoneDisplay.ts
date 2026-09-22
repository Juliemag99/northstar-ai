/** Display and split structured CRM phone + extension. Storage stays separate. */

const EXT_RE =
  /(?:(?:[\s\-./,])*(?:extension|ext\.?|xt)\s*[:.\-]?\s*|(?<![A-Za-z])x\s*[:.\-]?\s*|\s*#\s*)(\d{1,6})\)?\s*$/i

export function digitsOnlyExtension(raw: string | null | undefined): string {
  return String(raw || '').replace(/\D/g, '')
}

export function formatPhoneWithExtension(
  main: string | null | undefined,
  extension: string | null | undefined = '',
): string {
  const phone = String(main || '').trim()
  const ext = digitsOnlyExtension(extension)
  if (phone && ext) return `${phone} x${ext}`
  return phone
}

export function splitPhoneExtension(raw: string | null | undefined): {
  main: string
  extension: string
} {
  const trimmed = String(raw || '').trim()
  if (!trimmed) return { main: '', extension: '' }
  const extMatch = trimmed.match(EXT_RE)
  if (!extMatch || extMatch.index == null) {
    return { main: trimmed, extension: '' }
  }
  return {
    main: trimmed.slice(0, extMatch.index).trim(),
    extension: digitsOnlyExtension(extMatch[1] || ''),
  }
}

export function formatUsPhoneDisplay(raw: string | null | undefined): string {
  const trimmed = String(raw || '').trim()
  if (!trimmed) return ''
  const { main, extension } = splitPhoneExtension(trimmed)
  let digits = main.replace(/\D/g, '')
  if (digits.length >= 11 && digits.startsWith('1')) digits = digits.slice(1)
  if (digits.length > 10) digits = digits.slice(0, 10)
  if (digits.length !== 10) {
    return extension ? formatPhoneWithExtension(trimmed, extension) : trimmed
  }
  const formatted = `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`
  return formatPhoneWithExtension(formatted, extension)
}
