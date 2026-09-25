"""A placement's value and return, derived from the entries written on it
(services/placement.py), and what its deposits prove on the bank side."""

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import Session

from models.enums import PlacementEntryType as Kind
from models.placement import PlacementAccount, PlacementEntry
from services.banking.contributions import load_contributions
from services.encryption import encrypt_data, hash_index
from services.placement import EntryPoint, PlacementTimeline

USER = "user-placement"


def _timeline(*entries: tuple[str, Kind, str]) -> PlacementTimeline:
    return PlacementTimeline(
        [EntryPoint(date.fromisoformat(d), kind, Decimal(amount)) for d, kind, amount in entries]
    )


# ---------------------------------------------------------------------------
# Value
# ---------------------------------------------------------------------------


def test_without_a_statement_the_value_is_what_was_paid_in():
    timeline = _timeline(
        ("2024-01-10", Kind.DEPOSIT, "1000"),
        ("2024-03-01", Kind.DEPOSIT, "500"),
        ("2024-06-01", Kind.WITHDRAW, "200"),
    )

    assert timeline.value_on(date(2024, 1, 9)) == 0
    assert timeline.value_on(date(2024, 2, 1)) == 1000
    assert timeline.value_on(date(2024, 12, 31)) == 1300


def test_the_gain_a_statement_reveals_accrues_linearly_since_the_previous_one():
    # The placement is worth nothing on 2023-12-31, the day before its first entry.
    timeline = _timeline(
        ("2024-01-01", Kind.DEPOSIT, "1000"),
        ("2024-12-31", Kind.VALUATION, "1100"),
    )

    assert timeline.value_on(date(2024, 12, 31)) == 1100
    halfway = timeline.value_on(date(2024, 7, 1))  # 183 of 366 days
    assert halfway == Decimal("1000") + Decimal("100") * 183 / 366


def test_after_the_last_statement_only_the_flows_move_the_value():
    timeline = _timeline(
        ("2024-01-01", Kind.DEPOSIT, "1000"),
        ("2024-12-31", Kind.VALUATION, "1100"),
        ("2025-02-01", Kind.DEPOSIT, "500"),
        ("2025-03-01", Kind.WITHDRAW, "100"),
    )

    assert timeline.value_on(date(2025, 1, 31)) == 1100
    assert timeline.value_on(date(2025, 2, 1)) == 1600
    assert timeline.value_on(date(2025, 9, 1)) == 1500


def test_a_statement_already_holds_the_deposit_of_its_own_day():
    timeline = _timeline(
        ("2024-01-01", Kind.DEPOSIT, "1000"),
        ("2024-06-01", Kind.DEPOSIT, "500"),
        ("2024-06-01", Kind.VALUATION, "1520"),
    )

    assert timeline.value_on(date(2024, 6, 1)) == 1520
    assert timeline.value_on(date(2024, 7, 1)) == 1520


def test_deposits_and_withdrawals_are_counted_up_to_the_day():
    timeline = _timeline(
        ("2024-01-01", Kind.DEPOSIT, "1000"),
        ("2024-05-01", Kind.WITHDRAW, "300"),
        ("2024-06-01", Kind.VALUATION, "750"),
    )

    assert timeline.deposits_until(date(2024, 4, 30)) == 1000
    assert timeline.withdrawals_until(date(2024, 4, 30)) == 0
    assert timeline.withdrawals_until(date(2024, 5, 1)) == 300
    assert timeline.flow_on(date(2024, 5, 1)) == -300
    assert timeline.flow_on(date(2024, 6, 1)) == 0


# ---------------------------------------------------------------------------
# Return
# ---------------------------------------------------------------------------


def test_under_a_year_of_statements_no_rate_is_derived():
    timeline = _timeline(
        ("2024-01-01", Kind.DEPOSIT, "1000"),
        ("2024-09-01", Kind.VALUATION, "1030"),
    )

    rate, days = timeline.annual_return()

    assert rate is None
    assert days == 245


def test_a_year_at_three_percent_reads_as_three_percent():
    timeline = _timeline(
        ("2024-01-01", Kind.DEPOSIT, "1000"),
        ("2024-12-31", Kind.VALUATION, "1030"),
    )

    rate, _ = timeline.annual_return()

    assert rate == pytest.approx(Decimal("0.03"), abs=Decimal("0.001"))


def test_a_large_deposit_does_not_read_as_performance():
    # Two years at 3% each, with five times the capital arriving for the second.
    timeline = _timeline(
        ("2023-01-01", Kind.DEPOSIT, "1000"),
        ("2023-12-31", Kind.VALUATION, "1030"),
        ("2024-01-01", Kind.DEPOSIT, "5000"),
        ("2024-12-31", Kind.VALUATION, str(Decimal("6030") * Decimal("1.03"))),
    )

    rate, days = timeline.annual_return()

    assert days == 731
    assert rate == pytest.approx(Decimal("0.03"), abs=Decimal("0.002"))


def test_no_entry_at_all_is_worth_nothing_and_yields_no_rate():
    timeline = PlacementTimeline([])

    assert timeline.start is None
    assert timeline.value_on(date(2024, 1, 1)) == 0
    assert timeline.annual_return() == (None, 0)


# ---------------------------------------------------------------------------
# Bank side
# ---------------------------------------------------------------------------


def _entry(placement: str, kind: Kind, day: str, amount: str, master_key: str) -> PlacementEntry:
    return PlacementEntry(
        account_uuid=placement,
        type_enc=encrypt_data(kind.value, master_key),
        amount_enc=encrypt_data(amount, master_key),
        occurred_at_enc=encrypt_data(day, master_key),
    )


def test_a_deposit_on_a_placement_proves_the_transfer_but_a_statement_does_not(
    session: Session, master_key: str
):
    session.add(
        PlacementAccount(
            uuid="placement",
            user_uuid_bidx=hash_index(USER, master_key),
            name_enc=encrypt_data("Linxea Spirit", master_key),
            placement_type_enc=encrypt_data("AV", master_key),
        )
    )
    session.commit()
    session.add(_entry("placement", Kind.DEPOSIT, "2025-03-05", "200", master_key))
    session.add(_entry("placement", Kind.WITHDRAW, "2025-04-10", "150.5", master_key))
    session.add(_entry("placement", Kind.VALUATION, "2025-03-05", "5000", master_key))
    session.commit()

    contributions = load_contributions(session, USER, master_key)

    deposit = contributions.by_amount[(True, Decimal("200.00"))]
    assert [(c.account_name, c.day) for c in deposit] == [("Linxea Spirit", date(2025, 3, 5))]
    assert (False, Decimal("150.50")) in contributions.by_amount
    assert (True, Decimal("5000.00")) not in contributions.by_amount


def test_a_single_lump_sum_is_not_projected_as_a_monthly_contribution(session: Session, master_key: str):
    from datetime import timedelta

    from services.analytics.projection_basis import derive_projection_defaults

    session.add(
        PlacementAccount(
            uuid="lump",
            user_uuid_bidx=hash_index(USER, master_key),
            name_enc=encrypt_data("Spirit", master_key),
            placement_type_enc=encrypt_data("AV", master_key),
        )
    )
    session.commit()
    two_years_ago = (date.today() - timedelta(days=730)).isoformat()
    session.add(_entry("lump", Kind.DEPOSIT, two_years_ago, "10000", master_key))
    session.commit()

    basis = derive_projection_defaults(session, USER, master_key)["PLACEMENT"]

    assert basis.monthly_contribution == pytest.approx(Decimal("10000") / 24, rel=Decimal("0.02"))
    assert [w.code for w in basis.warnings] == ["no_statement"]
