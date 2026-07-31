from datetime import date, datetime, timedelta
from decimal import Decimal

from services.analytics.counterfactual import MIN_COVERED_DAYS, build_bridge
from services.analytics.window import AnalysisWindow

BENCH = "IE00B4L5Y983"
START = date(2024, 1, 2)
END = date(2026, 1, 2)


class _Tx:
    def __init__(self, tx_type, asset_key, day, amount="10", price="100", fees="0"):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(amount)
        self.price_per_unit = Decimal(price)
        self.fees = Decimal(fees)
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)


def _window(start=START, end=END, covers=True, benchmark_from=date(2010, 1, 1)):
    return AnalysisWindow(
        start=start,
        end=end,
        days=(end - start).days,
        asset_keys=[BENCH],
        benchmark_key=BENCH,
        benchmark_from=benchmark_from,
        benchmark_covers_window=covers,
        clamped_start=None if covers else benchmark_from,
    )


def _flat_quotes(keys, price="100", start=START, end=END):
    """One quote per day at a constant price, for every asset."""
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    return {key: {d: Decimal(price) for d in days} for key in keys}


def _reconciles(bridge) -> bool:
    total = bridge.baseline + sum(s.amount for s in bridge.steps) + bridge.residual
    return abs(total - bridge.final) < Decimal("0.01")


def test_the_chain_reconciles_exactly():
    """Blocking guarantee from spec section 11."""
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="5000", price="1"),
        _Tx("BUY", "AAA", date(2024, 3, 5), amount="10", price="105", fees="2"),
        _Tx("BUY", BENCH, date(2024, 9, 9), amount="5", price="98", fees="1"),
        _Tx("SELL", "AAA", date(2025, 6, 2), amount="4", price="120", fees="1"),
        _Tx("DIVIDEND", "AAA", date(2025, 7, 1), amount="1", price="30"),
    ]
    matrix = _flat_quotes(["AAA", BENCH])
    price_end = {"AAA": Decimal("130"), BENCH: Decimal("115")}

    bridge = build_bridge(_window(), txs, matrix, price_end)

    assert bridge is not None
    assert _reconciles(bridge)
    assert bridge.residual == Decimal("0")


def test_the_residual_is_exposed_rather_than_absorbed():
    """An asset with no terminal price must show up, not vanish into a term."""
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="2000", price="1"),
        _Tx("BUY", "GHOST", date(2024, 3, 5), amount="10", price="100"),
        _Tx("BUY", BENCH, date(2024, 4, 5), amount="5", price="100"),
    ]
    matrix = _flat_quotes(["GHOST", BENCH])
    price_end = {BENCH: Decimal("120")}  # GHOST is missing on purpose

    bridge = build_bridge(_window(), txs, matrix, price_end)

    assert _reconciles(bridge)


def test_a_portfolio_that_mirrors_the_robot_has_no_timing_or_selection_effect():
    days = [date(2024, m, 1) for m in range(1, 13)]
    txs = [_Tx("DEPOSIT", "EUR", START, amount="12000", price="1")]
    txs += [_Tx("BUY", BENCH, d, amount="10", price="100") for d in days]
    matrix = _flat_quotes([BENCH])
    price_end = {BENCH: Decimal("100")}

    bridge = build_bridge(_window(), txs, matrix, price_end)
    by_key = {s.key: s.amount for s in bridge.steps}

    assert abs(by_key["timing"]) < Decimal("0.01")
    assert abs(by_key["selection"]) < Decimal("0.01")
    assert abs(by_key["execution"]) < Decimal("0.01")


def test_fees_always_subtract():
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="2000", price="1"),
        _Tx("BUY", BENCH, date(2024, 3, 5), amount="10", price="100", fees="25"),
    ]
    bridge = build_bridge(_window(), txs, _flat_quotes([BENCH]), {BENCH: Decimal("100")})

    assert {s.key: s.amount for s in bridge.steps}["fees"] == Decimal("-25")


def test_idle_cash_sits_on_both_sides_rather_than_inside_the_chain():
    """A large untouched deposit must not read as investing skill.

    The robot is handed the same leftover cash, so the headline cost reflects
    decisions only; the forgone return on that cash is reported separately.
    """
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="5000", price="1"),
        _Tx("BUY", BENCH, date(2024, 3, 5), amount="10", price="100"),
    ]
    bridge = build_bridge(_window(), txs, _flat_quotes([BENCH]), {BENCH: Decimal("100")})

    # 5000 in, 1000 deployed.
    assert bridge.idle_cash == Decimal("4000")
    assert "cash_drag" not in {s.key for s in bridge.steps}
    assert abs(bridge.behaviour_cost) < Decimal("0.01")


def test_idle_cash_reports_the_return_it_forwent():
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="5000", price="1"),
        _Tx("BUY", BENCH, date(2024, 3, 5), amount="10", price="100"),
    ]
    # Index doubled over the window: the idle 4000 gave up 4000.
    bridge = build_bridge(_window(), txs, _flat_quotes([BENCH]), {BENCH: Decimal("200")})

    assert bridge.idle_cash_opportunity == Decimal("4000")


def test_selling_before_a_rise_shows_a_negative_exit_effect():
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="2000", price="1"),
        _Tx("BUY", "AAA", date(2024, 3, 5), amount="10", price="100"),
        _Tx("SELL", "AAA", date(2024, 9, 5), amount="10", price="100"),
    ]
    matrix = _flat_quotes(["AAA", BENCH])
    bridge = build_bridge(_window(), txs, matrix, {"AAA": Decimal("180"), BENCH: Decimal("100")})

    # Sold for 1000 what would now be worth 1800.
    assert {s.key: s.amount for s in bridge.steps}["exits"] == Decimal("-800")


def test_the_substitution_order_is_part_of_the_output():
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="2000", price="1"),
        _Tx("BUY", BENCH, date(2024, 3, 5)),
    ]
    bridge = build_bridge(_window(), txs, _flat_quotes([BENCH]), {BENCH: Decimal("100")})

    assert bridge.order == ["timing", "selection", "execution", "fees", "exits"]


def test_a_window_shorter_than_a_year_is_refused():
    short_end = START + timedelta(days=MIN_COVERED_DAYS - 1)
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="2000", price="1"),
        _Tx("BUY", BENCH, date(2024, 3, 5)),
    ]

    bridge = build_bridge(
        _window(end=short_end), txs, _flat_quotes([BENCH], end=short_end), {BENCH: Decimal("100")}
    )

    assert bridge is None


def test_a_benchmark_younger_than_the_history_truncates_and_says_so():
    launched = date(2024, 6, 1)
    window = _window(start=date(2022, 1, 3), covers=False, benchmark_from=launched)
    txs = [
        _Tx("DEPOSIT", "EUR", date(2022, 1, 3), amount="5000", price="1"),
        # Before the benchmark existed: outside the comparable period.
        _Tx("BUY", BENCH, date(2022, 2, 1)),
        _Tx("BUY", BENCH, date(2024, 9, 1)),
    ]
    matrix = _flat_quotes([BENCH], start=launched)

    bridge = build_bridge(window, txs, matrix, {BENCH: Decimal("100")})

    assert bridge.truncated is True
    assert bridge.covered_from == launched
    assert _reconciles(bridge)


def _execution_pair(paid: str):
    """Same purchases, measured by both blocks."""
    from services.analytics.execution import analyse_execution

    quotes = {}
    txs = [_Tx("DEPOSIT", "EUR", START, amount="20000", price="1")]
    for month in range(1, 13):
        first = date(2024, month, 1)
        for i, price in enumerate(["90", "100", "110"]):
            quotes[first + timedelta(days=i)] = Decimal(price)  # month TWAP = 100
        txs.append(_Tx("BUY", "AAA", first, amount="10", price=paid))

    matrix = {"AAA": quotes, BENCH: _flat_quotes([BENCH])[BENCH]}
    bridge = build_bridge(_window(), txs, matrix, {"AAA": Decimal("100"), BENCH: Decimal("100")})
    execution = analyse_execution(txs, matrix, draws=50)
    return bridge, execution


def test_the_two_blocks_agree_on_the_direction_of_the_execution_effect():
    """The bridge and the slippage block must not contradict each other.

    They report the same gap in opposite units: paying above the monthly average
    is a positive slippage in bps and a negative amount of euros, because the same
    money buys fewer units.
    """
    dear_bridge, dear_execution = _execution_pair("110")
    cheap_bridge, cheap_execution = _execution_pair("90")

    dear_step = {s.key: s.amount for s in dear_bridge.steps}["execution"]
    cheap_step = {s.key: s.amount for s in cheap_bridge.steps}["execution"]

    assert dear_execution.weighted_slippage_bps > 0 and dear_step < 0
    assert cheap_execution.weighted_slippage_bps < 0 and cheap_step > 0


def test_no_purchase_at_all_yields_no_bridge():
    txs = [_Tx("DEPOSIT", "EUR", START, amount="2000", price="1")]

    assert build_bridge(_window(), txs, _flat_quotes([BENCH]), {BENCH: Decimal("100")}) is None


def test_an_unusable_benchmark_yields_no_bridge():
    txs = [
        _Tx("DEPOSIT", "EUR", START, amount="2000", price="1"),
        _Tx("BUY", BENCH, date(2024, 3, 5)),
    ]

    assert build_bridge(_window(), txs, {}, {BENCH: Decimal("100")}) is None
    assert build_bridge(_window(), txs, _flat_quotes([BENCH]), {}) is None
