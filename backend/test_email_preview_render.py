"""Focused tests for email template preview rendering (read-only).

Run: python test_email_preview_render.py
Does not send email. Restores any temporary template row it creates.
"""

from __future__ import annotations

import testdb
import sys

from client_email_accounts_data import (
    _clean_meeting_with,
    _crm_first_name,
    list_preview_appointments,
    preview_client_email,
)
from db import get_connection
from models import ClientEmailPreviewRequest


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
    }


def main() -> int:
    client_id = 1
    assert _crm_first_name("Bryan", "Bryan Bigelow") == "Bryan"
    assert _crm_first_name("", "Bryan Bigelow") == "Bryan"
    assert _clean_meeting_with("Heath McBride") == "Heath McBride"
    assert _clean_meeting_with("Rev Spec/Tyler") == ""

    with get_connection() as conn:
        before = _crm_counts(conn)
        bryan = conn.execute(
            """
            SELECT id, first_name, last_name FROM contacts
            WHERE lower(first_name)='bryan' AND lower(last_name) LIKE 'bigelow%'
            LIMIT 1
            """
        ).fetchone()
        jimmy = conn.execute("SELECT id FROM contacts WHERE id = 3593 LIMIT 1").fetchone()
        acct = conn.execute(
            """
            SELECT id FROM client_email_accounts
            WHERE client_id = ? AND active = 1
            ORDER BY is_default DESC, id ASC LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        tmpl = conn.execute(
            """
            SELECT id FROM client_email_templates
            WHERE client_id = ? AND is_active = 1
            ORDER BY id ASC LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        se = conn.execute(
            """
            SELECT id, event_date, event_time FROM client_sales_events
            WHERE client_id = ? AND contact_id = 3593
              AND lower(coalesce(event_type,'')) LIKE 'appointment%'
            ORDER BY event_date DESC, id DESC LIMIT 1
            """,
            (client_id,),
        ).fetchone()

    assert bryan is not None, "Bryan Bigelow contact missing"
    assert acct is not None, "Carmeco sender account missing"
    assert tmpl is not None, "Carmeco template missing"
    contact_id = int(bryan["id"])
    account_id = int(acct["id"])
    template_id = int(tmpl["id"])

    # 1) First-name greeting via legacy [Name]
    r1 = preview_client_email(
        client_id,
        ClientEmailPreviewRequest(
            account_id=account_id,
            contact_id=contact_id,
            template_id=template_id,
        ),
    )
    assert "Hi Bryan," in r1.body or r1.body.startswith("Hi Bryan\n"), r1.body[:120]
    assert "Hi Bryan Bigelow" not in r1.body
    name_res = next(
        (p for p in r1.resolved_placeholders if p.placeholder.lower() == "[name]"),
        None,
    )
    assert name_res and name_res.value == "Bryan", name_res
    assert "UNRESOLVED" in r1.body, "Date/time without appointment must stay UNRESOLVED"
    assert "[First Name]" in r1.supported_placeholders
    assert "[Meeting With]" in r1.supported_placeholders

    # 2) Appointment context from sales event (Jimmy Davis)
    assert jimmy is not None and se is not None, "Need Jimmy + appointment sales event"
    jimmy_id = int(jimmy["id"])
    appts = list_preview_appointments(client_id, jimmy_id)
    assert any(
        a.source == "sales_event" and a.event_id == int(se["id"]) for a in appts
    ), appts
    hit = next(
        a for a in appts if a.event_id == int(se["id"]) and a.source == "sales_event"
    )
    assert hit.appointment_date_raw == (se["event_date"] or "") or hit.appointment_date in (
        se["event_date"] or "",
        "Jul 14, 2026",
    )
    assert hit.meeting_with == ""
    assert "Jul 14" in hit.label or (se["event_date"] or "") in hit.label

    r2 = preview_client_email(
        client_id,
        ClientEmailPreviewRequest(
            account_id=account_id,
            contact_id=jimmy_id,
            template_id=template_id,
            sales_event_id=int(se["id"]),
        ),
    )
    expected_date = se["event_date"] or ""
    assert "Jul 14, 2026" in r2.body or expected_date in r2.body, r2.body[:200]
    if se["event_time"]:
        assert "9:00 AM" in r2.body or se["event_time"] in r2.body

    # 3) [First Name] + [Meeting With] with real participant data (temp template)
    temp_id = None
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO client_email_templates (
                    client_id, template_name, template_type, subject, body,
                    is_active, created_at, updated_at
                ) VALUES (?, ?, '', ?, ?, 1, datetime('now'), datetime('now'))
                """,
                (
                    client_id,
                    "__tmp_preview_render_test__",
                    "Test [First Name]",
                    "Hi [First Name],\nWith [Meeting With] on [Appointment Date] at [Appointment Time].",
                ),
            )
            temp_id = int(cur.lastrowid)
            conn.commit()

        r3 = preview_client_email(
            client_id,
            ClientEmailPreviewRequest(
                account_id=account_id,
                contact_id=contact_id,
                template_id=temp_id,
                sales_event_id=int(se["id"]),
                meeting_with="Heath McBride",
            ),
        )
        assert "Hi Bryan," in r3.body
        assert "Heath McBride" in r3.body
        assert "Jul 14, 2026" in r3.body or expected_date in r3.body
        assert "9:00 AM" in r3.body or (se["event_time"] or "") in r3.body
        assert "UNRESOLVED" not in r3.body or "[Meeting With]" not in r3.body

        r4 = preview_client_email(
            client_id,
            ClientEmailPreviewRequest(
                account_id=account_id,
                contact_id=contact_id,
                template_id=temp_id,
                # no appointment selected
            ),
        )
        assert "Hi Bryan," in r4.body
        assert "UNRESOLVED" in r4.body
        unresolved = {p.placeholder.lower() for p in r4.unresolved_placeholders}
        assert "[appointment date]" in unresolved
        assert "[appointment time]" in unresolved
        assert "[meeting with]" in unresolved
    finally:
        if temp_id is not None:
            with get_connection() as conn:
                conn.execute(
                    "DELETE FROM client_email_templates WHERE id = ? AND client_id = ?",
                    (temp_id, client_id),
                )
                conn.commit()

    with get_connection() as conn:
        after = _crm_counts(conn)
        leftover = conn.execute(
            """
            SELECT COUNT(*) FROM client_email_templates
            WHERE client_id = ? AND template_name = '__tmp_preview_render_test__'
            """,
            (client_id,),
        ).fetchone()[0]
    assert leftover == 0
    assert before == after, (before, after)

    print("PASS: email preview first-name + appointment context + Meeting With")
    print(f"  bryan_contact_id={contact_id}")
    print(f"  jimmy sales_event_id={int(se['id'])} date={se['event_date']}")
    print(f"  crm_counts={after}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL:", exc, file=sys.stderr)
        raise
