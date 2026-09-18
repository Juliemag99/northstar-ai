import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Administration from './Administration'
import AdministrationMasterDataExport from './AdministrationMasterDataExport'
import { AuthContext, type AuthContextValue } from './auth/useAuth'
import type { StaffUser } from './api/auth'
import * as masterDataExport from './api/masterDataExport'

vi.mock('./api/masterDataExport', async () => {
  const actual = await vi.importActual<typeof import('./api/masterDataExport')>(
    './api/masterDataExport',
  )
  return {
    ...actual,
    downloadMasterDataExport: vi.fn(),
  }
})

const adminUser: StaffUser = {
  id: 1,
  email: 'juliem@n-star.us',
  full_name: 'Julie Magnani',
  is_administrator: true,
  is_internal_northstar: true,
  active: true,
  created_at: '2026-08-07 16:18:04',
}

function authValue(overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  return {
    ready: true,
    sessionError: false,
    user: adminUser,
    authenticated: true,
    authAvailable: true,
    authEnforced: false,
    login: vi.fn(async () => undefined),
    logout: vi.fn(async () => undefined),
    retrySession: vi.fn(async () => undefined),
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.mocked(masterDataExport.downloadMasterDataExport).mockReset()
})

describe('Master Data Export', () => {
  it('renders export controls with Excel as the default', () => {
    render(<AdministrationMasterDataExport />)
    expect(screen.getByRole('heading', { name: 'Master Data Export' })).toBeTruthy()
    expect(
      screen.getByText(
        'Exports the current NorthStar master database. Client-specific statuses, assignments and campaign memberships remain labeled by client.',
      ),
    ).toBeTruthy()
    expect(screen.queryByLabelText('Client')).toBeNull()
    expect(screen.queryByLabelText(/Active Client/i)).toBeNull()
    const companiesOnly = screen.getByRole('radio', { name: 'Companies Only' }) as HTMLInputElement
    const excel = screen.getByRole('radio', { name: 'Excel (.xlsx)' }) as HTMLInputElement
    expect(companiesOnly.checked).toBe(true)
    expect(excel.checked).toBe(true)
    expect((screen.getByRole('radio', { name: 'CSV' }) as HTMLInputElement).checked).toBe(false)
  })

  it('downloads the default companies Excel export', async () => {
    vi.mocked(masterDataExport.downloadMasterDataExport).mockResolvedValue()
    render(<AdministrationMasterDataExport />)
    fireEvent.click(screen.getByRole('button', { name: 'Download Export' }))
    await waitFor(() => {
      expect(masterDataExport.downloadMasterDataExport).toHaveBeenCalledWith({
        mode: 'companies',
        format: 'xlsx',
      })
    })
  })

  it('downloads Companies + Contacts CSV when selected', async () => {
    vi.mocked(masterDataExport.downloadMasterDataExport).mockResolvedValue()
    render(<AdministrationMasterDataExport />)
    fireEvent.click(screen.getByRole('radio', { name: 'Companies + Contacts' }))
    fireEvent.click(screen.getByRole('radio', { name: 'CSV' }))
    fireEvent.click(screen.getByRole('button', { name: 'Download Export' }))
    await waitFor(() => {
      expect(masterDataExport.downloadMasterDataExport).toHaveBeenCalledWith({
        mode: 'companies-contacts',
        format: 'csv',
      })
    })
  })

  it('shows Data Management export without requiring an Active Client', () => {
    render(
      <AuthContext.Provider value={authValue()}>
        <MemoryRouter initialEntries={['/administration']}>
          <Routes>
            <Route
              path="/administration"
              element={<Administration activeClientId={null} availableClients={[]} />}
            />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>,
    )
    fireEvent.click(screen.getByRole('tab', { name: 'Data Management' }))
    expect(screen.getByRole('heading', { name: 'Master Data Export' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download Export' })).toBeTruthy()
    expect(screen.queryByLabelText('Client')).toBeNull()
    expect(screen.queryByLabelText('Client for Gmail connections')).toBeNull()
  })
})
