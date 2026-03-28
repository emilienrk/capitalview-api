"""Dashboard statistics schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from dtos.transaction import PortfolioResponse
from dtos.cashflow import CashflowBalanceResponse


class InvestmentDistribution(BaseModel):
    """Distribution between stock and crypto investments."""
    stock_invested: Decimal
    stock_current_value: Decimal | None = None
    stock_percentage: Decimal | None = None
    crypto_invested: Decimal
    crypto_current_value: Decimal | None = None
    crypto_percentage: Decimal | None = None


class WealthBreakdown(BaseModel):
    """Breakdown of total wealth: cash, investments, assets."""
    cash: Decimal
    cash_percentage: Decimal | None = None
    investments: Decimal
    investments_percentage: Decimal | None = None
    assets: Decimal
    assets_percentage: Decimal | None = None
    total_wealth: Decimal


class DashboardStatisticsResponse(BaseModel):
    """Aggregated dashboard statistics."""
    distribution: InvestmentDistribution
    wealth: WealthBreakdown


class DashboardSummaryResponse(BaseModel):
    """Complete financial summary for AI agent consumption."""
    statistics: DashboardStatisticsResponse
    portfolio: PortfolioResponse
    cashflow: CashflowBalanceResponse


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
