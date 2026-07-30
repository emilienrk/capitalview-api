"""Investor behaviour analytics schemas."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class MetricOut(BaseModel):
    """A single number plus how far it can be trusted.

    `value` is None whenever `reliability` is "insuffisant": an unreliable figure
    never crosses the wire, so it cannot be misread on the client.
    """

    value: Decimal | None = None
    unit: str
    sample_size: int
    reliability: str
    caveat: str | None = None


class InvestorGapResponse(BaseModel):
    twr: MetricOut
    """Cumulative time-weighted return — the strategy's performance."""
    twr_annualised: MetricOut
    benchmark_annualised: MetricOut
    """Benchmark total return over the exact same window."""
    mwr: MetricOut
    """Money-weighted return — what the investor's euros actually earned."""
    gap: MetricOut
    """mwr - twr_annualised. Negative means the timing of the money hurt."""
    gap_eur: MetricOut
    """The gap applied to the average capital at work."""
    average_capital: Decimal
    auto_provision_share: Decimal
    """Share of deposits the app generated itself; bounds how much the gap means."""
    verdict: str


class InvestorAnalyticsResponse(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    days: int
    benchmark_asset_key: str
    investor_gap: InvestorGapResponse | None = None
