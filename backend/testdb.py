"""Isolate automated tests from production northstar.db.

Import this module before any other NorthStar backend module in test files.
HTTP helpers use FastAPI TestClient against the isolated copy and never write
through the live uvicorn process on port 8007.
"""

from __future__ import annotations

from typing import Any

from db import (
    DEFAULT_USER_EMAIL,
    ensure_zoominfo_columns,
    get_connection,
    isolate_for_tests,
    migrate_schema,
)

isolate_for_tests()
with get_connection() as conn:
    ensure_zoominfo_columns(conn)
    migrate_schema(conn)
    # Isolated copies of live data still have the pre-cutover email. Align id 1
    # to the canonical staff email so default-user lookups match application code.
    conn.execute(
        """
        UPDATE users
        SET email = ?
        WHERE id = 1
          AND lower(trim(COALESCE(email, ''))) != lower(?)
        """,
        (DEFAULT_USER_EMAIL, DEFAULT_USER_EMAIL),
    )
    conn.commit()

_client = None


def test_client():
    global _client
    if _client is None:
        from fastapi.testclient import TestClient
        from main import app

        _client = TestClient(app)
    return _client


def http_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """In-process HTTP against the isolated test database."""
    client = test_client()
    verb = method.upper()
    if verb == "GET":
        resp = client.get(path)
    elif verb == "POST":
        resp = client.post(path, json=body)
    elif verb == "PATCH":
        resp = client.patch(path, json=body)
    elif verb == "PUT":
        resp = client.put(path, json=body)
    elif verb == "DELETE":
        resp = client.delete(path)
    else:
        raise ValueError(f"Unsupported HTTP method {method!r}")
    try:
        payload: Any = resp.json()
    except Exception:
        payload = {"detail": resp.text}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"data": payload}
    return int(resp.status_code), payload
