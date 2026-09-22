import { useMemo, useState } from 'react'
import {
  dryRunResearchImport,
  saveResearchContactResolution,
  saveResearchImportMapping,
  saveResearchMatchResolution,
  uploadResearchImport,
  type ResearchDryRun,
  type ResearchImportBatch,
  type ResearchPlanRow,
} from './api/researchImport'
import {
  RESEARCH_CATEGORIES,
  RESEARCH_SOURCE_TYPES,
  VALUE_TYPES,
  columnsFromBatch,
  fieldsForCategory,
  mappingFromColumns,
  mappingsEqual,
  normalizeAttributeKey,
  validateResearchMapping,
  type ColumnDraft,
} from './researchImportMapping'

type AssignedClient = {
  client_id: number
  client_name: string
  client_code?: string
}

type Props = {
  allMyClients: boolean
  connectClientId: number | null
  scopedClientId: number
  availableClients: AssignedClient[]
  onScopedClientId: (clientId: number) => void
  clientName: string
}

function classLabel(value: string): string {
  switch (value) {
    case 'EXACT_EXISTING':
    case 'STRONG_EXISTING':
      return 'EXISTING COMPANY'
    case 'NEW_COMPANY':
      return 'NEW COMPANY'
    case 'EXISTING_COMPANY_NEW_LOCATION':
      return 'NEW LOCATION'
    case 'POSSIBLE_MATCH_REVIEW':
      return 'REVIEW REQUIRED'
    case 'AMBIGUOUS':
      return 'AMBIGUOUS'
    default:
      return value
  }
}

function contactLabel(value: string): string {
  switch (value) {
    case 'EXACT_CONTACT':
      return 'EXACT CONTACT'
    case 'STRONG_CONTACT':
      return 'STRONG CONTACT'
    case 'NEW_CONTACT':
      return 'NEW CONTACT'
    case 'POSSIBLE_CONTACT_REVIEW':
      return 'POSSIBLE CONTACT — REVIEW'
    case 'AMBIGUOUS_CONTACT':
      return 'AMBIGUOUS CONTACT'
    case 'INVALID_CONTACT':
      return 'NOT A CONTACT'
    default:
      return value
  }
}

export default function AdministrationResearchImport({
  allMyClients,
  connectClientId,
  scopedClientId,
  availableClients,
  onScopedClientId,
  clientName,
}: Props) {
  const clientId = connectClientId && connectClientId > 0 ? connectClientId : scopedClientId
  const [file, setFile] = useState<File | null>(null)
  const [batch, setBatch] = useState<ResearchImportBatch | null>(null)
  const [columns, setColumns] = useState<ColumnDraft[]>([])
  const [preview, setPreview] = useState<ResearchDryRun | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [worksheet, setWorksheet] = useState('')
  const [sheets, setSheets] = useState<string[]>([])
  const [sourceType, setSourceType] = useState('CHATGPT_DEEP_RESEARCH')
  const [sourceLabel, setSourceLabel] = useState('')
  const [sourceSuppliedBy, setSourceSuppliedBy] = useState('')
  const [researchMethod, setResearchMethod] = useState('')
  const [researchDate, setResearchDate] = useState('')
  const [templateName, setTemplateName] = useState('')
  const [appliedTemplateId, setAppliedTemplateId] = useState<number | null>(null)

  const draftMapping = useMemo(() => mappingFromColumns(columns), [columns])
  const mappingValidation = useMemo(
    () => validateResearchMapping(draftMapping, batch?.headers || []),
    [draftMapping, batch],
  )

  async function onUpload() {
    if (!file || clientId <= 0) return
    setBusy(true)
    setError(null)
    try {
      const result = await uploadResearchImport(clientId, file, {
        worksheet,
        research_method: researchMethod,
        research_date: researchDate,
        source_type: sourceType,
        source_label: sourceLabel,
        source_supplied_by: sourceSuppliedBy,
      })
      if (result.needs_worksheet) {
        setSheets(result.visible_sheets)
        setError(result.message)
        return
      }
      if (!result.batch) {
        setError(result.message || 'Upload failed.')
        return
      }
      setBatch(result.batch)
      setColumns(columnsFromBatch(result.batch))
      setAppliedTemplateId(null)
      setPreview(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed.')
    } finally {
      setBusy(false)
    }
  }

  async function onSaveAndPreview() {
    if (!batch || clientId <= 0) return
    if (!mappingValidation.ok) {
      setError(mappingValidation.errors[0] || 'Fix mapping first.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const saved = await saveResearchImportMapping(clientId, batch.batch_id, draftMapping, {
        research_method: researchMethod,
        research_date: researchDate,
        source_type: sourceType,
        source_label: sourceLabel,
        source_supplied_by: sourceSuppliedBy,
        mapping_template_id: appliedTemplateId,
        save_as_template: templateName,
      })
      setBatch(saved)
      setColumns(columnsFromBatch(saved))
      const plan = await dryRunResearchImport(clientId, saved.batch_id, 0, 50)
      setPreview(plan)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview failed.')
    } finally {
      setBusy(false)
    }
  }

  async function onResolve(row: ResearchPlanRow, resolution: string, companyId?: number | null) {
    if (!batch || clientId <= 0) return
    setBusy(true)
    setError(null)
    try {
      await saveResearchMatchResolution(clientId, batch.batch_id, row.row_id, resolution, companyId)
      const plan = await dryRunResearchImport(clientId, batch.batch_id, 0, 50)
      setPreview(plan)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Resolution failed.')
    } finally {
      setBusy(false)
    }
  }

  async function onResolveContact(row: ResearchPlanRow, resolution: string, contactId?: number | null) {
    if (!batch || clientId <= 0) return
    setBusy(true)
    setError(null)
    try {
      await saveResearchContactResolution(clientId, batch.batch_id, row.row_id, resolution, contactId)
      const plan = await dryRunResearchImport(clientId, batch.batch_id, 0, 50)
      setPreview(plan)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Contact resolution failed.')
    } finally {
      setBusy(false)
    }
  }

  function updateColumn(header: string, patch: Partial<ColumnDraft>) {
    setColumns((current) =>
      current.map((col) => {
        if (col.header !== header) return col
        const next = { ...col, ...patch }
        if (patch.ignored) {
          next.selected_field = ''
          next.selected_category = 'IGNORE'
        }
        if (patch.selected_category === 'CUSTOM_ATTRIBUTE' && !next.custom_label) {
          next.custom_label = next.header
          next.custom_key = normalizeAttributeKey(next.header)
        }
        if (patch.selected_category && patch.selected_category !== 'CUSTOM_ATTRIBUTE' && patch.selected_category !== 'IGNORE') {
          const stillValid = fieldsForCategory(patch.selected_category).some((f) => f.key === next.selected_field)
          if (!stillValid) next.selected_field = ''
        }
        return next
      }),
    )
  }

  function applySuggestedTemplate() {
    const hint = batch?.mapping_template_suggestion
    if (!hint?.mapping) return
    setColumns(columnsFromBatch({ ...batch!, mapping: hint.mapping }))
    setAppliedTemplateId(hint.template_id || null)
  }

  const counts = preview?.counts || {}
  const mappingDirty = batch ? !mappingsEqual(draftMapping, batch.mapping) : false
  const suggestion = batch?.mapping_template_suggestion
  const ignoredCount = draftMapping.columns.filter((col) => col.ignored).length
  const customCount = draftMapping.columns.filter((col) => col.category === 'CUSTOM_ATTRIBUTE').length

  return (
    <section aria-labelledby="research-import-heading">
      <h2 id="research-import-heading">Research & Custom Prospect Import</h2>
      <p className="queue-sub">
        Accepts AI research, client lists, trade-show and association files, purchased lists, and
        other custom CSV/XLSX. Separate from Company & contact import and LeadMaster Refresh.
        Production confirm is disabled.
      </p>
      {allMyClients ? (
        <label>
          Client
          <select
            value={scopedClientId}
            onChange={(event) => onScopedClientId(Number(event.target.value))}
          >
            <option value={0}>Select a client</option>
            {availableClients.map((client) => (
              <option key={client.client_id} value={client.client_id}>
                {client.client_name}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <p>Client: {clientName}</p>
      )}
      {clientId <= 0 ? <p role="status">Choose an existing NorthStar client. Clients are not created from the spreadsheet.</p> : null}
      <div>
        <label>
          Source type
          <select
            aria-label="Source type"
            value={sourceType}
            onChange={(event) => setSourceType(event.target.value)}
          >
            {RESEARCH_SOURCE_TYPES.map((item) => (
              <option key={item.code} value={item.code}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Source name
          <input
            aria-label="Source name"
            value={sourceLabel}
            onChange={(event) => setSourceLabel(event.target.value)}
            placeholder="Optional report or list name"
          />
        </label>
        <label>
          Supplied by
          <input
            aria-label="Supplied by"
            value={sourceSuppliedBy}
            onChange={(event) => setSourceSuppliedBy(event.target.value)}
          />
        </label>
        <label>
          Research method
          <select value={researchMethod} onChange={(event) => setResearchMethod(event.target.value)}>
            <option value="">(default for source type)</option>
            <option value="CHATGPT_DEEP_RESEARCH">CHATGPT_DEEP_RESEARCH</option>
            <option value="AI_RESEARCH">AI_RESEARCH</option>
            <option value="CLIENT_RESEARCH">CLIENT_RESEARCH</option>
            <option value="INTERNAL_RESEARCH">INTERNAL_RESEARCH</option>
            <option value="LIST_IMPORT">LIST_IMPORT</option>
            <option value="MANUAL_RESEARCH">MANUAL_RESEARCH</option>
            <option value="OTHER">OTHER</option>
          </select>
        </label>
        <label>
          Research / received date
          <input value={researchDate} onChange={(event) => setResearchDate(event.target.value)} />
        </label>
        <input
          aria-label="Research workbook"
          type="file"
          accept=".csv,.xlsx"
          onChange={(event) => setFile(event.target.files?.[0] || null)}
        />
        {sheets.length > 1 ? (
          <label>
            Worksheet
            <select value={worksheet} onChange={(event) => setWorksheet(event.target.value)}>
              <option value="">Choose sheet</option>
              {sheets.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <button type="button" onClick={() => void onUpload()} disabled={busy || !file || clientId <= 0}>
          Upload
        </button>
      </div>
      {error ? <p role="alert">{error}</p> : null}
      {batch ? (
        <div>
          <h3>Map Fields</h3>
          <p>
            Source: {batch.original_filename} ({batch.source_row_count} rows). SHA {batch.sha256.slice(0, 12)}…
            Headers do not need to match the NorthStar template.
          </p>
          {suggestion?.template_name ? (
            <div role="status">
              <p>
                Mapping template suggested: {suggestion.template_name} (
                {suggestion.header_compatibility?.confidence || 'review'}
                {suggestion.review_required ? ', review required' : ', compatible'})
              </p>
              {suggestion.header_compatibility?.missing_headers?.length ? (
                <p>Changed / missing columns: {suggestion.header_compatibility.missing_headers.join(', ')}</p>
              ) : null}
              {suggestion.header_compatibility?.new_headers?.length ? (
                <p>New columns: {suggestion.header_compatibility.new_headers.join(', ')}</p>
              ) : null}
              {suggestion.safety_class === 'workflow_sensitive' ? (
                <p>Workflow-sensitive mapping. Review before reuse; it will not overwrite CRM status.</p>
              ) : null}
              <button type="button" onClick={applySuggestedTemplate} disabled={busy}>
                Use suggested mapping
              </button>
              {appliedTemplateId ? <p>Mapping template applied for review. Validate before continuing.</p> : null}
            </div>
          ) : null}
          <table>
            <thead>
              <tr>
                <th>Source column</th>
                <th>Sample values</th>
                <th>Suggested destination</th>
                <th>Category</th>
                <th>Selected destination</th>
                <th>Custom attribute</th>
                <th>Ignore</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((col) => (
                <tr key={col.header}>
                  <td>{col.header}</td>
                  <td>{col.samples.join(' · ') || '—'}</td>
                  <td>{col.suggested_field || 'none'}</td>
                  <td>
                    <select
                      aria-label={`Category for ${col.header}`}
                      value={col.ignored ? 'IGNORE' : col.selected_category}
                      onChange={(event) =>
                        updateColumn(col.header, {
                          selected_category: event.target.value,
                          ignored: event.target.value === 'IGNORE',
                        })
                      }
                    >
                      <option value="">(choose)</option>
                      {RESEARCH_CATEGORIES.map((cat) => (
                        <option key={cat.code} value={cat.code}>
                          {cat.label}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      aria-label={`Destination for ${col.header}`}
                      value={col.selected_field}
                      disabled={col.ignored || col.selected_category === 'CUSTOM_ATTRIBUTE' || col.selected_category === 'IGNORE'}
                      onChange={(event) => updateColumn(col.header, { selected_field: event.target.value })}
                    >
                      <option value="">(not mapped)</option>
                      {fieldsForCategory(col.selected_category).map((field) => (
                        <option key={field.key} value={field.key}>
                          {field.label}
                        </option>
                      ))}
                    </select>
                    {col.workflow_sensitive || col.selected_category === 'CRM_WORKFLOW' ? (
                      <p>{col.warning || 'Workflow-sensitive. Preview only; CRM status is not changed.'}</p>
                    ) : null}
                  </td>
                  <td>
                    {col.selected_category === 'CUSTOM_ATTRIBUTE' ? (
                      <div>
                        <input
                          aria-label={`Custom attribute label for ${col.header}`}
                          value={col.custom_label}
                          onChange={(event) =>
                            updateColumn(col.header, {
                              custom_label: event.target.value,
                              custom_key: normalizeAttributeKey(event.target.value || col.header),
                            })
                          }
                        />
                        <select
                          aria-label={`Custom attribute type for ${col.header}`}
                          value={col.value_type}
                          onChange={(event) => updateColumn(col.header, { value_type: event.target.value })}
                        >
                          {VALUE_TYPES.map((kind) => (
                            <option key={kind} value={kind}>
                              {kind}
                            </option>
                          ))}
                        </select>
                        <p>Key: {col.custom_key || normalizeAttributeKey(col.custom_label || col.header)}</p>
                      </div>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    <label>
                      <input
                        type="checkbox"
                        aria-label={`Ignore ${col.header}`}
                        checked={col.ignored}
                        onChange={(event) =>
                          updateColumn(col.header, {
                            ignored: event.target.checked,
                            selected_category: event.target.checked ? 'IGNORE' : '',
                          })
                        }
                      />
                      Ignore
                    </label>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            Ignored columns: {ignoredCount}. Custom attributes: {customCount}. Ignored fields stay
            visible here and are not stored as notes.
          </p>
          <label>
            Save mapping template as
            <input
              aria-label="Save mapping template as"
              value={templateName}
              onChange={(event) => setTemplateName(event.target.value)}
              placeholder="Optional, e.g. Janco Monthly Prospect Report"
            />
          </label>
          <button type="button" onClick={() => void onSaveAndPreview()} disabled={busy || !mappingValidation.ok}>
            Validate and preview
          </button>
          {mappingDirty ? <p>Mapping has unsaved changes.</p> : null}
        </div>
      ) : null}
      {preview ? (
        <div>
          <h3>Ready / Blocking Summary</h3>
          <ul>
            <li>Source rows: {counts.source_rows ?? 0}</li>
            <li>Mapped fields: {Object.keys(draftMapping.fields).length}</li>
            <li>Ignored fields: {preview.ignored_headers?.length ?? counts.ignored_fields ?? 0}</li>
            <li>Custom attributes: {counts.custom_attribute_values ?? 0}</li>
            <li>Contacts present: {counts.contacts_present ?? 0}</li>
            <li>Exact contacts: {counts.contacts_exact ?? 0}</li>
            <li>Strong contacts: {counts.contacts_strong ?? 0}</li>
            <li>New contacts: {counts.contacts_new ?? 0}</li>
            <li>Possible contacts: {counts.contacts_possible ?? 0}</li>
            <li>Ambiguous contacts: {counts.contacts_ambiguous ?? 0}</li>
            <li>Contact review blocking: {counts.contacts_blocking ?? 0}</li>
            <li>Existing companies: {counts.existing_companies ?? 0}</li>
            <li>New companies: {counts.new_companies ?? 0}</li>
            <li>New locations: {counts.new_locations ?? 0}</li>
            <li>Possible matches: {counts.possible_matches ?? 0}</li>
            <li>Ambiguous: {counts.ambiguous ?? 0}</li>
            <li>Invalid: {counts.invalid ?? 0}</li>
            <li>Existing CCRs: {counts.existing_ccrs ?? 0}</li>
            <li>New CCRs: {counts.new_ccrs ?? 0}</li>
            <li>Research rows to add: {counts.research_rows_to_add ?? 0}</li>
            <li>Master fills: {counts.master_fills ?? 0}</li>
            <li>Master proposed updates: {counts.master_proposed_updates ?? 0}</li>
            <li>Workflow fields (preview only): {counts.workflow_fields ?? 0}</li>
            <li>Explicit notes (append on isolated confirm; live confirm not enabled): {counts.explicit_notes ?? 0}</li>
            <li>Sources captured: {counts.sources_captured ?? 0}</li>
            <li>Blocking rows: {counts.blocking_rows ?? 0}</li>
            <li>Same-batch research conflicts: {counts.same_batch_conflicts ?? preview.forecast?.same_batch_conflicts ?? 0}</li>
            <li>Research records this batch: one per company ({preview.forecast?.research_created ?? counts.research_rows_to_add ?? 0})</li>
            <li>Will create companies: {preview.forecast?.companies_created ?? counts.new_companies ?? 0}</li>
            <li>Will reuse companies: {preview.forecast?.companies_reused ?? counts.existing_companies ?? 0}</li>
            <li>Will add locations: {preview.forecast?.locations_created ?? counts.new_locations ?? 0}</li>
            <li>Will create contacts: {preview.forecast?.contacts_created ?? counts.contacts_new ?? 0}</li>
            <li>Will reuse contacts: {preview.forecast?.contacts_reused ?? 0}</li>
            <li>Will skip contacts: {preview.forecast?.contacts_skipped ?? 0}</li>
            <li>Notes to append: {preview.forecast?.notes_append ?? 0}</li>
            <li>Notes to dedupe: {preview.forecast?.notes_dedupe ?? 0}</li>
            <li>Research records to version: {preview.forecast?.research_created ?? counts.research_rows_to_add ?? 0}</li>
            <li>Attributes to version: {preview.forecast?.attributes_created ?? counts.custom_attribute_values ?? 0}</li>
            <li>Master fields accepted/preserved: FILL {preview.forecast?.master_fill_blank ?? 0} / ACCEPT {preview.forecast?.master_accept_incoming ?? 0} / KEEP {preview.forecast?.master_keep_existing ?? 0}</li>
            <li>Workflow fields will NOT be written: {preview.workflow_fields_will_write ?? 0}</li>
            <li>
              Source lineage — aliases (not master-field changes): create{' '}
              {preview.forecast?.aliases_created ?? 0} / reuse {preview.forecast?.aliases_deduped ?? 0}
            </li>
            <li>
              Source lineage — source identities: create {preview.forecast?.identities_created ?? 0} / reuse{' '}
              {preview.forecast?.identities_deduped ?? 0}
            </li>
          </ul>
          {(preview.lineage?.length || preview.write_summary?.some((item) => item.section === 'Source Lineage')) ? (
            <details>
              <summary>Aliases &amp; Source Identities (lineage, not master-field changes)</summary>
              <p>Confirming this batch will add these RESEARCH_IMPORT lineage records. They do not update master company fields.</p>
              <ul>
                {(preview.lineage || preview.rows.map((row) => row.lineage).filter(Boolean)).map((item, index) => (
                  <li key={`${item?.row_id || index}-${item?.source_identity?.source_record_no || index}`}>
                    Row {item?.source_row}: {item?.company_name || 'unnamed'}
                    {item?.matched_company_id ? ` (company ${item.matched_company_id})` : ''} — alias{' '}
                    {item?.alias?.action || 'SKIP'}
                    {item?.alias?.alias_norm ? ` “${item.alias.alias_norm}”` : ''} / identity{' '}
                    {item?.source_identity?.action || 'SKIP'}
                    {item?.source_identity?.source_record_no ? ` ${item.source_identity.source_record_no}` : ''}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          {preview.batch_caveat ? (
            <p>
              Batch caveat (not duplicated onto every company note): {preview.batch_caveat}
            </p>
          ) : null}
          <p role="status">Live confirmation not enabled</p>
          <button type="button" disabled>
            Confirm (live confirmation not enabled)
          </button>
          <h3>Match Review</h3>
          {preview.rows.map((row) => (
            <article key={row.row_id}>
              <h4>
                {classLabel(row.ri_class)} — {row.company_name}
              </h4>
              <p>
                Incoming: {row.address}, {row.city} {row.state} {row.zip} · {row.phone} · {row.website}
              </p>
              {row.matched_company_name ? (
                <p>
                  NorthStar: {row.matched_company_name} #{row.matched_company_id} · {row.matched_address},{' '}
                  {row.matched_city} {row.matched_state} · {row.matched_phone} · {row.matched_website}
                </p>
              ) : null}
              <p>Match reasons: {row.matcher_reasons.join(', ') || 'none'}</p>
              {row.blocking ? <p>Blocking reason: {row.blocking_reasons.join('; ')}</p> : null}
              {row.same_batch_conflicts && row.same_batch_conflicts.length ? (
                <p>
                  Same-batch conflict: {row.same_batch_conflicts.map((item) => String(item.field || item.class || '')).join(', ')}
                </p>
              ) : null}
              {row.possibles.length ? (
                <ul>
                  {row.possibles.map((item) => (
                    <li key={`${item.company_id}-${item.name}`}>
                      {item.name} #{item.company_id} ({item.reasons.join(', ')}) {item.city} {item.state}
                      {row.allowed_resolutions.includes('USE_EXISTING') && item.company_id ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void onResolve(row, 'USE_EXISTING', item.company_id)}
                        >
                          USE_EXISTING: {item.name}
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              <h5>Research preview</h5>
              <p>Target Market: {(row.attributes.target_market || []).join('; ') || '—'}</p>
              <p>Equipment / Products: {(row.attributes.equipment_product || []).join('; ') || '—'}</p>
              <p>Why Client Fits: {row.why_client_fits || '—'}</p>
              <p>Potential Components: {(row.attributes.potential_component || []).join('; ') || '—'}</p>
              <p>
                Research Priority: {row.research_priority_code} {row.research_priority_label}
              </p>
              <p>Target Department: {(row.attributes.target_department || []).join('; ') || '—'}</p>
              <p>Qualification narrative: {row.qualification_notes || '—'}</p>
              {row.custom_attributes?.length ? (
                <p>
                  Custom attributes:{' '}
                  {row.custom_attributes
                    .map((item) => `${item.label || item.key}=${item.original_value || item.text_value}`)
                    .join('; ')}
                </p>
              ) : null}
              {row.workflow_fields && Object.keys(row.workflow_fields).length ? (
                <p>
                  Workflow fields (not applied):{' '}
                  {Object.entries(row.workflow_fields)
                    .map(([key, value]) => `${key}=${value}`)
                    .join('; ')}
                </p>
              ) : null}
              {row.notes_explicit ? <p>Explicit notes (append on isolated confirm; live confirm not enabled): {row.notes_explicit}</p> : null}
              {row.ignored_fields?.length ? <p>Ignored: {row.ignored_fields.join(', ')}</p> : null}
              <ul>
                {row.sources.map((source) => (
                  <li key={`${source.source_role}-${source.source_url}`}>
                    {source.source_role}: {source.source_url}
                  </li>
                ))}
              </ul>
              {row.conflicts.length ? (
                <div>
                  <h5>Master data proposals</h5>
                  <ul>
                    {row.conflicts.map((conflict) => (
                      <li key={conflict.field}>
                        {conflict.field}: {conflict.class}. NorthStar={conflict.current || '(blank)'} /
                        Research={conflict.incoming || '(blank)'}
                        {conflict.authority ? ` (${conflict.authority})` : ''}
                        {conflict.provenance?.length ? ` [${conflict.provenance.join('; ')}]` : ''}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {row.blocking ? (
                <div>
                  <p>Blocking: {row.blocking_reasons.join('; ')}</p>
                  {row.allowed_resolutions
                    .filter((action) => action !== 'USE_EXISTING' || (!row.possibles.length && row.matched_company_id))
                    .map((action) => (
                    <button
                      key={action}
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void onResolve(
                          row,
                          action,
                          action === 'USE_EXISTING' || action === 'TREAT_AS_NEW_LOCATION'
                            ? row.matched_company_id
                            : null,
                        )
                      }
                    >
                      {action}
                    </button>
                  ))}
                </div>
              ) : null}
            </article>
          ))}
          <h3>Contact Review</h3>
          {preview.rows.some((row) => row.contacts?.length) ? (
            preview.rows.flatMap((row) =>
              (row.contacts || []).map((contact, index) => {
                const incoming = contact.incoming ?? {
                  first_name: contact.first_name,
                  last_name: contact.last_name,
                  full_name: contact.full_name,
                  title: contact.title,
                  email: contact.email,
                  phone: contact.phone,
                  phone_extension: contact.phone_extension,
                }
                const incomingName =
                  incoming.full_name || `${incoming.first_name || ''} ${incoming.last_name || ''}`.trim()
                return (
                  <article key={`${row.row_id}-${index}`}>
                    <h4>
                      {contactLabel(contact.contact_class || '')} — {incomingName || 'Unnamed'}
                    </h4>
                    <p>Company: {row.company_name}</p>
                    <p>
                      Incoming contact: {incomingName} · {incoming.title || 'no title'} ·{' '}
                      {incoming.email || 'no email'} · {incoming.phone || 'no phone'}
                      {incoming.phone_extension ? ` x${incoming.phone_extension}` : ''}
                    </p>
                    <p>
                      Matched NorthStar contact:{' '}
                      {contact.matched_name
                        ? `${contact.matched_name} #${contact.matched_contact_id || ''} · ${contact.matched_email || 'no email'} · ${contact.matched_phone || 'no phone'} · ${contact.matched_title || 'no title'} · ${contact.matched_company_name || row.matched_company_name || ''}`
                        : 'none'}
                    </p>
                    <p>Match reason: {(contact.reasons || []).join(', ') || 'none'}</p>
                    <p>Classification: {contact.contact_class || 'none'}</p>
                    {contact.blocking ? <p>Blocking reason: {contact.blocking_reason || contact.contact_class}</p> : null}
                    <p>Resolution: {contact.resolution || 'none'}</p>
                    {(contact.allowed_resolutions || [])
                      .filter((action) => action !== contact.resolution)
                      .map((action) => (
                        <button
                          key={action}
                          type="button"
                          disabled={busy}
                          onClick={() => void onResolveContact(row, action, contact.matched_contact_id)}
                        >
                          {action}
                        </button>
                      ))}
                  </article>
                )
              }),
            )
          ) : (
            <p>No named contacts in this preview.</p>
          )}
        </div>
      ) : null}
    </section>
  )
}
