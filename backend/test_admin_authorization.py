"""Checkpoint A — session authorization for Administration and email APIs.

Run: python test_admin_authorization.py

Uses isolated testdb copies only — never writes production northstar.db,
never talks to live :8007, and never stores a real staff password in source.
"""

from __future__ import annotations

import testdb
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from auth_http import (
    ADMIN_REQUIRED_DETAIL,
    AUTH_REQUIRED_DETAIL,
    CSRF_HEADER,
    SESSION_COOKIE,
)
from auth_passwords import hash_password
from auth_sessions import hash_session_token, revoke_staff_session
from client_email_accounts_data import ensure_client_email_accounts_schema
from db import get_connection
from cryptography.fernet import Fernet
from email_oauth_credentials import consume_oauth_state, ensure_email_oauth_credentials_schema
from gmail_oauth import complete_google_callback
from main import app

ADMIN_STATUS = "/api/email/google/status"
ADMIN_CONNECT_GET = "/api/email/google/connect/{account_id}"
LIST_ACCOUNTS = "/api/clients/{client_id}/email-accounts"
SUGGESTIONS = "/api/clients/{client_id}/email-accounts/suggestions"
PREVIEW_CONTACTS = "/api/clients/{client_id}/email-preview/contacts"
PREVIEW_APPTS = "/api/clients/{client_id}/email-preview/appointments"
PREVIEW_POST = "/api/clients/{client_id}/email-preview"
SEND = "/api/clients/{client_id}/email-send"
CREATE_ACCOUNT = "/api/clients/{client_id}/email-accounts"
SIGNATURES = "/api/clients/{client_id}/email-signatures"


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
    for needle in (
        "access_token",
        "refresh_token",
        "bearer ",
        "oauth_token",
        "northstar_oauth_token_key",
    ):
        if needle in dumped:
            _fail("JSON leaked an OAuth token or secret field.")
    for item in secrets_out:
        if item and str(item).lower() in dumped:
            _fail("JSON leaked a raw secret.")


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _create_user(*, email: str, password: str, administrator: int = 0, active: int = 1) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, ?, ?, 1, ?, ?, 0, '')
            """,
            (email, "Admin Auth Test User", int(administrator), int(active), digest),
        )
        user_id = int(
            conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        )
        conn.commit()
    return user_id


def _assign(user_id: int, client_id: int, *, role: str = "staff") -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, ?, 1, datetime('now'))
            """,
            (user_id, client_id, role),
        )
        conn.commit()


def _delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_client_assignments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def _login(client: TestClient, email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _csrf(response) -> str:
    return str(response.json().get("csrf_token") or "")


def _insert_google_account(client_id: int, email: str) -> int:
    ensure_client_email_accounts_schema()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO client_email_accounts (
                client_id, email_address, display_name, provider, connection_status,
                active, is_default, created_at, updated_at
            ) VALUES (?, ?, '', 'Google', 'not_connected', 1, 0, '', '')
            """,
            (client_id, email),
        )
        account_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
    return account_id


def _delete_account(account_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM email_oauth_states WHERE email_account_id = ?", (account_id,))
        conn.execute(
            "DELETE FROM client_email_account_assignments WHERE client_email_account_id = ?",
            (account_id,),
        )
        conn.execute("DELETE FROM client_email_accounts WHERE id = ?", (account_id,))
        conn.commit()


def _delete_signature(signature_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM client_email_signatures WHERE id = ?", (signature_id,))
        conn.commit()


def _assert_401(response, path: str) -> None:
    if response.status_code != 401:
        _fail(f"{path} must be 401 without a session, got {response.status_code}.")
    if response.json().get("detail") != AUTH_REQUIRED_DETAIL:
        _fail(f"{path} 401 detail was not generic.")
    _assert_no_secrets(response.json())


def _assert_403(response, path: str) -> None:
    if response.status_code != 403:
        _fail(f"{path} must be 403, got {response.status_code}.")
    if response.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        _fail(f"{path} 403 detail was not generic.")
    _assert_no_secrets(response.json())


def test_unauthenticated_email_routes_are_401_when_enforcement_off() -> None:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    assigned_id, _other_id = _client_ids()
    http = _client()
    for path in (
        LIST_ACCOUNTS.format(client_id=assigned_id),
        SUGGESTIONS.format(client_id=assigned_id),
        ADMIN_STATUS,
        ADMIN_CONNECT_GET.format(account_id=1),
        PREVIEW_CONTACTS.format(client_id=assigned_id),
        SIGNATURES.format(client_id=assigned_id),
    ):
        _assert_401(http.get(path), path)

    appts = http.get(
        PREVIEW_APPTS.format(client_id=assigned_id),
        params={"contact_id": 1},
    )
    _assert_401(appts, "GET email-preview/appointments")

    _assert_401(
        http.post(f"/api/clients/{assigned_id}/email-accounts/1/connect"),
        "POST connect",
    )
    _assert_401(
        http.post(f"/api/clients/{assigned_id}/email-accounts/1/disconnect"),
        "POST disconnect",
    )
    _assert_401(
        http.post(
            CREATE_ACCOUNT.format(client_id=assigned_id),
            json={"email_address": "nobody@example.test"},
        ),
        "POST create email account",
    )
    _assert_401(
        http.put(
            f"/api/clients/{assigned_id}/email-accounts/1",
            json={"email_address": "nobody@example.test"},
        ),
        "PUT update email account",
    )
    _assert_401(
        http.post(f"/api/clients/{assigned_id}/email-accounts/1/deactivate"),
        "POST deactivate email account",
    )
    _assert_401(
        http.post(
            f"/api/clients/{assigned_id}/email-accounts/1/assignments",
            json={"source_rep_name": "Test Rep"},
        ),
        "POST assignments",
    )
    _assert_401(
        http.post(
            SIGNATURES.format(client_id=assigned_id),
            json={"signature_name": "n", "signature_body": "b"},
        ),
        "POST signatures",
    )
    _assert_401(
        http.put(
            f"/api/clients/{assigned_id}/email-signatures/1",
            json={"signature_name": "n", "signature_body": "b"},
        ),
        "PUT signatures",
    )
    _assert_401(
        http.post(f"/api/clients/{assigned_id}/email-signatures/1/deactivate"),
        "POST deactivate signature",
    )
    _assert_401(
        http.post(
            PREVIEW_POST.format(client_id=assigned_id),
            json={"account_id": 1, "contact_id": 1},
        ),
        "POST email-preview",
    )
    _assert_401(
        http.post(
            SEND.format(client_id=assigned_id),
            json={
                "account_id": 1,
                "to_address": "nobody@example.test",
                "subject": "x",
                "body": "y",
            },
        ),
        "POST email-send",
    )


def test_non_admin_cannot_use_google_admin_routes() -> None:
    password = _secret_password()
    email = f"rep.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=0)
    assigned_id, _other_id = _client_ids()
    _assign(user_id, assigned_id, role="staff")
    http = _client()
    try:
        login = _login(http, email, password)
        if login.status_code != 200:
            _fail(f"Non-admin login failed: {login.status_code}")
        csrf = _csrf(login)
        headers = {CSRF_HEADER: csrf}
        _assert_403(http.get(ADMIN_STATUS), "GET google/status")
        _assert_403(
            http.get(ADMIN_CONNECT_GET.format(account_id=1)),
            "GET google/connect",
        )
        connect = http.post(
            f"/api/clients/{assigned_id}/email-accounts/1/connect",
            headers=headers,
            json={"user_id": 1, "email": "juliem@n-star.us", "is_administrator": True},
        )
        _assert_403(connect, "POST connect")
        disconnect = http.post(
            f"/api/clients/{assigned_id}/email-accounts/1/disconnect",
            headers=headers,
            json={"user_id": 1},
        )
        _assert_403(disconnect, "POST disconnect")
    finally:
        _delete_user(user_id)


def test_assigned_non_admin_can_list_preview_and_send() -> None:
    password = _secret_password()
    email = f"rep.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=0)
    assigned_id, other_id = _client_ids()
    _assign(user_id, assigned_id, role="staff")
    account_id = _insert_google_account(
        assigned_id, f"sender.{secrets.token_hex(3)}@example.test"
    )
    http = _client()
    try:
        login = _login(http, email, password)
        if login.status_code != 200:
            _fail(f"Assigned non-admin login failed: {login.status_code}")
        csrf = _csrf(login)
        listed = http.get(LIST_ACCOUNTS.format(client_id=assigned_id))
        if listed.status_code != 200:
            _fail(f"Assigned non-admin list must be 200, got {listed.status_code}.")
        _assert_no_secrets(listed.json())
        suggestions = http.get(SUGGESTIONS.format(client_id=assigned_id))
        if suggestions.status_code != 200:
            _fail(f"Assigned suggestions must be 200, got {suggestions.status_code}.")
        preview = http.get(PREVIEW_CONTACTS.format(client_id=assigned_id))
        if preview.status_code != 200:
            _fail(f"Assigned preview contacts must be 200, got {preview.status_code}.")
        appts = http.get(
            PREVIEW_APPTS.format(client_id=assigned_id),
            params={"contact_id": 1},
        )
        if appts.status_code in {401, 403} and appts.json().get("detail") in {
            AUTH_REQUIRED_DETAIL,
            ADMIN_REQUIRED_DETAIL,
        }:
            _fail("Assigned preview appointments must not be auth-denied.")
        rendered = http.post(
            PREVIEW_POST.format(client_id=assigned_id),
            headers={CSRF_HEADER: csrf},
            json={"account_id": account_id, "contact_id": 1},
        )
        if rendered.status_code in {401, 403} and rendered.json().get("detail") in {
            AUTH_REQUIRED_DETAIL,
            ADMIN_REQUIRED_DETAIL,
        }:
            _fail("Assigned email-preview must not be auth-denied.")
        sent = http.post(
            SEND.format(client_id=assigned_id),
            headers={CSRF_HEADER: csrf},
            json={
                "account_id": account_id,
                "to_address": "nobody@example.test",
                "subject": "x",
                "body": "y",
            },
        )
        if sent.status_code in {401, 403}:
            _fail(f"Assigned email-send must reach the handler, got {sent.status_code}.")

        _assert_403(
            http.get(LIST_ACCOUNTS.format(client_id=other_id)),
            "list unassigned client",
        )
        _assert_403(
            http.post(
                SEND.format(client_id=other_id),
                headers={CSRF_HEADER: csrf},
                json={
                    "account_id": 1,
                    "to_address": "nobody@example.test",
                    "subject": "x",
                    "body": "y",
                    "user_id": 1,
                    "is_administrator": True,
                },
            ),
            "send unassigned client",
        )
    finally:
        _delete_account(account_id)
        _delete_user(user_id)


def test_setup_editor_can_manage_only_authorized_client() -> None:
    password = _secret_password()
    email = f"ops.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=0)
    assigned_id, other_id = _client_ids()
    _assign(user_id, assigned_id, role="rev_ops")
    http = _client()
    created_id = 0
    signature_id = 0
    try:
        login = _login(http, email, password)
        if login.status_code != 200:
            _fail(f"Setup editor login failed: {login.status_code}")
        csrf = _csrf(login)
        headers = {CSRF_HEADER: csrf}
        created = http.post(
            CREATE_ACCOUNT.format(client_id=assigned_id),
            headers=headers,
            json={
                "email_address": f"cfg.{secrets.token_hex(3)}@example.test",
                "display_name": "Config Test",
                "provider": "Other",
            },
        )
        if created.status_code != 200:
            _fail(f"Setup editor create must be 200, got {created.status_code}: {created.text}")
        created_id = int(created.json().get("account_id") or 0)
        updated = http.put(
            f"/api/clients/{assigned_id}/email-accounts/{created_id}",
            headers=headers,
            json={
                "email_address": created.json().get("email_address"),
                "display_name": "Config Test Updated",
                "provider": "Other",
            },
        )
        if updated.status_code != 200:
            _fail(f"Setup editor update must be 200, got {updated.status_code}.")
        assigned_rep = http.post(
            f"/api/clients/{assigned_id}/email-accounts/{created_id}/assignments",
            headers=headers,
            json={"source_rep_name": "Test Revenue Specialist"},
        )
        if assigned_rep.status_code != 200:
            _fail(f"Setup editor assignment must be 200, got {assigned_rep.status_code}.")
        sig = http.post(
            SIGNATURES.format(client_id=assigned_id),
            headers=headers,
            json={"signature_name": "Auth Test Sig", "signature_body": "Thanks"},
        )
        if sig.status_code != 200:
            _fail(f"Setup editor signature create must be 200, got {sig.status_code}.")
        signature_id = int(sig.json().get("signature_id") or 0)
        _assert_403(
            http.post(
                CREATE_ACCOUNT.format(client_id=other_id),
                headers=headers,
                json={"email_address": f"x.{secrets.token_hex(3)}@example.test"},
            ),
            "setup editor create on unauthorized client",
        )
    finally:
        if signature_id:
            _delete_signature(signature_id)
        if created_id:
            _delete_account(created_id)
        _delete_user(user_id)


def test_assigned_without_setup_cannot_manage_configuration() -> None:
    password = _secret_password()
    email = f"staff.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=0)
    assigned_id, _other_id = _client_ids()
    _assign(user_id, assigned_id, role="staff")
    account_id = _insert_google_account(
        assigned_id, f"locked.{secrets.token_hex(3)}@example.test"
    )
    http = _client()
    try:
        login = _login(http, email, password)
        if login.status_code != 200:
            _fail("Staff login failed.")
        csrf = _csrf(login)
        headers = {CSRF_HEADER: csrf}
        _assert_403(
            http.post(
                CREATE_ACCOUNT.format(client_id=assigned_id),
                headers=headers,
                json={"email_address": f"nope.{secrets.token_hex(3)}@example.test"},
            ),
            "staff create email account",
        )
        _assert_403(
            http.put(
                f"/api/clients/{assigned_id}/email-accounts/{account_id}",
                headers=headers,
                json={"email_address": "nope@example.test"},
            ),
            "staff update email account",
        )
        _assert_403(
            http.post(
                f"/api/clients/{assigned_id}/email-accounts/{account_id}/deactivate",
                headers=headers,
            ),
            "staff deactivate email account",
        )
        _assert_403(
            http.post(
                f"/api/clients/{assigned_id}/email-accounts/{account_id}/assignments",
                headers=headers,
                json={"source_rep_name": "Nope"},
            ),
            "staff assignments",
        )
        _assert_403(
            http.post(
                SIGNATURES.format(client_id=assigned_id),
                headers=headers,
                json={"signature_name": "n", "signature_body": "b"},
            ),
            "staff create signature",
        )
    finally:
        _delete_account(account_id)
        _delete_user(user_id)


def test_spoofed_identity_fields_do_not_bypass() -> None:
    password = _secret_password()
    email = f"rep.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=0)
    assigned_id, other_id = _client_ids()
    _assign(user_id, assigned_id, role="staff")
    http = _client()
    try:
        login = _login(http, email, password)
        csrf = _csrf(login)
        headers = {
            CSRF_HEADER: csrf,
            "X-User-Id": "1",
            "X-Email": "juliem@n-star.us",
        }
        bypass_list = http.get(
            LIST_ACCOUNTS.format(client_id=other_id),
            headers=headers,
            params={
                "user_id": 1,
                "email": "juliem@n-star.us",
                "is_administrator": True,
            },
        )
        _assert_403(bypass_list, "spoofed list of unassigned client")
        bypass_status = http.get(
            ADMIN_STATUS,
            headers=headers,
            params={"is_administrator": True, "user_id": 1},
        )
        _assert_403(bypass_status, "spoofed google status")
        bypass_create = http.post(
            CREATE_ACCOUNT.format(client_id=assigned_id),
            headers=headers,
            json={
                "email_address": f"spoof.{secrets.token_hex(3)}@example.test",
                "user_id": 1,
                "is_administrator": True,
            },
        )
        _assert_403(bypass_create, "spoofed create as admin")
    finally:
        _delete_user(user_id)


def test_inactive_revoked_expired_sessions_are_401() -> None:
    password = _secret_password()
    email = f"adm.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=1)
    assigned_id, _other_id = _client_ids()
    path = LIST_ACCOUNTS.format(client_id=assigned_id)
    try:
        expired_client = _client()
        login = _login(expired_client, email, password)
        if login.status_code != 200:
            _fail("Admin login failed for expiry test.")
        raw = str(expired_client.cookies.get(SESSION_COOKIE) or "")
        token_hash = hash_session_token(raw)
        past = "2000-01-01T00:00:00Z"
        with get_connection() as conn:
            conn.execute(
                "UPDATE staff_sessions SET expires_at = ? WHERE token_hash = ?",
                (past, token_hash),
            )
            conn.commit()
        expired = expired_client.get(path)
        if expired.status_code != 401:
            _fail(f"Expired admin session must be 401, got {expired.status_code}.")

        revoked_client = _client()
        login2 = _login(revoked_client, email, password)
        raw2 = str(revoked_client.cookies.get(SESSION_COOKIE) or "")
        with get_connection() as conn:
            revoke_staff_session(conn, raw2)
            conn.commit()
        revoked = revoked_client.get(path)
        if revoked.status_code != 401:
            _fail(f"Revoked admin session must be 401, got {revoked.status_code}.")

        inactive_client = _client()
        login3 = _login(inactive_client, email, password)
        with get_connection() as conn:
            conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
            conn.commit()
        inactive = inactive_client.get(path)
        if inactive.status_code != 401:
            _fail(f"Inactive administrator session must be 401, got {inactive.status_code}.")
        _assert_no_secrets(expired.json(), raw, password)
    finally:
        with get_connection() as conn:
            conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
            conn.commit()
        _delete_user(user_id)


def test_administrator_reaches_gated_handlers() -> None:
    password = _secret_password()
    email = f"adm.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=1)
    assigned_id, _other_id = _client_ids()
    account_id = _insert_google_account(
        assigned_id, f"sender.{secrets.token_hex(3)}@example.test"
    )
    http = _client()
    try:
        login = _login(http, email, password)
        if login.status_code != 200:
            _fail(f"Administrator login failed: {login.status_code}")
        csrf = _csrf(login)
        listed = http.get(LIST_ACCOUNTS.format(client_id=assigned_id))
        if listed.status_code != 200:
            _fail(f"Administrator list must be 200, got {listed.status_code}.")
        _assert_no_secrets(listed.json())
        status = http.get(ADMIN_STATUS)
        if status.status_code != 200:
            _fail(f"Administrator Google status must be 200, got {status.status_code}.")
        if "configured" not in status.json():
            _fail("Google status handler was not reached.")

        with patch("gmail_oauth.google_oauth_configured", return_value=True), patch(
            "gmail_oauth.google_client_id", return_value="test-client-id"
        ), patch(
            "gmail_oauth.google_redirect_uri",
            return_value="http://127.0.0.1:8007/api/email/google/callback",
        ):
            started = http.post(
                f"/api/clients/{assigned_id}/email-accounts/{account_id}/connect",
                headers={CSRF_HEADER: csrf},
            )
            if started.status_code != 200:
                _fail(
                    f"Administrator connect must be 200, got {started.status_code}: {started.text}"
                )
            url = str(started.json().get("authorization_url") or "")
            if "accounts.google.com" not in url:
                _fail("Connect handler must return a Google authorization URL.")
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT user_id, client_id, email_account_id
                    FROM email_oauth_states
                    WHERE email_account_id = ?
                    """,
                    (account_id,),
                ).fetchone()
            if row is None:
                _fail("Connect must store one-time OAuth state.")
            if int(row["user_id"]) != user_id:
                _fail("OAuth state must bind the initiating administrator.")
            if int(row["client_id"]) != assigned_id or int(row["email_account_id"]) != account_id:
                _fail("OAuth state must bind client and email-account ids.")

        disconnected = http.post(
            f"/api/clients/{assigned_id}/email-accounts/{account_id}/disconnect",
            headers={CSRF_HEADER: csrf},
        )
        if disconnected.status_code != 200:
            _fail(f"Administrator disconnect must be 200, got {disconnected.status_code}.")
        _assert_no_secrets(started.json(), password)
    finally:
        _delete_account(account_id)
        _delete_user(user_id)


def test_oauth_callback_state_rules() -> None:
    ensure_email_oauth_credentials_schema()
    password = _secret_password()
    email = f"adm.{secrets.token_hex(4)}@example.test"
    user_id = _create_user(email=email, password=password, administrator=1)
    assigned_id, _other_id = _client_ids()
    sender = f"sender.{secrets.token_hex(3)}@example.test"
    account_id = _insert_google_account(assigned_id, sender)
    http_client = _client()
    try:
        missing = http_client.get("/api/email/google/callback", follow_redirects=False)
        if missing.status_code == 401:
            _fail("OAuth callback must remain unauthenticated at middleware.")
        if missing.status_code != 302:
            _fail(f"Missing callback params should redirect, got {missing.status_code}.")

        invalid = http_client.get(
            "/api/email/google/callback",
            params={"code": "x", "state": "no-such-state"},
            follow_redirects=False,
        )
        if invalid.status_code == 401:
            _fail("Invalid OAuth state must not be middleware 401.")
        location = invalid.headers.get("location") or ""
        if "email_oauth=error" not in location:
            _fail("Invalid OAuth state must bounce as an error.")
        if "no-such-state" in location:
            _fail("Callback must not expose OAuth state.")

        from email_oauth_credentials import store_oauth_state

        reused_state = f"admin.reuse.{secrets.token_hex(8)}"
        store_oauth_state(
            state=reused_state,
            client_id=assigned_id,
            email_account_id=account_id,
            user_id=user_id,
        )
        first = consume_oauth_state(reused_state)
        if first is None:
            _fail("Fresh admin-bound OAuth state should consume once.")
        if consume_oauth_state(reused_state) is not None:
            _fail("OAuth state must be one-time.")

        expired_state = f"admin.exp.{secrets.token_hex(8)}"
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO email_oauth_states
                    (state, client_id, email_account_id, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (expired_state, assigned_id, account_id, user_id, past, past),
            )
            conn.commit()
        if consume_oauth_state(expired_state) is not None:
            _fail("Expired OAuth state must be rejected.")

        mismatch_state = f"admin.mis.{secrets.token_hex(8)}"
        store_oauth_state(
            state=mismatch_state,
            client_id=assigned_id,
            email_account_id=account_id + 99999,
            user_id=user_id,
        )
        with patch("gmail_oauth.google_oauth_configured", return_value=True):
            try:
                complete_google_callback(code="x", state=mismatch_state)
                _fail("Mismatched account id must not complete OAuth.")
            except ValueError:
                pass

        non_admin_id = _create_user(
            email=f"rep.{secrets.token_hex(4)}@example.test",
            password=_secret_password(),
            administrator=0,
        )
        try:
            bound_state = f"admin.ok.{secrets.token_hex(8)}"
            store_oauth_state(
                state=bound_state,
                client_id=assigned_id,
                email_account_id=account_id,
                user_id=non_admin_id,
            )
            with patch("gmail_oauth.google_oauth_configured", return_value=True):
                try:
                    complete_google_callback(code="x", state=bound_state)
                    _fail("State bound to a non-administrator must not complete OAuth.")
                except ValueError:
                    pass
        finally:
            _delete_user(non_admin_id)

        valid_state = f"admin.valid.{secrets.token_hex(8)}"
        store_oauth_state(
            state=valid_state,
            client_id=assigned_id,
            email_account_id=account_id,
            user_id=user_id,
        )
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/gmail.send",
        }
        info_resp = MagicMock()
        info_resp.status_code = 200
        info_resp.json.return_value = {"email": sender, "sub": "sub-1"}
        fake_http = MagicMock()
        fake_http.__enter__.return_value = fake_http
        fake_http.__exit__.return_value = False
        fake_http.post.return_value = token_resp
        fake_http.get.return_value = info_resp
        with patch("gmail_oauth.google_oauth_configured", return_value=True), patch(
            "gmail_oauth.google_client_id", return_value="test-client"
        ), patch("gmail_oauth.google_client_secret", return_value="test-secret"), patch(
            "email_oauth_credentials.oauth_token_encryption_key",
            return_value=Fernet.generate_key().decode(),
        ), patch("gmail_oauth.httpx.Client", return_value=fake_http):
            result = complete_google_callback(code="auth-code", state=valid_state)
        if not result.get("ok"):
            _fail("Valid unused admin-bound state must complete for the bound account.")
        if int(result.get("account_id") or 0) != account_id:
            _fail("Callback must update only the state-bound account.")
        if consume_oauth_state(valid_state) is not None:
            _fail("Successful callback must consume the OAuth state.")
        _assert_no_secrets(result, "access-token", "refresh-token", valid_state)
    finally:
        _delete_account(account_id)
        _delete_user(user_id)


def main() -> int:
    test_unauthenticated_email_routes_are_401_when_enforcement_off()
    test_non_admin_cannot_use_google_admin_routes()
    test_assigned_non_admin_can_list_preview_and_send()
    test_setup_editor_can_manage_only_authorized_client()
    test_assigned_without_setup_cannot_manage_configuration()
    test_spoofed_identity_fields_do_not_bypass()
    test_inactive_revoked_expired_sessions_are_401()
    test_administrator_reaches_gated_handlers()
    test_oauth_callback_state_rules()
    print("test_admin_authorization: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
