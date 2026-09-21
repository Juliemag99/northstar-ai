"""Dry-run and confirm orchestration for Research Prospect Import."""
from __future__ import annotations

from typing import Any

from db import get_connection
from models import NorthStarUser
from research_import_mapping import ignored_headers, validate_mapping
from research_import_plan import (
    ISOLATED_CONFIRM_DISABLED,
    PRODUCTION_CONFIRM_DISABLED,
    PLANNER_VERSION,
    aggregate_counts,
    confirm_enabled,
    forecast_confirm,
    is_production_db,
    is_production_path,
    plan_fingerprint,
    plan_research_rows,
    planned_contract_slice,
    row_to_dict,
    utc_now,
)
from research_import_staging import (
    fingerprint_options_from_batch,
    load_batch,
    mapped_rows_for_batch,
    public_batch,
)


def dry_run_research_import(
    client_id: int,
    batch_id: int,
    *,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    with get_connection() as conn:
        batch = load_batch(conn, client_id, batch_id)
        ok, errors = validate_mapping(batch.get("mapping") or {}, batch.get("headers") or [])
        if not ok:
            raise ValueError(errors[0] if errors else "Invalid mapping.")
        mapped, row_ids = mapped_rows_for_batch(batch)
        planned = plan_research_rows(
            conn,
            client_id=int(client_id),
            mapped_rows=mapped,
            row_ids=row_ids,
            match_resolutions=batch.get("match_resolutions") or {},
            master_resolutions=batch.get("master_resolutions") or {},
            contact_resolutions=batch.get("contact_resolutions") or {},
            mapping=batch.get("mapping") or {},
            raw_rows=batch.get("rows") or [],
        )
        caveat = batch.get("batch_caveat") or ""
        options = fingerprint_options_from_batch(batch)
        options["planned_contract"] = planned_contract_slice(planned)
        fingerprint = plan_fingerprint(
            client_id=int(client_id),
            sha256=batch.get("sha256") or "",
            mapping=batch.get("mapping") or {},
            research_method=batch.get("research_method") or "",
            research_date=batch.get("research_date") or "",
            mapped_rows=mapped,
            match_resolutions=batch.get("match_resolutions") or {},
            master_resolutions=batch.get("master_resolutions") or {},
            contact_resolutions=batch.get("contact_resolutions") or {},
            options=options,
        )
        counts = aggregate_counts(planned, caveat)
        blocking = counts["blocking_rows"] > 0
        if str(batch.get("status") or "") != "confirmed":
            try:
                conn.execute(
                    "UPDATE research_import_batches SET status=?, updated_at=? WHERE id=? AND client_id=?",
                    ("blocked" if blocking else "ready", utc_now(), int(batch_id), int(client_id)),
                )
            except Exception:
                pass
        page = planned[max(offset, 0) : max(offset, 0) + max(limit, 1)]
        return {
            "batch": public_batch(conn, batch),
            "planner_version": PLANNER_VERSION,
            "plan_fingerprint": fingerprint,
            "counts": counts,
            "forecast": forecast_confirm(planned),
            "batch_caveat": caveat,
            "prior_research_file": batch.get("prior_research_file") or "",
            "ignored_headers": ignored_headers(batch.get("mapping") or {}, batch.get("headers") or []),
            "column_map": public_batch(conn, batch).get("column_map") or [],
            "confirm_allowed": False,
            "production_confirm_enabled": False,
            "workflow_fields_will_write": 0,
            "blocking": blocking,
            "ready": (not blocking) and not (is_production_db(conn) or is_production_path()),
            "blocking_reasons": [
                reason
                for row in planned
                if row.blocking
                for reason in row.blocking_reasons
            ][:20],
            "default_status": next((row.planned_status for row in planned if row.planned_status), "New"),
            "offset": max(offset, 0),
            "limit": max(limit, 1),
            "total_rows": len(planned),
            "rows": [row_to_dict(row) for row in page],
            "contract": {
                "workflow_writes": 0,
                "production_confirm": False,
                "source_type_is_authority": False,
                "notes_require_explicit_mapping": True,
                "same_batch_research": "one_per_client_company_batch",
            },
        }


def confirm_research_import(
    *,
    client_id: int,
    batch_id: int,
    plan_fingerprint: str,
    actor: NorthStarUser | None,
) -> dict[str, Any]:
    with get_connection() as conn:
        if is_production_db(conn) or is_production_path():
            raise PermissionError(PRODUCTION_CONFIRM_DISABLED)
        if not confirm_enabled(conn):
            raise PermissionError(ISOLATED_CONFIRM_DISABLED)
        from research_import_confirm import apply_research_import_confirm

        return apply_research_import_confirm(
            conn,
            client_id=int(client_id),
            batch_id=int(batch_id),
            expected_fingerprint=plan_fingerprint,
            actor=actor,
        )
