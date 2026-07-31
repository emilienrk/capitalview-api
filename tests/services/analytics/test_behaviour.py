from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from services.analytics.behaviour import (
    analyse_deposit_regularity,
    analyse_purchase_regularity,
)


class _Tx:
    """Minimal stand-in for TransactionResponse."""

    def __init__(self, tx_type, asset_key, day, amount="1", price="100", notes=None, fees="0"):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(str(amount))
        self.price_per_unit = Decimal(str(price))
        self.fees = Decimal(str(fees))
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = notes


class _Window:
    def __init__(self, start, end):
        self.start = start
        self.end = end


def _buy(day, amount="1", price="100"):
    return _Tx("BUY", "IE00B4L5Y983", day, amount=amount, price=price)


def _monthly_buys(months: int, day_of_month: int = 5, amount="1"):
    """One purchase a month, same day, same size."""
    out = []
    for n in range(months):
        year = 2024 + (n // 12)
        month = (n % 12) + 1
        out.append(_buy(date(year, month, day_of_month), amount=amount))
    return out


def _window_over(months: int):
    last_year = 2024 + ((months - 1) // 12)
    last_month = ((months - 1) % 12) + 1
    return _Window(date(2024, 1, 1), date(last_year, last_month, 28))


def test_perfectly_regular_buying_has_no_variation_and_24_equivalents():
    result = analyse_purchase_regularity(_monthly_buys(24), _window_over(24))

    assert result.months_total == 24
    assert result.months_invested == 24
    assert result.invested_share == Decimal("1")
    assert result.variation_coefficient == pytest.approx(Decimal("0"), abs=1e-9)
    assert result.longest_gap_months == 0
    assert result.temporal_hhi == pytest.approx(Decimal("1") / Decimal("24"), abs=1e-9)
    assert result.equivalent_monthly_purchases == pytest.approx(Decimal("24"), abs=1e-6)


def test_everything_in_one_month_collapses_to_a_single_equivalent():
    txs = [_buy(date(2024, 6, 5), amount="10")]
    result = analyse_purchase_regularity(txs, _window_over(24))

    assert result.months_invested == 1
    assert result.temporal_hhi == pytest.approx(Decimal("1"), abs=1e-9)
    assert result.equivalent_monthly_purchases == pytest.approx(Decimal("1"), abs=1e-9)
    # A single purchase in month 6 of 24 leaves a run of 18 empty months after it.
    assert result.longest_gap_months == 18


def test_a_five_month_interruption_is_reported():
    txs = _monthly_buys(3) + _monthly_buys(3)[:0] + [_buy(date(2024, 9, 5))]
    result = analyse_purchase_regularity(txs, _Window(date(2024, 1, 1), date(2024, 9, 30)))

    # January, February, March, then nothing until September.
    assert result.longest_gap_months == 5
    assert result.months_invested == 4


def test_half_the_capital_in_three_months_shows_in_the_equivalents():
    txs = _monthly_buys(24, amount="1") + [
        _buy(date(2024, 3, 5), amount="8"),
        _buy(date(2024, 7, 5), amount="8"),
        _buy(date(2025, 2, 5), amount="8"),
    ]
    result = analyse_purchase_regularity(txs, _window_over(24))

    assert result.equivalent_monthly_purchases < Decimal("12")
    assert result.variation_coefficient > Decimal("1")


def test_buying_always_on_the_fifth_gives_a_zero_spread():
    result = analyse_purchase_regularity(_monthly_buys(24, day_of_month=5), _window_over(24))

    assert result.median_day_of_month == 5
    assert result.day_of_month_spread == Decimal("0")


def test_scattered_days_widen_the_spread():
    txs = [
        _buy(date(2024, (n % 12) + 1, ((n * 7) % 27) + 1))
        for n in range(24)
    ]
    result = analyse_purchase_regularity(txs, _window_over(12))

    assert result.day_of_month_spread > Decimal("5")


def test_the_day_of_month_needs_ten_purchases():
    result = analyse_purchase_regularity(_monthly_buys(9), _window_over(9))

    assert result.day_of_month_spread is None
    assert result.median_day_of_month is None


def test_a_short_window_is_not_measurable():
    result = analyse_purchase_regularity(_monthly_buys(3), _window_over(3))

    assert result.is_measurable is False


def test_two_purchases_are_not_measurable_even_over_two_years():
    txs = [_buy(date(2024, 1, 5)), _buy(date(2025, 6, 5))]
    result = analyse_purchase_regularity(txs, _window_over(24))

    assert result.is_measurable is False


def test_cash_rows_are_not_purchases():
    txs = [
        _Tx("DEPOSIT", "EUR", date(2024, 1, 2), amount="1000"),
        _Tx("WITHDRAW", "EUR", date(2024, 2, 2), amount="100"),
    ]

    assert analyse_purchase_regularity(txs, _window_over(24)) is None


def test_the_monthly_series_covers_every_month_of_the_window():
    result = analyse_purchase_regularity([_buy(date(2024, 3, 5))], _window_over(12))

    assert len(result.monthly) == 12
    assert result.monthly[0].amount == Decimal("0")
    assert result.monthly[2].amount == Decimal("100")


def test_deposits_use_the_same_indicators_and_drop_auto_provisions():
    txs = [
        _Tx("DEPOSIT", "EUR", date(2024, 1, 2), amount="500"),
        _Tx("DEPOSIT", "EUR", date(2024, 2, 2), amount="500"),
        _Tx("DEPOSIT", "EUR", date(2024, 3, 2), amount="500", notes="Provision automatique"),
    ]
    result = analyse_deposit_regularity(txs, _window_over(12))

    assert result.months_invested == 2
    assert result.total_invested == Decimal("1000")


# ── 2.4 · deposit to purchase lag ─────────────────────────────────────


def _deposit(day, amount, notes=None):
    return _Tx("DEPOSIT", "EUR", day, amount=amount, notes=notes)


def test_a_deposit_invested_the_next_day_has_a_one_day_lag():
    from services.analytics.behaviour import analyse_deposit_lag

    txs = [_deposit(date(2024, 1, 1), "100"), _buy(date(2024, 1, 2), amount="1", price="100")]
    lag = analyse_deposit_lag(txs)

    assert lag.median_days == Decimal("1")
    assert lag.matched_eur == Decimal("100")
    assert lag.unmatched_eur == Decimal("0")
    assert lag.is_measurable is True


def test_one_deposit_spread_over_three_purchases_weights_each_delay():
    from services.analytics.behaviour import analyse_deposit_lag

    txs = [
        _deposit(date(2024, 1, 1), "300"),
        _buy(date(2024, 1, 11), amount="1", price="100"),
        _buy(date(2024, 1, 21), amount="1", price="100"),
        _buy(date(2024, 1, 31), amount="1", price="100"),
    ]
    lag = analyse_deposit_lag(txs)

    assert lag.pairs == 3
    assert lag.q1_days == Decimal("10")
    assert lag.median_days == Decimal("20")
    assert lag.p90_days == Decimal("30")


def test_the_queue_is_fifo():
    from services.analytics.behaviour import analyse_deposit_lag

    txs = [
        _deposit(date(2024, 1, 1), "100"),
        _deposit(date(2024, 1, 20), "100"),
        _buy(date(2024, 1, 31), amount="1", price="100"),
    ]
    lag = analyse_deposit_lag(txs)

    # The oldest euro is spent first: 30 days, not 11.
    assert lag.median_days == Decimal("30")
    assert lag.never_invested_eur == Decimal("100")


def test_purchases_with_no_real_deposit_are_counted_apart_not_matched():
    from services.analytics.behaviour import analyse_deposit_lag

    txs = [
        _deposit(date(2024, 1, 5), "100", notes="Provision automatique"),
        _buy(date(2024, 1, 5), amount="1", price="100"),
    ]
    lag = analyse_deposit_lag(txs)

    assert lag.matched_eur == Decimal("0")
    assert lag.unmatched_eur == Decimal("100")
    assert lag.unmatched_share == Decimal("1")
    assert lag.is_measurable is False


def test_a_ledger_mostly_auto_provisioned_is_not_measurable():
    from services.analytics.behaviour import analyse_deposit_lag

    txs = [_deposit(date(2024, 1, 1), "100"), _buy(date(2024, 1, 2), amount="1", price="100")]
    for n in range(3):
        day = date(2024, 2 + n, 5)
        txs += [_deposit(day, "100", notes="Provision automatique"), _buy(day, amount="1", price="100")]

    lag = analyse_deposit_lag(txs)

    assert lag.unmatched_share > Decimal("0.5")
    assert lag.is_measurable is False


def test_a_deposit_after_the_purchase_never_funds_it():
    from services.analytics.behaviour import analyse_deposit_lag

    txs = [_buy(date(2024, 1, 2), amount="1", price="100"), _deposit(date(2024, 3, 1), "100")]
    lag = analyse_deposit_lag(txs)

    assert lag.pairs == 0
    assert lag.unmatched_eur == Decimal("100")
    assert lag.never_invested_eur == Decimal("100")


def test_no_purchase_at_all_yields_nothing():
    from services.analytics.behaviour import analyse_deposit_lag

    assert analyse_deposit_lag([_deposit(date(2024, 1, 1), "100")]) is None


# ── turnover ──────────────────────────────────────────────────────────


def _sell(day, amount="1", price="100"):
    return _Tx("SELL", "IE00B4L5Y983", day, amount=amount, price=price)


def test_a_buy_and_hold_portfolio_has_no_turnover():
    from services.analytics.behaviour import analyse_turnover

    result = analyse_turnover(_monthly_buys(24), _window_over(24), Decimal("10000"))

    assert result.sales_eur == Decimal("0")
    assert result.annual_rate == Decimal("0")


def test_turnover_takes_the_smaller_side_and_annualises_it():
    from services.analytics.behaviour import analyse_turnover

    # Over roughly two years: 2400 bought, 1200 sold, 1000 average capital.
    txs = _monthly_buys(24, amount="1") + [_sell(date(2025, 6, 5), amount="12")]
    result = analyse_turnover(txs, _window_over(24), Decimal("1000"))

    assert result.purchases_eur == Decimal("2400")
    assert result.sales_eur == Decimal("1200")
    # min(2400, 1200) / 1000 = 1.2 over ~1.9 years.
    assert result.annual_rate == pytest.approx(Decimal("0.63"), abs=Decimal("0.03"))


def test_turnover_needs_capital_to_divide_by():
    from services.analytics.behaviour import analyse_turnover

    result = analyse_turnover(_monthly_buys(24), _window_over(24), Decimal("0"))

    assert result.annual_rate is None
    assert result.is_measurable is False


# ── 3.2 · exits: disposition, euro cost, closed episodes ──────────────

TODAY = date(2026, 7, 30)


def _quotes(asset_key, start, days, start_price=100, drift=0.0):
    from datetime import timedelta as _td

    return {
        asset_key: {
            start + _td(days=n): Decimal(str(round(start_price * (1 + drift * n), 4)))
            for n in range(days)
        }
    }


def test_selling_only_winners_pushes_the_ratio_above_one():
    from services.analytics.behaviour import analyse_exits

    txs = [
        _buy(date(2024, 1, 5), amount="10", price="100"),
        _Tx("BUY", "FR0000120271", date(2024, 1, 5), amount="10", price="100"),
        # The first line doubled, the second halved; only the winner is sold.
        _Tx("SELL", "IE00B4L5Y983", date(2024, 6, 5), amount="10", price="200"),
    ]
    matrix = {"FR0000120271": {date(2024, 6, 5): Decimal("50")}}

    exits = analyse_exits(txs, matrix, today=TODAY)

    assert exits.realised_gains == 1
    assert exits.realised_losses == 0
    assert exits.paper_losses == 1
    assert exits.pgr == Decimal("1")
    assert exits.plr == Decimal("0")


def test_three_sales_stay_under_the_realisation_gate():
    from services.analytics.behaviour import analyse_exits

    txs = [_buy(date(2024, 1, 5), amount="30", price="100")]
    for n in range(3):
        txs.append(_Tx("SELL", "IE00B4L5Y983", date(2024, 6 + n, 5), amount="1", price="150"))

    exits = analyse_exits(txs, {}, today=TODAY)

    assert exits.realisations == 3
    assert exits.is_measurable is False


def test_a_portfolio_that_never_sells_still_returns_a_block():
    from services.analytics.behaviour import analyse_exits

    exits = analyse_exits(_monthly_buys(12), {}, today=TODAY)

    assert exits is not None
    assert exits.realisations == 0
    assert exits.episodes == []
    assert exits.is_measurable is False


def test_a_recent_sale_is_excluded_from_the_euro_cost_and_counted():
    from services.analytics.behaviour import analyse_exits

    recent = TODAY - timedelta(days=60)
    txs = [
        _buy(date(2024, 1, 5), amount="10", price="100"),
        _Tx("SELL", "IE00B4L5Y983", recent, amount="10", price="150"),
    ]

    exits = analyse_exits(txs, {}, today=TODAY)

    assert exits.recent_sales == 1
    assert exits.measured_sales == 0
    assert exits.cost_eur is None


def test_selling_a_line_that_then_beat_the_index_shows_a_positive_cost():
    from services.analytics.behaviour import analyse_exits

    sold_on = date(2024, 6, 5)
    # The sold line doubles over the year, the index is flat.
    asset = {
        "IE00B4L5Y983": {sold_on: Decimal("100"), sold_on + timedelta(days=365): Decimal("200")}
    }
    benchmark = {sold_on: Decimal("50"), sold_on + timedelta(days=365): Decimal("50")}
    txs = [
        _buy(date(2024, 1, 5), amount="10", price="100"),
        _Tx("SELL", "IE00B4L5Y983", sold_on, amount="10", price="100"),
    ]

    exits = analyse_exits(txs, asset, benchmark, today=TODAY)

    assert exits.measured_sales == 1
    # 1000 EUR sold, the line then gained 100% against a flat index.
    assert exits.cost_eur == pytest.approx(Decimal("1000"), abs=Decimal("1"))


def test_buying_back_a_closed_line_opens_a_second_episode():
    from services.analytics.behaviour import analyse_exits

    txs = [
        _buy(date(2024, 1, 5), amount="10", price="100"),
        _Tx("SELL", "IE00B4L5Y983", date(2024, 3, 5), amount="10", price="120"),
        _buy(date(2024, 6, 5), amount="10", price="130"),
        _Tx("SELL", "IE00B4L5Y983", date(2024, 9, 5), amount="10", price="110"),
    ]

    exits = analyse_exits(txs, {}, today=TODAY)

    assert len(exits.episodes) == 2
    assert exits.episodes[0].profit == Decimal("200")
    assert exits.episodes[1].profit == Decimal("-200")
    assert exits.hit_rate == Decimal("0.5")
    assert exits.payoff_ratio == Decimal("1")


def test_a_partial_sale_does_not_close_an_episode():
    from services.analytics.behaviour import analyse_exits

    txs = [
        _buy(date(2024, 1, 5), amount="10", price="100"),
        _Tx("SELL", "IE00B4L5Y983", date(2024, 3, 5), amount="4", price="120"),
    ]

    exits = analyse_exits(txs, {}, today=TODAY)

    assert exits.episodes == []
    assert exits.has_episodes is False


# ── Deployment regularity ────────────────────────────────────────────────
#
# The measure that judges regularity. It reads the cumulative capital curve, so
# it has no notion of a calendar month and cannot be fooled by one: buying every
# 30 days used to score 92% of months invested and one month of interruption
# against 100% and none for the exact same discipline anchored on the 6th.

_DEPLOY_START = date(2024, 1, 1)
_DEPLOY_END = date(2026, 1, 31)


def _deployment_window():
    return _Window(_DEPLOY_START, _DEPLOY_END)


def _buys_on(days):
    return [_buy(day) for day in days]


def _every_n_days(step: int, count: int):
    return [_DEPLOY_START + timedelta(days=step * n) for n in range(count)]


def _on_the_sixth():
    days = [date(y, m, 6) for y in (2024, 2025) for m in range(1, 13)]
    days.append(date(2026, 1, 6))
    return [day for day in days if _DEPLOY_START <= day <= _DEPLOY_END]


def test_a_thirty_day_rhythm_scores_like_a_fixed_day_of_the_month():
    """The test that proves the month-boundary artefact is gone.

    Same discipline, two anchors. The old monthly reading gave 92% / CV 0.400 to
    one and 100% / CV 0.000 to the other.
    """
    drifting = analyse_purchase_regularity(_buys_on(_every_n_days(30, 26)), _deployment_window())
    anchored = analyse_purchase_regularity(_buys_on(_on_the_sixth()), _deployment_window())

    assert drifting.deployment_gap == pytest.approx(anchored.deployment_gap, abs=0.01)
    # Both are a straight line in practice: the residue is the staircase of
    # discrete orders, about 1/(2n).
    assert drifting.deployment_gap < Decimal("0.05")
    assert anchored.deployment_gap < Decimal("0.05")


def test_bringing_one_purchase_forward_barely_moves_the_score():
    reference = _every_n_days(30, 26)
    early = [day - timedelta(days=7) if i == 12 else day for i, day in enumerate(reference)]

    baseline = analyse_purchase_regularity(_buys_on(reference), _deployment_window())
    moved = analyse_purchase_regularity(_buys_on(early), _deployment_window())

    assert moved.deployment_gap == pytest.approx(baseline.deployment_gap, abs=0.005)


def test_a_skipped_month_barely_moves_the_score():
    reference = _every_n_days(30, 26)
    skipped = [day for i, day in enumerate(reference) if i != 7]

    baseline = analyse_purchase_regularity(_buys_on(reference), _deployment_window())
    result = analyse_purchase_regularity(_buys_on(skipped), _deployment_window())

    assert result.deployment_gap == pytest.approx(baseline.deployment_gap, abs=0.01)


def test_all_the_capital_in_one_month_degrades_the_score():
    """A lump sum is not a rhythm, and the curve says so without ambiguity."""
    result = analyse_purchase_regularity(
        _buys_on(_every_n_days(5, 6)), _deployment_window()
    )

    assert result.deployment_gap > Decimal("0.4")


def test_the_cadence_names_a_day_of_the_month_when_that_is_the_tighter_rhythm():
    result = analyse_purchase_regularity(_buys_on(_on_the_sixth()), _deployment_window())

    assert result.cadence.label == "achats autour du 6 du mois"
    assert result.cadence.median_day_of_month == 6


def test_the_cadence_names_an_interval_when_the_day_of_month_drifts():
    result = analyse_purchase_regularity(_buys_on(_every_n_days(30, 26)), _deployment_window())

    assert result.cadence.label == "achats espacés de 30 jours en médiane"
    assert result.cadence.median_gap_days == 30


def test_too_few_purchases_leave_the_cadence_undescribed():
    result = analyse_purchase_regularity(_buys_on(_every_n_days(30, 2)), _deployment_window())

    assert result.cadence.label == ""
    assert result.cadence.median_gap_days is None
