"""Checkpoint C3 — atomic confirmation for fully importable CRM batches.

Single-connection, single-transaction apply:
- BEGIN IMMEDIATE
- plan_crm_import_batch(conn, ...) fingerprint + needs-review gate
- INSERT companies/contacts/relationships only for allowed create actions
- INSERT crm_import_results row per staged row
- UPDATE crm_import_batches terminal audit fields
- DELETE crm_import_rows only after success

This module must not:
- rematch (no match_company/match_contact)
- call create_or_link_contact / ensure_client_relationship
- call confirm_company_contact_add / create_activity
- open secondary connections
- call ensure_crm_import_schema / migrate_schema / any DDL
  (schema must already exist from controlled migration)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from contact_phone import format_us_phone_display, upsert_contact_phone_keys
from appointments_data import is_hot_prospect_status
from crm_add_data import allocate_ns_record_no
from crm_import_plan import plan_crm_import_batch
from crm_import_staging import BatchNotReusable
from crm_import_status_notes import (
    NOTES_ALREADY_PRESENT,
    NOTES_APPEND,
    NOTES_NO_CHANGE,
    NOTES_SET,
    STATUS_PRESERVE,
    STATUS_USE_DEFAULT,
    STATUS_USE_IMPORTED,
)
from db import get_connection
from models import NorthStarUser
from search_data import index_company_relationship, index_contact_relationship
import logging

log = logging.getLogger("northstar.crm_import")

_FP_RE = re.compile(r"^[a-f0-9]{64}$")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    # Keep format consistent with existing CRM write helpers.
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in _blank(full_name).split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _require_allowed_actions(plan) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int, int, int]:
    """Return company/contact/relationship counters plus status/notes audit counters."""

    created_company = reused_company = 0
    created_contact = reused_contact = 0
    no_contact_rows = 0
    created_relationship = existing_relationship = 0
    imported_status = default_status = preserved_status = 0
    notes_set = notes_appended = notes_duplicate = notes_unchanged = 0

    for row in plan.rows:
        # Company
        if row.company_action == "create_company":
            created_company += 1
        elif row.company_action == "use_existing_company":
            reused_company += 1
        else:
            raise BatchNotReusable("Import batch contains a non-actionable company plan.")

        # Contact
        if row.contact_action == "create_contact":
            created_contact += 1
        elif row.contact_action == "use_existing_contact":
            reused_contact += 1
        elif row.contact_action == "no_contact_data":
            no_contact_rows += 1
        else:
            raise BatchNotReusable("Import batch contains a non-actionable contact plan.")

        # Relationship
        if row.relationship_action == "create_client_relationship":
            created_relationship += 1
        elif row.relationship_action == "relationship_already_exists":
            existing_relationship += 1
        else:
            raise BatchNotReusable(
                "Import batch contains a non-actionable relationship plan."
            )

        if row.status_action == STATUS_USE_IMPORTED:
            imported_status += 1
        elif row.status_action == STATUS_USE_DEFAULT:
            default_status += 1
        elif row.status_action == STATUS_PRESERVE:
            preserved_status += 1
        else:
            raise BatchNotReusable("Import batch contains a non-actionable status plan.")

        if row.notes_action == NOTES_SET:
            notes_set += 1
        elif row.notes_action == NOTES_APPEND:
            notes_appended += 1
        elif row.notes_action == NOTES_ALREADY_PRESENT:
            notes_duplicate += 1
        elif row.notes_action == NOTES_NO_CHANGE:
            notes_unchanged += 1
        else:
            raise BatchNotReusable("Import batch contains a non-actionable notes plan.")

    total = int(plan.total_rows)
    if created_company + reused_company != total:
        raise BatchNotReusable("Company plan counters do not reconcile to total rows.")
    if created_contact + reused_contact + no_contact_rows != total:
        raise BatchNotReusable("Contact plan counters do not reconcile to total rows.")
    if created_relationship + existing_relationship != total:
        raise BatchNotReusable("Relationship plan counters do not reconcile to total rows.")
    if imported_status + default_status + preserved_status != total:
        raise BatchNotReusable("Status plan counters do not reconcile to total rows.")
    if notes_set + notes_appended + notes_duplicate + notes_unchanged != total:
        raise BatchNotReusable("Notes plan counters do not reconcile to total rows.")

    return (
        created_company,
        reused_company,
        created_contact,
        reused_contact,
        created_relationship,
        existing_relationship,
        no_contact_rows,
        imported_status,
        default_status,
        preserved_status,
        notes_set,
        notes_appended,
        notes_duplicate,
        notes_unchanged,
    )


def _assert_proposed_key_present(key: str | None, expected_prefix: str) -> str:
    text = _blank(key)
    if not text:
        raise BatchNotReusable("Missing proposed key required by import plan.")
    if not text.startswith(expected_prefix):
        raise BatchNotReusable("Proposed key kind mismatch for import plan.")
    return text


def confirm_admin_crm_import_batch(
    *,
    client_id: int,
    batch_id: int,
    plan_fingerprint: str,
    actor: NorthStarUser,
    fail_after: str | None = None,
) -> dict[str, Any]:
    if not actor or not bool(actor.active):
        raise PermissionError("User not found.")
    if not _FP_RE.match(plan_fingerprint or ""):
        raise ValueError("Malformed plan_fingerprint.")

    with get_connection() as conn:
        try:
            # Optional read-only fast precheck (no DDL). Locked validation happens
            # again via plan_crm_import_batch inside BEGIN IMMEDIATE.
            batch_row = conn.execute(
                """
                SELECT status FROM crm_import_batches
                WHERE id = ? AND client_id = ?
                """,
                (int(batch_id), int(client_id)),
            ).fetchone()
            if batch_row is None:
                raise LookupError("Import batch not found.")
            if _blank(batch_row["status"]) != "previewed":
                raise BatchNotReusable("Import batch is not in previewed status.")

            conn.execute("BEGIN IMMEDIATE")

            plan = plan_crm_import_batch(
                conn,
                client_id=client_id,
                batch_id=batch_id,
            )

            if _blank(plan.plan_fingerprint) != plan_fingerprint:
                raise BatchNotReusable("Plan fingerprint mismatch.")

            # Gate on fully importable-only plans.
            if int(plan.counts.get("needs_review_rows") or 0) != 0:
                raise BatchNotReusable("Import batch contains needs-review rows.")
            if int(plan.counts.get("importable_rows") or 0) != int(plan.total_rows):
                raise BatchNotReusable("Import batch is not fully importable.")

            (
                created_company_count,
                reused_company_count,
                created_contact_count,
                reused_contact_count,
                created_relationship_count,
                existing_relationship_count,
                no_contact_row_count,
                imported_status_count,
                default_status_count,
                preserved_status_count,
                notes_set_count,
                notes_appended_count,
                notes_duplicate_count,
                notes_unchanged_count,
            ) = _require_allowed_actions(plan)

            total_imported_row_count = int(plan.total_rows)

            # Proposed-key resolution maps (transaction-local).
            company_key_to_id: dict[str, tuple[int, str]] = {}
            contact_key_to_id: dict[str, int] = {}
            contact_key_to_company_id: dict[str, int] = {}
            relationship_key_to_id: dict[str, int] = {}
            relationship_key_to_company_id: dict[str, int] = {}

            actual_company_inserts = 0
            actual_contact_inserts = 0
            actual_relationship_inserts = 0
            actual_results_inserts = 0
            search_index_targets: list[tuple[int, int | None]] = []

            now = _now()

            # Apply in planner order to preserve proposed-key dependencies.
            for row in plan.rows:
                # -----------------
                # Company resolve/apply
                # -----------------
                company_id: int
                company_external_record_no: str

                if row.company_action == "create_company":
                    proposed_key = _assert_proposed_key_present(
                        row.company_proposed_key, "proposed:company:"
                    )
                    if proposed_key in company_key_to_id:
                        raise BatchNotReusable(
                            "Duplicate proposed company creation detected."
                        )

                    external_record_no = allocate_ns_record_no(conn)
                    mapped = row.mapped or {}

                    company_name = _blank(mapped.get("company_name") or row.company_name)
                    if not company_name:
                        raise BatchNotReusable("Missing company_name for create_company.")

                    address = _blank(mapped.get("address"))
                    city = _blank(mapped.get("city"))
                    state = _blank(mapped.get("state"))
                    zip_ = _blank(mapped.get("zip"))
                    website = _blank(mapped.get("website"))
                    legacy_phone = _blank(mapped.get("phone"))
                    # Source-history only — never write these onto created_at / updated_at.
                    entered_at = _blank(mapped.get("source_entered_at"))
                    source_updated_at = _blank(mapped.get("source_updated_at"))

                    cur = conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, address, city, state, zip, website,
                            legacy_phone, type_of_industry, entered_at, source_updated_at,
                            created_at, last_updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
                        """,
                        (
                            external_record_no,
                            company_name,
                            address,
                            city,
                            state,
                            zip_,
                            website,
                            legacy_phone,
                            entered_at,
                            source_updated_at,
                            now,
                            now,
                        ),
                    )
                    company_id = int(cur.lastrowid)
                    company_external_record_no = external_record_no
                    company_key_to_id[proposed_key] = (company_id, company_external_record_no)
                    actual_company_inserts += 1
                    if fail_after == "after_company_insert" and actual_company_inserts == 1:
                        raise RuntimeError("Injected failure after company insert.")
                else:
                    # use_existing_company
                    if row.company_id is not None:
                        company_id = int(row.company_id)
                        exists = conn.execute(
                            "SELECT 1 FROM companies WHERE id = ? LIMIT 1",
                            (company_id,),
                        ).fetchone()
                        if exists is None:
                            raise BatchNotReusable("Proposed plan references missing company.")
                        rec = conn.execute(
                            "SELECT external_record_no FROM companies WHERE id = ?",
                            (company_id,),
                        ).fetchone()
                        company_external_record_no = _blank(rec["external_record_no"] if rec else "")
                    else:
                        proposed_key = _assert_proposed_key_present(
                            row.company_proposed_key, "proposed:company:"
                        )
                        if proposed_key not in company_key_to_id:
                            raise BatchNotReusable("Missing proposed company resolution.")
                        company_id, company_external_record_no = company_key_to_id[proposed_key]

                # -----------------
                # Contact resolve/apply
                # -----------------
                contact_id: int | None = None
                if row.contact_action == "no_contact_data":
                    contact_id = None
                elif row.contact_action == "create_contact":
                    proposed_key = _assert_proposed_key_present(
                        row.contact_proposed_key, "proposed:contact:"
                    )
                    if proposed_key in contact_key_to_id:
                        raise BatchNotReusable("Duplicate proposed contact creation detected.")

                    mapped = row.mapped or {}
                    first = _blank(mapped.get("contact_first_name"))
                    last = _blank(mapped.get("contact_last_name"))
                    full = _blank(mapped.get("contact_full_name"))
                    if not first and not last and full:
                        first, last = _split_full_name(full)

                    stored_email = _blank(mapped.get("contact_email"))
                    # Email-only creates are allowed by the planner (name OR email).
                    if not first and not last and not full and not stored_email:
                        raise BatchNotReusable("Missing contact name/email for create_contact.")

                    title = _blank(mapped.get("contact_title"))
                    stored_phone = format_us_phone_display(_blank(mapped.get("contact_phone")))

                    cur = conn.execute(
                        """
                        INSERT INTO contacts (
                            company_id, external_record_no,
                            first_name, last_name, title,
                            phone, alt_phone, email,
                            source_row_index
                        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
                        """,
                        (
                            company_id,
                            company_external_record_no,
                            first,
                            last,
                            title,
                            stored_phone,
                            stored_email,
                            int(row.source_row_number),
                        ),
                    )
                    contact_id = int(cur.lastrowid)
                    contact_key_to_id[proposed_key] = contact_id
                    contact_key_to_company_id[proposed_key] = int(company_id)
                    actual_contact_inserts += 1

                    # Deterministic key refresh inside the same transaction.
                    upsert_contact_phone_keys(conn, contact_id, stored_phone, "")
                    if fail_after == "after_contact_insert" and actual_contact_inserts == 1:
                        raise RuntimeError("Injected failure after contact insert.")
                else:
                    # use_existing_contact
                    if row.contact_id is not None:
                        contact_id = int(row.contact_id)
                        # Verify belongs to the resolved company.
                        belongs = conn.execute(
                            "SELECT company_id FROM contacts WHERE id = ?",
                            (contact_id,),
                        ).fetchone()
                        if belongs is None or int(belongs["company_id"]) != int(company_id):
                            raise BatchNotReusable("Plan references contact in a different company.")
                    else:
                        proposed_key = _assert_proposed_key_present(
                            row.contact_proposed_key, "proposed:contact:"
                        )
                        if proposed_key not in contact_key_to_id:
                            raise BatchNotReusable("Missing proposed contact resolution.")
                        contact_id = contact_key_to_id[proposed_key]
                        if contact_key_to_company_id.get(proposed_key) != int(company_id):
                            raise BatchNotReusable("Proposed contact resolved to wrong company.")

                # -----------------
                # Relationship resolve/apply
                # -----------------
                relationship_id: int
                if row.relationship_action == "create_client_relationship":
                    proposed_key = _assert_proposed_key_present(
                        row.relationship_proposed_key, "proposed:relationship:"
                    )
                    if proposed_key in relationship_key_to_id:
                        raise BatchNotReusable("Duplicate proposed relationship creation detected.")

                    resolved_status = _blank(row.resolved_status) or "New"
                    planned_notes = "" if row.planned_notes is None else str(row.planned_notes)
                    if row.notes_action == NOTES_NO_CHANGE:
                        planned_notes = ""
                    cur = conn.execute(
                        """
                        INSERT INTO client_company_relationships (
                            client_id, company_id, external_record_no,
                            status, assigned_user_id, priority, next_action,
                            notes, is_hot, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, NULL, '', '', ?, ?, ?, ?)
                        """,
                        (
                            int(client_id),
                            company_id,
                            company_external_record_no,
                            resolved_status,
                            planned_notes,
                            1 if is_hot_prospect_status(resolved_status) else 0,
                            now,
                            now,
                        ),
                    )
                    relationship_id = int(cur.lastrowid)
                    relationship_key_to_id[proposed_key] = relationship_id
                    relationship_key_to_company_id[proposed_key] = int(company_id)
                    actual_relationship_inserts += 1
                    if fail_after == "after_relationship_insert" and actual_relationship_inserts == 1:
                        raise RuntimeError("Injected failure after relationship insert.")
                else:
                    # relationship_already_exists
                    if row.relationship_id is not None:
                        relationship_id = int(row.relationship_id)
                        # Verify belongs to same client + company.
                        rel = conn.execute(
                            """
                            SELECT client_id, company_id, notes, status
                            FROM client_company_relationships
                            WHERE id = ?
                            """,
                            (relationship_id,),
                        ).fetchone()
                        if rel is None:
                            raise BatchNotReusable("Plan references missing relationship.")
                        if int(rel["client_id"]) != int(client_id) or int(rel["company_id"]) != int(company_id):
                            raise BatchNotReusable("Plan references relationship in a different client/company.")

                        if row.status_action == STATUS_USE_IMPORTED:
                            resolved_status = _blank(row.resolved_status)
                            if not resolved_status:
                                raise BatchNotReusable("Missing resolved status for status replace.")
                            conn.execute(
                                """
                                UPDATE client_company_relationships
                                SET status = ?, is_hot = ?, updated_at = ?
                                WHERE id = ? AND client_id = ? AND company_id = ?
                                """,
                                (
                                    resolved_status,
                                    1 if is_hot_prospect_status(resolved_status) else 0,
                                    now,
                                    relationship_id,
                                    int(client_id),
                                    int(company_id),
                                ),
                            )
                            if int(conn.execute("SELECT changes() AS n").fetchone()["n"]) != 1:
                                raise BatchNotReusable(
                                    "Relationship status UPDATE did not change exactly one row."
                                )
                        elif row.status_action in {STATUS_PRESERVE, STATUS_USE_DEFAULT}:
                            pass
                        else:
                            raise BatchNotReusable("Unsupported status action for existing relationship.")

                        if row.notes_action == NOTES_APPEND:
                            planned_notes = "" if row.planned_notes is None else str(row.planned_notes)
                            conn.execute(
                                """
                                UPDATE client_company_relationships
                                SET notes = ?, updated_at = ?
                                WHERE id = ? AND client_id = ? AND company_id = ?
                                """,
                                (
                                    planned_notes,
                                    now,
                                    relationship_id,
                                    int(client_id),
                                    int(company_id),
                                ),
                            )
                            if int(conn.execute("SELECT changes() AS n").fetchone()["n"]) != 1:
                                raise BatchNotReusable("Relationship notes UPDATE did not change exactly one row.")
                        elif row.notes_action in {NOTES_NO_CHANGE, NOTES_ALREADY_PRESENT}:
                            pass
                        elif row.notes_action == NOTES_SET:
                            raise BatchNotReusable("set_imported_notes is invalid for existing relationships.")
                        else:
                            raise BatchNotReusable("Unsupported notes action for existing relationship.")
                    else:
                        proposed_key = _assert_proposed_key_present(
                            row.relationship_proposed_key, "proposed:relationship:"
                        )
                        if proposed_key not in relationship_key_to_id:
                            raise BatchNotReusable("Missing proposed relationship resolution.")
                        relationship_id = relationship_key_to_id[proposed_key]
                        if relationship_key_to_company_id.get(proposed_key) != int(company_id):
                            raise BatchNotReusable("Proposed relationship resolved to wrong company.")
                        if row.status_action == STATUS_USE_IMPORTED:
                            resolved_status = _blank(row.resolved_status)
                            if not resolved_status:
                                raise BatchNotReusable("Missing resolved status for status replace.")
                            conn.execute(
                                """
                                UPDATE client_company_relationships
                                SET status = ?, is_hot = ?, updated_at = ?
                                WHERE id = ? AND client_id = ? AND company_id = ?
                                """,
                                (
                                    resolved_status,
                                    1 if is_hot_prospect_status(resolved_status) else 0,
                                    now,
                                    relationship_id,
                                    int(client_id),
                                    int(company_id),
                                ),
                            )
                            if int(conn.execute("SELECT changes() AS n").fetchone()["n"]) != 1:
                                raise BatchNotReusable(
                                    "Relationship status UPDATE did not change exactly one row."
                                )
                        if row.notes_action == NOTES_APPEND:
                            planned_notes = "" if row.planned_notes is None else str(row.planned_notes)
                            conn.execute(
                                """
                                UPDATE client_company_relationships
                                SET notes = ?, updated_at = ?
                                WHERE id = ? AND client_id = ? AND company_id = ?
                                """,
                                (
                                    planned_notes,
                                    now,
                                    relationship_id,
                                    int(client_id),
                                    int(company_id),
                                ),
                            )
                            if int(conn.execute("SELECT changes() AS n").fetchone()["n"]) != 1:
                                raise BatchNotReusable("Relationship notes UPDATE did not change exactly one row.")

                # -----------------
                # Results audit row (one per staged row)
                # -----------------
                search_index_targets.append(
                    (int(company_id), int(contact_id) if contact_id is not None else None)
                )
                conn.execute(
                    """
                    INSERT INTO crm_import_results (
                        batch_id, source_row_number, staged_row_id,
                        company_action, company_id,
                        contact_action, contact_id,
                        relationship_action, relationship_id,
                        status_action, notes_action,
                        original_status_action, status_resolution_action, final_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(batch_id),
                        int(row.source_row_number),
                        int(row.row_id),
                        row.company_action,
                        int(company_id),
                        row.contact_action,
                        contact_id if contact_id is not None else None,
                        row.relationship_action,
                        int(relationship_id),
                        row.status_action,
                        row.notes_action,
                        _blank(getattr(row, "original_status_action", "") or row.status_action),
                        _blank(getattr(row, "status_resolution_type", "")),
                        _blank(row.resolved_status),
                    ),
                )
                actual_results_inserts += 1
                if fail_after == "during_result_audit" and actual_results_inserts == 1:
                    raise RuntimeError("Injected failure during result audit insert.")

            # -----------------
            # Final invariants before batch status update
            # -----------------
            if actual_results_inserts != total_imported_row_count:
                raise BatchNotReusable("Result audit rows count does not match plan.total_rows.")
            batch_results_n = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM crm_import_results WHERE batch_id = ?",
                    (int(batch_id),),
                ).fetchone()["n"]
            )
            if batch_results_n != total_imported_row_count:
                raise BatchNotReusable("crm_import_results row count does not match plan.total_rows.")

            if actual_company_inserts != created_company_count:
                raise BatchNotReusable("Company INSERT count does not match plan counters.")
            if actual_contact_inserts != created_contact_count:
                raise BatchNotReusable("Contact INSERT count does not match plan counters.")
            if actual_relationship_inserts != created_relationship_count:
                raise BatchNotReusable("Relationship INSERT count does not match plan counters.")

            # Test-only invariant injection: force terminal UPDATE to miss.
            if fail_after == "force_non_previewed_before_update":
                conn.execute(
                    """
                    UPDATE crm_import_batches
                    SET status = 'cancelled'
                    WHERE id = ? AND client_id = ?
                    """,
                    (int(batch_id), int(client_id)),
                )

            # Update batch audit + terminal status in exactly one statement.
            conn.execute(
                """
                UPDATE crm_import_batches SET
                    status = 'imported',
                    imported_at = ?,
                    imported_by_user_id = ?,
                    confirmed_plan_fingerprint = ?,
                    created_company_count = ?,
                    reused_company_count = ?,
                    created_contact_count = ?,
                    reused_contact_count = ?,
                    created_relationship_count = ?,
                    existing_relationship_count = ?,
                    no_contact_row_count = ?,
                    total_imported_row_count = ?,
                    imported_status_count = ?,
                    default_status_count = ?,
                    preserved_status_count = ?,
                    notes_set_count = ?,
                    notes_appended_count = ?,
                    notes_duplicate_count = ?,
                    notes_unchanged_count = ?,
                    updated_at = ?,
                    headers_json = '[]',
                    warnings_json = '[]',
                    mapping_json = '{}'
                WHERE id = ? AND client_id = ? AND status = 'previewed'
                """,
                (
                    now,
                    int(actor.id),
                    plan.plan_fingerprint,
                    created_company_count,
                    reused_company_count,
                    created_contact_count,
                    reused_contact_count,
                    created_relationship_count,
                    existing_relationship_count,
                    no_contact_row_count,
                    total_imported_row_count,
                    imported_status_count,
                    default_status_count,
                    preserved_status_count,
                    notes_set_count,
                    notes_appended_count,
                    notes_duplicate_count,
                    notes_unchanged_count,
                    now,
                    int(batch_id),
                    int(client_id),
                ),
            )
            if fail_after == "during_batch_completion_update":
                raise RuntimeError("Injected failure during batch completion update.")
            updated = conn.execute(
                """
                SELECT changes() AS n
                """
            ).fetchone()["n"]
            if int(updated) != 1:
                raise BatchNotReusable("Batch terminal UPDATE did not update exactly one row.")

            # Test-only invariant injection: inflate staged-row delete count.
            if fail_after == "inflate_delete_count":
                conn.execute(
                    """
                    INSERT INTO crm_import_rows (
                        batch_id, client_id, source_row_number, raw_json,
                        warnings_json, errors_json, is_blank, has_blocking_error
                    ) VALUES (?, ?, 999999, '{}', '[]', '[]', 0, 0)
                    """,
                    (int(batch_id), int(client_id)),
                )

            # Delete staged rows and verify delete count == plan.total_rows.
            conn.execute(
                """
                DELETE FROM crm_import_rows
                WHERE batch_id = ? AND client_id = ?
                """,
                (int(batch_id), int(client_id)),
            )
            deleted_changes = int(conn.execute("SELECT changes() AS n").fetchone()["n"])
            if deleted_changes != total_imported_row_count:
                raise BatchNotReusable(
                    "Staged row deletion count does not match plan.total_rows."
                )
            if fail_after == "during_staged_row_deletion":
                raise RuntimeError("Injected failure during staged-row deletion.")
            if fail_after == "before_commit":
                raise RuntimeError("Injected failure before commit.")

            conn.commit()

            # Refresh global search docs for imported companies/contacts (after commit).
            for company_id, contact_id in search_index_targets:
                try:
                    index_company_relationship(int(client_id), int(company_id))
                    if contact_id is not None:
                        index_contact_relationship(
                            int(client_id), int(company_id), int(contact_id)
                        )
                except Exception:
                    log.exception(
                        "search_fts index failed after CRM import client_id=%s company_id=%s contact_id=%s",
                        client_id,
                        company_id,
                        contact_id,
                    )

            # Read back the final audit fields for response.
            out = conn.execute(
                """
                SELECT
                    id, client_id, status, imported_at, imported_by_user_id,
                    confirmed_plan_fingerprint,
                    created_company_count, reused_company_count,
                    created_contact_count, reused_contact_count,
                    created_relationship_count, existing_relationship_count,
                    no_contact_row_count, total_imported_row_count,
                    imported_status_count, default_status_count, preserved_status_count,
                    notes_set_count, notes_appended_count, notes_duplicate_count,
                    notes_unchanged_count
                FROM crm_import_batches
                WHERE id = ? AND client_id = ?
                """,
                (int(batch_id), int(client_id)),
            ).fetchone()

            return {
                "batch_id": int(out["id"]),
                "client_id": int(out["client_id"]),
                "status": out["status"],
                "imported_at": out["imported_at"],
                "imported_by_user_id": (
                    int(out["imported_by_user_id"]) if out["imported_by_user_id"] is not None else None
                ),
                "confirmed_plan_fingerprint": out["confirmed_plan_fingerprint"],
                "created_company_count": int(out["created_company_count"]),
                "reused_company_count": int(out["reused_company_count"]),
                "created_contact_count": int(out["created_contact_count"]),
                "reused_contact_count": int(out["reused_contact_count"]),
                "created_relationship_count": int(out["created_relationship_count"]),
                "existing_relationship_count": int(out["existing_relationship_count"]),
                "no_contact_row_count": int(out["no_contact_row_count"]),
                "total_imported_row_count": int(out["total_imported_row_count"]),
                "imported_status_count": int(out["imported_status_count"] or 0),
                "default_status_count": int(out["default_status_count"] or 0),
                "preserved_status_count": int(out["preserved_status_count"] or 0),
                "notes_set_count": int(out["notes_set_count"] or 0),
                "notes_appended_count": int(out["notes_appended_count"] or 0),
                "notes_duplicate_count": int(out["notes_duplicate_count"] or 0),
                "notes_unchanged_count": int(out["notes_unchanged_count"] or 0),
            }
        except Exception:
            conn.rollback()
            raise

