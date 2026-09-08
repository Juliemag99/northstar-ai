"""Explicit database configuration for SQLite (live) and future PostgreSQL.

Credentials stay server-side. Never log or return passwords / full DSNs.
Live production remains SQLite until a later cutover phase.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DATABASE_DIR = REPO_ROOT / "database"
DEFAULT_SQLITE_PATH = DATABASE_DIR / "northstar.db"


class DatabaseEngine(str, Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class DatabaseConfigError(ValueError):
    """Raised for unsupported or incomplete database configuration.

    Messages must never include raw passwords or full DSNs — callers should
    pass ``DatabaseConfig.safe_summary()`` / ``redact_secret`` only.
    """

    def __init__(self, message: str):
        super().__init__(redact_secret(message) if "://" in message else message)


_PASSWORD_IN_URL = re.compile(
    r"(?P<pre>://[^:/?#]+:)(?P<pw>[^@]*)(?P<post>@)",
    re.IGNORECASE,
)
_PASSWORD_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "database_url",
        "northstar_database_url",
        "dsn",
    }
)


def redact_secret(value: object | None) -> str:
    """Return a safe display string; never include raw credentials."""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    redacted = _PASSWORD_IN_URL.sub(r"\g<pre>***\g<post>", text)
    if redacted != text:
        return redacted
    # Non-URL secrets: keep shape only.
    if len(text) <= 4:
        return "***"
    return text[:2] + "***" + text[-1:]


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Copy a mapping with secret-looking keys/values redacted."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        key_l = str(key).lower()
        if key_l in _PASSWORD_KEYS or "password" in key_l or "secret" in key_l:
            out[key] = redact_secret(value)
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, str) and (
            "://" in value and "@" in value
        ):
            out[key] = redact_secret(value)
        else:
            out[key] = value
    return out


def _normalize_engine(raw: str) -> DatabaseEngine:
    text = (raw or "").strip().lower()
    if text in {"", "sqlite", "sqlite3"}:
        return DatabaseEngine.SQLITE
    if text in {"postgres", "postgresql", "pg"}:
        return DatabaseEngine.POSTGRESQL
    raise DatabaseConfigError(
        f"Unsupported NORTHSTAR_DB_ENGINE={raw!r}. "
        "Use 'sqlite' (default) or 'postgresql'."
    )


def _parse_database_url(url: str) -> tuple[DatabaseEngine, str]:
    raw = url.strip()
    if not raw:
        raise DatabaseConfigError("DATABASE_URL is empty.")
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme in {"postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"}:
        return DatabaseEngine.POSTGRESQL, raw
    if scheme in {"sqlite", "sqlite3"}:
        return DatabaseEngine.SQLITE, raw
    raise DatabaseConfigError(
        f"Unsupported DATABASE_URL scheme {scheme!r}. "
        "Expected postgresql://… or sqlite:///…"
    )


@dataclass(frozen=True)
class DatabaseConfig:
    """Resolved database settings for the active process."""

    engine: DatabaseEngine
    sqlite_path: Path | None
    database_url: str | None
    source: str

    @property
    def is_sqlite(self) -> bool:
        return self.engine is DatabaseEngine.SQLITE

    @property
    def is_postgresql(self) -> bool:
        return self.engine is DatabaseEngine.POSTGRESQL

    def safe_summary(self) -> dict[str, Any]:
        """JSON-safe summary with credentials redacted."""
        return {
            "engine": self.engine.value,
            "sqlite_path": str(self.sqlite_path) if self.sqlite_path else None,
            "database_url": redact_secret(self.database_url) if self.database_url else None,
            "source": self.source,
        }

    def require_sqlite_path(self) -> Path:
        if self.engine is not DatabaseEngine.SQLITE or self.sqlite_path is None:
            raise DatabaseConfigError(
                "SQLite path required for the active engine, but configuration "
                f"resolved to {self.safe_summary()!r}."
            )
        return self.sqlite_path

    def require_database_url(self) -> str:
        if self.engine is not DatabaseEngine.POSTGRESQL or not self.database_url:
            raise DatabaseConfigError(
                "DATABASE_URL required for PostgreSQL, but configuration "
                f"resolved to {self.safe_summary()!r}."
            )
        return self.database_url


def load_database_config(
    *,
    environ: dict[str, str] | None = None,
    default_sqlite_path: Path | None = None,
) -> DatabaseConfig:
    """Load explicit DB config. Default remains local SQLite.

    Precedence:
    1. NORTHSTAR_DB_ENGINE=postgresql requires NORTHSTAR_DATABASE_URL or DATABASE_URL
    2. DATABASE_URL / NORTHSTAR_DATABASE_URL alone selects engine from scheme
    3. NORTHSTAR_SQLITE_PATH / default path for SQLite

    PostgreSQL is accepted for tooling/tests only in Phase 0C; production live
    traffic must keep SQLite until a later cutover.
    """
    env = environ if environ is not None else os.environ
    default_path = Path(default_sqlite_path or DEFAULT_SQLITE_PATH)

    engine_raw = (env.get("NORTHSTAR_DB_ENGINE") or "").strip()
    url = (
        (env.get("NORTHSTAR_DATABASE_URL") or env.get("DATABASE_URL") or "").strip()
    )
    sqlite_override = (env.get("NORTHSTAR_SQLITE_PATH") or "").strip()

    if engine_raw:
        engine = _normalize_engine(engine_raw)
        if engine is DatabaseEngine.POSTGRESQL:
            if not url:
                raise DatabaseConfigError(
                    "NORTHSTAR_DB_ENGINE=postgresql requires "
                    "NORTHSTAR_DATABASE_URL or DATABASE_URL."
                )
            url_engine, normalized = _parse_database_url(url)
            if url_engine is not DatabaseEngine.POSTGRESQL:
                raise DatabaseConfigError(
                    "NORTHSTAR_DB_ENGINE=postgresql but DATABASE_URL is not PostgreSQL."
                )
            return DatabaseConfig(
                engine=DatabaseEngine.POSTGRESQL,
                sqlite_path=None,
                database_url=normalized,
                source="NORTHSTAR_DB_ENGINE+DATABASE_URL",
            )
        # Explicit sqlite
        path = Path(sqlite_override) if sqlite_override else default_path
        if url and _parse_database_url(url)[0] is DatabaseEngine.POSTGRESQL:
            raise DatabaseConfigError(
                "NORTHSTAR_DB_ENGINE=sqlite cannot be combined with a "
                "PostgreSQL DATABASE_URL."
            )
        return DatabaseConfig(
            engine=DatabaseEngine.SQLITE,
            sqlite_path=path,
            database_url=None,
            source="NORTHSTAR_DB_ENGINE=sqlite",
        )

    if url:
        url_engine, normalized = _parse_database_url(url)
        if url_engine is DatabaseEngine.POSTGRESQL:
            return DatabaseConfig(
                engine=DatabaseEngine.POSTGRESQL,
                sqlite_path=None,
                database_url=normalized,
                source="DATABASE_URL",
            )
        # sqlite URL: sqlite:///absolute or sqlite:////path
        parts = urlsplit(normalized)
        path_str = parts.path or ""
        if path_str.startswith("/") and len(path_str) > 1 and os.name == "nt":
            # sqlite:///C:/path → /C:/path
            if re.match(r"^/[A-Za-z]:/", path_str):
                path_str = path_str[1:]
        return DatabaseConfig(
            engine=DatabaseEngine.SQLITE,
            sqlite_path=Path(path_str or default_path),
            database_url=normalized,
            source="DATABASE_URL(sqlite)",
        )

    path = Path(sqlite_override) if sqlite_override else default_path
    return DatabaseConfig(
        engine=DatabaseEngine.SQLITE,
        sqlite_path=path,
        database_url=None,
        source="default_sqlite",
    )


def public_url_for_logs(url: str | None) -> str:
    """Redact userinfo from a DSN for logs."""
    if not url:
        return ""
    return redact_secret(url)
