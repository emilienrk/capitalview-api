"""Transaction and portfolio schemas (shared between stock and crypto)."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel



class TransactionResponse(BaseModel):
    """Base transaction response with calculated fields."""
    id: str
    name: str | None = None
    symbol: str | None = None
    isin: str | None = None
    exchange: str | None = None
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: datetime
    currency: str = "EUR"
    
    notes: str | None = None
    total_cost: Decimal
    fees_percentage: Decimal
    group_uuid: str | None = None
    current_price: Decimal | None = None
    current_value: Decimal | None = None
    profit_loss: Decimal | None = None
    profit_loss_percentage: Decimal | None = None


class PositionResponse(BaseModel):
    """Aggregated position for a single asset."""
    symbol: str
    name: str | None = None
    isin: str | None = None
    exchange: str | None = None
    total_amount: Decimal
    average_buy_price: Decimal
    total_invested: Decimal
    total_fees: Decimal
    fees_percentage: Decimal
    total_dividends: Decimal = Decimal("0")
    currency: str = "EUR"

    current_price: Decimal | None = None
    current_value: Decimal | None = None
    profit_loss: Decimal | None = None
    profit_loss_percentage: Decimal | None = None


class AccountSummaryResponse(BaseModel):
    """Summary of an account with all positions."""
    total_invested: Decimal
    total_deposits: Decimal = Decimal("0")
    total_fees: Decimal
    total_dividends: Decimal = Decimal("0")
    currency: str = "EUR"
    current_value: Decimal | None = None
    profit_loss: Decimal | None = None
    profit_loss_percentage: Decimal | None = None
    positions: list[PositionResponse]


class PortfolioAccountSummaryResponse(AccountSummaryResponse):
    """Account summary enriched with portfolio-level account metadata."""
    account_id: str
    account_name: str
    account_type: str


class PortfolioResponse(BaseModel):
    """Global portfolio summary."""
    total_invested: Decimal
    total_fees: Decimal
    current_value: Decimal | None = None
    profit_loss: Decimal | None = None
    profit_loss_percentage: Decimal | None = None
    accounts: list[PortfolioAccountSummaryResponse]


class AccountHistoryPosition(BaseModel):
    """Single asset position within a daily snapshot."""
    symbol: str
    quantity: Decimal
    value: Decimal
    price: Decimal | None = None
    invested: Decimal
    percentage: Decimal


class AccountHistorySnapshotResponse(BaseModel):
    """Decrypted daily snapshot for an account."""
    snapshot_date: date
    total_value: Decimal
    total_invested: Decimal
    daily_pnl: Decimal | None = None
    positions: list[AccountHistoryPosition] | None = None