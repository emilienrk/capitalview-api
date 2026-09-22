"""
The recurring payments a user reads and corrects: the list, the operations of one,
and every decision and correction the routes take.

The recurring payments themselves are derived on each rebuild of the transfer
patterns (services/banking/recurring_series.py). Nothing here writes one:
a route writes a decision (services/banking/recurring_decisions.py), which
moves the digest the patterns are kept fresh by, and reads the recurring payment
back from the rebuild.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session

from dtos.banking import (
    BankRecurringEpisode,
    BankRecurringItem,
    BankRecurringPriceChange,
    BankRecurringRefund,
    BankRecurringRefunds,
    BankRecurringRename,
    BankRecurringResponse,
    BankRecurringYearPaid,
    BankTransactionItem,
    BankTransferStatus,
    CashflowType,
    OperationType,
    RecurringCadence,
    RecurringDecisionKind,
    RecurringOperationAction,
    RecurringState,
    RecurringStatus,
)
from services.banking import natures, recurrence
from services.banking.flows import (
    _Accounts,
    _filing,
    _internal_transfer_legs,
    _item_builder,
    _links,
    _load_movements,
    _pairing,
    _readable_row,
    _transaction_item,
    _user_accounts,
    transfer_patterns,
)
from services.banking.merchants import merchant_words
from services.banking.recurrence import CADENCE
from services.banking.recurring_decisions import (
    CONFIRMED,
    REFUSED,
    Decision,
    Identity,
    delete_decision,
    get_decision,
    save_decision,
)
from services.banking.recurring_series import annual_estimate, occurrence_count
from services.banking.transfer_patterns import (
    CANCELLED,
    EXTRA,
    MANUAL,
    REFUND,
    REGULAR,
    StoredRecurring,
    TransferPatterns,
)
from services.encryption import decrypt_data, hash_index

_CENT = Decimal("0.01")
_PERCENT = Decimal("0.1")
_PAID = frozenset({REGULAR, EXTRA, MANUAL})


class NoRecurringError(LookupError):
    """The operation belongs to no recurring payment."""


class NotAnExpenseError(ValueError):
    """Only a final debit counted as an expense, outside a pair, can be marked."""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def list_recurring(
    session: Session, user_uuid: str, master_key: str, today: date | None = None
) -> BankRecurringResponse:
    """Every recurring payment found or decided: the active ones first, the most
    costly first among them, then the late ones, the candidates, the ended
    ones (the latest first) and the refused ones.

    Read off the stored patterns alone: no operation is loaded."""
    today = today or date.today()
    accounts = _user_accounts(session, user_uuid, master_key)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    reader = _Reader(session, user_uuid, master_key, accounts, patterns, today)
    items = [reader.item(stored) for stored in patterns.recurring]

    counted = [
        item for item in items
        if item.state in (RecurringState.AUTO, RecurringState.CONFIRMED)
        and item.status in (RecurringStatus.ACTIVE, RecurringStatus.STALE)
    ]
    currencies = [item.currency for item in counted] or [item.currency for item in items]
    currency = max(sorted(set(currencies)), key=currencies.count) if currencies else "EUR"
    monthly = sum((item.monthly_equivalent for item in counted if item.currency == currency), Decimal("0"))
    return BankRecurringResponse(
        currency=currency,
        monthly_total=monthly,
        annual_total=sum((item.annual_estimate for item in counted if item.currency == currency), Decimal("0")),
        items=sorted(items, key=_rank),
    )


def active_counted(
    session: Session, user_uuid: str, master_key: str, accounts: _Accounts, patterns: TransferPatterns, today: date
) -> list[tuple[StoredRecurring, BankRecurringItem]]:
    """The counted recurring payments still running on `today`: what the real
    cashflow calls fixed, and whose next due dates it expects."""
    reader = _Reader(session, user_uuid, master_key, accounts, patterns, today)
    return [
        (stored, item)
        for stored in patterns.recurring if stored.counted
        for item in [reader.item(stored)]
        if item.status is RecurringStatus.ACTIVE
    ]


def _rank(item: BankRecurringItem) -> tuple:
    if item.state is RecurringState.REFUSED:
        return (4, -item.last_date.toordinal())
    if item.state is RecurringState.CANDIDATE:
        return (2, -item.monthly_equivalent, item.key)
    if item.status is RecurringStatus.ENDED:
        return (3, -item.last_date.toordinal(), item.key)
    if item.status is RecurringStatus.LATE:
        return (1, -item.monthly_equivalent, item.key)
    return (0, -item.monthly_equivalent, item.key)


def _by_year(stored: StoredRecurring) -> list[BankRecurringYearPaid]:
    """What it took each year, oldest first: the same debits as
    `paid_last_12_months`, cut by calendar year."""
    years: dict[int, Decimal] = {}
    for member in stored.members:
        if member.role in _PAID:
            years[member.day.year] = years.get(member.day.year, Decimal("0")) + member.amount
    return [BankRecurringYearPaid(year=year, amount=amount) for year, amount in sorted(years.items())]


class _Reader:
    """Turns stored recurring payments into what the list shows, on a given day."""

    def __init__(
        self, session: Session, user_uuid: str, master_key: str, accounts: _Accounts,
        patterns: TransferPatterns, today: date,
    ):
        self.today = today
        self.patterns = patterns
        self.names = {account.uuid: decrypt_data(account.name_enc, master_key) for account in accounts.by_bidx.values()}
        self.bidx = {account.uuid: bidx for bidx, account in accounts.by_bidx.items()}
        links = _links(session, user_uuid, master_key)
        self.covered_until = {
            bidx: links.get(bidx, last) for bidx, (_, last) in patterns.coverage.items()
        }

    def item(self, stored: StoredRecurring) -> BankRecurringItem:
        cadence = CADENCE[stored.cadence]
        covered_until = self.covered_until.get(stored.last_account)
        status = recurrence.status_at(cadence, stored.last, self.today, covered_until)
        if stored.ended_on and stored.ended_on < self.today and stored.last <= stored.ended_on:
            status = recurrence.Status.ENDED
        annual = annual_estimate(stored)
        nature = natures.of(stored.nature)
        year_ago = self.today - timedelta(days=365)
        refunds = [member for member in stored.members if member.role == REFUND]
        due = [member for member in stored.members if member.role in (REGULAR, CANCELLED)]
        starts = [
            self.patterns.coverage[self.bidx[account]][0] for account in stored.accounts
            if account in self.bidx and self.bidx[account] in self.patterns.coverage
        ]
        return BankRecurringItem(
            id=stored.decision,
            key=stored.key,
            transaction_id=stored.carrier or max(due, key=lambda m: m.day).uuid,
            name=stored.name,
            nature=nature,
            state=stored.state,
            confidence=stored.confidence,
            status=status.value,
            covered_until=covered_until if status is recurrence.Status.STALE else None,
            cadence=stored.cadence,
            variable=stored.variable,
            amount=stored.amount,
            currency=stored.currency,
            monthly_equivalent=(annual / 12).quantize(_CENT),
            annual_estimate=annual.quantize(_CENT),
            paid_last_12_months=sum(
                (m.amount for m in stored.members if m.role in _PAID and m.day > year_ago), Decimal("0"),
            ),
            paid_by_year=_by_year(stored),
            first_date=stored.first,
            since_at_least=bool(starts) and (stored.first - min(starts)).days < cadence.nominal,
            last_date=stored.last,
            next_date=recurrence.advance(cadence, stored.last),
            occurrence_count=occurrence_count(stored),
            extra_count=sum(1 for m in stored.members if m.role == EXTRA),
            accounts=[self.names.get(account, account) for account in stored.accounts],
            payment_method=OperationType(stored.method),
            price_changes=[
                BankRecurringPriceChange(
                    date=after[0], before=before[2], after=after[2],
                    percent=((after[2] - before[2]) * 100 / before[2]).quantize(_PERCENT),
                )
                for before, after in zip(stored.levels, stored.levels[1:])
            ],
            episodes=[BankRecurringEpisode(start=start, end=end) for start, end in stored.episodes],
            renamed=[BankRecurringRename(date=day, before=a, after=b) for day, a, b in stored.renamed],
            refunds=BankRecurringRefunds(
                total=sum((m.amount for m in refunds), Decimal("0")),
                items=[BankRecurringRefund(id=m.uuid, date=m.day, amount=m.amount, label=m.label) for m in refunds],
            ),
            ended_on=stored.ended_on,
        )


def recurring_operations(
    session: Session,
    user_uuid: str,
    master_key: str,
    recurring_id: str | None = None,
    transaction_id: str | None = None,
) -> list[BankTransactionItem]:
    """The operations of one recurring payment, the latest first: by its decision,
    or by any of its operations for one never decided. Read over the whole
    history in one pass, as the recurring payments were found."""
    accounts = _user_accounts(session, user_uuid, master_key)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    if recurring_id is not None:
        get_decision(session, user_uuid, master_key, recurring_id)
        stored = next((s for s in patterns.recurring if s.decision == recurring_id), None)
        if stored is None:
            return []
    else:
        _readable_row(session, accounts, transaction_id)
        found = patterns.recurring_of(transaction_id)
        if found is None:
            raise NoRecurringError(transaction_id)
        stored = found[0]

    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    filing = _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs)
    item = _item_builder(movements, transfer_legs, accounts, filing)
    members = {member.uuid for member in stored.members}
    return [item(index) for index in reversed(range(len(movements))) if movements[index].row.uuid in members]


# ---------------------------------------------------------------------------
# Decisions and corrections
# ---------------------------------------------------------------------------


def decide(
    session: Session, user_uuid: str, master_key: str, transaction_id: str, kind: RecurringDecisionKind,
    name: str | None = None,
) -> BankRecurringItem | None:
    """Say yes or no to the recurring payment an operation belongs to. A decision
    already on it is replaced, keeping what it held."""
    accounts = _user_accounts(session, user_uuid, master_key)
    _readable_row(session, accounts, transaction_id)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    found = patterns.recurring_of(transaction_id)
    if found is None:
        raise NoRecurringError(transaction_id)
    decision = _decided(session, user_uuid, master_key, found[0])
    decision.status = CONFIRMED if kind is RecurringDecisionKind.CONFIRM else REFUSED
    if name:
        decision.name = name
    save_decision(session, user_uuid, master_key, decision)
    return _read_back(session, user_uuid, master_key, decision.uuid)


def mark(
    session: Session, user_uuid: str, master_key: str, transaction_id: str,
    cadence: RecurringCadence | None = None, name: str | None = None,
) -> BankRecurringItem | None:
    """Make a recurring payment of an operation the detection left out. The rebuild
    grows its series from it: the debits of its merchant and account at each
    due date, however few — one is enough for a yearly charge seen once."""
    accounts = _user_accounts(session, user_uuid, master_key)
    row = _readable_row(session, accounts, transaction_id)
    current = _transaction_item(session, user_uuid, master_key, accounts, row)
    if (
        current.is_credit or current.is_pending or current.cashflow_type is not CashflowType.EXPENSE
        or current.transfer_status not in (None, BankTransferStatus.SUGGESTED)
    ):
        raise NotAnExpenseError(transaction_id)

    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    found = patterns.recurring_of(transaction_id)
    ref = hash_index(transaction_id, master_key)
    if found is not None:
        decision = _decided(session, user_uuid, master_key, found[0])
        decision.status = CONFIRMED
    else:
        decision = Decision(
            uuid=str(uuid.uuid4()), status=CONFIRMED, anchors=frozenset({ref}), includes=frozenset({ref}),
            identity=Identity(
                merchant_words(current.label), (current.account_id,), (cadence or RecurringCadence.MONTHLY).value,
                current.amount, current.operation_type.value,
            ),
        )
    if cadence is not None:
        decision.cadence = cadence.value
    if name:
        decision.name = name
    save_decision(session, user_uuid, master_key, decision)
    return _read_back(session, user_uuid, master_key, decision.uuid)


def update(
    session: Session, user_uuid: str, master_key: str, decision_id: str, changes: dict,
) -> BankRecurringItem | None:
    """Rename it, force its cadence, say what it is for or when it was ended:
    `changes` holds only the fields sent, None clearing one."""
    _, decision = get_decision(session, user_uuid, master_key, decision_id)
    if "name" in changes:
        decision.name = changes["name"] or None
    if "cadence" in changes:
        decision.cadence = changes["cadence"].value if changes["cadence"] else None
    if "nature" in changes:
        decision.nature = changes["nature"].value if changes["nature"] else None
    if "ended_on" in changes:
        decision.ended_on = changes["ended_on"]
    save_decision(session, user_uuid, master_key, decision)
    return _read_back(session, user_uuid, master_key, decision.uuid)


def correct(
    session: Session, user_uuid: str, master_key: str, decision_id: str, transaction_id: str,
    action: RecurringOperationAction,
) -> BankRecurringItem | None:
    """Attach an operation the detection missed, or detach one it took: over
    whatever the detection finds, now and after."""
    accounts = _user_accounts(session, user_uuid, master_key)
    _readable_row(session, accounts, transaction_id)
    _, decision = get_decision(session, user_uuid, master_key, decision_id)
    ref = hash_index(transaction_id, master_key)
    if action is RecurringOperationAction.INCLUDE:
        decision.includes = decision.includes | {ref}
        decision.excludes = decision.excludes - {ref}
    else:
        decision.excludes = decision.excludes | {ref}
        decision.includes = decision.includes - {ref}
        decision.anchors = decision.anchors - {ref}
    save_decision(session, user_uuid, master_key, decision)
    return _read_back(session, user_uuid, master_key, decision.uuid)


def merge(
    session: Session, user_uuid: str, master_key: str, decision_id: str,
    other_id: str | None = None, other_transaction_id: str | None = None,
) -> BankRecurringItem | None:
    """One recurring payment of two: a contract that changed hands (Orange then
    Bouygues), a series the detection cut. The other's decision goes."""
    _, decision = get_decision(session, user_uuid, master_key, decision_id)
    if other_id is None:
        accounts = _user_accounts(session, user_uuid, master_key)
        _readable_row(session, accounts, other_transaction_id)
        found = transfer_patterns(session, user_uuid, master_key, accounts).recurring_of(other_transaction_id)
        if found is None:
            raise NoRecurringError(other_transaction_id)
        other_recurring = found[0]
        if other_recurring.decision is None:
            decision.anchors = decision.anchors | _occurrences(other_recurring, master_key)
        else:
            other_id = other_recurring.decision
    if other_id is not None and other_id != decision.uuid:
        _, other = get_decision(session, user_uuid, master_key, other_id)
        decision.anchors = decision.anchors | other.anchors
        decision.includes = decision.includes | other.includes
        decision.excludes = (decision.excludes | other.excludes) - decision.includes
        delete_decision(session, user_uuid, master_key, other_id)
    save_decision(session, user_uuid, master_key, decision)
    return _read_back(session, user_uuid, master_key, decision.uuid)


def forget(session: Session, user_uuid: str, master_key: str, decision_id: str) -> None:
    """Drop a decision: its series is found and asked about again."""
    delete_decision(session, user_uuid, master_key, decision_id)


def _decided(session: Session, user_uuid: str, master_key: str, stored: StoredRecurring) -> Decision:
    """The decision already on a recurring payment, its current operations added to
    its anchors; else a new one recording what the recurring payment is now."""
    anchors = _occurrences(stored, master_key)
    if stored.decision is not None:
        _, decision = get_decision(session, user_uuid, master_key, stored.decision)
        decision.anchors = decision.anchors | anchors
        return decision
    return Decision(
        uuid=str(uuid.uuid4()), status=CONFIRMED, anchors=anchors,
        identity=Identity(
            tuple(stored.words), tuple(stored.accounts), stored.cadence,
            stored.amount, stored.method,
        ),
    )


def _occurrences(stored: StoredRecurring, master_key: str) -> frozenset[str]:
    return frozenset(hash_index(m.uuid, master_key) for m in stored.members if m.role != REFUND)


def _read_back(session: Session, user_uuid: str, master_key: str, decision_id: str) -> BankRecurringItem | None:
    """The recurring payment a decision holds once the patterns are rebuilt; None
    when it holds none (every operation it named is gone)."""
    accounts = _user_accounts(session, user_uuid, master_key)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    stored = next((s for s in patterns.recurring if s.decision == decision_id), None)
    if stored is None:
        return None
    return _Reader(session, user_uuid, master_key, accounts, patterns, date.today()).item(stored)
