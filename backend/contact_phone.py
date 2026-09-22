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

SAFE_SPLIT = "SAFE EXTENSION SPLIT"
REVIEW = "REVIEW"
NOT_EXT = "NOT AN EXTENSION"

_PHONE_CHUNK_RE = re.compile(
    r"(?:\+?1[\s.\-]*)?(?:\(?\d{3}\)?[\s.\-]*)\d{3}[\s.\-]*\d{4}"
)
_EXT_MARK_RE = re.compile(
    r"""(?ix)
    (?:
        (?:^|[\s\-./,(])
        (?:extension|ext\.?|xt)
        \s*[:.\-]?\s*
        |
        (?<![A-Za-z])x\s*[:.\-]?\s*
        |
        \#\s*
    )
    \d
    """
)
_SAFE_SPLIT_RE = re.compile(
    r"""(?ix)
    ^
    (?P<main>.*?)
    (?:
        (?:[\s\-./,])*
        (?:extension|ext\.?|xt)
        \s*[:.\-]?\s*
      | (?<![A-Za-z])x\s*[:.\-]?\s*
      | \s*\#\s*
      | \s*\(\s*(?:extension|ext\.?|xt|x)?\s*
    )
    (?P<ext>\d{1,6})
    \)?
    \s*$
    """
)
_EXT_ONLY_RE = re.compile(
    r"""(?ix)
    ^\s*(?:extension|ext\.?|xt|x|\#)?\s*[:.\-]?\s*\d{1,6}\s*$
    """
)
_NON_PLUS1_RE = re.compile(r"^\s*\+(?!1\b)")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _all_digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def _nanp_digits(main: str) -> str:
    digits = _all_digits(main)
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) > 10:
        digits = digits[:10]
    return digits


def _display_main(nanp10: str) -> str:
    if len(nanp10) != 10:
        return ""
    return f"({nanp10[:3]}) {nanp10[3:6]}-{nanp10[6:]}"


def parse_extension_candidate(raw: str, *, field: str = "phone") -> dict:
    """Classify a phone string. SAFE splits require an explicit marker and full digit accounting."""
    current = _blank(raw)
    chunks = _PHONE_CHUNK_RE.findall(current)
    all_d = _all_digits(current)
    ext_like = bool(current) and (
        bool(_EXT_MARK_RE.search(current))
        or (
            len(all_d[1:] if len(all_d) >= 11 and all_d.startswith("1") else all_d)
            > 10
        )
        or bool(re.search(r"\(\s*\d{1,6}\s*\)\s*$", current) and len(all_d) >= 10)
    )
    base = {
        "current": current,
        "parsed_main_digits": "",
        "proposed_main_display": "",
        "parsed_extension": "",
        "classification": NOT_EXT,
        "reason": "",
        "information_preserved": "n/a",
        "extension_like": False,
    }
    if not current:
        base["reason"] = "Blank."
        return base
    base["extension_like"] = ext_like
    if _NON_PLUS1_RE.search(current):
        return {
            **base,
            "parsed_main_digits": _nanp_digits(current),
            "classification": REVIEW,
            "reason": "International number (non-+1).",
            "information_preserved": "no — do not force US split",
            "extension_like": True,
        }
    if len(chunks) >= 2:
        return {
            **base,
            "parsed_main_digits": _nanp_digits(current),
            "classification": REVIEW,
            "reason": "Multiple phone numbers in one field.",
            "information_preserved": "no — would drop a second number",
            "extension_like": True,
        }
    if _EXT_ONLY_RE.fullmatch(current) and len(all_d) <= 6:
        return {
            **base,
            "parsed_extension": all_d,
            "classification": REVIEW,
            "reason": (
                "Extension-only value; no 10-digit main number in this field."
                if "alt" in field
                else "Looks like an extension only; no 10-digit main number."
            ),
            "information_preserved": "no — main number missing",
            "extension_like": True,
        }
    match = _SAFE_SPLIT_RE.match(current)
    if match:
        main = _blank(match.group("main"))
        ext = re.sub(r"\D", "", match.group("ext") or "")
        main_d = _all_digits(main)
        nanp = _nanp_digits(main)
        leftover_main = (
            main_d[1:] if len(main_d) >= 11 and main_d.startswith("1") else main_d
        )
        leftover_after_10 = leftover_main[10:] if len(leftover_main) > 10 else ""
        reconstructed = (
            ("1" if len(main_d) >= 11 and main_d.startswith("1") else "")
            + nanp
            + ext
        )
        preserved = reconstructed == all_d and not leftover_after_10
        if len(nanp) == 10 and ext and preserved:
            return {
                **base,
                "parsed_main_digits": nanp,
                "proposed_main_display": _display_main(nanp),
                "parsed_extension": ext,
                "classification": SAFE_SPLIT,
                "reason": "Unambiguous extension marker; 10-digit US/Canadian main; all digits accounted for.",
                "information_preserved": "yes",
                "extension_like": True,
            }
        return {
            **base,
            "parsed_main_digits": nanp,
            "proposed_main_display": _display_main(nanp),
            "parsed_extension": ext,
            "classification": REVIEW,
            "reason": "Extension marker present but main/extension digits are ambiguous or not fully accounted for.",
            "information_preserved": "no" if not preserved else "uncertain",
            "extension_like": True,
        }
    core = all_d[1:] if len(all_d) >= 11 and all_d.startswith("1") else all_d
    if len(core) > 10:
        return {
            **base,
            "parsed_main_digits": core[:10],
            "proposed_main_display": _display_main(core[:10]),
            "parsed_extension": core[10:],
            "classification": REVIEW,
            "reason": "Extra trailing digits without a proven extension marker (do not assume an extension).",
            "information_preserved": "no — leftover digits not proven as an extension",
            "extension_like": True,
        }
    if ext_like:
        return {
            **base,
            "parsed_main_digits": _nanp_digits(current),
            "classification": REVIEW,
            "reason": "Extension-like token present but could not safely isolate a 10-digit main + extension.",
            "information_preserved": "no",
            "extension_like": True,
        }
    nanp = _nanp_digits(current)
    if len(core) == 10:
        return {
            **base,
            "parsed_main_digits": nanp,
            "proposed_main_display": _display_main(nanp),
            "classification": NOT_EXT,
            "reason": "Single 10-digit US/Canadian number; no extension.",
            "information_preserved": "n/a",
            "extension_like": False,
        }
    return {
        **base,
        "parsed_main_digits": nanp,
        "classification": NOT_EXT if len(core) in {0, 10} else REVIEW,
        "reason": "No extension marker and no extra digits."
        if len(core) in {0, 10}
        else f"Unusual length ({len(all_d)} digits); not a clean extension split.",
        "information_preserved": "n/a",
        "extension_like": False,
    }


def split_phone_extension(raw: str) -> tuple[str, str]:
    """Return (main, extension) only for a high-confidence SAFE split. Otherwise (raw, '')."""
    text = _blank(raw)
    if not text:
        return "", ""
    parsed = parse_extension_candidate(text, field="phone")
    if parsed["classification"] == SAFE_SPLIT:
        return parsed["proposed_main_display"], parsed["parsed_extension"]
    return text, ""


def store_phone_parts(raw: str, *, field: str = "phone") -> tuple[str, str]:
    """Durable storage pair: main display without embedded extension, digits-only extension."""
    parsed = parse_extension_candidate(raw, field=field)
    if parsed["classification"] == SAFE_SPLIT:
        return parsed["proposed_main_display"], parsed["parsed_extension"]
    text = _blank(raw)
    if not text:
        return "", ""
    if parsed["classification"] == NOT_EXT and len(parsed["parsed_main_digits"]) == 10:
        return parsed["proposed_main_display"] or _display_main(parsed["parsed_main_digits"]), ""
    return text, ""


def digits_only_extension(raw: object = "") -> str:
    return re.sub(r"\D", "", _blank(raw))


def stored_phone_pair(
    phone_raw: str,
    extension_raw: str | None = None,
    *,
    field: str = "phone",
) -> tuple[str, str]:
    """Durable (main, extension). Explicit extension stays out of stored main phone."""
    main, parsed_ext = store_phone_parts(phone_raw, field=field)
    if extension_raw is None:
        return main, parsed_ext
    return main, digits_only_extension(extension_raw)


def format_phone_with_extension(main: object = "", extension: object = "") -> str:
    """Render stored main + extension as '(xxx) xxx-xxxx x16'."""
    phone = _blank(main)
    ext = digits_only_extension(extension)
    if phone and ext:
        return f"{phone} x{ext}"
    return phone


def row_phone_display(row: sqlite3.Row | dict[str, Any] | None, field: str, ext_field: str) -> str:
    if row is None:
        return ""
    keys = row.keys() if hasattr(row, "keys") else row
    main = _blank(row[field]) if field in keys else ""
    ext = _blank(row[ext_field]) if ext_field in keys else ""
    return format_phone_with_extension(main, ext)


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
    """Format a 10-digit US number as (xxx) xxx-xxxx, rendering a recognized extension as x{ext}."""
    text = _blank(raw)
    if not text:
        return ""
    main, ext = split_phone_extension(text)
    if ext:
        return format_phone_with_extension(main, ext)
    digits = _nanp_digits(main)
    if len(digits) != 10:
        return text
    return _display_main(digits)


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
