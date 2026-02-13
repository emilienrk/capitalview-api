"""Stock account and transaction schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

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
    name: Optional[str] = None
    exchange: Optional[str] = None
    type: StockTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    executed_at: datetime
    notes: Optional[str] = None


class StockTransactionUpdate(BaseModel):
    """Update a stock transaction."""
    symbol: Optional[str] = None
    isin: Optional[str] = None
    name: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[StockTransactionType] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    price_per_unit: Optional[Decimal] = Field(None, ge=0)
    fees: Optional[Decimal] = Field(None, ge=0)
    executed_at: Optional[datetime] = None
    notes: Optional[str] = None


class StockTransactionBulkCreate(BaseModel):
    """Create a stock transaction (without account_id, used in bulk import)."""
    symbol: str
    isin: Optional[str] = None
    name: Optional[str] = None
    exchange: Optional[str] = None
    type: StockTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
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
    symbol: Optional[str] = None
    isin: Optional[str] = None
    name: Optional[str] = None
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
    isin: Optional[str] = None
    name: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[str] = None
    currency: Optional[str] = None


class AssetInfoResponse(BaseModel):
    """Detailed info for an asset."""
    symbol: str
    isin: Optional[str] = None
    name: Optional[str] = None
    price: Optional[Decimal] = None
    currency: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[str] = None
    change_percent: Optional[float] = None  # 24h or daily change
