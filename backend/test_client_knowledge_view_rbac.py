"""Read-only Client Knowledge for RevOps specialists.

Isolated testdb only. Does not write production northstar.db.
"""

from __future__ import annotations

import secrets

import testdb

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from client_setup_data import user_can_edit_client_setup
from db import get_connection
from main import app
from staff_rbac import (
    APPOINTMENT_SETTER,
    OPERATIONS_ADMIN,
    REVOPS_MANAGER,
    REVOPS_SPECIALIST,
    SYSTEM_ADMINISTRATOR,
    permissions_for_role,
    user_has_permission,
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(8)}"


def _client(code: str, name: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE lower(code) = lower(?)",
            (code,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        conn.execute(
            "INSERT INTO clients (code, name) VALUES (?, ?)",
            (code, name),
        )
        client_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        return client_id


def _create_user(*, email: str, password: str, role: str, administrator: int = 0) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, ?, 1, 1, ?, 0, '', ?)
            """,
            (email, email.split("@", 1)[0], int(administrator), digest, role),
        )
        user_id = int(conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"])
        conn.commit()
    return user_id


def _assign(user_id: int, client_id: int, role: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_client_assignments (user_id, client_id, role, active)
            VALUES (?, ?, ?, 1)
            """,
            (int(user_id), int(client_id), role),
        )
        conn.commit()


def _login(http: TestClient, email: str, password: str) -> str:
    response = http.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        _fail(f"login failed {response.status_code} {response.text}")
    return str(response.json().get("csrf_token") or "")


def _knowledge(http: TestClient, client_id: int):
    return http.get(f"/api/clients/{client_id}/knowledge")


def test_permission_catalog() -> None:
    for role in (SYSTEM_ADMINISTRATOR, OPERATIONS_ADMIN, REVOPS_MANAGER, REVOPS_SPECIALIST):
        if "client_knowledge.view" not in permissions_for_role(role):
            _fail(f"{role} missing client_knowledge.view")
        if "feedback.submit" not in permissions_for_role(role):
            _fail(f"{role} lost feedback.submit")
    specialist = permissions_for_role(REVOPS_SPECIALIST)
    for forbidden in ("users.manage", "admin.view", "clients.all", "clients.manage", "client_setup.edit"):
        if forbidden in specialist:
            _fail(f"specialist must not have {forbidden}")
    if "client_knowledge.view" in permissions_for_role(APPOINTMENT_SETTER):
        _fail("appointment setter must not view client knowledge")


def test_specialist_reads_only_the_granted_client() -> None:
    midwest = _client("midwest", "Midwest Automation & Custom Fabrication")
    dawson = _client("dawson", "Dawson Fabrication")
    premier = _client("premier", "Premier Manufacturing")
    brown = _client("brown", "Brown Industries")
    carmeco = _client("carmeco", "Carmeco")
    password = _secret()
    ian = _create_user(email=f"iana.{secrets.token_hex(3)}@example.test", password=password, role=REVOPS_SPECIALIST)
    scott = _create_user(email=f"scottm.{secrets.token_hex(3)}@example.test", password=password, role=REVOPS_SPECIALIST)
    tyler = _create_user(email=f"tylers.{secrets.token_hex(3)}@example.test", password=password, role=REVOPS_SPECIALIST)
    todd = _create_user(email=f"toddw.{secrets.token_hex(3)}@example.test", password=password, role=REVOPS_SPECIALIST)
    _assign(ian, midwest, REVOPS_SPECIALIST)
    _assign(scott, midwest, REVOPS_SPECIALIST)
    _assign(tyler, dawson, REVOPS_SPECIALIST)
    _assign(todd, premier, REVOPS_SPECIALIST)

    if user_can_edit_client_setup(ian, midwest):
        _fail("specialist must not edit client setup")
    if not user_has_permission(ian, "client_knowledge.view", client_id=midwest):
        _fail("Ian must view Midwest knowledge")
    if user_has_permission(ian, "client_knowledge.view", client_id=dawson):
        _fail("Ian must not view Dawson knowledge")
    if not user_has_permission(ian, "feedback.submit"):
        _fail("feedback.submit must remain")

    for user_id, allowed, denied in (
        (ian, midwest, (dawson, premier, brown, carmeco)),
        (scott, midwest, (dawson, premier, brown, carmeco)),
        (tyler, dawson, (midwest, premier)),
        (todd, premier, (midwest, dawson)),
    ):
        http = TestClient(app)
        email = ""
        with get_connection() as conn:
            email = str(conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()["email"])
        csrf = _login(http, email, password)
        allowed_response = _knowledge(http, allowed)
        if allowed_response.status_code != 200:
            _fail(f"read {allowed} expected 200, got {allowed_response.status_code} {allowed_response.text}")
        body = allowed_response.json()
        if body.get("can_edit") is not False:
            _fail("specialist knowledge must be read-only")
        keys = {item.get("section_key") for item in body.get("sections") or []}
        for required in ("client_profile", "strategy", "call_playbook", "capabilities"):
            if required not in keys:
                _fail(f"missing section {required}")
        for other in denied:
            denied_response = _knowledge(http, other)
            if denied_response.status_code != 403:
                _fail(f"read {other} expected 403, got {denied_response.status_code}")
        edit = http.patch(
            f"/api/clients/{allowed}/knowledge/sections/strategy/fields",
            headers={CSRF_HEADER: csrf},
            json={"field_name": "sales_goals", "value": "should not save"},
        )
        if edit.status_code != 403:
            _fail(f"knowledge edit expected 403, got {edit.status_code} {edit.text}")
        contact = http.post(
            f"/api/clients/{allowed}/knowledge/contacts",
            headers={CSRF_HEADER: csrf},
            json={"name": "Should Not Create", "role_type": "Other"},
        )
        if contact.status_code != 403:
            _fail(f"contact create expected 403, got {contact.status_code} {contact.text}")
        template = http.post(
            f"/api/clients/{allowed}/knowledge/email-templates",
            headers={CSRF_HEADER: csrf},
            json={"template_name": "Should Not Create", "template_type": "Info email", "body": "no"},
        )
        if template.status_code != 403:
            _fail(f"template create expected 403, got {template.status_code} {template.text}")
        setup = http.put(
            f"/api/clients/{allowed}/setup/overview",
            headers={CSRF_HEADER: csrf},
            json={"client_name": "Should Not Rename", "website": "https://example.test"},
        )
        if setup.status_code != 403:
            _fail(f"setup edit expected 403, got {setup.status_code} {setup.text}")
        admin = http.get("/api/admin/users")
        if admin.status_code != 403:
            _fail(f"administration expected 403, got {admin.status_code}")


def test_administrator_knowledge_edit_unchanged() -> None:
    midwest = _client("midwest", "Midwest Automation & Custom Fabrication")
    password = _secret()
    admin_id = _create_user(
        email=f"admin.{secrets.token_hex(3)}@example.test",
        password=password,
        role=SYSTEM_ADMINISTRATOR,
        administrator=1,
    )
    if not user_can_edit_client_setup(admin_id, midwest):
        _fail("administrator must still edit client setup")
    http = TestClient(app)
    with get_connection() as conn:
        email = str(conn.execute("SELECT email FROM users WHERE id = ?", (admin_id,)).fetchone()["email"])
    _login(http, email, password)
    response = _knowledge(http, midwest)
    if response.status_code != 200 or response.json().get("can_edit") is not True:
        _fail(f"admin knowledge expected editable 200, got {response.status_code} {response.text}")


def main() -> None:
    test_permission_catalog()
    test_specialist_reads_only_the_granted_client()
    test_administrator_knowledge_edit_unchanged()
    print("3 tests ok")


if __name__ == "__main__":
    main()
