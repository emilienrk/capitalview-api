from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from services.analytics.execution import (
    MIN_ORDERS,
    analyse_execution,
    buy_orders,
    month_quotes,
)
from services.analytics.timing import rng

JANUARY = date(2026, 1, 1)


class _Tx:
    def __init__(self, tx_type, asset_key, day, amount="10", price="100"):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(amount)
        self.price_per_unit = Decimal(price)
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)


def _month(prices: list[str], start: date = JANUARY) -> dict[date, Decimal]:
    """One quote per day from `start`, as a sparse (unfilled) series."""
    return {start + timedelta(days=i): Decimal(p) for i, p in enumerate(prices)}


def test_only_real_purchases_count_as_orders():
    txs = [
        _Tx("BUY", "AAA", JANUARY),
        _Tx("SELL", "AAA", JANUARY),
        _Tx("DEPOSIT", "EUR", JANUARY),
        _Tx("DIVIDEND", "AAA", JANUARY),
    ]

    assert [tx.type for tx in buy_orders(txs)] == ["BUY"]


def test_month_quotes_keeps_only_the_order_s_own_month():
    quotes = {**_month(["100", "101"]), date(2026, 2, 3): Decimal("110")}

    assert set(month_quotes(quotes, date(2026, 1, 5))) == set(_month(["100", "101"]))


def test_paying_exactly_the_monthly_average_is_zero_slippage():
    prices = ["90", "100", "110"]  # mean = 100
    result = analyse_execution(
        [_Tx("BUY", "AAA", JANUARY, price="100")], {"AAA": _month(prices)}, draws=50
    )

    assert result.orders[0].twap == Decimal("100")
    assert result.orders[0].slippage_bps == Decimal("0")
    assert result.weighted_slippage_bps == Decimal("0")


def test_buying_at_the_monthly_high_shows_a_positive_slippage():
    result = analyse_execution(
        [_Tx("BUY", "AAA", JANUARY, price="110")], {"AAA": _month(["90", "100", "110"])}, draws=50
    )

    # 110 vs a TWAP of 100 is +1000 bps.
    assert result.orders[0].slippage_bps == Decimal("1000")
    assert result.cost_eur == Decimal("110")  # 10% of a 1100 EUR notional


def test_buying_below_the_monthly_average_shows_a_negative_slippage():
    result = analyse_execution(
        [_Tx("BUY", "AAA", JANUARY, price="90")], {"AAA": _month(["90", "100", "110"])}, draws=50
    )

    assert result.orders[0].slippage_bps == Decimal("-1000")
    assert result.cost_eur < 0


def test_a_large_order_outweighs_a_small_one():
    """The aggregate is notional-weighted, so size decides who moves the number."""
    txs = [
        _Tx("BUY", "AAA", JANUARY, amount="100", price="110"),
        _Tx("BUY", "AAA", JANUARY + timedelta(days=1), amount="1", price="90"),
    ]

    result = analyse_execution(txs, {"AAA": _month(["90", "100", "110"])}, draws=50)

    assert result.weighted_slippage_bps > Decimal("800")


def test_a_month_with_a_single_quote_still_yields_a_twap():
    result = analyse_execution(
        [_Tx("BUY", "AAA", JANUARY, price="105")], {"AAA": _month(["100"])}, draws=50
    )

    assert result.orders[0].twap == Decimal("100")
    assert result.orders[0].slippage_bps == Decimal("500")


def test_an_asset_with_no_quotes_at_all_is_skipped_not_guessed():
    result = analyse_execution([_Tx("BUY", "NOPE", JANUARY)], {}, draws=50)

    assert result.orders == []
    assert result.weighted_slippage_bps is None
    assert result.permutation is None


def test_quartiles_describe_the_whole_distribution():
    txs = [
        _Tx("BUY", "AAA", JANUARY + timedelta(days=i), price=p)
        for i, p in enumerate(["90", "100", "110"])
    ]

    result = analyse_execution(txs, {"AAA": _month(["90", "100", "110"])}, draws=50)

    low, q1, median, q3, high = result.quartiles
    assert low < q1 <= median <= q3 < high


def test_orders_spread_across_the_month_are_not_detectable():
    """No timing bias must not come back as a finding."""
    prices = [str(100 + (i % 5)) for i in range(28)]
    quotes = _month(prices)
    txs = [
        _Tx("BUY", "AAA", day, price=str(price))
        for day, price in list(quotes.items())[::3]
    ]

    result = analyse_execution(txs, {"AAA": quotes}, draws=2000, rng=rng(7))

    assert result.permutation is not None
    assert result.permutation.is_detectable is False


def test_always_buying_the_monthly_high_is_detectable():
    quotes = {}
    txs = []
    for month in range(1, 13):
        start = date(2026, month, 1)
        prices = [Decimal(str(100 + i)) for i in range(20)]
        for i, price in enumerate(prices):
            quotes[start + timedelta(days=i)] = price
        # Systematically the most expensive day of every month.
        txs.append(_Tx("BUY", "AAA", start + timedelta(days=19), price=str(prices[-1])))

    result = analyse_execution(txs, {"AAA": quotes}, draws=2000, rng=rng(7))

    assert result.weighted_slippage_bps > 0
    assert result.permutation.is_detectable is True


def test_gate_thresholds_match_the_spec():
    assert MIN_ORDERS == 10


def test_the_result_is_reproducible_across_runs():
    quotes = _month([str(100 + (i % 7)) for i in range(28)])
    txs = [_Tx("BUY", "AAA", day, price=str(price)) for day, price in list(quotes.items())[::4]]

    first = analyse_execution(txs, {"AAA": quotes}, draws=500, rng=rng())
    second = analyse_execution(txs, {"AAA": quotes}, draws=500, rng=rng())

    assert first.permutation.p_value == second.permutation.p_value


# ── Plausibility of the prices themselves ────────────────────────────────
#
# A real portfolio produced -129 bps on 77 orders with p = 0.000, and whiskers
# from -1000 to +1500 bps: 25% of intra-month amplitude on broad ETFs. The signal
# was not execution, it was the same fund quoted on a different venue than the
# one the orders were placed on. The permutation test cannot catch it — it
# compares days with each other, never sources with each other.


def _flat_month(price: str = "100", days: int = 20):
    return {"AAA": _month([price] * days)}


def _orders_at(price: str, count: int = 20):
    return [_Tx("BUY", "AAA", JANUARY + timedelta(days=i), price=price) for i in range(count)]


def test_a_normal_series_is_plausible():
    result = analyse_execution(_orders_at("101"), _flat_month(), rng=rng(0))

    assert result.is_plausible is True
    assert result.median_absolute_bps == pytest.approx(Decimal("100"), abs=1)


def test_a_venue_mismatch_is_flagged_as_implausible():
    """Every order 12% off the stored close: that is another instrument."""
    result = analyse_execution(_orders_at("112"), _flat_month(), rng=rng(0))

    assert result.median_absolute_bps > Decimal("300")
    assert result.is_plausible is False


def test_one_bad_order_does_not_condemn_the_series():
    """The median, not the mean: a single fat finger must not gate the block."""
    orders = _orders_at("101", count=19) + [_Tx("BUY", "AAA", JANUARY, price="150")]

    result = analyse_execution(orders, _flat_month(), rng=rng(0))

    assert result.is_plausible is True


def test_an_implausible_series_withholds_every_value_and_says_why():
    from services.analytics.report import _execution_payload

    result = analyse_execution(_orders_at("112"), _flat_month(), rng=rng(0))
    payload = _execution_payload(result, None)

    assert payload["slippage_bps"]["value"] is None
    assert payload["cost_eur"]["value"] is None
    assert payload["prices_are_plausible"] is False
    # A pattern detected on prices we cannot trust is not a finding.
    assert payload["is_detectable"] is False
    assert "place de cotation" in payload["verdict"]
