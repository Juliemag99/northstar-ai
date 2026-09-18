"""Phase 3B — SAFE master-data standardization apply.

Applies only Phase 3A SAFE AUTO-STANDARDIZE rows.
Does not process REVIEW. Does not merge. Does not change CCR RNs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from company_aliases import upsert_company_alias
from contact_phone import format_us_phone_display, split_phone_extension
from crm_identity_keys import (
    company_identity_tuple,
    reconcile_identity_key_counts,
    upsert_company_identity_key,
)
from db import PRODUCTION_DB_PATH, get_connection
from _phase3a_standardization_preview import (
    NO_CHANGE,
    REVIEW,
    SAFE,
    _snapshot,
    classify_live_dataset,
    standardize_street_address,
    strip_trailing_legal_suffixes,
)

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "database" / "northstar.db"
OUT_DIR = REPO / "working" / "master-data-std-phase3b"
SOURCE_SYSTEM = "master_standardization"

EXPECTED_LIVE = {
    "companies": 1792,
    "contacts": 4936,
    "ccr": 1889,
    "company_aliases": 174,
    "brown_sales_events": 97,
    "brown_quote_milestones": 26,
}
EXPECTED_3A = {
    "names": {SAFE: 246, REVIEW: 41, NO_CHANGE: 1505},
    "addresses": {SAFE: 145, REVIEW: 15, NO_CHANGE: 1632},
    "company_phones": {SAFE: 256, REVIEW: 20, NO_CHANGE: 1907},
    "contact_phones": {SAFE: 691, REVIEW: 71, NO_CHANGE: 4400},
}
PROTECTED_NAME_NEEDLES = (
    "johnson controls (north america hq)",
    "spx cooling tech",
    "heat & control-galesburg",
    "fanuc america",
    "haarslev /",
    "kelvion (formerly",
    "parsons co inc",
    "parsons company",
)
STREET_TRAPS = {
    "1900 North St",
    "715 South St",
    "93 East Ave",
    "1525 E North St",
}
_EXT_HINT = re.compile(r"(?i)(?:\bext(?:ension)?\b|\bxt\b|(?<![a-z])x(?=\s*\d)|#)")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backup(live: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = live.with_name(f"northstar.db.bak-master-data-std-phase3b-{stamp}")
    src = sqlite3.connect(str(live))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def partition_safe_phone(raw: str) -> tuple[str, str]:
    """Return (format|defer_ext|defer_other, proposed_display)."""
    text = _blank(raw)
    if not text:
        return "defer_other", ""
    _main, ext = split_phone_extension(text)
    if ext:
        return "defer_ext", ""
    if _EXT_HINT.search(text):
        return "defer_ext", ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 11 and digits.startswith("1"):
        if len(digits) != 11:
            return "defer_ext", ""
        core = digits[1:]
    else:
        if len(digits) != 10:
            return "defer_other", ""
        core = digits
    if len(core) != 10:
        return "defer_other", ""
    proposed = f"({core[:3]}) {core[3:6]}-{core[6:]}"
    formatted = format_us_phone_display(text)
    if formatted != proposed:
        return "defer_other", ""
    if re.sub(r"\D", "", formatted) != core:
        return "defer_other", ""
    return "format", proposed


def _protected_name(name: str) -> bool:
    lower = name.lower()
    return any(needle in lower for needle in PROTECTED_NAME_NEEDLES)


def _counts_ok(classified: dict) -> tuple[bool, dict]:
    actual = {
        "names": dict(classified["name_counts"]),
        "addresses": dict(classified["addr_counts"]),
        "company_phones": dict(classified["cphone_counts"]),
        "contact_phones": dict(classified["tphone_counts"]),
    }
    expected_match = True
    for group, expected in EXPECTED_3A.items():
        for klass, count in expected.items():
            if int(actual[group].get(klass, 0)) != count:
                expected_match = False
    return expected_match, actual


def _fingerprint_review(classified: dict) -> dict:
    return {
        "names": sorted(
            (r["id"], r["current"])
            for r in classified["name_rows"]
            if r["classification"] == REVIEW
        ),
        "addresses": sorted(
            (r["id"], r["current"])
            for r in classified["addr_rows"]
            if r["classification"] == REVIEW
        ),
        "company_phones": sorted(
            (r["id"], r["field"], r["current"])
            for r in classified["company_phone_rows"]
            if r["classification"] == REVIEW
        ),
        "contact_phones": sorted(
            (r["contact_id"], r["field"], r["current"])
            for r in classified["contact_phone_rows"]
            if r["classification"] == REVIEW
        ),
    }


def _protected_live(conn: sqlite3.Connection) -> dict:
    companies = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT id, company_name, external_record_no, address, city, state
            FROM companies WHERE id IN (551, 757, 298)
            ORDER BY id
            """
        )
    ]
    ccrs = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT company_id, client_id, external_record_no
            FROM client_company_relationships
            WHERE (company_id=551 AND client_id=2) OR (company_id=298 AND client_id=2)
            ORDER BY company_id, client_id
            """
        )
    ]
    return {
        "companies": companies,
        "ccrs": ccrs,
        "york_aliases": conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_record_no='1326674'"
        ).fetchone()[0],
        "whirlpool_1325571": conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_record_no='1325571'"
        ).fetchone()[0],
        "altec_sites": conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_record_no BETWEEN '1325877' AND '1325885'"
        ).fetchone()[0],
        "evapco": conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_record_no IN ('1326665','1326666','1326667')"
        ).fetchone()[0],
        "clayco": conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_record_no IN ('1374192','1388070','1388071')"
        ).fetchone()[0],
    }


def _audit_row(*, entity_type: str, entity_id: int, field: str, old: str, new: str, classification: str, reason: str) -> dict:
    return {
        "Entity Type": entity_type,
        "Entity ID": entity_id,
        "Field": field,
        "Old Value": old,
        "New Value": new,
        "Phase 3A Classification": classification,
        "Reason": reason,
        "Timestamp": _now(),
    }


def apply_once(conn: sqlite3.Connection, classified: dict, audit: list[dict]) -> dict:
    stats = {
        "aliases_created": 0,
        "aliases_existing": 0,
        "names_changed": 0,
        "names_skipped_unprotected_alias": 0,
        "addresses_changed": 0,
        "company_phones_formatted": 0,
        "company_phones_defer_ext": 0,
        "company_phones_defer_other": 0,
        "contact_phones_formatted": 0,
        "contact_phones_defer_ext": 0,
        "contact_phones_defer_other": 0,
    }
    affected_companies: set[int] = set()

    for item in classified["name_rows"]:
        if item["classification"] != SAFE:
            continue
        if _protected_name(item["current"]) or _protected_name(item["proposed"]):
            continue
        current = item["current"]
        proposed = item["proposed"]
        if current == proposed:
            continue
        live = conn.execute(
            """
            SELECT id, company_name, external_record_no, address, city, state, zip,
                   website, legacy_phone
            FROM companies WHERE id=?
            """,
            (item["id"],),
        ).fetchone()
        if live is None or _blank(live["company_name"]) != current:
            stats["names_skipped_unprotected_alias"] += 1
            continue
        current_l = current.lower()
        already = conn.execute(
            """
            SELECT 1 FROM company_aliases
            WHERE company_id=? AND lower(trim(alias_name))=?
            LIMIT 1
            """,
            (item["id"], current_l),
        ).fetchone()
        if already is None:
            alias_id, created = upsert_company_alias(
                conn,
                company_id=item["id"],
                alias_name=current,
                source_system=SOURCE_SYSTEM,
                source_record_no=_blank(live["external_record_no"]),
                client_id=None,
                source_address=_blank(live["address"]),
                source_city=_blank(live["city"]),
                source_state=_blank(live["state"]),
                source_zip=_blank(live["zip"]),
                source_phone=_blank(live["legacy_phone"]),
                source_website=_blank(live["website"]),
            )
            if alias_id is None:
                stats["names_skipped_unprotected_alias"] += 1
                continue
            if created:
                stats["aliases_created"] += 1
            else:
                stats["aliases_existing"] += 1
            still = conn.execute(
                """
                SELECT 1 FROM company_aliases
                WHERE company_id=? AND lower(trim(alias_name))=?
                LIMIT 1
                """,
                (item["id"], current_l),
            ).fetchone()
            if still is None:
                stats["names_skipped_unprotected_alias"] += 1
                continue
        conn.execute(
            """
            UPDATE companies
            SET company_name=?, last_updated_at=datetime('now')
            WHERE id=? AND company_name=?
            """,
            (proposed, item["id"], current),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            stats["names_skipped_unprotected_alias"] += 1
            continue
        stats["names_changed"] += 1
        affected_companies.add(item["id"])
        audit.append(
            _audit_row(
                entity_type="company",
                entity_id=item["id"],
                field="company_name",
                old=current,
                new=proposed,
                classification=SAFE,
                reason=item["reason"],
            )
        )

    for item in classified["addr_rows"]:
        if item["classification"] != SAFE:
            continue
        if item["current"] in STREET_TRAPS or item["proposed"] in STREET_TRAPS:
            continue
        if not item["proposed"] or item["proposed"] == item["current"]:
            continue
        conn.execute(
            """
            UPDATE companies
            SET address=?, last_updated_at=datetime('now')
            WHERE id=? AND address=?
            """,
            (item["proposed"], item["id"], item["current"]),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            continue
        stats["addresses_changed"] += 1
        affected_companies.add(item["id"])
        audit.append(
            _audit_row(
                entity_type="company",
                entity_id=item["id"],
                field="address",
                old=item["current"],
                new=item["proposed"],
                classification=SAFE,
                reason=item["reason"],
            )
        )

    for item in classified["company_phone_rows"]:
        if item["classification"] != SAFE:
            continue
        action, proposed = partition_safe_phone(item["current"])
        if action == "defer_ext":
            stats["company_phones_defer_ext"] += 1
            continue
        if action != "format" or not proposed or proposed == item["current"]:
            if action != "format":
                stats["company_phones_defer_other"] += 1
            continue
        conn.execute(
            f"""
            UPDATE companies
            SET {item['field']}=?, last_updated_at=datetime('now')
            WHERE id=? AND {item['field']}=?
            """,
            (proposed, item["id"], item["current"]),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            continue
        stats["company_phones_formatted"] += 1
        if item["field"] == "legacy_phone":
            affected_companies.add(item["id"])
        audit.append(
            _audit_row(
                entity_type="company",
                entity_id=item["id"],
                field=item["field"],
                old=item["current"],
                new=proposed,
                classification=SAFE,
                reason="US/Canadian 10-digit number, no extension.",
            )
        )

    for item in classified["contact_phone_rows"]:
        if item["classification"] != SAFE:
            continue
        action, proposed = partition_safe_phone(item["current"])
        if action == "defer_ext":
            stats["contact_phones_defer_ext"] += 1
            continue
        if action != "format" or not proposed or proposed == item["current"]:
            if action != "format":
                stats["contact_phones_defer_other"] += 1
            continue
        conn.execute(
            f"""
            UPDATE contacts
            SET {item['field']}=?
            WHERE id=? AND {item['field']}=?
            """,
            (proposed, item["contact_id"], item["current"]),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            continue
        stats["contact_phones_formatted"] += 1
        audit.append(
            _audit_row(
                entity_type="contact",
                entity_id=item["contact_id"],
                field=item["field"],
                old=item["current"],
                new=proposed,
                classification=SAFE,
                reason="US/Canadian 10-digit number, no extension.",
            )
        )

    for cid in sorted(affected_companies):
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no, website, legacy_phone,
                   address, city, state
            FROM companies WHERE id=?
            """,
            (cid,),
        ).fetchone()
        if row is None:
            continue
        upsert_company_identity_key(
            conn,
            cid,
            external_record_no=row["external_record_no"],
            company_name=row["company_name"],
            website=row["website"],
            legacy_phone=row["legacy_phone"],
            address=row["address"],
            city=row["city"],
            state=row["state"],
        )
    stats["identity_companies_refreshed"] = len(affected_companies)
    return stats


def _verify_identity(conn: sqlite3.Connection, company_ids: list[int]) -> dict:
    stale = []
    for cid in company_ids:
        company = conn.execute(
            """
            SELECT company_name, external_record_no, website, legacy_phone,
                   address, city, state
            FROM companies WHERE id=?
            """,
            (cid,),
        ).fetchone()
        key = conn.execute(
            "SELECT * FROM company_identity_keys WHERE company_id=?",
            (cid,),
        ).fetchone()
        if company is None or key is None:
            stale.append({"company_id": cid, "error": "missing company or key"})
            continue
        expected = company_identity_tuple(
            external_record_no=company["external_record_no"],
            company_name=company["company_name"],
            website=company["website"],
            legacy_phone=company["legacy_phone"],
            address=company["address"],
            city=company["city"],
            state=company["state"],
        )
        for field in expected:
            if _blank(key[field]) != expected[field]:
                stale.append(
                    {
                        "company_id": cid,
                        "field": field,
                        "stored": _blank(key[field]),
                        "expected": expected[field],
                    }
                )
    recon = reconcile_identity_key_counts(conn)
    return {"stale": stale[:20], "stale_count": len(stale), "reconcile": recon}


def _diff_changes(backup: Path, live_conn: sqlite3.Connection) -> list[dict]:
    bak = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
    bak.row_factory = sqlite3.Row
    bak.execute("PRAGMA query_only = ON")
    audit: list[dict] = []
    old_companies = {
        int(row["id"]): row
        for row in bak.execute(
            """
            SELECT id, company_name, address, legacy_phone, legacy_alt_phone, legacy_mobile
            FROM companies
            """
        )
    }
    for row in live_conn.execute(
        """
        SELECT id, company_name, address, legacy_phone, legacy_alt_phone, legacy_mobile
        FROM companies
        """
    ):
        cid = int(row["id"])
        old = old_companies[cid]
        for field, label in (
            ("company_name", "company_name"),
            ("address", "address"),
            ("legacy_phone", "legacy_phone"),
            ("legacy_alt_phone", "legacy_alt_phone"),
            ("legacy_mobile", "legacy_mobile"),
        ):
            if _blank(old[field]) != _blank(row[field]):
                audit.append(
                    _audit_row(
                        entity_type="company",
                        entity_id=cid,
                        field=label,
                        old=_blank(old[field]),
                        new=_blank(row[field]),
                        classification=SAFE,
                        reason="Phase 3B SAFE apply",
                    )
                )
    old_contacts = {
        int(row["id"]): row
        for row in bak.execute("SELECT id, phone, alt_phone FROM contacts")
    }
    for row in live_conn.execute("SELECT id, phone, alt_phone FROM contacts"):
        cid = int(row["id"])
        old = old_contacts[cid]
        for field in ("phone", "alt_phone"):
            if _blank(old[field]) != _blank(row[field]):
                audit.append(
                    _audit_row(
                        entity_type="contact",
                        entity_id=cid,
                        field=field,
                        old=_blank(old[field]),
                        new=_blank(row[field]),
                        classification=SAFE,
                        reason="Phase 3B SAFE apply",
                    )
                )
    bak.close()
    return audit


def finish_from_backup(backup_path: Path) -> int:
    live = Path(DB)
    sha_after = _sha256(live)
    sha_backup = _sha256(backup_path)
    conn = get_connection(live)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        bak = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        bak.row_factory = sqlite3.Row
        bak.execute("PRAGMA query_only = ON")
        pre = _snapshot(bak)
        classified_pre = classify_live_dataset(bak)
        review_pre = _fingerprint_review(classified_pre)
        protected_pre = _protected_live(bak)
        bak.close()

        classified_now = classify_live_dataset(conn)
        second_audit: list[dict] = []
        second = apply_once(conn, classified_now, second_audit)
        conn.commit()
        extra = (
            second["names_changed"]
            + second["addresses_changed"]
            + second["company_phones_formatted"]
            + second["contact_phones_formatted"]
            + second["aliases_created"]
        )
        if extra:
            print(json.dumps({"error": "idempotent rerun changed data; STOP", "second": second}, indent=2, default=str))
            return 2
        post = _snapshot(conn)
        review_now = _fingerprint_review(classified_now)
        protected_now = _protected_live(conn)
        audit = _diff_changes(backup_path, conn)
        identity = _verify_identity(
            conn,
            sorted({row["Entity ID"] for row in audit if row["Entity Type"] == "company"}),
        )
        ajax = conn.execute("SELECT company_name FROM companies WHERE id=6").fetchone()[0]
        north_st = conn.execute("SELECT address FROM companies WHERE id=230").fetchone()[0]
        new_aliases = conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_system=?",
            (SOURCE_SYSTEM,),
        ).fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    if review_pre != review_now:
        print(json.dumps({"error": "REVIEW fingerprints changed; STOP"}, indent=2))
        return 2
    if protected_pre != protected_now:
        print(json.dumps({"error": "protected identity changed; STOP", "before": protected_pre, "after": protected_now}, indent=2, default=str))
        return 2
    if identity["stale_count"]:
        print(json.dumps({"error": "stale identity keys; STOP", "identity": identity}, indent=2, default=str))
        return 2

    counts_pre_ok, actual_pre = _counts_ok(classified_pre)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = OUT_DIR / "phase3b_apply_audit.csv"
    with audit_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Entity Type",
                "Entity ID",
                "Field",
                "Old Value",
                "New Value",
                "Phase 3A Classification",
                "Reason",
                "Timestamp",
            ],
        )
        writer.writeheader()
        writer.writerows(audit)

    def _count_audit(field_prefix: str, entity: str) -> int:
        return sum(1 for row in audit if row["Entity Type"] == entity and row["Field"].startswith(field_prefix) or (entity == "company" and row["Field"] == field_prefix))

    name_changes = sum(1 for row in audit if row["Field"] == "company_name")
    addr_changes = sum(1 for row in audit if row["Field"] == "address")
    company_phone_changes = sum(
        1
        for row in audit
        if row["Entity Type"] == "company" and row["Field"] in {"legacy_phone", "legacy_alt_phone", "legacy_mobile"}
    )
    contact_phone_changes = sum(
        1
        for row in audit
        if row["Entity Type"] == "contact" and row["Field"] in {"phone", "alt_phone"}
    )
    # Partition remaining SAFE phones on current live classification (extensions still SAFE).
    defer_ext_c = sum(
        1
        for row in classified_pre["company_phone_rows"]
        if row["classification"] == SAFE and partition_safe_phone(row["current"])[0] == "defer_ext"
    )
    defer_other_c = sum(
        1
        for row in classified_pre["company_phone_rows"]
        if row["classification"] == SAFE and partition_safe_phone(row["current"])[0] == "defer_other"
    )
    defer_ext_t = sum(
        1
        for row in classified_pre["contact_phone_rows"]
        if row["classification"] == SAFE and partition_safe_phone(row["current"])[0] == "defer_ext"
    )
    defer_other_t = sum(
        1
        for row in classified_pre["contact_phone_rows"]
        if row["classification"] == SAFE and partition_safe_phone(row["current"])[0] == "defer_other"
    )

    report = {
        "backup": str(backup_path),
        "backup_integrity": sqlite3.connect(str(backup_path)).execute("PRAGMA integrity_check").fetchone()[0],
        "sha256_before": sha_backup,
        "sha256_after": sha_after if extra == 0 else _sha256(live),
        "pre": pre,
        "regenerated_3a_from_backup": actual_pre,
        "backup_3a_counts_matched": counts_pre_ok,
        "alias_needed_from_backup": classified_pre["alias_needed_n"],
        "aliases_before": pre["company_aliases"],
        "aliases_added": new_aliases,
        "aliases_after": post["company_aliases"],
        "names_changed": name_changes,
        "addresses_changed": addr_changes,
        "company_phones_formatted": company_phone_changes,
        "company_phones_defer_ext": defer_ext_c,
        "company_phones_defer_other": defer_other_c,
        "contact_phones_formatted": contact_phone_changes,
        "contact_phones_defer_ext": defer_ext_t,
        "contact_phones_defer_other": defer_other_t,
        "second_pass": second,
        "post": post,
        "identity": identity,
        "review_untouched": review_pre == review_now,
        "protected_untouched": protected_pre == protected_now,
        "ajax_6_after": ajax,
        "north_st_230_after": north_st,
        "audit_rows": len(audit),
        "audit_csv": str(audit_path),
        "name_counts_after": dict(classified_now["name_counts"]),
        "addr_counts_after": dict(classified_now["addr_counts"]),
        "cphone_counts_after": dict(classified_now["cphone_counts"]),
        "tphone_counts_after": dict(classified_now["tphone_counts"]),
    }
    json_path = OUT_DIR / "phase3b_apply_report.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


def main() -> int:
    live = Path(DB)
    if live.resolve() != PRODUCTION_DB_PATH.resolve():
        print(json.dumps({"error": "DB path is not production", "db": str(live)}, indent=2))
        return 2
    sha_before = _sha256(live)
    backup_path = _backup(live)
    bak = sqlite3.connect(str(backup_path))
    try:
        backup_integrity = bak.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        bak.close()

    conn = get_connection(live)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        pre = _snapshot(conn)
        if pre["integrity"] != "ok" or backup_integrity != "ok":
            print(json.dumps({"error": "integrity not ok", "pre": pre, "backup": backup_integrity}, indent=2, default=str))
            return 2
        for key, expected in EXPECTED_LIVE.items():
            if pre.get(key) != expected:
                print(json.dumps({"error": "pre-apply count mismatch; STOP", "key": key, "expected": expected, "actual": pre.get(key)}, indent=2, default=str))
                return 2
        if tuple(pre.get("doc19") or ())[:2] != (19, "uploaded"):
            print(json.dumps({"error": "Document 19 not uploaded", "pre": pre}, indent=2, default=str))
            return 2

        classified = classify_live_dataset(conn)
        counts_match, actual_counts = _counts_ok(classified)
        if not counts_match:
            print(
                json.dumps(
                    {
                        "error": "regenerated Phase 3A counts differ; STOP",
                        "expected": EXPECTED_3A,
                        "actual": actual_counts,
                    },
                    indent=2,
                    default=str,
                )
            )
            return 2
        if classified["alias_needed_n"] != 227:
            print(
                json.dumps(
                    {
                        "error": "alias-needed count differs; STOP",
                        "expected": 227,
                        "actual": classified["alias_needed_n"],
                    },
                    indent=2,
                )
            )
            return 2

        review_before = _fingerprint_review(classified)
        protected_before = _protected_live(conn)
        ajax_before = conn.execute("SELECT company_name FROM companies WHERE id=6").fetchone()[0]
        north_st_before = conn.execute("SELECT address FROM companies WHERE id=230").fetchone()[0]

        audit: list[dict] = []
        first = apply_once(conn, classified, audit)
        conn.commit()

        classified_after = classify_live_dataset(conn)
        second_audit: list[dict] = []
        second = apply_once(conn, classified_after, second_audit)
        conn.commit()
        extra = (
            second["names_changed"]
            + second["addresses_changed"]
            + second["company_phones_formatted"]
            + second["contact_phones_formatted"]
            + second["aliases_created"]
        )
        if extra:
            print(json.dumps({"error": "idempotent rerun changed data; STOP", "second": second}, indent=2, default=str))
            return 2

        post = _snapshot(conn)
        protected_after = _protected_live(conn)
        review_after = _fingerprint_review(classified_after)
        identity = _verify_identity(
            conn,
            sorted({row["Entity ID"] for row in audit if row["Entity Type"] == "company"}),
        )
        ajax_after = conn.execute("SELECT company_name FROM companies WHERE id=6").fetchone()[0]
        north_st_after = conn.execute("SELECT address FROM companies WHERE id=230").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    if review_before != review_after:
        print(json.dumps({"error": "REVIEW fingerprints changed; STOP"}, indent=2))
        return 2
    if protected_before != protected_after:
        print(
            json.dumps(
                {
                    "error": "protected records changed; STOP",
                    "before": protected_before,
                    "after": protected_after,
                },
                indent=2,
                default=str,
            )
        )
        return 2
    if identity["stale_count"]:
        print(json.dumps({"error": "stale identity keys; STOP", "identity": identity}, indent=2, default=str))
        return 2
    for key in ("companies", "contacts", "ccr", "brown_sales_events", "brown_quote_milestones"):
        if post[key] != pre[key]:
            print(json.dumps({"error": "master count changed", "pre": pre, "post": post}, indent=2, default=str))
            return 2

    sha_after = _sha256(live)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = OUT_DIR / "phase3b_apply_audit.csv"
    with audit_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Entity Type",
                "Entity ID",
                "Field",
                "Old Value",
                "New Value",
                "Phase 3A Classification",
                "Reason",
                "Timestamp",
            ],
        )
        writer.writeheader()
        writer.writerows(audit)
    report = {
        "backup": str(backup_path),
        "backup_integrity": backup_integrity,
        "sha256_before": sha_before,
        "sha256_after": sha_after,
        "pre": pre,
        "regenerated_3a": actual_counts,
        "alias_needed": classified["alias_needed_n"],
        "first_pass": first,
        "second_pass": second,
        "post": post,
        "aliases_before": pre["company_aliases"],
        "aliases_added": first["aliases_created"],
        "aliases_after": post["company_aliases"],
        "identity": identity,
        "review_untouched": review_before == review_after,
        "protected_untouched": protected_before == protected_after,
        "ajax_6": {"before": ajax_before, "after": ajax_after},
        "north_st_230": {"before": north_st_before, "after": north_st_after},
        "audit_rows": len(audit),
        "audit_csv": str(audit_path),
        "name_counts_after": dict(classified_after["name_counts"]),
        "addr_counts_after": dict(classified_after["addr_counts"]),
    }
    json_path = OUT_DIR / "phase3b_apply_report.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--finish":
        sys.exit(finish_from_backup(Path(sys.argv[2])))
    sys.exit(main())
