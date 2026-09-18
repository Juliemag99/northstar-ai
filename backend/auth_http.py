"""Staff login, logout, current-user HTTP, CSRF, and optional 401 enforcement.

Optional authentication. NORTHSTAR_AUTH_ENFORCE fail-safe lives here: the flag
activates route protection only when Julie has a password hash.

CSRF: when a valid northstar_session cookie is present, mutating requests
must send X-CSRF-Token matching the session csrf_secret. Unauthenticated
writes stay allowed while enforcement is off.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from access import (
    DEFAULT_USER_EMAIL,
    get_user_by_id,
    list_clients_for_user,
    user_can_access_client,
)
from staff_context import bind_staff_actor, reset_staff_actor
from auth_passwords import verify_password
from auth_sessions import (
    ABSOLUTE_HOURS,
    create_staff_session,
    lookup_staff_session,
    revoke_staff_session,
)
from db import get_connection
from models import NorthStarUser

ENFORCE_FLAG = "NORTHSTAR_AUTH_ENFORCE"
SESSION_COOKIE = "northstar_session"
CSRF_HEADER = "X-CSRF-Token"
LOGIN_FAILED_DETAIL = "Invalid email or password."
AUTH_REQUIRED_DETAIL = "Authentication required."
ADMIN_REQUIRED_DETAIL = "Not authorized."
CSRF_FAILED_DETAIL = "CSRF token missing or invalid."
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOGIN_PATH = "/api/auth/login"
UNAUTHENTICATED_EXACT = frozenset(
    {
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/me"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/email/google/callback"),
        ("GET", "/health"),
        ("GET", "/api/ready"),
    }
)

staff_auth_router = APIRouter(tags=["auth"])

_TIMING_HASHER = PasswordHasher()
_TIMING_HASH = _TIMING_HASHER.hash(f"NsDummy9{secrets.token_hex(16)}")


class StaffLoginRequest(BaseModel):
    email: str = Field(default="")
    password: str = Field(default="")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: object) -> datetime | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dummy_verify(password: str) -> None:
    """Burn Argon2 verify time so unknown emails are not cheap to probe."""
    candidate = password if isinstance(password, str) else ""
    try:
        _TIMING_HASHER.verify(_TIMING_HASH, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return


def _cookie_secure(request: Request) -> bool:
    """Secure cookies when the request is HTTPS, including a trusted reverse proxy.

    NORTHSTAR_TRUST_PROXY=1 is required before X-Forwarded-Proto is honored.
    Without it, a client cannot force Secure cookies by spoofing the header.
    """
    if request.url.scheme == "https":
        return True
    if os.environ.get("NORTHSTAR_TRUST_PROXY", "").strip() != "1":
        return False
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return forwarded.lower() == "https"


def _session_max_age() -> int:
    return int(ABSOLUTE_HOURS * 3600)


def _set_session_cookie(response: Response, request: Request, raw_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=raw_token,
        max_age=_session_max_age(),
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value="",
        max_age=0,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )


def _public_user(user: NorthStarUser, *, include_access: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "is_administrator": bool(user.is_administrator),
        "is_internal_northstar": bool(user.is_internal_northstar),
        "active": bool(user.active),
        "created_at": user.created_at,
    }
    if include_access:
        from staff_rbac import public_role_payload

        payload.update(public_role_payload(user))
        payload["clients"] = [
            {
                "client_id": a.client_id,
                "client_name": a.client_name,
                "client_code": a.client_code,
            }
            for a in list_clients_for_user(int(user.id), active_only=True)
        ]
    return payload


def _default_password_hash(conn=None) -> str:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE lower(email) = lower(?)",
            (DEFAULT_USER_EMAIL,),
        ).fetchone()
        if row is None:
            return ""
        return str(row["password_hash"] or "").strip()
    finally:
        if owns:
            conn.close()


def default_user_password_available() -> bool:
    return bool(_default_password_hash())


def auth_enforcement_active() -> bool:
    """True only when the env flag is 1 and Julie has a password hash.

    Defaults off. A missing or unreadable password hash cannot activate
    enforcement. Session-lookup failures while this is True fail closed.
    """
    if os.environ.get(ENFORCE_FLAG, "").strip() != "1":
        return False
    try:
        return default_user_password_available()
    except Exception:
        return False


def _auth_state() -> dict[str, bool]:
    available = default_user_password_available()
    return {
        "auth_enforced": auth_enforcement_active(),
        "auth_available": available,
    }


def _load_login_row(conn, email: str):
    return conn.execute(
        """
        SELECT id, email, full_name, active, password_hash,
               failed_login_count, locked_until
        FROM users
        WHERE lower(email) = lower(?)
        """,
        (email,),
    ).fetchone()


def _is_locked(row, now: datetime) -> bool:
    until = _parse_ts(row["locked_until"] if row is not None else "")
    return until is not None and now < until


def _register_login_failure(conn, user_id: int, now: datetime) -> None:
    conn.execute(
        """
        UPDATE users
        SET failed_login_count = COALESCE(failed_login_count, 0) + 1
        WHERE id = ?
        """,
        (int(user_id),),
    )
    count_row = conn.execute(
        "SELECT failed_login_count FROM users WHERE id = ?",
        (int(user_id),),
    ).fetchone()
    count = int(count_row["failed_login_count"] or 0) if count_row else 0
    if count >= LOCKOUT_THRESHOLD:
        conn.execute(
            "UPDATE users SET locked_until = ? WHERE id = ?",
            (_iso(now + timedelta(minutes=LOCKOUT_MINUTES)), int(user_id)),
        )


def _reset_lockout(conn, user_id: int) -> None:
    conn.execute(
        """
        UPDATE users
        SET failed_login_count = 0, locked_until = ''
        WHERE id = ?
        """,
        (int(user_id),),
    )


def _csrf_matches(given: str, expected: str) -> bool:
    if not given or not expected:
        return False
    if len(given) != len(expected):
        return False
    return secrets.compare_digest(given, expected)


def _raw_session_token(request: Request) -> str:
    return (request.cookies.get(SESSION_COOKIE) or "").strip()


def lookup_request_session(request: Request, *, touch: bool = False) -> dict[str, Any] | None:
    raw = _raw_session_token(request)
    if not raw:
        return None
    with get_connection() as conn:
        found = lookup_staff_session(conn, raw, touch=touch)
        if found is not None and touch:
            conn.commit()
        return found


def _login_failed() -> NoReturn:
    raise HTTPException(status_code=401, detail=LOGIN_FAILED_DETAIL)


def _unauthenticated_allowed(method: str, path: str) -> bool:
    if method == "OPTIONS":
        return True
    return (method, path) in UNAUTHENTICATED_EXACT


def _valid_active_staff_session(request: Request) -> bool:
    session = lookup_request_session(request, touch=False)
    if session is None:
        return False
    user = get_user_by_id(int(session["user_id"]))
    return user is not None and bool(user.active)


def require_authenticated_staff(request: Request) -> NorthStarUser:
    """Return the active staff user from the session cookie alone.

    Does not read get_default_user(), query parameters, headers, or body
    fields. Independent of NORTHSTAR_AUTH_ENFORCE: missing, invalid,
    expired, revoked, or inactive sessions are 401.
    """
    try:
        session = lookup_request_session(request, touch=False)
    except Exception:
        raise HTTPException(status_code=401, detail=AUTH_REQUIRED_DETAIL)
    if session is None:
        raise HTTPException(status_code=401, detail=AUTH_REQUIRED_DETAIL)
    try:
        user = get_user_by_id(int(session["user_id"]))
    except Exception:
        raise HTTPException(status_code=401, detail=AUTH_REQUIRED_DETAIL)
    if user is None or not bool(user.active):
        raise HTTPException(status_code=401, detail=AUTH_REQUIRED_DETAIL)
    return user


def require_administrator(request: Request) -> NorthStarUser:
    """Return the session user only when they are an active administrator."""
    user = require_authenticated_staff(request)
    if not bool(user.is_administrator):
        raise HTTPException(status_code=403, detail=ADMIN_REQUIRED_DETAIL)
    return user


def require_client_access(request: Request, client_id: int) -> NorthStarUser:
    """Session user must be assigned to the client (administrators included)."""
    user = require_authenticated_staff(request)
    try:
        allowed = user_can_access_client(int(user.id), int(client_id))
    except Exception:
        raise HTTPException(status_code=403, detail=ADMIN_REQUIRED_DETAIL)
    if not allowed:
        raise HTTPException(status_code=403, detail=ADMIN_REQUIRED_DETAIL)
    return user


def require_client_setup_editor(request: Request, client_id: int) -> NorthStarUser:
    """Session user must have existing Client Setup edit permission."""
    user = require_authenticated_staff(request)
    from client_setup_data import user_can_edit_client_setup

    try:
        allowed = user_can_edit_client_setup(int(user.id), int(client_id))
    except Exception:
        raise HTTPException(status_code=403, detail=ADMIN_REQUIRED_DETAIL)
    if not allowed:
        raise HTTPException(status_code=403, detail=ADMIN_REQUIRED_DETAIL)
    return user


def _auth_required_response() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": AUTH_REQUIRED_DETAIL})


class StaffCsrfMiddleware(BaseHTTPMiddleware):
    """Require X-CSRF-Token on writes only when a valid staff session exists."""

    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path
        if method in WRITE_METHODS and path != LOGIN_PATH:
            session = lookup_request_session(request, touch=False)
            if session is not None:
                header = request.headers.get(CSRF_HEADER) or request.headers.get(
                    CSRF_HEADER.lower()
                ) or ""
                if not _csrf_matches(header.strip(), str(session["csrf_secret"])):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": CSRF_FAILED_DETAIL},
                    )
        return await call_next(request)


class StaffAuthEnforceMiddleware(BaseHTTPMiddleware):
    """401-protect routes only when auth_enforcement_active() is true."""

    async def dispatch(self, request: Request, call_next):
        if not auth_enforcement_active():
            return await call_next(request)
        method = request.method.upper()
        path = request.url.path
        if _unauthenticated_allowed(method, path):
            return await call_next(request)
        try:
            if _valid_active_staff_session(request):
                return await call_next(request)
        except Exception:
            return _auth_required_response()
        return _auth_required_response()


class StaffActorMiddleware(BaseHTTPMiddleware):
    """Bind the session user as the request actor for CRM identity/ACL."""

    async def dispatch(self, request: Request, call_next):
        user = None
        try:
            session = lookup_request_session(request, touch=False)
            if session is not None:
                found = get_user_by_id(int(session["user_id"]))
                if found is not None and bool(found.active):
                    user = found
        except Exception:
            user = None
        token = bind_staff_actor(user)
        try:
            return await call_next(request)
        finally:
            reset_staff_actor(token)


def add_staff_csrf_middleware(app) -> None:
    # Add order is inner-first: last added runs first. main.py then wraps CORS
    # outermost, so the request stack is CORS → CSRF → enforcement → actor → routes.
    app.add_middleware(StaffActorMiddleware)
    app.add_middleware(StaffAuthEnforceMiddleware)
    app.add_middleware(StaffCsrfMiddleware)


@staff_auth_router.post("/api/auth/login")
def staff_login(body: StaffLoginRequest, request: Request, response: Response):
    email = (body.email or "").strip()
    password = body.password if isinstance(body.password, str) else ""
    now = _now()
    session: dict[str, Any] | None = None
    user_id: int | None = None
    failed = False

    conn = get_connection()
    try:
        row = _load_login_row(conn, email) if email else None
        if row is None:
            _dummy_verify(password)
            failed = True
        elif _is_locked(row, now):
            _dummy_verify(password)
            failed = True
        elif not bool(row["active"]):
            _dummy_verify(password)
            _register_login_failure(conn, int(row["id"]), now)
            failed = True
        elif not str(row["password_hash"] or "").strip():
            _dummy_verify(password)
            _register_login_failure(conn, int(row["id"]), now)
            failed = True
        elif not verify_password(password, str(row["password_hash"])):
            _register_login_failure(conn, int(row["id"]), now)
            failed = True
        else:
            user_id = int(row["id"])
            _reset_lockout(conn, user_id)
            ip = request.client.host if request.client else ""
            user_agent = request.headers.get("user-agent") or ""
            session = create_staff_session(
                conn,
                user_id=user_id,
                ip=ip,
                user_agent=user_agent,
                now=now,
            )
        conn.commit()
    finally:
        conn.close()

    if failed or session is None or user_id is None:
        _login_failed()

    user = get_user_by_id(int(user_id))
    if user is None or not user.active:
        _login_failed()

    _set_session_cookie(response, request, str(session["token"]))
    return {
        "ok": True,
        "authenticated": True,
        "user": _public_user(user, include_access=True),
        "csrf_token": str(session["csrf_secret"]),
        **_auth_state(),
    }


@staff_auth_router.post("/api/auth/logout")
def staff_logout(request: Request, response: Response):
    raw = _raw_session_token(request)
    if raw:
        with get_connection() as conn:
            revoke_staff_session(conn, raw)
            conn.commit()
    _clear_session_cookie(response, request)
    return {"ok": True, "authenticated": False, **_auth_state()}


@staff_auth_router.get("/api/auth/me")
def staff_me(request: Request, response: Response):
    raw = _raw_session_token(request)
    session = lookup_request_session(request, touch=True) if raw else None
    if raw and session is None:
        _clear_session_cookie(response, request)
        session = None

    if session is not None:
        user = get_user_by_id(int(session["user_id"]))
        if user is None or not user.active:
            _clear_session_cookie(response, request)
            with get_connection() as conn:
                revoke_staff_session(conn, raw)
                conn.commit()
            session = None
            user = None
        else:
            return {
                "authenticated": True,
                "user": _public_user(user, include_access=True),
                "csrf_token": str(session["csrf_secret"]),
                **_auth_state(),
            }

    return {
        "authenticated": False,
        "user": None,
        "csrf_token": "",
        **_auth_state(),
    }
