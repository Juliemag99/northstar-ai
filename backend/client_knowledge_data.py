"""Client Knowledge Hub — documents, extraction staging, appointment import foundation.

Phase 1: storage + library + staging. No automatic AI overwrite of Client Setup.
All records are strictly scoped by client_id.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from access import get_default_user, get_user_by_id, user_can_access_client
from client_setup_data import (
    user_can_edit_client_setup,
    update_campaign,
    update_client_overview,
    update_client_sells,
    get_client_setup,
)
from client_document_extraction import (
    classify_finding,
    extract_findings,
    load_current_values,
    parse_file,
)
from db import DATABASE_DIR, get_connection
from models import (
    ClientAppointmentImportBatchView,
    ClientAppointmentImportConfirmRequest,
    ClientAppointmentImportMapRequest,
    ClientAppointmentImportPreview,
    ClientAppointmentImportRowView,
    ClientCampaignUpdate,
    ClientDocumentProcessResult,
    ClientDocumentView,
    ClientEmailTemplateUpdate,
    ClientEmailTemplateView,
    ClientExtractionBulkReviewRequest,
    ClientExtractionProposalUpdate,
    ClientExtractionProposalView,
    ClientExtractionResolveRequest,
    ClientKnowledgeSectionUpdate,
    ClientKnowledgeSectionView,
    ClientKnowledgeHubResponse,
    ClientOverviewUpdate,
    ClientSellsUpdate,
)

DOCUMENTS_ROOT = DATABASE_DIR / "client_documents"

DOCUMENT_TYPES = (
    "Client Information",
    "Strategy Session 1",
    "Strategy Session 2",
    "Road Map / Call Playbook",
    "Appointment Grid",
    "Email Template",
    "Appointment Set Template",
    "Capabilities",
    "Other",
)

ALLOWED_EXTENSIONS = {".docx", ".xlsx", ".csv"}

KNOWLEDGE_SECTIONS = (
    "client_profile",
    "strategy",
    "call_playbook",
    "capabilities",
    "client_operations",
    "client_contacts",
    "email_templates",
    "appointments",
    "unmapped",
)

KNOWLEDGE_SECTION_LABELS = {
    "client_profile": "Client Profile",
    "strategy": "Strategy",
    "call_playbook": "Road Map / Call Playbook",
    "capabilities": "Capabilities",
    "client_operations": "Client Operations",
    "client_contacts": "Client Contacts",
    "email_templates": "Email Templates",
    "appointments": "Appointments",
    "unmapped": "Unmapped Intelligence",
}

# When remapping into Client Operations, coerce profile/playbook field keys.
CLIENT_OPERATIONS_FIELD_ALIASES = {
    "primary_owner_name": "northstar_revenue_specialist",
    "who_takes_appointments": "who_takes_appointments",
    "appointment_instructions": "appointment_handling_instructions",
}

EXTRACTION_APPLY_SOURCE = "Document Extraction Approval"

# Heuristic credential / password detection — never import into Client Setup
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_\s-]?key|private[_\s-]?key|"
    r"credentials?|login\s*:\s*\S+\s+pass(word)?\s*[:=])\b"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def ensure_client_knowledge_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL DEFAULT '',
                document_type TEXT NOT NULL DEFAULT 'Other',
                mime_type TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                uploaded_at TEXT NOT NULL DEFAULT '',
                uploaded_by_user_id INTEGER,
                uploaded_by_name TEXT NOT NULL DEFAULT '',
                processing_status TEXT NOT NULL DEFAULT 'uploaded',
                sensitive_flag INTEGER NOT NULL DEFAULT 0,
                sensitive_note TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT NOT NULL DEFAULT '',
                replaced_by_document_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_client_documents_client
                ON client_documents(client_id, archived, uploaded_at DESC);

            CREATE TABLE IF NOT EXISTS client_extraction_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                document_id INTEGER,
                section TEXT NOT NULL DEFAULT '',
                field_name TEXT NOT NULL,
                existing_value TEXT NOT NULL DEFAULT '',
                proposed_value TEXT NOT NULL DEFAULT '',
                source_reference TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Pending',
                reviewed_by_user_id INTEGER,
                reviewed_by_name TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES client_documents(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_client_extraction_client
                ON client_extraction_proposals(client_id, status, id DESC);

            CREATE TABLE IF NOT EXISTS client_knowledge_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                section_key TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by_user_id INTEGER,
                updated_by_name TEXT NOT NULL DEFAULT '',
                UNIQUE(client_id, section_key),
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS client_email_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                template_name TEXT NOT NULL,
                template_type TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                source_document_id INTEGER,
                source_proposal_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                updated_by_name TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER,
                created_by_name TEXT NOT NULL DEFAULT '',
                approved_by_user_id INTEGER,
                approved_by_name TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_client_email_templates_client
                ON client_email_templates(client_id, is_active);

            CREATE TABLE IF NOT EXISTS client_appointment_import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                document_id INTEGER,
                filename TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'uploaded',
                column_mapping_json TEXT NOT NULL DEFAULT '{}',
                headers_json TEXT NOT NULL DEFAULT '[]',
                row_count INTEGER NOT NULL DEFAULT 0,
                uploaded_at TEXT NOT NULL DEFAULT '',
                uploaded_by_user_id INTEGER,
                uploaded_by_name TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT NOT NULL DEFAULT '',
                confirmed_by_name TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES client_documents(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS client_appointment_import_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                row_index INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                mapped_json TEXT NOT NULL DEFAULT '{}',
                company_match_status TEXT NOT NULL DEFAULT 'NEW',
                company_id INTEGER,
                company_name TEXT NOT NULL DEFAULT '',
                company_record_no TEXT NOT NULL DEFAULT '',
                contact_match_status TEXT NOT NULL DEFAULT 'NEW',
                contact_id INTEGER,
                contact_name TEXT NOT NULL DEFAULT '',
                import_status TEXT NOT NULL DEFAULT 'staged',
                event_id INTEGER,
                FOREIGN KEY (batch_id) REFERENCES client_appointment_import_batches(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_appt_import_rows_batch
                ON client_appointment_import_rows(batch_id, row_index);

            -- Client-scoped appointment event history from imports (does not replace CRM milestones)
            CREATE TABLE IF NOT EXISTS client_appointment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                batch_id INTEGER,
                import_row_id INTEGER,
                company_id INTEGER,
                contact_id INTEGER,
                external_record_no TEXT NOT NULL DEFAULT '',
                company_name TEXT NOT NULL DEFAULT '',
                contact_name TEXT NOT NULL DEFAULT '',
                contact_title TEXT NOT NULL DEFAULT '',
                appointment_date TEXT NOT NULL DEFAULT '',
                appointment_time TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                zip TEXT NOT NULL DEFAULT '',
                caller_notes TEXT NOT NULL DEFAULT '',
                appointment_grade TEXT NOT NULL DEFAULT '',
                sales_notes TEXT NOT NULL DEFAULT '',
                dollars_quoted TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                revenue_specialist TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT 'Appointment',
                parent_event_id INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (batch_id) REFERENCES client_appointment_import_batches(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_client_appt_events_client
                ON client_appointment_events(client_id, appointment_date DESC, id DESC);
            """
        )
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(client_extraction_proposals)"
            ).fetchall()
        }
        for name, decl in [
            ("document_type", "TEXT NOT NULL DEFAULT ''"),
            ("raw_source_text", "TEXT NOT NULL DEFAULT ''"),
            ("source_locator", "TEXT NOT NULL DEFAULT ''"),
            ("confidence", "TEXT NOT NULL DEFAULT 'MEDIUM'"),
            ("classification", "TEXT NOT NULL DEFAULT ''"),
            ("approved_value", "TEXT NOT NULL DEFAULT ''"),
            ("extracted_value", "TEXT NOT NULL DEFAULT ''"),
            ("apply_target", "TEXT NOT NULL DEFAULT ''"),
            ("applied_at", "TEXT NOT NULL DEFAULT ''"),
            ("resolved_at", "TEXT NOT NULL DEFAULT ''"),
            ("resolved_by_user_id", "INTEGER"),
            ("resolved_by_name", "TEXT NOT NULL DEFAULT ''"),
            ("resolution_reason", "TEXT NOT NULL DEFAULT ''"),
            ("resolved_by_proposal_ids", "TEXT NOT NULL DEFAULT ''"),
            ("resolved_client_contact_ids", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if name not in cols:
                conn.execute(
                    f"ALTER TABLE client_extraction_proposals ADD COLUMN {name} {decl}"
                )
        # Email template provenance columns (idempotent)
        tmpl_cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(client_email_templates)").fetchall()
        }
        for name, decl in [
            ("source_document_id", "INTEGER"),
            ("source_proposal_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("created_by_user_id", "INTEGER"),
            ("created_by_name", "TEXT NOT NULL DEFAULT ''"),
            ("approved_by_user_id", "INTEGER"),
            ("approved_by_name", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if name not in tmpl_cols:
                conn.execute(
                    f"ALTER TABLE client_email_templates ADD COLUMN {name} {decl}"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_extraction_sensitive_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                document_id INTEGER,
                note TEXT NOT NULL DEFAULT 'Sensitive credential detected and excluded.',
                created_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );
            """
        )
        DOCUMENTS_ROOT.mkdir(parents=True, exist_ok=True)
        if owns:
            conn.commit()
        from client_contacts_data import ensure_client_contacts_schema

        ensure_client_contacts_schema(conn)
        if owns:
            conn.commit()
        from client_email_accounts_data import ensure_client_email_accounts_schema

        ensure_client_email_accounts_schema(conn)
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _require_access(user_id: int, client_id: int) -> None:
    from access import require_write_client_id

    require_write_client_id(client_id, user_id=user_id)


def _require_edit(user_id: int, client_id: int) -> None:
    if not user_can_edit_client_setup(user_id, client_id):
        raise PermissionError("Not authorized to edit client knowledge.")


def _client_dir(client_id: int) -> Path:
    path = DOCUMENTS_ROOT / str(int(client_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _doc_row_to_view(row: Any) -> ClientDocumentView:
    d = dict(row)
    return ClientDocumentView(
        document_id=int(d["id"]),
        client_id=int(d["client_id"]),
        filename=_blank(d.get("filename")),
        document_type=_blank(d.get("document_type")) or "Other",
        uploaded_at=_blank(d.get("uploaded_at")),
        uploaded_by=_blank(d.get("uploaded_by_name")),
        processing_status=_blank(d.get("processing_status")) or "uploaded",
        sensitive_flag=bool(d.get("sensitive_flag")),
        sensitive_note=_blank(d.get("sensitive_note")),
        archived=bool(d.get("archived")),
        file_size=int(d.get("file_size") or 0),
        replaced_by_document_id=int(d["replaced_by_document_id"])
        if d.get("replaced_by_document_id")
        else None,
    )


def _scan_sensitive_bytes(data: bytes, filename: str) -> tuple[bool, str]:
    """Flag likely credential content. Does not store password values."""
    text = ""
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        text = ""
    # Also sample CSV-ish
    sample = text[:200_000]
    if _SENSITIVE_PATTERNS.search(sample):
        return True, "Sensitive — Not Imported (credential-like content detected)"
    # Column headers that look like password fields
    if re.search(r"(?i)(^|,|\t)\s*password\s*(,|\t|$)", sample):
        return True, "Sensitive — Not Imported (password column detected)"
    return False, ""


def list_documents(client_id: int, *, user_id: int | None = None, include_archived: bool = False) -> list[ClientDocumentView]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        if include_archived:
            rows = conn.execute(
                """
                SELECT * FROM client_documents
                WHERE client_id = ?
                ORDER BY archived ASC, uploaded_at DESC, id DESC
                """,
                (client_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM client_documents
                WHERE client_id = ? AND archived = 0
                ORDER BY uploaded_at DESC, id DESC
                """,
                (client_id,),
            ).fetchall()
    return [_doc_row_to_view(r) for r in rows]


def upload_document(
    client_id: int,
    *,
    filename: str,
    content: bytes,
    document_type: str = "Other",
    user_id: int | None = None,
    replace_document_id: int | None = None,
) -> ClientDocumentView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)

    name = Path(_blank(filename)).name
    if not name:
        raise ValueError("Filename is required.")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("Only .docx, .xlsx, and .csv files are allowed.")

    dtype = _blank(document_type) or "Other"
    if dtype not in DOCUMENT_TYPES:
        dtype = "Other"

    ensure_client_knowledge_schema()
    sensitive, sensitive_note = _scan_sensitive_bytes(content, name)
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = _client_dir(client_id) / stored
    dest.write_bytes(content)

    now = _now()
    with get_connection() as conn:
        # Guard: replace only within same client
        if replace_document_id:
            old = conn.execute(
                "SELECT id FROM client_documents WHERE id = ? AND client_id = ?",
                (replace_document_id, client_id),
            ).fetchone()
            if not old:
                raise LookupError("Document to replace not found for this client.")

        cur = conn.execute(
            """
            INSERT INTO client_documents (
                client_id, filename, stored_filename, document_type, mime_type,
                file_size, uploaded_at, uploaded_by_user_id, uploaded_by_name,
                processing_status, sensitive_flag, sensitive_note, archived,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                client_id,
                name,
                stored,
                dtype,
                ext,
                len(content),
                now,
                user.id,
                _blank(user.full_name) or _blank(user.email),
                "uploaded",
                1 if sensitive else 0,
                sensitive_note,
                now,
                now,
            ),
        )
        doc_id = int(cur.lastrowid)
        if replace_document_id:
            conn.execute(
                """
                UPDATE client_documents
                SET archived = 1, archived_at = ?, replaced_by_document_id = ?, updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (now, doc_id, now, replace_document_id, client_id),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM client_documents WHERE id = ? AND client_id = ?",
            (doc_id, client_id),
        ).fetchone()
    return _doc_row_to_view(row)


def get_document_file(
    client_id: int, document_id: int, *, user_id: int | None = None
) -> tuple[Path, str]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT filename, stored_filename FROM client_documents
            WHERE id = ? AND client_id = ?
            """,
            (document_id, client_id),
        ).fetchone()
    if not row:
        raise LookupError("Document not found for this client.")
    path = _client_dir(client_id) / _blank(row["stored_filename"])
    if not path.exists():
        raise LookupError("Stored file missing.")
    return path, _blank(row["filename"])


def archive_document(
    client_id: int, document_id: int, *, user_id: int | None = None
) -> ClientDocumentView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_knowledge_schema()
    now = _now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_documents WHERE id = ? AND client_id = ?",
            (document_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Document not found for this client.")
        conn.execute(
            """
            UPDATE client_documents
            SET archived = 1, archived_at = ?, updated_at = ?, processing_status = ?
            WHERE id = ? AND client_id = ?
            """,
            (now, now, "archived", document_id, client_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM client_documents WHERE id = ? AND client_id = ?",
            (document_id, client_id),
        ).fetchone()
    return _doc_row_to_view(row)


def _proposal_row_to_view(row: Any, filename: str = "") -> ClientExtractionProposalView:
    d = dict(row)
    section = _blank(d.get("section"))
    field_name = _blank(d.get("field_name"))
    from client_document_extraction import FIELD_CATALOG

    label = FIELD_CATALOG.get(section, {}).get(field_name, field_name.replace("_", " ").title())
    status = _blank(d.get("status")) or "Pending"
    status_labels = {
        "Pending": "Pending",
        "Approved": "Approved",
        "Rejected": "Rejected",
        "Resolved": "Resolved / Incorporated",
    }

    def _ids(raw: Any) -> list[int]:
        text = _blank(raw)
        if not text:
            return []
        out: list[int] = []
        for part in re.split(r"[,;\s]+", text):
            if part.isdigit():
                out.append(int(part))
        return out

    return ClientExtractionProposalView(
        proposal_id=int(d["id"]),
        client_id=int(d["client_id"]),
        document_id=int(d["document_id"]) if d.get("document_id") else None,
        document_filename=filename,
        document_type=_blank(d.get("document_type")),
        section=section,
        field_name=field_name,
        field_label=label,
        existing_value=_blank(d.get("existing_value")),
        proposed_value=_blank(d.get("proposed_value")),
        source_reference=_blank(d.get("source_reference")),
        raw_source_text=_blank(d.get("raw_source_text")) or _blank(d.get("source_reference")),
        source_locator=_blank(d.get("source_locator")),
        confidence=_blank(d.get("confidence")) or "MEDIUM",
        classification=_blank(d.get("classification")),
        status=status,
        status_label=status_labels.get(status, status),
        reviewed_by=_blank(d.get("reviewed_by_name")),
        reviewed_at=_blank(d.get("reviewed_at")),
        approved_value=_blank(d.get("approved_value")),
        extracted_value=_blank(d.get("extracted_value")) or _blank(d.get("proposed_value")),
        apply_target=_blank(d.get("apply_target")),
        applied_at=_blank(d.get("applied_at")),
        resolved_at=_blank(d.get("resolved_at")),
        resolved_by=_blank(d.get("resolved_by_name")),
        resolution_reason=_blank(d.get("resolution_reason")),
        resolved_by_proposal_ids=_ids(d.get("resolved_by_proposal_ids")),
        resolved_client_contact_ids=_ids(d.get("resolved_client_contact_ids")),
    )


def mark_document_process(
    client_id: int, document_id: int, *, user_id: int | None = None
) -> ClientDocumentView:
    """Backward-compatible wrapper — returns document view only."""
    return process_document(client_id, document_id, user_id=user_id).document


def process_document(
    client_id: int, document_id: int, *, user_id: int | None = None
) -> ClientDocumentProcessResult:
    """Parse document, create extraction proposals. Never auto-approves or writes Client Setup."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_knowledge_schema()
    now = _now()
    appointment_batch_id: int | None = None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_documents WHERE id = ? AND client_id = ? AND archived = 0",
            (document_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Document not found for this client.")
        stored = _blank(row["stored_filename"])
        filename = _blank(row["filename"])
        dtype = _blank(row["document_type"]) or "Other"
        prior_sensitive_flag = int(row["sensitive_flag"] or 0)
        prior_sensitive_note = _blank(row["sensitive_note"])
        path = _client_dir(client_id) / stored
        if not path.exists():
            raise LookupError("Stored file missing.")

        conn.execute(
            """
            UPDATE client_documents
            SET processing_status = 'processing', updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (now, document_id, client_id),
        )
        conn.commit()

    try:
        parsed = parse_file(path, filename)
        findings, sensitive_notes, type_hint = extract_findings(
            parsed, user_document_type=dtype
        )
        current = load_current_values(client_id)
        created = 0
        sensitive_excluded = 0

        with get_connection() as conn:
            # Replace prior pending proposals for this document (re-process)
            conn.execute(
                """
                DELETE FROM client_extraction_proposals
                WHERE client_id = ? AND document_id = ? AND status = 'Pending'
                """,
                (client_id, document_id),
            )
            for note in sensitive_notes:
                sensitive_excluded += 1
                conn.execute(
                    """
                    INSERT INTO client_extraction_sensitive_flags (
                        client_id, document_id, note, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (client_id, document_id, note, now),
                )

            for f in findings:
                existing = current.get((f.section, f.field_name), "")
                if f.field_name == "sensitive_exclusion":
                    sensitive_excluded += 1
                    classification = "SENSITIVE — NOT IMPORTED"
                else:
                    classification = classify_finding(
                        f.section,
                        f.field_name,
                        existing,
                        f.proposed_value,
                        f.confidence,
                    )
                source_ref = _blank(f.source_locator) or _blank(f.raw_source_text)[:300]
                is_sensitive = classification == "SENSITIVE — NOT IMPORTED"
                conn.execute(
                    """
                    INSERT INTO client_extraction_proposals (
                        client_id, document_id, document_type, section, field_name,
                        existing_value, proposed_value, extracted_value,
                        source_reference, raw_source_text, source_locator,
                        confidence, classification, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)
                    """,
                    (
                        client_id,
                        document_id,
                        dtype,
                        f.section,
                        f.field_name,
                        existing,
                        "Sensitive credential detected and excluded."
                        if is_sensitive
                        else f.proposed_value,
                        "" if is_sensitive else f.proposed_value,
                        source_ref,
                        "[redacted]" if is_sensitive else f.raw_source_text,
                        f.source_locator,
                        f.confidence,
                        classification,
                        now,
                        now,
                    ),
                )
                created += 1

            # Appointment grid: stage headers into import batch (no confirm)
            if dtype == "Appointment Grid" or type_hint == "Appointment Grid":
                headers: list[str] = []
                for prev in parsed.sheet_previews:
                    headers = [str(h) for h in (prev.get("headers") or [])]
                    if headers:
                        break
                if headers:
                    cur = conn.execute(
                        """
                        INSERT INTO client_appointment_import_batches (
                            client_id, document_id, filename, status,
                            column_mapping_json, headers_json, row_count,
                            uploaded_at, uploaded_by_user_id, uploaded_by_name, notes
                        ) VALUES (?, ?, ?, 'headers_ready', '{}', ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            client_id,
                            document_id,
                            filename,
                            json.dumps(headers),
                            now,
                            user.id,
                            _blank(user.full_name) or _blank(user.email),
                            "Phase 2: headers staged only — no row import",
                        ),
                    )
                    appointment_batch_id = int(cur.lastrowid)

            note = prior_sensitive_note
            if sensitive_excluded:
                note = "Sensitive — Not Imported"
            conn.execute(
                """
                UPDATE client_documents
                SET processing_status = ?, sensitive_flag = ?, sensitive_note = ?, updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    "processed",
                    1 if sensitive_excluded else prior_sensitive_flag,
                    note,
                    now,
                    document_id,
                    client_id,
                ),
            )
            conn.commit()
            doc_row = conn.execute(
                "SELECT * FROM client_documents WHERE id = ? AND client_id = ?",
                (document_id, client_id),
            ).fetchone()

        return ClientDocumentProcessResult(
            document=_doc_row_to_view(doc_row),
            proposals_created=created,
            sensitive_excluded=sensitive_excluded,
            type_hint=type_hint,
            appointment_batch_id=appointment_batch_id,
            message=f"Created {created} proposal(s). None auto-approved.",
        )
    except Exception as exc:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_documents
                SET processing_status = 'error', notes = ?, updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (str(exc)[:500], now, document_id, client_id),
            )
            conn.commit()
        raise


def list_extraction_proposals(
    client_id: int,
    *,
    user_id: int | None = None,
    status: str | None = None,
    document_id: int | None = None,
) -> list[ClientExtractionProposalView]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        sql = """
            SELECT p.*, d.filename AS document_filename
            FROM client_extraction_proposals p
            LEFT JOIN client_documents d
              ON d.id = p.document_id AND d.client_id = p.client_id
            WHERE p.client_id = ?
        """
        params: list[Any] = [client_id]
        status_filter = _blank(status)
        if status_filter and status_filter.lower() not in {"all", "*"}:
            sql += " AND p.status = ?"
            params.append(status_filter)
        if document_id is not None:
            sql += " AND p.document_id = ?"
            params.append(document_id)
        sql += " ORDER BY p.id DESC"
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        _proposal_row_to_view(r, filename=_blank(dict(r).get("document_filename")))
        for r in rows
    ]


def _merge_knowledge_field(
    client_id: int,
    section_key: str,
    field_name: str,
    value: str,
    *,
    user_id: int,
    user_name: str,
) -> None:
    ensure_client_knowledge_schema()
    now = _now()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM client_knowledge_sections
            WHERE client_id = ? AND section_key = ?
            """,
            (client_id, section_key),
        ).fetchone()
        payload: dict[str, Any] = {}
        if row:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload[field_name] = value
        conn.execute(
            """
            INSERT INTO client_knowledge_sections (
                client_id, section_key, payload_json, updated_at,
                updated_by_user_id, updated_by_name
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id, section_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_by_name = excluded.updated_by_name
            """,
            (
                client_id,
                section_key,
                json.dumps(payload, ensure_ascii=False),
                now,
                user_id,
                user_name,
            ),
        )
        conn.commit()


def _append_client_operations_item(
    client_id: int,
    *,
    title: str,
    content: str,
    field_key: str,
    proposal: dict[str, Any],
    user_id: int,
    user_name: str,
    document_filename: str = "",
) -> str:
    """Append one approved operational knowledge item (never overwrites siblings)."""
    ensure_client_knowledge_schema()
    now = _now()
    # Credential safety — never store password-like content
    if _SENSITIVE_PATTERNS.search(content) or _SENSITIVE_PATTERNS.search(title):
        return "skipped_sensitive"
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM client_knowledge_sections
            WHERE client_id = ? AND section_key = 'client_operations'
            """,
            (client_id,),
        ).fetchone()
        payload: dict[str, Any] = {}
        if row:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items = payload.get("items")
        if not isinstance(items, list):
            items = []
        proposal_id = int(proposal["id"]) if proposal.get("id") is not None else None
        # Replace prior approved item from same proposal_id if re-approved
        if proposal_id is not None:
            items = [
                it
                for it in items
                if not (
                    isinstance(it, dict)
                    and it.get("proposal_id") == proposal_id
                )
            ]
        # Also prevent duplicate content under the same field_key
        content_key = _blank(content).lower()
        field_key_norm = _blank(field_key) or "other_operational_notes"
        items = [
            it
            for it in items
            if not (
                isinstance(it, dict)
                and _blank(it.get("field_key")) == field_key_norm
                and _blank(it.get("content")).lower() == content_key
            )
        ]
        item = {
            "item_id": f"ops-{proposal_id or now}",
            "title": _blank(title)
            or FIELD_CATALOG_LABEL(field_key)
            or field_key.replace("_", " ").title(),
            "content": _blank(content),
            "field_key": field_key_norm,
            "source_document": document_filename
            or _blank(proposal.get("document_filename")),
            "source_document_id": (
                int(proposal["document_id"]) if proposal.get("document_id") else None
            ),
            "source_snippet": _blank(proposal.get("raw_source_text"))
            or _blank(proposal.get("source_reference")),
            "source_locator": _blank(proposal.get("source_locator")),
            "status": "Approved",
            "approved_at": now,
            "approved_by": user_name,
            "proposal_id": proposal_id,
        }
        items.append(item)
        payload["items"] = items
        conn.execute(
            """
            INSERT INTO client_knowledge_sections (
                client_id, section_key, payload_json, updated_at,
                updated_by_user_id, updated_by_name
            ) VALUES (?, 'client_operations', ?, ?, ?, ?)
            ON CONFLICT(client_id, section_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_by_name = excluded.updated_by_name
            """,
            (
                client_id,
                json.dumps(payload, ensure_ascii=False),
                now,
                user_id,
                user_name,
            ),
        )
        conn.commit()
    return f"knowledge.client_operations.items[{len(items) - 1}]"


def FIELD_CATALOG_LABEL(field_key: str) -> str:
    from client_document_extraction import FIELD_CATALOG

    return FIELD_CATALOG.get("client_operations", {}).get(field_key, "")

def _apply_approved_proposal(
    client_id: int,
    proposal: dict[str, Any],
    approved_value: str,
    *,
    user_id: int,
    user_name: str,
) -> str:
    """Write one approved field to the mapped target. Returns apply_target label."""
    section = _blank(proposal.get("section"))
    field = _blank(proposal.get("field_name"))
    value = _blank(approved_value)
    if not value:
        return "skipped_empty"
    if _blank(proposal.get("classification")) == "SENSITIVE — NOT IMPORTED":
        return "skipped_sensitive"
    if field == "sensitive_exclusion":
        return "skipped_sensitive"

    # Client Operations must win over Client Setup profile writes when remapped here.
    if section == "client_operations":
        from client_document_extraction import FIELD_CATALOG

        ops_fields = FIELD_CATALOG.get("client_operations") or {}
        if field in CLIENT_OPERATIONS_FIELD_ALIASES:
            field = CLIENT_OPERATIONS_FIELD_ALIASES[field]
        if field not in ops_fields:
            field = "other_operational_notes"
        title = _blank(proposal.get("_item_title")) or ops_fields.get(
            field, field.replace("_", " ").title()
        )
        doc_name = ""
        doc_id = proposal.get("document_id")
        if doc_id:
            with get_connection() as conn:
                drow = conn.execute(
                    "SELECT filename FROM client_documents WHERE id = ? AND client_id = ?",
                    (int(doc_id), client_id),
                ).fetchone()
                if drow:
                    doc_name = _blank(drow["filename"])
        return _append_client_operations_item(
            client_id,
            title=title,
            content=value,
            field_key=field or "other_operational_notes",
            proposal=proposal,
            user_id=user_id,
            user_name=user_name,
            document_filename=doc_name,
        )

    # Client Contacts (people at NorthStar's client — not CRM prospect contacts)
    if section == "client_contacts" or (
        section == "client_profile" and field in {"client_contacts", "client_contact_person"}
    ):
        from client_contacts_data import (
            apply_approved_client_contact_proposal,
            apply_contact_enrichment_proposal,
            parse_client_contact_people,
        )

        if field == "client_contact_enrichment":
            return apply_contact_enrichment_proposal(
                client_id,
                proposal,
                value,
                user_id=user_id,
                user_name=user_name,
            )

        people = parse_client_contact_people(value)
        # JSON single-person payload counts as one
        if not people:
            try:
                import json as _json

                parsed = _json.loads(value)
                if isinstance(parsed, dict) and (parsed.get("name") or parsed.get("email")):
                    people = [parsed]
            except Exception:
                people = []
        if len(people) != 1 and field != "client_contact_person":
            raise ValueError(
                "Multi-person Client Contacts proposals cannot be approved as one record. "
                "Split into individual contacts in Strategy Import Review first."
            )
        if field == "client_contacts" and len(people) > 1:
            raise ValueError(
                "Multi-person Client Contacts proposals cannot be approved as one record. "
                "Split into individual contacts in Strategy Import Review first."
            )
        return apply_approved_client_contact_proposal(
            client_id,
            proposal,
            value,
            user_id=user_id,
            user_name=user_name,
        )

    setup = get_client_setup(client_id, user_id=user_id)
    default_campaign_id = setup.default_campaign_id

    # Profile / overview
    if section == "client_profile" and field in {
        "client_name",
        "website",
        "address",
        "main_phone",
        "primary_owner_name",
    }:
        body = ClientOverviewUpdate(
            client_name=setup.client_name if field != "client_name" else value,
            website=setup.website if field != "website" else value,
            main_location=setup.main_location if field != "address" else value,
            main_phone=setup.main_phone if field != "main_phone" else value,
            description=setup.description,
            primary_owner_name=(
                setup.primary_owner_name if field != "primary_owner_name" else value
            ),
            is_active=setup.is_active,
        )
        # Temporarily patch change source via knowledge apply using overview update
        # then record knowledge field too for who_takes etc.
        update_client_overview(client_id, body, user_id=user_id)
        _merge_knowledge_field(
            client_id, "client_profile", field, value, user_id=user_id, user_name=user_name
        )
        return f"client_setup.overview.{field}"

    if section == "client_profile" and field in {
        "who_takes_appointments",
        "decision_makers",
    }:
        _merge_knowledge_field(
            client_id, "client_profile", field, value, user_id=user_id, user_name=user_name
        )
        return f"knowledge.client_profile.{field}"

    # Strategy → campaign + knowledge
    # NOTE: prospecting_guidance and sales_challenges_barriers are knowledge-only.
    # They must never map into campaign fit signals / Campaign Fit computation.
    campaign_field_map = {
        "primary_service": "primary_service",
        "secondary_services": "secondary_services",
        "target_industries": "target_industries",
        "ideal_customer_profile": "target_customer_types",
        "product_part_characteristics": "target_products",
        "manufacturing_processes_sought": "manufacturing_processes_sought",
        "production_preference": "production_preference",
        "geographic_preferences": "geographic_preferences",
        "positive_fit_signals": "positive_signals",
        "negative_fit_signals": "negative_signals",
        "exclusions": "exclusions",
        "target_titles": "target_titles",
    }
    if section == "strategy" and field in campaign_field_map and default_campaign_id:
        camp = next((c for c in setup.campaigns if c.campaign_id == default_campaign_id), None)
        if camp:
            payload = camp.model_dump()
            # ClientCampaignUpdate expects different names
            body = ClientCampaignUpdate(
                campaign_name=camp.campaign_name,
                description=camp.description,
                active=camp.active,
                is_default=camp.is_default,
                primary_service=camp.primary_service,
                secondary_services=camp.secondary_services,
                target_industries=camp.target_industries,
                target_customer_types=camp.target_customer_types,
                target_products=camp.target_products,
                manufacturing_processes_sought=camp.manufacturing_processes_sought,
                production_preference=camp.production_preference,
                stamping_capability=camp.stamping_capability,
                tooling_notes=camp.tooling_notes,
                geographic_preferences=camp.geographic_preferences,
                geography_mode=camp.geography_mode,
                geography_required=camp.geography_required,
                company_size_preferences=camp.company_size_preferences,
                positive_signals=camp.positive_signals,
                negative_signals=camp.negative_signals,
                exclusions=camp.exclusions,
                target_titles=camp.target_titles,
                fit_weighting_notes=camp.fit_weighting_notes,
                notes=camp.notes,
            )
            setattr(body, campaign_field_map[field], value)
            update_campaign(client_id, default_campaign_id, body, user_id=user_id)
            # Also mirror sells primary/secondary/cert when relevant
            if field in {"primary_service", "secondary_services"}:
                sells = ClientSellsUpdate(
                    primary_service=setup.primary_service
                    if field != "primary_service"
                    else value,
                    secondary_services=setup.secondary_services
                    if field != "secondary_services"
                    else value,
                    products_services=setup.products_services,
                    differentiators=setup.differentiators,
                    certifications=setup.certifications,
                    equipment_capacity=setup.equipment_capacity,
                    value_proposition=setup.value_proposition,
                )
                update_client_sells(client_id, sells, user_id=user_id)
        _merge_knowledge_field(
            client_id, "strategy", field, value, user_id=user_id, user_name=user_name
        )
        return f"campaign.{campaign_field_map[field]}"

    if section == "strategy":
        _merge_knowledge_field(
            client_id, "strategy", field, value, user_id=user_id, user_name=user_name
        )
        return f"knowledge.strategy.{field}"

    if section == "call_playbook":
        _merge_knowledge_field(
            client_id, "call_playbook", field, value, user_id=user_id, user_name=user_name
        )
        if field == "value_propositions":
            sells = ClientSellsUpdate(
                primary_service=setup.primary_service,
                secondary_services=setup.secondary_services,
                products_services=setup.products_services,
                differentiators=setup.differentiators,
                certifications=setup.certifications,
                equipment_capacity=setup.equipment_capacity,
                value_proposition=value,
            )
            update_client_sells(client_id, sells, user_id=user_id)
            return "client_setup.sells.value_proposition"
        return f"knowledge.call_playbook.{field}"

    if section == "capabilities":
        _merge_knowledge_field(
            client_id, "capabilities", field, value, user_id=user_id, user_name=user_name
        )
        if field == "certifications":
            sells = ClientSellsUpdate(
                primary_service=setup.primary_service,
                secondary_services=setup.secondary_services,
                products_services=setup.products_services,
                differentiators=setup.differentiators,
                certifications=value,
                equipment_capacity=setup.equipment_capacity,
                value_proposition=setup.value_proposition,
            )
            update_client_sells(client_id, sells, user_id=user_id)
            return "client_setup.sells.certifications"
        if field in {"capacity", "equipment", "facility"}:
            sells = ClientSellsUpdate(
                primary_service=setup.primary_service,
                secondary_services=setup.secondary_services,
                products_services=setup.products_services,
                differentiators=setup.differentiators,
                certifications=setup.certifications,
                equipment_capacity=value
                if field == "capacity"
                else (setup.equipment_capacity or value),
                value_proposition=setup.value_proposition,
            )
            update_client_sells(client_id, sells, user_id=user_id)
            return "client_setup.sells.equipment_capacity"
        return f"knowledge.capabilities.{field}"

    if section == "email_templates":
        # Upsert a template from related pending fields is complex; store on knowledge + template row
        if field == "body":
            name = "Imported Email Template"
            ttype = ""
            # Look for sibling approved/pending name/type on same document
            with get_connection() as conn:
                sibs = conn.execute(
                    """
                    SELECT field_name, proposed_value, approved_value, status
                    FROM client_extraction_proposals
                    WHERE client_id = ? AND document_id = ?
                      AND section = 'email_templates'
                    """,
                    (client_id, proposal.get("document_id")),
                ).fetchall()
            for s in sibs:
                fn = _blank(s["field_name"])
                val = _blank(s["approved_value"]) or _blank(s["proposed_value"])
                if fn == "template_name" and val:
                    name = val
                if fn == "template_type" and val:
                    ttype = val
            upsert_email_template(
                client_id,
                ClientEmailTemplateUpdate(
                    template_name=name,
                    template_type=ttype or "Email Template",
                    subject="",
                    body=value,
                    is_active=True,
                ),
                user_id=user_id,
            )
            return "email_templates.body"
        _merge_knowledge_field(
            client_id, "email_templates", field, value, user_id=user_id, user_name=user_name
        )
        return f"knowledge.email_templates.{field}"

    if section == "unmapped":
        _merge_knowledge_field(
            client_id, "unmapped", field, value, user_id=user_id, user_name=user_name
        )
        return f"knowledge.unmapped.{field}"

    _merge_knowledge_field(
        client_id, section or "unmapped", field, value, user_id=user_id, user_name=user_name
    )
    return f"knowledge.{section}.{field}"


def review_extraction_proposal(
    client_id: int,
    proposal_id: int,
    body: ClientExtractionProposalUpdate,
    *,
    user_id: int | None = None,
) -> ClientExtractionProposalView:
    """Approve/Reject. On Approve, apply only this field (optional edited_value / remap)."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    status = _blank(body.status)
    if status not in {"Approved", "Rejected", "Pending"}:
        raise ValueError("Status must be Pending, Approved, or Rejected.")
    ensure_client_knowledge_schema()
    now = _now()
    user_name = _blank(user.full_name) or _blank(user.email)

    remap_section = _blank(body.section) if body.section is not None else ""
    remap_field = _blank(body.field_name) if body.field_name is not None else ""
    item_title = _blank(body.item_title) if body.item_title is not None else ""

    if remap_section and remap_section not in KNOWLEDGE_SECTIONS:
        raise ValueError("Unknown knowledge section for remapping.")

    # Coerce profile/playbook keys when destination is Client Operations
    if remap_section == "client_operations" and remap_field:
        remap_field = CLIENT_OPERATIONS_FIELD_ALIASES.get(remap_field, remap_field)

    if remap_section and remap_field:
        from client_document_extraction import FIELD_CATALOG

        catalog = FIELD_CATALOG.get(remap_section) or {}
        if catalog and remap_field not in catalog:
            if remap_section == "client_operations":
                # Never fail Save+Approve to Client Operations on unknown field keys
                remap_field = "other_operational_notes"
            else:
                raise ValueError(
                    f"Unknown field '{remap_field}' for section '{remap_section}'."
                )

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM client_extraction_proposals
            WHERE id = ? AND client_id = ?
            """,
            (proposal_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Proposal not found for this client.")
        proposal = dict(row)
        classification = _blank(proposal.get("classification"))
        confidence = _blank(proposal.get("confidence")) or "MEDIUM"
        approved_value = (
            _blank(body.edited_value)
            if body.edited_value is not None
            else _blank(proposal.get("proposed_value"))
        )

        if status == "Approved":
            if classification == "SENSITIVE — NOT IMPORTED":
                raise ValueError("Sensitive findings cannot be approved into Client Setup.")
            if confidence == "LOW" and classification == "CONFLICT":
                raise ValueError("LOW-confidence conflicts cannot be bulk-style approved blindly.")
            # Block multi-person Client Contacts lump-approval before status write
            check_section = remap_section or _blank(proposal.get("section"))
            check_field = remap_field or _blank(proposal.get("field_name"))
            if check_section in {"client_contacts", "client_profile"} and check_field in {
                "client_contacts",
                "client_contact_person",
            }:
                from client_contacts_data import parse_client_contact_people

                people = parse_client_contact_people(approved_value)
                is_json_one = False
                try:
                    import json as _json

                    parsed = _json.loads(approved_value)
                    if isinstance(parsed, dict) and (
                        parsed.get("name") or parsed.get("email")
                    ):
                        is_json_one = True
                        people = [parsed]
                except Exception:
                    pass
                if check_field == "client_contacts" and len(people) != 1 and not is_json_one:
                    raise ValueError(
                        "This Client Contacts proposal contains multiple people. "
                        "Use Split into contacts, then approve each person individually."
                    )
                if (
                    check_field == "client_contact_person"
                    and len(people) > 1
                    and not is_json_one
                ):
                    raise ValueError(
                        "This Client Contacts proposal contains multiple people. "
                        "Use Split into contacts, then approve each person individually."
                    )

        if remap_section or remap_field:
            new_section = remap_section or _blank(proposal.get("section"))
            new_field = remap_field or _blank(proposal.get("field_name"))
            conn.execute(
                """
                UPDATE client_extraction_proposals
                SET section = ?, field_name = ?, updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (new_section, new_field, now, proposal_id, client_id),
            )
            proposal["section"] = new_section
            proposal["field_name"] = new_field

        if item_title:
            proposal["_item_title"] = item_title

        conn.execute(
            """
            UPDATE client_extraction_proposals
            SET status = ?, reviewed_by_user_id = ?, reviewed_by_name = ?,
                reviewed_at = ?, updated_at = ?,
                approved_value = CASE WHEN ? = 'Approved' THEN ? ELSE approved_value END
            WHERE id = ? AND client_id = ?
            """,
            (
                status,
                user.id,
                user_name,
                now,
                now,
                status,
                approved_value,
                proposal_id,
                client_id,
            ),
        )
        conn.commit()

    if status == "Approved":
        apply_target = _apply_approved_proposal(
            client_id,
            proposal,
            approved_value,
            user_id=user.id,
            user_name=user_name,
        )
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_extraction_proposals
                SET apply_target = ?, applied_at = ?, updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (apply_target, now, now, proposal_id, client_id),
            )
            # Provenance audit row (client-scoped)
            try:
                conn.execute(
                    """
                    INSERT INTO client_setup_audit (
                        client_id, campaign_id, entity_type, field_name,
                        old_value, new_value, changed_by_user_id, changed_by_name,
                        changed_at, change_source
                    ) VALUES (?, NULL, 'extraction_proposal', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        client_id,
                        _blank(proposal.get("field_name")),
                        _blank(proposal.get("existing_value")),
                        approved_value,
                        user.id,
                        user_name,
                        now,
                        f"{EXTRACTION_APPLY_SOURCE}|doc:{proposal.get('document_id')}|{apply_target}",
                    ),
                )
            except Exception:
                pass
            conn.commit()

    return next(
        p
        for p in list_extraction_proposals(client_id, user_id=user.id, status="All")
        if p.proposal_id == proposal_id
    )


def resolve_extraction_proposal(
    client_id: int,
    proposal_id: int,
    body: ClientExtractionResolveRequest,
    *,
    user_id: int | None = None,
) -> ClientExtractionProposalView:
    """
    Mark a Pending proposal Resolved / Incorporated.
    Does not write Client Setup or Client Contacts — human confirmation only.
    """
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    reason = _blank(body.resolution_reason)
    if not reason:
        raise ValueError("A short resolution reason is required.")
    ensure_client_knowledge_schema()
    now = _now()
    user_name = _blank(user.full_name) or _blank(user.email)
    prop_ids = ",".join(str(int(x)) for x in (body.resolved_by_proposal_ids or []))
    contact_ids = ",".join(str(int(x)) for x in (body.resolved_client_contact_ids or []))

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM client_extraction_proposals
            WHERE id = ? AND client_id = ?
            """,
            (proposal_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Proposal not found for this client.")
        if _blank(row["status"]) != "Pending":
            raise ValueError(
                f"Only Pending proposals can be resolved (current status: {row['status']})."
            )
        conn.execute(
            """
            UPDATE client_extraction_proposals
            SET status = 'Resolved',
                resolved_at = ?,
                resolved_by_user_id = ?,
                resolved_by_name = ?,
                resolution_reason = ?,
                resolved_by_proposal_ids = ?,
                resolved_client_contact_ids = ?,
                reviewed_by_user_id = ?,
                reviewed_by_name = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (
                now,
                user.id,
                user_name,
                reason,
                prop_ids,
                contact_ids,
                user.id,
                user_name,
                now,
                now,
                proposal_id,
                client_id,
            ),
        )
        conn.commit()

    return next(
        p
        for p in list_extraction_proposals(client_id, user_id=user.id, status="All")
        if p.proposal_id == proposal_id
    )


def get_knowledge_section_catalog() -> list[dict[str, Any]]:
    """Sections + fields available for extraction remapping in the review UI."""
    from client_document_extraction import FIELD_CATALOG

    out: list[dict[str, Any]] = []
    for key in KNOWLEDGE_SECTIONS:
        fields = [
            {"field_name": fk, "field_label": fl}
            for fk, fl in (FIELD_CATALOG.get(key) or {}).items()
        ]
        out.append(
            {
                "section_key": key,
                "display_name": KNOWLEDGE_SECTION_LABELS.get(
                    key, key.replace("_", " ").title()
                ),
                "fields": fields,
            }
        )
    return out


def get_stored_knowledge_field(
    client_id: int,
    section_key: str,
    field_name: str,
    *,
    user_id: int | None = None,
) -> str:
    """Return a single stored knowledge field value (no campaign overlay invention)."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM client_knowledge_sections
            WHERE client_id = ? AND section_key = ?
            """,
            (client_id, section_key),
        ).fetchone()
    if not row:
        return ""
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return _blank(payload.get(field_name))


def update_knowledge_field(
    client_id: int,
    section_key: str,
    field_name: str,
    value: str,
    *,
    user_id: int | None = None,
) -> ClientKnowledgeSectionView:
    """Merge one catalog field into a knowledge section. Does not write CRM/campaigns."""
    from client_document_extraction import FIELD_CATALOG

    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    key = _blank(section_key)
    field = _blank(field_name)
    if key not in KNOWLEDGE_SECTIONS:
        raise ValueError("Unknown knowledge section.")
    catalog = FIELD_CATALOG.get(key) or {}
    if field not in catalog:
        raise ValueError(f"Unknown field '{field}' for section '{key}'.")
    # Guard: these strategy fields must never auto-write campaign fit signals
    if field in {"prospecting_guidance", "sales_challenges_barriers"}:
        pass
    user_name = _blank(user.full_name) or _blank(user.email)
    _merge_knowledge_field(
        client_id,
        key,
        field,
        _blank(value),
        user_id=user.id,
        user_name=user_name,
    )
    sections = get_knowledge_sections(client_id, user_id=user.id)
    for s in sections:
        if s.section_key == key:
            return s
    raise LookupError("Knowledge section not found after update.")


def restore_client_operations_from_approved_proposal(
    client_id: int,
    proposal_id: int,
    *,
    title: str,
    field_key: str,
    user_id: int | None = None,
) -> dict[str, Any]:
    """
    Restore a Client Operations item from an already-Approved proposal
    without re-approval or changing proposal status.
    """
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    user_name = _blank(user.full_name) or _blank(user.email)
    ensure_client_knowledge_schema()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.*, d.filename AS document_filename
            FROM client_extraction_proposals p
            LEFT JOIN client_documents d ON d.id = p.document_id
            WHERE p.id = ? AND p.client_id = ?
            """,
            (proposal_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Proposal not found for this client.")
        proposal = dict(row)

    if _blank(proposal.get("status")) != "Approved":
        raise ValueError("Only Approved proposals can be restored into Client Operations.")

    value = _blank(proposal.get("approved_value")) or _blank(proposal.get("proposed_value"))
    if not value:
        raise ValueError("Approved proposal has no value to restore.")

    field = CLIENT_OPERATIONS_FIELD_ALIASES.get(field_key, field_key)
    proposal["_item_title"] = title
    proposal["section"] = "client_operations"
    proposal["field_name"] = field

    apply_target = _append_client_operations_item(
        client_id,
        title=title,
        content=value,
        field_key=field,
        proposal=proposal,
        user_id=user.id,
        user_name=user_name,
        document_filename=_blank(proposal.get("document_filename")),
    )

    now = _now()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE client_extraction_proposals
            SET section = 'client_operations',
                field_name = ?,
                apply_target = ?,
                updated_at = ?
            WHERE id = ? AND client_id = ? AND status = 'Approved'
            """,
            (field, apply_target, now, proposal_id, client_id),
        )
        conn.commit()

    items = list_approved_client_operations(client_id, user_id=user.id)
    return {
        "proposal_id": proposal_id,
        "apply_target": apply_target,
        "items": items,
    }


def list_approved_client_operations(
    client_id: int, *, user_id: int | None = None
) -> list[dict[str, Any]]:
    """Approved Client Operations items only (Pending/Rejected proposals excluded)."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM client_knowledge_sections
            WHERE client_id = ? AND section_key = 'client_operations'
            """,
            (client_id,),
        ).fetchone()
    if not row:
        return []
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if _blank(it.get("status")).lower() not in {"", "approved"}:
            continue
        content = _blank(it.get("content"))
        if not content:
            continue
        if _SENSITIVE_PATTERNS.search(content):
            continue
        out.append(
            {
                "item_id": _blank(it.get("item_id")),
                "title": _blank(it.get("title")),
                "content": content,
                "field_key": _blank(it.get("field_key")),
                "source_document": _blank(it.get("source_document")),
                "source_snippet": _blank(it.get("source_snippet")),
                "source_locator": _blank(it.get("source_locator")),
                "approved_at": _blank(it.get("approved_at")),
                "approved_by": _blank(it.get("approved_by")),
                "proposal_id": it.get("proposal_id"),
            }
        )
    return out


def bulk_review_extraction_proposals(
    client_id: int,
    body: ClientExtractionBulkReviewRequest,
    *,
    user_id: int | None = None,
) -> list[ClientExtractionProposalView]:
    """Approve selected, or Approve All Matches (MATCH only; never conflicts/LOW)."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    mode = _blank(body.mode) or "selected"
    status = _blank(body.status) or "Approved"
    if status not in {"Approved", "Rejected"}:
        raise ValueError("Bulk status must be Approved or Rejected.")

    pending = list_extraction_proposals(client_id, user_id=user.id, status="Pending")
    targets: list[ClientExtractionProposalView] = []
    if mode == "matches":
        if status != "Approved":
            raise ValueError("Approve All Matches only supports Approved.")
        targets = [
            p
            for p in pending
            if p.classification == "MATCH"
            and p.confidence in {"HIGH", "MEDIUM"}
            and p.classification != "SENSITIVE — NOT IMPORTED"
        ]
    else:
        idset = set(int(x) for x in (body.proposal_ids or []))
        targets = [p for p in pending if p.proposal_id in idset]
        # Guard: do not allow bulk-approving conflicts or LOW in one blind action
        if status == "Approved":
            bad = [
                p
                for p in targets
                if p.classification in {"CONFLICT", "SENSITIVE — NOT IMPORTED"}
                or p.confidence == "LOW"
            ]
            if bad and len(bad) == len(targets):
                raise ValueError(
                    "Cannot approve only CONFLICT/LOW findings via bulk action. Review individually."
                )
            targets = [
                p
                for p in targets
                if p.classification not in {"CONFLICT", "SENSITIVE — NOT IMPORTED"}
                and p.confidence != "LOW"
            ]

    out: list[ClientExtractionProposalView] = []
    from client_contacts_data import parse_client_contact_people

    for p in targets:
        # Skip multi-person Client Contacts in bulk — must split first
        if status == "Approved" and (
            (p.section == "client_contacts" and p.field_name == "client_contacts")
            or (
                p.section == "client_profile"
                and p.field_name == "client_contacts"
            )
        ):
            people = parse_client_contact_people(p.proposed_value or "")
            if len(people) != 1:
                continue
        try:
            out.append(
                review_extraction_proposal(
                    client_id,
                    p.proposal_id,
                    ClientExtractionProposalUpdate(status=status),
                    user_id=user.id,
                )
            )
        except ValueError:
            continue
    return out


def _overlay_setup_defaults(client_id: int, section_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fill empty knowledge fields from Client Setup for display only (never writes back)."""
    merged = dict(payload or {})
    with get_connection() as conn:
        if section_key == "client_profile":
            row = conn.execute(
                "SELECT * FROM client_profiles WHERE client_id = ?", (client_id,)
            ).fetchone()
            if row:
                defaults = {
                    "website": row["website"] if "website" in row.keys() else "",
                    "address": row["address"] if "address" in row.keys() else "",
                    "main_location": row["main_location"] if "main_location" in row.keys() else "",
                    "main_phone": row["main_phone"] if "main_phone" in row.keys() else "",
                    "primary_owner_name": (
                        row["primary_owner_name"] if "primary_owner_name" in row.keys() else ""
                    ),
                    "who_takes_appointments": (
                        row["who_takes_appointments"]
                        if "who_takes_appointments" in row.keys()
                        else ""
                    ),
                }
                for k, v in defaults.items():
                    if not _blank(merged.get(k)) and _blank(v):
                        merged[k] = _blank(v)
        elif section_key == "strategy":
            camp = conn.execute(
                """
                SELECT * FROM client_campaigns
                WHERE client_id = ? AND is_default = 1
                ORDER BY id LIMIT 1
                """,
                (client_id,),
            ).fetchone()
            if camp:
                defaults = {
                    "primary_service": camp["primary_service"] if "primary_service" in camp.keys() else "",
                    "secondary_services": (
                        camp["secondary_services"] if "secondary_services" in camp.keys() else ""
                    ),
                    "target_industries": (
                        camp["target_customer_types"]
                        if "target_customer_types" in camp.keys()
                        else ""
                    ),
                    "target_geography": (
                        camp["geographic_preferences"]
                        if "geographic_preferences" in camp.keys()
                        else ""
                    ),
                    "target_titles": camp["target_titles"] if "target_titles" in camp.keys() else "",
                    "positive_fit_signals": (
                        camp["positive_signals"] if "positive_signals" in camp.keys() else ""
                    ),
                    "negative_fit_signals": (
                        camp["negative_signals"] if "negative_signals" in camp.keys() else ""
                    ),
                    "exclusions": camp["exclusions"] if "exclusions" in camp.keys() else "",
                }
                for k, v in defaults.items():
                    if not _blank(merged.get(k)) and _blank(v):
                        merged[k] = _blank(v)
    return merged


def get_knowledge_sections(
    client_id: int, *, user_id: int | None = None
) -> list[ClientKnowledgeSectionView]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        rows = {
            _blank(r["section_key"]): r
            for r in conn.execute(
                "SELECT * FROM client_knowledge_sections WHERE client_id = ?",
                (client_id,),
            ).fetchall()
        }
    out: list[ClientKnowledgeSectionView] = []
    for key in KNOWLEDGE_SECTIONS:
        r = rows.get(key)
        payload: dict[str, Any] = {}
        if r:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload = _overlay_setup_defaults(client_id, key, payload)
        out.append(
            ClientKnowledgeSectionView(
                section_key=key,
                payload=payload,
                updated_at=_blank(r["updated_at"]) if r else "",
                updated_by=_blank(r["updated_by_name"]) if r else "",
            )
        )
    return out


def upsert_knowledge_section(
    client_id: int,
    section_key: str,
    body: ClientKnowledgeSectionUpdate,
    *,
    user_id: int | None = None,
) -> ClientKnowledgeSectionView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    key = _blank(section_key)
    if key not in KNOWLEDGE_SECTIONS:
        raise ValueError("Unknown knowledge section.")
    # Never accept password-like keys into knowledge payload
    payload = dict(body.payload or {})
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        kl = str(k).lower()
        if "password" in kl or "secret" in kl or "credential" in kl or "token" in kl:
            continue
        if isinstance(v, str) and _SENSITIVE_PATTERNS.search(v):
            continue
        if k == "items" and isinstance(v, list):
            safe_items = []
            for it in v:
                if not isinstance(it, dict):
                    continue
                content = _blank(it.get("content"))
                title = _blank(it.get("title"))
                if _SENSITIVE_PATTERNS.search(content) or _SENSITIVE_PATTERNS.search(title):
                    continue
                safe_items.append(it)
            cleaned[k] = safe_items
            continue
        cleaned[k] = v
    ensure_client_knowledge_schema()
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO client_knowledge_sections (
                client_id, section_key, payload_json, updated_at,
                updated_by_user_id, updated_by_name
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id, section_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_by_name = excluded.updated_by_name
            """,
            (
                client_id,
                key,
                json.dumps(cleaned),
                now,
                user.id,
                _blank(user.full_name) or _blank(user.email),
            ),
        )
        conn.commit()
    return next(s for s in get_knowledge_sections(client_id, user_id=user.id) if s.section_key == key)


def list_email_templates(
    client_id: int, *, user_id: int | None = None
) -> list[ClientEmailTemplateView]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM client_email_templates
                WHERE client_id = ?
                ORDER BY is_active DESC, template_name COLLATE NOCASE
                """,
                (client_id,),
            ).fetchall()
    except Exception:
        # Empty / missing table must never break Client Knowledge hub
        return []

    out: list[ClientEmailTemplateView] = []
    for r in rows:
        d = dict(r)
        ids_raw = _blank(d.get("source_proposal_ids"))
        try:
            ids = json.loads(ids_raw) if ids_raw else []
        except Exception:
            ids = []
        if not isinstance(ids, list):
            ids = []
        out.append(
            ClientEmailTemplateView(
                template_id=int(d["id"]),
                client_id=int(d["client_id"]),
                template_name=_blank(d.get("template_name")),
                template_type=_blank(d.get("template_type")),
                subject=_blank(d.get("subject")),
                body=_blank(d.get("body")),
                is_active=bool(d.get("is_active")),
                source_document_id=(
                    int(d["source_document_id"])
                    if d.get("source_document_id") is not None
                    else None
                ),
                source_proposal_ids=[int(x) for x in ids if str(x).isdigit() or isinstance(x, int)],
                created_at=_blank(d.get("created_at")),
                updated_at=_blank(d.get("updated_at")),
                created_by_name=_blank(d.get("created_by_name")),
                approved_by_name=_blank(d.get("approved_by_name")),
            )
        )
    return out


def upsert_email_template(
    client_id: int,
    body: ClientEmailTemplateUpdate,
    *,
    template_id: int | None = None,
    user_id: int | None = None,
) -> ClientEmailTemplateView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    name = _blank(body.template_name)
    if not name:
        raise ValueError("Template name is required.")
    ensure_client_knowledge_schema()
    now = _now()
    user_name = _blank(user.full_name) or _blank(user.email)
    source_ids = [int(x) for x in (body.source_proposal_ids or [])]
    source_ids_json = json.dumps(source_ids)
    with get_connection() as conn:
        if template_id:
            row = conn.execute(
                "SELECT id FROM client_email_templates WHERE id = ? AND client_id = ?",
                (template_id, client_id),
            ).fetchone()
            if not row:
                raise LookupError("Template not found for this client.")
            conn.execute(
                """
                UPDATE client_email_templates SET
                    template_name = ?, template_type = ?, subject = ?, body = ?,
                    is_active = ?,
                    source_document_id = COALESCE(?, source_document_id),
                    source_proposal_ids = CASE
                        WHEN ? != '[]' THEN ?
                        ELSE source_proposal_ids
                    END,
                    updated_at = ?, updated_by_name = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    name,
                    _blank(body.template_type),
                    _blank(body.subject),
                    _blank(body.body),
                    1 if body.is_active else 0,
                    body.source_document_id,
                    source_ids_json,
                    source_ids_json,
                    now,
                    user_name,
                    template_id,
                    client_id,
                ),
            )
            tid = template_id
        else:
            cur = conn.execute(
                """
                INSERT INTO client_email_templates (
                    client_id, template_name, template_type, subject, body,
                    is_active, source_document_id, source_proposal_ids,
                    created_at, updated_at, updated_by_name,
                    created_by_user_id, created_by_name,
                    approved_by_user_id, approved_by_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    name,
                    _blank(body.template_type),
                    _blank(body.subject),
                    _blank(body.body),
                    1 if body.is_active else 0,
                    body.source_document_id,
                    source_ids_json,
                    now,
                    now,
                    user_name,
                    user.id,
                    user_name,
                    user.id,
                    user_name,
                ),
            )
            tid = int(cur.lastrowid)
        conn.commit()
    return next(t for t in list_email_templates(client_id, user_id=user.id) if t.template_id == tid)


def _detect_template_name_conflicts(
    client_id: int, body_text: str, *, user_id: int | None = None
) -> list[Any]:
    """Flag source person names that don't exactly match approved Client Contacts."""
    from client_contacts_data import list_client_contacts
    from models import EmailTemplateNameConflict

    contacts = list_client_contacts(client_id, user_id=user_id, include_inactive=False)
    active = [c for c in contacts if c.active]
    by_lower = {c.name.strip().lower(): c for c in active if c.name.strip()}
    conflicts: list[EmailTemplateNameConflict] = []

    candidates: list[str] = []
    for m in re.finditer(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b",
        body_text or "",
    ):
        candidates.append(m.group(1).strip())
    for m in re.finditer(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+or\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
        body_text or "",
    ):
        candidates.extend([m.group(1).strip(), m.group(2).strip()])

    # Skip obvious non-person phrases
    skip = re.compile(
        r"(?i)^(thank you|as requested|carmeco|progressive|laser cutting|"
        r"robotic and|whenever the|if you ever|lebanon|https)$"
    )

    seen: set[str] = set()
    for name in candidates:
        key = name.lower()
        if key in seen or skip.match(name) or len(name.split()) > 4:
            continue
        seen.add(key)
        if key in by_lower:
            continue
        first = name.split()[0].lower()
        first_hits = [
            c for c in active if c.name.strip().lower().split()[:1] == [first]
        ]
        if first_hits and all(c.name.strip().lower() != key for c in first_hits):
            hit = first_hits[0]
            conflicts.append(
                EmailTemplateNameConflict(
                    source_name=name,
                    approved_contact_name=hit.name,
                    approved_contact_id=hit.contact_id,
                    note=(
                        f"Source says '{name}' but approved Client Contact is "
                        f"'{hit.name}' (id={hit.contact_id}). Do not silently rewrite."
                    ),
                )
            )
    return conflicts


def preview_email_template_group(
    client_id: int,
    proposal_ids: list[int],
    *,
    user_id: int | None = None,
) -> Any:
    """Combine name/type/body proposals into one editable template preview. No writes."""
    from models import EmailTemplateGroupPreview

    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()

    idset = {int(x) for x in proposal_ids}
    if not idset:
        raise ValueError("Select at least one email template proposal.")

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT p.*, d.filename AS document_filename
            FROM client_extraction_proposals p
            LEFT JOIN client_documents d
              ON d.id = p.document_id AND d.client_id = p.client_id
            WHERE p.client_id = ? AND p.id IN ({",".join("?" * len(idset))})
            """,
            (client_id, *sorted(idset)),
        ).fetchall()

    if not rows:
        raise LookupError("No matching proposals found for this client.")

    # Expand to full document group when any email_templates piece is selected
    doc_ids = {
        int(r["document_id"])
        for r in rows
        if r["document_id"] is not None
        and _blank(r["section"]) == "email_templates"
    }
    if doc_ids:
        with get_connection() as conn:
            expanded = conn.execute(
                f"""
                SELECT p.*, d.filename AS document_filename
                FROM client_extraction_proposals p
                LEFT JOIN client_documents d
                  ON d.id = p.document_id AND d.client_id = p.client_id
                WHERE p.client_id = ?
                  AND p.section = 'email_templates'
                  AND p.document_id IN ({",".join("?" * len(doc_ids))})
                ORDER BY p.id
                """,
                (client_id, *sorted(doc_ids)),
            ).fetchall()
        rows = expanded

    for r in rows:
        if _blank(r["section"]) != "email_templates":
            raise ValueError(
                f"Proposal #{r['id']} is not an email_templates extraction "
                f"(section={r['section']})."
            )

    fields: dict[str, str] = {}
    statuses: dict[str, str] = {}
    evidence: list[str] = []
    source_ids: list[int] = []
    document_id = None
    document_filename = ""

    for r in rows:
        pid = int(r["id"])
        source_ids.append(pid)
        fn = _blank(r["field_name"])
        val = _blank(r["approved_value"]) or _blank(r["proposed_value"])
        statuses[fn or str(pid)] = _blank(r["status"]) or "Pending"
        if fn and val and fn not in fields:
            fields[fn] = val
        elif fn and val:
            fields[fn] = val
        ev = _blank(r["raw_source_text"]) or _blank(r["source_reference"])
        if ev and ev not in evidence:
            evidence.append(ev[:400])
        if r["document_id"] is not None:
            document_id = int(r["document_id"])
        if not document_filename:
            document_filename = _blank(r["document_filename"])

    template_name = fields.get("template_name", "")
    template_type = fields.get("template_type", "")
    subject = fields.get("subject", "")
    body = fields.get("body", "")

    # Prefer clearer Send Information name when source used "Send Email"
    if (
        "send information" in template_type.lower()
        and re.search(r"(?i)send\s+email\s+template", template_name)
    ):
        template_name = "Carmeco Send Information Template"

    conflicts = _detect_template_name_conflicts(client_id, body, user_id=user.id)

    return EmailTemplateGroupPreview(
        client_id=client_id,
        document_id=document_id,
        document_filename=document_filename,
        source_proposal_ids=source_ids,
        component_statuses=statuses,
        template_name=template_name,
        template_type=template_type,
        subject=subject,
        body=body,
        is_active=True,
        evidence=evidence[:8],
        name_conflicts=conflicts,
        requires_human_review=bool(conflicts),
        message=(
            f"Combined {len(source_ids)} extraction proposal(s) into one template preview. "
            + (
                f"{len(conflicts)} potential contact name conflict(s) require review."
                if conflicts
                else "No contact-name conflicts detected."
            )
        ),
    )


def approve_email_template_group(
    client_id: int,
    body: Any,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Create one email template and mark source proposals Resolved / Incorporated."""
    from models import ClientExtractionResolveRequest, EmailTemplateGroupApproveRequest

    if not isinstance(body, EmailTemplateGroupApproveRequest):
        body = EmailTemplateGroupApproveRequest.model_validate(body)

    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)

    preview = preview_email_template_group(
        client_id, body.proposal_ids or [], user_id=user.id
    )
    if preview.name_conflicts and not body.acknowledge_name_conflicts:
        raise ValueError(
            "Potential contact name conflict(s) require review. "
            "Edit the body and set acknowledge_name_conflicts=true to approve."
        )

    name = _blank(body.template_name) or _blank(preview.template_name)
    ttype = _blank(body.template_type) or _blank(preview.template_type)
    subject = body.subject if body.subject is not None else preview.subject
    tmpl_body = body.body if body.body is not None else preview.body
    if not name:
        raise ValueError("Template name is required.")
    if not tmpl_body:
        raise ValueError("Template body is required.")

    template = upsert_email_template(
        client_id,
        ClientEmailTemplateUpdate(
            template_name=name,
            template_type=ttype or "Email Template",
            subject=_blank(subject),
            body=_blank(tmpl_body),
            is_active=bool(body.is_active),
            source_document_id=preview.document_id,
            source_proposal_ids=list(preview.source_proposal_ids),
        ),
        user_id=user.id,
    )

    resolved: list[int] = []
    skipped: list[dict[str, Any]] = []
    reason = (
        f"Incorporated into Email Template #{template.template_id} "
        f"({template.template_name})."
    )
    for pid in preview.source_proposal_ids:
        try:
            # Only resolve Pending — leave Rejected/Approved unchanged
            with get_connection() as conn:
                st = conn.execute(
                    "SELECT status FROM client_extraction_proposals WHERE id = ? AND client_id = ?",
                    (pid, client_id),
                ).fetchone()
            if not st:
                skipped.append({"proposal_id": pid, "reason": "Not found."})
                continue
            if _blank(st["status"]) != "Pending":
                skipped.append(
                    {
                        "proposal_id": pid,
                        "reason": f"Status is {st['status']}, left unchanged.",
                    }
                )
                continue
            resolve_extraction_proposal(
                client_id,
                pid,
                ClientExtractionResolveRequest(resolution_reason=reason),
                user_id=user.id,
            )
            resolved.append(pid)
        except Exception as exc:
            skipped.append({"proposal_id": pid, "reason": str(exc)})

    return {
        "template": template,
        "resolved_proposal_ids": resolved,
        "skipped": skipped,
        "message": (
            f"Approved template '{template.template_name}'. "
            f"Resolved {len(resolved)} source proposal(s); {len(skipped)} skipped."
        ),
    }


# --- Appointment import foundation ---

APPOINTMENT_TARGET_FIELDS = (
    "external_record_no",
    "northstar_client",
    "revenue_specialist",
    "appointment_date",
    "appointment_time",
    "company",
    "contact",
    "title",
    "address",
    "city",
    "state",
    "zip",
    "phone",
    "email",
    "caller_notes",
    "appointment_grade",
    "sales_notes",
    "dollars_quoted",
    "outcome",
)


def _parse_tabular_headers_and_rows(filename: str, content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = [h for h in (reader.fieldnames or []) if h is not None]
        rows = []
        for i, row in enumerate(reader):
            if i >= 5000:
                break
            rows.append({str(k): _blank(v) for k, v in row.items() if k is not None})
        return headers, rows
    if ext == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError(
                "Excel support requires openpyxl. Upload CSV for Phase 1, or install openpyxl."
            ) from exc
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
        if not raw_rows:
            return [], []
        headers = [_blank(h) or f"Column{i+1}" for i, h in enumerate(raw_rows[0])]
        out = []
        for raw in raw_rows[1:5001]:
            item = {}
            empty = True
            for i, h in enumerate(headers):
                val = _blank(raw[i] if i < len(raw) else "")
                item[h] = val
                if val:
                    empty = False
            if not empty:
                out.append(item)
        return headers, out
    raise ValueError("Appointment grids must be .csv or .xlsx.")


def _normalize_name(value: str) -> str:
    t = _blank(value).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _match_company(conn, client_id: int, record_no: str, company_name: str) -> tuple[str, int | None, str, str]:
    """Return (status, company_id, display_name, record_no). Never creates companies."""
    rn = _blank(record_no)
    if rn:
        row = conn.execute(
            """
            SELECT c.id, c.company_name, ccr.external_record_no
            FROM client_company_relationships ccr
            JOIN companies c ON c.id = ccr.company_id
            WHERE ccr.client_id = ? AND ccr.external_record_no = ?
            LIMIT 1
            """,
            (client_id, rn),
        ).fetchone()
        if row:
            return (
                "MATCHED",
                int(row["id"]),
                _blank(row["company_name"]),
                _blank(row["external_record_no"]),
            )
        # Master record number fallback (still scoped by existence; display only)
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no FROM companies
            WHERE external_record_no = ?
            LIMIT 1
            """,
            (rn,),
        ).fetchone()
        if row:
            # Exists in NorthStar but may not have CCR for this client — POSSIBLE
            return (
                "POSSIBLE MATCH",
                int(row["id"]),
                _blank(row["company_name"]),
                _blank(row["external_record_no"]),
            )

    norm = _normalize_name(company_name)
    if not norm:
        return "NEW", None, company_name, rn

    # Exact-ish name within this client's relationships
    rows = conn.execute(
        """
        SELECT c.id, c.company_name, ccr.external_record_no
        FROM client_company_relationships ccr
        JOIN companies c ON c.id = ccr.company_id
        WHERE ccr.client_id = ?
        """,
        (client_id,),
    ).fetchall()
    exact = []
    partial = []
    for r in rows:
        cn = _normalize_name(r["company_name"])
        if cn == norm:
            exact.append(r)
        elif norm in cn or cn in norm:
            partial.append(r)
    if len(exact) == 1:
        r = exact[0]
        return "MATCHED", int(r["id"]), _blank(r["company_name"]), _blank(r["external_record_no"])
    if len(exact) > 1 or partial:
        r = (exact or partial)[0]
        return (
            "POSSIBLE MATCH",
            int(r["id"]),
            _blank(r["company_name"]),
            _blank(r["external_record_no"]),
        )
    return "NEW", None, company_name, rn


def _match_contact(
    conn, company_id: int | None, email: str, contact_name: str, phone: str
) -> tuple[str, int | None, str]:
    """Return (status, contact_id, display_name). Never creates contacts."""
    if company_id is None:
        return "NEW", None, contact_name
    em = _blank(email).lower()
    if em:
        row = conn.execute(
            """
            SELECT id, first_name, last_name FROM contacts
            WHERE company_id = ? AND lower(email) = ?
            LIMIT 1
            """,
            (company_id, em),
        ).fetchone()
        if row:
            return (
                "MATCHED",
                int(row["id"]),
                f"{_blank(row['first_name'])} {_blank(row['last_name'])}".strip(),
            )
    ph = re.sub(r"\D", "", _blank(phone))
    if len(ph) >= 7:
        for r in conn.execute(
            "SELECT id, first_name, last_name, phone, alt_phone FROM contacts WHERE company_id = ?",
            (company_id,),
        ).fetchall():
            for field in ("phone", "alt_phone"):
                digits = re.sub(r"\D", "", _blank(r[field]))
                if digits and (digits.endswith(ph[-7:]) or ph.endswith(digits[-7:])):
                    return (
                        "MATCHED",
                        int(r["id"]),
                        f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
                    )
    norm = _normalize_name(contact_name)
    if norm:
        possibles = []
        for r in conn.execute(
            "SELECT id, first_name, last_name FROM contacts WHERE company_id = ?",
            (company_id,),
        ).fetchall():
            full = _normalize_name(f"{r['first_name']} {r['last_name']}")
            if full == norm:
                return (
                    "MATCHED",
                    int(r["id"]),
                    f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
                )
            if norm and (norm in full or full in norm):
                possibles.append(r)
        if len(possibles) == 1:
            r = possibles[0]
            return (
                "POSSIBLE MATCH",
                int(r["id"]),
                f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
            )
        if possibles:
            r = possibles[0]
            return (
                "POSSIBLE MATCH",
                int(r["id"]),
                f"{_blank(r['first_name'])} {_blank(r['last_name'])}".strip(),
            )
    return "NEW", None, contact_name


def start_appointment_import(
    client_id: int,
    *,
    filename: str,
    content: bytes,
    user_id: int | None = None,
) -> ClientAppointmentImportBatchView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)

    # Store as document (Appointment Grid)
    doc = upload_document(
        client_id,
        filename=filename,
        content=content,
        document_type="Appointment Grid",
        user_id=user.id,
    )
    headers, rows = _parse_tabular_headers_and_rows(filename, content)
    ensure_client_knowledge_schema()
    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO client_appointment_import_batches (
                client_id, document_id, filename, status, column_mapping_json,
                headers_json, row_count, uploaded_at, uploaded_by_user_id, uploaded_by_name
            ) VALUES (?, ?, ?, 'uploaded', '{}', ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                doc.document_id,
                Path(filename).name,
                json.dumps(headers),
                len(rows),
                now,
                user.id,
                _blank(user.full_name) or _blank(user.email),
            ),
        )
        batch_id = int(cur.lastrowid)
        for i, row in enumerate(rows):
            conn.execute(
                """
                INSERT INTO client_appointment_import_rows (
                    batch_id, client_id, row_index, raw_json, mapped_json,
                    company_match_status, contact_match_status, import_status
                ) VALUES (?, ?, ?, ?, '{}', 'NEW', 'NEW', 'staged')
                """,
                (batch_id, client_id, i, json.dumps(row)),
            )
        conn.commit()
    return get_appointment_import_batch(client_id, batch_id, user_id=user.id)


def get_appointment_import_batch(
    client_id: int, batch_id: int, *, user_id: int | None = None
) -> ClientAppointmentImportBatchView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        b = conn.execute(
            """
            SELECT * FROM client_appointment_import_batches
            WHERE id = ? AND client_id = ?
            """,
            (batch_id, client_id),
        ).fetchone()
        if not b:
            raise LookupError("Import batch not found for this client.")
        try:
            headers = json.loads(b["headers_json"] or "[]")
        except Exception:
            headers = []
        try:
            mapping = json.loads(b["column_mapping_json"] or "{}")
        except Exception:
            mapping = {}
        return ClientAppointmentImportBatchView(
            batch_id=int(b["id"]),
            client_id=int(b["client_id"]),
            document_id=int(b["document_id"]) if b["document_id"] else None,
            filename=_blank(b["filename"]),
            status=_blank(b["status"]),
            headers=headers if isinstance(headers, list) else [],
            column_mapping=mapping if isinstance(mapping, dict) else {},
            row_count=int(b["row_count"] or 0),
            uploaded_at=_blank(b["uploaded_at"]),
            uploaded_by=_blank(b["uploaded_by_name"]),
            target_fields=list(APPOINTMENT_TARGET_FIELDS),
        )


def map_appointment_import_columns(
    client_id: int,
    batch_id: int,
    body: ClientAppointmentImportMapRequest,
    *,
    user_id: int | None = None,
) -> ClientAppointmentImportPreview:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_knowledge_schema()
    mapping = {str(k): _blank(v) for k, v in (body.column_mapping or {}).items() if _blank(v)}
    with get_connection() as conn:
        b = conn.execute(
            "SELECT * FROM client_appointment_import_batches WHERE id = ? AND client_id = ?",
            (batch_id, client_id),
        ).fetchone()
        if not b:
            raise LookupError("Import batch not found for this client.")
        rows = conn.execute(
            """
            SELECT * FROM client_appointment_import_rows
            WHERE batch_id = ? AND client_id = ?
            ORDER BY row_index
            """,
            (batch_id, client_id),
        ).fetchall()
        preview_rows: list[ClientAppointmentImportRowView] = []
        for r in rows:
            try:
                raw = json.loads(r["raw_json"] or "{}")
            except Exception:
                raw = {}
            mapped = {}
            for target, source_col in mapping.items():
                if target in APPOINTMENT_TARGET_FIELDS:
                    mapped[target] = _blank(raw.get(source_col))
            c_status, c_id, c_name, c_rn = _match_company(
                conn,
                client_id,
                mapped.get("external_record_no", ""),
                mapped.get("company", ""),
            )
            t_status, t_id, t_name = _match_contact(
                conn,
                c_id,
                mapped.get("email", ""),
                mapped.get("contact", ""),
                mapped.get("phone", ""),
            )
            conn.execute(
                """
                UPDATE client_appointment_import_rows SET
                    mapped_json = ?,
                    company_match_status = ?, company_id = ?, company_name = ?,
                    company_record_no = ?,
                    contact_match_status = ?, contact_id = ?, contact_name = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    json.dumps(mapped),
                    c_status,
                    c_id,
                    c_name or mapped.get("company", ""),
                    c_rn or mapped.get("external_record_no", ""),
                    t_status,
                    t_id,
                    t_name or mapped.get("contact", ""),
                    int(r["id"]),
                    client_id,
                ),
            )
            preview_rows.append(
                ClientAppointmentImportRowView(
                    row_id=int(r["id"]),
                    row_index=int(r["row_index"]),
                    mapped=mapped,
                    company_match_status=c_status,
                    company_id=c_id,
                    company_name=c_name or mapped.get("company", ""),
                    company_record_no=c_rn or mapped.get("external_record_no", ""),
                    contact_match_status=t_status,
                    contact_id=t_id,
                    contact_name=t_name or mapped.get("contact", ""),
                    import_status="staged",
                )
            )
        conn.execute(
            """
            UPDATE client_appointment_import_batches
            SET column_mapping_json = ?, status = 'mapped'
            WHERE id = ? AND client_id = ?
            """,
            (json.dumps(mapping), batch_id, client_id),
        )
        conn.commit()
    batch = get_appointment_import_batch(client_id, batch_id, user_id=user.id)
    return ClientAppointmentImportPreview(batch=batch, rows=preview_rows[:200])


def confirm_appointment_import(
    client_id: int,
    batch_id: int,
    body: ClientAppointmentImportConfirmRequest,
    *,
    user_id: int | None = None,
) -> ClientAppointmentImportBatchView:
    """
    Write client-scoped appointment events only.
    Does NOT create companies/contacts and does NOT modify CRM records.
    NEW matches are stored as events with null company/contact ids.
    """
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    if not body.confirm:
        raise ValueError("Import not confirmed.")
    ensure_client_knowledge_schema()
    now = _now()
    with get_connection() as conn:
        b = conn.execute(
            "SELECT * FROM client_appointment_import_batches WHERE id = ? AND client_id = ?",
            (batch_id, client_id),
        ).fetchone()
        if not b:
            raise LookupError("Import batch not found for this client.")
        if _blank(b["status"]) == "imported":
            return get_appointment_import_batch(client_id, batch_id, user_id=user.id)
        rows = conn.execute(
            """
            SELECT * FROM client_appointment_import_rows
            WHERE batch_id = ? AND client_id = ?
            ORDER BY row_index
            """,
            (batch_id, client_id),
        ).fetchall()
        for r in rows:
            try:
                mapped = json.loads(r["mapped_json"] or "{}")
            except Exception:
                mapped = {}
            # Preserve history: each spreadsheet row becomes its own event
            cur = conn.execute(
                """
                INSERT INTO client_appointment_events (
                    client_id, batch_id, import_row_id, company_id, contact_id,
                    external_record_no, company_name, contact_name, contact_title,
                    appointment_date, appointment_time, phone, email, address,
                    city, state, zip, caller_notes, appointment_grade, sales_notes,
                    dollars_quoted, outcome, revenue_specialist, event_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Appointment', ?)
                """,
                (
                    client_id,
                    batch_id,
                    int(r["id"]),
                    r["company_id"],
                    r["contact_id"],
                    _blank(mapped.get("external_record_no")) or _blank(r["company_record_no"]),
                    _blank(r["company_name"]) or _blank(mapped.get("company")),
                    _blank(r["contact_name"]) or _blank(mapped.get("contact")),
                    _blank(mapped.get("title")),
                    _blank(mapped.get("appointment_date")),
                    _blank(mapped.get("appointment_time")),
                    _blank(mapped.get("phone")),
                    _blank(mapped.get("email")),
                    _blank(mapped.get("address")),
                    _blank(mapped.get("city")),
                    _blank(mapped.get("state")),
                    _blank(mapped.get("zip")),
                    _blank(mapped.get("caller_notes")),
                    _blank(mapped.get("appointment_grade")),
                    _blank(mapped.get("sales_notes")),
                    _blank(mapped.get("dollars_quoted")),
                    _blank(mapped.get("outcome")),
                    _blank(mapped.get("revenue_specialist")),
                    now,
                ),
            )
            event_id = int(cur.lastrowid)
            conn.execute(
                """
                UPDATE client_appointment_import_rows
                SET import_status = 'imported', event_id = ?
                WHERE id = ? AND client_id = ?
                """,
                (event_id, int(r["id"]), client_id),
            )
        conn.execute(
            """
            UPDATE client_appointment_import_batches
            SET status = 'imported', confirmed_at = ?, confirmed_by_name = ?
            WHERE id = ? AND client_id = ?
            """,
            (now, _blank(user.full_name) or _blank(user.email), batch_id, client_id),
        )
        conn.commit()
    return get_appointment_import_batch(client_id, batch_id, user_id=user.id)


def list_appointment_events(
    client_id: int, *, user_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM client_appointment_events
            WHERE client_id = ?
            ORDER BY appointment_date DESC, id DESC
            LIMIT ?
            """,
            (client_id, max(1, min(limit, 500))),
        ).fetchall()
    return [dict(r) for r in rows]


def get_client_knowledge_hub(
    client_id: int, *, user_id: int | None = None
) -> ClientKnowledgeHubResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_knowledge_schema()
    with get_connection() as conn:
        cl = conn.execute(
            "SELECT id, name, code FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not cl:
            raise LookupError("Client not found.")
    from client_contacts_data import ROLE_TYPES, list_client_contacts

    email_accounts: list = []
    try:
        from client_email_accounts_data import list_email_accounts as _list_email_accounts

        email_accounts = _list_email_accounts(client_id, user_id=user.id)
    except Exception:
        email_accounts = []

    return ClientKnowledgeHubResponse(
        client_id=client_id,
        client_name=_blank(cl["name"]),
        client_code=_blank(cl["code"]),
        can_edit=user_can_edit_client_setup(user.id, client_id),
        documents=list_documents(client_id, user_id=user.id),
        sections=get_knowledge_sections(client_id, user_id=user.id),
        email_templates=list_email_templates(client_id, user_id=user.id),
        email_accounts=email_accounts,
        extraction_proposals=list_extraction_proposals(
            client_id, user_id=user.id, status="Pending"
        ),
        document_types=list(DOCUMENT_TYPES),
        appointment_events=list_appointment_events(client_id, user_id=user.id, limit=50),
        client_contacts=list_client_contacts(
            client_id, user_id=user.id, include_inactive=True
        ),
        client_contact_role_types=[r for r in ROLE_TYPES if r],
    )
