"""UM-2D — administrator password reset.

Every write uses the isolated testdb copy. This file does not change live
passwords, live users, or create Robert, Tyler, or Todd.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import testdb  # noqa: F401 — isolates NORTHSTAR_DB_PATH before app imports

from fastapi.testclient import TestClient

from auth_http import ADMIN_REQUIRED_DETAIL, AUTH_REQUIRED_DETAIL, CSRF_FAILED_DETAIL, CSRF_HEADER
from auth_passwords import hash_password, verify_password
from auth_sessions import create_staff_session, hash_session_token
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from staff_rbac import REVOPS_SPECIALIST, SYSTEM_ADMINISTRATOR

PILOT_EMAILS = ("robertk@n-star.us", "tylers@n-star.us", "toddw@n-star.us")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _password() -> str:
    return f"NsTest9{secrets.token_hex(4)}a"


def _email() -> str:
    return f"um2d.{secrets.token_hex(6)}@n-star.us"


def _client() -> TestClient:
    return TestClient(app)


def _insert_user(
    *,
    email: str,
    name: str,
    administrator: bool,
    active: bool = True,
    password: str = "",
    role: str,
    failed_login_count: int = 0,
    locked_until: str = "",
) -> int:
    digest = hash_password(password, email=email) if password else ""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, password_updated_at, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, ?, 1, ?, ?, '', ?, ?, ?)
            """,
            (
                email,
                name,
                1 if administrator else 0,
                1 if active else 0,
                digest,
                failed_login_count,
                locked_until,
                role,
            ),
        )
        user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    return user_id


def _delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM staff_admin_events WHERE target_user_id = ? OR actor_user_id = ?",
            (user_id, user_id),
        )
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def _login(http: TestClient, email: str, password: str) -> str:
    response = http.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        _fail(f"login failed {response.status_code} {response.text}")
    return str(response.json().get("csrf_token") or "")


def _reset(http: TestClient, csrf: str, user_id: int, password: str):
    return http.post(
        f"/api/admin/users/{user_id}/reset-password",
        json={"password": password},
        headers={CSRF_HEADER: csrf},
    )


def _row(user_id: int):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _events(user_id: int) -> list[tuple[str, str]]:
    with get_connection() as conn:
        return [
            (str(row["event_type"]), str(row["detail_json"]))
            for row in conn.execute(
                """
                SELECT event_type, detail_json
                FROM staff_admin_events
                WHERE target_user_id = ?
                ORDER BY id
                """,
                (user_id,),
            ).fetchall()
        ]


def _open_session(user_id: int) -> str:
    with get_connection() as conn:
        session = create_staff_session(conn, user_id=user_id)
        conn.commit()
    return str(session["token"])


def _session_revoked(token: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT revoked_at FROM staff_sessions WHERE token_hash = ?",
            (hash_session_token(token),),
        ).fetchone()
    if row is None:
        _fail("Expected a staff session row.")
    return bool(str(row["revoked_at"] or "").strip())


def _assignments(user_id: int) -> list[tuple]:
    with get_connection() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                """
                SELECT client_id, role, active
                FROM user_client_assignments
                WHERE user_id = ?
                ORDER BY client_id
                """,
                (user_id,),
            ).fetchall()
        ]


def _crm_fingerprint() -> list[tuple]:
    with get_connection() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT id, assigned_user_id FROM client_company_relationships ORDER BY id"
            ).fetchall()
        ] + [
            tuple(row)
            for row in conn.execute(
                "SELECT id, assigned_user_id FROM contact_client_workflows ORDER BY id"
            ).fetchall()
        ] + [
            tuple(row)
            for row in conn.execute(
                "SELECT id, assigned_user_id FROM work_queue_items ORDER BY id"
            ).fetchall()
        ]


def _assert_secret_free(payload: object, password: str) -> None:
    dumped = json.dumps(payload)
    if password and password in dumped:
        _fail("Response included the plaintext password.")
    if "$argon2" in dumped.lower() or "password_hash" in dumped:
        _fail("Response included a password hash.")
    if "csrf" in dumped.lower() or "token_hash" in dumped.lower():
        _fail("Response included a session secret.")


def _actor() -> tuple[TestClient, str, int]:
    password = _password()
    email = _email()
    user_id = _insert_user(
        email=email,
        name="UM2D Actor",
        administrator=True,
        password=password,
        role=SYSTEM_ADMINISTRATOR,
    )
    http = _client()
    csrf = _login(http, email, password)
    return http, csrf, user_id


def test_reset_active_user_replaces_password_and_revokes_sessions() -> None:
    http, csrf, actor_id = _actor()
    old_password = _password()
    new_password = _password()
    email = _email()
    locked_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    target_id = _insert_user(
        email=email,
        name="Reset Target",
        administrator=False,
        password=old_password,
        role=REVOPS_SPECIALIST,
        failed_login_count=4,
        locked_until=locked_until,
    )
    with get_connection() as conn:
        client_id = int(conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO user_client_assignments (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, ?, 1, '2026-09-27T00:00:00Z')
            """,
            (target_id, client_id, REVOPS_SPECIALIST),
        )
        rel = conn.execute(
            "SELECT id, assigned_user_id FROM client_company_relationships ORDER BY id LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE client_company_relationships SET assigned_user_id = ? WHERE id = ?",
            (target_id, int(rel["id"])),
        )
        conn.commit()
    before_assignments = _assignments(target_id)
    before_crm = _crm_fingerprint()
    tokens = [_open_session(target_id), _open_session(target_id)]
    before = _row(target_id)
    try:
        response = _reset(http, csrf, target_id, new_password)
        if response.status_code != 200:
            _fail(response.text)
        body = response.json()
        _assert_secret_free(body, new_password)
        _assert_secret_free(body, old_password)
        if body.get("has_password") is not True or int(body.get("sessions_revoked") or 0) != 2:
            _fail(f"Unexpected reset response: {body}")
        if not str(body.get("password_updated_at") or "").strip():
            _fail("password_updated_at was missing.")
        after = _row(target_id)
        if str(after["password_hash"]) == str(before["password_hash"]):
            _fail("Password hash did not change.")
        if not verify_password(new_password, str(after["password_hash"])):
            _fail("Stored hash does not match the new password.")
        if verify_password(old_password, str(after["password_hash"])):
            _fail("Old password still matches the stored hash.")
        if str(after["password_updated_at"]) == str(before["password_updated_at"]):
            _fail("password_updated_at did not change.")
        if int(after["failed_login_count"] or 0) != 0:
            _fail("failed_login_count was not reset.")
        if str(after["locked_until"] or "") != "":
            _fail("locked_until was not cleared to the empty string.")
        if int(after["active"]) != 1 or int(after["is_administrator"]) != 0:
            _fail("Reset changed active or administrator.")
        if after["staff_role"] != REVOPS_SPECIALIST or after["email"] != email or after["full_name"] != "Reset Target":
            _fail("Reset changed role, email, or name.")
        if _assignments(target_id) != before_assignments:
            _fail("Reset changed client assignments.")
        if _crm_fingerprint() != before_crm:
            _fail("Reset changed CRM assigned_user_id values.")
        if not all(_session_revoked(token) for token in tokens):
            _fail("Not every target session was revoked.")
        old_login = _client().post("/api/auth/login", json={"email": email, "password": old_password})
        if old_login.status_code == 200:
            _fail("Old password still authenticates.")
        new_login = _client().post("/api/auth/login", json={"email": email, "password": new_password})
        if new_login.status_code != 200:
            _fail("New password did not authenticate an active user.")
        kinds = [kind for kind, _detail in _events(target_id)]
        if kinds != ["password_reset", "sessions_revoked"]:
            _fail(f"Unexpected audit events: {kinds}")
        for _kind, detail in _events(target_id):
            if new_password in detail or old_password in detail or "$argon2" in detail.lower():
                _fail("Audit included a password or hash.")
            if "password_hash" in detail or "csrf" in detail.lower() or "token" in detail.lower():
                _fail("Audit included a secret field.")
        reset_detail = json.loads(_events(target_id)[0][1])
        if "password_updated_at" not in reset_detail or reset_detail.get("active") is not True:
            _fail(f"password_reset detail was not safe account state: {reset_detail}")
    finally:
        with get_connection() as conn:
            conn.execute(
                "UPDATE client_company_relationships SET assigned_user_id = ? WHERE id = ?",
                (rel["assigned_user_id"], int(rel["id"])),
            )
            conn.commit()
        _delete_user(target_id)
        _delete_user(actor_id)


def test_inactive_reset_stays_inactive_and_zero_sessions_skip_revoke_audit() -> None:
    http, csrf, actor_id = _actor()
    new_password = _password()
    email = _email()
    target_id = _insert_user(
        email=email,
        name="Inactive Target",
        administrator=False,
        active=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
        failed_login_count=2,
        locked_until="2026-09-27T18:00:00Z",
    )
    try:
        response = _reset(http, csrf, target_id, new_password)
        if response.status_code != 200:
            _fail(response.text)
        if int(response.json().get("sessions_revoked") or 0) != 0:
            _fail("A user with no sessions reported a revocation.")
        after = _row(target_id)
        if int(after["active"]) != 0:
            _fail("Inactive account was reactivated.")
        if str(after["locked_until"] or "") != "" or int(after["failed_login_count"] or 0) != 0:
            _fail("Inactive reset did not clear lockout.")
        if not verify_password(new_password, str(after["password_hash"])):
            _fail("Inactive account did not receive the new password.")
        login = _client().post("/api/auth/login", json={"email": email, "password": new_password})
        if login.status_code == 200:
            _fail("Inactive account authenticated after reset.")
        kinds = [kind for kind, _detail in _events(target_id)]
        if kinds != ["password_reset"]:
            _fail(f"Zero-session reset should audit only password_reset, got {kinds}")
        detail = json.loads(_events(target_id)[0][1])
        if detail.get("active") is not False:
            _fail("Inactive reset audit did not record active false.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_auth_and_weak_password_do_not_mutate() -> None:
    http, csrf, actor_id = _actor()
    password = _password()
    email = _email()
    locked_until = "2026-09-27T19:00:00Z"
    target_id = _insert_user(
        email=email,
        name="Guarded",
        administrator=False,
        password=password,
        role=REVOPS_SPECIALIST,
        failed_login_count=3,
        locked_until=locked_until,
    )
    specialist_password = _password()
    specialist_email = _email()
    specialist_id = _insert_user(
        email=specialist_email,
        name="Specialist",
        administrator=False,
        password=specialist_password,
        role=REVOPS_SPECIALIST,
    )
    before = str(_row(target_id)["password_hash"])
    try:
        missing_session = _client().post(
            f"/api/admin/users/{target_id}/reset-password",
            json={"password": _password()},
        )
        if missing_session.status_code != 401 or missing_session.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"Unauthenticated reset must be 401, got {missing_session.status_code}")
        specialist = _client()
        specialist_csrf = _login(specialist, specialist_email, specialist_password)
        forbidden = _reset(specialist, specialist_csrf, target_id, _password())
        if forbidden.status_code != 403 or forbidden.json().get("detail") != ADMIN_REQUIRED_DETAIL:
            _fail(f"Non-admin reset must be 403, got {forbidden.status_code} {forbidden.text}")
        no_csrf = http.post(
            f"/api/admin/users/{target_id}/reset-password",
            json={"password": _password()},
        )
        if no_csrf.status_code != 403 or no_csrf.json().get("detail") != CSRF_FAILED_DETAIL:
            _fail(f"Missing CSRF must be rejected, got {no_csrf.status_code} {no_csrf.text}")
        unknown = _reset(http, csrf, 999999, _password())
        if unknown.status_code != 404:
            _fail(f"Unknown user must be 404, got {unknown.status_code}")
        weak = _reset(http, csrf, target_id, "abcdefghijkl")
        if weak.status_code != 400:
            _fail(f"Weak password must be 400, got {weak.status_code} {weak.text}")
        if weak.json().get("detail") and "abcdefghijkl" in str(weak.json().get("detail")):
            _fail("Weak-password error echoed the password.")
        after = _row(target_id)
        if str(after["password_hash"]) != before:
            _fail("Rejected reset changed the password hash.")
        if int(after["failed_login_count"] or 0) != 3 or str(after["locked_until"] or "") != locked_until:
            _fail("Rejected reset cleared lockout.")
        if _events(target_id):
            _fail("Rejected reset created an audit event.")
    finally:
        _delete_user(specialist_id)
        _delete_user(target_id)
        _delete_user(actor_id)


def test_self_reset_revokes_current_session() -> None:
    password = _password()
    new_password = _password()
    email = _email()
    actor_id = _insert_user(
        email=email,
        name="Self Reset",
        administrator=True,
        password=password,
        role=SYSTEM_ADMINISTRATOR,
    )
    http = _client()
    csrf = _login(http, email, password)
    try:
        response = _reset(http, csrf, actor_id, new_password)
        if response.status_code != 200 or int(response.json().get("sessions_revoked") or 0) < 1:
            _fail(f"Self-reset must revoke the current session: {response.status_code} {response.text}")
        follow = http.get("/api/admin/users")
        if follow.status_code != 401:
            _fail("The old self-session still authorized a request.")
        old_login = _client().post("/api/auth/login", json={"email": email, "password": password})
        if old_login.status_code == 200:
            _fail("The old self password still authenticates.")
        new_login = _client().post("/api/auth/login", json={"email": email, "password": new_password})
        if new_login.status_code != 200:
            _fail("The new self password did not authenticate.")
    finally:
        _delete_user(actor_id)


def test_reset_rolls_back_password_sessions_and_audit() -> None:
    http, csrf, actor_id = _actor()
    old_password = _password()
    target_id = _insert_user(
        email=_email(),
        name="Rollback Reset",
        administrator=False,
        password=old_password,
        role=REVOPS_SPECIALIST,
        failed_login_count=2,
        locked_until="2026-09-27T20:00:00Z",
    )
    token = _open_session(target_id)
    before = str(_row(target_id)["password_hash"])
    import user_management as user_management_module

    real = user_management_module._record_admin_event
    calls = {"n": 0}

    def boom(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("audit failed")
        return real(conn, **kwargs)

    try:
        with patch("user_management._record_admin_event", side_effect=boom):
            try:
                _reset(http, csrf, target_id, _password())
            except RuntimeError as exc:
                if "audit failed" not in str(exc):
                    raise
        after = _row(target_id)
        if str(after["password_hash"]) != before:
            _fail("Rollback left the new password in place.")
        if int(after["failed_login_count"] or 0) != 2 or str(after["locked_until"] or "") != "2026-09-27T20:00:00Z":
            _fail("Rollback cleared lockout.")
        if _session_revoked(token):
            _fail("Rollback left the session revoked.")
        if _events(target_id):
            _fail("Rollback left an audit row.")
        if not verify_password(old_password, str(after["password_hash"])):
            _fail("Rollback broke the old password.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_tests_do_not_change_live_passwords() -> None:
    conn = sqlite3.connect(f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        emails = [str(row["email"]).lower() for row in conn.execute("SELECT email FROM users")]
        if len(emails) != 3:
            _fail(f"Live users count changed: {len(emails)}")
        for pilot in PILOT_EMAILS:
            if pilot in emails:
                _fail(f"Live database contains {pilot}")
        if any(email.startswith("um2d.") for email in emails):
            _fail("A UM-2D test user was written to the live database.")
        events = int(conn.execute("SELECT COUNT(*) FROM staff_admin_events").fetchone()[0])
        if events != 0:
            _fail(f"Live staff_admin_events changed: {events}")
    finally:
        conn.close()


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    tests = [
        test_reset_active_user_replaces_password_and_revokes_sessions,
        test_inactive_reset_stays_inactive_and_zero_sessions_skip_revoke_audit,
        test_auth_and_weak_password_do_not_mutate,
        test_self_reset_revokes_current_session,
        test_reset_rolls_back_password_sessions_and_audit,
        test_tests_do_not_change_live_passwords,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
