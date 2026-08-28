"""Bank account schemas."""

from datetime import datetime, date
from decimal import Decimal

from pydantic import BaseModel, field_validator

from models.enums import BankAccountType


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
    if code == "XXX":
        raise ValueError("XXX ne désigne aucune devise.")
    return code


class BankAccountCreate(BaseModel):
    """Create a bank account."""
    name: str
    account_type: BankAccountType
    institution_name: str | None = None
    identifier: str | None = None
    balance: Decimal = Decimal("0")
    currency: str = "EUR"
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
    link_status: str | None = None  # consent state, displayed as-is
    # `reconciled` | `gap` | `not_reconcilable` (ruling R18), derived, never
    # stored. Distinct from link_status, which is the consent state.
    reconciliation_status: str | None = None


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
