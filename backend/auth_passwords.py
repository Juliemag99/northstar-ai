"""Argon2id password hashing for NorthStar staff accounts.

Never stores, logs, or returns plaintext passwords. Empty hash means
the user cannot log in.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher()

# Obvious / default values only. Tests generate their own strong secrets.
_UNSAFE_LOWER = frozenset(
    {
        "password",
        "password123",
        "password1234",
        "password12345",
        "northstar",
        "northstar1",
        "northstar123",
        "admin",
        "admin123",
        "administrator",
        "julie",
        "juliemagnani",
        "letmein",
        "welcome",
        "changeme",
        "123456789012",
        "qwertyuiopas",
        "passw0rd",
        "default",
    }
)

MIN_PASSWORD_LENGTH = 12


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value)


def password_issue(password: str, *, email: str = "") -> str | None:
    """Return a reason the password is rejected, or None if acceptable."""
    if password is None or not isinstance(password, str):
        return "Password is required."
    if password != password.strip() or "\x00" in password:
        return "Password cannot start or end with spaces."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if not password.isprintable():
        return "Password contains unsupported characters."
    lowered = password.lower()
    if lowered in _UNSAFE_LOWER:
        return "That password is too common."
    email_norm = _blank(email).strip().lower()
    if email_norm and lowered == email_norm:
        return "Password cannot match the account email."
    local = email_norm.split("@", 1)[0] if "@" in email_norm else email_norm
    if local and len(local) >= 4 and lowered == local:
        return "Password cannot match the account email."
    if password.isdigit() or password.isalpha():
        return "Password must include letters and numbers."
    has_letter = any(ch.isalpha() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    if not (has_letter and has_digit):
        return "Password must include letters and numbers."
    return None


def hash_password(password: str, *, email: str = "") -> str:
    issue = password_issue(password, email=email)
    if issue:
        raise ValueError(issue)
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    stored = _blank(password_hash).strip()
    if not stored or not password:
        return False
    try:
        return bool(_HASHER.verify(stored, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    stored = _blank(password_hash).strip()
    if not stored:
        return False
    try:
        return bool(_HASHER.check_needs_rehash(stored))
    except (InvalidHashError, VerificationError):
        return False
