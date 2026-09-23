"""DS-8 governed Remove From Client + Restore relationship.

Isolated testdb / TestClient only. Never writes live northstar.db.
Does not enable master archive, merge, hard delete, or LeadMaster confirm.
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
from data_steward import (
    ACTION_REMOVE_FROM_CLIENT,
    ACTION_RESTORE_TO_CLIENT,
    SOURCE_MANUAL_ADMIN,
    _archive_row,
    _restore_row,
    create_company,
    delete_company_permanent,
    latest_provenance,
    live_ccr_lifecycle_enabled,
    live_destructive_enabled,
    provenance_history,
)
from data_steward_amend import load_master_company, search_master_companies
from data_steward_relationship import (
    RelationshipActionRequest,
    confirm_remove_relationship,
    confirm_restore_relationship,
    preview_remove_relationship,
    preview_restore_relationship,
)
from db import PRODUCTION_DB_PATH, get_connection
from main import app
from models import NorthStarUser
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST
from work_queue_data import list_due_work_items, list_open_follow_up_tasks


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


def _seed_shared(conn, actor: NorthStarUser) -> dict[str, int]:
    clients = _clients()
    created = create_company(
        conn,
        actor=actor,
        company_name=f"DS8 Shared {_stamp()}",
        city="Alma",
        state="MI",
        website="https://ds8-shared.example",
        phone="9895550100",
    )
    cid = int(created["company_id"])
    from data_steward import link_relationship

    brown = link_relationship(
        conn, actor=actor, client_id=clients["brown"], company_id=cid, status="Working"
    )
    premier = link_relationship(
        conn, actor=actor, client_id=clients["premier"], company_id=cid, status="New"
    )
    conn.execute(
        """
        UPDATE client_company_relationships
        SET assigned_user_id=?, is_hot=1, follow_up_date=?, next_action=?, notes=?,
            external_record_no=?
        WHERE id=?
        """,
        (int(actor.id), "2026-09-01", "Call back", "Keep this note", f"DS8RN{cid}", brown),
    )
    camp = None
    camp_id = 0
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_companies'"
    ).fetchone():
        camp_table = "client_campaigns" if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_campaigns'"
        ).fetchone() else "campaigns"
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
        "campaign_id": camp_id,
    }


def test_production_path_untouched() -> None:
    live = str(PRODUCTION_DB_PATH).replace("\\", "/").lower()
    with get_connection() as conn:
        path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if path.replace("\\", "/").lower() == live:
        _fail(f"test connection is live production db: {path}")


def test_gates_ccr_only() -> None:
    if not live_ccr_lifecycle_enabled():
        _fail("CCR lifecycle gate should be enabled")
    if live_destructive_enabled():
        _fail("destructive steward gate must stay false")
    if "assert_not_production_db" not in inspect.getsource(_archive_row):
        _fail("master archive lost live refusal")
    if "assert_not_production_db" not in inspect.getsource(delete_company_permanent):
        _fail("hard delete lost live refusal")
    if "assert_not_production_db" not in inspect.getsource(_restore_row):
        _fail("master restore lost live refusal")


def test_remove_preview_reason_and_warnings() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        preview = preview_remove_relationship(
            conn, actor=actor, ccr_id=seed["brown_ccr"], reason=""
        )
        filled = preview_remove_relationship(
            conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Client no longer in book"
        )
    if preview["writes"] is not False:
        _fail("preview wrote data")
    if preview["reason_ok"]:
        _fail("blank reason should not be ok")
    if not filled["reason_ok"]:
        _fail("reason should be accepted")
    if filled["ccr_id"] != seed["brown_ccr"]:
        _fail("preview lost CCR id")
    codes = {d["code"] for d in filled["dependencies"]}
    if "assigned_rep" not in codes or "hot" not in codes or "open_follow_up" not in codes:
        _fail(f"expected warnings, got {codes}")
    if any(d["severity"] == "block" for d in filled["dependencies"]):
        _fail("conservative policy should not block")
    if "Master Company" not in filled["warning"]:
        _fail("missing preservation warning")


def test_remove_restore_same_ccr_preserves_and_filters() -> None:
    actor = _admin_user()
    from client_workspace_data import list_prospects
    from data_steward_amend import search_master_companies

    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        cid = seed["company_id"]
        brown = seed["brown_ccr"]
        before_contacts = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE company_id=?", (cid,)
        ).fetchone()[0]
        before_ccr = conn.execute(
            "SELECT COUNT(*) FROM client_company_relationships WHERE company_id=?", (cid,)
        ).fetchone()[0]
        notes_before = conn.execute(
            "SELECT notes FROM client_company_relationships WHERE id=?", (brown,)
        ).fetchone()[0]
        due_before = list_due_work_items(kind="follow_up", client_id=seed["brown_client"])
        due_ids_before = {item.relationship_id for item in due_before}
        if brown not in due_ids_before:
            # next_action Call back should classify as follow_up or call
            due_call = list_due_work_items(kind="call", client_id=seed["brown_client"])
            due_ids_before |= {item.relationship_id for item in due_call}
        body = RelationshipActionRequest(
            reason="Client no longer in Brown book",
            confirm=True,
        )
        preview = preview_remove_relationship(
            conn, actor=actor, ccr_id=brown, reason=body.reason
        )
        body.preview_fingerprint = preview["preview_fingerprint"]
        body.expected_updated_at = preview["expected_updated_at"]
        body.expected_archived_at = preview["expected_archived_at"]
        removed = confirm_remove_relationship(conn, actor=actor, ccr_id=brown, body=body)
        conn.commit()
        row = conn.execute(
            "SELECT id, archived_at, status, assigned_user_id, is_hot, notes, follow_up_date, next_action, external_record_no FROM client_company_relationships WHERE id=?",
            (brown,),
        ).fetchone()
        premier = conn.execute(
            "SELECT id, archived_at, status FROM client_company_relationships WHERE id=?",
            (seed["premier_ccr"],),
        ).fetchone()
        company = conn.execute("SELECT id FROM companies WHERE id=?", (cid,)).fetchone()
        after_contacts = conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE company_id=?", (cid,)
        ).fetchone()[0]
        after_ccr = conn.execute(
            "SELECT COUNT(*) FROM client_company_relationships WHERE company_id=?", (cid,)
        ).fetchone()[0]
        prov = latest_provenance(
            conn, entity_type="client_relationship", entity_id=brown, field="archived_at"
        )
        loaded = load_master_company(conn, cid)
        hits = search_master_companies(conn, loaded["company_name"][:12])
        prospects_brown = list_prospects(client_id=seed["brown_client"], q=loaded["company_name"])
        prospects_premier = list_prospects(
            client_id=seed["premier_client"], q=loaded["company_name"]
        )
        due_after = list_due_work_items(kind="follow_up", client_id=seed["brown_client"])
        due_after_ids = {item.relationship_id for item in due_after}
        due_after_ids |= {
            item.relationship_id
            for item in list_due_work_items(kind="call", client_id=seed["brown_client"])
        }
        tasks_after = [t.relationship_id for t in list_open_follow_up_tasks(client_id=seed["brown_client"])]
        repeat_preview = preview_remove_relationship(
            conn, actor=actor, ccr_id=brown, reason="Client no longer in Brown book"
        )
        repeat = confirm_remove_relationship(
            conn,
            actor=actor,
            ccr_id=brown,
            body=RelationshipActionRequest(
                reason="Client no longer in Brown book",
                confirm=True,
                preview_fingerprint=repeat_preview["preview_fingerprint"],
                expected_updated_at=repeat_preview["expected_updated_at"],
                expected_archived_at=repeat_preview["expected_archived_at"],
            ),
        )
        restore_preview = preview_restore_relationship(
            conn, actor=actor, ccr_id=brown, reason="Return to Brown book"
        )
        restore_body = RelationshipActionRequest(
            reason="Return to Brown book",
            confirm=True,
            preview_fingerprint=restore_preview["preview_fingerprint"],
            expected_updated_at=restore_preview["expected_updated_at"],
            expected_archived_at=restore_preview["expected_archived_at"],
        )
        restored = confirm_restore_relationship(
            conn, actor=actor, ccr_id=brown, body=restore_body
        )
        conn.commit()
        after_restore = conn.execute(
            "SELECT id, archived_at, status, assigned_user_id, is_hot, notes, follow_up_date, next_action, external_record_no FROM client_company_relationships WHERE id=?",
            (brown,),
        ).fetchone()
        restore_prov = latest_provenance(
            conn, entity_type="client_relationship", entity_id=brown, field="archived_at"
        )
        history = provenance_history(
            conn, entity_type="client_relationship", entity_id=brown, field="archived_at"
        )
        already_preview = preview_restore_relationship(
            conn, actor=actor, ccr_id=brown, reason="Return to Brown book"
        )
        already_active = confirm_restore_relationship(
            conn,
            actor=actor,
            ccr_id=brown,
            body=RelationshipActionRequest(
                reason="Return to Brown book",
                confirm=True,
                preview_fingerprint=already_preview["preview_fingerprint"],
                expected_updated_at=already_preview["expected_updated_at"],
                expected_archived_at=already_preview["expected_archived_at"],
            ),
        )
        camp_count = 0
        if seed["campaign_id"]:
            camp_count = conn.execute(
                "SELECT COUNT(*) FROM campaign_companies WHERE relationship_id=?",
                (brown,),
            ).fetchone()[0]

    if not removed.get("archived") or removed.get("ccr_id") != brown:
        _fail(f"remove failed {removed}")
    if not removed.get("same_id") or not removed.get("company_still_exists"):
        _fail("company or CCR identity lost")
    if int(row["id"]) != brown or not str(row["archived_at"] or "").strip():
        _fail("CCR was not archived in place")
    if str(premier["archived_at"] or "").strip():
        _fail("Premier CCR was archived")
    if company is None:
        _fail("Master Company deleted")
    if after_contacts != before_contacts or after_ccr != before_ccr:
        _fail("contacts or CCR rows changed")
    if row["notes"] != notes_before:
        _fail("notes were rewritten")
    if row["status"] != "Working" or int(row["is_hot"] or 0) != 1:
        _fail("status/hot changed on remove")
    if int(row["assigned_user_id"] or 0) != int(actor.id):
        _fail("assignment changed on remove")
    if row["follow_up_date"] != "2026-09-01" or row["next_action"] != "Call back":
        _fail("follow-up rewritten on remove")
    if prov is None or prov["action"] != ACTION_REMOVE_FROM_CLIENT:
        _fail(f"remove provenance {prov}")
    if prov["source_type"] != SOURCE_MANUAL_ADMIN:
        _fail("remove source was not MANUAL_ADMIN")
    if int(prov["changed_by_user_id"] or 0) != int(actor.id):
        _fail("remove actor spoofable")
    linked = {item["ccr_id"]: item for item in loaded["linked_clients"]}
    if brown not in linked or not linked[brown].get("archived"):
        _fail("admin discovery lost removed CCR")
    if not any(h["company_id"] == cid for h in hits):
        _fail("Master Data search lost company after CCR archive")
    if any(int(p.id) == cid for p in prospects_brown):
        _fail("archived CCR still in Brown prospects")
    if not any(int(p.id) == cid for p in prospects_premier):
        _fail("Premier prospects lost the shared company")
    if brown in due_after_ids or brown in tasks_after:
        _fail("archived CCR still in Brown due work")
    if not repeat.get("noop"):
        _fail("second remove should be a no-op")
    if restored.get("ccr_id") != brown or restored.get("archived"):
        _fail(f"restore failed {restored}")
    if str(after_restore["archived_at"] or "").strip():
        _fail("restore did not clear archived_at")
    if after_restore["status"] != "Working":
        _fail("restore reset status")
    if int(after_restore["assigned_user_id"] or 0) != int(actor.id):
        _fail("restore unassigned the rep")
    if int(after_restore["is_hot"] or 0) != 1:
        _fail("restore cleared Hot")
    if after_restore["notes"] != notes_before:
        _fail("restore changed notes")
    if after_restore["follow_up_date"] != "2026-09-01":
        _fail("restore rewrote follow-up date")
    if restore_prov is None or restore_prov["action"] != ACTION_RESTORE_TO_CLIENT:
        _fail(f"restore provenance {restore_prov}")
    actions = [_blank_action(item) for item in history]
    if ACTION_REMOVE_FROM_CLIENT not in actions or ACTION_RESTORE_TO_CLIENT not in actions:
        _fail(f"audit trail missing remove/restore {actions}")
    if already_active.get("noop") is not True:
        _fail("second restore should be already_active no-op")
    if seed["campaign_id"] and camp_count != 1:
        _fail(f"campaign membership was duplicated or deleted {camp_count}")


def _blank_action(item: dict) -> str:
    return str(item.get("action") or "").strip()


def test_stale_preview_and_reason() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        preview = preview_remove_relationship(
            conn, actor=actor, ccr_id=seed["brown_ccr"], reason="Stale check"
        )
        conn.execute(
            "UPDATE client_company_relationships SET status='Contacted', updated_at=? WHERE id=?",
            ("changed-after-preview", seed["brown_ccr"]),
        )
        conn.commit()
        body = RelationshipActionRequest(
            reason="Stale check",
            confirm=True,
            preview_fingerprint=preview["preview_fingerprint"],
            expected_updated_at=preview["expected_updated_at"],
            expected_archived_at=preview["expected_archived_at"],
        )
        try:
            confirm_remove_relationship(conn, actor=actor, ccr_id=seed["brown_ccr"], body=body)
            _fail("stale preview should refuse")
        except Exception as exc:
            if str(exc) not in {"stale_preview", "stale_edit"}:
                _fail(f"expected stale, got {exc}")
        try:
            confirm_remove_relationship(
                conn,
                actor=actor,
                ccr_id=seed["brown_ccr"],
                body=RelationshipActionRequest(reason="  ", confirm=True),
            )
            _fail("blank reason should fail")
        except Exception as exc:
            if str(exc) != "reason_required":
                _fail(f"expected reason_required got {exc}")
        try:
            confirm_remove_relationship(
                conn,
                actor=actor,
                ccr_id=seed["brown_ccr"],
                body=RelationshipActionRequest(reason="Need confirm", confirm=False),
            )
            _fail("missing confirm should fail")
        except Exception as exc:
            if str(exc) != "confirmation_required":
                _fail(f"expected confirmation_required got {exc}")


def test_http_admin_only_and_destructive_still_off() -> None:
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
    if not body.get("remove_relationship_enabled") or not body.get("restore_enabled"):
        _fail(f"CCR flags off {body}")
    if body.get("delete_enabled") or body.get("merge_enabled"):
        _fail(f"destructive flags enabled {body}")
    if not body.get("archive_enabled"):
        _fail("archive_enabled should be on after DS-9")
    if body.get("live_mutations_enabled"):
        _fail("broad live mutations enabled")

    with get_connection() as conn:
        seed = _seed_shared(conn, actor)
        ccr_id = seed["brown_ccr"]

    preview = http.post(
        f"/api/admin/data-steward/relationships/{ccr_id}/remove/preview",
        json={"reason": "HTTP remove", "actor_id": 999999, "created_by": "Impostor"},
        headers=headers,
    )
    if preview.status_code != 200:
        _fail(f"preview {preview.status_code} {preview.text}")
    if preview.json().get("writes") is not False:
        _fail("HTTP preview wrote")
    save = http.post(
        f"/api/admin/data-steward/relationships/{ccr_id}/remove",
        json={
            "reason": "HTTP remove",
            "confirm": True,
            "preview_fingerprint": preview.json().get("preview_fingerprint"),
            "expected_updated_at": preview.json().get("expected_updated_at"),
            "expected_archived_at": preview.json().get("expected_archived_at"),
            "actor_id": 999999,
        },
        headers=headers,
    )
    if save.status_code != 200:
        _fail(f"remove {save.status_code} {save.text}")
    with get_connection() as conn:
        row = latest_provenance(
            conn, entity_type="client_relationship", entity_id=ccr_id, field="archived_at"
        )
        if row is None or int(row["changed_by_user_id"] or 0) != int(actor.id):
            _fail(f"spoofed actor used {row}")
    events = http.get(
        f"/api/admin/data-steward/companies/{seed['company_id']}/relationship-events"
    )
    if events.status_code != 200 or not events.json().get("events"):
        _fail(f"relationship audit {events.status_code} {events.text}")
    restore_preview = http.post(
        f"/api/admin/data-steward/relationships/{ccr_id}/restore/preview",
        json={"reason": "HTTP restore"},
        headers=headers,
    )
    if restore_preview.status_code != 200:
        _fail(f"restore preview {restore_preview.status_code} {restore_preview.text}")
    restored = http.post(
        f"/api/admin/data-steward/relationships/{ccr_id}/restore",
        json={
            "reason": "HTTP restore",
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
        email=f"ds8.spec.{_stamp()}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[seed["brown_client"]],
    )
    email = (spec.get("user") or {}).get("email") or spec.get("email")
    spec_csrf = _login(http, email, specialist_pw)
    denied = http.post(
        f"/api/admin/data-steward/relationships/{ccr_id}/remove",
        json={"reason": "nope", "confirm": True},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied.status_code != 403:
        _fail(f"specialist remove {denied.status_code}")
    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.post(
            f"/api/admin/data-steward/relationships/{ccr_id}/remove",
            json={"reason": "nope", "confirm": True},
        )
        if unauth.status_code != 401:
            _fail(f"unauthenticated remove {unauth.status_code}")
        if unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated detail {unauth.json()}")


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_gates_ccr_only,
        test_remove_preview_reason_and_warnings,
        test_remove_restore_same_ccr_preserves_and_filters,
        test_stale_preview_and_reason,
        test_http_admin_only_and_destructive_still_off,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-8 isolated tests passed")
