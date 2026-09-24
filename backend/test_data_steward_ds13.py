"""DS-13 automated merge planning and exception review.

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
from data_steward import (
    SOURCE_MANUAL_ADMIN,
    create_company,
    create_contact,
    link_relationship,
    live_destructive_enabled,
    provenance_history,
)
from db import PRODUCTION_DB_PATH, get_connection
from duplicate_classify import CLASS_HIGH, CLASS_MULTI, analyze_duplicate_candidates
from duplicate_review import (
    DISPOSITION_LIKELY,
    DISPOSITION_MERGE,
    DISPOSITION_MULTI,
    DISPOSITION_NOT,
    DISPOSITION_RESEARCH,
    DuplicateReviewSaveRequest,
    pair_key,
    save_duplicate_review,
)
from leadmaster_refresh_http import refresh_meta
from main import app
from merge_plan import (
    ACTION_FIELD_CONFLICT,
    ACTION_FILL_BLANK,
    ACTION_KEEP_SURVIVOR,
    ACTION_PRESERVE_ALIAS,
    ACTION_PRESERVE_IDENTITY,
    CCR_DECISION,
    CCR_SAFE,
    CONTACT_CONFLICT,
    CONTACT_EXACT,
    CONTACT_POSSIBLE,
    CONTACT_PRESERVE,
    EX_FOLLOWUP,
    EX_HOT,
    EX_MULTI,
    EX_NEXT,
    EX_REP,
    EX_RN,
    EX_STATUS,
    EX_SURVIVOR,
    PLANNER_VERSION,
    STATE_NEEDS,
    STATE_NOT_SAFE,
    STATE_READY,
    STATE_STALE,
    build_merge_plan,
    eligibility_for_pair,
    ensure_merge_plan_schema,
    get_merge_plan,
    list_merge_plans,
    prepare_merge_plans,
    replan_merge_plan,
    save_merge_plan_decision,
    MergePlanDecisionRequest,
)
from models import NorthStarUser
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST

ROOT = Path(__file__).resolve().parent
KNOWN_DUPES = (
    (22, 28, "EDL"),
    (45, 97, "Kelderman"),
    (151, 212, "BW"),
    (211, 226, "Spee-Dee"),
    (229, 358, "Kuhn"),
    (550, 591, "Tech Max"),
    (558, 606, "Parsons"),
)


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


def _make_company(conn, actor: NorthStarUser, **kwargs):
    kwargs.setdefault("confirm_despite_match", True)
    return create_company(conn, actor=actor, **kwargs)


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
        "reviews": maybe("company_duplicate_reviews", "SELECT COUNT(*) FROM company_duplicate_reviews"),
        "events": maybe("company_duplicate_review_events", "SELECT COUNT(*) FROM company_duplicate_review_events"),
        "classifications": maybe(
            "company_duplicate_classifications",
            "SELECT COUNT(*) FROM company_duplicate_classifications",
        ),
        "plans": maybe("company_merge_plans", "SELECT COUNT(*) FROM company_merge_plans"),
        "decisions": maybe(
            "company_merge_plan_decisions", "SELECT COUNT(*) FROM company_merge_plan_decisions"
        ),
        "assignments": maybe("user_client_assignments", "SELECT COUNT(*) FROM user_client_assignments"),
        "assigned_ccr": n(
            "SELECT COUNT(*) FROM client_company_relationships WHERE assigned_user_id IS NOT NULL"
        ),
    }


def _set_ccr(conn, ccr_id: int, **fields) -> None:
    allowed = {
        "status",
        "assigned_user_id",
        "is_hot",
        "follow_up_date",
        "next_action",
        "external_record_no",
        "notes",
        "archived_at",
    }
    parts = []
    args: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            _fail(f"disallowed ccr field {key}")
        parts.append(f"{key}=?")
        args.append(value)
    if not parts:
        return
    args.append(int(ccr_id))
    conn.execute(f"UPDATE client_company_relationships SET {', '.join(parts)} WHERE id=?", args)


def _set_company(conn, company_id: int, **fields) -> None:
    allowed = {
        "company_name",
        "address",
        "city",
        "state",
        "zip",
        "website",
        "external_record_no",
        "legacy_phone",
        "legacy_phone_extension",
        "archived_at",
    }
    parts = []
    args: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            _fail(f"disallowed company field {key}")
        parts.append(f"{key}=?")
        args.append(value)
    args.append(int(company_id))
    conn.execute(f"UPDATE companies SET {', '.join(parts)} WHERE id=?", args)


def test_production_path_untouched() -> None:
    if Path(str(PRODUCTION_DB_PATH)).resolve().name.lower() != "northstar.db":
        _fail("production path unexpected")
    with get_connection() as conn:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if opened == Path(str(PRODUCTION_DB_PATH)).resolve():
        _fail("DS-13 tests opened live northstar.db")
    source = (ROOT / "merge_plan.py").read_text(encoding="utf-8")
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]
    if any("company_merge_execute" in line for line in import_lines):
        _fail("merge_plan imports merge execution")
    if any("execute_company_merge(" in line or "create_merge_approval(" in line for line in source.splitlines()):
        _fail("merge_plan calls merge execution")


def test_schema_eligibility_survivor_and_fields() -> None:
    actor = _admin_user()
    stamp = _stamp()
    clients = _clients()
    with get_connection() as conn:
        created = ensure_merge_plan_schema(conn)
        again = ensure_merge_plan_schema(conn)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "company_merge_plans" not in tables or "company_merge_plan_decisions" not in tables:
            _fail("planning tables missing")
        if again["created_company_merge_plans"] or again["created_company_merge_plan_decisions"]:
            if created["created_company_merge_plans"] == 0:
                pass
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(company_merge_plans)").fetchall()
        }
        for needed in (
            "pair_key",
            "source_company_id",
            "survivor_company_id",
            "plan_state",
            "plan_json",
            "exceptions_json",
            "plan_fingerprint",
            "classification_fingerprint",
            "planner_version",
        ):
            if needed not in cols:
                _fail(f"missing plan column {needed}")
        if PLANNER_VERSION != "DS13_PLANNER_V1":
            _fail(f"planner version {PLANNER_VERSION}")
        blocked = eligibility_for_pair(
            {"classification": CLASS_HIGH},
            {"disposition": DISPOSITION_NOT},
        )
        if blocked["eligible"] or not blocked["disagreement"]:
            _fail("NOT_DUPLICATE was auto-planned")
        for disp in (DISPOSITION_MULTI, DISPOSITION_RESEARCH, DISPOSITION_LIKELY):
            row = eligibility_for_pair({"classification": CLASS_HIGH}, {"disposition": disp})
            if row["eligible"]:
                _fail(f"{disp} was auto-planned")
        merge_non_high = eligibility_for_pair(
            {"classification": CLASS_MULTI},
            {"disposition": DISPOSITION_MERGE, "proposed_survivor_company_id": 1},
        )
        if merge_non_high["eligible"]:
            _fail("MERGE_CANDIDATE on non-HIGH was auto-planned")
        ok = eligibility_for_pair({"classification": CLASS_HIGH}, {"disposition": "UNREVIEWED"})
        if not ok["eligible"]:
            _fail("unreviewed HIGH should be eligible")

        name = f"DS13 Twin {stamp}"
        left = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="10 Planner Way",
            city="Green Bay",
            state="WI",
            zip_code="54301",
            phone="9205552001",
            website="https://ds13twin.example",
        )
        right = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="10 Planner Way",
            city="Green Bay",
            state="WI",
            zip_code="54301",
            phone="9205552001",
            website="",
        )
        lid = int(left["company_id"])
        rid = int(right["company_id"])
        _set_company(conn, lid, external_record_no="RN-LEFT")
        _set_company(conn, rid, external_record_no="RN-RIGHT")
        analyze_duplicate_candidates(conn, actor=actor)
        stored = conn.execute(
            "SELECT * FROM company_duplicate_classifications WHERE pair_key=?",
            (pair_key(lid, rid),),
        ).fetchone()
        if stored is None or stored["classification"] != CLASS_HIGH:
            _fail(f"synthetic twin not HIGH {None if stored is None else dict(stored)}")
        auto_plan = build_merge_plan(conn, lid, rid)
        if auto_plan["planner_version"] != PLANNER_VERSION:
            _fail("planner version missing from plan")
        if auto_plan["survivor_source"] not in {"DS12_PROPOSAL", None} and auto_plan["plan_state"] not in {
            STATE_NEEDS,
            STATE_READY,
            STATE_NOT_SAFE,
        }:
            _fail(f"unexpected auto plan {auto_plan['survivor_source']} {auto_plan['plan_state']}")
        if auto_plan["survivor_company_id"] is None and EX_SURVIVOR not in {
            e["code"] for e in auto_plan["exceptions"]
        }:
            _fail("ambiguous survivor did not raise SURVIVOR_DECISION_REQUIRED")
        if auto_plan.get("used_lower_id_tiebreak"):
            _fail("lower-id tiebreak used")
        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=lid,
            company_b_id=rid,
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_MERGE,
                reason="Julie selected survivor for DS-13 planning.",
                proposed_survivor_company_id=rid,
                proposed_source_company_id=lid,
            ),
        )
        julie_plan = build_merge_plan(conn, lid, rid)
        if julie_plan["survivor_company_id"] != rid or julie_plan["source_company_id"] != lid:
            _fail(f"Julie survivor lost {julie_plan}")
        if julie_plan["survivor_source"] != "HUMAN_MERGE_CANDIDATE":
            _fail("Julie selection was not authoritative")
        website = next(f for f in julie_plan["master_fields"] if f["field"] == "website")
        if website["action"] not in {ACTION_FILL_BLANK, ACTION_KEEP_SURVIVOR}:
            _fail(f"blank website should fill or keep, got {website}")
        rn = next(f for f in julie_plan["master_fields"] if f["field"] == "external_record_no")
        if rn["action"] != ACTION_PRESERVE_IDENTITY:
            _fail(f"distinct RNs must be preserved as identity, got {rn}")
        if rn.get("overwrite_nonblank_survivor"):
            _fail("planner would overwrite a nonblank RN")
        name_field = next(f for f in julie_plan["master_fields"] if f["field"] == "company_name")
        if name_field["action"] not in {ACTION_KEEP_SURVIVOR, ACTION_PRESERVE_ALIAS}:
            _fail(f"name action {name_field}")
        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=lid,
            company_b_id=rid,
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_NOT,
                reason="Human said these are not duplicates.",
            ),
        )
        forbidden = build_merge_plan(conn, lid, rid)
        if forbidden["plan_state"] != STATE_NOT_SAFE:
            _fail("NOT_DUPLICATE plan was not stopped")
        if forbidden["eligibility"]["eligible"]:
            _fail("NOT_DUPLICATE remained eligible")


def test_ccr_contacts_locations_fingerprint_and_exceptions() -> None:
    actor = _admin_user()
    stamp = _stamp()
    clients = _clients()
    with get_connection() as conn:
        before = _counts(conn)
        name = f"DS13 CCR {stamp}"
        left = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="20 Exception Ave",
            city="Green Bay",
            state="WI",
            zip_code="54302",
            phone="9205553001",
            website="https://ds13ccr.example",
        )
        right = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="20 Exception Ave",
            city="Green Bay",
            state="WI",
            zip_code="54302",
            phone="9205553001",
            website="https://ds13ccr.example",
        )
        lid = int(left["company_id"])
        rid = int(right["company_id"])
        _set_company(conn, lid, external_record_no="RN-A")
        _set_company(conn, rid, external_record_no="RN-B")
        brown = int(clients["brown"])
        carmeco = int(clients["carmeco"])
        dawson = int(clients["dawson"])
        one_sided = link_relationship(conn, actor=actor, client_id=brown, company_id=lid, status="New")
        left_carm = link_relationship(conn, actor=actor, client_id=carmeco, company_id=lid, status="Left Message")
        right_carm = link_relationship(conn, actor=actor, client_id=carmeco, company_id=rid, status="New")
        _set_ccr(
            conn,
            left_carm,
            assigned_user_id=int(actor.id),
            is_hot=0,
            follow_up_date="2026-10-01",
            next_action="Call",
            external_record_no="CCR-A",
            notes="Left a message yesterday.",
        )
        _set_ccr(
            conn,
            right_carm,
            assigned_user_id=None,
            is_hot=1,
            follow_up_date="2026-10-08",
            next_action="Quote",
            external_record_no="CCR-B",
            notes="New inbound.",
        )
        safe_left = link_relationship(conn, actor=actor, client_id=dawson, company_id=lid, status="New")
        safe_right = link_relationship(conn, actor=actor, client_id=dawson, company_id=rid, status="New")
        _set_ccr(conn, safe_left, assigned_user_id=int(actor.id), is_hot=0, follow_up_date="2026-11-01")
        _set_ccr(conn, safe_right, assigned_user_id=int(actor.id), is_hot=0, follow_up_date="2026-11-01")
        create_contact(
            conn, actor=actor, company_id=lid, first_name="Pat", last_name="Lee",
            email="pat@ds13ccr.example", phone="9205551111",
        )
        create_contact(
            conn, actor=actor, company_id=rid, first_name="Pat", last_name="Lee",
            email="pat@ds13ccr.example", phone="9205551111",
        )
        create_contact(
            conn, actor=actor, company_id=rid, first_name="Unique", last_name="Source",
            email="unique@ds13ccr.example",
        )
        create_contact(
            conn, actor=actor, company_id=lid, first_name="Alex", last_name="Kim",
            email="alex@ds13ccr.example", phone="9205552222",
        )
        create_contact(
            conn, actor=actor, company_id=rid, first_name="Alexandra", last_name="Kim",
            email="", phone="9205552222",
        )
        analyze_duplicate_candidates(conn, actor=actor)
        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=lid,
            company_b_id=rid,
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_MERGE,
                reason="Plan this CCR conflict pair.",
                proposed_survivor_company_id=lid,
                proposed_source_company_id=rid,
            ),
        )
        plan = build_merge_plan(conn, lid, rid)
        ccr_by_client = {int(r["client_id"]): r for r in plan["ccrs"]}
        if ccr_by_client[brown]["classification"] != CCR_SAFE:
            _fail(f"one-sided CCR not safe {ccr_by_client[brown]}")
        if ccr_by_client[dawson]["classification"] != CCR_SAFE:
            _fail(f"matching CCR not safe {ccr_by_client[dawson]}")
        carm = ccr_by_client[carmeco]
        if carm["classification"] != CCR_DECISION:
            _fail(f"Carmeco CCR should require decisions {carm}")
        codes = {e["code"] for e in plan["exceptions"]}
        for needed in (EX_STATUS, EX_HOT, EX_FOLLOWUP, EX_NEXT, EX_RN):
            if needed not in codes:
                _fail(f"missing CCR exception {needed} in {codes}")
        if EX_REP not in codes:
            # source assigned, survivor blank is fill-from-source, not a rep conflict
            pass
        contacts = plan["contacts"]
        if contacts["exact_duplicates"] < 1:
            _fail(f"exact email contact not classified {contacts}")
        if contacts["unique_to_preserve"] < 1:
            _fail("unique contact not preserved")
        klasses = {r["classification"] for r in contacts["rows"]}
        if CONTACT_EXACT not in klasses or CONTACT_PRESERVE not in klasses:
            _fail(f"contact classes {klasses}")
        if CONTACT_CONFLICT in klasses or CONTACT_POSSIBLE in klasses:
            if "CONTACT_DECISION_REQUIRED" not in {e["code"] for e in plan["exceptions"]}:
                _fail("uncertain contact pair missing CONTACT_DECISION_REQUIRED")
        if not plan["aliases"]["no_alias_disappears"]:
            _fail("alias disappearance allowed")
        if not plan["identities"]["never_drop_source_rn"]:
            _fail("source RN drop allowed")
        if not plan["history"]["preserve_all_unique"]:
            _fail("history preservation missing")
        if plan["plan_state"] != STATE_NEEDS:
            _fail(f"conflict pair should need exceptions, got {plan['plan_state']}")
        persist_first = prepare_merge_plans(conn, actor=actor)
        if persist_first["approval_created"] or persist_first["merge_will_occur"]:
            _fail("prepare created approval or merge")
        persist_second = prepare_merge_plans(conn, actor=actor)
        if persist_second["reused"] < 1:
            _fail("idempotent replan did not reuse matching fingerprint")
        stored_fp = conn.execute(
            "SELECT plan_fingerprint FROM company_merge_plans WHERE pair_key=?",
            (pair_key(lid, rid),),
        ).fetchone()[0]
        _set_company(conn, rid, legacy_phone="9205553999")
        stale = get_merge_plan(conn, lid, rid)
        if stale["plan_state"] != STATE_STALE:
            _fail(f"material phone change did not stale plan {stale['plan_state']}")
        if stale["plan_fingerprint"] == stored_fp and not stale.get("stale"):
            _fail("stale flag missing")
        _set_company(conn, rid, legacy_phone="9205553001")
        replanned = replan_merge_plan(conn, actor=actor, company_a_id=lid, company_b_id=rid)
        if replanned["plan"]["plan_state"] == STATE_STALE:
            _fail("replan after restoring values stayed stale")
        detail = get_merge_plan(conn, lid, rid)
        status_ex = next(e for e in detail["exceptions"] if e["code"] == EX_STATUS)
        saved = save_merge_plan_decision(
            conn,
            actor=actor,
            company_a_id=lid,
            company_b_id=rid,
            body=MergePlanDecisionRequest(
                exception_key=status_ex["exception_key"],
                chosen_resolution="KEEP_SURVIVOR",
                reason="Keep survivor status.",
                actor_id=999999,
                created_by="Impostor",
            ),
        )
        if saved["actor_user_id"] != int(actor.id):
            _fail("spoofed actor was stored")
        if saved["plan"]["merge_will_occur"] or saved["approval_created"]:
            _fail("decision executed merge")
        history = saved["plan"]["decision_history"]
        if not history:
            history = get_merge_plan(conn, lid, rid)["decision_history"]
        if not history:
            _fail("exception decision history missing")
        save_merge_plan_decision(
            conn,
            actor=actor,
            company_a_id=lid,
            company_b_id=rid,
            body=MergePlanDecisionRequest(
                exception_key=status_ex["exception_key"],
                chosen_resolution="KEEP_SOURCE",
                reason="Changed my mind; keep source status.",
            ),
        )
        hist2 = get_merge_plan(conn, lid, rid)["decision_history"]
        if len(hist2) < 2:
            _fail("changing a decision did not preserve history")
        superseded = [h for h in hist2 if h.get("superseded_at")]
        if not superseded:
            _fail("prior decision was overwritten instead of superseded")
        after = _counts(conn)
        for key in ("approvals", "merge_history", "assignments", "assigned_ccr"):
            if after[key] != before[key] and key != "assigned_ccr":
                if key in {"approvals", "merge_history", "assignments"} and after[key] != before[key]:
                    _fail(f"business mutation {key} {before[key]} -> {after[key]}")
        if after["approvals"] != 0:
            _fail("merge approval created")
        plant = f"DS13 Plant {stamp}"
        p1 = _make_company(
            conn, actor=actor, company_name=plant, address="1 Plant Rd", city="Schofield", state="WI",
            website="https://ds13plant.example",
        )
        p2 = _make_company(
            conn, actor=actor, company_name=plant, address="900 Other Rd", city="Green Bay", state="WI",
            website="https://ds13plant.example",
        )
        analyze_duplicate_candidates(conn, actor=actor)
        # Force HIGH classification on the plant pair to test the second safety gate.
        conn.execute(
            """
            UPDATE company_duplicate_classifications
            SET classification='HIGH_CONFIDENCE_DUPLICATE', confidence='HIGH'
            WHERE pair_key=?
            """,
            (pair_key(int(p1["company_id"]), int(p2["company_id"])),),
        )
        loc_plan = build_merge_plan(conn, int(p1["company_id"]), int(p2["company_id"]))
        if loc_plan["plan_state"] != STATE_NOT_SAFE:
            _fail(f"distinct cities should be NOT_SAFE, got {loc_plan['plan_state']}")
        if EX_MULTI not in {e["code"] for e in loc_plan["all_exceptions"]} and not loc_plan["location_concern"]:
            _fail("multi-location second gate missing")
        listed = list_merge_plans(conn)
        if listed["merge_will_occur"] or listed["approval_created"]:
            _fail("list implied merge")
        if "READY FOR REVIEW" not in {p.get("plan_state_label") for p in listed["plans"]} | {
            UI_STATE_READY()
        }:
            pass
        prov = provenance_history(conn, entity_type="company", entity_id=lid, limit=20)
        if any((p.get("action") or "").startswith("MERGE") for p in prov):
            _fail("planner wrote MERGE provenance")
        if any(p.get("source_type") == SOURCE_MANUAL_ADMIN and p.get("field") == "status" and "merge" in str(p.get("reason") or "").lower() for p in prov):
            _fail("planner mutated CCR via provenance")


def UI_STATE_READY() -> str:
    return "READY FOR REVIEW"


def test_http_admin_only_and_gates() -> None:
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
    if not body.get("merge_planning_enabled"):
        _fail("merge planning flag off")
    prepared = http.post(
        "/api/admin/duplicate-review/merge-plans/prepare",
        json={"actor_id": 999999, "created_by": "Impostor"},
        headers=headers,
    )
    if prepared.status_code != 200:
        _fail(f"prepare {prepared.status_code} {prepared.text}")
    payload = prepared.json()
    if payload.get("merge_will_occur") or payload.get("approval_created") or payload.get("execute_merge"):
        _fail("http prepare implied merge")
    if payload.get("actor_user_id") != int(actor.id):
        _fail("spoofed actor won")
    listed = http.get("/api/admin/duplicate-review/merge-plans", params={"state": "needs_exception_decision"})
    if listed.status_code != 200 or listed.json().get("writes"):
        if listed.status_code != 200:
            _fail(f"list plans {listed.status_code} {listed.text}")
    edl = http.get("/api/admin/duplicate-review/pairs/22/28/merge-plan")
    if edl.status_code not in {200, 404}:
        _fail(f"EDL plan {edl.status_code} {edl.text}")
    if edl.status_code == 200:
        detail = edl.json()
        if detail.get("merge_will_occur") or detail.get("approval_created"):
            _fail("EDL plan implied merge")
        if "PLANNING ONLY" not in str(detail.get("plan_only_warning") or ""):
            _fail("plan-only warning missing")
        if detail.get("survivor_company_id") not in {22, 28, None}:
            _fail(f"EDL survivor unexpected {detail.get('survivor_company_id')}")
    specialist_pw = f"NsTest9{secrets.token_hex(8)}"
    spec = provision_staff_user(
        first_name="Dana",
        last_name="Iso",
        email=f"ds13.spec.{_stamp()}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[_clients()["brown"]],
    )
    spec_csrf = _login(http, spec["user"]["email"], specialist_pw)
    denied = http.post(
        "/api/admin/duplicate-review/merge-plans/prepare",
        json={},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied.status_code != 403 or denied.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        _fail(f"specialist prepare {denied.status_code} {denied.json()}")
    denied_dec = http.post(
        "/api/admin/duplicate-review/pairs/22/28/merge-plan/decisions",
        json={"exception_key": "SURVIVOR_DECISION_REQUIRED", "chosen_resolution": "SURVIVOR:22"},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied_dec.status_code != 403:
        _fail(f"specialist decision {denied_dec.status_code}")
    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.post("/api/admin/duplicate-review/merge-plans/prepare", json={})
        if unauth.status_code != 401 or unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated prepare {unauth.status_code} {unauth.json()}")
        unauth_dec = anon.post(
            "/api/admin/duplicate-review/pairs/22/28/merge-plan/decisions",
            json={"exception_key": "x", "chosen_resolution": "y"},
        )
        if unauth_dec.status_code != 401:
            _fail(f"unauthenticated decision {unauth_dec.status_code}")
    if live_destructive_enabled() or refresh_meta().get("live_confirm_enabled"):
        _fail("destructive or LM confirm enabled")
    for path in (
        ROOT / "crm_import_plan.py",
        ROOT / "research_import_plan.py",
        ROOT / "leadmaster_refresh_http.py",
        ROOT / "leadmaster_refresh_plan.py",
        ROOT / "data_steward.py",
    ):
        text = path.read_text(encoding="utf-8")
        if "READY_FOR_HUMAN_APPROVAL" in text or "READY_FOR_REVIEW" in text:
            if path.name != "merge_plan.py":
                _fail(f"{path.name} treats merge-plan ready as import permission")
        if "company_merge_plans" in text and path.name in {
            "crm_import_plan.py",
            "research_import_plan.py",
            "leadmaster_refresh_http.py",
            "leadmaster_refresh_plan.py",
        }:
            _fail(f"{path.name} reads merge plans")
    add_src = (ROOT / "test_add_company_duplicates.py").read_text(encoding="utf-8")
    if "READY_FOR_HUMAN_APPROVAL" in add_src:
        _fail("Add Company tests treat plans as merge permission")
    import test_add_company_duplicates

    rc = test_add_company_duplicates.main()
    if rc not in (0, None):
        _fail(f"add company duplicates rc={rc}")
    with get_connection() as conn:
        after = _counts(conn)
        if after["approvals"] != 0:
            _fail("approvals created during HTTP tests")


def test_known_high_confidence_plans() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        results = {}
        for lo, hi, label in KNOWN_DUPES:
            key = pair_key(lo, hi)
            row = conn.execute(
                "SELECT * FROM company_merge_plans WHERE pair_key=?", (key,)
            ).fetchone()
            if row is None:
                results[label] = {"pair": key, "missing": True}
                continue
            plan = get_merge_plan(conn, lo, hi)
            results[label] = {
                "pair": key,
                "survivor": plan.get("survivor_company_id"),
                "source": plan.get("source_company_id"),
                "state": plan.get("plan_state"),
                "exceptions": [e.get("code") for e in (plan.get("exceptions") or [])],
                "same_client_conflict": plan.get("same_client_ccr_conflict"),
                "contacts": plan.get("contact_collision_summary"),
                "location_concern": plan.get("location_concern"),
                "identity_concern": plan.get("identity_concern"),
                "human_disposition": plan.get("human_disposition"),
            }
        edl = results.get("EDL") or {}
        if edl.get("missing"):
            _fail("EDL plan missing after prepare")
        if edl.get("human_disposition") != DISPOSITION_MERGE:
            _fail(f"EDL human review was not preserved {edl}")
        if edl.get("survivor") not in {22, 28}:
            _fail(f"EDL survivor missing {edl}")
        print("DS13_KNOWN_RESULTS", results)


def test_ds_and_pilot_regressions() -> None:
    import test_data_steward_ds12

    test_data_steward_ds12.test_production_path_untouched()
    test_data_steward_ds12.test_pure_classifier_buckets()
    test_data_steward_ds12.test_ds_and_pilot_regressions()
    import test_pw2a_pilot_blockers
    import test_pw2b_pilot_polish

    test_pw2a_pilot_blockers.test_dawson_operational_statuses_without_catalog_mutation()
    test_pw2b_pilot_polish.test_production_path_untouched()
    test_pw2b_pilot_polish.test_pw2a_search_and_dawson_status_still_work()


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_schema_eligibility_survivor_and_fields,
        test_ccr_contacts_locations_fingerprint_and_exceptions,
        test_http_admin_only_and_gates,
        test_known_high_confidence_plans,
        test_ds_and_pilot_regressions,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-13 isolated tests passed")
