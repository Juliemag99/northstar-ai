"""Focused tests for Preview Email appointment selector + rendering (read-only)."""

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
    assert _clean_meeting_with("Rev Spec/Tyler") == ""
    assert _clean_meeting_with("Heath McBride") == "Heath McBride"

    with get_connection() as conn:
        before = _crm_counts(conn)
        bryan_id = int(
            conn.execute(
                """
                SELECT id FROM contacts
                WHERE lower(first_name)='bryan' AND lower(last_name) LIKE 'bigelow%'
                LIMIT 1
                """
            ).fetchone()["id"]
        )
        jimmy_id = 3593
        acct_id = int(
            conn.execute(
                """
                SELECT id FROM client_email_accounts
                WHERE client_id=? AND active=1
                ORDER BY is_default DESC, id ASC LIMIT 1
                """,
                (client_id,),
            ).fetchone()["id"]
        )
        tmpl_id = int(
            conn.execute(
                """
                SELECT id FROM client_email_templates
                WHERE client_id=? AND is_active=1 ORDER BY id ASC LIMIT 1
                """,
                (client_id,),
            ).fetchone()["id"]
        )
        se15 = conn.execute(
            """
            SELECT id, event_date, event_time, meeting_type, event_type
            FROM client_sales_events WHERE id=15 AND client_id=1
            """
        ).fetchone()

    # Jimmy / AAON options
    jimmy_opts = list_preview_appointments(client_id, jimmy_id)
    labels = [o.label for o in jimmy_opts]
    types = {o.event_type for o in jimmy_opts}
    assert len(jimmy_opts) >= 3, labels
    assert any("Site Visit" in o.label for o in jimmy_opts), labels
    assert any("Rescheduled" in o.label for o in jimmy_opts), labels
    assert any("Google Meet" in o.label for o in jimmy_opts), labels
    assert "Send Information" not in types
    assert all(o.source == "sales_event" for o in jimmy_opts)
    print("Jimmy options:")
    for o in jimmy_opts:
        print(f"  - {o.label}")

    # Bryan: none
    bryan_opts = list_preview_appointments(client_id, bryan_id)
    assert bryan_opts == [], bryan_opts
    print("Bryan options: (none)")

    # First name
    r1 = preview_client_email(
        client_id,
        ClientEmailPreviewRequest(
            account_id=acct_id, contact_id=bryan_id, template_id=tmpl_id
        ),
    )
    assert "Hi Bryan," in r1.body or r1.body.startswith("Hi Bryan")
    assert "Hi Bryan Bigelow" not in r1.body

    # Date/time placeholders from sales event 15
    assert se15 is not None
    r2 = preview_client_email(
        client_id,
        ClientEmailPreviewRequest(
            account_id=acct_id,
            contact_id=jimmy_id,
            template_id=tmpl_id,
            sales_event_id=int(se15["id"]),
        ),
    )
    assert "Jul 14, 2026" in r2.body, r2.body[:250]
    assert "9:00 AM CDT" in r2.body or "9:00 AM" in r2.body, r2.body[:250]
    mw_unresolved = [
        p
        for p in r2.unresolved_placeholders
        if p.placeholder.lower() == "[meeting with]"
    ]
    # Template does not use [Meeting With]; verify via temp template
    temp_id = None
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO client_email_templates (
                    client_id, template_name, template_type, subject, body,
                    is_active, created_at, updated_at
                ) VALUES (?, '__tmp_appt_selector_test__', '', 't',
                    'Hi [First Name], meet [Meeting With] on [Appointment Date] at [Appointment Time].',
                    1, datetime('now'), datetime('now'))
                """,
                (client_id,),
            )
            temp_id = int(cur.lastrowid)
            conn.commit()
        r3 = preview_client_email(
            client_id,
            ClientEmailPreviewRequest(
                account_id=acct_id,
                contact_id=jimmy_id,
                template_id=temp_id,
                sales_event_id=int(se15["id"]),
            ),
        )
        assert "Hi Jimmy," in r3.body
        assert "Jul 14, 2026" in r3.body
        assert "9:00 AM" in r3.body
        assert "[Meeting With]" in r3.body and "UNRESOLVED" in r3.body
        assert "Heath" not in r3.body and "Joe" not in r3.body
    finally:
        if temp_id is not None:
            with get_connection() as conn:
                conn.execute(
                    "DELETE FROM client_email_templates WHERE id=? AND client_id=?",
                    (temp_id, client_id),
                )
                conn.commit()

    with get_connection() as conn:
        after = _crm_counts(conn)
    assert before == after, (before, after)

    print("PASS: appointment selector + first name + date/time + meeting with")
    print(f"  crm_counts={after}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL:", exc, file=sys.stderr)
        raise
