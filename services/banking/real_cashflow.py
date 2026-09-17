"""
The real cashflow: what the linked accounts say was earned, spent, set aside
and invested, by completed month and by year.

The counterpart of the declared cashflow (`services/cashflow.py`). No figure
here has a rule of its own: movements are loaded and paired by `flows.py`, and
how each one counts comes from the very reading the Opérations list shows
(`flows._filed`).

Only completed months count. A month in progress reads as a drop in every
figure, and an average taken over it would carry that drop into the year. The
month in progress has its own reader, `real_cashflow_current`, which compares
it day by day with the months before it rather than totalling it.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from statistics import median, quantiles

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import (
    BankFlowCurrencyTotal,
    CashflowType,
    TypeSource,
    RealCashflowCounterpart,
    RealCashflowCoverageGap,
    RealCashflowCurrent,
    RealCashflowExpense,
    RealCashflowMonth,
    RealCashflowMonthDetail,
    RealCashflowPacePoint,
    RealCashflowSafetyNet,
    RealCashflowTotals,
    RealCashflowYear,
)
from models.banking import BankAccountLink, BankTransaction
from services.banking.cashflow_types import counted_leg, signed_amount
from services.banking.label_groups import group_key, group_name, group_words, merge_similar
from services.banking.transfer_patterns import TransferPatterns
from services.banking.flows import (
    SAVINGS_ACCOUNTS,
    _Accounts,
    _accounts_of_types,
    _filed,
    _filing,
    _label,
    _pairing,
    _paired_movements,
    _shift_period,
    _user_accounts,
)
from services.encryption import decrypt_data, hash_index

TOP_EXPENSES = 5
TOP_COUNTERPARTS = 5
# How far back a stored period is looked for. Each candidate is one HMAC, not a
# decryption: the periods are found without reading a single row.
_HISTORY_YEARS = 40
# The months a median month, a safety net or a pace is measured over.
RECENT_MONTHS = 12
# Below this many covered months, no month of a year is called unusual: the
# spread of three months says nothing.
ATYPICAL_MIN_MONTHS = 6
# A balance a sync has not refreshed for longer may be out of date.
STALE_BALANCE_DAYS = 7

_FIELD_OF = {
    CashflowType.INCOME: "income",
    CashflowType.EXPENSE: "expenses",
    CashflowType.SAVING: "saving",
    CashflowType.INVESTMENT: "investment",
    CashflowType.NEUTRAL: "neutral",
}
_AMOUNTS = ("income", "expenses", "saving", "investment", "neutral", "net")
_PERCENT = Decimal("0.1")


class PeriodNotCompletedError(ValueError):
    """Only a completed month has a real cashflow."""


@dataclass
class _Tally:
    totals: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    count: int = 0


def completed_period(today: date) -> str:
    """The last completed month."""
    return _shift_period(f"{today:%Y-%m}", -1)


def real_cashflow_year(
    session: Session, user_uuid: str, master_key: str, year: int | None = None, today: date | None = None
) -> RealCashflowYear:
    today = today or date.today()
    year = year or today.year
    accounts = _user_accounts(session, user_uuid, master_key)
    stored = _stored_periods(session, master_key, accounts, today)
    years_available = list(range(int(stored[0][:4]), today.year + 1)) if stored else []

    last = completed_period(today)
    periods = [p for p in (f"{year:04d}-{m:02d}" for m in range(1, 13)) if p <= last]
    if not periods or not stored:
        return _empty_year(year, years_available, periods)

    current = year == today.year
    previous = [f"{year - 1:04d}-{p[5:]}" for p in periods]
    recent = _recent_periods(last) if current else []
    reading = _read(session, user_uuid, master_key, accounts, _span([*periods, *previous, *recent]))

    months = [_month(reading, p) for p in periods]
    covered = [m for m in months if m.operation_count]
    _mark_atypical(covered)
    monthly_median = _per_month(covered, median)
    before = [reading.months[p] for p in previous if reading.months[p].count]
    window = set(periods)
    return RealCashflowYear(
        year=year,
        currency=reading.currency,
        years_available=years_available,
        months=months,
        totals=_sum(months),
        covered_months=len(covered),
        open_questions=sum(m.open_questions for m in months),
        open_amount=sum((m.open_amount for m in months), Decimal("0")),
        monthly_mean=_per_month(covered, lambda values: sum(values, Decimal("0")) / len(values)),
        monthly_median=monthly_median,
        top_expenses=reading.top_expenses(window),
        top_sources=reading.counterparts(window, sources=True),
        top_destinations=reading.counterparts(window, sources=False),
        other_currencies=reading.other_currencies(window),
        previous_year_to_date=_sum([_totals(t) for t in before]) if before else None,
        projection=_projection(_sum(months), monthly_median, 12 - len(periods)) if current and covered else None,
        safety_net=_safety_net(session, user_uuid, master_key, accounts, reading, recent, today) if current else None,
        coverage_gaps=_coverage_gaps(
            session, user_uuid, master_key, accounts, reading.patterns,
            date.fromisoformat(f"{periods[0]}-01"), _last_day(periods[-1]),
        ),
    )


def real_cashflow_month(
    session: Session, user_uuid: str, master_key: str, period: str, today: date | None = None
) -> RealCashflowMonthDetail:
    today = today or date.today()
    if period > completed_period(today):
        raise PeriodNotCompletedError(period)
    accounts = _user_accounts(session, user_uuid, master_key)
    stored = [p for p in _stored_periods(session, master_key, accounts, today) if p <= completed_period(today)]
    earlier = [p for p in stored if p < period]
    later = [p for p in stored if p > period]
    if not stored:
        return RealCashflowMonthDetail(
            period=period, currency="EUR", totals=RealCashflowTotals(), operation_count=0, other_currencies=[],
        )

    reading = _read(session, user_uuid, master_key, accounts, [period])
    tally = reading.months[period]
    window = {period}
    return RealCashflowMonthDetail(
        period=period,
        currency=reading.currency,
        totals=_totals(tally),
        operation_count=tally.count,
        open_questions=reading.open_questions(period),
        open_amount=reading.open_amount(period),
        previous_period=earlier[-1] if earlier else None,
        next_period=later[0] if later else None,
        other_currencies=reading.other_currencies(window),
        top_expenses=reading.top_expenses(window),
        top_sources=reading.counterparts(window, sources=True),
        top_destinations=reading.counterparts(window, sources=False),
        coverage_gaps=_coverage_gaps(
            session, user_uuid, master_key, accounts, reading.patterns,
            date.fromisoformat(f"{period}-01"), _last_day(period),
        ),
    )


def real_cashflow_current(
    session: Session, user_uuid: str, master_key: str, today: date | None = None
) -> RealCashflowCurrent:
    """What the month in progress has spent so far, against what the recent
    months had spent by the same day.

    Pending operations count here, unlike in any completed month: a card
    payment awaiting settlement is spent already, and leaving it out would
    read the last few days as a lull.
    """
    today = today or date.today()
    period = f"{today:%Y-%m}"
    length = calendar.monthrange(today.year, today.month)[1]
    accounts = _user_accounts(session, user_uuid, master_key)
    recent = _recent_periods(completed_period(today))
    reading = _read(
        session, user_uuid, master_key, accounts, [*recent, period], pending_in={period}, daily=True,
    )

    past = [p for p in recent if reading.months[p].count]
    curves = [_cumulated(reading.daily[p], calendar.monthrange(int(p[:4]), int(p[5:]))[1]) for p in past]

    def median_at(day: int) -> Decimal | None:
        if not curves:
            return None
        return Decimal(median(curve[min(day, len(curve)) - 1] for curve in curves))

    cumulated = _cumulated(reading.daily[period], length)
    spent_to_date = cumulated[today.day - 1]
    median_to_date = median_at(today.day)
    median_month = Decimal(median(curve[-1] for curve in curves)) if curves else None
    return RealCashflowCurrent(
        period=period,
        currency=reading.currency,
        day=today.day,
        spent_to_date=spent_to_date,
        pending_to_date=sum(reading.pending[period].values(), Decimal("0")),
        median_to_date=median_to_date,
        median_month=median_month,
        projection=(
            spent_to_date + max(Decimal("0"), median_month - median_to_date)
            if median_month is not None and median_to_date is not None else None
        ),
        open_amount=reading.open_amount(period),
        curve=[
            RealCashflowPacePoint(
                day=day, spent=cumulated[day - 1] if day <= today.day else None, median=median_at(day),
            )
            for day in range(1, length + 1)
        ],
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _Reading:
    currency: str
    months: dict[str, _Tally]
    expenses: list[tuple[str, Decimal, RealCashflowExpense]]
    # (period, is_source, key, day, label, amount)
    flows: list[tuple[str, bool, str, date | None, str | None, Decimal]]
    # The words each (side, key) is named by, to bring one counterpart's
    # spellings together.
    group_words: dict[tuple[bool, str], frozenset[str]]
    others: dict[str, dict[str, dict[str, Decimal]]]
    patterns: TransferPatterns
    # Expenses by period and day of the month, when asked for.
    daily: dict[str, dict[int, Decimal]]
    pending: dict[str, dict[int, Decimal]]

    def open_questions(self, period: str) -> int:
        # From the whole history: a label's question sits on its last operation,
        # possibly years later, while it decides how this month's count.
        return self.patterns.questions.get(period, 0) + self.patterns.flow_open.get(period, 0)

    def open_amount(self, period: str) -> Decimal:
        return (
            self.patterns.questions_amount.get(period, Decimal("0"))
            + self.patterns.flow_open_amount.get(period, Decimal("0"))
        )

    def top_expenses(self, periods: set[str]) -> list[RealCashflowExpense]:
        kept = [(amount, expense) for period, amount, expense in self.expenses if period in periods]
        ranked = sorted(kept, key=lambda e: (-e[0], e[1].operation_date or date.min, e[1].id))
        return [expense for _, expense in ranked[:TOP_EXPENSES]]

    def counterparts(self, periods: set[str], sources: bool) -> list[RealCashflowCounterpart]:
        raw_amounts: dict[str, Decimal] = defaultdict(Decimal)
        raw_occurrences: dict[str, list[tuple[date | None, str | None]]] = defaultdict(list)
        for period, is_source, key, day, label, amount in self.flows:
            if period in periods and is_source == sources:
                raw_amounts[key] += amount
                raw_occurrences[key].append((day, label))
        # The same counterpart spelled several ways holds one line, as in the
        # ledger the Explorer reads.
        into = merge_similar([
            (key, self.group_words[(sources, key)], len(raw_occurrences[key])) for key in raw_occurrences
        ])
        amounts: dict[str, Decimal] = defaultdict(Decimal)
        occurrences: dict[str, list[tuple[date | None, str | None]]] = defaultdict(list)
        for key, rows in raw_occurrences.items():
            amounts[into[key]] += raw_amounts[key]
            occurrences[into[key]].extend(rows)
        total = sum(amounts.values(), Decimal("0"))
        ranked = sorted(amounts, key=lambda key: (-amounts[key], key))[:TOP_COUNTERPARTS]
        return [
            RealCashflowCounterpart(
                group_key=key,
                name=group_name(occurrences[key]),
                amount=amounts[key],
                operation_count=len(occurrences[key]),
                share=(amounts[key] * 100 / total).quantize(_PERCENT),
            )
            for key in ranked
        ]

    def other_currencies(self, periods: set[str]) -> list[BankFlowCurrencyTotal]:
        merged: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")})
        for period in periods:
            for currency, sides in self.others.get(period, {}).items():
                merged[currency]["in"] += sides["in"]
                merged[currency]["out"] += sides["out"]
        return [
            BankFlowCurrencyTotal(currency=c, inflow=v["in"], outflow=v["out"])
            for c, v in sorted(merged.items())
        ]


def _read(
    session: Session,
    user_uuid: str,
    master_key: str,
    accounts: _Accounts,
    periods: list[str],
    pending_in: set[str] = frozenset(),  # type: ignore[assignment]
    daily: bool = False,
) -> _Reading:
    """Every operation of `periods`, counted by type. `pending_in` names the
    periods whose pending operations count too."""
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements, transfer_legs = _paired_movements(session, master_key, accounts.readable, periods, pairing)
    filing = _filing(session, user_uuid, master_key, accounts, pairing.patterns, movements, transfer_legs)
    window = set(periods)
    selected = [
        i for i, m in enumerate(movements)
        if m.period in window and (m.is_final or m.period in pending_in)
    ]

    # The headline currency, chosen as `flows._aggregate` chooses it.
    counts: dict[str, int] = defaultdict(int)
    for index in selected:
        counts[movements[index].currency] += 1
    currency = max(counts, key=lambda c: counts[c]) if counts else "EUR"

    names = {bidx: decrypt_data(a.name_enc, master_key) for bidx, a in accounts.by_bidx.items()}
    # The words too common to tell counterparts apart, over every account of a
    # side: a merchant paid from two accounts stays one counterpart.
    common = {
        side: frozenset().union(*(pairing.patterns.label_common(bidx, side) for bidx in accounts.readable))
        for side in (True, False)
    }
    reading = _Reading(
        currency=currency,
        months={p: _Tally() for p in periods},
        expenses=[],
        flows=[],
        others=defaultdict(lambda: defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")})),
        patterns=pairing.patterns,
        daily={p: defaultdict(Decimal) for p in periods},
        pending={p: defaultdict(Decimal) for p in periods},
        group_words={},
    )
    for index in selected:
        movement = movements[index]
        if movement.currency != currency:
            if movement.is_final:
                reading.others[movement.period][movement.currency]["in" if movement.is_credit else "out"] += movement.amount
            continue
        label = _label(movement, master_key)
        resolution = _filed(movements, transfer_legs, index, label, filing)
        kind = resolution.type
        paired = resolution.source is TypeSource.PAIR
        if not counted_leg(movement.is_credit, movement.account_bidx in filing.savings, kind, paired):
            continue
        signed = signed_amount(movement.amount, movement.is_credit, kind)
        if daily and kind is CashflowType.EXPENSE and movement.day is not None:
            reading.daily[movement.period][movement.day.day] += signed
            if not movement.is_final:
                reading.pending[movement.period][movement.day.day] += signed
        if not movement.is_final:
            continue
        tally = reading.months[movement.period]
        tally.totals[_FIELD_OF[kind]] += signed
        tally.count += 1
        if kind is CashflowType.EXPENSE and not movement.is_credit:
            reading.expenses.append((movement.period, movement.amount, RealCashflowExpense(
                id=movement.row.uuid,
                operation_date=movement.day,
                label=label,
                amount=movement.amount,
                account_name=names[movement.account_bidx],
            )))
        if (kind is CashflowType.INCOME and movement.is_credit) or (kind is CashflowType.EXPENSE and not movement.is_credit):
            key = group_key(label, common[movement.is_credit])
            reading.group_words[(movement.is_credit, key)] = group_words(label, common[movement.is_credit])
            reading.flows.append((movement.period, movement.is_credit, key, movement.day, label, movement.amount))
    return reading


def _month(reading: _Reading, period: str) -> RealCashflowMonth:
    return RealCashflowMonth(
        period=period,
        operation_count=reading.months[period].count,
        open_questions=reading.open_questions(period),
        open_amount=reading.open_amount(period),
        **_totals(reading.months[period]).model_dump(),
    )


def _mark_atypical(months: list[RealCashflowMonth]) -> None:
    """Flag the months whose spending sits far above the others: beyond the
    upper quartile by one and a half times the spread of the middle half."""
    if len(months) < ATYPICAL_MIN_MONTHS:
        return
    lower, _, upper = quantiles([m.expenses for m in months], n=4, method="inclusive")
    ceiling = upper + (upper - lower) * Decimal("1.5")
    for month in months:
        month.atypical = month.expenses > ceiling


def _recent_periods(last: str) -> list[str]:
    return [_shift_period(last, -offset) for offset in range(RECENT_MONTHS - 1, -1, -1)]


def _span(periods: list[str]) -> list[str]:
    """Every month from the earliest to the latest: pairing reads a month
    either side of its window, so a window with holes would pair differently
    on each hole's edges."""
    first, last = min(periods), max(periods)
    span = [first]
    while span[-1] < last:
        span.append(_shift_period(span[-1], 1))
    return span


def _last_day(period: str) -> date:
    year, month = int(period[:4]), int(period[5:])
    return date(year, month, calendar.monthrange(year, month)[1])


def _cumulated(days: dict[int, Decimal], length: int) -> list[Decimal]:
    running, curve = Decimal("0"), []
    for day in range(1, length + 1):
        running += days.get(day, Decimal("0"))
        curve.append(running)
    return curve


def _stored_periods(session: Session, master_key: str, accounts: _Accounts, today: date) -> list[str]:
    """The "YYYY-MM" periods holding at least one stored row, oldest first."""
    if not accounts.readable:
        return []
    stored = set(session.exec(
        select(sa.distinct(BankTransaction.period_bidx)).where(
            BankTransaction.account_id_bidx.in_(accounts.readable)  # type: ignore[attr-defined]
        )
    ).all())
    candidates = (
        f"{year:04d}-{month:02d}"
        for year in range(today.year - _HISTORY_YEARS, today.year + 1)
        for month in range(1, 13)
    )
    return [p for p in candidates if hash_index(p, master_key) in stored]


def _rate(part: Decimal, income: Decimal) -> Decimal | None:
    return (part * 100 / income).quantize(_PERCENT) if income > 0 else None


def _with_rates(amounts: dict[str, Decimal]) -> RealCashflowTotals:
    income = amounts["income"]
    return RealCashflowTotals(
        **amounts,
        savings_rate=_rate(income - amounts["expenses"], income),
        placed_rate=_rate(amounts["saving"] + amounts["investment"], income),
    )


def _totals(tally: _Tally) -> RealCashflowTotals:
    t = tally.totals
    amounts = {name: t[name] for name in _AMOUNTS if name != "net"}
    return _with_rates({**amounts, "net": t["income"] - t["expenses"] - t["saving"] - t["investment"]})


def _sum(months: list[RealCashflowTotals]) -> RealCashflowTotals:
    return _with_rates({name: sum((getattr(m, name) for m in months), Decimal("0")) for name in _AMOUNTS})


def _per_month(months: list[RealCashflowMonth], reduce) -> RealCashflowTotals:
    """A monthly figure per nature, over the months carrying data — dividing a
    three-month history by twelve would read as a collapse. The rates are those
    of the monthly figures, never an average of monthly rates: a month without
    income has none to average."""
    if not months:
        return RealCashflowTotals()
    return _with_rates({name: Decimal(reduce([getattr(m, name) for m in months])) for name in _AMOUNTS})


def _projection(totals: RealCashflowTotals, monthly: RealCashflowTotals, months_left: int) -> RealCashflowTotals:
    amounts = {name: getattr(totals, name) + getattr(monthly, name) * months_left for name in _AMOUNTS if name != "net"}
    amounts["net"] = amounts["income"] - amounts["expenses"] - amounts["saving"] - amounts["investment"]
    return _with_rates(amounts)


def _links(session: Session, user_uuid: str, master_key: str) -> dict[str, date]:
    """Each linked account's last successful sync, by its blind index."""
    return {
        link.bank_account_uuid_bidx: link.last_synced_at
        for link in session.exec(
            select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == hash_index(user_uuid, master_key))
        ).all()
    }


def _safety_net(
    session: Session,
    user_uuid: str,
    master_key: str,
    accounts: _Accounts,
    reading: _Reading,
    recent: list[str],
    today: date,
) -> RealCashflowSafetyNet | None:
    spending = [reading.months[p].totals["expenses"] for p in recent if reading.months[p].count]
    if not spending:
        return None
    monthly = Decimal(median(spending))
    savings = _accounts_of_types(accounts, SAVINGS_ACCOUNTS, master_key)
    links = _links(session, user_uuid, master_key)
    available = held = Decimal("0")
    stale: list[str] = []
    for bidx, account in accounts.by_bidx.items():
        currency = decrypt_data(account.currency_enc, master_key) if account.currency_enc else reading.currency
        if currency != reading.currency:
            continue
        balance = Decimal(decrypt_data(account.balance_enc, master_key))
        available += balance
        if bidx in savings:
            held += balance
        synced = links.get(bidx)
        if synced is None or synced < today - timedelta(days=STALE_BALANCE_DAYS):
            stale.append(decrypt_data(account.name_enc, master_key))

    def months_of(amount: Decimal) -> Decimal | None:
        return (amount / monthly).quantize(_PERCENT) if monthly > 0 else None

    return RealCashflowSafetyNet(
        available=available,
        savings=held,
        monthly_expenses=monthly,
        months=months_of(available),
        savings_months=months_of(held),
        stale_accounts=sorted(stale),
    )


def _coverage_gaps(
    session: Session,
    user_uuid: str,
    master_key: str,
    accounts: _Accounts,
    patterns: TransferPatterns,
    start: date,
    end: date,
) -> list[RealCashflowCoverageGap]:
    """The accounts whose history does not span `start` to `end`.

    A linked account is complete up to its last sync, whatever its last
    operation: a quiet account is not a stale one. An imported one is known
    only as far as its last operation.
    """
    links = _links(session, user_uuid, master_key)
    gaps = []
    for bidx in accounts.readable:
        if bidx not in patterns.coverage:
            continue
        first, last = patterns.coverage[bidx]
        covered_until = links.get(bidx, last)
        starts_late, ends_early = first > start, covered_until < end
        if starts_late or ends_early:
            account = accounts.by_bidx[bidx]
            gaps.append(RealCashflowCoverageGap(
                account_id=account.uuid,
                account_name=decrypt_data(account.name_enc, master_key),
                first_day=first,
                covered_until=covered_until,
                starts_late=starts_late,
                ends_early=ends_early,
            ))
    return sorted(gaps, key=lambda gap: gap.account_name)


def _empty_year(year: int, years_available: list[int], periods: list[str]) -> RealCashflowYear:
    return RealCashflowYear(
        year=year,
        currency="EUR",
        years_available=years_available,
        months=[RealCashflowMonth(period=p) for p in periods],
        totals=RealCashflowTotals(),
        covered_months=0,
        monthly_mean=RealCashflowTotals(),
        monthly_median=RealCashflowTotals(),
        top_expenses=[],
        other_currencies=[],
    )
