"""PW-2A: ordinary CRM names must not crash SQLite FTS5 MATCH.

Isolated testdb only. Never writes production northstar.db.
"""

from __future__ import annotations

import testdb  # noqa: F401

import secrets
import unittest
from pathlib import Path

from db import DB_PATH, PRODUCTION_DB_PATH, get_connection
from search_data import rebuild_search_index, safe_fts_match_query, search
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST

from fastapi.testclient import TestClient

from auth_http import CSRF_HEADER
from main import app


class SafeFtsQueryTests(unittest.TestCase):
    def test_operator_words_are_quoted_literals(self) -> None:
        q = safe_fts_match_query("BISON GEAR AND ENGINEERING")
        self.assertIn('"AND"*', q)
        self.assertIn('"BISON"*', q)
        self.assertIn('"ENGINEERING"*', q)
        self.assertNotIn("AND AND", q)

    def test_or_not_near_are_quoted(self) -> None:
        for raw in ("Johnson OR Controls", "NOT Smith", "NEAR steel"):
            q = safe_fts_match_query(raw)
            self.assertTrue(q)
            self.assertNotIn("syntax", q.lower())

    def test_punctuation_company_names(self) -> None:
        cases = (
            "A. O. Smith",
            "Smith & Nephew",
            "Duke & Son",
            "B&W",
            "O'Reilly",
            "ABC/XYZ",
            "Company (USA)",
            '"quoted company"',
        )
        for raw in cases:
            q = safe_fts_match_query(raw)
            self.assertTrue(q, raw)
            self.assertNotIn("(", q)
            self.assertNotIn(")", q)
            self.assertNotIn("&", q)

    def test_blank_and_whitespace(self) -> None:
        self.assertEqual(safe_fts_match_query(""), "")
        self.assertEqual(safe_fts_match_query("   "), "")
        self.assertEqual(safe_fts_match_query("***"), "")

    def test_single_and_multiple_tokens(self) -> None:
        self.assertEqual(safe_fts_match_query("Valmont"), '"Valmont"*')
        self.assertIn(" AND ", safe_fts_match_query("Johnson Controls"))

    def test_numeric_phone_email(self) -> None:
        self.assertTrue(safe_fts_match_query("812142"))
        self.assertTrue(safe_fts_match_query("419-555-0100"))
        self.assertTrue(safe_fts_match_query("buyer@example.com"))


class SafeFtsSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(DB_PATH).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())

    def test_bison_and_engineering_does_not_500(self) -> None:
        with get_connection() as conn:
            rebuild_search_index(conn)
            conn.commit()
            admin = conn.execute(
                "SELECT id FROM users WHERE is_administrator = 1 AND active = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        result = search("BISON GEAR AND ENGINEERING", user_id=int(admin["id"]))
        self.assertEqual(result.query, "BISON GEAR AND ENGINEERING")
        self.assertIsInstance(result.total, int)

    def test_punctuation_searches_do_not_raise(self) -> None:
        with get_connection() as conn:
            admin = conn.execute(
                "SELECT id FROM users WHERE is_administrator = 1 AND active = 1 ORDER BY id LIMIT 1"
            ).fetchone()
        uid = int(admin["id"])
        for raw in (
            "Johnson Controls",
            "A. O. Smith",
            "Smith & Nephew",
            "O'Reilly",
            "ABC/XYZ",
            "Company (USA)",
            '"quoted company"',
            "812142",
        ):
            result = search(raw, user_id=uid)
            self.assertEqual(result.query, raw)


class SearchClientScopeTests(unittest.TestCase):
    def test_specialist_foreign_client_is_403(self) -> None:
        suffix = secrets.token_hex(3)
        email = f"pw2a.search.{suffix}@northstar.example.test"
        password = f"NsTest9{secrets.token_hex(10)}"
        with get_connection() as conn:
            brown = conn.execute(
                "SELECT id FROM clients WHERE lower(code) = 'brown'"
            ).fetchone()
            dawson = conn.execute(
                "SELECT id FROM clients WHERE lower(code) = 'dawson'"
            ).fetchone()
        self.assertIsNotNone(brown)
        self.assertIsNotNone(dawson)
        provision_staff_user(
            first_name="Search",
            last_name="Scope",
            email=email,
            password=password,
            staff_role=REVOPS_SPECIALIST,
            client_ids=[int(brown["id"])],
        )
        http = TestClient(app)
        login = http.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(login.status_code, 200, login.text)
        csrf = str(login.json().get("csrf_token") or "")
        headers = {CSRF_HEADER: csrf}
        own = http.get(
            f"/api/search?q=BISON&client_id={int(brown['id'])}",
            headers=headers,
        )
        self.assertEqual(own.status_code, 200, own.text)
        foreign = http.get(
            f"/api/search?q=BISON&client_id={int(dawson['id'])}",
            headers=headers,
        )
        self.assertEqual(foreign.status_code, 403, foreign.text)


if __name__ == "__main__":
    unittest.main()
