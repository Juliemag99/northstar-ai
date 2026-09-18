"""Project structured engagement quote evidence into company-level Quote milestones.

client_sales_events remain the authoritative detailed event store. This module only
creates or reuses one revenue_milestones row per (client_id, company_id) with
milestone_type='Quote' when reliable structured quote evidence exists.

Does not infer Purchase Orders from Won/PO narrative.
Does not change client_company_relationships.status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db import get_connection
from milestones_data import (
    _blank,
    _has_structured_quoted_metadata,
    _is_blankish_quote_value,
    _table_exists,
)

QUOTE_MILESTONE_SOURCE = "engagement_import"
QUOTE_MILESTONE_TYPE = "Quote"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sales_event_has_quote_evidence(row: Any) -> bool:
    """Reliable structured quote evidence only (not free-text Won/PO narrative)."""
    event_type = _blank(row["event_type"] if hasattr(row, "keys") else row.get("event_type"))
    amount = row["quoted_amount"] if hasattr(row, "keys") else row.get("quoted_amount")
    try:
        has_positive_amount = amount is not None and float(amount) > 0
    except (TypeError, ValueError):
        has_positive_amount = False
    src_q = row["source_quoted_value"] if hasattr(row, "keys") else row.get("source_quoted_value")
    notes = row["sales_notes"] if hasattr(row, "keys") else row.get("sales_notes")
    return (
        event_type == "RFQ"
        or has_positive_amount
        or not _is_blankish_quote_value(src_q)
        or _has_structured_quoted_metadata(notes)
    )


def _evidence_reasons(row: Any) -> list[str]:
    reasons: list[str] = []
    event_type = _blank(row["event_type"] if hasattr(row, "keys") else row.get("event_type"))
    if event_type == "RFQ":
        reasons.append("RFQ")
    amount = row["quoted_amount"] if hasattr(row, "keys") else row.get("quoted_amount")
    try:
        if amount is not None and float(amount) > 0:
            reasons.append(f"quoted_amount={float(amount):g}")
    except (TypeError, ValueError):
        pass
    src_q = row["source_quoted_value"] if hasattr(row, "keys") else row.get("source_quoted_value")
    if not _is_blankish_quote_value(src_q):
        reasons.append(f"source_quoted_value={_blank(src_q)[:40]}")
    notes = row["sales_notes"] if hasattr(row, "keys") else row.get("sales_notes")
    if _has_structured_quoted_metadata(notes):
        reasons.append("Quoted: metadata")
    return reasons


def _batch_meta(conn, client_id: int, batch_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, client_id, document_id, filename, status
        FROM client_engagement_import_batches
        WHERE id = ? AND client_id = ?
        """,
        (batch_id, client_id),
    ).fetchone()
    if row is None:
        raise LookupError("Import batch not found for this client.")
    meta = dict(row)
    sha = ""
    doc_id = meta.get("document_id")
    if doc_id is not None and _table_exists(conn, "client_documents"):
        cols = {
            str(r["name"] if hasattr(r, "keys") else r[1])
            for r in conn.execute("PRAGMA table_info(client_documents)").fetchall()
        }
        sha_col = "sha256" if "sha256" in cols else ("content_sha256" if "content_sha256" in cols else "")
        if sha_col:
            drow = conn.execute(
                f"SELECT {sha_col} AS sha FROM client_documents WHERE id = ?",
                (int(doc_id),),
            ).fetchone()
            if drow is not None:
                sha = _blank(drow["sha"])
    meta["sha256"] = sha
    return meta


def _load_batch_sales_events(conn, client_id: int, batch_id: int) -> list[Any]:
    if not _table_exists(conn, "client_sales_events"):
        return []
    return list(
        conn.execute(
            """
            SELECT id, client_id, company_id, relationship_id, contact_id,
                   event_type, event_date, quoted_amount, source_quoted_value,
                   sales_notes, source_document_id, source_batch_id,
                   source_row_fingerprint, company_name
            FROM client_sales_events
            WHERE client_id = ? AND source_batch_id = ?
              AND company_id IS NOT NULL
            ORDER BY company_id, id
            """,
            (client_id, batch_id),
        ).fetchall()
    )


def _existing_quote_milestones(conn, client_id: int, company_ids: list[int]) -> dict[int, list[dict]]:
    if not company_ids or not _table_exists(conn, "revenue_milestones"):
        return {}
    placeholders = ",".join("?" * len(company_ids))
    rows = conn.execute(
        f"""
        SELECT milestone_id, company_id, relationship_id, external_record_no,
               milestone_date, amount, reference_number, source, notes
        FROM revenue_milestones
        WHERE client_id = ?
          AND milestone_type = ?
          AND company_id IN ({placeholders})
        ORDER BY milestone_id
        """,
        [client_id, QUOTE_MILESTONE_TYPE, *company_ids],
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(int(r["company_id"]), []).append(dict(r))
    return out


def _company_record_no(conn, company_id: int) -> str:
    row = conn.execute(
        "SELECT external_record_no FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    return _blank(row["external_record_no"]) if row else ""


def _relationship_id(conn, client_id: int, company_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (client_id, company_id),
    ).fetchone()
    return int(row["id"]) if row else None


def _build_notes(batch_id: int, document_id: object, events: list[Any]) -> str:
    reason_bits: list[str] = []
    event_ids: list[int] = []
    for ev in events:
        event_ids.append(int(ev["id"]))
        reason_bits.extend(_evidence_reasons(ev))
    # De-dupe reason labels while keeping order
    seen: set[str] = set()
    unique_reasons: list[str] = []
    for r in reason_bits:
        key = r.split("=", 1)[0]
        if key in seen and key in {"RFQ", "Quoted: metadata"}:
            continue
        if r in seen:
            continue
        seen.add(r if "=" not in r else key)
        unique_reasons.append(r)
    ids_preview = ", ".join(str(i) for i in event_ids[:12])
    if len(event_ids) > 12:
        ids_preview += f", …(+{len(event_ids) - 12})"
    return (
        f"Quote milestone projected from engagement import batch {batch_id}"
        f" (document_id={document_id}). "
        f"Supporting structured events ({len(event_ids)}): {ids_preview}. "
        f"Evidence: {'; '.join(unique_reasons[:8]) or 'structured quote'}. "
        "Individual quote amounts remain on client_sales_events (not totaled)."
    )


def _pick_milestone_date(events: list[Any]) -> str:
    dates = sorted(
        {_blank(ev["event_date"]) for ev in events if _blank(ev["event_date"])}
    )
    return dates[0] if dates else _now_iso()[:10]


def _parse_moneyish(value: object | None) -> float | None:
    text = _blank(value)
    if _is_blankish_quote_value(text):
        return None
    cleaned = (
        text.replace("$", "")
        .replace(",", "")
        .replace("USD", "")
        .replace("usd", "")
        .strip()
    )
    # Take leading numeric token
    token = ""
    for ch in cleaned:
        if ch.isdigit() or ch in {".", "-"}:
            token += ch
        elif token:
            break
    if not token or token in {".", "-", "-."}:
        return None
    try:
        amount = float(token)
    except ValueError:
        return None
    return amount if amount > 0 else None


def _pick_amount(events: list[Any]) -> float | None:
    """Single positive amount only; never sum multiple quote amounts."""
    amounts: list[float] = []
    for ev in events:
        parsed = _parse_moneyish(ev["quoted_amount"])
        if parsed is not None:
            amounts.append(parsed)
        parsed = _parse_moneyish(ev["source_quoted_value"])
        if parsed is not None:
            amounts.append(parsed)
        notes = _blank(ev["sales_notes"])
        for line in notes.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("quoted:"):
                parsed = _parse_moneyish(stripped.split(":", 1)[1] if ":" in stripped else "")
                if parsed is not None:
                    amounts.append(parsed)
    uniq = sorted({round(a, 4) for a in amounts})
    if len(uniq) == 1:
        return uniq[0]
    return None


def _reference_number(batch: dict[str, Any]) -> str:
    doc = batch.get("document_id")
    sha = _blank(batch.get("sha256"))[:16]
    parts = [f"eng_batch:{int(batch['id'])}"]
    if doc is not None:
        parts.append(f"doc:{doc}")
    if sha:
        parts.append(f"sha:{sha}")
    return "|".join(parts)


def plan_quote_milestone_projection(
    conn,
    client_id: int,
    batch_id: int,
) -> dict[str, Any]:
    """Build a preview/apply plan. Does not write."""
    batch = _batch_meta(conn, client_id, batch_id)
    events = _load_batch_sales_events(conn, client_id, batch_id)
    by_company: dict[int, list[Any]] = {}
    skipped_no_company = 0
    for ev in events:
        if ev["company_id"] is None:
            skipped_no_company += 1
            continue
        if not sales_event_has_quote_evidence(ev):
            continue
        by_company.setdefault(int(ev["company_id"]), []).append(ev)

    existing = _existing_quote_milestones(conn, client_id, list(by_company.keys()))
    eligible: list[dict[str, Any]] = []
    to_create: list[dict[str, Any]] = []
    reuse: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []

    for company_id, company_events in sorted(by_company.items()):
        rel_id = _relationship_id(conn, client_id, company_id)
        record_no = _company_record_no(conn, company_id)
        company_name = _blank(company_events[0]["company_name"]) or f"company:{company_id}"
        entry = {
            "client_id": client_id,
            "company_id": company_id,
            "company_name": company_name,
            "external_record_no": record_no,
            "relationship_id": rel_id,
            "event_ids": [int(e["id"]) for e in company_events],
            "event_count": len(company_events),
            "evidence": sorted(
                {r for e in company_events for r in _evidence_reasons(e)}
            ),
            "milestone_date": _pick_milestone_date(company_events),
            "amount": _pick_amount(company_events),
        }
        if rel_id is None:
            skips.append({**entry, "reason": "no_client_company_relationship"})
            continue
        if not record_no:
            skips.append({**entry, "reason": "missing_external_record_no"})
            continue

        existing_rows = existing.get(company_id) or []
        if len(existing_rows) > 1:
            conflicts.append(
                {
                    **entry,
                    "reason": "multiple_existing_quote_milestones",
                    "existing_milestone_ids": [int(r["milestone_id"]) for r in existing_rows],
                }
            )
            # Still reuse the earliest — do not create another
            reuse.append(
                {
                    **entry,
                    "action": "reuse",
                    "milestone_id": int(existing_rows[0]["milestone_id"]),
                    "conflict": True,
                }
            )
            eligible.append(entry)
            continue
        if len(existing_rows) == 1:
            reuse.append(
                {
                    **entry,
                    "action": "reuse",
                    "milestone_id": int(existing_rows[0]["milestone_id"]),
                }
            )
            eligible.append(entry)
            continue

        to_create.append(
            {
                **entry,
                "action": "create",
                "source": QUOTE_MILESTONE_SOURCE,
                "reference_number": _reference_number(batch),
                "notes": _build_notes(batch_id, batch.get("document_id"), company_events),
            }
        )
        eligible.append(entry)

    return {
        "client_id": client_id,
        "batch_id": batch_id,
        "batch_status": _blank(batch.get("status")),
        "document_id": batch.get("document_id"),
        "filename": _blank(batch.get("filename")),
        "eligible_company_count": len(eligible),
        "existing_quote_milestone_count": len(reuse),
        "milestones_to_create_count": len(to_create),
        "skip_count": len(skips) + skipped_no_company,
        "conflict_count": len(conflicts),
        "purchase_orders_inferred": 0,
        "eligible_companies": eligible,
        "to_create": to_create,
        "reuse": reuse,
        "skips": skips,
        "conflicts": conflicts,
        "expected_dashboard_after_apply": {
            "quotes": len(eligible),  # distinct companies with Quote milestone after apply
            "purchase_orders_from_this_projection": 0,
        },
    }


def apply_quote_milestone_projection(
    conn,
    client_id: int,
    batch_id: int,
    *,
    dry_run: bool = True,
    created_by: str = "engagement_import",
) -> dict[str, Any]:
    """Create missing Quote milestones for a batch. Idempotent when re-run.

    dry_run=True (default) only returns the plan — never writes.
    """
    plan = plan_quote_milestone_projection(conn, client_id, batch_id)
    if dry_run:
        return {**plan, "applied": False, "created_milestone_ids": []}

    created_ids: list[int] = []
    now = _now_iso()
    for item in plan["to_create"]:
        # Re-check existence inside apply for race / re-entry safety
        existing = conn.execute(
            """
            SELECT milestone_id FROM revenue_milestones
            WHERE client_id = ? AND company_id = ? AND milestone_type = ?
            ORDER BY milestone_id LIMIT 1
            """,
            (client_id, int(item["company_id"]), QUOTE_MILESTONE_TYPE),
        ).fetchone()
        if existing is not None:
            continue
        cur = conn.execute(
            """
            INSERT INTO revenue_milestones (
                client_id, company_id, relationship_id, external_record_no,
                contact_id, milestone_type, milestone_date, amount,
                reference_number, source, notes, created_by, created_at,
                source_activity_id
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                client_id,
                int(item["company_id"]),
                int(item["relationship_id"]),
                _blank(item["external_record_no"]),
                QUOTE_MILESTONE_TYPE,
                _blank(item["milestone_date"]) or now[:10],
                item.get("amount"),
                _blank(item["reference_number"]),
                QUOTE_MILESTONE_SOURCE,
                _blank(item["notes"]),
                _blank(created_by) or "engagement_import",
                now,
            ),
        )
        created_ids.append(int(cur.lastrowid))

    plan_after = plan_quote_milestone_projection(conn, client_id, batch_id)
    return {
        **plan_after,
        "applied": True,
        "created_milestone_ids": created_ids,
        "created_count": len(created_ids),
        "pre_apply_to_create_count": len(plan["to_create"]),
    }


def preview_quote_milestone_projection(client_id: int, batch_id: int) -> dict[str, Any]:
    """Read-only preview using the active DB connection factory."""
    with get_connection() as conn:
        return apply_quote_milestone_projection(
            conn, client_id, batch_id, dry_run=True
        )


def project_quote_milestones_for_batch(
    conn,
    client_id: int,
    batch_id: int,
    *,
    created_by: str = "engagement_import",
) -> dict[str, Any]:
    """Write path used by engagement confirm (same transaction as sales events)."""
    return apply_quote_milestone_projection(
        conn,
        client_id,
        batch_id,
        dry_run=False,
        created_by=created_by,
    )
