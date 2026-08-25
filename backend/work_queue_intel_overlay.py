"""Work Queue NorthStar Insight overlay — OBSERVATION MODE ONLY.

Attaches stored Fit / Engagement / Recommendation beside existing Work Queue
rows. Never changes ranking, priority scores, next actions, or CRM data.
Never triggers public web research.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from db import get_connection
from models import WorkQueueInsightSummary, WorkQueueNorthStarInsight, WorkQueueRow
from recommendation_data import build_northstar_recommendation
from sales_events_intel import sales_event_engagement_signals


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_closed_status(status: str) -> bool:
    t = _blank(status).lower()
    return any(
        x in t
        for x in (
            "closed",
            "disqual",
            "do not call",
            "dnc",
            "do not pursue",
            "lost",
            "not interested",
        )
    )


def _parse_age_days(event_date: str) -> int | None:
    ed = _blank(event_date)
    if not ed:
        return None
    try:
        return (date.today() - datetime.strptime(ed[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _norm_fit_bucket(fit: str) -> str:
    t = _blank(fit).lower()
    if not t or t == "not evaluated":
        return "not_evaluated"
    if "strong" in t:
        return "strong"
    if "possible" in t or "partial" in t:
        return "possible"
    if "weak" in t or "poor" in t or "not a fit" in t:
        return "weak"
    if "insufficient" in t:
        return "insufficient"
    return "insufficient"


def _norm_engagement_bucket(level: str) -> str:
    t = _blank(level).lower()
    if not t or t == "not evaluated":
        return "not_evaluated"
    if "closed" in t or "not pursuing" in t:
        return "closed"
    if "active opportunity" in t:
        return "active"
    if "engaged" in t:
        return "engaged"
    if "early" in t:
        return "early"
    if "nurture" in t:
        return "nurture"
    if "no" in t and "engagement" in t:
        return "none"
    if "insufficient" in t:
        return "insufficient"
    return "insufficient"


def _is_high_priority(row: WorkQueueRow) -> bool:
    return row.work_priority >= 78 or row.priority_label in {"Critical", "High"}


def _is_low_priority(row: WorkQueueRow) -> bool:
    return row.work_priority < 50 or row.priority_label == "Low"


def _pursue_oriented(row: WorkQueueRow) -> bool:
    blob = f"{row.next_action} {row.work_type} {row.why_in_queue}".lower()
    return any(
        x in blob
        for x in (
            "call",
            "follow",
            "appoint",
            "outreach",
            "contact",
            "weblead",
            "hot",
            "start working",
            "confirm appointment",
            "needs next",
        )
    )


def _passive_recommendation(action: str) -> bool:
    return _blank(action) in {"Do Not Pursue", "No Immediate Action"}


def _table_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _batch_load_stored_intel(
    pairs: list[tuple[int, int]],
) -> dict[tuple[int, int], dict[str, Any]]:
    """Load fit + client-scoped sales_events + milestones. No public research."""
    out: dict[tuple[int, int], dict[str, Any]] = {
        p: {
            "fit_result": "",
            "missing_information": [],
            "has_fit_row": False,
            "sales_events": [],
            "milestone_signals": [],
            "contacts": [],
        }
        for p in pairs
    }
    if not pairs:
        return out

    client_ids = sorted({c for c, _ in pairs})
    company_ids = sorted({co for _, co in pairs})
    pair_set = set(pairs)

    with get_connection() as conn:
        if _table_exists(conn, "company_client_fit"):
            cph = ",".join("?" * len(client_ids))
            coh = ",".join("?" * len(company_ids))
            for row in conn.execute(
                f"""
                SELECT company_id, client_id, fit_result, missing_information
                FROM company_client_fit
                WHERE client_id IN ({cph}) AND company_id IN ({coh})
                """,
                [*client_ids, *company_ids],
            ):
                key = (int(row["client_id"]), int(row["company_id"]))
                if key not in pair_set:
                    continue
                miss: list[str] = []
                try:
                    miss = [
                        _blank(x)
                        for x in json.loads(row["missing_information"] or "[]")
                        if _blank(x)
                    ]
                except Exception:
                    miss = []
                out[key]["has_fit_row"] = True
                out[key]["fit_result"] = _blank(row["fit_result"])
                out[key]["missing_information"] = miss

        if _table_exists(conn, "client_sales_events"):
            cph = ",".join("?" * len(client_ids))
            coh = ",".join("?" * len(company_ids))
            for row in conn.execute(
                f"""
                SELECT client_id, company_id, event_type, event_date,
                       source_date_time_text
                FROM client_sales_events
                WHERE client_id IN ({cph})
                  AND company_id IN ({coh})
                  AND company_id IS NOT NULL
                ORDER BY COALESCE(event_date, '') DESC, id DESC
                """,
                [*client_ids, *company_ids],
            ):
                key = (int(row["client_id"]), int(row["company_id"]))
                if key not in pair_set:
                    continue
                out[key]["sales_events"].append(
                    {
                        "event_type": _blank(row["event_type"]),
                        "event_date": _blank(row["event_date"]),
                        "source_date_time_text": _blank(row["source_date_time_text"]),
                    }
                )

        if _table_exists(conn, "revenue_milestones"):
            cph = ",".join("?" * len(client_ids))
            coh = ",".join("?" * len(company_ids))
            for row in conn.execute(
                f"""
                SELECT client_id, company_id, milestone_type
                FROM revenue_milestones
                WHERE client_id IN ({cph}) AND company_id IN ({coh})
                """,
                [*client_ids, *company_ids],
            ):
                key = (int(row["client_id"]), int(row["company_id"]))
                if key not in pair_set:
                    continue
                mt = _blank(row["milestone_type"])
                if mt and mt not in out[key]["milestone_signals"]:
                    out[key]["milestone_signals"].append(mt)

        # Contact titles for decision-maker hints (client-scoped via company)
        if _table_exists(conn, "contacts") and company_ids:
            coh = ",".join("?" * len(company_ids))
            for row in conn.execute(
                f"""
                SELECT company_id, title
                FROM contacts
                WHERE company_id IN ({coh})
                LIMIT 5000
                """,
                company_ids,
            ):
                # Attach to all client pairs for that company (titles are shared)
                for cid in client_ids:
                    key = (cid, int(row["company_id"]))
                    if key not in pair_set:
                        continue
                    title = _blank(row["title"])
                    if title:
                        out[key]["contacts"].append({"title": title})

    return out


def _classify_alignment(
    *,
    row: WorkQueueRow,
    evaluated: bool,
    recommendation: str,
    engagement: str,
    signals: list[str],
    sales_events: list[dict[str, Any]],
) -> tuple[str, str, str]:
    """
    Returns (alignment, review_reason, review_flag).
    alignment: Aligned | Review | Insufficient AI Data
    """
    if not evaluated:
        return "Insufficient AI Data", "", ""

    closed = _is_closed_status(row.status)
    high = _is_high_priority(row)
    low = _is_low_priority(row)
    pursue = _pursue_oriented(row)
    reasons: list[str] = []
    review_flag = ""

    if closed and pursue:
        review_flag = "REVIEW — CURRENT CRM DISPOSITION"
        reasons.append(
            "Current CRM disposition is Closed/Disqualified/Do Not Call while the "
            "item remains actionable in Work Queue."
        )

    if high and recommendation == "Do Not Pursue":
        reasons.append(
            "High Work Queue priority but NorthStar Recommendation is Do Not Pursue."
        )

    if high and closed:
        reasons.append(
            "High Work Queue priority with Closed/Disqualified CRM disposition."
        )

    recent_rfq = False
    recent_appt = False
    for ev in sales_events:
        et = _blank(ev.get("event_type"))
        age = _parse_age_days(_blank(ev.get("event_date")))
        if et == "RFQ" and (age is None or age <= 365):
            recent_rfq = True
        if et.startswith("Appointment") and (age is None or age <= 180):
            recent_appt = True

    eng_active = engagement in {"Active Opportunity", "Engaged"} or any(
        s in {"RFQ", "Appointment Set"} for s in signals
    )

    if low and (recent_rfq or recent_appt or eng_active):
        parts = []
        if recent_rfq or "RFQ" in signals:
            parts.append("RFQ")
        if recent_appt or "Appointment Set" in signals:
            parts.append("appointment")
        if not parts and eng_active:
            parts.append("active commercial signals")
        reasons.append(
            "Low Work Queue priority but stored engagement shows "
            + " / ".join(parts)
            + "."
        )

    if _passive_recommendation(recommendation) and pursue and not closed:
        reasons.append(
            "Work Queue next action is pursuit-oriented while NorthStar Recommendation "
            f"is {recommendation}."
        )

    if recommendation == "Review RFQ" and "rfq" not in f"{row.work_type} {row.next_action} {row.why_in_queue}".lower():
        if recent_rfq or "RFQ" in signals:
            reasons.append(
                "Stored RFQ evidence suggests reviewing the RFQ, which is not reflected "
                "in the existing Work Queue action wording."
            )

    if reasons:
        return "Review", " ".join(reasons), review_flag or "REVIEW"

    return "Aligned", "", ""


def build_insight_for_pair(
    *,
    row: WorkQueueRow,
    stored: dict[str, Any],
) -> WorkQueueNorthStarInsight:
    advisory = (
        "Observation only — NorthStar Insight does not change Work Queue ranking, "
        "priority scores, or next actions."
    )
    has_fit = bool(stored.get("has_fit_row"))
    events = list(stored.get("sales_events") or [])
    milestones = list(stored.get("milestone_signals") or [])
    evaluated = has_fit or bool(events) or bool(milestones)

    if not evaluated:
        return WorkQueueNorthStarInsight(
            evaluated=False,
            fit="Not Evaluated",
            engagement="Not Evaluated",
            recommendation="Not Evaluated",
            why="",
            evidence_used=[],
            still_need_to_know=[],
            signals=[],
            alignment="Insufficient AI Data",
            review_reason="",
            review_flag="",
            advisory_note=advisory,
        )

    fit_result = _blank(stored.get("fit_result")) or "Insufficient Information"
    se_signals, se_level = sales_event_engagement_signals(
        events, closed_status=row.status
    )
    eng_signals = list(dict.fromkeys([*se_signals, *milestones]))
    eng_level = se_level
    if milestones and eng_level in {"Insufficient Information", ""}:
        # Client-scoped milestone only — never another client's history
        mil_l = {m.lower() for m in milestones}
        if any(x in mil_l for x in ("rfq", "quote", "purchase order", "po")):
            eng_level = "Active Opportunity"
        elif any("appointment" in m for m in mil_l):
            eng_level = "Engaged"
        else:
            eng_level = "Early Engagement"

    if _is_closed_status(row.status):
        eng_level = "Closed / Not Pursuing"

    rec = build_northstar_recommendation(
        fit_result=fit_result,
        engagement_level=eng_level,
        engagement_signals=eng_signals,
        relationship_status=row.status,
        sales_events=events,
        contacts=list(stored.get("contacts") or []),
        missing_information=list(stored.get("missing_information") or []),
        campaign_name="Default",
        client_name=row.client_name or "this client",
    )

    alignment, review_reason, review_flag = _classify_alignment(
        row=row,
        evaluated=True,
        recommendation=rec.action,
        engagement=eng_level,
        signals=eng_signals,
        sales_events=events,
    )

    return WorkQueueNorthStarInsight(
        evaluated=True,
        fit=fit_result,
        engagement=eng_level,
        recommendation=rec.action,
        why=rec.why,
        evidence_used=list(rec.evidence_used),
        still_need_to_know=list(rec.still_need_to_know),
        signals=eng_signals,
        alignment=alignment,
        review_reason=review_reason,
        review_flag=review_flag,
        advisory_note=advisory,
    )


def attach_northstar_insight_overlay(
    rows: list[WorkQueueRow],
) -> tuple[list[WorkQueueRow], WorkQueueInsightSummary]:
    """
    Enrich ranked Work Queue rows with observation-only insight.
    Preserves order and never mutates work_priority / next_action.
    """
    if not rows:
        return [], WorkQueueInsightSummary()

    pairs = list({(r.client_id, r.company_id) for r in rows})
    stored_map = _batch_load_stored_intel(pairs)
    insight_cache: dict[tuple[int, int], WorkQueueNorthStarInsight] = {}

    enriched: list[WorkQueueRow] = []
    aligned = review = insufficient = 0

    for row in rows:
        key = (row.client_id, row.company_id)
        if key not in insight_cache:
            insight_cache[key] = build_insight_for_pair(
                row=row, stored=stored_map.get(key) or {}
            )
        # Re-classify per row (priority/next_action differ even for same company)
        base = insight_cache[key]
        if not base.evaluated:
            insight = base
        else:
            alignment, review_reason, review_flag = _classify_alignment(
                row=row,
                evaluated=True,
                recommendation=base.recommendation,
                engagement=base.engagement,
                signals=base.signals,
                sales_events=(stored_map.get(key) or {}).get("sales_events") or [],
            )
            insight = base.model_copy(
                update={
                    "alignment": alignment,
                    "review_reason": review_reason,
                    "review_flag": review_flag,
                }
            )

        if insight.alignment == "Aligned":
            aligned += 1
        elif insight.alignment == "Review":
            review += 1
        else:
            insufficient += 1

        enriched.append(row.model_copy(update={"northstar_insight": insight}))

    summary = WorkQueueInsightSummary(
        aligned=aligned,
        review=review,
        insufficient_ai_data=insufficient,
        evaluated=aligned + review,
        not_evaluated=insufficient,
    )
    return enriched, summary


def filter_rows_by_insight(
    rows: list[WorkQueueRow],
    *,
    ai_alignment: str | None = None,
    ai_recommendation: str | None = None,
    ai_fit: str | None = None,
    ai_engagement: str | None = None,
) -> list[WorkQueueRow]:
    """Filter without reordering. Ranking order is preserved."""
    out = rows

    if ai_alignment:
        want = ai_alignment.strip().lower()
        mapping = {
            "aligned": "aligned",
            "review": "review",
            "insufficient": "insufficient ai data",
            "insufficient ai data": "insufficient ai data",
            "insufficient_ai_data": "insufficient ai data",
        }
        target = mapping.get(want, want)
        out = [
            r
            for r in out
            if r.northstar_insight
            and r.northstar_insight.alignment.lower() == target
        ]

    if ai_recommendation:
        want = ai_recommendation.strip().lower()
        if want == "not evaluated":
            out = [
                r
                for r in out
                if not r.northstar_insight
                or not r.northstar_insight.evaluated
                or r.northstar_insight.recommendation.lower() == "not evaluated"
            ]
        else:
            out = [
                r
                for r in out
                if r.northstar_insight
                and r.northstar_insight.recommendation.lower() == want
            ]

    if ai_fit:
        want = ai_fit.strip().lower()
        if want in {"not evaluated", "not_evaluated"}:
            out = [
                r
                for r in out
                if not r.northstar_insight
                or not r.northstar_insight.evaluated
                or r.northstar_insight.fit.lower() == "not evaluated"
            ]
        else:
            out = [
                r
                for r in out
                if r.northstar_insight
                and _norm_fit_bucket(r.northstar_insight.fit) == _norm_fit_bucket(want)
            ]

    if ai_engagement:
        want = ai_engagement.strip().lower()
        if want in {"not evaluated", "not_evaluated"}:
            out = [
                r
                for r in out
                if not r.northstar_insight
                or not r.northstar_insight.evaluated
                or r.northstar_insight.engagement.lower() == "not evaluated"
            ]
        else:
            # Map filter labels to buckets
            bucket_map = {
                "active opportunity": "active",
                "active": "active",
                "engaged": "engaged",
                "early engagement": "early",
                "early": "early",
                "nurture": "nurture",
                "no current engagement": "none",
                "none": "none",
                "closed / not pursuing": "closed",
                "closed": "closed",
                "insufficient information": "insufficient",
                "insufficient": "insufficient",
            }
            target = bucket_map.get(want, _norm_engagement_bucket(want))
            out = [
                r
                for r in out
                if r.northstar_insight
                and _norm_engagement_bucket(r.northstar_insight.engagement) == target
            ]

    return out
