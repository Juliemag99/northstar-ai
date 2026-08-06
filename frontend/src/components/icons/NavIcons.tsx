import type { NavIconName } from '../../data/navigation'

interface IconProps {
  name: NavIconName
  className?: string
}

export function NavIcon({ name, className }: IconProps) {
  const common = {
    className,
    width: 18,
    height: 18,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  }

  switch (name) {
    case 'dashboard':
      return (
        <svg {...common}>
          <rect x="3" y="3" width="7" height="9" rx="1.5" />
          <rect x="14" y="3" width="7" height="5" rx="1.5" />
          <rect x="14" y="12" width="7" height="9" rx="1.5" />
          <rect x="3" y="16" width="7" height="5" rx="1.5" />
        </svg>
      )
    case 'prospects':
      return (
        <svg {...common}>
          <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
          <circle cx="9" cy="7" r="3.5" />
          <path d="M22 21v-2a3.5 3.5 0 0 0-2.5-3.35" />
          <path d="M16.5 3.7a3.5 3.5 0 0 1 0 6.6" />
        </svg>
      )
    case 'companies':
      return (
        <svg {...common}>
          <path d="M3 21h18" />
          <path d="M5 21V7l7-4 7 4v14" />
          <path d="M9 21v-6h6v6" />
          <path d="M9 10h.01M15 10h.01M9 14h.01M15 14h.01" />
        </svg>
      )
    case 'contacts':
      return (
        <svg {...common}>
          <rect x="4" y="3" width="16" height="18" rx="2" />
          <circle cx="12" cy="10" r="2.5" />
          <path d="M8.5 17.5c.8-1.8 2-2.7 3.5-2.7s2.7.9 3.5 2.7" />
        </svg>
      )
    case 'activities':
      return (
        <svg {...common}>
          <path d="M4 6h16M4 12h10M4 18h13" />
          <circle cx="18" cy="12" r="2" />
          <circle cx="19.5" cy="18" r="1.5" />
        </svg>
      )
    case 'appointments':
      return (
        <svg {...common}>
          <rect x="3.5" y="5" width="17" height="15" rx="2" />
          <path d="M8 3.5v3M16 3.5v3M3.5 10h17" />
          <path d="M8.5 14h3M14.5 14h1" />
        </svg>
      )
    case 'campaigns':
      return (
        <svg {...common}>
          <path d="M4 12v5a2 2 0 0 0 2 2h3" />
          <path d="M20 8V7a2 2 0 0 0-2-2h-3" />
          <path d="M4 9l8-4 8 4-8 4-8-4z" />
          <path d="M4 13l8 4 8-4" />
        </svg>
      )
    case 'tasks':
      return (
        <svg {...common}>
          <path d="M9 6h11M9 12h11M9 18h11" />
          <path d="M4.5 6.5l1 1 2-2M4.5 12.5l1 1 2-2M4.5 18.5l1 1 2-2" />
        </svg>
      )
    case 'reports':
      return (
        <svg {...common}>
          <path d="M4 19V5M4 19h16" />
          <path d="M8 15v-4M12 15V8M16 15v-6" />
        </svg>
      )
    case 'research':
      return (
        <svg {...common}>
          <circle cx="11" cy="11" r="6.5" />
          <path d="M16 16l4.5 4.5" />
        </svg>
      )
    case 'clients':
      return (
        <svg {...common}>
          <path d="M3 20h18" />
          <path d="M6 20V9h4v11M14 20V5h4v15" />
          <path d="M8 12h.01M16 9h.01M16 13h.01" />
        </svg>
      )
    case 'administration':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="3" />
          <path d="M12 3.5v2.2M12 18.3v2.2M4.9 6.5l1.6 1.6M17.5 16l1.6 1.6M3.5 12h2.2M18.3 12h2.2M4.9 17.5l1.6-1.6M17.5 8l1.6-1.6" />
        </svg>
      )
    default:
      return null
  }
}
