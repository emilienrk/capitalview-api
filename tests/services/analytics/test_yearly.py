"""Each calendar year beside the benchmark (services/analytics/yearly.py)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from models.enums import PlacementEntryType
from services.analytics.benchmark import total_return
from services.analytics.period import PocketInputs
from services.analytics.yearly import measure_years
from services.placement import EntryPoint, PlacementTimeline


def _daily(start: date, end: date, value) -> list[tuple[date, Decimal]]:
    """A flat daily series, `value(day)` per calendar day."""
    return [(start + timedelta(days=n), Decimal(value(start + timedelta(days=n)))) for n in range((end - start).days + 1)]


def _index(prices: dict[date, str], start: date, end: date) -> dict[date, Decimal]:
    series, last = {}, None
    for n in range((end - start).days + 1):
        day = start + timedelta(days=n)
        last = Decimal(prices[day]) if day in prices else last
        if last is not None:
            series[day] = last
    return series


def test_each_year_separates_what_was_paid_in_from_what_was_earned():
    """1 000 € in June 2024, worth 1 100 € at year end; 500 € more in 2025, 1 800 € today."""
    opened, today = date(2024, 6, 1), date(2025, 9, 29)

    def value(day):
        if day < date(2024, 12, 31):
            return "1000"
        if day < date(2025, 3, 1):
            return "1100"
        return "1600" if day < date(2025, 9, 1) else "1800"

    inputs = PocketInputs(
        stocks=(_daily(opened, today, value), {opened: Decimal("1000"), date(2025, 3, 1): Decimal("500")}),
        crypto=([], {}),
    )
    index = _index({date(2024, 5, 31): "80", date(2024, 6, 1): "80", date(2024, 12, 31): "88", today: "96.8"}, date(2023, 12, 31), today)

    first, current = measure_years(inputs, index, today)

    assert (first.year, first.covered_from, first.complete) == (2024, opened, False)
    assert first.net_contributions == Decimal("1000") and first.gain == Decimal("100")
    # The index is measured from the day the pocket opened, not from January.
    assert first.benchmark_start == opened
    assert first.benchmark_return == pytest.approx(Decimal("0.10"))

    assert (current.year, current.end, current.complete) == (2025, today, False)
    assert current.net_contributions == Decimal("500")
    assert current.gain == Decimal("200")
    assert current.benchmark_return == pytest.approx(Decimal("0.10"))


def test_a_closed_year_held_from_before_it_is_complete():
    today = date(2026, 2, 1)
    inputs = PocketInputs(stocks=(_daily(date(2024, 12, 1), today, lambda _: "100"), {}), crypto=([], {}))

    years = {year.year: year for year in measure_years(inputs, {}, today)}

    assert years[2025].complete is True
    assert years[2025].covered_from == date(2025, 1, 1)
    # No quote for the index: the comparison is withheld, not zero.
    assert years[2025].benchmark_return is None


def test_a_placement_does_not_appear_in_the_years_before_it_existed():
    today = date(2025, 12, 31)
    placement = PlacementTimeline([
        EntryPoint(date(2025, 4, 1), PlacementEntryType.DEPOSIT, Decimal("500")),
        EntryPoint(date(2025, 12, 1), PlacementEntryType.VALUATION, Decimal("520")),
    ])
    inputs = PocketInputs(
        stocks=(_daily(date(2024, 1, 1), today, lambda _: "100"), {date(2024, 1, 1): Decimal("100")}),
        crypto=([], {}),
        placements=[placement],
    )

    years = {year.year: year for year in measure_years(inputs, {}, today)}

    assert "placements" not in years[2024].pockets
    assert years[2025].pockets["placements"].gain == Decimal("20")
    assert years[2025].gain == Decimal("20")


def test_nothing_held_is_no_year_at_all():
    assert measure_years(PocketInputs(stocks=([], {}), crypto=([], {})), {}, date(2026, 1, 1)) == []


def test_the_benchmark_return_needs_a_quote_at_both_ends():
    series = {date(2025, 1, 1): Decimal("100"), date(2025, 6, 1): Decimal("110")}

    assert total_return(series, date(2025, 1, 1), date(2025, 6, 1)) == Decimal("0.1")
    assert total_return(series, date(2024, 12, 31), date(2025, 6, 1)) is None
