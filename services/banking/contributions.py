"""
What the user's own investment accounts prove about a transfer leaving the bank.

A transfer sent that nothing pairs asks its question: the bank names no account,
and none of the user's own holds the other leg (`flows._asks_flow`). But the
other leg is sometimes there, outside the banking tables — written by the user
on a stock or crypto account as a EUR deposit. Same day, same amount, it is the
same movement seen from the other side, and the question answers itself.

Measured on 53 months, 62 declared deposits against 3 421 final debits: on the
**same day**, same amount, among transfers sent, 51 matched one debit and one
only, 4 matched several, 7 matched none. Widening to three days made it worse —
50 unique, 6 ambiguous — so the deduction is made on the exact day alone.
Within `TOLERANCE_DAYS` a deposit is reported as a hint beside the question,
which the user answers; it types nothing by itself.

Two rows never stand for a transfer from the bank, because the app wrote them
itself and no money crossed the account's boundary: a stock account's automatic
provision, and a crypto account's EUR leg beside another row of its group (a
purchase's funding, a sale's proceeds). Both carry `is_auto_provision`; the rows
stored before that column carry their note or their group instead, and are
marked here the first time they are read, as the transfer patterns backfill the
label signatures.

Deposits are matched one for one: a single 200 € deposit cannot prove two 200 €
debits of the same day, and none of the two is typed — the user is shown the
deposit and settles it. Nothing is ever deduced from an amount alone.

A platform may keep a fee on the way: 100 € leave the bank, 99 € reach the
account. On the same day, a debit a little above a deposit (or a credit a
little below a withdrawal) proves it too, when each is the other's only fit.
Measured on the same history: 57 of the 62 deposits proved instead of 54, and
with every deposit moved two weeks to three months away, 0.30 coincidences a
trial instead of 0.25 — 2 € or 3 % proved no more, for 0.40.
Between two bank accounts nothing is kept on the way: the pairing stays exact.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from sqlmodel import Session, select

from models.crypto import CryptoAccount, CryptoTransaction
from models.stock import StockAccount, StockTransaction
from services.analytics.flows import AUTO_PROVISION_NOTE
from services.encryption import decrypt_data, hash_index

# How far a deposit may sit from a movement to be worth showing beside it. Three
# days holds the settlement delay of a transfer booked on a Friday; past it, the
# amounts on show are coincidences.
TOLERANCE_DAYS = 3

# What a platform may keep of a deposit, or of a withdrawal, on the way: the
# larger of a flat fee and a share of the amount.
FEE_MAX = Decimal("1")
FEE_MAX_SHARE = Decimal("0.02")

# The only currency an investment account holds cash in (docs/currencies.md).
_CASH_ASSET = "EUR"
_DEPOSIT = "DEPOSIT"
_WITHDRAW = "WITHDRAW"


@dataclass(frozen=True)
class Contribution:
    """One cash movement the user declared on an investment account."""
    account_name: str
    day: date
    amount: Decimal
    # A deposit faces a debit on the bank, a withdrawal faces a credit.
    is_deposit: bool


@dataclass(frozen=True)
class Match:
    """The contribution a bank movement was found to be.

    `exact` is what separates evidence from a hint: on the very day, and alone,
    it types the movement; near it, it is only shown to the user.
    """
    contribution: Contribution
    exact: bool


@dataclass
class Contributions:
    """A user's declared cash movements, keyed by what a bank movement knows."""
    # (is_deposit, amount) -> contributions, oldest first
    by_amount: dict[tuple[bool, Decimal], list[Contribution]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_amount)


def load_contributions(session: Session, user_uuid: str, master_key: str) -> Contributions:
    """Every EUR deposit and withdrawal the user declared, the app's own rows
    left out.

    The type, the amount and the date are all encrypted, so no filter can run
    in SQL: the rows of the user's investment accounts are read and sifted here.
    """
    user_bidx = hash_index(user_uuid, master_key)
    found: dict[tuple[bool, Decimal], list[Contribution]] = defaultdict(list)
    marked = False

    stock_names = _account_names(session, StockAccount, user_bidx, master_key)
    if stock_names:
        rows = session.exec(
            select(StockTransaction).where(
                StockTransaction.account_id_bidx.in_(stock_names)  # type: ignore[attr-defined]
            )
        ).all()
        for row in rows:
            if row.is_auto_provision:
                continue
            kind = decrypt_data(row.type_enc, master_key)
            if kind not in (_DEPOSIT, _WITHDRAW) or decrypt_data(row.asset_key_enc, master_key).upper() != _CASH_ASSET:
                continue
            note = decrypt_data(row.notes_enc, master_key) if row.notes_enc else None
            if kind == _DEPOSIT and (note or "").strip() == AUTO_PROVISION_NOTE:
                row.is_auto_provision = True
                session.add(row)
                marked = True
                continue
            _collect(found, row, stock_names[row.account_id_bidx], kind == _DEPOSIT, master_key)

    crypto_names = _account_names(session, CryptoAccount, user_bidx, master_key)
    if crypto_names:
        rows = session.exec(
            select(CryptoTransaction).where(
                CryptoTransaction.account_id_bidx.in_(crypto_names)  # type: ignore[attr-defined]
            )
        ).all()
        group_sizes: dict[str, int] = defaultdict(int)
        for row in rows:
            if row.group_uuid is not None:
                group_sizes[row.group_uuid] += 1
        for row in rows:
            if row.is_auto_provision:
                continue
            kind = decrypt_data(row.type_enc, master_key)
            if kind not in (_DEPOSIT, _WITHDRAW) or decrypt_data(row.asset_key_enc, master_key).upper() != _CASH_ASSET:
                continue
            if row.group_uuid is not None and group_sizes[row.group_uuid] > 1:
                row.is_auto_provision = True
                session.add(row)
                marked = True
                continue
            _collect(found, row, crypto_names[row.account_id_bidx], kind == _DEPOSIT, master_key)

    if marked:
        session.commit()
    return Contributions(by_amount={key: sorted(items, key=lambda c: c.day) for key, items in found.items()})


def _account_names(session: Session, model, user_bidx: str, master_key: str) -> dict[str, str]:
    """The user's accounts of one kind, keyed by the blind index its rows carry."""
    accounts = session.exec(select(model).where(model.user_uuid_bidx == user_bidx)).all()
    return {
        hash_index(account.uuid, master_key): decrypt_data(account.name_enc, master_key)
        for account in accounts
    }


def _collect(
    found: dict[tuple[bool, Decimal], list[Contribution]],
    row,
    account_name: str,
    is_deposit: bool,
    master_key: str,
) -> None:
    day = _day(decrypt_data(row.executed_at_enc, master_key))
    amount = _amount(decrypt_data(row.amount_enc, master_key))
    if day is None or amount is None or amount <= 0:
        return
    found[(is_deposit, amount)].append(
        Contribution(account_name=account_name, day=day, amount=amount, is_deposit=is_deposit)
    )


def _day(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _amount(value: str) -> Decimal | None:
    """Normalised, so a deposit stored as "200.0" meets a 200 € debit."""
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


class Candidate(NamedTuple):
    """A bank movement a contribution could be, as the matching reads it.

    Only the movements whose question the user would otherwise answer are
    offered: what types them is decided elsewhere (`cashflow_types.resolve_type`).
    """
    index: int
    day: date
    amount: Decimal
    is_credit: bool


def match_candidates(candidates: list[Candidate], contributions: Contributions) -> dict[int, Match]:
    """What each bank movement is, by its index: evidence, a hint, or absent.

    The exact day is settled first, across every movement, so a contribution
    proving one of them can never be a hint on another. A day holding more
    movements than contributions proves none of them: which one went to the
    investment account is exactly what is unknown.
    """
    if not contributions or not candidates:
        return {}

    matches: dict[int, Match] = {}
    # (is_deposit, amount, rank in its list): the contributions already spent as
    # evidence, which no other movement may claim again.
    claimed: set[tuple[bool, Decimal, int]] = set()

    groups: dict[tuple[bool, date, Decimal], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[(candidate.is_credit, candidate.day, candidate.amount)].append(candidate)

    for (is_credit, day, amount), members in sorted(groups.items()):
        key = (not is_credit, amount)
        same_day = [
            (rank, contribution)
            for rank, contribution in enumerate(contributions.by_amount.get(key, ()))
            if contribution.day == day
        ]
        if len(same_day) < len(members):
            continue
        for candidate, (rank, contribution) in zip(sorted(members), same_day):
            matches[candidate.index] = Match(contribution, exact=True)
            claimed.add((*key, rank))

    _match_fees(candidates, contributions, matches, claimed)

    for candidate in sorted(candidates):
        if candidate.index in matches:
            continue
        key = (not candidate.is_credit, candidate.amount)
        near = sorted(
            (
                (abs((contribution.day - candidate.day).days), contribution)
                for rank, contribution in enumerate(contributions.by_amount.get(key, ()))
                if abs((contribution.day - candidate.day).days) <= TOLERANCE_DAYS
                and (*key, rank) not in claimed
            ),
            # On the day and the gap alone: two contributions of the same amount
            # and day are interchangeable here, and never compared themselves.
            key=lambda pair: (pair[0], pair[1].day),
        )
        if near:
            matches[candidate.index] = Match(near[0][1], exact=False)
    return matches


def _match_fees(
    candidates: list[Candidate],
    contributions: Contributions,
    matches: dict[int, Match],
    claimed: set[tuple[bool, Decimal, int]],
) -> None:
    """On the same day, a movement and a contribution a fee apart, each the
    other's only fit: evidence as much as an exact amount."""
    open_by_day: dict[tuple[bool, date], list[tuple[tuple[bool, Decimal, int], Contribution]]] = defaultdict(list)
    for (is_deposit, amount), items in contributions.by_amount.items():
        for rank, contribution in enumerate(items):
            if (is_deposit, amount, rank) not in claimed:
                open_by_day[(is_deposit, contribution.day)].append(((is_deposit, amount, rank), contribution))

    fits: dict[int, list[tuple[tuple[bool, Decimal, int], Contribution]]] = {}
    takers: dict[tuple[bool, Decimal, int], int] = defaultdict(int)
    for candidate in candidates:
        if candidate.index in matches:
            continue
        fits[candidate.index] = [
            (ref, contribution) for ref, contribution in open_by_day.get((not candidate.is_credit, candidate.day), ())
            if _a_fee_apart(candidate, contribution)
        ]
        for ref, _ in fits[candidate.index]:
            takers[ref] += 1
    for index, found in fits.items():
        if len(found) == 1 and takers[found[0][0]] == 1:
            ref, contribution = found[0]
            matches[index] = Match(contribution, exact=True)
            claimed.add(ref)


def _a_fee_apart(candidate: Candidate, contribution: Contribution) -> bool:
    """A deposit arrives short of the debit, a withdrawal leaves short of itself."""
    kept = contribution.amount - candidate.amount if candidate.is_credit else candidate.amount - contribution.amount
    return Decimal("0") < kept <= max(FEE_MAX, FEE_MAX_SHARE * contribution.amount)
