"""Phase 3 preview-only validation for Carmeco Appointment Grid. NO CONFIRM IMPORT."""
from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from client_engagement_import import (
    ensure_engagement_import_schema,
    get_engagement_import_preview,
    map_engagement_import,
    start_engagement_import,
    update_sheet_classifications,
)
from db import DB_PATH, DATABASE_DIR, get_connection
from models import SheetClassificationUpdate

WORKBOOK = Path(r"C:\Users\julie\Downloads\Carmeco Appointment Grid 8 5 26 MC.xlsx")


def main() -> None:
    ensure_engagement_import_schema()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = DATABASE_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"northstar_pre_engagement_preview_{stamp}.db"
    shutil.copy2(DB_PATH, backup_path)
    print("BACKUP", backup_path)

    with get_connection() as conn:
        before = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "sales_events": conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0]
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_sales_events'"
            ).fetchone()
            else 0,
        }
    print("BEFORE", before)

    content = WORKBOOK.read_bytes()
    batch = start_engagement_import(1, filename=WORKBOOK.name, content=content)
    print("BATCH", batch.batch_id, batch.status)
    for s in batch.sheets:
        print(
            "SHEET",
            s.sheet_name,
            "detected",
            s.detected_type,
            "rows",
            s.row_count,
            "empty",
            s.is_empty,
            "headers",
            len(s.headers),
        )

    # Accept detected classifications (user-reviewable in UI)
    update_sheet_classifications(
        1,
        batch.batch_id,
        [
            SheetClassificationUpdate(sheet_id=s.sheet_id, user_type=s.user_type or s.detected_type)
            for s in batch.sheets
        ],
    )
    preview = map_engagement_import(1, batch.batch_id, None)
    summary = preview.summary
    print("SUMMARY", summary.model_dump())

    event_types = Counter(r.event_type for r in preview.rows)
    print("EVENT_TYPES", dict(event_types))

    aaon = [r for r in preview.rows if r.company_record_no == "1195125" or "AAON" in (r.company_name or "").upper()]
    print("AAON_COUNT", len(aaon))
    for r in aaon:
        print(
            "AAON",
            r.source_row_number,
            r.event_type,
            r.source_date_time_text,
            r.contact_name,
            r.company_match_status,
            r.contact_match_status,
            "thread",
            r.thread_key,
            "parent_possible",
            "Resched" in r.event_type,
        )

    send = [r for r in preview.rows if r.sheet_type == "Send Information" or r.event_type == "Send Information"]
    print("SEND_COUNT", len(send))
    if send:
        s0 = send[0]
        print(
            "SEND_SAMPLE",
            s0.company_name,
            s0.event_type,
            s0.source_date_time_text,
            "NOT_APPOINTMENT",
            not s0.event_type.startswith("Appointment"),
        )

    # Contact workspace link check for a matched contact
    matched_ct = next((r for r in preview.rows if r.contact_id), None)
    if matched_ct:
        from client_workspace_data import get_contact_workspace

        cw = get_contact_workspace(matched_ct.contact_id, client_id=1)
        print(
            "CONTACT_WS",
            cw.contact_id,
            f"{cw.first_name} {cw.last_name}".strip(),
            "company",
            cw.company_name,
            "record",
            cw.company_record_no,
            "href",
            f"/contacts/{cw.contact_id}?client_id=1",
        )

    with get_connection() as conn:
        after = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "sales_events": conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0],
        }
    print("AFTER", after)
    print("CRM_UNCHANGED", before["companies"] == after["companies"] and before["contacts"] == after["contacts"] and before["activities"] == after["activities"])
    print("SALES_EVENTS_UNCHANGED", before["sales_events"] == after["sales_events"])
    print("NO_CONFIRM_IMPORT True")

    report = {
        "backup_path": str(backup_path),
        "sheets": [
            {
                "name": s.sheet_name,
                "detected": s.detected_type,
                "rows": s.row_count,
                "empty": s.is_empty,
                "mapped_columns": s.column_mapping,
            }
            for s in preview.batch.sheets
        ],
        "summary": summary.model_dump(),
        "event_types": dict(event_types),
        "aaon": [
            {
                "row": r.source_row_number,
                "event_type": r.event_type,
                "datetime": r.source_date_time_text,
                "parsed_date": r.event_date,
                "contact": r.contact_name,
                "company_match": r.company_match_status,
                "contact_match": r.contact_match_status,
            }
            for r in aaon
        ],
        "send_sample": {
            "company": send[0].company_name if send else None,
            "event_type": send[0].event_type if send else None,
            "datetime_text": send[0].source_date_time_text if send else None,
        },
    }
    out = DATABASE_DIR / "backups" / f"engagement_preview_report_{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("REPORT_JSON", out)


if __name__ == "__main__":
    main()
