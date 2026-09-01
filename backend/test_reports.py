"""Reports module: client isolation, filters, campaign attribution, pagination, CSV.

Run: python test_reports.py

Creates and deletes temporary company/contact/CCR/campaign/activity rows.
Does not change Flora Jia, Whirlpool, or other production identity values.
Uses the isolated testdb copy — never writes through live :8007.
"""

from __future__ import annotations

import testdb
import sys
from datetime import date, datetime, timedelta

from activities_data import insert_activity_row
from appointments_data import ensure_appointments_schema
from campaigns_data import (
    add_campaign_company,
    add_campaign_contact,
    create_operational_campaign,
    ensure_campaigns_schema,
)
from access import DEFAULT_USER_EMAIL
from db import get_connection
from models import CampaignCreateRequest, CampaignMemberAddRequest

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSREP"
CARMECO_ID = 1
BROWN_ID = 2


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _snapshot_flora(conn) -> dict:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, email
        FROM contacts
        WHERE id = ? OR (first_name = 'Flora' AND last_name = 'Jia')
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, id
        LIMIT 1
        """,
        (FLORA_ID, FLORA_ID),
    ).fetchone()
    return dict(row) if row is not None else {}


def _snapshot_whirlpool(conn) -> dict:
    row = conn.execute(
        "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
        (WHIRLPOOL_ID,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _ccr_snapshot(conn, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
            FROM client_company_relationships
            WHERE company_id = ?
            ORDER BY client_id, id
            """,
            (company_id,),
        ).fetchall()
    ]


def _client_row(payload: dict, client_id: int) -> dict | None:
    for item in payload.get("items") or []:
        if int(item.get("client_id") or 0) == client_id:
            return item
    return None


def _n(row: dict | None, key: str) -> int:
    if not row:
        return 0
    return int(row.get(key) or 0)


def _kpi(payload: dict, key: str):
    for item in payload.get("kpis") or []:
        if item.get("key") == key:
            return item.get("value")
    return None


def _assert_drill_matches(
    records: dict,
    expected_total: int,
    *,
    client_id: int | None = None,
    record_type: str | None = None,
    label: str,
) -> None:
    total = int(records.get("total") or 0)
    items = records.get("items") or []
    if total != expected_total:
        _fail(f"{label}: drill-down total {total} must equal displayed count {expected_total}.")
    if len(items) != min(total, 50):
        _fail(
            f"{label}: page should show {min(total, 50)} supporting rows, got {len(items)} "
            f"(total {total})."
        )
    if client_id is not None:
        leaked = [item for item in items if int(item.get("client_id") or 0) != client_id]
        if leaked:
            _fail(f"{label}: drill-down included a record for another client.")
    if record_type is not None:
        wrong = [
            item
            for item in items
            if _blank(item.get("record_type")).lower() != record_type.lower()
        ]
        if wrong:
            _fail(
                f"{label}: expected record type {record_type!r}, "
                f"got {_blank(wrong[0].get('record_type'))!r}."
            )


UTF8_BOM = b"\xef\xbb\xbf"


def _assert_excel_csv(
    resp,
    *,
    title_kind: str,
    columns: list[str],
    date_from: str,
    date_to: str,
    must_contain: str | None = None,
    must_not_contain: str | None = None,
    label: str,
) -> str:
    if resp.status_code != 200:
        _fail(f"{label} CSV failed: {resp.status_code} {resp.text[:400]}")
    ctype = resp.headers.get("content-type", "")
    if "text/csv" not in ctype:
        _fail(f"{label} content-type should be text/csv, got {ctype}")
    raw = resp.content
    if not raw.startswith(UTF8_BOM):
        _fail(f"{label} must start with a UTF-8 BOM so Excel displays the em dash.")
    text = raw.decode("utf-8-sig")
    if "â€”" in text:
        _fail(f"{label} still contains a mojibake em dash.")
    if f"# NorthStar Reports — {title_kind}" not in text:
        _fail(f"{label} title line with em dash is missing.")
    if f"# Date range: {date_from} to {date_to}" not in text:
        _fail(f"{label} must keep the date-range filter summary.")
    if "# Active Client:" not in text:
        _fail(f"{label} must keep the Active Client filter summary.")
    header = ""
    for line in text.splitlines():
        if line and not line.startswith("#"):
            header = line
            break
    for col in columns:
        if col not in header:
            _fail(f"{label} is missing readable heading {col!r} in {header!r}.")
    for internal in (
        "client_name",
        "follow_ups_scheduled",
        "call_to_appointment_pct",
        "occurred_at",
        "user_name",
        "hot_prospects",
        "assigned_clients",
    ):
        if internal in header:
            _fail(f"{label} still uses internal field name {internal!r}.")
    if must_contain and must_contain not in text:
        _fail(f"{label} should include {must_contain!r}.")
    if must_not_contain and must_not_contain in text:
        _fail(f"{label} included {must_not_contain!r}.")
    return text


def _cleanup(
    *,
    company_ids: list[int],
    contact_ids: list[int],
    activity_ids: list[int],
    campaign_ids: list[int],
    appointment_ids: list[int],
    user_ids: list[int],
) -> None:
    with get_connection() as conn:
        for aid in activity_ids:
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
        for appt_id in appointment_ids:
            conn.execute("DELETE FROM appointments WHERE id = ?", (appt_id,))
        if campaign_ids:
            ph = ",".join("?" * len(campaign_ids))
            conn.execute(f"DELETE FROM campaign_companies WHERE campaign_id IN ({ph})", campaign_ids)
            conn.execute(f"DELETE FROM campaign_contacts WHERE campaign_id IN ({ph})", campaign_ids)
            conn.execute(
                f"DELETE FROM client_campaigns WHERE id IN ({ph}) AND campaign_name LIKE ?",
                [*campaign_ids, f"{MARKER}%"],
            )
        conn.execute("DELETE FROM client_campaigns WHERE campaign_name LIKE ?", (f"{MARKER}%",))
        for contact_id in contact_ids:
            conn.execute("DELETE FROM contact_client_workflows WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM work_queue_items WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        for company_id in company_ids:
            conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM appointments WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM campaign_companies WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM campaign_contacts WHERE company_id = ?", (company_id,))
            conn.execute(
                "DELETE FROM opportunity_assignments WHERE company_id = ?",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (company_id,),
            )
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        leftover = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        for row in leftover:
            cid = int(row["id"])
            conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM activities WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM appointments WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM campaign_companies WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM campaign_contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM opportunity_assignments WHERE company_id = ?", (cid,))
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (cid,),
            )
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (cid,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        for uid in user_ids:
            conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (uid,))
            conn.execute(
                "DELETE FROM users WHERE id = ? AND email LIKE ?",
                (uid, f"{MARKER.lower()}%@test.invalid"),
            )
        conn.commit()


def main() -> None:
    stamp = datetime.now().strftime("%H%M%S%f")
    record_no = f"{MARKER}-{stamp}"
    company_ids: list[int] = []
    contact_ids: list[int] = []
    activity_ids: list[int] = []
    campaign_ids: list[int] = []
    appointment_ids: list[int] = []
    user_ids: list[int] = []
    now = datetime.now().replace(microsecond=0)
    membership_at = now - timedelta(days=5)
    before_at = membership_at - timedelta(days=2)
    after_at = membership_at + timedelta(days=1)
    out_of_range = now - timedelta(days=40)
    date_from = (now - timedelta(days=20)).date().isoformat()
    date_to = now.date().isoformat()
    overdue_date = (date.today() - timedelta(days=3)).isoformat()
    qs = f"date_from={date_from}&date_to={date_to}"

    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        whirlpool_before = _snapshot_whirlpool(conn)
        julie = conn.execute(
            "SELECT id FROM users WHERE lower(email) = lower(?)",
            (DEFAULT_USER_EMAIL,),
        ).fetchone()
        if julie is None:
            _fail("Julie Magnani user not found.")
        julie_id = int(julie["id"])
        cur = conn.execute(
            """
            INSERT INTO users (email, full_name, is_administrator, is_internal_northstar, active)
            VALUES (?, ?, 0, 1, 1)
            """,
            (f"{MARKER.lower()}-{stamp}@test.invalid", f"{MARKER} Rep {stamp}"),
        )
        other_id = int(cur.lastrowid)
        user_ids.append(other_id)
        conn.execute(
            """
            INSERT INTO user_client_assignments (user_id, client_id, role, active)
            VALUES (?, ?, 'revenue_development_specialist', 1)
            """,
            (other_id, CARMECO_ID),
        )
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, city, state)
            VALUES (?, ?, 'Testville', 'OH')
            """,
            (record_no, f"{MARKER} Co {stamp}"),
        )
        company_id = int(cur.lastrowid)
        company_ids.append(company_id)
        if company_id == WHIRLPOOL_ID:
            _fail("Refusing to reuse Whirlpool as the test company.")
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action, assigned_user_id
            ) VALUES (?, ?, ?, 'Existing Brown Status', '', NULL)
            """,
            (BROWN_ID, company_id, record_no),
        )
        cur = conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action, assigned_user_id
            ) VALUES (?, ?, ?, 'Working', 'Call', ?)
            """,
            (CARMECO_ID, company_id, f"{record_no}-C", julie_id),
        )
        carmeco_rel = int(cur.lastrowid)
        brown_rel = int(
            conn.execute(
                """
                SELECT id FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (BROWN_ID, company_id),
            ).fetchone()["id"]
        )
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title, source_row_index
            ) VALUES (?, ?, 'RepTmp', 'Primary', 'Buyer', 1)
            """,
            (company_id, record_no),
        )
        contact_id = int(cur.lastrowid)
        contact_ids.append(contact_id)
        if contact_id == FLORA_ID:
            _fail("Refusing to use Flora Jia as the test contact.")
        brown_before = _ccr_snapshot(conn, company_id)
        conn.commit()

    try:
        ensure_campaigns_schema()
        ensure_appointments_schema()
        base_carmeco_code, base_carmeco = testdb.http_json(
            "GET", f"/api/reports/client-results?client_id={CARMECO_ID}&{qs}&limit=50&offset=0"
        )
        if base_carmeco_code != 200:
            _fail(f"baseline Carmeco client-results failed: {base_carmeco_code} {base_carmeco}")
        base_brown_code, base_brown = testdb.http_json(
            "GET", f"/api/reports/client-results?client_id={BROWN_ID}&{qs}"
        )
        if base_brown_code != 200:
            _fail(f"baseline Brown client-results failed: {base_brown_code} {base_brown}")
        base_julie_code, base_julie = testdb.http_json(
            "GET",
            f"/api/reports/client-results?client_id={CARMECO_ID}&{qs}&user_id={julie_id}",
        )
        if base_julie_code != 200:
            _fail(f"baseline Julie filter failed: {base_julie_code} {base_julie}")
        base_team_code, base_team = testdb.http_json(
            "GET",
            f"/api/reports/team-performance?client_id={CARMECO_ID}&{qs}&user_id={julie_id}",
        )
        if base_team_code != 200:
            _fail(f"baseline team-performance failed: {base_team_code} {base_team}")
        base_carmeco_row = _client_row(base_carmeco, CARMECO_ID) or {}
        base_brown_row = _client_row(base_brown, BROWN_ID) or {}
        base_julie_row = _client_row(base_julie, CARMECO_ID) or {}
        base_team_row = (base_team.get("items") or [{}])[0]

        with get_connection() as conn:
            conn.execute(
                "UPDATE client_company_relationships SET status = 'Hot Prospect' WHERE id = ?",
                (carmeco_rel,),
            )
            activity_ids.append(
                insert_activity_row(
                    conn,
                    client_id=CARMECO_ID,
                    company_id=company_id,
                    relationship_id=carmeco_rel,
                    external_record_no=f"{record_no}-C",
                    user_id=julie_id,
                    contact_id=contact_id,
                    activity_type="Call",
                    outcome="Connected",
                    notes=f"{MARKER} carmeco in-range call",
                    assigned_user="Julie Magnani",
                    created_by="Julie Magnani",
                    activity_at=after_at.isoformat(sep=" "),
                )
            )
            activity_ids.append(
                insert_activity_row(
                    conn,
                    client_id=CARMECO_ID,
                    company_id=company_id,
                    relationship_id=carmeco_rel,
                    external_record_no=f"{record_no}-C",
                    user_id=julie_id,
                    contact_id=contact_id,
                    activity_type="Call",
                    notes=f"{MARKER} carmeco out-of-range call",
                    assigned_user="Julie Magnani",
                    created_by="Julie Magnani",
                    activity_at=out_of_range.isoformat(sep=" "),
                )
            )
            activity_ids.append(
                insert_activity_row(
                    conn,
                    client_id=CARMECO_ID,
                    company_id=company_id,
                    relationship_id=carmeco_rel,
                    external_record_no=f"{record_no}-C",
                    user_id=other_id,
                    contact_id=contact_id,
                    activity_type="Call",
                    notes=f"{MARKER} other-rep call",
                    assigned_user=f"{MARKER} Rep {stamp}",
                    created_by=f"{MARKER} Rep {stamp}",
                    activity_at=after_at.isoformat(sep=" "),
                )
            )
            activity_ids.append(
                insert_activity_row(
                    conn,
                    client_id=BROWN_ID,
                    company_id=company_id,
                    relationship_id=brown_rel,
                    external_record_no=record_no,
                    user_id=julie_id,
                    contact_id=contact_id,
                    activity_type="Call",
                    notes=f"{MARKER} brown in-range call",
                    assigned_user="Julie Magnani",
                    created_by="Julie Magnani",
                    activity_at=after_at.isoformat(sep=" "),
                )
            )
            fu_id = insert_activity_row(
                conn,
                client_id=CARMECO_ID,
                company_id=company_id,
                relationship_id=carmeco_rel,
                external_record_no=f"{record_no}-C",
                user_id=julie_id,
                contact_id=contact_id,
                activity_type="Follow-Up",
                notes=f"{MARKER} follow-up completed task",
                assigned_user="Julie Magnani",
                created_by="Julie Magnani",
                activity_at=after_at.isoformat(sep=" "),
                follow_up_at=after_at.isoformat(sep=" "),
            )
            activity_ids.append(fu_id)
            conn.execute(
                """
                UPDATE activities
                SET follow_up_completed = 1, completion_status = 'completed', updated_at = ?
                WHERE activity_id = ?
                """,
                (after_at.isoformat(sep=" "), fu_id),
            )
            activity_ids.append(
                insert_activity_row(
                    conn,
                    client_id=CARMECO_ID,
                    company_id=company_id,
                    relationship_id=carmeco_rel,
                    external_record_no=f"{record_no}-C",
                    user_id=julie_id,
                    contact_id=contact_id,
                    activity_type="Note",
                    outcome="Follow-up completed",
                    notes=f"{MARKER} companion completion note",
                    assigned_user="Julie Magnani",
                    created_by="Julie Magnani",
                    activity_at=after_at.isoformat(sep=" "),
                )
            )
            activity_ids.append(
                insert_activity_row(
                    conn,
                    client_id=CARMECO_ID,
                    company_id=company_id,
                    relationship_id=carmeco_rel,
                    external_record_no=f"{record_no}-C",
                    user_id=julie_id,
                    contact_id=contact_id,
                    activity_type="Call",
                    notes=f"{MARKER} call before campaign membership",
                    assigned_user="Julie Magnani",
                    created_by="Julie Magnani",
                    activity_at=before_at.isoformat(sep=" "),
                )
            )
            oa_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_assignments'"
            ).fetchone()
            if oa_exists:
                oa_cols = {
                    str(r[1]) for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
                }
                if "target_campaign_id" in oa_cols:
                    conn.execute(
                        """
                        INSERT INTO opportunity_assignments (
                            target_client_id, company_id, relationship_id,
                            originated_from, opportunity_score, source_summary,
                            created_by, created_at, target_campaign_id
                        ) VALUES (?, ?, ?, 'Cross-Client Opportunity', 0, ?, 'NSREP', ?, NULL)
                        """,
                        (CARMECO_ID, company_id, carmeco_rel, f"{MARKER} oa", after_at.isoformat(sep=" ")),
                    )
            ensure_appointments_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO appointments (
                    client_id, company_id, relationship_id, contact_id,
                    appointment_date, start_time, timezone, appointment_type,
                    revenue_specialist_user_id, notes, source, status,
                    created_by, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, '10:00', 'America/Chicago', 'Phone',
                          ?, ?, 'Phone Call', 'completed', 'Julie Magnani', ?, ?, ?)
                """,
                (
                    CARMECO_ID,
                    company_id,
                    carmeco_rel,
                    contact_id,
                    after_at.date().isoformat(),
                    julie_id,
                    f"{MARKER} completed appt",
                    after_at.isoformat(sep=" "),
                    after_at.isoformat(sep=" "),
                    after_at.isoformat(sep=" "),
                ),
            )
            appointment_ids.append(int(cur.lastrowid))
            cur = conn.execute(
                """
                INSERT INTO appointments (
                    client_id, company_id, relationship_id, contact_id,
                    appointment_date, start_time, timezone, appointment_type,
                    revenue_specialist_user_id, notes, source, status,
                    created_by, created_at, updated_at, cancelled_at
                ) VALUES (?, ?, ?, ?, ?, '11:00', 'America/Chicago', 'Phone',
                          ?, ?, 'Phone Call', 'cancelled', 'Julie Magnani', ?, ?, ?)
                """,
                (
                    CARMECO_ID,
                    company_id,
                    carmeco_rel,
                    contact_id,
                    after_at.date().isoformat(),
                    julie_id,
                    f"{MARKER} cancelled appt",
                    after_at.isoformat(sep=" "),
                    after_at.isoformat(sep=" "),
                    after_at.isoformat(sep=" "),
                ),
            )
            appointment_ids.append(int(cur.lastrowid))
            conn.execute(
                """
                INSERT INTO work_queue_items (
                    client_id, company_id, relationship_id, contact_id, action_type,
                    due_date, due_time, assigned_user_id, assigned_user, completion_status,
                    source, notes, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'Follow-Up', ?, '09:00', ?, 'Julie Magnani', 'open',
                          'test', ?, 'Julie Magnani', datetime('now'), datetime('now'))
                """,
                (
                    CARMECO_ID,
                    company_id,
                    carmeco_rel,
                    contact_id,
                    overdue_date,
                    julie_id,
                    f"{MARKER} overdue",
                ),
            )
            conn.commit()

        camp = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Stamping {stamp}",
                description="Reports attribution tests",
                category="Stamping",
                status="Active",
            )
        )
        extra_a = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Extra A {stamp}",
                status="Active",
            )
        )
        extra_b = create_operational_campaign(
            CampaignCreateRequest(
                client_id=CARMECO_ID,
                campaign_name=f"{MARKER} Extra B {stamp}",
                status="Active",
            )
        )
        campaign_ids = [camp.campaign_id, extra_a.campaign_id, extra_b.campaign_id]
        add_campaign_company(
            camp.campaign_id,
            CampaignMemberAddRequest(company_id=company_id, notes=f"{MARKER} ccr"),
        )
        add_campaign_contact(
            camp.campaign_id,
            CampaignMemberAddRequest(contact_id=contact_id, notes=f"{MARKER} cct"),
        )
        with get_connection() as conn:
            conn.execute(
                "UPDATE campaign_companies SET created_at = ? WHERE campaign_id = ? AND company_id = ?",
                (membership_at.isoformat(sep=" "), camp.campaign_id, company_id),
            )
            conn.execute(
                "UPDATE campaign_contacts SET created_at = ? WHERE campaign_id = ? AND contact_id = ?",
                (membership_at.isoformat(sep=" "), camp.campaign_id, contact_id),
            )
            conn.commit()

        code, filters = testdb.http_json(
            "GET", f"/api/reports/filters?client_id={CARMECO_ID}"
        )
        if code != 200:
            _fail(f"filters failed: {code} {filters}")
        if date.today().replace(day=1).isoformat() != filters.get("date_from"):
            _fail(f"filters should default date_from to current month start, got {filters.get('date_from')}")
        if not any(int(r.get("id") or 0) == julie_id for r in filters.get("reps") or []):
            _fail("Julie Magnani missing from report rep filters.")
        if not any(int(c.get("id") or 0) == camp.campaign_id for c in filters.get("campaigns") or []):
            _fail("Temp campaign missing from report campaign filters.")

        code, carmeco = testdb.http_json(
            "GET", f"/api/reports/client-results?client_id={CARMECO_ID}&{qs}&limit=50&offset=0"
        )
        if code != 200:
            _fail(f"client-results Carmeco failed: {code} {carmeco}")
        row = _client_row(carmeco, CARMECO_ID)
        if row is None:
            _fail("Carmeco missing from client results.")
        if _client_row(carmeco, BROWN_ID) is not None:
            _fail("Brown leaked into Carmeco client results.")
        calls_delta = _n(row, "calls") - _n(base_carmeco_row, "calls")
        fu_done_delta = _n(row, "follow_ups_completed") - _n(base_carmeco_row, "follow_ups_completed")
        fu_sched_delta = _n(row, "follow_ups_scheduled") - _n(base_carmeco_row, "follow_ups_scheduled")
        set_delta = _n(row, "appointments_set") - _n(base_carmeco_row, "appointments_set")
        done_delta = _n(row, "appointments_completed") - _n(base_carmeco_row, "appointments_completed")
        cancel_delta = _n(row, "appointments_cancelled") - _n(base_carmeco_row, "appointments_cancelled")
        hot_delta = _n(row, "hot_prospects") - _n(base_carmeco_row, "hot_prospects")
        opp_delta = _n(row, "opportunities") - _n(base_carmeco_row, "opportunities")
        if calls_delta != 3:
            _fail(
                "Carmeco should add Julie in-range, other-rep, and pre-membership calls "
                f"(+3). Got delta {calls_delta}."
            )
        if fu_done_delta != 1:
            _fail(
                "Follow-ups completed must add the completed Follow-Up row once, "
                f"not the companion note. Got delta {fu_done_delta}."
            )
        if fu_sched_delta < 1:
            _fail("Follow-ups scheduled should include the Follow-Up task.")
        if set_delta != 2:
            _fail("Completed and cancelled appointments must remain in appointments set.")
        if done_delta != 1:
            _fail("Completed appointment missing from historical reporting.")
        if cancel_delta != 1:
            _fail("Cancelled appointment missing from historical reporting.")
        if hot_delta != 1:
            _fail("Current Hot Prospect status was not counted for the temp Carmeco relationship.")
        if opp_delta != 1:
            _fail("Opportunity identified this period was not counted.")
        if int(_kpi(carmeco, "calls") or 0) != int(row["calls"]):
            _fail("Client Results KPI totals must match the table counts.")

        code, brown = testdb.http_json(
            "GET", f"/api/reports/client-results?client_id={BROWN_ID}&{qs}"
        )
        if code != 200:
            _fail(f"client-results Brown failed: {code} {brown}")
        brown_row = _client_row(brown, BROWN_ID)
        if brown_row is None:
            _fail("Brown missing from Brown client results.")
        if _client_row(brown, CARMECO_ID) is not None:
            _fail("Carmeco leaked into Brown client results.")
        brown_calls_delta = _n(brown_row, "calls") - _n(base_brown_row, "calls")
        brown_hot_delta = _n(brown_row, "hot_prospects") - _n(base_brown_row, "hot_prospects")
        if brown_calls_delta != 1:
            _fail(f"Brown should add exactly its in-range call, got delta {brown_calls_delta}.")
        if brown_hot_delta != 0:
            _fail("Shared-company Hot Prospect must stay on the Carmeco relationship, not Brown.")

        code, all_clients = testdb.http_json(
            "GET", f"/api/reports/client-results?client_id=0&{qs}&sort_by=client_name&sort_dir=asc"
        )
        if code != 200:
            _fail(f"All My Clients client-results failed: {code} {all_clients}")
        all_carmeco = _client_row(all_clients, CARMECO_ID)
        all_brown = _client_row(all_clients, BROWN_ID)
        if all_carmeco is None or all_brown is None:
            _fail("All My Clients must include authorized Carmeco and Brown rows.")
        combined_calls = int(all_carmeco["calls"]) + int(all_brown["calls"])
        if int(_kpi(all_clients, "calls") or 0) != combined_calls:
            _fail("All My Clients totals must sum per-client rows, not collapse shared companies.")

        code, paged = testdb.http_json(
            "GET",
            f"/api/reports/client-results?client_id=0&{qs}&sort_by=client_name&sort_dir=asc&limit=1&offset=0",
        )
        if code != 200:
            _fail(f"pagination page 1 failed: {code} {paged}")
        if int(paged.get("total") or 0) < 2:
            _fail("All My Clients pagination total should include at least two clients.")
        if len(paged.get("items") or []) != 1:
            _fail("limit=1 should return one client results row.")
        first_id = int(paged["items"][0]["client_id"])
        code, paged2 = testdb.http_json(
            "GET",
            f"/api/reports/client-results?client_id=0&{qs}&sort_by=client_name&sort_dir=asc&limit=1&offset=1",
        )
        if code != 200:
            _fail(f"pagination page 2 failed: {code} {paged2}")
        second_id = int((paged2.get("items") or [{}])[0].get("client_id") or 0)
        if second_id == first_id:
            _fail("Server-side pagination returned the same client on offset=1.")

        code, julie_only = testdb.http_json(
            "GET",
            f"/api/reports/client-results?client_id={CARMECO_ID}&{qs}&user_id={julie_id}",
        )
        if code != 200:
            _fail(f"rep filter failed: {code} {julie_only}")
        julie_row = _client_row(julie_only, CARMECO_ID)
        if julie_row is None:
            _fail("Carmeco missing after rep filter.")
        julie_calls_delta = _n(julie_row, "calls") - _n(base_julie_row, "calls")
        if julie_calls_delta != 2:
            _fail(
                "Julie filter should add her two in-range Carmeco calls and exclude the other rep. "
                f"Got delta {julie_calls_delta}."
            )
        if julie_calls_delta >= calls_delta:
            _fail("Rep filter should exclude the other specialist's Carmeco call.")

        code, camp_rep = testdb.http_json(
            "GET",
            f"/api/reports/campaign-performance?client_id={CARMECO_ID}&{qs}&campaign_id={camp.campaign_id}",
        )
        if code != 200:
            _fail(f"campaign-performance failed: {code} {camp_rep}")
        camp_items = camp_rep.get("items") or []
        if len(camp_items) != 1 or int(camp_items[0]["campaign_id"]) != camp.campaign_id:
            _fail("Campaign filter should return only the selected campaign.")
        camp_calls = int(camp_items[0]["calls"])
        if camp_calls != 2:
            _fail(
                "Campaign should credit the two calls after membership began and not the earlier one. "
                f"Got {camp_calls}."
            )
        if int(camp_items[0]["companies"]) < 1 or int(camp_items[0]["contacts"]) < 1:
            _fail("Campaign membership companies/contacts were not counted.")

        code, records = testdb.http_json(
            "GET",
            f"/api/reports/records?section=client-results&metric=calls&client_id={CARMECO_ID}"
            f"&{qs}&row_client_id={CARMECO_ID}&limit=50&offset=0",
        )
        if code != 200:
            _fail(f"drill-down failed: {code} {records}")
        _assert_drill_matches(
            records,
            int(row["calls"]),
            client_id=CARMECO_ID,
            record_type="Call",
            label="Carmeco Client Results calls",
        )
        notes = [_blank(item.get("notes")) for item in records.get("items") or []]
        if not any(f"{MARKER} carmeco in-range call" == n for n in notes):
            _fail("Carmeco call drill-down should include the in-range test call.")
        if any(f"{MARKER} brown in-range call" == n for n in notes):
            _fail("Carmeco call drill-down included a Brown record.")
        if any(f"{MARKER} carmeco out-of-range call" == n for n in notes):
            _fail("Carmeco call drill-down included an out-of-range call.")

        kpi_calls = int(_kpi(carmeco, "calls") or 0)
        code, kpi_records = testdb.http_json(
            "GET",
            f"/api/reports/records?section=client-results&metric=calls&client_id={CARMECO_ID}"
            f"&{qs}&limit=50&offset=0",
        )
        if code != 200:
            _fail(f"KPI call drill-down failed: {code} {kpi_records}")
        _assert_drill_matches(
            kpi_records,
            kpi_calls,
            client_id=CARMECO_ID,
            record_type="Call",
            label="Carmeco Client Results KPI calls",
        )

        code, team = testdb.http_json(
            "GET",
            f"/api/reports/team-performance?client_id={CARMECO_ID}&{qs}&user_id={julie_id}",
        )
        if code != 200:
            _fail(f"team-performance failed: {code} {team}")
        team_items = team.get("items") or []
        if len(team_items) != 1:
            _fail("Team rep filter should return that specialist only.")
        overdue_delta = _n(team_items[0], "overdue_tasks") - _n(base_team_row, "overdue_tasks")
        team_fu_delta = _n(team_items[0], "follow_ups_completed") - _n(
            base_team_row, "follow_ups_completed"
        )
        if overdue_delta != 1:
            _fail(f"Currently overdue follow-up was not counted on team performance (delta {overdue_delta}).")
        if team_fu_delta != 1:
            _fail("Team follow-ups completed should ignore the companion note.")

        code, team_calls = testdb.http_json(
            "GET",
            f"/api/reports/records?section=team-performance&metric=calls&client_id={CARMECO_ID}"
            f"&{qs}&user_id={julie_id}&row_user_id={julie_id}&limit=50&offset=0",
        )
        if code != 200:
            _fail(f"team call drill-down failed: {code} {team_calls}")
        _assert_drill_matches(
            team_calls,
            int(team_items[0]["calls"]),
            client_id=CARMECO_ID,
            record_type="Call",
            label="Team Performance calls",
        )

        code, camp_companies = testdb.http_json(
            "GET",
            f"/api/reports/records?section=campaign-performance&metric=companies"
            f"&client_id={CARMECO_ID}&{qs}&campaign_id={camp.campaign_id}"
            f"&row_campaign_id={camp.campaign_id}&row_client_id={CARMECO_ID}&limit=50&offset=0",
        )
        if code != 200:
            _fail(f"campaign company drill-down failed: {code} {camp_companies}")
        _assert_drill_matches(
            camp_companies,
            int(camp_items[0]["companies"]),
            client_id=CARMECO_ID,
            label="Campaign Performance companies",
        )

        code, camp_calls = testdb.http_json(
            "GET",
            f"/api/reports/records?section=campaign-performance&metric=calls"
            f"&client_id={CARMECO_ID}&{qs}&campaign_id={camp.campaign_id}"
            f"&row_campaign_id={camp.campaign_id}&row_client_id={CARMECO_ID}&limit=50&offset=0",
        )
        if code != 200:
            _fail(f"campaign call drill-down failed: {code} {camp_calls}")
        _assert_drill_matches(
            camp_calls,
            int(camp_items[0]["calls"]),
            client_id=CARMECO_ID,
            record_type="Call",
            label="Campaign Performance calls",
        )

        kpi_companies = int(_kpi(camp_rep, "companies") or 0)
        code, camp_kpi_companies = testdb.http_json(
            "GET",
            f"/api/reports/records?section=campaign-performance&metric=companies"
            f"&client_id={CARMECO_ID}&{qs}&campaign_id={camp.campaign_id}&limit=50&offset=0",
        )
        if code != 200:
            _fail(f"campaign KPI company drill-down failed: {code} {camp_kpi_companies}")
        _assert_drill_matches(
            camp_kpi_companies,
            kpi_companies,
            client_id=CARMECO_ID,
            label="Campaign Performance KPI companies",
        )

        client = testdb.test_client()
        csv_resp = client.get(
            f"/api/reports/export?section=client-results&client_id={CARMECO_ID}&{qs}"
        )
        _assert_excel_csv(
            csv_resp,
            title_kind="Client Results",
            columns=[
                "Client",
                "Calls Logged",
                "Follow-Ups Scheduled",
                "Follow-Ups Completed",
                "Appointments Set",
                "Current Hot Prospects",
            ],
            date_from=date_from,
            date_to=date_to,
            must_contain="Carmeco",
            label="Client Results",
        )

        csv_team = client.get(
            f"/api/reports/export?section=team-performance&client_id={CARMECO_ID}"
            f"&{qs}&user_id={julie_id}"
        )
        _assert_excel_csv(
            csv_team,
            title_kind="Team Performance",
            columns=[
                "Revenue Development Specialist",
                "Assigned Clients",
                "Calls",
                "Follow-Ups Completed",
                "Tasks Currently Overdue",
            ],
            date_from=date_from,
            date_to=date_to,
            must_contain="Julie Magnani",
            label="Team Performance",
        )

        csv_camp = client.get(
            f"/api/reports/export?section=campaign-performance&client_id={CARMECO_ID}"
            f"&{qs}&campaign_id={camp.campaign_id}"
        )
        _assert_excel_csv(
            csv_camp,
            title_kind="Campaign Performance",
            columns=[
                "Campaign",
                "Client",
                "Companies",
                "Contacts",
                "Appt / Companies",
                "Outcomes / Calls",
            ],
            date_from=date_from,
            date_to=date_to,
            must_contain=f"{MARKER} Stamping {stamp}",
            label="Campaign Performance",
        )

        csv_records = client.get(
            f"/api/reports/export?section=client-results&metric=calls&client_id={CARMECO_ID}"
            f"&{qs}&row_client_id={CARMECO_ID}"
        )
        _assert_excel_csv(
            csv_records,
            title_kind="Supporting records",
            columns=[
                "Client",
                "Company Name",
                "Company Record No.",
                "Contact Name",
                "Assigned Rep/User",
                "Date/Time",
                "Activity Or Record Type",
                "Status/Outcome",
                "Campaign",
                "Notes",
            ],
            date_from=date_from,
            date_to=date_to,
            must_contain=f"{MARKER} carmeco in-range call",
            must_not_contain=f"{MARKER} brown in-range call",
            label="Drill-down records",
        )

        detail_columns = [
            "Client",
            "Company Name",
            "Company Record No.",
            "Contact Name",
            "Assigned Rep/User",
            "Date/Time",
            "Activity Or Record Type",
            "Status/Outcome",
            "Campaign",
            "Notes",
        ]
        csv_detail = client.get(
            f"/api/reports/export?section=client-results&detail=true&client_id={CARMECO_ID}&{qs}"
        )
        detail_text = _assert_excel_csv(
            csv_detail,
            title_kind="Client Results (Detailed)",
            columns=detail_columns,
            date_from=date_from,
            date_to=date_to,
            must_contain=f"{MARKER} carmeco in-range call",
            must_not_contain=f"{MARKER} brown in-range call",
            label="Client Results detailed",
        )
        if detail_text.count(f"{MARKER} carmeco in-range call") != 1:
            _fail("Client Results detailed CSV duplicated an activity record.")
        if f"{MARKER} Co {stamp}" not in detail_text:
            _fail("Detailed CSV must include the company name even on activity rows.")
        if f"{record_no}-C" not in detail_text and record_no not in detail_text:
            _fail("Detailed CSV must include Company Record No.")

        csv_team_detail = client.get(
            f"/api/reports/export?section=team-performance&detail=true&client_id={CARMECO_ID}"
            f"&{qs}&user_id={julie_id}"
        )
        team_detail_text = _assert_excel_csv(
            csv_team_detail,
            title_kind="Team Performance (Detailed)",
            columns=detail_columns,
            date_from=date_from,
            date_to=date_to,
            must_contain=f"{MARKER} carmeco in-range call",
            must_not_contain=f"{MARKER} other-rep call",
            label="Team Performance detailed",
        )
        if team_detail_text.count(f"{MARKER} carmeco in-range call") != 1:
            _fail("Team Performance detailed CSV duplicated an activity record.")

        csv_camp_detail = client.get(
            f"/api/reports/export?section=campaign-performance&detail=true&client_id={CARMECO_ID}"
            f"&{qs}&campaign_id={camp.campaign_id}"
        )
        camp_detail_text = _assert_excel_csv(
            csv_camp_detail,
            title_kind="Campaign Performance (Detailed)",
            columns=detail_columns,
            date_from=date_from,
            date_to=date_to,
            must_contain=f"{MARKER} Co {stamp}",
            must_not_contain=f"{MARKER} call before campaign membership",
            label="Campaign Performance detailed",
        )
        if f"{MARKER} carmeco in-range call" not in camp_detail_text:
            _fail("Campaign detailed CSV should include calls after campaign membership.")
        if f"{MARKER} Stamping {stamp}" not in camp_detail_text:
            _fail("Campaign detailed CSV should include the campaign name when applicable.")
        if camp_detail_text.count(f"{MARKER} carmeco in-range call") > 1:
            _fail("Campaign detailed CSV duplicated an activity record.")

        csv_all_detail = client.get(
            f"/api/reports/export?section=client-results&detail=true&client_id=0&{qs}"
        )
        all_detail_text = _assert_excel_csv(
            csv_all_detail,
            title_kind="Client Results (Detailed)",
            columns=detail_columns,
            date_from=date_from,
            date_to=date_to,
            must_contain=f"{MARKER} carmeco in-range call",
            label="All My Clients detailed",
        )
        if f"{MARKER} brown in-range call" not in all_detail_text:
            _fail("All My Clients detailed CSV should keep Carmeco and Brown records on their own clients.")
        if all_detail_text.count(f"{MARKER} carmeco in-range call") != 1:
            _fail("All My Clients detailed CSV duplicated a shared-company activity.")
        if all_detail_text.count(f"{MARKER} brown in-range call") != 1:
            _fail("All My Clients detailed CSV duplicated the Brown activity.")

        post_code, post_body = testdb.http_json("POST", "/api/reports/client-results", {})
        if post_code not in {404, 405, 422}:
            _fail(f"Reports must stay read-only; POST returned {post_code} {post_body}")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            whirlpool_after = _snapshot_whirlpool(conn)
            brown_after = _ccr_snapshot(conn, company_id)
        if flora_after != flora_before:
            _fail("Flora Jia identity values changed during reports tests.")
        if whirlpool_after != whirlpool_before:
            _fail("Whirlpool identity values changed during reports tests.")
        brown_status = [r for r in brown_after if int(r["client_id"]) == BROWN_ID]
        brown_status_before = [r for r in brown_before if int(r["client_id"]) == BROWN_ID]
        if brown_status != brown_status_before:
            _fail("Brown CCR values changed during reports tests.")
        print("test_reports: PASS")
    finally:
        _cleanup(
            company_ids=company_ids,
            contact_ids=contact_ids,
            activity_ids=activity_ids,
            campaign_ids=campaign_ids,
            appointment_ids=appointment_ids,
            user_ids=user_ids,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_reports: FAIL: {exc}", file=sys.stderr)
        raise
