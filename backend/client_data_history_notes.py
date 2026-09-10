"""Client Data Import history-note safeguards.

Marketing-send exclusion and duplicate detection for chronological history
events imported into company_shared_history_events (and compared against
legacy_notes / activities already in NorthStar).

Does not overwrite existing notes. Exact duplicates are skipped.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

# Lines / short notes that are routine automated marketing sends only.
_MARKETING_LINE_RE = re.compile(
    r"""
    ^\s*(?:
        marketing\s+email\s+sent
      | email\s+marketing\s+sent
      | e-?marketing
      | email\s+was\s+sent\s+to\b
      | automated\s+(?:campaign|email)(?:\s+send)?(?:\s+confirmation)?
      | campaign\s+email\s+sent
      | email\s+campaign\s+sent
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Author/event labels that mark automated marketing systems.
_MARKETING_ACTOR_RE = re.compile(r"^\s*e-?marketing\s*$", re.IGNORECASE)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_history_note_text(text: object | None) -> str:
    """Normalize note body for exact-duplicate comparison.

    Collapses whitespace/case, strips simple HTML/entities. Does not alter
    stored note_text — only dedupe keys.
    """
    raw = "" if text is None else str(text)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = html.unescape(raw)
    raw = _HTML_TAG_RE.sub(" ", raw)
    raw = raw.replace("\xa0", " ").replace("&nbsp;", " ")
    collapsed = _WS_RE.sub(" ", raw).strip().casefold()
    return collapsed


def normalize_history_event_at(value: object | None) -> str:
    """Normalize activity timestamps for fingerprinting (trim + casefold)."""
    return blank(value).casefold()


def _strip_marketing_lines(text: str) -> str:
    """Remove routine marketing-send lines; keep everything else."""
    cleaned = html.unescape("" if text is None else str(text))
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _HTML_TAG_RE.sub("\n", cleaned)
    kept: list[str] = []
    for line in cleaned.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # Timestamp + "E-Marketing" on one line is still routine.
        if _MARKETING_ACTOR_RE.search(stripped.split("-")[-1] if "-" in stripped else stripped):
            # e.g. "22-Apr-2026 ... - E-Marketing"
            if _MARKETING_ACTOR_RE.search(stripped.rsplit("-", 1)[-1]):
                continue
        if _MARKETING_LINE_RE.search(stripped):
            continue
        # Combined "… - E-Marketing" headers
        if re.search(r"-\s*e-?marketing\s*$", stripped, re.IGNORECASE):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def is_routine_marketing_send_note(text: object | None) -> bool:
    """True when the note's only meaningful content is a marketing send confirmation.

    Meaningful replies, objections, follow-ups, appointments, and other activity
    are retained even when marketing lines are mixed into the same blob.
    """
    raw = "" if text is None else str(text)
    if not raw.strip():
        return False
    remainder = _strip_marketing_lines(raw)
    if not remainder:
        return True
    # If anything non-marketing remains, keep the note.
    if normalize_history_note_text(remainder):
        return False
    return True


def resolve_history_source_id(
    *,
    source_note_id: object | None = None,
    event_hash: object | None = None,
) -> str:
    """Prefer an explicit LeadMaster note/activity id; fall back to provided hash."""
    for value in (source_note_id, event_hash):
        text = blank(value)
        if text:
            return text
    return ""


def history_content_fingerprint(
    *,
    client_id: int,
    company_id: int | None,
    company_record_no: str,
    contact_key: str,
    note_text: object | None,
    event_at: object | None,
) -> str:
    """Fingerprint for notes that lack a stable LeadMaster source id."""
    company_key = str(int(company_id)) if company_id is not None else blank(company_record_no)
    payload = "|".join(
        [
            str(int(client_id)),
            company_key,
            blank(contact_key),
            normalize_history_note_text(note_text),
            normalize_history_event_at(event_at),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_history_event_hash(
    *,
    client_id: int,
    company_id: int | None = None,
    company_record_no: str = "",
    contact_key: str = "",
    note_text: object | None = None,
    event_at: object | None = None,
    source_note_id: object | None = None,
    event_hash: object | None = None,
) -> str:
    """Stable LeadMaster id when present; otherwise content fingerprint."""
    source_id = resolve_history_source_id(
        source_note_id=source_note_id, event_hash=event_hash
    )
    if source_id:
        return source_id
    return history_content_fingerprint(
        client_id=client_id,
        company_id=company_id,
        company_record_no=company_record_no,
        contact_key=contact_key,
        note_text=note_text,
        event_at=event_at,
    )


@dataclass(frozen=True, slots=True)
class ExistingHistoryDedupeIndex:
    source_ids: frozenset[str]
    content_fingerprints: frozenset[str]
    undated_note_norms: frozenset[str]


@dataclass(slots=True)
class MutableHistoryDedupeIndex:
    """In-memory dedupe keys for within-batch first-occurrence tracking.

    Key rules mirror shared-history rows loaded by
    ``load_existing_history_dedupe_index`` (company-scoped; contact ignored).
    """

    source_ids: set[str]
    content_fingerprints: set[str]
    undated_note_norms: set[str]

    @classmethod
    def empty(cls) -> "MutableHistoryDedupeIndex":
        return cls(source_ids=set(), content_fingerprints=set(), undated_note_norms=set())

    def as_existing(self) -> ExistingHistoryDedupeIndex:
        return ExistingHistoryDedupeIndex(
            source_ids=frozenset(self.source_ids),
            content_fingerprints=frozenset(self.content_fingerprints),
            undated_note_norms=frozenset(self.undated_note_norms),
        )

    def register_shared_event(
        self,
        *,
        client_id: int,
        company_id: int,
        note_text: object | None,
        event_at: object | None,
        event_hash: str,
    ) -> None:
        """Record keys as if this shared-history row were already stored."""
        eh = blank(event_hash)
        if eh:
            self.source_ids.add(eh)
        note_norm = normalize_history_note_text(note_text)
        at_norm = normalize_history_event_at(event_at)
        if not note_norm:
            return
        self.content_fingerprints.add(
            history_content_fingerprint(
                client_id=int(client_id),
                company_id=int(company_id),
                company_record_no="",
                contact_key="",
                note_text=note_text,
                event_at=event_at,
            )
        )
        if not at_norm:
            self.undated_note_norms.add(note_norm)


def load_existing_history_dedupe_index(
    conn,
    *,
    client_id: int,
    company_id: int,
) -> ExistingHistoryDedupeIndex:
    """Build dedupe keys from shared history, legacy notes, and activities."""
    source_ids: set[str] = set()
    content_fps: set[str] = set()
    undated: set[str] = set()

    shared = conn.execute(
        """
        SELECT event_hash, note_text, event_at
        FROM company_shared_history_events
        WHERE company_id = ?
        """,
        (int(company_id),),
    ).fetchall()
    for row in shared:
        eh = blank(row["event_hash"])
        if eh:
            source_ids.add(eh)
        note_norm = normalize_history_note_text(row["note_text"])
        at_norm = normalize_history_event_at(row["event_at"])
        if note_norm:
            content_fps.add(
                history_content_fingerprint(
                    client_id=client_id,
                    company_id=company_id,
                    company_record_no="",
                    contact_key="",
                    note_text=row["note_text"],
                    event_at=row["event_at"],
                )
            )
            if not at_norm:
                undated.add(note_norm)

    legacy = conn.execute(
        """
        SELECT note_text
        FROM legacy_notes
        WHERE company_id = ? AND client_id = ?
        """,
        (int(company_id), int(client_id)),
    ).fetchall()
    for row in legacy:
        note_norm = normalize_history_note_text(row["note_text"])
        if note_norm:
            undated.add(note_norm)
            content_fps.add(
                history_content_fingerprint(
                    client_id=client_id,
                    company_id=company_id,
                    company_record_no="",
                    contact_key="",
                    note_text=row["note_text"],
                    event_at="",
                )
            )

    activities = conn.execute(
        """
        SELECT notes, activity_at, contact_id
        FROM activities
        WHERE company_id = ? AND client_id = ?
        """,
        (int(company_id), int(client_id)),
    ).fetchall()
    for row in activities:
        note_norm = normalize_history_note_text(row["notes"])
        if not note_norm:
            continue
        contact_key = (
            str(int(row["contact_id"])) if row["contact_id"] is not None else ""
        )
        content_fps.add(
            history_content_fingerprint(
                client_id=client_id,
                company_id=company_id,
                company_record_no="",
                contact_key=contact_key,
                note_text=row["notes"],
                event_at=row["activity_at"],
            )
        )
        # Also index company-level (no contact) so undated/company imports match.
        content_fps.add(
            history_content_fingerprint(
                client_id=client_id,
                company_id=company_id,
                company_record_no="",
                contact_key="",
                note_text=row["notes"],
                event_at=row["activity_at"],
            )
        )
        if not normalize_history_event_at(row["activity_at"]):
            undated.add(note_norm)

    return ExistingHistoryDedupeIndex(
        source_ids=frozenset(source_ids),
        content_fingerprints=frozenset(content_fps),
        undated_note_norms=frozenset(undated),
    )


def history_event_already_present(
    index: ExistingHistoryDedupeIndex,
    *,
    client_id: int,
    company_id: int,
    contact_key: str,
    note_text: object | None,
    event_at: object | None,
    event_hash: str,
) -> bool:
    """True when this event is an exact duplicate of something already stored."""
    eh = blank(event_hash)
    if eh and eh in index.source_ids:
        return True
    note_norm = normalize_history_note_text(note_text)
    if not note_norm:
        return False
    fp = history_content_fingerprint(
        client_id=client_id,
        company_id=company_id,
        company_record_no="",
        contact_key=blank(contact_key),
        note_text=note_text,
        event_at=event_at,
    )
    if fp in index.content_fingerprints:
        return True
    # Older undated notes: same normalized body is an exact content duplicate.
    if note_norm in index.undated_note_norms:
        return True
    return False
