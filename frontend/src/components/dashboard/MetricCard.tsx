import type { DashboardMetric } from '../../types/dashboard'
import './MetricCard.css'

interface MetricCardProps {
  metric: DashboardMetric
  index?: number
}

export function MetricCard({ metric, index = 0 }: MetricCardProps) {
  return (
    <article
      className={`ns-metric ns-metric--${metric.tone}`}
      style={{ animationDelay: `${index * 55}ms` }}
    >
      <p className="ns-metric__label">{metric.label}</p>
      <p className="ns-metric__value">{metric.value}</p>
      <p className="ns-metric__helper">{metric.helper}</p>
    </article>
  )
}
