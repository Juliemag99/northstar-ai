"""Consolidated shared note-history import (prospects + chronological events).

Expected prepared inputs (Dawson example filenames):
  - Dawson_NorthStar_Prospects_No_Closed.csv
  - Dawson_Shared_Note_History_No_Closed.csv

Join key: normalized LeadMaster Record No.
History is company-scoped (never repeated per contact).
Idempotency: UNIQUE(company_id, event_hash).
Preview is dry-run; confirm is a single atomic transaction.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contact_phone import format_us_phone_display, upsert_contact_phone_keys
from db import DATABASE_DIR, get_connection
from models import (
    NorthStarUser,
    SharedNoteHistoryConfirmResponse,
    SharedNoteHistoryImportBatchView,
    SharedNoteHistoryPreviewCounts,
    SharedNoteHistoryPreviewResponse,
    SharedNoteHistoryUploadResult,
)
from search_data import index_company_relationship, index_contact_relationship

UNATTRIBUTED_LABEL = "Unattributed shared history"
CLOSED_STATUS = "closed"

MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_PROSPECT_ROWS = 50_000
MAX_HISTORY_ROWS = 100_000

STATUS_PREVIEWED = "previewed"
STATUS_CONFIRMED = "confirmed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

IMPORT_ROOT = DATABASE_DIR / "shared_note_history_imports"

_PROSPECT_ALIASES: dict[str, tuple[str, ...]] = {
    "record_no": (
        "leadmaster record no.",
        "leadmaster record no",
        "record no.",
        "record no",
        "record number",
    ),
    "company_name": ("company", "company name"),
    "status": (
        "status",
        "current dawson status",
        "current status",
        "dawson",
        "relationship status",
    ),
    "first_name": ("firstname", "first name", "first"),
    "last_name": ("lastname", "last name", "last"),
    "title": ("title",),
    "phone": ("phone", "phone number"),
    "alt_phone": ("alt phone", "alt_phone", "alternate phone"),
    "mobile": ("mobile",),
    "email": ("email", "email address"),
    "address": ("address1", "address", "address 1"),
    "city": ("city",),
    "state": ("state",),
    "zip": ("zip", "zip code", "postal"),
    "website": ("web address", "website", "url"),
    "customer_campaign": ("customer campaign", "campaign"),
}

_HISTORY_ALIASES: dict[str, tuple[str, ...]] = {
    "record_no": (
        "leadmaster record no.",
        "leadmaster record no",
        "record no.",
        "record no",
        "record number",
    ),
    "company_name": ("company", "company name"),
    "status": (
        "current dawson status",
        "current status",
        "status",
        "dawson",
    ),
    "event_at": (
        "event timestamp",
        "timestamp",
        "event at",
        "event_at",
        "date time",
    ),
    "event_sequence": ("event sequence", "event_sequence", "sequence"),
    "author": ("author", "created by", "user"),
    "event_type": ("event type", "event_type", "type"),
    "attribution": (
        "attributed client/campaign",
        "attributed client",
        "attribution",
        "client attribution",
        "campaign attribution",
    ),
    "attribution_evidence": (
        "attribution evidence",
        "attribution_evidence",
        "evidence",
    ),
    "source_file": ("source file", "source_file", "source"),
    "note_text": (
        "note text",
        "note_text",
        "full note text",
        "notes",
        "note",
        "body",
    ),
    "event_hash": ("event hash", "event_hash", "hash"),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_record_no(value: object | None) -> str:
    """Normalize LeadMaster Record No. for exact join (trim; drop Excel .0)."""
    text = _blank(value)
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", _blank(value).lower().rstrip(":"))


def _map_headers(fieldnames: list[str] | None, aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    headers = [_blank(h) for h in (fieldnames or []) if _blank(h)]
    by_norm = {_norm_header(h): h for h in headers}
    mapped: dict[str, str] = {}
    for canonical, names in aliases.items():
        for name in names:
            hit = by_norm.get(_norm_header(name))
            if hit:
                mapped[canonical] = hit
                break
    return mapped


def _row_get(row: dict[str, str], mapping: dict[str, str], key: str) -> str:
    header = mapping.get(key)
    if not header:
        return ""
    return _blank(row.get(header))


def is_closed_status(status: str) -> bool:
    return _blank(status).casefold() == CLOSED_STATUS


@dataclass
class ProspectContactRow:
    record_no: str
    company_name: str
    status: str
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    phone: str = ""
    alt_phone: str = ""
    mobile: str = ""
    email: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    website: str = ""
    customer_campaign: str = ""
    source_row: int = 0


@dataclass
class HistoryEventRow:
    record_no: str
    event_at: str
    author: str
    event_type: str
    attribution: str
    attribution_evidence: str
    source_file: str
    note_text: str
    event_hash: str
    company_name: str = ""
    event_sequence: str = ""
    source_row: int = 0


@dataclass
class ParsedProspects:
    contacts: list[ProspectContactRow] = field(default_factory=list)
    excluded_closed_contacts: int = 0
    excluded_closed_companies: int = 0
    included_company_record_nos: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ParsedHistory:
    events: list[HistoryEventRow] = field(default_factory=list)
    excluded_closed_events: int = 0
    long_note_count: int = 0
    unattributed_count: int = 0
    errors: list[str] = field(default_factory=list)


def parse_prospects_csv(content: bytes | str) -> ParsedProspects:
    text = content.decode("utf-8-sig") if isinstance(content, (bytes, bytearray)) else content
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(list(reader.fieldnames or []), _PROSPECT_ALIASES)
    out = ParsedProspects()
    if "record_no" not in mapping or "company_name" not in mapping:
        out.errors.append("Prospects CSV must include Record No. and Company columns.")
        return out
    if "status" not in mapping:
        out.errors.append("Prospects CSV must include a Status column.")
        return out

    # First pass: any Closed status for a Record No. excludes the whole company.
    closed_companies: set[str] = set()
    staged: list[ProspectContactRow] = []
    rows = 0
    for idx, raw in enumerate(reader, start=2):
        rows += 1
        if rows > MAX_PROSPECT_ROWS:
            out.errors.append(f"Prospects CSV exceeds {MAX_PROSPECT_ROWS} rows.")
            break
        rn = normalize_record_no(_row_get(raw, mapping, "record_no"))
        if not rn:
            continue
        status = _row_get(raw, mapping, "status")
        if is_closed_status(status):
            closed_companies.add(rn)
        staged.append(
            ProspectContactRow(
                record_no=rn,
                company_name=_row_get(raw, mapping, "company_name"),
                status=status,
                first_name=_row_get(raw, mapping, "first_name"),
                last_name=_row_get(raw, mapping, "last_name"),
                title=_row_get(raw, mapping, "title"),
                phone=_row_get(raw, mapping, "phone"),
                alt_phone=_row_get(raw, mapping, "alt_phone"),
                mobile=_row_get(raw, mapping, "mobile"),
                email=_row_get(raw, mapping, "email"),
                address=_row_get(raw, mapping, "address"),
                city=_row_get(raw, mapping, "city"),
                state=_row_get(raw, mapping, "state"),
                zip=_row_get(raw, mapping, "zip"),
                website=_row_get(raw, mapping, "website"),
                customer_campaign=_row_get(raw, mapping, "customer_campaign"),
                source_row=idx,
            )
        )
    included: dict[str, str] = {}
    for row in staged:
        if row.record_no in closed_companies:
            out.excluded_closed_contacts += 1
            continue
        included.setdefault(row.record_no, row.company_name)
        out.contacts.append(row)
    out.excluded_closed_companies = len(closed_companies)
    out.included_company_record_nos = sorted(included.keys(), key=lambda x: (len(x), x))
    return out


def parse_history_csv(
    content: bytes | str,
    *,
    allowed_record_nos: set[str] | None = None,
    closed_record_nos: set[str] | None = None,
) -> ParsedHistory:
    text = content.decode("utf-8-sig") if isinstance(content, (bytes, bytearray)) else content
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(list(reader.fieldnames or []), _HISTORY_ALIASES)
    out = ParsedHistory()
    required = ("record_no", "event_hash", "note_text")
    missing = [k for k in required if k not in mapping]
    if missing:
        out.errors.append(
            "History CSV must include Record No., Event Hash, and Note Text columns."
        )
        return out

    closed = set(closed_record_nos or set())
    allowed = allowed_record_nos
    rows = 0
    for idx, raw in enumerate(reader, start=2):
        rows += 1
        if rows > MAX_HISTORY_ROWS:
            out.errors.append(f"History CSV exceeds {MAX_HISTORY_ROWS} rows.")
            break
        rn = normalize_record_no(_row_get(raw, mapping, "record_no"))
        if not rn:
            continue
        row_status = _row_get(raw, mapping, "status")
        if is_closed_status(row_status):
            closed.add(rn)
            out.excluded_closed_events += 1
            continue
        if rn in closed:
            out.excluded_closed_events += 1
            continue
        if allowed is not None and rn not in allowed:
            continue
        # Preserve full note text — do not truncate long history.
        note = raw.get(mapping["note_text"])
        note = "" if note is None else str(note)
        # Keep trailing/leading content; only normalize newlines for storage consistency.
        note = note.replace("\r\n", "\n").replace("\r", "\n")
        event_hash = _row_get(raw, mapping, "event_hash")
        if not event_hash:
            # Deterministic fallback hash when source omitted (fixtures / repair).
            event_hash = hashlib.sha256(
                "|".join(
                    [
                        rn,
                        _row_get(raw, mapping, "event_at"),
                        _row_get(raw, mapping, "author"),
                        _row_get(raw, mapping, "event_type"),
                        _row_get(raw, mapping, "attribution"),
                        note,
                    ]
                ).encode("utf-8")
            ).hexdigest()
        attribution = _row_get(raw, mapping, "attribution") or UNATTRIBUTED_LABEL
        event = HistoryEventRow(
            record_no=rn,
            event_at=_row_get(raw, mapping, "event_at"),
            author=_row_get(raw, mapping, "author"),
            event_type=_row_get(raw, mapping, "event_type"),
            attribution=attribution,
            attribution_evidence=_row_get(raw, mapping, "attribution_evidence"),
            source_file=_row_get(raw, mapping, "source_file"),
            note_text=note,
            event_hash=event_hash,
            company_name=_row_get(raw, mapping, "company_name"),
            event_sequence=_row_get(raw, mapping, "event_sequence"),
            source_row=idx,
        )
        out.events.append(event)
        if len(note) > 2000:
            out.long_note_count += 1
        if attribution.casefold() == UNATTRIBUTED_LABEL.casefold():
            out.unattributed_count += 1
    return out


def ensure_shared_note_history_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS company_shared_history_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                external_record_no TEXT NOT NULL DEFAULT '',
                source_company_name TEXT NOT NULL DEFAULT '',
                event_at TEXT NOT NULL DEFAULT '',
                event_sequence TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                attribution TEXT NOT NULL DEFAULT '',
                attribution_evidence TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                note_text TEXT NOT NULL DEFAULT '',
                event_hash TEXT NOT NULL,
                import_batch_id INTEGER,
                imported_at TEXT NOT NULL DEFAULT '',
                imported_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_company_shared_history_idempotency
                ON company_shared_history_events(company_id, event_hash);

            CREATE INDEX IF NOT EXISTS idx_company_shared_history_company
                ON company_shared_history_events(company_id, event_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_company_shared_history_record_no
                ON company_shared_history_events(external_record_no);

            CREATE TABLE IF NOT EXISTS shared_note_history_import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'previewed',
                prospects_filename TEXT NOT NULL DEFAULT '',
                history_filename TEXT NOT NULL DEFAULT '',
                prospects_sha256 TEXT NOT NULL DEFAULT '',
                history_sha256 TEXT NOT NULL DEFAULT '',
                prospects_path TEXT NOT NULL DEFAULT '',
                history_path TEXT NOT NULL DEFAULT '',
                preview_json TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER,
                created_by_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT NOT NULL DEFAULT '',
                cancelled_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_snh_import_batches_client
                ON shared_note_history_import_batches(client_id, id DESC);
            """
        )
        # Additive columns for DBs created before source_company_name / event_sequence.
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(company_shared_history_events)").fetchall()
        }
        if "source_company_name" not in cols:
            conn.execute(
                "ALTER TABLE company_shared_history_events "
                "ADD COLUMN source_company_name TEXT NOT NULL DEFAULT ''"
            )
        if "event_sequence" not in cols:
            conn.execute(
                "ALTER TABLE company_shared_history_events "
                "ADD COLUMN event_sequence TEXT NOT NULL DEFAULT ''"
            )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _require_admin_client_access(actor: NorthStarUser, client_id: int) -> None:
    from access import user_can_access_client

    if actor is None or not bool(actor.active) or not bool(actor.is_administrator):
        raise PermissionError("Not authorized.")
    if client_id <= 0:
        raise ValueError("Choose a specific client before uploading.")
    if not user_can_access_client(actor.id, client_id):
        raise PermissionError("You do not have access to this client.")


def _company_by_record_no(conn, record_no: str) -> Any | None:
    return conn.execute(
        """
        SELECT id, company_name, external_record_no
        FROM companies
        WHERE TRIM(external_record_no) = ?
        LIMIT 1
        """,
        (record_no,),
    ).fetchone()


def _contact_match(
    conn, company_id: int, first: str, last: str, email: str
) -> int | None:
    first_l = first.casefold()
    last_l = last.casefold()
    email_l = email.casefold()
    rows = conn.execute(
        """
        SELECT id, first_name, last_name, email
        FROM contacts
        WHERE company_id = ?
        """,
        (company_id,),
    ).fetchall()
    for row in rows:
        if email_l and _blank(row["email"]).casefold() == email_l:
            return int(row["id"])
        if (
            first_l
            and last_l
            and _blank(row["first_name"]).casefold() == first_l
            and _blank(row["last_name"]).casefold() == last_l
        ):
            return int(row["id"])
    return None


def _relationship(conn, client_id: int, company_id: int) -> Any | None:
    return conn.execute(
        """
        SELECT id, status, external_record_no
        FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        """,
        (client_id, company_id),
    ).fetchone()


def build_preview_counts(
    conn,
    *,
    client_id: int,
    prospects: ParsedProspects,
    history: ParsedHistory,
) -> SharedNoteHistoryPreviewCounts:
    companies_to_create = 0
    companies_to_reuse = 0
    contacts_to_create = 0
    contacts_to_reuse = 0
    relationships_to_create = 0
    relationships_existing = 0
    status_preserved = 0
    status_set_new = 0
    seen_companies: set[str] = set()
    company_ids: dict[str, int] = {}
    first_by_rn: dict[str, ProspectContactRow] = {}
    for row in prospects.contacts:
        first_by_rn.setdefault(row.record_no, row)

    for rn, row in first_by_rn.items():
        seen_companies.add(rn)
        existing = _company_by_record_no(conn, rn)
        if existing is None:
            companies_to_create += 1
            relationships_to_create += 1
            if row.status:
                status_set_new += 1
        else:
            companies_to_reuse += 1
            company_ids[rn] = int(existing["id"])
            rel = _relationship(conn, client_id, int(existing["id"]))
            if rel is None:
                relationships_to_create += 1
                if row.status:
                    status_set_new += 1
            else:
                relationships_existing += 1
                status_preserved += 1

    for row in prospects.contacts:
        company_id = company_ids.get(row.record_no)
        has_contact = bool(row.first_name or row.last_name or row.email or row.phone)
        if not has_contact:
            continue
        if company_id is not None:
            match = _contact_match(
                conn, company_id, row.first_name, row.last_name, row.email
            )
            if match is None:
                contacts_to_create += 1
            else:
                contacts_to_reuse += 1
        else:
            contacts_to_create += 1

    history_already = 0
    history_to_insert = 0
    for event in history.events:
        company_id = company_ids.get(event.record_no)
        if company_id is None:
            existing = _company_by_record_no(conn, event.record_no)
            if existing is not None:
                company_id = int(existing["id"])
                company_ids[event.record_no] = company_id
        if company_id is None:
            history_to_insert += 1
            continue
        found = conn.execute(
            """
            SELECT 1 FROM company_shared_history_events
            WHERE company_id = ? AND event_hash = ?
            LIMIT 1
            """,
            (company_id, event.event_hash),
        ).fetchone()
        if found:
            history_already += 1
        else:
            history_to_insert += 1

    return SharedNoteHistoryPreviewCounts(
        companies_included=len(prospects.included_company_record_nos),
        companies_excluded_closed=prospects.excluded_closed_companies,
        contacts_included=len(prospects.contacts),
        contacts_excluded_closed=prospects.excluded_closed_contacts,
        history_events_total=len(history.events),
        history_events_excluded_closed=history.excluded_closed_events,
        history_long_notes=history.long_note_count,
        history_unattributed=history.unattributed_count,
        companies_to_create=companies_to_create,
        companies_to_reuse=companies_to_reuse,
        contacts_to_create=contacts_to_create,
        contacts_to_reuse=contacts_to_reuse,
        relationships_to_create=relationships_to_create,
        relationships_existing=relationships_existing,
        status_preserved=status_preserved,
        status_set_on_new_relationships=status_set_new,
        history_to_insert=history_to_insert,
        history_already_present=history_already,
    )


def preview_from_parsed(
    conn,
    *,
    client_id: int,
    prospects: ParsedProspects,
    history: ParsedHistory,
) -> SharedNoteHistoryPreviewResponse:
    errors = [*prospects.errors, *history.errors]
    counts = (
        SharedNoteHistoryPreviewCounts()
        if errors
        else build_preview_counts(
            conn, client_id=client_id, prospects=prospects, history=history
        )
    )
    return SharedNoteHistoryPreviewResponse(
        client_id=client_id,
        ok=not errors,
        errors=errors,
        counts=counts,
        included_record_nos=list(prospects.included_company_record_nos),
    )


def upload_shared_note_history_import(
    *,
    client_id: int,
    actor: NorthStarUser,
    prospects_filename: str,
    prospects_content: bytes,
    history_filename: str,
    history_content: bytes,
) -> SharedNoteHistoryUploadResult:
    _require_admin_client_access(actor, client_id)
    if len(prospects_content) > MAX_FILE_BYTES or len(history_content) > MAX_FILE_BYTES:
        raise ValueError("One of the upload files is too large.")
    if not prospects_content or not history_content:
        raise ValueError("Both prospects and history CSV files are required.")

    ensure_shared_note_history_schema()
    prospects = parse_prospects_csv(prospects_content)
    closed_rns = set()
    # Re-scan closed from prospects file for history filter (status Closed rows).
    # parse_prospects already excluded them from contacts; closed set reconstructed
    # via excluded count alone is insufficient — re-parse status from raw for closed.
    text = prospects_content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(list(reader.fieldnames or []), _PROSPECT_ALIASES)
    for raw in reader:
        rn = normalize_record_no(_row_get(raw, mapping, "record_no"))
        if rn and is_closed_status(_row_get(raw, mapping, "status")):
            closed_rns.add(rn)

    allowed = set(prospects.included_company_record_nos)
    history = parse_history_csv(
        history_content, allowed_record_nos=allowed, closed_record_nos=closed_rns
    )

    batch_token = uuid.uuid4().hex
    dest_dir = IMPORT_ROOT / str(client_id) / batch_token
    dest_dir.mkdir(parents=True, exist_ok=True)
    prospects_path = dest_dir / "prospects.csv"
    history_path = dest_dir / "history.csv"
    prospects_path.write_bytes(prospects_content)
    history_path.write_bytes(history_content)

    with get_connection() as conn:
        ensure_shared_note_history_schema(conn)
        preview = preview_from_parsed(
            conn, client_id=client_id, prospects=prospects, history=history
        )
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO shared_note_history_import_batches (
                client_id, status, prospects_filename, history_filename,
                prospects_sha256, history_sha256, prospects_path, history_path,
                preview_json, created_by_user_id, created_by_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                STATUS_PREVIEWED if preview.ok else STATUS_FAILED,
                _blank(prospects_filename) or "prospects.csv",
                _blank(history_filename) or "history.csv",
                _sha256_bytes(prospects_content),
                _sha256_bytes(history_content),
                str(prospects_path),
                str(history_path),
                json.dumps(preview.model_dump()),
                int(actor.id),
                _blank(getattr(actor, "full_name", "")),
                now,
            ),
        )
        batch_id = int(cur.lastrowid)
        conn.commit()

    return SharedNoteHistoryUploadResult(
        batch_id=batch_id,
        client_id=client_id,
        status=STATUS_PREVIEWED if preview.ok else STATUS_FAILED,
        preview=preview,
    )


def get_shared_note_history_batch(
    client_id: int, batch_id: int
) -> SharedNoteHistoryImportBatchView:
    ensure_shared_note_history_schema()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM shared_note_history_import_batches
            WHERE id = ? AND client_id = ?
            """,
            (batch_id, client_id),
        ).fetchone()
        if row is None:
            raise LookupError("Import batch not found.")
        preview = None
        raw_preview = _blank(row["preview_json"])
        if raw_preview:
            try:
                preview = SharedNoteHistoryPreviewResponse.model_validate(
                    json.loads(raw_preview)
                )
            except Exception:
                preview = None
        return SharedNoteHistoryImportBatchView(
            batch_id=int(row["id"]),
            client_id=int(row["client_id"]),
            status=_blank(row["status"]),
            prospects_filename=_blank(row["prospects_filename"]),
            history_filename=_blank(row["history_filename"]),
            created_at=_blank(row["created_at"]),
            confirmed_at=_blank(row["confirmed_at"]),
            preview=preview,
        )


def _load_batch_files(conn, client_id: int, batch_id: int) -> tuple[Any, bytes, bytes]:
    row = conn.execute(
        """
        SELECT * FROM shared_note_history_import_batches
        WHERE id = ? AND client_id = ?
        """,
        (batch_id, client_id),
    ).fetchone()
    if row is None:
        raise LookupError("Import batch not found.")
    if _blank(row["status"]) not in {STATUS_PREVIEWED, STATUS_CONFIRMED}:
        raise ValueError("This import batch can no longer be confirmed.")
    prospects_path = Path(_blank(row["prospects_path"]))
    history_path = Path(_blank(row["history_path"]))
    if not prospects_path.is_file() or not history_path.is_file():
        raise ValueError("Staged import files are missing.")
    return row, prospects_path.read_bytes(), history_path.read_bytes()


def apply_shared_note_history_import(
    conn,
    *,
    client_id: int,
    actor: NorthStarUser,
    prospects: ParsedProspects,
    history: ParsedHistory,
    batch_id: int | None = None,
    fail_after: str | None = None,
) -> SharedNoteHistoryConfirmResponse:
    """Apply import on an open connection (caller owns transaction boundaries)."""
    if prospects.errors or history.errors:
        raise ValueError("Import files have validation errors.")

    now = _now()
    company_id_by_rn: dict[str, int] = {}
    created_companies = 0
    reused_companies = 0
    created_contacts = 0
    reused_contacts = 0
    created_relationships = 0
    existing_relationships = 0
    status_preserved = 0
    status_set = 0
    history_inserted = 0
    history_skipped = 0

    # Company + relationship first (one per Record No.).
    first_by_rn: dict[str, ProspectContactRow] = {}
    for row in prospects.contacts:
        first_by_rn.setdefault(row.record_no, row)

    for rn, row in first_by_rn.items():
        existing = _company_by_record_no(conn, rn)
        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO companies (
                    external_record_no, company_name, address, city, state, zip, website,
                    legacy_phone, legacy_alt_phone, legacy_mobile, legacy_email,
                    legacy_first_name, legacy_last_name, legacy_title,
                    customer_campaign, source, created_at, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'shared_note_history_import', ?, ?)
                """,
                (
                    rn,
                    row.company_name,
                    row.address,
                    row.city,
                    row.state,
                    row.zip,
                    row.website,
                    format_us_phone_display(row.phone) if row.phone else "",
                    format_us_phone_display(row.alt_phone) if row.alt_phone else "",
                    format_us_phone_display(row.mobile) if row.mobile else "",
                    row.email,
                    row.first_name,
                    row.last_name,
                    row.title,
                    row.customer_campaign,
                    now,
                    now,
                ),
            )
            company_id = int(cur.lastrowid)
            created_companies += 1
            if fail_after == "after_company_insert" and created_companies == 1:
                raise RuntimeError("Injected failure after company insert.")
        else:
            company_id = int(existing["id"])
            reused_companies += 1

        company_id_by_rn[rn] = company_id
        rel = _relationship(conn, client_id, company_id)
        if rel is None:
            status = row.status or "New"
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at,
                    external_record_no
                ) VALUES (?, ?, ?, ?, '', '', '', 0, ?, ?, ?)
                """,
                (client_id, company_id, status, int(actor.id), now, now, rn),
            )
            created_relationships += 1
            if row.status:
                status_set += 1
            index_company_relationship(client_id, company_id, conn=conn)
        else:
            existing_relationships += 1
            status_preserved += 1
            # Never overwrite existing client-specific status.
            # Ensure relationship Record No. is set when blank.
            if not _blank(rel["external_record_no"]):
                pass
            else:
                conn.execute(
                    """
                    UPDATE client_company_relationships
                    SET external_record_no = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (rn, now, int(rel["id"])),
                )

    # Contacts — do not attach company history to contacts.
    for row in prospects.contacts:
        company_id = company_id_by_rn[row.record_no]
        has_contact = bool(row.first_name or row.last_name or row.email or row.phone)
        if not has_contact:
            continue
        match_id = _contact_match(
            conn, company_id, row.first_name, row.last_name, row.email
        )
        if match_id is not None:
            reused_contacts += 1
            continue
        phone = format_us_phone_display(row.phone) if row.phone else ""
        alt = format_us_phone_display(row.alt_phone) if row.alt_phone else ""
        if not alt and row.mobile:
            alt = format_us_phone_display(row.mobile)
        cur = conn.execute(
            """
            INSERT INTO contacts (
                company_id, external_record_no, first_name, last_name, title,
                phone, alt_phone, email, source_row_index, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'shared_note_history_import', ?)
            """,
            (
                company_id,
                row.record_no,
                row.first_name,
                row.last_name,
                row.title,
                phone,
                alt,
                row.email,
                row.source_row,
                now,
            ),
        )
        contact_id = int(cur.lastrowid)
        upsert_contact_phone_keys(conn, contact_id, phone=phone, alt_phone=alt)
        index_contact_relationship(client_id, company_id, contact_id, conn=conn)
        created_contacts += 1
        if fail_after == "after_contact_insert" and created_contacts == 1:
            raise RuntimeError("Injected failure after contact insert.")

    # History events — one row per CSV event; company-scoped; idempotent.
    for event in history.events:
        company_id = company_id_by_rn.get(event.record_no)
        if company_id is None:
            existing = _company_by_record_no(conn, event.record_no)
            if existing is None:
                # Orphan history for non-included Record No. should already be filtered.
                continue
            company_id = int(existing["id"])
            company_id_by_rn[event.record_no] = company_id
        try:
            conn.execute(
                """
                INSERT INTO company_shared_history_events (
                    company_id, external_record_no, source_company_name,
                    event_at, event_sequence, author, event_type,
                    attribution, attribution_evidence, source_file, note_text,
                    event_hash, import_batch_id, imported_at, imported_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    event.record_no,
                    event.company_name,
                    event.event_at,
                    event.event_sequence,
                    event.author,
                    event.event_type,
                    event.attribution or UNATTRIBUTED_LABEL,
                    event.attribution_evidence,
                    event.source_file,
                    event.note_text,
                    event.event_hash,
                    batch_id,
                    now,
                    int(actor.id),
                ),
            )
            history_inserted += 1
            if fail_after == "after_history_insert" and history_inserted == 1:
                raise RuntimeError("Injected failure after history insert.")
        except sqlite3.IntegrityError:
            history_skipped += 1
            continue

    return SharedNoteHistoryConfirmResponse(
        client_id=client_id,
        batch_id=batch_id or 0,
        ok=True,
        companies_created=created_companies,
        companies_reused=reused_companies,
        contacts_created=created_contacts,
        contacts_reused=reused_contacts,
        relationships_created=created_relationships,
        relationships_existing=existing_relationships,
        status_preserved=status_preserved,
        status_set_on_new_relationships=status_set,
        history_inserted=history_inserted,
        history_skipped_duplicates=history_skipped,
    )


def confirm_shared_note_history_import(
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    fail_after: str | None = None,
) -> SharedNoteHistoryConfirmResponse:
    _require_admin_client_access(actor, client_id)
    ensure_shared_note_history_schema()

    with get_connection() as conn:
        ensure_shared_note_history_schema(conn)
        batch_row, prospects_bytes, history_bytes = _load_batch_files(
            conn, client_id, batch_id
        )
        if _blank(batch_row["status"]) == STATUS_CONFIRMED:
            # Idempotent re-confirm: re-run apply (history inserts skip duplicates).
            pass
        elif _blank(batch_row["status"]) != STATUS_PREVIEWED:
            raise ValueError("This import batch can no longer be confirmed.")

        prospects = parse_prospects_csv(prospects_bytes)
        text = prospects_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        mapping = _map_headers(list(reader.fieldnames or []), _PROSPECT_ALIASES)
        closed_rns: set[str] = set()
        for raw in reader:
            rn = normalize_record_no(_row_get(raw, mapping, "record_no"))
            if rn and is_closed_status(_row_get(raw, mapping, "status")):
                closed_rns.add(rn)
        history = parse_history_csv(
            history_bytes,
            allowed_record_nos=set(prospects.included_company_record_nos),
            closed_record_nos=closed_rns,
        )

        conn.execute("BEGIN IMMEDIATE")
        try:
            result = apply_shared_note_history_import(
                conn,
                client_id=client_id,
                actor=actor,
                prospects=prospects,
                history=history,
                batch_id=batch_id,
                fail_after=fail_after,
            )
            conn.execute(
                """
                UPDATE shared_note_history_import_batches
                SET status = ?, confirmed_at = ?, result_json = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    STATUS_CONFIRMED,
                    _now(),
                    json.dumps(result.model_dump()),
                    batch_id,
                    client_id,
                ),
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def preview_shared_note_history_paths(
    *,
    client_id: int,
    actor: NorthStarUser,
    prospects_path: Path,
    history_path: Path,
) -> SharedNoteHistoryUploadResult:
    """Convenience for local prepared files (no live DB use unless caller confirms)."""
    return upload_shared_note_history_import(
        client_id=client_id,
        actor=actor,
        prospects_filename=prospects_path.name,
        prospects_content=prospects_path.read_bytes(),
        history_filename=history_path.name,
        history_content=history_path.read_bytes(),
    )
