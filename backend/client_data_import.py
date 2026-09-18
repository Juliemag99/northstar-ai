"""Client Data Import orchestrator.

Composes Company & contact CRM import staging/plan/confirm with optional
shared note-history CSV staging, or history-only mode that never creates a
CRM prospects batch and never mutates master CRM rows.

Preview/mapping/dry-run write only staging and audit tables — never
companies, contacts, or relationships.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crm_import_confirm import apply_crm_import_plan_on_connection
from crm_import_plan import (
    EXCLUDED_CLOSED,
    IDENTITY_CLIENT_DATA,
    plan_crm_import_batch,
)
from crm_import_staging import (
    BatchNotReusable,
    CLIENT_REQUIRED,
    cancel_crm_import,
    get_crm_import_batch,
    save_crm_import_mapping,
    upload_crm_import,
    _safe_filename,
)
from client_data_history_match import resolve_history_company
from client_data_history_notes import (
    MutableHistoryDedupeIndex,
    compute_history_event_hash,
    history_event_already_present,
    is_routine_marketing_send_note,
    load_existing_history_dedupe_index,
)
from db import DATABASE_DIR, DB_PATH, get_connection
from models import (
    ClientDataImportBatchView,
    ClientDataImportConfirmResponse,
    ClientDataImportDryRunResponse,
    ClientDataImportHistoryCounts,
    ClientDataImportProspectsCounts,
    ClientDataImportUploadResult,
    NorthStarUser,
)
from shared_note_history_import import (
    CLEANUP_COMPLETED,
    CLEANUP_PENDING,
    MAX_FILE_BYTES as HISTORY_MAX_FILE_BYTES,
    MAX_HISTORY_ROWS,
    UNATTRIBUTED_LABEL,
    HistoryEventRow,
    ParsedHistory,
    is_closed_status,
    normalize_record_no,
    _company_by_record_no,
)

log = logging.getLogger("northstar.client_data_import")

STATUS_PREVIEWED = "previewed"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

IMPORT_MODE_FULL = "full"
IMPORT_MODE_HISTORY_ONLY = "history_only"

IMPORT_ROOT = DATABASE_DIR / "client_data_imports"


def _import_root() -> Path:
    """Resolve staging root from the active DB path (honors isolate_for_tests)."""
    try:
        return Path(DB_PATH).resolve().parent / "client_data_imports"
    except Exception:
        return IMPORT_ROOT


HISTORY_SAMPLE_ROWS = 10

CLOSED_POLICY_TEXT = (
    "Closed means closed/not-fit for this selected client. "
    "Those records are excluded from this client import by default. "
    "Closed is not treated as deletion from the shared company master."
)

HISTORY_CANONICAL_FIELDS = frozenset(
    {
        "history_record_no",
        "history_event_at",
        "history_author",
        "history_event_type",
        "history_attribution",
        "history_attribution_evidence",
        "history_note_text",
        "history_contact_note",
        "history_event_hash",
        "history_source_note_id",
        "history_contact_no",
        "history_company_name",
        "history_status",
        "history_event_sequence",
        "history_source_file",
    }
)

HISTORY_REQUIRED_FIELDS = frozenset({"history_note_text"})
HISTORY_REQUIRED_FIELDS_WITH_RECORD = frozenset(
    {"history_record_no", "history_note_text"}
)

_FP_RE = re.compile(r"^[a-f0-9]{64}$")


MAPPING_UNKNOWN_HISTORY = "That history destination field is not supported."
MAPPING_HISTORY_HEADER_MISSING = "That history source column is not in this spreadsheet."
MAPPING_HISTORY_EMPTY = "Each mapped history column must have a name."
MAPPING_HISTORY_DUPLICATE = "Each history source column can map to only one field."
MAPPING_HISTORY_REQUIRED = (
    "Map History note text (and History record No. or History company name) before continuing."
)
HISTORY_FILE_REQUIRED_FOR_MAPPING = "This import has no history file to map."
HISTORY_FILE_REQUIRED = "Upload a history CSV for history-only Client Data Import."
HISTORY_TOO_LARGE = "This history file is too large to upload."
HISTORY_NOT_CSV = "Upload a CSV history file."
BATCH_NOT_REUSABLE = "This import batch can no longer be previewed."
PROSPECTS_OR_HISTORY_REQUIRED = "Upload a prospects spreadsheet and/or a history CSV."



def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_loads(text: object, default: Any) -> Any:
    raw = _blank(text)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ensure_client_data_import_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_data_import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                crm_batch_id INTEGER,
                import_mode TEXT NOT NULL DEFAULT 'full',
                status TEXT NOT NULL DEFAULT 'previewed',
                history_original_filename TEXT NOT NULL DEFAULT '',
                history_sha256 TEXT NOT NULL DEFAULT '',
                history_file_size_bytes INTEGER NOT NULL DEFAULT 0,
                history_staging_path TEXT NOT NULL DEFAULT '',
                history_headers_json TEXT NOT NULL DEFAULT '[]',
                history_mapping_json TEXT NOT NULL DEFAULT '{}',
                history_row_count INTEGER NOT NULL DEFAULT 0,
                plan_fingerprint TEXT NOT NULL DEFAULT '',
                preview_json TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '',
                staging_cleanup_status TEXT NOT NULL DEFAULT '',
                staging_cleanup_error TEXT NOT NULL DEFAULT '',
                staging_cleanup_at TEXT NOT NULL DEFAULT '',
                uploaded_by_user_id INTEGER,
                uploaded_by_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT NOT NULL DEFAULT '',
                confirmed_by_user_id INTEGER,
                closed_excluded_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (crm_batch_id) REFERENCES crm_import_batches(id) ON DELETE CASCADE,
                FOREIGN KEY (uploaded_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (confirmed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_client_data_import_batches_client
                ON client_data_import_batches(client_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_client_data_import_batches_crm
                ON client_data_import_batches(crm_batch_id);
            """
        )
        _migrate_client_data_import_history_only(conn)
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _cdi_columns(conn) -> dict[str, bool]:
    rows = conn.execute("PRAGMA table_info(client_data_import_batches)").fetchall()
    return {str(r[1]): (int(r[3]) == 1) for r in rows}


def _migrate_client_data_import_history_only(conn) -> None:
    """Add import_mode and allow nullable crm_batch_id on existing DBs."""
    exists = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'client_data_import_batches'
        """
    ).fetchone()
    if exists is None:
        return
    cols = _cdi_columns(conn)
    if "import_mode" not in cols:
        conn.execute(
            """
            ALTER TABLE client_data_import_batches
            ADD COLUMN import_mode TEXT NOT NULL DEFAULT 'full'
            """
        )
        cols = _cdi_columns(conn)
    # Rebuild when crm_batch_id is still NOT NULL so history-only rows can omit it.
    if cols.get("crm_batch_id") is True:
        conn.executescript(
            """
            CREATE TABLE client_data_import_batches__hist_only (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                crm_batch_id INTEGER,
                import_mode TEXT NOT NULL DEFAULT 'full',
                status TEXT NOT NULL DEFAULT 'previewed',
                history_original_filename TEXT NOT NULL DEFAULT '',
                history_sha256 TEXT NOT NULL DEFAULT '',
                history_file_size_bytes INTEGER NOT NULL DEFAULT 0,
                history_staging_path TEXT NOT NULL DEFAULT '',
                history_headers_json TEXT NOT NULL DEFAULT '[]',
                history_mapping_json TEXT NOT NULL DEFAULT '{}',
                history_row_count INTEGER NOT NULL DEFAULT 0,
                plan_fingerprint TEXT NOT NULL DEFAULT '',
                preview_json TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '',
                staging_cleanup_status TEXT NOT NULL DEFAULT '',
                staging_cleanup_error TEXT NOT NULL DEFAULT '',
                staging_cleanup_at TEXT NOT NULL DEFAULT '',
                uploaded_by_user_id INTEGER,
                uploaded_by_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT NOT NULL DEFAULT '',
                confirmed_by_user_id INTEGER,
                closed_excluded_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (crm_batch_id) REFERENCES crm_import_batches(id) ON DELETE CASCADE,
                FOREIGN KEY (uploaded_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (confirmed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            INSERT INTO client_data_import_batches__hist_only (
                id, client_id, crm_batch_id, import_mode, status,
                history_original_filename, history_sha256, history_file_size_bytes,
                history_staging_path, history_headers_json, history_mapping_json,
                history_row_count, plan_fingerprint, preview_json, result_json,
                staging_cleanup_status, staging_cleanup_error, staging_cleanup_at,
                uploaded_by_user_id, uploaded_by_name, created_at, updated_at,
                confirmed_at, confirmed_by_user_id, closed_excluded_count
            )
            SELECT
                id, client_id, crm_batch_id,
                COALESCE(NULLIF(import_mode, ''), 'full'),
                status,
                history_original_filename, history_sha256, history_file_size_bytes,
                history_staging_path, history_headers_json, history_mapping_json,
                history_row_count, plan_fingerprint, preview_json, result_json,
                staging_cleanup_status, staging_cleanup_error, staging_cleanup_at,
                uploaded_by_user_id, uploaded_by_name, created_at, updated_at,
                confirmed_at, confirmed_by_user_id, closed_excluded_count
            FROM client_data_import_batches;
            DROP TABLE client_data_import_batches;
            ALTER TABLE client_data_import_batches__hist_only
                RENAME TO client_data_import_batches;
            CREATE INDEX IF NOT EXISTS idx_client_data_import_batches_client
                ON client_data_import_batches(client_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_client_data_import_batches_crm
                ON client_data_import_batches(crm_batch_id);
            """
        )


def _require_admin(actor: NorthStarUser, client_id: int) -> None:
    if actor is None or not bool(actor.active) or not bool(actor.is_administrator):
        raise PermissionError("Not authorized.")
    if client_id <= 0:
        raise ValueError(CLIENT_REQUIRED)


def _load_cd_row(conn, client_id: int, batch_id: int) -> Any:
    row = conn.execute(
        """
        SELECT * FROM client_data_import_batches
        WHERE id = ? AND client_id = ?
        """,
        (int(batch_id), int(client_id)),
    ).fetchone()
    if row is None:
        raise LookupError("Import batch not found.")
    return row


def _parse_history_headers(content: bytes) -> tuple[list[str], int]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = [_blank(h) for h in (reader.fieldnames or []) if _blank(h)]
    if not headers:
        raise ValueError("History CSV must include a header row.")
    row_count = 0
    for _ in reader:
        row_count += 1
        if row_count > MAX_HISTORY_ROWS:
            raise ValueError(f"History CSV exceeds {MAX_HISTORY_ROWS} rows.")
    return headers, row_count


def _history_sample_rows(content: bytes, limit: int = HISTORY_SAMPLE_ROWS) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, str]] = []
    for raw in reader:
        out.append({_blank(k): "" if v is None else str(v) for k, v in raw.items() if _blank(k)})
        if len(out) >= limit:
            break
    return out


def _exact_header(headers: list[str], submitted: str) -> str | None:
    for header in headers:
        if header == submitted:
            return header
    for header in headers:
        if header.strip() == submitted:
            return header
    return None


def _normalize_history_mapping(
    raw: dict[str, str] | None, headers: list[str]
) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(MAPPING_UNKNOWN_HISTORY)
    normalized: dict[str, str] = {}
    used: dict[str, str] = {}
    for dest_raw, src_raw in raw.items():
        dest = _blank(dest_raw)
        if dest not in HISTORY_CANONICAL_FIELDS:
            raise ValueError(MAPPING_UNKNOWN_HISTORY)
        submitted = _blank(src_raw)
        if not submitted:
            raise ValueError(MAPPING_HISTORY_EMPTY)
        exact = _exact_header(headers, submitted)
        if exact is None:
            raise ValueError(MAPPING_HISTORY_HEADER_MISSING)
        prior = used.get(exact)
        if prior and prior != dest:
            raise ValueError(MAPPING_HISTORY_DUPLICATE)
        used[exact] = dest
        normalized[dest] = exact
    if normalized:
        if "history_note_text" not in normalized and "history_contact_note" not in normalized:
            raise ValueError(MAPPING_HISTORY_REQUIRED)
        if (
            "history_record_no" not in normalized
            and "history_company_name" not in normalized
        ):
            raise ValueError(MAPPING_HISTORY_REQUIRED)
    return normalized


def _row_get_mapped(row: dict[str, str], mapping: dict[str, str], field: str) -> str:
    header = mapping.get(field)
    if not header:
        return ""
    return _blank(row.get(header))


def _row_get_raw_mapped(row: dict[str, str], mapping: dict[str, str], field: str) -> str:
    header = mapping.get(field)
    if not header:
        return ""
    value = row.get(header)
    return "" if value is None else str(value)


def _row_is_fully_blank(row: dict[str, str]) -> bool:
    return not any(_blank(v) for v in row.values())


def _resolve_note_text(raw: dict[str, str], mapping: dict[str, str]) -> str:
    note = _row_get_raw_mapped(raw, mapping, "history_note_text")
    note = note.replace("\r\n", "\n").replace("\r", "\n")
    if note.strip():
        return note
    contact_note = _row_get_raw_mapped(raw, mapping, "history_contact_note")
    if not contact_note.strip():
        # Brown exports often include Contact_Note even when not mapped.
        for key, value in raw.items():
            if _blank(key).casefold().replace(" ", "") == "contact_note":
                contact_note = "" if value is None else str(value)
                break
    contact_note = contact_note.replace("\r\n", "\n").replace("\r", "\n")
    return contact_note


def parse_history_with_mapping(
    content: bytes | str,
    mapping: dict[str, str],
    *,
    client_id: int,
    closed_record_nos: set[str] | None = None,
) -> ParsedHistory:
    """Parse history CSV using Client Data Import destination→header mapping."""
    text = content.decode("utf-8-sig") if isinstance(content, (bytes, bytearray)) else content
    reader = csv.DictReader(io.StringIO(text))
    out = ParsedHistory()
    if not mapping:
        out.errors.append(MAPPING_HISTORY_REQUIRED)
        return out
    if "history_note_text" not in mapping and "history_contact_note" not in mapping:
        out.errors.append(MAPPING_HISTORY_REQUIRED)
        return out
    if "history_record_no" not in mapping and "history_company_name" not in mapping:
        out.errors.append(MAPPING_HISTORY_REQUIRED)
        return out

    closed = set(closed_record_nos or set())
    rows = 0
    for idx, raw_in in enumerate(reader, start=2):
        rows += 1
        if rows > MAX_HISTORY_ROWS:
            out.errors.append(f"History CSV exceeds {MAX_HISTORY_ROWS} rows.")
            break
        raw = {
            _blank(k): ("" if v is None else str(v))
            for k, v in raw_in.items()
            if _blank(k)
        }
        if _row_is_fully_blank(raw):
            out.blank_rows_skipped += 1
            continue
        rn = normalize_record_no(_row_get_mapped(raw, mapping, "history_record_no"))
        company_name = _row_get_mapped(raw, mapping, "history_company_name")
        row_status = _row_get_mapped(raw, mapping, "history_status")
        if is_closed_status(row_status) or (rn and rn in closed):
            if rn:
                closed.add(rn)
            out.excluded_closed_events += 1
            continue
        note = _resolve_note_text(raw, mapping)
        if not note.strip():
            out.blank_history_skipped += 1
            continue
        if not rn and not company_name:
            out.errors.append(
                f"History row {idx} is missing Record No. and company name."
            )
            continue
        if is_routine_marketing_send_note(note):
            out.excluded_marketing_events += 1
            continue
        contact_no = normalize_record_no(
            _row_get_mapped(raw, mapping, "history_contact_no")
        )
        event_at = _row_get_mapped(raw, mapping, "history_event_at")
        source_note_id = _row_get_mapped(raw, mapping, "history_source_note_id")
        provided_hash = _row_get_mapped(raw, mapping, "history_event_hash")
        event_hash = compute_history_event_hash(
            client_id=int(client_id),
            company_record_no=rn,
            contact_key=contact_no,
            note_text=note,
            event_at=event_at,
            source_note_id=source_note_id,
            event_hash=provided_hash,
        )
        attribution = (
            _row_get_mapped(raw, mapping, "history_attribution") or UNATTRIBUTED_LABEL
        )
        event = HistoryEventRow(
            record_no=rn,
            event_at=event_at,
            author=_row_get_mapped(raw, mapping, "history_author"),
            event_type=_row_get_mapped(raw, mapping, "history_event_type"),
            attribution=attribution,
            attribution_evidence=_row_get_mapped(
                raw, mapping, "history_attribution_evidence"
            ),
            source_file=_row_get_mapped(raw, mapping, "history_source_file"),
            note_text=note,
            event_hash=event_hash,
            company_name=company_name,
            event_sequence=_row_get_mapped(raw, mapping, "history_event_sequence"),
            source_row=idx,
            contact_no=contact_no,
        )
        out.events.append(event)
        if len(note) > 2000:
            out.long_note_count += 1
        if attribution.casefold() == UNATTRIBUTED_LABEL.casefold():
            out.unattributed_count += 1
    return out


def _prospects_counts_from_plan(counts: dict[str, int]) -> ClientDataImportProspectsCounts:
    def n(key: str) -> int:
        return int(counts.get(key) or 0)

    return ClientDataImportProspectsCounts(
        companies_create=n("create_company"),
        companies_reuse=n("use_existing_company"),
        companies_possible=n("possible_company_match"),
        contacts_create=n("create_contact"),
        contacts_reuse=n("use_existing_contact"),
        contacts_possible=n("possible_contact_match"),
        relationships_create=n("create_client_relationship"),
        relationships_existing=n("relationship_already_exists"),
        statuses_imported=n("use_imported_status"),
        statuses_conflicting=n("status_conflict"),
        statuses_invalid=n("invalid_status"),
        notes_set=n("set_imported_notes"),
        notes_appended=n("append_imported_notes"),
        notes_duplicate=n("imported_notes_already_present"),
        blocking=n("blocking_error"),
        needs_review=n("needs_review_rows"),
        possible_company_match=n("possible_company_match"),
        possible_contact_match=n("possible_contact_match"),
        create_company=n("create_company"),
        use_existing_company=n("use_existing_company"),
        possible_company_match_count=n("possible_company_match"),
        create_contact=n("create_contact"),
        use_existing_contact=n("use_existing_contact"),
        create_client_relationship=n("create_client_relationship"),
        relationship_already_exists=n("relationship_already_exists"),
        use_imported_status=n("use_imported_status"),
        status_conflict=n("status_conflict"),
        invalid_status=n("invalid_status"),
        set_imported_notes=n("set_imported_notes"),
        append_imported_notes=n("append_imported_notes"),
        imported_notes_already_present=n("imported_notes_already_present"),
        blocking_error=n("blocking_error"),
        needs_review_rows=n("needs_review_rows"),
        excluded_closed=n("excluded_closed"),
    )


def _planned_company_record_nos(plan) -> tuple[set[str], set[str]]:
    """Return (importable_record_nos, closed_record_nos) from a CRM plan."""
    importable: set[str] = set()
    closed: set[str] = set()
    for row in plan.rows:
        mapped = row.mapped or {}
        rn = normalize_record_no(mapped.get("external_record_no"))
        if row.company_action == EXCLUDED_CLOSED:
            if rn:
                closed.add(rn)
            continue
        if rn and row.company_action in {"create_company", "use_existing_company"}:
            importable.add(rn)
    return importable, closed


def _history_plan_counts(
    conn,
    *,
    client_id: int,
    history: ParsedHistory | None,
    importable_record_nos: set[str],
    will_create_record_nos: set[str],
    history_only: bool = False,
) -> ClientDataImportHistoryCounts:
    if history is None:
        return ClientDataImportHistoryCounts()

    insert = already = within_batch = invalid = unresolved = 0
    unmatched = ambiguous = matched_rn = matched_name = 0
    invalid = len(history.errors or [])

    company_ids: dict[str, int] = {}
    if not history_only:
        for rn in importable_record_nos | will_create_record_nos:
            existing = _company_by_record_no(conn, rn)
            if existing is not None:
                company_ids[rn] = int(existing["id"])

    # Company-scoped shared history: ignore source contact_no so the same
    # company note is one event regardless of which contact row carried it.
    db_cache: dict[int, object] = {}
    batch_seen: dict[int, MutableHistoryDedupeIndex] = {}
    for event in history.events:
        if history_only:
            match = resolve_history_company(
                conn,
                client_id=int(client_id),
                record_no=event.record_no,
                company_name=event.company_name,
            )
            event.match_status = match.status
            event.match_method = match.method
            event.company_id = match.company_id
            if match.status == "ambiguous":
                ambiguous += 1
                event.needs_review = True
                event.review_reason = "ambiguous_company"
                continue
            if match.status != "matched" or match.company_id is None:
                unmatched += 1
                unresolved += 1
                event.needs_review = True
                event.review_reason = "unmatched_company"
                continue
            if match.method == "record_no":
                matched_rn += 1
            elif match.method == "company_name":
                matched_name += 1
            company_id = int(match.company_id)
        else:
            rn = event.record_no
            if rn not in importable_record_nos and rn not in will_create_record_nos:
                if rn not in company_ids:
                    existing = _company_by_record_no(conn, rn)
                    if existing is None:
                        unresolved += 1
                        unmatched += 1
                        continue
                    unresolved += 1
                    unmatched += 1
                    continue
            company_id = company_ids.get(rn)
            if company_id is None:
                # Planned create — event will insert after confirm.
                insert += 1
                continue
            matched_rn += 1

        db_index = db_cache.get(company_id)
        if db_index is None:
            db_index = load_existing_history_dedupe_index(
                conn, client_id=int(client_id), company_id=int(company_id)
            )
            db_cache[company_id] = db_index
        present_kwargs = dict(
            client_id=int(client_id),
            company_id=int(company_id),
            contact_key="",
            note_text=event.note_text,
            event_at=event.event_at,
            event_hash=event.event_hash,
        )
        if history_event_already_present(db_index, **present_kwargs):  # type: ignore[arg-type]
            already += 1
            continue
        seen = batch_seen.get(company_id)
        if seen is None:
            seen = MutableHistoryDedupeIndex.empty()
            batch_seen[company_id] = seen
        if history_event_already_present(seen.as_existing(), **present_kwargs):
            within_batch += 1
            continue
        insert += 1
        seen.register_shared_event(
            client_id=int(client_id),
            company_id=int(company_id),
            note_text=event.note_text,
            event_at=event.event_at,
            event_hash=event.event_hash,
        )

    needs_review = ambiguous + unmatched
    return ClientDataImportHistoryCounts(
        insert=insert,
        already_present=already,
        duplicate_within_batch=within_batch,
        invalid=invalid,
        unresolved=unresolved,
        excluded_marketing=int(getattr(history, "excluded_marketing_events", 0) or 0),
        blank_rows=int(getattr(history, "blank_rows_skipped", 0) or 0),
        blank_history=int(getattr(history, "blank_history_skipped", 0) or 0),
        unmatched_companies=unmatched,
        ambiguous_companies=ambiguous,
        matched_by_record_no=matched_rn,
        matched_by_name=matched_name,
        needs_review=needs_review,
    )


def _combined_fingerprint(
    *,
    crm_fingerprint: str,
    history_sha256: str,
    history_mapping: dict[str, str],
    closed_excluded_count: int,
    import_mode: str = IMPORT_MODE_FULL,
) -> str:
    payload = {
        "import_mode": import_mode or IMPORT_MODE_FULL,
        "crm_plan_fingerprint": crm_fingerprint,
        "history_sha256": history_sha256 or "",
        "history_mapping": history_mapping or {},
        "closed_excluded_count": int(closed_excluded_count),
        "closed_policy": CLOSED_POLICY_TEXT,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _empty_crm_batch_stub(*, client_id: int, history_filename: str = "") -> Any:
    return SimpleNamespace(
        batch_id=0,
        client_id=int(client_id),
        original_filename=history_filename or "",
        file_type="csv",
        worksheet_name="",
        file_size_bytes=0,
        sha256="",
        headers=[],
        warnings=[],
        error_message="",
        total_rows=0,
        source_row_count=0,
        blank_row_count=0,
        error_row_count=0,
        reusable=True,
        expires_at="",
        created_at="",
        updated_at="",
        cancelled_at="",
        uploaded_by_user_id=None,
        uploaded_by_name="",
        mapping={},
        mapping_updated_at="",
        mapping_updated_by_user_id=None,
        sample_rows=[],
        status=STATUS_PREVIEWED,
    )


def _cd_import_mode(cd_row: Any) -> str:
    mode = _blank(cd_row["import_mode"]) if "import_mode" in cd_row.keys() else ""
    return mode or IMPORT_MODE_FULL


def _cd_crm_batch_id(cd_row: Any) -> int | None:
    raw = cd_row["crm_batch_id"]
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _batch_view_from_parts(
    *,
    cd_row: Any,
    crm_batch: Any,
    history_sample: list[dict[str, str]] | None = None,
) -> ClientDataImportBatchView:
    history_headers = _json_loads(cd_row["history_headers_json"], [])
    if not isinstance(history_headers, list):
        history_headers = []
    history_mapping = _json_loads(cd_row["history_mapping_json"], {})
    if not isinstance(history_mapping, dict):
        history_mapping = {}
    prospects_mapping = dict(getattr(crm_batch, "mapping", {}) or {})
    crm_batch_id = _cd_crm_batch_id(cd_row)
    return ClientDataImportBatchView(
        id=int(cd_row["id"]),
        batch_id=int(cd_row["id"]),
        client_id=int(cd_row["client_id"]),
        crm_batch_id=crm_batch_id,
        import_mode=_cd_import_mode(cd_row),
        status=_blank(cd_row["status"]),
        original_filename=getattr(crm_batch, "original_filename", "") or "",
        history_filename=_blank(cd_row["history_original_filename"]),
        history_original_filename=_blank(cd_row["history_original_filename"]),
        file_type=getattr(crm_batch, "file_type", "") or "",
        worksheet_name=getattr(crm_batch, "worksheet_name", "") or "",
        file_size_bytes=int(getattr(crm_batch, "file_size_bytes", 0) or 0),
        sha256=getattr(crm_batch, "sha256", "") or "",
        headers=list(getattr(crm_batch, "headers", []) or []),
        history_headers=[str(h) for h in history_headers],
        warnings=list(getattr(crm_batch, "warnings", []) or []),
        error_message=getattr(crm_batch, "error_message", "") or "",
        total_rows=int(getattr(crm_batch, "total_rows", 0) or 0),
        source_row_count=int(getattr(crm_batch, "source_row_count", 0) or 0),
        blank_row_count=int(getattr(crm_batch, "blank_row_count", 0) or 0),
        error_row_count=int(getattr(crm_batch, "error_row_count", 0) or 0),
        history_row_count=int(cd_row["history_row_count"] or 0),
        reusable=bool(getattr(crm_batch, "reusable", False)),
        expires_at=getattr(crm_batch, "expires_at", "") or "",
        created_at=_blank(cd_row["created_at"]) or getattr(crm_batch, "created_at", "") or "",
        updated_at=_blank(cd_row["updated_at"]) or getattr(crm_batch, "updated_at", "") or "",
        cancelled_at=getattr(crm_batch, "cancelled_at", "") or "",
        uploaded_by_user_id=(
            int(cd_row["uploaded_by_user_id"])
            if cd_row["uploaded_by_user_id"] is not None
            else getattr(crm_batch, "uploaded_by_user_id", None)
        ),
        uploaded_by_name=_blank(cd_row["uploaded_by_name"])
        or getattr(crm_batch, "uploaded_by_name", "")
        or "",
        mapping=prospects_mapping,
        prospects_mapping=prospects_mapping,
        history_mapping={str(k): str(v) for k, v in history_mapping.items()},
        mapping_updated_at=getattr(crm_batch, "mapping_updated_at", "") or "",
        mapping_updated_by_user_id=getattr(crm_batch, "mapping_updated_by_user_id", None),
        closed_policy=CLOSED_POLICY_TEXT,
        closed_policy_note=CLOSED_POLICY_TEXT,
        closed_policy_notes=CLOSED_POLICY_TEXT,
        sample_rows=list(getattr(crm_batch, "sample_rows", []) or []),
        history_sample_rows=history_sample or [],
        staging_cleanup_status=_blank(cd_row["staging_cleanup_status"]),
        staging_cleanup_error=_blank(cd_row["staging_cleanup_error"]),
        staging_cleanup_at=_blank(cd_row["staging_cleanup_at"]),
        plan_fingerprint=_blank(cd_row["plan_fingerprint"]),
        closed_excluded_count=int(cd_row["closed_excluded_count"] or 0),
    )


def get_client_data_import_batch(
    client_id: int, batch_id: int, *, include_sample: bool = True
) -> ClientDataImportBatchView:
    with get_connection() as conn:
        cd_row = _load_cd_row(conn, client_id, batch_id)
        crm_batch_id = _cd_crm_batch_id(cd_row)
        import_mode = _cd_import_mode(cd_row)
        history_name = _blank(cd_row["history_original_filename"])
    if import_mode == IMPORT_MODE_HISTORY_ONLY or crm_batch_id is None:
        crm_batch = _empty_crm_batch_stub(
            client_id=client_id, history_filename=history_name
        )
    else:
        crm_batch = get_crm_import_batch(
            client_id, int(crm_batch_id), include_sample=include_sample
        )
    history_sample: list[dict[str, str]] = []
    staging = _blank(cd_row["history_staging_path"])
    if include_sample and staging:
        path = Path(staging)
        if path.is_file():
            try:
                history_sample = _history_sample_rows(path.read_bytes())
            except Exception:
                history_sample = []
    return _batch_view_from_parts(
        cd_row=cd_row, crm_batch=crm_batch, history_sample=history_sample
    )


def _stage_history_file(
    *,
    client_id: int,
    history_filename: str | None,
    history_content: bytes,
) -> tuple[str, str, int, str, list[str], int]:
    if len(history_content) > HISTORY_MAX_FILE_BYTES:
        raise ValueError(HISTORY_TOO_LARGE)
    safe_hist = _safe_filename(history_filename or "history.csv")
    if not safe_hist.lower().endswith(".csv"):
        raise ValueError(HISTORY_NOT_CSV)
    history_headers, history_row_count = _parse_history_headers(history_content)
    history_sha = _sha256_bytes(history_content)
    history_size = len(history_content)
    token = uuid.uuid4().hex
    dest_dir = _import_root() / str(client_id) / token
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "history.csv"
    dest.write_bytes(history_content)
    return safe_hist, history_sha, history_size, str(dest), history_headers, history_row_count


def upload_client_data_import(
    *,
    client_id: int,
    actor: NorthStarUser,
    prospects_filename: str | None = None,
    prospects_content: bytes | None = None,
    history_filename: str | None = None,
    history_content: bytes | None = None,
    worksheet: str = "",
) -> ClientDataImportUploadResult:
    _require_admin(actor, client_id)
    has_prospects = bool(prospects_content)
    has_history = bool(history_content)
    if not has_prospects and not has_history:
        raise ValueError(PROSPECTS_OR_HISTORY_REQUIRED)
    if has_prospects and not _blank(prospects_filename):
        prospects_filename = prospects_filename or "prospects.csv"

    history_only = has_history and not has_prospects
    import_mode = IMPORT_MODE_HISTORY_ONLY if history_only else IMPORT_MODE_FULL

    history_name = ""
    history_sha = ""
    history_size = 0
    history_path = ""
    history_headers: list[str] = []
    history_row_count = 0
    if has_history:
        (
            history_name,
            history_sha,
            history_size,
            history_path,
            history_headers,
            history_row_count,
        ) = _stage_history_file(
            client_id=client_id,
            history_filename=history_filename,
            history_content=history_content or b"",
        )
    elif history_only:
        raise ValueError(HISTORY_FILE_REQUIRED)

    crm_batch_id: int | None = None
    crm_kind = "client_data_import"
    crm_message = "Preview ready. Nothing was written to companies or contacts."
    crm_filename = history_name if history_only else (prospects_filename or "")
    crm_file_type = "csv"

    if not history_only:
        crm_result = upload_crm_import(
            client_id=client_id,
            actor=actor,
            filename=prospects_filename or "prospects.csv",
            content=prospects_content or b"",
            worksheet=worksheet or "",
        )
        if crm_result.needs_worksheet or crm_result.batch is None:
            return ClientDataImportUploadResult(
                kind=crm_result.kind,
                needs_worksheet=crm_result.needs_worksheet,
                visible_sheets=list(crm_result.visible_sheets or []),
                filename=crm_result.filename,
                file_type=crm_result.file_type,
                message=crm_result.message,
                batch=None,
            )
        crm_batch_id = int(crm_result.batch.batch_id)
        crm_kind = crm_result.kind
        crm_message = crm_result.message or crm_message
        crm_filename = crm_result.filename
        crm_file_type = crm_result.file_type
        batch_status = (
            STATUS_PREVIEWED
            if _blank(crm_result.batch.status) == STATUS_PREVIEWED
            else STATUS_FAILED
        )
    else:
        batch_status = STATUS_PREVIEWED
        crm_message = (
            "History-only preview ready. Nothing was written to companies or contacts."
        )

    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO client_data_import_batches (
                client_id, crm_batch_id, import_mode, status,
                history_original_filename, history_sha256, history_file_size_bytes,
                history_staging_path, history_headers_json, history_mapping_json,
                history_row_count,
                uploaded_by_user_id, uploaded_by_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)
            """,
            (
                int(client_id),
                crm_batch_id,
                import_mode,
                batch_status,
                history_name,
                history_sha,
                history_size,
                history_path,
                json.dumps(history_headers),
                history_row_count,
                int(actor.id),
                _blank(getattr(actor, "full_name", "")),
                now,
                now,
            ),
        )
        cd_batch_id = int(cur.lastrowid)
        conn.commit()

    batch = get_client_data_import_batch(client_id, cd_batch_id, include_sample=True)
    return ClientDataImportUploadResult(
        kind=crm_kind,
        needs_worksheet=False,
        visible_sheets=[],
        filename=crm_filename,
        file_type=crm_file_type,
        message=crm_message,
        batch=batch,
    )


def save_client_data_import_mapping(
    client_id: int,
    batch_id: int,
    *,
    actor: NorthStarUser,
    prospects_mapping: dict[str, str] | None = None,
    history_mapping: dict[str, str] | None = None,
) -> ClientDataImportBatchView:
    _require_admin(actor, client_id)
    with get_connection() as conn:
        cd_row = _load_cd_row(conn, client_id, batch_id)
        if _blank(cd_row["status"]) != STATUS_PREVIEWED:
            raise BatchNotReusable(BATCH_NOT_REUSABLE)
        import_mode = _cd_import_mode(cd_row)
        crm_batch_id = _cd_crm_batch_id(cd_row)
        headers = _json_loads(cd_row["history_headers_json"], [])
        if not isinstance(headers, list):
            headers = []
        headers = [str(h) for h in headers]
        has_history = bool(_blank(cd_row["history_staging_path"]) or _blank(cd_row["history_sha256"]))
        if history_mapping and not has_history:
            raise ValueError(HISTORY_FILE_REQUIRED_FOR_MAPPING)
        if has_history and history_mapping is not None:
            normalized_history = _normalize_history_mapping(history_mapping, headers)
        elif has_history:
            existing = _json_loads(cd_row["history_mapping_json"], {})
            normalized_history = (
                {str(k): str(v) for k, v in existing.items()}
                if isinstance(existing, dict)
                else {}
            )
        else:
            normalized_history = {}
        if import_mode == IMPORT_MODE_HISTORY_ONLY and not normalized_history:
            raise ValueError(MAPPING_HISTORY_REQUIRED)
        now = _now()
        conn.execute(
            """
            UPDATE client_data_import_batches
            SET history_mapping_json = ?,
                updated_at = ?,
                plan_fingerprint = '',
                preview_json = ''
            WHERE id = ? AND client_id = ?
            """,
            (
                json.dumps(normalized_history, separators=(",", ":")),
                now,
                int(batch_id),
                int(client_id),
            ),
        )
        conn.commit()

    if import_mode != IMPORT_MODE_HISTORY_ONLY and crm_batch_id is not None:
        save_crm_import_mapping(
            client_id,
            int(crm_batch_id),
            actor=actor,
            mapping=prospects_mapping or {},
        )
    return get_client_data_import_batch(client_id, batch_id, include_sample=True)


def _compute_dry_run(
    conn,
    *,
    client_id: int,
    cd_row: Any,
) -> tuple[ClientDataImportDryRunResponse, Any, ClientDataImportHistoryCounts, dict[str, str]]:
    import_mode = _cd_import_mode(cd_row)
    history_only = import_mode == IMPORT_MODE_HISTORY_ONLY
    crm_batch_id = _cd_crm_batch_id(cd_row)

    plan = None
    importable_rns: set[str] = set()
    closed_rns: set[str] = set()
    will_create: set[str] = set()
    prospects = ClientDataImportProspectsCounts()
    closed_excluded = 0
    companies_excluded_closed = 0
    contacts_excluded_closed = 0
    crm_fingerprint = ""
    status_catalog: list[str] = []
    total_rows = 0

    if not history_only:
        if crm_batch_id is None:
            raise BatchNotReusable("Prospects batch is missing.")
        plan = plan_crm_import_batch(
            conn,
            client_id=client_id,
            batch_id=int(crm_batch_id),
            identity_mode=IDENTITY_CLIENT_DATA,
            exclude_closed=True,
        )
        importable_rns, closed_rns = _planned_company_record_nos(plan)
        will_create = {
            normalize_record_no((row.mapped or {}).get("external_record_no"))
            for row in plan.rows
            if row.company_action == "create_company"
        }
        will_create.discard("")
        closed_excluded = int(plan.counts.get("excluded_closed") or 0)
        closed_company_rns = {
            normalize_record_no((row.mapped or {}).get("external_record_no"))
            for row in plan.rows
            if row.company_action == EXCLUDED_CLOSED
        }
        closed_company_rns.discard("")
        contacts_excluded_closed = closed_excluded
        companies_excluded_closed = len(closed_company_rns)
        prospects = _prospects_counts_from_plan(plan.counts)
        crm_fingerprint = plan.plan_fingerprint
        status_catalog = list(plan.status_catalog or [])
        total_rows = int(plan.total_rows)

    history_mapping = _json_loads(cd_row["history_mapping_json"], {})
    if not isinstance(history_mapping, dict):
        history_mapping = {}
    history_mapping = {str(k): str(v) for k, v in history_mapping.items()}
    history_sha = _blank(cd_row["history_sha256"])
    history_counts = ClientDataImportHistoryCounts()
    history_parsed: ParsedHistory | None = None
    staging = _blank(cd_row["history_staging_path"])
    if staging or history_sha or history_only:
        if not staging or not Path(staging).is_file():
            raise BatchNotReusable("Staged history file is missing.")
        content = Path(staging).read_bytes()
        history_parsed = parse_history_with_mapping(
            content,
            history_mapping,
            client_id=int(client_id),
            closed_record_nos=closed_rns,
        )
        history_counts = _history_plan_counts(
            conn,
            client_id=int(client_id),
            history=history_parsed,
            importable_record_nos=importable_rns,
            will_create_record_nos=will_create,
            history_only=history_only,
        )

    history_excluded = (
        int(getattr(history_parsed, "excluded_closed_events", 0) or 0)
        if history_parsed
        else 0
    )
    if history_parsed is not None:
        history_counts.excluded_closed = history_excluded
        history_counts.excluded_marketing = int(
            getattr(history_parsed, "excluded_marketing_events", 0) or 0
        )
        history_counts.blank_rows = int(
            getattr(history_parsed, "blank_rows_skipped", 0) or 0
        )
        history_counts.blank_history = int(
            getattr(history_parsed, "blank_history_skipped", 0) or 0
        )

    combined_fp = _combined_fingerprint(
        crm_fingerprint=crm_fingerprint,
        history_sha256=history_sha,
        history_mapping=history_mapping,
        closed_excluded_count=closed_excluded,
        import_mode=import_mode,
    )
    confirm_allowed = (
        prospects.needs_review_rows == 0
        and prospects.companies_possible == 0
        and prospects.contacts_possible == 0
        and prospects.status_conflict == 0
        and prospects.invalid_status == 0
        and history_counts.invalid == 0
        and history_counts.unresolved == 0
        and history_counts.ambiguous_companies == 0
        and history_counts.unmatched_companies == 0
    )

    flat = {
        **prospects.model_dump(),
        **history_counts.model_dump(),
        "companies_excluded_closed": companies_excluded_closed,
        "contacts_excluded_closed": contacts_excluded_closed,
        "history_events_excluded_closed": history_excluded,
    }
    response = ClientDataImportDryRunResponse(
        id=int(cd_row["id"]),
        batch_id=int(cd_row["id"]),
        client_id=int(client_id),
        import_mode=import_mode,
        plan_fingerprint=combined_fp,
        status=_blank(cd_row["status"]),
        status_catalog=status_catalog,
        closed_excluded_count=closed_excluded,
        closed_policy=CLOSED_POLICY_TEXT,
        closed_policy_note=CLOSED_POLICY_TEXT,
        closed_policy_notes=CLOSED_POLICY_TEXT,
        confirm_allowed=confirm_allowed,
        prospects=prospects,
        history=history_counts,
        counts=flat,
        rows=[],
        sample_rows=[],
        total_rows=total_rows if not history_only else int(history_counts.insert or 0),
        crm_plan_fingerprint=crm_fingerprint,
        companies_excluded_closed=companies_excluded_closed,
        contacts_excluded_closed=contacts_excluded_closed,
        history_events_excluded_closed=history_excluded,
    )
    return response, plan, history_counts, history_mapping


def dry_run_client_data_import(
    client_id: int, batch_id: int, *, actor: NorthStarUser | None = None
) -> ClientDataImportDryRunResponse:
    if actor is not None:
        _require_admin(actor, client_id)
    elif client_id <= 0:
        raise ValueError(CLIENT_REQUIRED)
    # Schema must already exist from controlled migration (no request-time DDL).
    with get_connection() as conn:
        cd_row = _load_cd_row(conn, client_id, batch_id)
        if _blank(cd_row["status"]) != STATUS_PREVIEWED:
            raise BatchNotReusable(BATCH_NOT_REUSABLE)
        conn.execute("PRAGMA query_only = ON")
        response, plan, _history_counts, _mapping = _compute_dry_run(
            conn, client_id=client_id, cd_row=cd_row
        )
    # Persist fingerprint/preview on a writable connection (audit only).
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE client_data_import_batches
            SET plan_fingerprint = ?,
                preview_json = ?,
                closed_excluded_count = ?,
                updated_at = ?
            WHERE id = ? AND client_id = ? AND status = ?
            """,
            (
                response.plan_fingerprint,
                json.dumps(response.model_dump()),
                int(response.closed_excluded_count or 0),
                _now(),
                int(batch_id),
                int(client_id),
                STATUS_PREVIEWED,
            ),
        )
        conn.commit()
    return response


def _apply_history_events(
    conn,
    *,
    client_id: int,
    actor: NorthStarUser,
    events: list[HistoryEventRow],
    closed_record_nos: set[str],
    batch_id: int,
    fail_after: str | None = None,
    history_only: bool = False,
) -> tuple[int, int, int]:
    """Insert company-scoped shared history.

    Returns (inserted, already_present_in_db, duplicate_within_batch).

    Uses the same company-scoped identity and first-occurrence order as
    ``_history_plan_counts`` (CSV/event list order; contact_no ignored).
    Never overwrites existing notes or rewrites company.external_record_no.
    Does not create companies.
    """
    now = _now()
    inserted = already = within_batch = 0
    db_cache: dict[int, object] = {}
    batch_seen: dict[int, MutableHistoryDedupeIndex] = {}
    for event in events:
        if event.record_no and event.record_no in closed_record_nos:
            continue
        company_id: int | None = None
        if history_only:
            match = resolve_history_company(
                conn,
                client_id=int(client_id),
                record_no=event.record_no,
                company_name=event.company_name,
            )
            if match.status != "matched" or match.company_id is None:
                continue
            company_id = int(match.company_id)
        else:
            company = _company_by_record_no(conn, event.record_no)
            if company is None:
                continue
            company_id = int(company["id"])
        db_index = db_cache.get(company_id)
        if db_index is None:
            db_index = load_existing_history_dedupe_index(
                conn, client_id=int(client_id), company_id=int(company_id)
            )
            db_cache[company_id] = db_index
        present_kwargs = dict(
            client_id=int(client_id),
            company_id=int(company_id),
            contact_key="",
            note_text=event.note_text,
            event_at=event.event_at,
            event_hash=event.event_hash,
        )
        if history_event_already_present(db_index, **present_kwargs):  # type: ignore[arg-type]
            already += 1
            continue
        seen = batch_seen.get(company_id)
        if seen is None:
            seen = MutableHistoryDedupeIndex.empty()
            batch_seen[company_id] = seen
        if history_event_already_present(seen.as_existing(), **present_kwargs):
            within_batch += 1
            continue
        stored_rn = event.record_no or ""
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
                    stored_rn,
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
                    int(batch_id),
                    now,
                    int(actor.id),
                ),
            )
            inserted += 1
            seen.register_shared_event(
                client_id=int(client_id),
                company_id=int(company_id),
                note_text=event.note_text,
                event_at=event.event_at,
                event_hash=event.event_hash,
            )
            if fail_after == "after_history_insert" and inserted == 1:
                raise RuntimeError("Injected failure after history insert.")
        except sqlite3.IntegrityError:
            already += 1
    return inserted, already, within_batch



def _unlink_history(path: Path) -> None:
    if path.is_file():
        path.unlink()
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        try:
            parent.rmdir()
        except OSError:
            pass


def cleanup_client_data_history_staging(
    conn,
    *,
    client_id: int,
    batch_id: int,
    unlink_fn: Any | None = None,
) -> str:
    unlink = unlink_fn or _unlink_history
    row = _load_cd_row(conn, client_id, batch_id)
    if _blank(row["status"]) != STATUS_CONFIRMED:
        raise ValueError("Staging cleanup is only allowed for confirmed batches.")
    raw = _blank(row["history_staging_path"])
    now = _now()
    if not raw:
        conn.execute(
            """
            UPDATE client_data_import_batches
            SET staging_cleanup_status = ?,
                staging_cleanup_error = '',
                staging_cleanup_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (CLEANUP_COMPLETED, now, int(batch_id), int(client_id)),
        )
        conn.commit()
        return CLEANUP_COMPLETED

    path = Path(raw)
    try:
        if path.is_file():
            unlink(path)
        elif path.exists():
            unlink(path)
    except Exception as exc:  # noqa: BLE001 — cleanup must not raise into confirm
        conn.execute(
            """
            UPDATE client_data_import_batches
            SET staging_cleanup_status = ?,
                staging_cleanup_error = ?,
                staging_cleanup_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (CLEANUP_PENDING, str(exc)[:2000], now, int(batch_id), int(client_id)),
        )
        conn.commit()
        return CLEANUP_PENDING

    conn.execute(
        """
        UPDATE client_data_import_batches
        SET history_staging_path = '',
            staging_cleanup_status = ?,
            staging_cleanup_error = '',
            staging_cleanup_at = ?
        WHERE id = ? AND client_id = ?
        """,
        (CLEANUP_COMPLETED, now, int(batch_id), int(client_id)),
    )
    conn.commit()
    return CLEANUP_COMPLETED


def _result_from_cd_row(cd_row: Any) -> ClientDataImportConfirmResponse:
    raw = _blank(cd_row["result_json"])
    if not raw:
        raise ValueError("Confirmed batch is missing result_json.")
    result = ClientDataImportConfirmResponse.model_validate(json.loads(raw))
    result.staging_cleanup_status = _blank(cd_row["staging_cleanup_status"])
    result.staging_cleanup_error = _blank(cd_row["staging_cleanup_error"])
    return result


def confirm_client_data_import(
    *,
    client_id: int,
    batch_id: int,
    plan_fingerprint: str,
    actor: NorthStarUser,
    confirm: bool = True,
    unlink_fn: Any | None = None,
    fail_after: str | None = None,
) -> ClientDataImportConfirmResponse:
    """Atomically confirm Client Data Import on one connection / one commit.

    Sequence (single connection):
      1. BEGIN IMMEDIATE
      2. Recompute dry-run plan + combined fingerprint (inside lock)
      3. apply_crm_import_plan_on_connection (prospects/companies/contacts/
         relationships/statuses/notes/CRM batch audit)
      4. Apply shared-history events (same conn resolves in-txn companies)
      5. Update client_data_import_batches audit
      6. Exactly one conn.commit()
      7. Staging file cleanup AFTER commit (separate connection; never rolls back CRM)
    """
    _require_admin(actor, client_id)
    if confirm is not True:
        raise ValueError("confirm must be true.")
    if not _FP_RE.match(plan_fingerprint or ""):
        raise ValueError("Malformed plan_fingerprint.")

    # No request-time DDL — schema must already exist from controlled migration.

    with get_connection() as conn:
        try:
            cd_row = _load_cd_row(conn, client_id, batch_id)
            status = _blank(cd_row["status"])
            if status == STATUS_CONFIRMED:
                # Idempotent: optional cleanup retry only.
                cleanup_status = _blank(cd_row["staging_cleanup_status"])
                if cleanup_status == CLEANUP_PENDING or (
                    cleanup_status != CLEANUP_COMPLETED
                    and _blank(cd_row["history_staging_path"])
                ):
                    cleanup_client_data_history_staging(
                        conn,
                        client_id=client_id,
                        batch_id=batch_id,
                        unlink_fn=unlink_fn,
                    )
                    cd_row = _load_cd_row(conn, client_id, batch_id)
                return _result_from_cd_row(cd_row)
            if status != STATUS_PREVIEWED:
                raise BatchNotReusable(BATCH_NOT_REUSABLE)

            conn.execute("BEGIN IMMEDIATE")
            cd_row = _load_cd_row(conn, client_id, batch_id)
            if _blank(cd_row["status"]) == STATUS_CONFIRMED:
                # Lost race: peer confirmed first.
                conn.commit()
                return _result_from_cd_row(cd_row)
            if _blank(cd_row["status"]) != STATUS_PREVIEWED:
                raise BatchNotReusable(BATCH_NOT_REUSABLE)

            dry, plan, history_counts, history_mapping = _compute_dry_run(
                conn, client_id=client_id, cd_row=cd_row
            )
            if dry.plan_fingerprint != plan_fingerprint:
                raise BatchNotReusable("Plan fingerprint mismatch.")
            if not dry.confirm_allowed:
                raise BatchNotReusable("Import batch is not fully importable.")

            import_mode = _cd_import_mode(cd_row)
            history_only = import_mode == IMPORT_MODE_HISTORY_ONLY
            crm_batch_id = _cd_crm_batch_id(cd_row)
            closed_rns: set[str] = set()
            if plan is not None:
                _importable_rns, closed_rns = _planned_company_record_nos(plan)
            history_events: list[HistoryEventRow] = []
            staging = _blank(cd_row["history_staging_path"])
            if staging:
                path = Path(staging).resolve()
                root = _import_root().resolve()
                if root not in path.parents and path.parent != root:
                    raise BatchNotReusable("History staging path is outside the controlled directory.")
                content = path.read_bytes()
                parsed = parse_history_with_mapping(
                    content,
                    history_mapping,
                    client_id=int(client_id),
                    closed_record_nos=closed_rns,
                )
                if parsed.errors:
                    raise BatchNotReusable("History file has validation errors.")
                history_events = list(parsed.events)

            crm_result: dict[str, object] = {
                "imported_at": _now(),
                "imported_by_user_id": int(actor.id),
                "created_company_count": 0,
                "reused_company_count": 0,
                "created_contact_count": 0,
                "reused_contact_count": 0,
                "created_relationship_count": 0,
                "existing_relationship_count": 0,
                "imported_status_count": 0,
                "notes_set_count": 0,
                "notes_appended_count": 0,
                "notes_duplicate_count": 0,
            }
            if not history_only:
                if crm_batch_id is None or plan is None:
                    raise BatchNotReusable("Prospects batch is missing.")
                crm_result = apply_crm_import_plan_on_connection(
                    conn,
                    client_id=client_id,
                    batch_id=int(crm_batch_id),
                    plan=plan,
                    actor=actor,
                    fail_after=fail_after,
                    source_system="client_data_import",
                )
                if fail_after == "after_crm_apply":
                    raise RuntimeError("Injected failure after CRM apply.")

            history_inserted = 0
            history_already = 0
            history_within = 0
            if history_events:
                history_inserted, history_already, history_within = _apply_history_events(
                    conn,
                    client_id=client_id,
                    actor=actor,
                    events=history_events,
                    closed_record_nos=closed_rns,
                    batch_id=int(batch_id),
                    fail_after=fail_after,
                    history_only=history_only,
                )
            # Preview/confirm equality for history counters.
            if int(history_counts.insert or 0) != history_inserted:
                raise BatchNotReusable(
                    "History insert counter drifted between preview and confirm."
                )
            if int(history_counts.already_present or 0) != history_already:
                raise BatchNotReusable(
                    "History already-present counter drifted between preview and confirm."
                )
            if int(history_counts.duplicate_within_batch or 0) != history_within:
                raise BatchNotReusable(
                    "History within-batch duplicate counter drifted between preview and confirm."
                )

            now = _now()
            if fail_after == "after_history_before_audit":
                raise RuntimeError("Injected failure after history before audit.")
            result = ClientDataImportConfirmResponse(
                id=int(batch_id),
                batch_id=int(batch_id),
                client_id=int(client_id),
                status=STATUS_CONFIRMED,
                imported_at=crm_result.get("imported_at") or now,
                imported_by_user_id=crm_result.get("imported_by_user_id"),
                confirmed_plan_fingerprint=plan_fingerprint,
                companies_created=int(crm_result.get("created_company_count") or 0),
                companies_reused=int(crm_result.get("reused_company_count") or 0),
                contacts_created=int(crm_result.get("created_contact_count") or 0),
                contacts_reused=int(crm_result.get("reused_contact_count") or 0),
                relationships_created=int(
                    crm_result.get("created_relationship_count") or 0
                ),
                relationships_existing=int(
                    crm_result.get("existing_relationship_count") or 0
                ),
                statuses_imported=int(crm_result.get("imported_status_count") or 0),
                notes_set=int(crm_result.get("notes_set_count") or 0),
                notes_appended=int(crm_result.get("notes_appended_count") or 0),
                notes_duplicate=int(crm_result.get("notes_duplicate_count") or 0),
                history_inserted=history_inserted,
                history_already_present=history_already,
                history_duplicate_within_batch=history_within,
                closed_excluded_count=int(dry.closed_excluded_count or 0),
                created_company_count=int(crm_result.get("created_company_count") or 0),
                reused_company_count=int(crm_result.get("reused_company_count") or 0),
                created_contact_count=int(crm_result.get("created_contact_count") or 0),
                reused_contact_count=int(crm_result.get("reused_contact_count") or 0),
                created_relationship_count=int(
                    crm_result.get("created_relationship_count") or 0
                ),
                existing_relationship_count=int(
                    crm_result.get("existing_relationship_count") or 0
                ),
                staging_cleanup_status="",
                staging_cleanup_error="",
                audit={
                    "crm": crm_result,
                    "history": {
                        "insert": history_inserted,
                        "already_present": history_already,
                        "duplicate_within_batch": history_within,
                        "dry_run": history_counts.model_dump(),
                    },
                    "prospects_preview": dry.prospects.model_dump() if dry.prospects else {},
                },
            )
            # Preview/confirm equality for prospect counters (full mode only).
            preview = dry.prospects
            if (not history_only) and preview is not None:
                if int(preview.companies_create or 0) != int(result.companies_created or 0):
                    raise BatchNotReusable("Company create counter drifted.")
                if int(preview.companies_reuse or 0) != int(result.companies_reused or 0):
                    raise BatchNotReusable("Company reuse counter drifted.")
                if int(preview.contacts_create or 0) != int(result.contacts_created or 0):
                    raise BatchNotReusable("Contact create counter drifted.")
                if int(preview.contacts_reuse or 0) != int(result.contacts_reused or 0):
                    raise BatchNotReusable("Contact reuse counter drifted.")
                if int(preview.relationships_create or 0) != int(
                    result.relationships_created or 0
                ):
                    raise BatchNotReusable("Relationship create counter drifted.")
                if int(preview.relationships_existing or 0) != int(
                    result.relationships_existing or 0
                ):
                    raise BatchNotReusable("Relationship existing counter drifted.")
                if int(preview.excluded_closed or 0) != int(
                    result.closed_excluded_count or 0
                ):
                    raise BatchNotReusable("Closed exclusion counter drifted.")

            conn.execute(
                """
                UPDATE client_data_import_batches
                SET status = ?,
                    result_json = ?,
                    plan_fingerprint = ?,
                    closed_excluded_count = ?,
                    confirmed_at = ?,
                    confirmed_by_user_id = ?,
                    updated_at = ?
                WHERE id = ? AND client_id = ? AND status = ?
                """,
                (
                    STATUS_CONFIRMED,
                    json.dumps(result.model_dump()),
                    plan_fingerprint,
                    int(result.closed_excluded_count or 0),
                    now,
                    int(actor.id),
                    now,
                    int(batch_id),
                    int(client_id),
                    STATUS_PREVIEWED,
                ),
            )
            if int(conn.execute("SELECT changes() AS n").fetchone()["n"]) != 1:
                raise BatchNotReusable(
                    "Client data batch terminal UPDATE did not update exactly one row."
                )
            if fail_after == "before_commit":
                raise RuntimeError("Injected failure before commit.")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    # Post-commit staging cleanup only — never part of the CRM transaction.
    with get_connection() as conn:
        cleanup_client_data_history_staging(
            conn, client_id=client_id, batch_id=batch_id, unlink_fn=unlink_fn
        )
        cd_row = _load_cd_row(conn, client_id, batch_id)
        return _result_from_cd_row(cd_row)


def retry_client_data_import_staging_cleanup(
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    unlink_fn: Any | None = None,
) -> ClientDataImportConfirmResponse:
    _require_admin(actor, client_id)
    with get_connection() as conn:
        cd_row = _load_cd_row(conn, client_id, batch_id)
        if _blank(cd_row["status"]) != STATUS_CONFIRMED:
            raise ValueError("Only confirmed batches support staging cleanup retry.")
        cleanup_client_data_history_staging(
            conn, client_id=client_id, batch_id=batch_id, unlink_fn=unlink_fn
        )
        cd_row = _load_cd_row(conn, client_id, batch_id)
        return _result_from_cd_row(cd_row)


def cancel_client_data_import(
    client_id: int, batch_id: int, *, actor: NorthStarUser
) -> ClientDataImportBatchView:
    _require_admin(actor, client_id)
    with get_connection() as conn:
        cd_row = _load_cd_row(conn, client_id, batch_id)
        status = _blank(cd_row["status"])
        import_mode = _cd_import_mode(cd_row)
        crm_batch_id = _cd_crm_batch_id(cd_row)
        if status == STATUS_CANCELLED:
            if import_mode == IMPORT_MODE_HISTORY_ONLY or crm_batch_id is None:
                crm_batch = _empty_crm_batch_stub(
                    client_id=client_id,
                    history_filename=_blank(cd_row["history_original_filename"]),
                )
            else:
                crm_batch = get_crm_import_batch(
                    client_id, int(crm_batch_id), include_sample=False
                )
            return _batch_view_from_parts(cd_row=cd_row, crm_batch=crm_batch)
        if status not in {STATUS_PREVIEWED, STATUS_FAILED}:
            raise BatchNotReusable(BATCH_NOT_REUSABLE)
        staging = _blank(cd_row["history_staging_path"])
        now = _now()
        conn.execute(
            """
            UPDATE client_data_import_batches
            SET status = ?,
                history_staging_path = '',
                updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (STATUS_CANCELLED, now, int(batch_id), int(client_id)),
        )
        conn.commit()

    if staging:
        path = Path(staging)
        try:
            _unlink_history(path)
        except Exception:
            log.exception("Failed to delete history staging on cancel path=%s", staging)

    if crm_batch_id is not None and import_mode != IMPORT_MODE_HISTORY_ONLY:
        cancel_crm_import(client_id, int(crm_batch_id), actor=actor)
    return get_client_data_import_batch(client_id, batch_id, include_sample=False)
