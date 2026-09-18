"""LeadMaster refresh confirm readiness and isolated apply wrapper.

REFRESH CONFIRM IS NOT INITIAL IMPORT CONFIRM.
Live production databases are fail-closed.
"""

from __future__ import annotations

import json
from typing import Any

from crm_import_staging import STATUS_PREVIEWED, BatchNotReusable
from db import PRODUCTION_DB_PATH
from leadmaster_refresh_apply import RefreshApplyError, apply_leadmaster_refresh
from leadmaster_refresh_mapping import validate_refresh_mapping
from leadmaster_refresh_plan import (
    ASSIGNMENT_CHANGE,
    CAMPAIGN_CHANGE,
    PLANNER_VERSION,
    POSSIBLE_DUPLICATE,
    REVIEW_REQUIRED,
    SOURCE_ID_CONFLICT,
    UNKNOWN_SOURCE_REP,
    refresh_policy_from_profile,
)
from leadmaster_refresh_policy import (
    MODE_INITIAL_IMPORT,
    MODE_LEADMASTER_REFRESH,
    SOURCE_LEADMASTER,
)
from leadmaster_refresh_resolutions import (
    CLASSIFY_CAMPAIGN_ACTIVE,
    MAP_SOURCE_REP,
    resolution_for,
    row_is_skipped,
)
from leadmaster_refresh_staging import (
    INITIAL_IMPORT_REFRESH_BATCH,
    LIVE_REFRESH_WRITES_DISABLED,
    REFRESH_APPLY_DISABLED,
    RefreshApplyDisabled,
    RefreshLiveWriteError,
    WRONG_BATCH_MODE,
    assert_not_production_db,
    batch_import_mode,
    ensure_leadmaster_refresh_schema,
    load_incoming_rows,
    load_refresh_resolutions,
    plan_refresh_batch,
    refuse_refresh_as_initial_import,
)
from models import NorthStarUser

SUPPORTED_PLANNER_VERSIONS = frozenset({PLANNER_VERSION, "leadmaster-refresh-plan-v2"})
INITIAL_IMPORT_REFRESH_CONFIRM = (
    "This batch is an initial CRM import and cannot run through LeadMaster refresh confirm."
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def refuse_initial_as_refresh(row) -> None:
    if batch_import_mode(row) != MODE_LEADMASTER_REFRESH:
        raise BatchNotReusable(INITIAL_IMPORT_REFRESH_CONFIRM)


def _required_review_actions(row: dict[str, Any]) -> list[tuple[str, str]]:
    needed: list[tuple[str, str]] = []
    if SOURCE_ID_CONFLICT in (row.get("classifications") or []):
        needed.append(("record_no", SOURCE_ID_CONFLICT))
    if row.get("blocking"):
        needed.append(("", "blocking"))
    for prop in row.get("proposals") or []:
        action = _blank(prop.get("action"))
        if action in {
            "CONFLICT",
            POSSIBLE_DUPLICATE,
            UNKNOWN_SOURCE_REP,
            "INACTIVE_TARGET",
            "AMBIGUOUS",
            "CAMPAIGN_UNKNOWN",
            SOURCE_ID_CONFLICT,
            REVIEW_REQUIRED,
            "LOCATION_DIFFERENCE",
        }:
            needed.append((_blank(prop.get("field")), action))
    return needed


def refresh_confirm_readiness(
    conn,
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser | None,
    expected_fingerprint: str = "",
) -> dict[str, Any]:
    """Deterministic, no-write readiness. No partial confirm."""
    reasons: list[str] = []
    from db import DB_PATH
    from pathlib import Path
    import os

    active = Path(os.fspath(DB_PATH)).resolve()
    live_disabled = active == PRODUCTION_DB_PATH.resolve()
    if live_disabled:
        reasons.append("live_confirm_disabled")

    if actor is None or not bool(getattr(actor, "active", False)):
        reasons.append("actor_inactive")
    elif not bool(getattr(actor, "is_administrator", False)):
        reasons.append("not_administrator")

    row = conn.execute(
        "SELECT * FROM crm_import_batches WHERE id=? AND client_id=?",
        (int(batch_id), int(client_id)),
    ).fetchone()
    if row is None:
        return {
            "ready": False,
            "reasons": reasons + ["batch_not_found"],
            "plan": None,
            "plan_fingerprint": "",
            "live_confirm_disabled": live_disabled,
        }

    if int(row["client_id"]) != int(client_id):
        reasons.append("wrong_client")
    source_system = ""
    if "source_system" in row.keys():
        source_system = _blank(row["source_system"])
    if source_system and source_system != SOURCE_LEADMASTER:
        reasons.append("wrong_source_system")
    if source_system == "" and batch_import_mode(row) == MODE_LEADMASTER_REFRESH:
        reasons.append("wrong_source_system")
    mode = batch_import_mode(row)
    if mode != MODE_LEADMASTER_REFRESH:
        reasons.append("wrong_import_mode")
        if mode == MODE_INITIAL_IMPORT:
            reasons.append("initial_import_batch")
    if _blank(row["status"]) != STATUS_PREVIEWED:
        reasons.append("wrong_lifecycle_state")

    headers = json.loads(row["headers_json"] or "[]")
    mapping = json.loads(row["mapping_json"] or "{}")
    ok, errors, _normalized = validate_refresh_mapping(mapping, headers)
    if not ok:
        reasons.append("mapping_invalid")
        reasons.extend(errors[:3])

    policy_raw = "{}"
    if "refresh_policy_json" in row.keys():
        policy_raw = row["refresh_policy_json"] or "{}"
    try:
        policy_obj = json.loads(policy_raw)
    except json.JSONDecodeError:
        policy_obj = {}
    if not policy_obj:
        reasons.append("policy_not_frozen")

    planner_version = ""
    if "planner_version" in row.keys():
        planner_version = _blank(row["planner_version"])
    if planner_version and planner_version not in SUPPORTED_PLANNER_VERSIONS:
        reasons.append("planner_version_unsupported")

    try:
        refuse_refresh_as_initial_import(row)
        refuse_initial_as_refresh(row)
    except BatchNotReusable as exc:
        reasons.append(str(exc))

    plan = None
    fingerprint = ""
    try:
        plan = plan_refresh_batch(conn, client_id=client_id, batch_id=batch_id)
        fingerprint = _blank(plan.get("plan_fingerprint"))
        incoming, _mapping, profile, sha, _filename = load_incoming_rows(
            conn, client_id=client_id, batch_id=batch_id
        )
        if sha and sha != _blank(row["sha256"]):
            reasons.append("source_sha_mismatch")
        if expected_fingerprint and fingerprint != expected_fingerprint:
            reasons.append("fingerprint_stale")
        resolutions = load_refresh_resolutions(row)
        for planned in plan.get("rows") or []:
            source_row = int(planned.get("source_row") or 0)
            if row_is_skipped(resolutions, source_row):
                continue
            if SOURCE_ID_CONFLICT in (planned.get("classifications") or []):
                reasons.append("source_id_conflict")
            if planned.get("blocking") and not row_is_skipped(resolutions, source_row):
                reasons.append("unresolved_blocking")
            for field, action in _required_review_actions(planned):
                if action == "blocking":
                    continue
                found = resolution_for(
                    resolutions, source_row=source_row, field=field, action=action
                )
                if found is None:
                    reasons.append("missing_review_resolutions")
                    break
            for prop in planned.get("proposals") or []:
                if prop.get("action") == ASSIGNMENT_CHANGE:
                    try:
                        uid = int(prop.get("new_value"))
                    except (TypeError, ValueError):
                        reasons.append("unresolved_assignment_maps")
                        continue
                    user = conn.execute(
                        "SELECT active FROM users WHERE id = ?", (uid,)
                    ).fetchone()
                    if user is None or not bool(user["active"]):
                        reasons.append("assignment_inactive_user")
                    from leadmaster_refresh_apply import _user_may_be_assigned

                    if not _user_may_be_assigned(conn, uid, int(client_id)):
                        reasons.append("assignment_user_lacks_client_access")
                if prop.get("action") == UNKNOWN_SOURCE_REP:
                    mapped = resolution_for(
                        resolutions, source_row=source_row, action=UNKNOWN_SOURCE_REP
                    )
                    if not mapped or mapped["resolution"] != MAP_SOURCE_REP:
                        reasons.append("unresolved_assignment_maps")
                if prop.get("action") == "CAMPAIGN_UNKNOWN":
                    classified = resolution_for(
                        resolutions, source_row=source_row, action="CAMPAIGN_UNKNOWN"
                    )
                    if classified is None:
                        reasons.append("unresolved_campaign_maps")
                if prop.get("action") == CAMPAIGN_CHANGE:
                    target = profile.campaign_target_map().get(
                        _blank(
                            next(
                                (
                                    r.campaign
                                    for r in incoming
                                    if int(r.source_row) == source_row
                                ),
                                "",
                            )
                        )
                    )
                    # campaign_target_map on planner policy:
                    pol = refresh_policy_from_profile(profile)
                    source_campaign = ""
                    for inc in incoming:
                        if int(inc.source_row) == source_row:
                            source_campaign = inc.campaign
                            break
                    target = pol.campaign_target_map.get(source_campaign)
                    if not target:
                        reasons.append("unresolved_campaign_maps")
                    else:
                        camp = conn.execute(
                            "SELECT client_id, is_active FROM client_campaigns WHERE id=?",
                            (int(target),),
                        ).fetchone()
                        if camp is None:
                            reasons.append("campaign_missing")
                        elif int(camp["client_id"]) != int(client_id):
                            reasons.append("campaign_wrong_client")
                        elif not bool(camp["is_active"]):
                            reasons.append("campaign_inactive")
    except Exception as exc:
        reasons.append(f"plan_error:{exc}")

    unique = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    ready = not unique
    return {
        "ready": ready,
        "reasons": unique,
        "plan": plan,
        "plan_fingerprint": fingerprint,
        "live_confirm_disabled": live_disabled,
        "apply_enabled": (not live_disabled) and ready,
    }


def confirm_refresh_batch(
    conn,
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    expected_fingerprint: str,
    force_fail_after: str = "",
) -> dict[str, Any]:
    assert_not_production_db()
    if actor is None or not bool(actor.active) or not bool(actor.is_administrator):
        raise PermissionError("Administrator access required.")
    ensure_leadmaster_refresh_schema(conn)
    row = conn.execute(
        "SELECT * FROM crm_import_batches WHERE id=? AND client_id=?",
        (int(batch_id), int(client_id)),
    ).fetchone()
    if row is None:
        raise LookupError("Import batch not found.")
    refuse_refresh_as_initial_import(row)
    refuse_initial_as_refresh(row)
    gate = refresh_confirm_readiness(
        conn,
        client_id=client_id,
        batch_id=batch_id,
        actor=actor,
        expected_fingerprint=expected_fingerprint,
    )
    if gate.get("live_confirm_disabled"):
        raise RefreshLiveWriteError(LIVE_REFRESH_WRITES_DISABLED)
    if not gate["ready"]:
        raise RefreshApplyError(";".join(gate["reasons"]))

    incoming, mapping, profile, sha, filename = load_incoming_rows(
        conn, client_id=client_id, batch_id=batch_id
    )
    pol = refresh_policy_from_profile(profile)
    pol.mapping = mapping
    resolutions = load_refresh_resolutions(row)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = apply_leadmaster_refresh(
            conn,
            incoming_rows=incoming,
            policy=pol,
            source_sha256=sha,
            source_filename=filename,
            expected_fingerprint=expected_fingerprint,
            force_fail_after=force_fail_after,
            resolutions=resolutions,
            batch_id=int(batch_id),
            actor_id=int(actor.id),
            manage_transaction=False,
        )
        if force_fail_after == "before_batch_completion":
            raise RefreshApplyError("forced_rollback")
        conn.execute(
            """
            UPDATE crm_import_batches
            SET status = 'imported',
                imported_at = ?,
                imported_by_user_id = ?,
                confirmed_plan_fingerprint = ?,
                refresh_result_json = ?,
                total_imported_row_count = ?
            WHERE id = ? AND client_id = ? AND status = 'previewed'
            """,
            (
                result["confirmed_at"],
                int(actor.id),
                result["plan_fingerprint"],
                json.dumps(result.get("counts") or {}),
                int((result.get("counts") or {}).get("unchanged_rows") or 0)
                + int((result.get("created") or {}).get("companies") or 0),
                int(batch_id),
                int(client_id),
            ),
        )
        updated = conn.execute("SELECT changes()").fetchone()[0]
        if int(updated or 0) != 1:
            raise RefreshApplyError("batch_state_changed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result["batch_id"] = int(batch_id)
    result["client_id"] = int(client_id)
    result["source_system"] = SOURCE_LEADMASTER
    result["import_mode"] = MODE_LEADMASTER_REFRESH
    result["source_filename"] = filename
    result["source_sha256"] = sha
    result["mapping"] = mapping
    result["policy"] = profile.fingerprint_slice()
    result["planner_version"] = PLANNER_VERSION
    result["resolutions"] = resolutions
    return result


def confirm_refresh_http(
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    expected_fingerprint: str,
) -> dict[str, Any]:
    """HTTP entry. Live production always 409. Isolated DBs may apply."""
    assert_not_production_db()
    from db import get_connection

    with get_connection() as conn:
        return confirm_refresh_batch(
            conn,
            client_id=client_id,
            batch_id=batch_id,
            actor=actor,
            expected_fingerprint=expected_fingerprint,
        )
