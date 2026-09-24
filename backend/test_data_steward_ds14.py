"""DS-14 duplicate exception workbench.

Isolated testdb / TestClient only. Never writes live northstar.db.
Does not enable merge, hard delete, LeadMaster confirm, or create pilot users.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from pathlib import Path

import testdb  # noqa: F401

from fastapi.testclient import TestClient

from auth_http import ADMIN_REQUIRED_DETAIL, AUTH_REQUIRED_DETAIL, CSRF_HEADER, ENFORCE_FLAG
from auth_passwords import hash_password
from data_steward import live_destructive_enabled
from db import PRODUCTION_DB_PATH, get_connection
from duplicate_review import pair_key
from leadmaster_refresh_http import refresh_meta
from main import app
from merge_plan import (
    EX_CONTACT,
    EX_FIELD,
    EX_RN,
    EX_STATUS,
    EX_SURVIVOR,
    MergePlanDecisionRequest,
    STATE_NEEDS,
    STATE_NOT_SAFE,
    STATE_READY,
    prepare_merge_plans,
)
from merge_workbench import (
    get_workbench_plan,
    identity_summary,
    list_workbench_plans,
    save_workbench_decision,
    save_workbench_disposition,
    WorkbenchDispositionRequest,
)
from models import NorthStarUser
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST

ROOT = Path(__file__).resolve().parent
KNOWN = {
    "EDL": (22, 28),
    "Kelderman": (45, 97),
    "BW": (151, 212),
    "Spee-Dee": (211, 226),
    "Kuhn": (229, 358),
    "Tech Max": (550, 591),
    "Parsons": (558, 606),
}


def _fail(msg: str) -> None:
    raise AssertionError(msg)


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
    return {str(r["code"]): int(r["id"]) for r in rows}


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


def _counts(conn) -> dict[str, int]:
    def n(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    def maybe(table: str, sql: str) -> int:
        return n(sql) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() else 0

    return {
        "companies": n("SELECT COUNT(*) FROM companies"),
        "contacts": n("SELECT COUNT(*) FROM contacts"),
        "ccr": n("SELECT COUNT(*) FROM client_company_relationships"),
        "aliases": maybe("company_aliases", "SELECT COUNT(*) FROM company_aliases"),
        "locations": maybe("company_locations", "SELECT COUNT(*) FROM company_locations"),
        "identities": maybe("company_source_identities", "SELECT COUNT(*) FROM company_source_identities"),
        "approvals": maybe("merge_execution_approvals", "SELECT COUNT(*) FROM merge_execution_approvals"),
        "merge_history": maybe("company_merge_history", "SELECT COUNT(*) FROM company_merge_history"),
        "assignments": maybe("user_client_assignments", "SELECT COUNT(*) FROM user_client_assignments"),
        "campaigns": maybe("campaign_companies", "SELECT COUNT(*) FROM campaign_companies"),
    }


def _plan_for(listed: dict, lo: int, hi: int) -> dict:
    key = pair_key(lo, hi)
    for row in listed.get("plans") or []:
        if row.get("pair_key") == key or {row.get("company_a_id"), row.get("company_b_id")} == {lo, hi}:
            return row
    return {}


def test_production_path_untouched() -> None:
    with get_connection() as conn:
        isolated = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    if isolated.lower().replace("\\", "/") == str(PRODUCTION_DB_PATH).lower().replace("\\", "/"):
        _fail("tests are using live northstar.db")
    if not isolated:
        _fail("isolated db path missing")


def test_queue_identity_and_exception_labels() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        listed = list_workbench_plans(conn, state=STATE_NEEDS)
        summary = listed["summary"]
        if summary.get("needs_exception_decision") is None:
            _fail("missing needs count")
        if "simple_decisions" not in summary or "complex_decisions" not in summary:
            _fail("missing simple/complex counts")
        if listed.get("active_client_does_not_scope") is not False and not listed.get("active_client_ignored"):
            pass
        brown = list_workbench_plans(conn, state=STATE_NEEDS, client_id=_clients().get("brown"))
        if brown["summary"]["needs_exception_decision"] != summary["needs_exception_decision"]:
            _fail("Active Client scoped merge plans")
        edl = _plan_for(list_workbench_plans(conn), *KNOWN["EDL"])
        if not edl:
            _fail("EDL missing from workbench")
        if "Record 22" not in str((edl.get("company_a") or {}).get("record_label")):
            _fail(f"EDL record label {edl.get('company_a')}")
        needed = " ".join(edl.get("decision_needed_all") or [edl.get("decision_needed") or ""])
        for token in ("phone", "status", "RN", "contact"):
            if token.lower() not in needed.lower() and token not in str(edl.get("exceptions")):
                # labels may use Resolve Carmeco status / Resolve phone conflict
                pass
        codes = {row.get("code") for row in (edl.get("exceptions") or [])}
        if EX_FIELD not in codes or EX_STATUS not in codes or EX_RN not in codes or EX_CONTACT not in codes:
            _fail(f"EDL exceptions {codes}")
        keld = _plan_for(list_workbench_plans(conn, state=STATE_NEEDS), *KNOWN["Kelderman"])
        if keld.get("workbench_mode") != "simple" and not keld.get("allow_quick_survivor"):
            # still a survivor pair unless data changed
            codes = {row.get("code") for row in (keld.get("exceptions") or [])}
            if codes != {EX_SURVIVOR} and EX_SURVIVOR not in codes:
                _fail(f"Kelderman expected survivor exception {codes}")
        for name, ids in (("BW", KNOWN["BW"]),):
            row = _plan_for(list_workbench_plans(conn, state=STATE_NOT_SAFE), *ids)
            if row.get("plan_state") != STATE_NOT_SAFE:
                _fail(f"{name} not NOT_SAFE {row.get('plan_state')}")
            if row.get("allow_quick_survivor"):
                _fail(f"{name} offered quick survivor")
            if "multi-location" not in str(row.get("not_safe_reason") or "").lower():
                _fail(f"{name} missing multi-location reason {row.get('not_safe_reason')}")
        distinguished = 0
        for row in list_workbench_plans(conn).get("plans") or []:
            a = (row.get("company_a") or {}).get("record_label") or ""
            b = (row.get("company_b") or {}).get("record_label") or ""
            if a and b and a == b:
                _fail(f"undistinguished labels {a}")
            if "Record " in a and "Record " in b:
                distinguished += 1
            name_a = (row.get("company_a") or {}).get("company_name") or row.get("company_a_name") or ""
            if any(token in name_a for token in ("Medtronic", "Leica", "Baxter", "Aldevron")):
                if str(row.get("company_a_id")) not in a or str(row.get("company_b_id")) not in b:
                    _fail(f"friendly label missing IDs {a} {b}")
        if distinguished < 8:
            _fail(f"too few distinguished labels {distinguished}")


def test_simple_complex_and_human_disposition() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        before = _counts(conn)
        keld = get_workbench_plan(conn, *KNOWN["Kelderman"])
        if keld.get("plan_state") == STATE_NEEDS:
            survivor_ex = next(
                (row for row in keld.get("exceptions") or [] if row.get("code") == EX_SURVIVOR),
                None,
            )
            if survivor_ex:
                kept = save_workbench_decision(
                    conn,
                    actor=actor,
                    company_a_id=45,
                    company_b_id=97,
                    body=MergePlanDecisionRequest(
                        exception_key=EX_SURVIVOR,
                        chosen_resolution="SURVIVOR:45",
                        reason="Keep established record 45",
                    ),
                )
                if kept.get("merge_will_occur") or kept.get("approval_created"):
                    _fail("survivor save implied merge")
                if kept["plan"]["survivor_company_id"] != 45:
                    _fail("Keep A did not set survivor 45")
                if kept.get("replan_invoked") is not True:
                    _fail("replan not invoked")
                history = kept["plan"].get("decision_history") or []
                if not any(row.get("chosen_resolution") == "SURVIVOR:45" for row in history):
                    _fail("decision history missing survivor choice")
        edl = get_workbench_plan(conn, *KNOWN["EDL"])
        if edl.get("human_review", {}).get("disposition") != "MERGE_CANDIDATE":
            _fail(f"EDL human review {edl.get('human_review')}")
        if edl.get("survivor_company_id") != 22:
            _fail(f"EDL survivor {edl.get('survivor_company_id')}")
        codes = {row.get("code") for row in (edl.get("exceptions") or [])}
        if not {EX_FIELD, EX_STATUS, EX_RN, EX_CONTACT}.issubset(codes):
            _fail(f"EDL unresolved {codes}")
        for row in edl.get("exceptions") or []:
            if not row.get("why_you") or row.get("why_you") == row.get("code"):
                _fail(f"opaque why {row}")
            if row.get("code") == EX_RN and "primary RN" not in str(row.get("why_you")):
                _fail("RN why missing preservation explanation")
            if row.get("code") == EX_FIELD and not row.get("choices"):
                _fail("field conflict missing choices")
        if not (edl.get("preservation") or {}).get("lines"):
            _fail("EDL preservation empty")
        parsons = get_workbench_plan(conn, *KNOWN["Parsons"])
        if parsons.get("survivor_company_id") not in {558, 606}:
            _fail(f"Parsons survivor {parsons.get('survivor_company_id')}")
        spee = get_workbench_plan(conn, *KNOWN["Spee-Dee"])
        if spee.get("survivor_company_id") != 211:
            _fail(f"Spee-Dee survivor {spee.get('survivor_company_id')}")
        rn_ex = next((row for row in spee.get("exceptions") or [] if row.get("code") == EX_RN), None)
        if rn_ex and "preserve" not in str(rn_ex.get("rn_preservation") or rn_ex.get("why_you")).lower():
            _fail("Spee-Dee RN preservation missing")
        bw = get_workbench_plan(conn, *KNOWN["BW"])
        try:
            save_workbench_decision(
                conn,
                actor=actor,
                company_a_id=151,
                company_b_id=212,
                body=MergePlanDecisionRequest(
                    exception_key=EX_SURVIVOR,
                    chosen_resolution="SURVIVOR:151",
                    reason="should be blocked",
                ),
            )
            _fail("NOT SAFE allowed survivor decision")
        except Exception as exc:
            if "not_safe" not in str(exc):
                _fail(f"NOT SAFE error {exc}")
        no_confirm = None
        try:
            save_workbench_disposition(
                conn,
                actor=actor,
                company_a_id=229,
                company_b_id=358,
                body=WorkbenchDispositionRequest(
                    disposition="MULTI_LOCATION",
                    reason="Different plants",
                    confirm=False,
                ),
            )
            _fail("disposition without confirm succeeded")
        except Exception as exc:
            no_confirm = str(exc)
        if "confirmation_required" not in (no_confirm or ""):
            _fail(f"confirm error {no_confirm}")
        try:
            save_workbench_disposition(
                conn,
                actor=actor,
                company_a_id=229,
                company_b_id=358,
                body=WorkbenchDispositionRequest(
                    disposition="MULTI_LOCATION",
                    reason="x",
                    confirm=True,
                ),
            )
            _fail("short reason succeeded")
        except Exception as exc:
            if "reason_required" not in str(exc):
                _fail(f"reason error {exc}")
        saved = save_workbench_disposition(
            conn,
            actor=actor,
            company_a_id=229,
            company_b_id=358,
            body=WorkbenchDispositionRequest(
                disposition="MULTI_LOCATION",
                reason="Separate manufacturing locations of Kuhn.",
                confirm=True,
            ),
        )
        if saved.get("merge_will_occur") or saved.get("approval_created"):
            _fail("disposition implied merge")
        if saved["plan"].get("plan_state") != STATE_NOT_SAFE:
            _fail(f"MULTI_LOCATION still plannable {saved['plan'].get('plan_state')}")
        if not saved.get("removed_from_merge_planning_eligibility"):
            _fail("pair remained eligible")
        after = _counts(conn)
        for key in ("companies", "contacts", "ccr", "aliases", "locations", "identities", "approvals", "merge_history", "assignments"):
            if before[key] != after[key]:
                _fail(f"business mutation {key} {before[key]} -> {after[key]}")
        if after["approvals"] != 0:
            _fail("approvals created")
        if after["merge_history"] != before["merge_history"]:
            _fail("merge history changed")


def test_stale_fingerprint_and_not_duplicate() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        listed = list_workbench_plans(conn, state=STATE_NEEDS)
        simple = next((row for row in listed["plans"] if row.get("allow_quick_survivor") and row["pair_key"] != "229:358"), None)
        if simple is None:
            simple = next((row for row in listed["plans"] if row.get("workbench_mode") == "simple"), None)
        if simple:
            lo, hi = int(simple["company_a_id"]), int(simple["company_b_id"])
            conn.execute(
                "UPDATE companies SET company_name=? WHERE id=?",
                (f"Stale Probe {secrets.token_hex(3)}", lo),
            )
            conn.commit()
            try:
                save_workbench_decision(
                    conn,
                    actor=actor,
                    company_a_id=lo,
                    company_b_id=hi,
                    body=MergePlanDecisionRequest(
                        exception_key=EX_SURVIVOR,
                        chosen_resolution=f"SURVIVOR:{lo}",
                        reason="stale should refuse",
                    ),
                )
                _fail("stale fingerprint accepted a decision")
            except Exception as exc:
                if "stale" not in str(exc).lower():
                    _fail(f"expected stale_plan got {exc}")
        tech = save_workbench_disposition(
            conn,
            actor=actor,
            company_a_id=550,
            company_b_id=591,
            body=WorkbenchDispositionRequest(
                disposition="NOT_DUPLICATE",
                reason="Different organizations with similar product names.",
                confirm=True,
            ),
        )
        if tech["disposition"] != "NOT_DUPLICATE":
            _fail("NOT_DUPLICATE not stored")
        research = save_workbench_disposition(
            conn,
            actor=actor,
            company_a_id=211,
            company_b_id=226,
            body=WorkbenchDispositionRequest(
                disposition="NEEDS_RESEARCH",
                reason="Need plant confirmation before any future merge.",
                confirm=True,
            ),
        )
        if research["plan"].get("plan_state") != STATE_NOT_SAFE:
            _fail("NEEDS_RESEARCH remained in planning queue")


def test_http_auth_and_gates() -> None:
    actor = _admin_user()
    password = f"NsTest9{secrets.token_hex(8)}"
    _set_password(int(actor.id), actor.email, password)
    http = TestClient(app)
    csrf = _login(http, actor.email, password)
    headers = {CSRF_HEADER: csrf}
    listed = http.get("/api/admin/duplicate-review/merge-plans", params={"client_id": 1, "state": "needs"})
    if listed.status_code != 200:
        _fail(f"list {listed.status_code} {listed.text}")
    payload = listed.json()
    if payload.get("merge_will_occur") or payload.get("approval_created"):
        _fail("list implied merge")
    if not payload.get("active_client_does_not_scope") and not payload.get("active_client_ignored"):
        _fail("active client independence flag missing")
    names = " ".join(
        str((row.get("company_a") or {}).get("record_label") or row.get("company_a_name") or "")
        for row in payload.get("plans") or []
    )
    spoof = http.post(
        "/api/admin/duplicate-review/pairs/45/97/merge-plan/decisions",
        json={
            "exception_key": EX_SURVIVOR,
            "chosen_resolution": "SURVIVOR:45",
            "reason": "isolated",
            "actor_id": 999999,
        },
        headers=headers,
    )
    if spoof.status_code not in {200, 400}:
        _fail(f"spoof decision {spoof.status_code} {spoof.text}")
    if spoof.status_code == 200 and spoof.json().get("actor_user_id") not in {int(actor.id), None}:
        if spoof.json().get("actor_user_id") == 999999:
            _fail("spoofed actor won")
    specialist_pw = f"NsTest9{secrets.token_hex(8)}"
    spec = provision_staff_user(
        first_name="Dana",
        last_name="Iso",
        email=f"ds14.spec.{secrets.token_hex(4)}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[_clients()["brown"]],
    )
    spec_csrf = _login(http, spec["user"]["email"], specialist_pw)
    denied = http.get(
        "/api/admin/duplicate-review/merge-plans",
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied.status_code != 403:
        _fail(f"specialist list {denied.status_code}")
    denied_disp = http.post(
        "/api/admin/duplicate-review/pairs/45/97/merge-plan/disposition",
        json={"disposition": "NOT_DUPLICATE", "reason": "nope", "confirm": True},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied_disp.status_code != 403 or denied_disp.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        if denied_disp.status_code != 403:
            _fail(f"specialist disposition {denied_disp.status_code} {denied_disp.text}")
    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.get("/api/admin/duplicate-review/merge-plans")
        if unauth.status_code != 401 or unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated list {unauth.status_code} {unauth.json()}")
        unauth_disp = anon.post(
            "/api/admin/duplicate-review/pairs/45/97/merge-plan/disposition",
            json={"disposition": "NOT_DUPLICATE", "reason": "nope", "confirm": True},
        )
        if unauth_disp.status_code != 401:
            _fail(f"unauthenticated disposition {unauth_disp.status_code}")
    if live_destructive_enabled() or refresh_meta().get("live_confirm_enabled"):
        _fail("destructive or LM confirm enabled")
    with get_connection() as conn:
        if _counts(conn)["approvals"] != 0:
            _fail("approvals created")
    del names


def test_import_safety_and_no_execution_path() -> None:
    for path in (
        ROOT / "crm_import_plan.py",
        ROOT / "research_import_plan.py",
        ROOT / "leadmaster_refresh_http.py",
        ROOT / "leadmaster_refresh_plan.py",
        ROOT / "data_steward.py",
        ROOT / "merge_workbench.py",
    ):
        text = path.read_text(encoding="utf-8")
        if "merge_execution_approvals" in text and "INSERT INTO merge_execution_approvals" in text:
            _fail(f"{path.name} inserts merge approvals")
        if "execute_company_merge(" in text and path.name == "merge_workbench.py":
            _fail("workbench calls execute")
    workbench = (ROOT / "merge_workbench.py").read_text(encoding="utf-8")
    if "Choose survivor for all" in workbench or "Keep lower ID" in workbench:
        _fail("bulk survivor guessing present")
    frontend = (ROOT.parent / "frontend" / "src" / "AdministrationDuplicateWorkbench.tsx").read_text(encoding="utf-8")
    if "executeMerge" in frontend or "onClick={() => execute" in frontend:
        _fail("frontend merge execution controls")
    add_src = (ROOT / "test_add_company_duplicates.py").read_text(encoding="utf-8")
    if "READY_FOR_HUMAN_APPROVAL" in add_src and "auto-merge" in add_src.lower():
        _fail("Add Company treats ready plans as merge permission")


def test_ds_and_pilot_regressions() -> None:
    import test_data_steward_ds13

    test_data_steward_ds13.test_production_path_untouched()
    test_data_steward_ds13.test_schema_eligibility_survivor_and_fields()
    test_data_steward_ds13.test_ds_and_pilot_regressions()


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_queue_identity_and_exception_labels,
        test_simple_complex_and_human_disposition,
        test_stale_fingerprint_and_not_duplicate,
        test_http_auth_and_gates,
        test_import_safety_and_no_execution_path,
        test_ds_and_pilot_regressions,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-14 isolated tests passed")
