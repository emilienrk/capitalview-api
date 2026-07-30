from datetime import date, timedelta
from decimal import Decimal

import pytest

from services.analytics.returns import (
    TwrResult,
    annualize,
    time_weighted_return,
    xirr,
)

START = date(2026, 1, 1)


def _days(n: int) -> date:
    return START + timedelta(days=n)


def test_twr_without_flows_is_the_raw_value_change():
    series = [(_days(0), Decimal("1000")), (_days(1), Decimal("1100"))]
    result = time_weighted_return(series, {})
    assert result.total_return == pytest.approx(Decimal("0.10"), abs=1e-9)
    assert result.days == 1
    assert result.skipped_days == 0


def test_twr_neutralises_a_deposit():
    # Value goes 1000 -> 2100 but 1000 of that is a deposit, so the strategy made 10%.
    series = [(_days(0), Decimal("1000")), (_days(1), Decimal("2100"))]
    result = time_weighted_return(series, {_days(1): Decimal("1000")})
    assert result.total_return == pytest.approx(Decimal("0.05"), abs=1e-9)


def test_twr_chains_daily_returns():
    series = [
        (_days(0), Decimal("100")),
        (_days(1), Decimal("110")),
        (_days(2), Decimal("99")),
    ]
    result = time_weighted_return(series, {})
    # 1.10 * 0.90 - 1 = -0.01
    assert result.total_return == pytest.approx(Decimal("-0.01"), abs=1e-9)
    assert result.days == 2


def test_twr_skips_days_with_a_non_positive_base_instead_of_zeroing_them():
    # Day 1 is dormant: base = 0 + 0, no capital and no flow, so nothing to measure.
    series = [
        (_days(0), Decimal("0")),
        (_days(1), Decimal("0")),
        (_days(2), Decimal("550")),
    ]
    result = time_weighted_return(series, {_days(2): Decimal("500")})
    assert result.skipped_days == 1
    assert result.days == 1
    assert result.total_return == pytest.approx(Decimal("0.10"), abs=1e-9)


def test_first_funded_day_earns_a_real_return_and_is_not_skipped():
    # Opening at 0 is not a reason to skip: the 500 funded that day rode a +10%
    # move and that return is real. Only a non-positive base is unmeasurable.
    series = [(_days(0), Decimal("0")), (_days(1), Decimal("550"))]
    result = time_weighted_return(series, {_days(1): Decimal("500")})
    assert result.total_return == pytest.approx(Decimal("0.10"), abs=1e-9)
    assert result.skipped_days == 0
    assert result.days == 1


def test_xirr_on_a_simple_one_year_flow():
    flows = [(_days(0), Decimal("-1000")), (_days(365), Decimal("1100"))]
    assert xirr(flows) == pytest.approx(Decimal("0.10"), abs=1e-6)


def test_xirr_returns_none_without_a_sign_change():
    assert xirr([(_days(0), Decimal("-1000")), (_days(365), Decimal("-500"))]) is None


def test_mwr_equals_twr_when_there_are_no_intermediate_flows():
    series = [(_days(0), Decimal("1000")), (_days(365), Decimal("1200"))]
    twr = time_weighted_return(series, {})
    mwr = xirr([(_days(0), Decimal("-1000")), (_days(365), Decimal("1200"))])
    assert mwr == pytest.approx(twr.total_return, abs=1e-6)


def test_money_arriving_after_the_rise_drags_mwr_below_twr():
    # 1000 rides a +50% move; a second 1000 lands after it and rides the flat rest.
    # The window must stay exactly 365 days: xirr is annualised while
    # total_return is cumulative, and only a one-year window puts them on the
    # same footing. Shortening it makes the comparison meaningless, not simpler.
    series = [
        (_days(0), Decimal("1000")),
        (_days(180), Decimal("1500")),
        (_days(181), Decimal("2500")),
        (_days(365), Decimal("2500")),
    ]
    twr = time_weighted_return(series, {_days(181): Decimal("1000")})
    mwr = xirr(
        [
            (_days(0), Decimal("-1000")),
            (_days(181), Decimal("-1000")),
            (_days(365), Decimal("2500")),
        ]
    )
    assert twr.total_return == pytest.approx(Decimal("0.50"), abs=1e-9)
    assert mwr < twr.total_return


def test_annualize_scales_a_two_year_return():
    # 21% over 730 days is 10% a year.
    assert annualize(Decimal("0.21"), 730) == pytest.approx(Decimal("0.10"), abs=1e-6)


def test_annualize_refuses_a_degenerate_window():
    assert annualize(Decimal("0.21"), 0) is None


def test_annualize_refuses_a_total_wipeout():
    # -100% has no real annual root to take.
    assert annualize(Decimal("-1"), 365) is None


def test_twr_needs_two_points():
    assert time_weighted_return([], {}) == TwrResult(None, 0, 0)
    assert time_weighted_return([(_days(0), Decimal("1000"))], {}) == TwrResult(None, 0, 0)


def test_twr_is_none_when_every_day_was_skipped():
    series = [(_days(0), Decimal("0")), (_days(1), Decimal("0"))]
    assert time_weighted_return(series, {}) == TwrResult(None, 0, 1)
