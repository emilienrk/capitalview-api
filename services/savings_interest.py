"""
Savings interest — what a savings account earns over the current year.

The regulated livrets, and most banks' own savings accounts, count by quinzaine:
a deposit earns from the 1st or the 16th that follows it, a withdrawal stops
earning from the 1st or the 16th before it, and each quinzaine earns

    balance × rate / 24

so 20 000 € paid in on 10 December earn one quinzaine, not a year. Some banks
count by the day instead. Either way the interest is paid on 31 December.

The balance on each day comes from the account's history, the rates from what
the user entered. Nothing here is stored.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from dtos.bank import INTEREST_BEARING_TYPES, SavingsInterestResponse
from models import BankAccount, BankAccountType
from models.currency import BASE_CURRENCY
from models.enums import InterestMethod
from services.bank import account_currency, get_bank_account_history, interest_method
from services.encryption import decrypt_data, hash_index
from services.market import get_exchange_rate

_ZERO = Decimal("0")
QUINZAINES_PER_YEAR = Decimal("24")


@dataclass(frozen=True)
class InterestTerms:
    rate: Decimal
    boosted_rate: Decimal | None = None
    boosted_until: date | None = None

    def rate_on(self, day: date) -> Decimal:
        if self.boosted_rate is not None and self.boosted_until is not None and day <= self.boosted_until:
            return self.boosted_rate
        return self.rate


def quinzaines(year: int) -> list[tuple[date, date]]:
    """The 24 quinzaines of a year: 1st–15th and 16th–end of each month."""
    spans = []
    for month in range(1, 13):
        last = calendar.monthrange(year, month)[1]
        spans.append((date(year, month, 1), date(year, month, 15)))
        spans.append((date(year, month, 16), date(year, month, last)))
    return spans


def fortnightly_interest(
    balance_on: dict[date, Decimal], year: int, terms: InterestTerms
) -> list[tuple[date, Decimal]]:
    """Interest each quinzaine earns, keyed by its last day.

    A quinzaine earns on the balance it starts with, less whatever leaves during
    it: money arriving waits for the next one, money leaving stops at once.
    *balance_on* holds the end-of-day balance of every day from 31 December of
    the year before to 31 December.
    """
    result = []
    for start, end in quinzaines(year):
        earning = balance_on[start - timedelta(days=1)]
        day = start
        while day <= end:
            earning += min(balance_on[day] - balance_on[day - timedelta(days=1)], _ZERO)
            day += timedelta(days=1)
        days = (end - start).days + 1
        rate = sum((terms.rate_on(start + timedelta(days=i)) for i in range(days)), _ZERO) / days
        result.append((end, max(earning, _ZERO) * rate / QUINZAINES_PER_YEAR))
    return result


def daily_interest(
    balance_on: dict[date, Decimal], year: int, terms: InterestTerms
) -> list[tuple[date, Decimal]]:
    """Interest each day's closing balance earns."""
    days_in_year = Decimal(366 if calendar.isleap(year) else 365)
    result = []
    day = date(year, 1, 1)
    while day.year == year:
        result.append((day, max(balance_on[day], _ZERO) * terms.rate_on(day) / days_in_year))
        day += timedelta(days=1)
    return result


def year_balances(
    known: dict[date, Decimal],
    current_balance: Decimal,
    year: int,
    today: date,
    method: InterestMethod = InterestMethod.FORTNIGHTLY,
) -> tuple[dict[date, Decimal], date | None]:
    """End-of-day balances from 31 December of the year before to 31 December.

    A day with no snapshot carries the last one forward; from today on, the
    balance is the account's current one, held until the year ends.

    Before the first known balance the account is taken as empty. Counted by
    quinzaine, that balance also reaches back to the start of its own: it is
    where tracking starts, not a deposit, and read as one it would earn nothing
    until the next quinzaine.
    The second value is the day counting starts when it falls inside the year.
    """
    day = date(year - 1, 12, 31)
    last: Decimal | None = None
    tracked_from: date | None = None
    balances: dict[date, Decimal] = {}
    while day <= date(year, 12, 31):
        if day >= today:
            value = current_balance
        else:
            value = known.get(day, last)
        if value is not None and last is None and tracked_from is None:
            tracked_from = day
        balances[day] = value if value is not None else _ZERO
        if value is not None:
            last = value
        day += timedelta(days=1)
    if tracked_from is None:
        return balances, None
    if method == InterestMethod.DAILY:
        return balances, (tracked_from if tracked_from > date(year, 1, 1) else None)
    start = tracked_from.replace(day=1 if tracked_from.day < 16 else 16)
    backfill = start - timedelta(days=1)
    while backfill < tracked_from:
        balances[backfill] = balances[tracked_from]
        backfill += timedelta(days=1)
    return balances, (start if start > date(year, 1, 1) else None)


def _terms(account: BankAccount, master_key: str) -> InterestTerms | None:
    if not account.interest_rate_enc:
        return None
    return InterestTerms(
        rate=Decimal(decrypt_data(account.interest_rate_enc, master_key)),
        boosted_rate=(
            Decimal(decrypt_data(account.boosted_rate_enc, master_key))
            if account.boosted_rate_enc
            else None
        ),
        boosted_until=account.boosted_until,
    )


def _balance_in_base(session: Session, account: BankAccount, master_key: str) -> Decimal:
    # In euros, like the history it continues.
    balance = Decimal(decrypt_data(account.balance_enc, master_key))
    currency = account_currency(account, master_key)
    if currency != BASE_CURRENCY:
        balance *= get_exchange_rate(session, currency, BASE_CURRENCY)
    return balance


def _interest_accounts(
    session: Session, user_uuid: str, master_key: str
) -> list[tuple[BankAccount, InterestTerms, InterestMethod]]:
    rows = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == hash_index(user_uuid, master_key))
    ).all()
    result = []
    for account in rows:
        account_type = BankAccountType(decrypt_data(account.account_type_enc, master_key))
        terms = _terms(account, master_key)
        method = interest_method(account, account_type, master_key)
        if account_type in INTEREST_BEARING_TYPES and terms is not None and method is not None:
            result.append((account, terms, method))
    return result


def get_user_savings_interest(
    session: Session, user_uuid: str, master_key: str, today: date | None = None
) -> list[SavingsInterestResponse]:
    """This year's interest on every savings account the user gave a rate."""
    today = today or date.today()
    year = today.year
    result = []
    for account, terms, method in _interest_accounts(session, user_uuid, master_key):
        history = get_bank_account_history(
            session, account.uuid, master_key,
            start_date=date(year - 1, 12, 31),
            end_date=today - timedelta(days=1),
        )
        known = {snap.snapshot_date: Decimal(snap.total_value) for snap in history}
        balances, tracked_from = year_balances(
            known, _balance_in_base(session, account, master_key), year, today, method
        )
        compute = fortnightly_interest if method == InterestMethod.FORTNIGHTLY else daily_interest
        periods = compute(balances, year, terms)
        result.append(
            SavingsInterestResponse(
                account_id=account.uuid,
                year=year,
                rate=terms.rate_on(today),
                method=method,
                earned=round(sum((a for end, a in periods if end < today), _ZERO), 2),
                estimated=round(sum((a for _, a in periods), _ZERO), 2),
                tracked_from=tracked_from,
            )
        )
    return result


def declared_savings_rate(session: Session, user_uuid: str, master_key: str) -> Decimal | None:
    """Today's rates of the user's savings accounts, weighted by their balances.

    None when no account carries a rate. Accounts with a rate and nothing on
    them still count, once each, so a rate entered ahead of the first deposit
    is not lost.
    """
    today = date.today()
    weighted = weight = _ZERO
    for account, terms, _ in _interest_accounts(session, user_uuid, master_key):
        share = max(_balance_in_base(session, account, master_key), Decimal("1"))
        weighted += terms.rate_on(today) * share
        weight += share
    return weighted / weight if weight > 0 else None
