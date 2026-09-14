"""Bank account schemas."""

from datetime import datetime, date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, field_validator

from models.currency import BASE_CURRENCY, NO_CURRENCY
from models.enums import BankAccountType


class LinkStatus(str, Enum):
    """State of a linked account's consent (ruling R16).

    Machine values, never wording: the front picks the label. It used to be the
    French label itself, recognised on the other side by a regex on "reconnect",
    so rewording the badge silently changed its colour. Anything but an
    authorised session reads as needing a fresh connection.
    """
    CONNECTED = "connected"
    RECONNECT_REQUIRED = "reconnect_required"


class ReconciliationStatus(str, Enum):
    """Outcome of the reconciliation check (ruling R18). Distinct from
    LinkStatus, which describes the consent, not the curve."""
    RECONCILED = "reconciled"
    GAP = "gap"
    # A card account (ruling R19): no balance a curve could be walked back from.
    NOT_RECONCILABLE = "not_reconcilable"
    # A curve anchored on an available balance (ITAV) rather than an accounting
    # one (ruling R23). The check still runs and its gap is still stored — it is
    # the only measurement of how far the two drift apart — but a gap here is the
    # expected signature of a blocked-then-booked card payment, not a missing
    # movement. Presenting it as one would teach the user to ignore gaps.
    ESTIMATED = "estimated"


def _normalise_currency(value: str | None) -> str | None:
    """ISO 4217 alphabetic code, upper-cased.

    Deliberately not checked against a list of real codes: the set moves, and a
    bank that answers with something exotic must not make an account
    unsaveable. "XXX" — the code for "no currency", which Boursorama returns on
    the account resource — is refused, since it would silently become the
    currency a balance is read in.
    """
    if value is None:
        return None
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("La devise doit être un code ISO de trois lettres, par exemple EUR.")
    if code == NO_CURRENCY:
        raise ValueError(f"{NO_CURRENCY} ne désigne aucune devise.")
    return code


class BankAccountCreate(BaseModel):
    """Create a bank account."""
    name: str
    account_type: BankAccountType
    institution_name: str | None = None
    identifier: str | None = None
    balance: Decimal = Decimal("0")
    currency: str = BASE_CURRENCY
    opened_at: date | None = None

    _check_currency = field_validator("currency")(_normalise_currency)


class BankAccountUpdate(BaseModel):
    """Update a bank account."""
    name: str | None = None
    institution_name: str | None = None
    identifier: str | None = None
    balance: Decimal | None = None
    currency: str | None = None
    opened_at: date | None = None

    _check_currency = field_validator("currency")(_normalise_currency)


class BankAccountResponse(BaseModel):
    """Bank account response."""
    id: str
    name: str
    institution_name: str | None = None
    balance: Decimal
    currency: str
    account_type: BankAccountType
    identifier: str | None = None
    opened_at: date | None = None
    created_at: datetime
    updated_at: datetime
    balance_updated_at: date | None = None  # Last auto-sync date from cashflows
    # Bank link metadata (ruling R6), read by the Banque page to decide whether
    # to trigger POST /banking/sync after the render.
    is_linked: bool = False
    last_synced_at: date | None = None  # null = never synced
    reconciliation_gap: Decimal | None = None  # null = no gap at the last check
    link_status: LinkStatus | None = None
    # Derived, never stored; null while no check has been able to run.
    reconciliation_status: ReconciliationStatus | None = None
    # True while the bank has never answered the long history fetch: the account
    # syncs, but over a history it does not have. Distinct from last_synced_at,
    # which only says when the last call happened.
    history_pending: bool = False
    # Oldest operation date the bank served on its long history fetch: the
    # measured limit of what a linked account's curve can go back to. null =
    # never measured (a link seeded before this was recorded, or never seeded).
    history_served_from: date | None = None
    # Why the last sync failed; null once one succeeds. Kept server-side so the
    # page can say it without calling the bank again.
    sync_error: str | None = None
    # The day the bank was last called for this account, whatever the outcome.
    # The front reads it to know the daily sync is spent, failure included.
    last_sync_attempt_at: date | None = None


class BankSummaryResponse(BaseModel):
    """Summary of all bank accounts."""
    # None when a currency held has no published rate: a total that silently
    # added it one-for-one would be wrong with nothing marking it as wrong.
    total_balance: Decimal | None
    accounts: list[BankAccountResponse]


class BankHistoryEntry(BaseModel):
    """A single (date, value) data point for bank history import."""
    snapshot_date: date
    value: Decimal


class BankHistoryImportRequest(BaseModel):
    """Import historical balance snapshots for a bank account."""
    entries: list[BankHistoryEntry]
    overwrite: bool = False
