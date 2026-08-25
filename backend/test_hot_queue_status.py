"""Hot Work Queue uses current client status only.

Run: python test_hot_queue_status.py

Read-only checks against Flora Jia / Whirlpool (Carmeco) and Brown.
Creates and deletes a temporary NSAPPT company for the positive Hot Prospect case.
Does not change Flora Jia or other production identity/status values.
"""

from __future__ import annotations

import testdb
import sys
import time

from access import get_default_user
from appointments_data import (
    ensure_appointments_schema,
    is_excluded_from_hot_queue,
    is_hot_prospect_status,
    list_appointments,
    count_hot_prospects,
)
from client_workspace_data import get_contact_workspace, list_prospects
from db import get_connection
from work_queue_data import list_work_queue

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSHOT"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _cleanup(company_id: int | None) -> None:
    if company_id is None:
        return
    with get_connection() as conn:
        conn.execute("DELETE FROM appointments WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
        conn.execute(
            "DELETE FROM client_company_relationships WHERE company_id = ?",
            (company_id,),
        )
        conn.execute(
            "DELETE FROM companies WHERE id = ? AND company_name LIKE ?",
            (company_id, f"{MARKER}%"),
        )
        conn.commit()


def _snapshot_flora(conn) -> dict:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, email
        FROM contacts
        WHERE id = ?
        """,
        (FLORA_ID,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _snapshot_ccrs(conn, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, is_hot, next_action, assigned_user_id
            FROM client_company_relationships
            WHERE company_id = ?
            ORDER BY client_id, id
            """,
            (company_id,),
        ).fetchall()
    ]


def main() -> int:
    ensure_appointments_schema()
    user = get_default_user()
    if user is None:
        _fail("Default user not found.")

    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        if not flora_before:
            _fail("Flora Jia contact not found.")
        whirlpool_ccrs_before = _snapshot_ccrs(conn, WHIRLPOOL_ID)
        clients = {
            str(r["code"]): int(r["id"])
            for r in conn.execute("SELECT id, code FROM clients").fetchall()
        }
        carmeco_id = clients.get("carmeco")
        brown_id = clients.get("brown")
        if not carmeco_id or not brown_id:
            _fail("Need both carmeco and brown clients.")
        brown_ccr_count = conn.execute(
            "SELECT COUNT(*) AS n FROM client_company_relationships WHERE client_id = ?",
            (brown_id,),
        ).fetchone()["n"]

    excluded = (
        "Appointment Set",
        "Appt Set Email Marketing",
        "Closed",
        "Do Not Call",
        "Disqualified - Fit",
        "Current Customer",
        "Competitor",
    )
    for status in excluded:
        if is_hot_prospect_status(status):
            _fail(f"{status!r} must not classify as Hot.")
        if not is_excluded_from_hot_queue(status):
            _fail(f"{status!r} must be excluded from Hot.")
    if not is_hot_prospect_status("Hot Prospect"):
        _fail("Hot Prospect must classify as Hot.")
    if is_excluded_from_hot_queue("Hot Prospect"):
        _fail("Hot Prospect must not be in the exclusion list.")

    carmeco_hot = list_work_queue(user.id, client_id=carmeco_id, work_type="hot")
    carmeco_hot_sql = count_hot_prospects([carmeco_id])
    if carmeco_hot.summary.hot != carmeco_hot_sql:
        _fail(
            f"Carmeco Work Queue Hot {carmeco_hot.summary.hot} != status count {carmeco_hot_sql}."
        )
    carmeco_prospect_hot = sum(1 for p in list_prospects(client_id=carmeco_id) if p.is_hot)
    if carmeco_prospect_hot != carmeco_hot_sql:
        _fail("Carmeco prospect Hot flag count diverges from Hot Prospect status.")
    brown_hot = list_work_queue(user.id, client_id=brown_id, work_type="hot")
    brown_hot_sql = count_hot_prospects([brown_id])
    if brown_hot.summary.hot != brown_hot_sql:
        _fail(f"Brown Work Queue Hot {brown_hot.summary.hot} != status count {brown_hot_sql}.")
    brown_prospect_hot = sum(1 for p in list_prospects(client_id=brown_id) if p.is_hot)
    if brown_prospect_hot != brown_hot_sql:
        _fail("Brown prospect Hot flag count diverges from Hot Prospect status.")
    if carmeco_hot.summary.hot != 0:
        _fail(f"Carmeco Hot count should be 0, got {carmeco_hot.summary.hot}.")
    if carmeco_hot.count != 0:
        _fail(f"Carmeco Hot list should be empty, got {carmeco_hot.count} items.")
    carmeco_hot_flag = list_work_queue(user.id, client_id=carmeco_id, hot=True)
    if any(
        item.company_id == WHIRLPOOL_ID or item.contact_id == FLORA_ID
        for item in carmeco_hot_flag.items
    ):
        _fail("Flora Jia / Whirlpool appeared in the Carmeco Hot boolean filter.")
    for item in carmeco_hot.items:
        if item.company_id == WHIRLPOOL_ID or item.contact_id == FLORA_ID:
            _fail("Flora Jia / Whirlpool appeared in Carmeco Hot.")
        if not is_hot_prospect_status(item.status):
            _fail(f"Hot list included non-Hot-Prospect status {item.status!r}.")

    carmeco_all = list_work_queue(user.id, client_id=carmeco_id)
    for item in carmeco_all.items:
        if item.company_id == WHIRLPOOL_ID and item.work_type == "Hot":
            _fail("Whirlpool still has Work Type = Hot while not Hot Prospect.")

    cancelled = list_appointments(carmeco_id, bucket="cancelled")
    flora_history = [
        item
        for item in cancelled.items
        if item.contact_id == FLORA_ID or item.company_id == WHIRLPOOL_ID
    ]
    appt_or_history = list_work_queue(
        user.id, client_id=carmeco_id, work_type="appointment"
    )
    flora_appts = [
        item
        for item in appt_or_history.items
        if item.company_id == WHIRLPOOL_ID or item.contact_id == FLORA_ID
    ]
    ws = get_contact_workspace(FLORA_ID, client_id=carmeco_id)
    if _blank(ws.status) != "Appointment Set":
        _fail(
            f"Flora Carmeco status should remain Appointment Set, got {ws.status!r}."
        )
    if not flora_history and not flora_appts and not (ws.timeline or ws.company_timeline):
        _fail("Flora disappeared from Appointments/history.")
    if not flora_appts:
        _fail("Flora/Whirlpool should remain visible under Appointments work type.")

    company_id: int | None = None
    try:
        with get_connection() as conn:
            stamp = str(int(time.time()))
            cur = conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (f"{MARKER}-{stamp}", f"{MARKER} Hot Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action, is_hot
                ) VALUES (?, ?, ?, 'Hot Prospect', 'Call', 1)
                """,
                (carmeco_id, company_id, f"{MARKER}-{stamp}"),
            )
            conn.commit()

        hot_now = list_work_queue(user.id, client_id=carmeco_id, work_type="hot")
        if not any(item.company_id == company_id for item in hot_now.items):
            _fail("Temporary Hot Prospect was missing from the Hot list.")

        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET status = 'Appointment Set'
                WHERE company_id = ? AND client_id = ?
                """,
                (company_id, carmeco_id),
            )
            conn.commit()

        after = list_work_queue(user.id, client_id=carmeco_id, work_type="hot")
        if any(item.company_id == company_id for item in after.items):
            _fail("Appointment Set temp company remained in Hot.")
        with get_connection() as conn:
            sticky = conn.execute(
                """
                SELECT status, COALESCE(is_hot, 0) AS is_hot
                FROM client_company_relationships
                WHERE company_id = ? AND client_id = ?
                """,
                (company_id, carmeco_id),
            ).fetchone()
            if sticky is None:
                _fail("Temp Carmeco relationship missing after status change.")
            if _blank(sticky["status"]) != "Appointment Set":
                _fail("Temp status was not Appointment Set.")
            if int(sticky["is_hot"] or 0) != 1:
                _fail("Expected leftover is_hot=1 to prove the flag is ignored.")
        appt_q = list_work_queue(user.id, client_id=carmeco_id, work_type="appointment")
        if not any(item.company_id == company_id for item in appt_q.items):
            _fail("Appointment Set temp company missing from Appointments work type.")
    finally:
        _cleanup(company_id)

    with get_connection() as conn:
        if _snapshot_flora(conn) != flora_before:
            _fail("Flora Jia identity values changed.")
        if _snapshot_ccrs(conn, WHIRLPOOL_ID) != whirlpool_ccrs_before:
            _fail("Whirlpool/Brown production relationships changed.")
        brown_after = conn.execute(
            "SELECT COUNT(*) AS n FROM client_company_relationships WHERE client_id = ?",
            (brown_id,),
        ).fetchone()["n"]
        if brown_after != brown_ccr_count:
            _fail("Brown Industries relationship count changed.")
        brown_whirlpool = conn.execute(
            """
            SELECT status, is_hot FROM client_company_relationships
            WHERE company_id = ? AND client_id = ?
            """,
            (WHIRLPOOL_ID, brown_id),
        ).fetchone()
        if brown_whirlpool is None:
            _fail("Brown Whirlpool relationship missing.")
        if _blank(brown_whirlpool["status"]) != "Left Message":
            _fail("Brown Industries Whirlpool status changed.")

    print("test_hot_queue_status: ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"test_hot_queue_status: FAIL: {exc}", file=sys.stderr)
        raise
