"""PW-2A isolated blockers: Dawson statuses, New assignment scoping, SAVE & NEXT.

Never writes production northstar.db. Temporary specialist accounts are
@northstar.example.test only.
"""

from __future__ import annotations

import testdb  # noqa: F401

import secrets

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER
from client_workspace_data import (
    list_relationship_statuses,
    update_relationship_status,
)
from crm_import_status_notes import STANDARD_CRM_RELATIONSHIP_STATUSES
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST
from work_queue_data import get_work_queue_next, list_work_queue

DAILY_STATUSES = (
    "New",
    "Contacted",
    "Left Message",
    "Send Information",
    "Hot Prospect",
    "Future/Nurture",
    "Appointment Set",
    "Good Fit-But no projects at this Time",
    "Need Contact Name",
    "Need Contact Phone Number",
    "Current Customer",
    "Competitor",
    "Do Not Call",
    "Do Not Email",
    "Dupe Record",
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
    email = f"pw2a.{first.lower()}.{suffix}@northstar.example.test"
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


def _insert_new_company(client_id: int, assigned_user_id: int | None, stamp: str) -> tuple[int, str]:
    rn = f"PW2A-{stamp}"
    with get_connection() as conn:
        company_id = int(
            conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (rn, f"PW2A Co {stamp}"),
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
            "DELETE FROM client_company_relationships WHERE company_id = ?",
            (company_id,),
        )
        conn.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        conn.commit()


def test_dawson_operational_statuses_without_catalog_mutation() -> None:
    dawson_id = _client_id("dawson")
    brown_id = _client_id("brown")
    with get_connection() as conn:
        before = [
            str(r["status_label"])
            for r in conn.execute(
                """
                SELECT status_label FROM client_status_catalog
                WHERE client_id = ? ORDER BY status_label
                """,
                (dawson_id,),
            ).fetchall()
        ]
        ccr_before = [
            (int(r["id"]), str(r["status"] or ""))
            for r in conn.execute(
                "SELECT id, status FROM client_company_relationships WHERE client_id = ? ORDER BY id",
                (dawson_id,),
            ).fetchall()
        ]
        brown_catalog = [
            str(r["status_label"])
            for r in conn.execute(
                """
                SELECT status_label FROM client_status_catalog
                WHERE client_id = ? ORDER BY status_label
                """,
                (brown_id,),
            ).fetchall()
        ]
    labels = set(list_relationship_statuses(client_id=dawson_id))
    missing = [item for item in DAILY_STATUSES if item not in labels]
    if missing:
        _fail(f"Dawson dropdown missing {missing}; got {sorted(labels)[:20]}")
    for label in STANDARD_CRM_RELATIONSHIP_STATUSES:
        if label not in labels:
            _fail(f"standard label missing from Dawson list: {label}")
    uid, email, password = _provision("Tyler", "Status", dawson_id)
    stamp = secrets.token_hex(3)
    company_id, rn = _insert_new_company(dawson_id, uid, stamp)
    try:
        updated = update_relationship_status(
            rn, client_id=dawson_id, status="Need Contact Name", user="Tyler Status"
        )
        if str(updated.get("new_value") or "") != "Need Contact Name":
            _fail(f"Dawson status save failed: {updated}")
        row = update_relationship_status(
            rn, client_id=dawson_id, status="Left Message", user="Tyler Status"
        )
        if str(row.get("new_value") or "") != "Left Message":
            _fail(f"Dawson could not select Left Message: {row}")
        with get_connection() as conn:
            after = [
                str(r["status_label"])
                for r in conn.execute(
                    """
                    SELECT status_label FROM client_status_catalog
                    WHERE client_id = ? ORDER BY status_label
                    """,
                    (dawson_id,),
                ).fetchall()
            ]
            brown_after = [
                str(r["status_label"])
                for r in conn.execute(
                    """
                    SELECT status_label FROM client_status_catalog
                    WHERE client_id = ? ORDER BY status_label
                    """,
                    (brown_id,),
                ).fetchall()
            ]
            live_ccr = [
                (int(r["id"]), str(r["status"] or ""))
                for r in conn.execute(
                    """
                    SELECT id, status FROM client_company_relationships
                    WHERE client_id = ? AND company_id != ?
                    ORDER BY id
                    """,
                    (dawson_id, company_id),
                ).fetchall()
            ]
        if after != before:
            _fail("Dawson catalog mutated; PW-2A must stay additive in code only.")
        if brown_after != brown_catalog:
            _fail("Brown catalog changed during Dawson status test.")
        if live_ccr != ccr_before:
            _fail("Existing Dawson CCR statuses changed.")
    finally:
        _cleanup_company(company_id)

    http = TestClient(app)
    csrf = _login(http, email, password)
    statuses = http.get(f"/api/clients/{dawson_id}/statuses", headers=_headers(csrf))
    if statuses.status_code != 200:
        _fail(f"Dawson statuses HTTP {statuses.status_code} {statuses.text}")
    payload = statuses.json()
    names = payload.get("statuses") or []
    if "Need Contact Name" not in list(names):
        _fail(f"Dawson HTTP statuses missing Need Contact Name: {names!r}")


def test_specialist_new_assignments_are_own_book() -> None:
    brown_id = _client_id("brown")
    dawson_id = _client_id("dawson")
    uid, email, password = _provision("Robert", "Queue", brown_id)
    stamp = secrets.token_hex(3)
    mine_id, mine_rn = _insert_new_company(brown_id, uid, f"{stamp}a")
    other_id, _other_rn = _insert_new_company(brown_id, None, f"{stamp}b")
    foreign_id, _frn = _insert_new_company(dawson_id, uid, f"{stamp}c")
    try:
        queue = list_work_queue(uid, client_id=brown_id, work_type="new")
        rns = {item.external_record_no for item in queue.items}
        if mine_rn not in rns:
            _fail("Assigned New row missing from specialist New Assignments.")
        if any(item.company_id == other_id for item in queue.items):
            _fail("Unassigned Brown New row leaked into specialist queue.")
        if any(item.client_id == dawson_id for item in queue.items):
            _fail("Dawson row leaked into Brown specialist New queue.")
        if queue.summary.new_assignments < 1:
            _fail("Specialist New Assignments count hid assigned rows.")

        empty_uid, empty_email, empty_password = _provision("Empty", "Book", brown_id)
        empty_q = list_work_queue(empty_uid, client_id=brown_id, work_type="new")
        if empty_q.total != 0:
            _fail("Unassigned specialist saw the client New book.")
        if empty_q.summary.new_assignments != 0:
            _fail("Unassigned specialist New count was not zero.")

        http = TestClient(app)
        csrf = _login(http, email, password)
        own = http.get(
            f"/api/work-queue?client_id={brown_id}&type=new",
            headers=_headers(csrf),
        )
        if own.status_code != 200:
            _fail(f"own queue {own.status_code} {own.text}")
        foreign = http.get(
            f"/api/work-queue?client_id={dawson_id}&type=new",
            headers=_headers(csrf),
        )
        if foreign.status_code != 403:
            _fail(f"foreign queue expected 403, got {foreign.status_code} {foreign.text}")
        admin_ri = http.get("/api/admin/leadmaster-refresh/meta", headers=_headers(csrf))
        if admin_ri.status_code != 403:
            _fail(f"Research Import / admin expected 403, got {admin_ri.status_code}")
        ri_upload = http.post(
            f"/api/clients/{brown_id}/admin/research-imports",
            headers=_headers(csrf),
            files={"file": ("x.xlsx", b"not-an-xlsx", "application/vnd.ms-excel")},
        )
        if ri_upload.status_code not in {403, 404}:
            _fail(f"Research Import upload expected 403, got {ri_upload.status_code}")
        _ = empty_email, empty_password
    finally:
        _cleanup_company(mine_id)
        _cleanup_company(other_id)
        _cleanup_company(foreign_id)


def test_save_and_next_stays_on_assigned_client() -> None:
    brown_id = _client_id("brown")
    uid, _email, _password = _provision("Robert", "Next", brown_id)
    stamp = secrets.token_hex(3)
    first_id, first_rn = _insert_new_company(brown_id, uid, f"{stamp}1")
    second_id, second_rn = _insert_new_company(brown_id, uid, f"{stamp}2")
    try:
        queue = list_work_queue(uid, client_id=brown_id, work_type="new")
        items = [item for item in queue.items if item.external_record_no in {first_rn, second_rn}]
        if len(items) < 2:
            _fail(f"expected two New rows, got {len(items)}")
        current = items[0]
        nxt = get_work_queue_next(
            uid,
            client_id=brown_id,
            work_type="new",
            after_queue_item_id=current.queue_item_id,
            after_work_priority=current.work_priority,
            after_company_name=current.company_name,
            after_company_id=current.company_id,
        )
        if not nxt.has_next or nxt.item is None:
            _fail("SAVE & NEXT had no next New Assignment.")
        if nxt.item.client_id != brown_id:
            _fail("SAVE & NEXT crossed clients.")
        if nxt.item.external_record_no not in {first_rn, second_rn}:
            _fail("SAVE & NEXT opened a stale unrelated record.")
        if nxt.item.queue_item_id == current.queue_item_id:
            _fail("SAVE & NEXT looped the same record.")
        update_relationship_status(
            current.external_record_no,
            client_id=brown_id,
            status="Left Message",
            user="Robert Next",
        )
        after = list_work_queue(uid, client_id=brown_id, work_type="new")
        if any(item.external_record_no == current.external_record_no for item in after.items):
            _fail("Record remained in New after status change.")
    finally:
        _cleanup_company(first_id)
        _cleanup_company(second_id)


def test_isolation_matrix_brown_dawson_premier() -> None:
    mapping = (
        ("Robert", "brown"),
        ("Tyler", "dawson"),
        ("Todd", "premier"),
    )
    clients = {code: _client_id(code) for _, code in mapping}
    http = TestClient(app)
    for first, code in mapping:
        cid = clients[code]
        uid, email, password = _provision(first, "Iso", cid)
        csrf = _login(http, email, password)
        headers = _headers(csrf)
        if http.get(f"/api/prospects?client_id={cid}&limit=3", headers=headers).status_code != 200:
            _fail(f"{code} own prospects denied")
        if http.get(f"/api/search?q=test&client_id={cid}", headers=headers).status_code != 200:
            _fail(f"{code} own search denied")
        if http.get(f"/api/work-queue?client_id={cid}&type=new", headers=headers).status_code != 200:
            _fail(f"{code} own queue denied")
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no) AS rn
                FROM client_company_relationships ccr
                JOIN companies co ON co.id = ccr.company_id
                WHERE ccr.client_id = ?
                ORDER BY ccr.id LIMIT 1
                """,
                (cid,),
            ).fetchone()
        if row is None:
            _fail(f"{code} has no CCR to open")
        company = http.get(
            f"/api/companies/by-record/{row['rn']}?client_id={cid}",
            headers=headers,
        )
        if company.status_code != 200:
            _fail(f"{code} own company {company.status_code} {company.text[:200]}")
        for other_code, other_id in clients.items():
            if other_id == cid:
                continue
            if http.get(f"/api/prospects?client_id={other_id}&limit=1", headers=headers).status_code != 403:
                _fail(f"{code} prospects {other_code} not 403")
            if http.get(f"/api/search?q=test&client_id={other_id}", headers=headers).status_code != 403:
                _fail(f"{code} search {other_code} not 403")
            if http.get(f"/api/work-queue?client_id={other_id}", headers=headers).status_code != 403:
                _fail(f"{code} queue {other_code} not 403")
        for path in (
            "/api/admin/master-data-export",
            "/api/admin/leadmaster-refresh/meta",
            "/api/admin/data-steward/meta",
        ):
            status = http.get(path, headers=headers).status_code
            if status != 403:
                _fail(f"{code} {path} expected 403, got {status}")
        ri = http.post(
            f"/api/clients/{cid}/admin/research-imports",
            headers=headers,
            files={"file": ("x.xlsx", b"not-an-xlsx", "application/vnd.ms-excel")},
        )
        if ri.status_code not in {403, 404}:
            _fail(f"{code} research-imports expected 403, got {ri.status_code}")


def main() -> int:
    from pathlib import Path
    from db import DB_PATH

    if Path(DB_PATH).resolve() == PRODUCTION_DB_PATH.resolve():
        _fail("Refusing to run PW-2A tests against live northstar.db")
    test_dawson_operational_statuses_without_catalog_mutation()
    test_specialist_new_assignments_are_own_book()
    test_save_and_next_stays_on_assigned_client()
    test_isolation_matrix_brown_dawson_premier()
    print("test_pw2a_pilot_blockers: ok")
    return 0


if __name__ == "__main__":
    from pathlib import Path
    from db import DB_PATH

    if Path(DB_PATH).resolve() == PRODUCTION_DB_PATH.resolve():
        raise SystemExit("Refusing to run PW-2A tests against live northstar.db")
    raise SystemExit(main())
