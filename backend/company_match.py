"""Deterministic + fuzzy company duplicate matching.

Does not load the full companies table into Python. Candidates are gathered with
bounded SQL, then scored. Compact-name equality does not depend on substring LIMIT.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from import_brown_industries import digits_phone, domain, norm_addr

STRONG_SCORE = 80
POSSIBLE_SCORE = 58
MAX_RETURN = 8
MAX_CANDIDATES = 160

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LEGAL_STOP = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "co",
        "company",
        "the",
        "of",
        "incorporated",
        "limited",
        "plc",
        "lp",
        "llp",
        "group",
        "holdings",
        "international",
        "industries",
    }
)

# SQL compact key: lowercase, drop punctuation/spaces. Suffixes are stripped in Python.
_SQL_RAW_ALNUM = """
replace(replace(replace(replace(replace(replace(lower(trim(company_name)), '.', ''), ' ', ''), ',', ''), '-', ''), '''', ''), '&', '')
"""


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def lowercase_normalized(value: str) -> str:
    return _blank(value).lower()


def punctuation_stripped(value: str) -> str:
    return _NON_ALNUM.sub(" ", lowercase_normalized(value))


def whitespace_normalized(value: str) -> str:
    return " ".join(punctuation_stripped(value).split())


def compact_company_name(value: str) -> str:
    """'AO Smith', 'A. O. Smith', and 'AO Smith Corp' become 'ao smith'."""
    raw = [t for t in whitespace_normalized(value).split() if t and t not in _LEGAL_STOP]
    out: list[str] = []
    initials = ""
    for token in raw:
        if len(token) == 1:
            initials += token
            continue
        if initials:
            out.append(initials)
            initials = ""
        out.append(token)
    if initials:
        out.append(initials)
    return " ".join(out)


def compact_company_alnum(value: str) -> str:
    return compact_company_name(value).replace(" ", "")


def token_set(value: str) -> frozenset[str]:
    return frozenset(compact_company_name(value).split())


def names_match(left: str, right: str) -> bool:
    compact_left = compact_company_name(left)
    compact_right = compact_company_name(right)
    if compact_left and compact_right and compact_left == compact_right:
        return True
    if token_set(left) and token_set(left) == token_set(right):
        return True
    alnum_left = compact_company_alnum(left)
    alnum_right = compact_company_alnum(right)
    return bool(alnum_left and alnum_right and len(alnum_left) >= 5 and alnum_left == alnum_right)


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _token_similarity(left: str, right: str) -> float:
    a, b = token_set(left), token_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _already_assigned(conn, company_id: int, client_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM client_company_relationships
        WHERE company_id = ? AND client_id = ?
        LIMIT 1
        """,
        (company_id, client_id),
    ).fetchone()
    return row is not None


def _client_relationships(conn, company_id: int) -> list[dict[str, Any]]:
    return [
        {
            "client_id": int(r["client_id"]),
            "client_name": _blank(r["client_name"]),
            "status": _blank(r["status"]),
        }
        for r in conn.execute(
            """
            SELECT cl.id AS client_id, cl.name AS client_name, COALESCE(ccr.status, '') AS status
            FROM client_company_relationships ccr
            JOIN clients cl ON cl.id = ccr.client_id
            WHERE ccr.company_id = ?
            ORDER BY cl.name, cl.id
            """,
            (company_id,),
        ).fetchall()
    ]


SELECT_COLS = (
    "id, company_name, external_record_no, website, address, city, state, zip, legacy_phone, "
    "TRIM(COALESCE(archived_at,'')) AS archived_at"
)


def _pack_row(row) -> dict[str, Any]:
    return {
        "company_id": int(row["id"]),
        "company_name": _blank(row["company_name"]),
        "external_record_no": _blank(row["external_record_no"]),
        "website": _blank(row["website"]),
        "address": _blank(row["address"]),
        "city": _blank(row["city"]),
        "state": _blank(row["state"]),
        "zip": _blank(row["zip"]),
        "phone": _blank(row["legacy_phone"]),
        "archived": bool(_blank(row["archived_at"]) if "archived_at" in row.keys() else ""),
    }


def score_company_pair(query: dict[str, str], candidate: dict[str, Any]) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons). Exact identity keys rank very highly."""
    reasons: list[str] = []
    score = 0
    q_name = query.get("company_name") or ""
    c_name = candidate.get("company_name") or ""

    q_rn = _blank(query.get("external_record_no"))
    c_rn = _blank(candidate.get("external_record_no"))
    if q_rn and c_rn and q_rn == c_rn:
        reasons.append("record_no")
        score = max(score, 100)

    q_dom = domain(_blank(query.get("website")))
    c_dom = domain(_blank(candidate.get("website")))
    if q_dom and c_dom and q_dom == c_dom:
        reasons.append("domain")
        score = max(score, 95)

    q_phone = digits_phone(_blank(query.get("phone")))
    c_phone = digits_phone(_blank(candidate.get("phone")))
    if q_phone and c_phone and len(q_phone) >= 7 and (
        q_phone == c_phone or q_phone[-7:] == c_phone[-7:]
    ):
        reasons.append("phone")
        score = max(score, 95)

    if names_match(q_name, c_name):
        reasons.append("compact_name")
        score = max(score, 100)
    else:
        tokens = _token_similarity(q_name, c_name)
        fuzzy = _ratio(compact_company_alnum(q_name), compact_company_alnum(c_name))
        name_score = int(round(max(tokens * 88, fuzzy * 92)))
        if tokens >= 0.99:
            reasons.append("token_set")
            score = max(score, 96)
        elif tokens >= 0.66:
            reasons.append("token_similarity")
            score = max(score, name_score)
        if fuzzy >= 0.84:
            reasons.append("fuzzy_name")
            score = max(score, name_score)

    q_addr = norm_addr(_blank(query.get("address")))
    c_addr = norm_addr(_blank(candidate.get("address")))
    q_city = _blank(query.get("city")).lower()
    c_city = _blank(candidate.get("city")).lower()
    q_state = _blank(query.get("state")).upper()
    c_state = _blank(candidate.get("state")).upper()
    if q_addr and c_addr and q_addr == c_addr:
        reasons.append("address")
        score = max(score, 88 if not (q_city and c_city and q_city == c_city) else 92)
    elif q_addr and c_addr and _ratio(q_addr, c_addr) >= 0.86:
        reasons.append("address_similar")
        score = max(score, 72)
    if q_city and c_city and q_state and c_state and q_city == c_city and q_state == c_state:
        if "address" in reasons or "address_similar" in reasons or "compact_name" in reasons:
            reasons.append("city_state")
            score = min(100, score + 4)

    unique = list(dict.fromkeys(reasons))
    return min(100, score), unique


def _add_candidate(bucket: dict[int, dict[str, Any]], row) -> None:
    packed = _pack_row(row)
    bucket[int(packed["company_id"])] = packed


def gather_candidates(
    conn,
    *,
    company_name: str,
    website: str = "",
    address: str = "",
    city: str = "",
    state: str = "",
    phone: str = "",
    external_record_no: str = "",
) -> list[dict[str, Any]]:
    """Bounded SQL candidate gather. Scores in Python; does not rely only on LIKE."""
    bucket: dict[int, dict[str, Any]] = {}
    cols = SELECT_COLS
    rn = _blank(external_record_no)
    if rn:
        row = conn.execute(
            f"SELECT {cols} FROM companies WHERE TRIM(external_record_no) = ? LIMIT 1",
            (rn,),
        ).fetchone()
        if row:
            _add_candidate(bucket, row)
        rel = conn.execute(
            f"""
            SELECT co.id, co.company_name, co.external_record_no, co.website, co.address,
                   co.city, co.state, co.zip, co.legacy_phone,
                   TRIM(COALESCE(co.archived_at,'')) AS archived_at
            FROM companies co
            JOIN client_company_relationships ccr ON ccr.company_id = co.id
            WHERE TRIM(ccr.external_record_no) = ?
            LIMIT 1
            """,
            (rn,),
        ).fetchone()
        if rel:
            _add_candidate(bucket, rel)

    b_dom = domain(_blank(website))
    if b_dom:
        for r in conn.execute(
            f"""
            SELECT {cols} FROM companies
            WHERE lower(trim(website)) LIKE ?
            LIMIT 40
            """,
            (f"%{b_dom}%",),
        ).fetchall():
            if domain(_blank(r["website"])) == b_dom:
                _add_candidate(bucket, r)

    b_phone = digits_phone(_blank(phone))
    if b_phone and len(b_phone) >= 7:
        last7 = b_phone[-7:]
        for r in conn.execute(
            f"""
            SELECT {cols} FROM companies
            WHERE legacy_phone LIKE ?
            LIMIT 40
            """,
            (f"%{last7}%",),
        ).fetchall():
            cph = digits_phone(_blank(r["legacy_phone"]))
            if cph and (cph == b_phone or cph.endswith(last7) or b_phone.endswith(cph[-7:])):
                _add_candidate(bucket, r)

    b_addr = norm_addr(_blank(address))
    b_city, b_state = _blank(city), _blank(state)
    if b_addr and b_city and b_state:
        token = b_addr[:16] if len(b_addr) >= 4 else b_addr
        for r in conn.execute(
            f"""
            SELECT {cols} FROM companies
            WHERE city = ? COLLATE NOCASE
              AND state = ? COLLATE NOCASE
              AND address LIKE ?
            LIMIT 40
            """,
            (b_city, b_state, f"%{token}%"),
        ).fetchall():
            _add_candidate(bucket, r)

    typed = _blank(company_name)
    compact = compact_company_name(typed)
    alnum = compact_company_alnum(typed)
    if typed:
        for r in conn.execute(
            f"SELECT {cols} FROM companies WHERE company_name = ? COLLATE NOCASE LIMIT 20",
            (typed,),
        ).fetchall():
            _add_candidate(bucket, r)

        if len(alnum) >= 5:
            for r in conn.execute(
                f"""
                SELECT {cols} FROM companies
                WHERE {_SQL_RAW_ALNUM} = ?
                   OR {_SQL_RAW_ALNUM} LIKE ?
                LIMIT 40
                """,
                (alnum, f"%{alnum}%"),
            ).fetchall():
                _add_candidate(bucket, r)

        tokens = compact.split()
        patterns: list[str] = []
        if tokens:
            last = tokens[-1]
            first = tokens[0]
            if last:
                # Initials + last name, allowing punctuation between letters.
                if first.isalpha() and 1 <= len(first) <= 4 and len(tokens) >= 2:
                    letters = list(first)
                    patterns.append("%".join(letters) + "%" + last + "%")
                prefix = last[:3] if len(last) >= 3 else last
                if prefix and first:
                    patterns.append(first[0] + "%" + prefix + "%")
            significant = [t for t in tokens if len(t) >= 3 and not t.isdigit()]
            if significant:
                longest = max(significant, key=len)
                if len(longest) >= 5:
                    patterns.append("%" + longest + "%")
        seen: set[str] = set()
        for pat in patterns:
            key = pat.lower()
            if key in seen:
                continue
            seen.add(key)
            for r in conn.execute(
                f"SELECT {cols} FROM companies WHERE company_name LIKE ? LIMIT 80",
                (pat,),
            ).fetchall():
                _add_candidate(bucket, r)
            if len(bucket) >= MAX_CANDIDATES:
                break

    return list(bucket.values())[:MAX_CANDIDATES]


def find_scored_matches(
    conn,
    *,
    client_id: int,
    company_name: str,
    website: str = "",
    address: str = "",
    city: str = "",
    state: str = "",
    phone: str = "",
    external_record_no: str = "",
    include_ai: bool = True,
) -> list[dict[str, Any]]:
    query = {
        "company_name": _blank(company_name),
        "website": _blank(website),
        "address": _blank(address),
        "city": _blank(city),
        "state": _blank(state),
        "phone": _blank(phone),
        "external_record_no": _blank(external_record_no),
    }
    scored: list[dict[str, Any]] = []
    for cand in gather_candidates(conn, **query):
        score, reasons = score_company_pair(query, cand)
        if score < POSSIBLE_SCORE and "compact_name" not in reasons:
            continue
        confidence = "high" if score >= STRONG_SCORE else "possible"
        item = {
            **cand,
            "reasons": reasons,
            "confidence": confidence,
            "score": score,
            "already_assigned": _already_assigned(conn, int(cand["company_id"]), client_id),
            "client_relationships": _client_relationships(conn, int(cand["company_id"])),
            "ai_assessment": "",
            "ai_explanation": "",
            "ai_confidence": "",
            "ai_available": False,
        }
        scored.append(item)
    scored.sort(key=lambda m: (-int(m["score"]), m["company_name"].lower(), m["company_id"]))
    shortlist = scored[:MAX_RETURN]
    if include_ai and shortlist:
        from company_match_ai import review_duplicate_candidates

        review_duplicate_candidates(query, shortlist)
    return shortlist
