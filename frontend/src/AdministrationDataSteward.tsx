import { useState } from 'react'

type StewardSection =
  | 'companies'
  | 'contacts'
  | 'locations'
  | 'archived'
  | 'duplicates'
  | 'relationships'
  | 'audit'

const SECTIONS: Array<{ id: StewardSection; label: string }> = [
  { id: 'companies', label: 'Companies' },
  { id: 'contacts', label: 'Contacts' },
  { id: 'locations', label: 'Locations' },
  { id: 'archived', label: 'Archived Records' },
  { id: 'duplicates', label: 'Duplicate Review' },
  { id: 'relationships', label: 'Client Relationships' },
  { id: 'audit', label: 'Audit History' },
]

export default function AdministrationDataSteward() {
  const [section, setSection] = useState<StewardSection>('companies')
  return (
    <section className="administration-section" aria-labelledby="data-steward-heading">
      <h2 id="data-steward-heading">Master Data</h2>
      <p className="queue-sub">
        Data Steward Phase DS2 records provenance on existing Add/Amend paths in isolated
        proof. Live archive, delete, and merge stay disabled. Schema is not on live
        northstar.db. Canonical company data is shared; client status, assignment, and notes
        stay on the client relationship. Remove From Client archives the relationship; it does
        not delete the CCR row or the company.
      </p>
      <div className="setup-campaign-tabs" role="tablist" aria-label="Master Data">
        {SECTIONS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={section === item.id}
            className={
              section === item.id
                ? 'setup-campaign-tab setup-campaign-tab--active'
                : 'setup-campaign-tab'
            }
            onClick={() => setSection(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <p className="queue-sub" role="status">
        Live archive / delete / merge not enabled
      </p>
      <div className="setup-actions" style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
        <button type="button" disabled>
          Archive (disabled)
        </button>
        <button type="button" disabled>
          Restore (disabled)
        </button>
        <button type="button" disabled>
          Merge (disabled)
        </button>
        <button type="button" disabled>
          Delete (disabled)
        </button>
      </div>
      {section === 'audit' ? (
        <div>
          <p className="queue-sub">
            Current Value / Current Source / Last Changed By / Last Changed At come from
            field_provenance_events. Live schema is not migrated, so this panel does not load
            production history.
          </p>
          <div className="setup-actions" style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
            <label>
              Company
              <input aria-label="Filter Company" disabled placeholder="Company id" />
            </label>
            <label>
              Contact
              <input aria-label="Filter Contact" disabled placeholder="Contact id" />
            </label>
            <label>
              Client Relationship
              <input aria-label="Filter Client Relationship" disabled placeholder="CCR id" />
            </label>
            <label>
              Field
              <input aria-label="Filter Field" disabled placeholder="phone" />
            </label>
            <label>
              Source
              <input aria-label="Filter Source" disabled placeholder="MANUAL_ADMIN" />
            </label>
            <label>
              Actor
              <input aria-label="Filter Actor" disabled placeholder="user id" />
            </label>
            <label>
              Date
              <input aria-label="Filter Date" disabled placeholder="2026-09-16" />
            </label>
          </div>
          <p className="queue-sub">
            Example history: Sep 16, 2026 2:14 PM · Julie · MANUAL_ADMIN · Company phone · Old:
            (402) 555-9876 · New: (402) 555-1212. LeadMaster KEEP_EXISTING does not steal
            current authority. ACCEPT_PROPOSED writes LEADMASTER as the latest source.
          </p>
        </div>
      ) : (
        <p className="queue-sub">
          Company detail actions when live enablement is separately authorized: Edit Company,
          Manage Locations, Manage Contacts, Manage Client Relationships, Archive, Duplicate
          Review / Merge, View Audit History. Removing a company from one client does not
          archive the canonical organization.
        </p>
      )}
    </section>
  )
}
