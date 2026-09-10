"""Deterministic Brown Industries history blob → per-event CSV converter.

Converts LeadMaster-style per-contact Notes blobs into one row per
recognizable activity event. Does not invent timestamps, authors, or types.
Rerunning on the same bytes yields identical Source Event IDs and rows.

Usage:
  python brown_history_event_convert.py INPUT.csv [--out OUTPUT.csv]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from client_data_history_notes import is_routine_marketing_send_note, normalize_history_note_text

OUTPUT_HEADERS = [
    "Record No.",
    "Contact No.",
    "Company",
    "Source Event ID",
    "Event Timestamp",
    "Author",
    "Event Type",
    "Note Text",
    "Original Source Row",
    "Review Flag",
]

# Timestamp + actor header lines used as reliable event boundaries.
_EVENT_HEADER_RE = re.compile(
    r"""
    ^
    (?P<ts>
        \d{1,2}-[A-Za-z]{3}-\d{4}
        \s+\d{1,2}:\d{2}\s*(?:AM|PM)
        (?:\(GMT[^)]*\))?
        (?:\s+Central\s+Time\s*\([^)]*\))?
        (?:\s+[A-Z]{2,5})?
    )
    \s*-\s*
    (?P<rest>.+)
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

_TASKS_ADDED_RE = re.compile(r"^Tasks added to contact\b", re.IGNORECASE)
_QUICK_ACTIONS_RE = re.compile(r"\(Quick Actions\)\s*$", re.IGNORECASE)
_STATUS_CHANGED_RE = re.compile(r"\(Status changed\b", re.IGNORECASE)
_EVENT_PAREN_RE = re.compile(r"\(Event:\s*", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_html_light(text: str) -> str:
    cleaned = _HTML_TAG_RE.sub(" ", text or "")
    return re.sub(r"[ \t]+\n", "\n", cleaned).strip()


@dataclass(frozen=True, slots=True)
class ConvertedHistoryEvent:
    record_no: str
    contact_no: str
    company: str
    source_event_id: str
    event_timestamp: str
    author: str
    event_type: str
    note_text: str
    original_source_row: int
    review_flag: str = ""
    excluded_marketing: bool = False


@dataclass
class BrownHistoryConversionResult:
    events: list[ConvertedHistoryEvent] = field(default_factory=list)
    source_row_count: int = 0
    blank_rows_skipped: int = 0
    blank_history_skipped: int = 0
    marketing_excluded: int = 0
    parsed_events: int = 0
    unparseable_retained: int = 0
    output_rows: int = 0


def deterministic_source_event_id(
    *,
    record_no: str,
    contact_no: str,
    original_source_row: int,
    event_index: int,
    event_timestamp: str,
    author: str,
    event_type: str,
    note_text: str,
) -> str:
    """Stable id from source identity + content (identical on rerun)."""
    payload = "|".join(
        [
            _blank(record_no),
            _blank(contact_no),
            str(int(original_source_row)),
            str(int(event_index)),
            _blank(event_timestamp),
            _blank(author),
            _blank(event_type),
            normalize_history_note_text(note_text),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_header_rest(rest: str) -> tuple[str, str]:
    """Return (author, event_type) from text after the timestamp dash."""
    text = _blank(rest)
    if not text:
        return "", ""
    if re.search(r"^e-?marketing\b", text, re.IGNORECASE):
        return "E-Marketing", "E-Marketing"
    if _QUICK_ACTIONS_RE.search(text):
        author = _QUICK_ACTIONS_RE.sub("", text).strip()
        return author, "Quick Actions"
    if _STATUS_CHANGED_RE.search(text):
        m = re.match(r"^(?P<author>.+?)\s*\((?P<body>Status changed[^)]*)\)\s*$", text, re.I)
        if m:
            return _blank(m.group("author")), _blank(m.group("body"))
        return text, "Status changed"
    if _EVENT_PAREN_RE.search(text):
        m = re.match(r"^(?P<author>.+?)\s*\((?P<body>Event:[^)]*)\)\s*$", text, re.I)
        if m:
            return _blank(m.group("author")), _blank(m.group("body"))
        author = text.split("(", 1)[0].strip()
        return author, "Event"
    return text, ""


def _is_boundary_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _TASKS_ADDED_RE.match(s):
        return True
    return bool(_EVENT_HEADER_RE.match(s))


def _split_blob_into_raw_chunks(blob: str) -> list[str]:
    """Split on reliable timestamp/actor or Tasks-added boundaries only."""
    text = _normalize_newlines(blob).strip()
    if not text:
        return []
    lines = text.split("\n")
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _is_boundary_line(line) and current:
            chunks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append(current)
    return ["\n".join(c).strip() for c in chunks if "\n".join(c).strip()]


def _classify_chunk(chunk: str) -> tuple[str, str, str, str, bool]:
    """Return timestamp, author, event_type, note_text, parse_ok."""
    lines = [ln for ln in _normalize_newlines(chunk).split("\n")]
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return "", "", "", "", False

    first = nonempty[0].strip()
    if _TASKS_ADDED_RE.match(first):
        # Prefer following timestamp header when present.
        if len(nonempty) >= 2 and _EVENT_HEADER_RE.match(nonempty[1].strip()):
            header = nonempty[1].strip()
            m = _EVENT_HEADER_RE.match(header)
            assert m is not None
            author, event_type = _parse_header_rest(m.group("rest"))
            if not event_type:
                event_type = "Task"
            body_lines = [first, header, *nonempty[2:]]
            return (
                _blank(m.group("ts")),
                author,
                event_type,
                "\n".join(body_lines).strip(),
                True,
            )
        return "", "", "Task", chunk.strip(), True

    m = _EVENT_HEADER_RE.match(first)
    if m:
        author, event_type = _parse_header_rest(m.group("rest"))
        body = "\n".join(nonempty[1:]).strip()
        note = first if not body else f"{first}\n{body}"
        return _blank(m.group("ts")), author, event_type, note.strip(), True

    return "", "", "", chunk.strip(), False


def convert_note_blob(
    *,
    note_text: str,
    record_no: str,
    contact_no: str,
    company: str,
    original_source_row: int,
) -> list[ConvertedHistoryEvent]:
    """Convert one Notes/Contact_Note blob into zero or more events."""
    raw = _normalize_newlines(note_text or "").strip()
    if not raw:
        return []
    raw = _strip_html_light(raw) if "<" in raw and ">" in raw else raw
    if not normalize_history_note_text(raw):
        return []

    chunks = _split_blob_into_raw_chunks(raw)
    if not chunks:
        return []

    # If nothing looked like a boundary, treat whole blob as one unparseable retain.
    if len(chunks) == 1 and not _is_boundary_line(chunks[0].split("\n", 1)[0]):
        if is_routine_marketing_send_note(chunks[0]):
            sid = deterministic_source_event_id(
                record_no=record_no,
                contact_no=contact_no,
                original_source_row=original_source_row,
                event_index=1,
                event_timestamp="",
                author="",
                event_type="",
                note_text=chunks[0],
            )
            return [
                ConvertedHistoryEvent(
                    record_no=record_no,
                    contact_no=contact_no,
                    company=company,
                    source_event_id=sid,
                    event_timestamp="",
                    author="",
                    event_type="",
                    note_text=chunks[0],
                    original_source_row=original_source_row,
                    excluded_marketing=True,
                )
            ]
        sid = deterministic_source_event_id(
            record_no=record_no,
            contact_no=contact_no,
            original_source_row=original_source_row,
            event_index=1,
            event_timestamp="",
            author="",
            event_type="",
            note_text=chunks[0],
        )
        return [
            ConvertedHistoryEvent(
                record_no=record_no,
                contact_no=contact_no,
                company=company,
                source_event_id=sid,
                event_timestamp="",
                author="",
                event_type="",
                note_text=chunks[0],
                original_source_row=original_source_row,
                review_flag="unparseable_retained",
            )
        ]

    out: list[ConvertedHistoryEvent] = []
    event_index = 0
    for chunk in chunks:
        ts, author, event_type, note, parse_ok = _classify_chunk(chunk)
        if not normalize_history_note_text(note):
            continue
        event_index += 1
        marketing = is_routine_marketing_send_note(note)
        review = "" if parse_ok else "unparseable_retained"
        sid = deterministic_source_event_id(
            record_no=record_no,
            contact_no=contact_no,
            original_source_row=original_source_row,
            event_index=event_index,
            event_timestamp=ts,
            author=author,
            event_type=event_type,
            note_text=note,
        )
        out.append(
            ConvertedHistoryEvent(
                record_no=record_no,
                contact_no=contact_no,
                company=company,
                source_event_id=sid,
                event_timestamp=ts,
                author=author,
                event_type=event_type,
                note_text=note,
                original_source_row=original_source_row,
                review_flag=review,
                excluded_marketing=marketing,
            )
        )
    return out


def _row_is_fully_blank(row: dict[str, str]) -> bool:
    return not any(_blank(v) for v in row.values())


def convert_brown_history_csv(
    content: bytes | str,
    *,
    source_filename: str = "",
) -> BrownHistoryConversionResult:
    """Convert Brown history CSV bytes into per-event rows + stats."""
    del source_filename  # reserved for future provenance labeling
    text = content.decode("utf-8-sig") if isinstance(content, (bytes, bytearray)) else content
    reader = csv.DictReader(io.StringIO(text))
    result = BrownHistoryConversionResult()
    if not reader.fieldnames:
        return result

    for idx, raw in enumerate(reader, start=2):
        result.source_row_count += 1
        row = {(_blank(k)): ("" if v is None else str(v)) for k, v in raw.items() if _blank(k)}
        if _row_is_fully_blank(row):
            result.blank_rows_skipped += 1
            continue
        notes = _normalize_newlines(row.get("Notes") or "")
        contact_note = _normalize_newlines(row.get("Contact_Note") or "")
        body = notes.strip()
        if not body and contact_note.strip():
            body = contact_note
        if not body.strip():
            result.blank_history_skipped += 1
            continue

        record_no = _blank(row.get("Record No."))
        contact_no = _blank(row.get("Contact No."))
        company = _blank(row.get("Company"))
        events = convert_note_blob(
            note_text=body,
            record_no=record_no,
            contact_no=contact_no,
            company=company,
            original_source_row=idx,
        )
        for ev in events:
            if ev.excluded_marketing:
                result.marketing_excluded += 1
                continue
            if ev.review_flag == "unparseable_retained":
                result.unparseable_retained += 1
            else:
                result.parsed_events += 1
            result.events.append(ev)

    result.output_rows = len(result.events)
    return result


def conversion_result_to_csv_bytes(result: BrownHistoryConversionResult) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_HEADERS, lineterminator="\n")
    writer.writeheader()
    for ev in result.events:
        writer.writerow(
            {
                "Record No.": ev.record_no,
                "Contact No.": ev.contact_no,
                "Company": ev.company,
                "Source Event ID": ev.source_event_id,
                "Event Timestamp": ev.event_timestamp,
                "Author": ev.author,
                "Event Type": ev.event_type,
                "Note Text": ev.note_text,
                "Original Source Row": str(ev.original_source_row),
                "Review Flag": ev.review_flag,
            }
        )
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def convert_brown_history_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
) -> BrownHistoryConversionResult:
    path = Path(input_path)
    content = path.read_bytes()
    result = convert_brown_history_csv(content, source_filename=path.name)
    if output_path is not None:
        Path(output_path).write_bytes(conversion_result_to_csv_bytes(result))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Brown_Industries_History_Import_1270.csv path")
    parser.add_argument(
        "--out",
        default="",
        help="Output CSV path (default: <input>_events.csv beside input)",
    )
    args = parser.parse_args(argv)
    inp = Path(args.input)
    out = Path(args.out) if args.out else inp.with_name(f"{inp.stem}_events.csv")
    result = convert_brown_history_file(inp, out)
    print(f"Wrote {out}")
    print(f"source_rows={result.source_row_count}")
    print(f"blank_rows_skipped={result.blank_rows_skipped}")
    print(f"blank_history_skipped={result.blank_history_skipped}")
    print(f"marketing_excluded={result.marketing_excluded}")
    print(f"parsed_events={result.parsed_events}")
    print(f"unparseable_retained={result.unparseable_retained}")
    print(f"output_rows={result.output_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
