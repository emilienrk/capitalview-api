"""Transaction and portfolio schemas (shared between stock and crypto)."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from dtos._dates import UtcDatetimeOut


class TransactionResponse(BaseModel):
    """Base transaction response with calculated fields."""
    id: str
    asset_key: str
    # Stock history only: the market name, so a sold line still reads as a name.
    name: str | None = None
    symbol: str | None = None
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: UtcDatetimeOut
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
    asset_key: str
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


class OrderFeesResponse(BaseModel):
    """Brokerage paid on an account's orders, for the account's own figure."""
    recorded: Decimal
    """Fees keyed in on buys and sells."""
    estimated: Decimal | None = None
    """The whole bill once unrecorded buy fees are extrapolated; null when the
    recorded fees are complete, or too few to stand in for the rest."""
    buy_orders: int = 0
    buy_orders_with_fee: int = 0


class NegativeBalanceResponse(BaseModel):
    """A crypto whose ledger balance went below zero: a missing or duplicated transaction."""
    asset_key: str
    since: UtcDatetimeOut
    """First transaction that took the balance below zero."""
    shortfall: Decimal
    """Largest quantity missing at any point."""
    shortfall_value: Decimal | None = None
    """That quantity at the current price, to tell dust from a real gap."""
    excluded_proceeds: Decimal
    """Euro value of the disposals the ledger could not cover. Their cost is
    unknown, so they are kept out of the realized P/L."""


class AccountSummaryResponse(BaseModel):
    """Summary of an account with all positions."""
    total_invested: Decimal
    total_deposits: Decimal = Decimal("0")
    total_withdrawals: Decimal = Decimal("0")
    total_fees: Decimal
    total_dividends: Decimal = Decimal("0")
    currency: str = "EUR"
    current_value: Decimal | None = None
    cash_balance: Decimal = Decimal("0")
    profit_loss: Decimal | None = None
    profit_loss_percentage: Decimal | None = None
    realized_profit_loss: Decimal | None = None
    total_profit_loss: Decimal | None = None
    order_fees: OrderFeesResponse | None = None
    negative_balances: list[NegativeBalanceResponse] = []
    positions: list[PositionResponse]


class PortfolioAccountSummaryResponse(AccountSummaryResponse):
    """Account summary enriched with portfolio-level account metadata."""
    account_id: str
    account_name: str
    account_type: str


class PortfolioResponse(BaseModel):
    """Global portfolio summary."""
    total_invested: Decimal
    total_deposits: Decimal = Decimal("0")
    total_withdrawals: Decimal = Decimal("0")
    total_fees: Decimal
    current_value: Decimal | None = None
    profit_loss: Decimal | None = None
    profit_loss_percentage: Decimal | None = None
    accounts: list[PortfolioAccountSummaryResponse]


class AccountHistoryPosition(BaseModel):
    """Single asset position within a daily snapshot."""
    asset_key: str
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
    total_deposits: Decimal = Decimal("0")
    total_withdrawals: Decimal = Decimal("0")
    total_fees: Decimal | None = None
    total_dividends: Decimal | None = None
    daily_pnl: Decimal | None = None
    cumulative_pnl: Decimal | None = None
    uncounted_cash: Decimal = Decimal("0")
    positions: list[AccountHistoryPosition] | None = None