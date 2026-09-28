"""How a pocket did over a window (services/analytics/period.py)."""

from datetime import date
from decimal import Decimal

import pytest

from services.analytics.period import _series_period


def test_a_deposit_is_value_and_flow_never_gain():
    """1 000 € grows 10 %, then 1 000 € more arrives: 100 € earned, not 1 100 €."""
    series = [
        (date(2026, 1, 1), Decimal("1000")),
        (date(2026, 2, 1), Decimal("1100")),
        (date(2026, 2, 2), Decimal("2100")),
    ]
    flows = {date(2026, 2, 2): Decimal("1000")}

    period = _series_period(series, flows, date(2026, 1, 1), date(2026, 3, 1))

    assert period.value_start == Decimal("1000")
    assert period.value_end == Decimal("2100")
    assert period.net_contributions == Decimal("1000")
    assert period.gain == Decimal("100")
    assert period.time_weighted_return == pytest.approx(Decimal("0.10"))
    # Two months: no annualised figure.
    assert period.annualised_return is None


def test_the_window_starts_from_the_last_value_before_it():
    series = [(date(2025, 12, 20), Decimal("500")), (date(2026, 1, 15), Decimal("550"))]

    period = _series_period(series, {}, date(2026, 1, 1), date(2026, 1, 31))

    assert (period.start, period.value_start, period.gain) == (date(2025, 12, 20), Decimal("500"), Decimal("50"))


def test_a_pocket_opened_inside_the_window_counts_its_opening_deposit_as_flow():
    series = [(date(2026, 3, 1), Decimal("1000")), (date(2026, 4, 1), Decimal("1050"))]
    flows = {date(2026, 3, 1): Decimal("1000")}

    period = _series_period(series, flows, date(2026, 1, 1), date(2026, 6, 30))

    assert period.value_start == 0
    assert period.net_contributions == Decimal("1000")
    assert period.gain == Decimal("50")
    assert "ouverte" in period.warnings[0]


def test_nothing_before_the_end_is_an_empty_period():
    period = _series_period([(date(2026, 5, 1), Decimal("10"))], {}, date(2026, 1, 1), date(2026, 3, 1))

    assert period.gain is None and period.value_end is None
