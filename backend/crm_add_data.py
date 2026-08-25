"""Shared runtime CRM add/link service for AI Research + future Add Company.

Preview is read-only. Confirm creates/links only after explicit user decisions.
Does not overwrite nonblank existing company/contact fields.
Does not use Client Knowledge contacts.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from access import get_default_user, get_user_by_id, user_can_access_client
from activities_data import create_activity
from db import get_connection
from import_brown_industries import digits_phone, domain, norm_addr, norm_name
from models import (
    ActivityCreateRequest,
    CrmAddCompanyInput,
    CrmAddConfirmRequest,
    CrmAddConfirmResult,
    CrmAddContactInput,
    CrmAddContactPreview,
    CrmAddFieldDiff,
    CrmAddPreviewRequest,
    CrmAddPreviewResponse,
    CrmAddRelationshipPreview,
)

NS_PREFIX = "NS-"
NS_START = 100001


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _norm_email(value: str) -> str:
    return _blank(value).lower()


def _norm_person_name(value: str) -> str:
    s = _blank(value).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in _blank(full_name).split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _resolve_contact_names(c: CrmAddContactInput) -> tuple[str, str, str]:
    first = _blank(c.first_name)
    last = _blank(c.last_name)
    full = _blank(c.full_name)
    if not first and not last and full:
        first, last = _split_full_name(full)
    if not full:
        full = f"{first} {last}".strip()
    return first, last, full


def _require_client_access(user_id: int, client_id: int) -> None:
    user = get_user_by_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    if user.is_administrator:
        return
    if not user_can_access_client(user_id, client_id):
        raise PermissionError("Not authorized for this client.")


def allocate_ns_record_no(conn) -> str:
    """Server-only sequential NS-* generator. Caller must hold a write lock."""
    row = conn.execute(
        """
        SELECT external_record_no FROM companies
        WHERE external_record_no GLOB 'NS-[0-9]*'
        ORDER BY CAST(substr(external_record_no, 4) AS INTEGER) DESC
        LIMIT 1
        """
    ).fetchone()
    nxt = NS_START
    if row:
        raw = _blank(row["external_record_no"])
        try:
            nxt = max(NS_START, int(raw[3:]) + 1)
        except ValueError:
            nxt = NS_START
    candidate = f"{NS_PREFIX}{nxt}"
    while True:
        hit = conn.execute(
            "SELECT 1 FROM companies WHERE external_record_no = ? LIMIT 1",
            (candidate,),
        ).fetchone()
        if hit is None:
            return candidate
        nxt += 1
        candidate = f"{NS_PREFIX}{nxt}"


def match_company(
    conn,
    company: CrmAddCompanyInput,
    *,
    trusted_record_no: str | None = None,
    known_company_id: int | None = None,
) -> dict[str, Any]:
    if known_company_id is not None and known_company_id > 0:
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no, website, address, city, state, zip,
                   legacy_phone, type_of_industry
            FROM companies WHERE id = ?
            """,
            (known_company_id,),
        ).fetchone()
        if row is None:
            raise LookupError("known_company_id not found.")
        return {
            "match_type": "existing_company",
            "company_id": int(row["id"]),
            "external_record_no": _blank(row["external_record_no"]),
            "company_name": _blank(row["company_name"]),
            "confidence": "high",
            "reasons": ["known_company_id"],
            "row": row,
            "possibles": [],
        }

    rn = _blank(trusted_record_no)
    if rn:
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no, website, address, city, state, zip,
                   legacy_phone, type_of_industry
            FROM companies WHERE TRIM(external_record_no) = ?
            LIMIT 1
            """,
            (rn,),
        ).fetchone()
        if row:
            return {
                "match_type": "existing_company",
                "company_id": int(row["id"]),
                "external_record_no": _blank(row["external_record_no"]),
                "company_name": _blank(row["company_name"]),
                "confidence": "high",
                "reasons": ["external_record_no"],
                "row": row,
                "possibles": [],
            }

    name = _blank(company.company_name)
    b_norm = norm_name(name)
    b_dom = domain(_blank(company.website))
    b_phone = digits_phone(_blank(company.phone))
    b_addr = norm_addr(_blank(company.address))
    b_city = _blank(company.city).lower()
    b_state = _blank(company.state).upper()

    candidates: dict[int, tuple[Any, list[str]]] = {}

    def _add(row, reasons: list[str]) -> None:
        hid = int(row["id"])
        if hid in candidates:
            prev = candidates[hid][1]
            candidates[hid] = (row, list(dict.fromkeys(prev + reasons)))
        else:
            candidates[hid] = (row, reasons)

    if b_dom:
        for r in conn.execute(
            """
            SELECT id, company_name, external_record_no, website, address, city, state, zip,
                   legacy_phone, type_of_industry
            FROM companies
            WHERE lower(trim(website)) LIKE ?
            """,
            (f"%{b_dom}%",),
        ).fetchall():
            if domain(_blank(r["website"])) == b_dom:
                _add(r, ["domain_exact"])

    if b_norm:
        for r in conn.execute(
            """
            SELECT id, company_name, external_record_no, website, address, city, state, zip,
                   legacy_phone, type_of_industry
            FROM companies
            """
        ).fetchall():
            if norm_name(_blank(r["company_name"])) == b_norm:
                _add(r, ["name_exact"])

    if b_phone and len(b_phone) >= 7:
        for r in conn.execute(
            """
            SELECT id, company_name, external_record_no, website, address, city, state, zip,
                   legacy_phone, type_of_industry
            FROM companies
            WHERE trim(coalesce(legacy_phone,'')) != ''
            """
        ).fetchall():
            cph = digits_phone(_blank(r["legacy_phone"]))
            if cph and (
                cph == b_phone
                or cph.endswith(b_phone[-7:])
                or b_phone.endswith(cph[-7:])
            ):
                _add(r, ["phone"])

    if b_addr and b_city and b_state:
        for row, _reasons in list(candidates.values()):
            if (
                norm_addr(_blank(row["address"])) == b_addr
                and _blank(row["city"]).lower() == b_city
                and _blank(row["state"]).upper() == b_state
            ):
                _add(row, ["address_city_state"])

    high: list[tuple[Any, list[str]]] = []
    possible: list[tuple[Any, list[str]]] = []
    for row, reasons in candidates.values():
        if "domain_exact" in reasons:
            high.append((row, reasons))
        elif "name_exact" in reasons and (
            "phone" in reasons
            or "address_city_state" in reasons
            or "domain_exact" in reasons
        ):
            high.append((row, reasons))
        elif "name_exact" in reasons and len(reasons) == 1:
            name_only = [x for x in candidates.values() if "name_exact" in x[1]]
            if len(name_only) == 1:
                high.append((row, reasons))
            else:
                possible.append((row, reasons))
        elif reasons:
            possible.append((row, reasons))

    def _pack_possible(items: list[tuple[Any, list[str]]]) -> list[dict[str, Any]]:
        return [
            {
                "company_id": int(r["id"]),
                "external_record_no": _blank(r["external_record_no"]),
                "company_name": _blank(r["company_name"]),
                "reasons": rs,
            }
            for r, rs in items[:5]
        ]

    if high:
        def _score(item: tuple[Any, list[str]]) -> int:
            rs = item[1]
            return (
                ("domain_exact" in rs) * 100
                + ("name_exact" in rs) * 40
                + ("phone" in rs) * 20
                + ("address_city_state" in rs) * 15
                + len(rs)
            )

        high.sort(key=_score, reverse=True)
        best_row, best_reasons = high[0]
        if len(high) > 1 and _score(high[1]) >= _score(high[0]) - 20:
            return {
                "match_type": "possible_match",
                "company_id": int(best_row["id"]),
                "external_record_no": _blank(best_row["external_record_no"]),
                "company_name": _blank(best_row["company_name"]),
                "confidence": "medium",
                "reasons": best_reasons,
                "row": best_row,
                "possibles": _pack_possible(high),
            }
        return {
            "match_type": "existing_company",
            "company_id": int(best_row["id"]),
            "external_record_no": _blank(best_row["external_record_no"]),
            "company_name": _blank(best_row["company_name"]),
            "confidence": "high",
            "reasons": best_reasons,
            "row": best_row,
            "possibles": _pack_possible(possible),
        }

    if possible:
        row, reasons = possible[0]
        return {
            "match_type": "possible_match",
            "company_id": int(row["id"]),
            "external_record_no": _blank(row["external_record_no"]),
            "company_name": _blank(row["company_name"]),
            "confidence": "low",
            "reasons": reasons,
            "row": row,
            "possibles": _pack_possible(possible),
        }

    return {
        "match_type": "new_company",
        "company_id": None,
        "external_record_no": "",
        "company_name": name,
        "confidence": "high",
        "reasons": [],
        "row": None,
        "possibles": [],
    }


def match_contact(conn, company_id: int, contact: CrmAddContactInput) -> dict[str, Any]:
    first, last, full = _resolve_contact_names(contact)
    email = _norm_email(contact.email)
    phone = digits_phone(_blank(contact.phone))
    title = _blank(contact.title)
    norm = _norm_person_name(full)

    if email:
        row = conn.execute(
            """
            SELECT id, first_name, last_name, title, phone, email
            FROM contacts
            WHERE company_id = ? AND lower(trim(email)) = ?
            LIMIT 1
            """,
            (company_id, email),
        ).fetchone()
        if row:
            return {
                "status": "existing",
                "contact_id": int(row["id"]),
                "matched_name": f"{_blank(row['first_name'])} {_blank(row['last_name'])}".strip(),
                "reasons": ["email_exact"],
                "confidence": "high",
            }

    if phone and len(phone) >= 7:
        for r in conn.execute(
            """
            SELECT id, first_name, last_name, title, phone, alt_phone, email
            FROM contacts WHERE company_id = ?
            """,
            (company_id,),
        ).fetchall():
            for field in ("phone", "alt_phone"):
                digits = digits_phone(_blank(r[field]))
                if digits and (
                    digits.endswith(phone[-7:]) or phone.endswith(digits[-7:])
                ):
                    return {
                        "status": "existing",
                        "contact_id": int(r["id"]),
                        "matched_name": f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
                        "reasons": ["phone"],
                        "confidence": "high",
                    }

    possibles: list[tuple[Any, list[str]]] = []
    if norm:
        for r in conn.execute(
            """
            SELECT id, first_name, last_name, title, phone, email
            FROM contacts WHERE company_id = ?
            """,
            (company_id,),
        ).fetchall():
            full_existing = _norm_person_name(
                f"{_blank(r['first_name'])} {_blank(r['last_name'])}"
            )
            if full_existing == norm:
                return {
                    "status": "existing",
                    "contact_id": int(r["id"]),
                    "matched_name": f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
                    "reasons": ["name_exact"],
                    "confidence": "high",
                }
            if full_existing and (norm in full_existing or full_existing in norm):
                possibles.append((r, ["name_partial"]))
            elif title and _norm_person_name(title) == _norm_person_name(
                _blank(r["title"])
            ):
                if norm.split() and norm.split()[0] in full_existing:
                    possibles.append((r, ["name_title"]))

    if possibles:
        r, reasons = possibles[0]
        return {
            "status": "possible_match",
            "contact_id": int(r["id"]),
            "matched_name": f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
            "reasons": reasons,
            "confidence": "medium" if len(possibles) == 1 else "low",
            "possibles_count": len(possibles),
        }

    return {
        "status": "new",
        "contact_id": None,
        "matched_name": full,
        "reasons": [],
        "confidence": "high",
    }


def _company_field_diffs(row, company: CrmAddCompanyInput) -> list[CrmAddFieldDiff]:
    mapping = [
        ("company_name", "company_name", company.company_name),
        ("website", "website", company.website),
        ("address", "address", company.address),
        ("city", "city", company.city),
        ("state", "state", company.state),
        ("zip", "zip", company.zip),
        ("legacy_phone", "phone", company.phone),
        ("type_of_industry", "industry", company.industry),
    ]
    diffs: list[CrmAddFieldDiff] = []
    for col, label, proposed in mapping:
        existing = _blank(row[col]) if row is not None else ""
        prop = _blank(proposed)
        if not prop and not existing:
            continue
        if existing and prop and existing.lower() != prop.lower():
            action = "preserve_existing"
        elif existing and not prop:
            action = "already_populated"
        elif not existing and prop:
            action = (
                "would_set_on_create" if row is None else "would_fill_blank_only_if_explicit"
            )
        else:
            action = "same"
        diffs.append(
            CrmAddFieldDiff(
                field=label,
                existing_value=existing,
                proposed_value=prop,
                action=action,
            )
        )
    return diffs


def _relationship_preview(
    conn, client_id: int, company_id: int | None
) -> CrmAddRelationshipPreview:
    if company_id is None:
        return CrmAddRelationshipPreview(
            exists=False,
            would_create=True,
            status_if_create="New",
            message="Would create Working For relationship with status New.",
        )
    row = conn.execute(
        """
        SELECT id, status FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (client_id, company_id),
    ).fetchone()
    if row is None:
        return CrmAddRelationshipPreview(
            exists=False,
            would_create=True,
            status_if_create="New",
            message="Would create Working For relationship with status New.",
        )
    return CrmAddRelationshipPreview(
        exists=True,
        relationship_id=int(row["id"]),
        current_status=_blank(row["status"]) or "New",
        would_create=False,
        status_if_create="",
        message="Existing relationship preserved (status not reset).",
    )


def preview_company_contact_add(
    body: CrmAddPreviewRequest,
    *,
    user_id: int | None = None,
) -> CrmAddPreviewResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    client_id = int(body.client_id)
    _require_client_access(user.id, client_id)
    company = body.company
    if not _blank(company.company_name) and body.known_company_id is None:
        raise ValueError("company_name is required.")

    warnings: list[str] = []
    if not (
        _blank(company.website)
        or _blank(company.address)
        or _blank(company.city)
        or _blank(company.phone)
    ):
        warnings.append(
            "Company name is the only identifying field — prefer website, address, or phone before create."
        )

    with get_connection() as conn:
        cl = conn.execute(
            "SELECT id, name, code FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not cl:
            raise LookupError("Client not found.")

        match = match_company(
            conn,
            company,
            trusted_record_no=body.trusted_external_record_no,
            known_company_id=body.known_company_id,
        )
        row = match.get("row")
        company_id = match.get("company_id")

        contact_previews: list[CrmAddContactPreview] = []
        for idx, c in enumerate(body.contacts or []):
            first, last, full = _resolve_contact_names(c)
            if not full and not _norm_email(c.email):
                warnings.append(
                    f"Contact #{idx + 1} skipped in preview — needs name or email."
                )
                continue
            if company_id:
                cm = match_contact(conn, int(company_id), c)
                contact_previews.append(
                    CrmAddContactPreview(
                        index=idx,
                        first_name=first,
                        last_name=last,
                        full_name=full,
                        title=_blank(c.title),
                        email=_blank(c.email),
                        phone=_blank(c.phone),
                        linkedin=_blank(c.linkedin),
                        source_url=_blank(c.source_url),
                        match_status=cm["status"],
                        matched_contact_id=cm.get("contact_id"),
                        matched_contact_name=_blank(cm.get("matched_name")),
                        match_reasons=list(cm.get("reasons") or []),
                        confidence=_blank(cm.get("confidence")) or "medium",
                    )
                )
            else:
                contact_previews.append(
                    CrmAddContactPreview(
                        index=idx,
                        first_name=first,
                        last_name=last,
                        full_name=full,
                        title=_blank(c.title),
                        email=_blank(c.email),
                        phone=_blank(c.phone),
                        linkedin=_blank(c.linkedin),
                        source_url=_blank(c.source_url),
                        match_status="new",
                        match_reasons=[],
                        confidence="high",
                    )
                )

        return CrmAddPreviewResponse(
            client_id=client_id,
            client_name=_blank(cl["name"]) or _blank(cl["code"]),
            company_match_type=match["match_type"],
            company_confidence=match["confidence"],
            company_match_reasons=list(match.get("reasons") or []),
            matched_company_id=company_id,
            matched_external_record_no=_blank(match.get("external_record_no")),
            matched_company_name=_blank(match.get("company_name")),
            proposed_company=company,
            company_field_diffs=_company_field_diffs(row, company),
            possible_company_matches=list(match.get("possibles") or []),
            contacts=contact_previews,
            relationship=_relationship_preview(conn, client_id, company_id),
            warnings=warnings,
            research_run_id=body.research_run_id,
            source=_blank(body.source),
            provider=_blank(body.provider),
        )


def ensure_client_relationship(
    conn,
    *,
    client_id: int,
    company_id: int,
    external_record_no: str,
    user_id: int,
    provenance_note: str,
) -> tuple[int, bool, str]:
    existing = conn.execute(
        """
        SELECT id, status, notes FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (client_id, company_id),
    ).fetchone()
    now = _now()
    if existing is not None:
        notes = _blank(existing["notes"])
        if not notes and provenance_note:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (provenance_note[:500], now, int(existing["id"])),
            )
        return int(existing["id"]), False, _blank(existing["status"]) or "New"

    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, status, assigned_user_id,
            priority, next_action, notes, is_hot, created_at, updated_at,
            external_record_no
        ) VALUES (?, ?, 'New', ?, '', ?, ?, 0, ?, ?, ?)
        """,
        (
            client_id,
            company_id,
            user_id,
            "Initial outreach",
            provenance_note[:500],
            now,
            now,
            external_record_no,
        ),
    )
    return int(cur.lastrowid), True, "New"


def create_or_link_company(
    conn,
    *,
    company: CrmAddCompanyInput,
    decision: str,
    existing_company_id: int | None,
    match: dict[str, Any],
) -> tuple[int, str, bool]:
    decision_key = _blank(decision).lower() or "auto"
    match_type = match["match_type"]

    if match_type == "possible_match" and decision_key not in {
        "use_existing",
        "create_new",
    }:
        raise ValueError(
            "Ambiguous company match requires explicit decision: use_existing or create_new."
        )

    if decision_key == "use_existing" or (
        decision_key == "auto" and match_type == "existing_company"
    ):
        cid = existing_company_id or match.get("company_id")
        if not cid:
            raise ValueError("use_existing requires a matched company.")
        row = conn.execute(
            "SELECT id, external_record_no FROM companies WHERE id = ?",
            (int(cid),),
        ).fetchone()
        if row is None:
            raise LookupError("Existing company not found.")
        return int(row["id"]), _blank(row["external_record_no"]), False

    if decision_key == "create_new" or (
        decision_key == "auto" and match_type == "new_company"
    ):
        name = _blank(company.company_name)
        if not name:
            raise ValueError("company_name is required to create a company.")
        rn = allocate_ns_record_no(conn)
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO companies (
                external_record_no, company_name, address, city, state, zip, website,
                legacy_phone, type_of_industry, created_at, last_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rn,
                name,
                _blank(company.address),
                _blank(company.city),
                _blank(company.state),
                _blank(company.zip),
                _blank(company.website),
                _blank(company.phone),
                _blank(company.industry),
                now,
                now,
            ),
        )
        return int(cur.lastrowid), rn, True

    raise ValueError(f"Unsupported company_decision: {decision}")


def create_or_link_contact(
    conn,
    *,
    company_id: int,
    external_record_no: str,
    contact: CrmAddContactInput,
    decision: str,
    matched_contact_id: int | None,
) -> tuple[int | None, str, bool]:
    decision_key = _blank(decision).lower() or "auto"
    if decision_key in {"skip", "omit"}:
        return None, "skipped", False

    cm = match_contact(conn, company_id, contact)
    status = cm["status"]

    if status == "possible_match" and decision_key not in {
        "use_existing",
        "create_new",
    }:
        raise ValueError(
            f"Ambiguous contact match for '{_blank(contact.full_name) or _blank(contact.email)}' "
            "requires use_existing or create_new."
        )

    if decision_key == "use_existing" or (
        decision_key == "auto" and status == "existing"
    ):
        cid = matched_contact_id or cm.get("contact_id")
        if not cid:
            raise ValueError("use_existing requires matched_contact_id.")
        return int(cid), "existing", False

    if decision_key == "create_new" or (decision_key == "auto" and status == "new"):
        first, last, full = _resolve_contact_names(contact)
        email = _blank(contact.email)
        if not full and not email:
            raise ValueError("Contact requires name or email.")
        if email:
            hit = conn.execute(
                """
                SELECT id FROM contacts
                WHERE company_id = ? AND lower(trim(email)) = ?
                LIMIT 1
                """,
                (company_id, _norm_email(email)),
            ).fetchone()
            if hit:
                return int(hit["id"]), "existing", False

        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no,
                first_name, last_name, title, phone, alt_phone, email,
                source_row_index
            ) VALUES (?, ?, ?, ?, ?, ?, '', ?, 0)
            """,
            (
                company_id,
                external_record_no,
                first,
                last,
                _blank(contact.title),
                _blank(contact.phone),
                email,
            ),
        )
        return int(cur.lastrowid), "created", True

    if status == "existing":
        return int(cm["contact_id"]), "existing", False

    raise ValueError(f"Unsupported contact decision: {decision}")


def _provenance_text(body: CrmAddConfirmRequest) -> str:
    parts = ["Created from NorthStar AI Research"]
    if body.research_run_id:
        parts.append(f"Research Run: {body.research_run_id}")
    src = _blank(body.source) or _blank(body.provider)
    if src:
        parts.append(f"Source: {src}")
    return ". ".join(parts) + "."


def confirm_company_contact_add(
    body: CrmAddConfirmRequest,
    *,
    user_id: int | None = None,
) -> CrmAddConfirmResult:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    client_id = int(body.client_id)
    _require_client_access(user.id, client_id)
    company = body.company
    if not _blank(company.company_name) and body.known_company_id is None:
        raise ValueError("company_name is required.")

    selected = [c for c in (body.contacts or []) if c.selected is not False]

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            match = match_company(
                conn,
                company,
                trusted_record_no=body.trusted_external_record_no,
                known_company_id=body.known_company_id,
            )
            company_id, record_no, company_created = create_or_link_company(
                conn,
                company=company,
                decision=body.company_decision,
                existing_company_id=body.existing_company_id or match.get("company_id"),
                match=match,
            )

            contact_results: list[dict[str, Any]] = []
            created_contact_ids: list[int] = []
            for item in selected:
                cid, status, created = create_or_link_contact(
                    conn,
                    company_id=company_id,
                    external_record_no=record_no,
                    contact=item.contact,
                    decision=item.decision,
                    matched_contact_id=item.matched_contact_id,
                )
                if created and cid:
                    created_contact_ids.append(cid)
                contact_results.append(
                    {
                        "contact_id": cid,
                        "status": status,
                        "created": created,
                        "name": _resolve_contact_names(item.contact)[2]
                        or _blank(item.contact.email),
                    }
                )

            provenance = _provenance_text(body)
            rel_id, rel_created, rel_status = ensure_client_relationship(
                conn,
                client_id=client_id,
                company_id=company_id,
                external_record_no=record_no,
                user_id=user.id,
                provenance_note=provenance,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    activity_id = None
    try:
        with get_connection() as conn:
            cl = conn.execute(
                "SELECT name, code FROM clients WHERE id = ?", (client_id,)
            ).fetchone()
            client_name = _blank(cl["name"]) if cl else "Client"
        note_bits = [_provenance_text(body)]
        if company_created:
            note_bits.append(f"Created company {record_no}.")
        else:
            note_bits.append(f"Linked existing company {record_no}.")
        if created_contact_ids:
            note_bits.append(
                f"Added contacts: {', '.join(str(i) for i in created_contact_ids)}."
            )
        act = create_activity(
            ActivityCreateRequest(
                client=client_name,
                client_id=client_id,
                external_record_no=record_no,
                user_id=user.id,
                activity_type="Note",
                notes=" ".join(note_bits),
                created_by=_blank(user.full_name) or "Julie Magnani",
                assigned_user=_blank(user.full_name) or "Julie Magnani",
            )
        )
        activity_id = act.activity_id
    except Exception:
        activity_id = None

    with get_connection() as conn:
        cname = conn.execute(
            "SELECT company_name FROM companies WHERE id = ?", (company_id,)
        ).fetchone()

    return CrmAddConfirmResult(
        ok=True,
        message="Add to NorthStar completed.",
        client_id=client_id,
        company_id=company_id,
        external_record_no=record_no,
        company_name=_blank(cname["company_name"]) if cname else _blank(company.company_name),
        company_created=company_created,
        relationship_id=rel_id,
        relationship_created=rel_created,
        relationship_status=rel_status,
        contacts=contact_results,
        activity_id=activity_id,
        workspace_path=f"/companies/{record_no}?client_id={client_id}",
    )
