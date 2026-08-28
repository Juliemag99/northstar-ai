"""Client-scoped writes require an explicit positive client_id.

Run: python test_write_requires_client_id.py

Creates and deletes temporary company/CCR/note/activity rows (NSWRT).
Does not change Flora Jia, Whirlpool, or production identity values.
Uses the isolated testdb copy — never writes through live :8007 or northstar.db.
"""

from __future__ import annotations

import testdb
import sys
import time
from pathlib import Path

from db import get_connection

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSWRT"
CARMECO_ID = 1
BROWN_ID = 2
REPO_ROOT = Path(__file__).resolve().parents[1]


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _reject_ok(status: int, payload: dict, label: str) -> None:
    if status not in {400, 422}:
        _fail(f"{label}: expected HTTP 400 or 422, got {status}: {payload}")


def _snapshot(conn, company_id: int) -> dict:
    return {
        "ccr": [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, client_id, status, next_action, follow_up_date
                FROM client_company_relationships
                WHERE company_id = ?
                ORDER BY client_id, id
                """,
                (company_id,),
            )
        ],
        "notes": [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, client_id, company_id, note_text
                FROM legacy_notes
                WHERE company_id = ?
                ORDER BY client_id, id
                """,
                (company_id,),
            )
        ],
        "activities": [
            dict(r)
            for r in conn.execute(
                """
                SELECT activity_id, client_id, activity_type, notes
                FROM activities
                WHERE company_id = ?
                ORDER BY activity_id
                """,
                (company_id,),
            )
        ],
        "activity_count": int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM activities WHERE company_id = ?",
                (company_id,),
            ).fetchone()["n"]
        ),
        "note_count": int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM legacy_notes WHERE company_id = ?",
                (company_id,),
            ).fetchone()["n"]
        ),
    }


def _cleanup(company_id: int | None) -> None:
    with get_connection() as conn:
        leftover = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        ids = [int(r["id"]) for r in leftover]
        if company_id is not None:
            ids.append(int(company_id))
        for cid in set(ids):
            conn.execute("DELETE FROM activities WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM legacy_notes WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM field_audit_log WHERE external_record_no LIKE ?", (f"{MARKER}-%",))
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?", (cid,)
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.commit()


def _frontend_helpers_no_carmeco_write_default() -> None:
    api = (REPO_ROOT / "frontend" / "src" / "api" / "carmeco.ts").read_text(encoding="utf-8")
    if "clientName = 'Carmeco'" in api:
        _fail("updateCompanyNotes still defaults clientName to Carmeco.")
    if "params.client ?? 'Carmeco'" in api:
        _fail("createCompanyNote still defaults client to Carmeco.")
    if "client = 'Carmeco'" in api:
        _fail("frontend API helper still defaults client to Carmeco.")
    wq = (REPO_ROOT / "frontend" / "src" / "WorkQueue.tsx").read_text(encoding="utf-8")
    if "|| 'Carmeco'" in wq:
        _fail("WorkQueue still falls back to Carmeco on a write.")
    helper = (REPO_ROOT / "frontend" / "src" / "writeClient.ts").read_text(encoding="utf-8")
    if "All My Clients cannot be used for writes" not in helper:
        _fail("writeClient.ts is missing the All My Clients write message.")


def main() -> int:
    company_id: int | None = None
    try:
        _frontend_helpers_no_carmeco_write_default()

        with get_connection() as conn:
            flora = conn.execute("SELECT id FROM contacts WHERE id = ?", (FLORA_ID,)).fetchone()
            whirl = conn.execute("SELECT id FROM companies WHERE id = ?", (WHIRLPOOL_ID,)).fetchone()
            if flora is None or whirl is None:
                _fail("Need Flora Jia and Whirlpool in the isolated test copy.")
            clients = {
                str(r["code"]): int(r["id"])
                for r in conn.execute("SELECT id, code FROM clients").fetchall()
            }
            carmeco_id = clients.get("carmeco")
            brown_id = clients.get("brown")
            if carmeco_id != CARMECO_ID or brown_id != BROWN_ID:
                _fail(f"Expected Carmeco=1 Brown=2, got {carmeco_id!r} {brown_id!r}.")
            stamp = str(int(time.time()))
            record_no = f"{MARKER}-{stamp}"
            cur = conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (record_no, f"{MARKER} Shared Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'New', '')
                """,
                (carmeco_id, company_id, record_no),
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'Existing Brown Status', '')
                """,
                (brown_id, company_id, f"{record_no}-B"),
            )
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, ?, 'Sales Rep Comments/Notes')
                """,
                (carmeco_id, company_id, f"{MARKER} carmeco original"),
            )
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, ?, 'Sales Rep Comments/Notes')
                """,
                (brown_id, company_id, f"{MARKER} brown original"),
            )
            conn.commit()
            before = _snapshot(conn, company_id)

        if company_id == WHIRLPOOL_ID:
            _fail("Refusing to use Whirlpool as test company.")

        notes_path = f"/api/companies/by-record/{record_no}/notes"
        status_path = f"/api/companies/by-record/{record_no}/status"
        missing_note = testdb.http_json(
            "PATCH",
            notes_path,
            {"note_text": f"{MARKER} should not save", "user": "Julie Magnani"},
        )
        _reject_ok(missing_note[0], missing_note[1], "notes missing client_id")

        zero_note = testdb.http_json(
            "PATCH",
            notes_path,
            {
                "client_id": 0,
                "note_text": f"{MARKER} should not save",
                "user": "Julie Magnani",
            },
        )
        _reject_ok(zero_note[0], zero_note[1], "notes client_id=0")

        missing_status = testdb.http_json(
            "PATCH",
            status_path,
            {"status": "Contacted", "user": "Julie Magnani"},
        )
        _reject_ok(missing_status[0], missing_status[1], "status missing client_id")

        zero_status = testdb.http_json(
            "PATCH",
            status_path,
            {"client_id": 0, "status": "Contacted", "user": "Julie Magnani"},
        )
        _reject_ok(zero_status[0], zero_status[1], "status client_id=0")

        missing_act = testdb.http_json(
            "POST",
            "/api/activities",
            {
                "external_record_no": record_no,
                "activity_type": "Note",
                "notes": f"{MARKER} should not save",
            },
        )
        _reject_ok(missing_act[0], missing_act[1], "activity missing client_id")

        zero_act = testdb.http_json(
            "POST",
            "/api/activities",
            {
                "client_id": 0,
                "external_record_no": record_no,
                "activity_type": "Note",
                "notes": f"{MARKER} should not save",
            },
        )
        _reject_ok(zero_act[0], zero_act[1], "activity client_id=0")

        bogus = 9_999_991
        with get_connection() as conn:
            if conn.execute("SELECT id FROM clients WHERE id = ?", (bogus,)).fetchone():
                _fail("Test sentinel client_id unexpectedly exists.")

        bogus_note = testdb.http_json(
            "PATCH",
            notes_path,
            {
                "client_id": bogus,
                "note_text": f"{MARKER} should not save",
                "user": "Julie Magnani",
            },
        )
        _reject_ok(bogus_note[0], bogus_note[1], "notes nonexistent client_id")

        bogus_act = testdb.http_json(
            "POST",
            "/api/activities",
            {
                "client_id": bogus,
                "external_record_no": record_no,
                "activity_type": "Note",
                "notes": f"{MARKER} should not save",
            },
        )
        _reject_ok(bogus_act[0], bogus_act[1], "activity nonexistent client_id")

        zero_mile = testdb.http_json(
            "POST",
            "/api/milestones",
            {
                "client_id": 0,
                "external_record_no": record_no,
                "milestone_type": "Quote",
                "milestone_date": "2026-08-25",
            },
        )
        _reject_ok(zero_mile[0], zero_mile[1], "milestone client_id=0")

        with get_connection() as conn:
            after_reject = _snapshot(conn, company_id)
        if after_reject != before:
            _fail("Rejected writes changed the isolated test database.")

        brown_note = testdb.http_json(
            "PATCH",
            notes_path,
            {
                "client_id": brown_id,
                "note_text": f"{MARKER} brown only",
                "user": "Julie Magnani",
            },
        )
        if brown_note[0] != 200:
            _fail(f"Brown notes write failed ({brown_note[0]}): {brown_note[1]}")

        carmeco_act = testdb.http_json(
            "POST",
            "/api/activities",
            {
                "client_id": carmeco_id,
                "external_record_no": record_no,
                "activity_type": "Note",
                "notes": f"{MARKER} carmeco only",
                "created_by": "Julie Magnani",
            },
        )
        if carmeco_act[0] not in {200, 201}:
            _fail(f"Carmeco activity write failed ({carmeco_act[0]}): {carmeco_act[1]}")
        if int(carmeco_act[1].get("client_id") or 0) != carmeco_id:
            _fail("Carmeco activity was not saved under Carmeco.")

        with get_connection() as conn:
            after_ok = _snapshot(conn, company_id)
            brown_notes = [
                r for r in after_ok["notes"] if int(r["client_id"]) == brown_id
            ]
            carmeco_notes = [
                r for r in after_ok["notes"] if int(r["client_id"]) == carmeco_id
            ]
            if not brown_notes or f"{MARKER} brown only" not in str(brown_notes[0].get("note_text")):
                _fail("Brown notes write did not update Brown.")
            if any(f"{MARKER} brown only" in str(r.get("note_text") or "") for r in carmeco_notes):
                _fail("Brown notes write changed Carmeco notes.")
            if f"{MARKER} carmeco original" not in str(carmeco_notes[0].get("note_text") if carmeco_notes else ""):
                _fail("Carmeco original notes were changed by a Brown write.")
            brown_acts = [
                r for r in after_ok["activities"] if int(r["client_id"]) == brown_id
            ]
            carmeco_acts = [
                r
                for r in after_ok["activities"]
                if int(r["client_id"]) == carmeco_id
                and f"{MARKER} carmeco only" in str(r.get("notes") or "")
            ]
            if brown_acts:
                _fail("Carmeco activity write also created a Brown activity.")
            if not carmeco_acts:
                _fail("Carmeco activity write did not create a Carmeco activity.")
            brown_ccr = [
                r for r in after_ok["ccr"] if int(r["client_id"]) == brown_id
            ]
            carmeco_ccr = [
                r for r in after_ok["ccr"] if int(r["client_id"]) == carmeco_id
            ]
            brown_ccr_before = [r for r in before["ccr"] if int(r["client_id"]) == brown_id]
            carmeco_ccr_before = [
                r for r in before["ccr"] if int(r["client_id"]) == carmeco_id
            ]
            if brown_ccr != brown_ccr_before:
                _fail("Brown relationship changed during Carmeco activity write.")
            if carmeco_ccr != carmeco_ccr_before:
                _fail("Carmeco relationship changed during note/activity isolation writes.")

        print("PASS: write requires explicit client_id; Brown and Carmeco stay isolated.")
        return 0
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        _cleanup(company_id)


if __name__ == "__main__":
    sys.exit(main())
