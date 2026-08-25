"""Sales-event intelligence verification — read-only Ask / Research / counts."""
from __future__ import annotations

import json

from ask_northstar_data import ask_northstar
from db import get_connection
from models import AskNorthStarRequest
from research_data import start_company_research
from models import ResearchStartRequest


def counts():
    with get_connection() as conn:
        return {
            "companies": conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "activities": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "sales_events": conn.execute("SELECT COUNT(*) FROM client_sales_events").fetchone()[0],
            "campaigns": conn.execute("SELECT COUNT(*) FROM client_campaigns").fetchone()[0],
        }


def ask(q: str):
    resp = ask_northstar(
        AskNorthStarRequest(question=q, scope="active_client", active_client_id=1)
    )
    return {
        "intent": resp.intent,
        "summary": resp.summary[:500],
        "sections": [s.id for s in resp.sections],
        "result_count": resp.result_count,
        "companies": len(resp.companies),
        "sample": (resp.sections[0].items[:3] if resp.sections and resp.sections[0].items else []),
        "links": [l.label for l in (resp.recommended_links or [])],
    }


def main():
    before = counts()
    print("BEFORE", json.dumps(before))

    tests = [
        "What happened with AAON?",
        "What do we know about AAON?",
        "Prepare me to call AAON for Carmeco.",
        "What happened with Jimmy Davis?",
        "Show me Jimmy Davis's appointment history.",
        "Show me Carmeco companies with RFQs.",
        "Show me Carmeco appointments.",
        "What appointments did Tyler set?",
        "Find appointment notes mentioning drawings.",
    ]
    for q in tests:
        try:
            out = ask(q)
            print("ASK", json.dumps({"q": q, **{k: out[k] for k in out if k != "sample"}}))
            if out["sample"]:
                print("  SAMPLE", json.dumps(out["sample"], default=str)[:800])
        except Exception as exc:
            print("ASK_FAIL", q, type(exc).__name__, str(exc)[:300])

    # Research engagement for AAON
    try:
        res = start_company_research(
            ResearchStartRequest(
                external_record_no="1195125",
                working_for_client_id=1,
                force_refresh=False,
            )
        )
        eng = res.engagement
        fit = res.fit
        print(
            "RESEARCH",
            json.dumps(
                {
                    "company": res.company_name,
                    "engagement_level": eng.level if eng else None,
                    "engagement_signals": eng.signals if eng else None,
                    "engagement_why": (eng.why[:240] if eng else None),
                    "fit_result": fit.fit_result if fit else None,
                    "sales_events_known": len(res.northstar_known.sales_events)
                    if res.northstar_known
                    else 0,
                },
                default=str,
            ),
        )
    except Exception as exc:
        print("RESEARCH_FAIL", type(exc).__name__, str(exc)[:400])

    # RFQ unique companies
    with get_connection() as conn:
        rfq = conn.execute(
            """
            SELECT COUNT(*) AS events,
                   COUNT(DISTINCT company_id) AS companies
            FROM client_sales_events
            WHERE client_id=1 AND event_type='RFQ'
            """
        ).fetchone()
        drawings = conn.execute(
            """
            SELECT company_name, event_type, contact_name
            FROM client_sales_events
            WHERE client_id=1
              AND (
                lower(caller_notes) LIKE '%drawing%'
                OR lower(sales_notes) LIKE '%drawing%'
              )
            """
        ).fetchall()
        print("RFQ", dict(rfq))
        print("DRAWINGS", [dict(r) for r in drawings])

    after = counts()
    print("AFTER", json.dumps(after))
    print("UNCHANGED", before == after)


if __name__ == "__main__":
    main()
