"""Isolated LeadMaster refresh apply.

BEGIN IMMEDIATE. Any exception → ROLLBACK. Stale fingerprint → refuse.
Live production databases are refused by the confirm wrapper.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from client_data_history_notes import compute_history_event_hash
from company_aliases import SOURCE_CRM_IMPORT, capture_import_company_alias
from company_locations import upsert_company_source_identity
from company_merges import resolve_company_id
from contact_phone import store_phone_parts
from crm_add_data import allocate_ns_record_no
from crm_import_status_notes import append_imported_notes
from leadmaster_refresh_plan import (
    ADDITIVE_UPDATE,
    ASSIGNMENT_CHANGE,
    CAMPAIGN_CHANGE,
    CONFLICT,
    NEW_COMPANY,
    NEW_CONTACT,
    NEW_HISTORY,
    NEW_NOTE,
    NEW_RELATIONSHIP,
    SAFE_CANONICAL_UPDATE,
    SOURCE_ID_CONFLICT,
    SOURCE_SYSTEM,
    STATUS_AUTHORITATIVE,
    STATUS_CHANGE,
    fingerprint_refresh_plan,
    plan_leadmaster_refresh,
)
from leadmaster_refresh_resolutions import (
    ACCEPT_PROPOSED,
    CREATE_NEW_COMPANY,
    CREATE_NEW_CONTACT,
    KEEP_EXISTING,
    LINK_EXISTING_COMPANY,
    LINK_EXISTING_CONTACT,
    MAP_SOURCE_REP,
    resolution_for,
    row_is_skipped,
)
from shared_note_history_import import normalize_record_no


class RefreshApplyError(ValueError):
    pass


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_fresh_fingerprint(plan: dict[str, Any]) -> None:
    stored = _blank(plan.get("plan_fingerprint"))
    recomputed = fingerprint_refresh_plan(plan)
    if stored != recomputed:
        raise RefreshApplyError("stale_or_tampered_plan")


def refuse_if_blocking(plan: dict[str, Any], resolutions: list | None = None) -> None:
    resolutions = resolutions or plan.get("resolutions") or []
    for row in plan.get("rows") or []:
        source_row = int(row.get("source_row") or 0)
        if row_is_skipped(resolutions, source_row):
            continue
        if SOURCE_ID_CONFLICT in (row.get("classifications") or []):
            raise RefreshApplyError("source_id_conflict")
        if "ARCHIVED_MATCH_REVIEW" in (row.get("classifications") or []):
            raise RefreshApplyError("archived_match")
        if "RESTORE_RELATIONSHIP_REVIEW" in (row.get("classifications") or []):
            raise RefreshApplyError("archived_relationship")
        if not row.get("blocking"):
            continue
        unresolved = False
        for prop in row.get("proposals") or []:
            if not (prop.get("blocking") or prop.get("action") == CONFLICT):
                continue
            found = resolution_for(
                resolutions,
                source_row=source_row,
                field=_blank(prop.get("field")),
                action=_blank(prop.get("action")),
            )
            if found is None:
                unresolved = True
                break
        if unresolved:
            raise RefreshApplyError("blocking_issues")


def _user_may_be_assigned(conn, user_id: int, client_id: int) -> bool:
    user = conn.execute(
        "SELECT id, active, is_administrator FROM users WHERE id = ?",
        (int(user_id),),
    ).fetchone()
    if user is None or not bool(user["active"]):
        return False
    if bool(user["is_administrator"]):
        return True
    row = conn.execute(
        """
        SELECT 1 FROM user_client_assignments
        WHERE user_id = ? AND client_id = ? AND active = 1
        """,
        (int(user_id), int(client_id)),
    ).fetchone()
    return row is not None


def _empty_counts() -> dict[str, int]:
    return {
        "unchanged_rows": 0,
        "companies_created": 0,
        "companies_updated": 0,
        "relationships_created": 0,
        "contacts_created": 0,
        "contacts_updated": 0,
        "statuses_updated": 0,
        "notes_added": 0,
        "history_added": 0,
        "assignments_changed": 0,
        "campaign_memberships_added": 0,
        "source_identities_added": 0,
        "rows_skipped": 0,
        "rows_reviewed_resolved": 0,
        "conflicts": 0,
        "errors": 0,
    }


def _audit(
    conn,
    *,
    batch_id: int | None,
    client_id: int,
    source_row: int,
    company_id: int | None,
    contact_id: int | None,
    ccr_id: int | None,
    field: str,
    old_value: str,
    new_value: str,
    action: str,
    skip_reason: str,
    actor_id: int | None,
    fingerprint: str,
    confirmed_at: str,
) -> None:
    if not batch_id:
        return
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leadmaster_refresh_audit_events'"
    ).fetchone() is None:
        return
    conn.execute(
        """
        INSERT INTO leadmaster_refresh_audit_events (
            batch_id, client_id, source_row, company_id, contact_id, ccr_id,
            field, old_value, new_value, action, skip_reason,
            confirmed_by_user_id, confirmed_at, plan_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(batch_id),
            int(client_id),
            int(source_row),
            int(company_id) if company_id else None,
            int(contact_id) if contact_id else None,
            int(ccr_id) if ccr_id else None,
            field,
            old_value or "",
            new_value or "",
            action,
            skip_reason or "",
            int(actor_id) if actor_id else None,
            confirmed_at,
            fingerprint,
        ),
    )
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_audit_log'"
    ).fetchone() is None:
        return
    rn = ""
    if company_id:
        row = conn.execute(
            "SELECT COALESCE(external_record_no,'') FROM companies WHERE id = ?",
            (int(company_id),),
        ).fetchone()
        rn = _blank(row[0] if row else "")
    conn.execute(
        """
        INSERT INTO field_audit_log (
            client_code, external_record_no, field_name,
            old_value, new_value, changed_by, changed_at
        ) VALUES ('leadmaster_refresh', ?, ?, ?, ?, ?, ?)
        """,
        (
            rn,
            field,
            old_value or "",
            new_value or "",
            f"user_id:{actor_id}" if actor_id else "leadmaster_refresh",
            confirmed_at,
        ),
    )


def _lm_prov(
    conn,
    *,
    entity_type: str,
    entity_id: int | None,
    field: str,
    old_value: object = "",
    new_value: object = "",
    actor_id: int | None,
    batch_id: int | None,
    action: str,
    client_id: int | None = None,
) -> None:
    if not entity_id:
        return
    from data_steward import SOURCE_LEADMASTER, record_provenance, source_ref_batch

    record_provenance(
        conn,
        entity_type=entity_type,
        entity_id=int(entity_id),
        field=field,
        old_value=old_value,
        new_value=new_value,
        source_type=SOURCE_LEADMASTER,
        changed_by_user_id=actor_id,
        action=action,
        source_ref=source_ref_batch("leadmaster_refresh_batch", batch_id),
        client_id=client_id,
        trusted=True,
    )


def _resolve_identity_company(conn, *, company_id: int, record_no: str, client_id: int) -> int:
    survivor = resolve_company_id(conn, int(company_id))
    ident = conn.execute(
        """
        SELECT id, company_id FROM company_source_identities
        WHERE source_system = ? AND source_record_no = ?
          AND COALESCE(client_id, 0) = COALESCE(?, 0)
        LIMIT 1
        """,
        (SOURCE_SYSTEM, normalize_record_no(record_no), int(client_id)),
    ).fetchone() if record_no else None
    if ident is None:
        return survivor
    existing = int(ident["company_id"])
    if existing == survivor:
        return survivor
    if resolve_company_id(conn, existing) == survivor:
        conn.execute(
            "UPDATE company_source_identities SET company_id = ? WHERE id = ?",
            (int(survivor), int(ident["id"])),
        )
        return survivor
    raise RefreshApplyError("source_id_conflict")


def apply_leadmaster_refresh(
    conn,
    *,
    incoming_rows: list,
    policy,
    source_sha256: str,
    source_filename: str,
    expected_fingerprint: str,
    force_fail_after: str = "",
    resolutions: list | None = None,
    batch_id: int | None = None,
    actor_id: int | None = None,
    manage_transaction: bool = True,
) -> dict[str, Any]:
    """Apply a refresh plan atomically. Isolated DBs only."""
    counts = _empty_counts()
    confirmed_at = _now()
    from company_merges import existing_user_id

    actor_id = existing_user_id(conn, actor_id)
    if manage_transaction:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # Caller already has an open transaction (SELECT/DML on this connection).
            manage_transaction = False
    try:
        plan = plan_leadmaster_refresh(
            conn,
            client_id=int(policy.client_id),
            rows=incoming_rows,
            policy=policy,
            source_sha256=source_sha256,
            source_filename=source_filename,
        )
        frozen = list(resolutions or [])
        plan["resolutions"] = frozen
        plan["plan_fingerprint"] = fingerprint_refresh_plan(plan)
        if plan["plan_fingerprint"] != expected_fingerprint:
            raise RefreshApplyError("stale_plan")
        assert_fresh_fingerprint(plan)
        refuse_if_blocking(plan, frozen)

        by_row = {int(r["source_row"]): r for r in plan["rows"]}
        incoming_by_row = {int(r.source_row): r for r in incoming_rows}
        for source_row in sorted(by_row):
            row = by_row[source_row]
            incoming = incoming_by_row[source_row]
            if row_is_skipped(frozen, source_row):
                counts["rows_skipped"] += 1
                _audit(
                    conn, batch_id=batch_id, client_id=int(policy.client_id),
                    source_row=source_row, company_id=None, contact_id=None, ccr_id=None,
                    field="row", old_value="", new_value="", action="SKIP_SOURCE_ROW",
                    skip_reason="resolved_skip", actor_id=actor_id,
                    fingerprint=plan["plan_fingerprint"], confirmed_at=confirmed_at,
                )
                continue

            company_id = row.get("company_id")
            link_company = resolution_for(
                frozen, source_row=source_row, action="REVIEW_REQUIRED"
            )
            if link_company and link_company["resolution"] == LINK_EXISTING_COMPANY:
                payload_id = link_company.get("payload", {}).get("company_id")
                if not payload_id:
                    raise RefreshApplyError("missing_link_company_id")
                company_id = resolve_company_id(conn, int(payload_id))
            elif link_company and link_company["resolution"] == CREATE_NEW_COMPANY:
                row["classifications"] = list(row.get("classifications") or []) + [NEW_COMPANY]

            if NEW_COMPANY in row["classifications"] and not company_id:
                rn = normalize_record_no(incoming.record_no)
                phone, ext = store_phone_parts(incoming.company_phone, field="legacy_phone")
                cur = conn.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_phone, legacy_phone_extension, type_of_industry,
                        created_at, last_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', datetime('now'), datetime('now'))
                    """,
                    (
                        rn or allocate_ns_record_no(conn),
                        incoming.company_name,
                        incoming.address,
                        incoming.city,
                        incoming.state,
                        incoming.zip,
                        incoming.website,
                        phone,
                        ext or None,
                    ),
                )
                company_id = int(cur.lastrowid)
                counts["companies_created"] += 1
                _lm_prov(
                    conn,
                    entity_type="company",
                    entity_id=company_id,
                    field="company_name",
                    new_value=incoming.company_name,
                    actor_id=actor_id,
                    batch_id=batch_id,
                    action="CREATE",
                    client_id=int(policy.client_id),
                )
                _audit(
                    conn, batch_id=batch_id, client_id=int(policy.client_id),
                    source_row=source_row, company_id=company_id, contact_id=None, ccr_id=None,
                    field="company", old_value="", new_value=incoming.company_name,
                    action=NEW_COMPANY, skip_reason="", actor_id=actor_id,
                    fingerprint=plan["plan_fingerprint"], confirmed_at=confirmed_at,
                )
                if force_fail_after == "company":
                    raise RefreshApplyError("forced_rollback")

            if company_id:
                company_id = _resolve_identity_company(
                    conn,
                    company_id=int(company_id),
                    record_no=incoming.record_no,
                    client_id=int(policy.client_id),
                )
                ident = upsert_company_source_identity(
                    conn,
                    company_id=int(company_id),
                    source_system=SOURCE_SYSTEM,
                    source_record_no=normalize_record_no(incoming.record_no)
                    or str(company_id),
                    client_id=int(policy.client_id),
                    source_company_name=incoming.company_name,
                    source_address=incoming.address,
                    source_city=incoming.city,
                    source_state=incoming.state,
                    source_zip=incoming.zip,
                    source_phone=incoming.company_phone,
                    source_website=incoming.website,
                    created_by_user_id=int(actor_id) if actor_id else None,
                )
                ident_id, ident_status = ident
                if ident_status == "conflict":
                    raise RefreshApplyError("source_id_conflict")
                if ident_status == "created":
                    counts["source_identities_added"] += 1
                capture_import_company_alias(
                    conn,
                    company_id=int(company_id),
                    mapped={
                        "company_name": incoming.company_name,
                        "external_record_no": normalize_record_no(incoming.record_no),
                        "address": incoming.address,
                        "city": incoming.city,
                        "state": incoming.state,
                        "zip": incoming.zip,
                        "phone": incoming.company_phone,
                        "website": incoming.website,
                    },
                    source_system=SOURCE_CRM_IMPORT,
                    client_id=int(policy.client_id),
                    source_row=int(incoming.source_row),
                )

            if company_id and NEW_RELATIONSHIP in row["classifications"]:
                existing = conn.execute(
                    """
                    SELECT id FROM client_company_relationships
                    WHERE client_id = ? AND company_id = ?
                    """,
                    (int(policy.client_id), int(company_id)),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO client_company_relationships (
                            client_id, company_id, external_record_no, status, notes,
                            assigned_user_id
                        ) VALUES (?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            int(policy.client_id),
                            int(company_id),
                            normalize_record_no(incoming.record_no),
                            _blank(incoming.status) or "New",
                            "",
                        ),
                    )
                    counts["relationships_created"] += 1

            ccr = conn.execute(
                """
                SELECT id, notes, status, assigned_user_id FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (int(policy.client_id), int(company_id) if company_id else -1),
            ).fetchone() if company_id else None

            contact_id = row.get("contact_id")
            link_contact = resolution_for(
                frozen, source_row=source_row, action="POSSIBLE_DUPLICATE"
            )
            if link_contact and link_contact["resolution"] == LINK_EXISTING_CONTACT:
                payload_id = link_contact.get("payload", {}).get("contact_id")
                if payload_id:
                    contact_id = int(payload_id)
            elif link_contact and link_contact["resolution"] == CREATE_NEW_CONTACT:
                row["classifications"] = list(row.get("classifications") or []) + [NEW_CONTACT]
                contact_id = None

            for prop in row.get("proposals") or []:
                action = prop.get("action")
                field = prop.get("field")
                resolved = resolution_for(
                    frozen,
                    source_row=source_row,
                    field=str(field or ""),
                    action=str(action or ""),
                )
                if resolved and resolved["resolution"] == KEEP_EXISTING:
                    continue
                accept = bool(resolved and resolved["resolution"] == ACCEPT_PROPOSED)
                if action == "MANUAL_OVERRIDE_CONFLICT" and not accept:
                    continue
                if action == "MANUAL_OVERRIDE_CONFLICT" and accept:
                    new_v = prop.get("new_value")
                    if field == "status" and ccr is not None:
                        conn.execute(
                            "UPDATE client_company_relationships SET status = ? WHERE id = ?",
                            (new_v, int(ccr["id"])),
                        )
                        counts["statuses_updated"] += 1
                        _lm_prov(
                            conn, entity_type="client_relationship", entity_id=int(ccr["id"]),
                            field="status", old_value=prop.get("old_value"), new_value=new_v,
                            actor_id=actor_id, batch_id=batch_id, action="ACCEPT_PROPOSED",
                            client_id=int(policy.client_id),
                        )
                    elif field == "email" and (contact_id or row.get("contact_id")):
                        conn.execute(
                            "UPDATE contacts SET email = ? WHERE id = ?",
                            (new_v, int(contact_id or row["contact_id"])),
                        )
                        counts["contacts_updated"] += 1
                        _lm_prov(
                            conn, entity_type="contact",
                            entity_id=int(contact_id or row["contact_id"]),
                            field="email", old_value=prop.get("old_value"), new_value=new_v,
                            actor_id=actor_id, batch_id=batch_id, action="ACCEPT_PROPOSED",
                            client_id=int(policy.client_id),
                        )
                    elif field == "phone" and company_id:
                        phone, ext = store_phone_parts(str(new_v or ""), field="legacy_phone")
                        conn.execute(
                            "UPDATE companies SET legacy_phone = ?, legacy_phone_extension = ? WHERE id = ?",
                            (phone, ext or None, int(company_id)),
                        )
                        counts["companies_updated"] += 1
                        _lm_prov(
                            conn, entity_type="company", entity_id=int(company_id),
                            field="phone", old_value=prop.get("old_value"), new_value=phone,
                            actor_id=actor_id, batch_id=batch_id, action="ACCEPT_PROPOSED",
                            client_id=int(policy.client_id),
                        )
                    elif field == "company_name" and company_id:
                        conn.execute(
                            "UPDATE companies SET company_name = ? WHERE id = ?",
                            (new_v, int(company_id)),
                        )
                        counts["companies_updated"] += 1
                        _lm_prov(
                            conn, entity_type="company", entity_id=int(company_id),
                            field="company_name", old_value=prop.get("old_value"), new_value=new_v,
                            actor_id=actor_id, batch_id=batch_id, action="ACCEPT_PROPOSED",
                            client_id=int(policy.client_id),
                        )
                    continue

                if action == SAFE_CANONICAL_UPDATE and company_id:
                    if field == "phone":
                        phone, ext = store_phone_parts(
                            str(prop.get("new_value") or ""), field="legacy_phone"
                        )
                        conn.execute(
                            """
                            UPDATE companies
                            SET legacy_phone = ?, legacy_phone_extension = ?
                            WHERE id = ? AND TRIM(COALESCE(legacy_phone,'')) = ''
                            """,
                            (phone, ext or None, int(company_id)),
                        )
                    else:
                        col = {
                            "address": "address",
                            "city": "city",
                            "state": "state",
                            "zip": "zip",
                            "website": "website",
                        }.get(field)
                        if col:
                            conn.execute(
                                f"UPDATE companies SET {col} = ? WHERE id = ? AND TRIM(COALESCE({col},'')) = ''",
                                (prop.get("new_value"), int(company_id)),
                            )
                    counts["companies_updated"] += 1
                    _lm_prov(
                        conn, entity_type="company", entity_id=int(company_id),
                        field=str(field), old_value=prop.get("old_value"),
                        new_value=prop.get("new_value"), actor_id=actor_id,
                        batch_id=batch_id, action="SAFE_CANONICAL_UPDATE",
                        client_id=int(policy.client_id),
                    )
                    _audit(
                        conn, batch_id=batch_id, client_id=int(policy.client_id),
                        source_row=source_row, company_id=company_id, contact_id=contact_id,
                        ccr_id=int(ccr["id"]) if ccr else None, field=str(field),
                        old_value=str(prop.get("old_value") or ""),
                        new_value=str(prop.get("new_value") or ""),
                        action=SAFE_CANONICAL_UPDATE, skip_reason="", actor_id=actor_id,
                        fingerprint=plan["plan_fingerprint"], confirmed_at=confirmed_at,
                    )
                    if force_fail_after == "after_company_update":
                        raise RefreshApplyError("forced_rollback")

                if action == CONFLICT and accept and contact_id and field == "email":
                    conn.execute(
                        "UPDATE contacts SET email = ? WHERE id = ?",
                        (prop.get("new_value"), int(contact_id)),
                    )
                    counts["contacts_updated"] += 1

                if action == ADDITIVE_UPDATE and (contact_id or row.get("contact_id")):
                    cid = int(contact_id or row["contact_id"])
                    if field in {"phone", "alt_phone"}:
                        phone, ext = store_phone_parts(
                            str(prop.get("new_value") or ""), field=str(field)
                        )
                        ext_col = "phone_extension" if field == "phone" else "alt_phone_extension"
                        conn.execute(
                            f"""
                            UPDATE contacts SET {field} = ?, {ext_col} = ?
                            WHERE id = ? AND TRIM(COALESCE({field},'')) = ''
                            """,
                            (phone, ext or None, cid),
                        )
                    else:
                        col = {
                            "email": "email",
                            "title": "title",
                            "first_name": "first_name",
                            "last_name": "last_name",
                        }.get(field)
                        if col:
                            conn.execute(
                                f"UPDATE contacts SET {col} = ? WHERE id = ? AND TRIM(COALESCE({col},'')) = ''",
                                (prop.get("new_value"), cid),
                            )
                    counts["contacts_updated"] += 1
                    _audit(
                        conn, batch_id=batch_id, client_id=int(policy.client_id),
                        source_row=source_row, company_id=company_id, contact_id=cid,
                        ccr_id=int(ccr["id"]) if ccr else None, field=str(field),
                        old_value=str(prop.get("old_value") or ""),
                        new_value=str(prop.get("new_value") or ""),
                        action=ADDITIVE_UPDATE, skip_reason="", actor_id=actor_id,
                        fingerprint=plan["plan_fingerprint"], confirmed_at=confirmed_at,
                    )
                    if force_fail_after == "after_contact_update":
                        raise RefreshApplyError("forced_rollback")

                if action == NEW_NOTE and ccr is not None:
                    merged = append_imported_notes(ccr["notes"], incoming.notes)
                    conn.execute(
                        "UPDATE client_company_relationships SET notes = ? WHERE id = ?",
                        (merged, int(ccr["id"])),
                    )
                    counts["notes_added"] += 1
                    ccr = dict(ccr)
                    ccr["notes"] = merged
                    if force_fail_after == "after_note_history":
                        raise RefreshApplyError("forced_rollback")

                write_status = (
                    action == STATUS_CHANGE
                    and ccr is not None
                    and _blank(prop.get("new_value"))
                    and (
                        policy.status_mode == STATUS_AUTHORITATIVE
                        or accept
                    )
                )
                if write_status:
                    conn.execute(
                        "UPDATE client_company_relationships SET status = ? WHERE id = ?",
                        (prop.get("new_value"), int(ccr["id"])),
                    )
                    counts["statuses_updated"] += 1
                    _lm_prov(
                        conn, entity_type="client_relationship", entity_id=int(ccr["id"]),
                        field="status", old_value=prop.get("old_value"),
                        new_value=prop.get("new_value"), actor_id=actor_id,
                        batch_id=batch_id, action="STATUS_CHANGE",
                        client_id=int(policy.client_id),
                    )

                if action in {ASSIGNMENT_CHANGE, "UNKNOWN_SOURCE_REP"} and ccr is not None:
                    mapped = resolution_for(
                        frozen, source_row=source_row, action="UNKNOWN_SOURCE_REP"
                    )
                    new_id_raw = prop.get("new_value")
                    if mapped and mapped["resolution"] == MAP_SOURCE_REP:
                        new_id_raw = mapped.get("payload", {}).get("user_id")
                    if action == "UNKNOWN_SOURCE_REP" and not (
                        mapped and mapped["resolution"] == MAP_SOURCE_REP
                    ):
                        continue
                    try:
                        new_id = int(new_id_raw)
                    except (TypeError, ValueError):
                        raise RefreshApplyError("unmapped_assignment")
                    if not _user_may_be_assigned(conn, new_id, int(policy.client_id)):
                        raise RefreshApplyError("assignment_user_lacks_client_access")
                    conn.execute(
                        "UPDATE client_company_relationships SET assigned_user_id = ? WHERE id = ?",
                        (new_id, int(ccr["id"])),
                    )
                    counts["assignments_changed"] += 1
                    if force_fail_after == "after_assignment":
                        raise RefreshApplyError("forced_rollback")

                if action == NEW_HISTORY and company_id:
                    want = _blank(prop.get("new_value"))
                    matched_ev = None
                    for ev in incoming.history:
                        eh = compute_history_event_hash(
                            client_id=int(policy.client_id),
                            company_id=None,
                            company_record_no=normalize_record_no(incoming.record_no),
                            note_text=ev.note_text,
                            event_at=ev.event_at,
                            source_note_id=ev.source_note_id,
                        )
                        if eh == want:
                            matched_ev = ev
                            break
                    if matched_ev is None:
                        continue
                    conn.execute(
                        """
                        INSERT INTO company_shared_history_events (
                            company_id, external_record_no, source_company_name,
                            event_hash, note_text, author, event_type, event_at,
                            source_file, imported_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        """,
                        (
                            int(company_id),
                            normalize_record_no(incoming.record_no),
                            incoming.company_name,
                            want,
                            matched_ev.note_text,
                            matched_ev.author,
                            matched_ev.event_type,
                            matched_ev.event_at,
                            source_filename,
                        ),
                    )
                    counts["history_added"] += 1
                    if force_fail_after == "after_note_history":
                        raise RefreshApplyError("forced_rollback")

                if action == CAMPAIGN_CHANGE and company_id:
                    target_id = policy.campaign_target_map.get(incoming.campaign)
                    if not target_id:
                        continue
                    camp = conn.execute(
                        """
                        SELECT id, client_id, is_active, status FROM client_campaigns
                        WHERE id = ?
                        """,
                        (int(target_id),),
                    ).fetchone()
                    if camp is None:
                        raise RefreshApplyError("campaign_missing")
                    if int(camp["client_id"]) != int(policy.client_id):
                        raise RefreshApplyError("campaign_wrong_client")
                    if not bool(camp["is_active"]):
                        raise RefreshApplyError("campaign_inactive")
                    rel = conn.execute(
                        """
                        SELECT id FROM client_company_relationships
                        WHERE client_id = ? AND company_id = ?
                        """,
                        (int(policy.client_id), int(company_id)),
                    ).fetchone()
                    if rel is not None:
                        before = conn.execute(
                            "SELECT id FROM campaign_companies WHERE campaign_id=? AND company_id=?",
                            (int(camp["id"]), int(company_id)),
                        ).fetchone()
                        conn.execute(
                            """
                            INSERT INTO campaign_companies (
                                campaign_id, client_id, company_id, relationship_id,
                                notes, created_by, created_at
                            ) VALUES (?, ?, ?, ?, '', 'leadmaster_refresh', datetime('now'))
                            ON CONFLICT(campaign_id, company_id) DO NOTHING
                            """,
                            (int(camp["id"]), int(policy.client_id), int(company_id), int(rel["id"])),
                        )
                        after = conn.execute(
                            "SELECT id FROM campaign_companies WHERE campaign_id=? AND company_id=?",
                            (int(camp["id"]), int(company_id)),
                        ).fetchone()
                        if before is None and after is not None:
                            counts["campaign_memberships_added"] += 1
                    if force_fail_after == "after_campaign":
                        raise RefreshApplyError("forced_rollback")

            if NEW_CONTACT in row["classifications"] and company_id and not contact_id:
                phone, ext = store_phone_parts(incoming.contact_phone, field="phone")
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company_id, external_record_no, first_name, last_name,
                        email, phone, phone_extension, title, source_row_index,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        int(company_id),
                        normalize_record_no(incoming.record_no),
                        incoming.first_name,
                        incoming.last_name,
                        incoming.email,
                        phone,
                        ext or None,
                        incoming.title,
                        int(incoming.source_row),
                    ),
                )
                counts["contacts_created"] += 1

            if (
                not row.get("blocking")
                and not row.get("review")
                and NEW_COMPANY not in row["classifications"]
                and NEW_CONTACT not in row["classifications"]
                and NEW_RELATIONSHIP not in row["classifications"]
            ):
                if all(
                    p.get("action")
                    in {
                        "NO_CHANGE",
                        "UNCHANGED_NOTE",
                        "DUPLICATE_NOTE",
                        "EXCLUDED_NOTE",
                        "DUPLICATE_HISTORY",
                        "UNCHANGED_ASSIGNMENT",
                        "UNCHANGED_HISTORY",
                        "CAMPAIGN_HISTORICAL",
                        "CAMPAIGN_SYSTEM",
                        "CAMPAIGN_IGNORED",
                        "PROVENANCE_ONLY",
                        "IGNORED",
                    }
                    or p.get("action") == STATUS_CHANGE
                    and policy.status_mode != STATUS_AUTHORITATIVE
                    for p in row.get("proposals") or []
                ):
                    counts["unchanged_rows"] += 1

        if frozen:
            counts["rows_reviewed_resolved"] = len(
                {int(r["source_row"]) for r in frozen}
            )
        if force_fail_after:
            raise RefreshApplyError("forced_rollback")
        if manage_transaction:
            conn.commit()
    except Exception:
        if manage_transaction:
            conn.rollback()
        raise
    return {
        "applied": True,
        "created": {
            "companies": counts["companies_created"],
            "contacts": counts["contacts_created"],
            "relationships": counts["relationships_created"],
            "notes": counts["notes_added"],
            "history": counts["history_added"],
            "identities": counts["source_identities_added"],
            "campaigns": counts["campaign_memberships_added"],
            "assignments": counts["assignments_changed"],
            "status": counts["statuses_updated"],
            "company_fills": counts["companies_updated"],
            "contact_fills": counts["contacts_updated"],
        },
        "counts": counts,
        "plan_fingerprint": plan["plan_fingerprint"],
        "confirmed_at": confirmed_at,
        "confirmed_by_user_id": actor_id,
    }
