"""PR-2 isolated RevOps rehearsal: users, ACL, book ownership, workflow, admin deny.

Uses a disposable copy of live northstar.db via testdb. Never writes production.
Placeholder emails are @northstar.example.test only — not guessed real addresses.
"""

from __future__ import annotations

import testdb

import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from access import user_can_access_client
from auth_http import CSRF_HEADER
from ccr_book_assignment import apply_ccr_book_assignment
from db import get_connection
from main import app
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST, user_has_permission

PILOT = (
    ("Robert", "Kirsten", "robert.kirsten@northstar.example.test", "brown", 2, 1243),
    ("Tyler", "Sullivan", "tyler.sullivan@northstar.example.test", "dawson", 3, 243),
    ("Todd", "White", "todd.white@northstar.example.test", "premier", 4, 729),
)
FORBIDDEN_CLIENTS = {
    "brown": ("dawson", "premier", "carmeco"),
    "dawson": ("brown", "premier", "carmeco"),
    "premier": ("brown", "dawson", "carmeco"),
}


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


def _record(client_id: int) -> tuple[str, int | None]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no) AS rn,
                   (
                       SELECT ctc.id FROM contacts ctc
                       WHERE ctc.company_id = ccr.company_id
                       ORDER BY ctc.id LIMIT 1
                   ) AS contact_id
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
              AND TRIM(COALESCE(ccr.archived_at, '')) = ''
            ORDER BY ccr.id LIMIT 1
            """,
            (int(client_id),),
        ).fetchone()
    if row is None:
        _fail(f"no CCR for {client_id}")
    contact_id = row["contact_id"]
    return str(row["rn"]), (int(contact_id) if contact_id is not None else None)


def _login(http: TestClient, email: str, password: str) -> str:
    resp = http.post("/api/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        _fail(f"login failed {email} {resp.status_code} {resp.text}")
    return str(resp.json().get("csrf_token") or "")


def _headers(csrf: str) -> dict[str, str]:
    return {CSRF_HEADER: csrf}


def test_pr2_isolated_rehearsal() -> dict:
    passwords: dict[str, str] = {}
    created: dict[str, dict] = {}
    for first, last, email, code, expected_id, expected_ccr in PILOT:
        cid = _client_id(code)
        if cid != expected_id:
            _fail(f"{code} id {cid} != {expected_id}")
        password = _secret()
        passwords[code] = password
        result = provision_staff_user(
            first_name=first,
            last_name=last,
            email=email,
            password=password,
            staff_role=REVOPS_SPECIALIST,
            client_ids=[cid],
            exist_ok=False,
        )
        created[code] = result["user"]
        uid = int(result["user"]["user_id"])
        if result["user"]["is_administrator"]:
            _fail(f"{email} is administrator")
        if not user_can_access_client(uid, cid):
            _fail(f"{email} missing {code} ACL")
        for other in FORBIDDEN_CLIENTS[code]:
            other_id = _client_id(other)
            if user_can_access_client(uid, other_id):
                _fail(f"{email} can access {other}")
            if user_has_permission(uid, "client.view", client_id=other_id):
                _fail(f"{email} client.view on {other}")
        for perm in (
            "client.view",
            "crm.edit",
            "notes.edit",
            "tasks.edit",
            "campaigns.view",
            "reports.view",
            "research.run",
            "feedback.submit",
        ):
            if not user_has_permission(uid, perm, client_id=cid):
                _fail(f"{email} missing {perm}")
        for denied in (
            "users.manage",
            "clients.all",
            "admin.view",
            "imports.manage",
            "duplicates.execute",
            "duplicates.approve",
            "master_data.archive",
            "system.manage",
            "campaigns.manage",
        ):
            if user_has_permission(uid, denied):
                _fail(f"{email} has {denied}")

        preview = apply_ccr_book_assignment(client_id=cid, target_user_id=uid, dry_run=True)
        if preview["total_active_ccr"] != expected_ccr:
            _fail(f"{code} preview CCR {preview['total_active_ccr']} != {expected_ccr}")
        applied = apply_ccr_book_assignment(client_id=cid, target_user_id=uid, dry_run=False)
        if applied["already_assigned_to_target"] != expected_ccr:
            _fail(f"{code} applied {applied}")
        rerun = apply_ccr_book_assignment(client_id=cid, target_user_id=uid, dry_run=False)
        if rerun["updated"] != 0:
            _fail(f"{code} rerun mutated {rerun['updated']}")

    http = TestClient(app)
    workflow: dict[str, dict] = {}
    for first, last, email, code, expected_id, expected_ccr in PILOT:
        csrf = _login(http, email, passwords[code])
        me = http.get("/api/auth/me").json()
        if me.get("authenticated") is not True:
            _fail(f"{email} /me not authenticated")
        if str((me.get("user") or {}).get("email") or "").lower() != email:
            _fail(f"{email} /me identity mismatch")
        client_ids = {int(c["client_id"]) for c in (me.get("user") or {}).get("clients") or []}
        if client_ids != {expected_id}:
            _fail(f"{email} picker {client_ids}")

        allowed = http.get(f"/api/prospects?client_id={expected_id}&limit=5")
        if allowed.status_code != 200:
            _fail(f"{code} prospects {allowed.status_code} {allowed.text}")
        campaigns = http.get(f"/api/campaigns?client_id={expected_id}")
        if campaigns.status_code != 200:
            _fail(f"{code} campaigns {campaigns.status_code}")
        reports = http.get(f"/api/reports/filters?client_id={expected_id}")
        if reports.status_code != 200:
            _fail(f"{code} reports {reports.status_code}")
        ask_ok = http.post(
            "/api/ask-northstar",
            headers=_headers(csrf),
            json={
                "question": f"How many companies are in {code}?",
                "scope": "active_client",
                "active_client_id": expected_id,
            },
        )
        if ask_ok.status_code != 200:
            _fail(f"{code} ask {ask_ok.status_code} {ask_ok.text[:300]}")

        record_no, contact_id = _record(expected_id)
        with get_connection() as conn:
            old = conn.execute(
                """
                SELECT status, next_action, notes FROM client_company_relationships
                WHERE client_id = ? AND (
                    TRIM(external_record_no) = ? OR company_id IN (
                        SELECT id FROM companies WHERE external_record_no = ?
                    )
                )
                LIMIT 1
                """,
                (expected_id, record_no, record_no),
            ).fetchone()
        status = str(old["status"] if old else "New") or "New"
        status_resp = http.patch(
            f"/api/companies/by-record/{record_no}/status",
            headers=_headers(csrf),
            json={"client_id": expected_id, "status": status},
        )
        if status_resp.status_code not in {200, 400}:
            _fail(f"{code} status {status_resp.status_code} {status_resp.text}")
        note_resp = http.patch(
            f"/api/companies/by-record/{record_no}/notes",
            headers=_headers(csrf),
            json={"client_id": expected_id, "note_text": f"PR2 {code} rehearsal note", "mode": "append"},
        )
        if note_resp.status_code != 200:
            _fail(f"{code} note {note_resp.status_code} {note_resp.text}")
        follow = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT15:00:00Z")
        call = http.post(
            "/api/activities",
            headers=_headers(csrf),
            json={
                "client_id": expected_id,
                "external_record_no": record_no,
                "activity_type": "Call",
                "notes": f"PR2 {code} rehearsal call",
                "follow_up_at": follow,
            },
        )
        if call.status_code not in {200, 201}:
            _fail(f"{code} call {call.status_code} {call.text}")
        if contact_id:
            wf = http.patch(
                f"/api/contacts/{contact_id}/workflow",
                headers=_headers(csrf),
                json={"client_id": expected_id, "next_action": "Call"},
            )
            if wf.status_code not in {200, 400}:
                _fail(f"{code} next action {wf.status_code} {wf.text}")

        export = http.get("/api/admin/master-data-export")
        if export.status_code != 403:
            _fail(f"{code} export {export.status_code}")
        upload = http.post(
            f"/api/clients/{expected_id}/admin/imports",
            headers=_headers(csrf),
            files={"file": ("x.csv", b"Company\nAcme", "text/csv")},
        )
        if upload.status_code != 403:
            _fail(f"{code} import {upload.status_code}")
        lm = http.get("/api/admin/leadmaster-refresh/meta")
        if lm.status_code != 403:
            _fail(f"{code} leadmaster meta {lm.status_code}")
        steward = http.get("/api/admin/data-steward/meta")
        if steward.status_code != 403:
            _fail(f"{code} steward {steward.status_code}")
        confirm = http.post(
            f"/api/clients/{expected_id}/admin/leadmaster-refresh/1/confirm",
            headers=_headers(csrf),
            json={},
        )
        if confirm.status_code not in {403, 404, 409}:
            _fail(f"{code} lm confirm {confirm.status_code}")

        for other in FORBIDDEN_CLIENTS[code]:
            other_id = _client_id(other)
            if http.get(f"/api/prospects?client_id={other_id}&limit=3").status_code != 403:
                _fail(f"{code} read {other}")
            other_rn, _ = _record(other_id)
            patch = http.patch(
                f"/api/companies/by-record/{other_rn}/status",
                headers=_headers(csrf),
                json={"client_id": other_id, "status": "New"},
            )
            if patch.status_code != 403:
                _fail(f"{code} mutate {other} {patch.status_code}")
            ask_bad = http.post(
                "/api/ask-northstar",
                headers=_headers(csrf),
                json={
                    "question": f"Show {other} pipeline",
                    "scope": "active_client",
                    "active_client_id": other_id,
                },
            )
            if ask_bad.status_code != 403:
                _fail(f"{code} ask {other} {ask_bad.status_code}")

        http.post("/api/auth/logout")
        workflow[code] = {
            "user_id": created[code]["user_id"],
            "email": email,
            "ccr_assigned": expected_ccr,
            "status": status_resp.status_code,
            "note": note_resp.status_code,
            "call": call.status_code,
        }

    dumped = json.dumps({"created": created, "workflow": workflow})
    if "$argon2id$" in dumped or "password_hash" in dumped:
        _fail("rehearsal payload leaked hashes")
    return {"users": created, "workflow": workflow}


def main() -> None:
    result = test_pr2_isolated_rehearsal()
    print("ok test_pr2_isolated_rehearsal")
    print(json.dumps(result, indent=2, default=str))
    print("1 tests ok")


if __name__ == "__main__":
    main()
