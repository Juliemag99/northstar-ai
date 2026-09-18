"""Isolated tests for pilot hosting foundation. Never writes production northstar.db."""

from __future__ import annotations

import json
import os
import secrets

import testdb

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER, ENFORCE_FLAG, SESSION_COOKIE
from auth_passwords import hash_password
from db import get_connection
from main import app
from staff_rbac import REVOPS_SPECIALIST, set_user_staff_role

os.environ.pop(ENFORCE_FLAG, None)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client() -> TestClient:
    return TestClient(app)


def test_health_and_ready_do_not_leak() -> None:
    testdb.isolate_for_tests()
    http = _client()
    health = http.get("/health")
    if health.status_code != 200 or health.json().get("status") != "healthy":
        _fail(f"/health failed: {health.status_code} {health.text}")
    dumped = json.dumps(health.json()).lower()
    for needle in ("northstar.db", "password", "token", "c:\\", "onedrive"):
        if needle in dumped:
            _fail(f"/health leaked {needle}")
    ready = http.get("/api/ready")
    if ready.status_code != 200:
        _fail(f"/api/ready {ready.status_code} {ready.text}")
    body = ready.json()
    if body.get("database") != "ok":
        _fail(f"/api/ready database={body}")
    if "auth_enforced" not in body:
        _fail("/api/ready missing auth_enforced")
    dumped = json.dumps(body).lower()
    for needle in ("northstar.db", "password_hash", "token", "c:\\users"):
        if needle in dumped:
            _fail(f"/api/ready leaked {needle}")


def test_cookie_secure_requires_trusted_https_proxy() -> None:
    testdb.isolate_for_tests()
    password = _secret()
    email = f"hosting.{secrets.token_hex(3)}@example.test"
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (email, full_name, password_hash, is_administrator, active)
            VALUES (?, 'Hosting Tester', ?, 1, 1)
            """,
            (email, hash_password(password)),
        )
        conn.commit()
    http = _client()
    os.environ.pop("NORTHSTAR_TRUST_PROXY", None)
    login = http.post("/api/auth/login", json={"email": email, "password": password})
    if login.status_code != 200:
        _fail(f"login {login.status_code}")
    cookie = login.headers.get("set-cookie") or ""
    if "secure" in cookie.lower():
        _fail("HTTP login must not set Secure without TRUST_PROXY")
    http.cookies.clear()
    os.environ["NORTHSTAR_TRUST_PROXY"] = "1"
    try:
        spoof = http.post(
            "/api/auth/login",
            json={"email": email, "password": password},
            headers={"X-Forwarded-Proto": "https"},
        )
        if spoof.status_code != 200:
            _fail(f"trusted proxy login {spoof.status_code}")
        cookie = spoof.headers.get("set-cookie") or ""
        if "httponly" not in cookie.lower():
            _fail("session cookie missing HttpOnly")
        if "samesite=lax" not in cookie.lower():
            _fail("session cookie missing SameSite=Lax")
        if "secure" not in cookie.lower():
            _fail("HTTPS forwarded proto should set Secure when TRUST_PROXY=1")
    finally:
        os.environ.pop("NORTHSTAR_TRUST_PROXY", None)


def test_untrusted_forwarded_proto_does_not_set_secure() -> None:
    testdb.isolate_for_tests()
    password = _secret()
    email = f"untrust.{secrets.token_hex(3)}@example.test"
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (email, full_name, password_hash, is_administrator, active)
            VALUES (?, 'Untrusted', ?, 1, 1)
            """,
            (email, hash_password(password)),
        )
        conn.commit()
    os.environ.pop("NORTHSTAR_TRUST_PROXY", None)
    http = _client()
    login = http.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={"X-Forwarded-Proto": "https"},
    )
    cookie = login.headers.get("set-cookie") or ""
    if "secure" in cookie.lower():
        _fail("untrusted X-Forwarded-Proto must not force Secure cookies")


def test_restricted_user_cannot_administer() -> None:
    testdb.isolate_for_tests()
    os.environ[ENFORCE_FLAG] = "1"
    try:
        password = _secret()
        email = f"spec.{secrets.token_hex(3)}@example.test"
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    email, full_name, password_hash, is_administrator,
                    is_internal_northstar, active
                ) VALUES (?, 'Spec', ?, 0, 1, 1)
                """,
                (email, hash_password(password)),
            )
            uid = int(
                conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
            )
            row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
            if row is None:
                _fail("isolated copy has no clients")
            cid = int(row[0])
            conn.execute("DELETE FROM user_client_assignments WHERE user_id=?", (uid,))
            conn.execute(
                """
                INSERT INTO user_client_assignments (user_id, client_id, role, active)
                VALUES (?, ?, 'revops', 1)
                """,
                (uid, cid),
            )
            set_user_staff_role(conn, uid, REVOPS_SPECIALIST)
            conn.commit()
        http = _client()
        login = http.post("/api/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            _fail(f"specialist login {login.status_code} {login.text}")
        csrf = str(login.json().get("csrf_token") or "")
        headers = {CSRF_HEADER: csrf}
        if http.get("/api/users/default").status_code != 200:
            _fail("specialist should read session user")
        other = http.get("/api/users/1/clients")
        if other.status_code != 403:
            _fail(f"specialist must not read user 1 clients: {other.status_code}")
        export = http.get("/api/admin/master-data-export")
        if export.status_code == 200:
            _fail("specialist obtained master data export")
        forbidden = http.post(
            f"/api/clients/{cid}/admin/imports/1/confirm",
            headers=headers,
            json={},
        )
        if forbidden.status_code == 200:
            _fail("specialist confirmed CRM import")
    finally:
        os.environ.pop(ENFORCE_FLAG, None)


def test_logout_clears_cookie() -> None:
    testdb.isolate_for_tests()
    password = _secret()
    email = f"out.{secrets.token_hex(3)}@example.test"
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (email, full_name, password_hash, is_administrator, active)
            VALUES (?, 'Out', ?, 1, 1)
            """,
            (email, hash_password(password)),
        )
        conn.commit()
    http = _client()
    login = http.post("/api/auth/login", json={"email": email, "password": password})
    csrf = str(login.json().get("csrf_token") or "")
    out = http.post("/api/auth/logout", headers={CSRF_HEADER: csrf})
    if out.status_code != 200:
        _fail(f"logout {out.status_code}")
    cookie = out.headers.get("set-cookie") or ""
    if SESSION_COOKIE in cookie and "max-age=0" not in cookie.lower() and "max_age=0" not in cookie.lower():
        # Starlette may emit Max-Age=0
        if "max-age=0" not in cookie.lower():
            _fail(f"logout did not expire cookie: {cookie[:200]}")


if __name__ == "__main__":
    testdb.isolate_for_tests()
    test_health_and_ready_do_not_leak()
    test_cookie_secure_requires_trusted_https_proxy()
    test_untrusted_forwarded_proto_does_not_set_secure()
    test_restricted_user_cannot_administer()
    test_logout_clears_cookie()
    print("PASS isolated pilot hosting foundation tests")
