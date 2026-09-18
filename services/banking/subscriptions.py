"""
The subscriptions a user reads and corrects: the list, the operations of one,
and every decision and correction the routes take.

The subscriptions themselves are derived on each rebuild of the transfer
patterns (services/banking/subscription_series.py). Nothing here writes one:
a route writes a decision (services/banking/subscription_decisions.py), which
moves the digest the patterns are kept fresh by, and reads the subscription
back from the rebuild.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session

from dtos.banking import (
    BankSubscriptionEpisode,
    BankSubscriptionItem,
    BankSubscriptionPriceChange,
    BankSubscriptionRefund,
    BankSubscriptionRefunds,
    BankSubscriptionRename,
    BankSubscriptionsResponse,
    BankTransactionItem,
    BankTransferStatus,
    CashflowType,
    OperationType,
    SubscriptionCadence,
    SubscriptionDecisionKind,
    SubscriptionOperationAction,
    SubscriptionState,
    SubscriptionStatus,
)
from services.banking import recurrence
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
from services.banking.subscription_decisions import (
    CONFIRMED,
    REFUSED,
    Decision,
    Identity,
    delete_decision,
    get_decision,
    save_decision,
)
from services.banking.subscription_series import annual_estimate, occurrence_count
from services.banking.transfer_patterns import (
    CANCELLED,
    EXTRA,
    MANUAL,
    REFUND,
    REGULAR,
    StoredSubscription,
    TransferPatterns,
)
from services.encryption import decrypt_data, hash_index

_CENT = Decimal("0.01")
_PERCENT = Decimal("0.1")
_PAID = frozenset({REGULAR, EXTRA, MANUAL})


class NoSubscriptionError(LookupError):
    """The operation belongs to no subscription."""


class NotAnExpenseError(ValueError):
    """Only a final debit counted as an expense, outside a pair, can be marked."""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def list_subscriptions(
    session: Session, user_uuid: str, master_key: str, today: date | None = None
) -> BankSubscriptionsResponse:
    """Every subscription found or decided: the active ones first, the most
    costly first among them, then the late ones, the candidates, the ended
    ones (the latest first) and the refused ones.

    Read off the stored patterns alone: no operation is loaded."""
    today = today or date.today()
    accounts = _user_accounts(session, user_uuid, master_key)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    reader = _Reader(session, user_uuid, master_key, accounts, patterns, today)
    items = [reader.item(subscription) for subscription in patterns.subscriptions]

    counted = [
        item for item in items
        if item.state in (SubscriptionState.AUTO, SubscriptionState.CONFIRMED)
        and item.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.STALE)
    ]
    currencies = [item.currency for item in counted] or [item.currency for item in items]
    currency = max(sorted(set(currencies)), key=currencies.count) if currencies else "EUR"
    monthly = sum((item.monthly_equivalent for item in counted if item.currency == currency), Decimal("0"))
    return BankSubscriptionsResponse(
        currency=currency,
        monthly_total=monthly,
        annual_total=sum((item.annual_estimate for item in counted if item.currency == currency), Decimal("0")),
        items=sorted(items, key=_rank),
    )


def active_counted(
    session: Session, user_uuid: str, master_key: str, accounts: _Accounts, patterns: TransferPatterns, today: date
) -> list[tuple[StoredSubscription, BankSubscriptionItem]]:
    """The counted subscriptions still running on `today`: what the real
    cashflow calls fixed, and whose next due dates it expects."""
    reader = _Reader(session, user_uuid, master_key, accounts, patterns, today)
    return [
        (subscription, item)
        for subscription in patterns.subscriptions if subscription.counted
        for item in [reader.item(subscription)]
        if item.status is SubscriptionStatus.ACTIVE
    ]


def _rank(item: BankSubscriptionItem) -> tuple:
    if item.state is SubscriptionState.REFUSED:
        return (4, -item.last_date.toordinal())
    if item.state is SubscriptionState.CANDIDATE:
        return (2, -item.monthly_equivalent, item.key)
    if item.status is SubscriptionStatus.ENDED:
        return (3, -item.last_date.toordinal(), item.key)
    if item.status is SubscriptionStatus.LATE:
        return (1, -item.monthly_equivalent, item.key)
    return (0, -item.monthly_equivalent, item.key)


class _Reader:
    """Turns stored subscriptions into what the list shows, on a given day."""

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

    def item(self, subscription: StoredSubscription) -> BankSubscriptionItem:
        cadence = CADENCE[subscription.cadence]
        covered_until = self.covered_until.get(subscription.last_account)
        status = recurrence.status_at(cadence, subscription.last, self.today, covered_until)
        if subscription.ended_on and subscription.ended_on < self.today and subscription.last <= subscription.ended_on:
            status = recurrence.Status.ENDED
        annual = annual_estimate(subscription)
        year_ago = self.today - timedelta(days=365)
        refunds = [member for member in subscription.members if member.role == REFUND]
        due = [member for member in subscription.members if member.role in (REGULAR, CANCELLED)]
        starts = [
            self.patterns.coverage[self.bidx[account]][0] for account in subscription.accounts
            if account in self.bidx and self.bidx[account] in self.patterns.coverage
        ]
        return BankSubscriptionItem(
            id=subscription.decision,
            key=subscription.key,
            transaction_id=subscription.carrier or max(due, key=lambda m: m.day).uuid,
            name=subscription.name,
            state=subscription.state,
            confidence=subscription.confidence,
            status=status.value,
            covered_until=covered_until if status is recurrence.Status.STALE else None,
            cadence=subscription.cadence,
            variable=subscription.variable,
            amount=subscription.amount,
            currency=subscription.currency,
            monthly_equivalent=(annual / 12).quantize(_CENT),
            annual_estimate=annual.quantize(_CENT),
            paid_last_12_months=sum(
                (m.amount for m in subscription.members if m.role in _PAID and m.day > year_ago), Decimal("0"),
            ),
            first_date=subscription.first,
            since_at_least=bool(starts) and (subscription.first - min(starts)).days < cadence.nominal,
            last_date=subscription.last,
            next_date=recurrence.advance(cadence, subscription.last),
            occurrence_count=occurrence_count(subscription),
            extra_count=sum(1 for m in subscription.members if m.role == EXTRA),
            accounts=[self.names.get(account, account) for account in subscription.accounts],
            payment_method=OperationType(subscription.method),
            price_changes=[
                BankSubscriptionPriceChange(
                    date=after[0], before=before[2], after=after[2],
                    percent=((after[2] - before[2]) * 100 / before[2]).quantize(_PERCENT),
                )
                for before, after in zip(subscription.levels, subscription.levels[1:])
            ],
            episodes=[BankSubscriptionEpisode(start=start, end=end) for start, end in subscription.episodes],
            renamed=[BankSubscriptionRename(date=day, before=a, after=b) for day, a, b in subscription.renamed],
            refunds=BankSubscriptionRefunds(
                total=sum((m.amount for m in refunds), Decimal("0")),
                items=[BankSubscriptionRefund(id=m.uuid, date=m.day, amount=m.amount, label=m.label) for m in refunds],
            ),
            ended_on=subscription.ended_on,
        )


def subscription_operations(
    session: Session,
    user_uuid: str,
    master_key: str,
    subscription_id: str | None = None,
    transaction_id: str | None = None,
) -> list[BankTransactionItem]:
    """The operations of one subscription, the latest first: by its decision,
    or by any of its operations for one never decided. Read over the whole
    history in one pass, as the subscriptions were found."""
    accounts = _user_accounts(session, user_uuid, master_key)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    if subscription_id is not None:
        get_decision(session, user_uuid, master_key, subscription_id)
        subscription = next((s for s in patterns.subscriptions if s.decision == subscription_id), None)
        if subscription is None:
            return []
    else:
        _readable_row(session, accounts, transaction_id)
        found = patterns.subscription_of(transaction_id)
        if found is None:
            raise NoSubscriptionError(transaction_id)
        subscription = found[0]

    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    filing = _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs)
    item = _item_builder(movements, transfer_legs, accounts, filing)
    members = {member.uuid for member in subscription.members}
    return [item(index) for index in reversed(range(len(movements))) if movements[index].row.uuid in members]


# ---------------------------------------------------------------------------
# Decisions and corrections
# ---------------------------------------------------------------------------


def decide(
    session: Session, user_uuid: str, master_key: str, transaction_id: str, kind: SubscriptionDecisionKind,
    name: str | None = None,
) -> BankSubscriptionItem | None:
    """Say yes or no to the subscription an operation belongs to. A decision
    already on it is replaced, keeping what it held."""
    accounts = _user_accounts(session, user_uuid, master_key)
    _readable_row(session, accounts, transaction_id)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    found = patterns.subscription_of(transaction_id)
    if found is None:
        raise NoSubscriptionError(transaction_id)
    decision = _decided(session, user_uuid, master_key, found[0])
    decision.status = CONFIRMED if kind is SubscriptionDecisionKind.CONFIRM else REFUSED
    if name:
        decision.name = name
    save_decision(session, user_uuid, master_key, decision)
    return _read_back(session, user_uuid, master_key, decision.uuid)


def mark(
    session: Session, user_uuid: str, master_key: str, transaction_id: str,
    cadence: SubscriptionCadence | None = None, name: str | None = None,
) -> BankSubscriptionItem | None:
    """Make a subscription of an operation the detection left out. The rebuild
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
    found = patterns.subscription_of(transaction_id)
    ref = hash_index(transaction_id, master_key)
    if found is not None:
        decision = _decided(session, user_uuid, master_key, found[0])
        decision.status = CONFIRMED
    else:
        decision = Decision(
            uuid=str(uuid.uuid4()), status=CONFIRMED, anchors=frozenset({ref}), includes=frozenset({ref}),
            identity=Identity(
                merchant_words(current.label), (current.account_id,), (cadence or SubscriptionCadence.MONTHLY).value,
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
) -> BankSubscriptionItem | None:
    """Rename it, force its cadence, or say when it was ended: `changes` holds
    only the fields sent, None clearing one."""
    _, decision = get_decision(session, user_uuid, master_key, decision_id)
    if "name" in changes:
        decision.name = changes["name"] or None
    if "cadence" in changes:
        decision.cadence = changes["cadence"].value if changes["cadence"] else None
    if "ended_on" in changes:
        decision.ended_on = changes["ended_on"]
    save_decision(session, user_uuid, master_key, decision)
    return _read_back(session, user_uuid, master_key, decision.uuid)


def correct(
    session: Session, user_uuid: str, master_key: str, decision_id: str, transaction_id: str,
    action: SubscriptionOperationAction,
) -> BankSubscriptionItem | None:
    """Attach an operation the detection missed, or detach one it took: over
    whatever the detection finds, now and after."""
    accounts = _user_accounts(session, user_uuid, master_key)
    _readable_row(session, accounts, transaction_id)
    _, decision = get_decision(session, user_uuid, master_key, decision_id)
    ref = hash_index(transaction_id, master_key)
    if action is SubscriptionOperationAction.INCLUDE:
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
) -> BankSubscriptionItem | None:
    """One subscription of two: a contract that changed hands (Orange then
    Bouygues), a series the detection cut. The other's decision goes."""
    _, decision = get_decision(session, user_uuid, master_key, decision_id)
    if other_id is None:
        accounts = _user_accounts(session, user_uuid, master_key)
        _readable_row(session, accounts, other_transaction_id)
        found = transfer_patterns(session, user_uuid, master_key, accounts).subscription_of(other_transaction_id)
        if found is None:
            raise NoSubscriptionError(other_transaction_id)
        other_subscription = found[0]
        if other_subscription.decision is None:
            decision.anchors = decision.anchors | _occurrences(other_subscription, master_key)
        else:
            other_id = other_subscription.decision
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


def _decided(session: Session, user_uuid: str, master_key: str, subscription: StoredSubscription) -> Decision:
    """The decision already on a subscription, its current operations added to
    its anchors; else a new one recording what the subscription is now."""
    anchors = _occurrences(subscription, master_key)
    if subscription.decision is not None:
        _, decision = get_decision(session, user_uuid, master_key, subscription.decision)
        decision.anchors = decision.anchors | anchors
        return decision
    return Decision(
        uuid=str(uuid.uuid4()), status=CONFIRMED, anchors=anchors,
        identity=Identity(
            tuple(subscription.words), tuple(subscription.accounts), subscription.cadence,
            subscription.amount, subscription.method,
        ),
    )


def _occurrences(subscription: StoredSubscription, master_key: str) -> frozenset[str]:
    return frozenset(hash_index(m.uuid, master_key) for m in subscription.members if m.role != REFUND)


def _read_back(session: Session, user_uuid: str, master_key: str, decision_id: str) -> BankSubscriptionItem | None:
    """The subscription a decision holds once the patterns are rebuilt; None
    when it holds none (every operation it named is gone)."""
    accounts = _user_accounts(session, user_uuid, master_key)
    patterns = transfer_patterns(session, user_uuid, master_key, accounts)
    subscription = next((s for s in patterns.subscriptions if s.decision == decision_id), None)
    if subscription is None:
        return None
    return _Reader(session, user_uuid, master_key, accounts, patterns, date.today()).item(subscription)
