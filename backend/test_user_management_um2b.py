"""UM-2B — audited non-admin user creation.

Uses an isolated testdb copy. Never creates Robert, Tyler, Todd, or any
other user in production northstar.db.

Future UM-2C note: an active is_administrator row is not proof the person
can sign in. NorthStar Admin is an administrator with no password. This
phase does not demote or deactivate anyone.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from unittest.mock import patch

import testdb  # noqa: F401 — isolates NORTHSTAR_DB_PATH before app imports

from fastapi.testclient import TestClient

from auth_http import ADMIN_REQUIRED_DETAIL, AUTH_REQUIRED_DETAIL, CSRF_HEADER
from auth_passwords import verify_password
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from staff_rbac import REVOPS_SPECIALIST

PILOT_EMAILS = ("robertk@n-star.us", "tylers@n-star.us", "toddw@n-star.us")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _password() -> str:
    return f"NsTest9{secrets.token_hex(4)}a"


def _email() -> str:
    return f"um2b.{secrets.token_hex(6)}@n-star.us"


def _client() -> TestClient:
    return TestClient(app)


def _client_id() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        _fail("Isolated testdb needs a client.")
    return int(row["id"])


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _create_admin(password: str) -> tuple[int, str]:
    from auth_passwords import hash_password
    from staff_rbac import ensure_staff_role_schema

    email = f"um2b.admin.{secrets.token_hex(4)}@n-star.us"
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        ensure_staff_role_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, 'UM2B Admin', 1, 1, 1, ?, 0, '', '')
            """,
            (email, digest),
        )
        user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    return user_id, email


def _delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_admin_events WHERE target_user_id = ? OR actor_user_id = ?", (user_id, user_id))
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def _login(http: TestClient, email: str, password: str) -> str:
    response = http.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        _fail(f"login failed {response.status_code} {response.text}")
    return str(response.json().get("csrf_token") or "")


def _post(http: TestClient, csrf: str, body: dict):
    return http.post("/api/admin/users", json=body, headers={CSRF_HEADER: csrf})


def _user_by_email(email: str):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE lower(email) = ?",
            (email.lower(),),
        ).fetchone()


def _assert_safe_payload(payload: object, password: str) -> None:
    dumped = json.dumps(payload)
    lowered_keys: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered_keys.append(str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    for key in lowered_keys:
        if key in {"password", "passwords", "password_hash"} or "csrf" in key or key.endswith("_token"):
            _fail(f"Response included {key}.")
    if password and password in dumped:
        _fail("Response included the plaintext password.")
    if "$argon2" in dumped.lower() or "password_hash" in dumped:
        _fail("Response included a password hash.")


def _crm_fingerprint() -> list[tuple]:
    with get_connection() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                """
                SELECT id, assigned_user_id FROM client_company_relationships ORDER BY id
                """
            ).fetchall()
        ] + [
            tuple(row)
            for row in conn.execute(
                "SELECT id, assigned_user_id FROM contact_client_workflows ORDER BY id"
            ).fetchall()
        ] + [
            tuple(row)
            for row in conn.execute(
                "SELECT id, assigned_user_id, completion_status FROM work_queue_items ORDER BY id"
            ).fetchall()
        ]


def test_unauthenticated_rejected() -> None:
    response = _client().post(
        "/api/admin/users",
        json={
            "full_name": "No Session",
            "email": _email(),
            "staff_role": REVOPS_SPECIALIST,
            "client_ids": [1],
            "password": _password(),
        },
    )
    if response.status_code != 401:
        _fail(f"Unauthenticated create must be 401, got {response.status_code}.")
    if response.json().get("detail") != AUTH_REQUIRED_DETAIL:
        _fail("Unauthenticated create detail was not generic.")


def test_non_admin_rejected() -> None:
    from auth_passwords import hash_password
    from staff_rbac import ensure_staff_role_schema

    password = _password()
    email = _email()
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        ensure_staff_role_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, 'UM2B Specialist', 0, 1, 1, ?, 0, '', ?)
            """,
            (email, digest, REVOPS_SPECIALIST),
        )
        user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    http = _client()
    try:
        csrf = _login(http, email, password)
        response = _post(
            http,
            csrf,
            {
                "full_name": "Blocked Create",
                "email": _email(),
                "staff_role": REVOPS_SPECIALIST,
                "client_ids": [_client_id()],
                "password": _password(),
            },
        )
        if response.status_code != 403:
            _fail(f"Specialist create must be 403, got {response.status_code}.")
        if response.json().get("detail") != ADMIN_REQUIRED_DETAIL:
            _fail("Specialist create detail was not the administrator denial.")
    finally:
        _delete_user(user_id)


def test_administrator_can_create_valid_user() -> None:
    password = _password()
    admin_id, admin_email = _create_admin(password)
    email = f"  {_email().upper()}  "
    client_a, client_b = _client_ids()
    before_crm = _crm_fingerprint()
    http = _client()
    created_id = 0
    try:
        csrf = _login(http, admin_email, password)
        response = _post(
            http,
            csrf,
            {
                "full_name": "  Ada   Lovelace  ",
                "email": email,
                "staff_role": "revops_specialist",
                "client_ids": [client_a, client_a, client_b],
                "password": password,
                "is_administrator": True,
            },
        )
        if response.status_code != 200:
            _fail(f"Create failed: {response.status_code} {response.text}")
        body = response.json()
        created_id = int(body["id"])
        if body.get("email") != email.strip().lower():
            _fail(f"Email was not normalized: {body.get('email')}")
        if body.get("full_name") != "Ada Lovelace":
            _fail("Name was not trimmed.")
        if body.get("is_administrator") is not False:
            _fail("Create accepted administrator status from the request.")
        if body.get("is_internal_northstar") is not True:
            _fail("New user was not internal.")
        if body.get("staff_role") != REVOPS_SPECIALIST:
            _fail("Role was not stored as revops_specialist.")
        if body.get("active") is not True:
            _fail("Omitted-equivalent active true was not applied.")
        if body.get("has_password") is not True:
            _fail("Create response did not report has_password.")
        client_ids = sorted(int(item["client_id"]) for item in body.get("assignments") or [])
        if client_ids != sorted({client_a, client_b}):
            _fail(f"Duplicate client ids were not collapsed: {client_ids}")
        _assert_safe_payload(body, password)
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (created_id,)).fetchone()
            if not str(row["password_hash"]).startswith("$argon2"):
                _fail("Password was not stored as an Argon2 hash.")
            if password in str(row["password_hash"]):
                _fail("Plaintext password was stored in password_hash.")
            if not verify_password(password, str(row["password_hash"])):
                _fail("Stored hash does not verify.")
            if int(row["failed_login_count"] or 0) != 0 or str(row["locked_until"] or "") != "":
                _fail("Lockout fields were not initialized clear.")
            if not str(row["password_updated_at"] or "").strip():
                _fail("password_updated_at was not set.")
            event = conn.execute(
                """
                SELECT * FROM staff_admin_events
                WHERE target_user_id = ? AND event_type = 'user_created'
                """,
                (created_id,),
            ).fetchone()
            if event is None:
                _fail("user_created audit event was not written.")
            if int(event["actor_user_id"]) != admin_id or int(event["target_user_id"]) != created_id:
                _fail("Audit actor or target was wrong.")
            detail = json.loads(event["detail_json"])
            if password in event["detail_json"] or "$argon2" in event["detail_json"].lower():
                _fail("Audit event stored a password or hash.")
            if detail.get("email") != email.strip().lower() or detail.get("is_administrator") is not False:
                _fail(f"Audit detail was incomplete: {detail}")
            assignments = conn.execute(
                "SELECT client_id, role, active FROM user_client_assignments WHERE user_id = ? ORDER BY client_id",
                (created_id,),
            ).fetchall()
            if len(assignments) != 2:
                _fail("Expected two assignment rows.")
            if any(str(item["role"]) != REVOPS_SPECIALIST or not item["active"] for item in assignments):
                _fail("Assignment role or active flag was wrong.")
        if _crm_fingerprint() != before_crm:
            _fail("Create changed CRM assigned_user_id rows.")
    finally:
        if created_id:
            _delete_user(created_id)
        _delete_user(admin_id)


def test_validation_rejections() -> None:
    password = _password()
    admin_id, admin_email = _create_admin(password)
    client_id = _client_id()
    first = _email()
    http = _client()
    created: list[int] = []
    try:
        csrf = _login(http, admin_email, password)
        ok = _post(
            http,
            csrf,
            {
                "full_name": "First User",
                "email": first,
                "staff_role": "read_only",
                "client_ids": [client_id],
                "password": password,
            },
        )
        if ok.status_code != 200:
            _fail(f"Seed user failed: {ok.text}")
        created.append(int(ok.json()["id"]))

        cases = [
            ("domain", {"full_name": "Outside", "email": "person@example.com", "staff_role": "read_only", "client_ids": [client_id], "password": password}, 400),
            ("duplicate", {"full_name": "First User", "email": first.upper(), "staff_role": "read_only", "client_ids": [client_id], "password": password}, 409),
            ("weak", {"full_name": "Weak User", "email": _email(), "staff_role": "read_only", "client_ids": [client_id], "password": "short"}, 400),
            ("invalid-role", {"full_name": "Bad Role", "email": _email(), "staff_role": "intern", "client_ids": [client_id], "password": password}, 400),
            ("admin-role", {"full_name": "Admin Role", "email": _email(), "staff_role": "system_administrator", "client_ids": [client_id], "password": password}, 400),
            ("ops-role", {"full_name": "Ops Role", "email": _email(), "staff_role": "operations_admin", "client_ids": [client_id], "password": password}, 400),
            ("no-clients", {"full_name": "No Clients", "email": _email(), "staff_role": "read_only", "client_ids": [], "password": password}, 400),
            ("missing-client", {"full_name": "Missing Client", "email": _email(), "staff_role": "read_only", "client_ids": [999999999], "password": password}, 400),
        ]
        for label, body, status in cases:
            response = _post(http, csrf, body)
            if response.status_code != status:
                _fail(f"{label} expected {status}, got {response.status_code} {response.text}")
            _assert_safe_payload(response.json(), str(body.get("password") or ""))
            if _user_by_email(str(body["email"])) is not None and label != "duplicate":
                _fail(f"{label} created a user.")
    finally:
        for user_id in created:
            _delete_user(user_id)
        _delete_user(admin_id)


def test_inactive_creation_stays_inactive() -> None:
    password = _password()
    admin_id, admin_email = _create_admin(password)
    http = _client()
    created_id = 0
    try:
        csrf = _login(http, admin_email, password)
        response = _post(
            http,
            csrf,
            {
                "full_name": "Inactive Hire",
                "email": _email(),
                "staff_role": "appointment_setter",
                "client_ids": [_client_id()],
                "active": False,
                "password": password,
            },
        )
        if response.status_code != 200:
            _fail(response.text)
        body = response.json()
        created_id = int(body["id"])
        if body.get("active") is not False or body.get("login_status") != "inactive":
            _fail(f"Inactive create was {body.get('active')} / {body.get('login_status')}")
    finally:
        if created_id:
            _delete_user(created_id)
        _delete_user(admin_id)


def test_create_rolls_back_when_audit_or_assignment_fails() -> None:
    password = _password()
    admin_id, admin_email = _create_admin(password)
    http = _client()
    try:
        csrf = _login(http, admin_email, password)
        email = _email()
        body = {
            "full_name": "Rollback Audit",
            "email": email,
            "staff_role": "revops_manager",
            "client_ids": [_client_id()],
            "password": password,
        }
        with patch("user_management._record_user_created", side_effect=RuntimeError("audit failed")):
            try:
                _post(http, csrf, body)
            except RuntimeError as exc:
                if "audit failed" not in str(exc):
                    raise
        if _user_by_email(email) is not None:
            _fail("Audit failure left a user row.")
        with get_connection() as conn:
            event = conn.execute(
                "SELECT COUNT(*) FROM staff_admin_events WHERE detail_json LIKE ?",
                (f"%{email}%",),
            ).fetchone()[0]
            if int(event) != 0:
                _fail("Audit failure left an audit row.")

        email = _email()
        body["email"] = email
        body["full_name"] = "Rollback Assign"
        with patch("user_management._insert_assignments", side_effect=RuntimeError("assignment failed")):
            try:
                _post(http, csrf, body)
            except RuntimeError as exc:
                if "assignment failed" not in str(exc):
                    raise
        if _user_by_email(email) is not None:
            _fail("Assignment failure left a user row.")
    finally:
        _delete_user(admin_id)


def test_tests_do_not_create_live_users() -> None:
    conn = sqlite3.connect(f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        emails = [str(row["email"]).lower() for row in conn.execute("SELECT email FROM users")]
        if len(emails) != 3:
            _fail(f"Live users count changed: {len(emails)}")
        for pilot in PILOT_EMAILS:
            if pilot in emails:
                _fail(f"Live database contains {pilot}")
        if any(email.startswith("um2b.") for email in emails):
            _fail("A UM-2B test user was written to the live database.")
    finally:
        conn.close()


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    tests = [
        test_unauthenticated_rejected,
        test_non_admin_rejected,
        test_administrator_can_create_valid_user,
        test_validation_rejections,
        test_inactive_creation_stays_inactive,
        test_create_rolls_back_when_audit_or_assignment_fails,
        test_tests_do_not_create_live_users,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
