"""
Bank synchronisation: anchors, reconciliation and the balance curve (spec §D).

This is what turns a bank balance from an extrapolation into a measurement.
`_apply_pending_cashflows` (services/bank.py) projects due cashflows onto a
stored balance, so a manually-entered account drifts until the next correction.
From here on, a linked account carries a balance read from the bank, a curve
rebuilt from the movements between two anchors, and a reconciliation check that
says whether that curve is exact.

Three things this module is deliberate about:

* **It is never wired into `get_user_bank_accounts`.** The Banque page would
  then wait on a network call to the bank at every load. The front reads
  `last_synced_at` from the accounts payload and calls `POST /banking/sync`
  after the render; the once-a-day cap is re-checked here, server-side, because
  the front is not an authority (§D1).
* **Order matters.** Cross-account deduplication (§E) is asymmetric: whichever
  account is synced first keeps the row, and §D4 builds each account's curve
  from the rows it kept. Card accounts therefore always sync last (ruling R12).
* **The accounting balance is authoritative.** Two balances coexist and the
  account-level currency is unusable; both are read the way §F prescribes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from dtos.bank import BankHistoryEntry
from dtos.banking import BankAccountSyncResult
from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession, BankTransaction
from services.bank import (
    RECONCILIATION_GAP,
    account_currency,
    RECONCILIATION_NOT_POSSIBLE,
    RECONCILIATION_OK,
    replace_history_window,
)
from services.banking.client import build_client
from services.banking.credentials import get_decrypted_credentials
from services.banking.errors import (
    BankingApiError,
    InvalidPeriodError,
    PaginationLimitExceededError,
    SessionInvalidError,
)
from services.banking.health import (
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_INVALID,
    STATUS_REVOKED,
    notify_user_expiring_consents,
    session_status_message,
)
from services.banking.linking import NotConfiguredError, is_card_account
from services.banking.transactions import (
    FINAL_STATUSES,
    STATUS_BOOKED,
    NormalizedTransaction,
    normalize_transaction,
    store_transactions,
)
from services.encryption import decrypt_data, encrypt_data, hash_index

logger = logging.getLogger(__name__)

# The first pass needs `strategy=longest` AND a deliberately ancient date_from:
# `longest` alone self-limits to two years despite its name, and omitting the
# lower bound loses years with no error at all (spec §B4).
SEED_DATE_FROM = date(2000, 1, 1)
SEED_STRATEGY = "longest"
# Later passes start at the anchor: the exhaustive strategy costs extra calls to
# the bank and raises the risk of hitting its quota.
INCREMENTAL_STRATEGY = "default"

# How far back pending rows are looked for when sizing the fetch window. A
# pending operation older than this has long since booked or vanished.
PENDING_LOOKBACK = timedelta(days=90)

# BalanceStatus member, referenced by NAME (CLBD = ISO20022 ClosingBooked, the
# accounting balance). Never by position in the list: the real-time balance
# XPCD comes first as often as not.
ACCOUNTING_BALANCE_TYPE = "CLBD"
# The one substitution allowed, and only on a card account (ruling R19): the
# real capture publishes a single OTHR balance there and no CLBD at all.
CARD_BALANCE_TYPE = "OTHR"


# Business error codes, never HTTP statuses (§B5), mapped onto the SessionStatus
# member the consent moves to. The link itself is always preserved.
_SESSION_STATUS_BY_CODE = {
    "EXPIRED_SESSION": STATUS_EXPIRED,
    "REVOKED_SESSION": STATUS_REVOKED,
    "CLOSED_SESSION": STATUS_CLOSED,
    "SESSION_DOES_NOT_EXIST": STATUS_INVALID,
    "WRONG_SESSION_STATUS": STATUS_INVALID,
}


class AccountingBalanceUnavailableError(Exception):
    """The bank published no accounting balance in the account's own currency.

    Not recoverable by guessing: taking the real-time balance instead would
    silently fold pending operations into the anchor, and taking a foreign
    currency amount would record francs as euros.
    """


def sync_user_accounts(
    session: Session,
    user_uuid: str,
    master_key: str,
    psu_context: dict[str, str] | None = None,
) -> list[BankAccountSyncResult]:
    """Synchronise every account this user has linked, in a stable order.

    Global by design (ruling R16): a per-account trigger would hand the ordering
    decision of R12 to the caller.
    """
    # Ruling R20: this is where a consent expiry gets announced, because this is
    # where a Master Key exists. Before the daily cap, so a capped call still
    # warns — the front calls this after every render. Never fatal to the sync:
    # a synchronisation that succeeded must not be reported as failed because a
    # notification could not be written.
    try:
        notify_user_expiring_consents(session, user_uuid, master_key)
    except Exception:
        # Rolled back, not merely logged: the failure this catches is most
        # likely the `session.commit()` that writes the Notification, which
        # leaves the session in a failed transaction. Without this the very next
        # statement — the link lookup below — would raise PendingRollbackError,
        # turning a warning that could not be written into the 500 this guard
        # exists to prevent.
        session.rollback()
        logger.exception("failed to notify expiring bank consents")

    user_bidx = hash_index(user_uuid, master_key)
    links = session.exec(
        select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == user_bidx)
    ).all()
    if not links:
        return []

    accounts = {
        hash_index(account.uuid, master_key): account
        for account in session.exec(
            select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
        ).all()
    }
    ordered = [
        (link, accounts[link.bank_account_uuid_bidx])
        for link in _in_sync_order(session, links, master_key)
        if link.bank_account_uuid_bidx in accounts
    ]

    today = date.today()
    if all(link.last_synced_at >= today for link, _ in ordered):
        # Nothing due: the cap is a no-op condition, so no credentials are read
        # and no client — hence no signed token — is built.
        return [_capped(account) for _, account in ordered]

    creds = get_decrypted_credentials(session, user_uuid, master_key)
    if creds is None:
        raise NotConfiguredError()

    marker_missing = _card_marker_missing(session, [link for link, _ in ordered], master_key)

    results = []
    with build_client(*creds, psu_context=psu_context) as client:
        for link, account in ordered:
            result = sync_account_link(session, user_uuid, master_key, link, account, client)
            result.card_marker_missing = marker_missing
            results.append(result)
    return results


def sync_account_link(
    session: Session,
    user_uuid: str,
    master_key: str,
    link: BankAccountLink,
    account: BankAccount,
    client: Any,
) -> BankAccountSyncResult:
    """The six steps of §D2, for one linked account."""
    today = date.today()
    result = BankAccountSyncResult(bank_account_uuid=account.uuid, status="synced")

    if link.last_synced_at >= today:
        result.status = "skipped_daily_cap"
        return result

    # A link starts life with last_synced_at one day before its bootstrap
    # anchor, and every successful sync sets both to today: the two dates being
    # apart is what marks an account that has never been seeded.
    seeding = link.last_synced_at < link.anchor_date
    # Ruling R19: a card account's movements live on the current account it
    # debits, so neither the check nor the curve can be built from what
    # deduplication leaves behind.
    not_reconcilable = is_card_account(session, link, master_key)
    currency = account_currency(account, master_key)
    uid = decrypt_data(link.account_uid_enc, master_key)
    window_start = _window_start(session, account, master_key, link.anchor_date, today)

    try:
        # 1. The accounting balance, never the real-time one. Strict CLBD for
        # regular accounts (§F); card accounts fall back to OTHR.
        accounting = _accounting_balance(
            client.get_balances(uid), currency, is_card=not_reconcilable
        )
        # 2. The movements, from a window that always re-includes pending rows.
        feed, fetched_from = _fetch(
            client,
            uid,
            SEED_DATE_FROM if seeding else window_start,
            SEED_STRATEGY if seeding else INCREMENTAL_STRATEGY,
        )
    except SessionInvalidError as exc:
        # The status the consent moved to decides the wording, mapped by member
        # name: the four ways a consent can be lost call for four different
        # instructions, and no raw vendor string reaches the user.
        result.status = "reconnect_required"
        result.detail = session_status_message(_mark_consent_lost(session, link, exc))
        return result
    except (BankingApiError, PaginationLimitExceededError, AccountingBalanceUnavailableError) as exc:
        result.status = "error"
        result.detail = str(exc)
        return result

    # 3. Deduplicate and store (§E). One malformed row never aborts a sync: it
    # is dropped and counted, and the reconciliation check below is what makes
    # the resulting hole visible instead of leaving it silent.
    raws: list[dict[str, Any]] = []
    parsed: list[NormalizedTransaction] = []
    for raw in feed:
        try:
            parsed.append(normalize_transaction(raw))
        except ValueError:
            result.malformed += 1
            continue
        raws.append(raw)

    # How far back the feed actually reached: the seeding pass asks for
    # everything, so its window is only known once the bank has answered.
    covered_from = min(
        [window_start] + [tx.effective_date for tx in parsed if tx.effective_date]
    )

    result.inserted, result.updated, result.skipped = store_transactions(
        session, user_uuid, master_key, account.uuid, raws
    )
    # Pruning is bounded by what the bank was actually asked for and answered —
    # never by what we wanted. A bank that refuses to serve beyond ninety days
    # is silent about older rows, and silence is not withdrawal.
    result.removed = _drop_vanished_pending(
        session, account, master_key, parsed, fetched_from, today
    )

    movements = _booked_movements(session, account, master_key, covered_from, today, currency)

    # 4. Reconciliation (§D3), with three outcomes rather than two (ruling R18).
    # Skipped on the seeding pass: its anchor is the manually-entered balance,
    # not a bank reading, so there is no comparable quantity to check against —
    # the seeded curve is derived from today's balance and holds by construction.
    gap = None
    if not_reconcilable:
        # Not a failure: decision 6 already separates a verified curve from an
        # estimated one, and an account whose movements are deduplicated onto
        # another is exactly one whose curve can only be estimated. Reporting it
        # as a gap would teach the user to ignore gaps, and the alert would be
        # worthless the day one is real.
        result.reconciliation_status = RECONCILIATION_NOT_POSSIBLE
    elif not seeding:
        gap = _reconciliation_gap(link, accounting, movements, master_key)
        result.reconciliation_gap = gap
        result.reconciliation_status = RECONCILIATION_GAP if gap else RECONCILIATION_OK

    # 5. The new anchor. Stored at a *day boundary*, not at the instant of the
    # call: the accounting balance minus everything already booked today. A row
    # the bank books later today carries the same booking date as one booked
    # before it, so no date could tell the two apart — leaving today's
    # movements out of the anchor and back into the next period is what keeps
    # the check from reporting a gap on entirely normal behaviour.
    link.anchor_date = today
    link.anchor_balance_enc = encrypt_data(
        str(accounting - movements.get(today, Decimal("0"))), master_key
    )
    link.last_synced_at = today
    link.last_reconciliation_gap_enc = (
        encrypt_data(str(gap), master_key) if gap is not None else None
    )
    session.add(link)
    account.balance_enc = encrypt_data(str(accounting), master_key)
    account.balance_updated_at = today
    session.add(account)
    session.commit()

    # 6. Rewrite the snapshots of the window just processed, and only those —
    # unless nothing reconcilable can be built (ruling R19). On a card account
    # the movements that survived deduplication are a fraction of the truth, so
    # a curve drawn from them would be flat and false; overwriting real
    # snapshots with it destroys data that decision 8 never licensed. The day's
    # balance is still exact, because it is the anchor.
    if not_reconcilable:
        result.detail = (
            "Courbe rétrospective non écrite : les mouvements de ce compte sont "
            "dédupliqués vers le compte courant."
        )
        return result

    result.snapshots_written = replace_history_window(
        session,
        account,
        _curve_entries(accounting, movements, covered_from, today),
        master_key,
        covered_from,
        today,
    )
    return result


# ---------------------------------------------------------------------------
# Ordering (ruling R12)
# ---------------------------------------------------------------------------


def _in_sync_order(
    session: Session, links: list[BankAccountLink], master_key: str
) -> list[BankAccountLink]:
    """Current accounts first, card accounts last.

    Cross-account deduplication keeps the row on whichever account was synced
    first, and §D4 rebuilds each account's curve from the rows it kept. On the
    real captures, syncing the card account first costs the current account 197
    of its 297 movements. A card account mirrors the current account it debits,
    so it is the one that can afford to lose them.
    """
    return sorted(links, key=lambda link: (is_card_account(session, link, master_key), link.uuid))


def _card_marker_missing(
    session: Session, links: list[BankAccountLink], master_key: str
) -> bool:
    """Whether several accounts are linked and none is recognised as a card one.

    R12's ordering, R18's third outcome and R19's "no curve" all hang on
    `cash_account_type`. The marker itself is confirmed on real Boursorama data
    (see `is_card_account`), but nothing says every bank spells a card account
    the same way. With two accounts and no marker the order degrades silently
    to uuid, which is the failure this flag exists to make loud. One linked
    account alone is not a signal: nothing is being ordered, and nothing can be
    deduplicated across accounts.
    """
    return len(links) > 1 and not any(
        is_card_account(session, link, master_key) for link in links
    )


# ---------------------------------------------------------------------------
# Reading the bank (§B4, §F)
# ---------------------------------------------------------------------------


def _balance_of_type(
    balances: list[dict[str, Any]], balance_type: str, currency: str
) -> dict[str, Any] | None:
    """The balance carrying `balance_type`, in `currency`, or None.

    Never by position, and never "the first balance of that type": a
    multi-currency account publishes one balance per currency under the same
    type, and reading the wrong one records francs as euros.
    """
    for balance in balances:
        if balance.get("balance_type") != balance_type:
            continue
        amount = balance.get("balance_amount") or {}
        if str(amount.get("currency") or "") != currency:
            continue
        return balance
    return None


def _accounting_balance_row(
    payload: dict[str, Any], currency: str, is_card: bool = False
) -> dict[str, Any]:
    """The balance object that carries the accounting balance, in `currency`.

    Strict CLBD, and the substitution is enumerated rather than open: a card
    account publishes **no** CLBD at all — the real capture holds one single
    `OTHR` balance — so `OTHR` is accepted for card accounts and nothing else.
    `XPCD` in particular is never a candidate: it is the real-time balance, and
    folding pending operations into an anchor is the exact silent substitution
    `AccountingBalanceUnavailableError` exists to forbid (§F, constraint 9).
    The fallback is narrow, named and logged — never a "first EUR balance wins".
    """
    balances = payload.get("balances", [])
    row = _balance_of_type(balances, ACCOUNTING_BALANCE_TYPE, currency)
    if row is not None:
        return row

    if is_card:
        row = _balance_of_type(balances, CARD_BALANCE_TYPE, currency)
        if row is not None:
            logger.warning(
                "no %s balance on this card account, falling back to %s (ruling R19)",
                ACCOUNTING_BALANCE_TYPE,
                CARD_BALANCE_TYPE,
            )
            return row

    raise AccountingBalanceUnavailableError(
        f"no {ACCOUNTING_BALANCE_TYPE} balance in {currency} for this account"
    )


def _accounting_balance(
    payload: dict[str, Any], currency: str, is_card: bool = False
) -> Decimal:
    """The accounting balance, matched by balance type and read in `currency`.

    Two balances coexist on checking accounts and the real-time one is published
    alongside; taking the first element of the list is wrong half the time (§F).
    """
    amount = _accounting_balance_row(payload, currency, is_card).get("balance_amount") or {}
    return Decimal(str(amount.get("amount")))


def _fetch(
    client: Any, uid: str, date_from: date, strategy: str
) -> tuple[list[dict[str, Any]], date]:
    """Walk the paginated feed, reframing once on the date the bank will serve.

    On WRONG_TRANSACTIONS_PERIOD the API states its earliest allowed date, so
    the seeding pass recovers instead of failing — Boursorama refuses anything
    older than ninety days in restricted production (§B4). Returns the rows
    along with the date the feed genuinely starts at, which is the only date
    range the answer can be read as authoritative over.
    """
    try:
        return list(client.iter_transactions(uid, date_from=date_from, strategy=strategy)), date_from
    except InvalidPeriodError as exc:
        earliest = exc.earliest_allowed_date
        if earliest is None or earliest <= date_from:
            raise
        return list(
            client.iter_transactions(uid, date_from=earliest, strategy=strategy)
        ), earliest


def _mark_consent_lost(session: Session, link: BankAccountLink, exc: SessionInvalidError) -> str:
    """The consent is gone; the rattachement is not. Only the session's status
    moves, so a reconnection can re-point this same link (§B5). Returns the
    status it moved to."""
    logger.info("consent lost on link %s: %s: %s", link.uuid, exc.code, exc.message)
    status = _SESSION_STATUS_BY_CODE.get(exc.code, STATUS_INVALID)
    bank_session = session.get(BankSession, link.session_uuid)
    if bank_session is None:
        return status
    bank_session.status = status
    session.add(bank_session)
    session.commit()
    return status


# ---------------------------------------------------------------------------
# The fetch window, and the pending rows it exists for
# ---------------------------------------------------------------------------


def _window_start(
    session: Session, account: BankAccount, master_key: str, anchor_date: date, today: date
) -> date:
    """Where the fetch starts: the anchor, pulled back to cover pending rows.

    A pending operation is never final — it can change amount, change reference
    or disappear entirely. A window that stopped at the anchor would leave an
    older pending row permanently unreachable: nothing could correct it, and an
    unrelated same-amount operation could claim it in its owner's absence.
    """
    scan_from = min(anchor_date, today) - PENDING_LOOKBACK
    pending = _pending_rows(session, account, master_key, scan_from, today)
    if not pending:
        return anchor_date
    return min([anchor_date] + [day for _, day in pending])


def _pending_rows(
    session: Session, account: BankAccount, master_key: str, start: date, end: date
) -> list[tuple[BankTransaction, date]]:
    """This account's non-final rows in a date range, with the date they carry."""
    rows = _rows_in_range(session, account, master_key, start, end)
    pending = []
    for row in rows:
        if decrypt_data(row.status_enc, master_key) in FINAL_STATUSES:
            continue
        day = _row_date(row, master_key)
        if day is not None and start <= day <= end:
            pending.append((row, day))
    return pending


def _drop_vanished_pending(
    session: Session,
    account: BankAccount,
    master_key: str,
    feed: list[NormalizedTransaction],
    fetched_from: date,
    today: date,
) -> int:
    """Remove pending rows the bank no longer reports (§E).

    A pending operation can simply disappear. Storing only ever adds or
    corrects, so without this a withdrawn operation would sit in the curve
    forever. Bounded to the window that was actually fetched: outside it the
    feed says nothing, and absence would not mean withdrawal.
    """
    refs = {
        hash_index(tx.entry_reference, master_key) for tx in feed if tx.entry_reference
    }
    dedups: set[str] = set()
    for tx in feed:
        if tx.dedup_key:
            dedups.add(hash_index(tx.dedup_key, master_key))
        for alternate in tx.alternate_dedup_keys:
            dedups.add(hash_index(alternate, master_key))

    removed = 0
    for row, _ in _pending_rows(session, account, master_key, fetched_from, today):
        if row.entry_ref_bidx in refs or row.dedup_bidx in dedups:
            continue
        session.delete(row)
        removed += 1
    if removed:
        session.commit()
    return removed


# ---------------------------------------------------------------------------
# The movements, the check and the curve (§D3, §D4)
# ---------------------------------------------------------------------------


def _booked_movements(
    session: Session,
    account: BankAccount,
    master_key: str,
    start: date,
    end: date,
    currency: str,
) -> dict[date, Decimal]:
    """Net signed amount per day in the account's own currency, booked only.

    Two exclusions, both required for the check to compare comparable
    quantities: pending operations, which the accounting balance does not
    contain (§D3), and rows in any other currency, which arrive without an
    exchange rate — adding Swiss francs to euros would make the check lie.

    The comparison stays in the account's currency all the way to the
    reconciliation. Converting first would turn every exchange-rate move into a
    reconciliation gap on an account that is behaving perfectly, which is
    exactly what ruling R18 exists to prevent.
    """
    net: dict[date, Decimal] = defaultdict(Decimal)
    for row in _rows_in_range(session, account, master_key, start, end):
        if decrypt_data(row.status_enc, master_key) != STATUS_BOOKED:
            continue
        if decrypt_data(row.currency_enc, master_key) != currency:
            continue
        day = _row_date(row, master_key)
        if day is None or not (start <= day <= end):
            continue
        amount = Decimal(decrypt_data(row.amount_enc, master_key))
        if decrypt_data(row.credit_debit_enc, master_key) == "CRDT":
            net[day] += amount
        else:
            net[day] -= amount
    return net


def _reconciliation_gap(
    link: BankAccountLink,
    accounting: Decimal,
    movements: dict[date, Decimal],
    master_key: str,
) -> Decimal | None:
    """`previous anchor + booked movements of the period = current anchor`.

    When it holds the period's curve is exact and can be presented as such.
    Otherwise the gap is returned, to be stored and dated by the sync that found
    it: it means a movement is missing or counted twice — the detector for the
    card / current-account double count, and for the deduplication fallback when
    a reference is absent.

    The period opens **on** the anchor day, not after it: the stored anchor is
    the closing balance of the day before, so the anchor day's own movements
    have never been counted — including the ones the bank booked after the
    previous sync had already read the balance.
    """
    previous = Decimal(decrypt_data(link.anchor_balance_enc, master_key))
    period = sum(
        (value for day, value in movements.items() if day >= link.anchor_date),
        Decimal("0"),
    )
    gap = accounting - (previous + period)
    return None if gap == 0 else gap


def _curve_entries(
    accounting: Decimal, movements: dict[date, Decimal], start: date, end: date
) -> list[BankHistoryEntry]:
    """Daily balances walked back from the accounting balance.

    `balance(d) = balance(today) - sum of the movements booked after d`. Today's
    own value is produced but never written: `replace_history_window` stops at
    yesterday, leaving pending operations time to settle (§D4).
    """
    entries = []
    running = accounting
    day = end
    while day >= start:
        entries.append(BankHistoryEntry(snapshot_date=day, value=running))
        running -= movements.get(day, Decimal("0"))
        day -= timedelta(days=1)
    return entries


# ---------------------------------------------------------------------------
# Reading rows without any date in clear (§A5)
# ---------------------------------------------------------------------------


def _rows_in_range(
    session: Session, account: BankAccount, master_key: str, start: date, end: date
) -> list[BankTransaction]:
    """This account's rows over a date range, fetched through period_bidx.

    A blind index only supports equality, so the months of the range are
    enumerated and queried with IN — never the whole account, which is exactly
    the performance trap `get_all_user_cashflows` fell into.
    """
    return list(
        session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account.uuid, master_key),
                BankTransaction.period_bidx.in_(_period_indexes(start, end, master_key)),
            )
        ).all()
    )


def _period_indexes(start: date, end: date, master_key: str) -> list[str]:
    indexes = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        indexes.append(hash_index(f"{year:04d}-{month:02d}", master_key))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return indexes


def _row_date(row: BankTransaction, master_key: str) -> date | None:
    """The date a stored row is placed on — the same fallback order
    `normalize_transaction` applied when it was written."""
    for column in (row.booking_date_enc, row.transaction_date_enc, row.value_date_enc):
        if column:
            return date.fromisoformat(decrypt_data(column, master_key))
    return None


def _capped(account: BankAccount) -> BankAccountSyncResult:
    return BankAccountSyncResult(bank_account_uuid=account.uuid, status="skipped_daily_cap")
