"""Durable company alias / source identity preservation.

Stores original source company names and provenance so later normalization
does not lose incoming names, record numbers, addresses, or phones.

Does not rewrite companies.company_name. Does not backfill historical aliases.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from crm_identity_keys import normalize_record_no
from crm_import_state import state_for_match

SOURCE_CRM_IMPORT = "crm_import"
SOURCE_CLIENT_DATA_IMPORT = "client_data_import"
SOURCE_MANUAL = "manual"
SOURCE_AI = "ai"
SOURCE_LEADMASTER = "LEADMASTER"

_IMPORT_SOURCES = frozenset({SOURCE_CRM_IMPORT, SOURCE_CLIENT_DATA_IMPORT, SOURCE_AI})


def _existing_user_id(conn: sqlite3.Connection, user_id: object) -> int | None:
    if user_id is None or str(user_id).strip() == "":
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    if uid <= 0 or not _table_exists(conn, "users"):
        return None
    row = conn.execute("SELECT id FROM users WHERE id = ? LIMIT 1", (uid,)).fetchone()
    return uid if row is not None else None


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def aliases_table_ready(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "company_aliases")


def ensure_company_alias_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent CREATE TABLE / INDEX only. Does not rewrite company rows."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            alias_name TEXT NOT NULL,
            alias_norm TEXT NOT NULL,
            source_system TEXT NOT NULL DEFAULT '',
            source_record_no TEXT NOT NULL DEFAULT '',
            client_id INTEGER,
            source_batch_id INTEGER,
            source_row INTEGER,
            source_address TEXT NOT NULL DEFAULT '',
            source_city TEXT NOT NULL DEFAULT '',
            source_state TEXT NOT NULL DEFAULT '',
            source_zip TEXT NOT NULL DEFAULT '',
            source_phone TEXT NOT NULL DEFAULT '',
            source_website TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_by_user_id INTEGER,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL,
            FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_company_aliases_idempotent
            ON company_aliases (
                company_id,
                COALESCE(client_id, 0),
                source_system,
                source_record_no,
                alias_norm
            )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_aliases_company
            ON company_aliases(company_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_aliases_record_no
            ON company_aliases(source_record_no)
            WHERE source_record_no != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_aliases_norm
            ON company_aliases(alias_norm)
            WHERE alias_norm != ''
        """
    )
    n_row = conn.execute("SELECT COUNT(*) FROM company_aliases").fetchone()
    n = int(n_row[0] if n_row is not None else 0)
    return {"alias_rows": n}


def _alias_norm(alias_name: str) -> str:
    from import_brown_industries import norm_name

    return norm_name(alias_name) if alias_name else ""


def should_store_alias(
    *,
    source_system: str,
    alias_name: str,
    canonical_name: str = "",
    source_record_no: str = "",
    master_record_no: str = "",
) -> bool:
    """Skip useless same-name/same-RN duplicates unless source provenance matters."""
    name = _blank(alias_name)
    if not name:
        return False
    system = _blank(source_system)
    if system == SOURCE_LEADMASTER:
        # Durable LEADMASTER source identity holds the RN. Alias only when the
        # incoming name is distinct from the canonical company name.
        return _alias_norm(name) != _alias_norm(_blank(canonical_name))
    if system in _IMPORT_SOURCES:
        return True
    if _alias_norm(name) != _alias_norm(_blank(canonical_name)):
        return True
    incoming_rn = normalize_record_no(source_record_no)
    master_rn = normalize_record_no(master_record_no)
    if incoming_rn and incoming_rn != master_rn:
        return True
    return False


def upsert_company_alias(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    alias_name: str,
    source_system: str,
    source_record_no: str = "",
    client_id: int | None = None,
    source_batch_id: int | None = None,
    source_row: int | None = None,
    source_address: str = "",
    source_city: str = "",
    source_state: str = "",
    source_zip: str = "",
    source_phone: str = "",
    source_website: str = "",
    created_by_user_id: int | None = None,
) -> tuple[int | None, bool]:
    """Insert or fill-blank an alias row. Idempotent. Does not rename the company.

    Returns (alias_id, created). None id when the alias name is blank.
    Assumes company_aliases already exists (no DDL).
    """
    name = _blank(alias_name)
    if not name:
        return None, False
    alias_norm = _alias_norm(name)
    if not alias_norm:
        return None, False
    system = _blank(source_system)
    rn = normalize_record_no(source_record_no)
    cid = int(client_id) if client_id is not None else None
    batch_id = int(source_batch_id) if source_batch_id is not None else None
    row_no = int(source_row) if source_row is not None else None
    user_id = _existing_user_id(conn, created_by_user_id)
    address = _blank(source_address)
    city = _blank(source_city)
    state = _blank(source_state)
    zip_ = _blank(source_zip)
    phone = _blank(source_phone)
    website = _blank(source_website)

    existing = conn.execute(
        """
        SELECT
            id, alias_name, source_batch_id, source_row,
            source_address, source_city, source_state, source_zip,
            source_phone, source_website
        FROM company_aliases
        WHERE company_id = ?
          AND COALESCE(client_id, 0) = COALESCE(?, 0)
          AND source_system = ?
          AND source_record_no = ?
          AND alias_norm = ?
        LIMIT 1
        """,
        (int(company_id), cid, system, rn, alias_norm),
    ).fetchone()
    if existing is not None:
        alias_id = int(existing["id"])
        updates: list[str] = []
        params: list[Any] = []

        def _fill(column: str, current: object, incoming: str) -> None:
            if incoming and not _blank(current):
                updates.append(f"{column} = ?")
                params.append(incoming)

        _fill("source_address", existing["source_address"], address)
        _fill("source_city", existing["source_city"], city)
        _fill("source_state", existing["source_state"], state)
        _fill("source_zip", existing["source_zip"], zip_)
        _fill("source_phone", existing["source_phone"], phone)
        _fill("source_website", existing["source_website"], website)
        if batch_id is not None and existing["source_batch_id"] is None:
            updates.append("source_batch_id = ?")
            params.append(batch_id)
        if row_no is not None and existing["source_row"] is None:
            updates.append("source_row = ?")
            params.append(row_no)
        if updates:
            params.append(alias_id)
            conn.execute(
                f"UPDATE company_aliases SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        return alias_id, False

    try:
        cur = conn.execute(
            """
            INSERT INTO company_aliases (
                company_id, alias_name, alias_norm, source_system, source_record_no,
                client_id, source_batch_id, source_row,
                source_address, source_city, source_state, source_zip,
                source_phone, source_website, created_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(company_id),
                name,
                alias_norm,
                system,
                rn,
                cid,
                batch_id,
                row_no,
                address,
                city,
                state,
                zip_,
                phone,
                website,
                user_id,
            ),
        )
        return int(cur.lastrowid), True
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            SELECT id FROM company_aliases
            WHERE company_id = ?
              AND COALESCE(client_id, 0) = COALESCE(?, 0)
              AND source_system = ?
              AND source_record_no = ?
              AND alias_norm = ?
            LIMIT 1
            """,
            (int(company_id), cid, system, rn, alias_norm),
        ).fetchone()
        if row is None:
            raise
        return int(row["id"]), False


def capture_import_company_alias(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    mapped: dict[str, Any] | None,
    source_system: str,
    client_id: int,
    source_batch_id: int | None = None,
    source_row: int | None = None,
    created_by_user_id: int | None = None,
) -> tuple[int | None, bool]:
    mapped = mapped or {}
    alias_name = _blank(mapped.get("company_name"))
    canonical_name = ""
    master_rn = ""
    row = conn.execute(
        "SELECT company_name, external_record_no FROM companies WHERE id = ? LIMIT 1",
        (int(company_id),),
    ).fetchone()
    if row is not None:
        canonical_name = _blank(row["company_name"])
        master_rn = _blank(row["external_record_no"])
    if not should_store_alias(
        source_system=source_system,
        alias_name=alias_name,
        canonical_name=canonical_name,
        source_record_no=_blank(mapped.get("external_record_no")),
        master_record_no=master_rn,
    ):
        return None, False
    return upsert_company_alias(
        conn,
        company_id=int(company_id),
        alias_name=_blank(mapped.get("company_name")),
        source_system=source_system,
        source_record_no=_blank(mapped.get("external_record_no")),
        client_id=int(client_id),
        source_batch_id=source_batch_id,
        source_row=source_row,
        source_address=_blank(mapped.get("address")),
        source_city=_blank(mapped.get("city")),
        source_state=_blank(mapped.get("state")),
        source_zip=_blank(mapped.get("zip")),
        source_phone=_blank(mapped.get("phone")),
        source_website=_blank(mapped.get("website")),
        created_by_user_id=created_by_user_id,
    )


def collect_alias_company_ids(
    conn: sqlite3.Connection,
    *,
    record_nos: set[str],
    names: set[str],
    chunk_size: int = 400,
) -> set[int]:
    """Indexed alias probe. Never scans companies."""
    if not aliases_table_ready(conn):
        return set()
    found: set[int] = set()

    def _add(sql: str, values: list[str]) -> None:
        if not values:
            return
        for i in range(0, len(values), chunk_size):
            chunk = values[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            for raw in conn.execute(sql.format(placeholders=placeholders), chunk):
                found.add(int(raw[0] if not hasattr(raw, "keys") else raw["company_id"]))

    _add(
        "SELECT company_id FROM company_aliases "
        "WHERE source_record_no != '' AND source_record_no IN ({placeholders})",
        sorted(normalize_record_no(v) for v in record_nos if normalize_record_no(v)),
    )
    _add(
        "SELECT company_id FROM company_aliases "
        "WHERE alias_norm != '' AND alias_norm IN ({placeholders})",
        sorted(n for n in names if n),
    )
    return found


def load_aliases_by_company(
    conn: sqlite3.Connection,
    company_ids: list[int],
    *,
    chunk_size: int = 400,
) -> dict[int, list[dict[str, str]]]:
    out: dict[int, list[dict[str, str]]] = {int(cid): [] for cid in company_ids}
    if not company_ids or not aliases_table_ready(conn):
        return out
    ids = sorted({int(cid) for cid in company_ids})
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        for raw in conn.execute(
            f"""
            SELECT
                company_id, alias_name, alias_norm, source_system, source_record_no,
                source_address, source_city, source_state, source_zip,
                source_phone, source_website, client_id
            FROM company_aliases
            WHERE company_id IN ({placeholders})
            ORDER BY id
            """,
            chunk,
        ):
            cid = int(raw["company_id"])
            out.setdefault(cid, []).append(
                {
                    "alias_name": _blank(raw["alias_name"]),
                    "alias_norm": _blank(raw["alias_norm"]),
                    "source_system": _blank(raw["source_system"]),
                    "source_record_no": normalize_record_no(raw["source_record_no"]),
                    "source_address": _blank(raw["source_address"]),
                    "source_city": _blank(raw["source_city"]),
                    "source_state": _blank(raw["source_state"]),
                    "source_zip": _blank(raw["source_zip"]),
                    "source_phone": _blank(raw["source_phone"]),
                    "source_website": _blank(raw["source_website"]),
                    "client_id": "" if raw["client_id"] is None else str(int(raw["client_id"])),
                }
            )
    return out


def alias_match_fields(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Normalized alias identity signals for matching (not display)."""
    from import_brown_industries import digits_phone, domain, norm_addr

    record_nos: list[str] = []
    norms: list[str] = []
    domains: list[str] = []
    phones: list[str] = []
    addrs: list[tuple[str, str, str]] = []
    seen_rn: set[str] = set()
    seen_norm: set[str] = set()
    seen_dom: set[str] = set()
    seen_phone: set[str] = set()
    seen_addr: set[tuple[str, str, str]] = set()
    for row in rows:
        rn = normalize_record_no(row.get("source_record_no"))
        if rn and rn not in seen_rn:
            seen_rn.add(rn)
            record_nos.append(rn)
        norm = _blank(row.get("alias_norm"))
        if norm and norm not in seen_norm:
            seen_norm.add(norm)
            norms.append(norm)
        dom = domain(_blank(row.get("source_website")))
        if dom and dom not in seen_dom:
            seen_dom.add(dom)
            domains.append(dom)
        phone = digits_phone(_blank(row.get("source_phone")))
        if phone and phone not in seen_phone:
            seen_phone.add(phone)
            phones.append(phone)
        addr = norm_addr(_blank(row.get("source_address"))) if _blank(row.get("source_address")) else ""
        city = _blank(row.get("source_city")).lower()
        state = state_for_match(row.get("source_state")) or ""
        triple = (addr, city, state)
        if addr and city and state and triple not in seen_addr:
            seen_addr.add(triple)
            addrs.append(triple)
    return {
        "record_nos": tuple(record_nos),
        "norm_names": tuple(norms),
        "domains": tuple(domains),
        "phones": tuple(phones),
        "addrs": tuple(addrs),
    }


def list_aliases_for_companies(
    conn: sqlite3.Connection,
    company_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Export-ready alias rows. Phase 3 can render these without a UI change now."""
    if not aliases_table_ready(conn):
        return []
    sql = """
        SELECT
            a.id, a.company_id, a.alias_name, a.alias_norm, a.source_system,
            a.source_record_no, a.client_id, a.source_batch_id, a.source_row,
            a.source_address, a.source_city, a.source_state, a.source_zip,
            a.source_phone, a.source_website, a.created_at, a.created_by_user_id
        FROM company_aliases a
    """
    params: list[Any] = []
    if company_ids:
        ids = sorted({int(cid) for cid in company_ids})
        placeholders = ",".join("?" * len(ids))
        sql += f" WHERE a.company_id IN ({placeholders})"
        params.extend(ids)
    sql += " ORDER BY a.company_id, a.id"
    rows = []
    for raw in conn.execute(sql, params):
        rows.append(
            {
                "id": int(raw["id"]),
                "company_id": int(raw["company_id"]),
                "alias_name": _blank(raw["alias_name"]),
                "alias_norm": _blank(raw["alias_norm"]),
                "source_system": _blank(raw["source_system"]),
                "source_record_no": _blank(raw["source_record_no"]),
                "client_id": None if raw["client_id"] is None else int(raw["client_id"]),
                "source_batch_id": (
                    None if raw["source_batch_id"] is None else int(raw["source_batch_id"])
                ),
                "source_row": None if raw["source_row"] is None else int(raw["source_row"]),
                "source_address": _blank(raw["source_address"]),
                "source_city": _blank(raw["source_city"]),
                "source_state": _blank(raw["source_state"]),
                "source_zip": _blank(raw["source_zip"]),
                "source_phone": _blank(raw["source_phone"]),
                "source_website": _blank(raw["source_website"]),
                "created_at": _blank(raw["created_at"]),
                "created_by_user_id": (
                    None
                    if raw["created_by_user_id"] is None
                    else int(raw["created_by_user_id"])
                ),
            }
        )
    return rows
