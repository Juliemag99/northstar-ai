"""Research import batch staging. Isolated DB tables, or in-memory on live.

Never writes companies, CCRs, notes, or provenance. Live schema is not created.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from db import get_connection
from models import NorthStarUser
from research_import_mapping import (
    apply_mapping,
    blank,
    compare_header_sets,
    custom_attribute_maps,
    default_research_method,
    describe_source_columns,
    fields_from_mapping,
    header_signature,
    ignored_headers,
    mapping_safety_class,
    normalize_mapping,
    source_type_record,
    suggest_mapping,
    validate_mapping,
)
from research_import_parse import parse_upload
from research_import_plan import (
    CLIENT_MISSING,
    CLIENT_REQUIRED,
    STATUS_PREVIEWED,
    detect_batch_caveat,
    plan_fingerprint,
    utc_now,
)
from research_import_schema import (
    LiveResearchSchemaForbidden,
    ensure_research_import_schema,
    is_production_db,
)
from research_import_policy import (
    RES_CREATE_CONTACT,
    RES_SKIP_CONTACT,
    RES_USE_CONTACT,
    source_is_authoritative,
    source_trust,
)

_MEMORY_BATCHES: dict[tuple[int, int], dict[str, Any]] = {}
_MEMORY_SEQ = 0


class BatchNotReusable(ValueError):
    pass


def _actor_name(actor: NorthStarUser | None) -> str:
    if actor is None:
        return ""
    return blank(getattr(actor, "full_name", "")) or blank(getattr(actor, "email", ""))


def _require_client(conn, client_id: int) -> None:
    if int(client_id) <= 0:
        raise ValueError(CLIENT_REQUIRED)
    row = conn.execute("SELECT id FROM clients WHERE id=?", (int(client_id),)).fetchone()
    if row is None:
        raise ValueError(CLIENT_MISSING)


def _use_memory(conn) -> bool:
    return is_production_db(conn)


def _next_memory_id() -> int:
    global _MEMORY_SEQ
    _MEMORY_SEQ += 1
    return _MEMORY_SEQ


def reset_memory_store() -> None:
    _MEMORY_BATCHES.clear()


def _store_memory(batch: dict[str, Any]) -> None:
    _MEMORY_BATCHES[(int(batch["client_id"]), int(batch["id"]))] = batch


def _get_memory(client_id: int, batch_id: int, *, allow_confirmed: bool = False) -> dict[str, Any]:
    batch = _MEMORY_BATCHES.get((int(client_id), int(batch_id)))
    if batch is None:
        raise LookupError("Research import batch was not found.")
    _assert_batch_readable(batch, allow_confirmed=allow_confirmed)
    return batch


def list_attribute_definitions(conn) -> list[dict[str, str]]:
    if is_production_db(conn):
        return []
    try:
        ensure_research_import_schema(conn)
        rows = conn.execute(
            """
            SELECT attribute_key, display_label, value_type, scope_default
            FROM research_attribute_definitions
            ORDER BY display_label, attribute_key
            """
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "key": blank(row["attribute_key"]),
            "label": blank(row["display_label"]) or blank(row["attribute_key"]),
            "value_type": blank(row["value_type"]) or "TEXT",
            "scope": blank(row["scope_default"]) or "client",
        }
        for row in rows
    ]


def fingerprint_options_from_batch(batch: dict[str, Any]) -> dict[str, Any]:
    mapping = batch.get("mapping") or {}
    headers = batch.get("headers") or []
    return {
        **(batch.get("options") or {}),
        "source_type": batch.get("source_type") or "",
        "source_label": batch.get("source_label") or "",
        "source_supplied_by": batch.get("source_supplied_by") or "",
        "mapping_template_id": batch.get("mapping_template_id") or None,
        "custom_attributes": custom_attribute_maps(mapping),
        "ignored_headers": ignored_headers(mapping, headers),
        "contact_resolutions": [
            {"row_id": k, **v}
            for k, v in sorted((batch.get("contact_resolutions") or {}).items(), key=lambda item: item[0])
        ],
    }


def upload_research_import(
    *,
    client_id: int,
    actor: NorthStarUser | None,
    filename: str,
    content: bytes,
    worksheet: str = "",
    research_method: str = "",
    research_date: str = "",
    batch_name: str = "",
    source_type: str = "",
    source_label: str = "",
    source_supplied_by: str = "",
) -> dict[str, Any]:
    parsed = parse_upload(filename, content, worksheet=worksheet)
    if parsed.get("kind") == "needs_worksheet":
        return parsed
    with get_connection() as conn:
        _require_client(conn, client_id)
        suggested = suggest_mapping(parsed["headers"])
        source = source_type_record(source_type or "CHATGPT_DEEP_RESEARCH", source_label)
        method = blank(research_method) or default_research_method(source["code"])
        now = utc_now()
        template_hint = suggest_mapping_template(
            conn,
            client_id=int(client_id),
            source_type=source["code"],
            headers=parsed["headers"],
        )
        batch = {
            "id": 0,
            "client_id": int(client_id),
            "batch_name": blank(batch_name) or blank(filename),
            "original_filename": blank(filename),
            "file_type": parsed["file_type"],
            "worksheet_name": parsed["worksheet_name"],
            "file_size_bytes": parsed.get("file_size_bytes") or 0,
            "sha256": parsed["sha256"],
            "research_method": method,
            "research_date": blank(research_date),
            "source_type": source["code"],
            "source_label": source["source_label"] or source["label"],
            "source_supplied_by": blank(source_supplied_by),
            "is_ai_generated": 1 if source["code"] in {"CHATGPT_DEEP_RESEARCH", "AI_RESEARCH_OTHER"} else 0,
            "mapping_template_id": None,
            "mapping_template_name": "",
            "received_at": now,
            "prior_research_file": "",
            "original_research_file": blank(filename),
            "batch_caveat": "",
            "status": STATUS_PREVIEWED,
            "error_message": "",
            "headers": parsed["headers"],
            "mapping": suggested,
            "options": {},
            "warnings": parsed.get("warnings") or [],
            "source_row_count": len(parsed["rows"]),
            "uploaded_by_user_id": getattr(actor, "id", None),
            "uploaded_by_name": _actor_name(actor),
            "created_at": now,
            "updated_at": now,
            "rows": parsed["rows"],
            "match_resolutions": {},
            "master_resolutions": {},
            "contact_resolutions": {},
            "suggested_mapping": suggested,
            "mapping_template_suggestion": template_hint,
        }
        if _use_memory(conn):
            batch["id"] = _next_memory_id()
            batch["storage"] = "memory"
            _store_memory(batch)
        else:
            ensure_research_import_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO research_import_batches (
                    client_id, batch_name, original_filename, file_type, worksheet_name,
                    file_size_bytes, sha256, research_method, research_date, status,
                    headers_json, mapping_json, warnings_json, source_row_count,
                    uploaded_by_user_id, uploaded_by_name, created_at, updated_at,
                    source_type, source_label, source_supplied_by, is_ai_generated,
                    received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(client_id),
                    batch["batch_name"],
                    batch["original_filename"],
                    batch["file_type"],
                    batch["worksheet_name"],
                    batch["file_size_bytes"],
                    batch["sha256"],
                    batch["research_method"],
                    batch["research_date"],
                    STATUS_PREVIEWED,
                    json.dumps(batch["headers"]),
                    json.dumps(batch["mapping"]),
                    json.dumps(batch["warnings"]),
                    batch["source_row_count"],
                    batch["uploaded_by_user_id"],
                    batch["uploaded_by_name"],
                    now,
                    now,
                    batch["source_type"],
                    batch["source_label"],
                    batch["source_supplied_by"],
                    batch["is_ai_generated"],
                    now,
                ),
            )
            batch_id = int(cur.lastrowid)
            for i, values in enumerate(parsed["rows"], start=2):
                conn.execute(
                    """
                    INSERT INTO research_import_rows (
                        batch_id, client_id, source_row_number, raw_json,
                        warnings_json, errors_json, is_blank, has_blocking_error
                    ) VALUES (?, ?, ?, ?, '[]', '[]', 0, 0)
                    """,
                    (batch_id, int(client_id), i, json.dumps(values, ensure_ascii=False)),
                )
            conn.commit()
            batch["id"] = batch_id
            batch["storage"] = "db"
        return {
            "kind": "ok",
            "needs_worksheet": False,
            "visible_sheets": parsed.get("visible_sheets") or [],
            "filename": batch["original_filename"],
            "file_type": batch["file_type"],
            "message": "",
            "batch": public_batch(conn, batch),
        }


PREVIEWABLE_STATUSES = {
    "",
    "uploaded",
    "mapped",
    STATUS_PREVIEWED,
    "blocked",
    "ready",
}


def _assert_batch_readable(batch: dict[str, Any], *, allow_confirmed: bool) -> None:
    status = blank(batch.get("status"))
    if status == "confirmed":
        if not allow_confirmed:
            raise BatchNotReusable("This research import batch is confirmed and cannot be edited.")
        return
    if status not in PREVIEWABLE_STATUSES:
        raise BatchNotReusable("This research import batch can no longer be previewed.")


def _assert_batch_mutable(batch: dict[str, Any]) -> None:
    if blank(batch.get("status")) == "confirmed":
        raise BatchNotReusable("This research import batch is confirmed and cannot be edited.")


def _load_db_batch(conn, client_id: int, batch_id: int, *, allow_confirmed: bool = False) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM research_import_batches WHERE id=? AND client_id=?",
        (int(batch_id), int(client_id)),
    ).fetchone()
    if row is None:
        raise LookupError("Research import batch was not found.")
    if blank(row["status"]) == "confirmed" and not allow_confirmed:
        raise BatchNotReusable("This research import batch is confirmed and cannot be edited.")
    if blank(row["status"]) not in PREVIEWABLE_STATUSES | ({"confirmed"} if allow_confirmed else set()):
        raise BatchNotReusable("This research import batch can no longer be previewed.")
    raw_rows = conn.execute(
        """
        SELECT id, source_row_number, raw_json FROM research_import_rows
        WHERE batch_id=? AND client_id=? ORDER BY source_row_number, id
        """,
        (int(batch_id), int(client_id)),
    ).fetchall()
    match_rows = conn.execute(
        """
        SELECT staged_row_id, resolution_type, company_id
        FROM research_import_match_resolutions
        WHERE batch_id=? AND client_id=?
        """,
        (int(batch_id), int(client_id)),
    ).fetchall()
    master_rows = conn.execute(
        """
        SELECT staged_row_id, field, resolution_type
        FROM research_import_master_resolutions
        WHERE batch_id=? AND client_id=?
        """,
        (int(batch_id), int(client_id)),
    ).fetchall()
    contact_rows = conn.execute(
        """
        SELECT staged_row_id, resolution_type, contact_id
        FROM research_import_contact_resolutions
        WHERE batch_id=? AND client_id=?
        """,
        (int(batch_id), int(client_id)),
    ).fetchall()
    return {
        "id": int(row["id"]),
        "client_id": int(row["client_id"]),
        "batch_name": blank(row["batch_name"]),
        "original_filename": blank(row["original_filename"]),
        "file_type": blank(row["file_type"]),
        "worksheet_name": blank(row["worksheet_name"]),
        "file_size_bytes": int(row["file_size_bytes"] or 0),
        "sha256": blank(row["sha256"]),
        "research_method": blank(row["research_method"]),
        "research_date": blank(row["research_date"]),
        "source_type": blank(row["source_type"]) if "source_type" in row.keys() else "",
        "source_label": blank(row["source_label"]) if "source_label" in row.keys() else "",
        "source_supplied_by": blank(row["source_supplied_by"]) if "source_supplied_by" in row.keys() else "",
        "is_ai_generated": int(row["is_ai_generated"] or 0) if "is_ai_generated" in row.keys() else 0,
        "received_at": blank(row["received_at"]) if "received_at" in row.keys() else blank(row["created_at"]),
        "mapping_template_id": int(row["mapping_template_id"]) if "mapping_template_id" in row.keys() and row["mapping_template_id"] else None,
        "mapping_template_name": blank(row["mapping_template_name"]) if "mapping_template_name" in row.keys() else "",
        "prior_research_file": blank(row["prior_research_file"]),
        "original_research_file": blank(row["original_research_file"]),
        "batch_caveat": blank(row["batch_caveat"]),
        "status": blank(row["status"]),
        "headers": json.loads(row["headers_json"] or "[]"),
        "mapping": json.loads(row["mapping_json"] or "{}"),
        "options": json.loads(row["options_json"] or "{}"),
        "warnings": json.loads(row["warnings_json"] or "[]"),
        "source_row_count": int(row["source_row_count"] or 0),
        "uploaded_by_name": blank(row["uploaded_by_name"]),
        "created_at": blank(row["created_at"]),
        "updated_at": blank(row["updated_at"]),
        "rows": [json.loads(r["raw_json"] or "{}") for r in raw_rows],
        "row_ids": [int(r["id"]) for r in raw_rows],
        "match_resolutions": {
            int(r["staged_row_id"]): {
                "resolution_type": blank(r["resolution_type"]),
                "company_id": int(r["company_id"]) if r["company_id"] is not None else None,
            }
            for r in match_rows
        },
        "master_resolutions": {
            (int(r["staged_row_id"]), blank(r["field"])): blank(r["resolution_type"])
            for r in master_rows
        },
        "contact_resolutions": {
            int(r["staged_row_id"]): {
                "resolution_type": blank(r["resolution_type"]),
                "contact_id": int(r["contact_id"]) if r["contact_id"] is not None else None,
            }
            for r in contact_rows
        },
        "suggested_mapping": suggest_mapping(json.loads(row["headers_json"] or "[]")),
        "storage": "db",
        "confirm_result": json.loads(row["confirm_result_json"] or "{}")
        if "confirm_result_json" in row.keys() and row["confirm_result_json"]
        else {},
        "confirmed_plan_fingerprint": blank(row["confirmed_plan_fingerprint"])
        if "confirmed_plan_fingerprint" in row.keys()
        else "",
    }


def load_batch(conn, client_id: int, batch_id: int, *, allow_confirmed: bool = False) -> dict[str, Any]:
    if _use_memory(conn):
        return _get_memory(client_id, batch_id, allow_confirmed=allow_confirmed)
    ensure_research_import_schema(conn)
    return _load_db_batch(conn, client_id, batch_id, allow_confirmed=allow_confirmed)


def public_batch(conn, batch: dict[str, Any]) -> dict[str, Any]:
    headers = batch.get("headers") or []
    mapping = batch.get("mapping") or {}
    return {
        "batch_id": int(batch["id"]),
        "client_id": int(batch["client_id"]),
        "batch_name": batch.get("batch_name", ""),
        "status": batch.get("status", STATUS_PREVIEWED),
        "original_filename": batch.get("original_filename", ""),
        "file_type": batch.get("file_type", ""),
        "worksheet_name": batch.get("worksheet_name", ""),
        "file_size_bytes": batch.get("file_size_bytes", 0),
        "sha256": batch.get("sha256", ""),
        "research_method": batch.get("research_method", ""),
        "research_date": batch.get("research_date", ""),
        "source_type": batch.get("source_type", ""),
        "source_label": batch.get("source_label", ""),
        "source_supplied_by": batch.get("source_supplied_by", ""),
        "is_ai_generated": int(batch.get("is_ai_generated") or 0),
        "received_at": batch.get("received_at") or batch.get("created_at") or "",
        "mapping_template_id": batch.get("mapping_template_id"),
        "mapping_template_name": batch.get("mapping_template_name", ""),
        "headers": headers,
        "warnings": batch.get("warnings", []),
        "source_row_count": batch.get("source_row_count", 0),
        "uploaded_by_name": batch.get("uploaded_by_name", ""),
        "created_at": batch.get("created_at", ""),
        "mapping": mapping,
        "suggested_mapping": batch.get("suggested_mapping") or suggest_mapping(headers),
        "column_map": describe_source_columns(headers, batch.get("rows") or [], mapping),
        "ignored_headers": ignored_headers(mapping, headers),
        "custom_attributes": custom_attribute_maps(mapping),
        "mapping_template_suggestion": batch.get("mapping_template_suggestion")
        or suggest_mapping_template(
            conn,
            client_id=int(batch["client_id"]),
            source_type=batch.get("source_type") or "",
            headers=headers,
        ),
        "sample_rows": (batch.get("rows") or [])[:8],
        "attribute_definitions": list_attribute_definitions(conn),
        "production_confirm_enabled": False,
        "source_trust": source_trust(batch.get("source_type") or ""),
        "source_type_is_authority": source_is_authoritative(batch.get("source_type") or ""),
        "storage": batch.get("storage", "db"),
        "product_name": "Research & Custom Prospect Import",
    }


def save_mapping(
    client_id: int,
    batch_id: int,
    *,
    actor: NorthStarUser | None,
    mapping: dict[str, Any],
    research_method: str = "",
    research_date: str = "",
    batch_name: str = "",
    source_type: str = "",
    source_label: str = "",
    source_supplied_by: str = "",
    mapping_template_id: int | None = None,
    save_as_template: str = "",
) -> dict[str, Any]:
    with get_connection() as conn:
        batch = load_batch(conn, client_id, batch_id)
        _assert_batch_mutable(batch)
        ok, errors = validate_mapping(mapping, batch["headers"])
        if not ok:
            raise ValueError(errors[0] if errors else "Invalid mapping.")
        batch["mapping"] = mapping if isinstance(mapping, dict) else {}
        if blank(research_method):
            batch["research_method"] = blank(research_method)
        if research_date != "":
            batch["research_date"] = blank(research_date)
        if blank(batch_name):
            batch["batch_name"] = blank(batch_name)
        if blank(source_type):
            rec = source_type_record(source_type, source_label or batch.get("source_label") or "")
            batch["source_type"] = rec["code"]
            batch["source_label"] = rec["source_label"] or rec["label"]
            batch["is_ai_generated"] = 1 if rec["code"] in {"CHATGPT_DEEP_RESEARCH", "AI_RESEARCH_OTHER"} else 0
        if source_supplied_by != "":
            batch["source_supplied_by"] = blank(source_supplied_by)
        if mapping_template_id:
            batch["mapping_template_id"] = int(mapping_template_id)
        if blank(save_as_template) and not _use_memory(conn):
            saved = save_mapping_template(
                conn,
                client_id=int(client_id),
                actor=actor,
                template_name=save_as_template,
                source_type=batch.get("source_type") or "",
                source_name=batch.get("source_label") or batch.get("batch_name") or "",
                headers=batch.get("headers") or [],
                mapping=batch["mapping"],
            )
            batch["mapping_template_id"] = saved["template_id"]
            batch["mapping_template_name"] = saved["template_name"]
        batch["updated_at"] = utc_now()
        if _use_memory(conn):
            _store_memory(batch)
        else:
            conn.execute(
                """
                UPDATE research_import_batches
                SET mapping_json=?, research_method=?, research_date=?, batch_name=?, updated_at=?,
                    source_type=?, source_label=?, source_supplied_by=?, is_ai_generated=?,
                    mapping_template_id=?, mapping_template_name=?
                WHERE id=? AND client_id=?
                """,
                (
                    json.dumps(batch["mapping"]),
                    batch["research_method"],
                    batch["research_date"],
                    batch["batch_name"],
                    batch["updated_at"],
                    batch.get("source_type") or "",
                    batch.get("source_label") or "",
                    batch.get("source_supplied_by") or "",
                    int(batch.get("is_ai_generated") or 0),
                    batch.get("mapping_template_id"),
                    batch.get("mapping_template_name") or "",
                    int(batch_id),
                    int(client_id),
                ),
            )
            conn.commit()
        return public_batch(conn, batch)


def save_match_resolution(
    client_id: int,
    batch_id: int,
    *,
    actor: NorthStarUser | None,
    staged_row_id: int,
    resolution_type: str,
    company_id: int | None = None,
) -> dict[str, Any]:
    res = blank(resolution_type).upper()
    with get_connection() as conn:
        batch = load_batch(conn, client_id, batch_id)
        _assert_batch_mutable(batch)
        payload = {"resolution_type": res, "company_id": int(company_id) if company_id else None}
        batch.setdefault("match_resolutions", {})[int(staged_row_id)] = payload
        if _use_memory(conn):
            _store_memory(batch)
        else:
            conn.execute(
                """
                INSERT INTO research_import_match_resolutions (
                    client_id, batch_id, staged_row_id, resolution_type, company_id,
                    updated_at, updated_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, batch_id, staged_row_id) DO UPDATE SET
                    resolution_type=excluded.resolution_type,
                    company_id=excluded.company_id,
                    updated_at=excluded.updated_at,
                    updated_by_user_id=excluded.updated_by_user_id
                """,
                (
                    int(client_id),
                    int(batch_id),
                    int(staged_row_id),
                    res,
                    payload["company_id"],
                    utc_now(),
                    getattr(actor, "id", None),
                ),
            )
            conn.commit()
        return payload


def save_master_resolution(
    client_id: int,
    batch_id: int,
    *,
    actor: NorthStarUser | None,
    staged_row_id: int,
    field: str,
    resolution_type: str,
) -> dict[str, Any]:
    res = blank(resolution_type).upper()
    with get_connection() as conn:
        batch = load_batch(conn, client_id, batch_id)
        _assert_batch_mutable(batch)
        batch.setdefault("master_resolutions", {})[(int(staged_row_id), blank(field))] = res
        if _use_memory(conn):
            _store_memory(batch)
        else:
            conn.execute(
                """
                INSERT INTO research_import_master_resolutions (
                    client_id, batch_id, staged_row_id, field, resolution_type,
                    updated_at, updated_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, batch_id, staged_row_id, field) DO UPDATE SET
                    resolution_type=excluded.resolution_type,
                    updated_at=excluded.updated_at,
                    updated_by_user_id=excluded.updated_by_user_id
                """,
                (
                    int(client_id),
                    int(batch_id),
                    int(staged_row_id),
                    blank(field),
                    res,
                    utc_now(),
                    getattr(actor, "id", None),
                ),
            )
            conn.commit()
        return {"staged_row_id": int(staged_row_id), "field": blank(field), "resolution_type": res}


def save_contact_resolution(
    client_id: int,
    batch_id: int,
    *,
    actor: NorthStarUser | None,
    staged_row_id: int,
    resolution_type: str,
    contact_id: int | None = None,
) -> dict[str, Any]:
    res = blank(resolution_type).upper()
    if res not in {RES_USE_CONTACT, RES_CREATE_CONTACT, RES_SKIP_CONTACT}:
        raise ValueError(f"Unsupported contact resolution: {res}")
    with get_connection() as conn:
        batch = load_batch(conn, client_id, batch_id)
        _assert_batch_mutable(batch)
        payload = {
            "resolution_type": res,
            "contact_id": int(contact_id) if contact_id else None,
        }
        batch.setdefault("contact_resolutions", {})[int(staged_row_id)] = payload
        if _use_memory(conn):
            _store_memory(batch)
        else:
            conn.execute(
                """
                INSERT INTO research_import_contact_resolutions (
                    client_id, batch_id, staged_row_id, resolution_type, contact_id,
                    updated_at, updated_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, batch_id, staged_row_id) DO UPDATE SET
                    resolution_type=excluded.resolution_type,
                    contact_id=excluded.contact_id,
                    updated_at=excluded.updated_at,
                    updated_by_user_id=excluded.updated_by_user_id
                """,
                (
                    int(client_id),
                    int(batch_id),
                    int(staged_row_id),
                    res,
                    payload["contact_id"],
                    utc_now(),
                    getattr(actor, "id", None),
                ),
            )
            conn.commit()
        return payload


def suggest_mapping_template(conn, *, client_id: int, source_type: str, headers: list[str]) -> dict[str, Any]:
    if is_production_db(conn):
        return {}
    ensure_research_import_schema(conn)
    sig = header_signature(headers)
    row = conn.execute(
        """
        SELECT * FROM research_import_mapping_templates
        WHERE header_signature=? AND (source_type=? OR source_type='')
          AND (client_id IS NULL OR client_id=?)
        ORDER BY CASE WHEN client_id=? THEN 0 WHEN client_id IS NULL THEN 1 ELSE 2 END, id DESC
        LIMIT 1
        """,
        (sig, blank(source_type), int(client_id), int(client_id)),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM research_import_mapping_templates
            WHERE source_type=? AND safety_class='safe_reusable'
              AND (client_id IS NULL OR client_id=?)
            ORDER BY id DESC LIMIT 1
            """,
            (blank(source_type), int(client_id)),
        ).fetchone()
        if row is None:
            return {}
    saved_headers = json.loads(row["headers_json"] or "[]")
    comparison = compare_header_sets(saved_headers, headers)
    safety = blank(row["safety_class"]) or mapping_safety_class(fields_from_mapping(json.loads(row["mapping_json"] or "{}")))
    return {
        "template_id": int(row["id"]),
        "template_name": blank(row["template_name"]),
        "source_type": blank(row["source_type"]),
        "source_name": blank(row["source_name"]),
        "safety_class": safety,
        "mapping": json.loads(row["mapping_json"] or "{}"),
        "status": "suggested",
        "applied": False,
        "review_required": bool(comparison["review_required"] or safety != "safe_reusable"),
        "header_compatibility": comparison,
        "client_specific": row["client_id"] is not None,
        "same_client": row["client_id"] is not None and int(row["client_id"]) == int(client_id),
    }


def save_mapping_template(
    conn,
    *,
    client_id: int,
    actor: NorthStarUser | None,
    template_name: str,
    source_type: str,
    source_name: str,
    headers: list[str],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    ensure_research_import_schema(conn)
    now = utc_now()
    safety = mapping_safety_class(fields_from_mapping(mapping))
    if safety in {"workflow_sensitive", "notes_sensitive"}:
        stored_client = int(client_id)
    else:
        stored_client = None
    cur = conn.execute(
        """
        INSERT INTO research_import_mapping_templates (
            template_name, source_type, source_name, client_id, header_signature,
            headers_json, mapping_json, safety_class, created_at, updated_at, updated_by_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            blank(template_name),
            blank(source_type),
            blank(source_name),
            stored_client,
            header_signature(headers),
            json.dumps(headers),
            json.dumps(mapping),
            safety,
            now,
            now,
            getattr(actor, "id", None),
        ),
    )
    conn.commit()
    return {
        "template_id": int(cur.lastrowid),
        "template_name": blank(template_name),
        "safety_class": safety,
        "client_specific": stored_client is not None,
    }


def apply_suggested_template(batch: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    hint = batch.get("mapping_template_suggestion") or {}
    if not apply or not hint or hint.get("review_required"):
        return hint
    batch["mapping"] = hint.get("mapping") or batch.get("mapping")
    batch["mapping_template_id"] = hint.get("template_id")
    batch["mapping_template_name"] = hint.get("template_name") or ""
    hint = {**hint, "applied": True, "status": "applied"}
    batch["mapping_template_suggestion"] = hint
    return hint


def mapped_rows_for_batch(batch: dict[str, Any]) -> tuple[list[dict[str, str]], list[int]]:
    mapping = batch.get("mapping") or {}
    rows = []
    ids = batch.get("row_ids")
    for values in batch.get("rows") or []:
        rows.append(apply_mapping(values, mapping))
    if not ids:
        ids = list(range(1, len(rows) + 1))
        batch["row_ids"] = ids
    caveat = detect_batch_caveat(rows)
    batch["batch_caveat"] = caveat
    if rows:
        prior = blank(rows[0].get("prior_research_file"))
        if prior and all(blank(r.get("prior_research_file")) == prior for r in rows):
            batch["prior_research_file"] = prior
        method = blank(rows[0].get("research_method"))
        if method and not blank(batch.get("research_method")):
            batch["research_method"] = method
        date = blank(rows[0].get("research_date"))
        if date and not blank(batch.get("research_date")):
            batch["research_date"] = date
        name = blank(rows[0].get("research_batch_name"))
        if name:
            batch["batch_name"] = name
    return rows, ids
