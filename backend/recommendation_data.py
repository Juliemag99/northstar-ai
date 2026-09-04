"""NorthStar Recommendation — advisory next-action layer (read-only).

Independent from Campaign Fit and Opportunity / Engagement.
Never writes CRM data or changes Work Queue ranking.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from models import NorthStarRecommendation


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_fit(fit_result: str) -> str:
    t = _blank(fit_result).lower()
    if not t:
        return "insufficient"
    if "strong" in t:
        return "strong"
    if "possible" in t or "partial" in t:
        return "possible"
    if "weak" in t or "poor" in t or "not a fit" in t or "nofit" in t.replace(" ", ""):
        return "weak"
    if "criteria not configured" in t:
        return "insufficient"
    if "insufficient" in t or "unknown" in t or "not enough" in t:
        return "insufficient"
    return "insufficient"


def _norm_engagement(level: str, signals: list[str] | None = None) -> str:
    t = _blank(level).lower()
    sigs = { _blank(s).lower() for s in (signals or []) }
    if any(x in t for x in ("closed", "not pursuing", "disqual")):
        return "closed"
    if "active opportunity" in t or "proven commercial" in t or "active commercial" in t:
        return "active"
    if "engaged" in t or "active sales" in t:
        return "engaged"
    if "early" in t or "nurture" in t:
        return "early"
    if "no" in t and "engagement" in t:
        return "none"
    if "insufficient" in t or "no stored" in t:
        # Signal-based fallback
        if any(s in sigs for s in ("rfq", "purchase order", "quote")):
            return "active"
        if "appointment set" in sigs:
            return "engaged"
        if "send information" in sigs:
            return "early"
        return "none"
    if "cross-client" in t:
        return "early"
    if any(s in sigs for s in ("rfq", "purchase order", "quote")):
        return "active"
    if "appointment set" in sigs:
        return "engaged"
    if "send information" in sigs:
        return "early"
    return "none"


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


def _parse_event_age_days(event: dict[str, Any]) -> int | None:
    ed = _blank(event.get("event_date"))
    if not ed:
        return None
    try:
        d = datetime.strptime(ed[:10], "%Y-%m-%d").date()
        return (date.today() - d).days
    except ValueError:
        return None


def _collect_evidence(
    *,
    engagement_signals: list[str],
    sales_events: list[dict[str, Any]],
    status: str,
    fit_result: str,
) -> list[str]:
    evidence: list[str] = []
    for s in engagement_signals or []:
        if _blank(s) and _blank(s) not in evidence:
            evidence.append(_blank(s))

    # Prefer recent / high-signal sales events (without inventing dates)
    ranked_types = []
    for ev in sales_events or []:
        et = _blank(ev.get("event_type"))
        if not et:
            continue
        age = _parse_event_age_days(ev)
        # Recency boost when confidently parsed; otherwise still include type once
        weight = 0
        if et == "RFQ":
            weight = 90
        elif et.startswith("Appointment"):
            weight = 80
        elif et == "Send Information":
            weight = 40
        if age is not None:
            if age <= 90:
                weight += 20
            elif age > 365:
                weight -= 25
        ranked_types.append((weight, et, age))

    ranked_types.sort(key=lambda x: -x[0])
    seen = set(e.lower() for e in evidence)
    for weight, et, age in ranked_types[:6]:
        label = et
        if age is not None and age <= 120:
            label = f"{et} (recent)"
        elif age is None and _blank(
            next((e.get("source_date_time_text") for e in sales_events if _blank(e.get("event_type")) == et), "")
        ):
            # Date text exists but not confidently parsed — do not claim recency
            label = et
        if label.lower() not in seen and et.lower() not in seen:
            evidence.append(label)
            seen.add(label.lower())
            seen.add(et.lower())

    if _blank(status):
        evidence.append(f"Current status: {status}")
    if _blank(fit_result):
        evidence.append(f"Campaign Fit: {fit_result}")
    return evidence[:10]


def build_northstar_recommendation(
    *,
    fit_result: str = "",
    engagement_level: str = "",
    engagement_signals: list[str] | None = None,
    relationship_status: str = "",
    sales_events: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    missing_information: list[str] | None = None,
    research_gaps: list[str] | None = None,
    campaign_name: str = "",
    client_name: str = "",
    has_follow_up: bool = False,
) -> NorthStarRecommendation:
    """Derive one primary advisory recommendation. Never mutates CRM."""
    fit = _norm_fit(fit_result)
    eng = _norm_engagement(engagement_level, engagement_signals)
    status = _blank(relationship_status)
    signals = list(engagement_signals or [])
    events = list(sales_events or [])
    camp = _blank(campaign_name) or "the selected campaign"
    client = _blank(client_name) or "this client"

    evidence = _collect_evidence(
        engagement_signals=signals,
        sales_events=events,
        status=status,
        fit_result=fit_result,
    )

    has_rfq = any(
        _blank(e.get("event_type")) == "RFQ"
        or "rfq" in _blank(s).lower()
        for e in events
        for s in ([e.get("event_type")] + signals)
    ) or any("rfq" in _blank(s).lower() for s in signals)

    # Prefer recent RFQ when date parsed
    recent_rfq = False
    for e in events:
        if _blank(e.get("event_type")) != "RFQ":
            continue
        age = _parse_event_age_days(e)
        if age is None or age <= 365:
            recent_rfq = True
            break

    has_appt = any(
        _blank(e.get("event_type")).startswith("Appointment") for e in events
    ) or any("appointment" in _blank(s).lower() for s in signals)

    has_send = any(
        _blank(e.get("event_type")) == "Send Information" for e in events
    ) or any("send information" in _blank(s).lower() for s in signals)

    has_contact = bool(contacts)
    decision_maker = False
    for c in contacts or []:
        title = _blank(c.get("title")).lower()
        if any(h in title for h in ("buyer", "purchas", "procure", "sourc", "engineer", "manager")):
            decision_maker = True
            break

    gaps = [
        _blank(g)
        for g in (missing_information or []) + (research_gaps or [])
        if _blank(g)
    ]
    # Dedupe gaps
    seen_g: set[str] = set()
    need: list[str] = []
    for g in gaps:
        key = g.lower()
        if key in seen_g:
            continue
        seen_g.add(key)
        need.append(g)

    advisory = (
        "Advisory only - does not change CRM status, create tasks, or alter Work Queue ranking."
    )

    # --- Closed / do-not-pursue safety first ---
    if _is_closed_status(status) or eng == "closed":
        return NorthStarRecommendation(
            action="Do Not Pursue",
            why=(
                f"Current {client} disposition indicates the account should not be actively "
                f"pursued right now ({status or engagement_level or 'Closed / Not Pursuing'}). "
                "Historical sales events remain visible for context but do not override "
                "the current CRM status."
            ),
            evidence_used=evidence,
            still_need_to_know=need[:4]
            or ["Confirm whether disposition should be revisited with the client team."],
            fit_context=_blank(fit_result) or "—",
            engagement_context=_blank(engagement_level) or "—",
            confidence="high",
            advisory_note=advisory,
        )

    # --- RFQ path ---
    if (has_rfq or recent_rfq) and eng in {"active", "engaged", "early"}:
        action = "Review RFQ"
        why = (
            f"NorthStar shows RFQ-related engagement for {client}. "
            "Review the request, clarify requirements, and decide whether it advances "
            f"toward a quote under {camp} - without assuming a Quote milestone yet."
        )
        if fit == "insufficient":
            why += (
                " Campaign Fit is still insufficient, so confirm the RFQ process/part type "
                "matches the campaign before investing more pursuit time."
            )
            if not any("stamp" in n.lower() or "fit" in n.lower() for n in need):
                need.insert(
                    0,
                    f"Whether the RFQ requires components aligned with {camp}.",
                )
        elif fit == "weak":
            action = "Review Account"
            why = (
                f"An RFQ is on file, but Campaign Fit looks weak for {camp}. "
                "Review the account carefully before investing further."
            )
        return NorthStarRecommendation(
            action=action,
            why=why,
            evidence_used=evidence,
            still_need_to_know=need[:5]
            or ["RFQ quantities, material, and timing expectations."],
            fit_context=_blank(fit_result) or "—",
            engagement_context=_blank(engagement_level) or "—",
            confidence="medium",
            advisory_note=advisory,
        )

    # --- Combination matrix ---
    action = "Review Account"
    why = ""
    confidence = "medium"

    if fit == "strong" and eng in {"active", "engaged"}:
        action = "Follow Up Now"
        why = (
            f"Campaign Fit looks strong for {camp} and there is active/engaged commercial "
            "history. Prioritize a timely follow-up to advance the opportunity."
        )
        if has_appt and has_follow_up:
            action = "Prepare for Appointment"
            why = (
                "An appointment-related history is on file and follow-up timing is relevant. "
                "Prepare talking points and confirm the meeting objective."
            )
    elif fit == "strong" and eng in {"none", "early"}:
        action = "Identify Decision Maker" if not decision_maker else "Start Outreach"
        # Use supported type names from the spec
        if not decision_maker or not has_contact:
            action = "Identify Decision Maker"
            why = (
                f"Campaign Fit looks strong for {camp}, but current engagement is limited. "
                "Identify the right purchasing/decision-maker contact before deeper outreach."
            )
        else:
            action = "Follow Up Now"
            why = (
                f"Campaign Fit looks strong for {camp} with little current engagement. "
                "A focused outreach to the known contact is the most useful next step."
            )
        if fit == "strong" and eng == "none" and not has_contact:
            action = "Research Company" if not gaps else "Identify Decision Maker"
    elif fit == "possible" and eng in {"active", "engaged", "early"}:
        action = "Confirm Opportunity Fit"
        why = (
            f"There is meaningful engagement, and Campaign Fit is only possible for {camp}. "
            "Continue the relationship while confirming the process/part need matches the campaign."
        )
    elif fit == "insufficient" and eng in {"active", "engaged", "early"}:
        action = "Confirm Opportunity Fit"
        why = (
            f"NorthStar shows engagement with the company"
            + (", including appointment history" if has_appt else "")
            + (", information exchange" if has_send else "")
            + f", but research has not yet verified that the opportunity fits {camp}. "
            "Confirm the actual process need before treating this as a campaign-qualified pursuit."
        )
        if not any("stamp" in n.lower() or "component" in n.lower() or "fit" in n.lower() for n in need):
            need.insert(
                0,
                f"Whether the current project requires components aligned with {camp}.",
            )
    elif fit == "insufficient" and eng in {"none", "early"} and not has_appt and not has_rfq:
        action = "Research Company"
        why = (
            f"There is little verified Campaign Fit evidence for {camp} and limited "
            "current engagement. Research the company before investing outreach time."
        )
    elif fit == "weak" and eng in {"active", "engaged"}:
        action = "Review Account"
        why = (
            f"There is commercial engagement on file, but Campaign Fit looks weak for {camp}. "
            "Review the account before pursuing further under this campaign."
        )
    elif fit == "weak" and eng in {"none", "early"}:
        action = "No Immediate Action"
        why = (
            f"Campaign Fit looks weak for {camp} and there is little current engagement. "
            "No immediate pursuit is recommended unless new qualifying evidence appears."
        )
    elif fit == "possible" and eng == "none":
        action = "Identify Decision Maker" if not has_contact else "Follow Up Now"
        if not has_contact:
            action = "Identify Decision Maker"
            why = (
                f"Possible fit for {camp} with no meaningful engagement yet. "
                "Identify a decision maker and begin light outreach."
            )
        else:
            action = "Nurture"
            why = (
                f"Possible fit for {camp} with limited engagement. "
                "Nurture the known contact and look for a concrete need signal."
            )
    elif has_appt and eng in {"engaged", "active", "early"}:
        action = "Prepare for Appointment" if has_follow_up else "Follow Up Now"
        why = (
            "Appointment-related history is on file. Prepare or follow up to keep momentum, "
            f"while confirming alignment with {camp}."
        )
    elif eng == "early" and has_send:
        action = "Nurture"
        why = (
            "Early engagement (such as Send Information) is on file. "
            "Nurture the contact and watch for a stronger opportunity signal."
        )
    else:
        action = "Review Account"
        why = (
            "NorthStar has some context on file, but the next step is not clear-cut. "
            "Review fit, engagement, and recent history before acting."
        )
        confidence = "low"

    # Contact gap refinement
    if action in {"Follow Up Now", "Confirm Opportunity Fit"} and not has_contact:
        need.append("A reachable decision-maker or purchasing contact.")
    if action == "Confirm Opportunity Fit" and not need:
        need.append(
            f"Whether the live opportunity requires work aligned with {camp}."
        )

    # Normalize action labels to allowed set
    allowed = {
        "Follow Up Now",
        "Prepare for Appointment",
        "Review RFQ",
        "Confirm Opportunity Fit",
        "Identify Decision Maker",
        "Research Company",
        "Nurture",
        "No Immediate Action",
        "Do Not Pursue",
        "Review Account",
        "Start Outreach",
    }
    if action == "Start Outreach":
        action = "Follow Up Now"
    if action not in allowed:
        action = "Review Account"

    return NorthStarRecommendation(
        action=action,
        why=why,
        evidence_used=evidence,
        still_need_to_know=need[:5],
        fit_context=_blank(fit_result) or "—",
        engagement_context=_blank(engagement_level) or "—",
        confidence=confidence,
        advisory_note=advisory,
    )
