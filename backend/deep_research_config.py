"""Deep Research configuration — secrets never logged or returned."""

from __future__ import annotations

import os

# Official OpenAI Responses + web_search defaults (docs, 2026):
# model gpt-5.5, tool type web_search, background for long runs.
DEFAULT_DEEP_RESEARCH_MODEL = "gpt-5.5"
DEFAULT_DEEP_RESEARCH_BASE_URL = "https://api.openai.com/v1"

# Server-side hard ceilings (always enforced)
MAX_OUTPUT_TOKENS = 6000
MAX_WEB_SEARCH_CALLS = 10
MAX_ATTEMPTS = 2
SEARCH_CONTEXT_SIZE = "medium"  # low | medium | high — not unlimited
REASONING_EFFORT = "high"
POLL_INTERVAL_SEC = 2.0
POLL_TIMEOUT_SEC = 480.0
# Jobs with no progress heartbeat after this many seconds are recovered
# (re-queue if a provider response id exists, otherwise fail safely).
STALE_JOB_SEC = 120.0

DEFAULT_MONTHLY_LIMIT_USD = 20.00
DEFAULT_MAX_RUN_USD = 1.00
DEFAULT_CACHE_DAYS = 30


def deep_research_api_key() -> str:
    return (os.getenv("NORTHSTAR_DEEP_RESEARCH_API_KEY") or "").strip()


def deep_research_is_configured() -> bool:
    return bool(deep_research_api_key())


def deep_research_model() -> str:
    return (
        os.getenv("NORTHSTAR_DEEP_RESEARCH_MODEL") or DEFAULT_DEEP_RESEARCH_MODEL
    ).strip() or DEFAULT_DEEP_RESEARCH_MODEL


def deep_research_base_url() -> str:
    return (
        os.getenv("NORTHSTAR_DEEP_RESEARCH_BASE_URL") or DEFAULT_DEEP_RESEARCH_BASE_URL
    ).strip().rstrip("/") or DEFAULT_DEEP_RESEARCH_BASE_URL


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def deep_research_monthly_limit_usd() -> float:
    return max(0.0, _env_float("NORTHSTAR_DEEP_RESEARCH_MONTHLY_LIMIT_USD", DEFAULT_MONTHLY_LIMIT_USD))


def deep_research_max_run_usd() -> float:
    return max(0.0, _env_float("NORTHSTAR_DEEP_RESEARCH_MAX_RUN_USD", DEFAULT_MAX_RUN_USD))


def deep_research_cache_days() -> int:
    return max(0, _env_int("NORTHSTAR_DEEP_RESEARCH_CACHE_DAYS", DEFAULT_CACHE_DAYS))


def deep_research_input_usd_per_1m() -> float | None:
    """Optional authoritative input token rate ($ / 1M tokens). None = unavailable."""
    raw = (os.getenv("NORTHSTAR_DEEP_RESEARCH_INPUT_USD_PER_1M") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def deep_research_output_usd_per_1m() -> float | None:
    """Optional authoritative output/reasoning token rate ($ / 1M tokens). None = unavailable."""
    raw = (os.getenv("NORTHSTAR_DEEP_RESEARCH_OUTPUT_USD_PER_1M") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def deep_research_web_search_usd_per_call() -> float | None:
    """Optional authoritative web-search call rate ($ / call). None = unavailable."""
    raw = (os.getenv("NORTHSTAR_DEEP_RESEARCH_WEB_SEARCH_USD_PER_CALL") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def pricing_rates_available() -> bool:
    """True only when all rate components needed for a ceiling estimate are set."""
    return (
        deep_research_input_usd_per_1m() is not None
        and deep_research_output_usd_per_1m() is not None
        and deep_research_web_search_usd_per_call() is not None
    )


def sanitize_error_message(exc: BaseException | str) -> str:
    """Strip likely secrets from error text before persistence/UI."""
    text = str(exc or "").strip()
    key = deep_research_api_key()
    if key and key in text:
        text = text.replace(key, "[redacted]")
    # Common bearer patterns
    if "bearer " in text.lower():
        parts = text.split()
        out = []
        skip_next = False
        for p in parts:
            if skip_next:
                out.append("[redacted]")
                skip_next = False
                continue
            if p.lower() == "bearer":
                out.append(p)
                skip_next = True
                continue
            out.append(p)
        text = " ".join(out)
    return text[:800]
