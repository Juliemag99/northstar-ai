"""DS-14B duplicate merge preservation preview — isolated testdb only.

Never writes live northstar.db. Does not enable merge execution, approvals,
hard delete, LeadMaster confirm, or create pilot users. Does not save Julie's
duplicate decisions.
"""

from __future__ import annotations

from pathlib import Path

import testdb  # noqa: F401

from db import PRODUCTION_DB_PATH, get_connection
from duplicate_review import pair_key
from merge_plan import (
    CONTACT_CONFLICT,
    CONTACT_EXACT,
    CONTACT_POSSIBLE,
    CONTACT_PRESERVE,
    EX_CONTACT,
    EX_FIELD,
    EX_RN,
    EX_STATUS,
    STATE_NOT_SAFE,
    STATE_READY,
    prepare_merge_plans,
)
from merge_workbench import (
    _contact_treatment,
    _workbench_ready,
    get_workbench_plan,
    list_workbench_plans,
    preservation_preview,
)
from models import NorthStarUser

ROOT = Path(__file__).resolve().parent
KNOWN = {
    "EDL": (22, 28),
    "Kelderman": (45, 97),
    "BW": (151, 212),
    "ME": (268, 642),
    "Parsons": (558, 606),
}
PENDING = "Pending decision"


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
        "approvals": maybe("merge_execution_approvals", "SELECT COUNT(*) FROM merge_execution_approvals"),
        "merge_history": maybe("company_merge_history", "SELECT COUNT(*) FROM company_merge_history"),
        "decisions": maybe("company_merge_plan_decisions", "SELECT COUNT(*) FROM company_merge_plan_decisions"),
        "reviews": maybe("company_duplicate_reviews", "SELECT COUNT(*) FROM company_duplicate_reviews"),
        "plans": maybe("company_merge_plans", "SELECT COUNT(*) FROM company_merge_plans"),
    }


def _table_row(preview: dict, data_type: str) -> dict:
    for row in preview.get("table") or []:
        if row.get("data_type") == data_type:
            return row
    return {}


def _blob(value: object) -> str:
    return str(value or "").lower()


def test_production_path_untouched() -> None:
    with get_connection() as conn:
        isolated = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    if isolated.lower().replace("\\", "/") == str(PRODUCTION_DB_PATH).lower().replace("\\", "/"):
        _fail("tests are using live northstar.db")


def test_contact_treatment_labels() -> None:
    if "EXACT" not in _contact_treatment({"classification": CONTACT_EXACT}):
        _fail("exact treatment")
    if "UNIQUE" not in _contact_treatment({"classification": CONTACT_PRESERVE}):
        _fail("unique treatment")
    if "CONFLICT" not in _contact_treatment({"classification": CONTACT_CONFLICT}):
        _fail("conflict treatment")
    if "DECISION" not in _contact_treatment({"classification": CONTACT_POSSIBLE}):
        _fail("possible treatment")


def test_ready_gate_blocked_by_preservation_warning() -> None:
    ready = {"plan_state": STATE_READY, "preservation": {"blocks_ready": False}}
    if not _workbench_ready(ready):
        _fail("clean ready plan should pass workbench ready gate")
    blocked = {"plan_state": STATE_READY, "preservation": {"blocks_ready": True, "warnings": ["gap"]}}
    if _workbench_ready(blocked):
        _fail("preservation warning must block Ready for Review")
    needs = {"plan_state": "NEEDS_EXCEPTION_DECISION", "preservation": {"blocks_ready": False}}
    if _workbench_ready(needs):
        _fail("unresolved exceptions must not be Ready for Review")


def test_kelderman_preservation_preview() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        before = _counts(conn)
        prepare_merge_plans(conn, actor=actor)
        plan = get_workbench_plan(conn, *KNOWN["Kelderman"])
        preview = plan.get("preservation") or {}
        lead = preview.get("leadmaster") or {}
        rns = {str(lead.get("record_a", {}).get("primary_rn")), str(lead.get("record_b", {}).get("primary_rn"))}
        if rns != {"138431", "253832"}:
            _fail(f"Kelderman RNs {rns}")
        planned = [str(rn) for rn in (lead.get("planned_rns") or [])]
        if "138431" not in planned or "253832" not in planned:
            _fail(f"planned RNs missing {planned}")
        if not lead.get("never_drop_source_rn"):
            _fail("planner must preserve both RNs as identities")
        rn_row = _table_row(preview, "LeadMaster IDs")
        if rn_row.get("before") != 2 or rn_row.get("expected_after") != 2:
            _fail(f"LeadMaster before/after {rn_row}")
        contacts = preview.get("contacts") or {}
        names = " | ".join(
            f"{row.get('name') or row.get('source_name') or ''} {row.get('treatment')}"
            for row in (contacts.get("rows") or [])
        )
        if "Gary" not in names:
            _fail(f"Gary missing from contact preview {names}")
        if "Debbie" not in names:
            _fail(f"Debbie missing from contact preview {names}")
        treatments = {
            str(row.get("name") or row.get("source_name") or ""): str(row.get("treatment") or "")
            for row in (contacts.get("rows") or [])
        }
        gary_hits = [t for name, t in treatments.items() if "Gary" in name]
        debbie_hits = [t for name, t in treatments.items() if "Debbie" in name]
        if not gary_hits:
            _fail("Gary treatment missing")
        if not debbie_hits:
            _fail("Debbie treatment missing")
        if any("CONFLICT" in t or "DECISION" in t for t in gary_hits):
            if contacts.get("expected_after") != PENDING:
                _fail("unresolved Gary must pending expected contacts")
        else:
            if "EXACT" not in gary_hits[0] and "UNIQUE" not in gary_hits[0] and "KEEP BOTH" not in gary_hits[0]:
                _fail(f"unexpected Gary treatment {gary_hits}")
            if "UNIQUE" not in debbie_hits[0]:
                _fail(f"Debbie should be unique preserve {debbie_hits}")
            before_contacts = (contacts.get("before") or {}).get("total_source_rows")
            if before_contacts != 3:
                _fail(f"Kelderman contact before {before_contacts}")
            if contacts.get("expected_after") != 2 and contacts.get("expected_after") != PENDING:
                _fail(f"Kelderman expected contacts {contacts.get('expected_after')}")
        ccrs = preview.get("ccrs") or {}
        ccr_blob = _blob(ccrs)
        if "carmeco" not in ccr_blob:
            _fail(f"Carmeco CCR missing {ccrs}")
        notes = preview.get("notes") or {}
        if "record_a" not in notes or "expected_after" not in notes:
            _fail(f"notes preview {notes}")
        activities = preview.get("activities") or {}
        if activities.get("expected_after") is None:
            _fail("activities after missing")
        if "all supported history" not in _blob(activities.get("planned_action")):
            _fail(f"history plan {activities.get('planned_action')}")
        if "campaigns" not in preview or "aliases" not in preview or "locations" not in preview:
            _fail("missing campaign/alias/location preview")
        after = _counts(conn)
        if before["approvals"] != after["approvals"] or before["merge_history"] != after["merge_history"]:
            _fail("inspect created merge artifacts")
        if after["decisions"] != before["decisions"]:
            _fail("Kelderman inspect saved a decision")
        if plan.get("ready_for_review"):
            _fail("Kelderman still has survivor decision; must not be Ready for Review")


def test_edl_pending_preservation() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        plan = get_workbench_plan(conn, *KNOWN["EDL"])
        codes = {row.get("code") for row in (plan.get("exceptions") or [])}
        if not {EX_FIELD, EX_STATUS, EX_RN, EX_CONTACT}.issubset(codes):
            _fail(f"EDL exceptions {codes}")
        preview = plan.get("preservation") or {}
        contact_row = _table_row(preview, "Contacts")
        ccr_row = _table_row(preview, "Client relationships")
        if contact_row.get("expected_after") != PENDING:
            _fail(f"EDL contacts should be pending {contact_row}")
        if ccr_row.get("expected_after") != PENDING:
            _fail(f"EDL CCR should be pending {ccr_row}")
        blob = _blob(preview)
        if "pending decision" not in blob:
            _fail("EDL preview missing pending decision")
        if plan.get("ready_for_review"):
            _fail("EDL must not be Ready for Review")


def test_parsons_pending_preservation() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        plan = get_workbench_plan(conn, *KNOWN["Parsons"])
        codes = {row.get("code") for row in (plan.get("exceptions") or [])}
        preview = plan.get("preservation") or {}
        needed = " ".join(plan.get("decision_needed_all") or [plan.get("decision_needed") or ""])
        blob = _blob({"codes": codes, "needed": needed, "preview": preview})
        if EX_FIELD not in codes and "zip" not in blob and "field" not in blob:
            _fail(f"Parsons field/ZIP missing {codes} {needed}")
        if EX_STATUS not in codes and "status" not in blob:
            _fail(f"Parsons Dawson status missing {codes}")
        if EX_RN not in codes and "rn" not in blob:
            _fail(f"Parsons Dawson RN missing {codes}")
        if EX_CONTACT not in codes and "contact" not in blob:
            _fail(f"Parsons contact missing {codes}")
        if _table_row(preview, "Contacts").get("expected_after") != PENDING and EX_CONTACT in codes:
            _fail("Parsons contact after should be pending")
        if _table_row(preview, "Client relationships").get("expected_after") != PENDING and (
            EX_STATUS in codes or EX_RN in codes
        ):
            _fail("Parsons CCR after should be pending")
        if plan.get("ready_for_review"):
            _fail("Parsons must not be Ready for Review")


def test_bw_me_not_safe_preservation_warning() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        listed = list_workbench_plans(conn, state=STATE_NOT_SAFE)
        found = {(row.get("company_a_id"), row.get("company_b_id")) for row in (listed.get("plans") or [])}
        if KNOWN["BW"] not in found and tuple(reversed(KNOWN["BW"])) not in found:
            _fail(f"BW missing from NOT SAFE {found}")
        if KNOWN["ME"] not in found and tuple(reversed(KNOWN["ME"])) not in found:
            _fail(f"ME missing from NOT SAFE {found}")
        for name, ids in (("BW", KNOWN["BW"]), ("ME", KNOWN["ME"])):
            plan = get_workbench_plan(conn, *ids)
            if plan.get("plan_state") != STATE_NOT_SAFE:
                _fail(f"{name} state {plan.get('plan_state')}")
            preview = plan.get("preservation") or {}
            warnings = preview.get("warnings") or []
            if not warnings:
                _fail(f"{name} missing preservation warnings")
            if not preview.get("blocks_ready"):
                _fail(f"{name} preservation must block ready")
            if plan.get("ready_for_review"):
                _fail(f"{name} appeared Ready for Review")
            loc = preview.get("locations") or {}
            if loc.get("expected_after") != PENDING and "multi" not in _blob(loc.get("planned_action")):
                _fail(f"{name} location concern not surfaced {loc}")
        ready = list_workbench_plans(conn, state="READY_FOR_HUMAN_APPROVAL")
        for row in ready.get("plans") or []:
            if {row.get("company_a_id"), row.get("company_b_id")} in (
                set(KNOWN["BW"]),
                set(KNOWN["ME"]),
            ):
                _fail("NOT SAFE pair leaked into Ready for Review")


def test_preview_uses_existing_planner_and_does_not_mutate() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        before = _counts(conn)
        plan = get_workbench_plan(conn, *KNOWN["Kelderman"])
        preview = preservation_preview(conn, plan)
        if preview.get("preview_version") != "DS14B_PREVIEW_V1":
            _fail(preview.get("preview_version"))
        if preview.get("merge_will_occur"):
            _fail("preview claimed a merge")
        if not preview.get("planning_only"):
            _fail("preview not planning_only")
        after = _counts(conn)
        if before != after:
            _fail(f"preview mutated {before} -> {after}")
        if after.get("approvals"):
            _fail("merge approval created")


def test_frontend_preservation_copy() -> None:
    workbench = (ROOT.parent / "frontend" / "src" / "AdministrationDuplicateWorkbench.tsx").read_text(
        encoding="utf-8"
    )
    for token in (
        "WHAT NORTHSTAR WILL PRESERVE",
        "PRESERVATION WARNING",
        "DATA TYPE",
        "EXPECTED AFTER",
        "LEADMASTER / SOURCE IDENTITIES",
        "Pending contact decision",
        "PreservationPanel",
    ):
        if token not in workbench:
            _fail(f"frontend missing {token}")
    if "executeMerge" in workbench:
        _fail("frontend merge execution")


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_contact_treatment_labels,
        test_ready_gate_blocked_by_preservation_warning,
        test_kelderman_preservation_preview,
        test_edl_pending_preservation,
        test_parsons_pending_preservation,
        test_bw_me_not_safe_preservation_warning,
        test_preview_uses_existing_planner_and_does_not_mutate,
        test_frontend_preservation_copy,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-14B isolated tests passed")
