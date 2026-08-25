"""Controlled Carmeco batch #3 Confirm Import — historical sales events only."""
from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from client_engagement_import import (
    confirm_engagement_import,
    ensure_engagement_import_schema,
    get_engagement_import_batch,
    list_sales_events,
)
from client_workspace_data import get_company_workspace, get_contact_workspace
from db import DATABASE_DIR, DB_PATH, get_connection
from models import EngagementImportConfirmRequest


def snapshot() -> dict:
    with get_connection() as conn:
        out = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "legacy_notes": conn.execute("SELECT COUNT(*) FROM legacy_notes").fetchone()[0],
            "sales_events": 0,
            "campaigns": None,
            "ccr_status_hash": None,
            "ccr_record_hash": None,
        }
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_sales_events'"
        ).fetchone():
            out["sales_events"] = conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0]
        # campaign-like tables
        for name in ("campaigns", "client_campaigns", "customer_campaigns"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone():
                out["campaigns"] = {
                    "table": name,
                    "count": conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0],
                }
                break
        # integrity probes — statuses and record numbers must not change
        out["ccr_status_hash"] = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(LENGTH(COALESCE(status,''))),0),
                   COALESCE(SUM(LENGTH(COALESCE(external_record_no,''))),0)
            FROM client_company_relationships
            """
        ).fetchone()
        out["ccr_status_hash"] = {
            "n": out["ccr_status_hash"][0],
            "status_len": out["ccr_status_hash"][1],
            "record_len": out["ccr_status_hash"][2],
        }
        return out


def main() -> None:
    ensure_engagement_import_schema()
    before = snapshot()
    print("BEFORE", json.dumps(before, default=str))

    with get_connection() as conn:
        batch = conn.execute(
            "SELECT id, status, filename FROM client_engagement_import_batches WHERE id=3 AND client_id=1"
        ).fetchone()
        if not batch:
            raise SystemExit("Batch #3 not found for Carmeco.")
        print("BATCH", dict(batch))
        row_stats = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN company_match_status='MATCHED' THEN 1 ELSE 0 END) AS co_matched,
                   SUM(CASE WHEN contact_match_status='MATCHED' THEN 1 ELSE 0 END) AS ct_matched,
                   SUM(CASE WHEN selected=1 THEN 1 ELSE 0 END) AS selected,
                   SUM(CASE WHEN COALESCE(duplicate_status,'')<>'' THEN 1 ELSE 0 END) AS dups
            FROM client_engagement_import_rows
            WHERE batch_id=3 AND client_id=1 AND COALESCE(sheet_type,'')<>'Ignore'
            """
        ).fetchone()
        print("ROW_STATS", dict(row_stats))
        types = conn.execute(
            """
            SELECT event_type, COUNT(*) AS n
            FROM client_engagement_import_rows
            WHERE batch_id=3 AND client_id=1 AND COALESCE(sheet_type,'')<>'Ignore'
            GROUP BY event_type
            """
        ).fetchall()
        print("STAGED_TYPES", {r["event_type"]: r["n"] for r in types})

    # 1) Backup
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(DATABASE_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"northstar_pre_carmeco_appt_import_{stamp}.db"
    try:
        shutil.copy2(DB_PATH, backup_path)
        if not backup_path.exists() or backup_path.stat().st_size < 1000:
            raise RuntimeError("Backup file missing or too small.")
    except Exception as exc:
        print("BACKUP_FAILED", exc)
        raise SystemExit("STOP — backup failed; import not performed.")
    print("BACKUP", str(backup_path))

    # 2) Confirm import — all selected matched rows, create_new_ids empty
    result = confirm_engagement_import(
        1,
        3,
        EngagementImportConfirmRequest(confirm=True, row_ids=[], create_new_ids=[]),
    )
    print("IMPORT_BATCH_STATUS", result.status, result.notes if hasattr(result, "notes") else "")

    after = snapshot()
    print("AFTER", json.dumps(after, default=str))

    with get_connection() as conn:
        imported_rows = conn.execute(
            """
            SELECT COUNT(*) FROM client_engagement_import_rows
            WHERE batch_id=3 AND import_status='imported'
            """
        ).fetchone()[0]
        event_types = dict(
            Counter(
                r["event_type"]
                for r in conn.execute(
                    "SELECT event_type FROM client_sales_events WHERE source_batch_id=3"
                ).fetchall()
            )
        )
        linked = conn.execute(
            """
            SELECT
              SUM(CASE WHEN company_id IS NOT NULL THEN 1 ELSE 0 END) AS companies_linked,
              SUM(CASE WHEN contact_id IS NOT NULL THEN 1 ELSE 0 END) AS contacts_linked,
              SUM(CASE WHEN rev_spec_user_id IS NOT NULL THEN 1 ELSE 0 END) AS rev_spec_linked,
              COUNT(*) AS events
            FROM client_sales_events WHERE source_batch_id=3
            """
        ).fetchone()
        print("IMPORTED_ROWS", imported_rows)
        print("EVENT_TYPES", json.dumps(event_types))
        print("LINKED", dict(linked))

        aaon = conn.execute(
            """
            SELECT event_type, source_date_time_text, event_date, contact_name,
                   contact_id, source_sheet, source_row, source_rev_spec_text,
                   parent_event_id, source_row_fingerprint
            FROM client_sales_events
            WHERE client_id=1 AND source_record_number='1195125'
            ORDER BY COALESCE(event_date,''), source_row, id
            """
        ).fetchall()
        print("AAON_EVENTS", len(aaon))
        for r in aaon:
            print("AAON", json.dumps(dict(r), default=str))

    # Contact workspace
    cw = get_contact_workspace(3593, client_id=1)
    print(
        "CONTACT_WS",
        json.dumps(
            {
                "contact_id": cw.contact_id,
                "name": f"{cw.first_name} {cw.last_name}".strip(),
                "company": cw.company_name,
                "record": cw.company_record_no,
                "events": len(cw.sales_events or []),
                "href": f"/contacts/{cw.contact_id}?client_id=1",
                "company_href": f"/companies/{cw.company_record_no}?client_id=1"
                if cw.company_record_no
                else "",
            }
        ),
    )
    for ev in cw.sales_events or []:
        if ev.source_record_number == "1195125" or "AAON" in (ev.company_name or "").upper():
            print(
                "CONTACT_AAON_EV",
                ev.event_type,
                ev.source_date_time_text,
                ev.event_date,
            )

    # Company workspace AAON
    from client_workspace_data import get_company_by_record_no

    ws = get_company_by_record_no("1195125", client_id=1)
    if ws:
        print(
            "COMPANY_WS",
            ws.company_name,
            ws.external_record_no,
            "sales_events",
            len(ws.sales_events or []),
        )
        for ev in ws.sales_events or []:
            print("CO_EV", ev.event_type, ev.source_date_time_text, ev.contact_name)

    # Appointment counts must exclude Send Information / RFQ
    appt_only = list_sales_events(1, event_family="appointment", limit=500)
    eng_only = list_sales_events(1, event_family="engagement", limit=500)
    all_ev = list_sales_events(1, limit=500)
    print(
        "FILTER_COUNTS",
        json.dumps(
            {
                "appointment_family": len(appt_only),
                "engagement_send_info": len(eng_only),
                "all": len(all_ev),
                "rfq": sum(1 for e in all_ev if e.event_type == "RFQ"),
            }
        ),
    )

    # 10) Idempotency — second confirm should add 0
    se_before_idem = after["sales_events"]
    result2 = confirm_engagement_import(
        1,
        3,
        EngagementImportConfirmRequest(confirm=True, row_ids=[], create_new_ids=[]),
    )
    after_idem = snapshot()
    print("IDEMPOTENCY_STATUS", result2.status)
    print(
        "IDEMPOTENCY",
        json.dumps(
            {
                "sales_events_before": se_before_idem,
                "sales_events_after": after_idem["sales_events"],
                "delta": after_idem["sales_events"] - se_before_idem,
            }
        ),
    )

    # Also probe fingerprint path for already-imported fingerprints
    with get_connection() as conn:
        fps = [
            r[0]
            for r in conn.execute(
                "SELECT source_row_fingerprint FROM client_sales_events WHERE source_batch_id=3"
            ).fetchall()
        ]
        blocked = 0
        for fp in fps:
            exists = conn.execute(
                """
                SELECT id FROM client_sales_events
                WHERE client_id=1 AND source_row_fingerprint=?
                """,
                (fp,),
            ).fetchone()
            if exists:
                blocked += 1
        print("FINGERPRINTS_WOULD_BLOCK", blocked, "of", len(fps))

    print(
        "SAFETY",
        json.dumps(
            {
                "companies_unchanged": before["companies"] == after["companies"],
                "contacts_unchanged": before["contacts"] == after["contacts"],
                "activities_unchanged": before["activities"] == after["activities"],
                "legacy_notes_unchanged": before["legacy_notes"] == after["legacy_notes"],
                "campaigns_unchanged": before["campaigns"] == after["campaigns"],
                "ccr_unchanged": before["ccr_status_hash"] == after["ccr_status_hash"],
                "sales_events_delta": after["sales_events"] - before["sales_events"],
            }
        ),
    )


if __name__ == "__main__":
    main()
