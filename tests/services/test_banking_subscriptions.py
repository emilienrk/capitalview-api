"""
Subscriptions in the rebuild of the transfer patterns
(services/banking/subscription_series.py) and what the readers make of them.

Labels are shaped like the real ones, names replaced.
"""
from datetime import date

from sqlmodel import Session

from dtos.banking import CashflowType as Type, TypeScope
from services.banking.flows import list_month_transactions, set_transaction_type, transfer_patterns
from tests.services.test_banking_flows import USER
from tests.services.test_banking_real_cashflow import CURRENT, LIVRET, _ops

NEOBANK = "neobank"


def _months(account: str, first: str, count: int, day: int, amount, label: str, direction: str = "DBIT"):
    """One operation a month from `first` ("YYYY-MM"), `amount` a string or a
    function of the month's rank."""
    year, month = int(first[:4]), int(first[5:])
    operations = []
    for k in range(count):
        index = year * 12 + month - 1 + k
        operations.append((
            account, f"{index // 12:04d}-{index % 12 + 1:02d}-{day:02d}",
            amount(k) if callable(amount) else amount, direction, label,
        ))
    return operations


def _subscriptions(session: Session, master_key: str):
    return transfer_patterns(session, USER, master_key).subscriptions


def test_a_monthly_direct_debit_is_counted_without_asking(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2025-06", 8, 5, "39.00", "PRLV SEPA BASIC FIT"))

    [subscription] = _subscriptions(session, master_key)
    assert (subscription.state, subscription.confidence, subscription.cadence) == ("auto", "certain", "monthly")
    assert (subscription.counted, subscription.question) == (True, False)
    assert len(subscription.members) == 8
    assert transfer_patterns(session, USER, master_key).subscription_questions == {}


def test_three_card_payments_ask_on_the_last_one(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, "21.60", "CARTE ANTHROPIC* CLAUDE CB*0837"))

    [subscription] = _subscriptions(session, master_key)
    assert (subscription.state, subscription.confidence, subscription.counted) == ("candidate", "probable", False)
    assert subscription.question
    march = {tx.operation_date: tx for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions}
    assert subscription.carrier == march[date(2026, 3, 2)].id
    assert transfer_patterns(session, USER, master_key).subscription_questions == {"2026-03": 1}


def test_rent_by_transfer_asks_its_flow_question_first(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 4, 3, "530.00", "VIR SEPA TRANSALP'DOME S.A.S."))

    [subscription] = _subscriptions(session, master_key)
    assert subscription.state == "candidate" and not subscription.question
    assert transfer_patterns(session, USER, master_key).subscription_questions == {}

    last = list_month_transactions(session, USER, master_key, "2026-04").transactions[0]
    set_transaction_type(session, USER, master_key, last.id, Type.EXPENSE, TypeScope.LABEL)

    [subscription] = _subscriptions(session, master_key)
    assert subscription.question
    assert transfer_patterns(session, USER, master_key).subscription_questions == {"2026-04": 1}


def test_rent_the_user_typed_neutral_is_not_offered(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 4, 3, "380.00", "VIR SEPA Frederic Durand"))
    last = list_month_transactions(session, USER, master_key, "2026-04").transactions[0]
    set_transaction_type(session, USER, master_key, last.id, Type.NEUTRAL, TypeScope.LABEL)

    assert _subscriptions(session, master_key) == []


def test_an_internal_transfer_is_never_a_member(session: Session, master_key: str):
    # One month's rent meets a credit of its amount on the savings account the
    # same day: a pair by law, so that month is not the rent's.
    _ops(
        session, master_key,
        *_months(CURRENT, "2026-01", 5, 3, "530.00", "VIR SEPA TRANSALP'DOME S.A.S."),
        (LIVRET, "2026-03-03", "530.00", "CRDT", "VIR SEPA DEPUIS COMPTE COURANT"),
    )
    last = list_month_transactions(session, USER, master_key, "2026-05").transactions[0]
    set_transaction_type(session, USER, master_key, last.id, Type.EXPENSE, TypeScope.LABEL)
    paired = {tx.id for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions}

    [subscription] = _subscriptions(session, master_key)
    assert len(subscription.members) == 4
    assert not paired & {member.uuid for member in subscription.members}


def test_the_rebuild_is_the_same_twice(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "39.00", "PRLV SEPA BASIC FIT"),
        *_months(CURRENT, "2026-01", 3, 2, "21.60", "CARTE ANTHROPIC* CLAUDE CB*0837"),
        *_months(NEOBANK, "2025-01", 6, 12, lambda k: f"{10 + k}.40", "CARTE LIDL"),
    )
    first = [s.to_json() for s in transfer_patterns(session, USER, master_key, rebuild=True).subscriptions]
    second = [s.to_json() for s in transfer_patterns(session, USER, master_key, rebuild=True).subscriptions]
    assert first == second and first
