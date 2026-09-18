"""
The subscriptions of a user, derived on each rebuild of the transfer patterns:
the series `recurrence.py` finds in their debits, with what the user said of
them (`subscription_decisions.py`) laid over.

Called by `flows.transfer_patterns` with the operations it already read and
typed, so that this module reads no row and imports no reader: movements in,
stored subscriptions out.

A decision finds its series again by its anchors — the operations it covered
when decided — and failing that by its identity, when those operations were
imported again under other ids. A confirmed decision nothing matches any
longer grows its own series from its operations (`recurrence.seed_series`):
that is also how an operation the user marked by hand becomes a subscription.
The latest decision on a series wins; one decision may hold several series,
merged by the user.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from dtos.banking import BankTransferStatus, CashflowType, OperationType
from services.banking import recurrence
from services.banking.cashflow_types import CANCELLATIONS, TRANSFERS
from services.banking.label_groups import group_name
from services.banking.merchants import (
    Idf,
    MerchantKey,
    group_merchants,
    merchant_words,
    same_merchant,
)
from services.banking.recurrence import CADENCE, Confidence, RecurrenceOp, Series
from services.banking.subscription_decisions import CONFIRMED, REFUSED, Decision
from services.banking.transfer_patterns import (
    CANCELLED,
    EXTRA,
    MANUAL,
    REFUND,
    REGULAR,
    StoredSubscription,
    SubscriptionMember,
)
from services.encryption import hash_index

AUTO, CANDIDATE = "auto", "candidate"
# A decision found again by its identity: an amount within a quarter of the
# one recorded, since a price moves between an import and the next.
IDENTITY_AMOUNT_SHARE = Decimal("0.25")


class Movement(NamedTuple):
    """What the rebuild knows of one operation."""
    index: int
    uuid: str
    account: str
    period: str
    day: date | None
    # The card payment's own date when the bank gives it.
    paid_on: date | None
    amount: Decimal
    currency: str
    is_credit: bool
    is_final: bool
    label: str | None
    leg: BankTransferStatus | None
    type: CashflowType
    method: OperationType


@dataclass
class Derived:
    subscriptions: list[StoredSubscription] = field(default_factory=list)
    # "YYYY-MM" -> subscription questions carried by an operation of that month.
    questions: dict[str, int] = field(default_factory=dict)
    # Movement indexes of the credits refunding a counted subscription: their
    # flow question is asked whatever their amount (decision 4 of the plan).
    refunds: set[int] = field(default_factory=set)


@dataclass
class _Held:
    """A series and the decision laid over it, if any."""
    series: Series
    decision: Decision | None = None
    confidence: Confidence | None = None
    manual: list[RecurrenceOp] = field(default_factory=list)


def derive(
    movements: list[Movement],
    decisions: list[Decision],
    account_uuids: dict[str, str],
    asking: set[int],
    master_key: str,
) -> Derived:
    """`asking` holds the movement indexes whose flow question is still open:
    a series whose last debit is one of them asks nothing yet — its type may
    still change, and a transfer to oneself stops being a candidate once filed
    as saving."""
    debits, credits = _eligible(movements)
    keys = {m.index: merchant_words(m.label) for m in debits + credits}
    merchants = group_merchants(keys.values())
    idf = Idf(keys.values())
    ops = {m.index: _op(m, merchants[keys[m.index]]) for m in debits + credits}
    heads: dict[int, set[str]] = defaultdict(set)
    for key in keys.values():
        if not key[0].startswith("#"):
            heads[merchants[key]].add(key[0])
    by_uuid = {m.uuid: m for m in debits + credits}
    debit_ops = [ops[m.index] for m in debits]
    credit_ops = [ops[m.index] for m in credits]

    detection = recurrence.detect(debit_ops)
    held = [
        _Held(series, confidence=recurrence.confidence(series, recurrence.features(series, detection)))
        for series in detection.series
    ]
    refs = {hash_index(m.uuid, master_key): m.uuid for m in debits + credits}
    covered = {decision.uuid: {refs[ref] for ref in decision.anchors | decision.includes if ref in refs} for decision in decisions}
    _attach_by_anchors(held, decisions, covered)
    _attach_by_identity(held, decisions, keys, by_uuid, idf, account_uuids)
    held += _seeds(held, decisions, covered, ops, by_uuid, debit_ops)
    grouped = _merged(held)
    _corrections(grouped, decisions, ops, by_uuid, refs)

    derived = Derived()
    taken_refunds: set[str] = set()
    excluded = {refs[ref] for decision in decisions for ref in decision.excludes if ref in refs}
    for entry in grouped:
        stored = _stored(entry, keys, heads, by_uuid, account_uuids, credit_ops, taken_refunds, excluded, asking)
        if stored is None:
            continue
        derived.subscriptions.append(stored)
        if stored.question:
            period = by_uuid[stored.carrier].period
            derived.questions[period] = derived.questions.get(period, 0) + 1
        if stored.counted:
            derived.refunds.update(by_uuid[m.uuid].index for m in stored.members if m.role == REFUND)
    derived.subscriptions.sort(key=lambda s: (s.first, s.key))
    derived.questions = dict(sorted(derived.questions.items()))
    return derived


def annual_estimate(subscription: StoredSubscription) -> Decimal:
    """What it costs a year at its current price: a subscription billed every
    four weeks is paid thirteen times."""
    return subscription.amount * CADENCE[subscription.cadence].per_year


def occurrence_count(subscription: StoredSubscription) -> int:
    return sum(1 for member in subscription.members if member.role in (REGULAR, CANCELLED))


def _eligible(movements: list[Movement]) -> tuple[list[Movement], list[Movement]]:
    """Final debits outside internal transfers and cash withdrawals, a debit
    its refund or rejection cancelled included for its rhythm; unpaired final
    credits, for refunds."""
    debits, credits = [], []
    for m in movements:
        if not m.is_final or m.day is None or m.amount <= 0:
            continue
        if m.is_credit:
            if m.leg is None:
                credits.append(m)
        elif m.leg not in TRANSFERS and m.method is not OperationType.WITHDRAWAL:
            debits.append(m)
    return debits, credits


def _op(m: Movement, merchant: int) -> RecurrenceOp:
    return RecurrenceOp(
        m.uuid, m.account, m.paid_on or m.day, m.amount, m.currency, m.method, m.type,
        m.leg in CANCELLATIONS, merchant,
    )


def _attach_by_anchors(held: list[_Held], decisions: list[Decision], covered: dict[str, set[str]]) -> None:
    """Each series takes the decision sharing the most of its operations, the
    latest on a tie — unless the user set that decision's cadence and the
    series runs at another: an operation marked as a yearly charge is not the
    monthly series of purchases it happened to fall into."""
    for entry in held:
        ids = {op.id for op in entry.series.regular + entry.series.extras}
        best: tuple[int, int] | None = None
        for order, decision in enumerate(decisions):
            forced = CADENCE.get(decision.cadence) if decision.cadence else None
            if forced is not None and not recurrence.compatible(forced, entry.series.cadence):
                continue
            shared = len(ids & covered[decision.uuid])
            if shared and (best is None or (shared, order) > best):
                best, entry.decision = (shared, order), decision


def _attach_by_identity(
    held: list[_Held],
    decisions: list[Decision],
    keys: dict[int, MerchantKey],
    by_uuid: dict[str, Movement],
    idf: Idf,
    account_uuids: dict[str, str],
) -> None:
    """A decision none of whose operations is found any more — its account
    imported again — takes the series of the same merchant, on one of its
    accounts, at a compatible cadence and a nearby amount."""
    attached = {entry.decision.uuid for entry in held if entry.decision}
    for decision in decisions:
        if decision.uuid in attached:
            continue
        identity = decision.identity
        cadence = CADENCE.get(identity.cadence)
        for entry in held:
            series = entry.series
            if entry.decision is not None or cadence is None or not recurrence.compatible(cadence, series.cadence):
                continue
            accounts = {account_uuids.get(op.account) for op in series.regular}
            if not accounts & set(identity.accounts):
                continue
            amount = recurrence.current_amount(series)
            if abs(amount - identity.amount) > IDENTITY_AMOUNT_SHARE * identity.amount:
                continue
            if not same_merchant(tuple(identity.words), keys[by_uuid[series.last.id].index], idf):
                continue
            entry.decision = decision


def _seeds(
    held: list[_Held],
    decisions: list[Decision],
    covered: dict[str, set[str]],
    ops: dict[int, RecurrenceOp],
    by_uuid: dict[str, Movement],
    debit_ops: list[RecurrenceOp],
) -> list[_Held]:
    """A confirmed decision no series carries grows one from its latest
    operation: an operation marked by hand, or a series the detection no
    longer finds."""
    attached = {entry.decision.uuid for entry in held if entry.decision}
    in_series = {op.id for entry in held if entry.decision or entry.confidence for op in entry.series.regular + entry.series.extras}
    pool = [op for op in debit_ops if op.id not in in_series]
    seeds = []
    for decision in decisions:
        if decision.status != CONFIRMED or decision.uuid in attached:
            continue
        own = sorted(
            (ops[by_uuid[uuid].index] for uuid in covered[decision.uuid] if not by_uuid[uuid].is_credit),
            key=lambda op: (op.day, op.id),
        )
        if not own:
            continue
        cadence = CADENCE.get(decision.cadence or decision.identity.cadence)
        series = recurrence.seed_series(own[-1], pool, cadence)
        taken = {op.id for op in series.regular}
        pool = [op for op in pool if op.id not in taken]
        seeds.append(_Held(series, decision))
    return seeds


def _merged(held: list[_Held]) -> list[_Held]:
    """One entry per decision, its series merged; undecided series as found."""
    by_decision: dict[str, list[_Held]] = defaultdict(list)
    kept: list[_Held] = []
    for entry in held:
        if entry.decision is None:
            if entry.confidence is not None:
                kept.append(entry)
        else:
            by_decision[entry.decision.uuid].append(entry)
    for entries in by_decision.values():
        if len(entries) == 1:
            kept.append(entries[0])
            continue
        latest = max(entries, key=lambda e: (e.series.last.day, e.series.last.id))
        regular = {op.id: op for e in entries for op in e.series.regular}
        extras = {op.id: op for e in entries for op in e.series.extras if op.id not in regular}
        series = Series(
            latest.series.cadence, list(regular.values()), list(extras.values()),
            variable=any(e.series.variable for e in entries),
            merchants=set().union(*(e.series.merchants for e in entries)),
            links=[link for e in entries for link in e.series.links],
        )
        series.sort()
        confidences = [e.confidence for e in entries if e.confidence]
        kept.append(_Held(series, latest.decision, confidences[0] if confidences else None))
    return kept


def _corrections(
    entries: list[_Held], decisions: list[Decision], ops: dict[int, RecurrenceOp], by_uuid: dict[str, Movement],
    refs: dict[str, str],
) -> None:
    """What the user attached or detached by hand, over what was detected: an
    operation they attached belongs to their subscription alone."""
    claimed: dict[str, str] = {}
    for decision in decisions:
        for ref in decision.includes:
            if ref in refs:
                claimed[refs[ref]] = decision.uuid
    for entry in entries:
        decision = entry.decision
        own = decision.uuid if decision else None
        excluded = {refs[ref] for ref in decision.excludes if ref in refs} if decision else set()
        series = entry.series
        series.regular = [op for op in series.regular if op.id not in excluded and claimed.get(op.id, own) == own]
        series.extras = [op for op in series.extras if op.id not in excluded and claimed.get(op.id, own) == own]
        if decision is None:
            continue
        present = {op.id for op in series.regular + series.extras}
        entry.manual = sorted(
            (ops[by_uuid[uuid].index] for uuid, owner in claimed.items()
             if owner == decision.uuid and uuid not in present and uuid not in excluded),
            key=lambda op: (op.day, op.id),
        )
        if decision.cadence in CADENCE:
            series.cadence = CADENCE[decision.cadence]


def _stored(
    entry: _Held,
    keys: dict[int, MerchantKey],
    heads: dict[int, set[str]],
    by_uuid: dict[str, Movement],
    account_uuids: dict[str, str],
    credit_ops: list[RecurrenceOp],
    taken_refunds: set[str],
    excluded: set[str],
    asking: set[int],
) -> StoredSubscription | None:
    series, decision = entry.series, entry.decision
    manual_debits = [op for op in entry.manual if not by_uuid[op.id].is_credit]
    if not series.regular:
        if not manual_debits:
            return None
        series.regular = manual_debits
        manual_debits = []
    if decision is None:
        state = AUTO if entry.confidence is Confidence.CERTAIN else CANDIDATE
    else:
        state = CONFIRMED if decision.status == CONFIRMED else REFUSED
    counted = state in (AUTO, CONFIRMED)

    refunds = [
        op for op in recurrence.linked_refunds(series, credit_ops, _refunding(series, heads))
        if op.id not in taken_refunds and op.id not in excluded
    ] + [op for op in entry.manual if by_uuid[op.id].is_credit]
    taken_refunds.update(op.id for op in refunds)

    members = [
        _member(by_uuid[op.id], CANCELLED if op.cancelled else REGULAR) for op in series.regular
    ] + [_member(by_uuid[op.id], EXTRA) for op in series.extras] + [
        _member(by_uuid[op.id], MANUAL) for op in manual_debits
    ] + [_member(by_uuid[op.id], REFUND) for op in refunds]

    carrier = recurrence.carrier(series)
    question = (
        state == CANDIDATE and carrier is not None and by_uuid[carrier.id].index not in asking
    )
    name, renamed = _names(series, by_uuid)
    method = Counter(op.method for op in series.regular).most_common(1)[0][0]
    return StoredSubscription(
        key=decision.uuid if decision else series.first.id,
        decision=decision.uuid if decision else None,
        state=state,
        confidence=entry.confidence.value if entry.confidence else None,
        cadence=series.cadence.name,
        variable=series.variable,
        currency=series.currency,
        members=members,
        levels=[tuple(level) for level in recurrence.levels(series)],
        episodes=recurrence.episodes(series)[0],
        first=series.first.day,
        last=series.last.day,
        amount=recurrence.current_amount(series),
        name=(decision.name if decision else None) or name,
        renamed=renamed,
        accounts=sorted({account_uuids.get(op.account, op.account) for op in series.regular + series.extras}),
        last_account=series.last.account,
        method=method.value,
        carrier=carrier.id if carrier else None,
        question=question,
        counted=counted,
        words=list(keys[by_uuid[series.last.id].index]),
        ended_on=decision.ended_on if decision else None,
    )


def _refunding(series: Series, heads: dict[int, set[str]]) -> set[int]:
    """The merchants a refund of the series may come from: its own, and any
    whose label starts with the same word as one of theirs. A supplier writes
    its refunds its own way ("EDF CLT PART RBT" for "EDF clients
    particuliers"), too short to read as the same merchant, but it keeps its
    name first."""
    own = set().union(*(heads.get(m, set()) for m in series.merchants))
    return set(series.merchants) | {m for m, first in heads.items() if first & own}


def _member(m: Movement, role: str) -> SubscriptionMember:
    return SubscriptionMember(
        m.uuid, role, m.day, m.amount, m.is_credit, m.type is CashflowType.EXPENSE,
        m.label if role == REFUND else None,
    )


def _names(series: Series, by_uuid: dict[str, Movement]) -> tuple[str, list[tuple[date, str, str]]]:
    """The name its last debit's merchant goes by, and each time the series
    moved on to a merchant it had not been paid under before."""
    occurrences: dict[int, list[tuple[date | None, str | None]]] = defaultdict(list)
    order: list[int] = []
    for op in series.regular:
        if op.merchant not in occurrences:
            order.append(op.merchant)
        occurrences[op.merchant].append((by_uuid[op.id].day, by_uuid[op.id].label))
    name = {merchant: group_name(occurrences[merchant]) for merchant in order}
    renamed = []
    seen = {series.regular[0].merchant}
    for before, after in zip(series.regular, series.regular[1:]):
        if after.merchant not in seen:
            seen.add(after.merchant)
            renamed.append((by_uuid[after.id].day, name[before.merchant], name[after.merchant]))
    return name[series.last.merchant], renamed
