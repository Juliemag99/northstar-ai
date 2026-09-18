"""Request-scoped authenticated staff actor.

HTTP middleware binds the session user when a valid staff session exists.
Interactive CRM then uses that user instead of get_default_user().

Fallback to the default user (Julie) happens only when:
- no session is bound, AND
- NORTHSTAR_AUTH_ENFORCE is not active

That fallback is the local/unenforced developer path. Shared pilot and
AUTH_ENFORCE=1 never impersonate Julie for a missing or other user's session.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from models import NorthStarUser

_staff_actor: ContextVar[NorthStarUser | None] = ContextVar(
    "northstar_staff_actor", default=None
)


def current_staff_actor() -> NorthStarUser | None:
    return _staff_actor.get()


def bind_staff_actor(user: NorthStarUser | None) -> Token:
    return _staff_actor.set(user)


def reset_staff_actor(token: Token) -> None:
    _staff_actor.reset(token)


def resolve_staff_actor(user_id: int | None = None) -> NorthStarUser | None:
    """Authoritative actor for the current operation.

    Bound session user always wins (browser-supplied user_id cannot spoof).
    Direct data-layer tests without HTTP may pass user_id.
    Unenforced / no session falls back to get_default_user().
    """
    bound = current_staff_actor()
    if bound is not None:
        return bound
    from access import get_default_user, get_user_by_id

    if user_id is not None:
        found = get_user_by_id(int(user_id))
        if found is not None:
            return found
    return get_default_user()


def require_staff_actor(user_id: int | None = None) -> NorthStarUser:
    user = resolve_staff_actor(user_id)
    if user is None or not user.active:
        raise PermissionError("Authentication required.")
    return user
