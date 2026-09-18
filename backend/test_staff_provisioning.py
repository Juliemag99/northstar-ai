"""PR-2 staff provisioning fail-closed tests. Isolated testdb only."""

from __future__ import annotations

import testdb  # noqa: F401

import os
import secrets

from db import PRODUCTION_DB_PATH, get_connection
from staff_provisioning import (
    ALLOWED_PROVISION_ROLES,
    StaffProvisionError,
    is_live_database,
    provision_staff_user,
)
from staff_rbac import REVOPS_SPECIALIST, permissions_for_role


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client_id(code: str) -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM clients WHERE code = ?", (code,)).fetchone()
    if row is None:
        _fail(f"missing client {code}")
    return int(row["id"])


def test_creates_revops_specialist_with_acl() -> None:
    email = f"robert.kirsten.{secrets.token_hex(3)}@northstar.example.test"
    brown = _client_id("brown")
    result = provision_staff_user(
        first_name="Robert",
        last_name="Kirsten",
        email=email,
        password=_secret(),
        staff_role=REVOPS_SPECIALIST,
        client_ids=[brown],
    )
    dumped = str(result)
    if "$argon2id$" in dumped or "password_hash" in dumped:
        _fail("provision result leaked a hash")
    user = result["user"]
    if not result["created"] or user["is_administrator"] or not user["has_password"]:
        _fail("expected new non-admin login-capable user")
    if user["staff_role"] != REVOPS_SPECIALIST:
        _fail(f"role {user['staff_role']}")
    if [c["client_id"] for c in result["client_assignments"]] != [brown]:
        _fail("ACL was not Brown-only")
    expected = permissions_for_role(REVOPS_SPECIALIST)
    if set(result["permissions"]) != set(expected):
        _fail("permissions drifted")
    if "clients.all" in expected or "users.manage" in expected:
        _fail("specialist must not have admin capabilities")
    if is_live_database(__import__("pathlib").Path(result["database_path"])):
        _fail("provision wrote live northstar.db")


def test_duplicate_email_fails_closed() -> None:
    email = f"dup.{secrets.token_hex(3)}@northstar.example.test"
    brown = _client_id("brown")
    password = _secret()
    provision_staff_user(
        first_name="Tyler",
        last_name="Sullivan",
        email=email,
        password=password,
        client_ids=[brown],
    )
    try:
        provision_staff_user(
            first_name="Tyler",
            last_name="Sullivan",
            email=email.upper(),
            password=password,
            client_ids=[brown],
        )
    except StaffProvisionError as exc:
        if "already exists" not in str(exc).lower():
            _fail(str(exc))
    else:
        _fail("duplicate email was accepted")


def test_exist_ok_is_idempotent_without_password_change() -> None:
    email = f"idem.{secrets.token_hex(3)}@northstar.example.test"
    brown = _client_id("brown")
    first = provision_staff_user(
        first_name="Todd",
        last_name="White",
        email=email,
        password=_secret(),
        client_ids=[brown],
    )
    second = provision_staff_user(
        first_name="Todd",
        last_name="White",
        email=email,
        password=_secret(),
        client_ids=[brown],
        exist_ok=True,
    )
    if second["created"] or not second["already_existed"]:
        _fail("exist_ok should reuse the account")
    if second["password_set"]:
        _fail("exist_ok must not rotate the password")
    if second["user"]["user_id"] != first["user"]["user_id"]:
        _fail("exist_ok created a different user")


def test_invalid_role_and_admin_elevation_fail() -> None:
    brown = _client_id("brown")
    password = _secret()
    try:
        provision_staff_user(
            first_name="Bad",
            last_name="Role",
            email=f"badrole.{secrets.token_hex(3)}@northstar.example.test",
            password=password,
            staff_role="not_a_role",
            client_ids=[brown],
        )
    except StaffProvisionError:
        pass
    else:
        _fail("invalid role accepted")
    try:
        provision_staff_user(
            first_name="Bad",
            last_name="Admin",
            email=f"badadmin.{secrets.token_hex(3)}@northstar.example.test",
            password=password,
            staff_role="system_administrator",
            client_ids=[brown],
        )
    except StaffProvisionError as exc:
        if "administrator" not in str(exc).lower():
            _fail(str(exc))
    else:
        _fail("administrator role accepted")
    if "system_administrator" in ALLOWED_PROVISION_ROLES:
        _fail("admin role must not be provisionable")


def test_missing_client_fails() -> None:
    try:
        provision_staff_user(
            first_name="No",
            last_name="Client",
            email=f"noclient.{secrets.token_hex(3)}@northstar.example.test",
            password=_secret(),
            client_ids=[999999],
        )
    except StaffProvisionError as exc:
        if "does not exist" not in str(exc).lower():
            _fail(str(exc))
    else:
        _fail("missing client accepted")


def test_weak_password_rejected() -> None:
    try:
        provision_staff_user(
            first_name="Weak",
            last_name="Password",
            email=f"weak.{secrets.token_hex(3)}@northstar.example.test",
            password="password",
            client_ids=[_client_id("brown")],
        )
    except StaffProvisionError:
        pass
    else:
        _fail("weak password accepted")


def test_refuse_live_without_confirmation() -> None:
    from pathlib import Path

    import staff_provisioning as sp

    brown = _client_id("brown")
    with get_connection() as conn:
        isolated = Path(str(conn.execute("PRAGMA database_list").fetchone()["file"]))
        if isolated.resolve() == Path(PRODUCTION_DB_PATH).resolve():
            _fail("testdb pointing at live")
        original = sp.PRODUCTION_DB_PATH
        previous = os.environ.get(sp.ALLOW_FLAG)
        os.environ[sp.ALLOW_FLAG] = "1"
        try:
            sp.PRODUCTION_DB_PATH = isolated
            try:
                provision_staff_user(
                    first_name="Live",
                    last_name="Guard",
                    email=f"liveguard.{secrets.token_hex(3)}@northstar.example.test",
                    password=_secret(),
                    client_ids=[brown],
                    conn=conn,
                    confirm_live=False,
                )
            except StaffProvisionError as exc:
                if "live database" not in str(exc).lower():
                    _fail(str(exc))
            else:
                _fail("simulated live provision wrote without confirmation")
        finally:
            sp.PRODUCTION_DB_PATH = original
            if previous is None:
                os.environ.pop(sp.ALLOW_FLAG, None)
            else:
                os.environ[sp.ALLOW_FLAG] = previous


def main() -> None:
    tests = [
        test_creates_revops_specialist_with_acl,
        test_duplicate_email_fails_closed,
        test_exist_ok_is_idempotent_without_password_change,
        test_invalid_role_and_admin_elevation_fail,
        test_missing_client_fails,
        test_weak_password_rejected,
        test_refuse_live_without_confirmation,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} tests ok")


if __name__ == "__main__":
    main()
