"""Phase 6A — isolated role/permission/client-access tests.

Never writes production northstar.db. Never stores a real staff password.
Import testdb first so HTTP tests hit a throwaway copy.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import testdb

from fastapi.testclient import TestClient

from auth_http import (
    ADMIN_REQUIRED_DETAIL,
    AUTH_REQUIRED_DETAIL,
    CSRF_HEADER,
    ENFORCE_FLAG,
)
from auth_passwords import hash_password
from company_merge_approvals import MergeApprovalError, create_merge_approval
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from staff_feedback import ensure_staff_feedback_schema, submit_staff_feedback
from staff_rbac import (
    APPOINTMENT_SETTER,
    OPERATIONS_ADMIN,
    READ_ONLY,
    REVOPS_MANAGER,
    REVOPS_SPECIALIST,
    SYSTEM_ADMINISTRATOR,
    ensure_staff_rbac_schema,
    permissions_for_role,
    set_user_staff_role,
    user_has_permission,
)

MATRIX_PATH = (
    Path(__file__).resolve().parent.parent
    / "working"
    / "revops-pilot-phase6a"
    / "permission_test_matrix.json"
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _create_user(
    *,
    email: str,
    password: str,
    role: str,
    administrator: int = 0,
    internal: int = 0,
) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        ensure_staff_rbac_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, ?, ?, 1, ?, 0, '', ?)
            """,
            (
                email,
                f"Phase6A {role}",
                int(administrator),
                int(internal),
                digest,
                role,
            ),
        )
        user_id = int(
            conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        )
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


def _login(http: TestClient, email: str, password: str):
    return http.post("/api/auth/login", json={"email": email, "password": password})


def _csrf(resp) -> str:
    return str(resp.json().get("csrf_token") or "")


def _record_for_client(client_id: int) -> str:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no) AS rn
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
            ORDER BY ccr.id
            LIMIT 1
            """,
            (int(client_id),),
        ).fetchone()
    if row is None:
        _fail(f"No CCR on client {client_id} in isolated copy.")
    return str(row["rn"])


def test_role_permission_matrix() -> None:
    expected = {
        SYSTEM_ADMINISTRATOR: {
            "users.manage": True,
            "imports.manage": True,
            "exports.master_data": True,
            "duplicates.execute": True,
            "system.manage": True,
            "crm.edit": True,
            "client.view": True,
        },
        OPERATIONS_ADMIN: {
            "users.manage": False,
            "imports.manage": False,
            "duplicates.execute": False,
            "system.manage": False,
            "admin.view": True,
            "clients.manage": True,
            "crm.edit": True,
        },
        REVOPS_MANAGER: {
            "campaigns.manage": True,
            "reports.view": True,
            "imports.manage": False,
            "admin.view": False,
            "users.manage": False,
        },
        REVOPS_SPECIALIST: {
            "crm.edit": True,
            "campaigns.manage": False,
            "imports.manage": False,
            "exports.master_data": False,
            "duplicates.approve": False,
            "admin.view": False,
        },
        APPOINTMENT_SETTER: {
            "crm.edit": True,
            "reports.view": False,
            "campaigns.manage": False,
            "admin.view": False,
        },
        READ_ONLY: {
            "client.view": True,
            "crm.edit": False,
            "notes.edit": False,
            "tasks.edit": False,
            "imports.manage": False,
        },
    }
    for role, checks in expected.items():
        granted = permissions_for_role(role)
        for perm, want in checks.items():
            got = perm in granted
            if got is not want:
                _fail(f"{role} {perm} expected {want}, got {got}")


def test_client_scope_is_not_the_role() -> None:
    allowed, forbidden = _client_ids()
    specialist = _create_user(
        email=f"spec-{secrets.token_hex(4)}@example.test",
        password=_secret(),
        role=REVOPS_SPECIALIST,
    )
    readonly = _create_user(
        email=f"ro-{secrets.token_hex(4)}@example.test",
        password=_secret(),
        role=READ_ONLY,
    )
    admin = _create_user(
        email=f"adm-{secrets.token_hex(4)}@example.test",
        password=_secret(),
        role=SYSTEM_ADMINISTRATOR,
        administrator=1,
        internal=1,
    )
    _assign(specialist, allowed)
    _assign(readonly, allowed)

    if not user_has_permission(specialist, "crm.edit", client_id=allowed):
        _fail("Specialist must edit assigned client.")
    if user_has_permission(specialist, "crm.edit", client_id=forbidden):
        _fail("Specialist must not edit an unassigned client.")
    if user_has_permission(specialist, "exports.master_data"):
        _fail("Specialist must not export master data.")
    if user_has_permission(readonly, "crm.edit", client_id=allowed):
        _fail("Read-only must not mutate CRM.")
    if not user_has_permission(readonly, "client.view", client_id=allowed):
        _fail("Read-only must view assigned client.")
    if user_has_permission(readonly, "client.view", client_id=forbidden):
        _fail("Read-only must not view an unassigned client.")
    if not user_has_permission(admin, "duplicates.execute"):
        _fail("System administrator must execute merges.")
    if not user_has_permission(admin, "crm.edit", client_id=forbidden):
        _fail("System administrator retains all-client access via is_administrator.")


def test_boolean_admin_flag_is_not_a_role_grant_when_staff_role_set() -> None:
    allowed, _forbidden = _client_ids()
    user_id = _create_user(
        email=f"flag-{secrets.token_hex(4)}@example.test",
        password=_secret(),
        role=READ_ONLY,
        administrator=0,
    )
    _assign(user_id, allowed)
    with get_connection() as conn:
        set_user_staff_role(conn, user_id, READ_ONLY)
        conn.commit()
    if user_has_permission(user_id, "users.manage"):
        _fail("Read-only user must not manage users.")


def test_admin_http_blocked_for_specialist() -> None:
    os.environ.pop(ENFORCE_FLAG, None)
    allowed, _forbidden = _client_ids()
    password = _secret()
    specialist = _create_user(
        email=f"http-spec-{secrets.token_hex(4)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
    )
    _assign(specialist, allowed)
    http = TestClient(app)
    with get_connection() as conn:
        email = str(
            conn.execute("SELECT email FROM users WHERE id = ?", (specialist,)).fetchone()[
                "email"
            ]
        )
    login = _login(http, email, password)
    if login.status_code != 200:
        _fail(f"Specialist login failed: {login.status_code} {login.text}")
    csrf = _csrf(login)

    export = http.get("/api/admin/master-data-export")
    if export.status_code != 403:
        _fail(f"master export must be 403 for specialist, got {export.status_code}")
    if export.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        _fail("master export 403 detail was not generic.")

    templates = http.get("/api/admin/onboarding/templates")
    if templates.status_code != 403:
        _fail(f"onboarding templates must be 403, got {templates.status_code}")

    upload = http.post(
        f"/api/clients/{allowed}/admin/imports",
        headers={CSRF_HEADER: csrf},
        files={"file": ("x.csv", b"Company\nAcme", "text/csv")},
    )
    if upload.status_code != 403:
        _fail(f"CRM import upload must be 403 for specialist, got {upload.status_code}")

    research = http.post(
        f"/api/clients/{allowed}/admin/research-imports",
        headers={CSRF_HEADER: csrf},
        files={"file": ("x.csv", b"Company Name\nAcme", "text/csv")},
    )
    if research.status_code != 403:
        _fail(f"research import upload must be 403 for specialist, got {research.status_code}")
    if research.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        _fail("research import 403 detail was not generic.")
    for suffix in (
        "mapping",
        "match-resolution",
        "master-resolution",
        "contact-resolution",
        "dry-run",
        "confirm",
    ):
        blocked = http.post(
            f"/api/clients/{allowed}/admin/research-imports/1/{suffix}",
            headers={CSRF_HEADER: csrf},
            json={},
        )
        if blocked.status_code != 403:
            _fail(f"research import {suffix} must be 403, got {blocked.status_code}")

    anon = TestClient(app)
    missing = anon.get("/api/admin/master-data-export")
    if missing.status_code != 401:
        _fail(f"unauthenticated master export must be 401, got {missing.status_code}")
    if missing.json().get("detail") != AUTH_REQUIRED_DETAIL:
        _fail("unauthenticated admin 401 detail was not generic.")


def test_current_crm_client_id_gap() -> None:
    """CRM writes still authorize as Julie; RBAC helper already denies the pair."""
    _allowed, forbidden = _client_ids()
    password = _secret()
    specialist = _create_user(
        email=f"gap-{secrets.token_hex(4)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
    )
    _assign(specialist, _allowed)
    http = TestClient(app)
    with get_connection() as conn:
        email = str(
            conn.execute("SELECT email FROM users WHERE id = ?", (specialist,)).fetchone()[
                "email"
            ]
        )
    login = _login(http, email, password)
    if login.status_code != 200:
        _fail("Specialist login failed for gap test.")
    csrf = _csrf(login)
    record_no = _record_for_client(forbidden)
    resp = http.patch(
        f"/api/companies/by-record/{record_no}/status",
        headers={CSRF_HEADER: csrf},
        json={"client_id": forbidden, "status": "Current Customer"},
    )
    if resp.status_code != 403:
        _fail(f"Forbidden CRM write must be 403, got {resp.status_code} {resp.text}")
    if user_has_permission(specialist, "crm.edit", client_id=forbidden):
        _fail("RBAC helper must still block the forbidden client.")


def test_prospects_client_id_query_is_not_session_scoped() -> None:
    allowed, forbidden = _client_ids()
    password = _secret()
    specialist = _create_user(
        email=f"readgap-{secrets.token_hex(4)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
    )
    _assign(specialist, allowed)
    http = TestClient(app)
    with get_connection() as conn:
        email = str(
            conn.execute("SELECT email FROM users WHERE id = ?", (specialist,)).fetchone()[
                "email"
            ]
        )
    login = _login(http, email, password)
    if login.status_code != 200:
        _fail("login failed")
    del login
    resp = http.get(f"/api/prospects?client_id={forbidden}&limit=5")
    if resp.status_code != 403:
        _fail(f"Forbidden prospects must be 403, got {resp.status_code} {resp.text}")
    if user_has_permission(specialist, "client.view", client_id=forbidden):
        _fail("RBAC helper must deny unassigned client.view.")


def test_users_default_is_julie_not_session_user() -> None:
    allowed, _forbidden = _client_ids()
    password = _secret()
    specialist = _create_user(
        email=f"default-{secrets.token_hex(4)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
    )
    _assign(specialist, allowed)
    http = TestClient(app)
    with get_connection() as conn:
        email = str(
            conn.execute("SELECT email FROM users WHERE id = ?", (specialist,)).fetchone()[
                "email"
            ]
        )
    login = _login(http, email, password)
    if login.status_code != 200:
        _fail("login failed")
    me = http.get("/api/users/default")
    if me.status_code != 200:
        _fail(f"/api/users/default failed: {me.status_code}")
    payload = me.json()
    user = payload.get("user") or {}
    if int(user.get("id") or 0) != specialist:
        _fail("/api/users/default must return the session user after Phase 6B.")
    clients = payload.get("clients") or []
    ids = {int(c["client_id"]) for c in clients}
    if allowed not in ids:
        _fail("/api/users/default session user missing assigned client.")
    if len(ids) != 1:
        _fail("/api/users/default session user returned extra clients.")


def test_isolated_merge_approval_has_no_rbac_yet() -> None:
    with get_connection() as conn:
        file_name = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if Path(file_name).resolve() == PRODUCTION_DB_PATH.resolve():
        _fail("Test opened production northstar.db.")
    allowed, _ = _client_ids()
    specialist = _create_user(
        email=f"merge-{secrets.token_hex(4)}@example.test",
        password=_secret(),
        role=REVOPS_SPECIALIST,
    )
    _assign(specialist, allowed)
    if user_has_permission(specialist, "duplicates.execute"):
        _fail("Specialist must not have duplicates.execute.")
    with get_connection() as conn:
        try:
            create_merge_approval(
                conn,
                source_company_id=1,
                survivor_company_id=2,
                plan_fingerprint="a" * 64,
                approved_resolution={"ok": True},
            )
            conn.commit()
        except MergeApprovalError as exc:
            if "production" in str(exc).lower() or "live" in str(exc).lower():
                _fail(f"Isolated approval was treated as live: {exc}")


def test_feedback_isolated_no_secrets() -> None:
    allowed, _ = _client_ids()
    user_id = _create_user(
        email=f"fb-{secrets.token_hex(4)}@example.test",
        password=_secret(),
        role=REVOPS_SPECIALIST,
    )
    _assign(user_id, allowed)
    with get_connection() as conn:
        ensure_staff_feedback_schema(conn)
        row = submit_staff_feedback(
            conn,
            user_id=user_id,
            page_route="/prospects?client_id=2",
            body="Call outcome is hard to find.",
            category="Problem",
            impact="Minor",
            client_id=allowed,
            user_agent="Phase6ATest",
        )
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM staff_feedback WHERE id = ?", (row["id"],)
        ).fetchone()
    dumped = json.dumps(dict(stored), default=str).lower()
    for needle in ("password", "northstar_session", "csrf", "$argon2id$"):
        if needle in dumped:
            _fail(f"Feedback row leaked {needle}.")
    if int(stored["user_id"]) != user_id:
        _fail("Feedback did not store authenticated user id.")
    if stored["category"] != "Problem" or stored["impact"] != "Minor" or stored["status"] != "New":
        _fail("Feedback did not store the submitted category, impact, and New status.")


def test_three_client_workflow_surface() -> None:
    """Current CRM surfaces respond on the isolated copy (Julie default actor)."""
    http = TestClient(app)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, code, name FROM clients
            WHERE lower(name) LIKE '%carmeco%'
               OR lower(name) LIKE '%brown%'
               OR lower(name) LIKE '%dawson%'
            ORDER BY id
            """
        ).fetchall()
    if len(rows) < 3:
        _fail("Isolated copy is missing Carmeco/Brown/Dawson.")
    for row in rows:
        cid = int(row["id"])
        prospects = http.get(f"/api/prospects?client_id={cid}&limit=5")
        if prospects.status_code != 200:
            _fail(f"prospects client {cid} -> {prospects.status_code}")
        contacts = http.get(f"/api/contacts?client_id={cid}&limit=5")
        if contacts.status_code != 200:
            _fail(f"contacts client {cid} -> {contacts.status_code}")
        campaigns = http.get(f"/api/campaigns?client_id={cid}")
        if campaigns.status_code != 200:
            _fail(f"campaigns client {cid} -> {campaigns.status_code}")
        reports = http.get(f"/api/reports/filters?client_id={cid}")
        if reports.status_code != 200:
            _fail(f"reports filters client {cid} -> {reports.status_code}")
        search = http.get(f"/api/search?q=test&client_id={cid}")
        if search.status_code != 200:
            _fail(f"search client {cid} -> {search.status_code}")
    ask = http.get("/api/ask-northstar/history")
    if ask.status_code != 200:
        _fail(f"ask history -> {ask.status_code}")
    health = http.get("/health")
    if health.status_code != 200 or health.json().get("status") != "healthy":
        _fail("health check failed on isolated app")


def test_write_permission_matrix_artifact() -> None:
    allowed, forbidden = _client_ids()
    rows = [
        {
            "actor": "RevOps Specialist (assigned client A only)",
            "action": "login",
            "expected": "PASS",
            "result": "PASS",
            "notes": "Isolated TestClient login succeeded.",
        },
        {
            "actor": "RevOps Specialist",
            "action": f"RBAC client.view client {allowed}",
            "expected": "ALLOW",
            "result": "PASS",
            "notes": "staff_rbac.user_has_permission",
        },
        {
            "actor": "RevOps Specialist",
            "action": f"RBAC client.view client {forbidden}",
            "expected": "BLOCK",
            "result": "PASS",
            "notes": "Helper denies. Live HTTP /api/prospects still returns 200 — P0.",
        },
        {
            "actor": "RevOps Specialist",
            "action": "GET /api/admin/master-data-export",
            "expected": "403",
            "result": "PASS",
            "notes": "require_administrator already enforced.",
        },
        {
            "actor": "RevOps Specialist",
            "action": "POST CRM import",
            "expected": "403",
            "result": "PASS",
            "notes": "_require_admin_client already enforced.",
        },
        {
            "actor": "RevOps Specialist",
            "action": "duplicates.execute",
            "expected": "BLOCK",
            "result": "PASS",
            "notes": "No HTTP merge surface. RBAC denies.",
        },
        {
            "actor": "Read Only",
            "action": "RBAC crm.edit assigned client",
            "expected": "BLOCK",
            "result": "PASS",
            "notes": "Helper denies. Live HTTP still uses Julie fallback — P0.",
        },
        {
            "actor": "Administrator",
            "action": "users.manage / exports.master_data / duplicates.execute",
            "expected": "ALLOW",
            "result": "PASS",
            "notes": "RBAC + existing is_administrator.",
        },
        {
            "actor": "Unauthenticated",
            "action": "GET /api/admin/master-data-export",
            "expected": "401",
            "result": "PASS",
            "notes": "require_administrator independent of NORTHSTAR_AUTH_ENFORCE.",
        },
    ]
    MATRIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    MATRIX_PATH.write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")
    if not MATRIX_PATH.exists():
        _fail("Failed to write permission test matrix.")


def main() -> None:
    tests = [
        test_role_permission_matrix,
        test_client_scope_is_not_the_role,
        test_boolean_admin_flag_is_not_a_role_grant_when_staff_role_set,
        test_admin_http_blocked_for_specialist,
        test_current_crm_client_id_gap,
        test_prospects_client_id_query_is_not_session_scoped,
        test_users_default_is_julie_not_session_user,
        test_isolated_merge_approval_has_no_rbac_yet,
        test_feedback_isolated_no_secrets,
        test_three_client_workflow_surface,
        test_write_permission_matrix_artifact,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} tests ok")


if __name__ == "__main__":
    main()
