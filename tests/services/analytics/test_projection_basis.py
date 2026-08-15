"""The measured defaults a projection starts from.

Each case here is one whose right answer can be worked out by hand, because the
point of the module is that its figures are checkable rather than plausible.
"""

import datetime
from decimal import Decimal

from services.analytics.projection_basis import (
    EXTREME_ANNUAL_RATE,
    MIN_DAYS_FOR_A_RATE,
    CategoryBasis,
    _category_basis,
    average_monthly_contribution,
)


class _Tx:
    """The three attributes the flow reader looks at."""

    def __init__(self, day: datetime.date, amount: str, type_: str = "DEPOSIT", notes=None):
        self.executed_at = datetime.datetime.combine(day, datetime.time())
        self.amount = Decimal(amount)
        self.type = type_
        self.asset_key = "EUR"
        self.notes = notes


def test_the_average_is_the_net_flow_over_the_months_it_spans():
    """Thirteen 500 deposits and one 600 withdrawal, over the 360 days spanned."""
    flows = {
        datetime.date(2025, 1, 15) + datetime.timedelta(days=30 * index): Decimal("500")
        for index in range(13)
    }
    flows[datetime.date(2025, 7, 20)] = Decimal("-600")

    average, months, total = average_monthly_contribution(flows)

    assert months == 11  # 360 days spanned / 30.4375, truncated
    assert total == Decimal("5900")  # 13 × 500 - 600
    assert average == total / (Decimal(360) / Decimal("30.4375"))


def test_a_withdrawal_heavy_ledger_reports_negative_saving():
    """Taking more out than in is disinvestment, not zero — the sign is the news."""
    flows = {
        datetime.date(2026, 1, 1): Decimal("1000"),
        datetime.date(2026, 7, 1): Decimal("-3000"),
    }

    average, _, total = average_monthly_contribution(flows)

    assert total == Decimal("-2000")
    assert average < 0


def test_flows_inside_a_single_month_are_not_divided_by_zero():
    """Two deposits a week apart describe one month, not a seventh of one."""
    flows = {
        datetime.date(2026, 3, 1): Decimal("300"),
        datetime.date(2026, 3, 8): Decimal("300"),
    }

    average, months, _ = average_monthly_contribution(flows)

    assert months == 1
    assert average == Decimal("600")


def test_nothing_to_average_says_so_rather_than_reporting_zero():
    """Zero is a measurement; None is the absence of one, and they differ."""
    average, months, total = average_monthly_contribution({})

    assert average is None
    assert (months, total) == (0, Decimal("0"))


def _flat_series(days: int, start_value: str, end_value: str):
    """A two-point series spanning *days*, which is all TWR needs here."""
    start = datetime.date(2024, 1, 1)
    return [
        (start, Decimal(start_value)),
        (start + datetime.timedelta(days=days), Decimal(end_value)),
    ]


def test_a_return_is_time_weighted_not_value_over_cost():
    """The whole reason this module exists, in one assertion.

    10 000 grows to 12 100 over two years with no flow: that is 21% total, and
    10% a year compounded. The cost-basis shortcut would divide by the money put
    in and land somewhere else entirely once contributions are involved.
    """
    basis = _category_basis(_flat_series(730, "10000", "12100"), [])

    assert basis.return_source == "annualised_twr"
    assert round(float(basis.annual_return_rate), 4) == 0.1
    assert basis.return_days == 730


def test_too_little_history_yields_no_rate_at_all():
    """Three months of gains annualise to a number nobody should compound."""
    basis = _category_basis(_flat_series(MIN_DAYS_FOR_A_RATE - 1, "10000", "10800"), [])

    assert basis.annual_return_rate is None
    assert basis.return_source == "unavailable"
    assert [w.code for w in basis.warnings] == ["insufficient_history"]


def test_a_short_but_usable_window_is_labelled_weak():
    basis = _category_basis(_flat_series(400, "10000", "11000"), [])

    assert basis.annual_return_rate is not None
    assert "weak_annualisation" in [w.code for w in basis.warnings]


def test_an_implausible_rate_is_flagged_and_not_rewritten():
    """Silently capping a bull run would be a second lie on top of the first."""
    basis = _category_basis(_flat_series(730, "10000", "40000"), [])

    assert basis.annual_return_rate > EXTREME_ANNUAL_RATE
    assert "extreme_rate" in [w.code for w in basis.warnings]


def test_money_paid_in_is_not_reported_as_money_earned():
    """The distinction the whole module turns on.

    A portfolio worth 10 000 receives a 2 000 deposit and ends at 12 000. It
    earned nothing: value over cost would call that +20%, time-weighting calls
    it 0%, and only one of the two is a return.
    """
    start = datetime.date(2024, 1, 1)
    deposit_day = start + datetime.timedelta(days=365)
    series = [
        (
            start + datetime.timedelta(days=offset),
            Decimal("10000") if offset < 365 else Decimal("12000"),
        )
        for offset in range(730)
    ]

    basis = _category_basis(series, [_Tx(deposit_day, "2000")])

    assert basis.contribution_source == "net_external_flows"
    assert basis.contribution_total == Decimal("2000")
    assert basis.annual_return_rate == Decimal("0")


def test_deposits_on_unpriced_days_disqualify_the_rate():
    """A flow the series cannot neutralise is read as performance, always upward.

    Two points four years apart with monthly deposits in between: time-weighting
    has nothing to divide by, and the naive chain reports a triple-digit return.
    Refusing is the only honest answer.
    """
    start = datetime.date(2024, 1, 1)
    sparse = [(start, Decimal("300")), (start + datetime.timedelta(days=1460), Decimal("20000"))]
    deposits = [
        _Tx(start + datetime.timedelta(days=30 * index), "300") for index in range(1, 48)
    ]

    basis = _category_basis(sparse, deposits)

    assert basis.annual_return_rate is None
    assert "unaligned_flows" in [w.code for w in basis.warnings]
    # The contribution is still measurable: it never needed the series.
    assert basis.monthly_contribution is not None


def test_a_stray_unpriced_deposit_does_not_disqualify_a_dense_series():
    """One weekend deposit against a priced portfolio is below the noise floor."""
    start = datetime.date(2024, 1, 1)
    dense = [
        (start + datetime.timedelta(days=offset), Decimal("10000") + Decimal(offset))
        for offset in range(0, 800)
    ]
    stray_day = start + datetime.timedelta(days=1000)  # outside the series entirely

    basis = _category_basis(dense, [_Tx(stray_day, "50")])

    assert basis.annual_return_rate is not None


def test_an_empty_category_carries_no_assumptions():
    basis = _category_basis([], [])

    assert basis == CategoryBasis()


def test_a_warning_carries_the_number_it_hinges_on():
    """"Too short" says nothing without the days it was short by."""
    from services.analytics.projection_basis import describe

    basis = _category_basis(_flat_series(200, "10000", "11000"), [])
    warning = basis.warnings[0]

    assert warning.code == "insufficient_history"
    assert warning.values == {"days": 200}
    # Rendered for a model, which needs the sentence rather than the code.
    assert "200 j" in describe(warning)
