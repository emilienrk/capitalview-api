"""
The real cashflow (services/banking/real_cashflow.py): completed months only,
each operation counted by the type the Opérations list shows.
"""
from datetime import date
from decimal import Decimal
from statistics import median

import pytest
from sqlmodel import Session

from dtos.banking import CashflowType, TypeScope
from models.bank import BankAccount
from services.banking.flows import list_month_transactions, set_transaction_type
from services.banking.real_cashflow import (
    PeriodNotCompletedError, _per_month, _Tally, _totals, real_cashflow_month, real_cashflow_year,
)
from services.encryption import encrypt_data
from tests.services.test_banking_flows import USER, _link, _raw, _store
from tests.services.test_banking_transfer_patterns import _top_up

CURRENT, LIVRET, LDDS = "current", "savings", "ldds"  # "savings" is a Livret A

TODAY = date(2026, 4, 10)  # March 2026 is the last completed month


def _ops(session: Session, master_key: str, *operations: tuple[str, str, str, str, str]) -> None:
    """(account, day, amount, direction, label)"""
    for account in sorted({op[0] for op in operations}):
        if session.get(BankAccount, account) is None:
            _link(session, master_key, account)
    for n, (account, day, amount, direction, label) in enumerate(operations):
        _store(session, master_key, account, _raw(amount, direction, day, ref=f"{account}-{day}-{n}", label=label))


def _as_ldds(session: Session, master_key: str) -> None:
    account = session.get(BankAccount, LDDS)
    account.account_type_enc = encrypt_data("LDD", master_key)
    session.add(account)
    session.commit()


def _month(session: Session, master_key: str, period: str = "2026-03"):
    return {tx.label: tx for tx in list_month_transactions(session, USER, master_key, period).transactions}


def _year(session: Session, master_key: str, year: int = 2026, today: date = TODAY):
    return real_cashflow_year(session, USER, master_key, year, today=today)


def _figures(totals) -> dict[str, Decimal]:
    return {name: value for name, value in totals.model_dump().items() if value and name in {
        "income", "expenses", "saving", "investment", "neutral",
    }}


def test_the_current_month_is_left_out(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-05", "40.00", "DBIT", "CARTE 04/03/26 BOULANGERIE CB*08"),
        (CURRENT, "2026-04-02", "900.00", "DBIT", "CARTE 01/04/26 BIJOUTERIE CB*08"),
    )
    year = _year(session, master_key)
    assert [m.period for m in year.months] == ["2026-01", "2026-02", "2026-03"]
    assert _figures(year.totals) == {"expenses": Decimal("40.00")}
    with pytest.raises(PeriodNotCompletedError):
        real_cashflow_month(session, USER, master_key, "2026-04", today=TODAY)


def test_a_median_month_one_off_is_a_month_s_not_a_difference_of_medians():
    months = []
    for expenses, recurring in (("100", "0"), ("100", "100"), ("300", "50")):
        tally = _Tally()
        tally.totals["expenses"], tally.totals["recurring"] = Decimal(expenses), Decimal(recurring)
        months.append(_totals(tally))

    monthly = _per_month(months, median)

    # 100 - 50 would describe no month at all; the one-off months are 100, 0 and 250.
    assert (monthly.expenses, monthly.recurring, monthly.one_off) == (Decimal("100"), Decimal("50"), Decimal("100"))


def test_a_transfer_to_a_livret_is_set_aside_not_spent(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant"),
        (LIVRET, "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        (LIVRET, "2026-03-20", "50.00", "DBIT", "VIR Virement depuis Compte epargne"),
        (CURRENT, "2026-03-20", "50.00", "CRDT", "VIR Virement depuis Compte epargne"),
    )
    assert _figures(_year(session, master_key).totals) == {"saving": Decimal("250.00")}


def test_a_refund_is_neutralised(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-02", "59.45", "DBIT", "CARTE 01/03/26 ZALANDO PAYMENTS CB*08"),
        (CURRENT, "2026-03-12", "59.45", "CRDT", "AVOIR 11/03/26 ZALANDO PAYMENTS CB*08"),
    )
    assert _figures(_year(session, master_key).totals) == {"neutral": Decimal("59.45")}


def test_the_monthly_median_and_mean_are_over_the_months_with_data(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-01-05", "100.00", "DBIT", "CARTE 04/01/26 BOULANGERIE CB*08"),
        (CURRENT, "2026-02-05", "100.00", "DBIT", "CARTE 04/02/26 BOULANGERIE CB*08"),
        (CURRENT, "2026-03-05", "400.00", "DBIT", "CARTE 04/03/26 BOULANGERIE CB*08"),
    )
    year = _year(session, master_key, today=date(2026, 7, 1))

    assert len(year.months) == 6
    assert year.covered_months == 3
    assert (year.monthly_mean.expenses, year.monthly_median.expenses) == (Decimal("200"), Decimal("100.00"))


def test_a_foreign_currency_is_reported_apart(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "40.00", "DBIT", "CARTE 04/03/26 BOULANGERIE CB*08"))
    _store(session, master_key, CURRENT, _raw("12.63", "DBIT", "2026-03-06", ref="chf", currency="CHF", label="DENNER GENEVE"))

    year = _year(session, master_key)

    assert (year.currency, _figures(year.totals)) == ("EUR", {"expenses": Decimal("40.00")})
    assert [(c.currency, c.outflow) for c in year.other_currencies] == [("CHF", Decimal("12.63"))]


def test_a_pending_operation_is_left_out(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "40.00", "DBIT", "CARTE 04/03/26 BOULANGERIE CB*08"))
    _store(session, master_key, CURRENT, _raw("99.00", "DBIT", "2026-03-06", ref="pdng", status="PDNG", label="CARTE LIBRAIRIE"))
    assert _figures(_year(session, master_key).totals) == {"expenses": Decimal("40.00")}


def test_the_five_largest_expenses_are_shown_and_still_counted(session: Session, master_key: str):
    amounts = ["10.00", "900.00", "35.00", "220.00", "18.00", "75.00"]
    _ops(session, master_key, *[
        (CURRENT, f"2026-03-{n + 1:02d}", amount, "DBIT", f"CARTE MARCHAND{chr(65 + n)} CB*08")
        for n, amount in enumerate(amounts)
    ])
    _ops(
        session, master_key,
        (CURRENT, "2026-03-20", "2000.00", "CRDT", "VIR SEPA EMPLOYEUR"),
        (CURRENT, "2026-03-21", "3000.00", "DBIT", "VIR Virement depuis Compte courant"),
        (LIVRET, "2026-03-21", "3000.00", "CRDT", "VIR Virement depuis Compte courant"),
    )

    year = _year(session, master_key)

    assert [e.amount for e in year.top_expenses] == [Decimal(a) for a in ("900.00", "220.00", "75.00", "35.00", "18.00")]
    assert year.totals.expenses == Decimal("1258.00")
    assert year.totals.income == Decimal("2000.00")


def test_years_run_from_the_first_operation_to_today(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2024-11-05", "40.00", "DBIT", "CARTE BOULANGERIE CB*08"),
        (CURRENT, "2026-02-05", "40.00", "DBIT", "CARTE BOULANGERIE CB*08"),
    )
    assert _year(session, master_key).years_available == [2024, 2025, 2026]


def test_a_month_offers_its_neighbours_among_completed_months_with_data(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2025-12-05", "40.00", "DBIT", "CARTE BOULANGERIE CB*08"),
        (CURRENT, "2026-02-05", "70.00", "DBIT", "CARTE LIBRAIRIE CB*08"),
        (CURRENT, "2026-02-06", "1500.00", "CRDT", "VIR SEPA EMPLOYEUR"),
        (CURRENT, "2026-04-05", "80.00", "DBIT", "CARTE PHARMACIE CB*08"),
    )

    month = real_cashflow_month(session, USER, master_key, "2026-02", today=TODAY)

    assert (month.previous_period, month.next_period) == ("2025-12", None)
    assert _figures(month.totals) == {"expenses": Decimal("70.00"), "income": Decimal("1500.00")}
    assert month.operation_count == 2


def test_nothing_stored_is_an_empty_year(session: Session, master_key: str):
    year = _year(session, master_key)
    assert (year.years_available, year.covered_months, [m.period for m in year.months]) == ([], 0, ["2026-01", "2026-02", "2026-03"])


def _answer(session: Session, master_key: str, label: str, kind, period: str = "2026-03") -> None:
    [target] = [tx for tx in list_month_transactions(session, USER, master_key, period).transactions if tx.label == label][-1:]
    set_transaction_type(session, USER, master_key, target.id, kind, TypeScope.LABEL)


def test_net_saving_adds_a_livret_of_the_app_and_an_answer_for_an_account_outside_it(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant"),
        (LIVRET, "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        (LIVRET, "2026-03-20", "50.00", "DBIT", "VIR Virement depuis Compte epargne"),
        (CURRENT, "2026-03-20", "50.00", "CRDT", "VIR Virement depuis Compte epargne"),
        (CURRENT, "2026-03-25", "200.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
    )
    assert _figures(_year(session, master_key).totals) == {"saving": Decimal("250.00"), "expenses": Decimal("200.00")}

    _answer(session, master_key, "VIR INST ROUKINE EMILIEN", CashflowType.SAVING)

    assert _figures(_year(session, master_key).totals) == {"saving": Decimal("450.00")}


def test_a_transfer_from_a_livret_to_an_ldds_is_not_saving(session: Session, master_key: str):
    _link(session, master_key, LDDS)
    _as_ldds(session, master_key)
    _ops(
        session, master_key,
        (LIVRET, "2026-03-05", "300.00", "DBIT", "VIR Virement interne depuis LIVRET A"),
        (LDDS, "2026-03-05", "300.00", "CRDT", "VIR Virement interne depuis LIVRET A"),
    )
    assert _figures(_year(session, master_key).totals) == {"neutral": Decimal("300.00")}


def test_livret_interest_is_income(session: Session, master_key: str):
    _ops(session, master_key, (LIVRET, "2026-03-31", "64.00", "CRDT", "*INTER.BRUTS 2025"))
    assert _figures(_year(session, master_key).totals) == {"income": Decimal("64.00")}


def test_a_refund_lowers_the_expenses_and_the_net_follows(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-01", "2000.00", "CRDT", "VIR SEPA EMPLOYEUR SALAIRE"),
        (CURRENT, "2026-03-02", "120.00", "DBIT", "CARTE 01/03/26 RESTAURANT DU LAC CB*08"),
        (CURRENT, "2026-03-04", "40.00", "CRDT", "Virement de : TITOUAN TIERS"),
        (CURRENT, "2026-03-06", "500.00", "DBIT", "VIR SEPA COURTIER EN LIGNE"),
    )
    _answer(session, master_key, "Virement de : TITOUAN TIERS", CashflowType.EXPENSE)
    _answer(session, master_key, "VIR SEPA COURTIER EN LIGNE", CashflowType.INVESTMENT)

    totals = _year(session, master_key).totals

    assert _figures(totals) == {"income": Decimal("2000.00"), "expenses": Decimal("80.00"), "investment": Decimal("500.00")}
    assert totals.net == Decimal("1420.00")


def test_a_recurring_top_up_between_current_accounts_is_left_out(session: Session, master_key: str):
    _ops(session, master_key, *_top_up("01", "05", "20.00"), *_top_up("02", "10", "35.50"), *_top_up("03", "14", "12.00"))
    year = real_cashflow_year(session, USER, master_key, 2025, today=TODAY)
    assert _figures(year.totals) == {"neutral": Decimal("67.50")}


def test_a_suggested_pair_counts_by_default_and_is_flagged(session: Session, master_key: str):
    _ops(
        session, master_key,
        ("neobank", "2026-03-16", "50.00", "DBIT", "out"),
        (CURRENT, "2026-03-17", "50.00", "CRDT", "in"),
    )
    year = _year(session, master_key)
    assert _figures(year.totals) == {"income": Decimal("50.00"), "expenses": Decimal("50.00")}
    assert (year.open_questions, year.months[2].open_questions) == (1, 1)


def test_a_question_asked_in_a_later_year_is_open_on_the_earlier_one(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2025-12-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (CURRENT, "2026-03-05", "90.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
    )
    earlier = real_cashflow_year(session, USER, master_key, 2025, today=TODAY)
    assert (earlier.open_questions, earlier.months[11].open_questions) == (1, 1)
    assert real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY).open_questions == 1
