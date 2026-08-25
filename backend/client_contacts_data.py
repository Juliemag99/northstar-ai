"""Client Contacts — people who work for NorthStar's clients (not prospect CRM contacts)."""

from __future__ import annotations

import json
import re
from typing import Any

from access import (
    get_default_user,
    get_user_by_id,
    user_can_access_client,
)
from client_setup_data import user_can_edit_client_setup
from db import get_connection
from models import (
    ClientContactCreate,
    ClientContactSplitPreview,
    ClientContactSplitPerson,
    ClientContactUpdate,
    ClientContactView,
)

ROLE_TYPES = (
    "Client Owner / Executive",
    "Sales Contact",
    "Operations Contact",
    "Takes Appointments",
    "Appointment CC",
    "Strategy Contact",
    "Billing / Administrative",
    "Other",
    "",  # allowed when unknown
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+", re.I)
_PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"
)
_PERSON_SPLIT_RE = re.compile(r"\s*;\s*|\s*\|\s*|\n+")
_NAME_TITLE_RE = re.compile(
    r"^(?P<name>[^—\-–<]+?)(?:\s*[—\-–]\s*(?P<title>[^<]+))?(?:\s*<(?P<email>[^>]+)>)?$"
)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_email(email: str) -> str:
    return _blank(email).lower()


def _norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", _blank(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _norm_name(name: str) -> str:
    t = re.sub(r"\s+", " ", _blank(name).lower())
    t = re.sub(r"[^\w\s]", "", t)
    return t


def ensure_client_contacts_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                role_type TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                source_document_id INTEGER,
                source_proposal_id INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                created_by_name TEXT NOT NULL DEFAULT '',
                updated_by_name TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_client_contacts_client
                ON client_contacts(client_id, active, name COLLATE NOCASE);

            CREATE INDEX IF NOT EXISTS idx_client_contacts_email
                ON client_contacts(client_id, email);
            """
        )
        conn.commit()
    finally:
        if owns:
            conn.close()


def _require_access(user_id: int, client_id: int) -> None:
    from access import user_can_access_client

    if not user_can_access_client(user_id, client_id):
        raise PermissionError("Not authorized for this client.")


def _require_edit(user_id: int, client_id: int) -> None:
    _require_access(user_id, client_id)
    if not user_can_edit_client_setup(user_id, client_id):
        raise PermissionError("Not authorized to edit this client.")


def _row_to_view(row: Any) -> ClientContactView:
    d = dict(row)
    return ClientContactView(
        contact_id=int(d["id"]),
        client_id=int(d["client_id"]),
        name=_blank(d.get("name")),
        title=_blank(d.get("title")),
        email=_blank(d.get("email")),
        phone=_blank(d.get("phone")),
        role_type=_blank(d.get("role_type")),
        notes=_blank(d.get("notes")),
        active=bool(int(d.get("active") or 0)),
        source_document_id=(
            int(d["source_document_id"]) if d.get("source_document_id") else None
        ),
        source_proposal_id=(
            int(d["source_proposal_id"]) if d.get("source_proposal_id") else None
        ),
        created_at=_blank(d.get("created_at")),
        updated_at=_blank(d.get("updated_at")),
        created_by=_blank(d.get("created_by_name")),
        updated_by=_blank(d.get("updated_by_name")),
    )


def parse_client_contact_people(text: str) -> list[dict[str, str]]:
    """Split a multi-person extraction string into individual people dicts."""
    raw = _blank(text)
    if not raw:
        return []
    chunks = [c.strip() for c in _PERSON_SPLIT_RE.split(raw) if c.strip()]
    people: list[dict[str, str]] = []
    seen: set[str] = set()
    for chunk in chunks:
        email = ""
        em = _EMAIL_RE.search(chunk)
        if em:
            email = em.group(0)
        phone = ""
        pm = _PHONE_RE.search(chunk)
        if pm:
            phone = pm.group(0)
        # Strip email/phone from working name portion
        work = chunk
        if email:
            work = work.replace(f"<{email}>", " ").replace(email, " ")
        if phone:
            work = work.replace(phone, " ")
        work = re.sub(r"[<>]", " ", work).strip(" ;,|-–—")
        name = ""
        title = ""
        m = re.match(
            r"^(?P<name>.+?)(?:\s*[—\-–:]\s*(?P<title>.+))?$",
            work,
        )
        if m:
            name = _blank(m.group("name"))
            title = _blank(m.group("title") or "")
        else:
            name = _blank(work)
        # Drop junk chunks
        if not name and not email:
            continue
        if name.lower() in {"and", "or", "the"}:
            continue
        # Title-only heuristics
        if not title and " — " in chunk:
            parts = [p.strip() for p in chunk.split("—", 1)]
            if len(parts) == 2:
                name, title = parts[0], re.sub(r"<[^>]+>", "", parts[1]).strip()
        key = f"{_norm_email(email)}|{_norm_name(name)}|{_norm_phone(phone)}"
        if key in seen:
            continue
        seen.add(key)
        people.append(
            {
                "name": name,
                "title": title,
                "email": email,
                "phone": phone,
                "role_type": "",
                "notes": "",
                "raw": chunk,
            }
        )
    return people


def find_duplicate_client_contact(
    client_id: int,
    *,
    name: str = "",
    email: str = "",
    phone: str = "",
    exclude_id: int | None = None,
) -> ClientContactView | None:
    """Match within same client only. Prefer email, then name+phone, then name."""
    ensure_client_contacts_schema()
    email_n = _norm_email(email)
    phone_n = _norm_phone(phone)
    name_n = _norm_name(name)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM client_contacts
            WHERE client_id = ?
            ORDER BY active DESC, id ASC
            """,
            (client_id,),
        ).fetchall()
    email_hit = None
    name_phone_hit = None
    name_hit = None
    for row in rows:
        cid = int(row["id"])
        if exclude_id is not None and cid == exclude_id:
            continue
        r_email = _norm_email(row["email"])
        r_phone = _norm_phone(row["phone"])
        r_name = _norm_name(row["name"])
        if email_n and r_email and email_n == r_email:
            email_hit = row
            break
        if name_n and phone_n and r_name == name_n and r_phone and r_phone == phone_n:
            name_phone_hit = name_phone_hit or row
        if name_n and r_name == name_n and (email_n or phone_n):
            # name match only when other identifying data present on the candidate
            if (email_n and not r_email) or (phone_n and not r_phone):
                name_hit = name_hit or row
            elif not email_n and not phone_n:
                pass
            elif r_name == name_n and (email_n or phone_n):
                name_hit = name_hit or row
    hit = email_hit or name_phone_hit
    if hit:
        return _row_to_view(hit)
    # Uncertain name-only with supporting data on incoming → flag, don't auto-merge
    if name_hit and (email_n or phone_n):
        return _row_to_view(name_hit)
    return None


def list_client_contacts(
    client_id: int,
    *,
    user_id: int | None = None,
    include_inactive: bool = False,
) -> list[ClientContactView]:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_contacts_schema()
    with get_connection() as conn:
        if include_inactive:
            rows = conn.execute(
                """
                SELECT * FROM client_contacts
                WHERE client_id = ?
                ORDER BY active DESC, name COLLATE NOCASE, id
                """,
                (client_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM client_contacts
                WHERE client_id = ? AND active = 1
                ORDER BY name COLLATE NOCASE, id
                """,
                (client_id,),
            ).fetchall()
    return [_row_to_view(r) for r in rows]


def create_client_contact(
    client_id: int,
    body: ClientContactCreate,
    *,
    user_id: int | None = None,
    allow_duplicate_flag: bool = False,
) -> ClientContactView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_contacts_schema()
    name = _blank(body.name)
    email = _blank(body.email)
    phone = _blank(body.phone)
    if not name and not email:
        raise ValueError("Name or email is required.")
    role = _blank(body.role_type)
    if role and role not in ROLE_TYPES:
        raise ValueError(f"Invalid role_type: {role}")

    dup = find_duplicate_client_contact(
        client_id, name=name, email=email, phone=phone
    )
    if dup is not None and not allow_duplicate_flag:
        raise ValueError(
            f"Possible duplicate Client Contact: {dup.name}"
            + (f" <{dup.email}>" if dup.email else "")
            + f" (id={dup.contact_id}). Review before creating."
        )

    now = _now()
    user_name = _blank(user.full_name) or _blank(user.email)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO client_contacts (
                client_id, name, title, email, phone, role_type, notes, active,
                source_document_id, source_proposal_id,
                created_at, updated_at, created_by_name, updated_by_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                name,
                _blank(body.title),
                email,
                phone,
                role,
                _blank(body.notes),
                body.source_document_id,
                body.source_proposal_id,
                now,
                now,
                user_name,
                user_name,
            ),
        )
        new_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM client_contacts WHERE id = ? AND client_id = ?",
            (new_id, client_id),
        ).fetchone()
    return _row_to_view(row)


def update_client_contact(
    client_id: int,
    contact_id: int,
    body: ClientContactUpdate,
    *,
    user_id: int | None = None,
) -> ClientContactView:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_contacts_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_contacts WHERE id = ? AND client_id = ?",
            (contact_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Client contact not found.")
        data = dict(row)
        name = _blank(body.name) if body.name is not None else _blank(data["name"])
        title = _blank(body.title) if body.title is not None else _blank(data["title"])
        email = _blank(body.email) if body.email is not None else _blank(data["email"])
        phone = _blank(body.phone) if body.phone is not None else _blank(data["phone"])
        role = (
            _blank(body.role_type)
            if body.role_type is not None
            else _blank(data["role_type"])
        )
        notes = _blank(body.notes) if body.notes is not None else _blank(data["notes"])
        active = (
            1
            if (body.active if body.active is not None else bool(data["active"]))
            else 0
        )
        if role and role not in ROLE_TYPES:
            raise ValueError(f"Invalid role_type: {role}")
        if not name and not email:
            raise ValueError("Name or email is required.")

        dup = find_duplicate_client_contact(
            client_id,
            name=name,
            email=email,
            phone=phone,
            exclude_id=contact_id,
        )
        if dup is not None:
            raise ValueError(
                f"Possible duplicate Client Contact: {dup.name} (id={dup.contact_id})."
            )

        now = _now()
        user_name = _blank(user.full_name) or _blank(user.email)
        conn.execute(
            """
            UPDATE client_contacts SET
                name = ?, title = ?, email = ?, phone = ?, role_type = ?,
                notes = ?, active = ?, updated_at = ?, updated_by_name = ?
            WHERE id = ? AND client_id = ?
            """,
            (
                name,
                title,
                email,
                phone,
                role,
                notes,
                active,
                now,
                user_name,
                contact_id,
                client_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM client_contacts WHERE id = ? AND client_id = ?",
            (contact_id, client_id),
        ).fetchone()
    return _row_to_view(row)


def deactivate_client_contact(
    client_id: int, contact_id: int, *, user_id: int | None = None
) -> ClientContactView:
    return update_client_contact(
        client_id,
        contact_id,
        ClientContactUpdate(active=False),
        user_id=user_id,
    )


def preview_split_contact_proposal(
    client_id: int,
    proposal_id: int,
    *,
    user_id: int | None = None,
) -> ClientContactSplitPreview:
    """Parse a multi-person proposal into individual draft people. Read-only."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_contacts_schema()
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
        text = _blank(row["proposed_value"]) or _blank(row["approved_value"])
    people = parse_client_contact_people(text)
    drafts: list[ClientContactSplitPerson] = []
    for p in people:
        dup = find_duplicate_client_contact(
            client_id,
            name=p["name"],
            email=p["email"],
            phone=p["phone"],
        )
        drafts.append(
            ClientContactSplitPerson(
                name=p["name"],
                title=p["title"],
                email=p["email"],
                phone=p["phone"],
                role_type=p.get("role_type") or "",
                notes="",
                raw=p.get("raw") or "",
                possible_duplicate_id=dup.contact_id if dup else None,
                possible_duplicate_name=dup.name if dup else "",
                needs_review=bool(dup) or (not p["name"] and not p["email"]),
            )
        )
    return ClientContactSplitPreview(
        proposal_id=proposal_id,
        client_id=client_id,
        source_text=text,
        people=drafts,
        person_count=len(drafts),
    )


def create_split_contact_proposals(
    client_id: int,
    proposal_id: int,
    people: list[ClientContactSplitPerson] | None = None,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Create individual Pending extraction proposals for each person.
    Does NOT approve the parent proposal and does NOT create contacts yet.
    """
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_contacts_schema()
    from client_knowledge_data import ensure_client_knowledge_schema

    ensure_client_knowledge_schema()

    preview = preview_split_contact_proposal(
        client_id, proposal_id, user_id=user.id
    )
    drafts = people if people is not None else preview.people
    if not drafts:
        raise ValueError("No people found to split from this proposal.")

    with get_connection() as conn:
        parent = conn.execute(
            """
            SELECT * FROM client_extraction_proposals
            WHERE id = ? AND client_id = ?
            """,
            (proposal_id, client_id),
        ).fetchone()
        if not parent:
            raise LookupError("Proposal not found for this client.")
        if _blank(parent["status"]) != "Pending":
            raise ValueError("Only Pending proposals can be split into contact drafts.")

        now = _now()
        created: list[dict[str, Any]] = []
        for person in drafts:
            name = _blank(person.name)
            email = _blank(person.email)
            if not name and not email:
                continue
            payload = {
                "name": name,
                "title": _blank(person.title),
                "email": email,
                "phone": _blank(person.phone),
                "role_type": _blank(person.role_type),
                "notes": _blank(person.notes),
            }
            display = name or email
            if email and name:
                display = f"{name} <{email}>"
            elif _blank(person.title):
                display = f"{name} — {person.title}"
            cur = conn.execute(
                """
                INSERT INTO client_extraction_proposals (
                    client_id, document_id, section, field_name,
                    existing_value, proposed_value, source_reference,
                    status, created_at, updated_at,
                    document_type, raw_source_text, source_locator,
                    confidence, classification, extracted_value
                ) VALUES (
                    ?, ?, 'client_contacts', 'client_contact_person',
                    '', ?, ?,
                    'Pending', ?, ?,
                    ?, ?, ?,
                    'HIGH', 'NEW INFORMATION', ?
                )
                """,
                (
                    client_id,
                    parent["document_id"],
                    json.dumps(payload, ensure_ascii=False),
                    f"Split from proposal #{proposal_id}: {display}",
                    now,
                    now,
                    _blank(parent["document_type"]),
                    _blank(person.raw) or display,
                    f"parent_proposal:{proposal_id}",
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            created.append(
                {
                    "proposal_id": int(cur.lastrowid),
                    "name": name,
                    "email": email,
                    "title": _blank(person.title),
                    "possible_duplicate_id": person.possible_duplicate_id,
                    "needs_review": person.needs_review,
                }
            )
        # Annotate parent source_locator; keep Pending (do not approve)
        loc = _blank(parent["source_locator"])
        note = f"split_into:{len(created)}"
        new_loc = f"{loc}; {note}".strip("; ") if loc else note
        conn.execute(
            """
            UPDATE client_extraction_proposals
            SET source_locator = ?, updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (new_loc, now, proposal_id, client_id),
        )
        conn.commit()
    return created


def _merge_provenance_notes(
    existing_notes: str,
    *,
    proposal_id: int | None,
    document_id: int | None,
    document_filename: str,
    related_refs: list[str],
    evidence: str,
) -> str:
    """Append approval provenance without discarding prior notes."""
    lines: list[str] = []
    base = _blank(existing_notes)
    if base:
        lines.append(base)
    chunk_parts = []
    if proposal_id is not None:
        chunk_parts.append(f"approved_proposal:#{proposal_id}")
    if document_id is not None:
        chunk_parts.append(f"source_document_id:{document_id}")
    if document_filename:
        chunk_parts.append(f"source_document:{document_filename}")
    if evidence:
        chunk_parts.append(f"evidence:{evidence[:180]}")
    for ref in related_refs:
        if ref and ref not in "\n".join(lines):
            chunk_parts.append(ref)
    chunk = " | ".join(chunk_parts)
    if chunk and chunk not in base:
        lines.append(chunk)
    return "\n".join(lines).strip()


def _related_pending_contact_refs(
    client_id: int, *, email: str, name: str, exclude_proposal_id: int | None
) -> list[str]:
    """Collect alternate pending proposal references matching email (preferred) or name."""
    email_n = _norm_email(email)
    name_n = _norm_name(name)
    refs: list[str] = []
    seen: set[str] = set()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.proposed_value, p.raw_source_text, d.filename
            FROM client_extraction_proposals p
            LEFT JOIN client_documents d ON d.id = p.document_id
            WHERE p.client_id = ? AND p.status = 'Pending'
              AND (
                p.field_name IN ('client_contacts', 'client_contact_person')
                OR p.section = 'client_contacts'
              )
            ORDER BY p.id
            """,
            (client_id,),
        ).fetchall()
    for row in rows:
        pid = int(row["id"])
        if exclude_proposal_id is not None and pid == exclude_proposal_id:
            continue
        text = _blank(row["proposed_value"]) or _blank(row["raw_source_text"])
        people = parse_client_contact_people(text)
        matched = False
        matched_raw = ""
        for p in people:
            if email_n and _norm_email(p.get("email") or "") == email_n:
                matched = True
                matched_raw = p.get("raw") or text[:120]
                break
            if name_n and _norm_name(p.get("name") or "") == name_n and email_n:
                # name-only among multi-person only if email also present on candidate
                if _norm_email(p.get("email") or "") == email_n:
                    matched = True
                    matched_raw = p.get("raw") or text[:120]
                    break
        if not matched and email_n and email_n in text.lower():
            matched = True
            matched_raw = text[:120]
        if matched:
            key = f"related_pending:#{pid}"
            if key not in seen:
                seen.add(key)
                fname = _blank(row["filename"]) or "document"
                refs.append(
                    f"{key} ({fname})"
                    + (f" :: {matched_raw[:100]}" if matched_raw else "")
                )
    return refs


def apply_approved_client_contact_proposal(
    client_id: int,
    proposal: dict[str, Any],
    approved_value: str,
    *,
    user_id: int,
    user_name: str,
) -> str:
    """Create or merge one Client Contact from an approved single-person proposal."""
    ensure_client_contacts_schema()
    text = _blank(approved_value)
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and (
            parsed.get("name") or parsed.get("email")
        ):
            payload = parsed
    except Exception:
        payload = {}
    if not payload:
        people = parse_client_contact_people(text)
        if len(people) != 1:
            raise ValueError(
                "Client Contacts approval requires a single person. "
                "Split multi-person proposals first."
            )
        payload = people[0]

    name = _blank(payload.get("name"))
    title = _blank(payload.get("title"))
    email = _blank(payload.get("email"))
    phone = _blank(payload.get("phone"))
    role_type = _blank(payload.get("role_type"))
    notes_in = _blank(payload.get("notes"))
    proposal_id = int(proposal["id"]) if proposal.get("id") is not None else None
    document_id = (
        int(proposal["document_id"]) if proposal.get("document_id") else None
    )
    doc_name = ""
    if document_id:
        with get_connection() as conn:
            drow = conn.execute(
                "SELECT filename FROM client_documents WHERE id = ? AND client_id = ?",
                (document_id, client_id),
            ).fetchone()
            if drow:
                doc_name = _blank(drow["filename"])

    related = _related_pending_contact_refs(
        client_id,
        email=email,
        name=name,
        exclude_proposal_id=proposal_id,
    )
    evidence = _blank(proposal.get("raw_source_text")) or _blank(
        proposal.get("source_reference")
    ) or text[:180]

    # Prefer explicit name match to known contact id when approving over a test record
    force_contact_id = payload.get("_merge_contact_id")
    dup = None
    if force_contact_id is not None:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM client_contacts WHERE id = ? AND client_id = ?",
                (int(force_contact_id), client_id),
            ).fetchone()
            if row:
                dup = _row_to_view(row)
    if dup is None:
        dup = find_duplicate_client_contact(
            client_id, name=name, email=email, phone=phone
        )

    provenance_notes = _merge_provenance_notes(
        notes_in if not dup else _blank(dup.notes),
        proposal_id=proposal_id,
        document_id=document_id,
        document_filename=doc_name,
        related_refs=related,
        evidence=evidence,
    )
    if dup is not None and notes_in and notes_in not in provenance_notes:
        provenance_notes = _merge_provenance_notes(
            provenance_notes,
            proposal_id=None,
            document_id=None,
            document_filename="",
            related_refs=[notes_in],
            evidence="",
        )

    if dup is not None:
        # Do not clear existing role/title/phone with blanks from thinner sources
        updated = update_client_contact(
            client_id,
            dup.contact_id,
            ClientContactUpdate(
                name=name or None,
                title=title or None,
                email=email or None,
                phone=phone or None,
                role_type=role_type or None,
                notes=provenance_notes,
                active=True,
            ),
            user_id=user_id,
        )
        # Stamp primary source ids from this approval
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE client_contacts
                SET source_document_id = COALESCE(?, source_document_id),
                    source_proposal_id = COALESCE(?, source_proposal_id),
                    updated_at = ?, updated_by_name = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    document_id,
                    proposal_id,
                    _now(),
                    user_name,
                    dup.contact_id,
                    client_id,
                ),
            )
            conn.commit()
        return f"client_contacts.update:{updated.contact_id}"

    body = ClientContactCreate(
        name=name,
        title=title,
        email=email,
        phone=phone,
        role_type=role_type,
        notes=provenance_notes,
        source_document_id=document_id,
        source_proposal_id=proposal_id,
    )
    created = create_client_contact(
        client_id, body, user_id=user_id, allow_duplicate_flag=False
    )
    return f"client_contacts.create:{created.contact_id}"



def contacts_mentioned_in_text(
    client_id: int, text: str, *, user_id: int | None = None
) -> list[ClientContactView]:
    """Soft-link helper: find active Client Contacts named in operational text."""
    contacts = list_client_contacts(client_id, user_id=user_id, include_inactive=False)
    blob = _blank(text).lower()
    if not blob:
        return []
    found: list[ClientContactView] = []
    for c in contacts:
        name = _blank(c.name)
        if not name or len(name) < 3:
            continue
        if name.lower() in blob:
            found.append(c)
            continue
        # First + last token match
        parts = name.split()
        if len(parts) >= 2 and parts[0].lower() in blob and parts[-1].lower() in blob:
            found.append(c)
    return found


def _match_structured_contact(
    contacts: list[ClientContactView], *, name: str, email: str
) -> ClientContactView | None:
    email_n = _norm_email(email)
    name_n = _norm_name(name)
    # Prefer active contacts
    ordered = sorted(contacts, key=lambda c: (0 if c.active else 1, c.contact_id))
    if email_n:
        for c in ordered:
            if _norm_email(c.email) == email_n:
                return c
    if name_n:
        for c in ordered:
            if not c.active and email_n and _norm_email(c.email) != email_n:
                # Do not attach real emails to inactive test contacts by name alone
                continue
            if _norm_name(c.name) == name_n:
                return c
        for c in ordered:
            if not c.active:
                continue
            c_parts = _norm_name(c.name).split()
            n_parts = name_n.split()
            if (
                len(n_parts) == 1
                and c_parts
                and n_parts[0] == c_parts[0]
                and len(c_parts) >= 2
                and email_n
                and _norm_email(c.email) == email_n
            ):
                return c
            if (
                len(n_parts) == 1
                and c_parts
                and n_parts[0] == c_parts[0]
                and len(c_parts) >= 2
            ):
                return c
            if (
                len(c_parts) == 1
                and n_parts
                and c_parts[0] == n_parts[0]
                and len(n_parts) >= 2
            ):
                return c
    return None


def analyze_proposal_incorporation(
    client_id: int,
    proposal_id: int,
    *,
    user_id: int | None = None,
) -> Any:
    """Compare a multi-person contact proposal against approved Client Contacts.

    Suggestion only — never changes proposal status.
    """
    from models import (
        ContactFieldIncorporation,
        ContactPersonIncorporation,
        ProposalIncorporationAnalysis,
    )

    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_contacts_schema()

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
        approved_children = conn.execute(
            """
            SELECT id, proposed_value, approved_value, apply_target, status
            FROM client_extraction_proposals
            WHERE client_id = ? AND status = 'Approved'
              AND (
                source_locator LIKE ?
                OR source_reference LIKE ?
              )
            ORDER BY id
            """,
            (
                client_id,
                f"%parent_proposal:{proposal_id}%",
                f"%Split from proposal #{proposal_id}%",
            ),
        ).fetchall()

    contacts = list_client_contacts(client_id, user_id=user.id, include_inactive=True)
    section = _blank(row["section"])
    field_name = _blank(row["field_name"])
    # Contact incorporation only applies to Client Contacts proposals — not email bodies, etc.
    if section not in {"client_contacts", "client_profile"} or field_name not in {
        "client_contacts",
        "client_contact_person",
        "decision_makers",
    }:
        return ProposalIncorporationAnalysis(
            proposal_id=int(proposal_id),
            client_id=client_id,
            status=_blank(row["status"]) or "Pending",
            document_filename=_blank(row["document_filename"]),
            people=[],
            fully_incorporated=False,
            unresolved_summary=[],
            resolution_suggestion=(
                "Not a Client Contacts proposal — incorporation analysis does not apply."
            ),
            can_suggest_resolve=False,
            suggested_resolved_by_proposal_ids=[],
            suggested_resolved_client_contact_ids=[],
            note=(
                "Incorporation analysis is for Client Contacts proposals only. "
                "Email templates use Build Template review."
            ),
        )

    text = _blank(row["proposed_value"])
    people = parse_client_contact_people(text)
    person_models: list[ContactPersonIncorporation] = []
    unresolved: list[str] = []
    suggested_contact_ids: list[int] = []
    suggested_proposal_ids: list[int] = []

    for child in approved_children:
        suggested_proposal_ids.append(int(child["id"]))
        at = _blank(child["apply_target"])
        m = re.search(r"client_contacts\.(?:create|update):(\d+)", at)
        if m:
            suggested_contact_ids.append(int(m.group(1)))

    for p in people:
        matched = _match_structured_contact(
            contacts, name=p.get("name") or "", email=p.get("email") or ""
        )
        fields: list[ContactFieldIncorporation] = []
        name_v = _blank(p.get("name"))
        email_v = _blank(p.get("email"))
        title_v = _blank(p.get("title"))
        phone_v = _blank(p.get("phone"))
        role_v = _blank(p.get("role_type"))

        def add_field(field: str, value: str) -> None:
            if not value:
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value="",
                        status="not_present",
                        matched_contact_id=matched.contact_id if matched else None,
                        note="",
                    )
                )
                return
            if matched is None:
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value=value,
                        status="unresolved",
                        note="No matching Client Contact yet",
                    )
                )
                unresolved.append(f"{name_v or email_v}: {field}={value}")
                return
            existing = _blank(getattr(matched, field if field != "role" else "role_type", ""))
            if field == "role":
                existing = _blank(matched.role_type)
            if field == "name":
                # Fuller structured name counts as incorporated for shorter extract
                if _norm_name(value) == _norm_name(matched.name) or (
                    _norm_name(value) in _norm_name(matched.name)
                    or _norm_name(matched.name) in _norm_name(value)
                ):
                    fields.append(
                        ContactFieldIncorporation(
                            field=field,
                            value=value,
                            status="already_incorporated",
                            matched_contact_id=matched.contact_id,
                            note=f"Structured as {matched.name}",
                        )
                    )
                    return
            if field == "email" and _norm_email(value) == _norm_email(matched.email):
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value=value,
                        status="already_incorporated",
                        matched_contact_id=matched.contact_id,
                    )
                )
                return
            if field == "email" and value and matched.email and _norm_email(value) != _norm_email(matched.email):
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value=value,
                        status="unresolved",
                        matched_contact_id=matched.contact_id,
                        note=f"Conflicts with existing email '{matched.email}'",
                    )
                )
                unresolved.append(
                    f"{matched.name}: email '{value}' vs existing '{matched.email}'"
                )
                return
            if field == "email" and value and matched and not matched.email:
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value=value,
                        status="unresolved",
                        matched_contact_id=matched.contact_id,
                        note="Existing email is blank — enrichment not yet approved",
                    )
                )
                unresolved.append(
                    f"{matched.name}: email proposed '{value}' (not yet approved)"
                )
                return
            if field in {"title", "phone", "role"} and existing and _norm_name(existing) == _norm_name(value):
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value=value,
                        status="already_incorporated",
                        matched_contact_id=matched.contact_id,
                    )
                )
                return
            if field in {"title", "phone", "role"} and value and not existing:
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value=value,
                        status="unresolved",
                        matched_contact_id=matched.contact_id,
                        note=f"Existing {field} is blank — enrichment not yet approved",
                    )
                )
                unresolved.append(
                    f"{matched.name}: {field} proposed '{value}' (not yet approved)"
                )
                return
            if field in {"title", "phone", "role"} and value and existing and _norm_name(existing) != _norm_name(value):
                fields.append(
                    ContactFieldIncorporation(
                        field=field,
                        value=value,
                        status="unresolved",
                        matched_contact_id=matched.contact_id,
                        note=f"Conflicts with existing '{existing}'",
                    )
                )
                unresolved.append(
                    f"{matched.name}: {field} '{value}' vs existing '{existing}'"
                )
                return
            fields.append(
                ContactFieldIncorporation(
                    field=field,
                    value=value,
                    status="already_incorporated"
                    if matched
                    else "unresolved",
                    matched_contact_id=matched.contact_id if matched else None,
                )
            )

        add_field("name", name_v)
        add_field("email", email_v)
        add_field("title", title_v)
        add_field("phone", phone_v)
        add_field("role", role_v)

        meaningful = [f for f in fields if f.status != "not_present"]
        if not meaningful:
            overall = "not_incorporated"
        elif all(f.status == "already_incorporated" for f in meaningful):
            overall = "fully_incorporated"
        elif any(f.status == "already_incorporated" for f in meaningful):
            overall = "partially_incorporated"
        else:
            overall = "not_incorporated"

        if matched:
            suggested_contact_ids.append(matched.contact_id)

        person_models.append(
            ContactPersonIncorporation(
                name=name_v,
                email=email_v,
                fields=fields,
                overall=overall,
            )
        )

    fully = bool(person_models) and all(
        p.overall == "fully_incorporated" for p in person_models
    )
    # Parent split children path (#59)
    child_ids = sorted(set(suggested_proposal_ids))
    if not child_ids and _blank(dict(row).get("source_locator")).startswith(
        "split_into"
    ):
        # locator may be "split_into:4" without child ids — discover by reference
        with get_connection() as conn:
            kids = conn.execute(
                """
                SELECT id FROM client_extraction_proposals
                WHERE client_id = ? AND status = 'Approved'
                  AND (
                    source_locator LIKE ?
                    OR source_reference LIKE ?
                  )
                ORDER BY id
                """,
                (
                    client_id,
                    f"%parent_proposal:{proposal_id}%",
                    f"%Split from proposal #{proposal_id}%",
                ),
            ).fetchall()
            child_ids = [int(k["id"]) for k in kids]

    can_suggest = fully or (
        int(row["id"]) == proposal_id
        and bool(child_ids)
        and len(child_ids) >= len(people) >= 1
        and all(p.overall in {"fully_incorporated", "partially_incorporated"} for p in person_models)
        and not unresolved
    )
    # Stronger rule for split parent: all people have matching contacts and no unresolved fields
    if people and all(
        _match_structured_contact(contacts, name=p.get("name") or "", email=p.get("email") or "")
        for p in people
    ) and not unresolved:
        can_suggest = True

    suggestion = ""
    if can_suggest:
        if child_ids:
            suggestion = (
                f"All extracted contacts appear to have been handled through "
                f"approved proposals {', '.join('#'+str(i) for i in child_ids)}. "
                "You may Resolve / Incorporated — confirmation required."
            )
        else:
            suggestion = (
                "All extracted contacts appear to have been incorporated into "
                "structured Client Contacts. You may Resolve / Incorporated — "
                "confirmation required."
            )
    elif unresolved:
        suggestion = (
            "Not fully resolved — unresolved information remains. "
            "Do not resolve until enrichment is approved or explicitly discarded."
        )
    else:
        suggestion = "Some contacts are not yet fully incorporated."

    return ProposalIncorporationAnalysis(
        proposal_id=proposal_id,
        client_id=client_id,
        status=_blank(row["status"]),
        document_filename=_blank(row["document_filename"]),
        people=person_models,
        fully_incorporated=fully and not unresolved,
        unresolved_summary=unresolved,
        resolution_suggestion=suggestion,
        can_suggest_resolve=bool(can_suggest and not unresolved),
        suggested_resolved_by_proposal_ids=sorted(set(child_ids)),
        suggested_resolved_client_contact_ids=sorted(set(suggested_contact_ids)),
    )


def apply_contact_enrichment_proposal(
    client_id: int,
    proposal: dict[str, Any],
    approved_value: str,
    *,
    user_id: int,
    user_name: str,
) -> str:
    """Apply a single-field Client Contact enrichment from an approved proposal."""
    ensure_client_contacts_schema()
    text = _blank(approved_value)
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise ValueError("Enrichment proposal requires JSON payload.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Enrichment proposal requires JSON payload.")
    contact_id = int(payload.get("contact_id") or 0)
    field_name = _blank(payload.get("field_name"))
    new_value = _blank(payload.get("proposed_value"))
    if contact_id <= 0:
        raise ValueError("Enrichment requires contact_id.")
    if field_name not in {"title", "phone", "role_type", "notes", "name", "email"}:
        raise ValueError(f"Unsupported enrichment field: {field_name}")
    if field_name != "notes" and not new_value:
        raise ValueError("Enrichment proposed_value is required.")

    update_kwargs: dict[str, Any] = {field_name: new_value}
    # Append provenance into notes when enriching non-notes fields
    existing = None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_contacts WHERE id = ? AND client_id = ?",
            (contact_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Client contact not found.")
        existing = _row_to_view(row)

    if field_name != "notes":
        pid = int(proposal["id"]) if proposal.get("id") is not None else None
        evidence = _blank(payload.get("evidence")) or _blank(
            proposal.get("raw_source_text")
        )
        prov = _merge_provenance_notes(
            existing.notes,
            proposal_id=pid,
            document_id=(
                int(proposal["document_id"]) if proposal.get("document_id") else None
            ),
            document_filename="",
            related_refs=[
                f"enrichment:{field_name}={new_value}",
                f"prior_{field_name}={getattr(existing, field_name) or '—'}",
            ],
            evidence=evidence,
        )
        update_kwargs["notes"] = prov

    updated = update_client_contact(
        client_id,
        contact_id,
        ClientContactUpdate(**update_kwargs),
        user_id=user_id,
    )
    return f"client_contacts.enrich:{updated.contact_id}.{field_name}"


def create_contact_enrichment_proposal(
    client_id: int,
    *,
    contact_id: int,
    field_name: str,
    proposed_value: str,
    source_proposal_id: int | None = None,
    source_document_id: int | None = None,
    evidence: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    """Create a Pending enrichment proposal. Does not approve."""
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_contacts_schema()
    from client_knowledge_data import ensure_client_knowledge_schema

    ensure_client_knowledge_schema()
    if field_name not in {"title", "phone", "role_type", "notes"}:
        raise ValueError("Enrichment field must be title, phone, role_type, or notes.")
    proposed_value = _blank(proposed_value)
    if not proposed_value:
        raise ValueError("proposed_value is required.")

    contacts = list_client_contacts(client_id, user_id=user.id, include_inactive=True)
    contact = next((c for c in contacts if c.contact_id == contact_id), None)
    if contact is None:
        raise LookupError("Client contact not found.")
    existing_val = _blank(getattr(contact, field_name))
    if _norm_name(existing_val) == _norm_name(proposed_value):
        raise ValueError("Proposed value already matches the Client Contact.")

    # Avoid duplicate pending enrichment for same contact+field+value
    with get_connection() as conn:
        pending = conn.execute(
            """
            SELECT id, proposed_value FROM client_extraction_proposals
            WHERE client_id = ? AND status = 'Pending'
              AND field_name = 'client_contact_enrichment'
            """,
            (client_id,),
        ).fetchall()
        for row in pending:
            try:
                parsed = json.loads(row["proposed_value"] or "{}")
            except Exception:
                continue
            if (
                int(parsed.get("contact_id") or 0) == contact_id
                and _blank(parsed.get("field_name")) == field_name
                and _norm_name(parsed.get("proposed_value") or "")
                == _norm_name(proposed_value)
            ):
                return {
                    "proposal_id": int(row["id"]),
                    "created": False,
                    "message": "Matching Pending enrichment proposal already exists.",
                }

        doc_id = source_document_id
        if doc_id is None and source_proposal_id:
            prow = conn.execute(
                "SELECT document_id FROM client_extraction_proposals WHERE id = ? AND client_id = ?",
                (source_proposal_id, client_id),
            ).fetchone()
            if prow and prow["document_id"]:
                doc_id = int(prow["document_id"])

        payload = {
            "contact_id": contact_id,
            "field_name": field_name,
            "proposed_value": proposed_value,
            "existing_value": existing_val,
            "name": contact.name,
            "email": contact.email,
            "evidence": _blank(evidence),
            "source_proposal_id": source_proposal_id,
        }
        display = (
            f"{contact.name}: {field_name} "
            f"'{existing_val or '-'}' -> '{proposed_value}'"
        )
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO client_extraction_proposals (
                client_id, document_id, section, field_name,
                existing_value, proposed_value, source_reference,
                status, created_at, updated_at,
                document_type, raw_source_text, source_locator,
                confidence, classification, extracted_value
            ) VALUES (
                ?, ?, 'client_contacts', 'client_contact_enrichment',
                ?, ?, ?,
                'Pending', ?, ?,
                ?, ?, ?,
                'HIGH', 'POTENTIAL UPDATE', ?
            )
            """,
            (
                client_id,
                doc_id,
                existing_val,
                json.dumps(payload, ensure_ascii=False),
                f"Enrichment from proposal #{source_proposal_id}"
                if source_proposal_id
                else "Client Contact enrichment",
                now,
                now,
                "",
                _blank(evidence) or display,
                f"enrich_contact:{contact_id}",
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {
            "proposal_id": int(cur.lastrowid),
            "created": True,
            "display": display,
            "contact_id": contact_id,
            "field_name": field_name,
            "existing_value": existing_val,
            "proposed_value": proposed_value,
        }


def ensure_title_enrichment_proposals_from_source(
    client_id: int,
    source_proposal_id: int,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    From a pending multi-person proposal, create Pending title enrichment
    proposals for matched Client Contacts that have blank titles.
    Does not approve anything.
    """
    analysis = analyze_proposal_incorporation(
        client_id, source_proposal_id, user_id=user_id
    )
    created: list[dict[str, Any]] = []
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT document_id, proposed_value FROM client_extraction_proposals
            WHERE id = ? AND client_id = ?
            """,
            (source_proposal_id, client_id),
        ).fetchone()
    if not row:
        raise LookupError("Source proposal not found.")
    people = parse_client_contact_people(_blank(row["proposed_value"]))
    contacts = list_client_contacts(client_id, user_id=user_id, include_inactive=False)
    for p in people:
        title = _blank(p.get("title"))
        if not title:
            continue
        matched = _match_structured_contact(
            contacts, name=p.get("name") or "", email=p.get("email") or ""
        )
        if matched is None:
            continue
        if _blank(matched.title):
            result = create_contact_enrichment_proposal(
                client_id,
                contact_id=matched.contact_id,
                field_name="title",
                proposed_value=title,
                source_proposal_id=source_proposal_id,
                source_document_id=int(row["document_id"]) if row["document_id"] else None,
                evidence=p.get("raw") or title,
                user_id=user_id,
            )
            created.append(result)
    return created
