"""
Recurring payments and income in the rebuild of the transfer patterns
(services/banking/recurring_series.py) and what the readers make of them.

Labels are shaped like the real ones, names replaced.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from dtos.banking import (
    BankTransferDecisionKind,
    RecurringDecisionKind,
    RecurringDirection,
    RecurringNature,
    RecurringState,
    TypeScope,
)
from dtos.banking import CashflowType as Type
from models.banking import BankAccountLink
from services.banking.flows import (
    list_flow_group,
    list_month_transactions,
    review_queue,
    set_transaction_type,
    transfer_patterns,
)
from services.banking.ledger import build_ledger
from services.banking.real_cashflow import (
    real_cashflow_current,
    real_cashflow_month,
    real_cashflow_year,
)
from services.banking.recurring import (
    DirectionMismatchError,
    NatureMismatchError,
    decide,
    list_recurring,
    merge,
    update,
)
from services.banking.transfer_decisions import record_decision
from services.encryption import hash_index
from tests.services.test_banking_flows import USER, _raw, _store
from tests.services.test_banking_real_cashflow import CURRENT, LIVRET, TODAY, _ops

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


def _recurring(session: Session, master_key: str):
    return transfer_patterns(session, USER, master_key).recurring


def test_a_monthly_direct_debit_is_counted_without_asking(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2025-06", 8, 5, "39.00", "PRLV SEPA BASIC FIT"))

    [stored] = _recurring(session, master_key)
    assert (stored.state, stored.confidence, stored.cadence) == ("auto", "certain", "monthly")
    assert (stored.counted, stored.question) == (True, False)
    assert len(stored.members) == 8
    assert transfer_patterns(session, USER, master_key).recurring_questions == {}


def test_three_card_payments_of_one_amount_count_without_asking(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, "21.60", "CARTE ANTHROPIC* CLAUDE CB*0837"))

    [stored] = _recurring(session, master_key)
    assert (stored.state, stored.confidence, stored.counted, stored.question) == ("auto", "probable", True, False)
    assert transfer_patterns(session, USER, master_key).recurring_questions == {}


def test_a_card_payment_whose_amount_moves_still_asks(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, lambda k: f"{7 + k}.79", "CARTE OVH SAS CB*0837"))

    [stored] = _recurring(session, master_key)
    assert (stored.state, stored.variable, stored.counted, stored.question) == ("candidate", True, False, True)


def test_three_transfers_ask_on_the_last_one(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, "21.60", POCKET_MONEY))

    [stored] = _recurring(session, master_key)
    assert (stored.state, stored.confidence, stored.counted) == ("candidate", "probable", False)
    assert stored.question
    march = {tx.operation_date: tx for tx in list_month_transactions(session, USER, master_key, "2026-03").transactions}
    assert stored.carrier == march[date(2026, 3, 2)].id
    assert transfer_patterns(session, USER, master_key).recurring_questions == {"2026-03": 1}


def test_rent_by_transfer_asks_its_flow_question_first(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 4, 3, "530.00", "VIR SEPA TRANSALP'DOME S.A.S."))

    [stored] = _recurring(session, master_key)
    assert stored.state == "candidate" and not stored.question
    assert transfer_patterns(session, USER, master_key).recurring_questions == {}

    last = list_month_transactions(session, USER, master_key, "2026-04").transactions[0]
    set_transaction_type(session, USER, master_key, last.id, Type.EXPENSE, TypeScope.LABEL)

    [stored] = _recurring(session, master_key)
    assert stored.question
    assert transfer_patterns(session, USER, master_key).recurring_questions == {"2026-04": 1}


def test_rent_the_user_typed_neutral_is_not_offered(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 4, 3, "380.00", "VIR SEPA Frederic Durand"))
    last = list_month_transactions(session, USER, master_key, "2026-04").transactions[0]
    set_transaction_type(session, USER, master_key, last.id, Type.NEUTRAL, TypeScope.LABEL)

    assert _recurring(session, master_key) == []


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

    [stored] = _recurring(session, master_key)
    assert len(stored.members) == 4
    assert not paired & {member.uuid for member in stored.members}


def test_the_rebuild_is_the_same_twice(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "39.00", "PRLV SEPA BASIC FIT"),
        *_months(CURRENT, "2026-01", 3, 2, "21.60", "CARTE ANTHROPIC* CLAUDE CB*0837"),
        *_months(NEOBANK, "2025-01", 6, 12, lambda k: f"{10 + k}.40", "CARTE LIDL"),
    )
    first = [s.to_json() for s in transfer_patterns(session, USER, master_key, rebuild=True).recurring]
    second = [s.to_json() for s in transfer_patterns(session, USER, master_key, rebuild=True).recurring]
    assert first == second and first


# ---------------------------------------------------------------------------
# What the readers make of them
# ---------------------------------------------------------------------------

BASIC_FIT = "PRLV SEPA BASIC FIT"
# A transfer to a person: rent, pocket money or savings elsewhere, always asked.
POCKET_MONEY = "VIR SEPA MARIE DUPONT"
EDF = "PRLV SEPA EDF clients particuliers"
EDF_NAME = "EDF clients particuliers"


def _synced(session: Session, master_key: str, account: str, day: date) -> None:
    """The account's last sync: a recurring payment on an account synced long ago
    reads as stale, whatever it last debited."""
    link = session.exec(select(BankAccountLink).where(
        BankAccountLink.bank_account_uuid_bidx == hash_index(account, master_key)
    )).one()
    link.last_synced_at = day
    session.add(link)
    session.commit()


def _month_items(session: Session, master_key: str, period: str) -> list:
    return list_month_transactions(session, USER, master_key, period).transactions


def _bounced(session: Session, master_key: str, period: str) -> None:
    """Bind the month's debit to the credit that took it back."""
    month = {tx.is_credit: tx for tx in _month_items(session, master_key, period)}
    record_decision(session, USER, master_key, month[False].id, month[True].id, BankTransferDecisionKind.REVERSAL)


def test_a_member_carries_its_recurring_payment_and_a_cancelled_one_counts_for_nothing(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "39.00", BASIC_FIT),
        # October's debit bounced.
        (CURRENT, "2025-10-09", "39.00", "CRDT", "REJ PRLV SEPA BASIC FIT"),
    )
    _bounced(session, master_key, "2025-10")
    [september] = _month_items(session, master_key, "2025-09")
    tag = september.recurring
    assert (tag.state, tag.role, tag.cadence, tag.id) == ("auto", "regular", "monthly", None)
    october = {tx.is_credit: tx for tx in _month_items(session, master_key, "2025-10")}
    assert october[False].recurring.role == "cancelled"

    ledger = build_ledger(session, USER, master_key)
    rows = {row.id: row for row in ledger.rows}
    assert ledger.recurring[rows[september.id].recurring].key == tag.key
    assert rows[october[False].id].recurring is None


def test_the_question_sits_on_the_last_debit_alone_and_counts_in_the_month(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, "21.60", POCKET_MONEY))
    [february] = _month_items(session, master_key, "2026-02")
    [march] = _month_items(session, master_key, "2026-03")
    assert february.recurring_question is None and february.recurring is None
    question = march.recurring_question
    assert (question.cadence, question.amount, question.occurrence_count, question.since) == (
        "monthly", Decimal("21.60"), 3, date(2026, 1, 2),
    )
    assert question.annual_estimate == Decimal("259.20")
    assert list_month_transactions(session, USER, master_key, "2026-03").transfer_questions == 1
    rows = {row.id: row for row in build_ledger(session, USER, master_key).rows}
    assert (rows[march.id].question, rows[february.id].question) == ("recurring", None)


def test_the_queue_ranks_a_recurring_payment_by_its_yearly_cost_without_adding_it_up(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2026-01", 3, 2, "21.60", POCKET_MONEY),
        (CURRENT, "2026-02-10", "500.00", "CRDT", "VIR SEPA JEAN TIERS"),
        (CURRENT, "2026-02-12", "150.00", "CRDT", "VIR SEPA PAUL AUTRE"),
    )
    queue = review_queue(session, USER, master_key)
    assert [(q.kind, q.amount) for q in queue.questions] == [
        ("flow", Decimal("500.00")), ("recurring", Decimal("259.20")), ("flow", Decimal("150.00")),
    ]
    assert (queue.total_amount, queue.total_count, queue.recurring_count) == (Decimal("650.00"), 3, 1)
    assert [(y.year, y.amount, y.count) for y in queue.years] == [(2026, Decimal("650.00"), 3)]


def test_a_refund_from_a_counted_recurring_payment_asks_whatever_its_amount(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
        (CURRENT, "2026-02-20", "44.15", "CRDT", "VIR SEPA EDF clients particuliers REGULARISATION"),
        # The same amount from nobody's recurring payment asks nothing.
        (CURRENT, "2026-02-21", "44.15", "CRDT", "VIR SEPA JEAN TIERS"),
    )
    credits = {tx.label: tx for tx in _month_items(session, master_key, "2026-02") if tx.is_credit}
    refund = credits["VIR SEPA EDF clients particuliers REGULARISATION"]
    assert (refund.flow_question.suggested, refund.flow_question.recurring_name) == ("EXPENSE", "EDF clients particuliers")
    assert refund.recurring.role == "refund"
    assert credits["VIR SEPA JEAN TIERS"].flow_question is None
    assert [tx.id for tx in list_flow_group(session, USER, master_key, refund.id)] == [refund.id]


def test_recurring_payments_are_part_of_the_expenses_month_by_month_as_the_ledger_adds_them(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
        (CURRENT, "2025-10-09", "60.00", "CRDT", "REJ PRLV SEPA EDF clients particuliers"),
        (CURRENT, "2026-01-20", "44.15", "CRDT", "VIR SEPA EDF clients particuliers REGULARISATION"),
        # A refund nobody answered for yet: income until then.
        (CURRENT, "2025-12-15", "39.26", "CRDT", "VIR SEPA EDF clients particuliers"),
        *_months(CURRENT, "2025-06", 8, 12, lambda k: f"{30 + 7 * k}.10", "CARTE CARREFOUR ANNECY CB*0837"),
    )
    _bounced(session, master_key, "2025-10")
    refund = next(tx for tx in _month_items(session, master_key, "2026-01") if tx.is_credit)
    unanswered = next(tx for tx in _month_items(session, master_key, "2025-12") if tx.is_credit)
    set_transaction_type(session, USER, master_key, refund.id, Type.EXPENSE, TypeScope.OPERATION)

    year = real_cashflow_year(session, USER, master_key, 2025, today=TODAY)
    months = {m.period: m for m in year.months}
    # Paid, bounced and refunded: October counts nothing, January less the refund.
    assert months["2025-09"].recurring == Decimal("60.00")
    assert months["2025-10"].recurring == Decimal("0")
    assert months["2025-12"].recurring == Decimal("60.00")
    assert real_cashflow_month(session, USER, master_key, "2026-01", today=TODAY).totals.recurring == Decimal("15.85")
    assert all(m.recurring <= m.expenses for m in year.months)

    ledger = build_ledger(session, USER, master_key)
    assert next(row for row in ledger.rows if row.id == unanswered.id).recurring is None
    by_month: dict[str, Decimal] = {}
    for row in ledger.rows:
        if row.recurring is not None:
            assert row.counted
            by_month[f"{row.day:%Y-%m}"] = by_month.get(f"{row.day:%Y-%m}", Decimal("0")) + row.signed
    assert {p: m.recurring for p, m in months.items() if m.recurring} == {
        p: amount for p, amount in by_month.items() if p.startswith("2025") and amount
    }


def test_the_month_says_what_each_recurring_payment_is_filed_as(session: Session, master_key: str):
    # Nothing is guessed: a payment carries a nature only once the user files it.
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
        *_months(CURRENT, "2025-06", 8, 12, "39.00", BASIC_FIT),
    )
    edf = next(i for i in list_recurring(session, USER, master_key, today=TODAY).items if i.amount == Decimal("60.00"))
    assert edf.nature is None
    decided = decide(session, USER, master_key, edf.transaction_id, RecurringDecisionKind.CONFIRM)
    update(session, USER, master_key, decided.id, {"nature": RecurringNature.ENERGY})

    detail = real_cashflow_month(session, USER, master_key, "2025-09", today=TODAY)
    assert {item.name: item.nature for item in detail.recurring} == {EDF_NAME: RecurringNature.ENERGY, "Basic Fit": None}
    # Filing one moves no total: it says what the spending is, not how it counts.
    year = real_cashflow_year(session, USER, master_key, 2025, today=TODAY)
    assert {m.period: m.recurring for m in year.months}["2025-09"] == Decimal("99.00")
    assert all(m.recurring <= m.expenses for m in year.months)


def test_the_month_lists_what_each_recurring_payment_weighed(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
        *_months(CURRENT, "2025-06", 8, 10, "39.00", BASIC_FIT),
    )
    detail = real_cashflow_month(session, USER, master_key, "2025-12", today=TODAY)
    assert [(s.name, s.amount, s.count) for s in detail.recurring] == [
        ("EDF clients particuliers", Decimal("60.00"), 1), ("Basic Fit", Decimal("39.00"), 1),
    ]
    assert detail.totals.recurring == Decimal("99.00")


def test_what_is_fixed_and_what_is_still_due_this_month(session: Session, master_key: str):
    today = date(2026, 4, 9)
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF),
        *_months(CURRENT, "2025-08", 8, 10, "39.00", BASIC_FIT),
        # Ended long ago: neither fixed nor due.
        *_months(CURRENT, "2024-01", 8, 15, "9.99", "PRLV SEPA DEEZER"),
    )
    # Not booked yet: the fifth of April's debit, still expected; the tenth's
    # already on its way.
    _store(session, master_key, CURRENT, _raw("39.00", "DBIT", "2026-04-08", ref="pending-fit", status="PDNG", label=BASIC_FIT))
    _synced(session, master_key, CURRENT, today)

    assert real_cashflow_year(session, USER, master_key, 2026, today=today).running_recurring == Decimal("99.00")
    current = real_cashflow_current(session, USER, master_key, today=today)
    assert [(due.name, due.date, due.amount) for due in current.upcoming] == [
        ("EDF clients particuliers", date(2026, 4, 5), Decimal("60.00")),
    ]
    assert current.upcoming_amount == Decimal("60.00")


def test_an_account_not_synced_since_expects_nothing(session: Session, master_key: str):
    today = date(2026, 4, 2)
    _ops(session, master_key, *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF))
    _synced(session, master_key, CURRENT, date(2026, 3, 20))

    assert real_cashflow_year(session, USER, master_key, 2026, today=today).running_recurring == Decimal("0")
    assert real_cashflow_current(session, USER, master_key, today=today).upcoming == []


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def _listed(session: Session, master_key: str, today: date):
    return {item.name: item for item in list_recurring(session, USER, master_key, today=today).items}


def test_status_on_the_day_and_an_account_known_only_up_to_a_sync(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF))
    _synced(session, master_key, CURRENT, date(2026, 9, 18))
    # Last debit the 5th of March 2026: a week's grace past the 5th of April,
    # then late while two more due dates may be missed.
    for today, status in (
        (date(2026, 4, 12), "active"), (date(2026, 4, 13), "late"), (date(2026, 6, 10), "late"), (date(2026, 6, 11), "ended"),
    ):
        _synced(session, master_key, CURRENT, today)
        assert _listed(session, master_key, today)[EDF_NAME].status == status, today

    _synced(session, master_key, CURRENT, date(2026, 3, 20))
    stale = _listed(session, master_key, date(2026, 6, 11))[EDF_NAME]
    assert (stale.status, stale.covered_until) == ("stale", date(2026, 3, 20))


def test_a_first_debit_near_the_start_of_the_history_may_have_started_before(session: Session, master_key: str):
    _ops(
        session, master_key,
        (CURRENT, "2025-07-20", "12.00", "DBIT", "CARTE BOULANGERIE CB*0837"),
        *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF),
        *_months(CURRENT, "2025-11", 5, 10, "39.00", BASIC_FIT),
    )
    listed = _listed(session, master_key, date(2026, 4, 1))
    assert (listed[EDF_NAME].since_at_least, listed["Basic Fit"].since_at_least) == (True, False)


def test_costs_a_year_by_cadence_and_price(session: Session, master_key: str):
    _ops(
        session, master_key,
        *[(CURRENT, f"{date(2025, 9, 3) + timedelta(days=28 * k)}", "30.00", "DBIT", "PRLV SEPA FITNESS PARK") for k in range(8)],
        *_months(CURRENT, "2025-09", 8, 12, lambda k: ["40", "60", "50", "90", "70", "30", "45", "80"][k], "PRLV SEPA ENGIE"),
    )
    listed = _listed(session, master_key, date(2026, 4, 20))
    fitness, engie = listed["Fitness Park"], listed["Engie"]
    assert (fitness.cadence, fitness.annual_estimate, fitness.monthly_equivalent) == ("fourweekly", Decimal("390.00"), Decimal("32.50"))
    assert (engie.variable, engie.amount, engie.annual_estimate) == (True, Decimal("45"), Decimal("540.00"))


def test_price_changes_are_between_price_levels_only(session: Session, master_key: str):
    _ops(
        session, master_key,
        # A start-up fee, a discounted month, then the price.
        *[(CURRENT, f"{date(2026, 1, 7) + timedelta(days=28 * k)}", amount, "DBIT", "PRLV SEPA ARVERNE FITNESS")
          for k, amount in enumerate(["35.00", "10.00", "30.00", "30.00", "30.00", "30.00"])],
        *_months(CURRENT, "2025-06", 11, 5, lambda k: "60.00" if k < 8 else "90.00", EDF),
    )
    listed = _listed(session, master_key, date(2026, 5, 20))
    assert listed["Arverne Fitness"].price_changes == []
    [rise] = listed[EDF_NAME].price_changes
    assert (rise.date, rise.before, rise.after, rise.percent) == (date(2026, 2, 5), Decimal("60"), Decimal("90"), Decimal("50.0"))


def test_a_rename_and_the_refunds_are_listed_with_it(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-11", 4, 4, "4.02", "CARTE 04/11/25 AllSecur CB*0837"),
        *_months(CURRENT, "2026-03", 4, 4, "4.02", "CARTE 04/03/26 Allianz Direct CB*0837"),
        (CURRENT, "2026-05-20", "45.65", "CRDT", "VIR SEPA Allianz Direct REMBOURSEMENT"),
    )
    [item] = list_recurring(session, USER, master_key, today=date(2026, 7, 10)).items
    assert (item.name, item.occurrence_count) == ("Allianz Direct", 8)
    assert [(r.date, r.before, r.after) for r in item.renamed] == [(date(2026, 3, 4), "AllSecur", "Allianz Direct")]
    assert (item.refunds.total, [r.label for r in item.refunds.items]) == (
        Decimal("45.65"), ["VIR SEPA Allianz Direct REMBOURSEMENT"],
    )


def test_totals_hold_the_active_counted_recurring_payments_only(session: Session, master_key: str):
    today = date(2026, 4, 9)
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF),
        *_months(CURRENT, "2024-01", 8, 15, "9.99", "PRLV SEPA DEEZER"),
        *_months(CURRENT, "2026-01", 3, 2, "21.60", POCKET_MONEY),
        *_months(CURRENT, "2025-08", 8, 10, "39.00", BASIC_FIT),
    )
    _synced(session, master_key, CURRENT, today)
    basic_fit = next(tx for tx in _month_items(session, master_key, "2026-03") if tx.label == BASIC_FIT)
    decide(session, USER, master_key, basic_fit.id, RecurringDecisionKind.REFUSE)

    listed = list_recurring(session, USER, master_key, today=today)
    assert (listed.monthly_total, listed.annual_total) == (Decimal("60.00"), Decimal("720.00"))
    assert [(i.name, i.state, i.status) for i in listed.items] == [
        (EDF_NAME, "auto", "active"), ("Marie Dupont", "candidate", "active"),
        ("Deezer", "auto", "ended"), ("Basic Fit", "refused", "active"),
    ]


def test_a_refund_its_supplier_abbreviates_is_still_its_refund(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
        (CURRENT, "2025-08-29", "44.15", "CRDT", "VIR SEPA EDF CLT PART RBT"),
        # Another supplier's refund, whose label shares a word further in.
        (CURRENT, "2025-09-16", "18.22", "CRDT", "VIR SEPA TOTALENERGIES ELECTRICITE PARTICULIERS"),
    )
    [stored] = _recurring(session, master_key)
    assert [(m.amount, m.label) for m in stored.members if m.role == "refund"] == [
        (Decimal("44.15"), "VIR SEPA EDF CLT PART RBT"),
    ]


# ---------------------------------------------------------------------------
# Recurring income
# ---------------------------------------------------------------------------

SALARY = "VIR SEPA VILMORIN & CIE SALAIRE"
# A parent's monthly transfer, among others of any amount: asked, never counted.
ALLOWANCE = "VIR SEPA MME JEANNE MARTIN"


def _allowance(session: Session, master_key: str) -> None:
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-08", 8, 4, "250.00", ALLOWANCE, "CRDT"),
        (CURRENT, "2025-11-15", "80.00", "CRDT", ALLOWANCE),
        (CURRENT, "2026-01-20", "35.00", "CRDT", ALLOWANCE),
    )


def test_a_salary_is_recurring_income_counted_without_asking(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2025-08", 8, 28, "1380.71", SALARY, "CRDT"))

    [stored] = _recurring(session, master_key)
    assert (stored.direction, stored.state, stored.confidence, stored.counted) == ("income", "auto", "certain", True)
    # Listed with the income alone.
    assert list_recurring(session, USER, master_key, today=TODAY).items == []
    listed = list_recurring(session, USER, master_key, today=TODAY, direction=RecurringDirection.INCOME)
    [item] = listed.items
    assert (item.direction, item.amount) == (RecurringDirection.INCOME, Decimal("1380.71"))
    assert (listed.direction, listed.monthly_total) == (RecurringDirection.INCOME, Decimal("1380.71"))


def test_income_splits_into_recurring_and_one_off_as_the_ledger_adds_them(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-08", 8, 28, "1380.71", SALARY, "CRDT"),
        *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF),
        (CURRENT, "2026-03-12", "45.00", "CRDT", "VIR SEPA Leboncoin"),
    )

    detail = real_cashflow_month(session, USER, master_key, "2026-03", today=TODAY)
    totals = detail.totals
    assert (totals.income, totals.recurring_income, totals.one_off_income) == (
        Decimal("1425.71"), Decimal("1380.71"), Decimal("45.00"),
    )
    # The payments keep their own split.
    assert (totals.recurring, totals.one_off) == (Decimal("60.00"), Decimal("0.00"))
    assert [(s.amount, s.count) for s in detail.recurring_income] == [(Decimal("1380.71"), 1)]
    assert [s.amount for s in detail.recurring] == [Decimal("60.00")]

    ledger = build_ledger(session, USER, master_key)
    income_rows = [
        row for row in ledger.rows
        if row.recurring is not None and ledger.recurring[row.recurring].direction is RecurringDirection.INCOME
    ]
    assert sum(row.signed for row in income_rows if f"{row.day:%Y-%m}" == "2026-03") == totals.recurring_income
    year = real_cashflow_year(session, USER, master_key, 2026, today=TODAY)
    assert all(m.recurring_income + m.one_off_income == m.income for m in year.months)


def test_a_parent_s_allowance_asks_once_its_flow_question_is_answered(session: Session, master_key: str):
    _allowance(session, master_key)

    [stored] = _recurring(session, master_key)
    assert (stored.direction, stored.state, stored.question) == ("income", "candidate", False)

    last = next(tx for tx in _month_items(session, master_key, "2026-03") if tx.label == ALLOWANCE)
    set_transaction_type(session, USER, master_key, last.id, Type.INCOME, TypeScope.LABEL)

    [stored] = _recurring(session, master_key)
    assert stored.question
    carrier = next(tx for tx in _month_items(session, master_key, "2026-03") if tx.id == stored.carrier)
    assert carrier.recurring_question.direction is RecurringDirection.INCOME
    decided = decide(session, USER, master_key, stored.carrier, RecurringDecisionKind.CONFIRM)
    assert (decided.direction, decided.state) == (RecurringDirection.INCOME, RecurringState.CONFIRMED)
    tagged = next(tx for tx in _month_items(session, master_key, "2026-03") if tx.id == stored.carrier)
    assert tagged.recurring.direction is RecurringDirection.INCOME


def test_what_the_income_brings_and_what_is_still_expected_this_month(session: Session, master_key: str):
    today = date(2026, 4, 9)
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-08", 8, 28, "1380.71", SALARY, "CRDT"),
        *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF),
    )
    _synced(session, master_key, CURRENT, today)

    year = real_cashflow_year(session, USER, master_key, 2026, today=today)
    assert (year.running_recurring, year.running_recurring_income) == (Decimal("60.00"), Decimal("1380.71"))
    current = real_cashflow_current(session, USER, master_key, today=today)
    assert [(due.date, due.amount) for due in current.upcoming_income] == [(date(2026, 4, 28), Decimal("1380.71"))]
    assert current.upcoming_income_amount == Decimal("1380.71")
    # Expected income never joins what is still to be spent.
    assert [due.amount for due in current.upcoming] == [Decimal("60.00")]


def test_an_income_takes_the_natures_of_income_only(session: Session, master_key: str):
    _ops(session, master_key, *_months(CURRENT, "2025-08", 8, 28, "1380.71", SALARY, "CRDT"))
    [item] = list_recurring(session, USER, master_key, today=TODAY, direction=RecurringDirection.INCOME).items
    decided = decide(session, USER, master_key, item.transaction_id, RecurringDecisionKind.CONFIRM)

    assert update(session, USER, master_key, decided.id, {"nature": RecurringNature.SALARY}).nature is RecurringNature.SALARY
    with pytest.raises(NatureMismatchError):
        update(session, USER, master_key, decided.id, {"nature": RecurringNature.HOUSING})


def test_a_payment_and_an_income_are_never_merged(session: Session, master_key: str):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-08", 8, 28, "1380.71", SALARY, "CRDT"),
        *_months(CURRENT, "2025-08", 8, 5, "60.00", EDF),
    )
    [salary] = list_recurring(session, USER, master_key, today=TODAY, direction=RecurringDirection.INCOME).items
    [edf] = list_recurring(session, USER, master_key, today=TODAY).items
    decided = decide(session, USER, master_key, salary.transaction_id, RecurringDecisionKind.CONFIRM)

    with pytest.raises(DirectionMismatchError):
        merge(session, USER, master_key, decided.id, other_transaction_id=edf.transaction_id)


def test_a_refused_income_past_the_threshold_waits_to_be_sorted(session: Session, master_key: str):
    # « Pas un revenu récurrent » says nothing of how it counts: past the
    # threshold, its credits keep their flow question in « À trier ».
    _allowance(session, master_key)
    _ops(session, master_key, *_months(CURRENT, "2025-08", 8, 10, "9.00", "VIR SEPA MANGOPAY REMBOURSEMENT", "CRDT"))
    for stored in _recurring(session, master_key):
        decide(session, USER, master_key, stored.carrier, RecurringDecisionKind.REFUSE)

    assert {s.state for s in _recurring(session, master_key)} <= {"refused"}
    queue = review_queue(session, USER, master_key)
    flows = {q.transaction.label: q.operation_count for q in queue.questions if q.kind.value == "flow"}
    assert flows == {ALLOWANCE: 10}
