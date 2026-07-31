from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from services.analytics.window import resolve_window

TODAY = date(2026, 7, 30)
YESTERDAY = TODAY - timedelta(days=1)


class _Tx:
    """Minimal stand-in for TransactionResponse."""

    def __init__(self, tx_type, asset_key, day, amount="10", price="100", currency="EUR"):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(amount)
        self.price_per_unit = Decimal(price)
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.currency = currency


def _run(transactions, benchmark_first_quote=None, benchmark_key="IE00B4L5Y983"):
    """Drive resolve_window with the market layer stubbed out.

    ensure_price_history and the FX helper both hit the network in real life; the
    point of these tests is which *arguments* they receive.
    """
    quotes = {}
    if benchmark_first_quote is not None:
        quotes[benchmark_key] = benchmark_first_quote

    with (
        patch("services.analytics.window.date") as mock_date,
        patch("services.analytics.window.ensure_price_history") as ensure,
        patch("services.analytics.window.get_historical_exchange_rates_db") as fx,
        patch(
            "services.analytics.window.first_quote_date",
            side_effect=lambda _session, key: quotes.get(key),
        ),
    ):
        mock_date.today.return_value = TODAY
        mock_date.side_effect = date
        window = resolve_window(None, transactions, benchmark_key)
    return window, ensure, fx


def test_three_years_of_history_yields_a_three_year_window():
    start = date(2023, 7, 30)
    txs = [
        _Tx("DEPOSIT", "EUR", start - timedelta(days=10)),
        _Tx("BUY", "IE00B4L5Y983", start),
        _Tx("BUY", "IE00B4L5Y983", date(2026, 6, 1)),
    ]

    window, ensure, _fx = _run(txs, benchmark_first_quote=date(2010, 1, 1))

    assert window.start == start
    assert window.end == YESTERDAY
    assert window.days == (YESTERDAY - start).days
    # Prices are fetched for exactly the user's own depth, never a fixed lookback
    # — except the benchmark, which needs a further year to have a trailing high
    # on the window's first day.
    from services.analytics.window import LOOKBACK_DAYS

    fetched = {call.args[1]: call.args[3] for call in ensure.call_args_list}
    assert fetched["IE00B4L5Y983"] == start - timedelta(days=LOOKBACK_DAYS)

    held_only, _ensure_held, _fx = _run(
        [_Tx("BUY", "FR0000120271", start)], benchmark_first_quote=date(2010, 1, 1)
    )
    assert held_only.asset_keys == ["FR0000120271"]
    held_calls = {call.args[1]: call.args[3] for call in _ensure_held.call_args_list}
    assert held_calls["FR0000120271"] == start


def test_the_window_opens_on_the_first_buy_not_the_first_deposit():
    """A deposit is not an investment decision (spec section 0 bis)."""
    txs = [
        _Tx("DEPOSIT", "EUR", date(2024, 1, 2)),
        _Tx("BUY", "IE00B4L5Y983", date(2024, 6, 15)),
    ]

    window, _ensure, _fx = _run(txs, benchmark_first_quote=date(2010, 1, 1))

    assert window.start == date(2024, 6, 15)


def test_every_asset_ever_held_is_backfilled_but_never_eur():
    txs = [
        _Tx("BUY", "IE00B4L5Y983", date(2024, 1, 2)),
        _Tx("BUY", "US0378331005", date(2024, 3, 4)),
        _Tx("SELL", "US0378331005", date(2025, 1, 1)),
        _Tx("DEPOSIT", "EUR", date(2024, 1, 1)),
    ]

    window, ensure, _fx = _run(txs, benchmark_first_quote=date(2010, 1, 1))

    assert set(window.asset_keys) == {"IE00B4L5Y983", "US0378331005"}
    backfilled = {call.args[1] for call in ensure.call_args_list}
    assert "EUR" not in backfilled
    # The benchmark is backfilled too, even when never held.
    assert "IE00B4L5Y983" in backfilled


def test_non_eur_currencies_are_fetched_over_the_same_span():
    start = date(2024, 1, 2)
    txs = [
        _Tx("BUY", "US0378331005", start, currency="USD"),
        _Tx("BUY", "IE00B4L5Y983", date(2025, 1, 2)),
    ]

    _window, _ensure, fx = _run(txs, benchmark_first_quote=date(2010, 1, 1))

    assert fx.called
    currencies = {call.args[1] for call in fx.call_args_list}
    assert currencies == {"USD"}
    for call in fx.call_args_list:
        assert call.args[2] == start and call.args[3] == YESTERDAY


def test_a_benchmark_younger_than_the_history_clamps_and_flags():
    start = date(2022, 1, 3)
    launched = date(2024, 5, 1)
    txs = [_Tx("BUY", "IE00B4L5Y983", start), _Tx("BUY", "IE00B4L5Y983", date(2026, 1, 5))]

    window, _ensure, _fx = _run(txs, benchmark_first_quote=launched)

    assert window.benchmark_covers_window is False
    assert window.benchmark_from == launched
    assert window.clamped_start == launched
    # The raw window is untouched — only the comparison start moves.
    assert window.start == start


def test_a_benchmark_with_no_quotes_at_all_is_not_covered():
    txs = [_Tx("BUY", "IE00B4L5Y983", date(2024, 1, 2))]

    window, _ensure, _fx = _run(txs, benchmark_first_quote=None)

    assert window.benchmark_covers_window is False
    assert window.benchmark_from is None
    assert window.clamped_start is None


def test_no_buy_at_all_yields_an_empty_window():
    txs = [_Tx("DEPOSIT", "EUR", date(2024, 1, 2))]

    window, ensure, _fx = _run(txs, benchmark_first_quote=date(2010, 1, 1))

    assert window.start is None
    assert window.days == 0
    assert window.asset_keys == []
    # Nothing to analyse means nothing to fetch — no pointless network work.
    assert not ensure.called


def test_effective_start_is_the_clamp_when_the_benchmark_is_short():
    start = date(2022, 1, 3)
    launched = date(2024, 5, 1)
    txs = [_Tx("BUY", "IE00B4L5Y983", start)]

    window, _ensure, _fx = _run(txs, benchmark_first_quote=launched)

    assert window.effective_start == launched
    assert window.effective_days == (YESTERDAY - launched).days


# ── resolve_trading_days ──────────────────────────────────────────────


def _seed_asset(session, asset_key, exchange):
    from models.enums import AssetType
    from models.market import MarketAsset

    session.add(
        MarketAsset(
            asset_key=asset_key,
            symbol=asset_key,
            name=asset_key,
            asset_type=AssetType.STOCK,
            exchange=exchange,
        )
    )
    session.commit()


def test_trading_days_come_from_the_exchange_calendar(session):
    from services.analytics.window import resolve_trading_days

    _seed_asset(session, "IE00B4L5Y983", "XPAR")

    days = resolve_trading_days(
        session, ["IE00B4L5Y983"], date(2026, 1, 1), date(2026, 1, 12)
    )

    sessions = days["IE00B4L5Y983"]
    assert date(2026, 1, 3) not in sessions  # Saturday
    assert date(2026, 1, 4) not in sessions  # Sunday
    assert date(2026, 1, 5) in sessions  # Monday


def test_an_asset_without_a_known_exchange_is_left_out(session):
    from services.analytics.window import resolve_trading_days

    _seed_asset(session, "NOMIC", None)
    _seed_asset(session, "UNKNOWN_MIC", "XXXX_UNKNOWN")

    days = resolve_trading_days(
        session, ["NOMIC", "UNKNOWN_MIC"], date(2026, 1, 1), date(2026, 1, 12)
    )

    # Absent rather than empty: callers must fall back to quoted days, not to a
    # calendar that says nothing ever traded.
    assert days == {}
