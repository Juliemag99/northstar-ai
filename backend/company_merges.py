"""Company redirect / merge foundation (Phase 5E).

Creates empty company_merge_history and contact_merge_history, resolves
redirect chains, and plans a DATA CONSOLIDATION merge. Does not execute a
live company or contact merge.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from contact_phone import canonical_contact_phone
from crm_identity_keys import contact_person_norm, sqlite_norm_addr, sqlite_state_for_match

MAX_REDIRECT_HOPS = 16

RETIREMENT_POLICY_CODE = "D"
RETIREMENT_POLICY_LABEL = "Hard-delete loser after remap + permanent redirect"
RETIREMENT_POLICY_RATIONALE = (
    "After children are remapped and company_merge_history records the losing "
    "ID, the loser row is deleted. Matching, FTS, and company counts stay clean. "
    "No retirement-status column is required: the redirect table is the durable "
    "ID. Before deleting a previous survivor that is itself merged later, rewrite "
    "inbound redirects onto the new survivor (flatten) so survivor_company_id FKs "
    "remain valid. resolve_company_id still walks A→B→C chains if flattening is "
    "skipped. Policy A (keep both) never consolidates. Policy B (inactive flag) "
    "pollutes counts/matching unless every reader excludes merged rows. Policy C "
    "(archive table) duplicates the redirect. Phase 5E does not retire anyone."
)

CONTACT_REDIRECT_REQUIRED = True
CONTACT_REDIRECT_RATIONALE = (
    "Contact consolidation retires duplicate contact rows the same way company "
    "merge retires duplicate company rows. campaign_contacts, workflows, "
    "activities, and crm_import_results store contact_id. A contact_merge_history "
    "table with no FK on source_contact_id is required so old contact IDs keep "
    "resolving after the loser is deleted. Phase 5E creates the empty table only."
)

CONTACT_FIELD_KEYS = (
    "first_name",
    "last_name",
    "title",
    "email",
    "phone",
    "phone_extension",
    "alt_phone",
    "alt_phone_extension",
    "location",
    "location_id",
    "linkedin_url",
    "zoominfo_contact_id",
    "external_record_no",
    "source",
)

NOTE_COLUMN_NAMES = frozenset(
    {
        "notes",
        "note_text",
        "follow_up_notes",
        "tooling_notes",
        "fit_weighting_notes",
    }
)

SKIP_REF_TABLES = frozenset(
    {
        "sqlite_sequence",
        "company_merge_history",
        "contact_merge_history",
        "merge_execution_approvals",
        "company_duplicate_reviews",
        "company_duplicate_review_events",
        "company_duplicate_classifications",
        "search_fts",
        "search_fts_data",
        "search_fts_idx",
        "search_fts_content",
        "search_fts_docsize",
        "search_fts_config",
    }
)


class MergeRedirectError(ValueError):
    """Invalid redirect: self, cycle, missing survivor, or duplicate source."""


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_email(value: object | None) -> str:
    return _blank(value).lower()


def _norm_title(value: object | None) -> str:
    return " ".join(_blank(value).lower().split())


def _norm_note(value: object | None) -> str:
    return " ".join(_blank(value).lower().split())


def _norm_status(value: object | None) -> str:
    return " ".join(_blank(value).lower().split())


def existing_user_id(conn: sqlite3.Connection, user_id: object) -> int | None:
    """Return user_id only if that users row exists. Never invent an actor."""
    if user_id is None or str(user_id).strip() == "":
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    if not _table_exists(conn, "users"):
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


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def _company_snapshot(conn: sqlite3.Connection, company_id: int) -> dict[str, Any]:
    cols = _table_columns(conn, "companies")
    select: list[str] = []
    for name in (
        "id",
        "company_name",
        "external_record_no",
        "address",
        "city",
        "state",
        "zip",
        "website",
    ):
        if name in cols:
            select.append(name)
    if "phone" in cols:
        select.append("phone")
    elif "legacy_phone" in cols:
        select.append("legacy_phone AS phone")
    row = conn.execute(
        f"SELECT {', '.join(select)} FROM companies WHERE id = ?",
        (int(company_id),),
    ).fetchone()
    if row is None:
        return {}
    return _row_dict(row)


def ensure_company_merge_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent empty merge-history tables. No redirects, no remaps."""
    stats = {
        "created_company_merge_history": 0,
        "created_contact_merge_history": 0,
        "created_merge_execution_approvals": 0,
    }
    if not _table_exists(conn, "companies"):
        return stats
    if not _table_exists(conn, "company_merge_history"):
        conn.execute(
            """
            CREATE TABLE company_merge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_company_id INTEGER NOT NULL,
                survivor_company_id INTEGER NOT NULL,
                merged_at TEXT NOT NULL DEFAULT (datetime('now')),
                merged_by_user_id INTEGER,
                reason TEXT NOT NULL DEFAULT '',
                operation_id TEXT NOT NULL DEFAULT '',
                source_company_name TEXT NOT NULL DEFAULT '',
                source_external_record_no TEXT NOT NULL DEFAULT '',
                survivor_company_name TEXT NOT NULL DEFAULT '',
                survivor_external_record_no TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL DEFAULT '',
                CHECK (source_company_id != survivor_company_id),
                FOREIGN KEY (survivor_company_id) REFERENCES companies(id),
                FOREIGN KEY (merged_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        stats["created_company_merge_history"] = 1
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_company_merge_history_source
            ON company_merge_history(source_company_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_merge_history_survivor
            ON company_merge_history(survivor_company_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_merge_history_merged_at
            ON company_merge_history(merged_at)
        """
    )
    if _table_exists(conn, "contacts") and not _table_exists(conn, "contact_merge_history"):
        conn.execute(
            """
            CREATE TABLE contact_merge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_contact_id INTEGER NOT NULL,
                survivor_contact_id INTEGER NOT NULL,
                source_company_id INTEGER,
                survivor_company_id INTEGER,
                merged_at TEXT NOT NULL DEFAULT (datetime('now')),
                merged_by_user_id INTEGER,
                reason TEXT NOT NULL DEFAULT '',
                operation_id TEXT NOT NULL DEFAULT '',
                source_contact_name TEXT NOT NULL DEFAULT '',
                source_external_record_no TEXT NOT NULL DEFAULT '',
                survivor_contact_name TEXT NOT NULL DEFAULT '',
                survivor_external_record_no TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL DEFAULT '',
                CHECK (source_contact_id != survivor_contact_id),
                FOREIGN KEY (survivor_contact_id) REFERENCES contacts(id),
                FOREIGN KEY (survivor_company_id) REFERENCES companies(id),
                FOREIGN KEY (merged_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        stats["created_contact_merge_history"] = 1
    if _table_exists(conn, "contact_merge_history"):
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_merge_history_source
                ON contact_merge_history(source_contact_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_contact_merge_history_survivor
                ON contact_merge_history(survivor_contact_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_contact_merge_history_survivor_company
                ON contact_merge_history(survivor_company_id)
            """
        )
    from company_merge_approvals import ensure_merge_approval_schema

    approval_stats = ensure_merge_approval_schema(conn)
    stats["created_merge_execution_approvals"] = int(
        approval_stats.get("created_merge_execution_approvals") or 0
    )
    return stats


def _walk_redirect(
    conn: sqlite3.Connection,
    table: str,
    source_col: str,
    survivor_col: str,
    start_id: int,
    *,
    max_hops: int = MAX_REDIRECT_HOPS,
) -> int:
    if not _table_exists(conn, table):
        return int(start_id)
    current = int(start_id)
    seen: set[int] = set()
    for _ in range(int(max_hops)):
        if current in seen:
            raise MergeRedirectError(
                f"Redirect cycle involving {table} id {int(start_id)}."
            )
        seen.add(current)
        row = conn.execute(
            f"SELECT {survivor_col} FROM {table} WHERE {source_col} = ?",
            (current,),
        ).fetchone()
        if row is None:
            return current
        nxt = int(row[0])
        if nxt == current:
            raise MergeRedirectError(f"Self-redirect stored for {table} id {current}.")
        current = nxt
    raise MergeRedirectError(
        f"Redirect chain exceeded {max_hops} hops from {table} id {int(start_id)}."
    )


def resolve_company_id(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    max_hops: int = MAX_REDIRECT_HOPS,
) -> int:
    """Follow company_merge_history until a non-redirected id. Self = identity."""
    return _walk_redirect(
        conn,
        "company_merge_history",
        "source_company_id",
        "survivor_company_id",
        int(company_id),
        max_hops=max_hops,
    )


def resolve_contact_id(
    conn: sqlite3.Connection,
    contact_id: int,
    *,
    max_hops: int = MAX_REDIRECT_HOPS,
) -> int:
    """Follow contact_merge_history until a non-redirected id. Self = identity."""
    return _walk_redirect(
        conn,
        "contact_merge_history",
        "source_contact_id",
        "survivor_contact_id",
        int(contact_id),
        max_hops=max_hops,
    )


def _validate_redirect_pair(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    survivor_id: int,
    resolve_fn,
    entity: str,
    survivor_table: str,
) -> None:
    src = int(source_id)
    dst = int(survivor_id)
    if src == dst:
        raise MergeRedirectError(f"Refusing self-redirect for {entity} {src}.")
    exists = conn.execute(
        f"SELECT 1 FROM {survivor_table} WHERE id = ? LIMIT 1",
        (dst,),
    ).fetchone()
    if exists is None:
        raise MergeRedirectError(
            f"Survivor {entity} {dst} does not exist."
        )
    try:
        canonical = resolve_fn(conn, dst)
    except MergeRedirectError as exc:
        raise MergeRedirectError(
            f"Survivor {entity} {dst} redirect chain is invalid: {exc}"
        ) from exc
    if canonical == src:
        raise MergeRedirectError(
            f"Redirect {src} → {dst} would create a cycle for {entity}."
        )
    try:
        walked = resolve_fn(conn, src)
    except MergeRedirectError:
        walked = src
    if walked != src and walked != dst and walked != canonical:
        raise MergeRedirectError(
            f"{entity} {src} is already redirected to {walked}."
        )


def record_company_merge_redirect(
    conn: sqlite3.Connection,
    *,
    source_company_id: int,
    survivor_company_id: int,
    reason: str = "",
    operation_id: str = "",
    merged_by_user_id: int | None = None,
    summary: dict[str, Any] | None = None,
) -> int:
    """Insert a company redirect. Does not remap children or delete the loser."""
    ensure_company_merge_schema(conn)
    src = int(source_company_id)
    dst = int(survivor_company_id)
    _validate_redirect_pair(
        conn,
        source_id=src,
        survivor_id=dst,
        resolve_fn=resolve_company_id,
        entity="company",
        survivor_table="companies",
    )
    existing = conn.execute(
        """
        SELECT survivor_company_id FROM company_merge_history
        WHERE source_company_id = ?
        """,
        (src,),
    ).fetchone()
    if existing is not None:
        if int(existing[0]) == dst:
            return int(
                conn.execute(
                    "SELECT id FROM company_merge_history WHERE source_company_id = ?",
                    (src,),
                ).fetchone()[0]
            )
        raise MergeRedirectError(
            f"Company {src} already redirects to {int(existing[0])}."
        )
    source_row = _company_snapshot(conn, src)
    survivor_row = _company_snapshot(conn, dst)
    cur = conn.execute(
        """
        INSERT INTO company_merge_history (
            source_company_id, survivor_company_id, merged_by_user_id, reason,
            operation_id, source_company_name, source_external_record_no,
            survivor_company_name, survivor_external_record_no, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            src,
            dst,
            merged_by_user_id,
            _blank(reason),
            _blank(operation_id),
            _blank(source_row.get("company_name")),
            _blank(source_row.get("external_record_no")),
            _blank(survivor_row.get("company_name")),
            _blank(survivor_row.get("external_record_no")),
            json.dumps(summary or {}, sort_keys=True),
        ),
    )
    return int(cur.lastrowid)


def flatten_inbound_contact_redirects(
    conn: sqlite3.Connection,
    *,
    old_survivor_contact_id: int,
    new_survivor_contact_id: int,
) -> int:
    """Rewrite inbound contact redirects onto the new survivor before retiring the old one.

    Prevents FK breaks on contact_merge_history.survivor_contact_id and dangling chains.
    """
    old = int(old_survivor_contact_id)
    new = int(new_survivor_contact_id)
    if old == new:
        raise MergeRedirectError(f"Refusing to flatten contact {old} onto itself.")
    exists = conn.execute(
        "SELECT 1 FROM contacts WHERE id = ? LIMIT 1", (new,)
    ).fetchone()
    if exists is None:
        raise MergeRedirectError(
            f"Cannot flatten redirects onto missing contact {new}."
        )
    if resolve_contact_id(conn, new) == old:
        raise MergeRedirectError(
            f"Flatten {old} → {new} would create a contact redirect cycle."
        )
    cur = conn.execute(
        """
        UPDATE contact_merge_history
        SET survivor_contact_id = ?
        WHERE survivor_contact_id = ?
          AND source_contact_id != ?
        """,
        (new, old, new),
    )
    return int(cur.rowcount or 0)


def record_contact_merge_redirect(
    conn: sqlite3.Connection,
    *,
    source_contact_id: int,
    survivor_contact_id: int,
    source_company_id: int | None = None,
    survivor_company_id: int | None = None,
    reason: str = "",
    operation_id: str = "",
    merged_by_user_id: int | None = None,
    summary: dict[str, Any] | None = None,
) -> int:
    """Insert a contact redirect. Does not remap children or delete the loser."""
    ensure_company_merge_schema(conn)
    src = int(source_contact_id)
    dst = int(survivor_contact_id)
    _validate_redirect_pair(
        conn,
        source_id=src,
        survivor_id=dst,
        resolve_fn=resolve_contact_id,
        entity="contact",
        survivor_table="contacts",
    )
    existing = conn.execute(
        """
        SELECT survivor_contact_id FROM contact_merge_history
        WHERE source_contact_id = ?
        """,
        (src,),
    ).fetchone()
    if existing is not None:
        if int(existing[0]) == dst:
            return int(
                conn.execute(
                    "SELECT id FROM contact_merge_history WHERE source_contact_id = ?",
                    (src,),
                ).fetchone()[0]
            )
        raise MergeRedirectError(
            f"Contact {src} already redirects to {int(existing[0])}."
        )

    def _contact_name(cid: int) -> tuple[str, str, int | None]:
        row = conn.execute(
            """
            SELECT first_name, last_name, external_record_no, company_id
            FROM contacts WHERE id = ?
            """,
            (cid,),
        ).fetchone()
        if row is None:
            return "", "", None
        name = " ".join(p for p in (_blank(row[0]), _blank(row[1])) if p)
        return name, _blank(row[2]), int(row[3]) if row[3] is not None else None

    src_name, src_rn, src_co = _contact_name(src)
    dst_name, dst_rn, dst_co = _contact_name(dst)
    cur = conn.execute(
        """
        INSERT INTO contact_merge_history (
            source_contact_id, survivor_contact_id, source_company_id,
            survivor_company_id, merged_by_user_id, reason, operation_id,
            source_contact_name, source_external_record_no,
            survivor_contact_name, survivor_external_record_no, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            src,
            dst,
            source_company_id if source_company_id is not None else src_co,
            survivor_company_id if survivor_company_id is not None else dst_co,
            existing_user_id(conn, merged_by_user_id),
            _blank(reason),
            _blank(operation_id),
            src_name,
            src_rn,
            dst_name,
            dst_rn,
            json.dumps(summary or {}, sort_keys=True),
        ),
    )
    return int(cur.lastrowid)


def discover_company_id_refs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Inspect live schema for company_id columns, uniqueness, and FKs."""
    rows = conn.execute(
        """
        SELECT name, type, sql FROM sqlite_master
        WHERE type IN ('table', 'index')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    index_sql = {str(r[0]): _blank(r[2]) for r in rows if str(r[1]) == "index"}
    refs: list[dict[str, Any]] = []
    for r in rows:
        if str(r[1]) != "table":
            continue
        table = str(r[0])
        if table in SKIP_REF_TABLES:
            continue
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = [str(c[1]) for c in cols]
        if "company_id" not in col_names:
            continue
        fks = [
            {
                "from": str(fk[3]),
                "to_table": str(fk[2]),
                "to_col": str(fk[4]),
                "on_delete": _blank(fk[6]) or "NO ACTION",
            }
            for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            if str(fk[3]) == "company_id"
        ]
        unique: list[dict[str, Any]] = []
        for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
            if not int(idx[2]):
                continue
            idx_name = str(idx[1])
            idx_cols = [
                str(info[2])
                for info in conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
            ]
            if "company_id" not in idx_cols:
                continue
            unique.append(
                {
                    "index": idx_name,
                    "columns": idx_cols,
                    "sql": index_sql.get(idx_name, ""),
                }
            )
        count = int(
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE company_id IS NOT NULL").fetchone()[0]
        )
        refs.append(
            {
                "table": table,
                "column": "company_id",
                "row_count": count,
                "foreign_keys": fks,
                "unique_indexes": unique,
                "uniqueness_risk": bool(unique),
                "fts": table.startswith("search_fts"),
            }
        )
    return refs


def _city_token(value: object | None) -> str:
    text = _blank(value).lower()
    if not text:
        return ""
    text = re.split(r",", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", text)
    text = re.sub(r"\s+\b[a-z]{2}\b$", "", text).strip()
    return text


def _phone_keys(phone: object | None, alt: object | None) -> tuple[set[str], set[str]]:
    nanp: set[str] = set()
    last7: set[str] = set()
    for raw in (phone, alt):
        n10, l7 = canonical_contact_phone(_blank(raw))
        if n10:
            nanp.add(n10)
        if l7:
            last7.add(l7)
    return nanp, last7


def _company_switchboard_nanp(
    conn: sqlite3.Connection, *company_ids: int
) -> set[str]:
    numbers: set[str] = set()
    cols = _table_columns(conn, "companies")
    phone_cols = [
        c
        for c in ("phone", "legacy_phone", "legacy_alt_phone", "legacy_mobile")
        if c in cols
    ]
    if phone_cols:
        for cid in company_ids:
            row = conn.execute(
                f"SELECT {', '.join(phone_cols)} FROM companies WHERE id = ?",
                (int(cid),),
            ).fetchone()
            if row is None:
                continue
            payload = _row_dict(row)
            for col in phone_cols:
                n10, _ = canonical_contact_phone(_blank(payload.get(col)))
                if n10:
                    numbers.add(n10)
    if _table_exists(conn, "company_locations"):
        for cid in company_ids:
            for row in conn.execute(
                "SELECT phone, alt_phone FROM company_locations WHERE company_id = ?",
                (int(cid),),
            ).fetchall():
                payload = _row_dict(row)
                for col in ("phone", "alt_phone"):
                    n10, _ = canonical_contact_phone(_blank(payload.get(col)))
                    if n10:
                        numbers.add(n10)
    return numbers


def _title_tokens(value: object | None) -> set[str]:
    return {
        tok
        for tok in re.split(r"[^a-z0-9]+", _norm_title(value))
        if tok and tok not in {"and", "the", "of"}
    }


def _first_stem(value: object | None) -> str:
    text = _blank(value).lower()
    if not text:
        return ""
    return re.split(r"[^a-z0-9]+", text)[0]


def _names_compatible(source: dict[str, Any], survivor: dict[str, Any]) -> tuple[bool, str]:
    person_a = contact_person_norm(source.get("first_name"), source.get("last_name"))
    person_b = contact_person_norm(survivor.get("first_name"), survivor.get("last_name"))
    if person_a and person_a == person_b:
        return True, "first_last_plus_company"
    last_a = _blank(source.get("last_name")).lower()
    last_b = _blank(survivor.get("last_name")).lower()
    first_a = _first_stem(source.get("first_name"))
    first_b = _first_stem(survivor.get("first_name"))
    if last_a and last_a == last_b and first_a and first_b:
        if first_a == first_b:
            return True, "first_last_plus_company"
        shorter, longer = sorted((first_a, first_b), key=len)
        if len(shorter) >= 3 and longer.startswith(shorter):
            return True, "first_stem_last_plus_company"
    return False, ""


def _contact_identity(
    row: dict[str, Any], *, company_nanp: set[str] | None = None
) -> dict[str, Any]:
    email = _norm_email(row.get("email"))
    nanp, last7 = _phone_keys(row.get("phone"), row.get("alt_phone"))
    org = company_nanp or set()
    personal = set(nanp) - org
    person = contact_person_norm(row.get("first_name"), row.get("last_name"))
    title = _norm_title(row.get("title"))
    rn = _blank(row.get("external_record_no"))
    zoom = _blank(row.get("zoominfo_contact_id"))
    return {
        "email": email,
        "nanp10": personal,
        "org_nanp10": nanp & org,
        "last7": last7,
        "person_norm": person,
        "title_norm": title,
        "title_tokens": _title_tokens(row.get("title")),
        "external_record_no": rn,
        "zoominfo_contact_id": zoom,
    }


def _identifier_conflict(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    if a["email"] and b["email"] and a["email"] != b["email"]:
        conflicts.append("email")
    if a["nanp10"] and b["nanp10"] and a["nanp10"].isdisjoint(b["nanp10"]):
        conflicts.append("phone")
    return conflicts


def classify_contact_pair(
    source: dict[str, Any],
    survivor: dict[str, Any],
    *,
    company_nanp: set[str] | None = None,
    company_record_nos: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Return (MOVE|MERGE|POSSIBLE|KEEP_SEPARATE, evidence reasons)."""
    a = _contact_identity(source, company_nanp=company_nanp)
    b = _contact_identity(survivor, company_nanp=company_nanp)
    reasons: list[str] = []
    if a["email"] and a["email"] == b["email"]:
        reasons.append("exact_normalized_email")
    shared_phone = a["nanp10"] & b["nanp10"]
    if shared_phone:
        reasons.append("exact_normalized_nanp10")
    if a["org_nanp10"] and a["org_nanp10"] == b["org_nanp10"] and not shared_phone:
        reasons.append("shared_company_switchboard_ignored")
    if (
        a["external_record_no"]
        and a["external_record_no"] == b["external_record_no"]
        and a["external_record_no"] not in (company_record_nos or set())
    ):
        reasons.append("source_record_no")
    if (
        a["zoominfo_contact_id"]
        and a["zoominfo_contact_id"] == b["zoominfo_contact_id"]
    ):
        reasons.append("zoominfo_contact_id")
    name_match, name_reason = _names_compatible(source, survivor)
    title_match = bool(a["title_norm"] and a["title_norm"] == b["title_norm"])
    title_overlap = bool(a["title_tokens"] and a["title_tokens"] & b["title_tokens"])
    if name_match:
        reasons.append(name_reason)
    if name_match and (title_match or title_overlap):
        reasons.append("first_last_plus_title")
    if name_match and (a["email"] and a["email"] == b["email"] or shared_phone):
        reasons.append("first_last_plus_email_or_phone")

    conflicts = _identifier_conflict(a, b)
    strong = {
        "exact_normalized_email",
        "exact_normalized_nanp10",
        "source_record_no",
        "zoominfo_contact_id",
        "first_last_plus_email_or_phone",
    }
    if strong.intersection(reasons):
        return "MERGE", reasons
    if name_match and (title_match or title_overlap) and not conflicts:
        return "MERGE", reasons
    if name_match and "email" in conflicts and "phone" in conflicts:
        return "KEEP_SEPARATE", reasons + ["conflicting_email_and_phone"]
    if name_match:
        return "POSSIBLE", reasons + ([f"conflict:{c}" for c in conflicts] if conflicts else [])
    last_only = (
        _blank(source.get("last_name")).lower()
        and _blank(source.get("last_name")).lower() == _blank(survivor.get("last_name")).lower()
        and _first_stem(source.get("first_name")) != _first_stem(survivor.get("first_name"))
    )
    if last_only and (title_match or title_overlap):
        return "POSSIBLE", reasons + ["same_last_name_and_title"]
    if reasons and "shared_company_switchboard_ignored" not in reasons:
        return "POSSIBLE", reasons
    return "MOVE", [r for r in reasons if r == "shared_company_switchboard_ignored"] or []


def _field_conflicts(source: dict[str, Any], survivor: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in CONTACT_FIELD_KEYS:
        sv = source.get(key)
        tv = survivor.get(key)
        s_blank = sv is None or _blank(sv) == ""
        t_blank = tv is None or _blank(tv) == ""
        if s_blank or t_blank:
            continue
        if key in ("email",):
            if _norm_email(sv) != _norm_email(tv):
                out.append({"field": key, "source": _blank(sv), "survivor": _blank(tv)})
        elif key in ("phone", "alt_phone"):
            s10, _ = canonical_contact_phone(_blank(sv))
            t10, _ = canonical_contact_phone(_blank(tv))
            if s10 and t10 and s10 != t10:
                out.append({"field": key, "source": _blank(sv), "survivor": _blank(tv)})
            elif _blank(sv) != _blank(tv) and not (s10 and t10):
                out.append({"field": key, "source": _blank(sv), "survivor": _blank(tv)})
        elif _blank(sv) != _blank(tv):
            out.append({"field": key, "source": _blank(sv), "survivor": _blank(tv)})
    return out


def _load_contacts(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "contacts"):
        return []
    rows = conn.execute(
        """
        SELECT id, company_id, external_record_no, first_name, last_name, title,
               phone, alt_phone, phone_extension, alt_phone_extension, email,
               linkedin_url, location, location_id, zoominfo_contact_id, source,
               created_at
        FROM contacts WHERE company_id = ?
        ORDER BY last_name, first_name, id
        """,
        (int(company_id),),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _contact_campaigns(conn: sqlite3.Connection, contact_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "campaign_contacts"):
        return []
    rows = conn.execute(
        """
        SELECT campaign_id, client_id, notes, created_at
        FROM campaign_contacts WHERE contact_id = ?
        ORDER BY campaign_id
        """,
        (int(contact_id),),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _contact_workflows(conn: sqlite3.Connection, contact_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "contact_client_workflows"):
        return []
    rows = conn.execute(
        """
        SELECT client_id, relationship_id, status, assigned_user_id, next_action,
               follow_up_date
        FROM contact_client_workflows WHERE contact_id = ?
        """,
        (int(contact_id),),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _plan_contacts(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    source_contacts = _load_contacts(conn, source_id)
    survivor_contacts = _load_contacts(conn, survivor_id)
    company_nanp = _company_switchboard_nanp(conn, source_id, survivor_id)
    company_record_nos = {
        _blank(_company_snapshot(conn, source_id).get("external_record_no")),
        _blank(_company_snapshot(conn, survivor_id).get("external_record_no")),
    } - {""}
    classifications: list[dict[str, Any]] = []
    used_survivor: set[int] = set()

    for src in source_contacts:
        best: tuple[int, str, list[str], dict[str, Any]] | None = None
        rank = {"MERGE": 3, "POSSIBLE": 2, "KEEP_SEPARATE": 1, "MOVE": 0}
        for tgt in survivor_contacts:
            klass, reasons = classify_contact_pair(
                src,
                tgt,
                company_nanp=company_nanp,
                company_record_nos=company_record_nos,
            )
            if klass == "MOVE":
                continue
            score = rank[klass]
            if best is None or score > best[0]:
                best = (score, klass, reasons, tgt)
        if best is None:
            classifications.append(
                {
                    "action": "MOVE",
                    "source_contact_id": int(src["id"]),
                    "survivor_contact_id": None,
                    "source_name": f"{_blank(src.get('first_name'))} {_blank(src.get('last_name'))}".strip(),
                    "survivor_name": "",
                    "reasons": ["unique_contact"],
                    "field_conflicts": [],
                    "campaigns": _contact_campaigns(conn, int(src["id"])),
                    "workflows": _contact_workflows(conn, int(src["id"])),
                    "source": src,
                    "survivor": None,
                }
            )
            continue
        _, klass, reasons, tgt = best
        if klass in {"MERGE", "POSSIBLE"} and int(tgt["id"]) in used_survivor:
            klass = "POSSIBLE"
            reasons = list(reasons) + ["ambiguous_survivor_contact"]
        if klass == "MERGE":
            used_survivor.add(int(tgt["id"]))
        src_camps = _contact_campaigns(conn, int(src["id"]))
        tgt_camps = _contact_campaigns(conn, int(tgt["id"]))
        src_camp_ids = {int(c["campaign_id"]) for c in src_camps}
        tgt_camp_ids = {int(c["campaign_id"]) for c in tgt_camps}
        camp_conflicts = []
        for cid in src_camp_ids & tgt_camp_ids:
            s_note = next(
                (_blank(c.get("notes")) for c in src_camps if int(c["campaign_id"]) == cid),
                "",
            )
            t_note = next(
                (_blank(c.get("notes")) for c in tgt_camps if int(c["campaign_id"]) == cid),
                "",
            )
            if s_note and t_note and _norm_note(s_note) != _norm_note(t_note):
                camp_conflicts.append(
                    {"campaign_id": cid, "source_notes": s_note, "survivor_notes": t_note}
                )
        src_wf = _contact_workflows(conn, int(src["id"]))
        tgt_wf = _contact_workflows(conn, int(tgt["id"]))
        src_wf_clients = {int(w["client_id"]) for w in src_wf}
        tgt_wf_clients = {int(w["client_id"]) for w in tgt_wf}
        wf_conflicts = []
        for client_id in src_wf_clients & tgt_wf_clients:
            s_row = next(w for w in src_wf if int(w["client_id"]) == client_id)
            t_row = next(w for w in tgt_wf if int(w["client_id"]) == client_id)
            if _norm_status(s_row.get("status")) != _norm_status(t_row.get("status")):
                if _blank(s_row.get("status")) and _blank(t_row.get("status")):
                    wf_conflicts.append(
                        {
                            "client_id": client_id,
                            "field": "status",
                            "source": _blank(s_row.get("status")),
                            "survivor": _blank(t_row.get("status")),
                        }
                    )
        classifications.append(
            {
                "action": klass,
                "source_contact_id": int(src["id"]),
                "survivor_contact_id": int(tgt["id"]),
                "source_name": f"{_blank(src.get('first_name'))} {_blank(src.get('last_name'))}".strip(),
                "survivor_name": f"{_blank(tgt.get('first_name'))} {_blank(tgt.get('last_name'))}".strip(),
                "reasons": reasons,
                "field_conflicts": _field_conflicts(src, tgt) if klass != "KEEP_SEPARATE" else [],
                "campaign_union": sorted(src_camp_ids | tgt_camp_ids),
                "campaign_conflicts": camp_conflicts,
                "workflow_conflicts": wf_conflicts,
                "campaigns": src_camps,
                "workflows": src_wf,
                "source": src,
                "survivor": tgt,
            }
        )

    move_n = sum(1 for c in classifications if c["action"] == "MOVE")
    merge_n = sum(1 for c in classifications if c["action"] == "MERGE")
    possible_n = sum(1 for c in classifications if c["action"] == "POSSIBLE")
    keep_n = sum(1 for c in classifications if c["action"] == "KEEP_SEPARATE")
    projected = len(survivor_contacts) + move_n + keep_n
    return {
        "source_contact_count": len(source_contacts),
        "survivor_contact_count": len(survivor_contacts),
        "unique_contacts_to_move": move_n,
        "high_confidence_duplicates_to_consolidate": merge_n,
        "possible_duplicates_requiring_review": possible_n,
        "distinct_contacts_to_keep_separate": keep_n,
        "projected_survivor_contact_count": projected,
        "contact_field_conflicts": [
            {
                "source_contact_id": c["source_contact_id"],
                "survivor_contact_id": c["survivor_contact_id"],
                "conflicts": c["field_conflicts"],
            }
            for c in classifications
            if c.get("field_conflicts")
        ],
        "contact_campaign_conflicts": [
            {
                "source_contact_id": c["source_contact_id"],
                "survivor_contact_id": c["survivor_contact_id"],
                "conflicts": c.get("campaign_conflicts") or [],
            }
            for c in classifications
            if c.get("campaign_conflicts")
        ],
        "contact_workflow_conflicts": [
            {
                "source_contact_id": c["source_contact_id"],
                "survivor_contact_id": c["survivor_contact_id"],
                "conflicts": c.get("workflow_conflicts") or [],
            }
            for c in classifications
            if c.get("workflow_conflicts")
        ],
        "classifications": classifications,
        "source_contacts": source_contacts,
        "survivor_contacts": survivor_contacts,
        "future_behavior": {
            "preserve_best_nonblank": True,
            "never_replace_populated_with_blank": True,
            "conflicting_email_or_phone": "HUMAN REVIEW — do not silently choose",
            "campaign_memberships": "union",
            "history_and_activities": "survive on surviving contact_id",
            "contact_merge_history": CONTACT_REDIRECT_REQUIRED,
        },
    }


def _discover_note_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in tables:
        table = str(name)
        if table in SKIP_REF_TABLES:
            continue
        cols = _table_columns(conn, table)
        note_cols = sorted(cols & NOTE_COLUMN_NAMES)
        if not note_cols:
            continue
        scope = []
        if "client_id" in cols:
            scope.append("client")
        if "company_id" in cols:
            scope.append("company")
        if "contact_id" in cols:
            scope.append("contact")
        sources.append(
            {
                "table": table,
                "note_columns": note_cols,
                "scope": scope or ["unknown"],
                "has_company_id": "company_id" in cols,
                "has_contact_id": "contact_id" in cols,
                "has_client_id": "client_id" in cols,
                "has_created_at": "created_at" in cols or "event_at" in cols or "activity_at" in cols,
                "has_author": any(
                    c in cols for c in ("author", "created_by", "attribution", "user_id")
                ),
            }
        )
    return sources


def _collect_row_notes(
    conn: sqlite3.Connection,
    table: str,
    note_col: str,
    where_sql: str,
    params: tuple[Any, ...],
    *,
    extra_cols: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    cols = _table_columns(conn, table)
    if note_col not in cols:
        return []
    select_cols = ["rowid AS _rowid", note_col]
    for extra in extra_cols:
        if extra in cols:
            select_cols.append(extra)
    sql = f"SELECT {', '.join(select_cols)} FROM {table} WHERE {where_sql}"
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, params).fetchall():
        payload = _row_dict(row)
        text = payload.get(note_col)
        if _blank(text) == "":
            continue
        out.append(
            {
                "table": table,
                "column": note_col,
                "rowid": payload.get("_rowid"),
                "text": _blank(text),
                "norm": _norm_note(text),
                "client_id": payload.get("client_id"),
                "contact_id": payload.get("contact_id"),
                "created_at": payload.get("created_at")
                or payload.get("event_at")
                or payload.get("activity_at")
                or "",
                "author": payload.get("author")
                or payload.get("created_by")
                or payload.get("attribution")
                or "",
                "event_hash": payload.get("event_hash") or "",
            }
        )
    return out


def _plan_notes(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    sources = _discover_note_sources(conn)
    extra = (
        "client_id",
        "contact_id",
        "created_at",
        "event_at",
        "activity_at",
        "author",
        "created_by",
        "attribution",
        "event_hash",
        "user_id",
    )
    source_notes: list[dict[str, Any]] = []
    survivor_notes: list[dict[str, Any]] = []
    for spec in sources:
        table = spec["table"]
        cols = _table_columns(conn, table)
        for note_col in spec["note_columns"]:
            if "company_id" in cols:
                source_notes.extend(
                    _collect_row_notes(
                        conn,
                        table,
                        note_col,
                        "company_id = ?",
                        (source_id,),
                        extra_cols=extra,
                    )
                )
                survivor_notes.extend(
                    _collect_row_notes(
                        conn,
                        table,
                        note_col,
                        "company_id = ?",
                        (survivor_id,),
                        extra_cols=extra,
                    )
                )
    # Contact-scoped notes whose contacts live on either company.
    src_contact_ids = [int(c["id"]) for c in _load_contacts(conn, source_id)]
    tgt_contact_ids = [int(c["id"]) for c in _load_contacts(conn, survivor_id)]
    for table, note_col in (("campaign_contacts", "notes"),):
        if not _table_exists(conn, table):
            continue
        for cid in src_contact_ids:
            source_notes.extend(
                _collect_row_notes(
                    conn, table, note_col, "contact_id = ?", (cid,), extra_cols=extra
                )
            )
        for cid in tgt_contact_ids:
            survivor_notes.extend(
                _collect_row_notes(
                    conn, table, note_col, "contact_id = ?", (cid,), extra_cols=extra
                )
            )

    def _key(note: dict[str, Any]) -> tuple:
        return (
            note["table"],
            note["column"],
            note.get("client_id"),
            note["norm"],
        )

    survivor_keys = {_key(n) for n in survivor_notes}
    duplicates: list[dict[str, Any]] = []
    distinct: list[dict[str, Any]] = []
    for note in source_notes:
        if _key(note) in survivor_keys:
            duplicates.append(note)
        else:
            distinct.append(note)

    client_scoped = [
        n for n in source_notes + survivor_notes if n.get("client_id") not in (None, "")
    ]
    contact_scoped = [
        n for n in source_notes + survivor_notes if n.get("contact_id") not in (None, "")
    ]
    combined_fields = [
        s
        for s in sources
        if s["table"]
        in {"client_company_relationships", "companies", "contacts", "company_client_fit"}
        and "notes" in s["note_columns"]
    ]
    return {
        "note_bearing_sources": sources,
        "source_company_notes": len(source_notes),
        "survivor_company_notes": len(survivor_notes),
        "distinct_notes_to_preserve": len(distinct),
        "exact_normalized_duplicates": len(duplicates),
        "client_scoped_notes": len(client_scoped),
        "contact_scoped_notes": len(contact_scoped),
        "source_note_rows": source_notes,
        "survivor_note_rows": survivor_notes,
        "distinct_note_rows": distinct,
        "duplicate_note_rows": duplicates,
        "combined_text_fields": combined_fields,
        "append_without_duplicating": (
            "KEEP EXISTING NOTES AND ADD NEW DISTINCT NOTES. "
            "Normalize whitespace/case only for duplicate detection. "
            "Do not deduplicate merely similar text. "
            "Never fold client-scoped notes into a global company note. "
            "Never overwrite one note with another."
        ),
        "final_consolidated_preview": survivor_notes + distinct,
    }


def _plan_ccrs(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    if not _table_exists(conn, "client_company_relationships"):
        return {"pairs": [], "verdict_hints": []}
    sql = """
        SELECT r.id, r.client_id, r.company_id, r.external_record_no, r.status,
               r.assigned_user_id, r.priority, r.next_action, r.follow_up_date,
               r.notes, r.is_hot, r.location_id, c.name AS client_name
        FROM client_company_relationships r
        JOIN clients c ON c.id = r.client_id
        WHERE r.company_id IN (?, ?)
        ORDER BY r.client_id, r.company_id
    """
    rows = [_row_dict(r) for r in conn.execute(sql, (source_id, survivor_id)).fetchall()]
    by_client: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_client.setdefault(int(row["client_id"]), {})
        role = "source" if int(row["company_id"]) == int(source_id) else "survivor"
        by_client[int(row["client_id"])][role] = row
    pairs: list[dict[str, Any]] = []
    hints: list[str] = []
    for client_id, sides in by_client.items():
        src = sides.get("source")
        tgt = sides.get("survivor")
        if src and tgt:
            src_status = _blank(src.get("status"))
            tgt_status = _blank(tgt.get("status"))
            status_conflict = (
                bool(src_status)
                and bool(tgt_status)
                and _norm_status(src_status) != _norm_status(tgt_status)
            )
            rn_src = _blank(src.get("external_record_no"))
            rn_tgt = _blank(tgt.get("external_record_no"))
            notes_src = _blank(src.get("notes"))
            notes_tgt = _blank(tgt.get("notes"))
            note_dup = bool(notes_src) and _norm_note(notes_src) == _norm_note(notes_tgt)
            note_plan = "keep_survivor"
            if notes_src and not notes_tgt:
                note_plan = "copy_source"
            elif notes_src and notes_tgt and not note_dup:
                note_plan = "append_distinct"
            elif note_dup:
                note_plan = "store_once"
            decision = "SAFE CONSOLIDATE"
            if status_conflict:
                decision = "HUMAN REVIEW"
                hints.append("MERGE BLOCKED")
            pair = {
                "client_id": client_id,
                "client_name": src.get("client_name") or tgt.get("client_name"),
                "same_client": True,
                "decision": decision,
                "source_ccr_id": src["id"],
                "survivor_ccr_id": tgt["id"],
                "source_status": src_status,
                "survivor_status": tgt_status,
                "status_conflict": status_conflict,
                "source_assigned_user_id": src.get("assigned_user_id"),
                "survivor_assigned_user_id": tgt.get("assigned_user_id"),
                "source_is_hot": src.get("is_hot"),
                "survivor_is_hot": tgt.get("is_hot"),
                "source_next_action": _blank(src.get("next_action")),
                "survivor_next_action": _blank(tgt.get("next_action")),
                "source_follow_up_date": src.get("follow_up_date"),
                "survivor_follow_up_date": tgt.get("follow_up_date"),
                "source_rn": rn_src,
                "survivor_rn": rn_tgt,
                "rn_plan": (
                    "preserve_both_via_source_identities"
                    if rn_src and rn_tgt and rn_src != rn_tgt
                    else "keep_nonblank"
                ),
                "note_plan": note_plan,
                "auto_resolve_status": False,
            }
            pairs.append(pair)
        elif src and not tgt:
            pairs.append(
                {
                    "client_id": client_id,
                    "client_name": src.get("client_name"),
                    "same_client": False,
                    "decision": "KEEP INDEPENDENT — remap CCR to survivor org",
                    "source_ccr_id": src["id"],
                    "survivor_ccr_id": None,
                    "source_status": _blank(src.get("status")),
                    "survivor_status": "",
                    "status_conflict": False,
                    "source_rn": _blank(src.get("external_record_no")),
                    "survivor_rn": "",
                    "rn_plan": "move_with_ccr",
                    "note_plan": "move",
                    "auto_resolve_status": False,
                }
            )
        elif tgt and not src:
            pairs.append(
                {
                    "client_id": client_id,
                    "client_name": tgt.get("client_name"),
                    "same_client": False,
                    "decision": "KEEP SURVIVOR CCR UNCHANGED",
                    "source_ccr_id": None,
                    "survivor_ccr_id": tgt["id"],
                    "source_status": "",
                    "survivor_status": _blank(tgt.get("status")),
                    "status_conflict": False,
                    "source_rn": "",
                    "survivor_rn": _blank(tgt.get("external_record_no")),
                    "rn_plan": "keep_survivor",
                    "note_plan": "keep_survivor",
                    "auto_resolve_status": False,
                }
            )
    return {"pairs": pairs, "verdict_hints": hints, "rows": rows}


def _plan_locations(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    def _locs(cid: int) -> list[dict[str, Any]]:
        if not _table_exists(conn, "company_locations"):
            return []
        return [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT id, company_id, location_name, location_type, address, city,
                       state, zip, phone, is_headquarters, is_primary
                FROM company_locations WHERE company_id = ?
                """,
                (cid,),
            ).fetchall()
        ]

    def _site_key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (
            sqlite_norm_addr(row.get("address")),
            _city_token(row.get("city")),
            sqlite_state_for_match(row.get("state")),
        )

    source_locs = _locs(source_id)
    survivor_locs = _locs(survivor_id)
    src_company = _company_snapshot(conn, source_id)
    tgt_company = _company_snapshot(conn, survivor_id)
    comparisons: list[dict[str, Any]] = []

    def _classify_sites(a: dict[str, Any], b: dict[str, Any]) -> str:
        ka, kb = _site_key(a), _site_key(b)
        city_same = bool(
            ka[1] and kb[1] and (ka[1] == kb[1] or ka[1] in kb[1] or kb[1] in ka[1])
        )
        if ka[0] and kb[0] and ka[0] == kb[0] and (city_same or not ka[1] or not kb[1]):
            return "SAME_SITE"
        if ka[0] and ka == kb:
            return "SAME_SITE"
        if (
            city_same
            and ka[2]
            and kb[2]
            and ka[2] == kb[2]
            and (not ka[0] or not kb[0] or ka[0] == kb[0])
        ):
            return "SAME_SITE"
        if ka[1] and kb[1] and not city_same:
            return "DISTINCT_SITE"
        if ka[2] and kb[2] and ka[2] != kb[2] and ka[1] and kb[1] and not city_same:
            return "DISTINCT_SITE"
        if (ka[0] and kb[0] and ka[0] != kb[0]) and city_same:
            return "REVIEW"
        if ka == ("", "", "") or kb == ("", "", ""):
            return "REVIEW"
        return "REVIEW"

    if source_locs and survivor_locs:
        for sl in source_locs:
            best = None
            for tl in survivor_locs:
                kind = _classify_sites(sl, tl)
                best = (kind, sl, tl)
                if kind == "SAME_SITE":
                    break
            if best:
                kind, sl, tl = best
                comparisons.append(
                    {
                        "classification": kind,
                        "source_location_id": sl["id"],
                        "survivor_location_id": tl["id"],
                        "source_label": _blank(sl.get("location_name"))
                        or f"{_blank(sl.get('city'))} {_blank(sl.get('state'))}".strip(),
                        "survivor_label": _blank(tl.get("location_name"))
                        or f"{_blank(tl.get('city'))} {_blank(tl.get('state'))}".strip(),
                    }
                )
    else:
        kind = _classify_sites(src_company, tgt_company)
        comparisons.append(
            {
                "classification": kind,
                "source_location_id": None,
                "survivor_location_id": None,
                "source_label": f"{_blank(src_company.get('city'))} {_blank(src_company.get('state'))}".strip(),
                "survivor_label": f"{_blank(tgt_company.get('city'))} {_blank(tgt_company.get('state'))}".strip(),
                "from_company_master": True,
            }
        )
    return {
        "source_locations": source_locs,
        "survivor_locations": survivor_locs,
        "comparisons": comparisons,
        "creates_locations": False,
    }


def _plan_identities(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    def _load(cid: int) -> list[dict[str, Any]]:
        if not _table_exists(conn, "company_source_identities"):
            return []
        return [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT id, company_id, location_id, client_id, source_system,
                       source_record_no, source_company_name
                FROM company_source_identities WHERE company_id = ?
                """,
                (cid,),
            ).fetchall()
        ]

    src = _load(source_id)
    tgt = _load(survivor_id)

    def _ikey(row: dict[str, Any]) -> tuple:
        return (
            _blank(row.get("source_system")).lower(),
            _blank(row.get("source_record_no")),
            int(row["client_id"] or 0),
        )

    tgt_keys = {_ikey(r) for r in tgt}
    moves = []
    keeps = []
    for row in src:
        if _ikey(row) in tgt_keys:
            keeps.append({**row, "plan": "ALREADY ON SURVIVOR — keep one"})
        else:
            moves.append({**row, "plan": "MOVE identity to survivor (do not drop RN)"})
    master_rns = {
        "source_master_rn": _blank(_company_snapshot(conn, source_id).get("external_record_no")),
        "survivor_master_rn": _blank(
            _company_snapshot(conn, survivor_id).get("external_record_no")
        ),
    }
    return {
        "source_identities": src,
        "survivor_identities": tgt,
        "move": moves,
        "already_present": keeps,
        "master_record_nos": master_rns,
        "never_drop_source_rn": True,
        "uniqueness": "(source_system, source_record_no, COALESCE(client_id,0)) — not company_id",
    }


def _plan_aliases(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    if not _table_exists(conn, "company_aliases"):
        return {"source": [], "survivor": [], "move": [], "skip_duplicate": []}

    def _load(cid: int) -> list[dict[str, Any]]:
        return [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT id, company_id, alias_name, alias_norm, source_system,
                       source_record_no, client_id
                FROM company_aliases WHERE company_id = ?
                """,
                (cid,),
            ).fetchall()
        ]

    src = _load(source_id)
    tgt = _load(survivor_id)

    def _akey(row: dict[str, Any]) -> tuple:
        return (
            int(row["client_id"] or 0),
            _blank(row.get("source_system")),
            _blank(row.get("source_record_no")),
            _blank(row.get("alias_norm")),
        )

    tgt_keys = {_akey(r) for r in tgt}
    move, skip = [], []
    for row in src:
        if _akey(row) in tgt_keys:
            skip.append({**row, "plan": "SKIP — unique key already on survivor"})
        else:
            move.append({**row, "plan": "TRANSFER alias to survivor"})
    return {"source": src, "survivor": tgt, "move": move, "skip_duplicate": skip}


def _plan_campaigns(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    if not _table_exists(conn, "campaign_companies"):
        return {"source": [], "survivor": [], "union_move": [], "already_member": []}
    src = [
        _row_dict(r)
        for r in conn.execute(
            """
            SELECT campaign_id, client_id, company_id, notes
            FROM campaign_companies WHERE company_id = ?
            """,
            (source_id,),
        ).fetchall()
    ]
    tgt = [
        _row_dict(r)
        for r in conn.execute(
            """
            SELECT campaign_id, client_id, company_id, notes
            FROM campaign_companies WHERE company_id = ?
            """,
            (survivor_id,),
        ).fetchall()
    ]
    tgt_ids = {int(r["campaign_id"]) for r in tgt}
    union_move = []
    already = []
    for row in src:
        if int(row["campaign_id"]) in tgt_ids:
            already.append({**row, "plan": "UNION — already on survivor, absorb distinct notes"})
        else:
            union_move.append({**row, "plan": "UNION — add survivor membership"})
    return {
        "source": src,
        "survivor": tgt,
        "union_move": union_move,
        "already_member": already,
        "unique_constraint": "(campaign_id, company_id)",
    }


def _history_uniqueness(
    conn: sqlite3.Connection, source_id: int, survivor_id: int
) -> dict[str, Any]:
    flags: list[dict[str, Any]] = []
    if _table_exists(conn, "company_shared_history_events"):
        src_hashes = {
            _blank(r[0])
            for r in conn.execute(
                "SELECT event_hash FROM company_shared_history_events WHERE company_id = ?",
                (source_id,),
            ).fetchall()
        }
        tgt_hashes = {
            _blank(r[0])
            for r in conn.execute(
                "SELECT event_hash FROM company_shared_history_events WHERE company_id = ?",
                (survivor_id,),
            ).fetchall()
        }
        overlap = src_hashes & tgt_hashes
        flags.append(
            {
                "table": "company_shared_history_events",
                "unique": "(company_id, event_hash)",
                "source_count": len(src_hashes),
                "survivor_count": len(tgt_hashes),
                "duplicate_hashes": len(overlap),
                "plan": "MOVE distinct hashes; skip exact hash already on survivor",
            }
        )
    for table, unique in (
        ("opportunity_dismissals", "(target_client_id, company_id)"),
        ("opportunity_assignments", "(target_client_id, company_id)"),
        ("opportunity_reviews", "(target_client_id, company_id)"),
        ("company_client_fit", "(company_id, client_id)" if _table_exists(conn, "company_client_fit") else ""),
        ("company_intelligence", "(company_id, field_key)" if _table_exists(conn, "company_intelligence") else ""),
        ("campaign_companies", "(campaign_id, company_id)"),
        ("client_company_relationships", "(client_id, company_id)"),
    ):
        if unique and _table_exists(conn, table):
            src_n = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE company_id = ?",
                    (source_id,),
                ).fetchone()[0]
            )
            tgt_n = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE company_id = ?",
                    (survivor_id,),
                ).fetchone()[0]
            )
            flags.append(
                {
                    "table": table,
                    "unique": unique,
                    "source_count": src_n,
                    "survivor_count": tgt_n,
                    "plan": "CONSOLIDATE or KEEP-ONE — remap would collide if both rows exist",
                }
            )
    fts = _table_exists(conn, "search_fts")
    return {
        "uniqueness_flags": flags,
        "fts_rebuild_required": fts,
        "identity_key_refresh": (
            "Delete source company_identity_keys. Rebuild survivor keys from the "
            "consolidated company + aliases + source identities. Do not live-refresh in 5E."
        ),
        "sales_workflow": [
            t
            for t in (
                "activities",
                "work_queue_items",
                "appointments",
                "revenue_milestones",
                "client_sales_events",
                "crm_import_results",
            )
            if _table_exists(conn, t)
        ],
    }


def plan_company_merge(
    conn: sqlite3.Connection,
    source_company_id: int,
    survivor_company_id: int,
) -> dict[str, Any]:
    """Read-only consolidation plan. Never writes."""
    ensure_company_merge_schema(conn)
    src = int(source_company_id)
    dst = int(survivor_company_id)
    blockers: list[str] = []
    warnings: list[str] = []
    if src == dst:
        blockers.append("Source and survivor are the same company.")
    source_row = _company_snapshot(conn, src)
    survivor_row = _company_snapshot(conn, dst)
    if not source_row:
        blockers.append(f"Source company {src} does not exist.")
    if not survivor_row:
        blockers.append(f"Survivor company {dst} does not exist.")

    refs = discover_company_id_refs(conn)
    contacts = _plan_contacts(conn, src, dst) if source_row and survivor_row else {}
    notes = _plan_notes(conn, src, dst) if source_row and survivor_row else {}
    ccrs = _plan_ccrs(conn, src, dst) if source_row and survivor_row else {}
    locations = _plan_locations(conn, src, dst) if source_row and survivor_row else {}
    identities = _plan_identities(conn, src, dst) if source_row and survivor_row else {}
    aliases = _plan_aliases(conn, src, dst) if source_row and survivor_row else {}
    campaigns = _plan_campaigns(conn, src, dst) if source_row and survivor_row else {}
    history = _history_uniqueness(conn, src, dst) if source_row and survivor_row else {}

    if ccrs.get("verdict_hints"):
        blockers.extend(
            [
                "Same-client CCR status conflict requires HUMAN REVIEW. "
                "Statuses are never auto-resolved."
            ]
        )
    loc_classes = {
        c.get("classification")
        for c in (locations.get("comparisons") or [])
    }
    if "DISTINCT_SITE" in loc_classes:
        blockers.append(
            "DISTINCT_SITE — these look like different physical sites, not a "
            "duplicate master. Future duplicate cleanup must not auto-merge them."
        )
    if "REVIEW" in loc_classes:
        warnings.append("Location SAME/DISTINCT could not be decided — REVIEW.")
    if contacts.get("possible_duplicates_requiring_review"):
        warnings.append("Possible duplicate contacts require human review before automatic merge.")
    if contacts.get("contact_field_conflicts"):
        warnings.append("Contact field conflicts (email/phone/title) will not be chosen silently.")

    verdict = "MERGE READY"
    if blockers:
        verdict = "MERGE BLOCKED"

    projected_contacts = contacts.get("projected_survivor_contact_count")
    projected_notes = None
    if notes:
        projected_notes = notes.get("survivor_company_notes", 0) + notes.get(
            "distinct_notes_to_preserve", 0
        )

    return {
        "source_company_id": src,
        "survivor_company_id": dst,
        "source": source_row,
        "survivor": survivor_row,
        "verdict": verdict,
        "blockers": blockers,
        "warnings": warnings,
        "retirement_policy": {
            "code": RETIREMENT_POLICY_CODE,
            "label": RETIREMENT_POLICY_LABEL,
            "rationale": RETIREMENT_POLICY_RATIONALE,
            "implemented_in_phase_5e": False,
        },
        "contact_redirect": {
            "required": CONTACT_REDIRECT_REQUIRED,
            "table": "contact_merge_history",
            "rationale": CONTACT_REDIRECT_RATIONALE,
            "implemented_empty_in_phase_5e": True,
        },
        "consolidation": {
            "kind": "DATA CONSOLIDATION — not a company-ID remap only",
            "preserve_from_both": True,
            "contacts": "MOVE unique / MERGE high-confidence / REVIEW possible / KEEP SEPARATE distinct",
            "notes": "KEEP EXISTING NOTES AND ADD NEW DISTINCT NOTES",
            "client_specific": "Never collapse different clients; same-client CCR consolidates with review on status",
        },
        "bulk_readiness": {
            "workflow": [
                "Potential Duplicate Groups",
                "review evidence",
                "choose survivor",
                "preview consolidation via plan_company_merge",
                "show contacts/notes/status/RNs/campaigns affected",
                "approve merge",
                "atomic merge",
                "permanent redirect/audit record",
            ],
            "bulk_ui_in_phase_5e": False,
            "planner_is_preview_api": True,
        },
        "dependencies": refs,
        "contacts": contacts,
        "notes": notes,
        "ccrs": ccrs,
        "locations": locations,
        "source_identities": identities,
        "aliases": aliases,
        "campaigns": campaigns,
        "history_workflow": history,
        "projected": {
            "contacts_on_survivor": projected_contacts,
            "note_rows_on_survivor": projected_notes,
            "question": (
                "If these two company records merge, which contacts and notes "
                "exist on the surviving record?"
            ),
        },
        "live_merge_executed": False,
    }


def classify_contact_match_public(source: dict[str, Any], survivor: dict[str, Any]) -> str:
    klass, _reasons = classify_contact_pair(source, survivor)
    return klass
