"""Data Steward Phase DS1 — isolated master-data maintenance and provenance.

Canonical vs client-relationship vs provenance vs audit are distinct.
Live production writes are refused. Schema is applied only on isolated DBs.
Does not refresh Carmeco, Brown, or Dawson.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from company_locations import create_company_location
from company_merges import resolve_company_id, resolve_contact_id
from contact_phone import store_phone_parts
from db import PRODUCTION_DB_PATH
from import_brown_industries import digits_phone, norm_name
from models import NorthStarUser

SOURCE_MANUAL_ADMIN = "MANUAL_ADMIN"
SOURCE_MANUAL_STAFF = "MANUAL_STAFF"
SOURCE_LEADMASTER = "LEADMASTER"
SOURCE_CRM_IMPORT = "CRM_IMPORT"
SOURCE_APPOINTMENT_GRID = "APPOINTMENT_GRID"
SOURCE_ZOOMINFO = "ZOOMINFO"
SOURCE_AI_RESEARCH = "AI_RESEARCH"
SOURCE_SYSTEM = "SYSTEM"
SOURCE_MERGE = "MERGE"
SOURCE_LEGACY_EXISTING = "LEGACY_EXISTING"
SOURCE_LEADMASTER_LEGACY = "LEADMASTER_LEGACY"
SOURCE_REF_ACTIVATION = "data_steward_activation"
ACTION_BASELINE = "BASELINE"
ACTION_REMOVE_FROM_CLIENT = "REMOVE_FROM_CLIENT"
ACTION_RESTORE_TO_CLIENT = "RESTORE_TO_CLIENT"

MANUAL_SOURCES = frozenset({SOURCE_MANUAL_ADMIN, SOURCE_MANUAL_STAFF})
ALLOWED_SOURCE_TYPES = frozenset(
    {
        SOURCE_MANUAL_ADMIN,
        SOURCE_MANUAL_STAFF,
        SOURCE_LEADMASTER,
        SOURCE_CRM_IMPORT,
        SOURCE_APPOINTMENT_GRID,
        SOURCE_ZOOMINFO,
        SOURCE_AI_RESEARCH,
        SOURCE_SYSTEM,
        SOURCE_MERGE,
        SOURCE_LEGACY_EXISTING,
        SOURCE_LEADMASTER_LEGACY,
    }
)
TRUSTED_SOURCES = frozenset(
    {
        SOURCE_LEADMASTER,
        SOURCE_CRM_IMPORT,
        SOURCE_APPOINTMENT_GRID,
        SOURCE_ZOOMINFO,
        SOURCE_AI_RESEARCH,
        SOURCE_SYSTEM,
        SOURCE_MERGE,
        SOURCE_LEGACY_EXISTING,
        SOURCE_LEADMASTER_LEGACY,
    }
)

ENTITY_COMPANY = "company"
ENTITY_CONTACT = "contact"
ENTITY_LOCATION = "location"
ENTITY_CLIENT_RELATIONSHIP = "client_relationship"
ENTITY_CCR = "ccr"
ENTITY_NOTE = "note"

NULL_LIKE_VALUES = frozenset(
    {
        "",
        "n/a",
        "na",
        "null",
        "none",
        "-",
        "--",
        "—",
        "unknown",
        "tbd",
        "nil",
        ".",
        "n.a.",
        "not applicable",
    }
)

# Populated business fields only. Identity keys, timestamps, hashes, secrets excluded.
BASELINE_FIELD_CATALOG: dict[str, tuple[tuple[str, str], ...]] = {
    ENTITY_COMPANY: (
        ("company_name", "company_name"),
        ("address", "address"),
        ("city", "city"),
        ("state", "state"),
        ("zip", "zip"),
        ("website", "website"),
        ("legacy_phone", "phone"),
        ("legacy_phone_extension", "phone_extension"),
        ("type_of_industry", "type_of_industry"),
    ),
    ENTITY_CONTACT: (
        ("first_name", "first_name"),
        ("last_name", "last_name"),
        ("title", "title"),
        ("email", "email"),
        ("phone", "phone"),
        ("phone_extension", "phone_extension"),
        ("alt_phone", "alt_phone"),
        ("alt_phone_extension", "alt_phone_extension"),
    ),
    ENTITY_CLIENT_RELATIONSHIP: (
        ("status", "status"),
        ("assigned_user_id", "assigned_user_id"),
        ("next_action", "next_action"),
        ("follow_up_date", "follow_up_date"),
        ("is_hot", "is_hot"),
        ("notes", "notes"),
    ),
    ENTITY_LOCATION: (
        ("location_name", "location_name"),
        ("location_type", "location_type"),
        ("address", "address"),
        ("city", "city"),
        ("state", "state"),
        ("zip", "zip"),
        ("country", "country"),
        ("phone", "phone"),
    ),
}

BASELINE_TABLES = {
    ENTITY_COMPANY: "companies",
    ENTITY_CONTACT: "contacts",
    ENTITY_CLIENT_RELATIONSHIP: "client_company_relationships",
    ENTITY_LOCATION: "company_locations",
}

EXCLUDED_BASELINE_FIELDS = (
    "created_at",
    "last_updated_at",
    "updated_at",
    "archived_at",
    "archived_by_user_id",
    "archive_reason",
    "password_hash",
    "password_updated_at",
    "external_record_no",
    "zoominfo_company_id",
    "zoominfo_contact_id",
    "source_row_index",
    "identity_key",
    "phone_key",
    "source",
    "source_updated_at",
)

_PROVENANCE_FAIL: ContextVar[str] = ContextVar("ds_provenance_fail", default="")
_AUTHORIZED_LIVE_SCHEMA_ACTIVATION: ContextVar[bool] = ContextVar(
    "ds_authorized_live_schema_activation", default=False
)
ENV_LIVE_MIGRATE = "NORTHSTAR_DS_LIVE_MIGRATE"
ENV_LIVE_MIGRATE_EXECUTE = "NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE"

DELETE_ALLOWED = "DELETE_ALLOWED"
ARCHIVE_ONLY = "ARCHIVE_ONLY"
MERGE_RECOMMENDED = "MERGE_RECOMMENDED"
BLOCKED = "BLOCKED"

STALE_EDIT = "stale_edit"
LIVE_STEWARD_WRITES_DISABLED = (
    "Live Data Steward mutations are disabled. Use an isolated database."
)

CAP_VIEW = "master_data.view"
CAP_EDIT = "master_data.edit"
CAP_ARCHIVE = "master_data.archive"
CAP_RESTORE = "master_data.restore"
CAP_DUP_REVIEW = "duplicates.review"
CAP_DUP_EXECUTE = "duplicates.execute"
CAP_CCR_EDIT = "client_relationships.edit"


class StewardError(ValueError):
    """Deterministic Data Steward refusal."""


class StewardLiveWriteError(StewardError):
    pass


class StewardPermissionError(PermissionError):
    pass


def _blank(value: object | None) -> str:
    """Return stripped text. Empty string means blank.

    This is not a boolean. ``if _blank(value)`` is true when the value has
    non-empty text (populated), which is why CREATE provenance records
    populated fields and skips blanks.
    """
    if value is None:
        return ""
    return str(value).strip()


def _has_text(value: object | None) -> bool:
    return bool(_blank(value))


def _is_baseline_value(value: object, *, field: str = "") -> bool:
    if field == "is_hot":
        try:
            return int(value or 0) == 1
        except (TypeError, ValueError):
            return False
    if field == "assigned_user_id":
        try:
            return int(value or 0) > 0
        except (TypeError, ValueError):
            return False
    text = _blank(value)
    if not text:
        return False
    return text.casefold() not in NULL_LIKE_VALUES


def _baseline_anomaly(field: str, value: object) -> str:
    text = _blank(value)
    if field in {"phone", "legacy_phone", "alt_phone"}:
        digits = "".join(ch for ch in text if ch.isdigit())
        if text and len(digits) < 7:
            return "invalid_phone"
    if field == "email" and text and "@" not in text:
        return "invalid_email"
    if len(text) >= 40 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return "hash_like"
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        is not None
    )


def _cols(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def assert_not_production_db(conn=None) -> None:
    from db import DB_PATH

    active = Path(os.fspath(DB_PATH)).resolve()
    if active == PRODUCTION_DB_PATH.resolve():
        raise StewardLiveWriteError(LIVE_STEWARD_WRITES_DISABLED)
    # Conn-file PRAGMA is intentionally not used on the hot write path.
    # CRM import confirm freezes schema PRAGMAs; env path is the fail-closed gate.
    _ = conn


def begin_authorized_live_schema_activation():
    """Enable schema/baseline apply on live. Archive/merge/create stay blocked.

    Requires both live-migrate env flags. Intended only for
    data_steward_live_migrate.py after backup verification.
    """
    if os.environ.get(ENV_LIVE_MIGRATE, "").strip() != "1":
        raise StewardLiveWriteError(
            "Live schema activation requires NORTHSTAR_DS_LIVE_MIGRATE=1"
        )
    if os.environ.get(ENV_LIVE_MIGRATE_EXECUTE, "").strip() != "1":
        raise StewardLiveWriteError(
            "Live schema activation requires NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE=1"
        )
    return _AUTHORIZED_LIVE_SCHEMA_ACTIVATION.set(True)


def end_authorized_live_schema_activation(token) -> None:
    _AUTHORIZED_LIVE_SCHEMA_ACTIVATION.reset(token)


@contextmanager
def authorized_live_schema_activation() -> Iterator[None]:
    token = begin_authorized_live_schema_activation()
    try:
        yield
    finally:
        end_authorized_live_schema_activation(token)


def _assert_schema_or_baseline_allowed(conn=None) -> None:
    """Production assertion for schema apply and LEGACY_EXISTING baseline only."""
    if _AUTHORIZED_LIVE_SCHEMA_ACTIVATION.get():
        return
    assert_not_production_db(conn)


def assert_not_production_path(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == PRODUCTION_DB_PATH.resolve():
        raise StewardLiveWriteError(LIVE_STEWARD_WRITES_DISABLED)
    if resolved.name.lower() == "northstar.db" and resolved.parent.name.lower() == "database":
        raise StewardLiveWriteError(LIVE_STEWARD_WRITES_DISABLED)
    return resolved


def ensure_data_steward_schema(conn) -> dict[str, bool]:
    """Isolated ALTER/CREATE only. Never called from db.migrate_schema."""
    _assert_schema_or_baseline_allowed(conn)
    added: dict[str, bool] = {}
    provenance_existed = _table_exists(conn, "field_provenance_events")
    archive_cols = [
        ("archived_at", "TEXT NOT NULL DEFAULT ''"),
        ("archived_by_user_id", "INTEGER"),
        ("archive_reason", "TEXT NOT NULL DEFAULT ''"),
    ]
    for table in (
        "companies",
        "contacts",
        "company_locations",
        "client_company_relationships",
    ):
        if not _table_exists(conn, table):
            continue
        cols = _cols(conn, table)
        for name, decl in archive_cols:
            key = f"{table}.{name}"
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added[key] = True
            else:
                added[key] = False
    if _table_exists(conn, "contacts") and "last_updated_at" not in _cols(conn, "contacts"):
        conn.execute(
            "ALTER TABLE contacts ADD COLUMN last_updated_at TEXT NOT NULL DEFAULT ''"
        )
        added["contacts.last_updated_at"] = True
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS field_provenance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            field TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL DEFAULT '',
            changed_by_user_id INTEGER,
            changed_at TEXT NOT NULL DEFAULT (datetime('now')),
            action TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            client_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_field_prov_entity
            ON field_provenance_events(entity_type, entity_id, field, id);
        CREATE INDEX IF NOT EXISTS idx_field_prov_changed
            ON field_provenance_events(changed_at);
        """
    )
    archive_indexes = (
        ("companies", "idx_companies_archived_at"),
        ("contacts", "idx_contacts_archived_at"),
        ("company_locations", "idx_locations_archived_at"),
        ("client_company_relationships", "idx_ccr_archived_at"),
    )
    for table, index_name in archive_indexes:
        if _table_exists(conn, table) and "archived_at" in _cols(conn, table):
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}(archived_at)"
            )
    added["field_provenance_events"] = not provenance_existed
    return added


def provenance_table_ready(conn) -> bool:
    return _table_exists(conn, "field_provenance_events")


def current_field_authority(
    conn,
    *,
    entity_type: str,
    entity_id: int,
    field: str,
    current_value: object = None,
) -> dict[str, Any]:
    row = latest_provenance(
        conn, entity_type=entity_type, entity_id=entity_id, field=field
    )
    return {
        "entity_type": entity_type,
        "entity_id": int(entity_id),
        "field": _blank(field),
        "current_value": "" if current_value is None else _blank(current_value),
        "source_type": _blank(row.get("source_type")) if row else "",
        "changed_by_user_id": row.get("changed_by_user_id") if row else None,
        "changed_at": _blank(row.get("changed_at")) if row else "",
        "previous_value": _blank(row.get("old_value")) if row else "",
        "action": _blank(row.get("action")) if row else "",
        "source_ref": _blank(row.get("source_ref")) if row else "",
        "reason": _blank(row.get("reason")) if row else "",
        "has_provenance": row is not None,
    }


def provenance_history(
    conn,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    field: str = "",
    source_type: str = "",
    actor_id: int | None = None,
    since: str = "",
    until: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not provenance_table_ready(conn):
        return []
    clauses = ["1=1"]
    params: list[object] = []
    if entity_type:
        aliases = _entity_type_aliases(entity_type)
        placeholders = ",".join("?" for _ in aliases)
        clauses.append(f"e.entity_type IN ({placeholders})")
        params.extend(aliases)
    if entity_id:
        clauses.append("e.entity_id = ?")
        params.append(int(entity_id))
    if _has_text(field):
        clauses.append("e.field = ?")
        params.append(_blank(field))
    if _has_text(source_type):
        clauses.append("e.source_type = ?")
        params.append(_blank(source_type))
    if actor_id:
        clauses.append("e.changed_by_user_id = ?")
        params.append(int(actor_id))
    if _has_text(since):
        clauses.append("e.changed_at >= ?")
        params.append(_blank(since))
    if _has_text(until):
        clauses.append("e.changed_at <= ?")
        params.append(_blank(until))
    params.append(int(limit))
    user_join = ""
    name_select = "'' AS changed_by_name"
    if _table_exists(conn, "users"):
        user_join = "LEFT JOIN users u ON u.id = e.changed_by_user_id"
        name_select = "COALESCE(u.full_name, '') AS changed_by_name"
    rows = conn.execute(
        f"""
        SELECT e.*, {name_select}
        FROM field_provenance_events e
        {user_join}
        WHERE {' AND '.join(clauses)}
        ORDER BY e.id DESC LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def apply_data_steward_schema_isolated(conn) -> dict[str, bool]:
    """Idempotent isolated schema apply inside one transaction."""
    _assert_schema_or_baseline_allowed(conn)
    started = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        started = True
    except Exception:
        started = False
    try:
        added = ensure_data_steward_schema(conn)
        if started:
            conn.commit()
        return added
    except Exception:
        if started:
            conn.rollback()
        raise


def sql_not_archived(conn, table: str, alias: str | None = None) -> str:
    """Operational filter. Duplicate/refresh matching must NOT use this."""
    label = alias or table
    if "archived_at" not in _cols(conn, table):
        return "1=1"
    return f"TRIM(COALESCE({label}.archived_at,'')) = ''"


def sql_active_ccr(conn, alias: str = "ccr") -> str:
    return sql_not_archived(conn, "client_company_relationships", alias)


def sql_active_company(conn, alias: str = "co") -> str:
    return sql_not_archived(conn, "companies", alias)


def sql_active_contact(conn, alias: str = "ct") -> str:
    return sql_not_archived(conn, "contacts", alias)


def list_operational_contacts(conn, *, company_id: int | None = None) -> list[dict[str, Any]]:
    where = [sql_active_contact(conn, "ct")]
    params: list[object] = []
    if company_id:
        where.append("ct.company_id = ?")
        params.append(int(company_id))
    rows = conn.execute(
        f"SELECT ct.* FROM contacts ct WHERE {' AND '.join(where)} ORDER BY ct.id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def list_operational_relationships(conn, *, client_id: int | None = None) -> list[dict[str, Any]]:
    where = [sql_active_ccr(conn, "ccr")]
    params: list[object] = []
    if client_id:
        where.append("ccr.client_id = ?")
        params.append(int(client_id))
    rows = conn.execute(
        f"SELECT ccr.* FROM client_company_relationships ccr WHERE {' AND '.join(where)} ORDER BY ccr.id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


CONTACT_DEP_CLASS = {
    "activities": "MOVE_TO_SURVIVOR",
    "campaign_contacts": "UNION_DEDUPE",
    "contact_client_workflows": "UNION_DEDUPE",
    "contact_phone_keys": "UNION_DEDUPE",
    "contact_person_keys": "UNION_DEDUPE",
    "contact_merge_history": "PRESERVE",
    "appointments": "MOVE_TO_SURVIVOR",
    "client_appointments": "MOVE_TO_SURVIVOR",
    "client_sales_events": "MOVE_TO_SURVIVOR",
    "field_provenance_events": "PRESERVE",
    "legacy_notes": "PRESERVE",
    "company_shared_history_events": "MOVE_TO_SURVIVOR",
    "crm_import_results": "AUDIT_ONLY",
}


def inspect_contact_dependencies(conn, contact_id: int) -> dict[str, Any]:
    """Discover formal FKs and logical contact_id columns. Isolated DBs only."""
    assert_not_production_db(conn)
    from company_merge_execute import SKIP_CONTACT_REMAP, _tables_with_column

    cid = int(contact_id)
    formal: list[dict[str, Any]] = []
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if str(fk[2]).lower() != "contacts":
                continue
            col = str(fk[3])
            n = int(
                conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" = ?',
                    (cid,),
                ).fetchone()[0]
            )
            formal.append(
                {
                    "table": str(table),
                    "column": col,
                    "on_delete": str(fk[6] or ""),
                    "row_count": n,
                    "kind": "formal_fk",
                    "class": CONTACT_DEP_CLASS.get(str(table), "MOVE_TO_SURVIVOR"),
                }
            )
    logical: list[dict[str, Any]] = []
    seen = {(r["table"], r["column"]) for r in formal}
    for table in _tables_with_column(conn, "contact_id"):
        if (table, "contact_id") in seen:
            continue
        n = int(
            conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE contact_id = ?',
                (cid,),
            ).fetchone()[0]
        )
        logical.append(
            {
                "table": table,
                "column": "contact_id",
                "on_delete": "",
                "row_count": n,
                "kind": "logical",
                "class": CONTACT_DEP_CLASS.get(
                    table,
                    "PRESERVE" if table in SKIP_CONTACT_REMAP else "MOVE_TO_SURVIVOR",
                ),
            }
        )
    return {
        "contact_id": cid,
        "formal_fks": formal,
        "logical_refs": logical,
        "skip_remap": sorted(SKIP_CONTACT_REMAP),
    }


def inspect_relationship_dependencies(conn, ccr_id: int) -> dict[str, Any]:
    stored = _load(conn, "client_company_relationships", int(ccr_id))
    cid = int(stored["company_id"])
    client_id = int(stored["client_id"])
    counts = {
        "campaigns": _count_where(
            conn,
            "campaign_companies",
            "client_id = ? AND company_id = ?",
            (client_id, cid),
        ),
        "activities": _count_where(
            conn,
            "activities",
            "relationship_id = ?",
            (int(ccr_id),),
        ),
        "notes": 1 if _blank(stored.get("notes")) else 0,
        "legacy_notes": _count_where(
            conn,
            "legacy_notes",
            "client_id = ? AND company_id = ?",
            (client_id, cid),
        ),
        "history": _count_where(
            conn,
            "company_shared_history_events",
            "company_id = ?",
            (cid,),
        ),
        "open_follow_up": 1 if _blank(stored.get("follow_up_date")) else 0,
        "open_next_action": 1 if _blank(stored.get("next_action")) else 0,
        "hot": 1 if int(stored.get("is_hot") or 0) else 0,
    }
    return {
        "ccr_id": int(ccr_id),
        "company_id": cid,
        "client_id": client_id,
        "counts": counts,
        "policy": "preserve_all_exclude_from_active_queue",
    }


def _count_where(conn, table: str, where: str, params: tuple) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])


def conservative_legacy_backfill(
    conn,
    *,
    dry_run: bool = True,
    changed_at: str | None = None,
    source_ref: str = SOURCE_REF_ACTIVATION,
) -> dict[str, Any]:
    """Conservative LEGACY_EXISTING baseline. Isolated only.

    Activation timestamp means the value existed when provenance tracking
    was activated. It does not mean the value was created on that date.
    Company-level source identity never claims field-level LEADMASTER origin.
    """
    _assert_schema_or_baseline_allowed(conn)
    ensure_data_steward_schema(conn)
    activation_at = _blank(changed_at) or _now()
    source_ref = _blank(source_ref) or SOURCE_REF_ACTIVATION
    planned = 0
    written = 0
    skipped_existing = 0
    skipped_blank = 0
    entities_with_events: dict[str, set[int]] = {k: set() for k in BASELINE_FIELD_CATALOG}
    field_counts: dict[str, int] = {}
    anomalies: list[dict[str, Any]] = []

    def _consider(entity_type: str, entity_id: int, column: str, written_field: str, value: object) -> None:
        nonlocal planned, written, skipped_existing, skipped_blank
        if not _is_baseline_value(value, field=column):
            skipped_blank += 1
            return
        anomaly = _baseline_anomaly(written_field, value)
        if anomaly:
            if len(anomalies) < 200:
                anomalies.append(
                    {
                        "entity_type": entity_type,
                        "entity_id": int(entity_id),
                        "field": written_field,
                        "kind": anomaly,
                        "preview": _blank(value)[:80],
                    }
                )
        existing = latest_provenance(
            conn, entity_type=entity_type, entity_id=int(entity_id), field=written_field
        )
        if existing:
            skipped_existing += 1
            return
        planned += 1
        entities_with_events[entity_type].add(int(entity_id))
        field_key = f"{entity_type}.{written_field}"
        field_counts[field_key] = field_counts.get(field_key, 0) + 1
        if dry_run:
            return
        record_provenance(
            conn,
            entity_type=entity_type,
            entity_id=int(entity_id),
            field=written_field,
            old_value="",
            new_value=value,
            source_type=SOURCE_LEGACY_EXISTING,
            action=ACTION_BASELINE,
            reason="value_existed_at_data_steward_activation",
            source_ref=source_ref,
            changed_at=activation_at,
            trusted=True,
        )
        written += 1

    def _run() -> None:
        for entity_type, pairs in BASELINE_FIELD_CATALOG.items():
            table = BASELINE_TABLES[entity_type]
            if not _table_exists(conn, table):
                continue
            cols = _cols(conn, table)
            usable = [(col, field) for col, field in pairs if col in cols]
            if not usable:
                continue
            select_cols = ", ".join(["id"] + [col for col, _ in usable])
            for row in conn.execute(f"SELECT {select_cols} FROM {table}").fetchall():
                for col, field in usable:
                    _consider(entity_type, int(row["id"]), col, field, row[col])

    if dry_run:
        _run()
    else:
        with _amend_transaction(conn):
            _run()

    identity_count = 0
    if _table_exists(conn, "company_source_identities"):
        identity_count = int(
            conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0]
        )
    return {
        "dry_run": dry_run,
        "planned_events": planned,
        "written_events": written,
        "skipped_existing": skipped_existing,
        "skipped_blank_or_null_like": skipped_blank,
        "activation_at": activation_at,
        "source_ref": source_ref,
        "action": ACTION_BASELINE,
        "source_type": SOURCE_LEGACY_EXISTING,
        "source_identities": identity_count,
        "leadmaster_legacy_fields": 0,
        "companies_with_events": len(entities_with_events[ENTITY_COMPANY]),
        "company_field_events": sum(
            v for k, v in field_counts.items() if k.startswith(f"{ENTITY_COMPANY}.")
        ),
        "contacts_with_events": len(entities_with_events[ENTITY_CONTACT]),
        "contact_field_events": sum(
            v for k, v in field_counts.items() if k.startswith(f"{ENTITY_CONTACT}.")
        ),
        "ccrs_with_events": len(entities_with_events[ENTITY_CLIENT_RELATIONSHIP]),
        "ccr_field_events": sum(
            v for k, v in field_counts.items() if k.startswith(f"{ENTITY_CLIENT_RELATIONSHIP}.")
        ),
        "locations_with_events": len(entities_with_events[ENTITY_LOCATION]),
        "location_field_events": sum(
            v for k, v in field_counts.items() if k.startswith(f"{ENTITY_LOCATION}.")
        ),
        "field_counts": dict(sorted(field_counts.items())),
        "anomalies": anomalies,
        "note": (
            "Company-level source identity does not claim field-level LEADMASTER origin. "
            "Activation timestamp means the value existed when Data Steward provenance "
            "tracking was activated, not that the value was created on that date."
        ),
    }


def preview_legacy_baseline(conn, *, changed_at: str | None = None) -> dict[str, Any]:
    return conservative_legacy_backfill(conn, dry_run=True, changed_at=changed_at)


def sanitize_source_ref(value: object) -> str:
    text = _blank(value)
    lowered = text.lower()
    if any(token in lowered for token in ("password", "token", "secret", "authorization", "api_key", "bearer ")):
        return ""
    return text[:200]


def source_ref_manual() -> str:
    return "manual"


def source_ref_batch(kind: str, batch_id: int | None) -> str:
    label = _blank(kind) or "batch"
    if batch_id in (None, ""):
        return label
    return f"{label}:{int(batch_id)}"


@contextmanager
def _amend_transaction(conn) -> Iterator[None]:
    """Nested-safe atomic unit: business change + provenance, or neither."""
    conn.execute("SAVEPOINT ds_amend")
    try:
        yield
        conn.execute("RELEASE SAVEPOINT ds_amend")
    except Exception:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT ds_amend")
        except Exception:
            pass
        try:
            conn.execute("RELEASE SAVEPOINT ds_amend")
        except Exception:
            pass
        raise


@contextmanager
def force_provenance_failure(reason: str = "provenance_insert_failed") -> Iterator[None]:
    token = _PROVENANCE_FAIL.set(_blank(reason) or "provenance_insert_failed")
    try:
        yield
    finally:
        _PROVENANCE_FAIL.reset(token)


def record_provenance(
    conn,
    *,
    entity_type: str,
    entity_id: int,
    field: str,
    old_value: object = "",
    new_value: object = "",
    source_type: str = "",
    changed_by_user_id: int | None = None,
    action: str = "",
    reason: str = "",
    source_ref: str = "",
    client_id: int | None = None,
    changed_at: str = "",
    actor: NorthStarUser | None = None,
    trusted: bool = False,
) -> None:
    injected = _PROVENANCE_FAIL.get()
    if injected:
        raise StewardError(injected)
    if not provenance_table_ready(conn):
        return
    if trusted:
        resolved = _blank(source_type)
        if resolved not in ALLOWED_SOURCE_TYPES:
            resolved = SOURCE_SYSTEM
        if actor is not None and changed_by_user_id is None:
            changed_by_user_id = int(actor.id)
    elif actor is not None:
        resolved = source_type_for_actor(actor)
        changed_by_user_id = int(actor.id)
    else:
        resolved = _blank(source_type)
        if resolved in MANUAL_SOURCES:
            pass
        elif resolved in TRUSTED_SOURCES:
            resolved = SOURCE_SYSTEM
        elif resolved not in ALLOWED_SOURCE_TYPES:
            resolved = SOURCE_SYSTEM
    conn.execute(
        """
        INSERT INTO field_provenance_events (
            entity_type, entity_id, field, old_value, new_value, source_type,
            source_ref, changed_by_user_id, changed_at, action, reason, client_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            int(entity_id),
            _blank(field),
            _blank(old_value),
            _blank(new_value),
            resolved or SOURCE_SYSTEM,
            sanitize_source_ref(source_ref),
            int(changed_by_user_id) if changed_by_user_id else None,
            changed_at or _now(),
            _blank(action),
            _blank(reason),
            int(client_id) if client_id else None,
        ),
    )


def record_populated_creates(
    conn,
    *,
    entity_type: str,
    entity_id: int,
    fields: dict[str, object],
    actor: NorthStarUser | None = None,
    source_type: str = "",
    source_ref: str = "",
    action: str = "CREATE",
    client_id: int | None = None,
    trusted: bool = False,
    changed_by_user_id: int | None = None,
) -> None:
    now = _now()
    for field, value in fields.items():
        if not _has_text(value):
            continue
        record_provenance(
            conn,
            entity_type=entity_type,
            entity_id=int(entity_id),
            field=field,
            new_value=value,
            source_type=source_type,
            source_ref=source_ref,
            action=action,
            client_id=client_id,
            changed_at=now,
            actor=actor,
            trusted=trusted,
            changed_by_user_id=changed_by_user_id,
        )


def record_changed_fields(
    conn,
    *,
    entity_type: str,
    entity_id: int,
    changes: list[tuple[str, object, object]],
    actor: NorthStarUser | None = None,
    source_type: str = "",
    source_ref: str = "",
    action: str = "AMEND",
    client_id: int | None = None,
    trusted: bool = False,
    changed_by_user_id: int | None = None,
) -> None:
    now = _now()
    for field, old, new in changes:
        if _blank(old) == _blank(new):
            continue
        record_provenance(
            conn,
            entity_type=entity_type,
            entity_id=int(entity_id),
            field=field,
            old_value=old,
            new_value=new,
            source_type=source_type,
            source_ref=source_ref,
            action=action,
            client_id=client_id,
            changed_at=now,
            actor=actor,
            trusted=trusted,
            changed_by_user_id=changed_by_user_id,
        )


def _entity_type_aliases(entity_type: str) -> list[str]:
    key = _blank(entity_type)
    if key in {ENTITY_CCR, ENTITY_CLIENT_RELATIONSHIP, ENTITY_NOTE}:
        return [ENTITY_CLIENT_RELATIONSHIP, ENTITY_CCR, ENTITY_NOTE]
    return [key]


def latest_provenance(
    conn, *, entity_type: str, entity_id: int, field: str
) -> dict[str, Any] | None:
    if not provenance_table_ready(conn) or not entity_id:
        return None
    aliases = _entity_type_aliases(entity_type)
    placeholders = ",".join("?" for _ in aliases)
    row = conn.execute(
        f"""
        SELECT * FROM field_provenance_events
        WHERE entity_type IN ({placeholders}) AND entity_id = ? AND field = ?
        ORDER BY id DESC LIMIT 1
        """,
        (*aliases, int(entity_id), _blank(field)),
    ).fetchone()
    return dict(row) if row is not None else None


def is_manual_authority(conn, *, entity_type: str, entity_id: int, field: str) -> bool:
    row = latest_provenance(
        conn, entity_type=entity_type, entity_id=entity_id, field=field
    )
    if row is None:
        return False
    return _blank(row.get("source_type")) in MANUAL_SOURCES


def provenance_evidence(
    conn, *, entity_type: str, entity_id: int, field: str
) -> list[str]:
    row = latest_provenance(
        conn, entity_type=entity_type, entity_id=entity_id, field=field
    )
    if row is None:
        return []
    return [
        f"source_type={row.get('source_type')}",
        f"changed_by_user_id={row.get('changed_by_user_id') or ''}",
        f"changed_at={row.get('changed_at') or ''}",
        f"action={row.get('action') or ''}",
    ]


def is_archived(conn, table: str, entity_id: int) -> bool:
    cols = _cols(conn, table)
    if "archived_at" not in cols or not entity_id:
        return False
    row = conn.execute(
        f"SELECT archived_at FROM {table} WHERE id = ?",
        (int(entity_id),),
    ).fetchone()
    if row is None:
        return False
    return bool(_blank(row["archived_at"] if "archived_at" in row.keys() else row[0]))


def source_type_for_actor(actor: NorthStarUser | None) -> str:
    if actor is None:
        return SOURCE_SYSTEM
    if bool(getattr(actor, "is_administrator", False)):
        return SOURCE_MANUAL_ADMIN
    return SOURCE_MANUAL_STAFF


def require_capability(
    actor: NorthStarUser | None,
    capability: str,
    *,
    client_id: int | None = None,
    conn=None,
) -> None:
    if actor is None or not bool(getattr(actor, "active", False)):
        raise StewardPermissionError("Authentication required.")
    from staff_rbac import user_has_permission

    if bool(getattr(actor, "is_administrator", False)):
        return
    if not user_has_permission(int(actor.id), capability, client_id=client_id, conn=conn):
        raise StewardPermissionError(f"Missing capability {capability}.")


def _stamp(conn, table: str, entity_id: int, when: str) -> None:
    cols = _cols(conn, table)
    if "last_updated_at" in cols:
        conn.execute(
            f"UPDATE {table} SET last_updated_at = ? WHERE id = ?",
            (when, int(entity_id)),
        )
    elif "updated_at" in cols:
        conn.execute(
            f"UPDATE {table} SET updated_at = ? WHERE id = ?",
            (when, int(entity_id)),
        )


def _current_token(conn, table: str, entity_id: int) -> str:
    cols = _cols(conn, table)
    for col in ("last_updated_at", "updated_at", "created_at"):
        if col in cols:
            row = conn.execute(
                f"SELECT {col} FROM {table} WHERE id = ?",
                (int(entity_id),),
            ).fetchone()
            if row is not None:
                return _blank(row[0])
    return ""


def _refuse_stale(conn, table: str, entity_id: int, expected: str | None) -> None:
    if expected is None:
        return
    current = _current_token(conn, table, entity_id)
    if _blank(expected) != current:
        raise StewardError(STALE_EDIT)


def _load(conn, table: str, entity_id: int) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(entity_id),)).fetchone()
    if row is None:
        raise LookupError(f"{table} {entity_id} not found.")
    return dict(row)


def find_company_duplicates(
    conn,
    *,
    company_name: str,
    city: str = "",
    state: str = "",
    exclude_id: int | None = None,
) -> list[dict[str, Any]]:
    """Includes archived rows. Name-only is a warning, never a silent link."""
    want = norm_name(company_name)
    if not want:
        return []
    rows = conn.execute("SELECT id, company_name, city, state, external_record_no FROM companies").fetchall()
    hits = []
    for row in rows:
        if exclude_id and int(row["id"]) == int(exclude_id):
            continue
        if norm_name(row["company_name"]) != want:
            continue
        city_ok = not _blank(city) or _blank(row["city"]).lower() == _blank(city).lower()
        state_ok = not _blank(state) or _blank(row["state"]).upper() == _blank(state).upper()
        if city_ok and state_ok:
            item = dict(row)
            item["archived"] = is_archived(conn, "companies", int(row["id"]))
            hits.append(item)
    return hits


def list_operational_companies(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT id, company_name FROM companies co
        WHERE {sql_active_company(conn)}
        ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def create_company(
    conn,
    *,
    actor: NorthStarUser,
    company_name: str,
    address: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    phone: str = "",
    website: str = "",
    confirm_despite_match: bool = False,
    client_id: int | None = None,
) -> dict[str, Any]:
    assert_not_production_db(conn)
    require_capability(actor, CAP_EDIT, conn=conn)
    ensure_data_steward_schema(conn)
    name = _blank(company_name)
    if not name:
        raise StewardError("Company name is required.")
    dups = find_company_duplicates(conn, company_name=name, city=city, state=state)
    if dups and not confirm_despite_match:
        raise StewardError("duplicate_company")
    walked = None
    if name.isdigit():
        walked = resolve_company_id(conn, int(name))
        if walked != int(name):
            raise StewardError("merge_redirect")
    from crm_add_data import allocate_ns_record_no

    phone_store, ext = store_phone_parts(phone, field="legacy_phone")
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, address, city, state, zip, website,
            legacy_phone, legacy_phone_extension, created_at, last_updated_at, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            allocate_ns_record_no(conn),
            name,
            _blank(address),
            _blank(city),
            _blank(state),
            _blank(zip_code),
            _blank(website),
            phone_store,
            ext or None,
            now,
            now,
            SOURCE_MANUAL_ADMIN if actor.is_administrator else SOURCE_MANUAL_STAFF,
        ),
    )
    company_id = int(cur.lastrowid)
    src = source_type_for_actor(actor)
    for field, value in (
        ("company_name", name),
        ("address", address),
        ("city", city),
        ("state", state),
        ("zip", zip_code),
        ("phone", phone_store),
        ("website", website),
    ):
        if not _has_text(value):
            continue
        record_provenance(
            conn,
            entity_type=ENTITY_COMPANY,
            entity_id=company_id,
            field=field,
            new_value=value,
            source_type=src,
            changed_by_user_id=int(actor.id),
            action="CREATE",
            reason="manual_create",
            client_id=client_id,
            changed_at=now,
            actor=actor,
        )
    return {"company_id": company_id, "duplicates": dups}


def amend_company(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    fields: dict[str, Any],
    expected_updated_at: str | None = None,
    reason: str = "",
    force_fail_after: str = "",
) -> dict[str, Any]:
    require_capability(actor, CAP_EDIT, conn=conn)
    _prepare_company_amend(conn)
    company_id = resolve_company_id(conn, int(company_id))
    stored = _load(conn, "companies", company_id)
    _refuse_stale(conn, "companies", company_id, expected_updated_at)
    if is_archived(conn, "companies", company_id):
        raise StewardError("archived")
    src = source_type_for_actor(actor)
    now = _now()
    allowed = {
        "company_name": "company_name",
        "address": "address",
        "city": "city",
        "state": "state",
        "zip": "zip",
        "website": "website",
        "phone": "legacy_phone",
        "phone_extension": "legacy_phone_extension",
    }
    changed: list[str] = []
    try:
        with _amend_transaction(conn):
            _run_company_amend_body(
                conn, actor=actor, company_id=company_id, fields=fields, stored=stored,
                allowed=allowed, src=src, now=now, reason=reason,
                force_fail_after=force_fail_after, changed=changed,
            )
    except Exception:
        raise
    return {"company_id": company_id, "changed": changed, "updated_at": now}


def _run_company_amend_body(
    conn, *, actor, company_id, fields, stored, allowed, src, now, reason, force_fail_after, changed,
) -> None:
        for key, value in fields.items():
            col = allowed.get(key)
            if not col:
                continue
            new_val = _blank(value)
            if key == "phone":
                new_val, ext = store_phone_parts(new_val, field="legacy_phone")
                old = _blank(stored.get("legacy_phone"))
                if digits_phone(old) == digits_phone(new_val) and old and new_val:
                    continue
                conn.execute(
                    "UPDATE companies SET legacy_phone = ?, legacy_phone_extension = ? WHERE id = ?",
                    (new_val, ext or None, company_id),
                )
                if key == "phone" and "phone_extension" in fields:
                    pass
                record_provenance(
                    conn,
                    entity_type=ENTITY_COMPANY,
                    entity_id=company_id,
                    field="phone",
                    old_value=old,
                    new_value=new_val,
                    source_type=src,
                    changed_by_user_id=int(actor.id),
                    action="AMEND",
                    reason=reason,
                    changed_at=now,
                )
                changed.append("phone")
                if force_fail_after == "after_phone":
                    raise StewardError("forced_rollback")
                continue
            if key == "phone_extension":
                old = _blank(stored.get("legacy_phone_extension"))
                if old == new_val:
                    continue
                conn.execute(
                    "UPDATE companies SET legacy_phone_extension = ? WHERE id = ?",
                    (new_val or None, company_id),
                )
                record_provenance(
                    conn,
                    entity_type=ENTITY_COMPANY,
                    entity_id=company_id,
                    field="phone_extension",
                    old_value=old,
                    new_value=new_val,
                    source_type=src,
                    changed_by_user_id=int(actor.id),
                    action="AMEND",
                    reason=reason,
                    changed_at=now,
                )
                changed.append("phone_extension")
                continue
            old = _blank(stored.get(col))
            if old == new_val:
                continue
            if key == "company_name" and find_company_duplicates(
                conn, company_name=new_val, city=_blank(fields.get("city") or stored.get("city")),
                state=_blank(fields.get("state") or stored.get("state")),
                exclude_id=company_id,
            ):
                raise StewardError("duplicate_company")
            conn.execute(f"UPDATE companies SET {col} = ? WHERE id = ?", (new_val, company_id))
            record_provenance(
                conn,
                entity_type=ENTITY_COMPANY,
                entity_id=company_id,
                field=key,
                old_value=old,
                new_value=new_val,
                source_type=src,
                changed_by_user_id=int(actor.id),
                action="AMEND",
                reason=reason,
                changed_at=now,
            )
            changed.append(key)
            if force_fail_after == f"after_{key}":
                raise StewardError("forced_rollback")
        if changed:
            _stamp(conn, "companies", company_id, now)
        if force_fail_after == "before_commit":
            raise StewardError("forced_rollback")


def _archive_row(conn, table: str, entity_id: int, actor: NorthStarUser, reason: str, entity_type: str) -> None:
    assert_not_production_db(conn)
    require_capability(actor, CAP_ARCHIVE, conn=conn)
    ensure_data_steward_schema(conn)
    now = _now()
    conn.execute(
        f"""
        UPDATE {table}
        SET archived_at = ?, archived_by_user_id = ?, archive_reason = ?
        WHERE id = ?
        """,
        (now, int(actor.id), _blank(reason), int(entity_id)),
    )
    record_provenance(
        conn,
        entity_type=entity_type,
        entity_id=int(entity_id),
        field="archived_at",
        old_value="",
        new_value=now,
        source_type=source_type_for_actor(actor),
        changed_by_user_id=int(actor.id),
        action="ARCHIVE",
        reason=reason,
        changed_at=now,
    )


def _restore_row(conn, table: str, entity_id: int, actor: NorthStarUser, entity_type: str) -> int:
    assert_not_production_db(conn)
    require_capability(actor, CAP_RESTORE, conn=conn)
    ensure_data_steward_schema(conn)
    now = _now()
    conn.execute(
        f"""
        UPDATE {table}
        SET archived_at = '', archived_by_user_id = NULL, archive_reason = ''
        WHERE id = ?
        """,
        (int(entity_id),),
    )
    record_provenance(
        conn,
        entity_type=entity_type,
        entity_id=int(entity_id),
        field="archived_at",
        old_value="archived",
        new_value="",
        source_type=source_type_for_actor(actor),
        changed_by_user_id=int(actor.id),
        action="RESTORE",
        changed_at=now,
    )
    return int(entity_id)


def archive_company(conn, *, actor: NorthStarUser, company_id: int, reason: str = "") -> None:
    _archive_row(conn, "companies", resolve_company_id(conn, int(company_id)), actor, reason, ENTITY_COMPANY)


def restore_company(conn, *, actor: NorthStarUser, company_id: int) -> int:
    return _restore_row(conn, "companies", int(company_id), actor, ENTITY_COMPANY)


def inspect_company_dependencies(conn, company_id: int) -> dict[str, Any]:
    cid = int(company_id)
    counts = {
        "relationships": _count(conn, "client_company_relationships", "company_id", cid),
        "contacts": _count(conn, "contacts", "company_id", cid),
        "locations": _count(conn, "company_locations", "company_id", cid) if _table_exists(conn, "company_locations") else 0,
        "source_identities": _count(conn, "company_source_identities", "company_id", cid) if _table_exists(conn, "company_source_identities") else 0,
        "aliases": _count(conn, "company_aliases", "company_id", cid) if _table_exists(conn, "company_aliases") else 0,
        "notes": _count(conn, "legacy_notes", "company_id", cid) if _table_exists(conn, "legacy_notes") else 0,
        "history": _count(conn, "company_shared_history_events", "company_id", cid) if _table_exists(conn, "company_shared_history_events") else 0,
        "activities": _count(conn, "activities", "company_id", cid) if _table_exists(conn, "activities") else 0,
        "campaigns": _count(conn, "campaign_companies", "company_id", cid) if _table_exists(conn, "campaign_companies") else 0,
        "merge_as_source": _count(conn, "company_merge_history", "source_company_id", cid) if _table_exists(conn, "company_merge_history") else 0,
        "merge_as_survivor": _count(conn, "company_merge_history", "survivor_company_id", cid) if _table_exists(conn, "company_merge_history") else 0,
        "sales": _count(conn, "sales_events", "company_id", cid) if _table_exists(conn, "sales_events") else 0,
        "appointments": _count(conn, "appointments", "company_id", cid) if _table_exists(conn, "appointments") else 0,
        "provenance": (
            conn.execute(
                "SELECT COUNT(*) FROM field_provenance_events WHERE entity_type='company' AND entity_id=?",
                (cid,),
            ).fetchone()[0]
            if provenance_table_ready(conn)
            else 0
        ),
    }
    protected = [
        "relationships",
        "contacts",
        "locations",
        "source_identities",
        "notes",
        "history",
        "activities",
        "campaigns",
        "sales",
        "appointments",
        "merge_as_source",
        "merge_as_survivor",
        "aliases",
    ]
    has_protected = any(int(counts[k] or 0) > 0 for k in protected)
    classification = DELETE_ALLOWED
    if counts["merge_as_source"] or counts["merge_as_survivor"]:
        classification = BLOCKED
    elif counts["sales"] or counts["appointments"]:
        classification = BLOCKED
    elif has_protected:
        classification = ARCHIVE_ONLY
        stored = conn.execute(
            "SELECT company_name, city, state FROM companies WHERE id=?", (cid,)
        ).fetchone()
        if stored is not None:
            dups = find_company_duplicates(
                conn,
                company_name=stored["company_name"],
                city=stored["city"],
                state=stored["state"],
                exclude_id=cid,
            )
            if dups:
                classification = MERGE_RECOMMENDED
    return {"company_id": cid, "counts": counts, "classification": classification}


def _count(conn, table: str, col: str, value: int) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (int(value),)).fetchone()[0]
    )


def delete_company_permanent(conn, *, actor: NorthStarUser, company_id: int) -> None:
    """Exceptional. Isolated only. Refuses when the dependency gate is not DELETE_ALLOWED."""
    assert_not_production_db(conn)
    require_capability(actor, CAP_ARCHIVE, conn=conn)
    gate = inspect_company_dependencies(conn, company_id)
    if gate["classification"] != DELETE_ALLOWED:
        raise StewardError(gate["classification"])
    conn.execute("DELETE FROM companies WHERE id = ?", (int(company_id),))


def create_location(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    address: str,
    city: str = "",
    state: str = "",
    zip_code: str = "",
    location_name: str = "",
    location_type: str = "plant",
) -> int:
    assert_not_production_db(conn)
    require_capability(actor, CAP_EDIT, conn=conn)
    ensure_data_steward_schema(conn)
    loc_id = create_company_location(
        conn,
        company_id=resolve_company_id(conn, int(company_id)),
        location_name=location_name or address,
        location_type=location_type,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
        source_system=source_type_for_actor(actor),
    )
    record_provenance(
        conn,
        entity_type=ENTITY_LOCATION,
        entity_id=loc_id,
        field="address",
        new_value=address,
        source_type=source_type_for_actor(actor),
        changed_by_user_id=int(actor.id),
        action="CREATE",
    )
    return loc_id


def amend_location(
    conn, *, actor: NorthStarUser, location_id: int, fields: dict[str, Any], expected_updated_at: str | None = None
) -> None:
    assert_not_production_db(conn)
    require_capability(actor, CAP_EDIT, conn=conn)
    stored = _load(conn, "company_locations", location_id)
    _refuse_stale(conn, "company_locations", location_id, expected_updated_at)
    allowed = {"address", "city", "state", "zip", "location_name", "phone"}
    now = _now()
    for key, value in fields.items():
        if key not in allowed:
            continue
        old = _blank(stored.get(key))
        new_val = _blank(value)
        if old == new_val:
            continue
        conn.execute(
            f"UPDATE company_locations SET {key} = ?, updated_at = ? WHERE id = ?",
            (new_val, now, int(location_id)),
        )
        record_provenance(
            conn,
            entity_type=ENTITY_LOCATION,
            entity_id=int(location_id),
            field=key,
            old_value=old,
            new_value=new_val,
            source_type=source_type_for_actor(actor),
            changed_by_user_id=int(actor.id),
            action="AMEND",
        )


def archive_location(conn, *, actor: NorthStarUser, location_id: int, reason: str = "") -> None:
    _archive_row(conn, "company_locations", location_id, actor, reason, ENTITY_LOCATION)


def restore_location(conn, *, actor: NorthStarUser, location_id: int) -> int:
    return _restore_row(conn, "company_locations", location_id, actor, ENTITY_LOCATION)


def find_contact_duplicates(
    conn, *, company_id: int, email: str = "", first_name: str = "", last_name: str = "", exclude_id: int | None = None
) -> list[dict[str, Any]]:
    email_n = _blank(email).lower()
    hits = []
    rows = conn.execute(
        "SELECT id, first_name, last_name, email, company_id FROM contacts WHERE company_id = ?",
        (int(company_id),),
    ).fetchall()
    for row in rows:
        if exclude_id and int(row["id"]) == int(exclude_id):
            continue
        if email_n and _blank(row["email"]).lower() == email_n:
            hits.append(dict(row))
            continue
        if (
            _blank(first_name)
            and _blank(last_name)
            and _blank(row["first_name"]).lower() == _blank(first_name).lower()
            and _blank(row["last_name"]).lower() == _blank(last_name).lower()
        ):
            hits.append(dict(row))
    return hits


def create_contact(
    conn,
    *,
    actor: NorthStarUser,
    company_id: int,
    first_name: str,
    last_name: str,
    email: str = "",
    phone: str = "",
    title: str = "",
    confirm_despite_match: bool = False,
) -> dict[str, Any]:
    assert_not_production_db(conn)
    require_capability(actor, CAP_EDIT, conn=conn)
    ensure_data_steward_schema(conn)
    company_id = resolve_company_id(conn, int(company_id))
    dups = find_contact_duplicates(
        conn, company_id=company_id, email=email, first_name=first_name, last_name=last_name
    )
    if dups and not confirm_despite_match:
        raise StewardError("duplicate_contact")
    phone_store, ext = store_phone_parts(phone, field="phone")
    now = _now()
    rn = conn.execute(
        "SELECT COALESCE(external_record_no,'') FROM companies WHERE id=?",
        (company_id,),
    ).fetchone()
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name, email, phone,
            phone_extension, title, source_row_index, created_at, last_updated_at, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            company_id,
            _blank(rn[0] if rn else ""),
            _blank(first_name),
            _blank(last_name),
            _blank(email),
            phone_store,
            ext or None,
            _blank(title),
            now,
            now,
            source_type_for_actor(actor),
        ),
    )
    contact_id = int(cur.lastrowid)
    for field, value in (
        ("first_name", first_name),
        ("last_name", last_name),
        ("email", email),
        ("phone", phone_store),
        ("title", title),
    ):
        if not _has_text(value):
            continue
        record_provenance(
            conn,
            entity_type=ENTITY_CONTACT,
            entity_id=contact_id,
            field=field,
            new_value=value,
            source_type=source_type_for_actor(actor),
            changed_by_user_id=int(actor.id),
            action="CREATE",
            changed_at=now,
            actor=actor,
        )
    return {"contact_id": contact_id, "duplicates": dups}


def amend_contact(
    conn,
    *,
    actor: NorthStarUser,
    contact_id: int,
    fields: dict[str, Any],
    expected_updated_at: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    assert_not_production_db(conn)
    require_capability(actor, CAP_EDIT, conn=conn)
    ensure_data_steward_schema(conn)
    contact_id = resolve_contact_id(conn, int(contact_id))
    stored = _load(conn, "contacts", contact_id)
    _refuse_stale(conn, "contacts", contact_id, expected_updated_at)
    src = source_type_for_actor(actor)
    now = _now()
    allowed = {
        "first_name": "first_name",
        "last_name": "last_name",
        "title": "title",
        "email": "email",
        "phone": "phone",
        "phone_extension": "phone_extension",
        "alt_phone": "alt_phone",
        "alt_phone_extension": "alt_phone_extension",
    }
    dest_company = fields.get("company_id")
    with _amend_transaction(conn):
        if dest_company:
            dest = resolve_company_id(conn, int(dest_company))
            dups = find_contact_duplicates(
                conn,
                company_id=dest,
                email=_blank(fields.get("email") or stored.get("email")),
                first_name=_blank(fields.get("first_name") or stored.get("first_name")),
                last_name=_blank(fields.get("last_name") or stored.get("last_name")),
                exclude_id=contact_id,
            )
            if dups:
                raise StewardError("duplicate_contact")
            conn.execute("UPDATE contacts SET company_id = ? WHERE id = ?", (dest, contact_id))
            record_provenance(
                conn,
                entity_type=ENTITY_CONTACT,
                entity_id=contact_id,
                field="company_id",
                old_value=str(stored.get("company_id")),
                new_value=str(dest),
                source_type=src,
                changed_by_user_id=int(actor.id),
                action="AMEND",
                reason=reason or "company_association",
                changed_at=now,
            )
        for key, col in allowed.items():
            if key not in fields:
                continue
            new_val = _blank(fields[key])
            if key in {"phone", "alt_phone"}:
                new_val, ext = store_phone_parts(new_val, field=key)
                ext_col = "phone_extension" if key == "phone" else "alt_phone_extension"
                old = _blank(stored.get(col))
                conn.execute(
                    f"UPDATE contacts SET {col} = ?, {ext_col} = ? WHERE id = ?",
                    (new_val, ext or None, contact_id),
                )
            else:
                old = _blank(stored.get(col))
                if old == new_val:
                    continue
                conn.execute(f"UPDATE contacts SET {col} = ? WHERE id = ?", (new_val, contact_id))
            record_provenance(
                conn,
                entity_type=ENTITY_CONTACT,
                entity_id=contact_id,
                field=key,
                old_value=old,
                new_value=new_val,
                source_type=src,
                changed_by_user_id=int(actor.id),
                action="AMEND",
                reason=reason,
                changed_at=now,
            )
        _stamp(conn, "contacts", contact_id, now)
    return {"contact_id": contact_id, "updated_at": now}


def archive_contact(conn, *, actor: NorthStarUser, contact_id: int, reason: str = "") -> None:
    _archive_row(conn, "contacts", resolve_contact_id(conn, int(contact_id)), actor, reason, ENTITY_CONTACT)


def restore_contact(conn, *, actor: NorthStarUser, contact_id: int) -> int:
    return _restore_row(conn, "contacts", int(contact_id), actor, ENTITY_CONTACT)


def merge_contacts_isolated(
    conn,
    *,
    actor: NorthStarUser,
    source_contact_id: int,
    survivor_contact_id: int,
    fields: dict[str, Any] | None = None,
    allow_cross_company: bool = False,
) -> dict[str, Any]:
    assert_not_production_db(conn)
    require_capability(actor, CAP_DUP_EXECUTE, conn=conn)
    from company_merge_execute import _merge_contact_into
    from company_merges import existing_user_id

    src = _load(conn, "contacts", source_contact_id)
    dst = _load(conn, "contacts", survivor_contact_id)
    if int(src["company_id"]) != int(dst["company_id"]) and not allow_cross_company:
        raise StewardError(
            "Cross-company contact merge is blocked pending explicit review."
        )
    with _amend_transaction(conn):
        result = _merge_contact_into(
            conn,
            int(source_contact_id),
            int(survivor_contact_id),
            source_company_id=int(src["company_id"]),
            survivor_company_id=int(dst["company_id"]),
            resolution={
                "reason": "ds1_contact_merge",
                "actor_user_id": existing_user_id(conn, actor.id),
            },
            fields=fields or {},
        )
        record_provenance(
            conn,
            entity_type=ENTITY_CONTACT,
            entity_id=int(survivor_contact_id),
            field="merged_from",
            old_value=str(source_contact_id),
            new_value=str(survivor_contact_id),
            source_type=SOURCE_MERGE,
            changed_by_user_id=int(actor.id),
            action="MERGE",
            trusted=True,
        )
        return result


def link_relationship(
    conn,
    *,
    actor: NorthStarUser,
    client_id: int,
    company_id: int,
    status: str = "New",
) -> int:
    assert_not_production_db(conn)
    require_capability(actor, CAP_CCR_EDIT, client_id=client_id, conn=conn)
    company_id = resolve_company_id(conn, int(company_id))
    existing = conn.execute(
        "SELECT id FROM client_company_relationships WHERE client_id=? AND company_id=?",
        (int(client_id), company_id),
    ).fetchone()
    if existing is not None:
        ccr_id = int(existing["id"])
        if is_archived(conn, "client_company_relationships", ccr_id):
            return restore_relationship(conn, actor=actor, ccr_id=ccr_id)
        return ccr_id
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (int(client_id), company_id, _blank(status) or "New", now, now),
    )
    ccr_id = int(cur.lastrowid)
    record_provenance(
        conn,
        entity_type=ENTITY_CLIENT_RELATIONSHIP,
        entity_id=ccr_id,
        field="status",
        new_value=_blank(status) or "New",
        source_type=source_type_for_actor(actor),
        changed_by_user_id=int(actor.id),
        action="CREATE",
        source_ref=source_ref_manual(),
        client_id=int(client_id),
        actor=actor,
    )
    record_provenance(
        conn,
        entity_type=ENTITY_CLIENT_RELATIONSHIP,
        entity_id=ccr_id,
        field="company_id",
        new_value=str(company_id),
        source_type=source_type_for_actor(actor),
        changed_by_user_id=int(actor.id),
        action="CREATE",
        source_ref=source_ref_manual(),
        client_id=int(client_id),
        actor=actor,
    )
    return ccr_id


def remove_relationship(conn, *, actor: NorthStarUser, ccr_id: int, reason: str = "") -> dict[str, Any]:
    """REMOVE FROM CLIENT — archive the CCR. Never DELETE the row. Never delete the company."""
    _prepare_ccr_lifecycle(conn)
    stored = _load(conn, "client_company_relationships", ccr_id)
    require_capability(actor, CAP_CCR_EDIT, client_id=int(stored["client_id"]), conn=conn)
    inspect_relationship_dependencies(conn, int(ccr_id))
    company_id = int(stored["company_id"])
    already = is_archived(conn, "client_company_relationships", int(ccr_id))
    if already:
        return {
            "company_still_exists": True,
            "company_id": company_id,
            "ccr_id": int(ccr_id),
            "archived": True,
            "same_id": True,
            "noop": True,
            "code": "already_removed",
        }
    now = _now()
    reason_text = _blank(reason) or "remove_from_client"
    conn.execute(
        """
        UPDATE client_company_relationships
        SET archived_at = ?, archived_by_user_id = ?, archive_reason = ?
        WHERE id = ?
        """,
        (now, int(actor.id), reason_text, int(ccr_id)),
    )
    record_provenance(
        conn,
        entity_type=ENTITY_CLIENT_RELATIONSHIP,
        entity_id=int(ccr_id),
        field="archived_at",
        old_value="",
        new_value=now,
        source_type=source_type_for_actor(actor),
        changed_by_user_id=int(actor.id),
        action=ACTION_REMOVE_FROM_CLIENT,
        reason=reason_text,
        changed_at=now,
        client_id=int(stored["client_id"]),
        actor=actor,
    )
    remaining = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE id = ?", (company_id,)
    ).fetchone()[0]
    still = conn.execute(
        "SELECT id, archived_at FROM client_company_relationships WHERE id = ?",
        (int(ccr_id),),
    ).fetchone()
    return {
        "company_still_exists": int(remaining) == 1,
        "company_id": company_id,
        "ccr_id": int(ccr_id),
        "archived": bool(still and _blank(still["archived_at"])),
        "same_id": int(still["id"]) == int(ccr_id) if still else False,
        "noop": False,
        "code": "removed",
    }


def restore_relationship(conn, *, actor: NorthStarUser, ccr_id: int, reason: str = "") -> int:
    """Restore the same CCR id. Does not create a new relationship."""
    _prepare_ccr_lifecycle(conn)
    stored = _load(conn, "client_company_relationships", ccr_id)
    require_capability(actor, CAP_CCR_EDIT, client_id=int(stored["client_id"]), conn=conn)
    if not is_archived(conn, "client_company_relationships", int(ccr_id)):
        return int(ccr_id)
    now = _now()
    old_archived = _blank(stored.get("archived_at"))
    conn.execute(
        """
        UPDATE client_company_relationships
        SET archived_at = '', archived_by_user_id = NULL, archive_reason = ''
        WHERE id = ?
        """,
        (int(ccr_id),),
    )
    record_provenance(
        conn,
        entity_type=ENTITY_CLIENT_RELATIONSHIP,
        entity_id=int(ccr_id),
        field="archived_at",
        old_value=old_archived or "archived",
        new_value="",
        source_type=source_type_for_actor(actor),
        changed_by_user_id=int(actor.id),
        action=ACTION_RESTORE_TO_CLIENT,
        reason=_blank(reason) or "restore_to_client",
        changed_at=now,
        client_id=int(stored["client_id"]),
        actor=actor,
    )
    return int(ccr_id)


def amend_relationship(
    conn,
    *,
    actor: NorthStarUser,
    ccr_id: int,
    fields: dict[str, Any],
    expected_updated_at: str | None = None,
) -> None:
    assert_not_production_db(conn)
    stored = _load(conn, "client_company_relationships", ccr_id)
    require_capability(actor, CAP_CCR_EDIT, client_id=int(stored["client_id"]), conn=conn)
    _refuse_stale(conn, "client_company_relationships", ccr_id, expected_updated_at)
    allowed = {"status", "assigned_user_id", "is_hot", "next_action", "follow_up_date", "notes"}
    now = _now()
    src = source_type_for_actor(actor)
    with _amend_transaction(conn):
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "assigned_user_id":
                from leadmaster_refresh_apply import _user_may_be_assigned

                uid = int(value)
                if not _user_may_be_assigned(conn, uid, int(stored["client_id"])):
                    raise StewardError("assignment_user_lacks_client_access")
                old = stored.get("assigned_user_id")
                conn.execute(
                    "UPDATE client_company_relationships SET assigned_user_id=?, updated_at=? WHERE id=?",
                    (uid, now, int(ccr_id)),
                )
                record_provenance(
                    conn,
                    entity_type=ENTITY_CLIENT_RELATIONSHIP,
                    entity_id=int(ccr_id),
                    field="assigned_user_id",
                    old_value=str(old or ""),
                    new_value=str(uid),
                    source_type=src,
                    changed_by_user_id=int(actor.id),
                    action="AMEND",
                    client_id=int(stored["client_id"]),
                )
                continue
            if key == "notes":
                append_note(
                    conn,
                    actor=actor,
                    ccr_id=int(ccr_id),
                    text=str(value),
                )
                continue
            old = _blank(stored.get(key))
            new_val = _blank(value) if key != "is_hot" else int(value or 0)
            conn.execute(
                f"UPDATE client_company_relationships SET {key} = ?, updated_at = ? WHERE id = ?",
                (new_val, now, int(ccr_id)),
            )
            record_provenance(
                conn,
                entity_type=ENTITY_CLIENT_RELATIONSHIP,
                entity_id=int(ccr_id),
                field=key,
                old_value=str(old),
                new_value=str(new_val),
                source_type=src,
                changed_by_user_id=int(actor.id),
                action="AMEND",
                client_id=int(stored["client_id"]),
            )


def append_note(conn, *, actor: NorthStarUser, ccr_id: int, text: str) -> None:
    """Append-only. Exact duplicate adds are allowed; do not fuzzy-dedupe."""
    assert_not_production_db(conn)
    stored = _load(conn, "client_company_relationships", ccr_id)
    require_capability(actor, CAP_CCR_EDIT, client_id=int(stored["client_id"]), conn=conn)
    incoming = _blank(text)
    if not incoming:
        raise StewardError("Note text is required.")
    existing = _blank(stored.get("notes"))
    merged = f"{existing}\n{incoming}".strip() if existing else incoming
    now = _now()
    note_hash = hashlib.sha256(incoming.encode("utf-8")).hexdigest()[:16]
    with _amend_transaction(conn):
        conn.execute(
            "UPDATE client_company_relationships SET notes = ?, updated_at = ? WHERE id = ?",
            (merged, now, int(ccr_id)),
        )
        record_provenance(
            conn,
            entity_type=ENTITY_CLIENT_RELATIONSHIP,
            entity_id=int(ccr_id),
            field="notes",
            old_value="",
            new_value=incoming,
            source_type=source_type_for_actor(actor),
            changed_by_user_id=int(actor.id),
            action="NOTE",
            reason=f"note_hash:{note_hash}",
            source_ref=source_ref_manual(),
            client_id=int(stored["client_id"]),
            changed_at=now,
            actor=actor,
        )


def attach_leadmaster_identity(
    conn, *, actor: NorthStarUser, company_id: int, record_no: str, client_id: int | None = None
) -> None:
    assert_not_production_db(conn)
    require_capability(actor, CAP_EDIT, conn=conn)
    from company_locations import upsert_company_source_identity
    from leadmaster_refresh_plan import SOURCE_SYSTEM

    company_id = resolve_company_id(conn, int(company_id))
    from company_merges import existing_user_id

    upsert_company_source_identity(
        conn,
        company_id=company_id,
        source_system=SOURCE_SYSTEM,
        source_record_no=_blank(record_no),
        client_id=client_id,
        source_company_name=_load(conn, "companies", company_id).get("company_name") or "",
        created_by_user_id=existing_user_id(conn, actor.id),
    )
    record_provenance(
        conn,
        entity_type=ENTITY_COMPANY,
        entity_id=company_id,
        field="source_identity",
        new_value=_blank(record_no),
        source_type=SOURCE_LEADMASTER,
        changed_by_user_id=int(actor.id),
        action="ATTACH_IDENTITY",
        client_id=client_id,
        trusted=True,
    )


def audit_history(
    conn, *, entity_type: str, entity_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    if not provenance_table_ready(conn):
        return []
    rows = conn.execute(
        """
        SELECT * FROM field_provenance_events
        WHERE entity_type = ? AND entity_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (entity_type, int(entity_id), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def live_company_amend_enabled() -> bool:
    """DS-7 governed Master Company amend only. Does not enable archive/delete/merge."""
    return True


def live_ccr_lifecycle_enabled() -> bool:
    """DS-8 governed CCR remove/restore only. Does not enable master archive/delete/merge."""
    return True


def live_destructive_enabled() -> bool:
    return False


def _is_live_db_path() -> bool:
    from db import DB_PATH

    return Path(os.fspath(DB_PATH)).resolve() == PRODUCTION_DB_PATH.resolve()


def _assert_company_amend_allowed(conn=None) -> None:
    """Allow isolated writes always; live writes only for governed company amend."""
    _ = conn
    if not _is_live_db_path():
        return
    if not live_company_amend_enabled():
        raise StewardLiveWriteError(LIVE_STEWARD_WRITES_DISABLED)


def _prepare_company_amend(conn) -> None:
    _assert_company_amend_allowed(conn)
    if _is_live_db_path():
        if not provenance_table_ready(conn):
            raise StewardError("provenance_schema_missing")
        return
    ensure_data_steward_schema(conn)


def _assert_ccr_lifecycle_allowed(conn=None) -> None:
    """Allow isolated writes always; live writes only for governed CCR remove/restore."""
    _ = conn
    if not _is_live_db_path():
        return
    if not live_ccr_lifecycle_enabled():
        raise StewardLiveWriteError(LIVE_STEWARD_WRITES_DISABLED)


def _prepare_ccr_lifecycle(conn) -> None:
    _assert_ccr_lifecycle_allowed(conn)
    if _is_live_db_path():
        if not provenance_table_ready(conn):
            raise StewardError("provenance_schema_missing")
        if "archived_at" not in _cols(conn, "client_company_relationships"):
            raise StewardError("ccr_archive_schema_missing")
        return
    ensure_data_steward_schema(conn)
