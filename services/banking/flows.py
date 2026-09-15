"""
Observed cash flows: what actually moved on the linked accounts.

The counterpart of `services/cashflow.py`, which holds what the user *declared*
would move. Everything here is derived from stored `BankTransaction` rows, so it
needs no network and no Enable Banking credentials.

Two readers share one pipeline — load, pair the internal transfers, aggregate —
so the monthly totals and the list of a month's operations can never disagree:
`compute_real_flows` sums months, `list_month_transactions` lists one of them.

Deliberately bank-agnostic. It reads only the three fields the Enable Banking
contract marks required on every transaction — amount, currency and
`credit_debit_indicator` — plus the status. It never parses a label: the
`remittance_information` format is the bank's own invention (Boursorama writes
`CARTE 03/08/25 AIRBNB * HMFYWK533K`, another writes something else), and the
structured fields that would replace it — `merchant_category_code`,
`bank_transaction_code`, `creditor` — are empty on all 4 240 real rows captured
so far. The label is only ever handed back as-is, for the user to read.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlmodel import Session, select

from dtos.banking import (
    BankFlowCurrencyTotal,
    BankFlowMonth,
    BankFlowsResponse,
    BankTransactionItem,
    BankTransactionsResponse,
)
from models.bank import BankAccount
from models.banking import BankTransaction
from services.banking.linking import readable_account_bidxs
from services.banking.transactions import CREDIT, FINAL_STATUSES, row_date
from services.encryption import decrypt_data, hash_index

logger = logging.getLogger(__name__)


# How far apart the two legs of one internal transfer may be dated. Banks book
# the debit and the matching credit on the same day as a rule, but a value date
# rolling over a weekend is common enough to cost real matches at zero tolerance.
TRANSFER_DATE_TOLERANCE_DAYS = 3

DEFAULT_MONTHS = 12
MAX_MONTHS = 120


class UnknownAccountError(LookupError):
    """The account filter names no bank account of this user."""


class _Movement(NamedTuple):
    row: BankTransaction
    account_bidx: str
    period: str
    day: date | None
    amount: Decimal
    currency: str
    is_credit: bool
    is_final: bool


class _Accounts(NamedTuple):
    """The user's bank accounts, keyed by the blind index movements carry."""
    by_bidx: dict[str, BankAccount]
    # Only those whose movements a reader may sum (see readable_account_bidxs).
    readable: list[str]


@dataclass
class _Totals:
    currency: str
    months: list[BankFlowMonth]
    pending_count: int
    pending_inflow: Decimal
    pending_outflow: Decimal
    transfers_count: int
    transfers_amount: Decimal
    other_currencies: list[BankFlowCurrencyTotal]


def _months_back(anchor: date, months: int) -> list[str]:
    """The `months` "YYYY-MM" periods ending on `anchor`'s own month."""
    periods = []
    year, month = anchor.year, anchor.month
    for _ in range(months):
        periods.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(periods))


def _shift_period(period: str, months: int) -> str:
    year, month = (int(part) for part in period.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _internal_transfer_legs(movements: list[_Movement]) -> dict[int, int]:
    """Movements that pair up as one transfer between the user's own accounts,
    each index mapped to its other leg's.

    A transfer inflates both sides of the summary: it leaves one linked account
    and lands on another, so counting it makes the user look like they earn and
    spend money they merely moved. Matched on the only signals every bank
    supplies — opposite direction, identical amount and currency, different
    accounts, dates close together — never on a label.

    Greedy and one-to-one: a credit already claimed cannot serve a second debit.
    """
    credits_by_key: dict[tuple[str, Decimal], list[int]] = defaultdict(list)
    for index, movement in enumerate(movements):
        if movement.is_credit and movement.day is not None:
            credits_by_key[(movement.currency, movement.amount)].append(index)

    paired: dict[int, int] = {}
    for index, movement in enumerate(movements):
        if movement.is_credit or movement.day is None:
            continue
        # The nearest eligible credit, not the first one found: with three or
        # four accounts several may sit inside the tolerance, and pairing a
        # same-day leg with a three-day-old one leaves the true pair unmatched.
        best: int | None = None
        best_gap = TRANSFER_DATE_TOLERANCE_DAYS + 1
        for candidate in credits_by_key.get((movement.currency, movement.amount), ()):
            if candidate in paired:
                continue
            other = movements[candidate]
            if other.account_bidx == movement.account_bidx or other.day is None:
                continue
            gap = abs((other.day - movement.day).days)
            if gap < best_gap:
                best, best_gap = candidate, gap
        if best is not None:
            paired[best] = index
            paired[index] = best
    return paired


def _user_accounts(session: Session, user_uuid: str, master_key: str) -> _Accounts:
    user_bidx = hash_index(user_uuid, master_key)
    by_bidx = {
        hash_index(account.uuid, master_key): account
        for account in session.exec(
            select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
        ).all()
    }
    # `BankAccountLink.bank_account_uuid_bidx` and `BankTransaction.account_id_bidx`
    # are the same blind index of the same CapitalView account uuid. Linked
    # accounts and CSV-imported ones alike.
    return _Accounts(by_bidx, readable_account_bidxs(session, user_bidx, master_key))


def _scope(accounts: _Accounts, account_id: str | None, master_key: str) -> list[str]:
    """The accounts a reader asked about, among those it may read."""
    if account_id is None:
        return accounts.readable
    bidx = hash_index(account_id, master_key)
    if bidx not in accounts.by_bidx:
        raise UnknownAccountError(account_id)
    # A manual account nobody imported anything into is the user's, but has
    # nothing to show: an empty answer, not a missing one.
    return [bidx] if bidx in accounts.readable else []


def _paired_movements(
    session: Session,
    master_key: str,
    account_bidxs: list[str],
    periods: list[str],
    pair_transfers: bool,
) -> tuple[list[_Movement], dict[int, int]]:
    """Every movement of `periods` and of the month either side, sorted, with
    the internal transfers paired.

    Loaded across *every* readable account, whatever the reader filters on
    afterwards: a transfer's other leg sits on another account by definition.
    And a month past each edge, because a transfer booked on the 30th may land
    on the 2nd — without it the first month of a window would count as spending
    what the same month reads as a transfer once it is no longer on the edge.
    """
    padded = [_shift_period(periods[0], -1), *periods, _shift_period(periods[-1], 1)]
    period_bidx_to_period = {hash_index(p, master_key): p for p in padded}
    rows = session.exec(
        select(BankTransaction).where(
            BankTransaction.account_id_bidx.in_(account_bidxs),  # type: ignore[attr-defined]
            BankTransaction.period_bidx.in_(list(period_bidx_to_period)),  # type: ignore[attr-defined]
        )
    ).all()

    movements: list[_Movement] = [
        _Movement(
            row=row,
            account_bidx=row.account_id_bidx,
            period=period_bidx_to_period[row.period_bidx],
            day=row_date(row, master_key),
            amount=Decimal(decrypt_data(row.amount_enc, master_key)),
            currency=decrypt_data(row.currency_enc, master_key),
            is_credit=decrypt_data(row.credit_debit_enc, master_key) == CREDIT,
            is_final=decrypt_data(row.status_enc, master_key) in FINAL_STATUSES,
        )
        for row in rows
    ]

    # Sorted before anything reads an index: the database returns rows in no
    # promised order, and transfer pairing would otherwise hand back a different
    # answer for the same data from one call to the next.
    movements.sort(
        key=lambda m: (m.day or date.min, m.account_bidx, m.amount, m.is_credit, m.row.uuid)
    )
    return movements, (_internal_transfer_legs(movements) if pair_transfers else {})


def _aggregate(
    movements: list[_Movement],
    transfer_legs: dict[int, int],
    selected: list[int],
    periods: list[str],
) -> _Totals:
    """The monthly totals of the `selected` movements."""
    # A pair counts once, and in full, as soon as one of its legs is selected:
    # filtered on one account, only one leg ever is.
    pairs = {min(i, transfer_legs[i]) for i in selected if i in transfer_legs}
    kept = [i for i in selected if i not in transfer_legs]

    # The currency the headline totals speak. Picking the most frequent one keeps
    # a stray foreign-currency movement from silently joining a euro total —
    # amounts arrive unconverted, with no exchange rate attached.
    counts: dict[str, int] = defaultdict(int)
    for index in kept:
        counts[movements[index].currency] += 1
    main_currency = max(counts, key=lambda c: counts[c]) if counts else "EUR"

    per_month = {p: {"in": Decimal("0"), "out": Decimal("0"), "nin": 0, "nout": 0} for p in periods}
    pending_in = pending_out = Decimal("0")
    pending_count = 0
    others: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"in": Decimal("0"), "out": Decimal("0")}
    )

    for index in kept:
        movement = movements[index]
        if movement.currency != main_currency:
            others[movement.currency]["in" if movement.is_credit else "out"] += movement.amount
            continue
        if not movement.is_final:
            pending_count += 1
            if movement.is_credit:
                pending_in += movement.amount
            else:
                pending_out += movement.amount
            continue
        bucket = per_month[movement.period]
        if movement.is_credit:
            bucket["in"] += movement.amount
            bucket["nin"] += 1
        else:
            bucket["out"] += movement.amount
            bucket["nout"] += 1

    return _Totals(
        currency=main_currency,
        months=[
            BankFlowMonth(
                period=p,
                inflow=per_month[p]["in"],
                outflow=per_month[p]["out"],
                net=per_month[p]["in"] - per_month[p]["out"],
                inflow_count=int(per_month[p]["nin"]),
                outflow_count=int(per_month[p]["nout"]),
            )
            for p in periods
        ],
        pending_count=pending_count,
        pending_inflow=pending_in,
        pending_outflow=pending_out,
        transfers_count=len(pairs),
        transfers_amount=sum((movements[i].amount for i in pairs), Decimal("0")),
        other_currencies=[
            BankFlowCurrencyTotal(currency=c, inflow=v["in"], outflow=v["out"])
            for c, v in sorted(others.items())
        ],
    )


def compute_real_flows(
    session: Session,
    user_uuid: str,
    master_key: str,
    months: int = DEFAULT_MONTHS,
    exclude_internal_transfers: bool = True,
    today: date | None = None,
    account_id: str | None = None,
) -> BankFlowsResponse:
    """Aggregate what actually moved, month by month, over the last `months`.

    `account_id` narrows the totals to one account; transfers are still paired
    against all of them.
    """
    months = max(1, min(months, MAX_MONTHS))
    anchor = today or date.today()
    periods = _months_back(anchor, months)

    accounts = _user_accounts(session, user_uuid, master_key)
    scope = _scope(accounts, account_id, master_key)
    if not scope:
        return _empty(periods)

    movements, transfer_legs = _paired_movements(
        session, master_key, accounts.readable, periods, exclude_internal_transfers,
    )
    window, in_scope = set(periods), set(scope)
    selected = [
        i for i, m in enumerate(movements) if m.period in window and m.account_bidx in in_scope
    ]
    totals = _aggregate(movements, transfer_legs, selected, periods)

    total_in = sum((m.inflow for m in totals.months), Decimal("0"))
    total_out = sum((m.outflow for m in totals.months), Decimal("0"))
    # Averaged over the months that actually carry data, not over the window:
    # dividing a three-month history by twelve reads as a 75 % drop in income.
    covered = sum(1 for m in totals.months if m.inflow_count or m.outflow_count) or 1

    return BankFlowsResponse(
        currency=totals.currency,
        months=totals.months,
        inflow=total_in,
        outflow=total_out,
        net=total_in - total_out,
        monthly_inflow=total_in / covered,
        monthly_outflow=total_out / covered,
        covered_months=covered,
        account_count=len(scope),
        # Named, not just counted: a total across several accounts is only
        # trustworthy once the reader can see which ones it is made of — and
        # which one is missing when the figures look too big.
        account_names=sorted(
            decrypt_data(accounts.by_bidx[bidx].name_enc, master_key) for bidx in scope
        ),
        internal_transfers_excluded=totals.transfers_count,
        internal_transfers_amount=totals.transfers_amount,
        pending_count=totals.pending_count,
        pending_inflow=totals.pending_inflow,
        pending_outflow=totals.pending_outflow,
        other_currencies=totals.other_currencies,
    )


def list_month_transactions(
    session: Session,
    user_uuid: str,
    master_key: str,
    period: str,
    account_id: str | None = None,
) -> BankTransactionsResponse:
    """Every operation of one "YYYY-MM" month, newest first, with the month's
    totals computed exactly as `compute_real_flows` computes that month.

    Nothing is dropped from the list: an internal transfer, a pending operation
    or a foreign-currency one is flagged rather than hidden, so the list always
    adds up to what the bank app shows.
    """
    accounts = _user_accounts(session, user_uuid, master_key)
    scope = _scope(accounts, account_id, master_key)
    if not scope:
        return _empty_month(period)

    movements, transfer_legs = _paired_movements(
        session, master_key, accounts.readable, [period], pair_transfers=True,
    )
    in_scope = set(scope)
    selected = [
        i for i, m in enumerate(movements) if m.period == period and m.account_bidx in in_scope
    ]
    totals = _aggregate(movements, transfer_legs, selected, [period])
    [month] = totals.months

    names = {
        bidx: decrypt_data(account.name_enc, master_key)
        for bidx, account in accounts.by_bidx.items()
    }

    def item(index: int) -> BankTransactionItem:
        movement = movements[index]
        counterpart = movements[transfer_legs[index]] if index in transfer_legs else None
        row = movement.row
        return BankTransactionItem(
            id=row.uuid,
            account_id=accounts.by_bidx[movement.account_bidx].uuid,
            account_name=names[movement.account_bidx],
            operation_date=movement.day,
            amount=movement.amount,
            currency=movement.currency,
            is_credit=movement.is_credit,
            is_pending=not movement.is_final,
            label=decrypt_data(row.remittance_enc, master_key) if row.remittance_enc else None,
            transfer_account_id=(
                accounts.by_bidx[counterpart.account_bidx].uuid if counterpart else None
            ),
            transfer_account_name=names[counterpart.account_bidx] if counterpart else None,
        )

    return BankTransactionsResponse(
        period=period,
        currency=totals.currency,
        inflow=month.inflow,
        outflow=month.outflow,
        net=month.net,
        internal_transfers_excluded=totals.transfers_count,
        internal_transfers_amount=totals.transfers_amount,
        pending_count=totals.pending_count,
        pending_inflow=totals.pending_inflow,
        pending_outflow=totals.pending_outflow,
        other_currencies=totals.other_currencies,
        transactions=[item(i) for i in reversed(selected)],
    )


def _empty(periods: list[str]) -> BankFlowsResponse:
    return BankFlowsResponse(
        currency="EUR",
        months=[
            BankFlowMonth(period=p, inflow=Decimal("0"), outflow=Decimal("0"), net=Decimal("0"))
            for p in periods
        ],
        inflow=Decimal("0"),
        outflow=Decimal("0"),
        net=Decimal("0"),
        monthly_inflow=Decimal("0"),
        monthly_outflow=Decimal("0"),
        covered_months=0,
        account_count=0,
        account_names=[],
        internal_transfers_excluded=0,
        internal_transfers_amount=Decimal("0"),
        pending_count=0,
        pending_inflow=Decimal("0"),
        pending_outflow=Decimal("0"),
        other_currencies=[],
    )


def _empty_month(period: str) -> BankTransactionsResponse:
    return BankTransactionsResponse(
        period=period,
        currency="EUR",
        inflow=Decimal("0"),
        outflow=Decimal("0"),
        net=Decimal("0"),
        internal_transfers_excluded=0,
        internal_transfers_amount=Decimal("0"),
        pending_count=0,
        pending_inflow=Decimal("0"),
        pending_outflow=Decimal("0"),
        other_currencies=[],
        transactions=[],
    )
