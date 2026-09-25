"""A savings account's interest over the year, from its balances and its rates
(services/savings_interest.py)."""

import calendar
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session

from models.bank import BankAccount
from models.enums import InterestMethod
from services.encryption import encrypt_data, hash_index
from services.savings_interest import (
    InterestTerms,
    daily_interest,
    fortnightly_interest,
    get_user_savings_interest,
    quinzaines,
    year_balances,
)

USER = "user-savings"
RATE = InterestTerms(rate=Decimal("0.024"))


def _balances(year: int, *moves: tuple[str, str], opening: str = "0") -> dict[date, Decimal]:
    """End-of-day balances over the year, from an opening balance and dated moves."""
    by_day = {date.fromisoformat(d): Decimal(a) for d, a in moves}
    balance = Decimal(opening)
    day = date(year - 1, 12, 31)
    result = {}
    while day <= date(year, 12, 31):
        balance += by_day.get(day, Decimal("0"))
        result[day] = balance
        day += timedelta(days=1)
    return result


def _total(periods) -> Decimal:
    return sum((a for _, a in periods), Decimal("0"))


def test_a_year_has_24_quinzaines_covering_every_day():
    spans = quinzaines(2024)

    assert len(spans) == 24
    assert spans[0] == (date(2024, 1, 1), date(2024, 1, 15))
    assert spans[3] == (date(2024, 2, 16), date(2024, 2, 29))
    assert sum((end - start).days + 1 for start, end in spans) == 366


def test_a_balance_held_all_year_earns_the_full_rate():
    balances = _balances(2025, opening="10000")

    assert _total(fortnightly_interest(balances, 2025, RATE)) == pytest.approx(Decimal("240"))


def test_a_deposit_on_10_december_earns_a_single_quinzaine():
    balances = _balances(2025, ("2025-12-10", "20000"))

    total = _total(fortnightly_interest(balances, 2025, InterestTerms(rate=Decimal("0.025"))))

    assert total == pytest.approx(Decimal("20000") * Decimal("0.025") / 24)  # 20.83 €, not 500 €


def test_a_deposit_on_the_first_waits_for_the_sixteenth_and_one_on_the_fifteenth_does_not():
    on_the_1st = _balances(2025, ("2025-03-01", "2400"))
    on_the_15th = _balances(2025, ("2025-03-15", "2400"))

    # Both earn from 16 March: 19 quinzaines of 2400 × 2.4 % / 24.
    expected = Decimal("2400") * Decimal("0.024") / 24 * 19
    assert _total(fortnightly_interest(on_the_1st, 2025, RATE)) == pytest.approx(expected)
    assert _total(fortnightly_interest(on_the_15th, 2025, RATE)) == pytest.approx(expected)


def test_a_withdrawal_stops_earning_from_the_start_of_its_quinzaine():
    balances = _balances(2025, ("2025-06-20", "-1000"), opening="3000")

    periods = dict(fortnightly_interest(balances, 2025, RATE))

    assert periods[date(2025, 6, 15)] == pytest.approx(Decimal("3000") * Decimal("0.024") / 24)
    assert periods[date(2025, 6, 30)] == pytest.approx(Decimal("2000") * Decimal("0.024") / 24)


def test_a_boosted_rate_applies_until_its_date_then_the_base_rate():
    terms = InterestTerms(rate=Decimal("0.015"), boosted_rate=Decimal("0.045"), boosted_until=date(2025, 3, 31))
    balances = _balances(2025, opening="24000")

    total = _total(fortnightly_interest(balances, 2025, terms))

    assert total == pytest.approx(Decimal("24000") * (Decimal("0.045") * 6 + Decimal("0.015") * 18) / 24)


def test_daily_interest_counts_every_day_a_deposit_stays():
    balances = _balances(2025, ("2025-12-10", "36500"))

    total = _total(daily_interest(balances, 2025, InterestTerms(rate=Decimal("0.02"))))

    assert total == pytest.approx(Decimal("36500") * Decimal("0.02") * 22 / 365)


def test_year_balances_carry_the_last_snapshot_and_hold_today_s_balance():
    known = {date(2025, 3, 10): Decimal("500"), date(2025, 3, 12): Decimal("800")}

    balances, tracked_from = year_balances(known, Decimal("900"), 2025, date(2025, 3, 20))

    # The first balance reaches back to its quinzaine's start: tracking, not a deposit.
    assert balances[date(2025, 2, 27)] == 0
    assert balances[date(2025, 2, 28)] == 500
    assert balances[date(2025, 3, 11)] == 500
    assert balances[date(2025, 3, 19)] == 800
    assert balances[date(2025, 3, 20)] == 900
    assert balances[date(2025, 12, 31)] == 900
    assert tracked_from == date(2025, 3, 1)


def test_a_history_covering_the_whole_year_is_tracked_from_its_start():
    _, tracked_from = year_balances({date(2024, 12, 31): Decimal("10")}, Decimal("10"), 2025, date(2025, 6, 1))

    assert tracked_from is None


def _account(session: Session, master_key: str, account_type: str, rate: str | None, **extra) -> BankAccount:
    account = BankAccount(
        user_uuid_bidx=hash_index(USER, master_key),
        name_enc=encrypt_data("Livret", master_key),
        balance_enc=encrypt_data("12000", master_key),
        account_type_enc=encrypt_data(account_type, master_key),
        interest_rate_enc=encrypt_data(rate, master_key) if rate else None,
        **extra,
    )
    session.add(account)
    session.commit()
    return account


def test_only_accounts_with_a_rate_get_an_estimate(session: Session, master_key: str):
    livret = _account(session, master_key, "LIVRET_A", "0.024")
    _account(session, master_key, "LDD", None)
    _account(session, master_key, "CHECKING", None)

    today = date(date.today().year, 7, 1)
    result = get_user_savings_interest(session, USER, master_key, today=today)

    assert [r.account_id for r in result] == [livret.uuid]
    estimate = result[0]
    assert estimate.method == InterestMethod.FORTNIGHTLY
    # No history: the account counts from today, at today's 12 000 €, for 12 quinzaines.
    assert estimate.tracked_from == today
    assert estimate.earned == 0
    assert estimate.estimated == pytest.approx(Decimal("12000") * Decimal("0.024") / 24 * 12, abs=Decimal("0.01"))


def test_a_savings_account_can_count_by_the_day(session: Session, master_key: str):
    _account(
        session, master_key, "SAVINGS", "0.02",
        interest_method_enc=encrypt_data("DAILY", master_key),
    )

    result = get_user_savings_interest(session, USER, master_key, today=date(date.today().year, 12, 31))

    assert result[0].method == InterestMethod.DAILY
    assert result[0].estimated == pytest.approx(
        Decimal("12000") * Decimal("0.02") / (366 if calendar.isleap(date.today().year) else 365),
        abs=Decimal("0.01"),
    )
