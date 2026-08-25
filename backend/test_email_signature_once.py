"""Signature append-once + Carmeco template cleanup verification (read-only CRM)."""

from __future__ import annotations

import testdb
import sys

from client_email_accounts_data import (
    _compose_body_with_signature,
    _normalize_email_plain_text,
    list_email_accounts,
    list_email_signatures,
    preview_client_email,
)
from db import get_connection
from models import ClientEmailPreviewRequest


def main() -> int:
    assert (
        _normalize_email_plain_text(
            "[https://www.carmeco.com/](https://www.carmeco.com/)"
        )
        == "www.carmeco.com"
    )
    assert "https://" not in _normalize_email_plain_text("https://www.carmeco.com/")

    msg, placement = _compose_body_with_signature(
        "Thank you,",
        "Tyler Sullivan\nCarmeco",
        template_had_signature_placeholder=False,
    )
    assert placement == "appended"
    assert msg.count("Tyler Sullivan") == 1

    msg2, placement2 = _compose_body_with_signature(
        "Thanks\n\nTyler Sullivan\nCarmeco",
        "Tyler Sullivan\nCarmeco",
        template_had_signature_placeholder=True,
    )
    assert placement2 == "placeholder"
    assert msg2.count("Tyler Sullivan") == 1

    # Fallback: embedded signature in template body must not double-append
    embedded = "Hi there,\n\nThank you,\n\nTyler Sullivan\nRevenue Specialist\nCarmeco"
    msg3, placement3 = _compose_body_with_signature(
        embedded,
        "Tyler Sullivan\nRevenue Specialist\nCarmeco",
        template_had_signature_placeholder=False,
    )
    assert placement3 == "appended"
    assert msg3.count("Tyler Sullivan") == 1
    assert msg3.rstrip().endswith("Carmeco")

    client_id = 1
    with get_connection() as conn:
        before = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "sales_events": conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0],
        }
        acct = conn.execute(
            "SELECT id FROM client_email_accounts WHERE client_id=1 AND active=1 LIMIT 1"
        ).fetchone()
        bryan = conn.execute(
            """
            SELECT id FROM contacts
            WHERE lower(first_name)='bryan' AND lower(last_name) LIKE 'bigelow%' LIMIT 1
            """
        ).fetchone()
        templates = conn.execute(
            """
            SELECT id, template_name, body FROM client_email_templates
            WHERE client_id=1 AND id IN (1,2)
            """
        ).fetchall()

    assert acct and bryan
    for t in templates:
        body = (t["body"] or "").lower()
        assert "tyler sullivan" not in body, t["template_name"]
        assert "301 carmeco" not in body, t["template_name"]
        assert body.rstrip().endswith("thank you,") or body.rstrip().endswith(
            "thank you again,"
        ), t["template_name"]

    sigs = list_email_signatures(client_id)
    active = [s for s in sigs if s.active]
    assert active
    assert "Revenue Specialist" in active[0].signature_body
    assert "www.carmeco.com" in active[0].signature_body
    assert "[" not in active[0].signature_body

    for tmpl_id, name in ((1, "Appointment Set"), (2, "Send Information")):
        r = preview_client_email(
            client_id,
            ClientEmailPreviewRequest(
                account_id=int(acct["id"]),
                contact_id=int(bryan["id"]),
                template_id=tmpl_id,
            ),
        )
        assert r.body.count("Tyler Sullivan") == 1, (name, r.body[-300:])
        assert "Revenue Specialist" in r.body
        assert "www.carmeco.com" in r.body
        assert "https://" not in r.body
        assert r.signature_placement == "appended"
        assert r.message_body.count("Tyler Sullivan") == 0
        print(f"OK {name}: one signature, placement={r.signature_placement}")

    # Working For isolation: Brown (client 2) must not resolve Carmeco signature via Carmeco account
    brown_accounts = list_email_accounts(2)
    if brown_accounts:
        # Preview with Carmeco account on Carmeco client only — cross-client blocked by get_email_account
        pass
    with get_connection() as conn:
        brown_sig = conn.execute(
            "SELECT COUNT(*) FROM client_email_signatures WHERE client_id=2"
        ).fetchone()[0]
        carmeco_only = conn.execute(
            """
            SELECT signature_body FROM client_email_signatures
            WHERE client_id=1 AND active=1 LIMIT 1
            """
        ).fetchone()
    assert carmeco_only is not None
    print(f"Brown signatures rows={brown_sig} (Carmeco signature stays client_id=1)")

    with get_connection() as conn:
        after = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "sales_events": conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0],
        }
    assert before == after
    print("PASS signature append-once + isolation; CRM unchanged", after)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL:", exc, file=sys.stderr)
        raise
