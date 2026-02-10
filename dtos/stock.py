"""Stock account and transaction schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from models.enums import StockAccountType, StockTransactionType


class StockAccountCreate(BaseModel):
    """Create a stock account."""
    name: str
    account_type: StockAccountType
    institution_name: Optional[str] = None
    identifier: Optional[str] = None


class StockAccountUpdate(BaseModel):
    """Update a stock account."""
    name: Optional[str] = None
    institution_name: Optional[str] = None
    identifier: Optional[str] = None


class StockAccountBasicResponse(BaseModel):
    """Basic stock account response (without positions)."""
    id: str
    name: str
    account_type: StockAccountType
    institution_name: Optional[str] = None
    identifier: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class StockTransactionCreate(BaseModel):
    """Create a stock transaction."""
    account_id: str
    symbol: str
    isin: Optional[str] = None
    exchange: Optional[str] = None
    type: StockTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal = Decimal("0")
    executed_at: datetime
    notes: Optional[str] = None


class StockTransactionUpdate(BaseModel):
    """Update a stock transaction."""
    symbol: Optional[str] = None
    isin: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[StockTransactionType] = None
    amount: Optional[Decimal] = None
    price_per_unit: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    executed_at: Optional[datetime] = None
    notes: Optional[str] = None


class StockTransactionBulkCreate(BaseModel):
    """Create a stock transaction (without account_id, used in bulk import)."""
    symbol: str
    exchange: Optional[str] = None
    type: StockTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal = Decimal("0")
    executed_at: datetime
    notes: Optional[str] = None


class StockBulkImportRequest(BaseModel):
    """Bulk import multiple stock transactions for a given account."""
    account_id: str
    transactions: list[StockTransactionBulkCreate]


class StockBulkImportResponse(BaseModel):
    """Response for bulk import of stock transactions."""
    imported_count: int
    transactions: list["StockTransactionBasicResponse"]


class StockTransactionBasicResponse(BaseModel):
    """Basic stock transaction response."""
    id: str
    account_id: str
    symbol: str
    exchange: Optional[str] = None
    type: StockTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: datetime
    notes: Optional[str] = None


class AssetSearchResult(BaseModel):
    """Result of an asset search."""
    symbol: str
    name: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[str] = None
    currency: Optional[str] = None


class AssetInfoResponse(BaseModel):
    """Detailed info for an asset."""
    symbol: str
    name: Optional[str] = None
    price: Optional[Decimal] = None
    currency: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[str] = None
    change_percent: Optional[float] = None  # 24h or daily change
