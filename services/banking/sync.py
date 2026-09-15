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
* **Accounts no longer depend on each other.** Cross-account deduplication was
  removed (see `transactions.py`), so each account's curve is built from its own
  rows alone and the sync order carries no meaning beyond reproducibility.
* **The accounting balance is authoritative.** Two balances coexist and the
  account-level currency is unusable; both are read the way §F prescribes.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, NamedTuple

from sqlmodel import Session, select

from database import get_engine
from dtos.bank import BankHistoryEntry, ReconciliationStatus
from dtos.banking import BankAccountSyncResult, SyncStatus
from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession, BankTransaction
from services.bank import (
    AVAILABLE_BALANCE_TYPE,
    account_currency,
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
    CREDIT,
    FINAL_STATUSES,
    STATUS_BOOKED,
    NormalizedTransaction,
    normalize_transaction,
    row_date,
    store_transactions,
)
from services.encryption import decrypt_data, encrypt_data, hash_index
from services.jobs import wait_for_lock

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

# How far back the curve is redrawn on every sync, whatever window the bank was
# asked for. A bank publishes an operation in its feed a day or two after its
# balance already counts it, and it arrives dated *before* the last sync: the
# days it belongs to were drawn without it and nothing would ever redraw them.
# Walking back is done from stored rows, so widening the window costs one query.
CURVE_REDRAW = timedelta(days=30)

# How long a balance reading must age before the check trusts it. A balance
# counts an operation up to a day or two before the transaction feed publishes
# it, so today's reading always disagrees with today's operations — and again,
# the other way round, the day the operation lands. The check therefore never
# looks at today's balance at all: it compares two readings this old against the
# movements stored between them. A missing operation is found two days late,
# which is the price of never crying wolf.
SETTLE_LAG = timedelta(days=2)

# How far apart the two readings must be. Wide enough that one day's noise
# cannot hide in it, narrow enough to date a gap to a single week.
CHECK_SPAN = timedelta(days=7)

# How long balance readings are kept. Comfortably past CHECK_LAG, so a fortnight
# without a sync still leaves something to check against.
CHECKPOINT_RETENTION = timedelta(days=60)

# BalanceStatus member, referenced by NAME (CLBD = ISO20022 ClosingBooked, the
# accounting balance). Never by position in the list: the real-time balance
# XPCD comes first as often as not.
ACCOUNTING_BALANCE_TYPE = "CLBD"
# The one substitution allowed on a card account (ruling R19): the real capture
# publishes a single OTHR balance there and no CLBD at all.
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

    Global by design (ruling R16): one trigger, one daily cap, one place where
    a consent expiry is announced while a Master Key is in hand.
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
    # Two syncs of one user must not overlap: right after a rattachement the
    # server seeds in the background while the front's post-render call arrives,
    # and both would read `last_synced_at` as yesterday — two full paginations,
    # two rewrites of the same curve. The second waits instead of skipping, then
    # reads links the first has already committed and finds them capped.
    with wait_for_lock(f"bank-sync:{user_bidx}", session.get_bind()):
        # Whatever the notification step loaded may predate the lock.
        session.expire_all()
        results = _sync_links(session, user_uuid, master_key, user_bidx, psu_context)
    # Rebuilt now rather than on the next read, which would otherwise pay for it.
    # A reader rebuilds anyway when this fails: never fatal to the sync.
    try:
        from services.banking.flows import transfer_patterns

        transfer_patterns(session, user_uuid, master_key)
    except Exception:
        session.rollback()
        logger.exception("failed to rebuild transfer patterns after sync")
    return results


def _sync_links(
    session: Session,
    user_uuid: str,
    master_key: str,
    user_bidx: str,
    psu_context: dict[str, str] | None,
) -> list[BankAccountSyncResult]:
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
        for link in _in_stable_order(links)
        if link.bank_account_uuid_bidx in accounts
    ]

    today = date.today()
    if all(_tried_today(link, today) for link, _ in ordered):
        # Nothing due: the cap is a no-op condition, so no credentials are read
        # and no client — hence no signed token — is built.
        return [_capped(account) for _, account in ordered]

    creds = get_decrypted_credentials(session, user_uuid, master_key)
    if creds is None:
        raise NotConfiguredError()

    results = []
    with build_client(*creds, psu_context=psu_context) as client:
        for link, account in ordered:
            results.append(
                sync_account_link(session, user_uuid, master_key, link, account, client)
            )
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
    result = BankAccountSyncResult(bank_account_uuid=account.uuid, status=SyncStatus.SYNCED)

    if _tried_today(link, today):
        result.status = SyncStatus.SKIPPED_DAILY_CAP
        return result

    # Read from the flag, not from a date comparison: the long fetch has either
    # brought history back or it has not, and only the fetch itself can say so.
    seeding = not link.history_seeded
    # Ruling R19: a card account's movements live on the current account it
    # debits, so neither the check nor the curve can be built from what
    # deduplication leaves behind.
    not_reconcilable = is_card_account(session, link, master_key)
    currency = account_currency(account, master_key)
    uid = decrypt_data(link.account_uid_enc, master_key)
    window_start = _window_start(session, account, master_key, link.anchor_date, today)

    try:
        # 1. The accounting balance, never the real-time one. CLBD first; card
        # accounts fall back to OTHR, any account to ITAV as a last resort (§F).
        accounting, balance_type = _accounting_balance(
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
        result.status = SyncStatus.RECONNECT_REQUIRED
        result.detail = session_status_message(_mark_consent_lost(session, link, exc))
        _record_failure(session, link, result.detail, today, master_key)
        return result
    except (BankingApiError, PaginationLimitExceededError, AccountingBalanceUnavailableError) as exc:
        result.status = SyncStatus.ERROR
        result.detail = str(exc)
        _record_failure(session, link, result.detail, today, master_key)
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

    result.inserted, result.updated, result.skipped = store_transactions(session, master_key, account.uuid, raws
    )
    # Pruning is bounded by what the bank was actually asked for and answered —
    # never by what we wanted. A bank that refuses to serve beyond ninety days
    # is silent about older rows, and silence is not withdrawal.
    result.removed = _drop_vanished_pending(
        session, account, master_key, parsed, fetched_from, today
    )

    # What the bank fills in on `balance_after_transaction`, counted and never
    # used: filled, the last row of a day *is* that day's balance, and a curve
    # could be read straight off the feed instead of walked back from one
    # anchor — exact even where a balance type is missing or an operation was
    # dropped. Measured on the real Boursorama production capture it is empty on
    # all 3 275 rows, so nothing is built on it until a bank is seen filling it.
    result.balance_after_rows = sum(
        1 for raw in raws if (raw.get("balance_after_transaction") or {}).get("amount") is not None
    )

    # The curve is redrawn further back than the fetch reached, and the check
    # compares against a reading older still: both need the movements of that
    # whole stretch, not only of the window the bank was asked for. Never before
    # the oldest operation the bank ever served, which is where this account's
    # own history starts — beyond it a redraw would flatten manual snapshots
    # over days the bank says nothing about.
    check_window = _check_window(read_checkpoints(link, master_key), today)
    served_from = (
        date.fromisoformat(decrypt_data(link.history_served_from_enc, master_key))
        if link.history_served_from_enc
        else None
    )
    curve_from = min(covered_from, today - CURVE_REDRAW)
    if check_window is not None:
        curve_from = min(curve_from, check_window[0].day)
    if served_from is not None:
        curve_from = max(curve_from, served_from)
    curve_from = min(curve_from, covered_from)
    movements = booked_movements(session, account, master_key, curve_from, today, currency)

    # An available balance is the accounting one minus what is currently
    # withheld, so the withheld part is added back before anything anchors on
    # it. Only what the bank actually reports as pending can be added back: a
    # bank that publishes ITAV and shares no pending rows — Revolut does exactly
    # that — leaves the net at zero and the figure uncorrected, which is the
    # whole reason the verdict below stays `estimated`.
    estimated = balance_type == AVAILABLE_BALANCE_TYPE
    pending_net = (
        _pending_net(session, account, master_key, today, currency)
        if estimated
        else Decimal("0")
    )
    accounting -= pending_net

    # 4. Reconciliation (§D3), with four outcomes rather than two (ruling R18).
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
        result.reconciliation_status = ReconciliationStatus.NOT_RECONCILABLE
    elif not seeding:
        # Two settled readings or no verdict at all (see _check_window).
        if check_window is not None:
            gap = _reconciliation_gap(*check_window, movements)
            result.reconciliation_gap = gap
        if estimated:
            # The gap is still computed and still stored: on an available
            # balance it is the only measurement of how far the two readings
            # drift apart, and the answer decides whether this account can
            # graduate back to a verified curve. It is the verdict, not the
            # number, that is held back — a card authorisation blocked one day
            # and booked the next produces a gap on a perfectly healthy account.
            result.reconciliation_status = ReconciliationStatus.ESTIMATED
        elif check_window is not None:
            result.reconciliation_status = (
                ReconciliationStatus.GAP if gap else ReconciliationStatus.RECONCILED
            )

    # 5. The new anchor. Stored at a *day boundary*, not at the instant of the
    # call: the accounting balance minus everything already booked today. A row
    # the bank books later today carries the same booking date as one booked
    # before it, so no date could tell the two apart — leaving today's
    # movements out of the anchor and back into the next period is what keeps
    # the check from reporting a gap on entirely normal behaviour.
    link.anchor_date = today
    anchor_balance = accounting - movements.get(today, Decimal("0"))
    link.anchor_balance_enc = encrypt_data(str(anchor_balance), master_key)
    # The same reading, kept: it is what a later sync compares against, once the
    # bank's publication delay has had time to resolve.
    _record_checkpoint(link, _Checkpoint(today, anchor_balance), master_key, today)
    link.last_synced_at = today
    link.last_sync_attempt_at = today
    link.last_sync_error_enc = None
    link.last_balance_type = balance_type
    result.balance_type = balance_type
    # The flag is earned, never merely spent: a seeding pass that comes back
    # empty — a bank still settling the authorization, a feed answered blank —
    # leaves it off, so the next sync asks for those years again instead of
    # writing them off for good.
    if seeding:
        if parsed:
            link.history_seeded = True
            _widen_history_served_from(link, parsed, master_key)
        else:
            result.detail = (
                "La banque n'a renvoyé aucune opération : l'historique reste à récupérer, "
                "la prochaine synchronisation le redemandera."
            )
    if estimated and result.detail is None:
        result.detail = (
            "Votre banque ne publie pas de solde comptable : la courbe est estimée "
            "à partir du solde disponible."
        )
    link.last_reconciliation_gap_enc = (
        encrypt_data(str(gap), master_key) if gap is not None else None
    )
    session.add(link)
    account.balance_enc = encrypt_data(str(accounting), master_key)
    account.balance_updated_at = today
    session.add(account)
    session.commit()

    # One line per sync, and the only place the three open questions about a
    # bank's feed can be answered from a running instance: which balance type it
    # actually publishes, how far an available balance drifts from the booked
    # movements, and whether it fills `balance_after_transaction`. No amount and
    # no identifier beyond the link's own uuid — this goes to the server log.
    logger.info(
        "sync %s: balance_type=%s gap=%s pending_net=%s balance_after_rows=%d/%d",
        link.uuid,
        balance_type,
        "none" if gap is None else gap,
        pending_net,
        result.balance_after_rows,
        len(raws),
    )

    # 6. Rewrite the snapshots of the window just processed, and only those —
    # unless nothing reconcilable can be built (ruling R19). A card account
    # publishes a single OTHR balance and no CLBD at all, and OTHR has no defined
    # meaning in the contract. A curve is walked back *from a balance* and the
    # reconciliation check compares *to a balance*: with none that can be named,
    # neither says anything. Measured on the real capture, walking back from the
    # OTHR of 0 of a debit-immédiat card invents +27 887 € eighteen months back —
    # the spending history read as a balance, added to the wealth curve.
    # The day's balance is still exact, because it is the anchor.
    if not_reconcilable:
        result.detail = (
            "Courbe non écrite : votre banque ne publie pas de solde comptable pour ce "
            "compte carte, seulement un solde de type OTHR — il n'y a rien à quoi "
            "rattacher une courbe."
        )
        return result

    # Native currency: `replace_history_window` converts, being the writer.
    result.snapshots_written = replace_history_window(
        session,
        account,
        curve_entries(accounting, movements, curve_from, today),
        master_key,
        curve_from,
        today,
    )
    return result


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def _in_stable_order(links: list[BankAccountLink]) -> list[BankAccountLink]:
    """A deterministic order, and nothing more.

    Card accounts used to be forced last (ruling R12) because cross-account
    deduplication kept a row on whichever account was stored first. That level
    is gone: no account's outcome depends on another's any more, so no role
    ordering is left to enforce.

    The sort stays because the link query has no ORDER BY, and Postgres
    guarantees no order without one — an unordered sync would make any future
    failure irreproducible.
    """
    return sorted(links, key=lambda link: link.uuid)


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


def _published_balances(balances: list[dict[str, Any]]) -> str:
    """`type/currency` of everything the bank did publish, for the refusal message.

    Which balance types an ASPSP publishes is not in any contract and varies by
    account kind, so a refusal that only names what was expected leaves nobody
    able to say what to accept instead. Amounts are deliberately left out: this
    string reaches the user's screen through `BankAccountSyncResult.detail`.
    """
    if not balances:
        return "aucun"
    seen = []
    for balance in balances:
        amount = balance.get("balance_amount") or {}
        pair = f"{balance.get('balance_type') or '?'}/{amount.get('currency') or '?'}"
        if pair not in seen:
            seen.append(pair)
    return ", ".join(seen)


def accounting_balance_row(
    payload: dict[str, Any], currency: str, is_card: bool = False
) -> dict[str, Any]:
    """The balance object that carries the accounting balance, in `currency`.

    CLBD first, always, and the substitutions are enumerated rather than open:

    * `OTHR`, on a card account only — it publishes **no** CLBD at all, the real
      capture holds one single `OTHR` balance (ruling R19).
    * `ITAV`, as a last resort — some banks publish no accounting balance on any
      account. Revolut's PSD2 implementation returns a single `InterimAvailable`
      and nothing else, so the choice is an estimated curve or no sync at all.
      An available balance has pending card authorisations already withheld from
      it, which is why the caller re-adds what it can see of them and why the
      reconciliation verdict downgrades to `estimated`.

    `XPCD` in particular is never a candidate: it is the real-time balance, with
    no offsetting rows to correct it with, and folding pending operations into
    an anchor is the exact silent substitution `AccountingBalanceUnavailableError`
    exists to forbid (§F, constraint 9). Every fallback is narrow, named and
    logged — never a "first EUR balance wins".
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

    row = _balance_of_type(balances, AVAILABLE_BALANCE_TYPE, currency)
    if row is not None:
        logger.warning(
            "no %s balance published, falling back to %s: the curve is estimated",
            ACCOUNTING_BALANCE_TYPE,
            AVAILABLE_BALANCE_TYPE,
        )
        return row

    raise AccountingBalanceUnavailableError(
        f"votre banque ne publie pas de solde comptable ({ACCOUNTING_BALANCE_TYPE}) "
        f"en {currency} pour ce compte. Soldes publiés : {_published_balances(balances)}."
    )


def _accounting_balance(
    payload: dict[str, Any], currency: str, is_card: bool = False
) -> tuple[Decimal, str]:
    """The accounting balance and the type it was read from, in `currency`.

    Two balances coexist on checking accounts and the real-time one is published
    alongside; taking the first element of the list is wrong half the time (§F).
    The type comes back with the amount because it decides how much the figure
    can be trusted, and the balances payload is out of reach everywhere else.
    """
    row = accounting_balance_row(payload, currency, is_card)
    amount = row.get("balance_amount") or {}
    return Decimal(str(amount.get("amount"))), str(row.get("balance_type") or "")


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


def _widen_history_served_from(
    link: BankAccountLink, parsed: list[NormalizedTransaction], master_key: str
) -> None:
    """Record how far back this seeding pass reached, never narrowing it.

    Widened rather than overwritten: a re-seed on a bank that caps later
    requests — Revolut serves ninety days once the consent is minutes old —
    brings back less than the first pass did, while every older operation is
    still stored and still drawn.
    """
    dates = [tx.effective_date for tx in parsed if tx.effective_date]
    if not dates:
        return
    oldest = min(dates)
    if link.history_served_from_enc:
        oldest = min(oldest, date.fromisoformat(decrypt_data(link.history_served_from_enc, master_key)))
    link.history_served_from_enc = encrypt_data(oldest.isoformat(), master_key)


def _tried_today(link: BankAccountLink, today: date) -> bool:
    """Whether this link has already called the bank today, successfully or not.

    The cap used to read `last_synced_at` alone, which only a success moves: an
    account whose sync failed stayed due, and since the front triggers a sync
    after every render, each visit to the Banque page called the bank again for
    an answer that had not changed. A failure is final for the day; retrying
    early is a user's explicit decision (`retry_account_sync`).
    """
    return link.last_synced_at >= today or (
        link.last_sync_attempt_at is not None and link.last_sync_attempt_at >= today
    )


def _record_failure(
    session: Session, link: BankAccountLink, detail: str | None, today: date, master_key: str
) -> None:
    """Spend the day's attempt and keep the reason, so the page can still say why.

    The reason used to live only in the front store, from the response of the
    call that failed: a reload lost it, and getting it back meant calling the
    bank again. Encrypted, like every string that reaches the user from the
    bank's side.
    """
    link.last_sync_attempt_at = today
    link.last_sync_error_enc = encrypt_data(detail, master_key) if detail else None
    session.add(link)
    session.commit()


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


def _pending_net(
    session: Session, account: BankAccount, master_key: str, today: date, currency: str
) -> Decimal:
    """Net signed amount of everything still pending on this account.

    What an available balance (ITAV) has already withheld, as far as it can be
    seen: a card authorisation is subtracted from what the holder may spend long
    before it is booked. Subtracting this net — negative for the usual case of a
    blocked payment — turns the available balance back into the accounting one.

    Same currency filter and same window as everywhere else (§D3, ruling R18):
    a row in another currency arrives without a rate, and a pending row older
    than the lookback has long since booked or vanished.
    """
    net = Decimal("0")
    for row, _ in _pending_rows(
        session, account, master_key, today - PENDING_LOOKBACK, today
    ):
        if decrypt_data(row.currency_enc, master_key) != currency:
            continue
        net += _signed_amount(row, master_key)
    return net


def _pending_rows(
    session: Session, account: BankAccount, master_key: str, start: date, end: date
) -> list[tuple[BankTransaction, date]]:
    """This account's non-final rows in a date range, with the date they carry."""
    rows = _rows_in_range(session, account, master_key, start, end)
    pending = []
    for row in rows:
        if decrypt_data(row.status_enc, master_key) in FINAL_STATUSES:
            continue
        day = row_date(row, master_key)
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


def booked_movements(
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
        day = row_date(row, master_key)
        if day is None or not (start <= day <= end):
            continue
        net[day] += _signed_amount(row, master_key)
    return net


def _signed_amount(row: BankTransaction, master_key: str) -> Decimal:
    """A stored row's amount with its sign: the amount is unsigned, the
    direction indicator carries the sign."""
    amount = Decimal(decrypt_data(row.amount_enc, master_key))
    return amount if decrypt_data(row.credit_debit_enc, master_key) == CREDIT else -amount


class _Checkpoint(NamedTuple):
    """A balance reading and the day it opens: `day`'s own movements are not in
    `balance` yet, exactly like (anchor_date, anchor_balance)."""
    day: date
    balance: Decimal


def read_checkpoints(link: BankAccountLink, master_key: str) -> list[_Checkpoint]:
    """The readings previous syncs recorded, oldest first.

    A link that predates the column — or that has only ever synced once — falls
    back to its own anchor, which is the reading the check used to compare
    against on its own.
    """
    if not link.balance_checkpoints_enc:
        return [_Checkpoint(link.anchor_date, Decimal(decrypt_data(link.anchor_balance_enc, master_key)))]
    stored = json.loads(decrypt_data(link.balance_checkpoints_enc, master_key))
    points = [_Checkpoint(date.fromisoformat(item["d"]), Decimal(item["b"])) for item in stored]
    return sorted(points, key=lambda point: point.day)


def _record_checkpoint(
    link: BankAccountLink, point: _Checkpoint, master_key: str, today: date
) -> None:
    """Add today's reading, drop what is too old to serve, and keep one per day:
    a second sync on the same day would otherwise push the older readings out.
    """
    kept = {
        existing.day: existing
        for existing in read_checkpoints(link, master_key)
        if existing.day >= today - CHECKPOINT_RETENTION
    }
    kept[point.day] = point
    link.balance_checkpoints_enc = encrypt_data(
        json.dumps([
            {"d": item.day.isoformat(), "b": str(item.balance)}
            for item in sorted(kept.values(), key=lambda item: item.day)
        ]),
        master_key,
    )


def _check_window(
    points: list[_Checkpoint], today: date
) -> tuple[_Checkpoint, _Checkpoint] | None:
    """The two readings the check compares, or None while there are not two old
    enough — a freshly linked account, or one whose readings the migration has
    yet to fill. No verdict at all beats a verdict measured against a balance
    the bank had not finished publishing.
    """
    settled = [point for point in points if point.day <= today - SETTLE_LAG]
    if not settled:
        return None
    late = settled[-1]
    early = next(
        (point for point in reversed(settled[:-1]) if point.day <= late.day - CHECK_SPAN),
        None,
    )
    return None if early is None else (early, late)


def _reconciliation_gap(
    early: _Checkpoint,
    late: _Checkpoint,
    movements: dict[date, Decimal],
) -> Decimal | None:
    """`earlier reading + booked movements between = later reading`.

    Both readings come from the bank itself, days apart and old enough to have
    settled; what is on trial is the stored operations between them. When it
    holds, that stretch of the curve is exact. Otherwise the gap is returned, to
    be stored and dated by the sync that found it: a movement is missing or
    counted twice — the detector for the card / current-account double count,
    and for the deduplication fallback when a reference is absent.

    The period opens **on** the earlier reading's day and stops before the later
    one's: a reading is the closing balance of the day before it, so its own
    day's movements belong to the period that follows it.
    """
    period = sum(
        (value for day, value in movements.items() if early.day <= day < late.day),
        Decimal("0"),
    )
    gap = late.balance - (early.balance + period)
    return None if gap == 0 else gap


def curve_entries(
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


def _capped(account: BankAccount) -> BankAccountSyncResult:
    return BankAccountSyncResult(bank_account_uuid=account.uuid, status=SyncStatus.SKIPPED_DAILY_CAP)


def seed_after_linking(user_uuid: str, master_key: str, psu_context: dict[str, str] | None) -> None:
    """Fetch a freshly linked account's history right away, off the request.

    Some banks only serve the full history for a few minutes after the consent
    is authorised — Revolut restricts it to five, everything later being capped
    at ninety days. The link is created with `last_synced_at` set to yesterday
    precisely so a sync fires immediately, but that sync was the *front's* to
    make: an account picker left open too long, or a closed tab, and the window
    is gone for good, since only a new consent reopens it.

    Runs on its own session — the request's is closed by the time a background
    task runs — and never raises: this is a best-effort head start, and the
    front's own sync call remains the guarantee that a link gets synchronised.
    """
    try:
        with Session(get_engine()) as session:
            sync_user_accounts(session, user_uuid, master_key, psu_context=psu_context)
    except Exception:
        logger.exception("post-link seeding failed for user %s", user_uuid)
