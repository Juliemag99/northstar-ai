"""CRM import relationship status and notes planning helpers.

Pure deterministic helpers used by the dry-run planner and atomic confirm.
Never invents brand-new freeform statuses outside the client's catalog union
and the shared standard CRM status list. Never stores note bodies in audit results.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RELATIONSHIP_STATUS = "New"

# Canonical NorthStar relationship statuses for every client (import validation +
# catalog seeding). Merged into the import catalog so shared labels are valid even
# when they have not yet appeared on a CCR row for that client. Preserve spelling
# and capitalization exactly; do not invent freeform labels outside this list +
# any client-specific catalog/CCR entries.
STANDARD_CRM_RELATIONSHIP_STATUSES: tuple[str, ...] = (
    "New",
    "Contacted",
    "Qualified",
    "Future/Nurture",
    "Closed",
    "Appointment Set",
    "Good Fit-But no projects at this Time",
    "Disqualified-Not a good fit-No relevant work",
    "Disqualified-Production/Packed Outside US",
    "Disqualified-Purchasing Done at Parent/Elsewhere",
    "Actively Calling-Not getting through yet",
    "Company Not Called Yet",
    "Other",
    "Left Message",
    "Good fit under contract with competitor",
    "Send Information",
    "Hot Prospect",
    "Need Contact Name",
    "Need Contact Phone Number",
    "Competitor",
    "Current Customer",
    "Obtained New Contact Name/Number",
    "Dupe Record",
    "Do Not Call",
    "Do Not Email",
    "Appt Set Email Marketing",
)

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

# Batch option: imported nonblank valid status overwrites existing relationship.
BATCH_RESOLUTION_USE_IMPORTED = "batch_use_imported_existing"


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
    # True when option converted a would-be conflict into use_imported_status.
    authoritative_existing_update: bool = False


def plan_status_and_notes(
    *,
    relationship_is_new: bool,
    existing_status: str,
    existing_notes: str,
    imported_status: object | None,
    imported_notes: object | None,
    catalog: StatusCatalog,
    use_imported_for_existing: bool = False,
) -> StatusNotesPlan:
    """Plan status/notes decisions for one resolvable relationship row.

    use_imported_for_existing (default False): when True, a valid nonblank
    imported status that differs from the existing relationship status becomes
    authoritative (use_imported_status) instead of status_conflict. Blank
    imports still preserve the existing status.
    """
    incoming_status = blank(imported_status)
    incoming_notes = prepare_imported_notes(imported_notes)
    existing_status_blank = blank(existing_status)
    existing_notes_text = "" if existing_notes is None else str(existing_notes)
    authoritative_update = False

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
            elif use_imported_for_existing:
                status_action = STATUS_USE_IMPORTED
                resolved_status = canonical
                authoritative_update = True
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
        authoritative_existing_update=authoritative_update,
    )


def load_client_status_catalog(conn, client_id: int) -> StatusCatalog:
    """Build import status catalog: CCR ∪ client_status_catalog ∪ standards.

    Read-only. Does not invent freeform statuses. Standards cover the full
    shared NorthStar CRM label list even when a client has not used them on a
    CCR yet.
    """
    labels: list[str] = []
    rows = conn.execute(
        """
        SELECT DISTINCT status
        FROM client_company_relationships
        WHERE client_id = ? AND TRIM(status) != ''
        ORDER BY status COLLATE NOCASE ASC
        """,
        (int(client_id),),
    ).fetchall()
    labels.extend(blank(r["status"] if hasattr(r, "keys") else r[0]) for r in rows)
    try:
        catalog_rows = conn.execute(
            """
            SELECT DISTINCT status_label
            FROM client_status_catalog
            WHERE client_id = ?
              AND active = 1
              AND TRIM(status_label) != ''
            ORDER BY status_label COLLATE NOCASE ASC
            """,
            (int(client_id),),
        ).fetchall()
        labels.extend(
            blank(r["status_label"] if hasattr(r, "keys") else r[0])
            for r in catalog_rows
        )
    except Exception:
        # Table may be absent on older fixtures; CCR + standards still apply.
        pass
    labels.extend(STANDARD_CRM_RELATIONSHIP_STATUSES)
    return StatusCatalog.from_labels(labels)


def ensure_client_standard_status_catalog(conn, client_id: int) -> list[str]:
    """Upsert missing standard CRM statuses into client_status_catalog.

    Idempotent; never duplicates casefold-equivalent labels. Does not touch CCR.
    Safe for isolated tests / controlled migrate — not invoked by dry-run.
    """
    cid = int(client_id)
    existing_keys: set[str] = set()
    try:
        for row in conn.execute(
            """
            SELECT status_label FROM client_status_catalog
            WHERE client_id = ? AND TRIM(status_label) != ''
            """,
            (cid,),
        ).fetchall():
            existing_keys.add(
                normalize_status_key(row["status_label"] if hasattr(row, "keys") else row[0])
            )
    except Exception:
        return []

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added: list[str] = []
    # Place standards after any existing sort_order.
    try:
        max_sort = conn.execute(
            """
            SELECT COALESCE(MAX(sort_order), -1) AS n
            FROM client_status_catalog WHERE client_id = ?
            """,
            (cid,),
        ).fetchone()
        next_sort = int(max_sort["n"] if hasattr(max_sort, "keys") else max_sort[0]) + 1
    except Exception:
        next_sort = 0

    for label in STANDARD_CRM_RELATIONSHIP_STATUSES:
        key = normalize_status_key(label)
        if not key or key in existing_keys:
            continue
        conn.execute(
            """
            INSERT INTO client_status_catalog (
                client_id, status_label, is_default, sort_order, active,
                created_at, updated_at
            ) VALUES (?, ?, 0, ?, 1, ?, ?)
            ON CONFLICT(client_id, status_label) DO UPDATE SET
                active = 1,
                updated_at = excluded.updated_at
            """,
            (cid, label, next_sort, now, now),
        )
        existing_keys.add(key)
        added.append(label)
        next_sort += 1
    return added
