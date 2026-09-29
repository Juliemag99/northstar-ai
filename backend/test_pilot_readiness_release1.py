"""Pilot Readiness Release 1 — campaign gate and staff feedback.

Uses the isolated testdb copy. Never migrates production northstar.db.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import testdb

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from manual_contact_data import preview_manual_contact
from models import ManualContactPreviewRequest
from staff_context import bind_staff_actor, reset_staff_actor
from staff_feedback import (
    ensure_staff_feedback_schema,
    submit_staff_feedback,
)
from staff_rbac import (
    REVOPS_MANAGER,
    REVOPS_SPECIALIST,
    SYSTEM_ADMINISTRATOR,
    ensure_staff_role_schema,
    set_user_staff_role,
    user_has_permission,
)
from access import get_user_by_id


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _assert_isolated() -> None:
    with get_connection() as conn:
        opened = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if Path(opened).resolve() == PRODUCTION_DB_PATH.resolve():
        _fail("Test opened production northstar.db.")


def _secret(label: str) -> str:
    return f"NsTest9{label}"


def _clients() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _create_user(email: str, role: str, administrator: int = 0) -> tuple[int, str]:
    password = _secret(email)
    with get_connection() as conn:
        ensure_staff_role_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, ?, 1, 1, ?, 0, '', ?)
            """,
            (email, f"Release1 {role}", administrator, hash_password(password, email=email), role),
        )
        user_id = int(conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"])
        set_user_staff_role(conn, user_id, role)
        conn.commit()
    return user_id, password


def _assign(user_id: int, client_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, 'staff', 1, datetime('now'))
            """,
            (user_id, client_id),
        )
        conn.commit()


def _login(http: TestClient, email: str, password: str) -> str:
    response = http.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        _fail(f"Login failed for {email}: {response.status_code} {response.text}")
    return str(response.json().get("csrf_token") or "")


def _company_for(client_id: int) -> tuple[int, str, int | None]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT co.id AS company_id,
                   COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no) AS record_no,
                   ct.id AS contact_id
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            LEFT JOIN contacts ct ON ct.company_id = co.id
            WHERE ccr.client_id = ?
            ORDER BY ct.id IS NULL, ccr.id
            LIMIT 1
            """,
            (client_id,),
        ).fetchone()
    if row is None or not str(row["record_no"] or "").strip():
        _fail(f"No company for client {client_id}.")
    contact_id = int(row["contact_id"]) if row["contact_id"] is not None else None
    return int(row["company_id"]), str(row["record_no"]), contact_id


def _unassigned_count(client_id: int, company_id: int) -> int:
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_unassigned'"
        ).fetchone()
        if exists is None:
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM campaign_unassigned
            WHERE client_id = ? AND company_id = ?
            """,
            (client_id, company_id),
        ).fetchone()
    return int(row["n"])


def _feedback_count() -> int:
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='staff_feedback'"
        ).fetchone()
        if exists is None:
            return 0
        return int(conn.execute("SELECT COUNT(*) AS n FROM staff_feedback").fetchone()["n"])


def test_specialist_route_get_does_not_write_and_manager_still_prompts() -> None:
    _assert_isolated()
    allowed, _forbidden = _clients()
    company_id, _record_no, _contact_id = _company_for(allowed)
    specialist_email = "route-spec-r1@example.test"
    manager_email = "route-mgr-r1@example.test"
    spec_id, spec_pw = _create_user(specialist_email, REVOPS_SPECIALIST)
    mgr_id, mgr_pw = _create_user(manager_email, REVOPS_MANAGER)
    _assign(spec_id, allowed)
    _assign(mgr_id, allowed)
    if user_has_permission(spec_id, "campaigns.manage", client_id=allowed):
        _fail("Specialist must not have campaigns.manage.")
    if not user_has_permission(mgr_id, "campaigns.manage", client_id=allowed):
        _fail("Manager must have campaigns.manage.")

    before = _unassigned_count(allowed, company_id)
    spec_http = TestClient(app)
    spec_csrf = _login(spec_http, specialist_email, spec_pw)
    suggestion = spec_http.get(
        "/api/campaigns/route",
        params={
            "client_id": allowed,
            "company_id": company_id,
            "source": "status",
            "force": "true",
        },
    )
    if suggestion.status_code != 200:
        _fail(f"Specialist route GET -> {suggestion.status_code} {suggestion.text}")
    body = suggestion.json()
    if body.get("should_prompt") is not False or body.get("auto_unassigned") is not False:
        _fail(f"Specialist route still prompts or writes: {body}")
    after = _unassigned_count(allowed, company_id)
    if after != before:
        _fail(f"Specialist route GET changed campaign_unassigned {before} -> {after}.")

    confirm = spec_http.post(
        "/api/campaigns/route/confirm",
        headers={CSRF_HEADER: spec_csrf},
        json={
            "client_id": allowed,
            "company_id": company_id,
            "campaign_id": 1,
            "source": "status",
        },
    )
    defer = spec_http.post(
        "/api/campaigns/route/defer",
        headers={CSRF_HEADER: spec_csrf},
        json={"client_id": allowed, "company_id": company_id, "source": "not_now"},
    )
    if confirm.status_code != 403 or defer.status_code != 403:
        _fail(f"Specialist confirm/defer were not forbidden: {confirm.status_code} {defer.status_code}")
    if _unassigned_count(allowed, company_id) != before:
        _fail("Specialist defer wrote campaign_unassigned.")

    with get_connection() as conn:
        from campaigns_data import ensure_campaigns_schema

        ensure_campaigns_schema()
        conn.execute(
            """
            INSERT INTO client_campaigns (
                client_id, campaign_name, description, is_active, is_default,
                status, owner_user_id, owner_name, start_date, end_date, category,
                notes, created_at, updated_at
            ) VALUES (?, 'Pilot Route Test', '', 1, 0, 'Active', ?, 'Release1', '', '', '', '', datetime('now'), datetime('now'))
            """,
            (allowed, mgr_id),
        )
        open_company = conn.execute(
            """
            SELECT co.id
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM campaign_companies cc
                WHERE cc.client_id = ccr.client_id AND cc.company_id = co.id
              )
            ORDER BY co.id
            LIMIT 1
            """,
            (allowed,),
        ).fetchone()
        conn.commit()
    if open_company is None:
        _fail("No company without a campaign membership on the isolated client.")
    mgr_http = TestClient(app)
    _login(mgr_http, manager_email, mgr_pw)
    managed = mgr_http.get(
        "/api/campaigns/route",
        params={"client_id": allowed, "company_id": int(open_company["id"]), "source": "status"},
    )
    if managed.status_code != 200:
        _fail(f"Manager route GET -> {managed.status_code} {managed.text}")
    if managed.json().get("should_prompt") is not True:
        _fail(f"Manager lost the campaign prompt: {managed.json()}")


def test_null_and_named_contact_calls_stay_scoped() -> None:
    _assert_isolated()
    allowed, _forbidden = _clients()
    company_id, record_no, contact_id = _company_for(allowed)
    if contact_id is None:
        _fail("Isolated company has no contact for the named-contact assertion.")
    email = "call-spec-r1@example.test"
    user_id, password = _create_user(email, REVOPS_SPECIALIST)
    _assign(user_id, allowed)
    http = TestClient(app)
    csrf = _login(http, email, password)
    headers = {CSRF_HEADER: csrf}

    def log_call(chosen: int | None) -> int:
        response = http.post(
            "/api/work-queue/log-call",
            headers=headers,
            json={
                "client_id": allowed,
                "external_record_no": record_no,
                "contact_id": chosen,
                "outcome": "Left Message",
                "notes": "Pilot readiness contact scope check.",
                "next_action": "Follow-Up",
                "complete_current": False,
            },
        )
        if response.status_code != 200:
            _fail(f"Log call failed: {response.status_code} {response.text}")
        activity_id = response.json().get("activity_id")
        if not activity_id:
            _fail(f"Log call did not return an activity: {response.text}")
        return int(activity_id)

    null_id = log_call(None)
    named_id = log_call(contact_id)
    with get_connection() as conn:
        null_row = conn.execute(
            "SELECT client_id, company_id, contact_id FROM activities WHERE activity_id = ?",
            (null_id,),
        ).fetchone()
        named_row = conn.execute(
            "SELECT client_id, company_id, contact_id FROM activities WHERE activity_id = ?",
            (named_id,),
        ).fetchone()
        on_company = conn.execute(
            """
            SELECT activity_id FROM activities
            WHERE client_id = ? AND company_id = ? AND contact_id IS NULL AND activity_id = ?
            """,
            (allowed, company_id, null_id),
        ).fetchone()
        named_on_contact = conn.execute(
            """
            SELECT activity_id FROM activities
            WHERE client_id = ? AND company_id = ? AND contact_id = ? AND activity_id = ?
            """,
            (allowed, company_id, contact_id, named_id),
        ).fetchone()
        null_on_contact = conn.execute(
            """
            SELECT activity_id FROM activities
            WHERE contact_id = ? AND activity_id = ?
            """,
            (contact_id, null_id),
        ).fetchone()
    if null_row is None or null_row["contact_id"] is not None:
        _fail("Null-contact call was stored on a person.")
    if on_company is None:
        _fail("Null-contact call is missing from the company-level activity set.")
    if null_on_contact is not None:
        _fail("Null-contact call was attributed to the named contact.")
    if named_row is None or int(named_row["contact_id"]) != contact_id or named_on_contact is None:
        _fail("Named-contact call was not stored on that contact.")


def test_add_contact_still_uses_crm_edit_and_duplicate_checks() -> None:
    _assert_isolated()
    allowed, _forbidden = _clients()
    company_id, _record_no, contact_id = _company_for(allowed)
    if contact_id is None:
        _fail("Need an existing contact to prove duplicate detection.")
    email = "add-spec-r1@example.test"
    user_id, password = _create_user(email, REVOPS_SPECIALIST)
    _assign(user_id, allowed)
    if not user_has_permission(user_id, "crm.edit", client_id=allowed):
        _fail("Specialist lost crm.edit.")
    http = TestClient(app)
    csrf = _login(http, email, password)
    headers = {CSRF_HEADER: csrf}
    missing = http.post(
        "/api/contacts/manual/preview",
        headers=headers,
        json={
            "client_id": allowed,
            "company_id": company_id,
            "first_name": "",
            "last_name": "",
        },
    )
    if missing.status_code != 400:
        _fail(f"Blank Add Contact name was not rejected: {missing.status_code} {missing.text}")
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT first_name, last_name FROM contacts WHERE id = ?",
            (contact_id,),
        ).fetchone()
    duplicate = http.post(
        "/api/contacts/manual/preview",
        headers=headers,
        json={
            "client_id": allowed,
            "company_id": company_id,
            "first_name": existing["first_name"],
            "last_name": existing["last_name"],
        },
    )
    if duplicate.status_code != 200:
        _fail(f"Duplicate preview failed: {duplicate.status_code} {duplicate.text}")
    preview = duplicate.json()
    if not preview.get("matches"):
        _fail("Existing contact was not detected by the duplicate check.")
    created = http.post(
        "/api/contacts/manual",
        headers=headers,
        json={
            "client_id": allowed,
            "company_id": company_id,
            "action": "create",
            "first_name": "Pilot",
            "last_name": "ReadinessUnique",
            "confirm_without_contact_info": True,
        },
    )
    if created.status_code != 200:
        _fail(f"Legitimate Add Contact failed: {created.status_code} {created.text}")
    if int(created.json().get("contact_id") or 0) <= 0:
        _fail("Add Contact did not return a contact id.")
    user = get_user_by_id(user_id)
    token = bind_staff_actor(user)
    try:
        direct = preview_manual_contact(
            ManualContactPreviewRequest(
                client_id=allowed,
                company_id=company_id,
                first_name="Pilot",
                last_name="ReadinessUnique",
            )
        )
    finally:
        reset_staff_actor(token)
    if not any(match.contact_id == int(created.json()["contact_id"]) for match in direct.matches):
        _fail("Newly added contact was not found by the duplicate preview.")


def _feedback_body(**overrides):
    payload = {
        "category": "Problem",
        "impact": "Can continue",
        "body": "The queue label is unclear.",
        "page_route": "/work-queue",
        "client_id": None,
        "company_id": None,
        "contact_id": None,
    }
    payload.update(overrides)
    return payload


def test_feedback_security_and_admin_review() -> None:
    _assert_isolated()
    allowed, forbidden = _clients()
    company_id, _record_no, contact_id = _company_for(allowed)
    spec_email = "fb-spec-r1@example.test"
    admin_email = "fb-admin-r1@example.test"
    spec_id, spec_pw = _create_user(spec_email, REVOPS_SPECIALIST)
    admin_id, admin_pw = _create_user(admin_email, SYSTEM_ADMINISTRATOR, administrator=1)
    _assign(spec_id, allowed)
    _assign(admin_id, allowed)
    with get_connection() as conn:
        ensure_staff_feedback_schema(conn)
        conn.commit()
        before_activities = int(conn.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"])
    before_feedback = _feedback_count()

    anon = TestClient(app)
    unauthenticated = anon.post("/api/feedback", json=_feedback_body())
    if unauthenticated.status_code != 401:
        _fail(f"Unauthenticated feedback was not 401: {unauthenticated.status_code}")

    spec = TestClient(app)
    spec_csrf = _login(spec, spec_email, spec_pw)
    headers = {CSRF_HEADER: spec_csrf}

    def post(payload: dict, client: TestClient | None = None, csrf: str | None = None):
        http = client or spec
        token = spec_csrf if csrf is None else csrf
        return http.post("/api/feedback", headers={CSRF_HEADER: token}, json=payload)

    rejected = [
        ("category", _feedback_body(category="Workflow")),
        ("impact", _feedback_body(impact="Urgent")),
        ("blank", _feedback_body(body="   ")),
        ("long", _feedback_body(body="x" * 4001)),
        ("secret", {**_feedback_body(), "password": "secret-value"}),
        ("user", {**_feedback_body(), "user_id": admin_id}),
        ("client", _feedback_body(client_id=forbidden)),
    ]
    for label, payload in rejected:
        response = post(payload)
        if response.status_code not in {400, 403, 422}:
            _fail(f"{label} feedback was accepted: {response.status_code} {response.text}")
    if _feedback_count() != before_feedback:
        _fail("Rejected feedback was stored.")

    bad_company = post(_feedback_body(client_id=allowed, company_id=company_id + 999999))
    if bad_company.status_code != 400:
        _fail(f"Invalid company context was not 400: {bad_company.status_code} {bad_company.text}")
    if contact_id is not None:
        bad_contact = post(
            _feedback_body(client_id=allowed, company_id=company_id, contact_id=contact_id + 999999)
        )
        if bad_contact.status_code != 400:
            _fail(f"Invalid contact context was not 400: {bad_contact.status_code}")

    ok = post(
        _feedback_body(
            client_id=allowed,
            company_id=company_id,
            contact_id=contact_id,
            body="Saved from the specialist session.",
        )
    )
    if ok.status_code != 200:
        _fail(f"Valid feedback failed: {ok.status_code} {ok.text}")
    created = ok.json()
    if int(created["user_id"]) != spec_id or created.get("status") != "New":
        _fail(f"Feedback did not keep the session user and New status: {created}")
    feedback_id = int(created["id"])
    with get_connection() as conn:
        stored = conn.execute("SELECT * FROM staff_feedback WHERE id = ?", (feedback_id,)).fetchone()
        after_activities = int(conn.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"])
    dumped = json.dumps(dict(stored), default=str).lower()
    for needle in ("password", "northstar_session", "csrf", "$argon2id$", "secret-value"):
        if needle in dumped:
            _fail(f"Feedback row leaked {needle}.")
    if after_activities != before_activities:
        _fail("Feedback insert changed activity rows.")
    if int(stored["user_id"]) != spec_id:
        _fail("Stored user_id did not come from the session.")

    listed = spec.get("/api/feedback")
    updated = spec.patch(
        f"/api/feedback/{feedback_id}",
        headers=headers,
        json={"status": "Reviewed"},
    )
    if listed.status_code != 403 or updated.status_code != 403:
        _fail(f"Specialist was allowed to review feedback: {listed.status_code} {updated.status_code}")

    admin = TestClient(app)
    admin_csrf = _login(admin, admin_email, admin_pw)
    admin_list = admin.get("/api/feedback")
    if admin_list.status_code != 200:
        _fail(f"Admin list failed: {admin_list.status_code} {admin_list.text}")
    if not any(int(item["id"]) == feedback_id for item in admin_list.json().get("items", [])):
        _fail("Admin list did not include the specialist submission.")
    for status in ("Reviewed", "Planned", "Resolved", "Won't Change"):
        changed = admin.patch(
            f"/api/feedback/{feedback_id}",
            headers={CSRF_HEADER: admin_csrf},
            json={"status": status},
        )
        if changed.status_code != 200 or changed.json().get("status") != status:
            _fail(f"Admin could not set status {status}: {changed.status_code} {changed.text}")
    invalid_status = admin.patch(
        f"/api/feedback/{feedback_id}",
        headers={CSRF_HEADER: admin_csrf},
        json={"status": "Closed", "body": "no replies"},
    )
    if invalid_status.status_code not in {400, 422}:
        _fail(f"Invalid status was accepted: {invalid_status.status_code}")

    with get_connection() as conn:
        try:
            submit_staff_feedback(
                conn,
                user_id=spec_id,
                page_route="/work-queue",
                body=" ",
                category="Problem",
                impact="Minor",
            )
            _fail("Blank body was accepted by the helper.")
        except ValueError:
            conn.rollback()
        still = int(conn.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"])
        status_row = conn.execute(
            "SELECT status FROM staff_feedback WHERE id = ?",
            (feedback_id,),
        ).fetchone()
    if still != before_activities:
        _fail("Failed feedback insert changed CRM activity rows.")
    if status_row["status"] != "Won't Change":
        _fail("Failed insert rolled back an earlier feedback status.")


def test_feedback_migration_is_idempotent_and_upgrades_old_table() -> None:
    _assert_isolated()
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    try:
        ensure_staff_feedback_schema(empty)
        ensure_staff_feedback_schema(empty)
        names = {str(row["name"]) for row in empty.execute("PRAGMA table_info(staff_feedback)")}
        for column in ("impact", "status", "company_id", "contact_id", "user_agent", "body"):
            if column not in names:
                _fail(f"Fresh feedback table is missing {column}.")
        indexes = {
            str(row["name"])
            for row in empty.execute("PRAGMA index_list(staff_feedback)")
        }
        for index in (
            "idx_staff_feedback_created",
            "idx_staff_feedback_status",
            "idx_staff_feedback_client",
        ):
            if index not in indexes:
                _fail(f"Missing feedback index {index}.")
        auth = empty.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='staff_auth_events'"
        ).fetchone()
        if auth is not None:
            _fail("Feedback schema helper created staff_auth_events.")
    finally:
        empty.close()

    handle = tempfile.NamedTemporaryFile(prefix="ns-feedback-old-", suffix=".db", delete=False)
    handle.close()
    old_path = Path(handle.name)
    try:
        if old_path.resolve() == PRODUCTION_DB_PATH.resolve():
            _fail("Upgrade test targeted production.")
        old = sqlite3.connect(old_path)
        old.row_factory = sqlite3.Row
        old.execute(
            """
            CREATE TABLE staff_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                created_at TEXT NOT NULL,
                page_route TEXT NOT NULL DEFAULT '',
                client_id INTEGER,
                category TEXT NOT NULL DEFAULT 'Other',
                body TEXT NOT NULL,
                user_agent TEXT NOT NULL DEFAULT ''
            )
            """
        )
        old.execute(
            """
            INSERT INTO staff_feedback (user_id, created_at, page_route, category, body)
            VALUES (1, '2026-01-01T00:00:00Z', '/old', 'Other', 'legacy')
            """
        )
        old.commit()
        ensure_staff_feedback_schema(old)
        ensure_staff_feedback_schema(old)
        row = old.execute("SELECT impact, status, company_id, contact_id, body FROM staff_feedback").fetchone()
        if row["body"] != "legacy" or row["status"] != "New":
            _fail(f"Old feedback row was not preserved: {dict(row)}")
        if row["company_id"] is not None or row["contact_id"] is not None:
            _fail("Upgrade invented company or contact context.")
        old.close()
    finally:
        old_path.unlink(missing_ok=True)


def main() -> None:
    tests = [
        test_specialist_route_get_does_not_write_and_manager_still_prompts,
        test_null_and_named_contact_calls_stay_scoped,
        test_add_contact_still_uses_crm_edit_and_duplicate_checks,
        test_feedback_security_and_admin_review,
        test_feedback_migration_is_idempotent_and_upgrades_old_table,
    ]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"PASS {len(tests)} pilot readiness release 1 tests")


if __name__ == "__main__":
    main()
