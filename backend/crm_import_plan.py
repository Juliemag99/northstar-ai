"""Checkpoint C2 — read-only CRM import dry-run planner.

Plans what a previewed, mapped batch would do without writing staging or CRM.
A future confirm must call plan_crm_import_batch inside its write transaction and
compare plan_fingerprint before applying anything.

Does not call ensure_crm_import_schema, purge_expired_staging_rows,
create_or_link_*, ensure_client_relationship, upsert_contact_phone_keys,
confirm_company_contact_add, get_default_user, company_match, or AI.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from contact_phone import canonical_contact_phone
from crm_import_staging import (
    BATCH_NOT_REUSABLE,
    BatchNotReusable,
    CELL_PREVIEW_CHARS,
    MAPPING_COMPANY_REQUIRED,
    MAPPING_NO_ROWS,
    STATUS_PREVIEWED,
    _normalize_mapping,
)
from crm_import_datetime import INVALID_DATE, normalize_import_datetime
from crm_import_state import INVALID_STATE, normalize_us_state, state_for_match
from crm_import_status_notes import (
    NOTES_ALREADY_PRESENT,
    NOTES_APPEND,
    NOTES_NO_CHANGE,
    NOTES_SET,
    STATUS_CONFLICT,
    STATUS_INVALID,
    STATUS_PRESERVE,
    STATUS_USE_DEFAULT,
    STATUS_USE_IMPORTED,
    BATCH_RESOLUTION_USE_IMPORTED,
    load_client_status_catalog,
    normalize_status_key,
    plan_status_and_notes,
)
from crm_import_status_resolution import (
    StatusResolution,
    apply_status_resolution,
    catalog_labels,
    load_batch_status_resolutions,
)
from crm_import_match_resolution import (
    MatchResolution,
    RESOLUTION_CREATE_COMPANY,
    RESOLUTION_CREATE_CONTACT,
    RESOLUTION_IMPORT_COMPANY_ONLY,
    RESOLUTION_SKIP_ROW,
    RESOLUTION_USE_EXISTING_COMPANY,
    RESOLUTION_USE_EXISTING_CONTACT,
    RESOLUTION_USE_PROPOSED_COMPANY,
    load_batch_match_resolutions,
)
from crm_import_source import (
    DEFAULT_SOURCE_TYPE,
    IDENTITY_BOUND_DETAIL,
    is_leadmaster_source,
    normalize_source_type,
)
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from shared_note_history_import import is_closed_status, normalize_record_no
from models import (
    CrmImportDryRunCompanyPlan,
    CrmImportDryRunCompanyPossible,
    CrmImportDryRunContactPlan,
    CrmImportDryRunContactPossible,
    CrmImportDryRunCounts,
    CrmImportDryRunRelationshipPlan,
    CrmImportDryRunResponse,
    CrmImportDryRunRow,
    CrmImportValidationSummary,
)

PLANNER_VERSION = "crm-import-plan-v11"
IDENTITY_CRM = "crm"
IDENTITY_CLIENT_DATA = "client_data"
EXCLUDED_CLOSED = "excluded_closed"
EXCLUDED_SKIP = "excluded_skip"
EXCLUDED_ACTIONS = frozenset({EXCLUDED_CLOSED, EXCLUDED_SKIP})
MAX_DRY_RUN_PAGE = 100
IN_CHUNK = 400
INVALID_PAGING = "Invalid paging."
INVALID_SAVED_MAPPING = "The saved column mapping is invalid."
CONTACT_NEEDS_NAME_OR_EMAIL = "contact_requires_name_or_email"
INSUFFICIENT_CONTACT_DATA = "insufficient_contact_data"
BLANK_COMPANY_NAME = "blank_company_name"
BLOCKING_ERROR = "blocking_error"
INVALID_STATE_DETAIL = INVALID_STATE
INVALID_DATE_DETAIL = INVALID_DATE

_NON_ALNUM_PERSON = re.compile(r"[^a-z0-9]+")


def _blank(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_email(value: str) -> str:
    return _blank(value).lower()


def _norm_person_name(value: str) -> str:
    s = _NON_ALNUM_PERSON.sub(" ", _blank(value).lower())
    return " ".join(s.split())


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in _blank(full_name).split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _resolve_contact_names(mapped: dict[str, str]) -> tuple[str, str, str]:
    first = _blank(mapped.get("contact_first_name"))
    last = _blank(mapped.get("contact_last_name"))
    full = _blank(mapped.get("contact_full_name"))
    if not first and not last and full:
        first, last = _split_full_name(full)
    if not full:
        full = f"{first} {last}".strip()
    return first, last, full


def _proposed_key(kind: str, source_row_number: int, row_id: int) -> str:
    return f"proposed:{kind}:{int(source_row_number)}:{int(row_id)}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _cap_mapped(value: str) -> str:
    text = _blank(value)
    if len(text) <= CELL_PREVIEW_CHARS:
        return text
    return text[:CELL_PREVIEW_CHARS]


def _row_get(row, key: str, default: str = "") -> str:
    if key not in row.keys():
        return default
    return _blank(row[key])


def _row_get_opt_int(row, key: str) -> int | None:
    if key not in row.keys():
        return None
    value = row[key]
    if value is None or value == "":
        return None
    return int(value)


def _chunked(ids: list[int], size: int = IN_CHUNK) -> Iterable[list[int]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


@dataclass(slots=True)
class CompanyRec:
    company_id: int | None
    proposed_key: str | None
    name: str
    norm_name: str
    domain: str
    phone: str
    addr: str
    city: str
    state: str
    record_no: str
    source_row: int | None = None
    alias_record_nos: tuple[str, ...] = ()
    alias_norm_names: tuple[str, ...] = ()
    alias_domains: tuple[str, ...] = ()
    alias_phones: tuple[str, ...] = ()
    alias_addrs: tuple[tuple[str, str, str], ...] = ()
    display_address: str = ""
    display_city: str = ""
    display_state: str = ""
    display_zip: str = ""
    display_phone: str = ""
    display_website: str = ""
    identity_record_nos: tuple[str, ...] = ()


@dataclass(slots=True)
class PackedCompanyPossible:
    company_id: int
    name: str
    record_no: str
    reasons: list[str]
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    website: str = ""
    identity_record_nos: tuple[str, ...] = ()


@dataclass(slots=True)
class PackedContactPossible:
    contact_id: int
    display_name: str
    reasons: list[str]
    email: str = ""
    phone: str = ""
    company_id: int | None = None
    company_name: str = ""


@dataclass(slots=True)
class ContactRec:
    contact_id: int | None
    proposed_key: str | None
    company_id: int | None
    company_key: str
    display_name: str
    norm_name: str
    email: str
    nanp10: str
    last7: str
    source_row: int | None = None


@dataclass(slots=True)
class StagedPlanRow:
    row_id: int
    source_row_number: int
    raw_sha256: str
    has_blocking_error: bool
    mapped: dict[str, str]
    company_name: str
    website: str
    phone: str
    address: str
    city: str
    state: str
    state_invalid: bool
    date_invalid: bool
    zip: str
    contact_first: str
    contact_last: str
    contact_full: str
    contact_title: str
    contact_email: str
    contact_phone: str
    relationship_status: str
    relationship_notes: str
    norm_company: str
    company_domain: str
    company_phone_digits: str
    company_addr: str
    email_norm: str
    nanp10: str
    last7: str
    person_norm: str
    has_any_contact_field: bool
    can_create_contact: bool


@dataclass(slots=True)
class RelationshipRec:
    relationship_id: int
    company_id: int
    status: str
    notes: str


@dataclass(slots=True)
class ProposedRelationshipState:
    proposed_key: str
    status: str
    notes: str


@dataclass(slots=True)
class RowDecision:
    row_id: int
    source_row_number: int
    validity: str
    validity_detail: str
    company_action: str
    company_id: int | None
    company_proposed_key: str | None
    company_reasons: list[str]
    company_name: str
    company_created_at: int | None
    company_possibles: list[PackedCompanyPossible]
    contact_action: str
    contact_id: int | None
    contact_proposed_key: str | None
    contact_reasons: list[str]
    contact_name: str
    contact_created_at: int | None
    contact_possibles: list[PackedContactPossible]
    relationship_action: str
    relationship_id: int | None
    relationship_proposed_key: str | None
    status_action: str
    notes_action: str
    resolved_status: str
    planned_notes: str
    original_status_action: str
    status_resolution_type: str
    status_resolution_updated_at: str
    status_resolution_updated_by_user_id: int | None
    match_resolution_type: str
    match_resolution_updated_at: str
    match_resolution_updated_by_user_id: int | None
    existing_status: str
    mapped: dict[str, str]
    identity_bound: bool = False
    can_create_company: bool = True


@dataclass
class ImportPlan:
    batch_id: int
    client_id: int
    planner_version: str
    plan_fingerprint: str
    mapping_updated_at: str
    total_rows: int
    counts: dict[str, int]
    rows: list[RowDecision]
    stats: dict[str, int] = field(default_factory=dict)
    status_catalog: list[str] = field(default_factory=list)
    use_imported_status_for_existing: bool = False
    source_type: str = DEFAULT_SOURCE_TYPE
    original_filename: str = ""
    mapping: dict[str, str] = field(default_factory=dict)
    client_name: str = ""


def _company_score(reasons: list[str]) -> int:
    return (
        ("record_no_exact" in reasons) * 200
        + ("source_identity_exact" in reasons) * 250
        + ("merge_redirect" in reasons) * 220
        + ("domain_exact" in reasons) * 100
        + ("name_exact" in reasons) * 40
        + ("phone" in reasons) * 20
        + ("address_city_state" in reasons) * 15
        + len(reasons)
    )


def _phones_compatible(left: str, right: str) -> bool:
    if not left or not right or len(left) < 7 or len(right) < 7:
        return False
    return left == right or left.endswith(right[-7:]) or right.endswith(left[-7:])


def _alias_record_nos(cand: CompanyRec) -> set[str]:
    found = {normalize_record_no(rn) for rn in cand.alias_record_nos}
    found.discard("")
    return found


def _company_reasons(query: CompanyRec, cand: CompanyRec) -> list[str]:
    reasons: list[str] = []
    qrn = normalize_record_no(query.record_no)
    crn = normalize_record_no(cand.record_no)
    alias_rns = _alias_record_nos(cand)
    if qrn and crn and qrn == crn:
        reasons.append("record_no_exact")
        return reasons
    if qrn and qrn in alias_rns:
        reasons.append("record_no_exact")
        reasons.append("alias_record_no")
        return reasons
    domains = {cand.domain, *cand.alias_domains}
    domains.discard("")
    if query.domain and query.domain in domains:
        reasons.append("domain_exact")
    names = {cand.norm_name, *cand.alias_norm_names}
    names.discard("")
    if query.norm_name and query.norm_name in names:
        reasons.append("name_exact")
    phones = [p for p in (cand.phone, *cand.alias_phones) if p]
    if query.phone and any(_phones_compatible(query.phone, phone) for phone in phones):
        reasons.append("phone")
    if query.addr and query.city and query.state:
        addrs = {(cand.addr, cand.city, cand.state), *cand.alias_addrs}
        if (query.addr, query.city, query.state) in addrs:
            reasons.append("address_city_state")
    return reasons


def _identity_disagrees(query: CompanyRec, cand: CompanyRec) -> bool:
    """True when incoming identity fields conflict with a candidate.

    Empty-vs-present is not a conflict. Alias source identity is an alternative
    to the canonical master, so a plant alias with its own city is not treated
    as conflicting with HQ. Used so a unique normalized name cannot auto-reuse
    a stored master (or an in-batch proposed company) when RN, domain, phone,
    street, city, or state disagree with both the master and its aliases.
    """
    qrn = normalize_record_no(query.record_no)
    crns = {normalize_record_no(cand.record_no), *_alias_record_nos(cand)}
    crns.discard("")
    if qrn and crns and qrn not in crns:
        return True
    domains = {cand.domain, *cand.alias_domains}
    domains.discard("")
    if query.domain and domains and query.domain not in domains:
        return True
    phones = [p for p in (cand.phone, *cand.alias_phones) if p]
    if query.phone and phones and not any(_phones_compatible(query.phone, phone) for phone in phones):
        return True
    cities = {cand.city, *(item[1] for item in cand.alias_addrs)}
    cities.discard("")
    if query.city and cities and query.city not in cities:
        return True
    states = {cand.state, *(item[2] for item in cand.alias_addrs)}
    states.discard("")
    if query.state and states and query.state not in states:
        return True
    addrs = {cand.addr, *(item[0] for item in cand.alias_addrs)}
    addrs.discard("")
    if query.addr and addrs and query.addr not in addrs:
        return True
    return False


def _classify_company(
    query: CompanyRec,
    hits: list[tuple[CompanyRec, list[str]]],
) -> tuple[str, CompanyRec | None, list[str], list[tuple[CompanyRec, list[str]]]]:
    high: list[tuple[CompanyRec, list[str]]] = []
    possible: list[tuple[CompanyRec, list[str]]] = []
    for cand, reasons in hits:
        if "record_no_exact" in reasons:
            high.append((cand, reasons))
        elif "domain_exact" in reasons:
            high.append((cand, reasons))
        elif "name_exact" in reasons and (
            "phone" in reasons or "address_city_state" in reasons or "domain_exact" in reasons
        ):
            high.append((cand, reasons))
        elif "name_exact" in reasons and len(reasons) == 1:
            if _identity_disagrees(query, cand) or cand.company_id is not None:
                # Stored masters never auto-reuse on name alone. In-batch proposed
                # companies also stay possible when identity fields disagree.
                possible.append((cand, reasons))
                continue
            name_only = [item for item in hits if "name_exact" in item[1]]
            if len(name_only) == 1:
                high.append((cand, reasons))
            else:
                possible.append((cand, reasons))
        elif reasons:
            possible.append((cand, reasons))

    if high:
        high.sort(key=lambda item: _company_score(item[1]), reverse=True)
        best, best_reasons = high[0]
        if len(high) > 1 and _company_score(high[1][1]) >= _company_score(high[0][1]) - 20:
            return "possible_company_match", best, best_reasons, high
        return "use_existing_company", best, best_reasons, possible
    if possible:
        return "possible_company_match", possible[0][0], possible[0][1], possible
    return "create_company", None, [], []


def _parse_stored_mapping(raw: object, headers: list[str]) -> dict[str, str]:
    text = _blank(raw)
    if not text:
        raise ValueError(MAPPING_COMPANY_REQUIRED)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(INVALID_SAVED_MAPPING) from exc
    if not isinstance(parsed, dict):
        raise ValueError(INVALID_SAVED_MAPPING)
    as_str = {str(k): "" if v is None else str(v) for k, v in parsed.items()}
    return _normalize_mapping(as_str, headers)


def _parse_headers(raw: object) -> list[str]:
    text = _blank(raw)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(v) for v in parsed if str(v).strip()]


def _parse_raw_values(raw: object) -> dict[str, str]:
    text = raw if isinstance(raw, str) else _blank(raw)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in parsed.items()}


def _apply_mapping(values: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for dest, header in mapping.items():
        mapped[dest] = _blank(values.get(header, ""))
    return mapped


def _query_company(mapped: dict[str, str]) -> CompanyRec:
    name = _blank(mapped.get("company_name"))
    website = _blank(mapped.get("website"))
    phone = _blank(mapped.get("phone"))
    address = _blank(mapped.get("address"))
    city = _blank(mapped.get("city"))
    # Mapped state is already normalized (or blank) for valid rows.
    state = state_for_match(mapped.get("state"))
    return CompanyRec(
        company_id=None,
        proposed_key=None,
        name=name,
        norm_name=norm_name(name) if name else "",
        domain=domain(website),
        phone=digits_phone(phone),
        addr=norm_addr(address) if address else "",
        city=_blank(city).lower(),
        state=state,
        record_no=normalize_record_no(mapped.get("external_record_no")),
    )


def _company_ref(action_id: int | None, proposed_key: str | None) -> str | None:
    if action_id is not None:
        return f"db:{int(action_id)}"
    if proposed_key:
        return proposed_key
    return None


def _is_expired(expires_at: str, now: str) -> bool:
    exp = _blank(expires_at)
    return bool(exp) and exp < now


def _build_staged_row(row, mapping: dict[str, str]) -> StagedPlanRow:
    raw_text = row["raw_json"] if "raw_json" in row.keys() else ""
    raw_text = "" if raw_text is None else str(raw_text)
    values = _parse_raw_values(raw_text)
    mapped = _apply_mapping(values, mapping)
    first, last, full = _resolve_contact_names(mapped)
    email = _norm_email(mapped.get("contact_email", ""))
    phone = _blank(mapped.get("contact_phone"))
    nanp10, last7 = canonical_contact_phone(phone)
    title = _blank(mapped.get("contact_title"))
    person = _norm_person_name(full)
    has_any_contact_field = bool(first or last or full or title or email or phone)
    can_create = bool(full or email)
    relationship_status = _blank(mapped.get("relationship_status"))
    # Keep full notes for planning; dry-run mapped display remains capped.
    relationship_notes_raw = "" if mapped.get("relationship_notes") is None else str(
        mapped.get("relationship_notes") or ""
    )
    raw_state = _blank(mapped.get("state"))
    normalized_state = normalize_us_state(raw_state)
    state_invalid = normalized_state is None
    if state_invalid:
        # Keep trimmed original for review display; never treat as matchable.
        mapped["state"] = raw_state
        stored_state = ""
    else:
        mapped["state"] = normalized_state or ""
        stored_state = normalized_state or ""

    date_invalid = False
    for date_field in ("source_entered_at", "source_updated_at"):
        raw_date = mapped.get(date_field)
        # Preserve full mapped text before capping; normalize blank/serial/string.
        raw_date_text = "" if raw_date is None else str(raw_date)
        normalized_date = normalize_import_datetime(raw_date_text)
        if normalized_date is None:
            date_invalid = True
            mapped[date_field] = _blank(raw_date_text)
        else:
            mapped[date_field] = normalized_date

    return StagedPlanRow(
        row_id=int(row["id"]),
        source_row_number=int(row["source_row_number"] or 0),
        raw_sha256=_sha256_text(raw_text),
        has_blocking_error=bool(row["has_blocking_error"]),
        mapped={k: _cap_mapped(v) for k, v in mapped.items()},
        company_name=_blank(mapped.get("company_name")),
        website=_blank(mapped.get("website")),
        phone=_blank(mapped.get("phone")),
        address=_blank(mapped.get("address")),
        city=_blank(mapped.get("city")),
        state=stored_state,
        state_invalid=state_invalid,
        date_invalid=date_invalid,
        zip=_blank(mapped.get("zip")),
        contact_first=first,
        contact_last=last,
        contact_full=full,
        contact_title=title,
        contact_email=_blank(mapped.get("contact_email")),
        contact_phone=phone,
        relationship_status=relationship_status,
        relationship_notes=relationship_notes_raw,
        norm_company=norm_name(_blank(mapped.get("company_name"))),
        company_domain=domain(_blank(mapped.get("website"))),
        company_phone_digits=digits_phone(_blank(mapped.get("phone"))),
        company_addr=norm_addr(_blank(mapped.get("address"))) if _blank(mapped.get("address")) else "",
        email_norm=email,
        nanp10=nanp10,
        last7=last7,
        person_norm=person,
        has_any_contact_field=has_any_contact_field,
        can_create_contact=can_create,
    )


def _empty_decision(staged: StagedPlanRow, validity: str, detail: str) -> RowDecision:
    return RowDecision(
        row_id=staged.row_id,
        source_row_number=staged.source_row_number,
        validity=validity,
        validity_detail=detail,
        company_action="none",
        company_id=None,
        company_proposed_key=None,
        company_reasons=[],
        company_name=staged.company_name,
        company_created_at=None,
        company_possibles=[],
        contact_action="none",
        contact_id=None,
        contact_proposed_key=None,
        contact_reasons=[],
        contact_name=staged.contact_full,
        contact_created_at=None,
        contact_possibles=[],
        relationship_action="none",
        relationship_id=None,
        relationship_proposed_key=None,
        status_action="none",
        notes_action="none",
        resolved_status="",
        planned_notes="",
        original_status_action="none",
        status_resolution_type="",
        status_resolution_updated_at="",
        status_resolution_updated_by_user_id=None,
        match_resolution_type="",
        match_resolution_updated_at="",
        match_resolution_updated_by_user_id=None,
        existing_status="",
        mapped=dict(staged.mapped),
    )


def _pack_company_possibles(
    items: list[tuple[CompanyRec, list[str]]],
) -> list[PackedCompanyPossible]:
    packed: list[PackedCompanyPossible] = []
    seen: set[int] = set()
    ordered = sorted(
        [(c, r) for c, r in items if c.company_id is not None],
        key=lambda item: int(item[0].company_id or 0),
    )
    for cand, reasons in ordered:
        cid = int(cand.company_id or 0)
        if cid in seen:
            continue
        seen.add(cid)
        packed.append(
            PackedCompanyPossible(
                company_id=cid,
                name=cand.name,
                record_no=cand.record_no,
                reasons=list(reasons),
                address=cand.display_address or cand.addr,
                city=cand.display_city or cand.city,
                state=cand.display_state or cand.state,
                zip=cand.display_zip,
                phone=cand.display_phone or cand.phone,
                website=cand.display_website or cand.domain,
                identity_record_nos=tuple(cand.identity_record_nos),
            )
        )
    return packed


def _match_company(
    query: CompanyRec,
    db_companies: list[CompanyRec],
    proposed: list[CompanyRec],
    *,
    record_no_exclusive: bool = False,
) -> tuple[str, CompanyRec | None, list[str], list[tuple[CompanyRec, list[str]]]]:
    qrn = normalize_record_no(query.record_no)
    if qrn:
        exact: list[tuple[CompanyRec, list[str]]] = []
        for cand in (*db_companies, *proposed):
            if normalize_record_no(cand.record_no) == qrn:
                exact.append((cand, ["record_no_exact"]))
            elif qrn in _alias_record_nos(cand):
                exact.append((cand, ["record_no_exact", "alias_record_no"]))
        if exact:
            return _classify_company(query, exact)
        if record_no_exclusive:
            return "create_company", None, [], []
    hits: list[tuple[CompanyRec, list[str]]] = []
    for cand in (*db_companies, *proposed):
        reasons = _company_reasons(query, cand)
        if reasons:
            hits.append((cand, reasons))
    return _classify_company(query, hits)


def _counts_from_rows(rows: list[RowDecision]) -> dict[str, int]:
    counts = {
        "blocking_error": 0,
        "invalid_mapping_data": 0,
        "ok": 0,
        "create_company": 0,
        "use_existing_company": 0,
        "possible_company_match": 0,
        "create_contact": 0,
        "use_existing_contact": 0,
        "possible_contact_match": 0,
        "insufficient_contact_data": 0,
        "no_contact_data": 0,
        "contact_deferred": 0,
        "create_client_relationship": 0,
        "relationship_already_exists": 0,
        "relationship_deferred": 0,
        "use_default_status": 0,
        "preserve_existing_status": 0,
        "use_imported_status": 0,
        "status_conflict": 0,
        "invalid_status": 0,
        "update_existing_status": 0,
        "no_notes_change": 0,
        "set_imported_notes": 0,
        "append_imported_notes": 0,
        "imported_notes_already_present": 0,
        "importable_rows": 0,
        "needs_review_rows": 0,
        "excluded_closed": 0,
        "excluded_skip": 0,
    }
    for row in rows:
        if row.company_action in EXCLUDED_ACTIONS:
            counts[row.company_action] += 1
            continue
        if row.validity == "blocking_error":
            counts["blocking_error"] += 1
        elif row.validity == "invalid_mapping_data":
            counts["invalid_mapping_data"] += 1
        elif row.validity == "ok":
            counts["ok"] += 1
        if row.company_action in counts:
            counts[row.company_action] += 1
        if row.contact_action == "deferred":
            counts["contact_deferred"] += 1
        elif row.contact_action in counts:
            counts[row.contact_action] += 1
        if row.relationship_action == "deferred":
            counts["relationship_deferred"] += 1
        elif row.relationship_action in counts:
            counts[row.relationship_action] += 1
        if row.status_action in counts:
            counts[row.status_action] += 1
        if (
            row.relationship_action == "relationship_already_exists"
            and row.status_action == STATUS_USE_IMPORTED
            and normalize_status_key(row.existing_status)
            != normalize_status_key(row.resolved_status)
            and _blank(row.resolved_status)
        ):
            counts["update_existing_status"] += 1
        if row.notes_action in counts:
            counts[row.notes_action] += 1
        importable = (
            row.validity == "ok"
            and row.company_action in {"create_company", "use_existing_company"}
            and row.contact_action
            in {"create_contact", "use_existing_contact", "no_contact_data"}
            and row.relationship_action
            in {"create_client_relationship", "relationship_already_exists"}
            and row.status_action
            in {STATUS_USE_DEFAULT, STATUS_PRESERVE, STATUS_USE_IMPORTED}
            and row.notes_action
            in {NOTES_NO_CHANGE, NOTES_SET, NOTES_APPEND, NOTES_ALREADY_PRESENT}
        )
        if importable:
            counts["importable_rows"] += 1
        else:
            counts["needs_review_rows"] += 1
    return counts


def _fingerprint_payload(
    *,
    client_id: int,
    batch_id: int,
    file_sha256: str,
    mapping: dict[str, str],
    mapping_updated_at: str,
    mapping_updated_by_user_id: int | None,
    status: str,
    expires_at: str,
    staged: list[StagedPlanRow],
    decisions: list[RowDecision],
    use_imported_status_for_existing: bool = False,
    source_type: str = DEFAULT_SOURCE_TYPE,
) -> dict[str, Any]:
    return {
        "schema": PLANNER_VERSION,
        "client_id": int(client_id),
        "batch_id": int(batch_id),
        "file_sha256": file_sha256,
        "mapping": mapping,
        "mapping_updated_at": mapping_updated_at,
        "mapping_updated_by_user_id": mapping_updated_by_user_id,
        "status": status,
        "expires_at": expires_at,
        "use_imported_status_for_existing": bool(use_imported_status_for_existing),
        "source_type": source_type,
        "total_rows": len(staged),
        "staged_rows": [
            {
                "row_id": r.row_id,
                "source_row_number": r.source_row_number,
                "raw_sha256": r.raw_sha256,
            }
            for r in staged
        ],
        "decisions": [
            {
                "row_id": d.row_id,
                "source_row_number": d.source_row_number,
                "validity": d.validity,
                "company_action": d.company_action,
                "company_id": d.company_id,
                "company_proposed_key": d.company_proposed_key,
                "company_reasons": list(d.company_reasons),
                "company_possible": [
                    {"company_id": item.company_id, "reasons": list(item.reasons)}
                    for item in d.company_possibles
                ],
                "contact_action": d.contact_action,
                "contact_id": d.contact_id,
                "contact_proposed_key": d.contact_proposed_key,
                "contact_reasons": list(d.contact_reasons),
                "contact_possible": [
                    {"contact_id": item.contact_id, "reasons": list(item.reasons)}
                    for item in d.contact_possibles
                ],
                "relationship_action": d.relationship_action,
                "relationship_id": d.relationship_id,
                "relationship_proposed_key": d.relationship_proposed_key,
                "status_action": d.status_action,
                "notes_action": d.notes_action,
                "resolved_status": d.resolved_status,
                "existing_status": d.existing_status,
                "original_status_action": d.original_status_action,
                "status_resolution_type": d.status_resolution_type,
                "status_resolution_updated_at": d.status_resolution_updated_at,
                "status_resolution_updated_by_user_id": d.status_resolution_updated_by_user_id,
                "match_resolution_type": d.match_resolution_type,
                "match_resolution_updated_at": d.match_resolution_updated_at,
                "match_resolution_updated_by_user_id": d.match_resolution_updated_by_user_id,
                "identity_bound": bool(d.identity_bound),
                "normalized_state": (
                    ""
                    if d.validity_detail == INVALID_STATE_DETAIL
                    else _blank((d.mapped or {}).get("state"))
                ),
                "state_invalid": d.validity_detail == INVALID_STATE_DETAIL,
                "source_entered_at": _blank((d.mapped or {}).get("source_entered_at")),
                "source_updated_at": _blank((d.mapped or {}).get("source_updated_at")),
                "date_invalid": d.validity_detail == INVALID_DATE_DETAIL,
            }
            for d in decisions
        ],
    }


def _chunked_values(values: list[Any], size: int = IN_CHUNK) -> Iterable[list[Any]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _resolve_canonical_company_id(conn, company_id: int) -> int:
    cid = int(company_id)
    if cid <= 0:
        return cid
    try:
        from company_merges import resolve_company_id

        return int(resolve_company_id(conn, cid))
    except Exception:
        return cid


def _load_source_identity_map(
    conn,
    *,
    client_id: int,
    source_type: str,
    record_nos: set[str],
) -> dict[str, int]:
    if not is_leadmaster_source(source_type) or not record_nos:
        return {}
    if not _table_exists(conn, "company_source_identities"):
        return {}
    found: dict[str, int] = {}
    values = sorted(rn for rn in record_nos if rn)
    for chunk in _chunked_values(values):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT source_record_no, company_id
            FROM company_source_identities
            WHERE source_system = 'LEADMASTER'
              AND source_record_no IN ({placeholders})
              AND COALESCE(client_id, 0) = COALESCE(?, 0)
            """,
            (*chunk, int(client_id)),
        ).fetchall()
        for raw in rows:
            rn = normalize_record_no(raw["source_record_no"])
            if not rn or rn in found:
                continue
            found[rn] = _resolve_canonical_company_id(conn, int(raw["company_id"]))
    return found


def _attach_source_identities(conn, companies: list[CompanyRec], client_id: int) -> None:
    ids = [int(c.company_id) for c in companies if c.company_id is not None]
    if not ids or not _table_exists(conn, "company_source_identities"):
        return
    by_company: dict[int, list[str]] = {cid: [] for cid in ids}
    for chunk in _chunked(sorted(set(ids))):
        placeholders = ",".join("?" * len(chunk))
        for raw in conn.execute(
            f"""
            SELECT company_id, source_record_no
            FROM company_source_identities
            WHERE company_id IN ({placeholders})
              AND source_system = 'LEADMASTER'
              AND COALESCE(client_id, 0) = COALESCE(?, 0)
            """,
            (*chunk, int(client_id)),
        ):
            cid = int(raw["company_id"])
            rn = normalize_record_no(raw["source_record_no"])
            if rn and rn not in by_company.setdefault(cid, []):
                by_company[cid].append(rn)
    for cand in companies:
        if cand.company_id is None:
            continue
        cand.identity_record_nos = tuple(by_company.get(int(cand.company_id), []))


def _collect_identity_company_ids(
    conn,
    *,
    record_nos: set[str],
    domains: set[str],
    names: set[str],
    phones: set[str],
    last7: set[str],
    addr_keys: set[tuple[str, str, str]],
) -> set[int]:
    """Probe company_identity_keys in chunks. Never scan companies."""
    found: set[int] = set()

    def _add_from(sql: str, values: list[Any]) -> None:
        if not values:
            return
        for chunk in _chunked_values(values):
            placeholders = ",".join("?" * len(chunk))
            for raw in conn.execute(sql.format(placeholders=placeholders), chunk):
                found.add(int(raw[0] if not hasattr(raw, "keys") else raw["company_id"]))

    # Include partial-index predicates (col != '') so SQLite uses covering indexes.
    _add_from(
        "SELECT company_id FROM company_identity_keys "
        "WHERE record_no != '' AND record_no IN ({placeholders})",
        sorted(record_nos),
    )
    _add_from(
        "SELECT company_id FROM company_identity_keys "
        "WHERE domain != '' AND domain IN ({placeholders})",
        sorted(domains),
    )
    _add_from(
        "SELECT company_id FROM company_identity_keys "
        "WHERE norm_name != '' AND norm_name IN ({placeholders})",
        sorted(names),
    )
    _add_from(
        "SELECT company_id FROM company_identity_keys "
        "WHERE phone_digits != '' AND phone_digits IN ({placeholders})",
        sorted(phones),
    )
    _add_from(
        "SELECT company_id FROM company_identity_keys "
        "WHERE phone_last7 != '' AND phone_last7 IN ({placeholders})",
        sorted(last7),
    )
    addr_list = sorted(addr_keys)
    for chunk in _chunked_values(addr_list):
        clauses = " OR ".join(
            [
                "(addr_norm != '' AND addr_norm = ? AND city_norm = ? AND state_norm = ?)"
            ]
            * len(chunk)
        )
        params: list[str] = []
        for addr, city, state in chunk:
            params.extend([addr, city, state])
        for raw in conn.execute(
            f"SELECT company_id FROM company_identity_keys WHERE {clauses}",
            params,
        ):
            found.add(int(raw[0] if not hasattr(raw, "keys") else raw["company_id"]))
    return found


def _load_matching_companies(
    conn,
    staged: list[StagedPlanRow],
    *,
    client_id: int,
    source_type: str,
) -> tuple[list[CompanyRec], dict[str, int]]:
    from crm_identity_keys import require_company_identity_ready

    require_company_identity_ready(conn)
    domains = {r.company_domain for r in staged if r.company_domain}
    names = {r.norm_company for r in staged if r.norm_company}
    phones = {r.company_phone_digits for r in staged if r.company_phone_digits}
    last7 = {p[-7:] for p in phones if len(p) >= 7}
    record_nos = {
        normalize_record_no(r.mapped.get("external_record_no"))
        for r in staged
        if normalize_record_no(r.mapped.get("external_record_no"))
    }
    addr_keys = {
        (r.company_addr, _blank(r.city).lower(), state_for_match(r.state))
        for r in staged
        if r.company_addr and _blank(r.city) and _blank(r.state) and not r.state_invalid
    }
    # Drop incomplete address triples (state_for_match may blank invalid states).
    addr_keys = {
        (a, c, s) for (a, c, s) in addr_keys if a and c and s
    }
    from company_aliases import collect_alias_company_ids

    identity_map = _load_source_identity_map(
        conn,
        client_id=client_id,
        source_type=source_type,
        record_nos=record_nos,
    )

    if not (domains or names or last7 or addr_keys or record_nos or phones or identity_map):
        return [], identity_map

    company_ids = _collect_identity_company_ids(
        conn,
        record_nos=record_nos,
        domains=domains,
        names=names,
        phones=phones,
        last7=last7,
        addr_keys=addr_keys,
    )
    company_ids |= collect_alias_company_ids(
        conn,
        record_nos=record_nos,
        names=names,
    )
    company_ids |= set(identity_map.values())
    if not company_ids:
        return [], identity_map

    kept: list[CompanyRec] = []
    for chunk in _chunked(sorted(company_ids)):
        placeholders = ",".join("?" * len(chunk))
        for raw in conn.execute(
            f"""
            SELECT
                c.id,
                c.company_name,
                COALESCE(c.address, '') AS address,
                COALESCE(c.city, '') AS city,
                COALESCE(c.state, '') AS state,
                COALESCE(c.zip, '') AS zip,
                COALESCE(c.website, '') AS website,
                COALESCE(c.legacy_phone, '') AS legacy_phone,
                k.record_no,
                k.domain,
                k.norm_name,
                k.phone_digits,
                k.addr_norm,
                k.city_norm,
                k.state_norm
            FROM companies c
            JOIN company_identity_keys k ON k.company_id = c.id
            WHERE c.id IN ({placeholders})
            """,
            chunk,
        ):
            kept.append(
                CompanyRec(
                    company_id=int(raw["id"]),
                    proposed_key=None,
                    name=_blank(raw["company_name"]),
                    norm_name=_blank(raw["norm_name"]),
                    domain=_blank(raw["domain"]),
                    phone=_blank(raw["phone_digits"]),
                    addr=_blank(raw["addr_norm"]),
                    city=_blank(raw["city_norm"]),
                    state=_blank(raw["state_norm"]),
                    record_no=_blank(raw["record_no"]),
                    display_address=_blank(raw["address"]),
                    display_city=_blank(raw["city"]),
                    display_state=_blank(raw["state"]),
                    display_zip=_blank(raw["zip"]),
                    display_phone=_blank(raw["legacy_phone"]),
                    display_website=_blank(raw["website"]),
                )
            )
    _attach_company_aliases(conn, kept)
    _attach_source_identities(conn, kept, client_id)
    return kept, identity_map


def _attach_company_aliases(conn, companies: list[CompanyRec]) -> None:
    from company_aliases import alias_match_fields, load_aliases_by_company

    ids = [int(c.company_id) for c in companies if c.company_id is not None]
    if not ids:
        return
    by_company = load_aliases_by_company(conn, ids)
    for cand in companies:
        if cand.company_id is None:
            continue
        fields = alias_match_fields(by_company.get(int(cand.company_id), []))
        cand.alias_record_nos = fields["record_nos"]
        cand.alias_norm_names = fields["norm_names"]
        cand.alias_domains = fields["domains"]
        cand.alias_phones = fields["phones"]
        cand.alias_addrs = fields["addrs"]


def _load_relationships(conn, client_id: int) -> dict[int, RelationshipRec]:
    found: dict[int, RelationshipRec] = {}
    for raw in conn.execute(
        """
        SELECT id, company_id, status, notes
        FROM client_company_relationships
        WHERE client_id = ?
        """,
        (client_id,),
    ):
        company_id = int(raw["company_id"])
        found[company_id] = RelationshipRec(
            relationship_id=int(raw["id"]),
            company_id=company_id,
            status=_blank(raw["status"]),
            notes="" if raw["notes"] is None else str(raw["notes"]),
        )
    return found


def _load_contacts_for_companies(
    conn, company_ids: list[int]
) -> tuple[dict[int, list[ContactRec]], dict[int, list[tuple[str, str]]]]:
    by_company: dict[int, list[ContactRec]] = {cid: [] for cid in company_ids}
    if not company_ids:
        return by_company, {}
    contacts: dict[int, ContactRec] = {}
    for chunk in _chunked(sorted(set(company_ids))):
        placeholders = ",".join("?" * len(chunk))
        for raw in conn.execute(
            f"""
            SELECT id, company_id, first_name, last_name, email, phone, alt_phone
            FROM contacts
            WHERE company_id IN ({placeholders})
            """,
            chunk,
        ):
            cid = int(raw["id"])
            company_id = int(raw["company_id"])
            first = _blank(raw["first_name"])
            last = _blank(raw["last_name"])
            display = f"{first} {last}".strip()
            email = _norm_email(_blank(raw["email"]))
            nanp10, last7 = canonical_contact_phone(_blank(raw["phone"]))
            alt10, alt7 = canonical_contact_phone(_blank(raw["alt_phone"]) if "alt_phone" in raw.keys() else "")
            rec = ContactRec(
                contact_id=cid,
                proposed_key=None,
                company_id=company_id,
                company_key=f"db:{company_id}",
                display_name=display,
                norm_name=_norm_person_name(display),
                email=email,
                nanp10=nanp10 or alt10,
                last7=last7 or alt7,
            )
            contacts[cid] = rec
            by_company.setdefault(company_id, []).append(rec)
    phone_keys: dict[int, list[tuple[str, str]]] = {cid: [] for cid in contacts}
    if contacts and _table_exists(conn, "contact_phone_keys"):
        for chunk in _chunked(sorted(contacts)):
            placeholders = ",".join("?" * len(chunk))
            for raw in conn.execute(
                f"""
                SELECT contact_id, nanp10, last7
                FROM contact_phone_keys
                WHERE contact_id IN ({placeholders})
                """,
                chunk,
            ):
                contact_id = int(raw["contact_id"])
                nanp10 = _blank(raw["nanp10"])
                last7 = _blank(raw["last7"])
                phone_keys.setdefault(contact_id, []).append((nanp10, last7))
                rec = contacts.get(contact_id)
                if rec is None:
                    continue
                if nanp10 and not rec.nanp10:
                    rec.nanp10 = nanp10
                if last7 and not rec.last7:
                    rec.last7 = last7
    for contact_id, rec in contacts.items():
        if contact_id not in phone_keys or not phone_keys[contact_id]:
            phone_keys[contact_id] = [(rec.nanp10, rec.last7)]
    return by_company, phone_keys


def _load_name_elsewhere(conn, staged_names: set[str]) -> dict[str, set[int]]:
    """Batched person_norm lookup — never scans the full contacts table."""
    found: dict[str, set[int]] = {name: set() for name in staged_names if name}
    if not found:
        return found
    from crm_identity_keys import _table_exists as _id_table_exists

    if not _id_table_exists(conn, "contact_person_keys"):
        # Optional warning only; omit rather than full-table scan at scale.
        return found
    names = sorted(found)
    for chunk in _chunked_values(names):
        placeholders = ",".join("?" * len(chunk))
        for raw in conn.execute(
            f"""
            SELECT company_id, person_norm
            FROM contact_person_keys
            WHERE person_norm != ''
              AND person_norm IN ({placeholders})
            """,
            chunk,
        ):
            key = _blank(raw["person_norm"])
            bucket = found.get(key)
            if bucket is None or len(bucket) >= 2:
                continue
            bucket.add(int(raw["company_id"]))
    return found


def _name_exists_elsewhere(
    person_norm: str,
    resolved_company_id: int | None,
    name_companies: dict[str, set[int]],
) -> bool:
    if not person_norm:
        return False
    seen = name_companies.get(person_norm) or set()
    if not seen:
        return False
    if resolved_company_id is None:
        return True
    return any(cid != resolved_company_id for cid in seen)


def plan_crm_import_batch(
    conn,
    *,
    client_id: int,
    batch_id: int,
    identity_mode: str = IDENTITY_CRM,
    exclude_closed: bool = False,
    use_imported_status_for_existing: bool = False,
) -> ImportPlan:
    """Read-only full-batch plan. Caller supplies the connection.

    Does not set PRAGMA query_only. The HTTP dry-run wrapper enables that
    guard. A future confirm may call this inside BEGIN IMMEDIATE and then write.

    identity_mode:
      - crm: never auto-reuse a stored master on normalized name alone;
        name plus domain/phone/address+city/state, an exact record number,
        or an exact alias record number may still auto-reuse. Alias name
        plus corroborating alias/master identity may reuse; alias name
        alone does not. Unique in-batch proposed names still group
        contacts when identity fields do not disagree.
      - client_data: LeadMaster Record No. first; never reuse on name alone;
        when Record No. present and unmatched → create (no fuzzy fallback)
    exclude_closed: Closed status rows are excluded for the selected client
      (not deleted from the shared master).
    use_imported_status_for_existing: when True, valid nonblank imported
      statuses update existing relationships for this client (default False).
    """
    client_data_mode = identity_mode == IDENTITY_CLIENT_DATA
    record_no_exclusive = client_data_mode
    use_imported_for_existing = bool(use_imported_status_for_existing)
    if not conn.in_transaction:
        conn.execute("BEGIN")
    now = _now_iso()

    batch = conn.execute(
        """
        SELECT * FROM crm_import_batches
        WHERE id = ? AND client_id = ?
        """,
        (batch_id, client_id),
    ).fetchone()
    if batch is None:
        raise LookupError("Import batch not found.")
    from leadmaster_refresh_staging import refuse_refresh_as_initial_import

    refuse_refresh_as_initial_import(batch)

    status = _row_get(batch, "status")
    if status != STATUS_PREVIEWED:
        raise BatchNotReusable(BATCH_NOT_REUSABLE)
    if _is_expired(_row_get(batch, "expires_at"), now):
        raise BatchNotReusable(BATCH_NOT_REUSABLE)

    headers = _parse_headers(batch["headers_json"] if "headers_json" in batch.keys() else "[]")
    mapping = _parse_stored_mapping(
        batch["mapping_json"] if "mapping_json" in batch.keys() else "",
        headers,
    )
    mapping_updated_at = _row_get(batch, "mapping_updated_at")
    mapping_updated_by = _row_get_opt_int(batch, "mapping_updated_by_user_id")
    file_sha256 = _row_get(batch, "sha256")
    expires_at = _row_get(batch, "expires_at")
    try:
        source_type = normalize_source_type(_row_get(batch, "source_type"))
    except ValueError:
        source_type = DEFAULT_SOURCE_TYPE
    original_filename = _row_get(batch, "original_filename")
    client_name = ""
    client_row = conn.execute(
        "SELECT name FROM clients WHERE id = ? LIMIT 1",
        (int(client_id),),
    ).fetchone()
    if client_row is not None:
        client_name = _blank(client_row["name"] if "name" in client_row.keys() else client_row[0])

    staged_raw = conn.execute(
        """
        SELECT id, source_row_number, raw_json, has_blocking_error
        FROM crm_import_rows
        WHERE batch_id = ? AND client_id = ?
        ORDER BY source_row_number, id
        """,
        (batch_id, client_id),
    ).fetchall()
    if not staged_raw:
        raise BatchNotReusable(MAPPING_NO_ROWS)

    staged = [_build_staged_row(row, mapping) for row in staged_raw]
    from crm_identity_keys import require_company_identity_ready

    # Fail closed before any classify: incomplete keys would look like creates.
    require_company_identity_ready(conn)
    db_companies, identity_map = _load_matching_companies(
        conn,
        staged,
        client_id=int(client_id),
        source_type=source_type,
    )
    relationships = _load_relationships(conn, client_id)
    status_catalog = load_client_status_catalog(conn, client_id)
    resolutions = load_batch_status_resolutions(conn, client_id, batch_id)
    match_resolutions = load_batch_match_resolutions(conn, client_id, batch_id)
    staged_names = {row.person_norm for row in staged if row.person_norm}
    name_elsewhere = _load_name_elsewhere(conn, staged_names)

    proposed_companies: list[CompanyRec] = []
    decisions: list[RowDecision] = []
    for row in staged:
        match_res = match_resolutions.get(int(row.row_id))
        if row.has_blocking_error:
            decisions.append(_empty_decision(row, "blocking_error", BLOCKING_ERROR))
            continue
        if match_res is not None and match_res.resolution_type == RESOLUTION_SKIP_ROW:
            decision = _empty_decision(row, "ok", "")
            decision.company_name = row.company_name
            decision.company_action = EXCLUDED_SKIP
            decision.contact_action = EXCLUDED_SKIP
            decision.relationship_action = EXCLUDED_SKIP
            decision.status_action = EXCLUDED_SKIP
            decision.notes_action = EXCLUDED_SKIP
            _stamp_match_resolution(decision, match_res)
            decision.mapped = dict(row.mapped)
            decisions.append(decision)
            continue
        if not row.company_name:
            decisions.append(_empty_decision(row, "invalid_mapping_data", BLANK_COMPANY_NAME))
            continue
        if row.state_invalid:
            decisions.append(
                _empty_decision(row, "invalid_mapping_data", INVALID_STATE_DETAIL)
            )
            continue
        if row.date_invalid:
            decisions.append(
                _empty_decision(row, "invalid_mapping_data", INVALID_DATE_DETAIL)
            )
            continue
        if exclude_closed and is_closed_status(row.relationship_status):
            decision = _empty_decision(row, "ok", "")
            decision.company_name = row.company_name
            decision.company_action = EXCLUDED_CLOSED
            decision.contact_action = EXCLUDED_CLOSED
            decision.relationship_action = EXCLUDED_CLOSED
            decision.status_action = EXCLUDED_CLOSED
            decision.notes_action = EXCLUDED_CLOSED
            decision.mapped = dict(row.mapped)
            decisions.append(decision)
            continue
        query = _query_company(row.mapped)
        qrn = normalize_record_no(query.record_no)
        bound_id = identity_map.get(qrn) if qrn else None
        identity_bound = bound_id is not None
        action, matched, reasons, extras = _match_company(
            query,
            db_companies,
            proposed_companies,
            record_no_exclusive=record_no_exclusive,
        )
        if identity_bound:
            ident_cand = next(
                (c for c in db_companies if int(c.company_id or 0) == int(bound_id)),
                None,
            )
            if ident_cand is None:
                ident_cand = CompanyRec(
                    company_id=int(bound_id),
                    proposed_key=None,
                    name="",
                    norm_name="",
                    domain="",
                    phone="",
                    addr="",
                    city="",
                    state="",
                    record_no=qrn,
                )
            action = "use_existing_company"
            matched = ident_cand
            reasons = ["source_identity_exact"]
            extras = [(ident_cand, list(reasons)), *extras]
        decision = _empty_decision(row, "ok", "")
        decision.company_name = row.company_name
        decision.company_reasons = list(reasons)
        decision.company_possibles = _pack_company_possibles(extras)
        decision.identity_bound = identity_bound
        decision.can_create_company = not identity_bound

        resolved_company = False
        if match_res is not None:
            if match_res.resolution_type == RESOLUTION_USE_EXISTING_COMPANY:
                cid = int(match_res.company_id or 0)
                if identity_bound and cid != int(bound_id):
                    decision.company_action = "possible_company_match"
                    decision.contact_action = "deferred"
                    decision.relationship_action = "deferred"
                    decision.company_reasons = [
                        "source_identity_bound",
                        IDENTITY_BOUND_DETAIL,
                    ]
                    decision.can_create_company = False
                    _stamp_match_resolution(decision, match_res)
                    decisions.append(decision)
                    continue
                possible_ids = {p.company_id for p in decision.company_possibles}
                in_db = any(
                    c.company_id == cid for c in db_companies if c.company_id is not None
                )
                allowed = cid > 0 and (
                    (possible_ids and cid in possible_ids)
                    or (not possible_ids and in_db)
                    or (action != "possible_company_match" and in_db)
                    or bool(
                        conn.execute(
                            "SELECT 1 FROM companies WHERE id = ? LIMIT 1",
                            (cid,),
                        ).fetchone()
                    )
                )
                if not allowed:
                    decision.company_action = "possible_company_match"
                    decision.contact_action = "deferred"
                    decision.relationship_action = "deferred"
                    _stamp_match_resolution(decision, match_res)
                    decisions.append(decision)
                    continue
                decision.company_action = "use_existing_company"
                decision.company_id = cid
                decision.company_proposed_key = None
                resolved_company = True
            elif match_res.resolution_type == RESOLUTION_CREATE_COMPANY:
                if identity_bound:
                    decision.company_action = "possible_company_match"
                    decision.contact_action = "deferred"
                    decision.relationship_action = "deferred"
                    decision.company_reasons = [
                        "source_identity_bound",
                        IDENTITY_BOUND_DETAIL,
                    ]
                    decision.can_create_company = False
                    _stamp_match_resolution(decision, match_res)
                    decisions.append(decision)
                    continue
                key = _proposed_key("company", row.source_row_number, row.row_id)
                created = CompanyRec(
                    company_id=None,
                    proposed_key=key,
                    name=row.company_name,
                    norm_name=query.norm_name,
                    domain=query.domain,
                    phone=query.phone,
                    addr=query.addr,
                    city=query.city,
                    state=query.state,
                    record_no=query.record_no,
                    source_row=row.source_row_number,
                )
                proposed_companies.append(created)
                decision.company_action = "create_company"
                decision.company_proposed_key = key
                decision.company_created_at = row.source_row_number
                decision.company_id = None
                resolved_company = True
            elif match_res.resolution_type == RESOLUTION_USE_PROPOSED_COMPANY:
                if identity_bound:
                    decision.company_action = "possible_company_match"
                    decision.contact_action = "deferred"
                    decision.relationship_action = "deferred"
                    decision.can_create_company = False
                    _stamp_match_resolution(decision, match_res)
                    decisions.append(decision)
                    continue
                key = _blank(match_res.company_proposed_key)
                prior = next((c for c in proposed_companies if c.proposed_key == key), None)
                if not key or prior is None:
                    decision.company_action = "possible_company_match"
                    decision.contact_action = "deferred"
                    decision.relationship_action = "deferred"
                    _stamp_match_resolution(decision, match_res)
                    decisions.append(decision)
                    continue
                decision.company_action = "use_existing_company"
                decision.company_id = None
                decision.company_proposed_key = key
                decision.company_created_at = prior.source_row
                resolved_company = True
            if resolved_company:
                _stamp_match_resolution(decision, match_res)

        if not resolved_company:
            if action == "possible_company_match":
                decision.company_action = "possible_company_match"
                decision.contact_action = "deferred"
                decision.relationship_action = "deferred"
                if match_res is not None:
                    _stamp_match_resolution(decision, match_res)
                decisions.append(decision)
                continue
            if action == "use_existing_company" and matched is not None:
                decision.company_action = "use_existing_company"
                decision.company_id = matched.company_id
                decision.company_proposed_key = matched.proposed_key
                decision.company_created_at = matched.source_row
            else:
                if identity_bound:
                    decision.company_action = "possible_company_match"
                    decision.contact_action = "deferred"
                    decision.relationship_action = "deferred"
                    decision.can_create_company = False
                    decisions.append(decision)
                    continue
                key = _proposed_key("company", row.source_row_number, row.row_id)
                created = CompanyRec(
                    company_id=None,
                    proposed_key=key,
                    name=row.company_name,
                    norm_name=query.norm_name,
                    domain=query.domain,
                    phone=query.phone,
                    addr=query.addr,
                    city=query.city,
                    state=query.state,
                    record_no=query.record_no,
                    source_row=row.source_row_number,
                )
                proposed_companies.append(created)
                decision.company_action = "create_company"
                decision.company_proposed_key = key
                decision.company_created_at = row.source_row_number
            if match_res is not None:
                _stamp_match_resolution(decision, match_res)
        decisions.append(decision)

    needed_company_ids = sorted(
        {
            int(d.company_id)
            for d in decisions
            if d.company_action == "use_existing_company" and d.company_id is not None
        }
    )
    contacts_by_company, phone_keys = _load_contacts_for_companies(conn, needed_company_ids)

    proposed_contacts: list[ContactRec] = []
    proposed_relationships: dict[str, ProposedRelationshipState] = {}

    for staged_row, decision in zip(staged, decisions):
        if (
            decision.validity != "ok"
            or decision.company_action
            in {"possible_company_match", *EXCLUDED_ACTIONS}
        ):
            continue
        company_key = _company_ref(decision.company_id, decision.company_proposed_key)
        if not company_key:
            continue
        match_res = match_resolutions.get(int(staged_row.row_id))
        _plan_contact(
            staged_row,
            decision,
            company_key=company_key,
            db_contacts=contacts_by_company.get(int(decision.company_id), [])
            if decision.company_id is not None
            else [],
            phone_keys=phone_keys,
            proposed_contacts=proposed_contacts,
            name_elsewhere=name_elsewhere,
            match_resolution=match_res,
        )
        _plan_relationship(
            staged_row,
            decision,
            company_key=company_key,
            db_relationships=relationships,
            proposed_relationships=proposed_relationships,
            status_catalog=status_catalog,
            resolutions=resolutions,
            use_imported_for_existing=use_imported_for_existing,
        )

    fingerprint = hashlib.sha256(
        _canonical_json(
            _fingerprint_payload(
                client_id=client_id,
                batch_id=batch_id,
                file_sha256=file_sha256,
                mapping=mapping,
                mapping_updated_at=mapping_updated_at,
                mapping_updated_by_user_id=mapping_updated_by,
                status=status,
                expires_at=expires_at,
                staged=staged,
                decisions=decisions,
                use_imported_status_for_existing=use_imported_for_existing,
                source_type=source_type,
            )
        ).encode("utf-8")
    ).hexdigest()

    return ImportPlan(
        batch_id=int(batch_id),
        client_id=int(client_id),
        planner_version=PLANNER_VERSION,
        plan_fingerprint=fingerprint,
        mapping_updated_at=mapping_updated_at,
        total_rows=len(decisions),
        counts=_counts_from_rows(decisions),
        rows=decisions,
        stats={
            "retained_companies": len(db_companies),
            "retained_contacts": sum(len(v) for v in contacts_by_company.values()),
            "proposed_companies": len(proposed_companies),
            "proposed_contacts": len(proposed_contacts),
        },
        status_catalog=catalog_labels(status_catalog),
        use_imported_status_for_existing=use_imported_for_existing,
        source_type=source_type,
        original_filename=original_filename,
        mapping=dict(mapping),
        client_name=client_name,
    )


def _stamp_match_resolution(decision: RowDecision, resolution: MatchResolution) -> None:
    decision.match_resolution_type = resolution.resolution_type
    decision.match_resolution_updated_at = resolution.updated_at
    decision.match_resolution_updated_by_user_id = resolution.updated_by_user_id


def _plan_contact(
    staged: StagedPlanRow,
    decision: RowDecision,
    *,
    company_key: str,
    db_contacts: list[ContactRec],
    phone_keys: dict[int, list[tuple[str, str]]],
    proposed_contacts: list[ContactRec],
    name_elsewhere: dict[str, set[int]],
    match_resolution: MatchResolution | None = None,
) -> None:
    if match_resolution is not None and match_resolution.resolution_type == RESOLUTION_IMPORT_COMPANY_ONLY:
        decision.contact_action = "no_contact_data"
        decision.contact_id = None
        decision.contact_proposed_key = None
        decision.contact_possibles = []
        decision.contact_reasons = ["import_company_only"]
        _stamp_match_resolution(decision, match_resolution)
        return

    if match_resolution is not None and match_resolution.resolution_type == RESOLUTION_CREATE_CONTACT:
        if not staged.has_any_contact_field or not staged.can_create_contact:
            decision.contact_action = INSUFFICIENT_CONTACT_DATA
            decision.contact_reasons = [CONTACT_NEEDS_NAME_OR_EMAIL]
            _stamp_match_resolution(decision, match_resolution)
            return
        key = _proposed_key("contact", staged.source_row_number, staged.row_id)
        created = ContactRec(
            contact_id=None,
            proposed_key=key,
            company_id=decision.company_id,
            company_key=company_key,
            display_name=staged.contact_full or staged.email_norm,
            norm_name=staged.person_norm,
            email=staged.email_norm,
            nanp10=staged.nanp10,
            last7="",
            source_row=staged.source_row_number,
        )
        proposed_contacts.append(created)
        decision.contact_action = "create_contact"
        decision.contact_proposed_key = key
        decision.contact_created_at = staged.source_row_number
        decision.contact_name = created.display_name
        decision.contact_reasons = ["match_resolution"]
        _stamp_match_resolution(decision, match_resolution)
        return

    if (
        match_resolution is not None
        and match_resolution.resolution_type == RESOLUTION_USE_EXISTING_CONTACT
        and match_resolution.contact_id is not None
    ):
        chosen_id = int(match_resolution.contact_id)
        chosen = next((c for c in db_contacts if int(c.contact_id or 0) == chosen_id), None)
        if chosen is None:
            decision.contact_action = "possible_contact_match"
            decision.contact_reasons = ["unsafe_contact_move"]
            _stamp_match_resolution(decision, match_resolution)
            return
        decision.contact_action = "use_existing_contact"
        decision.contact_id = chosen_id
        decision.contact_name = chosen.display_name or staged.contact_full
        decision.contact_reasons = ["match_resolution"]
        _stamp_match_resolution(decision, match_resolution)
        return

    if not staged.has_any_contact_field:
        decision.contact_action = "no_contact_data"
        return

    proposed_here = [c for c in proposed_contacts if c.company_key == company_key]
    reasons: list[str] = []

    if staged.email_norm:
        for rec in (*db_contacts, *proposed_here):
            if rec.email and rec.email == staged.email_norm:
                decision.contact_action = "use_existing_contact"
                decision.contact_id = rec.contact_id
                decision.contact_proposed_key = rec.proposed_key
                decision.contact_created_at = rec.source_row
                decision.contact_name = rec.display_name or staged.contact_full
                decision.contact_reasons = ["email_exact"]
                if match_resolution is not None:
                    _stamp_match_resolution(decision, match_resolution)
                return

    if staged.nanp10:
        for rec in db_contacts:
            keys = phone_keys.get(int(rec.contact_id or 0), [(rec.nanp10, rec.last7)])
            if any(nanp == staged.nanp10 for nanp, _last in keys if nanp):
                decision.contact_action = "use_existing_contact"
                decision.contact_id = rec.contact_id
                decision.contact_reasons = ["phone_exact"]
                decision.contact_name = rec.display_name or staged.contact_full
                if match_resolution is not None:
                    _stamp_match_resolution(decision, match_resolution)
                return
        for rec in proposed_here:
            if rec.nanp10 and rec.nanp10 == staged.nanp10:
                decision.contact_action = "use_existing_contact"
                decision.contact_proposed_key = rec.proposed_key
                decision.contact_created_at = rec.source_row
                decision.contact_reasons = ["phone_exact"]
                decision.contact_name = rec.display_name or staged.contact_full
                if match_resolution is not None:
                    _stamp_match_resolution(decision, match_resolution)
                return

    possibles_map: dict[str, tuple[ContactRec, list[str]]] = {}

    def _add_possible(rec: ContactRec, reason: str) -> None:
        key = f"db:{rec.contact_id}" if rec.contact_id is not None else f"p:{rec.proposed_key}"
        prior = possibles_map.get(key)
        if prior is None:
            possibles_map[key] = (rec, [reason])
            return
        reasons = prior[1]
        if reason not in reasons:
            reasons.append(reason)

    if staged.last7 and not staged.nanp10:
        for rec in db_contacts:
            keys = phone_keys.get(int(rec.contact_id or 0), [(rec.nanp10, rec.last7)])
            if any(last == staged.last7 for _nanp, last in keys if last):
                _add_possible(rec, "phone_last7")
    elif staged.nanp10 and staged.last7:
        for rec in db_contacts:
            keys = phone_keys.get(int(rec.contact_id or 0), [(rec.nanp10, rec.last7)])
            if any(
                last == staged.last7 and nanp and nanp != staged.nanp10
                for nanp, last in keys
                if last
            ):
                _add_possible(rec, "phone_last7")

    if staged.person_norm:
        for rec in (*db_contacts, *proposed_here):
            if rec.norm_name and rec.norm_name == staged.person_norm:
                _add_possible(rec, "name_exact")

    possibles = list(possibles_map.values())

    if match_resolution is not None and match_resolution.contact_id is not None:
        chosen_id = int(match_resolution.contact_id)
        chosen = next((c for c in db_contacts if int(c.contact_id or 0) == chosen_id), None)
        if chosen is not None:
            decision.contact_action = "use_existing_contact"
            decision.contact_id = chosen_id
            decision.contact_name = chosen.display_name or staged.contact_full
            decision.contact_reasons = ["match_resolution"]
            _stamp_match_resolution(decision, match_resolution)
            return

    if possibles:
        db_poss = sorted(
            [(c, r) for c, r in possibles if c.contact_id is not None],
            key=lambda item: int(item[0].contact_id or 0),
        )
        proposed_poss = [item for item in possibles if item[0].contact_id is None]
        decision.contact_possibles = [
            PackedContactPossible(
                contact_id=int(c.contact_id or 0),
                display_name=c.display_name,
                reasons=list(r),
                email=c.email,
                phone=c.nanp10,
                company_id=c.company_id,
                company_name=decision.company_name,
            )
            for c, r in db_poss
        ]
        if (
            match_resolution is not None
            and match_resolution.resolution_type == RESOLUTION_USE_EXISTING_CONTACT
            and match_resolution.contact_id is not None
        ):
            chosen_id = int(match_resolution.contact_id)
            chosen = next((c for c, _r in db_poss if int(c.contact_id or 0) == chosen_id), None)
            if chosen is None:
                chosen = next(
                    (c for c in db_contacts if int(c.contact_id or 0) == chosen_id),
                    None,
                )
            if chosen is not None:
                decision.contact_action = "use_existing_contact"
                decision.contact_id = chosen_id
                decision.contact_name = chosen.display_name or staged.contact_full
                decision.contact_reasons = ["match_resolution"]
                _stamp_match_resolution(decision, match_resolution)
                return
        decision.contact_action = "possible_contact_match"
        if db_poss:
            decision.contact_reasons = list(db_poss[0][1])
            decision.contact_name = db_poss[0][0].display_name
        elif proposed_poss:
            decision.contact_reasons = list(proposed_poss[0][1])
            decision.contact_proposed_key = proposed_poss[0][0].proposed_key
            decision.contact_name = proposed_poss[0][0].display_name or staged.contact_full
        if _name_exists_elsewhere(staged.person_norm, decision.company_id, name_elsewhere):
            if "name_exists_elsewhere" not in decision.contact_reasons:
                decision.contact_reasons = [*decision.contact_reasons, "name_exists_elsewhere"]
        if match_resolution is not None:
            _stamp_match_resolution(decision, match_resolution)
        return

    if not staged.can_create_contact:
        if (
            match_resolution is not None
            and match_resolution.resolution_type == RESOLUTION_IMPORT_COMPANY_ONLY
        ):
            decision.contact_action = "no_contact_data"
            decision.contact_reasons = ["import_company_only"]
            _stamp_match_resolution(decision, match_resolution)
            return
        decision.contact_action = INSUFFICIENT_CONTACT_DATA
        decision.contact_reasons = [CONTACT_NEEDS_NAME_OR_EMAIL]
        decision.contact_name = staged.contact_full or staged.contact_title or staged.contact_phone
        if match_resolution is not None:
            _stamp_match_resolution(decision, match_resolution)
        return

    key = _proposed_key("contact", staged.source_row_number, staged.row_id)
    created = ContactRec(
        contact_id=None,
        proposed_key=key,
        company_id=decision.company_id,
        company_key=company_key,
        display_name=staged.contact_full or staged.email_norm,
        norm_name=staged.person_norm,
        email=staged.email_norm,
        nanp10=staged.nanp10,
        last7="",
        source_row=staged.source_row_number,
    )
    proposed_contacts.append(created)
    decision.contact_action = "create_contact"
    decision.contact_proposed_key = key
    decision.contact_created_at = staged.source_row_number
    decision.contact_name = created.display_name
    if _name_exists_elsewhere(staged.person_norm, decision.company_id, name_elsewhere):
        decision.contact_reasons = ["name_exists_elsewhere"]
    if match_resolution is not None:
        _stamp_match_resolution(decision, match_resolution)

def _plan_relationship(
    staged: StagedPlanRow,
    decision: RowDecision,
    *,
    company_key: str,
    db_relationships: dict[int, RelationshipRec],
    proposed_relationships: dict[str, ProposedRelationshipState],
    status_catalog,
    resolutions: dict[int, StatusResolution] | None = None,
    use_imported_for_existing: bool = False,
) -> None:
    if decision.validity != "ok":
        return

    relationship_is_new = True
    existing_status = ""
    existing_notes = ""
    if decision.company_id is not None and decision.company_id in db_relationships:
        existing = db_relationships[decision.company_id]
        decision.relationship_action = "relationship_already_exists"
        decision.relationship_id = existing.relationship_id
        relationship_is_new = False
        existing_status = existing.status
        existing_notes = existing.notes
    else:
        prior = proposed_relationships.get(company_key)
        if prior:
            decision.relationship_action = "relationship_already_exists"
            decision.relationship_proposed_key = prior.proposed_key
            relationship_is_new = False
            existing_status = prior.status
            existing_notes = prior.notes
        else:
            key = _proposed_key("relationship", staged.source_row_number, staged.row_id)
            decision.relationship_action = "create_client_relationship"
            decision.relationship_proposed_key = key
            relationship_is_new = True
            existing_status = ""
            existing_notes = ""

    decision.existing_status = _blank(existing_status)
    sn_plan = plan_status_and_notes(
        relationship_is_new=relationship_is_new,
        existing_status=existing_status,
        existing_notes=existing_notes,
        imported_status=staged.relationship_status,
        imported_notes=staged.relationship_notes,
        catalog=status_catalog,
        use_imported_for_existing=use_imported_for_existing,
    )
    resolution = (resolutions or {}).get(int(staged.row_id))
    status_action, resolved_status, original_action, resolution_type = apply_status_resolution(
        base_status_action=sn_plan.status_action,
        base_resolved_status=sn_plan.resolved_status,
        existing_status=existing_status,
        resolution=resolution,
        catalog=status_catalog,
    )
    if sn_plan.authoritative_existing_update and not resolution_type:
        original_action = STATUS_CONFLICT
        resolution_type = BATCH_RESOLUTION_USE_IMPORTED
    decision.status_action = status_action
    decision.notes_action = sn_plan.notes_action
    decision.resolved_status = resolved_status
    decision.planned_notes = sn_plan.planned_notes
    decision.original_status_action = original_action
    decision.status_resolution_type = resolution_type
    if resolution is not None and resolution_type:
        decision.status_resolution_updated_at = resolution.updated_at
        decision.status_resolution_updated_by_user_id = resolution.updated_by_user_id
    else:
        decision.status_resolution_updated_at = ""
        decision.status_resolution_updated_by_user_id = None

    if decision.relationship_action == "create_client_relationship":
        proposed_relationships[company_key] = ProposedRelationshipState(
            proposed_key=str(decision.relationship_proposed_key or ""),
            status=decision.resolved_status,
            notes=decision.planned_notes,
        )
    elif (
        decision.relationship_action == "relationship_already_exists"
        and decision.relationship_id is None
        and company_key in proposed_relationships
        and decision.notes_action == NOTES_APPEND
        and status_action
        not in {STATUS_CONFLICT, STATUS_INVALID}
    ):
        # Keep in-batch proposed notes current for later duplicate-company rows.
        proposed_relationships[company_key].notes = decision.planned_notes
        proposed_relationships[company_key].status = (
            decision.resolved_status or proposed_relationships[company_key].status
        )


def _row_to_api(row: RowDecision) -> CrmImportDryRunRow:
    return CrmImportDryRunRow(
        row_id=row.row_id,
        source_row_number=row.source_row_number,
        validity=row.validity,
        validity_detail=row.validity_detail,
        mapped=row.mapped,
        company=CrmImportDryRunCompanyPlan(
            action=row.company_action,
            reasons=list(row.company_reasons),
            company_id=row.company_id,
            proposed_key=row.company_proposed_key,
            created_at_source_row=row.company_created_at,
            name=row.company_name if row.company_action != "none" else "",
            possibles=[
                CrmImportDryRunCompanyPossible(
                    company_id=item.company_id,
                    company_name=item.name,
                    external_record_no=item.record_no,
                    address=item.address,
                    city=item.city,
                    state=item.state,
                    zip=item.zip,
                    phone=item.phone,
                    website=item.website,
                    identity_record_nos=list(item.identity_record_nos),
                    reasons=list(item.reasons),
                )
                for item in row.company_possibles[:5]
            ],
            can_create_company=bool(row.can_create_company)
            and row.company_action == "possible_company_match"
            and not row.identity_bound,
        ),
        contact=CrmImportDryRunContactPlan(
            action=row.contact_action,
            reasons=list(row.contact_reasons),
            contact_id=row.contact_id,
            proposed_key=row.contact_proposed_key,
            created_at_source_row=row.contact_created_at,
            display_name=row.contact_name if row.contact_action not in {"none", "no_contact_data"} else "",
            can_create_contact=row.contact_action == "possible_contact_match",
            possibles=[
                CrmImportDryRunContactPossible(
                    contact_id=item.contact_id,
                    display_name=item.display_name,
                    email=item.email,
                    phone=item.phone,
                    company_id=item.company_id,
                    company_name=item.company_name,
                    reasons=list(item.reasons),
                )
                for item in row.contact_possibles[:5]
            ],
        ),
        relationship=CrmImportDryRunRelationshipPlan(
            action=row.relationship_action,
            relationship_id=row.relationship_id,
            proposed_key=row.relationship_proposed_key,
            status_action=row.status_action if row.status_action != "none" else "none",
            notes_action=row.notes_action if row.notes_action != "none" else "none",
            resolved_status=row.resolved_status
            if row.status_action
            in {STATUS_USE_DEFAULT, STATUS_PRESERVE, STATUS_USE_IMPORTED}
            or bool(row.status_resolution_type)
            else "",
            original_status_action=row.original_status_action or row.status_action or "none",
            status_resolution_type=row.status_resolution_type or "",
            existing_status=row.existing_status or "",
            needs_status_resolution=row.status_action in {STATUS_CONFLICT, STATUS_INVALID},
        ),
    )


def _validation_summary(plan: ImportPlan) -> CrmImportValidationSummary:
    counts = plan.counts
    excluded = int(counts.get("excluded_skip") or 0) + int(counts.get("excluded_closed") or 0)
    invalid_rows = int(counts.get("blocking_error") or 0) + int(
        counts.get("invalid_mapping_data") or 0
    )
    unresolved_company = int(counts.get("possible_company_match") or 0)
    unresolved_contact = int(counts.get("possible_contact_match") or 0) + int(
        counts.get("insufficient_contact_data") or 0
    )
    status_conflicts = int(counts.get("status_conflict") or 0) + int(
        counts.get("invalid_status") or 0
    )
    confirm_blocked = (
        int(counts.get("needs_review_rows") or 0) != 0
        or int(counts.get("importable_rows") or 0) + excluded != int(plan.total_rows)
        or unresolved_company > 0
        or unresolved_contact > 0
        or status_conflicts > 0
        or invalid_rows > 0
    )
    mapping = plan.mapping or {}
    return CrmImportValidationSummary(
        client_id=int(plan.client_id),
        client_name=plan.client_name,
        source_type=plan.source_type,
        original_filename=plan.original_filename,
        source_row_count=int(plan.total_rows),
        mapped_company_name_field=_blank(mapping.get("company_name")),
        mapped_source_rn_field=_blank(mapping.get("external_record_no")),
        mapped_status_field=_blank(mapping.get("relationship_status")),
        mapped_notes_field=_blank(mapping.get("relationship_notes")),
        unresolved_company_matches=unresolved_company,
        unresolved_contact_matches=unresolved_contact,
        status_conflicts=status_conflicts,
        invalid_rows=invalid_rows,
        confirm_blocked=confirm_blocked,
    )


def _paginate(plan: ImportPlan, offset: int, limit: int) -> CrmImportDryRunResponse:
    sliced = plan.rows[offset : offset + limit]
    return CrmImportDryRunResponse(
        batch_id=plan.batch_id,
        client_id=plan.client_id,
        planner_version=plan.planner_version,
        plan_fingerprint=plan.plan_fingerprint,
        mapping_updated_at=plan.mapping_updated_at,
        total_rows=plan.total_rows,
        offset=offset,
        limit=limit,
        use_imported_status_for_existing=bool(plan.use_imported_status_for_existing),
        counts=CrmImportDryRunCounts(**plan.counts),
        rows=[_row_to_api(row) for row in sliced],
        status_catalog=list(plan.status_catalog or []),
        source_type=plan.source_type,
        original_filename=plan.original_filename,
        mapped_fields=dict(plan.mapping or {}),
        validation_summary=_validation_summary(plan),
    )


def dry_run_crm_import(
    client_id: int,
    batch_id: int,
    *,
    offset: int = 0,
    limit: int = MAX_DRY_RUN_PAGE,
    use_imported_status_for_existing: bool = False,
) -> CrmImportDryRunResponse:
    if offset < 0 or limit < 1 or limit > MAX_DRY_RUN_PAGE:
        raise ValueError(INVALID_PAGING)
    from db import get_connection

    with get_connection() as conn:
        conn.execute("PRAGMA query_only = ON")
        plan = plan_crm_import_batch(
            conn,
            client_id=client_id,
            batch_id=batch_id,
            use_imported_status_for_existing=use_imported_status_for_existing,
        )
    return _paginate(plan, offset, limit)
