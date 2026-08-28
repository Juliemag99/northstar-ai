"""Google OAuth / Gmail configuration from environment (never commit secrets)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent

# Narrow send-only scope + email identity for match verification.
GMAIL_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "email",
    "profile",
)

# Prefer gmail.send over gmail.compose / mail.google.com (minimum for send workflow).
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URI = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"
GMAIL_API_SEND_URI = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _reload_dotenv() -> None:
    """Re-read .env on each config access so uvicorn --reload picks up secrets
    without requiring a process kill when only .env changed."""
    load_dotenv(_BACKEND_DIR / ".env", override=True)
    load_dotenv(_REPO_ROOT / ".env", override=False)


def google_oauth_configured() -> bool:
    _reload_dotenv()
    return bool(google_client_id() and google_client_secret() and oauth_token_encryption_key())


def google_client_id() -> str:
    _reload_dotenv()
    return _blank(os.getenv("GOOGLE_OAUTH_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID"))


def google_client_secret() -> str:
    _reload_dotenv()
    return _blank(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or os.getenv("GOOGLE_CLIENT_SECRET"))


def google_redirect_uri() -> str:
    """Must match the Authorized redirect URI in Google Cloud Console."""
    _reload_dotenv()
    return _blank(os.getenv("GOOGLE_OAUTH_REDIRECT_URI")) or (
        "http://127.0.0.1:8007/api/email/google/callback"
    )


def oauth_token_encryption_key() -> str:
    """Fernet key (url-safe base64) or any long secret used to derive Fernet key."""
    _reload_dotenv()
    return _blank(
        os.getenv("NORTHSTAR_OAUTH_TOKEN_KEY")
        or os.getenv("NORTHSTAR_EMAIL_TOKEN_KEY")
    )


def frontend_after_connect_base() -> str:
    """Where to send the browser after OAuth completes."""
    _reload_dotenv()
    return _blank(os.getenv("NORTHSTAR_FRONTEND_ORIGIN")) or "http://localhost:5174"


def api_public_base() -> str:
    _reload_dotenv()
    return _blank(os.getenv("NORTHSTAR_API_PUBLIC_BASE")) or "http://127.0.0.1:8007"
