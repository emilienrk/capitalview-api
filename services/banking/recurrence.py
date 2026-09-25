"""
Money that comes back: the recurring payments in a user's debits and the
recurring income in their credits, whatever moves under them — a price, a
missed month, a pause, a refund, a rejected debit, a new label, a new account,
a new means of payment.

Pure: operations in, series out. Nothing here reads a label (the merchant comes
from `merchants.py`) nor a type rule (each operation carries its resolved type).
Debits and credits run through the same layers, apart: a series is one or the
other (its `kind`), and only how sure it is reads differently.

Layers, each catching what the one before lets through:

1. streams per merchant: the best chain of dates per cadence, flat amounts
   first (a recurring payment hidden among a shop's purchases), any amount then;
2. stitching inside a merchant: a price change, a pause; a weak stream
   overlapping a strong one is its irregular part;
3. renames across merchants: the same amount, to the cent, carried on at the
   next due date on the same account;
4. extras: a prorata, a regularisation, a late debit at a merchant the series
   has to itself;
5. reading: how sure, what it costs now, whether it still runs.

Measured on the real dump of 2026-09-18 (15 recurring payments, no false one) and
on 29 synthetic edge cases, all kept as tests. Income measured on the dump of
2026-09-23: 440 credits, 56 chains, the salaries, allowances and family
transfers kept, the friends paying back left out.
"""

from __future__ import annotations

import calendar
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from statistics import median
from typing import NamedTuple

from dtos.banking import CashflowType, OperationType

# ---------------------------------------------------------------------------
# Cadences
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cadence:
    name: str
    days: int | None
    months: int | None
    # Days a due date may slip either way.
    tolerance: int
    # Due dates in a row that may go unpaid without ending an episode.
    max_missed: int
    min_points: int
    per_year: int
    # Favours the common cadences when two fit as well: a monthly charge also
    # reads as a bimonthly one on every other debit.
    prior: float = 0.0

    @property
    def nominal(self) -> float:
        return float(self.days) if self.days else self.months * 30.44


CADENCES = (
    Cadence("weekly", 7, None, 2, 2, 6, 52),
    Cadence("biweekly", 14, None, 3, 2, 5, 26),
    Cadence("fourweekly", 28, None, 3, 2, 5, 13),
    Cadence("monthly", None, 1, 6, 2, 2, 12, 0.6),
    Cadence("bimonthly", None, 2, 8, 1, 3, 6),
    Cadence("quarterly", None, 3, 10, 1, 3, 4, 0.1),
    Cadence("semiannual", None, 6, 15, 1, 2, 2),
    Cadence("annual", None, 12, 20, 1, 2, 1, 0.3),
)
CADENCE = {cadence.name: cadence for cadence in CADENCES}
MONTHLY, FOURWEEKLY = CADENCE["monthly"], CADENCE["fourweekly"]


def add_months(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def advance(cadence: Cadence, day: date, count: int = 1) -> date:
    if cadence.days:
        return day + timedelta(days=cadence.days * count)
    return add_months(day, cadence.months * count)


def month_coordinate(day: date) -> float:
    """Months since year 0, the day as a fraction of its month: from the 31st
    of January to the 28th of February is exactly one month."""
    days = calendar.monthrange(day.year, day.month)[1]
    return day.year * 12 + day.month - 1 + (day.day - 1) / days


# ---------------------------------------------------------------------------
# Input and output
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class RecurrenceOp:
    """One final debit or credit."""
    id: str
    account: str
    # The card payment's own date when the bank gives it, else the booking one.
    day: date
    amount: Decimal
    currency: str
    method: OperationType
    type: CashflowType
    # Its refund or its rejection is paired with it: it keeps the rhythm and
    # never counts for an amount.
    cancelled: bool
    merchant: int
    ordinal: int = field(init=False)
    month: float = field(init=False)
    value: float = field(init=False)

    def __post_init__(self) -> None:
        self.ordinal = self.day.toordinal()
        self.month = month_coordinate(self.day)
        self.value = float(self.amount)


class Link(NamedTuple):
    """Where a series was stitched, and why."""
    kind: str  # price | gap | pause | rename | rename_once
    day: date


@dataclass(eq=False)
class Series:
    cadence: Cadence
    regular: list[RecurrenceOp]
    extras: list[RecurrenceOp] = field(default_factory=list)
    variable: bool = False
    merchants: set[int] = field(default_factory=set)
    links: list[Link] = field(default_factory=list)
    # EXPENSE for recurring payments, INCOME for recurring income: the type its
    # operations must carry to count.
    kind: CashflowType = CashflowType.EXPENSE

    @property
    def first(self) -> RecurrenceOp:
        return self.regular[0]

    @property
    def last(self) -> RecurrenceOp:
        return self.regular[-1]

    @property
    def currency(self) -> str:
        return self.regular[0].currency

    def sort(self) -> None:
        self.regular.sort(key=_by_day)
        self.extras.sort(key=_by_day)


def _by_day(op: RecurrenceOp) -> tuple[date, str]:
    return op.day, op.id


# ---------------------------------------------------------------------------
# Amounts and steps
# ---------------------------------------------------------------------------

# One price, give or take the exchange rate of a recurring payment billed in
# another currency.
FLAT_SHARE, FLAT_MIN = 0.03, 0.10
# One price, a cent's rounding apart.
CLOSE_SHARE, CLOSE_MIN = 0.01, 0.05
CENT = Decimal("0.01")


def flat(a: float, b: float) -> bool:
    return abs(a - b) <= max(FLAT_SHARE * max(a, b), FLAT_MIN)


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(CLOSE_SHARE * max(a, b), CLOSE_MIN)


def exact(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= CENT


def is_round(amount: Decimal) -> bool:
    return amount == amount.to_integral_value()


def step(cadence: Cadence, a: RecurrenceOp, b: RecurrenceOp) -> tuple[int, float] | None:
    """How many due dates from `a` to `b`, and by how many days the second
    misses its own; None when it falls on none, or too many were skipped."""
    gap = b.ordinal - a.ordinal
    if gap <= 0:
        return None
    if cadence.days:
        slots = max(1, round(gap / cadence.days))
        residual = abs(gap - slots * cadence.days)
    else:
        span = (b.month - a.month) / cadence.months
        slots = max(1, round(span))
        residual = abs(span - slots) * cadence.months * 30.44
    if slots > 1 + cadence.max_missed or residual > cadence.tolerance + (slots - 1) * max(1, cadence.tolerance // 2):
        return None
    return slots, residual


def _slots(cadence: Cadence, a: date, b: date) -> float:
    return (b - a).days / cadence.nominal


def _step_penalty(a: float, b: float) -> float:
    if flat(a, b):
        return 0.0
    return 0.25 + 0.5 * min(1.0, abs(math.log(b / a)) / math.log(2))


# ---------------------------------------------------------------------------
# Streams in one merchant
# ---------------------------------------------------------------------------

SKIP_PENALTY = 0.35
RESIDUAL_PENALTY = 0.4
FLAT_AMOUNT_PENALTY = 0.2
# A merchant debited this many times per due date cannot be one stream at that
# cadence (a canteen, a bakery), once it holds enough debits to say so.
DENSE_PER_SLOT = 2.5
DENSE_MIN_POINTS = 30
# Four gaps of 27 to 29 days in a row: a gym billing every four weeks, not
# monthly. Fewer points never say it: two dates 56 days apart are that by
# chance far more often.
FOURWEEKLY_RUN = 4


def _best_chain(points: list[RecurrenceOp], cadence: Cadence, flat_only: bool) -> tuple[float, list[RecurrenceOp]]:
    """The best-scored chain of points at this cadence: a point per due date,
    missed ones and slipped dates penalised, and changes of amount."""
    horizon = (2 + cadence.max_missed) * cadence.nominal + cadence.tolerance
    best: list[tuple[float, int]] = [(1.0, -1)] * len(points)
    for j in range(len(points)):
        best_j = (1.0, -1)
        for i in range(j - 1, -1, -1):
            if points[j].ordinal - points[i].ordinal > horizon:
                break
            stepped = step(cadence, points[i], points[j])
            if stepped is None:
                continue
            a, b = points[i].value, points[j].value
            if flat_only and not flat(a, b):
                continue
            slots, residual = stepped
            score = best[i][0] + 1.0 - SKIP_PENALTY * (slots - 1) - RESIDUAL_PENALTY * (residual / (cadence.tolerance + 1)) ** 2
            score -= FLAT_AMOUNT_PENALTY * abs(a - b) / b if flat_only else _step_penalty(a, b)
            if score > best_j[0]:
                best_j = (score, i)
        best[j] = best_j
    j = max(range(len(points)), key=lambda k: (best[k][0], -k))
    score, chain = best[j][0], []
    while j != -1:
        chain.append(points[j])
        j = best[j][1]
    return score, chain[::-1]


def _too_dense(points: list[RecurrenceOp], cadence: Cadence) -> bool:
    if len(points) < DENSE_MIN_POINTS:
        return False
    return len(points) / ((points[-1].ordinal - points[0].ordinal) / cadence.nominal + 1) > DENSE_PER_SLOT


def fourweekly_or(cadence: Cadence, chain: list[RecurrenceOp]) -> Cadence:
    if cadence is not MONTHLY:
        return cadence
    run = best_run = 0
    for a, b in zip(chain, chain[1:]):
        run = run + 1 if 27 <= b.ordinal - a.ordinal <= 29 else 0
        best_run = max(best_run, run)
    return FOURWEEKLY if best_run >= FOURWEEKLY_RUN else cadence


def _extract(points: list[RecurrenceOp], flat_only: bool, min_len: int) -> list[tuple[Cadence, list[RecurrenceOp]]]:
    """Streams, best first: the best chain across cadences, its points taken
    out, again."""
    remaining, streams = list(points), []
    while len(remaining) >= 2:
        best = None
        span = remaining[-1].ordinal - remaining[0].ordinal
        for cadence in CADENCES:
            needed = max(min_len, cadence.min_points)
            if len(remaining) < needed or _too_dense(remaining, cadence) or span < cadence.nominal * 0.5:
                continue
            score, chain = _best_chain(remaining, cadence, flat_only)
            if len(chain) >= needed and (best is None or score + cadence.prior > best[0]):
                best = (score + cadence.prior, cadence, chain)
        if best is None:
            break
        _, cadence, chain = best
        streams.append((fourweekly_or(cadence, chain), chain))
        taken = {op.id for op in chain}
        remaining = [op for op in remaining if op.id not in taken]
    return streams


def _is_variable(chain: list[RecurrenceOp]) -> bool:
    pairs = list(zip(chain, chain[1:]))
    return len(chain) >= 3 and sum(flat(a.value, b.value) for a, b in pairs) < 0.5 * len(pairs)


def compatible(a: Cadence, b: Cadence) -> bool:
    return a is b or (a.name in _MONTH_LIKE and b.name in _MONTH_LIKE)


_MONTH_LIKE = frozenset({"monthly", "fourweekly"})


def _family(method: OperationType) -> str:
    """How sure a means of payment can make a series: a mandate, a transfer,
    or anything else (a card, or a bank that names none)."""
    if method is OperationType.DIRECT_DEBIT:
        return "direct_debit"
    if method is OperationType.TRANSFER:
        return "transfer"
    return "card"


# A merchant is the series' own when its regular debits are most of what it
# was paid over the series' span: only then are its other debits the series'
# (a prorata, a regularisation) rather than purchases at a shop that also
# bills a recurring payment.
DEDICATED_SHARE = 0.75


def _dedicated(series: Series, merchant_ops: list[RecurrenceOp]) -> bool:
    ordinals = sorted(op.ordinal for op in merchant_ops)
    pad = int(series.cadence.nominal)
    around = bisect_right(ordinals, series.last.ordinal + pad) - bisect_left(ordinals, series.first.ordinal - pad)
    return len(series.regular) >= DEDICATED_SHARE * around


def _merchant_series(merchant: int, points: list[RecurrenceOp], kind: CashflowType) -> list[Series]:
    streams = [Series(c, chain, merchants={merchant}, kind=kind) for c, chain in _extract(points, True, 3)]
    taken = {op.id for s in streams for op in s.regular}
    for cadence, chain in _extract([op for op in points if op.id not in taken], False, 2):
        streams.append(Series(cadence, chain, merchants={merchant}, variable=_is_variable(chain), kind=kind))

    # One after the other: a price change, a gap, a pause.
    streams.sort(key=lambda s: (s.first.day, s.first.id))
    merged: list[Series] = []
    for stream in streams:
        host = None
        for candidate in merged:
            if not compatible(candidate.cadence, stream.cadence) or stream.first.day <= candidate.last.day:
                continue
            if any(op.day > stream.first.day for op in candidate.regular):
                continue
            gap = _slots(candidate.cadence, candidate.last.day, stream.first.day)
            near = gap <= 1 + candidate.cadence.max_missed + 0.5
            if near or flat(candidate.last.value, stream.first.value) or candidate.variable or stream.variable:
                if host is None or candidate.last.day > host.last.day:
                    host = candidate
        if host is None:
            merged.append(stream)
            continue
        gap = _slots(host.cadence, host.last.day, stream.first.day)
        if gap > 1 + host.cadence.max_missed + 0.5:
            kind = "pause"
        elif flat(host.last.value, stream.first.value):
            kind = "gap"
        else:
            kind = "price"
        host.links.append(Link(kind, stream.first.day))
        host.regular += stream.regular
        host.variable = host.variable or stream.variable
        if host.cadence is not stream.cadence and len(stream.regular) > len(host.regular) / 2:
            host.cadence = fourweekly_or(MONTHLY, sorted(host.regular, key=_by_day))

    # A weak stream overlapping a stronger one is its irregular part, unless it
    # is a clean fixed stream of its own: a second recurring payment of one merchant.
    merged.sort(key=lambda s: (-len(s.regular), s.first.day, s.first.id))
    kept: list[Series] = []
    for stream in merged:
        stream.sort()
        clean = not stream.variable and sum(
            exact(a.amount, b.amount) for a, b in zip(stream.regular, stream.regular[1:])
        ) >= 1
        host = next(
            (k for k in kept if k.first.day <= stream.last.day and stream.first.day <= k.last.day), None
        )
        if host is not None and not (clean and len(stream.regular) >= 2) and _dedicated(host, points):
            host.extras += [op for op in stream.regular if op.type is kind]
            continue
        kept.append(stream)
    return kept


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# A single debit under a new name takes over a series only from a merchant
# seen this rarely: a shop visited often is not a renamed recurring payment.
RENAME_ONCE_MAX_DEBITS = 2
# Extras sit between a tenth and four times the series' usual amount.
EXTRA_MIN_SHARE, EXTRA_MAX_SHARE = Decimal("0.1"), Decimal("4")


@dataclass
class Detection:
    series: list[Series]
    # (currency, merchant) -> its debits, by day.
    by_merchant: dict[tuple[str, int], list[RecurrenceOp]]
    # Series id -> the regular debits of the other clean fixed series of its
    # merchants: a second recurring payment billed under one label is not a shop
    # visited in between.
    others: dict[int, set[str]] = field(default_factory=dict)

    def merchant_ops(self, series: Series) -> list[RecurrenceOp]:
        return [op for m in sorted(series.merchants) for op in self.by_merchant.get((series.currency, m), [])]


def detect(ops: Iterable[RecurrenceOp], kind: CashflowType = CashflowType.EXPENSE) -> Detection:
    """Every series among the operations — debits for EXPENSE, credits for
    INCOME — in a fixed order whatever order they come in. Currencies never
    mix: two amounts in two currencies do not compare."""
    by_currency: dict[str, list[RecurrenceOp]] = defaultdict(list)
    for op in ops:
        by_currency[op.currency].append(op)
    series: list[Series] = []
    by_merchant: dict[tuple[str, int], list[RecurrenceOp]] = {}
    for currency in sorted(by_currency):
        ordered = sorted(by_currency[currency], key=lambda o: (o.day, o.account, o.amount, o.id))
        found, merchants = _detect_currency(ordered, kind)
        series += found
        by_merchant.update({(currency, m): points for m, points in merchants.items()})
    series.sort(key=lambda s: (s.first.day, s.first.id))
    detection = Detection(series, by_merchant)
    detection.others = _clean_neighbours(series)
    return detection


def _detect_currency(
    debits: list[RecurrenceOp], kind: CashflowType,
) -> tuple[list[Series], dict[int, list[RecurrenceOp]]]:
    by_merchant: dict[int, list[RecurrenceOp]] = defaultdict(list)
    for op in debits:
        by_merchant[op.merchant].append(op)

    series: list[Series] = []
    for merchant in sorted(by_merchant):
        points = by_merchant[merchant]
        if len(points) >= 2:
            series += _merchant_series(merchant, points, kind)
    for s in series:
        s.sort()
        if kind is CashflowType.INCOME:
            _drop_scattered_start(s, by_merchant[s.first.merchant])
        s.cadence = fourweekly_or(s.cadence, s.regular)

    _hand_offs(series)
    used = {op.id for s in series for op in s.regular + s.extras}
    _renamed_once(series, debits, by_merchant, used)
    _extras(series, by_merchant, used)
    return series, dict(by_merchant)


def _drop_scattered_start(series: Series, merchant_ops: list[RecurrenceOp]) -> None:
    """An income from a payer who also sends other amounts starts at its first
    steady amount: before it, the chain only strung that payer's one-off
    transfers together, dating a parent's monthly allowance years too early.
    A payer the series has to itself keeps its first payments, however
    uneven: a first salary paid for part of a month."""
    regular = series.regular
    start = _steady_start(regular, series.cadence)
    if not start:
        return
    head = regular[:start]
    around = sum(1 for op in merchant_ops if head[0].ordinal <= op.ordinal < regular[start].ordinal)
    if len(head) >= DEDICATED_SHARE * around:
        return
    series.regular = regular[start:]
    series.variable = _is_variable(series.regular)


def _steady_start(regular: list[RecurrenceOp], cadence: Cadence) -> int:
    """Where the first run of three amounts equal to the cent starts from
    which the rest stays steady, taking in the amounts of its level before it
    and a pair equal to the cent one due date before that (two months at 400
    before a rise to 650). Within 3 %, a
    few dozen random amounts hold a pair by chance, and a run of three or
    four now and then; to the cent, never — and an allowance repeats to the
    cent. 0 when no run holds."""
    alike = [flat(a.value, b.value) for a, b in zip(regular, regular[1:])]
    same = [exact(a.amount, b.amount) for a, b in zip(regular, regular[1:])]
    start = next((
        n for n in range(len(same) - 1)
        if same[n] and same[n + 1] and sum(alike[n:]) >= INCOME_FLAT_SHARE * len(alike[n:])
    ), 0)
    # Amounts before it within 3 % are its level, rounded otherwise (a salary
    # at 1 380,99 then 1 380,71).
    while start and alike[start - 1]:
        start -= 1
    if start >= 2 and same[start - 2] and _slots(cadence, regular[start - 1].day, regular[start].day) <= 1.5:
        start -= 2
    return start


def _near(amount: Decimal) -> tuple[Decimal, ...]:
    """The amounts `exact` takes for this one."""
    return amount - CENT, amount, amount + CENT


def _hand_offs(series: list[Series]) -> None:
    """A merchant renamed: the same amount, to the cent, carried on at the next
    due date, on the same account and the same kind of payment."""
    series.sort(key=lambda s: (s.first.day, s.first.id))
    # Looked up by how a series starts rather than compared pair by pair:
    # a real history holds a few hundred series.
    starts: dict[tuple[str, str, Decimal], list[Series]] = defaultdict(list)
    for s in series:
        starts[(s.first.account, _family(s.first.method), s.first.amount)].append(s)
    changed = True
    while changed:
        changed = False
        position = {id(s): n for n, s in enumerate(series)}
        for before in series:
            if before.variable or len(before.regular) < 2 or not exact(before.regular[-2].amount, before.last.amount):
                continue
            last = before.last
            candidates = sorted(
                (after for amount in _near(last.amount) for after in starts.get((last.account, _family(last.method), amount), ())),
                key=lambda after: position[id(after)],
            )
            for after in candidates:
                if after is before or before.merchants & after.merchants or not compatible(before.cadence, after.cadence):
                    continue
                if after.first.day <= before.last.day:
                    continue
                if any(op.day >= after.first.day for op in before.regular):
                    continue
                if _slots(before.cadence, before.last.day, after.first.day) > 1 + before.cadence.max_missed + 0.3:
                    continue
                before.links.append(Link("rename", after.first.day))
                before.regular += after.regular
                before.extras += after.extras
                before.merchants |= after.merchants
                before.variable = before.variable or after.variable
                before.sort()
                series.remove(after)
                starts[(after.first.account, _family(after.first.method), after.first.amount)].remove(after)
                changed = True
                break
            if changed:
                break


def _renamed_once(
    series: list[Series], debits: list[RecurrenceOp], by_merchant: dict[int, list[RecurrenceOp]], used: set[str]
) -> None:
    """The same, seen only once so far under its new name: the only debit of
    that amount at the next due date."""
    by_payment: dict[tuple[str, OperationType, Decimal], list[RecurrenceOp]] = defaultdict(list)
    for op in debits:
        by_payment[(op.account, op.method, op.amount)].append(op)
    position = {op.id: n for n, op in enumerate(debits)}
    for s in series:
        s.sort()
        if len(s.regular) < 3:
            continue
        if not s.variable and not exact(s.regular[-2].amount, s.last.amount):
            continue
        # A variable series meets a round amount by chance.
        if s.variable and is_round(s.last.amount):
            continue
        successors = []
        last = s.last
        candidates = sorted(
            (op for amount in _near(last.amount) for op in by_payment.get((last.account, last.method, amount), ())),
            key=lambda op: position[op.id],
        )
        for op in candidates:
            if op.id in used or op.merchant in s.merchants or len(by_merchant[op.merchant]) > RENAME_ONCE_MAX_DEBITS:
                continue
            stepped = step(s.cadence, s.last, op)
            if stepped is not None and stepped[0] == 1:
                successors.append(op)
        if len(successors) == 1:
            [successor] = successors
            s.links.append(Link("rename_once", successor.day))
            s.regular.append(successor)
            s.merchants.add(successor.merchant)
            used.add(successor.id)


def _extras(series: list[Series], by_merchant: dict[int, list[RecurrenceOp]], used: set[str]) -> None:
    """A first card payment, a prorata, a regularisation, a debit billed twice,
    at a merchant the series has to itself."""
    for s in series:
        merchant_ops = [op for m in sorted(s.merchants) for op in by_merchant[m]]
        if not _dedicated(s, merchant_ops):
            continue
        c = s.cadence
        usual = median(op.amount for op in s.regular)
        low = s.first.day - timedelta(days=int(c.nominal + c.tolerance))
        high = s.last.day + timedelta(days=int((1 + c.max_missed) * c.nominal + c.tolerance))
        for op in merchant_ops:
            if op.id in used or op.type is not s.kind:
                continue
            if low <= op.day <= high and EXTRA_MIN_SHARE * usual <= op.amount <= EXTRA_MAX_SHARE * usual:
                s.extras.append(op)
                used.add(op.id)
        s.sort()


# Two steps in three to the cent: a series of fixed price.
CLEAN_EXACT_SHARE = 0.6


def _clean_neighbours(series: list[Series]) -> dict[int, set[str]]:
    clean: list[Series] = []
    for s in series:
        pairs = list(zip(s.regular, s.regular[1:]))
        if not s.variable and len(s.regular) >= 3 and sum(exact(a.amount, b.amount) for a, b in pairs) >= CLEAN_EXACT_SHARE * len(pairs):
            clean.append(s)
    return {
        id(s): {op.id for other in clean if other is not s and other.currency == s.currency and other.merchants & s.merchants for op in other.regular}
        for s in series
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class Confidence(str, Enum):
    # Counted without asking, whatever pays it.
    CERTAIN = "certain"
    # Counted without asking too, unless a habit could look the same (`counted_unasked`).
    PROBABLE = "probable"


class Status(str, Enum):
    ACTIVE = "active"
    LATE = "late"
    ENDED = "ended"
    # The account is known only up to a day before the next due date: an
    # account nobody synced does not end its recurring payments.
    STALE = "stale"


class Level(NamedTuple):
    start: date
    end: date
    amount: Decimal
    count: int


@dataclass(frozen=True)
class Features:
    count: int
    extras: int
    # Debits over due dates within the episodes.
    coverage: float
    # Median days a debit misses its due date by.
    deviation: float
    flat: float
    exact: float
    method: OperationType
    family: str
    amount: Decimal
    round: bool
    # Regular debits over the merchant's debits across the series' span.
    exclusive: float
    # The share typed as the series' kind.
    typed: float
    cancelled: float


def carrier(series: Series) -> RecurrenceOp | None:
    """The debit a question sits on: the last one not cancelled."""
    return next((op for op in reversed(series.regular) if not op.cancelled), None)


def episodes(series: Series) -> tuple[list[tuple[date, date]], int]:
    """The runs without a pause, and how many due dates they span."""
    c, runs, start = series.cadence, [], series.regular[0]
    expected = 1
    for a, b in zip(series.regular, series.regular[1:]):
        gap = _slots(c, a.day, b.day)
        if gap > 1 + c.max_missed + 0.5:
            runs.append((start.day, a.day))
            start = b
            expected += 1
        else:
            expected += max(1, round(gap))
    runs.append((start.day, series.regular[-1].day))
    return runs, expected


# A fixed price repeats to the cent between its changes (EDF on 89 % of its
# steps, MACIF and its five changes on 59 %); an amount following an exchange
# rate almost never does. Below this share, the series floats and its levels
# take the wider tolerance. Measured on a jitter of ±1.5 %: counting the steps
# within 1 % instead cut half of the draws into two to four levels.
FLOATING_EXACT_SHARE = 0.5


def levels(series: Series) -> list[Level]:
    """Runs of one price, oldest first, a run's amount being its latest. A
    single odd amount between two runs is not a level: a prorata, a
    regularisation."""
    paid = [op for op in series.regular if not op.cancelled]
    if not paid:
        return []
    pairs = list(zip(paid, paid[1:]))
    to_the_cent = sum(exact(a.amount, b.amount) for a, b in pairs)
    same = flat if to_the_cent < FLOATING_EXACT_SHARE * len(pairs) else close
    runs: list[Level] = []
    for op in paid:
        if runs and same(float(runs[-1].amount), op.value):
            runs[-1] = Level(runs[-1].start, op.day, op.amount, runs[-1].count + 1)
        else:
            runs.append(Level(op.day, op.day, op.amount, 1))
    return [run for run in runs if run.count >= 2] or runs[-1:]


def current_amount(series: Series) -> Decimal:
    """The last level's price; for a variable series, the median of its last
    three debits."""
    paid = [op for op in series.regular if not op.cancelled]
    if not paid:
        return series.last.amount
    if series.variable:
        return Decimal(median(op.amount for op in paid[-3:]))
    return levels(series)[-1].amount


def annual_estimate(series: Series) -> Decimal:
    return current_amount(series) * series.cadence.per_year


def next_date(series: Series) -> date:
    return advance(series.cadence, series.last.day)


def status(series: Series, today: date, covered_until: date | None) -> Status:
    return status_at(series.cadence, series.last.day, today, covered_until)


# An account known up to three days ago is up to date: a bank books a debit a
# day or two after it is made.
STALE_AFTER_DAYS = 3


def status_at(cadence: Cadence, last: date, today: date, covered_until: date | None) -> Status:
    """Read on the day, never stored. `covered_until` is the last day the
    account of the last debit is known complete: its last sync when linked,
    its last operation when imported."""
    due = advance(cadence, last)
    grace = timedelta(days=max(5, int(0.25 * cadence.nominal)))
    if (
        covered_until is not None and covered_until < today - timedelta(days=STALE_AFTER_DAYS)
        and due + grace > covered_until
    ):
        return Status.STALE
    if today <= due + grace:
        return Status.ACTIVE
    if today <= last + timedelta(days=int((1 + cadence.max_missed) * cadence.nominal + cadence.tolerance)):
        return Status.LATE
    return Status.ENDED


def features(series: Series, detection: Detection) -> Features:
    c, regular = series.cadence, series.regular
    _, expected = episodes(series)
    residuals = [stepped[1] for a, b in zip(regular, regular[1:]) if (stepped := step(c, a, b)) is not None]
    pairs = list(zip(regular, regular[1:]))
    method = Counter(op.method for op in regular).most_common(1)[0][0]
    others = detection.others.get(id(series), set())
    span = [
        op for op in detection.merchant_ops(series)
        if regular[0].day <= op.day <= regular[-1].day and op.id not in others
    ]
    types = Counter(op.type for op in regular + series.extras)
    amount = current_amount(series)
    return Features(
        count=len(regular),
        extras=len(series.extras),
        coverage=min(1.0, len(regular) / expected),
        deviation=median(residuals) if residuals else 0.0,
        flat=sum(flat(a.value, b.value) for a, b in pairs) / max(len(pairs), 1),
        exact=sum(exact(a.amount, b.amount) for a, b in pairs) / max(len(pairs), 1),
        method=method,
        family=_family(method),
        amount=amount,
        round=is_round(amount),
        exclusive=len(regular) / max(len(span), 1),
        typed=types[series.kind] / sum(types.values()),
        cancelled=sum(op.cancelled for op in regular) / len(regular),
    )


# Transfers to a friend: chains of three or four alike amounts formed by
# chance held a quarter of the transfers over their span. The real rent held
# three quarters.
TRANSFER_MIN_EXCLUSIVE = 0.5


def confidence(series: Series, f: Features) -> Confidence | None:
    """How sure the series is a recurring payment, or a recurring income, by
    what the means of payment can prove; None when it is not offered at all."""
    if series.kind is CashflowType.INCOME:
        return _income_confidence(series, f)
    c, n = series.cadence, f.count
    last = carrier(series)
    # Only an expense is a recurring payment; a debit refunded every time is not one.
    if f.cancelled >= 0.5 or f.typed < 0.5 or last is None or last.type is not CashflowType.EXPENSE:
        return None
    if c.name in ("annual", "semiannual") and n == 2:
        a, b = series.regular[0].amount, series.regular[1].amount
        if f.family == "direct_debit":
            # A premium revised within a tenth.
            ok = abs(a - b) <= Decimal("0.10") * max(a, b) and f.deviation <= 15 and f.exclusive >= 0.5
        else:
            ok = exact(a, b) and not f.round and f.amount >= 10 and f.deviation <= 7 and f.exclusive >= 0.7
        return Confidence.PROBABLE if ok else None
    if f.family == "direct_debit":
        # A mandate: the amount may vary. Four or five instalments then nothing
        # is never counted unasked.
        if n >= 6 and f.coverage >= 0.75 and f.deviation <= 3:
            return Confidence.CERTAIN
        if n >= 2 and n + f.extras >= 3 and f.coverage >= 0.6 and f.deviation <= 5:
            return Confidence.PROBABLE
        return None
    if f.family == "transfer":
        # Rent, pocket money and savings elsewhere look alike: always asked.
        # And paid to someone the user also sends other amounts to, a few
        # alike ones fall into step by chance.
        if series.variable or f.flat < 0.75 or f.exclusive < TRANSFER_MIN_EXCLUSIVE:
            return None
        if n >= 3 and f.coverage >= 0.6 and f.deviation <= 5:
            return Confidence.PROBABLE
        return None
    # A card, or a bank that names no means of payment: habits look regular too.
    if c.name in ("weekly", "biweekly"):
        return None
    if series.variable:
        ok = c in (MONTHLY, FOURWEEKLY) and f.exclusive >= 0.75 and f.coverage >= 0.8 and (
            (n >= 4 and f.deviation <= 2.5) or (n >= 3 and f.deviation <= 1)
        )
        return Confidence.PROBABLE if ok else None
    if not (f.exact >= 0.6 or (f.flat >= 0.9 and not f.round)):
        return None
    if f.deviation > 3 or f.coverage < 0.6 or f.exclusive < 0.7:
        return None
    needed = 2 if c.name in ("annual", "semiannual") and f.amount >= 10 and not f.round else 3
    if f.amount < 2:
        needed = max(needed, 6)
    if f.round:
        # Bets, top-ups, tickets: round amounts come back by habit.
        needed = max(needed, 5)
        if f.deviation > 1:
            return None
    if n < needed:
        return None
    if n >= 12 and f.exact >= 0.9 and f.deviation <= 2 and f.coverage >= 0.9 and f.exclusive >= 0.9:
        return Confidence.CERTAIN
    return Confidence.PROBABLE


# Income from a payer who also sends other amounts: chains of alike transfers
# from friends paying back held a third of their credits over the span, a
# parent's monthly allowance two thirds.
INCOME_MIN_EXCLUSIVE = 0.5
# Three steps in four alike: an income of one amount, whatever its raises.
INCOME_FLAT_SHARE = 0.75
# Fewer steady payments than this must repeat to the cent.
INCOME_SHORT = 5
# A salary moving with hours, bonuses or a first partial month is told from a
# friend paying back by its payer, who pays nothing else, by its steps — most
# within a quarter of the one before (8 in 10 on the real one), where a
# friend's amounts jump by half or double (none in 5) — and by its length:
# five of a friend's in a row fell within a quarter once in 200 draws.
INCOME_VARIABLE_MIN_COUNT = 6
INCOME_VARIABLE_MIN_EXCLUSIVE = 0.75
INCOME_STEP_SHARE, INCOME_STEP_RATIO = 0.7, 1.25


def _income_confidence(series: Series, f: Features) -> Confidence | None:
    """Income is nearly always a transfer, which a mandate never vouches for:
    a salary, an allowance and a friend paying back a loan all look alike. So
    it rests on the amounts and on the payer paying nothing else; certain only
    over half a year, hardly a due date missed."""
    c, n = series.cadence, f.count
    last = carrier(series)
    if f.cancelled >= 0.5 or f.typed < 0.5 or last is None or last.type is not CashflowType.INCOME:
        return None
    if f.method is OperationType.INTEREST:
        # The bank's own interest: no habit looks like it.
        return Confidence.CERTAIN if f.deviation <= 7 else None
    if c.name in ("weekly", "biweekly"):
        return None
    if n < 3 or f.coverage < 0.6 or f.exclusive < INCOME_MIN_EXCLUSIVE:
        return None
    if f.amount < 10 and n < 6:
        return None
    # Judged on the steps rather than `series.variable`, which a stitched
    # series keeps from any stream it took in.
    if f.flat >= INCOME_FLAT_SHARE:
        if f.deviation > 5:
            return None
        # Within 3 %, a few of a friend's amounts fall in step by chance; to
        # the cent they never do, and a salary, an allowance or a parent's
        # transfer repeats to the cent.
        if n < INCOME_SHORT and f.exact < 0.5:
            return None
        if n >= 6 and f.coverage >= 0.9 and f.deviation <= 3 and f.exclusive >= 0.9:
            return Confidence.CERTAIN
        return Confidence.PROBABLE
    paid = [op.value for op in series.regular if not op.cancelled]
    steps = list(zip(paid, paid[1:]))
    small = sum(max(a, b) <= INCOME_STEP_RATIO * min(a, b) for a, b in steps)
    ok = (
        c in (MONTHLY, FOURWEEKLY) and n >= INCOME_VARIABLE_MIN_COUNT and f.coverage >= 0.75 and f.deviation <= 3
        and f.exclusive >= INCOME_VARIABLE_MIN_EXCLUSIVE and small >= INCOME_STEP_SHARE * len(steps)
    )
    return Confidence.PROBABLE if ok else None


def counted_unasked(series: Series, level: Confidence | None, f: Features) -> bool:
    """Whether a series is counted without a question: a certain one, a probable
    mandate, a probable card payment of a steady amount. The rest asks, being
    what a habit looks like too: a transfer (rent, pocket money and savings
    elsewhere alike), a card payment whose amount moves (a monthly shop). Counted
    unasked, either would be spending nobody sees was never recurring. Income
    asks unless certain: a friend paying back monthly is no salary."""
    if level is Confidence.CERTAIN:
        return True
    if level is not Confidence.PROBABLE or series.kind is CashflowType.INCOME:
        return False
    return f.family == "direct_debit" or (f.family == "card" and not series.variable)


# A refund comes back within this long of the series' last debit.
REFUND_MONTHS_AFTER = 4


def linked_refunds(
    series: Series, credits: Iterable[RecurrenceOp], merchants: set[int] | None = None,
) -> list[RecurrenceOp]:
    """Credits from the series' own merchants — or from `merchants`, when the
    caller knows more of them — on any account, from a due date before its
    first debit to four months after its last."""
    merchants = merchants if merchants is not None else series.merchants
    low = series.first.day - timedelta(days=int(series.cadence.nominal))
    high = add_months(series.last.day, REFUND_MONTHS_AFTER)
    return sorted(
        (
            op for op in credits
            if op.merchant in merchants and op.currency == series.currency
            and not op.cancelled and low <= op.day <= high
        ),
        key=_by_day,
    )


def seed_series(
    seed: RecurrenceOp, pool: Iterable[RecurrenceOp], cadence: Cadence | None = None,
    kind: CashflowType = CashflowType.EXPENSE,
) -> Series:
    """The series a user marked by hand, grown from one of its operations: the
    nearest one at each due date either side, of its merchant and account,
    with no minimum. The cadence growing the longest one wins, monthly on a tie."""
    points = sorted(
        (op for op in pool if op.merchant == seed.merchant and op.account == seed.account and op.currency == seed.currency),
        key=_by_day,
    )
    if all(op.id != seed.id for op in points):
        points = sorted([*points, seed], key=_by_day)
    candidates = [cadence] if cadence else sorted(CADENCES, key=lambda c: (c is not MONTHLY, -c.prior))
    best: Series | None = None
    for c in candidates:
        chain = _grown(seed, points, c)
        if best is None or len(chain) > len(best.regular):
            best = Series(c, chain, merchants={seed.merchant}, variable=_is_variable(chain), kind=kind)
    return best


def _grown(seed: RecurrenceOp, points: list[RecurrenceOp], cadence: Cadence) -> list[RecurrenceOp]:
    chain = [seed]
    for forward in (True, False):
        current = seed
        while True:
            options = []
            for op in points:
                stepped = step(cadence, current, op) if forward else step(cadence, op, current)
                if stepped is not None:
                    slots, residual = stepped
                    options.append((slots, residual, abs(op.value - current.value), op.day, op.id, op))
            if not options:
                break
            current = min(options, key=lambda o: o[:5])[-1]
            chain.append(current)
    return sorted(chain, key=_by_day)
