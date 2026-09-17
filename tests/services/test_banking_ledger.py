"""
The ledger (services/banking/ledger.py): every operation typed, for a reader
that filters and adds up on its own side. Its counted amounts must add up to
the real cashflow, month by month and type by type.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlmodel import Session, select

from dtos.banking import BankReviewKind, CashflowType, TypeScope
from models.banking import BankAccountLink
from services.banking import ledger
from services.banking.flows import list_month_transactions, set_transaction_type
from services.banking.ledger import build_ledger, ledger_etag
from services.banking.real_cashflow import real_cashflow_year
from tests.services.test_banking_flows import USER, _raw, _store
from tests.services.test_banking_real_cashflow import CURRENT, LIVRET, TODAY, _ops
from tests.services.test_banking_transfer_patterns import _top_up

NEOBANK = "neobank"
_FIELDS = {
    CashflowType.INCOME: "income", CashflowType.EXPENSE: "expenses", CashflowType.SAVING: "saving",
    CashflowType.INVESTMENT: "investment", CashflowType.NEUTRAL: "neutral",
}


def _answer(session: Session, master_key: str, label: str, kind: CashflowType, period: str) -> None:
    [target] = [tx for tx in list_month_transactions(session, USER, master_key, period).transactions if tx.label == label][-1:]
    set_transaction_type(session, USER, master_key, target.id, kind, TypeScope.LABEL)


def _by_label(ledger) -> dict[str, list]:
    rows = defaultdict(list)
    for row in ledger.rows:
        rows[row.label].append(row)
    return rows


def test_the_counted_rows_add_up_to_the_real_cashflow(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-01-02", "2000.00", "CRDT", "VIR SEPA EMPLOYEUR"),
        (CURRENT, "2026-01-05", "300.00", "DBIT", "VIR Virement depuis Compte courant"),
        (LIVRET, "2026-01-05", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        (CURRENT, "2026-02-02", "59.45", "DBIT", "CARTE 01/02/26 ZALANDO PAYMENTS CB*08"),
        (CURRENT, "2026-02-12", "59.45", "CRDT", "AVOIR 11/02/26 ZALANDO PAYMENTS CB*08"),
        (CURRENT, "2026-02-20", "120.00", "DBIT", "CARTE 19/02/26 RESTAURANT CB*08"),
        (CURRENT, "2026-02-21", "40.00", "CRDT", "Virement de : TITOUAN TIERS"),
        (CURRENT, "2026-03-06", "500.00", "DBIT", "VIR SEPA COURTIER EN LIGNE"),
        (CURRENT, "2026-03-10", "250.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (NEOBANK, "2026-03-16", "50.00", "DBIT", "To Emilien Roukine"),
        (CURRENT, "2026-03-17", "50.00", "CRDT", "VIR Virement de Emilien ROUKINE"),
        *_top_up("01", "05", "20.00"), *_top_up("02", "10", "35.50"), *_top_up("03", "14", "12.00"),
    )
    _store(session, master_key, CURRENT, _raw("99.00", "DBIT", "2026-03-20", ref="pdng", status="PDNG", label="CARTE LIBRAIRIE"))
    _store(session, master_key, CURRENT, _raw("12.63", "DBIT", "2026-03-21", ref="chf", currency="CHF", label="DENNER GENEVE"))
    _answer(session, master_key, "Virement de : TITOUAN TIERS", CashflowType.EXPENSE, "2026-02")
    _answer(session, master_key, "VIR SEPA COURTIER EN LIGNE", CashflowType.INVESTMENT, "2026-03")
    _answer(session, master_key, "VIR INST ROUKINE EMILIEN", CashflowType.SAVING, "2026-03")

    ledger = build_ledger(session, USER, master_key)
    summed: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    counted: dict[str, int] = defaultdict(int)
    for row in ledger.rows:
        if row.counted:
            period = f"{row.day:%Y-%m}"
            summed[(period, _FIELDS[row.cashflow_type])] += row.signed
            counted[period] += 1

    for year in (2025, 2026):
        for month in real_cashflow_year(session, USER, master_key, year, today=TODAY).months:
            for name in _FIELDS.values():
                assert summed[(month.period, name)] == getattr(month, name), (month.period, name)
            assert counted[month.period] == month.operation_count, month.period


def test_a_pending_or_foreign_operation_is_listed_but_not_counted(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "40.00", "DBIT", "CARTE 04/03/26 BOULANGERIE CB*08"))
    _store(session, master_key, CURRENT, _raw("99.00", "DBIT", "2026-03-06", ref="pdng", status="PDNG", label="CARTE LIBRAIRIE"))
    _store(session, master_key, CURRENT, _raw("12.63", "DBIT", "2026-03-07", ref="chf", currency="CHF", label="DENNER GENEVE"))

    rows = _by_label(build_ledger(session, USER, master_key))

    assert [(r.counted, r.signed) for r in rows["CARTE 04/03/26 BOULANGERIE CB*08"]] == [(True, Decimal("40.00"))]
    assert [(r.is_pending, r.counted, r.signed) for r in rows["CARTE LIBRAIRIE"]] == [(True, False, Decimal("0"))]
    assert [(r.currency, r.counted) for r in rows["DENNER GENEVE"]] == [("CHF", False)]


def test_questions_and_what_they_can_still_move_are_flagged(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-02-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (CURRENT, "2026-03-05", "90.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (NEOBANK, "2026-03-16", "50.00", "DBIT", "To Emilien Roukine"),
        (CURRENT, "2026-03-17", "50.00", "CRDT", "VIR Virement de Emilien ROUKINE"),
        (CURRENT, "2026-03-20", "40.00", "CRDT", "VIR SEPA VINTED"),
    )
    rows = _by_label(build_ledger(session, USER, master_key))

    # Newest first, as the Opérations list reads.
    assert [(r.day, r.question, r.open) for r in rows["VIR INST ROUKINE EMILIEN"]] == [
        (date(2026, 3, 5), BankReviewKind.FLOW, True), (date(2026, 2, 5), None, True),
    ]
    assert [(r.question, r.open) for r in rows["To Emilien Roukine"]] == [(BankReviewKind.TRANSFER, True)]
    assert [(r.question, r.open) for r in rows["VIR Virement de Emilien ROUKINE"]] == [(None, True)]
    assert [(r.question, r.open) for r in rows["VIR SEPA VINTED"]] == [(None, False)]


def test_one_counterpart_is_one_group_across_accounts_and_references(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-02-27", "1500.00", "CRDT", "VIR SEPA EMPLOYEUR SALAIRE DE 2026-02 402147-1 Réf ZZ1KQCU8"),
        (CURRENT, "2026-03-31", "1500.00", "CRDT", "VIR SEPA EMPLOYEUR SALAIRE DE 2026-03 402147-1 Réf ZZ1KWVXD"),
        (CURRENT, "2026-03-02", "10.00", "DBIT", "Vinted"),
        (NEOBANK, "2026-03-03", "12.00", "DBIT", "Vinted"),
        (CURRENT, "2026-03-04", "8.00", "CRDT", "Vinted"),
    )
    ledger = build_ledger(session, USER, master_key)
    rows = _by_label(ledger)
    salary = {row.group for label, found in rows.items() if "EMPLOYEUR" in label for row in found}
    purchases = {row.group for row in rows["Vinted"] if not row.is_credit}
    [sale] = [row.group for row in rows["Vinted"] if row.is_credit]

    assert len(salary) == len(purchases) == 1
    assert sale not in purchases
    [group] = salary
    assert (ledger.groups[group].name, ledger.groups[group].is_credit) == ("Employeur Salaire De", True)


def test_accounts_carry_their_coverage(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2025-11-03", "10.00", "DBIT", "CARTE BOULANGERIE CB*08"),
        (CURRENT, "2026-03-05", "10.00", "DBIT", "CARTE BOULANGERIE CB*08"),
    )
    [account] = build_ledger(session, USER, master_key).accounts
    # Linked by the test helper, last synced on 2026-01-01.
    assert (account.first_day, account.covered_until, account.linked) == (date(2025, 11, 3), date(2026, 1, 1), True)


def test_the_etag_moves_with_an_answer_and_an_import_only(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"))
    build_ledger(session, USER, master_key)
    first = ledger_etag(session, USER, master_key)
    assert ledger_etag(session, USER, master_key) == first

    _answer(session, master_key, "VIR INST ROUKINE EMILIEN", CashflowType.SAVING, "2026-03")
    answered = ledger_etag(session, USER, master_key)
    _ops(session, master_key, (CURRENT, "2026-03-06", "4.00", "DBIT", "CARTE BOULANGERIE CB*08"))

    assert len({first, answered, ledger_etag(session, USER, master_key)}) == 3


def test_the_etag_moves_with_a_sync(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "4.00", "DBIT", "CARTE BOULANGERIE CB*08"))
    before = ledger_etag(session, USER, master_key)
    link = session.exec(select(BankAccountLink)).one()
    link.last_synced_at = date(2026, 4, 9)
    session.add(link)
    session.commit()

    assert ledger_etag(session, USER, master_key) != before


def test_the_etag_moves_with_the_ledger_version(session: Session, master_key: str, monkeypatch):
    _ops(session, master_key, (CURRENT, "2026-03-05", "4.00", "DBIT", "CARTE BOULANGERIE CB*08"))
    before = ledger_etag(session, USER, master_key)
    monkeypatch.setattr(ledger, "LEDGER_VERSION", ledger.LEDGER_VERSION + "-next")

    assert ledger_etag(session, USER, master_key) != before


def test_a_rule_typed_row_names_its_rule(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"))
    _answer(session, master_key, "VIR INST ROUKINE EMILIEN", CashflowType.SAVING, "2026-03")
    [row] = build_ledger(session, USER, master_key).rows
    assert (row.type_source.value, row.type_rule_id is not None) == ("rule", True)
