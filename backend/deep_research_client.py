"""OpenAI Responses API client for Deep Research (web_search + citations).

Never logs API keys. Tests inject a fake transport — zero live OpenAI calls.
Verified against OpenAI docs: Responses background mode, web_search tool,
url_citation annotations, GET retrieve, POST cancel.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from deep_research_config import (
    MAX_OUTPUT_TOKENS,
    MAX_WEB_SEARCH_CALLS,
    POLL_INTERVAL_SEC,
    POLL_TIMEOUT_SEC,
    REASONING_EFFORT,
    SEARCH_CONTEXT_SIZE,
    deep_research_api_key,
    deep_research_base_url,
    deep_research_model,
    sanitize_error_message,
)
from deep_research_usage import build_usage_record, count_web_search_calls, extract_token_usage

TransportFn = Callable[[str, str, dict[str, Any] | None, dict[str, str]], dict[str, Any]]

MAX_CITATIONS = 40
MAX_REPORT_CHARS = 120_000
SAFE_URL_SCHEMES = {"http", "https"}


@dataclass
class DeepResearchResult:
    response_id: str = ""
    model: str = ""
    output_text: str = ""
    report: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, str]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    usage_audit: dict[str, Any] = field(default_factory=dict)
    raw_status: str = ""
    limited_by: str = ""


def safe_http_url(url: object, *, title: str = "") -> dict[str, str] | None:
    """Allow only http(s) URLs for citations/sources (SSRF-safe for UI links)."""
    raw = str(url or "").strip()
    if not raw or len(raw) > 2000:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme.lower() not in SAFE_URL_SCHEMES:
        return None
    if not parsed.netloc:
        return None
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        return None
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return None
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return None
        except (IndexError, ValueError):
            pass
    return {"url": raw, "title": (title or "").strip()[:300] or raw}


def _default_http_transport(
    method: str, url: str, body: dict[str, Any] | None, headers: dict[str, str]
) -> dict[str, Any]:
    """Live HTTP transport — only used when a real run is executed outside tests."""
    from urllib import error, request

    method_u = method.upper()
    data = None
    if method_u != "GET" and body is not None:
        data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method=method_u)
    try:
        with request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:400]
        except Exception:
            detail = str(exc)
        raise RuntimeError(sanitize_error_message(f"OpenAI HTTP {exc.code}: {detail}")) from exc
    except Exception as exc:
        raise RuntimeError(sanitize_error_message(exc)) from exc


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_output_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("output_text") or "").strip()
    if text:
        return text[:MAX_REPORT_CHARS]
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {
                    "output_text",
                    "text",
                }:
                    chunks.append(str(part.get("text") or ""))
    return "\n".join(c for c in chunks if c).strip()[:MAX_REPORT_CHARS]


def _extract_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(url: str, title: str = "") -> None:
        safe = safe_http_url(url, title=title)
        if not safe or safe["url"] in seen:
            return
        seen.add(safe["url"])
        found.append(safe)

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action") or {}
            if isinstance(action, dict):
                for src in action.get("sources") or []:
                    if isinstance(src, dict):
                        add(str(src.get("url") or ""), str(src.get("title") or ""))
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                for ann in part.get("annotations") or []:
                    if not isinstance(ann, dict):
                        continue
                    if ann.get("type") in {"url_citation", "citation"}:
                        add(str(ann.get("url") or ""), str(ann.get("title") or ""))
        if len(found) >= MAX_CITATIONS:
            break
    return found[:MAX_CITATIONS]


def _parse_report_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    if len(raw) > MAX_REPORT_CHARS:
        raw = raw[:MAX_REPORT_CHARS]
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"overview": str(data)[:8000]}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
    return {
        "overview": raw[:4000],
        "missing_information": ["Structured JSON parse failed."],
    }


def build_deep_research_input(
    *,
    company_name: str,
    website: str,
    city: str,
    state: str,
    client_name: str,
    campaign_name: str,
    campaign_criteria_configured: bool,
    campaign_summary: str,
) -> str:
    criteria_note = (
        "Campaign targeting criteria ARE configured — include a campaign_fit object "
        "with fit_result, why, evidence, missing_information."
        if campaign_criteria_configured
        else (
            "Campaign targeting criteria are NOT configured — set campaign_fit.fit_result "
            "to 'Campaign Criteria Not Configured' and still return the full company profile."
        )
    )
    return (
        f"Research the company '{company_name}' for NorthStar sales intelligence.\n"
        f"Known website: {website or '(unknown)'}\n"
        f"Known location: {', '.join(x for x in [city, state] if x) or '(unknown)'}\n"
        f"Working For client: {client_name}\n"
        f"Campaign: {campaign_name or 'Default'}\n"
        f"Client/campaign context: {campaign_summary or '(none)'}\n"
        f"{criteria_note}\n"
        "Use web search. Prefer the official website and reputable sources. "
        "Return ONLY JSON with keys: "
        "verified_identity (company_name, website), overview, locations (array), "
        "products (array), manufacturing_capabilities (array), industries (array), "
        "client_relevant_evidence (array of strings), "
        "campaign_fit (fit_result, why, evidence array, missing_information array), "
        "decision_maker_titles (array), outreach_angle, missing_information (array), "
        "sources (array of {title, url}). "
        "Do not invent CRM records. Engagement history is out of scope."
    )


def cancel_provider_response(
    response_id: str,
    *,
    transport: TransportFn | None = None,
) -> None:
    """Best-effort provider cancel: POST /v1/responses/{id}/cancel (background only)."""
    rid = (response_id or "").strip()
    if not rid:
        return
    api_key = deep_research_api_key()
    if not api_key:
        return
    base = deep_research_base_url()
    http = transport or _default_http_transport
    try:
        http("POST", f"{base}/responses/{rid}/cancel", {}, _headers(api_key))
    except Exception:
        # Cancellation is best-effort; local job state still flips to cancelled.
        pass


def run_deep_research_responses(
    *,
    prompt: str,
    transport: TransportFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, str], None] | None = None,
    on_response_id: Callable[[str], None] | None = None,
    existing_response_id: str = "",
) -> DeepResearchResult:
    """Create a Responses API web_search job (background) and poll to completion.

    When existing_response_id is set (restart recovery), skip create and poll
    GET /responses/{id} until a terminal status.
    """
    api_key = deep_research_api_key()
    if not api_key:
        raise RuntimeError("Deep Research is not configured.")
    model = deep_research_model()
    base = deep_research_base_url()
    http = transport or _default_http_transport
    headers = _headers(api_key)

    resume_id = (existing_response_id or "").strip()
    payload: dict[str, Any]
    if resume_id:
        response_id = resume_id
        if on_response_id:
            on_response_id(response_id)
        if on_progress:
            on_progress(25, "Resuming Deep Research")
        payload = http("GET", f"{base}/responses/{response_id}", None, headers)
        status = str(payload.get("status") or "in_progress").strip().lower()
    else:
        if on_progress:
            on_progress(10, "Submitting Deep Research request")

        # Official docs: background=true for long research; store=true so the
        # response is retained for polling beyond the temporary window;
        # tools=[{type: web_search}]; reasoning.effort + max_output_tokens.
        create_body: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "tools": [
                {
                    "type": "web_search",
                    "search_context_size": SEARCH_CONTEXT_SIZE,
                }
            ],
            "tool_choice": "required",
            "background": True,
            "store": True,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_tool_calls": MAX_WEB_SEARCH_CALLS,
            "reasoning": {"effort": REASONING_EFFORT},
        }
        created = http("POST", f"{base}/responses", create_body, headers)
        response_id = str(created.get("id") or "").strip()
        if response_id and on_response_id:
            on_response_id(response_id)
        status = str(created.get("status") or "").strip().lower()
        payload = created
        if on_progress:
            on_progress(25, "Deep Research running")

    deadline = time.time() + POLL_TIMEOUT_SEC
    limited_by = ""
    while status in {"queued", "in_progress", "pending"}:
        if should_cancel and should_cancel():
            if response_id:
                cancel_provider_response(response_id, transport=http)
            raise RuntimeError("cancelled")
        if time.time() > deadline:
            raise RuntimeError("Deep Research timed out waiting for OpenAI response.")
        # Server-side limit enforcement while polling.
        search_count = count_web_search_calls(payload)
        if search_count > MAX_WEB_SEARCH_CALLS:
            limited_by = "web_search_calls"
            if response_id:
                cancel_provider_response(response_id, transport=http)
            raise RuntimeError(
                f"Deep Research cancelled: exceeded {MAX_WEB_SEARCH_CALLS} web-search calls."
            )
        tokens = extract_token_usage(payload)
        if tokens["output_tokens"] > MAX_OUTPUT_TOKENS or (
            tokens["total_tokens"] > 0
            and tokens["output_tokens"] >= MAX_OUTPUT_TOKENS
            and status == "incomplete"
        ):
            limited_by = "max_output_tokens"
            if response_id and status in {"queued", "in_progress", "pending"}:
                cancel_provider_response(response_id, transport=http)
                raise RuntimeError(
                    f"Deep Research cancelled: exceeded {MAX_OUTPUT_TOKENS} generated tokens."
                )
        time.sleep(POLL_INTERVAL_SEC)
        if not response_id:
            break
        if on_progress:
            on_progress(45, "Waiting for web search synthesis")
        payload = http("GET", f"{base}/responses/{response_id}", None, headers)
        status = str(payload.get("status") or "").strip().lower()
        response_id = str(payload.get("id") or response_id).strip()

    if should_cancel and should_cancel():
        if response_id:
            cancel_provider_response(response_id, transport=http)
        raise RuntimeError("cancelled")

    # Final limit checks on terminal payload.
    final_searches = count_web_search_calls(payload)
    if final_searches > MAX_WEB_SEARCH_CALLS:
        limited_by = limited_by or "web_search_calls"
        raise RuntimeError(
            f"Deep Research refused result: exceeded {MAX_WEB_SEARCH_CALLS} web-search calls."
        )
    final_tokens = extract_token_usage(payload)
    if final_tokens["output_tokens"] > MAX_OUTPUT_TOKENS:
        limited_by = limited_by or "max_output_tokens"
        # Keep partial parse only when provider already stopped at incomplete;
        # otherwise refuse to treat as a normal success without recording limit.
        if status not in {"incomplete", "completed", "succeeded", ""}:
            raise RuntimeError(
                f"Deep Research refused result: exceeded {MAX_OUTPUT_TOKENS} generated tokens."
            )

    terminal_ok = {"completed", "succeeded", ""}
    terminal_bad = {"failed", "cancelled", "incomplete"}
    if status not in terminal_ok and status not in terminal_bad:
        if not _extract_output_text(payload):
            raise RuntimeError(
                sanitize_error_message(
                    f"Deep Research ended with status={status or 'unknown'}"
                )
            )
    if status in {"failed", "cancelled"}:
        err = payload.get("error") or status
        raise RuntimeError(sanitize_error_message(err))
    if status == "incomplete":
        incomplete = payload.get("incomplete_details") or {}
        reason = ""
        if isinstance(incomplete, dict):
            reason = str(incomplete.get("reason") or "")
        if reason == "max_output_tokens":
            limited_by = limited_by or "max_output_tokens"

    if on_progress:
        on_progress(80, "Parsing citations and report")

    text = _extract_output_text(payload)
    report = _parse_report_json(text)
    citations = _extract_citations(payload)
    sources = list(citations)
    seen = {s["url"] for s in sources}
    for row in report.get("sources") or []:
        if not isinstance(row, dict):
            continue
        safe = safe_http_url(row.get("url"), title=str(row.get("title") or ""))
        if not safe or safe["url"] in seen:
            continue
        seen.add(safe["url"])
        sources.append(safe)
        if len(sources) >= MAX_CITATIONS:
            break

    raw_usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    model_name = str(payload.get("model") or model)
    usage_audit = build_usage_record(
        payload=payload,
        model=model_name,
        limited_by=limited_by,
    )
    return DeepResearchResult(
        response_id=response_id,
        model=model_name,
        output_text=text,
        report=report,
        citations=citations[:MAX_CITATIONS],
        sources=sources[:MAX_CITATIONS],
        usage=dict(raw_usage),
        usage_audit=usage_audit,
        raw_status=status or "completed",
        limited_by=limited_by,
    )
