"""
The review queue (flows.review_queue): every open question of the history,
the one an answer moves most money with first.
"""
from decimal import Decimal

from sqlmodel import Session

from dtos.banking import BankReviewKind, CashflowType as Type, TypeScope
from services.banking import transfer_patterns as stored_patterns
from services.banking.flows import (
    _regulated_savings,
    _user_accounts,
    list_month_transactions,
    review_queue,
    set_transaction_type,
    transfer_patterns,
)
from services.banking.transfer_patterns import FlowCarrier
from services.encryption import hash_index
from tests.services.test_banking_flows import USER
from tests.services.test_banking_real_cashflow import CURRENT, _ops

NEOBANK = "neobank"


def _queue(session: Session, master_key: str, year: int | None = None):
    queue = review_queue(session, USER, master_key, year)
    return [(q.kind, q.transaction.label, q.amount, q.operation_count) for q in queue.questions], queue


def test_questions_come_heaviest_first_whatever_their_month(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2025-06-05", "19000.00", "DBIT", "VIR SEPA JEAN TIERS"),
        (CURRENT, "2026-01-05", "150.00", "CRDT", "VIR SEPA VINTED"),
        (CURRENT, "2026-02-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (CURRENT, "2026-03-05", "90.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
    )
    questions, queue = _queue(session, master_key)
    assert questions == [
        (BankReviewKind.FLOW, "VIR SEPA JEAN TIERS", Decimal("19000.00"), 1),
        (BankReviewKind.FLOW, "VIR INST ROUKINE EMILIEN", Decimal("490.00"), 2),
        (BankReviewKind.FLOW, "VIR SEPA VINTED", Decimal("150.00"), 1),
    ]
    assert (queue.total_amount, queue.total_count) == (Decimal("19640.00"), 3)


def test_a_suggested_pair_is_asked_once_on_its_debit(session: Session, master_key: str):
    _ops(
        session, master_key,
        (NEOBANK, "2026-03-16", "50.00", "DBIT", "To Emilien Roukine"),
        (CURRENT, "2026-03-17", "50.00", "CRDT", "VIR Virement de Emilien ROUKINE"),
    )
    questions, _ = _queue(session, master_key)
    assert questions == [(BankReviewKind.TRANSFER, "To Emilien Roukine", Decimal("50.00"), 2)]


def test_an_answered_label_leaves_the_queue(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"))
    [(_, _, _, _)], queue = _queue(session, master_key)

    set_transaction_type(session, USER, master_key, queue.questions[0].transaction.id, Type.SAVING, TypeScope.LABEL)

    assert _queue(session, master_key)[0] == []


def test_a_label_under_the_minimum_amount_is_not_queued(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "40.00", "CRDT", "VIR SEPA VINTED"))
    assert _queue(session, master_key)[0] == []


def test_a_year_narrows_the_questions_but_not_the_years(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2025-06-05", "1000.00", "DBIT", "VIR SEPA JEAN TIERS"),
        (CURRENT, "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
        (CURRENT, "2026-03-06", "300.00", "CRDT", "VIR SEPA EMPLOYEUR"),
    )
    questions, queue = _queue(session, master_key, 2025)

    assert questions == [(BankReviewKind.FLOW, "VIR SEPA JEAN TIERS", Decimal("1000.00"), 1)]
    assert (queue.total_amount, queue.total_count) == (Decimal("1000.00"), 1)
    assert [(y.year, y.amount, y.count) for y in queue.years] == [
        (2026, Decimal("700.00"), 2), (2025, Decimal("1000.00"), 1),
    ]


def test_equal_amounts_come_latest_first(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-01-05", "300.00", "DBIT", "VIR SEPA JEAN TIERS"),
        (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR SEPA PAUL TIERS"),
    )
    assert [label for _, label, _, _ in _queue(session, master_key)[0]] == ["VIR SEPA PAUL TIERS", "VIR SEPA JEAN TIERS"]


def test_a_stored_carrier_nothing_asks_about_is_not_queued(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-02", "420.10", "DBIT", "CARTE 01/03/26 CARREFOUR ANNECY CB*08"))
    [card] = list_month_transactions(session, USER, master_key, "2026-03").transactions
    patterns = transfer_patterns(session, USER, master_key)
    patterns.flow_carriers[card.id] = FlowCarrier(1, Decimal("420.10"))
    accounts = _user_accounts(session, USER, master_key)
    user_bidx = hash_index(USER, master_key)
    source = stored_patterns.source_digest(
        session, user_bidx, accounts.readable, _regulated_savings(accounts, master_key), master_key,
    )
    stored_patterns.write_patterns(session, user_bidx, source, patterns, master_key)

    assert _queue(session, master_key)[0] == []
