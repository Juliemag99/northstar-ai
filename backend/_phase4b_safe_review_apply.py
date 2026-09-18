"""Phase 4B — revalidate and apply only unambiguous Phase 4A safe actions.

Does not merge, process deferred rows, international phones, contamination,
Clark-MXR, Dale Nelsen, unmarked trailing digits, location/site records,
Johnson Controls, CCR RNs, or company_locations.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from _phase4a_review_triage_preview import NAME_TRIAGE, _phone_disp_company, _phone_disp_contact
from company_aliases import upsert_company_alias
from contact_phone import (
    NOT_EXT,
    REVIEW,
    _EXT_MARK_RE,
    canonical_contact_phone,
    parse_extension_candidate,
    upsert_contact_phone_keys,
)
from crm_identity_keys import upsert_company_identity_key
from db import PRODUCTION_DB_PATH, get_connection
from import_brown_industries import digits_phone

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "database" / "northstar.db"
DUMP = REPO / "working" / "master-data-std-phase4a" / "phase4a_review_dump.json"
OUT_DIR = REPO / "working" / "master-data-std-phase4b"

SOURCE_SYSTEM = "master_standardization"
EXPECTED_CANDIDATES = 44
EXPECTED_LIVE = {
    "companies": 1792,
    "contacts": 4936,
    "ccr": 1889,
    "brown_sales_events": 97,
    "brown_quote_milestones": 26,
}

PROTECTED_COMPANY_IDS = frozenset({345, 551, 757, 298})
PROTECTED_CONTACT_IDS = frozenset({3898, 2134, 2088, 2091, 2246})
FORBIDDEN_ALIAS_RNS = frozenset({"1326674"})

COMPANY_EXT_MAP = {
    "legacy_phone": ("legacy_phone", "legacy_phone_extension", "legacy_alt_phone"),
    "legacy_alt_phone": ("legacy_phone", "legacy_phone_extension", "legacy_alt_phone"),
}
CONTACT_EXT_MAP = {
    "phone": ("phone", "phone_extension", "alt_phone"),
    "alt_phone": ("phone", "phone_extension", "alt_phone"),
}


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_nanp_main(value: object | None) -> bool:
    text = _blank(value)
    if not text or text.startswith("+"):
        return False
    parsed = parse_extension_candidate(text)
    return parsed["classification"] == NOT_EXT and len(parsed["parsed_main_digits"]) == 10


def has_explicit_extension_marker(value: object | None) -> bool:
    return bool(_EXT_MARK_RE.search(_blank(value)))


def parse_extension_only(value: object | None, *, field: str) -> dict:
    parsed = parse_extension_candidate(_blank(value), field=field)
    ext = _blank(parsed.get("parsed_extension"))
    ok = (
        parsed["classification"] == REVIEW
        and "Extension-only" in (parsed.get("reason") or "")
        and ext.isdigit()
        and 2 <= len(ext) <= 6
        and has_explicit_extension_marker(value)
    )
    return {**parsed, "extension_ok": ok}


def load_recommended_safe_actions(dump: dict) -> list[dict]:
    rows: list[dict] = []
    for item in dump["name_review"]:
        cid = int(item["id"])
        disp, rec_name, conf, reason, alias, merge, loc, safe = NAME_TRIAGE[cid]
        if safe != "yes":
            continue
        rows.append(
            {
                "entity_type": "company",
                "entity_id": cid,
                "kind": "rename",
                "field": "company_name",
                "current": item["current"],
                "recommended": rec_name,
                "disposition": disp,
                "confidence": conf,
                "reason": reason,
                "evidence": f"RN {item.get('rn')}",
            }
        )
    for row in dump["company_phone_3a_review"]:
        disp, rec_v, conf, reason, safe = _phone_disp_company(row)
        if safe != "yes":
            continue
        rows.append(
            {
                "entity_type": "company",
                "entity_id": int(row["id"]),
                "kind": "extension_only",
                "field": row["field"],
                "current": row["current"],
                "recommended": rec_v,
                "disposition": disp,
                "confidence": conf,
                "reason": reason,
                "evidence": json.dumps(row.get("sibling") or {}),
            }
        )
    for row in dump["contact_phone_3a_review"]:
        disp, rec_v, conf, reason, safe = _phone_disp_contact(row)
        if safe != "yes":
            continue
        rows.append(
            {
                "entity_type": "contact",
                "entity_id": int(row["contact_id"]),
                "kind": "extension_only",
                "field": row["field"],
                "current": row["current"],
                "recommended": rec_v,
                "disposition": disp,
                "confidence": conf,
                "reason": reason,
                "evidence": f"sibling {row.get('sibling_phone')} company {row.get('company_phone')}",
            }
        )
    return rows


def load_deferred_decisions(dump: dict) -> list[dict]:
    rows: list[dict] = []
    for item in dump["name_review"]:
        cid = int(item["id"])
        disp, rec_name, conf, reason, alias, merge, loc, safe = NAME_TRIAGE[cid]
        if disp in {"B. POSSIBLE DUPLICATE — INVESTIGATE", "F. BAD / CONTAMINATED DATA"} or merge == "yes" or cid == 757:
            rows.append(
                {
                    "entity_type": "company",
                    "entity_id": cid,
                    "field": "company_name",
                    "current": item["current"],
                    "disposition": disp,
                }
            )
    for row in dump["company_phone_3a_review"]:
        disp, rec_v, conf, reason, safe = _phone_disp_company(row)
        if disp in {
            "C. MALFORMED BUT RECOVERABLE",
            "E. NON-PHONE CONTAMINATION",
            "F. INVALID / INCOMPLETE",
            "H. NEEDS HUMAN DECISION",
            "D. INTERNATIONAL — VALID",
        } or (disp.startswith("B.") and safe != "yes"):
            rows.append(
                {
                    "entity_type": "company",
                    "entity_id": int(row["id"]),
                    "field": row["field"],
                    "current": row["current"],
                    "disposition": disp,
                }
            )
    for row in dump["contact_phone_3a_review"]:
        disp, rec_v, conf, reason, safe = _phone_disp_contact(row)
        if safe == "yes":
            continue
        rows.append(
            {
                "entity_type": "contact",
                "entity_id": int(row["contact_id"]),
                "field": row["field"],
                "current": row["current"],
                "disposition": disp,
            }
        )
    return rows


def _audit_row(
    *,
    entity_type: str,
    entity_id: int,
    field: str,
    old: str,
    new: str,
    ext_target: str,
    disposition: str,
    status: str,
    reason: str,
    evidence: str,
) -> dict:
    return {
        "Entity Type": entity_type,
        "Entity ID": entity_id,
        "Field": field,
        "Old Value": old,
        "New Value": new,
        "Extension Target Field": ext_target,
        "Disposition": disposition,
        "Applied / Deferred": status,
        "Reason": reason,
        "Evidence": evidence,
    }


def revalidate_rename(conn: sqlite3.Connection, candidate: dict) -> dict:
    cid = int(candidate["entity_id"])
    current = _blank(candidate["current"])
    proposed = _blank(candidate["recommended"])
    if cid in PROTECTED_COMPANY_IDS:
        return {"status": "deferred", "reason": "Protected company."}
    if not current or not proposed or current == proposed:
        return {"status": "deferred", "reason": "Missing or unchanged rename."}
    live = conn.execute(
        """
        SELECT id, company_name, external_record_no, address, city, state, zip,
               website, legacy_phone
        FROM companies WHERE id=?
        """,
        (cid,),
    ).fetchone()
    if live is None:
        return {"status": "deferred", "reason": "Company not found."}
    live_name = _blank(live["company_name"])
    if live_name == proposed:
        return {
            "status": "already",
            "reason": "Canonical name already applied.",
            "live": live,
        }
    if live_name != current:
        return {"status": "deferred", "reason": f"Live name drifted to {live_name!r}."}
    collision = conn.execute(
        """
        SELECT id, company_name FROM companies
        WHERE id != ? AND lower(trim(company_name)) = lower(trim(?))
        """,
        (cid, proposed),
    ).fetchall()
    if collision:
        ids = ", ".join(str(int(r["id"])) for r in collision)
        return {"status": "deferred", "reason": f"Canonical-name collision with id {ids}."}
    return {"status": "apply", "reason": candidate["reason"], "live": live}


def revalidate_company_extension(conn: sqlite3.Connection, candidate: dict) -> dict:
    cid = int(candidate["entity_id"])
    field = candidate["field"]
    if cid in PROTECTED_COMPANY_IDS:
        return {"status": "deferred", "reason": "Protected company."}
    if field != "legacy_alt_phone":
        return {"status": "deferred", "reason": "Only alt→main sibling moves are in scope; reverse slots deferred."}
    live = conn.execute(
        """
        SELECT id, company_name, legacy_phone, legacy_phone_extension,
               legacy_alt_phone, legacy_alt_phone_extension, legacy_mobile
        FROM companies WHERE id=?
        """,
        (cid,),
    ).fetchone()
    if live is None:
        return {"status": "deferred", "reason": "Company not found."}
    source = _blank(live["legacy_alt_phone"])
    parsed = parse_extension_only(source or candidate["current"], field=field)
    ext = _blank(parsed.get("parsed_extension"))
    main = _blank(live["legacy_phone"])
    main_ext = _blank(live["legacy_phone_extension"])
    if source == "" and main_ext and ext and main_ext == ext:
        return {
            "status": "already",
            "reason": "Extension already stored on main; obsolete alt already cleared.",
            "extension": ext,
            "target": "legacy_phone_extension",
        }
    if source != _blank(candidate["current"]):
        return {"status": "deferred", "reason": "Live alt-phone drifted from Phase 4A current value."}
    if not parsed["extension_ok"]:
        return {
            "status": "deferred",
            "reason": "Extension-only value is unmarked, incomplete, or not a confident 2–6 digit extension.",
        }
    if not is_valid_nanp_main(main):
        return {"status": "deferred", "reason": "Sibling main phone is not a valid 10-digit NANP number."}
    if is_valid_nanp_main(live["legacy_alt_phone"]):
        return {"status": "deferred", "reason": "Alt phone is itself a main number; not extension-only."}
    if main_ext and main_ext != ext:
        return {
            "status": "deferred",
            "reason": f"Existing legacy_phone_extension {main_ext!r} conflicts with {ext!r}.",
        }
    return {
        "status": "apply",
        "reason": (
            f"legacy_alt_phone is extension-only {source!r}; unique sibling main is legacy_phone {main}. "
            "Do not copy the main into alt_phone."
        ),
        "extension": ext,
        "target": "legacy_phone_extension",
        "main": main,
        "live": live,
    }


def revalidate_contact_extension(conn: sqlite3.Connection, candidate: dict) -> dict:
    tid = int(candidate["entity_id"])
    field = candidate["field"]
    if tid in PROTECTED_CONTACT_IDS:
        return {"status": "deferred", "reason": "Protected contact."}
    if field != "alt_phone":
        return {"status": "deferred", "reason": "Only alt→phone sibling moves are in scope; reverse slots deferred."}
    live = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, phone, phone_extension,
               alt_phone, alt_phone_extension
        FROM contacts WHERE id=?
        """,
        (tid,),
    ).fetchone()
    if live is None:
        return {"status": "deferred", "reason": "Contact not found."}
    source = _blank(live["alt_phone"])
    parsed = parse_extension_only(source or candidate["current"], field=field)
    ext = _blank(parsed.get("parsed_extension"))
    main = _blank(live["phone"])
    main_ext = _blank(live["phone_extension"])
    if source == "" and main_ext and ext and main_ext == ext:
        return {
            "status": "already",
            "reason": "Extension already stored on phone; obsolete alt already cleared.",
            "extension": ext,
            "target": "phone_extension",
        }
    if source != _blank(candidate["current"]):
        return {"status": "deferred", "reason": "Live alt-phone drifted from Phase 4A current value."}
    if not parsed["extension_ok"]:
        return {
            "status": "deferred",
            "reason": "Extension-only value is unmarked, incomplete, or not a confident 2–6 digit extension.",
        }
    if not is_valid_nanp_main(main):
        return {
            "status": "deferred",
            "reason": "No valid contact sibling main. Company main alone is not sufficient.",
        }
    if is_valid_nanp_main(live["alt_phone"]):
        return {"status": "deferred", "reason": "Alt phone is itself a main number; not extension-only."}
    if main_ext and main_ext != ext:
        return {
            "status": "deferred",
            "reason": f"Existing phone_extension {main_ext!r} conflicts with {ext!r}.",
        }
    return {
        "status": "apply",
        "reason": (
            f"alt_phone is extension-only {source!r}; unique sibling main is contact.phone {main}. "
            "Do not copy the main into alt_phone. Do not attach to company phone."
        ),
        "extension": ext,
        "target": "phone_extension",
        "main": main,
        "live": live,
    }


def apply_rename(conn: sqlite3.Connection, candidate: dict, decision: dict) -> dict:
    live = decision["live"]
    cid = int(candidate["entity_id"])
    current = _blank(candidate["current"])
    proposed = _blank(candidate["recommended"])
    alias_id, created = upsert_company_alias(
        conn,
        company_id=cid,
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
        return {"applied": False, "alias_created": False, "reason": "Alias insert failed."}
    still = conn.execute(
        """
        SELECT 1 FROM company_aliases
        WHERE company_id=? AND lower(trim(alias_name))=?
        LIMIT 1
        """,
        (cid, current.lower()),
    ).fetchone()
    if still is None:
        return {"applied": False, "alias_created": created, "reason": "Old name not present in aliases."}
    conn.execute(
        """
        UPDATE companies
        SET company_name=?, last_updated_at=datetime('now')
        WHERE id=? AND company_name=?
        """,
        (proposed, cid, current),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        return {"applied": False, "alias_created": created, "reason": "Rename UPDATE did not change one row."}
    row = conn.execute(
        """
        SELECT id, company_name, external_record_no, website, legacy_phone,
               address, city, state
        FROM companies WHERE id=?
        """,
        (cid,),
    ).fetchone()
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
    return {"applied": True, "alias_created": created, "reason": decision["reason"]}


def apply_company_extension(conn: sqlite3.Connection, candidate: dict, decision: dict) -> dict:
    cid = int(candidate["entity_id"])
    ext = decision["extension"]
    source = _blank(candidate["current"])
    conn.execute(
        """
        UPDATE companies
        SET legacy_phone_extension=?, legacy_alt_phone='', last_updated_at=datetime('now')
        WHERE id=? AND legacy_alt_phone=?
        """,
        (ext, cid, source),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        return {"applied": False, "reason": "Company extension UPDATE did not change one row."}
    return {"applied": True, "reason": decision["reason"]}


def apply_contact_extension(conn: sqlite3.Connection, candidate: dict, decision: dict) -> dict:
    tid = int(candidate["entity_id"])
    ext = decision["extension"]
    source = _blank(candidate["current"])
    conn.execute(
        """
        UPDATE contacts
        SET phone_extension=?, alt_phone=''
        WHERE id=? AND alt_phone=?
        """,
        (ext, tid, source),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        return {"applied": False, "reason": "Contact extension UPDATE did not change one row."}
    phones = conn.execute(
        "SELECT phone, alt_phone FROM contacts WHERE id=?",
        (tid,),
    ).fetchone()
    upsert_contact_phone_keys(
        conn,
        tid,
        _blank(phones["phone"]) if phones else "",
        _blank(phones["alt_phone"]) if phones else "",
    )
    return {"applied": True, "reason": decision["reason"]}


def apply_once(conn: sqlite3.Connection, candidates: list[dict], audit: list[dict]) -> dict:
    stats = {
        "renames_applied": 0,
        "renames_deferred": 0,
        "renames_already": 0,
        "aliases_created": 0,
        "company_ext_applied": 0,
        "company_ext_deferred": 0,
        "company_ext_already": 0,
        "contact_ext_applied": 0,
        "contact_ext_deferred": 0,
        "contact_ext_already": 0,
        "fields_cleared": 0,
    }
    for candidate in candidates:
        kind = candidate["kind"]
        if kind == "rename":
            decision = revalidate_rename(conn, candidate)
            if decision["status"] == "apply":
                result = apply_rename(conn, candidate, decision)
                if result["applied"]:
                    stats["renames_applied"] += 1
                    if result["alias_created"]:
                        stats["aliases_created"] += 1
                    audit.append(
                        _audit_row(
                            entity_type="company",
                            entity_id=candidate["entity_id"],
                            field="company_name",
                            old=candidate["current"],
                            new=candidate["recommended"],
                            ext_target="",
                            disposition=candidate["disposition"],
                            status="applied",
                            reason=result["reason"],
                            evidence=candidate["evidence"],
                        )
                    )
                    continue
                decision = {"status": "deferred", "reason": result["reason"]}
            status = decision["status"]
            if status == "already":
                stats["renames_already"] += 1
                label = "already"
            else:
                stats["renames_deferred"] += 1
                label = "deferred"
            audit.append(
                _audit_row(
                    entity_type="company",
                    entity_id=candidate["entity_id"],
                    field="company_name",
                    old=candidate["current"],
                    new=candidate["recommended"],
                    ext_target="",
                    disposition=candidate["disposition"],
                    status=label,
                    reason=decision["reason"],
                    evidence=candidate["evidence"],
                )
            )
            continue

        if candidate["entity_type"] == "company":
            decision = revalidate_company_extension(conn, candidate)
            if decision["status"] == "apply":
                result = apply_company_extension(conn, candidate, decision)
                if result["applied"]:
                    stats["company_ext_applied"] += 1
                    stats["fields_cleared"] += 1
                    audit.append(
                        _audit_row(
                            entity_type="company",
                            entity_id=candidate["entity_id"],
                            field=candidate["field"],
                            old=candidate["current"],
                            new="",
                            ext_target=decision["target"],
                            disposition=candidate["disposition"],
                            status="applied",
                            reason=result["reason"],
                            evidence=f"{decision['main']} + ext {decision['extension']}",
                        )
                    )
                    continue
                decision = {"status": "deferred", "reason": result["reason"]}
            if decision["status"] == "already":
                stats["company_ext_already"] += 1
                label = "already"
            else:
                stats["company_ext_deferred"] += 1
                label = "deferred"
            audit.append(
                _audit_row(
                    entity_type="company",
                    entity_id=candidate["entity_id"],
                    field=candidate["field"],
                    old=candidate["current"],
                    new="",
                    ext_target=decision.get("target", "legacy_phone_extension"),
                    disposition=candidate["disposition"],
                    status=label,
                    reason=decision["reason"],
                    evidence=candidate["evidence"],
                )
            )
            continue

        decision = revalidate_contact_extension(conn, candidate)
        if decision["status"] == "apply":
            result = apply_contact_extension(conn, candidate, decision)
            if result["applied"]:
                stats["contact_ext_applied"] += 1
                stats["fields_cleared"] += 1
                audit.append(
                    _audit_row(
                        entity_type="contact",
                        entity_id=candidate["entity_id"],
                        field=candidate["field"],
                        old=candidate["current"],
                        new="",
                        ext_target=decision["target"],
                        disposition=candidate["disposition"],
                        status="applied",
                        reason=result["reason"],
                        evidence=f"{decision['main']} + ext {decision['extension']}",
                    )
                )
                continue
            decision = {"status": "deferred", "reason": result["reason"]}
        if decision["status"] == "already":
            stats["contact_ext_already"] += 1
            label = "already"
        else:
            stats["contact_ext_deferred"] += 1
            label = "deferred"
        audit.append(
            _audit_row(
                entity_type="contact",
                entity_id=candidate["entity_id"],
                field=candidate["field"],
                old=candidate["current"],
                new="",
                ext_target=decision.get("target", "phone_extension"),
                disposition=candidate["disposition"],
                status=label,
                reason=decision["reason"],
                evidence=candidate["evidence"],
            )
        )
    return stats


def snapshot(conn: sqlite3.Connection) -> dict:
    return {
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "ccr": conn.execute("SELECT COUNT(*) FROM client_company_relationships").fetchone()[0],
        "company_aliases": conn.execute("SELECT COUNT(*) FROM company_aliases").fetchone()[0],
        "brown_sales_events": conn.execute(
            "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        ).fetchone()[0],
        "brown_quote_milestones": conn.execute(
            """
            SELECT COUNT(*) FROM revenue_milestones
            WHERE client_id=2 AND milestone_type='Quote'
            """
        ).fetchone()[0],
        "doc19": tuple(
            conn.execute(
                "SELECT id, processing_status FROM client_documents WHERE id=19"
            ).fetchone()
            or ()
        ),
        "company_identity_keys": conn.execute(
            "SELECT COUNT(*) FROM company_identity_keys"
        ).fetchone()[0],
        "contact_phone_keys": conn.execute(
            "SELECT COUNT(*) FROM contact_phone_keys"
        ).fetchone()[0],
        "ext": {
            "legacy_phone_extension": conn.execute(
                "SELECT COUNT(*) FROM companies WHERE TRIM(COALESCE(legacy_phone_extension,''))!=''"
            ).fetchone()[0],
            "legacy_alt_phone_extension": conn.execute(
                "SELECT COUNT(*) FROM companies WHERE TRIM(COALESCE(legacy_alt_phone_extension,''))!=''"
            ).fetchone()[0],
            "phone_extension": conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE TRIM(COALESCE(phone_extension,''))!=''"
            ).fetchone()[0],
            "alt_phone_extension": conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE TRIM(COALESCE(alt_phone_extension,''))!=''"
            ).fetchone()[0],
        },
    }


def protected_live(conn: sqlite3.Connection) -> dict:
    companies = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT id, company_name, external_record_no, address, city, state,
                   legacy_phone, legacy_alt_phone
            FROM companies WHERE id IN (551, 757, 298, 345)
            ORDER BY id
            """
        )
    ]
    contacts = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT id, phone, alt_phone, phone_extension, alt_phone_extension
            FROM contacts WHERE id IN (3898, 2134, 2088, 2091, 2246)
            ORDER BY id
            """
        )
    ]
    return {
        "companies": companies,
        "contacts": contacts,
        "company_ids": [r[0] for r in conn.execute("SELECT id FROM companies ORDER BY id")],
        "contact_ids": [r[0] for r in conn.execute("SELECT id FROM contacts ORDER BY id")],
        "ccr_ids": [r[0] for r in conn.execute("SELECT id FROM client_company_relationships ORDER BY id")],
        "york_aliases": conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_record_no='1326674'"
        ).fetchone()[0],
        "whirlpool_1325571": conn.execute(
            "SELECT COUNT(*) FROM company_aliases WHERE source_record_no='1325571'"
        ).fetchone()[0],
        "locations": conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='company_locations'"
        ).fetchone(),
    }


def deferred_fingerprint(conn: sqlite3.Connection, deferred: list[dict]) -> list[tuple]:
    rows: list[tuple] = []
    for item in deferred:
        if item["entity_type"] == "company":
            field = item["field"]
            live = conn.execute(
                f"SELECT {field} FROM companies WHERE id=?",
                (item["entity_id"],),
            ).fetchone()
            rows.append(("company", item["entity_id"], field, _blank(live[0] if live else "")))
        else:
            field = item["field"]
            live = conn.execute(
                f"SELECT {field} FROM contacts WHERE id=?",
                (item["entity_id"],),
            ).fetchone()
            rows.append(("contact", item["entity_id"], field, _blank(live[0] if live else "")))
    return rows


def identity_main_only_check(conn: sqlite3.Connection, company_ids: list[int], contact_ids: list[int]) -> dict:
    company_bad = []
    for cid in company_ids:
        row = conn.execute(
            """
            SELECT c.legacy_phone, c.legacy_phone_extension, k.phone_digits, k.phone_last7
            FROM companies c
            LEFT JOIN company_identity_keys k ON k.company_id=c.id
            WHERE c.id=?
            """,
            (cid,),
        ).fetchone()
        if row is None:
            continue
        expected = digits_phone(_blank(row["legacy_phone"]))
        stored = _blank(row["phone_digits"])
        ext = _blank(row["legacy_phone_extension"])
        if stored != expected:
            company_bad.append({"id": cid, "stored": stored, "expected": expected})
        if ext and ext in stored:
            company_bad.append({"id": cid, "stored": stored, "extension_leaked": ext})
    contact_bad = []
    for tid in contact_ids:
        row = conn.execute(
            "SELECT phone, phone_extension, alt_phone FROM contacts WHERE id=?",
            (tid,),
        ).fetchone()
        if row is None:
            continue
        keys = list(
            conn.execute(
                "SELECT slot, nanp10, last7 FROM contact_phone_keys WHERE contact_id=?",
                (tid,),
            )
        )
        by_slot = {r["slot"]: r for r in keys}
        nanp, last7 = canonical_contact_phone(_blank(row["phone"]))
        stored = by_slot.get("phone")
        if stored and (_blank(stored["nanp10"]) != nanp or _blank(stored["last7"]) != last7):
            contact_bad.append({"id": tid, "slot": "phone", "stored": dict(stored)})
        ext = _blank(row["phone_extension"])
        if stored and ext and ext in _blank(stored["nanp10"]):
            contact_bad.append({"id": tid, "slot": "phone", "extension_leaked": ext})
        alt_row = by_slot.get("alt_phone")
        if not _blank(row["alt_phone"]) and alt_row is not None:
            contact_bad.append(
                {
                    "id": tid,
                    "slot": "alt_phone",
                    "unexpected_key": dict(alt_row),
                }
            )
    return {"company_bad": company_bad, "contact_bad": contact_bad}


def _backup(live: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = live.with_name(f"northstar.db.bak-master-data-std-phase4b-{stamp}")
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


def _write_audit(audit: list[dict]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "phase4b_safe_review_apply.csv"
    fields = [
        "Entity Type",
        "Entity ID",
        "Field",
        "Old Value",
        "New Value",
        "Extension Target Field",
        "Disposition",
        "Applied / Deferred",
        "Reason",
        "Evidence",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in audit:
            writer.writerow(row)
    return path


def main() -> int:
    live = Path(DB)
    if live.resolve() != PRODUCTION_DB_PATH.resolve():
        print(json.dumps({"error": "DB path is not production", "db": str(live)}, indent=2))
        return 2
    dump = json.loads(DUMP.read_text(encoding="utf-8"))
    candidates = load_recommended_safe_actions(dump)
    deferred = load_deferred_decisions(dump)
    if len(candidates) != EXPECTED_CANDIDATES:
        print(
            json.dumps(
                {
                    "error": "Candidate set differs unexpectedly; STOP",
                    "expected": EXPECTED_CANDIDATES,
                    "actual": len(candidates),
                },
                indent=2,
            )
        )
        return 2

    sha_before = _sha256(live)
    backup_path = _backup(live)
    bak = sqlite3.connect(str(backup_path))
    try:
        backup_integrity = bak.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        bak.close()

    aliases_before = None
    conn = get_connection(live)
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
        pre = snapshot(conn)
        if pre["integrity"] != "ok" or backup_integrity != "ok":
            print(json.dumps({"error": "integrity not ok", "pre": pre, "backup": backup_integrity}, indent=2, default=str))
            return 2
        for key, expected in EXPECTED_LIVE.items():
            if pre.get(key) != expected:
                print(
                    json.dumps(
                        {"error": "pre-apply count mismatch; STOP", "key": key, "expected": expected, "actual": pre.get(key)},
                        indent=2,
                        default=str,
                    )
                )
                return 2
        if tuple(pre.get("doc19") or ())[:2] != (19, "uploaded"):
            print(json.dumps({"error": "Document 19 not uploaded", "pre": pre}, indent=2, default=str))
            return 2
        protected_before = protected_live(conn)
        deferred_before = deferred_fingerprint(conn, deferred)
        aliases_before = pre["company_aliases"]

        audit: list[dict] = []
        conn.execute("BEGIN IMMEDIATE")
        first = apply_once(conn, candidates, audit)
        conn.commit()

        second_audit: list[dict] = []
        second = apply_once(conn, candidates, second_audit)
        conn.commit()
        if (
            second["renames_applied"]
            or second["aliases_created"]
            or second["company_ext_applied"]
            or second["contact_ext_applied"]
            or second["fields_cleared"]
        ):
            print(json.dumps({"error": "idempotent rerun changed data", "second": second}, indent=2, default=str))
            return 2

        post = snapshot(conn)
        protected_after = protected_live(conn)
        deferred_after = deferred_fingerprint(conn, deferred)
        if post["integrity"] != "ok":
            print(json.dumps({"error": "post integrity not ok", "post": post}, indent=2, default=str))
            return 2
        for key in ("companies", "contacts", "ccr", "brown_sales_events", "brown_quote_milestones"):
            if post[key] != pre[key]:
                print(json.dumps({"error": "master count changed", "key": key, "pre": pre[key], "post": post[key]}, indent=2))
                return 2
        if tuple(post.get("doc19") or ())[:2] != (19, "uploaded"):
            print(json.dumps({"error": "Document 19 changed", "post": post}, indent=2, default=str))
            return 2
        if protected_after != protected_before:
            print(json.dumps({"error": "protected data changed", "before": protected_before, "after": protected_after}, indent=2, default=str))
            return 2
        if deferred_after != deferred_before:
            print(json.dumps({"error": "deferred decision rows changed"}, indent=2, default=str))
            return 2
        applied_company_ids = [
            int(r["Entity ID"]) for r in audit if r["Applied / Deferred"] == "applied" and r["Entity Type"] == "company"
        ]
        applied_contact_ids = [
            int(r["Entity ID"]) for r in audit if r["Applied / Deferred"] == "applied" and r["Entity Type"] == "contact"
        ]
        identity = identity_main_only_check(conn, applied_company_ids, applied_contact_ids)
        if identity["company_bad"] or identity["contact_bad"]:
            print(json.dumps({"error": "identity keys not main-only", "identity": identity}, indent=2, default=str))
            return 2
        csv_path = _write_audit(audit)
        sha_after = _sha256(live)
        report = {
            "backup": str(backup_path),
            "backup_integrity": backup_integrity,
            "candidates": len(candidates),
            "first": first,
            "second": second,
            "aliases_before": aliases_before,
            "aliases_after": post["company_aliases"],
            "pre": pre,
            "post": post,
            "identity": identity,
            "sha_before": sha_before,
            "sha_after": sha_after,
            "csv": str(csv_path),
            "applied_rows": sum(1 for r in audit if r["Applied / Deferred"] == "applied"),
            "deferred_revalidation": sum(1 for r in audit if r["Applied / Deferred"] == "deferred"),
            "already": sum(1 for r in audit if r["Applied / Deferred"] == "already"),
            "protected_unchanged": protected_after == protected_before,
            "deferred_untouched": deferred_after == deferred_before,
            "generated_at": _now(),
        }
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        summary_path = OUT_DIR / "phase4b_summary.json"
        summary_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
