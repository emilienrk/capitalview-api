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




class MonthlyAmountOut(BaseModel):
    year: int
    month: int
    amount: Decimal


class RegularityResponse(BaseModel):
    """What the purchase rhythm actually is, month by month."""

    monthly: list[MonthlyAmountOut] = []
    """Every month of the window, zeros included. Empty when the gate withheld it."""
    months_total: int
    months_invested: int
    purchase_count: int
    invested_share: MetricOut
    variation_coefficient: MetricOut
    longest_gap_months: MetricOut
    temporal_hhi: MetricOut
    equivalent_monthly_purchases: MetricOut
    """1/HHI: how many equal monthly purchases the real pattern amounts to."""
    day_of_month_spread: MetricOut
    median_day_of_month: int | None = None
    verdict: str


class DepositLagResponse(BaseModel):
    """How long deposited money waits before it is invested."""

    median_days: MetricOut
    q1_days: MetricOut
    q3_days: MetricOut
    p90_days: MetricOut
    matched_eur: Decimal
    unmatched_eur: Decimal
    """Purchase euros no real deposit could have funded — auto-provisions, mostly."""
    unmatched_share: Decimal
    never_invested_eur: Decimal
    deposit_variation: MetricOut
    """Same indicator as the purchases, so the two rhythms can be compared."""
    purchase_variation: MetricOut
    idle_cash_opportunity: Decimal | None = None
    """What the waiting cash gave up, taken from the counterfactual bridge."""
    verdict: str


class DensityBinOut(BaseModel):
    centre: Decimal
    purchase_share: Decimal
    session_share: Decimal


class MarketPointOut(BaseModel):
    day: date
    amount: Decimal
    drawdown: Decimal


class YearlyDrawdownOut(BaseModel):
    label: str
    drawdown: Decimal


class MarketConditioningResponse(BaseModel):
    """Where the investor's euros enter, versus where a random day sits."""

    weighted_drawdown: MetricOut
    unconditional_drawdown: MetricOut
    weighted_momentum: MetricOut
    unconditional_momentum: MetricOut
    density: list[DensityBinOut] = []
    points: list[MarketPointOut] = []
    yearly: list[YearlyDrawdownOut] = []
    """12-month buckets. Trend, not proof — the UI must say so."""
    p_value: Decimal | None = None
    percentile: Decimal | None = None
    is_detectable: bool = False
    sessions: int
    verdict: str


class TurnoverOut(BaseModel):
    annual_rate: MetricOut
    purchases_eur: Decimal
    sales_eur: Decimal


class AssetLabelOut(BaseModel):
    """An ISIN identifies a line; a ticker and a name are what a reader can use.

    Both fall back to the key itself when the market table does not know it.
    """

    asset_key: str
    symbol: str
    name: str


class WeightOut(AssetLabelOut):
    weight: Decimal


class CorrelationOut(BaseModel):
    left: str
    right: str
    value: Decimal
    left_symbol: str
    right_symbol: str
    left_name: str
    right_name: str


class ConcentrationResponse(BaseModel):
    """Lines held, effective positions, and independent bets."""

    lines: int
    effective_positions: MetricOut
    independent_bets: MetricOut
    """Capped at "indicatif": two years of daily returns is a noisy PCA."""
    weights: list[WeightOut] = []
    correlations: list[CorrelationOut] = []
    max_correlation: Decimal | None = None
    overlap: int
    dropped: list[AssetLabelOut] = []
    """Lines held but too thinly quoted to enter the covariance."""
    verdict: str


class FeesResponse(BaseModel):
    total_fees: MetricOut
    fee_share: MetricOut
    annual_bps: MetricOut
    threshold_order_size: MetricOut
    """Order size below which entry fees exceed 25 bps."""
    orders_below_threshold: int
    cost_below_threshold: Decimal
    invested_below_threshold: Decimal
    average_fee: Decimal | None = None
    average_order: Decimal | None = None
    order_count: int
    projection_eur: Decimal | None = None
    projection_note: str
    ter_note: str
    """Never conditional: brokerage is not the main cost of a buy-and-hold ETF."""
    verdict: str


class EpisodeOut(BaseModel):
    asset_key: str
    opened: date
    closed: date
    profit: Decimal


class ExitsResponse(BaseModel):
    """Disposition effect, what the exits cost, and the closed round trips."""

    pgr: MetricOut
    plr: MetricOut
    ratio: MetricOut
    cost_eur: MetricOut
    realisations: int
    recent_sales: int
    """Sales too recent for the one-year horizon: excluded, never measured short."""
    measured_sales: int
    horizon_days: int
    hit_rate: MetricOut
    payoff_ratio: MetricOut
    episode_count: int
    episodes: list[EpisodeOut] = []
    verdict: str


class MonthlyAdherenceOut(BaseModel):
    year: int
    month: int
    target: Decimal
    invested: Decimal


class AllocationDriftOut(AssetLabelOut):
    target: Decimal
    actual: Decimal


class PlanResponse(BaseModel):
    """Only present when a plan is declared."""

    monthly_target: Decimal
    since: date
    months: list[MonthlyAdherenceOut] = []
    total_target: Decimal
    total_invested: Decimal
    adherence_ratio: MetricOut
    average_monthly: MetricOut
    drift: list[AllocationDriftOut] = []
    drift_l1: MetricOut
    rebalance_eur: Decimal | None = None
    under_invested_months: int
    under_in_down_months: int
    verdict: str
    error: str | None = None
    """Why a declared plan could not be scored, when that is the case."""


class InvestorAnalyticsResponse(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    days: int
    benchmark_asset_key: str
    verdict: str = ""
    """The page's opening statement, written only from figures that passed their gate."""
    investor_gap: InvestorGapResponse | None = None
    counterfactual: CounterfactualResponse | None = None
    execution: ExecutionResponse | None = None
    regularity: RegularityResponse | None = None
    turnover: TurnoverOut | None = None
    deposit_lag: DepositLagResponse | None = None
    market_conditioning: MarketConditioningResponse | None = None
    concentration: ConcentrationResponse | None = None
    fees: FeesResponse | None = None
    exits: ExitsResponse | None = None
    plan: PlanResponse | None = None
