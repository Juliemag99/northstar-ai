"""Manual CRM company create/link for Prospects, Companies, Research, and campaigns.

Creates or links a shared master `companies` row plus an explicit client relationship.
Never infers Carmeco. Does not use Client Knowledge.
"""

from __future__ import annotations

from staff_context import resolve_staff_actor

from typing import Any

from access import get_default_user, require_write_client_id, user_can_access_client
from activities_data import insert_activity_row
from company_match import (
    compact_company_name,
    find_scored_matches,
    names_match,
)
from contact_phone import store_phone_parts
from db import get_connection
from models import (
    ManualCompanyMatch,
    ManualCompanyPreviewRequest,
    ManualCompanyPreviewResponse,
    ManualCompanySaveRequest,
    ManualCompanySaveResult,
)

DUPLICATE_MUST_LINK_MSG = (
    "A likely match exists. Open or link the existing company instead of creating a duplicate."
)
CONFIRM_POSSIBLE_MSG = (
    "A possible match exists. Link it, or confirm Create New Anyway."
)
MATCHES_BLOCK_CREATE_MSG = CONFIRM_POSSIBLE_MSG
NO_MATCHES_MSG = "No likely duplicates found"


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _require_user_and_client(client_id: object) -> tuple[Any, int]:
    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    cid = require_write_client_id(client_id, user_id=user.id)
    if not user_can_access_client(user.id, cid) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")
    return user, cid


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


def _already_assigned(conn, company_id: int, client_id: int) -> bool:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(client_company_relationships)")}
    archived_sql = "AND TRIM(COALESCE(archived_at,'')) = ''" if "archived_at" in cols else ""
    row = conn.execute(
        f"""
        SELECT 1 FROM client_company_relationships
        WHERE company_id = ? AND client_id = ? {archived_sql}
        LIMIT 1
        """,
        (company_id, client_id),
    ).fetchone()
    return row is not None


def find_company_matches(
    conn,
    *,
    client_id: int,
    company_name: str,
    website: str = "",
    address: str = "",
    city: str = "",
    state: str = "",
    phone: str = "",
    external_record_no: str = "",
    zoominfo_company_id: str = "",
    include_ai: bool = True,
) -> list[dict[str, Any]]:
    """Scored duplicate shortlist. AI review is annotation-only and never writes."""
    zi = _blank(zoominfo_company_id)
    if zi:
        try:
            row = conn.execute(
                """
                SELECT id, company_name, external_record_no, website, address, city, state, zip,
                       legacy_phone
                FROM companies
                WHERE TRIM(zoominfo_company_id) = ?
                LIMIT 1
                """,
                (zi,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            from company_match import _pack_row, score_company_pair, _already_assigned as _asg
            from company_match import _client_relationships as _rels

            packed = _pack_row(row)
            query = {
                "company_name": _blank(company_name),
                "website": _blank(website),
                "address": _blank(address),
                "city": _blank(city),
                "state": _blank(state),
                "phone": _blank(phone),
                "external_record_no": _blank(external_record_no),
            }
            score, reasons = score_company_pair(query, packed)
            reasons = list(dict.fromkeys(["zoominfo_company_id", *reasons]))
            packed.update(
                {
                    "reasons": reasons,
                    "confidence": "high",
                    "score": max(score, 100),
                    "already_assigned": _asg(conn, int(packed["company_id"]), client_id),
                    "client_relationships": _rels(conn, int(packed["company_id"])),
                    "ai_assessment": "",
                    "ai_explanation": "",
                    "ai_confidence": "",
                    "ai_available": False,
                }
            )
            others = find_scored_matches(
                conn,
                client_id=client_id,
                company_name=company_name,
                website=website,
                address=address,
                city=city,
                state=state,
                phone=phone,
                external_record_no=external_record_no,
                include_ai=include_ai,
            )
            merged = {int(packed["company_id"]): packed}
            for item in others:
                if int(item["company_id"]) != int(packed["company_id"]):
                    merged[int(item["company_id"])] = item
            ranked = list(merged.values())
            ranked.sort(key=lambda m: (-int(m.get("score") or 0), m["company_name"].lower()))
            return ranked[:8]
    return find_scored_matches(
        conn,
        client_id=client_id,
        company_name=company_name,
        website=website,
        address=address,
        city=city,
        state=state,
        phone=phone,
        external_record_no=external_record_no,
        include_ai=include_ai,
    )


def preview_manual_company(body: ManualCompanyPreviewRequest) -> ManualCompanyPreviewResponse:
    _user, client_id = _require_user_and_client(body.client_id)
    name = _blank(body.company_name)
    if not name:
        raise ValueError("Company name is required.")
    with get_connection() as conn:
        matches = find_company_matches(
            conn,
            client_id=client_id,
            company_name=name,
            website=_blank(body.website),
            address=_blank(body.address),
            city=_blank(body.city),
            state=_blank(body.state),
            phone=_blank(body.phone),
            external_record_no=_blank(body.external_record_no),
            include_ai=True,
        )
    has_matches = bool(matches)
    ai_available = any(bool(m.get("ai_available")) for m in matches)
    return ManualCompanyPreviewResponse(
        matches=[ManualCompanyMatch(**m) for m in matches],
        can_create=not has_matches,
        requires_create_confirmation=has_matches,
        message=MATCHES_BLOCK_CREATE_MSG if has_matches else NO_MATCHES_MSG,
        ai_available=ai_available,
    )


def _link_company_on_conn(
    conn,
    *,
    client_id: int,
    company_id: int,
    user,
    created_by: str,
    notes: str,
    typed_name: str = "",
    typed_record_no: str = "",
    typed_address: str = "",
    typed_city: str = "",
    typed_state: str = "",
    typed_zip: str = "",
    typed_phone: str = "",
    typed_website: str = "",
) -> dict[str, Any]:
    from company_aliases import SOURCE_MANUAL, should_store_alias, upsert_company_alias
    from crm_add_data import ensure_client_relationship
    from crm_identity_keys import normalize_record_no

    row = conn.execute(
        "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Company not found.")
    other_before = _snapshot_other_ccrs(conn, company_id, client_id)
    already = _already_assigned(conn, company_id, client_id)
    client_name = _blank(
        conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()["name"]
    )
    company_name = _blank(row["company_name"])
    record_no = _blank(row["external_record_no"])
    incoming_rn = normalize_record_no(typed_record_no)
    ccr_rn = incoming_rn or record_no
    rel_id, created, _status = ensure_client_relationship(
        conn,
        client_id=client_id,
        company_id=company_id,
        external_record_no=ccr_rn,
        user_id=int(user.id),
        provenance_note=_blank(notes) or f"Linked by {created_by}.",
    )
    if created:
        from data_steward import ENTITY_CLIENT_RELATIONSHIP, record_populated_creates, source_ref_manual

        record_populated_creates(
            conn,
            entity_type=ENTITY_CLIENT_RELATIONSHIP,
            entity_id=int(rel_id),
            fields={"status": _status or "New", "company_id": str(company_id)},
            actor=user,
            source_ref=source_ref_manual(),
            action="CREATE",
            client_id=client_id,
        )
    if should_store_alias(
        source_system=SOURCE_MANUAL,
        alias_name=_blank(typed_name),
        canonical_name=company_name,
        source_record_no=incoming_rn,
        master_record_no=record_no,
    ):
        upsert_company_alias(
            conn,
            company_id=company_id,
            alias_name=_blank(typed_name),
            source_system=SOURCE_MANUAL,
            source_record_no=incoming_rn,
            client_id=client_id,
            source_address=_blank(typed_address),
            source_city=_blank(typed_city),
            source_state=_blank(typed_state),
            source_zip=_blank(typed_zip),
            source_phone=_blank(typed_phone),
            source_website=_blank(typed_website),
            created_by_user_id=int(user.id),
        )
    _assert_other_ccrs_unchanged(conn, company_id, client_id, other_before)
    if already and not created:
        return {
            "ok": True,
            "action": "linked",
            "message": f"{company_name} is already assigned to {client_name}.",
            "company_id": company_id,
            "client_id": client_id,
            "external_record_no": record_no,
            "activity_id": None,
            "already_assigned": True,
        }
    activity_id = insert_activity_row(
        conn,
        client_id=client_id,
        company_id=company_id,
        relationship_id=int(rel_id),
        external_record_no=record_no,
        user_id=int(user.id),
        contact_id=None,
        activity_type="Company Linked",
        notes=(
            f"{created_by} linked company {company_name} (company_id {company_id}) "
            f"to {client_name}."
        ),
        created_by=created_by,
        assigned_user=created_by,
    )
    return {
        "ok": True,
        "action": "linked",
        "message": f"{company_name} linked to {client_name}.",
        "company_id": company_id,
        "client_id": client_id,
        "external_record_no": record_no,
        "activity_id": activity_id,
        "already_assigned": False,
    }


def save_manual_company(body: ManualCompanySaveRequest) -> ManualCompanySaveResult:
    user, client_id = _require_user_and_client(body.client_id)
    action = _blank(body.action).lower()
    if action not in {"create", "link"}:
        raise ValueError("action must be create or link.")
    created_by = _blank(body.created_by) or _blank(user.full_name) or "Julie Magnani"
    name = _blank(body.company_name)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if action == "link":
                if body.existing_company_id is None or int(body.existing_company_id) <= 0:
                    raise ValueError("existing_company_id is required to link a company.")
                result = _link_company_on_conn(
                    conn,
                    client_id=client_id,
                    company_id=int(body.existing_company_id),
                    user=user,
                    created_by=created_by,
                    notes=_blank(body.notes),
                    typed_name=name,
                    typed_record_no=_blank(body.external_record_no),
                    typed_address=_blank(body.address),
                    typed_city=_blank(body.city),
                    typed_state=_blank(body.state),
                    typed_zip=_blank(body.zip),
                    typed_phone=_blank(body.phone),
                    typed_website=_blank(body.website),
                )
                conn.commit()
                return ManualCompanySaveResult(**result)

            if not name:
                raise ValueError("Company name is required.")
            matches = find_company_matches(
                conn,
                client_id=client_id,
                company_name=name,
                website=_blank(body.website),
                address=_blank(body.address),
                city=_blank(body.city),
                state=_blank(body.state),
                phone=_blank(body.phone),
                external_record_no=_blank(body.external_record_no),
                zoominfo_company_id=_blank(body.zoominfo_company_id),
                include_ai=False,
            )
            if matches and not body.confirm_create_despite_match:
                raise ValueError(MATCHES_BLOCK_CREATE_MSG)

            from crm_add_data import allocate_ns_record_no, ensure_client_relationship

            requested_rn = _blank(body.external_record_no)
            if requested_rn:
                taken = conn.execute(
                    "SELECT id FROM companies WHERE TRIM(external_record_no) = ? LIMIT 1",
                    (requested_rn,),
                ).fetchone()
                if taken is not None:
                    raise ValueError(
                        "That Record No. already belongs to an existing company. Link it instead."
                    )
                record_no = requested_rn
            else:
                record_no = allocate_ns_record_no(conn)

            from datetime import datetime

            now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
            columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(companies)")}
            insert_cols = [
                "external_record_no",
                "company_name",
                "address",
                "city",
                "state",
                "zip",
                "website",
                "legacy_phone",
                "type_of_industry",
                "employee_size_range",
                "sales_volume_range",
                "created_at",
                "last_updated_at",
            ]
            phone_main, phone_ext = store_phone_parts(_blank(body.phone), field="legacy_phone")
            insert_vals: list[object] = [
                record_no,
                name,
                _blank(body.address),
                _blank(body.city),
                _blank(body.state),
                _blank(body.zip),
                _blank(body.website),
                phone_main,
                _blank(body.industry),
                _blank(body.employee_size),
                _blank(body.sales_volume),
                now,
                now,
            ]
            source_value = _blank(body.source) or "manual"
            if "legacy_phone_extension" in columns:
                insert_cols.insert(-2, "legacy_phone_extension")
                insert_vals.insert(-2, phone_ext or None)
            if "source" in columns:
                insert_cols.insert(-2, "source")
                insert_vals.insert(-2, source_value)
            zi_company_id = _blank(body.zoominfo_company_id)
            if zi_company_id and "zoominfo_company_id" in columns:
                insert_cols.insert(-2, "zoominfo_company_id")
                insert_vals.insert(-2, zi_company_id)
            if source_value.lower() == "zoominfo" and "source_updated_at" in columns:
                insert_cols.insert(-2, "source_updated_at")
                insert_vals.insert(-2, now)
            placeholders = ", ".join("?" for _ in insert_cols)
            cur = conn.execute(
                f"INSERT INTO companies ({', '.join(insert_cols)}) VALUES ({placeholders})",
                insert_vals,
            )
            company_id = int(cur.lastrowid)
            from data_steward import (
                ENTITY_CLIENT_RELATIONSHIP,
                ENTITY_COMPANY,
                record_populated_creates,
                source_ref_manual,
            )

            record_populated_creates(
                conn,
                entity_type=ENTITY_COMPANY,
                entity_id=company_id,
                fields={
                    "company_name": name,
                    "address": _blank(body.address),
                    "city": _blank(body.city),
                    "state": _blank(body.state),
                    "zip": _blank(body.zip),
                    "phone": phone_main,
                    "phone_extension": phone_ext or "",
                    "website": _blank(body.website),
                },
                actor=user,
                source_ref=source_ref_manual(),
                action="CREATE",
                client_id=client_id,
            )
            rel_id, _created, _status = ensure_client_relationship(
                conn,
                client_id=client_id,
                company_id=company_id,
                external_record_no=record_no,
                user_id=int(user.id),
                provenance_note=_blank(body.notes) or f"Created by {created_by}.",
            )
            record_populated_creates(
                conn,
                entity_type=ENTITY_CLIENT_RELATIONSHIP,
                entity_id=int(rel_id),
                fields={"status": _status or "New", "company_id": str(company_id)},
                actor=user,
                source_ref=source_ref_manual(),
                action="CREATE",
                client_id=client_id,
            )
            client_name = _blank(
                conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()["name"]
            )
            activity_id = insert_activity_row(
                conn,
                client_id=client_id,
                company_id=company_id,
                relationship_id=int(rel_id),
                external_record_no=record_no,
                user_id=int(user.id),
                contact_id=None,
                activity_type="Company Created",
                notes=(
                    f"{created_by} created company {name} (company_id {company_id}) "
                    f"for {client_name}."
                ),
                created_by=created_by,
                assigned_user=created_by,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return ManualCompanySaveResult(
        ok=True,
        action="created",
        message=f"{name} created for {client_name}.",
        company_id=company_id,
        client_id=client_id,
        external_record_no=record_no,
        activity_id=activity_id,
        already_assigned=False,
    )
