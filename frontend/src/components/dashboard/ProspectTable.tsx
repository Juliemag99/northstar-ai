import type { PriorityProspect, ProspectPriority } from '../../types/dashboard'
import { Badge } from '../ui/Badge'
import './ProspectTable.css'

interface ProspectTableProps {
  prospects: PriorityProspect[]
}

function priorityTone(priority: ProspectPriority) {
  switch (priority) {
    case 'Critical':
      return 'critical' as const
    case 'High':
      return 'high' as const
    default:
      return 'medium' as const
  }
}

export function ProspectTable({ prospects }: ProspectTableProps) {
  return (
    <section className="ns-panel ns-prospects">
      <div className="ns-panel__header">
        <div>
          <h2 className="ns-panel__title">Today&apos;s Priority Prospects</h2>
          <p className="ns-panel__subtitle">Highest-urgency NorthStar and Carmeco accounts needing action today</p>
        </div>
      </div>

      <div className="ns-prospects__table-wrap">
        <table className="ns-prospects__table">
          <thead>
            <tr>
              <th scope="col">Prospect</th>
              <th scope="col">Company</th>
              <th scope="col">Priority</th>
              <th scope="col">Next action</th>
              <th scope="col">Due</th>
              <th scope="col">Owner</th>
            </tr>
          </thead>
          <tbody>
            {prospects.map((prospect) => (
              <tr key={prospect.id}>
                <td>
                  <div className="ns-prospects__person">
                    <span className="ns-prospects__name">{prospect.name}</span>
                    <span className="ns-prospects__role">{prospect.title}</span>
                  </div>
                </td>
                <td>{prospect.company}</td>
                <td>
                  <Badge label={prospect.priority} tone={priorityTone(prospect.priority)} />
                </td>
                <td>{prospect.nextAction}</td>
                <td>
                  <span
                    className={
                      prospect.dueLabel.toLowerCase().includes('overdue')
                        ? 'ns-prospects__due is-overdue'
                        : 'ns-prospects__due'
                    }
                  >
                    {prospect.dueLabel}
                  </span>
                </td>
                <td>{prospect.owner}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
