"""Review classification & bulk resolve eligibility for extraction proposals.

Read-only evaluation against CURRENT approved/stored client knowledge.
Never writes Client Setup / CRM / Client Contacts.
"""

from __future__ import annotations

from staff_context import resolve_staff_actor

import json
import re
from typing import Any

from access import get_default_user, get_user_by_id
from client_setup_data import get_client_setup, user_can_edit_client_setup
from db import get_connection


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_url(value: str) -> str:
    t = _blank(value).lower()
    t = re.sub(r"^https?://", "", t)
    t = re.sub(r"^www\.", "", t)
    return t.rstrip("/")


def _norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", _blank(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _norm_text(value: str) -> str:
    t = _blank(value).lower()
    # Normalize common OCR/unicode punctuation before stripping
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    t = t.replace("\u00d7", "x").replace("\u2715", "x")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s@./+-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _significant_token_overlap(a: str, b: str, *, min_hits: int = 3) -> bool:
    stop = {
        "that",
        "this",
        "with",
        "from",
        "have",
        "they",
        "them",
        "their",
        "would",
        "could",
        "about",
        "into",
        "only",
        "also",
        "than",
        "then",
        "when",
        "where",
        "what",
        "which",
        "while",
        "being",
        "been",
        "were",
        "will",
        "your",
        "more",
        "most",
        "some",
        "such",
        "other",
        "over",
        "under",
        "after",
        "before",
        "between",
        "through",
        "during",
        "without",
        "within",
        "across",
        "around",
        "among",
        "against",
        "because",
        "should",
        "may",
        "might",
        "must",
        "need",
        "needs",
        "wanting",
        "anymore",
        "ideal",
        "help",
        "them",
        "not",
        "but",
        "and",
        "the",
        "for",
        "are",
        "has",
        "had",
        "was",
        "new",
        "now",
    }
    ta = {
        t
        for t in re.findall(r"[a-z]{4,}", _norm_text(a))
        if t not in stop
    }
    tb = {
        t
        for t in re.findall(r"[a-z]{4,}", _norm_text(b))
        if t not in stop
    }
    if not ta or not tb:
        return False
    return len(ta & tb) >= min_hits


def _contains_norm(hay: str, needle: str) -> bool:
    h = _norm_text(hay)
    n = _norm_text(needle)
    if not n or len(n) < 4:
        return False
    return n in h or h in n


def _capacity_represented(stored: str, proposed: str) -> bool:
    """True when proposed capacity/equipment facts are already in stored equip text."""
    stored_n = _norm_text(stored)
    proposed_n = _norm_text(proposed)
    if not stored_n or not proposed_n:
        return False
    if _contains_norm(stored, proposed) or _contains_norm(proposed, stored):
        return True
    # Numeric fact overlap (tons, bed size, thickness, coil) including leading decimals
    num_re = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)(?![\w.])")
    props = {m.group(1).lstrip("0") or m.group(1) for m in num_re.finditer(proposed_n)}
    stores = {m.group(1).lstrip("0") or m.group(1) for m in num_re.finditer(stored_n)}
    # Also keep normalized forms for .013 → 013 / .013
    def expand(nums: set[str]) -> set[str]:
        out = set(nums)
        for n in list(nums):
            if n.startswith("."):
                out.add(n.lstrip("."))
            elif "." not in n and n.isdigit():
                out.add(n.lstrip("0") or n)
        return out

    props = expand(props)
    stores = expand(stores)
    if not props:
        return False
    hits = props & stores
    return len(hits) >= max(1, (len(props) + 1) // 2)


def _knowledge_blob(client_id: int, user_id: int | None) -> dict[str, Any]:
    from client_contacts_data import list_client_contacts
    from client_knowledge_data import (
        get_knowledge_sections,
        list_approved_client_operations,
        list_email_templates,
    )

    setup = get_client_setup(client_id, user_id=user_id)
    sections = {
        s.section_key: (s.payload or {})
        for s in get_knowledge_sections(client_id, user_id=user_id)
    }
    ops = list_approved_client_operations(client_id, user_id=user_id)
    contacts = list_client_contacts(client_id, user_id=user_id, include_inactive=True)
    templates = list_email_templates(client_id, user_id=user_id)

    camp = None
    if setup.default_campaign_id:
        camp = next(
            (c for c in setup.campaigns if c.campaign_id == setup.default_campaign_id),
            None,
        )
    if camp is None and setup.campaigns:
        camp = setup.campaigns[0]

    return {
        "setup": setup,
        "sections": sections,
        "ops": ops,
        "contacts": contacts,
        "templates": templates,
        "campaign": camp,
    }


def _section_text(sections: dict[str, Any], key: str) -> str:
    payload = sections.get(key) or {}
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def evaluate_proposal_resolve_eligibility(
    client_id: int,
    proposal: dict[str, Any] | Any,
    *,
    blob: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """
    Return eligibility for Resolve / Incorporated against CURRENT stored knowledge.

    ready=True only when material info is already represented in approved/stored
    locations — NOT merely because a related Pending proposal exists.
    """
    if blob is None:
        blob = _knowledge_blob(client_id, user_id)

    if hasattr(proposal, "model_dump"):
        p = proposal.model_dump()
    elif hasattr(proposal, "keys"):
        p = dict(proposal)
    else:
        p = dict(proposal)

    pid = int(p.get("proposal_id") or p.get("id") or 0)
    section = _blank(p.get("section"))
    field = _blank(p.get("field_name"))
    value = _blank(p.get("proposed_value") or p.get("extracted_value"))
    classification = _blank(p.get("classification"))
    status = _blank(p.get("status")) or "Pending"

    base = {
        "proposal_id": pid,
        "section": section,
        "field_name": field,
        "field_label": _blank(p.get("field_label")) or field,
        "status": status,
        "ready": False,
        "review_class": "needs_review",
        "reason": "",
        "resolution_reason": "",
    }

    if status != "Pending":
        base["reason"] = f"Status is {status}, not Pending."
        return base

    if (
        classification == "SENSITIVE — NOT IMPORTED"
        or field in {"sensitive_exclusion"}
        or "password" in field.lower()
        or "credential" in field.lower()
    ):
        base["review_class"] = "sensitive"
        base["reason"] = "Sensitive credential / secret — do not import."
        return base

    # Noise heuristics
    if field in {"appointment_grid_headers"} or "grid_header" in field:
        base["review_class"] = "noise"
        base["reason"] = "Appointment grid column headers — not client knowledge."
        return base
    if field == "production_processes" and re.search(
        r"(?i)units/?year|size of parts|certifications\s*;|headers?",
        value,
    ) and len(value) < 280:
        # spreadsheet-ish labels
        if value.count("\n") < 3 and ";" in value and len(value.split()) < 40:
            base["review_class"] = "noise"
            base["reason"] = "Looks like spreadsheet labels/headers, not processes."
            return base

    setup = blob["setup"]
    camp = blob["campaign"]
    sections = blob["sections"]
    contacts = blob["contacts"]
    ops = blob["ops"]
    templates = blob["templates"]

    profile = sections.get("client_profile") or {}
    strategy = sections.get("strategy") or {}
    playbook = sections.get("call_playbook") or {}
    caps = sections.get("capabilities") or {}

    def ready(reason: str, resolution_reason: str, review_class: str = "already_incorporated") -> dict[str, Any]:
        return {
            **base,
            "ready": True,
            "review_class": review_class,
            "reason": reason,
            "resolution_reason": resolution_reason,
        }

    def not_ready(reason: str, review_class: str = "needs_review") -> dict[str, Any]:
        return {
            **base,
            "ready": False,
            "review_class": review_class,
            "reason": reason,
            "resolution_reason": "",
        }

    # --- Profile / Setup ---
    if field == "client_name":
        if _norm_text(value) == _norm_text(setup.client_name) or _norm_text(
            value
        ) == _norm_text(setup.client_code):
            return ready(
                f"Already in Client Setup as '{setup.client_name}'.",
                "Already incorporated in Client Setup client name.",
            )

    if field == "website":
        if _norm_url(value) and _norm_url(value) == _norm_url(setup.website):
            return ready(
                f"Already in Client Setup website ({setup.website}).",
                "Already incorporated in Client Setup website.",
            )

    if field == "main_phone":
        if _norm_phone(value) and _norm_phone(value) == _norm_phone(setup.main_phone):
            return ready(
                f"Already in Client Setup phone ({setup.main_phone}).",
                "Already incorporated in Client Setup main phone.",
            )

    if field == "address":
        loc = _blank(setup.main_location)
        if loc and _contains_norm(value, loc) and len(_norm_text(value)) <= len(_norm_text(loc)) + 5:
            return ready(
                "Address already represented in Client Setup location.",
                "Already incorporated in Client Setup main location.",
            )
        # Full street address with more detail than city/state → new info
        if loc and _contains_norm(value, loc) and len(_norm_text(value)) > len(_norm_text(loc)) + 10:
            return not_ready(
                "Contains fuller street address than Client Setup location — review as New Information."
            )

    # Client contacts / decision makers
    if field in {"client_contacts", "client_contact_person", "decision_makers"}:
        from client_contacts_data import (
            analyze_proposal_incorporation,
            parse_client_contact_people,
        )

        people = parse_client_contact_people(value)
        if people:
            try:
                analysis = analyze_proposal_incorporation(
                    client_id, pid, user_id=user_id
                )
                if analysis.fully_incorporated and not analysis.unresolved_summary:
                    return ready(
                        "All extracted people are represented in structured Client Contacts.",
                        "Information incorporated into structured Client Contacts.",
                    )
                return not_ready(
                    "Unresolved Client Contact details remain: "
                    + "; ".join(analysis.unresolved_summary[:4])
                )
            except Exception:
                # Fall back: every email/name in active contacts
                active = [c for c in contacts if c.active]
                unresolved = []
                for person in people:
                    email = _blank(person.get("email")).lower()
                    name = _blank(person.get("name"))
                    hit = None
                    if email:
                        hit = next(
                            (c for c in active if c.email.lower() == email), None
                        )
                    if hit is None and name:
                        hit = next(
                            (
                                c
                                for c in active
                                if c.name.lower() == name.lower()
                            ),
                            None,
                        )
                    if hit is None:
                        unresolved.append(name or email or "?")
                if not unresolved:
                    return ready(
                        "People match structured Client Contacts.",
                        "Information incorporated into structured Client Contacts.",
                    )
                return not_ready(
                    "Missing Client Contacts for: " + ", ".join(unresolved[:5])
                )

    # Client operations-ish
    if section == "client_operations" or field in {
        "who_takes_appointments",
        "northstar_client_email",
        "northstar_revenue_specialist",
        "appointment_handling_instructions",
    }:
        blob_ops = " ".join(
            f"{it.get('title','')} {it.get('content','')}" for it in ops
        )
        if value and _contains_norm(blob_ops, value[:80]):
            return ready(
                "Already represented in approved Client Operations.",
                "Already incorporated in approved Client Operations.",
            )
        # who takes: check names
        if "josh" in value.lower() and "john" in value.lower():
            if any(
                "takes appointment" in f"{it.get('title','')}".lower()
                or "who takes" in f"{it.get('title','')}".lower()
                for it in ops
            ):
                return ready(
                    "Who Takes Appointments already approved in Client Operations.",
                    "Already incorporated in approved Client Operations (Who Takes Appointments).",
                )

    # Strategy / campaign
    if camp is not None:
        camp_map = {
            "primary_service": camp.primary_service,
            "secondary_services": camp.secondary_services,
            "target_industries": camp.target_industries,
            "ideal_customer_profile": getattr(camp, "target_customer_types", "")
            or getattr(camp, "ideal_customer_profile", ""),
            "geographic_preferences": camp.geographic_preferences,
            "positive_fit_signals": camp.positive_signals,
            "negative_fit_signals": camp.negative_signals,
            "exclusions": camp.exclusions,
            "target_titles": camp.target_titles,
            "manufacturing_processes_sought": getattr(
                camp, "manufacturing_processes_sought", ""
            ),
            "production_preference": getattr(camp, "production_preference", ""),
            "product_part_characteristics": getattr(camp, "target_products", ""),
        }
        if field in camp_map:
            existing = _blank(camp_map.get(field))
            if existing and (
                _norm_text(value) == _norm_text(existing)
                or _contains_norm(existing, value)
                or _contains_norm(value, existing)
            ):
                # If proposal is substantially longer with new tactics, treat as enrichment
                if len(_norm_text(value)) > len(_norm_text(existing)) * 1.5 + 40:
                    return not_ready(
                        "Campaign field exists but proposal adds substantial new detail — review as Enrichment."
                    )
                return ready(
                    f"Already in Client Setup campaign ({field}).",
                    f"Already incorporated in Client Setup/Strategy ({field.replace('_', ' ')}).",
                )

    # Knowledge section payloads
    section_payloads = {
        "client_profile": profile,
        "strategy": strategy,
        "call_playbook": playbook,
        "capabilities": caps,
    }
    payload = section_payloads.get(section) or {}
    if isinstance(payload, dict) and field in payload:
        existing = payload.get(field)
        existing_s = (
            existing
            if isinstance(existing, str)
            else json.dumps(existing, ensure_ascii=False)
        )
        if _blank(existing_s) and (
            _norm_text(value) == _norm_text(existing_s)
            or _contains_norm(str(existing_s), value)
        ):
            return ready(
                f"Already in knowledge section {section}.{field}.",
                f"Already incorporated in {section.replace('_', ' ')} ({field.replace('_', ' ')}).",
            )

    # Capabilities mirrored in setup sells / equipment_capacity
    if section == "capabilities" or field in {
        "capacity",
        "equipment",
        "materials",
        "facility",
        "production_processes",
        "logistics",
        "certifications",
    }:
        equip = _blank(getattr(setup, "equipment_capacity", "") or "")
        secondary = ""
        if camp is not None:
            secondary = _blank(camp.secondary_services)
        primary = _blank(camp.primary_service) if camp else ""
        sell_blob = f"{equip} {secondary} {primary} {_section_text(sections, 'capabilities')}"

        if field in {"capacity", "equipment", "materials"} and equip and (
            _capacity_represented(equip, value) or _contains_norm(equip, value)
        ):
            return ready(
                "Already represented in Client Setup equipment/capacity.",
                "Information already incorporated in approved Client Setup/Capabilities.",
            )
        if field == "primary_service" and primary and _contains_norm(primary, value):
            return ready(
                "Already in campaign primary service.",
                "Already incorporated in Client Setup/Strategy primary service.",
            )
        if field == "secondary_services" and secondary:
            # exact list overlap for short values
            if _norm_text(value) in _norm_text(secondary) or _contains_norm(
                secondary, value
            ):
                if len(_norm_text(value)) < len(_norm_text(secondary)) * 0.8 + 20:
                    return ready(
                        "Already covered by campaign secondary services.",
                        "Already incorporated in Client Setup/Strategy secondary services.",
                    )
        if field == "logistics" and (
            _contains_norm(secondary, value) or _contains_norm(sell_blob, value)
        ):
            # "Own OTR trucks" may only be in pending siblings — require blob match
            if _contains_norm(sell_blob, value):
                return ready(
                    "Logistics already represented in stored capabilities/services.",
                    "Information already incorporated in approved Client Setup/Capabilities.",
                )
        if field == "production_processes" and _contains_norm(sell_blob, value[:60]):
            # Only if substantial overlap with stored — short unique narratives stay needs_review
            overlap_tokens = [
                t
                for t in re.findall(r"[a-z]{4,}", _norm_text(value))
                if t in _norm_text(sell_blob)
            ]
            if len(overlap_tokens) >= 8 and len(_norm_text(value)) < 200:
                return ready(
                    "Production processes already reflected in stored capabilities/services.",
                    "Information already incorporated in approved Client Setup/Capabilities.",
                )

    # Fit signals
    if field in {"positive_fit_signals", "negative_fit_signals"} and camp is not None:
        stored = (
            _blank(camp.positive_signals)
            if field == "positive_fit_signals"
            else _blank(camp.negative_signals)
        )
        if stored:
            soft_match = (
                _contains_norm(stored, value)
                or _contains_norm(value, stored)
                or _significant_token_overlap(stored, value, min_hits=3)
            )
            # Negative: informal "no ongoing/frequent needs" ≈ stored "no recurring/frequent…"
            if field == "negative_fit_signals" and not soft_match:
                pv = _norm_text(value)
                sv = _norm_text(stored)
                neg_cues = (
                    ("frequent" in pv and "frequent" in sv)
                    or ("ongoing" in pv and ("recurring" in sv or "ongoing" in sv))
                    or ("one-off" in pv and "one-off" in sv)
                    or ("one off" in pv and "one-off" in sv)
                )
                soft_match = bool(neg_cues and ("need" in pv or "requirement" in sv))
            if soft_match:
                if len(_norm_text(value)) < 20 and not _contains_norm(stored, value):
                    return not_ready(
                        "Fit signal phrasing is too vague to confirm against stored campaign signals."
                    )
                label = (
                    "positive fit signals"
                    if field == "positive_fit_signals"
                    else "negative fit signals"
                )
                return ready(
                    f"Already represented in campaign {label}.",
                    f"Already incorporated in Client Setup/Strategy {label}.",
                )

    # Fit signals misfiled as ICP — if value matches stored signals
    if field == "ideal_customer_profile" and camp is not None:
        sig = f"{camp.positive_signals} {camp.negative_signals}"
        if _contains_norm(sig, value[:100]) or _contains_norm(
            value, camp.positive_signals[:60]
        ):
            return ready(
                "Content already stored as campaign fit signals (misfiled as ICP).",
                "Already incorporated in Client Setup/Strategy fit signals.",
            )

    # Email templates
    if section == "email_templates" and templates:
        for t in templates:
            if field == "template_name" and _norm_text(value) == _norm_text(
                t.template_name
            ):
                return ready(
                    f"Email template '{t.template_name}' already stored.",
                    "Already incorporated in Email Templates.",
                )
            if field == "body" and _contains_norm(t.body, value[:80]):
                return ready(
                    "Email template body already stored.",
                    "Already incorporated in Email Templates.",
                )

    # Geographic soft preference
    if field == "geographic_preferences" and camp is not None:
        geo = _blank(camp.geographic_preferences)
        if geo and (
            "600" in value and "600" in geo
            or _contains_norm(geo, value)
            or _contains_norm(value, geo)
        ):
            return ready(
                "Geographic preference already on campaign.",
                "Already incorporated in Client Setup/Strategy geographic preferences.",
            )

    return not_ready(
        "Not independently represented in approved/stored knowledge — keep for review."
    )


def classify_pending_proposals_for_review(
    client_id: int, *, user_id: int | None = None
) -> list[dict[str, Any]]:
    """Classify all Pending proposals for smart filters (read-only)."""
    from client_knowledge_data import list_extraction_proposals

    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")
    pending = list_extraction_proposals(
        client_id, user_id=user.id, status="Pending"
    )
    blob = _knowledge_blob(client_id, user.id)
    out: list[dict[str, Any]] = []
    for p in pending:
        ev = evaluate_proposal_resolve_eligibility(
            client_id, p, blob=blob, user_id=user.id
        )
        out.append(ev)
    return out


def preview_bulk_resolve(
    client_id: int,
    proposal_ids: list[int],
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Preview which selected proposals are READY to resolve. No writes."""
    from client_knowledge_data import list_extraction_proposals

    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_edit_client_setup(user.id, client_id) and not getattr(
        user, "is_administrator", False
    ):
        # still allow preview for viewers? require edit for consistency
        pass

    all_p = list_extraction_proposals(client_id, user_id=user.id, status="All")
    by_id = {p.proposal_id: p for p in all_p}
    blob = _knowledge_blob(client_id, user.id)

    ready: list[dict[str, Any]] = []
    not_ready: list[dict[str, Any]] = []
    for pid in proposal_ids:
        p = by_id.get(int(pid))
        if p is None:
            not_ready.append(
                {
                    "proposal_id": int(pid),
                    "ready": False,
                    "review_class": "needs_review",
                    "reason": "Proposal not found for this client.",
                    "resolution_reason": "",
                    "field_label": "",
                }
            )
            continue
        ev = evaluate_proposal_resolve_eligibility(
            client_id, p, blob=blob, user_id=user.id
        )
        if ev["ready"]:
            ready.append(ev)
        else:
            not_ready.append(ev)

    return {
        "selected_count": len(proposal_ids),
        "eligible_count": len(ready),
        "not_eligible_count": len(not_ready),
        "eligible": ready,
        "not_eligible": not_ready,
        "message": (
            f"{len(ready)} eligible to Resolve / Incorporated; "
            f"{len(not_ready)} not eligible and will remain Pending."
        ),
    }


def bulk_resolve_eligible_proposals(
    client_id: int,
    proposal_ids: list[int],
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Re-verify then resolve ONLY ready proposals. Skips the rest."""
    from client_knowledge_data import resolve_extraction_proposal
    from models import ClientExtractionResolveRequest

    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit this client.")

    preview = preview_bulk_resolve(client_id, proposal_ids, user_id=user.id)
    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = list(preview["not_eligible"])
    errors: list[dict[str, Any]] = []

    for item in preview["eligible"]:
        pid = int(item["proposal_id"])
        reason = _blank(item.get("resolution_reason")) or _blank(item.get("reason"))
        try:
            view = resolve_extraction_proposal(
                client_id,
                pid,
                ClientExtractionResolveRequest(resolution_reason=reason),
                user_id=user.id,
            )
            resolved.append(
                {
                    "proposal_id": pid,
                    "status": view.status,
                    "resolution_reason": view.resolution_reason,
                }
            )
        except Exception as exc:
            errors.append({"proposal_id": pid, "error": str(exc)})

    return {
        "resolved_count": len(resolved),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "resolved": resolved,
        "skipped": skipped,
        "errors": errors,
        "message": (
            f"{len(resolved)} proposals resolved · "
            f"{len(skipped)} skipped — review required · "
            f"{len(errors)} errors"
        ),
    }


def bulk_reject_proposals(
    client_id: int,
    proposal_ids: list[int],
    *,
    rejection_category: str,
    rejection_note: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    """Reject selected Pending proposals with a required category. No Setup/CRM writes."""
    from client_knowledge_data import _now

    user = resolve_staff_actor(user_id)
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit this client.")

    category = _blank(rejection_category)
    allowed = {
        "Noise / Bad Extraction",
        "Sensitive — Do Not Import",
        "Duplicate Invalid Extraction",
        "Other",
    }
    if category not in allowed:
        raise ValueError(
            "rejection_category must be one of: " + "; ".join(sorted(allowed))
        )

    note = _blank(rejection_note)
    # Never echo secret values for sensitive
    if category.startswith("Sensitive"):
        reason = f"Rejected: {category}."
        if note and not re.search(
            r"(?i)password|passwd|secret|token|api[_-]?key", note
        ):
            reason = f"Rejected: {category}. {note}"
    else:
        reason = f"Rejected: {category}." + (f" {note}" if note else "")

    now = _now()
    user_name = _blank(user.full_name) or _blank(user.email)
    rejected: list[int] = []
    skipped: list[dict[str, Any]] = []

    with get_connection() as conn:
        for pid in proposal_ids:
            row = conn.execute(
                """
                SELECT id, status, classification, field_name, proposed_value
                FROM client_extraction_proposals
                WHERE id = ? AND client_id = ?
                """,
                (int(pid), client_id),
            ).fetchone()
            if not row:
                skipped.append({"proposal_id": int(pid), "reason": "Not found."})
                continue
            if _blank(row["status"]) != "Pending":
                skipped.append(
                    {
                        "proposal_id": int(pid),
                        "reason": f"Status is {row['status']}, not Pending.",
                    }
                )
                continue
            conn.execute(
                """
                UPDATE client_extraction_proposals
                SET status = 'Rejected',
                    resolution_reason = ?,
                    resolved_at = ?,
                    resolved_by_user_id = ?,
                    resolved_by_name = ?,
                    reviewed_by_user_id = ?,
                    reviewed_by_name = ?,
                    reviewed_at = ?,
                    updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    reason[:500],
                    now,
                    user.id,
                    user_name,
                    user.id,
                    user_name,
                    now,
                    now,
                    int(pid),
                    client_id,
                ),
            )
            rejected.append(int(pid))
        conn.commit()

    return {
        "rejected_count": len(rejected),
        "skipped_count": len(skipped),
        "rejected_ids": rejected,
        "skipped": skipped,
        "rejection_category": category,
        "message": (
            f"{len(rejected)} proposals rejected · {len(skipped)} skipped"
        ),
    }
