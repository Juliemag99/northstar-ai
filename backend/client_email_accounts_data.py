"""Client-scoped email sender accounts, assignments, signatures, and preview.

Configuration + preview readiness only. Does NOT send email, store passwords,
create provider drafts, or write CRM sales_events / activities.

Future (not implemented here): a successful provider send may create
client_sales_events with type = 'Email Sent' including client_id, company_id,
contact_id, sender_account_id, rep, recipient, subject, template_id, sent_at,
provider_message_id, and source/provenance. Do not write that event in this module.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from access import get_user_by_id
from client_setup_data import user_can_edit_client_setup
from db import get_connection
from models import (
    ClientEmailAccountAssignmentUpdate,
    ClientEmailAccountAssignmentView,
    ClientEmailAccountSuggestion,
    ClientEmailAccountUpdate,
    ClientEmailAccountView,
    ClientEmailPreviewRequest,
    ClientEmailPreviewResult,
    ClientEmailSignatureUpdate,
    ClientEmailSignatureView,
    EmailPreviewAppointmentOption,
    NorthStarUser,
    PlaceholderResolution,
)

PROVIDERS = ("Google", "Microsoft 365", "Other")
CONNECTION_STATUSES = (
    "not_connected",
    "connecting",
    "connected",
    "error",
    "revoked",
    "disconnected",
    "pending",
)

EMAIL_PLACEHOLDERS = (
    "[First Name]",
    "[Name]",  # legacy alias — resolves to CRM first name in email templates
    "[Company]",
    "[Appointment Date]",
    "[Appointment Time]",
    "[Appointment Contact]",
    "[Meeting With]",
    "[Revenue Specialist]",
    "[Revenue Specialist Signature]",
    "[Client Name]",
)

# Legacy tokens found in older templates → map to standard placeholders.
LEGACY_PLACEHOLDER_ALIASES = {
    "[day, date]": "[Appointment Date]",
    "[day date]": "[Appointment Date]",
    "[date]": "[Appointment Date]",
    "[time]": "[Appointment Time]",
    "[meeting with]": "[Meeting With]",
    "[northstar rep]": "[Meeting With]",
    "[carmeco participant]": "[Meeting With]",
}

# Documented for future send-event wiring — not written by this module.
FUTURE_EMAIL_SENT_EVENT_TYPE = "Email Sent"

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,80}\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", re.I)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\]]+", re.I)
_SIG_PLACEHOLDER = "[Revenue Specialist Signature]"


def _website_display(url_or_host: str) -> str:
    """Plain website text for email — no Markdown link syntax."""
    raw = _blank(url_or_host)
    if not raw:
        return ""
    # Prefer label when markdown already split
    host = re.sub(r"^https?://", "", raw, flags=re.I).strip().rstrip("/")
    if not host:
        return ""
    return host


def _normalize_email_plain_text(text: str) -> str:
    """Strip Markdown URL syntax; keep readable www./host text for plain email."""
    out = text or ""

    def _md_repl(m: re.Match[str]) -> str:
        label = _blank(m.group(1))
        url = _blank(m.group(2))
        if re.search(r"(?i)https?://|www\.", label):
            return _website_display(label)
        return _website_display(url) or _website_display(label)

    out = _MD_LINK_RE.sub(_md_repl, out)
    out = _BARE_URL_RE.sub(lambda m: _website_display(m.group(0)), out)
    return out


def _normalize_block_compare(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _strip_trailing_configured_signature(message_body: str, signature: str) -> str:
    """Remove a trailing copy of the configured signature from message text.

    Render-time safeguard when a template body still embeds the sender signature.
    """
    msg = (message_body or "").rstrip()
    sig = _normalize_email_plain_text(_blank(signature))
    if not msg or not sig:
        return msg
    msg_n = _normalize_block_compare(msg)
    sig_n = _normalize_block_compare(sig)
    if not sig_n or not msg_n.endswith(sig_n):
        return msg
    # Remove by trimming from the last occurrence of the signature's first line.
    first_line = sig.splitlines()[0].strip()
    if not first_line:
        return msg
    # Walk back line-by-line while the suffix matches the signature lines.
    msg_lines = msg.splitlines()
    sig_lines = [ln.rstrip() for ln in sig.splitlines()]
    if len(msg_lines) < len(sig_lines):
        return msg
    tail = msg_lines[-len(sig_lines) :]
    if _normalize_block_compare("\n".join(tail)) != sig_n:
        # Soft match: last non-empty lines equal sig lines ignoring blank gaps
        compact_msg = [ln.strip() for ln in msg_lines if ln.strip()]
        compact_sig = [ln.strip() for ln in sig_lines if ln.strip()]
        if len(compact_msg) < len(compact_sig):
            return msg
        if [x.lower() for x in compact_msg[-len(compact_sig) :]] != [
            x.lower() for x in compact_sig
        ]:
            return msg
        # Drop trailing compact signature lines from original lines.
        drop = 0
        need = list(reversed(compact_sig))
        for ln in reversed(msg_lines):
            if not need:
                break
            if not ln.strip():
                drop += 1
                continue
            if ln.strip().lower() == need[0].lower():
                need.pop(0)
                drop += 1
            else:
                break
        if need:
            return msg
        return "\n".join(msg_lines[:-drop]).rstrip() if drop else msg
    return "\n".join(msg_lines[: -len(sig_lines)]).rstrip()


def _compose_body_with_signature(
    message_body: str,
    signature: str,
    *,
    template_had_signature_placeholder: bool,
) -> tuple[str, str]:
    """Return (final_body, placement) with signature exactly once.

    placement: appended | placeholder | none
    """
    sig = _normalize_email_plain_text(_blank(signature))
    # Always strip a trailing embedded copy first (fallback safeguard).
    msg = _strip_trailing_configured_signature(message_body, sig)
    if template_had_signature_placeholder:
        # Placeholder path already injected sig into msg via apply_text.
        # If strip removed it, restore once at end.
        if sig and _normalize_block_compare(msg).endswith(_normalize_block_compare(sig)):
            return msg, "placeholder"
        if sig:
            return f"{msg}\n\n{sig}" if msg else sig, "placeholder"
        return msg, "none"
    if not sig:
        return msg, "none"
    if _normalize_block_compare(msg).endswith(_normalize_block_compare(sig)):
        return msg, "appended"
    return f"{msg}\n\n{sig}" if msg else sig, "appended"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _actor_from_user_id(user_id: int | None) -> NorthStarUser:
    """Authorize from an explicit session user id. Never fall back to Julie."""
    if user_id is None:
        raise PermissionError("Authentication required.")
    user = get_user_by_id(int(user_id))
    if user is None or not bool(user.active):
        raise PermissionError("User not found.")
    return user


def _require_access(user_id: int, client_id: int) -> None:
    from access import require_write_client_id

    require_write_client_id(client_id, user_id=user_id)


def _require_edit(user_id: int, client_id: int) -> None:
    if not user_can_edit_client_setup(user_id, client_id):
        raise PermissionError("Not authorized to edit client email accounts.")


def ensure_client_email_accounts_schema(conn=None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_email_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                email_address TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'Other',
                connection_status TEXT NOT NULL DEFAULT 'not_connected',
                active INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                provider_connection_ref TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_client_email_accounts_client
                ON client_email_accounts(client_id, active, is_default);

            CREATE TABLE IF NOT EXISTS client_email_account_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_email_account_id INTEGER NOT NULL,
                user_id INTEGER,
                source_rep_name TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                is_default_for_rep INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_email_account_id)
                    REFERENCES client_email_accounts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_client_email_account_assign_acct
                ON client_email_account_assignments(client_email_account_id, active);

            CREATE TABLE IF NOT EXISTS client_email_signatures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                email_account_id INTEGER,
                rep_user_id INTEGER,
                source_rep_name TEXT NOT NULL DEFAULT '',
                signature_name TEXT NOT NULL DEFAULT '',
                signature_body TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (email_account_id)
                    REFERENCES client_email_accounts(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_client_email_signatures_client
                ON client_email_signatures(client_id, active, is_default);
            """
        )
        try:
            from email_oauth_credentials import ensure_email_oauth_credentials_schema

            ensure_email_oauth_credentials_schema(conn)
        except Exception:
            pass
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _norm_provider(value: str) -> str:
    raw = _blank(value) or "Other"
    for p in PROVIDERS:
        if raw.lower() == p.lower():
            return p
    # Soft aliases
    low = raw.lower()
    if "google" in low or "gmail" in low:
        return "Google"
    if "microsoft" in low or "outlook" in low or "365" in low or "o365" in low:
        return "Microsoft 365"
    return "Other"


def _norm_connection_status(value: str) -> str:
    raw = _blank(value).lower().replace(" ", "_") or "not_connected"
    aliases = {
        "not_connected": "not_connected",
        "disconnected": "disconnected",
        "pending": "pending",
        "connecting": "connecting",
        "connected": "connected",
        "error": "error",
        "revoked": "revoked",
        "none": "not_connected",
        "stub": "not_connected",
    }
    return aliases.get(raw, "not_connected")


def _extract_email(text: str) -> str:
    m = _EMAIL_RE.search(_blank(text))
    return m.group(0) if m else ""


def _assignment_view(row: Any) -> ClientEmailAccountAssignmentView:
    d = dict(row)
    return ClientEmailAccountAssignmentView(
        assignment_id=int(d["id"]),
        client_email_account_id=int(d["client_email_account_id"]),
        user_id=int(d["user_id"]) if d.get("user_id") is not None else None,
        source_rep_name=_blank(d.get("source_rep_name")),
        active=bool(d.get("active")),
        is_default_for_rep=bool(d.get("is_default_for_rep")),
        created_at=_blank(d.get("created_at")),
        updated_at=_blank(d.get("updated_at")),
    )


def _signature_view(row: Any) -> ClientEmailSignatureView:
    d = dict(row)
    return ClientEmailSignatureView(
        signature_id=int(d["id"]),
        client_id=int(d["client_id"]),
        email_account_id=(
            int(d["email_account_id"]) if d.get("email_account_id") is not None else None
        ),
        rep_user_id=int(d["rep_user_id"]) if d.get("rep_user_id") is not None else None,
        source_rep_name=_blank(d.get("source_rep_name")),
        signature_name=_blank(d.get("signature_name")),
        signature_body=_blank(d.get("signature_body")),
        active=bool(d.get("active")),
        is_default=bool(d.get("is_default")),
        created_at=_blank(d.get("created_at")),
        updated_at=_blank(d.get("updated_at")),
    )


def _load_assignments(conn, account_id: int) -> list[ClientEmailAccountAssignmentView]:
    rows = conn.execute(
        """
        SELECT * FROM client_email_account_assignments
        WHERE client_email_account_id = ?
        ORDER BY active DESC, is_default_for_rep DESC, id ASC
        """,
        (account_id,),
    ).fetchall()
    return [_assignment_view(r) for r in rows]


def _account_view(conn, row: Any) -> ClientEmailAccountView:
    d = dict(row)
    account_id = int(d["id"])
    return ClientEmailAccountView(
        account_id=account_id,
        client_id=int(d["client_id"]),
        email_address=_blank(d.get("email_address")),
        display_name=_blank(d.get("display_name")),
        provider=_blank(d.get("provider")) or "Other",
        connection_status=_blank(d.get("connection_status")) or "not_connected",
        active=bool(d.get("active")),
        is_default=bool(d.get("is_default")),
        provider_connection_ref=_blank(d.get("provider_connection_ref")),
        created_at=_blank(d.get("created_at")),
        updated_at=_blank(d.get("updated_at")),
        created_by=_blank(d.get("created_by")),
        updated_by=_blank(d.get("updated_by")),
        assignments=_load_assignments(conn, account_id),
    )


def list_email_accounts(
    client_id: int, *, user_id: int | None = None, include_inactive: bool = True
) -> list[ClientEmailAccountView]:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_email_accounts_schema()
    with get_connection() as conn:
        if include_inactive:
            rows = conn.execute(
                """
                SELECT * FROM client_email_accounts
                WHERE client_id = ?
                ORDER BY active DESC, is_default DESC, email_address COLLATE NOCASE
                """,
                (client_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM client_email_accounts
                WHERE client_id = ? AND active = 1
                ORDER BY is_default DESC, email_address COLLATE NOCASE
                """,
                (client_id,),
            ).fetchall()
        return [_account_view(conn, r) for r in rows]


def get_email_account_bound(client_id: int, account_id: int) -> ClientEmailAccountView:
    """Load an account by client + id only. No user lookup (OAuth callback)."""
    ensure_client_email_accounts_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_email_accounts WHERE id = ? AND client_id = ?",
            (int(account_id), int(client_id)),
        ).fetchone()
        if not row:
            raise LookupError("Sender account not found for this client.")
        return _account_view(conn, row)


def get_email_account(
    client_id: int, account_id: int, *, user_id: int | None = None
) -> ClientEmailAccountView:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_email_accounts_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_email_accounts WHERE id = ? AND client_id = ?",
            (account_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Sender account not found for this client.")
        return _account_view(conn, row)


def suggest_email_account_setup(
    client_id: int, *, user_id: int | None = None
) -> ClientEmailAccountSuggestion:
    """Suggest sender + rep from approved Client Operations. Never creates rows."""
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_email_accounts_schema()

    from client_knowledge_data import list_approved_client_operations

    suggested_email = ""
    suggested_rep = ""
    email_source = ""
    rep_source = ""
    for it in list_approved_client_operations(client_id, user_id=user.id):
        fk = _blank(it.get("field_key"))
        content = _blank(it.get("content"))
        if fk == "northstar_client_email" and not suggested_email:
            suggested_email = _extract_email(content) or content
            email_source = "Approved Client Operations · NorthStar Client Email"
        elif fk == "northstar_revenue_specialist" and not suggested_rep:
            suggested_rep = content
            rep_source = "Approved Client Operations · NorthStar Revenue Specialist"

    existing = list_email_accounts(client_id, user_id=user.id)
    already = any(
        _blank(a.email_address).lower() == _blank(suggested_email).lower()
        for a in existing
        if suggested_email
    )

    # Never invent a user_id — only exact active full_name match.
    mapped_user_id = None
    if suggested_rep:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id FROM users
                WHERE active = 1 AND lower(trim(full_name)) = lower(trim(?))
                """,
                (suggested_rep,),
            ).fetchall()
            if len(rows) == 1:
                mapped_user_id = int(rows[0]["id"])

    notes: list[str] = []
    if suggested_email and not already:
        notes.append(
            "Suggested sender is not configured yet — create only after user approval."
        )
    if suggested_email and already:
        notes.append("Suggested sender email already exists for this client.")
    if suggested_rep and mapped_user_id is None:
        notes.append(
            "Revenue Specialist is not mapped to a NorthStar user — store source_rep_name only."
        )
    if not suggested_email and not suggested_rep:
        notes.append("No approved Client Operations email/rep suggestion available.")

    return ClientEmailAccountSuggestion(
        client_id=client_id,
        suggested_email_address=_blank(suggested_email),
        suggested_display_name="",  # never auto-choose display name
        suggested_provider="Other",
        suggested_source_rep_name=_blank(suggested_rep),
        suggested_user_id=mapped_user_id,
        email_suggestion_source=email_source,
        rep_suggestion_source=rep_source,
        already_configured=already,
        auto_created=False,
        notes=notes,
    )


def upsert_email_account(
    client_id: int,
    body: ClientEmailAccountUpdate,
    *,
    account_id: int | None = None,
    user_id: int | None = None,
) -> ClientEmailAccountView:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)

    email = _blank(body.email_address)
    if not email or not _EMAIL_RE.search(email) or " " in email or email.count("@") != 1:
        raise ValueError("A valid email address is required.")
    email = _extract_email(email) or email
    # Reject credential-like payloads in display name / connection ref
    for field_label, field_val in (
        ("display_name", body.display_name),
        ("provider_connection_ref", body.provider_connection_ref),
    ):
        raw = _blank(field_val)
        if re.search(r"(?i)\b(password|passwd|pwd|secret|api[_\s-]?key|token)\b", raw):
            raise ValueError(
                f"Refusing to store credential-like content in {field_label}."
            )

    provider = _norm_provider(body.provider)
    # Never accept connected status from create/edit without Connect flow.
    status = _norm_connection_status(body.connection_status)
    if status == "connected" and not _blank(body.provider_connection_ref):
        # Still allow if ref already exists on update; otherwise force not_connected.
        pass
    display_name = _blank(body.display_name)
    active = bool(body.active)
    is_default = bool(body.is_default)
    # Never store secrets in provider_connection_ref — opaque id only.
    conn_ref = _blank(body.provider_connection_ref)
    if len(conn_ref) > 200:
        raise ValueError("provider_connection_ref is too long.")

    ensure_client_email_accounts_schema()
    now = _now()
    actor = _blank(user.full_name) or _blank(user.email)

    with get_connection() as conn:
        if account_id:
            row = conn.execute(
                "SELECT * FROM client_email_accounts WHERE id = ? AND client_id = ?",
                (account_id, client_id),
            ).fetchone()
            if not row:
                raise LookupError("Sender account not found for this client.")
            # Preserve connection_status unless explicitly changed to non-connected
            # via connect stub / deactivate. Edit form may pass current status.
            existing_status = _blank(row["connection_status"]) or "not_connected"
            if status == "connected" and not _blank(row["provider_connection_ref"]) and not conn_ref:
                status = existing_status if existing_status != "connected" else "not_connected"
            conn.execute(
                """
                UPDATE client_email_accounts SET
                    email_address = ?, display_name = ?, provider = ?,
                    connection_status = ?, active = ?, is_default = ?,
                    provider_connection_ref = COALESCE(NULLIF(?, ''), provider_connection_ref),
                    updated_at = ?, updated_by = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    email,
                    display_name,
                    provider,
                    status,
                    1 if active else 0,
                    1 if is_default else 0,
                    conn_ref,
                    now,
                    actor,
                    account_id,
                    client_id,
                ),
            )
            new_id = account_id
        else:
            # New accounts always start not_connected — Connect is separate.
            status = "not_connected"
            conn_ref = ""
            cur = conn.execute(
                """
                INSERT INTO client_email_accounts (
                    client_id, email_address, display_name, provider,
                    connection_status, active, is_default, provider_connection_ref,
                    created_at, updated_at, created_by, updated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    email,
                    display_name,
                    provider,
                    status,
                    1 if active else 0,
                    1 if is_default else 0,
                    conn_ref,
                    now,
                    now,
                    actor,
                    actor,
                ),
            )
            new_id = int(cur.lastrowid)

        if is_default:
            conn.execute(
                """
                UPDATE client_email_accounts
                SET is_default = 0, updated_at = ?, updated_by = ?
                WHERE client_id = ? AND id != ?
                """,
                (now, actor, client_id, new_id),
            )
            conn.execute(
                "UPDATE client_email_accounts SET is_default = 1 WHERE id = ? AND client_id = ?",
                (new_id, client_id),
            )

        row = conn.execute(
            "SELECT * FROM client_email_accounts WHERE id = ? AND client_id = ?",
            (new_id, client_id),
        ).fetchone()
        return _account_view(conn, row)


def deactivate_email_account(
    client_id: int, account_id: int, *, user_id: int | None = None
) -> ClientEmailAccountView:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_email_accounts_schema()
    now = _now()
    actor = _blank(user.full_name) or _blank(user.email)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_email_accounts WHERE id = ? AND client_id = ?",
            (account_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Sender account not found for this client.")
        conn.execute(
            """
            UPDATE client_email_accounts
            SET active = 0, is_default = 0, updated_at = ?, updated_by = ?
            WHERE id = ? AND client_id = ?
            """,
            (now, actor, account_id, client_id),
        )
        row = conn.execute(
            "SELECT * FROM client_email_accounts WHERE id = ? AND client_id = ?",
            (account_id, client_id),
        ).fetchone()
        return _account_view(conn, row)


def connect_email_account_stub(
    client_id: int, account_id: int, *, user_id: int | None = None
) -> dict[str, Any]:
    """Architecture stub — provider OAuth not implemented. Does not store secrets."""
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    account = get_email_account(client_id, account_id, user_id=user.id)
    return {
        "account_id": account.account_id,
        "client_id": client_id,
        "email_address": account.email_address,
        "provider": account.provider,
        "connection_status": account.connection_status,
        "connected": account.connection_status == "connected",
        "message": (
            "Connect Account is not available yet. "
            "Provider OAuth will store only a provider_connection_ref — never passwords or tokens."
        ),
        "available": False,
    }


def upsert_email_account_assignment(
    client_id: int,
    account_id: int,
    body: ClientEmailAccountAssignmentUpdate,
    *,
    assignment_id: int | None = None,
    user_id: int | None = None,
) -> ClientEmailAccountAssignmentView:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_email_accounts_schema()

    mapped_user_id = body.user_id
    source_rep = _blank(body.source_rep_name)
    if mapped_user_id is not None:
        u = get_user_by_id(int(mapped_user_id))
        if u is None or not u.active:
            raise ValueError("Assigned user_id is not an active NorthStar user.")
        if not source_rep:
            source_rep = _blank(u.full_name)
    if mapped_user_id is None and not source_rep:
        raise ValueError("Provide user_id or source_rep_name for the Revenue Specialist.")

    now = _now()
    with get_connection() as conn:
        acct = conn.execute(
            "SELECT id FROM client_email_accounts WHERE id = ? AND client_id = ?",
            (account_id, client_id),
        ).fetchone()
        if not acct:
            raise LookupError("Sender account not found for this client.")

        if assignment_id:
            row = conn.execute(
                """
                SELECT a.* FROM client_email_account_assignments a
                JOIN client_email_accounts e ON e.id = a.client_email_account_id
                WHERE a.id = ? AND e.client_id = ? AND e.id = ?
                """,
                (assignment_id, client_id, account_id),
            ).fetchone()
            if not row:
                raise LookupError("Assignment not found for this account.")
            conn.execute(
                """
                UPDATE client_email_account_assignments SET
                    user_id = ?, source_rep_name = ?, active = ?,
                    is_default_for_rep = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    mapped_user_id,
                    source_rep,
                    1 if body.active else 0,
                    1 if body.is_default_for_rep else 0,
                    now,
                    assignment_id,
                ),
            )
            new_id = assignment_id
        else:
            cur = conn.execute(
                """
                INSERT INTO client_email_account_assignments (
                    client_email_account_id, user_id, source_rep_name,
                    active, is_default_for_rep, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    mapped_user_id,
                    source_rep,
                    1 if body.active else 0,
                    1 if body.is_default_for_rep else 0,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid)

        if body.is_default_for_rep:
            # Clear other defaults for same rep identity on this account
            conn.execute(
                """
                UPDATE client_email_account_assignments
                SET is_default_for_rep = 0, updated_at = ?
                WHERE client_email_account_id = ? AND id != ?
                  AND (
                    (? IS NOT NULL AND user_id = ?)
                    OR (lower(trim(source_rep_name)) = lower(trim(?)))
                  )
                """,
                (now, account_id, new_id, mapped_user_id, mapped_user_id, source_rep),
            )
            conn.execute(
                "UPDATE client_email_account_assignments SET is_default_for_rep = 1 WHERE id = ?",
                (new_id,),
            )

        row = conn.execute(
            "SELECT * FROM client_email_account_assignments WHERE id = ?",
            (new_id,),
        ).fetchone()
        return _assignment_view(row)


def list_email_signatures(
    client_id: int, *, user_id: int | None = None, include_inactive: bool = True
) -> list[ClientEmailSignatureView]:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_email_accounts_schema()
    with get_connection() as conn:
        if include_inactive:
            rows = conn.execute(
                """
                SELECT * FROM client_email_signatures
                WHERE client_id = ?
                ORDER BY active DESC, is_default DESC, signature_name COLLATE NOCASE
                """,
                (client_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM client_email_signatures
                WHERE client_id = ? AND active = 1
                ORDER BY is_default DESC, signature_name COLLATE NOCASE
                """,
                (client_id,),
            ).fetchall()
        return [_signature_view(r) for r in rows]


def upsert_email_signature(
    client_id: int,
    body: ClientEmailSignatureUpdate,
    *,
    signature_id: int | None = None,
    user_id: int | None = None,
) -> ClientEmailSignatureView:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_email_accounts_schema()

    name = _blank(body.signature_name) or "Signature"
    sig_body = _normalize_email_plain_text(
        body.signature_body if body.signature_body is not None else ""
    )
    # Never treat signature as credential vault
    if re.search(r"(?i)\b(password|passwd|api[_\s-]?key)\s*[:=]", sig_body or ""):
        raise ValueError("Signature body looks like it contains credentials — refused.")

    email_account_id = body.email_account_id
    now = _now()
    with get_connection() as conn:
        if email_account_id is not None:
            acct = conn.execute(
                "SELECT id FROM client_email_accounts WHERE id = ? AND client_id = ?",
                (email_account_id, client_id),
            ).fetchone()
            if not acct:
                raise LookupError("Sender account not found for this client.")

        if signature_id:
            row = conn.execute(
                "SELECT * FROM client_email_signatures WHERE id = ? AND client_id = ?",
                (signature_id, client_id),
            ).fetchone()
            if not row:
                raise LookupError("Signature not found for this client.")
            conn.execute(
                """
                UPDATE client_email_signatures SET
                    email_account_id = ?, rep_user_id = ?, source_rep_name = ?,
                    signature_name = ?, signature_body = ?, active = ?, is_default = ?,
                    updated_at = ?
                WHERE id = ? AND client_id = ?
                """,
                (
                    email_account_id,
                    body.rep_user_id,
                    _blank(body.source_rep_name),
                    name,
                    sig_body,
                    1 if body.active else 0,
                    1 if body.is_default else 0,
                    now,
                    signature_id,
                    client_id,
                ),
            )
            new_id = signature_id
        else:
            cur = conn.execute(
                """
                INSERT INTO client_email_signatures (
                    client_id, email_account_id, rep_user_id, source_rep_name,
                    signature_name, signature_body, active, is_default,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    email_account_id,
                    body.rep_user_id,
                    _blank(body.source_rep_name),
                    name,
                    sig_body,
                    1 if body.active else 0,
                    1 if body.is_default else 0,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid)

        if body.is_default:
            conn.execute(
                """
                UPDATE client_email_signatures
                SET is_default = 0, updated_at = ?
                WHERE client_id = ? AND id != ?
                  AND (
                    (? IS NOT NULL AND email_account_id = ?)
                    OR (? IS NULL AND email_account_id IS NULL)
                  )
                """,
                (now, client_id, new_id, email_account_id, email_account_id, email_account_id),
            )
            conn.execute(
                "UPDATE client_email_signatures SET is_default = 1 WHERE id = ? AND client_id = ?",
                (new_id, client_id),
            )

        row = conn.execute(
            "SELECT * FROM client_email_signatures WHERE id = ? AND client_id = ?",
            (new_id, client_id),
        ).fetchone()
        return _signature_view(row)


def deactivate_email_signature(
    client_id: int, signature_id: int, *, user_id: int | None = None
) -> ClientEmailSignatureView:
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_edit(user.id, client_id)
    ensure_client_email_accounts_schema()
    now = _now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_email_signatures WHERE id = ? AND client_id = ?",
            (signature_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Signature not found for this client.")
        conn.execute(
            """
            UPDATE client_email_signatures
            SET active = 0, is_default = 0, updated_at = ?
            WHERE id = ? AND client_id = ?
            """,
            (now, signature_id, client_id),
        )
        row = conn.execute(
            "SELECT * FROM client_email_signatures WHERE id = ? AND client_id = ?",
            (signature_id, client_id),
        ).fetchone()
        return _signature_view(row)


def list_preview_contacts(
    client_id: int, *, user_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """CRM prospect contacts for the client that have an email (read-only)."""
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    with get_connection() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        name_expr = "trim(coalesce(ct.first_name,'') || ' ' || coalesce(ct.last_name,''))"
        if "full_name" in cols:
            name_expr = "coalesce(nullif(trim(ct.full_name),''), " + name_expr + ")"
        elif "name" in cols:
            name_expr = "coalesce(nullif(trim(ct.name),''), " + name_expr + ")"
        rows = conn.execute(
            f"""
            SELECT ct.id AS contact_id,
                   {name_expr} AS contact_name,
                   coalesce(ct.first_name, '') AS first_name,
                   ct.email AS email,
                   c.id AS company_id,
                   c.company_name AS company_name
            FROM contacts ct
            JOIN client_company_relationships ccr ON ccr.company_id = ct.company_id
            JOIN companies c ON c.id = ct.company_id
            WHERE ccr.client_id = ?
              AND ct.email IS NOT NULL AND trim(ct.email) != ''
            ORDER BY c.company_name COLLATE NOCASE, contact_name COLLATE NOCASE
            LIMIT ?
            """,
            (client_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [
            {
                "contact_id": int(r["contact_id"]),
                "contact_name": _blank(r["contact_name"]),
                "first_name": _blank(r["first_name"]),
                "email": _blank(r["email"]),
                "company_id": int(r["company_id"]),
                "company_name": _blank(r["company_name"]),
            }
            for r in rows
        ]


def _crm_first_name(first_name: str, full_contact_name: str) -> str:
    """First-name greeting only. Does not invent; empty if unknown."""
    fn = _blank(first_name)
    if fn:
        return fn.split()[0] if fn else ""
    # Fallback: first token of stored display name (still first-name only).
    parts = _blank(full_contact_name).split()
    return parts[0] if parts else ""


def _clean_meeting_with(raw: str) -> str:
    """Normalize appointment participant text; never invent a person."""
    val = _blank(raw)
    if not val:
        return ""
    # Drop common import prefixes that are not the meeting participant name.
    low = val.lower()
    for prefix in ("rev spec/", "revspec/", "revenue specialist/", "rs/"):
        if low.startswith(prefix):
            # Rev Spec text is setter attribution, not Carmeco meeting host.
            return ""
    return val


_MONTH_ABBR = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_EXCLUDED_APPOINTMENT_EVENT_TYPES = {
    "send information",
    "rfq",
    "email sent",
    "send e-mail",
    "send email",
}


def _format_preview_date(raw: str) -> str:
    """Readable date for labels/placeholders. Empty if not reliably parseable."""
    text = _blank(raw)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return ""
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if month < 1 or month > 12 or day < 1 or day > 31:
        return ""
    return f"{_MONTH_ABBR[month]} {day}, {year}"


def _format_preview_time(raw: str, timezone: str = "") -> str:
    """Readable time for labels/placeholders. Empty if not reliably parseable."""
    text = _blank(raw)
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$", text)
    if not m:
        return ""
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute > 59:
        return ""
    suffix = (m.group(3) or "").upper()
    if suffix:
        if hour < 1 or hour > 12:
            return ""
        hour12 = hour
        ampm = suffix
    else:
        if hour > 23:
            return ""
        ampm = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
    out = f"{hour12}:{minute:02d} {ampm}"
    tz = _blank(timezone)
    return f"{out} {tz}".strip() if tz else out


def _is_appointment_context_event(event_type: str, event_family: str, meeting_type: str) -> bool:
    """True for appointment/meeting context; excludes Send Information / RFQ / Email Sent."""
    et = _blank(event_type).lower()
    ef = _blank(event_family).lower()
    mt = _blank(meeting_type).lower()
    if et in _EXCLUDED_APPOINTMENT_EVENT_TYPES:
        return False
    if et.startswith("email") or et.startswith("send information") or et == "rfq":
        return False
    if et.startswith("appointment"):
        return True
    if "appoint" in ef:
        return True
    if et in {"meeting", "site visit"} or "meeting" in et or "site visit" in et:
        return True
    if mt in {
        "site visit",
        "google meet",
        "microsoft teams",
        "teams",
        "zoom",
        "phone",
        "call",
        "in person",
        "in-person",
    }:
        return True
    return False


def _build_appointment_label(
    *,
    date_raw: str,
    time_raw: str,
    timezone: str,
    event_type: str,
    meeting_type: str,
    source_date_time_text: str,
) -> str:
    date_fmt = _format_preview_date(date_raw)
    time_fmt = _format_preview_time(time_raw, timezone)
    bits: list[str] = []
    if date_fmt:
        bits.append(date_fmt)
    if time_fmt:
        bits.append(time_fmt)
    mt = _blank(meeting_type)
    et = _blank(event_type)
    if mt:
        bits.append(mt)
    et_low = et.lower()
    if "reschedul" in et_low:
        bits.append("Rescheduled")
    elif "completed" in et_low:
        bits.append("Completed")
    elif not mt and et:
        bits.append(et)
    if bits:
        return " · ".join(bits)
    # Preserve original source text when parsed date/time is incomplete.
    return _blank(source_date_time_text) or et or "Appointment"


def list_preview_appointments(
    client_id: int,
    contact_id: int,
    *,
    user_id: int | None = None,
    limit: int = 50,
) -> list[EmailPreviewAppointmentOption]:
    """Appointments relevant to a CRM contact for Preview Email (read-only).

    Primary source: client_sales_events (imported Appointment Scheduled/Rescheduled/etc.).
    """
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_email_accounts_schema()
    out: list[EmailPreviewAppointmentOption] = []
    lim = max(1, min(int(limit), 200))

    with get_connection() as conn:
        crow = conn.execute(
            """
            SELECT ct.id AS contact_id,
                   lower(trim(coalesce(ct.email,''))) AS email,
                   ct.company_id AS company_id,
                   trim(coalesce(ct.first_name,'') || ' ' || coalesce(ct.last_name,'')) AS contact_name
            FROM contacts ct
            JOIN client_company_relationships ccr ON ccr.company_id = ct.company_id
            WHERE ccr.client_id = ? AND ct.id = ?
            """,
            (client_id, contact_id),
        ).fetchone()
        if not crow:
            raise LookupError("Contact not found for this client.")
        email = _blank(crow["email"])
        company_id = int(crow["company_id"]) if crow["company_id"] is not None else None

        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except Exception:
            tables = set()

        if "client_sales_events" in tables:
            # Prefer contact-linked rows for this client only (never other clients).
            se_rows = conn.execute(
                """
                SELECT id, event_type, event_family, event_date, event_time, timezone,
                       meeting_type, company_name, contact_name, company_id, contact_id,
                       source_rev_spec_text, source_date_time_text
                FROM client_sales_events
                WHERE client_id = ?
                  AND contact_id = ?
                ORDER BY event_date DESC, id DESC
                LIMIT ?
                """,
                (client_id, contact_id, max(lim * 4, 80)),
            ).fetchall()
            for r in se_rows:
                d = dict(r)
                et = _blank(d.get("event_type"))
                ef = _blank(d.get("event_family"))
                mt = _blank(d.get("meeting_type"))
                if not _is_appointment_context_event(et, ef, mt):
                    continue
                date_raw = _blank(d.get("event_date"))
                time_raw = _blank(d.get("event_time"))
                tz = _blank(d.get("timezone")) or "CDT"
                source_txt = _blank(d.get("source_date_time_text"))
                date_fmt = _format_preview_date(date_raw)
                time_fmt = _format_preview_time(time_raw, tz) if time_raw else ""
                mw = _clean_meeting_with(_blank(d.get("source_rev_spec_text")))
                out.append(
                    EmailPreviewAppointmentOption(
                        source="sales_event",
                        event_id=int(d["id"]),
                        label=_build_appointment_label(
                            date_raw=date_raw,
                            time_raw=time_raw,
                            timezone=tz,
                            event_type=et,
                            meeting_type=mt,
                            source_date_time_text=source_txt,
                        ),
                        appointment_date=date_fmt,
                        appointment_time=time_fmt,
                        appointment_date_raw=date_raw,
                        appointment_time_raw=time_raw,
                        company_name=_blank(d.get("company_name")),
                        contact_name=_blank(d.get("contact_name")),
                        meeting_with=mw,
                        event_type=et or "Appointment",
                        meeting_type=mt,
                        timezone=tz,
                        source_date_time_text=source_txt,
                        company_id=int(d["company_id"])
                        if d.get("company_id") is not None
                        else None,
                        contact_id=int(d["contact_id"])
                        if d.get("contact_id") is not None
                        else None,
                    )
                )
                if len(out) >= lim:
                    break

        if "client_appointment_events" in tables and len(out) < lim:
            ae_rows = conn.execute(
                """
                SELECT id, event_type, appointment_date, appointment_time, company_name,
                       contact_name, revenue_specialist, email, company_id, contact_id
                FROM client_appointment_events
                WHERE client_id = ?
                  AND (
                    contact_id = ?
                    OR (
                      ? IS NOT NULL AND company_id = ?
                      AND (
                        (? != '' AND lower(trim(coalesce(email,''))) = ?)
                        OR (
                          ? != '' AND lower(trim(coalesce(contact_name,'')))
                            = lower(trim(?))
                        )
                      )
                    )
                  )
                ORDER BY appointment_date DESC, id DESC
                LIMIT ?
                """,
                (
                    client_id,
                    contact_id,
                    company_id,
                    company_id,
                    email,
                    email,
                    _blank(crow["contact_name"]),
                    _blank(crow["contact_name"]),
                    lim,
                ),
            ).fetchall()
            seen_ae = {(o.source, o.event_id) for o in out}
            for r in ae_rows:
                d = dict(r)
                key = ("appointment_event", int(d["id"]))
                if key in seen_ae:
                    continue
                date_raw = _blank(d.get("appointment_date"))
                time_raw = _blank(d.get("appointment_time"))
                et = _blank(d.get("event_type")) or "Appointment"
                tz = "CDT"
                date_fmt = _format_preview_date(date_raw)
                time_fmt = _format_preview_time(time_raw, tz) if time_raw else ""
                mw = _clean_meeting_with(_blank(d.get("revenue_specialist")))
                out.append(
                    EmailPreviewAppointmentOption(
                        source="appointment_event",
                        event_id=int(d["id"]),
                        label=_build_appointment_label(
                            date_raw=date_raw,
                            time_raw=time_raw,
                            timezone=tz,
                            event_type=et,
                            meeting_type="",
                            source_date_time_text="",
                        ),
                        appointment_date=date_fmt,
                        appointment_time=time_fmt,
                        appointment_date_raw=date_raw,
                        appointment_time_raw=time_raw,
                        company_name=_blank(d.get("company_name")),
                        contact_name=_blank(d.get("contact_name")),
                        meeting_with=mw,
                        event_type=et,
                        meeting_type="",
                        timezone=tz,
                        source_date_time_text="",
                        company_id=int(d["company_id"])
                        if d.get("company_id") is not None
                        else None,
                        contact_id=int(d["contact_id"])
                        if d.get("contact_id") is not None
                        else None,
                    )
                )

    out.sort(
        key=lambda a: (a.appointment_date_raw or "", a.event_id),
        reverse=True,
    )
    return out[:lim]


def _load_appointment_context(
    conn,
    client_id: int,
    body: ClientEmailPreviewRequest,
) -> dict[str, Any]:
    """Resolve date/time/meeting-with from selected appointment only — never invent."""
    appt_date = ""
    appt_time = ""
    appt_contact = ""
    meeting_with = _blank(body.meeting_with)
    used_appointment_event_id: int | None = None
    used_sales_event_id: int | None = None
    note = "No appointment context selected"

    if body.sales_event_id is not None and int(body.sales_event_id) > 0:
        row = conn.execute(
            """
            SELECT id, event_date, event_time, timezone, contact_name,
                   source_rev_spec_text, event_type, meeting_type
            FROM client_sales_events
            WHERE client_id = ? AND id = ?
            """,
            (client_id, int(body.sales_event_id)),
        ).fetchone()
        if not row:
            raise LookupError("Sales appointment not found for this client.")
        date_raw = _blank(row["event_date"])
        time_raw = _blank(row["event_time"])
        tz = _blank(row["timezone"]) or "CDT"
        appt_date = _format_preview_date(date_raw)
        appt_time = _format_preview_time(time_raw, tz) if time_raw else ""
        # If raw exists but formatting failed, leave that portion unresolved (empty).
        appt_contact = _blank(row["contact_name"])
        if not meeting_with:
            meeting_with = _clean_meeting_with(_blank(row["source_rev_spec_text"]))
        used_sales_event_id = int(row["id"])
        note = "Selected sales appointment"
    elif body.appointment_event_id is not None and int(body.appointment_event_id) > 0:
        row = conn.execute(
            """
            SELECT id, appointment_date, appointment_time, contact_name, revenue_specialist
            FROM client_appointment_events
            WHERE client_id = ? AND id = ?
            """,
            (client_id, int(body.appointment_event_id)),
        ).fetchone()
        if not row:
            raise LookupError("Appointment event not found for this client.")
        date_raw = _blank(row["appointment_date"])
        time_raw = _blank(row["appointment_time"])
        appt_date = _format_preview_date(date_raw)
        appt_time = _format_preview_time(time_raw, "CDT") if time_raw else ""
        appt_contact = _blank(row["contact_name"])
        if not meeting_with:
            meeting_with = _clean_meeting_with(_blank(row["revenue_specialist"]))
        used_appointment_event_id = int(row["id"])
        note = "Selected appointment event"
    else:
        # Explicit compose context only (e.g. Send Email props) — no auto-pick.
        # Accept already-readable values or ISO raw from callers.
        date_in = _blank(body.appointment_date)
        time_in = _blank(body.appointment_time)
        appt_date = _format_preview_date(date_in) or date_in
        # Only reformat time when it looks like HH:MM; otherwise keep caller text.
        if re.match(r"^\d{1,2}:\d{2}", time_in):
            appt_time = _format_preview_time(time_in, "CDT")
        else:
            appt_time = time_in
        appt_contact = _blank(body.appointment_contact)
        if appt_date or appt_time or appt_contact or meeting_with:
            note = "Explicit appointment context"

    return {
        "appointment_date": appt_date,
        "appointment_time": appt_time,
        "appointment_contact": appt_contact,
        "meeting_with": meeting_with,
        "appointment_event_id": used_appointment_event_id,
        "sales_event_id": used_sales_event_id,
        "note": note,
    }


def _resolve_signature_for_preview(
    conn,
    *,
    client_id: int,
    account_id: int,
    assignments: list[ClientEmailAccountAssignmentView],
) -> tuple[str, str]:
    """Return (signature_body, provenance). Prefer client+account+rep match."""
    primary = next((a for a in assignments if a.active and a.is_default_for_rep), None)
    if primary is None:
        primary = next((a for a in assignments if a.active), None)

    sigs = conn.execute(
        """
        SELECT * FROM client_email_signatures
        WHERE client_id = ? AND active = 1
        ORDER BY is_default DESC, id ASC
        """,
        (client_id,),
    ).fetchall()
    if not sigs:
        return "", "No active client signature configured"

    def score(row: Any) -> int:
        d = dict(row)
        s = 0
        ea = d.get("email_account_id")
        if ea is not None and int(ea) == account_id:
            s += 40
        elif ea is None:
            s += 5
        else:
            return -1
        if primary:
            if (
                primary.user_id is not None
                and d.get("rep_user_id") is not None
                and int(d["rep_user_id"]) == int(primary.user_id)
            ):
                s += 30
            if (
                _blank(primary.source_rep_name)
                and _blank(d.get("source_rep_name")).lower()
                == _blank(primary.source_rep_name).lower()
            ):
                s += 25
        if d.get("is_default"):
            s += 10
        return s

    best = None
    best_score = -1
    for row in sigs:
        sc = score(row)
        if sc > best_score:
            best_score = sc
            best = row
    if best is None or best_score < 0:
        return "", "No matching client-specific signature"
    body = _normalize_email_plain_text(_blank(best["signature_body"]))
    return body, (
        f"client_email_signatures#{best['id']} · {_blank(best['signature_name'])}"
    )


def _appointment_contact_name(conn, client_id: int) -> str:
    """Approved Client Contact only — never unapproved extraction proposals."""
    try:
        rows = conn.execute(
            """
            SELECT name FROM client_contacts
            WHERE client_id = ? AND active = 1
              AND name IS NOT NULL AND trim(name) != ''
            ORDER BY id ASC
            LIMIT 1
            """,
            (client_id,),
        ).fetchall()
    except Exception:
        return ""
    # Do not invent a single appointment contact from an arbitrary first contact.
    # Only resolve when exactly one approved Client Contact exists, or when a
    # contact is explicitly marked in role_type for appointments.
    try:
        appt_rows = conn.execute(
            """
            SELECT name FROM client_contacts
            WHERE client_id = ? AND active = 1
              AND (
                lower(coalesce(role_type,'')) LIKE '%appointment%'
                OR lower(coalesce(role_type,'')) LIKE '%takes appointment%'
              )
              AND name IS NOT NULL AND trim(name) != ''
            ORDER BY id ASC
            """,
            (client_id,),
        ).fetchall()
        if len(appt_rows) == 1:
            return _blank(appt_rows[0]["name"])
    except Exception:
        pass
    if len(rows) == 1:
        return _blank(rows[0]["name"])
    return ""


def preview_client_email(
    client_id: int,
    body: ClientEmailPreviewRequest,
    *,
    user_id: int | None = None,
) -> ClientEmailPreviewResult:
    """Read-only render / compose draft. Never sends email or writes CRM events."""
    user = _actor_from_user_id(user_id)
    if user is None:
        raise PermissionError("User not found.")
    _require_access(user.id, client_id)
    ensure_client_email_accounts_schema()

    from client_knowledge_data import ensure_client_knowledge_schema, list_email_templates

    ensure_client_knowledge_schema()

    account = get_email_account(client_id, body.account_id, user_id=user.id)
    is_blank = body.template_id is None or int(body.template_id) <= 0
    template_name = "Blank Email"
    subject_src = ""
    body_src = ""
    template_id: int | None = None
    if not is_blank:
        templates = list_email_templates(client_id, user_id=user.id)
        template = next(
            (t for t in templates if t.template_id == int(body.template_id) and t.is_active),
            None,
        )
        if template is None:
            raise LookupError("Active email template not found for this client.")
        template_id = template.template_id
        template_name = template.template_name
        subject_src = template.subject or ""
        body_src = template.body or ""

    with get_connection() as conn:
        cl = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        client_name = _blank(cl["name"]) if cl else ""

        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        name_expr = "trim(coalesce(ct.first_name,'') || ' ' || coalesce(ct.last_name,''))"
        if "full_name" in cols:
            name_expr = "coalesce(nullif(trim(ct.full_name),''), " + name_expr + ")"
        elif "name" in cols:
            name_expr = "coalesce(nullif(trim(ct.name),''), " + name_expr + ")"

        contact_row = conn.execute(
            f"""
            SELECT ct.id AS contact_id,
                   {name_expr} AS contact_name,
                   coalesce(ct.first_name, '') AS first_name,
                   ct.email AS email,
                   c.id AS company_id,
                   c.company_name AS company_name,
                   coalesce(c.external_record_no, '') AS external_record_no
            FROM contacts ct
            JOIN client_company_relationships ccr ON ccr.company_id = ct.company_id
            JOIN companies c ON c.id = ct.company_id
            WHERE ccr.client_id = ? AND ct.id = ?
            """,
            (client_id, body.contact_id),
        ).fetchone()
        if not contact_row:
            raise LookupError("Contact not found for this client.")

        contact_name = _blank(contact_row["contact_name"])
        first_name = _crm_first_name(
            _blank(contact_row["first_name"]), contact_name
        )
        contact_email = _blank(contact_row["email"])
        company_name = _blank(contact_row["company_name"])
        company_id = int(contact_row["company_id"])
        contact_id = int(contact_row["contact_id"])
        external_record_no = _blank(contact_row["external_record_no"])

        assignments = [a for a in account.assignments if a.active]
        primary = next((a for a in assignments if a.is_default_for_rep), None)
        if primary is None and assignments:
            primary = assignments[0]
        rev_spec = ""
        if primary:
            if primary.user_id is not None:
                u = get_user_by_id(primary.user_id)
                rev_spec = (_blank(u.full_name) if u else "") or primary.source_rep_name
            else:
                rev_spec = primary.source_rep_name

        sig_body, sig_prov = _resolve_signature_for_preview(
            conn,
            client_id=client_id,
            account_id=account.account_id,
            assignments=assignments,
        )
        sig_body = _normalize_email_plain_text(sig_body)
        # Appointment: selected event or explicit compose fields only — never guess.
        appt_ctx = _load_appointment_context(conn, client_id, body)
        appt_date = _blank(appt_ctx["appointment_date"])
        appt_time = _blank(appt_ctx["appointment_time"])
        appt_contact = _blank(appt_ctx["appointment_contact"])
        meeting_with = _blank(appt_ctx["meeting_with"])
        if not appt_contact:
            appt_contact = _appointment_contact_name(conn, client_id)
        appt_note = _blank(appt_ctx["note"])

    template_had_sig_placeholder = bool(
        re.search(re.escape(_SIG_PLACEHOLDER), body_src or "", re.I)
    )

    values: dict[str, tuple[str, bool, str]] = {
        "[First Name]": (
            first_name,
            bool(first_name),
            "CRM contact first name",
        ),
        # Legacy [Name] in email templates → first name only (greeting), not full name.
        "[Name]": (
            first_name,
            bool(first_name),
            "CRM contact first name (legacy [Name])",
        ),
        "[Company]": (company_name, bool(company_name), "CRM company name"),
        "[Client Name]": (client_name, bool(client_name), "Active client"),
        "[Revenue Specialist]": (
            rev_spec,
            bool(rev_spec),
            "Sender account assignment",
        ),
        "[Revenue Specialist Signature]": (
            sig_body,
            bool(sig_body),
            sig_prov or "Client signature",
        ),
        "[Appointment Contact]": (
            appt_contact,
            bool(appt_contact),
            "Appointment context or approved Client Contact",
        ),
        "[Meeting With]": (
            meeting_with,
            bool(meeting_with),
            appt_note
            if meeting_with
            else "No meeting participant on selected appointment",
        ),
        "[Appointment Date]": (
            appt_date,
            bool(appt_date),
            appt_note if appt_date else "No appointment context selected",
        ),
        "[Appointment Time]": (
            appt_time,
            bool(appt_time),
            appt_note if appt_time else "No appointment context selected",
        ),
    }

    unresolved: list[PlaceholderResolution] = []
    resolved: list[PlaceholderResolution] = []
    legacy_notes: list[str] = []
    seen_tokens: set[str] = set()

    def apply_text(text: str) -> str:
        out = text
        # Rewrite legacy tokens to standard placeholders (then resolve normally).
        for m in list(_PLACEHOLDER_RE.finditer(out)):
            raw = m.group(0)
            canon = LEGACY_PLACEHOLDER_ALIASES.get(raw.lower())
            if canon and raw != canon:
                out = out.replace(raw, canon, 1)
                note = (
                    f"Legacy token {raw} mapped to {canon}. "
                    "Consider updating the stored template."
                )
                if note not in legacy_notes:
                    legacy_notes.append(note)
        for token, (val, ok, note) in values.items():
            pattern = re.compile(re.escape(token), re.I)
            if not pattern.search(out):
                continue
            key = token.lower()
            if ok and val:
                out = pattern.sub(val, out)
                if key not in seen_tokens:
                    seen_tokens.add(key)
                    resolved.append(
                        PlaceholderResolution(
                            placeholder=token,
                            resolved=True,
                            value=val,
                            note=note,
                        )
                    )
            else:
                out = pattern.sub(f"{token}\nUNRESOLVED", out)
                if key not in seen_tokens:
                    seen_tokens.add(key)
                    unresolved.append(
                        PlaceholderResolution(
                            placeholder=token,
                            resolved=False,
                            value="",
                            note=note or "UNRESOLVED",
                        )
                    )
        for m in list(_PLACEHOLDER_RE.finditer(out)):
            token = m.group(0)
            if "UNRESOLVED" in token:
                continue
            key = token.lower()
            if key in seen_tokens:
                continue
            seen_tokens.add(key)
            out = out.replace(token, f"{token}\nUNRESOLVED", 1)
            unresolved.append(
                PlaceholderResolution(
                    placeholder=token,
                    resolved=False,
                    value="",
                    note="Unknown or unsupported placeholder",
                )
            )
        return out

    rendered_subject = apply_text(subject_src)
    rendered_message = _normalize_email_plain_text(apply_text(body_src))
    final_body, sig_placement = _compose_body_with_signature(
        rendered_message,
        sig_body,
        template_had_signature_placeholder=template_had_sig_placeholder,
    )
    if (
        sig_placement == "appended"
        and sig_body
        and "[revenue specialist signature]" not in seen_tokens
    ):
        resolved.append(
            PlaceholderResolution(
                placeholder="[Revenue Specialist Signature]",
                resolved=True,
                value=sig_body,
                note=f"Appended automatically · {sig_prov or 'Client signature'}",
            )
        )

    from_display = (
        f"{account.display_name} <{account.email_address}>"
        if account.display_name
        else account.email_address
    )
    to_display = contact_email if contact_email else "No contact email available"
    connected = account.connection_status == "connected"

    return ClientEmailPreviewResult(
        client_id=client_id,
        client_name=client_name,
        account_id=account.account_id,
        template_id=template_id,
        template_name=template_name,
        is_blank=is_blank,
        contact_id=contact_id,
        company_id=company_id,
        from_address=account.email_address,
        from_display=from_display,
        to_address=to_display,
        to_contact_name=contact_name,
        subject=rendered_subject,
        body=final_body,
        message_body=rendered_message,
        signature=sig_body,
        signature_placement=sig_placement,
        signature_source=sig_prov,
        revenue_specialist=rev_spec,
        meeting_with=meeting_with,
        connection_status=account.connection_status,
        connection_connected=connected,
        send_enabled=connected,  # UI still requires human confirm; API enforces confirm_send
        unresolved_placeholders=unresolved,
        resolved_placeholders=resolved,
        legacy_token_notes=legacy_notes,
        message=(
            (
                "Not Connected — Preview Only. "
                if not connected
                else "Preview only — confirm before a future Send. "
            )
            + (
                "Contact has no email address. "
                if not contact_email
                else ""
            )
            + (
                "Unresolved placeholders are marked UNRESOLVED. "
                if unresolved
                else "Placeholders resolved where data was available. "
            )
            + (
                "Sender signature included once. "
                if sig_placement in {"appended", "placeholder"}
                else "No client signature configured. "
            )
            + "Compose edits do not change the stored template. No email is sent."
        ),
        supported_placeholders=list(EMAIL_PLACEHOLDERS),
        appointment_event_id=appt_ctx["appointment_event_id"],
        sales_event_id=appt_ctx["sales_event_id"],
        external_record_no=external_record_no,
        campaign_id=None,
    )


def pick_default_sender_account(
    client_id: int, *, user_id: int | None = None
) -> ClientEmailAccountView | None:
    """Prefer active default sender; else first active account for the client."""
    accounts = [
        a
        for a in list_email_accounts(client_id, user_id=user_id, include_inactive=False)
        if a.active
    ]
    if not accounts:
        return None
    for a in accounts:
        if a.is_default:
            return a
    return accounts[0]


def list_configured_sender_facts(
    client_id: int, *, user_id: int | None = None
) -> list[dict[str, Any]]:
    """Read-only facts for Ask NorthStar — configured accounts only."""
    accounts = list_email_accounts(client_id, user_id=user_id, include_inactive=False)
    facts: list[dict[str, Any]] = []
    for a in accounts:
        reps = []
        for asg in a.assignments:
            if not asg.active:
                continue
            if asg.user_id is not None:
                u = get_user_by_id(asg.user_id)
                reps.append(_blank(u.full_name) if u else asg.source_rep_name)
            else:
                reps.append(asg.source_rep_name)
        facts.append(
            {
                "email_address": a.email_address,
                "display_name": a.display_name,
                "provider": a.provider,
                "connection_status": a.connection_status,
                "is_default": a.is_default,
                "assigned_reps": [r for r in reps if r],
                "connected": a.connection_status == "connected",
            }
        )
    return facts
