"""Controlled Next Action catalog — one backend source for all NorthStar surfaces.

Global defaults live in client_next_actions with client_id = 0.
A client can later override by inserting its own rows (client_id > 0).
Existing free-text values are matched on read; production rows are not rewritten.
"""

from __future__ import annotations

import json

from db import get_connection

GLOBAL_CLIENT_ID = 0
NO_NEXT_ACTION_CODE = "no_next_action"
OTHER_CODE = "other"

# Single source of truth for default choices. Do not copy this list into UI files.
DEFAULT_NEXT_ACTIONS: tuple[dict[str, object], ...] = (
    {
        "code": "call",
        "label": "Call",
        "aliases": ("call",),
        "requires_detail": False,
    },
    {
        "code": "send_email",
        "label": "Send Email",
        "aliases": ("send email", "email", "e-mail", "e mail"),
        "requires_detail": False,
    },
    {
        "code": "follow_up",
        "label": "Follow-Up",
        "aliases": ("follow-up", "follow up", "followup", "follow"),
        "requires_detail": False,
    },
    {
        "code": "send_information",
        "label": "Send Information",
        "aliases": ("send information", "send info", "send information/literature"),
        "requires_detail": False,
    },
    {
        "code": "research_company",
        "label": "Research Company",
        "aliases": ("research company", "research"),
        "requires_detail": False,
    },
    {
        "code": "find_contact_name",
        "label": "Find Contact Name",
        "aliases": ("find contact name", "find contact", "get contact name"),
        "requires_detail": False,
    },
    {
        "code": "find_phone_number",
        "label": "Find Phone Number",
        "aliases": ("find phone number", "find phone", "get phone number", "get phone"),
        "requires_detail": False,
    },
    {
        "code": "schedule_meeting",
        "label": "Schedule Meeting",
        "aliases": ("schedule meeting", "schedule appointment", "meeting"),
        "requires_detail": False,
    },
    {
        "code": "prepare_quote",
        "label": "Prepare Quote",
        "aliases": ("prepare quote", "quote", "quotation"),
        "requires_detail": False,
    },
    {
        "code": NO_NEXT_ACTION_CODE,
        "label": "No Next Action",
        "aliases": ("no next action", "none", "n/a", "na", "no action"),
        "requires_detail": False,
    },
    {
        "code": OTHER_CODE,
        "label": "Other",
        "aliases": ("other",),
        "requires_detail": True,
    },
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _fold(value: str) -> str:
    key = _blank(value).lower().replace("_", " ").replace("-", " ")
    return " ".join(key.split())


def status_defaults_no_next_action(status: str) -> bool:
    """Closed / Do Not Call / disqualified default to No Next Action in the UI."""
    key = _blank(status).lower()
    if not key:
        return False
    if "do not call" in key or key in {"dnc"}:
        return True
    if key == "closed" or key.startswith("closed"):
        return True
    if "disqualified" in key:
        return True
    return False


def ensure_next_action_schema(conn=None) -> None:
    """Create and seed the global Next Action catalog. Does not alter CRM rows."""
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_next_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL DEFAULT 0,
                code TEXT NOT NULL,
                label TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                requires_detail INTEGER NOT NULL DEFAULT 0,
                aliases TEXT NOT NULL DEFAULT '[]',
                UNIQUE (client_id, code)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_client_next_actions_client
                ON client_next_actions(client_id, sort_order, id)
            """
        )
        for index, item in enumerate(DEFAULT_NEXT_ACTIONS):
            conn.execute(
                """
                INSERT INTO client_next_actions (
                    client_id, code, label, sort_order, active, requires_detail, aliases
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(client_id, code) DO UPDATE SET
                    label = excluded.label,
                    sort_order = excluded.sort_order,
                    requires_detail = excluded.requires_detail,
                    aliases = excluded.aliases,
                    active = 1
                """,
                (
                    GLOBAL_CLIENT_ID,
                    str(item["code"]),
                    str(item["label"]),
                    index,
                    1 if item["requires_detail"] else 0,
                    json.dumps(list(item["aliases"])),
                ),
            )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _rows_to_choices(rows) -> list[dict]:
    choices: list[dict] = []
    for row in rows:
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        if not isinstance(aliases, list):
            aliases = []
        label = _blank(row["label"])
        folded_aliases = {_fold(label), _fold(str(row["code"]))}
        for alias in aliases:
            folded = _fold(str(alias))
            if folded:
                folded_aliases.add(folded)
        choices.append(
            {
                "code": _blank(row["code"]),
                "label": label,
                "aliases": sorted(folded_aliases),
                "requires_detail": bool(int(row["requires_detail"] or 0)),
                "sort_order": int(row["sort_order"] or 0),
            }
        )
    return choices


def _choices_from_defaults() -> list[dict]:
    return [
        {
            "code": str(item["code"]),
            "label": str(item["label"]),
            "aliases": sorted(
                {
                    _fold(str(item["label"])),
                    _fold(str(item["code"])),
                    *(_fold(str(alias)) for alias in item["aliases"]),
                }
            ),
            "requires_detail": bool(item["requires_detail"]),
            "sort_order": index,
        }
        for index, item in enumerate(DEFAULT_NEXT_ACTIONS)
    ]


def _load_choice_rows(conn, client_id: int) -> list[dict]:
    source_id = GLOBAL_CLIENT_ID
    scoped_id = int(client_id or 0)
    if scoped_id > 0:
        has_client = conn.execute(
            """
            SELECT 1 FROM client_next_actions
            WHERE client_id = ? AND active = 1
            LIMIT 1
            """,
            (scoped_id,),
        ).fetchone()
        if has_client is not None:
            source_id = scoped_id
    rows = conn.execute(
        """
        SELECT code, label, sort_order, requires_detail, aliases
        FROM client_next_actions
        WHERE client_id = ? AND active = 1
        ORDER BY sort_order ASC, id ASC
        """,
        (source_id,),
    ).fetchall()
    return _rows_to_choices(rows)


def list_next_action_choices(*, client_id: int | None = None, conn=None) -> list[dict]:
    """Active Next Action choices for a client, falling back to the global catalog.

    Pass conn when already inside a write transaction so this does not open a
    second SQLite connection.
    """
    scoped_id = int(client_id or 0)
    if conn is not None:
        try:
            choices = _load_choice_rows(conn, scoped_id)
        except Exception:
            choices = []
        return choices or _choices_from_defaults()

    ensure_next_action_schema()
    with get_connection() as owned:
        try:
            choices = _load_choice_rows(owned, scoped_id)
        except Exception:
            choices = []
    return choices or _choices_from_defaults()


def get_next_action_catalog(*, client_id: int | None = None) -> dict:
    choices = list_next_action_choices(client_id=client_id)
    return {
        "client_id": int(client_id or 0),
        "choices": [
            {
                "code": item["code"],
                "label": item["label"],
                "aliases": item["aliases"],
                "requires_detail": item["requires_detail"],
            }
            for item in choices
        ],
        "default_for_closed_code": NO_NEXT_ACTION_CODE,
    }


def match_next_action(
    raw: str,
    *,
    client_id: int | None = None,
    conn=None,
) -> tuple[dict | None, str]:
    """Map stored free text onto a catalog choice without rewriting the row.

    Exact/alias matches (e.g. "call" → Call) use the canonical choice.
    Unknown values stay intact as Other custom text.
    """
    value = _blank(raw)
    choices = list_next_action_choices(client_id=client_id, conn=conn)
    other = next((item for item in choices if item["code"] == OTHER_CODE), None)
    if not value:
        return None, ""
    folded = _fold(value)
    for item in choices:
        if item["code"] == OTHER_CODE:
            continue
        if folded in set(item["aliases"]) or folded == _fold(item["label"]):
            return item, ""
    if other is not None:
        custom = "" if folded == _fold(other["label"]) else value
        return other, custom
    return None, value


def canonicalize_next_action(
    raw: str,
    *,
    client_id: int | None = None,
    status: str | None = None,
    allow_empty: bool = True,
    conn=None,
) -> str:
    """Validate and store a Next Action. Canonicalizes known aliases; keeps Other text.

    Does not bulk-update existing records — callers invoke this on user save.
    """
    value = _blank(raw)
    if not value:
        if status and status_defaults_no_next_action(status):
            matched, _ = match_next_action("No Next Action", client_id=client_id, conn=conn)
            return str(matched["label"]) if matched else "No Next Action"
        if allow_empty:
            return ""
        raise ValueError("Next Action is required.")
    choice, custom = match_next_action(value, client_id=client_id, conn=conn)
    if choice is None:
        return value
    if choice["requires_detail"]:
        detail = _blank(custom) or ("" if _fold(value) == _fold(choice["label"]) else value)
        if not detail:
            raise ValueError("Enter a custom Next Action when Other is selected.")
        return detail
    return str(choice["label"])


def display_next_action(raw: str, *, client_id: int | None = None) -> str:
    """Canonical label for display; unknown values are returned unchanged."""
    value = _blank(raw)
    if not value:
        return ""
    choice, custom = match_next_action(value, client_id=client_id)
    if choice is None:
        return value
    if choice["requires_detail"]:
        return custom or value
    return str(choice["label"])
