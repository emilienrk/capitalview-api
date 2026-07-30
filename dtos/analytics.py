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


class BridgeStepOut(BaseModel):
    key: str
    label: str
    amount: Decimal
    """Euros this decision added or removed, versus the step before it."""


class CounterfactualResponse(BaseModel):
    baseline: Decimal
    """What a robot buying the benchmark monthly would hold today."""
    steps: list[BridgeStepOut]
    residual: Decimal
    """Whatever the steps fail to explain. Shown, never absorbed into a step."""
    final: Decimal
    behaviour_cost: Decimal
    """Sum of the decision terms. Idle cash sits on both ends, so it is excluded."""
    idle_cash: Decimal
    """Money deposited but never deployed. Not a decision term — reported apart."""
    idle_cash_opportunity: Decimal | None = None
    """What that idle cash would have earned on the benchmark."""
    covered_from: date
    covered_days: int
    truncated: bool
    """True when the benchmark is younger than the history and the window moved."""
    order: list[str]
    """The substitution order. The decomposition is path dependent, so it is stated."""
    verdict: str


class SlippageDistributionOut(BaseModel):
    minimum: Decimal
    q1: Decimal
    median: Decimal
    q3: Decimal
    maximum: Decimal


class ExecutionResponse(BaseModel):
    slippage_bps: MetricOut
    """Notional-weighted gap between prices paid and each order's monthly average."""
    cost_eur: MetricOut
    order_count: int
    distribution: SlippageDistributionOut | None = None
    p_value: Decimal | None = None
    percentile: Decimal | None = None
    is_detectable: bool = False
    """False means the pattern is indistinguishable from chance — say nothing."""
    verdict: str


class InvestorAnalyticsResponse(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    days: int
    benchmark_asset_key: str
    investor_gap: InvestorGapResponse | None = None
    counterfactual: CounterfactualResponse | None = None
    execution: ExecutionResponse | None = None
