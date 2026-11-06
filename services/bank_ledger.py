"""
The balance of an account no bank feeds, derived from its operations.

On such an account the operations are the only source of truth: its balance is
the sum of all of them from zero, its curve their running total, day by day.
`balance_enc` and the `account_history` rows are a cache this module rewrites
whole after every write. Nobody types a balance: money already on the account
when it is opened is its first operation. See docs/bank-ledger.md.

A synced account is left alone everywhere here: the bank's word is its balance.

Rules: docs/bank-ledger.md — L1 to L9; L7 for the opening adjustment.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.bank import BankBalanceResponse, BankEntryRequest, BankEntryResponse, BankHistoryEntry
from models.account_history import AccountHistory
from models.bank import BankAccount
from models.banking import BankAccountLink, BankTransaction
from models.currency import BASE_CURRENCY
from services.banking.transactions import (
    ORIGIN_ADJUSTMENT,
    ORIGIN_MANUAL,
    is_adjustment,
    rewrite_entry,
    row_date,
    signed_row_amount,
    store_entry,
)
from services.encryption import decrypt_data, encrypt_data, hash_index

logger = logging.getLogger(__name__)

LEDGER_VERSION = 1

ADJUSTMENT_LABEL = "Ajustement de solde"
OPENING_LABEL = "Solde d'ouverture"


class SyncedAccountError(ValueError):
    """The account's balance is the bank's: it takes no entry by hand."""


def is_synced(session: Session, account: BankAccount, master_key: str) -> bool:
    return session.exec(
        select(BankAccountLink.uuid).where(
            BankAccountLink.bank_account_uuid_bidx == hash_index(account.uuid, master_key)
        )
    ).first() is not None


def _rows(session: Session, account: BankAccount, master_key: str) -> list[BankTransaction]:
    return list(session.exec(
        select(BankTransaction).where(BankTransaction.account_id_bidx == hash_index(account.uuid, master_key))
    ).all())


def _dated(session: Session, account: BankAccount, master_key: str) -> list[tuple[date, Decimal, BankTransaction]]:
    out = []
    for row in _rows(session, account, master_key):
        day = row_date(row, master_key)
        if day is not None:
            out.append((day, signed_row_amount(row, master_key), row))
    return out


def balance_at(session: Session, account: BankAccount, day: date, master_key: str) -> Decimal:
    """What the operations add up to at the end of `day`."""
    return sum((amount for d, amount, _ in _dated(session, account, master_key) if d <= day), Decimal("0"))


def rebuild_from_operations(session: Session, account: BankAccount, master_key: str) -> None:
    """Rewrite the account's whole curve and balance from its operations.

    The curve runs from the first operation — or the opening, or the creation,
    whichever is earliest — to yesterday, like every curve of the store; the
    balance counts every operation, today's included.
    """
    from services.bank import replace_history_window

    if is_synced(session, account, master_key):
        return
    dated = _dated(session, account, master_key)
    by_day: dict[date, Decimal] = {}
    for day, amount, _ in dated:
        by_day[day] = by_day.get(day, Decimal("0")) + amount

    yesterday = date.today() - timedelta(days=1)
    starts = [account.created_at.date()]
    if account.opened_at:
        starts.append(account.opened_at)
    if by_day:
        starts.append(min(by_day))
    start = min(starts)

    entries: list[BankHistoryEntry] = []
    running = Decimal("0")
    day = start
    while day <= yesterday:
        running += by_day.get(day, Decimal("0"))
        entries.append(BankHistoryEntry(snapshot_date=day, value=running))
        day += timedelta(days=1)

    session.exec(
        sa.delete(AccountHistory).where(AccountHistory.account_id_bidx == hash_index(account.uuid, master_key))
    )
    account.balance_enc = encrypt_data(str(sum(by_day.values(), Decimal("0"))), master_key)
    account.balance_updated_at = date.today()
    session.add(account)
    if entries:
        replace_history_window(session, account, entries, master_key, start, yesterday)
    session.commit()


class Opening(NamedTuple):
    """The balance carried from before the operations: the earliest adjustment
    of the account, when no real operation is dated before it. It states what
    the account held at the end of its day; whatever is written before that
    day keeps that statement true (docs/bank-ledger.md)."""
    day: date
    amount: Decimal
    row: BankTransaction


class Carried(NamedTuple):
    """What the opening adjustment became once something was written before it."""
    day: date
    amount: Decimal
    label: str | None
    to_day: date
    to_amount: Decimal


def opening_adjustment(session: Session, account: BankAccount, master_key: str) -> Opening | None:
    dated = _dated(session, account, master_key)
    adjustments = sorted(
        ((day, amount, row) for day, amount, row in dated if is_adjustment(row, master_key)),
        key=lambda found: (found[0], found[2].uuid),
    )
    if not adjustments:
        return None
    day, amount, row = adjustments[0]
    if any(d < day for d, _, r in dated if not is_adjustment(r, master_key)):
        return None
    return Opening(day, amount, row)


def carried_opening(opening: Opening, added: Decimal, to_day: date, master_key: str) -> Carried:
    """Where the opening adjustment goes once `added` (signed) has been written
    on or before its day: back to `to_day` when that is earlier, less what was
    added — so the balance it stated on its day does not move."""
    label = decrypt_data(opening.row.remittance_enc, master_key) if opening.row.remittance_enc else None
    return Carried(opening.day, opening.amount, label, min(opening.day, to_day), opening.amount - added)


def carry_opening(
    session: Session, account: BankAccount, master_key: str, added: Decimal, to_day: date,
    opening: Opening | None = None,
) -> Carried | None:
    """Rewrite the opening adjustment as `carried_opening` says, uncommitted;
    None when the account has none. A carried adjustment that comes to zero
    is dropped: nothing is left to carry."""
    opening = opening or opening_adjustment(session, account, master_key)
    if opening is None:
        return None
    carried = carried_opening(opening, added, to_day, master_key)
    if carried.to_amount == 0:
        session.delete(opening.row)
    else:
        rewrite_entry(
            session, master_key, opening.row, carried.to_day, carried.to_amount, carried.label,
            _currency(account, master_key), ORIGIN_ADJUSTMENT,
        )
    session.flush()
    return carried


def adjustments_between(
    session: Session, account: BankAccount, master_key: str, start: date | None, end: date,
) -> list[tuple[date, Decimal, BankTransaction]]:
    """The adjustments dated in [start, end] (from the first one when `start`
    is None): what real operations of that period replace. The opening
    adjustment is never among them: it is carried, not replaced."""
    opening = opening_adjustment(session, account, master_key)
    return [
        (day, amount, row) for day, amount, row in _dated(session, account, master_key)
        if is_adjustment(row, master_key) and (start is None or day >= start) and day <= end
        and (opening is None or row.uuid != opening.row.uuid)
    ]


def drop_adjustments(
    session: Session, account: BankAccount, master_key: str, start: date | None, end: date,
) -> int:
    rows = adjustments_between(session, account, master_key, start, end)
    for _, _, row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def _currency(account: BankAccount, master_key: str) -> str:
    from services.bank import account_currency

    return account_currency(account, master_key)


# ---------------------------------------------------------------------------
# Entries by hand
# ---------------------------------------------------------------------------


class OperationNotDeletableError(ValueError):
    """An operation the bank reported: the next sync would bring it back."""


def add_entry(
    session: Session, account: BankAccount, entry: BankEntryRequest, master_key: str
) -> BankEntryResponse:
    """An operation typed by hand."""
    if is_synced(session, account, master_key):
        raise SyncedAccountError("Le solde de ce compte est lu à la banque : il ne se saisit pas à la main.")
    ensure_ledgers(session, account.user_uuid_bidx, master_key)
    opening = opening_adjustment(session, account, master_key)
    row = store_entry(
        session, master_key, account.uuid, entry.day, entry.amount, entry.label,
        _currency(account, master_key), ORIGIN_MANUAL,
    )
    # Dated before the opening balance: that balance stays what it was.
    if opening is not None and entry.day <= opening.day:
        carry_opening(session, account, master_key, entry.amount, entry.day - timedelta(days=1), opening)
    session.commit()
    rebuild_from_operations(session, account, master_key)
    return BankEntryResponse(id=row.uuid)


def balance_on(session: Session, account: BankAccount, day: date, master_key: str) -> BankBalanceResponse:
    """The balance at the end of `day` and today, in the account's currency."""
    if is_synced(session, account, master_key):
        raise SyncedAccountError("Le solde de ce compte est lu à la banque : il ne se saisit pas à la main.")
    ensure_ledgers(session, account.user_uuid_bidx, master_key)
    dated = _dated(session, account, master_key)
    return BankBalanceResponse(
        day=day,
        balance_on_day=sum((amount for d, amount, _ in dated if d <= day), Decimal("0")),
        balance_now=sum((amount for _, amount, _ in dated), Decimal("0")),
    )


def delete_operation(session: Session, account: BankAccount, row: BankTransaction, master_key: str) -> None:
    """Any operation of an unsynced account; on a synced one, only what was
    imported before the bank's own history."""
    link = session.exec(
        select(BankAccountLink).where(BankAccountLink.bank_account_uuid_bidx == hash_index(account.uuid, master_key))
    ).first()
    if link is not None:
        starts = (
            date.fromisoformat(decrypt_data(link.history_served_from_enc, master_key))
            if link.history_served_from_enc else None
        )
        day = row_date(row, master_key)
        if starts is not None and (day is None or day >= starts):
            raise OperationNotDeletableError(
                "Cette opération vient de la banque : la prochaine synchronisation la ramènerait."
            )
    session.delete(row)
    session.commit()
    rebuild_from_operations(session, account, master_key)


# ---------------------------------------------------------------------------
# Conversion of the accounts that predate the ledger
# ---------------------------------------------------------------------------


def ensure_ledgers(session: Session, user_bidx: str, master_key: str) -> int:
    """Convert every unsynced account of the user still on a stored balance.
    Idempotent; returns how many were converted."""
    pending = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx, BankAccount.ledger_version.is_(None))  # type: ignore[union-attr]
    ).all()
    if not pending:
        return 0
    linked = set(session.exec(
        select(BankAccountLink.bank_account_uuid_bidx).where(BankAccountLink.user_uuid_bidx == user_bidx)
    ).all())
    converted = 0
    for account in pending:
        if hash_index(account.uuid, master_key) in linked:
            continue
        _convert(session, account, master_key)
        converted += 1
    if converted:
        # A count, never an amount: the server holds no clear figure.
        logger.info("bank_ledger: converted %d account(s)", converted)
    return converted


def refresh_ledgers(session: Session, user_bidx: str, master_key: str) -> None:
    """Convert what still needs it, then carry every unsynced curve to
    yesterday from the operations — never by repeating a stored balance."""
    ensure_ledgers(session, user_bidx, master_key)
    linked = set(session.exec(
        select(BankAccountLink.bank_account_uuid_bidx).where(BankAccountLink.user_uuid_bidx == user_bidx)
    ).all())
    yesterday = date.today() - timedelta(days=1)
    for account in session.exec(select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)).all():
        bidx = hash_index(account.uuid, master_key)
        if bidx in linked or account.ledger_version is None:
            continue
        last = session.exec(
            select(sa.func.max(AccountHistory.snapshot_date)).where(AccountHistory.account_id_bidx == bidx)
        ).one()
        if last is None or last < yesterday:
            rebuild_from_operations(session, account, master_key)


def on_ledger(session: Session, account: BankAccount, master_key: str) -> bool:
    """Whether the account's balance is derived from its operations."""
    return account.ledger_version is not None and not is_synced(session, account, master_key)


def _convert(session: Session, account: BankAccount, master_key: str) -> None:
    """Turn what the account held as a stored balance into adjustments, so the
    operations alone give it back.

    With real operations, they are the truth: only the balance the history held
    on the eve of the first one is carried, as an opening adjustment. With none,
    each day the history changed becomes an adjustment of that change — the
    same curve — and a balance typed since the last snapshot one more. With
    nothing at all, the stored balance on the opening day.
    """
    currency = _currency(account, master_key)
    real = [
        (day, amount) for day, amount, row in _dated(session, account, master_key)
        if not is_adjustment(row, master_key)
    ]
    history = _history(session, account, master_key, currency)

    def add(day: date, amount: Decimal, label: str) -> None:
        if amount != 0:
            store_entry(session, master_key, account.uuid, day, amount, label, currency, ORIGIN_ADJUSTMENT)

    if real:
        eve = min(day for day, _ in real) - timedelta(days=1)
        before = [value for day, value in history if day <= eve]
        add(eve, before[-1] if before else Decimal("0"), OPENING_LABEL)
    elif history:
        previous = Decimal("0")
        for day, value in history:
            if value != previous:
                add(day, value - previous, ADJUSTMENT_LABEL)
                previous = value
        stored = Decimal(decrypt_data(account.balance_enc, master_key))
        add(date.today(), stored - previous, ADJUSTMENT_LABEL)
    else:
        opened = account.opened_at or account.created_at.date()
        add(min(opened, date.today()), Decimal(decrypt_data(account.balance_enc, master_key)), OPENING_LABEL)

    account.ledger_version = LEDGER_VERSION
    session.add(account)
    session.flush()
    rebuild_from_operations(session, account, master_key)


def _history(session: Session, account: BankAccount, master_key: str, currency: str) -> list[tuple[date, Decimal]]:
    """The stored curve, back in the account's own currency: the store keeps
    euros (services.bank.curve_in_base_currency)."""
    from services.market import get_historical_exchange_rates_db

    rows = session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == hash_index(account.uuid, master_key))
        .order_by(AccountHistory.snapshot_date)
    ).all()
    values = [(row.snapshot_date, Decimal(decrypt_data(row.total_value_enc, master_key))) for row in rows]
    if currency == BASE_CURRENCY or not values:
        return values
    rates = get_historical_exchange_rates_db(session, currency, values[0][0], values[-1][0])
    return [(day, round(value / rates[day], 2) if rates.get(day) else value) for day, value in values]


def forget_ledger(account: BankAccount | None) -> None:
    """An account a bank stops feeding is converted again: its history, the
    bank's, gives the balance its operations start from."""
    if account is not None:
        account.ledger_version = None
