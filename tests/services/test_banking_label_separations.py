"""
Separated labels (services/banking/label_separations.py, docs/bank-sorting.md
R54): one identity holding operations of different natures, each asked alone.

Labels are shaped like the real ones, names replaced.
"""
from decimal import Decimal

import pytest
from sqlmodel import Session

from dtos.banking import (
    BankContributionDecisionKind as Said,
    BankTransferStatus,
    BankTypeRuleWrite,
    CashflowType as Type,
    RuleMatch,
    TypeScope,
    TypeSource as Source,
)
from services.bank import delete_bank_account
from services.banking.contribution_decisions import record_contribution_decision
from services.banking.flows import (
    LabelRequiredError,
    SeparatedLabelError,
    list_flow_group,
    list_label_separations,
    list_month_transactions,
    list_type_rules,
    review_queue,
    separate_label,
    separation_preview,
    set_transaction_type,
    write_rule,
)
from services.banking.label_separations import delete_separation, load_separations
from tests.services.test_banking_contributions import _deposit, _pea
from tests.services.test_banking_flows import USER
from tests.services.test_banking_real_cashflow import CURRENT, LIVRET, _ops

# The bank's format alone is left of these labels' words: "carte cb".
AREA = "CARTE 12/03/26 A.R.E.A. CB*0837"
HL = "CARTE 14/03/26 H&L CB*0837"
SHOP = "CARTE 20/03/26 TOMMY347 CB*0837"


def _operations(session: Session, master_key: str, period: str = "2026-03"):
    return list_month_transactions(session, USER, master_key, period).transactions


def _asked(session: Session, master_key: str, period: str = "2026-03") -> dict[Decimal, int]:
    return {
        tx.amount: tx.flow_question.operation_count
        for tx in _operations(session, master_key, period) if tx.flow_question
    }


def _two_shops(session: Session, master_key: str) -> None:
    """Two payments past the threshold, to two shops "carte cb" cannot tell apart."""
    _ops(
        session, master_key,
        (CURRENT, "2026-03-12", "900.00", "DBIT", AREA),
        (CURRENT, "2026-03-14", "620.00", "DBIT", HL),
    )


def _one(session: Session, master_key: str, amount: str, period: str = "2026-03"):
    [tx] = [tx for tx in _operations(session, master_key, period) if tx.amount == Decimal(amount)]
    return tx


def test_a_separated_label_asks_each_operation_alone(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-12", "900.00", "DBIT", AREA),
        (CURRENT, "2026-03-14", "620.00", "DBIT", HL),
        (CURRENT, "2026-03-20", "40.00", "DBIT", SHOP),
    )
    assert _asked(session, master_key) == {Decimal("620.00"): 2}

    separate_label(session, USER, master_key, _one(session, master_key, "620.00").id)

    assert _asked(session, master_key) == {Decimal("900.00"): 1, Decimal("620.00"): 1}
    assert all(tx.label_separated for tx in _operations(session, master_key))
    assert [tx.amount for tx in list_flow_group(session, USER, master_key, _one(session, master_key, "900.00").id)] == [
        Decimal("900.00"),
    ]


def test_a_separated_credit_asks_only_past_the_minimum_on_its_own(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-05", "125.00", "CRDT", "Paiement envoyé par MANGOPAY"),
        (CURRENT, "2026-03-12", "60.00", "CRDT", "Paiement envoyé par MANGOPAY"),
        (CURRENT, "2026-03-19", "45.00", "CRDT", "Paiement envoyé par MANGOPAY"),
    )
    assert _asked(session, master_key) == {Decimal("45.00"): 3}

    separate_label(session, USER, master_key, _one(session, master_key, "45.00").id)

    assert _asked(session, master_key) == {Decimal("125.00"): 1}
    assert _one(session, master_key, "60.00").type_source is Source.DEFAULT


def test_the_review_queue_asks_each_operation_of_a_separated_label_apart(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-12", "900.00", "DBIT", AREA),
        (CURRENT, "2026-03-14", "620.00", "DBIT", HL),
    )
    separate_label(session, USER, master_key, _one(session, master_key, "620.00").id)

    queue = review_queue(session, USER, master_key)

    assert sorted((q.amount, q.operation_count) for q in queue.questions) == [
        (Decimal("620.00"), 1), (Decimal("900.00"), 1),
    ]
    assert len({q.group for q in queue.questions}) == 2
    assert queue.total_amount == Decimal("1520.00")


def test_a_separated_label_takes_no_answer_for_the_label(session: Session, master_key: str):
    _two_shops(session, master_key)
    target = _one(session, master_key, "620.00")
    separate_label(session, USER, master_key, target.id)

    with pytest.raises(SeparatedLabelError):
        set_transaction_type(session, USER, master_key, target.id, Type.SAVING, TypeScope.LABEL)

    set_transaction_type(session, USER, master_key, target.id, Type.SAVING, TypeScope.OPERATION)
    assert (_one(session, master_key, "620.00").cashflow_type, _one(session, master_key, "900.00").type_source) == (
        Type.SAVING, Source.DEFAULT,
    )


def test_separating_withdraws_the_label_s_own_rule_and_says_so_before(session: Session, master_key: str):
    _two_shops(session, master_key)
    target = _one(session, master_key, "620.00")
    set_transaction_type(session, USER, master_key, target.id, Type.SAVING, TypeScope.LABEL)

    preview = separation_preview(session, USER, master_key, target.id)
    assert (preview.separated, preview.operation_count, preview.nearby_count) == (False, 2, 0)
    assert (preview.rule.type, preview.rule.operation_count) == (Type.SAVING, 2)

    separate_label(session, USER, master_key, target.id)

    assert list_type_rules(session, USER, master_key) == []
    assert _asked(session, master_key) == {Decimal("900.00"): 1, Decimal("620.00"): 1}
    assert separation_preview(session, USER, master_key, target.id).separated


def test_no_nearby_label_s_rule_reaches_a_separated_label(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-05", "300.00", "CRDT", "VIR SEPA VILMORIN SALAIRE FEVRIER REFAB"),
        (CURRENT, "2026-03-28", "200.00", "CRDT", "VIR SEPA VILMORIN SALAIRE MARS REFAB"),
    )
    salary = _one(session, master_key, "300.00")
    set_transaction_type(session, USER, master_key, salary.id, Type.SAVING, TypeScope.LABEL)
    nearby = _one(session, master_key, "200.00")
    assert nearby.type_source is Source.RULE

    assert separation_preview(session, USER, master_key, nearby.id).nearby_count == 1
    separate_label(session, USER, master_key, nearby.id)

    assert (_one(session, master_key, "200.00").type_source, _one(session, master_key, "300.00").type_source) == (
        Source.DEFAULT, Source.RULE,
    )
    assert [rule.operation_count for rule in list_type_rules(session, USER, master_key)] == [1]


def test_a_written_rule_still_types_a_separated_label(session: Session, master_key: str):
    _two_shops(session, master_key)
    separate_label(session, USER, master_key, _one(session, master_key, "620.00").id)

    write_rule(session, USER, master_key, BankTypeRuleWrite(
        match=RuleMatch.CONTAINS, text="A.R.E.A.", is_credit=False, type=Type.NEUTRAL,
    ))

    area = _one(session, master_key, "900.00")
    assert (area.cashflow_type, area.type_source, area.flow_question) == (Type.NEUTRAL, Source.RULE, None)
    assert _asked(session, master_key) == {Decimal("620.00"): 1}


def test_a_yes_no_longer_vouches_for_a_separated_label(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-02-05", "200.00", "DBIT", "VIR Virement vers PEA"),
        (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement vers PEA"),
    )
    _pea(session, master_key)
    _deposit(session, master_key, "2026-02-05", "200")
    _deposit(session, master_key, "2026-03-05", "300")
    first = _one(session, master_key, "200.00", "2026-02")
    record_contribution_decision(session, USER, master_key, first.id, first.contribution.id, Said.CONTRIBUTION)
    assert _one(session, master_key, "300.00").type_source is Source.CONTRIBUTION

    separate_label(session, USER, master_key, first.id)

    later = _one(session, master_key, "300.00")
    assert (later.type_source, later.asks_contribution, later.flow_question.operation_count) == (
        Source.DEFAULT, True, 1,
    )
    assert _one(session, master_key, "200.00", "2026-02").type_source is Source.CONTRIBUTION


def test_a_separated_label_keeps_its_pairs(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-10", "500.00", "DBIT", "VIR Virement interne"),
        (LIVRET, "2026-03-10", "500.00", "CRDT", "VIR Virement interne"),
    )
    debit = next(tx for tx in _operations(session, master_key) if not tx.is_credit)

    separate_label(session, USER, master_key, debit.id)

    assert {tx.transfer_status for tx in _operations(session, master_key)} == {BankTransferStatus.SAVINGS}


def test_a_separation_holds_for_the_operations_imported_later(session: Session, master_key: str):
    _two_shops(session, master_key)
    separate_label(session, USER, master_key, _one(session, master_key, "620.00").id)

    _ops(session, master_key, (CURRENT, "2026-04-02", "700.00", "DBIT", "CARTE 01/04/26 TOMMY347 CB*0837"))

    later = _one(session, master_key, "700.00", "2026-04")
    assert (later.label_separated, later.flow_question.operation_count) == (True, 1)


def test_regrouping_brings_the_label_s_question_back_whole(session: Session, master_key: str):
    _two_shops(session, master_key)
    separated = separate_label(session, USER, master_key, _one(session, master_key, "620.00").id)

    delete_separation(session, USER, master_key, separated.id)

    assert _asked(session, master_key) == {Decimal("620.00"): 2}
    assert not any(tx.label_separated for tx in _operations(session, master_key))


def test_the_separated_label_is_listed_with_its_operations(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-12", "900.00", "DBIT", AREA),
        (CURRENT, "2026-03-14", "620.00", "DBIT", HL),
        (CURRENT, "2026-03-15", "20.00", "CRDT", "AVOIR 15/03/26 H&L CB*0837"),
    )
    separate_label(session, USER, master_key, _one(session, master_key, "620.00").id)
    separate_label(session, USER, master_key, _one(session, master_key, "620.00").id)

    [listed] = list_label_separations(session, USER, master_key)
    assert (listed.signature, listed.is_credit, listed.operation_count, listed.label) == ("carte cb", False, 2, HL)


def test_a_label_without_words_separates_on_its_own_text(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-05", "120.00", "CRDT", "00160669"),
        (CURRENT, "2026-03-12", "60.00", "CRDT", "00160669"),
    )
    assert _asked(session, master_key) == {Decimal("60.00"): 2}

    separated = separate_label(session, USER, master_key, _one(session, master_key, "60.00").id)

    assert separated.signature == "#00160669"
    assert _asked(session, master_key) == {Decimal("120.00"): 1}


def test_a_label_without_letter_or_digit_does_not_separate(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-05", "120.00", "CRDT", "* - *"))

    with pytest.raises(LabelRequiredError):
        separate_label(session, USER, master_key, _one(session, master_key, "120.00").id)


def test_the_separations_of_an_account_go_with_it(session: Session, master_key: str):
    _ops(session, master_key, (CURRENT, "2026-03-12", "900.00", "DBIT", AREA))
    separate_label(session, USER, master_key, _one(session, master_key, "900.00").id)

    delete_bank_account(session, CURRENT, master_key)

    assert load_separations(session, USER, master_key) == []
