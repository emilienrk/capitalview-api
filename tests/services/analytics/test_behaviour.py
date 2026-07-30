from datetime import date, datetime
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
