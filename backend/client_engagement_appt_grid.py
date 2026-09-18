"""Appointment Grid / engagement-import safeguards (Phase 3A).

Pure planning + matching helpers for history-aware, idempotent imports.
Does not write CRM masters or shared history.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

FIELD_CLASSES = (
    "ALREADY_REPRESENTED",
    "NEW_STRUCTURED_VALUE",
    "PARTIALLY_REPRESENTED",
    "AMBIGUOUS",
    "BLANK",
)

PROPOSED_ACTIONS = (
    "CREATE_STRUCTURED_EVENT",
    "CREATE_DELTA_STRUCTURED_EVENT",
    "SKIP_FULL_DUPLICATE",
    "SKIP",
    "REVIEW",
)

FQW_BLOCK_START = "--- Appointment Grid metadata ---"


def blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def norm_name(value: str) -> str:
    t = blank(value).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_text(value: str) -> str:
    return norm_name(value)


def digits(value: str) -> str:
    return re.sub(r"\D", "", blank(value))


def nanp10(value: str) -> str:
    d = digits(value)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else d


def is_blankish(value: Any) -> bool:
    t = blank(value)
    return t == "" or t in {"-", "—", "n/a", "N/A", "na", "NA", "."}


def is_footer_or_total_row(headers: list[str], cells: list[str]) -> bool:
    """Deterministically exclude Appointment Grid footer/total/summary rows."""
    if not any(blank(c) for c in cells):
        return True
    raw = {
        blank(h): blank(cells[i]) if i < len(cells) else ""
        for i, h in enumerate(headers)
        if blank(h)
    }

    def get(*names: str) -> str:
        for n in names:
            for k, v in raw.items():
                if blank(k).lower().rstrip() == n.lower():
                    return blank(v)
        return ""

    company = get("Company Name", "company")
    rn = get("Record Number", "record number", "record no")
    contact = get("Contact Person", "contact")
    joined = " ".join(blank(c) for c in cells).lower()

    dt = get("Appointment Date/Time", "appointment_date_time")
    # Non-account summary / blank stub rows (no identity + no schedule)
    if not company and not rn and not contact and not dt:
        return True

    if company or rn or contact:
        return False

    if re.search(r"\b(total|totals|grand total|35 appts|appts\b)\b", joined):
        return True
    if re.search(r"\b(20\d{2})\b", joined) and re.search(
        r"\b(quoted|won|forecast|sum)\b", joined
    ):
        return True
    if re.search(r"\b(2025|2026)\b", joined) and not company and not rn:
        return True
    return False


def _parse_any_history_date(value: str) -> datetime | None:
    t = blank(value)
    if not t:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", t)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            return None
    # Brown shared-history style: 24-Oct-2025 9:27 AM CDT
    m = re.match(
        r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:\s+(\d{1,2}:\d{2}\s*[AaPp][Mm]))?",
        t,
    )
    if m:
        try:
            base = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if m.group(4):
                return datetime.strptime(
                    f"{base} {m.group(4).upper().replace(' ', '')}",
                    "%d-%b-%Y %I:%M%p",
                )
            return datetime.strptime(base, "%d-%b-%Y")
        except ValueError:
            pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(t[:19], fmt)
        except ValueError:
            continue
    return None


def extract_schedule_month_day(text: str) -> dict[str, Any] | None:
    """Extract month/day/optional-year/time from schedule text (no year invention)."""
    raw = blank(text)
    if not raw:
        return None
    low = raw.lower()
    if re.search(r"(?i)^\s*rfq\s*$", raw) or (
        re.search(r"(?i)\brfq\b", raw) and len(raw) <= 12
    ):
        return None
    if re.search(r"(?i)\b(fyi|send e-?mail|send information)\b", low):
        return None
    m = re.search(
        r"(?i)(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?"
        r"(?:[,\s]+(\d{4}))?\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)",
        raw,
    )
    if not m:
        return None
    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    return {
        "month": months[m.group(1).lower()],
        "day": int(m.group(2)),
        "year": int(m.group(3)) if m.group(3) else None,
        "time_raw": m.group(4),
    }


def corroborate_year_from_history(
    *,
    appointment_date_time: str,
    caller_notes: str = "",
    history_events: list[dict[str, Any]] | None = None,
) -> tuple[int | None, str]:
    """Deterministic year for year-less schedule text from history/caller notes.

    Returns (year, evidence). Year is set only when uniquely corroborated —
    never invents current/default year.
    """
    md = extract_schedule_month_day(appointment_date_time)
    if not md:
        return None, ""
    if md.get("year"):
        return int(md["year"]), "explicit"
    history_events = history_events or []
    month_name = datetime(2000, md["month"], 1).strftime("%B")
    day = md["day"]
    candidates: set[int] = set()
    for h in history_events:
        note = blank(h.get("note_text"))
        if not re.search(r"(?i)appt|appointment", note):
            continue
        if not re.search(rf"(?i){month_name}\s+{day}(?:st|nd|rd|th)?", note):
            continue
        years: set[int] = set()
        for y in re.findall(r"\b(20\d{2})\b", note):
            years.add(int(y))
        for m2 in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", note):
            yy = int(m2[2])
            if yy < 100:
                yy += 2000
            years.add(yy)
        hd = _parse_any_history_date(blank(h.get("event_at")))
        if hd:
            years.add(hd.year)
        candidates |= years

    if len(candidates) == 1:
        y = next(iter(candidates))
        return y, f"history unique year {y} for {month_name} {day}"

    cn_years: set[int] = set()
    cn = blank(caller_notes)
    for m2 in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", cn):
        yy = int(m2[2])
        if yy < 100:
            yy += 2000
        cn_years.add(yy)
    for y in re.findall(r"\b(20\d{2})\b", cn):
        cn_years.add(int(y))

    if len(cn_years) == 1:
        y = next(iter(cn_years))
        if not candidates or y in candidates:
            has_appt_that_year = any(
                (_parse_any_history_date(blank(h.get("event_at"))) or datetime.min).year
                == y
                and re.search(r"(?i)appt|appointment", blank(h.get("note_text")))
                for h in history_events
            )
            if has_appt_that_year or candidates == {y}:
                return y, f"caller-note year {y} corroborated by history"

    return None, (
        f"unresolved year for {month_name} {day}"
        + (f"; candidates={sorted(candidates)}" if candidates else "")
    )


def parse_datetime_text(
    text: str, *, corroborated_year: int | None = None
) -> dict[str, Any]:
    """Parse Appointment Grid datetime text without inventing a year.

    Year rules:
    - use explicit year in the source string when present
    - else use corroborated_year only when supplied by review/resolution
    - else datetime_needs_review=1 and leave event_date empty
    Never silently assigns 2026 or the current year.
    """
    raw = blank(text)
    out: dict[str, Any] = {
        "source_date_time_text": raw,
        "event_date": "",
        "event_time": "",
        "timezone": "",
        "meeting_type": "",
        "datetime_needs_review": 0,
        "year_source": "",
    }
    if not raw:
        return out
    low = raw.lower()

    if re.search(r"(?i)^\s*rfq\s*$", raw) or (
        re.search(r"(?i)\brfq\b", raw) and len(raw) <= 12
    ):
        return out
    if re.search(r"(?i)\b(fyi|send e-?mail|send information)\b", low):
        return out

    if "google meet" in low:
        out["meeting_type"] = "Google Meet"
    elif "microsoft teams" in low or "teams meeting" in low:
        out["meeting_type"] = "Microsoft Teams"
    elif "site visit" in low:
        out["meeting_type"] = "Site Visit"
    elif "phone" in low:
        out["meeting_type"] = "Phone"

    tz = re.search(r"\b(CDT|CST|EDT|EST|MDT|MST|PDT|PST)\b", raw, re.I)
    if tz:
        out["timezone"] = tz.group(1).upper()

    m = re.search(
        r"(?i)(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?"
        r"(?:[,\s]+(\d{4}))?\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)",
        raw,
    )
    if not m:
        if re.search(
            r"(?i)(monday|tuesday|wednesday|thursday|friday|saturday|sunday|at\s+\d|"
            r"january|february|march|april|may|june|july|august|september|october|"
            r"november|december)",
            raw,
        ):
            out["datetime_needs_review"] = 1
        return out

    month_name, day, year_s, tim = m.group(1), m.group(2), m.group(3), m.group(4)
    year: int | None = int(year_s) if year_s else None
    year_source = "explicit" if year is not None else ""
    if year is None and corroborated_year is not None:
        try:
            year = int(corroborated_year)
            year_source = "corroborated"
        except (TypeError, ValueError):
            year = None

    if year is None:
        out["datetime_needs_review"] = 1
        out["year_source"] = "unresolved"
        try:
            out["event_time"] = datetime.strptime(
                tim.upper().replace(" ", ""), "%I:%M%p"
            ).strftime("%H:%M")
        except ValueError:
            pass
        return out

    try:
        dt = datetime.strptime(
            f"{month_name} {day} {year} {tim.upper().replace(' ', '')}",
            "%B %d %Y %I:%M%p",
        )
        out["event_date"] = dt.strftime("%Y-%m-%d")
        out["event_time"] = dt.strftime("%H:%M")
        out["year_source"] = year_source
    except ValueError:
        out["datetime_needs_review"] = 1
        out["year_source"] = "invalid"
    return out


def classify_event_type(sheet_type: str, date_time_text: str) -> str:
    st = blank(sheet_type)
    text = blank(date_time_text)
    low = text.lower()
    if st in {"Send Information", "Engagement"}:
        return "Send Information"
    if st == "Appointments":
        if re.search(r"(?i)^\s*rfq\s*$", text) or (
            re.search(r"(?i)\brfq\b", text) and len(text) <= 12
        ):
            return "RFQ"
        if re.search(r"(?i)\breschedul", text):
            return "Appointment Rescheduled"
        if re.search(r"(?i)completed|held|met with", text):
            return "Appointment Completed"
        if text:
            return "Appointment Scheduled"
        return "Other"
    if re.search(r"(?i)\bfyi\b|send e-?mail", low):
        return "Send Information"
    return "Other"


def is_partial_company_name(nn: str, cn: str) -> bool:
    if not nn or not cn or nn == cn:
        return False
    if len(nn) < 4 or len(cn) < 4:
        return False
    if nn in cn or cn in nn:
        shorter, longer = (nn, cn) if len(nn) <= len(cn) else (cn, nn)
        if longer.startswith(shorter + " ") or longer.endswith(" " + shorter):
            return True
        if len(shorter) / max(len(longer), 1) >= 0.7:
            return True
        return False
    nt, ct = nn.split(), cn.split()
    if len(nt) < 2 or len(ct) < 2:
        return False
    if nt[0] != ct[0] and nt[0] not in ct:
        return False
    inter = set(nt) & set(ct)
    return len(inter) >= 2 and (len(inter) / len(nt)) >= 0.75


def match_company_for_appt_grid(
    conn,
    client_id: int,
    record_no: str,
    company_name: str,
) -> tuple[str, int | None, int | None, str, str, list[str]]:
    """Safe company match for Appointment Grid.

    Returns (status, company_id, relationship_id, display_name, brown_record_no, flags).
    Foreign-client RN is never an automatic Brown match.
    Returned record_no is always the Brown relationship RN (never overwrite source).
    """
    flags: list[str] = []
    rn = blank(record_no)
    foreign = None
    if rn:
        brown = conn.execute(
            """
            SELECT ccr.id AS relationship_id, c.id AS company_id, c.company_name,
                   ccr.external_record_no
            FROM client_company_relationships ccr
            JOIN companies c ON c.id = ccr.company_id
            WHERE ccr.client_id = ? AND trim(ccr.external_record_no) = ?
            LIMIT 1
            """,
            (client_id, rn),
        ).fetchone()
        if brown:
            return (
                "MATCHED",
                int(brown["company_id"]),
                int(brown["relationship_id"]),
                blank(brown["company_name"]),
                blank(brown["external_record_no"]),
                flags,
            )
        foreign = conn.execute(
            """
            SELECT ccr.client_id, c.id AS company_id, c.company_name, ccr.external_record_no
            FROM client_company_relationships ccr
            JOIN companies c ON c.id = ccr.company_id
            WHERE ccr.client_id != ? AND trim(ccr.external_record_no) = ?
            LIMIT 1
            """,
            (client_id, rn),
        ).fetchone()
        if foreign:
            flags.append(
                f"Foreign RN on client {foreign['client_id']} "
                f"({blank(foreign['company_name'])}) — ignored for auto-match"
            )

    nn = norm_name(company_name)
    if not nn:
        return ("REVIEW" if flags else "NEW", None, None, company_name, "", flags)

    rows = conn.execute(
        """
        SELECT ccr.id AS relationship_id, c.id AS company_id, c.company_name,
               ccr.external_record_no
        FROM client_company_relationships ccr
        JOIN companies c ON c.id = ccr.company_id
        WHERE ccr.client_id = ?
        """,
        (client_id,),
    ).fetchall()
    exact = [r for r in rows if norm_name(r["company_name"]) == nn]
    if len(exact) == 1:
        r = exact[0]
        if rn and blank(r["external_record_no"]) and rn != blank(r["external_record_no"]):
            flags.append(
                f"Source RN {rn} differs from Brown RN {r['external_record_no']} "
                f"— link by name only; do not overwrite Brown RN"
            )
        return (
            "MATCHED",
            int(r["company_id"]),
            int(r["relationship_id"]),
            blank(r["company_name"]),
            blank(r["external_record_no"]),
            flags,
        )
    if len(exact) > 1:
        flags.append("Ambiguous exact company name on Brown")
        return ("REVIEW", None, None, company_name, "", flags)

    partial = [
        r for r in rows if is_partial_company_name(nn, norm_name(r["company_name"]))
    ]
    if len(partial) == 1:
        r = partial[0]
        flags.append(
            f"Partial name match: '{company_name}' ~ '{r['company_name']}' — REVIEW"
        )
        return (
            "POSSIBLE MATCH",
            int(r["company_id"]),
            int(r["relationship_id"]),
            blank(r["company_name"]),
            blank(r["external_record_no"]),
            flags,
        )
    if partial:
        flags.append("Ambiguous partial company-name candidates")
        return ("REVIEW", None, None, company_name, "", flags)

    if foreign:
        flags.append("Foreign RN and no Brown name match")
        return ("REVIEW", None, None, company_name, "", flags)
    return ("NEW", None, None, company_name, "", flags)


def _contact_full_name(row: Any) -> str:
    return f"{blank(row['first_name'])} {blank(row['last_name'])}".strip()


def match_contact_for_appt_grid(
    conn,
    company_id: int | None,
    email: str,
    contact_name: str,
    phone: str,
    title: str,
) -> tuple[str, int | None, str, list[int], str]:
    """Resolve contact without creating records.

    Returns (status, contact_id, display_name, candidate_ids, note).
    Same-person duplicates → canonical existing contact (earliest id tie-break).
    Different people sharing email → REVIEW.
    """
    if company_id is None:
        return "NEW", None, contact_name, [], "No company scope for contact match."

    em = blank(email).lower()
    pn = norm_name(contact_name)
    title_n = norm_name(title)
    phone_digits = [nanp10(p) for p in re.split(r"[/|;]", blank(phone)) if nanp10(p)]

    candidates: list[Any] = []
    if em:
        candidates = list(
            conn.execute(
                """
                SELECT id, first_name, last_name, title, phone, alt_phone, email, company_id
                FROM contacts
                WHERE company_id = ? AND lower(trim(email)) = ?
                """,
                (company_id, em),
            ).fetchall()
        )

    if not candidates and phone_digits:
        rows = conn.execute(
            """
            SELECT id, first_name, last_name, title, phone, alt_phone, email, company_id
            FROM contacts WHERE company_id = ?
            """,
            (company_id,),
        ).fetchall()
        for r in rows:
            c_phones = {nanp10(r["phone"] or ""), nanp10(r["alt_phone"] or "")}
            if set(phone_digits) & {p for p in c_phones if p}:
                candidates.append(r)

    if not candidates and pn:
        rows = conn.execute(
            """
            SELECT id, first_name, last_name, title, phone, alt_phone, email, company_id
            FROM contacts WHERE company_id = ?
            """,
            (company_id,),
        ).fetchall()
        for r in rows:
            if norm_name(_contact_full_name(r)) == pn:
                candidates.append(r)

    if not candidates:
        if not contact_name and not em and not phone:
            return "NEW", None, "", [], "No contact fields."
        return "NEW", None, contact_name, [], "No unique Brown contact match."

    by_id = {int(r["id"]): r for r in candidates}
    candidates = list(by_id.values())
    cand_ids = sorted(by_id.keys())

    if len(candidates) == 1:
        c = candidates[0]
        return (
            "MATCHED",
            int(c["id"]),
            _contact_full_name(c),
            cand_ids,
            "Unique match.",
        )

    # Different people sharing an email (or phone set) → REVIEW before scoring
    distinct_people = {norm_name(_contact_full_name(c)) for c in candidates}
    if len(distinct_people) > 1:
        return (
            "REVIEW",
            None,
            contact_name,
            cand_ids,
            "Different people share this email/phone evidence — REVIEW.",
        )

    scored: list[tuple[int, int, Any, list[str]]] = []
    for c in candidates:
        score = 0
        reasons: list[str] = []
        full = norm_name(_contact_full_name(c))
        if company_id and int(c["company_id"]) == int(company_id):
            score += 10
            reasons.append("company")
        if pn and full == pn:
            score += 100
            reasons.append("name_exact")
        elif pn and pn.split() and pn.split()[0] in full:
            score += 40
            reasons.append("first_name")
        if em and blank(c["email"]).lower() == em:
            score += 50
            reasons.append("email")
        c_phones = {nanp10(c["phone"] or ""), nanp10(c["alt_phone"] or "")}
        if set(phone_digits) & {p for p in c_phones if p}:
            score += 20
            reasons.append("phone")
        if title_n and title_n == norm_name(c["title"] or ""):
            score += 30
            reasons.append("title")
        scored.append((score, -int(c["id"]), c, reasons))
    scored.sort(reverse=True)
    best_score, _, best, reasons = scored[0]

    same_person = [
        item
        for item in scored
        if item[0] == best_score
        and norm_name(_contact_full_name(item[2])) == norm_name(_contact_full_name(best))
        and int(item[2]["company_id"]) == int(best["company_id"])
    ]
    distinct_names = {
        norm_name(_contact_full_name(item[2])) for item in scored if item[0] == best_score
    }
    if len(distinct_names) > 1:
        return (
            "REVIEW",
            None,
            contact_name,
            cand_ids,
            "Different people share this email/phone evidence — REVIEW.",
        )

    if len(same_person) >= 1 and best_score >= 100:
        chosen = sorted(same_person, key=lambda x: int(x[2]["id"]))[0][2]
        return (
            "MATCHED",
            int(chosen["id"]),
            _contact_full_name(chosen),
            cand_ids,
            (
                f"Same-person duplicates; canonical contact_id={chosen['id']} "
                f"(earliest id tie-break). Reasons: {','.join(reasons)}."
            ),
        )

    if best_score < 40:
        return (
            "REVIEW",
            None,
            contact_name,
            cand_ids,
            "Weak evidence among multiple contact candidates — REVIEW.",
        )

    return (
        "MATCHED",
        int(best["id"]),
        _contact_full_name(best),
        cand_ids,
        f"Resolved via {','.join(reasons)}; candidates={cand_ids}.",
    )


def classify_field_vs_history(value: Any, history_blob_norm: str) -> str:
    if is_blankish(value):
        return "BLANK"
    nv = norm_text(str(value))
    if not nv:
        return "BLANK"
    hb = history_blob_norm or ""
    if len(nv) >= 30 and nv[:60] in hb:
        return "ALREADY_REPRESENTED"
    if len(nv) >= 30:
        tokens = [t for t in nv.split() if len(t) > 3]
        if tokens:
            hit = sum(1 for t in tokens if t in hb)
            ratio = hit / len(tokens)
            if ratio >= 0.7:
                return "ALREADY_REPRESENTED"
            if ratio >= 0.35:
                return "PARTIALLY_REPRESENTED"
        return "NEW_STRUCTURED_VALUE"
    money = blank(str(value)).replace(",", "").replace("$", "")
    if re.fullmatch(r"[\d.]+", money):
        if money and money in hb.replace(",", ""):
            return "ALREADY_REPRESENTED"
        return "NEW_STRUCTURED_VALUE"
    if nv in hb:
        return "ALREADY_REPRESENTED"
    return "NEW_STRUCTURED_VALUE"


def classify_appointment_narrative(
    *,
    event_type: str,
    contact_name: str,
    caller_notes: str,
    appointment_date_time: str,
    resolved_date: str,
    history_events: list[dict[str, Any]],
) -> tuple[str, str]:
    blob = norm_text("\n".join(blank(h.get("note_text")) for h in history_events))
    if event_type == "Send Information":
        if re.search(r"\b(send e?mail|sent email|information sent|marketing)\b", blob):
            return "ALREADY_REPRESENTED", "history mentions email/send-info"
        return "NEW_STRUCTURED_VALUE", "no clear send-info in history"
    if event_type == "RFQ":
        if re.search(r"\brfq\b", blob):
            return "ALREADY_REPRESENTED", "RFQ keyword in company history"
        return "NEW_STRUCTURED_VALUE", "no RFQ keyword in history"

    last = norm_name(contact_name).split()[-1] if norm_name(contact_name) else ""
    has_appt = bool(re.search(r"\b(appt|appointment)\b", blob))
    has_contact = bool(last and last in blob)
    date_hit = False
    if resolved_date:
        try:
            rd = datetime.strptime(resolved_date, "%Y-%m-%d")
        except ValueError:
            rd = None
        if rd:
            month_name = rd.strftime("%B").lower()
            if month_name in blob and str(rd.day) in blob and has_appt:
                date_hit = True
    if has_appt and (has_contact or date_hit):
        return "ALREADY_REPRESENTED", "appt keyword + contact/date evidence"
    if has_appt:
        return "PARTIALLY_REPRESENTED", "appt keyword without clear contact/date link"
    cn = norm_text(caller_notes)
    if cn and len(cn) > 40:
        tokens = [t for t in cn.split() if len(t) > 3][:40]
        if tokens:
            hit = sum(1 for t in tokens if t in blob)
            if hit / len(tokens) >= 0.55:
                return "ALREADY_REPRESENTED", "caller-note narrative largely in history"
            if hit / len(tokens) >= 0.25:
                return "PARTIALLY_REPRESENTED", "partial caller-note overlap"
    if is_blankish(appointment_date_time) and is_blankish(caller_notes):
        return "BLANK", ""
    return "NEW_STRUCTURED_VALUE", "no clear appointment narrative in history"


def compose_forecast_quoted_won_block(
    *, forecast: str = "", quoted: str = "", won: str = ""
) -> str:
    lines = []
    if not is_blankish(forecast):
        lines.append(f"Forecast: {blank(forecast)}")
    if not is_blankish(quoted):
        lines.append(f"Quoted: {blank(quoted)}")
    if not is_blankish(won):
        lines.append(f"Won: {blank(won)}")
    if not lines:
        return ""
    return FQW_BLOCK_START + "\n" + "\n".join(lines)


def merge_sales_notes_with_fqw(base_sales_notes: str, fqw_block: str) -> str:
    """Append FQW block once; idempotent if block already present."""
    base = blank(base_sales_notes)
    block = blank(fqw_block)
    if not block:
        return base
    if FQW_BLOCK_START in base:
        parts = base.split(FQW_BLOCK_START, 1)
        prefix = parts[0].rstrip()
        return (prefix + "\n\n" + block).strip() if prefix else block
    if not base:
        return block
    return f"{base.rstrip()}\n\n{block}"


def appointment_grid_fingerprint(
    *,
    client_id: int,
    source_document_sha256: str,
    source_sheet: str,
    source_row: int,
    source_rn: str,
    company_name: str,
    contact_name: str,
    email: str,
    event_type: str,
    event_date: str,
    source_datetime_text: str,
    grade: str,
    dollars_quoted: str,
    outcome: str,
    forecast: str,
    quoted_label: str,
    won: str,
) -> str:
    """Deterministic idempotency fingerprint (no volatile DB IDs).

    Algorithm (v1): sha256 of pipe-joined fields:
      v1|appt_grid|client_id|source_document_sha256|norm(sheet)|source_row|
      source_rn|norm(company)|norm(contact)|lower(email)|event_type|event_date|
      norm(datetime_text)[:160]|norm(grade)|dollars_quoted|norm(outcome)|
      norm(forecast)|norm(quoted_label)|norm(won)
    """
    payload = "|".join(
        [
            "v1",
            "appt_grid",
            str(int(client_id)),
            blank(source_document_sha256).lower(),
            norm_name(source_sheet),
            str(int(source_row or 0)),
            blank(source_rn),
            norm_name(company_name),
            norm_name(contact_name),
            blank(email).lower(),
            blank(event_type),
            blank(event_date),
            norm_text(source_datetime_text)[:160],
            norm_name(grade),
            blank(str(dollars_quoted)),
            norm_name(outcome),
            norm_name(forecast),
            norm_name(quoted_label),
            norm_name(won),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def propose_row_action(
    field_classes: dict[str, str],
    *,
    company_status: str,
    contact_status: str,
    datetime_needs_review: bool,
    event_type: str,
) -> tuple[str, str]:
    if company_status in {"REVIEW", "POSSIBLE MATCH", "CONFLICT", "NEW"}:
        return "REVIEW", f"Company status {company_status} blocks auto-confirm."
    if contact_status == "REVIEW":
        return "REVIEW", "Ambiguous contact resolution blocks auto-confirm."
    if datetime_needs_review and event_type in {
        "Appointment Scheduled",
        "Appointment Rescheduled",
        "Appointment Completed",
    }:
        return "REVIEW", "Required appointment date unresolved."

    narrative = field_classes.get("appointment_narrative", "BLANK")
    structured_keys = (
        "appointment_grade",
        "dollars_quoted",
        "outcome",
        "forecast",
        "quoted_label",
        "won",
        "sales_notes",
        "appointment_date_time",
        "revenue_specialist",
    )
    new_struct = [
        k for k in structured_keys if field_classes.get(k) == "NEW_STRUCTURED_VALUE"
    ]
    meaningful = [
        k
        for k, v in field_classes.items()
        if v in {"NEW_STRUCTURED_VALUE", "PARTIALLY_REPRESENTED", "AMBIGUOUS"}
    ]
    already = [k for k, v in field_classes.items() if v == "ALREADY_REPRESENTED"]

    if not meaningful and already:
        return "SKIP_FULL_DUPLICATE", "All meaningful fields already represented."
    if not meaningful and not already:
        return "SKIP", "No meaningful content."

    if narrative == "ALREADY_REPRESENTED" and new_struct:
        return (
            "CREATE_DELTA_STRUCTURED_EVENT",
            f"History narrates appointment; preserve new structured fields: {', '.join(new_struct)}.",
        )
    if narrative == "NEW_STRUCTURED_VALUE" or (
        field_classes.get("appointment_date_time") == "NEW_STRUCTURED_VALUE"
        and narrative != "ALREADY_REPRESENTED"
    ):
        return (
            "CREATE_STRUCTURED_EVENT",
            "Structured sales event adds useful appointment/event information.",
        )
    if new_struct:
        return (
            "CREATE_STRUCTURED_EVENT",
            f"New structured values: {', '.join(new_struct)}.",
        )
    if any(v == "PARTIALLY_REPRESENTED" for v in field_classes.values()):
        return "REVIEW", "Only partial representation — human review."
    if any(v == "AMBIGUOUS" for v in field_classes.values()):
        return "REVIEW", "Ambiguous field classification."
    return "SKIP_FULL_DUPLICATE", "No new structured values identified."


def provenance_source_key(source_sheet: str, source_row: int | None) -> tuple[str, int] | None:
    """Stable same-document coordinate. Not unique across documents."""
    sheet = norm_name(source_sheet)
    try:
        row = int(source_row or 0)
    except (TypeError, ValueError):
        row = 0
    if not sheet or row <= 0:
        return None
    return (sheet, row)


def already_imported_by_fingerprint(
    conn, client_id: int, fingerprint: str
) -> int | None:
    fp = blank(fingerprint)
    if not fp:
        return None
    row = conn.execute(
        """
        SELECT id FROM client_sales_events
        WHERE client_id = ? AND source_row_fingerprint = ?
        LIMIT 1
        """,
        (int(client_id), fp),
    ).fetchone()
    return int(row["id"]) if row else None


def already_imported_by_provenance(
    conn,
    *,
    client_id: int,
    source_sheet: str,
    source_row: int | None,
    same_document_ids: set[int],
) -> int | None:
    """Same client + same source document identity + sheet + row.

    same_document_ids must already be scoped to this client and this document
    (document_id and/or SHA-256 equivalents). Sheet+row alone is never enough.
    """
    key = provenance_source_key(source_sheet, source_row)
    if not key or not same_document_ids:
        return None
    ids = sorted({int(x) for x in same_document_ids if int(x) > 0})
    if not ids:
        return None
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT id, source_sheet, source_row
        FROM client_sales_events
        WHERE client_id = ?
          AND source_row = ?
          AND source_document_id IN ({placeholders})
        """,
        (int(client_id), key[1], *ids),
    ).fetchall()
    for row in rows:
        if provenance_source_key(row["source_sheet"], row["source_row"]) == key:
            return int(row["id"])
    return None


def classify_already_imported(
    *,
    fingerprint_event_id: int | None,
    provenance_event_id: int | None,
) -> tuple[str, str, int | None]:
    """Return (duplicate_status, kind, event_id). kind is fingerprint|provenance|''."""
    if fingerprint_event_id:
        return (
            "DUPLICATE — ALREADY IMPORTED",
            "fingerprint",
            int(fingerprint_event_id),
        )
    if provenance_event_id:
        return (
            "DUPLICATE — ALREADY IMPORTED (same document row)",
            "provenance",
            int(provenance_event_id),
        )
    return "", "", None


def row_blocks_confirm(
    *,
    proposed_action: str,
    company_status: str,
    contact_status: str,
    datetime_needs_review: bool,
    event_type: str,
    import_status: str = "",
    client_validation: str = "",
) -> bool:
    if proposed_action in {"SKIP", "SKIP_FULL_DUPLICATE"}:
        return False
    if blank(import_status) in {"ignored", "skipped", "duplicate"}:
        return False
    if blank(client_validation).upper() == "MISMATCH":
        return True
    if proposed_action == "REVIEW":
        return True
    if company_status in {"REVIEW", "POSSIBLE MATCH", "CONFLICT", "NEW"}:
        return True
    if contact_status == "REVIEW":
        return True
    if datetime_needs_review and event_type.startswith("Appointment"):
        return True
    return False


def build_delta_caller_notes(*, narrative_class: str, caller_notes: str) -> str:
    if narrative_class == "ALREADY_REPRESENTED":
        return ""
    return blank(caller_notes)


@dataclass
class ApptGridRowPlan:
    field_classes: dict[str, str] = field(default_factory=dict)
    proposed_action: str = "REVIEW"
    reason: str = ""
    composed_sales_notes: str = ""
    caller_notes_for_event: str = ""
    blocks_confirm: bool = True
    fingerprint: str = ""
    flags: list[str] = field(default_factory=list)
