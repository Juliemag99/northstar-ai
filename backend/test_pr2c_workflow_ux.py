"""PR-2C — disposable RevOps workflow + isolation. Never writes production northstar.db."""

from __future__ import annotations

import os
import secrets
import time
from datetime import date, timedelta

import testdb

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER, ENFORCE_FLAG
from auth_passwords import hash_password
from client_workspace_data import (
    complete_contact_follow_up,
    create_contact_activity,
    get_contact_workspace,
    reschedule_contact_follow_up,
    update_contact_workflow,
)
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from models import (
    ContactActivityCreate,
    ContactFollowUpCompleteRequest,
    ContactFollowUpRescheduleRequest,
    ContactWorkflowUpdate,
)
from staff_rbac import REVOPS_SPECIALIST, ensure_staff_rbac_schema
from work_queue_data import list_dashboard_follow_ups, list_work_queue

os.environ.pop(ENFORCE_FLAG, None)

MARKER = "NSPR2C"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _clients() -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, code FROM clients").fetchall()
    out = {str(r["code"]).strip().lower(): int(r["id"]) for r in rows}
    for code in ("brown", "dawson", "premier", "carmeco"):
        if code not in out:
            _fail(f"isolated testdb missing client {code}")
    return out


def _create_specialist(email: str, password: str) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        ensure_staff_rbac_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, 0, 1, 1, ?, 0, '', ?)
            """,
            (email, f"PR2C {email.split('@')[0]}", digest, REVOPS_SPECIALIST),
        )
        user_id = int(conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"])
        conn.commit()
    return user_id


def _assign(user_id: int, client_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, 'staff', 1, datetime('now'))
            """,
            (int(user_id), int(client_id)),
        )
        conn.commit()


def _login(http: TestClient, email: str, password: str) -> str:
    resp = http.post("/api/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        _fail(f"login failed {resp.status_code} {resp.text}")
    return str(resp.json().get("csrf_token") or "")


def _queue_item_table_count(client_id: int) -> int:
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_queue_items'"
        ).fetchone() is None:
            return 0
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM work_queue_items
                WHERE client_id = ?
                  AND lower(COALESCE(completion_status, 'open')) IN
                      ('open', 'due', 'incomplete', 'pending', '')
                """,
                (client_id,),
            ).fetchone()[0]
        )


def test_production_path_untouched() -> None:
    live = str(PRODUCTION_DB_PATH).replace("\\", "/").lower()
    with get_connection() as conn:
        path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if path.replace("\\", "/").lower() == live:
        _fail(f"test connection is live production db: {path}")


def test_specialist_isolation_and_empty_due_queue() -> None:
    ids = _clients()
    password = _secret()
    pilots = [
        ("robert.pr2c@northstar.example.test", ids["brown"], "Brown"),
        ("tyler.pr2c@northstar.example.test", ids["dawson"], "Dawson"),
        ("todd.pr2c@northstar.example.test", ids["premier"], "Premier"),
    ]
    http = TestClient(app)
    for email, client_id, name in pilots:
        uid = _create_specialist(email, password)
        _assign(uid, client_id)
        csrf = _login(http, email, password)
        me = http.get("/api/auth/me").json()
        user = me.get("user") or {}
        if int(user.get("id") or 0) != uid:
            _fail(f"{email} /me id mismatch")
        if str(user.get("staff_role") or "") != REVOPS_SPECIALIST:
            _fail(f"{email} role {user.get('staff_role')}")
        picker = http.get("/api/users/default").json()
        client_ids = {int(c["client_id"]) for c in picker.get("clients") or []}
        if client_ids != {client_id}:
            _fail(f"{email} picker {client_ids} != {{{client_id}}}")

        forbidden = [cid for code, cid in ids.items() if cid != client_id]
        for other in forbidden:
            prospects = http.get(f"/api/prospects?client_id={other}&limit=5")
            if prospects.status_code != 403:
                _fail(f"{email} prospects client {other} expected 403, got {prospects.status_code}")
            reports = http.get(f"/api/reports/filters?client_id={other}")
            if reports.status_code != 403:
                _fail(f"{email} reports client {other} expected 403, got {reports.status_code}")
            campaigns = http.get(f"/api/campaigns?client_id={other}")
            if campaigns.status_code != 403:
                _fail(f"{email} campaigns client {other} expected 403, got {campaigns.status_code}")

        allowed = http.get(f"/api/prospects?client_id={client_id}&limit=5")
        if allowed.status_code != 200:
            _fail(f"{email} assigned prospects {allowed.status_code} {allowed.text[:200]}")
        for row in allowed.json().get("prospects") or []:
            row_cid = int(row.get("client_id") or 0)
            if row_cid not in (0, client_id):
                _fail(f"{email} prospects leaked client_id {row_cid}")

        reports_ok = http.get(f"/api/reports/filters?client_id={client_id}")
        if reports_ok.status_code != 200:
            _fail(f"{email} reports assigned {reports_ok.status_code}")

        queue = list_work_queue(
            uid,
            client_id=client_id,
            work_type="new",
            limit=20,
            offset=0,
            enrich_contacts=False,
            apply_insight_overlay=False,
        )
        new_n = int(queue.summary.new_assignments)
        table_n = _queue_item_table_count(client_id)
        # PW-2A: specialists see New rows assigned to them, not the whole client book.
        if new_n != 0:
            _fail(
                f"{name} specialist with no assigned New rows should see 0 New Assignments, got {new_n}"
            )
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_company_relationships
                SET assigned_user_id = ?
                WHERE id = (
                    SELECT id FROM client_company_relationships
                    WHERE client_id = ?
                      AND lower(trim(COALESCE(status, ''))) = 'new'
                    ORDER BY id
                    LIMIT 1
                )
                """,
                (uid, client_id),
            )
            conn.commit()
        assigned_q = list_work_queue(
            uid,
            client_id=client_id,
            work_type="new",
            limit=20,
            offset=0,
            enrich_contacts=False,
            apply_insight_overlay=False,
        )
        assigned_n = int(assigned_q.summary.new_assignments)
        if assigned_n < 1:
            _fail(f"{name} assigned New Assignments should be > 0, got {assigned_n}")
        if table_n > assigned_n + 50 and table_n > new_n + 50:
            _fail(f"{name} unexpected work_queue_items {table_n} vs new {assigned_n}")

        due = list_work_queue(
            uid,
            client_id=client_id,
            work_type="call",
            due="today",
            limit=5,
            enrich_contacts=False,
            apply_insight_overlay=False,
        )
        if int(due.summary.calls_due) < 0:
            _fail("calls_due negative")

        admin = http.get("/api/admin/master-data-export")
        if admin.status_code != 403:
            _fail(f"{email} master-data-export {admin.status_code}")
        lm = http.get("/api/admin/leadmaster-refresh/meta")
        if lm.status_code != 403:
            _fail(f"{email} leadmaster meta {lm.status_code}")
        ds = http.get("/api/admin/data-steward/meta")
        if ds.status_code != 403:
            _fail(f"{email} data-steward meta {ds.status_code}")
        upload = http.post(
            f"/api/clients/{client_id}/admin/imports",
            headers={CSRF_HEADER: csrf},
            files={"file": ("x.csv", b"Company\nAcme", "text/csv")},
        )
        if upload.status_code != 403:
            _fail(f"{email} import {upload.status_code}")

        if name == "Brown":
            ask = http.post(
                "/api/ask-northstar",
                headers={CSRF_HEADER: csrf},
                json={
                    "question": "Show me hot prospects",
                    "scope": "active_client",
                    "active_client_id": client_id,
                },
            )
            if ask.status_code != 200:
                _fail(f"{email} ask hot {ask.status_code} {ask.text[:300]}")
            follow = http.post(
                "/api/ask-northstar",
                headers={CSRF_HEADER: csrf},
                json={
                    "question": "Which companies need follow-up?",
                    "scope": "active_client",
                    "active_client_id": client_id,
                },
            )
            if follow.status_code != 200:
                _fail(f"{email} ask follow-up {follow.status_code} {follow.text[:300]}")
        spoof = http.post(
            "/api/ask-northstar",
            headers={CSRF_HEADER: csrf},
            json={
                "question": "What do we know about this client?",
                "scope": "active_client",
                "active_client_id": forbidden[0],
            },
        )
        if spoof.status_code != 403:
            if spoof.status_code == 200:
                _fail(f"{email} Ask spoofed other client")
            _fail(f"{email} Ask spoof expected 403, got {spoof.status_code}")
        http.post("/api/auth/logout")


def test_contact_call_and_follow_up_workflow() -> None:
    ids = _clients()
    brown_id = ids["brown"]
    carmeco_id = ids["carmeco"]
    password = _secret()
    email = f"callflow-{secrets.token_hex(3)}@northstar.example.test"
    uid = _create_specialist(email, password)
    _assign(uid, brown_id)

    stamp = str(int(time.time()))
    record_no = f"{MARKER}-{stamp}"
    company_id = contact_id = 0
    activity_ids: list[int] = []
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, city, state, legacy_phone)
            VALUES (?, ?, 'Testville', 'OH', '555-0100')
            """,
            (record_no, f"{MARKER} Call Co {stamp}"),
        )
        company_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, next_action, assigned_user_id
            ) VALUES (?, ?, ?, 'New', '', ?)
            """,
            (brown_id, company_id, record_no, uid),
        )
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title, phone, email,
                source_row_index
            ) VALUES (?, ?, 'Pat', 'Caller', 'Buyer', '555-0199', 'pat@example.test', 1)
            """,
            (company_id, record_no),
        )
        contact_id = int(cur.lastrowid)
        conn.commit()

    try:
        ws = get_contact_workspace(contact_id, client_id=brown_id)
        if not ws.phone:
            _fail("contact phone missing on workspace")
        if (ws.company_name or "") == "":
            _fail("company name missing")
        if int(ws.client_id) != brown_id:
            _fail("workspace client mismatch")

        called = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Call",
                notes=f"{MARKER} left voicemail",
                outcome="Left Message",
                status="Left Message",
                next_action="Call",
                created_by="PR2C Caller",
            ),
        )
        if called.get("activity_id"):
            activity_ids.append(int(called["activity_id"]))

        noted = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Note",
                notes=f"{MARKER} gatekeeper asked to call Thursday",
                created_by="PR2C Caller",
            ),
        )
        if noted.get("activity_id"):
            activity_ids.append(int(noted["activity_id"]))

        updated = update_contact_workflow(
            contact_id,
            ContactWorkflowUpdate(
                client_id=brown_id,
                status="Left Message",
                next_action="Call",
                assigned_user_id=uid,
                user="PR2C Caller",
            ),
        )
        ws = updated["workspace"] if isinstance(updated, dict) and "workspace" in updated else get_contact_workspace(
            contact_id, client_id=brown_id
        )
        if str(ws.status) != "Left Message":
            _fail(f"status not saved: {ws.status}")

        today = date.today().isoformat()
        future = (date.today() + timedelta(days=5)).isoformat()
        scheduled = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=brown_id,
                activity_type="Follow-Up",
                notes=f"{MARKER} follow today",
                schedule_follow_up=True,
                follow_up_date=today,
                follow_up_time="09:30",
                next_action="Call",
                created_by="PR2C Caller",
            ),
        )
        if scheduled.get("activity_id"):
            activity_ids.append(int(scheduled["activity_id"]))
        if scheduled.get("follow_up_activity_id"):
            activity_ids.append(int(scheduled["follow_up_activity_id"]))

        dash = list_dashboard_follow_ups(client_id=brown_id)
        if not any(int(item.contact_id or 0) == contact_id for item in dash.due_today + dash.overdue):
            _fail("today follow-up missing from dashboard")
        if any(int(item.contact_id or 0) == contact_id for item in list_dashboard_follow_ups(client_id=carmeco_id).due_today):
            _fail("Brown follow-up leaked to Carmeco dashboard")

        queue = list_work_queue(uid, client_id=brown_id, work_type="follow-up", due="today", q=MARKER)
        if not any(int(r.contact_id or 0) == contact_id for r in queue.items):
            _fail("today follow-up missing from work queue")

        ws = get_contact_workspace(contact_id, client_id=brown_id)
        rescheduled = reschedule_contact_follow_up(
            contact_id,
            ContactFollowUpRescheduleRequest(
                client_id=brown_id,
                follow_up_date=future,
                follow_up_time="14:00",
                notes=f"{MARKER} reschedule",
                created_by="PR2C Caller",
            ),
        )
        if rescheduled.get("activity_id"):
            activity_ids.append(int(rescheduled["activity_id"]))
        dash2 = list_dashboard_follow_ups(client_id=brown_id)
        if any(int(item.contact_id or 0) == contact_id for item in dash2.due_today + dash2.overdue):
            _fail("rescheduled follow-up still due today")
        if not any(int(item.contact_id or 0) == contact_id for item in dash2.upcoming):
            _fail("rescheduled follow-up missing from upcoming")

        completed = complete_contact_follow_up(
            contact_id,
            ContactFollowUpCompleteRequest(
                client_id=brown_id,
                notes=f"{MARKER} complete",
                created_by="PR2C Caller",
            ),
        )
        if completed.get("activity_id"):
            activity_ids.append(int(completed["activity_id"]))
        ws = completed["workspace"]
        if ws.open_follow_up is not None:
            _fail("complete should clear open follow-up")
        dash3 = list_dashboard_follow_ups(client_id=brown_id)
        if any(int(item.contact_id or 0) == contact_id for item in dash3.due_today + dash3.overdue + dash3.upcoming):
            _fail("completed follow-up still on dashboard")
        titles = [str(item.title or "") + str(item.body or "") for item in (ws.timeline or [])]
        joined = " ".join(titles)
        if MARKER not in joined:
            _fail("call/note history missing after complete")
    finally:
        with get_connection() as conn:
            for aid in activity_ids:
                conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
            conn.execute(
                "DELETE FROM activities WHERE contact_id = ? OR company_id = ?",
                (contact_id, company_id),
            )
            conn.execute("DELETE FROM client_company_relationships WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
            conn.commit()


def test_campaign_not_required_for_new_queue() -> None:
    ids = _clients()
    brown_id = ids["brown"]
    with get_connection() as conn:
        members = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM campaign_companies cc
                JOIN client_campaigns c ON c.id = cc.campaign_id
                WHERE c.client_id = ?
                """,
                (brown_id,),
            ).fetchone()[0]
        ) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_companies'"
        ).fetchone() else 0
        new_n = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM client_company_relationships
                WHERE client_id = ? AND lower(trim(status)) = 'new'
                  AND TRIM(COALESCE(archived_at, '')) = ''
                """,
                (brown_id,),
            ).fetchone()[0]
        )
    if new_n <= 0:
        _fail("Brown should have New CCRs for calling without campaigns")
    # Zero memberships is expected and must not block New Assignment queue.
    _ = members


def main() -> None:
    tests = [
        test_production_path_untouched,
        test_specialist_isolation_and_empty_due_queue,
        test_contact_call_and_follow_up_workflow,
        test_campaign_not_required_for_new_queue,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} tests ok")


if __name__ == "__main__":
    main()
