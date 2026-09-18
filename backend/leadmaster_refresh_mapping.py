"""LeadMaster refresh column mapping.

Separate from initial-import CANONICAL_MAPPING_FIELDS so optional refresh
fields can be NOT MAPPED without breaking CRM import.
"""

from __future__ import annotations

from typing import Any

MAPPING_UNKNOWN_FIELD = "Unknown refresh mapping field."
MAPPING_COMPANY_REQUIRED = "Map a Company name column before continuing."
MAPPING_HEADER_MISSING = "A mapped source column is not in this spreadsheet."
MAPPING_DUPLICATE_HEADER = "Each source column can map to only one field."
MAPPING_NAME_CONFLICT = (
    "Use either a full name column or first and last name columns, not both."
)
NOT_MAPPED = "NOT MAPPED"

REFRESH_MAPPING_FIELDS: tuple[dict[str, str], ...] = (
    {"key": "external_record_no", "label": "LeadMaster company RN", "group": "company"},
    {"key": "company_name", "label": "Company name", "group": "company", "required": "1"},
    {"key": "address", "label": "Address", "group": "company"},
    {"key": "address2", "label": "Address 2 / unit", "group": "company"},
    {"key": "city", "label": "City", "group": "company"},
    {"key": "state", "label": "State / province", "group": "company"},
    {"key": "zip", "label": "Postal code", "group": "company"},
    {"key": "country", "label": "Country", "group": "company"},
    {"key": "phone", "label": "Company phone", "group": "company"},
    {"key": "phone_extension", "label": "Company phone extension", "group": "company"},
    {"key": "website", "label": "Website / domain", "group": "company"},
    {"key": "contact_first_name", "label": "First name", "group": "contact"},
    {"key": "contact_last_name", "label": "Last name", "group": "contact"},
    {"key": "contact_full_name", "label": "Contact full name", "group": "contact"},
    {"key": "contact_title", "label": "Title", "group": "contact"},
    {"key": "contact_email", "label": "Email", "group": "contact"},
    {"key": "contact_phone", "label": "Phone", "group": "contact"},
    {"key": "contact_phone_extension", "label": "Phone extension", "group": "contact"},
    {"key": "contact_alt_phone", "label": "Alternate phone", "group": "contact"},
    {"key": "contact_alt_extension", "label": "Alternate extension", "group": "contact"},
    {"key": "relationship_status", "label": "Status", "group": "relationship"},
    {"key": "relationship_notes", "label": "Notes", "group": "relationship"},
    {"key": "assigned_rep", "label": "Assigned / source rep", "group": "relationship"},
    {"key": "campaign", "label": "Campaign / list", "group": "relationship"},
    {"key": "history_source_id", "label": "History source event/note ID", "group": "history"},
    {"key": "history_event_at", "label": "History event date", "group": "history"},
    {"key": "history_event_type", "label": "History event type", "group": "history"},
    {"key": "history_note_text", "label": "History event text", "group": "history"},
    {"key": "history_author", "label": "History source user/rep", "group": "history"},
)

FIELD_KEYS = frozenset(f["key"] for f in REFRESH_MAPPING_FIELDS)
REQUIRED_KEYS = frozenset(f["key"] for f in REFRESH_MAPPING_FIELDS if f.get("required"))

HEADER_ALIASES: dict[str, str] = {
    "company": "company_name",
    "companyname": "company_name",
    "recordno": "external_record_no",
    "recordnumber": "external_record_no",
    "leadmasterrecordno": "external_record_no",
    "address1": "address",
    "address2": "address2",
    "unit": "address2",
    "city": "city",
    "state": "state",
    "zip": "zip",
    "postal": "zip",
    "postalcode": "zip",
    "country": "country",
    "website": "website",
    "webaddress": "website",
    "phone": "phone",
    "companyphone": "phone",
    "extension": "phone_extension",
    "phoneextension": "phone_extension",
    "firstname": "contact_first_name",
    "lastname": "contact_last_name",
    "title": "contact_title",
    "email": "contact_email",
    "contactphone": "contact_phone",
    "mobile": "contact_phone",
    "altphone": "contact_alt_phone",
    "status": "relationship_status",
    "notes": "relationship_notes",
    "comments": "relationship_notes",
    "salesrepcommentsnotes": "relationship_notes",
    "rep": "assigned_rep",
    "assignedrep": "assigned_rep",
    "salesrep": "assigned_rep",
    "campaign": "campaign",
    "customercampaign": "campaign",
    "eventhash": "history_source_id",
    "sourcenoteid": "history_source_id",
    "eventtimestamp": "history_event_at",
    "eventtype": "history_event_type",
    "notetext": "history_note_text",
    "author": "history_author",
}


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_header_key(raw: str) -> str:
    return "".join(ch for ch in _blank(raw).lower() if ch.isalnum())


def normalize_mapping(raw: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for dest, header in (raw or {}).items():
        key = _blank(dest)
        value = _blank(header)
        if not key or key not in FIELD_KEYS:
            continue
        if not value or value.upper() == NOT_MAPPED:
            continue
        out[key] = value
    return out


def validate_refresh_mapping(
    draft: dict[str, str], headers: list[str]
) -> tuple[bool, list[str], dict[str, str]]:
    mapping = normalize_mapping(draft)
    errors: list[str] = []
    header_set = set(headers)
    used: dict[str, str] = {}
    if "company_name" not in mapping:
        errors.append(MAPPING_COMPANY_REQUIRED)
    for dest, header in mapping.items():
        if header not in header_set:
            errors.append(MAPPING_HEADER_MISSING)
            continue
        prior = used.get(header)
        if prior and prior != dest:
            errors.append(MAPPING_DUPLICATE_HEADER)
        else:
            used[header] = dest
    if mapping.get("contact_full_name") and (
        mapping.get("contact_first_name") or mapping.get("contact_last_name")
    ):
        errors.append(MAPPING_NAME_CONFLICT)
    return (len(errors) == 0, sorted(set(errors)), mapping)


def suggest_refresh_mapping(headers: list[str]) -> dict[str, str]:
    suggested: dict[str, str] = {}
    used: set[str] = set()
    ranked: list[tuple[int, str, str]] = []
    for header in headers:
        field = HEADER_ALIASES.get(normalize_header_key(header))
        if not field or field not in FIELD_KEYS:
            continue
        priority = 10
        if field == "company_name":
            priority = 0
        if field in {"contact_first_name", "contact_last_name"}:
            priority = 1
        if field == "external_record_no":
            priority = 2
        ranked.append((priority, header, field))
    ranked.sort()
    has_first_or_last = any(f in {"contact_first_name", "contact_last_name"} for _, _, f in ranked)
    for _p, header, field in ranked:
        if field in suggested or header in used:
            continue
        if field == "contact_full_name" and has_first_or_last:
            continue
        suggested[field] = header
        used.add(header)
    return suggested


def mapped_value(raw_row: dict[str, Any], mapping: dict[str, str], key: str) -> str:
    header = mapping.get(key) or ""
    if not header:
        return ""
    return _blank(raw_row.get(header))
