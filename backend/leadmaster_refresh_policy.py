"""Explicit LeadMaster refresh policy profile.

Client defaults may exist later. The policy used for a refresh is always frozen
into the batch so historical refreshes remain explainable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SOURCE_LEADMASTER = "LEADMASTER"
SOURCE_SYSTEM_DB = "leadmaster"

MODE_INITIAL_IMPORT = "INITIAL_IMPORT"
MODE_LEADMASTER_REFRESH = "LEADMASTER_REFRESH"

STATUS_PRESERVE_EXISTING = "PRESERVE_EXISTING"
STATUS_PROPOSE_CHANGES = "PROPOSE_CHANGES"
STATUS_AUTHORITATIVE_NONBLANK = "AUTHORITATIVE_NONBLANK"

COMPANY_PRESERVE_POPULATED = "PRESERVE_POPULATED_MASTER"
COMPANY_FILL_BLANK = "FILL_BLANK_SAFE_FIELDS"
COMPANY_REVIEW_CONFLICTS = "REVIEW_CONFLICTS"

CONTACT_FILL_BLANK = "FILL_BLANK_SAFE_FIELDS"
CONTACT_REVIEW_CONFLICTS = "REVIEW_POPULATED_CONFLICTS"

NOTES_APPEND_DISTINCT = "APPEND_DISTINCT"
HISTORY_APPEND_DISTINCT = "APPEND_DISTINCT_BY_SOURCE_ID_OR_FINGERPRINT"
ASSIGNMENT_EXPLICIT_MAP = "EXPLICIT_MAP_ONLY"
CAMPAIGN_EXPLICIT_CLASS = "EXPLICIT_CLASS_MAP_ONLY"

ASSIGN_MAPPED = "MAPPED"
ASSIGN_UNMAPPED = "UNMAPPED"
ASSIGN_INACTIVE = "INACTIVE_TARGET"
ASSIGN_AMBIGUOUS = "AMBIGUOUS"
ASSIGN_IGNORED = "IGNORED"

CAMPAIGN_ACTIVE = "ACTIVE_OPERATIONAL"
CAMPAIGN_HISTORICAL = "HISTORICAL_PROVENANCE"
CAMPAIGN_SYSTEM = "SYSTEM_TEST_ADMIN"
CAMPAIGN_UNKNOWN = "UNKNOWN_REVIEW"
CAMPAIGN_IGNORED = "IGNORED"

_STATUS_TO_PLANNER = {
    STATUS_PRESERVE_EXISTING: "preserve",
    "preserve": "preserve",
    STATUS_PROPOSE_CHANGES: "propose_change",
    "propose_change": "propose_change",
    STATUS_AUTHORITATIVE_NONBLANK: "authoritative",
    "authoritative": "authoritative",
}


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_status_mode(value: object | None) -> str:
    text = _blank(value) or STATUS_PROPOSE_CHANGES
    key = text.upper().replace(" ", "_")
    aliases = {
        "PRESERVE": STATUS_PRESERVE_EXISTING,
        "PRESERVE_EXISTING": STATUS_PRESERVE_EXISTING,
        "PROPOSE": STATUS_PROPOSE_CHANGES,
        "PROPOSE_CHANGE": STATUS_PROPOSE_CHANGES,
        "PROPOSE_CHANGES": STATUS_PROPOSE_CHANGES,
        "AUTHORITATIVE": STATUS_AUTHORITATIVE_NONBLANK,
        "AUTHORITATIVE_NONBLANK": STATUS_AUTHORITATIVE_NONBLANK,
    }
    return aliases.get(key, STATUS_PROPOSE_CHANGES)


def planner_status_mode(value: object | None) -> str:
    mode = normalize_status_mode(value)
    return _STATUS_TO_PLANNER[mode]


@dataclass
class AssignmentMapEntry:
    source_rep: str
    state: str = ASSIGN_UNMAPPED
    user_id: int | None = None
    note: str = ""

    def fingerprint_slice(self) -> dict[str, Any]:
        return {
            "source_rep": self.source_rep,
            "state": self.state,
            "user_id": self.user_id,
            "note": self.note,
        }


@dataclass
class CampaignMapEntry:
    source_campaign: str
    classification: str = CAMPAIGN_UNKNOWN
    northstar_campaign_id: int | None = None
    note: str = ""

    def fingerprint_slice(self) -> dict[str, Any]:
        return {
            "source_campaign": self.source_campaign,
            "classification": self.classification,
            "northstar_campaign_id": self.northstar_campaign_id,
            "note": self.note,
        }


@dataclass
class RefreshPolicyProfile:
    """Frozen policy for one refresh batch."""

    client_id: int
    source_system: str = SOURCE_LEADMASTER
    status_mode: str = STATUS_PROPOSE_CHANGES
    company_field_mode: str = COMPANY_FILL_BLANK
    contact_field_mode: str = CONTACT_FILL_BLANK
    notes_mode: str = NOTES_APPEND_DISTINCT
    history_mode: str = HISTORY_APPEND_DISTINCT
    assignment_mode: str = ASSIGNMENT_EXPLICIT_MAP
    campaign_mode: str = CAMPAIGN_EXPLICIT_CLASS
    assignment_maps: list[AssignmentMapEntry] = field(default_factory=list)
    campaign_maps: list[CampaignMapEntry] = field(default_factory=list)
    allow_additive_contact_fields: bool = True
    allow_safe_canonical_company_fill: bool = True

    def normalized(self) -> "RefreshPolicyProfile":
        self.status_mode = normalize_status_mode(self.status_mode)
        self.source_system = SOURCE_LEADMASTER
        if self.company_field_mode not in {
            COMPANY_PRESERVE_POPULATED,
            COMPANY_FILL_BLANK,
            COMPANY_REVIEW_CONFLICTS,
        }:
            self.company_field_mode = COMPANY_FILL_BLANK
        if self.contact_field_mode not in {
            CONTACT_FILL_BLANK,
            CONTACT_REVIEW_CONFLICTS,
        }:
            self.contact_field_mode = CONTACT_FILL_BLANK
        self.notes_mode = NOTES_APPEND_DISTINCT
        self.history_mode = HISTORY_APPEND_DISTINCT
        self.assignment_mode = ASSIGNMENT_EXPLICIT_MAP
        self.campaign_mode = CAMPAIGN_EXPLICIT_CLASS
        fill_company = self.company_field_mode == COMPANY_FILL_BLANK
        fill_contact = self.contact_field_mode == CONTACT_FILL_BLANK
        self.allow_safe_canonical_company_fill = fill_company
        self.allow_additive_contact_fields = fill_contact
        return self

    def assignment_user_map(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self.assignment_maps:
            if entry.state == ASSIGN_MAPPED and entry.user_id:
                out[entry.source_rep] = int(entry.user_id)
        return out

    def campaign_class_map(self) -> dict[str, str]:
        return {
            entry.source_campaign: entry.classification
            for entry in self.campaign_maps
            if _blank(entry.source_campaign)
        }

    def campaign_target_map(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self.campaign_maps:
            if (
                entry.classification == CAMPAIGN_ACTIVE
                and entry.northstar_campaign_id
            ):
                out[entry.source_campaign] = int(entry.northstar_campaign_id)
        return out

    def fingerprint_slice(self) -> dict[str, Any]:
        self.normalized()
        return {
            "client_id": int(self.client_id),
            "source_system": self.source_system,
            "status_mode": self.status_mode,
            "company_field_mode": self.company_field_mode,
            "contact_field_mode": self.contact_field_mode,
            "notes_mode": self.notes_mode,
            "history_mode": self.history_mode,
            "assignment_mode": self.assignment_mode,
            "campaign_mode": self.campaign_mode,
            "assignment_maps": [
                e.fingerprint_slice()
                for e in sorted(self.assignment_maps, key=lambda x: x.source_rep.casefold())
            ],
            "campaign_maps": [
                e.fingerprint_slice()
                for e in sorted(self.campaign_maps, key=lambda x: x.source_campaign.casefold())
            ],
            "allow_additive_contact_fields": bool(self.allow_additive_contact_fields),
            "allow_safe_canonical_company_fill": bool(
                self.allow_safe_canonical_company_fill
            ),
        }

    def to_json(self) -> dict[str, Any]:
        self.normalized()
        payload = asdict(self)
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None, *, client_id: int) -> "RefreshPolicyProfile":
        data = dict(payload or {})
        assigns = []
        for raw in data.get("assignment_maps") or []:
            if not isinstance(raw, dict):
                continue
            assigns.append(
                AssignmentMapEntry(
                    source_rep=_blank(raw.get("source_rep")),
                    state=_blank(raw.get("state")) or ASSIGN_UNMAPPED,
                    user_id=int(raw["user_id"]) if raw.get("user_id") not in (None, "") else None,
                    note=_blank(raw.get("note")),
                )
            )
        camps = []
        for raw in data.get("campaign_maps") or []:
            if not isinstance(raw, dict):
                continue
            camps.append(
                CampaignMapEntry(
                    source_campaign=_blank(raw.get("source_campaign")),
                    classification=_blank(raw.get("classification")) or CAMPAIGN_UNKNOWN,
                    northstar_campaign_id=(
                        int(raw["northstar_campaign_id"])
                        if raw.get("northstar_campaign_id") not in (None, "")
                        else None
                    ),
                    note=_blank(raw.get("note")),
                )
            )
        profile = cls(
            client_id=int(data.get("client_id") or client_id),
            source_system=SOURCE_LEADMASTER,
            status_mode=normalize_status_mode(data.get("status_mode")),
            company_field_mode=_blank(data.get("company_field_mode")) or COMPANY_FILL_BLANK,
            contact_field_mode=_blank(data.get("contact_field_mode")) or CONTACT_FILL_BLANK,
            assignment_maps=assigns,
            campaign_maps=camps,
        )
        return profile.normalized()


def default_policy(client_id: int) -> RefreshPolicyProfile:
    return RefreshPolicyProfile(client_id=int(client_id)).normalized()


def policy_catalog() -> dict[str, Any]:
    return {
        "source_system": SOURCE_LEADMASTER,
        "status_modes": [
            STATUS_PRESERVE_EXISTING,
            STATUS_PROPOSE_CHANGES,
            STATUS_AUTHORITATIVE_NONBLANK,
        ],
        "company_field_modes": [
            COMPANY_PRESERVE_POPULATED,
            COMPANY_FILL_BLANK,
            COMPANY_REVIEW_CONFLICTS,
        ],
        "contact_field_modes": [CONTACT_FILL_BLANK, CONTACT_REVIEW_CONFLICTS],
        "notes_mode": NOTES_APPEND_DISTINCT,
        "history_mode": HISTORY_APPEND_DISTINCT,
        "assignment_states": [
            ASSIGN_MAPPED,
            ASSIGN_UNMAPPED,
            ASSIGN_INACTIVE,
            ASSIGN_AMBIGUOUS,
            ASSIGN_IGNORED,
        ],
        "campaign_classes": [
            CAMPAIGN_ACTIVE,
            CAMPAIGN_HISTORICAL,
            CAMPAIGN_SYSTEM,
            CAMPAIGN_UNKNOWN,
            CAMPAIGN_IGNORED,
        ],
        "blank_status": "always preserve existing",
        "invalid_status": "never silently overwrite",
        "replace_notes": False,
        "auto_create_campaign": False,
        "fuzzy_rep_map": False,
    }
