"""Read-only helpers for client_sales_events intelligence (Ask / Research / UI)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from db import get_connection


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _table_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _placeholders(ids: list[int]) -> str:
    return ",".join("?" for _ in ids) if ids else "NULL"


def fetch_sales_events(
    *,
    visible_client_ids: list[int],
    preferred_client_id: int | None = None,
    company_id: int | None = None,
    contact_id: int | None = None,
    company_name: str | None = None,
    contact_name: str | None = None,
    event_type: str | None = None,
    event_family: str | None = None,
    rev_spec: str | None = None,
    text_query: str | None = None,
    has_event_types: list[str] | None = None,
    missing_event_types: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Authorized, newest-first sales-event retrieval. Never mutates."""
    if not visible_client_ids:
        return []
    client_ids = list(visible_client_ids)
    if preferred_client_id and preferred_client_id in visible_client_ids:
        client_ids = [preferred_client_id]

    with get_connection() as conn:
        if not _table_exists(conn, "client_sales_events"):
            return []

        clauses = [f"se.client_id IN ({_placeholders(client_ids)})"]
        params: list[Any] = list(client_ids)

        if company_id is not None:
            clauses.append("se.company_id = ?")
            params.append(company_id)
        if contact_id is not None:
            clauses.append("se.contact_id = ?")
            params.append(contact_id)
        if company_name:
            clauses.append("lower(COALESCE(se.company_name,'')) LIKE ?")
            params.append(f"%{_blank(company_name).lower()}%")
        if contact_name:
            clauses.append("lower(COALESCE(se.contact_name,'')) LIKE ?")
            params.append(f"%{_blank(contact_name).lower()}%")
        if event_type:
            if event_type.endswith("%"):
                clauses.append("se.event_type LIKE ?")
                params.append(event_type)
            else:
                clauses.append("se.event_type = ?")
                params.append(event_type)
        if event_family == "appointment":
            clauses.append("se.event_type LIKE 'Appointment%'")
        elif event_family == "engagement":
            clauses.append("se.event_type = 'Send Information'")
        elif event_family == "rfq":
            clauses.append("se.event_type = 'RFQ'")
        if rev_spec:
            clauses.append("lower(COALESCE(se.source_rev_spec_text,'')) LIKE ?")
            params.append(f"%{_blank(rev_spec).lower()}%")
        if text_query:
            q = f"%{_blank(text_query).lower()}%"
            clauses.append(
                """(
                    lower(COALESCE(se.caller_notes,'')) LIKE ?
                    OR lower(COALESCE(se.sales_notes,'')) LIKE ?
                    OR lower(COALESCE(se.source_date_time_text,'')) LIKE ?
                    OR lower(COALESCE(se.event_type,'')) LIKE ?
                    OR lower(COALESCE(se.source_outcome,'')) LIKE ?
                    OR lower(COALESCE(se.outcome_normalized,'')) LIKE ?
                )"""
            )
            params.extend([q, q, q, q, q, q])

        if has_event_types:
            type_ph = ",".join("?" for _ in has_event_types)
            clauses.append(
                f"""EXISTS (
                    SELECT 1 FROM client_sales_events hx
                    WHERE hx.client_id = se.client_id
                      AND COALESCE(hx.company_id,0) = COALESCE(se.company_id,0)
                      AND hx.event_type IN ({type_ph})
                )"""
            )
            params.extend(has_event_types)

        if missing_event_types:
            type_ph = ",".join("?" for _ in missing_event_types)
            clauses.append(
                f"""NOT EXISTS (
                    SELECT 1 FROM client_sales_events mx
                    WHERE mx.client_id = se.client_id
                      AND COALESCE(mx.company_id,0) = COALESCE(se.company_id,0)
                      AND mx.event_type IN ({type_ph})
                )"""
            )
            params.extend(missing_event_types)

        rows = conn.execute(
            f"""
            SELECT se.*, cl.name AS client_name
            FROM client_sales_events se
            JOIN clients cl ON cl.id = se.client_id
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(se.event_date,'') DESC, se.id DESC
            LIMIT ?
            """,
            [*params, max(1, min(int(limit), 500))],
        ).fetchall()
        return [dict(r) for r in rows]


def event_to_item(r: dict[str, Any], *, excerpt_topic: str | None = None) -> dict[str, Any]:
    caller = _blank(r.get("caller_notes"))
    sales = _blank(r.get("sales_notes"))
    excerpt = ""
    if excerpt_topic:
        topic = excerpt_topic.lower()
        for blob in (caller, sales, _blank(r.get("source_date_time_text"))):
            if topic in blob.lower():
                idx = blob.lower().find(topic)
                start = max(0, idx - 60)
                end = min(len(blob), idx + len(topic) + 120)
                excerpt = ("…" if start else "") + blob[start:end] + ("…" if end < len(blob) else "")
                break
    if not excerpt:
        excerpt = (caller or sales)[:220]
        if len(caller or sales) > 220:
            excerpt += "…"

    rev = _blank(r.get("source_rev_spec_text"))
    rev_user = r.get("rev_spec_user_id")
    return {
        "event_id": int(r["id"]) if r.get("id") is not None else None,
        "event_type": _blank(r.get("event_type")),
        "event_family": _blank(r.get("event_family")),
        "event_date": _blank(r.get("event_date")),
        "event_time": _blank(r.get("event_time")),
        "source_date_time_text": _blank(r.get("source_date_time_text")),
        "company_id": int(r["company_id"]) if r.get("company_id") else None,
        "company_name": _blank(r.get("company_name")),
        "contact_id": int(r["contact_id"]) if r.get("contact_id") else None,
        "contact_name": _blank(r.get("contact_name")),
        "client_id": int(r["client_id"]) if r.get("client_id") else None,
        "client_name": _blank(r.get("client_name")),
        "source_rev_spec_text": rev,
        "rev_spec_user_id": int(rev_user) if rev_user else None,
        "source_rev_spec_label": (
            f"Source Revenue Specialist: {_rev_display_name(rev)}"
            + (" (historical source text — not linked to a NorthStar user account)" if not rev_user else "")
        ),
        "source_appointment_grade": _blank(r.get("source_appointment_grade")),
        "caller_notes": caller[:500],
        "sales_notes": sales[:500],
        "quoted_amount": r.get("quoted_amount"),
        "source_quoted_value": _blank(r.get("source_quoted_value")),
        "outcome": _blank(r.get("outcome_normalized")) or _blank(r.get("source_outcome")),
        "source_record_number": _blank(r.get("source_record_number")),
        "source_sheet": _blank(r.get("source_sheet")),
        "source_row": int(r.get("source_row") or 0),
        "source_file_name": _blank(r.get("source_file_name")),
        "source_batch_id": int(r["source_batch_id"]) if r.get("source_batch_id") else None,
        "excerpt": excerpt,
        "source": (
            f"{_blank(r.get('client_name')) or 'Client'} · "
            f"{_blank(r.get('source_file_name')) or 'sales event'} · "
            f"{_blank(r.get('source_sheet'))} row {int(r.get('source_row') or 0)}"
        ),
    }


def _rev_display_name(raw: str) -> str:
    t = _blank(raw)
    t = __import__("re").sub(r"(?i)^rev\s*spec\s*/?\s*", "", t).strip()
    return t or raw


def summarize_events_for_brief(rows: list[dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    """Prefer latest appointment, reschedule, RFQ, Send Information — not a full dump."""
    if not rows:
        return []
    picked: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    def take(predicate, n: int = 1) -> None:
        count = 0
        for r in rows:
            eid = int(r.get("id") or 0)
            if eid in seen_ids:
                continue
            if predicate(r):
                picked.append(event_to_item(r))
                seen_ids.add(eid)
                count += 1
                if count >= n:
                    return

    take(lambda r: _blank(r.get("event_type")).startswith("Appointment") and "Reschedul" not in _blank(r.get("event_type")), 1)
    take(lambda r: "Reschedul" in _blank(r.get("event_type")), 1)
    take(lambda r: _blank(r.get("event_type")) == "RFQ", 1)
    take(lambda r: _blank(r.get("event_type")) == "Send Information", 1)
    # Meaningful notes leftovers
    take(lambda r: bool(_blank(r.get("caller_notes")) or _blank(r.get("sales_notes")) or r.get("quoted_amount")), 2)
    # Fill remaining newest
    for r in rows:
        if len(picked) >= limit:
            break
        eid = int(r.get("id") or 0)
        if eid not in seen_ids:
            picked.append(event_to_item(r))
            seen_ids.add(eid)
    return picked[:limit]


def sales_event_engagement_signals(
    rows: list[dict[str, Any]],
    *,
    closed_status: str = "",
) -> tuple[list[str], str]:
    """
    Map sales_events → engagement signal labels + suggested level hint.
    Does not invent milestones. Closed/Disqualified status suppresses Active Opportunity.
    """
    status = _blank(closed_status).lower()
    closed = any(x in status for x in ("closed", "disqual", "do not call", "dnc", "lost"))

    signals: list[str] = []
    newest_date = ""
    has_recent_rfq = False
    has_appt = False
    has_send = False
    has_rfq = False

    today = date.today()
    for r in rows:
        et = _blank(r.get("event_type"))
        ed = _blank(r.get("event_date"))
        if ed and ed > newest_date:
            newest_date = ed
        age_days = None
        if ed:
            try:
                age_days = (today - datetime.strptime(ed[:10], "%Y-%m-%d").date()).days
            except ValueError:
                age_days = None

        if et.startswith("Appointment"):
            has_appt = True
            if "Appointment Set" not in signals:
                signals.append("Appointment Set")
        elif et == "RFQ":
            has_rfq = True
            if "RFQ" not in signals:
                signals.append("RFQ")
            if age_days is None or age_days <= 365:
                has_recent_rfq = True
        elif et == "Send Information":
            has_send = True
            if "Send Information" not in signals:
                signals.append("Send Information")

    if closed:
        level = "Closed / Not Pursuing"
    elif has_recent_rfq and has_appt:
        level = "Active Opportunity"
    elif has_recent_rfq:
        level = "Active Opportunity"
    elif has_appt:
        level = "Engaged"
    elif has_send and not has_rfq and not has_appt:
        level = "Early Engagement"
    elif has_rfq:
        # Historical RFQ without recency certainty
        level = "Engaged"
    elif signals:
        level = "Early Engagement"
    else:
        level = "Insufficient Information"

    return signals, level
