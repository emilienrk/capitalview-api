"""
The real cashflow: what the linked accounts say was earned, spent, set aside
and invested, by completed month and by year.

The counterpart of the declared cashflow (`services/cashflow.py`). No figure
here has a rule of its own: movements are loaded and paired by `flows.py`, and
how each one counts comes from the very reading the Opérations list shows
(`flows._filed`).

Only completed months count. A month in progress reads as a drop in every
figure, and an average taken over it would carry that drop into the year.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import (
    BankFlowCurrencyTotal,
    CashflowType,
    TypeSource,
    RealCashflowExpense,
    RealCashflowMonth,
    RealCashflowMonthDetail,
    RealCashflowTotals,
    RealCashflowYear,
)
from models.banking import BankTransaction
from services.banking.cashflow_types import counted_leg, signed_amount
from services.banking.flows import (
    _Accounts,
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
# How far back a stored period is looked for. Each candidate is one HMAC, not a
# decryption: the periods are found without reading a single row.
_HISTORY_YEARS = 40

_FIELD_OF = {
    CashflowType.INCOME: "income",
    CashflowType.EXPENSE: "expenses",
    CashflowType.SAVING: "saving",
    CashflowType.INVESTMENT: "investment",
    CashflowType.NEUTRAL: "neutral",
}


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

    reading = _read(session, user_uuid, master_key, accounts, periods)
    months = [
        RealCashflowMonth(period=p, operation_count=reading.months[p].count, **_totals(reading.months[p]).model_dump())
        for p in periods
    ]
    covered = [m for m in months if m.operation_count]
    return RealCashflowYear(
        year=year,
        currency=reading.currency,
        years_available=years_available,
        months=months,
        totals=_sum(months),
        covered_months=len(covered),
        monthly_mean=_per_month(covered, lambda values: sum(values, Decimal("0")) / len(values)),
        monthly_median=_per_month(covered, median),
        top_expenses=reading.top_expenses(),
        other_currencies=reading.other_currencies(),
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
    return RealCashflowMonthDetail(
        period=period,
        currency=reading.currency,
        totals=_totals(tally),
        operation_count=tally.count,
        previous_period=earlier[-1] if earlier else None,
        next_period=later[0] if later else None,
        other_currencies=reading.other_currencies(),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _Reading:
    currency: str
    months: dict[str, _Tally]
    expenses: list[tuple[Decimal, RealCashflowExpense]]
    others: dict[str, dict[str, Decimal]]

    def top_expenses(self) -> list[RealCashflowExpense]:
        ranked = sorted(self.expenses, key=lambda e: (-e[0], e[1].operation_date or date.min, e[1].id))
        return [expense for _, expense in ranked[:TOP_EXPENSES]]

    def other_currencies(self) -> list[BankFlowCurrencyTotal]:
        return [
            BankFlowCurrencyTotal(currency=c, inflow=v["in"], outflow=v["out"])
            for c, v in sorted(self.others.items())
        ]


def _read(session: Session, user_uuid: str, master_key: str, accounts: _Accounts, periods: list[str]) -> _Reading:
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements, transfer_legs = _paired_movements(session, master_key, accounts.readable, periods, pairing)
    filing = _filing(session, user_uuid, master_key, accounts, pairing.patterns)
    window = set(periods)
    selected = [i for i, m in enumerate(movements) if m.period in window and m.is_final]

    # The headline currency, chosen as `flows._aggregate` chooses it.
    counts: dict[str, int] = defaultdict(int)
    for index in selected:
        counts[movements[index].currency] += 1
    currency = max(counts, key=lambda c: counts[c]) if counts else "EUR"

    names = {bidx: decrypt_data(a.name_enc, master_key) for bidx, a in accounts.by_bidx.items()}
    reading = _Reading(
        currency=currency,
        months={p: _Tally() for p in periods},
        expenses=[],
        others=defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")}),
    )
    for index in selected:
        movement = movements[index]
        if movement.currency != currency:
            reading.others[movement.currency]["in" if movement.is_credit else "out"] += movement.amount
            continue
        label = _label(movement, master_key)
        resolution = _filed(movements, transfer_legs, index, label, filing)
        kind = resolution.type
        paired = resolution.source is TypeSource.PAIR
        if not counted_leg(movement.is_credit, movement.account_bidx in filing.savings, kind, paired):
            continue
        tally = reading.months[movement.period]
        tally.totals[_FIELD_OF[kind]] += signed_amount(movement.amount, movement.is_credit, kind)
        tally.count += 1
        if kind is CashflowType.EXPENSE and not movement.is_credit:
            reading.expenses.append((movement.amount, RealCashflowExpense(
                id=movement.row.uuid,
                operation_date=movement.day,
                label=label,
                amount=movement.amount,
                account_name=names[movement.account_bidx],
            )))
    return reading


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


def _totals(tally: _Tally) -> RealCashflowTotals:
    return RealCashflowTotals(**tally.totals)


def _sum(months: list[RealCashflowMonth]) -> RealCashflowTotals:
    return RealCashflowTotals(**{
        name: sum((getattr(m, name) for m in months), Decimal("0")) for name in RealCashflowTotals.model_fields
    })


def _per_month(months: list[RealCashflowMonth], reduce) -> RealCashflowTotals:
    """A monthly figure per nature, over the months carrying data — dividing a
    three-month history by twelve would read as a collapse."""
    if not months:
        return RealCashflowTotals()
    return RealCashflowTotals(**{
        name: Decimal(reduce([getattr(m, name) for m in months])) for name in RealCashflowTotals.model_fields
    })


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
