"""UM-2A — read-only administrator user list and detail.

Uses an isolated testdb copy. Never writes production northstar.db and never
stores a real staff password.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import testdb  # noqa: F401 — isolates NORTHSTAR_DB_PATH before app imports

from fastapi.testclient import TestClient

from auth_http import ADMIN_REQUIRED_DETAIL, AUTH_REQUIRED_DETAIL
from auth_passwords import hash_password
from db import get_connection
from main import app
from staff_rbac import REVOPS_SPECIALIST, ensure_staff_role_schema

FORBIDDEN_SECRET_KEYS = {
    "token",
    "token_hash",
    "csrf",
    "csrf_secret",
    "csrf_token",
    "session",
    "session_token",
    "raw_token",
    "northstar_session",
}


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _password() -> str:
    return f"NsTest9{secrets.token_hex(8)}"


def _email() -> str:
    return f"um2a-{secrets.token_hex(6)}@example.test"


def _client() -> TestClient:
    return TestClient(app)


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _create_user(
    *,
    email: str,
    full_name: str,
    password: str = "",
    administrator: int = 0,
    active: int = 1,
    staff_role: str = "",
    locked_until: str = "",
    password_hash: str | None = None,
) -> int:
    digest = password_hash if password_hash is not None else (
        hash_password(password, email=email) if password else ""
    )
    with get_connection() as conn:
        ensure_staff_role_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, password_updated_at, failed_login_count, locked_until,
                staff_role
            ) VALUES (?, ?, ?, 1, ?, ?, '', 0, ?, ?)
            """,
            (
                email,
                full_name,
                int(administrator),
                int(active),
                digest,
                locked_until,
                staff_role,
            ),
        )
        user_id = int(
            conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        )
        conn.commit()
    return user_id


def _assign(user_id: int, client_id: int, *, active: int = 1) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_client_assignments (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, '', ?, datetime('now'))
            """,
            (int(user_id), int(client_id), int(active)),
        )
        conn.commit()


def _delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (int(user_id),))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (int(user_id),))
        conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        conn.commit()


def _login(http: TestClient, email: str, password: str):
    response = http.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        _fail(f"login failed {response.status_code} {response.text}")
    return response


def _find_user(payload: dict, user_id: int) -> dict:
    users = payload.get("users")
    if not isinstance(users, list):
        _fail("List response did not include users.")
    for row in users:
        if isinstance(row, dict) and int(row.get("id") or 0) == int(user_id):
            return row
    _fail(f"User {user_id} missing from list.")
    return {}


def _walk_keys(payload: object):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_keys(item)


def _assert_no_password_hash(payload: object) -> None:
    for key in _walk_keys(payload):
        if key == "password_hash" or "password_hash" in key:
            _fail(f"Response included {key}.")
    dumped = json.dumps(payload).lower()
    if "$argon2" in dumped:
        _fail("Response included an Argon2 hash.")


def _assert_no_password_values(payload: object, password: str) -> None:
    for key in _walk_keys(payload):
        if key in {"password", "passwords"}:
            _fail(f"Response included {key}.")
    if password and password in json.dumps(payload):
        _fail("Response included the plaintext password.")


def _assert_no_session_secrets(payload: object, csrf: str = "") -> None:
    for key in _walk_keys(payload):
        if key in FORBIDDEN_SECRET_KEYS or "csrf" in key or key.endswith("_token"):
            _fail(f"Response included {key}.")
    dumped = json.dumps(payload)
    if csrf and csrf in dumped:
        _fail("Response included the CSRF secret.")
    if "token_hash" in dumped or "csrf_secret" in dumped:
        _fail("Response included a session secret field.")


def _fingerprint() -> tuple:
    statements = (
        "SELECT id, email, full_name, is_administrator, is_internal_northstar, active, password_hash, password_updated_at, failed_login_count, locked_until, staff_role FROM users ORDER BY id",
        "SELECT id, user_id, client_id, role, active FROM user_client_assignments ORDER BY id",
        "SELECT id, user_id, token_hash, csrf_secret, revoked_at, last_seen_at, expires_at FROM staff_sessions ORDER BY id",
        "SELECT id, assigned_user_id FROM client_company_relationships ORDER BY id",
        "SELECT id, assigned_user_id FROM contact_client_workflows ORDER BY id",
        "SELECT id, assigned_user_id, completion_status FROM work_queue_items ORDER BY id",
        "SELECT COUNT(*) FROM companies",
        "SELECT COUNT(*) FROM contacts",
        "SELECT COUNT(*) FROM client_company_relationships",
        "SELECT COUNT(*) FROM merge_execution_approvals",
        "SELECT COUNT(*) FROM company_merge_history",
        "SELECT COUNT(*) FROM company_merge_plan_decisions",
        "SELECT pair_key, plan_state, survivor_company_id FROM company_merge_plans WHERE pair_key = '45:97'",
        "SELECT exception_key, chosen_resolution, superseded_at FROM company_merge_plan_decisions WHERE pair_key = '45:97' ORDER BY id",
    )
    with get_connection() as conn:
        snapshot = []
        for sql in statements:
            snapshot.append([tuple(row) for row in conn.execute(sql).fetchall()])
        audit = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'staff_admin_events'"
        ).fetchone()
        snapshot.append(audit is not None)
    return tuple(snapshot)


def test_unauthenticated_access_is_rejected() -> None:
    http = _client()
    listing = http.get("/api/admin/users")
    if listing.status_code != 401:
        _fail(f"List without a session must be 401, got {listing.status_code}.")
    if listing.json().get("detail") != AUTH_REQUIRED_DETAIL:
        _fail("Unauthenticated list detail was not generic.")
    detail = http.get("/api/admin/users/1")
    if detail.status_code != 401:
        _fail(f"Detail without a session must be 401, got {detail.status_code}.")
    _assert_no_password_hash(listing.json())
    _assert_no_session_secrets(listing.json())


def test_administrator_can_list_users() -> None:
    password = _password()
    email = _email()
    user_id = _create_user(
        email=email,
        full_name="UM2A List Admin",
        password=password,
        administrator=1,
        staff_role="",
    )
    http = _client()
    try:
        _login(http, email, password)
        response = http.get("/api/admin/users")
        if response.status_code != 200:
            _fail(f"Administrator list failed: {response.status_code} {response.text}")
        row = _find_user(response.json(), user_id)
        if row.get("full_name") != "UM2A List Admin":
            _fail("List did not return the administrator name.")
        if row.get("email") != email:
            _fail("List did not return the administrator email.")
        if row.get("staff_role") != "system_administrator":
            _fail("Empty administrator staff_role should surface the canonical role.")
    finally:
        _delete_user(user_id)


def test_administrator_can_open_user_detail() -> None:
    password = _password()
    email = _email()
    user_id = _create_user(
        email=email,
        full_name="UM2A Detail Admin",
        password=password,
        administrator=1,
    )
    http = _client()
    try:
        _login(http, email, password)
        response = http.get(f"/api/admin/users/{user_id}")
        if response.status_code != 200:
            _fail(f"Administrator detail failed: {response.status_code} {response.text}")
        body = response.json()
        if body.get("id") != user_id or body.get("full_name") != "UM2A Detail Admin":
            _fail("Detail did not return the requested user.")
        if "assignments" not in body or "crm_ownership" not in body:
            _fail("Detail omitted assignments or CRM ownership counts.")
    finally:
        _delete_user(user_id)


def test_specialist_receives_403() -> None:
    password = _password()
    email = _email()
    allowed, _other = _client_ids()
    user_id = _create_user(
        email=email,
        full_name="UM2A Specialist",
        password=password,
        administrator=0,
        staff_role=REVOPS_SPECIALIST,
    )
    _assign(user_id, allowed, active=1)
    http = _client()
    try:
        _login(http, email, password)
        listing = http.get("/api/admin/users")
        detail = http.get(f"/api/admin/users/{user_id}")
        for response, label in ((listing, "list"), (detail, "detail")):
            if response.status_code != 403:
                _fail(f"Specialist {label} must be 403, got {response.status_code}.")
            if response.json().get("detail") != ADMIN_REQUIRED_DETAIL:
                _fail(f"Specialist {label} detail was not the administrator denial.")
            _assert_no_password_hash(response.json())
    finally:
        _delete_user(user_id)


def test_unknown_user_receives_404() -> None:
    password = _password()
    email = _email()
    user_id = _create_user(
        email=email,
        full_name="UM2A Missing Admin",
        password=password,
        administrator=1,
    )
    http = _client()
    try:
        _login(http, email, password)
        response = http.get("/api/admin/users/999999999")
        if response.status_code != 404:
            _fail(f"Unknown user must be 404, got {response.status_code}.")
        if response.json().get("detail") != "User not found.":
            _fail("Unknown user detail was not generic.")
    finally:
        _delete_user(user_id)


def test_administrator_reports_all_clients() -> None:
    password = _password()
    email = _email()
    allowed, _other = _client_ids()
    user_id = _create_user(
        email=email,
        full_name="UM2A Scope Admin",
        password=password,
        administrator=1,
        staff_role="",
    )
    _assign(user_id, allowed, active=1)
    http = _client()
    try:
        _login(http, email, password)
        row = _find_user(http.get("/api/admin/users").json(), user_id)
        if row.get("is_administrator") is not True:
            _fail("Administrator flag was not reported.")
        if row.get("access_scope") != "all_clients":
            _fail("Administrator access_scope must be all_clients.")
        detail = http.get(f"/api/admin/users/{user_id}").json()
        if detail.get("access_scope") != "all_clients":
            _fail("Administrator detail access_scope must be all_clients.")
    finally:
        _delete_user(user_id)


def test_specialist_reports_only_active_assigned_clients() -> None:
    password = _password()
    email = _email()
    admin_email = _email()
    active_client, inactive_client = _client_ids()
    admin_id = _create_user(
        email=admin_email,
        full_name="UM2A Scope Reader",
        password=password,
        administrator=1,
    )
    user_id = _create_user(
        email=email,
        full_name="UM2A Scoped Specialist",
        password=password,
        administrator=0,
        staff_role=REVOPS_SPECIALIST,
    )
    _assign(user_id, active_client, active=1)
    _assign(user_id, inactive_client, active=0)
    http = _client()
    try:
        _login(http, admin_email, password)
        row = _find_user(http.get("/api/admin/users").json(), user_id)
        if row.get("access_scope") != "assigned":
            _fail("Specialist access_scope must be assigned.")
        client_ids = [int(item["client_id"]) for item in row.get("clients") or []]
        if client_ids != [active_client]:
            _fail(f"Specialist list clients were {client_ids}, expected {[active_client]}.")
        detail = http.get(f"/api/admin/users/{user_id}").json()
        states = {
            int(item["client_id"]): bool(item["active"])
            for item in detail.get("assignments") or []
        }
        if states.get(active_client) is not True or states.get(inactive_client) is not False:
            _fail(f"Detail assignments did not preserve active state: {states}.")
        if "client_code" not in detail["assignments"][0] or "client_name" not in detail["assignments"][0]:
            _fail("Assignment rows omitted client code or name.")
    finally:
        _delete_user(user_id)
        _delete_user(admin_id)


def test_login_status_derivation() -> None:
    password = _password()
    digest = hash_password(password, email="um2a-status@example.test")
    admin_email = _email()
    admin_id = _create_user(
        email=admin_email,
        full_name="UM2A Status Admin",
        password_hash=digest,
        administrator=1,
    )
    future = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cases = {
        "inactive": _create_user(
            email=_email(),
            full_name="UM2A Inactive",
            password_hash=digest,
            active=0,
            locked_until=future,
        ),
        "no_password": _create_user(
            email=_email(),
            full_name="UM2A No Password",
            password_hash="",
            active=1,
        ),
        "locked": _create_user(
            email=_email(),
            full_name="UM2A Locked",
            password_hash=digest,
            active=1,
            locked_until=future,
        ),
        "can_sign_in": _create_user(
            email=_email(),
            full_name="UM2A Ready",
            password_hash=digest,
            active=1,
        ),
    }
    http = _client()
    try:
        _login(http, admin_email, password)
        payload = http.get("/api/admin/users").json()
        for expected, user_id in cases.items():
            row = _find_user(payload, user_id)
            if row.get("login_status") != expected:
                _fail(f"login_status for {expected} was {row.get('login_status')}.")
    finally:
        for user_id in cases.values():
            _delete_user(user_id)
        _delete_user(admin_id)


def test_detail_ownership_counts() -> None:
    password = _password()
    admin_email = _email()
    admin_id = _create_user(
        email=admin_email,
        full_name="UM2A Count Admin",
        password=password,
        administrator=1,
    )
    user_id = _create_user(
        email=_email(),
        full_name="UM2A Count Specialist",
        password=password,
        administrator=0,
        staff_role=REVOPS_SPECIALIST,
    )
    created_queue: list[int] = []
    workflow_insert_id: int | None = None
    previous_workflow_assignee: int | None = None
    workflow_existed = False
    relationship_id = 0
    previous_ccr_assignee = None
    contact_id = 0
    client_id = 0
    with get_connection() as conn:
        source = conn.execute(
            """
            SELECT ccr.id AS relationship_id, ccr.client_id, ccr.company_id,
                   ccr.assigned_user_id AS previous_assigned, ct.id AS contact_id
            FROM client_company_relationships ccr
            JOIN contacts ct ON ct.company_id = ccr.company_id
            ORDER BY ccr.id
            LIMIT 1
            """
        ).fetchone()
        if source is None:
            _fail("Isolated testdb needs a company relationship with a contact.")
        relationship_id = int(source["relationship_id"])
        client_id = int(source["client_id"])
        company_id = int(source["company_id"])
        contact_id = int(source["contact_id"])
        previous_ccr_assignee = source["previous_assigned"]
        conn.execute(
            "UPDATE client_company_relationships SET assigned_user_id = ? WHERE id = ?",
            (user_id, relationship_id),
        )
        existing_workflow = conn.execute(
            """
            SELECT id, assigned_user_id
            FROM contact_client_workflows
            WHERE contact_id = ? AND client_id = ?
            """,
            (contact_id, client_id),
        ).fetchone()
        if existing_workflow is None:
            conn.execute(
                """
                INSERT INTO contact_client_workflows (
                    contact_id, client_id, relationship_id, assigned_user_id, status
                ) VALUES (?, ?, ?, ?, '')
                """,
                (contact_id, client_id, relationship_id, user_id),
            )
            workflow_insert_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            workflow_existed = True
            previous_workflow_assignee = existing_workflow["assigned_user_id"]
            conn.execute(
                "UPDATE contact_client_workflows SET assigned_user_id = ? WHERE id = ?",
                (user_id, int(existing_workflow["id"])),
            )
        for status in ("open", "open", "completed"):
            conn.execute(
                """
                INSERT INTO work_queue_items (
                    client_id, company_id, relationship_id, contact_id, action_type,
                    due_date, assigned_user_id, completion_status
                ) VALUES (?, ?, ?, ?, 'Follow-Up', '2026-09-27', ?, ?)
                """,
                (client_id, company_id, relationship_id, contact_id, user_id, status),
            )
            created_queue.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
        conn.commit()
    http = _client()
    try:
        _login(http, admin_email, password)
        body = http.get(f"/api/admin/users/{user_id}").json()
        counts = body.get("crm_ownership") or {}
        if counts.get("client_company_relationships") != 1:
            _fail(f"CCR ownership count was {counts.get('client_company_relationships')}.")
        if counts.get("contact_client_workflows") != 1:
            _fail(f"Workflow ownership count was {counts.get('contact_client_workflows')}.")
        if counts.get("open_work_queue_items") != 2:
            _fail(f"Open work queue count was {counts.get('open_work_queue_items')}.")
    finally:
        with get_connection() as conn:
            for item_id in created_queue:
                conn.execute("DELETE FROM work_queue_items WHERE id = ?", (item_id,))
            if workflow_insert_id is not None:
                conn.execute(
                    "DELETE FROM contact_client_workflows WHERE id = ?",
                    (workflow_insert_id,),
                )
            elif workflow_existed:
                conn.execute(
                    """
                    UPDATE contact_client_workflows
                    SET assigned_user_id = ?
                    WHERE contact_id = ? AND client_id = ?
                    """,
                    (previous_workflow_assignee, contact_id, client_id),
                )
            if relationship_id:
                conn.execute(
                    "UPDATE client_company_relationships SET assigned_user_id = ? WHERE id = ?",
                    (previous_ccr_assignee, relationship_id),
                )
            conn.commit()
        _delete_user(user_id)
        _delete_user(admin_id)


def test_password_hash_absent() -> None:
    password = _password()
    email = _email()
    user_id = _create_user(
        email=email,
        full_name="UM2A Hash Hidden",
        password=password,
        administrator=1,
    )
    http = _client()
    try:
        _login(http, email, password)
        listing = http.get("/api/admin/users").json()
        detail = http.get(f"/api/admin/users/{user_id}").json()
        _assert_no_password_hash(listing)
        _assert_no_password_hash(detail)
    finally:
        _delete_user(user_id)


def test_passwords_absent() -> None:
    password = _password()
    email = _email()
    user_id = _create_user(
        email=email,
        full_name="UM2A Password Hidden",
        password=password,
        administrator=1,
    )
    http = _client()
    try:
        _login(http, email, password)
        listing = http.get("/api/admin/users").json()
        detail = http.get(f"/api/admin/users/{user_id}").json()
        _assert_no_password_values(listing, password)
        _assert_no_password_values(detail, password)
        if "password_updated_at" not in detail:
            _fail("password_updated_at may be returned and was missing.")
    finally:
        _delete_user(user_id)


def test_session_token_csrf_fields_absent() -> None:
    password = _password()
    email = _email()
    user_id = _create_user(
        email=email,
        full_name="UM2A Secret Hidden",
        password=password,
        administrator=1,
    )
    http = _client()
    try:
        login = _login(http, email, password)
        csrf = str(login.json().get("csrf_token") or "")
        listing = http.get("/api/admin/users").json()
        detail = http.get(f"/api/admin/users/{user_id}").json()
        _assert_no_session_secrets(listing, csrf)
        _assert_no_session_secrets(detail, csrf)
    finally:
        _delete_user(user_id)


def test_get_endpoints_do_not_mutate() -> None:
    password = _password()
    email = _email()
    user_id = _create_user(
        email=email,
        full_name="UM2A Read Only",
        password=password,
        administrator=1,
    )
    http = _client()
    try:
        _login(http, email, password)
        before = _fingerprint()
        listing = http.get("/api/admin/users")
        detail = http.get(f"/api/admin/users/{user_id}")
        if listing.status_code != 200 or detail.status_code != 200:
            _fail("Read endpoints failed during the mutation check.")
        after = _fingerprint()
        if before != after:
            _fail("GET /api/admin/users changed the isolated database.")
        if after[-1] is not False:
            _fail("staff_admin_events must not be created by the read endpoints.")
    finally:
        _delete_user(user_id)


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    tests = [
        test_unauthenticated_access_is_rejected,
        test_administrator_can_list_users,
        test_administrator_can_open_user_detail,
        test_specialist_receives_403,
        test_unknown_user_receives_404,
        test_administrator_reports_all_clients,
        test_specialist_reports_only_active_assigned_clients,
        test_login_status_derivation,
        test_detail_ownership_counts,
        test_password_hash_absent,
        test_passwords_absent,
        test_session_token_csrf_fields_absent,
        test_get_endpoints_do_not_mutate,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
