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
