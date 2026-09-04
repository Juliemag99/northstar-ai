"""CRM import relationship status and notes planning helpers.

Pure deterministic helpers used by the dry-run planner and atomic confirm.
Never creates new client statuses. Never stores note bodies in audit results.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RELATIONSHIP_STATUS = "New"

STATUS_USE_DEFAULT = "use_default_status"
STATUS_PRESERVE = "preserve_existing_status"
STATUS_USE_IMPORTED = "use_imported_status"
STATUS_CONFLICT = "status_conflict"
STATUS_INVALID = "invalid_status"

NOTES_NO_CHANGE = "no_notes_change"
NOTES_SET = "set_imported_notes"
NOTES_APPEND = "append_imported_notes"
NOTES_ALREADY_PRESENT = "imported_notes_already_present"

STATUS_NEEDS_REVIEW = frozenset({STATUS_CONFLICT, STATUS_INVALID})


def blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_status_key(value: object | None) -> str:
    """Case- and outer-whitespace-insensitive status key."""
    return blank(value).casefold()


def normalize_notes_compare(value: object | None) -> str:
    """Normalize line endings and surrounding whitespace for note equality."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text


def prepare_imported_notes(value: object | None) -> str:
    """Trim surrounding whitespace; preserve readable internal newlines."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text


def notes_block_already_present(existing: object | None, incoming: object | None) -> bool:
    """True when the full normalized incoming block is already a contiguous block."""
    incoming_norm = normalize_notes_compare(incoming)
    if not incoming_norm:
        return True
    existing_norm = normalize_notes_compare(existing)
    if not existing_norm:
        return False
    return incoming_norm in existing_norm


def append_imported_notes(existing: object | None, incoming: object | None) -> str:
    """Append imported notes with exactly one blank line when both sides have text."""
    incoming_text = prepare_imported_notes(incoming)
    if not incoming_text:
        existing_text = "" if existing is None else str(existing)
        return existing_text.replace("\r\n", "\n").replace("\r", "\n")
    existing_text = "" if existing is None else str(existing)
    existing_text = existing_text.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    if not existing_text.strip():
        return incoming_text
    return f"{existing_text}\n\n{incoming_text}"


@dataclass(frozen=True, slots=True)
class StatusCatalog:
    """Client status labels keyed by normalized lookup."""

    by_key: dict[str, tuple[str, ...]]

    @classmethod
    def from_labels(cls, labels: list[str] | tuple[str, ...] | set[str]) -> StatusCatalog:
        buckets: dict[str, list[str]] = {}
        for raw in labels:
            label = blank(raw)
            if not label:
                continue
            key = normalize_status_key(label)
            bucket = buckets.setdefault(key, [])
            if label not in bucket:
                bucket.append(label)
        return cls(by_key={k: tuple(v) for k, v in buckets.items()})

    def resolve(self, imported: object | None) -> tuple[str | None, str | None]:
        """Return (canonical_label, error) for a nonblank imported status.

        error is 'ambiguous' or 'unknown' when not resolvable.
        """
        raw = blank(imported)
        if not raw:
            return None, None
        key = normalize_status_key(raw)
        matches = self.by_key.get(key) or ()
        if not matches:
            return None, "unknown"
        if len(matches) > 1:
            return None, "ambiguous"
        return matches[0], None

    def canonical_default_new(self) -> str:
        """Prefer the client's configured New label; otherwise literal New."""
        matches = self.by_key.get(normalize_status_key(DEFAULT_RELATIONSHIP_STATUS)) or ()
        if len(matches) == 1:
            return matches[0]
        return DEFAULT_RELATIONSHIP_STATUS


@dataclass(frozen=True, slots=True)
class StatusNotesPlan:
    status_action: str
    notes_action: str
    resolved_status: str
    planned_notes: str
    needs_review: bool


def plan_status_and_notes(
    *,
    relationship_is_new: bool,
    existing_status: str,
    existing_notes: str,
    imported_status: object | None,
    imported_notes: object | None,
    catalog: StatusCatalog,
) -> StatusNotesPlan:
    """Plan status/notes decisions for one resolvable relationship row."""
    incoming_status = blank(imported_status)
    incoming_notes = prepare_imported_notes(imported_notes)
    existing_status_blank = blank(existing_status)
    existing_notes_text = "" if existing_notes is None else str(existing_notes)

    if relationship_is_new:
        if not incoming_status:
            status_action = STATUS_USE_DEFAULT
            resolved_status = catalog.canonical_default_new()
        else:
            canonical, err = catalog.resolve(incoming_status)
            if err or canonical is None:
                status_action = STATUS_INVALID
                resolved_status = ""
            else:
                status_action = STATUS_USE_IMPORTED
                resolved_status = canonical
        if not incoming_notes:
            notes_action = NOTES_NO_CHANGE
            planned_notes = ""
        else:
            notes_action = NOTES_SET
            planned_notes = incoming_notes
    else:
        if not incoming_status:
            status_action = STATUS_PRESERVE
            resolved_status = existing_status_blank
        else:
            canonical, err = catalog.resolve(incoming_status)
            if err or canonical is None:
                status_action = STATUS_INVALID
                resolved_status = existing_status_blank
            elif normalize_status_key(canonical) == normalize_status_key(existing_status_blank):
                status_action = STATUS_PRESERVE
                resolved_status = existing_status_blank or canonical
            else:
                status_action = STATUS_CONFLICT
                resolved_status = existing_status_blank

        if not incoming_notes:
            notes_action = NOTES_NO_CHANGE
            planned_notes = existing_notes_text.replace("\r\n", "\n").replace("\r", "\n")
        elif notes_block_already_present(existing_notes_text, incoming_notes):
            notes_action = NOTES_ALREADY_PRESENT
            planned_notes = existing_notes_text.replace("\r\n", "\n").replace("\r", "\n")
        else:
            notes_action = NOTES_APPEND
            planned_notes = append_imported_notes(existing_notes_text, incoming_notes)

    return StatusNotesPlan(
        status_action=status_action,
        notes_action=notes_action,
        resolved_status=resolved_status,
        planned_notes=planned_notes,
        needs_review=status_action in STATUS_NEEDS_REVIEW,
    )


def load_client_status_catalog(conn, client_id: int) -> StatusCatalog:
    rows = conn.execute(
        """
        SELECT DISTINCT status
        FROM client_company_relationships
        WHERE client_id = ? AND TRIM(status) != ''
        ORDER BY status COLLATE NOCASE ASC
        """,
        (int(client_id),),
    ).fetchall()
    labels = [blank(r["status"] if hasattr(r, "keys") else r[0]) for r in rows]
    return StatusCatalog.from_labels(labels)
