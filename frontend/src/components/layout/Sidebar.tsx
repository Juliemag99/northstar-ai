import { NavLink } from 'react-router-dom'
import { NAV_ITEMS } from '../../data/navigation'
import { NavIcon } from '../icons/NavIcons'
import './Sidebar.css'

interface SidebarProps {
  open: boolean
  onNavigate?: () => void
}

export function Sidebar({ open, onNavigate }: SidebarProps) {
  return (
    <aside className={`ns-sidebar ${open ? 'is-open' : ''}`} aria-label="Primary">
      <div className="ns-sidebar__brand">
        <div className="ns-sidebar__mark" aria-hidden="true">
          <svg viewBox="0 0 32 32" fill="none">
            <path
              d="M16 4L18.1 12.2L26.5 14L18.1 15.8L16 24L13.9 15.8L5.5 14L13.9 12.2L16 4Z"
              fill="currentColor"
            />
            <circle cx="16" cy="14" r="1.6" fill="#0A1628" />
          </svg>
        </div>
        <div className="ns-sidebar__brand-text">
          <span className="ns-sidebar__product">NorthStar AI</span>
          <span className="ns-sidebar__org">NorthStar Group</span>
        </div>
      </div>

      <nav className="ns-sidebar__nav">
        <p className="ns-sidebar__section-label">Workspace</p>
        <ul className="ns-sidebar__list">
          {NAV_ITEMS.map((item) => (
            <li key={item.path}>
              <NavLink
                to={item.path}
                end={item.path === '/'}
                className={({ isActive }) =>
                  `ns-sidebar__link${isActive ? ' is-active' : ''}`
                }
                onClick={onNavigate}
              >
                <NavIcon name={item.icon} className="ns-sidebar__icon" />
                <span>{item.label}</span>
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <div className="ns-sidebar__footer">
        <p className="ns-sidebar__footer-label">Private workspace</p>
        <p className="ns-sidebar__footer-meta">app.follownorthstar.com</p>
      </div>
    </aside>
  )
}
