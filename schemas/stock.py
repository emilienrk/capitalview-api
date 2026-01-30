"""Stock account and transaction schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class StockAccountCreate(BaseModel):
    """Create a stock account."""
    user_id: int
    name: str
    account_type: str  # PEA, CTO, PEA_PME
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None


class StockAccountUpdate(BaseModel):
    """Update a stock account."""
    name: Optional[str] = None
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None


class StockAccountBasicResponse(BaseModel):
    """Basic stock account response (without positions)."""
    id: int
    user_id: int
    name: str
    account_type: str
    bank_name: Optional[str] = None
    created_at: datetime


class StockTransactionCreate(BaseModel):
    """Create a stock transaction."""
    account_id: int
    ticker: str
    exchange: Optional[str] = None
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal = Decimal("0")
    executed_at: datetime


class StockTransactionUpdate(BaseModel):
    """Update a stock transaction."""
    ticker: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[Decimal] = None
    price_per_unit: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    executed_at: Optional[datetime] = None


class StockTransactionBasicResponse(BaseModel):
    """Basic stock transaction response."""
    id: int
    account_id: int
    ticker: str
    exchange: Optional[str] = None
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: datetime
