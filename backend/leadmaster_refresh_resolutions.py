"""Deterministic LeadMaster refresh review resolutions.

Free-form values cannot drive writes. Changing a resolution changes the
plan fingerprint. Isolated schema only.
"""

from __future__ import annotations

from typing import Any

ACCEPT_PROPOSED = "ACCEPT_PROPOSED"
KEEP_EXISTING = "KEEP_EXISTING"
LINK_EXISTING_COMPANY = "LINK_EXISTING_COMPANY"
LINK_EXISTING_CONTACT = "LINK_EXISTING_CONTACT"
CREATE_NEW_COMPANY = "CREATE_NEW_COMPANY"
CREATE_NEW_CONTACT = "CREATE_NEW_CONTACT"
MAP_SOURCE_REP = "MAP_SOURCE_REP"
CLASSIFY_CAMPAIGN_ACTIVE = "CLASSIFY_CAMPAIGN_ACTIVE"
CLASSIFY_CAMPAIGN_HISTORICAL = "CLASSIFY_CAMPAIGN_HISTORICAL"
CLASSIFY_CAMPAIGN_SYSTEM = "CLASSIFY_CAMPAIGN_SYSTEM"
SKIP_SOURCE_ROW = "SKIP_SOURCE_ROW"

ALLOWED_RESOLUTIONS = frozenset(
    {
        ACCEPT_PROPOSED,
        KEEP_EXISTING,
        LINK_EXISTING_COMPANY,
        LINK_EXISTING_CONTACT,
        CREATE_NEW_COMPANY,
        CREATE_NEW_CONTACT,
        MAP_SOURCE_REP,
        CLASSIFY_CAMPAIGN_ACTIVE,
        CLASSIFY_CAMPAIGN_HISTORICAL,
        CLASSIFY_CAMPAIGN_SYSTEM,
        SKIP_SOURCE_ROW,
    }
)

# Proposal action / review_reason → allowed resolutions.
_ALLOWED_BY_ACTION: dict[str, frozenset[str]] = {
    "CONFLICT": frozenset({KEEP_EXISTING, ACCEPT_PROPOSED, SKIP_SOURCE_ROW}),
    "CLIENT_SOURCE_DIFFERENCE": frozenset({KEEP_EXISTING, ACCEPT_PROPOSED, SKIP_SOURCE_ROW}),
    "LOCATION_DIFFERENCE": frozenset({KEEP_EXISTING, SKIP_SOURCE_ROW}),
    "REVIEW_REQUIRED": frozenset(
        {
            KEEP_EXISTING,
            LINK_EXISTING_COMPANY,
            CREATE_NEW_COMPANY,
            SKIP_SOURCE_ROW,
        }
    ),
    "POSSIBLE_DUPLICATE": frozenset(
        {LINK_EXISTING_CONTACT, CREATE_NEW_CONTACT, SKIP_SOURCE_ROW}
    ),
    "UNKNOWN_SOURCE_REP": frozenset({MAP_SOURCE_REP, KEEP_EXISTING, SKIP_SOURCE_ROW}),
    "INACTIVE_TARGET": frozenset({KEEP_EXISTING, SKIP_SOURCE_ROW}),
    "AMBIGUOUS": frozenset({KEEP_EXISTING, MAP_SOURCE_REP, SKIP_SOURCE_ROW}),
    "CAMPAIGN_UNKNOWN": frozenset(
        {
            CLASSIFY_CAMPAIGN_ACTIVE,
            CLASSIFY_CAMPAIGN_HISTORICAL,
            CLASSIFY_CAMPAIGN_SYSTEM,
            SKIP_SOURCE_ROW,
        }
    ),
    "SOURCE_ID_CONFLICT": frozenset({SKIP_SOURCE_ROW}),
    "ARCHIVED_MATCH_REVIEW": frozenset({KEEP_EXISTING, SKIP_SOURCE_ROW}),
    "RESTORE_RELATIONSHIP_REVIEW": frozenset({KEEP_EXISTING, SKIP_SOURCE_ROW}),
    "MANUAL_OVERRIDE_CONFLICT": frozenset({KEEP_EXISTING, ACCEPT_PROPOSED, SKIP_SOURCE_ROW}),
    "STATUS_CHANGE": frozenset({KEEP_EXISTING, ACCEPT_PROPOSED, SKIP_SOURCE_ROW}),
}


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_resolutions(raw: object | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        resolution = _blank(item.get("resolution")).upper()
        if resolution not in ALLOWED_RESOLUTIONS:
            raise ValueError(f"Unsupported refresh resolution: {resolution or '(blank)'}")
        source_row = int(item.get("source_row") or 0)
        if source_row <= 0:
            raise ValueError("Resolution source_row is required.")
        field = _blank(item.get("field"))
        action = _blank(item.get("proposal_action"))
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        allowed = _ALLOWED_BY_ACTION.get(action)
        if action and allowed is not None and resolution not in allowed:
            raise ValueError(
                f"Resolution {resolution} is not allowed for proposal {action}."
            )
        if resolution == SKIP_SOURCE_ROW:
            pass
        elif action and allowed is None and resolution not in ALLOWED_RESOLUTIONS:
            raise ValueError(f"Resolution {resolution} is not allowed for proposal {action}.")
        out.append(
            {
                "source_row": source_row,
                "field": field,
                "proposal_action": action,
                "resolution": resolution,
                "payload": {
                    k: payload[k]
                    for k in sorted(payload)
                    if payload[k] not in (None, "")
                },
            }
        )
    out.sort(
        key=lambda r: (
            int(r["source_row"]),
            str(r["field"]),
            str(r["proposal_action"]),
            str(r["resolution"]),
        )
    )
    return out


def resolution_for(
    resolutions: list[dict[str, Any]],
    *,
    source_row: int,
    field: str = "",
    action: str = "",
) -> dict[str, Any] | None:
    skip = None
    matched = None
    for item in resolutions:
        if int(item["source_row"]) != int(source_row):
            continue
        if item["resolution"] == SKIP_SOURCE_ROW:
            skip = item
        if field and item["field"] and item["field"] != field:
            continue
        if action and item["proposal_action"] and item["proposal_action"] != action:
            continue
        matched = item
    return skip or matched


def row_is_skipped(resolutions: list[dict[str, Any]], source_row: int) -> bool:
    item = resolution_for(resolutions, source_row=source_row)
    return bool(item and item["resolution"] == SKIP_SOURCE_ROW)


def fingerprint_slice(resolutions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return normalize_resolutions(resolutions)
