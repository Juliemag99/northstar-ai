"""Phase 0 staff authentication helpers (no login UI, no route protection).

Run: python test_staff_auth_phase0.py

Uses isolated database copies only — never writes production northstar.db,
never runs the bootstrap CLI against live data, and never stores a real
staff password in source.
"""

from __future__ import annotations

import testdb
import io
import os
import secrets
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from access import DEFAULT_USER_EMAIL, get_default_user
from auth_passwords import hash_password, password_issue, verify_password
from auth_sessions import (
    create_staff_session,
    ensure_staff_auth_schema,
    hash_session_token,
    lookup_staff_session,
    revoke_staff_session,
    revoke_staff_sessions_for_user,
)
import bootstrap_staff_admin as bsa
from bootstrap_staff_admin import (
    ALLOW_FLAG,
    FIRST_ADMIN_EMAIL,
    PASSWORD_ENV,
    bootstrap_first_admin,
    main as bootstrap_main,
    _read_password,
)
from db import PRODUCTION_DB_PATH, get_connection, migrate_schema

AUTH_COLUMNS = (
    "password_hash",
    "password_updated_at",
    "failed_login_count",
    "locked_until",
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret_password() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(r["name"])
        for r in conn.execute(f"PRAGMA index_list({table})").fetchall()
    }


def _copy_production_isolated() -> tuple[sqlite3.Connection, str]:
    handle, name = tempfile.mkstemp(prefix="northstar-auth-mig-", suffix=".db")
    os.close(handle)
    os.unlink(name)
    src = sqlite3.connect(str(PRODUCTION_DB_PATH))
    try:
        dst = sqlite3.connect(name)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    conn = get_connection(Path(name))
    return conn, name


def _cleanup_copy(conn: sqlite3.Connection | None, name: str) -> None:
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    for candidate in (name, name + "-wal", name + "-shm"):
        try:
            os.remove(candidate)
        except OSError:
            pass


def test_unauthenticated_app_unchanged() -> None:
    user = get_default_user()
    if user is None:
        _fail("Default user should still resolve without login.")
    if user.email.lower() != DEFAULT_USER_EMAIL:
        _fail(f"Default user drifted: {user.email}")
    if user.is_administrator:
        _fail("Julie must remain non-administrator until a later bootstrap on live data.")

    health_code, health = testdb.http_json("GET", "/health")
    if health_code != 200:
        _fail(f"GET /health should still work, got {health_code}: {health}")
    users_code, payload = testdb.http_json("GET", "/api/users/default")
    if users_code != 200:
        _fail(f"GET /api/users/default should still work, got {users_code}: {payload}")
    returned = payload.get("user") or {}
    if str(returned.get("email") or "").lower() != DEFAULT_USER_EMAIL:
        _fail("Default user API no longer returns Julie.")
    if "password_hash" in payload or "password_hash" in returned:
        _fail("User API leaked password_hash.")

    main_src = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
    if "AUTH_ENFORCE" in main_src:
        _fail("Phase 0 must not wire AUTH_ENFORCE into the application.")
    if "/api/auth/" in main_src or "bootstrap_staff_admin" in main_src:
        _fail("Phase 0 must not add login routes.")


def test_idempotent_migration() -> None:
    conn = None
    name = ""
    try:
        conn, name = _copy_production_isolated()
        before = _table_columns(conn, "users")
        migrate_schema(conn)
        after_first = _table_columns(conn, "users")
        for column in AUTH_COLUMNS:
            if column not in after_first:
                _fail(f"migrate_schema did not add users.{column}")
        session_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'staff_sessions'"
        ).fetchone()
        if session_table is None:
            _fail("migrate_schema did not create staff_sessions.")
        indexes = _index_names(conn, "staff_sessions")
        if "idx_staff_sessions_user" not in indexes:
            _fail("Missing idx_staff_sessions_user.")
        if "idx_staff_sessions_expires" not in indexes:
            _fail("Missing idx_staff_sessions_expires.")
        migrate_schema(conn)
        ensure_staff_auth_schema(conn)
        after_second = _table_columns(conn, "users")
        if after_second != after_first:
            _fail(f"Second migrate changed users columns: {sorted(after_first)} -> {sorted(after_second)}")
        if before and not AUTH_COLUMNS[0] in before:
            live_hash = conn.execute(
                "SELECT password_hash FROM users WHERE lower(email) = lower(?)",
                (DEFAULT_USER_EMAIL,),
            ).fetchone()
            if live_hash is None:
                _fail("Julie row missing from isolated production copy.")
            if str(live_hash["password_hash"] or "").strip():
                _fail("Isolated migrate stored a password hash without bootstrap.")
    finally:
        _cleanup_copy(conn, name)

    with get_connection() as testdb_conn:
        for column in AUTH_COLUMNS:
            if column not in _table_columns(testdb_conn, "users"):
                _fail(f"testdb copy missing users.{column}")


def test_password_hashing() -> None:
    password = _secret_password()
    digest = hash_password(password, email="phase0@example.test")
    if not digest.startswith("$argon2id$"):
        _fail("Expected Argon2id hash.")
    if password in digest or password.lower() in digest.lower():
        _fail("Password hash contained plaintext.")
    if not verify_password(password, digest):
        _fail("verify_password rejected the matching secret.")
    if verify_password(password + "x", digest):
        _fail("verify_password accepted a wrong secret.")
    if verify_password(password, ""):
        _fail("Empty hash must not verify.")
    if verify_password("", digest):
        _fail("Empty password must not verify.")
    for unsafe in ("password123", "admin", "northstar", "short", FIRST_ADMIN_EMAIL):
        issue = password_issue(unsafe, email=FIRST_ADMIN_EMAIL)
        if not issue:
            _fail(f"Unsafe password was accepted: {unsafe!r}")
        try:
            hash_password(unsafe, email=FIRST_ADMIN_EMAIL)
        except ValueError:
            pass
        else:
            _fail(f"hash_password accepted unsafe password: {unsafe!r}")


def test_sessions() -> None:
    password = _secret_password()
    with get_connection() as conn:
        email = f"phase0.session.{secrets.token_hex(4)}@example.test"
        conn.execute(
            """
            INSERT INTO users (email, full_name, is_administrator, active)
            VALUES (?, 'Phase 0 Session User', 0, 1)
            """,
            (email,),
        )
        user_id = int(
            conn.execute(
                "SELECT id FROM users WHERE email = ?",
                (email,),
            ).fetchone()["id"]
        )
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(password, email=email), user_id),
        )
        created = create_staff_session(conn, user_id=user_id, ip="127.0.0.1")
        raw = str(created["token"])
        row = conn.execute(
            "SELECT token_hash, csrf_secret FROM staff_sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            _fail("Session row was not stored.")
        if raw in str(row["token_hash"]) or password in str(row["token_hash"]):
            _fail("staff_sessions stored a raw token or password.")
        if str(row["token_hash"]) != hash_session_token(raw):
            _fail("token_hash is not SHA-256 of the raw token.")
        found = lookup_staff_session(conn, raw)
        if found is None or int(found["user_id"]) != user_id:
            _fail("Active session lookup failed.")
        if lookup_staff_session(conn, raw + "nope") is not None:
            _fail("Unknown token looked up as a session.")

        from auth_sessions import _parse

        created_at = _parse(created["created_at"])
        if created_at is None:
            _fail("Session created_at did not parse.")
        if lookup_staff_session(conn, raw, now=created_at + timedelta(hours=25), touch=False):
            _fail("Expired session still looked up.")
        idle = lookup_staff_session(conn, raw, now=created_at + timedelta(minutes=1))
        if idle is None:
            _fail("Fresh session should remain valid.")
        conn.execute(
            "UPDATE staff_sessions SET last_seen_at = ? WHERE user_id = ?",
            (created["created_at"], user_id),
        )
        if lookup_staff_session(
            conn, raw, now=created_at + timedelta(hours=13), touch=False
        ):
            _fail("Idle-expired session still looked up.")

        second = create_staff_session(conn, user_id=user_id)
        if not revoke_staff_session(conn, second["token"]):
            _fail("revoke_staff_session returned false.")
        if lookup_staff_session(conn, second["token"]) is not None:
            _fail("Revoked session still looked up.")
        third = create_staff_session(conn, user_id=user_id)
        revoked = revoke_staff_sessions_for_user(conn, user_id)
        if revoked < 1:
            _fail("revoke_staff_sessions_for_user did not revoke rows.")
        if lookup_staff_session(conn, third["token"]) is not None:
            _fail("User-wide revoke left a session active.")
        dump = " ".join(
            str(value)
            for row in conn.execute(
                "SELECT * FROM staff_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            for value in tuple(row)
        )
        if password in dump or raw in dump or second["token"] in dump:
            _fail("Plaintext secret found in staff_sessions rows.")
        conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def _pragma_path(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(str(row["file"])).resolve()


def test_bootstrap() -> None:
    password = _secret_password()
    other = _secret_password()
    if other == password:
        other = password + "Z1"

    previous_allow = os.environ.pop(ALLOW_FLAG, None)
    previous_pw = os.environ.pop(PASSWORD_ENV, None)
    try:
        if bootstrap_main([]) != 2:
            _fail("Bootstrap CLI must refuse without NORTHSTAR_ALLOW_ADMIN_BOOTSTRAP=1.")

        with get_connection() as conn:
            before = conn.execute(
                """
                SELECT id, password_hash, is_administrator, failed_login_count, locked_until
                FROM users WHERE lower(email) = lower(?)
                """,
                (FIRST_ADMIN_EMAIL,),
            ).fetchone()
            if before is None:
                _fail("First admin row missing from isolated testdb copy.")
            snapshot = dict(before)
            isolated_path = _pragma_path(conn)
            try:
                conn.execute(
                    "UPDATE users SET password_hash = '', is_administrator = 0 WHERE id = ?",
                    (int(snapshot["id"]),),
                )
                try:
                    bootstrap_first_admin(password, conn=conn)
                except ValueError as exc:
                    if ALLOW_FLAG not in str(exc):
                        _fail(f"Allow-flag refusal was unclear: {exc}")
                else:
                    _fail("bootstrap_first_admin skipped NORTHSTAR_ALLOW_ADMIN_BOOTSTRAP.")
                still_empty = conn.execute(
                    "SELECT password_hash, is_administrator FROM users WHERE id = ?",
                    (int(snapshot["id"]),),
                ).fetchone()
                if str(still_empty["password_hash"] or "").strip():
                    _fail("Bootstrap wrote a hash without the allow flag.")
                if int(still_empty["is_administrator"]):
                    _fail("Bootstrap promoted admin without the allow flag.")

                os.environ[ALLOW_FLAG] = "1"
                printed = io.StringIO()
                with redirect_stdout(printed):
                    result = bootstrap_first_admin(password, conn=conn)
                report = printed.getvalue()
                if str(isolated_path) not in report:
                    _fail("Bootstrap did not print the resolved database path.")
                if "LIVE production database" in report:
                    _fail("Isolated test copy was labeled LIVE.")
                if "isolated test database" not in report:
                    _fail("Bootstrap did not identify the test database.")
                if result.get("database_kind") != "isolated test database (not live northstar.db)":
                    _fail(f"Unexpected database_kind: {result.get('database_kind')}")
                if not result["ok"] or int(result["user_id"]) != int(snapshot["id"]):
                    _fail("Bootstrap create did not return the first admin.")
                row = conn.execute(
                    """
                    SELECT password_hash, is_administrator, password_updated_at
                    FROM users WHERE id = ?
                    """,
                    (int(snapshot["id"]),),
                ).fetchone()
                stored = str(row["password_hash"] or "")
                if not stored.startswith("$argon2id$"):
                    _fail("Bootstrap did not store an Argon2id hash.")
                if password in stored:
                    _fail("Bootstrap stored the plaintext password.")
                if not verify_password(password, stored):
                    _fail("Bootstrapped hash did not verify.")
                if not int(row["is_administrator"]):
                    _fail("Bootstrap did not promote the first admin.")
                try:
                    bootstrap_first_admin(other, conn=conn)
                except ValueError:
                    pass
                else:
                    _fail("Bootstrap replaced a hash without --reset.")
                session = create_staff_session(conn, user_id=int(snapshot["id"]))
                reset = bootstrap_first_admin(other, reset=True, conn=conn)
                if int(reset["sessions_revoked"]) < 1:
                    _fail("Password reset did not revoke sessions.")
                if lookup_staff_session(conn, session["token"]) is not None:
                    _fail("Reset left a session active.")
                new_hash = str(
                    conn.execute(
                        "SELECT password_hash FROM users WHERE id = ?",
                        (int(snapshot["id"]),),
                    ).fetchone()["password_hash"]
                )
                if verify_password(password, new_hash):
                    _fail("Reset left the previous password valid.")
                if not verify_password(other, new_hash):
                    _fail("Reset hash did not verify the new secret.")
            finally:
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?,
                        is_administrator = ?,
                        failed_login_count = ?,
                        locked_until = ?,
                        password_updated_at = ''
                    WHERE id = ?
                    """,
                    (
                        snapshot["password_hash"],
                        snapshot["is_administrator"],
                        snapshot["failed_login_count"],
                        snapshot["locked_until"],
                        int(snapshot["id"]),
                    ),
                )
                conn.execute(
                    "DELETE FROM staff_sessions WHERE user_id = ?",
                    (int(snapshot["id"]),),
                )

        os.environ[ALLOW_FLAG] = "1"
        os.environ[PASSWORD_ENV] = password
        # CLI uses get_connection() — testdb isolation, not live.
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET password_hash = '', is_administrator = 0 WHERE lower(email) = lower(?)",
                (FIRST_ADMIN_EMAIL,),
            )
        try:
            code = bootstrap_main([])
            if code != 0:
                _fail(f"Bootstrap CLI failed with {code}.")
            if PASSWORD_ENV in os.environ:
                _fail("NORTHSTAR_BOOTSTRAP_PASSWORD remained in the process environment.")
        finally:
            os.environ.pop(PASSWORD_ENV, None)
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT id, password_hash FROM users WHERE lower(email) = lower(?)",
                    (FIRST_ADMIN_EMAIL,),
                ).fetchone()
                stored = str(row["password_hash"] or "")
                if password in stored:
                    _fail("CLI stored plaintext password.")
                if not verify_password(password, stored):
                    _fail("CLI hash did not verify.")
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = '', is_administrator = 0,
                        password_updated_at = '', failed_login_count = 0, locked_until = ''
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
                conn.execute("DELETE FROM staff_sessions WHERE user_id = ?", (int(row["id"]),))
    finally:
        if previous_allow is None:
            os.environ.pop(ALLOW_FLAG, None)
        else:
            os.environ[ALLOW_FLAG] = previous_allow
        if previous_pw is None:
            os.environ.pop(PASSWORD_ENV, None)
        else:
            os.environ[PASSWORD_ENV] = previous_pw

    user = get_default_user()
    if user is None or user.email.lower() != DEFAULT_USER_EMAIL:
        _fail("Default user changed after bootstrap tests.")
    if user.is_administrator:
        _fail("Bootstrap tests leaked administrator promotion onto the default user.")


def test_bootstrap_protections() -> None:
    """Allow flag, path labeling, live confirmation, and env-var cleanup.

    Simulates a live path by pointing PRODUCTION_DB_PATH at the isolated copy.
    Never opens live northstar.db for bootstrap writes. Does not call input().
    """
    password = _secret_password()
    previous_allow = os.environ.pop(ALLOW_FLAG, None)
    previous_pw = os.environ.pop(PASSWORD_ENV, None)
    original_live = bsa.PRODUCTION_DB_PATH
    try:
        os.environ[PASSWORD_ENV] = password
        got = _read_password()
        if got != password:
            _fail("_read_password did not return the env password.")
        if PASSWORD_ENV in os.environ:
            _fail("_read_password left NORTHSTAR_BOOTSTRAP_PASSWORD in the environment.")

        os.environ[ALLOW_FLAG] = "1"
        with get_connection() as conn:
            before = conn.execute(
                """
                SELECT id, password_hash, is_administrator
                FROM users WHERE lower(email) = lower(?)
                """,
                (FIRST_ADMIN_EMAIL,),
            ).fetchone()
            if before is None:
                _fail("First admin row missing from isolated testdb copy.")
            snapshot = dict(before)
            isolated_path = _pragma_path(conn)
            if isolated_path.resolve() == Path(PRODUCTION_DB_PATH).resolve():
                _fail("Testdb isolation is pointing at live northstar.db.")
            conn.execute(
                "UPDATE users SET password_hash = '', is_administrator = 0 WHERE id = ?",
                (int(snapshot["id"]),),
            )
            try:
                bsa.PRODUCTION_DB_PATH = isolated_path
                printed = io.StringIO()
                with redirect_stdout(printed):
                    try:
                        bootstrap_first_admin(
                            password, conn=conn, confirm_live=False
                        )
                    except ValueError as exc:
                        if "live database without explicit confirmation" not in str(exc):
                            _fail(f"Live refusal was unclear: {exc}")
                    else:
                        _fail("Simulated live bootstrap wrote without confirmation.")
                report = printed.getvalue()
                if str(isolated_path) not in report:
                    _fail("Live-guard did not print the resolved database path.")
                if "LIVE production database" not in report:
                    _fail("Simulated live path was not identified as LIVE.")
                after = conn.execute(
                    "SELECT password_hash, is_administrator FROM users WHERE id = ?",
                    (int(snapshot["id"]),),
                ).fetchone()
                if str(after["password_hash"] or "").strip():
                    _fail("Unconfirmed live bootstrap stored a password hash.")
                if int(after["is_administrator"]):
                    _fail("Unconfirmed live bootstrap promoted the administrator.")
            finally:
                bsa.PRODUCTION_DB_PATH = original_live
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, is_administrator = ?
                    WHERE id = ?
                    """,
                    (
                        snapshot["password_hash"],
                        snapshot["is_administrator"],
                        int(snapshot["id"]),
                    ),
                )
    finally:
        bsa.PRODUCTION_DB_PATH = original_live
        if previous_allow is None:
            os.environ.pop(ALLOW_FLAG, None)
        else:
            os.environ[ALLOW_FLAG] = previous_allow
        if previous_pw is None:
            os.environ.pop(PASSWORD_ENV, None)
        else:
            os.environ[PASSWORD_ENV] = previous_pw


def test_live_database_not_migrated() -> None:
    live = sqlite3.connect(str(PRODUCTION_DB_PATH))
    try:
        names = {str(row[1]) for row in live.execute("PRAGMA table_info(users)").fetchall()}
        sessions = live.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'staff_sessions'"
        ).fetchone()
    finally:
        live.close()
    if "password_hash" in names or sessions is not None:
        _fail("Live northstar.db was migrated; Phase 0 must use isolated copies only.")


def main() -> int:
    test_unauthenticated_app_unchanged()
    test_idempotent_migration()
    test_password_hashing()
    test_sessions()
    test_bootstrap()
    test_bootstrap_protections()
    test_live_database_not_migrated()
    print("test_staff_auth_phase0: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
