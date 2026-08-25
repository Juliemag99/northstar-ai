"""Controlled Next Action catalog — one backend source, no production rewrites.

Run: python test_next_actions.py

Creates temporary company/contact/CCR rows and a fake client catalog override.
Does not change Flora Jia or other existing CRM records.
"""

from __future__ import annotations

import testdb
import json
import sys
import time
import urllib.error
import urllib.request

from client_workspace_data import (
    create_contact_activity,
    ensure_contact_workflow_schema,
    get_contact_workspace,
    list_relationship_statuses,
    update_contact_workflow,
)
from db import get_connection
from models import ContactActivityCreate, ContactWorkflowUpdate
from next_actions import (
    canonicalize_next_action,
    display_next_action,
    ensure_next_action_schema,
    list_next_action_choices,
    match_next_action,
    status_defaults_no_next_action,
)

FLORA_ID = 4631
API = "http://127.0.0.1:8007"
MARKER = "NSNXACT"
FAKE_CLIENT_ID = 989991


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _snapshot_flora(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, alt_phone, email,
               external_record_no, source_row_index
        FROM contacts
        WHERE id = ? OR (first_name = 'Flora' AND last_name = 'Jia')
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, id
        LIMIT 1
        """,
        (FLORA_ID, FLORA_ID),
    ).fetchone()
    return dict(row) if row is not None else None


def _ccr_snapshot(conn, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, status, assigned_user_id, next_action, follow_up_date
            FROM client_company_relationships
            WHERE company_id = ?
            ORDER BY client_id
            """,
            (company_id,),
        ).fetchall()
    ]


def _cleanup(company_id: int | None, contact_id: int | None, activity_ids: list[int]) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM client_next_actions WHERE client_id = ?", (FAKE_CLIENT_ID,))
        for aid in activity_ids:
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))
        if contact_id:
            conn.execute("DELETE FROM contact_client_workflows WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM work_queue_items WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        if company_id:
            conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (company_id,))
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (company_id,),
            )
            conn.execute("DELETE FROM activities WHERE company_id = ?", (company_id,))
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (company_id,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        leftover = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        for row in leftover:
            cid = int(row["id"])
            conn.execute("DELETE FROM work_queue_items WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM activities WHERE company_id = ?", (cid,))
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (cid,),
            )
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (cid,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.commit()


def _http_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    return testdb.http_json(method, path, body)


def main() -> int:
    ensure_contact_workflow_schema()
    ensure_next_action_schema()
    company_id: int | None = None
    contact_id: int | None = None
    activity_ids: list[int] = []
    flora_before: dict | None = None
    flora_company_id: int | None = None
    flora_ccr_before: list[dict] = []
    flora_next_before = ""

    try:
        with get_connection() as conn:
            flora_before = _snapshot_flora(conn)
            if flora_before is not None:
                flora_company_id = int(flora_before["company_id"])
                flora_ccr_before = _ccr_snapshot(conn, flora_company_id)
                flora_wf = conn.execute(
                    """
                    SELECT next_action FROM contact_client_workflows
                    WHERE contact_id = ?
                    """,
                    (int(flora_before["id"]),),
                ).fetchone()
                flora_next_before = _blank(flora_wf["next_action"]) if flora_wf is not None else ""
            clients = {
                str(r["code"]): int(r["id"])
                for r in conn.execute("SELECT id, code FROM clients").fetchall()
            }
            carmeco_id = clients.get("carmeco")
            brown_id = clients.get("brown")
            if not carmeco_id or not brown_id:
                _fail("Need both carmeco and brown clients.")
            julie = conn.execute(
                "SELECT id FROM users WHERE lower(full_name) = 'julie magnani' AND active = 1"
            ).fetchone()
            if julie is None:
                _fail("Julie Magnani user not found.")
            julie_id = int(julie["id"])
            stamp = str(int(time.time()))
            record_no = f"{MARKER}-{stamp}"
            cur = conn.execute(
                """
                INSERT INTO companies (external_record_no, company_name, city, state)
                VALUES (?, ?, 'Testville', 'OH')
                """,
                (record_no, f"{MARKER} Next Co {stamp}"),
            )
            company_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'New', 'call')
                """,
                (carmeco_id, company_id, record_no),
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'Existing Brown Status', '')
                """,
                (brown_id, company_id, f"{record_no}-B"),
            )
            cur = conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no, first_name, last_name, title,
                    source_row_index
                ) VALUES (?, ?, 'NextAct', 'Tester', 'Buyer', 1)
                """,
                (company_id, record_no),
            )
            contact_id = int(cur.lastrowid)
            conn.commit()

        if contact_id == FLORA_ID or (flora_before and contact_id == int(flora_before["id"])):
            _fail("Refusing to use Flora Jia as test contact.")

        choices = list_next_action_choices(client_id=carmeco_id)
        labels = [item["label"] for item in choices]
        expected = [
            "Call",
            "Send Email",
            "Follow-Up",
            "Send Information",
            "Research Company",
            "Find Contact Name",
            "Find Phone Number",
            "Schedule Meeting",
            "Prepare Quote",
            "No Next Action",
            "Other",
        ]
        if labels != expected:
            _fail(f"Default catalog mismatch: {labels}")
        other = next(item for item in choices if item["code"] == "other")
        if not other["requires_detail"]:
            _fail("Other must require custom text.")

        call_choice, call_custom = match_next_action("call", client_id=carmeco_id)
        if call_choice is None or call_choice["label"] != "Call" or call_custom:
            _fail('"call" should map to Call without losing the stored value until save.')
        assert canonicalize_next_action("call", client_id=carmeco_id) == "Call"
        assert display_next_action("call", client_id=carmeco_id) == "Call"

        other_choice, other_custom = match_next_action("Call back", client_id=carmeco_id)
        if other_choice is None or other_choice["code"] != "other" or other_custom != "Call back":
            _fail("Unknown free text must remain available as Other custom text.")
        assert canonicalize_next_action("Call back", client_id=carmeco_id) == "Call back"

        try:
            canonicalize_next_action("Other", client_id=carmeco_id)
            _fail("Other without custom text should be rejected.")
        except ValueError:
            pass

        if not status_defaults_no_next_action("Closed"):
            _fail("Closed should default to No Next Action.")
        if not status_defaults_no_next_action("Do Not Call"):
            _fail("Do Not Call should default to No Next Action.")
        if not status_defaults_no_next_action("Disqualified - No Fit"):
            _fail("Disqualified should default to No Next Action.")
        if status_defaults_no_next_action("Left Message"):
            _fail("Working statuses must not force No Next Action.")
        assert canonicalize_next_action("", client_id=carmeco_id, status="Closed") == "No Next Action"

        with get_connection() as conn:
            stored_call = conn.execute(
                """
                SELECT next_action FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (carmeco_id, company_id),
            ).fetchone()
        if _blank(stored_call["next_action"]) != "call":
            _fail("Existing free-text next_action was rewritten before a user save.")

        statuses = list_relationship_statuses(client_id=carmeco_id)
        status = next((s for s in statuses if s.strip() and "closed" not in s.lower()), "New")
        saved = update_contact_workflow(
            contact_id,
            ContactWorkflowUpdate(
                client_id=carmeco_id,
                status=status,
                assigned_user_id=julie_id,
                next_action="call",
                user="Julie Magnani",
            ),
        )
        ws = saved["workspace"]
        assert ws.next_action == "Call"
        assert ws.status == status

        custom_saved = update_contact_workflow(
            contact_id,
            ContactWorkflowUpdate(
                client_id=carmeco_id,
                status=status,
                next_action="Review drawings",
                user="Julie Magnani",
            ),
        )
        assert custom_saved["workspace"].next_action == "Review drawings"

        closed_status = next(
            (
                s
                for s in statuses
                if s.lower() == "closed"
                or s.lower().startswith("closed")
                or "do not call" in s.lower()
                or "disqualified" in s.lower()
            ),
            None,
        )
        if closed_status:
            closed_saved = update_contact_workflow(
                contact_id,
                ContactWorkflowUpdate(
                    client_id=carmeco_id,
                    status=closed_status,
                    next_action="",
                    user="Julie Magnani",
                ),
            )
            assert closed_saved["workspace"].next_action == "No Next Action"
            assert closed_saved["workspace"].status == closed_status

            keep_call = update_contact_workflow(
                contact_id,
                ContactWorkflowUpdate(
                    client_id=carmeco_id,
                    status=closed_status,
                    next_action="Call",
                    user="Julie Magnani",
                ),
            )
            assert keep_call["workspace"].next_action == "Call", "User must be able to override the Closed default."

        follow = create_contact_activity(
            contact_id,
            ContactActivityCreate(
                client_id=carmeco_id,
                activity_type="Follow-Up",
                notes=f"{MARKER} schedule",
                next_action="follow up",
                follow_up_date="2026-09-01",
                follow_up_time="09:00",
                assigned_user_id=julie_id,
                created_by="Julie Magnani",
            ),
        )
        activity_ids.append(int(follow["activity_id"]))
        if follow.get("follow_up_activity_id"):
            activity_ids.append(int(follow["follow_up_activity_id"]))
        assert follow["workspace"].next_action == "Follow-Up"

        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO client_next_actions (
                    client_id, code, label, sort_order, active, requires_detail, aliases
                ) VALUES (?, 'plant_visit', 'Plant Visit', 0, 1, 0, '["plant visit"]')
                """,
                (FAKE_CLIENT_ID,),
            )
            conn.commit()
        override = list_next_action_choices(client_id=FAKE_CLIENT_ID)
        if [item["label"] for item in override] != ["Plant Visit"]:
            _fail("Client-specific catalog should replace the global defaults.")
        brown_choices = list_next_action_choices(client_id=brown_id)
        if [item["label"] for item in brown_choices] != expected:
            _fail("Other clients must keep the shared default catalog.")

        with get_connection() as conn:
            brown_ccr = conn.execute(
                """
                SELECT status, next_action FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (brown_id, company_id),
            ).fetchone()
        assert _blank(brown_ccr["status"]) == "Existing Brown Status"
        assert _blank(brown_ccr["next_action"]) == ""

        http_ok = False
        try:
            code, body = _http_json("GET", f"/api/clients/{carmeco_id}/next-actions")
            if code != 200:
                _fail(f"HTTP GET next-actions failed ({code}): {body}")
            http_labels = [item.get("label") for item in body.get("choices") or []]
            if http_labels != expected:
                _fail(f"HTTP catalog mismatch: {http_labels}")
            if body.get("default_for_closed_code") != "no_next_action":
                _fail("HTTP catalog missing closed default.")
            code, ws_body = _http_json(
                "PATCH",
                f"/api/contacts/{contact_id}/workflow",
                {
                    "client_id": carmeco_id,
                    "status": status,
                    "next_action": "send email",
                    "user": "Julie Magnani",
                },
            )
            if code != 200:
                _fail(f"HTTP PATCH workflow failed ({code}): {ws_body}")
            if (ws_body.get("workspace") or {}).get("next_action") != "Send Email":
                _fail("HTTP save did not canonicalize send email → Send Email.")
            http_ok = True
        except ConnectionError:
            print(f"API not reachable at {API}; data-layer checks still passed.")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            flora_ccr_after = (
                _ccr_snapshot(conn, flora_company_id) if flora_company_id else []
            )
            flora_wf_after = None
            if flora_before is not None:
                flora_wf_after = conn.execute(
                    """
                    SELECT next_action FROM contact_client_workflows
                    WHERE contact_id = ?
                    """,
                    (int(flora_before["id"]),),
                ).fetchone()
        if flora_before is not None:
            assert flora_after == flora_before, "Flora Jia contact row changed."
            assert flora_ccr_after == flora_ccr_before, "Flora Jia company CCR changed."
            flora_next_after = (
                _blank(flora_wf_after["next_action"]) if flora_wf_after is not None else ""
            )
            assert flora_next_after == flora_next_before, "Flora Jia next action was rewritten."

        print("PASS: Next Action catalog is centralized, maps call to Call, keeps Other text,")
        print("      defaults Closed/DNC/disqualified to No Next Action, and is client-overridable.")
        if http_ok:
            print("PASS: live HTTP GET /next-actions and PATCH workflow on :8007.")
        return 0
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"FAIL: {exc}")
        return 1
    finally:
        _cleanup(company_id, contact_id, activity_ids)
        with get_connection() as conn:
            leftover = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
                (f"{MARKER} %", f"{MARKER}-%"),
            ).fetchone()["n"]
            leftover_choices = conn.execute(
                "SELECT COUNT(*) AS n FROM client_next_actions WHERE client_id = ?",
                (FAKE_CLIENT_ID,),
            ).fetchone()["n"]
            if leftover or leftover_choices:
                print(f"WARN: leftover test rows companies={leftover} catalog={leftover_choices}.")


if __name__ == "__main__":
    sys.exit(main())
