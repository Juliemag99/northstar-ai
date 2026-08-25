"""Phase 2 — extract structured proposals from client documents.

Never auto-writes Client Setup. Proposals stay Pending until human review.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db import get_connection

# --- Field catalog ---

FIELD_CATALOG: dict[str, dict[str, str]] = {
    # section -> field_name -> display label
    "client_profile": {
        "client_name": "Client Name",
        "website": "Website",
        "address": "Address",
        "main_phone": "Phone",
        "client_contacts": "Client Contacts",
        "decision_makers": "Decision Makers",
        "primary_owner_name": "NorthStar Owner / Revenue Specialist",
        "who_takes_appointments": "Who Takes Appointments",
    },
    "strategy": {
        "reason_hired": "Reason Client Hired NorthStar",
        "sales_goals": "Sales Goals",
        "primary_service": "Primary Service",
        "secondary_services": "Secondary Services",
        "target_industries": "Target Industries",
        "ideal_customer_profile": "Ideal Customer Types",
        "product_part_characteristics": "Product / Part Characteristics",
        "manufacturing_processes_sought": "Processes Sought",
        "production_preference": "Production Preferences",
        "geographic_preferences": "Geography",
        "positive_fit_signals": "Positive Fit Signals",
        "negative_fit_signals": "Negative Fit Signals",
        "exclusions": "Exclusions",
        "target_titles": "Target Titles",
        "prospecting_guidance": "Prospecting Guidance",
        "sales_challenges_barriers": "Sales Challenges / Barriers",
    },
    "call_playbook": {
        "thirty_second_commercial": "30-Second Commercial",
        "discovery_questions": "Discovery Questions",
        "common_objections": "Objections",
        "objection_responses": "Objection Responses",
        "value_propositions": "Value Proposition",
        "appointment_instructions": "Appointment Instructions",
        "caller_notes": "Caller Notes",
        "company_story_background": "Company Story / Background",
    },
    "capabilities": {
        "certifications": "Certifications",
        "facility": "Facility",
        "equipment": "Equipment",
        "capacity": "Capacity",
        "materials": "Materials",
        "production_processes": "Production Processes",
        "finishing": "Finishing",
        "logistics": "Logistics",
    },
    "email_templates": {
        "template_name": "Template Name",
        "template_type": "Type",
        "subject": "Subject",
        "body": "Body",
    },
    "client_operations": {
        "northstar_client_email": "NorthStar Client Email",
        "northstar_revenue_specialist": "NorthStar Revenue Specialist",
        "who_takes_appointments": "Who Takes Appointments",
        "appointment_handling_instructions": "Appointment Handling Instructions",
        "appointment_recap_cc": "Appointment Recap CC",
        "reporting_instructions": "Reporting Instructions",
        "lead_handoff_procedures": "Lead Handoff Procedures",
        "communication_procedures": "Client Communication Procedures",
        "workflow_instructions": "Internal Workflow Instructions",
        "other_operational_notes": "Other Operational Notes",
    },
    "client_contacts": {
        "client_contact_person": "Client Contact Person",
        "client_contacts": "Client Contacts (multi-person — split before approve)",
        "client_contact_enrichment": "Client Contact Field Update",
    },
    "unmapped": {
        "unmapped_intelligence": "Unmapped Intelligence",
    },
}

CLASSIFICATIONS = (
    "MATCH",
    "NEW INFORMATION",
    "POTENTIAL UPDATE",
    "CONFLICT",
    "UNMAPPED",
    "SENSITIVE — NOT IMPORTED",
)

CONFIDENCE = ("HIGH", "MEDIUM", "LOW")

_SENSITIVE_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_\s-]?key|access[_\s-]?token|"
    r"secret[_\s-]?key|private[_\s-]?key|credentials?)\b"
)
_PASSWORD_LINE_RE = re.compile(
    r"(?i)(password|passwd|pwd)\s*[:=\-–]\s*\S+"
)


@dataclass
class ParsedDocument:
    filename: str
    extension: str
    text: str
    lines: list[str]
    tables: list[list[list[str]]] = field(default_factory=list)
    sheet_previews: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RawFinding:
    section: str
    field_name: str
    proposed_value: str
    raw_source_text: str
    source_locator: str = ""
    confidence: str = "MEDIUM"
    suggested_document_type: str = ""


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm(value: str) -> str:
    t = _blank(value).lower()
    t = t.replace("http://", "").replace("https://", "").replace("www.", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", _blank(value)).strip()


def infer_document_type_hint(filename: str, text: str, user_type: str) -> str:
    """Suggest type from filename/content. Does not override user selection."""
    name = filename.lower()
    body = (text or "")[:8000].lower()
    if "appointment grid" in name or "appt grid" in name:
        return "Appointment Grid"
    if "capabilities" in name:
        return "Capabilities"
    if "send email" in name or ("send email template" in body):
        return "Email Template"
    if "appt set" in name or "appointment set" in name:
        return "Appointment Set Template"
    if "road map" in name or "roadmap" in name or "call playbook" in name:
        return "Road Map / Call Playbook"
    if "strategy session" in name or "strategy session" in body:
        if "2" in name:
            return "Strategy Session 2"
        return "Strategy Session 1"
    if "client information" in name or "account information" in body:
        return "Client Information"
    if "nstar email" in name or "northstar email" in body:
        return "Other"
    return user_type or "Other"


def parse_file(path: Path, filename: str) -> ParsedDocument:
    ext = path.suffix.lower()
    if ext == ".docx":
        return _parse_docx(path, filename)
    if ext == ".csv":
        return _parse_csv(path, filename)
    if ext == ".xlsx":
        return _parse_xlsx(path, filename)
    raise ValueError(f"Unsupported file type: {ext}")


def _parse_docx(path: Path, filename: str) -> ParsedDocument:
    try:
        from docx import Document
    except ImportError as exc:
        raise ValueError("python-docx is required to process .docx files.") from exc

    doc = Document(str(path))
    lines: list[str] = []
    for p in doc.paragraphs:
        t = _blank(p.text)
        if t:
            lines.append(t)
    tables: list[list[list[str]]] = []
    for table in doc.tables:
        rows: list[list[str]] = []
        for row in table.rows:
            # Word tables often repeat merged cell text — dedupe adjacent identical cells
            raw_cells = [_collapse(c.text) for c in row.cells]
            cells: list[str] = []
            for c in raw_cells:
                if not cells or cells[-1] != c:
                    cells.append(c)
            if any(cells):
                rows.append(cells)
                lines.append(" | ".join(c for c in cells if c))
        if rows:
            tables.append(rows)
    text = "\n".join(lines)
    return ParsedDocument(
        filename=filename, extension=".docx", text=text, lines=lines, tables=tables
    )


def _parse_csv(path: Path, filename: str) -> ParsedDocument:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = [[_blank(c) for c in row] for row in reader]
    lines = [" | ".join(r) for r in rows if any(r)]
    preview = {
        "sheet": "CSV",
        "headers": rows[0] if rows else [],
        "row_count": max(0, len(rows) - 1),
        "sample_rows": rows[1:6],
    }
    return ParsedDocument(
        filename=filename,
        extension=".csv",
        text="\n".join(lines),
        lines=lines,
        tables=[rows] if rows else [],
        sheet_previews=[preview],
    )


def _parse_xlsx(path: Path, filename: str) -> ParsedDocument:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("openpyxl is required to process .xlsx files.") from exc

    wb = load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    previews: list[dict[str, Any]] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            cells = [_blank(c) for c in row]
            if any(cells):
                rows.append(cells)
        if not rows:
            continue
        tables.append(rows)
        previews.append(
            {
                "sheet": sheet_name,
                "headers": rows[0],
                "row_count": max(0, len(rows) - 1),
                "sample_rows": rows[1:6],
            }
        )
        lines.append(f"[Sheet: {sheet_name}]")
        for r in rows[:80]:
            lines.append(" | ".join(r))
    wb.close()
    return ParsedDocument(
        filename=filename,
        extension=".xlsx",
        text="\n".join(lines),
        lines=lines,
        tables=tables,
        sheet_previews=previews,
    )


def redact_sensitive(text: str) -> tuple[str, bool, list[str]]:
    """Remove credential values; return cleaned text, flagged?, exclusion notes."""
    notes: list[str] = []
    flagged = False
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if _PASSWORD_LINE_RE.search(line) or (
            _SENSITIVE_RE.search(line) and re.search(r"[:=\-–]\s*\S+", line)
        ):
            flagged = True
            notes.append("Sensitive credential detected and excluded.")
            # Keep non-secret context only
            cleaned = _PASSWORD_LINE_RE.sub(r"\1: [REDACTED]", line)
            cleaned = re.sub(
                r"(?i)(password|passwd|pwd|api[_\s-]?key|access[_\s-]?token)\s*[:=\-–]\s*\S+",
                r"\1: [REDACTED]",
                cleaned,
            )
            cleaned_lines.append(cleaned)
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines), flagged, notes


def _section_block(lines: list[str], start_pat: str, end_pats: list[str]) -> tuple[str, str]:
    start_re = re.compile(start_pat, re.I)
    end_res = [re.compile(p, re.I) for p in end_pats]
    start_i = None
    for i, line in enumerate(lines):
        if start_re.search(line):
            start_i = i
            break
    if start_i is None:
        return "", ""
    body: list[str] = []
    for j in range(start_i + 1, len(lines)):
        if any(r.search(lines[j]) for r in end_res):
            break
        body.append(lines[j])
    raw = lines[start_i]
    return _collapse("\n".join(body)), raw


def _labeled_value(lines: list[str], labels: list[str], *, max_follow: int = 3) -> tuple[str, str]:
    lab_re = re.compile(
        r"^(?:%s)\s*[:\-–]?\s*(.*)$" % "|".join(re.escape(l) for l in labels),
        re.I,
    )
    for i, line in enumerate(lines):
        m = lab_re.match(line.strip())
        if not m:
            continue
        val = _blank(m.group(1))
        raw = line
        if not val:
            follow: list[str] = []
            for k in range(1, max_follow + 1):
                if i + k >= len(lines):
                    break
                nxt = lines[i + k].strip()
                if re.match(r"^[A-Za-z].{0,40}:\s*$", nxt) or re.match(
                    r"^(I{1,3}|IV|V|VI|VII)\.", nxt
                ):
                    break
                if not nxt:
                    continue
                follow.append(nxt)
                if len(" ".join(follow)) > 40:
                    break
            val = _collapse("\n".join(follow))
        return val, raw
    return "", ""


def _inline_labeled(text: str, labels: list[str]) -> tuple[str, str]:
    for lab in labels:
        m = re.search(
            rf"(?i)(?:^|[\|\n])\s*{re.escape(lab)}\s*[:\-–]\s*([^|\n]+)",
            text,
        )
        if m:
            return _collapse(m.group(1)), m.group(0)
    return "", ""


def _extract_contacts(lines: list[str], text: str) -> tuple[str, str, str]:
    contacts: list[str] = []
    decision: list[str] = []
    # Flatten pipe-duplicated cells into searchable lines
    flat: list[str] = []
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        # unique adjacent
        cleaned: list[str] = []
        for p in parts:
            if p and (not cleaned or cleaned[-1] != p):
                cleaned.append(p)
        flat.extend(cleaned if len(cleaned) > 1 else [line])

    for i, line in enumerate(flat):
        m = re.match(r"(?i)^contact\s*name\s*:\s*(.+)$", line.strip())
        if m:
            name = _blank(m.group(1))
            title = ""
            is_dm = False
            email = ""
            for k in range(1, 10):
                if i + k >= len(flat):
                    break
                nxt = flat[i + k]
                if re.match(r"(?i)^contact\s*name\s*:", nxt):
                    break
                tm = re.match(r"(?i)^title\s*:\s*(.+)$", nxt.strip())
                if tm:
                    title = _blank(tm.group(1))
                if re.search(r"(?i)\[\s*x\s*\].*decision\s*maker", nxt):
                    is_dm = True
                em = re.search(r"[\w.+-]+@[\w.-]+\.\w+", nxt)
                if em:
                    email = em.group(0)
            entry = name
            if title:
                entry += f" — {title}"
            if email:
                entry += f" <{email}>"
            if entry not in contacts:
                contacts.append(entry)
            if is_dm and entry not in decision:
                decision.append(entry)
            continue
        m2 = re.match(
            r"^([A-Z][A-Za-z .'-]{1,40})\s*[-–]\s*([\w.+-]+@[\w.-]+\.\w+)",
            line.strip(),
        )
        if m2 and "carmeco" in m2.group(2).lower():
            entry = f"{_blank(m2.group(1))} <{m2.group(2)}>"
            if entry not in contacts:
                contacts.append(entry)
    return (
        "; ".join(contacts),
        "; ".join(decision) if decision else "",
        "Contact Name blocks" if contacts else "",
    )


def extract_findings(
    parsed: ParsedDocument,
    *,
    user_document_type: str,
) -> tuple[list[RawFinding], list[str], str]:
    """Return findings, sensitive exclusion notes, type hint (non-authoritative)."""
    cleaned, sensitive, notes = redact_sensitive(parsed.text)
    parsed_clean = ParsedDocument(
        filename=parsed.filename,
        extension=parsed.extension,
        text=cleaned,
        lines=[ln for ln in cleaned.splitlines() if _blank(ln)],
        tables=parsed.tables,
        sheet_previews=parsed.sheet_previews,
    )
    hint = infer_document_type_hint(
        parsed.filename, parsed_clean.text, user_document_type
    )
    dtype = user_document_type or hint
    findings: list[RawFinding] = []

    if sensitive:
        findings.append(
            RawFinding(
                section="unmapped",
                field_name="sensitive_exclusion",
                proposed_value="Sensitive credential detected and excluded.",
                raw_source_text="[credential value redacted]",
                source_locator="document",
                confidence="HIGH",
            )
        )

    # Appointment grids: headers only — no profile extraction flood
    if dtype == "Appointment Grid" or (
        hint == "Appointment Grid" and dtype in {"Other", "Appointment Grid"}
    ):
        chosen = None
        for prev in parsed.sheet_previews:
            headers = [str(h) for h in (prev.get("headers") or [])]
            sheet = str(prev.get("sheet") or "")
            joined = " ".join(headers).lower()
            if "record number" in joined or sheet.lower() == "appointments":
                chosen = prev
                break
        if chosen is None and parsed.sheet_previews:
            chosen = parsed.sheet_previews[0]
        if chosen:
            headers = [str(h) for h in (chosen.get("headers") or [])]
            findings.append(
                RawFinding(
                    section="unmapped",
                    field_name="appointment_grid_headers",
                    proposed_value=json.dumps(headers, ensure_ascii=False),
                    raw_source_text=f"Sheet {chosen.get('sheet')}: {', '.join(map(str, headers))}",
                    source_locator=f"sheet:{chosen.get('sheet')}",
                    confidence="HIGH",
                    suggested_document_type="Appointment Grid",
                )
            )
        return findings, notes, hint

    lines = parsed_clean.lines
    text = parsed_clean.text

    def add(
        section: str,
        field: str,
        value: str,
        raw: str,
        locator: str = "",
        confidence: str = "HIGH",
    ) -> None:
        value = _blank(value)
        if not value:
            return
        # Never store secrets
        if _PASSWORD_LINE_RE.search(value) or (
            _SENSITIVE_RE.search(value) and "REDACTED" not in value
        ):
            notes.append("Sensitive credential detected and excluded.")
            return
        findings.append(
            RawFinding(
                section=section,
                field_name=field,
                proposed_value=value,
                raw_source_text=_blank(raw) or value[:500],
                source_locator=locator,
                confidence=confidence,
                suggested_document_type=hint,
            )
        )

    # Email / appointment templates: preserve body + type only (placeholders intact)
    if dtype in {"Email Template", "Appointment Set Template"} or re.search(
        r"(?i)send email template|appt set template", parsed.filename + text[:500]
    ):
        body_start = next(
            (i for i, ln in enumerate(lines) if re.match(r"(?i)^hi\s*\[name\]", ln)),
            None,
        )
        if body_start is not None:
            body = "\n".join(lines[body_start:])
            add("email_templates", "body", body, lines[body_start], confidence="HIGH")
        if re.search(r"(?i)send email", parsed.filename + text[:200]):
            add(
                "email_templates",
                "template_type",
                "Send Information / Follow-Up",
                "Send Email template",
                confidence="HIGH",
            )
            add(
                "email_templates",
                "template_name",
                "Carmeco Send Email Template",
                parsed.filename,
                confidence="MEDIUM",
            )
        if re.search(r"(?i)appt set|appointment set", parsed.filename + text[:200]):
            add(
                "email_templates",
                "template_type",
                "Appointment Confirmation",
                "Appt set template",
                confidence="HIGH",
            )
            add(
                "email_templates",
                "template_name",
                "Carmeco Appointment Set Template",
                parsed.filename,
                confidence="MEDIUM",
            )
        # Deduplicate and return early — do not invent profile fields from templates
        best: dict[tuple[str, str], RawFinding] = {}
        rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        out: list[RawFinding] = []
        for f in findings:
            key = (f.section, f.field_name)
            prev = best.get(key)
            if not prev or rank.get(f.confidence, 0) >= rank.get(prev.confidence, 0):
                best[key] = f
        out.extend(best.values())
        return out, notes, hint

    # --- Shared labeled profile fields ---
    for labels, field, conf in [
        (["Client"], "client_name", "HIGH"),
        (["Web Site", "Website", "Web site"], "website", "HIGH"),
        (["Address"], "address", "HIGH"),
        (["Phone", "Main Phone"], "main_phone", "MEDIUM"),
        (
            ["Revenue Specialist", "NorthStar Owner", "Account Owner"],
            "primary_owner_name",
            "HIGH",
        ),
        (
            [
                "Who is taking Appointments",
                "Who Takes Appointments",
                "Who is taking appointments",
            ],
            "who_takes_appointments",
            "HIGH",
        ),
    ]:
        val, raw = _labeled_value(lines, labels)
        if not val:
            val, raw = _inline_labeled(text, labels)
        if field == "client_name":
            v2, r2 = _labeled_value(lines, ["Client"])
            if not v2:
                v2, r2 = _inline_labeled(text, ["Client"])
            if v2 and v2.lower() not in {"date"}:
                val, raw = v2, r2
        if field == "who_takes_appointments" and val:
            # Strip commercial paragraph accidentally joined after appointment names
            val = re.split(r"\s*\|\s*Since\s+\d{4}", val, maxsplit=1)[0]
            val = re.sub(r"(?i)^option\s*\d+\s*[-–]\s*", "", val).strip()
            add(
                "client_operations",
                "who_takes_appointments",
                val,
                raw,
                confidence=conf,
            )
            continue
        if field == "website" and not val:
            m = re.search(r"(?i)https?://[^\s|]+|www\.[^\s|]+", text)
            if m:
                val, raw, conf = m.group(0).rstrip(").,"), m.group(0), "MEDIUM"
        if field == "website" and val and not re.search(
            r"(?i)https?://|www\.|\.com|\.net|\.org", val
        ):
            # Ignore non-URL "Website" action-item headings
            m = re.search(r"(?i)https?://[^\s|]+|www\.[^\s|]+", text)
            if m:
                val, raw, conf = m.group(0).rstrip(").,"), m.group(0), "MEDIUM"
            else:
                val, raw = "", ""
        if field == "main_phone" and val:
            # Keep phone digits only segment when email joined
            pm = re.search(r"\(\d{3}\)\s*\d{3}[-–]?\d{4}|\d{3}[-–]\d{3}[-–]\d{4}", val)
            if pm:
                val = pm.group(0)
        if field == "main_phone" and (not val or len(re.sub(r"\D", "", val)) < 7):
            m = re.search(r"\(\d{3}\)\s*\d{3}[-–]?\d{4}|\d{3}[-–]\d{3}[-–]\d{4}", text)
            if m:
                val, raw, conf = m.group(0), m.group(0), "MEDIUM"
        add("client_profile", field, val, raw, confidence=conf)

    # Products / services row (Client Information tables)
    for i, line in enumerate(lines):
        if re.search(r"(?i)products\s*/\s*services", line):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            # Prefer right-side / non-facility tokens
            parts = [p.strip() for p in re.split(r"\s*\|\s*", nxt) if p.strip()]
            prod = ""
            for p in parts:
                if re.search(r"(?i)square\s*feet|sq\.?\s*ft", p):
                    add("capabilities", "facility", p, p, confidence="HIGH")
                elif re.search(
                    r"(?i)welding|stamping|fabrication|coating|laser|assembly|press|engineering",
                    p,
                ):
                    prod = p
            if prod:
                add(
                    "strategy",
                    "secondary_services",
                    prod,
                    "Products/Services",
                    confidence="HIGH",
                )
                add(
                    "capabilities",
                    "production_processes",
                    prod,
                    "Products/Services",
                    confidence="MEDIUM",
                )
            break

    # 30-second commercial may be merged after appointment line
    for line in lines:
        if re.search(r"(?i)since\s+1970.*stamping|metal fabrications for original equipment", line):
            m = re.search(r"(Since\s+1970.+)$", line)
            if m:
                add(
                    "call_playbook",
                    "thirty_second_commercial",
                    m.group(1),
                    "30 Second Commercial",
                    confidence="HIGH",
                )
            break

    contacts, dms, loc = _extract_contacts(lines, text)
    # Client staff people → Client Contacts (not CRM prospect contacts / not profile blob)
    add("client_contacts", "client_contacts", contacts, loc or contacts, confidence="HIGH")
    add("client_profile", "decision_makers", dms, loc or dms, confidence="HIGH")

    # Appointment handling from Nstar / roadmap → Client Operations
    for line in lines:
        if re.search(r"(?i)take appointments|will take appointments|cc all on recaps", line):
            add(
                "client_operations",
                "appointment_handling_instructions",
                line,
                line,
                confidence="HIGH",
            )
            add(
                "client_operations",
                "who_takes_appointments",
                line,
                line,
                confidence="HIGH",
            )
        if re.search(r"(?i)phone or google meet|avoid fridays|site visits", line):
            add(
                "call_playbook",
                "appointment_instructions",
                line,
                line,
                confidence="HIGH",
            )

    # Reason hired
    val, raw = _labeled_value(
        lines,
        [
            "Reason They Decided on NorthStar",
            "Reason They Chose NorthStar",
            "Reason Client Hired NorthStar",
        ],
        max_follow=4,
    )
    if val:
        add("strategy", "reason_hired", val, raw, confidence="HIGH")

    # Products/services list from Client Information
    prod_block, prod_raw = _section_block(
        lines,
        r"^Products/Services",
        [r"^30\s*Second", r"^Who is taking", r"^Industries", r"^I\."],
    )
    if prod_block:
        add(
            "strategy",
            "secondary_services",
            prod_block,
            prod_raw or "Products/Services",
            confidence="HIGH",
        )
        # Primary from leading with stamping language elsewhere
        add(
            "capabilities",
            "production_processes",
            prod_block,
            prod_raw or "Products/Services",
            confidence="MEDIUM",
        )

    # 30-second commercial
    commercial, c_raw = _section_block(
        lines,
        r"30[-\s]*Second Commercial|II\.\s*.*30",
        [
            r"^III\.",
            r"^Questions for Callers",
            r"^Who is taking",
            r"^IV\.",
            r"^V\.",
            r"^Capabilities",
        ],
    )
    if not commercial:
        # Client info: heading then paragraph after Who is taking / Option
        idx = next(
            (i for i, ln in enumerate(lines) if re.search(r"(?i)30\s*second commercial", ln)),
            None,
        )
        if idx is not None:
            parts = []
            for ln in lines[idx + 1 :]:
                if re.search(r"(?i)^who is taking|^option\s*\d|^account information", ln):
                    continue
                if len(ln) > 80:
                    parts.append(ln)
                if len(parts) >= 2:
                    break
            commercial = _collapse("\n".join(parts))
            c_raw = lines[idx]
    add(
        "call_playbook",
        "thirty_second_commercial",
        commercial,
        c_raw,
        confidence="HIGH",
    )

    # Road map / strategy sections
    titles, t_raw = _section_block(
        lines,
        r"Titles of Key Decision Makers|^Titles$|I\.\s*.*Titles",
        [r"^II\.", r"^30", r"^Questions", r"^Ideal Customer", r"^Geography"],
    )
    if titles:
        add("strategy", "target_titles", titles, t_raw, confidence="HIGH")

    questions, q_raw = _section_block(
        lines,
        r"Questions for Callers|III\.\s*.*Questions",
        [r"^IV\.", r"^Common Objections", r"^V\."],
    )
    add("call_playbook", "discovery_questions", questions, q_raw, confidence="HIGH")

    objections, o_raw = _section_block(
        lines,
        r"Common Objections|IV\.\s*.*Objection",
        [r"^V\.", r"^Value Proposition"],
    )
    if objections:
        add("call_playbook", "common_objections", objections, o_raw, confidence="HIGH")
        # Split responses if Objection: lines present
        objs = []
        resps = []
        cur_o = None
        for ln in objections.split("\n") if "\n" in objections else objections.split(". "):
            ln = _blank(ln)
            if not ln:
                continue
            if re.match(r"(?i)^objection\s*:", ln):
                cur_o = re.sub(r"(?i)^objection\s*:\s*", "", ln)
                objs.append(cur_o)
            elif cur_o and not re.match(r"(?i)^objection", ln):
                resps.append(f"{cur_o} → {ln}")
        if objs:
            add(
                "call_playbook",
                "common_objections",
                "; ".join(objs),
                o_raw,
                confidence="HIGH",
            )
        if resps:
            add(
                "call_playbook",
                "objection_responses",
                "; ".join(resps),
                o_raw,
                confidence="MEDIUM",
            )

    values, v_raw = _section_block(
        lines,
        r"Value Proposition|V\.\s*.*Value",
        [r"^VI\.", r"^Capabilities", r"^VII\.", r"^Close"],
    )
    add("call_playbook", "value_propositions", values, v_raw, confidence="HIGH")

    caps, cap_raw = _section_block(
        lines,
        r"^VI\.\s*.*Capabilities|^Capabilities\b|Capabilities - Lead",
        [r"^VII\.", r"^Close", r"^Current Sales", r"^Industries", r"^What are we Selling"],
    )
    if caps:
        add("capabilities", "production_processes", caps, cap_raw, confidence="HIGH")
        cap_bits: list[str] = []
        if re.search(r"(?i)20\s*tons?\s*to\s*1,?200|20–1,200|20-1,200", caps):
            cap_bits.append("Metal stamping 20–1,200 tons")
        if re.search(r"(?i)30,?000\s*lb|60\s*in", caps):
            cap_bits.append("Coil capacity 30,000 lb / 60 inches wide")
        if cap_bits:
            add(
                "capabilities",
                "capacity",
                "; ".join(cap_bits),
                cap_raw,
                confidence="HIGH",
            )
        if re.search(r"(?i)63\s*[x×]\s*157", caps):
            add(
                "capabilities",
                "equipment",
                "Bed size up to 63 × 157 inches",
                cap_raw,
                confidence="HIGH",
            )
        if re.search(r"(?i)\.013|\.5\s*\(inches\)|thin|thick", caps):
            add(
                "capabilities",
                "materials",
                "Material thickness .013–.5 inches",
                cap_raw,
                confidence="HIGH",
            )
        if re.search(r"(?i)OTR trucks|logistics", caps):
            add(
                "capabilities",
                "logistics",
                "Own OTR trucks",
                cap_raw,
                confidence="HIGH",
            )
        finish_lines = [
            ln
            for ln in caps.split("\n")
            if re.search(r"(?i)paint|powder|plating|finish", ln)
            and not re.search(r"(?i)lead with stamping|metal stamping|laser", ln)
        ]
        if finish_lines:
            add(
                "capabilities",
                "finishing",
                _collapse("\n".join(finish_lines)),
                cap_raw,
                confidence="MEDIUM",
            )

    # Strategy session specifics
    if dtype not in {"Email Template", "Appointment Set Template"}:
        if re.search(r"(?i)primary focus is stamping|lead with stamping", text):
            add(
                "strategy",
                "primary_service",
                "Metal stamping",
                "Lead with Stamping / primary focus is stamping",
                confidence="HIGH",
            )
        if re.search(
            r"(?i)fabrication supports this|secondary.*fabrication|fabrication supports",
            text,
        ):
            add(
                "strategy",
                "secondary_services",
                "Fabrication",
                "Fabrication supports stamping",
                confidence="MEDIUM",
            )

    if re.search(r"(?i)ISO\s*9001", text):
        add(
            "capabilities",
            "certifications",
            "ISO 9001",
            "ISO 9001 Certified",
            confidence="HIGH",
        )
    m = re.search(r"(?i)(160,?000\s*(?:ft|square feet|sq\.?\s*ft)[^\n]*)", text)
    if m:
        add("capabilities", "facility", _collapse(m.group(1)), m.group(1), confidence="HIGH")
    elif re.search(r"(?i)158,?000\s*Square Feet", text):
        add(
            "capabilities",
            "facility",
            "158,000 Square Feet",
            "158,000 Square Feet",
            confidence="HIGH",
        )

    sales, s_raw = _section_block(
        lines,
        r"Sales Goals",
        [r"^What are we Selling", r"^Capabilities", r"^Current Sales", r"^Industries"],
    )
    add("strategy", "sales_goals", sales, s_raw, confidence="HIGH")

    industries, i_raw = _section_block(
        lines,
        r"Industries to Focus On",
        [r"^Ideal Customer", r"^Titles", r"^Geography", r"^Products"],
    )
    if not industries:
        # Avoid Client Info "Industries Served:" empty header that shares a row with Products
        for i, line in enumerate(lines):
            if re.match(r"(?i)^industries to focus on", line.strip()):
                industries, i_raw = _section_block(
                    lines[i:],
                    r"Industries to Focus On",
                    [r"^Ideal Customer", r"^Titles", r"^Geography"],
                )
                break
    add("strategy", "target_industries", industries, i_raw, confidence="HIGH")

    icp, icp_raw = _section_block(
        lines,
        r"Ideal Customer Profile",
        [r"^Titles", r"^Geography", r"^Challenges", r"^Competitors"],
    )
    add("strategy", "ideal_customer_profile", icp, icp_raw, confidence="HIGH")

    geo, g_raw = _labeled_value(
        lines, ["Geography for NorthStar to Work"], max_follow=2
    )
    if geo:
        geo = re.split(r"(?i)Challenges in the Business", geo)[0].strip()
    if not geo:
        m = re.search(r"(?i)600\s*mi(?:le)?s?\s*radius[^\n|]*", text)
        if m:
            geo, g_raw = m.group(0), m.group(0)
    add("strategy", "geographic_preferences", geo, g_raw, confidence="HIGH")

    # Positive / negative from ICP & challenges
    if icp:
        pos = []
        neg = []
        # Prefer bullet-like splits
        chunks = re.split(r"[\n•]| (?=[A-Z][a-z])", icp)
        if len(chunks) <= 1:
            chunks = re.split(r"\s{2,}|(?<=\.)\s+", icp)
        for ln in chunks:
            ln = _blank(ln)
            if not ln:
                continue
            if re.search(r"(?i)(^|\b)(not ideal|no ongoing frequent)", ln):
                neg.append(ln)
            else:
                pos.append(ln)
        add(
            "strategy",
            "positive_fit_signals",
            "; ".join(pos),
            icp_raw,
            confidence="MEDIUM",
        )
        add(
            "strategy",
            "negative_fit_signals",
            "; ".join(neg),
            icp_raw,
            confidence="MEDIUM",
        )
    # Also capture positive bullets when ICP block is flat
    if not icp:
        pos_hits = re.findall(
            r"(?im)^(?:Ongoing needs|Growth potential|Overflow issues|May have stamping).+$",
            text,
        )
        if pos_hits:
            add(
                "strategy",
                "positive_fit_signals",
                "; ".join(_collapse(x) for x in pos_hits),
                "Ideal Customer Profile",
                confidence="MEDIUM",
            )

    challenges, ch_raw = _section_block(
        lines,
        r"Challenges in the Business",
        [r"^Competitors", r"^Additional Info", r"^Action Items"],
    )
    if challenges:
        add(
            "unmapped",
            "unmapped_intelligence",
            f"Sales process/challenges: {challenges}",
            ch_raw,
            confidence="LOW",
        )

    # Capabilities spreadsheet
    if dtype == "Capabilities" or "capabilities" in parsed.filename.lower():
        for prev in parsed.sheet_previews:
            headers = [str(h) for h in (prev.get("headers") or [])]
            add(
                "unmapped",
                "unmapped_intelligence",
                f"Capabilities columns: {', '.join(headers)}",
                f"Sheet {prev.get('sheet')} headers",
                confidence="HIGH",
            )
            # Flatten sample capability tokens
            tokens: list[str] = []
            for row in prev.get("sample_rows") or []:
                for cell in row:
                    c = _blank(cell)
                    if c and c.lower() not in {"in-house", "outsourced"}:
                        tokens.append(c)
            if tokens:
                add(
                    "capabilities",
                    "production_processes",
                    "; ".join(tokens[:40]),
                    "Capabilities spreadsheet rows",
                    confidence="MEDIUM",
                )
            # Certifications column values
            certs = [
                t
                for t in tokens
                if re.search(r"(?i)ISO|IATF|cert", t)
            ]
            if certs:
                add(
                    "capabilities",
                    "certifications",
                    "; ".join(dict.fromkeys(certs)),
                    "Capabilities certifications",
                    confidence="HIGH",
                )

    # Safe email identity from Nstar (not password) → Client Operations
    if "nstar" in parsed.filename.lower() or re.search(r"(?i)northstar email", text):
        m = re.search(r"(?i)(projects@carmeco\.com)", text) or re.search(
            r"(?i)([\w.+-]+@carmeco\.com)", text
        )
        if m:
            add(
                "client_operations",
                "northstar_client_email",
                m.group(1),
                f"NorthStar email account identity: {m.group(1)}",
                confidence="HIGH",
            )


    # Deduplicate by section+field keeping highest confidence / longest value
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    best: dict[tuple[str, str], RawFinding] = {}
    for f in findings:
        key = (f.section, f.field_name)
        prev = best.get(key)
        if not prev or rank.get(f.confidence, 0) > rank.get(prev.confidence, 0):
            best[key] = f
        elif (
            rank.get(f.confidence, 0) == rank.get(prev.confidence, 0)
            and len(f.proposed_value) > len(prev.proposed_value)
        ):
            best[key] = f
    # Keep multiple unmapped entries
    out: list[RawFinding] = []
    seen_unmapped: set[str] = set()
    for f in findings:
        if f.section == "unmapped":
            sig = _norm(f.proposed_value)[:120]
            if sig in seen_unmapped:
                continue
            seen_unmapped.add(sig)
            out.append(f)
        else:
            key = (f.section, f.field_name)
            if best.get(key) is f:
                out.append(f)
    return out, notes, hint


def load_current_values(client_id: int) -> dict[tuple[str, str], str]:
    """Map (section, field) -> existing value from setup / knowledge / templates."""
    current: dict[tuple[str, str], str] = {}
    with get_connection() as conn:
        cl = conn.execute(
            "SELECT name FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if cl:
            current[("client_profile", "client_name")] = _blank(cl["name"])
        prof = conn.execute(
            "SELECT * FROM client_profiles WHERE client_id = ?", (client_id,)
        ).fetchone()
        if prof:
            current[("client_profile", "website")] = _blank(prof["website"])
            current[("client_profile", "address")] = _blank(prof["main_location"])
            current[("client_profile", "main_phone")] = _blank(prof["main_phone"])
            current[("client_profile", "primary_owner_name")] = _blank(
                prof["primary_owner_name"]
            )
            current[("strategy", "primary_service")] = _blank(prof["primary_service"])
            current[("strategy", "secondary_services")] = _blank(
                prof["secondary_services"]
            )
            current[("capabilities", "certifications")] = _blank(prof["certifications"])
            current[("capabilities", "capacity")] = _blank(prof["equipment_capacity"])
            current[("call_playbook", "value_propositions")] = _blank(
                prof["value_proposition"]
            )
        camp = conn.execute(
            """
            SELECT * FROM client_campaigns
            WHERE client_id = ? AND is_default = 1
            ORDER BY id LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        if camp:
            current[("strategy", "primary_service")] = _blank(camp["primary_service"]) or current.get(
                ("strategy", "primary_service"), ""
            )
            current[("strategy", "secondary_services")] = _blank(
                camp["secondary_services"]
            ) or current.get(("strategy", "secondary_services"), "")
            current[("strategy", "target_industries")] = _blank(
                camp["target_industries"]
            ) or _blank(camp["target_customer_types"])
            current[("strategy", "ideal_customer_profile")] = _blank(
                camp["target_customer_types"]
            )
            current[("strategy", "product_part_characteristics")] = _blank(
                camp["target_products"]
            )
            current[("strategy", "manufacturing_processes_sought")] = _blank(
                camp["manufacturing_processes_sought"]
            )
            current[("strategy", "production_preference")] = _blank(
                camp["production_preference"]
            )
            current[("strategy", "geographic_preferences")] = _blank(
                camp["geographic_preferences"]
            )
            current[("strategy", "positive_fit_signals")] = _blank(camp["positive_signals"])
            current[("strategy", "negative_fit_signals")] = _blank(camp["negative_signals"])
            current[("strategy", "exclusions")] = _blank(camp["exclusions"])
            current[("strategy", "target_titles")] = _blank(camp["target_titles"])
        for row in conn.execute(
            "SELECT section_key, payload_json FROM client_knowledge_sections WHERE client_id = ?",
            (client_id,),
        ).fetchall():
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                continue
            section = _blank(row["section_key"])
            for k, v in payload.items():
                if _blank(v):
                    current[(section, str(k))] = _blank(v)
        # Email templates: store composite existing for type/name
        trows = conn.execute(
            "SELECT template_name, template_type, subject, body FROM client_email_templates WHERE client_id = ?",
            (client_id,),
        ).fetchall()
        if trows:
            current[("email_templates", "template_name")] = "; ".join(
                _blank(r["template_name"]) for r in trows if _blank(r["template_name"])
            )
            current[("email_templates", "template_type")] = "; ".join(
                _blank(r["template_type"]) for r in trows if _blank(r["template_type"])
            )
    return current


def classify_finding(
    section: str, field_name: str, existing: str, proposed: str, confidence: str
) -> str:
    if field_name == "sensitive_exclusion" or section == "sensitive":
        return "SENSITIVE — NOT IMPORTED"
    if section == "unmapped" or field_name.startswith("unmapped"):
        return "UNMAPPED"
    if field_name not in FIELD_CATALOG.get(section, {}):
        return "UNMAPPED"

    ex = _norm(existing)
    pr = _norm(proposed)
    if not pr:
        return "UNMAPPED"
    if not ex:
        return "NEW INFORMATION"
    if ex == pr or ex in pr or pr in ex:
        return "MATCH"
    # Soft synonym: stamping
    if "stamping" in ex and "stamping" in pr:
        return "MATCH"
    # Overlap tokens
    et = set(ex.split())
    pt = set(pr.split())
    if et and pt:
        overlap = len(et & pt) / max(1, len(et | pt))
        if overlap >= 0.6:
            return "POTENTIAL UPDATE"
        if overlap <= 0.15 and confidence == "HIGH":
            return "CONFLICT"
    if confidence == "LOW":
        return "POTENTIAL UPDATE"
    return "POTENTIAL UPDATE"
