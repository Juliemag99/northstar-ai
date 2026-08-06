export interface NavItem {
  label: string
  path: string
  icon: NavIconName
}

export type NavIconName =
  | 'dashboard'
  | 'prospects'
  | 'companies'
  | 'contacts'
  | 'activities'
  | 'appointments'
  | 'campaigns'
  | 'tasks'
  | 'reports'
  | 'research'
  | 'clients'
  | 'administration'

export const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard', path: '/', icon: 'dashboard' },
  { label: 'Prospects', path: '/prospects', icon: 'prospects' },
  { label: 'Companies', path: '/companies', icon: 'companies' },
  { label: 'Contacts', path: '/contacts', icon: 'contacts' },
  { label: 'Activities', path: '/activities', icon: 'activities' },
  { label: 'Appointments', path: '/appointments', icon: 'appointments' },
  { label: 'Campaigns', path: '/campaigns', icon: 'campaigns' },
  { label: 'Tasks', path: '/tasks', icon: 'tasks' },
  { label: 'Reports', path: '/reports', icon: 'reports' },
  { label: 'Research', path: '/research', icon: 'research' },
  { label: 'Clients', path: '/clients', icon: 'clients' },
  { label: 'Administration', path: '/administration', icon: 'administration' },
]
