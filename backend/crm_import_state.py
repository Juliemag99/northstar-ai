"""Deterministic state/province normalization for CRM import.

Converts full names and two-letter codes to uppercase abbreviations for:
- U.S. 50 states + District of Columbia
- Canadian provinces and territories

No fuzzy/AI matching. U.S. territories are omitted (none in existing company data).
"""

from __future__ import annotations

# 50 states + District of Columbia. No U.S. territories.
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

# Canadian provinces and territories (ISO 3166-2:CA codes).
_CA_PROVINCE_NAME_TO_CODE: dict[str, str] = {
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "newfoundland": "NL",
    "nova scotia": "NS",
    "northwest territories": "NT",
    "nunavut": "NU",
    "ontario": "ON",
    "prince edward island": "PE",
    "quebec": "QC",
    "saskatchewan": "SK",
    "yukon": "YT",
    "yukon territory": "YT",
}

_USPS_CODES: frozenset[str] = frozenset(_US_STATE_NAME_TO_CODE.values()) | {"DC"}
_CA_CODES: frozenset[str] = frozenset(_CA_PROVINCE_NAME_TO_CODE.values())
_REGION_CODES: frozenset[str] = _USPS_CODES | _CA_CODES

INVALID_STATE = "invalid_state"


def blank_state(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_us_state(value: object | None) -> str | None:
    """Normalize a U.S. state or Canadian province to an uppercase code.

    Returns:
      "" for blank/whitespace-only input
      two-letter uppercase code for valid names or abbreviations
      None for unknown nonblank input
    """
    text = blank_state(value)
    if not text:
        return ""
    upper = text.upper()
    if len(upper) == 2 and upper.isalpha() and upper in _REGION_CODES:
        return upper
    folded = text.casefold()
    code = _US_STATE_NAME_TO_CODE.get(folded)
    if code:
        return code
    code = _CA_PROVINCE_NAME_TO_CODE.get(folded)
    if code:
        return code
    return None


def state_for_match(value: object | None) -> str:
    """Normalize a state/province for address matching without rewriting storage.

    Valid names/codes become abbreviations. Blank stays blank. Unknown nonblank
    values fall back to uppercase trim so they only match the same unknown form.
    """
    normalized = normalize_us_state(value)
    if normalized is None:
        return blank_state(value).upper()
    return normalized


def all_state_name_mappings() -> tuple[tuple[str, str], ...]:
    """Canonical (display name, code) pairs for exhaustive U.S. tests."""
    out: list[tuple[str, str]] = []
    for name, code in sorted(_US_STATE_NAME_TO_CODE.items(), key=lambda item: item[1]):
        if name == "district of columbia":
            display = "District of Columbia"
        else:
            display = " ".join(part.capitalize() for part in name.split())
        out.append((display, code))
    return tuple(out)


def all_canadian_province_name_mappings() -> tuple[tuple[str, str], ...]:
    """Canonical (display name, code) pairs for Canadian provinces/territories."""
    # Prefer the primary full name for each code (skip aliases).
    primary = {
        "AB": "Alberta",
        "BC": "British Columbia",
        "MB": "Manitoba",
        "NB": "New Brunswick",
        "NL": "Newfoundland and Labrador",
        "NS": "Nova Scotia",
        "NT": "Northwest Territories",
        "NU": "Nunavut",
        "ON": "Ontario",
        "PE": "Prince Edward Island",
        "QC": "Quebec",
        "SK": "Saskatchewan",
        "YT": "Yukon",
    }
    return tuple((name, code) for code, name in sorted(primary.items(), key=lambda i: i[0]))
