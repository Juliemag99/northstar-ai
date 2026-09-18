/** Administrator Master Data Export download helper. Same-origin blob, not JSON. */

import { apiFetch } from './http'

export type MasterDataExportMode = 'companies' | 'companies-contacts'
export type MasterDataExportFormat = 'xlsx' | 'csv'

export type MasterDataExportParams = {
  mode: MasterDataExportMode
  format: MasterDataExportFormat
}

export function masterDataExportQueryString(params: MasterDataExportParams): string {
  const query = new URLSearchParams()
  query.set('mode', params.mode)
  query.set('format', params.format)
  return query.toString()
}

export async function downloadMasterDataExport(params: MasterDataExportParams): Promise<void> {
  const response = await apiFetch(
    `/api/admin/master-data-export?${masterDataExportQueryString(params)}`,
  )
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `Export failed (${response.status})`)
  }
  const blob = await response.blob()
  const header = response.headers.get('Content-Disposition') || ''
  const match = header.match(/filename="([^"]+)"/)
  const fallback =
    params.format === 'csv' ? 'northstar-master-export.csv' : 'northstar-master-export.xlsx'
  const filename = match?.[1] || fallback
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
