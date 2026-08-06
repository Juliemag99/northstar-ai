import { ActivityFeed } from '../components/dashboard/ActivityFeed'
import { AppointmentsList } from '../components/dashboard/AppointmentsList'
import { MetricCard } from '../components/dashboard/MetricCard'
import { ProspectTable } from '../components/dashboard/ProspectTable'
import {
  DASHBOARD_METRICS,
  PRIORITY_PROSPECTS,
  RECENT_ACTIVITY,
  UPCOMING_APPOINTMENTS,
} from '../data/mockDashboard'
import './DashboardPage.css'

export function DashboardPage() {
  const today = new Intl.DateTimeFormat('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  }).format(new Date())

  return (
    <div className="ns-dashboard">
      <section className="ns-dashboard__intro">
        <div>
          <h2 className="ns-dashboard__heading">Today at a glance</h2>
          <p className="ns-dashboard__copy">
            Focus outreach on Carmeco and priority accounts — calls, follow-ups, and hot
            prospects needing attention.
          </p>
        </div>
        <p className="ns-dashboard__date">{today}</p>
      </section>

      <section className="ns-dashboard__metrics" aria-label="Key metrics">
        {DASHBOARD_METRICS.map((metric, index) => (
          <MetricCard key={metric.id} metric={metric} index={index} />
        ))}
      </section>

      <section className="ns-dashboard__grid">
        <ProspectTable prospects={PRIORITY_PROSPECTS} />
        <ActivityFeed items={RECENT_ACTIVITY} />
      </section>

      <section className="ns-dashboard__appointments" aria-label="Upcoming appointments">
        <AppointmentsList appointments={UPCOMING_APPOINTMENTS} />
      </section>
    </div>
  )
}
