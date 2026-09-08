"""Reload Carmeco SOURCE DATA into local NorthStar DB by Record No.

Preserves NorthStar-created data (activities, milestones, CCR CRM fields,
audit, opportunities). Replaces companies/contacts source fields and
legacy_notes only.
"""
from __future__ import annotations

import csv
import io
import random
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from db import DB_PATH as _CANONICAL_DB_PATH, get_connection

REPO_ROOT = Path(__file__).resolve().parent.parent
DATABASE_DIR = REPO_ROOT / "database"
DB_PATH = DATABASE_DIR / "northstar.db"
BACKUPS_DIR = DATABASE_DIR / "backups"

CARMECO_DIR = Path(
    r"C:\Users\julie\OneDrive\OneDrive JRM\OneDrive\JRM Recruitment\NorthStar\Carmeco"
)
DB_CSV = CARMECO_DIR / "carmeco database.csv"
NOTES_CSV = CARMECO_DIR / "notes only.csv"

# Company columns present in this source file (update these only).
COMPANY_SOURCE_FIELDS = (
    "company_name",
    "address",
    "city",
    "state",
    "zip",
    "website",
    "legacy_first_name",
    "legacy_last_name",
    "legacy_title",
    "legacy_phone",
    "legacy_alt_phone",
    "legacy_mobile",
    "legacy_email",
    "sales_volume_range",
    "location_sales_volume_range",
    "employee_size_range",
    "primary_sic_code",
    "primary_sic_description",
    "primary_naics_code",
    "primary_naics_description",
    "type_of_industry",
    "customer_campaign",
)


def decode_csv_bytes(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return enc, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return "latin-1", raw.decode("latin-1")


def load_csv(path: Path) -> tuple[str, list[dict[str, str | None]]]:
    enc, text = decode_csv_bytes(path.read_bytes())
    # Use StringIO so quoted multiline fields keep embedded newlines.
    rows = list(csv.DictReader(io.StringIO(text)))
    return enc, rows


def cell(row: dict, *keys: str) -> str:
    for key in keys:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return ""


def norm_record_no(value: str) -> str:
    return value.strip()


def company_payload(row: dict) -> dict[str, str]:
    primary_sic = cell(row, "Primary SIC Code") or cell(row, "SIC_CODE")
    primary_sic_desc = cell(row, "Primary SIC Code Description") or cell(
        row, "DESCRIPTION"
    )
    industry = cell(row, "Type of Industry") or cell(row, "Industry") or cell(
        row, "INDUSTRY DESCRIPTION"
    )
    return {
        "company_name": cell(row, "Company"),
        "address": cell(row, "Address1", "Address"),
        "city": cell(row, "City"),
        "state": cell(row, "State"),
        "zip": cell(row, "Zip"),
        "website": cell(row, "Web Address"),
        "legacy_first_name": cell(row, "FirstName", "F Name"),
        "legacy_last_name": cell(row, "LastName", "L Name"),
        "legacy_title": cell(row, "Title"),
        "legacy_phone": cell(row, "Phone"),
        "legacy_alt_phone": cell(row, "Alt Phone"),
        "legacy_mobile": cell(row, "Mobile"),
        "legacy_email": cell(row, "Email"),
        "sales_volume_range": cell(row, "Location Sales Volume Range", "Sales Volume Range"),
        "location_sales_volume_range": cell(row, "Location Sales Volume Range"),
        "employee_size_range": cell(row, "Location Employee Size Range"),
        "primary_sic_code": primary_sic,
        "primary_sic_description": primary_sic_desc,
        "primary_naics_code": cell(row, "Primary NAICS Code"),
        "primary_naics_description": cell(row, "Primary NAICS Description"),
        "type_of_industry": industry,
        "customer_campaign": cell(row, "Customer Campaign"),
    }


def backup_database() -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUPS_DIR / f"northstar_pre_carmeco_source_reload_{stamp}.db"
    shutil.copy2(DB_PATH, dest)
    return dest


def count_table(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0])


def snapshot_preserve(conn: sqlite3.Connection) -> dict[str, int]:
    keys = [
        "activities",
        "revenue_milestones",
        "field_audit_log",
        "opportunity_dismissals",
        "opportunity_assignments",
        "work_queue_items",
        "users",
        "user_client_assignments",
    ]
    out = {k: count_table(conn, k) for k in keys}
    out["ccr_hot"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM client_company_relationships WHERE is_hot = 1"
        ).fetchone()[0]
    )
    out["ccr_with_status"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM client_company_relationships
            WHERE TRIM(COALESCE(status, '')) <> ''
            """
        ).fetchone()[0]
    )
    out["ccr_cross_client_notes"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM client_company_relationships
            WHERE notes LIKE '%Cross-Client Opportunity%'
            """
        ).fetchone()[0]
    )
    return out


def reload() -> dict:
    if not DB_CSV.exists():
        raise FileNotFoundError(DB_CSV)
    if not NOTES_CSV.exists():
        raise FileNotFoundError(NOTES_CSV)
    if not DB_PATH.exists():
        raise FileNotFoundError(DB_PATH)

    backup_path = backup_database()
    print(f"BACKUP: {backup_path}")

    db_enc, db_rows = load_csv(DB_CSV)
    notes_enc, notes_rows = load_csv(NOTES_CSV)
    print(f"Loaded company/contact CSV ({db_enc}): {len(db_rows)} rows")
    print(f"Loaded notes CSV ({notes_enc}): {len(notes_rows)} rows")

    # Status field check BEFORE altering anything
    headers = list(db_rows[0].keys()) if db_rows else []
    status_cols = [
        h
        for h in headers
        if h and ("carmeco" in h.lower() or str(h).strip().lower() == "status")
    ]
    print(
        "STATUS SOURCE CHECK:",
        "FOUND " + ", ".join(status_cols)
        if status_cols
        else "NO Carmeco status column in source — existing statuses will be preserved",
    )

    # Group contacts by Record No.; first row supplies company fields
    companies_by_rec: dict[str, dict[str, str]] = {}
    contacts_by_rec: dict[str, list[tuple[int, dict]]] = {}
    blank_record_rows = 0
    for source_row_index, row in enumerate(db_rows, start=1):
        rn = norm_record_no(cell(row, "Record No."))
        if not rn:
            blank_record_rows += 1
            continue
        if rn not in companies_by_rec:
            companies_by_rec[rn] = company_payload(row)
        contacts_by_rec.setdefault(rn, []).append((source_row_index, row))

    carmeco_recnos = set(companies_by_rec)
    print(f"Unique Carmeco Record Nos in source: {len(carmeco_recnos)}")
    print(f"Blank Record No. contact rows skipped: {blank_record_rows}")

    # Notes: exact Record No. join only
    note_map: dict[str, str] = {}
    note_dup_recs: list[str] = []
    note_seen: Counter[str] = Counter()
    for row in notes_rows:
        rn = norm_record_no(cell(row, "Record No."))
        if not rn:
            continue
        note_seen[rn] += 1
        note = cell(row, "Sales Rep Comments/Notes")
        if rn in note_map:
            if rn not in note_dup_recs:
                note_dup_recs.append(rn)
            # Keep first nonempty; do not merge unrelated histories
            if note and not note_map[rn]:
                note_map[rn] = note
            continue
        note_map[rn] = note

    matched_notes = {rn: note_map[rn] for rn in note_map if rn in carmeco_recnos}
    skipped_outside = sorted(set(note_map) - carmeco_recnos)
    without_notes = sorted(carmeco_recnos - set(note_map))
    nonempty_matched = {rn: n for rn, n in matched_notes.items() if n}
    blank_matched = sorted(rn for rn, n in matched_notes.items() if not n)

    print(f"Notes RecNos matched to Carmeco: {len(matched_notes)}")
    print(f"Notes RecNos skipped (outside Carmeco): {len(skipped_outside)}")
    print(f"Carmeco RecNos with no notes row: {len(without_notes)}")
    print(f"Matched nonempty legacy notes: {len(nonempty_matched)}")
    print(f"Matched blank legacy notes (treated as no notes): {len(blank_matched)}")
    print(f"Duplicate note Record Nos (kept first): {len(note_dup_recs)}")

    # Canonical helper registers CRM identity UDFs required by identity-key triggers.
    if Path(DB_PATH).resolve() != Path(_CANONICAL_DB_PATH).resolve():
        raise RuntimeError(
            f"reload_carmeco_source DB_PATH ({DB_PATH}) diverges from db.DB_PATH "
            f"({_CANONICAL_DB_PATH}); refusing raw connect."
        )
    conn = get_connection()
    before = snapshot_preserve(conn)
    print("PRESERVE SNAPSHOT BEFORE:", before)

    try:
        cur = conn.cursor()
        client = cur.execute(
            "SELECT id FROM clients WHERE code = 'carmeco'"
        ).fetchone()
        if client is None:
            cur.execute(
                "INSERT INTO clients (code, name) VALUES ('carmeco', 'Carmeco')"
            )
            client_id = int(cur.lastrowid)
        else:
            client_id = int(client["id"])

        # Existing company map
        existing = {
            str(r["external_record_no"]): int(r["id"])
            for r in cur.execute(
                "SELECT id, external_record_no FROM companies"
            )
        }
        record_to_company_id: dict[str, int] = {}
        companies_updated = 0
        companies_inserted = 0

        update_sql = """
            UPDATE companies SET
                company_name = ?,
                address = ?,
                city = ?,
                state = ?,
                zip = ?,
                website = ?,
                legacy_first_name = ?,
                legacy_last_name = ?,
                legacy_title = ?,
                legacy_phone = ?,
                legacy_alt_phone = ?,
                legacy_mobile = ?,
                legacy_email = ?,
                sales_volume_range = ?,
                location_sales_volume_range = ?,
                employee_size_range = ?,
                primary_sic_code = ?,
                primary_sic_description = ?,
                primary_naics_code = ?,
                primary_naics_description = ?,
                type_of_industry = ?,
                customer_campaign = ?
            WHERE id = ?
        """
        insert_sql = """
            INSERT INTO companies (
                external_record_no, company_name, address, city, state, zip, website,
                legacy_first_name, legacy_last_name, legacy_title,
                legacy_phone, legacy_alt_phone, legacy_mobile, legacy_email,
                sales_volume_range, location_sales_volume_range, employee_size_range,
                primary_sic_code, primary_sic_description,
                primary_naics_code, primary_naics_description,
                type_of_industry, customer_campaign
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        for rn, payload in companies_by_rec.items():
            values = [payload[k] for k in COMPANY_SOURCE_FIELDS]
            if rn in existing:
                company_id = existing[rn]
                cur.execute(update_sql, (*values, company_id))
                companies_updated += 1
            else:
                cur.execute(insert_sql, (rn, *values))
                company_id = int(cur.lastrowid)
                companies_inserted += 1
                # New relationship: empty status only (no invented status)
                cur.execute(
                    """
                    INSERT INTO client_company_relationships (client_id, company_id, status)
                    VALUES (?, ?, '')
                    """,
                    (client_id, company_id),
                )
            record_to_company_id[rn] = company_id

            # Ensure CCR exists for existing companies; never overwrite status/CRM fields
            cur.execute(
                """
                INSERT OR IGNORE INTO client_company_relationships (client_id, company_id, status)
                VALUES (?, ?, '')
                """,
                (client_id, company_id),
            )

        # Replace contacts for Carmeco Record Nos only (by company_id)
        company_ids = list(record_to_company_id.values())
        # Delete contacts linked to these companies
        cur.execute(
            f"DELETE FROM contacts WHERE company_id IN ({','.join('?' * len(company_ids))})",
            company_ids,
        )

        contacts_imported = 0
        orphan_contacts = 0
        for rn, items in contacts_by_rec.items():
            company_id = record_to_company_id.get(rn)
            if company_id is None:
                orphan_contacts += len(items)
                continue
            for source_row_index, row in items:
                cur.execute(
                    """
                    INSERT INTO contacts (
                        company_id, external_record_no,
                        first_name, last_name, title, phone, alt_phone, email,
                        source_row_index
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company_id,
                        rn,
                        cell(row, "FirstName"),
                        cell(row, "LastName"),
                        cell(row, "Title"),
                        cell(row, "Phone"),
                        cell(row, "Alt Phone"),
                        cell(row, "Email"),
                        source_row_index,
                    ),
                )
                contacts_imported += 1

        # Replace legacy notes for Carmeco companies only
        cur.execute(
            f"""
            DELETE FROM legacy_notes
            WHERE client_id = ?
              AND company_id IN ({','.join('?' * len(company_ids))})
            """,
            [client_id, *company_ids],
        )

        notes_imported = 0
        for rn, note in nonempty_matched.items():
            company_id = record_to_company_id[rn]
            cur.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, ?, ?)
                """,
                (client_id, company_id, note, "Sales Rep Comments/Notes"),
            )
            notes_imported += 1

        conn.commit()

        after = snapshot_preserve(conn)
        print("PRESERVE SNAPSHOT AFTER:", after)
        preserve_ok = before == after
        if not preserve_ok:
            print("WARNING: preserve snapshot changed!", file=sys.stderr)
            for k in before:
                if before[k] != after[k]:
                    print(f"  {k}: {before[k]} -> {after[k]}", file=sys.stderr)

        # Validation
        company_count = int(
            cur.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        )
        carmeco_company_count = int(
            cur.execute(
                """
                SELECT COUNT(*) FROM companies co
                JOIN client_company_relationships ccr ON ccr.company_id = co.id
                JOIN clients cl ON cl.id = ccr.client_id AND cl.code = 'carmeco'
                """
            ).fetchone()[0]
        )
        contact_count = int(cur.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
        legacy_note_companies = int(
            cur.execute(
                """
                SELECT COUNT(DISTINCT company_id) FROM legacy_notes
                WHERE client_id = ?
                """,
                (client_id,),
            ).fetchone()[0]
        )
        companies_without_legacy = carmeco_company_count - legacy_note_companies

        orphan_contacts_db = int(
            cur.execute(
                """
                SELECT COUNT(*) FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                WHERE co.id IS NULL
                """
            ).fetchone()[0]
        )
        unmatched_contact_recnos = int(
            cur.execute(
                """
                SELECT COUNT(*) FROM contacts c
                WHERE NOT EXISTS (
                    SELECT 1 FROM companies co
                    WHERE co.external_record_no = c.external_record_no
                )
                """
            ).fetchone()[0]
        )
        orphan_notes = int(
            cur.execute(
                """
                SELECT COUNT(*) FROM legacy_notes ln
                LEFT JOIN companies co ON co.id = ln.company_id
                WHERE co.id IS NULL
                """
            ).fetchone()[0]
        )
        # Duplicate Record Nos among companies (should be 0 due to UNIQUE)
        dup_companies = int(
            cur.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT external_record_no FROM companies
                    GROUP BY external_record_no HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )

        # Trailerman spot check
        trail = cur.execute(
            """
            SELECT co.id, co.external_record_no, co.company_name,
                   (SELECT COUNT(*) FROM contacts ct WHERE ct.company_id = co.id) AS contact_count,
                   (SELECT substr(note_text, 1, 120) FROM legacy_notes ln
                    WHERE ln.company_id = co.id AND ln.client_id = ?
                    LIMIT 1) AS note_start,
                   (SELECT status FROM client_company_relationships ccr
                    WHERE ccr.company_id = co.id AND ccr.client_id = ?) AS status
            FROM companies co
            WHERE co.external_record_no = '95654'
            """,
            (client_id, client_id),
        ).fetchone()

        # Source-to-DB validation for ALL 402
        mismatches: list[str] = []
        for rn, payload in companies_by_rec.items():
            row = cur.execute(
                """
                SELECT company_name FROM companies WHERE external_record_no = ?
                """,
                (rn,),
            ).fetchone()
            if row is None:
                mismatches.append(f"{rn}: missing company")
                continue
            if (row["company_name"] or "").strip() != payload["company_name"]:
                mismatches.append(
                    f"{rn}: company name DB={row['company_name']!r} SRC={payload['company_name']!r}"
                )
            db_contacts = cur.execute(
                """
                SELECT first_name, last_name, phone FROM contacts
                WHERE external_record_no = ?
                ORDER BY source_row_index, id
                """,
                (rn,),
            ).fetchall()
            src_contacts = contacts_by_rec[rn]
            if len(db_contacts) != len(src_contacts):
                mismatches.append(
                    f"{rn}: contact count DB={len(db_contacts)} SRC={len(src_contacts)}"
                )
            else:
                for dbc, (_, src) in zip(db_contacts, src_contacts):
                    if (dbc["first_name"] or "") != cell(src, "FirstName") or (
                        dbc["last_name"] or ""
                    ) != cell(src, "LastName"):
                        mismatches.append(
                            f"{rn}: contact mismatch {dbc['first_name']} {dbc['last_name']}"
                        )
                        break
            db_note = cur.execute(
                """
                SELECT note_text FROM legacy_notes
                WHERE client_id = ? AND company_id = (
                    SELECT id FROM companies WHERE external_record_no = ?
                )
                """,
                (client_id, rn),
            ).fetchone()
            expected_note = nonempty_matched.get(rn)
            if expected_note:
                if db_note is None or db_note["note_text"] != expected_note:
                    mismatches.append(f"{rn}: legacy note mismatch")
            else:
                if db_note is not None and (db_note["note_text"] or "").strip():
                    mismatches.append(f"{rn}: unexpected legacy note present")

        # Random spot checks (10 + Trailerman)
        rng = random.Random(95654)
        sample = ["95654"] + rng.sample(
            sorted(carmeco_recnos - {"95654"}), min(10, len(carmeco_recnos) - 1)
        )
        spot_results = []
        for rn in sample:
            co = cur.execute(
                "SELECT id, company_name FROM companies WHERE external_record_no = ?",
                (rn,),
            ).fetchone()
            contacts = cur.execute(
                """
                SELECT first_name, last_name FROM contacts
                WHERE external_record_no = ? ORDER BY source_row_index, id
                """,
                (rn,),
            ).fetchall()
            note = cur.execute(
                """
                SELECT substr(note_text, 1, 80) AS preview FROM legacy_notes
                WHERE client_id = ? AND company_id = ?
                """,
                (client_id, co["id"] if co else -1),
            ).fetchone()
            src_name = companies_by_rec[rn]["company_name"]
            src_contact_n = len(contacts_by_rec[rn])
            expected = nonempty_matched.get(rn)
            ok = (
                co is not None
                and co["company_name"] == src_name
                and len(contacts) == src_contact_n
                and (
                    (expected is None and note is None)
                    or (expected is not None and note is not None)
                )
            )
            if expected and note:
                # verify full text via join already in mismatches; preview for report
                pass
            spot_results.append(
                {
                    "record_no": rn,
                    "company": co["company_name"] if co else None,
                    "src_company": src_name,
                    "contacts": len(contacts),
                    "src_contacts": src_contact_n,
                    "has_legacy_notes": note is not None,
                    "expected_legacy_notes": expected is not None,
                    "note_preview": note["preview"] if note else "No legacy notes available",
                    "ok": ok and rn not in [m.split(":")[0] for m in mismatches],
                }
            )

        # Trailerman detailed
        trailerman_ok = False
        if trail:
            note_full = cur.execute(
                """
                SELECT note_text FROM legacy_notes
                WHERE client_id = ? AND company_id = ?
                """,
                (client_id, trail["id"]),
            ).fetchone()
            trailerman_ok = (
                trail["company_name"] == "Trailerman Trailers"
                and trail["external_record_no"] == "95654"
                and int(trail["contact_count"]) == len(contacts_by_rec["95654"])
                and note_full is not None
                and note_full["note_text"].startswith(
                    "12-Nov-2019 8:03 PM CST - Marcy Ratliff (Call Stats)"
                )
                and "Update Record - Charles Perkins" in note_full["note_text"]
            )

        result = {
            "backup_path": str(backup_path),
            "status_column_in_source": bool(status_cols),
            "unique_carmeco_companies": len(carmeco_recnos),
            "companies_in_db": company_count,
            "carmeco_relationships": carmeco_company_count,
            "companies_updated": companies_updated,
            "companies_inserted": companies_inserted,
            "contacts_imported": contacts_imported,
            "contacts_in_db": contact_count,
            "companies_with_legacy_notes": legacy_note_companies,
            "companies_without_legacy_notes": companies_without_legacy,
            "notes_records_skipped_outside_carmeco": len(skipped_outside),
            "duplicate_company_record_nos": dup_companies,
            "duplicate_note_record_nos": len(note_dup_recs),
            "orphan_contacts_skipped": orphan_contacts,
            "orphan_contacts_in_db": orphan_contacts_db,
            "unmatched_contact_record_nos": unmatched_contact_recnos,
            "orphan_notes": orphan_notes,
            "source_validation_mismatches": len(mismatches),
            "preserve_ok": preserve_ok,
            "preserve_before": before,
            "preserve_after": after,
            "trailerman_ok": trailerman_ok,
            "trailerman": dict(trail) if trail else None,
            "spot_checks": spot_results,
            "mismatch_samples": mismatches[:20],
        }
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    stats = reload()
    print("\n========== RELOAD REPORT ==========")
    for k, v in stats.items():
        if k in {"spot_checks", "mismatch_samples", "preserve_before", "preserve_after"}:
            continue
        print(f"{k}: {v}")
    print("\n--- Spot checks ---")
    for s in stats["spot_checks"]:
        print(s)
    if stats["mismatch_samples"]:
        print("\n--- Mismatch samples ---")
        for m in stats["mismatch_samples"]:
            print(m)

    # Rebuild search index after data change
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    try:
        from search_data import rebuild_search_index

        rebuild_search_index()
        print("Search index rebuilt.")
    except Exception as e:
        print(f"Search index rebuild warning: {e}", file=sys.stderr)

    ok = (
        stats["unique_carmeco_companies"] == 402
        and stats["contacts_imported"] == 2000
        and stats["orphan_contacts_in_db"] == 0
        and stats["orphan_contacts_skipped"] == 0
        and stats["unmatched_contact_record_nos"] == 0
        and stats["orphan_notes"] == 0
        and stats["duplicate_company_record_nos"] == 0
        and stats["source_validation_mismatches"] == 0
        and stats["preserve_ok"]
        and stats["trailerman_ok"]
        and all(s["ok"] for s in stats["spot_checks"])
        and not stats["status_column_in_source"]
    )
    if ok:
        print("\nVALIDATION PASSED: Record No. maps Company -> Contacts -> Legacy Notes")
        return 0
    print("\nVALIDATION FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
