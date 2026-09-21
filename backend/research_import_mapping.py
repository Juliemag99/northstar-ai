"""Research & Custom Prospect Import field mapping.

Supports the NorthStar template, Janco 18-column workbooks, and arbitrary CSV/XLSX
headers. Does not mutate source files. RI-2 destination->header mappings remain valid.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

CANONICAL_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("company_name", "Company Name", True),
    ("address", "Address 1", False),
    ("address2", "Address 2", False),
    ("city", "City", False),
    ("state", "State/Province", False),
    ("zip", "Postal Code", False),
    ("country", "Country", False),
    ("phone", "Phone", False),
    ("website", "Website", False),
    ("target_market", "Target Market / Industry", False),
    ("equipment_product", "Equipment / Products", False),
    ("why_client_fits", "Why Client Fits", False),
    ("potential_components", "Potential Components / Opportunities", False),
    ("research_priority", "Research Priority", False),
    ("target_department", "Target Department", False),
    ("qualification_notes", "Qualification Notes", False),
    ("product_source", "Product Source", False),
    ("address_source", "Address Source", False),
    ("phone_source", "Phone Source", False),
    ("website_source", "Website Source", False),
    ("general_source", "General Research Source", False),
    ("research_date", "Research Date", False),
    ("research_method", "Research Method", False),
    ("research_batch_name", "Research Batch", False),
    ("original_research_file", "Original Research File", False),
    ("prior_research_file", "Prior Research File", False),
    ("verification_status", "Verification Status", False),
    ("contact_first_name", "Contact First Name", False),
    ("contact_last_name", "Contact Last Name", False),
    ("contact_full_name", "Contact Full Name", False),
    ("contact_title", "Contact Title", False),
    ("contact_email", "Contact Email", False),
    ("contact_phone", "Contact Phone", False),
    ("contact_phone_extension", "Contact Phone Extension", False),
    ("imported_notes", "Notes (explicit)", False),
    ("workflow_status", "CRM Status (review)", False),
    ("workflow_assigned_rep", "Assigned Rep (review)", False),
    ("workflow_follow_up", "Next Follow-Up (review)", False),
    ("workflow_next_action", "Next Action (review)", False),
    ("workflow_hot", "Hot (review)", False),
    ("workflow_campaign", "Campaign (review)", False),
)

REQUIRED_FIELDS = frozenset(key for key, _label, required in CANONICAL_FIELDS if required)
FIELD_LABELS = {key: label for key, label, _req in CANONICAL_FIELDS}
FIELD_KEYS = frozenset(FIELD_LABELS)

COMPANY_NAME_REQUIRED = "Map a Company Name column before continuing."

SOURCE_TYPES: tuple[tuple[str, str], ...] = (
    ("CHATGPT_DEEP_RESEARCH", "ChatGPT Deep Research"),
    ("AI_RESEARCH_OTHER", "Other AI research"),
    ("CLIENT_PROVIDED", "Client-provided list"),
    ("INTERNAL_RESEARCH", "Internal NorthStar research"),
    ("TRADE_SHOW", "Trade show list"),
    ("ASSOCIATION_DIRECTORY", "Association / directory list"),
    ("PURCHASED_LIST", "Purchased prospect list"),
    ("SALESPERSON_PROVIDED", "Salesperson-created spreadsheet"),
    ("CUSTOM_REPORT", "Legacy / custom report"),
    ("OTHER", "Other"),
)
SOURCE_TYPE_LABELS = {code: label for code, label in SOURCE_TYPES}
SOURCE_TYPE_CODES = frozenset(SOURCE_TYPE_LABELS)

# source_type = who/what supplied the file. research_method = how intelligence was produced.
SOURCE_TYPE_DEFAULT_METHOD = {
    "CHATGPT_DEEP_RESEARCH": "CHATGPT_DEEP_RESEARCH",
    "AI_RESEARCH_OTHER": "AI_RESEARCH",
    "CLIENT_PROVIDED": "CLIENT_RESEARCH",
    "INTERNAL_RESEARCH": "INTERNAL_RESEARCH",
    "TRADE_SHOW": "LIST_IMPORT",
    "ASSOCIATION_DIRECTORY": "LIST_IMPORT",
    "PURCHASED_LIST": "LIST_IMPORT",
    "SALESPERSON_PROVIDED": "SALESPERSON_RESEARCH",
    "CUSTOM_REPORT": "CUSTOM_REPORT",
    "OTHER": "OTHER",
}

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("MASTER_COMPANY", "Master company"),
    ("LOCATION", "Location"),
    ("CONTACT", "Contact"),
    ("CLIENT_PROSPECT", "Client prospect"),
    ("RESEARCH_INTELLIGENCE", "Research intelligence"),
    ("TARGETING", "Targeting"),
    ("SOURCE_PROVENANCE", "Source / provenance"),
    ("BATCH_LINEAGE", "Batch / lineage"),
    ("CRM_WORKFLOW", "CRM workflow"),
    ("CUSTOM_ATTRIBUTE", "Custom attribute"),
    ("NOTES", "Notes"),
    ("IGNORE", "Ignore"),
)
CATEGORY_LABELS = {code: label for code, label in CATEGORIES}

FIELD_CATEGORY: dict[str, str] = {
    "company_name": "MASTER_COMPANY",
    "website": "MASTER_COMPANY",
    "phone": "MASTER_COMPANY",
    "address": "LOCATION",
    "address2": "LOCATION",
    "city": "LOCATION",
    "state": "LOCATION",
    "zip": "LOCATION",
    "country": "LOCATION",
    "contact_first_name": "CONTACT",
    "contact_last_name": "CONTACT",
    "contact_full_name": "CONTACT",
    "contact_title": "CONTACT",
    "contact_email": "CONTACT",
    "contact_phone": "CONTACT",
    "contact_phone_extension": "CONTACT",
    "research_priority": "CLIENT_PROSPECT",
    "why_client_fits": "CLIENT_PROSPECT",
    "target_market": "RESEARCH_INTELLIGENCE",
    "equipment_product": "RESEARCH_INTELLIGENCE",
    "potential_components": "RESEARCH_INTELLIGENCE",
    "qualification_notes": "RESEARCH_INTELLIGENCE",
    "target_department": "TARGETING",
    "product_source": "SOURCE_PROVENANCE",
    "address_source": "SOURCE_PROVENANCE",
    "phone_source": "SOURCE_PROVENANCE",
    "website_source": "SOURCE_PROVENANCE",
    "general_source": "SOURCE_PROVENANCE",
    "research_date": "BATCH_LINEAGE",
    "research_method": "BATCH_LINEAGE",
    "research_batch_name": "BATCH_LINEAGE",
    "original_research_file": "BATCH_LINEAGE",
    "prior_research_file": "BATCH_LINEAGE",
    "verification_status": "BATCH_LINEAGE",
    "workflow_status": "CRM_WORKFLOW",
    "workflow_assigned_rep": "CRM_WORKFLOW",
    "workflow_follow_up": "CRM_WORKFLOW",
    "workflow_next_action": "CRM_WORKFLOW",
    "workflow_hot": "CRM_WORKFLOW",
    "workflow_campaign": "CRM_WORKFLOW",
    "imported_notes": "NOTES",
}

WORKFLOW_FIELDS = frozenset(k for k, cat in FIELD_CATEGORY.items() if cat == "CRM_WORKFLOW")
NOTES_FIELDS = frozenset(k for k, cat in FIELD_CATEGORY.items() if cat == "NOTES")
CONTACT_FIELDS = frozenset(k for k, cat in FIELD_CATEGORY.items() if cat == "CONTACT")
VALUE_TYPES = frozenset({"TEXT", "NUMBER", "DATE", "BOOLEAN", "URL"})
DEPARTMENT_ONLY = frozenset(
    {
        "purchasing",
        "engineering",
        "operations",
        "supplychain",
        "manufacturing",
        "sales",
        "accounting",
        "quality",
    }
)
AMBIGUOUS_AUTOMAP = frozenset(
    {
        "status",
        "owner",
        "type",
        "rating",
        "score",
        "category",
        "comments",
        "comment",
        "rep",
        "assignedrep",
        "salesperson",
        "nextfollowup",
        "nextaction",
        "hot",
        "campaign",
        "notes",
        "note",
        "remarks",
        "remark",
    }
)

_HEADER_ALIASES: dict[str, str] = {
    "company": "company_name",
    "companyname": "company_name",
    "account": "company_name",
    "accountname": "company_name",
    "organization": "company_name",
    "org": "company_name",
    "orgname": "company_name",
    "firm": "company_name",
    "firmname": "company_name",
    "businessname": "company_name",
    "business": "company_name",
    "street": "address",
    "streetaddress": "address",
    "address1": "address",
    "addressline1": "address",
    "address2": "address2",
    "addressline2": "address2",
    "stateprovince": "state",
    "province": "state",
    "postalcode": "zip",
    "zip": "zip",
    "zipcode": "zip",
    "url": "website",
    "web": "website",
    "websiteurl": "website",
    "website": "website",
    "homepage": "website",
    "webpage": "website",
    "mainphone": "phone",
    "hqphone": "phone",
    "companyphone": "phone",
    "jancotargetmarket": "target_market",
    "targetmarket": "target_market",
    "targetmarketindustry": "target_market",
    "industry": "target_market",
    "equipmentproduct": "equipment_product",
    "equipmentproducts": "equipment_product",
    "equipmentproductsmanufactured": "equipment_product",
    "whyjancofits": "why_client_fits",
    "whyclientfits": "why_client_fits",
    "potentialcomponents": "potential_components",
    "potentialcomponentsopportunities": "potential_components",
    "priority": "research_priority",
    "researchpriority": "research_priority",
    "researchprioritylabel": "research_priority",
    "targetdepartment": "target_department",
    "targetdepartments": "target_department",
    "qualificationnotes": "qualification_notes",
    "productsource": "product_source",
    "addresssource": "address_source",
    "phonesource": "phone_source",
    "websitesource": "website_source",
    "generalsource": "general_source",
    "generalresearch": "general_source",
    "generalresearchsource": "general_source",
    "researchdate": "research_date",
    "researchmethod": "research_method",
    "researchbatch": "research_batch_name",
    "researchbatchname": "research_batch_name",
    "originalresearchfile": "original_research_file",
    "priorresearchfile": "prior_research_file",
    "verificationstatus": "verification_status",
    "firstname": "contact_first_name",
    "contactfirstname": "contact_first_name",
    "lastname": "contact_last_name",
    "contactlastname": "contact_last_name",
    "fullname": "contact_full_name",
    "contactname": "contact_full_name",
    "contactfullname": "contact_full_name",
    "jobtitle": "contact_title",
    "contacttitle": "contact_title",
    "email": "contact_email",
    "emailaddress": "contact_email",
    "contactemail": "contact_email",
    "contactphone": "contact_phone",
    "mobile": "contact_phone",
    "phoneextension": "contact_phone_extension",
    "extension": "contact_phone_extension",
    "ext": "contact_phone_extension",
    "client": "client_ignored",
}

WORKFLOW_HEADER_HINTS: dict[str, str] = {
    "status": "workflow_status",
    "crmstatus": "workflow_status",
    "owner": "workflow_assigned_rep",
    "assignedrep": "workflow_assigned_rep",
    "rep": "workflow_assigned_rep",
    "salesperson": "workflow_assigned_rep",
    "nextfollowup": "workflow_follow_up",
    "followup": "workflow_follow_up",
    "nextaction": "workflow_next_action",
    "hot": "workflow_hot",
    "campaign": "workflow_campaign",
    "comments": "imported_notes",
    "comment": "imported_notes",
    "notes": "imported_notes",
}


def blank(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def normalize_header_key(header: object | None) -> str:
    text = blank(header).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def normalize_attribute_key(label: object | None) -> str:
    text = blank(label).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


def default_research_method(source_type: str) -> str:
    return SOURCE_TYPE_DEFAULT_METHOD.get(blank(source_type), "OTHER")


def source_type_record(code: str, extra_label: str = "") -> dict[str, str]:
    key = blank(code) or "OTHER"
    if key not in SOURCE_TYPE_CODES:
        key = "OTHER"
    label = SOURCE_TYPE_LABELS[key]
    extra = blank(extra_label)
    if key == "OTHER" and extra:
        label = extra
    return {"code": key, "label": label, "source_label": extra}


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for header in headers:
        norm = normalize_header_key(header)
        if norm in AMBIGUOUS_AUTOMAP:
            continue
        key = _HEADER_ALIASES.get(norm)
        if not key and norm in FIELD_KEYS:
            key = norm
        if not key or key == "client_ignored" or key in used or key not in FIELD_KEYS:
            continue
        if key in WORKFLOW_FIELDS:
            continue
        mapping[key] = header
        used.add(key)
    if "website_source" not in mapping and "website" in mapping:
        mapping["website_source"] = mapping["website"]
    return mapping


def header_signature(headers: list[str]) -> str:
    norms = sorted(normalize_header_key(h) for h in headers if blank(h))
    raw = json.dumps(norms, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mapping_safety_class(fields: dict[str, str]) -> str:
    if any(key in WORKFLOW_FIELDS for key in fields):
        return "workflow_sensitive"
    if any(key in NOTES_FIELDS for key in fields):
        return "notes_sensitive"
    return "safe_reusable"


def _is_flat_mapping(mapping: dict[str, Any] | None) -> bool:
    if not mapping:
        return True
    if "columns" in mapping or "fields" in mapping:
        return False
    return True


def fields_from_mapping(mapping: dict[str, Any] | None) -> dict[str, str]:
    if not mapping:
        return {}
    if _is_flat_mapping(mapping):
        return normalize_mapping(mapping)
    fields = normalize_mapping(mapping.get("fields") or {})
    for column in mapping.get("columns") or []:
        if not isinstance(column, dict):
            continue
        if blank(column.get("category")) == "IGNORE" or column.get("ignored"):
            continue
        dest = blank(column.get("field"))
        header = blank(column.get("header"))
        if dest in FIELD_KEYS and header:
            fields[dest] = header
    return fields


def ignored_headers(mapping: dict[str, Any] | None, headers: list[str] | None = None) -> list[str]:
    if mapping and not _is_flat_mapping(mapping):
        out = []
        for column in mapping.get("columns") or []:
            if not isinstance(column, dict):
                continue
            if blank(column.get("category")) == "IGNORE" or column.get("ignored"):
                header = blank(column.get("header"))
                if header:
                    out.append(header)
        return out
    mapped = set(fields_from_mapping(mapping).values())
    return [h for h in (headers or []) if h not in mapped]


def build_v3_mapping(
    headers: list[str],
    *,
    fields: dict[str, str] | None = None,
    ignored: list[str] | None = None,
    custom: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    mapped = normalize_mapping(fields)
    ignored_set = {blank(h) for h in (ignored or [])}
    custom_by_header = {blank(item.get("header")): item for item in (custom or [])}
    columns: list[dict[str, Any]] = []
    for header in headers:
        name = blank(header)
        if not name:
            continue
        if name in ignored_set:
            columns.append(
                {
                    "header": name,
                    "category": "IGNORE",
                    "field": "",
                    "ignored": True,
                    "custom_key": "",
                    "custom_label": "",
                    "value_type": "TEXT",
                }
            )
            continue
        custom_item = custom_by_header.get(name)
        if custom_item:
            columns.append(
                {
                    "header": name,
                    "category": "CUSTOM_ATTRIBUTE",
                    "field": "",
                    "ignored": False,
                    "custom_key": custom_item.get("key") or normalize_attribute_key(custom_item.get("label") or name),
                    "custom_label": custom_item.get("label") or name,
                    "value_type": (custom_item.get("value_type") or "TEXT").upper(),
                    "scope": custom_item.get("scope") or "client",
                }
            )
            continue
        dest = next((key for key, src in mapped.items() if src == name), "")
        columns.append(
            {
                "header": name,
                "category": FIELD_CATEGORY.get(dest, ""),
                "field": dest,
                "ignored": False,
                "custom_key": "",
                "custom_label": "",
                "value_type": "TEXT",
            }
        )
    return {"fields": mapped, "columns": columns}


def custom_attribute_maps(mapping: dict[str, Any] | None) -> list[dict[str, str]]:
    if not mapping or _is_flat_mapping(mapping):
        return []
    out: list[dict[str, str]] = []
    for column in mapping.get("columns") or []:
        if not isinstance(column, dict):
            continue
        if blank(column.get("category")) != "CUSTOM_ATTRIBUTE":
            continue
        header = blank(column.get("header"))
        label = blank(column.get("custom_label")) or header
        key = normalize_attribute_key(column.get("custom_key") or label)
        if not header or not key:
            continue
        value_type = blank(column.get("value_type")).upper() or "TEXT"
        if value_type not in VALUE_TYPES:
            value_type = "TEXT"
        out.append(
            {
                "header": header,
                "key": key,
                "label": label,
                "value_type": value_type,
                "scope": blank(column.get("scope")) or "client",
            }
        )
    return out


def normalize_mapping(mapping: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    raw = mapping or {}
    if not _is_flat_mapping(raw) and isinstance(raw.get("fields"), dict):
        raw = raw.get("fields") or {}
    for key, header in raw.items():
        dest = blank(key)
        src = blank(header)
        if dest not in FIELD_KEYS or not src:
            continue
        out[dest] = src
    return out


def validate_mapping(mapping: dict[str, Any], headers: list[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    header_set = {blank(h) for h in headers}
    mapped = fields_from_mapping(mapping)
    company_header = mapped.get("company_name", "")
    if not company_header:
        errors.append(COMPANY_NAME_REQUIRED)
    elif company_header not in header_set:
        errors.append(COMPANY_NAME_REQUIRED)
    for dest, header in mapped.items():
        if header not in header_set:
            errors.append(f"Mapped column {header!r} is not in the uploaded file.")
    return (not errors), errors


def apply_mapping(values: dict[str, str], mapping: dict[str, Any]) -> dict[str, str]:
    mapped_fields = fields_from_mapping(mapping)
    out: dict[str, str] = {key: "" for key in FIELD_KEYS}
    for dest, header in mapped_fields.items():
        out[dest] = blank(values.get(header))
    if not out.get("website_source") and out.get("website"):
        out["website_source"] = out["website"]
    return out


def parse_typed_value(raw: object | None, value_type: str) -> dict[str, Any]:
    original = blank(raw)
    kind = (blank(value_type) or "TEXT").upper()
    if kind not in VALUE_TYPES:
        kind = "TEXT"
    rec: dict[str, Any] = {
        "original_value": original,
        "value_type": kind,
        "text_value": original,
        "numeric_value": None,
        "date_value": "",
        "boolean_value": None,
    }
    if not original:
        return rec
    if kind == "NUMBER":
        compact = original.replace(",", "").replace("$", "").strip()
        try:
            rec["numeric_value"] = float(compact)
        except ValueError:
            digits = re.sub(r"[^0-9.\-]", "", compact)
            try:
                rec["numeric_value"] = float(digits) if digits not in {"", "-", "."} else None
            except ValueError:
                rec["numeric_value"] = None
    elif kind == "BOOLEAN":
        rec["boolean_value"] = original.casefold() in {"1", "true", "yes", "y", "hot"}
    elif kind == "DATE":
        rec["date_value"] = original
    elif kind == "URL":
        rec["text_value"] = original
    return rec


def is_department_only_name(value: object | None) -> bool:
    text = normalize_header_key(value)
    if not text:
        return False
    parts = re.split(r"[;/,&]+", blank(value).lower())
    tokens = [normalize_header_key(p) for p in parts if blank(p)]
    if not tokens:
        return False
    return all(tok in DEPARTMENT_ONLY or tok.replace("and", "") in DEPARTMENT_ONLY for tok in tokens)


def contact_from_mapped(mapped: dict[str, str]) -> dict[str, str] | None:
    first = blank(mapped.get("contact_first_name"))
    last = blank(mapped.get("contact_last_name"))
    full = blank(mapped.get("contact_full_name"))
    if not full:
        full = f"{first} {last}".strip()
    if not full:
        return None
    if is_department_only_name(full) or is_department_only_name(first) or is_department_only_name(last):
        return None
    return {
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "title": blank(mapped.get("contact_title")),
        "email": blank(mapped.get("contact_email")),
        "phone": blank(mapped.get("contact_phone")),
        "phone_extension": blank(mapped.get("contact_phone_extension")),
    }


def describe_source_columns(
    headers: list[str],
    sample_rows: list[dict[str, str]] | None = None,
    mapping: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    suggested = suggest_mapping(headers)
    suggested_by_header = {header: key for key, header in suggested.items()}
    selected = fields_from_mapping(mapping)
    selected_by_header = {header: key for key, header in selected.items()}
    ignored = set(ignored_headers(mapping, headers))
    custom_by_header = {item["header"]: item for item in custom_attribute_maps(mapping)}
    rows: list[dict[str, Any]] = []
    for header in headers:
        norm = normalize_header_key(header)
        samples = []
        for row in sample_rows or []:
            value = blank(row.get(header))
            if value and value not in samples:
                samples.append(value)
            if len(samples) >= 3:
                break
        suggested_field = suggested_by_header.get(header, "")
        selected_field = selected_by_header.get(header, "")
        custom = custom_by_header.get(header)
        ignored_here = header in ignored
        workflow_sensitive = norm in AMBIGUOUS_AUTOMAP or (selected_field in WORKFLOW_FIELDS)
        warning = ""
        if workflow_sensitive:
            warning = "Workflow-sensitive. Will not change CRM status, assignment, or follow-up unless a later confirm phase explicitly allows it."
        category = "IGNORE" if ignored_here else (
            "CUSTOM_ATTRIBUTE" if custom else FIELD_CATEGORY.get(selected_field or suggested_field, "")
        )
        if not category and norm in WORKFLOW_HEADER_HINTS:
            category = "CRM_WORKFLOW" if WORKFLOW_HEADER_HINTS[norm] in WORKFLOW_FIELDS else "NOTES"
        rows.append(
            {
                "header": header,
                "samples": samples,
                "suggested_field": suggested_field,
                "suggested_category": FIELD_CATEGORY.get(suggested_field, category or ""),
                "selected_field": selected_field,
                "selected_category": category or FIELD_CATEGORY.get(selected_field, ""),
                "custom_key": (custom or {}).get("key", ""),
                "custom_label": (custom or {}).get("label", ""),
                "value_type": (custom or {}).get("value_type", "TEXT"),
                "ignored": ignored_here,
                "workflow_sensitive": workflow_sensitive,
                "warning": warning,
                "review_hint_field": WORKFLOW_HEADER_HINTS.get(norm, ""),
            }
        )
    return rows


def compare_header_sets(saved_headers: list[str], incoming_headers: list[str]) -> dict[str, Any]:
    saved = [blank(h) for h in saved_headers]
    incoming = [blank(h) for h in incoming_headers]
    saved_n = {normalize_header_key(h) for h in saved}
    incoming_n = {normalize_header_key(h) for h in incoming}
    missing = [h for h in saved if normalize_header_key(h) not in incoming_n]
    added = [h for h in incoming if normalize_header_key(h) not in saved_n]
    compatible = header_signature(saved) == header_signature(incoming)
    return {
        "compatible": compatible,
        "missing_headers": missing,
        "new_headers": added,
        "review_required": (not compatible) or bool(missing) or bool(added),
        "confidence": "exact" if compatible else ("partial" if not missing else "low"),
    }


def split_multi(value: object | None) -> list[str]:
    text = blank(value)
    if not text:
        return []
    parts = re.split(r"[;|]", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = part.strip()
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    if len(out) == 1 and "," in out[0] and " / " not in out[0]:
        comma_parts = [p.strip() for p in out[0].split(",") if p.strip()]
        if len(comma_parts) > 1:
            return comma_parts
    return out


def parse_priority(value: object | None) -> tuple[str, str]:
    label = blank(value)
    if not label:
        return "", ""
    match = re.match(r"^([A-Za-z0-9]+)\b", label)
    code = match.group(1).upper() if match else ""
    return code, label


ATTRIBUTE_TYPES = frozenset(
    {
        "target_market",
        "equipment_product",
        "potential_component",
        "target_department",
    }
)

SOURCE_ROLES = frozenset(
    {
        "PRODUCT",
        "ADDRESS",
        "PHONE",
        "WEBSITE",
        "GENERAL_RESEARCH",
    }
)

RESEARCH_METHODS = frozenset(
    {
        "CHATGPT_DEEP_RESEARCH",
        "AI_RESEARCH",
        "CLIENT_RESEARCH",
        "INTERNAL_RESEARCH",
        "LIST_IMPORT",
        "SALESPERSON_RESEARCH",
        "CUSTOM_REPORT",
        "MANUAL_RESEARCH",
        "OTHER",
    }
)


@dataclass(frozen=True, slots=True)
class MappingField:
    key: str
    label: str
    required: bool


def mapping_fields() -> list[MappingField]:
    return [MappingField(key, label, required) for key, label, required in CANONICAL_FIELDS]
