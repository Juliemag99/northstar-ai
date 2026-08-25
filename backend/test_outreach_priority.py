"""Tests for Log Outreach + Today's Priority Prospects queue.

Run: python test_outreach_priority.py
Restores CCR fields and deletes activities created by this script.
Does not send email. Does not create appointments/sales events.
"""

from __future__ import annotations

import testdb
import sys
from datetime import date, timedelta

from db import get_connection
from models import OutreachLogRequest
from outreach_data import (
    ACTIVITY_ONLY_OUTCOMES,
    list_priority_prospects,
    log_outreach,
    next_priority_prospect,
)


def _crm_counts(conn) -> dict[str, int]:
    return {
        "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
        "client_campaigns": conn.execute(
            "SELECT COUNT(*) FROM client_campaigns"
        ).fetchone()[0],
        "client_sales_events": conn.execute(
            "SELECT COUNT(*) FROM client_sales_events"
        ).fetchone()[0],
        "client_company_relationships": conn.execute(
            "SELECT COUNT(*) FROM client_company_relationships"
        ).fetchone()[0],
    }


def _delete_test_orphan_activities(conn) -> int:
    rows = conn.execute(
        """
        SELECT activity_id FROM activities
        WHERE created_by = 'Test Outreach'
           OR notes LIKE 'test isolation%'
           OR notes LIKE 'test send information%'
           OR notes LIKE 'test appointment set%'
           OR notes LIKE 'test save and next%'
        """
    ).fetchall()
    ids = [int(r["activity_id"]) for r in rows]
    if ids:
        conn.execute(
            f"DELETE FROM activities WHERE activity_id IN ({','.join('?' * len(ids))})",
            ids,
        )
    return len(ids)


def _pick_ccr(conn, client_id: int, *, prefer_status: str | None = None):
    if prefer_status:
        row = conn.execute(
            """
            SELECT ccr.id, ccr.company_id, ccr.status, ccr.next_action, ccr.follow_up_date,
                   ccr.is_hot, ccr.external_record_no, co.external_record_no AS co_record
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ?
              AND lower(trim(coalesce(ccr.status,''))) = lower(?)
            ORDER BY ccr.id
            LIMIT 1
            """,
            (client_id, prefer_status),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        """
        SELECT ccr.id, ccr.company_id, ccr.status, ccr.next_action, ccr.follow_up_date,
               ccr.is_hot, ccr.external_record_no, co.external_record_no AS co_record
        FROM client_company_relationships ccr
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.client_id = ?
          AND lower(trim(coalesce(ccr.status,''))) NOT IN
              ('closed','current customer','appointment set','completed')
          AND lower(trim(coalesce(ccr.status,''))) NOT LIKE 'disqualified%'
        ORDER BY ccr.id
        LIMIT 1
        """,
        (client_id,),
    ).fetchone()


def _record_no(row) -> str:
    return (row["external_record_no"] or row["co_record"] or "").strip()


def _restore_ccr(conn, snap: dict) -> None:
    if snap.get("kind") == "activity_fu":
        conn.execute(
            """
            UPDATE activities SET follow_up_completed = ? WHERE activity_id = ?
            """,
            (snap["follow_up_completed"], snap["activity_id"]),
        )
        return
    conn.execute(
        """
        UPDATE client_company_relationships
        SET status = ?, next_action = ?, follow_up_date = ?, is_hot = ?
        WHERE id = ?
        """,
        (
            snap["status"],
            snap["next_action"],
            snap["follow_up_date"],
            snap["is_hot"],
            snap["id"],
        ),
    )


def _snap_ccr(row) -> dict:
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "next_action": row["next_action"],
        "follow_up_date": row["follow_up_date"],
        "is_hot": row["is_hot"],
    }


def _cleanup(created_activity_ids: list[int], restores: list[dict]) -> dict[str, int]:
    with get_connection() as conn:
        ids = list({i for i in created_activity_ids if i})
        if ids:
            conn.execute(
                f"DELETE FROM activities WHERE activity_id IN ({','.join('?' * len(ids))})",
                ids,
            )
        _delete_test_orphan_activities(conn)
        seen: set[str] = set()
        for snap in restores:
            key = (
                f"a:{snap['activity_id']}"
                if snap.get("kind") == "activity_fu"
                else f"c:{snap['id']}"
            )
            if key in seen:
                continue
            seen.add(key)
            _restore_ccr(conn, snap)
        conn.commit()
        return _crm_counts(conn)


def main() -> int:
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    today_s = today.isoformat()
    created_activity_ids: list[int] = []
    restores: list[dict] = []
    before: dict[str, int] = {}
    carmeco = None
    due_row = None
    closed_row = None
    appt_row = None
    nurture_row = None
    carmeco_rec = ""
    overdue_id = 0

    with get_connection() as conn:
        removed = _delete_test_orphan_activities(conn)
        if removed:
            print(f"cleaned {removed} orphan Test Outreach activities")
        conn.commit()
        before = _crm_counts(conn)
        carmeco = _pick_ccr(conn, 1)
        brown = _pick_ccr(conn, 2)
        assert carmeco is not None, "Need Carmeco CCR"
        assert brown is not None, "Need Brown CCR"
        carmeco_rec = _record_no(carmeco)
        assert carmeco_rec

        hot_row = _pick_ccr(conn, 1, prefer_status="Hot Prospect") or carmeco
        closed_row = conn.execute(
            """
            SELECT ccr.id, ccr.company_id, ccr.status, ccr.next_action, ccr.follow_up_date,
                   ccr.is_hot, ccr.external_record_no, co.external_record_no AS co_record
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = 1 AND lower(trim(coalesce(ccr.status,''))) = 'closed'
            LIMIT 1
            """
        ).fetchone()
        appt_row = conn.execute(
            """
            SELECT ccr.id, ccr.company_id, ccr.status, ccr.next_action, ccr.follow_up_date,
                   ccr.is_hot, ccr.external_record_no, co.external_record_no AS co_record
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = 1
              AND lower(trim(coalesce(ccr.status,''))) LIKE '%appointment set%'
            LIMIT 1
            """
        ).fetchone()
        nurture_row = conn.execute(
            """
            SELECT ccr.id, ccr.company_id, ccr.status, ccr.next_action, ccr.follow_up_date,
                   ccr.is_hot, ccr.external_record_no, co.external_record_no AS co_record
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = 1
              AND (lower(trim(coalesce(ccr.status,''))) LIKE '%future%'
                   OR lower(trim(coalesce(ccr.status,''))) LIKE '%nurture%')
            LIMIT 1
            """
        ).fetchone()

        for row in (carmeco, brown, hot_row, closed_row, appt_row, nurture_row):
            if row is not None:
                restores.append(_snap_ccr(row))

        overdue_id = int(carmeco["id"])
        conn.execute(
            """
            UPDATE client_company_relationships
            SET status = 'Left Message', follow_up_date = ?, next_action = 'Call back', is_hot = 0
            WHERE id = ?
            """,
            (yesterday, overdue_id),
        )
        due_row = conn.execute(
            """
            SELECT ccr.id, ccr.company_id, ccr.status, ccr.next_action, ccr.follow_up_date,
                   ccr.is_hot, ccr.external_record_no, co.external_record_no AS co_record
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = 1 AND ccr.id != ?
              AND lower(trim(coalesce(ccr.status,''))) NOT IN
                  ('closed','current customer','appointment set','completed')
              AND lower(trim(coalesce(ccr.status,''))) NOT LIKE 'disqualified%'
            ORDER BY ccr.id
            LIMIT 1
            """,
            (overdue_id,),
        ).fetchone()
        assert due_row is not None
        restores.append(_snap_ccr(due_row))
        conn.execute(
            """
            UPDATE client_company_relationships
            SET status = 'Contacted', follow_up_date = ?, next_action = 'Follow up', is_hot = 0
            WHERE id = ?
            """,
            (today_s, int(due_row["id"])),
        )
        if hot_row is not None and int(hot_row["id"]) not in (
            overdue_id,
            int(due_row["id"]),
        ):
            conn.execute(
                """
                UPDATE client_company_relationships
                SET status = 'Hot Prospect', follow_up_date = NULL, next_action = 'Call', is_hot = 1
                WHERE id = ?
                """,
                (int(hot_row["id"]),),
            )
        if nurture_row is not None:
            # CCR date alone must control due — pause open activity follow-ups for this company.
            open_fus = conn.execute(
                """
                SELECT activity_id, follow_up_completed
                FROM activities
                WHERE client_id = 1 AND company_id = ?
                  AND follow_up_at IS NOT NULL AND TRIM(follow_up_at) != ''
                  AND COALESCE(follow_up_completed, 0) = 0
                """,
                (int(nurture_row["company_id"]),),
            ).fetchall()
            for fr in open_fus:
                restores.append(
                    {
                        "kind": "activity_fu",
                        "activity_id": int(fr["activity_id"]),
                        "follow_up_completed": fr["follow_up_completed"],
                    }
                )
            if open_fus:
                conn.execute(
                    """
                    UPDATE activities
                    SET follow_up_completed = 1
                    WHERE client_id = 1 AND company_id = ?
                      AND follow_up_at IS NOT NULL AND TRIM(follow_up_at) != ''
                      AND COALESCE(follow_up_completed, 0) = 0
                    """,
                    (int(nurture_row["company_id"]),),
                )
            conn.execute(
                """
                UPDATE client_company_relationships
                SET status = 'Future/Nurture', follow_up_date = ?, next_action = 'Nurture', is_hot = 0
                WHERE id = ?
                """,
                (tomorrow, int(nurture_row["id"])),
            )
        conn.commit()

    try:
        q1 = list_priority_prospects(1, limit=100)
        assert q1.client_id == 1
        records = [i.external_record_no for i in q1.items]
        buckets = [i.priority_bucket for i in q1.items]
        assert buckets == sorted(buckets), "Buckets must be ordered 1→5"
        assert carmeco_rec in records or any(
            i.priority_bucket == 1 for i in q1.items
        ), "Overdue should appear in queue"
        overdue_items = [i for i in q1.items if i.priority_bucket == 1]
        due_items = [i for i in q1.items if i.priority_bucket == 2]
        if overdue_items and due_items:
            assert records.index(overdue_items[0].external_record_no) < records.index(
                due_items[0].external_record_no
            )

        excluded_statuses = {(i.status or "").strip().lower() for i in q1.items}
        assert "closed" not in excluded_statuses
        assert "current customer" not in excluded_statuses
        assert not any(s.startswith("disqualified") for s in excluded_statuses)
        assert not any("appointment set" in s for s in excluded_statuses)

        if closed_row is not None:
            assert _record_no(closed_row) not in records
        if appt_row is not None:
            assert _record_no(appt_row) not in records

        if nurture_row is not None:
            nurture_rec = _record_no(nurture_row)
            assert nurture_rec not in records, "Future/Nurture not due must be excluded"

            with get_connection() as conn:
                conn.execute(
                    """
                    UPDATE client_company_relationships
                    SET follow_up_date = ?
                    WHERE id = ?
                    """,
                    (yesterday, int(nurture_row["id"])),
                )
                conn.commit()
            q_n = list_priority_prospects(1, limit=100)
            assert nurture_rec in [i.external_record_no for i in q_n.items], (
                "Future/Nurture returns when due"
            )

        q2 = list_priority_prospects(2, limit=50)
        assert all(i.client_id == 2 for i in q2.items)

        with get_connection() as conn:
            status_before = conn.execute(
                "SELECT status FROM client_company_relationships WHERE id = ?",
                (overdue_id,),
            ).fetchone()["status"]

        r_no_answer = log_outreach(
            OutreachLogRequest(
                client_id=1,
                external_record_no=carmeco_rec,
                outreach_type="Call",
                outcome="No Answer",
                notes="test isolation no answer",
                next_action="Retry call",
                follow_up_date=tomorrow,
                return_next=False,
                created_by="Test Outreach",
            )
        )
        created_activity_ids.append(int(r_no_answer.activity_id))
        if r_no_answer.follow_up_activity_id:
            created_activity_ids.append(int(r_no_answer.follow_up_activity_id))
        assert r_no_answer.ok
        assert r_no_answer.client_id == 1
        assert not r_no_answer.status_updated
        assert r_no_answer.applied_status == ""
        assert not r_no_answer.open_email_compose
        assert "no answer" in ACTIVITY_ONLY_OUTCOMES

        with get_connection() as conn:
            status_after = conn.execute(
                """
                SELECT status, next_action, follow_up_date
                FROM client_company_relationships WHERE id = ?
                """,
                (overdue_id,),
            ).fetchone()
            assert status_after["status"] == status_before
            assert status_after["follow_up_date"] == tomorrow
            act = conn.execute(
                """
                SELECT client_id, activity_type, outcome, notes
                FROM activities WHERE activity_id = ?
                """,
                (r_no_answer.activity_id,),
            ).fetchone()
            assert act["client_id"] == 1
            assert act["activity_type"] == "Call"
            assert act["outcome"] == "No Answer"
            brown_acts = conn.execute(
                """
                SELECT COUNT(*) FROM activities
                WHERE client_id = 2 AND notes LIKE 'test isolation%'
                """
            ).fetchone()[0]
            assert brown_acts == 0
            statuses = [
                r[0]
                for r in conn.execute(
                    """
                    SELECT DISTINCT status FROM client_company_relationships
                    WHERE client_id = 1
                    """
                ).fetchall()
            ]
            assert "No Answer" not in statuses
            assert "Spoke With Contact" not in statuses
            fu = conn.execute(
                """
                SELECT activity_id, activity_type, follow_up_at
                FROM activities WHERE activity_id = ?
                """,
                (r_no_answer.follow_up_activity_id,),
            ).fetchone()
            assert fu is not None
            assert fu["activity_type"] == "Follow-Up"

        sales_before = before["client_sales_events"]

        r_send = log_outreach(
            OutreachLogRequest(
                client_id=1,
                external_record_no=carmeco_rec,
                outreach_type="Email",
                outcome="Send Information",
                notes="test send information",
                return_next=False,
                created_by="Test Outreach",
            )
        )
        created_activity_ids.append(int(r_send.activity_id))
        assert r_send.open_email_compose is True
        assert r_send.status_updated is True
        assert "send information" in (r_send.applied_status or "").lower()
        with get_connection() as conn:
            email_sent = conn.execute(
                """
                SELECT COUNT(*) FROM activities
                WHERE client_id = 1 AND company_id = ?
                  AND lower(coalesce(outcome,'')) LIKE '%email sent%'
                  AND activity_id IN ({})
                """.format(",".join("?" * len(created_activity_ids))),
                (int(carmeco["company_id"]), *created_activity_ids),
            ).fetchone()[0]
            assert email_sent == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM client_sales_events").fetchone()[0]
                == sales_before
            )

        r_appt = log_outreach(
            OutreachLogRequest(
                client_id=1,
                external_record_no=carmeco_rec,
                outreach_type="Call",
                outcome="Appointment Set",
                notes="test appointment set",
                return_next=True,
                created_by="Test Outreach",
            )
        )
        created_activity_ids.append(int(r_appt.activity_id))
        assert r_appt.open_appointment_workflow is True
        assert r_appt.status_updated is True
        with get_connection() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM client_sales_events").fetchone()[0]
                == sales_before
            )

        q_after_appt = list_priority_prospects(1, limit=100)
        assert carmeco_rec not in [i.external_record_no for i in q_after_appt.items]

        due_rec = _record_no(due_row)
        nxt = next_priority_prospect(1, after_record_no=due_rec)
        q_full = list_priority_prospects(1, limit=200)
        if due_rec in [i.external_record_no for i in q_full.items]:
            idx = [i.external_record_no for i in q_full.items].index(due_rec)
            expected = (
                q_full.items[idx + 1].external_record_no
                if idx + 1 < len(q_full.items)
                else None
            )
            assert (nxt.external_record_no if nxt else None) == expected
        if nxt:
            assert nxt.client_id == 1

        r_next = log_outreach(
            OutreachLogRequest(
                client_id=1,
                external_record_no=due_rec,
                outreach_type="Call",
                outcome="Spoke With Contact",
                notes="test save and next",
                return_next=True,
                created_by="Test Outreach",
            )
        )
        created_activity_ids.append(int(r_next.activity_id))
        assert r_next.client_id == 1
        assert not r_next.status_updated
        if r_next.next_external_record_no:
            assert r_next.next_external_record_no != due_rec

        after = _cleanup(created_activity_ids, restores)
        created_activity_ids.clear()
        restores.clear()

        assert after["companies"] == before["companies"]
        assert after["contacts"] == before["contacts"]
        assert after["client_campaigns"] == before["client_campaigns"]
        assert after["client_sales_events"] == before["client_sales_events"]
        assert after["client_company_relationships"] == before[
            "client_company_relationships"
        ]
        assert after["activities"] == before["activities"], (
            f"activities {before['activities']} -> {after['activities']}"
        )

        print("PASS: outreach isolation, activity, follow-up, queue, exclusions, save&next")
        print("DATA COUNTS (restored):", after)
        return 0
    finally:
        if created_activity_ids or restores:
            _cleanup(created_activity_ids, restores)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL:", exc, file=sys.stderr)
        raise
