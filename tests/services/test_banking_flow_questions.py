"""
Flow questions (services/banking/flows.py, stored by the transfer-pattern
rebuild): what only the user can say about an operation nothing pairs or types.

Labels are shaped like the real ones, names replaced.
"""
from datetime import date
from decimal import Decimal

from sqlmodel import Session

from dtos.banking import BankTransferDecisionKind, CashflowType as Type, TypeScope
from services.banking import transfer_patterns as stored_patterns
from services.banking.flows import (
    CREDIT_CHOICES,
    DEBIT_CHOICES,
    _regulated_savings,
    _user_accounts,
    list_month_transactions,
    set_transaction_type,
    transfer_patterns,
)
from services.banking.real_cashflow import real_cashflow_month
from services.banking.transfer_decisions import record_decision
from services.encryption import hash_index
from tests.services.test_banking_flows import USER, _raw, _store
from tests.services.test_banking_real_cashflow import CURRENT, _ops

NEOBANK = "neobank"


def _questions(session: Session, master_key: str, period: str = "2026-03") -> dict[str, tuple]:
    return {
        tx.label: (tx.flow_question.choices, tx.flow_question.operation_count)
        for tx in list_month_transactions(session, USER, master_key, period).transactions
        if tx.flow_question
    }


def _total(session: Session, master_key: str) -> int:
    return sum(transfer_patterns(session, USER, master_key).flow_questions.values())


def test_a_credit_asks_whatever_its_payment_means(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-12", "59.45", "CRDT", "AVOIR 11/03/26 ZALANDO PAYMENTS CB*08"))
    assert _questions(session, master_key) == {"AVOIR 11/03/26 ZALANDO PAYMENTS CB*08": (CREDIT_CHOICES, 1)}


def test_a_card_payment_never_asks_and_a_transfer_sent_does(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-02", "42.10", "DBIT", "CARTE 01/03/26 CARREFOUR ANNECY CB*08"),
        (CURRENT, "2026-03-05", "19000.00", "DBIT", "VIR SEPA JEAN TIERS"),
    )
    assert _questions(session, master_key) == {"VIR SEPA JEAN TIERS": (DEBIT_CHOICES, 1)}


def test_a_label_asks_once_on_its_last_operation(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-01-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (CURRENT, "2026-02-05", "150.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (CURRENT, "2026-03-05", "90.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
    )

    assert _questions(session, master_key, "2026-01") == {}
    assert _questions(session, master_key) == {"VIR INST ROUKINE EMILIEN": (DEBIT_CHOICES, 3)}
    assert transfer_patterns(session, USER, master_key).flow_questions == {"2026-03": 1}
    assert transfer_patterns(session, USER, master_key).flow_open == {"2026-01": 1, "2026-02": 1, "2026-03": 1}
    assert list_month_transactions(session, USER, master_key, "2026-03").transfer_questions == 1


def test_a_pending_operation_does_not_ask(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-02", "10.00", "DBIT", "CARTE 01/03/26 BOULANGERIE CB*08"))
    _store(session, master_key, CURRENT, _raw("80.00", "CRDT", "2026-03-06", ref="pdng", status="PDNG", label="VIR SEPA JEAN TIERS"))
    assert _questions(session, master_key) == {}


def test_an_answer_settles_the_label_and_a_nearby_one_imported_later(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-02-05", "1850.00", "CRDT", "VIR SEPA VILMORIN SALAIRE FEVRIER REFAB"),
        (CURRENT, "2026-03-05", "1850.00", "CRDT", "VIR SEPA VILMORIN SALAIRE FEVRIER REFAB"),
    )
    [carrier] = [tx for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions]
    assert _total(session, master_key) == 1

    set_transaction_type(session, USER, master_key, carrier.id, Type.INCOME, TypeScope.LABEL)

    # Nothing but the rule moved: the stored questions follow it all the same.
    assert _total(session, master_key) == 0
    _ops(session, master_key, (CURRENT, "2026-03-28", "1850.00", "CRDT", "VIR SEPA VILMORIN SALAIRE MARS REFAB"))
    assert _questions(session, master_key) == {}


def test_a_salary_whose_reference_changes_every_month_asks_once(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-06-30", "1410.86", "CRDT", "VIR SEPA VILMORIN & CIE SALAIRE DE 2026-06 402147-1 Réf ZZ1KQCU8WQC5OZZ7OWGDQFCIK"),
        (CURRENT, "2026-07-31", "1410.86", "CRDT", "VIR SEPA VILMORIN & CIE SALAIRE DE 2026-07 402147-1 Réf ZZ1KWVXDWRC9GJPBAZZ1KWVXDX4JPFVFQ"),
    )
    [june] = list_month_transactions(session, USER, master_key, "2026-06").transactions
    set_transaction_type(session, USER, master_key, june.id, Type.INCOME, TypeScope.LABEL)

    _ops(session, master_key, (CURRENT, "2026-08-31", "1410.86", "CRDT", "VIR SEPA VILMORIN & CIE SALAIRE DE 2026-08 402147-1 Réf ZZ1L2ZJSYU78NB5TWZZ1L2ZJSZ8833T8P0"))

    assert _total(session, master_key) == 0


def test_a_suggested_pair_asks_its_own_question_until_it_is_refused(session: Session, master_key: str):
    _ops(
        session, master_key,
        (NEOBANK, "2026-03-16", "50.00", "DBIT", "To Emilien Roukine"),
        (CURRENT, "2026-03-17", "50.00", "CRDT", "VIR Virement de Emilien ROUKINE"),
    )
    month = {tx.label: tx for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions}
    assert _questions(session, master_key) == {}

    record_decision(
        session, USER, master_key, month["To Emilien Roukine"].id, month["VIR Virement de Emilien ROUKINE"].id,
        BankTransferDecisionKind.NOT_TRANSFER,
    )

    assert _questions(session, master_key) == {
        "To Emilien Roukine": (DEBIT_CHOICES, 1), "VIR Virement de Emilien ROUKINE": (CREDIT_CHOICES, 1),
    }


def test_a_refund_answer_lowers_the_month_s_expenses(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-02", "120.00", "DBIT", "CARTE 01/03/26 RESTAURANT DU LAC CB*08"),
        (CURRENT, "2026-03-04", "40.00", "CRDT", "Virement de : TITOUAN TIERS"),
    )
    [friend] = [tx for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions if tx.is_credit]

    set_transaction_type(session, USER, master_key, friend.id, Type.EXPENSE, TypeScope.LABEL)

    month = real_cashflow_month(session, USER, master_key, "2026-03", today=date(2026, 4, 10))
    assert (month.totals.expenses, month.totals.income) == (Decimal("80.00"), Decimal("0"))


def test_a_stored_carrier_asks_only_if_the_month_still_reads_it_as_one(session: Session, master_key: str):
    """The month pairs over its own window, which can settle what the whole
    history left open."""
    _ops(session, master_key, (CURRENT, "2026-03-02", "42.10", "DBIT", "CARTE 01/03/26 CARREFOUR ANNECY CB*08"))
    [card] = list_month_transactions(session, USER, master_key, "2026-03").transactions
    patterns = transfer_patterns(session, USER, master_key)
    patterns.flow_carriers[card.id] = 1
    accounts = _user_accounts(session, USER, master_key)
    user_bidx = hash_index(USER, master_key)
    source = stored_patterns.source_digest(
        session, user_bidx, accounts.readable, _regulated_savings(accounts, master_key), master_key,
    )
    stored_patterns.write_patterns(session, user_bidx, source, patterns, master_key)

    assert _questions(session, master_key) == {}
