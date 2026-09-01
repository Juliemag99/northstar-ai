"""Checkpoint B — HTTP 401 enforcement when auth_enforcement_active() is true.

Run: python test_staff_auth_phase2.py

Uses isolated testdb copies only — never writes production northstar.db,
never talks to live :8007, and never stores a real staff password in source.
"""

from __future__ import annotations

import testdb
import json
import os
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from access import DEFAULT_USER_EMAIL
from auth_http import (
    AUTH_REQUIRED_DETAIL,
    CSRF_HEADER,
    ENFORCE_FLAG,
    LOGIN_FAILED_DETAIL,
    SESSION_COOKIE,
    auth_enforcement_active,
)
from auth_passwords import hash_password
from auth_sessions import hash_session_token, revoke_staff_session
from db import get_connection
from email_oauth_credentials import (
    consume_oauth_state,
    ensure_email_oauth_credentials_schema,
    store_oauth_state,
)
from main import app

PROTECTED_GETS = (
    "/api/users/default",
    "/api/prospects",
    "/api/email/google/status",
    "/api/email/google/connect/1",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
    "/",
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret_password() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client() -> TestClient:
    return TestClient(app)


def _dump(payload: object) -> str:
    return json.dumps(payload, default=str)


def _assert_no_secrets(payload: object, *secrets_out: str) -> None:
    dumped = _dump(payload).lower()
    if "password_hash" in dumped:
        _fail("JSON leaked password_hash.")
    if "$argon2id$" in dumped:
        _fail("JSON leaked an Argon2 hash.")
    if "csrf_secret" in dumped:
        _fail("JSON leaked csrf_secret.")
    for item in secrets_out:
        if item and item.lower() in dumped:
            _fail("JSON leaked a raw secret.")


def _assert_auth_required(response, *secrets_out: str) -> None:
    if response.status_code != 401:
        _fail(f"Expected 401, got {response.status_code}: {response.text}")
    payload = response.json()
    if payload.get("detail") != AUTH_REQUIRED_DETAIL:
        _fail(f"Protected 401 detail was not generic: {payload}")
    _assert_no_secrets(payload, *secrets_out)


def _create_user(*, email: str, password: str, active: int = 1) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, ?, 0, 1, ?, ?, 0, '')
            """,
            (email, "Phase 2 Auth User", int(active), digest),
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


def _login(client: TestClient, email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


@contextmanager
def _enforcement_on():
    previous = os.environ.get(ENFORCE_FLAG)
    os.environ[ENFORCE_FLAG] = "1"
    try:
        if not auth_enforcement_active():
            _fail("Test expected enforcement to activate with flag=1 and Julie hash.")
        yield
    finally:
        if previous is None:
            os.environ.pop(ENFORCE_FLAG, None)
        else:
            os.environ[ENFORCE_FLAG] = previous


def _expire_session_cookie(raw: str) -> None:
    token_hash = hash_session_token(raw)
    past = "2000-01-01T00:00:00Z"
    with get_connection() as conn:
        conn.execute(
            "UPDATE staff_sessions SET expires_at = ?, last_seen_at = ? WHERE token_hash = ?",
            (past, past, token_hash),
        )
        conn.commit()


def test_optional_mode_unchanged() -> None:
    os.environ.pop(ENFORCE_FLAG, None)
    if auth_enforcement_active():
        _fail("Enforcement must default to off.")
    client = _client()
    root = client.get("/")
    if root.status_code != 200:
        _fail(f"GET / should stay 200 when enforcement is off, got {root.status_code}.")
    users = client.get("/api/users/default")
    if users.status_code != 200:
        _fail(f"GET /api/users/default should stay 200 when off, got {users.status_code}.")
    docs = client.get("/openapi.json")
    if docs.status_code != 200:
        _fail(f"OpenAPI should stay 200 when off, got {docs.status_code}.")


def test_flag_without_julie_password_does_not_enforce() -> None:
    previous = os.environ.get(ENFORCE_FLAG)
    snapshot = None
    julie_id = None
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, password_hash FROM users WHERE lower(email) = lower(?)
                """,
                (DEFAULT_USER_EMAIL,),
            ).fetchone()
            if row is None:
                _fail("Julie row missing from isolated testdb.")
            snapshot = dict(row)
            julie_id = int(row["id"])
            conn.execute("UPDATE users SET password_hash = '' WHERE id = ?", (julie_id,))
            conn.commit()
        os.environ[ENFORCE_FLAG] = "1"
        if auth_enforcement_active():
            _fail("Flag 1 without Julie password must not activate enforcement.")
        client = _client()
        users = client.get("/api/users/default")
        if users.status_code != 200:
            _fail("Optional CRM access must remain when enforcement is inactive.")
    finally:
        if snapshot is not None and julie_id is not None:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (snapshot["password_hash"], julie_id),
                )
                conn.commit()
        if previous is None:
            os.environ.pop(ENFORCE_FLAG, None)
        else:
            os.environ[ENFORCE_FLAG] = previous


def test_exact_allowlist_and_protected_paths() -> None:
    client = _client()
    with _enforcement_on():
        allowed = (
            ("GET", "/health"),
            ("GET", "/api/auth/me"),
        )
        for method, path in allowed:
            response = client.request(method, path)
            if response.status_code == 401:
                _fail(f"{method} {path} must stay allowlisted, got 401.")
            _assert_no_secrets(response.json() if response.headers.get("content-type", "").startswith("application/json") else {})

        me = client.get("/api/auth/me")
        if me.status_code != 200 or me.json().get("authenticated") is not False:
            _fail("Allowlisted /me must remain 200 unauthenticated.")
        if me.json().get("auth_enforced") is not True:
            _fail("/me must report auth_enforced true.")

        health = client.get("/health")
        if health.status_code != 200 or health.json().get("status") != "healthy":
            _fail("GET /health must remain 200 when enforced.")

        root = client.get("/")
        _assert_auth_required(root)

        for path in PROTECTED_GETS:
            if path == "/":
                continue
            _assert_auth_required(client.get(path))

        _assert_auth_required(client.get("/api/auth/login"))
        _assert_auth_required(client.post("/api/auth/me"))
        _assert_auth_required(client.get("/api/auth/logout"))
        _assert_auth_required(client.post("/api/email/google/callback"))
        _assert_auth_required(client.get("/api/email/google/callback/extra"))
        _assert_auth_required(client.get("/api/auth/login/"))
        _assert_auth_required(client.post("/health"))

        options = client.options("/api/prospects")
        if options.status_code == 401:
            _fail("OPTIONS must not 401 when enforcement is on.")

        login = client.post(
            "/api/auth/login",
            json={"email": "nobody@example.test", "password": "wrong"},
        )
        if login.status_code != 401:
            _fail(f"Failed login should stay 401, got {login.status_code}.")
        if login.json().get("detail") != LOGIN_FAILED_DETAIL:
            _fail("Failed login detail drifted.")
        _assert_no_secrets(login.json())


def test_valid_session_reaches_protected_handlers() -> None:
    password = _secret_password()
    email = f"phase2.ok.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    client = _client()
    try:
        with _enforcement_on():
            login = _login(client, email, password)
            if login.status_code != 200:
                _fail(f"Login should succeed when enforced, got {login.status_code}.")
            csrf = str(login.json().get("csrf_token") or "")
            users = client.get("/api/users/default")
            if users.status_code == 401:
                _fail("Valid session must reach GET /api/users/default.")
            root = client.get("/")
            if root.status_code == 401:
                _fail("Valid session must reach GET /.")
            write = client.post("/api/activities", json={})
            if write.status_code != 403:
                _fail(f"Authenticated write without CSRF must stay 403, got {write.status_code}.")
            if write.json().get("detail") != "CSRF token missing or invalid.":
                _fail(f"CSRF 403 detail drifted: {write.json()}")
            logged_out = client.post("/api/auth/logout", headers={CSRF_HEADER: csrf})
            if logged_out.status_code != 200:
                _fail(f"Authenticated logout with CSRF failed: {logged_out.status_code}.")
            after = client.get("/api/users/default")
            _assert_auth_required(after)
    finally:
        _delete_user(user_id)


def test_invalid_sessions_are_unauthenticated() -> None:
    password = _secret_password()
    email = f"phase2.bad.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    client = _client()
    try:
        with _enforcement_on():
            missing = _client()
            _assert_auth_required(missing.get("/api/users/default"))

            garbage = _client()
            garbage.cookies.set(SESSION_COOKIE, "not-a-real-session")
            _assert_auth_required(garbage.get("/api/users/default"))

            login = _login(client, email, password)
            if login.status_code != 200:
                _fail("Login failed while preparing expired-session test.")
            raw = client.cookies.get(SESSION_COOKIE)
            _expire_session_cookie(str(raw))
            _assert_auth_required(client.get("/api/users/default"), str(raw))

            client2 = _client()
            login2 = _login(client2, email, password)
            if login2.status_code != 200:
                _fail("Login failed while preparing revoked-session test.")
            raw2 = str(client2.cookies.get(SESSION_COOKIE) or "")
            with get_connection() as conn:
                revoke_staff_session(conn, raw2)
                conn.commit()
            _assert_auth_required(client2.get("/api/users/default"), raw2)

            client3 = _client()
            login3 = _login(client3, email, password)
            if login3.status_code != 200:
                _fail("Login failed while preparing inactive-user test.")
            with get_connection() as conn:
                conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
                conn.commit()
            _assert_auth_required(client3.get("/api/users/default"))
    finally:
        with get_connection() as conn:
            conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
            conn.commit()
        _delete_user(user_id)


def test_logout_allowlist() -> None:
    password = _secret_password()
    email = f"phase2.logout.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password)
    client = _client()
    try:
        with _enforcement_on():
            absent = _client()
            gone = absent.post("/api/auth/logout")
            if gone.status_code != 200:
                _fail(f"Logout without a cookie must stay 200, got {gone.status_code}.")

            login = _login(client, email, password)
            csrf = str(login.json().get("csrf_token") or "")
            raw = str(client.cookies.get(SESSION_COOKIE) or "")
            blocked = client.post("/api/auth/logout")
            if blocked.status_code != 403:
                _fail(f"Authenticated logout without CSRF must 403, got {blocked.status_code}.")

            _expire_session_cookie(raw)
            expired = client.post("/api/auth/logout")
            if expired.status_code != 200:
                _fail(f"Logout with expired cookie must stay 200, got {expired.status_code}.")
            _assert_no_secrets(expired.json(), raw, csrf)
    finally:
        _delete_user(user_id)


def test_oauth_callback_allowlist_and_state() -> None:
    ensure_email_oauth_credentials_schema()
    client = _client()
    with _enforcement_on():
        missing = client.get(
            "/api/email/google/callback",
            follow_redirects=False,
        )
        if missing.status_code == 401:
            _fail("GET /api/email/google/callback must not be 401-blocked.")
        if missing.status_code != 302:
            _fail(f"Missing OAuth params should redirect, got {missing.status_code}.")

        with patch("gmail_oauth.google_oauth_configured", return_value=True):
            invalid = client.get(
                "/api/email/google/callback",
                params={"code": "x", "state": "no-such-state"},
                follow_redirects=False,
            )
            if invalid.status_code == 401:
                _fail("Invalid OAuth state must be rejected by the handler, not 401 middleware.")
            if invalid.status_code != 302:
                _fail(f"Invalid OAuth state should redirect, got {invalid.status_code}.")
            location = invalid.headers.get("location") or ""
            if "email_oauth=error" not in location:
                _fail("Invalid OAuth state redirect must stay an error bounce.")

        state = f"phase2.{secrets.token_hex(8)}"
        store_oauth_state(
            state=state,
            client_id=1,
            email_account_id=1,
            user_id=1,
            ttl_seconds=600,
        )
        first = consume_oauth_state(state)
        if first is None:
            _fail("Fresh OAuth state should consume once.")
        reused = consume_oauth_state(state)
        if reused is not None:
            _fail("OAuth state must be one-time.")

        expired_state = f"phase2.exp.{secrets.token_hex(8)}"
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO email_oauth_states
                    (state, client_id, email_account_id, user_id, created_at, expires_at)
                VALUES (?, 1, 1, 1, ?, ?)
                """,
                (expired_state, past, past),
            )
            conn.commit()
        if consume_oauth_state(expired_state) is not None:
            _fail("Expired OAuth state must be rejected.")


def test_session_lookup_exception_fails_closed() -> None:
    with _enforcement_on():
        client = _client()
        with patch(
            "auth_http.lookup_request_session",
            side_effect=RuntimeError("lookup failed internally"),
        ):
            response = client.get("/api/users/default")
            _assert_auth_required(response, "lookup failed internally")
            allow = client.get("/health")
            if allow.status_code != 200:
                _fail("Allowlisted /health must still work if session lookup fails.")


def main() -> int:
    test_optional_mode_unchanged()
    test_flag_without_julie_password_does_not_enforce()
    test_exact_allowlist_and_protected_paths()
    test_valid_session_reaches_protected_handlers()
    test_invalid_sessions_are_unauthenticated()
    test_logout_allowlist()
    test_oauth_callback_allowlist_and_state()
    test_session_lookup_exception_fails_closed()
    print("test_staff_auth_phase2: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
