"""Locale-aware numeric parsing, presentation-currency decoding, and date helpers."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")

_DASH_ONLY = re.compile(r"^[-–—−]+$")
_CURRENCY_RE = re.compile(r"^\s*(?P<scale>\d+)?\s*(?P<code>TL|TRY|USD|EUR|GBP)\s*$", re.IGNORECASE)
_CURRENCY_CODES = {"TL": "TRY", "TRY": "TRY", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}
_DMY_DATE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")


class UnitError(ValueError):
    """Raised for unparseable numbers, unknown presentation units, or bad dates."""


def parse_turkish_number(text: str | None) -> Decimal | None:
    """Parse a Turkish-formatted amount: `.` groups thousands, `,` is the decimal mark.

    Returns None for blank cells and bare dashes (the caller decides whether a dash means
    zero). Parentheses or a leading minus denote negatives. Raises UnitError on malformed
    input instead of guessing.
    """
    if text is None:
        return None
    raw = text.replace(" ", " ").strip()
    if not raw or _DASH_ONLY.match(raw):
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1].strip()
    if raw[:1] in "-−–":
        negative = not negative
        raw = raw[1:].strip()
    elif raw[:1] == "+":
        raw = raw[1:].strip()
    raw = raw.replace(" ", "")
    if not raw:
        return None
    integer_part, _, fraction_part = raw.partition(",")
    if "," in fraction_part:
        raise UnitError(f"multiple decimal marks in {text!r}")
    if "." in integer_part:
        groups = integer_part.split(".")
        if not groups[0] or len(groups[0]) > 3 or any(len(group) != 3 for group in groups[1:]):
            raise UnitError(f"unexpected thousands grouping in {text!r}")
        integer_part = "".join(groups)
    if "." in fraction_part:
        raise UnitError(f"unexpected '.' after decimal mark in {text!r}")
    if not integer_part.isdigit() or (fraction_part and not fraction_part.isdigit()):
        raise UnitError(f"not a number: {text!r}")
    try:
        value = Decimal(f"{integer_part}.{fraction_part}" if fraction_part else integer_part)
    except InvalidOperation as exc:  # pragma: no cover - guarded by isdigit checks
        raise UnitError(f"not a number: {text!r}") from exc
    return -value if negative else value


def parse_structured_number(value: object) -> Decimal | None:
    """Parse a JSON-style numeric value that already uses a decimal point."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise UnitError(f"boolean is not a number: {value!r}")
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    text = str(value).strip()
    if _DASH_ONLY.match(text):
        return None
    try:
        parsed = Decimal(text)
        if not parsed.is_finite():
            raise UnitError(f"non-finite number: {value!r}")
        return parsed
    except InvalidOperation as exc:
        raise UnitError(f"not a number: {value!r}") from exc


def decode_presentation_currency(raw: str | None) -> tuple[str, int]:
    """Map `TL`, `1000TL`, `1000000TL` (and USD/EUR variants) to (ISO code, scale)."""
    if raw is None:
        raise UnitError("presentation currency is missing")
    match = _CURRENCY_RE.match(raw)
    if not match:
        raise UnitError(f"unknown presentation currency {raw!r}")
    scale = int(match.group("scale") or 1)
    if scale not in (1, 1000, 1_000_000):
        raise UnitError(f"unsupported presentation scale {scale} in {raw!r}")
    return _CURRENCY_CODES[match.group("code").upper()], scale


def scale_monetary(value: Decimal | None, scale: int) -> Decimal | None:
    if value is None:
        return None
    return value * scale


def parse_dmy_date(text: str | None) -> date | None:
    """Parse `31.12.2024` (also `/` or `-` separators) as day-month-year."""
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    match = _DMY_DATE.match(cleaned)
    if not match:
        raise UnitError(f"not a day-month-year date: {text!r}")
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise UnitError(f"invalid date {text!r}") from exc


def istanbul_to_utc(naive_or_aware: datetime) -> datetime:
    """KAP timestamps without an offset are Europe/Istanbul wall-clock time."""
    if naive_or_aware.tzinfo is None:
        return naive_or_aware.replace(tzinfo=ISTANBUL).astimezone(timezone.utc)
    return naive_or_aware.astimezone(timezone.utc)


def parse_source_timestamp(text: str) -> datetime:
    """Parse `31.12.2024 18:05:12` or ISO-8601 into an aware UTC datetime."""
    cleaned = text.strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return istanbul_to_utc(datetime.strptime(cleaned, fmt))
        except ValueError:
            continue
    try:
        return istanbul_to_utc(datetime.fromisoformat(cleaned.replace("Z", "+00:00")))
    except ValueError as exc:
        raise UnitError(f"unrecognised timestamp {text!r}") from exc


def is_twelve_month_span(start: date | None, end: date | None) -> bool:
    if start is None or end is None:
        return False
    try:
        expected_start = end.replace(year=end.year - 1)
    except ValueError:  # 29 February end date
        expected_start = end.replace(year=end.year - 1, day=28)
    return abs((start - expected_start).days - 1) <= 1
