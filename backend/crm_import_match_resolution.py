"""Per-row CRM import match resolutions for previewed batches.

Persists administrator choices that clear possible_company_match /
possible_contact_match / skip / company-only review gates.
Does not merge or delete master companies or contacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

RESOLUTION_USE_EXISTING_COMPANY = "use_existing_company"
RESOLUTION_USE_EXISTING_CONTACT = "use_existing_contact"
RESOLUTION_CREATE_COMPANY = "create_company"
RESOLUTION_CREATE_CONTACT = "create_contact"
RESOLUTION_USE_PROPOSED_COMPANY = "use_proposed_company"
RESOLUTION_SKIP_ROW = "skip_row"
RESOLUTION_IMPORT_COMPANY_ONLY = "import_company_only"

VALID_MATCH_RESOLUTION_TYPES = frozenset(
    {
        RESOLUTION_USE_EXISTING_COMPANY,
        RESOLUTION_USE_EXISTING_CONTACT,
        RESOLUTION_CREATE_COMPANY,
        RESOLUTION_CREATE_CONTACT,
        RESOLUTION_USE_PROPOSED_COMPANY,
        RESOLUTION_SKIP_ROW,
        RESOLUTION_IMPORT_COMPANY_ONLY,
    }
)


def blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iso(now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class MatchResolution:
    staged_row_id: int
    resolution_type: str
    company_id: int | None
    contact_id: int | None
    company_proposed_key: str
    updated_at: str
    updated_by_user_id: int | None


def ensure_crm_import_match_resolution_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crm_import_match_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL,
            staged_row_id INTEGER NOT NULL,
            resolution_type TEXT NOT NULL,
            company_id INTEGER,
            contact_id INTEGER,
            company_proposed_key TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by_user_id INTEGER,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
            FOREIGN KEY (batch_id) REFERENCES crm_import_batches(id) ON DELETE CASCADE,
            FOREIGN KEY (staged_row_id) REFERENCES crm_import_rows(id) ON DELETE CASCADE,
            FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE (client_id, batch_id, staged_row_id)
        );

        CREATE INDEX IF NOT EXISTS idx_crm_import_match_resolutions_batch
            ON crm_import_match_resolutions(batch_id, client_id);
        """
    )


def clear_batch_match_resolutions(conn, client_id: int, batch_id: int) -> int:
    cur = conn.execute(
        """
        DELETE FROM crm_import_match_resolutions
        WHERE client_id = ? AND batch_id = ?
        """,
        (int(client_id), int(batch_id)),
    )
    return int(cur.rowcount or 0)


def load_batch_match_resolutions(
    conn, client_id: int, batch_id: int
) -> dict[int, MatchResolution]:
    rows = conn.execute(
        """
        SELECT staged_row_id, resolution_type, company_id, contact_id,
               company_proposed_key, updated_at, updated_by_user_id
        FROM crm_import_match_resolutions
        WHERE client_id = ? AND batch_id = ?
        """,
        (int(client_id), int(batch_id)),
    ).fetchall()
    out: dict[int, MatchResolution] = {}
    for row in rows:
        rid = int(row["staged_row_id"])
        out[rid] = MatchResolution(
            staged_row_id=rid,
            resolution_type=blank(row["resolution_type"]),
            company_id=(
                int(row["company_id"]) if row["company_id"] is not None else None
            ),
            contact_id=(
                int(row["contact_id"]) if row["contact_id"] is not None else None
            ),
            company_proposed_key=blank(row["company_proposed_key"]),
            updated_at=blank(row["updated_at"]),
            updated_by_user_id=(
                int(row["updated_by_user_id"])
                if row["updated_by_user_id"] is not None
                else None
            ),
        )
    return out


def save_crm_import_match_resolution(
    conn,
    *,
    client_id: int,
    batch_id: int,
    staged_row_id: int,
    resolution_type: str,
    company_id: int | None = None,
    contact_id: int | None = None,
    company_proposed_key: str = "",
    updated_by_user_id: int | None = None,
    now: datetime | None = None,
) -> MatchResolution:
    rtype = blank(resolution_type)
    if rtype not in VALID_MATCH_RESOLUTION_TYPES:
        raise ValueError(f"Unsupported match resolution type: {rtype}")
    if rtype == RESOLUTION_USE_EXISTING_COMPANY and company_id is None:
        raise ValueError("company_id is required for use_existing_company.")
    if rtype == RESOLUTION_USE_EXISTING_CONTACT and contact_id is None:
        raise ValueError("contact_id is required for use_existing_contact.")
    if rtype == RESOLUTION_USE_PROPOSED_COMPANY and not blank(company_proposed_key):
        raise ValueError("company_proposed_key is required for use_proposed_company.")

    stamp = _iso(now)
    conn.execute(
        """
        INSERT INTO crm_import_match_resolutions (
            client_id, batch_id, staged_row_id, resolution_type,
            company_id, contact_id, company_proposed_key,
            updated_at, updated_by_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_id, batch_id, staged_row_id) DO UPDATE SET
            resolution_type = excluded.resolution_type,
            company_id = excluded.company_id,
            contact_id = excluded.contact_id,
            company_proposed_key = excluded.company_proposed_key,
            updated_at = excluded.updated_at,
            updated_by_user_id = excluded.updated_by_user_id
        """,
        (
            int(client_id),
            int(batch_id),
            int(staged_row_id),
            rtype,
            int(company_id) if company_id is not None else None,
            int(contact_id) if contact_id is not None else None,
            blank(company_proposed_key),
            stamp,
            int(updated_by_user_id) if updated_by_user_id is not None else None,
        ),
    )
    return MatchResolution(
        staged_row_id=int(staged_row_id),
        resolution_type=rtype,
        company_id=int(company_id) if company_id is not None else None,
        contact_id=int(contact_id) if contact_id is not None else None,
        company_proposed_key=blank(company_proposed_key),
        updated_at=stamp,
        updated_by_user_id=(
            int(updated_by_user_id) if updated_by_user_id is not None else None
        ),
    )


def match_resolution_fingerprint_slice(resolution: MatchResolution | None) -> dict[str, Any]:
    if resolution is None:
        return {
            "match_resolution_type": "",
            "match_resolution_company_id": None,
            "match_resolution_contact_id": None,
            "match_resolution_company_proposed_key": "",
            "match_resolution_updated_at": "",
            "match_resolution_updated_by_user_id": None,
        }
    return {
        "match_resolution_type": resolution.resolution_type,
        "match_resolution_company_id": resolution.company_id,
        "match_resolution_contact_id": resolution.contact_id,
        "match_resolution_company_proposed_key": resolution.company_proposed_key,
        "match_resolution_updated_at": resolution.updated_at,
        "match_resolution_updated_by_user_id": resolution.updated_by_user_id,
    }


def save_crm_import_match_resolution_http(
    client_id: int,
    batch_id: int,
    staged_row_id: int,
    *,
    actor,
    resolution_type: str | None = None,
    company_id: int | None = None,
    contact_id: int | None = None,
    company_proposed_key: str = "",
    clear: bool = False,
) -> dict[str, Any]:
    if actor is None or not bool(actor.active) or not bool(actor.is_administrator):
        raise PermissionError("Not authorized.")
    from crm_import_staging import (
        BATCH_NOT_REUSABLE,
        BatchNotReusable,
        USABLE_STATUS,
        _blank,
        _load_batch_row,
        purge_expired_staging_rows,
    )
    from db import get_connection

    with get_connection() as conn:
        purge_expired_staging_rows(conn)
        ensure_crm_import_match_resolution_schema(conn)
        row = _load_batch_row(conn, client_id, batch_id)
        if row is None:
            raise LookupError("Import batch not found.")
        if _blank(row["status"]) != USABLE_STATUS:
            raise BatchNotReusable(BATCH_NOT_REUSABLE)
        staged = conn.execute(
            """
            SELECT id FROM crm_import_rows
            WHERE id = ? AND batch_id = ? AND client_id = ?
            """,
            (int(staged_row_id), int(batch_id), int(client_id)),
        ).fetchone()
        if staged is None:
            raise LookupError("Import row not found.")
        if clear or not blank(resolution_type):
            conn.execute(
                """
                DELETE FROM crm_import_match_resolutions
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
        resolution = save_crm_import_match_resolution(
            conn,
            client_id=int(client_id),
            batch_id=int(batch_id),
            staged_row_id=int(staged_row_id),
            resolution_type=str(resolution_type or ""),
            company_id=company_id,
            contact_id=contact_id,
            company_proposed_key=company_proposed_key,
            updated_by_user_id=int(actor.id),
        )
    return {
        "client_id": int(client_id),
        "batch_id": int(batch_id),
        "staged_row_id": resolution.staged_row_id,
        "cleared": False,
        "resolution": {
            "resolution_type": resolution.resolution_type,
            "company_id": resolution.company_id,
            "contact_id": resolution.contact_id,
            "company_proposed_key": resolution.company_proposed_key,
            "updated_at": resolution.updated_at,
            "updated_by_user_id": resolution.updated_by_user_id,
        },
    }
