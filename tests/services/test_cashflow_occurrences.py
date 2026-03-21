"""Tests for get_cashflow_occurrences — the recurring cashflow scheduling engine."""

from datetime import date
from decimal import Decimal

import pytest

from dtos.cashflow import CashflowResponse
from models.enums import FlowType, Frequency
from services.cashflow import get_cashflow_occurrences


def _cf(frequency: Frequency, transaction_date: date) -> CashflowResponse:
    """Build a minimal CashflowResponse for testing occurrences."""
    return CashflowResponse(
        id="test",
        name="Test",
        flow_type=FlowType.OUTFLOW,
        category="test",
        amount=Decimal("100"),
        frequency=frequency,
        transaction_date=transaction_date,
        monthly_amount=Decimal("100"),
        bank_account_id=None,
        created_at=date(2026, 1, 1),
        updated_at=date(2026, 1, 1),
    )


# ─── ONCE ────────────────────────────────────────────────────


class TestOnce:
    def test_fires_when_in_range(self):
        cf = _cf(Frequency.ONCE, date(2026, 3, 10))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 21))
        assert result == [date(2026, 3, 10)]

    def test_does_not_fire_on_from_date(self):
        # from_date is exclusive
        cf = _cf(Frequency.ONCE, date(2026, 3, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 21))
        assert result == []

    def test_fires_on_to_date(self):
        # to_date is inclusive
        cf = _cf(Frequency.ONCE, date(2026, 3, 21))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 21))
        assert result == [date(2026, 3, 21)]

    def test_does_not_fire_before_range(self):
        cf = _cf(Frequency.ONCE, date(2026, 2, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 21))
        assert result == []

    def test_does_not_fire_after_range(self):
        cf = _cf(Frequency.ONCE, date(2026, 4, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 21))
        assert result == []

    def test_empty_range(self):
        cf = _cf(Frequency.ONCE, date(2026, 3, 10))
        result = get_cashflow_occurrences(cf, date(2026, 3, 21), date(2026, 3, 21))
        assert result == []


# ─── DAILY ───────────────────────────────────────────────────


class TestDaily:
    def test_counts_each_day(self):
        cf = _cf(Frequency.DAILY, date(2026, 3, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 5))
        assert result == [
            date(2026, 3, 2),
            date(2026, 3, 3),
            date(2026, 3, 4),
            date(2026, 3, 5),
        ]

    def test_single_day(self):
        cf = _cf(Frequency.DAILY, date(2026, 3, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 4), date(2026, 3, 5))
        assert result == [date(2026, 3, 5)]

    def test_reference_in_future(self):
        # Starts from reference; if reference > to_date → empty
        cf = _cf(Frequency.DAILY, date(2026, 4, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 21))
        assert result == []

    def test_crosses_month_boundary(self):
        cf = _cf(Frequency.DAILY, date(2026, 3, 29))
        result = get_cashflow_occurrences(cf, date(2026, 3, 29), date(2026, 4, 2))
        assert result == [
            date(2026, 3, 30),
            date(2026, 3, 31),
            date(2026, 4, 1),
            date(2026, 4, 2),
        ]


# ─── WEEKLY ──────────────────────────────────────────────────


class TestWeekly:
    def test_fires_every_7_days(self):
        # Reference: Monday 2 March 2026, from_date March 1 (exclusive)
        # March 2 is strictly after from_date → it fires too
        cf = _cf(Frequency.WEEKLY, date(2026, 3, 2))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 25))
        assert result == [
            date(2026, 3, 2),
            date(2026, 3, 9),
            date(2026, 3, 16),
            date(2026, 3, 23),
        ]

    def test_exactly_on_next_occurrence(self):
        cf = _cf(Frequency.WEEKLY, date(2026, 3, 2))
        result = get_cashflow_occurrences(cf, date(2026, 3, 8), date(2026, 3, 9))
        assert result == [date(2026, 3, 9)]

    def test_no_occurrences_when_range_too_short(self):
        cf = _cf(Frequency.WEEKLY, date(2026, 3, 2))
        result = get_cashflow_occurrences(cf, date(2026, 3, 2), date(2026, 3, 8))
        assert result == []

    def test_reference_in_future(self):
        cf = _cf(Frequency.WEEKLY, date(2026, 5, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 21))
        assert result == []


# ─── MONTHLY ─────────────────────────────────────────────────


class TestMonthly:
    def test_fires_same_day_each_month(self):
        # Salary on the 28th
        cf = _cf(Frequency.MONTHLY, date(2026, 1, 28))
        result = get_cashflow_occurrences(cf, date(2026, 1, 28), date(2026, 3, 28))
        assert result == [date(2026, 2, 28), date(2026, 3, 28)]

    def test_fires_on_to_date(self):
        cf = _cf(Frequency.MONTHLY, date(2026, 2, 15))
        result = get_cashflow_occurrences(cf, date(2026, 2, 14), date(2026, 3, 15))
        assert result == [date(2026, 2, 15), date(2026, 3, 15)]

    def test_does_not_double_count_on_from_date(self):
        # from_date is exclusive
        cf = _cf(Frequency.MONTHLY, date(2026, 3, 5))
        result = get_cashflow_occurrences(cf, date(2026, 3, 5), date(2026, 4, 5))
        assert result == [date(2026, 4, 5)]

    def test_clamps_to_short_month(self):
        # Reference is the 31st — February only has 28 days in 2026
        cf = _cf(Frequency.MONTHLY, date(2026, 1, 31))
        result = get_cashflow_occurrences(cf, date(2026, 1, 31), date(2026, 3, 1))
        assert result == [date(2026, 2, 28)]

    def test_clamps_to_short_month_then_restores(self):
        # Jan 31 → Feb 28 → Mar 31
        cf = _cf(Frequency.MONTHLY, date(2026, 1, 31))
        result = get_cashflow_occurrences(cf, date(2026, 1, 31), date(2026, 4, 1))
        assert result == [date(2026, 2, 28), date(2026, 3, 31)]

    def test_no_occurrence_when_next_is_after_range(self):
        cf = _cf(Frequency.MONTHLY, date(2026, 3, 20))
        result = get_cashflow_occurrences(cf, date(2026, 3, 20), date(2026, 4, 19))
        assert result == []

    def test_single_occurrence(self):
        cf = _cf(Frequency.MONTHLY, date(2026, 1, 5))
        result = get_cashflow_occurrences(cf, date(2026, 2, 4), date(2026, 2, 5))
        assert result == [date(2026, 2, 5)]

    def test_reference_far_in_past(self):
        # Reference is 2 years ago — should fire normally in range
        cf = _cf(Frequency.MONTHLY, date(2024, 3, 10))
        result = get_cashflow_occurrences(cf, date(2026, 3, 9), date(2026, 3, 10))
        assert result == [date(2026, 3, 10)]


# ─── YEARLY ──────────────────────────────────────────────────


class TestYearly:
    def test_fires_once_per_year(self):
        cf = _cf(Frequency.YEARLY, date(2024, 6, 15))
        result = get_cashflow_occurrences(cf, date(2024, 6, 14), date(2026, 7, 1))
        assert result == [date(2024, 6, 15), date(2025, 6, 15), date(2026, 6, 15)]

    def test_does_not_fire_on_from_date(self):
        cf = _cf(Frequency.YEARLY, date(2025, 3, 21))
        result = get_cashflow_occurrences(cf, date(2025, 3, 21), date(2026, 3, 21))
        assert result == [date(2026, 3, 21)]

    def test_no_occurrence_in_range(self):
        cf = _cf(Frequency.YEARLY, date(2025, 12, 31))
        result = get_cashflow_occurrences(cf, date(2026, 1, 1), date(2026, 11, 30))
        assert result == []

    def test_leap_year_feb29_on_non_leap(self):
        # Defined on Feb 29 leap year → fires Feb 28 on normal years
        cf = _cf(Frequency.YEARLY, date(2024, 2, 29))
        result = get_cashflow_occurrences(cf, date(2024, 2, 29), date(2025, 3, 1))
        assert result == [date(2025, 2, 28)]

    def test_reference_far_in_past(self):
        cf = _cf(Frequency.YEARLY, date(2020, 1, 1))
        result = get_cashflow_occurrences(cf, date(2025, 12, 31), date(2026, 1, 1))
        assert result == [date(2026, 1, 1)]


# ─── Edge cases ───────────────────────────────────────────────


class TestEdgeCases:
    def test_inverted_range_returns_empty(self):
        cf = _cf(Frequency.MONTHLY, date(2026, 1, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 2, 1))
        assert result == []

    def test_same_from_and_to_returns_empty(self):
        cf = _cf(Frequency.MONTHLY, date(2026, 1, 1))
        result = get_cashflow_occurrences(cf, date(2026, 3, 1), date(2026, 3, 1))
        assert result == []
