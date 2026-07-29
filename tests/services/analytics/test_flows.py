from datetime import date, datetime
from decimal import Decimal

from services.analytics.flows import (
    is_auto_provision,
    stock_external_flow_for_day,
    stock_external_flows,
)


class _Tx:
    """Minimal stand-in for TransactionResponse — the flow helpers only read these fields."""

    def __init__(self, tx_type, asset_key, amount, day, notes=None):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(str(amount))
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = notes


D1 = date(2026, 1, 5)
D2 = date(2026, 1, 6)


def test_deposit_is_positive_and_withdraw_is_negative():
    txs = [
        _Tx("DEPOSIT", "EUR", 1000, D1),
        _Tx("WITHDRAW", "EUR", 250, D1),
    ]
    assert stock_external_flow_for_day(txs, D1) == Decimal("750")


def test_buys_and_sells_are_not_external_flows():
    txs = [
        _Tx("BUY", "IE00B4L5Y983", 5, D1),
        _Tx("SELL", "IE00B4L5Y983", 2, D1),
        _Tx("DIVIDEND", "IE00B4L5Y983", 3, D1),
    ]
    assert stock_external_flow_for_day(txs, D1) == Decimal("0")


def test_flows_are_grouped_by_day():
    txs = [
        _Tx("DEPOSIT", "EUR", 1000, D1),
        _Tx("DEPOSIT", "EUR", 400, D2),
        _Tx("WITHDRAW", "EUR", 100, D2),
    ]
    assert stock_external_flows(txs) == {D1: Decimal("1000"), D2: Decimal("300")}


def test_auto_provisions_are_detected():
    auto = _Tx("DEPOSIT", "EUR", 500, D1, notes="Provision automatique")
    manual = _Tx("DEPOSIT", "EUR", 500, D1, notes="Virement mensuel")
    assert is_auto_provision(auto) is True
    assert is_auto_provision(manual) is False


def test_auto_provisions_included_by_default_excluded_on_request():
    txs = [
        _Tx("DEPOSIT", "EUR", 500, D1, notes="Provision automatique"),
        _Tx("DEPOSIT", "EUR", 300, D1),
    ]
    assert stock_external_flow_for_day(txs, D1) == Decimal("800")
    assert stock_external_flow_for_day(txs, D1, include_auto_provisions=False) == Decimal("300")
    assert stock_external_flows(txs, include_auto_provisions=False) == {D1: Decimal("300")}


def test_days_without_external_flow_are_absent_from_the_mapping():
    txs = [_Tx("BUY", "IE00B4L5Y983", 5, D1)]
    assert stock_external_flows(txs) == {}
