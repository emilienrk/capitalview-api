import numpy as np

from services.analytics.timing import (
    DEFAULT_DRAWS,
    permutation_test,
    rng,
)


def test_an_observation_in_the_middle_of_the_null_is_not_detectable():
    samples = rng().normal(0, 1, 5000)

    result = permutation_test(0.0, samples)

    assert result.p_value > 0.5
    assert result.is_detectable is False


def test_an_observation_far_outside_the_null_is_detectable():
    samples = rng().normal(0, 1, 5000)

    result = permutation_test(6.0, samples)

    assert result.p_value < 0.01
    assert result.is_detectable is True
    assert result.percentile > 99


def test_p_value_is_never_zero():
    """An unreachable statistic reports 1/(n+1), not an impossible certainty."""
    result = permutation_test(1e6, rng().normal(0, 1, 100))

    assert result.p_value == 1 / 101


def test_the_test_is_two_sided():
    samples = rng().normal(0, 1, 5000)

    assert permutation_test(4.0, samples).p_value == permutation_test(-4.0, samples).p_value


def test_empty_or_non_finite_input_yields_no_result():
    assert permutation_test(1.0, []) is None
    assert permutation_test(float("nan"), [1.0, 2.0]) is None
    assert permutation_test(1.0, [float("nan"), float("inf")]) is None


def test_the_seed_makes_results_reproducible():
    assert rng().normal(0, 1, 10).tolist() == rng().normal(0, 1, 10).tolist()
    assert rng(1).normal(0, 1, 10).tolist() != rng(2).normal(0, 1, 10).tolist()


def test_p_values_are_uniform_on_data_with_no_bias():
    """Calibration check required by spec section 11.

    On synthetic data where the null is true, p-values must be roughly uniform on
    [0,1]. A test that reports significance more often than chance would turn
    noise into behavioural accusations.
    """
    generator = rng(12345)
    p_values = []
    for _ in range(200):
        draws = generator.normal(0, 1, 400)
        # Observed statistic drawn from the same distribution: the null holds.
        observed = float(generator.normal(0, 1))
        p_values.append(permutation_test(observed, draws).p_value)

    p_values = np.asarray(p_values)
    below_5pct = float(np.mean(p_values < 0.05))
    below_50pct = float(np.mean(p_values < 0.50))

    # Wide bounds on purpose: this must catch a broken test, not flap on sampling.
    assert 0.0 <= below_5pct <= 0.15
    assert 0.35 <= below_50pct <= 0.65


def test_default_draw_count_matches_the_spec():
    assert DEFAULT_DRAWS == 5000


# ── 2.2 · market conditioning ─────────────────────────────────────────

import math
from datetime import date, timedelta
from decimal import Decimal

from services.analytics.timing import (
    MIN_SESSIONS,
    TRAILING_HIGH_SESSIONS,
    analyse_market_conditioning,
    market_states,
    rng,
)

_START = date(2022, 1, 3)


def _sessions(count: int) -> list[date]:
    """Weekday sessions, which is close enough to a real calendar here."""
    out: list[date] = []
    day = _START
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _wave_series(sessions: list[date], period: int = 120) -> dict[date, Decimal]:
    """A price that keeps cycling, so drawdowns span a real range."""
    return {
        day: Decimal(str(round(100 + 20 * math.sin(2 * math.pi * i / period), 4)))
        for i, day in enumerate(sessions)
    }


def test_sessions_without_a_full_trailing_year_are_dropped():
    sessions = _sessions(300)
    states = market_states(_wave_series(sessions), sessions)

    assert len(states) == 300 - TRAILING_HIGH_SESSIONS
    assert states[0].day == sessions[TRAILING_HIGH_SESSIONS]
    # A drawdown is a distance below a high: never positive.
    assert all(state.drawdown <= 0 for state in states)


def test_buying_the_dips_lands_below_the_average_day_and_is_detectable():
    sessions = _sessions(800)
    series = _wave_series(sessions)
    states = market_states(series, sessions)
    deepest = sorted(states, key=lambda s: s.drawdown)[:40]
    purchases = [(state.day, Decimal("100")) for state in deepest]

    result = analyse_market_conditioning(
        purchases, series, sessions, draws=2000, rng=rng(0)
    )

    assert result.weighted_drawdown < result.unconditional_drawdown
    assert result.permutation.p_value < 0.05
    assert result.is_measurable is True


def test_buying_on_arbitrary_days_is_not_detectable():
    sessions = _sessions(800)
    series = _wave_series(sessions)
    states = market_states(series, sessions)
    purchases = [(state.day, Decimal("100")) for state in states[::12]]

    result = analyse_market_conditioning(
        purchases, series, sessions, draws=2000, rng=rng(0)
    )

    assert result.permutation.is_detectable is False


def test_the_yearly_split_needs_two_years_of_purchases():
    sessions = _sessions(800)
    series = _wave_series(sessions)
    states = market_states(series, sessions)

    short = [(state.day, Decimal("100")) for state in states[:60]]
    assert analyse_market_conditioning(short, series, sessions, draws=200, rng=rng(0)).yearly == []

    spread = [(state.day, Decimal("100")) for state in states[::10]]
    buckets = analyse_market_conditioning(spread, series, sessions, draws=200, rng=rng(0)).yearly
    # One bucket per 12 months of purchases, in order, starting at the first one.
    assert [label for label, _ in buckets][:2] == ["an1", "an2"]
    assert len(buckets) >= 2


def test_too_few_sessions_is_not_measurable():
    sessions = _sessions(TRAILING_HIGH_SESSIONS + 20)
    series = _wave_series(sessions)
    purchases = [(sessions[-1], Decimal("100"))] * 12

    result = analyse_market_conditioning(purchases, series, sessions, draws=200, rng=rng(0))

    assert result.sessions < MIN_SESSIONS
    assert result.is_measurable is False


def test_the_same_seed_gives_the_same_p_value():
    sessions = _sessions(800)
    series = _wave_series(sessions)
    states = market_states(series, sessions)
    purchases = [(state.day, Decimal("100")) for state in states[::7]]

    first = analyse_market_conditioning(purchases, series, sessions, draws=500, rng=rng(0))
    second = analyse_market_conditioning(purchases, series, sessions, draws=500, rng=rng(0))

    assert first.permutation.p_value == second.permutation.p_value


def test_purchases_on_non_session_days_are_ignored_not_guessed():
    sessions = _sessions(800)
    series = _wave_series(sessions)
    weekend = _START + timedelta(days=5)

    result = analyse_market_conditioning(
        [(weekend, Decimal("100"))], series, sessions, draws=200, rng=rng(0)
    )

    assert result.sample_size == 0
    assert result.weighted_drawdown is None
