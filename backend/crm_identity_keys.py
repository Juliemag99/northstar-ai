"""Maintained CRM identity keys for indexed import matching.

Side tables store normalized company/contact identity signals so CRM Import and
Client Data Import can probe candidates without full-table scans.

SQLite today; PostgreSQL later:
  company_identity_keys / contact_person_keys → plain tables with btree
  (or expression) indexes on the same columns. Triggers become AFTER INSERT/
  UPDATE functions, or application upserts via the same helpers. No FTS5 /
  generated-column dependency is required.

Does not rewrite master display columns (company_name, website, phone, etc.).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from crm_import_state import state_for_match

IN_CHUNK = 400

# Future PostgreSQL equivalents (documentation for cutover):
#   CREATE TABLE company_identity_keys (...);
#   CREATE INDEX ON company_identity_keys (record_no) WHERE record_no <> '';
#   CREATE INDEX ON company_identity_keys (domain) WHERE domain <> '';
#   CREATE INDEX ON company_identity_keys (norm_name) WHERE norm_name <> '';
#   CREATE INDEX ON company_identity_keys (phone_digits) WHERE phone_digits <> '';
#   CREATE INDEX ON company_identity_keys (phone_last7) WHERE phone_last7 <> '';
#   CREATE INDEX ON company_identity_keys (addr_norm, city_norm, state_norm)
#     WHERE addr_norm <> '';
#   CREATE TABLE contact_person_keys (...);
#   CREATE INDEX ON contact_person_keys (person_norm) WHERE person_norm <> '';


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


# Keep Record No. normalization local so get_connection() does not import
# shared_note_history_import → models during connection setup.
def normalize_record_no(value: object | None) -> str:
    """Trim; drop Excel-style trailing .0 — must match shared_note_history_import."""
    import re

    text = _blank(value)
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _phone_last7(digits: str) -> str:
    d = _blank(digits)
    return d[-7:] if len(d) >= 7 else ""


def company_identity_tuple(
    *,
    external_record_no: object = "",
    company_name: object = "",
    website: object = "",
    legacy_phone: object = "",
    address: object = "",
    city: object = "",
    state: object = "",
) -> dict[str, str]:
    # Lazy import avoids db → crm_identity_keys → import_brown → db cycle.
    from import_brown_industries import digits_phone, domain, norm_addr, norm_name

    phone = digits_phone(_blank(legacy_phone))
    addr = norm_addr(_blank(address)) if _blank(address) else ""
    city_n = _blank(city).lower()
    state_n = state_for_match(state) or ""
    if not (addr and city_n and state_n):
        addr = city_n = state_n = ""
    return {
        "record_no": normalize_record_no(external_record_no),
        "domain": domain(_blank(website)),
        "norm_name": norm_name(_blank(company_name)) if _blank(company_name) else "",
        "phone_digits": phone,
        "phone_last7": _phone_last7(phone),
        "addr_norm": addr,
        "city_norm": city_n,
        "state_norm": state_n,
    }


def contact_person_norm(first_name: object = "", last_name: object = "") -> str:
    display = f"{_blank(first_name)} {_blank(last_name)}".strip()
    if not display:
        return ""
    import re

    s = re.sub(r"[^a-z0-9]+", " ", display.lower())
    return " ".join(s.split())


# --- SQLite UDFs (deterministic; registered on every get_connection) ---


def sqlite_norm_record_no(value: object | None) -> str:
    return normalize_record_no("" if value is None else str(value))


def sqlite_domain(value: object | None) -> str:
    from import_brown_industries import domain

    return domain("" if value is None else str(value))


def sqlite_norm_name(value: object | None) -> str:
    from import_brown_industries import norm_name

    text = _blank(value)
    return norm_name(text) if text else ""


def sqlite_digits_phone(value: object | None) -> str:
    from import_brown_industries import digits_phone

    return digits_phone("" if value is None else str(value))


def sqlite_phone_last7_digits(value: object | None) -> str:
    from import_brown_industries import digits_phone

    return _phone_last7(digits_phone("" if value is None else str(value)))


def sqlite_norm_addr(value: object | None) -> str:
    from import_brown_industries import norm_addr

    text = _blank(value)
    return norm_addr(text) if text else ""


def sqlite_state_for_match(value: object | None) -> str:
    return state_for_match("" if value is None else str(value)) or ""


def sqlite_city_norm(value: object | None) -> str:
    return _blank(value).lower()


def sqlite_person_norm(first: object | None, last: object | None) -> str:
    return contact_person_norm(first, last)


def register_crm_identity_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "northstar_norm_record_no", 1, sqlite_norm_record_no, deterministic=True
    )
    conn.create_function("northstar_domain", 1, sqlite_domain, deterministic=True)
    conn.create_function("northstar_norm_name", 1, sqlite_norm_name, deterministic=True)
    conn.create_function(
        "northstar_digits_phone", 1, sqlite_digits_phone, deterministic=True
    )
    conn.create_function(
        "northstar_company_phone_last7", 1, sqlite_phone_last7_digits, deterministic=True
    )
    conn.create_function("northstar_norm_addr", 1, sqlite_norm_addr, deterministic=True)
    conn.create_function(
        "northstar_state_for_match", 1, sqlite_state_for_match, deterministic=True
    )
    conn.create_function("northstar_city_norm", 1, sqlite_city_norm, deterministic=True)
    conn.create_function(
        "northstar_person_norm", 2, sqlite_person_norm, deterministic=True
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _chunked(values: list[Any], size: int = IN_CHUNK) -> Iterable[list[Any]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def upsert_company_identity_key(conn: sqlite3.Connection, company_id: int, **fields: object) -> None:
    """Explicit upsert (write paths / tests). Triggers cover normal INSERT/UPDATE."""
    if not _table_exists(conn, "company_identity_keys"):
        ensure_crm_identity_key_schema(conn)
    keys = company_identity_tuple(**fields)
    conn.execute(
        """
        INSERT INTO company_identity_keys (
            company_id, record_no, domain, norm_name, phone_digits, phone_last7,
            addr_norm, city_norm, state_norm
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
            record_no = excluded.record_no,
            domain = excluded.domain,
            norm_name = excluded.norm_name,
            phone_digits = excluded.phone_digits,
            phone_last7 = excluded.phone_last7,
            addr_norm = excluded.addr_norm,
            city_norm = excluded.city_norm,
            state_norm = excluded.state_norm
        """,
        (
            int(company_id),
            keys["record_no"],
            keys["domain"],
            keys["norm_name"],
            keys["phone_digits"],
            keys["phone_last7"],
            keys["addr_norm"],
            keys["city_norm"],
            keys["state_norm"],
        ),
    )


def backfill_company_identity_keys(conn: sqlite3.Connection) -> dict[str, int]:
    before = int(
        conn.execute("SELECT COUNT(*) AS n FROM company_identity_keys").fetchone()["n"]
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO company_identity_keys (
            company_id, record_no, domain, norm_name, phone_digits, phone_last7,
            addr_norm, city_norm, state_norm
        )
        SELECT
            id,
            northstar_norm_record_no(external_record_no),
            northstar_domain(website),
            northstar_norm_name(company_name),
            northstar_digits_phone(legacy_phone),
            northstar_company_phone_last7(legacy_phone),
            CASE
              WHEN TRIM(COALESCE(address, '')) = ''
                OR TRIM(COALESCE(city, '')) = ''
                OR northstar_state_for_match(state) = ''
              THEN ''
              ELSE northstar_norm_addr(address)
            END,
            CASE
              WHEN TRIM(COALESCE(address, '')) = ''
                OR TRIM(COALESCE(city, '')) = ''
                OR northstar_state_for_match(state) = ''
              THEN ''
              ELSE northstar_city_norm(city)
            END,
            CASE
              WHEN TRIM(COALESCE(address, '')) = ''
                OR TRIM(COALESCE(city, '')) = ''
                OR northstar_state_for_match(state) = ''
              THEN ''
              ELSE northstar_state_for_match(state)
            END
        FROM companies
        """
    )
    after = int(
        conn.execute("SELECT COUNT(*) AS n FROM company_identity_keys").fetchone()["n"]
    )
    return {"keys_before": before, "keys_after": after, "display_rewritten": 0}


def backfill_contact_person_keys(conn: sqlite3.Connection) -> dict[str, int]:
    before = int(
        conn.execute("SELECT COUNT(*) AS n FROM contact_person_keys").fetchone()["n"]
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO contact_person_keys (contact_id, company_id, person_norm)
        SELECT id, company_id, northstar_person_norm(first_name, last_name)
        FROM contacts
        """
    )
    after = int(
        conn.execute("SELECT COUNT(*) AS n FROM contact_person_keys").fetchone()["n"]
    )
    return {"keys_before": before, "keys_after": after, "display_rewritten": 0}


class IdentityKeysNotReady(RuntimeError):
    """Decision-critical identity infrastructure missing or incomplete."""


IDENTITY_KEYS_NOT_READY = (
    "CRM identity keys are missing or incomplete; run controlled migrate_schema "
    "before CRM Import or Client Data Import."
)

_REQUIRED_COMPANY_TRIGGERS = (
    "trg_company_identity_keys_ai",
    "trg_company_identity_keys_au",
)
_REQUIRED_CONTACT_TRIGGERS = (
    "trg_contact_person_keys_ai",
    "trg_contact_person_keys_au",
)


def reconcile_identity_key_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts for post-migrate reconciliation. Does not rewrite display values."""
    companies = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
    company_keys = (
        int(conn.execute("SELECT COUNT(*) AS n FROM company_identity_keys").fetchone()["n"])
        if _table_exists(conn, "company_identity_keys")
        else -1
    )
    company_missing = (
        int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM companies c
                LEFT JOIN company_identity_keys k ON k.company_id = c.id
                WHERE k.company_id IS NULL
                """
            ).fetchone()["n"]
        )
        if company_keys >= 0
        else companies
    )
    company_orphans = (
        int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM company_identity_keys k
                LEFT JOIN companies c ON c.id = k.company_id
                WHERE c.id IS NULL
                """
            ).fetchone()["n"]
        )
        if company_keys >= 0
        else 0
    )
    contacts = (
        int(conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"])
        if _table_exists(conn, "contacts")
        else 0
    )
    contact_keys = (
        int(conn.execute("SELECT COUNT(*) AS n FROM contact_person_keys").fetchone()["n"])
        if _table_exists(conn, "contact_person_keys")
        else -1
    )
    contact_missing = (
        int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM contacts c
                LEFT JOIN contact_person_keys k ON k.contact_id = c.id
                WHERE k.contact_id IS NULL
                """
            ).fetchone()["n"]
        )
        if contact_keys >= 0
        else contacts
    )
    contact_orphans = (
        int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM contact_person_keys k
                LEFT JOIN contacts c ON c.id = k.contact_id
                WHERE c.id IS NULL
                """
            ).fetchone()["n"]
        )
        if contact_keys >= 0
        else 0
    )
    return {
        "companies": companies,
        "company_keys": company_keys,
        "company_missing_keys": company_missing,
        "company_orphan_keys": company_orphans,
        "contacts": contacts,
        "contact_keys": contact_keys,
        "contact_missing_keys": contact_missing,
        "contact_orphan_keys": contact_orphans,
    }


def require_company_identity_ready(conn: sqlite3.Connection) -> None:
    """Fail closed: never plan matches without complete company identity keys."""
    if not _table_exists(conn, "companies"):
        return
    if not _table_exists(conn, "company_identity_keys"):
        raise IdentityKeysNotReady(IDENTITY_KEYS_NOT_READY)
    trigger_rows = {
        str(r["name"] if hasattr(r, "keys") else r[0])
        for r in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'trigger' AND name IN (?, ?)
            """,
            _REQUIRED_COMPANY_TRIGGERS,
        )
    }
    if set(_REQUIRED_COMPANY_TRIGGERS) - trigger_rows:
        raise IdentityKeysNotReady(IDENTITY_KEYS_NOT_READY)
    missing = conn.execute(
        """
        SELECT 1
        FROM companies c
        LEFT JOIN company_identity_keys k ON k.company_id = c.id
        WHERE k.company_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if missing is not None:
        raise IdentityKeysNotReady(IDENTITY_KEYS_NOT_READY)
    orphan = conn.execute(
        """
        SELECT 1
        FROM company_identity_keys k
        LEFT JOIN companies c ON c.id = k.company_id
        WHERE c.id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan is not None:
        raise IdentityKeysNotReady(IDENTITY_KEYS_NOT_READY)


def ensure_crm_identity_key_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Create identity tables/indexes/triggers and idempotently backfill."""
    stats = {"company_keys": 0, "contact_keys": 0}
    register_crm_identity_functions(conn)
    if not _table_exists(conn, "companies"):
        return stats

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_identity_keys (
            company_id INTEGER PRIMARY KEY,
            record_no TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            norm_name TEXT NOT NULL DEFAULT '',
            phone_digits TEXT NOT NULL DEFAULT '',
            phone_last7 TEXT NOT NULL DEFAULT '',
            addr_norm TEXT NOT NULL DEFAULT '',
            city_norm TEXT NOT NULL DEFAULT '',
            state_norm TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_identity_record_no
            ON company_identity_keys(record_no)
            WHERE record_no != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_identity_domain
            ON company_identity_keys(domain)
            WHERE domain != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_identity_norm_name
            ON company_identity_keys(norm_name)
            WHERE norm_name != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_identity_phone
            ON company_identity_keys(phone_digits)
            WHERE phone_digits != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_identity_phone_last7
            ON company_identity_keys(phone_last7)
            WHERE phone_last7 != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_identity_addr
            ON company_identity_keys(addr_norm, city_norm, state_norm)
            WHERE addr_norm != ''
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_company_identity_keys_ai")
    conn.execute("DROP TRIGGER IF EXISTS trg_company_identity_keys_au")
    conn.execute(
        """
        CREATE TRIGGER trg_company_identity_keys_ai
        AFTER INSERT ON companies
        BEGIN
            INSERT OR REPLACE INTO company_identity_keys (
                company_id, record_no, domain, norm_name, phone_digits, phone_last7,
                addr_norm, city_norm, state_norm
            )
            SELECT
                NEW.id,
                northstar_norm_record_no(NEW.external_record_no),
                northstar_domain(NEW.website),
                northstar_norm_name(NEW.company_name),
                northstar_digits_phone(NEW.legacy_phone),
                northstar_company_phone_last7(NEW.legacy_phone),
                CASE
                  WHEN TRIM(COALESCE(NEW.address, '')) = ''
                    OR TRIM(COALESCE(NEW.city, '')) = ''
                    OR northstar_state_for_match(NEW.state) = ''
                  THEN ''
                  ELSE northstar_norm_addr(NEW.address)
                END,
                CASE
                  WHEN TRIM(COALESCE(NEW.address, '')) = ''
                    OR TRIM(COALESCE(NEW.city, '')) = ''
                    OR northstar_state_for_match(NEW.state) = ''
                  THEN ''
                  ELSE northstar_city_norm(NEW.city)
                END,
                CASE
                  WHEN TRIM(COALESCE(NEW.address, '')) = ''
                    OR TRIM(COALESCE(NEW.city, '')) = ''
                    OR northstar_state_for_match(NEW.state) = ''
                  THEN ''
                  ELSE northstar_state_for_match(NEW.state)
                END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trg_company_identity_keys_au
        AFTER UPDATE OF external_record_no, company_name, website, legacy_phone,
            address, city, state ON companies
        BEGIN
            INSERT OR REPLACE INTO company_identity_keys (
                company_id, record_no, domain, norm_name, phone_digits, phone_last7,
                addr_norm, city_norm, state_norm
            )
            SELECT
                NEW.id,
                northstar_norm_record_no(NEW.external_record_no),
                northstar_domain(NEW.website),
                northstar_norm_name(NEW.company_name),
                northstar_digits_phone(NEW.legacy_phone),
                northstar_company_phone_last7(NEW.legacy_phone),
                CASE
                  WHEN TRIM(COALESCE(NEW.address, '')) = ''
                    OR TRIM(COALESCE(NEW.city, '')) = ''
                    OR northstar_state_for_match(NEW.state) = ''
                  THEN ''
                  ELSE northstar_norm_addr(NEW.address)
                END,
                CASE
                  WHEN TRIM(COALESCE(NEW.address, '')) = ''
                    OR TRIM(COALESCE(NEW.city, '')) = ''
                    OR northstar_state_for_match(NEW.state) = ''
                  THEN ''
                  ELSE northstar_city_norm(NEW.city)
                END,
                CASE
                  WHEN TRIM(COALESCE(NEW.address, '')) = ''
                    OR TRIM(COALESCE(NEW.city, '')) = ''
                    OR northstar_state_for_match(NEW.state) = ''
                  THEN ''
                  ELSE northstar_state_for_match(NEW.state)
                END;
        END
        """
    )
    stats["company_keys"] = backfill_company_identity_keys(conn)["keys_after"]

    if _table_exists(conn, "contacts"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_person_keys (
                contact_id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL,
                person_norm TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_contact_person_keys_norm
                ON contact_person_keys(person_norm)
                WHERE person_norm != ''
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_contact_person_keys_company
                ON contact_person_keys(company_id)
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_contact_person_keys_ai")
        conn.execute("DROP TRIGGER IF EXISTS trg_contact_person_keys_au")
        conn.execute(
            """
            CREATE TRIGGER trg_contact_person_keys_ai
            AFTER INSERT ON contacts
            BEGIN
                INSERT OR REPLACE INTO contact_person_keys (
                    contact_id, company_id, person_norm
                ) VALUES (
                    NEW.id,
                    NEW.company_id,
                    northstar_person_norm(NEW.first_name, NEW.last_name)
                );
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_contact_person_keys_au
            AFTER UPDATE OF first_name, last_name, company_id ON contacts
            BEGIN
                INSERT OR REPLACE INTO contact_person_keys (
                    contact_id, company_id, person_norm
                ) VALUES (
                    NEW.id,
                    NEW.company_id,
                    northstar_person_norm(NEW.first_name, NEW.last_name)
                );
            END
            """
        )
        stats["contact_keys"] = backfill_contact_person_keys(conn)["keys_after"]
    return stats
