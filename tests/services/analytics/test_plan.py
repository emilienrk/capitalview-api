from datetime import date
from decimal import Decimal

import pytest

from services.analytics.plan import PlanError, analyse_plan, parse_plan


class _Window:
    def __init__(self, start, end):
        self.start = start
        self.end = end


WINDOW = _Window(date(2024, 1, 10), date(2025, 7, 15))
PLAN = {"monthly_target": "500", "allocation": {"AAA": "80", "BBB": "20"}}


def _purchases(months: int, amount="500", start_month=1, start_year=2024):
    out = []
    for n in range(months):
        year = start_year + (start_month - 1 + n) // 12
        month = (start_month - 1 + n) % 12 + 1
        out.append((date(year, month, 5), "AAA", Decimal(amount)))
    return out


def test_no_plan_means_no_block():
    assert analyse_plan(None, [], [], Decimal("1000"), WINDOW) is None
    assert analyse_plan({}, [], [], Decimal("1000"), WINDOW) is None


def test_an_allocation_that_does_not_add_up_is_rejected():
    with pytest.raises(PlanError) as error:
        parse_plan({"monthly_target": "500", "allocation": {"AAA": "90"}}, date(2024, 1, 1))

    assert "90" in str(error.value)


def test_a_zero_monthly_target_is_rejected():
    with pytest.raises(PlanError):
        parse_plan({"monthly_target": "0"}, date(2024, 1, 1))


def test_a_plan_followed_to_the_euro_scores_one():
    result = analyse_plan(PLAN, _purchases(18), [], Decimal("0"), WINDOW)

    assert result.adherence_ratio == Decimal("1")
    assert result.average_monthly == Decimal("500")
    assert result.under_invested_months == 0


def test_under_investing_by_thirty_percent_shows_exactly_that():
    result = analyse_plan(PLAN, _purchases(18, amount="350"), [], Decimal("0"), WINDOW)

    assert result.adherence_ratio == Decimal("0.7")
    assert result.average_monthly == Decimal("350")
    assert result.under_invested_months == len(result.months)


def test_the_running_month_is_excluded():
    # The window ends mid-July 2025, so June 2025 is the last complete month.
    result = analyse_plan(PLAN, _purchases(18), [], Decimal("0"), WINDOW)

    assert (result.months[-1].year, result.months[-1].month) == (2025, 6)


def test_months_before_the_declared_start_are_ignored():
    plan = {**PLAN, "since": "2025-01"}
    result = analyse_plan(plan, _purchases(18), [], Decimal("0"), WINDOW)

    assert result.since == date(2025, 1, 1)
    assert (result.months[0].year, result.months[0].month) == (2025, 1)
    # January to June 2025: July is still running and never counts.
    assert len(result.months) == 6


def test_a_matching_allocation_has_no_drift():
    weights = [("AAA", Decimal("0.8")), ("BBB", Decimal("0.2"))]
    result = analyse_plan(PLAN, _purchases(18), weights, Decimal("10000"), WINDOW)

    assert result.drift_l1 == Decimal("0")
    assert result.rebalance_eur == Decimal("0")


def test_drift_counts_a_held_line_absent_from_the_target():
    weights = [("AAA", Decimal("0.5")), ("CCC", Decimal("0.5"))]
    result = analyse_plan(PLAN, _purchases(18), weights, Decimal("10000"), WINDOW)

    gaps = {row.asset_key: row.gap for row in result.drift}
    assert gaps["CCC"] == Decimal("50")
    assert gaps["BBB"] == Decimal("-20")
    # |−30| + |−20| + |+50| = 100 points, half of which has to change hands.
    assert result.drift_l1 == Decimal("100")
    assert result.rebalance_eur == Decimal("5000")


def test_under_investing_is_crossed_with_falling_months():
    benchmark = {
        date(2024, 3, 1): Decimal("100"),
        date(2024, 3, 28): Decimal("90"),
        date(2024, 4, 1): Decimal("90"),
        date(2024, 4, 28): Decimal("110"),
    }
    purchases = [(date(2024, 4, 5), "AAA", Decimal("500"))]
    result = analyse_plan(PLAN, purchases, [], Decimal("0"), WINDOW, benchmark)

    assert result.under_in_down_months == 1


def test_three_months_is_the_floor_for_a_verdict():
    window = _Window(date(2025, 5, 10), date(2025, 7, 15))
    result = analyse_plan(PLAN, _purchases(2, start_month=5, start_year=2025), [], Decimal("0"), window)

    assert len(result.months) == 2
    assert result.is_measurable is False
