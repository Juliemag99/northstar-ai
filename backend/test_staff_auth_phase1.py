"""Phase 1 Checkpoint A — staff auth HTTP (optional; enforcement off).

Run: python test_staff_auth_phase1.py

Uses isolated testdb copies only — never writes production northstar.db,
never bootstraps live data, and never stores a real staff password in source.
Does not enable route-protection middleware (Checkpoint B).
"""

from __future__ import annotations

import testdb
import json
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from access import DEFAULT_USER_EMAIL, get_default_user
from auth_http import (
    CSRF_HEADER,
    ENFORCE_FLAG,
    LOGIN_FAILED_DETAIL,
    SESSION_COOKIE,
    auth_enforcement_active,
)
from auth_passwords import hash_password
from auth_sessions import ABSOLUTE_HOURS, hash_session_token, revoke_staff_session
from db import PRODUCTION_DB_PATH, get_connection
from main import app

GENERIC_FAIL = LOGIN_FAILED_DETAIL


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret_password() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client() -> TestClient:
    return TestClient(app)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(payload: object) -> str:
    return json.dumps(payload, default=str)


def _assert_no_secrets(payload: object, *secrets_out: str) -> None:
    dumped = _dump(payload).lower()
    if "password_hash" in dumped:
        _fail("JSON leaked password_hash.")
    if "$argon2id$" in dumped:
        _fail("JSON leaked an Argon2 hash.")
    for item in secrets_out:
        if item and item.lower() in dumped:
            _fail("JSON leaked a raw secret.")


def _assert_generic_fail(response) -> None:
    if response.status_code != 401:
        _fail(f"Expected 401 generic failure, got {response.status_code}: {response.text}")
    payload = response.json()
    if payload.get("detail") != GENERIC_FAIL:
        _fail(f"Failure detail was not generic: {payload}")
    _assert_no_secrets(payload)
    cookie = "\n".join(_set_cookie_headers(response))
    if SESSION_COOKIE in cookie and "max-age=0" not in cookie.lower():
        _fail("Failed login set a session cookie.")


def _set_cookie_headers(response) -> list[str]:
    headers = response.headers
    getter = getattr(headers, "get_list", None)
    if callable(getter):
        raw = getter("set-cookie")
        if raw:
            return list(raw)
    single = headers.get("set-cookie")
    return [single] if single else []


def _session_set_cookie(response) -> str:
    for header in _set_cookie_headers(response):
        if header.lower().startswith(f"{SESSION_COOKIE.lower()}="):
            return header
    joined = "\n".join(_set_cookie_headers(response))
    _fail(f"Login did not Set-Cookie {SESSION_COOKIE}: {joined}")
    return ""


def _assert_cookie_attrs(header: str, *, secure: bool) -> None:
    lowered = header.lower()
    if "httponly" not in lowered:
        _fail(f"Session cookie missing HttpOnly: {header}")
    if "path=/" not in lowered:
        _fail(f"Session cookie Path is not /: {header}")
    if "samesite=lax" not in lowered:
        _fail(f"Session cookie SameSite is not Lax: {header}")
    max_age = str(int(ABSOLUTE_HOURS * 3600))
    if f"max-age={max_age}" not in lowered:
        _fail(f"Session cookie Max-Age is not {max_age}: {header}")
    if re.search(r"(^|;)\s*domain=", header, flags=re.I):
        _fail(f"Session cookie must not set Domain: {header}")
    if secure and "secure" not in lowered:
        _fail(f"HTTPS session cookie missing Secure: {header}")
    if not secure and re.search(r"(^|;)\s*secure(;|$)", header, flags=re.I):
        _fail(f"HTTP session cookie must not set Secure: {header}")


def _create_user(
    *,
    email: str,
    password: str | None,
    active: int = 1,
    empty_hash: bool = False,
) -> int:
    digest = "" if empty_hash or password is None else hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, ?, 0, 1, ?, ?, 0, '')
            """,
            (email, "Phase 1 Auth User", int(active), digest),
        )
        user_id = int(
            conn.execute(
                "SELECT id FROM users WHERE email = ?",
                (email,),
            ).fetchone()["id"]
        )
        conn.commit()
    return user_id


def _delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def _user_lock_state(user_id: int) -> tuple[int, str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT failed_login_count, locked_until FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        _fail("Test user missing.")
    return int(row["failed_login_count"] or 0), str(row["locked_until"] or "")


def _session_rows(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM staff_sessions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _login(client: TestClient, email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_login_success_and_cookie() -> None:
    password = _secret_password()
    email = f"phase1.login.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    client = _client()
    try:
        response = _login(client, email, password)
        if response.status_code != 200:
            _fail(f"Login should succeed, got {response.status_code}: {response.text}")
        payload = response.json()
        if not payload.get("ok") or not payload.get("authenticated"):
            _fail(f"Login payload missing ok/authenticated: {payload}")
        if payload.get("auth_enforced") is not False:
            _fail("Enforcement must default to off.")
        user = payload.get("user") or {}
        if str(user.get("email") or "").lower() != email:
            _fail("Login user email mismatch.")
        csrf = str(payload.get("csrf_token") or "")
        if len(csrf) < 16:
            _fail("Login did not return a CSRF token.")
        raw = client.cookies.get(SESSION_COOKIE)
        if not raw:
            _fail("Login did not store northstar_session on the client.")
        _assert_no_secrets(payload, password, raw)
        header = _session_set_cookie(response)
        _assert_cookie_attrs(header, secure=False)
        if "northstar_csrf" in "\n".join(_set_cookie_headers(response)).lower():
            _fail("Checkpoint A must not set a readable CSRF cookie.")

        rows = _session_rows(user_id)
        if len(rows) != 1:
            _fail(f"Expected one session row, got {len(rows)}.")
        stored = rows[0]
        if str(stored.get("token_hash")) != hash_session_token(raw):
            _fail("Session token_hash is not SHA-256 of the cookie token.")
        blob = " ".join(str(value) for value in stored.values())
        if raw in blob or password in blob:
            _fail("staff_sessions stored a raw token or password.")
        if csrf != str(stored.get("csrf_secret")):
            _fail("JSON csrf_token does not match the session csrf_secret.")
    finally:
        _delete_user(user_id)


def test_https_cookie_sets_secure() -> None:
    password = _secret_password()
    email = f"phase1.https.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    client = TestClient(app, base_url="https://testserver")
    try:
        response = _login(client, email, password)
        if response.status_code != 200:
            _fail(f"HTTPS login failed: {response.status_code}: {response.text}")
        _assert_cookie_attrs(_session_set_cookie(response), secure=True)
    finally:
        _delete_user(user_id)


def test_generic_login_failures() -> None:
    password = _secret_password()
    email = f"phase1.fail.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    empty_email = f"phase1.empty.{secrets.token_hex(4)}@example.test"
    empty_id = _create_user(email=empty_email, password=None, empty_hash=True)
    inactive_email = f"phase1.inactive.{secrets.token_hex(4)}@example.test"
    inactive_id = _create_user(email=inactive_email, password=password, active=0)
    client = _client()
    try:
        cases = [
            ("unknown@example.test", password, "unknown email"),
            (email, "WrongPass9xxxx", "wrong password"),
            (inactive_email, password, "inactive account"),
            (empty_email, password, "empty hash"),
            ("", password, "blank email"),
            (email, "", "empty password"),
        ]
        for attempt_email, attempt_password, label in cases:
            response = _login(client, attempt_email, attempt_password)
            try:
                _assert_generic_fail(response)
            except AssertionError as exc:
                _fail(f"{label}: {exc}")
            if client.cookies.get(SESSION_COOKIE):
                _fail(f"{label} left a session cookie.")
    finally:
        _delete_user(user_id)
        _delete_user(empty_id)
        _delete_user(inactive_id)


def test_unknown_email_uses_dummy_argon2() -> None:
    import auth_http as auth_http_mod

    seen = {"n": 0}
    original = auth_http_mod._dummy_verify

    def wrapped(password: str) -> None:
        seen["n"] += 1
        return original(password)

    auth_http_mod._dummy_verify = wrapped  # type: ignore[method-assign]
    client = _client()
    try:
        response = _login(client, "nobody@example.test", _secret_password())
        _assert_generic_fail(response)
        if seen["n"] < 1:
            _fail("Unknown email skipped dummy Argon2 verification.")
    finally:
        auth_http_mod._dummy_verify = original  # type: ignore[method-assign]


def test_me_session_states() -> None:
    password = _secret_password()
    email = f"phase1.me.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    default = get_default_user()
    if default is None:
        _fail("Default user missing.")
    try:
        none_client = _client()
        none_resp = none_client.get("/api/auth/me")
        if none_resp.status_code != 200:
            _fail(f"GET /me without cookie failed: {none_resp.status_code}")
        none_payload = none_resp.json()
        if none_payload.get("authenticated") is not False:
            _fail("Unauthenticated /me should set authenticated false.")
        if str((none_payload.get("user") or {}).get("email") or "").lower() != DEFAULT_USER_EMAIL:
            _fail("/me without a session should return Julie.")
        if none_payload.get("csrf_token"):
            _fail("Unauthenticated /me should not return a CSRF token.")
        _assert_no_secrets(none_payload)

        client = _client()
        login_resp = _login(client, email, password)
        if login_resp.status_code != 200:
            _fail(f"Login for /me tests failed: {login_resp.text}")
        csrf = login_resp.json()["csrf_token"]
        raw = client.cookies.get(SESSION_COOKIE)
        me = client.get("/api/auth/me")
        if me.status_code != 200 or me.json().get("authenticated") is not True:
            _fail(f"Valid session /me failed: {me.status_code} {me.text}")
        me_payload = me.json()
        if str((me_payload.get("user") or {}).get("email") or "").lower() != email:
            _fail("Valid /me returned the wrong user.")
        if me_payload.get("csrf_token") != csrf:
            _fail("Valid /me CSRF token drifted.")
        _assert_no_secrets(me_payload, password, raw or "")

        invalid = _client()
        invalid_resp = invalid.get(
            "/api/auth/me",
            cookies={SESSION_COOKIE: "not-a-real-session-token"},
        )
        if invalid_resp.status_code != 200:
            _fail(f"Invalid session /me should be 200 logged-out, got {invalid_resp.status_code}")
        if invalid_resp.json().get("authenticated") is not False:
            _fail("Invalid session /me should be unauthenticated.")
        clear_hdr = "\n".join(_set_cookie_headers(invalid_resp)).lower()
        if SESSION_COOKIE not in clear_hdr or "max-age=0" not in clear_hdr:
            _fail("Invalid session /me should clear the cookie.")

        expired_client = _client()
        expired_login = _login(expired_client, email, password)
        if expired_login.status_code != 200:
            _fail("Expired-session login failed.")
        expired_raw = expired_client.cookies.get(SESSION_COOKIE)
        past = _iso(_now() - timedelta(hours=25))
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE staff_sessions
                SET created_at = ?, last_seen_at = ?, expires_at = ?
                WHERE token_hash = ?
                """,
                (past, past, past, hash_session_token(expired_raw or "")),
            )
            conn.commit()
        expired_me = expired_client.get("/api/auth/me")
        if expired_me.json().get("authenticated") is not False:
            _fail("Expired session /me should be unauthenticated.")

        revoked_client = _client()
        revoked_login = _login(revoked_client, email, password)
        revoked_raw = revoked_client.cookies.get(SESSION_COOKIE) or ""
        with get_connection() as conn:
            if not revoke_staff_session(conn, revoked_raw):
                _fail("revoke_staff_session failed in /me test.")
            conn.commit()
        revoked_me = revoked_client.get("/api/auth/me")
        if revoked_me.json().get("authenticated") is not False:
            _fail("Revoked session /me should be unauthenticated.")
    finally:
        _delete_user(user_id)


def test_csrf_and_unauthenticated_writes() -> None:
    password = _secret_password()
    email = f"phase1.csrf.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    try:
        anon = testdb.http_json("POST", "/api/activities", {})
        if anon[0] not in {400, 422}:
            _fail(f"Unauthenticated write should still validate, not CSRF-block, got {anon[0]}: {anon[1]}")
        if "CSRF" in _dump(anon[1]).upper():
            _fail("Unauthenticated write was CSRF-blocked while enforcement is off.")

        health = testdb.http_json("GET", "/health")
        if health[0] != 200:
            _fail(f"GET /health changed: {health}")
        users = testdb.http_json("GET", "/api/users/default")
        if users[0] != 200:
            _fail(f"GET /api/users/default changed: {users}")
        returned = users[1].get("user") or {}
        if str(returned.get("email") or "").lower() != DEFAULT_USER_EMAIL:
            _fail("Default user API no longer returns Julie.")
        _assert_no_secrets(users[1])

        client = _client()
        login_resp = _login(client, email, password)
        if login_resp.status_code != 200:
            _fail(f"CSRF test login failed: {login_resp.text}")
        csrf = str(login_resp.json()["csrf_token"])

        missing = client.post("/api/activities", json={})
        if missing.status_code != 403:
            _fail(f"Authenticated write without CSRF should be 403, got {missing.status_code}")
        if missing.json().get("detail") != "CSRF token missing or invalid.":
            _fail(f"Unexpected CSRF error body: {missing.json()}")

        wrong = client.post(
            "/api/activities",
            json={},
            headers={CSRF_HEADER: "x" * len(csrf)},
        )
        if wrong.status_code != 403:
            _fail(f"Wrong CSRF should be 403, got {wrong.status_code}")

        ok_csrf = client.post(
            "/api/activities",
            json={},
            headers={CSRF_HEADER: csrf},
        )
        if ok_csrf.status_code not in {400, 422}:
            _fail(
                "Authenticated write with CSRF should reach the handler, "
                f"got {ok_csrf.status_code}: {ok_csrf.text}"
            )

        bare_logout = client.post("/api/auth/logout")
        if bare_logout.status_code != 403:
            _fail(f"Logout with a valid session and no CSRF should be 403, got {bare_logout.status_code}")
        still = client.get("/api/auth/me")
        if still.json().get("authenticated") is not True:
            _fail("CSRF-rejected logout must leave the session active.")

        logged_out = client.post("/api/auth/logout", headers={CSRF_HEADER: csrf})
        if logged_out.status_code != 200:
            _fail(f"Logout with CSRF failed: {logged_out.status_code} {logged_out.text}")
        after = client.get("/api/auth/me")
        if after.json().get("authenticated") is not False:
            _fail("Logout should end the session.")

        stale_logout = client.post("/api/auth/logout")
        if stale_logout.status_code != 200:
            _fail(f"Logout without a valid session should succeed, got {stale_logout.status_code}")
    finally:
        _delete_user(user_id)


def test_lockout_and_reset() -> None:
    password = _secret_password()
    email = f"phase1.lock.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    client = _client()
    try:
        for _ in range(3):
            _assert_generic_fail(_login(client, email, "WrongPass9xxxx"))
        count, locked = _user_lock_state(user_id)
        if count != 3 or locked.strip():
            _fail(f"Three failures should not lock yet: count={count} locked={locked!r}")

        success = _login(client, email, password)
        if success.status_code != 200:
            _fail(f"Login after three failures should succeed: {success.text}")
        count, locked = _user_lock_state(user_id)
        if count != 0 or locked.strip():
            _fail(f"Successful login did not reset lockout: count={count} locked={locked!r}")
        client.post("/api/auth/logout", headers={CSRF_HEADER: success.json()["csrf_token"]})

        for i in range(5):
            _assert_generic_fail(_login(client, email, "WrongPass9xxxx"))
            count, locked = _user_lock_state(user_id)
            if i < 4 and locked.strip():
                _fail(f"Locked before five failures (attempt {i + 1}): {locked!r}")
        count, locked = _user_lock_state(user_id)
        if count < 5 or not locked.strip():
            _fail(f"Five failures should lock the account: count={count} locked={locked!r}")
        until = locked
        locked_login = _login(client, email, password)
        _assert_generic_fail(locked_login)
        count_after, locked_after = _user_lock_state(user_id)
        if locked_after != until:
            _fail("Locked logins should not extend or clear locked_until.")
        if count_after != count:
            _fail("Locked logins should not increment failed_login_count.")
    finally:
        _delete_user(user_id)


def test_enforcement_fail_safe() -> None:
    previous = os.environ.pop(ENFORCE_FLAG, None)
    julie_id = None
    snapshot = None
    password = _secret_password()
    try:
        if auth_enforcement_active():
            _fail("Enforcement must default to off.")
        os.environ[ENFORCE_FLAG] = "1"
        if auth_enforcement_active():
            _fail("Enforce flag without a Julie password hash must stay inactive.")

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, password_hash, failed_login_count, locked_until
                FROM users WHERE lower(email) = lower(?)
                """,
                (DEFAULT_USER_EMAIL,),
            ).fetchone()
            if row is None:
                _fail("Julie row missing from isolated testdb.")
            snapshot = dict(row)
            julie_id = int(row["id"])
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password, email=DEFAULT_USER_EMAIL), julie_id),
            )
            conn.commit()
        if not auth_enforcement_active():
            _fail("Enforce flag plus Julie hash should activate fail-safe True.")
        os.environ[ENFORCE_FLAG] = "0"
        if auth_enforcement_active():
            _fail("NORTHSTAR_AUTH_ENFORCE=0 must not enforce.")
        os.environ[ENFORCE_FLAG] = "true"
        if auth_enforcement_active():
            _fail("Only NORTHSTAR_AUTH_ENFORCE=1 may enable enforcement.")

        os.environ[ENFORCE_FLAG] = "1"
        users = testdb.http_json("GET", "/api/users/default")
        if users[0] != 200:
            _fail("Checkpoint A must not 401-protect routes when the flag is on.")
        me = testdb.http_json("GET", "/api/auth/me")
        if me[0] != 200 or me[1].get("authenticated") is not False:
            _fail("Unauthenticated /me must still work in Checkpoint A.")
        if me[1].get("auth_enforced") is not True:
            _fail("/me should report auth_enforced true when fail-safe is active.")
        if me[1].get("auth_available") is not True:
            _fail("/me should report auth_available when Julie has a hash.")
    finally:
        if snapshot is not None and julie_id is not None:
            with get_connection() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, failed_login_count = ?, locked_until = ?
                    WHERE id = ?
                    """,
                    (
                        snapshot["password_hash"],
                        snapshot["failed_login_count"],
                        snapshot["locked_until"],
                        julie_id,
                    ),
                )
                conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (julie_id,))
                conn.commit()
        if previous is None:
            os.environ.pop(ENFORCE_FLAG, None)
        else:
            os.environ[ENFORCE_FLAG] = previous
        if auth_enforcement_active():
            _fail("Enforcement leaked on after fail-safe tests.")


def test_live_database_not_migrated() -> None:
    live = sqlite3.connect(str(PRODUCTION_DB_PATH))
    try:
        names = {str(row[1]) for row in live.execute("PRAGMA table_info(users)").fetchall()}
        sessions = live.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'staff_sessions'"
        ).fetchone()
    finally:
        live.close()
    if "password_hash" in names or sessions is not None:
        _fail("Live northstar.db was migrated; Phase 1 must use isolated copies only.")


def test_no_route_protection_middleware() -> None:
    from pathlib import Path

    main_src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
    if "Authentication required." in main_src:
        _fail("Global 401 route protection must wait for Checkpoint B.")
    if "AUTH_ENFORCE" in main_src:
        _fail("AUTH_ENFORCE must not be wired in main.py.")


def main() -> int:
    test_login_success_and_cookie()
    test_https_cookie_sets_secure()
    test_generic_login_failures()
    test_unknown_email_uses_dummy_argon2()
    test_me_session_states()
    test_csrf_and_unauthenticated_writes()
    test_lockout_and_reset()
    test_enforcement_fail_safe()
    test_live_database_not_migrated()
    test_no_route_protection_middleware()
    print("test_staff_auth_phase1: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
