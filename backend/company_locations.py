"""Company locations and durable source identities.

Schema foundation only. Does not backfill locations or source RNs.
Does not change matching, merges, CCRs, or master display fields.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

LOCATION_TYPES = (
    "headquarters",
    "plant",
    "office",
    "branch",
    "warehouse",
    "facility",
    "unknown",
)

LOCATION_ID_COLUMNS = (
    ("contacts", "location_id"),
    ("client_company_relationships", "location_id"),
    ("company_shared_history_events", "location_id"),
    ("client_sales_events", "location_id"),
)

IdentityStatus = Literal["created", "existing", "conflict"]


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def normalize_location_flags(
    location_type: str, is_headquarters: int | bool
) -> tuple[str, int]:
    """Keep location_type and is_headquarters from silently contradicting."""
    kind = _blank(location_type).lower() or "unknown"
    if kind not in LOCATION_TYPES:
        raise ValueError(f"Unsupported location_type {location_type!r}.")
    hq = 1 if int(is_headquarters or 0) else 0
    if kind == "headquarters":
        hq = 1
    elif hq == 1:
        kind = "headquarters"
    return kind, hq


def ensure_company_location_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent CREATE TABLE / INDEX / nullable location_id columns. No backfill."""
    stats = {"created_locations_table": 0, "created_identities_table": 0, "columns_added": 0}
    if not _table_exists(conn, "companies"):
        return stats
    if not _table_exists(conn, "company_locations"):
        conn.execute(
            """
            CREATE TABLE company_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                location_name TEXT NOT NULL DEFAULT '',
                location_type TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (location_type IN (
                        'headquarters', 'plant', 'office', 'branch',
                        'warehouse', 'facility', 'unknown'
                    )),
                address TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                zip TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT 'US',
                phone TEXT NOT NULL DEFAULT '',
                phone_extension TEXT NOT NULL DEFAULT '',
                alt_phone TEXT NOT NULL DEFAULT '',
                alt_phone_extension TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                is_headquarters INTEGER NOT NULL DEFAULT 0,
                is_primary INTEGER NOT NULL DEFAULT 0,
                source_system TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            )
            """
        )
        stats["created_locations_table"] = 1
    if not _table_exists(conn, "company_source_identities"):
        conn.execute(
            """
            CREATE TABLE company_source_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                location_id INTEGER,
                client_id INTEGER,
                source_system TEXT NOT NULL,
                source_record_no TEXT NOT NULL,
                source_company_name TEXT NOT NULL DEFAULT '',
                source_address TEXT NOT NULL DEFAULT '',
                source_city TEXT NOT NULL DEFAULT '',
                source_state TEXT NOT NULL DEFAULT '',
                source_zip TEXT NOT NULL DEFAULT '',
                source_phone TEXT NOT NULL DEFAULT '',
                source_website TEXT NOT NULL DEFAULT '',
                source_batch_id INTEGER,
                source_row INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_by_user_id INTEGER,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY (location_id) REFERENCES company_locations(id) ON DELETE SET NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL,
                FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        stats["created_identities_table"] = 1
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_company_locations_company ON company_locations(company_id)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_locations_city_state
            ON company_locations(city, state, zip)
            WHERE TRIM(city) != ''
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_company_locations_one_hq
            ON company_locations(company_id)
            WHERE is_headquarters = 1
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_company_locations_one_primary
            ON company_locations(company_id)
            WHERE is_primary = 1
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_company_source_identities_idempotent
            ON company_source_identities (
                source_system,
                source_record_no,
                COALESCE(client_id, 0)
            )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_source_identities_company
            ON company_source_identities(company_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_source_identities_location
            ON company_source_identities(location_id)
            WHERE location_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_source_identities_client
            ON company_source_identities(client_id)
            WHERE client_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_source_identities_rn
            ON company_source_identities(source_system, source_record_no)
        """
    )
    for table, column in LOCATION_ID_COLUMNS:
        if not _table_exists(conn, table):
            continue
        if column in _table_columns(conn, table):
            continue
        # Live tables are non-empty: SQLite forbids REFERENCES on ALTER ADD COLUMN.
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")
        stats["columns_added"] += 1
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{table}_location_id
                ON {table}(location_id)
                WHERE location_id IS NOT NULL
            """
        )
    for table, column in LOCATION_ID_COLUMNS:
        if not _table_exists(conn, table) or column not in _table_columns(conn, table):
            continue
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{table}_location_id
                ON {table}(location_id)
                WHERE location_id IS NOT NULL
            """
        )
    return stats


def create_company_location(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    location_name: str = "",
    location_type: str = "unknown",
    address: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    country: str = "US",
    phone: str = "",
    phone_extension: str = "",
    alt_phone: str = "",
    alt_phone_extension: str = "",
    website: str = "",
    is_headquarters: int = 0,
    is_primary: int = 0,
    source_system: str = "",
) -> int:
    kind, hq = normalize_location_flags(location_type, is_headquarters)
    cur = conn.execute(
        """
        INSERT INTO company_locations (
            company_id, location_name, location_type, address, city, state, zip,
            country, phone, phone_extension, alt_phone, alt_phone_extension,
            website, is_headquarters, is_primary, source_system
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(company_id),
            _blank(location_name),
            kind,
            _blank(address),
            _blank(city),
            _blank(state),
            _blank(zip_code),
            _blank(country) or "US",
            _blank(phone),
            _blank(phone_extension),
            _blank(alt_phone),
            _blank(alt_phone_extension),
            _blank(website),
            hq,
            1 if int(is_primary or 0) else 0,
            _blank(source_system),
        ),
    )
    return int(cur.lastrowid)


def delete_company_location(conn: sqlite3.Connection, location_id: int) -> None:
    """Remove a location without deleting source identities or history rows."""
    lid = int(location_id)
    for table, column in LOCATION_ID_COLUMNS:
        if _table_exists(conn, table) and column in _table_columns(conn, table):
            conn.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} = ?", (lid,))
    if _table_exists(conn, "company_source_identities"):
        conn.execute(
            "UPDATE company_source_identities SET location_id = NULL WHERE location_id = ?",
            (lid,),
        )
    conn.execute("DELETE FROM company_locations WHERE id = ?", (lid,))


def upsert_company_source_identity(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    source_system: str,
    source_record_no: str,
    location_id: int | None = None,
    client_id: int | None = None,
    source_company_name: str = "",
    source_address: str = "",
    source_city: str = "",
    source_state: str = "",
    source_zip: str = "",
    source_phone: str = "",
    source_website: str = "",
    source_batch_id: int | None = None,
    source_row: int | None = None,
    created_by_user_id: int | None = None,
) -> tuple[int | None, IdentityStatus]:
    """Insert or fill-blank an identity. Never overwrite company/location conflicts."""
    system = _blank(source_system)
    rn = _blank(source_record_no)
    if not system or not rn:
        return None, "conflict"
    cid = int(client_id) if client_id is not None else None
    loc = int(location_id) if location_id is not None else None
    existing = conn.execute(
        """
        SELECT id, company_id, location_id, source_company_name, source_address,
               source_city, source_state, source_zip, source_phone, source_website,
               source_batch_id, source_row
        FROM company_source_identities
        WHERE source_system = ?
          AND source_record_no = ?
          AND COALESCE(client_id, 0) = COALESCE(?, 0)
        LIMIT 1
        """,
        (system, rn, cid),
    ).fetchone()
    if existing is not None:
        if int(existing["company_id"]) != int(company_id):
            return int(existing["id"]), "conflict"
        existing_loc = existing["location_id"]
        if loc is not None and existing_loc is not None and int(existing_loc) != loc:
            return int(existing["id"]), "conflict"
        updates: list[str] = []
        params: list[Any] = []
        if loc is not None and existing_loc is None:
            updates.append("location_id = ?")
            params.append(loc)

        def _fill(column: str, current: object, incoming: str) -> None:
            if incoming and not _blank(current):
                updates.append(f"{column} = ?")
                params.append(incoming)

        _fill("source_company_name", existing["source_company_name"], _blank(source_company_name))
        _fill("source_address", existing["source_address"], _blank(source_address))
        _fill("source_city", existing["source_city"], _blank(source_city))
        _fill("source_state", existing["source_state"], _blank(source_state))
        _fill("source_zip", existing["source_zip"], _blank(source_zip))
        _fill("source_phone", existing["source_phone"], _blank(source_phone))
        _fill("source_website", existing["source_website"], _blank(source_website))
        if source_batch_id is not None and existing["source_batch_id"] is None:
            updates.append("source_batch_id = ?")
            params.append(int(source_batch_id))
        if source_row is not None and existing["source_row"] is None:
            updates.append("source_row = ?")
            params.append(int(source_row))
        if updates:
            params.append(int(existing["id"]))
            conn.execute(
                f"UPDATE company_source_identities SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        return int(existing["id"]), "existing"
    try:
        cur = conn.execute(
            """
            INSERT INTO company_source_identities (
                company_id, location_id, client_id, source_system, source_record_no,
                source_company_name, source_address, source_city, source_state,
                source_zip, source_phone, source_website, source_batch_id, source_row,
                created_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(company_id),
                loc,
                cid,
                system,
                rn,
                _blank(source_company_name),
                _blank(source_address),
                _blank(source_city),
                _blank(source_state),
                _blank(source_zip),
                _blank(source_phone),
                _blank(source_website),
                int(source_batch_id) if source_batch_id is not None else None,
                int(source_row) if source_row is not None else None,
                _existing_user_id(conn, created_by_user_id),
            ),
        )
        return int(cur.lastrowid), "created"
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            SELECT id, company_id FROM company_source_identities
            WHERE source_system = ?
              AND source_record_no = ?
              AND COALESCE(client_id, 0) = COALESCE(?, 0)
            LIMIT 1
            """,
            (system, rn, cid),
        ).fetchone()
        if row is None:
            raise
        if int(row["company_id"]) != int(company_id):
            return int(row["id"]), "conflict"
        return int(row["id"]), "existing"


def location_site_key(
    address: object = "",
    city: object = "",
    state: object = "",
    zip_code: object = "",
) -> tuple[str, str, str, str]:
    """Normalized (address, city, state, zip5) for location dedupe. Not a matching key table."""
    addr = " ".join(_blank(address).lower().split())
    city_text = _blank(city).lower()
    city_text = city_text.split(",", 1)[0].strip()
    zip5 = "".join(ch for ch in _blank(zip_code) if ch.isdigit())[:5]
    state_text = _blank(state).upper()
    if len(state_text) > 2:
        state_text = state_text[:2]
    return addr, city_text, state_text, zip5


def sites_match(
    left: tuple[str, str, str, str],
    right: tuple[str, str, str, str],
) -> bool:
    """Same site if address+city+state match; ZIP must agree when both present."""
    l_addr, l_city, l_state, l_zip = left
    r_addr, r_city, r_state, r_zip = right
    if not l_addr or not r_addr or l_addr != r_addr:
        return False
    if not l_city or not r_city or l_city != r_city:
        return False
    if not l_state or not r_state or l_state != r_state:
        return False
    if l_zip and r_zip and l_zip != r_zip:
        return False
    return True


def find_company_location_by_site(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    address: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
) -> int | None:
    """Return an existing location id for this org+site, or None."""
    if not _table_exists(conn, "company_locations"):
        return None
    wanted = location_site_key(address, city, state, zip_code)
    if not wanted[0] or not wanted[1] or not wanted[2]:
        return None
    rows = conn.execute(
        """
        SELECT id, address, city, state, zip
        FROM company_locations
        WHERE company_id = ?
        """,
        (int(company_id),),
    ).fetchall()
    for row in rows:
        have = location_site_key(row["address"], row["city"], row["state"], row["zip"])
        if sites_match(wanted, have):
            return int(row["id"])
    return None


def ensure_company_location(
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> tuple[int, Literal["created", "existing"]]:
    """Create a location or reuse the same org+site. Does not overwrite existing rows."""
    found = find_company_location_by_site(
        conn,
        company_id=int(kwargs["company_id"]),
        address=_blank(kwargs.get("address")),
        city=_blank(kwargs.get("city")),
        state=_blank(kwargs.get("state")),
        zip_code=_blank(kwargs.get("zip_code")),
    )
    if found is not None:
        return found, "existing"
    return create_company_location(conn, **kwargs), "created"
