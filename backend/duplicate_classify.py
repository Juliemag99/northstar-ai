"""DS-12 explainable duplicate classification and governed batch review.

Deterministic evidence engine. Not merge approval, not merge execution,
and not a networked LLM. Human review dispositions are never overwritten
by automated classification.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Protocol

from pydantic import BaseModel

from company_match import compact_company_name, names_match
from duplicate_review import (
    CAT_IDENTITY,
    CAT_NAME_ONLY,
    DISPOSITION_LIKELY,
    DISPOSITION_MERGE,
    DISPOSITION_MULTI,
    DISPOSITION_NOT,
    DISPOSITION_RESEARCH,
    DISPOSITION_UNREVIEWED,
    DuplicateReviewError,
    DuplicateReviewSaveRequest,
    _blank,
    _core_name,
    _load_aliases,
    _load_ccr_identity,
    _load_companies,
    _load_identities,
    _load_locations,
    _now,
    _row_dict,
    _table_exists,
    discover_duplicate_pairs,
    ensure_duplicate_review_schema,
    evidence_fingerprint,
    pair_ids,
    pair_key,
    save_duplicate_review,
)
from import_brown_industries import digits_phone, domain, norm_addr
from models import NorthStarUser

CLASSIFIER_VERSION = "DS12_RULES_V1"
CLASSIFIER_KIND = "deterministic_rules"
ASSESSMENT_TITLE = "NorthStar Automated Assessment"

CLASS_HIGH = "HIGH_CONFIDENCE_DUPLICATE"
CLASS_LIKELY = "LIKELY_DUPLICATE"
CLASS_MULTI = "LIKELY_MULTI_LOCATION"
CLASS_NOT = "LIKELY_NOT_DUPLICATE"
CLASS_HUMAN = "HUMAN_REVIEW_REQUIRED"
CLASS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

CLASSIFICATION_BUCKETS = (
    CLASS_HIGH,
    CLASS_LIKELY,
    CLASS_MULTI,
    CLASS_NOT,
    CLASS_HUMAN,
    CLASS_INSUFFICIENT,
)
CLASSIFICATION_SET = frozenset(CLASSIFICATION_BUCKETS)

CONF_HIGH = "HIGH"
CONF_MEDIUM = "MEDIUM"
CONF_LOW = "LOW"

BATCH_ACCEPT_LIKELY = "ACCEPT_LIKELY_DUPLICATE"
BATCH_ACCEPT_MULTI = "ACCEPT_MULTI_LOCATION"
BATCH_ACCEPT_NOT = "ACCEPT_NOT_DUPLICATE"
BATCH_SEND_RESEARCH = "SEND_NEEDS_RESEARCH"
BATCH_ACTIONS = {
    BATCH_ACCEPT_LIKELY: DISPOSITION_LIKELY,
    BATCH_ACCEPT_MULTI: DISPOSITION_MULTI,
    BATCH_ACCEPT_NOT: DISPOSITION_NOT,
    BATCH_SEND_RESEARCH: DISPOSITION_RESEARCH,
}
BATCH_FORBIDDEN = frozenset(
    {
        DISPOSITION_MERGE,
        "MERGE",
        "EXECUTE_MERGE",
        "APPROVE_MERGE",
        "MERGE_CANDIDATE",
        "BATCH_MERGE_CANDIDATE",
    }
)
REVIEW_SOURCE_BATCH = "DUPLICATE_BATCH_REVIEW"
REVIEW_SOURCE_MANUAL = "MANUAL"
SELECTION_EXPLICIT = "explicit"
SELECTION_FILTERED = "filtered"

DUP_AUTO = frozenset({CLASS_HIGH, CLASS_LIKELY})
DUP_HUMAN = frozenset({DISPOSITION_LIKELY, DISPOSITION_MERGE})
MULTI_AUTO = frozenset({CLASS_MULTI})
MULTI_HUMAN = frozenset({DISPOSITION_MULTI})
NOT_AUTO = frozenset({CLASS_NOT})
NOT_HUMAN = frozenset({DISPOSITION_NOT})
HQ_TOKENS = ("headquarters", " hq", "hq ", "(hq)", "corporate office", "head office")
PLANT_TOKENS = (" plant", "plant ", "facility", "division", "factory", " works")


class DuplicateSecondOpinionProvider(Protocol):
    """Optional future AI second opinion. DS-12 never calls a network provider."""

    def assess(self, evidence: dict[str, Any]) -> dict[str, Any] | None:
        ...


def maybe_second_opinion(
    evidence: dict[str, Any],
    provider: DuplicateSecondOpinionProvider | None = None,
) -> dict[str, Any] | None:
    """Hook only. Default provider is None; no company data leaves the process."""
    if provider is None:
        return None
    return provider.assess(evidence)


class DuplicateAnalyzeRequest(BaseModel):
    actor_id: int | None = None
    created_by: str = ""


class DuplicateBatchReviewRequest(BaseModel):
    action: str
    reason: str = ""
    selection_mode: str = SELECTION_EXPLICIT
    pair_keys: list[str] = []
    queue: str = ""
    q: str = ""
    preview_fingerprint: str = ""
    confirm: bool = False
    actor_id: int | None = None
    created_by: str = ""


def _zip_digits(value: object | None) -> str:
    digits = "".join(ch for ch in _blank(value) if ch.isdigit())
    return digits[:5] if len(digits) >= 5 else digits


def _site_role(name: str) -> str:
    text = f" {_blank(name).lower()} "
    if any(token in text for token in HQ_TOKENS):
        return "hq"
    if any(token in text for token in PLANT_TOKENS):
        return "plant"
    return ""


def _fact(code: str, label: str, polarity: str = "support") -> dict[str, str]:
    return {"code": code, "label": label, "polarity": polarity}


def classification_disagrees(automated: str, human: str) -> bool:
    auto = _blank(automated).upper()
    disp = _blank(human).upper()
    if not auto or auto not in CLASSIFICATION_SET:
        return False
    if disp in {"", DISPOSITION_UNREVIEWED}:
        return False
    if auto in {CLASS_HUMAN, CLASS_INSUFFICIENT}:
        return False
    if auto in DUP_AUTO and disp in DUP_HUMAN:
        return False
    if auto in MULTI_AUTO and disp in MULTI_HUMAN:
        return False
    if auto in NOT_AUTO and disp in NOT_HUMAN:
        return False
    return True


def review_needed_reasons(
    *,
    classification: str,
    confidence: str,
    human: str,
    stale_human: bool,
    stale_classification: bool,
    survivor_required: bool,
    disagreement: bool,
) -> list[str]:
    reasons: list[str] = []
    if classification == CLASS_HUMAN:
        reasons.append("HUMAN_REVIEW_REQUIRED")
    if disagreement:
        reasons.append("HUMAN_AUTOMATION_DISAGREEMENT")
    if stale_human:
        reasons.append("STALE_HUMAN_REVIEW")
    if survivor_required and classification in DUP_AUTO:
        reasons.append("SURVIVOR_DECISION_REQUIRED")
    if stale_classification and classification in {CLASS_HUMAN, CLASS_HIGH}:
        reasons.append("STALE_CLASSIFICATION")
    _ = confidence
    return list(dict.fromkeys(reasons))


def classify_from_signals(signals: dict[str, Any]) -> dict[str, Any]:
    """Pure deterministic classifier. Every bucket is tied to named facts."""
    facts: list[dict[str, str]] = []
    concerns: list[dict[str, str]] = []
    canonical_equal = bool(signals.get("canonical_equal"))
    core_only = bool(signals.get("core_only"))
    name_equal = bool(signals.get("name_equal")) or canonical_equal
    addr_equal = bool(signals.get("addr_equal"))
    city_state = bool(signals.get("city_state"))
    zip_equal = bool(signals.get("zip_equal"))
    phone_equal = bool(signals.get("phone_equal"))
    domain_equal = bool(signals.get("domain_equal"))
    alias_match = bool(signals.get("alias_match"))
    identity_conflict = bool(signals.get("identity_conflict"))
    different_city = bool(signals.get("different_city"))
    different_phone = bool(signals.get("different_phone"))
    different_domain = bool(signals.get("different_domain"))
    both_phone = bool(signals.get("both_have_phone"))
    both_domain = bool(signals.get("both_have_domain"))
    exact_email = bool(signals.get("exact_contact_email"))
    exact_phone_overlap = bool(signals.get("exact_contact_phone"))
    location_distinct = bool(signals.get("location_distinct_cities"))
    same_client = bool(signals.get("same_client_ccr"))
    material_ccr = bool(signals.get("material_ccr_conflict"))
    archived_any = bool(signals.get("archived_any"))
    both_rn = bool(signals.get("both_have_rn"))
    different_rn = bool(signals.get("different_rn"))
    hq_plant = bool(signals.get("hq_vs_plant"))
    categories = list(signals.get("categories") or [])

    if canonical_equal:
        facts.append(_fact("NAME_EQUAL", "Canonical names normalize identically"))
    elif core_only:
        facts.append(_fact("CORE_NAME", "Core name tokens match; full canonical names differ", "concern"))
    if addr_equal:
        facts.append(_fact("ADDR_EQUAL", "Street address matches"))
    if city_state:
        facts.append(_fact("CITY_STATE", "City and state match"))
    if zip_equal:
        facts.append(_fact("ZIP_EQUAL", "ZIP code matches"))
    if phone_equal:
        facts.append(_fact("PHONE_EQUAL", "Main phone matches"))
    elif different_phone:
        concerns.append(_fact("PHONE_DIFFERS", "Main phones differ", "concern"))
    if domain_equal:
        facts.append(_fact("DOMAIN_EQUAL", "Website domain matches"))
    elif different_domain:
        concerns.append(_fact("DOMAIN_DIFFERS", "Website domains differ", "concern"))
    if alias_match:
        facts.append(_fact("ALIAS_MATCH", "Alias relationship links the two names"))
    if exact_email:
        facts.append(_fact("CONTACT_EMAIL", "Contacts share an exact email identity"))
    if exact_phone_overlap:
        facts.append(_fact("CONTACT_PHONE", "Contacts share an exact phone identity"))
    if identity_conflict:
        concerns.append(
            _fact("IDENTITY_CONFLICT", "Source identity keys collide across two masters", "concern")
        )
    if same_client:
        concerns.append(
            _fact(
                "CCR_CONFLICT",
                "Same client has a relationship on both records (merge complexity, not identity proof)",
                "concern",
            )
        )
    if material_ccr:
        concerns.append(
            _fact(
                "CCR_STATUS_OWNER",
                "Same-client CCRs have materially different statuses, owners, or history",
                "concern",
            )
        )
    if archived_any:
        concerns.append(_fact("ARCHIVED", "At least one company is archived", "concern"))
    if both_rn and different_rn:
        concerns.append(_fact("RN_CONFLICT", "LeadMaster record numbers differ", "concern"))
    if different_city and not addr_equal:
        concerns.append(_fact("DIFFERENT_CITY", "Physical cities differ", "concern"))
    if location_distinct:
        concerns.append(
            _fact("LOCATION_ROWS", "company_locations support distinct plant/city sites", "concern")
        )
    if hq_plant:
        concerns.append(_fact("HQ_PLANT", "Names suggest an HQ versus plant relationship", "concern"))

    corroboration = sum(
        [
            addr_equal,
            phone_equal,
            domain_equal,
            alias_match,
            exact_email,
            exact_phone_overlap,
            city_state and zip_equal,
        ]
    )
    strong_duplicate = bool(
        (canonical_equal and addr_equal)
        or (canonical_equal and phone_equal and (addr_equal or city_state or domain_equal))
        or (canonical_equal and domain_equal and (addr_equal or (city_state and not different_city)))
        or (alias_match and (addr_equal or phone_equal) and canonical_equal and not different_city)
        or (canonical_equal and addr_equal and (exact_email or exact_phone_overlap))
    )
    strong_multi = bool(
        (canonical_equal or domain_equal or alias_match)
        and different_city
        and not addr_equal
    )

    classification = CLASS_INSUFFICIENT
    confidence = CONF_LOW
    human_triggers: list[str] = []

    if identity_conflict:
        human_triggers.append("source identity conflict")
    if hq_plant and different_city:
        human_triggers.append("uncertain HQ vs plant relationship")
    if archived_any and not (canonical_equal and (addr_equal or phone_equal or domain_equal)):
        human_triggers.append("archived company with unclear identity")
    if both_rn and different_rn and not (addr_equal or phone_equal or domain_equal):
        human_triggers.append("conflicting LeadMaster RNs that cannot be safely reconciled")
    if material_ccr and not strong_duplicate:
        human_triggers.append("same-client CCR conflict without strong identity corroboration")

    if identity_conflict:
        classification = CLASS_HUMAN
        confidence = CONF_MEDIUM if corroboration else CONF_LOW
    elif different_city and not addr_equal and (canonical_equal or alias_match) and domain_equal:
        classification = CLASS_MULTI
        confidence = CONF_HIGH if canonical_equal else CONF_MEDIUM
    elif different_city and not addr_equal and location_distinct and (canonical_equal or alias_match):
        classification = CLASS_MULTI
        confidence = CONF_MEDIUM
    elif different_city and not addr_equal and (canonical_equal or core_only):
        classification = CLASS_HUMAN
        confidence = CONF_MEDIUM if phone_equal or domain_equal else CONF_LOW
        human_triggers.append("different cities without matching street address")
    elif human_triggers and not strong_duplicate:
        classification = CLASS_HUMAN
        confidence = CONF_MEDIUM if corroboration else CONF_LOW
    elif strong_duplicate:
        classification = CLASS_HIGH
        confidence = CONF_HIGH
        if material_ccr:
            human_triggers.append("same-client CCR conflict requires reconciliation")
    elif canonical_equal and phone_equal:
        classification = CLASS_LIKELY
        confidence = CONF_MEDIUM
    elif canonical_equal and domain_equal and not different_city:
        classification = CLASS_LIKELY
        confidence = CONF_MEDIUM
    elif canonical_equal and city_state and (zip_equal or alias_match or exact_email or exact_phone_overlap):
        classification = CLASS_LIKELY
        confidence = CONF_MEDIUM
    elif alias_match and (phone_equal or domain_equal or addr_equal) and not different_city:
        classification = CLASS_LIKELY
        confidence = CONF_MEDIUM
    elif (
        not canonical_equal
        and not alias_match
        and not addr_equal
        and not phone_equal
        and different_domain
        and different_phone
        and (different_city or (both_domain and both_phone))
    ):
        classification = CLASS_NOT
        confidence = CONF_MEDIUM
    elif core_only or ((name_equal or CAT_NAME_ONLY in categories) and corroboration == 0):
        classification = CLASS_INSUFFICIENT
        confidence = CONF_LOW
    elif canonical_equal and corroboration <= 1 and (different_phone or different_domain):
        classification = CLASS_HUMAN
        confidence = CONF_LOW
        human_triggers.append("name match with conflicting secondary identity fields")
    else:
        classification = CLASS_HUMAN if corroboration else CLASS_INSUFFICIENT
        confidence = CONF_LOW if classification == CLASS_INSUFFICIENT else CONF_MEDIUM
        if classification == CLASS_HUMAN:
            human_triggers.append("evidence is mixed or incomplete")

    if classification == CLASS_HIGH and identity_conflict:
        classification = CLASS_HUMAN
        confidence = CONF_MEDIUM
        human_triggers.append("source identity conflict")

    why = [row["label"] for row in facts if row["polarity"] == "support"]
    concern_labels = [row["label"] for row in concerns]
    return {
        "classification": classification,
        "confidence": confidence,
        "classifier_version": CLASSIFIER_VERSION,
        "classifier_kind": CLASSIFIER_KIND,
        "assessment_title": ASSESSMENT_TITLE,
        "evidence": facts,
        "concerns": concerns,
        "why": why,
        "concern_labels": concern_labels,
        "human_review_triggers": list(dict.fromkeys(human_triggers)),
        "strong_duplicate": strong_duplicate,
        "strong_multi_location": strong_multi,
        "same_client_ccr_conflict": same_client,
        "material_ccr_conflict": material_ccr,
        "opaque_score": False,
        "external_ai_used": False,
        "categories": categories,
    }


def propose_survivor(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Informational proposal only. Never uses lower company ID as a tie-break."""

    def score(side: dict[str, Any]) -> tuple[int, list[str]]:
        pts = 0
        why: list[str] = []
        if side.get("archived"):
            pts -= 8
            why.append("record is archived")
        if side.get("has_external_rn"):
            pts += 3
            why.append("established LeadMaster RN")
        clients = int(side.get("client_count") or 0)
        ccrs = int(side.get("active_ccr_count") or 0)
        if clients:
            pts += 2 if clients >= 2 else 1
            why.append("more client relationships" if clients >= 2 else "has a client relationship")
        elif ccrs:
            pts += 1
            why.append("has an active client relationship")
        if int(side.get("identity_count") or 0):
            pts += min(2, int(side["identity_count"]))
            why.append("stronger source identity coverage")
        if side.get("has_canonical_address"):
            pts += 2
            why.append("canonical address complete")
        if int(side.get("alias_count") or 0):
            pts += 1
            why.append("has alias coverage")
        if int(side.get("location_count") or 0):
            pts += 1
            why.append("has location rows")
        if int(side.get("contact_count") or 0):
            pts += 1
            why.append("has contacts")
        history = int(side.get("history_note_count") or 0) + int(side.get("history_activity_count") or 0)
        if history:
            pts += 1
            why.append("longer operational history")
        if int(side.get("campaign_count") or 0):
            pts += 1
            why.append("has campaign history")
        if int(side.get("research_count") or 0):
            pts += 1
            why.append("has research history")
        if int(side.get("merge_as_survivor") or 0):
            pts += 2
            why.append("previous survivor / redirect role")
        if int(side.get("merge_as_source") or 0):
            pts -= 1
        return pts, why

    left_id = int(left["company_id"])
    right_id = int(right["company_id"])
    left_pts, left_why = score(left)
    right_pts, right_why = score(right)
    if abs(left_pts - right_pts) < 2:
        return {
            "proposed_survivor_company_id": None,
            "proposed_source_company_id": None,
            "survivor_decision_required": True,
            "survivor_reason": "Neither record is clearly preferable.",
            "survivor_reasons": ["Neither record is clearly preferable. Survivor decision required."],
            "scores": {str(left_id): left_pts, str(right_id): right_pts},
            "used_lower_id_tiebreak": False,
        }
    if left_pts > right_pts:
        winner, loser, reasons = left_id, right_id, left_why
    else:
        winner, loser, reasons = right_id, left_id, right_why
    return {
        "proposed_survivor_company_id": winner,
        "proposed_source_company_id": loser,
        "survivor_decision_required": False,
        "survivor_reason": "; ".join(reasons) or "Stronger operational completeness.",
        "survivor_reasons": reasons,
        "scores": {str(left_id): left_pts, str(right_id): right_pts},
        "used_lower_id_tiebreak": False,
    }


def _load_contacts_lite(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not _table_exists(conn, "contacts"):
        return out
    for row in conn.execute(
        "SELECT id, company_id, first_name, last_name, email, phone FROM contacts"
    ).fetchall():
        item = _row_dict(row)
        out.setdefault(int(item["company_id"]), []).append(item)
    return out


def _load_ccr_rows(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not _table_exists(conn, "client_company_relationships"):
        return out
    sql = """
        SELECT ccr.company_id, ccr.client_id, ccr.status, ccr.assigned_user_id,
               TRIM(COALESCE(ccr.archived_at, '')) AS archived_at,
               TRIM(COALESCE(ccr.notes, '')) AS notes,
               ccr.follow_up_date, ccr.is_hot, cl.code AS client_code, cl.name AS client_name
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
    """
    for row in conn.execute(sql).fetchall():
        item = _row_dict(row)
        item["active"] = not bool(_blank(item.get("archived_at")))
        out.setdefault(int(item["company_id"]), []).append(item)
    return out


def _load_planning_stats(conn: sqlite3.Connection, company_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    ids = sorted(company_ids)
    placeholders = ",".join("?" * len(ids))
    out: dict[int, dict[str, Any]] = {
        cid: {
            "company_id": cid,
            "has_external_rn": False,
            "active_ccr_count": 0,
            "client_count": 0,
            "contact_count": 0,
            "has_canonical_address": False,
            "alias_count": 0,
            "location_count": 0,
            "identity_count": 0,
            "archived": False,
            "merge_as_source": 0,
            "merge_as_survivor": 0,
            "history_note_count": 0,
            "history_activity_count": 0,
            "campaign_count": 0,
            "research_count": 0,
        }
        for cid in ids
    }
    for row in conn.execute(
        f"""
        SELECT id, TRIM(COALESCE(external_record_no,'')) AS rn,
               TRIM(COALESCE(address,'')) AS address,
               TRIM(COALESCE(archived_at,'')) AS archived_at
        FROM companies WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall():
        item = out[int(row["id"])]
        item["has_external_rn"] = bool(_blank(row["rn"]))
        item["has_canonical_address"] = bool(_blank(row["address"]))
        item["archived"] = bool(_blank(row["archived_at"]))
    if _table_exists(conn, "contacts"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM contacts WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["contact_count"] = int(row["n"])
    if _table_exists(conn, "client_company_relationships"):
        for row in conn.execute(
            f"""
            SELECT company_id,
                   SUM(CASE WHEN TRIM(COALESCE(archived_at,''))='' THEN 1 ELSE 0 END) AS active_n,
                   COUNT(DISTINCT client_id) AS clients
            FROM client_company_relationships
            WHERE company_id IN ({placeholders})
            GROUP BY company_id
            """,
            ids,
        ).fetchall():
            out[int(row["company_id"])]["active_ccr_count"] = int(row["active_n"] or 0)
            out[int(row["company_id"])]["client_count"] = int(row["clients"] or 0)
    if _table_exists(conn, "company_aliases"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM company_aliases WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["alias_count"] = int(row["n"])
    if _table_exists(conn, "company_locations"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM company_locations WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["location_count"] = int(row["n"])
    if _table_exists(conn, "company_source_identities"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM company_source_identities WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["identity_count"] = int(row["n"])
    if _table_exists(conn, "company_merge_history"):
        for row in conn.execute(
            f"SELECT source_company_id AS id, COUNT(*) n FROM company_merge_history WHERE source_company_id IN ({placeholders}) GROUP BY source_company_id",
            ids,
        ).fetchall():
            out[int(row["id"])]["merge_as_source"] = int(row["n"])
        for row in conn.execute(
            f"SELECT survivor_company_id AS id, COUNT(*) n FROM company_merge_history WHERE survivor_company_id IN ({placeholders}) GROUP BY survivor_company_id",
            ids,
        ).fetchall():
            out[int(row["id"])]["merge_as_survivor"] = int(row["n"])
    if _table_exists(conn, "activities"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM activities WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["history_activity_count"] = int(row["n"])
    if _table_exists(conn, "legacy_notes"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM legacy_notes WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["history_note_count"] = int(row["n"])
    if _table_exists(conn, "campaign_companies"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM campaign_companies WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["campaign_count"] = int(row["n"])
    if _table_exists(conn, "company_research_jobs"):
        for row in conn.execute(
            f"SELECT company_id, COUNT(*) n FROM company_research_jobs WHERE company_id IN ({placeholders}) GROUP BY company_id",
            ids,
        ).fetchall():
            out[int(row["company_id"])]["research_count"] = int(row["n"])
    return out


def _contact_identity_flags(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> tuple[bool, bool]:
    emails_l = {_blank(r.get("email")).lower() for r in left if _blank(r.get("email"))}
    emails_r = {_blank(r.get("email")).lower() for r in right if _blank(r.get("email"))}
    phones_l = {
        digits_phone(r.get("phone"))[-7:]
        for r in left
        if len(digits_phone(r.get("phone"))) >= 7
    }
    phones_r = {
        digits_phone(r.get("phone"))[-7:]
        for r in right
        if len(digits_phone(r.get("phone"))) >= 7
    }
    return bool(emails_l & emails_r), bool(phones_l & phones_r)


def _location_cities(rows: list[dict[str, Any]]) -> set[str]:
    cities: set[str] = set()
    for row in rows:
        city = _blank(row.get("city")).lower()
        state = _blank(row.get("state")).upper()
        if city:
            cities.add(f"{city}|{state}")
    return cities


def pair_signals(
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    categories: list[str],
    alias_match: bool,
    identity_conflict: bool,
    left_contacts: list[dict[str, Any]],
    right_contacts: list[dict[str, Any]],
    left_ccrs: list[dict[str, Any]],
    right_ccrs: list[dict[str, Any]],
    left_locations: list[dict[str, Any]],
    right_locations: list[dict[str, Any]],
) -> dict[str, Any]:
    a_name = _blank(left.get("company_name"))
    b_name = _blank(right.get("company_name"))
    compact_a = compact_company_name(a_name)
    compact_b = compact_company_name(b_name)
    canonical_equal = bool(names_match(a_name, b_name) or (compact_a and compact_a == compact_b))
    core_equal = bool(_core_name(a_name) and _core_name(a_name) == _core_name(b_name))
    addr_a = norm_addr(_blank(left.get("address") or left.get("addr_norm")))
    addr_b = norm_addr(_blank(right.get("address") or right.get("addr_norm")))
    addr_equal = bool(addr_a and addr_a == addr_b)
    city_a = _blank(left.get("city")).lower()
    city_b = _blank(right.get("city")).lower()
    state_a = _blank(left.get("state")).upper()
    state_b = _blank(right.get("state")).upper()
    city_state = bool(city_a and city_b and state_a and city_a == city_b and state_a == state_b)
    different_city = bool(city_a and city_b and city_a != city_b)
    zip_equal = bool(_zip_digits(left.get("zip")) and _zip_digits(left.get("zip")) == _zip_digits(right.get("zip")))
    phone_a = digits_phone(left.get("legacy_phone") or left.get("phone") or left.get("phone_digits"))
    phone_b = digits_phone(right.get("legacy_phone") or right.get("phone") or right.get("phone_digits"))
    both_phone = bool(len(phone_a) >= 7 and len(phone_b) >= 7)
    phone_equal = bool(both_phone and phone_a[-7:] == phone_b[-7:])
    different_phone = bool(both_phone and phone_a[-7:] != phone_b[-7:])
    domain_a = domain(_blank(left.get("website") or left.get("domain")))
    domain_b = domain(_blank(right.get("website") or right.get("domain")))
    both_domain = bool(domain_a and domain_b)
    domain_equal = bool(both_domain and domain_a == domain_b)
    different_domain = bool(both_domain and domain_a != domain_b)
    exact_email, exact_phone = _contact_identity_flags(left_contacts, right_contacts)
    loc_a = _location_cities(left_locations)
    loc_b = _location_cities(right_locations)
    location_distinct = bool(loc_a and loc_b and loc_a != loc_b)
    by_left = {int(r["client_id"]): r for r in left_ccrs}
    same_client = False
    material = False
    for rel in right_ccrs:
        other = by_left.get(int(rel["client_id"]))
        if not other:
            continue
        same_client = True
        if (
            _blank(rel.get("status")).lower() != _blank(other.get("status")).lower()
            or int(rel.get("assigned_user_id") or 0) != int(other.get("assigned_user_id") or 0)
            or bool(rel.get("active")) != bool(other.get("active"))
            or bool(_blank(rel.get("notes"))) != bool(_blank(other.get("notes")))
            or bool(_blank(rel.get("follow_up_date"))) != bool(_blank(other.get("follow_up_date")))
        ):
            material = True
    rn_a = _blank(left.get("external_record_no"))
    rn_b = _blank(right.get("external_record_no"))
    role_a = _site_role(a_name)
    role_b = _site_role(b_name)
    return {
        "canonical_equal": canonical_equal,
        "core_only": bool(core_equal and not canonical_equal),
        "name_equal": canonical_equal or core_equal,
        "addr_equal": addr_equal,
        "city_state": city_state,
        "zip_equal": zip_equal,
        "phone_equal": phone_equal,
        "both_have_phone": both_phone,
        "different_phone": different_phone,
        "domain_equal": domain_equal,
        "both_have_domain": both_domain,
        "different_domain": different_domain,
        "alias_match": alias_match,
        "identity_conflict": identity_conflict or CAT_IDENTITY in categories,
        "different_city": different_city,
        "exact_contact_email": exact_email,
        "exact_contact_phone": exact_phone,
        "location_distinct_cities": location_distinct,
        "same_client_ccr": same_client,
        "material_ccr_conflict": material,
        "archived_any": bool(left.get("archived") or right.get("archived")),
        "both_have_rn": bool(rn_a and rn_b),
        "different_rn": bool(rn_a and rn_b and rn_a != rn_b),
        "hq_vs_plant": bool(role_a and role_b and role_a != role_b),
        "categories": categories,
    }


def load_classifications(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    ensure_duplicate_review_schema(conn)
    out: dict[str, dict[str, Any]] = {}
    if not _table_exists(conn, "company_duplicate_classifications"):
        return out
    for row in conn.execute("SELECT * FROM company_duplicate_classifications").fetchall():
        item = _row_dict(row)
        out[str(item["pair_key"])] = item
    return out


def _parse_json(value: object, fallback: Any) -> Any:
    raw = _blank(value)
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _assessment_from_row(
    stored: dict[str, Any],
    *,
    current_fingerprint: str,
    human: str,
    stale_human: bool,
) -> dict[str, Any]:
    evidence = _parse_json(stored.get("evidence_json"), {})
    survivor = _parse_json(stored.get("survivor_reason_json"), {})
    classification = _blank(stored.get("classification"))
    stored_fp = _blank(stored.get("evidence_fingerprint"))
    stale = bool(stored_fp and stored_fp != current_fingerprint)
    if stored.get("stale_at"):
        stale = True
    disagreement = classification_disagrees(classification, human)
    survivor_required = bool(survivor.get("survivor_decision_required"))
    needed = review_needed_reasons(
        classification=classification,
        confidence=_blank(stored.get("confidence")),
        human=human,
        stale_human=stale_human,
        stale_classification=stale,
        survivor_required=survivor_required,
        disagreement=disagreement,
    )
    return {
        "assessment_title": ASSESSMENT_TITLE,
        "classifier_kind": CLASSIFIER_KIND,
        "external_ai_used": False,
        "classification": classification,
        "confidence": _blank(stored.get("confidence")),
        "classifier_version": _blank(stored.get("classifier_version")) or CLASSIFIER_VERSION,
        "classified_at": _blank(stored.get("classified_at")),
        "stale": stale,
        "evidence": evidence.get("evidence") or [],
        "concerns": evidence.get("concerns") or [],
        "why": evidence.get("why") or [],
        "concern_labels": evidence.get("concern_labels") or [],
        "human_review_triggers": evidence.get("human_review_triggers") or [],
        "same_client_ccr_conflict": bool(evidence.get("same_client_ccr_conflict")),
        "material_ccr_conflict": bool(evidence.get("material_ccr_conflict")),
        "proposed_survivor_company_id": stored.get("proposed_survivor_company_id"),
        "proposed_source_company_id": stored.get("proposed_source_company_id"),
        "survivor_decision_required": survivor_required,
        "survivor_reasons": survivor.get("survivor_reasons") or [],
        "survivor_reason": survivor.get("survivor_reason") or "",
        "evidence_fingerprint": stored_fp,
        "disagreement": disagreement,
        "review_needed": bool(needed),
        "review_needed_reasons": needed,
        "current": not stale,
    }


def build_pair_assessment(
    conn: sqlite3.Connection,
    pair: dict[str, Any],
    *,
    companies: dict[int, dict[str, Any]],
    aliases: dict[int, list[dict[str, Any]]],
    identities: dict[int, list[dict[str, Any]]],
    locations: dict[int, list[dict[str, Any]]],
    contacts: dict[int, list[dict[str, Any]]],
    ccrs: dict[int, list[dict[str, Any]]],
    stats: dict[int, dict[str, Any]],
    persist: bool = False,
) -> dict[str, Any]:
    lo = int(pair["company_a_id"])
    hi = int(pair["company_b_id"])
    left = companies.get(lo) or {}
    right = companies.get(hi) or {}
    signals = pair_signals(
        left=left,
        right=right,
        categories=list(pair.get("categories") or []),
        alias_match="ALIAS_MATCH" in (pair.get("categories") or []),
        identity_conflict=CAT_IDENTITY in (pair.get("categories") or []),
        left_contacts=contacts.get(lo) or [],
        right_contacts=contacts.get(hi) or [],
        left_ccrs=ccrs.get(lo) or [],
        right_ccrs=ccrs.get(hi) or [],
        left_locations=locations.get(lo) or [],
        right_locations=locations.get(hi) or [],
    )
    # Alias from discovery categories is authoritative, but also accept flag on pair.
    if pair.get("alias_match"):
        signals["alias_match"] = True
    if "ALIAS_MATCH" in (pair.get("categories") or []):
        signals["alias_match"] = True
    result = classify_from_signals(signals)
    survivor = {
        "proposed_survivor_company_id": None,
        "proposed_source_company_id": None,
        "survivor_decision_required": False,
        "survivor_reason": "",
        "survivor_reasons": [],
        "used_lower_id_tiebreak": False,
    }
    if result["classification"] in DUP_AUTO:
        survivor = propose_survivor(
            stats.get(lo) or {"company_id": lo},
            stats.get(hi) or {"company_id": hi},
        )
        if survivor.get("survivor_decision_required"):
            result["human_review_triggers"] = list(
                dict.fromkeys(
                    list(result.get("human_review_triggers") or []) + ["survivor decision required"]
                )
            )
    fingerprint = _blank(pair.get("evidence_fingerprint")) or evidence_fingerprint(
        conn,
        lo,
        hi,
        companies=companies,
        aliases=aliases,
        identities=identities,
        locations=locations,
        ccrs=_load_ccr_identity(conn),
    )
    payload = {
        **result,
        **survivor,
        "pair_key": f"{lo}:{hi}",
        "company_a_id": lo,
        "company_b_id": hi,
        "evidence_fingerprint": fingerprint,
        "classifier_version": CLASSIFIER_VERSION,
    }
    _ = persist
    return payload


def _upsert_classification(conn: sqlite3.Connection, payload: dict[str, Any]) -> str:
    ensure_duplicate_review_schema(conn)
    key = payload["pair_key"]
    now = _now()
    existing = conn.execute(
        "SELECT * FROM company_duplicate_classifications WHERE pair_key=?",
        (key,),
    ).fetchone()
    evidence_json = json.dumps(
        {
            "evidence": payload.get("evidence") or [],
            "concerns": payload.get("concerns") or [],
            "why": payload.get("why") or [],
            "concern_labels": payload.get("concern_labels") or [],
            "human_review_triggers": payload.get("human_review_triggers") or [],
            "same_client_ccr_conflict": bool(payload.get("same_client_ccr_conflict")),
            "material_ccr_conflict": bool(payload.get("material_ccr_conflict")),
            "strong_duplicate": bool(payload.get("strong_duplicate")),
            "strong_multi_location": bool(payload.get("strong_multi_location")),
            "categories": payload.get("categories") or [],
            "classifier_kind": CLASSIFIER_KIND,
            "external_ai_used": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    survivor_json = json.dumps(
        {
            "survivor_reason": payload.get("survivor_reason") or "",
            "survivor_reasons": payload.get("survivor_reasons") or [],
            "survivor_decision_required": bool(payload.get("survivor_decision_required")),
            "scores": (payload.get("scores") or {}),
            "used_lower_id_tiebreak": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if existing is not None:
        old = _row_dict(existing)
        same = (
            _blank(old.get("evidence_fingerprint")) == _blank(payload.get("evidence_fingerprint"))
            and _blank(old.get("classifier_version")) == CLASSIFIER_VERSION
            and _blank(old.get("classification")) == _blank(payload.get("classification"))
            and _blank(old.get("confidence")) == _blank(payload.get("confidence"))
            and (old.get("proposed_survivor_company_id") or None)
            == (payload.get("proposed_survivor_company_id") or None)
        )
        if same and not old.get("stale_at"):
            return "reused"
        conn.execute(
            """
            UPDATE company_duplicate_classifications
            SET classification=?, confidence=?, evidence_json=?,
                proposed_survivor_company_id=?, proposed_source_company_id=?,
                survivor_reason_json=?, evidence_fingerprint=?, classifier_version=?,
                classified_at=?, stale_at=NULL
            WHERE pair_key=?
            """,
            (
                payload["classification"],
                payload["confidence"],
                evidence_json,
                payload.get("proposed_survivor_company_id"),
                payload.get("proposed_source_company_id"),
                survivor_json,
                payload.get("evidence_fingerprint") or "",
                CLASSIFIER_VERSION,
                now,
                key,
            ),
        )
        return "updated"
    conn.execute(
        """
        INSERT INTO company_duplicate_classifications (
            pair_key, company_a_id, company_b_id, classification, confidence,
            evidence_json, proposed_survivor_company_id, proposed_source_company_id,
            survivor_reason_json, evidence_fingerprint, classifier_version,
            classified_at, stale_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            key,
            int(payload["company_a_id"]),
            int(payload["company_b_id"]),
            payload["classification"],
            payload["confidence"],
            evidence_json,
            payload.get("proposed_survivor_company_id"),
            payload.get("proposed_source_company_id"),
            survivor_json,
            payload.get("evidence_fingerprint") or "",
            CLASSIFIER_VERSION,
            now,
        ),
    )
    return "inserted"


def analyze_duplicate_candidates(
    conn: sqlite3.Connection, *, actor: NorthStarUser
) -> dict[str, Any]:
    if not bool(getattr(actor, "is_administrator", False)):
        raise PermissionError("Not authorized.")
    ensure_duplicate_review_schema(conn)
    before_reviews = 0
    if _table_exists(conn, "company_duplicate_reviews"):
        before_reviews = int(conn.execute("SELECT COUNT(*) FROM company_duplicate_reviews").fetchone()[0])
    pairs = discover_duplicate_pairs(conn)
    companies = _load_companies(conn)
    aliases = _load_aliases(conn)
    identities = _load_identities(conn)
    locations = _load_locations(conn)
    contacts = _load_contacts_lite(conn)
    ccrs = _load_ccr_rows(conn)
    ids = {int(p["company_a_id"]) for p in pairs} | {int(p["company_b_id"]) for p in pairs}
    stats = _load_planning_stats(conn, ids)
    buckets = {key: 0 for key in CLASSIFICATION_BUCKETS}
    inserted = updated = reused = 0
    for pair in pairs:
        payload = build_pair_assessment(
            conn,
            pair,
            companies=companies,
            aliases=aliases,
            identities=identities,
            locations=locations,
            contacts=contacts,
            ccrs=ccrs,
            stats=stats,
            persist=True,
        )
        action = _upsert_classification(conn, payload)
        if action == "inserted":
            inserted += 1
        elif action == "updated":
            updated += 1
        else:
            reused += 1
        buckets[payload["classification"]] = buckets.get(payload["classification"], 0) + 1
    after_reviews = before_reviews
    if _table_exists(conn, "company_duplicate_reviews"):
        after_reviews = int(conn.execute("SELECT COUNT(*) FROM company_duplicate_reviews").fetchone()[0])
    approvals = 0
    if _table_exists(conn, "merge_execution_approvals"):
        approvals = int(conn.execute("SELECT COUNT(*) FROM merge_execution_approvals").fetchone()[0])
    merge_history = 0
    if _table_exists(conn, "company_merge_history"):
        merge_history = int(conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0])
    return {
        "ok": True,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "writes_classification_metadata_only": True,
        "companies_mutated": False,
        "contacts_mutated": False,
        "ccrs_mutated": False,
        "human_reviews_mutated": after_reviews != before_reviews,
        "human_review_rows": after_reviews,
        "classifier_version": CLASSIFIER_VERSION,
        "classifier_kind": CLASSIFIER_KIND,
        "external_ai_used": False,
        "assessment_title": ASSESSMENT_TITLE,
        "candidates_analyzed": len(pairs),
        "inserted": inserted,
        "updated": updated,
        "reused": reused,
        "buckets": buckets,
        "merge_execution_approvals": approvals,
        "merge_history": merge_history,
        "second_opinion": maybe_second_opinion({"pairs": len(pairs)}, provider=None),
    }


def classify_pair_readonly(conn: sqlite3.Connection, company_a_id: int, company_b_id: int) -> dict[str, Any]:
    """Compute assessment without writing human dispositions or business rows."""
    ensure_duplicate_review_schema(conn)
    lo, hi = pair_ids(company_a_id, company_b_id)
    pairs = discover_duplicate_pairs(conn)
    found = next((p for p in pairs if p["pair_key"] == f"{lo}:{hi}"), None)
    if found is None:
        found = {
            "company_a_id": lo,
            "company_b_id": hi,
            "pair_key": f"{lo}:{hi}",
            "categories": [],
            "evidence_fingerprint": evidence_fingerprint(conn, lo, hi),
        }
    companies = _load_companies(conn)
    payload = build_pair_assessment(
        conn,
        found,
        companies=companies,
        aliases=_load_aliases(conn),
        identities=_load_identities(conn),
        locations=_load_locations(conn),
        contacts=_load_contacts_lite(conn),
        ccrs=_load_ccr_rows(conn),
        stats=_load_planning_stats(conn, {lo, hi}),
        persist=False,
    )
    payload["writes"] = False
    payload["human_disposition_written"] = False
    return payload


def empty_summary() -> dict[str, int]:
    summary = {key: 0 for key in CLASSIFICATION_BUCKETS}
    summary.update(
        {
            "candidates": 0,
            "classified": 0,
            "unclassified": 0,
            "stale_classification": 0,
            "review_needed": 0,
            "disagreement": 0,
            "survivor_decision_required": 0,
            "stale_human_review": 0,
            "human_reviewed": 0,
        }
    )
    return summary


def summarize_candidates(pairs: list[dict[str, Any]]) -> dict[str, int]:
    summary = empty_summary()
    summary["candidates"] = len(pairs)
    for row in pairs:
        classification = _blank(row.get("classification"))
        if classification in CLASSIFICATION_SET:
            summary[classification] += 1
            summary["classified"] += 1
        else:
            summary["unclassified"] += 1
        if row.get("classification_stale"):
            summary["stale_classification"] += 1
        if row.get("review_needed"):
            summary["review_needed"] += 1
        if row.get("disagreement"):
            summary["disagreement"] += 1
        if row.get("survivor_decision_required"):
            summary["survivor_decision_required"] += 1
        if row.get("stale"):
            summary["stale_human_review"] += 1
        if _blank(row.get("disposition")) not in {"", DISPOSITION_UNREVIEWED}:
            summary["human_reviewed"] += 1
    return summary


def enrich_candidate_rows(conn: sqlite3.Connection, pairs: list[dict[str, Any]]) -> None:
    stored = load_classifications(conn)
    for pair in pairs:
        key = pair["pair_key"]
        row = stored.get(key)
        human = _blank(pair.get("disposition")) or DISPOSITION_UNREVIEWED
        stale_human = bool(pair.get("stale"))
        if row is None:
            pair["classification"] = ""
            pair["confidence"] = ""
            pair["classification_stale"] = False
            pair["classifier_version"] = ""
            pair["auto_proposed_survivor_company_id"] = None
            pair["auto_proposed_source_company_id"] = None
            pair["survivor_decision_required"] = False
            pair["disagreement"] = False
            pair["review_needed"] = stale_human
            pair["review_needed_reasons"] = ["STALE_HUMAN_REVIEW"] if stale_human else []
            pair["same_client_ccr_conflict"] = False
            pair["assessment_title"] = ASSESSMENT_TITLE
            continue
        assessment = _assessment_from_row(
            row,
            current_fingerprint=_blank(pair.get("evidence_fingerprint")),
            human=human,
            stale_human=stale_human,
        )
        pair["classification"] = assessment["classification"]
        pair["confidence"] = assessment["confidence"]
        pair["classification_stale"] = bool(assessment["stale"])
        pair["classifier_version"] = assessment["classifier_version"]
        pair["auto_proposed_survivor_company_id"] = assessment["proposed_survivor_company_id"]
        pair["auto_proposed_source_company_id"] = assessment["proposed_source_company_id"]
        pair["survivor_decision_required"] = bool(assessment["survivor_decision_required"])
        pair["disagreement"] = bool(assessment["disagreement"])
        pair["review_needed"] = bool(assessment["review_needed"])
        pair["review_needed_reasons"] = assessment["review_needed_reasons"]
        pair["same_client_ccr_conflict"] = bool(assessment["same_client_ccr_conflict"])
        pair["assessment_title"] = ASSESSMENT_TITLE
        pair["why_summary"] = "; ".join(assessment.get("why") or [])
        pair["concern_summary"] = "; ".join(assessment.get("concern_labels") or [])


def filter_candidate_rows(
    pairs: list[dict[str, Any]],
    *,
    queue: str = "",
    disposition: str = "unreviewed",
    q: str = "",
) -> list[dict[str, Any]]:
    wanted = _blank(queue or disposition).lower() or "unreviewed"
    needle = _blank(q).lower()
    filtered: list[dict[str, Any]] = []
    for row in pairs:
        disp = _blank(row.get("disposition")).upper()
        stale = bool(row.get("stale"))
        classification = _blank(row.get("classification")).upper()
        if wanted in {"review_needed", "review-needed"}:
            if not row.get("review_needed"):
                continue
        elif wanted == "high_confidence":
            if classification != CLASS_HIGH or row.get("classification_stale"):
                continue
        elif wanted in {"likely_duplicate_auto", "likely_duplicate_class"}:
            if classification != CLASS_LIKELY or row.get("classification_stale"):
                continue
        elif wanted == "likely_multi_location":
            if classification != CLASS_MULTI or row.get("classification_stale"):
                continue
        elif wanted == "likely_not_duplicate":
            if classification != CLASS_NOT or row.get("classification_stale"):
                continue
        elif wanted == "human_review_required":
            if classification != CLASS_HUMAN or row.get("classification_stale"):
                continue
        elif wanted == "insufficient_evidence":
            if classification != CLASS_INSUFFICIENT or row.get("classification_stale"):
                continue
        elif wanted == "disagreement":
            if not row.get("disagreement"):
                continue
        elif wanted in {"unreviewed", ""}:
            if not stale and disp not in {DISPOSITION_UNREVIEWED, ""}:
                continue
        elif wanted == "all":
            pass
        elif wanted == "stale":
            if not stale:
                continue
        elif wanted == "likely_duplicate":
            if disp != DISPOSITION_LIKELY or stale:
                continue
        elif wanted == "not_duplicate":
            if disp != DISPOSITION_NOT:
                continue
        elif wanted == "multi_location":
            if disp != DISPOSITION_MULTI:
                continue
        elif wanted == "needs_research":
            if disp != DISPOSITION_RESEARCH or stale:
                continue
        elif wanted == "merge_candidate":
            if disp != DISPOSITION_MERGE or stale:
                continue
        elif wanted == "reviewed":
            if disp == DISPOSITION_UNREVIEWED:
                continue
        else:
            continue
        if needle:
            blob = " ".join(
                [
                    str(row["company_a"].get("company_name") or ""),
                    str(row["company_b"].get("company_name") or ""),
                    str(row["company_a"].get("external_record_no") or ""),
                    str(row["company_b"].get("external_record_no") or ""),
                    str(row.get("pair_key") or ""),
                    str(row.get("classification") or ""),
                ]
            ).lower()
            if needle not in blob:
                continue
        filtered.append(row)
    return filtered


def attach_assessment_to_detail(conn: sqlite3.Connection, detail: dict[str, Any]) -> dict[str, Any]:
    ensure_duplicate_review_schema(conn)
    lo, hi = pair_ids(detail["company_a"]["company_id"], detail["company_b"]["company_id"])
    stored = load_classifications(conn).get(f"{lo}:{hi}")
    human = _blank((detail.get("review") or {}).get("disposition")) or DISPOSITION_UNREVIEWED
    stale_human = bool((detail.get("review") or {}).get("stale"))
    fingerprint = _blank((detail.get("review") or {}).get("evidence_fingerprint"))
    if stored is None or (
        _blank(stored.get("evidence_fingerprint")) != fingerprint
        or _blank(stored.get("classifier_version")) != CLASSIFIER_VERSION
    ):
        computed = classify_pair_readonly(conn, lo, hi)
        assessment = {
            "assessment_title": ASSESSMENT_TITLE,
            "classifier_kind": CLASSIFIER_KIND,
            "external_ai_used": False,
            "classification": computed["classification"],
            "confidence": computed["confidence"],
            "classifier_version": CLASSIFIER_VERSION,
            "classified_at": "",
            "stale": bool(stored is not None),
            "persisted": False,
            "evidence": computed.get("evidence") or [],
            "concerns": computed.get("concerns") or [],
            "why": computed.get("why") or [],
            "concern_labels": computed.get("concern_labels") or [],
            "human_review_triggers": computed.get("human_review_triggers") or [],
            "same_client_ccr_conflict": bool(computed.get("same_client_ccr_conflict")),
            "material_ccr_conflict": bool(computed.get("material_ccr_conflict")),
            "proposed_survivor_company_id": computed.get("proposed_survivor_company_id"),
            "proposed_source_company_id": computed.get("proposed_source_company_id"),
            "survivor_decision_required": bool(computed.get("survivor_decision_required")),
            "survivor_reasons": computed.get("survivor_reasons") or [],
            "survivor_reason": computed.get("survivor_reason") or "",
            "evidence_fingerprint": computed.get("evidence_fingerprint") or fingerprint,
        }
    else:
        assessment = _assessment_from_row(
            stored,
            current_fingerprint=fingerprint,
            human=human,
            stale_human=stale_human,
        )
        assessment["persisted"] = True
    assessment["disagreement"] = classification_disagrees(assessment["classification"], human)
    assessment["review_needed_reasons"] = review_needed_reasons(
        classification=assessment["classification"],
        confidence=assessment["confidence"],
        human=human,
        stale_human=stale_human,
        stale_classification=bool(assessment.get("stale")),
        survivor_required=bool(assessment.get("survivor_decision_required")),
        disagreement=bool(assessment["disagreement"]),
    )
    assessment["review_needed"] = bool(assessment["review_needed_reasons"])
    detail["automated_assessment"] = assessment
    detail["human_review"] = detail.get("review")
    return detail


def _preview_fingerprint(action: str, eligible: list[str], reason: str) -> str:
    payload = {"action": action, "eligible": eligible, "reason": reason}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _resolve_batch_keys(conn: sqlite3.Connection, body: DuplicateBatchReviewRequest) -> list[str]:
    mode = _blank(body.selection_mode).lower() or SELECTION_EXPLICIT
    if mode not in {SELECTION_EXPLICIT, SELECTION_FILTERED}:
        raise DuplicateReviewError("invalid_selection_mode")
    pairs = discover_duplicate_pairs(conn)
    enrich_candidate_rows(conn, pairs)
    if mode == SELECTION_FILTERED:
        filtered = filter_candidate_rows(pairs, queue=body.queue, q=body.q)
        return [str(p["pair_key"]) for p in filtered]
    keys = []
    seen: set[str] = set()
    for raw in body.pair_keys or []:
        token = _blank(raw)
        if not token or token in seen:
            continue
        seen.add(token)
        keys.append(token)
    return keys


def preview_batch_review(
    conn: sqlite3.Connection, *, actor: NorthStarUser, body: DuplicateBatchReviewRequest
) -> dict[str, Any]:
    if not bool(getattr(actor, "is_administrator", False)):
        raise PermissionError("Not authorized.")
    ensure_duplicate_review_schema(conn)
    action = _blank(body.action).upper()
    if action in BATCH_FORBIDDEN or action == DISPOSITION_MERGE:
        raise DuplicateReviewError("batch_merge_candidate_forbidden")
    if action not in BATCH_ACTIONS:
        raise DuplicateReviewError("invalid_batch_action", {"action": action})
    reason = _blank(body.reason)
    keys = _resolve_batch_keys(conn, body)
    pairs = {p["pair_key"]: p for p in discover_duplicate_pairs(conn)}
    stored = load_classifications(conn)
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for key in keys:
        pair = pairs.get(key)
        if pair is None:
            excluded.append({"pair_key": key, "reason": "not_a_current_candidate"})
            continue
        row = stored.get(key)
        if row is None:
            excluded.append({"pair_key": key, "reason": "not_classified", "requires_individual_review": True})
            continue
        current_fp = _blank(pair.get("evidence_fingerprint"))
        stored_fp = _blank(row.get("evidence_fingerprint"))
        if stored_fp != current_fp or row.get("stale_at") or _blank(row.get("classifier_version")) != CLASSIFIER_VERSION:
            excluded.append(
                {
                    "pair_key": key,
                    "reason": "stale_classification",
                    "requires_individual_review": True,
                    "company_a": pair.get("company_a"),
                    "company_b": pair.get("company_b"),
                }
            )
            continue
        eligible.append(
            {
                "pair_key": key,
                "company_a_id": pair["company_a_id"],
                "company_b_id": pair["company_b_id"],
                "company_a": pair.get("company_a"),
                "company_b": pair.get("company_b"),
                "old_disposition": _blank(pair.get("disposition")) or DISPOSITION_UNREVIEWED,
                "new_disposition": BATCH_ACTIONS[action],
                "classification": _blank(row.get("classification")),
                "classifier_version": CLASSIFIER_VERSION,
                "evidence_fingerprint": stored_fp,
            }
        )
    eligible_keys = [row["pair_key"] for row in eligible]
    return {
        "ok": True,
        "writes": False,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "action": action,
        "new_disposition": BATCH_ACTIONS[action],
        "reason": reason,
        "reason_ok": len(reason) >= 3,
        "selected": len(keys),
        "eligible": len(eligible),
        "excluded": len(excluded),
        "excluded_rows": excluded,
        "eligible_rows": eligible,
        "confirm_allowed": bool(eligible) and len(reason) >= 3,
        "preview_fingerprint": _preview_fingerprint(action, eligible_keys, reason),
        "batch_merge_candidate": False,
        "no_merge_button": True,
    }


def confirm_batch_review(
    conn: sqlite3.Connection, *, actor: NorthStarUser, body: DuplicateBatchReviewRequest
) -> dict[str, Any]:
    if not bool(getattr(actor, "is_administrator", False)):
        raise PermissionError("Not authorized.")
    if not body.confirm:
        raise DuplicateReviewError("confirmation_required")
    preview = preview_batch_review(conn, actor=actor, body=body)
    if not preview["reason_ok"]:
        raise DuplicateReviewError("reason_required")
    if _blank(body.preview_fingerprint) != _blank(preview.get("preview_fingerprint")):
        raise DuplicateReviewError("preview_stale")
    if not preview["eligible"]:
        raise DuplicateReviewError("nothing_eligible")
    disposition = preview["new_disposition"]
    saved = 0
    for row in preview["eligible_rows"]:
        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=int(row["company_a_id"]),
            company_b_id=int(row["company_b_id"]),
            body=DuplicateReviewSaveRequest(
                disposition=disposition,
                reason=_blank(body.reason),
            ),
            review_source=REVIEW_SOURCE_BATCH,
            automated_classification=_blank(row.get("classification")),
            classifier_version=CLASSIFIER_VERSION,
        )
        saved += 1
    approvals = 0
    if _table_exists(conn, "merge_execution_approvals"):
        approvals = int(conn.execute("SELECT COUNT(*) FROM merge_execution_approvals").fetchone()[0])
    return {
        "ok": True,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "merge_execution_approvals": approvals,
        "action": preview["action"],
        "new_disposition": disposition,
        "selected": preview["selected"],
        "eligible": preview["eligible"],
        "excluded": preview["excluded"],
        "saved": saved,
        "excluded_rows": preview["excluded_rows"],
        "review_source": REVIEW_SOURCE_BATCH,
        "classifier_version": CLASSIFIER_VERSION,
        "companies_mutated": False,
        "contacts_mutated": False,
        "ccrs_mutated": False,
        "no_merge_button": True,
    }
