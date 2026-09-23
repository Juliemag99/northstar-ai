"""DS-9 governed Master Company archive + restore.

Isolated testdb / TestClient only. Never writes live northstar.db.
Does not enable merge, hard delete, contact archive, or LeadMaster confirm.
"""

from __future__ import annotations

import inspect
import os
import secrets
from contextlib import contextmanager

import testdb  # noqa: F401

from fastapi.testclient import TestClient

from auth_http import AUTH_REQUIRED_DETAIL, CSRF_HEADER, ENFORCE_FLAG
from auth_passwords import hash_password
from company_match import find_scored_matches
from data_steward import (
    ACTION_ARCHIVE_MASTER_COMPANY,
    ACTION_RESTORE_MASTER_COMPANY,
    SOURCE_MANUAL_ADMIN,
    StewardError,
    _archive_row,
    archive_company,
    create_company,
    create_contact,
    delete_company_permanent,
    find_company_duplicates,
    latest_provenance,
    link_relationship,
    list_active_company_relationships,
    list_operational_companies,
    live_ccr_lifecycle_enabled,
    live_destructive_enabled,
    live_master_archive_enabled,
    provenance_history,
    remove_relationship,
    restore_company,
    restore_relationship,
)
from data_steward_amend import search_master_companies
from data_steward_archive import (
    MasterArchiveRequest,
    confirm_archive_company,
    confirm_restore_company,
    preview_archive_company,
    preview_restore_company,
)
from db import PRODUCTION_DB_PATH, get_connection
from leadmaster_refresh_http import refresh_meta
from main import app
from models import NorthStarUser
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _stamp() -> str:
    return secrets.token_hex(4)


def _admin_user() -> NorthStarUser:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, full_name FROM users WHERE is_administrator=1 AND active=1 ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        _fail("isolated testdb has no administrator")
    return NorthStarUser(
        id=int(row["id"]),
        email=row["email"],
        full_name=row["full_name"],
        is_administrator=True,
        is_internal_northstar=True,
        active=True,
        created_at="",
    )


def _clients() -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, code FROM clients ORDER BY id").fetchall()
    out = {str(r["code"]): int(r["id"]) for r in rows}
    if "brown" not in out or "premier" not in out:
        _fail(f"need brown and premier clients, got {out}")
    return out


def _login(http: TestClient, email: str, password: str) -> str:
    resp = http.post("/api/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        _fail(f"login failed {email} {resp.status_code} {resp.text}")
    return str(resp.json().get("csrf_token") or "")


def _set_password(user_id: int, email: str, password: str) -> None:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (digest, int(user_id)))
        conn.commit()


@contextmanager
def _enforcement_on():
    previous = os.environ.get(ENFORCE_FLAG)
    os.environ[ENFORCE_FLAG] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENFORCE_FLAG, None)
        else:
            os.environ[ENFORCE_FLAG] = previous


def _count_action(conn, company_id: int, action: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM field_provenance_events
            WHERE entity_type='company' AND entity_id=? AND action=?
            """,
            (int(company_id), action),
        ).fetchone()[0]
    )


def _seed_shared(conn, actor: NorthStarUser, *, name: str | None = None) -> dict[str, int]:
    clients = _clients()
    created = create_company(
        conn,
        actor=actor,
        company_name=name or f"DS9 Shared {_stamp()}",
        city="Alma",
        state="MI",
        address="100 Industrial",
        website="https://ds9-shared.example",
        phone="9895550199",
    )
    cid = int(created["company_id"])
    brown = link_relationship(
        conn, actor=actor, client_id=clients["brown"], company_id=cid, status="Working"
    )
    premier = link_relationship(
        conn, actor=actor, client_id=clients["premier"], company_id=cid, status="New"
    )
    contact = create_contact(
        conn,
        actor=actor,
        company_id=cid,
        first_name="Pat",
        last_name=f"DS9{_stamp()}",
        email=f"pat.ds9.{_stamp()}@example.test",
    )
    conn.execute(
        """
        UPDATE client_company_relationships
        SET assigned_user_id=?, notes=?, external_record_no=?
        WHERE id=?
        """,
        (int(actor.id), "Keep this CCR note", f"DS9RN{cid}", brown),
    )
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_aliases'"
    ).fetchone():
        try:
            conn.execute(
                """
                INSERT INTO company_aliases (company_id, alias_name, alias_norm, source_system)
                VALUES (?, ?, ?, 'TEST')
                """,
                (cid, f"DS9 Alias {cid}", f"ds9 alias {cid}"),
            )
        except Exception:
            pass
    camp_id = 0
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_companies'"
    ).fetchone():
        camp_table = (
            "client_campaigns"
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_campaigns'"
            ).fetchone()
            else "campaigns"
        )
        camp = None
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (camp_table,),
        ).fetchone():
            camp = conn.execute(
                f"SELECT id FROM {camp_table} WHERE client_id=? ORDER BY id LIMIT 1",
                (clients["brown"],),
            ).fetchone()
        if camp is not None:
            camp_id = int(camp["id"])
            try:
                conn.execute(
                    """
                    INSERT INTO campaign_companies (campaign_id, client_id, company_id, relationship_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (camp_id, clients["brown"], cid, brown),
                )
            except Exception:
                camp_id = 0
    conn.commit()
    return {
        "company_id": cid,
        "brown_ccr": int(brown),
        "premier_ccr": int(premier),
        "brown_client": clients["brown"],
        "premier_client": clients["premier"],
        "contact_id": int(contact["contact_id"]),
        "campaign_id": camp_id,
    }


def test_production_path_untouched() -> None:
    live = str(PRODUCTION_DB_PATH).replace("\\", "/").lower()
    with get_connection() as conn:
        path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if path.replace("\\", "/").lower() == live:
        _fail(f"test connection is live production db: {path}")


def test_gates_master_archive_only() -> None:
    if not live_master_archive_enabled():
        _fail("master archive gate should be enabled")
    if not live_ccr_lifecycle_enabled():
        _fail("CCR lifecycle gate should stay enabled")
    if live_destructive_enabled():
        _fail("destructive steward gate must stay false")
    if "assert_not_production_db" not in inspect.getsource(_archive_row):
        _fail("generic archive_row lost live refusal")
    if "assert_not_production_db" not in inspect.getsource(delete_company_permanent):
        _fail("hard delete lost live refusal")
    meta = refresh_meta()
    if meta.get("live_confirm_enabled"):
        _fail("LeadMaster confirm enabled")


def test_active_ccr_blocks_archive_no_bypass() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        cid = seed["company_id"]
        preview = preview_archive_company(conn, actor=actor, company_id=cid, reason="Closed")
        if preview.get("eligible") or not preview.get("blocked"):
            _fail("active Brown CCR must block Master Archive")
        if "Remove this company from all active clients" not in str(preview.get("instruction")):
            _fail("missing remove-from-client instruction")
        try:
            archive_company(conn, actor=actor, company_id=cid, reason="Closed")
            _fail("archive_company should refuse active CCR")
        except StewardError as exc:
            if str(exc) != "active_ccr_blocks_archive":
                _fail(f"unexpected archive error {exc}")
        try:
            confirm_archive_company(
                conn,
                actor=actor,
                company_id=cid,
                body=MasterArchiveRequest(
                    reason="Closed",
                    confirm=True,
                    preview_fingerprint=preview["preview_fingerprint"],
                    force=True,
                    actor_id=999999,
                ),
            )
            _fail("force=true must not bypass active CCR")
        except StewardError as exc:
            if str(exc) != "active_ccr_blocks_archive":
                _fail(f"force bypass error {exc}")
        remove_relationship(conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Brown out")
        still = preview_archive_company(conn, actor=actor, company_id=cid, reason="Closed")
        if still.get("eligible") or not still.get("blocked"):
            _fail("Premier still active must keep blocking")
        remove_relationship(conn, actor=actor, ccr_id=seed["premier_ccr"], reason="Premier out")
        eligible = preview_archive_company(conn, actor=actor, company_id=cid, reason="Closed")
        if not eligible.get("eligible") or eligible.get("blocked"):
            _fail(f"zero active CCR should be eligible {eligible}")


def test_archive_restore_preserves_identity_and_not_ccr() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        cid = seed["company_id"]
        remove_relationship(conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Brown out")
        remove_relationship(conn, actor=actor, ccr_id=seed["premier_ccr"], reason="Premier out")
        before = conn.execute(
            """
            SELECT company_name, external_record_no, website, legacy_phone
            FROM companies WHERE id=?
            """,
            (cid,),
        ).fetchone()
        contacts_before = int(
            conn.execute("SELECT COUNT(*) FROM contacts WHERE company_id=?", (cid,)).fetchone()[0]
        )
        ccr_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM client_company_relationships WHERE company_id=?",
                (cid,),
            ).fetchone()[0]
        )
        alias_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM company_aliases WHERE company_id=?", (cid,)
            ).fetchone()[0]
        ) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_aliases'"
        ).fetchone() else 0
        camp_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM campaign_companies WHERE company_id=?", (cid,)
            ).fetchone()[0]
        ) if seed["campaign_id"] else 0
        preview = preview_archive_company(
            conn, actor=actor, company_id=cid, reason="Company permanently closed"
        )
        result = confirm_archive_company(
            conn,
            actor=actor,
            company_id=cid,
            body=MasterArchiveRequest(
                reason="Company permanently closed",
                confirm=True,
                preview_fingerprint=preview["preview_fingerprint"],
                expected_updated_at=preview["expected_updated_at"],
                expected_archived_at=preview["expected_archived_at"],
                actor_id=999999,
                created_by="Impostor",
            ),
        )
        if result.get("noop") or int(result.get("company_id") or 0) != cid:
            _fail(f"archive did not keep same id {result}")
        exists = conn.execute("SELECT COUNT(*) FROM companies WHERE id=?", (cid,)).fetchone()[0]
        if int(exists) != 1:
            _fail("company row was deleted")
        after = conn.execute(
            """
            SELECT company_name, external_record_no, website, legacy_phone, archived_at, archive_reason
            FROM companies WHERE id=?
            """,
            (cid,),
        ).fetchone()
        if _blank_row(after, "company_name") != _blank_row(before, "company_name"):
            _fail("company name changed")
        if _blank_row(after, "external_record_no") != _blank_row(before, "external_record_no"):
            _fail("external RN changed")
        if not _blank_row(after, "archived_at"):
            _fail("archived_at not set")
        if int(conn.execute("SELECT COUNT(*) FROM contacts WHERE company_id=?", (cid,)).fetchone()[0]) != contacts_before:
            _fail("contacts not preserved")
        if int(conn.execute("SELECT COUNT(*) FROM client_company_relationships WHERE company_id=?", (cid,)).fetchone()[0]) != ccr_before:
            _fail("CCR rows not preserved")
        active = list_active_company_relationships(conn, cid)
        if active:
            _fail(f"archive left active CCR {active}")
        if cid in {int(r["id"]) for r in list_operational_companies(conn)}:
            _fail("archived company still in operational list")
        admin_hits = search_master_companies(conn, _blank_row(after, "company_name"), visibility="archived")
        if cid not in {int(h["company_id"]) for h in admin_hits}:
            _fail("archived company missing from Admin Master Data")
        dups = find_company_duplicates(conn, company_name=_blank_row(after, "company_name"), city="Alma", state="MI")
        if not any(int(d["id"]) == cid and d.get("archived") for d in dups):
            _fail("duplicate detection missed archived company")
        scored = find_scored_matches(
            conn,
            client_id=seed["brown_client"],
            company_name=_blank_row(after, "company_name"),
            city="Alma",
            state="MI",
            website="https://ds9-shared.example",
            include_ai=False,
        )
        if not any(int(m["company_id"]) == cid and m.get("archived") for m in scored):
            _fail("Add Company matching missed archived company")
        if alias_before:
            alias_after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM company_aliases WHERE company_id=?", (cid,)
                ).fetchone()[0]
            )
            if alias_after != alias_before:
                _fail("aliases not preserved")
        if camp_before:
            camp_after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM campaign_companies WHERE company_id=?", (cid,)
                ).fetchone()[0]
            )
            if camp_after != camp_before:
                _fail("campaign history not preserved")
        event = latest_provenance(conn, entity_type="company", entity_id=cid, field="archived_at")
        if event is None or event.get("action") != ACTION_ARCHIVE_MASTER_COMPANY:
            _fail(f"missing archive provenance {event}")
        if event.get("source_type") != SOURCE_MANUAL_ADMIN:
            _fail("archive source is not MANUAL_ADMIN")
        if int(event.get("changed_by_user_id") or 0) != int(actor.id):
            _fail("spoof actor was used")
        if _blank_row(event, "reason") != "Company permanently closed":
            _fail("archive reason missing")

        again = archive_company(conn, actor=actor, company_id=cid, reason="Company permanently closed")
        if not again.get("noop"):
            _fail("repeat archive should no-op")
        if _count_action(conn, cid, ACTION_ARCHIVE_MASTER_COMPANY) != 1:
            _fail("duplicate archive provenance")

        restore_preview = preview_restore_company(
            conn, actor=actor, company_id=cid, reason="Return to active use"
        )
        if "does not create a new company" not in str(restore_preview.get("warning")):
            _fail("restore same-company messaging missing")
        restored = confirm_restore_company(
            conn,
            actor=actor,
            company_id=cid,
            body=MasterArchiveRequest(
                reason="Return to active use",
                confirm=True,
                preview_fingerprint=restore_preview["preview_fingerprint"],
                expected_updated_at=restore_preview["expected_updated_at"],
                expected_archived_at=restore_preview["expected_archived_at"],
            ),
        )
        if int(restored.get("company_id") or 0) != cid:
            _fail("restore changed company id")
        if restored.get("archived"):
            _fail("company still archived after restore")
        if restored.get("active_ccr_count"):
            _fail("restore reactivated CCR")
        brown = conn.execute(
            "SELECT archived_at FROM client_company_relationships WHERE id=?",
            (seed["brown_ccr"],),
        ).fetchone()
        premier = conn.execute(
            "SELECT archived_at FROM client_company_relationships WHERE id=?",
            (seed["premier_ccr"],),
        ).fetchone()
        if not _blank_row(brown, "archived_at") or not _blank_row(premier, "archived_at"):
            _fail("restore cleared CCR archive fields")
        restore_event = latest_provenance(conn, entity_type="company", entity_id=cid, field="archived_at")
        if restore_event is None or restore_event.get("action") != ACTION_RESTORE_MASTER_COMPANY:
            _fail(f"missing restore provenance {restore_event}")
        restore_company(conn, actor=actor, company_id=cid, reason="Return to active use")
        if _count_action(conn, cid, ACTION_RESTORE_MASTER_COMPANY) != 1:
            _fail("duplicate restore provenance")
        restore_relationship(conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Brown back")
        active = list_active_company_relationships(conn, cid)
        if [int(r["ccr_id"]) for r in active] != [seed["brown_ccr"]]:
            _fail(f"only Brown CCR should be active {active}")


def _blank_row(row, key: str) -> str:
    if row is None:
        return ""
    value = row[key] if key in row.keys() else ""
    return str(value or "").strip()


def test_restore_collision_blocks_no_merge() -> None:
    actor = _admin_user()
    stamp = _stamp()
    name = f"DS9 Twin {stamp}"
    with get_connection() as conn:
        first = create_company(
            conn, actor=actor, company_name=name, city="Green Bay", state="WI", address="1 Packer Ave"
        )
        second = create_company(
            conn,
            actor=actor,
            company_name=name,
            city="Green Bay",
            state="WI",
            address="1 Packer Ave",
            confirm_despite_match=True,
        )
        cid = int(first["company_id"])
        archive_company(conn, actor=actor, company_id=cid, reason="Duplicate retired after review")
        preview = preview_restore_company(conn, actor=actor, company_id=cid, reason="Reactivate")
        if not preview.get("blocked") or preview.get("eligible"):
            _fail(f"HIGH collision should block restore {preview.get('collisions')}")
        if int(second["company_id"]) not in {int(c["company_id"]) for c in preview.get("collisions") or []}:
            _fail("collision did not name the active twin")
        try:
            confirm_restore_company(
                conn,
                actor=actor,
                company_id=cid,
                body=MasterArchiveRequest(
                    reason="Reactivate",
                    confirm=True,
                    preview_fingerprint=preview["preview_fingerprint"],
                ),
            )
            _fail("restore confirm should refuse collision")
        except StewardError as exc:
            if str(exc) != "restore_collision":
                _fail(f"unexpected restore error {exc}")
        still = conn.execute("SELECT archived_at FROM companies WHERE id=?", (cid,)).fetchone()
        if not _blank_row(still, "archived_at"):
            _fail("blocked restore archived the company")
        approvals = 0
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_merge_approvals'"
        ).fetchone():
            approvals = int(conn.execute("SELECT COUNT(*) FROM company_merge_approvals").fetchone()[0])
        if approvals:
            _fail("restore created merge approvals")


def test_stale_preview_and_reason() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        cid = seed["company_id"]
        preview = preview_archive_company(conn, actor=actor, company_id=cid, reason="")
        if preview.get("reason_ok"):
            _fail("blank reason should fail reason_ok")
        try:
            confirm_archive_company(
                conn,
                actor=actor,
                company_id=cid,
                body=MasterArchiveRequest(reason="  ", confirm=True, preview_fingerprint="x"),
            )
            _fail("blank reason should refuse")
        except StewardError as exc:
            if str(exc) != "reason_required":
                _fail(f"expected reason_required got {exc}")
        remove_relationship(conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Brown out")
        remove_relationship(conn, actor=actor, ccr_id=seed["premier_ccr"], reason="Premier out")
        fresh = preview_archive_company(conn, actor=actor, company_id=cid, reason="Closed")
        restore_relationship(conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Brown back")
        try:
            confirm_archive_company(
                conn,
                actor=actor,
                company_id=cid,
                body=MasterArchiveRequest(
                    reason="Closed",
                    confirm=True,
                    preview_fingerprint=fresh["preview_fingerprint"],
                    expected_updated_at=fresh["expected_updated_at"],
                    expected_archived_at=fresh["expected_archived_at"],
                ),
            )
            _fail("stale preview after CCR restore should refuse")
        except StewardError as exc:
            if str(exc) != "stale_preview":
                _fail(f"expected stale_preview got {exc}")


def test_http_admin_only_and_other_gates_off() -> None:
    actor = _admin_user()
    password = f"NsTest9{secrets.token_hex(8)}"
    _set_password(int(actor.id), actor.email, password)
    http = TestClient(app)
    csrf = _login(http, actor.email, password)
    headers = {CSRF_HEADER: csrf}
    meta = http.get("/api/admin/data-steward/meta")
    if meta.status_code != 200:
        _fail(f"admin meta {meta.status_code}")
    body = meta.json()
    if not body.get("archive_enabled") or not body.get("company_restore_enabled"):
        _fail(f"archive flags off {body}")
    if body.get("delete_enabled") or body.get("merge_enabled") or body.get("live_mutations_enabled"):
        _fail(f"destructive flags enabled {body}")

    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        cid = seed["company_id"]

    blocked = http.post(
        f"/api/admin/data-steward/companies/{cid}/archive/preview",
        json={"reason": "Closed", "actor_id": 999999, "force": True},
        headers=headers,
    )
    if blocked.status_code != 200:
        _fail(f"preview {blocked.status_code} {blocked.text}")
    if blocked.json().get("writes") is not False:
        _fail("HTTP preview wrote")
    if blocked.json().get("eligible"):
        _fail("HTTP preview eligible with active CCR")
    confirm_blocked = http.post(
        f"/api/admin/data-steward/companies/{cid}/archive",
        json={
            "reason": "Closed",
            "confirm": True,
            "force": True,
            "preview_fingerprint": blocked.json().get("preview_fingerprint"),
        },
        headers=headers,
    )
    if confirm_blocked.status_code != 409:
        _fail(f"active CCR confirm {confirm_blocked.status_code} {confirm_blocked.text}")

    with get_connection() as conn:
        remove_relationship(conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Brown out")
        remove_relationship(conn, actor=actor, ccr_id=seed["premier_ccr"], reason="Premier out")
        conn.commit()

    preview = http.post(
        f"/api/admin/data-steward/companies/{cid}/archive/preview",
        json={"reason": "Closed"},
        headers=headers,
    )
    if preview.status_code != 200 or not preview.json().get("eligible"):
        _fail(f"eligible preview {preview.status_code} {preview.text}")
    saved = http.post(
        f"/api/admin/data-steward/companies/{cid}/archive",
        json={
            "reason": "Closed",
            "confirm": True,
            "preview_fingerprint": preview.json().get("preview_fingerprint"),
            "expected_updated_at": preview.json().get("expected_updated_at"),
            "expected_archived_at": preview.json().get("expected_archived_at"),
            "actor_id": 999999,
        },
        headers=headers,
    )
    if saved.status_code != 200:
        _fail(f"archive {saved.status_code} {saved.text}")
    restore_preview = http.post(
        f"/api/admin/data-steward/companies/{cid}/restore/preview",
        json={"reason": "Reactivate"},
        headers=headers,
    )
    if restore_preview.status_code != 200:
        _fail(f"restore preview {restore_preview.status_code} {restore_preview.text}")
    restored = http.post(
        f"/api/admin/data-steward/companies/{cid}/restore",
        json={
            "reason": "Reactivate",
            "confirm": True,
            "preview_fingerprint": restore_preview.json().get("preview_fingerprint"),
            "expected_updated_at": restore_preview.json().get("expected_updated_at"),
            "expected_archived_at": restore_preview.json().get("expected_archived_at"),
        },
        headers=headers,
    )
    if restored.status_code != 200:
        _fail(f"restore {restored.status_code} {restored.text}")

    specialist_pw = f"NsTest9{secrets.token_hex(8)}"
    spec = provision_staff_user(
        first_name="Dana",
        last_name="Specialist",
        email=f"ds9.spec.{_stamp()}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[seed["brown_client"]],
    )
    email = (spec.get("user") or {}).get("email") or spec.get("email")
    spec_csrf = _login(http, email, specialist_pw)
    denied = http.post(
        f"/api/admin/data-steward/companies/{cid}/archive",
        json={"reason": "nope", "confirm": True},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied.status_code != 403:
        _fail(f"specialist archive {denied.status_code}")
    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.post(
            f"/api/admin/data-steward/companies/{cid}/archive",
            json={"reason": "nope", "confirm": True},
        )
        if unauth.status_code != 401:
            _fail(f"unauthenticated archive {unauth.status_code}")
        if unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated detail {unauth.json()}")
    delete_route = http.request(
        "DELETE", f"/api/admin/data-steward/companies/{cid}", headers=headers
    )
    if delete_route.status_code not in {403, 404, 405, 422}:
        _fail(f"delete endpoint available {delete_route.status_code}")


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_gates_master_archive_only,
        test_active_ccr_blocks_archive_no_bypass,
        test_archive_restore_preserves_identity_and_not_ccr,
        test_restore_collision_blocks_no_merge,
        test_stale_preview_and_reason,
        test_http_admin_only_and_other_gates_off,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-9 isolated tests passed")
