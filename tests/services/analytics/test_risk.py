from datetime import date, timedelta
from decimal import Decimal

import numpy as np

from services.analytics.risk import MIN_SESSIONS, analyse_risk, max_drawdown, session_returns

START = date(2022, 1, 3)


def _sessions(n: int) -> list[date]:
    """n weekdays from START."""
    out, day = [], START
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _walk(sessions, daily_returns, start=100.0):
    level = start * np.cumprod(np.concatenate(([1.0], 1.0 + daily_returns)))
    return {day: Decimal(str(round(float(v), 6))) for day, v in zip(sessions, level, strict=True)}


def _calendar_series(quotes: dict[date, Decimal]) -> list[tuple[date, Decimal]]:
    """A snapshot every calendar day, weekends repeating Friday's value."""
    first, last = min(quotes), max(quotes)
    series, value = [], None
    for n in range((last - first).days + 1):
        day = first + timedelta(days=n)
        value = quotes.get(day, value)
        series.append((day, value))
    return series


def test_a_portfolio_identical_to_the_index_has_no_excess_and_unit_ratio():
    rng = np.random.default_rng(1)
    sessions = _sessions(600)
    bench = _walk(sessions, rng.normal(0.0004, 0.01, 599))

    risk = analyse_risk(_calendar_series(bench), {}, bench)

    assert risk.sessions == 599
    assert risk.volatility_ratio == Decimal("1")
    assert risk.beta == Decimal("1")
    assert risk.tracking_error == Decimal("0")
    assert risk.excess_return == Decimal("0")
    assert risk.excess_p_value == 1.0
    assert risk.years_to_conclude is None
    assert risk.max_drawdown == risk.benchmark_max_drawdown
    assert risk.return_per_risk == risk.benchmark_return_per_risk
    assert risk.is_measurable and not risk.is_detectable


def test_a_leveraged_copy_of_the_index_shows_in_the_ratio_and_the_beta():
    rng = np.random.default_rng(2)
    sessions = _sessions(800)
    r = rng.normal(0.0003, 0.01, 799)
    bench = _walk(sessions, r)
    portfolio = _walk(sessions, 1.5 * r)

    risk = analyse_risk(_calendar_series(portfolio), {}, bench)

    assert abs(float(risk.volatility_ratio) - 1.5) < 0.01
    assert abs(float(risk.beta) - 1.5) < 0.01
    assert abs(float(risk.correlation) - 1.0) < 1e-6
    assert float(risk.volatility) > float(risk.benchmark_volatility)


def test_a_deposit_between_two_sessions_is_not_a_return():
    sessions = _sessions(10)
    bench = {day: Decimal("100") for day in sessions}
    series = _calendar_series(bench)
    # A weekend deposit doubles the portfolio: with flows it is neutral.
    saturday = next(day for day, _ in series if day.weekday() == 5)
    series = [(day, value * 2 if day >= saturday else value) for day, value in series]

    _, with_flow, _ = session_returns(series, {saturday: Decimal("100")}, bench)
    _, without, _ = session_returns(series, {}, bench)

    assert np.allclose(with_flow, 0.0)
    assert without.max() > 0.9


def test_the_drawdown_names_the_peak_the_trough_and_the_recovery():
    days = _sessions(7)
    returns = np.array([0.10, -0.20, -0.10, 0.05, 0.60, 0.0])
    # Levels: 1, 1.1, 0.88, 0.792, 0.8316, 1.33, 1.33 → fall of −28 % from day 2 to day 4.

    dd = max_drawdown(days, returns)

    assert dd.depth == Decimal("-0.28")
    assert dd.peak == days[1]
    assert dd.trough == days[3]
    assert dd.recovered == days[5]


def test_a_drawdown_still_open_has_no_recovery():
    days = _sessions(4)
    dd = max_drawdown(days, np.array([0.05, -0.30, 0.02]))

    # Levels 1, 1.05, 0.735, 0.7497: −30 % from the second day, never back.
    assert dd.depth == Decimal("-0.3")
    assert dd.peak == days[1] and dd.trough == days[2]
    assert dd.recovered is None


def test_a_steady_outperformance_is_detectable_and_says_how_long_it_took():
    sessions = _sessions(MIN_SESSIONS + 1)
    rng = np.random.default_rng(3)
    r = rng.normal(0.0, 0.01, MIN_SESSIONS)
    bench = _walk(sessions, r)
    # +0.3 % every session over the index, at no extra noise: unmistakable.
    portfolio = _walk(sessions, r + 0.003)

    risk = analyse_risk(_calendar_series(portfolio), {}, bench)

    assert risk.is_detectable
    assert float(risk.excess_return) > 0.5
    assert risk.years_to_conclude is not None and float(risk.years_to_conclude) < 1


def test_a_noisy_small_edge_is_not_detectable_and_needs_decades():
    sessions = _sessions(760)
    rng = np.random.default_rng(4)
    r = rng.normal(0.0004, 0.01, 759)
    noise = rng.normal(0.00004, 0.004, 759)  # 1 % a year of edge under 6 % of tracking error
    bench = _walk(sessions, r)
    portfolio = _walk(sessions, r + noise)

    risk = analyse_risk(_calendar_series(portfolio), {}, bench)

    assert not risk.is_detectable
    assert float(risk.years_to_conclude) > 10


def test_fewer_than_two_common_sessions_yields_nothing():
    sessions = _sessions(1)
    bench = {sessions[0]: Decimal("100")}

    assert analyse_risk([(sessions[0], Decimal("100"))], {}, bench) is None
