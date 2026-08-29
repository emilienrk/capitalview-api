"""
Error taxonomy for the Enable Banking API client.

Enable Banking's HTTP status codes are not a reliable signal: session expiry
surfaces as 401, but 401 covers other causes too (spec §B5). Callers must
branch on the business `error` code in the response body instead, so every
exception here is keyed by that code and grouped into the reaction families
the spec lists — reconnect, restart the auth journey, reframe the date
window, wait out a quota, retry with backoff, or reload the ASPSP catalogue.

The grouping below is our own reading of the ErrorCode enum
(`vendor-docs/enablebanking-api.yaml`) against the families in §B5 — the API
does not label codes with a family itself.
"""

from __future__ import annotations

import re
from datetime import date

import httpx

_EARLIEST_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class BankingApiError(Exception):
    """Base exception for all Enable Banking business errors, keyed by `error` code."""

    def __init__(self, code: str, message: str, detail: str | None = None):
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(f"{code}: {message}")


class SessionInvalidError(BankingApiError):
    """Session expired, revoked, closed, cancelled or unknown — reconnect, keep the link."""


class AuthorizationInvalidError(BankingApiError):
    """Auth code expired/wrong, or session already authorized — restart the auth journey."""


class RedirectNotAllowedError(BankingApiError):
    """Redirect URL isn't declared on the application — a configuration error."""


class PsuHeaderError(BankingApiError):
    """A PSU context header is missing or invalid — the all-or-nothing rule was broken."""


class InvalidPeriodError(BankingApiError):
    """
    The requested transaction period is unavailable.

    On WRONG_TRANSACTIONS_PERIOD the API states the earliest allowed date in
    free text (message/detail) rather than a dedicated field. `earliest_allowed_date`
    best-effort extracts it so a caller can reframe on it instead of guessing.
    """

    def __init__(self, code: str, message: str, detail: str | None = None):
        super().__init__(code, message, detail)
        self.earliest_allowed_date = _extract_date(detail) or _extract_date(message)


class InvalidContinuationKeyError(BankingApiError):
    """Continuation key rejected — pagination must restart from scratch."""


class AspspQuotaExceededError(BankingApiError):
    """The ASPSP's background-fetch quota is exhausted — wait roughly six hours."""


class AspspUnavailableError(BankingApiError):
    """ASPSP-side error or timeout — retry with backoff (1 min, 1 h, 2 h, 4 h)."""


class UnknownAspspError(BankingApiError):
    """Bank name/country pair not recognised — reload the catalogue, it may have rebranded."""


_ERROR_CODE_MAP: dict[str, type[BankingApiError]] = {
    "EXPIRED_SESSION": SessionInvalidError,
    "REVOKED_SESSION": SessionInvalidError,
    "CLOSED_SESSION": SessionInvalidError,
    "SESSION_DOES_NOT_EXIST": SessionInvalidError,
    "WRONG_SESSION_STATUS": SessionInvalidError,
    "EXPIRED_AUTHORIZATION_CODE": AuthorizationInvalidError,
    "WRONG_AUTHORIZATION_CODE": AuthorizationInvalidError,
    "ALREADY_AUTHORIZED": AuthorizationInvalidError,
    "REDIRECT_URI_NOT_ALLOWED": RedirectNotAllowedError,
    "PSU_HEADER_NOT_PROVIDED": PsuHeaderError,
    "PSU_HEADER_INVALID": PsuHeaderError,
    "WRONG_TRANSACTIONS_PERIOD": InvalidPeriodError,
    "DATE_FROM_IN_FUTURE": InvalidPeriodError,
    "DATE_TO_WITHOUT_DATE_FROM": InvalidPeriodError,
    "WRONG_DATE_INTERVAL": InvalidPeriodError,
    "WRONG_CONTINUATION_KEY": InvalidContinuationKeyError,
    "ASPSP_RATE_LIMIT_EXCEEDED": AspspQuotaExceededError,
    "ASPSP_ERROR": AspspUnavailableError,
    "ASPSP_TIMEOUT": AspspUnavailableError,
    "WRONG_ASPSP_PROVIDED": UnknownAspspError,
}


def build_banking_api_error(code: str, message: str, detail: str | None) -> BankingApiError:
    """Instantiate the exception family matching a business error code, or the base class."""
    error_cls = _ERROR_CODE_MAP.get(code, BankingApiError)
    return error_cls(code, message, detail)


def error_from_response(response: httpx.Response) -> BankingApiError:
    """Parse an Enable Banking ErrorResponse body into a typed exception."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("error") or "UNKNOWN_ERROR"
    message = body.get("message") or response.text or f"HTTP {response.status_code}"
    detail = body.get("detail")
    return build_banking_api_error(code, message, detail)


def _extract_date(text: str | None) -> date | None:
    if not text:
        return None
    match = _EARLIEST_DATE_RE.search(text)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group())
    except ValueError:
        return None


class PaginationLimitExceededError(Exception):
    """
    Client-side guard, not an API error: raised when a transaction feed exceeds
    the page bound without ever exhausting its continuation key, so a repeating
    key can't loop forever (spec §B3).
    """
