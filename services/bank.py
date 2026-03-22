"""Bank account service."""

import json
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from models import BankAccount, BankAccountType
from models.account_history import AccountHistory
from models.enums import AccountCategory, FlowType
from dtos import BankAccountCreate, BankAccountUpdate, BankAccountResponse, BankSummaryResponse
from dtos.bank import BankHistoryEntry
from dtos.transaction import AccountHistoryPosition, AccountHistorySnapshotResponse
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_to_response(account: BankAccount, master_key: str) -> BankAccountResponse:
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
        institution_name=inst_name,
        identifier=identifier,
        opened_at=account.opened_at,
        balance_updated_at=account.balance_updated_at,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def create_bank_account(
    session: Session, 
    data: BankAccountCreate, 
    user_uuid: str, 
    master_key: str
) -> BankAccountResponse:
    """Create a new encrypted bank account."""
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

    if data.opened_at is not None:
        account.opened_at = data.opened_at
        
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_to_response(account, master_key)


def delete_bank_account(
    session: Session,
    account_uuid: str
) -> bool:
    """Delete a bank account."""
    account = session.get(BankAccount, account_uuid)
    if not account:
        return False
        
    session.delete(account)
    session.commit()
    return True


def _apply_pending_cashflows(
    session: Session,
    account: BankAccount,
    cashflows: list,
    master_key: str,
    get_cashflow_occurrences_fn,
) -> None:
    """Apply cashflow occurrences that have fired since balance_updated_at.

    On the first call (balance_updated_at is None), we just stamp today without
    applying anything — this prevents retroactively adjusting a manually-entered balance.
    Subsequent calls apply all occurrences in (balance_updated_at, today].
    """
    today = date.today()

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
    linked = [cf for cf in cashflows if cf.bank_account_id == account.uuid]
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

    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()

    # Fetch cashflows once and apply pending ones to each linked account
    cashflows = get_all_user_cashflows(session, user_uuid, master_key)
    for account in accounts:
        _apply_pending_cashflows(session, account, cashflows, master_key, get_cashflow_occurrences)

    responses = [_map_to_response(acc, master_key) for acc in accounts]
    total_balance = sum(acc.balance for acc in responses)

    return BankSummaryResponse(
        total_balance=total_balance,
        accounts=responses
    )


def get_bank_account(
    session: Session,
    account_uuid: str,
    user_uuid: str,
    master_key: str
) -> Optional[BankAccountResponse]:
    """Get a single bank account if it belongs to the user."""
    account = session.get(BankAccount, account_uuid)
    if not account:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if account.user_uuid_bidx != user_bidx:
        return None
        
    return _map_to_response(account, master_key)


def _decode_history_row(row: AccountHistory, master_key: str) -> AccountHistorySnapshotResponse:
    """Decrypt a single AccountHistory row into a response DTO."""
    total_value = Decimal(decrypt_data(row.total_value_enc, master_key))
    total_invested = Decimal(decrypt_data(row.total_invested_enc, master_key))
    daily_pnl = (
        Decimal(decrypt_data(row.daily_pnl_enc, master_key))
        if row.daily_pnl_enc
        else None
    )

    positions = None
    if row.positions_enc:
        raw_json = decrypt_data(row.positions_enc, master_key)
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                positions = [
                    AccountHistoryPosition(
                        symbol=p["symbol"],
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
        daily_pnl=daily_pnl,
        positions=positions,
    )


def get_bank_account_history(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> list[AccountHistorySnapshotResponse]:
    """Return decrypted daily snapshots for a bank account, ordered by date."""
    account_id_bidx = hash_index(account_uuid, master_key)

    rows = session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .order_by(AccountHistory.snapshot_date)
    ).all()

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

        positions_json: Optional[str] = None
        if total_value > Decimal("0"):
            positions_json = json.dumps([{
                "symbol": "EUR",
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


def get_all_bank_accounts_history(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> list[AccountHistorySnapshotResponse]:
    """
    Aggregate daily snapshots across all bank accounts for a user.
    The bank position is always EUR so values are simply summed by date.
    """
    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()

    # date -> {total_value, total_invested, total_qty}
    aggregated: dict = {}

    for acc in accounts:
        for snap in get_bank_account_history(session, acc.uuid, master_key):
            d = snap.snapshot_date
            if d not in aggregated:
                aggregated[d] = {"total_value": Decimal("0"), "total_invested": Decimal("0")}
            aggregated[d]["total_value"] += snap.total_value
            aggregated[d]["total_invested"] += snap.total_invested

    result = []
    for d in sorted(aggregated):
        day = aggregated[d]
        total_value = day["total_value"]
        positions = [
            AccountHistoryPosition(
                symbol="EUR",
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
                daily_pnl=None,
                positions=positions,
            )
        )

    return result
