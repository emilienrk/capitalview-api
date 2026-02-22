"""Dashboard statistics schemas."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class InvestmentDistribution(BaseModel):
    """Distribution between stock and crypto investments."""
    stock_invested: Decimal
    stock_current_value: Optional[Decimal] = None
    stock_percentage: Optional[Decimal] = None
    crypto_invested: Decimal
    crypto_current_value: Optional[Decimal] = None
    crypto_percentage: Optional[Decimal] = None


class WealthBreakdown(BaseModel):
    """Breakdown of total wealth: cash, investments, assets."""
    cash: Decimal
    cash_percentage: Optional[Decimal] = None
    investments: Decimal
    investments_percentage: Optional[Decimal] = None
    assets: Decimal
    assets_percentage: Optional[Decimal] = None
    total_wealth: Decimal


class DashboardStatisticsResponse(BaseModel):
    """Aggregated dashboard statistics."""
    distribution: InvestmentDistribution
    wealth: WealthBreakdown
