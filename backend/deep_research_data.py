"""Deep Research durable jobs — background worker, cancel, limits, persistence.

Does not write CRM company/contact/relationship masters. Uses research tables only.
Request handlers must not perform schema DDL; migrate_schema owns table creation.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from access import get_default_user, resolve_visibility_client_ids
from deep_research_client import (
    TransportFn,
    build_deep_research_input,
    cancel_provider_response,
    run_deep_research_responses,
    safe_http_url,
)
from deep_research_config import (
    MAX_ATTEMPTS,
    STALE_JOB_SEC,
    deep_research_cache_days,
    deep_research_is_configured,
    deep_research_max_run_usd,
    deep_research_model,
    deep_research_monthly_limit_usd,
    sanitize_error_message,
)
from deep_research_usage import (
    COST_COMPLETE,
    COST_UNAVAILABLE,
    dollar_limits_enforceable,
    estimate_ceiling_cost_usd,
    monthly_usage_from_rows,
    public_limits_payload,
    utc_month_start_iso,
)
from db import get_connection
from models import (
    ResearchCompanyResponse,
    ResearchFindingView,
    ResearchJobView,
    ResearchStartRequest,
)
from research_data import (
    FIT_CRITERIA_NOT_CONFIGURED,
    _authorized_relationships,
    _blank,
    _campaign_criteria_configured,
    _compute_fit,
    _gather_northstar_known,
    _now,
    _resolve_company,
)

log = logging.getLogger("northstar.deep_research")


def _known_for(conn, *, user_id: int, company: dict, working_id: int, visible_ids: list[int], campaign_id: int | None = None):
    return _gather_northstar_known(
        conn,
        user_id=user_id,
        company=company,
        working_for_client_id=working_id,
        visible_ids=visible_ids,
        campaign_id=campaign_id,
    )


ACTIVE_STATUSES = ("queued", "running")
_TRANSPORT_OVERRIDE: TransportFn | None = None
_WORKER_HOOK: Callable[[int], None] | None = None


def set_deep_research_transport_for_tests(transport: TransportFn | None) -> None:
    """Test-only injection — never used in production paths unless set."""
    global _TRANSPORT_OVERRIDE
    _TRANSPORT_OVERRIDE = transport


def set_deep_research_worker_hook_for_tests(hook: Callable[[int], None] | None) -> None:
    """When set, enqueue calls hook(job_id) instead of spawning a background thread."""
    global _WORKER_HOOK
    _WORKER_HOOK = hook


def _jobs_ready(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_research_jobs'"
    ).fetchone()
    return row is not None


def _job_view(row, *, from_cache: bool = False, paid_refresh_required: bool = False) -> ResearchJobView:
    def _json_list(raw: object) -> list:
        try:
            data = json.loads(raw or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    usage: dict = {}
    try:
        parsed = json.loads(row["usage_json"] or "{}")
        if isinstance(parsed, dict):
            usage = parsed
    except Exception:
        usage = {}

    return ResearchJobView(
        job_id=int(row["id"]),
        company_id=int(row["company_id"]),
        working_for_client_id=int(row["working_for_client_id"]),
        campaign_id=int(row["campaign_id"]) if row["campaign_id"] is not None else None,
        research_run_id=int(row["research_run_id"])
        if row["research_run_id"] is not None
        else None,
        research_depth=_blank(row["research_depth"]) or "deep",
        status=_blank(row["status"]) or "queued",
        progress=int(row["progress"] or 0),
        progress_message=_blank(row["progress_message"]),
        cancel_requested=bool(row["cancel_requested"]),
        attempt_count=int(row["attempt_count"] or 0),
        max_attempts=int(row["max_attempts"] or MAX_ATTEMPTS),
        openai_response_id=_blank(row["openai_response_id"]),
        openai_model=_blank(row["openai_model"]),
        usage=usage,
        citations=_json_list(row["citations_json"]),
        sources=_json_list(row["sources_json"]),
        error_message=_blank(row["error_message"]),
        started_at=_blank(row["started_at"]),
        completed_at=_blank(row["completed_at"]),
        created_at=_blank(row["created_at"]),
        updated_at=_blank(row["updated_at"]),
        deep_research_configured=deep_research_is_configured(),
        from_cache=from_cache,
        paid_refresh_required=paid_refresh_required,
        limited_by=str(usage.get("limited_by") or ""),
    )


def _update_job(conn, job_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE company_research_jobs SET {cols} WHERE id = ?",
        [*fields.values(), job_id],
    )


def _active_job(conn, *, company_id: int, client_id: int, campaign_id: int | None):
    if campaign_id is None:
        return conn.execute(
            """
            SELECT * FROM company_research_jobs
            WHERE company_id = ? AND working_for_client_id = ?
              AND campaign_id IS NULL AND status IN ('queued', 'running')
            ORDER BY id DESC LIMIT 1
            """,
            (company_id, client_id),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM company_research_jobs
        WHERE company_id = ? AND working_for_client_id = ?
          AND campaign_id = ? AND status IN ('queued', 'running')
        ORDER BY id DESC LIMIT 1
        """,
        (company_id, client_id, campaign_id),
    ).fetchone()


def _require_admin_user(user) -> None:
    if user is None or not bool(getattr(user, "is_administrator", False)):
        raise PermissionError("Deep Research is administrator-only during the pilot.")


def _cached_completed_job(
    conn,
    *,
    company_id: int,
    client_id: int,
    campaign_id: int | None,
    cache_days: int,
):
    if cache_days <= 0:
        return None
    if campaign_id is None:
        return conn.execute(
            """
            SELECT * FROM company_research_jobs
            WHERE company_id = ? AND working_for_client_id = ?
              AND campaign_id IS NULL AND status = 'completed'
              AND research_run_id IS NOT NULL
              AND completed_at IS NOT NULL
              AND completed_at >= datetime('now', ?)
            ORDER BY id DESC LIMIT 1
            """,
            (company_id, client_id, f"-{int(cache_days)} days"),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM company_research_jobs
        WHERE company_id = ? AND working_for_client_id = ?
          AND campaign_id = ? AND status = 'completed'
          AND research_run_id IS NOT NULL
          AND completed_at IS NOT NULL
          AND completed_at >= datetime('now', ?)
        ORDER BY id DESC LIMIT 1
        """,
        (company_id, client_id, campaign_id, f"-{int(cache_days)} days"),
    ).fetchone()


def _month_completed_usage_rows(conn):
    return conn.execute(
        """
        SELECT usage_json FROM company_research_jobs
        WHERE status = 'completed'
          AND completed_at IS NOT NULL
          AND completed_at >= ?
        """,
        (utc_month_start_iso(),),
    ).fetchall()


def _active_job_count_month(conn) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM company_research_jobs
        WHERE status IN ('queued', 'running')
          AND created_at >= ?
        """,
        (utc_month_start_iso(),),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def _assert_spend_allows_new_job(conn) -> None:
    """Refuse when monthly/run dollar ceilings are enforceable and exhausted."""
    if not dollar_limits_enforceable():
        return
    ceiling, status, _note = estimate_ceiling_cost_usd()
    if status != COST_COMPLETE or ceiling is None:
        return
    max_run = deep_research_max_run_usd()
    if ceiling > max_run + 1e-9:
        raise RuntimeError(
            f"Deep Research refused: configured ceiling estimate ${ceiling:.4f} "
            f"exceeds NORTHSTAR_DEEP_RESEARCH_MAX_RUN_USD (${max_run:.2f})."
        )
    month = monthly_usage_from_rows(_month_completed_usage_rows(conn))
    spent = float(month.get("spent_usd") or 0.0)
    active = _active_job_count_month(conn)
    reserved = active * float(ceiling)
    limit = deep_research_monthly_limit_usd()
    if spent + reserved + float(ceiling) > limit + 1e-9:
        raise RuntimeError(
            "Deep Research monthly NorthStar allowance is exhausted "
            f"(limit ${limit:.2f} UTC month)."
        )


def deep_research_status_payload() -> dict:
    """Presence + limits + monthly usage — never includes secrets."""
    payload = {
        "deep_research_configured": deep_research_is_configured(),
        "limits": public_limits_payload(),
        "pricing": {
            "estimate_available": dollar_limits_enforceable(),
            "note": (
                ""
                if dollar_limits_enforceable()
                else (
                    "Exact cost unavailable — set INPUT/OUTPUT/WEB_SEARCH USD rates "
                    "to enable dollar estimates and dollar ceilings."
                )
            ),
        },
        "monthly": {
            "month_start_utc": utc_month_start_iso(),
            "spent_usd": None,
            "remaining_usd": None,
            "monthly_limit_usd": deep_research_monthly_limit_usd(),
            "status": COST_UNAVAILABLE,
            "dollar_limits_enforceable": dollar_limits_enforceable(),
        },
    }
    try:
        with get_connection() as conn:
            if _jobs_ready(conn):
                month = monthly_usage_from_rows(_month_completed_usage_rows(conn))
                payload["monthly"] = month
    except Exception:
        pass
    return payload


def _parse_ts(raw: object) -> float | None:
    text = _blank(raw)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (
                datetime.strptime(text[:19], fmt)
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _job_is_stale(row, *, now_ts: float | None = None) -> bool:
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    updated = _parse_ts(row["updated_at"]) or _parse_ts(row["started_at"]) or _parse_ts(
        row["created_at"]
    )
    if updated is None:
        return True
    return (now - updated) >= STALE_JOB_SEC


def recover_deep_research_jobs(*, spawn_workers: bool = True) -> list[int]:
    """After backend restart: fail abandoned jobs or re-queue resumable ones.

    Jobs with a provider response id are re-queued so the worker resumes polling.
    Jobs without a response id are failed (cannot safely invent a new provider run
    as a silent duplicate of an unknown in-flight request).
    Returns job ids that were re-queued for worker resume.
    """
    resume_ids: list[int] = []
    with get_connection() as conn:
        if not _jobs_ready(conn):
            return resume_ids
        rows = conn.execute(
            """
            SELECT * FROM company_research_jobs
            WHERE status IN ('queued', 'running')
            """
        ).fetchall()
        now_ts = datetime.now(timezone.utc).timestamp()
        for row in rows:
            if not _job_is_stale(row, now_ts=now_ts):
                continue
            job_id = int(row["id"])
            response_id = _blank(row["openai_response_id"])
            if response_id:
                _update_job(
                    conn,
                    job_id,
                    status="queued",
                    progress_message="Recovering after restart",
                    cancel_requested=0,
                    error_message="",
                )
                resume_ids.append(job_id)
            else:
                _update_job(
                    conn,
                    job_id,
                    status="failed",
                    progress_message="Abandoned after restart",
                    error_message=(
                        "Deep Research job was interrupted before a provider "
                        "response id was stored."
                    ),
                    completed_at=_now(),
                )
        conn.commit()
    if spawn_workers:
        for job_id in resume_ids:
            _enqueue_worker(job_id)
    return resume_ids


def _enqueue_worker(job_id: int) -> None:
    if _WORKER_HOOK is not None:
        _WORKER_HOOK(job_id)
        return
    threading.Thread(
        target=process_deep_research_job,
        args=(job_id,),
        daemon=True,
        name=f"deep-research-{job_id}",
    ).start()


def _finding(
    *,
    finding_type: str,
    field_key: str,
    value: str,
    source_url: str = "",
    source_name: str = "Deep Research",
    evidence_level: str = "verified",
) -> ResearchFindingView:
    return ResearchFindingView(
        finding_type=finding_type,
        field_key=field_key,
        value=_blank(value),
        source_name=source_name,
        source_url=_blank(source_url),
        researched_at=_now(),
        confidence="medium",
        evidence_level=evidence_level,
        provider_id="deep_research",
    )


def _report_to_findings(report: dict[str, Any], sources: list[dict[str, str]]) -> list[ResearchFindingView]:
    primary_url = ""
    if sources:
        primary_url = _blank(sources[0].get("url"))
    identity = report.get("verified_identity") or {}
    if not isinstance(identity, dict):
        identity = {}
    rows: list[ResearchFindingView] = []
    website = _blank(identity.get("website")) or primary_url
    safe_site = safe_http_url(website)
    website_url = safe_site["url"] if safe_site else ""
    name = _blank(identity.get("company_name"))
    if website:
        rows.append(
            _finding(
                finding_type="website",
                field_key="website",
                value=website,
                source_url=website_url,
            )
        )
    if name:
        rows.append(
            _finding(
                finding_type="company_name",
                field_key="company_name",
                value=name,
                source_url=website_url,
            )
        )
    overview = _blank(report.get("overview"))
    if overview:
        rows.append(
            _finding(
                finding_type="overview",
                field_key="overview",
                value=overview,
                source_url=website_url,
            )
        )
    for loc in report.get("locations") or []:
        if _blank(loc):
            rows.append(
                _finding(
                    finding_type="location",
                    field_key="manufacturing_locations",
                    value=_blank(loc),
                    source_url=website_url,
                )
            )
    for prod in report.get("products") or []:
        if _blank(prod):
            rows.append(
                _finding(
                    finding_type="product",
                    field_key="products",
                    value=_blank(prod),
                    source_url=website_url,
                )
            )
    for cap in report.get("manufacturing_capabilities") or []:
        if _blank(cap):
            rows.append(
                _finding(
                    finding_type="capability",
                    field_key="capabilities",
                    value=_blank(cap),
                    source_url=website_url,
                )
            )
    for ind in report.get("industries") or []:
        if _blank(ind):
            rows.append(
                _finding(
                    finding_type="industry",
                    field_key="industries",
                    value=_blank(ind),
                    source_url=website_url,
                )
            )
    for title in report.get("decision_maker_titles") or []:
        if _blank(title):
            rows.append(
                _finding(
                    finding_type="public_contact",
                    field_key="recommended_titles",
                    value=_blank(title),
                    source_url=website_url,
                    evidence_level="supported_inference",
                )
            )
    return rows


def _persist_completed_run(
    conn,
    *,
    job_id: int,
    company: dict[str, Any],
    known,
    campaign_id: int | None,
    campaign_name: str,
    profile: dict[str, str] | None,
    result,
    user_name: str,
    user_id: int | None,
) -> int:
    # Idempotent: retries must not insert a second completed run for the same job.
    existing = conn.execute(
        "SELECT research_run_id, status FROM company_research_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if existing is not None and existing["research_run_id"] is not None:
        return int(existing["research_run_id"])
    if existing is not None and _blank(existing["status"]) in {
        "cancelled",
        "failed",
        "completed",
    }:
        if existing["research_run_id"] is not None:
            return int(existing["research_run_id"])

    report = result.report or {}
    findings = _report_to_findings(report, result.sources or result.citations or [])
    verified_products = [
        f.value for f in findings if f.finding_type == "product" and f.value
    ]
    verified_caps = [
        f.value for f in findings if f.finding_type == "capability" and f.value
    ]
    verified_inds = [
        f.value for f in findings if f.finding_type == "industry" and f.value
    ]
    overview_bits = [
        f.value for f in findings if f.finding_type == "overview" and f.value
    ]
    fit = _compute_fit(
        client_name=known.working_for_client_name,
        client_id=int(known.working_for_client_id),
        profile=profile,
        known=known,
        verified_caps=verified_caps,
        verified_products=verified_products,
        verified_inds=verified_inds,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        research_text_blobs=overview_bits
        + [str(x) for x in (report.get("client_relevant_evidence") or [])],
    )
    # Prefer model outreach / missing info when criteria not configured
    if not _campaign_criteria_configured(profile or {}):
        fit.fit_result = FIT_CRITERIA_NOT_CONFIGURED
        if _blank(report.get("outreach_angle")):
            pass
        miss = list(fit.missing_information or [])
        for m in report.get("missing_information") or []:
            if _blank(m) and _blank(m) not in miss:
                miss.append(_blank(m))
        fit.missing_information = miss

    summary_parts = [
        f"1) Company identity: {_blank(company['company_name'])}",
    ]
    website = next((f.value for f in findings if f.finding_type == "website"), "")
    if website:
        summary_parts[0] += f" · website {website}"
    if overview_bits:
        summary_parts.append(f"2) Overview: {overview_bits[0][:280]}")
    if verified_products:
        summary_parts.append(
            f"Products: {', '.join(verified_products[:6])}."
        )
    if verified_caps:
        summary_parts.append(
            f"Capabilities: {', '.join(verified_caps[:6])}."
        )
    angle = _blank(report.get("outreach_angle"))
    if angle:
        summary_parts.append(f"Outreach angle: {angle}")
    summary_parts.append(f"Rating: {fit.fit_result} — {fit.why}")
    summary = " ".join(summary_parts)

    cur = conn.execute(
        """
        INSERT INTO company_research_runs (
            company_id, working_for_client_id, initiated_by_user_id, initiated_by_name,
            status, summary, providers_used, sources_checked, started_at, completed_at,
            campaign_id, research_depth, openai_response_id, usage_json, citations_json,
            error_message
        ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, 'deep', ?, ?, ?, '')
        """,
        (
            int(company["id"]),
            int(known.working_for_client_id),
            user_id,
            user_name,
            summary,
            "Deep Research (OpenAI Responses + web_search)",
            json.dumps([s.get("url") for s in (result.sources or []) if s.get("url")]),
            _now(),
            _now(),
            campaign_id,
            _blank(result.response_id),
            json.dumps(getattr(result, "usage_audit", None) or result.usage or {}),
            json.dumps(result.citations or result.sources or []),
        ),
    )
    run_id = int(cur.lastrowid)
    for f in findings:
        conn.execute(
            """
            INSERT INTO company_research_findings (
                research_run_id, company_id, finding_type, field_key, value,
                source_name, source_url, researched_at, confidence,
                is_public_contact, contact_name, contact_title, provider_id,
                evidence_level, page_title
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', ?, ?, '')
            """,
            (
                run_id,
                int(company["id"]),
                f.finding_type,
                f.field_key,
                f.value,
                f.source_name,
                f.source_url,
                f.researched_at,
                f.confidence,
                f.provider_id,
                f.evidence_level,
            ),
        )
    conn.execute(
        """
        INSERT INTO company_client_fit (
            company_id, client_id, research_run_id, fit_result, why,
            supporting_evidence, potential_opportunity, concerns,
            missing_information, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, client_id) DO UPDATE SET
            research_run_id = excluded.research_run_id,
            fit_result = excluded.fit_result,
            why = excluded.why,
            supporting_evidence = excluded.supporting_evidence,
            potential_opportunity = excluded.potential_opportunity,
            concerns = excluded.concerns,
            missing_information = excluded.missing_information,
            updated_at = excluded.updated_at
        """,
        (
            int(company["id"]),
            int(known.working_for_client_id),
            run_id,
            fit.fit_result,
            fit.why,
            json.dumps(
                {
                    "supporting_evidence": fit.supporting_evidence,
                    "evidence_chains": fit.evidence_chains,
                    "outreach_angle": angle,
                    "decision_maker_titles": report.get("decision_maker_titles") or [],
                    "client_relevant_evidence": report.get("client_relevant_evidence")
                    or [],
                }
            ),
            fit.potential_opportunity,
            json.dumps(fit.concerns or []),
            json.dumps(fit.missing_information or []),
            _now(),
            _now(),
        ),
    )
    _update_job(
        conn,
        job_id,
        research_run_id=run_id,
        status="completed",
        progress=100,
        progress_message="Deep Research completed",
        openai_response_id=_blank(result.response_id),
        openai_model=_blank(result.model) or deep_research_model(),
        usage_json=json.dumps(
            {
                **(getattr(result, "usage_audit", None) or {}),
                "completed_at": _now(),
                "dollar_limit_guaranteed": False,
            }
        ),
        citations_json=json.dumps(result.citations or []),
        sources_json=json.dumps(result.sources or []),
        report_json=json.dumps(report),
        error_message="",
        completed_at=_now(),
    )
    return run_id


def process_deep_research_job(job_id: int) -> None:
    """Worker entry — safe to call from a thread or synchronously in tests."""
    with get_connection() as conn:
        if not _jobs_ready(conn):
            return
        job = conn.execute(
            "SELECT * FROM company_research_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            return
        if _blank(job["status"]) not in {"queued", "running"}:
            return
        if job["research_run_id"] is not None:
            _update_job(
                conn,
                job_id,
                status="completed",
                progress=100,
                progress_message="Deep Research completed",
                completed_at=_blank(job["completed_at"]) or _now(),
            )
            conn.commit()
            return
        if int(job["cancel_requested"] or 0):
            _update_job(
                conn,
                job_id,
                status="cancelled",
                progress_message="Cancelled",
                completed_at=_now(),
                error_message="",
            )
            conn.commit()
            return

        company = conn.execute(
            "SELECT * FROM companies WHERE id = ?", (int(job["company_id"]),)
        ).fetchone()
        if company is None:
            _update_job(
                conn,
                job_id,
                status="failed",
                error_message="Company not found.",
                completed_at=_now(),
            )
            conn.commit()
            return
        company = dict(company)
        existing_response_id = _blank(job["openai_response_id"])
        # Only count a new provider create as an attempt; resume polls reuse attempt.
        attempt = int(job["attempt_count"] or 0)
        if not existing_response_id:
            attempt += 1
        _update_job(
            conn,
            job_id,
            status="running",
            attempt_count=attempt,
            started_at=_blank(job["started_at"]) or _now(),
            progress=5,
            progress_message="Gathering NorthStar context",
        )
        conn.commit()

        uid = int(job["initiated_by_user_id"] or 0) or None
        if uid is None:
            from access import get_default_user
            uid = get_default_user().id
        visible_ids = resolve_visibility_client_ids(uid)
        known = _known_for(
            conn,
            user_id=uid,
            company=company,
            working_id=int(job["working_for_client_id"]),
            visible_ids=visible_ids,
            campaign_id=int(job["campaign_id"]) if job["campaign_id"] is not None else None,
        )
        from client_setup_data import campaign_to_fit_profile, get_campaign_for_client

        campaign = get_campaign_for_client(
            conn,
            int(job["working_for_client_id"]),
            int(job["campaign_id"]) if job["campaign_id"] is not None else None,
        )
        profile = campaign_to_fit_profile(campaign) if campaign else {}
        campaign_name = _blank((campaign or {}).get("campaign_name")) or "Default"
        campaign_id = int(campaign["id"]) if campaign else None
        criteria_ok = _campaign_criteria_configured(profile)
        prompt = build_deep_research_input(
            company_name=_blank(company["company_name"]),
            website=_blank(company["website"]),
            city=_blank(company["city"]),
            state=_blank(company["state"]),
            client_name=known.working_for_client_name,
            campaign_name=campaign_name,
            campaign_criteria_configured=criteria_ok,
            campaign_summary=_blank(profile.get("summary")),
        )
        conn.commit()

    def should_cancel() -> bool:
        with get_connection() as c2:
            row = c2.execute(
                "SELECT cancel_requested, status FROM company_research_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return True
            return bool(row["cancel_requested"]) or _blank(row["status"]) == "cancelled"

    def on_progress(pct: int, message: str) -> None:
        with get_connection() as c2:
            if should_cancel():
                return
            _update_job(
                c2,
                job_id,
                progress=max(0, min(99, int(pct))),
                progress_message=_blank(message)[:240],
            )
            c2.commit()

    def on_response_id(response_id: str) -> None:
        with get_connection() as c2:
            _update_job(
                c2,
                job_id,
                openai_response_id=_blank(response_id)[:120],
                openai_model=deep_research_model(),
            )
            c2.commit()

    try:
        result = run_deep_research_responses(
            prompt=prompt,
            transport=_TRANSPORT_OVERRIDE,
            should_cancel=should_cancel,
            on_progress=on_progress,
            on_response_id=on_response_id,
            existing_response_id=existing_response_id,
        )
        with get_connection() as conn:
            job = conn.execute(
                "SELECT * FROM company_research_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                return
            if int(job["cancel_requested"] or 0) or _blank(job["status"]) == "cancelled":
                if _blank(result.response_id):
                    cancel_provider_response(
                        _blank(job["openai_response_id"]),
                        transport=_TRANSPORT_OVERRIDE,
                    )
                else:
                    cancel_provider_response(
                        result.response_id, transport=_TRANSPORT_OVERRIDE
                    )
                _update_job(
                    conn,
                    job_id,
                    status="cancelled",
                    progress_message="Cancelled",
                    completed_at=_now(),
                )
                conn.commit()
                return
            company = dict(
                conn.execute(
                    "SELECT * FROM companies WHERE id = ?", (int(job["company_id"]),)
                ).fetchone()
            )
            uid = int(job["initiated_by_user_id"] or 0) or None
            if uid is None:
                from access import get_default_user
                uid = get_default_user().id
            visible_ids = resolve_visibility_client_ids(uid)
            known = _known_for(
                conn,
                user_id=uid,
                company=company,
                working_id=int(job["working_for_client_id"]),
                visible_ids=visible_ids,
                campaign_id=int(job["campaign_id"]) if job["campaign_id"] is not None else None,
            )
            from client_setup_data import campaign_to_fit_profile, get_campaign_for_client

            campaign = get_campaign_for_client(
                conn,
                int(job["working_for_client_id"]),
                int(job["campaign_id"]) if job["campaign_id"] is not None else None,
            )
            profile = campaign_to_fit_profile(campaign) if campaign else {}
            campaign_name = _blank((campaign or {}).get("campaign_name")) or "Default"
            campaign_id = int(campaign["id"]) if campaign else None
            _persist_completed_run(
                conn,
                job_id=job_id,
                company=company,
                known=known,
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                profile=profile,
                result=result,
                user_name=_blank(job["initiated_by_name"]),
                user_id=int(job["initiated_by_user_id"])
                if job["initiated_by_user_id"] is not None
                else None,
            )
            conn.commit()
    except Exception as exc:
        msg = sanitize_error_message(exc)
        cancelled = msg.lower() == "cancelled" or "cancelled" in msg.lower()
        with get_connection() as conn:
            job = conn.execute(
                """
                SELECT attempt_count, max_attempts, cancel_requested,
                       openai_response_id, research_run_id
                FROM company_research_jobs WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                return
            if job["research_run_id"] is not None:
                return
            attempts = int(job["attempt_count"] or 0)
            max_attempts = int(job["max_attempts"] or MAX_ATTEMPTS)
            if cancelled or int(job["cancel_requested"] or 0):
                cancel_provider_response(
                    _blank(job["openai_response_id"]),
                    transport=_TRANSPORT_OVERRIDE,
                )
                _update_job(
                    conn,
                    job_id,
                    status="cancelled",
                    error_message="",
                    progress_message="Cancelled",
                    completed_at=_now(),
                )
            elif attempts < max_attempts and not cancelled:
                _update_job(
                    conn,
                    job_id,
                    status="queued",
                    error_message=msg,
                    progress_message=f"Retry scheduled ({attempts}/{max_attempts})",
                    progress=0,
                )
                conn.commit()
                # Immediate retry in-process (tests) or re-queue thread.
                # Existing openai_response_id causes resume poll — no duplicate create.
                _enqueue_worker(job_id)
                return
            else:
                _update_job(
                    conn,
                    job_id,
                    status="failed",
                    error_message=msg,
                    progress_message="Deep Research failed",
                    completed_at=_now(),
                )
            conn.commit()
        if not cancelled:
            log.exception("deep research job_id=%s failed", job_id)


def start_deep_research_job(
    body: ResearchStartRequest,
    *,
    user_id: int | None = None,
) -> ResearchCompanyResponse:
    if not deep_research_is_configured() and _TRANSPORT_OVERRIDE is None:
        raise RuntimeError("Deep Research is not configured.")

    user = get_default_user() if user_id is None else None
    if user_id is not None:
        from access import get_user_by_id

        user = get_user_by_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_admin_user(user)

    visible_ids = resolve_visibility_client_ids(user.id)
    with get_connection() as conn:
        if not _jobs_ready(conn):
            raise RuntimeError(
                "Deep Research schema is not migrated yet. Run controlled migrate_schema before use."
            )
        company = _resolve_company(
            conn,
            company_id=body.company_id,
            external_record_no=body.external_record_no,
        )
        company_id = int(company["id"])
        rels = _authorized_relationships(
            conn, company_id=company_id, visible_ids=visible_ids
        )
        working_id = body.working_for_client_id
        if working_id is not None and working_id <= 0:
            working_id = None
        if working_id is None:
            if len(rels) == 1:
                working_id = int(rels[0]["client_id"])
            else:
                choices = [
                    {
                        "client_id": int(r["client_id"]),
                        "client_name": _blank(r["client_name"]),
                        "external_record_no": _blank(r["external_record_no"]),
                        "status": _blank(r["status"]),
                    }
                    for r in rels
                ]
                return ResearchCompanyResponse(
                    company_id=company_id,
                    company_name=_blank(company["company_name"]),
                    needs_working_for=True,
                    working_for_choices=choices,
                    summary=(
                        "Which client are you researching this company for? "
                        "Deep Research evaluates fit for one Working For client."
                    ),
                    research_depth="deep",
                    read_only_crm=True,
                )
        if working_id not in visible_ids:
            raise PermissionError("Not authorized for that Working For client.")

        from client_setup_data import get_campaign_for_client

        campaign = get_campaign_for_client(conn, int(working_id), body.campaign_id)
        campaign_id = int(campaign["id"]) if campaign else None
        campaign_name = _blank((campaign or {}).get("campaign_name")) or "Default"

        # End any deferred read txn, recover abandoned jobs, then lock for insert.
        conn.commit()
    recover_deep_research_jobs(spawn_workers=True)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _active_job(
            conn,
            company_id=company_id,
            client_id=int(working_id),
            campaign_id=campaign_id,
        )
        if existing is not None and not body.force_refresh:
            job = _job_view(existing)
            known = _known_for(
                conn,
                user_id=user.id,
                company=dict(company),
                working_id=int(working_id),
                visible_ids=visible_ids,
                campaign_id=campaign_id,
            )
            conn.commit()
            return ResearchCompanyResponse(
                company_id=company_id,
                company_name=_blank(company["company_name"]),
                working_for_client_id=int(working_id),
                working_for_client_name=known.working_for_client_name,
                working_for_record_no=known.working_for_record_no,
                working_for_status=known.working_for_status,
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                northstar_known=known,
                research_depth="deep",
                job=job,
                summary=f"Deep Research {job.status} ({job.progress}%).",
                read_only_crm=True,
                deep_research_usage=dict(job.usage or {}),
            )

        # 30-day cache: reuse unless explicit paid refresh confirmation.
        cached = _cached_completed_job(
            conn,
            company_id=company_id,
            client_id=int(working_id),
            campaign_id=campaign_id,
            cache_days=deep_research_cache_days(),
        )
        if cached is not None and not (
            body.force_refresh and body.confirm_paid_refresh
        ):
            job = _job_view(
                cached,
                from_cache=True,
                paid_refresh_required=bool(body.force_refresh),
            )
            known = _known_for(
                conn,
                user_id=user.id,
                company=dict(company),
                working_id=int(working_id),
                visible_ids=visible_ids,
                campaign_id=campaign_id,
            )
            conn.commit()
            if job.research_run_id:
                from research_data import get_research_run

                resp = get_research_run(int(job.research_run_id), user_id=user.id)
                resp.research_depth = "deep"
                resp.job = job
                resp.citations = list(job.citations or [])
                resp.deep_research_usage = dict(job.usage or {})
                resp.deep_research_from_cache = True
                resp.deep_research_paid_refresh_required = bool(body.force_refresh)
                if body.force_refresh and not body.confirm_paid_refresh:
                    resp.summary = (
                        "Cached Deep Research is still fresh. "
                        "Confirm paid refresh to run again inside the cache window."
                    )
                return resp
            return ResearchCompanyResponse(
                company_id=company_id,
                company_name=_blank(company["company_name"]),
                working_for_client_id=int(working_id),
                working_for_client_name=known.working_for_client_name,
                working_for_record_no=known.working_for_record_no,
                working_for_status=known.working_for_status,
                campaign_id=campaign_id,
                campaign_name=campaign_name,
                northstar_known=known,
                research_depth="deep",
                job=job,
                summary="Cached Deep Research is still fresh.",
                read_only_crm=True,
                deep_research_usage=dict(job.usage or {}),
                deep_research_from_cache=True,
                deep_research_paid_refresh_required=bool(body.force_refresh),
            )

        # Concurrent duplicate paid runs: active job already blocks above;
        # force_refresh supersedes only after spend check.
        try:
            _assert_spend_allows_new_job(conn)
        except RuntimeError:
            conn.commit()
            raise

        if existing is not None and body.force_refresh:
            prior_response_id = _blank(existing["openai_response_id"])
            _update_job(
                conn,
                int(existing["id"]),
                cancel_requested=1,
                status="cancelled",
                progress_message="Superseded by force refresh",
                completed_at=_now(),
            )
        else:
            prior_response_id = ""

        cur = conn.execute(
            """
            INSERT INTO company_research_jobs (
                company_id, working_for_client_id, campaign_id, research_depth,
                status, progress, progress_message, cancel_requested, attempt_count,
                max_attempts, openai_model, initiated_by_user_id, initiated_by_name,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'deep', 'queued', 0, 'Queued', 0, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                int(working_id),
                campaign_id,
                MAX_ATTEMPTS,
                deep_research_model(),
                user.id,
                _blank(user.full_name) or _blank(user.email),
                _now(),
                _now(),
            ),
        )
        job_id = int(cur.lastrowid)
        conn.commit()
        if prior_response_id:
            cancel_provider_response(
                prior_response_id, transport=_TRANSPORT_OVERRIDE
            )
        job_row = conn.execute(
            "SELECT * FROM company_research_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        known = _known_for(
            conn,
            user_id=user.id,
            company=dict(company),
            working_id=int(working_id),
            visible_ids=visible_ids,
            campaign_id=campaign_id,
        )
        job = _job_view(job_row)

    _enqueue_worker(job_id)

    return ResearchCompanyResponse(
        company_id=company_id,
        company_name=_blank(company["company_name"]),
        working_for_client_id=int(working_id),
        working_for_client_name=known.working_for_client_name,
        working_for_record_no=known.working_for_record_no,
        working_for_status=known.working_for_status,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        northstar_known=known,
        research_depth="deep",
        job=job,
        summary="Deep Research queued.",
        read_only_crm=True,
        deep_research_usage=dict(job.usage or {}),
    )


def get_deep_research_job(job_id: int, *, user_id: int | None = None) -> ResearchCompanyResponse:
    user = get_default_user() if user_id is None else None
    if user_id is not None:
        from access import get_user_by_id

        user = get_user_by_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    visible_ids = resolve_visibility_client_ids(user.id)
    with get_connection() as conn:
        if not _jobs_ready(conn):
            raise LookupError("Deep Research job not found.")
        row = conn.execute(
            "SELECT * FROM company_research_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Deep Research job not found.")
        if int(row["working_for_client_id"]) not in visible_ids:
            raise PermissionError("Not authorized.")
        job = _job_view(row)
        company = dict(
            conn.execute(
                "SELECT * FROM companies WHERE id = ?", (int(row["company_id"]),)
            ).fetchone()
        )
        if job.status == "completed" and job.research_run_id:
            from research_data import get_research_run

            resp = get_research_run(int(job.research_run_id), user_id=user.id)
            resp.research_depth = "deep"
            resp.job = job
            resp.citations = list(job.citations or [])
            resp.deep_research_usage = dict(job.usage or {})
            return resp
        known = _known_for(
            conn,
            user_id=user.id,
            company=company,
            working_id=int(row["working_for_client_id"]),
            visible_ids=visible_ids,
            campaign_id=int(row["campaign_id"]) if row["campaign_id"] is not None else None,
        )
        return ResearchCompanyResponse(
            company_id=int(company["id"]),
            company_name=_blank(company["company_name"]),
            working_for_client_id=int(row["working_for_client_id"]),
            working_for_client_name=known.working_for_client_name,
            working_for_record_no=known.working_for_record_no,
            working_for_status=known.working_for_status,
            campaign_id=int(row["campaign_id"]) if row["campaign_id"] is not None else None,
            research_depth="deep",
            job=job,
            northstar_known=known,
            summary=job.progress_message or f"Deep Research {job.status}",
            citations=list(job.citations or []),
            sources=list(job.sources or []),
            read_only_crm=True,
            missing_information=[job.error_message] if job.error_message else [],
            deep_research_usage=dict(job.usage or {}),
        )


def cancel_deep_research_job(job_id: int, *, user_id: int | None = None) -> ResearchCompanyResponse:
    user = get_default_user() if user_id is None else None
    if user_id is not None:
        from access import get_user_by_id

        user = get_user_by_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_admin_user(user)
    visible_ids = resolve_visibility_client_ids(user.id)
    with get_connection() as conn:
        if not _jobs_ready(conn):
            raise LookupError("Deep Research job not found.")
        row = conn.execute(
            "SELECT * FROM company_research_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Deep Research job not found.")
        if int(row["working_for_client_id"]) not in visible_ids:
            raise PermissionError("Not authorized.")
        status = _blank(row["status"])
        response_id = ""
        if status in ACTIVE_STATUSES:
            response_id = _blank(row["openai_response_id"])
            _update_job(
                conn,
                job_id,
                cancel_requested=1,
                progress_message="Cancel requested",
            )
            if status == "queued":
                _update_job(
                    conn,
                    job_id,
                    status="cancelled",
                    completed_at=_now(),
                    progress_message="Cancelled",
                )
            conn.commit()
    if response_id:
        cancel_provider_response(response_id, transport=_TRANSPORT_OVERRIDE)
    return get_deep_research_job(job_id, user_id=user.id)
