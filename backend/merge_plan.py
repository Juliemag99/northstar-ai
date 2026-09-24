"""DS-13 read-only automated merge planning and exception review.

Prepares explainable future merge plans for HIGH_CONFIDENCE_DUPLICATE pairs.
Never executes a merge, never creates merge_execution_approvals, and never
mutates Master Company / CCR / contact / alias / location / identity rows.

Reuses plan_company_merge. Does not load the live merge execution module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from company_merges import plan_company_merge
from duplicate_classify import CLASS_HIGH, CLASSIFIER_VERSION
from duplicate_review import (
    DISPOSITION_LIKELY,
    DISPOSITION_MERGE,
    DISPOSITION_MULTI,
    DISPOSITION_NOT,
    DISPOSITION_RESEARCH,
    DISPOSITION_UNREVIEWED,
    DuplicateReviewError,
    _blank,
    _now,
    _row_dict,
    _table_exists,
    ensure_duplicate_review_schema,
    pair_ids,
    pair_key,
)
from import_brown_industries import digits_phone, domain, norm_addr
from models import NorthStarUser

PLANNER_VERSION = "DS13_PLANNER_V1"
PLAN_ONLY_WARNING = "PLANNING ONLY — NO MERGE WILL OCCUR"
FUTURE_HANDOFF = (
    "A future merge execution phase may require ALL of: current "
    "HIGH_CONFIDENCE_DUPLICATE or explicit human Merge Candidate; current "
    "non-stale merge plan; all exceptions resolved; explicit Julie merge "
    "approval; source/survivor-bound fingerprint; one-time approval; final "
    "execution preview. DS-13 implements none of that execution."
)

STATE_READY = "READY_FOR_HUMAN_APPROVAL"
STATE_NEEDS = "NEEDS_EXCEPTION_DECISION"
STATE_NOT_SAFE = "NOT_SAFE_TO_PLAN"
STATE_STALE = "STALE"
PLAN_STATES = frozenset({STATE_READY, STATE_NEEDS, STATE_NOT_SAFE, STATE_STALE})

UI_STATE = {
    STATE_READY: "READY FOR REVIEW",
    STATE_NEEDS: "NEEDS DECISIONS",
    STATE_NOT_SAFE: "NOT SAFE",
    STATE_STALE: "STALE",
}

ACTION_KEEP_SURVIVOR = "KEEP_SURVIVOR"
ACTION_FILL_BLANK = "FILL_SURVIVOR_BLANK_FROM_SOURCE"
ACTION_PRESERVE_ALIAS = "PRESERVE_SOURCE_AS_ALIAS"
ACTION_PRESERVE_IDENTITY = "PRESERVE_SOURCE_AS_IDENTITY"
ACTION_PRESERVE_LOCATION = "PRESERVE_AS_LOCATION"
ACTION_FIELD_CONFLICT = "FIELD_CONFLICT_REVIEW"

CCR_SAFE = "CCR_SAFE_RECONCILIATION"
CCR_DECISION = "CCR_DECISION_REQUIRED"

CONTACT_PRESERVE = "PRESERVE_UNIQUE"
CONTACT_EXACT = "EXACT_DUPLICATE_CONTACT"
CONTACT_POSSIBLE = "POSSIBLE_DUPLICATE_CONTACT"
CONTACT_CONFLICT = "CONFLICT"

EX_SURVIVOR = "SURVIVOR_DECISION_REQUIRED"
EX_FIELD = "FIELD_CONFLICT_REVIEW"
EX_IDENTITY = "IDENTITY_INCOMPATIBLE"
EX_STATUS = "STATUS_DECISION_REQUIRED"
EX_REP = "ASSIGNED_REP_DECISION_REQUIRED"
EX_HOT = "HOT_STATE_DECISION_REQUIRED"
EX_FOLLOWUP = "FOLLOWUP_DECISION_REQUIRED"
EX_NEXT = "NEXT_ACTION_DECISION_REQUIRED"
EX_RN = "EXTERNAL_RN_DECISION_REQUIRED"
EX_CAMPAIGN = "CAMPAIGN_RECONCILIATION_REQUIRED"
EX_CONTACT = "CONTACT_DECISION_REQUIRED"
EX_MULTI = "MULTI_LOCATION_IDENTITY_CONCERN"

NEVER_AUTO_PLAN = frozenset(
    {DISPOSITION_NOT, DISPOSITION_MULTI, DISPOSITION_RESEARCH, DISPOSITION_LIKELY}
)
MASTER_FIELDS = (
    "company_name",
    "address",
    "city",
    "state",
    "zip",
    "phone",
    "phone_extension",
    "website",
    "external_record_no",
    "archive_state",
)


class MergePlanError(DuplicateReviewError):
    """Planning-only error. Mapped like DuplicateReviewError."""


class MergePlanDecisionRequest(BaseModel):
    exception_key: str
    chosen_resolution: str
    reason: str = ""
    actor_id: int | None = None
    created_by: str = ""


class MergePlanPrepareRequest(BaseModel):
    actor_id: int | None = None
    created_by: str = ""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_merge_plan_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent planning tables. Does not insert plans or decisions."""
    ensure_duplicate_review_schema(conn)
    stats = {"created_company_merge_plans": 0, "created_company_merge_plan_decisions": 0}
    if not _table_exists(conn, "companies"):
        return stats
    existed_plans = _table_exists(conn, "company_merge_plans")
    existed_decisions = _table_exists(conn, "company_merge_plan_decisions")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_merge_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_key TEXT NOT NULL UNIQUE,
            company_a_id INTEGER NOT NULL,
            company_b_id INTEGER NOT NULL,
            source_company_id INTEGER,
            survivor_company_id INTEGER,
            plan_state TEXT NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '{}',
            exceptions_json TEXT NOT NULL DEFAULT '[]',
            plan_fingerprint TEXT NOT NULL DEFAULT '',
            classification_fingerprint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            planner_version TEXT NOT NULL DEFAULT 'DS13_PLANNER_V1',
            CHECK (company_a_id < company_b_id),
            CHECK (plan_state IN (
                'READY_FOR_HUMAN_APPROVAL',
                'NEEDS_EXCEPTION_DECISION',
                'NOT_SAFE_TO_PLAN',
                'STALE'
            )),
            FOREIGN KEY (company_a_id) REFERENCES companies(id),
            FOREIGN KEY (company_b_id) REFERENCES companies(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_merge_plan_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            pair_key TEXT NOT NULL,
            exception_key TEXT NOT NULL,
            chosen_resolution TEXT NOT NULL,
            resolution_json TEXT NOT NULL DEFAULT '{}',
            actor_user_id INTEGER,
            created_at TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            evidence_fingerprint TEXT NOT NULL DEFAULT '',
            superseded_at TEXT,
            FOREIGN KEY (plan_id) REFERENCES company_merge_plans(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_merge_plans_state ON company_merge_plans(plan_state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_merge_plan_decisions_plan ON company_merge_plan_decisions(plan_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_merge_plan_decisions_key ON company_merge_plan_decisions(pair_key, exception_key, id)"
    )
    if not existed_plans:
        stats["created_company_merge_plans"] = 1
    if not existed_decisions:
        stats["created_company_merge_plan_decisions"] = 1
    return stats


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _as_int(value: object | None) -> int | None:
    if value is None or _blank(value) == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: object | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"", "0", "false", "no", "n"}:
        return False
    return True


def _ccr_active(row: dict[str, Any]) -> bool:
    return not bool(_blank(row.get("archived_at")))


def _norm_text(value: object | None) -> str:
    return " ".join(_blank(value).lower().split())


def _norm_field(field: str, value: object | None) -> str:
    raw = _blank(value)
    if not raw:
        return ""
    if field == "phone":
        return digits_phone(raw) or raw.lower()
    if field == "website":
        return domain(raw) or raw.lower()
    if field == "address":
        return norm_addr(raw) or raw.lower()
    if field == "zip":
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits[:5] if digits else raw.lower()
    if field in {"city", "state", "company_name"}:
        return _norm_text(raw)
    if field == "archive_state":
        return "archived" if raw else "active"
    return _norm_text(raw)


def _company_row(conn: sqlite3.Connection, company_id: int) -> dict[str, Any]:
    cols = _table_columns(conn, "companies")
    select = ["id", "company_name"]
    for name in (
        "address",
        "city",
        "state",
        "zip",
        "website",
        "external_record_no",
        "archived_at",
        "created_at",
        "updated_at",
    ):
        if name in cols:
            select.append(name)
    if "phone" in cols:
        select.append("phone")
    elif "legacy_phone" in cols:
        select.append("legacy_phone AS phone")
    if "phone_extension" in cols:
        select.append("phone_extension")
    elif "legacy_phone_extension" in cols:
        select.append("legacy_phone_extension AS phone_extension")
    row = conn.execute(
        f"SELECT {', '.join(select)} FROM companies WHERE id = ?",
        (int(company_id),),
    ).fetchone()
    if row is None:
        return {}
    item = _row_dict(row)
    item["archive_state"] = "archived" if _blank(item.get("archived_at")) else "active"
    item["archived"] = bool(_blank(item.get("archived_at")))
    return item


def _load_classification(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not _table_exists(conn, "company_duplicate_classifications"):
        return {}
    row = conn.execute(
        "SELECT * FROM company_duplicate_classifications WHERE pair_key=?",
        (key,),
    ).fetchone()
    return _row_dict(row)


def _load_review(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not _table_exists(conn, "company_duplicate_reviews"):
        return {}
    row = conn.execute(
        "SELECT * FROM company_duplicate_reviews WHERE pair_key=?",
        (key,),
    ).fetchone()
    return _row_dict(row)


def _load_ccrs(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "client_company_relationships"):
        return []
    cols = _table_columns(conn, "client_company_relationships")
    extra = []
    for name in (
        "status",
        "assigned_user_id",
        "is_hot",
        "follow_up_date",
        "next_action",
        "external_record_no",
        "notes",
        "archived_at",
        "priority",
        "location_id",
    ):
        if name in cols:
            extra.append(f"r.{name}")
    sql = f"""
        SELECT r.id, r.client_id, r.company_id, c.code AS client_code, c.name AS client_name
               {"".join(f", {col}" for col in extra)}
        FROM client_company_relationships r
        JOIN clients c ON c.id = r.client_id
        WHERE r.company_id = ?
        ORDER BY r.client_id, r.id
    """
    rows = [_row_dict(r) for r in conn.execute(sql, (int(company_id),)).fetchall()]
    for row in rows:
        row["active"] = _ccr_active(row)
        row["hot"] = _truthy(row.get("is_hot"))
    return rows


def _count_where(conn: sqlite3.Connection, table: str, where: str, args: tuple) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0])


def _history_counts(conn: sqlite3.Connection, company_id: int) -> dict[str, int]:
    cid = int(company_id)
    contacts = 0
    if _table_exists(conn, "contacts"):
        contact_ids = [
            int(r[0])
            for r in conn.execute("SELECT id FROM contacts WHERE company_id=?", (cid,)).fetchall()
        ]
        contacts = len(contact_ids)
    else:
        contact_ids = []
    activities = _count_where(conn, "activities", "company_id=?", (cid,))
    if not activities and contact_ids:
        placeholders = ",".join("?" * len(contact_ids))
        activities = _count_where(
            conn, "activities", f"contact_id IN ({placeholders})", tuple(contact_ids)
        )
    return {
        "notes": _count_where(conn, "legacy_notes", "company_id=?", (cid,)),
        "activities": activities,
        "appointments": _count_where(conn, "appointments", "company_id=?", (cid,)),
        "tasks_follow_ups": _count_where(
            conn,
            "client_company_relationships",
            "company_id=? AND TRIM(COALESCE(follow_up_date,'')) != ''",
            (cid,),
        ),
        "research": _count_where(conn, "company_research_findings", "company_id=?", (cid,)),
        "sales_events": _count_where(conn, "client_sales_events", "company_id=?", (cid,)),
        "campaigns": _count_where(conn, "campaign_companies", "company_id=?", (cid,)),
        "provenance": _count_where(
            conn,
            "field_provenance_events",
            "entity_type='company' AND entity_id=?",
            (cid,),
        ),
        "contacts": contacts,
    }


def _load_active_decisions(conn: sqlite3.Connection, plan_id: int | None) -> dict[str, dict[str, Any]]:
    if not plan_id or not _table_exists(conn, "company_merge_plan_decisions"):
        return {}
    rows = conn.execute(
        """
        SELECT * FROM company_merge_plan_decisions
        WHERE plan_id=? AND TRIM(COALESCE(superseded_at,'')) = ''
        ORDER BY id
        """,
        (int(plan_id),),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _row_dict(row)
        out[str(item["exception_key"])] = item
    return out


def _decision_for(decisions: dict[str, dict[str, Any]], key: str) -> str:
    row = decisions.get(key) or {}
    return _blank(row.get("chosen_resolution"))


def _resolved(decisions: dict[str, dict[str, Any]], key: str) -> bool:
    return bool(_decision_for(decisions, key))


def eligibility_for_pair(
    classification: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    human = _blank(review.get("disposition")) or DISPOSITION_UNREVIEWED
    auto = _blank(classification.get("classification"))
    eligible = auto == CLASS_HIGH
    blocked_reason = ""
    if auto != CLASS_HIGH:
        eligible = False
        blocked_reason = "not_high_confidence"
    elif human in {DISPOSITION_NOT, DISPOSITION_MULTI}:
        eligible = False
        blocked_reason = "human_disposition_forbids_planning"
    elif human == DISPOSITION_RESEARCH:
        eligible = False
        blocked_reason = "needs_research"
    elif human == DISPOSITION_LIKELY:
        eligible = False
        blocked_reason = "likely_duplicate_review_only"
    disagreement = False
    if auto == CLASS_HIGH and human in NEVER_AUTO_PLAN:
        disagreement = True
    if human == DISPOSITION_MERGE and auto and auto != CLASS_HIGH:
        eligible = False
        blocked_reason = "merge_candidate_not_high_confidence"
        disagreement = True
    return {
        "eligible": eligible,
        "blocked_reason": blocked_reason,
        "human_disposition": human,
        "classification": auto or None,
        "disagreement": disagreement,
        "human_authoritative": True,
    }


def _choose_survivor(
    lo: int,
    hi: int,
    classification: dict[str, Any],
    review: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    decision = _decision_for(decisions, EX_SURVIVOR)
    if decision.upper().startswith("SURVIVOR:"):
        chosen = _as_int(decision.split(":", 1)[1])
        if chosen in {lo, hi}:
            other = hi if chosen == lo else lo
            return {
                "survivor_company_id": chosen,
                "source_company_id": other,
                "survivor_decision_required": False,
                "survivor_source": "EXCEPTION_DECISION",
            }
    human = _blank(review.get("disposition"))
    human_survivor = _as_int(review.get("proposed_survivor_company_id"))
    human_source = _as_int(review.get("proposed_source_company_id"))
    if human == DISPOSITION_MERGE and human_survivor in {lo, hi}:
        source = human_source if human_source in {lo, hi} and human_source != human_survivor else (
            hi if human_survivor == lo else lo
        )
        return {
            "survivor_company_id": human_survivor,
            "source_company_id": source,
            "survivor_decision_required": False,
            "survivor_source": "HUMAN_MERGE_CANDIDATE",
        }
    auto_survivor = _as_int(classification.get("proposed_survivor_company_id"))
    auto_source = _as_int(classification.get("proposed_source_company_id"))
    if auto_survivor in {lo, hi}:
        source = auto_source if auto_source in {lo, hi} and auto_source != auto_survivor else (
            hi if auto_survivor == lo else lo
        )
        return {
            "survivor_company_id": auto_survivor,
            "source_company_id": source,
            "survivor_decision_required": False,
            "survivor_source": "DS12_PROPOSAL",
        }
    return {
        "survivor_company_id": None,
        "source_company_id": None,
        "survivor_decision_required": True,
        "survivor_source": None,
    }


def _exception(
    key: str,
    code: str,
    label: str,
    *,
    client_id: int | None = None,
    field: str | None = None,
    options: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "exception_key": key,
        "code": code,
        "label": label,
        "client_id": client_id,
        "field": field,
        "options": options or [],
        "details": details or {},
        "plan_decision_only": True,
        "mutates_business_data": False,
    }


def _plan_master_fields(
    survivor: dict[str, Any],
    source: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    for field in MASTER_FIELDS:
        s_val = _blank(survivor.get(field))
        t_val = _blank(source.get(field))
        s_norm = _norm_field(field, s_val)
        t_norm = _norm_field(field, t_val)
        action = ACTION_KEEP_SURVIVOR
        exception_key = f"{EX_FIELD}:{field}"
        chosen = _decision_for(decisions, exception_key)
        if not t_norm or s_norm == t_norm:
            action = ACTION_KEEP_SURVIVOR
        elif not s_norm and t_norm:
            action = ACTION_FILL_BLANK
        elif field == "company_name":
            action = ACTION_PRESERVE_ALIAS
        elif field == "external_record_no":
            action = ACTION_PRESERVE_IDENTITY
        elif field == "archive_state":
            action = ACTION_FIELD_CONFLICT
        elif field in {"address", "city", "state", "zip"}:
            same_city = _norm_field("city", survivor.get("city")) and (
                _norm_field("city", survivor.get("city")) == _norm_field("city", source.get("city"))
            )
            same_state = _norm_field("state", survivor.get("state")) and (
                _norm_field("state", survivor.get("state"))
                == _norm_field("state", source.get("state"))
            )
            if field == "address" and same_city and same_state and t_norm and s_norm != t_norm:
                action = ACTION_PRESERVE_LOCATION
            elif field in {"city", "state"} and s_norm != t_norm:
                action = ACTION_FIELD_CONFLICT
            elif s_norm != t_norm:
                action = ACTION_FIELD_CONFLICT
        else:
            action = ACTION_FIELD_CONFLICT
        if chosen:
            mapped = {
                "KEEP_SURVIVOR": ACTION_KEEP_SURVIVOR,
                "FILL_FROM_SOURCE": ACTION_FILL_BLANK,
                ACTION_FILL_BLANK: ACTION_FILL_BLANK,
                ACTION_PRESERVE_ALIAS: ACTION_PRESERVE_ALIAS,
                ACTION_PRESERVE_IDENTITY: ACTION_PRESERVE_IDENTITY,
                ACTION_PRESERVE_LOCATION: ACTION_PRESERVE_LOCATION,
                "PRESERVE_SOURCE_AS_ALIAS/IDENTITY": ACTION_PRESERVE_IDENTITY,
            }
            action = mapped.get(chosen, ACTION_KEEP_SURVIVOR if chosen == ACTION_KEEP_SURVIVOR else action)
        row = {
            "field": field,
            "survivor_value": s_val,
            "source_value": t_val,
            "action": action,
            "overwrite_nonblank_survivor": False,
        }
        rows.append(row)
        if action == ACTION_FIELD_CONFLICT and not chosen:
            exceptions.append(
                _exception(
                    exception_key,
                    EX_FIELD,
                    f"Master field {field} differs and cannot be overwritten automatically.",
                    field=field,
                    options=[
                        ACTION_KEEP_SURVIVOR,
                        ACTION_PRESERVE_ALIAS,
                        ACTION_PRESERVE_IDENTITY,
                        ACTION_PRESERVE_LOCATION,
                    ],
                    details={"survivor": s_val, "source": t_val},
                )
            )
    return rows, exceptions


def _plan_identities_safe(
    merge: dict[str, Any],
    survivor: dict[str, Any],
    source: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    identities = merge.get("source_identities") or {}
    src_rn = _blank(source.get("external_record_no"))
    dst_rn = _blank(survivor.get("external_record_no"))
    incompatible = False
    reasons: list[str] = []
    move = identities.get("move") or []
    already = identities.get("already_present") or []
    if src_rn and dst_rn and src_rn != dst_rn:
        reasons.append("Both master records have distinct LeadMaster RNs; both must be preserved as identities.")
    plan = {
        "never_drop_source_rn": True,
        "never_drop_aliases": True,
        "never_drop_identities": True,
        "never_drop_provenance": True,
        "source_master_rn": src_rn,
        "survivor_master_rn": dst_rn,
        "move_identities": len(move),
        "already_on_survivor": len(already),
        "preserve_source_rn_as_identity": bool(src_rn),
        "incompatible": incompatible,
        "reasons": reasons,
        "move": [
            {
                "id": row.get("id"),
                "source_system": row.get("source_system"),
                "source_record_no": row.get("source_record_no"),
                "source_company_name": row.get("source_company_name"),
                "plan": row.get("plan"),
            }
            for row in move
        ],
        "already_present": [
            {
                "id": row.get("id"),
                "source_system": row.get("source_system"),
                "source_record_no": row.get("source_record_no"),
                "plan": row.get("plan"),
            }
            for row in already
        ],
    }
    exceptions: list[dict[str, Any]] = []
    if incompatible:
        exceptions.append(
            _exception(
                EX_IDENTITY,
                EX_IDENTITY,
                "Two incompatible source identities cannot safely coexist.",
                options=["PRESERVE_BOTH", "KEEP_SEPARATE"],
            )
        )
    return plan, exceptions


def _plan_aliases_safe(merge: dict[str, Any], source_name: str) -> dict[str, Any]:
    aliases = merge.get("aliases") or {}
    move = aliases.get("move") or []
    skip = aliases.get("skip_duplicate") or []
    preserve_name = bool(_blank(source_name))
    return {
        "unique_to_preserve": len(move) + (1 if preserve_name else 0),
        "exact_normalized_duplicates": len(skip),
        "preserve_source_canonical_as_alias": preserve_name,
        "no_alias_disappears": True,
        "move": [{"id": r.get("id"), "alias_name": r.get("alias_name"), "plan": r.get("plan")} for r in move],
        "skip_duplicate": [
            {"id": r.get("id"), "alias_name": r.get("alias_name"), "plan": r.get("plan")} for r in skip
        ],
    }


def _plan_campaigns_safe(
    merge: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    campaigns = merge.get("campaigns") or {}
    union_move = campaigns.get("union_move") or []
    already = campaigns.get("already_member") or []
    exceptions: list[dict[str, Any]] = []
    ambiguous = []
    for row in already:
        src_notes = _norm_text(row.get("notes"))
        key = f"{EX_CAMPAIGN}:{int(row.get('campaign_id') or 0)}"
        if src_notes and not _resolved(decisions, key):
            # Distinct notes are preserved; only flag if membership state is otherwise ambiguous.
            pass
        ambiguous.append(row)
    plan = {
        "unique_memberships_to_preserve": len(union_move),
        "same_campaign_already_on_survivor": len(already),
        "dedupe_same_client_same_campaign": True,
        "union_move": [
            {
                "campaign_id": r.get("campaign_id"),
                "client_id": r.get("client_id"),
                "plan": r.get("plan"),
            }
            for r in union_move
        ],
        "already_member": [
            {
                "campaign_id": r.get("campaign_id"),
                "client_id": r.get("client_id"),
                "plan": r.get("plan"),
            }
            for r in already
        ],
    }
    return plan, exceptions


def _location_plan(
    merge: dict[str, Any],
    survivor: dict[str, Any],
    source: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    locations = merge.get("locations") or {}
    comparisons = locations.get("comparisons") or []
    source_locs = locations.get("source_locations") or []
    survivor_locs = locations.get("survivor_locations") or []
    classes = {c.get("classification") for c in comparisons}
    confirmed = _decision_for(decisions, EX_MULTI) in {
        ACTION_PRESERVE_LOCATION,
        "CONFIRM_PRESERVE_AS_LOCATION",
    }
    keep_separate = _decision_for(decisions, EX_MULTI) in {"KEEP_SEPARATE", "NOT_DUPLICATE"}
    not_safe: list[str] = []
    exceptions: list[dict[str, Any]] = []
    intended = []
    src_city = _norm_field("city", source.get("city"))
    dst_city = _norm_field("city", survivor.get("city"))
    src_state = _norm_field("state", source.get("state"))
    dst_state = _norm_field("state", survivor.get("state"))
    different_city = bool(src_city and dst_city and src_city != dst_city)
    different_state = bool(src_state and dst_state and src_state != dst_state)
    strong_multi = "DISTINCT_SITE" in classes or different_city or different_state
    if strong_multi and not confirmed:
        not_safe.append(EX_MULTI)
        exceptions.append(
            _exception(
                EX_MULTI,
                EX_MULTI,
                "Location evidence suggests these may be distinct manufacturing/sites, not a safe master merge.",
                options=["CONFIRM_PRESERVE_AS_LOCATION", "KEEP_SEPARATE"],
                details={"comparisons": comparisons, "source_city": source.get("city"), "survivor_city": survivor.get("city")},
            )
        )
    if keep_separate:
        not_safe.append(EX_MULTI)
    for loc in source_locs:
        intended.append(
            {
                "action": ACTION_PRESERVE_LOCATION,
                "location_id": loc.get("id"),
                "label": _blank(loc.get("location_name"))
                or f"{_blank(loc.get('city'))} {_blank(loc.get('state'))}".strip(),
            }
        )
    src_addr = _norm_field("address", source.get("address"))
    dst_addr = _norm_field("address", survivor.get("address"))
    if src_addr and src_addr != dst_addr and not source_locs:
        intended.append(
            {
                "action": ACTION_PRESERVE_LOCATION,
                "location_id": None,
                "label": f"{_blank(source.get('address'))} {_blank(source.get('city'))} {_blank(source.get('state'))}".strip(),
                "from_company_master": True,
            }
        )
    plan = {
        "source_location_count": len(source_locs),
        "survivor_location_count": len(survivor_locs),
        "comparisons": comparisons,
        "preserve_distinct_valid_locations": True,
        "intended": intended,
        "strong_multi_location_concern": strong_multi and not confirmed,
        "confirmed_preserve_as_location": confirmed,
    }
    return plan, exceptions, not_safe


def _contact_plan(
    merge: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contacts = merge.get("contacts") or {}
    rows = []
    exceptions: list[dict[str, Any]] = []
    for item in contacts.get("classifications") or []:
        action = _blank(item.get("action"))
        conflicts = item.get("field_conflicts") or []
        src_id = item.get("source_contact_id")
        tgt_id = item.get("survivor_contact_id")
        key = f"{EX_CONTACT}:{src_id}:{tgt_id or 0}"
        chosen = _decision_for(decisions, key)
        if action == "MOVE" or action == "KEEP_SEPARATE":
            klass = CONTACT_PRESERVE
            consolidation = None
        elif action == "MERGE":
            identity_only = bool(conflicts) and all(
                _blank(c.get("field")) in {"external_record_no", "source", "zoominfo_contact_id"}
                for c in conflicts
            )
            if not conflicts or identity_only:
                klass = CONTACT_EXACT
                consolidation = "SAFE_CONTACT_CONSOLIDATION"
            else:
                klass = CONTACT_CONFLICT
                consolidation = None
        elif action == "POSSIBLE":
            klass = CONTACT_POSSIBLE
            consolidation = None
        else:
            klass = CONTACT_PRESERVE
            consolidation = None
        if chosen == "CONSOLIDATE":
            consolidation = "SAFE_CONTACT_CONSOLIDATION"
            klass = CONTACT_EXACT if klass != CONTACT_CONFLICT else klass
        elif chosen == "KEEP_SEPARATE":
            klass = CONTACT_PRESERVE
            consolidation = None
        needs_decision = klass in {CONTACT_POSSIBLE, CONTACT_CONFLICT} and not chosen
        if needs_decision:
            exceptions.append(
                _exception(
                    key,
                    EX_CONTACT,
                    "Contact pair is not a deterministic exact duplicate.",
                    options=["CONSOLIDATE", "KEEP_SEPARATE"],
                    details={
                        "source_contact_id": src_id,
                        "survivor_contact_id": tgt_id,
                        "source_name": item.get("source_name"),
                        "survivor_name": item.get("survivor_name"),
                        "reasons": item.get("reasons") or [],
                        "field_conflicts": conflicts,
                    },
                )
            )
        rows.append(
            {
                "classification": klass,
                "consolidation": consolidation,
                "source_contact_id": src_id,
                "survivor_contact_id": tgt_id,
                "source_name": item.get("source_name"),
                "survivor_name": item.get("survivor_name"),
                "reasons": item.get("reasons") or [],
                "field_conflicts": conflicts,
                "campaign_conflicts": item.get("campaign_conflicts") or [],
            }
        )
    plan = {
        "source_contact_count": contacts.get("source_contact_count") or 0,
        "survivor_contact_count": contacts.get("survivor_contact_count") or 0,
        "unique_to_preserve": sum(1 for r in rows if r["classification"] == CONTACT_PRESERVE),
        "exact_duplicates": sum(1 for r in rows if r["classification"] == CONTACT_EXACT),
        "possible_duplicates": sum(1 for r in rows if r["classification"] == CONTACT_POSSIBLE),
        "conflicts": sum(1 for r in rows if r["classification"] == CONTACT_CONFLICT),
        "rows": rows,
        "no_contact_merge_now": True,
    }
    return plan, exceptions


def _campaigns_for_company(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "campaign_companies"):
        return []
    sql = """
        SELECT cc.campaign_id, cc.client_id, cc.company_id, cc.notes,
               camp.name AS campaign_name
        FROM campaign_companies cc
        LEFT JOIN client_campaigns camp ON camp.id = cc.campaign_id
        WHERE cc.company_id = ?
    """
    if "name" not in _table_columns(conn, "client_campaigns"):
        sql = """
            SELECT campaign_id, client_id, company_id, notes
            FROM campaign_companies WHERE company_id = ?
        """
    return [_row_dict(r) for r in conn.execute(sql, (int(company_id),)).fetchall()]


def _ccr_plan(
    conn: sqlite3.Connection,
    source_id: int,
    survivor_id: int,
    decisions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_ccrs = _load_ccrs(conn, source_id)
    survivor_ccrs = _load_ccrs(conn, survivor_id)
    source_camps = _campaigns_for_company(conn, source_id)
    survivor_camps = _campaigns_for_company(conn, survivor_id)
    by_client: dict[int, dict[str, dict[str, Any]]] = {}
    for row in source_ccrs:
        by_client.setdefault(int(row["client_id"]), {})["source"] = row
    for row in survivor_ccrs:
        by_client.setdefault(int(row["client_id"]), {})["survivor"] = row
    rows: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []

    def _opt(keep_src: str, keep_tgt: str) -> list[str]:
        return ["KEEP_SURVIVOR", "KEEP_SOURCE", keep_src, keep_tgt]

    for client_id, sides in sorted(by_client.items()):
        src = sides.get("source")
        tgt = sides.get("survivor")
        client_name = _blank((src or tgt or {}).get("client_name"))
        client_code = _blank((src or tgt or {}).get("client_code"))
        if src and not tgt:
            rows.append(
                {
                    "client_id": client_id,
                    "client_name": client_name,
                    "client_code": client_code,
                    "same_client": False,
                    "classification": CCR_SAFE,
                    "action": "REHOME_SOURCE_CCR_TO_SURVIVOR",
                    "source_ccr_id": src.get("id"),
                    "survivor_ccr_id": None,
                    "fields": {},
                    "exceptions": [],
                }
            )
            continue
        if tgt and not src:
            rows.append(
                {
                    "client_id": client_id,
                    "client_name": client_name,
                    "client_code": client_code,
                    "same_client": False,
                    "classification": CCR_SAFE,
                    "action": "KEEP_SURVIVOR_CCR",
                    "source_ccr_id": None,
                    "survivor_ccr_id": tgt.get("id"),
                    "fields": {},
                    "exceptions": [],
                }
            )
            continue
        assert src is not None and tgt is not None
        src_active = bool(src.get("active"))
        tgt_active = bool(tgt.get("active"))
        field_map = {
            "status": (_blank(src.get("status")), _blank(tgt.get("status")), EX_STATUS),
            "assigned_user_id": (
                _blank(src.get("assigned_user_id")),
                _blank(tgt.get("assigned_user_id")),
                EX_REP,
            ),
            "hot": (str(bool(src.get("hot"))), str(bool(tgt.get("hot"))), EX_HOT),
            "follow_up_date": (
                _blank(src.get("follow_up_date")),
                _blank(tgt.get("follow_up_date")),
                EX_FOLLOWUP,
            ),
            "next_action": (_blank(src.get("next_action")), _blank(tgt.get("next_action")), EX_NEXT),
            "external_record_no": (
                _blank(src.get("external_record_no")),
                _blank(tgt.get("external_record_no")),
                EX_RN,
            ),
        }
        removed_safe = (src_active and not tgt_active) or (tgt_active and not src_active)
        field_plans = {}
        local_ex: list[str] = []
        for field, (s_val, t_val, code) in field_map.items():
            key = f"{code}:{client_id}"
            chosen = _decision_for(decisions, key)
            if chosen:
                field_plans[field] = {
                    "source": s_val,
                    "survivor": t_val,
                    "action": chosen,
                    "resolved": True,
                }
                continue
            if field == "hot":
                conflict = s_val != t_val
            elif field == "assigned_user_id":
                conflict = bool(s_val) and bool(t_val) and s_val != t_val
            else:
                conflict = bool(s_val) and bool(t_val) and _norm_text(s_val) != _norm_text(t_val)
            if removed_safe and field in {"status", "assigned_user_id", "hot", "follow_up_date", "next_action"}:
                keep_side = "KEEP_SOURCE" if src_active and not tgt_active else ACTION_KEEP_SURVIVOR
                field_plans[field] = {
                    "source": s_val,
                    "survivor": t_val,
                    "action": keep_side if field != "external_record_no" else (
                        "PRESERVE_BOTH_AS_IDENTITY" if s_val and t_val and s_val != t_val else "KEEP_NONBLANK"
                    ),
                    "resolved": True,
                    "removed_ccr_safe": True,
                }
                continue
            if not conflict:
                if field == "external_record_no" and s_val and t_val and s_val != t_val:
                    conflict = True
                else:
                    action = ACTION_FILL_BLANK if (t_val and not s_val) else (
                        "KEEP_NONBLANK" if field == "external_record_no" else ACTION_KEEP_SURVIVOR
                    )
                    if field == "external_record_no" and s_val and not t_val:
                        action = ACTION_FILL_BLANK
                    field_plans[field] = {
                        "source": s_val,
                        "survivor": t_val,
                        "action": action,
                        "resolved": True,
                    }
                    continue
            options = ["KEEP_SURVIVOR", "KEEP_SOURCE"]
            if field == "external_record_no":
                options.append("PRESERVE_BOTH_AS_IDENTITY")
            if field == "hot":
                options.extend(["HOT", "NOT_HOT"])
            exceptions.append(
                _exception(
                    key,
                    code,
                    f"{client_name or client_code} CCR {code.replace('_', ' ').title()} requires a human choice.",
                    client_id=client_id,
                    field=field,
                    options=options,
                    details={"source": s_val, "survivor": t_val, "client_code": client_code},
                )
            )
            local_ex.append(code)
            field_plans[field] = {
                "source": s_val,
                "survivor": t_val,
                "action": CCR_DECISION,
                "resolved": False,
            }
        src_notes = _blank(src.get("notes"))
        tgt_notes = _blank(tgt.get("notes"))
        if src_notes and tgt_notes and _norm_text(src_notes) != _norm_text(tgt_notes):
            note_plan = "PRESERVE_ALL_UNIQUE"
        elif src_notes and not tgt_notes:
            note_plan = ACTION_FILL_BLANK
        else:
            note_plan = ACTION_KEEP_SURVIVOR
        field_plans["notes"] = {
            "source": src_notes,
            "survivor": tgt_notes,
            "action": note_plan,
            "resolved": True,
        }
        src_camp_ids = {
            int(r["campaign_id"])
            for r in source_camps
            if int(r.get("client_id") or 0) == int(client_id)
        }
        tgt_camp_ids = {
            int(r["campaign_id"])
            for r in survivor_camps
            if int(r.get("client_id") or 0) == int(client_id)
        }
        overlap = src_camp_ids & tgt_camp_ids
        unique_camps = src_camp_ids - tgt_camp_ids
        camp_key = f"{EX_CAMPAIGN}:{client_id}"
        camp_conflict = False
        for campaign_id in overlap:
            s_note = next(
                (
                    _blank(r.get("notes"))
                    for r in source_camps
                    if int(r["campaign_id"]) == campaign_id
                ),
                "",
            )
            t_note = next(
                (
                    _blank(r.get("notes"))
                    for r in survivor_camps
                    if int(r["campaign_id"]) == campaign_id
                ),
                "",
            )
            if s_note and t_note and _norm_text(s_note) != _norm_text(t_note):
                camp_conflict = True
        if camp_conflict and not _resolved(decisions, camp_key):
            exceptions.append(
                _exception(
                    camp_key,
                    EX_CAMPAIGN,
                    f"{client_name or client_code} campaign membership notes conflict.",
                    client_id=client_id,
                    options=["PRESERVE_ALL_UNIQUE", "KEEP_SURVIVOR", "KEEP_SOURCE"],
                )
            )
            local_ex.append(EX_CAMPAIGN)
        classification = CCR_DECISION if local_ex else CCR_SAFE
        rows.append(
            {
                "client_id": client_id,
                "client_name": client_name,
                "client_code": client_code,
                "same_client": True,
                "classification": classification,
                "action": "CONSOLIDATE_SAME_CLIENT_CCR" if classification == CCR_SAFE else "HUMAN_CCR_DECISION",
                "source_ccr_id": src.get("id"),
                "survivor_ccr_id": tgt.get("id"),
                "source_active": src_active,
                "survivor_active": tgt_active,
                "fields": field_plans,
                "exceptions": local_ex,
                "unique_campaigns_to_preserve": sorted(unique_camps),
                "overlapping_campaigns": sorted(overlap),
            }
        )
    return rows, exceptions


def _material_fingerprint(
    *,
    key: str,
    classification: dict[str, Any],
    review: dict[str, Any],
    survivor_id: int | None,
    source_id: int | None,
    master_fields: list[dict[str, Any]],
    ccrs: list[dict[str, Any]],
    contacts: dict[str, Any],
    aliases: dict[str, Any],
    locations: dict[str, Any],
    identities: dict[str, Any],
    campaigns: dict[str, Any],
    history: dict[str, Any],
) -> str:
    payload = {
        "pair_key": key,
        "planner_version": PLANNER_VERSION,
        "classifier_version": _blank(classification.get("classifier_version")) or CLASSIFIER_VERSION,
        "classification": _blank(classification.get("classification")),
        "classification_fingerprint": _blank(classification.get("evidence_fingerprint")),
        "human_disposition": _blank(review.get("disposition")) or DISPOSITION_UNREVIEWED,
        "human_review_fingerprint": _blank(review.get("evidence_fingerprint")),
        "human_reviewed_at": _blank(review.get("reviewed_at")),
        "human_survivor": review.get("proposed_survivor_company_id"),
        "human_source": review.get("proposed_source_company_id"),
        "survivor_company_id": survivor_id,
        "source_company_id": source_id,
        "master_fields": [(r.get("field"), r.get("action"), r.get("survivor_value"), r.get("source_value")) for r in master_fields],
        "ccrs": [
            (
                r.get("client_id"),
                r.get("classification"),
                r.get("action"),
                r.get("source_ccr_id"),
                r.get("survivor_ccr_id"),
                r.get("exceptions"),
            )
            for r in ccrs
        ],
        "contacts": [
            (
                r.get("source_contact_id"),
                r.get("survivor_contact_id"),
                r.get("classification"),
                r.get("consolidation"),
            )
            for r in (contacts.get("rows") or [])
        ],
        "aliases": (aliases.get("unique_to_preserve"), aliases.get("exact_normalized_duplicates")),
        "locations": (
            locations.get("strong_multi_location_concern"),
            [(c.get("classification"), c.get("source_location_id"), c.get("survivor_location_id")) for c in (locations.get("comparisons") or [])],
        ),
        "identities": (
            identities.get("source_master_rn"),
            identities.get("survivor_master_rn"),
            identities.get("move_identities"),
        ),
        "campaigns": (
            campaigns.get("unique_memberships_to_preserve"),
            campaigns.get("same_campaign_already_on_survivor"),
        ),
        "history": history,
    }
    return _fingerprint(payload)


def _empty_merge_shell(lo: int, hi: int) -> dict[str, Any]:
    return {
        "contacts": {},
        "ccrs": {},
        "locations": {},
        "source_identities": {},
        "aliases": {},
        "campaigns": {},
        "notes": {},
        "history_workflow": {},
        "blockers": [],
        "warnings": [],
        "dependencies": [],
        "live_merge_executed": False,
    }


def build_merge_plan(
    conn: sqlite3.Connection,
    company_a_id: int,
    company_b_id: int,
    *,
    decisions: dict[str, dict[str, Any]] | None = None,
    existing_plan_id: int | None = None,
) -> dict[str, Any]:
    """Read-only plan object. Caller persists. Never executes merge."""
    ensure_merge_plan_schema(conn)
    lo, hi = pair_ids(company_a_id, company_b_id)
    key = pair_key(lo, hi)
    classification = _load_classification(conn, key)
    review = _load_review(conn, key)
    eligibility = eligibility_for_pair(classification, review)
    decisions = decisions if decisions is not None else _load_active_decisions(conn, existing_plan_id)
    survivor_info = _choose_survivor(lo, hi, classification, review, decisions)
    survivor_id = survivor_info["survivor_company_id"]
    source_id = survivor_info["source_company_id"]
    exceptions: list[dict[str, Any]] = []
    not_safe: list[str] = []
    if survivor_info["survivor_decision_required"]:
        exceptions.append(
            _exception(
                EX_SURVIVOR,
                EX_SURVIVOR,
                "Survivor remains ambiguous. NorthStar will not choose the lower ID to proceed.",
                options=[f"SURVIVOR:{lo}", f"SURVIVOR:{hi}"],
            )
        )
    left = _company_row(conn, lo)
    right = _company_row(conn, hi)
    if not left or not right:
        raise MergePlanError("company_not_found")
    merge = _empty_merge_shell(lo, hi)
    if survivor_id and source_id:
        merge = plan_company_merge(conn, int(source_id), int(survivor_id))
        if merge.get("live_merge_executed"):
            raise MergePlanError("planner_executed_merge")
    survivor_row = _company_row(conn, int(survivor_id)) if survivor_id else {}
    source_row = _company_row(conn, int(source_id)) if source_id else {}
    master_fields: list[dict[str, Any]] = []
    ccrs: list[dict[str, Any]] = []
    contacts: dict[str, Any] = {"rows": [], "no_contact_merge_now": True}
    aliases: dict[str, Any] = {"unique_to_preserve": 0, "exact_normalized_duplicates": 0, "no_alias_disappears": True}
    locations: dict[str, Any] = {"strong_multi_location_concern": False, "comparisons": []}
    identities: dict[str, Any] = {"never_drop_source_rn": True}
    campaigns: dict[str, Any] = {"unique_memberships_to_preserve": 0}
    if survivor_id and source_id and survivor_row and source_row:
        master_fields, field_ex = _plan_master_fields(survivor_row, source_row, decisions)
        exceptions.extend(field_ex)
        ccrs, ccr_ex = _ccr_plan(conn, int(source_id), int(survivor_id), decisions)
        exceptions.extend(ccr_ex)
        contacts, contact_ex = _contact_plan(merge, decisions)
        exceptions.extend(contact_ex)
        aliases = _plan_aliases_safe(merge, _blank(source_row.get("company_name")))
        locations, loc_ex, loc_not_safe = _location_plan(merge, survivor_row, source_row, decisions)
        exceptions.extend(loc_ex)
        not_safe.extend(loc_not_safe)
        identities, id_ex = _plan_identities_safe(merge, survivor_row, source_row)
        exceptions.extend(id_ex)
        if id_ex:
            not_safe.append(EX_IDENTITY)
        campaigns, camp_ex = _plan_campaigns_safe(merge, decisions)
        exceptions.extend(camp_ex)
    side_a = survivor_row or left
    side_b = source_row or right
    a_city = _norm_field("city", side_a.get("city"))
    b_city = _norm_field("city", side_b.get("city"))
    a_state = _norm_field("state", side_a.get("state"))
    b_state = _norm_field("state", side_b.get("state"))
    different_site = bool(
        (a_city and b_city and a_city != b_city) or (a_state and b_state and a_state != b_state)
    )
    confirmed_location = _decision_for(decisions, EX_MULTI) in {
        ACTION_PRESERVE_LOCATION,
        "CONFIRM_PRESERVE_AS_LOCATION",
    }
    if different_site and not confirmed_location:
        locations["strong_multi_location_concern"] = True
        if EX_MULTI not in not_safe:
            not_safe.append(EX_MULTI)
        if not any(e.get("code") == EX_MULTI for e in exceptions):
            exceptions.append(
                _exception(
                    EX_MULTI,
                    EX_MULTI,
                    "Location evidence suggests these may be distinct manufacturing/sites, not a safe master merge.",
                    options=["CONFIRM_PRESERVE_AS_LOCATION", "KEEP_SEPARATE"],
                    details={
                        "company_a_city": left.get("city"),
                        "company_b_city": right.get("city"),
                    },
                )
            )
    history = {
        "source": _history_counts(conn, int(source_id)) if source_id else _history_counts(conn, lo),
        "survivor": _history_counts(conn, int(survivor_id)) if survivor_id else _history_counts(conn, hi),
        "preserve_all_unique": True,
        "no_destructive_conflict_resolution": True,
    }
    unresolved = [e for e in exceptions if not _resolved(decisions, e["exception_key"])]
    # Confirmed multi-location preserve removes NOT_SAFE if Julie confirmed.
    if _decision_for(decisions, EX_MULTI) in {ACTION_PRESERVE_LOCATION, "CONFIRM_PRESERVE_AS_LOCATION"}:
        not_safe = [r for r in not_safe if r != EX_MULTI]
        unresolved = [e for e in unresolved if e["code"] != EX_MULTI]
        locations["strong_multi_location_concern"] = False
    state = STATE_READY
    if not eligibility["eligible"]:
        state = STATE_NOT_SAFE
        not_safe.append(eligibility["blocked_reason"] or "not_eligible")
    elif not_safe:
        state = STATE_NOT_SAFE
    elif unresolved or survivor_info["survivor_decision_required"]:
        state = STATE_NEEDS
    live_fp = _material_fingerprint(
        key=key,
        classification=classification,
        review=review,
        survivor_id=survivor_id,
        source_id=source_id,
        master_fields=master_fields,
        ccrs=ccrs,
        contacts=contacts,
        aliases=aliases,
        locations=locations,
        identities=identities,
        campaigns=campaigns,
        history=history,
    )
    same_client_conflict = any(r.get("same_client") and r.get("classification") == CCR_DECISION for r in ccrs)
    plan = {
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "execute_merge": False,
        "merge_now": False,
        "plan_only_warning": PLAN_ONLY_WARNING,
        "planner_version": PLANNER_VERSION,
        "pair_key": key,
        "company_a_id": lo,
        "company_b_id": hi,
        "company_a_name": _blank(left.get("company_name")),
        "company_b_name": _blank(right.get("company_name")),
        "source_company_id": source_id,
        "survivor_company_id": survivor_id,
        "survivor_source": survivor_info["survivor_source"],
        "plan_state": state,
        "plan_state_label": UI_STATE[state],
        "eligibility": eligibility,
        "classification": _blank(classification.get("classification")) or None,
        "classification_fingerprint": _blank(classification.get("evidence_fingerprint")),
        "classifier_version": _blank(classification.get("classifier_version")) or CLASSIFIER_VERSION,
        "human_disposition": eligibility["human_disposition"],
        "master_fields": master_fields,
        "ccrs": ccrs,
        "contacts": contacts,
        "aliases": aliases,
        "locations": locations,
        "identities": identities,
        "campaigns": campaigns,
        "history": history,
        "dependencies": {
            "blockers": merge.get("blockers") or [],
            "warnings": merge.get("warnings") or [],
            "source_history": history["source"],
            "survivor_history": history["survivor"],
        },
        "exceptions": unresolved,
        "all_exceptions": exceptions,
        "not_safe_reasons": not_safe,
        "same_client_ccr_conflict": same_client_conflict,
        "contact_collision_summary": {
            "unique": contacts.get("unique_to_preserve") or 0,
            "exact": contacts.get("exact_duplicates") or 0,
            "possible": contacts.get("possible_duplicates") or 0,
            "conflict": contacts.get("conflicts") or 0,
        },
        "location_concern": bool(locations.get("strong_multi_location_concern")),
        "identity_concern": bool(identities.get("incompatible")),
        "plan_fingerprint": live_fp,
        "future_handoff": FUTURE_HANDOFF,
        "future_execution_requirements": [
            "current HIGH_CONFIDENCE_DUPLICATE or explicit human MERGE_CANDIDATE",
            "current non-stale merge plan",
            "all exceptions resolved",
            "explicit Julie merge approval",
            "source/survivor-bound fingerprint",
            "one-time approval",
            "final execution preview",
        ],
        "does_not_create_merge_candidate": True,
        "does_not_create_merge_approval": True,
        "live_merge_executed": False,
    }
    return plan


def _existing_plan_row(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not _table_exists(conn, "company_merge_plans"):
        return {}
    row = conn.execute("SELECT * FROM company_merge_plans WHERE pair_key=?", (key,)).fetchone()
    return _row_dict(row)


def persist_plan(conn: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    ensure_merge_plan_schema(conn)
    now = _now()
    key = plan["pair_key"]
    existing = _existing_plan_row(conn, key)
    reused = False
    if existing and _blank(existing.get("plan_fingerprint")) == plan["plan_fingerprint"] and _blank(
        existing.get("planner_version")
    ) == PLANNER_VERSION and _blank(existing.get("plan_state")) == plan["plan_state"] and _blank(
        existing.get("exceptions_json")
    ) == _canonical_json(plan.get("exceptions") or []):
        reused = True
        plan_id = int(existing["id"])
        plan["id"] = plan_id
        plan["created_at"] = existing.get("created_at")
        plan["updated_at"] = existing.get("updated_at")
        plan["reused"] = True
        return plan
    if existing:
        conn.execute(
            """
            UPDATE company_merge_plans
            SET source_company_id=?, survivor_company_id=?, plan_state=?,
                plan_json=?, exceptions_json=?, plan_fingerprint=?,
                classification_fingerprint=?, updated_at=?, planner_version=?
            WHERE pair_key=?
            """,
            (
                plan.get("source_company_id"),
                plan.get("survivor_company_id"),
                plan["plan_state"],
                _canonical_json(plan),
                _canonical_json(plan.get("exceptions") or []),
                plan["plan_fingerprint"],
                _blank(plan.get("classification_fingerprint")),
                now,
                PLANNER_VERSION,
                key,
            ),
        )
        plan_id = int(existing["id"])
        created_at = existing.get("created_at") or now
    else:
        cur = conn.execute(
            """
            INSERT INTO company_merge_plans (
                pair_key, company_a_id, company_b_id, source_company_id,
                survivor_company_id, plan_state, plan_json, exceptions_json,
                plan_fingerprint, classification_fingerprint, created_at,
                updated_at, planner_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                int(plan["company_a_id"]),
                int(plan["company_b_id"]),
                plan.get("source_company_id"),
                plan.get("survivor_company_id"),
                plan["plan_state"],
                _canonical_json(plan),
                _canonical_json(plan.get("exceptions") or []),
                plan["plan_fingerprint"],
                _blank(plan.get("classification_fingerprint")),
                now,
                now,
                PLANNER_VERSION,
            ),
        )
        plan_id = int(cur.lastrowid)
        created_at = now
    plan["id"] = plan_id
    plan["created_at"] = created_at
    plan["updated_at"] = now
    plan["reused"] = reused
    return plan


def _eligible_high_pairs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "company_duplicate_classifications"):
        return []
    rows = conn.execute(
        """
        SELECT * FROM company_duplicate_classifications
        WHERE classification=?
        ORDER BY company_a_id, company_b_id
        """,
        (CLASS_HIGH,),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _assert_no_business_mutation(before: dict[str, int], after: dict[str, int]) -> None:
    for key in (
        "companies",
        "contacts",
        "ccr",
        "aliases",
        "locations",
        "identities",
        "approvals",
        "merge_history",
        "assignments",
    ):
        if before.get(key) != after.get(key):
            raise MergePlanError("business_mutation")


def _safety_counts(conn: sqlite3.Connection) -> dict[str, int]:
    def n(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    def maybe(table: str, sql: str) -> int:
        return n(sql) if _table_exists(conn, table) else 0

    return {
        "companies": n("SELECT COUNT(*) FROM companies"),
        "contacts": n("SELECT COUNT(*) FROM contacts"),
        "ccr": n("SELECT COUNT(*) FROM client_company_relationships"),
        "aliases": maybe("company_aliases", "SELECT COUNT(*) FROM company_aliases"),
        "locations": maybe("company_locations", "SELECT COUNT(*) FROM company_locations"),
        "identities": maybe(
            "company_source_identities", "SELECT COUNT(*) FROM company_source_identities"
        ),
        "approvals": maybe(
            "merge_execution_approvals", "SELECT COUNT(*) FROM merge_execution_approvals"
        ),
        "merge_history": maybe(
            "company_merge_history", "SELECT COUNT(*) FROM company_merge_history"
        ),
        "assignments": maybe(
            "user_client_assignments", "SELECT COUNT(*) FROM user_client_assignments"
        ),
        "reviews": maybe(
            "company_duplicate_reviews", "SELECT COUNT(*) FROM company_duplicate_reviews"
        ),
        "classifications": maybe(
            "company_duplicate_classifications",
            "SELECT COUNT(*) FROM company_duplicate_classifications",
        ),
        "plans": maybe("company_merge_plans", "SELECT COUNT(*) FROM company_merge_plans"),
        "decisions": maybe(
            "company_merge_plan_decisions",
            "SELECT COUNT(*) FROM company_merge_plan_decisions",
        ),
    }


def prepare_merge_plans(conn: sqlite3.Connection, *, actor: NorthStarUser) -> dict[str, Any]:
    """Batch-plan eligible HIGH_CONFIDENCE_DUPLICATE pairs. Planning metadata only."""
    if not actor or not actor.is_administrator:
        raise PermissionError("administrator required")
    ensure_merge_plan_schema(conn)
    before = _safety_counts(conn)
    analyzed = 0
    skipped = 0
    ready = 0
    needs = 0
    not_safe = 0
    stale = 0
    reused = 0
    written = 0
    pairs: list[dict[str, Any]] = []
    for row in _eligible_high_pairs(conn):
        lo = int(row["company_a_id"])
        hi = int(row["company_b_id"])
        key = pair_key(lo, hi)
        review = _load_review(conn, key)
        eligibility = eligibility_for_pair(row, review)
        analyzed += 1
        if not eligibility["eligible"]:
            skipped += 1
            existing = _existing_plan_row(conn, key)
            plan = build_merge_plan(conn, lo, hi, existing_plan_id=_as_int(existing.get("id")))
            persist_plan(conn, plan)
            written += 1
            if plan["plan_state"] == STATE_NOT_SAFE:
                not_safe += 1
            pairs.append(_plan_list_row(plan))
            continue
        existing = _existing_plan_row(conn, key)
        plan = build_merge_plan(conn, lo, hi, existing_plan_id=_as_int(existing.get("id")))
        persist_plan(conn, plan)
        written += 1
        if plan.get("reused"):
            reused += 1
        if plan["plan_state"] == STATE_READY:
            ready += 1
        elif plan["plan_state"] == STATE_NEEDS:
            needs += 1
        elif plan["plan_state"] == STATE_NOT_SAFE:
            not_safe += 1
        elif plan["plan_state"] == STATE_STALE:
            stale += 1
        pairs.append(_plan_list_row(plan))
    after = _safety_counts(conn)
    _assert_no_business_mutation(before, after)
    conn.commit()
    return {
        "ok": True,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "execute_merge": False,
        "plan_only_warning": PLAN_ONLY_WARNING,
        "planner_version": PLANNER_VERSION,
        "analyzed": analyzed,
        "skipped_ineligible": skipped,
        "ready_for_review": ready,
        "needs_exception_decision": needs,
        "not_safe_to_plan": not_safe,
        "stale": stale,
        "reused": reused,
        "written": written,
        "READY_FOR_HUMAN_APPROVAL": ready,
        "NEEDS_EXCEPTION_DECISION": needs,
        "NOT_SAFE_TO_PLAN": not_safe,
        "STALE": stale,
        "pairs": pairs,
        "merge_execution_approvals": after["approvals"],
        "merge_history": after["merge_history"],
        "human_reviews_mutated": after["reviews"] != before["reviews"],
        "spoofed_actor_ignored": True,
        "actor_user_id": int(actor.id),
    }


def _plan_list_row(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": plan.get("id"),
        "pair_key": plan.get("pair_key"),
        "company_a_id": plan.get("company_a_id"),
        "company_b_id": plan.get("company_b_id"),
        "company_a_name": plan.get("company_a_name"),
        "company_b_name": plan.get("company_b_name"),
        "source_company_id": plan.get("source_company_id"),
        "survivor_company_id": plan.get("survivor_company_id"),
        "plan_state": plan.get("plan_state"),
        "plan_state_label": plan.get("plan_state_label") or UI_STATE.get(plan.get("plan_state") or "", plan.get("plan_state")),
        "exceptions": plan.get("exceptions") or [],
        "exception_count": len(plan.get("exceptions") or []),
        "same_client_ccr_conflict": plan.get("same_client_ccr_conflict"),
        "contact_collision_summary": plan.get("contact_collision_summary"),
        "location_concern": plan.get("location_concern"),
        "identity_concern": plan.get("identity_concern"),
        "classification": plan.get("classification"),
        "human_disposition": plan.get("human_disposition"),
        "planning_only": True,
        "merge_will_occur": False,
    }


def _hydrate_stored_plan(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    stored = json.loads(row.get("plan_json") or "{}")
    live = build_merge_plan(
        conn,
        int(row["company_a_id"]),
        int(row["company_b_id"]),
        existing_plan_id=_as_int(row.get("id")),
    )
    stale = live["plan_fingerprint"] != _blank(row.get("plan_fingerprint"))
    if stale:
        live["plan_state"] = STATE_STALE
        live["plan_state_label"] = UI_STATE[STATE_STALE]
        live["stored_plan_state"] = row.get("plan_state")
        live["stale"] = True
    else:
        live["stale"] = False
    live["id"] = row.get("id")
    live["created_at"] = row.get("created_at")
    live["updated_at"] = row.get("updated_at")
    live["stored_plan"] = stored
    live["active_decisions"] = list(_load_active_decisions(conn, _as_int(row.get("id"))).values())
    live["decision_history"] = []
    if _table_exists(conn, "company_merge_plan_decisions"):
        live["decision_history"] = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT * FROM company_merge_plan_decisions
                WHERE pair_key=? ORDER BY id
                """,
                (row.get("pair_key"),),
            ).fetchall()
        ]
    live["planning_only"] = True
    live["merge_will_occur"] = False
    live["approval_created"] = False
    live["plan_only_warning"] = PLAN_ONLY_WARNING
    return live


def list_merge_plans(
    conn: sqlite3.Connection,
    *,
    state: str = "",
    q: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    ensure_merge_plan_schema(conn)
    rows = [
        _row_dict(r)
        for r in conn.execute(
            "SELECT * FROM company_merge_plans ORDER BY company_a_id, company_b_id"
        ).fetchall()
    ]
    hydrated = [_hydrate_stored_plan(conn, row) for row in rows]
    summary = {
        "analyzed": len(hydrated),
        "ready_for_review": 0,
        "needs_exception_decision": 0,
        "not_safe_to_plan": 0,
        "stale": 0,
        "READY_FOR_HUMAN_APPROVAL": 0,
        "NEEDS_EXCEPTION_DECISION": 0,
        "NOT_SAFE_TO_PLAN": 0,
        "STALE": 0,
        "exceptions": 0,
        "exception_decisions": 0,
    }
    if _table_exists(conn, "company_merge_plan_decisions"):
        summary["exception_decisions"] = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM company_merge_plan_decisions
                WHERE TRIM(COALESCE(superseded_at,'')) = ''
                """
            ).fetchone()[0]
        )
    wanted = _blank(state).upper()
    if wanted in {"READY", "READY_FOR_REVIEW", "READY_FOR_HUMAN_APPROVAL"}:
        wanted = STATE_READY
    elif wanted in {"NEEDS", "NEEDS_DECISIONS", "NEEDS_EXCEPTION_DECISION"}:
        wanted = STATE_NEEDS
    elif wanted in {"NOT_SAFE", "NOT_SAFE_TO_PLAN"}:
        wanted = STATE_NOT_SAFE
    filtered = []
    needle = _norm_text(q)
    for plan in hydrated:
        bucket = plan["plan_state"]
        summary[bucket] = int(summary.get(bucket) or 0) + 1
        if bucket == STATE_READY:
            summary["ready_for_review"] += 1
        elif bucket == STATE_NEEDS:
            summary["needs_exception_decision"] += 1
        elif bucket == STATE_NOT_SAFE:
            summary["not_safe_to_plan"] += 1
        elif bucket == STATE_STALE:
            summary["stale"] += 1
        summary["exceptions"] += len(plan.get("exceptions") or [])
        if wanted and bucket != wanted:
            continue
        hay = " ".join(
            [
                str(plan.get("pair_key") or ""),
                str(plan.get("company_a_name") or ""),
                str(plan.get("company_b_name") or ""),
            ]
        ).lower()
        if needle and needle not in hay:
            continue
        filtered.append(plan)
    offset = max(int(offset or 0), 0)
    limit = min(max(int(limit or 50), 1), 200)
    page = filtered[offset : offset + limit]
    return {
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "execute_merge": False,
        "plan_only_warning": PLAN_ONLY_WARNING,
        "planner_version": PLANNER_VERSION,
        "summary": summary,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "plans": [_plan_list_row(p) for p in page],
        "no_merge_button": True,
    }


def get_merge_plan(conn: sqlite3.Connection, company_a_id: int, company_b_id: int) -> dict[str, Any]:
    ensure_merge_plan_schema(conn)
    key = pair_key(company_a_id, company_b_id)
    row = _existing_plan_row(conn, key)
    if not row:
        raise MergePlanError("plan_not_found")
    plan = _hydrate_stored_plan(conn, row)
    history = []
    if _table_exists(conn, "company_merge_plan_decisions"):
        history = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT * FROM company_merge_plan_decisions
                WHERE pair_key=? ORDER BY id
                """,
                (key,),
            ).fetchall()
        ]
    plan["decision_history"] = history
    plan["no_merge_button"] = True
    plan["no_execute_merge"] = True
    return plan


def save_merge_plan_decision(
    conn: sqlite3.Connection,
    *,
    actor: NorthStarUser,
    company_a_id: int,
    company_b_id: int,
    body: MergePlanDecisionRequest,
) -> dict[str, Any]:
    if not actor or not actor.is_administrator:
        raise PermissionError("administrator required")
    ensure_merge_plan_schema(conn)
    key = pair_key(company_a_id, company_b_id)
    existing = _existing_plan_row(conn, key)
    if not existing:
        raise MergePlanError("plan_not_found")
    exception_key = _blank(body.exception_key)
    resolution = _blank(body.chosen_resolution)
    if not exception_key or not resolution:
        raise MergePlanError("decision_required")
    before = _safety_counts(conn)
    now = _now()
    plan_id = int(existing["id"])
    conn.execute(
        """
        UPDATE company_merge_plan_decisions
        SET superseded_at=?
        WHERE plan_id=? AND exception_key=? AND TRIM(COALESCE(superseded_at,'')) = ''
        """,
        (now, plan_id, exception_key),
    )
    live = build_merge_plan(conn, company_a_id, company_b_id, existing_plan_id=plan_id)
    conn.execute(
        """
        INSERT INTO company_merge_plan_decisions (
            plan_id, pair_key, exception_key, chosen_resolution, resolution_json,
            actor_user_id, created_at, reason, evidence_fingerprint, superseded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            plan_id,
            key,
            exception_key,
            resolution,
            _canonical_json(
                {
                    "chosen_resolution": resolution,
                    "reason": _blank(body.reason),
                    "spoofed_actor_id": body.actor_id,
                    "created_by": body.created_by,
                }
            ),
            int(actor.id),
            now,
            _blank(body.reason),
            live["plan_fingerprint"],
        ),
    )
    replanned = build_merge_plan(conn, company_a_id, company_b_id, existing_plan_id=plan_id)
    persist_plan(conn, replanned)
    after = _safety_counts(conn)
    _assert_no_business_mutation(before, after)
    conn.commit()
    return {
        "ok": True,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "plan_only_warning": PLAN_ONLY_WARNING,
        "exception_key": exception_key,
        "chosen_resolution": resolution,
        "actor_user_id": int(actor.id),
        "spoofed_actor_ignored": True,
        "plan": _hydrate_stored_plan(conn, _existing_plan_row(conn, key)),
    }


def replan_merge_plan(
    conn: sqlite3.Connection,
    *,
    actor: NorthStarUser,
    company_a_id: int,
    company_b_id: int,
) -> dict[str, Any]:
    if not actor or not actor.is_administrator:
        raise PermissionError("administrator required")
    ensure_merge_plan_schema(conn)
    key = pair_key(company_a_id, company_b_id)
    existing = _existing_plan_row(conn, key)
    if not existing:
        raise MergePlanError("plan_not_found")
    before = _safety_counts(conn)
    plan = build_merge_plan(
        conn, company_a_id, company_b_id, existing_plan_id=int(existing["id"])
    )
    persist_plan(conn, plan)
    after = _safety_counts(conn)
    _assert_no_business_mutation(before, after)
    conn.commit()
    return {
        "ok": True,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "plan_only_warning": PLAN_ONLY_WARNING,
        "plan": _hydrate_stored_plan(conn, _existing_plan_row(conn, key)),
    }
