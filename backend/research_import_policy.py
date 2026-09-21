"""RI-3B Research & Custom Prospect Import field policy contract.

This is the safety matrix RI-4 will implement. Isolated confirm may prove
guards; production confirm stays fail-closed. Source type is never authority.
"""
from __future__ import annotations

from typing import Any

# A SAFE CREATE / B SAFE FILL BLANK / C EXPLICIT REVIEW / D PRESERVE EXISTING
# E WORKFLOW-SENSITIVE / F NEVER AUTO-WRITE / G IGNORE

POLICY_A = "SAFE_CREATE"
POLICY_B = "SAFE_FILL_BLANK"
POLICY_C = "EXPLICIT_REVIEW"
POLICY_D = "PRESERVE_EXISTING"
POLICY_E = "WORKFLOW_SENSITIVE"
POLICY_F = "NEVER_AUTO_WRITE"
POLICY_G = "IGNORE"

MASTER_KEEP = "KEEP_EXISTING"
MASTER_FILL = "FILL_BLANK"
MASTER_ACCEPT_INCOMING = "ACCEPT_INCOMING"
MASTER_ADD_LOCATION = "ADD_AS_LOCATION"
MASTER_SKIP = "SKIP_FIELD"

CONTACT_EXACT = "EXACT_CONTACT"
CONTACT_STRONG = "STRONG_CONTACT"
CONTACT_NEW = "NEW_CONTACT"
CONTACT_POSSIBLE = "POSSIBLE_CONTACT_REVIEW"
CONTACT_AMBIGUOUS = "AMBIGUOUS_CONTACT"
CONTACT_INVALID = "INVALID_CONTACT"
CONTACT_SKIPPED = "SKIPPED_CONTACT"

RES_USE_CONTACT = "USE_EXISTING_CONTACT"
RES_CREATE_CONTACT = "CREATE_NEW_CONTACT"
RES_SKIP_CONTACT = "SKIP_CONTACT"

TRUST_RESEARCH = "research_informational"
TRUST_CLIENT = "client_provided"
TRUST_INTERNAL = "internal_northstar"
TRUST_PURCHASED = "purchased_third_party"
TRUST_LEGACY = "legacy_custom"
TRUST_LEADMASTER = "leadmaster_separate"

SOURCE_TRUST = {
    "CHATGPT_DEEP_RESEARCH": TRUST_RESEARCH,
    "AI_RESEARCH_OTHER": TRUST_RESEARCH,
    "CLIENT_PROVIDED": TRUST_CLIENT,
    "INTERNAL_RESEARCH": TRUST_INTERNAL,
    "TRADE_SHOW": TRUST_RESEARCH,
    "ASSOCIATION_DIRECTORY": TRUST_RESEARCH,
    "PURCHASED_LIST": TRUST_PURCHASED,
    "SALESPERSON_PROVIDED": TRUST_INTERNAL,
    "CUSTOM_REPORT": TRUST_LEGACY,
    "OTHER": TRUST_RESEARCH,
}

FIELD_POLICY: dict[str, dict[str, str]] = {
    "company_name": {
        "category": "MASTER_COMPANY",
        "new_company": POLICY_A,
        "existing_company": POLICY_C,
        "summary": "Create on confirmed new company. Existing: SAME no-write; FILL_BLANK explicit; PROPOSE_UPDATE review; MANUAL never auto-write.",
    },
    "website": {
        "category": "MASTER_COMPANY",
        "new_company": POLICY_A,
        "existing_company": POLICY_B,
        "summary": "Eligible FILL_BLANK only under explicit import policy. Updates require review.",
    },
    "phone": {
        "category": "MASTER_COMPANY",
        "new_company": POLICY_A,
        "existing_company": POLICY_B,
        "summary": "Company phone. FILL_BLANK explicit; update review; manual-authority never auto-write.",
    },
    "address": {
        "category": "LOCATION",
        "new_company": POLICY_A,
        "existing_company": POLICY_C,
        "summary": "Address 1. Differing address is POSSIBLE_NEW_LOCATION, not a silent overwrite.",
    },
    "address2": {
        "category": "LOCATION",
        "new_company": POLICY_A,
        "existing_company": POLICY_B,
        "summary": "Address 2. Fill blank explicit; do not clobber.",
    },
    "city": {
        "category": "LOCATION",
        "new_company": POLICY_A,
        "existing_company": POLICY_B,
        "summary": "City. Fill blank explicit with address review.",
    },
    "state": {
        "category": "LOCATION",
        "new_company": POLICY_A,
        "existing_company": POLICY_B,
        "summary": "State/Province. Fill blank explicit with address review.",
    },
    "zip": {
        "category": "LOCATION",
        "new_company": POLICY_A,
        "existing_company": POLICY_B,
        "summary": "Postal code. Fill blank explicit with address review.",
    },
    "country": {
        "category": "LOCATION",
        "new_company": POLICY_A,
        "existing_company": POLICY_B,
        "summary": "Country. Fill blank explicit.",
    },
    "contact_first_name": {"category": "CONTACT", "new_company": POLICY_A, "existing_company": POLICY_C, "summary": "Named contact matching; never department-only."},
    "contact_last_name": {"category": "CONTACT", "new_company": POLICY_A, "existing_company": POLICY_C, "summary": "Named contact matching; never department-only."},
    "contact_full_name": {"category": "CONTACT", "new_company": POLICY_A, "existing_company": POLICY_C, "summary": "Named contact matching; never department-only."},
    "contact_title": {"category": "CONTACT", "new_company": POLICY_A, "existing_company": POLICY_B, "summary": "Title stored on create; fill blank only, never overwrite."},
    "contact_email": {"category": "CONTACT", "new_company": POLICY_A, "existing_company": POLICY_C, "summary": "Exact email at same company is EXACT_CONTACT reuse."},
    "contact_phone": {"category": "CONTACT", "new_company": POLICY_A, "existing_company": POLICY_C, "summary": "Exact 10-digit same company is exact/strong. Last-seven is review."},
    "contact_phone_extension": {"category": "CONTACT", "new_company": POLICY_A, "existing_company": POLICY_B, "summary": "Preserve extension on create/reuse; do not invent."},
    "research_priority": {"category": "CLIENT_PROSPECT", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Research priority is not CRM status."},
    "why_client_fits": {"category": "CLIENT_PROSPECT", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Client-scoped research narrative."},
    "target_market": {"category": "RESEARCH_INTELLIGENCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Structured research attribute; version on later batches."},
    "equipment_product": {"category": "RESEARCH_INTELLIGENCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Structured research attribute; version on later batches."},
    "potential_components": {"category": "RESEARCH_INTELLIGENCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Structured research attribute; version on later batches."},
    "qualification_notes": {"category": "RESEARCH_INTELLIGENCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Research narrative, not CRM Notes unless mapped to imported_notes."},
    "target_department": {"category": "TARGETING", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Department targeting. Not a contact."},
    "product_source": {"category": "SOURCE_PROVENANCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Source URL lineage."},
    "address_source": {"category": "SOURCE_PROVENANCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Source URL lineage."},
    "phone_source": {"category": "SOURCE_PROVENANCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Source URL lineage."},
    "website_source": {"category": "SOURCE_PROVENANCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Source URL lineage."},
    "general_source": {"category": "SOURCE_PROVENANCE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Source URL lineage."},
    "research_date": {"category": "BATCH_LINEAGE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Batch lineage only."},
    "research_method": {"category": "BATCH_LINEAGE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "How intelligence was produced. Not source_type."},
    "research_batch_name": {"category": "BATCH_LINEAGE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Batch name lineage."},
    "original_research_file": {"category": "BATCH_LINEAGE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Original filename lineage."},
    "prior_research_file": {"category": "BATCH_LINEAGE", "new_company": POLICY_A, "existing_company": POLICY_A, "summary": "Prior file lineage."},
    "imported_notes": {
        "category": "NOTES",
        "new_company": POLICY_C,
        "existing_company": POLICY_C,
        "summary": "Write only when explicitly mapped. Append distinct; skip duplicate; never replace.",
    },
    "workflow_status": {"category": "CRM_WORKFLOW", "new_company": POLICY_E, "existing_company": POLICY_E, "summary": "Preview only. Preserve existing CCR status."},
    "workflow_assigned_rep": {"category": "CRM_WORKFLOW", "new_company": POLICY_E, "existing_company": POLICY_E, "summary": "Preview only. Preserve assignment unless a later phase explicitly enables it."},
    "workflow_follow_up": {"category": "CRM_WORKFLOW", "new_company": POLICY_E, "existing_company": POLICY_E, "summary": "Preview only. Preserve follow-up."},
    "workflow_next_action": {"category": "CRM_WORKFLOW", "new_company": POLICY_E, "existing_company": POLICY_E, "summary": "Preview only. Preserve next action."},
    "workflow_hot": {"category": "CRM_WORKFLOW", "new_company": POLICY_E, "existing_company": POLICY_E, "summary": "Preview only. Preserve Hot."},
    "workflow_campaign": {"category": "CRM_WORKFLOW", "new_company": POLICY_E, "existing_company": POLICY_E, "summary": "Preview only. Preserve campaign."},
}

CCR_POLICY = {
    "new_ccr": "Eligible after confirmed company match/create. Default status New. Research priority is separate.",
    "existing_ccr": "Reuse. Do not create a second relationship.",
    "status": POLICY_D,
    "assigned_rep": POLICY_E,
    "hot": POLICY_E,
    "follow_up": POLICY_E,
    "next_action": POLICY_E,
    "campaign": POLICY_E,
}

NOTES_POLICY = {
    "write_when": "Only when a source column is explicitly mapped to imported_notes / Notes.",
    "unmapped": "Never concatenate ignored or unmapped columns into notes.",
    "new_distinct": "Append.",
    "duplicate": "Skip exact/normalized duplicates.",
    "replace": "Never replace existing notes.",
    "scope": "Client-scoped CCR notes.",
    "lineage": "Batch id / source type / filename remain on the research batch, not mixed into note text.",
    "ri3b": "Preview and isolated-confirm proof only. No live notes.",
}

CUSTOM_ATTRIBUTE_POLICY = {
    "create": "Insert on the new current research record.",
    "later_batch": "Mark prior same client+company+key is_current=0, insert new current. History retained.",
    "overwrite": "Never destructive overwrite of prior rows.",
    "scope": "Client/research scoped unless attribute is shared equipment_product.",
}

WORKFLOW_POLICY = {
    "ri3b_writes": 0,
    "production_switch": False,
    "fields": [
        "workflow_status",
        "workflow_assigned_rep",
        "workflow_hot",
        "workflow_follow_up",
        "workflow_next_action",
        "workflow_campaign",
    ],
}


def source_trust(source_type: str) -> str:
    return SOURCE_TRUST.get((source_type or "").strip(), TRUST_RESEARCH)


def source_is_authoritative(source_type: str) -> bool:
    return False


def master_actions_for(conflict_class: str) -> list[str]:
    if conflict_class == "SAME":
        return [MASTER_KEEP, MASTER_SKIP]
    if conflict_class == "FILL_BLANK":
        return [MASTER_FILL, MASTER_KEEP, MASTER_SKIP]
    if conflict_class == "PROPOSE_UPDATE":
        return [MASTER_KEEP, MASTER_ACCEPT_INCOMING, MASTER_SKIP]
    if conflict_class == "MANUAL_AUTHORITY_CONFLICT":
        return [MASTER_KEEP, MASTER_SKIP]
    if conflict_class == "POSSIBLE_NEW_LOCATION":
        return [MASTER_KEEP, MASTER_ADD_LOCATION, MASTER_SKIP]
    return [MASTER_KEEP, MASTER_SKIP]


def contact_actions_for(contact_class: str, *, cross_company: bool = False) -> list[str]:
    if contact_class in {CONTACT_INVALID, CONTACT_SKIPPED}:
        return [RES_SKIP_CONTACT]
    if contact_class == CONTACT_NEW:
        return [RES_CREATE_CONTACT, RES_SKIP_CONTACT]
    if contact_class in {CONTACT_EXACT, CONTACT_STRONG}:
        return [RES_USE_CONTACT, RES_SKIP_CONTACT]
    if cross_company:
        return [RES_CREATE_CONTACT, RES_SKIP_CONTACT]
    if contact_class in {CONTACT_POSSIBLE, CONTACT_AMBIGUOUS}:
        return [RES_USE_CONTACT, RES_CREATE_CONTACT, RES_SKIP_CONTACT]
    return [RES_SKIP_CONTACT]


def policy_matrix_rows() -> list[dict[str, str]]:
    return [
        {"field": key, **value}
        for key, value in FIELD_POLICY.items()
    ]


def public_policy() -> dict[str, Any]:
    return {
        "planner_contract": "ri4b.1",
        "production_confirm": False,
        "source_type_is_authority": False,
        "same_batch_research": "one_per_client_company_batch",
        "source_trust": SOURCE_TRUST,
        "fields": FIELD_POLICY,
        "ccr": CCR_POLICY,
        "notes": NOTES_POLICY,
        "custom_attributes": CUSTOM_ATTRIBUTE_POLICY,
        "workflow": WORKFLOW_POLICY,
        "master_actions_by_class": {
            "SAME": master_actions_for("SAME"),
            "FILL_BLANK": master_actions_for("FILL_BLANK"),
            "PROPOSE_UPDATE": master_actions_for("PROPOSE_UPDATE"),
            "MANUAL_AUTHORITY_CONFLICT": master_actions_for("MANUAL_AUTHORITY_CONFLICT"),
            "POSSIBLE_NEW_LOCATION": master_actions_for("POSSIBLE_NEW_LOCATION"),
        },
        "contact_classes": [
            CONTACT_EXACT,
            CONTACT_STRONG,
            CONTACT_NEW,
            CONTACT_POSSIBLE,
            CONTACT_AMBIGUOUS,
            CONTACT_INVALID,
        ],
        "contact_resolutions": [RES_USE_CONTACT, RES_CREATE_CONTACT, RES_SKIP_CONTACT],
    }
