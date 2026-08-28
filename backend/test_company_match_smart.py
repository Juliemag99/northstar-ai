"""Smart company duplicate matching: compact keys, fuzzy scores, AI fallback.

Run: python test_company_match_smart.py

Isolated testdb only. Does not modify Flora Jia, Whirlpool, or live A. O. Smith.
"""

from __future__ import annotations

import testdb
import sys
import time
from pathlib import Path

from company_match import compact_company_name, names_match, score_company_pair
from db import get_connection

FLORA_ID = 4631
WHIRLPOOL_ID = 298
AO_SMITH_ID = 289
MARKER = "NSMCS"
CARMECO_ID = 1
BROWN_ID = 2
REPO = Path(__file__).resolve().parent.parent


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _reject_ok(status: int, payload: dict, label: str) -> None:
    if status not in {400, 422}:
        _fail(f"{label}: expected HTTP 400 or 422, got {status}: {payload}")


def _preview(client_id: int, **fields: object) -> dict:
    status, payload = testdb.http_json(
        "POST", "/api/companies/manual/preview", {"client_id": client_id, **fields}
    )
    if status != 200:
        _fail(f"preview failed ({status}): {payload}")
    return payload


def _match_ids(preview: dict) -> set[int]:
    return {int(m["company_id"]) for m in (preview.get("matches") or [])}


def _cleanup() -> None:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        for row in rows:
            cid = int(row["id"])
            conn.execute("DELETE FROM activities WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM client_company_relationships WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.commit()


def _insert_clone(conn, *, name: str, record_no: str, website: str, phone: str) -> int:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, address, city, state, zip, website,
            legacy_phone, created_at, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_no,
            name,
            "500 Tennessee Waltz Pkwy",
            "Ashland City",
            "TN",
            "37015",
            website,
            phone,
            now,
            now,
        ),
    )
    company_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO client_company_relationships (client_id, company_id, external_record_no, status)
        VALUES (?, ?, ?, 'New')
        """,
        (CARMECO_ID, company_id, record_no),
    )
    return company_id


def test_normalization() -> None:
    if compact_company_name("AO Smith") != compact_company_name("A. O. Smith"):
        _fail("AO Smith did not compact to A. O. Smith.")
    if not names_match("A O Smith", "A.O. Smith"):
        _fail("A O Smith did not match A.O. Smith.")
    if not names_match("A. O. Smith Corporation", "AO Smith Corp"):
        _fail("Corporate suffix variation did not match.")
    if not names_match("Smith AO", "A. O. Smith"):
        _fail("Reordered tokens did not match.")
    score, reasons = score_company_pair(
        {"company_name": "AO Smith"},
        {"company_name": "A. O. Smith", "website": "", "address": "", "city": "", "state": "", "phone": "", "external_record_no": ""},
    )
    if score < 80 or "compact_name" not in reasons:
        _fail(f"Compact-name score too low: {score} {reasons}")


def test_frontend_contract() -> None:
    api = (REPO / "frontend" / "src" / "api" / "carmeco.ts").read_text(encoding="utf-8")
    if "fetch('/api/companies/manual/preview'" not in api.replace('"', "'"):
        if 'fetch("/api/companies/manual/preview"' not in api:
            _fail("Frontend preview does not POST /api/companies/manual/preview.")
    if "client_id: writeClientId" not in api:
        _fail("Frontend preview does not send requireWriteClientId client_id.")
    modal = (REPO / "frontend" / "src" / "AddCompanyModal.tsx").read_text(encoding="utf-8")
    if "Match confidence" not in modal or "Match reasons" not in modal:
        _fail("Modal does not display match confidence and reasons.")
    if "AI:" not in modal:
        _fail("Modal does not display AI assessment when available.")


def main() -> None:
    stamp = str(int(time.time()))
    test_normalization()
    test_frontend_contract()
    with get_connection() as conn:
        flora_before = conn.execute(
            "SELECT id, first_name, last_name FROM contacts WHERE id = ?", (FLORA_ID,)
        ).fetchone()
        whirl_before = conn.execute(
            "SELECT id, company_name FROM companies WHERE id = ?", (WHIRLPOOL_ID,)
        ).fetchone()
        ao_before = conn.execute(
            """
            SELECT id, company_name, website, address, city, state, zip, legacy_phone, external_record_no
            FROM companies WHERE id = ?
            """,
            (AO_SMITH_ID,),
        ).fetchone()
        companies_before = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])

    try:
        missing = testdb.http_json("POST", "/api/companies/manual/preview", {"company_name": "AO Smith"})
        _reject_ok(missing[0], missing[1], "preview missing client_id")

        ao_preview = _preview(CARMECO_ID, company_name="AO Smith")
        if AO_SMITH_ID not in _match_ids(ao_preview):
            _fail(f"AO Smith did not match live-shaped A. O. Smith id {AO_SMITH_ID}: {ao_preview}")
        if ao_preview.get("can_create") is not False:
            _fail("Ordinary create was allowed while A. O. Smith was a candidate.")
        if ao_preview.get("message") == "No likely duplicates found":
            _fail("UI no-match copy was returned while a strong candidate exists.")
        hit = next(m for m in ao_preview["matches"] if int(m["company_id"]) == AO_SMITH_ID)
        if int(hit.get("score") or 0) < 80:
            _fail(f"A. O. Smith score was not high: {hit}")
        if "compact_name" not in (hit.get("reasons") or []):
            _fail(f"A. O. Smith reasons missing compact_name: {hit.get('reasons')}")
        if ao_preview.get("ai_available"):
            _fail("AI was marked available when no AI key is configured.")
        if hit.get("ai_assessment"):
            _fail("AI assessment was populated without a configured AI service.")

        suffix = _preview(CARMECO_ID, company_name="A. O. Smith Corporation")
        if AO_SMITH_ID not in _match_ids(suffix):
            _fail("A. O. Smith Corporation did not match A. O. Smith.")

        reordered = _preview(CARMECO_ID, company_name="Smith AO")
        if AO_SMITH_ID not in _match_ids(reordered):
            _fail("Reordered tokens Smith AO did not match A. O. Smith.")

        misspelled = _preview(CARMECO_ID, company_name="AO Smiith")
        if AO_SMITH_ID not in _match_ids(misspelled):
            _fail("Misspelling AO Smiith did not match A. O. Smith.")

        with get_connection() as conn:
            clone_id = _insert_clone(
                conn,
                name=f"{MARKER} A. O. Widget {stamp}",
                record_no=f"{MARKER}-AO-{stamp}",
                website=f"https://nsmcs-{stamp}.example.com",
                phone="(414)555-0199",
            )
            other_id = _insert_clone(
                conn,
                name=f"{MARKER} Johnson Controls {stamp}",
                record_no=f"{MARKER}-JC-{stamp}",
                website=f"https://johnson-{stamp}.example.com",
                phone="(312)555-0100",
            )
            conn.commit()

        domain_preview = _preview(
            BROWN_ID,
            company_name=f"{MARKER} Different Name {stamp}",
            website=f"https://nsmcs-{stamp}.example.com",
        )
        if clone_id not in _match_ids(domain_preview):
            _fail("Same domain with a different name was not detected.")

        phone_preview = _preview(
            BROWN_ID,
            company_name=f"{MARKER} Phone Format {stamp}",
            phone="414-555-0199",
        )
        if clone_id not in _match_ids(phone_preview):
            _fail("Same phone with different formatting was not detected.")

        different = _preview(CARMECO_ID, company_name=f"{MARKER} AO Widget {stamp}")
        if other_id in _match_ids(different):
            _fail("Genuinely different Johnson Controls company was returned as a match.")
        if clone_id not in _match_ids(different):
            _fail("AO Widget did not match the A. O. Widget structural clone.")

        blocked = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {"client_id": BROWN_ID, "action": "create", "company_name": "AO Smith"},
        )
        _reject_ok(blocked[0], blocked[1], "create AO Smith without confirmation")

        import company_match_ai

        original = company_match_ai.review_duplicate_candidates
        wrote = {"n": 0}

        def _fake_ai(query, shortlist):
            with get_connection() as conn:
                conn.execute(
                    "SELECT COUNT(*) AS n FROM companies"
                )
            wrote["n"] += 1
            for row in shortlist:
                row["ai_available"] = True
                row["ai_assessment"] = "Likely same company"
                row["ai_explanation"] = "Normalized names match."
                row["ai_confidence"] = "high"

        company_match_ai.review_duplicate_candidates = _fake_ai
        try:
            with get_connection() as conn:
                before_n = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
            ai_preview = _preview(CARMECO_ID, company_name="AO Smith")
            with get_connection() as conn:
                after_n = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
            if after_n != before_n:
                _fail("AI classification wrote a company row.")
            ai_hit = next(m for m in ai_preview["matches"] if int(m["company_id"]) == AO_SMITH_ID)
            if ai_hit.get("ai_assessment") != "Likely same company":
                _fail("Patched AI assessment was not returned.")
        finally:
            company_match_ai.review_duplicate_candidates = original

        # Create protection must work with AI unavailable (default).
        confirmed = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "create",
                "company_name": f"{MARKER} A.O. Widget {stamp}",
                "website": f"https://widget-extra-{stamp}.{MARKER.lower()}.example",
                "confirm_create_despite_match": True,
            },
        )
        if confirmed[0] != 200:
            _fail(f"Create New Anyway failed ({confirmed[0]}): {confirmed[1]}")

        with get_connection() as conn:
            if flora_before is not None:
                flora_after = conn.execute(
                    "SELECT id, first_name, last_name FROM contacts WHERE id = ?", (FLORA_ID,)
                ).fetchone()
                if dict(flora_after) != dict(flora_before):
                    _fail("Flora Jia identity changed.")
            if whirl_before is not None:
                whirl_after = conn.execute(
                    "SELECT id, company_name FROM companies WHERE id = ?", (WHIRLPOOL_ID,)
                ).fetchone()
                if dict(whirl_after) != dict(whirl_before):
                    _fail("Whirlpool identity changed.")
            if ao_before is not None:
                ao_after = conn.execute(
                    """
                    SELECT id, company_name, website, address, city, state, zip, legacy_phone, external_record_no
                    FROM companies WHERE id = ?
                    """,
                    (AO_SMITH_ID,),
                ).fetchone()
                if dict(ao_after) != dict(ao_before):
                    _fail("Live A. O. Smith identity changed.")
            if int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]) < companies_before:
                _fail("Companies were lost.")

        print("test_company_match_smart: ok")
    finally:
        _cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_company_match_smart: FAIL: {exc}", file=sys.stderr)
        raise
