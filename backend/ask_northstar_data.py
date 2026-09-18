"""Ask NorthStar — Phase 1 read-only retrieval intelligence.

Architecture:
- Intent classifier (rule-based; no LLM) maps natural language to retrieval plans.
- Answers are assembled only from authorized NorthStar database records.
- Research providers (web / data provider) are declared but disabled for Phase 1.
- Writes are never performed by Ask NorthStar.
"""

from __future__ import annotations

from staff_context import resolve_staff_actor

import json
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Literal

from access import (
    get_default_user,
    resolve_visibility_client_ids,
    user_can_access_client,
)
from db import DB_PATH, get_connection
from models import (
    AskCompanyCard,
    AskContactCard,
    AskHistoryItem,
    AskHistoryResponse,
    AskLink,
    AskMilestoneBadge,
    AskNoteHit,
    AskNorthStarRequest,
    AskNorthStarResponse,
    AskResearchOption,
    AskSection,
    AskSource,
)
from opportunities_data import list_cross_client_opportunities
from search_data import search as run_search
from work_queue_data import list_work_queue
from data_steward import sql_active_ccr, sql_active_company, sql_active_contact

_ASK_INCLUDE_ARCHIVED: ContextVar[bool] = ContextVar("ask_include_archived", default=False)

ScopeMode = Literal["all", "active_client"]

# Declared for Phase 2A — Public Web research is available from Research Company.
RESEARCH_OPTIONS: list[AskResearchOption] = [
    AskResearchOption(
        id="web",
        label="Research This Company",
        status="Available",
        enabled=True,
        description=(
            "Opens Research Company: NorthStar-first intelligence, public web research, "
            "proposed CRM updates, and fit for the Working For client."
        ),
    ),
    AskResearchOption(
        id="data_provider",
        label="Research Data Provider",
        status="ZoomInfo not connected",
        enabled=False,
        description=(
            "Future ZoomInfo enrichment: firmographics, decision makers, and phones."
        ),
    ),
]

NO_DATA = "NorthStar does not currently have that information."

_DECISION_MAKER_HINTS = (
    "buyer",
    "purchas",
    "procure",
    "sourcing",
    "supply chain",
    "director",
    "manager",
    "vp ",
    "vice president",
    "owner",
    "president",
)

_MILESTONE_LABELS = {
    "purchase order": "Purchase Order",
    "quote": "Quote",
    "appointment set": "Appointment",
    "appointment": "Appointment",
    "weblead": "WebLead",
    "web lead": "WebLead",
    "hot": "Hot",
}


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db_exists() -> bool:
    return DB_PATH.exists()


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def ensure_ask_northstar_schema(conn=None) -> None:
    """Idempotent history table for per-user Ask NorthStar questions."""
    owns = conn is None
    if owns:
        if not _db_exists():
            return
        conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ask_northstar_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'all',
                active_client_id INTEGER,
                intent TEXT NOT NULL DEFAULT '',
                answer_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ask_history_user_created
                ON ask_northstar_history(user_id, created_at DESC)
            """
        )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _placeholders(ids: list[int]) -> str:
    return ",".join("?" for _ in ids)


def _ask_sql_company(conn, alias: str = "co") -> str:
    return "1=1" if _ASK_INCLUDE_ARCHIVED.get() else sql_active_company(conn, alias)


def _ask_sql_ccr(conn, alias: str = "ccr") -> str:
    return "1=1" if _ASK_INCLUDE_ARCHIVED.get() else sql_active_ccr(conn, alias)


def _ask_sql_contact(conn, alias: str = "ct") -> str:
    return "1=1" if _ASK_INCLUDE_ARCHIVED.get() else sql_active_contact(conn, alias)


def _ask_visible_company_clause(conn, company_alias: str, id_placeholders: str) -> str:
    extra = "" if _ASK_INCLUDE_ARCHIVED.get() else f" AND {sql_active_ccr(conn, 'ccr_vis')}"
    return (
        f"{_ask_sql_company(conn, company_alias)} AND EXISTS ("
        f"SELECT 1 FROM client_company_relationships ccr_vis "
        f"WHERE ccr_vis.company_id = {company_alias}.id "
        f"AND ccr_vis.client_id IN ({id_placeholders}){extra})"
    )


def _resolve_scope_client_ids(
    user_id: int,
    *,
    scope: ScopeMode,
    active_client_id: int | None,
) -> tuple[list[int], list[int], str, int | None]:
    """Return (work_client_ids, intel_client_ids, scope_label, active_client_id_resolved).

    work_client_ids: narrowed for Work Next / notes / status when Active Client scope.
    intel_client_ids: all clients the user is authorized to search (never broader).
    """
    authorized = resolve_visibility_client_ids(
        user_id,
        selected_client_id=None,
        purpose="search",
    )
    if scope == "active_client":
        if active_client_id is None or active_client_id <= 0:
            raise ValueError("Active Client scope requires a selected client.")
        if active_client_id not in authorized and not user_can_access_client(
            user_id, active_client_id
        ):
            raise PermissionError("Not authorized for Active Client.")
        if active_client_id not in authorized:
            # Assigned work client that may not appear in search-all list — still allow.
            authorized = sorted({*authorized, active_client_id})
        with get_connection() as conn:
            row = conn.execute(
                "SELECT name FROM clients WHERE id = ?",
                (active_client_id,),
            ).fetchone()
        name = _blank(row["name"]) if row else f"Client {active_client_id}"
        return [active_client_id], authorized, name, active_client_id

    return authorized, authorized, "All NorthStar", None


def _client_name_map(conn, client_ids: list[int]) -> dict[int, str]:
    if not client_ids:
        return {}
    rows = conn.execute(
        f"SELECT id, name FROM clients WHERE id IN ({_placeholders(client_ids)})",
        client_ids,
    ).fetchall()
    return {int(r["id"]): _blank(r["name"]) for r in rows}


def _find_client_by_text(
    conn,
    text: str,
    visible_ids: list[int],
) -> dict[str, Any] | None:
    needle = _blank(text).lower()
    if not needle or not visible_ids:
        return None
    rows = conn.execute(
        f"""
        SELECT id, name, code FROM clients
        WHERE id IN ({_placeholders(visible_ids)})
        ORDER BY length(name) ASC
        """,
        visible_ids,
    ).fetchall()
    # Prefer exact / startswith on code or name; then contains.
    exact = []
    starts = []
    contains = []
    for r in rows:
        name = _blank(r["name"]).lower()
        code = _blank(r["code"]).lower()
        first = name.split()[0] if name else ""
        if needle in {name, code, first}:
            exact.append(r)
        elif name.startswith(needle) or first.startswith(needle):
            starts.append(r)
        elif needle in name or needle in code:
            contains.append(r)
    pick = (exact or starts or contains)
    if not pick:
        return None
    r = pick[0]
    return {"id": int(r["id"]), "name": _blank(r["name"]), "code": _blank(r["code"])}


def _find_companies_by_name(
    conn,
    name_query: str,
    *,
    visible_ids: list[int],
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Resolve natural-language company references to master company rows.

    Supports partial names (Trailerman → Trailerman Trailers, 3M → 3M Co)
    when the match is unambiguous. Returns multiple rows when ambiguous.
    """
    needle = _blank(name_query)
    if not needle:
        return []
    ids = visible_ids if visible_ids else [-1]
    ph = _placeholders(ids)
    vis = _ask_visible_company_clause(conn, "co", ph)
    rows = conn.execute(
        f"""
        SELECT DISTINCT
            co.id AS company_id,
            co.company_name,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                AS external_record_no,
            co.city,
            co.state
        FROM companies co
        LEFT JOIN client_company_relationships ccr
            ON ccr.company_id = co.id
           AND ccr.client_id IN ({ph})
           AND {_ask_sql_ccr(conn, "ccr")}
        WHERE ({vis})
          AND (
            lower(co.company_name) = lower(?)
            OR lower(co.company_name) LIKE lower(?)
            OR lower(co.company_name) LIKE lower(?)
            OR lower(co.company_name) LIKE lower(?)
          )
        ORDER BY
            CASE
                WHEN lower(co.company_name) = lower(?) THEN 0
                WHEN lower(co.company_name) LIKE lower(?) THEN 1
                WHEN lower(co.company_name) LIKE lower(?) THEN 2
                ELSE 3
            END,
            length(co.company_name) ASC,
            co.company_name COLLATE NOCASE
        LIMIT ?
        """,
        [
            *ids,
            *ids,
            needle,
            f"{needle}%",
            f"{needle} %",
            f"%{needle}%",
            needle,
            f"{needle}%",
            f"{needle} %",
            max(limit, 12),
        ],
    ).fetchall()
    matches = [dict(r) for r in rows]
    if not matches:
        # Token fallback: first word of query vs company name tokens
        token = needle.split()[0]
        if len(token) >= 2:
            rows = conn.execute(
                f"""
                SELECT
                    co.id AS company_id,
                    co.company_name,
                    co.external_record_no,
                    co.city,
                    co.state
                FROM companies co
                WHERE ({vis})
                  AND (
                    lower(co.company_name) = lower(?)
                    OR lower(co.company_name) LIKE lower(?)
                    OR lower(co.company_name) LIKE lower(?)
                  )
                ORDER BY
                    CASE
                        WHEN lower(co.company_name) = lower(?) THEN 0
                        WHEN lower(co.company_name) LIKE lower(?) THEN 1
                        ELSE 2
                    END,
                    length(co.company_name) ASC,
                    co.company_name COLLATE NOCASE
                LIMIT ?
                """,
                (
                    *ids,
                    token,
                    f"{token}%",
                    f"{token} %",
                    token,
                    f"{token}%",
                    max(limit, 12),
                ),
            ).fetchall()
            matches = [dict(r) for r in rows]

    if not matches:
        return []

    needle_l = needle.lower()
    exact = [m for m in matches if _blank(m["company_name"]).lower() == needle_l]
    if len(exact) == 1:
        return exact[:limit]
    prefix = [
        m
        for m in matches
        if _blank(m["company_name"]).lower().startswith(needle_l)
        or _blank(m["company_name"]).lower().startswith(needle_l + " ")
    ]
    if len(prefix) == 1:
        return prefix[:limit]
    if len(prefix) > 1:
        return prefix[:limit]
    # Single contains match is unambiguous enough
    if len(matches) == 1:
        return matches
    return matches[:limit]


def _company_match_confidence(name_query: str, company_name: str) -> int:
    """Lower is better. 0=exact, 1=prefix, 2=contains."""
    needle = _blank(name_query).lower()
    name = _blank(company_name).lower()
    if name == needle:
        return 0
    if name.startswith(needle) or name.startswith(needle + " "):
        return 1
    if needle in name:
        return 2
    return 9


def _extract_call_prep(question: str) -> dict[str, Any] | None:
    """Detect call-prep / account-briefing questions. Returns company/client fragments."""
    q = _blank(question)
    lower = q.lower()
    is_call_prep = any(
        hint in lower
        for hint in (
            "before i call",
            "before calling",
            "prepare me to call",
            "call brief",
            "brief me on",
            "account briefing",
            "prepare me for a call",
            "need to know about",
            "what should i know before",
        )
    ) or bool(
        re.search(
            r"(?:call\s+prep|account\s+brief|before\s+(?:i\s+)?call)",
            lower,
        )
    )
    if not is_call_prep:
        return None

    patterns = [
        # Explicit Working For client
        r"what\s+should\s+i\s+know\s+before\s+(?:i\s+)?call\s+(.+?)\s+for\s+(.+?)(?:\?|$)",
        r"prepare\s+me\s+to\s+call\s+(.+?)\s+for\s+(.+?)(?:\?|$)",
        r"(?:give\s+me\s+(?:a\s+)?)?call\s+brief\s+for\s+(.+?)\s+for\s+(.+?)(?:\?|$)",
        r"brief\s+me\s+on\s+(.+?)\s+for\s+(.+?)(?:\?|$)",
        r"what\s+do\s+i\s+need\s+to\s+know\s+about\s+(.+?)\s+before\s+calling"
        r"(?:\s+for\s+(.+?))?(?:\?|$)",
        r"about\s+(.+?)\s+before\s+calling(?:\s+for\s+(.+?))?(?:\?|$)",
        # Company only (Active Client / disambiguation later)
        r"what\s+should\s+i\s+know\s+before\s+(?:i\s+)?call\s+(.+?)(?:\?|$)",
        r"prepare\s+me\s+to\s+call\s+(.+?)(?:\?|$)",
        r"(?:give\s+me\s+(?:a\s+)?)?call\s+brief\s+for\s+(.+?)(?:\?|$)",
        r"brief\s+me\s+on\s+(.+?)(?:\?|$)",
    ]
    for pat in patterns:
        m = re.search(pat, q, flags=re.IGNORECASE)
        if not m:
            continue
        company = _blank(m.group(1))
        client = _blank(m.group(2)) if m.lastindex and m.lastindex >= 2 else ""
        company = re.sub(
            r"\b(please|today|now)\b",
            "",
            company,
            flags=re.IGNORECASE,
        ).strip(" .?,")
        # Strip trailing "for <client>" if a no-client pattern captured it
        if not client:
            m2 = re.match(r"^(.+?)\s+for\s+(.+)$", company, flags=re.IGNORECASE)
            if m2:
                company, client = _blank(m2.group(1)), _blank(m2.group(2))
        client = client.strip(" .?,")
        if company:
            return {"company": company, "target_client": client or None}
    return {"company": None, "target_client": None}


def _extract_quoted_or_about(question: str) -> str | None:
    q = _blank(question)
    m = re.search(r'["“](.+?)["”]', q)
    if m:
        return _blank(m.group(1))
    m = re.search(
        r"(?:about|regarding|for|on)\s+(.+?)(?:\?|$)",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        frag = _blank(m.group(1))
        frag = re.sub(
            r"\b(please|today|now|in northstar)\b",
            "",
            frag,
            flags=re.IGNORECASE,
        ).strip(" .?")
        return frag or None
    return None


def _extract_company_for_opportunity(question: str) -> tuple[str | None, str | None]:
    """Return (company_fragment, target_client_fragment)."""
    q = _blank(question)
    m = re.search(
        r"why\s+is\s+(.+?)\s+(?:an?\s+)?opportunity\s+for\s+(.+?)(?:\?|$)",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        return _blank(m.group(1)), _blank(m.group(2))
    m = re.search(
        r"opportunity\s+for\s+(.+?)\s*[:\-]?\s*(.+?)(?:\?|$)",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        return _blank(m.group(2)), _blank(m.group(1))
    return None, None


def _extract_status_query(question: str) -> tuple[str | None, str | None]:
    """Return (client_fragment, status)."""
    q = _blank(question)
    m = re.search(
        r"which\s+(.+?)\s+prospects\s+(?:are|have status|with status)\s+(.+?)(?:\?|$)",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        return _blank(m.group(1)), _blank(m.group(2)).rstrip(".")
    m = re.search(
        r"(.+?)\s+prospects\s+(?:that are|are)\s+(.+?)(?:\?|$)",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        return _blank(m.group(1)), _blank(m.group(2)).rstrip(".")
    return None, None


def _extract_note_topic(question: str) -> str | None:
    q = _blank(question)
    patterns = [
        r"notes?\s+mentioning\s+(.+?)(?:\?|$)",
        r"mentioned\s+(?:a\s+)?(.+?)(?:\?|$)",
        r"talked\s+to\s+about\s+(.+?)(?:\?|$)",
        r"about\s+(.+?)(?:\?|$)",
        r"find\s+(?:companies\s+where\s+someone\s+mentioned\s+)?(.+?)(?:\?|$)",
    ]
    for pat in patterns:
        m = re.search(pat, q, flags=re.IGNORECASE)
        if m:
            topic = _blank(m.group(1)).strip(" .\"'")
            topic = re.sub(
                r"^(notes?\s+mentioning|companies\s+where\s+)",
                "",
                topic,
                flags=re.IGNORECASE,
            ).strip()
            if topic.lower() in {"stamping", "laser cutting", "a new project", "new project"}:
                return topic
            if len(topic) >= 2:
                return topic
    return None


def _classify_intent(question: str) -> tuple[str, dict[str, Any]]:
    q = _blank(question)
    lower = q.lower()

    call_prep = _extract_call_prep(q)
    if call_prep is not None:
        return "call_prep", call_prep

    # Approved Client Contacts (people who work for NorthStar's client — not CRM prospects)
    if (
        re.search(r"who\s+is\s+(?:our|the)\s+contact\s+(?:at|for|with)\b", lower)
        or re.search(r"client\s+contacts?\s+(?:for|at)\b", lower)
        or re.search(
            r"(?:what|who)\s+is\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+['’]?s?\s+(?:email|phone|title|role)\b",
            q,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:email|phone|title|role)\s+(?:for|of)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b",
            q,
            flags=re.IGNORECASE,
        )
        or re.search(r"who\s+(?:works|is)\s+(?:at|with)\s+(carmeco|brown)", lower)
        or (
            "contact" in lower
            and any(x in lower for x in ("carmeco", "brown"))
            and any(
                x in lower
                for x in ("email", "phone", "our contact", "client contact", "who is")
            )
        )
    ):
        client_frag = None
        m = re.search(
            r"(?:for|at|with)\s+(carmeco|brown(?:\s+industries)?)\b",
            lower,
        )
        if m:
            client_frag = m.group(1)
        person = None
        m_p = re.search(
            r"(?:what|who)\s+is\s+([A-Za-z]+(?:\s+[A-Za-z]+)+)['’]?s?",
            q,
            flags=re.IGNORECASE,
        )
        if m_p:
            person = _blank(m_p.group(1))
        if not person:
            m_p2 = re.search(
                r"(?:email|phone|title|role)\s+(?:for|of)\s+([A-Za-z]+(?:\s+[A-Za-z]+)+)",
                q,
                flags=re.IGNORECASE,
            )
            if m_p2:
                person = _blank(m_p2.group(1))
        return "client_contacts", {"client": client_frag, "person": person}

    # Approved Client Operations knowledge (email identities, appointment handling, etc.)
    if (
        re.search(r"who\s+takes\s+appointments?", lower)
        or re.search(r"copy\s+on\s+(?:a\s+)?(?:\w+\s+)?appointment\s+recap", lower)
        or re.search(r"cc\s+on\s+(?:a\s+)?(?:\w+\s+)?(?:appointment\s+)?recap", lower)
        or re.search(r"northstar\s+email\s+(?:do\s+we\s+use|for|account)", lower)
        or re.search(r"what\s+northstar\s+email", lower)
        or re.search(r"what\s+email\s+do\s+we\s+use", lower)
        or re.search(r"sender\s+account", lower)
        or re.search(r"who\s+is\s+assigned\s+(?:to|for)", lower)
        or re.search(r"what\s+sender\s+(?:account|email)", lower)
        or re.search(r"appointment\s+handling\s+(?:for|instructions)", lower)
        or re.search(r"lead\s+handoff", lower)
        or (
            "client operations" in lower
            and any(x in lower for x in ("carmeco", "brown", "appointment", "email", "recap"))
        )
    ):
        client_frag = None
        m = re.search(
            r"(?:for|at|with|to)\s+(carmeco|brown(?:\s+industries)?)\b",
            lower,
        )
        if m:
            client_frag = m.group(1)
        topic = "general"
        if "assigned" in lower or "revenue specialist" in lower:
            topic = "assignment"
        elif "email" in lower or "sender" in lower:
            topic = "email"
        elif "recap" in lower or "copy" in lower or "cc " in lower:
            topic = "recap_cc"
        elif "takes appointment" in lower or "who takes" in lower:
            topic = "who_takes_appointments"
        elif "handling" in lower or "appointment" in lower:
            topic = "appointment_handling"
        return "client_operations", {"client": client_frag, "topic": topic}

    # Approved email templates (not Pending extraction pieces)
    if (
        re.search(r"(?:send[\s-]?information|follow[\s-]?up)\s+(?:email\s+)?template", lower)
        or re.search(r"appointment\s+confirmation\s+template", lower)
        or re.search(r"email\s+template(?:s)?\s+(?:for|do we use)", lower)
        or re.search(r"what\s+(?:send[\s-]?information|appointment)\s+email", lower)
        or re.search(r"show\s+me\s+.+\s+(?:email\s+)?template", lower)
    ):
        client_frag = None
        m = re.search(
            r"(?:for|at|with)\s+(carmeco|brown(?:\s+industries)?)\b",
            lower,
        )
        if m:
            client_frag = m.group(1)
        topic = "general"
        if "appointment" in lower:
            topic = "appointment"
        elif "send" in lower or "follow" in lower:
            topic = "send_information"
        return "email_templates", {"client": client_frag, "topic": topic}

    # Client Strategy / Playbook knowledge (approved stored sections only)
    if (
        re.search(r"prospecting\s+guidance", lower)
        or re.search(r"how\s+(?:should|do)\s+(?:we|i)\s+prospect", lower)
        or re.search(r"sales\s+challenges?(?:\s*/\s*|\s+)?barriers?", lower)
        or re.search(r"business[\s-]?development\s+(?:challenges?|barriers?|obstacles?)", lower)
        or re.search(r"company\s+story(?:\s*/\s*|\s+)?background", lower)
        or re.search(r"(?:client|company)\s+(?:history|background|story|credibility)", lower)
        or (
            any(x in lower for x in ("carmeco", "brown", "strategy", "playbook"))
            and any(
                x in lower
                for x in (
                    "prospect",
                    "gone dark",
                    "past customer",
                    "barrier",
                    "in-house",
                    "outsourc",
                    "company story",
                    "founded",
                    "since 19",
                    "since 20",
                )
            )
        )
    ):
        client_frag = None
        m = re.search(
            r"(?:for|at|with)\s+(carmeco|brown(?:\s+industries)?)\b",
            lower,
        )
        if m:
            client_frag = m.group(1)
        topic = "general"
        if "prospect" in lower or "gone dark" in lower or "past customer" in lower:
            topic = "prospecting_guidance"
        elif "barrier" in lower or "challenge" in lower or "in-house" in lower or "outsourc" in lower:
            topic = "sales_challenges_barriers"
        elif any(
            x in lower
            for x in ("story", "background", "history", "credibility", "founded", "since ")
        ):
            topic = "company_story_background"
        return "client_strategy_knowledge", {"client": client_frag, "topic": topic}

    if re.search(r"what\s+should\s+i\s+work\s+next", lower):
        return "work_next", {}

    if "opportunity for" in lower or re.search(r"why\s+is\s+.+\s+opportunity", lower):
        company, target = _extract_company_for_opportunity(q)
        return "cross_client_why", {"company": company, "target_client": target}

    if "need a next action" in lower or "needs a next action" in lower or "needs next action" in lower:
        client_frag = None
        m = re.search(
            r"which\s+(.+?)\s+prospects\s+need",
            q,
            flags=re.IGNORECASE,
        )
        if m:
            client_frag = _blank(m.group(1))
        return "needs_next_action", {"client": client_frag}

    if "another northstar client has worked" in lower or (
        "another" in lower and "worked" in lower and "prospect" in lower
    ):
        client_frag = None
        m = re.search(r"show\s+me\s+(.+?)\s+prospects", q, flags=re.IGNORECASE)
        if m:
            client_frag = _blank(m.group(1))
        return "cross_client_list", {"client": client_frag}

    if "appointment" in lower and "no quote" in lower:
        return "appointments_without_quote", {}

    if (
        "appointment history but no quote" in lower
        or ("appointment history" in lower and "no quote" in lower)
        or ("companies have appointment" in lower and "no quote" in lower)
    ):
        return "appointments_without_quote", {}

    # Contact history (person First Last) — before company "what happened"
    m_contact = re.search(
        r"(?:[Ww]hat\s+happened\s+with|[Ss]how\s+me|[Aa]ppointment\s+history\s+for)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
        q,
    )
    if m_contact and (
        "appointment" in lower
        or "happened with" in lower
        or "history" in lower
    ):
        name = _blank(m_contact.group(1))
        # Avoid treating client names / generic phrases as contacts
        blocked = {
            "carmeco",
            "brown industries",
            "brown",
            "carmeco appointments",
            "northstar client",
        }
        second = name.split()[-1].lower() if name.split() else ""
        if (
            name.lower() not in blocked
            and second not in {"appointments", "appointment", "companies", "prospects", "rfqs"}
        ):
            return "contact_history", {"contact": name}

    # Client-wide appointment list (before loose contact matching)
    if re.search(r"show\s+me\s+\w+\s+appointments?\b", lower) or re.search(
        r"\b(carmeco|brown)\s+appointments?\b", lower
    ):
        return "sales_events", {
            "event_family": "appointment",
            "appointment_only": True,
        }

    # Company history phrasing
    if (
        "what happened with" in lower
        or "what happened to" in lower
        or "appointment history for" in lower
        or "show me the appointment history" in lower
    ):
        company = _extract_quoted_or_about(q)
        if not company:
            m = re.search(
                r"(?i)(?:with|to|for|about)\s+([A-Za-z0-9 &.'-]{2,60})",
                q,
            )
            if m:
                company = _blank(m.group(1)).rstrip("?.!")
        if company and company.lower() not in {"carmeco", "brown"}:
            return "company_intelligence", {"company": company}

    # RFQ / Send Information / appointment client-wide lists
    if re.search(r"\brfqs?\b", lower) and (
        "compan" in lower or "show" in lower or "which" in lower or "with" in lower
    ):
        return "sales_events", {
            "event_family": "rfq",
            "appointment_only": False,
            "company": None,
            "rev_spec": None,
        }

    if "send information" in lower and (
        "compan" in lower or "prospect" in lower or "which" in lower or "show" in lower
    ):
        return "sales_events", {
            "event_family": "engagement",
            "appointment_only": False,
        }

    if (
        "appointment history but no rfq" in lower
        or ("appointment" in lower and "no rfq" in lower)
        or ("companies have appointment" in lower and "no rfq" in lower)
    ):
        return "sales_events", {
            "event_family": "appointment",
            "appointment_only": True,
            "missing_event_types": ["RFQ"],
        }

    # Imported appointment / engagement sales events
    if (
        ("appointment" in lower or "engagement" in lower or "send information" in lower)
        and any(
            p in lower
            for p in (
                "show me",
                "show appointments",
                "which",
                "still in progress",
                "did tyler",
                "tyler set",
                "appointments tyler",
                "appointments set",
            )
        )
    ) or re.search(r"what\s+appointments?\s+did\s+\w+\s+set", lower):
        rev = None
        m = re.search(r"appointments?\s+(\w+)\s+set", lower)
        if m:
            rev = m.group(1)
        elif re.search(r"did\s+(\w+)\s+set", lower):
            rev = re.search(r"did\s+(\w+)\s+set", lower).group(1)
        elif "tyler" in lower:
            rev = "tyler"
        outcome = None
        if "in progress" in lower:
            outcome = "In Progress"
        company = None
        m2 = re.search(r"(?:with|for|about)\s+([A-Za-z0-9 &.'-]{2,60})", q, flags=re.I)
        if m2 and m2.group(1).lower() not in {
            "carmeco",
            "brown",
            "appointments",
            "appointment",
        }:
            company = _blank(m2.group(1))
        return "sales_events", {
            "rev_spec": rev,
            "outcome": outcome,
            "company": company,
            "appointment_only": "engagement" not in lower
            and "send information" not in lower
            and "rfq" not in lower,
            "event_family": "appointment"
            if (
                "appointment" in lower
                and "send information" not in lower
                and "rfq" not in lower
            )
            else None,
        }

    if (
        "appointment notes mentioning" in lower
        or ("appointment" in lower and "notes mentioning" in lower)
        or ("rfq notes mentioning" in lower)
        or ("find" in lower and "notes mentioning" in lower)
        or ("find appointment notes" in lower)
        or ("find rfq notes" in lower)
    ):
        topic = _extract_note_topic(q) or _extract_quoted_or_about(q)
        if not topic:
            m = re.search(r"mentioning\s+([A-Za-z0-9 &./-]{2,40})", q, flags=re.I)
            if m:
                topic = _blank(m.group(1)).rstrip("?.!")
        return "note_search", {"topic": topic}

    if any(
        p in lower
        for p in (
            "received a purchase order",
            "have received a purchase order",
            "purchase order",
            "purchase orders",
        )
    ) and ("compan" in lower or "show" in lower or "which" in lower):
        return "milestone_list", {"milestone": "Purchase Order"}

    if any(
        p in lower
        for p in ("received a quote", "have received a quote", "quotes", "a quote")
    ) and ("compan" in lower or "show" in lower or "which" in lower):
        return "milestone_list", {"milestone": "Quote"}

    if "weblead" in lower or "web lead" in lower:
        if "compan" in lower or "which" in lower or "show" in lower:
            return "milestone_list", {"milestone": "WebLead"}

    if re.search(r"\bhot\b", lower) and ("compan" in lower or "which" in lower):
        return "milestone_list", {"milestone": "Hot"}

    if any(
        p in lower
        for p in (
            "notes mentioning",
            "find notes",
            "talked to about",
            "someone mentioned",
            "mentioned",
        )
    ):
        return "note_search", {"topic": _extract_note_topic(q)}

    client_frag, status = _extract_status_query(q)
    if client_frag and status:
        return "status_list", {"client": client_frag, "status": status}

    if "what do we know" in lower or "tell me about" in lower or lower.startswith("who is "):
        company = _extract_quoted_or_about(q)
        if not company and "about " in lower:
            company = _extract_quoted_or_about(q)
        return "company_intelligence", {"company": company}

    # Bare company question: "Whirlpool?"
    if len(q.split()) <= 4 and "?" in q:
        frag = q.rstrip("?").strip()
        if frag and frag.lower() not in {"what", "who", "why", "how"}:
            return "company_intelligence", {"company": frag}

    return "general_search", {"q": q}


def _company_card_from_row(
    *,
    company_id: int,
    company_name: str,
    external_record_no: str,
    client_id: int | None,
    client_name: str,
    status: str = "",
    city: str = "",
    state: str = "",
    badges: list[str] | None = None,
    why: str = "",
    score: int | None = None,
    rank: int | None = None,
    next_action: str = "",
    work_priority: int | None = None,
    work_type: str = "",
    northstar_recommendation: str = "",
    northstar_fit: str = "",
    northstar_engagement: str = "",
    northstar_alignment: str = "",
    northstar_recommendation_why: str = "",
) -> AskCompanyCard:
    return AskCompanyCard(
        company_id=company_id,
        company_name=company_name,
        external_record_no=external_record_no,
        client_id=client_id,
        client_name=client_name,
        status=status,
        city=city,
        state=state,
        badges=badges or [],
        why=why,
        opportunity_score=score,
        workspace_path=(
            f"/companies/{external_record_no}"
            + (f"?client_id={client_id}" if client_id else "")
            if external_record_no
            else ""
        ),
        rank=rank,
        next_action=next_action,
        work_priority=work_priority,
        work_type=work_type,
        northstar_recommendation=northstar_recommendation,
        northstar_fit=northstar_fit,
        northstar_engagement=northstar_engagement,
        northstar_alignment=northstar_alignment,
        northstar_recommendation_why=northstar_recommendation_why,
    )


def _contact_rank(title: str, has_phone: bool, has_email: bool) -> tuple[int, int, int]:
    title_l = title.lower()
    if any(h in title_l for h in ("buyer", "purchas", "procure", "sourc")):
        decision = 0
    elif any(h in title_l for h in _DECISION_MAKER_HINTS):
        decision = 1
    else:
        decision = 2
    phone_rank = 0 if has_phone else 1
    email_rank = 0 if has_email else 1
    return (decision, phone_rank, email_rank)


_NOT_VERIFIED_TEXT = "Not verified in current public research."

_USEFUL_RESEARCH_TYPES = {
    "product",
    "capability",
    "material",
    "industry",
    "location",
    "headquarters",
    "overview",
    "company_name",
    "website",
    "phone",
}


def _load_stored_research_for_call_brief(
    conn,
    *,
    company_id: int,
    working_client_id: int,
) -> dict[str, Any]:
    """Read stored research only — never run web research from Ask NorthStar.

    Shared company findings come from the latest completed research run.
    Fit is loaded only for the Working For client (never another client's fit).
    """
    empty: dict[str, Any] = {
        "has_research": False,
        "run_id": None,
        "last_researched": "",
        "summary": "",
        "findings": [],
        "intelligence": [],
        "fit": None,
    }
    if not _table_exists(conn, "company_research_runs"):
        return empty

    # Prefer latest completed run for this Working For client; else any completed run
    # (company facts are shared; fit is loaded separately by client_id).
    run = conn.execute(
        """
        SELECT id, working_for_client_id, summary, completed_at, started_at, status
        FROM company_research_runs
        WHERE company_id = ?
          AND lower(COALESCE(status, '')) = 'completed'
          AND working_for_client_id = ?
        ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
        LIMIT 1
        """,
        (company_id, working_client_id),
    ).fetchone()
    if not run:
        run = conn.execute(
            """
            SELECT id, working_for_client_id, summary, completed_at, started_at, status
            FROM company_research_runs
            WHERE company_id = ?
              AND lower(COALESCE(status, '')) = 'completed'
            ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
            LIMIT 1
            """,
            (company_id,),
        ).fetchone()

    findings: list[dict[str, Any]] = []
    last_researched = ""
    summary = ""
    run_id = None
    if run:
        run_id = int(run["id"])
        last_researched = _blank(run["completed_at"] or run["started_at"])
        summary = _blank(run["summary"])
        if _table_exists(conn, "company_research_findings"):
            findings = [
                {
                    "finding_type": _blank(r["finding_type"]),
                    "field_key": _blank(r["field_key"]),
                    "value": _blank(r["value"]),
                    "source_name": _blank(r["source_name"]),
                    "source_url": _blank(r["source_url"]),
                    "researched_at": _blank(r["researched_at"]),
                    "confidence": _blank(r["confidence"]),
                    "evidence_level": _blank(r["evidence_level"]) or "verified",
                    "page_title": _blank(r["page_title"]) if "page_title" in r.keys() else "",
                    "provider_id": _blank(r["provider_id"]),
                }
                for r in conn.execute(
                    """
                    SELECT finding_type, field_key, value, source_name, source_url,
                           researched_at, confidence, evidence_level, page_title, provider_id
                    FROM company_research_findings
                    WHERE research_run_id = ?
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
            ]

    intelligence: list[dict[str, Any]] = []
    if _table_exists(conn, "company_intelligence"):
        intelligence = [
            {
                "field_key": _blank(r["field_key"]),
                "value": _blank(r["value"]),
                "finding_type": _blank(r["finding_type"]),
                "source_name": _blank(r["source_name"]),
                "source_url": _blank(r["source_url"]),
                "confidence": _blank(r["confidence"]),
                "last_verified_at": _blank(r["last_verified_at"]),
            }
            for r in conn.execute(
                """
                SELECT field_key, value, finding_type, source_name, source_url,
                       confidence, last_verified_at
                FROM company_intelligence
                WHERE company_id = ?
                ORDER BY id
                """,
                (company_id,),
            ).fetchall()
        ]

    fit: dict[str, Any] | None = None
    campaign_id: int | None = None
    campaign_name: str = ""
    if _table_exists(conn, "company_client_fit"):
        fit_row = conn.execute(
            """
            SELECT fit_result, why, supporting_evidence, potential_opportunity,
                   concerns, missing_information, research_run_id, updated_at
            FROM company_client_fit
            WHERE company_id = ? AND client_id = ?
            """,
            (company_id, working_client_id),
        ).fetchone()
        if fit_row:
            try:
                concerns = json.loads(fit_row["concerns"] or "[]")
            except json.JSONDecodeError:
                concerns = []
            try:
                missing_information = json.loads(fit_row["missing_information"] or "[]")
            except json.JSONDecodeError:
                missing_information = []
            support_raw: Any = []
            chains: list[dict] = []
            profile_incomplete = False
            try:
                parsed = json.loads(fit_row["supporting_evidence"] or "[]")
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, dict):
                support_raw = parsed.get("supporting_evidence") or []
                chains = parsed.get("evidence_chains") or []
                profile_incomplete = bool(parsed.get("profile_incomplete"))
            elif isinstance(parsed, list):
                support_raw = parsed
            fit = {
                "fit_result": _blank(fit_row["fit_result"]),
                "why": _blank(fit_row["why"]),
                "supporting_evidence": list(support_raw) if isinstance(support_raw, list) else [],
                "evidence_chains": chains,
                "potential_opportunity": _blank(fit_row["potential_opportunity"]),
                "concerns": concerns if isinstance(concerns, list) else [],
                "missing_information": (
                    missing_information if isinstance(missing_information, list) else []
                ),
                "profile_incomplete": profile_incomplete,
                "updated_at": _blank(fit_row["updated_at"]),
                "research_run_id": fit_row["research_run_id"],
            }
            # Campaign context from the research run that produced this fit
            rid = fit_row["research_run_id"]
            if rid is not None and _table_exists(conn, "company_research_runs"):
                run_cols = {
                    str(c["name"])
                    for c in conn.execute(
                        "PRAGMA table_info(company_research_runs)"
                    ).fetchall()
                }
                if "campaign_id" in run_cols:
                    crow = conn.execute(
                        """
                        SELECT r.campaign_id, cc.campaign_name
                        FROM company_research_runs r
                        LEFT JOIN client_campaigns cc ON cc.id = r.campaign_id
                        WHERE r.id = ?
                        """,
                        (int(rid),),
                    ).fetchone()
                    if crow and crow["campaign_id"] is not None:
                        campaign_id = int(crow["campaign_id"])
                        campaign_name = _blank(crow["campaign_name"])

    has_research = bool(run_id) or bool(intelligence)
    return {
        "has_research": has_research,
        "run_id": run_id,
        "last_researched": last_researched,
        "summary": summary,
        "findings": findings,
        "intelligence": intelligence,
        "fit": fit,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
    }


def _fit_confidence_label(fit_result: str, *, profile_incomplete: bool = False) -> str:
    key = _blank(fit_result).lower()
    if profile_incomplete and "strong" not in key:
        return "Low–Medium"
    if "strong" in key:
        return "High"
    if "possible" in key:
        return "Medium"
    if "weak" in key:
        return "Low"
    return "Low"


def _build_campaign_fit_brief(
    *,
    company_name: str,
    working_name: str,
    working_id: int,
    campaign_name: str,
    stored_fit: dict[str, Any] | None,
    research_items: list[dict[str, Any]],
    working_milestones: list[Any],
    other_milestones: list[Any],
    primary_service: str = "",
) -> dict[str, Any]:
    """Structured CAMPAIGN FIT block for Call Brief — stored evidence only; never invent."""
    fit_result = _blank((stored_fit or {}).get("fit_result"))
    if not fit_result:
        return {
            "has_fit": False,
            "campaign": campaign_name or "Default",
            "fit_rating": "",
            "confidence": "",
            "why": (
                f"No stored fit evaluation for {working_name} yet. "
                "Run Research This Company while Working For this client to generate campaign fit."
            ),
            "evidence_supporting": [],
            "still_unverified": [],
            "commercial_experience": [],
            "items": [],
        }

    confidence = _fit_confidence_label(
        fit_result,
        profile_incomplete=bool((stored_fit or {}).get("profile_incomplete")),
    )
    camp = campaign_name or "Default"
    primary = _blank(primary_service) or camp

    # Company identity from stored research (products / capabilities) — not invented
    products = [
        _blank(i.get("value"))
        for i in research_items
        if _blank(i.get("finding_type")).lower() == "product"
        and _blank(i.get("value"))
        and (_blank(i.get("evidence_level")) or "verified") != "not_verified"
    ][:5]
    caps = [
        _blank(i.get("value"))
        for i in research_items
        if _blank(i.get("finding_type")).lower() == "capability"
        and _blank(i.get("value"))
        and (_blank(i.get("evidence_level")) or "verified") != "not_verified"
    ][:4]
    industries = [
        _blank(i.get("value"))
        for i in research_items
        if _blank(i.get("finding_type")).lower() == "industry"
        and _blank(i.get("value"))
        and (_blank(i.get("evidence_level")) or "verified") != "not_verified"
    ][:3]

    # Commercial history — confidence only; never equals manufacturing fit
    commercial_lines: list[str] = []
    working_sigs: list[str] = []
    if working_milestones:
        working_sigs = sorted(
            {
                _blank(getattr(m, "label", None) or getattr(m, "milestone_type", None))
                for m in working_milestones
            }
            - {""}
        )
        if working_sigs:
            commercial_lines.append(f"{working_name}: " + ", ".join(working_sigs))
    other_by_client: dict[str, set[str]] = {}
    for m in other_milestones:
        cname = _blank(getattr(m, "client_name", None))
        sig = _blank(getattr(m, "label", None) or getattr(m, "milestone_type", None))
        if not cname or not sig or cname == working_name:
            continue
        other_by_client.setdefault(cname, set()).add(sig)
    for cname, sigs in sorted(other_by_client.items()):
        commercial_lines.append(f"{cname}: " + ", ".join(sorted(sigs)))

    # Plain-English WHY for the salesperson (generic; driven by stored research + fit)
    why_bits: list[str] = []
    if products:
        why_bits.append(
            f"{company_name} is a manufacturer whose public product line includes "
            f"{', '.join(products)}."
        )
    elif caps:
        why_bits.append(
            f"{company_name} has verified manufacturing language including {', '.join(caps)}."
        )
    elif industries:
        why_bits.append(
            f"{company_name} appears in markets/industries including {', '.join(industries)}."
        )

    if working_sigs:
        why_bits.append(
            f"{working_name} has prior commercial history with {company_name} "
            f"({', '.join(working_sigs)}). That is meaningful positive evidence and raises "
            "confidence to pursue the account."
        )
        why_bits.append(
            "Commercial history can increase confidence but must not automatically "
            f"establish manufacturing fit for the {camp} campaign."
        )

    primary_lower = primary.lower()
    stampish = "stamp" in primary_lower or "stamp" in camp.lower()
    if stampish:
        why_bits.append(
            f"NorthStar has NOT yet verified that {company_name} outsources recurring "
            f"stamped components matching {working_name}'s primary focus ({primary})."
        )
        if working_sigs:
            why_bits.append(
                f"NorthStar has NOT verified that {working_name}'s previous work for "
                f"{company_name} was stamping — stored milestones do not identify work type."
            )
    else:
        # Generic primary-need gap from stored missing_information / stored why
        stored_why = _blank((stored_fit or {}).get("why"))
        if stored_why and "unverified" in stored_why.lower():
            why_bits.append(stored_why)
        else:
            gaps0 = [
                _blank(g)
                for g in ((stored_fit or {}).get("missing_information") or [])
                if _blank(g)
            ]
            if gaps0:
                why_bits.append(
                    f"Key campaign need still unverified: {gaps0[0]}"
                )
            elif stored_why:
                why_bits.append(stored_why)

    why_bits.append(
        f"Therefore the current rating remains {fit_result} unless stronger evidence "
        f"of {primary} demand (or related outsourced recurring need) is found."
    )
    why_text = " ".join(why_bits)

    # Evidence supporting fit — strongest verified / supported lines only
    evidence: list[str] = []
    if products:
        evidence.append(f"Verified product categories: {', '.join(products)}.")
    if working_sigs:
        evidence.append(
            f"Previous {working_name} commercial history: {', '.join(working_sigs)} "
            "(confidence signal only — not manufacturing fit by itself)."
        )
    # Prefer short supporting_evidence over long evidence_chain dumps
    for e in (stored_fit or {}).get("supporting_evidence") or []:
        et = _blank(e)
        if not et:
            continue
        # Skip near-duplicates of product/history lines already added
        el = et.lower()
        if any(p.lower() in el for p in products[:2]) and "product" in el:
            continue
        if working_sigs and any(s.lower() in el for s in working_sigs) and (
            "purchase" in el or "quote" in el or "weblead" in el or "history" in el
        ):
            continue
        if et not in evidence:
            evidence.append(et)
        if len(evidence) >= 6:
            break
    if len(evidence) < 4:
        for ch in (stored_fit or {}).get("evidence_chains") or []:
            if not isinstance(ch, dict):
                continue
            fact = _blank(ch.get("fact"))
            if not fact:
                continue
            if fact not in evidence and not any(fact.lower() in e.lower() for e in evidence):
                evidence.append(fact)
            if len(evidence) >= 6:
                break
    if industries and not any("industr" in e.lower() for e in evidence):
        evidence.append(f"Verified industries/markets: {', '.join(industries)}.")

    # Still unverified — important gaps only (dedupe / prioritize)
    unverified: list[str] = []
    priority_keywords = (
        "stamp",
        "outsourc",
        "recurring",
        "tooling",
        "type of prior",
        "volume",
        "program",
        "overflow",
        "capacity",
        "primary",
    )
    if stampish:
        unverified.append(
            f"Whether {company_name} outsources recurring stamped components "
            f"for {working_name}'s {camp} campaign is unverified."
        )
        if working_sigs:
            unverified.append(
                f"Whether prior {working_name} work for {company_name} was stamping "
                "(vs other work) is unverified from stored commercial history."
            )
    gaps = [_blank(g) for g in ((stored_fit or {}).get("missing_information") or []) if _blank(g)]
    gaps_sorted = sorted(
        gaps,
        key=lambda g: (
            0 if any(k in g.lower() for k in priority_keywords) else 1,
            len(g),
        ),
    )
    for g in gaps_sorted:
        gl = g.lower()
        # Skip verbose near-duplicates of the explicit lines above
        if stampish and working_sigs and (
            "type of prior" in gl or ("stored history" in gl and "stamp" in gl)
        ):
            continue
        if stampish and "outsourced metal stamping need" in gl:
            continue
        if g not in unverified and not any(
            g[:48].lower() in u.lower() or u[:48].lower() in gl for u in unverified
        ):
            unverified.append(g)
        if len(unverified) >= 6:
            break

    items: list[dict[str, Any]] = [
        {
            "section": "header",
            "campaign": camp,
            "fit_rating": fit_result,
            "confidence": confidence,
            "working_for": working_name,
            "working_for_client_id": working_id,
        },
        {
            "section": "why",
            "label": "WHY",
            "text": why_text,
        },
        {
            "section": "evidence",
            "label": "EVIDENCE SUPPORTING FIT",
            "bullets": evidence[:6],
        },
        {
            "section": "unverified",
            "label": "WHAT IS STILL UNVERIFIED",
            "bullets": unverified[:6],
        },
        {
            "section": "commercial",
            "label": "NORTHSTAR COMMERCIAL EXPERIENCE",
            "bullets": commercial_lines
            or [
                f"No commercial milestone signals on file for {working_name}."
            ],
            "note": (
                "Commercial history can increase confidence but must not automatically "
                "establish manufacturing fit for the campaign."
            ),
        },
    ]

    return {
        "has_fit": True,
        "campaign": camp,
        "fit_rating": fit_result,
        "confidence": confidence,
        "why": why_text,
        "evidence_supporting": evidence[:6],
        "still_unverified": unverified[:6],
        "commercial_experience": commercial_lines,
        "items": items,
    }


def _format_research_finding_items(stored: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact provenance items for WHAT NORTHSTAR KNOWS ABOUT THE COMPANY."""
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for f in stored.get("findings") or []:
        ftype = _blank(f.get("finding_type")).lower()
        value = _blank(f.get("value"))
        if not value or value.lower() == _NOT_VERIFIED_TEXT.lower():
            continue
        if ftype not in _USEFUL_RESEARCH_TYPES:
            continue
        key = (ftype, value.lower())
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "label": ftype.replace("_", " ").title(),
                "finding_type": ftype,
                "value": value,
                "evidence_level": _blank(f.get("evidence_level")) or "verified",
                "confidence": _blank(f.get("confidence")),
                "source_name": _blank(f.get("source_name")),
                "source_url": _blank(f.get("source_url")),
                "page_title": _blank(f.get("page_title")),
                "researched_at": _blank(f.get("researched_at")),
                "provenance": "Stored company research finding",
            }
        )
    # Fill gaps from shared company_intelligence if findings sparse
    if len(items) < 3:
        for intel in stored.get("intelligence") or []:
            value = _blank(intel.get("value"))
            ftype = _blank(intel.get("finding_type") or intel.get("field_key"))
            if not value or value.lower() == _NOT_VERIFIED_TEXT.lower():
                continue
            key = (ftype.lower(), value.lower())
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "label": ftype.replace("_", " ").title() or "Intelligence",
                    "finding_type": ftype,
                    "value": value,
                    "evidence_level": "verified",
                    "confidence": _blank(intel.get("confidence")),
                    "source_name": _blank(intel.get("source_name")),
                    "source_url": _blank(intel.get("source_url")),
                    "page_title": "",
                    "researched_at": _blank(intel.get("last_verified_at")),
                    "provenance": "Stored company intelligence",
                }
            )
    return items[:24]


def _answer_call_prep(
    conn,
    *,
    user_id: int,
    company_name_query: str | None,
    target_client_query: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
    question: str = "",
) -> AskNorthStarResponse:
    if not company_name_query:
        return AskNorthStarResponse(
            question="",
            intent="call_prep",
            summary=(
                "I can prepare a call brief. Tell me the company and Working For client — "
                'for example: “What should I know before I call Trailerman for Brown?”'
            ),
            scope="",
            active_client_id=preferred_client_id,
            active_client_name="",
            no_data=False,
            sections=[
                AskSection(
                    id="clarify_call_prep",
                    title="CLARIFY CALL PREP",
                    body="Company and Working For client are required for a call brief.",
                    items=[],
                )
            ],
            companies=[],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[AskSource(label="NorthStar CRM", detail="Call prep clarification")],
            recommended_links=[],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
        )

    matches = _find_companies_by_name(conn, company_name_query, visible_ids=visible_ids)
    if not matches:
        return _empty_answer(
            intent="call_prep",
            summary=NO_DATA,
            detail=f'No authorized company matched “{company_name_query}”.',
        )

    # Ambiguous company resolution — ask instead of guessing
    best = min(_company_match_confidence(company_name_query, m["company_name"]) for m in matches)
    top = [
        m
        for m in matches
        if _company_match_confidence(company_name_query, m["company_name"]) == best
    ]
    if len(top) > 1 and best > 0:
        return AskNorthStarResponse(
            question="",
            intent="call_prep",
            summary=(
                f"Several companies match “{company_name_query}”. "
                "Which company should this call brief use?"
            ),
            scope="",
            active_client_id=preferred_client_id,
            active_client_name="",
            no_data=False,
            sections=[
                AskSection(
                    id="need_company",
                    title="SELECT COMPANY",
                    body="Multiple plausible NorthStar company matches — choose one.",
                    items=[
                        {
                            "company_id": int(m["company_id"]),
                            "company_name": _blank(m["company_name"]),
                            "external_record_no": _blank(m["external_record_no"]),
                        }
                        for m in top[:8]
                    ],
                )
            ],
            companies=[
                _company_card_from_row(
                    company_id=int(m["company_id"]),
                    company_name=_blank(m["company_name"]),
                    external_record_no=_blank(m["external_record_no"]),
                    client_id=preferred_client_id,
                    client_name="",
                    city=_blank(m.get("city")),
                    state=_blank(m.get("state")),
                )
                for m in top[:8]
            ],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[AskSource(label="NorthStar CRM", detail="Ambiguous company match")],
            recommended_links=[],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
        )
    matches = top or matches

    company = matches[0]
    company_id = int(company["company_id"])
    company_name = _blank(company["company_name"])
    master_rn = _blank(company["external_record_no"])

    # All authorized relationships for this company
    rel_rows = conn.execute(
        f"""
        SELECT
            cl.id AS client_id,
            cl.name AS client_name,
            TRIM(COALESCE(ccr.external_record_no, '')) AS external_record_no,
            ccr.status,
            ccr.is_hot,
            ccr.follow_up_date,
            ccr.next_action,
            ccr.notes AS ccr_notes
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        WHERE ccr.company_id = ?
          AND ccr.client_id IN ({_placeholders(visible_ids)})
          AND {_ask_sql_ccr(conn, "ccr")}
        ORDER BY cl.name COLLATE NOCASE
        """,
        [company_id, *visible_ids],
    ).fetchall()
    relationships = [dict(r) for r in rel_rows]

    # Resolve Working For client — fresh every question; never reuse prior answers.
    target = None
    if target_client_query:
        target = _find_client_by_text(conn, target_client_query, visible_ids)
        if target is None:
            return _empty_answer(
                intent="call_prep",
                summary=NO_DATA,
                detail=(
                    f'Could not resolve Working For client “{target_client_query}". '
                    "Name an authorized NorthStar client."
                ),
            )
    elif preferred_client_id and preferred_client_id in visible_ids:
        row = conn.execute(
            "SELECT id, name, code FROM clients WHERE id = ?",
            (preferred_client_id,),
        ).fetchone()
        if row:
            target = {
                "id": int(row["id"]),
                "name": _blank(row["name"]),
                "code": _blank(row["code"]),
            }
    elif len(relationships) == 1:
        r0 = relationships[0]
        target = {
            "id": int(r0["client_id"]),
            "name": _blank(r0["client_name"]),
            "code": "",
        }
    elif len(relationships) > 1:
        options = [
            {
                "client_id": int(r["client_id"]),
                "client_name": _blank(r["client_name"]),
                "external_record_no": _blank(r["external_record_no"]),
                "status": _blank(r["status"]),
            }
            for r in relationships
        ]
        return AskNorthStarResponse(
            question="",
            intent="call_prep",
            summary=(
                f"Which client are you preparing to call {company_name} for?"
            ),
            scope="",
            active_client_id=preferred_client_id,
            active_client_name="",
            no_data=False,
            sections=[
                AskSection(
                    id="need_working_for",
                    title="SELECT WORKING FOR CLIENT",
                    body=(
                        f"{company_name} is associated with more than one authorized "
                        "NorthStar client. Do not guess — choose Working For."
                    ),
                    items=options,
                )
            ],
            companies=[
                _company_card_from_row(
                    company_id=company_id,
                    company_name=company_name,
                    external_record_no=_blank(r["external_record_no"]) or master_rn,
                    client_id=int(r["client_id"]),
                    client_name=_blank(r["client_name"]),
                    status=_blank(r["status"]),
                )
                for r in relationships
            ],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[AskSource(label="NorthStar CRM", detail="Multiple client relationships")],
            recommended_links=[],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
        )
    else:
        return _empty_answer(
            intent="call_prep",
            summary=NO_DATA,
            detail=(
                f"Specify which client you are calling {company_name} for "
                "(example: … for Brown), or set Active Client."
            ),
        )

    working_id = int(target["id"])
    working_name = target["name"]
    working_rel = next(
        (r for r in relationships if int(r["client_id"]) == working_id),
        None,
    )
    has_working_ccr = working_rel is not None
    working_status = _blank(working_rel["status"]) if working_rel else "New"
    # Use THIS client's relationship RN only — never borrow another client's /
    # master RN for a soft-open Working For context.
    working_rn = (
        _blank(working_rel["external_record_no"]) if working_rel else ""
    )
    # Navigation / research still need a resolvable company key
    research_rn = working_rn or master_rn
    other_rels = [r for r in relationships if int(r["client_id"]) != working_id]

    # Cross-client opportunity (for Working For target)
    opp = None
    try:
        listed = list_cross_client_opportunities(
            user_id,
            target_client_id=working_id,
            include_dismissed=True,
        )
        opp = next((o for o in listed.opportunities if o.company_id == company_id), None)
    except Exception:
        opp = None

    # Contacts (master company) — label with relationship client when possible
    contact_rows = conn.execute(
        f"""
        SELECT
            ct.id,
            ct.first_name,
            ct.last_name,
            ct.title,
            ct.phone,
            ct.alt_phone,
            ct.email
        FROM contacts ct
        WHERE ct.company_id = ?
          AND {_ask_sql_contact(conn, "ct")}
        """,
        (company_id,),
    ).fetchall()
    contact_items = []
    for r in contact_rows:
        name = f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip()
        if not name:
            continue
        title = _blank(r["title"])
        phone = _blank(r["phone"])
        alt = _blank(r["alt_phone"])
        email = _blank(r["email"])
        # Contacts are master-company records; attribute to other-client history when
        # the Working For relationship is new/thin and another client already knows them.
        if other_rels:
            source_client = _blank(other_rels[0]["client_name"])
        elif has_working_ccr:
            source_client = working_name
        else:
            source_client = "NorthStar CRM"
        source_label = (
            f"{source_client} History"
            if source_client != "NorthStar CRM"
            else "NorthStar CRM"
        )
        contact_items.append(
            {
                "contact_id": int(r["id"]),
                "name": name,
                "title": title,
                "phone": phone,
                "alt_phone": alt,
                "email": email,
                "client_name": source_client,
                "source": source_label,
                "_rank": _contact_rank(title, bool(phone or alt), bool(email)),
            }
        )
    contact_items.sort(key=lambda c: (*c["_rank"], c["name"].lower()))
    for c in contact_items:
        c.pop("_rank", None)
    contacts = [
        AskContactCard(
            contact_id=c["contact_id"],
            name=c["name"],
            title=c["title"],
            phone=c["phone"] or c.get("alt_phone") or "",
            email=c["email"],
            client_id=None,
            client_name=c["client_name"],
            source=c["source"],
        )
        for c in contact_items[:12]
    ]

    # Activities for Working For + other clients (label clearly)
    activity_rows = conn.execute(
        f"""
        SELECT
            a.activity_id AS id,
            a.activity_type,
            a.activity_at,
            a.notes,
            a.outcome,
            a.follow_up_at,
            cl.name AS client_name,
            cl.id AS client_id
        FROM activities a
        JOIN clients cl ON cl.id = a.client_id
        WHERE a.company_id = ?
          AND a.client_id IN ({_placeholders(visible_ids)})
        ORDER BY COALESCE(a.activity_at, a.created_at) DESC
        LIMIT 12
        """,
        [company_id, *visible_ids],
    ).fetchall()
    history_items: list[dict[str, Any]] = []
    for r in activity_rows:
        notes = _blank(r["notes"])
        history_items.append(
            {
                "kind": "activity",
                "activity_type": _blank(r["activity_type"]),
                "activity_at": _blank(r["activity_at"]),
                "excerpt": (notes[:280] + ("…" if len(notes) > 280 else "")) if notes else _blank(r["outcome"]),
                "follow_up_at": _blank(r["follow_up_at"]),
                "client_name": _blank(r["client_name"]),
                "client_id": int(r["client_id"]),
                "source": "NorthStar Activity",
                "is_working_for": int(r["client_id"]) == working_id,
            }
        )

    if _table_exists(conn, "legacy_notes"):
        legacy_rows = conn.execute(
            f"""
            SELECT
                ln.id,
                ln.note_text,
                ln.created_at,
                cl.name AS client_name,
                cl.id AS client_id
            FROM legacy_notes ln
            JOIN clients cl ON cl.id = ln.client_id
            WHERE ln.company_id = ?
              AND ln.client_id IN ({_placeholders(visible_ids)})
            ORDER BY COALESCE(ln.created_at, '') DESC, ln.id DESC
            LIMIT 8
            """,
            [company_id, *visible_ids],
        ).fetchall()
        for r in legacy_rows:
            text = _blank(r["note_text"])
            history_items.append(
                {
                    "kind": "legacy_note",
                    "activity_type": "Legacy Note",
                    "activity_at": _blank(r["created_at"]),
                    "excerpt": text[:280] + ("…" if len(text) > 280 else ""),
                    "follow_up_at": "",
                    "client_name": _blank(r["client_name"]),
                    "client_id": int(r["client_id"]),
                    "source": "Legacy LeadMaster Notes",
                    "is_working_for": int(r["client_id"]) == working_id,
                }
            )

    # Prefer Working For history first, then others; newest first within each group.
    working_hist = [h for h in history_items if h.get("is_working_for")]
    other_hist = [h for h in history_items if not h.get("is_working_for")]
    working_hist.sort(key=lambda h: str(h.get("activity_at") or ""), reverse=True)
    other_hist.sort(key=lambda h: str(h.get("activity_at") or ""), reverse=True)
    history_items = (working_hist + other_hist)[:10]

    # Imported sales events for Working For client (call brief)
    from sales_events_intel import fetch_sales_events, summarize_events_for_brief

    se_raw = fetch_sales_events(
        visible_client_ids=visible_ids,
        preferred_client_id=working_id,
        company_id=company_id,
        limit=40,
    )
    sales_brief_items = summarize_events_for_brief(se_raw, limit=6)
    # Also allow authorized other-client events labeled clearly (not re-owned)
    other_se = fetch_sales_events(
        visible_client_ids=[i for i in visible_ids if i != working_id],
        company_id=company_id,
        limit=8,
    )
    for r in other_se[:3]:
        item = summarize_events_for_brief([r], limit=1)
        if item:
            row = item[0]
            row["cross_client_label"] = f"{_blank(r.get('client_name'))} History"
            row["attribution"] = (
                f"Attributed to {_blank(r.get('client_name'))} — not {_blank(working_name)} engagement."
            )
            # Keep separate from working brief dump
            pass

    # Milestones / commercial signals
    milestone_rows = conn.execute(
        f"""
        SELECT
            rm.milestone_type,
            rm.milestone_date,
            cl.name AS client_name,
            cl.id AS client_id
        FROM revenue_milestones rm
        JOIN clients cl ON cl.id = rm.client_id
        WHERE rm.company_id = ?
          AND rm.client_id IN ({_placeholders(visible_ids)})
          AND rm.milestone_type IN (
              'Purchase Order', 'Quote', 'Appointment Set', 'WebLead', 'Hot'
          )
        ORDER BY COALESCE(rm.milestone_date, '') DESC, rm.milestone_id DESC
        """,
        [company_id, *visible_ids],
    ).fetchall()
    milestones = [
        AskMilestoneBadge(
            milestone_type=_blank(r["milestone_type"]),
            label=_MILESTONE_LABELS.get(
                _blank(r["milestone_type"]).lower(), _blank(r["milestone_type"])
            ),
            milestone_date=_blank(r["milestone_date"]),
            client_id=int(r["client_id"]),
            client_name=_blank(r["client_name"]),
            source=f"{_blank(r['client_name'])} History",
        )
        for r in milestone_rows
    ]

    # Stored company research (read-only) + Working-For-specific fit
    stored_research = _load_stored_research_for_call_brief(
        conn, company_id=company_id, working_client_id=working_id
    )
    research_items = _format_research_finding_items(stored_research)
    stored_fit = stored_research.get("fit")
    has_stored_research = bool(stored_research.get("has_research"))

    # Split commercial milestones: Working For vs other authorized clients
    working_milestones = [m for m in milestones if m.client_id == working_id]
    other_milestones = [m for m in milestones if m.client_id != working_id]

    # Why this account matters + narrative summary
    why_parts: list[str] = []
    camp_label = ""
    why_parts.append(
        f"{company_name} is currently {working_status or 'New'} for {working_name}."
    )
    if not has_working_ccr:
        why_parts.append(
            f"No {working_name} relationship row is stored yet — this brief uses Working For "
            f"{working_name} as call context (soft-open)."
        )
    if working_milestones:
        why_parts.append(
            f"{working_name} commercial history includes "
            + ", ".join(sorted({m.label or m.milestone_type for m in working_milestones}))
            + "."
        )
    if other_rels:
        other_bits = ", ".join(
            f"{_blank(r['client_name'])} ({_blank(r['status']) or '—'})" for r in other_rels
        )
        why_parts.append(
            f"NorthStar also has experience with {company_name} through {other_bits}."
        )
    if opp:
        src = ", ".join(opp.source_client_names or []) or "another NorthStar client"
        sigs = ", ".join(opp.signal_types or []) or "commercial signals"
        why_parts.append(
            f"{company_name} should be reviewed closely for {working_name} because NorthStar "
            f"already has meaningful commercial history with the company through {src} "
            f"({sigs}; opportunity score {opp.opportunity_score}). "
            f"That history informs prioritization for {working_name}; it does not change "
            f"the {working_name} status."
        )
    if stored_fit and _blank(stored_fit.get("fit_result")):
        camp_label = _blank(stored_research.get("campaign_name"))
        try:
            from client_setup_data import (
                ensure_client_setup_schema,
                get_default_campaign,
                resolve_campaign_by_name,
            )

            ensure_client_setup_schema(conn)
            # Prefer named campaign in the question; else stored research campaign; else default
            qlow = _blank(question).lower()
            camp = None
            named = None
            import re as _re

            m = _re.search(
                r"(?:'s\s+|)\b([a-z0-9][a-z0-9 &\-/]{1,40}?)\s+campaign\b",
                qlow,
            )
            if m:
                named = m.group(1).strip()
                if named in {"the", "a", "an", "this", "default"}:
                    named = None
            if named:
                camp = resolve_campaign_by_name(conn, working_id, named)
            if camp is None and stored_research.get("campaign_id"):
                from client_setup_data import get_campaign_for_client

                camp = get_campaign_for_client(
                    conn, working_id, int(stored_research["campaign_id"])
                )
            if camp is None:
                camp = get_default_campaign(conn, working_id)
            if camp:
                camp_label = _blank(camp.get("campaign_name")) or camp_label
        except Exception:
            pass
        if camp_label:
            why_parts.append(
                f"Stored fit for {working_name} ({camp_label} campaign): "
                f"{_blank(stored_fit.get('fit_result'))}. See Campaign Fit below for why."
            )
        else:
            why_parts.append(
                f"Stored fit for {working_name}: {_blank(stored_fit.get('fit_result'))}. "
                "See Campaign Fit below for why."
            )
    elif has_stored_research:
        products = [
            i["value"]
            for i in research_items
            if i.get("finding_type") == "product"
        ][:4]
        if products:
            why_parts.append(
                f"Stored research shows product categories including {', '.join(products)}."
            )
    why_body = " ".join(why_parts)

    # Resolve campaign label for CAMPAIGN FIT even when fit sentence path skipped
    campaign_label_for_fit = _blank(stored_research.get("campaign_name"))
    if not campaign_label_for_fit:
        try:
            from client_setup_data import ensure_client_setup_schema, get_default_campaign

            ensure_client_setup_schema(conn)
            dc = get_default_campaign(conn, working_id)
            if dc:
                campaign_label_for_fit = _blank(dc.get("campaign_name"))
        except Exception:
            campaign_label_for_fit = ""
    # Prefer camp_label from fit sentence when set
    if camp_label:
        campaign_label_for_fit = camp_label

    primary_service_for_fit = ""
    try:
        from client_setup_data import (
            ensure_client_setup_schema,
            get_campaign_for_client,
            get_default_campaign,
        )

        ensure_client_setup_schema(conn)
        camp_row = None
        cid = stored_research.get("campaign_id")
        if cid:
            camp_row = get_campaign_for_client(conn, working_id, int(cid))
        if camp_row is None:
            camp_row = get_default_campaign(conn, working_id)
        if camp_row:
            primary_service_for_fit = _blank(camp_row.get("primary_service"))
            if not campaign_label_for_fit:
                campaign_label_for_fit = _blank(camp_row.get("campaign_name"))
    except Exception:
        primary_service_for_fit = ""

    campaign_fit = _build_campaign_fit_brief(
        company_name=company_name,
        working_name=working_name,
        working_id=working_id,
        campaign_name=campaign_label_for_fit or "Default",
        stored_fit=stored_fit if isinstance(stored_fit, dict) else None,
        research_items=research_items,
        working_milestones=working_milestones,
        other_milestones=other_milestones,
        primary_service=primary_service_for_fit,
    )

    # Talking points — CRM + stored research + fit (no invented facts)
    talking: list[str] = []
    talking.append(
        f"Confirm you are calling on behalf of {working_name} "
        f"(current status: {working_status or 'New'})."
    )
    if working_rn:
        talking.append(f"Reference Record No. {working_rn} in your CRM notes after the call.")
    if contacts:
        top = contacts[0]
        bit = top.name
        if top.title:
            bit += f", {top.title}"
        talking.append(f"Start with a known contact when appropriate: {bit}.")
    if working_milestones:
        talking.append(
            f"Acknowledge prior {working_name} engagement: "
            + ", ".join(sorted({m.label or m.milestone_type for m in working_milestones}))
            + "."
        )
    if sales_brief_items:
        top_ev = sales_brief_items[0]
        talking.append(
            f"Recent {working_name} sales-event history includes "
            f"{top_ev.get('event_type')} "
            f"({top_ev.get('event_date') or top_ev.get('source_date_time_text') or 'date on file'})."
        )
        for ev in sales_brief_items:
            note = _blank(ev.get("caller_notes")) or _blank(ev.get("sales_notes"))
            if note and len(note) > 40:
                talking.append(
                    f"Sales-event note ({ev.get('event_type')}): “{note[:160]}"
                    f"{'…' if len(note) > 160 else ''}”"
                )
                break
    caps = [
        i["value"]
        for i in research_items
        if i.get("finding_type") == "capability"
        and (i.get("evidence_level") or "") != "not_verified"
    ][:3]
    products = [
        i["value"] for i in research_items if i.get("finding_type") == "product"
    ][:4]
    if products:
        talking.append(
            f"Reference stored research: they publicly present "
            f"{', '.join(products)} — ask how metal components are sourced."
        )
    if caps:
        talking.append(
            f"Stored manufacturing language includes {', '.join(caps)}. "
            "Ask whether any stamped or fabricated components are outsourced."
        )
    if stored_fit and _blank(stored_fit.get("fit_result")):
        talking.append(
            f"Stored {working_name} fit is “{_blank(stored_fit.get('fit_result'))}” — "
            f"{_blank(stored_fit.get('why'))[:180]}"
            + ("…" if len(_blank(stored_fit.get("why"))) > 180 else "")
        )
    if stored_fit and stored_fit.get("missing_information"):
        gap0 = _blank(stored_fit["missing_information"][0])
        if gap0:
            talking.append(f"Investigate next: {gap0}")
    if opp and opp.signal_types and not working_milestones:
        src = ", ".join(opp.source_client_names or []) or "another NorthStar client"
        talking.append(
            f"Review NorthStar’s {src} commercial signals ({', '.join(opp.signal_types)}) "
            f"before outreach — useful context for {working_name}, not a claim that "
            f"{working_name} already completed that work."
        )
    for h in history_items[:3]:
        excerpt = _blank(h.get("excerpt"))
        if excerpt and len(excerpt) > 40:
            talking.append(
                f"Prior {_blank(h.get('client_name'))} note/activity: “{excerpt[:160]}"
                f"{'…' if len(excerpt) > 160 else ''}”"
            )
            break
    if len(talking) <= 2 and not history_items and not milestones and not research_items:
        talking.append(
            "Stored NorthStar detail for this call is limited — confirm decision maker "
            "and current need before pitching."
        )

    # Missing information — prefer stored research gaps; never claim unresearched when research exists
    missing: list[str] = []
    if not contacts:
        missing.append("No contacts on file for this company.")
    else:
        if not any(c.phone for c in contacts):
            missing.append("No verified phone numbers on known contacts.")
        if not any(c.email for c in contacts):
            missing.append("No verified email addresses on known contacts.")
        if not any(
            any(h in (c.title or "").lower() for h in ("buyer", "purchas", "procure", "sourc"))
            for c in contacts
        ):
            missing.append("Current purchasing / decision-maker contact is not clearly identified.")
    if not working_hist:
        missing.append(f"No recent {working_name} activity or notes on file.")
    if not has_working_ccr:
        missing.append(f"No stored {working_name} client-company relationship yet.")
    if not working_milestones and not (opp and opp.signal_types):
        missing.append(f"No {working_name} commercial milestone signals on file.")
    if stored_fit and isinstance(stored_fit.get("missing_information"), list):
        for g in stored_fit["missing_information"]:
            gtext = _blank(g)
            if gtext and gtext not in missing:
                missing.append(gtext)
    elif has_stored_research:
        # Research exists but no fit gaps — still surface common open questions as investigations
        if not any(
            i.get("finding_type") == "capability"
            and "stamp" in _blank(i.get("value")).lower()
            for i in research_items
        ):
            missing.append(
                "Outsourced / recurring stamped-component need not verified in stored research."
            )
    else:
        missing.append("NorthStar has not researched this company yet.")
    if "No known current project is confirmed in NorthStar for this Working For client." not in missing:
        missing.append(
            "No known current project is confirmed in NorthStar for this Working For client."
        )

    # Next step — research gaps + CRM context
    if stored_fit and _blank(stored_fit.get("fit_result")):
        next_step = (
            f"On the call for {working_name}, confirm how metal components are sourced "
            f"(make vs buy), whether any stamped parts or tooling exist, and whether new "
            f"programs or capacity constraints create opportunities. Stored fit: "
            f"{_blank(stored_fit.get('fit_result'))}."
        )
    elif has_stored_research:
        next_step = (
            f"Use stored company research in the call for {working_name}: confirm "
            "procurement ownership and whether any fabrication/stamping work is outsourced."
        )
    elif opp and not has_working_ccr:
        next_step = (
            f"Review cross-client history for {company_name}, then open the {working_name} "
            "workspace and confirm the decision maker before outreach."
        )
    elif working_hist:
        next_step = (
            f"Review the most recent {_blank(working_hist[0].get('client_name'))} history, "
            "confirm the contact, and follow up on the prior conversation."
        )
    elif contacts:
        next_step = (
            "Review existing contact details before outreach and confirm they are still "
            "the right decision maker."
        )
    else:
        next_step = (
            "Confirm decision maker and contact details before calling — NorthStar has "
            "limited outreach data for this Working For context."
        )

    # Narrative summary (business tone)
    if opp:
        summary = (
            f"{company_name} should be reviewed closely for {working_name} because NorthStar "
            f"already has meaningful commercial history with the company through "
            f"{', '.join(opp.source_client_names) or 'another NorthStar client'}. "
            f"Working For {working_name}: status {working_status or 'New'}"
            + (f", Record No. {working_rn}" if working_rn else "")
            + "."
        )
    elif other_rels:
        summary = (
            f"NorthStar has worked {company_name} for {working_name} and "
            f"{', '.join(_blank(r['client_name']) for r in other_rels)}. "
            f"For this call, Working For is {working_name} "
            f"(status {working_status or 'New'}"
            + (f", Record No. {working_rn}" if working_rn else "")
            + ")."
        )
    else:
        summary = (
            f"Prepare to call {company_name} for {working_name}. "
            f"Current status: {working_status or 'New'}"
            + (f"; Record No. {working_rn}" if working_rn else "")
            + "."
        )
    if has_stored_research and stored_research.get("last_researched"):
        summary += f" Last researched: {stored_research['last_researched']}."
    if stored_fit and _blank(stored_fit.get("fit_result")):
        summary += f" Stored {working_name} fit: {_blank(stored_fit.get('fit_result'))}."

    header_item = {
        "company": company_name,
        "working_for": working_name,
        "working_for_client_id": working_id,
        "external_record_no": working_rn,
        "master_record_no": master_rn,
        "research_record_no": research_rn,
        "status": working_status or "New",
        "has_relationship": has_working_ccr,
        "city": _blank(company.get("city")),
        "state": _blank(company.get("state")),
        "opportunity_score": opp.opportunity_score if opp else None,
        "last_researched": stored_research.get("last_researched") or "",
        "has_stored_research": has_stored_research,
        "stored_fit_result": _blank(stored_fit.get("fit_result")) if stored_fit else "",
    }

    cross_items = []
    for r in other_rels:
        cross_items.append(
            {
                "role": "Other NorthStar Client Experience",
                "client_name": _blank(r["client_name"]),
                "external_record_no": _blank(r["external_record_no"]),
                "status": _blank(r["status"]),
                "source": f"{_blank(r['client_name'])} History",
            }
        )
    for m in other_milestones:
        cross_items.append(
            {
                "role": "NORTHSTAR CROSS-CLIENT EXPERIENCE",
                "client_name": m.client_name,
                "signal": m.label or m.milestone_type,
                "milestone_date": m.milestone_date,
                "source": m.source,
                "note": (
                    f"{m.label or m.milestone_type} belongs to {m.client_name}; "
                    f"not a {working_name} milestone."
                ),
            }
        )
    if opp:
        cross_items.append(
            {
                "role": "Cross-Client Opportunity",
                "client_name": working_name,
                "source_clients": list(opp.source_client_names or []),
                "signals": list(opp.signal_types or []),
                "opportunity_score": opp.opportunity_score,
                "note": (
                    f"Signals belong to {', '.join(opp.source_client_names or ['another client'])}; "
                    f"they are not {working_name} milestones."
                ),
            }
        )

    working_signal_items = [
        {
            "signal": m.label or m.milestone_type,
            "client_name": m.client_name,
            "milestone_date": m.milestone_date,
            "source": m.source,
            "display": f"{m.label or m.milestone_type} · {m.client_name}",
        }
        for m in working_milestones
    ]

    fit_items: list[dict[str, Any]] = []
    if stored_fit:
        fit_items.append(
            {
                "client_name": working_name,
                "client_id": working_id,
                "fit_result": _blank(stored_fit.get("fit_result")),
                "why": _blank(stored_fit.get("why")),
                "potential_opportunity": _blank(stored_fit.get("potential_opportunity")),
                "concerns": stored_fit.get("concerns") or [],
                "missing_information": stored_fit.get("missing_information") or [],
                "supporting_evidence": stored_fit.get("supporting_evidence") or [],
                "provenance": f"Stored fit for {working_name} only — not shared across clients.",
            }
        )

    knowledge_body = (
        f"Stored NorthStar research findings (shared company intelligence). "
        f"Last researched: {stored_research.get('last_researched') or '—'}. "
        "Evidence levels are preserved from Research This Company — Ask does not re-crawl the web."
        if has_stored_research
        else "NorthStar has not researched this company yet."
    )

    # Advisory recommendation (read-only) — independent of Fit and Engagement
    from recommendation_data import build_northstar_recommendation
    from sales_events_intel import sales_event_engagement_signals

    se_signals, se_level = sales_event_engagement_signals(
        se_raw, closed_status=working_status
    )
    # Prefer richer engagement label when milestones also present
    eng_level = se_level
    eng_signals = list(se_signals)
    for m in working_milestones:
        label = _blank(m.label or m.milestone_type)
        if label and label not in eng_signals:
            eng_signals.append(label)
    fit_result_text = _blank(stored_fit.get("fit_result")) if stored_fit else ""
    rec = build_northstar_recommendation(
        fit_result=fit_result_text,
        engagement_level=eng_level,
        engagement_signals=eng_signals,
        relationship_status=working_status,
        sales_events=se_raw,
        contacts=[{
            "title": c.title,
            "first_name": "",
            "last_name": c.name,
        } for c in contacts],
        missing_information=list(missing),
        research_gaps=list(stored_fit.get("missing_information") or []) if stored_fit else [],
        campaign_name=campaign_label_for_fit or "Default",
        client_name=working_name,
        has_follow_up=any(_blank(h.get("follow_up_at")) for h in history_items),
    )
    rec_body = (
        f"Recommended Action: {rec.action}. {rec.advisory_note}"
    )
    rec_items = [
        {
            "recommended_action": rec.action,
            "why": rec.why,
            "evidence_used": rec.evidence_used,
            "still_need_to_know": rec.still_need_to_know,
            "fit_context": rec.fit_context,
            "engagement_context": rec.engagement_context,
            "confidence": rec.confidence,
            "advisory_note": rec.advisory_note,
        }
    ]

    # Client Operations — only when appointment-relevant (never dump email into every brief)
    ops_section: AskSection | None = None
    appointment_relevant = (
        "appointment" in working_status.lower()
        or "appointment" in (rec.action or "").lower()
        or any(
            _blank(e.get("event_type")).startswith("Appointment") for e in se_raw
        )
        or "appointment" in question.lower()
    )
    if appointment_relevant and working_id:
        try:
            from client_knowledge_data import list_approved_client_operations

            ops_all = list_approved_client_operations(int(working_id), user_id=None)
            ops_relevant = [
                it
                for it in ops_all
                if _blank(it.get("field_key"))
                in {
                    "who_takes_appointments",
                    "appointment_handling_instructions",
                    "appointment_recap_cc",
                }
                or re.search(
                    r"(?i)appointment|recap|takes?\s+appoint",
                    f"{it.get('title','')} {it.get('content','')}",
                )
            ]
            # Explicitly exclude email identity from routine call briefs
            ops_relevant = [
                it
                for it in ops_relevant
                if _blank(it.get("field_key")) != "northstar_client_email"
                and "@" not in _blank(it.get("title")).lower()
            ]
            if ops_relevant:
                ops_section = AskSection(
                    id="client_operations_relevant",
                    title="CLIENT OPERATIONS (RELEVANT)",
                    body=(
                        f"Approved appointment-related operational notes for {working_name}. "
                        "Not a full Client Operations dump."
                    ),
                    items=[
                        {
                            "title": it.get("title"),
                            "content": it.get("content"),
                            "provenance": (
                                f"Approved Client Operations"
                                + (
                                    f" · {it.get('source_document')}"
                                    if it.get("source_document")
                                    else ""
                                )
                            ),
                        }
                        for it in ops_relevant[:4]
                    ],
                )
        except Exception:
            ops_section = None

    # Company Story / Background — only when conversation background is relevant
    story_section: AskSection | None = None
    story_relevant = bool(
        re.search(
            r"(?i)\b(company\s+story|background|history|credibility|talking\s+points?|"
            r"how\s+long|founded|since\s+\d{4}|about\s+(?:the\s+)?(?:client|company)|"
            r"what\s+(?:should|can)\s+i\s+say)\b",
            question,
        )
        or "call brief" in question.lower()
        and re.search(r"(?i)\b(introduc|open(?:ing)?|credibility|story)\b", question)
    )
    # Soft relevance: include when talking points already reference client background language
    if not story_relevant and talking:
        joined = " ".join(talking).lower()
        story_relevant = any(
            x in joined for x in ("since ", "founded", "history", "family", "credibility")
        )
    if story_relevant and working_id:
        try:
            from client_knowledge_data import get_stored_knowledge_field

            story = get_stored_knowledge_field(
                int(working_id), "call_playbook", "company_story_background"
            )
            if story:
                story_section = AskSection(
                    id="company_story_background",
                    title="COMPANY STORY / BACKGROUND",
                    body=(
                        f"Stored conversation background for {working_name}. "
                        "Not the 30-Second Commercial — use when history/credibility helps."
                    ),
                    items=[{"content": story}],
                )
        except Exception:
            story_section = None

    sections = [
        AskSection(
            id="call_brief_header",
            title="CALL BRIEF",
            body="Working For context for this call only — statuses are not merged across clients.",
            items=[header_item],
        ),
        AskSection(
            id="why_matters",
            title="WHY THIS ACCOUNT MATTERS",
            body=why_body,
            items=[],
        ),
        AskSection(
            id="campaign_fit",
            title="CAMPAIGN FIT",
            body=(
                f"Stored {working_name} campaign fit from Research This Company. "
                "Commercial history increases confidence but does not by itself establish "
                "manufacturing fit."
                if campaign_fit.get("has_fit")
                else (
                    f"No stored fit for {working_name} yet — run Research This Company "
                    "with this Working For client."
                )
            ),
            items=list(campaign_fit.get("items") or []),
        ),
        AskSection(
            id="opportunity_engagement",
            title="OPPORTUNITY / ENGAGEMENT",
            body=(
                f"Engagement level for {working_name}: {eng_level or 'Insufficient Information'}. "
                "Independent from Campaign Fit and NorthStar Recommendation."
            ),
            items=[
                {
                    "level": eng_level or "Insufficient Information",
                    "signals": eng_signals,
                    "note": (
                        "Derived from imported sales events and commercial milestones. "
                        "Does not change CRM status."
                    ),
                }
            ],
        ),
        AskSection(
            id="northstar_recommendation",
            title="NORTHSTAR RECOMMENDATION",
            body=(
                rec_body
                if rec
                else "Not enough stored context to form an advisory recommendation yet."
            ),
            items=rec_items,
        ),
        AskSection(
            id="people",
            title="PEOPLE WE KNOW",
            body=(
                "Relevant known contacts from NorthStar. Missing fields are left blank — nothing invented."
                if contacts
                else "No contacts on file."
            ),
            items=[{k: v for k, v in c.items() if not k.startswith("_")} for c in contact_items[:12]],
        ),
        AskSection(
            id="company_knowledge",
            title="WHAT NORTHSTAR KNOWS ABOUT THE COMPANY",
            body=knowledge_body,
            items=research_items
            or (
                [
                    {
                        "label": "Research status",
                        "value": "NorthStar has not researched this company yet.",
                        "evidence_level": "not_verified",
                        "provenance": "Ask NorthStar (stored research only)",
                    }
                ]
                if not has_stored_research
                else []
            ),
        ),
        AskSection(
            id="northstar_history",
            title="NORTHSTAR HISTORY",
            body=(
                "Newest useful notes/activities first. Full history is available in Company Workspace."
                if history_items
                else "No notes or activities on file for authorized clients."
            ),
            items=history_items,
        ),
        AskSection(
            id="sales_appointment_history",
            title="RECENT SALES / APPOINTMENT HISTORY",
            body=(
                f"Useful recent imported {working_name} sales events (not a full dump). "
                "Send Information and RFQ are engagement/commercial events, not appointment counts. "
                "View Full History in Company Workspace."
                if sales_brief_items
                else f"No imported sales/appointment events on file for {working_name}."
            ),
            items=sales_brief_items,
        ),
        AskSection(
            id="commercial_history",
            title=f"COMMERCIAL HISTORY · {working_name.upper()}",
            body=(
                f"Working For {working_name} milestones only."
                if working_signal_items
                else f"No {working_name} commercial milestones on file."
            ),
            items=working_signal_items,
        ),
        AskSection(
            id="cross_client_intel",
            title="NORTHSTAR CROSS-CLIENT EXPERIENCE",
            body=(
                f"Authorized other-client experience for {company_name}. "
                f"Attributed to the source client — does not overwrite the {working_name} status."
                if [
                    i
                    for i in cross_items
                    if _blank(i.get("client_name")) != working_name
                    or _blank(i.get("signal"))
                    or i.get("signals")
                    or (
                        _blank(i.get("role"))
                        in {
                            "Other NorthStar Client Experience",
                            "NORTHSTAR CROSS-CLIENT EXPERIENCE",
                            "Cross-Client Opportunity",
                        }
                        and _blank(i.get("client_name")) != working_name
                    )
                ]
                else f"No other authorized client experience on file for {company_name}."
            ),
            items=(
                [
                    i
                    for i in cross_items
                    if (
                        _blank(i.get("role"))
                        in {
                            "Other NorthStar Client Experience",
                            "NORTHSTAR CROSS-CLIENT EXPERIENCE",
                            "Cross-Client Opportunity",
                        }
                        and (
                            _blank(i.get("client_name")) != working_name
                            or _blank(i.get("signal"))
                            or i.get("signals")
                        )
                    )
                    or (
                        _blank(i.get("client_name")) != working_name
                        and _blank(i.get("role")) not in {"Working For", "None", ""}
                    )
                ]
                or [
                    {
                        "role": "None",
                        "note": "No other authorized client relationships on file.",
                    }
                ]
            ),
        ),
        AskSection(
            id="fit_for_client",
            title=f"FIT FOR {working_name.upper()}",
            body=(
                f"Stored client-specific fit for {working_name}. "
                "Company research may be shared; fit is never copied between clients."
                if fit_items
                else (
                    f"No stored fit evaluation for {working_name} yet. "
                    "Run Research This Company while Working For this client to generate fit."
                )
            ),
            items=fit_items,
        ),
        AskSection(
            id="talking_points",
            title="SUGGESTED TALKING POINTS",
            body="Advisory talking points grounded only in stored NorthStar CRM + research.",
            items=[{"point": p} for p in talking],
        ),
        AskSection(
            id="missing_info",
            title="WHAT WE STILL DON'T KNOW",
            body="Actual remaining gaps — Ask NorthStar will not invent facts to fill them.",
            items=[{"gap": g} for g in missing],
        ),
        AskSection(
            id="next_step",
            title="RECOMMENDED NEXT STEP",
            body=next_step,
            items=[{"advisory_only": True, "next_step": next_step}],
        ),
        AskSection(
            id="research_preview",
            title="RESEARCH THIS COMPANY",
            body=(
                "Stored research is available — open Research This Company to review or refresh."
                if has_stored_research
                else "NorthStar has not researched this company yet."
            ),
            items=[
                {
                    "status": (
                        "Stored research available"
                        if has_stored_research
                        else "NorthStar has not researched this company yet."
                    ),
                    "last_researched": stored_research.get("last_researched") or "",
                    "has_stored_research": has_stored_research,
                    "fit_result": _blank(stored_fit.get("fit_result")) if stored_fit else "",
                    "action_label": (
                        "Refresh Research" if has_stored_research else "Research This Company"
                    ),
                    "could_add": (
                        [
                            "Refresh public web research if company intelligence may be stale",
                            "Review evidence levels and sources",
                            f"Review fit for {working_name}",
                        ]
                        if has_stored_research
                        else [
                            "Company verification",
                            "Website / locations",
                            "Capabilities and materials",
                            "Industries served",
                            f"Potential fit for {working_name}",
                        ]
                    ),
                }
            ],
        ),
    ]

    if ops_section is not None:
        # Place after recommendation — appointment-relevant ops only
        insert_at = next(
            (
                i + 1
                for i, s in enumerate(sections)
                if s.id == "northstar_recommendation"
            ),
            len(sections),
        )
        sections.insert(insert_at, ops_section)

    if story_section is not None:
        insert_at = next(
            (i for i, s in enumerate(sections) if s.id == "talking_points"),
            len(sections),
        )
        sections.insert(insert_at, story_section)

    sources = [
        AskSource(label="NorthStar CRM", detail="Call prep / account briefing"),
        AskSource(
            label=f"{working_name} History",
            detail=(
                f"Working For · Record No. {working_rn or '—'} · Status {working_status or 'New'}"
                + ("" if has_working_ccr else " · soft-open (no existing relationship)")
            ),
        ),
    ]
    if ops_section is not None:
        sources.append(
            AskSource(
                label="Client Knowledge Hub",
                detail="Approved Client Operations (appointment-relevant)",
            )
        )
    if story_section is not None:
        sources.append(
            AskSource(
                label="Client Knowledge Hub",
                detail="Company Story / Background (when conversation-relevant)",
            )
        )
    if has_stored_research:
        sources.append(
            AskSource(
                label="Stored company research",
                detail=(
                    f"company_research_findings / company_intelligence"
                    + (
                        f" · Last researched {stored_research.get('last_researched')}"
                        if stored_research.get("last_researched")
                        else ""
                    )
                ),
            )
        )
    if stored_fit:
        sources.append(
            AskSource(
                label=f"Stored fit · {working_name}",
                detail="company_client_fit (Working For client only)",
            )
        )
    for r in other_rels:
        sources.append(
            AskSource(
                label=f"{_blank(r['client_name'])} History",
                detail=f"Other NorthStar experience · Status {_blank(r['status']) or '—'}",
            )
        )
    if history_items:
        if any(h.get("source") == "NorthStar Activity" for h in history_items):
            sources.append(AskSource(label="NorthStar Activity", detail="Recent activities"))
        if any(h.get("source") == "Legacy LeadMaster Notes" for h in history_items):
            sources.append(AskSource(label="Legacy LeadMaster Notes", detail="Legacy notes"))
    if sales_brief_items:
        sources.append(
            AskSource(
                label="Imported Sales Events",
                detail=f"{len(sales_brief_items)} summarized from client_sales_events",
            )
        )
    if milestones or (opp and opp.signal_types):
        sources.append(AskSource(label="NorthStar CRM", detail="revenue_milestones / opportunity signals"))

    workspace = f"/companies/{research_rn}?client_id={working_id}" if research_rn else ""
    research_href = (
        f"/companies/{research_rn}/research?client_id={working_id}" if research_rn else ""
    )
    research_link_label = (
        "Refresh Research" if has_stored_research else "Research This Company"
    )
    links = [
        AskLink(label="View Full History", href=workspace),
        AskLink(label=f"Open {company_name} ({working_name})", href=workspace),
    ]
    if research_href:
        links.insert(0, AskLink(label=research_link_label, href=research_href))

    return AskNorthStarResponse(
        question="",
        intent="call_prep",
        summary=summary,
        scope="",
        # Resolved Working For for THIS answer (not a prior question's client).
        active_client_id=working_id,
        active_client_name=working_name,
        no_data=False,
        sections=sections,
        companies=[
            _company_card_from_row(
                company_id=company_id,
                company_name=company_name,
                external_record_no=research_rn,
                client_id=working_id,
                client_name=working_name,
                status=working_status or "New",
                city=_blank(company.get("city")),
                state=_blank(company.get("state")),
                badges=list(opp.signal_types) if opp else [],
                why=why_body,
                score=opp.opportunity_score if opp else None,
            )
        ],
        contacts=contacts,
        notes=[],
        milestones=milestones,
        sources=sources,
        recommended_links=links,
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=1,
    )


def _answer_company_intelligence(
    conn,
    *,
    company_name_query: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    if not company_name_query:
        return _empty_answer(
            intent="company_intelligence",
            summary=NO_DATA,
            detail="Ask about a specific company name stored in NorthStar.",
        )

    matches = _find_companies_by_name(conn, company_name_query, visible_ids=visible_ids)
    if not matches:
        return _empty_answer(
            intent="company_intelligence",
            summary=NO_DATA,
            detail=f'No authorized company matched “{company_name_query}”.',
        )

    company = matches[0]
    company_id = int(company["company_id"])
    company_name = _blank(company["company_name"])
    record_fallback = _blank(company["external_record_no"])

    # Client history (authorized only)
    history_rows = conn.execute(
        f"""
        SELECT
            cl.id AS client_id,
            cl.name AS client_name,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), ?) AS external_record_no,
            ccr.status,
            ccr.is_hot
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        WHERE ccr.company_id = ?
          AND ccr.client_id IN ({_placeholders(visible_ids)})
          AND {_ask_sql_ccr(conn, "ccr")}
        ORDER BY cl.name COLLATE NOCASE
        """,
        [record_fallback, company_id, *visible_ids],
    ).fetchall()

    sections: list[AskSection] = [
        AskSection(
            id="company",
            title="COMPANY",
            body=company_name,
            items=[
                {
                    "company_id": company_id,
                    "company_name": company_name,
                    "city": _blank(company.get("city")),
                    "state": _blank(company.get("state")),
                }
            ],
        )
    ]

    history_items = []
    companies: list[AskCompanyCard] = []
    for r in history_rows:
        cid = int(r["client_id"])
        rn = _blank(r["external_record_no"])
        status = _blank(r["status"])
        history_items.append(
            {
                "client_id": cid,
                "client_name": _blank(r["client_name"]),
                "external_record_no": rn,
                "status": status,
                "is_hot": bool(r["is_hot"]),
                "source": f"{_blank(r['client_name'])} History",
            }
        )
        companies.append(
            _company_card_from_row(
                company_id=company_id,
                company_name=company_name,
                external_record_no=rn,
                client_id=cid,
                client_name=_blank(r["client_name"]),
                status=status,
                city=_blank(company.get("city")),
                state=_blank(company.get("state")),
                badges=["Hot"] if r["is_hot"] else [],
            )
        )

    if history_items:
        sections.append(
            AskSection(
                id="client_history",
                title="NORTHSTAR CLIENT HISTORY",
                body="Authorized client-company relationships on file.",
                items=history_items,
            )
        )
    else:
        sections.append(
            AskSection(
                id="client_history",
                title="NORTHSTAR CLIENT HISTORY",
                body="No client-company relationship found in your authorized clients.",
                items=[],
            )
        )

    # Contacts are master-company scoped; label with authorized client relationships.
    contact_rows = conn.execute(
        f"""
        SELECT DISTINCT
            ct.id,
            ct.first_name,
            ct.last_name,
            ct.title,
            ct.phone,
            ct.email,
            cl.name AS client_name,
            cl.id AS client_id
        FROM contacts ct
        JOIN client_company_relationships ccr ON ccr.company_id = ct.company_id
        JOIN clients cl ON cl.id = ccr.client_id
        WHERE ct.company_id = ?
          AND ccr.client_id IN ({_placeholders(visible_ids)})
          AND {_ask_sql_contact(conn, "ct")}
          AND {_ask_sql_ccr(conn, "ccr")}
        ORDER BY ct.last_name COLLATE NOCASE, ct.first_name COLLATE NOCASE
        LIMIT 25
        """,
        [company_id, *visible_ids],
    ).fetchall()

    contacts = [
        AskContactCard(
            contact_id=int(r["id"]),
            name=f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
            title=_blank(r["title"]),
            phone=_blank(r["phone"]),
            email=_blank(r["email"]),
            client_id=int(r["client_id"]) if r["client_id"] is not None else None,
            client_name=_blank(r["client_name"]),
            source=f"{_blank(r['client_name'])} History" if r["client_name"] else "NorthStar CRM",
        )
        for r in contact_rows
    ]
    sections.append(
        AskSection(
            id="contacts",
            title="CONTACTS",
            body="Relevant known contacts from authorized client records."
            if contacts
            else "No contacts on file for authorized clients.",
            items=[c.model_dump() for c in contacts],
        )
    )

    # Recent activity
    activity_rows = conn.execute(
        f"""
        SELECT
            a.activity_id AS id,
            a.activity_type,
            a.activity_at,
            a.notes,
            a.outcome,
            a.created_by,
            cl.name AS client_name,
            cl.id AS client_id
        FROM activities a
        JOIN clients cl ON cl.id = a.client_id
        WHERE a.company_id = ?
          AND a.client_id IN ({_placeholders(visible_ids)})
        ORDER BY COALESCE(a.activity_at, a.created_at) DESC
        LIMIT 20
        """,
        [company_id, *visible_ids],
    ).fetchall()
    activity_items = [
        {
            "id": int(r["id"]),
            "activity_type": _blank(r["activity_type"]),
            "activity_at": _blank(r["activity_at"]),
            "notes": _blank(r["notes"])[:400],
            "outcome": _blank(r["outcome"]),
            "created_by": _blank(r["created_by"]),
            "client_name": _blank(r["client_name"]),
            "client_id": int(r["client_id"]),
            "source": "NorthStar Activity",
        }
        for r in activity_rows
    ]
    sections.append(
        AskSection(
            id="activity",
            title="RECENT NORTHSTAR ACTIVITY",
            body="Newest first from authorized clients."
            if activity_items
            else "No NorthStar activities on file for authorized clients.",
            items=activity_items,
        )
    )

    # Milestones
    milestone_rows = conn.execute(
        f"""
        SELECT
            rm.milestone_id AS id,
            rm.milestone_type,
            rm.milestone_date,
            cl.name AS client_name,
            cl.id AS client_id
        FROM revenue_milestones rm
        JOIN clients cl ON cl.id = rm.client_id
        WHERE rm.company_id = ?
          AND rm.client_id IN ({_placeholders(visible_ids)})
          AND rm.milestone_type IN (
              'Purchase Order', 'Quote', 'Appointment Set', 'WebLead', 'Hot'
          )
        ORDER BY COALESCE(rm.milestone_date, '') DESC, rm.milestone_id DESC
        """,
        [company_id, *visible_ids],
    ).fetchall()
    milestones = [
        AskMilestoneBadge(
            milestone_type=_blank(r["milestone_type"]),
            label=_MILESTONE_LABELS.get(_blank(r["milestone_type"]).lower(), _blank(r["milestone_type"])),
            milestone_date=_blank(r["milestone_date"]),
            client_id=int(r["client_id"]),
            client_name=_blank(r["client_name"]),
            source=f"{_blank(r['client_name'])} History",
        )
        for r in milestone_rows
    ]
    sections.append(
        AskSection(
            id="milestones",
            title="MILESTONES",
            body="Appointments, Quotes, Purchase Orders, WebLeads, Hot — from stored NorthStar milestones."
            if milestones
            else "No milestone records on file for authorized clients.",
            items=[m.model_dump() for m in milestones],
        )
    )

    # Imported sales / appointment / engagement history (client-scoped)
    from sales_events_intel import event_to_item, fetch_sales_events

    se_rows = fetch_sales_events(
        visible_client_ids=visible_ids,
        preferred_client_id=preferred_client_id,
        company_id=company_id,
        limit=40,
    )
    se_items = [event_to_item(r) for r in se_rows]
    # Cross-client labeling: if preferred client set, tag other-client rows
    for item, raw in zip(se_items, se_rows):
        cid = int(raw.get("client_id") or 0)
        cname = _blank(raw.get("client_name"))
        if preferred_client_id and cid and cid != preferred_client_id:
            item["cross_client_label"] = f"{cname} History"
            item["attribution"] = (
                f"Attributed to {cname} — not copied into the Working For client relationship."
            )
    sections.append(
        AskSection(
            id="sales_events",
            title="APPOINTMENT / ENGAGEMENT HISTORY",
            body=(
                "Imported client sales events (newest first). "
                "Send Information and RFQ are not counted as appointments."
                if se_items
                else "No imported appointment/engagement sales events on file for authorized clients."
            ),
            items=se_items,
        )
    )
    summary_bits_pending: list[str] = []
    if se_items:
        preferred_events = [
            i
            for i in se_items
            if not preferred_client_id or i.get("client_id") == preferred_client_id
        ] or se_items
        for i in preferred_events[:5]:
            summary_bits_pending.append(
                f"{i.get('event_type')} ({i.get('event_date') or i.get('source_date_time_text') or '—'})"
            )

    # Legacy notes (summarize / surface)
    legacy_items: list[dict[str, Any]] = []
    if _table_exists(conn, "legacy_notes"):
        legacy_rows = conn.execute(
            f"""
            SELECT
                ln.id,
                ln.note_text,
                ln.created_at,
                ln.source_field,
                cl.name AS client_name,
                cl.id AS client_id,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), ?) AS external_record_no
            FROM legacy_notes ln
            JOIN clients cl ON cl.id = ln.client_id
            JOIN client_company_relationships ccr
                ON ccr.company_id = ln.company_id AND ccr.client_id = ln.client_id
               AND {_ask_sql_ccr(conn, "ccr")}
            WHERE ln.company_id = ?
              AND ln.client_id IN ({_placeholders(visible_ids)})
            ORDER BY COALESCE(ln.created_at, '') DESC, ln.id DESC
            LIMIT 12
            """,
            [record_fallback, company_id, *visible_ids],
        ).fetchall()
        for r in legacy_rows:
            text = _blank(r["note_text"])
            legacy_items.append(
                {
                    "id": int(r["id"]),
                    "excerpt": text[:320] + ("…" if len(text) > 320 else ""),
                    "note_date": _blank(r["created_at"]),
                    "created_by": "",
                    "client_name": _blank(r["client_name"]),
                    "client_id": int(r["client_id"]),
                    "external_record_no": _blank(r["external_record_no"]),
                    "source": "Legacy LeadMaster Notes",
                }
            )
    sections.append(
        AskSection(
            id="legacy",
            title="LEGACY HISTORY",
            body="Relevant legacy notes from authorized client relationships."
            if legacy_items
            else "No legacy notes on file for authorized clients.",
            items=legacy_items,
        )
    )

    open_client = preferred_client_id
    if open_client is None and history_rows:
        open_client = int(history_rows[0]["client_id"])
    open_rn = record_fallback
    if open_client is not None:
        for r in history_rows:
            if int(r["client_id"]) == open_client:
                open_rn = _blank(r["external_record_no"]) or open_rn
                break

    sources = [
        AskSource(label="NorthStar CRM", detail="Master company record"),
    ]
    for item in history_items:
        sources.append(
            AskSource(
                label=f"{item['client_name']} History",
                detail=f"Record No. {item['external_record_no']} · Status {item['status']}",
            )
        )
    if activity_items:
        sources.append(AskSource(label="NorthStar Activity", detail=f"{len(activity_items)} recent activities"))
    if se_items:
        sources.append(
            AskSource(
                label="Imported Sales Events",
                detail=f"{len(se_items)} event(s) from client_sales_events",
            )
        )
    if legacy_items:
        sources.append(AskSource(label="Legacy LeadMaster Notes", detail=f"{len(legacy_items)} notes"))

    summary_bits: list[str] = []
    if history_items:
        names = [h["client_name"] for h in history_items]
        if len(names) == 1:
            summary_bits.append(
                f"NorthStar has worked {company_name} for {names[0]} "
                f"(Record No. {history_items[0]['external_record_no']}, "
                f"status {history_items[0]['status'] or '—'})."
            )
        else:
            summary_bits.append(
                f"NorthStar has worked {company_name} for both "
                + " and ".join(names)
                + "."
            )
            summary_bits.append(
                " ".join(
                    f"{h['client_name']}: Record No. {h['external_record_no']}, "
                    f"status {h['status'] or '—'}."
                    for h in history_items
                )
            )
    else:
        summary_bits.append(
            f"NorthStar recognizes {company_name}, but no authorized client relationship "
            "is on file yet."
        )
    if summary_bits_pending:
        summary_bits.append(
            "Recent appointment/engagement history (newest first): "
            + "; ".join(summary_bits_pending)
            + "."
        )

    return AskNorthStarResponse(
        question="",
        intent="company_intelligence",
        summary=" ".join(summary_bits),
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=sections,
        companies=companies
        or [
            _company_card_from_row(
                company_id=company_id,
                company_name=company_name,
                external_record_no=open_rn,
                client_id=open_client,
                client_name="",
                city=_blank(company.get("city")),
                state=_blank(company.get("state")),
            )
        ],
        contacts=contacts,
        notes=[],
        milestones=milestones,
        sources=sources,
        recommended_links=[
            AskLink(
                label=f"Open {company_name}",
                href=(
                    f"/companies/{open_rn}"
                    + (f"?client_id={open_client}" if open_client else "")
                ),
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
    )


def _answer_cross_client_why(
    *,
    user_id: int,
    company_query: str | None,
    target_query: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    with get_connection() as conn:
        target = None
        if target_query:
            target = _find_client_by_text(conn, target_query, visible_ids)
        if target is None and preferred_client_id and preferred_client_id in visible_ids:
            row = conn.execute(
                "SELECT id, name, code FROM clients WHERE id = ?",
                (preferred_client_id,),
            ).fetchone()
            if row:
                target = {
                    "id": int(row["id"]),
                    "name": _blank(row["name"]),
                    "code": _blank(row["code"]),
                }
        if target is None or not company_query:
            return _empty_answer(
                intent="cross_client_why",
                summary=NO_DATA,
                detail="Specify a company and target client (example: Why is Trailerman an opportunity for Brown?).",
            )

        matches = _find_companies_by_name(
            conn, company_query, visible_ids=visible_ids, limit=5
        )
        if not matches:
            return _empty_answer(
                intent="cross_client_why",
                summary=NO_DATA,
                detail=f'No company matched “{company_query}”.',
            )
        company_id = int(matches[0]["company_id"])
        company_name = _blank(matches[0]["company_name"])

    listed = list_cross_client_opportunities(
        user_id,
        target_client_id=int(target["id"]),
        include_dismissed=True,
    )
    opp = next((o for o in listed.opportunities if o.company_id == company_id), None)
    if opp is None:
        return _empty_answer(
            intent="cross_client_why",
            summary=NO_DATA,
            detail=(
                f"NorthStar does not currently flag {company_name} as a cross-client "
                f"opportunity for {target['name']}."
            ),
        )

    signals = list(opp.signal_types or [])
    sources_names = list(opp.source_client_names or [])
    explanation = (
        "NorthStar has previously generated significant commercial activity with this "
        "company for another authorized NorthStar client. Cross-client signals inform "
        "prioritization only; they do not change the target client's stored status."
    )
    section = AskSection(
        id="cross_client",
        title="CROSS-CLIENT OPPORTUNITY",
        body=explanation,
        items=[
            {
                "company_name": company_name,
                "target_client": opp.target_client_name or target["name"],
                "target_status": opp.target_client_status or "New",
                "source_clients": sources_names,
                "signals": signals,
                "opportunity_score": opp.opportunity_score,
                "score_label": opp.score_label,
                "why_summary": opp.why_summary or explanation,
            }
        ],
    )
    card = _company_card_from_row(
        company_id=company_id,
        company_name=company_name,
        external_record_no=opp.external_record_no,
        client_id=int(target["id"]),
        client_name=target["name"],
        status=opp.target_client_status or "New",
        badges=signals,
        why=explanation,
        score=opp.opportunity_score,
    )
    return AskNorthStarResponse(
        question="",
        intent="cross_client_why",
        summary=(
            f"{company_name} is a cross-client opportunity for {target['name']} "
            f"(score {opp.opportunity_score}). Source: {', '.join(sources_names) or 'other NorthStar client'}. "
            f"Signals: {', '.join(signals) or '—'}. Current {target['name']} status: "
            f"{opp.target_client_status or 'New'}."
        ),
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[section],
        companies=[card],
        contacts=[],
        notes=[],
        milestones=[
            AskMilestoneBadge(
                milestone_type=s,
                label=_MILESTONE_LABELS.get(s.lower(), s),
                client_name=(sources_names[0] if sources_names else ""),
                source=(f"{sources_names[0]} History" if sources_names else "NorthStar CRM"),
            )
            for s in signals
        ],
        sources=[
            AskSource(label="NorthStar CRM", detail="Cross-client opportunity engine"),
            AskSource(
                label=f"{target['name']} History",
                detail=f"Target status: {opp.target_client_status or 'New'}",
            ),
            *[
                AskSource(label=f"{name} History", detail="Source commercial signals")
                for name in sources_names
            ],
        ],
        recommended_links=[
            AskLink(
                label=f"Open {company_name} for {target['name']}",
                href=f"/companies/{opp.external_record_no}?client_id={target['id']}",
            ),
            AskLink(
                label="Cross-Client Opportunities",
                href=f"/cross-client-opportunities?target_client_id={target['id']}",
            ),
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
    )


def _answer_needs_next_action(
    *,
    client_query: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    with get_connection() as conn:
        client = None
        if client_query:
            client = _find_client_by_text(conn, client_query, visible_ids)
        if client is None and preferred_client_id and preferred_client_id in visible_ids:
            row = conn.execute(
                "SELECT id, name, code FROM clients WHERE id = ?",
                (preferred_client_id,),
            ).fetchone()
            if row:
                client = {
                    "id": int(row["id"]),
                    "name": _blank(row["name"]),
                    "code": _blank(row["code"]),
                }
        if client is None and len(visible_ids) == 1:
            row = conn.execute(
                "SELECT id, name, code FROM clients WHERE id = ?",
                (visible_ids[0],),
            ).fetchone()
            if row:
                client = {
                    "id": int(row["id"]),
                    "name": _blank(row["name"]),
                    "code": _blank(row["code"]),
                }
        if client is None:
            return _empty_answer(
                intent="needs_next_action",
                summary=NO_DATA,
                detail="Specify which client's prospects need a next action.",
            )

    wq = list_work_queue(client_id=int(client["id"]), work_type="needs-next-action")
    companies = [
        _company_card_from_row(
            company_id=item.company_id,
            company_name=item.company_name,
            external_record_no=item.external_record_no,
            client_id=item.client_id,
            client_name=item.client_name,
            status=item.status,
            why=item.why_in_queue or "Needs Next Action",
        )
        for item in wq.items
    ]
    if not companies:
        return _empty_answer(
            intent="needs_next_action",
            summary=NO_DATA,
            detail=f"No {client['name']} prospects currently need a next action.",
        )

    status_counts: dict[str, int] = {}
    for c in companies:
        status_counts[c.status or "—"] = status_counts.get(c.status or "—", 0) + 1
    breakdown = ", ".join(f"{n} {s}" for s, n in sorted(status_counts.items(), key=lambda x: (-x[1], x[0])))

    return AskNorthStarResponse(
        question="",
        intent="needs_next_action",
        summary=(
            f"{len(companies)} {client['name']} prospect"
            f"{'' if len(companies) == 1 else 's'} need a next action"
            + (f" ({breakdown})." if breakdown else ".")
        ),
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="needs_next_action",
                title="NEEDS NEXT ACTION",
                body=f"From Work Queue for {client['name']} — working statuses with no open follow-up.",
                items=[c.model_dump() for c in companies],
            )
        ],
        companies=companies,
        contacts=[],
        notes=[],
        milestones=[],
        sources=[
            AskSource(label="NorthStar CRM", detail="Work Queue · Needs Next Action"),
            AskSource(label=f"{client['name']} History", detail="Client-company relationship statuses"),
        ],
        recommended_links=[
            AskLink(
                label="Open Needs Next Action",
                href=f"/work-queue?client_id={client['id']}&type=needs-next-action",
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(companies),
    )


def _answer_status_list(
    *,
    client_query: str | None,
    status: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    status_key = _blank(status)
    if not status_key:
        return _empty_answer(intent="status_list", summary=NO_DATA, detail="Specify a status.")

    with get_connection() as conn:
        client = None
        if client_query:
            client = _find_client_by_text(conn, client_query, visible_ids)
        if client is None and preferred_client_id and preferred_client_id in visible_ids:
            row = conn.execute(
                "SELECT id, name, code FROM clients WHERE id = ?",
                (preferred_client_id,),
            ).fetchone()
            if row:
                client = {
                    "id": int(row["id"]),
                    "name": _blank(row["name"]),
                    "code": _blank(row["code"]),
                }
        if client is None:
            return _empty_answer(
                intent="status_list",
                summary=NO_DATA,
                detail="Specify which client's prospects to list.",
            )

        rows = conn.execute(
            f"""
            SELECT
                co.id AS company_id,
                co.company_name,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                    AS external_record_no,
                ccr.status,
                co.city,
                co.state,
                cl.id AS client_id,
                cl.name AS client_name
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            JOIN clients cl ON cl.id = ccr.client_id
            WHERE ccr.client_id = ?
              AND lower(trim(ccr.status)) = lower(trim(?))
              AND {_ask_sql_ccr(conn, "ccr")}
              AND {_ask_sql_company(conn, "co")}
            ORDER BY co.company_name COLLATE NOCASE
            """,
            (int(client["id"]), status_key),
        ).fetchall()

    companies = [
        _company_card_from_row(
            company_id=int(r["company_id"]),
            company_name=_blank(r["company_name"]),
            external_record_no=_blank(r["external_record_no"]),
            client_id=int(r["client_id"]),
            client_name=_blank(r["client_name"]),
            status=_blank(r["status"]),
            city=_blank(r["city"]),
            state=_blank(r["state"]),
        )
        for r in rows
    ]
    if not companies:
        return _empty_answer(
            intent="status_list",
            summary=NO_DATA,
            detail=f"No {client['name']} prospects currently have status “{status_key}”.",
        )

    return AskNorthStarResponse(
        question="",
        intent="status_list",
        summary=f"{len(companies)} {client['name']} prospect{'s' if len(companies) != 1 else ''} with status {status_key}.",
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="status_list",
                title=f"STATUS: {status_key.upper()}",
                body=f"Stored client-company relationship status for {client['name']}.",
                items=[c.model_dump() for c in companies[:100]],
            )
        ],
        companies=companies[:100],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[
            AskSource(label="NorthStar CRM", detail="client_company_relationships.status"),
            AskSource(label=f"{client['name']} History", detail=f"{len(companies)} matching relationships"),
        ],
        recommended_links=[
            AskLink(
                label=f"Prospects · {status_key}",
                href=f"/prospects?status={status_key}",
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(companies),
    )


def _answer_note_search(
    *,
    user_id: int,
    topic: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    if not topic:
        return _empty_answer(
            intent="note_search",
            summary=NO_DATA,
            detail="Specify a note topic to search (example: Find notes mentioning stamping).",
        )

    # Prefer FTS note/activity hits; fall back to LIKE on legacy_notes / activities.
    search = run_search(
        topic,
        user_id=user_id,
        client_id=preferred_client_id if preferred_client_id in (visible_ids or []) else None,
        all_clients=preferred_client_id is None,
        limit=40,
    )
    note_hits: list[AskNoteHit] = []
    seen: set[tuple[str, str, str]] = set()

    for hit in list(search.notes) + list(search.companies):
        # Only keep note-like / activity bodies that actually contain the topic
        blob = f"{hit.snippet or ''} {hit.title or ''} {hit.company_name or ''}".lower()
        if topic.lower() not in blob and hit.doc_type not in {"note", "activity"}:
            # Still include if FTS matched a note/activity doc
            if hit.doc_type not in {"note", "activity"}:
                continue
        if hit.doc_type not in {"note", "activity", "company"}:
            continue
        # For company hits, skip unless snippet mentions topic
        if hit.doc_type == "company" and topic.lower() not in (hit.snippet or "").lower():
            continue
        key = (
            _blank(hit.external_record_no),
            _blank(hit.client_name),
            _blank(hit.snippet)[:80],
        )
        if key in seen:
            continue
        seen.add(key)
        note_hits.append(
            AskNoteHit(
                company_id=hit.company_id,
                company_name=hit.company_name or hit.title,
                external_record_no=hit.external_record_no,
                client_id=hit.client_id,
                client_name=hit.client_name,
                excerpt=_strip_html(hit.snippet or hit.title),
                event_at=hit.event_at,
                source=(
                    "Legacy LeadMaster Notes"
                    if hit.source_table == "legacy_notes"
                    else "NorthStar Activity"
                    if hit.doc_type == "activity"
                    else "NorthStar CRM"
                ),
                doc_type=hit.doc_type,
                workspace_path=(
                    f"/companies/{hit.external_record_no}?client_id={hit.client_id}"
                    if hit.external_record_no and hit.client_id
                    else ""
                ),
            )
        )

    # Direct LIKE fallback for legacy notes if FTS missed
    if len(note_hits) < 5:
        with get_connection() as conn:
            if _table_exists(conn, "legacy_notes") and visible_ids:
                rows = conn.execute(
                    f"""
                    SELECT
                        co.id AS company_id,
                        co.company_name,
                        COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                            AS external_record_no,
                        cl.id AS client_id,
                        cl.name AS client_name,
                        ln.note_text,
                        ln.created_at
                    FROM legacy_notes ln
                    JOIN companies co ON co.id = ln.company_id
                    JOIN clients cl ON cl.id = ln.client_id
                    JOIN client_company_relationships ccr
                        ON ccr.company_id = ln.company_id AND ccr.client_id = ln.client_id
                       AND {_ask_sql_ccr(conn, "ccr")}
                    WHERE ln.client_id IN ({_placeholders(visible_ids)})
                      AND {_ask_sql_company(conn, "co")}
                      AND lower(ln.note_text) LIKE lower(?)
                    ORDER BY COALESCE(ln.created_at, '') DESC
                    LIMIT 30
                    """,
                    [*visible_ids, f"%{topic}%"],
                ).fetchall()
                for r in rows:
                    text = _blank(r["note_text"])
                    key = (
                        _blank(r["external_record_no"]),
                        _blank(r["client_name"]),
                        text[:80],
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    excerpt = _excerpt_around(text, topic)
                    note_hits.append(
                        AskNoteHit(
                            company_id=int(r["company_id"]),
                            company_name=_blank(r["company_name"]),
                            external_record_no=_blank(r["external_record_no"]),
                            client_id=int(r["client_id"]),
                            client_name=_blank(r["client_name"]),
                            excerpt=excerpt,
                            event_at=_blank(r["created_at"]),
                            source="Legacy LeadMaster Notes",
                            doc_type="note",
                            workspace_path=(
                                f"/companies/{_blank(r['external_record_no'])}"
                                f"?client_id={int(r['client_id'])}"
                            ),
                        )
                    )

            if _table_exists(conn, "activities") and visible_ids:
                rows = conn.execute(
                    f"""
                    SELECT
                        co.id AS company_id,
                        co.company_name,
                        COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                            AS external_record_no,
                        cl.id AS client_id,
                        cl.name AS client_name,
                        a.notes,
                        a.activity_at,
                        a.activity_type
                    FROM activities a
                    JOIN companies co ON co.id = a.company_id
                    JOIN clients cl ON cl.id = a.client_id
                    JOIN client_company_relationships ccr
                        ON ccr.company_id = a.company_id AND ccr.client_id = a.client_id
                       AND {_ask_sql_ccr(conn, "ccr")}
                    WHERE a.client_id IN ({_placeholders(visible_ids)})
                      AND {_ask_sql_company(conn, "co")}
                      AND (
                        lower(COALESCE(a.notes, '')) LIKE lower(?)
                        OR lower(COALESCE(a.outcome, '')) LIKE lower(?)
                      )
                    ORDER BY COALESCE(a.activity_at, a.created_at) DESC
                    LIMIT 30
                    """,
                    [*visible_ids, f"%{topic}%", f"%{topic}%"],
                ).fetchall()
                for r in rows:
                    text = _blank(r["notes"])
                    key = (
                        _blank(r["external_record_no"]),
                        _blank(r["client_name"]),
                        text[:80],
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    note_hits.append(
                        AskNoteHit(
                            company_id=int(r["company_id"]),
                            company_name=_blank(r["company_name"]),
                            external_record_no=_blank(r["external_record_no"]),
                            client_id=int(r["client_id"]),
                            client_name=_blank(r["client_name"]),
                            excerpt=_excerpt_around(text, topic),
                            event_at=_blank(r["activity_at"]),
                            source="NorthStar Activity",
                            doc_type="activity",
                            workspace_path=(
                                f"/companies/{_blank(r['external_record_no'])}"
                                f"?client_id={int(r['client_id'])}"
                            ),
                        )
                    )

    # Always include imported sales-event note text in note search
    with get_connection() as conn:
        if _table_exists(conn, "client_sales_events") and visible_ids:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(se.company_id, 0) AS company_id,
                    se.company_name,
                    se.source_record_number AS external_record_no,
                    se.client_id,
                    cl.name AS client_name,
                    se.caller_notes,
                    se.sales_notes,
                    se.event_date,
                    se.event_type,
                    se.source_date_time_text,
                    se.source_outcome,
                    se.outcome_normalized
                FROM client_sales_events se
                JOIN clients cl ON cl.id = se.client_id
                WHERE se.client_id IN ({_placeholders(visible_ids)})
                  AND (
                    lower(COALESCE(se.caller_notes, '')) LIKE lower(?)
                    OR lower(COALESCE(se.sales_notes, '')) LIKE lower(?)
                    OR lower(COALESCE(se.source_date_time_text, '')) LIKE lower(?)
                    OR lower(COALESCE(se.event_type, '')) LIKE lower(?)
                    OR lower(COALESCE(se.source_outcome, '')) LIKE lower(?)
                    OR lower(COALESCE(se.outcome_normalized, '')) LIKE lower(?)
                  )
                ORDER BY COALESCE(se.event_date, '') DESC, se.id DESC
                LIMIT 30
                """,
                [*visible_ids, f"%{topic}%", f"%{topic}%", f"%{topic}%", f"%{topic}%", f"%{topic}%", f"%{topic}%"],
            ).fetchall()
            for r in rows:
                text = _blank(r["caller_notes"]) or _blank(r["sales_notes"]) or _blank(r["source_date_time_text"])
                key = (
                    _blank(r["external_record_no"]),
                    _blank(r["client_name"]),
                    text[:80],
                    _blank(r["event_type"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                note_hits.append(
                    AskNoteHit(
                        company_id=int(r["company_id"]) or None,
                        company_name=_blank(r["company_name"]),
                        external_record_no=_blank(r["external_record_no"]),
                        client_id=int(r["client_id"]),
                        client_name=_blank(r["client_name"]),
                        excerpt=_excerpt_around(text, topic),
                        event_at=_blank(r["event_date"]),
                        source=f"Imported {_blank(r['event_type']) or 'Sales Event'}",
                        doc_type="note",
                        workspace_path=(
                            f"/companies/{_blank(r['external_record_no'])}"
                            f"?client_id={int(r['client_id'])}"
                            if _blank(r["external_record_no"])
                            else ""
                        ),
                    )
                )

    if not note_hits:
        return _empty_answer(
            intent="note_search",
            summary=NO_DATA,
            detail=f'No authorized notes or activities mention “{topic}”.',
        )

    companies = []
    seen_co: set[tuple[int, int | None]] = set()
    for n in note_hits:
        key = (n.company_id or 0, n.client_id)
        if key in seen_co:
            continue
        seen_co.add(key)
        if n.company_id and n.external_record_no:
            companies.append(
                _company_card_from_row(
                    company_id=n.company_id,
                    company_name=n.company_name,
                    external_record_no=n.external_record_no,
                    client_id=n.client_id,
                    client_name=n.client_name,
                    why=n.excerpt[:160],
                )
            )

    return AskNorthStarResponse(
        question="",
        intent="note_search",
        summary=f"Found {len(note_hits)} note/activity match{'es' if len(note_hits) != 1 else ''} for “{topic}”.",
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="notes",
                title="NOTE SEARCH RESULTS",
                body="Matches from Legacy LeadMaster Notes and NorthStar Activity only.",
                items=[n.model_dump() for n in note_hits],
            )
        ],
        companies=companies,
        contacts=[],
        notes=note_hits,
        milestones=[],
        sources=[
            AskSource(label="Legacy LeadMaster Notes", detail="legacy_notes.note_text"),
            AskSource(label="NorthStar Activity", detail="activities.notes"),
            AskSource(label="NorthStar CRM", detail="Authorized client scope only"),
        ],
        recommended_links=[],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(note_hits),
    )


def _answer_milestone_list(
    *,
    milestone_type: str,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                co.id AS company_id,
                co.company_name,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                    AS external_record_no,
                cl.id AS client_id,
                cl.name AS client_name,
                ccr.status,
                rm.milestone_type,
                rm.milestone_date
            FROM revenue_milestones rm
            JOIN companies co ON co.id = rm.company_id
            JOIN clients cl ON cl.id = rm.client_id
            JOIN client_company_relationships ccr
                ON ccr.company_id = rm.company_id AND ccr.client_id = rm.client_id
               AND {_ask_sql_ccr(conn, "ccr")}
            WHERE rm.client_id IN ({_placeholders(visible_ids)})
              AND {_ask_sql_company(conn, "co")}
              AND lower(rm.milestone_type) = lower(?)
            ORDER BY COALESCE(rm.milestone_date, '') DESC, co.company_name COLLATE NOCASE
            LIMIT 100
            """,
            [*visible_ids, milestone_type],
        ).fetchall()

    companies = [
        _company_card_from_row(
            company_id=int(r["company_id"]),
            company_name=_blank(r["company_name"]),
            external_record_no=_blank(r["external_record_no"]),
            client_id=int(r["client_id"]),
            client_name=_blank(r["client_name"]),
            status=_blank(r["status"]),
            badges=[_MILESTONE_LABELS.get(milestone_type.lower(), milestone_type)],
            why=f"{milestone_type} · {_blank(r['milestone_date'])}",
        )
        for r in rows
    ]
    if not companies:
        return _empty_answer(
            intent="milestone_list",
            summary=NO_DATA,
            detail=f"No authorized companies currently have a stored {milestone_type} milestone.",
        )

    return AskNorthStarResponse(
        question="",
        intent="milestone_list",
        summary=f"{len(companies)} compan{'y' if len(companies) == 1 else 'ies'} with a stored {milestone_type} milestone.",
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="milestones",
                title=milestone_type.upper(),
                body="From revenue_milestones — stored NorthStar commercial milestones only.",
                items=[c.model_dump() for c in companies],
            )
        ],
        companies=companies,
        contacts=[],
        notes=[],
        milestones=[],
        sources=[AskSource(label="NorthStar CRM", detail="revenue_milestones")],
        recommended_links=[],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(companies),
    )


def _answer_appointments_without_quote(
    *,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                co.id AS company_id,
                co.company_name,
                COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                    AS external_record_no,
                cl.id AS client_id,
                cl.name AS client_name,
                ccr.status
            FROM revenue_milestones appt
            JOIN companies co ON co.id = appt.company_id
            JOIN clients cl ON cl.id = appt.client_id
            JOIN client_company_relationships ccr
                ON ccr.company_id = appt.company_id AND ccr.client_id = appt.client_id
               AND {_ask_sql_ccr(conn, "ccr")}
            WHERE appt.client_id IN ({_placeholders(visible_ids)})
              AND {_ask_sql_company(conn, "co")}
              AND appt.milestone_type = 'Appointment Set'
              AND NOT EXISTS (
                  SELECT 1 FROM revenue_milestones q
                  WHERE q.company_id = appt.company_id
                    AND q.client_id = appt.client_id
                    AND q.milestone_type = 'Quote'
              )
            ORDER BY co.company_name COLLATE NOCASE
            LIMIT 100
            """,
            visible_ids,
        ).fetchall()

        # Also include companies with imported Appointment* sales events and no quote signal/milestone
        try:
            from client_engagement_import import ensure_engagement_import_schema

            ensure_engagement_import_schema()
            extra = conn.execute(
                f"""
                SELECT DISTINCT
                    COALESCE(se.company_id, 0) AS company_id,
                    se.company_name,
                    se.source_record_number AS external_record_no,
                    se.client_id,
                    cl.name AS client_name,
                    '' AS status
                FROM client_sales_events se
                JOIN clients cl ON cl.id = se.client_id
                WHERE se.client_id IN ({_placeholders(visible_ids)})
                  AND se.event_type LIKE 'Appointment%'
                  AND COALESCE(se.potential_quote_signal, 0) = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM revenue_milestones q
                      WHERE q.client_id = se.client_id
                        AND q.company_id = se.company_id
                        AND q.milestone_type = 'Quote'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM client_sales_events qe
                      WHERE qe.client_id = se.client_id
                        AND qe.company_id = se.company_id
                        AND (
                          qe.event_type = 'Quote'
                          OR COALESCE(qe.potential_quote_signal, 0) = 1
                          OR COALESCE(qe.quoted_amount, 0) > 0
                        )
                  )
                ORDER BY se.company_name COLLATE NOCASE
                LIMIT 100
                """,
                visible_ids,
            ).fetchall()
        except Exception:
            extra = []

    seen: set[tuple[int, int]] = set()
    merged = []
    for r in list(rows) + list(extra):
        key = (int(r["client_id"]), int(r["company_id"] or 0))
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)

    companies = [
        _company_card_from_row(
            company_id=int(r["company_id"]),
            company_name=_blank(r["company_name"]),
            external_record_no=_blank(r["external_record_no"]),
            client_id=int(r["client_id"]),
            client_name=_blank(r["client_name"]),
            status=_blank(r["status"]),
            badges=["Appointment"],
            why="Appointment history on file; no Quote milestone/signal for this client.",
        )
        for r in merged
    ]
    if not companies:
        return _empty_answer(
            intent="appointments_without_quote",
            summary=NO_DATA,
            detail="No authorized companies have Appointment Set without a Quote.",
        )

    return AskNorthStarResponse(
        question="",
        intent="appointments_without_quote",
        summary=f"{len(companies)} compan{'y' if len(companies) == 1 else 'ies'} with Appointment Set and no Quote.",
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="appt_no_quote",
                title="APPOINTMENTS WITHOUT QUOTE",
                body="Compared stored milestones and imported appointment sales events per client-company pair.",
                items=[c.model_dump() for c in companies],
            )
        ],
        companies=companies,
        contacts=[],
        notes=[],
        milestones=[],
        sources=[
            AskSource(label="NorthStar CRM", detail="revenue_milestones + client_sales_events"),
        ],
        recommended_links=[],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(companies),
    )


def _answer_sales_events(
    *,
    visible_ids: list[int],
    preferred_client_id: int | None,
    rev_spec: str | None = None,
    outcome: str | None = None,
    company: str | None = None,
    contact: str | None = None,
    appointment_only: bool = True,
    event_family: str | None = None,
    missing_event_types: list[str] | None = None,
) -> AskNorthStarResponse:
    """Read-only retrieval over imported client_sales_events."""
    from sales_events_intel import event_to_item, fetch_sales_events

    if not visible_ids:
        return _empty_answer(
            intent="sales_events",
            summary=NO_DATA,
            detail="No imported appointment/engagement events are available yet.",
        )

    family = event_family
    if appointment_only and not family:
        family = "appointment"

    rows = fetch_sales_events(
        visible_client_ids=visible_ids,
        preferred_client_id=preferred_client_id,
        company_name=company,
        contact_name=contact,
        event_family=family,
        rev_spec=rev_spec,
        missing_event_types=missing_event_types,
        limit=100,
    )
    if outcome:
        o = outcome.lower()
        rows = [
            r
            for r in rows
            if o in _blank(r.get("outcome_normalized")).lower()
            or o in _blank(r.get("source_outcome")).lower()
        ]

    if not rows:
        return _empty_answer(
            intent="sales_events",
            summary=NO_DATA,
            detail="No matching imported appointment/engagement events found.",
        )

    items = [event_to_item(r) for r in rows]
    companies = []
    seen_co: set[tuple[int, str]] = set()
    for r in rows:
        cid = int(r["client_id"])
        cname = _blank(r["company_name"])
        key = (cid, cname.lower())
        if key in seen_co or not cname:
            continue
        seen_co.add(key)
        companies.append(
            _company_card_from_row(
                company_id=int(r["company_id"]) if r["company_id"] else 0,
                company_name=cname,
                external_record_no=_blank(r["source_record_number"]),
                client_id=cid,
                client_name=_blank(r.get("client_name")),
                status=_blank(r["outcome_normalized"]),
                badges=[_blank(r["event_type"])],
                why=_blank(r["source_date_time_text"]) or _blank(r["event_type"]),
            )
        )

    rev_note = ""
    if rev_spec:
        linked = any(r.get("rev_spec_user_id") for r in rows)
        if not linked:
            rev_note = (
                f" Matched Source Revenue Specialist text containing “{rev_spec}” "
                "(historical import text — not linked to a NorthStar user account)."
            )

    unique_companies = len(seen_co)
    title = "APPOINTMENT / ENGAGEMENT HISTORY"
    if family == "rfq":
        title = "RFQ SALES EVENTS"
    elif family == "engagement":
        title = "SEND INFORMATION / ENGAGEMENT"
    elif family == "appointment":
        title = "APPOINTMENT EVENTS"

    return AskNorthStarResponse(
        question="",
        intent="sales_events",
        summary=(
            f"{len(items)} imported event{'s' if len(items) != 1 else ''} "
            f"across {unique_companies} compan{'y' if unique_companies == 1 else 'ies'}."
            + rev_note
        ),
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="sales_events",
                title=title,
                body=(
                    "Read-only imported client sales events. "
                    "Send Information and RFQ are not counted as appointments."
                    + rev_note
                ),
                items=items,
            )
        ],
        companies=companies[:50],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[AskSource(label="NorthStar CRM", detail="client_sales_events")],
        recommended_links=[],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(items),
    )


def _answer_contact_history(
    *,
    contact_query: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    """Contact-scoped sales-event history — do not mix other contacts at same company."""
    from sales_events_intel import event_to_item, fetch_sales_events

    name = _blank(contact_query)
    if not name:
        return _empty_answer(
            intent="contact_history",
            summary=NO_DATA,
            detail="Ask about a specific contact name.",
        )

    rows = fetch_sales_events(
        visible_client_ids=visible_ids,
        preferred_client_id=preferred_client_id,
        contact_name=name,
        limit=100,
    )
    tokens = [t for t in name.lower().split() if t]
    if len(tokens) >= 2:
        rows = [
            r
            for r in rows
            if all(t in _blank(r.get("contact_name")).lower() for t in tokens)
        ]
    if not rows:
        return _empty_answer(
            intent="contact_history",
            summary=NO_DATA,
            detail=f'No imported sales events found for contact “{name}”.',
        )

    items = [event_to_item(r) for r in rows]
    contact_id = next((int(r["contact_id"]) for r in rows if r.get("contact_id")), None)
    company_name = _blank(rows[0].get("company_name"))
    record_no = _blank(rows[0].get("source_record_number"))
    client_id = int(rows[0]["client_id"])
    links = []
    if contact_id:
        links.append(
            AskLink(
                label=f"Open {name}",
                href=f"/contacts/{contact_id}?client_id={client_id}",
            )
        )
    if record_no:
        links.append(
            AskLink(
                label=f"Open {company_name or 'Company'}",
                href=f"/companies/{record_no}?client_id={client_id}",
            )
        )

    return AskNorthStarResponse(
        question="",
        intent="contact_history",
        summary=(
            f"{len(items)} imported sales event{'s' if len(items) != 1 else ''} "
            f"for {name} at {company_name or 'their company'} (newest first)."
        ),
        scope="",
        active_client_id=preferred_client_id or client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="contact_sales_events",
                title=f"CONTACT HISTORY · {name.upper()}",
                body="Events for this contact only — not other people at the same company.",
                items=items,
            )
        ],
        companies=[
            _company_card_from_row(
                company_id=int(rows[0]["company_id"]) if rows[0].get("company_id") else 0,
                company_name=company_name,
                external_record_no=record_no,
                client_id=client_id,
                client_name=_blank(rows[0].get("client_name")),
                badges=[_blank(rows[0].get("event_type"))],
            )
        ],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[AskSource(label="Imported Sales Events", detail="client_sales_events")],
        recommended_links=links,
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(items),
    )


def _answer_cross_client_list(
    *,
    user_id: int,
    client_query: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    with get_connection() as conn:
        client = None
        if client_query:
            client = _find_client_by_text(conn, client_query, visible_ids)
        if client is None and preferred_client_id and preferred_client_id in visible_ids:
            row = conn.execute(
                "SELECT id, name, code FROM clients WHERE id = ?",
                (preferred_client_id,),
            ).fetchone()
            if row:
                client = {
                    "id": int(row["id"]),
                    "name": _blank(row["name"]),
                    "code": _blank(row["code"]),
                }
        if client is None:
            return _empty_answer(
                intent="cross_client_list",
                summary=NO_DATA,
                detail="Specify the target client for cross-client opportunities.",
            )

    listed = list_cross_client_opportunities(
        user_id, target_client_id=int(client["id"]), include_dismissed=False
    )
    companies = [
        _company_card_from_row(
            company_id=o.company_id,
            company_name=o.company_name,
            external_record_no=o.external_record_no,
            client_id=int(client["id"]),
            client_name=client["name"],
            status=o.target_client_status,
            badges=list(o.signal_types or []),
            why=o.why_summary,
            score=o.opportunity_score,
        )
        for o in listed.opportunities
    ]
    if not companies:
        return _empty_answer(
            intent="cross_client_list",
            summary=NO_DATA,
            detail=f"No cross-client opportunities currently flagged for {client['name']}.",
        )

    return AskNorthStarResponse(
        question="",
        intent="cross_client_list",
        summary=(
            f"{len(companies)} {client['name']} prospect"
            f"{'' if len(companies) == 1 else 's'} where another NorthStar client has commercial signals."
        ),
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="cross_client_list",
                title="CROSS-CLIENT OPPORTUNITIES",
                body=f"Target client: {client['name']}. Sorted by opportunity score.",
                items=[c.model_dump() for c in companies],
            )
        ],
        companies=companies,
        contacts=[],
        notes=[],
        milestones=[],
        sources=[
            AskSource(label="NorthStar CRM", detail="Cross-client opportunity engine"),
            AskSource(label=f"{client['name']} History", detail="Target client relationships"),
        ],
        recommended_links=[
            AskLink(
                label="Open Cross-Client Opportunities",
                href=f"/cross-client-opportunities?target_client_id={client['id']}",
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(companies),
    )


def _answer_client_contacts(
    *,
    user_id: int,
    client_query: str | None,
    person_query: str | None,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    """Answer from APPROVED/ACTIVE Client Contacts only (not CRM prospect contacts)."""
    from client_contacts_data import list_client_contacts

    client_id = preferred_client_id if preferred_client_id in (visible_ids or []) else None
    client_name = ""
    if client_query:
        with get_connection() as conn:
            found = _find_client_by_text(conn, client_query, visible_ids)
            if found:
                client_id = int(found["id"])
                client_name = _blank(found["name"])
    if client_id is None and preferred_client_id in (visible_ids or []):
        client_id = preferred_client_id
    if client_id is None:
        return _empty_answer(
            intent="client_contacts",
            summary=NO_DATA,
            detail=(
                "Which client? Switch to Active Client scope or name the client "
                "(e.g. Carmeco) in your question."
            ),
        )

    with get_connection() as conn:
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        client_name = client_name or (_blank(row["name"]) if row else "")

    contacts = list_client_contacts(client_id, user_id=user_id, include_inactive=False)
    person_q = _blank(person_query).lower()
    if person_q:
        contacts = [
            c
            for c in contacts
            if person_q in c.name.lower()
            or (c.email and person_q in c.email.lower())
        ]

    if not contacts:
        return AskNorthStarResponse(
            question="",
            intent="client_contacts",
            summary=(
                f"No active Client Contacts are stored yet for {client_name or 'this client'}. "
                "These are people who work for the NorthStar client (not CRM prospect contacts). "
                "Pending extraction proposals are not used as facts."
            ),
            scope="",
            active_client_id=client_id,
            active_client_name=client_name,
            no_data=True,
            sections=[
                AskSection(
                    id="client_contacts",
                    title="CLIENT CONTACTS",
                    body=(
                        "Approved/active Client Contacts only. "
                        "Add or approve people in Client Knowledge → Client Contacts."
                    ),
                )
            ],
            research_options=[],
            research_available=False,
            result_count=0,
        )

    lines: list[str] = []
    for c in contacts:
        bits = [c.name]
        if c.title:
            bits.append(c.title)
        if c.role_type:
            bits.append(f"Role: {c.role_type}")
        if c.email:
            bits.append(c.email)
        if c.phone:
            bits.append(c.phone)
        lines.append(" · ".join(bits))

    summary = (
        f"{client_name} Client Contacts ({len(contacts)} active — "
        "people who work for this NorthStar client, not CRM prospects):\n"
        + "\n".join(f"• {ln}" for ln in lines)
    )
    return AskNorthStarResponse(
        question="",
        intent="client_contacts",
        summary=summary,
        scope="",
        active_client_id=client_id,
        active_client_name=client_name,
        no_data=False,
        sections=[
            AskSection(
                id="client_contacts",
                title="CLIENT CONTACTS (client staff — not prospect CRM)",
                body="\n".join(lines),
            )
        ],
        research_options=[],
        research_available=False,
        result_count=len(contacts),
    )


def _answer_client_strategy_knowledge(
    *,
    user_id: int,
    client_query: str | None,
    topic: str,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    """Answer from stored Strategy / Call Playbook knowledge fields (not Pending proposals)."""
    from client_document_extraction import FIELD_CATALOG
    from client_knowledge_data import get_knowledge_sections, get_stored_knowledge_field

    client_id = preferred_client_id if preferred_client_id in (visible_ids or []) else None
    client_name = ""
    if client_query:
        with get_connection() as conn:
            found = _find_client_by_text(conn, client_query, visible_ids)
            if found:
                client_id = int(found["id"])
                client_name = _blank(found["name"])
    if client_id is None and preferred_client_id in (visible_ids or []):
        client_id = preferred_client_id
    if client_id is None:
        return _empty_answer(
            intent="client_strategy_knowledge",
            summary=NO_DATA,
            detail=(
                "Which client? Switch to Active Client scope or name the client "
                "(e.g. Carmeco) in your question."
            ),
        )

    with get_connection() as conn:
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        client_name = client_name or (_blank(row["name"]) if row else "")

    field_specs: list[tuple[str, str]] = []
    if topic == "prospecting_guidance":
        field_specs = [("strategy", "prospecting_guidance")]
    elif topic == "sales_challenges_barriers":
        field_specs = [("strategy", "sales_challenges_barriers")]
    elif topic == "company_story_background":
        field_specs = [("call_playbook", "company_story_background")]
    else:
        field_specs = [
            ("strategy", "prospecting_guidance"),
            ("strategy", "sales_challenges_barriers"),
            ("call_playbook", "company_story_background"),
        ]

    items: list[dict[str, Any]] = []
    for section, field in field_specs:
        value = get_stored_knowledge_field(client_id, section, field, user_id=user_id)
        if not value:
            continue
        label = (FIELD_CATALOG.get(section) or {}).get(field) or field.replace("_", " ").title()
        items.append(
            {
                "field": field,
                "label": label,
                "section": section,
                "content": value,
                "note": (
                    "Strategy context only — does not independently determine Campaign Fit."
                    if field == "sales_challenges_barriers"
                    else (
                        "Targeting / prospecting guidance — not Target Industries."
                        if field == "prospecting_guidance"
                        else "Conversation background — not the 30-Second Commercial."
                    )
                ),
            }
        )

    # Also surface related strategy context when asking generally
    if topic == "general":
        try:
            sections = get_knowledge_sections(client_id, user_id=user_id)
            strat = next((s for s in sections if s.section_key == "strategy"), None)
            if strat and isinstance(strat.payload, dict):
                for fk in ("sales_goals", "reason_hired"):
                    val = _blank(strat.payload.get(fk))
                    if val and not any(i.get("field") == fk for i in items):
                        label = (FIELD_CATALOG.get("strategy") or {}).get(fk) or fk
                        items.append(
                            {
                                "field": fk,
                                "label": label,
                                "section": "strategy",
                                "content": val,
                                "note": "Related strategy knowledge.",
                            }
                        )
        except Exception:
            pass

    if not items:
        return AskNorthStarResponse(
            question="",
            intent="client_strategy_knowledge",
            summary=(
                f"No stored Strategy / Playbook knowledge for this topic yet for "
                f"{client_name or 'this client'}. Pending extraction proposals are not used as facts."
            ),
            scope="",
            active_client_id=client_id,
            active_client_name=client_name,
            no_data=True,
            sections=[
                AskSection(
                    id="client_strategy_knowledge",
                    title="CLIENT STRATEGY KNOWLEDGE",
                    body=(
                        "Approve remapped proposals into Prospecting Guidance, "
                        "Sales Challenges / Barriers, or Company Story / Background "
                        "in Client Knowledge to make them available here."
                    ),
                    items=[],
                )
            ],
            companies=[],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[AskSource(label="Client Knowledge", detail="Strategy / Call Playbook")],
            recommended_links=[
                AskLink(
                    label="Client Knowledge",
                    href=f"/clients/{client_id}/knowledge",
                )
            ],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
            result_count=0,
        )

    return AskNorthStarResponse(
        question="",
        intent="client_strategy_knowledge",
        summary=(
            f"Stored strategy/playbook knowledge for {client_name} "
            f"({len(items)} field{'s' if len(items) != 1 else ''})."
        ),
        scope="",
        active_client_id=client_id,
        active_client_name=client_name,
        no_data=False,
        sections=[
            AskSection(
                id="client_strategy_knowledge",
                title="CLIENT STRATEGY KNOWLEDGE",
                body=(
                    "Approved/stored Client Knowledge only. "
                    "Sales Challenges / Barriers inform strategy context and do not "
                    "independently determine Campaign Fit."
                ),
                items=items,
            )
        ],
        companies=[],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[AskSource(label="Client Knowledge", detail="Strategy / Call Playbook")],
        recommended_links=[
            AskLink(
                label="Client Knowledge",
                href=f"/clients/{client_id}/knowledge",
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(items),
    )


def _answer_email_templates(
    *,
    user_id: int,
    client_query: str | None,
    topic: str,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    """Answer from APPROVED active email templates only — never Pending extractions."""
    from client_knowledge_data import list_email_templates

    client_id = preferred_client_id if preferred_client_id in (visible_ids or []) else None
    client_name = ""
    if client_query:
        with get_connection() as conn:
            found = _find_client_by_text(conn, client_query, visible_ids)
            if found:
                client_id = int(found["id"])
                client_name = _blank(found["name"])
    if client_id is None and preferred_client_id in (visible_ids or []):
        client_id = preferred_client_id
    if client_id is None:
        return _empty_answer(
            intent="email_templates",
            summary=NO_DATA,
            detail=(
                "Which client? Switch to Active Client scope or name the client "
                "(e.g. Carmeco) in your question."
            ),
        )

    with get_connection() as conn:
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        client_name = client_name or (_blank(row["name"]) if row else "")

    templates = [
        t for t in list_email_templates(client_id, user_id=user_id) if t.is_active
    ]
    if topic == "appointment":
        templates = [
            t
            for t in templates
            if "appointment" in (t.template_type or "").lower()
            or "appointment" in (t.template_name or "").lower()
        ]
    elif topic == "send_information":
        templates = [
            t
            for t in templates
            if "send" in (t.template_type or "").lower()
            or "follow" in (t.template_type or "").lower()
            or "send" in (t.template_name or "").lower()
        ]

    if not templates:
        return AskNorthStarResponse(
            question="",
            intent="email_templates",
            summary=(
                f"No approved active email templates stored yet for {client_name or 'this client'}. "
                "Pending Strategy Import Review pieces are not used as templates."
            ),
            scope="",
            active_client_id=client_id,
            active_client_name=client_name,
            no_data=True,
            sections=[
                AskSection(
                    id="email_templates",
                    title="EMAIL TEMPLATES",
                    body=(
                        "Approve templates via Build Template in Strategy Import Review "
                        "to make them available here."
                    ),
                    items=[],
                )
            ],
            companies=[],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[AskSource(label="Client Knowledge", detail="Email Templates")],
            recommended_links=[
                AskLink(
                    label="Client Knowledge · Email Templates",
                    href=f"/clients/{client_id}/knowledge",
                )
            ],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
            result_count=0,
        )

    items = [
        {
            "template_id": t.template_id,
            "template_name": t.template_name,
            "template_type": t.template_type,
            "subject": t.subject or "(no subject stored)",
            "body": t.body,
            "active": t.is_active,
        }
        for t in templates
    ]
    return AskNorthStarResponse(
        question="",
        intent="email_templates",
        summary=f"{len(items)} approved active email template(s) for {client_name}.",
        scope="",
        active_client_id=client_id,
        active_client_name=client_name,
        no_data=False,
        sections=[
            AskSection(
                id="email_templates",
                title="EMAIL TEMPLATES",
                body=(
                    "Approved active Client Knowledge templates only. "
                    "Storage/retrieval — Ask does not send email."
                ),
                items=items,
            )
        ],
        companies=[],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[AskSource(label="Client Knowledge", detail="Email Templates")],
        recommended_links=[
            AskLink(
                label="Client Knowledge · Email Templates",
                href=f"/clients/{client_id}/knowledge",
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(items),
    )


def _answer_client_operations(
    *,
    user_id: int,
    client_query: str | None,
    topic: str,
    visible_ids: list[int],
    preferred_client_id: int | None,
) -> AskNorthStarResponse:
    """Answer from APPROVED Client Operations knowledge only."""
    from client_knowledge_data import list_approved_client_operations

    client_id = preferred_client_id if preferred_client_id in (visible_ids or []) else None
    client_name = ""
    if client_query:
        with get_connection() as conn:
            found = _find_client_by_text(conn, client_query, visible_ids)
            if found:
                client_id = int(found["id"])
                client_name = _blank(found["name"])
    if client_id is None and preferred_client_id in (visible_ids or []):
        client_id = preferred_client_id
    if client_id is None:
        return _empty_answer(
            intent="client_operations",
            summary=NO_DATA,
            detail=(
                "Which client? Switch to Active Client scope or name the client "
                "(e.g. Carmeco) in your question."
            ),
        )

    with get_connection() as conn:
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        client_name = client_name or (_blank(row["name"]) if row else "")

    items = list_approved_client_operations(client_id, user_id=user_id)
    topic_keys = {
        "email": {"northstar_client_email"},
        "assignment": {"northstar_revenue_specialist"},
        "who_takes_appointments": {"who_takes_appointments", "appointment_handling_instructions"},
        "recap_cc": {"appointment_recap_cc", "appointment_handling_instructions"},
        "appointment_handling": {
            "appointment_handling_instructions",
            "who_takes_appointments",
            "appointment_recap_cc",
        },
        "general": set(),
    }
    wanted = topic_keys.get(topic) or set()
    if wanted:
        filtered = [
            it
            for it in items
            if _blank(it.get("field_key")) in wanted
            or any(
                w.replace("_", " ") in f"{it.get('title','')} {it.get('content','')}".lower()
                for w in wanted
            )
        ]
        # Fall back to keyword match in content for remapped unmapped titles
        if not filtered and topic == "email":
            filtered = [it for it in items if "@" in _blank(it.get("content"))]
        if not filtered and topic == "assignment":
            filtered = [
                it
                for it in items
                if _blank(it.get("field_key")) == "northstar_revenue_specialist"
                or re.search(
                    r"(?i)revenue\s+specialist|tyler\s+sullivan",
                    f"{it.get('title','')} {it.get('content','')}",
                )
            ]
        if not filtered and topic in {"who_takes_appointments", "recap_cc", "appointment_handling"}:
            filtered = [
                it
                for it in items
                if re.search(
                    r"(?i)appointment|recap|takes?\s+appoint",
                    f"{it.get('title','')} {it.get('content','')}",
                )
            ]
        items = filtered or items

    if not items:
        # Still allow sender-account answers when configured accounts exist
        sender_only_sections: list[AskSection] = []
        if topic in {"email", "assignment", "general"}:
            try:
                from client_email_accounts_data import list_configured_sender_facts

                facts = list_configured_sender_facts(client_id, user_id=user_id)
                if facts:
                    sender_items = []
                    for f in facts:
                        reps = ", ".join(f.get("assigned_reps") or []) or "(no rep assignment)"
                        status = _blank(f.get("connection_status")) or "not_connected"
                        connected_note = (
                            "Connected"
                            if f.get("connected")
                            else f"Not connected ({status}) — configuration only"
                        )
                        sender_items.append(
                            {
                                "title": f.get("email_address") or "Sender account",
                                "content": (
                                    f"Display: {f.get('display_name') or '—'} · "
                                    f"Provider: {f.get('provider') or 'Other'} · "
                                    f"Default: {'yes' if f.get('is_default') else 'no'} · "
                                    f"Assigned: {reps} · {connected_note}"
                                ),
                                "provenance": "Configured client email account (not auto-send)",
                            }
                        )
                    sender_only_sections.append(
                        AskSection(
                            id="sender_accounts",
                            title="EMAIL & SENDING (configured sender accounts)",
                            body=(
                                "Configured sender-account data only. "
                                "Not connected unless connection_status is connected."
                            ),
                            items=sender_items,
                        )
                    )
            except Exception:
                pass
        if sender_only_sections:
            return AskNorthStarResponse(
                question="",
                intent="client_operations",
                summary=(
                    f"Configured sender accounts for {client_name or 'this client'} "
                    "(no matching approved Client Operations items for this topic)."
                ),
                scope="",
                active_client_id=client_id,
                active_client_name=client_name,
                no_data=False,
                sections=sender_only_sections,
                companies=[],
                contacts=[],
                notes=[],
                milestones=[],
                sources=[
                    AskSource(
                        label="Client Email Accounts",
                        detail="Configured sender identity (preview/config only)",
                    )
                ],
                recommended_links=[
                    AskLink(
                        label="Open Client Knowledge",
                        href=f"/clients/{client_id}/knowledge",
                    )
                ],
                research_options=list(RESEARCH_OPTIONS),
                research_available=False,
                result_count=sum(len(s.items or []) for s in sender_only_sections),
            )
        return AskNorthStarResponse(
            question="",
            intent="client_operations",
            summary=(
                f"No approved Client Operations knowledge is stored yet for {client_name or 'this client'}. "
                "Pending extraction proposals are not used as facts."
            ),
            scope="",
            active_client_id=client_id,
            active_client_name=client_name,
            no_data=True,
            sections=[
                AskSection(
                    id="client_operations",
                    title="CLIENT OPERATIONS",
                    body=(
                        "Approved operational knowledge only. "
                        "Review and approve items in Client Knowledge → Client Operations."
                    ),
                    items=[],
                )
            ],
            companies=[],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[
                AskSource(
                    label="Client Knowledge Hub",
                    detail="Approved Client Operations (Pending/Rejected excluded)",
                )
            ],
            recommended_links=[
                AskLink(
                    label="Open Client Knowledge",
                    href=f"/clients/{client_id}/knowledge",
                )
            ],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
            result_count=0,
        )

    section_items = [
        {
            "title": it.get("title") or "Operational note",
            "content": it.get("content"),
            "field_key": it.get("field_key"),
            "source_document": it.get("source_document"),
            "source_snippet": it.get("source_snippet"),
            "approved_at": it.get("approved_at"),
            "approved_by": it.get("approved_by"),
            "provenance": (
                f"Approved Client Operations"
                + (f" · {it.get('source_document')}" if it.get("source_document") else "")
                + (f" · by {it.get('approved_by')}" if it.get("approved_by") else "")
            ),
        }
        for it in items
    ]
    summary_bits = [
        f"{it.get('title')}: {it.get('content')}" for it in items[:3] if it.get("content")
    ]

    sections = [
        AskSection(
            id="client_operations",
            title="CLIENT OPERATIONS",
            body=(
                f"Approved operational knowledge for {client_name}. "
                "Pending or Rejected extraction proposals are not included."
            ),
            items=section_items,
        )
    ]

    # Configured sender accounts (approved configuration only — not implied connected)
    if topic in {"email", "assignment", "general"}:
        try:
            from client_email_accounts_data import list_configured_sender_facts

            facts = list_configured_sender_facts(client_id, user_id=user_id)
            if facts:
                sender_items = []
                for f in facts:
                    reps = ", ".join(f.get("assigned_reps") or []) or "(no rep assignment)"
                    status = _blank(f.get("connection_status")) or "not_connected"
                    connected_note = (
                        "Connected"
                        if f.get("connected")
                        else f"Not connected ({status}) — configuration only"
                    )
                    sender_items.append(
                        {
                            "title": f.get("email_address") or "Sender account",
                            "content": (
                                f"Display: {f.get('display_name') or '—'} · "
                                f"Provider: {f.get('provider') or 'Other'} · "
                                f"Default: {'yes' if f.get('is_default') else 'no'} · "
                                f"Assigned: {reps} · {connected_note}"
                            ),
                            "provenance": "Configured client email account (not auto-send)",
                        }
                    )
                sections.append(
                    AskSection(
                        id="sender_accounts",
                        title="EMAIL & SENDING (configured sender accounts)",
                        body=(
                            "Uses configured sender-account data only. "
                            "NorthStar does not imply mailbox connection unless connection_status is connected."
                        ),
                        items=sender_items,
                    )
                )
            elif topic in {"email", "assignment"}:
                sections.append(
                    AskSection(
                        id="sender_accounts",
                        title="EMAIL & SENDING (configured sender accounts)",
                        body=(
                            "No sender accounts are configured yet for this client. "
                            "Client Operations may still list an approved NorthStar Client Email / "
                            "Revenue Specialist as suggestions — configure under Client Knowledge → "
                            "Client Operations → EMAIL & SENDING."
                        ),
                        items=[],
                    )
                )
        except Exception:
            pass

    # Soft-link named people to structured Client Contacts (does not rewrite ops text)
    if topic in {"who_takes_appointments", "appointment_handling", "recap_cc", "general"}:
        try:
            from client_contacts_data import contacts_mentioned_in_text

            linked: list[Any] = []
            seen_ids: set[int] = set()
            for it in items:
                for c in contacts_mentioned_in_text(
                    client_id, _blank(it.get("content")), user_id=user_id
                ):
                    if c.contact_id in seen_ids:
                        continue
                    seen_ids.add(c.contact_id)
                    linked.append(c)
            if linked:
                link_lines = []
                for c in linked:
                    bits = [c.name]
                    if c.title:
                        bits.append(c.title)
                    if c.email:
                        bits.append(c.email)
                    if c.phone:
                        bits.append(c.phone)
                    if c.role_type:
                        bits.append(f"Role: {c.role_type}")
                    link_lines.append(" · ".join(bits))
                sections.append(
                    AskSection(
                        id="client_contacts_linked",
                        title="LINKED CLIENT CONTACTS (client staff — not prospect CRM)",
                        body="\n".join(link_lines),
                    )
                )
        except Exception:
            pass

    return AskNorthStarResponse(
        question="",
        intent="client_operations",
        summary=(
            f"From approved Client Operations for {client_name}: "
            + (" · ".join(summary_bits) if summary_bits else "See details below.")
        ),
        scope="",
        active_client_id=client_id,
        active_client_name=client_name,
        no_data=False,
        sections=sections,
        companies=[],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[
            AskSource(
                label="Client Knowledge Hub",
                detail="Approved Client Operations",
            ),
            *[
                AskSource(
                    label=_blank(it.get("source_document")) or "Source document",
                    detail=_blank(it.get("source_snippet"))[:180]
                    or _blank(it.get("title")),
                )
                for it in items
                if _blank(it.get("source_document")) or _blank(it.get("source_snippet"))
            ][:5],
        ],
        recommended_links=[
            AskLink(
                label="Open Client Knowledge",
                href=f"/clients/{client_id}/knowledge",
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=len(section_items),
    )


def _answer_work_next(
    *,
    user_id: int,
    preferred_client_id: int | None,
    visible_ids: list[int],
) -> AskNorthStarResponse:
    # All NorthStar: do not merge client queues — summarize per authorized client.
    if preferred_client_id is None:
        return _answer_work_next_all_clients(user_id=user_id, visible_ids=visible_ids)

    client_id = preferred_client_id if preferred_client_id in visible_ids else None
    if client_id is None:
        return AskNorthStarResponse(
            question="",
            intent="work_next",
            summary=(
                "Which client should Work Next use? Switch Search Scope to Active Client "
                "and pick a client, or name the client in your question."
            ),
            scope="",
            active_client_id=None,
            active_client_name="",
            no_data=False,
            sections=[
                AskSection(
                    id="need_working_for",
                    title="SELECT WORKING FOR CLIENT",
                    body="Work Next requires a specific client context.",
                    items=[],
                )
            ],
            companies=[],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[AskSource(label="NorthStar CRM", detail="Work Next clarification")],
            recommended_links=[],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
        )

    return _answer_work_next_for_client(user_id=user_id, client_id=client_id)


def _answer_work_next_for_client(*, user_id: int, client_id: int) -> AskNorthStarResponse:
    """Top 5 from existing Work Queue prioritization for one client."""
    wq = list_work_queue(user_id=user_id, client_id=client_id, work_type="work-next")
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        client_name = _blank(row["name"]) if row else ""

    total = len(wq.items)
    if total == 0:
        return _empty_answer(
            intent="work_next",
            summary=NO_DATA,
            detail=f"You're caught up — no prioritized Work Next items for {client_name or 'this client'} right now.",
        )

    top = wq.items[:5]
    companies = []
    for idx, item in enumerate(top, start=1):
        insight = item.northstar_insight
        companies.append(
            _company_card_from_row(
                company_id=item.company_id,
                company_name=item.company_name,
                external_record_no=item.external_record_no,
                client_id=item.client_id,
                client_name=item.client_name,
                status=item.status,
                city=item.city,
                state=item.state,
                badges=[item.work_type] if item.work_type else [],
                why=item.why_in_queue or "",
                score=item.opportunity_score,
                rank=idx,
                next_action=_blank(item.next_action),
                work_priority=item.work_priority,
                work_type=item.work_type,
                northstar_recommendation=(
                    insight.recommendation if insight else ""
                ),
                northstar_fit=insight.fit if insight else "",
                northstar_engagement=insight.engagement if insight else "",
                northstar_alignment=insight.alignment if insight else "",
                northstar_recommendation_why=insight.why if insight else "",
            )
        )

    queue_href = f"/work-queue?client_id={client_id}&type=work-next"
    return AskNorthStarResponse(
        question="",
        intent="work_next",
        summary=(
            f"Here are your top priorities for {client_name}. "
            f"NorthStar found {total} actionable item{'s' if total != 1 else ''} in the Work Queue."
        ),
        scope="",
        active_client_id=client_id,
        active_client_name=client_name,
        no_data=False,
        sections=[
            AskSection(
                id="work_next_top",
                title="TOP PRIORITIES",
                body=f"First {len(companies)} of {total} in Work Queue rank order for {client_name}.",
                items=[c.model_dump() for c in companies],
            )
        ],
        companies=companies,
        contacts=[],
        notes=[],
        milestones=[],
        sources=[
            AskSource(label="NorthStar CRM", detail="Work Queue prioritization"),
            AskSource(label=f"{client_name} History", detail="Active Client context"),
        ],
        recommended_links=[
            AskLink(
                label=f"View all {total} in Work Queue",
                href=queue_href,
            )
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=total,
    )


def _answer_work_next_all_clients(
    *, user_id: int, visible_ids: list[int]
) -> AskNorthStarResponse:
    """Cross-client Work Next summary — no mixed unexplained ranking."""
    if not visible_ids:
        return _empty_answer(
            intent="work_next",
            summary=NO_DATA,
            detail="No authorized clients available for Work Next.",
        )

    client_rows: list[dict[str, Any]] = []
    with get_connection() as conn:
        name_rows = conn.execute(
            f"""
            SELECT id, name FROM clients
            WHERE id IN ({_placeholders(visible_ids)})
            ORDER BY name COLLATE NOCASE
            """,
            visible_ids,
        ).fetchall()
        names = {int(r["id"]): _blank(r["name"]) for r in name_rows}

    for cid in sorted(visible_ids, key=lambda i: names.get(i, "").lower()):
        cname = names.get(cid, f"Client {cid}")
        wq = list_work_queue(user_id=user_id, client_id=cid, work_type="work-next")
        count = len(wq.items)
        if count <= 0:
            continue
        top = wq.items[0]
        client_rows.append(
            {
                "client_id": cid,
                "client_name": cname,
                "actionable_count": count,
                "top_company": top.company_name,
                "top_work_type": top.work_type,
                "top_status": top.status,
                "work_queue_href": f"/work-queue?client_id={cid}&type=work-next",
            }
        )

    if not client_rows:
        return _empty_answer(
            intent="work_next",
            summary=NO_DATA,
            detail="You're caught up — no prioritized Work Next items across your authorized clients.",
        )

    total_across = sum(int(r["actionable_count"]) for r in client_rows)
    summary = (
        f"Across your authorized clients, NorthStar found actionable Work Queue items for "
        f"{len(client_rows)} client{'s' if len(client_rows) != 1 else ''} "
        f"({total_across} total). Pick a client to see ranked priorities — "
        "queues are not mixed into one list."
    )
    return AskNorthStarResponse(
        question="",
        intent="work_next",
        summary=summary,
        scope="",
        active_client_id=None,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="work_next_by_client",
                title="WORK NEXT BY CLIENT",
                body=(
                    "Choose a client below (or switch Search Scope to Active Client) "
                    "to see that client's Top 5 in Work Queue order."
                ),
                items=client_rows,
            )
        ],
        companies=[],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[
            AskSource(label="NorthStar CRM", detail="Work Queue prioritization by client"),
        ],
        recommended_links=[
            AskLink(
                label=f"Open Work Queue · {r['client_name']} ({r['actionable_count']})",
                href=str(r["work_queue_href"]),
            )
            for r in client_rows
        ],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=total_across,
    )


def _answer_general_search(
    *,
    user_id: int,
    q: str,
    preferred_client_id: int | None,
    visible_ids: list[int],
) -> AskNorthStarResponse:
    # Try company intelligence first if a company name matches strongly
    with get_connection() as conn:
        matches = _find_companies_by_name(conn, q.rstrip("?"), visible_ids=visible_ids, limit=3)
        if matches and len(_blank(q).split()) <= 6:
            return _answer_company_intelligence(
                conn,
                company_name_query=_blank(matches[0]["company_name"]),
                visible_ids=visible_ids,
                preferred_client_id=preferred_client_id,
            )

    search = run_search(
        q,
        user_id=user_id,
        client_id=preferred_client_id,
        all_clients=preferred_client_id is None,
        limit=25,
    )
    companies = [
        _company_card_from_row(
            company_id=h.company_id or 0,
            company_name=h.company_name or h.title,
            external_record_no=h.external_record_no,
            client_id=h.client_id,
            client_name=h.client_name,
            why=_strip_html(h.snippet or ""),
        )
        for h in search.companies
        if h.company_id and h.external_record_no
    ]
    notes = [
        AskNoteHit(
            company_id=h.company_id,
            company_name=h.company_name or h.title,
            external_record_no=h.external_record_no,
            client_id=h.client_id,
            client_name=h.client_name,
            excerpt=_strip_html(h.snippet or h.title),
            event_at=h.event_at,
            source="NorthStar CRM",
            doc_type=h.doc_type,
            workspace_path=(
                f"/companies/{h.external_record_no}?client_id={h.client_id}"
                if h.external_record_no and h.client_id
                else ""
            ),
        )
        for h in search.notes
    ]
    if not companies and not notes and not search.contacts:
        lower = q.lower()
        if any(
            h in lower
            for h in (
                "before i call",
                "call brief",
                "prepare me to call",
                "brief me on",
                "before calling",
            )
        ):
            return AskNorthStarResponse(
                question="",
                intent="general_search",
                summary=(
                    "I understood this as a call-prep question, but could not resolve a company "
                    "from that phrasing. Try: "
                    '“What should I know before I call [Company] for [Client]?”'
                ),
                scope="",
                active_client_id=preferred_client_id,
                active_client_name="",
                no_data=False,
                sections=[
                    AskSection(
                        id="clarify",
                        title="TRY A CLEARER CALL PREP QUESTION",
                        body="Example: What should I know before I call Trailerman for Brown?",
                        items=[],
                    )
                ],
                companies=[],
                contacts=[],
                notes=[],
                milestones=[],
                sources=[AskSource(label="NorthStar CRM", detail="Clarification")],
                recommended_links=[],
                research_options=list(RESEARCH_OPTIONS),
                research_available=False,
            )
        return _empty_answer(
            intent="general_search",
            summary=NO_DATA,
            detail="Try a company name, status question, note topic, call brief, or Work Queue question.",
        )

    contacts = [
        AskContactCard(
            contact_id=h.contact_id,
            name=h.contact_name or h.title,
            title="",
            phone="",
            email="",
            client_id=h.client_id,
            client_name=h.client_name,
            source=f"{h.client_name} History" if h.client_name else "NorthStar CRM",
        )
        for h in search.contacts
    ]

    return AskNorthStarResponse(
        question="",
        intent="general_search",
        summary=f"Search found {search.total} authorized result{'s' if search.total != 1 else ''} in NorthStar.",
        scope="",
        active_client_id=preferred_client_id,
        active_client_name="",
        no_data=False,
        sections=[
            AskSection(
                id="search",
                title="SEARCH RESULTS",
                body="Retrieved from the NorthStar full-text index (authorized clients only).",
                items=[],
            )
        ],
        companies=companies,
        contacts=contacts,
        notes=notes,
        milestones=[],
        sources=[AskSource(label="NorthStar CRM", detail="search_fts")],
        recommended_links=[],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=search.total,
    )


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


def _excerpt_around(text: str, topic: str, radius: int = 140) -> str:
    lower = text.lower()
    idx = lower.find(topic.lower())
    if idx < 0:
        return text[: radius * 2] + ("…" if len(text) > radius * 2 else "")
    start = max(0, idx - radius)
    end = min(len(text), idx + len(topic) + radius)
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt = excerpt + "…"
    return excerpt


def _empty_answer(*, intent: str, summary: str, detail: str = "") -> AskNorthStarResponse:
    return AskNorthStarResponse(
        question="",
        intent=intent,
        summary=summary,
        scope="",
        active_client_id=None,
        active_client_name="",
        no_data=True,
        sections=[
            AskSection(
                id="empty",
                title="NO MATCHING NORTHSTAR DATA",
                body=detail or summary,
                items=[],
            )
        ],
        companies=[],
        contacts=[],
        notes=[],
        milestones=[],
        sources=[AskSource(label="NorthStar CRM", detail="Read-only retrieval")],
        recommended_links=[],
        research_options=list(RESEARCH_OPTIONS),
        research_available=False,
        result_count=0,
    )


def _save_history(
    *,
    user_id: int,
    question: str,
    scope: str,
    active_client_id: int | None,
    intent: str,
    answer_summary: str,
) -> int | None:
    ensure_ask_northstar_schema()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO ask_northstar_history (
                user_id, question, scope, active_client_id, intent, answer_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                question,
                scope,
                active_client_id,
                intent,
                answer_summary[:500],
                _now_iso(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_ask_history(
    *,
    limit: int = 12,
    scope: str | None = None,
    active_client_id: int | None = None,
) -> AskHistoryResponse:
    """Return recent Ask history for the current user.

    When scope=active_client and active_client_id is set, return only rows whose
    *resolved* Working For client (stored active_client_id) matches — not text match.
    When scope=all (or unset filter), return authorized cross-client history.
    Never deletes or rewrites stored rows.
    """
    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")
    ensure_ask_northstar_schema()

    scope_norm = _blank(scope).lower()
    filter_client_id: int | None = None
    filter_client_name = ""
    filter_scope_label = ""

    if scope_norm == "active_client":
        if active_client_id is None or active_client_id <= 0:
            return AskHistoryResponse(
                items=[],
                count=0,
                filter_scope="active_client",
                filter_client_id=None,
                filter_client_name="",
            )
        filter_client_id = int(active_client_id)
        from access import user_can_access_client

        if not user_can_access_client(user.id, filter_client_id) and not user.is_administrator:
            raise PermissionError("Not authorized for this client.")
        filter_scope_label = "active_client"
    else:
        filter_scope_label = "all"

    with get_connection() as conn:
        if filter_client_id is not None:
            row = conn.execute(
                "SELECT name FROM clients WHERE id = ?",
                (filter_client_id,),
            ).fetchone()
            filter_client_name = _blank(row["name"]) if row else ""
            rows = conn.execute(
                """
                SELECT h.id, h.question, h.scope, h.active_client_id, h.intent,
                       h.answer_summary, h.created_at, cl.name AS active_client_name
                FROM ask_northstar_history h
                LEFT JOIN clients cl ON cl.id = h.active_client_id
                WHERE h.user_id = ?
                  AND h.active_client_id = ?
                ORDER BY datetime(h.created_at) DESC, h.id DESC
                LIMIT ?
                """,
                (user.id, filter_client_id, max(1, min(limit, 50))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT h.id, h.question, h.scope, h.active_client_id, h.intent,
                       h.answer_summary, h.created_at, cl.name AS active_client_name
                FROM ask_northstar_history h
                LEFT JOIN clients cl ON cl.id = h.active_client_id
                WHERE h.user_id = ?
                ORDER BY datetime(h.created_at) DESC, h.id DESC
                LIMIT ?
                """,
                (user.id, max(1, min(limit, 50))),
            ).fetchall()

    items = [
        AskHistoryItem(
            id=int(r["id"]),
            question=_blank(r["question"]),
            scope=_blank(r["scope"]),
            active_client_id=(
                int(r["active_client_id"]) if r["active_client_id"] is not None else None
            ),
            active_client_name=_blank(r["active_client_name"]),
            intent=_blank(r["intent"]),
            answer_summary=_blank(r["answer_summary"]),
            created_at=_blank(r["created_at"]),
        )
        for r in rows
    ]
    return AskHistoryResponse(
        items=items,
        count=len(items),
        filter_scope=filter_scope_label,
        filter_client_id=filter_client_id,
        filter_client_name=filter_client_name,
    )


def ask_northstar(body: AskNorthStarRequest) -> AskNorthStarResponse:
    """Phase 1 entrypoint — read-only retrieval, never mutates CRM data."""
    user = resolve_staff_actor()
    if user is None:
        raise PermissionError("User not found.")

    question = _blank(body.question)
    if not question:
        raise ValueError("Question is required.")

    scope: ScopeMode = "active_client" if body.scope == "active_client" else "all"
    try:
        work_ids, intel_ids, scope_label, active_resolved = _resolve_scope_client_ids(
            user.id,
            scope=scope,
            active_client_id=body.active_client_id,
        )
    except ValueError as exc:
        return AskNorthStarResponse(
            question=question,
            intent="scope_required",
            summary=str(exc),
            scope=scope,
            scope_label="Active Client",
            active_client_id=None,
            active_client_name="",
            no_data=False,
            read_only=True,
            sections=[
                AskSection(
                    id="need_active_client",
                    title="SELECT A CLIENT",
                    body=(
                        "Active Client scope needs a specific Working For client. "
                        "Choose one in the Ask NorthStar scope control, or switch the sidebar "
                        "Active Client away from All My Clients."
                    ),
                    items=[],
                )
            ],
            companies=[],
            contacts=[],
            notes=[],
            milestones=[],
            sources=[AskSource(label="NorthStar CRM", detail="Scope clarification")],
            recommended_links=[],
            research_options=list(RESEARCH_OPTIONS),
            research_available=False,
        )

    if not work_ids and not intel_ids:
        raise PermissionError("No authorized clients available for Ask NorthStar.")

    include_archived = bool(getattr(body, "include_archived", False)) and bool(
        getattr(user, "is_administrator", False)
    )
    archived_token = _ASK_INCLUDE_ARCHIVED.set(include_archived)
    try:
        return _ask_northstar_dispatch(
            body=body,
            user=user,
            question=question,
            scope=scope,
            work_ids=work_ids,
            intel_ids=intel_ids,
            active_resolved=active_resolved,
            scope_label=scope_label,
        )
    finally:
        _ASK_INCLUDE_ARCHIVED.reset(archived_token)


def _ask_northstar_dispatch(
    *,
    body: AskNorthStarRequest,
    user,
    question: str,
    scope: ScopeMode,
    work_ids: list[int],
    intel_ids: list[int],
    active_resolved: int | None,
    scope_label: str,
) -> AskNorthStarResponse:
    # Active Client scope: preferred = that client.
    # All NorthStar: do NOT inherit sidebar client — only explicit question text may set Working For.
    preferred_client_id = active_resolved if scope == "active_client" else None

    active_name = ""
    if preferred_client_id:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT name FROM clients WHERE id = ?",
                (preferred_client_id,),
            ).fetchone()
            active_name = _blank(row["name"]) if row else ""

    intent, params = _classify_intent(question)

    # Work-scoped vs intelligence-scoped retrieval
    work_visible = work_ids
    intel_visible = intel_ids

    with get_connection() as conn:
        if intent == "call_prep":
            answer = _answer_call_prep(
                conn,
                user_id=user.id,
                company_name_query=params.get("company"),
                target_client_query=params.get("target_client"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
                question=question,
            )
        elif intent == "client_operations":
            answer = _answer_client_operations(
                user_id=user.id,
                client_query=params.get("client"),
                topic=str(params.get("topic") or "general"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "email_templates":
            answer = _answer_email_templates(
                user_id=user.id,
                client_query=params.get("client"),
                topic=str(params.get("topic") or "general"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "client_strategy_knowledge":
            answer = _answer_client_strategy_knowledge(
                user_id=user.id,
                client_query=params.get("client"),
                topic=str(params.get("topic") or "general"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "client_contacts":
            answer = _answer_client_contacts(
                user_id=user.id,
                client_query=params.get("client"),
                person_query=params.get("person"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "company_intelligence":
            answer = _answer_company_intelligence(
                conn,
                company_name_query=params.get("company"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "cross_client_why":
            answer = _answer_cross_client_why(
                user_id=user.id,
                company_query=params.get("company"),
                target_query=params.get("target_client"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "needs_next_action":
            answer = _answer_needs_next_action(
                client_query=params.get("client"),
                visible_ids=work_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "status_list":
            answer = _answer_status_list(
                client_query=params.get("client"),
                status=params.get("status"),
                visible_ids=work_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "note_search":
            answer = _answer_note_search(
                user_id=user.id,
                topic=params.get("topic"),
                visible_ids=work_visible if scope == "active_client" else intel_visible,
                preferred_client_id=preferred_client_id if scope == "active_client" else None,
            )
        elif intent == "milestone_list":
            answer = _answer_milestone_list(
                milestone_type=params["milestone"],
                visible_ids=work_visible if scope == "active_client" else intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "appointments_without_quote":
            answer = _answer_appointments_without_quote(
                visible_ids=work_visible if scope == "active_client" else intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "sales_events":
            answer = _answer_sales_events(
                visible_ids=work_visible if scope == "active_client" else intel_visible,
                preferred_client_id=preferred_client_id,
                rev_spec=params.get("rev_spec"),
                outcome=params.get("outcome"),
                company=params.get("company"),
                contact=params.get("contact"),
                appointment_only=bool(params.get("appointment_only", True)),
                event_family=params.get("event_family"),
                missing_event_types=params.get("missing_event_types"),
            )
        elif intent == "contact_history":
            answer = _answer_contact_history(
                contact_query=params.get("contact"),
                visible_ids=work_visible if scope == "active_client" else intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "cross_client_list":
            answer = _answer_cross_client_list(
                user_id=user.id,
                client_query=params.get("client"),
                visible_ids=intel_visible,
                preferred_client_id=preferred_client_id,
            )
        elif intent == "work_next":
            answer = _answer_work_next(
                user_id=user.id,
                preferred_client_id=preferred_client_id,
                visible_ids=work_visible,
            )
        else:
            answer = _answer_general_search(
                user_id=user.id,
                q=question,
                preferred_client_id=preferred_client_id if scope == "active_client" else None,
                visible_ids=work_visible if scope == "active_client" else intel_visible,
            )

    answer.question = question
    answer.scope = scope
    answer.scope_label = scope_label
    # Prefer resolved Working For from this answer (call brief) over scope preferred.
    # Never leave a prior question's client on the response.
    hdr = next((s for s in answer.sections if s.id == "call_brief_header"), None)
    hdr_item = hdr.items[0] if hdr and hdr.items else None
    resolved_wf_id = None
    resolved_wf_name = ""
    if isinstance(hdr_item, dict):
        raw_id = hdr_item.get("working_for_client_id")
        if raw_id is not None and str(raw_id).strip():
            try:
                resolved_wf_id = int(raw_id)
            except (TypeError, ValueError):
                resolved_wf_id = None
        resolved_wf_name = _blank(hdr_item.get("working_for"))
    if resolved_wf_id and resolved_wf_id > 0:
        answer.active_client_id = resolved_wf_id
        answer.active_client_name = resolved_wf_name or active_name
    else:
        answer.active_client_id = preferred_client_id
        answer.active_client_name = active_name if scope == "active_client" else ""
    answer.read_only = True
    answer.research_options = list(RESEARCH_OPTIONS)
    answer.research_available = bool(answer.companies) or any(
        s.id == "call_brief_header" for s in answer.sections
    )

    history_id = _save_history(
        user_id=user.id,
        question=question,
        scope=scope,
        active_client_id=answer.active_client_id,
        intent=answer.intent,
        answer_summary=answer.summary,
    )
    answer.history_id = history_id
    return answer
