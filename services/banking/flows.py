"""
Observed cash flows: what actually moved on the linked accounts.

The counterpart of `services/cashflow.py`, which holds what the user *declared*
would move. Everything here is derived from stored `BankTransaction` rows, so it
needs no network and no Enable Banking credentials.

Deliberately bank-agnostic. It reads only the three fields the Enable Banking
contract marks required on every transaction — amount, currency and
`credit_debit_indicator` — plus the status. It never parses a label: the
`remittance_information` format is the bank's own invention (Boursorama writes
`CARTE 03/08/25 AIRBNB * HMFYWK533K`, another writes something else), and the
structured fields that would replace it — `merchant_category_code`,
`bank_transaction_code`, `creditor` — are empty on all 4 240 real rows captured
so far.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlmodel import Session, select

from dtos.banking import (
    BankFlowCurrencyTotal,
    BankFlowMonth,
    BankFlowsResponse,
)
from models.banking import BankAccountLink, BankTransaction
from services.banking.transactions import FINAL_STATUSES
from services.encryption import decrypt_data, hash_index

logger = logging.getLogger(__name__)

CREDIT = "CRDT"

# How far apart the two legs of one internal transfer may be dated. Banks book
# the debit and the matching credit on the same day as a rule, but a value date
# rolling over a weekend is common enough to cost real matches at zero tolerance.
TRANSFER_DATE_TOLERANCE_DAYS = 3

DEFAULT_MONTHS = 12
MAX_MONTHS = 120


class _Movement(NamedTuple):
    account_bidx: str
    period: str
    day: date | None
    amount: Decimal
    currency: str
    is_credit: bool
    is_final: bool


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


def _row_day(row: BankTransaction, master_key: str) -> date | None:
    """Same fallback order `normalize_transaction` applied when writing."""
    for column in (row.booking_date_enc, row.transaction_date_enc, row.value_date_enc):
        if column:
            return date.fromisoformat(decrypt_data(column, master_key))
    return None


def _internal_transfer_legs(movements: list[_Movement]) -> set[int]:
    """Indexes of movements that pair up as one transfer between the user's own
    accounts.

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

    paired: set[int] = set()
    for index, movement in enumerate(movements):
        if movement.is_credit or movement.day is None:
            continue
        for candidate in credits_by_key.get((movement.currency, movement.amount), ()):
            if candidate in paired:
                continue
            other = movements[candidate]
            if other.account_bidx == movement.account_bidx or other.day is None:
                continue
            if abs((other.day - movement.day).days) > TRANSFER_DATE_TOLERANCE_DAYS:
                continue
            paired.add(candidate)
            paired.add(index)
            break
    return paired


def compute_real_flows(
    session: Session,
    user_uuid: str,
    master_key: str,
    months: int = DEFAULT_MONTHS,
    exclude_internal_transfers: bool = True,
    today: date | None = None,
) -> BankFlowsResponse:
    """Aggregate what actually moved, month by month, over the last `months`."""
    months = max(1, min(months, MAX_MONTHS))
    anchor = today or date.today()
    periods = _months_back(anchor, months)

    user_bidx = hash_index(user_uuid, master_key)
    # `BankAccountLink.bank_account_uuid_bidx` and `BankTransaction.account_id_bidx`
    # are the same blind index of the same CapitalView account uuid.
    account_bidxs = [
        link.bank_account_uuid_bidx
        for link in session.exec(
            select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == user_bidx)
        ).all()
    ]
    if not account_bidxs:
        return _empty(periods, exclude_internal_transfers)

    period_bidx_to_period = {hash_index(p, master_key): p for p in periods}
    rows = session.exec(
        select(BankTransaction).where(
            BankTransaction.account_id_bidx.in_(account_bidxs),  # type: ignore[attr-defined]
            BankTransaction.period_bidx.in_(list(period_bidx_to_period)),  # type: ignore[attr-defined]
        )
    ).all()

    movements = [
        _Movement(
            account_bidx=row.account_id_bidx,
            period=period_bidx_to_period[row.period_bidx],
            day=_row_day(row, master_key),
            amount=Decimal(decrypt_data(row.amount_enc, master_key)),
            currency=decrypt_data(row.currency_enc, master_key),
            is_credit=decrypt_data(row.credit_debit_enc, master_key) == CREDIT,
            is_final=decrypt_data(row.status_enc, master_key) in FINAL_STATUSES,
        )
        for row in rows
    ]

    transfer_legs = _internal_transfer_legs(movements) if exclude_internal_transfers else set()
    transfers_amount = sum(
        (movements[i].amount for i in transfer_legs if movements[i].is_credit),
        Decimal("0"),
    )

    # The currency the headline totals speak. Picking the most frequent one keeps
    # a stray foreign-currency movement from silently joining a euro total —
    # amounts arrive unconverted, with no exchange rate attached.
    counts: dict[str, int] = defaultdict(int)
    for index, movement in enumerate(movements):
        if index not in transfer_legs:
            counts[movement.currency] += 1
    main_currency = max(counts, key=lambda c: counts[c]) if counts else "EUR"

    per_month = {p: {"in": Decimal("0"), "out": Decimal("0"), "nin": 0, "nout": 0} for p in periods}
    pending_in = pending_out = Decimal("0")
    pending_count = 0
    others: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"in": Decimal("0"), "out": Decimal("0")}
    )

    for index, movement in enumerate(movements):
        if index in transfer_legs:
            continue
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

    month_rows = [
        BankFlowMonth(
            period=p,
            inflow=per_month[p]["in"],
            outflow=per_month[p]["out"],
            net=per_month[p]["in"] - per_month[p]["out"],
            inflow_count=int(per_month[p]["nin"]),
            outflow_count=int(per_month[p]["nout"]),
        )
        for p in periods
    ]
    total_in = sum((m.inflow for m in month_rows), Decimal("0"))
    total_out = sum((m.outflow for m in month_rows), Decimal("0"))

    # Averaged over the months that actually carry data, not over the window:
    # dividing a three-month history by twelve reads as a 75 % drop in income.
    covered = sum(1 for m in month_rows if m.inflow_count or m.outflow_count) or 1

    return BankFlowsResponse(
        currency=main_currency,
        months=month_rows,
        inflow=total_in,
        outflow=total_out,
        net=total_in - total_out,
        monthly_inflow=total_in / covered,
        monthly_outflow=total_out / covered,
        covered_months=covered,
        account_count=len(account_bidxs),
        internal_transfers_excluded=len(transfer_legs) // 2,
        internal_transfers_amount=transfers_amount,
        pending_count=pending_count,
        pending_inflow=pending_in,
        pending_outflow=pending_out,
        other_currencies=[
            BankFlowCurrencyTotal(currency=c, inflow=v["in"], outflow=v["out"])
            for c, v in sorted(others.items())
        ],
    )


def _empty(periods: list[str], exclude_internal_transfers: bool) -> BankFlowsResponse:
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
        internal_transfers_excluded=0,
        internal_transfers_amount=Decimal("0"),
        pending_count=0,
        pending_inflow=Decimal("0"),
        pending_outflow=Decimal("0"),
        other_currencies=[],
    )
