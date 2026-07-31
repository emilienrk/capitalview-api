from datetime import date, datetime
from decimal import Decimal

import pytest

from services.analytics.fees import TARGET_BPS, TER_NOTE, analyse_fees


class _Tx:
    def __init__(self, tx_type, asset_key, day, amount="1", price="100", fees="0"):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(str(amount))
        self.price_per_unit = Decimal(str(price))
        self.fees = Decimal(str(fees))
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = None


class _Window:
    def __init__(self, start, end):
        self.start = start
        self.end = end


TWO_YEARS = _Window(date(2024, 1, 1), date(2025, 12, 31))


def _buy(day, notional="1000", fees="4.20"):
    return _Tx("BUY", "IE00B4L5Y983", day, amount="1", price=notional, fees=fees)


def _orders(count: int, notional="1000", fees="4.20"):
    return [
        _buy(date(2024 + n // 12, n % 12 + 1, 5), notional=notional, fees=fees)
        for n in range(count)
    ]


def test_the_threshold_is_where_a_flat_fee_costs_25_bps():
    result = analyse_fees(_orders(12, fees="4.20"), TWO_YEARS)

    # 4.20 EUR is 25 bps of 1680 EUR.
    assert result.threshold_order_size == pytest.approx(Decimal("1680"), abs=Decimal("0.01"))
    assert result.average_fee == Decimal("4.20")


def test_orders_under_the_threshold_are_counted_and_costed():
    txs = _orders(6, notional="3000", fees="4.20") + _orders(4, notional="500", fees="4.20")
    result = analyse_fees(txs, TWO_YEARS)

    assert result.orders_below_threshold == 4
    assert result.cost_below_threshold == Decimal("16.80")
    assert result.invested_below_threshold == Decimal("2000")


def test_fees_are_reported_as_a_share_and_in_annual_bps():
    result = analyse_fees(_orders(10, notional="1000", fees="5"), TWO_YEARS)

    assert result.total_fees == Decimal("50")
    assert result.deployed_capital == Decimal("10000")
    assert result.fee_share == Decimal("0.005")
    # 50 bps spread over roughly two years.
    assert result.annual_bps == pytest.approx(Decimal("25"), abs=Decimal("1"))


def test_the_ter_note_is_always_there_even_with_no_fees():
    result = analyse_fees(_orders(8, fees="0"), TWO_YEARS)

    assert result.total_fees == Decimal("0")
    assert result.threshold_order_size is None
    assert result.orders_below_threshold == 0
    assert result.ter_note == TER_NOTE


def test_too_few_orders_is_not_measurable():
    result = analyse_fees(_orders(3), TWO_YEARS)

    assert result.is_measurable is False


def test_the_projection_compounds_the_current_cadence():
    result = analyse_fees(_orders(10, fees="5"), TWO_YEARS)

    # Twenty years of roughly 25 EUR a year, compounded at 5%: far above the raw sum.
    assert result.projection_eur > Decimal("500")


def test_cash_rows_and_sales_carry_no_purchase_fee():
    txs = [
        _Tx("DEPOSIT", "EUR", date(2024, 1, 2), amount="1000", price="1", fees="2"),
        _Tx("SELL", "IE00B4L5Y983", date(2024, 2, 2), amount="1", price="100", fees="3"),
    ]

    assert analyse_fees(txs, TWO_YEARS) is None


def test_the_target_is_the_documented_25_bps():
    assert TARGET_BPS == Decimal("25")


# ── The advice must not contradict the figure above it ───────────────────
#
# On a real portfolio the page showed "coût annuel +20 bps" and, right below,
# "77 de tes 77 ordres sont sous le seuil — regroupe-les". Both numbers were
# right; the advice was not. The threshold was calibrated for a 4.20 EUR broker,
# and at 0.68 EUR an order every order sits under it while the whole load stays
# a fraction of the target.


# The real portfolio the addendum describes: 77 orders of about 136 EUR over
# 31 months, at 0.68 EUR each. Every order is under the threshold, and the whole
# fee load is still 20 bps a year.
REAL_WINDOW = _Window(date(2023, 7, 1), date(2026, 1, 31))


def _cheap_broker_orders():
    return [
        _buy(date(2023 + (6 + n // 3) // 12, (6 + n // 3) % 12 + 1, 5),
             notional="136", fees="0.68")
        for n in range(77)
    ]


def test_a_cheap_broker_does_not_trigger_the_grouping_advice():
    from services.analytics.report import _fees_verdict

    fees = analyse_fees(_cheap_broker_orders(), REAL_WINDOW)

    assert fees.orders_below_threshold == 77
    assert fees.annual_bps < TARGET_BPS
    assert fees.is_avoidable is False
    assert "regrouper" not in _fees_verdict(fees, fees.threshold_order_size)


def test_an_expensive_broker_still_gets_the_advice():
    from services.analytics.report import _fees_verdict

    fees = analyse_fees(_orders(24, notional="200", fees="4.20"), TWO_YEARS)

    assert fees.annual_bps > TARGET_BPS
    assert fees.is_avoidable is True
    assert "regrouper" in _fees_verdict(fees, fees.threshold_order_size)


def test_the_threshold_stays_visible_as_calibration():
    """Suppressing the advice must not suppress the number behind it."""
    from services.analytics.report import _fees_verdict

    fees = analyse_fees(_cheap_broker_orders(), REAL_WINDOW)
    verdict = _fees_verdict(fees, fees.threshold_order_size)

    assert str(round(fees.threshold_order_size)) in verdict
    assert "calibrage" in verdict


def test_no_orders_below_the_threshold_is_not_avoidable_either():
    fees = analyse_fees(_orders(24, notional="100000", fees="4.20"), TWO_YEARS)

    assert fees.orders_below_threshold == 0
    assert fees.is_avoidable is False
