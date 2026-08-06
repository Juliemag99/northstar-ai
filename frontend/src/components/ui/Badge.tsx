import './Badge.css'

type BadgeTone = 'neutral' | 'critical' | 'high' | 'medium' | 'success' | 'info'

interface BadgeProps {
  label: string
  tone?: BadgeTone
}

export function Badge({ label, tone = 'neutral' }: BadgeProps) {
  return <span className={`ns-badge ns-badge--${tone}`}>{label}</span>
}
