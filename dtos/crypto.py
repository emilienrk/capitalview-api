"""Crypto account and transaction schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from models.enums import CryptoTransactionType


class CryptoAccountCreate(BaseModel):
    """Create a crypto account."""
    name: str
    platform: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountUpdate(BaseModel):
    """Update a crypto account."""
    name: Optional[str] = None
    platform: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountBasicResponse(BaseModel):
    """Basic crypto account response (without positions)."""
    id: str
    name: str
    platform: Optional[str] = None
    public_address: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ============== CRYPTO TRANSACTION CRUD SCHEMAS ==============

class CryptoTransactionCreate(BaseModel):
    """Create a crypto transaction."""
    account_id: str
    symbol: str
    name: Optional[str] = None
    type: CryptoTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    fees_symbol: Optional[str] = None
    executed_at: datetime
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


class CryptoTransactionUpdate(BaseModel):
    """Update a crypto transaction."""
    symbol: Optional[str] = None
    name: Optional[str] = None
    type: Optional[CryptoTransactionType] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    price_per_unit: Optional[Decimal] = Field(None, ge=0)
    fees: Optional[Decimal] = Field(None, ge=0)
    fees_symbol: Optional[str] = None
    executed_at: Optional[datetime] = None
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


class CryptoTransactionBulkCreate(BaseModel):
    """Create a crypto transaction (without account_id, used in bulk import)."""
    symbol: str
    type: CryptoTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    fees_symbol: Optional[str] = None
    executed_at: datetime
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


class CryptoBulkImportRequest(BaseModel):
    """Bulk import multiple crypto transactions for a given account."""
    account_id: str
    transactions: list[CryptoTransactionBulkCreate]


class CryptoBulkImportResponse(BaseModel):
    """Response for bulk import of crypto transactions."""
    imported_count: int
    transactions: list["CryptoTransactionBasicResponse"]


class CryptoTransactionBasicResponse(BaseModel):
    """Basic crypto transaction response."""
    id: str
    account_id: str
    symbol: str
    type: CryptoTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    fees_symbol: Optional[str] = None
    executed_at: datetime
    tx_hash: Optional[str] = None
    notes: Optional[str] = None
