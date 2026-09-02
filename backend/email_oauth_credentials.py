"""Encrypted OAuth credential storage for email provider connections.

Never stores Google passwords. Access/refresh tokens are encrypted at rest
with NORTHSTAR_OAUTH_TOKEN_KEY. client_email_accounts.provider_connection_ref
holds only the opaque credential row id (e.g. "gcred:12").
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from db import get_connection
from google_oauth_config import oauth_token_encryption_key

PROVIDER_GOOGLE = "Google"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def ensure_email_oauth_credentials_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS email_oauth_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                email_account_id INTEGER NOT NULL,
                provider TEXT NOT NULL DEFAULT 'Google',
                google_sub TEXT NOT NULL DEFAULT '',
                connected_email TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT '',
                token_blob_enc TEXT NOT NULL DEFAULT '',
                token_expires_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                revoked_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (email_account_id) REFERENCES client_email_accounts(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_email_oauth_creds_account
                ON email_oauth_credentials(email_account_id, status);

            CREATE TABLE IF NOT EXISTS email_oauth_states (
                state TEXT PRIMARY KEY,
                client_id INTEGER NOT NULL,
                email_account_id INTEGER NOT NULL,
                user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def connection_ref_for_id(credential_id: int) -> str:
    return f"gcred:{int(credential_id)}"


def parse_connection_ref(ref: str) -> int | None:
    raw = _blank(ref)
    if raw.startswith("gcred:"):
        tail = raw.split(":", 1)[1]
        if tail.isdigit():
            return int(tail)
    if raw.isdigit():
        return int(raw)
    return None


def _fernet() -> Fernet:
    secret = oauth_token_encryption_key()
    if not secret:
        raise RuntimeError(
            "NORTHSTAR_OAUTH_TOKEN_KEY is not set. "
            "Generate a Fernet key and put it in backend/.env."
        )
    # Accept raw Fernet key or derive from arbitrary secret.
    try:
        return Fernet(secret.encode("utf-8") if isinstance(secret, str) else secret)
    except Exception:
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)


def encrypt_token_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _fernet().encrypt(raw).decode("utf-8")


def decrypt_token_payload(blob: str) -> dict[str, Any]:
    try:
        raw = _fernet().decrypt(_blank(blob).encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt OAuth credential blob.") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid OAuth credential payload.")
    return data


def store_oauth_state(
    *,
    state: str,
    client_id: int,
    email_account_id: int,
    user_id: int,
    ttl_seconds: int = 600,
) -> None:
    initiator_id = int(user_id)
    if initiator_id <= 0:
        raise ValueError("OAuth state requires an initiating administrator.")
    ensure_email_oauth_credentials_schema()
    now = datetime.now(timezone.utc)
    expires = now.timestamp() + max(60, ttl_seconds)
    expires_at = datetime.fromtimestamp(expires, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_connection() as conn:
        # prune expired
        conn.execute(
            "DELETE FROM email_oauth_states WHERE expires_at < ?",
            (_now(),),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO email_oauth_states
                (state, client_id, email_account_id, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (state, client_id, email_account_id, initiator_id, _now(), expires_at),
        )


def consume_oauth_state(state: str) -> dict[str, Any] | None:
    ensure_email_oauth_credentials_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM email_oauth_states WHERE state = ?",
            (_blank(state),),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM email_oauth_states WHERE state = ?", (_blank(state),))
        d = dict(row)
        if _blank(d.get("expires_at")) < _now():
            return None
        return d


def upsert_google_credential(
    *,
    client_id: int,
    email_account_id: int,
    connected_email: str,
    google_sub: str,
    scopes: str,
    access_token: str,
    refresh_token: str,
    expires_at: str = "",
    token_type: str = "Bearer",
) -> int:
    """Encrypt and store tokens. Returns credential id. Never logs token values."""
    ensure_email_oauth_credentials_schema()
    if not _blank(access_token):
        raise ValueError("access_token required")
    # refresh may be empty on reconnect if Google omits it — keep prior refresh
    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token_type or "Bearer",
        "expires_at": expires_at,
        "scopes": scopes,
    }
    blob = encrypt_token_payload(payload)
    now = _now()
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT id, token_blob_enc FROM email_oauth_credentials
            WHERE email_account_id = ? AND provider = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1
            """,
            (email_account_id, PROVIDER_GOOGLE),
        ).fetchone()
        if existing and not _blank(refresh_token):
            try:
                prior = decrypt_token_payload(_blank(existing["token_blob_enc"]))
                if _blank(prior.get("refresh_token")):
                    payload["refresh_token"] = prior["refresh_token"]
                    blob = encrypt_token_payload(payload)
            except Exception:
                pass

        if existing:
            conn.execute(
                """
                UPDATE email_oauth_credentials SET
                    connected_email = ?, google_sub = ?, scopes = ?,
                    token_blob_enc = ?, token_expires_at = ?,
                    status = 'active', updated_at = ?, revoked_at = ''
                WHERE id = ?
                """,
                (
                    _blank(connected_email).lower(),
                    _blank(google_sub),
                    _blank(scopes),
                    blob,
                    expires_at,
                    now,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])

        cur = conn.execute(
            """
            INSERT INTO email_oauth_credentials (
                client_id, email_account_id, provider, google_sub, connected_email,
                scopes, token_blob_enc, token_expires_at, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                client_id,
                email_account_id,
                PROVIDER_GOOGLE,
                _blank(google_sub),
                _blank(connected_email).lower(),
                _blank(scopes),
                blob,
                expires_at,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def get_active_credential_for_account(
    email_account_id: int, *, client_id: int | None = None
) -> dict[str, Any] | None:
    ensure_email_oauth_credentials_schema()
    with get_connection() as conn:
        if client_id is not None:
            row = conn.execute(
                """
                SELECT * FROM email_oauth_credentials
                WHERE email_account_id = ? AND client_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (email_account_id, client_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM email_oauth_credentials
                WHERE email_account_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (email_account_id,),
            ).fetchone()
        return dict(row) if row else None


def load_decrypted_tokens(
    email_account_id: int, *, client_id: int
) -> dict[str, Any]:
    row = get_active_credential_for_account(email_account_id, client_id=client_id)
    if not row:
        raise LookupError("No active Google connection for this sender account.")
    tokens = decrypt_token_payload(_blank(row["token_blob_enc"]))
    return {
        "credential_id": int(row["id"]),
        "connected_email": _blank(row["connected_email"]),
        "google_sub": _blank(row["google_sub"]),
        "scopes": _blank(row["scopes"]),
        "access_token": _blank(tokens.get("access_token")),
        "refresh_token": _blank(tokens.get("refresh_token")),
        "token_type": _blank(tokens.get("token_type")) or "Bearer",
        "expires_at": _blank(tokens.get("expires_at") or row.get("token_expires_at")),
    }


def update_access_token(
    credential_id: int,
    *,
    access_token: str,
    expires_at: str = "",
    refresh_token: str | None = None,
) -> None:
    ensure_email_oauth_credentials_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM email_oauth_credentials WHERE id = ?",
            (credential_id,),
        ).fetchone()
        if not row:
            raise LookupError("Credential not found.")
        payload = decrypt_token_payload(_blank(row["token_blob_enc"]))
        payload["access_token"] = access_token
        if expires_at:
            payload["expires_at"] = expires_at
        if refresh_token:
            payload["refresh_token"] = refresh_token
        blob = encrypt_token_payload(payload)
        conn.execute(
            """
            UPDATE email_oauth_credentials
            SET token_blob_enc = ?, token_expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (blob, expires_at or _blank(row["token_expires_at"]), _now(), credential_id),
        )


def revoke_credential(credential_id: int) -> str:
    """Mark revoked; return refresh_token (if any) for Google revoke call — caller must not log it."""
    ensure_email_oauth_credentials_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM email_oauth_credentials WHERE id = ?",
            (credential_id,),
        ).fetchone()
        if not row:
            return ""
        refresh = ""
        try:
            payload = decrypt_token_payload(_blank(row["token_blob_enc"]))
            refresh = _blank(payload.get("refresh_token"))
        except Exception:
            refresh = ""
        # Wipe ciphertext; keep metadata for audit
        empty = encrypt_token_payload(
            {"access_token": "", "refresh_token": "", "revoked": True}
        )
        conn.execute(
            """
            UPDATE email_oauth_credentials
            SET status = 'revoked', token_blob_enc = ?, revoked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (empty, _now(), _now(), credential_id),
        )
        return refresh
