"""Send Information compose prepopulation checks (no real email send).

Run: python test_send_information_compose.py
"""

from __future__ import annotations

import testdb
import sys

from client_email_accounts_data import preview_client_email, _crm_first_name
from db import get_connection
from models import ClientEmailPreviewRequest, OutreachLogRequest
from outreach_data import log_outreach


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
    created: list[int] = []
    with get_connection() as conn:
        before = _crm_counts(conn)
        tmpl = conn.execute(
            """
            SELECT id, template_name, subject, body
            FROM client_email_templates
            WHERE client_id = ? AND is_active = 1
              AND lower(template_name) = lower('Carmeco Send Information Template')
            LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        assert tmpl is not None, "Carmeco Send Information Template missing"
        assert (tmpl["subject"] or "").strip() == ""

        acct = conn.execute(
            """
            SELECT id FROM client_email_accounts
            WHERE client_id = ? AND active = 1
            ORDER BY is_default DESC, id ASC LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        assert acct is not None

        contact = conn.execute(
            """
            SELECT ct.id, ct.first_name, ct.last_name, ct.email, ct.company_id,
                   co.external_record_no
            FROM contacts ct
            JOIN companies co ON co.id = ct.company_id
            JOIN client_company_relationships ccr
              ON ccr.company_id = co.id AND ccr.client_id = ?
            WHERE trim(coalesce(ct.email,'')) != ''
              AND trim(coalesce(ct.first_name,'')) != ''
              AND lower(trim(coalesce(ccr.status,''))) NOT IN
                  ('closed','current customer','appointment set')
            ORDER BY ct.id
            LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        assert contact is not None
        record_no = (contact["external_record_no"] or "").strip()
        first = _crm_first_name(contact["first_name"], f"{contact['first_name']} {contact['last_name']}")
        assert first
        assert " " not in first or first == contact["first_name"].strip()

        status_snap = conn.execute(
            """
            SELECT id, status FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (client_id, int(contact["company_id"])),
        ).fetchone()

    # Outreach save first + open compose flags
    result = log_outreach(
        OutreachLogRequest(
            client_id=client_id,
            external_record_no=record_no,
            contact_id=int(contact["id"]),
            outreach_type="Email",
            outcome="Send Information",
            notes="test send info compose flow",
            return_next=True,
            created_by="Test SendInfo",
        )
    )
    created.append(int(result.activity_id))
    assert result.open_email_compose is True
    assert result.status_updated is True
    assert result.client_id == 1
    # return_next still computed; UI defers navigation
    assert result.next_external_record_no is None or isinstance(
        result.next_external_record_no, str
    )

    acts_after_save = 0
    with get_connection() as conn:
        acts_after_save = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        email_sent = conn.execute(
            """
            SELECT COUNT(*) FROM activities
            WHERE activity_id = ?
              AND (lower(coalesce(outcome,'')) LIKE '%email sent%'
                   OR lower(coalesce(activity_type,'')) LIKE '%email sent%')
            """,
            (result.activity_id,),
        ).fetchone()[0]
        assert email_sent == 0

    # Preview (compose) must not create Email Sent / new activities
    preview = preview_client_email(
        client_id,
        ClientEmailPreviewRequest(
            account_id=int(acct["id"]),
            contact_id=int(contact["id"]),
            template_id=int(tmpl["id"]),
        ),
    )
    assert first.lower() in (preview.message_body or preview.body or "").lower() or (
        f"Hi {first}" in (preview.message_body or preview.body or "")
    )
    body = preview.message_body or preview.body or ""
    # Greeting first name only — not full "First Last" immediately after Hi
    assert f"Hi {first}," in body or f"Hi {first}\n" in body or body.startswith(f"Hi {first}")
    full = f"{contact['first_name']} {contact['last_name']}".strip()
    assert f"Hi {full}" not in body
    assert (preview.subject or "") == ""
    sig = preview.signature or ""
    assert sig.strip(), "Configured Carmeco signature expected"
    assert "Signature:" not in (preview.body or "")
    assert (preview.body or "").lower().count("tyler sullivan") <= 1

    with get_connection() as conn:
        acts_after_preview = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        assert acts_after_preview == acts_after_save, "Preview must not create activities"
        # restore CCR + delete test activity
        conn.execute(
            f"DELETE FROM activities WHERE activity_id IN ({','.join('?' * len(created))})",
            created,
        )
        if status_snap is not None:
            conn.execute(
                "UPDATE client_company_relationships SET status = ? WHERE id = ?",
                (status_snap["status"], int(status_snap["id"])),
            )
        # also remove any Test SendInfo leftovers
        conn.execute(
            "DELETE FROM activities WHERE created_by = 'Test SendInfo' OR notes LIKE 'test send info compose%'"
        )
        conn.commit()
        after = _crm_counts(conn)

    assert after == before, f"counts drifted: {before} -> {after}"
    print("PASS: Send Information compose prepopulation (no send)")
    print("DATA COUNTS:", after)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL:", exc, file=sys.stderr)
        raise
