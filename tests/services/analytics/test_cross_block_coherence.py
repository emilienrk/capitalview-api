"""The page must not contradict itself from one block to the next.

Two pairs of numbers measure the same phenomenon through different lenses. If
their signs disagree, one block tells the user selling was a mistake while
another congratulates them on the same trade.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from services.analytics.behaviour import analyse_exits
from services.analytics.counterfactual import build_bridge
from services.analytics.execution import analyse_execution
from services.analytics.window import AnalysisWindow

ASSET = "AAA"
BENCH = "IE00B4L5Y983"

END = date.today() - timedelta(days=1)
START = END - timedelta(days=1095)
SOLD_ON = START + timedelta(days=180)


class _Tx:
    def __init__(self, tx_type, asset_key, day, amount="10", price="100", fees="0"):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(amount)
        self.price_per_unit = Decimal(price)
        self.fees = Decimal(fees)
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = None
        self.currency = "EUR"


def _rising(start_price: float, end_price: float) -> dict[date, Decimal]:
    """A line that climbs steadily across the whole window."""
    days = (END - START).days
    step = (end_price - start_price) / days
    return {
        START + timedelta(days=n): Decimal(str(round(start_price + step * n, 4)))
        for n in range(days + 1)
    }


def _flat(price: float) -> dict[date, Decimal]:
    days = (END - START).days
    return {START + timedelta(days=n): Decimal(str(price)) for n in range(days + 1)}


def _window() -> AnalysisWindow:
    return AnalysisWindow(
        start=START,
        end=END,
        days=(END - START).days,
        asset_keys=[ASSET],
        benchmark_key=BENCH,
        benchmark_from=START,
        benchmark_covers_window=True,
        clamped_start=None,
    )


def test_a_bad_exit_reads_as_bad_in_both_blocks():
    """Sold a line that then doubled, against a flat index.

    The bridge's exits term is signed as a contribution to wealth (negative =
    the exit destroyed value), the exits block is signed as a cost (positive =
    the exit gave value up). Opposite signs, same story — and this test is what
    keeps them from drifting into agreeing by accident.
    """
    prices = {ASSET: _rising(100, 300), BENCH: _flat(100)}
    transactions = [
        _Tx("DEPOSIT", "EUR", START, amount="5000", price="1"),
        _Tx("BUY", ASSET, START, amount="10", price="100"),
        _Tx("SELL", ASSET, SOLD_ON, amount="10", price=str(prices[ASSET][SOLD_ON])),
    ]
    price_end = {ASSET: prices[ASSET][END], BENCH: prices[BENCH][END]}

    bridge = build_bridge(_window(), transactions, prices, price_end)
    exits = analyse_exits(transactions, prices, prices[BENCH], today=END)

    exit_term = next(step.amount for step in bridge.steps if step.key == "exits")

    assert exit_term < 0, "the bridge must read the exit as value destroyed"
    assert exits.cost_eur > 0, "the exits block must read the same exit as a cost"


def test_a_good_exit_reads_as_good_in_both_blocks():
    """Sold a line that then collapsed, against a flat index."""
    prices = {ASSET: _rising(300, 100), BENCH: _flat(100)}
    transactions = [
        _Tx("DEPOSIT", "EUR", START, amount="5000", price="1"),
        _Tx("BUY", ASSET, START, amount="10", price="300"),
        _Tx("SELL", ASSET, SOLD_ON, amount="10", price=str(prices[ASSET][SOLD_ON])),
    ]
    price_end = {ASSET: prices[ASSET][END], BENCH: prices[BENCH][END]}

    bridge = build_bridge(_window(), transactions, prices, price_end)
    exits = analyse_exits(transactions, prices, prices[BENCH], today=END)

    exit_term = next(step.amount for step in bridge.steps if step.key == "exits")

    assert exit_term > 0
    assert exits.cost_eur < 0


def test_execution_slippage_and_the_bridge_execution_term_disagree_in_sign():
    """Paying above the month's average is a positive slippage and a negative term.

    Same invariant as M2 recorded: basis points up means euros down.
    """
    prices = {ASSET: _flat(100), BENCH: _flat(100)}
    transactions = [
        _Tx("DEPOSIT", "EUR", START, amount="5000", price="1"),
        # Paid 120 in a month whose every close is 100.
        _Tx("BUY", ASSET, START + timedelta(days=5), amount="10", price="120"),
    ]
    price_end = {ASSET: prices[ASSET][END], BENCH: prices[BENCH][END]}

    bridge = build_bridge(_window(), transactions, prices, price_end)
    execution = analyse_execution(transactions, prices, draws=200)

    execution_term = next(step.amount for step in bridge.steps if step.key == "execution")

    assert execution.weighted_slippage_bps > 0
    assert execution_term < 0
