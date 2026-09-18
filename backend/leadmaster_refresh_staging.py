"""LeadMaster refresh staging on crm_import_batches (mode=LEADMASTER_REFRESH).

Isolated schema helper. Live production writes are refused.
Does not run initial-import confirm.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from crm_import_staging import (
    get_crm_import_batch,
    upload_crm_import,
)
from db import PRODUCTION_DB_PATH, get_connection
from leadmaster_refresh_mapping import (
    mapped_value,
    normalize_mapping,
    validate_refresh_mapping,
)
from leadmaster_refresh_plan import IncomingHistory, IncomingRow
from leadmaster_refresh_policy import (
    MODE_INITIAL_IMPORT,
    MODE_LEADMASTER_REFRESH,
    SOURCE_LEADMASTER,
    RefreshPolicyProfile,
    default_policy,
)
from models import NorthStarUser

LIVE_REFRESH_WRITES_DISABLED = (
    "Live LeadMaster refresh writes are disabled. Use an isolated database."
)
REFRESH_APPLY_DISABLED = "Live LeadMaster refresh confirmation is not enabled."
WRONG_BATCH_MODE = "This batch is not a LeadMaster refresh."
INITIAL_IMPORT_REFRESH_BATCH = (
    "This batch is a LeadMaster refresh and cannot run through initial CRM import."
)


class RefreshLiveWriteError(PermissionError):
    pass


class RefreshApplyDisabled(PermissionError):
    pass


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def assert_not_production_db() -> None:
    from db import DB_PATH

    active = Path(os.fspath(DB_PATH)).resolve()
    if active == PRODUCTION_DB_PATH.resolve():
        raise RefreshLiveWriteError(LIVE_REFRESH_WRITES_DISABLED)


def ensure_leadmaster_refresh_schema(conn) -> None:
    """Additive columns + client-default table. No live call unless a refresh write runs."""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(crm_import_batches)")}
    additions = [
        ("import_mode", "TEXT NOT NULL DEFAULT 'INITIAL_IMPORT'"),
        ("source_system", "TEXT NOT NULL DEFAULT ''"),
        ("refresh_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("planner_version", "TEXT NOT NULL DEFAULT ''"),
        ("refresh_plan_json", "TEXT NOT NULL DEFAULT ''"),
        ("refresh_resolutions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("refresh_result_json", "TEXT NOT NULL DEFAULT '{}'"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE crm_import_batches ADD COLUMN {name} {decl}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS leadmaster_refresh_client_defaults (
            client_id INTEGER PRIMARY KEY,
            source_system TEXT NOT NULL DEFAULT 'LEADMASTER',
            policy_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by_user_id INTEGER,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS leadmaster_refresh_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            source_row INTEGER NOT NULL DEFAULT 0,
            company_id INTEGER,
            contact_id INTEGER,
            ccr_id INTEGER,
            field TEXT NOT NULL DEFAULT '',
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            skip_reason TEXT NOT NULL DEFAULT '',
            confirmed_by_user_id INTEGER,
            confirmed_at TEXT NOT NULL DEFAULT '',
            plan_fingerprint TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_lm_refresh_audit_batch
            ON leadmaster_refresh_audit_events(batch_id, client_id);
        """
    )


def batch_import_mode(row) -> str:
    if row is None:
        return MODE_INITIAL_IMPORT
    keys = row.keys() if hasattr(row, "keys") else []
    if "import_mode" not in keys:
        return MODE_INITIAL_IMPORT
    return _blank(row["import_mode"]) or MODE_INITIAL_IMPORT


def refuse_refresh_as_initial_import(row) -> None:
    if batch_import_mode(row) == MODE_LEADMASTER_REFRESH:
        from crm_import_staging import BatchNotReusable

        raise BatchNotReusable(INITIAL_IMPORT_REFRESH_BATCH)


def upload_leadmaster_refresh(
    *,
    client_id: int,
    actor: NorthStarUser,
    filename: str,
    content: bytes,
    worksheet: str = "",
) -> dict[str, Any]:
    assert_not_production_db()
    if not _blank(filename):
        raise ValueError("Choose a CSV or XLSX file to upload.")
    result = upload_crm_import(
        client_id=client_id,
        actor=actor,
        filename=filename,
        content=content,
        worksheet=worksheet,
    )
    batch = result.batch
    if batch is None or not getattr(batch, "batch_id", 0):
        return {"kind": result.kind, "message": result.message, "batch": None,
                "needs_worksheet": bool(result.needs_worksheet),
                "visible_sheets": list(result.visible_sheets or []),
                "filename": result.filename, "file_type": result.file_type}
    with get_connection() as conn:
        ensure_leadmaster_refresh_schema(conn)
        policy = default_policy(client_id)
        conn.execute(
            """
            UPDATE crm_import_batches
            SET import_mode = ?, source_system = ?, refresh_policy_json = ?,
                planner_version = ?
            WHERE id = ? AND client_id = ?
            """,
            (
                MODE_LEADMASTER_REFRESH,
                SOURCE_LEADMASTER,
                json.dumps(policy.to_json()),
                "leadmaster-refresh-plan-v2",
                int(batch.batch_id),
                int(client_id),
            ),
        )
        conn.commit()
    view = refresh_batch_view(client_id, int(batch.batch_id))
    return {
        "kind": result.kind,
        "message": result.message,
        "batch": view,
        "needs_worksheet": False,
        "filename": result.filename,
        "file_type": result.file_type,
    }


def _load_batch(conn, client_id: int, batch_id: int):
    return conn.execute(
        "SELECT * FROM crm_import_batches WHERE id=? AND client_id=?",
        (int(batch_id), int(client_id)),
    ).fetchone()


def refresh_batch_view(client_id: int, batch_id: int) -> dict[str, Any]:
    from leadmaster_refresh_mapping import suggest_refresh_mapping
    from leadmaster_refresh_plan import PLANNER_VERSION

    batch = get_crm_import_batch(client_id, batch_id, include_sample=True)
    with get_connection() as conn:
        row = _load_batch(conn, client_id, batch_id)
        if row is None:
            raise LookupError("Import batch not found.")
        if batch_import_mode(row) != MODE_LEADMASTER_REFRESH:
            raise ValueError(WRONG_BATCH_MODE)
        policy_raw = "{}"
        if "refresh_policy_json" in row.keys():
            policy_raw = row["refresh_policy_json"] or "{}"
        policy = RefreshPolicyProfile.from_json(
            json.loads(policy_raw), client_id=client_id
        )
        headers = list(batch.headers or [])
        mapping = normalize_mapping(batch.mapping or {})
        suggested = suggest_refresh_mapping(headers)
        return {
            "batch_id": int(batch.batch_id),
            "client_id": int(client_id),
            "import_mode": MODE_LEADMASTER_REFRESH,
            "source_system": SOURCE_LEADMASTER,
            "status": batch.status,
            "original_filename": batch.original_filename,
            "sha256": batch.sha256,
            "headers": headers,
            "mapping": mapping,
            "suggested_mapping": suggested,
            "policy": policy.to_json(),
            "planner_version": _blank(row["planner_version"]) or PLANNER_VERSION,
            "resolutions": load_refresh_resolutions(row),
            "total_rows": int(batch.total_rows or 0),
            "warnings": list(batch.warnings or []),
            "uploaded_by_name": batch.uploaded_by_name,
            "created_at": batch.created_at,
        }


def save_refresh_mapping(
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    mapping: dict[str, str],
) -> dict[str, Any]:
    assert_not_production_db()
    from datetime import datetime, timezone

    with get_connection() as conn:
        ensure_leadmaster_refresh_schema(conn)
        row = _load_batch(conn, client_id, batch_id)
        if row is None:
            raise LookupError("Import batch not found.")
        if batch_import_mode(row) != MODE_LEADMASTER_REFRESH:
            raise ValueError(WRONG_BATCH_MODE)
        headers = json.loads(row["headers_json"] or "[]")
        ok, errors, normalized = validate_refresh_mapping(mapping, headers)
        if not ok:
            raise ValueError(errors[0] if errors else "Invalid mapping.")
        now = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            UPDATE crm_import_batches
            SET mapping_json = ?, mapping_updated_at = ?, mapping_updated_by_user_id = ?
            WHERE id = ? AND client_id = ?
            """,
            (json.dumps(normalized), now, int(actor.id), int(batch_id), int(client_id)),
        )
        conn.commit()
    return refresh_batch_view(client_id, batch_id)


def save_refresh_policy(
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    policy_payload: dict[str, Any],
) -> dict[str, Any]:
    assert_not_production_db()
    profile = RefreshPolicyProfile.from_json(policy_payload, client_id=client_id)
    with get_connection() as conn:
        ensure_leadmaster_refresh_schema(conn)
        row = _load_batch(conn, client_id, batch_id)
        if row is None:
            raise LookupError("Import batch not found.")
        if batch_import_mode(row) != MODE_LEADMASTER_REFRESH:
            raise ValueError(WRONG_BATCH_MODE)
        conn.execute(
            """
            UPDATE crm_import_batches
            SET refresh_policy_json = ?, mapping_updated_by_user_id = ?
            WHERE id = ? AND client_id = ?
            """,
            (json.dumps(profile.to_json()), int(actor.id), int(batch_id), int(client_id)),
        )
        conn.commit()
    return refresh_batch_view(client_id, batch_id)


def load_incoming_rows(conn, *, client_id: int, batch_id: int) -> tuple[list[IncomingRow], dict[str, str], RefreshPolicyProfile, str, str]:
    row = _load_batch(conn, client_id, batch_id)
    if row is None:
        raise LookupError("Import batch not found.")
    if batch_import_mode(row) != MODE_LEADMASTER_REFRESH:
        raise ValueError(WRONG_BATCH_MODE)
    mapping = normalize_mapping(json.loads(row["mapping_json"] or "{}"))
    policy = RefreshPolicyProfile.from_json(
        json.loads(row["refresh_policy_json"] or "{}"), client_id=client_id
    )
    staged = conn.execute(
        """
        SELECT source_row_number, raw_json FROM crm_import_rows
        WHERE batch_id=? AND client_id=?
        ORDER BY source_row_number, id
        """,
        (int(batch_id), int(client_id)),
    ).fetchall()
    incoming: list[IncomingRow] = []
    for staged_row in staged:
        raw = json.loads(staged_row["raw_json"] or "{}")
        if not isinstance(raw, dict):
            raw = {}
        hist_text = mapped_value(raw, mapping, "history_note_text")
        hist_id = mapped_value(raw, mapping, "history_source_id")
        history = []
        if hist_text or hist_id:
            history.append(
                IncomingHistory(
                    source_note_id=hist_id,
                    event_at=mapped_value(raw, mapping, "history_event_at"),
                    author=mapped_value(raw, mapping, "history_author"),
                    event_type=mapped_value(raw, mapping, "history_event_type"),
                    note_text=hist_text,
                )
            )
        first = mapped_value(raw, mapping, "contact_first_name")
        last = mapped_value(raw, mapping, "contact_last_name")
        full = mapped_value(raw, mapping, "contact_full_name")
        if not first and not last and full:
            parts = full.split()
            first = parts[0] if parts else ""
            last = " ".join(parts[1:]) if len(parts) > 1 else ""
        incoming.append(
            IncomingRow(
                source_row=int(staged_row["source_row_number"] or 0),
                record_no=mapped_value(raw, mapping, "external_record_no"),
                company_name=mapped_value(raw, mapping, "company_name"),
                address=mapped_value(raw, mapping, "address"),
                address2=mapped_value(raw, mapping, "address2"),
                city=mapped_value(raw, mapping, "city"),
                state=mapped_value(raw, mapping, "state"),
                zip=mapped_value(raw, mapping, "zip"),
                country=mapped_value(raw, mapping, "country"),
                website=mapped_value(raw, mapping, "website"),
                company_phone=mapped_value(raw, mapping, "phone"),
                company_phone_ext=mapped_value(raw, mapping, "phone_extension"),
                first_name=first,
                last_name=last,
                title=mapped_value(raw, mapping, "contact_title"),
                email=mapped_value(raw, mapping, "contact_email"),
                contact_phone=mapped_value(raw, mapping, "contact_phone"),
                contact_phone_ext=mapped_value(raw, mapping, "contact_phone_extension"),
                alt_phone=mapped_value(raw, mapping, "contact_alt_phone"),
                alt_phone_ext=mapped_value(raw, mapping, "contact_alt_extension"),
                status=mapped_value(raw, mapping, "relationship_status"),
                notes=mapped_value(raw, mapping, "relationship_notes"),
                assigned_rep=mapped_value(raw, mapping, "assigned_rep"),
                campaign=mapped_value(raw, mapping, "campaign"),
                history=history,
            )
        )
    return incoming, mapping, policy, _blank(row["sha256"]), _blank(row["original_filename"])


def load_refresh_resolutions(row) -> list:
    from leadmaster_refresh_resolutions import normalize_resolutions

    raw = "[]"
    if row is not None and "refresh_resolutions_json" in row.keys():
        raw = row["refresh_resolutions_json"] or "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = []
    return normalize_resolutions(parsed)


def save_refresh_resolutions(
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    resolutions: list,
) -> dict[str, Any]:
    assert_not_production_db()
    from leadmaster_refresh_resolutions import normalize_resolutions

    normalized = normalize_resolutions(resolutions)
    with get_connection() as conn:
        ensure_leadmaster_refresh_schema(conn)
        row = _load_batch(conn, client_id, batch_id)
        if row is None:
            raise LookupError("Import batch not found.")
        if batch_import_mode(row) != MODE_LEADMASTER_REFRESH:
            raise ValueError(WRONG_BATCH_MODE)
        conn.execute(
            """
            UPDATE crm_import_batches
            SET refresh_resolutions_json = ?, mapping_updated_by_user_id = ?
            WHERE id = ? AND client_id = ?
            """,
            (json.dumps(normalized), int(actor.id), int(batch_id), int(client_id)),
        )
        conn.commit()
    return refresh_batch_view(client_id, batch_id)


def plan_refresh_batch(
    conn, *, client_id: int, batch_id: int, persist_summary: bool = False
) -> dict[str, Any]:
    from leadmaster_refresh_plan import (
        fingerprint_refresh_plan,
        plan_leadmaster_refresh,
        refresh_policy_from_profile,
    )

    incoming, mapping, profile, sha, filename = load_incoming_rows(
        conn, client_id=client_id, batch_id=batch_id
    )
    row = _load_batch(conn, client_id, batch_id)
    resolutions = load_refresh_resolutions(row)
    pol = refresh_policy_from_profile(profile)
    pol.mapping = mapping
    plan = plan_leadmaster_refresh(
        conn,
        client_id=client_id,
        rows=incoming,
        policy=pol,
        source_sha256=sha,
        source_filename=filename,
    )
    plan["resolutions"] = resolutions
    plan["plan_fingerprint"] = fingerprint_refresh_plan(plan)
    plan["import_mode"] = MODE_LEADMASTER_REFRESH
    plan["source_system"] = SOURCE_LEADMASTER
    plan["mapping"] = mapping
    plan["policy_visible"] = profile.fingerprint_slice()
    review_rows = [r for r in plan.get("rows") or [] if r.get("review") or r.get("blocking")]
    plan["review_rows"] = review_rows
    if persist_summary:
        conn.execute(
            """
            UPDATE crm_import_batches
            SET refresh_plan_json = ?, planner_version = ?
            WHERE id = ? AND client_id = ?
            """,
            (
                json.dumps(
                    {
                        "plan_fingerprint": plan.get("plan_fingerprint"),
                        "counts": plan.get("counts"),
                    }
                ),
                plan.get("schema") or "",
                int(batch_id),
                int(client_id),
            ),
        )
    return plan
