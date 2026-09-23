"""Manual CRM contact create/link for the Contacts page and Company Workspace.

Creates or links a shared master `contacts` row. Does not use Client Knowledge.
Does not auto-create NorthStar users (including Julie Magnani) as outside contacts.
"""

from __future__ import annotations

from staff_context import resolve_staff_actor, runtime_attribution_name

import re
from typing import Any

from access import get_default_user, require_write_client_id, user_can_access_client
from data_steward import sql_active_ccr, sql_active_company
from activities_data import insert_activity_row
from contact_phone import (
    canonical_contact_phone,
    format_phone_with_extension,
    format_us_phone_display,
    lookup_contact_phone_matches,
    stored_phone_pair,
    store_phone_parts,
    upsert_contact_phone_keys,
)
from db import get_connection
from models import (
    ContactPhoneUpdate,
    CrmAddContactInput,
    ManualContactMatch,
    ManualContactPreviewRequest,
    ManualContactPreviewResponse,
    ManualContactSaveRequest,
    ManualContactSaveResult,
)

CONTACT_INFO_REQUIRED_MSG = (
    "Enter an email or phone, or confirm creating this contact without either."
)
DUPLICATE_MUST_LINK_MSG = (
    "A likely match exists. Link the existing contact instead of creating a duplicate."
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_email(value: str) -> str:
    return _blank(value).lower()


def _norm_person_name(value: str) -> str:
    s = _blank(value).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _phone_identity(value: str) -> str:
    nanp10, last7 = canonical_contact_phone(value)
    return nanp10 or last7


def _full_name(first: str, last: str) -> str:
    return f"{_blank(first)} {_blank(last)}".strip()


def _has_contact_info(email: str, phone: str, alt_phone: str) -> bool:
    return bool(
        _norm_email(email) or _phone_identity(phone) or _phone_identity(alt_phone)
    )


def _require_user_and_client(client_id: object) -> tuple[Any, int]:
    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    cid = require_write_client_id(client_id, user_id=user.id)
    if not user_can_access_client(user.id, cid) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")
    return user, cid


def lookup_companies_for_client(client_id: object, q: str = "") -> dict[str, Any]:
    """Client-scoped company picker for Add Contact. Requires a specific client."""
    _user, cid = _require_user_and_client(client_id)
    query = re.sub(r"[%_]", "", _blank(q))
    like = f"%{query}%" if query else None
    with get_connection() as conn:
        active_co = sql_active_company(conn, "co")
        active_ccr = sql_active_ccr(conn, "ccr")
        if like is None:
            rows = conn.execute(
                f"""
                SELECT
                    co.id,
                    co.company_name,
                    COALESCE(
                        NULLIF(TRIM(ccr.external_record_no), ''),
                        co.external_record_no
                    ) AS external_record_no
                FROM companies co
                JOIN client_company_relationships ccr ON ccr.company_id = co.id
                WHERE ccr.client_id = ?
                  AND {active_co}
                  AND {active_ccr}
                ORDER BY co.company_name COLLATE NOCASE, co.id
                LIMIT 20
                """,
                (cid,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT
                    co.id,
                    co.company_name,
                    COALESCE(
                        NULLIF(TRIM(ccr.external_record_no), ''),
                        co.external_record_no
                    ) AS external_record_no
                FROM companies co
                JOIN client_company_relationships ccr ON ccr.company_id = co.id
                WHERE ccr.client_id = ?
                  AND {active_co}
                  AND {active_ccr}
                  AND (
                    co.company_name LIKE ?
                    OR co.external_record_no LIKE ?
                    OR ccr.external_record_no LIKE ?
                  )
                ORDER BY co.company_name COLLATE NOCASE, co.id
                LIMIT 20
                """,
                (cid, like, like, like),
            ).fetchall()
    return {
        "companies": [
            {
                "id": int(r["id"]),
                "company_name": _blank(r["company_name"]),
                "external_record_no": _blank(r["external_record_no"]),
            }
            for r in rows
        ]
    }


def _load_client_company(conn, client_id: int, company_id: int):
    row = conn.execute(
        f"""
        SELECT
            co.id AS company_id,
            co.company_name,
            co.external_record_no AS company_master_record_no,
            ccr.id AS relationship_id,
            ccr.external_record_no AS relationship_record_no,
            cl.name AS client_name
        FROM companies co
        JOIN client_company_relationships ccr
            ON ccr.company_id = co.id AND ccr.client_id = ?
        JOIN clients cl ON cl.id = ccr.client_id
        WHERE co.id = ?
          AND {sql_active_company(conn, "co")}
          AND {sql_active_ccr(conn, "ccr")}
        """,
        (client_id, company_id),
    ).fetchone()
    if row is None:
        raise LookupError("Company is not assigned to this client.")
    return row


def _relationship_record_no(row) -> str:
    return _blank(row["relationship_record_no"]) or _blank(row["company_master_record_no"])


def _snapshot_other_ccrs(conn, company_id: int, client_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
            FROM client_company_relationships
            WHERE company_id = ? AND client_id != ?
            ORDER BY client_id, id
            """,
            (company_id, client_id),
        ).fetchall()
    ]


def _assert_other_ccrs_unchanged(conn, company_id: int, client_id: int, before: list[dict]) -> None:
    after = _snapshot_other_ccrs(conn, company_id, client_id)
    if after != before:
        raise RuntimeError("Refusing to overwrite another client's relationship.")


def _already_assigned(conn, contact_id: int, client_id: int) -> bool:
    from client_workspace_data import _contact_assignment_sql, _contact_record_join

    row = conn.execute(
        f"""
        SELECT 1 AS ok
        {_contact_record_join()}
        WHERE ct.id = ? AND ccr.client_id = ?
        {_contact_assignment_sql()}
        LIMIT 1
        """,
        (int(contact_id), int(client_id)),
    ).fetchone()
    return row is not None


def _match_dict(
    conn,
    *,
    contact_id: int,
    reasons: list[str],
    confidence: str,
    client_id: int,
    selected_company_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            ct.id, ct.first_name, ct.last_name, ct.title, ct.email,
            ct.phone, ct.phone_extension, ct.alt_phone, ct.alt_phone_extension, ct.company_id, co.company_name
        FROM contacts ct
        JOIN companies co ON co.id = ct.company_id
        WHERE ct.id = ?
        """,
        (contact_id,),
    ).fetchone()
    if row is None:
        return None
    company_id = int(row["company_id"])
    return {
        "contact_id": int(row["id"]),
        "first_name": _blank(row["first_name"]),
        "last_name": _blank(row["last_name"]),
        "title": _blank(row["title"]),
        "email": _blank(row["email"]),
        "phone": format_phone_with_extension(
            _blank(row["phone"]),
            _blank(row["phone_extension"]) if "phone_extension" in row.keys() else "",
        ),
        "alt_phone": format_phone_with_extension(
            _blank(row["alt_phone"]),
            _blank(row["alt_phone_extension"]) if "alt_phone_extension" in row.keys() else "",
        ),
        "company_id": company_id,
        "company_name": _blank(row["company_name"]),
        "reasons": reasons,
        "confidence": confidence,
        "already_assigned": _already_assigned(conn, int(row["id"]), client_id),
        "same_company": company_id == int(selected_company_id),
    }


def _merge_match(bucket: dict[int, dict[str, Any]], item: dict[str, Any]) -> None:
    cid = int(item["contact_id"])
    existing = bucket.get(cid)
    if existing is None:
        bucket[cid] = item
        return
    reasons = list(dict.fromkeys([*existing["reasons"], *item["reasons"]]))
    confidence = existing["confidence"]
    if item["confidence"] == "high":
        confidence = "high"
    existing.update(item)
    existing["reasons"] = reasons
    existing["confidence"] = confidence


def _global_email_matches(conn, email: str) -> list[tuple[int, list[str]]]:
    norm = _norm_email(email)
    if not norm:
        return []
    rows = conn.execute(
        """
        SELECT id FROM contacts
        WHERE trim(email) != '' AND lower(trim(email)) = ?
        ORDER BY id
        LIMIT 20
        """,
        (norm,),
    ).fetchall()
    return [(int(r["id"]), ["email_exact"]) for r in rows]


def _global_phone_matches(conn, *phones: str) -> list[tuple[int, list[str], str]]:
    return lookup_contact_phone_matches(conn, *phones)


GLOBAL_NAME_MATCH_LIMIT = 40


def _name_lookup_probes(first: str, last: str) -> list[tuple[str, str]]:
    """Equality probes that can use idx_contacts_last_first_nocase."""
    first_t = _blank(first)
    last_t = _blank(last)
    if not first_t or not last_t:
        return []
    probes = [(first_t, last_t)]
    first_n = _norm_person_name(first_t)
    last_n = _norm_person_name(last_t)
    if first_n and last_n:
        probes.append((first_n, last_n))
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for first_q, last_q in probes:
        key = (first_q.lower(), last_q.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((first_q, last_q))
    return out


def _global_name_matches(
    conn, first: str, last: str, selected_company_id: int
) -> list[tuple[int, list[str]]]:
    """Cross-company first+last matches. Possible only; never a table scan."""
    target = _norm_person_name(_full_name(first, last))
    if not target:
        return []
    seen: dict[int, list[str]] = {}
    selected = int(selected_company_id)
    for first_q, last_q in _name_lookup_probes(first, last):
        rows = conn.execute(
            """
            SELECT id, first_name, last_name
            FROM contacts
            WHERE last_name = ? COLLATE NOCASE
              AND first_name = ? COLLATE NOCASE
              AND company_id != ?
            ORDER BY id
            LIMIT ?
            """,
            (last_q, first_q, selected, GLOBAL_NAME_MATCH_LIMIT),
        ).fetchall()
        for row in rows:
            if _norm_person_name(_full_name(row["first_name"], row["last_name"])) != target:
                continue
            seen.setdefault(int(row["id"]), ["name_exact"])
    return list(seen.items())


def find_manual_contact_matches(
    conn,
    *,
    client_id: int,
    company_id: int,
    first_name: str,
    last_name: str,
    title: str,
    email: str,
    phone: str,
    alt_phone: str,
) -> list[dict[str, Any]]:
    from crm_add_data import match_contact

    bucket: dict[int, dict[str, Any]] = {}
    for contact_id, reasons in _global_email_matches(conn, email):
        item = _match_dict(
            conn,
            contact_id=contact_id,
            reasons=reasons,
            confidence="high",
            client_id=client_id,
            selected_company_id=company_id,
        )
        if item:
            _merge_match(bucket, item)
    for contact_id, reasons, confidence in _global_phone_matches(conn, phone, alt_phone):
        item = _match_dict(
            conn,
            contact_id=contact_id,
            reasons=reasons,
            confidence=confidence,
            client_id=client_id,
            selected_company_id=company_id,
        )
        if item:
            _merge_match(bucket, item)

    local = match_contact(
        conn,
        company_id,
        CrmAddContactInput(
            first_name=first_name,
            last_name=last_name,
            title=title,
            email=email,
            phone=phone,
        ),
    )
    local_id = local.get("contact_id")
    if local_id and local.get("status") in {"existing", "possible_match"}:
        confidence = "high" if local.get("status") == "existing" else "possible"
        reasons = list(local.get("reasons") or ["name_company"])
        item = _match_dict(
            conn,
            contact_id=int(local_id),
            reasons=reasons,
            confidence=confidence,
            client_id=client_id,
            selected_company_id=company_id,
        )
        if item:
            _merge_match(bucket, item)

    for contact_id, reasons in _global_name_matches(conn, first_name, last_name, company_id):
        item = _match_dict(
            conn,
            contact_id=contact_id,
            reasons=reasons,
            confidence="possible",
            client_id=client_id,
            selected_company_id=company_id,
        )
        if item:
            _merge_match(bucket, item)

    ranked = list(bucket.values())
    ranked.sort(
        key=lambda m: (
            0 if m["confidence"] == "high" else 1,
            0 if m["same_company"] else 1,
            _full_name(m["first_name"], m["last_name"]).lower(),
            m["contact_id"],
        )
    )
    return ranked


def preview_manual_contact(body: ManualContactPreviewRequest) -> ManualContactPreviewResponse:
    from client_workspace_data import ensure_contact_workflow_schema

    _user, client_id = _require_user_and_client(body.client_id)
    first = _blank(body.first_name)
    last = _blank(body.last_name)
    if not first or not last:
        raise ValueError("First name and last name are required.")
    ensure_contact_workflow_schema()
    with get_connection() as conn:
        _load_client_company(conn, client_id, int(body.company_id))
        matches = find_manual_contact_matches(
            conn,
            client_id=client_id,
            company_id=int(body.company_id),
            first_name=first,
            last_name=last,
            title=_blank(body.title),
            email=_blank(body.email),
            phone=_blank(body.phone),
            alt_phone=_blank(body.alt_phone),
        )
    has_info = _has_contact_info(body.email, body.phone, body.alt_phone)
    high = any(m["confidence"] == "high" for m in matches)
    message = ""
    if high:
        message = DUPLICATE_MUST_LINK_MSG
    elif matches:
        message = "A possible match exists. Link it, or create a new contact if this is a different person."
    elif not has_info:
        message = CONTACT_INFO_REQUIRED_MSG
    return ManualContactPreviewResponse(
        matches=[ManualContactMatch(**m) for m in matches],
        can_create=not high,
        requires_contact_info_confirmation=not has_info,
        message=message,
    )


def _ensure_client_company_relationship(
    conn,
    *,
    client_id: int,
    company_id: int,
    user_id: int,
    created_by: str,
    contact_id: int,
) -> tuple[int, str, str]:
    from client_workspace_data import (
        _allocate_relationship_record_no,
        _client_default_new_status,
    )

    existing = conn.execute(
        """
        SELECT id, external_record_no FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (client_id, company_id),
    ).fetchone()
    company = conn.execute(
        "SELECT company_name, external_record_no FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if company is None:
        raise LookupError("Company not found.")
    company_name = _blank(company["company_name"])
    if existing is not None:
        record_no = _blank(existing["external_record_no"]) or _blank(
            company["external_record_no"]
        )
        return int(existing["id"]), record_no, company_name

    record_no = _allocate_relationship_record_no(conn)
    status = _client_default_new_status(conn, client_id)
    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, status, assigned_user_id,
            priority, next_action, notes, is_hot, created_at, updated_at,
            external_record_no
        ) VALUES (?, ?, ?, ?, '', '', ?, 0, datetime('now'), datetime('now'), ?)
        """,
        (
            client_id,
            company_id,
            status,
            user_id,
            f"Added with shared contact {contact_id} by {created_by}.",
            record_no,
        ),
    )
    return int(cur.lastrowid), record_no, company_name


def _link_existing_on_conn(
    conn,
    *,
    client_id: int,
    contact_id: int,
    user,
    created_by: str,
    activity_type: str,
) -> dict[str, Any]:
    from client_workspace_data import _client_default_new_status

    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, alt_phone, email
        FROM contacts WHERE id = ?
        """,
        (contact_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Contact not found.")
    company_id = int(row["company_id"])
    identity = {
        "first_name": _blank(row["first_name"]),
        "last_name": _blank(row["last_name"]),
        "title": _blank(row["title"]),
        "phone": _blank(row["phone"]),
        "alt_phone": _blank(row["alt_phone"]),
        "email": _blank(row["email"]),
    }
    other_before = _snapshot_other_ccrs(conn, company_id, client_id)
    client_name = _blank(
        conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()["name"]
    )
    already = _already_assigned(conn, contact_id, client_id)
    relationship_id, record_no, company_name = _ensure_client_company_relationship(
        conn,
        client_id=client_id,
        company_id=company_id,
        user_id=int(user.id),
        created_by=created_by,
        contact_id=contact_id,
    )
    if already:
        _assert_other_ccrs_unchanged(conn, company_id, client_id, other_before)
        return {
            "ok": True,
            "action": "linked",
            "message": (
                f"{_full_name(identity['first_name'], identity['last_name']) or 'Contact'} "
                f"is already assigned to {client_name}."
            ),
            "contact_id": contact_id,
            "company_id": company_id,
            "client_id": client_id,
            "activity_id": None,
            "already_assigned": True,
        }

    conn.execute(
        """
        INSERT OR IGNORE INTO contact_client_relationships (
            contact_id, client_id, relationship_id, created_at, created_by
        ) VALUES (?, ?, ?, datetime('now'), ?)
        """,
        (contact_id, client_id, relationship_id, created_by),
    )
    new_status = _client_default_new_status(conn, client_id)
    conn.execute(
        """
        INSERT INTO contact_client_workflows (
            contact_id, client_id, relationship_id, status, assigned_user_id,
            next_action, follow_up_date, follow_up_time, updated_at
        ) VALUES (?, ?, ?, ?, NULL, '', NULL, '', datetime('now'))
        ON CONFLICT(contact_id, client_id) DO UPDATE SET
            relationship_id = excluded.relationship_id,
            status = CASE
                WHEN TRIM(contact_client_workflows.status) = ''
                THEN excluded.status ELSE contact_client_workflows.status
            END,
            updated_at = datetime('now')
        """,
        (contact_id, client_id, relationship_id, new_status),
    )
    after_row = conn.execute(
        "SELECT first_name, last_name, title, phone, alt_phone, email FROM contacts WHERE id = ?",
        (contact_id,),
    ).fetchone()
    if (
        _blank(after_row["first_name"]) != identity["first_name"]
        or _blank(after_row["last_name"]) != identity["last_name"]
        or _blank(after_row["title"]) != identity["title"]
        or _blank(after_row["phone"]) != identity["phone"]
        or _blank(after_row["alt_phone"]) != identity["alt_phone"]
        or _blank(after_row["email"]) != identity["email"]
    ):
        raise RuntimeError("Link must not alter the shared master contact.")
    _assert_other_ccrs_unchanged(conn, company_id, client_id, other_before)
    full_name = _full_name(identity["first_name"], identity["last_name"]) or "Contact"
    activity_id = insert_activity_row(
        conn,
        client_id=client_id,
        company_id=company_id,
        relationship_id=relationship_id,
        external_record_no=record_no,
        user_id=int(user.id),
        contact_id=contact_id,
        activity_type=activity_type,
        notes=(
            f"{created_by} linked contact {full_name} (contact_id {contact_id}) "
            f"at {company_name} to {client_name}."
        ),
        created_by=created_by,
        assigned_user=created_by,
    )
    return {
        "ok": True,
        "action": "linked",
        "message": f"{full_name} linked to {client_name}.",
        "contact_id": contact_id,
        "company_id": company_id,
        "client_id": client_id,
        "activity_id": activity_id,
        "already_assigned": False,
    }


def save_manual_contact(body: ManualContactSaveRequest) -> ManualContactSaveResult:
    user, client_id = _require_user_and_client(body.client_id)
    action = _blank(body.action).lower()
    if action not in {"create", "link"}:
        raise ValueError("action must be create or link.")
    first = _blank(body.first_name)
    last = _blank(body.last_name)
    if action == "create" and (not first or not last):
        raise ValueError("First name and last name are required.")
    created_by = runtime_attribution_name(user, getattr(body, "created_by", ""))
    selected_company_id = int(body.company_id)
    from client_workspace_data import ensure_contact_workflow_schema

    ensure_contact_workflow_schema()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if action == "link":
                if body.existing_contact_id is None or int(body.existing_contact_id) <= 0:
                    raise ValueError("existing_contact_id is required to link a contact.")
                result = _link_existing_on_conn(
                    conn,
                    client_id=client_id,
                    contact_id=int(body.existing_contact_id),
                    user=user,
                    created_by=created_by,
                    activity_type="Contact Linked",
                )
                conn.commit()
                return ManualContactSaveResult(**result)

            rel = _load_client_company(conn, client_id, selected_company_id)
            if not _has_contact_info(body.email, body.phone, body.alt_phone):
                if not body.confirm_without_contact_info:
                    raise ValueError(CONTACT_INFO_REQUIRED_MSG)
            matches = find_manual_contact_matches(
                conn,
                client_id=client_id,
                company_id=selected_company_id,
                first_name=first,
                last_name=last,
                title=_blank(body.title),
                email=_blank(body.email),
                phone=_blank(body.phone),
                alt_phone=_blank(body.alt_phone),
            )
            if any(m["confidence"] == "high" for m in matches):
                raise ValueError(DUPLICATE_MUST_LINK_MSG)

            other_before = _snapshot_other_ccrs(conn, selected_company_id, client_id)
            record_no = _relationship_record_no(rel)
            company_name = _blank(rel["company_name"])
            client_name = _blank(rel["client_name"])
            relationship_id = int(rel["relationship_id"])
            columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(contacts)")}
            explicit_ext = _blank(getattr(body, "phone_extension", ""))
            explicit_alt_ext = _blank(getattr(body, "alt_phone_extension", ""))
            phone_main, phone_ext = stored_phone_pair(
                _blank(body.phone),
                explicit_ext if explicit_ext else None,
                field="phone",
            )
            alt_main, alt_ext = stored_phone_pair(
                _blank(body.alt_phone),
                explicit_alt_ext if explicit_alt_ext else None,
                field="alt_phone",
            )
            insert_cols = [
                "company_id",
                "external_record_no",
                "first_name",
                "last_name",
                "title",
                "phone",
                "alt_phone",
                "email",
                "source_row_index",
            ]
            insert_vals: list[object] = [
                selected_company_id,
                record_no,
                first,
                last,
                _blank(body.title),
                phone_main,
                alt_main,
                _blank(body.email),
                0,
            ]
            if "phone_extension" in columns:
                insert_cols.append("phone_extension")
                insert_vals.append(phone_ext or None)
            if "alt_phone_extension" in columns:
                insert_cols.append("alt_phone_extension")
                insert_vals.append(alt_ext or None)
            if _blank(body.linkedin_url) and "linkedin_url" in columns:
                insert_cols.append("linkedin_url")
                insert_vals.append(_blank(body.linkedin_url))
            if _blank(body.location) and "location" in columns:
                insert_cols.append("location")
                insert_vals.append(_blank(body.location))
            if _blank(body.zoominfo_contact_id) and "zoominfo_contact_id" in columns:
                insert_cols.append("zoominfo_contact_id")
                insert_vals.append(_blank(body.zoominfo_contact_id))
            source_value = _blank(body.source) or "manual"
            if "source" in columns:
                insert_cols.append("source")
                insert_vals.append(source_value)
            if source_value.lower() == "zoominfo" and "source_updated_at" in columns:
                from datetime import datetime as _dt

                insert_cols.append("source_updated_at")
                insert_vals.append(_dt.now().replace(microsecond=0).isoformat(sep=" "))
            placeholders = ", ".join("?" for _ in insert_cols)
            cur = conn.execute(
                f"INSERT INTO contacts ({', '.join(insert_cols)}) VALUES ({placeholders})",
                insert_vals,
            )
            contact_id = int(cur.lastrowid)
            from data_steward import ENTITY_CONTACT, record_populated_creates, source_ref_manual

            record_populated_creates(
                conn,
                entity_type=ENTITY_CONTACT,
                entity_id=contact_id,
                fields={
                    "first_name": first,
                    "last_name": last,
                    "title": _blank(body.title),
                    "email": _blank(body.email),
                    "phone": phone_main,
                    "phone_extension": phone_ext or "",
                    "alt_phone": alt_main,
                    "alt_phone_extension": alt_ext or "",
                    "company_id": str(selected_company_id),
                },
                actor=user,
                source_ref=source_ref_manual(),
                action="CREATE",
                client_id=client_id,
            )
            upsert_contact_phone_keys(
                conn,
                contact_id,
                phone_main,
                alt_main,
            )
            conn.execute(
                """
                INSERT INTO contact_client_relationships (
                    contact_id, client_id, relationship_id, created_at, created_by
                ) VALUES (?, ?, ?, datetime('now'), ?)
                """,
                (contact_id, client_id, relationship_id, created_by),
            )
            from client_workspace_data import _client_default_new_status

            new_status = _client_default_new_status(conn, client_id)
            conn.execute(
                """
                INSERT INTO contact_client_workflows (
                    contact_id, client_id, relationship_id, status, assigned_user_id,
                    next_action, follow_up_date, follow_up_time, updated_at
                ) VALUES (?, ?, ?, ?, NULL, '', NULL, '', datetime('now'))
                """,
                (contact_id, client_id, relationship_id, new_status),
            )
            _assert_other_ccrs_unchanged(conn, selected_company_id, client_id, other_before)
            full_name = _full_name(first, last)
            activity_id = insert_activity_row(
                conn,
                client_id=client_id,
                company_id=selected_company_id,
                relationship_id=relationship_id,
                external_record_no=record_no,
                user_id=int(user.id),
                contact_id=contact_id,
                activity_type="Contact Created",
                notes=(
                    f"{created_by} created contact {full_name} (contact_id {contact_id}) "
                    f"at {company_name} for {client_name}."
                ),
                created_by=created_by,
                assigned_user=created_by,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return ManualContactSaveResult(
        ok=True,
        action="created",
        message=f"{full_name} created for {client_name}.",
        contact_id=contact_id,
        company_id=selected_company_id,
        client_id=client_id,
        activity_id=activity_id,
        already_assigned=False,
    )


def update_contact_phones(contact_id: int, body: ContactPhoneUpdate) -> dict:
    """Update stored main phone and structured extension for one CRM contact."""
    from client_workspace_data import get_contact_workspace

    if not isinstance(body, ContactPhoneUpdate):
        body = ContactPhoneUpdate.model_validate(body)
    _user, client_id = _require_user_and_client(body.client_id)
    cid = int(contact_id)
    if cid <= 0:
        raise ValueError("contact_id is required.")

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT ct.id, ct.company_id, ct.phone, ct.phone_extension,
                       ct.alt_phone, ct.alt_phone_extension
                FROM contacts ct
                JOIN client_company_relationships ccr
                  ON ccr.company_id = ct.company_id AND ccr.client_id = ?
                WHERE ct.id = ?
                """,
                (client_id, cid),
            ).fetchone()
            if row is None:
                raise LookupError("Contact not found for this client.")
            columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(contacts)")}
            stored_phone = _blank(row["phone"])
            stored_ext = _blank(row["phone_extension"]) if "phone_extension" in row.keys() else ""
            stored_alt = _blank(row["alt_phone"])
            stored_alt_ext = (
                _blank(row["alt_phone_extension"]) if "alt_phone_extension" in row.keys() else ""
            )
            if body.phone is not None and body.phone_extension is None:
                phone_main, phone_ext = stored_phone_pair(body.phone, None, field="phone")
            elif body.phone_extension is not None:
                phone_main, phone_ext = stored_phone_pair(
                    stored_phone if body.phone is None else _blank(body.phone),
                    body.phone_extension,
                    field="phone",
                )
            else:
                phone_main, phone_ext = stored_phone, stored_ext
            if body.alt_phone is not None and body.alt_phone_extension is None:
                alt_main, alt_ext = stored_phone_pair(body.alt_phone, None, field="alt_phone")
            elif body.alt_phone_extension is not None:
                alt_main, alt_ext = stored_phone_pair(
                    stored_alt if body.alt_phone is None else _blank(body.alt_phone),
                    body.alt_phone_extension,
                    field="alt_phone",
                )
            else:
                alt_main, alt_ext = stored_alt, stored_alt_ext
            sets = ["phone = ?", "alt_phone = ?"]
            vals: list[object] = [phone_main, alt_main]
            if "phone_extension" in columns:
                sets.append("phone_extension = ?")
                vals.append(phone_ext or "")
            if "alt_phone_extension" in columns:
                sets.append("alt_phone_extension = ?")
                vals.append(alt_ext or "")
            vals.append(cid)
            conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?", vals)
            upsert_contact_phone_keys(conn, cid, phone_main, alt_main)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    workspace = get_contact_workspace(cid, client_id=client_id)
    return {
        "ok": True,
        "message": "Contact phone updated.",
        "contact_id": cid,
        "client_id": client_id,
        "phone": phone_main,
        "phone_extension": phone_ext or "",
        "alt_phone": alt_main,
        "alt_phone_extension": alt_ext or "",
        "workspace": workspace,
    }

