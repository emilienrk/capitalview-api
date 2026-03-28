"""Bank account schemas."""

from datetime import datetime, date
from decimal import Decimal

from pydantic import BaseModel

from models.enums import BankAccountType


class BankAccountCreate(BaseModel):
    """Create a bank account."""
    name: str
    account_type: BankAccountType
    institution_name: str | None = None
    identifier: str | None = None
    balance: Decimal = Decimal("0")
    opened_at: date | None = None


class BankAccountUpdate(BaseModel):
    """Update a bank account."""
    name: str | None = None
    institution_name: str | None = None
    identifier: str | None = None
    balance: Decimal | None = None
    opened_at: date | None = None


class BankAccountResponse(BaseModel):
    """Bank account response."""
    id: str
    name: str
    institution_name: str | None = None
    balance: Decimal
    account_type: BankAccountType
    identifier: str | None = None
    opened_at: date | None = None
    created_at: datetime
    updated_at: datetime
    balance_updated_at: date | None = None  # Last auto-sync date from cashflows


class BankSummaryResponse(BaseModel):
    """Summary of all bank accounts."""
    total_balance: Decimal
    accounts: list[BankAccountResponse]


class BankHistoryEntry(BaseModel):
    """A single (date, value) data point for bank history import."""
    snapshot_date: date
    value: Decimal


class BankHistoryImportRequest(BaseModel):
    """Import historical balance snapshots for a bank account."""
    entries: list[BankHistoryEntry]
    overwrite: bool = False
