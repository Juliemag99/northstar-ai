"""Canonical contact-phone keys and US display formatting.

Display `contacts.phone` / `alt_phone` stay user-visible text. Matching uses
derived NANP digits in `contact_phone_keys`. Does not change `digits_phone`
(company matching).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

PHONE_KEY_SLOTS = ("phone", "alt_phone")
PHONE_MATCH_LIMIT = 20

_EXT_SPLIT = re.compile(
    r"(?i)(?:\s+extension|\s+ext\.?|\s+x|x|#)\s*([0-9][0-9 \-]*)\s*$"
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def split_phone_extension(raw: str) -> tuple[str, str]:
    text = _blank(raw)
    if not text:
        return "", ""
    match = _EXT_SPLIT.search(text)
    if not match:
        return text, ""
    main = text[: match.start()].rstrip()
    ext = re.sub(r"\D", "", match.group(1) or "")
    return main, ext


def _nanp_digits(main: str) -> str:
    digits = re.sub(r"\D", "", _blank(main))
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) > 10:
        digits = digits[:10]
    return digits


def canonical_contact_phone(raw: str) -> tuple[str, str]:
    """Return (nanp10, last7). nanp10 is set only for a 10-digit NANP number."""
    main, _ext = split_phone_extension(raw)
    digits = _nanp_digits(main)
    if len(digits) == 10:
        return digits, digits[-7:]
    if len(digits) >= 7:
        return "", digits[-7:]
    return "", ""


def format_us_phone_display(raw: str) -> str:
    """Format a 10-digit US number as (xxx) xxx-xxxx, keeping a trailing extension."""
    text = _blank(raw)
    if not text:
        return ""
    main, ext = split_phone_extension(text)
    digits = _nanp_digits(main)
    if len(digits) != 10:
        return text
    formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if ext:
        return f"{formatted} x{ext}"
    return formatted


def sqlite_phone_nanp10(value: object | None) -> str:
    nanp10, _last7 = canonical_contact_phone("" if value is None else str(value))
    return nanp10


def sqlite_phone_last7(value: object | None) -> str:
    _nanp10, last7 = canonical_contact_phone("" if value is None else str(value))
    return last7


def register_contact_phone_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "northstar_phone_nanp10", 1, sqlite_phone_nanp10, deterministic=True
    )
    conn.create_function(
        "northstar_phone_last7", 1, sqlite_phone_last7, deterministic=True
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _index_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    sql = row["sql"]
    return None if sql is None else str(sql)


def _phone_index_is_valid(sql: str | None, column: str) -> bool:
    """True when sql indexes contact_phone_keys on the expected column."""
    if not sql:
        return False
    compact = " ".join(sql.lower().split()).replace(" ", "")
    return f"contact_phone_keys({column})" in compact


def _ensure_contact_phone_index(
    conn: sqlite3.Connection, name: str, column: str
) -> None:
    """Create the index if missing. Do not drop or rebuild a valid existing index."""
    existing = _index_sql(conn, name)
    if _phone_index_is_valid(existing, column):
        return
    if existing is not None:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {name} ON contact_phone_keys({column})"
    )


def upsert_contact_phone_keys(
    conn: sqlite3.Connection,
    contact_id: int,
    phone: str = "",
    alt_phone: str = "",
) -> None:
    if not _table_exists(conn, "contact_phone_keys"):
        ensure_contact_phone_key_schema(conn)
    conn.execute(
        "DELETE FROM contact_phone_keys WHERE contact_id = ?",
        (int(contact_id),),
    )
    values = {"phone": phone, "alt_phone": alt_phone}
    for slot in PHONE_KEY_SLOTS:
        nanp10, last7 = canonical_contact_phone(values[slot])
        if not nanp10 and not last7:
            continue
        conn.execute(
            """
            INSERT INTO contact_phone_keys (contact_id, slot, nanp10, last7)
            VALUES (?, ?, ?, ?)
            """,
            (int(contact_id), slot, nanp10, last7),
        )


def backfill_contact_phone_keys(conn: sqlite3.Connection) -> dict[str, int]:
    """Populate keys from stored display phones. Does not rewrite phone text."""
    before = int(
        conn.execute("SELECT COUNT(*) AS n FROM contact_phone_keys").fetchone()["n"]
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
        SELECT id, 'phone',
               northstar_phone_nanp10(phone),
               northstar_phone_last7(phone)
        FROM contacts
        WHERE northstar_phone_nanp10(phone) != ''
           OR northstar_phone_last7(phone) != ''
        UNION ALL
        SELECT id, 'alt_phone',
               northstar_phone_nanp10(alt_phone),
               northstar_phone_last7(alt_phone)
        FROM contacts
        WHERE northstar_phone_nanp10(alt_phone) != ''
           OR northstar_phone_last7(alt_phone) != ''
        """
    )
    after = int(
        conn.execute("SELECT COUNT(*) AS n FROM contact_phone_keys").fetchone()["n"]
    )
    return {
        "keys_before": before,
        "keys_inserted": after - before,
        "keys_after": after,
        "display_rewritten": 0,
    }


def ensure_contact_phone_key_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Create table, indexes, triggers, and backfill keys when the table is new/empty."""
    stats = {
        "created_table": 0,
        "keys_before": 0,
        "keys_inserted": 0,
        "keys_after": 0,
        "display_rewritten": 0,
    }
    if not _table_exists(conn, "contacts"):
        return stats
    created = not _table_exists(conn, "contact_phone_keys")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_phone_keys (
            contact_id INTEGER NOT NULL,
            slot TEXT NOT NULL CHECK (slot IN ('phone', 'alt_phone')),
            nanp10 TEXT NOT NULL DEFAULT '',
            last7 TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (contact_id, slot),
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_contact_phone_index(conn, "idx_contact_phone_keys_nanp10", "nanp10")
    _ensure_contact_phone_index(conn, "idx_contact_phone_keys_last7", "last7")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_contact_phone_keys_ai
        AFTER INSERT ON contacts
        BEGIN
            INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
            SELECT NEW.id, 'phone',
                   northstar_phone_nanp10(NEW.phone),
                   northstar_phone_last7(NEW.phone)
            WHERE northstar_phone_nanp10(NEW.phone) != ''
               OR northstar_phone_last7(NEW.phone) != '';
            INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
            SELECT NEW.id, 'alt_phone',
                   northstar_phone_nanp10(NEW.alt_phone),
                   northstar_phone_last7(NEW.alt_phone)
            WHERE northstar_phone_nanp10(NEW.alt_phone) != ''
               OR northstar_phone_last7(NEW.alt_phone) != '';
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_contact_phone_keys_au
        AFTER UPDATE OF phone, alt_phone ON contacts
        BEGIN
            DELETE FROM contact_phone_keys WHERE contact_id = NEW.id;
            INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
            SELECT NEW.id, 'phone',
                   northstar_phone_nanp10(NEW.phone),
                   northstar_phone_last7(NEW.phone)
            WHERE northstar_phone_nanp10(NEW.phone) != ''
               OR northstar_phone_last7(NEW.phone) != '';
            INSERT OR IGNORE INTO contact_phone_keys (contact_id, slot, nanp10, last7)
            SELECT NEW.id, 'alt_phone',
                   northstar_phone_nanp10(NEW.alt_phone),
                   northstar_phone_last7(NEW.alt_phone)
            WHERE northstar_phone_nanp10(NEW.alt_phone) != ''
               OR northstar_phone_last7(NEW.alt_phone) != '';
        END
        """
    )
    existing = int(
        conn.execute("SELECT COUNT(*) AS n FROM contact_phone_keys").fetchone()["n"]
    )
    stats["created_table"] = 1 if created else 0
    stats["keys_before"] = existing
    if existing == 0:
        filled = backfill_contact_phone_keys(conn)
        stats.update(filled)
    else:
        stats["keys_after"] = existing
    return stats


def lookup_contact_phone_matches(
    conn: sqlite3.Connection,
    *raw_phones: str,
    company_id: int | None = None,
) -> list[tuple[int, list[str], str]]:
    """Return (contact_id, reasons, confidence). Exact 10-digit is high; last-7 is possible."""
    if not _table_exists(conn, "contact_phone_keys"):
        return []
    seen: dict[int, tuple[list[str], str]] = {}

    def _remember(contact_id: int, reason: str, confidence: str) -> None:
        existing = seen.get(contact_id)
        if existing is None:
            seen[contact_id] = ([reason], confidence)
            return
        reasons, current = existing
        if reason not in reasons:
            reasons.append(reason)
        if confidence == "high":
            seen[contact_id] = (reasons, "high")

    def _rows(sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        return list(conn.execute(sql, params).fetchall())

    company_join = ""
    company_clause = ""
    company_params: tuple[Any, ...] = ()
    if company_id is not None:
        company_join = " JOIN contacts c ON c.id = k.contact_id "
        company_clause = " AND c.company_id = ? "
        company_params = (int(company_id),)

    def _select_ids(where_sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        sql = (
            "SELECT DISTINCT k.contact_id AS contact_id "
            "FROM contact_phone_keys k "
            f"{company_join}"
            f"WHERE {where_sql}"
            f"{company_clause}"
            " LIMIT ?"
        )
        return _rows(sql, (*params, *company_params, PHONE_MATCH_LIMIT))

    for raw in raw_phones:
        nanp10, last7 = canonical_contact_phone(raw)
        if nanp10:
            for row in _select_ids("k.nanp10 = ?", (nanp10,)):
                _remember(int(row["contact_id"]), "phone_exact", "high")
        if last7:
            if nanp10:
                for row in _select_ids("k.last7 = ? AND k.nanp10 != ?", (last7, nanp10)):
                    cid = int(row["contact_id"])
                    if seen.get(cid, ([], ""))[1] != "high":
                        _remember(cid, "phone_last7", "possible")
            else:
                for row in _select_ids("k.last7 = ? AND k.nanp10 = ''", (last7,)):
                    _remember(int(row["contact_id"]), "phone_exact", "high")
                for row in _select_ids("k.last7 = ? AND k.nanp10 != ''", (last7,)):
                    cid = int(row["contact_id"])
                    if seen.get(cid, ([], ""))[1] != "high":
                        _remember(cid, "phone_last7", "possible")

    return [(cid, reasons, conf) for cid, (reasons, conf) in seen.items()]
