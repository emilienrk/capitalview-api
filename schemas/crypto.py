"""Crypto account and transaction schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from models.enums import CryptoTransactionType


class CryptoAccountCreate(BaseModel):
    """Create a crypto account."""
    name: str
    wallet_name: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountUpdate(BaseModel):
    """Update a crypto account."""
    name: Optional[str] = None
    wallet_name: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountBasicResponse(BaseModel):
    """Basic crypto account response (without positions)."""
    id: int
    name: str
    wallet_name: Optional[str] = None
    public_address: Optional[str] = None
    created_at: datetime


# ============== CRYPTO TRANSACTION CRUD SCHEMAS ==============

class CryptoTransactionCreate(BaseModel):
    """Create a crypto transaction."""
    account_id: int
    ticker: str
    type: CryptoTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal = Decimal("0")
    fees_ticker: Optional[str] = None
    executed_at: datetime


class CryptoTransactionUpdate(BaseModel):
    """Update a crypto transaction."""
    ticker: Optional[str] = None
    type: Optional[CryptoTransactionType] = None
    amount: Optional[Decimal] = None
    price_per_unit: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    fees_ticker: Optional[str] = None
    executed_at: Optional[datetime] = None


class CryptoTransactionBulkCreate(BaseModel):
    """Create a crypto transaction (without account_id, used in bulk import)."""
    ticker: str
    type: CryptoTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal = Decimal("0")
    fees_ticker: Optional[str] = None
    executed_at: datetime


class CryptoBulkImportRequest(BaseModel):
    """Bulk import multiple crypto transactions for a given account."""
    account_id: int
    transactions: list[CryptoTransactionBulkCreate]


class CryptoBulkImportResponse(BaseModel):
    """Response for bulk import of crypto transactions."""
    imported_count: int
    transactions: list["CryptoTransactionBasicResponse"]


class CryptoTransactionBasicResponse(BaseModel):
    """Basic crypto transaction response."""
    id: int
    account_id: int
    ticker: str
    type: CryptoTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    fees_ticker: Optional[str] = None
    executed_at: datetime
