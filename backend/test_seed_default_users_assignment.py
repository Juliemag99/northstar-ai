"""Startup must not assign unowned Carmeco or Brown CRM records.

Uses a blank temporary database. Never opens database/northstar.db.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-seed-users-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from db import (  # noqa: E402
    DEFAULT_USER_EMAIL,
    PRODUCTION_DB_PATH,
    get_connection,
    init_schema,
    seed_default_users,
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _assert_isolated(conn: sqlite3.Connection) -> None:
    opened = Path(str(conn.execute("PRAGMA database_list").fetchone()["file"] or ""))
    if opened.resolve() == PRODUCTION_DB_PATH.resolve():
        _fail("Test opened production northstar.db.")
    if opened.resolve() != Path(_TEST_DB).resolve():
        _fail(f"Test opened {opened}, not the isolated database.")
    if "northstar.db" in opened.name:
        _fail("Test database name must not be northstar.db.")


def _seed_crm(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute("INSERT INTO clients (code, name) VALUES ('carmeco', 'Carmeco')")
    conn.execute("INSERT INTO clients (code, name) VALUES ('brown', 'Brown Industries')")
    conn.execute(
        "INSERT INTO companies (external_record_no, company_name) VALUES ('900001', 'Pilot Co A')"
    )
    conn.execute(
        "INSERT INTO companies (external_record_no, company_name) VALUES ('900002', 'Pilot Co B')"
    )
    clients = {
        str(row["code"]): int(row["id"])
        for row in conn.execute("SELECT id, code FROM clients").fetchall()
    }
    companies = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM companies ORDER BY id").fetchall()
    ]
    for code, company_id in (("carmeco", companies[0]), ("brown", companies[1])):
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, status, assigned_user_id, next_action, notes, is_hot
            ) VALUES (?, ?, 'New', NULL, 'Initial outreach', '', 0)
            """,
            (clients[code], company_id),
        )
        relationship_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO activities (
                client_id, company_id, relationship_id, external_record_no,
                user_id, activity_type, activity_at
            ) VALUES (?, ?, ?, ?, NULL, 'Note', '2026-10-05T00:00:00Z')
            """,
            (clients[code], company_id, relationship_id, f"act-{code}"),
        )
    conn.commit()
    return clients


def _ownership(conn: sqlite3.Connection) -> list[tuple[str, object, object]]:
    return [
        (str(row["code"]), row["assigned_user_id"], row["user_id"])
        for row in conn.execute(
            """
            SELECT c.code, r.assigned_user_id, a.user_id
            FROM clients c
            JOIN client_company_relationships r ON r.client_id = c.id
            JOIN activities a ON a.client_id = c.id AND a.relationship_id = r.id
            ORDER BY c.code
            """
        ).fetchall()
    ]


def test_unowned_carmeco_and_brown_records_stay_unowned() -> None:
    with get_connection() as conn:
        _assert_isolated(conn)
        init_schema(conn)
        _seed_crm(conn)
        before = _ownership(conn)
        if before != [("brown", None, None), ("carmeco", None, None)]:
            _fail(f"Setup ownership was {before}")
        seed_default_users(conn)
        after = _ownership(conn)
        if after != before:
            _fail(f"seed_default_users assigned unowned records: {after}")
        seed_default_users(conn)
        if _ownership(conn) != before:
            _fail("A second startup seed assigned unowned records.")


def test_required_bootstrap_still_runs() -> None:
    with get_connection() as conn:
        _assert_isolated(conn)
        julie = conn.execute(
            "SELECT id, email, full_name, active FROM users WHERE id = 1"
        ).fetchone()
        if julie is None:
            _fail("Default user id 1 was not created.")
        if str(julie["email"]) != DEFAULT_USER_EMAIL:
            _fail(f"Default user email is {julie['email']}.")
        if str(julie["full_name"]) != "Julie Magnani" or int(julie["active"]) != 1:
            _fail("Default user was not created as active Julie Magnani.")
        admin = conn.execute(
            """
            SELECT full_name, is_administrator, active
            FROM users WHERE email = 'admin@northstargroup.com'
            """
        ).fetchone()
        if admin is None:
            _fail("NorthStar Admin was not created.")
        if (
            str(admin["full_name"]) != "NorthStar Admin"
            or int(admin["is_administrator"]) != 1
            or int(admin["active"]) != 1
        ):
            _fail("NorthStar Admin bootstrap row is wrong.")
        rows = conn.execute(
            """
            SELECT c.code, a.role, a.active
            FROM user_client_assignments a
            JOIN clients c ON c.id = a.client_id
            WHERE a.user_id = 1
            ORDER BY c.code
            """
        ).fetchall()
        found = [(str(row["code"]), str(row["role"]), int(row["active"])) for row in rows]
        if found != [("brown", "account_executive", 1), ("carmeco", "account_executive", 1)]:
            _fail(f"Client assignments were {found}")


def main() -> None:
    if Path(_TEST_DB).resolve() == PRODUCTION_DB_PATH.resolve():
        _fail("Isolated path resolved to production northstar.db.")
    test_unowned_carmeco_and_brown_records_stay_unowned()
    print("PASS test_unowned_carmeco_and_brown_records_stay_unowned")
    test_required_bootstrap_still_runs()
    print("PASS test_required_bootstrap_still_runs")
    print("PASS 2 seed default user assignment tests")


if __name__ == "__main__":
    try:
        main()
    finally:
        for candidate in (_TEST_DB, _TEST_DB + "-wal", _TEST_DB + "-shm"):
            try:
                os.remove(candidate)
            except OSError:
                pass
