"""DS-11 administrator Duplicate Review and merge planning.

Discovery and review dispositions only. Never creates merge approvals,
never executes merge, and never mutates Master Company / CCR / contact rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from company_match import (
    compact_company_name,
    names_match,
    score_company_pair,
)
from company_merges import classify_contact_pair, plan_company_merge
from import_brown_industries import digits_phone, domain, norm_addr
from models import NorthStarUser

DISPOSITION_UNREVIEWED = "UNREVIEWED"
DISPOSITION_LIKELY = "LIKELY_DUPLICATE"
DISPOSITION_NOT = "NOT_DUPLICATE"
DISPOSITION_MULTI = "MULTI_LOCATION"
DISPOSITION_RESEARCH = "NEEDS_RESEARCH"
DISPOSITION_MERGE = "MERGE_CANDIDATE"

REVIEW_DISPOSITIONS = frozenset(
    {
        DISPOSITION_LIKELY,
        DISPOSITION_NOT,
        DISPOSITION_MULTI,
        DISPOSITION_RESEARCH,
        DISPOSITION_MERGE,
    }
)
SUPPRESS_UNREVIEWED = frozenset({DISPOSITION_NOT, DISPOSITION_MULTI})

CAT_EXACT_NAME_ADDRESS = "EXACT_NAME_ADDRESS"
CAT_EXACT_PHONE = "EXACT_PHONE"
CAT_EXACT_DOMAIN = "EXACT_DOMAIN"
CAT_ALIAS_MATCH = "ALIAS_MATCH"
CAT_NAME_CITY = "NAME_CITY"
CAT_NAME_ONLY = "NAME_ONLY"
CAT_IDENTITY = "SOURCE_IDENTITY_CONFLICT"
CAT_MULTI_LOCATION = "MULTI_LOCATION_PATTERN"

CATEGORY_RANK = {
    CAT_EXACT_NAME_ADDRESS: 0,
    CAT_EXACT_PHONE: 1,
    CAT_EXACT_DOMAIN: 2,
    CAT_IDENTITY: 3,
    CAT_ALIAS_MATCH: 4,
    CAT_NAME_CITY: 5,
    CAT_MULTI_LOCATION: 6,
    CAT_NAME_ONLY: 7,
}

MAX_BLOCK = 80
MAX_PAIRS = 8000
DEFAULT_LIMIT = 50
PLAN_ONLY_WARNING = "This records a merge plan only. No records will be merged."
ENTITY_DUPLICATE_REVIEW = "duplicate_review"


class DuplicateReviewError(ValueError):
    def __init__(self, code: str, payload: dict[str, Any] | None = None):
        super().__init__(code)
        self.payload = payload or {}


class DuplicateReviewSaveRequest(BaseModel):
    disposition: str
    reason: str = ""
    proposed_survivor_company_id: int | None = None
    proposed_source_company_id: int | None = None
    actor_id: int | None = None
    created_by: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def pair_ids(company_a_id: int, company_b_id: int) -> tuple[int, int]:
    a = int(company_a_id)
    b = int(company_b_id)
    if a <= 0 or b <= 0 or a == b:
        raise DuplicateReviewError("invalid_pair")
    return (a, b) if a < b else (b, a)


def pair_key(company_a_id: int, company_b_id: int) -> str:
    lo, hi = pair_ids(company_a_id, company_b_id)
    return f"{lo}:{hi}"


def ensure_duplicate_review_schema(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent empty review tables. Does not insert review decisions."""
    stats = {"created_company_duplicate_reviews": 0, "created_review_events": 0}
    if not _table_exists(conn, "companies"):
        return stats
    existed_reviews = _table_exists(conn, "company_duplicate_reviews")
    existed_events = _table_exists(conn, "company_duplicate_review_events")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_duplicate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_a_id INTEGER NOT NULL,
            company_b_id INTEGER NOT NULL,
            pair_key TEXT NOT NULL UNIQUE,
            disposition TEXT NOT NULL,
            proposed_survivor_company_id INTEGER,
            proposed_source_company_id INTEGER,
            review_reason TEXT NOT NULL DEFAULT '',
            reviewed_by_user_id INTEGER,
            reviewed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            evidence_fingerprint TEXT NOT NULL DEFAULT '',
            CHECK (company_a_id < company_b_id),
            CHECK (disposition IN (
                'UNREVIEWED','LIKELY_DUPLICATE','NOT_DUPLICATE',
                'MULTI_LOCATION','NEEDS_RESEARCH','MERGE_CANDIDATE'
            )),
            FOREIGN KEY (company_a_id) REFERENCES companies(id),
            FOREIGN KEY (company_b_id) REFERENCES companies(id),
            FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_duplicate_review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            pair_key TEXT NOT NULL,
            old_disposition TEXT NOT NULL DEFAULT '',
            new_disposition TEXT NOT NULL,
            old_survivor_company_id INTEGER,
            new_survivor_company_id INTEGER,
            old_source_company_id INTEGER,
            new_source_company_id INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            evidence_fingerprint TEXT NOT NULL DEFAULT '',
            actor_user_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (review_id) REFERENCES company_duplicate_reviews(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dup_reviews_disposition ON company_duplicate_reviews(disposition)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dup_reviews_reviewed_at ON company_duplicate_reviews(reviewed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dup_review_events_review ON company_duplicate_review_events(review_id, id)"
    )
    if not existed_reviews:
        stats["created_company_duplicate_reviews"] = 1
    if not existed_events:
        stats["created_review_events"] = 1
    return stats


def _core_name(value: str) -> str:
    tokens = [t for t in compact_company_name(value).split() if len(t) >= 3]
    if len(tokens) >= 2:
        return " ".join(tokens[:2])
    if tokens:
        return tokens[0]
    return compact_company_name(value)


def _add_block(blocks: dict[str, list[int]], prefix: str, key: str, company_id: int) -> None:
    token = _blank(key).lower()
    if not token:
        return
    blocks.setdefault(f"{prefix}:{token}", []).append(int(company_id))


def classify_evidence(
    *,
    name_equal: bool,
    addr_equal: bool,
    city_state: bool,
    phone_equal: bool,
    domain_equal: bool,
    alias_match: bool,
    identity_conflict: bool,
    distinct_site: bool,
) -> list[str]:
    cats: list[str] = []
    if name_equal and addr_equal:
        cats.append(CAT_EXACT_NAME_ADDRESS)
    if phone_equal:
        cats.append(CAT_EXACT_PHONE)
    if domain_equal:
        cats.append(CAT_EXACT_DOMAIN)
    if identity_conflict:
        cats.append(CAT_IDENTITY)
    if alias_match:
        cats.append(CAT_ALIAS_MATCH)
    if name_equal and city_state and not addr_equal:
        cats.append(CAT_NAME_CITY)
    if name_equal and distinct_site:
        cats.append(CAT_MULTI_LOCATION)
    if name_equal and not addr_equal and not phone_equal and not domain_equal and not city_state:
        cats.append(CAT_NAME_ONLY)
    if not cats and (alias_match or identity_conflict):
        pass
    if not cats and name_equal:
        cats.append(CAT_NAME_ONLY)
    return list(dict.fromkeys(cats))


def evidence_sentence(categories: list[str]) -> str:
    labels = {
        CAT_EXACT_NAME_ADDRESS: "normalized name and address match",
        CAT_EXACT_PHONE: "phone numbers match",
        CAT_EXACT_DOMAIN: "website domains match",
        CAT_ALIAS_MATCH: "an alias matches the other company name",
        CAT_NAME_CITY: "normalized name and city/state match",
        CAT_NAME_ONLY: "normalized names match",
        CAT_IDENTITY: "source identity keys collide across two masters",
        CAT_MULTI_LOCATION: "the same organization name appears at different locations",
    }
    bits = [labels[c] for c in categories if c in labels]
    if not bits:
        return "Possible duplicate based on shared identity keys. Julie decides the disposition."
    return "Possible duplicate because " + "; ".join(bits) + ". Julie decides the disposition."


def _load_companies(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, company_name, address, city, state, zip, website, legacy_phone,
               COALESCE(legacy_phone_extension, '') AS phone_extension,
               external_record_no, created_at, last_updated_at,
               TRIM(COALESCE(archived_at, '')) AS archived_at
        FROM companies
        """
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = _row_dict(row)
        cid = int(item["id"])
        item["company_id"] = cid
        item["archived"] = bool(_blank(item.get("archived_at")))
        item["compact_name"] = compact_company_name(_blank(item.get("company_name")))
        item["core_name"] = _core_name(_blank(item.get("company_name")))
        item["phone_digits"] = digits_phone(_blank(item.get("legacy_phone")))
        item["domain"] = domain(_blank(item.get("website")))
        item["addr_norm"] = norm_addr(_blank(item.get("address")))
        out[cid] = item
    return out


def _load_aliases(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not _table_exists(conn, "company_aliases"):
        return out
    for row in conn.execute(
        """
        SELECT company_id, alias_name, alias_norm, source_system, source_record_no,
               source_address, source_city, source_state
        FROM company_aliases
        """
    ).fetchall():
        item = _row_dict(row)
        out.setdefault(int(item["company_id"]), []).append(item)
    return out


def _load_identities(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not _table_exists(conn, "company_source_identities"):
        return out
    for row in conn.execute(
        """
        SELECT id, company_id, location_id, client_id, source_system, source_record_no,
               source_company_name, source_address, source_city, source_state
        FROM company_source_identities
        """
    ).fetchall():
        item = _row_dict(row)
        out.setdefault(int(item["company_id"]), []).append(item)
    return out


def _load_locations(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    if not _table_exists(conn, "company_locations"):
        return out
    for row in conn.execute(
        """
        SELECT id, company_id, location_name, location_type, address, city, state, zip,
               phone, is_headquarters, is_primary
        FROM company_locations
        """
    ).fetchall():
        item = _row_dict(row)
        out.setdefault(int(item["company_id"]), []).append(item)
    return out


def _load_identity_keys(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not _table_exists(conn, "company_identity_keys"):
        return out
    for row in conn.execute("SELECT * FROM company_identity_keys").fetchall():
        item = _row_dict(row)
        out[int(item["company_id"])] = item
    return out


def _load_ccr_identity(conn: sqlite3.Connection) -> dict[int, list[list[Any]]]:
    out: dict[int, list[list[Any]]] = {}
    if not _table_exists(conn, "client_company_relationships"):
        return out
    for row in conn.execute(
        """
        SELECT company_id, client_id, assigned_user_id, external_record_no,
               TRIM(COALESCE(archived_at, '')) AS archived_at
        FROM client_company_relationships
        ORDER BY company_id, client_id, id
        """
    ).fetchall():
        out.setdefault(int(row["company_id"]), []).append(
            [
                int(row["client_id"]),
                1 if _blank(row["archived_at"]) else 0,
                int(row["assigned_user_id"] or 0),
                _blank(row["external_record_no"]),
            ]
        )
    return out


def _load_reviews(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    ensure_duplicate_review_schema(conn)
    out: dict[str, dict[str, Any]] = {}
    if not _table_exists(conn, "company_duplicate_reviews"):
        return out
    for row in conn.execute("SELECT * FROM company_duplicate_reviews").fetchall():
        item = _row_dict(row)
        out[str(item["pair_key"])] = item
    return out


def _identity_conflicts(identities: dict[int, list[dict[str, Any]]]) -> set[tuple[int, int]]:
    buckets: dict[tuple[str, str], list[int]] = {}
    for cid, rows in identities.items():
        for row in rows:
            key = (
                _blank(row.get("source_system")).lower(),
                _blank(row.get("source_record_no")),
            )
            if not key[0] or not key[1]:
                continue
            buckets.setdefault(key, []).append(int(cid))
    pairs: set[tuple[int, int]] = set()
    for ids in buckets.values():
        uniq = sorted(set(ids))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1 :]:
                pairs.add((a, b))
    return pairs


def _alias_index(aliases: dict[int, list[dict[str, Any]]]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {}
    for cid, rows in aliases.items():
        for row in rows:
            for raw in (
                _blank(row.get("alias_norm")),
                compact_company_name(_blank(row.get("alias_name"))),
            ):
                if raw:
                    index.setdefault(raw.lower(), set()).add(int(cid))
    return index


def evidence_fingerprint(
    conn: sqlite3.Connection,
    company_a_id: int,
    company_b_id: int,
    *,
    companies: dict[int, dict[str, Any]] | None = None,
    aliases: dict[int, list[dict[str, Any]]] | None = None,
    identities: dict[int, list[dict[str, Any]]] | None = None,
    locations: dict[int, list[dict[str, Any]]] | None = None,
    ccrs: dict[int, list[list[Any]]] | None = None,
) -> str:
    lo, hi = pair_ids(company_a_id, company_b_id)
    companies = companies if companies is not None else _load_companies(conn)
    aliases = aliases if aliases is not None else _load_aliases(conn)
    identities = identities if identities is not None else _load_identities(conn)
    locations = locations if locations is not None else _load_locations(conn)
    ccrs = ccrs if ccrs is not None else _load_ccr_identity(conn)
    payload: dict[str, Any] = {"pair": [lo, hi], "sides": {}}
    for cid in (lo, hi):
        co = companies.get(cid) or {}
        alias_vals = sorted(
            {
                _blank(r.get("alias_norm")).lower()
                for r in (aliases.get(cid) or [])
                if _blank(r.get("alias_norm"))
            }
        )
        loc_vals = sorted(
            {
                "|".join(
                    [
                        norm_addr(_blank(r.get("address"))),
                        _blank(r.get("city")).lower(),
                        _blank(r.get("state")).upper(),
                    ]
                )
                for r in (locations.get(cid) or [])
            }
        )
        ident_vals = sorted(
            {
                f"{_blank(r.get('source_system')).lower()}|{_blank(r.get('source_record_no'))}"
                for r in (identities.get(cid) or [])
            }
        )
        payload["sides"][str(cid)] = {
            "name": _blank(co.get("company_name")),
            "address": _blank(co.get("address")),
            "city": _blank(co.get("city")),
            "state": _blank(co.get("state")),
            "phone": digits_phone(_blank(co.get("legacy_phone"))),
            "website": domain(_blank(co.get("website"))),
            "archived": bool(co.get("archived")),
            "rn": _blank(co.get("external_record_no")),
            "aliases": alias_vals,
            "locations": loc_vals,
            "identities": ident_vals,
            "ccrs": ccrs.get(cid) or [],
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _score_pair(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    alias_match: bool,
    identity_conflict: bool,
) -> dict[str, Any] | None:
    query = {
        "company_name": _blank(a.get("company_name")),
        "website": _blank(a.get("website")),
        "address": _blank(a.get("address")),
        "city": _blank(a.get("city")),
        "state": _blank(a.get("state")),
        "phone": _blank(a.get("legacy_phone")),
        "external_record_no": _blank(a.get("external_record_no")),
    }
    cand = {
        "company_id": int(b["id"]),
        "company_name": _blank(b.get("company_name")),
        "website": _blank(b.get("website")),
        "address": _blank(b.get("address")),
        "city": _blank(b.get("city")),
        "state": _blank(b.get("state")),
        "zip": _blank(b.get("zip")),
        "phone": _blank(b.get("legacy_phone")),
        "external_record_no": _blank(b.get("external_record_no")),
    }
    score, reasons = score_company_pair(query, cand)
    name_equal = names_match(_blank(a.get("company_name")), _blank(b.get("company_name")))
    if not name_equal:
        name_equal = bool(a.get("core_name") and a.get("core_name") == b.get("core_name"))
    addr_equal = bool(a.get("addr_norm") and a.get("addr_norm") == b.get("addr_norm"))
    city_state = bool(
        _blank(a.get("city"))
        and _blank(b.get("city"))
        and _blank(a.get("state"))
        and _blank(a.get("city")).lower() == _blank(b.get("city")).lower()
        and _blank(a.get("state")).upper() == _blank(b.get("state")).upper()
    )
    phone_equal = bool(
        a.get("phone_digits")
        and b.get("phone_digits")
        and len(str(a.get("phone_digits"))) >= 7
        and (
            a.get("phone_digits") == b.get("phone_digits")
            or str(a.get("phone_digits"))[-7:] == str(b.get("phone_digits"))[-7:]
        )
    )
    domain_equal = bool(a.get("domain") and a.get("domain") == b.get("domain"))
    distinct_site = bool(
        name_equal
        and (
            (addr_equal is False and (_blank(a.get("address")) or _blank(b.get("address"))))
            or (
                _blank(a.get("city"))
                and _blank(b.get("city"))
                and _blank(a.get("city")).lower() != _blank(b.get("city")).lower()
            )
        )
    )
    fuzzy_only = (not name_equal) and (not phone_equal) and (not domain_equal) and (
        not alias_match
    ) and (not identity_conflict) and (not addr_equal)
    if fuzzy_only and not set(reasons) - {"fuzzy_name", "token_similarity", "token_set"}:
        return None
    if not (
        name_equal
        or phone_equal
        or domain_equal
        or alias_match
        or identity_conflict
        or addr_equal
        or "compact_name" in reasons
    ):
        return None
    categories = classify_evidence(
        name_equal=name_equal,
        addr_equal=addr_equal,
        city_state=city_state,
        phone_equal=phone_equal,
        domain_equal=domain_equal,
        alias_match=alias_match,
        identity_conflict=identity_conflict,
        distinct_site=distinct_site,
    )
    if not categories:
        return None
    return {
        "score": int(score),
        "reasons": reasons,
        "categories": categories,
        "name_equal": name_equal,
        "distinct_site": distinct_site,
        "evidence_summary": evidence_sentence(categories),
        "automatic_verdict": False,
    }


def discover_duplicate_pairs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Server-side blocked discovery. Does not walk N² in the browser."""
    ensure_duplicate_review_schema(conn)
    companies = _load_companies(conn)
    aliases = _load_aliases(conn)
    identities = _load_identities(conn)
    locations = _load_locations(conn)
    keys = _load_identity_keys(conn)
    ccrs = _load_ccr_identity(conn)
    reviews = _load_reviews(conn)
    identity_pairs = _identity_conflicts(identities)
    alias_idx = _alias_index(aliases)

    blocks: dict[str, list[int]] = {}
    for cid, co in companies.items():
        _add_block(blocks, "name", co.get("compact_name") or "", cid)
        _add_block(blocks, "core", co.get("core_name") or "", cid)
        phone = str(co.get("phone_digits") or "")
        if len(phone) >= 7:
            _add_block(blocks, "phone", phone[-7:], cid)
        _add_block(blocks, "domain", co.get("domain") or "", cid)
        ik = keys.get(cid) or {}
        _add_block(blocks, "kname", _blank(ik.get("norm_name")), cid)
        _add_block(blocks, "kphone", _blank(ik.get("phone_last7")), cid)
        _add_block(blocks, "kdom", _blank(ik.get("domain")), cid)
        addr = _blank(ik.get("addr_norm"))
        city = _blank(ik.get("city_norm"))
        if addr and city:
            _add_block(blocks, "kaddr", f"{addr}|{city}", cid)
        for al in aliases.get(cid) or []:
            _add_block(blocks, "alias", _blank(al.get("alias_norm")), cid)

    candidate_ids: set[tuple[int, int]] = set(identity_pairs)
    for ids in blocks.values():
        uniq = sorted(set(ids))
        if len(uniq) < 2:
            continue
        if len(uniq) > MAX_BLOCK:
            uniq = uniq[:MAX_BLOCK]
        for i, a in enumerate(uniq):
            for b in uniq[i + 1 :]:
                candidate_ids.add((a, b) if a < b else (b, a))
                if len(candidate_ids) >= MAX_PAIRS:
                    break
            if len(candidate_ids) >= MAX_PAIRS:
                break
        if len(candidate_ids) >= MAX_PAIRS:
            break

    pairs: list[dict[str, Any]] = []
    for lo, hi in candidate_ids:
        a = companies.get(lo)
        b = companies.get(hi)
        if not a or not b:
            continue
        a_names = {a.get("compact_name"), a.get("core_name")}
        b_names = {b.get("compact_name"), b.get("core_name")}
        alias_match = False
        for al in aliases.get(lo) or []:
            norm = compact_company_name(_blank(al.get("alias_name")))
            if norm and (norm in b_names or norm == b.get("compact_name")):
                alias_match = True
        for al in aliases.get(hi) or []:
            norm = compact_company_name(_blank(al.get("alias_name")))
            if norm and (norm in a_names or norm == a.get("compact_name")):
                alias_match = True
        shared_alias = set()
        for key, members in alias_idx.items():
            if lo in members and hi in members:
                shared_alias.add(key)
        if shared_alias:
            alias_match = True
        scored = _score_pair(
            a,
            b,
            alias_match=alias_match,
            identity_conflict=(lo, hi) in identity_pairs,
        )
        if scored is None:
            continue
        key = f"{lo}:{hi}"
        review = reviews.get(key) or {}
        current_fp = evidence_fingerprint(
            conn,
            lo,
            hi,
            companies=companies,
            aliases=aliases,
            identities=identities,
            locations=locations,
            ccrs=ccrs,
        )
        stored_fp = _blank(review.get("evidence_fingerprint"))
        stale = bool(review) and stored_fp != current_fp
        disposition = _blank(review.get("disposition")) or DISPOSITION_UNREVIEWED
        suppressed = (
            disposition in SUPPRESS_UNREVIEWED and not stale and bool(review)
        )
        pairs.append(
            {
                "company_a_id": lo,
                "company_b_id": hi,
                "pair_key": key,
                "company_a": _list_company(a),
                "company_b": _list_company(b),
                "categories": scored["categories"],
                "evidence_summary": scored["evidence_summary"],
                "match_reasons": scored["reasons"],
                "automatic_verdict": False,
                "disposition": disposition,
                "review_status": "RE_REVIEW_NEEDED" if stale else (
                    "UNREVIEWED" if disposition == DISPOSITION_UNREVIEWED else "REVIEWED"
                ),
                "stale": stale,
                "suppressed": suppressed,
                "reviewed_at": _blank(review.get("reviewed_at")),
                "reviewed_by_user_id": review.get("reviewed_by_user_id"),
                "review_reason": _blank(review.get("review_reason")),
                "proposed_survivor_company_id": review.get("proposed_survivor_company_id"),
                "proposed_source_company_id": review.get("proposed_source_company_id"),
                "evidence_fingerprint": current_fp,
                "planning_only": True,
                "merge_will_occur": False,
            }
        )
    pairs.sort(
        key=lambda p: (
            min(CATEGORY_RANK.get(c, 9) for c in p["categories"]),
            p["company_a"]["company_name"].lower(),
            p["company_a_id"],
            p["company_b_id"],
        )
    )
    return pairs


def _list_company(co: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_id": int(co["id"]),
        "company_name": _blank(co.get("company_name")),
        "city": _blank(co.get("city")),
        "state": _blank(co.get("state")),
        "phone": _blank(co.get("legacy_phone")),
        "website": _blank(co.get("website")),
        "external_record_no": _blank(co.get("external_record_no")),
        "archived": bool(co.get("archived")),
        "address": _blank(co.get("address")),
        "zip": _blank(co.get("zip")),
    }


def list_duplicate_candidates(
    conn: sqlite3.Connection,
    *,
    disposition: str = "unreviewed",
    q: str = "",
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    ensure_duplicate_review_schema(conn)
    needle = _blank(q).lower()
    wanted = _blank(disposition).lower() or "unreviewed"
    rows = discover_duplicate_pairs(conn)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        disp = _blank(row.get("disposition")).upper()
        stale = bool(row.get("stale"))
        if wanted in {"unreviewed", ""}:
            if not stale and disp not in {DISPOSITION_UNREVIEWED, ""}:
                continue
        elif wanted == "all":
            pass
        elif wanted == "stale":
            if not stale:
                continue
        elif wanted == "likely_duplicate":
            if disp != DISPOSITION_LIKELY or stale:
                continue
        elif wanted == "not_duplicate":
            if disp != DISPOSITION_NOT:
                continue
        elif wanted == "multi_location":
            if disp != DISPOSITION_MULTI:
                continue
        elif wanted == "needs_research":
            if disp != DISPOSITION_RESEARCH or stale:
                continue
        elif wanted == "merge_candidate":
            if disp != DISPOSITION_MERGE or stale:
                continue
        elif wanted == "reviewed":
            if disp == DISPOSITION_UNREVIEWED:
                continue
        if needle:
            blob = " ".join(
                [
                    str(row["company_a"].get("company_name") or ""),
                    str(row["company_b"].get("company_name") or ""),
                    str(row["company_a"].get("external_record_no") or ""),
                    str(row["company_b"].get("external_record_no") or ""),
                    str(row.get("pair_key") or ""),
                ]
            ).lower()
            if needle not in blob:
                continue
        filtered.append(row)
    total = len(filtered)
    start = max(0, int(offset))
    size = min(100, max(1, int(limit)))
    page = filtered[start : start + size]
    return {
        "planning_only": True,
        "merge_will_occur": False,
        "automatic_verdict": False,
        "writes": False,
        "total": total,
        "offset": start,
        "limit": size,
        "pairs": page,
    }


def _require_companies(conn: sqlite3.Connection, lo: int, hi: int) -> None:
    for cid in (lo, hi):
        row = conn.execute("SELECT id FROM companies WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise DuplicateReviewError("company_not_found", {"company_id": cid})


def _company_detail(conn: sqlite3.Connection, company_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, company_name, address, city, state, zip, website, legacy_phone,
               COALESCE(legacy_phone_extension, '') AS phone_extension,
               external_record_no, created_at, last_updated_at, source,
               TRIM(COALESCE(archived_at, '')) AS archived_at,
               TRIM(COALESCE(archive_reason, '')) AS archive_reason
        FROM companies WHERE id=?
        """,
        (int(company_id),),
    ).fetchone()
    if row is None:
        raise DuplicateReviewError("company_not_found", {"company_id": int(company_id)})
    item = _row_dict(row)
    cid = int(item["id"])
    item["company_id"] = cid
    item["archived"] = bool(_blank(item.get("archived_at")))
    item["phone"] = _blank(item.get("legacy_phone"))
    user_join = ""
    name_sql = "'' AS assigned_user_name"
    if _table_exists(conn, "users"):
        user_join = "LEFT JOIN users u ON u.id = ccr.assigned_user_id"
        name_sql = "COALESCE(u.full_name, '') AS assigned_user_name"
    ccrs = []
    if _table_exists(conn, "client_company_relationships"):
        ccrs = [
            _row_dict(r)
            for r in conn.execute(
                f"""
                SELECT ccr.id AS ccr_id, ccr.client_id, cl.code AS client_code, cl.name AS client_name,
                       ccr.status, ccr.assigned_user_id, {name_sql},
                       ccr.is_hot, ccr.follow_up_date, ccr.next_action, ccr.external_record_no,
                       TRIM(COALESCE(ccr.archived_at, '')) AS archived_at,
                       TRIM(COALESCE(ccr.notes, '')) AS notes
                FROM client_company_relationships ccr
                JOIN clients cl ON cl.id = ccr.client_id
                {user_join}
                WHERE ccr.company_id = ?
                ORDER BY cl.name, ccr.id
                """,
                (cid,),
            ).fetchall()
        ]
        for rel in ccrs:
            rel["active"] = not bool(_blank(rel.get("archived_at")))
            rel["hot"] = bool(rel.get("is_hot"))
    aliases = []
    if _table_exists(conn, "company_aliases"):
        aliases = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT id, alias_name, alias_norm, source_system, source_record_no,
                       source_address, source_city, source_state,
                       source_phone, source_website
                FROM company_aliases WHERE company_id=? ORDER BY id
                """,
                (cid,),
            ).fetchall()
        ]
    identities = []
    if _table_exists(conn, "company_source_identities"):
        identities = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT id, source_system, source_record_no, source_company_name,
                       source_address, source_city, source_state, location_id, client_id
                FROM company_source_identities WHERE company_id=? ORDER BY id
                """,
                (cid,),
            ).fetchall()
        ]
    locations = []
    if _table_exists(conn, "company_locations"):
        locations = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT id, location_name, location_type, address, city, state, zip, phone,
                       is_headquarters, is_primary
                FROM company_locations WHERE company_id=? ORDER BY id
                """,
                (cid,),
            ).fetchall()
        ]
    contacts = []
    if _table_exists(conn, "contacts"):
        contacts = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT id, first_name, last_name, title, email, phone, phone_extension,
                       external_record_no, TRIM(COALESCE(archived_at, '')) AS archived_at
                FROM contacts WHERE company_id=? ORDER BY last_name, first_name, id
                """,
                (cid,),
            ).fetchall()
        ]
    campaigns = []
    if _table_exists(conn, "campaign_companies") and _table_exists(conn, "client_campaigns"):
        campaigns = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT cc.id, cc.campaign_id, ca.campaign_name AS campaign_name, ca.client_id,
                       cl.name AS client_name, cl.code AS client_code
                FROM campaign_companies cc
                JOIN client_campaigns ca ON ca.id = cc.campaign_id
                JOIN clients cl ON cl.id = ca.client_id
                WHERE cc.company_id=?
                ORDER BY cl.name, ca.campaign_name, cc.id
                """,
                (cid,),
            ).fetchall()
        ]
    history = _history_summary(conn, cid, ccrs)
    merge_as_source = 0
    merge_as_survivor = 0
    if _table_exists(conn, "company_merge_history"):
        merge_as_source = int(
            conn.execute(
                "SELECT COUNT(*) FROM company_merge_history WHERE source_company_id=?",
                (cid,),
            ).fetchone()[0]
        )
        merge_as_survivor = int(
            conn.execute(
                "SELECT COUNT(*) FROM company_merge_history WHERE survivor_company_id=?",
                (cid,),
            ).fetchone()[0]
        )
    active_ccrs = sum(1 for r in ccrs if r.get("active"))
    return {
        "company_id": cid,
        "company_name": _blank(item.get("company_name")),
        "address": _blank(item.get("address")),
        "city": _blank(item.get("city")),
        "state": _blank(item.get("state")),
        "zip": _blank(item.get("zip")),
        "phone": _blank(item.get("legacy_phone")),
        "phone_extension": _blank(item.get("phone_extension")),
        "website": _blank(item.get("website")),
        "external_record_no": _blank(item.get("external_record_no")),
        "archived": bool(item.get("archived")),
        "archived_at": _blank(item.get("archived_at")),
        "archive_reason": _blank(item.get("archive_reason")),
        "created_at": _blank(item.get("created_at")),
        "updated_at": _blank(item.get("last_updated_at")),
        "source": _blank(item.get("source")),
        "ccrs": ccrs,
        "aliases": aliases,
        "identities": identities,
        "locations": locations,
        "contacts": contacts,
        "campaigns": campaigns,
        "history": history,
        "planning_factors": {
            "has_external_rn": bool(_blank(item.get("external_record_no"))),
            "active_ccr_count": active_ccrs,
            "contact_count": len(contacts),
            "has_canonical_address": bool(_blank(item.get("address"))),
            "alias_count": len(aliases),
            "location_count": len(locations),
            "identity_count": len(identities),
            "archived": bool(item.get("archived")),
            "merge_as_source": merge_as_source,
            "merge_as_survivor": merge_as_survivor,
            "history_note_count": int(history.get("notes") or 0),
            "history_activity_count": int(history.get("activities") or 0),
        },
    }


def _history_summary(
    conn: sqlite3.Connection, company_id: int, ccrs: list[dict[str, Any]]
) -> dict[str, int]:
    cid = int(company_id)
    def count_table(table: str, where: str = "company_id=?") -> int:
        if not _table_exists(conn, table):
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", (cid,)).fetchone()[0])

    notes = sum(1 for r in ccrs if _blank(r.get("notes")))
    if _table_exists(conn, "legacy_notes"):
        notes += count_table("legacy_notes")
    follow_ups = sum(1 for r in ccrs if _blank(r.get("follow_up_date")))
    appointments = 0
    if _table_exists(conn, "appointments"):
        appointments = count_table("appointments")
    sales = 0
    if _table_exists(conn, "client_sales_events"):
        sales = count_table("client_sales_events")
    if _table_exists(conn, "revenue_milestones"):
        sales += count_table("revenue_milestones")
    research = 0
    if _table_exists(conn, "company_research_jobs"):
        research = count_table("company_research_jobs")
    if _table_exists(conn, "research_import_rows"):
        try:
            research += int(
                conn.execute(
                    "SELECT COUNT(*) FROM research_import_rows WHERE company_id=?",
                    (cid,),
                ).fetchone()[0]
            )
        except Exception:
            pass
    return {
        "notes": notes,
        "activities": count_table("activities"),
        "sales_events": sales,
        "appointments": appointments,
        "research_records": research,
        "campaigns": count_table("campaign_companies") if _table_exists(conn, "campaign_companies") else 0,
        "follow_ups": follow_ups,
    }


def _contact_overlaps(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overlaps: list[dict[str, Any]] = []
    for a in left:
        for b in right:
            klass, reasons = classify_contact_pair(a, b)
            email_a = _blank(a.get("email")).lower()
            email_b = _blank(b.get("email")).lower()
            phone_a = digits_phone(_blank(a.get("phone")))
            phone_b = digits_phone(_blank(b.get("phone")))
            name_a = f"{_blank(a.get('first_name')).lower()} {_blank(a.get('last_name')).lower()}".strip()
            name_b = f"{_blank(b.get('first_name')).lower()} {_blank(b.get('last_name')).lower()}".strip()
            flags: list[str] = []
            if email_a and email_a == email_b:
                flags.append("exact_email")
            if phone_a and phone_b and len(phone_a) >= 7 and phone_a[-7:] == phone_b[-7:]:
                flags.append("exact_phone")
            if name_a and name_a == name_b:
                flags.append("same_name")
            if not flags and klass not in {"MERGE", "POSSIBLE"}:
                continue
            kind = "possible_match"
            if "exact_email" in flags:
                kind = "exact_email"
            elif "exact_phone" in flags:
                kind = "exact_phone"
            elif "same_name" in flags:
                kind = "same_name"
            overlaps.append(
                {
                    "contact_a_id": int(a["id"]),
                    "contact_b_id": int(b["id"]),
                    "kind": kind,
                    "classification": klass,
                    "flags": flags,
                    "reasons": reasons,
                    "name_a": f"{_blank(a.get('first_name'))} {_blank(a.get('last_name'))}".strip(),
                    "name_b": f"{_blank(b.get('first_name'))} {_blank(b.get('last_name'))}".strip(),
                }
            )
    return overlaps


def _same_client_conflicts(
    left_ccrs: list[dict[str, Any]], right_ccrs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_left = {int(r["client_id"]): r for r in left_ccrs}
    conflicts = []
    for rel in right_ccrs:
        other = by_left.get(int(rel["client_id"]))
        if not other:
            continue
        conflicts.append(
            {
                "client_id": int(rel["client_id"]),
                "client_code": _blank(rel.get("client_code") or other.get("client_code")),
                "client_name": _blank(rel.get("client_name") or other.get("client_name")),
                "classification": "CONFLICT",
                "label": "SAME-CLIENT CCR CONFLICT",
                "company_a": {
                    "ccr_id": other.get("ccr_id"),
                    "status": _blank(other.get("status")),
                    "assigned_user_name": _blank(other.get("assigned_user_name")),
                    "assigned_user_id": other.get("assigned_user_id"),
                    "hot": bool(other.get("hot")),
                    "follow_up_date": other.get("follow_up_date"),
                    "next_action": _blank(other.get("next_action")),
                    "external_record_no": _blank(other.get("external_record_no")),
                    "active": bool(other.get("active")),
                    "notes_present": bool(_blank(other.get("notes"))),
                },
                "company_b": {
                    "ccr_id": rel.get("ccr_id"),
                    "status": _blank(rel.get("status")),
                    "assigned_user_name": _blank(rel.get("assigned_user_name")),
                    "assigned_user_id": rel.get("assigned_user_id"),
                    "hot": bool(rel.get("hot")),
                    "follow_up_date": rel.get("follow_up_date"),
                    "next_action": _blank(rel.get("next_action")),
                    "external_record_no": _blank(rel.get("external_record_no")),
                    "active": bool(rel.get("active")),
                    "notes_present": bool(_blank(rel.get("notes"))),
                },
            }
        )
    return conflicts


def _dependency_classification(
    *,
    conflicts: list[dict[str, Any]],
    overlaps: list[dict[str, Any]],
    distinct_site: bool,
    merge_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    labels: list[str] = []
    if conflicts:
        labels.append("CONFLICT")
    if distinct_site:
        labels.append("REQUIRES REVIEW")
    if overlaps:
        labels.append("REQUIRES REVIEW")
    blockers = list((merge_plan or {}).get("blockers") or [])
    warnings = list((merge_plan or {}).get("warnings") or [])
    if blockers:
        labels.append("CONFLICT")
    if warnings:
        labels.append("REQUIRES REVIEW")
    if not labels:
        labels.append("SAFE TO COMBINE")
    unique = list(dict.fromkeys(labels))
    overall = "SAFE TO COMBINE"
    if "CONFLICT" in unique:
        overall = "CONFLICT"
    elif "REQUIRES REVIEW" in unique:
        overall = "REQUIRES REVIEW"
    return {
        "overall": overall,
        "labels": unique,
        "same_client_ccr_conflict": bool(conflicts),
        "contact_overlap": bool(overlaps),
        "multi_location_indicator": distinct_site,
        "merge_plan_blockers": blockers,
        "merge_plan_warnings": warnings,
    }


def get_duplicate_pair_detail(
    conn: sqlite3.Connection,
    company_a_id: int,
    company_b_id: int,
    *,
    planning_survivor_company_id: int | None = None,
) -> dict[str, Any]:
    ensure_duplicate_review_schema(conn)
    lo, hi = pair_ids(company_a_id, company_b_id)
    _require_companies(conn, lo, hi)
    left = _company_detail(conn, lo)
    right = _company_detail(conn, hi)
    discovered = discover_duplicate_pairs(conn)
    found = next((p for p in discovered if p["pair_key"] == f"{lo}:{hi}"), None)
    categories = list((found or {}).get("categories") or [])
    if not categories:
        scored = _score_pair(
            {**left, "id": lo, "legacy_phone": left.get("phone"), "core_name": _core_name(left["company_name"]), "compact_name": compact_company_name(left["company_name"]), "phone_digits": digits_phone(left.get("phone")), "domain": domain(left.get("website")), "addr_norm": norm_addr(left.get("address")), "archived": left.get("archived")},
            {**right, "id": hi, "legacy_phone": right.get("phone"), "core_name": _core_name(right["company_name"]), "compact_name": compact_company_name(right["company_name"]), "phone_digits": digits_phone(right.get("phone")), "domain": domain(right.get("website")), "addr_norm": norm_addr(right.get("address")), "archived": right.get("archived")},
            alias_match=False,
            identity_conflict=False,
        )
        categories = list((scored or {}).get("categories") or [])
        evidence_summary = (scored or {}).get("evidence_summary") or evidence_sentence(categories)
        match_reasons = list((scored or {}).get("reasons") or [])
        distinct_site = bool((scored or {}).get("distinct_site"))
    else:
        evidence_summary = found.get("evidence_summary")
        match_reasons = list(found.get("match_reasons") or [])
        distinct_site = CAT_MULTI_LOCATION in categories
    fingerprint = evidence_fingerprint(conn, lo, hi)
    reviews = _load_reviews(conn)
    review = reviews.get(f"{lo}:{hi}") or {}
    stale = bool(review) and _blank(review.get("evidence_fingerprint")) != fingerprint
    overlaps = _contact_overlaps(left["contacts"], right["contacts"])
    conflicts = _same_client_conflicts(left["ccrs"], right["ccrs"])
    proposed_survivor = review.get("proposed_survivor_company_id")
    proposed_source = review.get("proposed_source_company_id")
    plan_survivor = planning_survivor_company_id or proposed_survivor
    merge_plan = None
    if plan_survivor in {lo, hi}:
        source = hi if int(plan_survivor) == lo else lo
        merge_plan = plan_company_merge(conn, source, int(plan_survivor))
        merge_plan = {
            "source_company_id": merge_plan.get("source_company_id"),
            "survivor_company_id": merge_plan.get("survivor_company_id"),
            "verdict": merge_plan.get("verdict"),
            "blockers": merge_plan.get("blockers") or [],
            "warnings": merge_plan.get("warnings") or [],
            "ccrs": merge_plan.get("ccrs") or {},
            "contacts": {
                "possible_duplicates_requiring_review": (merge_plan.get("contacts") or {}).get(
                    "possible_duplicates_requiring_review"
                ),
                "contact_field_conflicts": (merge_plan.get("contacts") or {}).get(
                    "contact_field_conflicts"
                ),
            },
            "locations": merge_plan.get("locations") or {},
            "aliases": merge_plan.get("aliases") or {},
            "source_identities": merge_plan.get("source_identities") or {},
            "campaigns": merge_plan.get("campaigns") or {},
            "notes": {
                "distinct_notes_to_preserve": (merge_plan.get("notes") or {}).get(
                    "distinct_notes_to_preserve"
                )
            },
            "live_merge_executed": False,
            "planning_only": True,
        }
    classification = _dependency_classification(
        conflicts=conflicts,
        overlaps=overlaps,
        distinct_site=distinct_site,
        merge_plan=merge_plan,
    )
    return {
        "planning_only": True,
        "merge_will_occur": False,
        "automatic_verdict": False,
        "writes": False,
        "pair_key": f"{lo}:{hi}",
        "company_a": left,
        "company_b": right,
        "categories": categories,
        "evidence_summary": evidence_summary,
        "match_reasons": match_reasons,
        "same_client_conflicts": conflicts,
        "contact_overlaps": overlaps,
        "dependency": classification,
        "merge_plan": merge_plan,
        "review": {
            "disposition": _blank(review.get("disposition")) or DISPOSITION_UNREVIEWED,
            "reason": _blank(review.get("review_reason")),
            "reviewed_at": _blank(review.get("reviewed_at")),
            "reviewed_by_user_id": review.get("reviewed_by_user_id"),
            "proposed_survivor_company_id": proposed_survivor,
            "proposed_source_company_id": proposed_source,
            "stale": stale,
            "review_status": "RE_REVIEW_NEEDED" if stale else (
                "UNREVIEWED"
                if not review or _blank(review.get("disposition")) in {"", DISPOSITION_UNREVIEWED}
                else "REVIEWED"
            ),
            "evidence_fingerprint": fingerprint,
            "stored_fingerprint": _blank(review.get("evidence_fingerprint")),
        },
        "plan_only_warning": PLAN_ONLY_WARNING,
        "no_merge_button": True,
    }


def list_review_history(conn: sqlite3.Connection, company_a_id: int, company_b_id: int) -> dict[str, Any]:
    ensure_duplicate_review_schema(conn)
    key = pair_key(company_a_id, company_b_id)
    events = []
    if _table_exists(conn, "company_duplicate_review_events"):
        events = [
            _row_dict(r)
            for r in conn.execute(
                """
                SELECT e.*, COALESCE(u.full_name, '') AS actor_name
                FROM company_duplicate_review_events e
                LEFT JOIN users u ON u.id = e.actor_user_id
                WHERE e.pair_key=?
                ORDER BY e.id DESC
                """,
                (key,),
            ).fetchall()
        ]
    return {"pair_key": key, "events": events, "writes": False, "planning_only": True}


def save_duplicate_review(
    conn: sqlite3.Connection,
    *,
    actor: NorthStarUser,
    company_a_id: int,
    company_b_id: int,
    body: DuplicateReviewSaveRequest,
) -> dict[str, Any]:
    if not bool(getattr(actor, "is_administrator", False)):
        raise PermissionError("Not authorized.")
    ensure_duplicate_review_schema(conn)
    lo, hi = pair_ids(company_a_id, company_b_id)
    _require_companies(conn, lo, hi)
    disposition = _blank(body.disposition).upper()
    if disposition not in REVIEW_DISPOSITIONS:
        raise DuplicateReviewError("invalid_disposition", {"disposition": disposition})
    reason = _blank(body.reason)
    if len(reason) < 3:
        raise DuplicateReviewError("reason_required")
    survivor = body.proposed_survivor_company_id
    source = body.proposed_source_company_id
    if disposition == DISPOSITION_MERGE:
        if survivor is None or source is None:
            raise DuplicateReviewError("survivor_required")
        try:
            survivor_i = int(survivor)
            source_i = int(source)
        except (TypeError, ValueError) as exc:
            raise DuplicateReviewError("survivor_required") from exc
        if survivor_i == source_i:
            raise DuplicateReviewError("source_equals_survivor")
        if {survivor_i, source_i} != {lo, hi}:
            raise DuplicateReviewError("survivor_not_in_pair")
        survivor, source = survivor_i, source_i
    else:
        survivor = None
        source = None
    fingerprint = evidence_fingerprint(conn, lo, hi)
    now = _now()
    existing = conn.execute(
        "SELECT * FROM company_duplicate_reviews WHERE pair_key=?",
        (f"{lo}:{hi}",),
    ).fetchone()
    old = _row_dict(existing)
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO company_duplicate_reviews (
                company_a_id, company_b_id, pair_key, disposition,
                proposed_survivor_company_id, proposed_source_company_id,
                review_reason, reviewed_by_user_id, reviewed_at, created_at,
                updated_at, evidence_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lo,
                hi,
                f"{lo}:{hi}",
                disposition,
                survivor,
                source,
                reason,
                int(actor.id),
                now,
                now,
                now,
                fingerprint,
            ),
        )
        review_id = int(cur.lastrowid)
    else:
        review_id = int(old["id"])
        conn.execute(
            """
            UPDATE company_duplicate_reviews
            SET disposition=?, proposed_survivor_company_id=?, proposed_source_company_id=?,
                review_reason=?, reviewed_by_user_id=?, reviewed_at=?, updated_at=?,
                evidence_fingerprint=?
            WHERE id=?
            """,
            (
                disposition,
                survivor,
                source,
                reason,
                int(actor.id),
                now,
                now,
                fingerprint,
                review_id,
            ),
        )
    conn.execute(
        """
        INSERT INTO company_duplicate_review_events (
            review_id, pair_key, old_disposition, new_disposition,
            old_survivor_company_id, new_survivor_company_id,
            old_source_company_id, new_source_company_id,
            reason, evidence_fingerprint, actor_user_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id,
            f"{lo}:{hi}",
            _blank(old.get("disposition")),
            disposition,
            old.get("proposed_survivor_company_id"),
            survivor,
            old.get("proposed_source_company_id"),
            source,
            reason,
            fingerprint,
            int(actor.id),
            now,
        ),
    )
    from data_steward import ACTION_DUPLICATE_REVIEW, SOURCE_MANUAL_ADMIN, record_provenance

    record_provenance(
        conn,
        entity_type=ENTITY_DUPLICATE_REVIEW,
        entity_id=review_id,
        field="disposition",
        old_value=_blank(old.get("disposition")),
        new_value=disposition,
        source_type=SOURCE_MANUAL_ADMIN,
        action=ACTION_DUPLICATE_REVIEW,
        reason=reason,
        source_ref=f"{lo}:{hi}",
        actor=actor,
    )
    approvals = 0
    if _table_exists(conn, "merge_execution_approvals"):
        approvals = int(conn.execute("SELECT COUNT(*) FROM merge_execution_approvals").fetchone()[0])
    return {
        "ok": True,
        "writes": True,
        "planning_only": True,
        "merge_will_occur": False,
        "approval_created": False,
        "merge_execution_approvals": approvals,
        "plan_only_warning": PLAN_ONLY_WARNING,
        "review_id": review_id,
        "pair_key": f"{lo}:{hi}",
        "disposition": disposition,
        "reason": reason,
        "proposed_survivor_company_id": survivor,
        "proposed_source_company_id": source,
        "evidence_fingerprint": fingerprint,
        "reviewed_at": now,
        "reviewed_by_user_id": int(actor.id),
        "companies_mutated": False,
        "contacts_mutated": False,
        "ccrs_mutated": False,
    }
