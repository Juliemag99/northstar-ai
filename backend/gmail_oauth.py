"""Google OAuth web-server flow for client email accounts (Gmail)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from access import get_default_user, get_user_by_id
from client_email_accounts_data import get_email_account, ensure_client_email_accounts_schema
from client_setup_data import user_can_edit_client_setup
from db import get_connection
from email_oauth_credentials import (
    connection_ref_for_id,
    consume_oauth_state,
    ensure_email_oauth_credentials_schema,
    get_active_credential_for_account,
    parse_connection_ref,
    revoke_credential,
    store_oauth_state,
    upsert_google_credential,
)
from google_oauth_config import (
    GMAIL_OAUTH_SCOPES,
    GOOGLE_AUTH_URI,
    GOOGLE_REVOKE_URI,
    GOOGLE_TOKEN_URI,
    GOOGLE_USERINFO_URI,
    frontend_after_connect_base,
    google_client_id,
    google_client_secret,
    google_oauth_configured,
    google_redirect_uri,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _require_edit(user_id: int, client_id: int) -> None:
    if not user_can_edit_client_setup(user_id, client_id):
        raise PermissionError("Not authorized to connect email accounts for this client.")


def oauth_status() -> dict[str, Any]:
    return {
        "configured": google_oauth_configured(),
        "redirect_uri": google_redirect_uri(),
        "scopes": list(GMAIL_OAUTH_SCOPES),
        "provider": "Google",
        "message": (
            "Google OAuth is ready."
            if google_oauth_configured()
            else (
                "Set GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, and "
                "NORTHSTAR_OAUTH_TOKEN_KEY in backend/.env (see .env.example)."
            )
        ),
    }


def begin_google_connect(
    account_id: int, *, user_id: int | None = None
) -> dict[str, Any]:
    """Validate account, set connecting, return Google authorization URL."""
    if not google_oauth_configured():
        raise RuntimeError(oauth_status()["message"])

    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")

    ensure_client_email_accounts_schema()
    ensure_email_oauth_credentials_schema()

    # Resolve account across clients then enforce edit rights on its client
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_email_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    if not row:
        raise LookupError("Sender account not found.")
    client_id = int(row["client_id"])
    _require_edit(user.id, client_id)
    account = get_email_account(client_id, account_id, user_id=user.id)

    provider = _blank(account.provider).lower()
    if provider not in {"google", "gmail", "other", ""}:
        # Allow Other → Google connect if Julie chooses Google for this mailbox
        pass
    # Force provider label to Google on connect attempt
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE client_email_accounts
            SET provider = 'Google', connection_status = 'connecting', updated_at = ?, updated_by = ?
            WHERE id = ? AND client_id = ?
            """,
            (_now(), _blank(user.full_name) or _blank(user.email), account_id, client_id),
        )

    state = secrets.token_urlsafe(32)
    store_oauth_state(
        state=state,
        client_id=client_id,
        email_account_id=account_id,
        user_id=user.id,
    )

    params = {
        "client_id": google_client_id(),
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(GMAIL_OAUTH_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "login_hint": account.email_address,
    }
    return {
        "authorization_url": f"{GOOGLE_AUTH_URI}?{urlencode(params)}",
        "account_id": account_id,
        "client_id": client_id,
        "email_address": account.email_address,
        "redirect_uri": google_redirect_uri(),
        "scopes": list(GMAIL_OAUTH_SCOPES),
    }


def _set_account_status(
    *,
    client_id: int,
    account_id: int,
    status: str,
    provider_connection_ref: str = "",
    actor: str = "",
) -> None:
    with get_connection() as conn:
        if provider_connection_ref:
            conn.execute(
                """
                UPDATE client_email_accounts SET
                    connection_status = ?, provider_connection_ref = ?,
                    provider = 'Google', updated_at = ?, updated_by = ?
                WHERE id = ? AND client_id = ?
                """,
                (status, provider_connection_ref, _now(), actor, account_id, client_id),
            )
        else:
            conn.execute(
                """
                UPDATE client_email_accounts SET
                    connection_status = ?, updated_at = ?, updated_by = ?
                WHERE id = ? AND client_id = ?
                """,
                (status, _now(), actor, account_id, client_id),
            )


def complete_google_callback(
    *, code: str, state: str
) -> dict[str, Any]:
    """Exchange code, verify Google identity matches configured sender, mark connected."""
    if not google_oauth_configured():
        raise RuntimeError("Google OAuth is not configured.")
    st = consume_oauth_state(state)
    if not st:
        raise ValueError("Invalid or expired OAuth state. Start Connect again.")

    client_id = int(st["client_id"])
    account_id = int(st["email_account_id"])
    account = get_email_account(client_id, account_id)

    try:
        with httpx.Client(timeout=30.0) as client:
            token_resp = client.post(
                GOOGLE_TOKEN_URI,
                data={
                    "code": code,
                    "client_id": google_client_id(),
                    "client_secret": google_client_secret(),
                    "redirect_uri": google_redirect_uri(),
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code >= 400:
                _set_account_status(
                    client_id=client_id, account_id=account_id, status="error"
                )
                raise ValueError(
                    "Google token exchange failed. Check OAuth client configuration."
                )
            token_data = token_resp.json()
            access_token = _blank(token_data.get("access_token"))
            refresh_token = _blank(token_data.get("refresh_token"))
            expires_in = int(token_data.get("expires_in") or 0)
            scope = _blank(token_data.get("scope"))
            if not access_token:
                _set_account_status(
                    client_id=client_id, account_id=account_id, status="error"
                )
                raise ValueError("Google did not return an access token.")

            info_resp = client.get(
                GOOGLE_USERINFO_URI,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info_resp.status_code >= 400:
                _set_account_status(
                    client_id=client_id, account_id=account_id, status="error"
                )
                raise ValueError("Unable to verify Google account identity.")
            info = info_resp.json()
    except ValueError:
        raise
    except Exception as exc:
        _set_account_status(client_id=client_id, account_id=account_id, status="error")
        raise ValueError(f"OAuth callback failed: {exc}") from exc

    connected_email = _blank(info.get("email")).lower()
    google_sub = _blank(info.get("sub"))
    configured = _blank(account.email_address).lower()

    if not connected_email or connected_email != configured:
        _set_account_status(client_id=client_id, account_id=account_id, status="error")
        return {
            "ok": False,
            "match": False,
            "account_id": account_id,
            "client_id": client_id,
            "configured_email": configured,
            "connected_email": connected_email or "(unknown)",
            "connection_status": "error",
            "message": (
                "Connected Google account does not match configured sender. "
                f"Expected {configured}, got {connected_email or '(unknown)'}. "
                "Corrective action: reconnect using the correct Google identity."
            ),
            "frontend_redirect": (
                f"{frontend_after_connect_base()}/clients/{client_id}/knowledge"
                f"?email_oauth=mismatch&account_id={account_id}"
            ),
        }

    expires_at = ""
    if expires_in > 0:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    cred_id = upsert_google_credential(
        client_id=client_id,
        email_account_id=account_id,
        connected_email=connected_email,
        google_sub=google_sub,
        scopes=scope or " ".join(GMAIL_OAUTH_SCOPES),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    ref = connection_ref_for_id(cred_id)
    _set_account_status(
        client_id=client_id,
        account_id=account_id,
        status="connected",
        provider_connection_ref=ref,
        actor="Google OAuth",
    )

    return {
        "ok": True,
        "match": True,
        "account_id": account_id,
        "client_id": client_id,
        "configured_email": configured,
        "connected_email": connected_email,
        "connection_status": "connected",
        "provider_connection_ref": ref,
        "message": f"Connected as {connected_email}",
        "frontend_redirect": (
            f"{frontend_after_connect_base()}/clients/{client_id}/knowledge"
            f"?email_oauth=connected&account_id={account_id}"
        ),
    }


def disconnect_google_account(
    client_id: int, account_id: int, *, user_id: int | None = None
) -> dict[str, Any]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    account = get_email_account(client_id, account_id, user_id=user.id)

    cred = get_active_credential_for_account(account_id, client_id=client_id)
    refresh = ""
    if cred:
        refresh = revoke_credential(int(cred["id"]))
    else:
        # Try ref parse
        cid = parse_connection_ref(account.provider_connection_ref)
        if cid:
            refresh = revoke_credential(cid)

    if refresh:
        try:
            with httpx.Client(timeout=15.0) as client:
                client.post(GOOGLE_REVOKE_URI, data={"token": refresh})
        except Exception:
            pass  # local revoke still applies

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE client_email_accounts SET
                connection_status = 'revoked',
                provider_connection_ref = '',
                updated_at = ?, updated_by = ?
            WHERE id = ? AND client_id = ?
            """,
            (_now(), _blank(user.full_name) or _blank(user.email), account_id, client_id),
        )

    return {
        "account_id": account_id,
        "client_id": client_id,
        "connection_status": "revoked",
        "message": (
            f"Disconnected Google for {account.email_address}. "
            "Historical Email Sent events are preserved. Sending is blocked until reconnect."
        ),
    }
