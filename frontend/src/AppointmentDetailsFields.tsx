import type { Ref } from 'react'
import type { ContactRepOption } from './api/carmeco'
import {
  APPOINTMENT_SOURCES,
  APPOINTMENT_TIMEZONES,
  APPOINTMENT_TYPES,
  type AppointmentDetails,
} from './appointmentSet'

export default function AppointmentDetailsFields({
  value,
  onChange,
  reps,
  companyName,
  contactName,
  idPrefix = 'appt',
  headingRef,
}: {
  value: AppointmentDetails
  onChange: (next: AppointmentDetails) => void
  reps: ContactRepOption[]
  companyName: string
  contactName: string
  idPrefix?: string
  headingRef?: Ref<HTMLHeadingElement>
}) {
  function patch(partial: Partial<AppointmentDetails>) {
    onChange({ ...value, ...partial })
  }

  return (
    <div className="appt-details">
      <h3
        ref={headingRef}
        id={`${idPrefix}-heading`}
        className="add-note-card__title"
      >
        Appointment Details
      </h3>
      <p className="muted-note">
        Required when the call outcome is Appointment Set or Appt Set Email Marketing. Choose a
        date and start time, or mark Date/Time TBD.
      </p>
      <label className="schedule-follow-toggle">
        <input
          type="checkbox"
          checked={value.datetime_tbd}
          onChange={(e) =>
            patch({
              datetime_tbd: e.target.checked,
              appointment_date: e.target.checked ? '' : value.appointment_date,
              start_time: e.target.checked ? '' : value.start_time,
            })
          }
        />
        Date/Time TBD
      </label>
      <div className="milestone-form-grid">
        <label className="edit-field">
          <span className="edit-field__label">Appointment date</span>
          <input
            id={`${idPrefix}-date`}
            className="edit-input"
            type="date"
            value={value.appointment_date}
            onChange={(e) => patch({ appointment_date: e.target.value })}
            disabled={value.datetime_tbd}
            required={!value.datetime_tbd}
          />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Start time</span>
          <input
            id={`${idPrefix}-time`}
            className="edit-input"
            type="time"
            value={value.start_time}
            onChange={(e) => patch({ start_time: e.target.value })}
            disabled={value.datetime_tbd}
            required={!value.datetime_tbd}
          />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Time zone</span>
          <select
            className="edit-select"
            value={value.timezone}
            onChange={(e) => patch({ timezone: e.target.value })}
          >
            {APPOINTMENT_TIMEZONES.map((tz) => (
              <option key={tz.value} value={tz.value}>
                {tz.label}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Appointment type</span>
          <select
            className="edit-select"
            value={value.appointment_type}
            onChange={(e) => patch({ appointment_type: e.target.value })}
          >
            {APPOINTMENT_TYPES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Meeting link or location</span>
          <input
            className="edit-input"
            value={value.location_or_link}
            onChange={(e) => patch({ location_or_link: e.target.value })}
            placeholder="Video link or address"
          />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Company</span>
          <input className="edit-input" value={companyName} readOnly />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Contact</span>
          <input className="edit-input" value={contactName} readOnly />
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Revenue Development Specialist</span>
          <select
            className="edit-select"
            value={value.revenue_specialist_user_id ? String(value.revenue_specialist_user_id) : ''}
            onChange={(e) =>
              patch({
                revenue_specialist_user_id: e.target.value ? Number(e.target.value) : null,
              })
            }
          >
            <option value="">Unassigned</option>
            {reps.map((rep) => (
              <option key={rep.user_id} value={rep.user_id}>
                {rep.full_name}
              </option>
            ))}
          </select>
        </label>
        <label className="edit-field">
          <span className="edit-field__label">Source</span>
          <select
            className="edit-select"
            value={value.source}
            onChange={(e) => patch({ source: e.target.value })}
          >
            {APPOINTMENT_SOURCES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="edit-field__label" htmlFor={`${idPrefix}-notes`}>
        Appointment notes
      </label>
      <textarea
        id={`${idPrefix}-notes`}
        className="edit-textarea add-note-card__textarea"
        rows={3}
        value={value.notes}
        onChange={(e) => patch({ notes: e.target.value })}
        placeholder="Agenda, attendees, or setup notes…"
      />
    </div>
  )
}
