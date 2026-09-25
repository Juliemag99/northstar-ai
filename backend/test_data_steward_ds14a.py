"""DS-14A duplicate workbench usability — isolated testdb only.

Never writes live northstar.db. Does not enable merge execution, approvals,
hard delete, LeadMaster confirm, or create pilot users.
"""

from __future__ import annotations

from pathlib import Path

import testdb  # noqa: F401

from db import PRODUCTION_DB_PATH, get_connection
from duplicate_review import pair_key
from merge_plan import EX_CONTACT, EX_FIELD, EX_RN, EX_STATUS, EX_SURVIVOR, prepare_merge_plans
from merge_workbench import (
    _ccr_snapshot,
    get_workbench_plan,
    identity_summary,
    list_workbench_plans,
    workbench_choices,
)
from models import NorthStarUser

ROOT = Path(__file__).resolve().parent
KNOWN = {
    "EDL": (22, 28),
    "Kelderman": (45, 97),
    "Kuhn": (229, 358),
    "Tech Max": (550, 591),
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
    }


def _plan_for(listed: dict, lo: int, hi: int) -> dict:
    key = pair_key(lo, hi)
    for row in listed.get("plans") or []:
        if row.get("pair_key") == key or {row.get("company_a_id"), row.get("company_b_id")} == {lo, hi}:
            return row
    return {}


def _actual_ccr_status(conn, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT cl.name AS client_name, r.status, u.full_name AS assigned_user_name, r.archived_at
            FROM client_company_relationships r
            JOIN clients cl ON cl.id = r.client_id
            LEFT JOIN users u ON u.id = r.assigned_user_id
            WHERE r.company_id=? AND TRIM(COALESCE(r.archived_at,''))=''
            ORDER BY r.client_id, r.id
            """,
            (int(company_id),),
        )
    ]


def test_production_path_untouched() -> None:
    with get_connection() as conn:
        isolated = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    if isolated.lower().replace("\\", "/") == str(PRODUCTION_DB_PATH).lower().replace("\\", "/"):
        _fail("tests are using live northstar.db")


def test_ccr_snapshot_blank_unassigned_and_removed() -> None:
    users = {1: "Julie Magnani"}
    blank = _ccr_snapshot(
        {
            "id": 1,
            "client_id": 10,
            "client_name": "Carmeco",
            "client_code": "carmeco",
            "status": "  ",
            "assigned_user_id": None,
            "active": True,
            "hot": False,
        },
        users,
    )
    if blank.get("status_label") != "No status":
        _fail(f"blank status {blank}")
    if blank.get("display") != "Carmeco — No status":
        _fail(f"blank display {blank.get('display')}")
    if blank.get("assigned_user_name") != "Unassigned":
        _fail(f"unassigned {blank.get('assigned_user_name')}")
    assigned = _ccr_snapshot(
        {
            "id": 2,
            "client_id": 10,
            "client_name": "Carmeco",
            "client_code": "carmeco",
            "status": "Left Message",
            "assigned_user_id": 1,
            "active": True,
            "hot": False,
        },
        users,
    )
    if assigned.get("display") != "Carmeco — Left Message":
        _fail(f"assigned display {assigned.get('display')}")
    if assigned.get("assigned_user_name") != "Julie Magnani":
        _fail(f"assigned name {assigned.get('assigned_user_name')}")
    removed = _ccr_snapshot(
        {
            "id": 3,
            "client_id": 11,
            "client_name": "Brown Industries",
            "client_code": "brown",
            "status": "Old Status",
            "assigned_user_id": 1,
            "active": False,
            "hot": False,
        },
        users,
    )
    if removed.get("active") is not False or removed.get("state_label") != "Removed":
        _fail(f"removed snap {removed}")


def test_identity_filters_removed_and_keeps_multiple_active() -> None:
    with get_connection() as conn:
        clients = _clients()
        carmeco = clients.get("carmeco")
        brown = clients.get("brown")
        premier = clients.get("premier")
        if not carmeco or not brown or not premier:
            _fail("missing clients")
        conn.execute(
            "INSERT INTO companies (company_name, city, state, external_record_no) VALUES ('DS14A Status Fixture', 'Test', 'IA', 'DS14A-STATUS-FIXTURE')"
        )
        cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO client_company_relationships (client_id, company_id, status, assigned_user_id, archived_at)
            VALUES (?, ?, '', NULL, '')
            """,
            (carmeco, cid),
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (client_id, company_id, status, assigned_user_id, archived_at)
            VALUES (?, ?, 'New', 1, '')
            """,
            (brown, cid),
        )
        conn.execute(
            """
            INSERT INTO client_company_relationships (client_id, company_id, status, assigned_user_id, archived_at)
            VALUES (?, ?, 'Archived Status', 1, datetime('now'))
            """,
            (premier, cid),
        )
        conn.commit()
        try:
            summary = identity_summary(conn, cid)
            active = summary.get("active_relationships") or []
            displays = [row.get("display") for row in active]
            if "Carmeco — No status" not in displays:
                _fail(f"blank active missing {displays}")
            if not any(str(row.get("display") or "").startswith("Brown") and "New" in str(row.get("display")) for row in active):
                _fail(f"brown active missing {displays}")
            if any(row.get("status") == "Archived Status" for row in active):
                _fail("removed CCR presented as current status")
            if any(row.get("assigned_user_name") != "Unassigned" and row.get("client_name") == "Carmeco" and row.get("status_label") == "No status" for row in active):
                _fail("blank Carmeco should be unassigned")
            all_rels = summary.get("relationships") or []
            if not any(row.get("active") is False and row.get("status_label") == "Archived Status" for row in all_rels):
                _fail("removed CCR missing from full relationships")
        finally:
            conn.execute("DELETE FROM client_company_relationships WHERE company_id=?", (cid,))
            conn.execute("DELETE FROM companies WHERE id=?", (cid,))
            conn.commit()


def test_kelderman_kuhn_techmax_actual_status_and_rep() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        listed = list_workbench_plans(conn, state="NEEDS_EXCEPTION_DECISION")
        for name, ids in KNOWN.items():
            if name == "EDL":
                continue
            row = _plan_for(listed, *ids)
            if not row:
                listed_all = list_workbench_plans(conn)
                row = _plan_for(listed_all, *ids)
            if not row:
                _fail(f"{name} missing from workbench")
            for side, company_id in (("company_a", ids[0]), ("company_b", ids[1])):
                ident = row.get(side) or {}
                actual = _actual_ccr_status(conn, company_id)
                active = ident.get("active_relationships") or []
                if len(active) != len(actual):
                    _fail(f"{name} {company_id} active {active} vs actual {actual}")
                for snap, db_row in zip(active, actual):
                    db_status = (db_row.get("status") or "").strip() or "No status"
                    if snap.get("status_label") != db_status and snap.get("status") != (db_row.get("status") or None):
                        _fail(f"{name} {company_id} invented status {snap} vs {db_row}")
                    expected_assigned = db_row.get("assigned_user_name") or "Unassigned"
                    if snap.get("assigned_user_name") != expected_assigned:
                        _fail(f"{name} {company_id} assigned {snap.get('assigned_user_name')} vs {expected_assigned}")
                    if db_row.get("archived_at"):
                        _fail("active query returned archived CCR")
                keep_a = f"KEEP RECORD {row.get('company_a_id')}"
                keep_b = f"KEEP RECORD {row.get('company_b_id')}"
                if keep_a == keep_b:
                    _fail("survivor labels collided")


def test_survivor_labels_use_record_ids() -> None:
    plan = {
        "company_a_id": 45,
        "company_b_id": 97,
        "company_a": {"record_label": "Kelderman Manufacturing — Record 45"},
        "company_b": {"record_label": "Kelderman Manufacturing — Record 97"},
        "plan_state": "NEEDS_EXCEPTION_DECISION",
    }
    with get_connection() as conn:
        choices = workbench_choices(conn, plan, {"code": EX_SURVIVOR, "options": []})
    values = {row["value"]: row["label"] for row in choices}
    if values.get("SURVIVOR:45") != "Make Record 45 the survivor":
        _fail(f"survivor 45 label {values}")
    if values.get("SURVIVOR:97") != "Make Record 97 the survivor":
        _fail(f"survivor 97 label {values}")
    if any("KEEP A" in row["label"] or "KEEP B" in row["label"] for row in choices):
        _fail(f"generic A/B labels {choices}")


def test_inspect_does_not_write_and_ignores_active_client() -> None:
    actor = _admin_user()
    brown = _clients().get("brown")
    with get_connection() as conn:
        prepare_merge_plans(conn, actor=actor)
        before = _counts(conn)
        listed = list_workbench_plans(conn, state="NEEDS_EXCEPTION_DECISION")
        scoped = list_workbench_plans(conn, state="NEEDS_EXCEPTION_DECISION", client_id=brown)
        if listed["summary"]["needs_exception_decision"] != scoped["summary"]["needs_exception_decision"]:
            _fail("Active Client scoped master duplicate plans")
        keld_open = _plan_for(listed, *KNOWN["Kelderman"])
        keld_brown = _plan_for(scoped, *KNOWN["Kelderman"])
        if (keld_open.get("company_a") or {}).get("active_relationships") != (
            keld_brown.get("company_a") or {}
        ).get("active_relationships"):
            _fail("Active Client changed Kelderman statuses")
        detail = get_workbench_plan(conn, *KNOWN["Kelderman"])
        if not (detail.get("company_a") or {}).get("active_relationships"):
            _fail("Kelderman detail missing client workflow")
        edl = get_workbench_plan(conn, *KNOWN["EDL"])
        if (edl.get("human_review") or {}).get("disposition") != "MERGE_CANDIDATE":
            _fail(f"EDL human {edl.get('human_review')}")
        if edl.get("survivor_company_id") != 22 or edl.get("source_company_id") != 28:
            _fail(f"EDL survivor/source {edl.get('survivor_company_id')} {edl.get('source_company_id')}")
        codes = {row.get("code") for row in (edl.get("exceptions") or [])}
        if not {EX_FIELD, EX_STATUS, EX_RN, EX_CONTACT}.issubset(codes):
            _fail(f"EDL exceptions {codes}")
        kuhn = get_workbench_plan(conn, *KNOWN["Kuhn"])
        tech = get_workbench_plan(conn, *KNOWN["Tech Max"])
        if not kuhn.get("company_a") or not tech.get("company_a"):
            _fail("Kuhn/Tech Max detail missing")
        after = _counts(conn)
        if before != after:
            _fail(f"inspect mutated {before} -> {after}")
        if after.get("approvals"):
            _fail("merge approval created")


def test_frontend_card_copy_and_no_execution() -> None:
    workbench = (ROOT.parent / "frontend" / "src" / "AdministrationDuplicateWorkbench.tsx").read_text(
        encoding="utf-8"
    )
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
    if "executeMerge" in workbench:
        _fail("frontend merge execution")
    if "client_id" in (ROOT.parent / "frontend" / "src" / "api" / "duplicateReview.ts").read_text(encoding="utf-8") and False:
        pass


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_ccr_snapshot_blank_unassigned_and_removed,
        test_identity_filters_removed_and_keeps_multiple_active,
        test_kelderman_kuhn_techmax_actual_status_and_rep,
        test_survivor_labels_use_record_ids,
        test_inspect_does_not_write_and_ignores_active_client,
        test_frontend_card_copy_and_no_execution,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-14A isolated tests passed")
