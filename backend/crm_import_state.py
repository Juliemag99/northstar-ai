"""Deterministic U.S. state normalization for CRM import.

Converts full state names and two-letter codes to uppercase USPS abbreviations.
No fuzzy/AI matching. Territories are omitted — live NorthStar company data uses
only the 50 states (plus occasional spelled names); DC is included as a district.
"""

from __future__ import annotations

# 50 states + District of Columbia. No territories (none in existing company data).
_US_STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

_USPS_CODES: frozenset[str] = frozenset(_US_STATE_NAME_TO_CODE.values()) | {"DC"}

INVALID_STATE = "invalid_state"


def blank_state(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_us_state(value: object | None) -> str | None:
    """Normalize a U.S. state to an uppercase USPS code.

    Returns:
      "" for blank/whitespace-only input
      two-letter uppercase code for valid names or abbreviations
      None for unknown nonblank input
    """
    text = blank_state(value)
    if not text:
        return ""
    upper = text.upper()
    if len(upper) == 2 and upper.isalpha() and upper in _USPS_CODES:
        return upper
    code = _US_STATE_NAME_TO_CODE.get(text.casefold())
    if code:
        return code
    return None


def state_for_match(value: object | None) -> str:
    """Normalize a state for address matching without rewriting storage.

    Valid names/codes become USPS codes. Blank stays blank. Unknown nonblank
    values fall back to uppercase trim so they only match the same unknown form.
    """
    normalized = normalize_us_state(value)
    if normalized is None:
        return blank_state(value).upper()
    return normalized


def all_state_name_mappings() -> tuple[tuple[str, str], ...]:
    """Canonical (display name, code) pairs for exhaustive tests."""
    out: list[tuple[str, str]] = []
    for name, code in sorted(_US_STATE_NAME_TO_CODE.items(), key=lambda item: item[1]):
        if name == "district of columbia":
            display = "District of Columbia"
        else:
            display = " ".join(part.capitalize() for part in name.split())
        out.append((display, code))
    return tuple(out)
