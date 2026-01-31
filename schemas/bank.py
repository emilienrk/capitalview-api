"""Bank account schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class BankAccountCreate(BaseModel):
    """Create a bank account."""
    name: str
    account_type: str
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None
    balance: Decimal = Decimal("0")


class BankAccountUpdate(BaseModel):
    """Update a bank account."""
    name: Optional[str] = None
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None
    balance: Optional[Decimal] = None


class BankAccountResponse(BaseModel):
    """Bank account response."""
    id: int
    name: str
    bank_name: Optional[str] = None
    balance: Decimal
    account_type: str
    updated_at: datetime


class BankSummaryResponse(BaseModel):
    """Summary of all bank accounts."""
    total_balance: Decimal
    accounts: list[BankAccountResponse]
