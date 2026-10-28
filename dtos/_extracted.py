"""Lenient readers for what a model extracts from a photo.

A provider that holds the answer to the JSON schema (Gemini, Claude) returns
"20.87" and "2026-10-09T15:23:09"; one that only keeps it to JSON (DeepSeek)
may hand back what the screen shows: "20,87 €", "09/10/2026 15:23:09", null
fees. Each such transaction used to fail validation and be dropped unseen.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BeforeValidator

_NOT_NUMERIC = re.compile(r"[^\d.,+-]")
_FRENCH_DATETIME = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T,à]+(\d{1,2})[:h](\d{2})(?::(\d{2}))?)?$"
)


def _read_decimal(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = _NOT_NUMERIC.sub("", value)
    if not text:
        return None
    if "," in text and "." in text:
        # The separator written last is the decimal one: "1.234,56" / "1,234.56"
        thousands = "." if text.rfind(",") > text.rfind(".") else ","
        text = text.replace(thousands, "")
    return text.replace(",", ".")


def _read_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    match = _FRENCH_DATETIME.match(value.strip())
    if not match:
        return value
    day, month, year, hour, minute, second = match.groups()
    return datetime(
        int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0)
    )


def _read_upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


ReadDecimal = Annotated[Decimal, BeforeValidator(_read_decimal)]
ReadDecimalOpt = Annotated[Decimal | None, BeforeValidator(_read_decimal)]
# Fees a model leaves out or nulls are none paid
ReadFees = Annotated[Decimal, BeforeValidator(lambda v: _read_decimal(v) or Decimal("0"))]
ReadDatetimeInput = BeforeValidator(_read_datetime)
ReadUpper = BeforeValidator(_read_upper)


def describe_rejection(exc: Exception) -> str:
    """Name the field a rejected transaction failed on, for the user to read."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        first = errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "?"
        return f"{field} : {first.get('msg')}"
    return str(exc)
