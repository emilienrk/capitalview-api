"""Shared date validation, UTC normalization and serialization for DTOs.

Convention: UTC is the single source of truth on the backend. Incoming aware
datetimes are converted to naive UTC; naive datetimes are assumed to already
be UTC (the frontend converts the user's local input before sending).
Outgoing datetimes are serialized with an explicit "Z" suffix so clients can
convert to their display timezone.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer

# Tolerated client/server clock drift when rejecting future datetimes.
_FUTURE_SKEW = timedelta(minutes=5)


def to_naive_utc(v: datetime) -> datetime:
    """Convert any datetime to naive UTC (the backend-internal representation)."""
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _normalize_and_validate(v):
    if v is None:
        return v
    if isinstance(v, datetime):
        v = to_naive_utc(v)
        if v.year < 2000:
            raise ValueError("La date ne peut pas être avant 2000.")
        if v > datetime.now(timezone.utc).replace(tzinfo=None) + _FUTURE_SKEW:
            raise ValueError("La date ne peut pas être dans le futur.")
    elif isinstance(v, date):
        if v.year < 2000:
            raise ValueError("La date ne peut pas être avant 2000.")
        # Civil dates carry no timezone: allow one day past the UTC date so
        # users ahead of UTC can still enter "today".
        if v > datetime.now(timezone.utc).date() + timedelta(days=1):
            raise ValueError("La date ne peut pas être dans le futur.")
    return v


def serialize_utc(v: datetime) -> str:
    """Serialize a (naive-UTC or aware) datetime as ISO 8601 with a Z suffix."""
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


ValidDateOpt = Annotated[date | None, AfterValidator(_normalize_and_validate)]
ValidDateReq = Annotated[date, AfterValidator(_normalize_and_validate)]
ValidDatetime = Annotated[datetime, AfterValidator(_normalize_and_validate)]
ValidDatetimeOpt = Annotated[datetime | None, AfterValidator(_normalize_and_validate)]

# Response-side datetime: kept naive-UTC in Python, rendered with "Z" in JSON.
UtcDatetimeOut = Annotated[datetime, PlainSerializer(serialize_utc, when_used="json")]
