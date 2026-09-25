"""DS-14 duplicate exception workbench.

Presentation, decision ergonomics, and safe plan-decision handling on top of
DS-11 human review, DS-12 classification, and DS-13 merge planning.

Does not create a second classifier, planner, or merge execution path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from duplicate_classify import CLASSIFIER_KIND, CLASSIFIER_VERSION
from duplicate_review import (
    DISPOSITION_MULTI,
    DISPOSITION_NOT,
    DISPOSITION_RESEARCH,
    DuplicateReviewSaveRequest,
    _blank,
    _row_dict,
    _table_exists,
    pair_ids,
    pair_key,
    save_duplicate_review,
)
from import_brown_industries import domain
from merge_plan import (
    ACTION_KEEP_SURVIVOR,
    ACTION_PRESERVE_ALIAS,
    ACTION_PRESERVE_IDENTITY,
    ACTION_PRESERVE_LOCATION,
    CONTACT_EXACT,
    EX_CAMPAIGN,
    EX_CONTACT,
    EX_FIELD,
    EX_FOLLOWUP,
    EX_HOT,
    EX_IDENTITY,
    EX_MULTI,
    EX_NEXT,
    EX_REP,
    EX_RN,
    EX_STATUS,
    EX_SURVIVOR,
    MergePlanDecisionRequest,
    MergePlanError,
    PLAN_ONLY_WARNING,
    STATE_NEEDS,
    STATE_NOT_SAFE,
    STATE_READY,
    STATE_STALE,
    UI_STATE,
    _as_int,
    _campaigns_for_company,
    _company_row,
    _count_where,
    _existing_plan_row,
    _history_counts,
    _load_ccrs,
    _load_classification,
    _load_review,
    _norm_text,
    _table_columns,
    build_merge_plan,
    get_merge_plan,
    list_merge_plans,
    replan_merge_plan,
    save_merge_plan_decision,
)
from models import NorthStarUser

WORKBENCH_VERSION = "DS14_WORKBENCH_V1"

HUMAN_DISPOSITIONS = frozenset({DISPOSITION_MULTI, DISPOSITION_NOT, DISPOSITION_RESEARCH})

DECISION_TYPE_CODES = {
    "survivor": {EX_SURVIVOR},
    "status": {EX_STATUS},
    "rn": {EX_RN},
    "field": {EX_FIELD},
    "phone": {EX_FIELD},
    "contact": {EX_CONTACT},
    "rep": {EX_REP},
    "hot": {EX_HOT},
    "followup": {EX_FOLLOWUP},
    "follow-up": {EX_FOLLOWUP},
    "next_action": {EX_NEXT},
    "next-action": {EX_NEXT},
    "campaign": {EX_CAMPAIGN},
    "location": {EX_MULTI},
    "other": {EX_IDENTITY, EX_CAMPAIGN},
}

FIELD_LABELS = {
    "phone": "phone",
    "phone_extension": "phone extension",
    "website": "website",
    "address": "address",
    "city": "city",
    "state": "state",
    "zip": "ZIP",
    "company_name": "company name",
    "external_record_no": "LeadMaster RN",
    "archive_state": "archive state",
}


class WorkbenchDispositionRequest(DuplicateReviewSaveRequest):
    confirm: bool = False


def _users(conn: sqlite3.Connection) -> dict[int, str]:
    if not _table_exists(conn, "users"):
        return {}
    return {
        int(row["id"]): _blank(row["full_name"]) or _blank(row["email"]) or f"User {row['id']}"
        for row in conn.execute("SELECT id, full_name, email FROM users").fetchall()
    }


def _json_obj(value: object | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raw = _blank(value)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _identities_for(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "company_source_identities"):
        return []
    cols = _table_columns(conn, "company_source_identities")
    select = ["id"]
    for name in ("source_system", "source_record_no", "source_company_name", "client_id"):
        if name in cols:
            select.append(name)
    rows = conn.execute(
        f"SELECT {', '.join(select)} FROM company_source_identities WHERE company_id=? ORDER BY id",
        (int(company_id),),
    ).fetchall()
    out = []
    for row in rows:
        item = _row_dict(row)
        rn = _blank(item.get("source_record_no"))
        system = _blank(item.get("source_system"))
        out.append(
            {
                "id": item.get("id"),
                "source_system": system,
                "source_record_no": rn or None,
                "source_company_name": _blank(item.get("source_company_name")),
                "label": " ".join(part for part in (system, rn) if part) or system or "identity",
            }
        )
    return out


def _client_label(row: dict[str, Any]) -> str:
    return _blank(row.get("client_name")) or _blank(row.get("client_code")) or (
        f"Client {row.get('client_id')}" if row.get("client_id") else "Client"
    )


def _campaigns_for_client(campaigns: list[dict[str, Any]], client_id: object) -> list[dict[str, Any]]:
    wanted = _as_int(client_id)
    if not wanted:
        return []
    out = []
    for camp in campaigns:
        if _as_int(camp.get("client_id")) != wanted:
            continue
        name = _blank(camp.get("campaign_name")) or f"Campaign {camp.get('campaign_id')}"
        out.append(
            {
                "campaign_id": camp.get("campaign_id"),
                "client_id": camp.get("client_id"),
                "campaign_name": name,
                "notes": _blank(camp.get("notes")),
            }
        )
    return out


def _contacts_for_company(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "contacts"):
        return []
    cols = _table_columns(conn, "contacts")
    select = ["id", "company_id"]
    for name in (
        "first_name",
        "last_name",
        "title",
        "email",
        "phone",
        "phone_extension",
        "external_record_no",
        "source",
    ):
        if name in cols:
            select.append(name)
    order = "id"
    if "last_name" in cols and "first_name" in cols:
        order = "last_name, first_name, id"
    rows = conn.execute(
        f"SELECT {', '.join(select)} FROM contacts WHERE company_id=? ORDER BY {order}",
        (int(company_id),),
    ).fetchall()
    out = []
    for row in rows:
        item = _row_dict(row)
        first = _blank(item.get("first_name"))
        last = _blank(item.get("last_name"))
        item["name"] = " ".join(part for part in (first, last) if part) or f"Contact {item.get('id')}"
        item["master_rn_label"] = _blank(item.get("external_record_no")) or "None"
        out.append(item)
    return out


def _aliases_for_company(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "company_aliases"):
        return []
    cols = _table_columns(conn, "company_aliases")
    select = ["id"]
    for name in ("alias_name", "source_system", "source_record_no"):
        if name in cols:
            select.append(name)
    rows = conn.execute(
        f"SELECT {', '.join(select)} FROM company_aliases WHERE company_id=? ORDER BY id",
        (int(company_id),),
    ).fetchall()
    out = []
    for row in rows:
        item = _row_dict(row)
        item["label"] = _blank(item.get("alias_name")) or f"Alias {item.get('id')}"
        out.append(item)
    return out


def _locations_for_company(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "company_locations"):
        return []
    cols = _table_columns(conn, "company_locations")
    select = ["id"]
    for name in (
        "location_name",
        "location_type",
        "address",
        "city",
        "state",
        "zip",
        "phone",
        "is_primary",
        "is_headquarters",
    ):
        if name in cols:
            select.append(name)
    rows = conn.execute(
        f"SELECT {', '.join(select)} FROM company_locations WHERE company_id=? ORDER BY id",
        (int(company_id),),
    ).fetchall()
    out = []
    for row in rows:
        item = _row_dict(row)
        city_state = ", ".join(part for part in (_blank(item.get("city")), _blank(item.get("state"))) if part)
        item["label"] = _blank(item.get("location_name")) or city_state or f"Location {item.get('id')}"
        out.append(item)
    return out


def identity_summary(conn: sqlite3.Connection, company_id: int) -> dict[str, Any]:
    row = _company_row(conn, int(company_id))
    name = _blank(row.get("company_name")) or f"Company {company_id}"
    ccrs = _load_ccrs(conn, int(company_id))
    users = _users(conn)
    campaigns = _campaigns_for_company(conn, int(company_id))
    clients = []
    seen: set[int] = set()
    active_ccr = 0
    removed_ccr = 0
    relationships: list[dict[str, Any]] = []
    active_relationships: list[dict[str, Any]] = []
    for ccr in ccrs:
        snap = _ccr_snapshot(ccr, users, campaigns)
        relationships.append(snap)
        cid = int(ccr.get("client_id") or 0)
        if cid and cid not in seen:
            seen.add(cid)
            clients.append(
                {
                    "client_id": cid,
                    "client_code": _blank(ccr.get("client_code")),
                    "client_name": _blank(ccr.get("client_name")),
                }
            )
        if ccr.get("active"):
            active_ccr += 1
            active_relationships.append(snap)
        else:
            removed_ccr += 1
    identities = _identities_for(conn, int(company_id))
    history = _history_counts(conn, int(company_id))
    contacts = _contacts_for_company(conn, int(company_id))
    aliases = _aliases_for_company(conn, int(company_id))
    locations = _locations_for_company(conn, int(company_id))
    master_rn = _blank(row.get("external_record_no"))
    website = _blank(row.get("website"))
    city = _blank(row.get("city"))
    state = _blank(row.get("state"))
    zip_code = _blank(row.get("zip"))
    active_client_names = ", ".join(_client_label(item) for item in active_relationships) or "None"
    return {
        "company_id": int(company_id),
        "company_name": name,
        "record_label": f"{name} — Record {int(company_id)}",
        "city": city,
        "state": state,
        "zip": zip_code,
        "city_state": ", ".join(part for part in (city, state) if part),
        "city_state_zip": ", ".join(part for part in (city, state, zip_code) if part),
        "address": _blank(row.get("address")),
        "phone": _blank(row.get("phone")),
        "phone_extension": _blank(row.get("phone_extension")),
        "website": website,
        "domain": domain(website) if website else "",
        "master_rn": master_rn or None,
        "master_rn_label": master_rn or "None",
        "identities": identities,
        "identity_summary": ", ".join(item["label"] for item in identities) or "None",
        "clients": clients,
        "client_names": active_client_names,
        "relationships": relationships,
        "active_relationships": active_relationships,
        "active_ccr_count": active_ccr,
        "removed_ccr_count": removed_ccr,
        "contacts": contacts,
        "contact_count": history.get("contacts") or len(contacts),
        "aliases": aliases,
        "alias_count": len(aliases),
        "locations": locations,
        "location_count": len(locations),
        "campaigns": campaigns,
        "campaign_count": history.get("campaigns") or len(campaigns),
        "notes_count": history.get("notes") or 0,
        "activities_count": history.get("activities") or 0,
        "history": history,
        "archive_state": row.get("archive_state") or "active",
        "archived": bool(row.get("archived")),
    }


def _contact_snapshot(conn: sqlite3.Connection, contact_id: object) -> dict[str, Any] | None:
    cid = _as_int(contact_id)
    if not cid or not _table_exists(conn, "contacts"):
        return None
    cols = _table_columns(conn, "contacts")
    select = ["id", "company_id"]
    for name in (
        "first_name",
        "last_name",
        "title",
        "email",
        "phone",
        "phone_extension",
        "external_record_no",
        "source",
    ):
        if name in cols:
            select.append(name)
    row = conn.execute(
        f"SELECT {', '.join(select)} FROM contacts WHERE id=?",
        (cid,),
    ).fetchone()
    if row is None:
        return None
    item = _row_dict(row)
    first = _blank(item.get("first_name"))
    last = _blank(item.get("last_name"))
    item["name"] = " ".join(part for part in (first, last) if part) or f"Contact {cid}"
    item["master_rn_label"] = _blank(item.get("external_record_no")) or "None"
    return item


def _ccr_snapshot(
    ccr: dict[str, Any] | None,
    users: dict[int, str],
    campaigns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not ccr:
        return {}
    uid = _as_int(ccr.get("assigned_user_id"))
    assigned = users.get(uid, "") if uid else ""
    if not assigned:
        assigned = "Unassigned"
    client = _client_label(ccr)
    status = _blank(ccr.get("status"))
    status_label = status if status else "No status"
    active = bool(ccr.get("active"))
    return {
        "ccr_id": ccr.get("id") or ccr.get("ccr_id"),
        "client_id": ccr.get("client_id"),
        "client_name": _blank(ccr.get("client_name")) or client,
        "client_code": _blank(ccr.get("client_code")),
        "status": status or None,
        "status_label": status_label,
        "display": f"{client} — {status_label}",
        "assigned_user_id": uid,
        "assigned_user_name": assigned,
        "hot": bool(ccr.get("hot")),
        "follow_up_date": _blank(ccr.get("follow_up_date")) or None,
        "next_action": _blank(ccr.get("next_action")),
        "external_record_no": _blank(ccr.get("external_record_no")) or None,
        "active": active,
        "state_label": "Active" if active else "Removed",
        "campaigns": _campaigns_for_client(campaigns or [], ccr.get("client_id")),
    }


def decision_needed_label(exception: dict[str, Any]) -> str:
    code = _blank(exception.get("code"))
    field = _blank(exception.get("field"))
    details = exception.get("details") or {}
    client = _blank(details.get("client_name")) or _blank(details.get("client_code"))
    if code == EX_SURVIVOR:
        return "Choose survivor"
    if code == EX_STATUS:
        return f"Resolve {client or 'client'} status"
    if code == EX_RN:
        return f"Resolve {client or 'LeadMaster'} RN"
    if code == EX_FIELD:
        if field == "phone":
            return "Resolve phone conflict"
        return f"Resolve {FIELD_LABELS.get(field, field or 'field')} conflict"
    if code == EX_CONTACT:
        return "Review possible duplicate contact"
    if code == EX_MULTI:
        return "Confirm separate locations"
    if code == EX_REP:
        return f"Resolve {client or 'assigned'} rep"
    if code == EX_HOT:
        return f"Resolve {client or 'Hot'} Hot state"
    if code == EX_FOLLOWUP:
        return f"Resolve {client or 'follow-up'} follow-up"
    if code == EX_NEXT:
        return f"Resolve {client or 'next action'} next action"
    if code == EX_CAMPAIGN:
        return f"Resolve {client or 'campaign'} campaign"
    if code == EX_IDENTITY:
        return "Identity is not safe to plan"
    return (exception.get("label") or code or "Needs a decision").replace("_", " ").title()


def why_northstar_needs_you(exception: dict[str, Any], plan: dict[str, Any] | None = None) -> str:
    code = _blank(exception.get("code"))
    field = _blank(exception.get("field"))
    details = exception.get("details") or {}
    client = _blank(details.get("client_name")) or _blank(details.get("client_code")) or "this client"
    if code == EX_SURVIVOR:
        return (
            "The records appear to be the same organization, but neither has enough "
            "evidence to choose the canonical survivor."
        )
    if code == EX_STATUS:
        return (
            f"Both records have a {client} relationship with different statuses. "
            "NorthStar will not choose which status survives."
        )
    if code == EX_RN:
        return (
            "Both records have valid LeadMaster record numbers. NorthStar will preserve "
            "both identities, but needs you to choose the primary RN."
        )
    if code == EX_FIELD:
        nice = FIELD_LABELS.get(field, field or "field")
        return (
            f"The {nice} values differ. NorthStar will not overwrite a non-blank "
            "survivor value, and needs you to choose how the source value is preserved."
        )
    if code == EX_CONTACT:
        return (
            "These contacts may be the same person, but they are not a mechanical exact "
            "duplicate. NorthStar will not consolidate them automatically."
        )
    if code == EX_MULTI:
        return (
            "Location evidence suggests these may be distinct physical sites, not a safe "
            "single-master merge."
        )
    if code == EX_REP:
        return (
            f"Both {client} records have assigned representatives. NorthStar will not "
            "choose which assignment survives."
        )
    if code == EX_HOT:
        return f"The {client} Hot flags differ. NorthStar will not choose Hot vs Not Hot."
    if code == EX_FOLLOWUP:
        return f"The {client} follow-up dates differ. NorthStar needs an explicit date choice."
    if code == EX_NEXT:
        return f"The {client} next-action values differ. NorthStar will not pick one automatically."
    if code == EX_CAMPAIGN:
        return f"{client} campaign membership needs a human look before a future merge."
    if code == EX_IDENTITY:
        return "Source identities cannot be combined safely, so merge planning is blocked."
    if plan and plan.get("plan_state") == STATE_NOT_SAFE:
        return "NorthStar will not plan a merge for this pair until the identity concern is resolved."
    return _blank(exception.get("label")) or "NorthStar needs a human choice before planning can continue."


def _side_labels(plan: dict[str, Any]) -> tuple[str, str, int | None, int | None]:
    a_id = _as_int(plan.get("company_a_id"))
    b_id = _as_int(plan.get("company_b_id"))
    survivor = _as_int(plan.get("survivor_company_id"))
    source = _as_int(plan.get("source_company_id"))
    ident_a = plan.get("company_a") or {}
    ident_b = plan.get("company_b") or {}
    label_a = ident_a.get("record_label") or f"Record {a_id}"
    label_b = ident_b.get("record_label") or f"Record {b_id}"
    return label_a, label_b, survivor, source


def _choice(value: str, label: str, *, helper: str = "") -> dict[str, str]:
    item = {"value": value, "label": label}
    if helper:
        item["helper"] = helper
    return item


def _parse_date(value: str) -> datetime | None:
    raw = _blank(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def workbench_choices(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    exception: dict[str, Any],
) -> list[dict[str, str]]:
    code = _blank(exception.get("code"))
    supported = {_blank(opt) for opt in (exception.get("options") or [])}
    details = exception.get("details") or {}
    label_a, label_b, survivor, source = _side_labels(plan)
    a_id = _as_int(plan.get("company_a_id"))
    b_id = _as_int(plan.get("company_b_id"))

    if plan.get("plan_state") == STATE_NOT_SAFE or code in {EX_MULTI, EX_IDENTITY}:
        return []

    if code == EX_SURVIVOR:
        return [
            _choice(f"SURVIVOR:{a_id}", f"Make Record {a_id} the survivor"),
            _choice(f"SURVIVOR:{b_id}", f"Make Record {b_id} the survivor"),
        ]

    if code == EX_FIELD:
        return [
            _choice(ACTION_KEEP_SURVIVOR, "Keep survivor value"),
            _choice(
                ACTION_PRESERVE_IDENTITY,
                "Keep survivor value and preserve source as historical/source data",
            ),
            _choice(ACTION_PRESERVE_ALIAS, "Keep survivor value and preserve source as an alias"),
            _choice(ACTION_PRESERVE_LOCATION, "Keep survivor value and preserve source as a location"),
        ]

    if code == EX_STATUS:
        survivor_status = _blank(details.get("survivor_status") or details.get("survivor"))
        source_status = _blank(details.get("source_status") or details.get("source"))
        return [
            _choice(
                ACTION_KEEP_SURVIVOR,
                f'Keep "{survivor_status or "survivor status"}"',
            ),
            _choice( "KEEP_SOURCE", f'Keep "{source_status or "source status"}"'),
        ]

    if code == EX_REP:
        users = _users(conn)
        surv_id = _as_int(details.get("survivor_user_id") or details.get("survivor"))
        src_id = _as_int(details.get("source_user_id") or details.get("source"))
        surv_name = details.get("survivor_user_name") or users.get(surv_id or -1) or f"Rep {surv_id}"
        src_name = details.get("source_user_name") or users.get(src_id or -1) or f"Rep {src_id}"
        return [
            _choice(ACTION_KEEP_SURVIVOR, f"Keep {surv_name}"),
            _choice("KEEP_SOURCE", f"Keep {src_name}"),
            _choice("UNASSIGNED", "Unassigned"),
        ]

    if code == EX_HOT:
        return [
            _choice("HOT", "Hot"),
            _choice("NOT_HOT", "Not Hot"),
        ]

    if code == EX_FOLLOWUP:
        choices = [
            _choice(ACTION_KEEP_SURVIVOR, f"Use {label_a if survivor == a_id else 'survivor'} follow-up"),
            _choice("KEEP_SOURCE", f"Use {label_b if source == b_id else 'source'} follow-up"),
        ]
        left = _parse_date(_blank(details.get("survivor") or details.get("survivor_follow_up")))
        right = _parse_date(_blank(details.get("source") or details.get("source_follow_up")))
        if left and right:
            choices.append(_choice("USE_EARLIEST", "Use earliest"))
            choices.append(_choice("USE_LATEST", "Use latest"))
        choices.append(_choice("CLEAR", "Clear"))
        return choices

    if code == EX_NEXT:
        return [
            _choice(ACTION_KEEP_SURVIVOR, "Use survivor next action"),
            _choice("KEEP_SOURCE", "Use source next action"),
            _choice("CLEAR", "Clear"),
        ]

    if code == EX_RN:
        if plan.get("identity_concern"):
            return []
        a_rn = ((plan.get("company_a") or {}).get("master_rn_label")) or "None"
        b_rn = ((plan.get("company_b") or {}).get("master_rn_label")) or "None"
        if survivor == a_id:
            keep_a, keep_b = ACTION_KEEP_SURVIVOR, "KEEP_SOURCE"
        elif survivor == b_id:
            keep_a, keep_b = "KEEP_SOURCE", ACTION_KEEP_SURVIVOR
        else:
            keep_a, keep_b = ACTION_KEEP_SURVIVOR, "KEEP_SOURCE"
        helper = "The non-primary RN will be preserved as a source identity when safe."
        return [
            _choice(keep_a, f"Use Record {a_id} RN ({a_rn}) as primary", helper=helper),
            _choice(keep_b, f"Use Record {b_id} RN ({b_rn}) as primary", helper=helper),
        ]

    if code == EX_CONTACT:
        return [
            _choice("CONSOLIDATE", "Same person — consolidate in future merge"),
            _choice("KEEP_SEPARATE", "Keep both contacts"),
        ]

    if code == EX_CAMPAIGN:
        return [
            _choice("PRESERVE_ALL_UNIQUE", "Preserve all unique memberships"),
            _choice(ACTION_KEEP_SURVIVOR, "Keep survivor campaign notes"),
            _choice("KEEP_SOURCE", "Keep source campaign notes"),
        ]

    return [_choice(opt, opt.replace("_", " ")) for opt in supported if opt]


def _enrich_exception(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    exception: dict[str, Any],
) -> dict[str, Any]:
    item = dict(exception)
    details = dict(item.get("details") or {})
    code = _blank(item.get("code"))
    a_id = _as_int(plan.get("company_a_id"))
    b_id = _as_int(plan.get("company_b_id"))
    survivor = _as_int(plan.get("survivor_company_id")) or a_id
    source = _as_int(plan.get("source_company_id")) or b_id
    users = _users(conn)
    if code in {EX_STATUS, EX_REP, EX_HOT, EX_FOLLOWUP, EX_NEXT, EX_RN} and item.get("client_id"):
        client_id = int(item["client_id"])
        src_ccrs = {int(r["client_id"]): r for r in _load_ccrs(conn, int(source or b_id or 0))}
        tgt_ccrs = {int(r["client_id"]): r for r in _load_ccrs(conn, int(survivor or a_id or 0))}
        src = _ccr_snapshot(src_ccrs.get(client_id), users)
        tgt = _ccr_snapshot(tgt_ccrs.get(client_id), users)
        details["client_name"] = tgt.get("client_name") or src.get("client_name") or details.get("client_name")
        details["client_code"] = tgt.get("client_code") or src.get("client_code") or details.get("client_code")
        details["source_ccr"] = src
        details["survivor_ccr"] = tgt
        details["source_status"] = src.get("status")
        details["survivor_status"] = tgt.get("status")
        details["source_user_id"] = src.get("assigned_user_id")
        details["survivor_user_id"] = tgt.get("assigned_user_id")
        details["source_user_name"] = src.get("assigned_user_name")
        details["survivor_user_name"] = tgt.get("assigned_user_name")
        details["source_follow_up"] = src.get("follow_up_date")
        details["survivor_follow_up"] = tgt.get("follow_up_date")
        details["campaigns"] = {
            "source": _campaigns_for_company(conn, int(source or 0)) if source else [],
            "survivor": _campaigns_for_company(conn, int(survivor or 0)) if survivor else [],
        }
        item["rn_prompt"] = (
            "MASTER / PRIMARY RN AFTER FUTURE MERGE"
            if code == EX_RN
            else ""
        )
        item["rn_preservation"] = (
            "The non-primary RN will be preserved as a source identity when safe."
            if code == EX_RN
            else ""
        )
    if code == EX_CONTACT:
        details["source_contact"] = _contact_snapshot(conn, details.get("source_contact_id"))
        details["survivor_contact"] = _contact_snapshot(conn, details.get("survivor_contact_id"))
    if code == EX_FIELD:
        field = _blank(item.get("field"))
        details["field_label"] = FIELD_LABELS.get(field, field)
        details["record_a_value"] = (
            details.get("survivor") if survivor == a_id else details.get("source")
        )
        details["record_b_value"] = (
            details.get("source") if survivor == a_id else details.get("survivor")
        )
        if not details.get("record_a_value"):
            details["record_a_value"] = ((plan.get("company_a") or {}).get(field))
        if not details.get("record_b_value"):
            details["record_b_value"] = ((plan.get("company_b") or {}).get(field))
    item["details"] = details
    item["decision_needed"] = decision_needed_label(item)
    item["why_you"] = why_northstar_needs_you(item, plan)
    item["choices"] = workbench_choices(conn, plan, item)
    item["simple_survivor"] = code == EX_SURVIVOR
    item["code_secondary"] = code
    return item


def _queue_complexity(plan: dict[str, Any]) -> str:
    state = _blank(plan.get("plan_state"))
    exceptions = plan.get("exceptions") or []
    if state == STATE_NOT_SAFE:
        return "not_safe"
    if state == STATE_STALE or plan.get("stale"):
        return "stale"
    if state == STATE_READY:
        return "ready"
    if (
        state == STATE_NEEDS
        and len(exceptions) == 1
        and _blank((exceptions[0] or {}).get("code")) == EX_SURVIVOR
    ):
        return "simple"
    if state == STATE_NEEDS:
        return "complex"
    return "other"


def _sort_rank(mode: str) -> int:
    return {"simple": 0, "complex": 1, "not_safe": 2, "ready": 3, "stale": 4, "other": 5}.get(mode, 9)


def preservation_summary(plan: dict[str, Any]) -> dict[str, Any]:
    identities = plan.get("identities") or {}
    contacts = plan.get("contacts") or {}
    aliases = plan.get("aliases") or {}
    campaigns = plan.get("campaigns") or {}
    history = plan.get("history") or {}
    source_h = history.get("source") or {}
    survivor_h = history.get("survivor") or {}
    ident_count = int(identities.get("move_identities") or 0) + int(
        identities.get("already_on_survivor") or 0
    )
    if identities.get("preserve_source_rn_as_identity") or identities.get("never_drop_source_rn"):
        ident_count = max(ident_count, 1)
        if identities.get("source_master_rn") and identities.get("survivor_master_rn"):
            ident_count = max(ident_count, 2)
    contact_count = int(contacts.get("unique_to_preserve") or 0) + int(
        contacts.get("exact_duplicates") or 0
    ) + int(contacts.get("possible_duplicates") or 0) + int(contacts.get("conflicts") or 0)
    notes = int(source_h.get("notes") or 0) + int(survivor_h.get("notes") or 0)
    activities = int(source_h.get("activities") or 0) + int(survivor_h.get("activities") or 0)
    alias_count = int(aliases.get("unique_to_preserve") or 0)
    location_count = int((plan.get("locations") or {}).get("additional_source_locations") or 0)
    if not location_count:
        location_count = len((plan.get("locations") or {}).get("comparisons") or [])
    campaign_count = int(campaigns.get("unique_memberships_to_preserve") or 0)
    lines = []
    if ident_count:
        lines.append(f"{ident_count} LeadMaster/source identities")
    if contact_count:
        lines.append(f"{contact_count} contacts")
    if notes:
        lines.append(f"{notes} notes")
    if activities:
        lines.append(f"{activities} activities")
    if alias_count:
        lines.append(f"{alias_count} aliases")
    if location_count:
        lines.append(f"{location_count} additional location{'s' if location_count != 1 else ''}")
    if campaign_count:
        lines.append(f"{campaign_count} campaign memberships")
    if not lines:
        lines.append("Existing survivor data plus unique source history")
    return {
        "identities": ident_count,
        "contacts": contact_count,
        "notes": notes,
        "activities": activities,
        "aliases": alias_count,
        "locations": location_count,
        "campaigns": campaign_count,
        "lines": lines,
        "automatic_contacts": [
            row
            for row in (contacts.get("rows") or [])
            if row.get("consolidation") == "SAFE_CONTACT_CONSOLIDATION"
            or row.get("classification") == CONTACT_EXACT
        ],
    }


def survivor_comparison(plan: dict[str, Any]) -> dict[str, Any]:
    left = plan.get("company_a") or {}
    right = plan.get("company_b") or {}
    fields = [
        ("company_id", "Company ID"),
        ("company_name", "Canonical name"),
        ("address", "Address"),
        ("city_state", "City/state"),
        ("zip", "ZIP"),
        ("phone", "Phone"),
        ("website", "Website"),
        ("domain", "Domain"),
        ("master_rn_label", "Master RN"),
        ("identity_summary", "Source identities"),
        ("client_names", "Clients"),
        ("active_ccr_count", "Active CCR count"),
        ("removed_ccr_count", "Removed CCR count"),
        ("contact_count", "Contacts"),
        ("alias_count", "Aliases"),
        ("location_count", "Locations"),
        ("campaign_count", "Campaigns"),
        ("notes_count", "Notes"),
        ("activities_count", "Activities"),
        ("archive_state", "Archive state"),
    ]
    rows = []
    for key, label in fields:
        a_val = left.get(key)
        b_val = right.get(key)
        a_text = "None" if a_val in (None, "") else a_val
        b_text = "None" if b_val in (None, "") else b_val
        rows.append(
            {
                "field": key,
                "label": label,
                "company_a": a_text,
                "company_b": b_text,
                "different": str(a_text).strip().lower() != str(b_text).strip().lower(),
            }
        )
    return {
        "company_a": left,
        "company_b": right,
        "rows": rows,
        "proposed_survivor_company_id": plan.get("survivor_company_id"),
        "proposed_source_company_id": plan.get("source_company_id"),
        "survivor_source": plan.get("survivor_source"),
        "survivor_reason": _survivor_reason(plan),
    }


def _survivor_reason(plan: dict[str, Any]) -> str:
    source = _blank(plan.get("survivor_source"))
    survivor = plan.get("survivor_company_id")
    if source == "HUMAN_MERGE_CANDIDATE":
        return f"Julie already chose Record {survivor} as the Merge Candidate survivor."
    if source == "DS12_PROPOSAL":
        return f"NorthStar proposed Record {survivor} from the automated assessment."
    if source == "EXCEPTION_DECISION":
        return f"A prior plan decision chose Record {survivor} as survivor."
    if not survivor:
        return "NorthStar will not guess a survivor for this pair."
    return f"Record {survivor} is the current proposed survivor."


def _not_safe_reason(plan: dict[str, Any]) -> str:
    reasons = plan.get("not_safe_reasons") or []
    texts = []
    for reason in reasons:
        code = _blank(reason)
        if code == EX_MULTI:
            texts.append("Possible multi-location identity.")
        elif code == EX_IDENTITY:
            texts.append("Source identities cannot be combined safely.")
        elif code == "needs_research":
            texts.append("Human marked this pair Needs Research.")
        elif code == "human_disposition_forbids_planning":
            texts.append("A human Not Duplicate or Multi-Location decision removed merge-planning eligibility.")
        elif code == "not_high_confidence":
            texts.append("This pair is not a current high-confidence duplicate.")
        else:
            texts.append(code.replace("_", " "))
    if not texts:
        if plan.get("location_concern"):
            texts.append("Possible multi-location identity.")
        else:
            texts.append("NorthStar will not plan a merge for this pair.")
    return " ".join(texts)


def _human_review_payload(conn: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    key = _blank(plan.get("pair_key"))
    review = _load_review(conn, key)
    if not review:
        return {
            "disposition": "UNREVIEWED",
            "reason": "",
            "actor_user_id": None,
            "actor_name": "",
            "reviewed_at": "",
            "proposed_survivor_company_id": None,
            "proposed_source_company_id": None,
            "human_authoritative": True,
        }
    users = _users(conn)
    uid = _as_int(review.get("reviewed_by_user_id"))
    return {
        "disposition": _blank(review.get("disposition")) or "UNREVIEWED",
        "reason": _blank(review.get("review_reason")),
        "actor_user_id": uid,
        "actor_name": users.get(uid or -1, ""),
        "reviewed_at": _blank(review.get("reviewed_at")),
        "proposed_survivor_company_id": review.get("proposed_survivor_company_id"),
        "proposed_source_company_id": review.get("proposed_source_company_id"),
        "human_authoritative": True,
    }


def _labels(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict):
            label = _blank(item.get("label") or item.get("code"))
            if label:
                out.append(label)
        elif item:
            out.append(str(item))
    return out


def _automated_assessment(conn: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    stored = _load_classification(conn, _blank(plan.get("pair_key")))
    evidence = _json_obj(stored.get("evidence_json"))
    return {
        "assessment_title": "NorthStar Automated Assessment",
        "classifier_kind": CLASSIFIER_KIND,
        "classifier_version": _blank(stored.get("classifier_version")) or CLASSIFIER_VERSION,
        "external_ai_used": False,
        "classification": _blank(stored.get("classification")) or plan.get("classification"),
        "confidence": _blank(stored.get("confidence")),
        "classified_at": _blank(stored.get("classified_at") or stored.get("updated_at")),
        "evidence": evidence.get("evidence") or [],
        "concerns": evidence.get("concerns") or [],
        "why": _labels(evidence.get("why") or evidence.get("evidence")),
        "concern_labels": _labels(evidence.get("concern_labels") or evidence.get("concerns")),
        "proposed_survivor_company_id": stored.get("proposed_survivor_company_id"),
        "proposed_source_company_id": stored.get("proposed_source_company_id"),
        "survivor_decision_required": bool(evidence.get("survivor_decision_required")),
        "not_julies_decision": True,
    }


def enrich_queue_plan(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    cache: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cache = cache if cache is not None else {}
    a_id = int(plan["company_a_id"])
    b_id = int(plan["company_b_id"])
    if a_id not in cache:
        cache[a_id] = identity_summary(conn, a_id)
    if b_id not in cache:
        cache[b_id] = identity_summary(conn, b_id)
    out = dict(plan)
    out["company_a"] = cache[a_id]
    out["company_b"] = cache[b_id]
    exceptions = [_enrich_exception(conn, out, row) for row in (plan.get("exceptions") or [])]
    out["exceptions"] = exceptions
    out["exception_count"] = len(exceptions)
    out["decision_needed"] = exceptions[0]["decision_needed"] if exceptions else (
        "Not safe to plan" if plan.get("plan_state") == STATE_NOT_SAFE else "Ready for Review"
    )
    out["decision_needed_all"] = [row["decision_needed"] for row in exceptions]
    out["workbench_mode"] = _queue_complexity(out)
    out["simple_decision"] = out["workbench_mode"] == "simple"
    out["complex_decision"] = out["workbench_mode"] == "complex"
    out["allow_quick_survivor"] = out["simple_decision"]
    out["human_disposition_allowed"] = True
    out["not_safe_reason"] = _not_safe_reason(out) if out["workbench_mode"] == "not_safe" else ""
    out["active_client_does_not_scope"] = True
    out["planning_only"] = True
    out["merge_will_occur"] = False
    out["no_merge_button"] = True
    out["no_execute_merge"] = True
    return out


def enrich_plan_detail(conn: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    out = enrich_queue_plan(conn, plan)
    out["all_exceptions"] = [
        _enrich_exception(conn, out, row) for row in (plan.get("all_exceptions") or plan.get("exceptions") or [])
    ]
    out["preservation"] = preservation_summary(out)
    out["survivor_comparison"] = survivor_comparison(out)
    out["automated_assessment"] = _automated_assessment(conn, out)
    out["human_review"] = _human_review_payload(conn, out)
    out["why_proposed_survivor"] = _survivor_reason(out)
    out["next_unresolved_exception"] = (out.get("exceptions") or [None])[0]
    history = []
    for row in plan.get("decision_history") or []:
        item = dict(row)
        item["exception_label"] = decision_needed_label(
            {"code": _blank(item.get("exception_key")).split(":")[0], "exception_key": item.get("exception_key")}
        )
        history.append(item)
    out["decision_history"] = history
    out["workbench_version"] = WORKBENCH_VERSION
    out["plan_only_warning"] = PLAN_ONLY_WARNING
    out["ready_for_review"] = out.get("plan_state") == STATE_READY
    out["not_safe_reason"] = _not_safe_reason(out) if out.get("plan_state") == STATE_NOT_SAFE else out.get("not_safe_reason") or ""
    return out


def _matches_decision_type(plan: dict[str, Any], decision_type: str) -> bool:
    wanted = _norm_text(decision_type).replace(" ", "_")
    if not wanted or wanted == "all":
        return True
    if wanted == "other":
        known: set[str] = set()
        for key, codes in DECISION_TYPE_CODES.items():
            if key != "other":
                known.update(codes)
        return any(_blank(row.get("code")) not in known for row in (plan.get("exceptions") or []))
    codes = DECISION_TYPE_CODES.get(wanted)
    if not codes:
        return True
    return any(_blank(row.get("code")) in codes for row in (plan.get("exceptions") or []))


def _matches_same_client(plan: dict[str, Any], same_client: str) -> bool:
    wanted = _norm_text(same_client)
    if wanted in {"", "all"}:
        return True
    flag = bool(plan.get("same_client_ccr_conflict"))
    if wanted in {"yes", "true", "1"}:
        return flag
    if wanted in {"no", "false", "0"}:
        return not flag
    return True


def list_workbench_plans(
    conn: sqlite3.Connection,
    *,
    state: str = "",
    q: str = "",
    decision_type: str = "",
    same_client: str = "",
    client_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    del client_id  # Active Client must not scope master duplicate plans.
    raw = list_merge_plans(conn, state="", q="", offset=0, limit=200)
    cache: dict[int, dict[str, Any]] = {}
    plans = [enrich_queue_plan(conn, row, cache) for row in raw.get("plans") or []]
    wanted_state = _blank(state).upper()
    if wanted_state in {"READY", "READY_FOR_REVIEW", "READY_FOR_HUMAN_APPROVAL"}:
        wanted_state = STATE_READY
    elif wanted_state in {"NEEDS", "NEEDS_DECISIONS", "NEEDS_EXCEPTION_DECISION"}:
        wanted_state = STATE_NEEDS
    elif wanted_state in {"NOT_SAFE", "NOT_SAFE_TO_PLAN"}:
        wanted_state = STATE_NOT_SAFE
    needle = _norm_text(q)
    filtered = []
    for row in plans:
        if wanted_state and _blank(row.get("plan_state")) != wanted_state:
            continue
        if not _matches_decision_type(row, decision_type) or not _matches_same_client(row, same_client):
            continue
        hay = " ".join(
            [
                str(row.get("pair_key") or ""),
                str((row.get("company_a") or {}).get("record_label") or ""),
                str((row.get("company_b") or {}).get("record_label") or ""),
                str((row.get("company_a") or {}).get("city_state") or ""),
                str((row.get("company_b") or {}).get("city_state") or ""),
                str((row.get("company_a") or {}).get("master_rn_label") or ""),
                str((row.get("company_b") or {}).get("master_rn_label") or ""),
                str((row.get("company_a") or {}).get("client_names") or ""),
                str((row.get("company_b") or {}).get("client_names") or ""),
                " ".join(
                    str(rel.get("display") or "")
                    for rel in ((row.get("company_a") or {}).get("active_relationships") or [])
                ),
                " ".join(
                    str(rel.get("display") or "")
                    for rel in ((row.get("company_b") or {}).get("active_relationships") or [])
                ),
                str(row.get("decision_needed") or ""),
            ]
        ).lower()
        if needle and needle not in hay:
            continue
        filtered.append(row)
    filtered.sort(
        key=lambda row: (
            _sort_rank(row.get("workbench_mode") or "other"),
            int(row.get("company_a_id") or 0),
            int(row.get("company_b_id") or 0),
        )
    )
    summary = dict(raw.get("summary") or {})
    summary["simple_decisions"] = sum(1 for row in plans if row.get("workbench_mode") == "simple")
    summary["complex_decisions"] = sum(1 for row in plans if row.get("workbench_mode") == "complex")
    summary["needs_exception_decision"] = sum(1 for row in plans if row.get("plan_state") == STATE_NEEDS)
    summary["ready_for_review"] = sum(1 for row in plans if row.get("plan_state") == STATE_READY)
    summary["not_safe_to_plan"] = sum(1 for row in plans if row.get("plan_state") == STATE_NOT_SAFE)
    summary["stale"] = sum(1 for row in plans if row.get("plan_state") == STATE_STALE or row.get("stale"))
    offset = max(int(offset or 0), 0)
    limit = min(max(int(limit or 50), 1), 200)
    page = filtered[offset : offset + limit]
    return {
        **raw,
        "summary": summary,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "plans": page,
        "active_client_ignored": True,
        "active_client_does_not_scope": True,
        "workbench_version": WORKBENCH_VERSION,
        "no_merge_button": True,
        "no_execute_merge": True,
        "planning_only": True,
        "merge_will_occur": False,
    }


def get_workbench_plan(conn: sqlite3.Connection, company_a_id: int, company_b_id: int) -> dict[str, Any]:
    plan = get_merge_plan(conn, company_a_id, company_b_id)
    return enrich_plan_detail(conn, plan)


def _next_needs_pair(
    conn: sqlite3.Connection,
    *,
    exclude_key: str,
) -> dict[str, Any] | None:
    listed = list_workbench_plans(conn, state=STATE_NEEDS, offset=0, limit=50)
    for row in listed.get("plans") or []:
        if row.get("pair_key") != exclude_key:
            return {
                "pair_key": row.get("pair_key"),
                "company_a_id": row.get("company_a_id"),
                "company_b_id": row.get("company_b_id"),
                "decision_needed": row.get("decision_needed"),
                "record_a": (row.get("company_a") or {}).get("record_label"),
                "record_b": (row.get("company_b") or {}).get("record_label"),
            }
    return None


def save_workbench_decision(
    conn: sqlite3.Connection,
    *,
    actor: NorthStarUser,
    company_a_id: int,
    company_b_id: int,
    body: MergePlanDecisionRequest,
) -> dict[str, Any]:
    existing = _existing_plan_row(conn, pair_key(company_a_id, company_b_id))
    if not existing:
        raise MergePlanError("plan_not_found")
    live = build_merge_plan(
        conn,
        company_a_id,
        company_b_id,
        existing_plan_id=_as_int(existing.get("id")),
    )
    stored_fp = _blank(existing.get("plan_fingerprint"))
    if stored_fp and stored_fp != _blank(live.get("plan_fingerprint")):
        conn.execute(
            "UPDATE company_merge_plans SET plan_state=? WHERE id=?",
            (STATE_STALE, int(existing["id"])),
        )
        conn.commit()
        raise MergePlanError(
            "stale_plan",
            {"plan_state": STATE_STALE, "plan_state_label": UI_STATE[STATE_STALE]},
        )
    expected = _blank(body.expected_plan_fingerprint)
    if expected and expected != stored_fp:
        raise MergePlanError("stale_plan")
    if live.get("plan_state") == STATE_NOT_SAFE:
        raise MergePlanError("not_safe_to_plan", {"reason": _not_safe_reason(live)})
    if live.get("plan_state") == STATE_STALE:
        raise MergePlanError("stale_plan")
    saved = save_merge_plan_decision(
        conn,
        actor=actor,
        company_a_id=company_a_id,
        company_b_id=company_b_id,
        body=body,
    )
    plan = enrich_plan_detail(conn, saved["plan"])
    remaining = plan.get("exceptions") or []
    ready = plan.get("plan_state") == STATE_READY
    return {
        **saved,
        "plan": plan,
        "next_unresolved_exception": remaining[0] if remaining else None,
        "ready_for_review": ready,
        "remaining_exception_count": len(remaining),
        "next_needs_decision_pair": _next_needs_pair(conn, exclude_key=plan["pair_key"]) if ready else None,
        "replan_invoked": True,
        "approval_created": False,
        "merge_will_occur": False,
        "no_merge_button": True,
        "workbench_version": WORKBENCH_VERSION,
    }


def save_workbench_disposition(
    conn: sqlite3.Connection,
    *,
    actor: NorthStarUser,
    company_a_id: int,
    company_b_id: int,
    body: WorkbenchDispositionRequest,
) -> dict[str, Any]:
    if not actor or not actor.is_administrator:
        raise PermissionError("administrator required")
    disposition = _blank(body.disposition).upper()
    if disposition not in HUMAN_DISPOSITIONS:
        raise MergePlanError("invalid_disposition", {"disposition": disposition})
    if not bool(body.confirm):
        raise MergePlanError("confirmation_required")
    if len(_blank(body.reason)) < 3:
        raise MergePlanError("reason_required")
    review = save_duplicate_review(
        conn,
        actor=actor,
        company_a_id=company_a_id,
        company_b_id=company_b_id,
        body=DuplicateReviewSaveRequest(
            disposition=disposition,
            reason=body.reason,
            actor_id=body.actor_id,
            created_by=body.created_by,
        ),
        review_source="EXCEPTION_WORKBENCH",
    )
    replanned = replan_merge_plan(
        conn,
        actor=actor,
        company_a_id=company_a_id,
        company_b_id=company_b_id,
    )
    plan = enrich_plan_detail(conn, replanned["plan"])
    lo, hi = pair_ids(company_a_id, company_b_id)
    auto = _load_classification(conn, pair_key(lo, hi))
    disagreement = _blank(auto.get("classification")) == "HIGH_CONFIDENCE_DUPLICATE" and disposition in HUMAN_DISPOSITIONS
    return {
        "ok": True,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "plan_only_warning": PLAN_ONLY_WARNING,
        "disposition": disposition,
        "reason": _blank(body.reason),
        "confirm": True,
        "review": review,
        "plan": plan,
        "removed_from_merge_planning_eligibility": True,
        "classification_preserved": True,
        "disagreement": disagreement,
        "human_authoritative": True,
        "next_needs_decision_pair": _next_needs_pair(conn, exclude_key=plan["pair_key"]),
        "replan_invoked": True,
        "companies_mutated": False,
        "contacts_mutated": False,
        "ccrs_mutated": False,
        "spoofed_actor_ignored": True,
        "actor_user_id": int(actor.id),
        "workbench_version": WORKBENCH_VERSION,
        "no_merge_button": True,
        "no_execute_merge": True,
    }
