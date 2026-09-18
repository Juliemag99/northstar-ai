"""Controlled live Data Steward schema+baseline migration utility.

THIS MODULE IS NOT WIRED TO APP STARTUP OR db.migrate_schema.

Fail-closed: refuses unless every authorization gate is explicit.
Does not enable archive, hard delete, company merge, contact merge,
or LeadMaster refresh confirm.

Live production assertion bypass applies ONLY to schema apply and
LEGACY_EXISTING baseline, and only after:

  NORTHSTAR_DS_LIVE_MIGRATE=1
  NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE=1
  --i-authorize-live-data-steward-migration
  --quiesced
  verified backup
  expected SHA / business-count gate

NORTHSTAR_TEST_DB must be unset. Setting it to the live path, or to a
dummy path, is refused: the former is not a bypass, the latter would
silently skip the production assertion while this utility still opens
the live file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LIVE = REPO / "database" / "northstar.db"
AUTH_FLAG = "--i-authorize-live-data-steward-migration"
ENV_AUTH = "NORTHSTAR_DS_LIVE_MIGRATE"
ENV_EXECUTE = "NORTHSTAR_DS_LIVE_MIGRATE_EXECUTE"
EXPECTED_LIVE_SHA = "85746fa08404b25ef6a10f4cbf092df88d2f5c45c3d62ba9a533a08a52db8cec"
EXPECTED_BASELINE_EVENTS = 40167
EXPECTED_COUNTS = {
    "companies": 1791,
    "contacts": 4932,
    "ccr": 1888,
    "aliases": 403,
    "locations": 39,
    "identities": 44,
    "company_merge_history": 1,
    "contact_merge_history": 4,
    "brown_sales": 97,
    "brown_quotes": 26,
}
SCHEMA_COLUMNS = (
    ("companies", "archived_at"),
    ("companies", "archived_by_user_id"),
    ("companies", "archive_reason"),
    ("contacts", "archived_at"),
    ("contacts", "archived_by_user_id"),
    ("contacts", "archive_reason"),
    ("contacts", "last_updated_at"),
    ("company_locations", "archived_at"),
    ("company_locations", "archived_by_user_id"),
    ("company_locations", "archive_reason"),
    ("client_company_relationships", "archived_at"),
    ("client_company_relationships", "archived_by_user_id"),
    ("client_company_relationships", "archive_reason"),
)
SCHEMA_INDEXES = (
    "idx_field_prov_entity",
    "idx_field_prov_changed",
    "idx_companies_archived_at",
    "idx_contacts_archived_at",
    "idx_locations_archived_at",
    "idx_ccr_archived_at",
)


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refuse(message: str, *, code: int = 2) -> None:
    print(json.dumps({"ok": False, "refused": True, "error": message, "at": _utc()}, indent=2))
    raise SystemExit(code)


def _detect_not_quiesced(db_path: Path) -> str:
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    journal = Path(str(db_path) + "-journal")
    if wal.exists() or shm.exists():
        return "SQLite WAL/SHM present; live is not in the expected delete-journal quiesced state."
    if journal.exists() and journal.stat().st_size > 0:
        return "SQLite rollback journal is present; wait for a clean shutdown."
    lock = REPO / "backend" / ".northstar-backend.pid"
    if lock.exists():
        return f"Backend pid file present at {lock}. Stop the app before live migration."
    return ""


def _fingerprint(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for sql in (
        "SELECT id, company_name, address, city, state, zip, website, legacy_phone FROM companies ORDER BY id",
        "SELECT id, company_id, first_name, last_name, email, phone FROM contacts ORDER BY id",
        "SELECT id, client_id, company_id, status, notes, assigned_user_id, next_action, follow_up_date, is_hot FROM client_company_relationships ORDER BY id",
    ):
        for row in conn.execute(sql):
            digest.update(repr(tuple(row)).encode("utf-8"))
            digest.update(b"\n")
        digest.update(b"|")
    return digest.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, Any]:
    from company_merge_approvals import live_executable_approval_count
    from company_merges import resolve_company_id

    doc = conn.execute(
        "SELECT id, processing_status FROM client_documents WHERE id=19"
    ).fetchone()
    return {
        "companies": int(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]),
        "contacts": int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]),
        "ccr": int(
            conn.execute("SELECT COUNT(*) FROM client_company_relationships").fetchone()[0]
        ),
        "aliases": int(conn.execute("SELECT COUNT(*) FROM company_aliases").fetchone()[0]),
        "locations": int(conn.execute("SELECT COUNT(*) FROM company_locations").fetchone()[0]),
        "identities": int(
            conn.execute("SELECT COUNT(*) FROM company_source_identities").fetchone()[0]
        ),
        "company_merge_history": int(
            conn.execute("SELECT COUNT(*) FROM company_merge_history").fetchone()[0]
        ),
        "contact_merge_history": int(
            conn.execute("SELECT COUNT(*) FROM contact_merge_history").fetchone()[0]
        ),
        "brown_sales": int(
            conn.execute(
                "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
            ).fetchone()[0]
        ),
        "brown_quotes": int(
            conn.execute(
                """
                SELECT COUNT(*) FROM revenue_milestones
                WHERE client_id=2 AND milestone_type='Quote'
                """
            ).fetchone()[0]
        ),
        "doc19": [int(doc["id"]), str(doc["processing_status"])] if doc else [None, None],
        "ronson_484": resolve_company_id(conn, 484),
        "executable_approvals": live_executable_approval_count(conn),
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "fk_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
    }


def _count_mismatches(observed: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, expected in EXPECTED_COUNTS.items():
        if int(observed.get(key) or -1) != expected:
            problems.append(f"{key}: expected {expected} got {observed.get(key)}")
    if list(observed.get("doc19") or []) != [19, "uploaded"]:
        problems.append(f"doc19: expected [19, uploaded] got {observed.get('doc19')}")
    if int(observed.get("ronson_484") or 0) != 425:
        problems.append(f"ronson_484: expected 425 got {observed.get('ronson_484')}")
    if observed.get("executable_approvals") != 0:
        problems.append(
            f"executable_approvals: expected 0 got {observed.get('executable_approvals')}"
        )
    if observed.get("integrity") != "ok":
        problems.append(f"integrity: {observed.get('integrity')}")
    if int(observed.get("fk_violations") or 0) != 0:
        problems.append(f"fk_violations: {observed.get('fk_violations')}")
    return problems


def _schema_present(conn: sqlite3.Connection) -> dict[str, Any]:
    def _table(name: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (name,),
            ).fetchone()
            is not None
        )

    def _cols(table: str) -> set[str]:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}

    indexes = {
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    missing_cols = [
        f"{table}.{col}"
        for table, col in SCHEMA_COLUMNS
        if _table(table) and col not in _cols(table)
    ]
    missing_indexes = [name for name in SCHEMA_INDEXES if name not in indexes]
    return {
        "field_provenance_events": _table("field_provenance_events"),
        "missing_columns": missing_cols,
        "missing_indexes": missing_indexes,
        "ready": _table("field_provenance_events") and not missing_cols and not missing_indexes,
    }


def _provenance_breakdown(conn: sqlite3.Connection) -> dict[str, Any]:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_provenance_events' LIMIT 1"
    ).fetchone() is None:
        return {"total": 0, "by_entity_type": {}, "by_field": {}, "by_source_type": {}, "by_action": {}}
    total = int(conn.execute("SELECT COUNT(*) FROM field_provenance_events").fetchone()[0])

    def _group(sql: str) -> dict[str, int]:
        return {str(r[0]): int(r[1]) for r in conn.execute(sql)}

    return {
        "total": total,
        "by_entity_type": _group(
            "SELECT entity_type, COUNT(*) FROM field_provenance_events GROUP BY entity_type ORDER BY 1"
        ),
        "by_field": _group(
            "SELECT field, COUNT(*) FROM field_provenance_events GROUP BY field ORDER BY 1"
        ),
        "by_source_type": _group(
            "SELECT source_type, COUNT(*) FROM field_provenance_events GROUP BY source_type ORDER BY 1"
        ),
        "by_action": _group(
            "SELECT action, COUNT(*) FROM field_provenance_events GROUP BY action ORDER BY 1"
        ),
        "actor_not_null": int(
            conn.execute(
                "SELECT COUNT(*) FROM field_provenance_events WHERE changed_by_user_id IS NOT NULL"
            ).fetchone()[0]
        ),
        "old_value_not_blank": int(
            conn.execute(
                "SELECT COUNT(*) FROM field_provenance_events WHERE TRIM(COALESCE(old_value,'')) != ''"
            ).fetchone()[0]
        ),
    }


def _rollback_cmd(backup_path: Path, db_path: Path) -> str:
    return (
        "Stop NorthStar, then restore the PRE-DS5 verified backup with the SQLite backup API "
        f'from "{backup_path}" onto "{db_path}". Do not repair-forward a failed live migration.'
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Controlled live Data Steward schema+baseline migration. Default is refuse."
    )
    parser.add_argument("--db", required=True, help="Explicit live database path.")
    parser.add_argument(
        AUTH_FLAG,
        dest="authorize",
        action="store_true",
        help="Required authorization flag. Without it the utility refuses.",
    )
    parser.add_argument(
        "--quiesced",
        action="store_true",
        help="Operator attests the backend/app is stopped.",
    )
    parser.add_argument("--confirm-db-sha", default="", help="Expected live SHA-256.")
    parser.add_argument(
        "--apply-legacy-baseline",
        action="store_true",
        help="Apply LEGACY_EXISTING baseline after schema verification.",
    )
    parser.add_argument("--result-json", default="", help="Optional result JSON path.")
    parser.add_argument("--backup-dir", default="", help="Verified backup directory.")
    parser.add_argument(
        "--expected-baseline-events",
        type=int,
        default=EXPECTED_BASELINE_EVENTS,
        help="Refuse and stop before restart if written events differ.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).resolve()
    if os.environ.get("NORTHSTAR_TEST_DB", "").strip():
        _refuse(
            "Refused: NORTHSTAR_TEST_DB must be unset. Isolation env would skip the "
            "production assertion while this utility still opens the live file."
        )
    if os.environ.get(ENV_AUTH, "").strip() != "1":
        _refuse(f"Refused: set {ENV_AUTH}=1 in addition to {AUTH_FLAG}.")
    if not args.authorize:
        _refuse(f"Refused: missing {AUTH_FLAG}.")
    if not args.quiesced:
        _refuse("Refused: --quiesced is required. Stop the backend first.")
    if db_path != DEFAULT_LIVE.resolve():
        _refuse(f"Refused: --db must be the canonical live path {DEFAULT_LIVE}.")
    if not db_path.is_file():
        _refuse(f"Refused: database not found: {db_path}")
    busy = _detect_not_quiesced(db_path)
    if busy:
        _refuse(f"Refused: not quiesced ({busy})")

    from data_steward import (
        SOURCE_REF_ACTIVATION,
        apply_data_steward_schema_isolated,
        authorized_live_schema_activation,
        conservative_legacy_backfill,
    )
    from db import PRODUCTION_DB_PATH
    from pilot_backup import sha256_file, sqlite_backup, verify_backup

    if db_path != PRODUCTION_DB_PATH.resolve():
        _refuse("Refused: resolved live path does not match PRODUCTION_DB_PATH.")
    live_sha = sha256_file(db_path)
    expected_sha = (args.confirm_db_sha or EXPECTED_LIVE_SHA).strip().lower()
    if expected_sha != live_sha.lower():
        _refuse(
            "Refused: live SHA does not match the authorized pre-migration fingerprint "
            f"{expected_sha}."
        )

    backup_dir = Path(args.backup_dir) if args.backup_dir else (
        Path(os.environ.get("LOCALAPPDATA", str(REPO / "database"))) / "NorthStar" / "backups"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"northstar-pre-data-steward-live-{stamp}.db"
    sqlite_backup(db_path, backup_path)
    verified = verify_backup(backup_path)
    if verified.get("integrity") != "ok" or int(verified.get("fk_violations") or 0) != 0:
        _refuse("Refused: verified backup failed integrity/FK. No live schema applied.")
    backup_count_problems = []
    for key, expected in EXPECTED_COUNTS.items():
        if int(verified.get(key) or -1) != expected:
            backup_count_problems.append(f"{key}: expected {expected} got {verified.get(key)}")
    if backup_count_problems:
        _refuse(
            "Refused: verified backup counts do not match the authorized baseline. "
            + "; ".join(backup_count_problems)
        )

    if os.environ.get(ENV_EXECUTE, "").strip() != "1":
        result = {
            "ok": False,
            "refused": True,
            "error": (
                "Prepared only. Live schema apply is disabled until "
                f"{ENV_EXECUTE}=1 is set by an authorized operator. Backup was created and verified."
            ),
            "backup_path": str(backup_path),
            "backup_sha256": verified.get("sha256"),
            "backup_bytes": verified.get("bytes"),
            "live_sha256": live_sha,
            "rollback": _rollback_cmd(backup_path, db_path),
            "at": _utc(),
        }
        if args.result_json:
            Path(args.result_json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 2

    if "NORTHSTAR_TEST_DB" in os.environ:
        del os.environ["NORTHSTAR_TEST_DB"]

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    schema = None
    baseline = None
    idempotency = None
    schema_verify: dict[str, Any] = {}
    rollback_required = False
    stop_reason = ""
    activation_at = ""
    post_counts: dict[str, Any] = {}
    breakdown: dict[str, Any] = {}
    schema_state: dict[str, Any] = {}
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        pre_counts = _counts(conn)
        pre_fp = _fingerprint(conn)
        pre_problems = _count_mismatches(pre_counts)
        if pre_problems:
            rollback_required = False
            stop_reason = "Pre-apply live counts/integrity failed: " + "; ".join(pre_problems)
            result = {
                "ok": False,
                "refused": True,
                "error": stop_reason,
                "backup_path": str(backup_path),
                "backup_sha256": verified.get("sha256"),
                "live_sha256": live_sha,
                "pre_counts": pre_counts,
                "rollback": _rollback_cmd(backup_path, db_path),
                "at": _utc(),
            }
            if args.result_json:
                Path(args.result_json).write_text(
                    json.dumps(result, indent=2, default=str), encoding="utf-8"
                )
            print(json.dumps(result, indent=2, default=str))
            return 2

        with authorized_live_schema_activation():
            schema = apply_data_steward_schema_isolated(conn)
            post_schema_counts = _counts(conn)
            post_schema_fp = _fingerprint(conn)
            schema_state = _schema_present(conn)
            schema_verify = {
                "counts": post_schema_counts,
                "fingerprint_unchanged": post_schema_fp == pre_fp,
                "schema": schema_state,
                "count_problems": _count_mismatches(post_schema_counts),
            }
            if (
                schema_verify["count_problems"]
                or not schema_verify["fingerprint_unchanged"]
                or not schema_state["ready"]
            ):
                rollback_required = True
                stop_reason = (
                    "Schema verification failed before baseline. "
                    f"count_problems={schema_verify['count_problems']} "
                    f"fingerprint_unchanged={schema_verify['fingerprint_unchanged']} "
                    f"schema={schema_state}"
                )
            elif args.apply_legacy_baseline:
                activation_at = _utc()
                preview = conservative_legacy_backfill(
                    conn,
                    dry_run=True,
                    changed_at=activation_at,
                    source_ref=SOURCE_REF_ACTIVATION,
                )
                baseline = conservative_legacy_backfill(
                    conn,
                    dry_run=False,
                    changed_at=activation_at,
                    source_ref=SOURCE_REF_ACTIVATION,
                )
                baseline["preview_planned"] = preview.get("planned_events")
                written = int(baseline.get("written_events") or 0)
                if written != int(args.expected_baseline_events):
                    conn.rollback()
                    rollback_required = True
                    stop_reason = (
                        "LEGACY_EXISTING written_events "
                        f"{written} != expected {args.expected_baseline_events}"
                    )
                else:
                    conn.commit()
                    idempotency = conservative_legacy_backfill(
                        conn,
                        dry_run=False,
                        changed_at=activation_at,
                        source_ref=SOURCE_REF_ACTIVATION,
                    )
                    if int(idempotency.get("written_events") or 0) != 0:
                        conn.rollback()
                        rollback_required = True
                        stop_reason = (
                            "Baseline idempotency failed: second apply wrote "
                            f"{idempotency.get('written_events')} events"
                        )
                    else:
                        conn.commit()
                    post_fp = _fingerprint(conn)
                    if post_fp != pre_fp:
                        rollback_required = True
                        stop_reason = "Business-value fingerprint changed after baseline."
        post_counts = _counts(conn)
        breakdown = _provenance_breakdown(conn)
        schema_state = _schema_present(conn)
    finally:
        conn.close()

    result = {
        "ok": (not rollback_required) and (not stop_reason) and bool(schema),
        "refused": False,
        "rollback_required": rollback_required,
        "error": stop_reason,
        "northstar_test_db_set": bool(os.environ.get("NORTHSTAR_TEST_DB", "").strip()),
        "bypass_scope": "schema_apply_and_legacy_baseline_only",
        "schema": schema,
        "schema_verify": schema_verify,
        "baseline": baseline,
        "idempotency": idempotency,
        "activation_at": activation_at,
        "source_ref": SOURCE_REF_ACTIVATION,
        "post_counts": post_counts,
        "provenance": breakdown,
        "schema_present": schema_state,
        "backup_path": str(backup_path),
        "backup_sha256": verified.get("sha256"),
        "backup_bytes": verified.get("bytes"),
        "pre_live_sha256": live_sha,
        "rollback": _rollback_cmd(backup_path, db_path),
        "at": _utc(),
    }
    result["ok"] = (
        not rollback_required
        and not stop_reason
        and bool(schema)
        and post_counts.get("integrity") == "ok"
        and int(post_counts.get("fk_violations") or 0) == 0
        and schema_state.get("ready") is True
        and (not args.apply_legacy_baseline or breakdown.get("total") == int(args.expected_baseline_events))
    )
    if args.result_json:
        Path(args.result_json).write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
