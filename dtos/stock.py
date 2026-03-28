"""Stock account and transaction schemas."""

from datetime import datetime, date
from decimal import Decimal

from pydantic import BaseModel, Field

from models.enums import StockAccountType, StockTransactionType


class StockAccountCreate(BaseModel):
    """Create a stock account."""
    name: str
    account_type: StockAccountType
    institution_name: str | None = None
    identifier: str | None = None
    opened_at: date | None = None


class StockAccountUpdate(BaseModel):
    """Update a stock account."""
    name: str | None = None
    institution_name: str | None = None
    identifier: str | None = None
    opened_at: date | None = None


class StockAccountBasicResponse(BaseModel):
    """Basic stock account response (without positions)."""
    id: str
    name: str
    account_type: StockAccountType
    institution_name: str | None = None
    identifier: str | None = None
    opened_at: date | None = None
    created_at: datetime
    updated_at: datetime


class EurDepositCreate(BaseModel):
    """Deposit EUR cash into a stock account."""
    amount: Decimal = Field(gt=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    executed_at: datetime
    notes: str | None = None


class StockTransactionCreate(BaseModel):
    """Create a stock transaction."""
    account_id: str
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    exchange: str | None = None
    type: StockTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    executed_at: datetime
    notes: str | None = None


class StockTransactionUpdate(BaseModel):
    """Update a stock transaction."""
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    exchange: str | None = None
    type: StockTransactionType | None = None
    amount: Decimal | None = Field(None, gt=0)
    price_per_unit: Decimal | None = Field(None, ge=0)
    fees: Decimal | None = Field(None, ge=0)
    executed_at: datetime | None = None
    notes: str | None = None


class StockTransactionBulkCreate(BaseModel):
    """Create a stock transaction (without account_id, used in bulk import).
    Only DB-stored fields: isin, type, amount, price_per_unit, fees, executed_at, notes.
    symbol/name/exchange are resolved automatically via market_prices API."""
    isin: str
    type: StockTransactionType
    amount: Decimal = Field(gt=0)
    price_per_unit: Decimal = Field(ge=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    executed_at: datetime
    notes: str | None = None


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
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    exchange: str | None = None
    type: StockTransactionType
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: datetime
    notes: str | None = None


class AssetSearchResult(BaseModel):
    """Result of an asset search."""
    symbol: str
    isin: str | None = None
    name: str | None = None
    exchange: str | None = None
    type: str | None = None
    currency: str | None = None


class AssetInfoResponse(BaseModel):
    """Detailed info for an asset."""
    symbol: str
    isin: str | None = None
    name: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    exchange: str | None = None
    type: str | None = None
    change_percent: float | None = None  # 24h or daily change
