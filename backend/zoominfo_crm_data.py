"""ZoomInfo CRM review: update existing contacts and add new ZoomInfo records.

ZoomInfo never overwrites NorthStar automatically. Apply only user-selected fields.
Blank ZoomInfo values never erase populated fields. Company moves require confirmation.
No live ZoomInfo API — callers supply a snapshot (tests and future fetch).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from access import get_default_user, require_write_client_id, user_can_access_client
from activities_data import insert_activity_row
from contact_phone import format_us_phone_display, upsert_contact_phone_keys
from db import get_connection
from models import (
    ManualContactMatch,
    ZoomInfoAddPreviewRequest,
    ZoomInfoAddResult,
    ZoomInfoAddSaveRequest,
    ZoomInfoContactApplyRequest,
    ZoomInfoContactApplyResult,
    ZoomInfoContactPreviewRequest,
    ZoomInfoContactPreviewResponse,
    ZoomInfoFieldChoice,
    ZoomInfoSnapshot,
)

ZOOMINFO_UNAVAILABLE = "ZoomInfo not connected"
SOURCE_ZOOMINFO = "ZoomInfo"

CONTACT_FIELDS = (
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("title", "Title"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("alt_phone", "Alternate / mobile phone"),
    ("linkedin_url", "LinkedIn URL"),
    ("location", "Location"),
)

INFO_FIELDS = (
    ("website", "Website"),
    ("industry", "Industry"),
    ("employee_size", "Employee size"),
    ("sales_volume", "Sales volume"),
    ("zoominfo_contact_id", "ZoomInfo contact ID"),
)

IDENTITY_FIELDS = frozenset({"first_name", "last_name", "email", "phone", "alt_phone"})


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _require_user_and_client(client_id: object) -> tuple[Any, int]:
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    cid = require_write_client_id(client_id, user_id=user.id)
    if not user_can_access_client(user.id, cid) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")
    return user, cid


def _row_text(row, key: str) -> str:
    try:
        keys = row.keys()
    except Exception:
        keys = []
    if key not in keys:
        return ""
    return _blank(row[key])


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


def _snapshot_client_scoped(conn, client_id: int, contact_id: int, company_id: int) -> dict:
    return {
        "workflow": [
            dict(r)
            for r in conn.execute(
                """
                SELECT contact_id, client_id, status, assigned_user_id, next_action, follow_up_date
                FROM contact_client_workflows
                WHERE contact_id = ?
                ORDER BY client_id
                """,
                (contact_id,),
            ).fetchall()
        ],
        "notes": [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, client_id, company_id, note_text
                FROM legacy_notes WHERE company_id = ? ORDER BY id
                """,
                (company_id,),
            ).fetchall()
        ],
        "campaigns": _campaign_membership(conn, contact_id, company_id),
        "tasks": _task_snapshot(conn, contact_id),
        "other_ccr": _snapshot_other_ccrs(conn, company_id, client_id),
    }


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _campaign_membership(conn, contact_id: int, company_id: int) -> dict:
    companies = []
    contacts = []
    if _table_exists(conn, "campaign_companies"):
        companies = [
            dict(r)
            for r in conn.execute(
                """
                SELECT campaign_id, company_id FROM campaign_companies
                WHERE company_id = ? ORDER BY campaign_id, id
                """,
                (company_id,),
            ).fetchall()
        ]
    if _table_exists(conn, "campaign_contacts"):
        contacts = [
            dict(r)
            for r in conn.execute(
                """
                SELECT campaign_id, contact_id, company_id FROM campaign_contacts
                WHERE contact_id = ? ORDER BY campaign_id, id
                """,
                (contact_id,),
            ).fetchall()
        ]
    return {"companies": companies, "contacts": contacts}


def _task_snapshot(conn, contact_id: int) -> list[dict]:
    if not _table_exists(conn, "work_queue_items"):
        return []
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, contact_id, completion_status, due_date
            FROM work_queue_items
            WHERE contact_id = ?
            ORDER BY id
            """,
            (contact_id,),
        ).fetchall()
    ]


def _alt_phone_from_snapshot(snap: ZoomInfoSnapshot) -> str:
    return _blank(snap.alt_phone) or _blank(snap.mobile_phone)


def _zoominfo_contact_values(snap: ZoomInfoSnapshot) -> dict[str, str]:
    return {
        "first_name": _blank(snap.first_name),
        "last_name": _blank(snap.last_name),
        "title": _blank(snap.title),
        "email": _blank(snap.email),
        "phone": _blank(snap.phone),
        "alt_phone": _alt_phone_from_snapshot(snap),
        "linkedin_url": _blank(snap.linkedin_url),
        "location": _blank(snap.location),
    }


def preview_zoominfo_contact_update(
    contact_id: int, body: ZoomInfoContactPreviewRequest
) -> ZoomInfoContactPreviewResponse:
    _user, client_id = _require_user_and_client(body.client_id)
    retrieved_at = _now()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT ct.*, co.company_name
            FROM contacts ct
            JOIN companies co ON co.id = ct.company_id
            WHERE ct.id = ?
            """,
            (contact_id,),
        ).fetchone()
        if row is None:
            raise LookupError("Contact not found.")
        company_id = int(row["company_id"])
        company_name = _blank(row["company_name"])
        snap = body.zoominfo
        if snap is None:
            return ZoomInfoContactPreviewResponse(
                available=False,
                status=ZOOMINFO_UNAVAILABLE,
                contact_id=contact_id,
                client_id=client_id,
                company_id=company_id,
                company_name=company_name,
                fields=[],
                retrieved_at=retrieved_at,
            )
        zi = _zoominfo_contact_values(snap)
        fields = []
        for key, label in CONTACT_FIELDS:
            ns_val = _row_text(row, key)
            zi_val = zi.get(key, "")
            fields.append(
                ZoomInfoFieldChoice(
                    field=key,
                    label=label,
                    northstar_value=ns_val,
                    zoominfo_value=zi_val,
                    keep_northstar=True,
                    blank_zoominfo=not bool(zi_val),
                    applyable=True,
                )
            )
        zi_company = _blank(snap.company_name)
        different = bool(zi_company) and zi_company.lower() != company_name.lower()
        fields.append(
            ZoomInfoFieldChoice(
                field="company",
                label="Company",
                northstar_value=company_name,
                zoominfo_value=zi_company,
                keep_northstar=True,
                blank_zoominfo=not bool(zi_company),
                different_company=different,
                applyable=False,
            )
        )
        for key, label in INFO_FIELDS:
            zi_val = _blank(getattr(snap, key, ""))
            ns_val = _row_text(row, key)
            if not zi_val and not ns_val:
                continue
            fields.append(
                ZoomInfoFieldChoice(
                    field=key,
                    label=label,
                    northstar_value=ns_val,
                    zoominfo_value=zi_val,
                    keep_northstar=True,
                    blank_zoominfo=not bool(zi_val),
                    applyable=False,
                )
            )
        matches: list[dict] = []
        if any(zi[k] for k in ("email", "phone", "alt_phone", "first_name", "last_name")):
            from manual_contact_data import find_manual_contact_matches

            matches = find_manual_contact_matches(
                conn,
                client_id=client_id,
                company_id=company_id,
                first_name=zi["first_name"] or _row_text(row, "first_name"),
                last_name=zi["last_name"] or _row_text(row, "last_name"),
                title=zi["title"],
                email=zi["email"],
                phone=zi["phone"],
                alt_phone=zi["alt_phone"],
            )
            matches = [m for m in matches if int(m["contact_id"]) != int(contact_id)]
    return ZoomInfoContactPreviewResponse(
        available=True,
        status="ready",
        contact_id=contact_id,
        client_id=client_id,
        company_id=company_id,
        company_name=company_name,
        fields=fields,
        different_company=different,
        zoominfo_company_name=zi_company,
        matches=[ManualContactMatch(**m) for m in matches],
        retrieved_at=retrieved_at,
    )


def apply_zoominfo_contact_update(
    contact_id: int, body: ZoomInfoContactApplyRequest
) -> ZoomInfoContactApplyResult:
    user, client_id = _require_user_and_client(body.client_id)
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    apply_set = { _blank(f) for f in body.apply_fields if _blank(f) }
    zi = _zoominfo_contact_values(body.zoominfo)
    retrieved_at = _now()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Contact not found.")
            company_id = int(row["company_id"])
            company_row = conn.execute(
                "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
                (company_id,),
            ).fetchone()
            company_name = _blank(company_row["company_name"]) if company_row else ""
            scoped_before = _snapshot_client_scoped(conn, client_id, contact_id, company_id)
            other_before = scoped_before["other_ccr"]

            zi_company = _blank(body.zoominfo.company_name)
            different = bool(zi_company) and zi_company.lower() != company_name.lower()
            company_relinked = False
            if different and body.confirm_company_relink:
                target_id = body.target_company_id
                if target_id is None or int(target_id) <= 0:
                    raise ValueError(
                        "Confirming a company relink requires target_company_id."
                    )
                target = conn.execute(
                    "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
                    (int(target_id),),
                ).fetchone()
                if target is None:
                    raise LookupError("Target company not found.")
                from crm_add_data import ensure_client_relationship

                ensure_client_relationship(
                    conn,
                    client_id=client_id,
                    company_id=int(target["id"]),
                    external_record_no=_blank(target["external_record_no"]),
                    user_id=int(user.id),
                    provenance_note=f"Contact relinked from ZoomInfo by {created_by}.",
                )
                conn.execute(
                    "UPDATE contacts SET company_id = ? WHERE id = ?",
                    (int(target["id"]), contact_id),
                )
                company_id = int(target["id"])
                company_name = _blank(target["company_name"])
                company_relinked = True
            elif different and "company" in apply_set:
                raise ValueError(
                    "ZoomInfo reports a different company. Confirm a separate relink; "
                    "the contact is not moved automatically."
                )

            from manual_contact_data import find_manual_contact_matches

            proposed = {
                "first_name": zi["first_name"]
                if "first_name" in apply_set and zi["first_name"]
                else _row_text(row, "first_name"),
                "last_name": zi["last_name"]
                if "last_name" in apply_set and zi["last_name"]
                else _row_text(row, "last_name"),
                "email": zi["email"]
                if "email" in apply_set and zi["email"]
                else _row_text(row, "email"),
                "phone": zi["phone"]
                if "phone" in apply_set and zi["phone"]
                else _row_text(row, "phone"),
                "alt_phone": zi["alt_phone"]
                if "alt_phone" in apply_set and zi["alt_phone"]
                else _row_text(row, "alt_phone"),
                "title": zi["title"]
                if "title" in apply_set and zi["title"]
                else _row_text(row, "title"),
            }
            identity_changing = bool(apply_set & IDENTITY_FIELDS)
            if identity_changing:
                matches = find_manual_contact_matches(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    first_name=proposed["first_name"],
                    last_name=proposed["last_name"],
                    title=proposed["title"],
                    email=proposed["email"],
                    phone=proposed["phone"],
                    alt_phone=proposed["alt_phone"],
                )
                high_other = [
                    m
                    for m in matches
                    if m["confidence"] == "high" and int(m["contact_id"]) != int(contact_id)
                ]
                if high_other:
                    raise ValueError(
                        "Those ZoomInfo identity values match another contact. "
                        "Uncheck email/phone/name or link the existing contact instead."
                    )

            changed: list[str] = []
            updates: list[str] = []
            params: list[object] = []
            contact_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(contacts)")}
            for key, _label in CONTACT_FIELDS:
                if key not in apply_set:
                    continue
                if key not in contact_cols:
                    continue
                new_val = zi.get(key, "")
                if not new_val:
                    continue
                if key in {"phone", "alt_phone"}:
                    new_val = format_us_phone_display(str(new_val))
                old_val = _row_text(row, key)
                if new_val == old_val:
                    continue
                updates.append(f"{key} = ?")
                params.append(new_val)
                changed.append(key)

            zi_contact_id = _blank(body.zoominfo.zoominfo_contact_id)
            if (
                zi_contact_id
                and "zoominfo_contact_id" in contact_cols
                and not _row_text(row, "zoominfo_contact_id")
            ):
                updates.append("zoominfo_contact_id = ?")
                params.append(zi_contact_id)
            if "source" in contact_cols:
                updates.append("source = ?")
                params.append(SOURCE_ZOOMINFO)
            if "source_updated_at" in contact_cols:
                updates.append("source_updated_at = ?")
                params.append(retrieved_at)
            params.append(contact_id)
            if updates:
                conn.execute(
                    f"UPDATE contacts SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                if "phone" in changed or "alt_phone" in changed:
                    refreshed = conn.execute(
                        "SELECT phone, alt_phone FROM contacts WHERE id = ?",
                        (contact_id,),
                    ).fetchone()
                    upsert_contact_phone_keys(
                        conn,
                        contact_id,
                        _row_text(refreshed, "phone") if refreshed else "",
                        _row_text(refreshed, "alt_phone") if refreshed else "",
                    )

            rel = conn.execute(
                """
                SELECT id, external_record_no FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (client_id, company_id),
            ).fetchone()
            if rel is None:
                raise LookupError("Company is not assigned to this client.")
            _assert_other_ccrs_unchanged(conn, company_id, client_id, other_before)
            scoped_after = _snapshot_client_scoped(conn, client_id, contact_id, company_id)
            if scoped_after["workflow"] != scoped_before["workflow"]:
                raise RuntimeError("ZoomInfo update must not change contact workflow.")
            if scoped_after["notes"] != scoped_before["notes"]:
                raise RuntimeError("ZoomInfo update must not change notes.")
            if scoped_after["campaigns"] != scoped_before["campaigns"]:
                raise RuntimeError("ZoomInfo update must not change campaign membership.")
            if scoped_after["tasks"] != scoped_before["tasks"]:
                raise RuntimeError("ZoomInfo update must not change tasks.")

            activity_id = None
            if changed or company_relinked:
                field_list = ", ".join(changed) if changed else "company relink"
                activity_id = insert_activity_row(
                    conn,
                    client_id=client_id,
                    company_id=company_id,
                    relationship_id=int(rel["id"]),
                    external_record_no=_blank(rel["external_record_no"]),
                    user_id=int(user.id),
                    contact_id=contact_id,
                    activity_type="Contact Updated from ZoomInfo",
                    notes=(
                        f"{created_by} updated contact {contact_id} from ZoomInfo. "
                        f"Fields changed: {field_list}."
                    ),
                    created_by=created_by,
                    assigned_user=created_by,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return ZoomInfoContactApplyResult(
        ok=True,
        message="ZoomInfo values applied for the selected fields."
        if changed
        else "No field values changed. ZoomInfo source metadata was stored.",
        contact_id=contact_id,
        client_id=client_id,
        company_id=company_id,
        changed_fields=changed,
        activity_id=activity_id,
        company_relinked=company_relinked,
    )


def preview_zoominfo_add(body: ZoomInfoAddPreviewRequest) -> dict[str, Any]:
    _user, client_id = _require_user_and_client(body.client_id)
    kind = _blank(body.kind).lower() or "contact"
    snap = body.zoominfo
    with get_connection() as conn:
        if kind == "company":
            from manual_company_data import find_company_matches

            matches = find_company_matches(
                conn,
                client_id=client_id,
                company_name=_blank(snap.company_name),
                website=_blank(snap.website),
                address=_blank(snap.address),
                city=_blank(snap.city),
                state=_blank(snap.state),
                phone=_blank(snap.phone),
                zoominfo_company_id=_blank(snap.zoominfo_company_id),
            )
            high = any(m["confidence"] == "high" for m in matches)
            return {
                "kind": "company",
                "client_id": client_id,
                "matches": matches,
                "can_create": not high,
                "requires_create_confirmation": bool(matches) and not high,
                "message": (
                    "A likely ZoomInfo company match exists. Link it instead of creating a duplicate."
                    if high
                    else (
                        "A possible match exists. Link it, or confirm creating a new company."
                        if matches
                        else "No existing company match. You can add this ZoomInfo company."
                    )
                ),
            }

        company_id = int(body.company_id) if body.company_id else 0
        if company_id <= 0:
            raise ValueError("company_id is required to add a ZoomInfo contact.")
        from manual_contact_data import find_manual_contact_matches

        matches = find_manual_contact_matches(
            conn,
            client_id=client_id,
            company_id=company_id,
            first_name=_blank(snap.first_name),
            last_name=_blank(snap.last_name),
            title=_blank(snap.title),
            email=_blank(snap.email),
            phone=_blank(snap.phone),
            alt_phone=_alt_phone_from_snapshot(snap),
        )
        high = any(m["confidence"] == "high" for m in matches)
        return {
            "kind": "contact",
            "client_id": client_id,
            "company_id": company_id,
            "matches": matches,
            "can_create": not high,
            "requires_create_confirmation": bool(matches) and not high,
            "message": (
                "A likely match exists. Link the existing contact instead of creating a duplicate."
                if high
                else (
                    "A possible match exists. Link it, or confirm creating a new contact."
                    if matches
                    else "No existing contact match. You can add this ZoomInfo person."
                )
            ),
        }


def save_zoominfo_add(body: ZoomInfoAddSaveRequest) -> ZoomInfoAddResult:
    user, client_id = _require_user_and_client(body.client_id)
    kind = _blank(body.kind).lower() or "contact"
    action = _blank(body.action).lower()
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    snap = body.zoominfo
    if kind == "company":
        from manual_company_data import save_manual_company
        from models import ManualCompanySaveRequest

        result = save_manual_company(
            ManualCompanySaveRequest(
                client_id=client_id,
                action=action,
                existing_company_id=body.existing_company_id,
                company_name=_blank(snap.company_name),
                website=_blank(snap.website),
                address=_blank(snap.address),
                city=_blank(snap.city),
                state=_blank(snap.state),
                zip=_blank(snap.zip),
                phone=_blank(snap.phone),
                industry=_blank(snap.industry),
                employee_size=_blank(snap.employee_size),
                sales_volume=_blank(snap.sales_volume),
                confirm_create_despite_match=body.confirm_create_despite_match,
                created_by=created_by,
                source=SOURCE_ZOOMINFO,
                zoominfo_company_id=_blank(snap.zoominfo_company_id),
            )
        )
        if result.company_id and _blank(snap.zoominfo_company_id) and action == "link":
            with get_connection() as conn:
                cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(companies)")}
                if "zoominfo_company_id" in cols:
                    conn.execute(
                        """
                        UPDATE companies
                        SET zoominfo_company_id = ?, source = ?, source_updated_at = ?
                        WHERE id = ? AND TRIM(COALESCE(zoominfo_company_id, '')) = ''
                        """,
                        (
                            _blank(snap.zoominfo_company_id),
                            SOURCE_ZOOMINFO,
                            _now(),
                            result.company_id,
                        ),
                    )
                    conn.commit()
        return ZoomInfoAddResult(
            ok=True,
            action=result.action,
            message=result.message,
            kind="company",
            company_id=result.company_id,
            client_id=client_id,
            external_record_no=result.external_record_no,
            activity_id=result.activity_id,
        )

    from manual_contact_data import save_manual_contact
    from models import ManualContactSaveRequest

    company_id = int(body.company_id) if body.company_id else 0
    if action == "link":
        if body.existing_contact_id is None or int(body.existing_contact_id) <= 0:
            raise ValueError("existing_contact_id is required to link.")
        with get_connection() as conn:
            crow = conn.execute(
                "SELECT company_id FROM contacts WHERE id = ?",
                (int(body.existing_contact_id),),
            ).fetchone()
            if crow is None:
                raise LookupError("Contact not found.")
            company_id = int(crow["company_id"])
    elif company_id <= 0:
        raise ValueError("company_id is required to create a ZoomInfo contact.")
    if action == "create" and not body.confirm_create_despite_match:
        preview = preview_zoominfo_add(
            ZoomInfoAddPreviewRequest(
                client_id=client_id,
                company_id=company_id,
                zoominfo=snap,
                kind="contact",
            )
        )
        if not preview["can_create"]:
            raise ValueError(preview["message"])
        if preview["requires_create_confirmation"]:
            raise ValueError(preview["message"])

    result = save_manual_contact(
        ManualContactSaveRequest(
            client_id=client_id,
            company_id=company_id,
            action=action,
            existing_contact_id=body.existing_contact_id,
            first_name=_blank(snap.first_name),
            last_name=_blank(snap.last_name),
            title=_blank(snap.title),
            email=_blank(snap.email),
            phone=_blank(snap.phone),
            alt_phone=_alt_phone_from_snapshot(snap),
            confirm_without_contact_info=not bool(
                _blank(snap.email) or _blank(snap.phone) or _alt_phone_from_snapshot(snap)
            ),
            created_by=created_by,
            source=SOURCE_ZOOMINFO,
            zoominfo_contact_id=_blank(snap.zoominfo_contact_id),
            linkedin_url=_blank(snap.linkedin_url),
            location=_blank(snap.location),
        )
    )
    if result.contact_id and _blank(snap.zoominfo_contact_id) and action == "link":
        with get_connection() as conn:
            cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(contacts)")}
            if "zoominfo_contact_id" in cols:
                conn.execute(
                    """
                    UPDATE contacts
                    SET zoominfo_contact_id = ?, source = ?, source_updated_at = ?
                    WHERE id = ? AND TRIM(COALESCE(zoominfo_contact_id, '')) = ''
                    """,
                    (
                        _blank(snap.zoominfo_contact_id),
                        SOURCE_ZOOMINFO,
                        _now(),
                        result.contact_id,
                    ),
                )
                conn.commit()
    return ZoomInfoAddResult(
        ok=True,
        action=result.action,
        message=result.message,
        kind="contact",
        company_id=result.company_id,
        contact_id=result.contact_id,
        client_id=client_id,
        activity_id=result.activity_id,
    )
