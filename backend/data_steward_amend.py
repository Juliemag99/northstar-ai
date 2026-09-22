"""DS-7 governed Master Company amend: preview, canonicalize, collisions.

Reuses data_steward.amend_company. Does not enable archive, delete, merge,
remove-from-client, restore, or merge-approval creation.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from contact_phone import digits_only_extension, stored_phone_pair
from data_steward import (
    SOURCE_MANUAL_ADMIN,
    StewardError,
    _blank,
    _has_text,
    _load,
    amend_company,
    is_archived,
    provenance_history,
    sql_active_ccr,
)
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from pydantic import BaseModel, Field

from models import NorthStarUser

COMPANY_AMEND_FIELDS = (
    "company_name",
    "address",
    "city",
    "state",
    "zip",
    "website",
    "phone",
    "phone_extension",
)

class CompanyAmendRequest(BaseModel):
    """Governed amend payload. Session actor wins; body actor fields are ignored."""

    company_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    website: str | None = None
    phone: str | None = None
    phone_extension: str | None = None
    reason: str = ""
    expected_updated_at: str | None = None
    preview_fingerprint: str = ""
    actor_id: int | None = Field(default=None, description="Ignored. Session user is the actor.")
    user_id: int | None = Field(default=None, description="Ignored.")
    created_by: str | None = Field(default=None, description="Ignored.")


def fields_from_request(body: CompanyAmendRequest) -> dict[str, Any]:
    payload = body.model_dump()
    out: dict[str, Any] = {}
    for key in COMPANY_AMEND_FIELDS:
        if payload.get(key) is not None:
            out[key] = payload[key]
    return out


HIGH_COLLISION_REASONS = frozenset(
    {
        "same_name+same_city_state",
        "same_name+same_address",
        "same_name+same_phone",
        "same_name+same_website",
        "alias+same_city_state",
        "alias+same_address",
        "alias+same_phone",
        "alias+same_website",
        "source_identity+same_name",
    }
)

_STREET_ABBREV = (
    (re.compile(r"\bstreet\b", re.I), "St"),
    (re.compile(r"\bavenue\b", re.I), "Ave"),
    (re.compile(r"\broad\b", re.I), "Rd"),
    (re.compile(r"\bdrive\b", re.I), "Dr"),
    (re.compile(r"\bboulevard\b", re.I), "Blvd"),
    (re.compile(r"\blane\b", re.I), "Ln"),
    (re.compile(r"\bcourt\b", re.I), "Ct"),
    (re.compile(r"\bhighway\b", re.I), "Hwy"),
    (re.compile(r"\bparkway\b", re.I), "Pkwy"),
    (re.compile(r"\bsuite\b", re.I), "Ste"),
    (re.compile(r"\bnorth\b", re.I), "N"),
    (re.compile(r"\bsouth\b", re.I), "S"),
    (re.compile(r"\beast\b", re.I), "E"),
    (re.compile(r"\bwest\b", re.I), "W"),
)


class DuplicateCompanyError(StewardError):
    def __init__(self, candidates: list[dict[str, Any]]):
        super().__init__("duplicate_company")
        self.candidates = candidates


def _collapse(value: object) -> str:
    return re.sub(r"\s+", " ", _blank(value))


def canonicalize_company_name(value: object) -> str:
    return _collapse(value)


def canonicalize_address(value: object) -> str:
    text = _collapse(value)
    if not text:
        return ""
    for pattern, repl in _STREET_ABBREV:
        text = pattern.sub(repl, text)
    return text


def canonicalize_city(value: object) -> str:
    return _collapse(value)


def canonicalize_state(value: object) -> str:
    text = _collapse(value)
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return text


def canonicalize_zip(value: object) -> str:
    text = _blank(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 9:
        return f"{digits[:5]}-{digits[5:]}"
    if len(digits) == 5:
        return digits
    return _collapse(value)


def canonicalize_website(value: object) -> str:
    text = _blank(value)
    if not text:
        return ""
    lowered = text.casefold()
    if lowered in {"n/a", "na", "none", "-", "(blank)"}:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    text = text.strip()
    if text.endswith("/") and text.count("/") == 3:
        text = text[:-1]
    return text


def canonicalize_phone_pair(phone: object, extension: object | None = None) -> tuple[str, str]:
    main, ext = stored_phone_pair(
        _blank(phone),
        None if extension is None else _blank(extension),
        field="legacy_phone",
    )
    if extension is not None:
        ext = digits_only_extension(extension)
    return main, ext


def canonicalize_amend_fields(fields: dict[str, Any], *, stored: dict[str, Any] | None = None) -> dict[str, str]:
    stored = stored or {}
    out: dict[str, str] = {}
    incoming_phone = fields["phone"] if "phone" in fields else stored.get("legacy_phone")
    incoming_ext = (
        fields["phone_extension"] if "phone_extension" in fields else stored.get("legacy_phone_extension")
    )
    if "phone" in fields or "phone_extension" in fields:
        main, ext = canonicalize_phone_pair(
            incoming_phone,
            incoming_ext if "phone_extension" in fields or incoming_ext is not None else None,
        )
        if "phone" in fields:
            out["phone"] = main
        if "phone_extension" in fields:
            out["phone_extension"] = ext
    for key, fn in (
        ("company_name", canonicalize_company_name),
        ("address", canonicalize_address),
        ("city", canonicalize_city),
        ("state", canonicalize_state),
        ("zip", canonicalize_zip),
        ("website", canonicalize_website),
    ):
        if key in fields:
            out[key] = fn(fields[key])
    return out


def _stored_view(stored: dict[str, Any]) -> dict[str, str]:
    return {
        "company_name": _blank(stored.get("company_name")),
        "address": _blank(stored.get("address")),
        "city": _blank(stored.get("city")),
        "state": _blank(stored.get("state")),
        "zip": _blank(stored.get("zip")),
        "website": _blank(stored.get("website")),
        "phone": _blank(stored.get("legacy_phone")),
        "phone_extension": _blank(stored.get("legacy_phone_extension")),
        "external_record_no": _blank(stored.get("external_record_no")),
        "updated_at": _blank(stored.get("last_updated_at") or stored.get("updated_at")),
    }


def _phone_equal(left: str, right: str) -> bool:
    a, b = digits_phone(left), digits_phone(right)
    if a and b:
        return a == b
    return _blank(left) == _blank(right)


def _field_equal(field: str, old: str, new: str) -> bool:
    if field == "phone":
        return _phone_equal(old, new)
    if field == "website":
        return (domain(old) and domain(old) == domain(new)) or old == new
    if field == "address":
        return (norm_addr(old) and norm_addr(old) == norm_addr(new)) or old == new
    if field == "company_name":
        return (norm_name(old) and norm_name(old) == norm_name(new)) or old == new
    return old == new


def proposed_changes(current: dict[str, str], canonical: dict[str, str]) -> list[dict[str, str]]:
    changes = []
    for field, new_val in canonical.items():
        old_val = current.get(field, "")
        if field == "phone" and _phone_equal(old_val, new_val):
            continue
        if old_val == new_val:
            continue
        changes.append({"field": field, "current": old_val, "proposed": new_val})
    return changes


def preview_fingerprint(
    *,
    company_id: int,
    updated_at: str,
    canonical: dict[str, str],
    reason: str,
) -> str:
    payload = json.dumps(
        {
            "company_id": int(company_id),
            "updated_at": _blank(updated_at),
            "canonical": canonical,
            "reason": _blank(reason),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def list_linked_clients(conn, company_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT cl.id AS client_id, cl.code, cl.name, ccr.status, ccr.id AS ccr_id
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        WHERE ccr.company_id = ?
          AND {sql_active_ccr(conn, "ccr")}
        ORDER BY cl.name, cl.id
        """,
        (int(company_id),),
    ).fetchall()
    return [
        {
            "client_id": int(r["client_id"]),
            "code": _blank(r["code"]),
            "name": _blank(r["name"]),
            "status": _blank(r["status"]),
            "ccr_id": int(r["ccr_id"]),
        }
        for r in rows
    ]


def _candidate(row: Any, reasons: list[str]) -> dict[str, Any]:
    return {
        "company_id": int(row["id"]),
        "company_name": _blank(row["company_name"]),
        "address": _blank(row["address"]),
        "city": _blank(row["city"]),
        "state": _blank(row["state"]),
        "zip": _blank(row["zip"]),
        "phone": _blank(row["legacy_phone"]),
        "website": _blank(row["website"]),
        "external_record_no": _blank(row["external_record_no"]),
        "reasons": reasons,
        "severity": "block" if any(r in HIGH_COLLISION_REASONS for r in reasons) else "warning",
    }


def find_amend_collisions(
    conn,
    *,
    company_id: int,
    proposed: dict[str, str],
) -> list[dict[str, Any]]:
    name = proposed.get("company_name", "")
    city = proposed.get("city", "")
    state = proposed.get("state", "")
    address = proposed.get("address", "")
    phone = proposed.get("phone", "")
    website = proposed.get("website", "")
    want_name = norm_name(name)
    want_addr = norm_addr(address) if address else ""
    want_phone = digits_phone(phone) if phone else ""
    want_domain = domain(website) if website else ""
    if not want_name and not want_phone and not want_domain and not want_addr:
        return []

    rows = conn.execute(
        """
        SELECT id, company_name, address, city, state, zip, website,
               legacy_phone, external_record_no
        FROM companies
        """
    ).fetchall()
    by_id = {int(r["id"]): r for r in rows}
    grouped: dict[int, list[str]] = {}

    def add(cid: int, reason: str) -> None:
        if cid == int(company_id):
            return
        grouped.setdefault(cid, [])
        if reason not in grouped[cid]:
            grouped[cid].append(reason)

    for row in rows:
        cid = int(row["id"])
        if cid == int(company_id):
            continue
        same_name = bool(want_name) and norm_name(row["company_name"] or "") == want_name
        same_city = (not _blank(city)) and _blank(row["city"]).casefold() == city.casefold()
        same_state = (not _blank(state)) and _blank(row["state"]).upper() == state.upper()
        same_addr = bool(want_addr) and norm_addr(row["address"] or "") == want_addr
        same_phone = bool(want_phone) and len(want_phone) == 10 and digits_phone(row["legacy_phone"] or "") == want_phone
        same_web = bool(want_domain) and domain(row["website"] or "") == want_domain
        if same_name and same_city and same_state:
            add(cid, "same_name+same_city_state")
        if same_name and same_addr:
            add(cid, "same_name+same_address")
        if same_name and same_phone:
            add(cid, "same_name+same_phone")
        if same_name and same_web:
            add(cid, "same_name+same_website")
        if same_name and not (same_city and same_state) and not same_addr and not same_phone and not same_web:
            add(cid, "name_only")

    if want_name and _table_exists(conn, "company_aliases"):
        for row in conn.execute(
            """
            SELECT company_id, alias_name, source_address, source_phone, source_website,
                   source_city, source_state
            FROM company_aliases
            WHERE alias_norm = ?
            """,
            (want_name,),
        ):
            cid = int(row["company_id"])
            same_city = (not _blank(city)) and _blank(row["source_city"]).casefold() == city.casefold()
            same_state = (not _blank(state)) and _blank(row["source_state"]).upper() == state.upper()
            same_addr = bool(want_addr) and norm_addr(row["source_address"] or "") == want_addr
            same_phone = bool(want_phone) and digits_phone(row["source_phone"] or "") == want_phone
            same_web = bool(want_domain) and domain(row["source_website"] or "") == want_domain
            if same_city and same_state:
                add(cid, "alias+same_city_state")
            elif same_addr:
                add(cid, "alias+same_address")
            elif same_phone:
                add(cid, "alias+same_phone")
            elif same_web:
                add(cid, "alias+same_website")
            else:
                add(cid, "alias_name_only")

    if want_name and _table_exists(conn, "company_source_identities"):
        for row in conn.execute(
            """
            SELECT company_id, source_company_name
            FROM company_source_identities
            WHERE TRIM(COALESCE(source_company_name,'')) != ''
            """
        ):
            cid = int(row["company_id"])
            if cid == int(company_id):
                continue
            if norm_name(row["source_company_name"] or "") == want_name:
                add(cid, "source_identity+same_name")

    out = []
    for cid, reasons in grouped.items():
        row = by_id.get(cid)
        if row is None:
            continue
        out.append(_candidate(row, reasons))
    out.sort(key=lambda item: (0 if item["severity"] == "block" else 1, item["company_name"], item["company_id"]))
    return out


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def search_master_companies(conn, q: str, *, limit: int = 40) -> list[dict[str, Any]]:
    query = _collapse(q)
    if not query:
        return []
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT id, company_name, address, city, state, zip, website,
               legacy_phone, legacy_phone_extension, external_record_no, last_updated_at
        FROM companies
        WHERE company_name LIKE ?
           OR CAST(id AS TEXT) = ?
           OR TRIM(COALESCE(external_record_no,'')) LIKE ?
        ORDER BY company_name, id
        LIMIT ?
        """,
        (like, query, like, int(limit)),
    ).fetchall()
    return [
        {
            "company_id": int(r["id"]),
            **_stored_view(dict(r)),
        }
        for r in rows
    ]


def load_master_company(conn, company_id: int) -> dict[str, Any]:
    stored = _load(conn, "companies", int(company_id))
    current = _stored_view(stored)
    return {
        "company_id": int(company_id),
        "archived": is_archived(conn, "companies", int(company_id)),
        **current,
        "linked_clients": list_linked_clients(conn, int(company_id)),
        "master_data_warning": (
            "This updates the shared Master Company record and may be visible across multiple clients."
        ),
    }


def _merged_proposed(current: dict[str, str], canonical: dict[str, str]) -> dict[str, str]:
    merged = dict(current)
    merged.update(canonical)
    return {
        key: merged.get(key, "")
        for key in COMPANY_AMEND_FIELDS
    }


def preview_company_amend(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    fields: dict[str, Any],
    reason: str = "",
) -> dict[str, Any]:
    _ = actor
    payload = load_master_company(conn, int(company_id))
    if payload["archived"]:
        raise StewardError("archived")
    stored = _load(conn, "companies", int(company_id))
    submitted = {key: fields[key] for key in COMPANY_AMEND_FIELDS if key in fields}
    canonical = canonicalize_amend_fields(submitted, stored=stored)
    current = _stored_view(stored)
    changes = proposed_changes(current, canonical)
    proposed = _merged_proposed(current, canonical)
    collisions = find_amend_collisions(conn, company_id=int(company_id), proposed=proposed)
    blocking = [c for c in collisions if c["severity"] == "block"]
    reason_text = _blank(reason)
    fingerprint = preview_fingerprint(
        company_id=int(company_id),
        updated_at=current["updated_at"],
        canonical=canonical,
        reason=reason_text,
    )
    return {
        "company_id": int(company_id),
        "current": current,
        "canonical": canonical,
        "changes": changes,
        "noop": len(changes) == 0,
        "reason": reason_text,
        "reason_ok": bool(reason_text),
        "linked_clients": payload["linked_clients"],
        "linked_client_count": len(payload["linked_clients"]),
        "master_data_warning": payload["master_data_warning"],
        "collisions": collisions,
        "blocked": bool(blocking),
        "preview_fingerprint": fingerprint,
        "expected_updated_at": current["updated_at"],
        "writes": False,
    }


def save_company_amend(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    fields: dict[str, Any],
    reason: str,
    expected_updated_at: str | None = None,
    preview_fingerprint_value: str = "",
) -> dict[str, Any]:
    if not actor.is_administrator:
        raise PermissionError("Administrator required.")
    reason_text = _blank(reason)
    if not reason_text:
        raise StewardError("reason_required")
    preview = preview_company_amend(
        conn, actor=actor, company_id=int(company_id), fields=fields, reason=reason_text
    )
    if preview["blocked"]:
        raise DuplicateCompanyError(preview["collisions"])
    if _has_text(preview_fingerprint_value) and preview["preview_fingerprint"] != _blank(
        preview_fingerprint_value
    ):
        raise StewardError("stale_preview")
    if expected_updated_at is not None and _blank(expected_updated_at) != preview["expected_updated_at"]:
        raise StewardError("stale_edit")
    if preview["noop"]:
        return {
            "company_id": int(company_id),
            "changed": [],
            "updated_at": preview["expected_updated_at"],
            "noop": True,
            "provenance": [],
            "source_type": SOURCE_MANUAL_ADMIN,
        }
    changed_fields = {
        row["field"]: preview["canonical"][row["field"]]
        for row in preview["changes"]
        if row.get("field") in preview["canonical"]
    }
    result = amend_company(
        conn,
        actor=actor,
        company_id=int(company_id),
        fields=changed_fields,
        expected_updated_at=preview["expected_updated_at"],
        reason=reason_text,
    )
    history = provenance_history(
        conn,
        entity_type="company",
        entity_id=int(company_id),
        limit=50,
    )
    changed_set = set(result.get("changed") or [])
    events = [row for row in history if _blank(row.get("action")) == "AMEND" and row.get("field") in changed_set]
    return {
        "company_id": int(result["company_id"]),
        "changed": result.get("changed") or [],
        "updated_at": result.get("updated_at") or "",
        "noop": False,
        "source_type": SOURCE_MANUAL_ADMIN,
        "actor_id": int(actor.id),
        "actor_name": _blank(actor.full_name),
        "reason": reason_text,
        "provenance": events[: len(changed_set) + 2],
    }
