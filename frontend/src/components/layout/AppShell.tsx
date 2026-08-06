import { useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { NAV_ITEMS } from '../../data/navigation'
import { Header } from './Header'
import { Sidebar } from './Sidebar'
import './AppShell.css'

function titleFromPath(pathname: string): string {
  const match = NAV_ITEMS.find((item) =>
    item.path === '/' ? pathname === '/' : pathname.startsWith(item.path),
  )
  return match?.label ?? 'NorthStar AI'
}

export function AppShell() {
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const title = titleFromPath(location.pathname)

  useEffect(() => {
    setSidebarOpen(false)
  }, [location.pathname])

  return (
    <div className="ns-shell">
      <Sidebar open={sidebarOpen} onNavigate={() => setSidebarOpen(false)} />
      {sidebarOpen ? (
        <button
          type="button"
          className="ns-shell__backdrop"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      ) : null}
      <div className="ns-shell__main">
        <Header title={title} onMenuToggle={() => setSidebarOpen((open) => !open)} />
        <main className="ns-shell__content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
