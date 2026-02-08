"""Bank account schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from models.enums import BankAccountType


class BankAccountCreate(BaseModel):
    """Create a bank account."""
    name: str
    account_type: BankAccountType
    institution_name: Optional[str] = None
    identifier: Optional[str] = None
    balance: Decimal = Decimal("0")


class BankAccountUpdate(BaseModel):
    """Update a bank account."""
    name: Optional[str] = None
    institution_name: Optional[str] = None
    identifier: Optional[str] = None
    balance: Optional[Decimal] = None


class BankAccountResponse(BaseModel):
    """Bank account response."""
    id: str
    name: str
    institution_name: Optional[str] = None
    balance: Decimal
    account_type: BankAccountType
    identifier: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BankSummaryResponse(BaseModel):
    """Summary of all bank accounts."""
    total_balance: Decimal
    accounts: list[BankAccountResponse]
