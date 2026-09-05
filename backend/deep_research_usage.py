"""Deep Research usage accounting and cost estimation.

Never invents model prices. Estimates are complete only when all authoritative
NORTHSTAR_DEEP_RESEARCH_*_USD_* rates are configured. Dollar ceilings are
enforceable only when estimates are complete; search/token ceilings always apply.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deep_research_config import (
    MAX_OUTPUT_TOKENS,
    MAX_WEB_SEARCH_CALLS,
    deep_research_cache_days,
    deep_research_input_usd_per_1m,
    deep_research_max_run_usd,
    deep_research_model,
    deep_research_monthly_limit_usd,
    deep_research_output_usd_per_1m,
    deep_research_web_search_usd_per_call,
    pricing_rates_available,
)

COST_COMPLETE = "complete"
COST_UNAVAILABLE = "unavailable"


def utc_month_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-01 00:00:00")


def count_web_search_calls(payload: dict[str, Any] | None) -> int:
    n = 0
    for item in (payload or {}).get("output") or []:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            n += 1
    return n


def extract_token_usage(payload: dict[str, Any] | None) -> dict[str, int]:
    usage = (payload or {}).get("usage") if isinstance((payload or {}).get("usage"), dict) else {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    reasoning_tokens = 0
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    if isinstance(details, dict):
        reasoning_tokens = int(details.get("reasoning_tokens") or 0)
    total = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total,
    }


def estimate_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    web_search_call_count: int,
) -> tuple[float | None, str, str]:
    """Return (cost, status, note). Does not invent rates."""
    inp = deep_research_input_usd_per_1m()
    out = deep_research_output_usd_per_1m()
    search = deep_research_web_search_usd_per_call()
    if inp is None or out is None or search is None:
        missing = []
        if inp is None:
            missing.append("NORTHSTAR_DEEP_RESEARCH_INPUT_USD_PER_1M")
        if out is None:
            missing.append("NORTHSTAR_DEEP_RESEARCH_OUTPUT_USD_PER_1M")
        if search is None:
            missing.append("NORTHSTAR_DEEP_RESEARCH_WEB_SEARCH_USD_PER_CALL")
        return (
            None,
            COST_UNAVAILABLE,
            "Exact cost unavailable — configure " + ", ".join(missing) + ".",
        )
    cost = (
        (max(0, input_tokens) / 1_000_000.0) * float(inp)
        + (max(0, output_tokens) / 1_000_000.0) * float(out)
        + max(0, web_search_call_count) * float(search)
    )
    return (round(cost, 6), COST_COMPLETE, "")


def estimate_ceiling_cost_usd() -> tuple[float | None, str, str]:
    """Worst-case cost for one run at configured search/token ceilings."""
    return estimate_cost_usd(
        input_tokens=0,  # input unknown; only bound output+search for ceiling
        output_tokens=MAX_OUTPUT_TOKENS,
        web_search_call_count=MAX_WEB_SEARCH_CALLS,
    )


def dollar_limits_enforceable() -> bool:
    return pricing_rates_available()


def build_usage_record(
    *,
    payload: dict[str, Any] | None,
    model: str = "",
    completed_at: str = "",
    limited_by: str = "",
) -> dict[str, Any]:
    tokens = extract_token_usage(payload)
    searches = count_web_search_calls(payload)
    cost, status, note = estimate_cost_usd(
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        web_search_call_count=searches,
    )
    return {
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "reasoning_tokens": tokens["reasoning_tokens"],
        "total_tokens": tokens["total_tokens"],
        "web_search_call_count": searches,
        "model": (model or deep_research_model()).strip(),
        "estimated_cost_usd": cost,
        "cost_estimate_status": status,
        "cost_estimate_note": note,
        "max_output_tokens_limit": MAX_OUTPUT_TOKENS,
        "max_web_search_calls": MAX_WEB_SEARCH_CALLS,
        "limited_by": limited_by,
        "completed_at": completed_at,
        "dollar_limit_guaranteed": False,  # never claim $1 unless enforced via rates
    }


def public_limits_payload() -> dict[str, Any]:
    ceiling_cost, ceiling_status, ceiling_note = estimate_ceiling_cost_usd()
    enforceable = dollar_limits_enforceable()
    return {
        "max_web_search_calls": MAX_WEB_SEARCH_CALLS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_attempts": 2,
        "max_run_usd": deep_research_max_run_usd(),
        "monthly_limit_usd": deep_research_monthly_limit_usd(),
        "cache_days": deep_research_cache_days(),
        "dollar_limits_enforceable": enforceable,
        "estimated_max_run_usd": ceiling_cost if ceiling_status == COST_COMPLETE else None,
        "estimated_max_run_status": ceiling_status,
        "estimated_max_run_note": (
            ceiling_note
            if ceiling_status != COST_COMPLETE
            else (
                "Ceiling uses max output tokens + max web searches; "
                "input tokens are not included in the pre-run ceiling."
            )
        ),
        "pilot_admin_only": True,
    }


def monthly_usage_from_rows(rows: list[Any]) -> dict[str, Any]:
    """Aggregate completed-job usage rows (must expose usage_json / status)."""
    import json

    spent = 0.0
    complete = 0
    unavailable = 0
    for row in rows:
        try:
            usage = json.loads(row["usage_json"] or "{}")
        except Exception:
            usage = {}
        if not isinstance(usage, dict):
            usage = {}
        status = str(usage.get("cost_estimate_status") or COST_UNAVAILABLE)
        cost = usage.get("estimated_cost_usd")
        if status == COST_COMPLETE and isinstance(cost, (int, float)):
            spent += float(cost)
            complete += 1
        else:
            unavailable += 1
    limit = deep_research_monthly_limit_usd()
    if unavailable and complete == 0 and not rows:
        remaining_status = "ok"
        remaining = limit
        spent_out: float | None = 0.0
    elif unavailable and not dollar_limits_enforceable():
        remaining_status = COST_UNAVAILABLE
        remaining = None
        spent_out = spent if complete else None
    else:
        remaining = max(0.0, round(limit - spent, 6))
        remaining_status = "exhausted" if remaining <= 0 else "ok"
        spent_out = round(spent, 6)
    return {
        "month_start_utc": utc_month_start_iso(),
        "spent_usd": spent_out,
        "remaining_usd": remaining,
        "monthly_limit_usd": limit,
        "status": remaining_status,
        "complete_estimate_count": complete,
        "unavailable_estimate_count": unavailable,
        "dollar_limits_enforceable": dollar_limits_enforceable(),
    }
