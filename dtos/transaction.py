"""Transaction and portfolio schemas (shared between stock and crypto)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel



class TransactionResponse(BaseModel):
    """Base transaction response with calculated fields."""
    id: str
    name: Optional[str] = None
    symbol: Optional[str] = None
    isin: Optional[str] = None
    exchange: Optional[str] = None
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: datetime
    currency: str = "EUR"
    
    notes: Optional[str] = None
    total_cost: Decimal
    fees_percentage: Decimal
    group_uuid: Optional[str] = None
    current_price: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None


class PositionResponse(BaseModel):
    """Aggregated position for a single asset."""
    symbol: str
    name: Optional[str] = None
    isin: Optional[str] = None
    exchange: Optional[str] = None
    total_amount: Decimal
    average_buy_price: Decimal
    total_invested: Decimal
    total_fees: Decimal
    fees_percentage: Decimal
    total_dividends: Decimal = Decimal("0")
    currency: str = "EUR"

    current_price: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None


class AccountSummaryResponse(BaseModel):
    """Summary of an account with all positions."""
    account_id: str
    account_name: str
    account_type: str
    total_invested: Decimal
    total_deposits: Decimal = Decimal("0")
    total_fees: Decimal
    total_dividends: Decimal = Decimal("0")
    currency: str = "EUR"
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None
    positions: list[PositionResponse]


class PortfolioResponse(BaseModel):
    """Global portfolio summary."""
    total_invested: Decimal
    total_fees: Decimal
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None
    accounts: list[AccountSummaryResponse]


class AccountHistoryPosition(BaseModel):
    """Single asset position within a daily snapshot."""
    symbol: str
    quantity: Decimal
    value: Decimal
    price: Optional[Decimal] = None
    invested: Decimal
    percentage: Decimal


class AccountHistorySnapshotResponse(BaseModel):
    """Decrypted daily snapshot for an account."""
    snapshot_date: date
    total_value: Decimal
    total_invested: Decimal
    daily_pnl: Optional[Decimal] = None
    positions: Optional[list[AccountHistoryPosition]] = None