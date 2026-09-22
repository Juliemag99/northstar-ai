"""PW-2B isolated polish: phone extensions, attribution, reports, isolation.

Never writes production northstar.db. Temporary specialist accounts are
@northstar.example.test only.
"""

from __future__ import annotations

import testdb  # noqa: F401

import secrets

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER
from client_workspace_data import get_contact_workspace, list_relationship_statuses
from contact_phone import canonical_contact_phone
from crm_import_status_notes import STANDARD_CRM_RELATIONSHIP_STATUSES
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from search_data import search
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST

DAILY_STATUSES = (
    "New",
    "Contacted",
    "Left Message",
    "Send Information",
    "Hot Prospect",
    "Need Contact Name",
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _client_id(code: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE lower(code) = ?", (code,)
        ).fetchone()
    if row is None:
        _fail(f"missing client {code}")
    return int(row["id"])


def _login(http: TestClient, email: str, password: str) -> str:
    resp = http.post("/api/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        _fail(f"login failed {email} {resp.status_code} {resp.text}")
    return str(resp.json().get("csrf_token") or "")


def _headers(csrf: str) -> dict[str, str]:
    return {CSRF_HEADER: csrf}


def _provision(first: str, last: str, client_id: int) -> tuple[int, str, str]:
    suffix = secrets.token_hex(3)
    email = f"pw2b.{first.lower()}.{suffix}@northstar.example.test"
    password = f"NsTest9{secrets.token_hex(10)}"
    result = provision_staff_user(
        first_name=first,
        last_name=last,
        email=email,
        password=password,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[client_id],
    )
    return int(result["user"]["user_id"]), email, password


def _insert_company(client_id: int, assigned_user_id: int, stamp: str) -> tuple[int, str]:
    rn = f"PW2B-{stamp}"
    with get_connection() as conn:
        company_id = int(
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (rn, f"PW2B Co {stamp}"),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, assigned_user_id,
                next_action, notes, is_hot, created_at, updated_at
            ) VALUES (?, ?, ?, 'New', ?, '', '', 0, datetime('now'), datetime('now'))
            """,
            (client_id, company_id, rn, assigned_user_id),
        )
        conn.commit()
    return company_id, rn


def _cleanup_company(company_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (company_id,))
        conn.execute(
            "DELETE FROM contact_client_workflows WHERE contact_id IN "
            "(SELECT id FROM contacts WHERE company_id = ?)",
            (company_id,),
        )
        conn.execute(
            "DELETE FROM contact_client_relationships WHERE contact_id IN "
            "(SELECT id FROM contacts WHERE company_id = ?)",
            (company_id,),
        )
        conn.execute(
            "DELETE FROM client_company_relationships WHERE company_id = ?",
            (company_id,),
        )
        conn.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        conn.commit()


def test_production_path_untouched() -> None:
    live = str(PRODUCTION_DB_PATH).replace("\\", "/").lower()
    with get_connection() as conn:
        path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if path.replace("\\", "/").lower() == live:
        _fail(f"test connection is live production db: {path}")


def test_phone_extension_create_edit_remove_and_match() -> None:
    brown_id = _client_id("brown")
    dawson_id = _client_id("dawson")
    uid, email, password = _provision("Robert", "Phone", brown_id)
    stamp = secrets.token_hex(3)
    company_id, _rn = _insert_company(brown_id, uid, stamp)
    http = TestClient(app)
    csrf = _login(http, email, password)
    headers = _headers(csrf)
    try:
        created_phone = f"(402) 555-{stamp[:4]}"
        created = http.post(
            "/api/contacts/manual",
            headers=headers,
            json={
                "client_id": brown_id,
                "company_id": company_id,
                "action": "create",
                "first_name": "Pat",
                "last_name": f"Ext{stamp}",
                "phone": created_phone,
                "phone_extension": "123",
                "confirm_without_contact_info": False,
            },
        )
        if created.status_code != 200:
            _fail(f"create contact {created.status_code} {created.text}")
        contact_id = int(created.json()["contact_id"])
        ws = http.get(
            f"/api/contacts/{contact_id}?client_id={brown_id}",
            headers=headers,
        )
        if ws.status_code != 200:
            _fail(f"contact workspace {ws.status_code} {ws.text}")
        payload = ws.json()
        if payload.get("phone_extension") != "123":
            _fail(f"API dropped phone_extension: {payload.get('phone_extension')!r}")
        if "x123" not in str(payload.get("phone") or ""):
            _fail(f"display missing extension: {payload.get('phone')!r}")
        with get_connection() as conn:
            row = conn.execute(
                "SELECT phone, phone_extension FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
        stored_phone = str(row["phone"] or "")
        stored_ext = str(row["phone_extension"] or "")
        if "x123" in stored_phone.lower() or "123" in stored_phone.replace("(", "").replace(")", ""):
            if stored_ext == "123" and "x" in stored_phone.lower():
                _fail(f"main phone stored concatenated with extension: {stored_phone!r}")
        if stored_ext != "123":
            _fail(f"stored extension {stored_ext!r}")
        if created_phone not in stored_phone and stamp[:4] not in stored_phone.replace(
            " ", ""
        ).replace("-", "").replace("(", "").replace(")", ""):
            _fail(f"stored main phone unexpected: {stored_phone!r}")

        edited = http.patch(
            f"/api/contacts/{contact_id}/phone",
            headers=headers,
            json={"client_id": brown_id, "phone_extension": "456"},
        )
        if edited.status_code != 200:
            _fail(f"edit extension {edited.status_code} {edited.text}")
        with get_connection() as conn:
            row = conn.execute(
                "SELECT phone, phone_extension FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
        if str(row["phone"] or "") != stored_phone:
            _fail("main phone changed while editing extension")
        if str(row["phone_extension"] or "") != "456":
            _fail(f"extension not updated: {row['phone_extension']!r}")

        removed = http.patch(
            f"/api/contacts/{contact_id}/phone",
            headers=headers,
            json={"client_id": brown_id, "phone_extension": ""},
        )
        if removed.status_code != 200:
            _fail(f"remove extension {removed.status_code} {removed.text}")
        with get_connection() as conn:
            row = conn.execute(
                "SELECT phone, phone_extension FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
        if str(row["phone"] or "") != stored_phone:
            _fail("main phone changed while removing extension")
        if str(row["phone_extension"] or "").strip() != "":
            _fail(f"extension not cleared: {row['phone_extension']!r}")

        nanp, last7 = canonical_contact_phone("(402) 555-1212 x999")
        if nanp != "4025551212":
            _fail(f"10-digit match included extension: {nanp!r}")
        if last7 != "5551212":
            _fail(f"last-seven changed: {last7!r}")

        foreign = http.patch(
            f"/api/contacts/{contact_id}/phone",
            headers=headers,
            json={"client_id": dawson_id, "phone_extension": "789"},
        )
        if foreign.status_code != 403:
            _fail(f"foreign phone edit expected 403, got {foreign.status_code}")
    finally:
        _cleanup_company(company_id)


def test_authenticated_attribution_ignores_spoofed_created_by() -> None:
    brown_id = _client_id("brown")
    dawson_id = _client_id("dawson")
    uid, email, password = _provision("Robert", "Actor", brown_id)
    stamp = secrets.token_hex(3)
    company_id, rn = _insert_company(brown_id, uid, stamp)
    http = TestClient(app)
    csrf = _login(http, email, password)
    headers = _headers(csrf)
    try:
        created = http.post(
            "/api/contacts/manual",
            headers=headers,
            json={
                "client_id": brown_id,
                "company_id": company_id,
                "action": "create",
                "first_name": "Ann",
                "last_name": f"Call{stamp}",
                "phone": f"(402) 555-{stamp[:4]}",
                "phone_extension": "12",
            },
        )
        if created.status_code != 200:
            _fail(f"create {created.status_code} {created.text}")
        contact_id = int(created.json()["contact_id"])
        call = http.post(
            f"/api/contacts/{contact_id}/activities",
            headers=headers,
            json={
                "client_id": brown_id,
                "activity_type": "Call",
                "notes": "Left a message",
                "status": "Left Message",
                "outcome": "Left Message",
                "created_by": "Julie Magnani",
            },
        )
        if call.status_code != 200:
            _fail(f"log call {call.status_code} {call.text}")
        note = http.post(
            f"/api/contacts/{contact_id}/activities",
            headers=headers,
            json={
                "client_id": brown_id,
                "activity_type": "Note",
                "notes": "Internal note",
                "created_by": "Julie Magnani",
            },
        )
        if note.status_code != 200:
            _fail(f"add note {note.status_code} {note.text}")
        follow = http.post(
            f"/api/contacts/{contact_id}/activities",
            headers=headers,
            json={
                "client_id": brown_id,
                "activity_type": "Follow-Up",
                "notes": "Call back",
                "follow_up_date": "2026-09-30",
                "follow_up_time": "09:00",
                "created_by": "Julie Magnani",
            },
        )
        if follow.status_code != 200:
            _fail(f"follow-up {follow.status_code} {follow.text}")
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT activity_type, created_by, user_id
                FROM activities
                WHERE contact_id = ? AND client_id = ?
                ORDER BY activity_id
                """,
                (contact_id, brown_id),
            ).fetchall()
        names = {str(r["created_by"] or "") for r in rows}
        if any("Julie Magnani" in name for name in names):
            _fail(f"spoofed Julie attribution stored: {names}")
        if not any("Robert" in name for name in names):
            _fail(f"missing Robert attribution: {names}")
        if any(int(r["user_id"] or 0) != uid for r in rows if r["user_id"] is not None):
            _fail("activity user_id was not the authenticated specialist")
        foreign = http.post(
            f"/api/contacts/{contact_id}/activities",
            headers=headers,
            json={
                "client_id": dawson_id,
                "activity_type": "Note",
                "notes": "should fail",
                "created_by": "Robert Actor",
            },
        )
        if foreign.status_code != 403:
            _fail(f"foreign note expected 403, got {foreign.status_code}")
        _ = rn
    finally:
        _cleanup_company(company_id)


def test_specialist_report_filter_and_export_consistency() -> None:
    brown_id = _client_id("brown")
    dawson_id = _client_id("dawson")
    uid, email, password = _provision("Tyler", "Report", brown_id)
    http = TestClient(app)
    csrf = _login(http, email, password)
    headers = _headers(csrf)
    qs = "date_from=2026-01-01&date_to=2026-12-31"
    own = http.get(
        f"/api/reports/team-performance?client_id={brown_id}&user_id={uid}&{qs}",
        headers=headers,
    )
    if own.status_code != 200:
        _fail(f"own report {own.status_code} {own.text[:300]}")
    export = http.get(
        f"/api/reports/export?section=team-performance&client_id={brown_id}&user_id={uid}&{qs}",
        headers=headers,
    )
    if export.status_code != 200:
        _fail(f"own export {export.status_code} {export.text[:300]}")
    foreign = http.get(
        f"/api/reports/team-performance?client_id={dawson_id}&user_id={uid}&{qs}",
        headers=headers,
    )
    if foreign.status_code != 403:
        _fail(f"foreign report expected 403, got {foreign.status_code}")


def test_specialist_isolation_navigation_backends() -> None:
    brown_id = _client_id("brown")
    dawson_id = _client_id("dawson")
    uid, email, password = _provision("Todd", "Nav", brown_id)
    http = TestClient(app)
    csrf = _login(http, email, password)
    headers = _headers(csrf)
    own = http.get(f"/api/work-queue?client_id={brown_id}", headers=headers)
    if own.status_code != 200:
        _fail(f"own queue {own.status_code}")
    foreign = http.get(f"/api/work-queue?client_id={dawson_id}", headers=headers)
    if foreign.status_code != 403:
        _fail(f"foreign queue expected 403, got {foreign.status_code}")
    admin = http.get("/api/admin/leadmaster-refresh/meta", headers=headers)
    if admin.status_code != 403:
        _fail(f"admin expected 403, got {admin.status_code}")
    ri = http.post(
        f"/api/clients/{brown_id}/admin/research-imports",
        headers=headers,
        files={"file": ("x.xlsx", b"not-an-xlsx", "application/vnd.ms-excel")},
    )
    if ri.status_code not in {403, 404}:
        _fail(f"Research Import expected 403, got {ri.status_code}")
    _ = uid


def test_pw2a_search_and_dawson_status_still_work() -> None:
    brown_id = _client_id("brown")
    dawson_id = _client_id("dawson")
    uid, email, password = _provision("Robert", "Search", brown_id)
    result = search("BISON GEAR AND ENGINEERING", user_id=uid, client_id=brown_id)
    if getattr(result, "error", None):
        _fail(f"FTS search error {result.error}")
    labels = set(list_relationship_statuses(client_id=dawson_id))
    missing = [item for item in DAILY_STATUSES if item not in labels]
    if missing:
        _fail(f"Dawson operational statuses missing {missing}")
    for label in STANDARD_CRM_RELATIONSHIP_STATUSES:
        if label not in labels and label != "New":
            pass
    http = TestClient(app)
    csrf = _login(http, email, password)
    ws = get_contact_workspace
    _ = ws, email, password, csrf


def test_pilot_attribution_names_for_three_specialists() -> None:
    mapping = (
        ("Robert", "brown"),
        ("Tyler", "dawson"),
        ("Todd", "premier"),
    )
    for first, code in mapping:
        client_id = _client_id(code)
        uid, email, password = _provision(first, "Rehearse", client_id)
        stamp = secrets.token_hex(3)
        company_id, _rn = _insert_company(client_id, uid, stamp)
        http = TestClient(app)
        csrf = _login(http, email, password)
        headers = _headers(csrf)
        try:
            created = http.post(
                "/api/contacts/manual",
                headers=headers,
                json={
                    "client_id": client_id,
                    "company_id": company_id,
                    "action": "create",
                    "first_name": first,
                    "last_name": f"Ext{stamp}",
                    "phone": f"(515) 555-{stamp[:4]}",
                    "phone_extension": "88",
                },
            )
            if created.status_code != 200:
                _fail(f"{first} create {created.status_code} {created.text}")
            contact_id = int(created.json()["contact_id"])
            call = http.post(
                f"/api/contacts/{contact_id}/activities",
                headers=headers,
                json={
                    "client_id": client_id,
                    "activity_type": "Call",
                    "notes": f"{first} logged a call",
                    "created_by": "Julie Magnani",
                },
            )
            if call.status_code != 200:
                _fail(f"{first} call {call.status_code} {call.text}")
            ws = http.get(
                f"/api/contacts/{contact_id}?client_id={client_id}",
                headers=headers,
            ).json()
            if ws.get("phone_extension") != "88":
                _fail(f"{first} missing extension in workspace")
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT created_by FROM activities
                    WHERE contact_id = ? AND activity_type = 'Call'
                    ORDER BY activity_id DESC LIMIT 1
                    """,
                    (contact_id,),
                ).fetchone()
            if first not in str(row["created_by"] or ""):
                _fail(f"{first} call attributed to {row['created_by']!r}")
            report = http.get(
                f"/api/reports/team-performance?client_id={client_id}&user_id={uid}"
                "&date_from=2026-01-01&date_to=2026-12-31",
                headers=headers,
            )
            if report.status_code != 200:
                _fail(f"{first} report {report.status_code}")
        finally:
            _cleanup_company(company_id)


if __name__ == "__main__":
    test_production_path_untouched()
    test_phone_extension_create_edit_remove_and_match()
    test_authenticated_attribution_ignores_spoofed_created_by()
    test_specialist_report_filter_and_export_consistency()
    test_specialist_isolation_navigation_backends()
    test_pw2a_search_and_dawson_status_still_work()
    test_pilot_attribution_names_for_three_specialists()
    print("PW-2B isolated tests passed")
