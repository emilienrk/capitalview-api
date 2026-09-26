"""
What the real cashflow reads beyond its totals (services/banking/real_cashflow.py):
what open questions weigh, the rates, the previous year, the projection, the
unusual months, the safety net, the accounts' coverage, where money came from
and went, and the pace of the month in progress.
"""
from datetime import date
from decimal import Decimal

from sqlmodel import Session, select

from dtos.banking import CashflowType, TypeScope
from models.bank import BankAccount
from models.banking import BankAccountLink
from services.banking.flows import list_month_transactions, set_transaction_type
from services.bank import confirm_up_to_date
from services.banking.real_cashflow import real_cashflow_current, real_cashflow_month, real_cashflow_year
from services.encryption import encrypt_data, hash_index
from tests.services.test_banking_flows import USER, _raw, _store
from tests.services.test_banking_real_cashflow import CURRENT, LIVRET, TODAY, _ops

NEOBANK = "neobank"


def _year(session: Session, master_key: str, year: int = 2026, today: date = TODAY):
    return real_cashflow_year(session, USER, master_key, year, today=today)


def _set_account(session: Session, master_key: str, account: str, *, name: str | None = None,
                 balance: str | None = None, synced: date | None = None) -> None:
    row = session.get(BankAccount, account)
    if name is not None:
        row.name_enc = encrypt_data(name, master_key)
    if balance is not None:
        row.balance_enc = encrypt_data(balance, master_key)
    session.add(row)
    if synced is not None:
        link = session.exec(
            select(BankAccountLink).where(BankAccountLink.bank_account_uuid_bidx == hash_index(account, master_key))
        ).one()
        link.last_synced_at = synced
        session.add(link)
    session.commit()


def _unlink(session: Session, master_key: str, account: str) -> None:
    link = session.exec(
        select(BankAccountLink).where(BankAccountLink.bank_account_uuid_bidx == hash_index(account, master_key))
    ).one()
    session.delete(link)
    session.commit()


class TestOpenAmount:
    def test_a_month_weighs_its_flow_questions_and_its_suggested_pairs(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
            (CURRENT, "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
            (NEOBANK, "2026-03-16", "50.00", "DBIT", "To Emilien Roukine"),
            (CURRENT, "2026-03-17", "50.00", "CRDT", "VIR Virement de Emilien ROUKINE"),
            (CURRENT, "2026-03-20", "40.00", "CRDT", "VIR SEPA VINTED"),
        )
        year = _year(session, master_key)

        assert [m.open_amount for m in year.months] == [Decimal("0"), Decimal("400.00"), Decimal("450.00")]
        assert year.open_amount == Decimal("850.00")
        assert real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY).open_amount == Decimal("450.00")

    def test_an_answer_leaves_nothing_open(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"))
        [carrier] = list_month_transactions(session, USER, master_key, "2026-03").transactions

        set_transaction_type(session, USER, master_key, carrier.id, CashflowType.SAVING, TypeScope.LABEL)

        assert _year(session, master_key).open_amount == Decimal("0")


class TestRates:
    def test_the_savings_rate_counts_what_was_not_spent_and_the_placed_rate_what_was_set_aside(
        self, session: Session, master_key: str,
    ):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-01", "2000.00", "CRDT", "VIR SEPA EMPLOYEUR"),
            (CURRENT, "2026-03-02", "1500.00", "DBIT", "CARTE 01/03/26 MAGASIN CB*08"),
            (CURRENT, "2026-03-05", "200.00", "DBIT", "VIR Virement depuis Compte courant"),
            (LIVRET, "2026-03-05", "200.00", "CRDT", "VIR Virement depuis Compte courant"),
            (CURRENT, "2026-03-06", "100.00", "DBIT", "VIR SEPA COURTIER EN LIGNE"),
        )
        [broker] = [
            tx for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions
            if tx.label == "VIR SEPA COURTIER EN LIGNE"
        ]
        set_transaction_type(session, USER, master_key, broker.id, CashflowType.INVESTMENT, TypeScope.LABEL)

        totals = _year(session, master_key).totals

        assert (totals.savings_rate, totals.placed_rate) == (Decimal("25.0"), Decimal("15.0"))

    def test_no_income_has_no_rate(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-02", "15.00", "DBIT", "CARTE 01/03/26 MAGASIN CB*08"))
        totals = _year(session, master_key).totals
        assert (totals.savings_rate, totals.placed_rate) == (None, None)

    def test_income_taken_back_has_no_rate(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-02", "15.00", "DBIT", "CARTE 01/03/26 MAGASIN CB*08"))
        [payment] = list_month_transactions(session, USER, master_key, "2026-03").transactions
        set_transaction_type(session, USER, master_key, payment.id, CashflowType.INCOME, TypeScope.OPERATION)

        totals = _year(session, master_key).totals

        assert (totals.income, totals.savings_rate) == (Decimal("-15.00"), None)

    def test_a_median_month_s_rate_is_that_of_its_median_figures(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-01-05", "1000.00", "CRDT", "VIR SEPA EMPLOYEUR"),
            (CURRENT, "2026-01-06", "900.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-02-05", "2000.00", "CRDT", "VIR SEPA EMPLOYEUR"),
            (CURRENT, "2026-02-06", "100.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-03-05", "3000.00", "CRDT", "VIR SEPA EMPLOYEUR"),
            (CURRENT, "2026-03-06", "500.00", "DBIT", "CARTE MAGASIN CB*08"),
        )
        # Medians 2000 in, 500 out: 75 %, where the monthly rates' median is 83.3 %.
        assert _year(session, master_key).monthly_median.savings_rate == Decimal("75.0")


class TestAgainstTheYearBefore:
    def test_the_previous_year_is_read_over_the_same_months(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-03-05", "100.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2025-04-05", "900.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-03-05", "40.00", "DBIT", "CARTE MAGASIN CB*08"),
        )
        assert _year(session, master_key).previous_year_to_date.expenses == Decimal("100.00")
        assert _year(session, master_key, 2025).previous_year_to_date is None

    def test_a_past_year_is_compared_with_the_whole_year_before(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2024-03-05", "100.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2024-12-05", "900.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2025-03-05", "40.00", "DBIT", "CARTE MAGASIN CB*08"),
        )
        assert _year(session, master_key, 2025).previous_year_to_date.expenses == Decimal("1000.00")

    def test_the_current_year_is_projected_with_its_median_month(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-06-05", "100.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-01-05", "100.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-02-05", "300.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-03-05", "200.00", "DBIT", "CARTE MAGASIN CB*08"),
        )
        # 600 over three months, then the 200 median for each of the nine left.
        assert _year(session, master_key).projection.expenses == Decimal("2400.00")
        assert _year(session, master_key, 2025).projection is None


class TestUnusualMonths:
    def _months(self, session: Session, master_key: str, amounts: list[str]) -> None:
        _ops(session, master_key, *[
            (CURRENT, f"2025-{n + 1:02d}-05", amount, "DBIT", "CARTE MAGASIN CB*08") for n, amount in enumerate(amounts)
        ])

    def test_a_month_spending_far_above_the_others_is_unusual(self, session: Session, master_key: str):
        self._months(session, master_key, ["1000.00", "1100.00", "900.00", "1050.00", "950.00", "4000.00"])
        year = _year(session, master_key, 2025)
        assert [m.period for m in year.months if m.atypical] == ["2025-06"]

    def test_fewer_than_six_months_call_nothing_unusual(self, session: Session, master_key: str):
        self._months(session, master_key, ["1000.00", "1100.00", "900.00", "1050.00", "4000.00"])
        assert not any(m.atypical for m in _year(session, master_key, 2025).months)


class TestSafetyNet:
    def test_the_money_at_hand_is_read_in_months_of_median_spending(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-01-05", "1000.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-02-05", "3000.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-03-05", "2000.00", "DBIT", "CARTE MAGASIN CB*08"),
            (LIVRET, "2026-03-31", "1.00", "CRDT", "*INTER.BRUTS 2025"),
            (NEOBANK, "2026-03-31", "1.00", "CRDT", "Cashback"),
        )
        _set_account(session, master_key, CURRENT, name="Courant", balance="1000.00", synced=date(2026, 4, 3))
        _set_account(session, master_key, NEOBANK, name="Néobanque", balance="0", synced=date(2026, 4, 2))
        _set_account(session, master_key, LIVRET, name="Livret A", balance="4000.00")
        _unlink(session, master_key, LIVRET)

        net = _year(session, master_key).safety_net

        assert (net.available, net.savings, net.monthly_expenses) == (Decimal("5000.00"), Decimal("4000.00"), Decimal("2000.00"))
        assert (net.months, net.savings_months) == (Decimal("2.5"), Decimal("2.0"))
        # A week without a sync, and never synced at all.
        assert net.stale_accounts == ["Livret A", "Néobanque"]

    def test_a_balance_the_user_vouched_for_this_week_is_not_stale(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "2000.00", "DBIT", "CARTE MAGASIN CB*08"),
            (LIVRET, "2026-01-31", "1.00", "CRDT", "*INTER.BRUTS 2025"),
        )
        _set_account(session, master_key, CURRENT, synced=TODAY)
        _set_account(session, master_key, LIVRET, name="Livret A")
        _unlink(session, master_key, LIVRET)
        confirm_up_to_date(session, session.get(BankAccount, LIVRET), today=TODAY)

        assert _year(session, master_key).safety_net.stale_accounts == []

    def test_only_the_current_year_has_one(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2025-03-05", "100.00", "DBIT", "CARTE MAGASIN CB*08"))
        assert _year(session, master_key, 2025).safety_net is None


class TestCoverage:
    def test_an_account_starting_after_the_period_began_is_reported(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-12-30", "10.00", "DBIT", "CARTE MAGASIN CB*08"),
            (LIVRET, "2026-02-10", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        _set_account(session, master_key, CURRENT, synced=TODAY)
        _set_account(session, master_key, LIVRET, name="Livret A", synced=TODAY)

        [gap] = _year(session, master_key).coverage_gaps

        assert (gap.account_name, gap.first_day, gap.starts_late, gap.ends_early) == (
            "Livret A", date(2026, 2, 10), True, False,
        )
        assert real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY).coverage_gaps == []

    def test_an_account_spanning_the_period_exactly_leaves_no_gap(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-01", "10.00", "DBIT", "CARTE MAGASIN CB*08"))
        _set_account(session, master_key, CURRENT, synced=date(2026, 3, 31))
        assert real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY).coverage_gaps == []

    def test_an_imported_account_is_known_until_its_last_operation(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-12-02", "10.00", "DBIT", "CARTE MAGASIN CB*08"),
            (LIVRET, "2025-12-10", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
            (LIVRET, "2026-02-10", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        _set_account(session, master_key, CURRENT, synced=TODAY)
        _unlink(session, master_key, LIVRET)

        [gap] = real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY).coverage_gaps

        assert (gap.covered_until, gap.starts_late, gap.ends_early) == (date(2026, 2, 10), False, True)
        assert real_cashflow_month(session, USER, master_key, "2026-01", today=TODAY).coverage_gaps == []

    def test_an_imported_account_is_known_until_the_user_vouched_for_it(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2025-12-02", "10.00", "DBIT", "CARTE MAGASIN CB*08"),
            (LIVRET, "2025-12-10", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        _set_account(session, master_key, CURRENT, synced=TODAY)
        _unlink(session, master_key, LIVRET)
        confirm_up_to_date(session, session.get(BankAccount, LIVRET), today=date(2026, 3, 31))

        assert real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY).coverage_gaps == []

    def test_a_linked_account_is_known_until_its_last_sync(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2025-12-02", "10.00", "DBIT", "CARTE MAGASIN CB*08"))
        _set_account(session, master_key, CURRENT, synced=date(2026, 3, 20))

        [gap] = real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY).coverage_gaps

        assert (gap.covered_until, gap.ends_early) == (date(2026, 3, 20), True)


class TestCounterparts:
    def test_income_is_listed_by_counterpart_whatever_its_reference(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-27", "1500.00", "CRDT", "VIR SEPA EMPLOYEUR & CIE SALAIRE DE 2026-02 402147-1 Réf ZZ1KQCU8"),
            (CURRENT, "2026-03-31", "1500.00", "CRDT", "VIR SEPA EMPLOYEUR & CIE SALAIRE DE 2026-03 402147-1 Réf ZZ1KWVXD"),
            (CURRENT, "2026-03-10", "1000.00", "CRDT", "VIR SEPA JEAN TIERS"),
            (CURRENT, "2026-03-12", "120.00", "DBIT", "CARTE 11/03/26 RESTAURANT CB*08"),
            (CURRENT, "2026-03-14", "40.00", "CRDT", "Virement de : TITOUAN TIERS"),
            # The year before, read for the comparison, lists nothing.
            (CURRENT, "2025-03-10", "9000.00", "CRDT", "VIR SEPA ANCIEN EMPLOYEUR"),
            (CURRENT, "2025-03-12", "5000.00", "DBIT", "CARTE 11/03/25 CONCESSION CB*08"),
        )
        [friend] = [
            tx for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions
            if tx.label == "Virement de : TITOUAN TIERS"
        ]
        set_transaction_type(session, USER, master_key, friend.id, CashflowType.EXPENSE, TypeScope.LABEL)

        year = _year(session, master_key)

        assert [(c.name, c.amount, c.operation_count, c.share) for c in year.top_sources] == [
            ("Employeur & Cie Salaire De", Decimal("3000.00"), 2, Decimal("75.0")),
            ("Jean Tiers", Decimal("1000.00"), 1, Decimal("25.0")),
        ]
        assert [(c.name, c.amount) for c in year.top_destinations] == [("Restaurant", Decimal("120.00"))]
        assert [e.amount for e in year.top_expenses] == [Decimal("120.00")]

    def test_a_month_lists_only_its_own(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-12", "300.00", "DBIT", "CARTE 11/02/26 LIBRAIRIE CB*08"),
            (CURRENT, "2026-03-12", "120.00", "DBIT", "CARTE 11/03/26 RESTAURANT CB*08"),
        )
        month = real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY)
        assert [c.name for c in month.top_destinations] == ["Restaurant"]
        assert [e.label for e in month.top_expenses] == ["CARTE 11/03/26 RESTAURANT CB*08"]


class TestPace:
    def test_the_month_in_progress_is_read_against_the_recent_months_by_day(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-01-10", "100.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-01-25", "900.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-02-10", "300.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-02-25", "900.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-03-10", "500.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-03-25", "900.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-04-02", "350.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-04-03", "600.00", "DBIT", "VIR Virement depuis Compte courant"),
            (LIVRET, "2026-04-03", "600.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        _store(session, master_key, CURRENT, _raw("50.00", "DBIT", "2026-04-09", ref="pdng", status="PDNG", label="CARTE LIBRAIRIE"))

        pace = real_cashflow_current(session, USER, master_key, today=TODAY)

        assert (pace.period, pace.day, pace.spent_to_date, pace.pending_to_date) == (
            "2026-04", 10, Decimal("400.00"), Decimal("50.00"),
        )
        assert (pace.median_to_date, pace.median_month) == (Decimal("300.00"), Decimal("1200.00"))
        # 400 so far, plus the 900 the median month still spends after its 10th.
        assert pace.projection == Decimal("1300.00")
        assert len(pace.curve) == 30
        assert (pace.curve[9].spent, pace.curve[10].spent, pace.curve[29].median) == (Decimal("400.00"), None, Decimal("1200.00"))

    def test_a_day_past_a_shorter_month_s_end_reads_its_last_day(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-02-28", "80.00", "DBIT", "CARTE MAGASIN CB*08"),
            (CURRENT, "2026-03-02", "10.00", "DBIT", "CARTE MAGASIN CB*08"),
        )
        pace = real_cashflow_current(session, USER, master_key, today=date(2026, 3, 31))
        # February's 80 by its 28th still counts on March's 31st.
        assert pace.median_to_date == Decimal("80.00")

    def test_without_a_completed_month_there_is_nothing_to_compare(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-04-02", "25.00", "DBIT", "CARTE MAGASIN CB*08"))
        pace = real_cashflow_current(session, USER, master_key, today=TODAY)
        assert (pace.spent_to_date, pace.median_to_date, pace.median_month, pace.projection) == (
            Decimal("25.00"), None, None, None,
        )
