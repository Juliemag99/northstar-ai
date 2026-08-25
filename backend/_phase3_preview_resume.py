"""Preview-only Carmeco appointment workbook staging. NO CONFIRM IMPORT."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from client_engagement_import import (
    ensure_engagement_import_schema,
    map_engagement_import,
    start_engagement_import,
    update_sheet_classifications,
)
from client_workspace_data import get_contact_workspace
from db import get_connection
from models import SheetClassificationUpdate

WORKBOOK = Path(r"C:\Users\julie\Downloads\Carmeco Appointment Grid 8 5 26 MC.xlsx")


def counts() -> dict[str, int]:
    with get_connection() as conn:
        out = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
        }
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_sales_events'"
        ).fetchone():
            out["sales_events"] = conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0]
        else:
            out["sales_events"] = 0
        for table, key in (
            ("campaigns", "campaigns"),
            ("legacy_notes", "legacy_notes"),
        ):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                out[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out


def main() -> None:
    ensure_engagement_import_schema()
    before = counts()
    print("BEFORE", json.dumps(before))

    if not WORKBOOK.exists():
        raise SystemExit(f"Workbook not found: {WORKBOOK}")

    batch = start_engagement_import(1, filename=WORKBOOK.name, content=WORKBOOK.read_bytes())
    print("BATCH", batch.batch_id, batch.status, batch.filename)
    for s in batch.sheets:
        print(
            "SHEET",
            json.dumps(
                {
                    "name": s.sheet_name,
                    "detected": s.detected_type,
                    "rows": s.row_count,
                    "empty": s.is_empty,
                    "headers": s.headers,
                }
            ),
        )

    update_sheet_classifications(
        1,
        batch.batch_id,
        [
            SheetClassificationUpdate(
                sheet_id=s.sheet_id, user_type=s.user_type or s.detected_type
            )
            for s in batch.sheets
        ],
    )
    preview = map_engagement_import(1, batch.batch_id, None)
    summary = preview.summary.model_dump()
    print("SUMMARY", json.dumps(summary))
    print("EVENT_TYPES", json.dumps(dict(Counter(r.event_type for r in preview.rows))))

    for s in preview.batch.sheets:
        print("MAPPED", s.sheet_name, json.dumps(s.column_mapping or {}))

    aaon = [
        r
        for r in preview.rows
        if r.company_record_no == "1195125" or "AAON" in (r.company_name or "").upper()
    ]
    print("AAON_COUNT", len(aaon))
    for r in aaon:
        print(
            "AAON",
            json.dumps(
                {
                    "sheet": r.sheet_name,
                    "row": r.source_row_number,
                    "event_type": r.event_type,
                    "datetime": r.source_date_time_text,
                    "parsed_date": r.event_date,
                    "contact": r.contact_name,
                    "contact_id": r.contact_id,
                    "company_match": r.company_match_status,
                    "contact_match": r.contact_match_status,
                    "fingerprint": (r.source_row_fingerprint or "")[:16],
                    "duplicate": r.duplicate_status,
                }
            ),
        )

    send = [r for r in preview.rows if r.event_type == "Send Information"]
    if send:
        s0 = send[0]
        print(
            "SEND_SAMPLE",
            json.dumps(
                {
                    "sheet": s0.sheet_name,
                    "row": s0.source_row_number,
                    "company": s0.company_name,
                    "contact": s0.contact_name,
                    "event_type": s0.event_type,
                    "datetime_text": s0.source_date_time_text,
                    "is_appointment": s0.event_type.startswith("Appointment"),
                }
            ),
        )

    # Legitimate repeats vs exact duplicates
    by_rec: dict[str, list] = defaultdict(list)
    fps = set()
    exact_dup = 0
    for r in preview.rows:
        by_rec[r.company_record_no or r.company_name].append(r.event_type)
        if r.duplicate_status:
            exact_dup += 1
        fps.add(r.source_row_fingerprint)
    repeats = {k: v for k, v in by_rec.items() if len(v) > 1}
    print("UNIQUE_FINGERPRINTS", len(fps))
    print("EXACT_DUPLICATES", exact_dup)
    print("LEGITIMATE_REPEATED_COMPANIES", len(repeats))

    matched = next((r for r in preview.rows if r.contact_id), None)
    if matched and matched.contact_id:
        cw = get_contact_workspace(matched.contact_id, client_id=1)
        print(
            "CONTACT_WS",
            json.dumps(
                {
                    "contact_id": cw.contact_id,
                    "name": f"{cw.first_name} {cw.last_name}".strip(),
                    "company": cw.company_name,
                    "record": cw.company_record_no,
                    "href": f"/contacts/{cw.contact_id}?client_id=1",
                }
            ),
        )

    after = counts()
    print("AFTER", json.dumps(after))
    print(
        "CRM_UNCHANGED",
        before["companies"] == after["companies"]
        and before["contacts"] == after["contacts"]
        and before["activities"] == after["activities"]
        and before["sales_events"] == after["sales_events"],
    )
    print("PRODUCTION_IMPORT_PERFORMED", False)
    print("BATCH_ID", batch.batch_id)


if __name__ == "__main__":
    main()
