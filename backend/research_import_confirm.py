"""Isolated confirm for Research & Custom Prospect Import.

Production remains fail-closed. One atomic transaction. Frozen fingerprint
must match. Workflow-sensitive fields are never written.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from company_locations import create_company_location
from contact_phone import store_phone_parts, upsert_contact_phone_keys
from crm_identity_keys import upsert_company_identity_key
from data_steward import (
    SOURCE_AI_RESEARCH,
    is_manual_authority,
    record_populated_creates,
    record_provenance,
)
from import_brown_industries import norm_name
from models import NorthStarUser
from research_import_contacts import note_already_present
from research_import_mapping import blank, parse_typed_value
from research_import_live_confirm import assert_confirm_permitted
from research_import_plan import (
    CLASS_INVALID,
    CLASS_NEW,
    CLASS_NEW_LOCATION,
    CONFIRM_BLOCKED,
    STALE_FINGERPRINT,
    location_tuple,
    plan_fingerprint,
    plan_research_rows,
    planned_contract_slice,
    utc_now,
)
from research_import_policy import (
    CONTACT_EXACT,
    CONTACT_INVALID,
    CONTACT_NEW,
    CONTACT_STRONG,
    MASTER_ACCEPT_INCOMING,
    MASTER_ADD_LOCATION,
    MASTER_FILL,
    MASTER_KEEP,
    MASTER_SKIP,
    RES_CREATE_CONTACT,
    RES_SKIP_CONTACT,
    RES_USE_CONTACT,
)
from research_import_schema import ensure_research_import_schema
from research_import_staging import fingerprint_options_from_batch, load_batch, mapped_rows_for_batch

_CONFIRM_FAIL: ContextVar[str] = ContextVar("research_import_confirm_fail", default="")

COMPANY_FIELD_COLUMNS = {
    "company_name": "company_name",
    "website": "website",
    "phone": "legacy_phone",
    "address": "address",
    "city": "city",
    "state": "state",
    "zip": "zip",
}

EMPTY_RESULT = {
    "companies_created": 0,
    "companies_reused": 0,
    "locations_created": 0,
    "locations_reused": 0,
    "ccrs_created": 0,
    "ccrs_reused": 0,
    "contacts_created": 0,
    "contacts_reused": 0,
    "contacts_skipped": 0,
    "notes_created": 0,
    "notes_deduped": 0,
    "research_created": 0,
    "research_superseded": 0,
    "attributes_created": 0,
    "sources_created": 0,
    "master_fields_filled": 0,
    "master_fields_updated": 0,
    "master_fields_preserved": 0,
    "provenance_events_created": 0,
    "rows_skipped": 0,
    "rows_blocked": 0,
    "workflow_fields_written": 0,
    "master_proposals_applied": 0,
    "aliases_created": 0,
    "aliases_deduped": 0,
    "identities_created": 0,
    "identities_deduped": 0,
}


def _sqlite_wrote(conn) -> bool:
    return int(conn.execute("SELECT changes()").fetchone()[0]) > 0


@contextmanager
def inject_confirm_failure(stage: str) -> Iterator[None]:
    token = _CONFIRM_FAIL.set(blank(stage))
    try:
        yield
    finally:
        _CONFIRM_FAIL.reset(token)


def _fail_if(stage: str) -> None:
    injected = blank(_CONFIRM_FAIL.get())
    if injected and injected == blank(stage):
        raise RuntimeError(f"RI-4 injected confirm failure: {stage}")


def apply_research_import_confirm(
    conn,
    *,
    client_id: int,
    batch_id: int,
    expected_fingerprint: str,
    actor: NorthStarUser | None,
) -> dict[str, Any]:
    assert_confirm_permitted(
        conn,
        client_id=int(client_id),
        batch_id=int(batch_id),
        expected_fingerprint=expected_fingerprint,
        actor=actor,
    )
    ensure_research_import_schema(conn)
    batch = load_batch(conn, client_id, batch_id, allow_confirmed=True)
    if blank(batch.get("status")) == "confirmed":
        stored_fp = blank(batch.get("confirmed_plan_fingerprint"))
        if blank(expected_fingerprint) != stored_fp:
            raise ValueError(STALE_FINGERPRINT)
        stored = batch.get("confirm_result") or {}
        return {**stored, "idempotent": True, "status": "confirmed"}
    mapped, row_ids = mapped_rows_for_batch(batch)
    planned = plan_research_rows(
        conn,
        client_id=int(client_id),
        mapped_rows=mapped,
        row_ids=row_ids,
        match_resolutions=batch.get("match_resolutions") or {},
        master_resolutions=batch.get("master_resolutions") or {},
        contact_resolutions=batch.get("contact_resolutions") or {},
        mapping=batch.get("mapping") or {},
        raw_rows=batch.get("rows") or [],
        batch_id=int(batch_id),
    )
    options = fingerprint_options_from_batch(batch)
    options["planned_contract"] = planned_contract_slice(planned)
    current = plan_fingerprint(
        client_id=int(client_id),
        sha256=batch.get("sha256") or "",
        mapping=batch.get("mapping") or {},
        research_method=batch.get("research_method") or "",
        research_date=batch.get("research_date") or "",
        mapped_rows=mapped,
        match_resolutions=batch.get("match_resolutions") or {},
        master_resolutions=batch.get("master_resolutions") or {},
        contact_resolutions=batch.get("contact_resolutions") or {},
        options=options,
    )
    if blank(expected_fingerprint) != current:
        raise ValueError(STALE_FINGERPRINT)
    if any(row.blocking for row in planned):
        raise ValueError(CONFIRM_BLOCKED)

    counts = dict(EMPTY_RESULT)
    created_companies_by_key: dict[str, int] = {}
    created_ccr_by_company: dict[int, int] = {}
    now = utc_now()
    try:
        for row in planned:
            if row.ri_class in {CLASS_INVALID, "SKIPPED"}:
                counts["rows_skipped"] += 1
                continue
            _confirm_row(
                conn,
                batch=batch,
                row=row,
                client_id=int(client_id),
                batch_id=int(batch_id),
                actor=actor,
                now=now,
                counts=counts,
                created_companies_by_key=created_companies_by_key,
                created_ccr_by_company=created_ccr_by_company,
            )
        result = {
            **counts,
            "batch_id": int(batch_id),
            "client_id": int(client_id),
            "plan_fingerprint": current,
            "created_companies": counts["companies_created"],
            "created_locations": counts["locations_created"],
            "created_ccrs": counts["ccrs_created"],
            "created_research": counts["research_created"],
            "created_contacts": counts["contacts_created"],
            "reused_contacts": counts["contacts_reused"],
            "created_custom_attributes": counts["attributes_created"],
            "notes_written": counts["notes_created"],
            "mapping_template_id": batch.get("mapping_template_id"),
            "mapping_template_name": batch.get("mapping_template_name") or "",
            "source_type": batch.get("source_type") or "",
            "status": "confirmed",
            "idempotent": False,
        }
        conn.execute(
            """
            UPDATE research_import_batches
            SET status='confirmed', confirmed_at=?, confirmed_plan_fingerprint=?,
                confirm_result_json=?, updated_at=?, error_message=''
            WHERE id=? AND client_id=?
            """,
            (now, current, json.dumps(result), now, int(batch_id), int(client_id)),
        )
        return result
    except Exception:
        conn.rollback()
        raise


def _confirm_row(
    conn,
    *,
    batch: dict[str, Any],
    row,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser | None,
    now: str,
    counts: dict[str, int],
    created_companies_by_key: dict[str, int],
    created_ccr_by_company: dict[int, int],
) -> None:
    company_id = row.matched_company_id
    company_key = norm_name(row.mapped.get("company_name", ""))
    reused_company = False
    if (row.ri_class == CLASS_NEW or company_id is None) and company_key and company_key in created_companies_by_key:
        company_id = created_companies_by_key[company_key]
        reused_company = True
        counts["companies_reused"] += 1
    elif row.ri_class == CLASS_NEW or company_id is None:
        company_id = _create_company(conn, batch_id=batch_id, row=row, actor=actor, client_id=client_id, now=now, counts=counts)
        _fail_if("after_company")
        if company_key:
            created_companies_by_key[company_key] = int(company_id)
    else:
        reused_company = True
        counts["companies_reused"] += 1
        if row.research_anchor:
            _apply_master_resolutions(conn, row=row, company_id=int(company_id), batch=batch, actor=actor, client_id=client_id, counts=counts)
            _fail_if("after_provenance")
        loc_outcome = _ensure_location(conn, company_id=int(company_id), row=row, as_new=False)
        if loc_outcome == "created":
            counts["locations_created"] += 1
        elif loc_outcome == "reused":
            counts["locations_reused"] += 1
        if row.ri_class == CLASS_NEW_LOCATION and row.match_resolution == "TREAT_AS_NEW_LOCATION":
            loc_outcome = _ensure_location(conn, company_id=int(company_id), row=row, as_new=True)
            if loc_outcome == "created":
                counts["locations_created"] += 1
            elif loc_outcome == "reused":
                counts["locations_reused"] += 1

    relationship_id = row.this_client_ccr_id or created_ccr_by_company.get(int(company_id) if company_id else -1)
    note_body = _note_body(row)
    if row.relationship_action == "create_client_relationship" and not relationship_id:
        status = blank(row.planned_status) or "New"
        if status.casefold() == "hot prospect":
            status = "New"
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, status, notes
            ) VALUES (?, ?, ?, ?)
            """,
            (int(client_id), int(company_id), status, note_body if note_body else ""),
        )
        relationship_id = int(
            conn.execute(
                """
                SELECT id FROM client_company_relationships
                WHERE client_id=? AND company_id=?
                """,
                (int(client_id), int(company_id)),
            ).fetchone()[0]
        )
        counts["ccrs_created"] += 1
        if note_body:
            counts["notes_created"] += 1
        created_ccr_by_company[int(company_id)] = int(relationship_id)
        _fail_if("after_ccr")
        _fail_if("after_note")
    else:
        if relationship_id:
            counts["ccrs_reused"] += 1
        if relationship_id and note_body:
            existing = conn.execute(
                "SELECT notes FROM client_company_relationships WHERE id=?",
                (int(relationship_id),),
            ).fetchone()
            prev = ""
            if existing is not None:
                try:
                    prev = "" if existing["notes"] is None else str(existing["notes"])
                except (KeyError, IndexError, TypeError):
                    prev = "" if existing[0] is None else str(existing[0])
            if note_already_present(prev, note_body):
                counts["notes_deduped"] += 1
            else:
                conn.execute(
                    "UPDATE client_company_relationships SET notes=? WHERE id=?",
                    ((prev + "\n\n" + note_body).strip() if prev else note_body, int(relationship_id)),
                )
                counts["notes_created"] += 1
                _fail_if("after_note")

    if row.research_anchor:
        prior = conn.execute(
            """
            SELECT id FROM client_company_research
            WHERE client_id=? AND company_id=? AND is_current=1
            """,
            (int(client_id), int(company_id)),
        ).fetchone()
        if prior is not None:
            conn.execute(
                """
                UPDATE client_company_research
                SET is_current=0, updated_at=?
                WHERE client_id=? AND company_id=? AND is_current=1
                """,
                (now, int(client_id), int(company_id)),
            )
            counts["research_superseded"] += 1
        conn.execute(
            """
            INSERT INTO client_company_research (
                client_id, company_id, relationship_id, batch_id,
                research_priority_code, research_priority_label, why_client_fits,
                qualification_notes, is_current, researched_at, created_at, updated_at,
                created_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                int(client_id),
                int(company_id),
                relationship_id,
                int(batch_id),
                row.research_priority_code,
                row.research_priority_label,
                row.mapped.get("why_client_fits", ""),
                "" if batch.get("batch_caveat") == row.mapped.get("qualification_notes", "") else row.mapped.get("qualification_notes", ""),
                batch.get("research_date") or now[:10],
                now,
                now,
                getattr(actor, "id", None),
            ),
        )
        research_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        counts["research_created"] += 1
        _fail_if("after_research")

        custom_keys = {blank(item.get("key")) for item in row.custom_attributes}
        for atype, values in row.attributes.items():
            if blank(atype) in custom_keys:
                continue
            scope = "shared" if atype == "equipment_product" else "client"
            for value in values:
                if not blank(value):
                    continue
                conn.execute(
                    """
                    UPDATE research_attributes
                    SET is_current=0
                    WHERE company_id=? AND attribute_type=? AND is_current=1
                      AND COALESCE(client_id, -1)=COALESCE(?, -1)
                    """,
                    (int(company_id), atype, int(client_id) if scope == "client" else None),
                )
                conn.execute(
                    """
                    INSERT INTO research_attributes (
                        client_id, company_id, research_id, batch_id, attribute_type,
                        attribute_value, value_norm, scope, is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        int(client_id) if scope == "client" else None,
                        int(company_id),
                        research_id,
                        int(batch_id),
                        atype,
                        value,
                        norm_name(value),
                        scope,
                    ),
                )
                counts["attributes_created"] += 1
        for item in row.custom_attributes:
            _upsert_attribute_definition(conn, item)
            typed = parse_typed_value(item.get("original_value") or item.get("text_value"), item.get("value_type") or "TEXT")
            scope = blank(item.get("scope")) or "client"
            attr_client = int(client_id) if scope == "client" else None
            conn.execute(
                """
                UPDATE research_attributes
                SET is_current=0
                WHERE company_id=? AND attribute_type=? AND is_current=1
                  AND COALESCE(client_id, -1)=COALESCE(?, -1)
                """,
                (int(company_id), blank(item.get("key")), attr_client),
            )
            conn.execute(
                """
                INSERT INTO research_attributes (
                    client_id, company_id, research_id, batch_id, attribute_type,
                    attribute_value, value_norm, scope, is_current,
                    display_label, value_type, numeric_value, date_value, boolean_value, original_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(client_id) if scope == "client" else None,
                    int(company_id),
                    research_id,
                    int(batch_id),
                    blank(item.get("key")),
                    typed["text_value"] or typed["original_value"],
                    norm_name(typed["original_value"]),
                    scope,
                    blank(item.get("label")) or blank(item.get("key")),
                    typed["value_type"],
                    typed["numeric_value"],
                    typed["date_value"] or "",
                    None if typed["boolean_value"] is None else int(bool(typed["boolean_value"])),
                    typed["original_value"],
                ),
            )
            counts["attributes_created"] += 1
        _fail_if("after_attributes")

        seen_sources: set[tuple[str, str]] = set()
        for source in row.sources:
            key = (blank(source.get("source_role")), blank(source.get("source_url")))
            if not key[1] or key in seen_sources:
                continue
            seen_sources.add(key)
            existing = conn.execute(
                """
                SELECT id FROM research_sources
                WHERE research_id=? AND source_role=? AND source_url=?
                """,
                (research_id, key[0], key[1]),
            ).fetchone()
            if existing is not None:
                continue
            conn.execute(
                """
                INSERT INTO research_sources (
                    batch_id, research_id, company_id, client_id, source_role,
                    source_url, source_domain, field_supported, original_value, researched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(batch_id),
                    research_id,
                    int(company_id),
                    int(client_id),
                    source["source_role"],
                    source["source_url"],
                    source["source_domain"],
                    source["field_supported"],
                    source["original_value"],
                    batch.get("research_date") or now[:10],
                ),
            )
            counts["sources_created"] += 1

    for contact in row.contacts:
        outcome = _confirm_named_contact(
            conn, company_id=int(company_id), contact=contact, batch_id=int(batch_id), row_id=row.row_id
        )
        if outcome == "created":
            counts["contacts_created"] += 1
            _fail_if("after_contact")
        elif outcome == "reused":
            counts["contacts_reused"] += 1
        else:
            counts["contacts_skipped"] += 1

    conn.execute(
        """
        INSERT OR IGNORE INTO company_source_identities (
            company_id, client_id, source_system, source_record_no, source_company_name,
            source_address, source_city, source_state, source_zip, source_phone,
            source_website, source_batch_id, source_row
        ) VALUES (?, ?, 'RESEARCH_IMPORT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(company_id),
            int(client_id),
            f"{batch_id}:{row.row_id}",
            row.mapped.get("company_name", ""),
            row.mapped.get("address", ""),
            row.mapped.get("city", ""),
            row.mapped.get("state", ""),
            row.mapped.get("zip", ""),
            row.mapped.get("phone", ""),
            row.mapped.get("website", ""),
            int(batch_id),
            row.source_row_number,
        ),
    )
    if _sqlite_wrote(conn):
        counts["identities_created"] += 1
    else:
        counts["identities_deduped"] += 1
    if blank(row.mapped.get("company_name")):
        conn.execute(
            """
            INSERT OR IGNORE INTO company_aliases (
                company_id, alias_name, alias_norm, source_system, client_id,
                source_batch_id, source_row
            ) VALUES (?, ?, ?, 'RESEARCH_IMPORT', ?, ?, ?)
            """,
            (
                int(company_id),
                row.mapped.get("company_name", ""),
                norm_name(row.mapped.get("company_name", "")),
                int(client_id),
                int(batch_id),
                row.source_row_number,
            ),
        )
        if _sqlite_wrote(conn):
            counts["aliases_created"] += 1
        else:
            counts["aliases_deduped"] += 1
    del reused_company


def _create_company(conn, *, batch_id: int, row, actor, client_id: int, now: str, counts: dict[str, int]) -> int:
    conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, address, city, state, zip,
            website, legacy_phone, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RESEARCH_IMPORT')
        """,
        (
            f"RI-{batch_id}-{row.row_id}",
            row.mapped.get("company_name", ""),
            row.mapped.get("address", ""),
            row.mapped.get("city", ""),
            row.mapped.get("state", ""),
            row.mapped.get("zip", ""),
            row.mapped.get("website", ""),
            row.mapped.get("phone", ""),
        ),
    )
    company_id = int(
        conn.execute(
            "SELECT id FROM companies WHERE external_record_no=?",
            (f"RI-{batch_id}-{row.row_id}",),
        ).fetchone()[0]
    )
    upsert_company_identity_key(
        conn,
        company_id,
        external_record_no=f"RI-{batch_id}-{row.row_id}",
        company_name=row.mapped.get("company_name", ""),
        website=row.mapped.get("website", ""),
        legacy_phone=row.mapped.get("phone", ""),
        address=row.mapped.get("address", ""),
        city=row.mapped.get("city", ""),
        state=row.mapped.get("state", ""),
    )
    counts["companies_created"] += 1
    loc_outcome = _ensure_location(conn, company_id=company_id, row=row, as_new=True)
    if loc_outcome == "created":
        counts["locations_created"] += 1
    elif loc_outcome == "reused":
        counts["locations_reused"] += 1
    before = _provenance_count(conn)
    record_populated_creates(
        conn,
        entity_type="company",
        entity_id=company_id,
        fields={
            "company_name": row.mapped.get("company_name", ""),
            "website": row.mapped.get("website", ""),
            "legacy_phone": row.mapped.get("phone", ""),
            "address": row.mapped.get("address", ""),
            "city": row.mapped.get("city", ""),
            "state": row.mapped.get("state", ""),
            "zip": row.mapped.get("zip", ""),
        },
        actor=actor,
        source_type=SOURCE_AI_RESEARCH,
        source_ref=_source_ref(batch_id=batch_id, row_id=row.row_id),
        action="CREATE",
        client_id=client_id,
        trusted=True,
    )
    counts["provenance_events_created"] += max(0, _provenance_count(conn) - before)
    return company_id


def _apply_master_resolutions(conn, *, row, company_id: int, batch, actor, client_id: int, counts: dict[str, int]) -> None:
    current = _company_fields(conn, company_id)
    for conflict in row.conflicts or []:
        field = blank(conflict.get("field"))
        klass = blank(conflict.get("class"))
        decision = blank((row.master_resolutions_by_field or {}).get(field)) or blank(conflict.get("resolution"))
        if not decision:
            decision = MASTER_KEEP if klass in {"SAME", "PROPOSE_UPDATE", "MANUAL_AUTHORITY_CONFLICT", "FILL_BLANK"} else ""
        if klass == "SAME" or decision in {MASTER_KEEP, MASTER_SKIP, ""}:
            counts["master_fields_preserved"] += 1
            continue
        if klass == "MANUAL_AUTHORITY_CONFLICT" or (
            decision == MASTER_ACCEPT_INCOMING and _manual(conn, company_id, field)
        ):
            counts["master_fields_preserved"] += 1
            continue
        if decision == MASTER_ADD_LOCATION:
            loc_outcome = _ensure_location(conn, company_id=company_id, row=row, as_new=True)
            if loc_outcome == "created":
                counts["locations_created"] += 1
                counts["master_proposals_applied"] += 1
            elif loc_outcome == "reused":
                counts["locations_reused"] += 1
            continue
        column = COMPANY_FIELD_COLUMNS.get(field)
        if not column:
            counts["master_fields_preserved"] += 1
            continue
        incoming = _incoming_for_field(row, field)
        existing = blank(current.get(column))
        if decision == MASTER_FILL:
            if existing:
                raise ValueError(STALE_FINGERPRINT)
            _write_master_field(
                conn,
                company_id=company_id,
                column=column,
                incoming=incoming,
                existing=existing,
                actor=actor,
                client_id=client_id,
                batch=batch,
                row_id=row.row_id,
                action="FILL",
            )
            counts["master_fields_filled"] += 1
            counts["master_proposals_applied"] += 1
            counts["provenance_events_created"] += 1
        elif decision == MASTER_ACCEPT_INCOMING:
            if klass != "PROPOSE_UPDATE":
                counts["master_fields_preserved"] += 1
                continue
            _write_master_field(
                conn,
                company_id=company_id,
                column=column,
                incoming=incoming,
                existing=existing,
                actor=actor,
                client_id=client_id,
                batch=batch,
                row_id=row.row_id,
                action="UPDATE",
            )
            counts["master_fields_updated"] += 1
            counts["master_proposals_applied"] += 1
            counts["provenance_events_created"] += 1
        else:
            counts["master_fields_preserved"] += 1


def _manual(conn, company_id: int, field: str) -> bool:
    prov_field = "legacy_phone" if field == "phone" else field
    return is_manual_authority(conn, entity_type="company", entity_id=int(company_id), field=prov_field)


def _incoming_for_field(row, field: str) -> str:
    if field == "address":
        return blank(row.mapped.get("address"))
    if field == "phone":
        return blank(row.mapped.get("phone"))
    return blank(row.mapped.get(field))


def _company_fields(conn, company_id: int) -> dict[str, str]:
    row = conn.execute(
        "SELECT company_name, website, legacy_phone, address, city, state, zip FROM companies WHERE id=?",
        (int(company_id),),
    ).fetchone()
    if row is None:
        return {}
    return {key: blank(row[key]) for key in row.keys()}


def _write_master_field(
    conn,
    *,
    company_id: int,
    column: str,
    incoming: str,
    existing: str,
    actor,
    client_id: int,
    batch,
    row_id: int,
    action: str,
) -> None:
    conn.execute(f"UPDATE companies SET {column}=? WHERE id=?", (incoming, int(company_id)))
    record_provenance(
        conn,
        entity_type="company",
        entity_id=int(company_id),
        field=column,
        old_value=existing,
        new_value=incoming,
        source_type=SOURCE_AI_RESEARCH,
        source_ref=_source_ref(batch_id=int(batch.get("id") or 0), row_id=row_id),
        action=action,
        client_id=int(client_id),
        actor=actor,
        trusted=True,
        reason=f"research_import:{blank(batch.get('source_type'))}:{blank(batch.get('original_filename'))}",
    )


def _ensure_location(conn, *, company_id: int, row, as_new: bool) -> str:
    addr = blank(row.mapped.get("address"))
    city = blank(row.mapped.get("city"))
    state = blank(row.mapped.get("state"))
    postal = blank(row.mapped.get("zip"))
    if not addr and not city:
        return "skipped"
    incoming = location_tuple(addr, city, state, postal)
    existing = conn.execute(
        """
        SELECT id, address, city, state, zip FROM company_locations
        WHERE company_id=?
        """,
        (int(company_id),),
    ).fetchall()
    for loc in existing:
        loc_t = location_tuple(loc["address"], loc["city"], loc["state"], loc["zip"])
        if incoming == loc_t or (
            incoming[0]
            and incoming[1]
            and incoming[0] == loc_t[0]
            and incoming[1] == loc_t[1]
            and (not incoming[2] or not loc_t[2] or incoming[2] == loc_t[2])
            and (not incoming[3] or not loc_t[3] or incoming[3] == loc_t[3])
        ):
            return "reused"
    if not as_new and not (
        any(
            blank(c.get("class")) == "POSSIBLE_NEW_LOCATION"
            and blank((row.master_resolutions_by_field or {}).get(c.get("field"))) == MASTER_ADD_LOCATION
            for c in (row.conflicts or [])
        )
    ):
        if incoming[0] or incoming[1]:
            master = conn.execute(
                "SELECT address, city, state, zip FROM companies WHERE id=?",
                (int(company_id),),
            ).fetchone()
            if master is not None:
                master_t = location_tuple(master["address"], master["city"], master["state"], master["zip"])
                if incoming == master_t or (
                    incoming[0] == master_t[0] and incoming[1] == master_t[1]
                ):
                    return "reused"
        return "skipped"
    create_company_location(
        conn,
        company_id=int(company_id),
        location_name=blank(row.mapped.get("city")) or blank(row.mapped.get("company_name")),
        location_type="unknown",
        address=addr,
        city=city,
        state=state,
        zip_code=postal,
        phone=blank(row.mapped.get("phone")),
        website=blank(row.mapped.get("website")),
        source_system="RESEARCH_IMPORT",
    )
    return "created"


def _source_ref(*, batch_id: int, row_id: int) -> str:
    return f"research_import:{batch_id}:{row_id}"


def _provenance_count(conn) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0])
    except Exception:
        return 0


def _upsert_attribute_definition(conn, item: dict[str, Any]) -> None:
    key = blank(item.get("key"))
    if not key:
        return
    now = utc_now()
    existing = conn.execute(
        "SELECT id FROM research_attribute_definitions WHERE attribute_key=?",
        (key,),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """
        INSERT INTO research_attribute_definitions (
            attribute_key, display_label, value_type, scope_default, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            blank(item.get("label")) or key,
            blank(item.get("value_type")) or "TEXT",
            blank(item.get("scope")) or "client",
            now,
            now,
        ),
    )


def _note_body(row) -> str:
    explicit = blank(getattr(row, "notes_explicit", "")) or blank((row.mapped or {}).get("imported_notes"))
    if explicit:
        return explicit
    return ""


def _confirm_named_contact(conn, *, company_id: int, contact: dict[str, Any], batch_id: int, row_id: int) -> str:
    incoming = contact.get("incoming") if isinstance(contact.get("incoming"), dict) else contact
    klass = blank(contact.get("contact_class"))
    res = blank(contact.get("resolution")).upper()
    if klass in {CONTACT_INVALID, ""} or res == RES_SKIP_CONTACT:
        return "skipped"
    if bool(contact.get("cross_company")) and res == RES_USE_CONTACT:
        return "skipped"
    if res == RES_USE_CONTACT or (klass in {CONTACT_EXACT, CONTACT_STRONG} and res != RES_CREATE_CONTACT):
        return "reused"
    if res not in {RES_CREATE_CONTACT, ""} and klass != CONTACT_NEW:
        return "skipped"
    first = blank(incoming.get("first_name"))
    last = blank(incoming.get("last_name"))
    full = blank(incoming.get("full_name")) or f"{first} {last}".strip()
    if not first and not last and full:
        parts = full.split(None, 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
    phone_raw = blank(incoming.get("phone"))
    phone_main, phone_ext = store_phone_parts(phone_raw, field="phone")
    ext = blank(incoming.get("phone_extension")) or phone_ext
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no,
            first_name, last_name, title, phone, phone_extension, alt_phone, email,
            source_row_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, 0)
        """,
        (
            company_id,
            f"RI-{batch_id}-{row_id}",
            first,
            last,
            blank(incoming.get("title")),
            phone_main,
            ext or None,
            blank(incoming.get("email")),
        ),
    )
    contact_id = int(cur.lastrowid)
    upsert_contact_phone_keys(conn, contact_id, phone_main, "")
    return "created"
