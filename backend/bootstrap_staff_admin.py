"""One-time local bootstrap of the first NorthStar staff administrator.

Does not log, print, or commit the password. Requires
NORTHSTAR_ALLOW_ADMIN_BOOTSTRAP=1 in the local environment — including
library calls to bootstrap_first_admin(), not only the CLI.

Usage (from backend/, venv active):
  python bootstrap_staff_admin.py
  python bootstrap_staff_admin.py --reset

Optional: NORTHSTAR_BOOTSTRAP_PASSWORD for non-interactive local use.
It is removed from the process environment immediately after it is read.
Prefer interactive getpass. Never pass the password on the command line.

Live northstar.db writes require typing the exact resolved path.
Isolated tests never prompt: they use throwaway copies, or pass
confirm_live=False when simulating a live path.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from auth_passwords import hash_password, password_issue
from auth_sessions import ensure_staff_auth_schema, revoke_staff_sessions_for_user
from db import PRODUCTION_DB_PATH, get_connection

FIRST_ADMIN_EMAIL = "julie.magnani@northstargroup.com"
FIRST_ADMIN_NAME = "Julie Magnani"
ALLOW_FLAG = "NORTHSTAR_ALLOW_ADMIN_BOOTSTRAP"
PASSWORD_ENV = "NORTHSTAR_BOOTSTRAP_PASSWORD"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def bootstrap_allowed() -> bool:
    return os.environ.get(ALLOW_FLAG, "").strip() == "1"


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
            os.path.realpath(right)
        )
    except OSError:
        return False


def _is_live_database(path: Path) -> bool:
    if str(path) in {":memory:", ""}:
        return False
    return _same_file(path, Path(PRODUCTION_DB_PATH))


def _connection_path(conn) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    file_name = ""
    if row is not None:
        try:
            file_name = str(row["file"] or "")
        except (KeyError, IndexError, TypeError):
            file_name = str(row[2] if len(row) > 2 else "")
    if not file_name:
        return Path(":memory:")
    return Path(file_name).resolve()


def _database_kind(path: Path) -> str:
    if _is_live_database(path):
        return "LIVE production database"
    return "isolated test database (not live northstar.db)"


def _print_bootstrap_target(path: Path) -> None:
    print(f"Bootstrap database path: {path}")
    print(f"Bootstrap database kind: {_database_kind(path)}")


def _require_live_confirmation(path: Path, *, confirm_live: bool | None) -> None:
    """Refuse silent writes to live northstar.db. Tests must not call input()."""
    if not _is_live_database(path):
        return
    if confirm_live is True:
        return
    if confirm_live is False or not sys.stdin.isatty():
        raise ValueError(
            "Refusing to bootstrap the live database without explicit confirmation."
        )
    typed = input("Type the exact database path to confirm a LIVE bootstrap: ")
    if os.path.normcase(typed.strip()) != os.path.normcase(str(path)):
        raise ValueError("Live bootstrap confirmation did not match the database path.")


def bootstrap_first_admin(
    password: str,
    *,
    reset: bool = False,
    conn=None,
    confirm_live: bool | None = None,
) -> dict[str, object]:
    """Set Argon2 hash and promote the first admin. Never returns the password.

    Requires NORTHSTAR_ALLOW_ADMIN_BOOTSTRAP=1. Prints the resolved database
    path and whether it is live or a test copy before any write. Live
    northstar.db requires explicit confirmation; pass confirm_live=False from
    automated tests that simulate a live path so they never prompt.
    """
    if not bootstrap_allowed():
        raise ValueError(
            f"Refusing to bootstrap. Set {ALLOW_FLAG}=1 in the local environment."
        )
    issue = password_issue(password, email=FIRST_ADMIN_EMAIL)
    if issue:
        raise ValueError(issue)

    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        target = _connection_path(conn)
        _print_bootstrap_target(target)
        _require_live_confirmation(target, confirm_live=confirm_live)

        ensure_staff_auth_schema(conn)
        row = conn.execute(
            "SELECT id, email, password_hash, is_administrator FROM users WHERE lower(email) = lower(?)",
            (FIRST_ADMIN_EMAIL,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO users (
                    email, full_name, is_administrator, is_internal_northstar, active
                ) VALUES (?, ?, 1, 1, 1)
                """,
                (FIRST_ADMIN_EMAIL, FIRST_ADMIN_NAME),
            )
            row = conn.execute(
                "SELECT id, email, password_hash, is_administrator FROM users WHERE lower(email) = lower(?)",
                (FIRST_ADMIN_EMAIL,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to load the first administrator account.")

        stored = str(row["password_hash"] or "").strip()
        if stored and not reset:
            raise ValueError(
                "A password is already set. Re-run with --reset and "
                f"{ALLOW_FLAG}=1 to replace it."
            )

        digest = hash_password(password, email=FIRST_ADMIN_EMAIL)
        user_id = int(row["id"])
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?,
                password_updated_at = ?,
                is_administrator = 1,
                is_internal_northstar = 1,
                active = 1,
                failed_login_count = 0,
                locked_until = ''
            WHERE id = ?
            """,
            (digest, _now(), user_id),
        )
        revoked = 0
        if reset:
            revoked = revoke_staff_sessions_for_user(conn, user_id)
        if owns:
            conn.commit()
        return {
            "ok": True,
            "user_id": user_id,
            "email": FIRST_ADMIN_EMAIL,
            "reset": bool(reset),
            "sessions_revoked": int(revoked),
            "database_path": str(target),
            "database_kind": _database_kind(target),
        }
    finally:
        if owns:
            conn.close()


def _read_password() -> str:
    env_pw = os.environ.pop(PASSWORD_ENV, "")
    if env_pw:
        return env_pw
    first = getpass.getpass("New administrator password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise ValueError("Passwords do not match.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set the local NorthStar administrator password (no Git secrets)."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Replace an existing password and revoke active staff sessions.",
    )
    args = parser.parse_args(argv)
    if not bootstrap_allowed():
        print(
            f"Refusing to bootstrap. Set {ALLOW_FLAG}=1 in the local environment.",
            file=sys.stderr,
        )
        return 2
    try:
        password = _read_password()
        result = bootstrap_first_admin(password, reset=bool(args.reset))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    email = result["email"]
    print(f"password set for {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
