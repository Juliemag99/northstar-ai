import type { UpcomingAppointment } from '../../types/dashboard'
import { Badge } from '../ui/Badge'
import './AppointmentsList.css'

interface AppointmentsListProps {
  appointments: UpcomingAppointment[]
}

function typeTone(type: UpcomingAppointment['type']) {
  switch (type) {
    case 'Discovery':
      return 'info' as const
    case 'Review':
      return 'high' as const
    case 'Demo':
      return 'success' as const
    default:
      return 'medium' as const
  }
}

export function AppointmentsList({ appointments }: AppointmentsListProps) {
  return (
    <section className="ns-panel ns-appointments">
      <div className="ns-panel__header">
        <div>
          <h2 className="ns-panel__title">Upcoming Appointments</h2>
          <p className="ns-panel__subtitle">Scheduled meetings for the rest of today</p>
        </div>
      </div>

      <ul className="ns-appointments__list">
        {appointments.map((appointment, index) => (
          <li
            key={appointment.id}
            className="ns-appointments__item"
            style={{ animationDelay: `${200 + index * 55}ms` }}
          >
            <div className="ns-appointments__time" aria-label={`Starts at ${appointment.time}`}>
              {appointment.time}
            </div>
            <div className="ns-appointments__body">
              <div className="ns-appointments__top">
                <p className="ns-appointments__title">{appointment.title}</p>
                <Badge label={appointment.type} tone={typeTone(appointment.type)} />
              </div>
              <p className="ns-appointments__meta">
                {appointment.contact} · {appointment.company}
              </p>
              <p className="ns-appointments__owner">{appointment.owner}</p>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
