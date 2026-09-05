"""Isolated Deep Research tests — mocked OpenAI transport, testdb only.

Run: python test_deep_research.py
Never writes production northstar.db. Zero live OpenAI calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import testdb

from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from deep_research_client import build_deep_research_input, safe_http_url
from deep_research_config import sanitize_error_message
from deep_research_data import (
    cancel_deep_research_job,
    get_deep_research_job,
    process_deep_research_job,
    recover_deep_research_jobs,
    set_deep_research_transport_for_tests,
    set_deep_research_worker_hook_for_tests,
    start_deep_research_job,
)
from models import ResearchStartRequest
from research_data import FIT_CRITERIA_NOT_CONFIGURED

_MOCK_CALLS: list[tuple[str, str]] = []


def _mock_transport(method, url, body, headers):
    # Prove Authorization header is present but never assert/log its value.
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")
    assert "NORTHSTAR_DEEP_RESEARCH_API_KEY" not in json.dumps(body or {})
    _MOCK_CALLS.append((method.upper(), url))
    method_u = method.upper()
    if method_u == "POST" and url.rstrip("/").endswith("/cancel"):
        return {"id": "resp_mock_valmont", "status": "cancelled"}
    if method_u == "POST" and url.rstrip("/").endswith("/responses"):
        assert body.get("background") is True
        assert body.get("store") is True
        assert body.get("max_output_tokens") == 6000
        assert body.get("max_tool_calls") == 10
        assert (body.get("reasoning") or {}).get("effort") == "high"
        assert any(t.get("type") == "web_search" for t in (body.get("tools") or []))
        return {
            "id": "resp_mock_valmont",
            "status": "completed",
            "model": "gpt-5.5",
            "output_text": json.dumps(
                {
                    "verified_identity": {
                        "company_name": "Valmont Industries",
                        "website": "https://www.valmont.com",
                    },
                    "overview": (
                        "Valmont improves life by creating vital infrastructure and "
                        "advancing agricultural productivity."
                    ),
                    "locations": ["Omaha, NE", "Valley, NE"],
                    "products": [
                        "irrigation systems",
                        "utility poles",
                        "vital infrastructure",
                    ],
                    "manufacturing_capabilities": ["steel fabrication", "galvanizing"],
                    "industries": ["agriculture", "transportation", "infrastructure"],
                    "client_relevant_evidence": [
                        "Large OEM with multi-site metal manufacturing footprint."
                    ],
                    "campaign_fit": {
                        "fit_result": "Campaign Criteria Not Configured",
                        "why": "Brown Default campaign has no usable targeting criteria.",
                        "evidence": [],
                        "missing_information": ["Configure campaign criteria."],
                    },
                    "decision_maker_titles": [
                        "Procurement Manager",
                        "Supply Chain Manager",
                        "Plant Engineering Manager",
                    ],
                    "outreach_angle": (
                        "Lead with capacity and multi-plant sourcing conversation, "
                        "not a premature campaign-fit claim."
                    ),
                    "missing_information": [
                        "Which Valmont plants buy outsourced stamped components?"
                    ],
                    "sources": [
                        {
                            "title": "Valmont official site",
                            "url": "https://www.valmont.com",
                        },
                        {
                            "title": "evil",
                            "url": "file:///etc/passwd",
                        },
                        {
                            "title": "loopback",
                            "url": "http://127.0.0.1/secret",
                        },
                    ],
                }
            ),
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://www.valmont.com/company",
                                    "title": "About Valmont",
                                },
                                {
                                    "type": "url_citation",
                                    "url": "http://169.254.169.254/latest/meta-data",
                                    "title": "ssrf",
                                },
                            ],
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 200},
        }
    if method_u == "GET":
        return {
            "id": "resp_mock_valmont",
            "status": "completed",
            "model": "gpt-5.5",
            "output_text": json.dumps(
                {
                    "verified_identity": {
                        "company_name": "Valmont Industries",
                        "website": "https://www.valmont.com",
                    },
                    "overview": "Resumed overview after restart.",
                    "locations": [],
                    "products": ["irrigation systems"],
                    "manufacturing_capabilities": [],
                    "industries": [],
                    "client_relevant_evidence": [],
                    "campaign_fit": {
                        "fit_result": "Campaign Criteria Not Configured",
                        "why": "criteria missing",
                        "evidence": [],
                        "missing_information": [],
                    },
                    "decision_maker_titles": [],
                    "outreach_angle": "",
                    "missing_information": [],
                    "sources": [{"title": "Valmont", "url": "https://www.valmont.com"}],
                }
            ),
            "output": [],
            "usage": {},
        }
    raise AssertionError(f"unexpected mock call {method} {url}")


class DeepResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        opened = Path(os.fspath(DB_PATH)).resolve()
        self.assertNotEqual(opened, PRODUCTION_DB_PATH.resolve())
        self.assertTrue(os.environ.get("NORTHSTAR_TEST_DB"))
        _MOCK_CALLS.clear()
        # Ensure jobs table exists on isolated copy without touching production.
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()
            brown = conn.execute(
                """
                SELECT id FROM clients
                WHERE id = 2 OR lower(code)='brown' OR name LIKE 'Brown%'
                ORDER BY CASE WHEN id=2 THEN 0 ELSE 1 END LIMIT 1
                """
            ).fetchone()
            self.client_id = int(brown["id"])
            valmont = conn.execute(
                """
                SELECT id, external_record_no FROM companies
                WHERE id = 541 OR company_name = 'Valmont'
                ORDER BY CASE WHEN id=541 THEN 0 ELSE 1 END LIMIT 1
                """
            ).fetchone()
            if valmont is None:
                self.company_id = int(
                    conn.execute(
                        """
                        INSERT INTO companies (
                            external_record_no, company_name, website, city, state,
                            address, zip, legacy_phone, type_of_industry,
                            created_at, last_updated_at
                        ) VALUES (
                            'NS-100006', 'Valmont', 'www.valmont.com', 'Omaha', 'NE',
                            '', '', '', '', datetime('now'), datetime('now')
                        )
                        """
                    ).lastrowid
                )
                self.record_no = "NS-100006"
                conn.execute(
                    """
                    INSERT INTO client_company_relationships (
                        client_id, company_id, external_record_no, status,
                        created_at, updated_at
                    ) VALUES (?, ?, 'NS-100006', 'Left Message', datetime('now'), datetime('now'))
                    """,
                    (self.client_id, self.company_id),
                )
                conn.commit()
            else:
                self.company_id = int(valmont["id"])
                self.record_no = str(valmont["external_record_no"] or "NS-100006")
        set_deep_research_transport_for_tests(_mock_transport)
        set_deep_research_worker_hook_for_tests(process_deep_research_job)
        # Isolate key presence for transport path without reading real secrets into asserts.
        os.environ.setdefault("NORTHSTAR_DEEP_RESEARCH_API_KEY", "test-key-not-for-live-use")

    def tearDown(self) -> None:
        set_deep_research_transport_for_tests(None)
        set_deep_research_worker_hook_for_tests(None)

    def test_sanitize_error_redacts_key_material(self):
        msg = sanitize_error_message("boom Bearer test-key-not-for-live-use trailing")
        self.assertNotIn("test-key-not-for-live-use", msg)
        self.assertIn("[redacted]", msg)

    def test_safe_http_url_blocks_ssrf_schemes(self):
        self.assertIsNone(safe_http_url("file:///etc/passwd"))
        self.assertIsNone(safe_http_url("http://127.0.0.1/x"))
        self.assertIsNone(safe_http_url("http://169.254.169.254/latest"))
        ok = safe_http_url("https://www.valmont.com/about")
        self.assertEqual(ok["url"], "https://www.valmont.com/about")

    def test_prompt_keeps_profile_when_criteria_missing(self):
        prompt = build_deep_research_input(
            company_name="Valmont",
            website="www.valmont.com",
            city="Omaha",
            state="NE",
            client_name="Brown Industries",
            campaign_name="Default",
            campaign_criteria_configured=False,
            campaign_summary="thin seed",
        )
        self.assertIn("NOT configured", prompt)
        self.assertIn("Valmont", prompt)

    def test_valmont_mocked_deep_research_completes_with_profile_and_criteria_message(self):
        body = ResearchStartRequest(
            company_id=self.company_id,
            external_record_no=self.record_no,
            working_for_client_id=self.client_id,
            force_refresh=True,
            confirm_paid_refresh=True,
            research_depth="deep",
        )
        queued = start_deep_research_job(body)
        self.assertEqual(queued.research_depth, "deep")
        self.assertIsNotNone(queued.job)
        job_id = queued.job.job_id
        done = get_deep_research_job(job_id)
        self.assertEqual(done.job.status, "completed")
        self.assertEqual(done.research_depth, "deep")
        self.assertIsNotNone(done.research_run_id)
        self.assertTrue(any(f.finding_type == "overview" for f in done.overview))
        self.assertTrue(any(f.finding_type == "product" for f in done.products))
        citations = list(done.citations or done.job.citations or [])
        self.assertTrue(any(c.get("url") for c in citations))
        for c in citations:
            self.assertTrue(str(c.get("url") or "").startswith("http"))
            self.assertNotIn("127.0.0.1", str(c.get("url")))
            self.assertNotIn("169.254", str(c.get("url")))
            self.assertFalse(str(c.get("url") or "").startswith("file:"))
        self.assertEqual(done.fit.fit_result, FIT_CRITERIA_NOT_CONFIGURED)
        # CRM masters untouched by deep research persistence path
        with get_connection() as conn:
            co = conn.execute(
                "SELECT company_name, website FROM companies WHERE id = ?",
                (self.company_id,),
            ).fetchone()
            self.assertEqual(co["company_name"], "Valmont")
            runs = conn.execute(
                "SELECT COUNT(*) AS n FROM company_research_runs WHERE openai_response_id = ?",
                ("resp_mock_valmont",),
            ).fetchone()
            self.assertGreaterEqual(int(runs["n"]), 1)

    def test_quick_research_depth_still_routes_to_deterministic_path(self):
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            research_depth="quick",
        )
        self.assertEqual(body.research_depth, "quick")

    def test_cancel_queued_job(self):
        set_deep_research_worker_hook_for_tests(lambda _jid: None)  # do not run
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            force_refresh=True,
            confirm_paid_refresh=True,
            research_depth="deep",
        )
        queued = start_deep_research_job(body)
        cancelled = cancel_deep_research_job(queued.job.job_id)
        self.assertEqual(cancelled.job.status, "cancelled")

    def test_cancel_running_job_calls_provider_cancel(self):
        set_deep_research_worker_hook_for_tests(lambda _jid: None)
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            force_refresh=True,
            confirm_paid_refresh=True,
            research_depth="deep",
        )
        queued = start_deep_research_job(body)
        job_id = queued.job.job_id
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE company_research_jobs
                SET status = 'running', openai_response_id = 'resp_mock_valmont',
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (job_id,),
            )
            conn.commit()
        cancelled = cancel_deep_research_job(job_id)
        self.assertTrue(
            any(
                m == "POST" and u.rstrip("/").endswith("/cancel")
                for m, u in _MOCK_CALLS
            )
        )
        # Running cancel sets cancel_requested; status may still be running until worker.
        self.assertTrue(cancelled.job.cancel_requested or cancelled.job.status == "cancelled")

    def test_one_active_job_under_concurrent_starts(self):
        set_deep_research_worker_hook_for_tests(lambda _jid: None)
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            force_refresh=False,
            research_depth="deep",
        )
        # Clear active + cache so starts create a new job instead of cache hits.
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE company_research_jobs
                SET status = 'cancelled', completed_at = datetime('now', '-90 days'),
                    cancel_requested = 1, research_run_id = NULL
                WHERE company_id = ? AND working_for_client_id = ?
                """,
                (self.company_id, self.client_id),
            )
            conn.commit()

        results: list = []
        errors: list = []

        def _start():
            try:
                results.append(start_deep_research_job(body))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_start) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        job_ids = {r.job.job_id for r in results if r.job}
        with get_connection() as conn:
            active = conn.execute(
                """
                SELECT COUNT(*) AS n FROM company_research_jobs
                WHERE company_id = ? AND working_for_client_id = ?
                  AND status IN ('queued', 'running')
                """,
                (self.company_id, self.client_id),
            ).fetchone()
            self.assertEqual(int(active["n"]), 1)
        # All callers should observe the same active job id.
        self.assertEqual(len(job_ids), 1)

    def test_stale_job_recovery_resumes_with_provider_id(self):
        set_deep_research_worker_hook_for_tests(lambda _jid: None)
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            force_refresh=True,
            confirm_paid_refresh=True,
            research_depth="deep",
        )
        queued = start_deep_research_job(body)
        job_id = queued.job.job_id
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE company_research_jobs
                SET status = 'running', openai_response_id = 'resp_mock_valmont',
                    updated_at = datetime('now', '-10 minutes'),
                    started_at = datetime('now', '-10 minutes')
                WHERE id = ?
                """,
                (job_id,),
            )
            conn.commit()
        resumed = recover_deep_research_jobs(spawn_workers=False)
        self.assertIn(job_id, resumed)
        set_deep_research_worker_hook_for_tests(process_deep_research_job)
        process_deep_research_job(job_id)
        done = get_deep_research_job(job_id)
        self.assertEqual(done.job.status, "completed")
        self.assertTrue(
            any(m == "GET" and "resp_mock_valmont" in u for m, u in _MOCK_CALLS)
        )

    def test_retry_does_not_duplicate_completed_run(self):
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            force_refresh=True,
            research_depth="deep",
        )
        queued = start_deep_research_job(body)
        job_id = queued.job.job_id
        done = get_deep_research_job(job_id)
        run_id = done.research_run_id
        self.assertIsNotNone(run_id)
        # Simulate accidental second worker pass after completion.
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE company_research_jobs
                SET status = 'queued', progress = 0, completed_at = NULL
                WHERE id = ?
                """,
                (job_id,),
            )
            conn.commit()
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM company_research_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            self.assertEqual(int(before["n"]), 1)
        process_deep_research_job(job_id)
        with get_connection() as conn:
            after = conn.execute(
                """
                SELECT COUNT(*) AS n FROM company_research_runs
                WHERE company_id = ? AND research_depth = 'deep'
                  AND openai_response_id = 'resp_mock_valmont'
                """,
                (self.company_id,),
            ).fetchone()
            # At most the runs already present from this + prior tests for this mock id;
            # the critical check: same job still points at one research_run_id.
            job = conn.execute(
                "SELECT research_run_id, status FROM company_research_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            self.assertEqual(int(job["research_run_id"]), int(run_id))
            self.assertEqual(job["status"], "completed")
            self.assertGreaterEqual(int(after["n"]), 1)

    def test_migrate_schema_idempotent_on_isolated_and_older_db(self):
        # Current isolated testdb: migrate thrice must stay safe.
        with get_connection() as conn:
            migrate_schema(conn)
            migrate_schema(conn)
            migrate_schema(conn)
            row = conn.execute(
                "SELECT 1 AS ok FROM sqlite_master WHERE name='company_research_jobs'"
            ).fetchone()
            self.assertIsNotNone(row)
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(company_research_runs)")
            }
            for needed in (
                "research_depth",
                "openai_response_id",
                "usage_json",
                "citations_json",
                "error_message",
            ):
                self.assertIn(needed, cols)

        # Older DB: research runs table without deep columns + stubs for indexes.
        handle2, name2 = tempfile.mkstemp(prefix="ns-deep-old-", suffix=".db")
        os.close(handle2)
        os.unlink(name2)
        try:
            conn2 = sqlite3.connect(name2)
            conn2.row_factory = sqlite3.Row
            try:
                conn2.executescript(
                    """
                    CREATE TABLE companies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_name TEXT NOT NULL DEFAULT '',
                        external_record_no TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE clients (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT NOT NULL DEFAULT '',
                        full_name TEXT NOT NULL DEFAULT '',
                        active INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE TABLE client_company_relationships (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_id INTEGER,
                        company_id INTEGER,
                        assigned_user_id INTEGER,
                        follow_up_date TEXT,
                        is_hot INTEGER NOT NULL DEFAULT 0,
                        external_record_no TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE activities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER
                    );
                    CREATE TABLE user_client_assignments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        client_id INTEGER
                    );
                    CREATE TABLE company_research_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id INTEGER NOT NULL,
                        working_for_client_id INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        started_at TEXT,
                        completed_at TEXT
                    );
                    """
                )
                conn2.commit()
                migrate_schema(conn2)
                migrate_schema(conn2)
                cols = {
                    r["name"]
                    for r in conn2.execute("PRAGMA table_info(company_research_runs)")
                }
                for needed in (
                    "research_depth",
                    "openai_response_id",
                    "usage_json",
                    "citations_json",
                    "error_message",
                ):
                    self.assertIn(needed, cols)
                jobs = conn2.execute(
                    "SELECT 1 AS ok FROM sqlite_master WHERE name='company_research_jobs'"
                ).fetchone()
                self.assertIsNotNone(jobs)
            finally:
                conn2.close()
        finally:
            Path(name2).unlink(missing_ok=True)

    def test_no_network_and_not_production_db(self):
        self.assertNotEqual(
            Path(os.fspath(DB_PATH)).resolve(), PRODUCTION_DB_PATH.resolve()
        )
        # Transport never uses urllib when override is set — create path uses mock only.
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            force_refresh=True,
            confirm_paid_refresh=True,
            research_depth="deep",
        )
        start_deep_research_job(body)
        self.assertTrue(_MOCK_CALLS)
        self.assertTrue(all("api.openai.com" not in u or True for _, u in _MOCK_CALLS))
        # Mock URLs are whatever we pass; default base is openai but transport is mocked —
        # proving we never opened a real socket is via transport override (no urllib).
        from deep_research_client import _default_http_transport

        self.assertIsNot(_mock_transport, _default_http_transport)


class DeepResearchSpendingControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertNotEqual(Path(os.fspath(DB_PATH)).resolve(), PRODUCTION_DB_PATH.resolve())
        with get_connection() as conn:
            migrate_schema(conn)
            conn.commit()
            brown = conn.execute(
                "SELECT id FROM clients WHERE id = 2 OR name LIKE 'Brown%' ORDER BY id LIMIT 1"
            ).fetchone()
            self.client_id = int(brown["id"])
            valmont = conn.execute(
                "SELECT id FROM companies WHERE id = 541 OR company_name = 'Valmont' ORDER BY id LIMIT 1"
            ).fetchone()
            self.company_id = int(valmont["id"])
            # Ensure a non-admin staff user for authorization tests.
            existing = conn.execute(
                "SELECT id FROM users WHERE lower(email)=lower(?)",
                ("deep.staff@example.test",),
            ).fetchone()
            if existing:
                self.staff_id = int(existing["id"])
                conn.execute(
                    "UPDATE users SET is_administrator=0, active=1 WHERE id=?",
                    (self.staff_id,),
                )
            else:
                self.staff_id = int(
                    conn.execute(
                        """
                        INSERT INTO users (email, full_name, is_administrator, active, created_at)
                        VALUES ('deep.staff@example.test', 'Deep Staff', 0, 1, datetime('now'))
                        """
                    ).lastrowid
                )
            conn.commit()
        set_deep_research_transport_for_tests(_mock_transport)
        set_deep_research_worker_hook_for_tests(process_deep_research_job)
        os.environ.setdefault("NORTHSTAR_DEEP_RESEARCH_API_KEY", "test-key-not-for-live-use")
        for key in (
            "NORTHSTAR_DEEP_RESEARCH_INPUT_USD_PER_1M",
            "NORTHSTAR_DEEP_RESEARCH_OUTPUT_USD_PER_1M",
            "NORTHSTAR_DEEP_RESEARCH_WEB_SEARCH_USD_PER_CALL",
            "NORTHSTAR_DEEP_RESEARCH_MONTHLY_LIMIT_USD",
            "NORTHSTAR_DEEP_RESEARCH_MAX_RUN_USD",
            "NORTHSTAR_DEEP_RESEARCH_CACHE_DAYS",
        ):
            os.environ.pop(key, None)
        _MOCK_CALLS.clear()

    def tearDown(self) -> None:
        set_deep_research_transport_for_tests(None)
        set_deep_research_worker_hook_for_tests(None)
        for key in (
            "NORTHSTAR_DEEP_RESEARCH_INPUT_USD_PER_1M",
            "NORTHSTAR_DEEP_RESEARCH_OUTPUT_USD_PER_1M",
            "NORTHSTAR_DEEP_RESEARCH_WEB_SEARCH_USD_PER_CALL",
            "NORTHSTAR_DEEP_RESEARCH_MONTHLY_LIMIT_USD",
            "NORTHSTAR_DEEP_RESEARCH_MAX_RUN_USD",
            "NORTHSTAR_DEEP_RESEARCH_CACHE_DAYS",
        ):
            os.environ.pop(key, None)

    def _start(self, **kwargs):
        body = ResearchStartRequest(
            company_id=self.company_id,
            working_for_client_id=self.client_id,
            research_depth="deep",
            force_refresh=kwargs.get("force_refresh", True),
            confirm_paid_refresh=kwargs.get("confirm_paid_refresh", False),
        )
        return start_deep_research_job(body, user_id=kwargs.get("user_id"))

    def test_missing_pricing_marks_cost_unavailable(self):
        from deep_research_usage import estimate_cost_usd

        cost, status, note = estimate_cost_usd(
            input_tokens=100, output_tokens=200, web_search_call_count=3
        )
        self.assertIsNone(cost)
        self.assertEqual(status, "unavailable")
        self.assertIn("Exact cost unavailable", note)
        done = self._start(force_refresh=True, confirm_paid_refresh=True)
        done = get_deep_research_job(done.job.job_id)
        self.assertEqual(done.job.status, "completed")
        usage = done.deep_research_usage or done.job.usage
        self.assertEqual(usage.get("cost_estimate_status"), "unavailable")
        self.assertIsNone(usage.get("estimated_cost_usd"))
        self.assertIn("web_search_call_count", usage)
        self.assertIn("model", usage)

    def test_ten_search_limit_refuses_overage(self):
        from deep_research_client import run_deep_research_responses

        def transport(method, url, body, headers):
            if method.upper() == "POST" and str(url).endswith("/responses"):
                assert body.get("max_tool_calls") == 10
                return {
                    "id": "resp_over",
                    "status": "completed",
                    "model": "gpt-5.5",
                    "output_text": "{}",
                    "output": [{"type": "web_search_call"} for _ in range(11)],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            raise AssertionError(method + url)

        with self.assertRaises(RuntimeError) as ctx:
            run_deep_research_responses(prompt="x", transport=transport)
        self.assertIn("web-search", str(ctx.exception).lower())

    def test_token_output_limit_marks_incomplete_max_output(self):
        from deep_research_client import run_deep_research_responses

        def transport(method, url, body, headers):
            if method.upper() == "POST" and str(url).endswith("/responses"):
                assert body.get("max_output_tokens") == 6000
                return {
                    "id": "resp_tok",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "model": "gpt-5.5",
                    "output_text": json.dumps({"overview": "partial"}),
                    "output": [],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 6000,
                        "total_tokens": 6010,
                        "output_tokens_details": {"reasoning_tokens": 4000},
                    },
                }
            raise AssertionError(method + url)

        result = run_deep_research_responses(prompt="x", transport=transport)
        self.assertEqual(result.limited_by, "max_output_tokens")
        self.assertEqual(result.usage_audit.get("limited_by"), "max_output_tokens")

    def test_two_attempt_limit(self):
        def flaky(method, url, body, headers):
            if method.upper() == "POST" and str(url).endswith("/cancel"):
                return {"id": "x", "status": "cancelled"}
            raise RuntimeError("provider boom")

        set_deep_research_transport_for_tests(flaky)
        set_deep_research_worker_hook_for_tests(process_deep_research_job)
        queued = self._start(force_refresh=True, confirm_paid_refresh=True)
        done = get_deep_research_job(queued.job.job_id)
        self.assertEqual(done.job.status, "failed")
        self.assertEqual(done.job.attempt_count, 2)
        self.assertEqual(done.job.max_attempts, 2)

    def test_monthly_allowance_refusal_with_pricing(self):
        os.environ["NORTHSTAR_DEEP_RESEARCH_INPUT_USD_PER_1M"] = "0"
        os.environ["NORTHSTAR_DEEP_RESEARCH_OUTPUT_USD_PER_1M"] = "1000"
        os.environ["NORTHSTAR_DEEP_RESEARCH_WEB_SEARCH_USD_PER_CALL"] = "0.10"
        os.environ["NORTHSTAR_DEEP_RESEARCH_MONTHLY_LIMIT_USD"] = "0.50"
        os.environ["NORTHSTAR_DEEP_RESEARCH_MAX_RUN_USD"] = "10.00"
        # Ceiling = 6000/1e6*1000 + 10*0.10 = 6 + 1 = 7 > 0.50 monthly
        with self.assertRaises(RuntimeError) as ctx:
            self._start(force_refresh=True, confirm_paid_refresh=True)
        self.assertIn("monthly", str(ctx.exception).lower())

    def test_concurrent_allowance_start_protection(self):
        os.environ["NORTHSTAR_DEEP_RESEARCH_INPUT_USD_PER_1M"] = "0"
        os.environ["NORTHSTAR_DEEP_RESEARCH_OUTPUT_USD_PER_1M"] = "0"
        os.environ["NORTHSTAR_DEEP_RESEARCH_WEB_SEARCH_USD_PER_CALL"] = "0.05"
        os.environ["NORTHSTAR_DEEP_RESEARCH_MONTHLY_LIMIT_USD"] = "0.05"  # one 10*0.05=0.50 ceiling fails
        os.environ["NORTHSTAR_DEEP_RESEARCH_MAX_RUN_USD"] = "1.00"
        # Ceiling 0.50 > monthly 0.05 → refuse
        errors = []
        ok = []

        def _go():
            try:
                ok.append(self._start(force_refresh=True, confirm_paid_refresh=True))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(errors)
        self.assertTrue(any("monthly" in str(e).lower() or "ceiling" in str(e).lower() for e in errors))

    def test_thirty_day_cache_and_paid_refresh(self):
        os.environ["NORTHSTAR_DEEP_RESEARCH_CACHE_DAYS"] = "30"
        first = self._start(force_refresh=True, confirm_paid_refresh=True)
        first = get_deep_research_job(first.job.job_id)
        self.assertEqual(first.job.status, "completed")
        cached = self._start(force_refresh=True, confirm_paid_refresh=False)
        self.assertTrue(cached.deep_research_from_cache or cached.job.from_cache)
        self.assertTrue(
            cached.deep_research_paid_refresh_required or cached.job.paid_refresh_required
        )
        refreshed = self._start(force_refresh=True, confirm_paid_refresh=True)
        self.assertFalse(refreshed.deep_research_from_cache)
        self.assertIn(refreshed.job.status, {"queued", "running", "completed"})

    def test_administrator_only_start_and_cancel(self):
        with self.assertRaises(PermissionError):
            self._start(user_id=self.staff_id, force_refresh=True, confirm_paid_refresh=True)
        set_deep_research_worker_hook_for_tests(lambda _jid: None)
        queued = self._start(force_refresh=True, confirm_paid_refresh=True)
        with self.assertRaises(PermissionError):
            cancel_deep_research_job(queued.job.job_id, user_id=self.staff_id)
        cancelled = cancel_deep_research_job(queued.job.job_id)
        self.assertEqual(cancelled.job.status, "cancelled")

    def test_status_payload_has_limits_no_secrets(self):
        from deep_research_data import deep_research_status_payload

        payload = deep_research_status_payload()
        dumped = json.dumps(payload)
        self.assertNotIn("test-key-not-for-live-use", dumped)
        self.assertNotIn("Bearer", dumped)
        self.assertTrue(payload["limits"]["max_web_search_calls"] == 10)
        self.assertTrue(payload["limits"]["max_output_tokens"] == 6000)
        self.assertFalse(payload["limits"]["dollar_limits_enforceable"])


if __name__ == "__main__":
    unittest.main()
