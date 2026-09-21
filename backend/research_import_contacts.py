"""Contact planning for Research & Custom Prospect Import.

Reuses crm_add_data.match_contact and contact_phone last-7/10-digit keys.
Never merges a contact across companies.
"""
from __future__ import annotations

import re
from typing import Any

from crm_add_data import match_contact
from models import CrmAddContactInput
from research_import_mapping import blank, is_department_only_name
from research_import_policy import (
    CONTACT_AMBIGUOUS,
    CONTACT_EXACT,
    CONTACT_INVALID,
    CONTACT_NEW,
    CONTACT_POSSIBLE,
    CONTACT_STRONG,
    RES_CREATE_CONTACT,
    RES_SKIP_CONTACT,
    RES_USE_CONTACT,
    contact_actions_for,
)


def _norm_person_name(value: str) -> str:
    text = blank(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _norm_note(value: str) -> str:
    return re.sub(r"\s+", " ", blank(value).casefold())


def note_already_present(existing: str, incoming: str) -> bool:
    needle = _norm_note(incoming)
    if not needle:
        return True
    return needle in _norm_note(existing)


def classify_research_contact(
    conn,
    *,
    company_id: int | None,
    incoming: dict[str, str],
    resolution: str = "",
) -> dict[str, Any]:
    first = blank(incoming.get("first_name"))
    last = blank(incoming.get("last_name"))
    full = blank(incoming.get("full_name")) or f"{first} {last}".strip()
    title = blank(incoming.get("title"))
    email = blank(incoming.get("email"))
    phone = blank(incoming.get("phone"))
    extension = blank(incoming.get("phone_extension"))
    base = {
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "title": title,
        "email": email,
        "phone": phone,
        "phone_extension": extension,
        "incoming": {
            "first_name": first,
            "last_name": last,
            "full_name": full,
            "title": title,
            "email": email,
            "phone": phone,
            "phone_extension": extension,
        },
        "contact_class": CONTACT_INVALID,
        "matched_contact_id": None,
        "matched_name": "",
        "matched_email": "",
        "matched_phone": "",
        "matched_title": "",
        "matched_company_id": company_id,
        "matched_company_name": "",
        "reasons": [],
        "allowed_resolutions": [RES_SKIP_CONTACT],
        "resolution": blank(resolution).upper(),
        "blocking": False,
        "blocking_reason": "",
        "cross_company": False,
        "other_company_hits": [],
        "confidence": "",
    }
    if not full or is_department_only_name(full) or is_department_only_name(first) or is_department_only_name(last):
        base["reasons"] = ["department_or_missing_name"]
        base["blocking"] = False
        return base

    other = _other_company_name_hits(conn, company_id, full)
    base["other_company_hits"] = other

    if company_id:
        payload = CrmAddContactInput(
            first_name=first,
            last_name=last,
            full_name=full,
            title=title,
            email=email,
            phone=phone,
        )
        matched = match_contact(conn, int(company_id), payload)
        status = matched.get("status") or "new"
        reasons = list(matched.get("reasons") or [])
        confidence = matched.get("confidence") or ""
        contact_id = matched.get("contact_id")
        if contact_id:
            detail = conn.execute(
                "SELECT first_name, last_name, title, email, phone, company_id FROM contacts WHERE id=?",
                (int(contact_id),),
            ).fetchone()
            if detail is not None:
                base["matched_name"] = f"{blank(detail['first_name'])} {blank(detail['last_name'])}".strip()
                base["matched_email"] = blank(detail["email"])
                base["matched_phone"] = blank(detail["phone"])
                base["matched_title"] = blank(detail["title"])
                base["matched_company_id"] = int(detail["company_id"]) if detail["company_id"] is not None else company_id
        base["matched_contact_id"] = int(contact_id) if contact_id else None
        base["reasons"] = reasons
        base["confidence"] = confidence
        if company_id:
            name_row = conn.execute(
                "SELECT company_name FROM companies WHERE id=?",
                (int(company_id),),
            ).fetchone()
            if name_row is not None:
                base["matched_company_name"] = blank(name_row["company_name"])
        if status == "existing":
            if "email_exact" in reasons or "phone_exact" in reasons:
                klass = CONTACT_EXACT
            else:
                klass = CONTACT_STRONG
            base["contact_class"] = klass
            base["allowed_resolutions"] = contact_actions_for(klass)
        elif status == "possible_match":
            base["contact_class"] = CONTACT_POSSIBLE
            base["allowed_resolutions"] = contact_actions_for(CONTACT_POSSIBLE)
            if matched.get("possibles_count", 1) > 1:
                base["contact_class"] = CONTACT_AMBIGUOUS
                base["allowed_resolutions"] = contact_actions_for(CONTACT_AMBIGUOUS)
        else:
            if other:
                base["contact_class"] = CONTACT_POSSIBLE
                base["cross_company"] = True
                base["reasons"] = reasons + ["name_other_company"]
                base["allowed_resolutions"] = contact_actions_for(CONTACT_POSSIBLE, cross_company=True)
            else:
                base["contact_class"] = CONTACT_NEW
                base["allowed_resolutions"] = contact_actions_for(CONTACT_NEW)
    elif other:
        base["contact_class"] = CONTACT_POSSIBLE
        base["cross_company"] = True
        base["reasons"] = ["name_other_company"]
        base["allowed_resolutions"] = contact_actions_for(CONTACT_POSSIBLE, cross_company=True)
        base["matched_contact_id"] = other[0].get("contact_id")
        base["matched_name"] = other[0].get("name") or ""
        base["matched_company_id"] = other[0].get("company_id")
        base["matched_company_name"] = other[0].get("company_name") or ""
    else:
        base["contact_class"] = CONTACT_NEW
        base["allowed_resolutions"] = contact_actions_for(CONTACT_NEW)

    return _apply_contact_resolution(base)


def _other_company_name_hits(conn, company_id: int | None, full_name: str) -> list[dict[str, Any]]:
    want = _norm_person_name(full_name)
    if not want:
        return []
    parts = want.split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    hits: list[dict[str, Any]] = []
    sql = """
        SELECT c.id, c.company_id, c.first_name, c.last_name, c.email, c.phone, co.company_name
        FROM contacts c
        JOIN companies co ON co.id = c.company_id
        WHERE (? IS NULL OR c.company_id != ?)
    """
    params: list[Any] = [int(company_id) if company_id else None, int(company_id) if company_id else -1]
    if first:
        sql += " AND lower(trim(c.first_name)) = ?"
        params.append(first)
    if last:
        sql += " AND lower(trim(c.last_name)) = ?"
        params.append(last)
    for row in conn.execute(sql, params):
        existing = _norm_person_name(f"{blank(row['first_name'])} {blank(row['last_name'])}")
        if existing != want:
            continue
        hits.append(
            {
                "contact_id": int(row["id"]),
                "company_id": int(row["company_id"]),
                "company_name": blank(row["company_name"]),
                "name": f"{blank(row['first_name'])} {blank(row['last_name'])}".strip(),
                "email": blank(row["email"]),
                "phone": blank(row["phone"]),
                "reason": "name_other_company",
            }
        )
        if len(hits) >= 5:
            break
    return hits


def _apply_contact_resolution(plan: dict[str, Any]) -> dict[str, Any]:
    res = blank(plan.get("resolution")).upper()
    allowed = plan.get("allowed_resolutions") or []
    klass = plan.get("contact_class")
    if res and res not in allowed:
        plan["blocking"] = True
        plan["blocking_reason"] = f"Invalid contact resolution {res}."
        return plan
    if klass in {CONTACT_POSSIBLE, CONTACT_AMBIGUOUS} and res not in {
        RES_USE_CONTACT,
        RES_CREATE_CONTACT,
        RES_SKIP_CONTACT,
    }:
        plan["blocking"] = True
        plan["blocking_reason"] = klass
        return plan
    if klass in {CONTACT_EXACT, CONTACT_STRONG} and not res:
        plan["resolution"] = RES_USE_CONTACT
    if klass == CONTACT_NEW and not res:
        plan["resolution"] = RES_CREATE_CONTACT
    if klass == CONTACT_INVALID:
        plan["resolution"] = RES_SKIP_CONTACT
    if plan.get("cross_company") and res == RES_USE_CONTACT:
        plan["blocking"] = True
        plan["blocking_reason"] = "Cannot reuse a contact from a different company."
    return plan
