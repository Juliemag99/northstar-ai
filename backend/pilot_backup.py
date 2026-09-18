"""SQLite-safe backup helper for the NorthStar pilot.

Uses the SQLite backup API. Never enables WAL on the live database.
Default destination is %LOCALAPPDATA%\\NorthStar\\backups (outside OneDrive).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from company_merge_approvals import live_executable_approval_count
from company_merges import resolve_company_id

REPO = Path(__file__).resolve().parent.parent
LIVE_DB = REPO / "database" / "northstar.db"
PHASE_BACKUP_MARKERS = (
    "phase",
    "master-data-std",
    "brown-",
    "cdi-",
    "auth-status",
    "ronson",
)


def default_backup_dir() -> Path:
    override = os.environ.get("NORTHSTAR_BACKUP_DIR", "").strip()
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "NorthStar" / "backups"
    return REPO / "database" / "backups"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def verify_backup(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        payload = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "integrity": integrity,
            "fk_violations": fk,
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "ccr": conn.execute(
                "SELECT COUNT(*) FROM client_company_relationships"
            ).fetchone()[0],
            "aliases": conn.execute("SELECT COUNT(*) FROM company_aliases").fetchone()[0],
            "locations": conn.execute(
                "SELECT COUNT(*) FROM company_locations"
            ).fetchone()[0],
            "identities": conn.execute(
                "SELECT COUNT(*) FROM company_source_identities"
            ).fetchone()[0],
            "company_merge_history": conn.execute(
                "SELECT COUNT(*) FROM company_merge_history"
            ).fetchone()[0],
            "contact_merge_history": conn.execute(
                "SELECT COUNT(*) FROM contact_merge_history"
            ).fetchone()[0],
            "brown_sales": conn.execute(
                "SELECT COUNT(*) FROM client_sales_events WHERE client_id=2"
            ).fetchone()[0],
            "brown_quotes": conn.execute(
                """
                SELECT COUNT(*) FROM revenue_milestones
                WHERE client_id=2 AND milestone_type='Quote'
                """
            ).fetchone()[0],
            "doc19": list(
                conn.execute(
                    "SELECT id, processing_status FROM client_documents WHERE id=19"
                ).fetchone()
                or (None, None)
            ),
            "ronson_484": resolve_company_id(conn, 484),
            "executable_approvals": live_executable_approval_count(conn),
        }
        return payload
    finally:
        conn.close()


def is_protected_backup_name(name: str) -> bool:
    lower = name.lower()
    return any(marker in lower for marker in PHASE_BACKUP_MARKERS)


def timestamp_name(prefix: str = "northstar-pilot") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}.db"


def create_verified_backup(
    source: Path | None = None,
    dest_dir: Path | None = None,
    prefix: str = "northstar-pilot",
) -> dict:
    src = source or LIVE_DB
    folder = dest_dir or default_backup_dir()
    dest = folder / timestamp_name(prefix)
    sqlite_backup(src, dest)
    verified = verify_backup(dest)
    verified["source"] = str(src)
    return verified
