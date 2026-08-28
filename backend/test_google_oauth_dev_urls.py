"""Google OAuth development URL configuration (no token exchange).

Run: python test_google_oauth_dev_urls.py

Uses isolated testdb HTTP. Does not print secrets, send mail, or open
production northstar.db. Does not change Flora Jia or Whirlpool.
"""

from __future__ import annotations

import testdb
import os
import sys
from pathlib import Path
from unittest.mock import patch

from google_oauth_config import (
    api_public_base,
    frontend_after_connect_base,
    google_redirect_uri,
)

CALLBACK_PATH = "/api/email/google/callback"
EXPECTED_REDIRECT = "http://127.0.0.1:8007/api/email/google/callback"
EXPECTED_API_PUBLIC = "http://127.0.0.1:8007"
EXPECTED_FRONTEND = "http://localhost:5174"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def main() -> int:
    try:
        redirect = google_redirect_uri()
        if redirect != EXPECTED_REDIRECT:
            _fail("GOOGLE_OAUTH_REDIRECT_URI must use 127.0.0.1:8007 callback.")
        if "8006" in redirect:
            _fail("Redirect URI still references port 8006.")
        public = api_public_base()
        if public != EXPECTED_API_PUBLIC:
            _fail("NORTHSTAR_API_PUBLIC_BASE must be http://127.0.0.1:8007.")
        if "8006" in public:
            _fail("API public base still references port 8006.")
        origin = frontend_after_connect_base()
        if origin != EXPECTED_FRONTEND:
            _fail("NORTHSTAR_FRONTEND_ORIGIN must be http://localhost:5174.")
        if origin.endswith(":5173") or "://127.0.0.1:5173" in origin:
            _fail("Frontend return origin still references port 5173.")

        with patch("google_oauth_config._reload_dotenv"):
            cleared = {
                "GOOGLE_OAUTH_REDIRECT_URI": "",
                "NORTHSTAR_FRONTEND_ORIGIN": "",
                "NORTHSTAR_API_PUBLIC_BASE": "",
            }
            with patch.dict(os.environ, cleared, clear=False):
                if google_redirect_uri() != EXPECTED_REDIRECT:
                    _fail("Default google_redirect_uri must use 127.0.0.1:8007.")
                if api_public_base() != EXPECTED_API_PUBLIC:
                    _fail("Default api_public_base must be http://127.0.0.1:8007.")
                if frontend_after_connect_base() != EXPECTED_FRONTEND:
                    _fail("Default frontend origin must be http://localhost:5174.")

        client = testdb.test_client()
        paths = {getattr(route, "path", "") for route in client.app.routes}
        if CALLBACK_PATH not in paths:
            _fail("FastAPI is missing GET /api/email/google/callback.")
        status_code, status = testdb.http_json("GET", "/api/email/google/status")
        if status_code != 200:
            _fail(f"GET /api/email/google/status failed ({status_code}).")
        reported = str(status.get("redirect_uri") or "")
        if reported != EXPECTED_REDIRECT:
            _fail("OAuth status redirect_uri must be the 8007 callback URL.")
        if CALLBACK_PATH not in reported:
            _fail("OAuth status redirect_uri must include /api/email/google/callback.")

        backend_dir = Path(__file__).resolve().parent
        oauth_src = (backend_dir / "gmail_oauth.py").read_text(encoding="utf-8")
        main_src = (backend_dir / "main.py").read_text(encoding="utf-8")
        if "/administration" not in oauth_src or "email_oauth=connected" not in oauth_src:
            _fail("OAuth success return must send the browser to /administration.")
        if "/administration" not in main_src or "email_oauth=error" not in main_src:
            _fail("OAuth error return must send the browser to /administration.")

        print("PASS: OAuth callback/public backend is 127.0.0.1:8007.")
        print("      Frontend return origin is http://localhost:5174.")
        print("      OAuth browser return path is /administration.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
