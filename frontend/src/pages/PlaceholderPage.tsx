import { useLocation } from 'react-router-dom'
import { NAV_ITEMS } from '../data/navigation'
import './PlaceholderPage.css'

export function PlaceholderPage() {
  const { pathname } = useLocation()
  const item = NAV_ITEMS.find((nav) => nav.path === pathname)
  const label = item?.label ?? 'Module'

  return (
    <section className="ns-placeholder">
      <div className="ns-placeholder__card">
        <p className="ns-placeholder__eyebrow">Coming next</p>
        <h2 className="ns-placeholder__title">{label}</h2>
        <p className="ns-placeholder__copy">
          This module is part of the NorthStar AI roadmap. The application shell is ready;
          full {label.toLowerCase()} workflows will land in a later milestone.
        </p>
      </div>
    </section>
  )
}
