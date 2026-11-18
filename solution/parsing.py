"""Pure parsing / normalisation primitives. No I/O, no database.

Everything here follows the rules in assignment-a-data/README.md. Each function returns
``None`` (or ``(False, None)`` from :func:`parse_value`) when the input is invalid, so callers
decide what "invalid" means (reject the row, warn, orphan the event...).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

IST = timezone(timedelta(hours=5, minutes=30))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_US = timedelta(microseconds=1)
IST_OFFSET_SECONDS = 5 * 3600 + 1800

# ---------------------------------------------------------------- headers
_WS_UNDERSCORE = re.compile(r"[_\s]+")


def normalise_header(h: str) -> str:
    """lower-case, trim, collapse every run of '_' / whitespace to one space.

    A final strip is applied so that '_phone_' behaves like 'phone' (the literal spec order
    would leave stray spaces that can never match any alias).
    """
    return _WS_UNDERSCORE.sub(" ", h.lower().strip()).strip()


# ---------------------------------------------------------------- phone
_NON_DIGIT = re.compile(r"[^0-9]")


def normalise_phone(raw: Any) -> Optional[str]:
    """Return canonical '+91XXXXXXXXXX' or None if the number is not a valid Indian mobile."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float):
        return None
    digits = _NON_DIGIT.sub("", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def mask_phone(canonical: str) -> str:
    return "+91******" + canonical[-4:]


# ---------------------------------------------------------------- dates
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_D_ISO = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_D_SLASH = re.compile(r"^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$")
_D_DASH = re.compile(r"^([0-9]{1,2})-([0-9]{1,2})-([0-9]{4})$")
_D_MON = re.compile(r"^([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})$")


def parse_date(raw: str) -> Optional[str]:
    """Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (day first). Returns ISO or None."""
    s = raw.strip()
    try:
        m = _D_ISO.match(s)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        m = _D_SLASH.match(s) or _D_DASH.match(s)
        if m:
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        m = _D_MON.match(s)
        if m:
            mon = _MONTHS.get(m[2].lower())
            if mon is None:
                return None
            return date(int(m[3]), mon, int(m[1])).isoformat()
    except ValueError:
        return None
    return None


# ---------------------------------------------------------------- decimal
_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
_CENT = Decimal("0.01")


def parse_decimal(raw: str) -> Optional[str]:
    """'₹12,345' / 'Rs. 4521' / 'INR 12345.5' / '999.5' -> '12345.00' style string, >= 0 only."""
    s = _CURRENCY_PREFIX.sub("", raw.strip(), count=1).replace(",", "")
    if not _NUMBER.match(s):
        return None
    return format(Decimal(s).quantize(_CENT, rounding=ROUND_HALF_UP), "f")


# ---------------------------------------------------------------- enum / typed dispatch
def parse_enum(raw: str, values: list[str]) -> Optional[str]:
    low = raw.strip().lower()
    for v in values:
        if v.lower() == low:
            return v.lower()
    return None


def parse_value(ftype: str, raw: str, values: Optional[list[str]] = None) -> tuple[bool, Optional[str]]:
    """Parse a non-empty, trimmed raw string. Returns (ok, normalised_value)."""
    if ftype == "string":
        return True, raw
    if ftype == "phone":
        v = normalise_phone(raw)
    elif ftype == "date":
        v = parse_date(raw)
    elif ftype == "decimal":
        v = parse_decimal(raw)
    elif ftype == "enum":
        v = parse_enum(raw, values or [])
    else:  # unknown type in a schema: keep the trimmed text rather than guess
        return True, raw
    return (v is not None), v


# ---------------------------------------------------------------- timestamps
def parse_timestamp_us(raw: Any) -> Optional[int]:
    """Event timestamp -> integer microseconds since the UTC epoch.

    Accepts '...Z', '...+05:30' and naive 'YYYY-MM-DD HH:MM:SS' (naive means IST).
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s[-1] in "zZ":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return (dt - _EPOCH) // _ONE_US


def us_to_ist_iso(ts_us: int) -> str:
    """Integer microseconds -> '2026-10-14T17:55:24+05:30' (whole seconds)."""
    return datetime.fromtimestamp(ts_us // 1_000_000, IST).isoformat()
