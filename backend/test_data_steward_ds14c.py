"""DS-14C restore DS-14A queue interaction without losing DS-14B preview.

Isolated testdb only. Never writes live northstar.db. Does not save Julie's
duplicate decisions, create merge approvals, enable merge execution, hard
delete, LeadMaster confirm, or create pilot users.
"""

from __future__ import annotations

from pathlib import Path

import testdb  # noqa: F401

from db import PRODUCTION_DB_PATH, get_connection
from merge_plan import EX_CONTACT, EX_FIELD, EX_RN, EX_STATUS, prepare_merge_plans
from merge_workbench import get_workbench_plan, list_workbench_plans
from models import NorthStarUser

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT.parent / "frontend" / "src"
KNOWN = {
    "EDL": (22, 28),
    "Kelderman": (45, 97),
    "Kuhn": (229, 358),
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
        "identities": maybe("company_identities", "SELECT COUNT(*) FROM company_identities"),
        "aliases": maybe("company_aliases", "SELECT COUNT(*) FROM company_aliases"),
        "locations": maybe("company_locations", "SELECT COUNT(*) FROM company_locations"),
    }


def test_production_path_untouched() -> None:
    with get_connection() as conn:
        isolated = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    if isolated.lower().replace("\\", "/") == str(PRODUCTION_DB_PATH).lower().replace("\\", "/"):
        _fail("tests are using live northstar.db")


def test_frontend_queue_controls_and_preservation_copy() -> None:
    workbench = (FRONTEND / "AdministrationDuplicateWorkbench.tsx").read_text(encoding="utf-8")
    admin = (FRONTEND / "Administration.tsx").read_text(encoding="utf-8")
    review = (FRONTEND / "AdministrationDuplicateReview.tsx").read_text(encoding="utf-8")
    queue_test = (FRONTEND / "AdministrationMergePlanning.queue.test.tsx").read_text(encoding="utf-8")
    css = (FRONTEND / "App.css").read_text(encoding="utf-8")

    if "KEEP A AS SURVIVOR" in workbench or "KEEP B AS SURVIVOR" in workbench:
        _fail("generic KEEP A/B labels remain")
    if "KEEP RECORD {" not in workbench:
        _fail("KEEP RECORD {id} labels missing")
    if "REVIEW DETAILS" not in workbench:
        _fail("REVIEW DETAILS missing")
    if "CLOSE DETAILS" not in workbench:
        _fail("CLOSE DETAILS missing")
    if "stopCardOpen" not in workbench:
        _fail("decision-button stopPropagation helper missing")
    if "openDetails(row)" not in workbench:
        _fail("card/body openDetails missing")
    if "data-queue-card" not in workbench:
        _fail("queue card marker missing")
    if "dup-queue-status" not in workbench or "dup-queue-assigned" not in workbench:
        _fail("queue status/assigned classes missing")
    if "Assigned:" not in workbench:
        _fail("Assigned: display missing")
    if "preservation" not in workbench or "DATA TYPE" not in workbench:
        _fail("DS-14B preservation table missing from workbench")
    if "EXPECTED AFTER" not in workbench or "PLANNED ACTION" not in workbench:
        _fail("DS-14B preservation columns missing")
    if "executeMerge" in workbench or "Execute Merge" in workbench:
        _fail("frontend merge execution")
    if "dataMgmtTab === 'duplicate-review' ? <AdministrationDuplicateReview" not in admin.replace("\n", " "):
        if "dataMgmtTab === 'duplicate-review' ? <AdministrationDuplicateReview />" not in admin:
            _fail("Administration no longer mounts Duplicate Review from Data Management")
    if "MergePlanningPanel" not in review:
        _fail("Duplicate Review no longer renders MergePlanningPanel")
    if ".dup-queue-actions" not in css:
        _fail("sticky queue actions CSS missing")
    if "Data Management" not in queue_test or "Merge Planning" not in queue_test:
        _fail("production-path queue test does not walk Administration tabs")
    if "KEEP RECORD 45" not in queue_test or "REVIEW DETAILS" not in queue_test:
        _fail("production-path queue test missing required labels")
    if "Carmeco — New" not in queue_test or "Assigned: Julie Magnani" not in queue_test:
        _fail("production-path queue test missing status/assigned assertions")
    if "DATA TYPE" not in queue_test or "EXPECTED AFTER" not in queue_test:
        _fail("production-path queue test missing DS-14B preservation assertions")


def test_inspect_does_not_write_and_keeps_preservation() -> None:
    user = _admin_user()
    with get_connection() as conn:
        before = _counts(conn)
        prepare_merge_plans(conn, actor=user)
        listed = list_workbench_plans(conn, state="NEEDS_EXCEPTION_DECISION", limit=50)
        keys = {(row.get("company_a_id"), row.get("company_b_id")) for row in (listed.get("plans") or [])}
        if KNOWN["Kelderman"] not in keys and (KNOWN["Kelderman"][1], KNOWN["Kelderman"][0]) not in keys:
            _fail("Kelderman not in needs-decision queue")
        kelderman = get_workbench_plan(conn, *KNOWN["Kelderman"])
        kuhn = get_workbench_plan(conn, *KNOWN["Kuhn"])
        edl = get_workbench_plan(conn, *KNOWN["EDL"])
        if not kelderman.get("preservation"):
            _fail("Kelderman preservation preview missing")
        table = {row.get("data_type"): row for row in (kelderman.get("preservation") or {}).get("table") or []}
        if "LeadMaster IDs" not in table or "Contacts" not in table:
            _fail(f"Kelderman preservation table incomplete {list(table)}")
        contacts = (kelderman.get("preservation") or {}).get("contacts") or {}
        names = {str(row.get("name") or "") for row in (contacts.get("rows") or [])}
        if "Gary L Kelderman" not in names:
            _fail(f"Gary missing from preservation {names}")
        if not kuhn.get("company_a") or not edl.get("company_a"):
            _fail("Kuhn/EDL detail missing")
        codes = {row.get("code") for row in (edl.get("exceptions") or [])}
        if not {EX_FIELD, EX_STATUS, EX_RN, EX_CONTACT}.issubset(codes):
            _fail(f"EDL exceptions {codes}")
        after = _counts(conn)
        if before.get("approvals") != after.get("approvals"):
            _fail("merge approval created")
        if after.get("decisions") != before.get("decisions"):
            _fail("plan decision written by inspect")
        for key in ("companies", "contacts", "ccr", "identities", "aliases", "locations"):
            if before.get(key) != after.get(key):
                _fail(f"{key} mutated {before.get(key)} -> {after.get(key)}")


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_frontend_queue_controls_and_preservation_copy,
        test_inspect_does_not_write_and_keeps_preservation,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-14C isolated tests passed")
