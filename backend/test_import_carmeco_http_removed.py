"""POST /api/import/carmeco must not exist on the HTTP API.

Run: python test_import_carmeco_http_removed.py

Uses the isolated testdb copy — never writes through live :8007 and never
opens production northstar.db. Does not call the CLI importer.
Does not change Flora Jia, Whirlpool, or other production identity values.
"""

from __future__ import annotations

import testdb
import sys
from unittest.mock import patch

from db import get_connection

FLORA_ID = 4631
WHIRLPOOL_ID = 298
IMPORT_PATH = "/api/import/carmeco"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _counts(conn) -> tuple[int, int, int]:
    companies = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
    contacts = int(conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"])
    clients = int(conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"])
    return companies, contacts, clients


def _flora_snapshot(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, email
        FROM contacts
        WHERE id = ?
        """,
        (FLORA_ID,),
    ).fetchone()
    return dict(row) if row is not None else None


def main() -> int:
    try:
        with get_connection() as conn:
            before = _counts(conn)
            flora_before = _flora_snapshot(conn)
            whirlpool = conn.execute(
                "SELECT id FROM companies WHERE id = ?",
                (WHIRLPOOL_ID,),
            ).fetchone()

        with (
            patch("import_carmeco.import_carmeco") as mock_import,
            patch("db.reset_database") as mock_reset,
        ):
            code, payload = testdb.http_json("POST", IMPORT_PATH, {})
            if code != 404:
                _fail(f"POST {IMPORT_PATH} must be unavailable, got {code}: {payload}")
            get_code, _ = testdb.http_json("GET", IMPORT_PATH)
            if get_code != 404:
                _fail(f"GET {IMPORT_PATH} must be unavailable, got {get_code}")
            if mock_import.called:
                _fail("POST /api/import/carmeco must not call import_carmeco().")
            if mock_reset.called:
                _fail("POST /api/import/carmeco must not call reset_database().")

        client = testdb.test_client()
        spec = client.get("/openapi.json")
        if spec.status_code != 200:
            _fail(f"GET /openapi.json failed ({spec.status_code}).")
        paths = spec.json().get("paths") or {}
        if IMPORT_PATH in paths:
            _fail("POST /api/import/carmeco is still listed in OpenAPI paths.")
        for route in client.app.routes:
            path = getattr(route, "path", "")
            if path == IMPORT_PATH:
                _fail("FastAPI still registers /api/import/carmeco.")

        health_code, health = testdb.http_json("GET", "/health")
        if health_code != 200 or health.get("status") != "healthy":
            _fail(f"GET /health should still work, got {health_code}: {health}")
        home_code, home = testdb.http_json("GET", "/")
        if home_code != 200 or home.get("application") != "NorthStar AI":
            _fail(f"GET / should still work, got {home_code}: {home}")
        users_code, users = testdb.http_json("GET", "/api/users/default")
        if users_code != 200 or not users.get("user"):
            _fail(f"GET /api/users/default should still work, got {users_code}: {users}")
        clients = users.get("clients") or []
        if not any(int(c.get("client_id") or 0) == 1 for c in clients):
            _fail("Default user clients should still include Carmeco (id=1).")
        if not any(int(c.get("client_id") or 0) == 2 for c in clients):
            _fail("Default user clients should still include Brown Industries (id=2).")

        with get_connection() as conn:
            after = _counts(conn)
            flora_after = _flora_snapshot(conn)
            whirlpool_after = conn.execute(
                "SELECT id FROM companies WHERE id = ?",
                (WHIRLPOOL_ID,),
            ).fetchone()
        if after != before:
            _fail(f"Import HTTP call changed isolated DB counts {before} -> {after}.")
        if flora_before != flora_after:
            _fail("Flora Jia contact row changed.")
        if (whirlpool is None) != (whirlpool_after is None):
            _fail("Whirlpool company presence changed.")

        print("PASS: POST /api/import/carmeco is unavailable and does not import.")
        print("      Route is absent from OpenAPI. Health and default-user APIs still work.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
