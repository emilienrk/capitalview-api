"""Bank account service."""

import json
import uuid
from typing import Any
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from models import BankAccount, BankAccountType
from models.account_history import AccountHistory
from models.banking import BankAccountLink, BankSession
from models.enums import AccountCategory, FlowType
from dtos import BankAccountCreate, BankAccountUpdate, BankAccountResponse, BankSummaryResponse
from dtos.bank import BankHistoryEntry
from dtos.transaction import AccountHistoryPosition, AccountHistorySnapshotResponse
from services.banking.health import is_session_active
from services.banking.linking import is_card_account
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_exchange_rate, has_exchange_rate

# Consent wording the front reads back (ruling R16): anything but an authorized
# session is presented as needing a fresh connection.
LINK_STATUS_CONNECTED = "connecté"
LINK_STATUS_RECONNECT = "à reconnecter"

# The three outcomes of the reconciliation check (ruling R18), kept here rather
# than in the sync because that module already depends on this one. Distinct
# from LINK_STATUS_*, which describes the consent, not the curve.
RECONCILIATION_OK = "reconciled"
RECONCILIATION_GAP = "gap"
RECONCILIATION_NOT_POSSIBLE = "not_reconcilable"


class LinkMetadata:
    """The link-side fields of BankAccountResponse (ruling R6), read once per request."""

    def __init__(
        self,
        link: BankAccountLink,
        session_status: str | None,
        master_key: str,
        not_reconcilable: bool = False,
    ):
        # A link is created with last_synced_at one day before its bootstrap
        # anchor, so the column is never NULL. The contract's "null = never
        # synced" is that marker, mapped: reporting yesterday for an account the
        # bank has never been called for would read as a successful sync.
        never_synced = link.last_synced_at < link.anchor_date
        self.last_synced_at = None if never_synced else link.last_synced_at
        self.reconciliation_gap = (
            Decimal(decrypt_data(link.last_reconciliation_gap_enc, master_key))
            if link.last_reconciliation_gap_enc
            else None
        )
        # Matched by SessionStatus member name, never by the raw literal: the
        # OpenAPI enum descriptions are misaligned with their values (trap 4).
        self.link_status = (
            LINK_STATUS_CONNECTED if is_session_active(session_status) else LINK_STATUS_RECONNECT
        )
        # Derived, never stored (R7's precedent): three outcomes, and none yet
        # while no check has been able to run.
        if not_reconcilable:
            self.reconciliation_status = RECONCILIATION_NOT_POSSIBLE
        elif never_synced:
            self.reconciliation_status = None
        else:
            self.reconciliation_status = (
                RECONCILIATION_GAP if self.reconciliation_gap is not None else RECONCILIATION_OK
            )


def _link_metadata(session: Session, user_bidx: str, master_key: str) -> dict[str, LinkMetadata]:
    """bank_account_uuid_bidx → link metadata, for every link this user holds."""
    rows = session.exec(
        select(BankAccountLink, BankSession.status)
        .join(BankSession, BankSession.uuid == BankAccountLink.session_uuid, isouter=True)
        .where(BankAccountLink.user_uuid_bidx == user_bidx)
    ).all()
    return {
        link.bank_account_uuid_bidx: LinkMetadata(
            link, status, master_key, is_card_account(session, link, master_key)
        )
        for link, status in rows
    }


# Every account created before currency_enc existed is in euros, and so is every
# account whose owner never chose otherwise.
DEFAULT_CURRENCY = "EUR"


class UnconvertibleCurrencyError(ValueError):
    """No exchange rate is available for this currency.

    Refused at the door rather than absorbed: a currency that cannot be
    converted would be added to the euro total one-for-one, silently, and a
    wrong total presented as a right one is worse than a refusal.
    """


def require_convertible(session: Session, currency: str | None) -> None:
    if currency is not None and not has_exchange_rate(session, currency):
        raise UnconvertibleCurrencyError(
            f"Aucun taux de change n'est disponible pour {currency}. "
            "Choisissez une devise dont le cours est publié."
        )


def account_currency(account: BankAccount, master_key: str) -> str:
    """The currency an account's balance and movements are denominated in.

    The single reader of `currency_enc`. NULL means EUR — the column is nullable
    because a migration cannot encrypt a back-fill it holds no Master Key for.
    """
    if account.currency_enc is None:
        return DEFAULT_CURRENCY
    return decrypt_data(account.currency_enc, master_key)


def _map_to_response(
    account: BankAccount, master_key: str, link: LinkMetadata | None = None
) -> BankAccountResponse:
    """Decrypt and map a BankAccount to a response DTO."""
    name = decrypt_data(account.name_enc, master_key)
    balance_str = decrypt_data(account.balance_enc, master_key)
    type_str = decrypt_data(account.account_type_enc, master_key)
    
    inst_name = None
    if account.institution_name_enc:
        inst_name = decrypt_data(account.institution_name_enc, master_key)
        
    identifier = None
    if account.identifier_enc:
        identifier = decrypt_data(account.identifier_enc, master_key)

    return BankAccountResponse(
        id=account.uuid,
        name=name,
        balance=Decimal(balance_str),
        account_type=BankAccountType(type_str),
        currency=account_currency(account, master_key),
        institution_name=inst_name,
        identifier=identifier,
        opened_at=account.opened_at,
        balance_updated_at=account.balance_updated_at,
        created_at=account.created_at,
        updated_at=account.updated_at,
        is_linked=link is not None,
        last_synced_at=link.last_synced_at if link else None,
        reconciliation_gap=link.reconciliation_gap if link else None,
        link_status=link.link_status if link else None,
        reconciliation_status=link.reconciliation_status if link else None,
    )


def create_bank_account(
    session: Session, 
    data: BankAccountCreate, 
    user_uuid: str, 
    master_key: str
) -> BankAccountResponse:
    """Create a new encrypted bank account."""
    require_convertible(session, data.currency)
    user_bidx = hash_index(user_uuid, master_key)
    
    name_enc = encrypt_data(data.name, master_key)
    balance_enc = encrypt_data(str(data.balance), master_key)
    type_enc = encrypt_data(data.account_type.value, master_key)
    
    inst_enc = None
    if data.institution_name:
        inst_enc = encrypt_data(data.institution_name, master_key)
        
    ident_enc = None
    if data.identifier:
        ident_enc = encrypt_data(data.identifier, master_key)
        
    account = BankAccount(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        balance_enc=balance_enc,
        account_type_enc=type_enc,
        institution_name_enc=inst_enc,
        identifier_enc=ident_enc,
        currency_enc=encrypt_data(data.currency, master_key),
        opened_at=data.opened_at,
    )
    
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_to_response(account, master_key)


def update_bank_account(
    session: Session,
    account: BankAccount,
    data: BankAccountUpdate,
    master_key: str
) -> BankAccountResponse:
    """Update an existing bank account."""
    require_convertible(session, data.currency)

    if data.name is not None:
        account.name_enc = encrypt_data(data.name, master_key)
        
    if data.balance is not None:
        account.balance_enc = encrypt_data(str(data.balance), master_key)
        # Reset the sync date: the balance is now manually set to today's real value,
        # so the next auto-sync must start from today to avoid double-applying cashflows.
        account.balance_updated_at = date.today()

    if data.institution_name is not None:
        account.institution_name_enc = encrypt_data(data.institution_name, master_key)
        
    if data.identifier is not None:
        account.identifier_enc = encrypt_data(data.identifier, master_key)

    if data.currency is not None:
        account.currency_enc = encrypt_data(data.currency, master_key)

    if data.opened_at is not None:
        account.opened_at = data.opened_at
        
    session.add(account)
    session.commit()
    session.refresh(account)

    return _map_to_response(account, master_key, _account_link(session, account, master_key))


def _account_link(
    session: Session, account: BankAccount, master_key: str
) -> LinkMetadata | None:
    """Link metadata for a single account, when it is attached to a bank."""
    return _link_metadata(session, account.user_uuid_bidx, master_key).get(
        hash_index(account.uuid, master_key)
    )


def delete_bank_account(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> bool:
    """Delete a bank account and its account history snapshots."""
    account = session.get(BankAccount, account_uuid)
    if not account:
        return False

    account_id_bidx = hash_index(account_uuid, master_key)
    session.exec(
        sa.delete(AccountHistory).where(AccountHistory.account_id_bidx == account_id_bidx)
    )
        
    session.delete(account)
    session.commit()
    return True


def _apply_pending_cashflows(
    session: Session,
    account: BankAccount,
    cashflows: list,
    master_key: str,
    get_cashflow_occurrences_fn,
    auto_sync_enabled: bool,
    is_linked: bool = False,
) -> None:
    """Apply cashflow occurrences that have fired since balance_updated_at.

    On the first call (balance_updated_at is None), we just stamp today without
    applying anything — this prevents retroactively adjusting a manually-entered balance.
    Subsequent calls apply all occurrences in (balance_updated_at, today].

    Inactive cashflows, a disabled global switch and a bank-linked account skip
    the balance update but still advance the stamp: a paused period must never
    be caught up later.
    """
    today = date.today()

    if is_linked:
        # Spec §D5: a linked account carries a real balance read from the bank,
        # so projecting a salary already inside it would double-count.
        if account.balance_updated_at != today:
            account.balance_updated_at = today
            session.add(account)
            session.commit()
        return

    if account.balance_updated_at is None:
        # First run: stamp today, do not touch the balance
        account.balance_updated_at = today
        session.add(account)
        session.commit()
        return

    from_date = account.balance_updated_at
    if from_date >= today:
        return  # Already up to date

    # Filter cashflows linked to this account
    linked = [
        cf for cf in cashflows
        if cf.bank_account_id == account.uuid and cf.is_active
    ] if auto_sync_enabled else []

    if not linked:
        account.balance_updated_at = today
        session.add(account)
        session.commit()
        return

    # Compute net delta from all occurrences in (from_date, today]
    current_balance = Decimal(decrypt_data(account.balance_enc, master_key))
    delta = Decimal("0")

    for cf in linked:
        occurrences = get_cashflow_occurrences_fn(cf, from_date, today)
        if not occurrences:
            continue
        amount_per_occurrence = cf.amount
        count = Decimal(str(len(occurrences)))
        if cf.flow_type == FlowType.INFLOW:
            delta += amount_per_occurrence * count
        else:
            delta -= amount_per_occurrence * count

    new_balance = current_balance + delta
    account.balance_enc = encrypt_data(str(new_balance), master_key)
    account.balance_updated_at = today
    session.add(account)
    session.commit()


def get_user_bank_accounts(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> BankSummaryResponse:
    """Get all bank accounts for a user, applying pending cashflows first."""
    # Lazy import to avoid circular dependency
    from services.cashflow import get_all_user_cashflows, get_cashflow_occurrences
    from services.settings import get_or_create_settings

    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()

    auto_sync_enabled = get_or_create_settings(session, user_uuid, master_key).bank_auto_sync_enabled
    links = _link_metadata(session, user_bidx, master_key)

    # Fetch cashflows once and apply pending ones to each linked account
    cashflows = get_all_user_cashflows(session, user_uuid, master_key)
    for account in accounts:
        _apply_pending_cashflows(
            session,
            account,
            cashflows,
            master_key,
            get_cashflow_occurrences,
            auto_sync_enabled,
            is_linked=hash_index(account.uuid, master_key) in links,
        )

    responses = [
        _map_to_response(acc, master_key, links.get(hash_index(acc.uuid, master_key)))
        for acc in accounts
    ]
    return BankSummaryResponse(
        total_balance=_total_in_base_currency(session, responses),
        accounts=responses,
    )


def _total_in_base_currency(
    session: Session, responses: list[BankAccountResponse]
) -> Decimal | None:
    """The accounts' balances added up in euros, or None if one cannot be.

    Adding the raw figures would total francs with euros. Converted at today's
    rate, not at a historical one: this is an instantaneous total, and the only
    honest rate for "what is this worth now" is the current one.

    `None` rather than a figure when any currency has no published rate.
    `get_exchange_rate` answers 1 in that case, indistinguishably from a rate
    that genuinely is 1, so adding it would put a wrong total on screen with
    nothing marking it as wrong. A currency is checked at account creation, but
    a rate can stop being published afterwards — the check has to happen here
    too. Showing nothing is recoverable; showing a wrong total is not.
    """
    rates: dict[str, Decimal] = {}
    total = Decimal("0")
    for account in responses:
        if account.currency not in rates:
            if not has_exchange_rate(session, account.currency):
                return None
            rates[account.currency] = get_exchange_rate(session, account.currency, "EUR")
        total += account.balance * rates[account.currency]
    return total


def get_bank_account(
    session: Session,
    account_uuid: str,
    user_uuid: str,
    master_key: str
) -> BankAccountResponse | None:
    """Get a single bank account if it belongs to the user."""
    account = session.get(BankAccount, account_uuid)
    if not account:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if account.user_uuid_bidx != user_bidx:
        return None

    return _map_to_response(account, master_key, _account_link(session, account, master_key))


def _decode_history_row(row: AccountHistory, master_key: str) -> AccountHistorySnapshotResponse:
    """Decrypt a single AccountHistory row into a response DTO."""
    total_value = Decimal(decrypt_data(row.total_value_enc, master_key))
    total_invested = Decimal(decrypt_data(row.total_invested_enc, master_key))
    total_deposits = (
        Decimal(decrypt_data(row.total_deposits_enc, master_key))
        if row.total_deposits_enc
        else total_invested
    )
    total_withdrawals = (
        Decimal(decrypt_data(row.total_withdrawals_enc, master_key))
        if row.total_withdrawals_enc
        else Decimal("0")
    )
    # Bank accounts do not expose a performance PnL series.
    daily_pnl = None

    positions = None
    if row.positions_enc:
        raw_json = decrypt_data(row.positions_enc, master_key)
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                positions = [
                    AccountHistoryPosition(
                        asset_key=p["asset_key"],
                        quantity=Decimal(p["quantity"]),
                        value=Decimal(p["value"]),
                        price=Decimal(p["price"]) if p.get("price") is not None else None,
                        invested=Decimal(p["invested"]),
                        percentage=Decimal(p["percentage"]),
                    )
                    for p in parsed
                ]
            except Exception:
                positions = None

    return AccountHistorySnapshotResponse(
        snapshot_date=row.snapshot_date,
        total_value=total_value,
        total_invested=total_invested,
        total_deposits=total_deposits,
        total_withdrawals=total_withdrawals,
        daily_pnl=daily_pnl,
        positions=positions,
    )


def get_bank_account_history(
    session: Session,
    account_uuid: str,
    master_key: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[AccountHistorySnapshotResponse]:
    """Return decrypted daily snapshots for a bank account, ordered by date."""
    account_id_bidx = hash_index(account_uuid, master_key)

    query = select(AccountHistory).where(AccountHistory.account_id_bidx == account_id_bidx)
    if start_date:
        query = query.where(AccountHistory.snapshot_date >= start_date)
    if end_date:
        query = query.where(AccountHistory.snapshot_date <= end_date)

    rows = session.exec(query.order_by(AccountHistory.snapshot_date)).all()

    return [_decode_history_row(row, master_key) for row in rows]


def delete_bank_account_history(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> int:
    """Delete all history snapshots for a bank account. Returns the number of deleted rows."""
    account_id_bidx = hash_index(account_uuid, master_key)
    result = session.exec(
        sa.delete(AccountHistory).where(AccountHistory.account_id_bidx == account_id_bidx)
    )
    session.commit()
    return result.rowcount


def import_bank_account_history(
    session: Session,
    account: BankAccount,
    entries: list[BankHistoryEntry],
    master_key: str,
    overwrite: bool = False,
) -> int:
    """
    Import a list of (date, value) snapshots for a bank account.

    Fills the full range from account creation to yesterday:
    - Dates before the first known entry are set to 0.
    - Gaps between known entries are forward-filled with the last known value.
    - If overwrite=True, existing history is deleted first; otherwise existing
      rows are preserved (on_conflict_do_nothing).

    Returns the number of rows written.
    """
    if not entries:
        return 0

    if overwrite:
        delete_bank_account_history(session, account.uuid, master_key)

    sorted_entries = sorted(entries, key=lambda e: e.snapshot_date)
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    account_start = account.created_at.date()
    first_entry_date = sorted_entries[0].snapshot_date

    # Start from the earliest of account creation and first imported entry,
    # so historical data predating the app account creation is not silently dropped.
    fill_start = min(account_start, first_entry_date)
    if fill_start > yesterday:
        return 0

    # Build a date → value lookup
    value_by_date: dict[date, Decimal] = {e.snapshot_date: e.value for e in sorted_entries}

    now = datetime.now(timezone.utc)
    account_id_bidx = hash_index(account.uuid, master_key)

    rows: list[dict] = []
    last_value = Decimal("0")
    prev_value = Decimal("0")

    d = fill_start
    while d <= yesterday:
        if d < first_entry_date:
            last_value = Decimal("0")
        elif d in value_by_date:
            last_value = value_by_date[d]
        # else: carry forward last_value

        total_value = last_value
        daily_pnl = total_value - prev_value

        positions_json: str | None = None
        if total_value > Decimal("0"):
            positions_json = json.dumps([{
                "asset_key": "EUR",
                "quantity": str(total_value),
                "value": str(total_value),
                "price": "1",
                "invested": str(total_value),
                "percentage": "100",
            }])

        rows.append({
            "uuid": str(uuid.uuid4()),
            "user_uuid_bidx": account.user_uuid_bidx,
            "account_id_bidx": account_id_bidx,
            "account_type": AccountCategory.BANK.value,
            "snapshot_date": d,
            "total_value_enc": encrypt_data(str(round(total_value, 2)), master_key),
            "total_invested_enc": encrypt_data(str(round(total_value, 2)), master_key),
            "total_deposits_enc": encrypt_data(str(round(total_value, 2)), master_key),
            "total_withdrawals_enc": encrypt_data("0.00", master_key),
            "daily_pnl_enc": encrypt_data(str(round(daily_pnl, 2)), master_key),
            "positions_enc": encrypt_data(positions_json, master_key) if positions_json else None,
            "created_at": now,
            "updated_at": now,
        })

        prev_value = total_value
        d += timedelta(days=1)

    if not rows:
        return 0

    stmt = pg_insert(AccountHistory).values(rows).on_conflict_do_nothing(
        constraint="uq_account_history_account_date"
    )
    session.exec(stmt)
    session.commit()
    return len(rows)


def replace_history_window(
    session: Session,
    account: BankAccount,
    entries: list[BankHistoryEntry],
    master_key: str,
    start_date: date,
    end_date: date,
) -> int:
    """
    Replace the snapshots of one bank account **inside [start_date, end_date]**,
    and touch nothing outside it. Returns the number of rows written.

    This exists because neither mode of `import_bank_account_history` fits the
    bank sync (spec §D4): `overwrite=True` deletes the account's *entire*
    history — years of manual entry with it — and the default mode
    (`on_conflict_do_nothing`) overwrites nothing at all, so bank data could
    never take precedence over a manual snapshot on the seeding window
    (decision 8).

    The window is emptied first, so a day the bank no longer accounts for
    disappears instead of lingering; only the supplied entries are written back.
    `end_date` is clamped to yesterday, following the same convention as
    `import_bank_account_history`: today is left out so pending operations have
    time to settle. "Today" is the server's civil day, as everywhere else in
    this module, so the clamp can never disagree with the caller's own notion
    of yesterday.
    """
    yesterday = date.today() - timedelta(days=1)
    end_date = min(end_date, yesterday)
    if start_date > end_date:
        return 0

    account_id_bidx = hash_index(account.uuid, master_key)

    # Read the value carried into the window before emptying it, so the first
    # day's daily_pnl is a real delta rather than a jump from zero.
    previous_row = session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .where(AccountHistory.snapshot_date < start_date)
        .order_by(AccountHistory.snapshot_date.desc())
    ).first()
    prev_value = (
        Decimal(decrypt_data(previous_row.total_value_enc, master_key))
        if previous_row
        else Decimal("0")
    )

    session.exec(
        sa.delete(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .where(AccountHistory.snapshot_date >= start_date)
        .where(AccountHistory.snapshot_date <= end_date)
    )

    kept = sorted(
        (e for e in entries if start_date <= e.snapshot_date <= end_date),
        key=lambda e: e.snapshot_date,
    )
    if not kept:
        session.commit()
        return 0

    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for entry in kept:
        total_value = round(entry.value, 2)
        daily_pnl = total_value - prev_value

        positions_json: str | None = None
        if total_value > Decimal("0"):
            positions_json = json.dumps([{
                "asset_key": "EUR",
                "quantity": str(total_value),
                "value": str(total_value),
                "price": "1",
                "invested": str(total_value),
                "percentage": "100",
            }])

        rows.append({
            "uuid": str(uuid.uuid4()),
            "user_uuid_bidx": account.user_uuid_bidx,
            "account_id_bidx": account_id_bidx,
            "account_type": AccountCategory.BANK.value,
            "snapshot_date": entry.snapshot_date,
            "total_value_enc": encrypt_data(str(total_value), master_key),
            "total_invested_enc": encrypt_data(str(total_value), master_key),
            "total_deposits_enc": encrypt_data(str(total_value), master_key),
            "total_withdrawals_enc": encrypt_data("0.00", master_key),
            "daily_pnl_enc": encrypt_data(str(round(daily_pnl, 2)), master_key),
            "positions_enc": encrypt_data(positions_json, master_key) if positions_json else None,
            "created_at": now,
            "updated_at": now,
        })
        prev_value = total_value

    stmt = pg_insert(AccountHistory).values(rows).on_conflict_do_nothing(
        constraint="uq_account_history_account_date"
    )
    session.exec(stmt)
    session.commit()
    return len(rows)


def get_all_bank_accounts_history(
    session: Session,
    user_uuid: str,
    master_key: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[AccountHistorySnapshotResponse]:
    """
    Aggregate daily snapshots across all bank accounts for a user.
    The bank position is always EUR so values are simply summed by date.
    """
    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()

    # date -> {total_value, total_invested}
    aggregated: dict = {}

    for acc in accounts:
        for snap in get_bank_account_history(session, acc.uuid, master_key, start_date, end_date):
            d = snap.snapshot_date
            if d not in aggregated:
                aggregated[d] = {
                    "total_value": Decimal("0"),
                    "total_invested": Decimal("0"),
                    "total_deposits": Decimal("0"),
                    "total_withdrawals": Decimal("0"),
                }
            aggregated[d]["total_value"] += snap.total_value
            aggregated[d]["total_invested"] += snap.total_invested
            aggregated[d]["total_deposits"] += snap.total_deposits
            aggregated[d]["total_withdrawals"] += snap.total_withdrawals

    result = []
    for d in sorted(aggregated):
        day = aggregated[d]
        total_value = day["total_value"]
        positions = [
            AccountHistoryPosition(
                asset_key="EUR",
                quantity=total_value,
                value=total_value,
                price=Decimal("1"),
                invested=day["total_invested"],
                percentage=Decimal("100"),
            )
        ] if total_value > Decimal("0") else None
        result.append(
            AccountHistorySnapshotResponse(
                snapshot_date=d,
                total_value=total_value,
                total_invested=day["total_invested"],
                total_deposits=day["total_deposits"],
                total_withdrawals=day["total_withdrawals"],
                daily_pnl=None,
                positions=positions,
            )
        )

    return result


def get_all_bank_accounts_snapshot_for_date(
    session: Session,
    user_uuid: str,
    target_date: date,
    master_key: str,
) -> dict[str, Any]:
    """
    Returns a list of bank account containing account name, institution, and balance.
    """
    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()

    if not accounts:
        return []

    result = []
    total_value = Decimal("0")
    for account in accounts:
        account_id_bidx = hash_index(account.uuid, master_key)
        
        # Récupère l'historique de CE compte pour cette date
        row = session.exec(
            select(AccountHistory)
            .where(AccountHistory.account_id_bidx == account_id_bidx)
            .where(AccountHistory.snapshot_date == target_date)
        ).first()

        name = decrypt_data(account.name_enc, master_key)
        inst_name = None
        if account.institution_name_enc:
            inst_name = decrypt_data(account.institution_name_enc, master_key)

        if row:
            snap = _decode_history_row(row, master_key)
            balance = snap.total_value
        else:
            balance = Decimal("0")
        total_value += balance
        result.append({
            "name": name,
            "institution": inst_name,
            "balance": balance,
        })

    return {
        "total_value": total_value,
        "accounts": result,
    }
