import math
from datetime import date, datetime, timedelta
from decimal import Decimal

from services.analytics.concentration import (
    MIN_OVERLAP,
    analyse_concentration,
    holdings_from_transactions,
    portfolio_weights,
)

START = date(2023, 1, 2)


def _sessions(count: int) -> list[date]:
    out: list[date] = []
    day = START
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _series(sessions, factory) -> dict[date, Decimal]:
    return {day: Decimal(str(round(factory(i), 6))) for i, day in enumerate(sessions)}


def _quotes_two_assets(count=400, correlated=True):
    sessions = _sessions(count)
    base = _series(sessions, lambda i: 100 * (1 + 0.001 * math.sin(i / 5)))
    if correlated:
        other = _series(sessions, lambda i: 50 * (1 + 0.001 * math.sin(i / 5)))
    else:
        other = _series(sessions, lambda i: 50 * (1 + 0.001 * math.cos(i / 3)))
    return {"AAA": base, "BBB": other}


def test_two_perfectly_correlated_lines_are_about_one_bet():
    quotes = _quotes_two_assets(correlated=True)
    holdings = {"AAA": Decimal("1"), "BBB": Decimal("2")}
    prices = {"AAA": Decimal("100"), "BBB": Decimal("50")}

    result = analyse_concentration(holdings, prices, quotes)

    assert result.lines == 2
    assert result.effective_positions > Decimal("1.9")
    assert result.independent_bets < Decimal("1.2")
    assert result.max_correlation > Decimal("0.99")


def test_two_uncorrelated_lines_are_about_two_bets():
    quotes = _quotes_two_assets(correlated=False)
    holdings = {"AAA": Decimal("1"), "BBB": Decimal("2")}
    prices = {"AAA": Decimal("100"), "BBB": Decimal("50")}

    result = analyse_concentration(holdings, prices, quotes)

    assert result.independent_bets > Decimal("1.7")
    assert abs(result.max_correlation) < Decimal("0.5")


def test_a_dominant_line_collapses_the_effective_positions():
    quotes = _quotes_two_assets()
    holdings = {"AAA": Decimal("98"), "BBB": Decimal("2")}
    prices = {"AAA": Decimal("1"), "BBB": Decimal("1")}

    result = analyse_concentration(holdings, prices, quotes)

    assert result.effective_positions < Decimal("1.1")


def test_a_short_history_withholds_the_independent_bets():
    quotes = _quotes_two_assets(count=100)
    holdings = {"AAA": Decimal("1"), "BBB": Decimal("1")}
    prices = {"AAA": Decimal("100"), "BBB": Decimal("50")}

    result = analyse_concentration(holdings, prices, quotes)

    assert result.independent_bets is None
    assert result.correlations == []
    assert result.is_measurable is False
    # The weight-based count needs no history and stays available.
    assert result.effective_positions is not None


def test_a_single_line_has_no_independent_bets_to_count():
    sessions = _sessions(400)
    quotes = {"AAA": _series(sessions, lambda i: 100 + i)}

    result = analyse_concentration({"AAA": Decimal("1")}, {"AAA": Decimal("100")}, quotes)

    assert result.lines == 1
    assert result.independent_bets is None


def test_a_thinly_quoted_line_is_dropped_and_named():
    quotes = _quotes_two_assets()
    sessions = _sessions(10)
    quotes["CCC"] = _series(sessions, lambda i: 10 + i)
    holdings = {"AAA": Decimal("1"), "BBB": Decimal("1"), "CCC": Decimal("1")}
    prices = {"AAA": Decimal("100"), "BBB": Decimal("50"), "CCC": Decimal("10")}

    result = analyse_concentration(holdings, prices, quotes)

    assert result.dropped == ["CCC"]
    assert result.independent_bets is not None


def test_a_constant_price_does_not_raise():
    sessions = _sessions(MIN_OVERLAP + 50)
    quotes = {
        "AAA": _series(sessions, lambda i: 100),
        "BBB": _series(sessions, lambda i: 50 + 0.01 * i),
    }
    holdings = {"AAA": Decimal("1"), "BBB": Decimal("1")}
    prices = {"AAA": Decimal("100"), "BBB": Decimal("50")}

    result = analyse_concentration(holdings, prices, quotes)

    assert result is not None


def test_cash_is_not_a_bet():
    assert portfolio_weights({"EUR": Decimal("1000")}, {"EUR": Decimal("1")}) == [
        ("EUR", Decimal("1"))
    ]


class _Tx:
    def __init__(self, tx_type, asset_key, amount):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(str(amount))
        self.executed_at = datetime(2024, 1, 2, 10, 0)


def test_holdings_net_sales_against_purchases():
    txs = [
        _Tx("BUY", "AAA", 10),
        _Tx("SELL", "AAA", 4),
        _Tx("BUY", "BBB", 3),
        _Tx("SELL", "BBB", 3),
        _Tx("DEPOSIT", "EUR", 1000),
    ]

    assert holdings_from_transactions(txs) == {"AAA": Decimal("6")}
