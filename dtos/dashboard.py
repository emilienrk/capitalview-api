"""Dashboard statistics schemas."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from dtos.transaction import PortfolioResponse
from dtos.cashflow import CashflowBalanceResponse
from dtos.projection import ProjectionResponse


class InvestmentDistribution(BaseModel):
    """Distribution between stock and crypto investments."""
    stock_invested: Decimal
    stock_current_value: Decimal | None = None
    stock_percentage: Decimal | None = None
    crypto_invested: Decimal
    crypto_current_value: Decimal | None = None
    crypto_percentage: Decimal | None = None
    placements_invested: Decimal = Decimal("0")
    placements_current_value: Decimal = Decimal("0")
    placements_percentage: Decimal | None = None
    total_deposits: Decimal = Decimal("0")
    total_withdrawals: Decimal = Decimal("0")


class WealthBreakdown(BaseModel):
    """Breakdown of total wealth: cash, investments, assets."""
    cash: Decimal
    cash_percentage: Decimal | None = None
    investments: Decimal
    investments_percentage: Decimal | None = None
    assets: Decimal
    assets_percentage: Decimal | None = None
    total_deposits: Decimal = Decimal("0")
    total_withdrawals: Decimal = Decimal("0")
    total_wealth: Decimal


class NetWorthChange(BaseModel):
    """How far the total moved since a dated snapshot, deposits included."""
    reference: Literal["last_snapshot", "month_start", "year_start"]
    since: date
    change: float
    change_pct: float | None = None


class DashboardStatisticsResponse(BaseModel):
    """Aggregated dashboard statistics."""
    distribution: InvestmentDistribution
    wealth: WealthBreakdown
    changes: list[NetWorthChange] = []


class DashboardSummaryResponse(BaseModel):
    """Complete financial summary for AI agent consumption."""
    statistics: DashboardStatisticsResponse
    portfolio: PortfolioResponse
    cashflow: CashflowBalanceResponse
    projection: ProjectionResponse | None = None


class GlobalHistorySnapshotResponse(BaseModel):
    """
    Aggregated daily snapshot of total wealth across all account types.
    No positions included — lightweight overview for charts.
    """
    snapshot_date: date
    total_wealth: Decimal
    stock_value: Decimal
    crypto_value: Decimal
    bank_value: Decimal
    assets_value: Decimal
    placements_value: Decimal = Decimal("0")

class CardResponse(BaseModel):
    uuid: str
    title: str
    theme: str
    text: str
    scope: str
    created_at: date | datetime
