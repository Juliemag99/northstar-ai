import { Link, useNavigate } from 'react-router-dom'
import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import {
  archiveClientDocument,
  approveEmailTemplateGroup,
  bulkRejectExtractions,
  bulkResolveExtractions,
  bulkReviewExtractions,
  classifyEngagementSheets,
  connectEmailAccount,
  createClientContact,
  createEmailAccount,
  createEmailAccountAssignment,
  createEmailSignature,
  createSplitClientContactProposals,
  deactivateClientContact,
  deactivateEmailAccount,
  disconnectEmailAccount,
  fetchClientKnowledge,
  fetchEmailAccountSuggestions,
  fetchEngagementPreview,
  fetchGoogleOAuthStatus,
  fetchIncorporationAnalysis,
  fetchReviewClassifications,
  listEmailAccounts,
  listEmailPreviewAppointments,
  listEmailPreviewContacts,
  listEmailSignatures,
  listExtractionProposals,
  mapEngagementImport,
  previewBulkResolve,
  previewClientEmail,
  previewEmailTemplateGroup,
  previewSplitClientContacts,
  processClientDocument,
  resolveExtractionProposal,
  reviewExtractionProposal,
  fetchKnowledgeSectionCatalog,
  startEngagementImport,
  updateClientContact,
  updateEmailAccount,
  updateEmailSignature,
  updateEmailTemplate,
  updateKnowledgeField,
  uploadClientDocument,
  fetchClientSalesEvents,
  type BulkRejectResult,
  type BulkResolvePreview,
  type BulkResolveResult,
  type ClientContact,
  type ClientContactSplitPreview,
  type ClientDocument,
  type ClientEmailAccount,
  type ClientEmailAccountSuggestion,
  type ClientEmailPreviewResult,
  type ClientEmailSignature,
  type ClientEmailTemplate,
  type ClientExtractionProposal,
  type ClientKnowledgeHub,
  type EmailPreviewAppointment,
  type EmailPreviewContact,
  type EmailTemplateGroupPreview,
  type EngagementImportBatch,
  type EngagementImportPreview,
  type EngagementImportRow,
  type ExtractionReviewClassification,
  type ProposalIncorporationAnalysis,
} from './api/carmeco'

type ReviewViewFilter =
  | 'Pending'
  | 'Already Incorporated'
  | 'Needs Review'
  | 'Noise'
  | 'Sensitive'
  | 'Resolved'
  | 'Rejected'
  | 'All'

const REVIEW_CLASS_FILTERS: ReviewViewFilter[] = [
  'Already Incorporated',
  'Needs Review',
  'Noise',
  'Sensitive',
]

const REVIEW_CLASS_MAP: Record<string, string> = {
  'Already Incorporated': 'already_incorporated',
  'Needs Review': 'needs_review',
  Noise: 'noise',
  Sensitive: 'sensitive',
}

const REJECT_CATEGORIES = [
  'Noise / Bad Extraction',
  'Sensitive — Do Not Import',
  'Duplicate Invalid Extraction',
  'Other',
] as const

function display(value: string | null | undefined): string {
  const t = (value || '').trim()
  return t || '—'
}

const TABS = [
  { id: 'profile', label: 'Client Profile' },
  { id: 'strategy', label: 'Strategy' },
  { id: 'playbook', label: 'Road Map / Call Playbook' },
  { id: 'operations', label: 'Client Operations' },
  { id: 'contacts', label: 'Client Contacts' },
  { id: 'appointments', label: 'Appointments' },
  { id: 'templates', label: 'Email Templates' },
  { id: 'documents', label: 'Documents' },
  { id: 'review', label: 'Strategy Import Review' },
] as const

type TabId = (typeof TABS)[number]['id']

type KnowledgeCatalogSection = {
  section_key: string
  display_name: string
  fields: Array<{ field_name: string; field_label: string }>
}

const FALLBACK_OPS_FIELDS = [
  { field_name: 'northstar_client_email', field_label: 'NorthStar Client Email' },
  {
    field_name: 'northstar_revenue_specialist',
    field_label: 'NorthStar Revenue Specialist',
  },
  { field_name: 'who_takes_appointments', field_label: 'Who Takes Appointments' },
  {
    field_name: 'appointment_handling_instructions',
    field_label: 'Appointment Handling Instructions',
  },
  { field_name: 'appointment_recap_cc', field_label: 'Appointment Recap CC' },
  { field_name: 'reporting_instructions', field_label: 'Reporting Instructions' },
  { field_name: 'lead_handoff_procedures', field_label: 'Lead Handoff Procedures' },
  {
    field_name: 'communication_procedures',
    field_label: 'Client Communication Procedures',
  },
  { field_name: 'workflow_instructions', field_label: 'Internal Workflow Instructions' },
  { field_name: 'other_operational_notes', field_label: 'Other Operational Notes' },
]

const OPS_FIELD_ALIASES: Record<string, string> = {
  primary_owner_name: 'northstar_revenue_specialist',
  who_takes_appointments: 'who_takes_appointments',
  appointment_instructions: 'appointment_handling_instructions',
}

function sectionPayload(hub: ClientKnowledgeHub | null, key: string): Record<string, unknown> {
  const sec = hub?.sections.find((s) => s.section_key === key)
  return (sec?.payload || {}) as Record<string, unknown>
}

function operationsItems(hub: ClientKnowledgeHub | null): Array<Record<string, unknown>> {
  const payload = sectionPayload(hub, 'client_operations')
  const items = payload.items
  if (!Array.isArray(items)) return []
  return items.filter((it): it is Record<string, unknown> => !!it && typeof it === 'object')
}

function contactsLinkedInText(
  text: string,
  contacts: ClientContact[],
): ClientContact[] {
  const blob = (text || '').toLowerCase()
  if (!blob) return []
  return contacts.filter((c) => {
    if (!c.active) return false
    const name = (c.name || '').trim()
    if (name.length < 3) return false
    if (blob.includes(name.toLowerCase())) return true
    const parts = name.split(/\s+/).filter(Boolean)
    if (parts.length >= 2) {
      return (
        blob.includes(parts[0].toLowerCase()) &&
        blob.includes(parts[parts.length - 1].toLowerCase())
      )
    }
    return false
  })
}

const EMPTY_CONTACT_FORM = {
  name: '',
  title: '',
  email: '',
  phone: '',
  role_type: '',
  notes: '',
}

export default function ClientKnowledge({ clientId }: { clientId: number }) {
  const navigate = useNavigate()
  const [hub, setHub] = useState<ClientKnowledgeHub | null>(null)
  const [tab, setTab] = useState<TabId>('documents')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [docType, setDocType] = useState('Other')
  const [uploading, setUploading] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)

  const [engBatch, setEngBatch] = useState<EngagementImportBatch | null>(null)
  const [engPreview, setEngPreview] = useState<EngagementImportPreview | null>(null)
  const [sheetTypes, setSheetTypes] = useState<Record<number, string>>({})
  const [previewFilter, setPreviewFilter] = useState('')
  const [detailRow, setDetailRow] = useState<EngagementImportRow | null>(null)
  const [importedEvents, setImportedEvents] = useState<Array<Record<string, unknown>>>([])
  const [selectedProposalIds, setSelectedProposalIds] = useState<number[]>([])
  const [editProposalId, setEditProposalId] = useState<number | null>(null)
  const [editValue, setEditValue] = useState('')
  const [editSection, setEditSection] = useState('')
  const [editField, setEditField] = useState('')
  const [editTitle, setEditTitle] = useState('')
  const [sectionCatalog, setSectionCatalog] = useState<KnowledgeCatalogSection[]>([])
  const [contactForm, setContactForm] = useState({ ...EMPTY_CONTACT_FORM })
  const [editingContactId, setEditingContactId] = useState<number | null>(null)
  const [showInactiveContacts, setShowInactiveContacts] = useState(false)
  const [splitPreview, setSplitPreview] = useState<ClientContactSplitPreview | null>(null)
  const [splitBusy, setSplitBusy] = useState(false)
  const [reviewStatusFilter, setReviewStatusFilter] = useState<ReviewViewFilter>('Pending')
  const [reviewProposals, setReviewProposals] = useState<ClientExtractionProposal[]>([])
  const [reviewClassById, setReviewClassById] = useState<
    Record<number, ExtractionReviewClassification>
  >({})
  const [resolveProposalId, setResolveProposalId] = useState<number | null>(null)
  const [resolveReason, setResolveReason] = useState('')
  const [resolveChildIds, setResolveChildIds] = useState('')
  const [resolveContactIds, setResolveContactIds] = useState('')
  const [resolveChildLabels, setResolveChildLabels] = useState<string[]>([])
  const [resolveContactLabels, setResolveContactLabels] = useState<string[]>([])
  const [resolveBusy, setResolveBusy] = useState(false)
  const [incorporationById, setIncorporationById] = useState<
    Record<number, ProposalIncorporationAnalysis>
  >({})
  const [bulkResolvePreview, setBulkResolvePreview] = useState<BulkResolvePreview | null>(null)
  const [bulkRejectOpen, setBulkRejectOpen] = useState(false)
  const [rejectCategory, setRejectCategory] = useState<string>('')
  const [rejectNote, setRejectNote] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkSummary, setBulkSummary] = useState<
    | { kind: 'resolve'; result: BulkResolveResult }
    | { kind: 'reject'; result: BulkRejectResult }
    | null
  >(null)
  const [showSkippedDetails, setShowSkippedDetails] = useState(false)
  const [knowledgeDrafts, setKnowledgeDrafts] = useState<Record<string, string>>({})
  const [knowledgeSaveBusy, setKnowledgeSaveBusy] = useState(false)
  const [templatePreview, setTemplatePreview] = useState<EmailTemplateGroupPreview | null>(null)
  const [templateSeedId, setTemplateSeedId] = useState<number | null>(null)
  const [templateLoading, setTemplateLoading] = useState(false)
  const [templateInlineError, setTemplateInlineError] = useState<string | null>(null)
  const [templateDraft, setTemplateDraft] = useState({
    template_name: '',
    template_type: '',
    subject: '',
    body: '',
    is_active: true,
    acknowledge_name_conflicts: false,
  })
  const [templateBusy, setTemplateBusy] = useState(false)
  const [editingTemplateId, setEditingTemplateId] = useState<number | null>(null)
  const [templateEditDraft, setTemplateEditDraft] = useState({
    template_name: '',
    template_type: '',
    subject: '',
    body: '',
    is_active: true,
  })
  const [emailAccounts, setEmailAccounts] = useState<ClientEmailAccount[]>([])
  const [emailSuggestion, setEmailSuggestion] = useState<ClientEmailAccountSuggestion | null>(null)
  const [emailSignatures, setEmailSignatures] = useState<ClientEmailSignature[]>([])
  const [emailBusy, setEmailBusy] = useState(false)
  const [showAddSender, setShowAddSender] = useState(false)
  const [senderDraft, setSenderDraft] = useState({
    email_address: '',
    display_name: '',
    provider: 'Other',
    is_default: true,
    source_rep_name: '',
  })
  const [editingAccountId, setEditingAccountId] = useState<number | null>(null)
  const [sigDraft, setSigDraft] = useState({
    signature_name: '',
    signature_body: '',
    source_rep_name: '',
    email_account_id: '' as string | number,
    is_default: true,
  })
  const [editingSignatureId, setEditingSignatureId] = useState<number | null>(null)
  const [sigEditDraft, setSigEditDraft] = useState({
    signature_name: '',
    signature_body: '',
    source_rep_name: '',
    email_account_id: '' as string | number,
    is_default: true,
  })
  const [previewContacts, setPreviewContacts] = useState<EmailPreviewContact[]>([])
  const [previewAppointments, setPreviewAppointments] = useState<EmailPreviewAppointment[]>(
    [],
  )
  const [previewSel, setPreviewSel] = useState({
    account_id: '',
    contact_id: '',
    template_id: '',
    appointment_key: '',
  })
  const [emailPreview, setEmailPreview] = useState<ClientEmailPreviewResult | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)

  const fieldLabel = useCallback(
    (sectionKey: string, fieldName: string) => {
      const sec = sectionCatalog.find((s) => s.section_key === sectionKey)
      const hit = sec?.fields.find((f) => f.field_name === fieldName)
      return hit?.field_label || fieldName.replace(/_/g, ' ')
    },
    [sectionCatalog],
  )

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await fetchClientKnowledge(clientId)
      setHub(next)
      // Prefer dedicated email-accounts API; fall back to hub payload.
      // Load each resource independently so one 404 cannot wipe sender accounts.
      const hubAccounts = Array.isArray(next.email_accounts)
        ? (next.email_accounts as ClientEmailAccount[])
        : []
      if (hubAccounts.length > 0) {
        setEmailAccounts(hubAccounts)
      }
      try {
        const accounts = await listEmailAccounts(clientId)
        setEmailAccounts(accounts)
      } catch {
        if (hubAccounts.length === 0) setEmailAccounts([])
      }
      try {
        setEmailSuggestion(await fetchEmailAccountSuggestions(clientId))
      } catch {
        setEmailSuggestion(null)
      }
      try {
        setEmailSignatures(await listEmailSignatures(clientId))
      } catch {
        setEmailSignatures([])
      }
      try {
        const events = await fetchClientSalesEvents(clientId, { limit: 100 })
        setImportedEvents(events)
      } catch {
        setImportedEvents([])
      }
      try {
        const catalog = await fetchKnowledgeSectionCatalog()
        setSectionCatalog(catalog)
      } catch {
        setSectionCatalog([
          {
            section_key: 'client_operations',
            display_name: 'Client Operations',
            fields: FALLBACK_OPS_FIELDS,
          },
          {
            section_key: 'unmapped',
            display_name: 'Unmapped Intelligence',
            fields: [{ field_name: 'unmapped_intelligence', field_label: 'Unmapped Intelligence' }],
          },
        ])
      }
      try {
        let filtered: ClientExtractionProposal[]
        if (REVIEW_CLASS_FILTERS.includes(reviewStatusFilter)) {
          const pending = await listExtractionProposals(clientId, 'Pending')
          const classes = await fetchReviewClassifications(clientId)
          const map: Record<number, ExtractionReviewClassification> = {}
          for (const c of classes) map[c.proposal_id] = c
          setReviewClassById(map)
          const want = REVIEW_CLASS_MAP[reviewStatusFilter]
          filtered = pending.filter((p) => map[p.proposal_id]?.review_class === want)
        } else {
          filtered = await listExtractionProposals(
            clientId,
            reviewStatusFilter as 'Pending' | 'Approved' | 'Rejected' | 'Resolved' | 'All',
          )
          if (reviewStatusFilter === 'Pending' || reviewStatusFilter === 'All') {
            try {
              const classes = await fetchReviewClassifications(clientId)
              const map: Record<number, ExtractionReviewClassification> = {}
              for (const c of classes) map[c.proposal_id] = c
              setReviewClassById(map)
            } catch {
              setReviewClassById({})
            }
          } else {
            setReviewClassById({})
          }
        }
        setReviewProposals(filtered)
      } catch {
        setReviewProposals(next.extraction_proposals || [])
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load Client Knowledge.')
      setHub(null)
    } finally {
      setLoading(false)
    }
  }, [clientId, reviewStatusFilter])

  async function onSaveKnowledgeField(sectionKey: string, fieldName: string) {
    const key = `${sectionKey}.${fieldName}`
    const value = knowledgeDrafts[key]
    if (value === undefined) return
    setKnowledgeSaveBusy(true)
    setError(null)
    try {
      await updateKnowledgeField(clientId, sectionKey, fieldName, value)
      setMsg(`Saved ${fieldLabel(sectionKey, fieldName)}.`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save knowledge field failed.')
    } finally {
      setKnowledgeSaveBusy(false)
    }
  }

  function cancelTemplateEditor() {
    setTemplateSeedId(null)
    setTemplatePreview(null)
    setTemplateLoading(false)
    setTemplateInlineError(null)
    setTemplateBusy(false)
    setTemplateDraft({
      template_name: '',
      template_type: '',
      subject: '',
      body: '',
      is_active: true,
      acknowledge_name_conflicts: false,
    })
  }

  async function onBuildTemplate(seedProposalId: number) {
    // Same logical group already open — move the inline editor under this row
    if (
      templatePreview &&
      templatePreview.source_proposal_ids.includes(seedProposalId) &&
      !templateLoading
    ) {
      setTemplateSeedId(seedProposalId)
      setTemplateInlineError(null)
      return
    }

    // Open immediately under the clicked row (same pattern as Resolve)
    setTemplateSeedId(seedProposalId)
    setTemplatePreview(null)
    setTemplateInlineError(null)
    setTemplateLoading(true)
    setTemplateBusy(true)
    setResolveProposalId(null)
    try {
      const preview = await previewEmailTemplateGroup(clientId, [seedProposalId])
      setTemplatePreview(preview)
      setTemplateDraft({
        template_name: preview.template_name,
        template_type: preview.template_type,
        subject: preview.subject,
        body: preview.body,
        is_active: true,
        acknowledge_name_conflicts: false,
      })
    } catch (err) {
      setTemplateInlineError(
        err instanceof Error ? err.message : 'Build Template preview failed.',
      )
    } finally {
      setTemplateLoading(false)
      setTemplateBusy(false)
    }
  }

  async function onApproveTemplate() {
    if (!templatePreview) return
    if (templatePreview.name_conflicts.length > 0 && !templateDraft.acknowledge_name_conflicts) {
      setTemplateInlineError(
        'Acknowledge the potential contact name conflict(s) after reviewing/editing the body before approving.',
      )
      return
    }
    if (!templateDraft.template_name.trim() || !templateDraft.body.trim()) {
      setTemplateInlineError('Template name and body are required.')
      return
    }
    setTemplateBusy(true)
    setTemplateInlineError(null)
    try {
      const result = await approveEmailTemplateGroup(clientId, {
        proposal_ids: templatePreview.source_proposal_ids,
        template_name: templateDraft.template_name,
        template_type: templateDraft.template_type,
        subject: templateDraft.subject,
        body: templateDraft.body,
        is_active: templateDraft.is_active,
        acknowledge_name_conflicts: templateDraft.acknowledge_name_conflicts,
      })
      cancelTemplateEditor()
      setMsg(result.message)
      await reload()
    } catch (err) {
      setTemplateInlineError(err instanceof Error ? err.message : 'Approve template failed.')
    } finally {
      setTemplateBusy(false)
    }
  }

  function openTemplateEditor(t: ClientEmailTemplate) {
    setEditingTemplateId(t.template_id)
    setTemplateEditDraft({
      template_name: t.template_name,
      template_type: t.template_type,
      subject: t.subject,
      body: t.body,
      is_active: t.is_active,
    })
  }

  async function onSaveTemplateEdit() {
    if (editingTemplateId == null) return
    setTemplateBusy(true)
    setError(null)
    try {
      await updateEmailTemplate(clientId, editingTemplateId, templateEditDraft)
      setEditingTemplateId(null)
      setMsg('Email template saved.')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save template failed.')
    } finally {
      setTemplateBusy(false)
    }
  }

  async function onDeactivateTemplate(t: ClientEmailTemplate) {
    setTemplateBusy(true)
    setError(null)
    try {
      await updateEmailTemplate(clientId, t.template_id, {
        template_name: t.template_name,
        template_type: t.template_type,
        subject: t.subject,
        body: t.body,
        is_active: false,
      })
      setMsg(`Deactivated ${t.template_name}.`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Deactivate template failed.')
    } finally {
      setTemplateBusy(false)
    }
  }

  function applySenderSuggestion() {
    if (!emailSuggestion) return
    setShowAddSender(true)
    setSenderDraft({
      email_address: emailSuggestion.suggested_email_address || '',
      display_name: '',
      provider: emailSuggestion.suggested_provider || 'Other',
      is_default: true,
      source_rep_name: emailSuggestion.suggested_source_rep_name || '',
    })
  }

  async function onCreateSenderAccount() {
    const email = senderDraft.email_address.trim()
    if (!email) {
      setError('Email address is required.')
      return
    }
    setEmailBusy(true)
    setError(null)
    try {
      const acct = await createEmailAccount(clientId, {
        email_address: email,
        display_name: senderDraft.display_name.trim(),
        provider: senderDraft.provider,
        is_default: senderDraft.is_default,
        active: true,
      })
      if (senderDraft.source_rep_name.trim()) {
        await createEmailAccountAssignment(clientId, acct.account_id, {
          user_id: emailSuggestion?.suggested_user_id ?? null,
          source_rep_name: senderDraft.source_rep_name.trim(),
          active: true,
          is_default_for_rep: true,
        })
      }
      setShowAddSender(false)
      setMsg(`Sender account ${email} created (not connected).`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Create sender account failed.')
    } finally {
      setEmailBusy(false)
    }
  }

  async function onSaveSenderEdit(account: ClientEmailAccount) {
    setEmailBusy(true)
    setError(null)
    try {
      await updateEmailAccount(clientId, account.account_id, {
        email_address: senderDraft.email_address.trim() || account.email_address,
        display_name: senderDraft.display_name,
        provider: senderDraft.provider,
        connection_status: account.connection_status,
        active: account.active,
        is_default: senderDraft.is_default,
      })
      setEditingAccountId(null)
      setMsg('Sender account updated.')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Update sender account failed.')
    } finally {
      setEmailBusy(false)
    }
  }

  async function onDeactivateSender(accountId: number) {
    setEmailBusy(true)
    setError(null)
    try {
      await deactivateEmailAccount(clientId, accountId)
      setMsg('Sender account deactivated.')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Deactivate failed.')
    } finally {
      setEmailBusy(false)
    }
  }

  async function onConnectSender(accountId: number, provider: string) {
    setEmailBusy(true)
    setError(null)
    try {
      const prov = (provider || '').trim().toLowerCase()
      if (prov === 'google' || prov === 'gmail') {
        const status = await fetchGoogleOAuthStatus()
        if (!status.configured) {
          setError(
            status.message ||
              'Google OAuth is not configured. Set GOOGLE_OAUTH_* and NORTHSTAR_OAUTH_TOKEN_KEY in backend/.env, then restart the API.',
          )
          return
        }
        // Browser navigates to backend connect route → 302 to Google consent.
        window.location.href = `/api/email/google/connect/${encodeURIComponent(String(accountId))}`
        return
      }
      const res = await connectEmailAccount(clientId, accountId)
      if (res.authorization_url) {
        window.location.href = res.authorization_url
        return
      }
      setMsg(
        res.message ||
          'Provider connection is not available yet for this mailbox provider.',
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Connect failed.')
    } finally {
      setEmailBusy(false)
    }
  }

  async function onDisconnectSender(accountId: number) {
    if (
      !window.confirm(
        'Disconnect Google for this sender? Sending will stop. Email history is preserved.',
      )
    ) {
      return
    }
    setEmailBusy(true)
    setError(null)
    try {
      const res = await disconnectEmailAccount(clientId, accountId)
      setMsg(res.message || 'Disconnected.')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Disconnect failed.')
    } finally {
      setEmailBusy(false)
    }
  }

  async function onCreateSignature() {
    if (!sigDraft.signature_body.trim()) {
      setError('Signature body is required.')
      return
    }
    setEmailBusy(true)
    setError(null)
    try {
      await createEmailSignature(clientId, {
        signature_name: sigDraft.signature_name.trim() || 'Signature',
        signature_body: sigDraft.signature_body,
        source_rep_name: sigDraft.source_rep_name.trim(),
        email_account_id: sigDraft.email_account_id
          ? Number(sigDraft.email_account_id)
          : null,
        is_default: sigDraft.is_default,
        active: true,
      })
      setSigDraft({
        signature_name: '',
        signature_body: '',
        source_rep_name: '',
        email_account_id: '',
        is_default: true,
      })
      setMsg('Signature saved.')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save signature failed.')
    } finally {
      setEmailBusy(false)
    }
  }

  function startEditSignature(s: ClientEmailSignature) {
    setEditingSignatureId(s.signature_id)
    setSigEditDraft({
      signature_name: s.signature_name || '',
      signature_body: s.signature_body || '',
      source_rep_name: s.source_rep_name || '',
      email_account_id: s.email_account_id ?? '',
      is_default: !!s.is_default,
    })
  }

  async function onSaveSignatureEdit() {
    if (editingSignatureId == null) return
    if (!sigEditDraft.signature_body.trim()) {
      setError('Signature body is required.')
      return
    }
    setEmailBusy(true)
    setError(null)
    try {
      await updateEmailSignature(clientId, editingSignatureId, {
        signature_name: sigEditDraft.signature_name.trim() || 'Signature',
        signature_body: sigEditDraft.signature_body,
        source_rep_name: sigEditDraft.source_rep_name.trim(),
        email_account_id: sigEditDraft.email_account_id
          ? Number(sigEditDraft.email_account_id)
          : null,
        is_default: sigEditDraft.is_default,
        active: true,
      })
      setEditingSignatureId(null)
      setMsg('Signature updated.')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Update signature failed.')
    } finally {
      setEmailBusy(false)
    }
  }

  async function loadPreviewContacts() {
    try {
      const rows = await listEmailPreviewContacts(clientId)
      setPreviewContacts(rows)
    } catch {
      setPreviewContacts([])
    }
  }

  async function loadPreviewAppointments(contactId: number) {
    if (!contactId) {
      setPreviewAppointments([])
      return
    }
    try {
      const rows = await listEmailPreviewAppointments(clientId, contactId)
      setPreviewAppointments(rows)
      return
    } catch (primaryErr) {
      // Fallback: sales-events appointment family for this contact (same client scope).
      try {
        const events = await fetchClientSalesEvents(clientId, {
          contact_id: contactId,
          event_family: 'appointment',
          limit: 100,
        })
        const mapped: EmailPreviewAppointment[] = events
          .filter((e) => {
            const et = String(e.event_type || '').toLowerCase()
            return (
              et.startsWith('appointment') ||
              String(e.event_family || '')
                .toLowerCase()
                .includes('appoint')
            )
          })
          .filter((e) => {
            const et = String(e.event_type || '').toLowerCase()
            return !['send information', 'rfq', 'email sent'].includes(et)
          })
          .map((e) => {
            const eventId = Number(e.id)
            const dateRaw = String(e.event_date || '')
            const timeRaw = String(e.event_time || '')
            const meetingType = String(e.meeting_type || '')
            const eventType = String(e.event_type || 'Appointment')
            const tz = String(e.timezone || 'CDT')
            const sourceTxt = String(e.source_date_time_text || '')
            const labelParts = [
              dateRaw,
              timeRaw ? `${timeRaw} ${tz}`.trim() : '',
              meetingType || eventType,
              eventType.toLowerCase().includes('reschedul') ? 'Rescheduled' : '',
            ].filter(Boolean)
            return {
              source: 'sales_event' as const,
              event_id: eventId,
              label:
                labelParts.join(' · ') ||
                sourceTxt ||
                `Sales event #${eventId}`,
              appointment_date: dateRaw,
              appointment_time: timeRaw,
              company_name: String(e.company_name || ''),
              contact_name: String(e.contact_name || ''),
              meeting_with: '',
              event_type: eventType,
              meeting_type: meetingType,
              timezone: tz,
              source_date_time_text: sourceTxt,
              company_id: e.company_id != null ? Number(e.company_id) : null,
              contact_id: e.contact_id != null ? Number(e.contact_id) : null,
            }
          })
        setPreviewAppointments(mapped)
        setError(
          primaryErr instanceof Error
            ? `Appointment selector used sales-events fallback (${primaryErr.message}).`
            : 'Appointment selector used sales-events fallback.',
        )
      } catch (err) {
        setPreviewAppointments([])
        setError(
          err instanceof Error
            ? `Could not load appointments for contact: ${err.message}`
            : 'Could not load appointments for contact.',
        )
      }
    }
  }

  async function onRunEmailPreview() {
    const accountId = Number(previewSel.account_id)
    const contactId = Number(previewSel.contact_id)
    const templateId = Number(previewSel.template_id)
    if (!accountId || !contactId || !templateId) {
      setError('Select sender account, contact, and template for preview.')
      return
    }
    let salesEventId: number | null = null
    let appointmentEventId: number | null = null
    let appointmentDate = ''
    let appointmentTime = ''
    let appointmentContact = ''
    let meetingWith = ''
    if (previewSel.appointment_key) {
      const [source, idRaw] = previewSel.appointment_key.split(':')
      const eventId = Number(idRaw)
      const hit = previewAppointments.find(
        (a) => a.source === source && a.event_id === eventId,
      )
      if (hit) {
        appointmentDate = hit.appointment_date || ''
        appointmentTime = hit.appointment_time || ''
        appointmentContact = hit.contact_name || ''
        meetingWith = hit.meeting_with || ''
        if (hit.source === 'sales_event') salesEventId = hit.event_id
        else if (hit.source === 'appointment_event') appointmentEventId = hit.event_id
      }
    }
    setPreviewBusy(true)
    setError(null)
    setEmailPreview(null)
    try {
      const result = await previewClientEmail(clientId, {
        account_id: accountId,
        contact_id: contactId,
        template_id: templateId,
        appointment_date: appointmentDate,
        appointment_time: appointmentTime,
        appointment_contact: appointmentContact,
        appointment_event_id: appointmentEventId,
        sales_event_id: salesEventId,
        meeting_with: meetingWith,
      })
      setEmailPreview(result)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview failed.')
    } finally {
      setPreviewBusy(false)
    }
  }

  useEffect(() => {
    void reload()
    setEngBatch(null)
    setEngPreview(null)
    setSheetTypes({})
    setPreviewFilter('')
    setDetailRow(null)
    setMsg(null)
    setEmailPreview(null)
    setPreviewContacts([])
    setPreviewAppointments([])
    setPreviewSel({ account_id: '', contact_id: '', template_id: '', appointment_key: '' })
    setShowAddSender(false)
    setEditingAccountId(null)
  }, [reload])

  // Prefetch incorporation for already-split parents so "X/Y children approved" is accurate.
  useEffect(() => {
    for (const p of reviewProposals) {
      if (isAlreadySplitParent(p) && !incorporationById[p.proposal_id]) {
        void loadIncorporation(p.proposal_id)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: only when proposal list changes
  }, [reviewProposals])

  const docs = useMemo(() => {
    const list = hub?.documents || []
    return includeArchived ? list : list.filter((d) => !d.archived)
  }, [hub, includeArchived])

  async function onUpload(file: File | null, replaceId?: number) {
    if (!file || !hub?.can_edit) return
    setUploading(true)
    setMsg(null)
    setError(null)
    try {
      await uploadClientDocument(clientId, file, docType, replaceId)
      setMsg(`Uploaded ${file.name}.`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed.')
    } finally {
      setUploading(false)
    }
  }

  async function onProcess(doc: ClientDocument) {
    try {
      const result = await processClientDocument(clientId, doc.document_id)
      setMsg(
        result.document.sensitive_flag
          ? `${result.message} · ${display(result.document.sensitive_note)}`
          : result.message,
      )
      setTab('review')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Process failed.')
    }
  }

  async function onReview(p: ClientExtractionProposal, status: 'Approved' | 'Rejected') {
    try {
      await reviewExtractionProposal(clientId, p.proposal_id, status)
      setMsg(`${status}: ${p.field_label}`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Review failed.')
    }
  }

  async function onEditApprove(p: ClientExtractionProposal) {
    try {
      if (!editSection.trim()) {
        setError('Choose a destination section before Save + Approve.')
        return
      }
      let field = editField.trim()
      if (editSection === 'client_operations') {
        field = OPS_FIELD_ALIASES[field] || field || 'other_operational_notes'
      }
      if (!field) {
        setError('Choose a destination field before Save + Approve.')
        return
      }
      await reviewExtractionProposal(clientId, p.proposal_id, 'Approved', editValue, {
        section: editSection,
        field_name: field,
        item_title: editTitle || undefined,
      })
      setEditProposalId(null)
      setEditValue('')
      setEditSection('')
      setEditField('')
      setEditTitle('')
      setMsg(
        `Approved → ${editSection}${editTitle ? ` · ${editTitle}` : ''}: saved to destination section.`,
      )
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Edit + Approve failed.')
    }
  }

  async function onSaveContact() {
    if (!hub?.can_edit) return
    setError(null)
    try {
      if (editingContactId) {
        await updateClientContact(clientId, editingContactId, contactForm)
        setMsg('Client Contact updated.')
      } else {
        await createClientContact(clientId, contactForm)
        setMsg('Client Contact added.')
      }
      setContactForm({ ...EMPTY_CONTACT_FORM })
      setEditingContactId(null)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Contact save failed.')
    }
  }

  function beginEditContact(c: ClientContact) {
    setEditingContactId(c.contact_id)
    setContactForm({
      name: c.name || '',
      title: c.title || '',
      email: c.email || '',
      phone: c.phone || '',
      role_type: c.role_type || '',
      notes: c.notes || '',
    })
    setTab('contacts')
  }

  async function onDeactivateContact(c: ClientContact) {
    if (!hub?.can_edit) return
    try {
      await deactivateClientContact(clientId, c.contact_id)
      setMsg(`Deactivated ${c.name || 'contact'}.`)
      if (editingContactId === c.contact_id) {
        setEditingContactId(null)
        setContactForm({ ...EMPTY_CONTACT_FORM })
      }
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Deactivate failed.')
    }
  }

  function isMultiPersonContactProposal(p: ClientExtractionProposal): boolean {
    if (
      !(
        (p.section === 'client_contacts' || p.section === 'client_profile') &&
        (p.field_name === 'client_contacts' || p.field_name === 'client_contact_person')
      )
    ) {
      return false
    }
    const text = p.proposed_value || ''
    try {
      const parsed = JSON.parse(text) as { name?: string; email?: string }
      if (parsed && typeof parsed === 'object' && (parsed.name || parsed.email)) {
        return false
      }
    } catch {
      /* not JSON */
    }
    const parts = text.split(/\s*;\s*|\s*\|\s*|\n+/).map((s) => s.trim()).filter(Boolean)
    return parts.length > 1
  }

  async function onPreviewSplit(p: ClientExtractionProposal) {
    setError(null)
    try {
      const preview = await previewSplitClientContacts(clientId, p.proposal_id)
      setSplitPreview(preview)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Split preview failed.')
    }
  }

  async function onCreateSplitProposals() {
    if (!splitPreview) return
    setSplitBusy(true)
    setError(null)
    try {
      const result = await createSplitClientContactProposals(
        clientId,
        splitPreview.proposal_id,
        splitPreview.people,
      )
      setMsg(result.message)
      setSplitPreview(null)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Split failed.')
    } finally {
      setSplitBusy(false)
    }
  }

  function parseEnrichment(p: ClientExtractionProposal): {
    contact_id?: number
    field_name?: string
    proposed_value?: string
    existing_value?: string
    name?: string
    email?: string
  } | null {
    if (p.field_name !== 'client_contact_enrichment') return null
    try {
      return JSON.parse(p.proposed_value) as {
        contact_id?: number
        field_name?: string
        proposed_value?: string
        existing_value?: string
        name?: string
        email?: string
      }
    } catch {
      return null
    }
  }

  function splitIntoCount(p: ClientExtractionProposal): number | null {
    const m = (p.source_locator || '').match(/split_into:(\d+)/i)
    if (!m) return null
    const n = parseInt(m[1], 10)
    return Number.isFinite(n) ? n : null
  }

  function isAlreadySplitParent(p: ClientExtractionProposal): boolean {
    return splitIntoCount(p) != null
  }

  async function loadIncorporation(proposalId: number) {
    try {
      const analysis = await fetchIncorporationAnalysis(clientId, proposalId)
      setIncorporationById((prev) => ({ ...prev, [proposalId]: analysis }))
      return analysis
    } catch (err) {
      // Do not paint global page error for analysis misses (e.g. non-contact proposals)
      console.warn('Incorporation analysis failed', proposalId, err)
      return null
    }
  }

  async function beginResolve(p: ClientExtractionProposal) {
    setError(null)
    setMsg(null)
    cancelTemplateEditor()
    // Open the inline panel immediately so the click is visible even while analysis loads.
    setResolveProposalId(p.proposal_id)
    setResolveBusy(true)
    setResolveReason(
      'Split into individual Client Contacts and incorporated through approved proposals.',
    )
    setResolveChildIds('')
    setResolveContactIds('')
    setResolveChildLabels([])
    setResolveContactLabels([])

    try {
      const analysis = await loadIncorporation(p.proposal_id)
      const childIds = analysis?.suggested_resolved_by_proposal_ids || []
      const contactIds = analysis?.suggested_resolved_client_contact_ids || []

      if (childIds.length > 0) {
        const idList = childIds.map((id) => `#${id}`).join(', ')
        setResolveReason(
          `Split into individual Client Contacts and incorporated through approved proposals ${idList}.`,
        )
        setResolveChildIds(childIds.join(', '))
        try {
          const approved = await listExtractionProposals(clientId, 'Approved')
          setResolveChildLabels(
            childIds.map((id) => {
              const child = approved.find((x) => x.proposal_id === id)
              if (!child) return `#${id}`
              try {
                const parsed = JSON.parse(child.proposed_value || '{}') as {
                  name?: string
                  email?: string
                }
                const name = (parsed.name || parsed.email || '').trim()
                return name ? `#${id} ${name}` : `#${id}`
              } catch {
                const bit = (child.proposed_value || '').split(/[;<]/)[0].trim()
                return bit ? `#${id} ${bit}` : `#${id}`
              }
            }),
          )
        } catch {
          setResolveChildLabels(childIds.map((id) => `#${id}`))
        }
      } else if (analysis?.resolution_suggestion) {
        setResolveReason(analysis.resolution_suggestion)
      }

      if (contactIds.length > 0) {
        setResolveContactIds(contactIds.join(', '))
        const contacts = hub?.client_contacts || []
        setResolveContactLabels(
          contactIds.map((id) => {
            const c = contacts.find((x) => x.contact_id === id)
            return c ? c.name : `Contact #${id}`
          }),
        )
      }

      if (!analysis) {
        setError(
          `Could not load incorporation details for proposal #${p.proposal_id}. You can still enter a reason and confirm, or Cancel.`,
        )
      }
    } finally {
      setResolveBusy(false)
    }
  }

  async function onConfirmResolve() {
    if (!resolveProposalId) return
    if (!resolveReason.trim()) {
      setError('A short resolution reason is required.')
      return
    }
    setError(null)
    try {
      const childIds = resolveChildIds
        .split(/[,;\s]+/)
        .map((s) => parseInt(s, 10))
        .filter((n) => !Number.isNaN(n))
      const contactIds = resolveContactIds
        .split(/[,;\s]+/)
        .map((s) => parseInt(s, 10))
        .filter((n) => !Number.isNaN(n))
      await resolveExtractionProposal(clientId, resolveProposalId, {
        resolution_reason: resolveReason.trim(),
        resolved_by_proposal_ids: childIds,
        resolved_client_contact_ids: contactIds,
      })
      setMsg(`Resolved / Incorporated: proposal #${resolveProposalId}`)
      setResolveProposalId(null)
      setResolveReason('')
      setResolveChildIds('')
      setResolveContactIds('')
      setResolveChildLabels([])
      setResolveContactLabels([])
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Resolve failed.')
    }
  }

  function cancelResolve() {
    setResolveProposalId(null)
    setResolveReason('')
    setResolveChildIds('')
    setResolveContactIds('')
    setResolveChildLabels([])
    setResolveContactLabels([])
    setResolveBusy(false)
  }

  function beginEditProposal(p: ClientExtractionProposal) {
    setEditProposalId(p.proposal_id)
    setEditValue(p.proposed_value)
    setEditSection(p.section || 'unmapped')
    setEditField(p.field_name || 'unmapped_intelligence')
    setEditTitle(p.field_label || '')
  }

  function onDestinationSectionChange(nextSec: string) {
    const prevField = editField
    setEditSection(nextSec)
    const fields =
      sectionCatalog.find((s) => s.section_key === nextSec)?.fields ||
      (nextSec === 'client_operations' ? FALLBACK_OPS_FIELDS : [])
    if (nextSec === 'client_operations') {
      const mapped = OPS_FIELD_ALIASES[prevField]
      const nextField = mapped || fields[0]?.field_name || 'other_operational_notes'
      setEditField(nextField)
      const label =
        fields.find((f) => f.field_name === nextField)?.field_label ||
        FALLBACK_OPS_FIELDS.find((f) => f.field_name === nextField)?.field_label ||
        ''
      if (label) setEditTitle(label)
      return
    }
    const first = fields[0]?.field_name || ''
    setEditField(first)
    const label = fields[0]?.field_label || ''
    if (label) setEditTitle(label)
  }

  const editFieldsForSection = useMemo(() => {
    const sec = sectionCatalog.find((s) => s.section_key === editSection)
    if (sec?.fields?.length) return sec.fields
    if (editSection === 'client_operations') return FALLBACK_OPS_FIELDS
    return [{ field_name: 'unmapped_intelligence', field_label: 'Unmapped Intelligence' }]
  }, [sectionCatalog, editSection])

  async function onApproveMatches() {
    try {
      const rows = await bulkReviewExtractions(clientId, { mode: 'matches', status: 'Approved' })
      setMsg(`Approved ${rows.length} MATCH proposal(s).`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Approve All Matches failed.')
    }
  }

  function selectAllVisible() {
    const ids = reviewProposals.filter((p) => p.status === 'Pending').map((p) => p.proposal_id)
    setSelectedProposalIds(ids)
  }

  function clearSelection() {
    setSelectedProposalIds([])
    setBulkResolvePreview(null)
    setBulkRejectOpen(false)
  }

  async function onOpenBulkResolve() {
    if (selectedProposalIds.length === 0) return
    setBulkBusy(true)
    setError(null)
    setBulkSummary(null)
    try {
      const preview = await previewBulkResolve(clientId, selectedProposalIds)
      setBulkResolvePreview(preview)
      setBulkRejectOpen(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bulk resolve preview failed.')
    } finally {
      setBulkBusy(false)
    }
  }

  async function onConfirmBulkResolve() {
    if (!bulkResolvePreview || selectedProposalIds.length === 0) return
    setBulkBusy(true)
    setError(null)
    try {
      const result = await bulkResolveExtractions(clientId, selectedProposalIds)
      setBulkResolvePreview(null)
      setSelectedProposalIds([])
      setBulkSummary({ kind: 'resolve', result })
      setShowSkippedDetails(false)
      setMsg(result.message)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bulk resolve failed.')
    } finally {
      setBulkBusy(false)
    }
  }

  async function onConfirmBulkReject() {
    if (!rejectCategory || selectedProposalIds.length === 0) return
    setBulkBusy(true)
    setError(null)
    try {
      const result = await bulkRejectExtractions(clientId, {
        proposal_ids: selectedProposalIds,
        rejection_category: rejectCategory,
        rejection_note: rejectNote,
      })
      setBulkRejectOpen(false)
      setRejectCategory('')
      setRejectNote('')
      setSelectedProposalIds([])
      setBulkSummary({ kind: 'reject', result })
      setShowSkippedDetails(false)
      setMsg(result.message)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bulk reject failed.')
    } finally {
      setBulkBusy(false)
    }
  }

  async function onArchive(doc: ClientDocument) {
    try {
      await archiveClientDocument(clientId, doc.document_id)
      setMsg(`Archived ${doc.filename}.`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Archive failed.')
    }
  }

  async function onEngagementUpload(file: File | null) {
    if (!file || !hub?.can_edit) return
    setUploading(true)
    setError(null)
    setMsg(null)
    try {
      const batch = await startEngagementImport(clientId, file)
      setEngBatch(batch)
      const types: Record<number, string> = {}
      batch.sheets.forEach((s) => {
        types[s.sheet_id] = s.user_type || s.detected_type
      })
      setSheetTypes(types)
      setEngPreview(null)
      setDetailRow(null)
      setMsg(`Uploaded ${file.name}. Review sheet types, then preview.`)
      setTab('appointments')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Workbook upload failed.')
    } finally {
      setUploading(false)
    }
  }

  async function onEngagementPreview() {
    if (!engBatch) return
    try {
      const sheets = Object.entries(sheetTypes).map(([id, user_type]) => ({
        sheet_id: Number(id),
        user_type,
      }))
      const batch = await classifyEngagementSheets(clientId, engBatch.batch_id, sheets)
      setEngBatch(batch)
      const preview = await mapEngagementImport(clientId, engBatch.batch_id, {})
      setEngPreview(preview)
      setEngBatch(preview.batch)
      setMsg('Preview ready — no CRM import performed.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Preview failed.')
    }
  }

  if (loading && !hub) return <p className="data-status">Loading Client Knowledge…</p>
  if (error && !hub) {
    return (
      <p className="data-status data-status--error" role="alert">
        {error}
      </p>
    )
  }
  if (!hub) return null

  const profile = sectionPayload(hub, 'client_profile')
  const strategy = sectionPayload(hub, 'strategy')
  const playbook = sectionPayload(hub, 'call_playbook')
  const opsItems = operationsItems(hub)
  const allClientContacts = hub.client_contacts || []
  const roleTypes = hub.client_contact_role_types || [
    'Client Owner / Executive',
    'Sales Contact',
    'Operations Contact',
    'Takes Appointments',
    'Appointment CC',
    'Strategy Contact',
    'Billing / Administrative',
    'Other',
  ]
  const visibleContacts = showInactiveContacts
    ? allClientContacts
    : allClientContacts.filter((c) => c.active)

  return (
    <div className="client-setup-page">
      <div className="page-heading page-heading--split">
        <div>
          <button type="button" className="link-btn back-link" onClick={() => navigate(`/clients/${clientId}/setup`)}>
            ← Client Setup
          </button>
          <h1>{hub.client_name} — Client Knowledge &amp; Documents</h1>
          <p>Client-scoped documents, strategy, playbook, templates, and appointment import foundation.</p>
        </div>
        <div className="setup-actions">
          <Link className="link-btn" to={`/clients/${clientId}/setup`}>
            Open Client Setup
          </Link>
        </div>
      </div>

      {!hub.can_edit && (
        <p className="data-status">View only — authorized editors can upload and import.</p>
      )}
      {msg && <p className="data-status">{msg}</p>}
      {error && (
        <p className="data-status data-status--error" role="alert">
          {error}
        </p>
      )}

      <div className="setup-campaign-tabs" role="tablist" aria-label="Knowledge sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={
              tab === t.id ? 'setup-campaign-tab setup-campaign-tab--active' : 'setup-campaign-tab'
            }
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'profile' && (
        <section className="panel">
          <div className="panel-header">
            <h2>CLIENT PROFILE</h2>
          </div>
          <p className="queue-sub">
            Structured knowledge storage is ready. Edit authoritative fields in Client Setup; extracted
            proposals never auto-overwrite.
          </p>
          <dl className="ask-fact-grid">
            <div>
              <dt>Client</dt>
              <dd>{display(hub.client_name)}</dd>
            </div>
            <div>
              <dt>Website</dt>
              <dd>{display(String(profile.website || ''))}</dd>
            </div>
            <div>
              <dt>Address</dt>
              <dd>{display(String(profile.address || profile.main_location || ''))}</dd>
            </div>
            <div>
              <dt>Main phone</dt>
              <dd>{display(String(profile.main_phone || ''))}</dd>
            </div>
            <div>
              <dt>NorthStar owner</dt>
              <dd>{display(String(profile.primary_owner_name || ''))}</dd>
            </div>
            <div>
              <dt>Who takes appointments</dt>
              <dd>{display(String(profile.who_takes_appointments || ''))}</dd>
            </div>
          </dl>
        </section>
      )}

      {tab === 'strategy' && (
        <section className="panel">
          <div className="panel-header">
            <h2>STRATEGY</h2>
          </div>
          <p className="queue-sub">
            Strategy fields are prepared for staged extraction. Campaign/target profile fields remain
            in Client Setup. Prospecting Guidance and Sales Challenges / Barriers are knowledge-only
            (they do not independently determine Campaign Fit).
          </p>
          <ul className="ask-bullet-list">
            {(
              [
                'reason_hired',
                'sales_goals',
                'primary_service',
                'secondary_services',
                'target_industries',
                'ideal_customer_profile',
                'target_geography',
                'target_titles',
                'positive_fit_signals',
                'negative_fit_signals',
                'exclusions',
                'prospecting_guidance',
                'sales_challenges_barriers',
              ] as const
            ).map((k) => (
              <li key={k}>
                <strong>{fieldLabel('strategy', k)}</strong>:{' '}
                {display(String(strategy[k] || ''))}
              </li>
            ))}
          </ul>
          {hub.can_edit && (
            <div className="ask-section" style={{ marginTop: '1rem' }}>
              <h3>Edit knowledge-only strategy fields</h3>
              {(
                [
                  ['prospecting_guidance', 'Prospecting Guidance'],
                  ['sales_challenges_barriers', 'Sales Challenges / Barriers'],
                ] as const
              ).map(([field, label]) => {
                const draftKey = `strategy.${field}`
                const current = String(strategy[field] || '')
                const draft = knowledgeDrafts[draftKey] ?? current
                return (
                  <div key={field} className="edit-field" style={{ marginBottom: '0.75rem' }}>
                    <label className="edit-field__label">{label}</label>
                    <textarea
                      value={draft}
                      rows={4}
                      style={{ width: '100%' }}
                      onChange={(e) =>
                        setKnowledgeDrafts((prev) => ({ ...prev, [draftKey]: e.target.value }))
                      }
                    />
                    <button
                      type="button"
                      className="link-btn"
                      disabled={knowledgeSaveBusy || draft === current}
                      onClick={() => void onSaveKnowledgeField('strategy', field)}
                    >
                      Save {label}
                    </button>
                  </div>
                )
              })}
            </div>
          )}
          {hub.extraction_proposals.length > 0 && (
            <>
              <h3>Pending extraction proposals</h3>
              <ul className="ask-history-list">
                {hub.extraction_proposals.map((p) => (
                  <li key={p.proposal_id}>
                    <strong>{p.field_name}</strong> · {p.status}
                    <div className="queue-sub">
                      Current: {display(p.existing_value)} → Proposed: {display(p.proposed_value)}
                    </div>
                    <div className="queue-sub">{display(p.source_reference)}</div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      {tab === 'playbook' && (
        <section className="panel">
          <div className="panel-header">
            <h2>ROAD MAP / CALL PLAYBOOK</h2>
          </div>
          <ul className="ask-bullet-list">
            {(
              [
                'thirty_second_commercial',
                'company_story_background',
                'discovery_questions',
                'common_objections',
                'objection_responses',
                'value_propositions',
                'appointment_instructions',
                'caller_notes',
              ] as const
            ).map((k) => (
              <li key={k}>
                <strong>{fieldLabel('call_playbook', k)}</strong>:{' '}
                {display(String(playbook[k] || ''))}
              </li>
            ))}
          </ul>
          {hub.can_edit && (
            <div className="ask-section" style={{ marginTop: '1rem' }}>
              <h3>Edit Company Story / Background</h3>
              <p className="queue-sub">
                Company history and credibility points — not the 30-Second Commercial.
              </p>
              {(() => {
                const field = 'company_story_background'
                const draftKey = `call_playbook.${field}`
                const current = String(playbook[field] || '')
                const draft = knowledgeDrafts[draftKey] ?? current
                return (
                  <div className="edit-field">
                    <label className="edit-field__label">Company Story / Background</label>
                    <textarea
                      value={draft}
                      rows={5}
                      style={{ width: '100%' }}
                      onChange={(e) =>
                        setKnowledgeDrafts((prev) => ({ ...prev, [draftKey]: e.target.value }))
                      }
                    />
                    <button
                      type="button"
                      className="link-btn"
                      disabled={knowledgeSaveBusy || draft === current}
                      onClick={() => void onSaveKnowledgeField('call_playbook', field)}
                    >
                      Save Company Story / Background
                    </button>
                  </div>
                )
              })()}
            </div>
          )}
          <h3>Capabilities</h3>
          <ul className="ask-bullet-list">
            {[
              'capabilities',
              'equipment',
              'capacity',
              'certifications',
              'production_preferences',
            ].map((k) => (
              <li key={k}>
                <strong>{k.replace(/_/g, ' ')}</strong>:{' '}
                {display(String(sectionPayload(hub, 'capabilities')[k] || ''))}
              </li>
            ))}
          </ul>
        </section>
      )}

      {tab === 'templates' && (
        <section className="panel">
          <div className="panel-header">
            <h2>EMAIL TEMPLATES</h2>
          </div>
          <p className="queue-sub">
            Approved client email templates for storage/retrieval only. NorthStar does not send email
            from this screen. Build templates from Strategy Import Review extraction groups.
          </p>
          {(hub.email_templates || []).length === 0 ? (
            <p className="queue-sub">No email templates stored for this client yet.</p>
          ) : (
            <ul className="ask-history-list">
              {(hub.email_templates || []).map((t) => (
                <li key={t.template_id}>
                  {editingTemplateId === t.template_id ? (
                    <div className="edit-field" style={{ display: 'grid', gap: '0.4rem' }}>
                      <label className="edit-field__label">Template Name</label>
                      <input
                        className="edit-input"
                        value={templateEditDraft.template_name}
                        onChange={(e) =>
                          setTemplateEditDraft((d) => ({ ...d, template_name: e.target.value }))
                        }
                      />
                      <label className="edit-field__label">Type</label>
                      <input
                        className="edit-input"
                        value={templateEditDraft.template_type}
                        onChange={(e) =>
                          setTemplateEditDraft((d) => ({ ...d, template_type: e.target.value }))
                        }
                      />
                      <label className="edit-field__label">Subject</label>
                      <input
                        className="edit-input"
                        value={templateEditDraft.subject}
                        onChange={(e) =>
                          setTemplateEditDraft((d) => ({ ...d, subject: e.target.value }))
                        }
                      />
                      <label className="edit-field__label">Body</label>
                      <textarea
                        rows={8}
                        style={{ width: '100%' }}
                        value={templateEditDraft.body}
                        onChange={(e) =>
                          setTemplateEditDraft((d) => ({ ...d, body: e.target.value }))
                        }
                      />
                      <p className="queue-sub">
                        Sender signature is added automatically. Keep message content in the body;
                        do not paste the full signature into every template. Optional placeholder:{' '}
                        [Revenue Specialist Signature] inserts the signature at that location instead
                        of appending.
                      </p>
                      <p className="queue-sub">
                        Supported placeholders: [First Name] [Name] [Company]
                        [Appointment Date] [Appointment Time] [Appointment Contact]
                        [Meeting With] [Revenue Specialist] [Revenue Specialist Signature]
                        [Client Name]. [Name] resolves to first name for greetings.
                      </p>
                      <label>
                        <input
                          type="checkbox"
                          checked={templateEditDraft.is_active}
                          onChange={(e) =>
                            setTemplateEditDraft((d) => ({ ...d, is_active: e.target.checked }))
                          }
                        />{' '}
                        Active
                      </label>
                      <div className="setup-actions">
                        <button
                          type="button"
                          className="primary-btn"
                          disabled={templateBusy}
                          onClick={() => void onSaveTemplateEdit()}
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() => setEditingTemplateId(null)}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <strong>{t.template_name}</strong>
                      {t.is_active ? '' : ' · Inactive'}
                      <div className="queue-sub">{display(t.template_type)}</div>
                      <div>Subject: {display(t.subject)}</div>
                      <div className="queue-sub" style={{ whiteSpace: 'pre-wrap' }}>
                        {(t.body || '').slice(0, 280)}
                        {(t.body || '').length > 280 ? '…' : ''}
                      </div>
                      {hub.can_edit ? (
                        <div className="setup-actions" style={{ marginTop: '0.35rem' }}>
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => openTemplateEditor(t)}
                          >
                            Open/Edit
                          </button>
                          {t.is_active ? (
                            <button
                              type="button"
                              className="link-btn"
                              disabled={templateBusy}
                              onClick={() => void onDeactivateTemplate(t)}
                            >
                              Deactivate
                            </button>
                          ) : null}
                        </div>
                      ) : null}
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}

          <div className="panel-header panel-header--sub" style={{ marginTop: '1.25rem' }}>
            <h3>PREVIEW EMAIL</h3>
            <span className="queue-source">Read-only · does not send</span>
          </div>
          <p className="queue-sub">
            Select Client context (this client), Sender Account, CRM Contact, Appointment
            (optional), and Template. Unresolved placeholders are shown as UNRESOLVED —
            NorthStar does not invent values.
          </p>
          <div className="setup-grid" style={{ marginBottom: '0.75rem' }}>
            <label className="setup-field">
              <span className="setup-field-label">Sender Account</span>
              <select
                value={previewSel.account_id}
                onChange={(e) => setPreviewSel((s) => ({ ...s, account_id: e.target.value }))}
                onFocus={() => void loadPreviewContacts()}
              >
                <option value="">Select…</option>
                {emailAccounts
                  .filter((a) => a.active !== false)
                  .map((a) => {
                    const connected = (a.connection_status || '').toLowerCase() === 'connected'
                    const statusLabel = connected ? 'Connected' : 'Not Connected'
                    return (
                      <option key={a.account_id} value={a.account_id}>
                        {a.email_address} — {statusLabel}
                      </option>
                    )
                  })}
              </select>
            </label>
            <label className="setup-field">
              <span className="setup-field-label">Contact (CRM)</span>
              <select
                value={previewSel.contact_id}
                onChange={(e) => {
                  const contactId = e.target.value
                  setPreviewSel((s) => ({
                    ...s,
                    contact_id: contactId,
                    appointment_key: '',
                  }))
                  setEmailPreview(null)
                  void loadPreviewAppointments(Number(contactId) || 0)
                }}
                onFocus={() => void loadPreviewContacts()}
              >
                <option value="">Select…</option>
                {previewContacts.map((c) => (
                  <option key={c.contact_id} value={c.contact_id}>
                    {c.contact_name} · {c.company_name} · {c.email}
                  </option>
                ))}
              </select>
            </label>
            <label className="setup-field">
              <span className="setup-field-label">Appointment</span>
              <select
                value={previewSel.appointment_key}
                onChange={(e) =>
                  setPreviewSel((s) => ({ ...s, appointment_key: e.target.value }))
                }
                onFocus={() => {
                  const cid = Number(previewSel.contact_id) || 0
                  if (cid) void loadPreviewAppointments(cid)
                }}
                disabled={!previewSel.contact_id}
              >
                <option value="">
                  {!previewSel.contact_id
                    ? 'Select a contact first…'
                    : previewAppointments.length === 0
                      ? 'None — no appointments for this contact'
                      : 'None — leave date/time UNRESOLVED'}
                </option>
                {previewAppointments.map((a) => (
                  <option key={`${a.source}:${a.event_id}`} value={`${a.source}:${a.event_id}`}>
                    {a.label}
                  </option>
                ))}
              </select>
              {previewSel.contact_id ? (
                <span className="queue-sub">
                  {previewAppointments.length} appointment
                  {previewAppointments.length === 1 ? '' : 's'} for selected contact
                  (client-scoped · excludes Send Information / RFQ / Email Sent)
                </span>
              ) : null}
            </label>
            <label className="setup-field">
              <span className="setup-field-label">Template</span>
              <select
                value={previewSel.template_id}
                onChange={(e) => setPreviewSel((s) => ({ ...s, template_id: e.target.value }))}
              >
                <option value="">Select…</option>
                {(hub.email_templates || [])
                  .filter((t) => t.is_active)
                  .map((t) => (
                    <option key={t.template_id} value={t.template_id}>
                      {t.template_name}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          <div className="setup-actions">
            <button
              type="button"
              className="primary-btn"
              disabled={previewBusy}
              onClick={() => void onRunEmailPreview()}
            >
              {previewBusy ? 'Rendering…' : 'Preview Email'}
            </button>
          </div>
          {emailPreview ? (
            <div
              className="edit-field"
              style={{ marginTop: '0.85rem', display: 'grid', gap: '0.35rem' }}
            >
              <div className="queue-sub">{emailPreview.message}</div>
              <div>
                <strong>From:</strong> {display(emailPreview.from_display)}
              </div>
              <div>
                <strong>Connection:</strong>{' '}
                {emailPreview.connection_connected
                  ? 'Connected'
                  : `Not Connected (${display(emailPreview.connection_status)})`}
              </div>
              <div>
                <strong>To:</strong> {display(emailPreview.to_address)}
                {emailPreview.to_contact_name
                  ? ` (${emailPreview.to_contact_name})`
                  : ''}
              </div>
              <div>
                <strong>Subject:</strong>
                <div style={{ whiteSpace: 'pre-wrap' }}>{display(emailPreview.subject)}</div>
              </div>
              <div>
                <strong>Message</strong>
                <div
                  className="edit-field"
                  style={{ whiteSpace: 'pre-wrap', marginTop: '0.25rem' }}
                >
                  {display(emailPreview.body)}
                </div>
              </div>
              {(emailPreview.signature_source || emailPreview.signature_placement) && (
                <p className="queue-sub" style={{ marginTop: '0.35rem' }}>
                  (Outside email) Sender signature from{' '}
                  {display(emailPreview.signature_source) || 'client_email_signatures'} ·{' '}
                  {display(emailPreview.signature_placement)}
                </p>
              )}
              {(emailPreview.unresolved_placeholders || []).length > 0 ? (
                <div>
                  <strong>Unresolved placeholders</strong>
                  <ul className="ask-history-list">
                    {emailPreview.unresolved_placeholders.map((p, i) => (
                      <li key={`${p.placeholder}-${i}`}>
                        <code>{p.placeholder}</code> · UNRESOLVED
                        {p.note ? ` · ${p.note}` : ''}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}
        </section>
      )}

      {tab === 'operations' && (
        <>
        <section className="panel">
          <div className="panel-header">
            <h2>EMAIL &amp; SENDING</h2>
            <span className="queue-source">Configuration only · no send</span>
          </div>
          <p className="queue-sub">
            Client-scoped sender accounts for From-address sending via Google OAuth (no passwords).
            Connect Google Account when OAuth is configured in backend/.env.
          </p>
          {emailSuggestion &&
          (emailSuggestion.suggested_email_address ||
            emailSuggestion.suggested_source_rep_name) ? (
            <div className="edit-field" style={{ marginBottom: '0.85rem' }}>
              <strong>Suggested from approved Client Operations</strong>
              <div className="queue-sub" style={{ marginTop: '0.25rem' }}>
                Email: {display(emailSuggestion.suggested_email_address)}
                {emailSuggestion.email_suggestion_source
                  ? ` · ${emailSuggestion.email_suggestion_source}`
                  : ''}
              </div>
              <div className="queue-sub">
                Revenue Specialist: {display(emailSuggestion.suggested_source_rep_name)}
                {emailSuggestion.suggested_user_id == null
                  ? ' · (no NorthStar user mapping — will store source_rep_name)'
                  : ''}
              </div>
              {(emailSuggestion.notes || []).map((n) => (
                <div key={n} className="queue-sub">
                  {n}
                </div>
              ))}
              {hub.can_edit && !emailSuggestion.already_configured ? (
                <button
                  type="button"
                  className="link-btn"
                  style={{ marginTop: '0.35rem' }}
                  onClick={() => applySenderSuggestion()}
                >
                  Use suggestion (requires confirmation to create)
                </button>
              ) : null}
              {emailSuggestion.already_configured ? (
                <div className="queue-sub">Suggested sender already configured for this client.</div>
              ) : null}
            </div>
          ) : null}

          {emailAccounts.length === 0 ? (
            <p className="queue-sub">No sender accounts configured yet for this client.</p>
          ) : (
            <ul className="ask-history-list">
              {emailAccounts.map((a) => {
                const reps = (a.assignments || [])
                  .filter((x) => x.active)
                  .map((x) => x.source_rep_name || (x.user_id != null ? `user#${x.user_id}` : ''))
                  .filter(Boolean)
                return (
                  <li key={a.account_id}>
                    {editingAccountId === a.account_id ? (
                      <div className="edit-field" style={{ display: 'grid', gap: '0.35rem' }}>
                        <label className="edit-field__label">Email Address</label>
                        <input
                          className="edit-input"
                          value={senderDraft.email_address}
                          onChange={(e) =>
                            setSenderDraft((d) => ({ ...d, email_address: e.target.value }))
                          }
                        />
                        <label className="edit-field__label">Display Name</label>
                        <input
                          className="edit-input"
                          value={senderDraft.display_name}
                          onChange={(e) =>
                            setSenderDraft((d) => ({ ...d, display_name: e.target.value }))
                          }
                          placeholder="e.g. Carmeco / Tyler Sullivan — Carmeco"
                        />
                        <label className="edit-field__label">Provider</label>
                        <select
                          value={senderDraft.provider}
                          onChange={(e) =>
                            setSenderDraft((d) => ({ ...d, provider: e.target.value }))
                          }
                        >
                          <option value="Google">Google</option>
                          <option value="Microsoft 365">Microsoft 365</option>
                          <option value="Other">Other</option>
                        </select>
                        <label>
                          <input
                            type="checkbox"
                            checked={senderDraft.is_default}
                            onChange={(e) =>
                              setSenderDraft((d) => ({ ...d, is_default: e.target.checked }))
                            }
                          />{' '}
                          Default Sender
                        </label>
                        <div className="setup-actions">
                          <button
                            type="button"
                            className="primary-btn"
                            disabled={emailBusy}
                            onClick={() => void onSaveSenderEdit(a)}
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => setEditingAccountId(null)}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <strong>{a.email_address}</strong>
                        {a.is_default ? ' · Default Sender' : ''}
                        {!a.active ? ' · Inactive' : ''}
                        <div className="queue-sub">
                          Display Name: {display(a.display_name)} · Provider: {display(a.provider)} ·
                          Connection: {display(a.connection_status)}
                          {a.connection_status === 'connected' ? '' : ' (not connected)'}
                        </div>
                        <div className="queue-sub">
                          Assigned Revenue Specialist(s):{' '}
                          {reps.length ? reps.join('; ') : '—'}
                        </div>
                        {hub.can_edit ? (
                          <div className="setup-actions" style={{ marginTop: '0.35rem' }}>
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => {
                                setEditingAccountId(a.account_id)
                                setSenderDraft({
                                  email_address: a.email_address,
                                  display_name: a.display_name,
                                  provider: a.provider || 'Other',
                                  is_default: a.is_default,
                                  source_rep_name: '',
                                })
                              }}
                            >
                              Edit
                            </button>
                            {a.active ? (
                              <button
                                type="button"
                                className="link-btn"
                                disabled={emailBusy}
                                onClick={() => void onDeactivateSender(a.account_id)}
                              >
                                Deactivate
                              </button>
                            ) : null}
                            {(a.connection_status || '').toLowerCase() === 'connected' ? (
                              <>
                                <span className="queue-sub">
                                  Connected as {a.email_address}
                                </span>
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={emailBusy}
                                  onClick={() => void onConnectSender(a.account_id, a.provider)}
                                >
                                  Reconnect
                                </button>
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={emailBusy}
                                  onClick={() => void onDisconnectSender(a.account_id)}
                                >
                                  Disconnect
                                </button>
                              </>
                            ) : (a.provider || '').toLowerCase() === 'google' ||
                              (a.provider || '').toLowerCase() === 'gmail' ? (
                              <button
                                type="button"
                                className="link-btn"
                                disabled={emailBusy}
                                title="Google OAuth — never stores passwords"
                                onClick={() => void onConnectSender(a.account_id, a.provider)}
                              >
                                Connect Google Account
                              </button>
                            ) : (
                              <button
                                type="button"
                                className="link-btn"
                                disabled={emailBusy}
                                title="Only Google OAuth is implemented"
                                onClick={() => void onConnectSender(a.account_id, a.provider)}
                              >
                                Connect Account
                              </button>
                            )}
                          </div>
                        ) : null}
                      </>
                    )}
                  </li>
                )
              })}
            </ul>
          )}

          {hub.can_edit ? (
            <div style={{ marginTop: '0.85rem' }}>
              {!showAddSender ? (
                <button
                  type="button"
                  className="primary-btn"
                  onClick={() => {
                    setShowAddSender(true)
                    setSenderDraft({
                      email_address: '',
                      display_name: '',
                      provider: 'Other',
                      is_default: emailAccounts.length === 0,
                      source_rep_name: '',
                    })
                  }}
                >
                  Add Sender Account
                </button>
              ) : (
                <div className="edit-field" style={{ display: 'grid', gap: '0.35rem' }}>
                  <strong>Add Sender Account</strong>
                  <p className="queue-sub">
                    Creating stores configuration only. Connection stays not_connected. Display Name
                    is not auto-chosen.
                  </p>
                  <label className="edit-field__label">Email Address</label>
                  <input
                    className="edit-input"
                    value={senderDraft.email_address}
                    onChange={(e) =>
                      setSenderDraft((d) => ({ ...d, email_address: e.target.value }))
                    }
                  />
                  <label className="edit-field__label">Display Name</label>
                  <input
                    className="edit-input"
                    value={senderDraft.display_name}
                    onChange={(e) =>
                      setSenderDraft((d) => ({ ...d, display_name: e.target.value }))
                    }
                    placeholder="Leave blank unless you choose a display name"
                  />
                  <label className="edit-field__label">Provider</label>
                  <select
                    value={senderDraft.provider}
                    onChange={(e) => setSenderDraft((d) => ({ ...d, provider: e.target.value }))}
                  >
                    <option value="Google">Google</option>
                    <option value="Microsoft 365">Microsoft 365</option>
                    <option value="Other">Other</option>
                  </select>
                  <label className="edit-field__label">Assigned Revenue Specialist</label>
                  <input
                    className="edit-input"
                    value={senderDraft.source_rep_name}
                    onChange={(e) =>
                      setSenderDraft((d) => ({ ...d, source_rep_name: e.target.value }))
                    }
                    placeholder="source_rep_name when user is not mapped"
                  />
                  <label>
                    <input
                      type="checkbox"
                      checked={senderDraft.is_default}
                      onChange={(e) =>
                        setSenderDraft((d) => ({ ...d, is_default: e.target.checked }))
                      }
                    />{' '}
                    Default Sender
                  </label>
                  <div className="setup-actions">
                    <button
                      type="button"
                      className="primary-btn"
                      disabled={emailBusy}
                      onClick={() => void onCreateSenderAccount()}
                    >
                      Create (approve)
                    </button>
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => setShowAddSender(false)}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          ) : null}

          <div className="panel-header panel-header--sub" style={{ marginTop: '1.25rem' }}>
            <h3>Sender Signatures</h3>
          </div>
          <p className="queue-sub">
            One client-specific sender signature (account + rep scoped). Templates hold message
            content; NorthStar appends this signature automatically for the active Working For
            client. Do not paste signatures into every template.
          </p>
          {emailSignatures.filter((s) => s.active).length === 0 ? (
            <p className="queue-sub">No active signatures for this client.</p>
          ) : (
            <ul className="ask-history-list">
              {emailSignatures
                .filter((s) => s.active)
                .map((s) => {
                  const acct = emailAccounts.find((a) => a.account_id === s.email_account_id)
                  const forLabel = [
                    s.source_rep_name || 'Rep',
                    acct?.email_address ||
                      (s.email_account_id != null ? `Account #${s.email_account_id}` : 'client-level'),
                  ].join(' + ')
                  if (editingSignatureId === s.signature_id) {
                    return (
                      <li key={s.signature_id}>
                        <div className="edit-field" style={{ display: 'grid', gap: '0.35rem' }}>
                          <strong>Edit Signature</strong>
                          <div className="queue-sub">Signature for: {forLabel}</div>
                          <input
                            className="edit-input"
                            placeholder="Signature name"
                            value={sigEditDraft.signature_name}
                            onChange={(e) =>
                              setSigEditDraft((d) => ({ ...d, signature_name: e.target.value }))
                            }
                          />
                          <input
                            className="edit-input"
                            placeholder="source_rep_name (e.g. Tyler Sullivan)"
                            value={sigEditDraft.source_rep_name}
                            onChange={(e) =>
                              setSigEditDraft((d) => ({ ...d, source_rep_name: e.target.value }))
                            }
                          />
                          <select
                            value={String(sigEditDraft.email_account_id)}
                            onChange={(e) =>
                              setSigEditDraft((d) => ({
                                ...d,
                                email_account_id: e.target.value,
                              }))
                            }
                          >
                            <option value="">Any / client-level</option>
                            {emailAccounts.map((a) => (
                              <option key={a.account_id} value={a.account_id}>
                                {a.email_address}
                              </option>
                            ))}
                          </select>
                          <textarea
                            rows={8}
                            style={{ width: '100%' }}
                            value={sigEditDraft.signature_body}
                            onChange={(e) =>
                              setSigEditDraft((d) => ({ ...d, signature_body: e.target.value }))
                            }
                          />
                          <label>
                            <input
                              type="checkbox"
                              checked={sigEditDraft.is_default}
                              onChange={(e) =>
                                setSigEditDraft((d) => ({
                                  ...d,
                                  is_default: e.target.checked,
                                }))
                              }
                            />{' '}
                            Default for this account scope
                          </label>
                          <div className="setup-actions">
                            <button
                              type="button"
                              className="primary-btn"
                              disabled={emailBusy}
                              onClick={() => void onSaveSignatureEdit()}
                            >
                              Save
                            </button>
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => setEditingSignatureId(null)}
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      </li>
                    )
                  }
                  return (
                    <li key={s.signature_id}>
                      <strong>{display(s.signature_name)}</strong>
                      {s.is_default ? ' · Default' : ''}
                      <div className="queue-sub">Signature for: {forLabel}</div>
                      <div style={{ whiteSpace: 'pre-wrap' }}>{s.signature_body || ''}</div>
                      {hub.can_edit ? (
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() => startEditSignature(s)}
                        >
                          Edit Signature
                        </button>
                      ) : null}
                    </li>
                  )
                })}
            </ul>
          )}
          {hub.can_edit ? (
            <div className="edit-field" style={{ display: 'grid', gap: '0.35rem', marginTop: '0.75rem' }}>
              <strong>Add Signature</strong>
              <input
                className="edit-input"
                placeholder="Signature name"
                value={sigDraft.signature_name}
                onChange={(e) => setSigDraft((d) => ({ ...d, signature_name: e.target.value }))}
              />
              <input
                className="edit-input"
                placeholder="source_rep_name (e.g. Tyler Sullivan)"
                value={sigDraft.source_rep_name}
                onChange={(e) => setSigDraft((d) => ({ ...d, source_rep_name: e.target.value }))}
              />
              <select
                value={String(sigDraft.email_account_id)}
                onChange={(e) =>
                  setSigDraft((d) => ({ ...d, email_account_id: e.target.value }))
                }
              >
                <option value="">Any / client-level</option>
                {emailAccounts.map((a) => (
                  <option key={a.account_id} value={a.account_id}>
                    {a.email_address}
                  </option>
                ))}
              </select>
              <textarea
                rows={6}
                style={{ width: '100%' }}
                placeholder="Signature body"
                value={sigDraft.signature_body}
                onChange={(e) => setSigDraft((d) => ({ ...d, signature_body: e.target.value }))}
              />
              <label>
                <input
                  type="checkbox"
                  checked={sigDraft.is_default}
                  onChange={(e) => setSigDraft((d) => ({ ...d, is_default: e.target.checked }))}
                />{' '}
                Default for this account scope
              </label>
              <button
                type="button"
                className="primary-btn"
                disabled={emailBusy}
                onClick={() => void onCreateSignature()}
              >
                Save Signature
              </button>
            </div>
          ) : null}
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>CLIENT OPERATIONS</h2>
            <span className="queue-source">Approved operational knowledge only</span>
          </div>
          <p className="queue-sub">
            How NorthStar works with this client — email identities, appointment handling, reporting,
            handoffs, and workflow instructions. Not for passwords, strategy, capabilities, or ICP.
          </p>
          {opsItems.length === 0 ? (
            <p className="empty-state">
              No approved Client Operations items yet. Map Unmapped Intelligence proposals to Client
              Operations in Strategy Import Review, then Save + Approve.
            </p>
          ) : (
            <ul className="ask-history-list">
              {opsItems.map((it, idx) => {
                const linked = contactsLinkedInText(
                  String(it.content || ''),
                  allClientContacts,
                )
                return (
                  <li key={String(it.item_id || it.proposal_id || idx)}>
                    <strong>{display(String(it.title || it.field_key || 'Operational note'))}</strong>
                    <div style={{ marginTop: '0.35rem', whiteSpace: 'pre-wrap' }}>
                      {display(String(it.content || ''))}
                    </div>
                    {linked.length > 0 ? (
                      <div className="queue-sub" style={{ marginTop: '0.4rem' }}>
                        Linked Client Contacts:{' '}
                        {linked
                          .map((c) =>
                            [c.name, c.email, c.role_type].filter(Boolean).join(' · '),
                          )
                          .join('; ')}
                      </div>
                    ) : null}
                    <div className="queue-sub" style={{ marginTop: '0.35rem' }}>
                      Source: {display(String(it.source_document || ''))}
                      {it.source_snippet
                        ? ` · Evidence: ${String(it.source_snippet).slice(0, 160)}`
                        : ''}
                    </div>
                    <div className="queue-sub">
                      {display(String(it.status || 'Approved'))}
                      {it.approved_at ? ` · ${String(it.approved_at)}` : ''}
                      {it.approved_by ? ` · by ${String(it.approved_by)}` : ''}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
        </>
      )}

      {tab === 'contacts' && (
        <section className="panel">
          <div className="panel-header">
            <h2>CLIENT CONTACTS</h2>
            <span className="queue-source">People who work for this NorthStar client</span>
          </div>
          <p className="queue-sub">
            Structured people records for this client only — separate from CRM prospect contacts.
            Deactivate instead of hard-delete.
          </p>
          {hub.can_edit && (
            <div className="setup-grid" style={{ marginBottom: '1rem' }}>
              <label className="setup-field">
                <span className="setup-field-label">Name</span>
                <input
                  value={contactForm.name}
                  onChange={(e) => setContactForm((f) => ({ ...f, name: e.target.value }))}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Title</span>
                <input
                  value={contactForm.title}
                  onChange={(e) => setContactForm((f) => ({ ...f, title: e.target.value }))}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Email</span>
                <input
                  value={contactForm.email}
                  onChange={(e) => setContactForm((f) => ({ ...f, email: e.target.value }))}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Phone</span>
                <input
                  value={contactForm.phone}
                  onChange={(e) => setContactForm((f) => ({ ...f, phone: e.target.value }))}
                />
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Role</span>
                <select
                  value={contactForm.role_type}
                  onChange={(e) => setContactForm((f) => ({ ...f, role_type: e.target.value }))}
                >
                  <option value="">(none)</option>
                  {roleTypes.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Notes</span>
                <input
                  value={contactForm.notes}
                  onChange={(e) => setContactForm((f) => ({ ...f, notes: e.target.value }))}
                />
              </label>
              <div className="setup-actions" style={{ alignSelf: 'end' }}>
                <button type="button" className="primary-btn" onClick={() => void onSaveContact()}>
                  {editingContactId ? 'Save contact' : 'Add contact'}
                </button>
                {editingContactId ? (
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => {
                      setEditingContactId(null)
                      setContactForm({ ...EMPTY_CONTACT_FORM })
                    }}
                  >
                    Cancel edit
                  </button>
                ) : null}
              </div>
            </div>
          )}
          <label className="queue-sub">
            <input
              type="checkbox"
              checked={showInactiveContacts}
              onChange={(e) => setShowInactiveContacts(e.target.checked)}
            />{' '}
            Show inactive
          </label>
          <table className="data-table" style={{ marginTop: '0.75rem' }}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Title</th>
                <th>Email</th>
                <th>Phone</th>
                <th>Role</th>
                <th>Active</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleContacts.length === 0 ? (
                <tr>
                  <td colSpan={7}>No Client Contacts yet for this client.</td>
                </tr>
              ) : (
                visibleContacts.map((c) => (
                  <tr key={c.contact_id}>
                    <td>{display(c.name)}</td>
                    <td>{display(c.title)}</td>
                    <td>{display(c.email)}</td>
                    <td>{display(c.phone)}</td>
                    <td>{display(c.role_type)}</td>
                    <td>{c.active ? 'Yes' : 'No'}</td>
                    <td>
                      {hub.can_edit ? (
                        <div className="setup-actions" style={{ marginTop: 0 }}>
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => beginEditContact(c)}
                          >
                            Edit
                          </button>
                          {c.active ? (
                            <button
                              type="button"
                              className="link-btn"
                              onClick={() => void onDeactivateContact(c)}
                            >
                              Deactivate
                            </button>
                          ) : null}
                        </div>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>
      )}

      {tab === 'appointments' && (
        <section className="panel">
          <div className="panel-header">
            <h2>APPOINTMENTS / ENGAGEMENT IMPORT</h2>
          </div>
          <p className="queue-sub">
            Upload → Select Sheets → Preview. Nothing becomes CRM/history until Confirm Import.
          </p>

          <div className="ask-section">
            <h3>
              Imported history ({importedEvents.length}) · appointment family{' '}
              {
                importedEvents.filter((e) =>
                  String(e.event_type || '').startsWith('Appointment'),
                ).length
              }{' '}
              · Send Information{' '}
              {
                importedEvents.filter((e) => e.event_type === 'Send Information')
                  .length
              }{' '}
              · RFQ {importedEvents.filter((e) => e.event_type === 'RFQ').length}
            </h3>
            {importedEvents.length === 0 ? (
              <p className="queue-sub">No imported sales events for this client yet.</p>
            ) : (
              <ul className="ask-history-list">
                {importedEvents.slice(0, 40).map((ev) => {
                  const recordNo = String(ev.source_record_number || '')
                  const contactId = Number(ev.contact_id || 0)
                  return (
                    <li key={String(ev.event_id)}>
                      <strong>{String(ev.event_type || '')}</strong>
                      <span className="queue-sub">
                        {' '}
                        · {display(String(ev.event_date || ev.source_date_time_text || ''))}
                        {' · '}
                        {recordNo ? (
                          <Link
                            to={`/companies/${encodeURIComponent(recordNo)}?client_id=${clientId}`}
                          >
                            {display(String(ev.company_name || ''))}
                          </Link>
                        ) : (
                          display(String(ev.company_name || ''))
                        )}
                        {' · '}
                        {contactId > 0 ? (
                          <Link to={`/contacts/${contactId}?client_id=${clientId}`}>
                            {display(String(ev.contact_name || ''))}
                          </Link>
                        ) : (
                          display(String(ev.contact_name || ''))
                        )}
                      </span>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>

          {hub.can_edit && (
            <div className="setup-actions" style={{ marginBottom: '1rem' }}>
              <label className="link-btn">
                Upload Appointment / Engagement Workbook
                <input
                  type="file"
                  accept=".csv,.xlsx"
                  hidden
                  disabled={uploading}
                  onChange={(e) => void onEngagementUpload(e.target.files?.[0] || null)}
                />
              </label>
            </div>
          )}

          {engBatch && (
            <div className="ask-section">
              <h3>
                Batch #{engBatch.batch_id} · {engBatch.status} · {engBatch.filename}
              </h3>
              <h4>Select Sheets</h4>
              <div className="setup-grid">
                {engBatch.sheets.map((s) => (
                  <label key={s.sheet_id} className="setup-field">
                    <span className="setup-field-label">
                      {s.sheet_name} ({s.row_count} rows{s.is_empty ? ', empty' : ''})
                    </span>
                    <select
                      value={sheetTypes[s.sheet_id] || s.user_type}
                      onChange={(e) =>
                        setSheetTypes((p) => ({ ...p, [s.sheet_id]: e.target.value }))
                      }
                    >
                      {(engBatch.sheet_type_options || []).map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </select>
                    <span className="queue-sub">Detected: {s.detected_type}</span>
                  </label>
                ))}
              </div>
              <div className="setup-actions">
                <button type="button" className="primary-btn" onClick={() => void onEngagementPreview()}>
                  Save sheet types &amp; Preview
                </button>
              </div>
              <h4>Column mapping (review before Confirm Import)</h4>
              {engBatch.sheets
                .filter((s) => !(sheetTypes[s.sheet_id] || s.user_type) || (sheetTypes[s.sheet_id] || s.user_type) !== 'Ignore')
                .map((s) => {
                  const mapping = s.column_mapping || {}
                  const entries = Object.entries(mapping)
                  if (entries.length === 0) {
                    return (
                      <p key={s.sheet_id} className="queue-sub">
                        {s.sheet_name}: mapping appears after Preview.
                      </p>
                    )
                  }
                  return (
                    <div key={s.sheet_id} className="ask-section">
                      <strong>{s.sheet_name}</strong>
                      <ul className="ask-bullet-list">
                        {entries.map(([field, header]) => (
                          <li key={`${s.sheet_id}-${field}`}>
                            <strong>{field}</strong> ← {String(header)}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )
                })}
            </div>
          )}

          {engPreview && (
            <div className="ask-section">
              <h3>Preview summary</h3>
              {(engPreview.batch?.sheets || []).some((s) => Object.keys(s.column_mapping || {}).length > 0) && (
                <>
                  <h4>Confirmed column mapping</h4>
                  {(engPreview.batch?.sheets || [])
                    .filter((s) => Object.keys(s.column_mapping || {}).length > 0)
                    .map((s) => (
                      <div key={s.sheet_id} className="ask-section">
                        <strong>
                          {s.sheet_name} → {s.user_type || s.detected_type}
                        </strong>
                        <ul className="ask-bullet-list">
                          {Object.entries(s.column_mapping || {}).map(([field, header]) => (
                            <li key={`${s.sheet_id}-prev-${field}`}>
                              <strong>{field}</strong> ← {String(header)}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                </>
              )}
              <ul className="ask-bullet-list">
                {Object.entries(engPreview.summary || {}).map(([k, v]) => (
                  <li key={k}>
                    <strong>{k.replace(/_/g, ' ')}</strong>: {String(v)}
                  </li>
                ))}
              </ul>
              <div className="setup-actions">
                {['', 'Matched', 'Possible Match', 'New', 'Conflict', 'Duplicate', 'Needs Review'].map(
                  (f) => (
                    <button
                      key={f || 'all'}
                      type="button"
                      className="link-btn"
                      onClick={() => {
                        void (async () => {
                          setPreviewFilter(f)
                          if (!engBatch) return
                          const preview = await fetchEngagementPreview(
                            clientId,
                            engBatch.batch_id,
                            f || undefined,
                          )
                          setEngPreview(preview)
                        })()
                      }}
                    >
                      {f || 'All'}
                    </button>
                  ),
                )}
              </div>
              <p className="queue-sub">Filter: {previewFilter || 'All'} · Confirm Import not run in this pass.</p>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Sheet</th>
                    <th>Row</th>
                    <th>Record</th>
                    <th>Company</th>
                    <th>Co Match</th>
                    <th>Contact</th>
                    <th>Ct Match</th>
                    <th>Event</th>
                    <th>Date/Time</th>
                    <th>Rev Spec</th>
                    <th>Grade</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {engPreview.rows.map((r) => (
                    <tr key={r.row_id} onClick={() => setDetailRow(r)} style={{ cursor: 'pointer' }}>
                      <td>{r.sheet_name}</td>
                      <td>{r.source_row_number}</td>
                      <td>{display(r.company_record_no)}</td>
                      <td>
                        {r.company_record_no ? (
                          <Link
                            to={`/companies/${encodeURIComponent(r.company_record_no)}?client_id=${clientId}`}
                            onClick={(e) => e.stopPropagation()}
                          >
                            {display(r.company_name)}
                          </Link>
                        ) : (
                          display(r.company_name)
                        )}
                      </td>
                      <td>{r.company_match_status}</td>
                      <td>
                        {r.contact_id ? (
                          <Link
                            to={`/contacts/${r.contact_id}?client_id=${clientId}`}
                            onClick={(e) => e.stopPropagation()}
                          >
                            {display(r.contact_name)}
                          </Link>
                        ) : (
                          display(r.contact_name)
                        )}
                      </td>
                      <td>{r.contact_match_status}</td>
                      <td>{r.event_type}</td>
                      <td>{display(r.source_date_time_text || r.event_date)}</td>
                      <td>{display(r.source_rev_spec_text)}</td>
                      <td>{display(r.source_appointment_grade)}</td>
                      <td>
                        {r.duplicate_status ||
                          (r.needs_review ? 'Needs Review' : r.client_validation)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {detailRow && (
                <div className="ask-section">
                  <h4>
                    Row detail · {detailRow.sheet_name} #{detailRow.source_row_number}
                  </h4>
                  <dl className="ask-fact-grid">
                    <div>
                      <dt>Caller Notes</dt>
                      <dd>{display(detailRow.mapped.caller_notes)}</dd>
                    </div>
                    <div>
                      <dt>Sales Notes</dt>
                      <dd>{display(detailRow.mapped.sales_notes)}</dd>
                    </div>
                    <div>
                      <dt>Phone / Email</dt>
                      <dd>
                        {display(detailRow.mapped.phone)} · {display(detailRow.mapped.email)}
                      </dd>
                    </div>
                    <div>
                      <dt>Appointment text</dt>
                      <dd>{display(detailRow.source_date_time_text)}</dd>
                    </div>
                    <div>
                      <dt>Quoted / Outcome</dt>
                      <dd>
                        {display(detailRow.mapped.dollars_quoted)} ·{' '}
                        {display(detailRow.mapped.outcome)}
                      </dd>
                    </div>
                  </dl>
                </div>
              )}
            </div>
          )}
        </section>
      )}

      {tab === 'documents' && (
        <section className="panel">
          <div className="panel-header">
            <h2>DOCUMENT LIBRARY</h2>
          </div>
          {hub.can_edit && (
            <div className="setup-grid" style={{ marginBottom: '1rem' }}>
              <label className="setup-field">
                <span className="setup-field-label">Document type</span>
                <select value={docType} onChange={(e) => setDocType(e.target.value)}>
                  {(hub.document_types || []).map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
              <label className="setup-field">
                <span className="setup-field-label">Upload (.docx / .xlsx / .csv)</span>
                <input
                  type="file"
                  accept=".docx,.xlsx,.csv"
                  disabled={uploading}
                  onChange={(e) => void onUpload(e.target.files?.[0] || null)}
                />
              </label>
            </div>
          )}
          <label className="queue-sub">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
            />{' '}
            Show archived
          </label>
          <table className="data-table" style={{ marginTop: '0.75rem' }}>
            <thead>
              <tr>
                <th>Document Name</th>
                <th>Type</th>
                <th>Uploaded</th>
                <th>By</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {docs.length === 0 ? (
                <tr>
                  <td colSpan={6}>No documents for this client.</td>
                </tr>
              ) : (
                docs.map((d) => (
                  <tr key={d.document_id}>
                    <td>
                      {d.filename}
                      {d.sensitive_flag ? (
                        <div className="queue-sub">{display(d.sensitive_note)}</div>
                      ) : null}
                      {d.archived ? <div className="queue-sub">Archived</div> : null}
                    </td>
                    <td>{d.document_type}</td>
                    <td>{display(d.uploaded_at)}</td>
                    <td>{display(d.uploaded_by)}</td>
                    <td>{d.processing_status}</td>
                    <td>
                      <div className="setup-actions" style={{ marginTop: 0 }}>
                        <a
                          className="link-btn"
                          href={`/api/clients/${clientId}/knowledge/documents/${d.document_id}/file`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          View
                        </a>
                        {hub.can_edit && !d.archived && (
                          <>
                            <button type="button" className="link-btn" onClick={() => void onProcess(d)}>
                              Process
                            </button>
                            <label className="link-btn">
                              Replace
                              <input
                                type="file"
                                accept=".docx,.xlsx,.csv"
                                hidden
                                onChange={(e) =>
                                  void onUpload(e.target.files?.[0] || null, d.document_id)
                                }
                              />
                            </label>
                            <button type="button" className="link-btn" onClick={() => void onArchive(d)}>
                              Archive
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>
      )}

      {tab === 'review' && (
        <section className="panel">
          <div className="panel-header">
            <h2>STRATEGY IMPORT REVIEW</h2>
          </div>
          <p className="queue-sub">
            Extracted proposals never auto-write Client Setup. Approve individually or use Approve
            All Matches for high-confidence matches. New Information still requires individual
            human review — there is no bulk Approve. Use Edit + Approve to reassign Unmapped
            Intelligence to <strong>Client Operations</strong> or <strong>Client Contacts</strong>{' '}
            before saving. Multi-person Client Contacts must be <strong>Split</strong> into
            individual people before approval. Use <strong>Resolve / Incorporated</strong> (single
            or bulk) when valid source intelligence is already represented in approved/stored
            knowledge. Bulk Reject requires a category confirmation.
          </p>
          <div className="setup-actions" style={{ marginBottom: '1rem', gap: '0.5rem', flexWrap: 'wrap' }}>
            {(
              [
                ['Pending', 'Pending'],
                ['Already Incorporated', 'Already Incorporated'],
                ['Needs Review', 'Needs Review'],
                ['Noise', 'Noise'],
                ['Sensitive', 'Sensitive'],
                ['Resolved', 'Resolved'],
                ['Rejected', 'Rejected'],
                ['All', 'All'],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={reviewStatusFilter === value ? 'primary-btn' : 'link-btn'}
                onClick={() => {
                  setReviewStatusFilter(value)
                  setSelectedProposalIds([])
                  setBulkResolvePreview(null)
                  setBulkRejectOpen(false)
                  cancelTemplateEditor()
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {bulkSummary && (
            <div className="ask-section" style={{ marginBottom: '1rem' }}>
              {bulkSummary.kind === 'resolve' ? (
                <>
                  <p>
                    <strong>{bulkSummary.result.resolved_count}</strong> proposals resolved ·{' '}
                    <strong>{bulkSummary.result.skipped_count}</strong>
                    {bulkSummary.result.skipped_count > 0 ? ' skipped — review required' : ' skipped'}{' '}
                    · <strong>{bulkSummary.result.error_count}</strong> errors
                  </p>
                  {bulkSummary.result.skipped_count > 0 ? (
                    <>
                      <button
                        type="button"
                        className="link-btn"
                        onClick={() => setShowSkippedDetails((v) => !v)}
                      >
                        {showSkippedDetails ? 'Hide skipped reasons' : 'Show skipped reasons'}
                      </button>
                      {showSkippedDetails ? (
                        <ul className="ask-history-list">
                          {bulkSummary.result.skipped.map((s) => (
                            <li key={s.proposal_id}>
                              #{s.proposal_id}
                              {s.field_label ? ` · ${s.field_label}` : ''} — {s.reason}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </>
                  ) : null}
                </>
              ) : (
                <>
                  <p>
                    <strong>{bulkSummary.result.rejected_count}</strong> proposals rejected ·{' '}
                    <strong>{bulkSummary.result.skipped_count}</strong> skipped
                  </p>
                  {bulkSummary.result.skipped_count > 0 ? (
                    <>
                      <button
                        type="button"
                        className="link-btn"
                        onClick={() => setShowSkippedDetails((v) => !v)}
                      >
                        {showSkippedDetails ? 'Hide skipped reasons' : 'Show skipped reasons'}
                      </button>
                      {showSkippedDetails ? (
                        <ul className="ask-history-list">
                          {bulkSummary.result.skipped.map((s) => (
                            <li key={s.proposal_id}>
                              #{s.proposal_id} — {s.reason}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </>
                  ) : null}
                </>
              )}
              <button type="button" className="link-btn" onClick={() => setBulkSummary(null)}>
                Dismiss
              </button>
            </div>
          )}
          {splitPreview && (
            <div className="ask-section" style={{ marginBottom: '1rem' }}>
              <h3>
                Split Client Contacts · proposal #{splitPreview.proposal_id} (
                {splitPreview.person_count} people)
              </h3>
              <p className="queue-sub">
                Creates individual Pending proposals only. Does not approve the parent or create
                contacts yet.
              </p>
              <ul className="ask-history-list">
                {splitPreview.people.map((person, idx) => (
                  <li key={`${person.name}-${idx}`}>
                    <strong>{display(person.name)}</strong>
                    {person.email ? ` · ${person.email}` : ''}
                    {person.title ? ` · ${person.title}` : ''}
                    {person.possible_duplicate_name ? (
                      <div className="queue-sub">
                        Possible duplicate: {person.possible_duplicate_name} (id=
                        {person.possible_duplicate_id})
                      </div>
                    ) : null}
                    {person.needs_review ? (
                      <div className="queue-sub">Needs review before approve</div>
                    ) : null}
                  </li>
                ))}
              </ul>
              <div className="setup-actions">
                <button
                  type="button"
                  className="primary-btn"
                  disabled={splitBusy || splitPreview.person_count === 0}
                  onClick={() => void onCreateSplitProposals()}
                >
                  {splitBusy ? 'Creating…' : 'Create individual Pending proposals'}
                </button>
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => setSplitPreview(null)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          {hub.can_edit && (
            <div className="setup-actions" style={{ marginBottom: '1rem', gap: '0.5rem', flexWrap: 'wrap' }}>
              <button type="button" className="primary-btn" onClick={() => void onApproveMatches()}>
                Approve All Matches
              </button>
              <button
                type="button"
                className="link-btn"
                disabled={reviewProposals.every((p) => p.status !== 'Pending')}
                onClick={() => selectAllVisible()}
              >
                Select All Visible
              </button>
              <button
                type="button"
                className="link-btn"
                disabled={selectedProposalIds.length === 0}
                onClick={() => clearSelection()}
              >
                Clear Selection
              </button>
              {selectedProposalIds.length > 0 ? (
                <span className="queue-sub">{selectedProposalIds.length} selected</span>
              ) : null}
            </div>
          )}
          {hub.can_edit && selectedProposalIds.length > 0 && (
            <div className="setup-actions" style={{ marginBottom: '1rem', gap: '0.5rem', flexWrap: 'wrap' }}>
              <button
                type="button"
                className="primary-btn"
                disabled={bulkBusy}
                onClick={() => void onOpenBulkResolve()}
              >
                Resolve / Incorporated Selected
              </button>
              <button
                type="button"
                className="link-btn"
                disabled={bulkBusy}
                onClick={() => {
                  setBulkRejectOpen(true)
                  setBulkResolvePreview(null)
                }}
              >
                Reject Selected
              </button>
              <button type="button" className="link-btn" onClick={() => clearSelection()}>
                Clear Selection
              </button>
            </div>
          )}
          {bulkResolvePreview && (
            <div className="ask-section" style={{ marginBottom: '1rem' }}>
              <h3>Confirm Resolve / Incorporated</h3>
              <p className="queue-sub">
                Re-verified against current approved/stored knowledge. Only READY proposals will be
                resolved. Writes review status only — not Client Setup, Contacts, or CRM.
              </p>
              <p>
                Selected: <strong>{bulkResolvePreview.selected_count}</strong> · Eligible to
                Resolve: <strong>{bulkResolvePreview.eligible_count}</strong> · Not Eligible:{' '}
                <strong>{bulkResolvePreview.not_eligible_count}</strong>
              </p>
              {bulkResolvePreview.eligible.length > 0 ? (
                <>
                  <h4>READY</h4>
                  <ul className="ask-history-list">
                    {bulkResolvePreview.eligible.map((e) => (
                      <li key={e.proposal_id}>
                        #{e.proposal_id}
                        {e.field_label ? ` · ${e.field_label}` : ''} — {e.reason}
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
              {bulkResolvePreview.not_eligible.length > 0 ? (
                <>
                  <h4>NOT READY (remain Pending)</h4>
                  <ul className="ask-history-list">
                    {bulkResolvePreview.not_eligible.map((e) => (
                      <li key={e.proposal_id}>
                        #{e.proposal_id}
                        {e.field_label ? ` · ${e.field_label}` : ''} — {e.reason}
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
              <div className="setup-actions">
                <button
                  type="button"
                  className="primary-btn"
                  disabled={bulkBusy || bulkResolvePreview.eligible_count === 0}
                  onClick={() => void onConfirmBulkResolve()}
                >
                  {bulkBusy ? 'Resolving…' : 'Confirm Resolve Eligible'}
                </button>
                <button
                  type="button"
                  className="link-btn"
                  disabled={bulkBusy}
                  onClick={() => setBulkResolvePreview(null)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          {bulkRejectOpen && (
            <div className="ask-section" style={{ marginBottom: '1rem' }}>
              <h3>Confirm Reject Selected</h3>
              <p className="queue-sub">
                Selected: {selectedProposalIds.length}. Rejection updates proposal review status
                only. Sensitive values are never shown here.
              </p>
              <label className="edit-field__label">Rejection category (required)</label>
              <select
                className="edit-select"
                value={rejectCategory}
                onChange={(e) => setRejectCategory(e.target.value)}
              >
                <option value="">Select category…</option>
                {REJECT_CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <label className="edit-field__label" style={{ marginTop: '0.5rem' }}>
                Note (optional)
              </label>
              <input
                className="edit-input"
                value={rejectNote}
                onChange={(e) => setRejectNote(e.target.value)}
                placeholder={
                  rejectCategory.startsWith('Sensitive')
                    ? 'Do not paste secret values'
                    : 'Optional note'
                }
              />
              <div className="setup-actions" style={{ marginTop: '0.75rem' }}>
                <button
                  type="button"
                  className="primary-btn"
                  disabled={bulkBusy || !rejectCategory}
                  onClick={() => void onConfirmBulkReject()}
                >
                  {bulkBusy ? 'Rejecting…' : 'Confirm Reject'}
                </button>
                <button
                  type="button"
                  className="link-btn"
                  disabled={bulkBusy}
                  onClick={() => {
                    setBulkRejectOpen(false)
                    setRejectCategory('')
                    setRejectNote('')
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          {reviewProposals.length === 0 ? (
            <p className="queue-sub">
              No {reviewStatusFilter === 'All' ? '' : `${reviewStatusFilter} `}proposals.
              {reviewStatusFilter === 'Pending'
                ? ' Process a document to generate review items.'
                : REVIEW_CLASS_FILTERS.includes(reviewStatusFilter)
                  ? ' These filters classify Pending proposals only; they do not change status.'
                  : ''}
            </p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th />
                  <th>Field</th>
                  <th>Current</th>
                  <th>Proposed</th>
                  <th>Source Document</th>
                  <th>Evidence</th>
                  <th>Confidence</th>
                  <th>Classification</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {reviewProposals.map((p) => {
                  const selectable = p.status === 'Pending'
                  const enrichment = parseEnrichment(p)
                  const analysis = incorporationById[p.proposal_id]
                  const alreadySplit = isAlreadySplitParent(p)
                  const expectedSplit = splitIntoCount(p)
                  const approvedChildCount =
                    analysis?.suggested_resolved_by_proposal_ids?.length ?? null
                  const showResolvePanel = resolveProposalId === p.proposal_id
                  const showTemplatePanel = templateSeedId === p.proposal_id
                  const reviewClass = reviewClassById[p.proposal_id]
                  return (
                    <Fragment key={p.proposal_id}>
                    <tr>
                      <td>
                        {hub.can_edit && selectable ? (
                          <input
                            type="checkbox"
                            checked={selectedProposalIds.includes(p.proposal_id)}
                            onChange={(e) => {
                              setSelectedProposalIds((prev) =>
                                e.target.checked
                                  ? [...prev, p.proposal_id]
                                  : prev.filter((id) => id !== p.proposal_id),
                              )
                            }}
                          />
                        ) : null}
                      </td>
                      <td>
                        <strong>{display(p.field_label || p.field_name)}</strong>
                        <div className="queue-sub">{p.section}</div>
                        <div className="queue-sub">
                          {p.status_label || p.status}
                          {p.proposal_id ? ` · #${p.proposal_id}` : ''}
                        </div>
                        {reviewClass?.review_class ? (
                          <div className="queue-sub">
                            Review: {reviewClass.review_class.replace(/_/g, ' ')}
                            {reviewClass.ready ? ' · ready to resolve' : ''}
                          </div>
                        ) : null}
                        {alreadySplit ? (
                          <div className="queue-sub">
                            Split Completed
                            {expectedSplit != null
                              ? ` · ${approvedChildCount ?? '…'}/${expectedSplit} children approved`
                              : null}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        {enrichment ? (
                          <div>
                            <div>
                              <strong>{display(enrichment.name)}</strong>
                            </div>
                            <div className="queue-sub">
                              {enrichment.field_name}:{' '}
                              {display(enrichment.existing_value || p.existing_value)}
                            </div>
                          </div>
                        ) : (
                          display(p.existing_value)
                        )}
                      </td>
                      <td>
                        {editProposalId === p.proposal_id ? (
                          <div className="edit-field" style={{ display: 'grid', gap: '0.4rem' }}>
                            <label className="edit-field__label">Destination section</label>
                            <select
                              className="edit-select"
                              value={editSection}
                              onChange={(e) => onDestinationSectionChange(e.target.value)}
                            >
                              {(sectionCatalog.length
                                ? sectionCatalog
                                : [
                                    {
                                      section_key: 'client_operations',
                                      display_name: 'Client Operations',
                                      fields: FALLBACK_OPS_FIELDS,
                                    },
                                    {
                                      section_key: 'unmapped',
                                      display_name: 'Unmapped Intelligence',
                                      fields: [
                                        {
                                          field_name: 'unmapped_intelligence',
                                          field_label: 'Unmapped Intelligence',
                                        },
                                      ],
                                    },
                                  ]
                              ).map((s) => (
                                <option key={s.section_key} value={s.section_key}>
                                  {s.display_name}
                                </option>
                              ))}
                            </select>
                            <label className="edit-field__label">Field</label>
                            <select
                              className="edit-select"
                              value={editField}
                              onChange={(e) => {
                                const fk = e.target.value
                                setEditField(fk)
                                const label =
                                  editFieldsForSection.find((f) => f.field_name === fk)
                                    ?.field_label || ''
                                if (label) setEditTitle(label)
                              }}
                            >
                              {editFieldsForSection.map((f) => (
                                <option key={f.field_name} value={f.field_name}>
                                  {f.field_label}
                                </option>
                              ))}
                            </select>
                            <label className="edit-field__label">Item title</label>
                            <input
                              className="edit-input"
                              value={editTitle}
                              onChange={(e) => setEditTitle(e.target.value)}
                              placeholder="e.g. NorthStar Client Email"
                            />
                            <label className="edit-field__label">Extracted text</label>
                            <textarea
                              value={editValue}
                              onChange={(e) => setEditValue(e.target.value)}
                              rows={4}
                              style={{ width: '100%' }}
                            />
                          </div>
                        ) : enrichment ? (
                          <div>
                            <div>
                              Proposed {enrichment.field_name}:{' '}
                              <strong>{display(enrichment.proposed_value)}</strong>
                            </div>
                            <div className="queue-sub">
                              Contact #{enrichment.contact_id} · {display(enrichment.email)}
                            </div>
                          </div>
                        ) : (
                          <div>
                            {display(p.proposed_value)}
                            {analysis ? (
                              <div className="queue-sub" style={{ marginTop: '0.35rem' }}>
                                {analysis.can_suggest_resolve
                                  ? 'Suggestion: all extracted contacts appear handled (confirm to resolve).'
                                  : null}
                                {analysis.unresolved_summary.length > 0 ? (
                                  <div>
                                    Unresolved: {analysis.unresolved_summary.join('; ')}
                                  </div>
                                ) : null}
                              </div>
                            ) : null}
                          </div>
                        )}
                      </td>
                      <td>
                        {display(p.document_filename)}
                        <div className="queue-sub">{display(p.document_type)}</div>
                      </td>
                      <td>
                        <div className="queue-sub">
                          {display(p.raw_source_text || p.source_reference)}
                        </div>
                        {p.source_locator ? (
                          <div className="queue-sub">{p.source_locator}</div>
                        ) : null}
                        {p.resolution_reason ? (
                          <div className="queue-sub">Resolved: {p.resolution_reason}</div>
                        ) : null}
                      </td>
                      <td>{p.confidence}</td>
                      <td>{p.classification}</td>
                      <td>
                        {hub.can_edit &&
                        p.section === 'email_templates' &&
                        p.status === 'Rejected' ? (
                          <button
                            type="button"
                            className="primary-btn"
                            disabled={templateBusy}
                            onClick={() => void onBuildTemplate(p.proposal_id)}
                          >
                            Build Template
                          </button>
                        ) : null}
                        {hub.can_edit &&
                        p.status === 'Pending' &&
                        p.classification !== 'SENSITIVE — NOT IMPORTED' ? (
                          <div className="setup-actions" style={{ marginTop: 0 }}>
                            {editProposalId === p.proposal_id ? (
                              <>
                                <button
                                  type="button"
                                  className="primary-btn"
                                  onClick={() => void onEditApprove(p)}
                                >
                                  Save + Approve
                                  {editSection ? ` → ${editSection}` : ''}
                                </button>
                                <button
                                  type="button"
                                  className="link-btn"
                                  onClick={() => {
                                    setEditProposalId(null)
                                    setEditValue('')
                                    setEditSection('')
                                    setEditField('')
                                    setEditTitle('')
                                  }}
                                >
                                  Cancel
                                </button>
                              </>
                            ) : (
                              <>
                                {isMultiPersonContactProposal(p) && !alreadySplit ? (
                                  <button
                                    type="button"
                                    className="primary-btn"
                                    onClick={() => void onPreviewSplit(p)}
                                  >
                                    Split into contacts
                                  </button>
                                ) : alreadySplit ? (
                                  <span className="queue-sub">
                                    Split Completed
                                    {expectedSplit != null
                                      ? ` · ${approvedChildCount ?? '…'}/${expectedSplit} children approved`
                                      : ''}
                                  </span>
                                ) : p.section === 'email_templates' ? (
                                  <button
                                    type="button"
                                    className="primary-btn"
                                    disabled={templateBusy}
                                    onClick={() => void onBuildTemplate(p.proposal_id)}
                                  >
                                    Build Template
                                  </button>
                                ) : (
                                  <button
                                    type="button"
                                    className="link-btn"
                                    onClick={() => void onReview(p, 'Approved')}
                                  >
                                    {enrichment ? 'Approve Update' : 'Approve'}
                                  </button>
                                )}
                                <button
                                  type="button"
                                  className="link-btn"
                                  onClick={() => beginEditProposal(p)}
                                >
                                  Edit + Approve
                                </button>
                                <button
                                  type="button"
                                  className="link-btn"
                                  onClick={() => void onReview(p, 'Rejected')}
                                >
                                  Reject
                                </button>
                                <button
                                  type="button"
                                  className="link-btn"
                                  onClick={(e) => {
                                    e.preventDefault()
                                    e.stopPropagation()
                                    void beginResolve(p)
                                  }}
                                >
                                  Resolve / Incorporated
                                </button>
                                {(p.field_name === 'client_contacts' ||
                                  p.section === 'client_contacts' ||
                                  p.section === 'client_profile') && (
                                  <button
                                    type="button"
                                    className="link-btn"
                                    onClick={() => void loadIncorporation(p.proposal_id)}
                                  >
                                    Analyze incorporation
                                  </button>
                                )}
                              </>
                            )}
                          </div>
                        ) : (
                          <span className="queue-sub">{p.status_label || p.status}</span>
                        )}
                      </td>
                    </tr>
                    {showResolvePanel ? (
                      <tr>
                        <td colSpan={9}>
                          <div
                            className="ask-section"
                            style={{
                              margin: '0.35rem 0 0.75rem',
                              padding: '0.85rem 1rem',
                              border: '1px solid #c5d0dc',
                              background: '#f7fafc',
                            }}
                          >
                            <h3 style={{ marginTop: 0 }}>RESOLVE / INCORPORATED</h3>
                            <p className="queue-sub">
                              Proposal #{p.proposal_id}. Marks valid source intelligence as
                              incorporated elsewhere. Does not create or update Client Contacts.
                            </p>
                            {resolveBusy ? (
                              <p className="queue-sub">Loading incorporation details…</p>
                            ) : null}
                            <label className="setup-field">
                              <span className="setup-field-label">Reason</span>
                              <textarea
                                value={resolveReason}
                                onChange={(e) => setResolveReason(e.target.value)}
                                rows={3}
                                style={{ width: '100%' }}
                                placeholder="Describe how this source was incorporated."
                              />
                            </label>
                            {resolveChildLabels.length > 0 ? (
                              <div style={{ marginTop: '0.65rem' }}>
                                <strong>Approved child proposals</strong>
                                <ul className="ask-history-list">
                                  {resolveChildLabels.map((label) => (
                                    <li key={label}>{label}</li>
                                  ))}
                                </ul>
                              </div>
                            ) : null}
                            {resolveContactLabels.length > 0 ? (
                              <div style={{ marginTop: '0.35rem' }}>
                                <strong>Incorporated Client Contacts</strong>
                                <ul className="ask-history-list">
                                  {resolveContactLabels.map((label) => (
                                    <li key={label}>{label}</li>
                                  ))}
                                </ul>
                              </div>
                            ) : null}
                            <div className="setup-actions" style={{ marginTop: '0.75rem' }}>
                              <button
                                type="button"
                                className="primary-btn"
                                onClick={() => void onConfirmResolve()}
                              >
                                Confirm Resolve
                              </button>
                              <button
                                type="button"
                                className="link-btn"
                                onClick={() => cancelResolve()}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        </td>
                      </tr>
                    ) : null}
                    {showTemplatePanel ? (
                      <tr>
                        <td colSpan={9}>
                          <div
                            className="ask-section"
                            style={{
                              margin: '0.35rem 0 0.75rem',
                              padding: '0.85rem 1rem',
                              border: '1px solid #c5d0dc',
                              background: '#f7fafc',
                            }}
                          >
                            <h3 style={{ marginTop: 0 }}>BUILD TEMPLATE</h3>
                            <p className="queue-sub">
                              Combine email-template extraction pieces into one editable template.
                              Cancel leaves all proposal statuses unchanged.
                            </p>
                            {templateLoading ? (
                              <p className="queue-sub">Building template preview…</p>
                            ) : null}
                            {templateInlineError ? (
                              <p className="data-status data-status--error" role="alert">
                                {templateInlineError}
                              </p>
                            ) : null}
                            {templatePreview ? (
                              <>
                                <p className="queue-sub">
                                  Source proposals: #
                                  {templatePreview.source_proposal_ids.join(', #')} ·{' '}
                                  {display(templatePreview.document_filename)}
                                </p>
                                {templatePreview.name_conflicts.length > 0 ? (
                                  <div
                                    className="data-status data-status--error"
                                    role="alert"
                                    style={{ marginBottom: '0.75rem' }}
                                  >
                                    <strong>POTENTIAL CONTACT NAME CONFLICT</strong>
                                    <ul className="ask-history-list">
                                      {templatePreview.name_conflicts.map((c) => (
                                        <li key={`${c.source_name}-${c.approved_contact_id}`}>
                                          Source: <strong>{c.source_name}</strong>
                                          <div>
                                            Approved Client Contact:{' '}
                                            <strong>{c.approved_contact_name}</strong>
                                          </div>
                                          <div className="queue-sub">{c.note}</div>
                                        </li>
                                      ))}
                                    </ul>
                                    <p className="queue-sub">
                                      Do not silently rewrite. Edit the body if needed, then
                                      acknowledge before approving.
                                    </p>
                                  </div>
                                ) : null}
                                <div
                                  className="edit-field"
                                  style={{ display: 'grid', gap: '0.4rem' }}
                                >
                                  <label className="edit-field__label">Template Name</label>
                                  <input
                                    className="edit-input"
                                    value={templateDraft.template_name}
                                    onChange={(e) =>
                                      setTemplateDraft((d) => ({
                                        ...d,
                                        template_name: e.target.value,
                                      }))
                                    }
                                  />
                                  <label className="edit-field__label">Template Type</label>
                                  <input
                                    className="edit-input"
                                    value={templateDraft.template_type}
                                    onChange={(e) =>
                                      setTemplateDraft((d) => ({
                                        ...d,
                                        template_type: e.target.value,
                                      }))
                                    }
                                  />
                                  <label className="edit-field__label">Subject</label>
                                  <input
                                    className="edit-input"
                                    value={templateDraft.subject}
                                    onChange={(e) =>
                                      setTemplateDraft((d) => ({
                                        ...d,
                                        subject: e.target.value,
                                      }))
                                    }
                                    placeholder="Leave blank unless source provides one"
                                  />
                                  <label className="edit-field__label">Body</label>
                                  <textarea
                                    rows={10}
                                    style={{ width: '100%' }}
                                    value={templateDraft.body}
                                    onChange={(e) =>
                                      setTemplateDraft((d) => ({ ...d, body: e.target.value }))
                                    }
                                  />
                                  <label>
                                    <input
                                      type="checkbox"
                                      checked={templateDraft.is_active}
                                      onChange={(e) =>
                                        setTemplateDraft((d) => ({
                                          ...d,
                                          is_active: e.target.checked,
                                        }))
                                      }
                                    />{' '}
                                    Active
                                  </label>
                                  {templatePreview.name_conflicts.length > 0 ? (
                                    <label>
                                      <input
                                        type="checkbox"
                                        checked={templateDraft.acknowledge_name_conflicts}
                                        onChange={(e) =>
                                          setTemplateDraft((d) => ({
                                            ...d,
                                            acknowledge_name_conflicts: e.target.checked,
                                          }))
                                        }
                                      />{' '}
                                      I reviewed the name conflict(s) and edited/confirmed the body
                                    </label>
                                  ) : null}
                                </div>
                                <div className="setup-actions" style={{ marginTop: '0.75rem' }}>
                                  <button
                                    type="button"
                                    className="primary-btn"
                                    disabled={templateBusy || templateLoading}
                                    onClick={() => void onApproveTemplate()}
                                  >
                                    {templateBusy ? 'Saving…' : 'Save + Approve Template'}
                                  </button>
                                  <button
                                    type="button"
                                    className="link-btn"
                                    disabled={templateBusy}
                                    onClick={() => cancelTemplateEditor()}
                                  >
                                    Cancel
                                  </button>
                                </div>
                              </>
                            ) : !templateLoading ? (
                              <div className="setup-actions">
                                <button
                                  type="button"
                                  className="link-btn"
                                  onClick={() => cancelTemplateEditor()}
                                >
                                  Cancel
                                </button>
                              </div>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    ) : null}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          )}
        </section>
      )}
    </div>
  )
}
