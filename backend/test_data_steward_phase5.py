"""Data Steward Phase DS5 — fail-closed live migration gates.

Blank NORTHSTAR_TEST_DB for subprocess migrate checks.
Never copies or writes live northstar.db in these tests.
Does not enable live archive/delete/merge or LeadMaster confirm.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_HANDLE, _TEST_DB = tempfile.mkstemp(prefix="ns-ds5-", suffix=".db")
os.close(_HANDLE)
os.environ["NORTHSTAR_TEST_DB"] = _TEST_DB

from data_steward import (  # noqa: E402
    ENV_LIVE_MIGRATE,
    ENV_LIVE_MIGRATE_EXECUTE,
    StewardLiveWriteError,
    apply_data_steward_schema_isolated,
    assert_not_production_db,
    authorized_live_schema_activation,
    begin_authorized_live_schema_activation,
    conservative_legacy_backfill,
    create_company,
    ensure_data_steward_schema,
)

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "backend" / ".venv" / "Scripts" / "python.exe"
MIGRATE = REPO / "backend" / "data_steward_live_migrate.py"
LIVE = REPO / "database" / "northstar.db"


class DataStewardPhase5Tests(unittest.TestCase):
    def test_01_begin_authorized_requires_env_flags(self):
        os.environ.pop(ENV_LIVE_MIGRATE, None)
        os.environ.pop(ENV_LIVE_MIGRATE_EXECUTE, None)
        with self.assertRaises(StewardLiveWriteError):
            begin_authorized_live_schema_activation()
        os.environ[ENV_LIVE_MIGRATE] = "1"
        with self.assertRaises(StewardLiveWriteError):
            begin_authorized_live_schema_activation()
        os.environ.pop(ENV_LIVE_MIGRATE, None)

    def test_02_bypass_does_not_open_create_company(self):
        source = inspect.getsource(create_company)
        self.assertIn("assert_not_production_db", source)
        self.assertNotIn("_assert_schema_or_baseline_allowed", source)

    def test_03_bypass_is_only_on_schema_and_baseline(self):
        self.assertIn("_assert_schema_or_baseline_allowed", inspect.getsource(ensure_data_steward_schema))
        self.assertIn("_assert_schema_or_baseline_allowed", inspect.getsource(apply_data_steward_schema_isolated))
        self.assertIn("_assert_schema_or_baseline_allowed", inspect.getsource(conservative_legacy_backfill))
        self.assertNotIn("_AUTHORIZED_LIVE_SCHEMA_ACTIVATION", inspect.getsource(assert_not_production_db))

    def test_04_migrate_schema_not_wired(self):
        text = (REPO / "backend" / "db.py").read_text(encoding="utf-8")
        self.assertNotIn("data_steward_live_migrate", text)
        self.assertNotIn("apply_data_steward_schema", text)
        self.assertNotIn("ensure_data_steward_schema", text)

    def test_05_main_not_wired(self):
        text = (REPO / "backend" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("data_steward_live_migrate", text)
        self.assertNotIn("apply_data_steward_schema_isolated", text)

    def test_06_utility_refuses_without_env(self):
        env = os.environ.copy()
        env.pop("NORTHSTAR_TEST_DB", None)
        env.pop("NORTHSTAR_DS_LIVE_MIGRATE", None)
        env.pop("NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE", None)
        proc = subprocess.run(
            [str(PY), str(MIGRATE), "--db", str(LIVE)],
            cwd=str(REPO / "backend"),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("NORTHSTAR_DS_LIVE_MIGRATE=1", proc.stdout)

    def test_07_utility_refuses_without_authorize_flag(self):
        env = os.environ.copy()
        env.pop("NORTHSTAR_TEST_DB", None)
        env["NORTHSTAR_DS_LIVE_MIGRATE"] = "1"
        env.pop("NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE", None)
        proc = subprocess.run(
            [str(PY), str(MIGRATE), "--db", str(LIVE), "--quiesced"],
            cwd=str(REPO / "backend"),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--i-authorize-live-data-steward-migration", proc.stdout)

    def test_08_utility_refuses_without_quiesced(self):
        env = os.environ.copy()
        env.pop("NORTHSTAR_TEST_DB", None)
        env["NORTHSTAR_DS_LIVE_MIGRATE"] = "1"
        env.pop("NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE", None)
        proc = subprocess.run(
            [
                str(PY),
                str(MIGRATE),
                "--db",
                str(LIVE),
                "--i-authorize-live-data-steward-migration",
            ],
            cwd=str(REPO / "backend"),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--quiesced", proc.stdout)

    def test_09_utility_refuses_when_test_db_set(self):
        env = os.environ.copy()
        env["NORTHSTAR_TEST_DB"] = str(_TEST_DB)
        env["NORTHSTAR_DS_LIVE_MIGRATE"] = "1"
        env["NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE"] = "1"
        proc = subprocess.run(
            [
                str(PY),
                str(MIGRATE),
                "--db",
                str(LIVE),
                "--i-authorize-live-data-steward-migration",
                "--quiesced",
            ],
            cwd=str(REPO / "backend"),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("NORTHSTAR_TEST_DB must be unset", proc.stdout)

    def test_10_utility_refuses_non_live_path(self):
        env = os.environ.copy()
        env.pop("NORTHSTAR_TEST_DB", None)
        env["NORTHSTAR_DS_LIVE_MIGRATE"] = "1"
        env.pop("NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE", None)
        proc = subprocess.run(
            [
                str(PY),
                str(MIGRATE),
                "--db",
                str(_TEST_DB),
                "--i-authorize-live-data-steward-migration",
                "--quiesced",
            ],
            cwd=str(REPO / "backend"),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("canonical live path", proc.stdout)

    def test_11_live_destructive_still_false(self):
        from data_steward import live_destructive_enabled

        self.assertFalse(live_destructive_enabled())

    def test_12_bypass_context_requires_execute_flag(self):
        os.environ[ENV_LIVE_MIGRATE] = "1"
        os.environ.pop(ENV_LIVE_MIGRATE_EXECUTE, None)
        with self.assertRaises(StewardLiveWriteError):
            with authorized_live_schema_activation():
                pass
        os.environ.pop(ENV_LIVE_MIGRATE, None)


if __name__ == "__main__":
    unittest.main()
