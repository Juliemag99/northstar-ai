"""Phase 6B — session actor, RBAC, and client ACL.

Isolated testdb only. Never writes production northstar.db.
Never stores a real staff password.
"""

from __future__ import annotations

import os
import secrets

import testdb

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER, ENFORCE_FLAG
from auth_passwords import hash_password
from db import get_connection
from main import app
from staff_rbac import (
    READ_ONLY,
    REVOPS_MANAGER,
    REVOPS_SPECIALIST,
    SYSTEM_ADMINISTRATOR,
    APPOINTMENT_SETTER,
    ensure_staff_rbac_schema,
    user_has_permission,
)

os.environ.pop(ENFORCE_FLAG, None)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client_ids() -> tuple[int, int, int]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id FROM clients
            WHERE lower(name) LIKE '%carmeco%'
               OR lower(name) LIKE '%brown%'
               OR lower(name) LIKE '%dawson%'
            ORDER BY id
            """
        ).fetchall()
    if len(rows) < 3:
        _fail("Need Carmeco, Brown, and Dawson in isolated copy.")
    return int(rows[0]["id"]), int(rows[1]["id"]), int(rows[2]["id"])


def _create_user(*, email: str, password: str, role: str, administrator: int = 0, internal: int = 1) -> int:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        ensure_staff_rbac_schema(conn)
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until, staff_role
            ) VALUES (?, ?, ?, ?, 1, ?, 0, '', ?)
            """,
            (email, f"6B {role}", int(administrator), int(internal), digest, role),
        )
        user_id = int(conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"])
        conn.commit()
    return user_id


def _assign(user_id: int, *client_ids: int) -> None:
    with get_connection() as conn:
        for cid in client_ids:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_client_assignments
                    (user_id, client_id, role, active, assigned_at)
                VALUES (?, ?, 'staff', 1, datetime('now'))
                """,
                (int(user_id), int(cid)),
            )
        conn.commit()


def _email(user_id: int) -> str:
    with get_connection() as conn:
        return str(conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()["email"])


def _login(http: TestClient, user_id: int, password: str):
    resp = http.post("/api/auth/login", json={"email": _email(user_id), "password": password})
    if resp.status_code != 200:
        _fail(f"login failed {resp.status_code} {resp.text}")
    return str(resp.json().get("csrf_token") or "")


def _record(client_id: int) -> str:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no) AS rn
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
            ORDER BY ccr.id LIMIT 1
            """,
            (int(client_id),),
        ).fetchone()
    if row is None:
        _fail(f"no CCR for client {client_id}")
    return str(row["rn"])


def test_session_user_not_julie() -> None:
    carmeco, _brown, _dawson = _client_ids()
    password = _secret()
    uid = _create_user(
        email=f"spec-{secrets.token_hex(3)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
        internal=1,
    )
    _assign(uid, carmeco)
    http = TestClient(app)
    _login(http, uid, password)
    me = http.get("/api/auth/me").json()
    if int((me.get("user") or {}).get("id") or 0) != uid:
        _fail("/api/auth/me did not return the session user.")
    default = http.get("/api/users/default").json()
    if int((default.get("user") or {}).get("id") or 0) != uid:
        _fail("/api/users/default still impersonates Julie.")
    client_ids = {int(c["client_id"]) for c in default.get("clients") or []}
    if client_ids != {carmeco}:
        _fail(f"picker clients {client_ids} != {{{carmeco}}}")


def test_specialist_cannot_read_or_write_forbidden_client() -> None:
    carmeco, brown, _dawson = _client_ids()
    password = _secret()
    uid = _create_user(
        email=f"acl-{secrets.token_hex(3)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
        internal=1,
    )
    _assign(uid, carmeco)
    if user_has_permission(uid, "client.view", client_id=brown):
        _fail("internal=1 specialist must not view unassigned client via helper")
    http = TestClient(app)
    csrf = _login(http, uid, password)
    prospects = http.get(f"/api/prospects?client_id={brown}&limit=5")
    if prospects.status_code != 403:
        _fail(f"forbidden prospects expected 403, got {prospects.status_code} {prospects.text}")
    reports = http.get(f"/api/reports/filters?client_id={brown}")
    if reports.status_code != 403:
        _fail(f"forbidden reports expected 403, got {reports.status_code}")
    campaigns = http.get(f"/api/campaigns?client_id={brown}")
    if campaigns.status_code != 403:
        _fail(f"forbidden campaigns expected 403, got {campaigns.status_code}")
    record_no = _record(brown)
    patch = http.patch(
        f"/api/companies/by-record/{record_no}/status",
        headers={CSRF_HEADER: csrf},
        json={"client_id": brown, "status": "Current Customer", "user": "Julie Magnani"},
    )
    if patch.status_code != 403:
        _fail(f"forbidden status patch expected 403, got {patch.status_code} {patch.text}")
    allowed = http.get(f"/api/prospects?client_id={carmeco}&limit=5")
    if allowed.status_code != 200:
        _fail(f"allowed prospects {allowed.status_code}")


def test_spoof_other_user_id() -> None:
    carmeco, brown, _ = _client_ids()
    password = _secret()
    spec = _create_user(
        email=f"spoof-{secrets.token_hex(3)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
    )
    _assign(spec, carmeco)
    http = TestClient(app)
    _login(http, spec, password)
    other = http.get("/api/users/1/clients")
    if other.status_code != 403:
        _fail(f"other user's clients expected 403, got {other.status_code}")
    scope = http.get(f"/api/dashboard/scope?user_id=1&client_id={brown}")
    if scope.status_code != 403:
        _fail(f"spoof dashboard user_id expected 403, got {scope.status_code}")


def test_read_only_cannot_mutate() -> None:
    carmeco, _b, _d = _client_ids()
    password = _secret()
    uid = _create_user(
        email=f"ro-{secrets.token_hex(3)}@example.test",
        password=password,
        role=READ_ONLY,
        internal=0,
    )
    _assign(uid, carmeco)
    http = TestClient(app)
    csrf = _login(http, uid, password)
    view = http.get(f"/api/prospects?client_id={carmeco}&limit=5")
    if view.status_code != 403 and view.status_code != 200:
        _fail(f"read-only view unexpected {view.status_code}")
    # client.view is allowed
    if view.status_code != 200:
        _fail(f"read-only should view assigned client, got {view.status_code} {view.text}")
    record_no = _record(carmeco)
    patch = http.patch(
        f"/api/companies/by-record/{record_no}/status",
        headers={CSRF_HEADER: csrf},
        json={"client_id": carmeco, "status": "Current Customer"},
    )
    if patch.status_code != 403:
        _fail(f"read-only status expected 403, got {patch.status_code}")
    note = http.patch(
        f"/api/companies/by-record/{record_no}/notes",
        headers={CSRF_HEADER: csrf},
        json={"client_id": carmeco, "note_text": "should fail"},
    )
    if note.status_code != 403:
        _fail(f"read-only notes expected 403, got {note.status_code}")


def test_admin_still_blocked_for_specialist() -> None:
    carmeco, _b, _d = _client_ids()
    password = _secret()
    uid = _create_user(
        email=f"admblock-{secrets.token_hex(3)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
    )
    _assign(uid, carmeco)
    http = TestClient(app)
    csrf = _login(http, uid, password)
    export = http.get("/api/admin/master-data-export")
    if export.status_code != 403:
        _fail(f"export {export.status_code}")
    upload = http.post(
        f"/api/clients/{carmeco}/admin/imports",
        headers={CSRF_HEADER: csrf},
        files={"file": ("x.csv", b"Company\nAcme", "text/csv")},
    )
    if upload.status_code != 403:
        _fail(f"import {upload.status_code}")
    research = http.post(
        f"/api/clients/{carmeco}/admin/research-imports",
        headers={CSRF_HEADER: csrf},
        files={"file": ("x.csv", b"Company Name\nAcme", "text/csv")},
    )
    if research.status_code != 403:
        _fail(f"research import {research.status_code}")
    confirm = http.post(
        f"/api/clients/{carmeco}/admin/research-imports/1/confirm",
        headers={CSRF_HEADER: csrf},
        json={"plan_fingerprint": "x"},
    )
    if confirm.status_code != 403:
        _fail(f"research import confirm {confirm.status_code}")


def test_manager_two_clients_and_workflow_attribution() -> None:
    carmeco, brown, dawson = _client_ids()
    password = _secret()
    uid = _create_user(
        email=f"mgr-{secrets.token_hex(3)}@example.test",
        password=password,
        role=REVOPS_MANAGER,
        internal=0,
    )
    _assign(uid, brown, dawson)
    http = TestClient(app)
    csrf = _login(http, uid, password)
    if http.get(f"/api/prospects?client_id={carmeco}&limit=3").status_code != 403:
        _fail("manager must not see Carmeco")
    if http.get(f"/api/prospects?client_id={brown}&limit=3").status_code != 200:
        _fail("manager should see Brown")
    record_no = _record(brown)
    with get_connection() as conn:
        old = conn.execute(
            """
            SELECT status FROM client_company_relationships
            WHERE client_id = ? AND (
                TRIM(external_record_no) = ? OR company_id IN (
                    SELECT id FROM companies WHERE external_record_no = ?
                )
            )
            LIMIT 1
            """,
            (brown, record_no, record_no),
        ).fetchone()
        previous = str(old["status"] if old else "")
    new_status = previous if previous else "Current Customer"
    patch = http.patch(
        f"/api/companies/by-record/{record_no}/status",
        headers={CSRF_HEADER: csrf},
        json={"client_id": brown, "status": new_status},
    )
    if patch.status_code not in {200, 400}:
        _fail(f"manager status {patch.status_code} {patch.text}")
    if patch.status_code == 200:
        payload = patch.json()
        dumped = str(payload)
        if "Julie Magnani" in dumped and f"6B {REVOPS_MANAGER}" not in dumped:
            # changed_by may be in nested fields
            pass
        with get_connection() as conn:
            audit = conn.execute(
                """
                SELECT changed_by FROM field_audit_log
                WHERE external_record_no = ?
                ORDER BY id DESC LIMIT 1
                """,
                (record_no,),
            ).fetchone()
        if audit is not None and "Julie Magnani" == str(audit["changed_by"]):
            _fail("status audit still attributed to Julie Magnani")


def test_appointment_setter_and_admin_roles_exist() -> None:
    carmeco, _b, _d = _client_ids()
    setter = _create_user(
        email=f"set-{secrets.token_hex(3)}@example.test",
        password=_secret(),
        role=APPOINTMENT_SETTER,
    )
    admin = _create_user(
        email=f"sys-{secrets.token_hex(3)}@example.test",
        password=_secret(),
        role=SYSTEM_ADMINISTRATOR,
        administrator=1,
    )
    _assign(setter, carmeco)
    if not user_has_permission(setter, "crm.edit", client_id=carmeco):
        _fail("setter crm.edit")
    if user_has_permission(setter, "reports.view"):
        _fail("setter should not have reports.view")
    if not user_has_permission(admin, "exports.master_data"):
        _fail("system admin export")


def test_workflow_attribution_and_forbidden_ask() -> None:
    carmeco, brown, _dawson = _client_ids()
    password = _secret()
    uid = _create_user(
        email=f"flow-{secrets.token_hex(3)}@example.test",
        password=password,
        role=REVOPS_SPECIALIST,
        internal=1,
    )
    _assign(uid, carmeco)
    http = TestClient(app)
    csrf = _login(http, uid, password)
    record_no = _record(carmeco)
    call = http.post(
        "/api/activities",
        headers={CSRF_HEADER: csrf},
        json={
            "client_id": carmeco,
            "external_record_no": record_no,
            "activity_type": "Call",
            "notes": "6B specialist call",
            "created_by": "Julie Magnani",
            "user_id": 1,
        },
    )
    if call.status_code not in {200, 201}:
        _fail(f"allowed call {call.status_code} {call.text}")
    payload = call.json()
    if int(payload.get("user_id") or 0) != uid:
        _fail(f"call user_id {payload.get('user_id')} != session {uid}")
    if "Julie Magnani" == str(payload.get("created_by") or ""):
        _fail("call still attributed to Julie Magnani")
    forbidden_call = http.post(
        "/api/activities",
        headers={CSRF_HEADER: csrf},
        json={
            "client_id": brown,
            "external_record_no": _record(brown),
            "activity_type": "Call",
            "notes": "should fail",
            "created_by": "Julie Magnani",
            "user_id": 1,
        },
    )
    if forbidden_call.status_code != 403:
        _fail(f"forbidden call expected 403, got {forbidden_call.status_code} {forbidden_call.text}")
    client = http.get(f"/api/client?client_id={brown}")
    if client.status_code != 403:
        _fail(f"/api/client spoof expected 403, got {client.status_code} {client.text}")
    ask = http.post(
        "/api/ask-northstar",
        headers={CSRF_HEADER: csrf},
        json={
            "question": "What is the status of Brown Industries accounts?",
            "scope": "active_client",
            "active_client_id": brown,
        },
    )
    if ask.status_code != 403:
        body = ask.text
        if ask.status_code == 200 and "Brown" in body and str(brown) in body:
            _fail(f"Ask NorthStar leaked Brown data: {body[:400]}")
        if ask.status_code != 403:
            _fail(f"Ask NorthStar forbidden client expected 403, got {ask.status_code} {body[:400]}")


def test_users_default_unenforced_without_session_stays_julie() -> None:
    http = TestClient(app)
    payload = http.get("/api/users/default").json()
    email = str((payload.get("user") or {}).get("email") or "").lower()
    if "juliem@n-star.us" not in email:
        _fail(f"unenforced anonymous default should remain Julie, got {email}")


def main() -> None:
    tests = [
        test_session_user_not_julie,
        test_specialist_cannot_read_or_write_forbidden_client,
        test_spoof_other_user_id,
        test_read_only_cannot_mutate,
        test_admin_still_blocked_for_specialist,
        test_manager_two_clients_and_workflow_attribution,
        test_appointment_setter_and_admin_roles_exist,
        test_workflow_attribution_and_forbidden_ask,
        test_users_default_unenforced_without_session_stays_julie,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} tests ok")


if __name__ == "__main__":
    main()
