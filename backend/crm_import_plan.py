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
    load_client_status_catalog,
    plan_status_and_notes,
)
from crm_import_status_resolution import (
    StatusResolution,
    apply_status_resolution,
    catalog_labels,
    load_batch_status_resolutions,
)
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from models import (
    CrmImportDryRunCompanyPlan,
    CrmImportDryRunCompanyPossible,
    CrmImportDryRunContactPlan,
    CrmImportDryRunContactPossible,
    CrmImportDryRunCounts,
    CrmImportDryRunRelationshipPlan,
    CrmImportDryRunResponse,
    CrmImportDryRunRow,
)

PLANNER_VERSION = "crm-import-plan-v4"
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
    company_possibles: list[tuple[int, str, str, list[str]]]
    contact_action: str
    contact_id: int | None
    contact_proposed_key: str | None
    contact_reasons: list[str]
    contact_name: str
    contact_created_at: int | None
    contact_possibles: list[tuple[int, str, list[str]]]
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
    existing_status: str
    mapped: dict[str, str]


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


def _company_score(reasons: list[str]) -> int:
    return (
        ("domain_exact" in reasons) * 100
        + ("name_exact" in reasons) * 40
        + ("phone" in reasons) * 20
        + ("address_city_state" in reasons) * 15
        + len(reasons)
    )


def _company_reasons(query: CompanyRec, cand: CompanyRec) -> list[str]:
    reasons: list[str] = []
    if query.domain and cand.domain and query.domain == cand.domain:
        reasons.append("domain_exact")
    if query.norm_name and cand.norm_name and query.norm_name == cand.norm_name:
        reasons.append("name_exact")
    if query.phone and cand.phone and len(query.phone) >= 7:
        cph = cand.phone
        bph = query.phone
        if cph == bph or cph.endswith(bph[-7:]) or bph.endswith(cph[-7:]):
            reasons.append("phone")
    if query.addr and query.city and query.state:
        if cand.addr == query.addr and cand.city == query.city and cand.state == query.state:
            reasons.append("address_city_state")
    return reasons


def _classify_company(
    hits: list[tuple[CompanyRec, list[str]]],
) -> tuple[str, CompanyRec | None, list[str], list[tuple[CompanyRec, list[str]]]]:
    high: list[tuple[CompanyRec, list[str]]] = []
    possible: list[tuple[CompanyRec, list[str]]] = []
    for cand, reasons in hits:
        if "domain_exact" in reasons:
            high.append((cand, reasons))
        elif "name_exact" in reasons and (
            "phone" in reasons or "address_city_state" in reasons or "domain_exact" in reasons
        ):
            high.append((cand, reasons))
        elif "name_exact" in reasons and len(reasons) == 1:
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
        record_no="",
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
        existing_status="",
        mapped=dict(staged.mapped),
    )


def _pack_company_possibles(
    items: list[tuple[CompanyRec, list[str]]],
) -> list[tuple[int, str, str, list[str]]]:
    packed: list[tuple[int, str, str, list[str]]] = []
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
        packed.append((cid, cand.name, cand.record_no, list(reasons)))
    return packed


def _match_company(
    query: CompanyRec,
    db_companies: list[CompanyRec],
    proposed: list[CompanyRec],
) -> tuple[str, CompanyRec | None, list[str], list[tuple[CompanyRec, list[str]]]]:
    hits: list[tuple[CompanyRec, list[str]]] = []
    for cand in (*db_companies, *proposed):
        reasons = _company_reasons(query, cand)
        if reasons:
            hits.append((cand, reasons))
    return _classify_company(hits)


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
        "no_notes_change": 0,
        "set_imported_notes": 0,
        "append_imported_notes": 0,
        "imported_notes_already_present": 0,
        "importable_rows": 0,
        "needs_review_rows": 0,
    }
    for row in rows:
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
                    {"company_id": cid, "reasons": list(reasons)}
                    for cid, _name, _rn, reasons in d.company_possibles
                ],
                "contact_action": d.contact_action,
                "contact_id": d.contact_id,
                "contact_proposed_key": d.contact_proposed_key,
                "contact_reasons": list(d.contact_reasons),
                "contact_possible": [
                    {"contact_id": cid, "reasons": list(reasons)}
                    for cid, _name, reasons in d.contact_possibles
                ],
                "relationship_action": d.relationship_action,
                "relationship_id": d.relationship_id,
                "relationship_proposed_key": d.relationship_proposed_key,
                "status_action": d.status_action,
                "notes_action": d.notes_action,
                "resolved_status": d.resolved_status,
                "original_status_action": d.original_status_action,
                "status_resolution_type": d.status_resolution_type,
                "status_resolution_updated_at": d.status_resolution_updated_at,
                "status_resolution_updated_by_user_id": d.status_resolution_updated_by_user_id,
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


def _load_matching_companies(conn, staged: list[StagedPlanRow]) -> list[CompanyRec]:
    domains = {r.company_domain for r in staged if r.company_domain}
    names = {r.norm_company for r in staged if r.norm_company}
    phones = {r.company_phone_digits for r in staged if r.company_phone_digits}
    last7 = {p[-7:] for p in phones if len(p) >= 7}
    addr_keys = {
        (r.company_addr, _blank(r.city).lower(), state_for_match(r.state))
        for r in staged
        if r.company_addr and _blank(r.city) and _blank(r.state) and not r.state_invalid
    }
    kept: list[CompanyRec] = []
    if not (domains or names or last7 or addr_keys):
        return kept
    for raw in conn.execute(
        """
        SELECT id, company_name, external_record_no, website, address, city, state, zip,
               legacy_phone
        FROM companies
        """
    ):
        name = _blank(raw["company_name"])
        rec = CompanyRec(
            company_id=int(raw["id"]),
            proposed_key=None,
            name=name,
            norm_name=norm_name(name) if name else "",
            domain=domain(_blank(raw["website"])),
            phone=digits_phone(_blank(raw["legacy_phone"])),
            addr=norm_addr(_blank(raw["address"])) if _blank(raw["address"]) else "",
            city=_blank(raw["city"]).lower(),
            state=state_for_match(raw["state"]),
            record_no=_blank(raw["external_record_no"]),
        )
        phone_hit = bool(rec.phone) and (
            rec.phone in phones
            or (len(rec.phone) >= 7 and rec.phone[-7:] in last7)
        )
        addr_hit = bool(rec.addr and rec.city and rec.state) and (
            rec.addr,
            rec.city,
            rec.state,
        ) in addr_keys
        if (
            (rec.domain and rec.domain in domains)
            or (rec.norm_name and rec.norm_name in names)
            or phone_hit
            or addr_hit
        ):
            kept.append(rec)
    return kept


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


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


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
    found: dict[str, set[int]] = {name: set() for name in staged_names if name}
    if not found:
        return found
    for raw in conn.execute("SELECT company_id, first_name, last_name FROM contacts"):
        display = f"{_blank(raw['first_name'])} {_blank(raw['last_name'])}".strip()
        key = _norm_person_name(display)
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


def plan_crm_import_batch(conn, *, client_id: int, batch_id: int) -> ImportPlan:
    """Read-only full-batch plan. Caller supplies the connection.

    Does not set PRAGMA query_only. The HTTP dry-run wrapper enables that
    guard. A future confirm may call this inside BEGIN IMMEDIATE and then write.
    """
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
    db_companies = _load_matching_companies(conn, staged)
    relationships = _load_relationships(conn, client_id)
    status_catalog = load_client_status_catalog(conn, client_id)
    resolutions = load_batch_status_resolutions(conn, client_id, batch_id)
    staged_names = {row.person_norm for row in staged if row.person_norm}
    name_elsewhere = _load_name_elsewhere(conn, staged_names)

    proposed_companies: list[CompanyRec] = []
    decisions: list[RowDecision] = []
    for row in staged:
        if row.has_blocking_error:
            decisions.append(_empty_decision(row, "blocking_error", BLOCKING_ERROR))
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
        query = _query_company(row.mapped)
        action, matched, reasons, extras = _match_company(
            query, db_companies, proposed_companies
        )
        decision = _empty_decision(row, "ok", "")
        decision.company_name = row.company_name
        decision.company_reasons = list(reasons)
        if action == "possible_company_match":
            decision.company_action = "possible_company_match"
            decision.company_possibles = _pack_company_possibles(extras)
            decision.contact_action = "deferred"
            decision.relationship_action = "deferred"
            decisions.append(decision)
            continue
        if action == "use_existing_company" and matched is not None:
            decision.company_action = "use_existing_company"
            decision.company_id = matched.company_id
            decision.company_proposed_key = matched.proposed_key
            decision.company_created_at = matched.source_row
            decision.company_possibles = _pack_company_possibles(extras)
        else:
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
                record_no="",
                source_row=row.source_row_number,
            )
            proposed_companies.append(created)
            decision.company_action = "create_company"
            decision.company_proposed_key = key
            decision.company_created_at = row.source_row_number
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
        if decision.validity != "ok" or decision.company_action == "possible_company_match":
            continue
        company_key = _company_ref(decision.company_id, decision.company_proposed_key)
        if not company_key:
            continue
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
        )
        _plan_relationship(
            staged_row,
            decision,
            company_key=company_key,
            db_relationships=relationships,
            proposed_relationships=proposed_relationships,
            status_catalog=status_catalog,
            resolutions=resolutions,
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
    )


def _plan_contact(
    staged: StagedPlanRow,
    decision: RowDecision,
    *,
    company_key: str,
    db_contacts: list[ContactRec],
    phone_keys: dict[int, list[tuple[str, str]]],
    proposed_contacts: list[ContactRec],
    name_elsewhere: dict[str, set[int]],
) -> None:
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
                return

    if staged.nanp10:
        for rec in db_contacts:
            keys = phone_keys.get(int(rec.contact_id or 0), [(rec.nanp10, rec.last7)])
            if any(nanp == staged.nanp10 for nanp, _last in keys if nanp):
                decision.contact_action = "use_existing_contact"
                decision.contact_id = rec.contact_id
                decision.contact_reasons = ["phone_exact"]
                decision.contact_name = rec.display_name or staged.contact_full
                return
        for rec in proposed_here:
            if rec.nanp10 and rec.nanp10 == staged.nanp10:
                decision.contact_action = "use_existing_contact"
                decision.contact_proposed_key = rec.proposed_key
                decision.contact_created_at = rec.source_row
                decision.contact_reasons = ["phone_exact"]
                decision.contact_name = rec.display_name or staged.contact_full
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

    if possibles:
        db_poss = sorted(
            [(c, r) for c, r in possibles if c.contact_id is not None],
            key=lambda item: int(item[0].contact_id or 0),
        )
        proposed_poss = [item for item in possibles if item[0].contact_id is None]
        decision.contact_action = "possible_contact_match"
        decision.contact_possibles = [
            (int(c.contact_id or 0), c.display_name, list(r)) for c, r in db_poss
        ]
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
        return

    if not staged.can_create_contact:
        decision.contact_action = INSUFFICIENT_CONTACT_DATA
        decision.contact_reasons = [CONTACT_NEEDS_NAME_OR_EMAIL]
        decision.contact_name = staged.contact_full or staged.contact_title or staged.contact_phone
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


def _plan_relationship(
    staged: StagedPlanRow,
    decision: RowDecision,
    *,
    company_key: str,
    db_relationships: dict[int, RelationshipRec],
    proposed_relationships: dict[str, ProposedRelationshipState],
    status_catalog,
    resolutions: dict[int, StatusResolution] | None = None,
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
    )
    resolution = (resolutions or {}).get(int(staged.row_id))
    status_action, resolved_status, original_action, resolution_type = apply_status_resolution(
        base_status_action=sn_plan.status_action,
        base_resolved_status=sn_plan.resolved_status,
        existing_status=existing_status,
        resolution=resolution,
        catalog=status_catalog,
    )
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
                    company_id=cid,
                    company_name=name,
                    external_record_no=record_no,
                    reasons=list(reasons),
                )
                for cid, name, record_no, reasons in row.company_possibles[:5]
            ],
        ),
        contact=CrmImportDryRunContactPlan(
            action=row.contact_action,
            reasons=list(row.contact_reasons),
            contact_id=row.contact_id,
            proposed_key=row.contact_proposed_key,
            created_at_source_row=row.contact_created_at,
            display_name=row.contact_name if row.contact_action not in {"none", "no_contact_data"} else "",
            possibles=[
                CrmImportDryRunContactPossible(
                    contact_id=cid,
                    display_name=name,
                    reasons=list(reasons),
                )
                for cid, name, reasons in row.contact_possibles[:5]
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
        counts=CrmImportDryRunCounts(**plan.counts),
        rows=[_row_to_api(row) for row in sliced],
        status_catalog=list(plan.status_catalog or []),
    )


def dry_run_crm_import(
    client_id: int,
    batch_id: int,
    *,
    offset: int = 0,
    limit: int = MAX_DRY_RUN_PAGE,
) -> CrmImportDryRunResponse:
    if offset < 0 or limit < 1 or limit > MAX_DRY_RUN_PAGE:
        raise ValueError(INVALID_PAGING)
    from db import get_connection

    with get_connection() as conn:
        conn.execute("PRAGMA query_only = ON")
        plan = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_id)
    return _paginate(plan, offset, limit)
