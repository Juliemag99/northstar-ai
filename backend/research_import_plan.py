"""Research Prospect Import planner. Non-destructive. Reuses canonical matching."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from crm_import_plan import CompanyRec, _attach_company_aliases, _match_company, _query_company
from crm_import_state import state_for_match
from crm_import_status_notes import DEFAULT_RELATIONSHIP_STATUS
from data_steward import is_manual_authority, provenance_evidence
from db import PRODUCTION_DB_PATH
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from research_import_contacts import classify_research_contact, note_already_present
from research_import_mapping import (
    apply_mapping,
    blank,
    contact_from_mapped,
    custom_attribute_maps,
    ignored_headers,
    normalize_mapping,
    parse_priority,
    parse_typed_value,
    split_multi,
    suggest_mapping,
    validate_mapping,
)
from research_import_policy import master_actions_for
from research_import_schema import ensure_research_import_schema, is_production_db

PLANNER_VERSION = "ri4b.1"
CLASS_EXACT = "EXACT_EXISTING"
CLASS_STRONG = "STRONG_EXISTING"
CLASS_NEW_LOCATION = "EXISTING_COMPANY_NEW_LOCATION"
CLASS_NEW = "NEW_COMPANY"
CLASS_POSSIBLE = "POSSIBLE_MATCH_REVIEW"
CLASS_AMBIGUOUS = "AMBIGUOUS"
CLASS_INVALID = "INVALID_INSUFFICIENT"

RES_USE_EXISTING = "USE_EXISTING"
RES_CREATE_NEW = "CREATE_NEW"
RES_NEW_LOCATION = "TREAT_AS_NEW_LOCATION"
RES_SKIP = "SKIP"

CONFLICT_SAME = "SAME"
CONFLICT_FILL = "FILL_BLANK"
CONFLICT_UPDATE = "PROPOSE_UPDATE"
CONFLICT_MANUAL = "MANUAL_AUTHORITY_CONFLICT"
CONFLICT_NEW_LOC = "POSSIBLE_NEW_LOCATION"
CONFLICT_REVIEW = "REVIEW"
SAME_BATCH_RESEARCH_CONFLICT = "SAME_BATCH_RESEARCH_CONFLICT"
SAME_BATCH_MASTER_CONFLICT = "SAME_BATCH_MASTER_CONFLICT"
SAME_BATCH_ATTRIBUTE_CONFLICT = "SAME_BATCH_ATTRIBUTE_CONFLICT"

MULTI_VALUE_ATTRIBUTES = {
    "target_market",
    "equipment_product",
    "potential_component",
    "potential_components",
    "target_department",
}
MASTER_SCALAR_FIELDS = ("website", "phone", "address", "city", "state", "zip")
_MASTER_CLASS_RANK = {
    CONFLICT_MANUAL: 0,
    CONFLICT_UPDATE: 1,
    CONFLICT_FILL: 2,
    CONFLICT_NEW_LOC: 3,
    CONFLICT_REVIEW: 4,
    CONFLICT_SAME: 5,
}

MASTER_ACCEPT = "ACCEPT"
MASTER_REJECT = "REJECT"
MASTER_DEFER = "DEFER"

STATUS_PREVIEWED = "previewed"
PRODUCTION_CONFIRM_DISABLED = (
    "Research Prospect Import confirm is disabled on the live database."
)
ISOLATED_CONFIRM_DISABLED = (
    "Research Prospect Import confirm is disabled unless "
    "NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM=1 on an isolated database."
)
STALE_FINGERPRINT = "This preview is stale. Recalculate before confirming."
CLIENT_REQUIRED = "Choose an existing NorthStar client before uploading."
CLIENT_MISSING = "Research import requires an existing NorthStar client."
CONFIRM_BLOCKED = "Confirm is blocked until review rows and conflicts are resolved."
ALLOW_CONFIRM_ENV = "NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM"

_ORDINALS = {
    "first": "1",
    "1st": "1",
    "second": "2",
    "2nd": "2",
    "third": "3",
    "3rd": "3",
    "fourth": "4",
    "4th": "4",
    "fifth": "5",
    "5th": "5",
    "sixth": "6",
    "6th": "6",
    "seventh": "7",
    "7th": "7",
    "eighth": "8",
    "8th": "8",
    "ninth": "9",
    "9th": "9",
    "tenth": "10",
    "10th": "10",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_production_path() -> bool:
    from db import DB_PATH as LIVE_RUNTIME_PATH

    try:
        return Path(os.fspath(LIVE_RUNTIME_PATH)).resolve() == Path(PRODUCTION_DB_PATH).resolve()
    except OSError:
        return False


def confirm_enabled(conn) -> bool:
    if is_production_db(conn) or is_production_path():
        return False
    return os.environ.get(ALLOW_CONFIRM_ENV, "").strip() == "1"


def looks_url(value: str) -> bool:
    text = blank(value)
    if not re.match(r"^https?://", text, re.I):
        return False
    parsed = urlparse(text)
    return bool(parsed.scheme and parsed.netloc and "." in parsed.netloc)


def url_domain(value: str) -> str:
    text = blank(value)
    try:
        if looks_url(text):
            host = urlparse(text).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        return domain(text)
    except Exception:
        return domain(text)


def zip5(value: object | None) -> str:
    digits = re.sub(r"\D", "", blank(value))
    return digits[:5] if len(digits) >= 5 else digits


def street_key(value: object | None) -> str:
    text = norm_addr(blank(value)) if blank(value) else ""
    tokens = []
    for token in text.split():
        tokens.append(_ORDINALS.get(token, token))
    return " ".join(tokens)


def location_tuple(addr: str, city: str, state: str, postal: str) -> tuple[str, str, str, str]:
    return (
        street_key(addr),
        blank(city).lower(),
        state_for_match(state) or "",
        zip5(postal),
    )


def values_same_website(left: str, right: str) -> bool:
    da, db = domain(left), domain(right)
    if da and db:
        return da == db
    return blank(left).lower().rstrip("/") == blank(right).lower().rstrip("/")


def values_same_phone(left: str, right: str) -> bool:
    da, db = digits_phone(left), digits_phone(right)
    if not da or not db:
        return not da and not db
    return da == db or (len(da) >= 7 and len(db) >= 7 and (da.endswith(db[-7:]) or db.endswith(da[-7:])))


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def default_ccr_status(conn, client_id: int) -> str:
    if _table_exists(conn, "client_status_catalog"):
        row = conn.execute(
            """
            SELECT status_label FROM client_status_catalog
            WHERE client_id=? AND is_default=1 AND active=1
              AND TRIM(status_label) != ''
            ORDER BY sort_order, id LIMIT 1
            """,
            (int(client_id),),
        ).fetchone()
        if row is not None:
            return blank(row["status_label"] if "status_label" in row.keys() else row[0])
    return DEFAULT_RELATIONSHIP_STATUS


def load_company_recs(conn) -> list[CompanyRec]:
    has_archived = any(r[1] == "archived_at" for r in conn.execute("PRAGMA table_info(companies)"))
    archived_select = (
        "TRIM(COALESCE(c.archived_at, '')) AS archived_at" if has_archived else "'' AS archived_at"
    )
    sql = f"""
        SELECT
            c.id, c.company_name, c.address, c.city, c.state, c.zip, c.website, c.legacy_phone,
            COALESCE(k.record_no, '') AS record_no,
            COALESCE(k.domain, '') AS domain,
            COALESCE(k.norm_name, '') AS norm_name,
            COALESCE(k.phone_digits, '') AS phone_digits,
            COALESCE(k.addr_norm, '') AS addr_norm,
            COALESCE(k.city_norm, '') AS city_norm,
            COALESCE(k.state_norm, '') AS state_norm,
            {archived_select}
        FROM companies c
        LEFT JOIN company_identity_keys k ON k.company_id = c.id
    """
    kept: list[CompanyRec] = []
    for raw in conn.execute(sql):
        kept.append(
            CompanyRec(
                company_id=int(raw["id"]),
                proposed_key=None,
                name=blank(raw["company_name"]),
                norm_name=blank(raw["norm_name"])
                or (norm_name(blank(raw["company_name"])) if blank(raw["company_name"]) else ""),
                domain=blank(raw["domain"]) or domain(blank(raw["website"])),
                phone=blank(raw["phone_digits"]) or digits_phone(blank(raw["legacy_phone"])),
                addr=blank(raw["addr_norm"])
                or (norm_addr(blank(raw["address"])) if blank(raw["address"]) else ""),
                city=blank(raw["city_norm"]) or blank(raw["city"]).lower(),
                state=blank(raw["state_norm"]) or (state_for_match(raw["state"]) or ""),
                record_no=blank(raw["record_no"]),
                display_address=blank(raw["address"]),
                display_city=blank(raw["city"]),
                display_state=blank(raw["state"]),
                display_zip=blank(raw["zip"]),
                display_phone=blank(raw["legacy_phone"]),
                display_website=blank(raw["website"]),
                archived=bool(blank(raw["archived_at"])),
            )
        )
    _attach_company_aliases(conn, kept)
    return kept


def load_locations(conn) -> dict[int, list[dict[str, str]]]:
    out: dict[int, list[dict[str, str]]] = {}
    if not _table_exists(conn, "company_locations"):
        return out
    archived = any(r[1] == "archived_at" for r in conn.execute("PRAGMA table_info(company_locations)"))
    sql = "SELECT id, company_id, address, city, state, zip FROM company_locations"
    if archived:
        sql += " WHERE TRIM(COALESCE(archived_at,'')) = ''"
    for raw in conn.execute(sql):
        out.setdefault(int(raw["company_id"]), []).append(
            {
                "id": str(int(raw["id"])),
                "address": blank(raw["address"]),
                "city": blank(raw["city"]),
                "state": blank(raw["state"]),
                "zip": blank(raw["zip"]),
            }
        )
    return out


def load_ccrs(conn) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for raw in conn.execute(
        """
        SELECT ccr.id, ccr.client_id, ccr.company_id, ccr.status, ccr.notes,
               ccr.assigned_user_id, ccr.priority, ccr.next_action, ccr.follow_up_date,
               cl.code
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        """
    ):
        out.setdefault(int(raw["company_id"]), []).append(
            {
                "ccr_id": int(raw["id"]),
                "client_id": int(raw["client_id"]),
                "status": blank(raw["status"]),
                "notes": "" if raw["notes"] is None else str(raw["notes"]),
                "assigned_user_id": raw["assigned_user_id"],
                "priority": blank(raw["priority"]),
                "next_action": blank(raw["next_action"]),
                "follow_up_date": blank(raw["follow_up_date"]),
                "client_code": blank(raw["code"]),
            }
        )
    return out


def location_compare(query_mapped: dict[str, str], matched: CompanyRec, locs: list[dict[str, str]]) -> dict[str, Any]:
    incoming = location_tuple(
        query_mapped.get("address", ""),
        query_mapped.get("city", ""),
        query_mapped.get("state", ""),
        query_mapped.get("zip", ""),
    )
    master = location_tuple(
        matched.display_address, matched.display_city, matched.display_state, matched.display_zip
    )
    loc_hit = None
    for loc in locs:
        loc_t = location_tuple(loc["address"], loc["city"], loc["state"], loc["zip"])
        if incoming[0] and incoming[1] and incoming[0] == loc_t[0] and incoming[1] == loc_t[1]:
            if not incoming[2] or not loc_t[2] or incoming[2] == loc_t[2]:
                loc_hit = loc
                break
    street_city = bool(incoming[0] and incoming[1] and incoming[0] == master[0] and incoming[1] == master[1])
    state_ok = (not incoming[2] or not master[2] or incoming[2] == master[2])
    zip_ok = (not incoming[3] or not master[3] or incoming[3] == master[3])
    same_master = street_city and state_ok and zip_ok
    city_state_same = bool(incoming[1] and incoming[2] and incoming[1] == master[1] and incoming[2] == master[2])
    different_city_or_state = bool(
        incoming[1]
        and master[1]
        and (
            incoming[1] != master[1]
            or (incoming[2] and master[2] and incoming[2] != master[2])
        )
    )
    return {
        "same_master_address": same_master or bool(loc_hit),
        "matches_existing_location_id": int(loc_hit["id"]) if loc_hit else None,
        "same_city_state": city_state_same,
        "address_differs": bool(different_city_or_state and not loc_hit and not same_master),
        "street_city_same": street_city,
    }


def conflict_class(incoming: str, current: str, field: str, manual: bool) -> str:
    inc, cur = blank(incoming), blank(current)
    if not inc:
        return CONFLICT_SAME
    if not cur:
        return CONFLICT_MANUAL if manual else CONFLICT_FILL
    same = False
    if field == "website":
        same = values_same_website(inc, cur)
    elif field == "phone":
        same = values_same_phone(inc, cur)
    elif field == "address":
        same = location_tuple(inc, "", "", "")[0] == location_tuple(cur, "", "", "")[0] and inc.casefold() == cur.casefold()
    else:
        same = inc.casefold() == cur.casefold()
    if same:
        return CONFLICT_SAME
    if manual:
        return CONFLICT_MANUAL
    return CONFLICT_UPDATE


def classify_match(
    action: str,
    reasons: list[str],
    extras: list,
    query: CompanyRec,
    loc_info: dict[str, Any] | None,
) -> str:
    if not query.name:
        return CLASS_INVALID
    if action == "create_company":
        return CLASS_NEW
    high_like = [
        item
        for item in extras
        if "domain_exact" in item[1] or "record_no_exact" in item[1]
    ]
    if action == "possible_company_match":
        if len(high_like) >= 2 or len(extras) >= 2 and any("domain_exact" in item[1] for item in extras):
            return CLASS_AMBIGUOUS
        if len(extras) >= 2:
            return CLASS_AMBIGUOUS
        return CLASS_POSSIBLE
    strong = (
        "domain_exact" in reasons
        or "record_no_exact" in reasons
        or ("name_exact" in reasons and ("phone" in reasons or "address_city_state" in reasons))
    )
    if loc_info and loc_info.get("address_differs") and strong:
        return CLASS_NEW_LOCATION
    if strong and "domain_exact" in reasons and "name_exact" in reasons:
        return CLASS_EXACT
    if strong:
        return CLASS_STRONG
    if "name_exact" in reasons:
        return CLASS_POSSIBLE
    return CLASS_STRONG


def allowed_resolutions(ri_class: str, loc_info: dict[str, Any] | None = None) -> list[str]:
    if ri_class in {CLASS_POSSIBLE, CLASS_AMBIGUOUS}:
        allowed = [RES_USE_EXISTING, RES_CREATE_NEW, RES_SKIP]
        if loc_info and loc_info.get("address_differs"):
            allowed.insert(1, RES_NEW_LOCATION)
        return allowed
    if ri_class == CLASS_NEW_LOCATION:
        return [RES_USE_EXISTING, RES_NEW_LOCATION, RES_CREATE_NEW, RES_SKIP]
    if ri_class == CLASS_NEW:
        return [RES_CREATE_NEW, RES_USE_EXISTING, RES_SKIP]
    if ri_class in {CLASS_EXACT, CLASS_STRONG}:
        return [RES_SKIP]
    return [RES_SKIP]


def pack_possible(cand: CompanyRec, reasons: list[str]) -> dict[str, Any]:
    return {
        "company_id": cand.company_id,
        "name": cand.name,
        "city": cand.display_city,
        "state": cand.display_state,
        "address": cand.display_address,
        "phone": cand.display_phone,
        "website": cand.display_website,
        "reasons": list(reasons),
    }


@dataclass
class ResearchPlanRow:
    row_id: int
    source_row_number: int
    ri_class: str
    matcher_action: str
    matcher_reasons: list[str]
    mapped: dict[str, str]
    matched_company_id: int | None = None
    matched_company_name: str = ""
    matched_city: str = ""
    matched_state: str = ""
    matched_address: str = ""
    matched_phone: str = ""
    matched_website: str = ""
    possibles: list[dict[str, Any]] = field(default_factory=list)
    location: dict[str, Any] | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    existing_ccrs: list[dict[str, Any]] = field(default_factory=list)
    this_client_ccr_id: int | None = None
    relationship_action: str = ""
    planned_status: str = ""
    status_action: str = ""
    notes_action: str = "no_notes_change"
    research_priority_code: str = ""
    research_priority_label: str = ""
    attributes: dict[str, list[str]] = field(default_factory=dict)
    sources: list[dict[str, str]] = field(default_factory=list)
    allowed_resolutions: list[str] = field(default_factory=list)
    match_resolution: str = ""
    blocking: bool = False
    blocking_reasons: list[str] = field(default_factory=list)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    custom_attributes: list[dict[str, Any]] = field(default_factory=list)
    workflow_fields: dict[str, str] = field(default_factory=dict)
    ignored_fields: list[str] = field(default_factory=list)
    notes_explicit: str = ""
    master_resolutions_by_field: dict[str, str] = field(default_factory=dict)
    same_batch_key: str = ""
    same_batch_row_ids: list[int] = field(default_factory=list)
    same_batch_conflicts: list[dict[str, Any]] = field(default_factory=list)
    research_anchor: bool = True
    lineage: dict[str, Any] = field(default_factory=dict)


def _client_exists(conn, client_id: int) -> bool:
    return conn.execute("SELECT 1 FROM clients WHERE id=?", (int(client_id),)).fetchone() is not None


def detect_batch_caveat(mapped_rows: list[dict[str, str]]) -> str:
    notes = [blank(r.get("qualification_notes")) for r in mapped_rows if blank(r.get("qualification_notes"))]
    if len(notes) >= 2 and len(set(notes)) == 1:
        return notes[0]
    return ""


def plan_research_rows(
    conn,
    *,
    client_id: int,
    mapped_rows: list[dict[str, str]],
    row_ids: list[int] | None = None,
    match_resolutions: dict[int, dict[str, Any]] | None = None,
    master_resolutions: dict[tuple[int, str], str] | None = None,
    contact_resolutions: dict[int, dict[str, Any]] | None = None,
    mapping: dict[str, Any] | None = None,
    raw_rows: list[dict[str, str]] | None = None,
    batch_id: int = 0,
) -> list[ResearchPlanRow]:
    companies = load_company_recs(conn)
    locations = load_locations(conn)
    ccrs = load_ccrs(conn)
    default_status = default_ccr_status(conn, client_id)
    proposed: list[CompanyRec] = []
    caveat = detect_batch_caveat(mapped_rows)
    resolutions = match_resolutions or {}
    master_res = master_resolutions or {}
    contact_res = contact_resolutions or {}
    planned: list[ResearchPlanRow] = []
    for index, mapped in enumerate(mapped_rows):
        row_id = int(row_ids[index]) if row_ids else index + 1
        source_row = index + 2
        query = _query_company(
            {
                "company_name": mapped.get("company_name", ""),
                "website": mapped.get("website", ""),
                "phone": mapped.get("phone", ""),
                "address": mapped.get("address", ""),
                "city": mapped.get("city", ""),
                "state": mapped.get("state", ""),
                "zip": mapped.get("zip", ""),
                "external_record_no": "",
            }
        )
        action, matched, reasons, extras = _match_company(query, companies, proposed)
        loc_info = None
        conflicts: list[dict[str, Any]] = []
        existing = []
        this_ccr = None
        if matched is not None and matched.company_id is not None:
            loc_info = location_compare(mapped, matched, locations.get(int(matched.company_id), []))
            existing = ccrs.get(int(matched.company_id), [])
            this_ccr = next((row for row in existing if int(row["client_id"]) == int(client_id)), None)
            cid = int(matched.company_id)
            website_manual = is_manual_authority(conn, entity_type="company", entity_id=cid, field="website")
            phone_manual = is_manual_authority(conn, entity_type="company", entity_id=cid, field="legacy_phone")
            name_manual = is_manual_authority(conn, entity_type="company", entity_id=cid, field="company_name")
            addr_manual = is_manual_authority(conn, entity_type="company", entity_id=cid, field="address")
            website_c = conflict_class(
                mapped.get("website", ""),
                matched.display_website,
                "website",
                website_manual,
            )
            phone_c = conflict_class(
                mapped.get("phone", ""),
                matched.display_phone,
                "phone",
                phone_manual,
            )
            if loc_info.get("address_differs"):
                addr_c = CONFLICT_NEW_LOC
            elif loc_info.get("same_master_address"):
                addr_c = CONFLICT_SAME
            else:
                incoming_addr = f"{mapped.get('address','')} {mapped.get('city','')} {mapped.get('state','')} {mapped.get('zip','')}".strip()
                current_addr = f"{matched.display_address} {matched.display_city} {matched.display_state} {matched.display_zip}".strip()
                addr_c = conflict_class(
                    incoming_addr,
                    current_addr,
                    "address",
                    addr_manual,
                )
            name_c = conflict_class(
                mapped.get("company_name", ""),
                matched.name,
                "company_name",
                name_manual,
            )

            def _conflict(field: str, incoming: str, current: str, klass: str) -> dict[str, Any]:
                decision = blank(master_res.get((row_id, field)))
                return {
                    "field": field,
                    "incoming": incoming,
                    "current": current,
                    "class": klass,
                    "resolution": decision,
                    "authority": "manual" if klass == CONFLICT_MANUAL else "",
                    "allowed_master_actions": master_actions_for(klass),
                    "provenance": provenance_evidence(
                        conn, entity_type="company", entity_id=cid, field=field if field != "phone" else "legacy_phone"
                    ),
                }

            conflicts = [
                _conflict("company_name", mapped.get("company_name", ""), matched.name, name_c),
                _conflict("website", mapped.get("website", ""), matched.display_website, website_c),
                _conflict("phone", mapped.get("phone", ""), matched.display_phone, phone_c),
                _conflict(
                    "address",
                    f"{mapped.get('address','')}, {mapped.get('city','')}, {mapped.get('state','')} {mapped.get('zip','')}".strip(),
                    f"{matched.display_address}, {matched.display_city}, {matched.display_state} {matched.display_zip}".strip(),
                    addr_c,
                ),
            ]
        ri_class = classify_match(action, reasons, extras, query, loc_info)
        archived_match = matched is not None and getattr(matched, "archived", False)
        if archived_match:
            ri_class = CLASS_POSSIBLE
            if "archived_master_requires_restore" not in reasons:
                reasons = list(reasons) + ["archived_master_requires_restore"]
        resolution = resolutions.get(row_id) or {}
        res_type = blank(resolution.get("resolution_type"))
        if archived_match and res_type in {RES_USE_EXISTING, RES_CREATE_NEW, RES_NEW_LOCATION}:
            res_type = ""
        if res_type == RES_USE_EXISTING and resolution.get("company_id"):
            chosen = next((c for c, _r in extras if c.company_id == int(resolution["company_id"])), None)
            if chosen is None and matched is not None and matched.company_id == int(resolution["company_id"]):
                chosen = matched
            if chosen is not None and chosen.company_id is not None:
                matched = chosen
                ri_class = CLASS_STRONG if ri_class in {CLASS_POSSIBLE, CLASS_AMBIGUOUS, CLASS_NEW} else ri_class
                if ri_class == CLASS_NEW_LOCATION and res_type == RES_USE_EXISTING:
                    pass
        elif res_type == RES_NEW_LOCATION and matched is not None:
            ri_class = CLASS_NEW_LOCATION
        elif res_type == RES_CREATE_NEW:
            if ri_class in {CLASS_POSSIBLE, CLASS_AMBIGUOUS, CLASS_NEW_LOCATION}:
                ri_class = CLASS_NEW
                matched = None
                action = "create_company"
        elif res_type == RES_SKIP:
            ri_class = "SKIPPED"

        if action == "create_company" and query.name and ri_class == CLASS_NEW:
            proposed.append(
                CompanyRec(
                    company_id=None,
                    proposed_key=f"row:{row_id}",
                    name=query.name,
                    norm_name=query.norm_name,
                    domain=query.domain,
                    phone=query.phone,
                    addr=query.addr,
                    city=query.city,
                    state=query.state,
                    record_no="",
                    source_row=source_row,
                )
            )

        code, label = parse_priority(mapped.get("research_priority"))
        attributes = {
            "target_market": split_multi(mapped.get("target_market")),
            "equipment_product": split_multi(mapped.get("equipment_product")) or (
                [blank(mapped.get("equipment_product"))] if blank(mapped.get("equipment_product")) else []
            ),
            "potential_component": split_multi(mapped.get("potential_components"))
            or ([blank(mapped.get("potential_components"))] if blank(mapped.get("potential_components")) else []),
            "target_department": split_multi(mapped.get("target_department")),
        }
        raw = raw_rows[index] if raw_rows and index < len(raw_rows) else {}
        custom_attrs: list[dict[str, Any]] = []
        for spec in custom_attribute_maps(mapping):
            typed = parse_typed_value(raw.get(spec["header"]), spec["value_type"])
            if not typed["original_value"]:
                continue
            custom_attrs.append(
                {
                    "key": spec["key"],
                    "label": spec["label"],
                    "value_type": spec["value_type"],
                    "scope": spec["scope"],
                    **typed,
                }
            )
            attributes.setdefault(spec["key"], []).append(typed["original_value"])
        contact = contact_from_mapped(mapped)
        contacts: list[dict[str, Any]] = []
        if contact and ri_class != "SKIPPED":
            known_company = matched.company_id if matched is not None and ri_class not in {
                CLASS_POSSIBLE,
                CLASS_AMBIGUOUS,
                CLASS_INVALID,
            } else None
            planned_contact = classify_research_contact(
                conn,
                company_id=int(known_company) if known_company else None,
                incoming=contact,
                resolution=blank((contact_res.get(row_id) or {}).get("resolution_type")),
            )
            contacts = [planned_contact]
        workflow_fields = {
            key: blank(mapped.get(key))
            for key in (
                "workflow_status",
                "workflow_assigned_rep",
                "workflow_follow_up",
                "workflow_next_action",
                "workflow_hot",
                "workflow_campaign",
            )
            if blank(mapped.get(key))
        }
        ignored = ignored_headers(mapping, list(raw.keys()) if raw else None)
        sources = []
        for role, url, field_name, original in (
            ("PRODUCT", mapped.get("product_source", ""), "equipment_product", mapped.get("equipment_product", "")),
            ("ADDRESS", mapped.get("address_source", ""), "address", mapped.get("address", "")),
            ("PHONE", mapped.get("phone_source", ""), "phone", mapped.get("phone", "")),
            ("WEBSITE", mapped.get("website_source", "") or mapped.get("website", ""), "website", mapped.get("website", "")),
            ("GENERAL_RESEARCH", mapped.get("general_source", ""), "why_client_fits", mapped.get("why_client_fits", "")),
        ):
            url = blank(url)
            if not url:
                continue
            sources.append(
                {
                    "source_role": role,
                    "source_url": url,
                    "source_domain": url_domain(url),
                    "field_supported": field_name,
                    "original_value": blank(original),
                }
            )

        relationship_action = "none"
        planned_status = ""
        status_action = ""
        if ri_class == "SKIPPED":
            relationship_action = "skip"
        elif this_ccr is not None:
            relationship_action = "relationship_already_exists"
            planned_status = this_ccr["status"]
            status_action = "preserve_existing_status"
        elif ri_class in {CLASS_EXACT, CLASS_STRONG, CLASS_NEW_LOCATION, CLASS_NEW} or res_type in {
            RES_USE_EXISTING,
            RES_NEW_LOCATION,
            RES_CREATE_NEW,
        }:
            if ri_class in {CLASS_POSSIBLE, CLASS_AMBIGUOUS, CLASS_INVALID}:
                relationship_action = "deferred"
            else:
                relationship_action = "create_client_relationship"
                planned_status = default_status
                status_action = "use_default_status"

        notes_action = "no_notes_change"
        explicit_notes = blank(mapped.get("imported_notes"))
        if explicit_notes:
            if relationship_action == "create_client_relationship":
                notes_action = "set_imported_notes"
            elif this_ccr is not None:
                existing_notes = this_ccr.get("notes") or ""
                if note_already_present(existing_notes, explicit_notes):
                    notes_action = "imported_notes_already_present"
                else:
                    notes_action = "append_imported_notes"
            else:
                notes_action = "preview_only_explicit_notes"

        blocking_reasons: list[str] = []
        if not query.name:
            blocking_reasons.append("Company Name is required.")
        if ri_class in {CLASS_POSSIBLE, CLASS_AMBIGUOUS} and res_type not in {
            RES_USE_EXISTING,
            RES_CREATE_NEW,
            RES_SKIP,
        }:
            blocking_reasons.append(ri_class)
        if ri_class == CLASS_NEW_LOCATION and res_type not in {
            RES_USE_EXISTING,
            RES_NEW_LOCATION,
            RES_CREATE_NEW,
            RES_SKIP,
        }:
            blocking_reasons.append(CLASS_NEW_LOCATION)
        for conflict in conflicts:
            key = (row_id, conflict["field"])
            decision = blank(master_res.get(key))
            allowed = conflict.get("allowed_master_actions") or master_actions_for(conflict["class"])
            if decision and decision not in allowed and decision not in {MASTER_ACCEPT, MASTER_REJECT, MASTER_DEFER}:
                blocking_reasons.append(f"invalid master resolution {decision} on {conflict['field']}")
            if conflict["class"] == CONFLICT_MANUAL and decision in {
                MASTER_ACCEPT,
                "ACCEPT_INCOMING",
                "FILL_BLANK",
            }:
                blocking_reasons.append(f"manual-authority conflict on {conflict['field']}")
        for planned_contact in contacts:
            if planned_contact.get("blocking"):
                blocking_reasons.append(
                    blank(planned_contact.get("blocking_reason")) or blank(planned_contact.get("contact_class"))
                )

        planned.append(
            ResearchPlanRow(
                row_id=row_id,
                source_row_number=source_row,
                ri_class=ri_class,
                matcher_action=action,
                matcher_reasons=list(reasons),
                mapped=dict(mapped),
                matched_company_id=matched.company_id if matched else None,
                matched_company_name=matched.name if matched else "",
                matched_city=matched.display_city if matched else "",
                matched_state=matched.display_state if matched else "",
                matched_address=matched.display_address if matched else "",
                matched_phone=matched.display_phone if matched else "",
                matched_website=matched.display_website if matched else "",
                possibles=[pack_possible(c, r) for c, r in extras[:8]],
                location=loc_info,
                conflicts=conflicts,
                existing_ccrs=existing,
                this_client_ccr_id=int(this_ccr["ccr_id"]) if this_ccr else None,
                relationship_action=relationship_action,
                planned_status=planned_status,
                status_action=status_action,
                notes_action=notes_action,
                research_priority_code=code,
                research_priority_label=label,
                attributes=attributes,
                sources=sources,
                allowed_resolutions=allowed_resolutions(
                    ri_class if ri_class != "SKIPPED" else CLASS_NEW,
                    loc_info,
                ),
                match_resolution=res_type,
                blocking=bool(blocking_reasons),
                blocking_reasons=blocking_reasons,
                contacts=contacts,
                custom_attributes=custom_attrs,
                workflow_fields=workflow_fields,
                ignored_fields=ignored,
                notes_explicit=explicit_notes,
                master_resolutions_by_field={
                    conflict["field"]: blank(conflict.get("resolution"))
                    for conflict in conflicts
                    if blank(conflict.get("resolution"))
                },
            )
        )
    apply_same_batch_consolidation(planned)
    attach_lineage_forecast(conn, planned, client_id=int(client_id), batch_id=int(batch_id))
    return planned


def _norm_scalar(value: object) -> str:
    return re.sub(r"\s+", " ", blank(value).casefold())


def _unique_values(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = blank(value)
        key = _norm_scalar(text)
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _master_norm(field: str, value: object) -> str:
    text = blank(value)
    if not text:
        return ""
    if field == "phone":
        return digits_phone(text) or _norm_scalar(text)
    if field == "website":
        return domain(text) or _norm_scalar(text)
    if field in {"address", "city", "state", "zip"}:
        return norm_addr(text) or _norm_scalar(text)
    return _norm_scalar(text)


def _unique_master_values(field: str, values: list[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = blank(value)
        key = _master_norm(field, text)
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def same_batch_key(row: ResearchPlanRow) -> str:
    if row.ri_class in {CLASS_INVALID, "SKIPPED"}:
        return ""
    resolved = row.ri_class in {CLASS_EXACT, CLASS_STRONG, CLASS_NEW_LOCATION} or (
        row.match_resolution == RES_USE_EXISTING and row.matched_company_id
    )
    if resolved and row.matched_company_id:
        return f"id:{int(row.matched_company_id)}"
    name = norm_name(row.mapped.get("company_name", ""))
    if name:
        return f"new:{name}"
    return f"row:{row.row_id}"


def apply_same_batch_consolidation(rows: list[ResearchPlanRow]) -> None:
    """One research identity per client/company/batch. Contacts stay per source row."""
    groups: dict[str, list[ResearchPlanRow]] = {}
    for row in rows:
        key = same_batch_key(row)
        row.same_batch_key = key
        row.research_anchor = True
        row.same_batch_row_ids = [row.row_id]
        row.same_batch_conflicts = []
        if not key:
            continue
        groups.setdefault(key, []).append(row)
    for members in groups.values():
        ids = [row.row_id for row in members]
        for index, row in enumerate(members):
            row.same_batch_row_ids = list(ids)
            row.research_anchor = index == 0
        conflicts: list[dict[str, Any]] = []

        priority_vals = _unique_values([row.research_priority_code for row in members])
        if len(priority_vals) > 1:
            conflicts.append(
                {
                    "class": SAME_BATCH_RESEARCH_CONFLICT,
                    "field": "research_priority",
                    "values": priority_vals,
                    "row_ids": ids,
                }
            )
        else:
            chosen = next(
                (row for row in members if not blank(row.research_priority_code)),
                members[0],
            )
            for row in members:
                row.research_priority_code = chosen.research_priority_code
                row.research_priority_label = chosen.research_priority_label

        why_vals = _unique_values([row.mapped.get("why_client_fits", "") for row in members])
        if len(why_vals) > 1:
            conflicts.append(
                {
                    "class": SAME_BATCH_RESEARCH_CONFLICT,
                    "field": "why_client_fits",
                    "values": why_vals,
                    "row_ids": ids,
                }
            )
        elif why_vals:
            for row in members:
                row.mapped["why_client_fits"] = why_vals[0]

        narrative_vals = _unique_values([row.mapped.get("qualification_notes", "") for row in members])
        if len(narrative_vals) > 1:
            conflicts.append(
                {
                    "class": SAME_BATCH_RESEARCH_CONFLICT,
                    "field": "qualification_notes",
                    "values": narrative_vals,
                    "row_ids": ids,
                }
            )
        elif narrative_vals:
            for row in members:
                row.mapped["qualification_notes"] = narrative_vals[0]

        merged_attrs: dict[str, list[str]] = {}
        for row in members:
            for key, values in (row.attributes or {}).items():
                merged_attrs.setdefault(key, [])
                merged_attrs[key].extend(values or [])
        for key, values in list(merged_attrs.items()):
            merged_attrs[key] = _unique_values([blank(v) for v in values])
        for row in members:
            row.attributes = {key: list(vals) for key, vals in merged_attrs.items()}

        custom_by_key: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for row in members:
            for item in row.custom_attributes:
                key = blank(item.get("key"))
                if not key:
                    continue
                custom_by_key.setdefault(key, []).append((_norm_scalar(item.get("original_value")), item))
        merged_custom: list[dict[str, Any]] = []
        for key, items in custom_by_key.items():
            distinct: list[dict[str, Any]] = []
            seen: set[str] = set()
            for normed, item in items:
                if not normed or normed in seen:
                    continue
                seen.add(normed)
                distinct.append(item)
            if len(distinct) > 1:
                conflicts.append(
                    {
                        "class": SAME_BATCH_ATTRIBUTE_CONFLICT,
                        "field": key,
                        "values": [blank(item.get("original_value")) for item in distinct],
                        "row_ids": ids,
                    }
                )
            elif distinct:
                merged_custom.append(distinct[0])
        for row in members:
            row.custom_attributes = list(merged_custom)

        merged_sources: list[dict[str, str]] = []
        seen_sources: set[tuple[str, str]] = set()
        for row in members:
            for source in row.sources:
                pair = (blank(source.get("source_role")), blank(source.get("source_url")))
                if not pair[1] or pair in seen_sources:
                    continue
                seen_sources.add(pair)
                merged_sources.append(dict(source))
        for row in members:
            row.sources = list(merged_sources)

        for field in MASTER_SCALAR_FIELDS:
            distinct = _unique_master_values(field, [row.mapped.get(field, "") for row in members])
            if len(distinct) > 1:
                conflicts.append(
                    {
                        "class": SAME_BATCH_MASTER_CONFLICT,
                        "field": field,
                        "values": distinct,
                        "row_ids": ids,
                    }
                )
            elif distinct:
                for row in members:
                    row.mapped[field] = distinct[0]

        incoming_by_field: dict[str, list[dict[str, Any]]] = {}
        for row in members:
            for conflict in row.conflicts:
                field = blank(conflict.get("field"))
                if not field:
                    continue
                incoming_by_field.setdefault(field, []).append(conflict)
        merged_conflicts: list[dict[str, Any]] = []
        for field, items in incoming_by_field.items():
            distinct = _unique_master_values(field, [item.get("incoming") for item in items])
            already = any(
                blank(item.get("class")) == SAME_BATCH_MASTER_CONFLICT and blank(item.get("field")) == field
                for item in conflicts
            )
            if len(distinct) > 1:
                if not already:
                    conflicts.append(
                        {
                            "class": SAME_BATCH_MASTER_CONFLICT,
                            "field": field,
                            "values": distinct,
                            "row_ids": ids,
                        }
                    )
                merged_conflicts.extend(items)
            elif items:
                ranked = sorted(
                    items,
                    key=lambda item: _MASTER_CLASS_RANK.get(blank(item.get("class")), 9),
                )
                merged_conflicts.append(ranked[0])

        for row in members:
            if merged_conflicts and row.research_anchor:
                row.conflicts = list(merged_conflicts)
            row.same_batch_conflicts = list(conflicts)
            for item in conflicts:
                reason = blank(item.get("class")) + ":" + blank(item.get("field"))
                if reason not in row.blocking_reasons:
                    row.blocking_reasons.append(reason)
            row.blocking = bool(row.blocking_reasons)


LINEAGE_SOURCE_SYSTEM = "RESEARCH_IMPORT"
LINEAGE_ALIAS_RECORD_NO = ""
LINEAGE_ACTION_CREATE = "CREATE"
LINEAGE_ACTION_DEDUPE = "DEDUPE"
LINEAGE_ACTION_SKIP = "SKIP"


def research_identity_record_no(batch_id: int, row_id: int) -> str:
    """Confirm writes source_record_no as '{batch_id}:{staged_row_id}'."""
    return f"{int(batch_id)}:{int(row_id)}"


def _company_lineage_key(row: ResearchPlanRow) -> str:
    if row.matched_company_id:
        return f"id:{int(row.matched_company_id)}"
    return row.same_batch_key or f"row:{row.row_id}"


def _lineage_identity_exists(conn, *, client_id: int, source_record_no: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM company_source_identities
        WHERE source_system=?
          AND source_record_no=?
          AND COALESCE(client_id, 0)=?
        """,
        (LINEAGE_SOURCE_SYSTEM, source_record_no, int(client_id)),
    ).fetchone()
    return row is not None


def _lineage_alias_exists(conn, *, company_id: int, client_id: int, alias_norm: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM company_aliases
        WHERE company_id=?
          AND COALESCE(client_id, 0)=?
          AND source_system=?
          AND COALESCE(source_record_no, '')=?
          AND alias_norm=?
        """,
        (
            int(company_id),
            int(client_id),
            LINEAGE_SOURCE_SYSTEM,
            LINEAGE_ALIAS_RECORD_NO,
            alias_norm,
        ),
    ).fetchone()
    return row is not None


def attach_lineage_forecast(
    conn,
    rows: list[ResearchPlanRow],
    *,
    client_id: int,
    batch_id: int,
) -> None:
    """Forecast confirm-side INSERT OR IGNORE alias/identity writes.

    Unique keys match schema:
      identities: (source_system, source_record_no, COALESCE(client_id,0))
      aliases: (company_id, COALESCE(client_id,0), source_system, source_record_no, alias_norm)

    Confirm omits alias source_record_no, so it defaults to ''. Same company +
    client + RESEARCH_IMPORT + same alias_norm therefore DEDUPE across rows and
    later batches. Identities use batch_id:row_id, so each staged row is unique
    unless that exact key already exists (idempotent replay).
    """
    seen_alias: set[tuple[str, str]] = set()
    seen_identity: set[str] = set()
    for row in rows:
        identity_no = research_identity_record_no(batch_id, row.row_id)
        company_key = _company_lineage_key(row)
        name = blank(row.mapped.get("company_name"))
        alias_norm = norm_name(name) if name else ""
        skipped = row.ri_class in {CLASS_INVALID, "SKIPPED"}
        identity_action = LINEAGE_ACTION_SKIP
        alias_action = LINEAGE_ACTION_SKIP
        if not skipped:
            if identity_no in seen_identity or _lineage_identity_exists(
                conn, client_id=int(client_id), source_record_no=identity_no
            ):
                identity_action = LINEAGE_ACTION_DEDUPE
            else:
                identity_action = LINEAGE_ACTION_CREATE
                seen_identity.add(identity_no)
            if not name:
                alias_action = LINEAGE_ACTION_SKIP
            else:
                alias_key = (company_key, alias_norm)
                existing = False
                if row.matched_company_id:
                    existing = _lineage_alias_exists(
                        conn,
                        company_id=int(row.matched_company_id),
                        client_id=int(client_id),
                        alias_norm=alias_norm,
                    )
                if existing or alias_key in seen_alias:
                    alias_action = LINEAGE_ACTION_DEDUPE
                    seen_alias.add(alias_key)
                else:
                    alias_action = LINEAGE_ACTION_CREATE
                    seen_alias.add(alias_key)
        row.lineage = {
            "source_row": row.source_row_number,
            "row_id": row.row_id,
            "company_name": name,
            "matched_company_id": row.matched_company_id,
            "client_id": int(client_id),
            "batch_id": int(batch_id),
            "source_system": LINEAGE_SOURCE_SYSTEM,
            "alias": {
                "action": alias_action,
                "alias_name": name,
                "alias_norm": alias_norm,
                "source_system": LINEAGE_SOURCE_SYSTEM,
                "source_record_no": LINEAGE_ALIAS_RECORD_NO,
                "client_id": int(client_id),
                "batch_id": int(batch_id),
                "source_row": row.source_row_number,
                "company_id": row.matched_company_id,
                "master_field_change": False,
            },
            "source_identity": {
                "action": identity_action,
                "source_system": LINEAGE_SOURCE_SYSTEM,
                "source_record_no": identity_no,
                "client_id": int(client_id),
                "batch_id": int(batch_id),
                "source_row": row.source_row_number,
                "company_id": row.matched_company_id,
                "master_field_change": False,
            },
        }


def proposed_write_summary(forecast: dict[str, Any]) -> list[dict[str, Any]]:
    """Human-readable deterministic write forecast for preview."""
    items: list[tuple[str, str, str, bool]] = [
        ("Companies", "CREATE", "companies_created", True),
        ("Companies", "REUSE", "companies_reused", True),
        ("Locations", "CREATE", "locations_created", True),
        ("CCRs", "CREATE", "ccrs_created", True),
        ("CCRs", "REUSE", "ccrs_reused", True),
        ("Contacts", "CREATE", "contacts_created", True),
        ("Contacts", "REUSE", "contacts_reused", True),
        ("Contacts", "SKIP", "contacts_skipped", True),
        ("Notes", "APPEND", "notes_append", True),
        ("Notes", "DEDUPE", "notes_dedupe", True),
        ("Research records", "CREATE", "research_created", True),
        ("Custom attributes", "CREATE", "attributes_created", True),
        ("Research sources", "CREATE", "sources_captured", True),
        ("Company aliases", "CREATE", "aliases_created", False),
        ("Company aliases", "DEDUPE", "aliases_deduped", False),
        ("Company aliases", "SKIP", "aliases_skipped", False),
        ("Company source identities", "CREATE", "identities_created", False),
        ("Company source identities", "DEDUPE", "identities_deduped", False),
        ("Company source identities", "SKIP", "identities_skipped", False),
        ("Field provenance events", "CREATE", "provenance_events_created", True),
        ("Master fields", "FILL BLANK", "master_fill_blank", True),
        ("Master fields", "UPDATE", "master_accept_incoming", True),
        ("Master fields", "PRESERVE", "master_keep_existing", True),
        ("Workflow writes", "SKIP", "workflow_fields_will_write", True),
        ("Rows", "BLOCKED", "blocked_rows", True),
    ]
    summary: list[dict[str, Any]] = []
    for category, action, key, masterish in items:
        count = int(forecast.get(key) or 0)
        if count == 0 and key not in {
            "aliases_created",
            "aliases_deduped",
            "identities_created",
            "identities_deduped",
            "workflow_fields_will_write",
            "provenance_events_created",
        }:
            continue
        lineage = category in {"Company aliases", "Company source identities"}
        summary.append(
            {
                "category": category,
                "action": action,
                "count": count,
                "section": "Source Lineage" if lineage else "Entities",
                "master_field_change": False if lineage else masterish and action in {"UPDATE", "FILL BLANK"},
                "line": f"{action} {count} {category.lower()}",
            }
        )
    return summary


def aggregate_counts(rows: list[ResearchPlanRow], caveat: str) -> dict[str, int]:
    counts = {
        "source_rows": len(rows),
        "valid_rows": 0,
        "existing_companies": 0,
        "strong_matches": 0,
        "new_companies": 0,
        "new_locations": 0,
        "possible_matches": 0,
        "ambiguous": 0,
        "invalid": 0,
        "skipped": 0,
        "existing_ccrs": 0,
        "new_ccrs": 0,
        "research_rows_to_add": 0,
        "research_attributes": 0,
        "sources_captured": 0,
        "master_fills": 0,
        "master_proposed_updates": 0,
        "manual_authority_conflicts": 0,
        "blocking_rows": 0,
        "batch_caveat_applied": 1 if caveat else 0,
        "contacts_present": 0,
        "custom_attribute_values": 0,
        "workflow_fields": 0,
        "ignored_fields": 0,
        "explicit_notes": 0,
        "contacts_exact": 0,
        "contacts_strong": 0,
        "contacts_new": 0,
        "contacts_possible": 0,
        "contacts_ambiguous": 0,
        "contacts_invalid": 0,
        "contacts_blocking": 0,
        "same_batch_conflicts": 0,
    }
    for row in rows:
        if row.ri_class != CLASS_INVALID:
            counts["valid_rows"] += 1
        if row.ri_class in {CLASS_EXACT, CLASS_STRONG}:
            counts["existing_companies"] += 1
            if row.ri_class == CLASS_STRONG:
                counts["strong_matches"] += 1
        elif row.ri_class == CLASS_NEW_LOCATION:
            counts["existing_companies"] += 1
            counts["new_locations"] += 1
        elif row.ri_class == CLASS_NEW:
            counts["new_companies"] += 1
        elif row.ri_class == CLASS_POSSIBLE:
            counts["possible_matches"] += 1
        elif row.ri_class == CLASS_AMBIGUOUS:
            counts["ambiguous"] += 1
        elif row.ri_class == CLASS_INVALID:
            counts["invalid"] += 1
        elif row.ri_class == "SKIPPED":
            counts["skipped"] += 1
        if row.relationship_action == "relationship_already_exists":
            if row.research_anchor:
                counts["existing_ccrs"] += 1
        elif row.relationship_action == "create_client_relationship":
            if row.research_anchor:
                counts["new_ccrs"] += 1
        if row.ri_class not in {CLASS_INVALID, "SKIPPED"} and row.research_anchor:
            counts["research_rows_to_add"] += 1
        if row.research_anchor:
            counts["research_attributes"] += sum(len(v) for v in row.attributes.values())
            counts["sources_captured"] += len(row.sources)
            counts["custom_attribute_values"] += len(row.custom_attributes)
            if row.same_batch_conflicts:
                counts["same_batch_conflicts"] += 1
        for conflict in row.conflicts:
            if conflict["class"] == CONFLICT_FILL:
                counts["master_fills"] += 1
            elif conflict["class"] == CONFLICT_UPDATE:
                counts["master_proposed_updates"] += 1
            elif conflict["class"] == CONFLICT_MANUAL:
                counts["manual_authority_conflicts"] += 1
        if row.blocking:
            counts["blocking_rows"] += 1
        counts["contacts_present"] += len(row.contacts)
        for planned_contact in row.contacts:
            klass = blank(planned_contact.get("contact_class"))
            if klass == "EXACT_CONTACT":
                counts["contacts_exact"] += 1
            elif klass == "STRONG_CONTACT":
                counts["contacts_strong"] += 1
            elif klass == "NEW_CONTACT":
                counts["contacts_new"] += 1
            elif klass == "POSSIBLE_CONTACT_REVIEW":
                counts["contacts_possible"] += 1
            elif klass == "AMBIGUOUS_CONTACT":
                counts["contacts_ambiguous"] += 1
            elif klass == "INVALID_CONTACT":
                counts["contacts_invalid"] += 1
            if planned_contact.get("blocking"):
                counts["contacts_blocking"] += 1
        counts["workflow_fields"] += len(row.workflow_fields)
        counts["ignored_fields"] = max(counts["ignored_fields"], len(row.ignored_fields))
        if row.notes_explicit:
            counts["explicit_notes"] += 1
    return counts


def plan_fingerprint(
    *,
    client_id: int,
    sha256: str,
    mapping: dict[str, str],
    research_method: str,
    research_date: str,
    mapped_rows: list[dict[str, str]],
    match_resolutions: dict[int, dict[str, Any]],
    master_resolutions: dict[tuple[int, str], str],
    options: dict[str, Any] | None = None,
    contact_resolutions: dict[int, dict[str, Any]] | None = None,
) -> str:
    payload = {
        "planner_version": PLANNER_VERSION,
        "client_id": int(client_id),
        "sha256": blank(sha256),
        "mapping": normalize_mapping(mapping),
        "research_method": blank(research_method),
        "research_date": blank(research_date),
        "rows": mapped_rows,
        "match_resolutions": [
            {"row_id": k, **v} for k, v in sorted(match_resolutions.items(), key=lambda item: item[0])
        ],
        "master_resolutions": [
            {"row_id": k[0], "field": k[1], "resolution": v}
            for k, v in sorted(master_resolutions.items(), key=lambda item: (item[0][0], item[0][1]))
        ],
        "contact_resolutions": [
            {"row_id": k, **v}
            for k, v in sorted((contact_resolutions or {}).items(), key=lambda item: item[0])
        ],
        "options": options or {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def planned_contract_slice(rows: list[ResearchPlanRow]) -> list[dict[str, Any]]:
    """Material preview decisions that freeze the confirm fingerprint."""
    slice_rows: list[dict[str, Any]] = []
    for row in rows:
        slice_rows.append(
            {
                "row_id": row.row_id,
                "ri_class": row.ri_class,
                "matched_company_id": row.matched_company_id,
                "relationship_action": row.relationship_action,
                "planned_status": row.planned_status,
                "notes_action": row.notes_action,
                "notes_explicit": row.notes_explicit,
                "conflicts": [
                    {
                        "field": item.get("field"),
                        "class": item.get("class"),
                        "current": item.get("current"),
                        "incoming": item.get("incoming"),
                        "resolution": item.get("resolution") or "",
                    }
                    for item in row.conflicts
                ],
                "contacts": [
                    {
                        "class": item.get("contact_class"),
                        "resolution": item.get("resolution"),
                        "matched": item.get("matched_contact_id"),
                        "cross_company": bool(item.get("cross_company")),
                    }
                    for item in row.contacts
                ],
                "custom": [
                    {
                        "key": item.get("key"),
                        "type": item.get("value_type"),
                        "value": item.get("original_value"),
                    }
                    for item in row.custom_attributes
                ],
                "attributes": row.attributes,
                "sources": row.sources,
                "workflow_preview_only": True,
                "same_batch_key": row.same_batch_key,
                "same_batch_row_ids": row.same_batch_row_ids,
                "same_batch_conflicts": row.same_batch_conflicts,
                "research_anchor": row.research_anchor,
            }
        )
    return slice_rows


def forecast_confirm(rows: list[ResearchPlanRow]) -> dict[str, Any]:
    """Non-destructive summary of what isolated confirm would do."""
    forecast = {
        "companies_created": 0,
        "companies_reused": 0,
        "locations_created": 0,
        "ccrs_created": 0,
        "ccrs_reused": 0,
        "contacts_created": 0,
        "contacts_reused": 0,
        "contacts_skipped": 0,
        "notes_append": 0,
        "notes_dedupe": 0,
        "research_created": 0,
        "attributes_created": 0,
        "sources_captured": 0,
        "master_fill_blank": 0,
        "master_accept_incoming": 0,
        "master_keep_existing": 0,
        "master_add_location": 0,
        "blocked_rows": 0,
        "workflow_fields_will_write": 0,
        "same_batch_conflicts": 0,
        "aliases_created": 0,
        "aliases_deduped": 0,
        "aliases_skipped": 0,
        "identities_created": 0,
        "identities_deduped": 0,
        "identities_skipped": 0,
        "provenance_events_created": 0,
    }
    seen_new: set[str] = set()
    seen_research: set[str] = set()
    seen_ccr: set[str] = set()
    lineage_rows: list[dict[str, Any]] = []
    for row in rows:
        alias_action = blank((row.lineage or {}).get("alias", {}).get("action"))
        identity_action = blank((row.lineage or {}).get("source_identity", {}).get("action"))
        if alias_action == LINEAGE_ACTION_CREATE:
            forecast["aliases_created"] += 1
        elif alias_action == LINEAGE_ACTION_DEDUPE:
            forecast["aliases_deduped"] += 1
        else:
            forecast["aliases_skipped"] += 1
        if identity_action == LINEAGE_ACTION_CREATE:
            forecast["identities_created"] += 1
        elif identity_action == LINEAGE_ACTION_DEDUPE:
            forecast["identities_deduped"] += 1
        else:
            forecast["identities_skipped"] += 1
        if row.lineage:
            lineage_rows.append(row.lineage)
        if row.blocking:
            forecast["blocked_rows"] += 1
        if row.same_batch_conflicts and row.research_anchor:
            forecast["same_batch_conflicts"] += 1
        if row.ri_class in {CLASS_INVALID, "SKIPPED"}:
            continue
        key = row.same_batch_key or norm_name(row.mapped.get("company_name", ""))
        if row.ri_class == CLASS_NEW or row.matched_company_id is None:
            if key and key in seen_new:
                forecast["companies_reused"] += 1
            else:
                forecast["companies_created"] += 1
                if key:
                    seen_new.add(key)
                forecast["locations_created"] += 1
                for field in ("company_name", "website", "phone", "address", "city", "state", "zip"):
                    if blank(row.mapped.get(field)):
                        forecast["provenance_events_created"] += 1
        else:
            forecast["companies_reused"] += 1
        if key and key not in seen_ccr:
            seen_ccr.add(key)
            if row.relationship_action == "create_client_relationship":
                forecast["ccrs_created"] += 1
            elif row.this_client_ccr_id:
                forecast["ccrs_reused"] += 1
        elif key:
            forecast["ccrs_reused"] += 1
        if row.notes_action in {"set_imported_notes", "append_imported_notes"}:
            forecast["notes_append"] += 1
        elif row.notes_action == "imported_notes_already_present":
            forecast["notes_dedupe"] += 1
        if key and key not in seen_research and row.research_anchor:
            seen_research.add(key)
            forecast["research_created"] += 1
            forecast["attributes_created"] += sum(len(v) for v in row.attributes.values()) + len(row.custom_attributes)
            forecast["sources_captured"] += len(row.sources)
        for contact in row.contacts:
            klass = blank(contact.get("contact_class"))
            res = blank(contact.get("resolution"))
            if klass == "INVALID_CONTACT" or res == "SKIP_CONTACT":
                forecast["contacts_skipped"] += 1
            elif res == "USE_EXISTING_CONTACT" or klass in {"EXACT_CONTACT", "STRONG_CONTACT"}:
                forecast["contacts_reused"] += 1
            elif klass == "NEW_CONTACT" or res == "CREATE_NEW_CONTACT":
                forecast["contacts_created"] += 1
        if row.research_anchor:
            for conflict in row.conflicts:
                decision = blank(conflict.get("resolution"))
                klass = blank(conflict.get("class"))
                if decision == "FILL_BLANK":
                    forecast["master_fill_blank"] += 1
                    forecast["provenance_events_created"] += 1
                elif decision == "ACCEPT_INCOMING":
                    forecast["master_accept_incoming"] += 1
                    forecast["provenance_events_created"] += 1
                elif decision == "ADD_AS_LOCATION" or (
                    row.ri_class == CLASS_NEW_LOCATION and row.match_resolution == "TREAT_AS_NEW_LOCATION"
                ):
                    forecast["master_add_location"] += 1
                    forecast["locations_created"] += 1
                elif klass in {"SAME", "PROPOSE_UPDATE", "MANUAL_AUTHORITY_CONFLICT", "FILL_BLANK"}:
                    forecast["master_keep_existing"] += 1
    forecast["lineage"] = lineage_rows
    forecast["write_summary"] = proposed_write_summary(forecast)
    return forecast


def row_to_dict(row: ResearchPlanRow) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "source_row_number": row.source_row_number,
        "ri_class": row.ri_class,
        "matcher_action": row.matcher_action,
        "matcher_reasons": row.matcher_reasons,
        "company_name": row.mapped.get("company_name", ""),
        "address": row.mapped.get("address", ""),
        "city": row.mapped.get("city", ""),
        "state": row.mapped.get("state", ""),
        "zip": row.mapped.get("zip", ""),
        "phone": row.mapped.get("phone", ""),
        "website": row.mapped.get("website", ""),
        "mapped": row.mapped,
        "matched_company_id": row.matched_company_id,
        "matched_company_name": row.matched_company_name,
        "matched_city": row.matched_city,
        "matched_state": row.matched_state,
        "matched_address": row.matched_address,
        "matched_phone": row.matched_phone,
        "matched_website": row.matched_website,
        "possibles": row.possibles,
        "location": row.location,
        "conflicts": row.conflicts,
        "existing_clients": [x.get("client_code") for x in row.existing_ccrs],
        "this_client_ccr_id": row.this_client_ccr_id,
        "relationship_action": row.relationship_action,
        "planned_status": row.planned_status,
        "status_action": row.status_action,
        "notes_action": row.notes_action,
        "research_priority_code": row.research_priority_code,
        "research_priority_label": row.research_priority_label,
        "why_client_fits": row.mapped.get("why_client_fits", ""),
        "qualification_notes": row.mapped.get("qualification_notes", ""),
        "attributes": row.attributes,
        "sources": row.sources,
        "allowed_resolutions": row.allowed_resolutions,
        "match_resolution": row.match_resolution,
        "blocking": row.blocking,
        "blocking_reasons": row.blocking_reasons,
        "contacts": row.contacts,
        "custom_attributes": row.custom_attributes,
        "workflow_fields": row.workflow_fields,
        "ignored_fields": row.ignored_fields,
        "notes_explicit": row.notes_explicit,
        "workflow_preview_only": True,
        "master_resolutions_by_field": row.master_resolutions_by_field,
        "same_batch_key": row.same_batch_key,
        "same_batch_row_ids": row.same_batch_row_ids,
        "same_batch_conflicts": row.same_batch_conflicts,
        "research_anchor": row.research_anchor,
        "lineage": row.lineage,
    }
