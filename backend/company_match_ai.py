"""Optional AI review of a small company-duplicate shortlist.

Uses an already-configured AI env key if one exists (OPENAI_API_KEY or
NORTHSTAR_AI_API_KEY). Does not create a new secret, log key values, or write
CRM records. If no key is configured, matching still works without AI.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, request

ALLOWED_ASSESSMENTS = {
    "likely same company": "Likely same company",
    "possible match": "Possible match",
    "probably different": "Probably different",
}

_TIMEOUT_SEC = 8


def ai_is_configured() -> bool:
    return bool(
        (os.getenv("NORTHSTAR_AI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    )


def _min_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_id": int(row["company_id"]),
        "company_name": row.get("company_name") or "",
        "website": row.get("website") or "",
        "address": row.get("address") or "",
        "city": row.get("city") or "",
        "state": row.get("state") or "",
        "phone": row.get("phone") or "",
        "external_record_no": row.get("external_record_no") or "",
        "score": int(row.get("score") or 0),
        "reasons": list(row.get("reasons") or []),
    }


def _parse_assessments(payload: object) -> dict[int, dict[str, str]]:
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    rows = data
    if isinstance(data, dict):
        rows = data.get("assessments") or data.get("results") or []
    if not isinstance(rows, list):
        return {}
    out: dict[int, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            cid = int(row.get("company_id"))
        except (TypeError, ValueError):
            continue
        raw = str(row.get("assessment") or "").strip().lower()
        assessment = ALLOWED_ASSESSMENTS.get(raw, "")
        if not assessment:
            continue
        out[cid] = {
            "ai_assessment": assessment,
            "ai_explanation": str(row.get("explanation") or "").strip()[:280],
            "ai_confidence": str(row.get("confidence") or "").strip()[:32],
        }
    return out


def _call_configured_ai(prompt: str) -> str:
    """Best-effort OpenAI-compatible chat. Returns empty string on any failure."""
    key = (os.getenv("NORTHSTAR_AI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return ""
    base = (
        os.getenv("NORTHSTAR_AI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    model = (os.getenv("NORTHSTAR_AI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You compare company records. Reply with JSON only: "
                        '{"assessments":[{"company_id":1,"assessment":"Likely same company"'
                        ',"explanation":"...","confidence":"high"}]} . '
                        "assessment must be one of: Likely same company, Possible match, "
                        "Probably different. Do not invent records. Do not recommend writes."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
    ).encode("utf-8")
    req = request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError):
        return ""
    try:
        return str(raw["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def review_duplicate_candidates(query: dict[str, str], shortlist: list[dict[str, Any]]) -> None:
    """Annotate shortlist in place. Never creates, links, merges, or updates records."""
    if not shortlist:
        return
    if not ai_is_configured():
        return
    payload = {
        "entered": {
            "company_name": query.get("company_name") or "",
            "website": query.get("website") or "",
            "address": query.get("address") or "",
            "city": query.get("city") or "",
            "state": query.get("state") or "",
            "phone": query.get("phone") or "",
            "external_record_no": query.get("external_record_no") or "",
        },
        "candidates": [_min_candidate(row) for row in shortlist],
    }
    text = _call_configured_ai(json.dumps(payload, ensure_ascii=True))
    if not text:
        return
    assessments = _parse_assessments(text)
    for row in shortlist:
        note = assessments.get(int(row["company_id"]))
        if not note:
            continue
        row["ai_available"] = True
        row["ai_assessment"] = note["ai_assessment"]
        row["ai_explanation"] = note["ai_explanation"]
        row["ai_confidence"] = note["ai_confidence"]
