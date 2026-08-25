"""Gmail API send + Email Sent sales_event (after confirmed success only)."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

import httpx

from access import get_default_user, get_user_by_id, user_can_access_client
from client_email_accounts_data import get_email_account
from client_setup_data import user_can_edit_client_setup
from db import get_connection
from email_oauth_credentials import load_decrypted_tokens, update_access_token
from google_oauth_config import GMAIL_API_SEND_URI, GOOGLE_TOKEN_URI, google_client_id, google_client_secret
from models import ClientEmailSendRequest, ClientEmailSendResult

EMAIL_SENT_TYPE = "Email Sent"
_UNRESOLVED_RE = re.compile(r"\bUNRESOLVED\b")
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,80}\]")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _require_access(user_id: int, client_id: int) -> None:
    user = get_user_by_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    if user.is_administrator:
        return
    if not user_can_access_client(user_id, client_id):
        raise PermissionError("Not authorized for this client.")


def ensure_email_sent_schema(conn=None) -> None:
    """Add optional Email Sent columns to client_sales_events if missing."""
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(client_sales_events)").fetchall()
        }
        alters = [
            ("provider_message_id", "TEXT NOT NULL DEFAULT ''"),
            ("email_provider", "TEXT NOT NULL DEFAULT ''"),
            ("sender_account_id", "INTEGER"),
            ("email_subject", "TEXT NOT NULL DEFAULT ''"),
            ("template_id", "INTEGER"),
            ("appointment_event_id", "INTEGER"),
        ]
        for name, decl in alters:
            if name not in cols:
                conn.execute(
                    f"ALTER TABLE client_sales_events ADD COLUMN {name} {decl}"
                )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _refresh_access_token(tokens: dict[str, Any]) -> dict[str, Any]:
    refresh = _blank(tokens.get("refresh_token"))
    if not refresh:
        raise ValueError(
            "Google connection needs reconnect (missing refresh token). "
            "Use Reconnect under Email & Sending."
        )
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            GOOGLE_TOKEN_URI,
            data={
                "client_id": google_client_id(),
                "client_secret": google_client_secret(),
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            raise ValueError(
                "Google token refresh failed. Reconnect the Google account."
            )
        data = resp.json()
    access = _blank(data.get("access_token"))
    if not access:
        raise ValueError("Google token refresh returned no access token.")
    expires_in = int(data.get("expires_in") or 0)
    expires_at = ""
    if expires_in > 0:
        from datetime import timedelta

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_access_token(
        int(tokens["credential_id"]),
        access_token=access,
        expires_at=expires_at,
        refresh_token=_blank(data.get("refresh_token")) or None,
    )
    tokens["access_token"] = access
    tokens["expires_at"] = expires_at
    return tokens


def _validate_send_guards(body: ClientEmailSendRequest) -> None:
    if not body.confirm_send:
        raise ValueError(
            "Human confirmation required. Set confirm_send=true after reviewing the email."
        )
    if not _blank(body.to_address) or "@" not in body.to_address:
        raise ValueError("Recipient email is required.")
    if not _blank(body.subject):
        raise ValueError("Subject is required.")
    combined = f"{body.subject}\n{body.body}\n{body.signature}"
    if _UNRESOLVED_RE.search(combined):
        raise ValueError(
            "Cannot send while UNRESOLVED placeholders remain. "
            "Edit or remove them before sending."
        )
    if _PLACEHOLDER_RE.search(combined):
        raise ValueError(
            "Cannot send while unresolved [placeholders] remain in subject/body/signature."
        )


def _create_email_sent_event(
    *,
    client_id: int,
    company_id: int | None,
    contact_id: int | None,
    sender_account_id: int,
    recipient: str,
    subject: str,
    template_id: int | None,
    provider_message_id: str,
    rep_name: str,
    rep_user_id: int | None,
    appointment_event_id: int | None,
    user_id: int,
    user_name: str,
    company_name: str = "",
    contact_name: str = "",
) -> int:
    ensure_email_sent_schema()
    now = _now()
    event_date = now[:10]
    event_time = now[11:19] if len(now) >= 19 else ""
    relationship_id = None
    with get_connection() as conn:
        if company_id:
            rel = conn.execute(
                """
                SELECT id FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                LIMIT 1
                """,
                (client_id, company_id),
            ).fetchone()
            if rel:
                relationship_id = int(rel["id"])
            if not company_name:
                crow = conn.execute(
                    "SELECT company_name FROM companies WHERE id = ?",
                    (company_id,),
                ).fetchone()
                company_name = _blank(crow["company_name"]) if crow else ""

        notes = (
            f"Email Sent via Google Gmail API.\n"
            f"From sender_account_id={sender_account_id}\n"
            f"To: {recipient}\n"
            f"Subject: {subject}\n"
            f"provider_message_id={provider_message_id}"
        )
        cur = conn.execute(
            """
            INSERT INTO client_sales_events (
                client_id, relationship_id, company_id, contact_id, thread_key,
                event_type, event_family, source_date_time_text, event_date, event_time,
                company_name, contact_name, email, caller_notes, sales_notes,
                source_rev_spec_text, rev_spec_user_id,
                source_file_name, source_row_fingerprint,
                imported_at, imported_by_user_id, imported_by_name, created_at,
                provider_message_id, email_provider, sender_account_id,
                email_subject, template_id, appointment_event_id
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                client_id,
                relationship_id,
                company_id,
                contact_id,
                f"email:{provider_message_id}" if provider_message_id else "",
                EMAIL_SENT_TYPE,
                "Email",
                now,
                event_date,
                event_time,
                company_name,
                contact_name,
                recipient,
                notes,
                f"Subject: {subject}",
                rep_name,
                rep_user_id,
                "Gmail API",
                f"gmail:{provider_message_id}:{client_id}:{sender_account_id}",
                now,
                user_id,
                user_name,
                now,
                provider_message_id,
                "Google",
                sender_account_id,
                subject,
                template_id,
                appointment_event_id,
            ),
        )
        return int(cur.lastrowid)


def send_client_email(
    client_id: int,
    body: ClientEmailSendRequest,
    *,
    user_id: int | None = None,
) -> ClientEmailSendResult:
    """Send via Gmail API after human confirmation. Creates Email Sent only on success."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    # Sending requires edit-level client access (same as connect)
    if not user_can_edit_client_setup(user.id, client_id) and not user.is_administrator:
        raise PermissionError("Not authorized to send email for this client.")

    _validate_send_guards(body)

    account = get_email_account(client_id, body.account_id, user_id=user.id)
    if account.client_id != client_id:
        raise PermissionError("Cross-client sender account rejected.")
    if not account.active:
        raise ValueError("Sender account is inactive.")
    if account.connection_status != "connected":
        raise ValueError(
            f"Sender is not connected (status={account.connection_status}). "
            "Connect Google Account before sending."
        )

    configured = _blank(account.email_address).lower()
    tokens = load_decrypted_tokens(account.account_id, client_id=client_id)
    if _blank(tokens.get("connected_email")).lower() != configured:
        raise ValueError(
            "Connected Google identity does not match configured sender. Reconnect."
        )

    mime_body = body.body or ""
    sig = _blank(body.signature)
    if sig and sig not in mime_body:
        mime_body = f"{mime_body.rstrip()}\n\n{sig}"

    msg = MIMEText(mime_body, _charset="utf-8")
    msg["To"] = _blank(body.to_address)
    msg["From"] = (
        f"{account.display_name} <{account.email_address}>"
        if account.display_name
        else account.email_address
    )
    msg["Subject"] = _blank(body.subject)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8").rstrip("=")

    # Prefer existing access token; refresh only on 401
    def _do_send(access_token: str) -> dict[str, Any]:
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(
                GMAIL_API_SEND_URI,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"raw": raw},
            )
            if resp.status_code == 401:
                return {"_unauthorized": True}
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = str(resp.json().get("error", {}).get("message") or "")
                except Exception:
                    detail = resp.text[:200]
                raise ValueError(f"Gmail send failed: {detail or resp.status_code}")
            return resp.json()

    result = _do_send(_blank(tokens.get("access_token")))
    if result.get("_unauthorized"):
        tokens = _refresh_access_token(tokens)
        result = _do_send(_blank(tokens.get("access_token")))
        if result.get("_unauthorized"):
            raise ValueError("Gmail authorization expired. Reconnect Google Account.")

    provider_message_id = _blank(result.get("id"))
    if not provider_message_id:
        raise ValueError("Gmail did not return a message id — Email Sent not recorded.")

    # Resolve display names for event
    company_name = ""
    contact_name = ""
    with get_connection() as conn:
        if body.contact_id:
            crow = conn.execute(
                """
                SELECT trim(coalesce(first_name,'') || ' ' || coalesce(last_name,'')) AS n
                FROM contacts WHERE id = ?
                """,
                (body.contact_id,),
            ).fetchone()
            contact_name = _blank(crow["n"]) if crow else ""
        if body.company_id:
            crow = conn.execute(
                "SELECT company_name FROM companies WHERE id = ?",
                (body.company_id,),
            ).fetchone()
            company_name = _blank(crow["company_name"]) if crow else ""

    rep_name = ""
    rep_user_id = None
    for asg in account.assignments:
        if asg.active:
            rep_name = asg.source_rep_name
            rep_user_id = asg.user_id
            if asg.is_default_for_rep:
                break

    event_id = _create_email_sent_event(
        client_id=client_id,
        company_id=body.company_id,
        contact_id=body.contact_id,
        sender_account_id=account.account_id,
        recipient=_blank(body.to_address),
        subject=_blank(body.subject),
        template_id=body.template_id,
        provider_message_id=provider_message_id,
        rep_name=rep_name,
        rep_user_id=rep_user_id,
        appointment_event_id=body.appointment_event_id,
        user_id=user.id,
        user_name=_blank(user.full_name) or _blank(user.email),
        company_name=company_name,
        contact_name=contact_name,
    )

    return ClientEmailSendResult(
        ok=True,
        client_id=client_id,
        account_id=account.account_id,
        company_id=body.company_id,
        contact_id=body.contact_id,
        to_address=_blank(body.to_address),
        subject=_blank(body.subject),
        provider="Google",
        provider_message_id=provider_message_id,
        sales_event_id=event_id,
        event_type=EMAIL_SENT_TYPE,
        message=(
            f"Email sent from {account.email_address} to {body.to_address}. "
            f"Email Sent event #{event_id} created."
        ),
    )
