"""Permutation testing, and the market conditioning it serves.

A behavioural claim needs a null hypothesis, otherwise "you buy after the monthly
rise" is indistinguishable from forty-seven coin flips that happened to land the
same way. Every M2/M3 block that says something affirmative about behaviour goes
through here first.

The engine is deliberately ignorant of what it is testing: it takes an observed
statistic and an array of statistics drawn under the null, and reports where the
observation falls. Blocks own the resampling scheme, because only they know what
"holding everything else constant" means for their question.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np

# A fixed seed is a correctness requirement, not laziness: without it the same
# portfolio yields a different p-value on every page load, and a number that
# moves when nothing changed is a number nobody should trust.
DEFAULT_SEED = 0

DEFAULT_DRAWS = 5000

# Above this the effect is indistinguishable from chance and the UI must say
# "nothing detectable" rather than crediting or blaming the user (spec section 2).
DETECTABLE_P = 0.10


@dataclass(frozen=True)
class PermutationResult:
    observed: float
    p_value: float
    percentile: float
    n_draws: int

    @property
    def is_detectable(self) -> bool:
        return self.p_value <= DETECTABLE_P


def rng(seed: int = DEFAULT_SEED) -> np.random.Generator:
    """The generator every block should use, so results stay reproducible."""
    return np.random.default_rng(seed)


def permutation_test(observed: float, null_samples) -> PermutationResult | None:
    """Locate an observed statistic inside its null distribution.

    Two-sided by construction: a systematically favourable execution is as much a
    finding as an unfavourable one, and deciding the direction after seeing the
    data is how false positives are manufactured.

    The p-value uses the (r+1)/(n+1) convention rather than r/n, so a statistic no
    draw reaches reports 1/(n+1) instead of an impossible zero.
    """
    samples = np.asarray(null_samples, dtype=np.float64)
    samples = samples[np.isfinite(samples)]
    if samples.size == 0 or not np.isfinite(observed):
        return None

    centre = float(np.mean(samples))
    at_least_as_extreme = int(np.sum(np.abs(samples - centre) >= abs(observed - centre)))
    p_value = (at_least_as_extreme + 1) / (samples.size + 1)
    percentile = float(np.mean(samples < observed) * 100.0)

    return PermutationResult(
        observed=float(observed),
        p_value=min(p_value, 1.0),
        percentile=percentile,
        n_draws=int(samples.size),
    )


# ── 2.2 · market conditioning: contrarian or trend follower? ──────────

_ZERO = Decimal("0")

# One year of sessions for the trailing high, one month for the momentum window.
TRAILING_HIGH_SESSIONS = 252
MOMENTUM_SESSIONS = 21

# Below ten purchases the weighted mean is one order's opinion; below 250
# sessions the unconditional distribution it is compared against is too thin.
MIN_PURCHASES = 10
MIN_SESSIONS = 250
YEAR_SPLIT_MONTHS = 24


@dataclass(frozen=True)
class MarketState:
    day: date
    drawdown: Decimal
    """Distance to the trailing one-year high, negative or zero."""
    momentum: Decimal
    """Benchmark return over the previous 21 sessions."""


@dataclass(frozen=True)
class MarketConditioning:
    states: list[MarketState]
    """Every session of the window, with a complete trailing window."""
    purchase_states: list[tuple[date, Decimal, Decimal, Decimal]]
    """(day, euros, drawdown, momentum) for each purchase that lands on a session."""
    weighted_drawdown: Decimal | None
    unconditional_drawdown: Decimal | None
    weighted_momentum: Decimal | None
    unconditional_momentum: Decimal | None
    permutation: PermutationResult | None
    yearly: list[tuple[str, Decimal]]
    """Weighted drawdown per 12-month bucket. Trend, never proof."""
    sample_size: int
    sessions: int

    @property
    def is_measurable(self) -> bool:
        return self.sample_size >= MIN_PURCHASES and self.sessions >= MIN_SESSIONS


def market_states(series: dict, sessions: list) -> list[MarketState]:
    """Drawdown and momentum for every session with a full trailing window.

    Sessions, not calendar days: a forward-filled series repeats the previous
    close, so a 21-calendar-day return would silently become a 15-session one and
    a trailing high would be computed over a series that never moved.

    Sessions whose trailing window is incomplete are dropped rather than measured
    against a truncated maximum — an expanding-window high starts at zero
    drawdown by construction and would read as buying the dip.
    """
    ordered = [d for d in sorted(sessions) if d in series]
    out: list[MarketState] = []
    for index, day in enumerate(ordered):
        if index < TRAILING_HIGH_SESSIONS or index < MOMENTUM_SESSIONS:
            continue
        price = Decimal(str(series[day]))
        if price <= _ZERO:
            continue
        window = ordered[index - TRAILING_HIGH_SESSIONS : index + 1]
        high = max(Decimal(str(series[d])) for d in window)
        past = Decimal(str(series[ordered[index - MOMENTUM_SESSIONS]]))
        if high <= _ZERO or past <= _ZERO:
            continue
        out.append(
            MarketState(
                day=day,
                drawdown=price / high - Decimal("1"),
                momentum=price / past - Decimal("1"),
            )
        )
    return out


def _weighted_mean(values, weights) -> Decimal | None:
    total = sum(weights)
    if total <= _ZERO:
        return None
    return sum(v * w for v, w in zip(values, weights)) / total


def _year_bucket(day: date, start: date) -> str:
    return f"an{(day - start).days // 365 + 1}"


def analyse_market_conditioning(
    purchases,
    benchmark_series: dict,
    sessions: list,
    *,
    draws: int = DEFAULT_DRAWS,
    rng=None,
) -> MarketConditioning | None:
    """Where the investor's euros enter, compared with where a random day sits.

    The null hypothesis is "my money arrives on a day picked at random": purchase
    dates are permuted over every session of the window with the amounts held
    fixed. Anything the investor cannot control stays frozen, so what is left is
    the choice of day.
    """
    states = market_states(benchmark_series, sessions)
    if not states:
        return None

    by_day = {state.day: state for state in states}
    matched = [
        (day, amount, by_day[day].drawdown, by_day[day].momentum)
        for day, amount in purchases
        if day in by_day
    ]

    weights = [amount for _, amount, _, _ in matched]
    weighted_dd = _weighted_mean([dd for _, _, dd, _ in matched], weights)
    weighted_mom = _weighted_mean([mom for _, _, _, mom in matched], weights)

    unconditional_dd = (
        sum(state.drawdown for state in states) / Decimal(len(states)) if states else None
    )
    unconditional_mom = (
        sum(state.momentum for state in states) / Decimal(len(states)) if states else None
    )

    permutation = None
    if weighted_dd is not None and len(matched) >= 2:
        # `rng` shadows the module helper here, hence the direct call.
        generator = rng if rng is not None else np.random.default_rng(DEFAULT_SEED)
        pool = np.asarray([float(state.drawdown) for state in states], dtype=np.float64)
        amounts = np.asarray([float(w) for w in weights], dtype=np.float64)
        total = amounts.sum()
        if total > 0 and pool.size:
            picks = generator.integers(0, pool.size, size=(draws, amounts.size))
            null = (pool[picks] * amounts).sum(axis=1) / total
            permutation = permutation_test(float(weighted_dd), null)

    yearly: list[tuple[str, Decimal]] = []
    if matched:
        first = min(day for day, _, _, _ in matched)
        last = max(day for day, _, _, _ in matched)
        if (last - first).days >= YEAR_SPLIT_MONTHS * 30:
            buckets: dict[str, list[tuple[Decimal, Decimal]]] = {}
            for day, amount, drawdown, _ in matched:
                buckets.setdefault(_year_bucket(day, first), []).append((drawdown, amount))
            for label in sorted(buckets):
                pairs = buckets[label]
                mean = _weighted_mean([d for d, _ in pairs], [a for _, a in pairs])
                if mean is not None:
                    yearly.append((label, mean))

    return MarketConditioning(
        states=states,
        purchase_states=matched,
        weighted_drawdown=weighted_dd,
        unconditional_drawdown=unconditional_dd,
        weighted_momentum=weighted_mom,
        unconditional_momentum=unconditional_mom,
        permutation=permutation,
        yearly=yearly,
        sample_size=len(matched),
        sessions=len(states),
    )
