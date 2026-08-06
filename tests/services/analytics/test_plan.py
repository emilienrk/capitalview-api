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


SPLIT = {
    "periods": [
        {"since": "2024-01", "monthly_target": "200", "allocation": {"AAA": "50", "BBB": "50"}},
        {"since": "2025-01", "monthly_target": "600", "allocation": {"AAA": "25", "BBB": "75"}},
    ]
}


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


def test_a_flat_plan_is_one_period():
    periods = parse_plan(PLAN, date(2024, 1, 1))

    assert len(periods) == 1
    assert periods[0].monthly_target == Decimal("500")
    assert periods[0].since == date(2024, 1, 1)


def test_a_split_plan_keeps_every_period():
    periods = parse_plan(SPLIT, date(2024, 1, 1))

    assert [period.monthly_target for period in periods] == [Decimal("200"), Decimal("600")]
    assert [period.since for period in periods] == [date(2024, 1, 1), date(2025, 1, 1)]


def test_periods_are_read_in_order_whatever_the_order_stored():
    periods = parse_plan({"periods": list(reversed(SPLIT["periods"]))}, date(2024, 1, 1))

    assert [period.since for period in periods] == [date(2024, 1, 1), date(2025, 1, 1)]


def test_two_periods_starting_the_same_month_are_rejected():
    plan = {"periods": [SPLIT["periods"][0], dict(SPLIT["periods"][0], monthly_target="900")]}

    with pytest.raises(PlanError) as error:
        parse_plan(plan, date(2024, 1, 1))

    assert "même mois" in str(error.value)


def test_a_bad_period_is_named_in_the_error():
    plan = {"periods": [SPLIT["periods"][0], dict(SPLIT["periods"][1], monthly_target="0")]}

    with pytest.raises(PlanError) as error:
        parse_plan(plan, date(2024, 1, 1))

    assert "Période 2" in str(error.value)


def test_a_period_after_the_first_must_declare_its_month():
    plan = {"periods": [SPLIT["periods"][0], {"monthly_target": "600"}]}

    with pytest.raises(PlanError) as error:
        parse_plan(plan, date(2024, 1, 1))

    assert "Période 2" in str(error.value)


def test_each_month_is_scored_against_the_target_in_force_that_month():
    """The raise is the point: 200 until 2025, 600 after, never 600 throughout."""
    result = analyse_plan(SPLIT, [], [], Decimal("0"), WINDOW)

    targets = {(row.year, row.month): row.target for row in result.months}
    assert targets[(2024, 6)] == Decimal("200")
    assert targets[(2024, 12)] == Decimal("200")
    assert targets[(2025, 1)] == Decimal("600")
    assert targets[(2025, 6)] == Decimal("600")
    # 12 months at 200 + 6 complete months of 2025 at 600.
    assert result.total_target == Decimal("6000")


def test_a_split_plan_followed_to_the_euro_still_scores_one():
    purchases = [
        (date(year, month, 5), "AAA", Decimal("200") if year == 2024 else Decimal("600"))
        for year, month in [(2024, m) for m in range(1, 13)] + [(2025, m) for m in range(1, 7)]
    ]
    result = analyse_plan(SPLIT, purchases, [], Decimal("0"), WINDOW)

    assert result.adherence_ratio == Decimal("1")
    assert result.under_invested_months == 0
    # The headline amount is the one in force today, not the one it opened with.
    assert result.monthly_target == Decimal("600")


def test_a_raise_is_not_read_backwards_as_under_investment():
    """Scored against a flat 600, the 200 € months would each be a shortfall."""
    purchases = [
        (date(year, month, 5), "AAA", Decimal("200") if year == 2024 else Decimal("600"))
        for year, month in [(2024, m) for m in range(1, 13)] + [(2025, m) for m in range(1, 7)]
    ]
    flat = analyse_plan(
        {"monthly_target": "600", "since": "2024-01"}, purchases, [], Decimal("0"), WINDOW
    )

    assert flat.under_invested_months == 12
    assert analyse_plan(SPLIT, purchases, [], Decimal("0"), WINDOW).under_invested_months == 0


def test_drift_reads_the_allocation_in_force_today():
    """25/75 is the current target; the 50/50 it replaced says nothing about now."""
    weights = [("AAA", Decimal("0.25")), ("BBB", Decimal("0.75"))]
    result = analyse_plan(SPLIT, [], weights, Decimal("10000"), WINDOW)

    assert result.drift_l1 == Decimal("0")
    targets = {row.asset_key: row.target for row in result.drift}
    assert targets == {"AAA": Decimal("25"), "BBB": Decimal("75")}


def _split_purchases(first: tuple[str, str], second: tuple[str, str]):
    """Monthly buys of two lines, at one euro split before 2025 and another after."""
    out = []
    for year, month in [(2024, m) for m in range(1, 13)] + [(2025, m) for m in range(1, 7)]:
        total = Decimal("200") if year == 2024 else Decimal("600")
        aaa, bbb = (first if year == 2024 else second)
        out.append((date(year, month, 5), "AAA", total * Decimal(aaa) / Decimal("100")))
        out.append((date(year, month, 5), "BBB", total * Decimal(bbb) / Decimal("100")))
    return out


def test_each_period_is_scored_on_its_own_allocation():
    """50/50 then 25/75, both respected: neither period may show any drift.

    Only the flows can say this. The portfolio held today has been reshaped by
    two years of market moves, so the point-in-time drift cannot answer whether
    50/50 was followed back when 50/50 was the plan.
    """
    result = analyse_plan(SPLIT, _split_purchases(("50", "50"), ("25", "75")), [], Decimal("0"), WINDOW)

    assert [outcome.flow_drift_l1 for outcome in result.outcomes] == [Decimal("0"), Decimal("0")]
    assert [outcome.months for outcome in result.outcomes] == [12, 6]
    assert [outcome.adherence_ratio for outcome in result.outcomes] == [Decimal("1"), Decimal("1")]


def test_a_period_that_kept_the_old_split_shows_its_drift():
    """The raise was applied, the reallocation was not — and the page says so."""
    result = analyse_plan(SPLIT, _split_purchases(("50", "50"), ("50", "50")), [], Decimal("0"), WINDOW)

    first, second = result.outcomes
    assert first.flow_drift_l1 == Decimal("0")
    # 50 vs 25 on AAA and 50 vs 75 on BBB: 25 + 25 = 50 points apart.
    assert second.flow_drift_l1 == Decimal("50")
    # The amount was still respected: only the split moved.
    assert second.adherence_ratio == Decimal("1")


def test_a_period_carries_the_month_the_next_one_starts():
    result = analyse_plan(SPLIT, [], [], Decimal("0"), WINDOW)

    assert [outcome.until for outcome in result.outcomes] == [date(2025, 1, 1), None]


def test_a_period_outcome_ignores_the_running_month():
    """The window ends mid-July 2025; July euros must not count for the period."""
    purchases = [
        *_split_purchases(("50", "50"), ("25", "75")),
        (date(2025, 7, 5), "AAA", Decimal("5000")),
    ]
    result = analyse_plan(SPLIT, purchases, [], Decimal("0"), WINDOW)

    assert result.outcomes[1].invested_eur == Decimal("3600")
    assert result.outcomes[1].flow_drift_l1 == Decimal("0")


def test_a_flat_plan_still_gets_one_outcome():
    result = analyse_plan(PLAN, _purchases(18), [], Decimal("0"), WINDOW)

    assert len(result.outcomes) == 1
    assert result.outcomes[0].until is None
    assert result.outcomes[0].adherence_ratio == Decimal("1")


def test_a_plan_that_rotated_out_of_its_first_lines_entirely():
    """Two ETFs at 50/50 for a year, sold, then one line at 100 % since.

    The two periods share no line at all. The first has to be scored on lines
    the portfolio no longer holds — which the point-in-time drift cannot do, and
    which is the case the per-period flows exist for.
    """
    plan = {
        "periods": [
            {"since": "2024-01", "monthly_target": "200", "allocation": {"AAA": "50", "BBB": "50"}},
            {"since": "2025-01", "monthly_target": "600", "allocation": {"CCC": "100"}},
        ]
    }
    purchases = []
    for month in range(1, 13):
        purchases.append((date(2024, month, 5), "AAA", Decimal("100")))
        purchases.append((date(2024, month, 5), "BBB", Decimal("100")))
    for month in range(1, 7):
        purchases.append((date(2025, month, 5), "CCC", Decimal("600")))

    # Only CCC is still held: AAA and BBB were sold to zero.
    weights = [("CCC", Decimal("1"))]
    result = analyse_plan(plan, purchases, weights, Decimal("10000"), WINDOW)

    first, second = result.outcomes
    # Each period judged on its own lines, both followed to the point.
    assert first.flow_drift_l1 == Decimal("0")
    assert second.flow_drift_l1 == Decimal("0")
    assert (first.adherence_ratio, second.adherence_ratio) == (Decimal("1"), Decimal("1"))
    # The sold lines do not pollute the current drift: the target in force names
    # only CCC, and CCC is all that is held.
    assert result.drift_l1 == Decimal("0")
    assert [row.asset_key for row in result.drift] == ["CCC"]
    assert result.adherence_ratio == Decimal("1")
