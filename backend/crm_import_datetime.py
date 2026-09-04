"""Deterministic import datetime normalization for CRM source-history fields.

Accepts Excel serial dates (with fractional time), datetime objects, and common
date/time strings. Stores/displays as MM/DD/YYYY h:mm AM/PM after rounding to
the nearest minute. No fuzzy/AI parsing.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

# Excel / Windows serial day 0 (Lotus 1900 leap-year compatibility epoch).
_EXCEL_EPOCH = datetime(1899, 12, 30)

INVALID_DATE = "invalid_date"

_DISPLAY_RE = re.compile(
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[AaPp][Mm])$"
)
_LEGACY_24H_RE = re.compile(
    r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?$"
)
_ISO_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[ T](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"(?P<frac>\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)
_DATE_ONLY_ISO_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")
_DATE_ONLY_US_RE = re.compile(r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})$")
_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def blank_datetime(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _round_to_minute(dt: datetime) -> datetime:
    """Round floating-point seconds to the nearest minute."""
    base = dt.replace(second=0, microsecond=0, tzinfo=None)
    # Work on naive clock components; drop tz for storage/display.
    naive = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    advance = timedelta(seconds=naive.second, microseconds=naive.microsecond)
    half = timedelta(seconds=30)
    if advance >= half:
        return base + timedelta(minutes=1)
    return base


def format_import_datetime(dt: datetime) -> str:
    """NorthStar CRM-import stored/display form: MM/DD/YYYY h:mm AM/PM."""
    rounded = _round_to_minute(dt)
    hour12 = rounded.hour % 12 or 12
    ampm = "AM" if rounded.hour < 12 else "PM"
    return f"{rounded.month:02d}/{rounded.day:02d}/{rounded.year} {hour12}:{rounded.minute:02d} {ampm}"


def _from_excel_serial(serial: float) -> datetime | None:
    if not math.isfinite(serial):
        return None
    # Reject absurd / non-date magnitudes (Excel day numbers ~1..60000 for modern work).
    if serial < 1 or serial > 1000000:
        return None
    try:
        return _EXCEL_EPOCH + timedelta(days=float(serial))
    except (OverflowError, ValueError):
        return None


def _parse_datetime_string(text: str) -> datetime | None:
    m = _DISPLAY_RE.match(text)
    if m:
        hour = int(m.group("hour"))
        minute = int(m.group("minute"))
        if hour < 1 or hour > 12 or minute > 59:
            return None
        ampm = m.group("ampm").upper()
        if ampm == "AM":
            hour24 = 0 if hour == 12 else hour
        else:
            hour24 = 12 if hour == 12 else hour + 12
        try:
            return datetime(
                int(m.group("year")),
                int(m.group("month")),
                int(m.group("day")),
                hour24,
                minute,
            )
        except ValueError:
            return None

    m = _LEGACY_24H_RE.match(text)
    if m:
        try:
            return datetime(
                int(m.group("year")),
                int(m.group("month")),
                int(m.group("day")),
                int(m.group("hour")),
                int(m.group("minute")),
                int(m.group("second") or 0),
            )
        except ValueError:
            return None

    m = _ISO_RE.match(text)
    if m:
        try:
            return datetime(
                int(m.group("year")),
                int(m.group("month")),
                int(m.group("day")),
                int(m.group("hour")),
                int(m.group("minute")),
                int(m.group("second") or 0),
            )
        except ValueError:
            return None

    m = _DATE_ONLY_ISO_RE.match(text)
    if m:
        try:
            return datetime(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        except ValueError:
            return None

    m = _DATE_ONLY_US_RE.match(text)
    if m:
        try:
            return datetime(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        except ValueError:
            return None

    return None


def normalize_import_datetime(value: object | None) -> str | None:
    """Normalize an import datetime to MM/DD/YYYY h:mm AM/PM.

    Returns:
      "" for blank input
      normalized display/storage string for valid values
      None for unknown/invalid nonblank input
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return format_import_datetime(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        dt = _from_excel_serial(float(value))
        if dt is None:
            return None
        return format_import_datetime(dt)

    text = blank_datetime(value)
    if not text:
        return ""

    if _NUMERIC_RE.match(text):
        try:
            serial = float(text)
        except ValueError:
            return None
        dt = _from_excel_serial(serial)
        if dt is None:
            return None
        return format_import_datetime(dt)

    parsed = _parse_datetime_string(text)
    if parsed is None:
        return None
    return format_import_datetime(parsed)
