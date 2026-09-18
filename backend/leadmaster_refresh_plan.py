"""LeadMaster incremental refresh planner.

Read-only dry-run for an EXISTING NorthStar client. Does not write master data.
Reuses RN/alias matching rules, status/notes helpers, history hashes, and
resolve_company_id() redirects. This is a refresh mode on top of the CRM
import family — not a second matcher.

Live Carmeco/Brown/Dawson must not call apply.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from client_data_history_notes import (
    compute_history_event_hash,
    is_routine_marketing_send_note,
)
from company_merges import resolve_company_id
from contact_phone import canonical_contact_phone
from crm_import_status_notes import (
    load_client_status_catalog,
    normalize_notes_compare,
    normalize_status_key,
    notes_block_already_present,
)
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from shared_note_history_import import normalize_record_no

PLANNER_VERSION = "leadmaster-refresh-plan-v2"
SOURCE_SYSTEM = "leadmaster"

UNCHANGED = "UNCHANGED"
NEW_COMPANY = "NEW_COMPANY"
NEW_RELATIONSHIP = "NEW_RELATIONSHIP"
NEW_CONTACT = "NEW_CONTACT"
UPDATED_COMPANY = "UPDATED_COMPANY"
UPDATED_CONTACT = "UPDATED_CONTACT"
STATUS_CHANGE = "STATUS_CHANGE"
NEW_NOTE = "NEW_NOTE"
NEW_HISTORY = "NEW_HISTORY"
ASSIGNMENT_CHANGE = "ASSIGNMENT_CHANGE"
CAMPAIGN_CHANGE = "CAMPAIGN_CHANGE"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
SOURCE_ID_CONFLICT = "SOURCE_ID_CONFLICT"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
SKIPPED_INVALID = "SKIPPED_INVALID"
MANUAL_OVERRIDE_CONFLICT = "MANUAL_OVERRIDE_CONFLICT"
ARCHIVED_MATCH_REVIEW = "ARCHIVED_MATCH_REVIEW"
RESTORE_RELATIONSHIP_REVIEW = "RESTORE_RELATIONSHIP_REVIEW"

NO_CHANGE = "NO_CHANGE"
SAFE_CANONICAL_UPDATE = "SAFE_CANONICAL_UPDATE"
CLIENT_SOURCE_DIFFERENCE = "CLIENT_SOURCE_DIFFERENCE"
LOCATION_DIFFERENCE = "LOCATION_DIFFERENCE"
PROVENANCE_ONLY = "PROVENANCE_ONLY"
SAFE_UPDATE = "SAFE_UPDATE"
ADDITIVE_UPDATE = "ADDITIVE_UPDATE"
CONFLICT = "CONFLICT"

UNCHANGED_NOTE = "UNCHANGED_NOTE"
DUPLICATE_NOTE = "DUPLICATE_NOTE"
EXCLUDED_NOTE = "EXCLUDED_NOTE"
REVIEW_NOTE = "REVIEW_NOTE"
UNCHANGED_HISTORY = "UNCHANGED_HISTORY"
DUPLICATE_HISTORY = "DUPLICATE_HISTORY"
CONFLICTING_SOURCE_HISTORY = "CONFLICTING_SOURCE_HISTORY"
REVIEW_HISTORY = "REVIEW_HISTORY"
UNCHANGED_ASSIGNMENT = "UNCHANGED_ASSIGNMENT"
UNKNOWN_SOURCE_REP = "UNKNOWN_SOURCE_REP"
REVIEW_ASSIGNMENT = "REVIEW_ASSIGNMENT"
INACTIVE_TARGET = "INACTIVE_TARGET"
AMBIGUOUS_ASSIGNMENT = "AMBIGUOUS"
IGNORED_ASSIGNMENT = "IGNORED"

CAMPAIGN_ACTIVE = "ACTIVE_OPERATIONAL"
CAMPAIGN_HISTORICAL = "HISTORICAL_PROVENANCE"
CAMPAIGN_SYSTEM = "SYSTEM_TEST_ADMIN"
CAMPAIGN_UNKNOWN = "UNKNOWN_REVIEW"
CAMPAIGN_IGNORED = "IGNORED"

STATUS_PRESERVE = "preserve"
STATUS_PROPOSE = "propose_change"
STATUS_AUTHORITATIVE = "authoritative"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def refresh_policy_from_profile(profile) -> RefreshPolicy:
    """Adapt the Phase 2 frozen profile onto the planner policy object."""
    from leadmaster_refresh_policy import planner_status_mode

    profile.normalized()
    states = {
        _norm_person(e.source_rep): e.state
        for e in profile.assignment_maps
        if _blank(e.source_rep)
    }
    return RefreshPolicy(
        client_id=int(profile.client_id),
        status_mode=planner_status_mode(profile.status_mode),
        assignment_user_map=profile.assignment_user_map(),
        assignment_state_map=states,
        campaign_class_map=profile.campaign_class_map(),
        campaign_target_map=profile.campaign_target_map(),
        allow_additive_contact_fields=bool(profile.allow_additive_contact_fields),
        allow_safe_canonical_company_fill=bool(
            profile.allow_safe_canonical_company_fill
        ),
        source_system=profile.source_system,
        company_field_mode=profile.company_field_mode,
        contact_field_mode=profile.contact_field_mode,
        notes_mode=profile.notes_mode,
        history_mode=profile.history_mode,
    )


def _norm_person(value: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", _blank(value).lower()).split())


def _norm_email(value: str) -> str:
    return _blank(value).lower()


def _nanp(value: str) -> str:
    nanp10, _last = canonical_contact_phone(value)
    return _blank(nanp10)


def _last7(value: str) -> str:
    digits = re.sub(r"\D", "", _blank(value))
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-7:] if len(digits) >= 7 else ""


@dataclass
class RefreshPolicy:
    client_id: int
    status_mode: str = STATUS_PROPOSE
    assignment_user_map: dict[str, int] = field(default_factory=dict)
    assignment_state_map: dict[str, str] = field(default_factory=dict)
    campaign_class_map: dict[str, str] = field(default_factory=dict)
    campaign_target_map: dict[str, int] = field(default_factory=dict)
    allow_additive_contact_fields: bool = True
    allow_safe_canonical_company_fill: bool = True
    source_system: str = "LEADMASTER"
    company_field_mode: str = "FILL_BLANK_SAFE_FIELDS"
    contact_field_mode: str = "FILL_BLANK_SAFE_FIELDS"
    notes_mode: str = "APPEND_DISTINCT"
    history_mode: str = "APPEND_DISTINCT_BY_SOURCE_ID_OR_FINGERPRINT"
    mapping: dict[str, str] = field(default_factory=dict)

    def fingerprint_slice(self) -> dict[str, Any]:
        return {
            "client_id": int(self.client_id),
            "source_system": self.source_system,
            "status_mode": self.status_mode,
            "company_field_mode": self.company_field_mode,
            "contact_field_mode": self.contact_field_mode,
            "notes_mode": self.notes_mode,
            "history_mode": self.history_mode,
            "assignment_user_map": {
                k: int(v) for k, v in sorted(self.assignment_user_map.items())
            },
            "assignment_state_map": dict(sorted(self.assignment_state_map.items())),
            "campaign_class_map": dict(sorted(self.campaign_class_map.items())),
            "campaign_target_map": {
                k: int(v) for k, v in sorted(self.campaign_target_map.items())
            },
            "allow_additive_contact_fields": bool(self.allow_additive_contact_fields),
            "allow_safe_canonical_company_fill": bool(
                self.allow_safe_canonical_company_fill
            ),
            "mapping": dict(sorted(self.mapping.items())),
        }


@dataclass
class IncomingHistory:
    source_note_id: str = ""
    event_at: str = ""
    author: str = ""
    event_type: str = ""
    note_text: str = ""


@dataclass
class IncomingRow:
    source_row: int
    record_no: str = ""
    company_name: str = ""
    address: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    country: str = ""
    website: str = ""
    company_phone: str = ""
    company_phone_ext: str = ""
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    email: str = ""
    contact_phone: str = ""
    contact_phone_ext: str = ""
    alt_phone: str = ""
    alt_phone_ext: str = ""
    status: str = ""
    notes: str = ""
    assigned_rep: str = ""
    campaign: str = ""
    history: list[IncomingHistory] = field(default_factory=list)


@dataclass
class FieldProposal:
    kind: str
    field: str
    old_value: str
    new_value: str
    action: str
    confidence: str
    blocking: bool = False
    review_reason: str = ""
    policy: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class RowPlan:
    source_row: int
    record_no: str
    classifications: list[str]
    company_id: int | None = None
    ccr_id: int | None = None
    contact_id: int | None = None
    match_evidence: list[str] = field(default_factory=list)
    proposals: list[FieldProposal] = field(default_factory=list)
    blocking: bool = False
    review: bool = False


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        is not None
    )


def _cols(conn, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint_refresh_plan(plan: dict[str, Any]) -> str:
    body = {
        "schema": PLANNER_VERSION,
        "client_id": plan.get("client_id"),
        "policy": plan.get("policy"),
        "source_sha256": plan.get("source_sha256"),
        "source_filename": plan.get("source_filename"),
        "rows": plan.get("rows"),
        "resolutions": plan.get("resolutions") or [],
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _lookup_source_identity(conn, *, client_id: int, record_no: str) -> dict | None:
    if not record_no or not _table_exists(conn, "company_source_identities"):
        return None
    row = conn.execute(
        """
        SELECT id, company_id, location_id, source_record_no
        FROM company_source_identities
        WHERE source_system = ?
          AND source_record_no = ?
          AND COALESCE(client_id, 0) = COALESCE(?, 0)
        LIMIT 1
        """,
        (SOURCE_SYSTEM, record_no, client_id),
    ).fetchone()
    return dict(row) if row is not None else None


def _company_by_master_rn(conn, record_no: str) -> dict | None:
    if not record_no:
        return None
    row = conn.execute(
        """
        SELECT id, company_name, address, city, state, zip, website,
               COALESCE(legacy_phone,'') AS legacy_phone,
               COALESCE(external_record_no,'') AS external_record_no
        FROM companies WHERE TRIM(COALESCE(external_record_no,'')) = ?
        LIMIT 2
        """,
        (record_no,),
    ).fetchall()
    if len(row) > 1:
        return {"_conflict": True, "ids": [int(r["id"]) for r in row]}
    if not row:
        return None
    return dict(row[0])


def _ccr_by_rn(conn, *, client_id: int, record_no: str) -> dict | None:
    if not record_no:
        return None
    row = conn.execute(
        """
        SELECT id, company_id, external_record_no, status, notes, assigned_user_id
        FROM client_company_relationships
        WHERE client_id = ? AND TRIM(COALESCE(external_record_no,'')) = ?
        LIMIT 2
        """,
        (client_id, record_no),
    ).fetchall()
    if len(row) > 1:
        return {"_conflict": True, "ids": [int(r["id"]) for r in row]}
    if not row:
        return None
    return dict(row[0])


def _alias_company_ids(conn, record_no: str) -> list[int]:
    if not record_no or not _table_exists(conn, "company_aliases"):
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT company_id FROM company_aliases
        WHERE TRIM(COALESCE(source_record_no,'')) = ?
        """,
        (record_no,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _load_company(conn, company_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, company_name, address, city, state, zip, website,
               COALESCE(legacy_phone,'') AS legacy_phone,
               COALESCE(external_record_no,'') AS external_record_no
        FROM companies WHERE id = ?
        """,
        (int(company_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def _load_ccr(conn, *, client_id: int, company_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, company_id, external_record_no, status, notes, assigned_user_id
        FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (client_id, company_id),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    from data_steward import is_archived

    if is_archived(conn, "client_company_relationships", int(data["id"])):
        data["_archived"] = True
    return data


def _name_corroborated_other(conn, incoming: IncomingRow, matched_id: int) -> int | None:
    """If name+city+state uniquely match a different stored company, return that id."""
    nn = norm_name(incoming.company_name)
    city = _blank(incoming.city).lower()
    state = _blank(incoming.state).upper()
    if not nn or not city or not state:
        return None
    rows = conn.execute(
        """
        SELECT id, company_name, city, state FROM companies
        WHERE id != ?
        """,
        (matched_id,),
    ).fetchall()
    hits = []
    for r in rows:
        if norm_name(r["company_name"]) == nn and _blank(r["city"]).lower() == city:
            stored_state = _blank(r["state"]).upper()
            if stored_state == state:
                hits.append(int(r["id"]))
    if len(hits) == 1:
        return hits[0]
    return None


def _finish_company_match(
    conn, cid: int | None, evidence: list[str], conflict: str | None
) -> tuple[int | None, list[str], str | None]:
    if cid and conflict not in {SOURCE_ID_CONFLICT}:
        from data_steward import is_archived

        if is_archived(conn, "companies", int(cid)):
            return cid, list(evidence) + ["archived"], ARCHIVED_MATCH_REVIEW
    return cid, evidence, conflict


def _finish_contact_match(
    conn, cid: int | None, evidence: list[str], conflict: str | None
) -> tuple[int | None, list[str], str | None]:
    if cid:
        from data_steward import is_archived

        if is_archived(conn, "contacts", int(cid)):
            return cid, list(evidence) + ["archived"], ARCHIVED_MATCH_REVIEW
    return cid, evidence, conflict


def match_company_for_refresh(
    conn, *, client_id: int, incoming: IncomingRow
) -> tuple[int | None, list[str], str | None]:
    """Return (company_id, evidence, conflict_reason). Never name-only auto-match."""
    rn = normalize_record_no(incoming.record_no)
    evidence: list[str] = []
    if not rn:
        if incoming.company_name:
            return None, ["no_record_no"], REVIEW_REQUIRED
        return None, ["blank_row"], SKIPPED_INVALID

    ident = _lookup_source_identity(conn, client_id=client_id, record_no=rn)
    if ident is not None:
        cid = resolve_company_id(conn, int(ident["company_id"]))
        evidence.append("source_identity")
        other = _name_corroborated_other(conn, incoming, cid)
        if other is not None and other != cid:
            return cid, evidence, SOURCE_ID_CONFLICT
        return _finish_company_match(conn, cid, evidence, None)

    master = _company_by_master_rn(conn, rn)
    if master and master.get("_conflict"):
        return None, ["master_rn_duplicate"], SOURCE_ID_CONFLICT
    if master:
        cid = resolve_company_id(conn, int(master["id"]))
        evidence.append("master_rn")
        other = _name_corroborated_other(conn, incoming, cid)
        if other is not None and other != cid:
            return cid, evidence, SOURCE_ID_CONFLICT
        return _finish_company_match(conn, cid, evidence, None)

    ccr = _ccr_by_rn(conn, client_id=client_id, record_no=rn)
    if ccr and ccr.get("_conflict"):
        return None, ["ccr_rn_duplicate"], SOURCE_ID_CONFLICT
    if ccr:
        cid = resolve_company_id(conn, int(ccr["company_id"]))
        evidence.append("ccr_rn")
        return _finish_company_match(conn, cid, evidence, None)

    aliases = _alias_company_ids(conn, rn)
    resolved = sorted({resolve_company_id(conn, i) for i in aliases})
    if len(resolved) > 1:
        return None, ["alias_rn_ambiguous"], SOURCE_ID_CONFLICT
    if len(resolved) == 1:
        return _finish_company_match(conn, resolved[0], ["alias_rn"], None)

    # Redirected numeric company id (Ronson 484 -> 425) when export still cites old id.
    if rn.isdigit():
        try:
            walked = resolve_company_id(conn, int(rn))
        except Exception:
            walked = int(rn)
        if walked != int(rn) and _load_company(conn, walked) is not None:
            return _finish_company_match(conn, walked, ["merge_redirect"], None)

    other = _name_corroborated_other(conn, incoming, 0)
    if other is not None:
        return other, ["name_city_state_unique"], REVIEW_REQUIRED

    return None, ["unmatched_rn"], None


def _tag_manual_overrides(conn, entity_type: str, entity_id: int | None, proposals: list[FieldProposal]) -> None:
    if conn is None or not entity_id:
        return
    from data_steward import is_manual_authority, provenance_evidence

    skip = {
        NO_CHANGE, PROVENANCE_ONLY, UNCHANGED_NOTE, DUPLICATE_NOTE, EXCLUDED_NOTE,
        DUPLICATE_HISTORY, UNCHANGED_ASSIGNMENT, SAFE_CANONICAL_UPDATE, ADDITIVE_UPDATE,
    }
    for prop in proposals:
        if prop.action in skip:
            continue
        if not is_manual_authority(
            conn, entity_type=entity_type, entity_id=int(entity_id), field=prop.field
        ):
            continue
        if _blank(prop.old_value) == _blank(prop.new_value) or not _blank(prop.new_value):
            continue
        prop.action = MANUAL_OVERRIDE_CONFLICT
        prop.blocking = True
        prop.review_reason = "manual_override"
        prop.policy = "manual_correction_requires_review"
        prop.evidence = list(prop.evidence) + provenance_evidence(
            conn, entity_type=entity_type, entity_id=int(entity_id), field=prop.field
        )


def _company_field_proposals(
    stored: dict, incoming: IncomingRow, policy: RefreshPolicy,
    conn=None, company_id: int | None = None,
) -> list[FieldProposal]:
    mapping = [
        ("company_name", stored.get("company_name"), incoming.company_name, True),
        ("address", stored.get("address"), incoming.address, False),
        ("city", stored.get("city"), incoming.city, False),
        ("state", stored.get("state"), incoming.state, False),
        ("zip", stored.get("zip"), incoming.zip, False),
        ("website", stored.get("website"), incoming.website, False),
        ("phone", stored.get("legacy_phone"), incoming.company_phone, False),
        ("address2", "", incoming.address2, False),
        ("country", "", incoming.country, False),
    ]
    out: list[FieldProposal] = []
    addr_changed = False
    loc_changed = False
    for field, old, new, is_name in mapping:
        old_b, new_b = _blank(old), _blank(new)
        if not new_b or new_b == old_b:
            continue
        if is_name and norm_name(old_b) == norm_name(new_b):
            out.append(
                FieldProposal(
                    UPDATED_COMPANY, field, old_b, new_b, PROVENANCE_ONLY, "high",
                    policy="keep_canonical_name",
                )
            )
            continue
        if field == "address" and norm_addr(old_b) == norm_addr(new_b):
            continue
        if field == "website" and domain(old_b) and domain(old_b) == domain(new_b):
            continue
        if field == "phone":
            if digits_phone(old_b) == digits_phone(new_b) and digits_phone(new_b):
                continue
        if field in {"address2", "country"}:
            out.append(
                FieldProposal(
                    UPDATED_COMPANY, field, old_b, new_b, PROVENANCE_ONLY, "medium",
                    policy="preserve_populated_master",
                )
            )
            continue
        if not old_b and policy.allow_safe_canonical_company_fill:
            out.append(
                FieldProposal(
                    UPDATED_COMPANY, field, old_b, new_b, SAFE_CANONICAL_UPDATE, "high",
                    policy="fill_blank_master",
                )
            )
            continue
        if field == "address":
            addr_changed = True
        if field in {"city", "state", "zip"}:
            loc_changed = True
        action = LOCATION_DIFFERENCE if field in {"address", "city", "state", "zip"} else CLIENT_SOURCE_DIFFERENCE
        out.append(
            FieldProposal(
                UPDATED_COMPANY, field, old_b, new_b, action, "medium",
                blocking=False, review_reason="master_populated_differs",
                policy="do_not_silent_overwrite",
            )
        )
    if addr_changed and loc_changed:
        for p in out:
            if p.field in {"address", "city", "state", "zip"}:
                p.action = LOCATION_DIFFERENCE
                p.review_reason = "possible_separate_location"
    _tag_manual_overrides(conn, "company", company_id, out)
    return out


def _match_contact(conn, *, company_id: int, incoming: IncomingRow) -> tuple[int | None, list[str], str | None]:
    email = _norm_email(incoming.email)
    phone10 = _nanp(incoming.contact_phone)
    last7 = _last7(incoming.contact_phone)
    person = _norm_person(f"{incoming.first_name} {incoming.last_name}")
    contacts = conn.execute(
        """
        SELECT id, first_name, last_name, email, phone, alt_phone, title, company_id
        FROM contacts WHERE company_id = ?
        """,
        (company_id,),
    ).fetchall()
    email_hits = []
    phone_hits = []
    last7_hits = []
    name_hits = []
    for c in contacts:
        cid = int(c["id"])
        if email and _norm_email(c["email"]) == email:
            email_hits.append(cid)
        if phone10:
            stored10 = _nanp(c["phone"]) or _nanp(c["alt_phone"] or "")
            if stored10 == phone10:
                phone_hits.append(cid)
        if last7 and not phone10:
            stored7 = _last7(c["phone"]) or _last7(c["alt_phone"] or "")
            if stored7 == last7:
                last7_hits.append(cid)
        stored_person = _norm_person(f"{c['first_name']} {c['last_name']}")
        if person and stored_person == person:
            name_hits.append(cid)
    if len(email_hits) == 1:
        return _finish_contact_match(conn, email_hits[0], ["email_exact"], None)
    if len(email_hits) > 1:
        return None, ["email_ambiguous"], POSSIBLE_DUPLICATE
    if len(phone_hits) == 1:
        return _finish_contact_match(conn, phone_hits[0], ["phone_nanp10"], None)
    if len(phone_hits) > 1:
        return None, ["phone_ambiguous"], POSSIBLE_DUPLICATE
    if last7_hits:
        cid = last7_hits[0] if len(last7_hits) == 1 else None
        return _finish_contact_match(conn, cid, ["phone_last7"], POSSIBLE_DUPLICATE)
    if len(name_hits) == 1:
        return _finish_contact_match(conn, name_hits[0], ["name_at_company"], None)
    if len(name_hits) > 1:
        return None, ["name_ambiguous"], POSSIBLE_DUPLICATE
    if incoming.first_name or incoming.last_name or email:
        return None, ["no_contact_match"], None
    return None, ["no_contact_data"], None


def _load_contact(conn, contact_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, first_name, last_name, email, phone, alt_phone, title
        FROM contacts WHERE id = ?
        """,
        (contact_id,),
    ).fetchone()
    return dict(row)


def _contact_field_proposals(
    stored: dict, incoming: IncomingRow, policy: RefreshPolicy,
    conn=None, contact_id: int | None = None,
) -> list[FieldProposal]:
    pairs = [
        ("first_name", stored.get("first_name"), incoming.first_name),
        ("last_name", stored.get("last_name"), incoming.last_name),
        ("title", stored.get("title"), incoming.title),
        ("email", stored.get("email"), incoming.email),
        ("phone", stored.get("phone"), incoming.contact_phone),
        ("alt_phone", stored.get("alt_phone"), incoming.alt_phone),
    ]
    out: list[FieldProposal] = []
    for field, old, new in pairs:
        old_b, new_b = _blank(old), _blank(new)
        if not new_b:
            continue
        if field == "email" and _norm_email(old_b) == _norm_email(new_b) and new_b:
            continue
        if field in {"phone", "alt_phone"}:
            if _nanp(old_b) and _nanp(old_b) == _nanp(new_b):
                continue
        if old_b == new_b:
            continue
        if not old_b and policy.allow_additive_contact_fields:
            out.append(
                FieldProposal(
                    UPDATED_CONTACT, field, old_b, new_b, ADDITIVE_UPDATE, "high",
                    policy="fill_blank_contact",
                )
            )
            continue
        if field == "email" and old_b and new_b and _norm_email(old_b) != _norm_email(new_b):
            out.append(
                FieldProposal(
                    UPDATED_CONTACT, field, old_b, new_b, CONFLICT, "high",
                    blocking=True, review_reason="email_conflict",
                    policy="never_silent_replace_email",
                )
            )
            continue
        out.append(
            FieldProposal(
                UPDATED_CONTACT, field, old_b, new_b, CONFLICT, "medium",
                review_reason="populated_field_differs",
                policy="review_before_overwrite",
            )
        )
    _tag_manual_overrides(conn, "contact", contact_id, out)
    return out


def _status_proposal(
    ccr: dict | None, incoming: IncomingRow, policy: RefreshPolicy, catalog, conn=None
) -> FieldProposal | None:
    incoming_status = _blank(incoming.status)
    existing = _blank(ccr["status"]) if ccr else ""
    if not incoming_status:
        return FieldProposal(
            STATUS_CHANGE, "status", existing, existing, NO_CHANGE, "high",
            policy="blank_preserves_existing",
        )
    resolved, error = catalog.resolve(incoming_status)
    if error or not resolved:
        return FieldProposal(
            SKIPPED_INVALID, "status", existing, incoming_status, SKIPPED_INVALID, "high",
            blocking=False, review_reason=error or "invalid_status",
            policy=policy.status_mode,
        )
    if normalize_status_key(resolved) == normalize_status_key(existing):
        return FieldProposal(
            STATUS_CHANGE, "status", existing, existing, NO_CHANGE, "high",
            policy=policy.status_mode,
        )
    blocking = policy.status_mode == STATUS_PRESERVE
    prop = FieldProposal(
        STATUS_CHANGE, "status", existing, resolved, STATUS_CHANGE, "high",
        blocking=blocking,
        review_reason="" if policy.status_mode == STATUS_AUTHORITATIVE else "status_differs",
        policy=policy.status_mode,
    )
    ccr_id = int(ccr["id"]) if ccr and ccr.get("id") else None
    _tag_manual_overrides(conn, "ccr", ccr_id, [prop])
    return prop


def _notes_proposal(ccr: dict | None, incoming: IncomingRow) -> FieldProposal | None:
    text = _blank(incoming.notes)
    existing = _blank(ccr["notes"]) if ccr else ""
    if not text:
        return FieldProposal(NEW_NOTE, "notes", existing, "", UNCHANGED_NOTE, "high")
    if is_routine_marketing_send_note(text):
        return FieldProposal(NEW_NOTE, "notes", existing, text, EXCLUDED_NOTE, "high", policy="marketing_exclusion")
    if notes_block_already_present(existing, text):
        return FieldProposal(NEW_NOTE, "notes", existing, text, DUPLICATE_NOTE, "high")
    return FieldProposal(NEW_NOTE, "notes", existing, text, NEW_NOTE, "high", policy="append_distinct")


def _history_proposals(conn, *, client_id: int, company_id: int | None, incoming: IncomingRow) -> list[FieldProposal]:
    out: list[FieldProposal] = []
    if not incoming.history:
        return out
    existing_hashes: set[str] = set()
    other_company_hashes: set[str] = set()
    if _table_exists(conn, "company_shared_history_events"):
        rows = conn.execute(
            "SELECT company_id, event_hash FROM company_shared_history_events"
        ).fetchall()
        for r in rows:
            h = _blank(r["event_hash"] if hasattr(r, "keys") else r[1])
            if not h:
                continue
            cid = int(r["company_id"] if hasattr(r, "keys") else r[0])
            if company_id and cid == int(company_id):
                existing_hashes.add(h)
            else:
                other_company_hashes.add(h)
    seen: set[str] = set()
    rn = normalize_record_no(incoming.record_no)
    for ev in incoming.history:
        # Prefer source_note_id when present so hashes stay stable after a
        # NEW_COMPANY apply assigns a company_id. Fallback fingerprint uses RN.
        eh = compute_history_event_hash(
            client_id=client_id,
            company_id=None,
            company_record_no=rn,
            note_text=ev.note_text,
            event_at=ev.event_at,
            source_note_id=ev.source_note_id,
        )
        if eh in existing_hashes or eh in seen:
            out.append(
                FieldProposal(
                    NEW_HISTORY, "history", eh, eh, DUPLICATE_HISTORY, "high",
                    policy="event_hash",
                )
            )
        elif eh in other_company_hashes:
            out.append(
                FieldProposal(
                    NEW_HISTORY, "history", eh, eh, CONFLICTING_SOURCE_HISTORY, "high",
                    blocking=True, review_reason="history_hash_on_other_company",
                    policy="event_hash",
                )
            )
        else:
            out.append(
                FieldProposal(
                    NEW_HISTORY, "history", "", eh, NEW_HISTORY, "high",
                    policy="event_hash",
                )
            )
            seen.add(eh)
    return out


def _assignment_proposal(ccr: dict | None, incoming: IncomingRow, policy: RefreshPolicy) -> FieldProposal | None:
    rep = _blank(incoming.assigned_rep)
    if not rep:
        return None
    key = _norm_person(rep)
    mapped = {_norm_person(k): int(v) for k, v in policy.assignment_user_map.items()}
    states = {_norm_person(k): v for k, v in policy.assignment_state_map.items()}
    state = states.get(key, "")
    existing = ccr["assigned_user_id"] if ccr else None
    existing_i = int(existing) if existing not in (None, "") else None
    evidence = [f"source_rep={rep}"]
    if state == IGNORED_ASSIGNMENT or state == "IGNORED":
        return FieldProposal(
            ASSIGNMENT_CHANGE, "assigned_user_id", str(existing_i or ""), rep,
            IGNORED_ASSIGNMENT, "high", policy="explicit_map_ignored",
            evidence=evidence,
        )
    if state in {AMBIGUOUS_ASSIGNMENT, "AMBIGUOUS"}:
        return FieldProposal(
            ASSIGNMENT_CHANGE, "assigned_user_id", str(existing_i or ""), rep,
            AMBIGUOUS_ASSIGNMENT, "high", review_reason="ambiguous_source_rep",
            policy="never_fuzzy_name", evidence=evidence,
        )
    if state in {INACTIVE_TARGET, "INACTIVE_TARGET"}:
        mapped_id = mapped.get(key)
        return FieldProposal(
            ASSIGNMENT_CHANGE, "assigned_user_id", str(existing_i or ""),
            str(mapped_id or ""), INACTIVE_TARGET, "high",
            review_reason="inactive_northstar_user", policy="never_fuzzy_name",
            evidence=evidence + [f"mapped_user_id={mapped_id or ''}"],
        )
    if key not in mapped or state in {"UNMAPPED", UNKNOWN_SOURCE_REP}:
        return FieldProposal(
            ASSIGNMENT_CHANGE, "assigned_user_id", str(existing_i or ""), rep,
            UNKNOWN_SOURCE_REP, "high", review_reason="no_explicit_user_map",
            policy="never_fuzzy_name", evidence=evidence,
        )
    new_id = mapped[key]
    evidence.append(f"mapped_user_id={new_id}")
    if existing_i == new_id:
        return FieldProposal(
            ASSIGNMENT_CHANGE, "assigned_user_id", str(existing_i), str(new_id),
            UNCHANGED_ASSIGNMENT, "high", policy="explicit_map",
            evidence=evidence,
        )
    return FieldProposal(
        ASSIGNMENT_CHANGE, "assigned_user_id", str(existing_i or ""), str(new_id),
        ASSIGNMENT_CHANGE, "high", policy="explicit_map",
        evidence=evidence,
    )


def _campaign_proposal(conn, *, client_id: int, company_id: int | None, incoming: IncomingRow, policy: RefreshPolicy) -> FieldProposal | None:
    name = _blank(incoming.campaign)
    if not name:
        return None
    classified = policy.campaign_class_map.get(name) or policy.campaign_class_map.get(name.casefold())
    if not classified:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", "", name, CAMPAIGN_UNKNOWN, "high",
            review_reason="unclassified_leadmaster_campaign", policy="no_auto_create",
        )
    if classified == CAMPAIGN_IGNORED:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", "", name, CAMPAIGN_IGNORED, "high",
            policy="ignored",
        )
    if classified == CAMPAIGN_HISTORICAL:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", "", name, CAMPAIGN_HISTORICAL, "high",
            policy="provenance_only",
        )
    if classified == CAMPAIGN_SYSTEM:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", "", name, CAMPAIGN_SYSTEM, "high",
            policy="do_not_clutter",
        )
    if classified != CAMPAIGN_ACTIVE:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", "", name, CAMPAIGN_UNKNOWN, "high",
            review_reason="unknown_class",
        )
    target_id = policy.campaign_target_map.get(name) or policy.campaign_target_map.get(name.casefold())
    if not target_id:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", "", name, CAMPAIGN_UNKNOWN, "high",
            review_reason="active_without_northstar_campaign_id",
            policy="no_auto_create",
        )
    if company_id is None:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", "", str(target_id), CAMPAIGN_CHANGE, "high",
            policy="active_after_company_create",
        )
    row = conn.execute(
        """
        SELECT cc.id FROM campaign_companies cc
        WHERE cc.campaign_id = ? AND cc.company_id = ?
        LIMIT 1
        """,
        (int(target_id), company_id),
    ).fetchone() if _table_exists(conn, "campaign_companies") else None
    if row is not None:
        return FieldProposal(
            CAMPAIGN_CHANGE, "campaign", name, str(target_id), NO_CHANGE, "high",
            policy="already_member",
        )
    return FieldProposal(
        CAMPAIGN_CHANGE, "campaign", "", str(target_id), CAMPAIGN_CHANGE, "high",
        policy="active_operational",
    )


def _row_to_dict(row: RowPlan) -> dict[str, Any]:
    return {
        "source_row": row.source_row,
        "record_no": row.record_no,
        "classifications": list(row.classifications),
        "company_id": row.company_id,
        "ccr_id": row.ccr_id,
        "contact_id": row.contact_id,
        "match_evidence": list(row.match_evidence),
        "blocking": row.blocking,
        "review": row.review,
        "proposals": [asdict(p) for p in row.proposals],
    }


def plan_leadmaster_refresh(
    conn,
    *,
    client_id: int,
    rows: list[IncomingRow],
    policy: RefreshPolicy | None = None,
    source_sha256: str = "",
    source_filename: str = "",
) -> dict[str, Any]:
    """Zero-write refresh plan. Same source + DB + policy => same fingerprint."""
    pol = policy or RefreshPolicy(client_id=client_id)
    catalog = load_client_status_catalog(conn, client_id)
    planned: list[RowPlan] = []
    for incoming in rows:
        rp = RowPlan(
            source_row=int(incoming.source_row),
            record_no=normalize_record_no(incoming.record_no),
            classifications=[],
        )
        cid, evidence, conflict = match_company_for_refresh(
            conn, client_id=client_id, incoming=incoming
        )
        rp.match_evidence = evidence
        if conflict == SOURCE_ID_CONFLICT:
            rp.classifications.append(SOURCE_ID_CONFLICT)
            rp.company_id = cid
            rp.blocking = True
            rp.review = True
            rp.proposals.append(
                FieldProposal(
                    SOURCE_ID_CONFLICT, "record_no", str(cid or ""), incoming.record_no,
                    SOURCE_ID_CONFLICT, "high", blocking=True,
                    review_reason="source_identity_conflict", evidence=evidence,
                )
            )
            planned.append(rp)
            continue
        if conflict == ARCHIVED_MATCH_REVIEW:
            rp.classifications.append(ARCHIVED_MATCH_REVIEW)
            rp.company_id = cid
            rp.blocking = True
            rp.review = True
            rp.proposals.append(
                FieldProposal(
                    ARCHIVED_MATCH_REVIEW, "record_no", str(cid or ""), incoming.record_no,
                    ARCHIVED_MATCH_REVIEW, "high", blocking=True,
                    review_reason="archived_match", evidence=evidence,
                )
            )
            planned.append(rp)
            continue
        if conflict == REVIEW_REQUIRED:
            rp.classifications.append(REVIEW_REQUIRED)
            rp.company_id = cid
            rp.review = True
            rp.blocking = bool(cid)
            rp.proposals.append(
                FieldProposal(
                    REVIEW_REQUIRED, "record_no", str(cid or ""), incoming.record_no,
                    REVIEW_REQUIRED, "high", blocking=bool(cid),
                    review_reason=(
                        "manual_entity_link_candidate" if cid else "no_rn_name_only_forbidden"
                    ),
                    evidence=evidence,
                )
            )
            planned.append(rp)
            continue
        if conflict == SKIPPED_INVALID or (not incoming.record_no and not incoming.company_name):
            rp.classifications.append(SKIPPED_INVALID)
            planned.append(rp)
            continue

        stored_company = _load_company(conn, cid) if cid else None
        ccr = _load_ccr(conn, client_id=client_id, company_id=cid) if cid else None
        if ccr and ccr.get("_archived"):
            rp.classifications.append(RESTORE_RELATIONSHIP_REVIEW)
            rp.company_id = cid
            rp.ccr_id = int(ccr["id"])
            rp.blocking = True
            rp.review = True
            rp.proposals.append(
                FieldProposal(
                    RESTORE_RELATIONSHIP_REVIEW, "ccr", str(ccr["id"]), incoming.record_no,
                    RESTORE_RELATIONSHIP_REVIEW, "high", blocking=True,
                    review_reason="archived_relationship",
                )
            )
            planned.append(rp)
            continue
        if cid and stored_company:
            rp.company_id = cid
            rp.ccr_id = int(ccr["id"]) if ccr else None
            if ccr is None:
                rp.classifications.append(NEW_RELATIONSHIP)
                rp.proposals.append(
                    FieldProposal(NEW_RELATIONSHIP, "ccr", "", str(cid), NEW_RELATIONSHIP, "high")
                )
            rp.proposals.extend(
                _company_field_proposals(
                    stored_company, incoming, pol, conn=conn, company_id=cid
                )
            )
        else:
            rp.classifications.append(NEW_COMPANY)
            rp.proposals.append(
                FieldProposal(
                    NEW_COMPANY, "company", "", incoming.company_name, NEW_COMPANY, "high",
                    evidence=evidence,
                )
            )
            rp.classifications.append(NEW_RELATIONSHIP)

        if cid:
            contact_id, c_ev, c_conflict = _match_contact(conn, company_id=cid, incoming=incoming)
            rp.match_evidence.extend(c_ev)
            if c_conflict == ARCHIVED_MATCH_REVIEW:
                rp.classifications.append(ARCHIVED_MATCH_REVIEW)
                rp.contact_id = contact_id
                rp.blocking = True
                rp.review = True
                rp.proposals.append(
                    FieldProposal(
                        ARCHIVED_MATCH_REVIEW, "contact", str(contact_id or ""), incoming.email,
                        ARCHIVED_MATCH_REVIEW, "high", blocking=True,
                        review_reason="archived_contact", evidence=c_ev,
                    )
                )
            elif c_conflict == POSSIBLE_DUPLICATE:
                rp.classifications.append(POSSIBLE_DUPLICATE)
                rp.review = True
                rp.proposals.append(
                    FieldProposal(
                        POSSIBLE_DUPLICATE, "contact", "", incoming.email or incoming.contact_phone,
                        POSSIBLE_DUPLICATE, "medium", review_reason=";".join(c_ev),
                    )
                )
            elif contact_id:
                rp.contact_id = contact_id
                stored_c = _load_contact(conn, contact_id)
                rp.proposals.extend(
                    _contact_field_proposals(
                        stored_c, incoming, pol, conn=conn, contact_id=contact_id
                    )
                )
            elif incoming.first_name or incoming.last_name or incoming.email:
                rp.classifications.append(NEW_CONTACT)
                rp.proposals.append(
                    FieldProposal(
                        NEW_CONTACT, "contact", "",
                        f"{incoming.first_name} {incoming.last_name}".strip(),
                        NEW_CONTACT, "high", evidence=c_ev,
                    )
                )
        elif incoming.first_name or incoming.last_name or incoming.email:
            rp.classifications.append(NEW_CONTACT)

        st = _status_proposal(ccr, incoming, pol, catalog, conn=conn)
        if st:
            rp.proposals.append(st)
        nt = _notes_proposal(ccr, incoming)
        if nt:
            rp.proposals.append(nt)
        rp.proposals.extend(
            _history_proposals(conn, client_id=client_id, company_id=cid, incoming=incoming)
        )
        asg = _assignment_proposal(ccr, incoming, pol)
        if asg:
            rp.proposals.append(asg)
        camp = _campaign_proposal(
            conn, client_id=client_id, company_id=cid, incoming=incoming, policy=pol
        )
        if camp:
            rp.proposals.append(camp)

        kinds = []
        for p in rp.proposals:
            if p.action in {NO_CHANGE, UNCHANGED_NOTE, DUPLICATE_NOTE, EXCLUDED_NOTE,
                            DUPLICATE_HISTORY, UNCHANGED_ASSIGNMENT, UNCHANGED_HISTORY,
                            CAMPAIGN_HISTORICAL, CAMPAIGN_SYSTEM, CAMPAIGN_IGNORED,
                            PROVENANCE_ONLY, IGNORED_ASSIGNMENT}:
                continue
            if p.action in {SAFE_CANONICAL_UPDATE, ADDITIVE_UPDATE, CLIENT_SOURCE_DIFFERENCE,
                            LOCATION_DIFFERENCE} and p.kind == UPDATED_COMPANY:
                kinds.append(UPDATED_COMPANY)
            elif p.action in {ADDITIVE_UPDATE, SAFE_UPDATE, CONFLICT} and p.kind == UPDATED_CONTACT:
                kinds.append(UPDATED_CONTACT)
            elif p.kind in {NEW_COMPANY, NEW_RELATIONSHIP, NEW_CONTACT, STATUS_CHANGE,
                            NEW_NOTE, NEW_HISTORY, ASSIGNMENT_CHANGE, CAMPAIGN_CHANGE,
                            POSSIBLE_DUPLICATE, SOURCE_ID_CONFLICT, REVIEW_REQUIRED,
                            SKIPPED_INVALID, ARCHIVED_MATCH_REVIEW, RESTORE_RELATIONSHIP_REVIEW}:
                if p.action == SKIPPED_INVALID:
                    kinds.append(SKIPPED_INVALID)
                    rp.review = True
                    if REVIEW_REQUIRED not in kinds:
                        kinds.append(REVIEW_REQUIRED)
                elif p.action == CONFLICTING_SOURCE_HISTORY:
                    kinds.append(REVIEW_REQUIRED)
                    rp.review = True
                    rp.blocking = True
                elif p.action in {STATUS_CHANGE} or (p.kind == STATUS_CHANGE and p.action == STATUS_CHANGE):
                    kinds.append(STATUS_CHANGE)
                elif p.action == NEW_NOTE:
                    kinds.append(NEW_NOTE)
                elif p.action == NEW_HISTORY:
                    kinds.append(NEW_HISTORY)
                elif p.action == ASSIGNMENT_CHANGE:
                    kinds.append(ASSIGNMENT_CHANGE)
                elif p.action == CAMPAIGN_CHANGE:
                    kinds.append(CAMPAIGN_CHANGE)
                elif p.action == UNKNOWN_SOURCE_REP:
                    kinds.append(REVIEW_REQUIRED)
                    rp.review = True
                elif p.action in {INACTIVE_TARGET, AMBIGUOUS_ASSIGNMENT}:
                    kinds.append(REVIEW_REQUIRED)
                    rp.review = True
                elif p.action == CAMPAIGN_UNKNOWN:
                    kinds.append(REVIEW_REQUIRED)
                    rp.review = True
                else:
                    kinds.append(p.kind)
            if p.action == MANUAL_OVERRIDE_CONFLICT:
                rp.review = True
                rp.blocking = True
                kinds.append(MANUAL_OVERRIDE_CONFLICT)
                kinds.append(REVIEW_REQUIRED)
            if p.action == CONFLICT or p.blocking:
                rp.review = True
                rp.blocking = rp.blocking or p.blocking
                if p.action == CONFLICT:
                    kinds.append(REVIEW_REQUIRED)
            if p.action == LOCATION_DIFFERENCE:
                rp.review = True
                kinds.append(REVIEW_REQUIRED)
        ordered = []
        for k in kinds:
            if k not in ordered:
                ordered.append(k)
        for existing in rp.classifications:
            if existing not in ordered:
                ordered.insert(0, existing)
        rp.classifications = ordered or [UNCHANGED]
        if rp.classifications == [UNCHANGED] or (
            not ordered and not rp.review and not rp.blocking
        ):
            meaningful = [
                p for p in rp.proposals
                if p.action not in {
                    NO_CHANGE, UNCHANGED_NOTE, DUPLICATE_NOTE, EXCLUDED_NOTE,
                    DUPLICATE_HISTORY, UNCHANGED_ASSIGNMENT, PROVENANCE_ONLY,
                    CAMPAIGN_HISTORICAL, CAMPAIGN_SYSTEM,
                }
            ]
            if not meaningful:
                rp.classifications = [UNCHANGED]
        planned.append(rp)

    counts = {
        "source_rows": len(planned),
        "unchanged": 0,
        "new_companies": 0,
        "new_relationships": 0,
        "new_contacts": 0,
        "updated_companies": 0,
        "updated_contacts": 0,
        "status_changes": 0,
        "new_notes": 0,
        "duplicate_notes": 0,
        "excluded_notes": 0,
        "review_notes": 0,
        "new_history": 0,
        "duplicate_history": 0,
        "history_conflicts": 0,
        "review_history": 0,
        "assignment_changes": 0,
        "campaign_changes": 0,
        "possible_duplicates": 0,
        "source_id_conflicts": 0,
        "review_required": 0,
        "invalid_skipped": 0,
        "blocking": 0,
    }
    key_map = {
        UNCHANGED: "unchanged",
        NEW_COMPANY: "new_companies",
        NEW_RELATIONSHIP: "new_relationships",
        NEW_CONTACT: "new_contacts",
        UPDATED_COMPANY: "updated_companies",
        UPDATED_CONTACT: "updated_contacts",
        STATUS_CHANGE: "status_changes",
        NEW_NOTE: "new_notes",
        NEW_HISTORY: "new_history",
        ASSIGNMENT_CHANGE: "assignment_changes",
        CAMPAIGN_CHANGE: "campaign_changes",
        POSSIBLE_DUPLICATE: "possible_duplicates",
        SOURCE_ID_CONFLICT: "source_id_conflicts",
        REVIEW_REQUIRED: "review_required",
        SKIPPED_INVALID: "invalid_skipped",
    }
    for rp in planned:
        for c in rp.classifications:
            if c in key_map:
                counts[key_map[c]] += 1
        for p in rp.proposals:
            if p.field == "notes" or p.kind == NEW_NOTE:
                if p.action == DUPLICATE_NOTE:
                    counts["duplicate_notes"] += 1
                elif p.action == EXCLUDED_NOTE:
                    counts["excluded_notes"] += 1
                elif p.review_reason or p.action == REVIEW_REQUIRED:
                    counts["review_notes"] += 1
            if p.field == "history" or p.kind == NEW_HISTORY:
                if p.action == DUPLICATE_HISTORY:
                    counts["duplicate_history"] += 1
                elif p.action == CONFLICTING_SOURCE_HISTORY:
                    counts["history_conflicts"] += 1
                elif p.review_reason or p.action == REVIEW_REQUIRED:
                    counts["review_history"] += 1
        if rp.blocking:
            counts["blocking"] += 1
        if rp.review:
            counts["review_required"] = counts["review_required"]  # already counted per class
            if REVIEW_REQUIRED not in rp.classifications:
                counts["review_required"] += 1

    payload = {
        "schema": PLANNER_VERSION,
        "client_id": int(client_id),
        "policy": pol.fingerprint_slice(),
        "source_sha256": source_sha256,
        "source_filename": source_filename,
        "counts": counts,
        "rows": [_row_to_dict(r) for r in planned],
        "resolutions": [],
    }
    payload["plan_fingerprint"] = fingerprint_refresh_plan(payload)
    return payload
