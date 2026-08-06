from datetime import date, datetime
from decimal import Decimal

from services.analytics.universe import traded_assets


class _Tx:
    def __init__(self, tx_type, asset_key, amount, day, price="100"):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(str(amount))
        self.price_per_unit = Decimal(price)
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)


def test_nothing_traded_is_an_empty_list():
    assert traded_assets([]) == []
    assert traded_assets(None) == []


def test_a_held_line_carries_its_dates_and_cost():
    rows = traded_assets(
        [
            _Tx("BUY", "AAA", 10, date(2024, 1, 5)),
            _Tx("BUY", "AAA", 5, date(2025, 3, 5)),
        ]
    )

    assert len(rows) == 1
    assert rows[0].held is True
    assert rows[0].invested_eur == Decimal("1500")
    assert rows[0].first_bought == date(2024, 1, 5)
    assert rows[0].last_activity == date(2025, 3, 5)


def test_a_line_sold_to_zero_is_still_offered():
    """A plan can name a line being wound down; it must remain selectable."""
    rows = traded_assets(
        [
            _Tx("BUY", "AAA", 10, date(2024, 1, 5)),
            _Tx("SELL", "AAA", 10, date(2024, 9, 5)),
        ]
    )

    assert [(row.asset_key, row.held) for row in rows] == [("AAA", False)]
    assert rows[0].last_activity == date(2024, 9, 5)


def test_cash_is_not_a_line_to_allocate_to():
    rows = traded_assets(
        [_Tx("DEPOSIT", "EUR", 1000, date(2024, 1, 1)), _Tx("BUY", "EUR", 1000, date(2024, 1, 1))]
    )

    assert rows == []


def test_held_lines_come_first_then_the_largest():
    rows = traded_assets(
        [
            _Tx("BUY", "SMALL", 1, date(2024, 1, 5)),
            _Tx("BUY", "BIG", 50, date(2024, 1, 5)),
            _Tx("BUY", "SOLD", 100, date(2024, 1, 5)),
            _Tx("SELL", "SOLD", 100, date(2024, 2, 5)),
        ]
    )

    assert [row.asset_key for row in rows] == ["BIG", "SMALL", "SOLD"]
