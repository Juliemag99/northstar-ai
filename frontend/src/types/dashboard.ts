export type MetricTone = 'default' | 'warning' | 'success' | 'danger' | 'info'

export interface DashboardMetric {
  id: string
  label: string
  value: number
  helper: string
  tone: MetricTone
}

export type ProspectPriority = 'Critical' | 'High' | 'Medium'

export interface PriorityProspect {
  id: string
  name: string
  company: string
  title: string
  priority: ProspectPriority
  nextAction: string
  dueLabel: string
  owner: string
}

export type ActivityType = 'call' | 'email' | 'meeting' | 'note' | 'assignment'

export interface RecentActivityItem {
  id: string
  type: ActivityType
  title: string
  detail: string
  actor: string
  timestamp: string
}
