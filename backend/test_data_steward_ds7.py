"""DS-7 governed Master Company amend + audit.

Isolated testdb / TestClient only. Never writes live northstar.db.
Does not enable archive, merge, remove-from-client, or LeadMaster confirm.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager

import testdb  # noqa: F401

from fastapi.testclient import TestClient

from auth_http import AUTH_REQUIRED_DETAIL, CSRF_HEADER, ENFORCE_FLAG
from auth_passwords import hash_password
from data_steward import (
    SOURCE_MANUAL_ADMIN,
    amend_company,
    create_company,
    is_manual_authority,
    latest_provenance,
    live_company_amend_enabled,
    live_destructive_enabled,
    provenance_history,
)
from data_steward_amend import (
    canonicalize_address,
    canonicalize_phone_pair,
    canonicalize_website,
    find_amend_collisions,
    preview_company_amend,
    save_company_amend,
)
from db import PRODUCTION_DB_PATH, get_connection
from leadmaster_refresh_plan import (
    IncomingRow,
    MANUAL_OVERRIDE_CONFLICT,
    RefreshPolicy,
    plan_leadmaster_refresh,
)
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


def _client_id() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()
    if row is None:
        _fail("isolated testdb has no clients")
    return int(row["id"])


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


def test_production_path_untouched() -> None:
    live = str(PRODUCTION_DB_PATH).replace("\\", "/").lower()
    with get_connection() as conn:
        path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if path.replace("\\", "/").lower() == live:
        _fail(f"test connection is live production db: {path}")


def test_gates_amend_only() -> None:
    if not live_company_amend_enabled():
        _fail("company amend gate should be enabled")
    if live_destructive_enabled():
        _fail("destructive steward gate must stay false")
    from data_steward import _archive_row, _restore_row, delete_company_permanent, live_ccr_lifecycle_enabled
    import inspect

    if not live_ccr_lifecycle_enabled():
        _fail("CCR lifecycle gate should be enabled")
    if "assert_not_production_db" not in inspect.getsource(_archive_row):
        _fail("archive/restore lost live refusal")
    if "assert_not_production_db" not in inspect.getsource(delete_company_permanent):
        _fail("delete_company_permanent lost live refusal")
    if "assert_not_production_db" not in inspect.getsource(_restore_row):
        _fail("restore_company lost live refusal")


def test_create_provenance_records_populated_not_blank() -> None:
    actor = _admin_user()
    name = f"DS7 Create {_stamp()}"
    with get_connection() as conn:
        created = create_company(
            conn,
            actor=actor,
            company_name=name,
            city="Lansing",
            state="MI",
            website="https://ds7-create.example",
        )
        cid = int(created["company_id"])
        name_ev = latest_provenance(conn, entity_type="company", entity_id=cid, field="company_name")
        city_ev = latest_provenance(conn, entity_type="company", entity_id=cid, field="city")
        zip_ev = latest_provenance(conn, entity_type="company", entity_id=cid, field="zip")
        web_ev = latest_provenance(conn, entity_type="company", entity_id=cid, field="website")
    if name_ev is None or name_ev["source_type"] != SOURCE_MANUAL_ADMIN:
        _fail("populated company_name missing CREATE provenance")
    if city_ev is None or city_ev["new_value"] != "Lansing":
        _fail("populated city missing CREATE provenance")
    if web_ev is None:
        _fail("populated website missing CREATE provenance")
    if zip_ev is not None:
        _fail("blank zip should not receive CREATE provenance")


def test_canonicalization_and_noop() -> None:
    actor = _admin_user()
    name = f"DS7 Canon {_stamp()}"
    with get_connection() as conn:
        created = create_company(
            conn,
            actor=actor,
            company_name=name,
            address="100 North Main Street",
            city="Troy",
            state="mi",
            zip_code="48083",
            phone="2485550100",
            website="ds7-canon.example",
        )
        cid = int(created["company_id"])
        preview = preview_company_amend(
            conn,
            actor=actor,
            company_id=cid,
            fields={
                "address": "100 North Main Street",
                "state": "mi",
                "phone": "2485550100",
                "phone_extension": "16",
                "website": "ds7-canon.example",
            },
            reason="Canonical check",
        )
        if canonicalize_website("ds7-canon.example") != "https://ds7-canon.example":
            _fail(f"website canonical {canonicalize_website('ds7-canon.example')}")
        if canonicalize_address("100 North Main Street") != "100 N Main St":
            _fail(f"address canonical {canonicalize_address('100 North Main Street')}")
        main, ext = canonicalize_phone_pair("2485550100", "16")
        if main != "(248) 555-0100" or ext != "16":
            _fail(f"phone canonical {main!r} {ext!r}")
        saved = save_company_amend(
            conn,
            actor=actor,
            company_id=cid,
            fields={
                "state": "MI",
                "website": "https://ds7-canon.example",
            },
            reason="Canonical no-op after same stored website",
        )
        # state mi -> MI is a real change; website scheme-only may change
        _ = saved
        before = provenance_history(conn, entity_type="company", entity_id=cid, field="city")
        noop = save_company_amend(
            conn,
            actor=actor,
            company_id=cid,
            fields={"city": "Troy"},
            reason="No-op city",
        )
        after = provenance_history(conn, entity_type="company", entity_id=cid, field="city")
    if not noop.get("noop"):
        _fail("same city should be a no-op save")
    if len(after) != len(before):
        _fail("no-op save wrote provenance")
    if not preview["reason_ok"]:
        _fail("reason should be accepted on preview")


def test_duplicate_collision_blocks_save() -> None:
    actor = _admin_user()
    stamp = _stamp()
    with get_connection() as conn:
        first = create_company(
            conn,
            actor=actor,
            company_name=f"DS7 Twin {stamp}",
            city="Green Bay",
            state="WI",
            address="1 Packer Ave",
        )
        second = create_company(
            conn,
            actor=actor,
            company_name=f"DS7 Twin Other {stamp}",
            city="Madison",
            state="WI",
            confirm_despite_match=True,
        )
        collisions = find_amend_collisions(
            conn,
            company_id=int(second["company_id"]),
            proposed={
                "company_name": f"DS7 Twin {stamp}",
                "city": "Green Bay",
                "state": "WI",
                "address": "1 Packer Ave",
                "phone": "",
                "website": "",
                "zip": "",
                "phone_extension": "",
            },
        )
        blocked = [c for c in collisions if c["severity"] == "block"]
        try:
            save_company_amend(
                conn,
                actor=actor,
                company_id=int(second["company_id"]),
                fields={"company_name": f"DS7 Twin {stamp}", "city": "Green Bay", "state": "WI"},
                reason="Would collide",
            )
            _fail("duplicate save should have blocked")
        except Exception as exc:
            if str(exc) != "duplicate_company":
                _fail(f"expected duplicate_company, got {exc}")
    if not blocked:
        _fail(f"expected blocking collision, got {collisions}")
    if int(first["company_id"]) not in {int(c["company_id"]) for c in blocked}:
        _fail("collision did not name the existing twin")


def test_amend_provenance_actor_reason_manual_authority() -> None:
    actor = _admin_user()
    name = f"DS7 Auth {_stamp()}"
    with get_connection() as conn:
        created = create_company(
            conn, actor=actor, company_name=name, city="Alma", state="MI", website="https://old.example"
        )
        cid = int(created["company_id"])
        result = save_company_amend(
            conn,
            actor=actor,
            company_id=cid,
            fields={
                "company_name": f"{name} Inc",
                "address": "200 West Industrial Drive",
                "phone": "9895551212",
                "website": "www.ds7-auth.example",
            },
            reason="Verified company information",
        )
        if result.get("noop"):
            _fail("expected field changes")
        for field in ("company_name", "address", "phone", "website"):
            if not is_manual_authority(conn, entity_type="company", entity_id=cid, field=field):
                _fail(f"{field} is not MANUAL_ADMIN after amend")
            row = latest_provenance(conn, entity_type="company", entity_id=cid, field=field)
            if row is None:
                _fail(f"missing provenance for {field}")
            if row["source_type"] != SOURCE_MANUAL_ADMIN:
                _fail(f"{field} source {row['source_type']}")
            if int(row["changed_by_user_id"] or 0) != int(actor.id):
                _fail(f"{field} actor {row['changed_by_user_id']}")
            if row["reason"] != "Verified company information":
                _fail(f"{field} reason {row['reason']}")
            if not row["changed_at"]:
                _fail(f"{field} missing timestamp")
            if row["old_value"] == row["new_value"]:
                _fail(f"{field} old/new equal")


def test_leadmaster_planner_does_not_silently_overwrite_manual() -> None:
    actor = _admin_user()
    stamp = _stamp()
    rn = f"DS7RN{stamp}"
    client_id = _client_id()
    with get_connection() as conn:
        created = create_company(
            conn,
            actor=actor,
            company_name=f"DS7 LM {stamp}",
            address="10 Oak St",
            city="Saginaw",
            state="MI",
            phone="9895554444",
            website="https://ds7-lm-old.example",
        )
        cid = int(created["company_id"])
        conn.execute(
            "UPDATE companies SET external_record_no=? WHERE id=?",
            (rn, cid),
        )
        from data_steward import attach_leadmaster_identity, link_relationship

        link_relationship(conn, actor=actor, client_id=client_id, company_id=cid, status="New")
        attach_leadmaster_identity(conn, actor=actor, company_id=cid, record_no=rn, client_id=client_id)
        save_company_amend(
            conn,
            actor=actor,
            company_id=cid,
            fields={
                "company_name": f"DS7 LM Canonical {stamp}",
                "address": "200 West Industrial Drive",
                "phone": "9895557777",
                "website": "https://ds7-lm-new.example",
            },
            reason="Corrected company information",
        )
        plan = plan_leadmaster_refresh(
            conn,
            client_id=client_id,
            rows=[
                IncomingRow(
                    source_row=1,
                    record_no=rn,
                    company_name=f"DS7 LM {stamp}",
                    address="99 Stale Rd",
                    city="Saginaw",
                    state="MI",
                    company_phone="9895554444",
                    website="https://ds7-lm-old.example",
                    notes="",
                )
            ],
            policy=RefreshPolicy(client_id=client_id),
            source_sha256="ds7",
            source_filename="ds7.csv",
        )
        proposals = plan["rows"][0]["proposals"]
        by_field = {p["field"]: p["action"] for p in proposals}
    for field in ("company_name", "address", "phone", "website"):
        action = by_field.get(field)
        if action and action != MANUAL_OVERRIDE_CONFLICT:
            _fail(f"stale LM {field} action {action} should be MANUAL_OVERRIDE_CONFLICT")
        if field in {"company_name", "phone", "website"} and action != MANUAL_OVERRIDE_CONFLICT:
            _fail(f"missing manual override for {field}: {by_field}")


def test_http_admin_amend_specialist_forbidden_unauthenticated_401() -> None:
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
    if body.get("archive_enabled") or body.get("delete_enabled") or body.get("merge_enabled"):
        _fail(f"destructive flags enabled {body}")
    if not body.get("company_amend_enabled"):
        _fail("company_amend_enabled missing")

    stamp = _stamp()
    with get_connection() as conn:
        created = create_company(
            conn, actor=actor, company_name=f"DS7 HTTP {stamp}", city="Flint", state="MI"
        )
        cid = int(created["company_id"])

    preview = http.post(
        f"/api/admin/data-steward/companies/{cid}/amend/preview",
        json={
            "website": "https://ds7-http.example",
            "reason": "Updated website",
            "actor_id": 999999,
            "created_by": "Impostor",
        },
        headers=headers,
    )
    if preview.status_code != 200:
        _fail(f"preview {preview.status_code} {preview.text}")
    if preview.json().get("writes") is not False:
        _fail("preview wrote data")
    save = http.post(
        f"/api/admin/data-steward/companies/{cid}/amend",
        json={
            "website": "https://ds7-http.example",
            "reason": "Updated website",
            "expected_updated_at": preview.json().get("expected_updated_at"),
            "preview_fingerprint": preview.json().get("preview_fingerprint"),
            "actor_id": 999999,
            "created_by": "Impostor",
        },
        headers=headers,
    )
    if save.status_code != 200:
        _fail(f"save {save.status_code} {save.text}")
    with get_connection() as conn:
        row = latest_provenance(conn, entity_type="company", entity_id=cid, field="website")
        if row is None or int(row["changed_by_user_id"] or 0) != int(actor.id):
            _fail(f"spoofed actor was used: {row}")

    history = http.get(f"/api/admin/data-steward/provenance?entity_type=company&entity_id={cid}")
    if history.status_code != 200:
        _fail(f"provenance {history.status_code}")
    events = history.json().get("events") or []
    if not events:
        _fail("audit API returned no events")
    ids = [e["id"] for e in events]
    if ids != sorted(ids, reverse=True):
        _fail("provenance is not newest-first")

    missing = http.post(
        f"/api/admin/data-steward/companies/{cid}/amend",
        json={"website": "https://ds7-http-2.example", "reason": "   "},
        headers=headers,
    )
    if missing.status_code != 400:
        _fail(f"blank reason {missing.status_code} {missing.text}")

    specialist_pw = f"NsTest9{secrets.token_hex(8)}"
    spec = provision_staff_user(
        first_name="Dana",
        last_name="Specialist",
        email=f"ds7.spec.{stamp}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[_client_id()],
    )
    email = (spec.get("user") or {}).get("email") or spec.get("email")
    if not email:
        _fail(f"provision payload {spec}")
    spec_csrf = _login(http, email, specialist_pw)
    denied = http.get("/api/admin/data-steward/meta", headers={CSRF_HEADER: spec_csrf})
    if denied.status_code != 403:
        _fail(f"specialist meta {denied.status_code}")
    denied_save = http.post(
        f"/api/admin/data-steward/companies/{cid}/amend",
        json={"website": "https://nope.example", "reason": "nope"},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied_save.status_code != 403:
        _fail(f"specialist save {denied_save.status_code}")

    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.get("/api/admin/data-steward/meta")
        if unauth.status_code != 401:
            _fail(f"unauthenticated meta {unauth.status_code}")
        if unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated detail {unauth.json()}")
        unauth_save = anon.post(
            f"/api/admin/data-steward/companies/{cid}/amend",
            json={"website": "https://nope.example", "reason": "nope"},
        )
        if unauth_save.status_code != 401:
            _fail(f"unauthenticated save {unauth_save.status_code}")

    archive_route = http.post(f"/api/admin/data-steward/companies/{cid}/archive", headers=headers)
    if archive_route.status_code not in {403, 404, 405, 409}:
        _fail(f"archive route unexpectedly available {archive_route.status_code}")


def test_unchanged_companion_fields_skip_provenance() -> None:
    actor = _admin_user()
    name = f"DS7 Companion {_stamp()}"
    with get_connection() as conn:
        created = create_company(
            conn,
            actor=actor,
            company_name=name,
            city="Flint",
            state="MI",
            phone="8105550100",
            website="https://companion-old.example",
        )
        cid = int(created["company_id"])
        before_phone = provenance_history(conn, entity_type="company", entity_id=cid, field="phone")
        before_ext = provenance_history(
            conn, entity_type="company", entity_id=cid, field="phone_extension"
        )
        saved = save_company_amend(
            conn,
            actor=actor,
            company_id=cid,
            fields={
                "company_name": name,
                "city": "Flint",
                "state": "MI",
                "phone": "(810) 555-0100",
                "phone_extension": "",
                "website": "https://companion-new.example",
            },
            reason="Updated website",
        )
        after_phone = provenance_history(conn, entity_type="company", entity_id=cid, field="phone")
        after_ext = provenance_history(
            conn, entity_type="company", entity_id=cid, field="phone_extension"
        )
        web = latest_provenance(conn, entity_type="company", entity_id=cid, field="website")
    if saved.get("noop"):
        _fail("website change should not be a no-op")
    if "website" not in (saved.get("changed") or []):
        _fail(f"expected website in changed {saved}")
    if "phone" in (saved.get("changed") or []) or "phone_extension" in (saved.get("changed") or []):
        _fail(f"unchanged phone fields were written {saved}")
    if len(after_phone) != len(before_phone):
        _fail("unchanged phone received provenance")
    if len(after_ext) != len(before_ext):
        _fail("unchanged extension received provenance")
    if web is None or web["new_value"] != "https://companion-new.example":
        _fail(f"website provenance missing {web}")


def test_reason_required_engine() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        created = create_company(
            conn, actor=actor, company_name=f"DS7 Reason {_stamp()}", city="Alma", state="MI"
        )
        try:
            save_company_amend(
                conn,
                actor=actor,
                company_id=int(created["company_id"]),
                fields={"city": "Saginaw"},
                reason="  ",
            )
            _fail("blank reason should fail")
        except Exception as exc:
            if str(exc) != "reason_required":
                _fail(f"expected reason_required got {exc}")


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_gates_amend_only,
        test_create_provenance_records_populated_not_blank,
        test_canonicalization_and_noop,
        test_duplicate_collision_blocks_save,
        test_amend_provenance_actor_reason_manual_authority,
        test_leadmaster_planner_does_not_silently_overwrite_manual,
        test_reason_required_engine,
        test_unchanged_companion_fields_skip_provenance,
        test_http_admin_amend_specialist_forbidden_unauthenticated_401,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-7 isolated tests passed")
