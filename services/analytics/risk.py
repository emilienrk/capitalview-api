"""The risk taken, and whether beating the index is more than noise.

The rest of the page judges behaviour; the yearly table judges return. Neither
says what the return cost in risk, and a portfolio that beats the index by a
point while swinging twice as hard has not beaten it. So: how much the
portfolio moves against how much the index moves, how far it fell from its
worst peak and how long that took to recover, and — the question every
investor asks — whether the gap to the index is a finding or a coin flip.

Everything is measured on *sessions*, the days the benchmark actually quoted.
The snapshots exist every calendar day and repeat Friday's value over the
weekend, so a daily standard deviation over calendar days would be deflated by
the closed days — by √(7/5), systematically. Between two sessions the
portfolio return is taken under the same start-of-interval flow convention as
the time-weighted return (services/analytics/returns.py), so a deposit landing
on a Saturday is neutralised rather than read as a +3 % day.

The excess return carries its own uncertainty, like the permutation tests do
for behaviour: a t-statistic on the session excess returns, and the number of
years this gap would need, at this noise level, to clear the usual 5 % bar.
The honest answer for most retail histories is "decades", and the page says
so instead of crowning or blaming a three-year streak.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np

from services.analytics.timing import DETECTABLE_P

_ZERO = Decimal("0")

# One year of sessions before a volatility means anything; three before a beta
# or a drawdown stops being one episode's opinion.
MIN_SESSIONS = 250
SOLID_SESSIONS = 750
SESSIONS_PER_YEAR = 252
# Two-sided 5 % bar, the one every published alpha is held to.
_Z_95 = 1.96


@dataclass(frozen=True)
class Drawdown:
    depth: Decimal
    """Fall from the peak to the trough, negative or zero."""
    peak: date
    trough: date
    recovered: date | None
    """First session back at the peak, or None while still under water."""


@dataclass(frozen=True)
class RiskAnalysis:
    sessions: int
    volatility: Decimal | None
    """Annualised standard deviation of the session returns."""
    benchmark_volatility: Decimal | None
    volatility_ratio: Decimal | None
    beta: Decimal | None
    correlation: Decimal | None
    tracking_error: Decimal | None
    """Annualised standard deviation of the excess return — how far the portfolio strays."""
    max_drawdown: Drawdown | None
    benchmark_max_drawdown: Drawdown | None
    excess_return: Decimal | None
    """Annualised mean excess over the benchmark, arithmetic."""
    excess_p_value: float | None
    years_to_conclude: Decimal | None
    """Years of history this gap would need, at this noise, to be significant."""
    return_per_risk: Decimal | None
    """Annualised return over annualised volatility — a Sharpe ratio with no risk-free rate."""
    benchmark_return_per_risk: Decimal | None

    @property
    def is_measurable(self) -> bool:
        return self.sessions >= MIN_SESSIONS and self.volatility is not None

    @property
    def is_detectable(self) -> bool:
        return self.excess_p_value is not None and self.excess_p_value <= DETECTABLE_P


def session_returns(
    series: Sequence[tuple[date, Decimal]],
    flows: Mapping[date, Decimal],
    benchmark: Mapping[date, Decimal],
) -> tuple[list[date], np.ndarray, np.ndarray]:
    """Portfolio and benchmark returns between consecutive benchmark sessions.

    A session is a day the benchmark quoted *and* the portfolio was valued.
    Flows landing between two sessions are taken at the start of the interval,
    which is the daily Modified Dietz convention stretched over a closed market.
    Intervals whose base is not positive carry no return and are dropped.

    Returns the sessions kept — the first one, then the end of every interval
    measured, so there is one more day than returns — and the two return arrays.
    """
    values = {day: Decimal(str(value)) for day, value in series}
    sessions = sorted(day for day in benchmark if day in values and Decimal(str(benchmark[day])) > _ZERO)
    flow_days = sorted(flows)

    days: list[date] = sessions[:1]
    portfolio: list[float] = []
    index: list[float] = []
    cursor = 0
    for previous, day in zip(sessions, sessions[1:], strict=False):
        flow = _ZERO
        while cursor < len(flow_days) and flow_days[cursor] <= previous:
            cursor += 1
        probe = cursor
        while probe < len(flow_days) and flow_days[probe] <= day:
            flow += Decimal(str(flows[flow_days[probe]]))
            probe += 1
        base = values[previous] + flow
        if base <= _ZERO:
            continue
        days.append(day)
        portfolio.append(float((values[day] - values[previous] - flow) / base))
        index.append(float(Decimal(str(benchmark[day])) / Decimal(str(benchmark[previous])) - 1))
    return days, np.asarray(portfolio, dtype=np.float64), np.asarray(index, dtype=np.float64)


def max_drawdown(days: Sequence[date], returns: np.ndarray) -> Drawdown | None:
    """Deepest fall of the chained index, with when it started, bottomed and healed.

    `days` has one entry more than `returns`: the session each level is read on.
    """
    if returns.size == 0 or len(days) != returns.size + 1:
        return None
    level = np.cumprod(1.0 + returns)
    level = np.concatenate(([1.0], level))
    peaks = np.maximum.accumulate(level)
    falls = level / peaks - 1.0
    trough = int(np.argmin(falls))
    if falls[trough] >= 0:
        return Drawdown(_ZERO, days[0], days[0], days[0])
    peak = int(np.argmax(level[: trough + 1]))
    after = np.nonzero(level[trough:] >= peaks[trough])[0]
    recovered = trough + int(after[0]) if after.size else None
    return Drawdown(
        depth=_dec(falls[trough]),
        peak=days[peak],
        trough=days[trough],
        recovered=days[recovered] if recovered is not None else None,
    )


def analyse_risk(
    series: Sequence[tuple[date, Decimal]],
    flows: Mapping[date, Decimal],
    benchmark: Mapping[date, Decimal],
) -> RiskAnalysis | None:
    """Risk and excess return of the portfolio against its benchmark, per session."""
    days, portfolio, index = session_returns(series, flows, benchmark)
    sessions = int(portfolio.size)
    if sessions < 2:
        return None

    scale = math.sqrt(SESSIONS_PER_YEAR)
    vol = float(np.std(portfolio, ddof=1)) * scale
    bench_vol = float(np.std(index, ddof=1)) * scale
    excess = portfolio - index
    tracking = float(np.std(excess, ddof=1)) * scale

    beta = correlation = None
    bench_var = float(np.var(index, ddof=1))
    if bench_var > 0:
        beta = float(np.cov(portfolio, index, ddof=1)[0, 1]) / bench_var
        if vol > 0:
            correlation = float(np.corrcoef(portfolio, index)[0, 1])

    mean_excess = float(np.mean(excess))
    excess_sd = float(np.std(excess, ddof=1))
    p_value = years = None
    if excess_sd > 0:
        t_stat = mean_excess / (excess_sd / math.sqrt(sessions))
        p_value = math.erfc(abs(t_stat) / math.sqrt(2.0))
        if mean_excess != 0:
            years = (_Z_95 * excess_sd / abs(mean_excess)) ** 2 / SESSIONS_PER_YEAR
    elif mean_excess == 0:
        # Identical to the index: no gap, nothing to test.
        p_value = 1.0

    def annualised(returns: np.ndarray) -> float:
        growth = float(np.prod(1.0 + returns))
        return growth ** (SESSIONS_PER_YEAR / sessions) - 1.0 if growth > 0 else -1.0

    return RiskAnalysis(
        sessions=sessions,
        volatility=_dec(vol),
        benchmark_volatility=_dec(bench_vol),
        volatility_ratio=_dec(vol / bench_vol) if bench_vol > 0 else None,
        beta=_dec(beta) if beta is not None else None,
        correlation=_dec(correlation) if correlation is not None else None,
        tracking_error=_dec(tracking),
        max_drawdown=max_drawdown(days, portfolio),
        benchmark_max_drawdown=max_drawdown(days, index),
        excess_return=_dec(mean_excess * SESSIONS_PER_YEAR),
        excess_p_value=p_value,
        years_to_conclude=_dec(years, 1) if years is not None else None,
        return_per_risk=_dec(annualised(portfolio) / vol) if vol > 0 else None,
        benchmark_return_per_risk=_dec(annualised(index) / bench_vol) if bench_vol > 0 else None,
    )


def _dec(value: float, digits: int = 4) -> Decimal:
    return Decimal(str(round(float(value), digits)))
