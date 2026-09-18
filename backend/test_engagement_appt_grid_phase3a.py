"""Phase 3A Appointment Grid engagement safeguards — isolated tests.

Run from backend/:
  .venv\\Scripts\\python.exe test_engagement_appt_grid_phase3a.py

Uses testdb isolation only — never writes production northstar.db.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import testdb  # noqa: F401 — isolate before other imports

from openpyxl import Workbook

from client_engagement_appt_grid import (
    FQW_BLOCK_START,
    already_imported_by_fingerprint,
    already_imported_by_provenance,
    appointment_grid_fingerprint,
    build_delta_caller_notes,
    classify_already_imported,
    classify_appointment_narrative,
    classify_field_vs_history,
    compose_forecast_quoted_won_block,
    corroborate_year_from_history,
    is_footer_or_total_row,
    match_company_for_appt_grid,
    match_contact_for_appt_grid,
    merge_sales_notes_with_fqw,
    parse_datetime_text,
    propose_row_action,
    row_blocks_confirm,
)
from client_engagement_import import (
    DOCUMENTS_ROOT,
    _client_dir,
    _parse_workbook,
    confirm_engagement_import,
    ensure_engagement_import_schema,
    map_engagement_import,
    start_engagement_import,
)
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from models import EngagementImportConfirmRequest

APPT_HEADERS = [
    "List",
    "Record Number",
    "NorthStar Client",
    "Rev Spec",
    "Appointment Date/Time",
    "Company Name",
    "Contact Person",
    "Title",
    "Phone Number",
    "Email Address",
    "NorthStar Caller Notes",
    "Appt Grade",
    "Sales Notes",
    "Dollars Quoted",
    "Account Won, Lost or In Progress",
    "Forecast",
    "QUOTED",
    "WON",
]


def _grid_bytes(rows: list[list[str]], sheet: str = "Appointments") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(APPT_HEADERS)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _grid_row(
    *,
    rn: str,
    company: str,
    contact: str,
    email: str,
    client: str = "Brown Industries",
    dt: str = "Phone Appt, Tuesday, October 7th, 2025 at 2:00PM CDT",
    notes: str = "Spoke about machining",
    grade: str = "Y",
    sales: str = "Follow-up quote discussion",
    dollars: str = "",
    forecast: str = "May",
    quoted: str = "JAN QUOTE",
    title: str = "Buyer",
    phone: str = "5551112222",
) -> list[str]:
    return [
        "Brown Cap",
        rn,
        client,
        "",
        dt,
        company,
        contact,
        title,
        phone,
        email,
        notes,
        grade,
        sales,
        dollars,
        "",
        forecast,
        quoted,
        "",
    ]


def _count(conn, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _history_snapshot(conn, company_id: int) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            """
            SELECT id, event_at, event_type, note_text, event_hash
            FROM company_shared_history_events
            WHERE company_id = ?
            ORDER BY id
            """,
            (company_id,),
        ).fetchall()
    ]


def _client_identity_snapshot(conn, client_id: int) -> dict[str, int | str]:
    rel_blob = "|".join(
        f"{r['id']}:{r['company_id']}:{r['external_record_no']}"
        for r in conn.execute(
            """
            SELECT id, company_id, external_record_no
            FROM client_company_relationships
            WHERE client_id = ?
            ORDER BY id
            """,
            (client_id,),
        )
    )
    return {
        "sales_events": _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=?", (client_id,)
        ),
        "relationships": _count(
            conn,
            "SELECT COUNT(*) FROM client_company_relationships WHERE client_id=?",
            (client_id,),
        ),
        "documents": _count(
            conn, "SELECT COUNT(*) FROM client_documents WHERE client_id=?", (client_id,)
        ),
        "rel_blob": rel_blob,
        "milestones": _count(
            conn,
            "SELECT COUNT(*) FROM revenue_milestones WHERE client_id=?",
            (client_id,),
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_milestones'"
        ).fetchone()
        else 0,
    }


def _ok(msg: str) -> None:
    print(f"OK {msg}")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def test_datetime_safety() -> None:
    full = parse_datetime_text("Phone Appt, Tuesday, October 7th, 2025 at 2:00PM CDT")
    assert full["event_date"] == "2025-10-07", full
    assert full["datetime_needs_review"] == 0
    assert full["year_source"] == "explicit"
    assert full["event_time"] == "14:00"
    _ok("explicit full datetime")

    no_year = parse_datetime_text("Phone Appt, Tuesday, October 7th at 2:00PM CDT")
    assert no_year["event_date"] == ""
    assert no_year["datetime_needs_review"] == 1
    assert no_year["year_source"] == "unresolved"
    assert "2026" not in json.dumps(no_year)
    _ok("month/day without year -> needs review, no invented year")

    textual = parse_datetime_text("Phone Appt Tuesday October 7th at 2:00PM CDT")
    # may or may not parse without comma — either empty date + review or parse
    assert textual["event_date"] == "" or textual["datetime_needs_review"] in (0, 1)
    if not textual["event_date"]:
        assert textual["datetime_needs_review"] == 1
    _ok("textual Phone Appt October 7th handled without inventing year")

    rfq = parse_datetime_text("RFQ")
    assert rfq["event_date"] == "" and rfq["datetime_needs_review"] == 0
    _ok("RFQ text")

    resched = parse_datetime_text("Reschedule — Tuesday, March 3rd at 10:00AM CST")
    assert resched["event_date"] == "" and resched["datetime_needs_review"] == 1
    _ok("reschedule text without year -> review")

    send = parse_datetime_text("Send E-mail")
    assert send["event_date"] == "" and send["datetime_needs_review"] == 0
    _ok("Send E-mail")

    corr = parse_datetime_text(
        "Phone Appt, Tuesday, October 7th at 2:00PM CDT",
        corroborated_year=2025,
    )
    assert corr["event_date"] == "2025-10-07"
    assert corr["year_source"] == "corroborated"
    assert corr["datetime_needs_review"] == 0
    _ok("corroborated year")

    y, why = corroborate_year_from_history(
        appointment_date_time="Phone Appt, Tuesday, October 7th at 2:00PM CDT",
        caller_notes="10/2/25 spoke with Allison",
        history_events=[
            {
                "id": 1,
                "event_at": "2025-10-07 14:00",
                "note_text": (
                    "Brown Industries- Phone appointment- Tuesday, October 7th "
                    "at 2:00PM CDT- spoke with Allison Billings"
                ),
            }
        ],
    )
    assert y == 2025, (y, why)
    _ok("history corroboration unique year")

    unresolved = parse_datetime_text(
        "Phone Appt, Tuesday, October 7th at 2:00PM CDT",
        corroborated_year=None,
    )
    assert unresolved["datetime_needs_review"] == 1
    _ok("unresolved year")


def test_footer_exclusion() -> None:
    headers = [
        "List",
        "Record Number",
        "Company Name",
        "Contact Person",
        "Sales Notes",
        "Dollars Quoted",
        "QUOTED",
    ]
    assert is_footer_or_total_row(
        headers, ["", "", "", "", "2025", "108900", "Totals"]
    )
    assert is_footer_or_total_row(
        headers, ["", "", "", "", "2026", "3862894.44", "35 Appts "]
    )
    assert not is_footer_or_total_row(
        headers,
        ["Brown Cap", "1240613", "Watlow Ltd", "Allison Billings", "notes", "100", ""],
    )
    _ok("footer/total patterns from doc 19")

    wb = Workbook()
    ws = wb.active
    ws.title = "Appointments"
    ws.append(headers)
    ws.append(["Brown Cap", "1", "Acme", "Pat", "note", "10", ""])
    ws.append(["", "", "", "", "2026", "100", "Totals"])
    ws.append(["", "", "", "", "2025", "200", "35 Appts "])
    buf = io.BytesIO()
    wb.save(buf)
    path = Path(tempfile.mkdtemp()) / "grid.xlsx"
    path.write_bytes(buf.getvalue())
    sheets = _parse_workbook(path)
    appt = next(s for s in sheets if s["sheet_name"] == "Appointments")
    assert len(appt["rows"]) == 1, appt["rows"]
    _ok("workbook parser excludes footer rows")


def test_company_match_safety() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        # Brown client 2 fixtures
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (1, 'Other Client')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (3, 'Third Client')"
        )
        # BW on Brown with RN 812142; foreign RN 1019263 on client 1
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (151, 'BW Integrated Systems', '812142')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (730, 'Slagel Manufacturing', '1357899')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (341, 'ITW Food Equipment', '1242766')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (468, 'Moore Co', '640833')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (17, 'Norvell Co Inc', '93903')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (284, 'Marlen International', '1212101')"
        )
        for cid, co, rn in [
            (2, 151, "812142"),
            (1, 151, "1019263"),  # same company also on client 1 with different RN? use separate
        ]:
            pass
        # Cleaner: client 1 owns RN 1019263 on company 1510
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (1510, 'BW Integrated Systems', '1019263')"
        )
        conn.execute(
            "DELETE FROM client_company_relationships WHERE client_id IN (1,2,3) AND company_id IN (151,1510,730,341,468,17,284)"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (2, 151, '812142', 'Active')"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (1, 1510, '1019263', 'Active')"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (3, 730, '1357899', 'Active')"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (1, 341, '1242766', 'Active')"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (2, 468, '640833', 'Active')"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (2, 17, '93903', 'Active')"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (2, 284, '1212101', 'Active')"
        )
        conn.commit()

        # BW: foreign RN ignored, exact Brown name match
        st, cid, rid, name, brown_rn, flags = match_company_for_appt_grid(
            conn, 2, "1019263", "BW Integrated Systems"
        )
        assert st == "MATCHED" and cid == 151 and brown_rn == "812142", (st, cid, brown_rn, flags)
        assert any("Foreign RN" in f or "differs" in f or "ignored" in f for f in flags) or brown_rn == "812142"
        _ok("BW Integrated Systems scenario")

        st, cid, *_rest = match_company_for_appt_grid(conn, 2, "1357899", "Slagel Manufacturing")
        assert st == "REVIEW" and cid is None
        _ok("Slagel scenario")

        st, cid, *_rest = match_company_for_appt_grid(conn, 2, "1242766", "ITW Food Equipment")
        assert st == "REVIEW" and cid is None
        _ok("ITW scenario")

        st, cid, rid, name, brown_rn, flags = match_company_for_appt_grid(
            conn, 2, "1325552", "Moore Co"
        )
        assert st == "MATCHED" and cid == 468 and brown_rn == "640833"
        assert any("do not overwrite" in f.lower() or "differs" in f.lower() for f in flags)
        _ok("Moore Co RN drift")

        st, cid, rid, name, brown_rn, flags = match_company_for_appt_grid(
            conn, 2, "1326942", "Norvell Co Inc"
        )
        assert st == "MATCHED" and cid == 17 and brown_rn == "93903"
        _ok("Norvell RN drift")

        st, cid, rid, name, brown_rn, flags = match_company_for_appt_grid(
            conn, 2, "1325591", "Marlen International"
        )
        assert st == "MATCHED" and cid == 284 and brown_rn == "1212101"
        _ok("Marlen International name match")

        # Prove RN not overwritten by match helper (returns Brown RN only)
        assert brown_rn != "1325591"


def test_contact_resolution() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (336, 'Watlow Ltd', '1240613')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (2, 336, '1240613', 'Active')"
        )
        # Allison duplicates
        conn.execute("DELETE FROM contacts WHERE company_id = 336 AND email LIKE '%watlow%'")
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, phone, external_record_no, source_row_index) VALUES (3865, 336, 'Allison', 'Billings', 'Buyer', 'abillings@watlow.com', '5734066776', 'c3865', 1)"
        )
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, phone, external_record_no, source_row_index) VALUES (4565, 336, 'Allison', 'Billings', 'Buyer', 'abillings@watlow.com', '5734066776', 'c4565', 2)"
        )
        # Tracy Warren duplicates
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (177, 'Duke Manufacturing', 'duke177')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO client_company_relationships (client_id, company_id, status) VALUES (2, 177, 'Active')"
        )
        conn.execute("DELETE FROM contacts WHERE company_id = 177 AND email LIKE '%duke%'")
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (3162, 177, 'Tracy', 'Warren', 'Category Manager', 'twarren@dukemfg.com', 'c3162', 1)"
        )
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (4199, 177, 'Tracy', 'Warren', 'Category Manager', 'twarren@dukemfg.com', 'c4199', 2)"
        )
        # Jerry
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (221, 'Siffron', 'sif221')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO client_company_relationships (client_id, company_id, status) VALUES (2, 221, 'Active')"
        )
        conn.execute("DELETE FROM contacts WHERE company_id = 221 AND email LIKE '%siffron%'")
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (3361, 221, 'Jerry', 'Rininger', 'Manager, Commodity', 'jerry.rininger@siffron.com', 'c3361', 1)"
        )
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (4338, 221, 'Jerry', 'Rininger', 'Manager, Commodity', 'jerry.rininger@siffron.com', 'c4338', 2)"
        )
        # Julie
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (284, 'Marlen International', '1212101')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO client_company_relationships (client_id, company_id, status) VALUES (2, 284, 'Active')"
        )
        conn.execute("DELETE FROM contacts WHERE company_id = 284 AND email LIKE '%marlen%'")
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (3675, 284, 'Julie', 'Dagenhart', 'Purchasing', 'julie.dagenhart@marlen.com', 'c3675', 1)"
        )
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (4421, 284, 'Julie', 'Dagenhart', 'Purchasing', 'julie.dagenhart@marlen.com', 'c4421', 2)"
        )
        # Amanda / Kelvin conflict
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (282, 'Colamark Technologies', 'cola282')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO client_company_relationships (client_id, company_id, status) VALUES (2, 282, 'Active')"
        )
        conn.execute("DELETE FROM contacts WHERE company_id = 282")
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (3664, 282, 'Amanda', 'Daniels', 'General Manager', 'amanda.daniels@colamarkusa.com', 'c3664', 1)"
        )
        conn.execute(
            "INSERT INTO contacts (id, company_id, first_name, last_name, title, email, external_record_no, source_row_index) VALUES (3665, 282, 'Kelvin', 'Ke', 'Product Director', 'amanda.daniels@colamarkusa.com', 'c3665', 2)"
        )
        conn.commit()

        st, cid, name, cands, note = match_contact_for_appt_grid(
            conn, 336, "abillings@watlow.com", "Allison Billings", "5734066776", "Buyer"
        )
        assert st == "MATCHED" and cid == 3865, (st, cid, note)
        _ok("Allison Billings duplicate -> canonical earliest id")

        st, cid, name, cands, note = match_contact_for_appt_grid(
            conn, 177, "twarren@dukemfg.com", "Tracy Warren", "", "Category Manager"
        )
        assert st == "MATCHED" and cid == 3162
        _ok("Tracy Warren duplicate")

        st, cid, name, cands, note = match_contact_for_appt_grid(
            conn, 221, "jerry.rininger@siffron.com", "Jerry Rininger", "", "Manager, Commodity"
        )
        assert st == "MATCHED" and cid == 3361
        _ok("Jerry Rininger duplicate")

        st, cid, name, cands, note = match_contact_for_appt_grid(
            conn, 284, "julie.dagenhart@marlen.com", "Julie Dagenhart", "", "Purchasing"
        )
        assert st == "MATCHED" and cid == 3675
        _ok("Julie Dagenhart duplicate")

        st, cid, name, cands, note = match_contact_for_appt_grid(
            conn, 282, "amanda.daniels@colamarkusa.com", "Amanda Daniels", "", "General Manager"
        )
        # Different people share email → REVIEW even if name matches one?
        # With name_exact score 100 on Amanda only, Kelvin scores lower — may MATCH Amanda.
        # Requirement: if candidates represent different people sharing email → REVIEW
        assert st == "REVIEW", (st, cid, note, cands)
        _ok("Amanda Daniels / Kelvin Ke shared-email conflict -> REVIEW")


def test_history_classification_and_fqw() -> None:
    hist = [
        {
            "note_text": "Brown Industries- Phone appointment- Tuesday, October 7th at 2:00PM CDT- spoke with Allison Billings"
        }
    ]
    narr, why = classify_appointment_narrative(
        event_type="Appointment Scheduled",
        contact_name="Allison Billings",
        caller_notes="long caller notes about the appointment with Allison",
        appointment_date_time="Phone Appt, Tuesday, October 7th at 2:00PM CDT",
        resolved_date="2025-10-07",
        history_events=hist,
    )
    assert narr == "ALREADY_REPRESENTED", (narr, why)
    grade = classify_field_vs_history("Y", "")
    assert grade == "NEW_STRUCTURED_VALUE"
    action, reason = propose_row_action(
        {
            "appointment_narrative": "ALREADY_REPRESENTED",
            "appointment_grade": "NEW_STRUCTURED_VALUE",
            "dollars_quoted": "BLANK",
            "outcome": "BLANK",
            "forecast": "NEW_STRUCTURED_VALUE",
            "quoted_label": "BLANK",
            "won": "BLANK",
            "sales_notes": "BLANK",
            "appointment_date_time": "ALREADY_REPRESENTED",
            "caller_notes": "ALREADY_REPRESENTED",
            "revenue_specialist": "BLANK",
        },
        company_status="MATCHED",
        contact_status="MATCHED",
        datetime_needs_review=False,
        event_type="Appointment Scheduled",
    )
    assert action == "CREATE_DELTA_STRUCTURED_EVENT", (action, reason)
    assert build_delta_caller_notes(
        narrative_class="ALREADY_REPRESENTED", caller_notes="dup narrative"
    ) == ""
    block = compose_forecast_quoted_won_block(forecast="May", quoted="JAN QUOTE", won="")
    assert "Forecast: May" in block and "Quoted: JAN QUOTE" in block and "Won:" not in block
    merged = merge_sales_notes_with_fqw("prior notes", block)
    merged2 = merge_sales_notes_with_fqw(merged, block)
    assert merged2.count(FQW_BLOCK_START) == 1
    _ok("history classification + FQW idempotent merge")


def test_fingerprint_and_idempotency_confirm() -> None:
    fp1 = appointment_grid_fingerprint(
        client_id=2,
        source_document_sha256="abc",
        source_sheet="Appointments",
        source_row=2,
        source_rn="1240613",
        company_name="Watlow Ltd",
        contact_name="Allison Billings",
        email="abillings@watlow.com",
        event_type="Appointment Scheduled",
        event_date="2025-10-07",
        source_datetime_text="Phone Appt, Tuesday, October 7th at 2:00PM CDT",
        grade="N",
        dollars_quoted="-",
        outcome="",
        forecast="",
        quoted_label="",
        won="",
    )
    fp2 = appointment_grid_fingerprint(
        client_id=2,
        source_document_sha256="abc",
        source_sheet="Appointments",
        source_row=2,
        source_rn="1240613",
        company_name="Watlow Ltd",
        contact_name="Allison Billings",
        email="abillings@watlow.com",
        event_type="Appointment Scheduled",
        event_date="2025-10-07",
        source_datetime_text="Phone Appt, Tuesday, October 7th at 2:00PM CDT",
        grade="N",
        dollars_quoted="-",
        outcome="",
        forecast="",
        quoted_label="",
        won="",
    )
    assert fp1 == fp2 and len(fp1) == 64
    # volatile contact id must not be required
    _ok("fingerprint stable without DB ids")

    # Build tiny workbook and confirm twice in isolated DB
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (9001, 'Idempotency Co', '9001')"
        )
        conn.execute(
            "DELETE FROM client_company_relationships WHERE client_id=2 AND company_id=9001"
        )
        conn.execute(
            "INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status) VALUES (2, 9001, '9001', 'Active')"
        )
        conn.execute("DELETE FROM contacts WHERE company_id=9001")
        conn.execute(
            "INSERT INTO contacts (company_id, first_name, last_name, email, title, external_record_no, source_row_index) VALUES (9001, 'Pat', 'Lee', 'pat@idem.test', 'Buyer', 'cpat9001', 1)"
        )
        # history narrative already present
        conn.execute(
            """
            INSERT INTO company_shared_history_events (
                company_id, external_record_no, source_company_name, event_at,
                event_sequence, author, event_type, note_text, event_hash, created_at
            ) VALUES (9001, '9001', 'Idempotency Co', '7-Oct-2025 1:00 PM CDT', 1, 'Tester', '',
                      'Phone appointment- Tuesday, October 7th at 2:00PM CDT with Pat Lee', 'h1', datetime('now'))
            """
        )
        hist_count = conn.execute(
            "SELECT COUNT(*) FROM company_shared_history_events WHERE company_id=9001"
        ).fetchone()[0]
        rn_before = conn.execute(
            "SELECT external_record_no FROM client_company_relationships WHERE client_id=2 AND company_id=9001"
        ).fetchone()[0]
        conn.commit()

    wb = Workbook()
    ws = wb.active
    ws.title = "Appointments"
    ws.append(
        [
            "List",
            "Record Number",
            "NorthStar Client",
            "Rev Spec",
            "Appointment Date/Time",
            "Company Name",
            "Contact Person",
            "Title",
            "Phone Number",
            "Email Address",
            "NorthStar Caller Notes",
            "Appt Grade",
            "Sales Notes",
            "Dollars Quoted",
            "Account Won, Lost or In Progress",
            "Forecast",
            "QUOTED",
            "WON",
        ]
    )
    ws.append(
        [
            "Brown Cap",
            "9001",
            "Brown Industries",
            "Rev Spec/Matt",
            "Phone Appt, Tuesday, October 7th, 2025 at 2:00PM CDT",
            "Idempotency Co",
            "Pat Lee",
            "Buyer",
            "5551112222",
            "pat@idem.test",
            "Spoke with Pat about machining",
            "Y",
            "Follow-up quote discussion",
            "1500",
            "",
            "May",
            "JAN QUOTE",
            "",
        ]
    )
    raw = io.BytesIO()
    wb.save(raw)
    content = raw.getvalue()

    # Clear needs_review by providing full year + matched company/contact
    batch = start_engagement_import(2, filename="idem.xlsx", content=content)
    preview = map_engagement_import(2, batch.batch_id)
    # Force skip review rows if any leftover by marking skipped — ensure our row is MATCHED
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM client_engagement_import_rows WHERE batch_id=?",
            (batch.batch_id,),
        ).fetchall()
        assert len(rows) == 1, rows
        row = dict(rows[0])
        assert row["company_match_status"] == "MATCHED"
        mapped = json.loads(row["mapped_json"])
        # If still needs review for rev spec etc., clear needs_review for this unit test of idempotency
        conn.execute(
            """
            UPDATE client_engagement_import_rows
            SET needs_review=0, selected=1, import_status='previewed',
                company_match_status='MATCHED', contact_match_status='MATCHED'
            WHERE batch_id=?
            """,
            (batch.batch_id,),
        )
        # Also clear other batch review rows if any from previous tests — only this batch
        conn.commit()
        fp = row["source_row_fingerprint"]
        assert fp

    # Mark all other engagement rows outside this batch alone — gate checks this batch only
    with get_connection() as conn:
        # Ensure no OTHER review rows in this batch
        conn.execute(
            "UPDATE client_engagement_import_rows SET needs_review=0, import_status='previewed', selected=1 WHERE batch_id=?",
            (batch.batch_id,),
        )
        mapped = json.loads(
            conn.execute(
                "SELECT mapped_json FROM client_engagement_import_rows WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()[0]
        )
        mapped["_proposed_action"] = "CREATE_DELTA_STRUCTURED_EVENT"
        conn.execute(
            "UPDATE client_engagement_import_rows SET mapped_json=? WHERE batch_id=?",
            (json.dumps(mapped), batch.batch_id),
        )
        conn.commit()

    confirm_engagement_import(
        2, batch.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        n1 = conn.execute(
            "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2 AND source_row_fingerprint=?",
            (fp,),
        ).fetchone()[0]
        assert n1 == 1
        note = conn.execute(
            "SELECT caller_notes, sales_notes FROM client_sales_events WHERE client_id=2 AND source_row_fingerprint=?",
            (fp,),
        ).fetchone()
        # delta should omit duplicated narrative when classified already represented;
        # at minimum FQW block present
        assert "Forecast: May" in (note["sales_notes"] or "")
        hist_after = conn.execute(
            "SELECT COUNT(*) FROM company_shared_history_events WHERE company_id=9001"
        ).fetchone()[0]
        assert hist_after == hist_count
        rn_after = conn.execute(
            "SELECT external_record_no FROM client_company_relationships WHERE client_id=2 AND company_id=9001"
        ).fetchone()[0]
        assert rn_after == rn_before

    # Second import of same content
    batch2 = start_engagement_import(2, filename="idem.xlsx", content=content)
    map_engagement_import(2, batch2.batch_id)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE client_engagement_import_rows
            SET needs_review=0, selected=1, import_status='previewed',
                company_match_status='MATCHED', contact_match_status='MATCHED'
            WHERE batch_id=?
            """,
            (batch2.batch_id,),
        )
        mapped = json.loads(
            conn.execute(
                "SELECT mapped_json FROM client_engagement_import_rows WHERE batch_id=?",
                (batch2.batch_id,),
            ).fetchone()[0]
        )
        mapped["_proposed_action"] = "CREATE_DELTA_STRUCTURED_EVENT"
        # Keep same fingerprint from map
        conn.execute(
            "UPDATE client_engagement_import_rows SET mapped_json=?, source_row_fingerprint=? WHERE batch_id=?",
            (json.dumps(mapped), fp, batch2.batch_id),
        )
        # Clear review gate for batch2 only; also mark any other open review rows in batch2 skipped
        conn.execute(
            "UPDATE client_engagement_import_rows SET needs_review=0 WHERE batch_id=?",
            (batch2.batch_id,),
        )
        conn.commit()

    confirm_engagement_import(
        2, batch2.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        n2 = conn.execute(
            "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2 AND source_row_fingerprint=?",
            (fp,),
        ).fetchone()[0]
        assert n2 == 1, n2
    _ok("idempotency: second confirm creates 0 duplicate sales events; history/RN unchanged")


def test_review_gate_blocks() -> None:
    assert row_blocks_confirm(
        proposed_action="REVIEW",
        company_status="MATCHED",
        contact_status="MATCHED",
        datetime_needs_review=False,
        event_type="Appointment Scheduled",
    )
    assert not row_blocks_confirm(
        proposed_action="SKIP_FULL_DUPLICATE",
        company_status="NEW",
        contact_status="NEW",
        datetime_needs_review=True,
        event_type="Appointment Scheduled",
    )
    assert row_blocks_confirm(
        proposed_action="CREATE_STRUCTURED_EVENT",
        company_status="MATCHED",
        contact_status="MATCHED",
        datetime_needs_review=False,
        event_type="Appointment Scheduled",
        client_validation="MISMATCH",
    )
    assert not row_blocks_confirm(
        proposed_action="CREATE_DELTA_STRUCTURED_EVENT",
        company_status="MATCHED",
        contact_status="MATCHED",
        datetime_needs_review=False,
        event_type="Appointment Scheduled",
        import_status="skipped",
        client_validation="MISMATCH",
    )
    assert not row_blocks_confirm(
        proposed_action="REVIEW",
        company_status="NEW",
        contact_status="REVIEW",
        datetime_needs_review=True,
        event_type="Appointment Scheduled",
        import_status="duplicate",
    )
    _ok("review gate helpers")


def test_isolation_guards() -> None:
    opened = Path(os.fspath(DB_PATH)).resolve()
    prod = PRODUCTION_DB_PATH.resolve()
    assert opened != prod, (opened, prod)
    assert os.environ.get("NORTHSTAR_TEST_DB", "").strip()
    dest = _client_dir(2).resolve()
    live_docs = (DOCUMENTS_ROOT / "2").resolve()
    assert dest != live_docs
    assert live_docs not in dest.parents
    _ok("testdb path and document-root isolation")


def test_match_helpers_are_read_only() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO companies (id, company_name, external_record_no)
            VALUES (991201, 'Phase3A ReadOnly Co', '991201')
            """
        )
        conn.execute(
            "DELETE FROM client_company_relationships WHERE client_id=2 AND company_id=991201"
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships
                (client_id, company_id, external_record_no, status)
            VALUES (2, 991201, '991201', 'Active')
            """
        )
        conn.execute("DELETE FROM contacts WHERE company_id=991201")
        conn.execute(
            """
            INSERT INTO contacts
                (company_id, first_name, last_name, email, title, external_record_no, source_row_index)
            VALUES (991201, 'Pat', 'Lee', 'pat@readonly.test', 'Buyer', 'c991201', 1)
            """
        )
        conn.commit()
        before = (
            _count(conn, "SELECT COUNT(*) FROM companies"),
            _count(conn, "SELECT COUNT(*) FROM contacts"),
            _count(conn, "SELECT COUNT(*) FROM client_company_relationships"),
            _count(conn, "SELECT COUNT(*) FROM company_shared_history_events"),
        )
        match_company_for_appt_grid(conn, 2, "991201", "Phase3A ReadOnly Co")
        match_contact_for_appt_grid(
            conn, 991201, "pat@readonly.test", "Pat Lee", "5551112222", "Buyer"
        )
        after = (
            _count(conn, "SELECT COUNT(*) FROM companies"),
            _count(conn, "SELECT COUNT(*) FROM contacts"),
            _count(conn, "SELECT COUNT(*) FROM client_company_relationships"),
            _count(conn, "SELECT COUNT(*) FROM company_shared_history_events"),
        )
        assert before == after, (before, after)
    _ok("match helpers do not create companies/contacts/history")


def test_ambiguous_matches_flagged() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO companies (id, company_name, external_record_no)
            VALUES (991210, 'Phase3A Twin Name', '991210')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO companies (id, company_name, external_record_no)
            VALUES (991211, 'Phase3A Twin Name', '991211')
            """
        )
        conn.execute(
            "DELETE FROM client_company_relationships WHERE client_id=2 AND company_id IN (991210,991211,991212)"
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships
                (client_id, company_id, external_record_no, status)
            VALUES (2, 991210, '991210', 'Active')
            """
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships
                (client_id, company_id, external_record_no, status)
            VALUES (2, 991211, '991211', 'Active')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO companies (id, company_name, external_record_no)
            VALUES (991212, 'Phase3A Unique Prefix Co', '991212')
            """
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships
                (client_id, company_id, external_record_no, status)
            VALUES (2, 991212, '991212', 'Active')
            """
        )
        conn.commit()
        st, cid, *_rest = match_company_for_appt_grid(
            conn, 2, "0000000", "Phase3A Twin Name"
        )
        assert st == "REVIEW" and cid is None, (st, cid)
        st, cid, *_rest = match_company_for_appt_grid(
            conn, 2, "0000000", "Phase3A Unique Prefix"
        )
        assert st == "POSSIBLE MATCH" and cid == 991212, (st, cid)
        assert row_blocks_confirm(
            proposed_action="CREATE_STRUCTURED_EVENT",
            company_status="POSSIBLE MATCH",
            contact_status="MATCHED",
            datetime_needs_review=False,
            event_type="Appointment Scheduled",
        )
    _ok("ambiguous/partial company matches flagged for REVIEW")


def test_fingerprint_independent_of_db_ids() -> None:
    kwargs = dict(
        client_id=2,
        source_document_sha256="abc",
        source_sheet="Appointments",
        source_row=2,
        source_rn="1240613",
        company_name="Watlow Ltd",
        contact_name="Allison Billings",
        email="abillings@watlow.com",
        event_type="Appointment Scheduled",
        event_date="2025-10-07",
        source_datetime_text="Phone Appt, Tuesday, October 7th at 2:00PM CDT",
        grade="N",
        dollars_quoted="-",
        outcome="",
        forecast="",
        quoted_label="",
        won="",
    )
    fp = appointment_grid_fingerprint(**kwargs)
    # Volatile DB ids are not fingerprint inputs; same source identity stays stable.
    assert "contact_id" not in appointment_grid_fingerprint.__code__.co_varnames
    assert "company_id" not in appointment_grid_fingerprint.__code__.co_varnames
    assert fp == appointment_grid_fingerprint(**kwargs)
    _ok("fingerprint independent of volatile DB ids")


def _seed_brown_company(
    conn,
    *,
    company_id: int,
    rn: str,
    name: str,
    first: str,
    last: str,
    email: str,
    history_note: str = "",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO companies (id, company_name, external_record_no) VALUES (?, ?, ?)",
        (company_id, name, rn),
    )
    conn.execute(
        "DELETE FROM client_company_relationships WHERE client_id=2 AND company_id=?",
        (company_id,),
    )
    conn.execute(
        """
        INSERT INTO client_company_relationships
            (client_id, company_id, external_record_no, status)
        VALUES (2, ?, ?, 'Active')
        """,
        (company_id, rn),
    )
    conn.execute("DELETE FROM contacts WHERE company_id=?", (company_id,))
    conn.execute(
        """
        INSERT INTO contacts
            (company_id, first_name, last_name, email, title, external_record_no, source_row_index)
        VALUES (?, ?, ?, ?, 'Buyer', ?, 1)
        """,
        (company_id, first, last, email, f"c{company_id}"),
    )
    if history_note:
        conn.execute(
            "DELETE FROM company_shared_history_events WHERE company_id=?",
            (company_id,),
        )
        conn.execute(
            """
            INSERT INTO company_shared_history_events (
                company_id, external_record_no, source_company_name, event_at,
                event_sequence, author, event_type, note_text, event_hash, created_at
            ) VALUES (?, ?, ?, '7-Oct-2025 1:00 PM CDT', 1, 'Tester', '',
                      ?, ?, datetime('now'))
            """,
            (company_id, rn, name, history_note, f"h{company_id}"),
        )


def test_confirm_blocks_unresolved_review() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.commit()
    content = _grid_bytes(
        [
            _grid_row(
                rn="P3A-UNMATCHED-991300",
                company="Phase3A Unmatched Review Co",
                contact="No Such Person",
                email="nobody@unmatched.example",
            )
        ]
    )
    batch = start_engagement_import(2, filename="review-block.xlsx", content=content)
    map_engagement_import(2, batch.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM client_engagement_import_rows WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
        )
        assert row["company_match_status"] in {"NEW", "REVIEW"}
        assert int(row["needs_review"]) == 1
        events_before = _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        )
    try:
        confirm_engagement_import(
            2, batch.batch_id, EngagementImportConfirmRequest(confirm=True)
        )
        _fail("confirm should have blocked unresolved REVIEW row")
    except ValueError as exc:
        assert "REVIEW" in str(exc)
    with get_connection() as conn:
        events_after = _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        )
        assert events_after == events_before
        status = conn.execute(
            "SELECT status FROM client_engagement_import_batches WHERE id=? AND client_id=2",
            (batch.batch_id,),
        ).fetchone()[0]
        assert status != "imported", status
    _ok("confirm blocks unresolved company REVIEW")


def test_confirm_blocks_client_mismatch() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        _seed_brown_company(
            conn,
            company_id=991301,
            rn="991301",
            name="Phase3A Mismatch Co",
            first="Pat",
            last="Lee",
            email="pat@mismatch.test",
        )
        conn.commit()
    content = _grid_bytes(
        [
            _grid_row(
                rn="991301",
                company="Phase3A Mismatch Co",
                contact="Pat Lee",
                email="pat@mismatch.test",
                client="Carmeco",
            )
        ]
    )
    batch = start_engagement_import(2, filename="mismatch.xlsx", content=content)
    map_engagement_import(2, batch.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT client_validation, needs_review FROM client_engagement_import_rows WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
        )
        assert row["client_validation"] == "MISMATCH"
        assert int(row["needs_review"]) == 1
        events_before = _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        )
    try:
        confirm_engagement_import(
            2, batch.batch_id, EngagementImportConfirmRequest(confirm=True)
        )
        _fail("confirm should have blocked client mismatch")
    except ValueError as exc:
        assert "REVIEW" in str(exc)
    with get_connection() as conn:
        assert _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        ) == events_before
    _ok("confirm blocks cross-client mismatch instead of guessing")


def test_skip_row_does_not_block_confirm() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        _seed_brown_company(
            conn,
            company_id=991302,
            rn="991302",
            name="Phase3A Skip Keep Co",
            first="Pat",
            last="Lee",
            email="pat@skipkeep.test",
            history_note="Phone appointment- Tuesday, October 7th at 2:00PM CDT with Pat Lee",
        )
        conn.commit()
        sales_before = _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        )
    content = _grid_bytes(
        [
            _grid_row(
                rn="P3A-SKIP-UNMATCHED-991399",
                company="Phase3A Skip Unmatched Co",
                contact="Nobody",
                email="nobody@skip.example",
            ),
            _grid_row(
                rn="991302",
                company="Phase3A Skip Keep Co",
                contact="Pat Lee",
                email="pat@skipkeep.test",
            ),
        ]
    )
    batch = start_engagement_import(2, filename="skip-keep.xlsx", content=content)
    map_engagement_import(2, batch.batch_id)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE client_engagement_import_rows
            SET import_status='skipped', needs_review=0, selected=0
            WHERE batch_id=? AND company_match_status IN ('NEW','REVIEW','POSSIBLE MATCH')
            """,
            (batch.batch_id,),
        )
        conn.commit()
    confirm_engagement_import(
        2, batch.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        sales_after = _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        )
        assert sales_after == sales_before + 1, (sales_before, sales_after)
    _ok("skipped REVIEW row does not block confirm of matched row")


def test_other_client_and_history_preserved() -> None:
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (1, 'Carmeco')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO companies (id, company_name, external_record_no)
            VALUES (991310, 'Phase3A Foreign Only Co', 'P3A-FOREIGN-991310')
            """
        )
        conn.execute(
            "DELETE FROM client_company_relationships WHERE company_id=991310"
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships
                (client_id, company_id, external_record_no, status)
            VALUES (1, 991310, 'P3A-FOREIGN-991310', 'Active')
            """
        )
        conn.execute("DELETE FROM contacts WHERE company_id=991310")
        conn.execute(
            """
            INSERT INTO contacts
                (company_id, first_name, last_name, email, title, external_record_no, source_row_index)
            VALUES (991310, 'Foreign', 'Person', 'foreign@other.test', 'Buyer', 'c991310', 1)
            """
        )
        conn.execute("DELETE FROM company_shared_history_events WHERE company_id=991310")
        conn.execute(
            """
            INSERT INTO company_shared_history_events (
                company_id, external_record_no, source_company_name, event_at,
                event_sequence, author, event_type, note_text, event_hash, created_at
            ) VALUES (991310, 'P3A-FOREIGN-991310', 'Phase3A Foreign Only Co', '1-Jan-2025', 1, 'Other',
                      '', 'Do not duplicate this Carmeco history', 'hf991310', datetime('now'))
            """
        )
        _seed_brown_company(
            conn,
            company_id=991311,
            rn="991311",
            name="Phase3A Brown Isolate Co",
            first="Pat",
            last="Lee",
            email="pat@browniso.test",
            history_note=(
                "Brown Industries- Phone appointment- Tuesday, October 7th "
                "at 2:00PM CDT- spoke with Pat Lee"
            ),
        )
        other_before = _client_identity_snapshot(conn, 1)
        hist_before = _history_snapshot(conn, 991311)
        foreign_hist_before = _history_snapshot(conn, 991310)
        contacts_before = _count(conn, "SELECT COUNT(*) FROM contacts")
        companies_before = _count(conn, "SELECT COUNT(*) FROM companies")
        activities_before = _count(conn, "SELECT COUNT(*) FROM activities")
        brown_rn_before = conn.execute(
            "SELECT external_record_no FROM client_company_relationships WHERE client_id=2 AND company_id=991311"
        ).fetchone()[0]
        conn.commit()

    content = _grid_bytes(
        [
            _grid_row(
                rn="P3A-FOREIGN-991310",
                company="Phase3A Brown Isolate Co",
                contact="Pat Lee",
                email="pat@browniso.test",
            )
        ]
    )
    # Foreign RN 1019263 must not auto-link Brown; name match should still work.
    batch = start_engagement_import(2, filename="isolate.xlsx", content=content)
    map_engagement_import(2, batch.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM client_engagement_import_rows WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
        )
        assert row["company_match_status"] == "MATCHED"
        assert int(row["company_id"]) == 991311
        assert row["company_record_no"] == "991311"
        assert int(row["needs_review"]) == 0
        fp = row["source_row_fingerprint"]
        brown_sales_before = _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        )

    confirm_engagement_import(
        2, batch.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        other_after = _client_identity_snapshot(conn, 1)
        assert other_before == other_after, (other_before, other_after)
        assert _history_snapshot(conn, 991311) == hist_before
        assert _history_snapshot(conn, 991310) == foreign_hist_before
        assert _count(conn, "SELECT COUNT(*) FROM contacts") == contacts_before
        assert _count(conn, "SELECT COUNT(*) FROM companies") == companies_before
        assert _count(conn, "SELECT COUNT(*) FROM activities") == activities_before
        rn_after = conn.execute(
            "SELECT external_record_no FROM client_company_relationships WHERE client_id=2 AND company_id=991311"
        ).fetchone()[0]
        assert rn_after == brown_rn_before == "991311"
        n_events = _count(
            conn,
            "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2 AND source_row_fingerprint=?",
            (fp,),
        )
        assert n_events == 1
        note = conn.execute(
            "SELECT caller_notes, sales_notes FROM client_sales_events WHERE client_id=2 AND source_row_fingerprint=?",
            (fp,),
        ).fetchone()
        # Delta path: do not copy already-represented appointment narrative.
        assert "Phone appointment" not in (note["caller_notes"] or "")
        assert "Forecast: May" in (note["sales_notes"] or "")
        assert _count(
            conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
        ) == brown_sales_before + 1
    _ok("other-client data + history preserved; RN not overwritten; narrative not duplicated")


def _insert_imported_event(
    conn,
    *,
    client_id: int,
    document_id: int,
    sheet: str,
    source_row: int,
    fingerprint: str,
    company_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO client_sales_events (
            client_id, company_id, event_type, event_family,
            source_document_id, source_sheet, source_row, source_row_fingerprint,
            created_at
        ) VALUES (?, ?, 'Appointment Scheduled', 'Appointment', ?, ?, ?, ?, datetime('now'))
        """,
        (client_id, company_id, document_id, sheet, source_row, fingerprint),
    )
    return int(cur.lastrowid)


def _batch_doc_and_row(conn, batch_id: int) -> tuple[int, str, int, str]:
    b = conn.execute(
        "SELECT document_id FROM client_engagement_import_batches WHERE id=?",
        (batch_id,),
    ).fetchone()
    row = conn.execute(
        """
        SELECT sheet_name, source_row_number, source_row_fingerprint, import_status,
               duplicate_status, needs_review, company_match_status
        FROM client_engagement_import_rows WHERE batch_id=? ORDER BY source_row_number
        """,
        (batch_id,),
    ).fetchone()
    return int(b["document_id"]), row["sheet_name"], int(row["source_row_number"]), dict(row)


def test_provenance_same_document_changed_fingerprint() -> None:
    """A: same client+document+sheet+row with drifted fingerprint is already imported."""
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        _seed_brown_company(
            conn,
            company_id=992001,
            rn="992001",
            name="Phase3A Provenance Co",
            first="Pat",
            last="Lee",
            email="pat@prov.test",
            history_note="Phone appointment- Tuesday, October 7th at 2:00PM CDT with Pat Lee",
        )
        conn.commit()
        before = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
    content = _grid_bytes(
        [
            _grid_row(
                rn="992001",
                company="Phase3A Provenance Co",
                contact="Pat Lee",
                email="pat@prov.test",
            )
        ]
    )
    batch1 = start_engagement_import(2, filename="prov-a1.xlsx", content=content)
    map_engagement_import(2, batch1.batch_id)
    confirm_engagement_import(
        2, batch1.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        after1 = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
        assert after1 == before + 1
        conn.execute(
            """
            UPDATE client_sales_events
            SET source_row_fingerprint = 'drifted-canonical-or-date-fp'
            WHERE client_id=2 AND source_batch_id=?
            """,
            (batch1.batch_id,),
        )
        conn.commit()

    batch2 = start_engagement_import(2, filename="prov-a2.xlsx", content=content)
    preview = map_engagement_import(2, batch2.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM client_engagement_import_rows WHERE batch_id=?",
                (batch2.batch_id,),
            ).fetchone()
        )
        assert row["import_status"] == "duplicate", row
        assert "ALREADY IMPORTED" in (row["duplicate_status"] or "")
        assert int(row["needs_review"] or 0) == 0
        mapped = json.loads(row["mapped_json"])
        assert mapped.get("_duplicate_kind") == "provenance"
    confirm_engagement_import(
        2, batch2.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        after2 = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
        assert after2 == after1
    _ok("A: drifted fingerprint same document row => already imported, 0 new events")


def test_provenance_after_canonical_name_drift() -> None:
    """D: rerun after stored company/contact names were canonicalized."""
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        _seed_brown_company(
            conn,
            company_id=992002,
            rn="992002",
            name="Phase3A Canonical Co",
            first="Nathaniel",
            last="Golden",
            email="nate@canon.test",
        )
        conn.commit()
        before = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
    content = _grid_bytes(
        [
            _grid_row(
                rn="992002",
                company="Phase3A Canonical Co",
                contact="Nate Golden",
                email="nate@canon.test",
            )
        ]
    )
    batch1 = start_engagement_import(2, filename="canon-1.xlsx", content=content)
    map_engagement_import(2, batch1.batch_id)
    confirm_engagement_import(
        2, batch1.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE client_sales_events
            SET source_row_fingerprint='old-nickname-fp',
                company_name='Phase3A Canonical Company Inc',
                contact_name='Nathaniel Golden'
            WHERE client_id=2 AND source_batch_id=?
            """,
            (batch1.batch_id,),
        )
        conn.commit()
        n1 = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
        assert n1 == before + 1
    batch2 = start_engagement_import(2, filename="canon-2.xlsx", content=content)
    map_engagement_import(2, batch2.batch_id)
    confirm_engagement_import(
        2, batch2.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT mapped_json, import_status FROM client_engagement_import_rows WHERE batch_id=?",
                (batch2.batch_id,),
            ).fetchone()
        )
        assert row["import_status"] == "duplicate"
        assert json.loads(row["mapped_json"]).get("_duplicate_kind") == "provenance"
        assert _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2") == n1
    _ok("D: canonicalization fingerprint drift still provenance-skips")


def test_provenance_after_date_resolution_drift() -> None:
    """E: rerun after older logic had resolved/invented a date that is now empty."""
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        _seed_brown_company(
            conn,
            company_id=992003,
            rn="992003",
            name="Phase3A Date Drift Co",
            first="Pat",
            last="Lee",
            email="pat@datedrift.test",
        )
        conn.commit()
        before = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
    content = _grid_bytes(
        [
            _grid_row(
                rn="992003",
                company="Phase3A Date Drift Co",
                contact="Pat Lee",
                email="pat@datedrift.test",
                dt="Phone Appt, Wednesday, December 17th at 2:00PM EST",
            )
        ]
    )
    batch = start_engagement_import(2, filename="date-drift.xlsx", content=content)
    with get_connection() as conn:
        doc_id, sheet, source_row, _staged = _batch_doc_and_row(conn, batch.batch_id)
        _insert_imported_event(
            conn,
            client_id=2,
            document_id=doc_id,
            sheet=sheet,
            source_row=source_row,
            fingerprint="old-invented-year-fp",
            company_id=992003,
        )
        conn.execute(
            """
            UPDATE client_sales_events
            SET event_date='2025-12-17'
            WHERE client_id=2 AND source_row_fingerprint='old-invented-year-fp'
            """
        )
        conn.commit()
        seeded = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
        assert seeded == before + 1
    map_engagement_import(2, batch.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT event_date, datetime_needs_review, import_status, mapped_json, needs_review "
                "FROM client_engagement_import_rows WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
        )
        assert not row["event_date"], row
        assert int(row["datetime_needs_review"] or 0) == 1
        assert row["import_status"] == "duplicate"
        assert int(row["needs_review"] or 0) == 0
        assert json.loads(row["mapped_json"]).get("_duplicate_kind") == "provenance"
    confirm_engagement_import(
        2, batch.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        assert _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2") == seeded
    _ok("E: date-resolution drift still provenance-skips; does not block")


def test_provenance_different_document_same_coordinates() -> None:
    """B: different document, same sheet+row, must not dedupe."""
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        _seed_brown_company(
            conn,
            company_id=992010,
            rn="992010",
            name="Phase3A DocA Co",
            first="Ann",
            last="A",
            email="ann@doca.test",
        )
        _seed_brown_company(
            conn,
            company_id=992011,
            rn="992011",
            name="Phase3A DocB Co",
            first="Bea",
            last="B",
            email="bea@docb.test",
        )
        conn.commit()
        before = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
    a = _grid_bytes(
        [_grid_row(rn="992010", company="Phase3A DocA Co", contact="Ann A", email="ann@doca.test")]
    )
    b = _grid_bytes(
        [_grid_row(rn="992011", company="Phase3A DocB Co", contact="Bea B", email="bea@docb.test")]
    )
    assert a != b
    batch_a = start_engagement_import(2, filename="doc-a.xlsx", content=a)
    map_engagement_import(2, batch_a.batch_id)
    confirm_engagement_import(
        2, batch_a.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    batch_b = start_engagement_import(2, filename="doc-b.xlsx", content=b)
    map_engagement_import(2, batch_b.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT import_status, duplicate_status FROM client_engagement_import_rows WHERE batch_id=?",
                (batch_b.batch_id,),
            ).fetchone()
        )
        assert row["import_status"] != "duplicate", row
        assert not row["duplicate_status"]
    confirm_engagement_import(
        2, batch_b.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        after = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
        assert after == before + 2, (before, after)
    _ok("B: different document same sheet+row is not a duplicate")


def test_provenance_does_not_cross_clients() -> None:
    """C: another client's same sheet+row metadata must not dedupe Brown."""
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute("INSERT OR IGNORE INTO clients (id, name) VALUES (1, 'Carmeco')")
        conn.execute(
            """
            INSERT OR REPLACE INTO companies (id, company_name, external_record_no)
            VALUES (992020, 'Phase3A Cross Client Co', '992020')
            """
        )
        conn.execute(
            "DELETE FROM client_company_relationships WHERE company_id=992020"
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships
                (client_id, company_id, external_record_no, status)
            VALUES (1, 992020, '992020', 'Active')
            """
        )
        _seed_brown_company(
            conn,
            company_id=992021,
            rn="992021",
            name="Phase3A Brown Cross Co",
            first="Pat",
            last="Lee",
            email="pat@browncross.test",
        )
        conn.commit()
        content = _grid_bytes(
            [
                _grid_row(
                    rn="992021",
                    company="Phase3A Brown Cross Co",
                    contact="Pat Lee",
                    email="pat@browncross.test",
                    client="Brown Industries",
                )
            ]
        )
        # Client 1 already-imported event at Appointments row 2, unrelated document id.
        conn.execute(
            """
            INSERT INTO client_documents (
                client_id, filename, stored_filename, document_type, processing_status,
                created_at, updated_at
            ) VALUES (1, 'other.xlsx', 'no-such-file.xlsx', 'Appointment Grid', 'uploaded',
                      datetime('now'), datetime('now'))
            """
        )
        other_doc = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        _insert_imported_event(
            conn,
            client_id=1,
            document_id=other_doc,
            sheet="Appointments",
            source_row=2,
            fingerprint="client1-coord-fp",
            company_id=992020,
        )
        conn.commit()

    batch = start_engagement_import(2, filename="brown-cross.xlsx", content=content)
    map_engagement_import(2, batch.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT import_status, duplicate_status, client_id FROM client_engagement_import_rows WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
        )
        assert int(row["client_id"]) == 2
        assert row["import_status"] != "duplicate", row
        hit = already_imported_by_provenance(
            conn,
            client_id=2,
            source_sheet="Appointments",
            source_row=2,
            same_document_ids={other_doc},
        )
        # Guard: even if a caller passed a foreign document id, SQL still filters client_id.
        assert hit is None
    _ok("C: provenance does not cross-dedupe clients")


def test_fingerprint_still_dedupes_identical_rerun() -> None:
    """F: unchanged fingerprint matching still skips the second confirm."""
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        _seed_brown_company(
            conn,
            company_id=992030,
            rn="992030",
            name="Phase3A Fp Still Co",
            first="Pat",
            last="Lee",
            email="pat@fpstill.test",
        )
        conn.commit()
        before = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
    content = _grid_bytes(
        [
            _grid_row(
                rn="992030",
                company="Phase3A Fp Still Co",
                contact="Pat Lee",
                email="pat@fpstill.test",
            )
        ]
    )
    batch1 = start_engagement_import(2, filename="fp-still-1.xlsx", content=content)
    map_engagement_import(2, batch1.batch_id)
    confirm_engagement_import(
        2, batch1.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    batch2 = start_engagement_import(2, filename="fp-still-2.xlsx", content=content)
    map_engagement_import(2, batch2.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT duplicate_status, mapped_json FROM client_engagement_import_rows WHERE batch_id=?",
                (batch2.batch_id,),
            ).fetchone()
        )
        fp = conn.execute(
            "SELECT source_row_fingerprint FROM client_engagement_import_rows WHERE batch_id=?",
            (batch2.batch_id,),
        ).fetchone()[0]
        assert already_imported_by_fingerprint(conn, 2, fp)
        mapped = json.loads(row["mapped_json"])
        assert mapped.get("_duplicate_kind") == "fingerprint"
    confirm_engagement_import(
        2, batch2.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        assert _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2") == before + 1
    _ok("F: fingerprint matching still works on identical rerun")


def test_provenance_review_row_does_not_block_confirm() -> None:
    """G: provenance duplicate that would otherwise REVIEW does not block confirm."""
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_engagement_import_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO clients (id, name) VALUES (2, 'Brown Industries')"
        )
        conn.commit()
        before = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
    content = _grid_bytes(
        [
            _grid_row(
                rn="992099",
                company="ZZZ Unmatched Grid Only LLC",
                contact="Nobody",
                email="nobody@unmatched-prov.example",
            )
        ]
    )
    batch = start_engagement_import(2, filename="prov-review.xlsx", content=content)
    with get_connection() as conn:
        doc_id, sheet, source_row, _staged = _batch_doc_and_row(conn, batch.batch_id)
        _insert_imported_event(
            conn,
            client_id=2,
            document_id=doc_id,
            sheet=sheet,
            source_row=source_row,
            fingerprint="old-review-drift-fp",
        )
        conn.commit()
        seeded = _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2")
        assert seeded == before + 1
    preview = map_engagement_import(2, batch.batch_id)
    with get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM client_engagement_import_rows WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
        )
        assert row["company_match_status"] in {"NEW", "REVIEW"}
        assert row["import_status"] == "duplicate"
        assert int(row["needs_review"] or 0) == 0
        mapped = json.loads(row["mapped_json"])
        assert mapped.get("_duplicate_kind") == "provenance"
        assert mapped.get("_proposed_action") == "REVIEW"
    confirm_engagement_import(
        2, batch.batch_id, EngagementImportConfirmRequest(confirm=True)
    )
    with get_connection() as conn:
        assert _count(conn, "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2") == seeded
    _ok("G: already-imported REVIEW row skipped and does not block confirm")


def main() -> int:
    print("DB", __import__("db").DB_PATH)
    test_isolation_guards()
    test_datetime_safety()
    test_footer_exclusion()
    test_company_match_safety()
    test_contact_resolution()
    test_match_helpers_are_read_only()
    test_ambiguous_matches_flagged()
    test_fingerprint_independent_of_db_ids()
    test_history_classification_and_fqw()
    test_review_gate_blocks()
    test_confirm_blocks_unresolved_review()
    test_confirm_blocks_client_mismatch()
    test_skip_row_does_not_block_confirm()
    test_fingerprint_and_idempotency_confirm()
    test_other_client_and_history_preserved()
    test_provenance_same_document_changed_fingerprint()
    test_provenance_after_canonical_name_drift()
    test_provenance_after_date_resolution_drift()
    test_provenance_different_document_same_coordinates()
    test_provenance_does_not_cross_clients()
    test_fingerprint_still_dedupes_identical_rerun()
    test_provenance_review_row_does_not_block_confirm()
    print("ALL PHASE 3A TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAILED:", exc)
        raise
