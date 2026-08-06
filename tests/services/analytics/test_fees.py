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


class _FeeTx:
    def __init__(self, notional, fee, day):
        self.type = "BUY"
        self.asset_key = "AAA"
        self.amount = Decimal("1")
        self.price_per_unit = Decimal(str(notional))
        self.fees = Decimal(str(fee))
        self.executed_at = datetime(day.year, day.month, day.day, 10)


class _TwoYears:
    start = date(2024, 1, 5)
    end = date(2026, 1, 5)


def _ledger(charged: int, total: int = 24, notional="450", fee="6.25"):
    """`total` monthly buys, of which only the first `charged` carry a fee."""
    return [
        _FeeTx(notional, fee if n < charged else "0", date(2024 + n // 12, n % 12 + 1, 5))
        for n in range(total)
    ]


def test_orders_with_no_fee_recorded_are_not_averaged_in():
    """Folding them in halves what the broker looks like it charges.

    Which then halves the threshold drawn from that average, and turns a ledger
    imported without a fee column into a fee habit nobody has.
    """
    result = analyse_fees(_ledger(charged=8), _TwoYears)

    assert result.orders_with_fee == 8
    # 50 € over the 8 charged orders, not over all 24.
    assert result.average_fee == Decimal("6.25")
    assert result.recorded_fees == Decimal("50.00")


def test_coverage_reports_how_much_of_the_ledger_carries_fees():
    result = analyse_fees(_ledger(charged=8), _TwoYears)

    assert round(result.coverage, 4) == Decimal("0.3333")
    assert analyse_fees(_ledger(charged=24), _TwoYears).coverage == Decimal("1")


def test_a_ledger_without_any_fee_says_nothing_rather_than_zero():
    """No fee recorded and a broker charging none look identical from here."""
    result = analyse_fees(_ledger(charged=0), _TwoYears)

    assert result.orders_with_fee == 0
    assert result.average_fee is None
    assert result.threshold_order_size is None
    # The gate is what folds the block away instead of asserting a zero bill.
    assert result.is_measurable is False


def test_free_orders_are_not_counted_under_the_threshold():
    """An order with no fee has no entry cost to exceed the target with."""
    result = analyse_fees(_ledger(charged=8), _TwoYears)

    assert result.orders_below_threshold == 8
    assert result.invested_below_threshold == Decimal("3600")


def test_the_verdict_stops_advising_a_regrouping_it_calls_harmless():
    """0,50 € on 150 € orders is 17 bps a year — under the 25 the block targets.

    It said "regroupe-les" all the same, one line under a tile stating the
    annual load was fine, which is how a rounding error read as a problem.
    """
    from services.analytics.report import _fees_payload

    ledger = _ledger(charged=24, notional="150", fee="0.50")
    payload = _fees_payload(analyse_fees(ledger, _TwoYears))

    # Every order under the threshold, and none of it worth acting on.
    assert payload["orders_below_threshold"] == 24
    assert payload["avoidable"] is False
    assert "Regroupe-les" not in payload["verdict"]
    assert "pas un problème à corriger" in payload["verdict"]


def test_the_verdict_still_advises_a_regrouping_when_the_load_is_real():
    from services.analytics.report import _fees_payload

    # 8 € per order on 200 € orders: 400 bps of entry, nothing rounding about it.
    payload = _fees_payload(analyse_fees(_ledger(charged=24, notional="200", fee="8"), _TwoYears))

    assert payload["avoidable"] is True
    assert "Regroupe-les" in payload["verdict"]


def test_a_partly_filled_ledger_is_extrapolated_not_reported_as_a_floor():
    """A fee nobody typed in was still paid.

    Reporting only what was keyed in understates the bill by exactly the share
    nobody filled — which is how 150 € of real cost showed up as 50 €.
    """
    result = analyse_fees(_ledger(charged=8), _TwoYears)

    assert result.is_estimated is True
    assert result.recorded_fees == Decimal("50.00")
    # The 16 orders with nothing recorded are charged at the same 6,25 €.
    assert result.total_fees == Decimal("150.00")


def test_a_complete_ledger_is_never_marked_as_an_estimate():
    result = analyse_fees(_ledger(charged=24), _TwoYears)

    assert result.is_estimated is False
    assert result.total_fees == result.recorded_fees
    assert result.coverage == Decimal("1")


def test_below_a_tenth_of_the_ledger_nothing_is_extrapolated():
    """Four fees out of two hundred orders do not describe a broker.

    Extrapolating from them would be a guess wearing a number's clothes, and
    reporting their sum would understate the bill fifty-fold. Neither happens.
    """
    result = analyse_fees(_ledger(charged=8, total=200), _TwoYears)

    assert result.coverage < Decimal("0.10")
    assert result.is_estimated is False
    assert result.is_too_partial is True
    # The gate is what folds the block away rather than showing either number.
    assert result.is_measurable is False


def test_an_estimated_total_carries_the_estimated_marker_not_a_plain_one():
    from services.analytics.report import _fees_payload

    payload = _fees_payload(analyse_fees(_ledger(charged=8), _TwoYears))

    assert payload["total_fees"]["reliability"] == "estimé"
    assert payload["annual_bps"]["reliability"] == "estimé"
    assert "Estimé" in payload["total_fees"]["caveat"]
    # The per-order calibration is measured on the charged orders, not guessed.
    assert payload["threshold_order_size"]["reliability"] != "estimé"
    assert payload["is_estimated"] is True
    assert payload["recorded_fees"] == Decimal("50.00")


def test_a_ledger_too_partial_to_estimate_says_so_and_withholds():
    from services.analytics.report import _fees_payload

    payload = _fees_payload(analyse_fees(_ledger(charged=8, total=200), _TwoYears))

    assert payload["total_fees"]["value"] is None
    assert payload["total_fees"]["reliability"] == "insuffisant"
    assert "trop peu pour estimer le reste" in payload["total_fees"]["caveat"]


def test_no_fee_at_all_offers_the_three_readings_rather_than_picking_one():
    """Included in the price is likelier than free, and neither is measurable."""
    from services.analytics.report import _fees_payload

    verdict = _fees_payload(analyse_fees(_ledger(charged=0), _TwoYears))["verdict"]

    assert "déjà compris dans les prix" in verdict
    assert "rien n'est mesuré" in verdict
