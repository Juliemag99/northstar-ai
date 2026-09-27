"""UM-2C — protected staff editing and client access.

Every write uses the isolated testdb copy. This file does not edit live
users, live assignments, or create Robert, Tyler, or Todd.
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

from access import user_can_access_client
from auth_http import ADMIN_REQUIRED_DETAIL, CSRF_HEADER
from auth_passwords import hash_password
from auth_sessions import create_staff_session, hash_session_token
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from staff_rbac import REVOPS_MANAGER, REVOPS_SPECIALIST, SYSTEM_ADMINISTRATOR
from user_management import (
    LAST_USABLE_ADMIN_MESSAGE,
    SELF_DEACTIVATE_MESSAGE,
    SELF_DEMOTE_MESSAGE,
)

PILOT_EMAILS = ("robertk@n-star.us", "tylers@n-star.us", "toddw@n-star.us")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _password() -> str:
    return f"NsTest9{secrets.token_hex(4)}a"


def _email() -> str:
    return f"um2c.{secrets.token_hex(6)}@n-star.us"


def _client() -> TestClient:
    return TestClient(app)


def _client_ids() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    if len(rows) < 2:
        _fail("Isolated testdb needs two clients.")
    return int(rows[0]["id"]), int(rows[1]["id"])


def _insert_user(
    *,
    email: str,
    name: str,
    administrator: bool,
    active: bool = True,
    password: str = "",
    role: str,
    locked_until: str = "",
) -> int:
    digest = hash_password(password, email=email) if password else ""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, ?, 1, ?, ?, 0, ?, ?)
            """,
            (
                email,
                name,
                1 if administrator else 0,
                1 if active else 0,
                digest,
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


def _patch(http: TestClient, csrf: str, user_id: int, body: dict):
    return http.patch(
        f"/api/admin/users/{user_id}",
        json=body,
        headers={CSRF_HEADER: csrf},
    )


def _put_clients(http: TestClient, csrf: str, user_id: int, client_ids: list[int]):
    return http.put(
        f"/api/admin/users/{user_id}/clients",
        json={"client_ids": client_ids},
        headers={CSRF_HEADER: csrf},
    )


def _row(user_id: int):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _assignments(user_id: int) -> list[tuple]:
    with get_connection() as conn:
        return [
            (int(row["client_id"]), str(row["role"] or ""), int(row["active"]))
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


def _assign(user_id: int, client_id: int, role: str, active: int = 1) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_client_assignments (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, ?, ?, '2026-09-27T00:00:00Z')
            """,
            (user_id, client_id, role, active),
        )
        conn.commit()


def _neutralize_existing_admins() -> list[tuple[int, int]]:
    """Isolated copy only. Live northstar.db is a different file."""
    with get_connection() as conn:
        saved = [
            (int(row["id"]), int(row["is_administrator"]))
            for row in conn.execute("SELECT id, is_administrator FROM users").fetchall()
        ]
        conn.execute("UPDATE users SET is_administrator = 0")
        conn.commit()
    return saved


def _restore_admins(saved: list[tuple[int, int]]) -> None:
    with get_connection() as conn:
        for user_id, is_admin in saved:
            conn.execute(
                "UPDATE users SET is_administrator = ? WHERE id = ?",
                (is_admin, user_id),
            )
        conn.commit()


def _actor() -> tuple[TestClient, str, int, str]:
    password = _password()
    email = _email()
    user_id = _insert_user(
        email=email,
        name="UM2C Actor",
        administrator=True,
        password=password,
        role=SYSTEM_ADMINISTRATOR,
    )
    http = _client()
    csrf = _login(http, email, password)
    return http, csrf, user_id, password


def test_administrator_can_edit_another_users_name() -> None:
    http, csrf, actor_id, _password_value = _actor()
    email = _email()
    target_id = _insert_user(
        email=email,
        name="Before Name",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    try:
        response = _patch(http, csrf, target_id, {"full_name": "  After   Name  "})
        if response.status_code != 200:
            _fail(response.text)
        if response.json().get("full_name") != "After Name":
            _fail("Name was not trimmed.")
        if _row(target_id)["full_name"] != "After Name":
            _fail("Stored name was not updated.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_email_normalized_lowercase() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Email Case",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    try:
        new_email = f"UM2C.Case.{secrets.token_hex(3)}@N-STAR.US"
        response = _patch(http, csrf, target_id, {"email": f"  {new_email}  "})
        if response.status_code != 200:
            _fail(response.text)
        if response.json().get("email") != new_email.strip().lower():
            _fail(f"Email was not normalized: {response.json().get('email')}")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_duplicate_email_rejected() -> None:
    http, csrf, actor_id, _password_value = _actor()
    first = _email()
    first_id = _insert_user(
        email=first,
        name="First",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    second_id = _insert_user(
        email=_email(),
        name="Second",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    try:
        response = _patch(http, csrf, second_id, {"email": first.upper()})
        if response.status_code != 409:
            _fail(f"Duplicate email must be 409, got {response.status_code}.")
        if _row(second_id)["email"] == first:
            _fail("Duplicate email was stored.")
    finally:
        _delete_user(first_id)
        _delete_user(second_id)
        _delete_user(actor_id)


def test_non_domain_email_rejected() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Domain",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    original = _row(target_id)["email"]
    try:
        response = _patch(http, csrf, target_id, {"email": "person@example.com"})
        if response.status_code != 400:
            _fail(f"Non-domain email must be 400, got {response.status_code}.")
        if _row(target_id)["email"] != original:
            _fail("Rejected email change was stored.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_invalid_role_rejected() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Bad Role",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    try:
        for role in ("wizard", "operations_admin", "system_administrator"):
            response = _patch(http, csrf, target_id, {"staff_role": role})
            if response.status_code != 400:
                _fail(f"Role {role} must be 400, got {response.status_code}.")
        if _row(target_id)["staff_role"] != REVOPS_SPECIALIST:
            _fail("Invalid role was stored.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_promotion_forces_system_administrator() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Promote",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    try:
        response = _patch(
            http,
            csrf,
            target_id,
            {"is_administrator": True, "staff_role": REVOPS_SPECIALIST},
        )
        if response.status_code != 200:
            _fail(response.text)
        row = _row(target_id)
        if int(row["is_administrator"]) != 1 or row["staff_role"] != SYSTEM_ADMINISTRATOR:
            _fail("Promotion did not force System administrator.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_demotion_requires_non_admin_role() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Demote",
        administrator=True,
        password=_password(),
        role=SYSTEM_ADMINISTRATOR,
    )
    try:
        missing = _patch(http, csrf, target_id, {"is_administrator": False})
        if missing.status_code != 400:
            _fail(f"Demotion without a role must be 400, got {missing.status_code}.")
        kept = _patch(
            http,
            csrf,
            target_id,
            {"is_administrator": False, "staff_role": SYSTEM_ADMINISTRATOR},
        )
        if kept.status_code != 400:
            _fail("Demotion cannot keep System administrator.")
        if int(_row(target_id)["is_administrator"]) != 1:
            _fail("Rejected demotion changed administrator status.")
        ok = _patch(
            http,
            csrf,
            target_id,
            {"is_administrator": False, "staff_role": REVOPS_MANAGER},
        )
        if ok.status_code != 200:
            _fail(ok.text)
        row = _row(target_id)
        if int(row["is_administrator"]) != 0 or row["staff_role"] != REVOPS_MANAGER:
            _fail("Valid demotion was not stored.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_self_deactivation_and_demotion_rejected() -> None:
    password = _password()
    email = _email()
    actor_id = _insert_user(
        email=email,
        name="Self Admin",
        administrator=True,
        password=password,
        role=SYSTEM_ADMINISTRATOR,
    )
    other_id = _insert_user(
        email=_email(),
        name="Other Usable",
        administrator=True,
        password=_password(),
        role=SYSTEM_ADMINISTRATOR,
    )
    http = _client()
    csrf = _login(http, email, password)
    try:
        deactivate = _patch(http, csrf, actor_id, {"active": False})
        if deactivate.status_code != 409 or deactivate.json().get("detail") != SELF_DEACTIVATE_MESSAGE:
            _fail(f"Self-deactivation was not rejected clearly: {deactivate.status_code} {deactivate.text}")
        demote = _patch(
            http,
            csrf,
            actor_id,
            {"is_administrator": False, "staff_role": REVOPS_SPECIALIST},
        )
        if demote.status_code != 409 or demote.json().get("detail") != SELF_DEMOTE_MESSAGE:
            _fail(f"Self-demotion was not rejected clearly: {demote.status_code} {demote.text}")
        row = _row(actor_id)
        if int(row["active"]) != 1 or int(row["is_administrator"]) != 1:
            _fail("Self-protection changed the actor.")
        renamed = _patch(http, csrf, actor_id, {"full_name": "Self Renamed"})
        if renamed.status_code != 200 or renamed.json().get("full_name") != "Self Renamed":
            _fail("An administrator must still be able to change their own name.")
    finally:
        _delete_user(other_id)
        _delete_user(actor_id)


def test_last_usable_admin_shape_and_lockout() -> None:
    """One usable admin plus one active no-password admin, matching live shape."""
    saved = _neutralize_existing_admins()
    password_a = _password()
    email_a = _email()
    admin_a = _insert_user(
        email=email_a,
        name="Usable Admin A",
        administrator=True,
        password=password_a,
        role=SYSTEM_ADMINISTRATOR,
    )
    admin_b = _insert_user(
        email=_email(),
        name="No Password Admin B",
        administrator=True,
        password="",
        role=SYSTEM_ADMINISTRATOR,
    )
    created = [admin_a, admin_b]
    try:
        with get_connection() as conn:
            session = create_staff_session(conn, user_id=admin_b)
            conn.commit()
        http = _client()
        http.cookies.set("northstar_session", session["token"])
        csrf = str(session["csrf_secret"])
        deactivated = _patch(http, csrf, admin_a, {"active": False})
        if deactivated.status_code != 409 or deactivated.json().get("detail") != LAST_USABLE_ADMIN_MESSAGE:
            _fail(f"No-password admin must not unlock deactivation: {deactivated.status_code} {deactivated.text}")
        demoted = _patch(
            http,
            csrf,
            admin_a,
            {"is_administrator": False, "staff_role": REVOPS_SPECIALIST},
        )
        if demoted.status_code != 409 or demoted.json().get("detail") != LAST_USABLE_ADMIN_MESSAGE:
            _fail("No-password admin must not unlock demotion.")
        row = _row(admin_a)
        if int(row["active"]) != 1 or int(row["is_administrator"]) != 1:
            _fail("Last usable administrator was changed.")

        digest = hash_password(password_a, email=_row(admin_b)["email"])
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (digest, admin_b),
            )
            conn.commit()
        allowed = _patch(http, csrf, admin_a, {"active": False})
        if allowed.status_code != 200:
            _fail(f"A second usable admin should allow the change: {allowed.status_code} {allowed.text}")
        if int(_row(admin_a)["active"]) != 0:
            _fail("Admin A was not deactivated after Admin B became usable.")

        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET active = 1, is_administrator = 1 WHERE id = ?",
                (admin_a,),
            )
            conn.commit()
        self_http = _client()
        self_csrf = _login(self_http, email_a, password_a)
        blocked = _patch(self_http, self_csrf, admin_a, {"active": False})
        if blocked.status_code != 409 or blocked.json().get("detail") != SELF_DEACTIVATE_MESSAGE:
            _fail("Self-protection still applies after another usable admin exists.")

        with get_connection() as conn:
            conn.execute("UPDATE users SET password_hash = '' WHERE id = ?", (admin_b,))
            conn.commit()
        locked_password = _password()
        locked_email = _email()
        locked_id = _insert_user(
            email=locked_email,
            name="Locked Admin",
            administrator=True,
            password=locked_password,
            role=SYSTEM_ADMINISTRATOR,
        )
        created.append(locked_id)
        locked_http = _client()
        locked_csrf = _login(locked_http, locked_email, locked_password)
        future = (datetime.now(timezone.utc) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET locked_until = ? WHERE id = ?",
                (future, locked_id),
            )
            conn.commit()
        released = _patch(locked_http, locked_csrf, admin_a, {"full_name": "Still Protected"})
        if released.status_code != 200:
            _fail(released.text)
        removed = _patch(locked_http, locked_csrf, admin_a, {"active": False})
        if removed.status_code != 200:
            _fail(
                "Temporary lockout must not stop the locked administrator from counting: "
                f"{removed.status_code} {removed.text}"
            )
    finally:
        for user_id in created:
            _delete_user(user_id)
        _restore_admins(saved)


def test_specialist_receives_403_and_unknown_user_404() -> None:
    http, csrf, actor_id, _password_value = _actor()
    password = _password()
    email = _email()
    specialist_id = _insert_user(
        email=email,
        name="Specialist",
        administrator=False,
        password=password,
        role=REVOPS_SPECIALIST,
    )
    try:
        missing = _patch(http, csrf, 999999, {"full_name": "Nobody"})
        if missing.status_code != 404:
            _fail(f"Unknown user must be 404, got {missing.status_code}.")
        specialist = _client()
        specialist_csrf = _login(specialist, email, password)
        rejected = _patch(specialist, specialist_csrf, actor_id, {"full_name": "Nope"})
        if rejected.status_code != 403 or rejected.json().get("detail") != ADMIN_REQUIRED_DETAIL:
            _fail(f"Specialist PATCH must be 403, got {rejected.status_code} {rejected.text}")
        rejected_clients = _put_clients(specialist, specialist_csrf, actor_id, [1])
        if rejected_clients.status_code != 403:
            _fail("Specialist client update must be 403.")
        extra = _patch(http, csrf, specialist_id, {"password": "NsTest9secret"})
        if extra.status_code != 422:
            _fail(f"Password must not be accepted, got {extra.status_code}.")
    finally:
        _delete_user(specialist_id)
        _delete_user(actor_id)


def test_inactive_user_cannot_authenticate_and_session_rules() -> None:
    http, csrf, actor_id, _password_value = _actor()
    password = _password()
    email = _email()
    target_id = _insert_user(
        email=email,
        name="Session Target",
        administrator=False,
        password=password,
        role=REVOPS_SPECIALIST,
    )
    client_a, client_b = _client_ids()
    _assign(target_id, client_a, REVOPS_SPECIALIST, 1)
    _assign(target_id, client_b, REVOPS_SPECIALIST, 0)
    try:
        name_token = _open_session(target_id)
        renamed = _patch(http, csrf, target_id, {"full_name": "Session Target Renamed"})
        if renamed.status_code != 200 or int(renamed.json().get("sessions_revoked") or 0) != 0:
            _fail("A name-only change must not revoke sessions.")
        if _session_revoked(name_token):
            _fail("Name-only change revoked a session.")

        email_token = _open_session(target_id)
        emailed = _patch(http, csrf, target_id, {"email": _email()})
        if emailed.status_code != 200 or int(emailed.json().get("sessions_revoked") or 0) < 1:
            _fail("Email change must report revoked sessions.")
        if not _session_revoked(email_token):
            _fail("Email change left a session active.")

        role_token = _open_session(target_id)
        role = _patch(http, csrf, target_id, {"staff_role": REVOPS_MANAGER})
        if role.status_code != 200 or not _session_revoked(role_token):
            _fail("Role change must revoke sessions.")
        assignments = {client_id: (stored_role, active) for client_id, stored_role, active in _assignments(target_id)}
        if assignments[client_a] != (REVOPS_MANAGER, 1):
            _fail(f"Active assignment role was not synchronized: {assignments}")
        if assignments[client_b][0] != REVOPS_SPECIALIST or assignments[client_b][1] != 0:
            _fail("Inactive assignment role should stay until that client is granted again.")

        promoted_token = _open_session(target_id)
        promoted = _patch(http, csrf, target_id, {"is_administrator": True})
        if promoted.status_code != 200 or not _session_revoked(promoted_token):
            _fail("Administrator change must revoke sessions.")

        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET is_administrator = 0, staff_role = ? WHERE id = ?",
                (REVOPS_MANAGER, target_id),
            )
            conn.commit()
        off_token = _open_session(target_id)
        deactivated = _patch(http, csrf, target_id, {"active": False})
        if deactivated.status_code != 200 or not _session_revoked(off_token):
            _fail("Deactivation must revoke sessions.")
        if deactivated.json().get("login_status") != "inactive":
            _fail("Deactivated detail must report inactive login status.")
        login = _client().post("/api/auth/login", json={"email": _row(target_id)["email"], "password": password})
        if login.status_code == 200:
            _fail("Inactive user authenticated.")

        locked_until = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_connection() as conn:
            before = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()["password_hash"]
            conn.execute(
                "UPDATE users SET locked_until = ? WHERE id = ?",
                (locked_until, target_id),
            )
            conn.commit()
        restored = _patch(http, csrf, target_id, {"active": True})
        if restored.status_code != 200:
            _fail(restored.text)
        after = _row(target_id)
        if str(after["password_hash"]) != str(before) or str(after["locked_until"]) != locked_until:
            _fail("Reactivation invented a password or cleared lockout.")
        if int(restored.json().get("sessions_revoked") or 0) != 0:
            _fail("Reactivation must not revoke sessions.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_own_email_change_revokes_actor_session() -> None:
    password = _password()
    email = _email()
    actor_id = _insert_user(
        email=email,
        name="Own Email",
        administrator=True,
        password=password,
        role=SYSTEM_ADMINISTRATOR,
    )
    http = _client()
    csrf = _login(http, email, password)
    try:
        new_email = _email()
        response = _patch(http, csrf, actor_id, {"email": new_email})
        if response.status_code != 200 or int(response.json().get("sessions_revoked") or 0) < 1:
            _fail("Changing the actor's own email must revoke that session and say so.")
        follow = http.get("/api/admin/users")
        if follow.status_code != 401:
            _fail("The revoked session was still accepted.")
    finally:
        _delete_user(actor_id)


def test_audit_events_and_noop_and_crm_untouched() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Audit Target",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    with get_connection() as conn:
        rel = conn.execute(
            "SELECT id, assigned_user_id FROM client_company_relationships ORDER BY id LIMIT 1"
        ).fetchone()
        flow = conn.execute(
            "SELECT id, assigned_user_id FROM contact_client_workflows ORDER BY id LIMIT 1"
        ).fetchone()
        queue = conn.execute(
            "SELECT id, assigned_user_id FROM work_queue_items ORDER BY id LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE client_company_relationships SET assigned_user_id = ? WHERE id = ?",
            (target_id, int(rel["id"])),
        )
        conn.execute(
            "UPDATE contact_client_workflows SET assigned_user_id = ? WHERE id = ?",
            (target_id, int(flow["id"])),
        )
        conn.execute(
            "UPDATE work_queue_items SET assigned_user_id = ? WHERE id = ?",
            (target_id, int(queue["id"])),
        )
        conn.commit()
    before = _crm_fingerprint()
    try:
        same = _patch(http, csrf, target_id, {"full_name": "Audit Target"})
        if same.status_code != 200 or _events(target_id):
            _fail("A no-op edit created audit events.")
        _open_session(target_id)
        changed = _patch(
            http,
            csrf,
            target_id,
            {"full_name": "Audit Target Two", "email": _email()},
        )
        if changed.status_code != 200:
            _fail(changed.text)
        kinds = [kind for kind, _detail in _events(target_id)]
        if kinds != ["name_changed", "email_changed", "sessions_revoked"]:
            _fail(f"Unexpected audit events: {kinds}")
        for _kind, detail in _events(target_id):
            lowered = detail.lower()
            if "password" in lowered or "csrf" in lowered or "token" in lowered or "$argon2" in lowered:
                _fail("Audit detail included a credential.")
        if _crm_fingerprint() != before:
            _fail("User edit changed CRM assigned_user_id values.")
    finally:
        with get_connection() as conn:
            conn.execute(
                "UPDATE client_company_relationships SET assigned_user_id = ? WHERE id = ?",
                (rel["assigned_user_id"], int(rel["id"])),
            )
            conn.execute(
                "UPDATE contact_client_workflows SET assigned_user_id = ? WHERE id = ?",
                (flow["assigned_user_id"], int(flow["id"])),
            )
            conn.execute(
                "UPDATE work_queue_items SET assigned_user_id = ? WHERE id = ?",
                (queue["assigned_user_id"], int(queue["id"])),
            )
            conn.commit()
        _delete_user(target_id)
        _delete_user(actor_id)


def test_patch_rolls_back_when_audit_fails() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Rollback User",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    try:
        with patch("user_management._record_admin_event", side_effect=RuntimeError("audit failed")):
            try:
                _patch(http, csrf, target_id, {"full_name": "Should Not Stick"})
            except RuntimeError as exc:
                if "audit failed" not in str(exc):
                    raise
        if _row(target_id)["full_name"] != "Rollback User":
            _fail("Audit failure left the name change in place.")
        if _events(target_id):
            _fail("Audit failure left an audit row.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_client_access_rules() -> None:
    http, csrf, actor_id, _password_value = _actor()
    password = _password()
    email = _email()
    target_id = _insert_user(
        email=email,
        name="Client Access",
        administrator=False,
        password=password,
        role=REVOPS_SPECIALIST,
    )
    client_a, client_b = _client_ids()
    _assign(target_id, client_a, REVOPS_SPECIALIST, 1)
    _assign(target_id, client_b, "old-role", 0)
    before = _crm_fingerprint()
    token = _open_session(target_id)
    try:
        missing = _put_clients(http, csrf, target_id, [client_a, 999999999])
        if missing.status_code != 400:
            _fail(f"Missing client must be 400, got {missing.status_code}.")
        if _assignments(target_id) != [(client_a, REVOPS_SPECIALIST, 1), (client_b, "old-role", 0)]:
            _fail("Rejected client update changed assignments.")

        granted = _put_clients(http, csrf, target_id, [client_a, client_b, client_b])
        if granted.status_code != 200:
            _fail(granted.text)
        rows = _assignments(target_id)
        if rows != [(client_a, REVOPS_SPECIALIST, 1), (client_b, REVOPS_SPECIALIST, 1)]:
            _fail(f"Grant/reactivate/dedupe failed: {rows}")
        if sum(1 for client_id, _role, _active in rows if client_id == client_b) != 1:
            _fail("Duplicate client ids created a second row.")
        grant_events = [kind for kind, _detail in _events(target_id)]
        if grant_events != ["client_granted"]:
            _fail(f"Reactivating one client should audit one grant, got {grant_events}.")
        if _session_revoked(token):
            _fail("Client access change revoked a session.")
        if _crm_fingerprint() != before:
            _fail("Client access change updated CRM ownership.")

        removed = _put_clients(http, csrf, target_id, [client_a])
        if removed.status_code != 200:
            _fail(removed.text)
        after_remove = _assignments(target_id)
        if after_remove != [(client_a, REVOPS_SPECIALIST, 1), (client_b, REVOPS_SPECIALIST, 0)]:
            _fail(f"Removal should deactivate rather than delete: {after_remove}")
        if [kind for kind, _detail in _events(target_id)] != ["client_granted", "client_removed"]:
            _fail("Removal was not audited.")

        empty = _put_clients(http, csrf, target_id, [])
        if empty.status_code != 400:
            _fail("A non-administrator cannot end with zero clients.")
        if _assignments(target_id) != after_remove:
            _fail("Rejected empty client set changed assignments.")

        if not user_can_access_client(actor_id, client_b):
            _fail("Administrator effective access must remain all clients.")
        cleared = _put_clients(http, csrf, actor_id, [])
        if cleared.status_code != 200:
            _fail(cleared.text)
        if not user_can_access_client(actor_id, client_b):
            _fail("Clearing explicit rows limited an administrator.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_client_replace_rolls_back() -> None:
    http, csrf, actor_id, _password_value = _actor()
    target_id = _insert_user(
        email=_email(),
        name="Client Rollback",
        administrator=False,
        password=_password(),
        role=REVOPS_SPECIALIST,
    )
    client_a, client_b = _client_ids()
    calls = {"n": 0}
    import user_management as user_management_module

    real = user_management_module._record_admin_event

    def boom(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("audit failed")
        return real(conn, **kwargs)

    try:
        with patch("user_management._record_admin_event", side_effect=boom):
            try:
                _put_clients(http, csrf, target_id, [client_a, client_b])
            except RuntimeError as exc:
                if "audit failed" not in str(exc):
                    raise
        if _assignments(target_id):
            _fail("Client rollback left a partial assignment set.")
        if _events(target_id):
            _fail("Client rollback left an audit row.")
    finally:
        _delete_user(target_id)
        _delete_user(actor_id)


def test_tests_do_not_write_live_users() -> None:
    conn = sqlite3.connect(f"file:{PRODUCTION_DB_PATH.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        emails = [str(row["email"]).lower() for row in conn.execute("SELECT email FROM users")]
        if len(emails) != 3:
            _fail(f"Live users count changed: {len(emails)}")
        for pilot in PILOT_EMAILS:
            if pilot in emails:
                _fail(f"Live database contains {pilot}")
        if any(email.startswith("um2c.") for email in emails):
            _fail("A UM-2C test user was written to the live database.")
        events = conn.execute("SELECT COUNT(*) FROM staff_admin_events").fetchone()[0]
        if int(events) != 0:
            _fail(f"Live staff_admin_events changed: {events}")
    finally:
        conn.close()


def main() -> int:
    os.environ.pop("NORTHSTAR_AUTH_ENFORCE", None)
    tests = [
        test_administrator_can_edit_another_users_name,
        test_email_normalized_lowercase,
        test_duplicate_email_rejected,
        test_non_domain_email_rejected,
        test_invalid_role_rejected,
        test_promotion_forces_system_administrator,
        test_demotion_requires_non_admin_role,
        test_self_deactivation_and_demotion_rejected,
        test_last_usable_admin_shape_and_lockout,
        test_specialist_receives_403_and_unknown_user_404,
        test_inactive_user_cannot_authenticate_and_session_rules,
        test_own_email_change_revokes_actor_session,
        test_audit_events_and_noop_and_crm_untouched,
        test_patch_rolls_back_when_audit_fails,
        test_client_access_rules,
        test_client_replace_rolls_back,
        test_tests_do_not_write_live_users,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
