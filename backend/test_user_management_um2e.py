"""UM-2E — read-only staff administration history.

Uses the isolated testdb copy. Does not write live users, passwords, or events.
"""

from __future__ import annotations

import json
import os
import secrets

import testdb  # noqa: F401 — isolates NORTHSTAR_DB_PATH before app imports

from fastapi.testclient import TestClient

from auth_http import ADMIN_REQUIRED_DETAIL, AUTH_REQUIRED_DETAIL, CSRF_HEADER
from auth_passwords import hash_password
from db import get_connection
from main import app
from staff_rbac import REVOPS_MANAGER, REVOPS_SPECIALIST, SYSTEM_ADMINISTRATOR
from user_management import sanitize_staff_admin_detail

SECRET = "NsHidden9secret"
ARGON = "$argon2id$v=19$m=65536,t=3,p=4$secret"
TOKEN = "session-token-value"
CSRF = "csrf-secret-value"
COOKIE = "cookie-secret-value"
CREDENTIAL = "credential-secret-value"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _password() -> str:
    return f"NsTest9{secrets.token_hex(4)}a"


def _email() -> str:
    return f"um2e.{secrets.token_hex(6)}@n-star.us"


def _client() -> TestClient:
    return TestClient(app)


def _insert_user(*, email: str, name: str, administrator: bool, password: str, role: str) -> int:
    digest = hash_password(password, email=email) if password else ""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, ?, 1, 1, ?, 0, '', ?)
            """,
            (email, name, 1 if administrator else 0, digest, role),
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


def _history(http: TestClient, user_id: int):
    return http.get(f"/api/admin/users/{user_id}/history")


def _fingerprint() -> tuple:
    with get_connection() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM staff_admin_events").fetchone()[0]
        sessions = conn.execute("SELECT COUNT(*) FROM staff_sessions").fetchone()[0]
        assignments = conn.execute("SELECT COUNT(*) FROM user_client_assignments").fetchone()[0]
    return int(users), int(events), int(sessions), int(assignments)


def _insert_event(target_id: int, actor_id: int, event_type: str, created_at: str, detail: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO staff_admin_events (
                actor_user_id, target_user_id, event_type, created_at, detail_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (actor_id, target_id, event_type, created_at, detail),
        )
        conn.commit()


def test_history_order_actor_and_auth() -> None:
    password = _password()
    email = _email()
    actor_id = _insert_user(
        email=email,
        name="History Actor",
        administrator=True,
        password=password,
        role=SYSTEM_ADMINISTRATOR,
    )
    target_id = _insert_user(
        email=_email(),
        name="History Target",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    http = _client()
    csrf = _login(http, email, password)
    client_id = 0
    with get_connection() as conn:
        client_id = int(conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"])
    try:
        before = _fingerprint()
        empty = _history(http, target_id)
        if empty.status_code != 200 or empty.json().get("events") != []:
            _fail(f"Empty history was not a clean empty list: {empty.status_code} {empty.text}")
        created = http.post(
            "/api/admin/users",
            json={
                "full_name": "History Created",
                "email": _email(),
                "staff_role": REVOPS_SPECIALIST,
                "client_ids": [client_id],
                "password": _password(),
            },
            headers={CSRF_HEADER: csrf},
        )
        if created.status_code != 200:
            _fail(created.text)
        created_id = int(created.json()["id"])
        renamed = http.patch(
            f"/api/admin/users/{created_id}",
            json={"staff_role": REVOPS_MANAGER},
            headers={CSRF_HEADER: csrf},
        )
        if renamed.status_code != 200:
            _fail(renamed.text)
        history = _history(http, created_id)
        if history.status_code != 200:
            _fail(history.text)
        events = history.json()["events"]
        if [event["event_type"] for event in events[:2]] != ["role_changed", "user_created"]:
            _fail(f"History was not newest first: {events}")
        if events[0]["actor_full_name"] != "History Actor" or events[0]["actor_email"] != email:
            _fail(f"Actor identity was not returned safely: {events[0]}")
        if events[0]["detail"].get("old") != REVOPS_SPECIALIST or events[0]["detail"].get("new") != REVOPS_MANAGER:
            _fail("Role change detail was not sanitized into old/new values.")
        created_clients = events[1]["detail"].get("clients") or []
        if not created_clients or "client_name" not in created_clients[0]:
            _fail("Created-user history did not include a safe client name.")
        missing = _history(http, 999999)
        if missing.status_code != 404:
            _fail(f"Unknown target must be 404, got {missing.status_code}")
        outsider = _client().get(f"/api/admin/users/{created_id}/history")
        if outsider.status_code != 401 or outsider.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"Unauthenticated history must be 401, got {outsider.status_code}")
        specialist_password = _password()
        specialist_email = _email()
        specialist_id = _insert_user(
            email=specialist_email,
            name="History Specialist",
            administrator=False,
            password=specialist_password,
            role=REVOPS_SPECIALIST,
        )
        specialist = _client()
        _login(specialist, specialist_email, specialist_password)
        forbidden = specialist.get(f"/api/admin/users/{created_id}/history")
        if forbidden.status_code != 403 or forbidden.json().get("detail") != ADMIN_REQUIRED_DETAIL:
            _fail(f"Specialist history must be 403, got {forbidden.status_code} {forbidden.text}")
        after_reads = _history(http, target_id)
        if after_reads.status_code != 200:
            _fail(after_reads.text)
        if _fingerprint()[0] != before[0] + 2:
            _fail("History reads changed the user count beyond the users this test created.")
        _delete_user(created_id)
        _delete_user(specialist_id)
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_sanitizer_drops_secrets_and_malformed_text() -> None:
    password = _password()
    email = _email()
    actor_id = _insert_user(
        email=email,
        name="Sanitize Actor",
        administrator=True,
        password=password,
        role=SYSTEM_ADMINISTRATOR,
    )
    target_id = _insert_user(
        email=_email(),
        name="Sanitize Target",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    poisoned = {
        "full_name": "Visible Name",
        "email": "visible@n-star.us",
        "password": SECRET,
        "password_hash": ARGON,
        "token": TOKEN,
        "token_hash": TOKEN,
        "csrf": CSRF,
        "csrf_secret": CSRF,
        "secret": SECRET,
        "credential": CREDENTIAL,
        "cookie": COOKIE,
        "session_cookie": COOKIE,
        "nested": {
            "password": SECRET,
            "token": TOKEN,
            "csrf_secret": CSRF,
        },
        "staff_role": {
            "old": REVOPS_SPECIALIST,
            "password": SECRET,
            "token_hash": TOKEN,
            "credential": CREDENTIAL,
            "cookie": COOKIE,
        },
        "password_updated_at": "2026-09-27T18:00:00Z",
        "active": True,
    }
    http = _client()
    _login(http, email, password)
    try:
        direct, available = sanitize_staff_admin_detail(json.dumps(poisoned))
        if not available:
            _fail("A valid JSON object should stay available.")
        dumped = json.dumps(direct)
        for secret in (SECRET, ARGON, TOKEN, CSRF, COOKIE, CREDENTIAL):
            if secret in dumped:
                _fail(f"Sanitizer kept a secret value: {secret}")
        if direct.get("full_name") != "Visible Name" or direct.get("password_updated_at") != "2026-09-27T18:00:00Z":
            _fail(f"Sanitizer dropped safe fields: {direct}")
        if direct.get("staff_role") != {"old": REVOPS_SPECIALIST}:
            _fail(f"Nested safe field was not kept without secrets: {direct.get('staff_role')}")
        broken, broken_ok = sanitize_staff_admin_detail('not-json {"password":"' + SECRET + '"}')
        if broken_ok or broken:
            _fail("Malformed detail should be an empty unavailable result.")
        if SECRET in json.dumps(broken):
            _fail("Malformed detail returned raw content.")
        _insert_event(target_id, actor_id, "role_changed", "2026-09-27T18:00:00Z", json.dumps(poisoned))
        _insert_event(target_id, actor_id, "name_changed", "2026-09-27T17:00:00Z", "this is not json " + SECRET)
        before = _fingerprint()
        response = _history(http, target_id)
        if response.status_code != 200:
            _fail(response.text)
        body = response.text
        for secret in (SECRET, ARGON, TOKEN, CSRF, COOKIE, CREDENTIAL, "this is not json"):
            if secret in body:
                _fail(f"History response leaked secret or raw detail: {secret}")
        events = response.json()["events"]
        if events[0]["detail_available"] is not True or events[1]["detail_available"] is not False:
            _fail(f"Malformed detail availability was wrong: {events}")
        if events[1]["detail"] != {}:
            _fail(f"Malformed detail was returned: {events[1]}")
        if _fingerprint() != before:
            _fail("Reading history mutated the isolated database.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    tests = [
        test_history_order_actor_and_auth,
        test_sanitizer_drops_secrets_and_malformed_text,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
