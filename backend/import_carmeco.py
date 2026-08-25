"""Import Carmeco companies and contacts into the local NorthStar SQLite database."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from db import DATABASE_DIR, DB_PATH, reset_database

IMPORTS_DIR = DATABASE_DIR / "imports"
COMPANIES_CSV = IMPORTS_DIR / "carmeco_companies_import.csv"
CONTACTS_CSV = IMPORTS_DIR / "carmeco_contacts_import.csv"

CARMECO_CLIENT = {"code": "carmeco", "name": "Carmeco"}


def _cell(row: dict[str, str], *keys: str) -> str:
    """Return the first present cell value as-is (trimmed). Never invent blanks."""
    for key in keys:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return ""


def import_carmeco(
    companies_path: Path = COMPANIES_CSV,
    contacts_path: Path = CONTACTS_CSV,
    db_path: Path = DB_PATH,
) -> dict[str, int]:
    if not companies_path.exists():
        raise FileNotFoundError(f"Companies CSV not found: {companies_path}")
    if not contacts_path.exists():
        raise FileNotFoundError(f"Contacts CSV not found: {contacts_path}")

    conn = reset_database(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO clients (code, name) VALUES (?, ?)",
            (CARMECO_CLIENT["code"], CARMECO_CLIENT["name"]),
        )
        client_id = int(cur.lastrowid)

        record_to_company_id: dict[str, int] = {}
        companies_imported = 0
        notes_imported = 0

        with companies_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                record_no = _cell(row, "Record No.")
                if not record_no:
                    continue
                if record_no in record_to_company_id:
                    # One company per Record No. — keep first occurrence.
                    continue

                cur.execute(
                    """
                    INSERT INTO companies (
                        external_record_no, company_name, address, city, state, zip, website,
                        legacy_first_name, legacy_last_name, legacy_title,
                        legacy_phone, legacy_alt_phone, legacy_mobile, legacy_email,
                        sales_volume_range, location_sales_volume_range, employee_size_range,
                        primary_sic_code, primary_sic_description,
                        primary_naics_code, primary_naics_description,
                        sic_8_digit, sic_8_digit_description, type_of_industry,
                        customer_campaign, entered_at, last_updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?, ?
                    )
                    """,
                    (
                        record_no,
                        _cell(row, "Company"),
                        _cell(row, "Address"),
                        _cell(row, "City"),
                        _cell(row, "State"),
                        _cell(row, "Zip"),
                        _cell(row, "Web Address"),
                        _cell(row, "F Name"),
                        _cell(row, "L Name"),
                        _cell(row, "Title"),
                        _cell(row, "Phone"),
                        _cell(row, "Alt Phone"),
                        _cell(row, "Mobile"),
                        _cell(row, "Email"),
                        _cell(row, "Sales Volume Range"),
                        _cell(row, "Location Sales Volume Range"),
                        _cell(row, "Location Employee Size Range"),
                        _cell(row, "Primary SIC Code"),
                        _cell(row, "Primary SIC Code Description"),
                        _cell(row, "Primary NAICS Code"),
                        _cell(row, "Primary NAICS Description"),
                        _cell(row, "8 Digit Primary SIC"),
                        _cell(row, "8 Digit Primary SIC Description"),
                        _cell(row, "Type of Industry"),
                        _cell(row, "Customer Campaign"),
                        _cell(row, "Entered"),
                        _cell(row, "Last Updated"),
                    ),
                )
                company_id = int(cur.lastrowid)
                record_to_company_id[record_no] = company_id
                companies_imported += 1

                status = _cell(row, "Carmeco")
                cur.execute(
                    """
                    INSERT INTO client_company_relationships (client_id, company_id, status)
                    VALUES (?, ?, ?)
                    """,
                    (client_id, company_id, status),
                )

                notes = _cell(row, "Sales Rep Comments/Notes")
                if notes:
                    cur.execute(
                        """
                        INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                        VALUES (?, ?, ?, ?)
                        """,
                        (client_id, company_id, notes, "Sales Rep Comments/Notes"),
                    )
                    notes_imported += 1

        contacts_imported = 0
        orphan_contacts = 0
        with contacts_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for source_row_index, row in enumerate(reader, start=1):
                record_no = _cell(row, "Record No.")
                company_id = record_to_company_id.get(record_no)
                if company_id is None:
                    orphan_contacts += 1
                    continue
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
                        record_no,
                        _cell(row, "FirstName"),
                        _cell(row, "LastName"),
                        _cell(row, "Title"),
                        _cell(row, "Phone"),
                        _cell(row, "Alt Phone"),
                        _cell(row, "Email"),
                        source_row_index,
                    ),
                )
                contacts_imported += 1

        conn.commit()

        # Verification queries
        company_count = cur.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        contact_count = cur.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        orphan_check = cur.execute(
            """
            SELECT COUNT(*) FROM contacts c
            LEFT JOIN companies co ON co.id = c.company_id
            WHERE co.id IS NULL
            """
        ).fetchone()[0]
        unmatched_record_nos = cur.execute(
            """
            SELECT COUNT(*) FROM contacts c
            WHERE NOT EXISTS (
                SELECT 1 FROM companies co
                WHERE co.external_record_no = c.external_record_no
            )
            """
        ).fetchone()[0]

        return {
            "client_id": client_id,
            "companies_imported": companies_imported,
            "contacts_imported": contacts_imported,
            "notes_imported": notes_imported,
            "company_count": int(company_count),
            "contact_count": int(contact_count),
            "orphan_contacts_skipped": orphan_contacts,
            "orphan_contacts_in_db": int(orphan_check),
            "unmatched_record_nos": int(unmatched_record_nos),
            "db_path": str(db_path),
        }
    finally:
        conn.close()


def main() -> int:
    stats = import_carmeco()
    print("Carmeco import complete")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    ok = (
        stats["company_count"] == 402
        and stats["contact_count"] == 2000
        and stats["orphan_contacts_in_db"] == 0
        and stats["orphan_contacts_skipped"] == 0
        and stats["unmatched_record_nos"] == 0
    )
    if not ok:
        print("VERIFICATION FAILED", file=sys.stderr)
        return 1
    print("VERIFICATION PASSED: 402 companies, 2000 contacts, 0 orphans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
