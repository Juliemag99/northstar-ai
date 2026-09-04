"""Per-row CRM import status resolutions for previewed batches.

Persists administrator choices that clear invalid_status / status_conflict
review gates. Never stores notes or other PII beyond the canonical status label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from crm_import_status_notes import (
    STATUS_CONFLICT,
    STATUS_INVALID,
    STATUS_PRESERVE,
    STATUS_USE_IMPORTED,
    StatusCatalog,
    blank,
    load_client_status_catalog,
    normalize_status_key,
)
from models import NorthStarUser

RESOLUTION_KEEP_EXISTING = "keep_existing_status"
RESOLUTION_REPLACE = "replace_with_status"
VALID_RESOLUTION_TYPES = frozenset({RESOLUTION_KEEP_EXISTING, RESOLUTION_REPLACE})

BATCH_NOT_REUSABLE = "This import batch can no longer be resolved."
ROW_NOT_FOUND = "Import row not found."
STATUS_REQUIRED = "Choose a valid client status."
STATUS_UNKNOWN = "That status is not in this client's status catalog."
KEEP_NOT_ALLOWED = "Keep existing status is only available for status conflicts."
RESOLUTION_TYPE_INVALID = "Unsupported status resolution type."


@dataclass(frozen=True, slots=True)
class StatusResolution:
    staged_row_id: int
    resolution_type: str
    resolved_status: str
    updated_at: str
    updated_by_user_id: int | None


def _iso(now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_crm_import_status_resolution_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crm_import_row_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL,
            staged_row_id INTEGER NOT NULL,
            resolution_type TEXT NOT NULL,
            resolved_status TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by_user_id INTEGER,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
            FOREIGN KEY (batch_id) REFERENCES crm_import_batches(id) ON DELETE CASCADE,
            FOREIGN KEY (staged_row_id) REFERENCES crm_import_rows(id) ON DELETE CASCADE,
            FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE (client_id, batch_id, staged_row_id)
        );

        CREATE INDEX IF NOT EXISTS idx_crm_import_row_resolutions_batch
            ON crm_import_row_resolutions(batch_id, client_id);
        """
    )
    results_existing = {
        str(r["name"]) for r in conn.execute("PRAGMA table_info(crm_import_results)").fetchall()
    }
    for name, declaration in (
        ("original_status_action", "TEXT NOT NULL DEFAULT ''"),
        ("status_resolution_action", "TEXT NOT NULL DEFAULT ''"),
        ("final_status", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in results_existing:
            conn.execute(f"ALTER TABLE crm_import_results ADD COLUMN {name} {declaration}")


def clear_batch_status_resolutions(conn, client_id: int, batch_id: int) -> int:
    cur = conn.execute(
        """
        DELETE FROM crm_import_row_resolutions
        WHERE client_id = ? AND batch_id = ?
        """,
        (int(client_id), int(batch_id)),
    )
    return int(cur.rowcount or 0)


def load_batch_status_resolutions(
    conn, client_id: int, batch_id: int
) -> dict[int, StatusResolution]:
    rows = conn.execute(
        """
        SELECT staged_row_id, resolution_type, resolved_status,
               updated_at, updated_by_user_id
        FROM crm_import_row_resolutions
        WHERE client_id = ? AND batch_id = ?
        """,
        (int(client_id), int(batch_id)),
    ).fetchall()
    out: dict[int, StatusResolution] = {}
    for row in rows:
        rid = int(row["staged_row_id"])
        out[rid] = StatusResolution(
            staged_row_id=rid,
            resolution_type=blank(row["resolution_type"]),
            resolved_status=blank(row["resolved_status"]),
            updated_at=blank(row["updated_at"]),
            updated_by_user_id=(
                int(row["updated_by_user_id"])
                if row["updated_by_user_id"] is not None
                else None
            ),
        )
    return out


def apply_status_resolution(
    *,
    base_status_action: str,
    base_resolved_status: str,
    existing_status: str,
    resolution: StatusResolution | None,
    catalog: StatusCatalog,
) -> tuple[str, str, str, str]:
    """Return (status_action, resolved_status, original_action, resolution_type).

    Only persisted, compatible resolutions change the planned action.
    """
    original = blank(base_status_action) or "none"
    if resolution is None or original not in {STATUS_INVALID, STATUS_CONFLICT}:
        return original, blank(base_resolved_status), original, ""

    rtype = blank(resolution.resolution_type)
    if rtype == RESOLUTION_KEEP_EXISTING:
        if original != STATUS_CONFLICT:
            return original, blank(base_resolved_status), original, ""
        kept = blank(existing_status) or blank(base_resolved_status)
        return STATUS_PRESERVE, kept, original, RESOLUTION_KEEP_EXISTING

    if rtype == RESOLUTION_REPLACE:
        canonical, err = catalog.resolve(resolution.resolved_status)
        if err or not canonical:
            return original, blank(base_resolved_status), original, ""
        return STATUS_USE_IMPORTED, canonical, original, RESOLUTION_REPLACE

    return original, blank(base_resolved_status), original, ""


def save_crm_import_status_resolution(
    client_id: int,
    batch_id: int,
    staged_row_id: int,
    *,
    actor: NorthStarUser,
    resolution_type: str | None,
    resolved_status: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Save or clear one row resolution. Previewed batches only."""
    from crm_import_staging import (
        BatchNotReusable,
        STATUS_PREVIEWED,
        _blank,
        _load_batch_row,
        ensure_crm_import_schema,
        purge_expired_staging_rows,
    )
    from db import get_connection

    if actor is None or not bool(actor.active) or not bool(actor.is_administrator):
        raise PermissionError("Not authorized.")

    with get_connection() as conn:
        ensure_crm_import_schema(conn)
        ensure_crm_import_status_resolution_schema(conn)
        purge_expired_staging_rows(conn)

        batch = _load_batch_row(conn, client_id, batch_id)
        if batch is None:
            raise LookupError("Import batch not found.")
        if _blank(batch["status"]) != STATUS_PREVIEWED:
            raise BatchNotReusable(BATCH_NOT_REUSABLE)

        row = conn.execute(
            """
            SELECT id FROM crm_import_rows
            WHERE id = ? AND batch_id = ? AND client_id = ?
            """,
            (int(staged_row_id), int(batch_id), int(client_id)),
        ).fetchone()
        if row is None:
            raise LookupError(ROW_NOT_FOUND)

        if clear or resolution_type is None or blank(resolution_type) == "":
            conn.execute(
                """
                DELETE FROM crm_import_row_resolutions
                WHERE client_id = ? AND batch_id = ? AND staged_row_id = ?
                """,
                (int(client_id), int(batch_id), int(staged_row_id)),
            )
            return {
                "client_id": int(client_id),
                "batch_id": int(batch_id),
                "staged_row_id": int(staged_row_id),
                "cleared": True,
                "resolution": None,
            }

        rtype = blank(resolution_type)
        if rtype not in VALID_RESOLUTION_TYPES:
            raise ValueError(RESOLUTION_TYPE_INVALID)

        catalog = load_client_status_catalog(conn, client_id)
        stored_status = ""
        selected_status = blank(resolved_status)
        if rtype == RESOLUTION_REPLACE:
            if selected_status:
                canonical, err = catalog.resolve(selected_status)
                if err or not canonical:
                    raise ValueError(STATUS_UNKNOWN)
                stored_status = canonical
            else:
                raise ValueError(STATUS_REQUIRED)
        elif rtype == RESOLUTION_KEEP_EXISTING:
            stored_status = ""
        else:
            raise ValueError(RESOLUTION_TYPE_INVALID)

        # Drop this row's prior resolution so the planner reports the base need.
        conn.execute(
            """
            DELETE FROM crm_import_row_resolutions
            WHERE client_id = ? AND batch_id = ? AND staged_row_id = ?
            """,
            (int(client_id), int(batch_id), int(staged_row_id)),
        )
        from crm_import_plan import plan_crm_import_batch

        plan = plan_crm_import_batch(conn, client_id=client_id, batch_id=batch_id)
        target = next((r for r in plan.rows if int(r.row_id) == int(staged_row_id)), None)
        if target is None:
            raise LookupError(ROW_NOT_FOUND)
        base_action = blank(target.status_action)
        if base_action not in {STATUS_INVALID, STATUS_CONFLICT}:
            raise ValueError("This row does not need status resolution.")
        if rtype == RESOLUTION_KEEP_EXISTING and base_action != STATUS_CONFLICT:
            raise ValueError(KEEP_NOT_ALLOWED)

        now = _iso()
        conn.execute(
            """
            INSERT INTO crm_import_row_resolutions (
                client_id, batch_id, staged_row_id,
                resolution_type, resolved_status,
                updated_at, updated_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(client_id),
                int(batch_id),
                int(staged_row_id),
                rtype,
                stored_status,
                now,
                int(actor.id),
            ),
        )
        return {
            "client_id": int(client_id),
            "batch_id": int(batch_id),
            "staged_row_id": int(staged_row_id),
            "cleared": False,
            "resolution": {
                "resolution_type": rtype,
                "resolved_status": stored_status,
                "updated_at": now,
                "updated_by_user_id": int(actor.id),
            },
        }


def catalog_labels(catalog: StatusCatalog) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for group in catalog.by_key.values():
        for label in group:
            key = normalize_status_key(label)
            if key in seen:
                continue
            seen.add(key)
            labels.append(label)
    labels.sort(key=lambda s: s.casefold())
    return labels
