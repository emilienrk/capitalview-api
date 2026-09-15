"""
The real cashflow (services/banking/real_cashflow.py): completed months only,
each operation counted by the nature the Opérations list shows.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import Session

from dtos.banking import CategoryNature, CategoryOrigin
from services.banking.categories import create_category, delete_category
from services.banking.flows import assign_category
from services.banking.real_cashflow import PeriodNotCompletedError, real_cashflow_month, real_cashflow_year
from tests.services.test_banking_category_filing import CURRENT, LIVRET, _month, _ops
from tests.services.test_banking_flows import USER, _raw, _store

TODAY = date(2026, 4, 10)  # March 2026 is the last completed month


def _year(session: Session, master_key: str, year: int = 2026, today: date = TODAY):
    return real_cashflow_year(session, USER, master_key, year, today=today)


def _figures(totals) -> dict[str, Decimal]:
    return {name: value for name, value in totals.model_dump().items() if value and name in {
        "income", "expenses", "saving", "investment", "internal", "neutralized",
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
    assert _figures(_year(session, master_key).totals) == {"neutralized": Decimal("59.45")}


def test_an_investment_category_is_invested_not_spent(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "500.00", "DBIT", "VIR SEPA COURTIER EN LIGNE"))
    placements = create_category(session, USER, master_key, "Placements", CategoryNature.INVESTMENT, CategoryOrigin.BANK)
    target = _month(session, master_key)["VIR SEPA COURTIER EN LIGNE"]
    assign_category(session, USER, master_key, target.id, placements.uuid, apply_to_similar=False)

    year = _year(session, master_key)

    assert _figures(year.totals) == {"investment": Decimal("500.00")}
    assert [(s.name, s.amount) for s in year.by_category.investment] == [("Placements", Decimal("500.00"))]


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


def test_a_deleted_category_reads_as_uncategorised(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "40.00", "DBIT", "CARTE 04/03/26 BOULANGERIE CB*08"))
    courses = create_category(session, USER, master_key, "Courses", CategoryNature.EXPENSE, CategoryOrigin.BANK)
    target = _month(session, master_key)["CARTE 04/03/26 BOULANGERIE CB*08"]
    assign_category(session, USER, master_key, target.id, courses.uuid, apply_to_similar=False)
    assert [s.name for s in _year(session, master_key).by_category.expenses] == ["Courses"]

    delete_category(session, USER, master_key, courses.uuid)

    shares = _year(session, master_key).by_category.expenses
    assert [(s.category_id, s.name, s.amount, s.count) for s in shares] == [(None, "Sans catégorie", Decimal("40.00"), 1)]


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
    assert [s.name for s in month.by_category.income] == ["Sans catégorie"]
    assert month.operation_count == 2


def test_nothing_stored_is_an_empty_year(session: Session, master_key: str):
    year = _year(session, master_key)
    assert (year.years_available, year.covered_months, [m.period for m in year.months]) == ([], 0, ["2026-01", "2026-02", "2026-03"])


def test_an_income_taken_back_lowers_the_income(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-01", "2000.00", "CRDT", "VIR SEPA EMPLOYEUR SALAIRE"),
        (CURRENT, "2026-03-15", "150.00", "DBIT", "VIR SEPA EMPLOYEUR TROP PERCU"),
    )
    salaire = create_category(session, USER, master_key, "Salaire", CategoryNature.INCOME, CategoryOrigin.BANK)
    month = _month(session, master_key)
    for label in ("VIR SEPA EMPLOYEUR SALAIRE", "VIR SEPA EMPLOYEUR TROP PERCU"):
        assign_category(session, USER, master_key, month[label].id, salaire.uuid, apply_to_similar=False)

    year = _year(session, master_key)

    assert _figures(year.totals) == {"income": Decimal("1850.00")}
    assert [(s.name, s.amount, s.count) for s in year.by_category.income] == [("Salaire", Decimal("1850.00"), 2)]


def test_each_category_has_its_own_share(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-05", "40.00", "DBIT", "CARTE 04/03/26 BOULANGERIE CB*08"),
        (CURRENT, "2026-03-06", "25.00", "DBIT", "CARTE 05/03/26 LIBRAIRIE CB*08"),
        (CURRENT, "2026-03-07", "60.00", "DBIT", "CARTE 06/03/26 CARREFOUR CB*08"),
    )
    courses = create_category(session, USER, master_key, "Courses", CategoryNature.EXPENSE, CategoryOrigin.BANK)
    loisirs = create_category(session, USER, master_key, "Loisirs", CategoryNature.EXPENSE, CategoryOrigin.BANK)
    month = _month(session, master_key)
    assign_category(session, USER, master_key, month["CARTE 04/03/26 BOULANGERIE CB*08"].id, courses.uuid, False)
    assign_category(session, USER, master_key, month["CARTE 06/03/26 CARREFOUR CB*08"].id, courses.uuid, False)
    assign_category(session, USER, master_key, month["CARTE 05/03/26 LIBRAIRIE CB*08"].id, loisirs.uuid, False)

    shares = _year(session, master_key).by_category.expenses

    assert [(s.category_id, s.name, s.amount) for s in shares] == [
        (courses.uuid, "Courses", Decimal("100.00")), (loisirs.uuid, "Loisirs", Decimal("25.00")),
    ]
