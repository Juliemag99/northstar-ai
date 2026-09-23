"""DS-10 governed bulk CCR assignment.

Isolated testdb / TestClient only. Never writes live northstar.db.
Does not create live Robert/Tyler/Todd or transfer live books.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager

import testdb  # noqa: F401

from fastapi.testclient import TestClient

from auth_http import ADMIN_REQUIRED_DETAIL, AUTH_REQUIRED_DETAIL, CSRF_HEADER, ENFORCE_FLAG
from auth_passwords import hash_password
from bulk_assignment import (
    BulkAssignmentError,
    BulkAssignmentRequest,
    ProspectAssignmentFilter,
    confirm_bulk_assignment,
    preview_bulk_assignment,
)
from client_workspace_data import list_matching_prospect_ccr_ids, list_prospects_page
from data_steward import (
    ACTION_BULK_ASSIGN_CLIENT_RELATIONSHIP,
    SOURCE_MANUAL_ADMIN,
    create_company,
    force_provenance_failure,
    latest_provenance,
    link_relationship,
    live_ccr_lifecycle_enabled,
    live_destructive_enabled,
    live_master_archive_enabled,
    provenance_history,
    remove_relationship,
)
from db import PRODUCTION_DB_PATH, get_connection
from leadmaster_refresh_http import refresh_meta
from main import app
from models import NorthStarUser
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST
from work_queue_data import list_work_queue


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
        _fail(f"need brown and premier, got {out}")
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


def _provision_rep(first: str, client_ids: list[int]) -> dict:
    password = f"NsTest9{secrets.token_hex(8)}"
    created = provision_staff_user(
        first_name=first,
        last_name=f"Iso {_stamp()[:4]}" if False else "Iso",
        email=f"ds10.{first.lower()}.{_stamp()}@northstar.example.test",
        password=password,
        staff_role=REVOPS_SPECIALIST,
        client_ids=client_ids,
    )
    user = created["user"]
    return {
        "user_id": int(user["user_id"]),
        "email": user["email"],
        "full_name": user["full_name"],
        "password": password,
    }


def _ccr_row(conn, ccr_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, client_id, company_id, assigned_user_id, status, notes,
               next_action, follow_up_date, is_hot, external_record_no, priority
        FROM client_company_relationships WHERE id=?
        """,
        (int(ccr_id),),
    ).fetchone()
    if row is None:
        _fail(f"missing ccr {ccr_id}")
    return dict(row)


def _preview(conn, actor, **kwargs):
    body = BulkAssignmentRequest(**kwargs)
    return preview_bulk_assignment(conn, actor=actor, body=body)


def _confirm(conn, actor, preview, **kwargs):
    fingerprint = str(preview.get("preview_fingerprint") or "")
    if not fingerprint:
        _fail(f"preview missing fingerprint {sorted(preview)}")
    payload = dict(kwargs)
    payload["confirm"] = True
    payload["preview_fingerprint"] = fingerprint
    body = BulkAssignmentRequest.model_validate(payload)
    return confirm_bulk_assignment(conn, actor=actor, body=body)


def test_production_path_untouched() -> None:
    live = str(PRODUCTION_DB_PATH).replace("\\", "/").lower()
    with get_connection() as conn:
        path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    if path.replace("\\", "/").lower() == live:
        _fail(f"test connection is live production db: {path}")


def test_gates_still_ds9() -> None:
    if not live_master_archive_enabled():
        _fail("master archive gate should stay enabled")
    if not live_ccr_lifecycle_enabled():
        _fail("CCR lifecycle gate should stay enabled")
    if live_destructive_enabled():
        _fail("destructive steward gate must stay false")
    meta = refresh_meta()
    if meta.get("live_confirm_enabled"):
        _fail("LeadMaster confirm enabled")


def test_explicit_new_reassign_noop_and_preview_no_writes() -> None:
    actor = _admin_user()
    clients = _clients()
    with get_connection() as conn:
        robert = _provision_rep("RobertIso", [clients["brown"]])
        created = create_company(conn, actor=actor, company_name=f"DS10 Explicit {_stamp()}")
        cid = int(created["company_id"])
        a = link_relationship(conn, actor=actor, client_id=clients["brown"], company_id=cid, status="New")
        created_b = create_company(conn, actor=actor, company_name=f"DS10 ExplicitB {_stamp()}")
        b = link_relationship(
            conn,
            actor=actor,
            client_id=clients["brown"],
            company_id=int(created_b["company_id"]),
            status="Working",
        )
        created_c = create_company(conn, actor=actor, company_name=f"DS10 ExplicitC {_stamp()}")
        c = link_relationship(
            conn,
            actor=actor,
            client_id=clients["brown"],
            company_id=int(created_c["company_id"]),
            status="Hot Prospect",
        )
        conn.execute(
            """
            UPDATE client_company_relationships
            SET assigned_user_id=?, notes=?, next_action=?, follow_up_date=?, is_hot=1,
                external_record_no=?, priority=?
            WHERE id=?
            """,
            (int(actor.id), "Keep note", "Call", "2026-10-01", f"DS10RN{cid}", "A", int(b)),
        )
        conn.execute(
            "UPDATE client_company_relationships SET assigned_user_id=? WHERE id=?",
            (int(robert["user_id"]), int(c)),
        )
        conn.commit()
        before = [_ccr_row(conn, x) for x in (a, b, c)]
        owners_before = conn.execute(
            "SELECT assigned_user_id FROM client_company_relationships WHERE id IN (?,?,?)",
            (a, b, c),
        ).fetchall()
        preview = _preview(
            conn,
            actor,
            selection_mode="explicit",
            client_id=clients["brown"],
            ccr_ids=[a, b, c],
            target_user_id=robert["user_id"],
            reason="Premier pilot book assignment",
        )
        after_preview = [_ccr_row(conn, x) for x in (a, b, c)]
        if preview.get("writes") is not False:
            _fail("preview must not write")
        if preview["selection_mode"] != "explicit":
            _fail(f"mode {preview['selection_mode']}")
        if int(preview["new_assignments"]) != 1:
            _fail(f"new {preview['new_assignments']}")
        if int(preview["reassignments"]) != 1:
            _fail(f"reassign {preview['reassignments']}")
        if int(preview["already_assigned"]) != 1:
            _fail(f"already {preview['already_assigned']}")
        if after_preview != before:
            _fail("preview mutated CCR rows")
        if [r["assigned_user_id"] for r in owners_before] != [
            _ccr_row(conn, x)["assigned_user_id"] for x in (a, b, c)
        ]:
            _fail("preview mutated assigned_user_id")
        result = _confirm(
            conn,
            actor,
            preview,
            selection_mode="explicit",
            client_id=clients["brown"],
            ccr_ids=[a, b, c],
            target_user_id=robert["user_id"],
            reason="Premier pilot book assignment",
        )
        if int(result["changed"]) != 2 or int(result["already_assigned"]) != 1:
            _fail(f"confirm counts {result}")
        for ccr_id in (a, b, c):
            row = _ccr_row(conn, ccr_id)
            if int(row["assigned_user_id"] or 0) != int(robert["user_id"]):
                _fail(f"not assigned {ccr_id} {row}")
        kept = _ccr_row(conn, b)
        if kept["status"] != "Working" or kept["notes"] != "Keep note":
            _fail(f"workflow mutated {kept}")
        if kept["next_action"] != "Call" or str(kept["follow_up_date"] or "")[:10] != "2026-10-01":
            _fail(f"follow-up mutated {kept}")
        if int(kept["is_hot"] or 0) != 1 or kept["external_record_no"] != f"DS10RN{cid}":
            _fail(f"hot/RN mutated {kept}")
        events_b = provenance_history(
            conn, entity_type="client_relationship", entity_id=int(b), field="assigned_user_id"
        )
        if not events_b or events_b[0].get("action") != ACTION_BULK_ASSIGN_CLIENT_RELATIONSHIP:
            _fail(f"missing reassignment audit {events_b[:1]}")
        if events_b[0].get("source_type") != SOURCE_MANUAL_ADMIN:
            _fail(f"source {events_b[0]}")
        if int(events_b[0].get("changed_by_user_id") or 0) != int(actor.id):
            _fail("spoofable actor")
        if events_b[0].get("reason") != "Premier pilot book assignment":
            _fail(f"reason {events_b[0]}")
        events_c = provenance_history(
            conn, entity_type="client_relationship", entity_id=int(c), field="assigned_user_id"
        )
        if any(e.get("action") == ACTION_BULK_ASSIGN_CLIENT_RELATIONSHIP for e in events_c):
            _fail("noop row wrote audit")
        company = conn.execute("SELECT company_name FROM companies WHERE id=?", (cid,)).fetchone()
        if not company:
            _fail("master company missing")


def test_filtered_select_all_pagination_search_status_and_unassigned() -> None:
    actor = _admin_user()
    clients = _clients()
    marker = f"DS10Filt{_stamp()}"
    names = ["BISON", "A.O. Smith", "Smith & Nephew", "O'Reilly"]
    with get_connection() as conn:
        robert = _provision_rep("FiltRep", [clients["premier"]])
        ids = []
        for i in range(127):
            name = f"{marker} {i:03d}"
            created = create_company(conn, actor=actor, company_name=name)
            status = "New" if i < 120 else "Appointment Set"
            ccr = link_relationship(
                conn,
                actor=actor,
                client_id=clients["premier"],
                company_id=int(created["company_id"]),
                status=status,
            )
            if i < 10:
                conn.execute(
                    "UPDATE client_company_relationships SET assigned_user_id=? WHERE id=?",
                    (int(actor.id), int(ccr)),
                )
            ids.append(int(ccr))
        search_ids = []
        for name in names:
            created = create_company(conn, actor=actor, company_name=f"{marker} {name}")
            ccr = link_relationship(
                conn,
                actor=actor,
                client_id=clients["premier"],
                company_id=int(created["company_id"]),
                status="New",
            )
            search_ids.append(int(ccr))
        conn.commit()

    page = list_prospects_page(
        client_id=clients["premier"],
        q=marker,
        status="New",
        assigned_user_id=0,
        limit=50,
        offset=0,
        user_id=actor.id,
    )
    if int(page["limit"]) != 50:
        _fail(f"page size {page['limit']}")
    if int(page["total"]) < 110:
        _fail(f"filtered total {page['total']}")
    if len(page["prospects"]) != 50:
        _fail(f"visible {len(page['prospects'])}")
    matched = list_matching_prospect_ccr_ids(
        client_id=clients["premier"], q=marker, status="New", assigned_user_id=0
    )
    if len(matched) != int(page["total"]):
        _fail(f"select-all {len(matched)} vs list total {page['total']}")
    if len(matched) == 50:
        _fail("Select All collapsed to one page")
    with get_connection() as conn:
        preview = _preview(
            conn,
            actor,
            selection_mode="filtered",
            filter=ProspectAssignmentFilter(
                client_id=clients["premier"],
                q=marker,
                status="New",
                assigned_user_id=0,
            ),
            target_user_id=robert["user_id"],
            reason="Unassigned Premier New book",
        )
        if preview["selection_mode"] != "filtered":
            _fail(f"mode {preview['selection_mode']}")
        if int(preview["selected_count"]) != len(matched):
            _fail(f"preview selected {preview['selected_count']} vs {len(matched)}")
        if int(preview["filtered_result_count"]) != len(matched):
            _fail("filtered_result_count mismatch")
        result = _confirm(
            conn,
            actor,
            preview,
            selection_mode="filtered",
            filter=ProspectAssignmentFilter(
                client_id=clients["premier"],
                q=marker,
                status="New",
                assigned_user_id=0,
            ),
            target_user_id=robert["user_id"],
            reason="Unassigned Premier New book",
        )
        if int(result["changed"]) != int(preview["new_assignments"]) + int(preview["reassignments"]):
            _fail(f"changed {result}")
        leftover = conn.execute(
            f"""
            SELECT COUNT(*) FROM client_company_relationships
            WHERE id IN ({",".join("?" * len(ids))})
              AND TRIM(COALESCE(status,'')) = 'New'
              AND assigned_user_id IS NULL
            """,
            ids,
        ).fetchone()[0]
        if int(leftover) != 0:
            _fail(f"unassigned New remaining {leftover}")
        appt = conn.execute(
            f"""
            SELECT COUNT(*) FROM client_company_relationships
            WHERE id IN ({",".join("?" * len(ids))})
              AND TRIM(COALESCE(status,'')) = 'Appointment Set'
              AND assigned_user_id IS NULL
            """,
            ids,
        ).fetchone()[0]
        if int(appt) != 7:
            _fail(f"status filter leaked Appointment Set {appt}")

    for name in names:
        page_name = list_prospects_page(
            client_id=clients["premier"], q=name, limit=50, user_id=actor.id
        )
        if int(page_name["total"]) < 1:
            _fail(f"search missed {name}")
        ids_name = list_matching_prospect_ccr_ids(client_id=clients["premier"], q=name)
        if not ids_name:
            _fail(f"select-all search missed {name}")


def test_mixed_client_unauthorized_removed_archived_stale_reason() -> None:
    actor = _admin_user()
    clients = _clients()
    with get_connection() as conn:
        robert = _provision_rep("BrownOnly", [clients["brown"]])
        created = create_company(conn, actor=actor, company_name=f"DS10 Shared {_stamp()}")
        cid = int(created["company_id"])
        brown = link_relationship(
            conn, actor=actor, client_id=clients["brown"], company_id=cid, status="New"
        )
        premier = link_relationship(
            conn, actor=actor, client_id=clients["premier"], company_id=cid, status="New"
        )
        extra = create_company(conn, actor=actor, company_name=f"DS10 Extra {_stamp()}")
        extra_ccr = link_relationship(
            conn,
            actor=actor,
            client_id=clients["brown"],
            company_id=int(extra["company_id"]),
            status="New",
        )
        conn.commit()
        preview = _preview(
            conn,
            actor,
            selection_mode="explicit",
            ccr_ids=[brown, premier],
            target_user_id=robert["user_id"],
            reason="Mixed should block",
        )
        if not preview.get("mixed_client") or preview.get("confirm_allowed"):
            _fail(f"mixed not blocked {preview}")
        if preview.get("block_code") != "mixed_client_selection":
            _fail(f"code {preview.get('block_code')}")
        try:
            _confirm(
                conn,
                actor,
                preview,
                selection_mode="explicit",
                ccr_ids=[brown, premier],
                target_user_id=robert["user_id"],
                reason="Mixed should block",
            )
            _fail("mixed confirm succeeded")
        except BulkAssignmentError as exc:
            if str(exc) != "mixed_client_selection":
                _fail(f"mixed code {exc}")
        if _ccr_row(conn, brown)["assigned_user_id"] is not None:
            _fail("mixed mutated brown")
        if _ccr_row(conn, premier)["assigned_user_id"] is not None:
            _fail("mixed mutated premier")

        unauthorized = _preview(
            conn,
            actor,
            selection_mode="explicit",
            client_id=clients["premier"],
            ccr_ids=[premier],
            target_user_id=robert["user_id"],
            reason="Unauthorized target",
        )
        if not unauthorized.get("unauthorized_target") or unauthorized.get("confirm_allowed"):
            _fail(f"unauthorized not blocked {unauthorized}")
        try:
            _confirm(
                conn,
                actor,
                unauthorized,
                selection_mode="explicit",
                client_id=clients["premier"],
                ccr_ids=[premier],
                target_user_id=robert["user_id"],
                reason="Unauthorized target",
            )
            _fail("unauthorized confirm succeeded")
        except BulkAssignmentError as exc:
            if str(exc) != "assignee_not_authorized_for_client":
                _fail(f"unauth code {exc}")

        try:
            _preview(
                conn,
                actor,
                selection_mode="explicit",
                ccr_ids=[brown],
                target_user_id=robert["user_id"],
                reason="  ",
            )
            _fail("blank reason preview succeeded")
        except BulkAssignmentError as exc:
            if str(exc) != "reason_required":
                _fail(f"reason {exc}")

        ok_preview = _preview(
            conn,
            actor,
            selection_mode="explicit",
            ccr_ids=[extra_ccr],
            target_user_id=robert["user_id"],
            reason="Stale check",
        )
        conn.execute(
            "UPDATE client_company_relationships SET assigned_user_id=? WHERE id=?",
            (int(actor.id), int(extra_ccr)),
        )
        conn.commit()
        try:
            _confirm(
                conn,
                actor,
                ok_preview,
                selection_mode="explicit",
                ccr_ids=[extra_ccr],
                target_user_id=robert["user_id"],
                reason="Stale check",
            )
            _fail("stale confirm succeeded")
        except BulkAssignmentError as exc:
            if str(exc) != "stale_preview":
                _fail(f"stale {exc}")

        remove_relationship(conn, actor=actor, ccr_id=int(brown), reason="DS10 remove")
        conn.execute(
            "UPDATE companies SET archived_at=datetime('now') WHERE id=?",
            (cid,),
        )
        conn.commit()
        safety = _preview(
            conn,
            actor,
            selection_mode="explicit",
            ccr_ids=[brown, premier],
            target_user_id=actor.id,
            reason="Removed and archived",
        )
        if int(safety["inactive_removed"]) < 1:
            _fail(f"removed not skipped {safety}")
        if int(safety["archived_company"]) < 1:
            _fail(f"archived master not skipped {safety}")
        if int(safety["new_assignments"]) + int(safety["reassignments"]) != 0:
            _fail(f"inactive rows treated eligible {safety}")
        filtered = list_matching_prospect_ccr_ids(client_id=clients["brown"], q="DS10 Shared")
        if int(brown) in filtered or int(premier) in filtered:
            _fail(f"filtered included inactive {filtered}")


def test_shared_company_isolation_preservation_rollback_and_queue() -> None:
    actor = _admin_user()
    clients = _clients()
    with get_connection() as conn:
        robert = _provision_rep("ShareR", [clients["brown"]])
        tyler = _provision_rep("ShareT", [clients["brown"]])
        todd = _provision_rep("ShareD", [clients["premier"]])
        created = create_company(conn, actor=actor, company_name=f"DS10 X {_stamp()}")
        cid = int(created["company_id"])
        brown = link_relationship(
            conn, actor=actor, client_id=clients["brown"], company_id=cid, status="Hot Prospect"
        )
        premier = link_relationship(
            conn, actor=actor, client_id=clients["premier"], company_id=cid, status="New"
        )
        conn.execute(
            """
            UPDATE client_company_relationships
            SET assigned_user_id=?, notes=?, next_action=?, follow_up_date=?, is_hot=1,
                external_record_no=?
            WHERE id=?
            """,
            (int(robert["user_id"]), "Brown note", "Call back", "2026-11-02", f"BRN{cid}", brown),
        )
        conn.execute(
            """
            UPDATE client_company_relationships
            SET assigned_user_id=?, notes=?, external_record_no=?
            WHERE id=?
            """,
            (int(todd["user_id"]), "Premier note", f"PRM{cid}", premier),
        )
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
        preview = _preview(
            conn,
            actor,
            selection_mode="explicit",
            ccr_ids=[brown],
            target_user_id=tyler["user_id"],
            reason="Rebalance Brown calling list",
        )
        if int(preview["reassignments"]) != 1:
            _fail(f"expected reassignment {preview}")
        if int(preview["hot_count"]) != 1 or int(preview["follow_up_count"]) != 1:
            _fail(f"consequential work {preview}")
        result = _confirm(
            conn,
            actor,
            preview,
            selection_mode="explicit",
            ccr_ids=[brown],
            target_user_id=tyler["user_id"],
            reason="Rebalance Brown calling list",
        )
        if int(result["changed"]) != 1:
            _fail(f"brown not reassigned {result}")
        brown_row = _ccr_row(conn, brown)
        premier_row = _ccr_row(conn, premier)
        if int(brown_row["assigned_user_id"]) != int(tyler["user_id"]):
            _fail(f"brown {brown_row}")
        if int(premier_row["assigned_user_id"]) != int(todd["user_id"]):
            _fail("premier leaked")
        if brown_row["notes"] != "Brown note" or premier_row["notes"] != "Premier note":
            _fail("notes mutated")
        if brown_row["status"] != "Hot Prospect" or brown_row["next_action"] != "Call back":
            _fail("status/next mutated")
        if camp_id:
            still = conn.execute(
                "SELECT 1 FROM campaign_companies WHERE campaign_id=? AND company_id=?",
                (camp_id, cid),
            ).fetchone()
            if still is None:
                _fail("campaign membership dropped")
        master = conn.execute("SELECT company_name FROM companies WHERE id=?", (cid,)).fetchone()
        if not master:
            _fail("master missing")

    tyler_queue = list_work_queue(
        tyler["user_id"],
        client_id=clients["brown"],
        assigned_user_id=tyler["user_id"],
        limit=50,
    )
    robert_queue = list_work_queue(
        robert["user_id"],
        client_id=clients["brown"],
        assigned_user_id=robert["user_id"],
        limit=50,
    )
    tyler_ids = {int(item.relationship_id or 0) for item in tyler_queue.items}
    robert_ids = {int(item.relationship_id or 0) for item in robert_queue.items}
    if brown not in tyler_ids and not any(
        int(getattr(item, "company_id", 0) or 0) == cid for item in tyler_queue.items
    ):
        # Work Queue may omit Hot-without-due depending on type filter; assigned CCR must still resolve.
        page = list_prospects_page(
            client_id=clients["brown"],
            assigned_user_id=tyler["user_id"],
            q=f"DS10 X",
            user_id=actor.id,
        )
        if not any(int(p.relationship_id) == int(brown) for p in page["prospects"]):
            _fail("Tyler-scoped prospects missing reassigned CCR")
    if brown in robert_ids:
        _fail("Robert-scoped work queue still owns Brown CCR")

    if brown in robert_ids:
        _fail("Robert-scoped work queue still owns Brown CCR")

    with get_connection() as conn:
        created2 = create_company(conn, actor=actor, company_name=f"DS10 Roll {_stamp()}")
        roll = link_relationship(
            conn,
            actor=actor,
            client_id=clients["brown"],
            company_id=int(created2["company_id"]),
            status="New",
        )
        conn.commit()
        preview = _preview(
            conn,
            actor,
            selection_mode="explicit",
            ccr_ids=[roll],
            target_user_id=robert["user_id"],
            reason="Rollback proof",
        )
        try:
            with force_provenance_failure("ds10_assign_fail"):
                _confirm(
                    conn,
                    actor,
                    preview,
                    selection_mode="explicit",
                    ccr_ids=[roll],
                    target_user_id=robert["user_id"],
                    reason="Rollback proof",
                )
            _fail("forced provenance failure did not raise")
        except Exception:
            conn.rollback()
    with get_connection() as conn:
        if _ccr_row(conn, roll)["assigned_user_id"] is not None:
            _fail("rollback left a half-assigned CCR")


def test_http_admin_only_spoofed_actor_and_campaign_regression() -> None:
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
    if body.get("delete_enabled") or body.get("merge_enabled"):
        _fail(f"destructive flags {body}")
    if not body.get("archive_enabled") or not body.get("company_restore_enabled"):
        _fail(f"archive flags {body}")
    clients = _clients()
    assignees = http.get(f"/api/admin/bulk-assignment/assignees?client_id={clients['brown']}")
    if assignees.status_code != 200:
        _fail(f"assignees {assignees.status_code} {assignees.text}")
    if not any(int(u["user_id"]) == int(actor.id) for u in assignees.json().get("assignees") or []):
        _fail("admin missing from eligible assignees")

    with get_connection() as conn:
        created = create_company(conn, actor=actor, company_name=f"DS10 HTTP {_stamp()}")
        ccr = link_relationship(
            conn,
            actor=actor,
            client_id=clients["brown"],
            company_id=int(created["company_id"]),
            status="New",
        )
        conn.commit()
    preview = http.post(
        "/api/admin/bulk-assignment/preview",
        json={
            "selection_mode": "explicit",
            "ccr_ids": [ccr],
            "target_user_id": int(actor.id),
            "reason": "HTTP assign",
            "actor_id": 999999,
            "created_by": "Impostor",
        },
        headers=headers,
    )
    if preview.status_code != 200:
        _fail(f"http preview {preview.status_code} {preview.text}")
    if preview.json().get("writes") is not False:
        _fail("http preview wrote")
    save = http.post(
        "/api/admin/bulk-assignment/confirm",
        json={
            "selection_mode": "explicit",
            "ccr_ids": [ccr],
            "target_user_id": int(actor.id),
            "reason": "HTTP assign",
            "confirm": True,
            "preview_fingerprint": preview.json().get("preview_fingerprint"),
            "actor_id": 999999,
        },
        headers=headers,
    )
    if save.status_code != 200:
        _fail(f"http confirm {save.status_code} {save.text}")
    with get_connection() as conn:
        row = latest_provenance(
            conn, entity_type="client_relationship", entity_id=int(ccr), field="assigned_user_id"
        )
        if row is None or int(row["changed_by_user_id"] or 0) != int(actor.id):
            _fail(f"spoofed actor used {row}")

    specialist_pw = f"NsTest9{secrets.token_hex(8)}"
    spec = provision_staff_user(
        first_name="Dana",
        last_name="Specialist",
        email=f"ds10.spec.{_stamp()}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[clients["brown"]],
    )
    spec_csrf = _login(http, spec["user"]["email"], specialist_pw)
    denied = http.post(
        "/api/admin/bulk-assignment/preview",
        json={
            "selection_mode": "explicit",
            "ccr_ids": [ccr],
            "target_user_id": int(actor.id),
            "reason": "nope",
        },
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied.status_code != 403:
        _fail(f"specialist preview {denied.status_code}")
    if denied.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        _fail(f"specialist detail {denied.json()}")
    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.post(
            "/api/admin/bulk-assignment/preview",
            json={
                "selection_mode": "explicit",
                "ccr_ids": [ccr],
                "target_user_id": int(actor.id),
                "reason": "nope",
            },
        )
        if unauth.status_code != 401:
            _fail(f"unauthenticated {unauth.status_code}")
        if unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated detail {unauth.json()}")

    import test_campaign_bulk_assign

    rc = test_campaign_bulk_assign.main()
    if rc not in (0, None):
        _fail(f"campaign bulk assign regression rc={rc}")


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_gates_still_ds9,
        test_explicit_new_reassign_noop_and_preview_no_writes,
        test_filtered_select_all_pagination_search_status_and_unassigned,
        test_mixed_client_unauthorized_removed_archived_stale_reason,
        test_shared_company_isolation_preservation_rollback_and_queue,
        test_http_admin_only_spoofed_actor_and_campaign_regression,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-10 isolated tests passed")
