"""Idempotent Carmeco template signature strip + preview verification."""

from __future__ import annotations

import testdb
import re

from access import get_default_user
from client_email_accounts_data import preview_client_email
from client_knowledge_data import list_email_templates, upsert_email_template
from db import DB_PATH, get_connection
from models import ClientEmailPreviewRequest, ClientEmailTemplateUpdate

SIG_MARKERS = (
    "tyler sullivan",
    "revenue specialist",
    "(417) 221-5834",
    "301 carmeco road",
    "www.carmeco.com",
    "https://www.carmeco.com",
)


def _has_embedded_sig(body: str) -> dict[str, bool]:
    low = (body or "").lower()
    return {m: m in low for m in SIG_MARKERS}


def _strip_hardcoded_block(body: str) -> str:
    text = (body or "").replace("\r\n", "\n").rstrip()
    # Cut from last Thank you / Thank you again before Tyler block.
    patterns = [
        r"(?is)(\nThank you again,)\s*\n\s*Tyler Sullivan\b.*$",
        r"(?is)(\nThank you,)\s*\n\s*Tyler Sullivan\b.*$",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return text[: m.start(1)].rstrip() + "\n\n" + m.group(1).lstrip("\n") + "\n"
    # Generic: drop from last Tyler Sullivan if near end after thank you
    low = text.lower()
    idx = low.rfind("\ntyler sullivan")
    if idx > 0:
        head = text[:idx].rstrip()
        last = head.split("\n")[-1].strip().lower()
        if last in {"thank you,", "thank you again,"}:
            return head + "\n"
    return text + ("\n" if text and not text.endswith("\n") else "")


def main() -> None:
    print("DB", DB_PATH)
    client_id = 1
    uid = int(get_default_user().id)
    with get_connection() as conn:
        before_crm = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "sales_events": conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0],
        }
        acct = conn.execute(
            "SELECT id FROM client_email_accounts WHERE client_id=1 AND active=1 LIMIT 1"
        ).fetchone()["id"]
        bryan = conn.execute(
            """
            SELECT id FROM contacts
            WHERE lower(first_name)='bryan' AND lower(last_name) LIKE 'bigelow%'
            LIMIT 1
            """
        ).fetchone()["id"]
        sig = conn.execute(
            """
            SELECT signature_body FROM client_email_signatures
            WHERE client_id=1 AND active=1 ORDER BY is_default DESC, id ASC LIMIT 1
            """
        ).fetchone()["signature_body"]

    print("=== BEFORE (stored bodies) ===")
    templates = [t for t in list_email_templates(client_id) if t.template_id in (1, 2)]
    before_flags = {}
    for t in templates:
        flags = _has_embedded_sig(t.body or "")
        before_flags[t.template_id] = flags
        print(f"ID {t.template_id} {t.template_name}")
        print("  markers:", flags)
        print("  tail:", repr((t.body or "")[-100:]))

    changed: list[int] = []
    for t in templates:
        cleaned = _strip_hardcoded_block(t.body or "")
        if cleaned.replace("\r\n", "\n").rstrip() != (t.body or "").replace(
            "\r\n", "\n"
        ).rstrip():
            upsert_email_template(
                client_id,
                ClientEmailTemplateUpdate(
                    template_name=t.template_name,
                    template_type=t.template_type,
                    subject=t.subject,
                    body=cleaned,
                    is_active=t.is_active,
                    source_document_id=t.source_document_id,
                    source_proposal_ids=list(t.source_proposal_ids or []),
                ),
                template_id=t.template_id,
            )
            changed.append(t.template_id)
            print(f"UPDATED template {t.template_id}")
        else:
            print(f"NO BODY CHANGE needed for template {t.template_id}")

    print("=== AFTER (stored bodies) ===")
    after_templates = [
        t for t in list_email_templates(client_id) if t.template_id in (1, 2)
    ]
    for t in after_templates:
        flags = _has_embedded_sig(t.body or "")
        print(f"ID {t.template_id} {t.template_name}")
        print("  markers:", flags)
        print("  tail:", repr((t.body or "")[-100:]))
        assert not flags["tyler sullivan"], t.template_id
        assert not flags["(417) 221-5834"], t.template_id

    assert "Tyler Sullivan" in sig
    assert "www.carmeco.com" in sig

    print("=== PREVIEW ===")
    for tid in (1, 2):
        r = preview_client_email(
            client_id,
            ClientEmailPreviewRequest(
                account_id=acct, contact_id=bryan, template_id=tid
            ),
            user_id=uid,
        )
        n = r.body.count("Tyler Sullivan")
        print(
            f"template {tid}: tyler_count={n} placement={r.signature_placement} "
            f"Signature:_in_body={'Signature:' in r.body}"
        )
        assert n == 1, (tid, n, r.body[-400:])
        assert "Signature:" not in r.body
        assert "www.carmeco.com" in r.body
        assert "[" not in r.body.split("www.carmeco.com")[0][-20:]
        # message_body must not include signature (no placeholder in these templates)
        assert r.message_body.count("Tyler Sullivan") == 0

    with get_connection() as conn:
        after_crm = {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "sales_events": conn.execute(
                "SELECT COUNT(*) FROM client_sales_events"
            ).fetchone()[0],
        }
    assert before_crm == after_crm
    print("PASS changed_ids=", changed, "crm=", after_crm)


if __name__ == "__main__":
    main()
