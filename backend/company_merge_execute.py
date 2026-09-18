"""Atomic company + contact consolidation engine (Phase 5F/5J).

Internal-only. Not wired to FastAPI or any public route.
Live northstar.db requires a pending merge_execution_approvals row that
matches source, survivor, and fingerprint. Isolated copies may execute
without an approval. There is no company-id special case.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from company_aliases import upsert_company_alias
from company_locations import ensure_company_location, upsert_company_source_identity
from company_merges import (
    CONTACT_FIELD_KEYS,
    NOTE_COLUMN_NAMES,
    _blank,
    _company_snapshot,
    _norm_email,
    _norm_note,
    _norm_status,
    _norm_title,
    _row_dict,
    _table_columns,
    _table_exists,
    _title_tokens,
    discover_company_id_refs,
    flatten_inbound_contact_redirects,
    plan_company_merge,
    record_company_merge_redirect,
    record_contact_merge_redirect,
    resolve_company_id,
    resolve_contact_id,
)
from company_merge_approvals import (
    MergeApprovalError,
    claim_merge_approval,
    complete_merge_approval,
    fail_merge_approval,
    load_merge_approval,
    supersede_stale_approval,
)
from contact_phone import canonical_contact_phone
from db import PRODUCTION_DB_PATH

SOURCE_SYSTEM_DEFAULT = "leadmaster"

DO_NOT_CARRY_TOKENS = frozenset(
    {
        "do_not_carry",
        "do_not_carry_from_source",
        "skip_source",
        "exclude_source",
    }
)

SKIP_COMPANY_REMAP = frozenset(
    {
        "companies",
        "company_merge_history",
        "contact_merge_history",
        "merge_execution_approvals",
        "company_identity_keys",
        "contacts",
        "client_company_relationships",
        "company_locations",
        "company_source_identities",
        "company_aliases",
        "campaign_companies",
        "company_shared_history_events",
        "legacy_notes",
    }
)

SKIP_CONTACT_REMAP = frozenset(
    {
        "contacts",
        "contact_merge_history",
    }
)

COMPANY_SAFE_FILL_FIELDS = (
    "address",
    "city",
    "state",
    "zip",
    "website",
    "legacy_phone",
    "legacy_alt_phone",
    "legacy_mobile",
    "legacy_email",
    "legacy_first_name",
    "legacy_last_name",
    "legacy_title",
    "linkedin_url",
    "zoominfo_company_id",
)


class MergeExecutionError(ValueError):
    """Merge cannot run (safety, stale plan, or internal failure)."""


class MergeBlockedError(MergeExecutionError):
    """Approved resolutions are missing; no writes occurred."""


class MergeStalePlanError(MergeExecutionError):
    """Current data no longer matches the approved plan fingerprint."""


def connection_db_path(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list").fetchall():
        payload = _row_dict(row) if hasattr(row, "keys") else {"name": row[1], "file": row[2]}
        if str(payload.get("name") or "") == "main":
            file_name = _blank(payload.get("file"))
            return Path(file_name).resolve() if file_name else None
    return None


def assert_not_production_connection(
    conn: sqlite3.Connection, *, allow_production: bool = False
) -> None:
    path = connection_db_path(conn)
    if path is None:
        return
    if path == PRODUCTION_DB_PATH.resolve() and not allow_production:
        raise MergeExecutionError(
            "Refusing execute_company_merge against live northstar.db."
        )


def _decided_contact(resolution: dict[str, Any], contact_id: int) -> dict[str, Any]:
    contact_res = resolution.get("contacts") or {}
    return contact_res.get(str(int(contact_id))) or contact_res.get(int(contact_id)) or {}


def _is_do_not_carry(value: object) -> bool:
    return _blank(value).lower().replace(" ", "_") in DO_NOT_CARRY_TOKENS


def _canonical_resolution_decisions(resolution: dict[str, Any] | None) -> dict[str, Any]:
    """Contact/CCR/identity choices that must be part of the stale-plan hash."""
    resolution = resolution or {}
    contacts_out: dict[str, Any] = {}
    for key, raw in (resolution.get("contacts") or {}).items():
        if not isinstance(raw, dict):
            continue
        try:
            cid = str(int(key))
        except (TypeError, ValueError):
            cid = str(key)
        fields = raw.get("fields") or {}
        canon_fields = {
            str(field): _blank(val).lower() if isinstance(val, str) else val
            for field, val in sorted(fields.items(), key=lambda item: str(item[0]))
        }
        contacts_out[cid] = {
            "action": _blank(raw.get("action")).upper(),
            "survivor_contact_id": raw.get("survivor_contact_id"),
            "fields": canon_fields,
        }
    identities = resolution.get("identities") or {}
    extra = resolution.get("additional_source_identities") or identities.get("create") or []
    extra_canon = []
    for row in extra:
        if not isinstance(row, dict):
            continue
        extra_canon.append(
            {
                "rn": _blank(row.get("rn") or row.get("source_record_no")),
                "source_system": _blank(row.get("source_system")),
                "client_id": row.get("client_id"),
            }
        )
    extra_canon.sort(key=lambda r: (r["source_system"], r["rn"], str(r["client_id"])))
    ccrs = resolution.get("ccrs") or {}
    ccrs_out = {}
    for key, raw in ccrs.items():
        if not isinstance(raw, dict):
            ccrs_out[str(key)] = raw
            continue
        ccrs_out[str(key)] = {
            k: raw.get(k)
            for k in sorted(raw)
            if k not in {"notes", "preserve_source_status_evidence"}
        }
    return {
        "contacts": dict(sorted(contacts_out.items(), key=lambda item: item[0])),
        "ccrs": dict(sorted(ccrs_out.items(), key=lambda item: item[0])),
        "company_fields": resolution.get("company_fields") or {},
        "additional_source_identities": extra_canon,
        "allow_distinct_site": bool(resolution.get("allow_distinct_site")),
        "allow_location_review": bool(resolution.get("allow_location_review")),
    }


def stamp_merge_fingerprint(plan: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    """Write plan_fingerprint from current plan + approved decisions."""
    resolution["plan_fingerprint"] = merge_plan_fingerprint(plan, resolution)
    return resolution


def merge_plan_fingerprint(
    plan: dict[str, Any], approved_resolution: dict[str, Any] | None = None
) -> str:
    """Deterministic hash of merge-relevant state plus approved decisions.

    Excludes global table counts and operational metadata (reason, checkpoints).
    Changing a source-side contact survivor or DO_NOT_CARRY field invalidates
    an older fingerprint.
    """
    contacts = plan.get("contacts") or {}
    notes = plan.get("notes") or {}

    def _contact_snap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for row in rows or []:
            out.append(
                {
                    "id": row.get("id"),
                    "first_name": _blank(row.get("first_name")),
                    "last_name": _blank(row.get("last_name")),
                    "title": _blank(row.get("title")),
                    "email": _blank(row.get("email")),
                    "phone": _blank(row.get("phone")),
                    "alt_phone": _blank(row.get("alt_phone")),
                    "phone_extension": _blank(row.get("phone_extension")),
                    "alt_phone_extension": _blank(row.get("alt_phone_extension")),
                    "external_record_no": _blank(row.get("external_record_no")),
                    "location_id": row.get("location_id"),
                }
            )
        return sorted(out, key=lambda r: int(r["id"] or 0))

    def _note_snap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for row in rows or []:
            out.append(
                {
                    "table": row.get("table"),
                    "column": row.get("column"),
                    "rowid": row.get("rowid"),
                    "norm": row.get("norm"),
                    "client_id": row.get("client_id"),
                    "contact_id": row.get("contact_id"),
                }
            )
        return sorted(out, key=lambda r: (str(r["table"]), int(r["rowid"] or 0), str(r["column"])))

    payload = {
        "source_company_id": plan.get("source_company_id"),
        "survivor_company_id": plan.get("survivor_company_id"),
        "source": plan.get("source") or {},
        "survivor": plan.get("survivor") or {},
        "verdict": plan.get("verdict"),
        "blockers": plan.get("blockers") or [],
        "contacts": {
            "source": _contact_snap(contacts.get("source_contacts") or []),
            "survivor": _contact_snap(contacts.get("survivor_contacts") or []),
            "classifications": [
                {
                    "action": c.get("action"),
                    "source_contact_id": c.get("source_contact_id"),
                    "survivor_contact_id": c.get("survivor_contact_id"),
                    "reasons": c.get("reasons") or [],
                    "field_conflicts": c.get("field_conflicts") or [],
                }
                for c in contacts.get("classifications") or []
            ],
        },
        "ccrs": (plan.get("ccrs") or {}).get("pairs") or [],
        "notes": {
            "source": _note_snap(notes.get("source_note_rows") or []),
            "survivor": _note_snap(notes.get("survivor_note_rows") or []),
            "distinct": _note_snap(notes.get("distinct_note_rows") or []),
        },
        "identities": {
            "source": (plan.get("source_identities") or {}).get("source_identities") or [],
            "master": (plan.get("source_identities") or {}).get("master_record_nos") or {},
        },
        "aliases": {
            "move": [
                {"id": r.get("id"), "alias_norm": r.get("alias_norm")}
                for r in (plan.get("aliases") or {}).get("move") or []
            ]
        },
        "campaigns": {
            "union_move": [
                r.get("campaign_id")
                for r in (plan.get("campaigns") or {}).get("union_move") or []
            ],
            "already": [
                r.get("campaign_id")
                for r in (plan.get("campaigns") or {}).get("already_member") or []
            ],
        },
        "locations": (plan.get("locations") or {}).get("comparisons") or [],
        "approved_decisions": _canonical_resolution_decisions(approved_resolution),
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pk_column(conn: sqlite3.Connection, table: str) -> str | None:
    pks = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if int(r[5] or 0)]
    if len(pks) == 1:
        return pks[0]
    return None


def _unique_colsets(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    out: list[list[str]] = []
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not int(idx[2]):
            continue
        cols = [
            str(info[2])
            for info in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
        ]
        if cols:
            out.append(cols)
    return out


def _checkpoint(resolution: dict[str, Any], name: str) -> None:
    if _blank(resolution.get("test_fail_after")) == name:
        raise MergeExecutionError(f"Forced test failure after {name}.")


def _append_distinct(existing: object, incoming: object) -> str:
    left = _blank(existing)
    right = _blank(incoming)
    if not right:
        return left
    if not left:
        return right
    if _norm_note(left) == _norm_note(right):
        return left
    return left.rstrip() + "\n\n" + right


def _titles_equivalent(a: object, b: object) -> bool:
    ta, tb = _title_tokens(a), _title_tokens(b)
    return bool(ta) and ta == tb


def validate_merge_resolution(
    plan: dict[str, Any], resolution: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    src = int(plan["source_company_id"])
    dst = int(plan["survivor_company_id"])
    if int(resolution.get("source_company_id") or 0) != src:
        problems.append("Resolution source_company_id does not match the plan.")
    if int(resolution.get("survivor_company_id") or 0) != dst:
        problems.append("Resolution survivor_company_id does not match the plan.")
    if src == dst:
        problems.append("Source and survivor must be different existing companies.")
    if int(resolution.get("survivor_company_id") or 0) != dst:
        problems.append("Silent survivor switching is forbidden.")

    loc_classes = {
        c.get("classification")
        for c in (plan.get("locations") or {}).get("comparisons") or []
    }
    if "DISTINCT_SITE" in loc_classes and not resolution.get("allow_distinct_site"):
        problems.append(
            "DISTINCT_SITE blocked: not an automatic duplicate merge. "
            "Set allow_distinct_site only for an explicit approved location absorb."
        )
    if "REVIEW" in loc_classes and not resolution.get("allow_location_review"):
        problems.append("Location REVIEW requires an explicit approved location resolution.")

    contact_res = resolution.get("contacts") or {}
    dest_ids = {
        int(r["id"]) for r in (plan.get("contacts") or {}).get("survivor_contacts") or []
    }
    source_ids = {
        int(r["id"]) for r in (plan.get("contacts") or {}).get("source_contacts") or []
    }
    by_id = {
        int(r["id"]): r
        for r in ((plan.get("contacts") or {}).get("source_contacts") or [])
        + ((plan.get("contacts") or {}).get("survivor_contacts") or [])
    }

    def _action_for(cid: int) -> str:
        decided = _decided_contact(resolution, cid)
        return _blank(decided.get("action")).upper()

    merge_edges: dict[int, int] = {}
    for row in (plan.get("contacts") or {}).get("classifications") or []:
        cid_int = int(row["source_contact_id"])
        cid = str(cid_int)
        decided = contact_res.get(cid) or contact_res.get(cid_int) or {}
        action = _blank(decided.get("action")).upper()
        planned = row.get("action")
        if planned == "POSSIBLE" and action not in {"KEEP_SEPARATE", "MERGE_DUPLICATE", "MOVE_UNIQUE"}:
            problems.append(
                f"Contact {cid} is REVIEW_REQUIRED (possible duplicate); "
                "supply KEEP_SEPARATE, MOVE_UNIQUE, or MERGE_DUPLICATE."
            )
            continue
        if planned == "MERGE" and action != "MERGE_DUPLICATE":
            problems.append(
                f"Contact {cid} is a high-confidence duplicate; "
                "explicit MERGE_DUPLICATE with survivor_contact_id is required."
            )
            continue
        if action == "MERGE_DUPLICATE":
            sid = decided.get("survivor_contact_id")
            if sid is None:
                problems.append(
                    f"Contact {cid} MERGE_DUPLICATE requires an explicit survivor_contact_id. "
                    "Never auto-select lowest/oldest/newest/most-complete."
                )
                continue
            sid_int = int(sid)
            if sid_int == cid_int:
                problems.append(f"Contact {cid} cannot merge into itself.")
                continue
            if sid_int not in dest_ids and sid_int not in source_ids:
                problems.append(
                    f"Contact {cid} MERGE_DUPLICATE survivor {sid_int} is not a contact "
                    "on the source or survivor company."
                )
                continue
            if sid_int in source_ids:
                target_action = _action_for(sid_int)
                if target_action == "MERGE_DUPLICATE":
                    # Allowed only as an intermediate in an explicit chain; still
                    # record the edge so cycle detection can run.
                    pass
                elif target_action not in {"MOVE_UNIQUE", "KEEP_SEPARATE"}:
                    problems.append(
                        f"Contact {cid} source-side MERGE_DUPLICATE target {sid_int} "
                        "must itself be approved MOVE_UNIQUE or KEEP_SEPARATE "
                        "(or an explicit intermediate MERGE_DUPLICATE)."
                    )
            merge_edges[cid_int] = sid_int
            fields = decided.get("fields") or {}
            approved_survivor = by_id.get(sid_int) or {}
            source_row = by_id.get(cid_int) or {}
            for field in ("first_name", "last_name", "title", "email"):
                src_val = source_row.get(field)
                dst_val = approved_survivor.get(field)
                src_blank = src_val is None or _blank(src_val) == ""
                dst_blank = dst_val is None or _blank(dst_val) == ""
                if src_blank or dst_blank:
                    continue
                if field == "title" and _titles_equivalent(src_val, dst_val):
                    continue
                if field == "email" and _norm_email(src_val) == _norm_email(dst_val):
                    continue
                if _blank(src_val) == _blank(dst_val):
                    continue
                decided_field = fields.get(field)
                if _is_do_not_carry(decided_field):
                    continue
                if _blank(decided_field).lower() in {
                    "keep_survivor",
                    "keep_source",
                    "survivor",
                    "source",
                }:
                    continue
                if decided_field:
                    continue
                problems.append(
                    f"Contact {cid} conflicting {field} versus approved survivor {sid_int} "
                    "requires explicit fields.{field} "
                    "(keep_survivor, keep_source, or do_not_carry_from_source)."
                )

        if planned == "MOVE" and action and action not in {"MOVE_UNIQUE", "KEEP_SEPARATE", "MERGE_DUPLICATE"}:
            problems.append(f"Contact {cid} has unsupported action {action}.")

    # Cycle / dangling merge-graph checks. No implicit survivor selection.
    for start, _dst in merge_edges.items():
        walking = start
        hops = 0
        trail = []
        while walking in merge_edges:
            if walking in trail:
                problems.append(
                    f"Contact MERGE_DUPLICATE cycle involving {start}."
                )
                break
            trail.append(walking)
            walking = merge_edges[walking]
            hops += 1
            if hops > 16:
                problems.append(f"Contact MERGE_DUPLICATE chain too long from {start}.")
                break
        final = walking
        if final not in dest_ids and final not in source_ids:
            problems.append(
                f"Contact {start} merge chain ends at missing contact {final}."
            )
        elif final in source_ids and _action_for(final) not in {
            "MOVE_UNIQUE",
            "KEEP_SEPARATE",
            "",
        }:
            if _action_for(final) == "MERGE_DUPLICATE":
                problems.append(
                    f"Contact {start} merge chain does not end at an explicit surviving contact."
                )

    for pair in (plan.get("ccrs") or {}).get("pairs") or []:
        if not pair.get("same_client"):
            continue
        client_id = str(int(pair["client_id"]))
        decided = (resolution.get("ccrs") or {}).get(client_id) or (
            resolution.get("ccrs") or {}
        ).get(int(client_id)) or {}
        if pair.get("status_conflict") and _blank(decided.get("status")) not in {
            "keep_survivor",
            "keep_source",
        } and _blank(decided.get("status")) == "":
            problems.append(
                f"Same-client CCR for client {client_id} has different statuses; "
                "approved status winner is required."
            )
        src_rep = pair.get("source_assigned_user_id")
        dst_rep = pair.get("survivor_assigned_user_id")
        if src_rep not in (None, "") and dst_rep not in (None, "") and int(src_rep) != int(dst_rep):
            if _blank(decided.get("assigned_user_id")) not in {"keep_survivor", "keep_source"}:
                problems.append(
                    f"Same-client CCR for client {client_id} has different assigned reps; "
                    "approved assigned_user_id winner is required."
                )
        src_fu = _blank(pair.get("source_follow_up_date"))
        dst_fu = _blank(pair.get("survivor_follow_up_date"))
        if src_fu and dst_fu and src_fu != dst_fu:
            if _blank(decided.get("follow_up_date")) not in {"keep_survivor", "keep_source"}:
                problems.append(
                    f"Same-client CCR for client {client_id} has conflicting follow-up dates."
                )
        src_na = _blank(pair.get("source_next_action"))
        dst_na = _blank(pair.get("survivor_next_action"))
        if src_na and dst_na and src_na != dst_na:
            if _blank(decided.get("next_action")) not in {"keep_survivor", "keep_source"}:
                problems.append(
                    f"Same-client CCR for client {client_id} has conflicting next actions."
                )
    return problems


def _pick(winner: str, source_value: Any, survivor_value: Any) -> Any:
    choice = _blank(winner).lower()
    if choice in {"keep_source", "source"}:
        return source_value
    return survivor_value


def _safe_fill_contact(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    survivor: dict[str, Any],
    fields_res: dict[str, Any],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for key in CONTACT_FIELD_KEYS:
        if key not in survivor:
            continue
        src_val = source.get(key)
        dst_val = survivor.get(key)
        src_blank = src_val is None or _blank(src_val) == ""
        dst_blank = dst_val is None or _blank(dst_val) == ""
        decided = fields_res.get(key)
        if decided:
            if _is_do_not_carry(decided):
                continue
            if _blank(decided).lower() in {"keep_survivor", "survivor"}:
                continue
            if _blank(decided).lower() in {"keep_source", "source"}:
                updates[key] = src_val
            else:
                updates[key] = decided
            continue
        if dst_blank and not src_blank:
            if key in {"first_name", "last_name"}:
                continue
            updates[key] = src_val
            continue
        if src_blank or dst_blank:
            continue
        if _blank(src_val) == _blank(dst_val):
            continue
        if key == "email":
            if _norm_email(src_val) == _norm_email(dst_val):
                continue
            raise MergeBlockedError(
                f"Contact {source.get('id')} has two populated emails; resolve fields.email."
            )
        if key == "title" and _titles_equivalent(src_val, dst_val):
            continue
        if key == "external_record_no":
            continue
        if key in {"phone", "alt_phone"}:
            s10, _ = canonical_contact_phone(_blank(src_val))
            t10, _ = canonical_contact_phone(_blank(dst_val))
            if s10 and t10 and s10 == t10:
                continue
            if key == "phone" and s10 and t10 and s10 != t10:
                if dst_blank is False and (_blank(survivor.get("alt_phone")) == "") and s10:
                    updates["alt_phone"] = src_val
                    if source.get("phone_extension") and not _blank(survivor.get("alt_phone_extension")):
                        updates["alt_phone_extension"] = source.get("phone_extension")
            continue
        if key in {"first_name", "last_name", "title"}:
            raise MergeBlockedError(
                f"Contact field {key} conflict requires explicit fields.{key}."
            )
    return updates


def _apply_contact_updates(
    conn: sqlite3.Connection, contact_id: int, updates: dict[str, Any]
) -> None:
    if not updates:
        return
    cols = _table_columns(conn, "contacts")
    parts = []
    params: list[Any] = []
    for key, value in updates.items():
        if key not in cols:
            continue
        parts.append(f"{key} = ?")
        params.append(value)
    if not parts:
        return
    params.append(int(contact_id))
    conn.execute(f"UPDATE contacts SET {', '.join(parts)} WHERE id = ?", params)


def _remap_fk(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    old_id: int,
    new_id: int,
    *,
    note_union: bool = True,
) -> None:
    if not _table_exists(conn, table):
        return
    cols = _table_columns(conn, table)
    if column not in cols:
        return
    pk = _pk_column(conn, table)
    uniques = [u for u in _unique_colsets(conn, table) if column in u]
    # INTEGER PRIMARY KEY is a rowid alias and often does not appear in
    # PRAGMA index_list, so 1:1 tables like contact_person_keys would miss
    # UNION/DEDUPE and fail UNIQUE/FK when remapping two live-shaped contacts.
    if pk and column == pk and [pk] not in uniques:
        uniques.append([pk])
    rows = conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (int(old_id),)).fetchall()
    for row in rows:
        payload = _row_dict(row)
        collided = False
        for ukeys in uniques:
            clauses = []
            params: list[Any] = []
            for col in ukeys:
                if col == column:
                    clauses.append(f"{col} = ?")
                    params.append(int(new_id))
                else:
                    clauses.append(f"{col} IS ?")
                    params.append(payload.get(col))
            hit = conn.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} LIMIT 1",
                params,
            ).fetchone()
            if hit is not None:
                collided = True
                existing = _row_dict(hit)
                if note_union:
                    for ncol in NOTE_COLUMN_NAMES:
                        if ncol not in cols:
                            continue
                        merged = _append_distinct(existing.get(ncol), payload.get(ncol))
                        if pk and merged != _blank(existing.get(ncol)):
                            conn.execute(
                                f"UPDATE {table} SET {ncol} = ? WHERE {pk} = ?",
                                (merged, existing.get(pk)),
                            )
                break
        if collided:
            if pk:
                conn.execute(f"DELETE FROM {table} WHERE {pk} = ?", (payload.get(pk),))
            continue
        if pk:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {pk} = ?",
                (int(new_id), payload.get(pk)),
            )
        else:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                (int(new_id), int(old_id)),
            )


def _tables_with_column(conn: sqlite3.Connection, column: str) -> list[str]:
    names = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        table = str(name)
        if column in _table_columns(conn, table):
            names.append(table)
    return names


def _preserve_identities(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    source_id: int,
    survivor_id: int,
    location_id: int | None,
    resolution: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    source = plan.get("source") or {}
    source_rn = _blank(source.get("external_record_no"))
    source_name = _blank(source.get("company_name"))
    if _table_exists(conn, "company_source_identities"):
        cur = conn.execute(
            """
            UPDATE company_source_identities
            SET company_id = ?, location_id = COALESCE(?, location_id)
            WHERE company_id = ?
            """,
            (survivor_id, location_id, source_id),
        )
        created.append({"kind": "moved_identity_rows", "count": int(cur.rowcount or 0)})
    if source_rn:
        ident_id, status = upsert_company_source_identity(
            conn,
            company_id=survivor_id,
            location_id=location_id,
            source_system=SOURCE_SYSTEM_DEFAULT,
            source_record_no=source_rn,
            source_company_name=source_name,
            source_address=_blank(source.get("address")),
            source_city=_blank(source.get("city")),
            source_state=_blank(source.get("state")),
            source_zip=_blank(source.get("zip")),
            source_phone=_blank(source.get("phone")),
            source_website=_blank(source.get("website")),
        )
        if status == "conflict":
            raise MergeExecutionError(
                f"Source identity conflict for RN {source_rn} — transaction blocked."
            )
        created.append({"kind": "source_master_rn", "id": ident_id, "status": status, "rn": source_rn})
    for pair in (plan.get("ccrs") or {}).get("pairs") or []:
        rn = _blank(pair.get("source_rn"))
        if not rn:
            continue
        ident_id, status = upsert_company_source_identity(
            conn,
            company_id=survivor_id,
            location_id=location_id,
            client_id=int(pair["client_id"]) if pair.get("client_id") is not None else None,
            source_system=SOURCE_SYSTEM_DEFAULT,
            source_record_no=rn,
            source_company_name=source_name,
        )
        if status == "conflict":
            raise MergeExecutionError(
                f"CCR identity conflict for RN {rn} — transaction blocked."
            )
        created.append(
            {
                "kind": "ccr_rn",
                "id": ident_id,
                "status": status,
                "rn": rn,
                "client_id": pair.get("client_id"),
            }
        )
    extra = []
    payload = resolution or {}
    identities = payload.get("identities") or {}
    extra.extend(payload.get("additional_source_identities") or [])
    extra.extend(identities.get("create") or [])
    seen_extra = set()
    for row in extra:
        if not isinstance(row, dict):
            continue
        rn = _blank(row.get("rn") or row.get("source_record_no"))
        system = _blank(row.get("source_system")) or SOURCE_SYSTEM_DEFAULT
        if not rn:
            continue
        key = (system, rn, row.get("client_id"))
        if key in seen_extra:
            continue
        seen_extra.add(key)
        ident_id, status = upsert_company_source_identity(
            conn,
            company_id=survivor_id,
            location_id=location_id,
            client_id=int(row["client_id"]) if row.get("client_id") is not None else None,
            source_system=system,
            source_record_no=rn,
            source_company_name=_blank(row.get("source_company_name")) or source_name,
            source_address=_blank(row.get("source_address")),
            source_city=_blank(row.get("source_city")),
            source_state=_blank(row.get("source_state")),
            source_zip=_blank(row.get("source_zip")),
            source_phone=_blank(row.get("source_phone")),
            source_website=_blank(row.get("source_website")),
        )
        if status == "conflict":
            raise MergeExecutionError(
                f"Additional source identity conflict for RN {rn} — transaction blocked."
            )
        created.append(
            {
                "kind": "additional_rn",
                "id": ident_id,
                "status": status,
                "rn": rn,
                "source_system": system,
            }
        )
    return created


def _ensure_locations(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    source_id: int,
    survivor_id: int,
    *,
    allow_distinct: bool,
) -> int | None:
    loc_plan = plan.get("locations") or {}
    classes = {c.get("classification") for c in loc_plan.get("comparisons") or []}
    survivor = _company_snapshot(conn, survivor_id) or (plan.get("survivor") or {})
    source = _company_snapshot(conn, source_id) or (plan.get("source") or {})
    chosen = None
    if "DISTINCT_SITE" in classes and allow_distinct:
        seed = source
    else:
        seed = survivor
        if not _blank(seed.get("address")) and not _blank(seed.get("city")):
            pass
        elif _blank(source.get("address")) == "" and _blank(source.get("city")) == "":
            pass
        elif not _blank(source.get("address")) or not _blank(source.get("city")):
            if _blank(seed.get("address")):
                seed = {**seed, "address": source.get("address")}
            if _blank(seed.get("city")):
                seed = {**seed, "city": source.get("city")}
    if not _blank(seed.get("address")) and not _blank(seed.get("city")):
        existing = loc_plan.get("survivor_locations") or []
        if existing:
            return int(existing[0]["id"])
        return None
    loc_id, _status = ensure_company_location(
        conn,
        company_id=int(survivor_id),
        location_name=_blank(seed.get("company_name")) or _blank(seed.get("city")),
        location_type="facility",
        address=_blank(seed.get("address")),
        city=_blank(seed.get("city")),
        state=_blank(seed.get("state")),
        zip_code=_blank(seed.get("zip")),
        phone=_blank(seed.get("phone")),
        website=_blank(seed.get("website")),
    )
    if not loc_id:
        raise MergeExecutionError("SAME_SITE merge requires a surviving company_location row.")
    chosen = loc_id
    if _table_exists(conn, "company_locations"):
        for row in conn.execute(
            "SELECT id FROM company_locations WHERE company_id = ?",
            (source_id,),
        ).fetchall():
            lid = int(row[0])
            if allow_distinct and "DISTINCT_SITE" in classes:
                conn.execute(
                    "UPDATE company_locations SET company_id = ? WHERE id = ?",
                    (survivor_id, lid),
                )
            else:
                conn.execute(
                    "UPDATE company_source_identities SET location_id = ? WHERE location_id = ?",
                    (chosen, lid),
                )
                for table in ("contacts", "client_company_relationships", "company_shared_history_events", "client_sales_events"):
                    if _table_exists(conn, table) and "location_id" in _table_columns(conn, table):
                        conn.execute(
                            f"UPDATE {table} SET location_id = ? WHERE location_id = ?",
                            (chosen, lid),
                        )
                conn.execute("DELETE FROM company_locations WHERE id = ?", (lid,))
    return chosen


def _fill_company_blanks(
    conn: sqlite3.Connection,
    source_id: int,
    survivor_id: int,
    field_res: dict[str, Any],
) -> None:
    cols = _table_columns(conn, "companies")
    src = _row_dict(
        conn.execute("SELECT * FROM companies WHERE id = ?", (source_id,)).fetchone()
    )
    dst = _row_dict(
        conn.execute("SELECT * FROM companies WHERE id = ?", (survivor_id,)).fetchone()
    )
    updates: dict[str, Any] = {}
    for key in COMPANY_SAFE_FILL_FIELDS:
        if key not in cols:
            continue
        decided = field_res.get(key)
        if decided:
            updates[key] = _pick(decided, src.get(key), dst.get(key)) if _blank(decided) in {
                "keep_source",
                "keep_survivor",
                "source",
                "survivor",
            } else decided
            continue
        if _blank(dst.get(key)) == "" and _blank(src.get(key)):
            updates[key] = src.get(key)
    if "external_record_no" in field_res:
        raise MergeExecutionError("Survivor master RN cannot be overwritten by merge.")
    if updates:
        parts = [f"{k} = ?" for k in updates]
        params = list(updates.values()) + [survivor_id]
        conn.execute(f"UPDATE companies SET {', '.join(parts)} WHERE id = ?", params)


def _load_contact_row(conn: sqlite3.Connection, contact_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (int(contact_id),)).fetchone()
    if row is None:
        raise MergeExecutionError(f"Contact {contact_id} does not exist.")
    return _row_dict(row)


def _remap_contact_dependencies(
    conn: sqlite3.Connection, source_contact_id: int, survivor_contact_id: int
) -> list[str]:
    remapped = []
    for table in _tables_with_column(conn, "contact_id"):
        if table in SKIP_CONTACT_REMAP:
            continue
        before = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE contact_id = ?",
                (int(source_contact_id),),
            ).fetchone()[0]
        )
        if before == 0:
            continue
        _remap_fk(conn, table, "contact_id", source_contact_id, survivor_contact_id)
        remapped.append(table)
    return remapped


def _merge_contact_into(
    conn: sqlite3.Connection,
    source_contact_id: int,
    survivor_contact_id: int,
    *,
    source_company_id: int,
    survivor_company_id: int,
    resolution: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    src_id = int(source_contact_id)
    dst_id = int(survivor_contact_id)
    if src_id == dst_id:
        raise MergeBlockedError(f"Contact {src_id} cannot merge into itself.")
    source = _load_contact_row(conn, src_id)
    survivor = _load_contact_row(conn, dst_id)
    updates = _safe_fill_contact(conn, source, survivor, fields or {})
    _apply_contact_updates(conn, dst_id, updates)
    deps = _remap_contact_dependencies(conn, src_id, dst_id)
    flatten_inbound_contact_redirects(
        conn, old_survivor_contact_id=src_id, new_survivor_contact_id=dst_id
    )
    record_contact_merge_redirect(
        conn,
        source_contact_id=src_id,
        survivor_contact_id=dst_id,
        source_company_id=source_company_id,
        survivor_company_id=survivor_company_id,
        reason=_blank(resolution.get("reason")),
        operation_id=_blank(resolution.get("operation_id")),
        merged_by_user_id=resolution.get("actor_user_id"),
        summary={"fields_applied": updates, "action": "MERGE_DUPLICATE", "deps": deps},
    )
    if resolve_contact_id(conn, src_id) != dst_id:
        raise MergeExecutionError(
            f"Contact redirect {src_id} → {dst_id} failed after insert."
        )
    if resolve_contact_id(conn, dst_id) != dst_id:
        raise MergeExecutionError(
            f"Survivor contact {dst_id} unexpectedly redirects elsewhere."
        )
    conn.execute("DELETE FROM contacts WHERE id = ?", (src_id,))
    leftover = conn.execute(
        "SELECT 1 FROM contacts WHERE id = ? LIMIT 1", (src_id,)
    ).fetchone()
    if leftover is not None:
        raise MergeExecutionError(f"Contact {src_id} was not retired after merge.")
    return {
        "source_contact_id": src_id,
        "survivor_contact_id": dst_id,
        "fields": updates,
        "deps": deps,
    }


def _ordered_merge_edges(edges: dict[int, int]) -> list[tuple[int, int]]:
    """Process leaf duplicates first so intermediate targets still exist."""
    remaining = dict(edges)
    ordered: list[tuple[int, int]] = []
    while remaining:
        dests = set(remaining.values())
        leaves = sorted(src for src in remaining if src not in dests)
        if not leaves:
            raise MergeBlockedError(
                f"Contact MERGE_DUPLICATE cycle involving {sorted(remaining)}."
            )
        for src in leaves:
            ordered.append((src, remaining.pop(src)))
    return ordered


def _consolidate_contacts(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    resolution: dict[str, Any],
    source_id: int,
    survivor_id: int,
) -> dict[str, Any]:
    result = {"moved": [], "merged": [], "kept_separate": [], "order": []}
    planned = {
        int(c["source_contact_id"]): c
        for c in (plan.get("contacts") or {}).get("classifications") or []
    }

    def _action(cid: int) -> str:
        decided = _decided_contact(resolution, cid)
        action = _blank(decided.get("action")).upper()
        if action:
            return action
        row = planned.get(cid) or {}
        if row.get("action") == "MOVE":
            return "MOVE_UNIQUE"
        if row.get("action") == "KEEP_SEPARATE":
            return "KEEP_SEPARATE"
        return ""

    merge_edges: dict[int, int] = {}
    move_ids: list[int] = []
    for src_contact_id, row in planned.items():
        action = _action(src_contact_id)
        if action == "MERGE_DUPLICATE":
            decided = _decided_contact(resolution, src_contact_id)
            if decided.get("survivor_contact_id") is None:
                raise MergeBlockedError(
                    f"Contact {src_contact_id} MERGE_DUPLICATE requires explicit "
                    "survivor_contact_id. Never auto-select an ID."
                )
            merge_edges[src_contact_id] = int(decided["survivor_contact_id"])
        elif action in {"MOVE_UNIQUE", "KEEP_SEPARATE"}:
            move_ids.append(src_contact_id)
        elif action:
            raise MergeBlockedError(
                f"Contact {src_contact_id} has unsupported action {action}."
            )
        else:
            raise MergeBlockedError(f"Contact {src_contact_id} missing executable action.")

    for src_contact_id, dst_id in _ordered_merge_edges(merge_edges):
        decided = _decided_contact(resolution, src_contact_id)
        merged = _merge_contact_into(
            conn,
            src_contact_id,
            dst_id,
            source_company_id=source_id,
            survivor_company_id=survivor_id,
            resolution=resolution,
            fields=decided.get("fields") or {},
        )
        result["merged"].append(merged)
        result["order"].append(
            {"step": "source_merge", "source_contact_id": src_contact_id, "survivor_contact_id": dst_id}
        )
        _checkpoint(resolution, f"source_merge:{src_contact_id}")

    _checkpoint(resolution, "contact_redirects")

    for src_contact_id in sorted(move_ids):
        still = conn.execute(
            "SELECT 1 FROM contacts WHERE id = ? LIMIT 1", (src_contact_id,)
        ).fetchone()
        if still is None:
            continue
        conn.execute(
            "UPDATE contacts SET company_id = ? WHERE id = ?",
            (survivor_id, src_contact_id),
        )
        action = _action(src_contact_id)
        bucket = "moved" if action == "MOVE_UNIQUE" else "kept_separate"
        result[bucket].append({"contact_id": src_contact_id, "action": action})
        result["order"].append({"step": "contact_move", "contact_id": src_contact_id, "action": action})
        _checkpoint(resolution, f"contact_move:{src_contact_id}")
    return result


def _consolidate_ccrs(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    resolution: dict[str, Any],
    survivor_id: int,
) -> dict[str, Any]:
    out = {"consolidated": [], "remapped": []}
    ccr_res = resolution.get("ccrs") or {}
    for pair in (plan.get("ccrs") or {}).get("pairs") or []:
        if pair.get("same_client"):
            decided = ccr_res.get(str(int(pair["client_id"]))) or ccr_res.get(
                int(pair["client_id"])
            ) or {}
            src_id = int(pair["source_ccr_id"])
            dst_id = int(pair["survivor_ccr_id"])
            src = _row_dict(
                conn.execute(
                    "SELECT * FROM client_company_relationships WHERE id = ?", (src_id,)
                ).fetchone()
            )
            dst = _row_dict(
                conn.execute(
                    "SELECT * FROM client_company_relationships WHERE id = ?", (dst_id,)
                ).fetchone()
            )
            updates: dict[str, Any] = {}
            status_choice = _blank(decided.get("status")) or "keep_survivor"
            if pair.get("status_conflict"):
                updates["status"] = _pick(status_choice, src.get("status"), dst.get("status"))
            elif _blank(dst.get("status")) and _blank(src.get("status")):
                updates["status"] = src.get("status")
            if decided.get("assigned_user_id"):
                updates["assigned_user_id"] = _pick(
                    decided.get("assigned_user_id"),
                    src.get("assigned_user_id"),
                    dst.get("assigned_user_id"),
                )
            elif dst.get("assigned_user_id") in (None, "") and src.get("assigned_user_id") not in (None, ""):
                updates["assigned_user_id"] = src.get("assigned_user_id")
            if decided.get("next_action"):
                updates["next_action"] = _pick(
                    decided.get("next_action"), src.get("next_action"), dst.get("next_action")
                )
            elif _blank(dst.get("next_action")) and _blank(src.get("next_action")):
                updates["next_action"] = src.get("next_action")
            if decided.get("follow_up_date"):
                updates["follow_up_date"] = _pick(
                    decided.get("follow_up_date"),
                    src.get("follow_up_date"),
                    dst.get("follow_up_date"),
                )
            elif dst.get("follow_up_date") in (None, "") and src.get("follow_up_date") not in (None, ""):
                updates["follow_up_date"] = src.get("follow_up_date")
            hot_rule = _blank(decided.get("is_hot"))
            if hot_rule == "or":
                updates["is_hot"] = 1 if int(src.get("is_hot") or 0) or int(dst.get("is_hot") or 0) else 0
            elif _blank(dst.get("is_hot")) in {"", "0"} and int(src.get("is_hot") or 0):
                if hot_rule == "or" or decided.get("is_hot") == "or":
                    updates["is_hot"] = 1
            updates["notes"] = _append_distinct(dst.get("notes"), src.get("notes"))
            if updates:
                parts = [f"{k} = ?" for k in updates]
                params = list(updates.values()) + [dst_id]
                conn.execute(
                    f"UPDATE client_company_relationships SET {', '.join(parts)} WHERE id = ?",
                    params,
                )
            for table in _tables_with_column(conn, "relationship_id"):
                _remap_fk(conn, table, "relationship_id", src_id, dst_id)
            conn.execute("DELETE FROM client_company_relationships WHERE id = ?", (src_id,))
            out["consolidated"].append(
                {"source_ccr_id": src_id, "survivor_ccr_id": dst_id, "client_id": pair.get("client_id")}
            )
        elif pair.get("source_ccr_id") and not pair.get("survivor_ccr_id"):
            conn.execute(
                "UPDATE client_company_relationships SET company_id = ? WHERE id = ?",
                (survivor_id, int(pair["source_ccr_id"])),
            )
            out["remapped"].append(
                {"ccr_id": pair["source_ccr_id"], "client_id": pair.get("client_id")}
            )
    return out


def _union_campaigns(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    moved, unioned = [], []
    if not _table_exists(conn, "campaign_companies"):
        return {"moved": moved, "unioned": unioned}
    src_rows = [
        _row_dict(r)
        for r in conn.execute(
            "SELECT * FROM campaign_companies WHERE company_id = ?", (source_id,)
        ).fetchall()
    ]
    for row in src_rows:
        existing = conn.execute(
            """
            SELECT * FROM campaign_companies
            WHERE campaign_id = ? AND company_id = ?
            """,
            (row["campaign_id"], survivor_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                "UPDATE campaign_companies SET company_id = ? WHERE id = ?",
                (survivor_id, row["id"]),
            )
            moved.append(row["campaign_id"])
        else:
            payload = _row_dict(existing)
            merged = _append_distinct(payload.get("notes"), row.get("notes"))
            conn.execute(
                "UPDATE campaign_companies SET notes = ? WHERE id = ?",
                (merged, payload["id"]),
            )
            conn.execute("DELETE FROM campaign_companies WHERE id = ?", (row["id"],))
            unioned.append(row["campaign_id"])
    return {"moved": moved, "unioned": unioned}


def _remap_notes_and_history(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    stats = {"history_moved": 0, "history_skipped_dup": 0, "legacy_moved": 0, "legacy_skipped_dup": 0}
    if _table_exists(conn, "company_shared_history_events"):
        src_rows = conn.execute(
            "SELECT id, event_hash FROM company_shared_history_events WHERE company_id = ?",
            (source_id,),
        ).fetchall()
        tgt_hashes = {
            _blank(r[0])
            for r in conn.execute(
                "SELECT event_hash FROM company_shared_history_events WHERE company_id = ?",
                (survivor_id,),
            ).fetchall()
        }
        for row in src_rows:
            if _blank(row[1]) in tgt_hashes:
                conn.execute("DELETE FROM company_shared_history_events WHERE id = ?", (row[0],))
                stats["history_skipped_dup"] += 1
            else:
                conn.execute(
                    "UPDATE company_shared_history_events SET company_id = ? WHERE id = ?",
                    (survivor_id, row[0]),
                )
                stats["history_moved"] += 1
    if _table_exists(conn, "legacy_notes"):
        src_rows = [
            _row_dict(r)
            for r in conn.execute(
                "SELECT * FROM legacy_notes WHERE company_id = ?", (source_id,)
            ).fetchall()
        ]
        tgt_rows = [
            _row_dict(r)
            for r in conn.execute(
                "SELECT * FROM legacy_notes WHERE company_id = ?", (survivor_id,)
            ).fetchall()
        ]
        tgt_keys = {
            (int(r["client_id"]), _norm_note(r.get("note_text"))) for r in tgt_rows
        }
        for row in src_rows:
            key = (int(row["client_id"]), _norm_note(row.get("note_text")))
            if key in tgt_keys:
                conn.execute("DELETE FROM legacy_notes WHERE id = ?", (row["id"],))
                stats["legacy_skipped_dup"] += 1
            else:
                conn.execute(
                    "UPDATE legacy_notes SET company_id = ? WHERE id = ?",
                    (survivor_id, row["id"]),
                )
                stats["legacy_moved"] += 1
    return stats


def _transfer_aliases(
    conn: sqlite3.Connection, source_id: int, survivor_id: int, source_name: str, survivor_name: str
) -> dict[str, Any]:
    from company_aliases import _alias_norm, upsert_company_alias

    moved = 0
    skipped = 0
    if _table_exists(conn, "company_aliases"):
        rows = [
            _row_dict(r)
            for r in conn.execute(
                "SELECT * FROM company_aliases WHERE company_id = ?", (source_id,)
            ).fetchall()
        ]
        for row in rows:
            hit = conn.execute(
                """
                SELECT id FROM company_aliases
                WHERE company_id = ?
                  AND COALESCE(client_id, 0) = COALESCE(?, 0)
                  AND source_system = ?
                  AND source_record_no = ?
                  AND alias_norm = ?
                LIMIT 1
                """,
                (
                    survivor_id,
                    row.get("client_id"),
                    row.get("source_system"),
                    row.get("source_record_no"),
                    row.get("alias_norm"),
                ),
            ).fetchone()
            if hit is not None:
                conn.execute("DELETE FROM company_aliases WHERE id = ?", (row["id"],))
                skipped += 1
            else:
                conn.execute(
                    "UPDATE company_aliases SET company_id = ? WHERE id = ?",
                    (survivor_id, row["id"]),
                )
                moved += 1
    survivor_norm = _alias_norm(survivor_name)
    source_norm = _alias_norm(source_name)
    if source_name and source_norm and source_norm != survivor_norm:
        _id, created = upsert_company_alias(
            conn,
            company_id=survivor_id,
            alias_name=source_name,
            source_system=SOURCE_SYSTEM_DEFAULT,
            source_record_no="",
        )
        if created:
            moved += 1
        else:
            skipped += 1
    elif source_norm == survivor_norm:
        skipped += 1
    return {"moved": moved, "skipped_same_norm": skipped}


def _remap_remaining_company_fks(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> list[str]:
    remapped = []
    for table in _tables_with_column(conn, "company_id"):
        if table in SKIP_COMPANY_REMAP:
            continue
        before = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE company_id = ?", (source_id,)
            ).fetchone()[0]
        )
        if before == 0:
            continue
        _remap_fk(conn, table, "company_id", source_id, survivor_id)
        remapped.append(table)
    return remapped


def _remaining_company_refs(conn: sqlite3.Connection, source_id: int) -> list[dict[str, Any]]:
    leftover = []
    for ref in discover_company_id_refs(conn):
        table = ref["table"]
        n = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE company_id = ?", (source_id,)
            ).fetchone()[0]
        )
        if n:
            leftover.append({"table": table, "count": n})
    return leftover


def execute_company_merge(
    conn: sqlite3.Connection,
    source_company_id: int,
    survivor_company_id: int,
    approved_resolution: dict[str, Any],
) -> dict[str, Any]:
    """Atomically consolidate source into survivor.

    Isolated copies may run with a stamped fingerprint + approved_resolution.
    Live northstar.db also requires approved_resolution.live_approval_id for a
    pending merge_execution_approvals row matching this exact pair/fingerprint.
    Rolls back completely on any failure. Failed live attempts consume the
    approval as failed and require a new approval.
    """
    src = int(source_company_id)
    dst = int(survivor_company_id)
    path = connection_db_path(conn)
    is_prod = path is not None and path == PRODUCTION_DB_PATH.resolve()
    approval_id_raw = approved_resolution.get("live_approval_id")
    approval_id = int(approval_id_raw) if approval_id_raw not in (None, "") else None
    if is_prod:
        if approval_id is None:
            raise MergeExecutionError(
                "Live northstar.db merge requires a pending merge_execution_approvals "
                "row (live_approval_id) matching source, survivor, and fingerprint."
            )
    else:
        assert_not_production_connection(conn, allow_production=False)
    if src == dst:
        raise MergeExecutionError("Source and survivor must differ.")
    if not _company_snapshot(conn, src) or not _company_snapshot(conn, dst):
        raise MergeExecutionError("Source and survivor must be existing companies. Never create a new company to merge.")

    plan = plan_company_merge(conn, src, dst)
    fingerprint = merge_plan_fingerprint(plan, approved_resolution)
    expected = _blank(approved_resolution.get("plan_fingerprint"))
    if not expected:
        raise MergeStalePlanError("approved_resolution.plan_fingerprint is required.")
    if approval_id is not None:
        try:
            approval_row = load_merge_approval(conn, approval_id)
        except MergeApprovalError as exc:
            raise MergeExecutionError(str(exc)) from exc
        if int(approval_row["source_company_id"]) != src or int(approval_row["survivor_company_id"]) != dst:
            raise MergeExecutionError(
                "Approval source/survivor does not match the execute pair."
            )
        approval_fp = _blank(approval_row.get("plan_fingerprint"))
        if expected != fingerprint or (approval_fp and approval_fp != fingerprint):
            supersede_stale_approval(conn, approval_id)
            raise MergeStalePlanError(
                "Stale merge plan. Re-plan and re-approve before execution."
            )
    elif expected != fingerprint:
        raise MergeStalePlanError(
            "Stale merge plan. Re-plan and re-approve before execution."
        )
    problems = validate_merge_resolution(plan, approved_resolution)
    if problems:
        raise MergeBlockedError(" | ".join(problems))

    claimed_id: int | None = None
    if approval_id is not None:
        try:
            claim_merge_approval(
                conn,
                approval_id,
                source_company_id=src,
                survivor_company_id=dst,
                plan_fingerprint=fingerprint,
            )
        except MergeApprovalError as exc:
            raise MergeExecutionError(str(exc)) from exc
        claimed_id = approval_id

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan2 = plan_company_merge(conn, src, dst)
        fingerprint2 = merge_plan_fingerprint(plan2, approved_resolution)
        if fingerprint2 != fingerprint:
            raise MergeStalePlanError(
                "Merge state changed inside the transaction. Aborting."
            )
        problems2 = validate_merge_resolution(plan2, approved_resolution)
        if problems2:
            raise MergeBlockedError(" | ".join(problems2))

        _fill_company_blanks(conn, src, dst, approved_resolution.get("company_fields") or {})
        location_id = _ensure_locations(
            conn,
            plan2,
            src,
            dst,
            allow_distinct=bool(approved_resolution.get("allow_distinct_site")),
        )
        identities = _preserve_identities(
            conn, plan2, src, dst, location_id, approved_resolution
        )
        _checkpoint(approved_resolution, "identities")
        contacts = _consolidate_contacts(conn, plan2, approved_resolution, src, dst)
        _checkpoint(approved_resolution, "contacts")
        ccrs = _consolidate_ccrs(conn, plan2, approved_resolution, dst)
        _checkpoint(approved_resolution, "ccrs")
        campaigns = _union_campaigns(conn, src, dst)
        _checkpoint(approved_resolution, "campaigns")
        notes = _remap_notes_and_history(conn, src, dst)
        other = _remap_remaining_company_fks(conn, src, dst)
        _checkpoint(approved_resolution, "history")
        aliases = _transfer_aliases(
            conn,
            src,
            dst,
            _blank((plan2.get("source") or {}).get("company_name")),
            _blank((plan2.get("survivor") or {}).get("company_name")),
        )
        leftover = _remaining_company_refs(conn, src)
        leftover = [r for r in leftover if r["table"] != "company_identity_keys"]
        if _table_exists(conn, "company_identity_keys"):
            conn.execute("DELETE FROM company_identity_keys WHERE company_id = ?", (src,))
        if _table_exists(conn, "search_fts"):
            try:
                conn.execute(
                    "UPDATE search_fts SET company_id = ? WHERE company_id = ?",
                    (dst, src),
                )
            except sqlite3.Error:
                conn.execute("DELETE FROM search_fts WHERE company_id = ?", (src,))
        leftover = _remaining_company_refs(conn, src)
        leftover = [r for r in leftover if r["table"] != "company_identity_keys"]
        if leftover:
            raise MergeExecutionError(f"Unsafe source dependencies remain: {leftover}")
        redirect_id = record_company_merge_redirect(
            conn,
            source_company_id=src,
            survivor_company_id=dst,
            reason=_blank(approved_resolution.get("reason")),
            operation_id=_blank(approved_resolution.get("operation_id")),
            merged_by_user_id=approved_resolution.get("actor_user_id"),
            summary={
                "contacts": contacts,
                "ccrs": ccrs,
                "campaigns": campaigns,
                "notes": notes,
                "identities": identities,
                "aliases": aliases,
                "fingerprint": fingerprint,
            },
        )
        _checkpoint(approved_resolution, "redirect")
        conn.execute("DELETE FROM companies WHERE id = ?", (src,))
        if resolve_company_id(conn, src) != dst:
            raise MergeExecutionError("Company redirect failed after insert.")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MergeExecutionError(f"integrity_check failed: {integrity}")
        if claimed_id is not None:
            try:
                complete_merge_approval(
                    conn,
                    claimed_id,
                    company_merge_history_id=redirect_id,
                    executed_by_user_id=approved_resolution.get("actor_user_id"),
                )
            except MergeApprovalError as exc:
                raise MergeExecutionError(str(exc)) from exc
        from data_steward import SOURCE_MERGE, record_provenance, source_ref_batch

        record_provenance(
            conn,
            entity_type="company",
            entity_id=int(dst),
            field="merged_from",
            old_value=str(src),
            new_value=str(dst),
            source_type=SOURCE_MERGE,
            changed_by_user_id=approved_resolution.get("actor_user_id"),
            action="MERGE",
            source_ref=source_ref_batch("merge_history", redirect_id),
            trusted=True,
        )
        conn.commit()
        return {
            "ok": True,
            "source_company_id": src,
            "survivor_company_id": dst,
            "redirect_id": redirect_id,
            "live_approval_id": claimed_id,
            "fingerprint": fingerprint,
            "contacts": contacts,
            "ccrs": ccrs,
            "campaigns": campaigns,
            "notes": notes,
            "identities": identities,
            "aliases": aliases,
            "other_tables_remapped": other,
            "location_id": location_id,
            "test_fixture": _blank(approved_resolution.get("reason")),
        }
    except Exception as exc:
        conn.rollback()
        if claimed_id is not None:
            fail_merge_approval(conn, claimed_id, str(exc))
        raise
