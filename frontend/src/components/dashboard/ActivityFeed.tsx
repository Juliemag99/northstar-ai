import type { ActivityType, RecentActivityItem } from '../../types/dashboard'
import './ActivityFeed.css'

interface ActivityFeedProps {
  items: RecentActivityItem[]
}

function activityLabel(type: ActivityType): string {
  switch (type) {
    case 'call':
      return 'Call'
    case 'email':
      return 'Email'
    case 'meeting':
      return 'Meeting'
    case 'note':
      return 'Note'
    case 'assignment':
      return 'Assignment'
    default:
      return 'Activity'
  }
}

export function ActivityFeed({ items }: ActivityFeedProps) {
  return (
    <section className="ns-panel ns-activity">
      <div className="ns-panel__header">
        <div>
          <h2 className="ns-panel__title">Recent Activity</h2>
          <p className="ns-panel__subtitle">Latest touches across your book</p>
        </div>
      </div>

      <ul className="ns-activity__list">
        {items.map((item, index) => (
          <li
            key={item.id}
            className="ns-activity__item"
            style={{ animationDelay: `${180 + index * 60}ms` }}
          >
            <div className={`ns-activity__marker ns-activity__marker--${item.type}`} aria-hidden="true" />
            <div className="ns-activity__body">
              <div className="ns-activity__top">
                <span className="ns-activity__type">{activityLabel(item.type)}</span>
                <time className="ns-activity__time">{item.timestamp}</time>
              </div>
              <p className="ns-activity__title">{item.title}</p>
              <p className="ns-activity__detail">{item.detail}</p>
              <p className="ns-activity__actor">{item.actor}</p>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
