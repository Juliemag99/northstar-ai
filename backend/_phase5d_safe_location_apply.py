"""Phase 5D — controlled SAFE location / source-identity backfill.

Additive only. Does not merge companies, change CCRs/contacts/history,
repair Johnson Controls, or activate location-aware matching.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from _phase2b_alias_backfill_preview import REVIEW, build_classified_aliases
from _phase3a_standardization_preview import NO_CHANGE, SAFE as ADDR_SAFE, standardize_street_address
from _phase5a_identity_location_preview import B_LOCATION, classify_review_row
from _phase5c_location_model_preview import (
    LIKELY,
    NOT_LOC,
    SAFE,
    SRC_ONLY,
    UNRESOLVED,
    _city_token,
    classify_5c_row,
)
from company_locations import (
    ensure_company_location,
    ensure_company_location_schema,
    location_site_key,
    sites_match,
    upsert_company_source_identity,
)
from contact_phone import canonical_contact_phone, store_phone_parts
from crm_identity_keys import normalize_record_no
from crm_import_state import state_for_match
from db import PRODUCTION_DB_PATH, get_connection
from import_brown_industries import digits_phone, domain, norm_addr

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "database" / "northstar.db"
OUT = REPO / "working" / "master-data-std-phase5d"

EXPECTED_SAFE = 43
EXPECTED_LIVE = {
    "companies": 1792,
    "contacts": 4936,
    "ccr": 1889,
    "company_aliases": 403,
    "company_identity_keys": 1792,
    "contact_phone_keys": 5114,
    "brown_sales_events": 97,
    "brown_quote_milestones": 26,
}

PROTECTED_COMPANY_IDS = frozenset(
    {551, 757, 85, 192, 215, 383, 336, 337, 670, 298, 425, 484}
)
PROTECTED_RNS = frozenset({"1326674", "1326675", "102072", "1002756", "1218562", "1325571"})
PROTECTED_NAME_FRAGMENTS = (
    "johnson control",
    "parker hannifin",
    "greenheck",
    "watlow",
    "whirlpool",
    "ronson",
)
CA_CODES = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)
PLANT_TYPE_RE = re.compile(r"\b(plant|facility|branch|warehouse)\b", re.I)
HQ_RE = re.compile(r"\b(headquarters|\bhq\b)\b", re.I)
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

Decision = Literal["APPLIED", "DEFERRED"]


def _b(value: object | None) -> str:
    return "" if value is None else str(value).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Candidate:
    company_id: int
    master_name: str
    master_rn: str
    master_address: str
    master_city: str
    master_state: str
    master_zip: str
    master_phone: str
    master_phone_digits: str
    master_website: str
    alias_name: str
    alias_norm: str
    source_system: str
    source_record_no: str
    client_id: int | None
    source_address: str
    source_city: str
    source_state: str
    source_zip: str
    source_phone: str
    source_website: str
    source_batch_id: int | None
    source_row: int | None
    klass5c: str
    why: str
    decision: Decision = "DEFERRED"
    reason: str = ""
    existing_master_id: int | None = None
    existing_master_name: str = ""
    location_dedupe: str = ""
    identity_result: str = ""
    location_id: int | None = None
    identity_id: int | None = None
    location_name: str = ""
    location_type: str = "unknown"
    std_address: str = ""
    std_city: str = ""
    std_state: str = ""
    std_zip: str = ""
    std_phone: str = ""
    std_phone_ext: str = ""
    phone_omitted: str = ""
    country: str = "US"
    site_key: tuple[str, str, str, str] = ("", "", "", "")


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    def _count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    loc_exists = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_locations'"
        ).fetchone()
    )
    ident_exists = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_source_identities'"
        ).fetchone()
    )
    doc = conn.execute(
        "SELECT id, processing_status FROM client_documents WHERE id=19"
    ).fetchone()
    return {
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "companies": _count("SELECT COUNT(*) FROM companies"),
        "contacts": _count("SELECT COUNT(*) FROM contacts"),
        "ccr": _count("SELECT COUNT(*) FROM client_company_relationships"),
        "company_aliases": _count("SELECT COUNT(*) FROM company_aliases"),
        "company_identity_keys": _count("SELECT COUNT(*) FROM company_identity_keys"),
        "contact_phone_keys": _count("SELECT COUNT(*) FROM contact_phone_keys"),
        "brown_sales_events": _count(
            "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        ),
        "brown_quote_milestones": _count(
            """
            SELECT COUNT(*) FROM revenue_milestones
            WHERE client_id=2 AND milestone_type='Quote'
            """
        ),
        "doc19": tuple(doc) if doc else (),
        "company_locations": _count("SELECT COUNT(*) FROM company_locations") if loc_exists else 0,
        "company_source_identities": (
            _count("SELECT COUNT(*) FROM company_source_identities") if ident_exists else 0
        ),
        "location_id_nonnull": {
            "contacts": _count("SELECT COUNT(*) FROM contacts WHERE location_id IS NOT NULL"),
            "ccr": _count(
                "SELECT COUNT(*) FROM client_company_relationships WHERE location_id IS NOT NULL"
            ),
            "history": _count(
                "SELECT COUNT(*) FROM company_shared_history_events WHERE location_id IS NOT NULL"
            ),
            "sales": _count(
                "SELECT COUNT(*) FROM client_sales_events WHERE location_id IS NOT NULL"
            ),
        },
    }


def backup_live(live: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = live.with_name(f"northstar.db.bak-master-data-std-phase5d-{stamp}")
    src = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    try:
        src.execute("PRAGMA query_only=ON")
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    chk = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    try:
        integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if integrity != "ok":
        raise SystemExit(f"Backup integrity not ok: {integrity}")
    return dest


def parse_place(source_city: str, source_state: str, source_zip: str) -> tuple[str, str, str]:
    raw_city = _b(source_city)
    city_display = raw_city.split(",", 1)[0].strip()
    city_display = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", city_display).strip()
    if city_display == city_display.lower() or city_display == city_display.upper():
        city_display = city_display.title()
    city_display = city_display.replace("''", "'")
    zip_code = "".join(ch for ch in _b(source_zip) if ch.isdigit())[:5]
    if not zip_code:
        hit = ZIP_RE.search(raw_city) or ZIP_RE.search(_b(source_state))
        if hit:
            zip_code = hit.group(1)
    state = state_for_match(source_state) or ""
    if not state and "," in raw_city:
        tail = raw_city.split(",", 1)[1]
        tail = ZIP_RE.sub("", tail).strip(" ,")
        state = state_for_match(tail) or ""
    return city_display, state, zip_code


def standardize_address(raw: str) -> str:
    text = _b(raw)
    if not text:
        return ""
    proposed, klass, _reason = standardize_street_address(text)
    if klass in {ADDR_SAFE, NO_CHANGE} and proposed:
        return proposed
    return text


def location_name_and_type(alias_name: str, city: str) -> tuple[str, str]:
    blob = _b(alias_name)
    city_name = _b(city)
    if not city_name:
        return "", "unknown"
    if HQ_RE.search(blob):
        return city_name, "unknown"
    if re.search(r"\bplant\b", blob, re.I):
        return f"{city_name} Plant", "plant"
    if re.search(r"\bfacility\b", blob, re.I):
        return f"{city_name} Facility", "facility"
    if re.search(r"\bwarehouse\b", blob, re.I):
        return f"{city_name} Warehouse", "warehouse"
    if re.search(r"\bbranch\b", blob, re.I):
        return f"{city_name} Branch", "branch"
    return city_name, "unknown"


def format_location_phone(raw: str, master_digits: str, *, city_differs: bool) -> tuple[str, str, str]:
    """Return (phone, extension, omit_reason). Omit contaminated or untrusted phones."""
    text = _b(raw)
    if not text:
        return "", "", ""
    src_digits = digits_phone(text)
    if city_differs and master_digits and src_digits and src_digits == master_digits:
        return "", "", "omitted: source phone matches organization master phone at a different city"
    if "/" in text or " or " in text.lower() or "\xa0" in text:
        return "", "", "omitted: multiple or ambiguous source phone values"
    main, ext = store_phone_parts(text)
    nanp, _last7 = canonical_contact_phone(main or text)
    if len(nanp) != 10:
        return "", "", "omitted: not a trusted 10-digit NANP phone"
    if not re.fullmatch(r"\(\d{3}\) \d{3}-\d{4}", main):
        return "", "", "omitted: not a single trusted NANP display phone"
    return main, ext, ""


def recompute_safe_candidates(conn: sqlite3.Connection) -> tuple[list[Candidate], dict[str, int], int]:
    rows_2b, masters, _ccrs, _ccrs_by = build_classified_aliases(conn)
    review = [r for r in rows_2b if r.classification == REVIEW]
    rn_owners = {m["rn"]: m["id"] for m in masters.values() if m["rn"]}
    bucket_counts: dict[str, int] = Counter()
    safe: list[Candidate] = []
    for row in review:
        master = masters[row.company_id]
        klass5a = classify_review_row(row, master, rn_owners)
        if klass5a != B_LOCATION:
            continue
        klass5c, why = classify_5c_row(row, master, rn_owners, masters)
        bucket_counts[klass5c] += 1
        if klass5c != SAFE:
            continue
        cand = Candidate(
            company_id=int(row.company_id),
            master_name=master["name"],
            master_rn=master["rn"],
            master_address=master["address"],
            master_city=master["city"],
            master_state=master["state"],
            master_zip=master.get("zip") or "",
            master_phone=master.get("phone") or "",
            master_phone_digits=master.get("phone") or "",
            master_website=master.get("website") or "",
            alias_name=row.alias_name,
            alias_norm=row.alias_norm,
            source_system=row.source_system,
            source_record_no=normalize_record_no(row.source_record_no),
            client_id=row.client_id,
            source_address=_b(row.source_address),
            source_city=_b(row.source_city),
            source_state=_b(row.source_state),
            source_zip=_b(row.source_zip),
            source_phone=_b(row.source_phone),
            source_website=_b(row.source_website),
            source_batch_id=row.source_batch_id,
            source_row=row.source_row,
            klass5c=klass5c,
            why=why,
        )
        if not cand.master_phone_digits:
            cand.master_phone_digits = digits_phone(master.get("phone") or "")
        else:
            cand.master_phone_digits = digits_phone(cand.master_phone_digits)
        safe.append(cand)
    return safe, dict(bucket_counts), len(review)


def load_company_sites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for row in conn.execute(
        """
        SELECT c.id, c.company_name, c.address, c.city, c.state, c.zip,
               k.addr_norm, k.city_norm, k.state_norm
        FROM companies c
        LEFT JOIN company_identity_keys k ON k.company_id = c.id
        """
    ):
        addr = _b(row["addr_norm"]) or norm_addr(_b(row["address"]))
        city = _city_token(row["city_norm"] or row["city"])
        state = state_for_match(row["state_norm"] or row["state"]) or _b(row["state"]).upper()[:2]
        zip5 = "".join(ch for ch in _b(row["zip"]) if ch.isdigit())[:5]
        rows.append(
            {
                "id": int(row["id"]),
                "name": _b(row["company_name"]),
                "addr": addr,
                "city": city,
                "state": state,
                "zip": zip5,
            }
        )
    return rows


def find_existing_master_at_site(
    sites: list[dict[str, Any]],
    *,
    company_id: int,
    address: str,
    city: str,
    state: str,
    zip_code: str,
) -> dict[str, Any] | None:
    wanted_addr = norm_addr(address)
    wanted_city = _city_token(city)
    wanted_state = (state_for_match(state) or _b(state).upper()[:2]).upper()
    wanted_zip = "".join(ch for ch in _b(zip_code) if ch.isdigit())[:5]
    if not wanted_addr or not wanted_city or not wanted_state:
        return None
    for site in sites:
        if int(site["id"]) == int(company_id):
            continue
        if not site["addr"]:
            continue
        if site["addr"] != wanted_addr:
            continue
        city_ok = bool(site["city"] and site["city"] == wanted_city)
        state_ok = bool(site["state"] and site["state"][:2].upper() == wanted_state[:2])
        zip_ok = bool(wanted_zip and site["zip"] and site["zip"] == wanted_zip)
        if (city_ok and state_ok) or zip_ok:
            return site
    return None


def is_protected_org(cand: Candidate) -> bool:
    if cand.company_id in PROTECTED_COMPANY_IDS:
        return True
    if cand.source_record_no in PROTECTED_RNS:
        return True
    blob = f"{cand.master_name} {cand.alias_name}".lower()
    return any(frag in blob for frag in PROTECTED_NAME_FRAGMENTS)


def prepare_candidate(cand: Candidate) -> None:
    city, state, zip_code = parse_place(cand.source_city, cand.source_state, cand.source_zip)
    address = standardize_address(cand.source_address)
    cand.std_city = city
    cand.std_state = state
    cand.std_zip = zip_code
    cand.std_address = address
    cand.country = "CA" if state in CA_CODES else "US"
    name, kind = location_name_and_type(cand.alias_name, city)
    cand.location_name = name
    cand.location_type = kind
    city_differs = bool(
        _city_token(city) and _city_token(cand.master_city) and _city_token(city) != _city_token(cand.master_city)
    )
    phone, ext, omit = format_location_phone(
        cand.source_phone, cand.master_phone_digits, city_differs=city_differs
    )
    cand.std_phone = phone
    cand.std_phone_ext = ext
    cand.phone_omitted = omit
    cand.site_key = location_site_key(address, city, state, zip_code)


def evaluate_candidate(cand: Candidate, sites: list[dict[str, Any]]) -> None:
    prepare_candidate(cand)
    if is_protected_org(cand):
        cand.decision = "DEFERRED"
        cand.reason = "Phase 5C case-study organization protected from live restructuring"
        return
    if not cand.source_record_no or not cand.source_system:
        cand.decision = "DEFERRED"
        cand.reason = "Missing source identity/RN"
        return
    if not cand.std_address or not cand.std_city or not cand.std_state:
        cand.decision = "DEFERRED"
        cand.reason = "Location requires street, city, and state after normalization"
        return
    other = find_existing_master_at_site(
        sites,
        company_id=cand.company_id,
        address=cand.std_address,
        city=cand.std_city,
        state=cand.std_state,
        zip_code=cand.std_zip,
    )
    if other is not None:
        cand.decision = "DEFERRED"
        cand.existing_master_id = int(other["id"])
        cand.existing_master_name = other["name"]
        cand.reason = (
            f"Existing master company requires future controlled absorb/merge "
            f"(company {other['id']} {other['name']})"
        )
        return
    cand.decision = "APPLIED"
    cand.reason = cand.why


def apply_approved(
    conn: sqlite3.Connection,
    candidates: list[Candidate],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    stats = {
        "locations_created": 0,
        "locations_reused": 0,
        "identities_created": 0,
        "identities_reused": 0,
        "identities_conflict": 0,
        "identities_deferred": 0,
        "multi_identity_sites": 0,
    }
    approved = [c for c in candidates if c.decision == "APPLIED"]
    groups: dict[tuple, list[Candidate]] = defaultdict(list)
    for cand in approved:
        groups[(cand.company_id, cand.site_key)].append(cand)
    stats["multi_identity_sites"] = sum(1 for items in groups.values() if len(items) > 1)
    location_ids: dict[tuple, int] = {}
    if dry_run:
        return stats

    ensure_company_location_schema(conn)
    for key, items in groups.items():
        viable: list[Candidate] = []
        for cand in items:
            existing = conn.execute(
                """
                SELECT id, company_id, location_id FROM company_source_identities
                WHERE source_system = ?
                  AND source_record_no = ?
                  AND COALESCE(client_id, 0) = COALESCE(?, 0)
                LIMIT 1
                """,
                (cand.source_system, cand.source_record_no, cand.client_id),
            ).fetchone()
            if existing is not None and int(existing["company_id"]) != int(cand.company_id):
                cand.decision = "DEFERRED"
                cand.identity_result = "conflict"
                cand.identity_id = int(existing["id"])
                cand.reason = "Source identity conflicts with an existing company/location"
                stats["identities_conflict"] += 1
                stats["identities_deferred"] += 1
                continue
            if existing is not None and existing["location_id"] is not None:
                # Reuse path handled by upsert; keep viable.
                pass
            viable.append(cand)
        if not viable:
            continue
        lead = viable[0]
        loc_id, loc_status = ensure_company_location(
            conn,
            company_id=lead.company_id,
            location_name=lead.location_name,
            location_type=lead.location_type,
            address=lead.std_address,
            city=lead.std_city,
            state=lead.std_state,
            zip_code=lead.std_zip,
            country=lead.country,
            phone=lead.std_phone,
            phone_extension=lead.std_phone_ext,
            website=_b(lead.source_website),
            is_headquarters=0,
            is_primary=0,
            source_system="phase5d_safe_location_backfill",
        )
        location_ids[key] = loc_id
        if loc_status == "created":
            stats["locations_created"] += 1
        else:
            stats["locations_reused"] += 1
        for cand in viable:
            cand.location_id = loc_id
            if not cand.location_dedupe:
                cand.location_dedupe = loc_status
            ident_id, ident_status = upsert_company_source_identity(
                conn,
                company_id=cand.company_id,
                location_id=loc_id,
                client_id=cand.client_id,
                source_system=cand.source_system,
                source_record_no=cand.source_record_no,
                source_company_name=cand.alias_name,
                source_address=cand.source_address,
                source_city=cand.source_city,
                source_state=cand.source_state,
                source_zip=cand.source_zip,
                source_phone=cand.source_phone,
                source_website=cand.source_website,
                source_batch_id=cand.source_batch_id,
                source_row=cand.source_row,
            )
            cand.identity_id = ident_id
            if ident_status == "conflict" or not cand.identity_result:
                cand.identity_result = ident_status
            if ident_status == "created":
                stats["identities_created"] += 1
            elif ident_status == "existing":
                stats["identities_reused"] += 1
            else:
                stats["identities_conflict"] += 1
                cand.decision = "DEFERRED"
                cand.reason = "Source identity conflicts with an existing company/location"
                stats["identities_deferred"] += 1
    return stats


def protected_unchanged(conn: sqlite3.Connection, before: dict[str, tuple]) -> list[str]:
    problems = []
    for cid, prior in before.items():
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no, address, city, state, zip,
                   legacy_phone, website
            FROM companies WHERE id=?
            """,
            (cid,),
        ).fetchone()
        if row is None:
            problems.append(f"missing company {cid}")
            continue
        now = tuple(row)
        if now != prior:
            problems.append(f"company {cid} changed")
    return problems


def load_protected_snapshot(conn: sqlite3.Connection) -> dict[int, tuple]:
    out: dict[int, tuple] = {}
    for cid in sorted(PROTECTED_COMPANY_IDS):
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no, address, city, state, zip,
                   legacy_phone, website
            FROM companies WHERE id=?
            """,
            (cid,),
        ).fetchone()
        if row is not None:
            out[cid] = tuple(row)
    return out


def write_audit(path: Path, candidates: list[Candidate]) -> None:
    headers = [
        "source_identity",
        "organization_company_id",
        "master_name",
        "source_rn",
        "source_company_name",
        "source_system",
        "client_id",
        "source_address",
        "source_city",
        "source_state",
        "source_zip",
        "source_phone",
        "proposed_location_name",
        "proposed_location_type",
        "std_address",
        "std_city",
        "std_state",
        "std_zip",
        "std_phone",
        "phone_omitted",
        "classification",
        "existing_master_company_check",
        "location_dedupe_result",
        "identity_conflict_result",
        "APPLIED_OR_DEFERRED",
        "reason",
        "created_location_id",
        "created_or_reused_source_identity_id",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for cand in candidates:
            existing = ""
            if cand.existing_master_id:
                existing = f"{cand.existing_master_id} {cand.existing_master_name}"
            elif cand.decision == "APPLIED":
                existing = "none"
            writer.writerow(
                [
                    f"{cand.source_system}:{cand.source_record_no}",
                    cand.company_id,
                    cand.master_name,
                    cand.source_record_no,
                    cand.alias_name,
                    cand.source_system,
                    cand.client_id if cand.client_id is not None else "",
                    cand.source_address,
                    cand.source_city,
                    cand.source_state,
                    cand.source_zip,
                    cand.source_phone,
                    cand.location_name,
                    cand.location_type,
                    cand.std_address,
                    cand.std_city,
                    cand.std_state,
                    cand.std_zip,
                    cand.std_phone,
                    cand.phone_omitted,
                    cand.klass5c,
                    existing,
                    cand.location_dedupe,
                    cand.identity_result,
                    cand.decision,
                    cand.reason,
                    cand.location_id if cand.location_id is not None else "",
                    cand.identity_id if cand.identity_id is not None else "",
                ]
            )


def validate_live(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    snap = snapshot(conn)
    if snap["integrity"] != "ok":
        problems.append(f"integrity {snap['integrity']}")
    for key, expected in EXPECTED_LIVE.items():
        if snap.get(key) != expected:
            problems.append(f"{key} expected {expected} got {snap.get(key)}")
    for label, n in snap["location_id_nonnull"].items():
        if n != 0:
            problems.append(f"{label} location_id non-null={n}")
    if tuple(snap.get("doc19") or ())[:2] != (19, "uploaded"):
        problems.append(f"doc19 {snap.get('doc19')}")

    dup_loc = conn.execute(
        """
        SELECT company_id, lower(trim(address)), lower(trim(city)), upper(trim(state)), COUNT(*)
        FROM company_locations
        GROUP BY 1, 2, 3, 4
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if dup_loc:
        problems.append(f"duplicate locations {len(dup_loc)}")

    dup_ident = conn.execute(
        """
        SELECT source_system, source_record_no, COALESCE(client_id, 0), COUNT(*)
        FROM company_source_identities
        GROUP BY 1, 2, 3
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if dup_ident:
        problems.append(f"duplicate identities {len(dup_ident)}")

    hq = conn.execute(
        """
        SELECT company_id, COUNT(*) FROM company_locations
        WHERE is_headquarters=1 GROUP BY company_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    prim = conn.execute(
        """
        SELECT company_id, COUNT(*) FROM company_locations
        WHERE is_primary=1 GROUP BY company_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    if hq:
        problems.append(f"multiple HQ {len(hq)}")
    if prim:
        problems.append(f"multiple primary {len(prim)}")

    orphans = conn.execute(
        """
        SELECT COUNT(*) FROM company_source_identities i
        LEFT JOIN companies c ON c.id = i.company_id
        WHERE c.id IS NULL
        """
    ).fetchone()[0]
    if orphans:
        problems.append(f"identity missing company {orphans}")
    bad_loc = conn.execute(
        """
        SELECT COUNT(*) FROM company_source_identities i
        JOIN company_locations l ON l.id = i.location_id
        WHERE l.company_id != i.company_id
        """
    ).fetchone()[0]
    if bad_loc:
        problems.append(f"identity location company mismatch {bad_loc}")
    return problems


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sha_before = _sha256(DB)

    backup_path = backup_live(DB)
    with sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True) as bconn:
        backup_integrity = bconn.execute("PRAGMA integrity_check").fetchone()[0]

    conn = get_connection(PRODUCTION_DB_PATH)
    try:
        pre = snapshot(conn)
        if pre["integrity"] != "ok":
            raise SystemExit(json.dumps({"error": "live integrity not ok", "pre": pre}, indent=2, default=str))
        for key, expected in EXPECTED_LIVE.items():
            if pre.get(key) != expected:
                print(json.dumps({"warning": "count differs", "key": key, "expected": expected, "actual": pre.get(key)}))
        if pre["company_locations"] != 0 or pre["company_source_identities"] != 0:
            raise SystemExit("Refusing to apply: live locations/identities are not empty")
        protected_before = load_protected_snapshot(conn)

        safe, bucket_counts, review_n = recompute_safe_candidates(conn)
        if len(safe) != EXPECTED_SAFE:
            print(json.dumps({
                "warning": "recomputed SAFE count differs from 43",
                "recomputed": len(safe),
                "expected": EXPECTED_SAFE,
                "buckets": bucket_counts,
            }, indent=2))
            if abs(len(safe) - EXPECTED_SAFE) > 5:
                raise SystemExit("SAFE count discrepancy cannot be explained; STOP before writes")

        sites = load_company_sites(conn)
        for cand in safe:
            evaluate_candidate(cand, sites)

        deferred_master = [c for c in safe if "Existing master company" in c.reason]
        deferred_other = [c for c in safe if c.decision == "DEFERRED" and c not in deferred_master]
        approved = [c for c in safe if c.decision == "APPLIED"]

        stats = apply_approved(conn, safe, dry_run=False)
        conn.commit()
        post = snapshot(conn)
        problems = validate_live(conn)
        protected_problems = protected_unchanged(conn, protected_before)
        problems.extend(protected_problems)

        loc_types = dict(
            conn.execute(
                "SELECT location_type, COUNT(*) FROM company_locations GROUP BY location_type"
            ).fetchall()
        )
        hq_n = int(conn.execute("SELECT COUNT(*) FROM company_locations WHERE is_headquarters=1").fetchone()[0])
        prim_n = int(conn.execute("SELECT COUNT(*) FROM company_locations WHERE is_primary=1").fetchone()[0])

        # Idempotent rerun — previously APPLIED rows must reuse, not insert.
        rerun_stats = apply_approved(conn, safe, dry_run=False)
        conn.commit()
        if rerun_stats["locations_created"] or rerun_stats["identities_created"]:
            problems.append(f"idempotent rerun wrote data: {rerun_stats}")
        post_rerun = snapshot(conn)
    finally:
        conn.close()

    sha_after = _sha256(DB)
    audit_csv = OUT / "phase5d_safe_location_backfill.csv"
    write_audit(audit_csv, safe)

    applied = [c for c in safe if c.decision == "APPLIED" and c.identity_result != "conflict"]
    omitted_phones = [c for c in applied if c.phone_omitted]
    summary = {
        "backup": str(backup_path),
        "backup_integrity": backup_integrity,
        "sha_before": sha_before,
        "sha_after": sha_after,
        "pre": pre,
        "post": post,
        "post_rerun": post_rerun,
        "review_n": review_n,
        "recomputed_safe": len(safe),
        "buckets": bucket_counts,
        "deferred_existing_master": len(deferred_master),
        "deferred_other": len(deferred_other),
        "applied": len(applied),
        "stats": stats,
        "rerun_stats": rerun_stats,
        "location_types": loc_types,
        "hq": hq_n,
        "primary": prim_n,
        "phones_omitted": len(omitted_phones),
        "validation_problems": problems,
        "audit_csv": str(audit_csv),
        "generated_at": _now(),
    }
    (OUT / "phase5d_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "backup": str(backup_path),
        "recomputed_safe": len(safe),
        "buckets": bucket_counts,
        "deferred_existing_master": len(deferred_master),
        "deferred_other": len(deferred_other),
        "applied": len(applied),
        "stats": stats,
        "rerun_stats": rerun_stats,
        "locations": post["company_locations"],
        "identities": post["company_source_identities"],
        "problems": problems,
        "sha_identical_file": sha_before == sha_after,
    }, indent=2, default=str))
    if problems:
        raise SystemExit("Phase 5D validation problems: " + "; ".join(problems))


if __name__ == "__main__":
    main()
