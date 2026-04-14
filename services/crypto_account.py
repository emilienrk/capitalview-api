"""Crypto account services."""

import json
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Session, select

from models import CryptoAccount, CryptoTransaction
from models.account_history import AccountHistory
from dtos import (
    CryptoAccountCreate,
    CryptoAccountUpdate,
    CryptoAccountBasicResponse,
)
from dtos.transaction import (
    AccountHistoryPosition,
    AccountHistorySnapshotResponse,
)
from services.crypto_transaction import get_account_transactions, get_crypto_account_summary
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_account_to_response(account: CryptoAccount, master_key: str) -> CryptoAccountBasicResponse:
    """Decrypt and map CryptoAccount to basic response."""
    name = decrypt_data(account.name_enc, master_key)

    platform = None
    if account.platform_enc:
        platform = decrypt_data(account.platform_enc, master_key)

    address = None
    if account.public_address_enc:
        address = decrypt_data(account.public_address_enc, master_key)

    return CryptoAccountBasicResponse(
        id=account.uuid,
        name=name,
        platform=platform,
        public_address=address,
        opened_at=account.opened_at,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def create_crypto_account(
    session: Session,
    data: CryptoAccountCreate,
    user_uuid: str,
    master_key: str
) -> CryptoAccountBasicResponse:
    """Create a new encrypted crypto account."""
    user_bidx = hash_index(user_uuid, master_key)

    name_enc = encrypt_data(data.name, master_key)

    platform_enc = None
    if data.platform:
        platform_enc = encrypt_data(data.platform, master_key)

    address_enc = None
    if data.public_address:
        address_enc = encrypt_data(data.public_address, master_key)

    account = CryptoAccount(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        platform_enc=platform_enc,
        public_address_enc=address_enc,
        opened_at=data.opened_at,
    )
    
    session.add(account)
    session.commit()
    session.refresh(account)

    return _map_account_to_response(account, master_key)


def get_or_create_default_account(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> "CryptoAccount":
    """
    Get the existing single account for a SINGLE-mode user,
    or transparently create one if none exists yet.
    Returns the raw CryptoAccount model
    """
    user_bidx = hash_index(user_uuid, master_key)

    existing = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).first()

    if existing:
        return existing

    # Auto-create a transparent default account
    account = CryptoAccount(
        user_uuid_bidx=user_bidx,
        name_enc=encrypt_data("Mon Portefeuille", master_key),
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def get_user_crypto_accounts(
    session: Session,
    user_uuid: str,
    master_key: str
) -> list[CryptoAccountBasicResponse]:
    """List all crypto accounts for a user."""
    user_bidx = hash_index(user_uuid, master_key)

    accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    return [_map_account_to_response(acc, master_key) for acc in accounts]


def get_crypto_account(
    session: Session,
    account_uuid: str,
    user_uuid: str,
    master_key: str
) -> CryptoAccountBasicResponse | None:
    """Get a single crypto account if it belongs to the user."""
    account = session.get(CryptoAccount, account_uuid)
    if not account:
        return None

    user_bidx = hash_index(user_uuid, master_key)
    if account.user_uuid_bidx != user_bidx:
        return None

    return _map_account_to_response(account, master_key)


def update_crypto_account(
    session: Session,
    account: CryptoAccount,
    data: CryptoAccountUpdate,
    master_key: str
) -> CryptoAccountBasicResponse:
    """Update an existing crypto account."""
    if data.name is not None:
        account.name_enc = encrypt_data(data.name, master_key)

    if data.platform is not None:
        account.platform_enc = encrypt_data(data.platform, master_key)

    if data.public_address is not None:
        account.public_address_enc = encrypt_data(data.public_address, master_key)

    if data.opened_at is not None:
        account.opened_at = data.opened_at

    session.add(account)
    session.commit()
    session.refresh(account)

    return _map_account_to_response(account, master_key)


def delete_crypto_account(
    session: Session,
    account_uuid: str,
    master_key: str
) -> bool:
    """
    Delete a crypto account and all its transactions.
    """
    account = session.get(CryptoAccount, account_uuid)
    if not account:
        return False

    # Cascade delete for transactions
    account_bidx = hash_index(account_uuid, master_key)

    transactions = session.exec(
        select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == account_bidx)
    ).all()

    for tx in transactions:
        session.delete(tx)

    # Remove historical snapshots for this account as well.
    session.exec(
        sa.delete(AccountHistory).where(AccountHistory.account_id_bidx == account_bidx)
    )

    session.delete(account)
    session.commit()
    return True


def _build_current_account_snapshot(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> AccountHistorySnapshotResponse | None:
    """Build a fresh snapshot for today from the live account summary."""
    account = session.get(CryptoAccount, account_uuid)
    if not account:
        return None

    transactions = get_account_transactions(session, account_uuid, master_key)
    summary = get_crypto_account_summary(session, transactions, db_only=True)
    if summary.current_value is None:
        return None

    total_value = Decimal(summary.current_value)
    total_invested = Decimal(summary.total_invested)

    positions = []
    for position in summary.positions:
        position_value = (
            Decimal(position.current_value)
            if position.current_value is not None
            else (
                Decimal(position.current_price) * Decimal(position.total_amount)
                if position.current_price is not None
                else Decimal("0")
            )
        )
        positions.append(
            AccountHistoryPosition(
                asset_key=position.asset_key,
                quantity=Decimal(position.total_amount),
                value=position_value,
                price=Decimal(position.current_price) if position.current_price is not None else None,
                invested=Decimal(position.total_invested),
                percentage=(
                    (position_value / total_value * Decimal("100"))
                    if total_value > Decimal("0")
                    else Decimal("0")
                ),
            )
        )

    return AccountHistorySnapshotResponse(
        snapshot_date=date.today(),
        total_value=total_value,
        total_invested=total_invested,
        daily_pnl=None,
        all_time_pnl=round(total_value - total_invested, 2),
        positions=positions or None,
    )


def get_crypto_account_history(
    session: Session,
    account_uuid: str,
    master_key: str,
    include_current: bool = True,
) -> list[AccountHistorySnapshotResponse]:
    """Return decrypted daily snapshots for a crypto account, ordered by date."""
    account_id_bidx = hash_index(account_uuid, master_key)
    today = date.today()

    rows = session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .order_by(AccountHistory.snapshot_date)
    ).all()

    result: list[AccountHistorySnapshotResponse] = []
    for row in rows:
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

        result.append(
            AccountHistorySnapshotResponse(
                snapshot_date=row.snapshot_date,
                total_value=total_value,
                total_invested=total_invested,
                daily_pnl=daily_pnl,
                all_time_pnl=round(total_value - total_invested, 2),
                positions=positions,
            )
        )

    if include_current:
        current_snapshot = _build_current_account_snapshot(session, account_uuid, master_key)
        if current_snapshot is not None:
            result = [snap for snap in result if snap.snapshot_date != today]
            result.append(current_snapshot)

    result.sort(key=lambda snap: snap.snapshot_date)

    if include_current:
        previous_snapshot: AccountHistorySnapshotResponse | None = None
        for snapshot in result:
            if snapshot.snapshot_date == today and snapshot.daily_pnl is None and previous_snapshot is not None:
                prev_all_time_pnl = previous_snapshot.total_value - previous_snapshot.total_invested
                curr_all_time_pnl = snapshot.total_value - snapshot.total_invested
                snapshot.daily_pnl = round(curr_all_time_pnl - prev_all_time_pnl, 2)
            previous_snapshot = snapshot

    return result


def get_all_crypto_accounts_history(
    session: Session,
    user_uuid: str,
    master_key: str,
    include_current: bool = True,
) -> list[AccountHistorySnapshotResponse]:
    """
    Aggregate daily snapshots across all crypto accounts for a user.
    For each date, total_value and total_invested are summed; positions
    are merged by asset_key (quantities and values summed, price kept from
    the last account that had a price for that asset_key).
    """
    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    # date -> {"total_value": Decimal, 
    #          "total_invested": Decimal,
    #          "daily_pnl": Decimal,
    #          "positions": {asset_key: {"quantity", "value", "price", "invested"}}}
    aggregated: dict = {}

    for acc in accounts:
        for snap in get_crypto_account_history(session, acc.uuid, master_key, include_current):
            d = snap.snapshot_date
            if d not in aggregated:
                aggregated[d] = {
                    "total_value": Decimal("0"),
                    "total_invested": Decimal("0"),
                    "daily_pnl": Decimal("0"),
                    "positions": {},
                }
            aggregated[d]["total_value"] += snap.total_value
            aggregated[d]["total_invested"] += snap.total_invested
            if snap.daily_pnl is not None:
                aggregated[d]["daily_pnl"] += snap.daily_pnl

            for pos in (snap.positions or []):
                asset_key = pos.asset_key
                if asset_key not in aggregated[d]["positions"]:
                    aggregated[d]["positions"][asset_key] = {
                        "quantity": Decimal("0"),
                        "value": Decimal("0"),
                        "price": None,
                        "invested": Decimal("0"),
                    }
                aggregated[d]["positions"][asset_key]["quantity"] += pos.quantity
                aggregated[d]["positions"][asset_key]["value"] += pos.value
                aggregated[d]["positions"][asset_key]["invested"] += pos.invested
                if pos.price is not None:
                    aggregated[d]["positions"][asset_key]["price"] = pos.price

    result = []
    for index, d in enumerate(sorted(aggregated)):
        day = aggregated[d]
        total_value = day["total_value"]
        daily_pnl = None if index == 0 else day["daily_pnl"]
        positions = [
            AccountHistoryPosition(
                asset_key=asset_key,
                quantity=data["quantity"],
                value=data["value"],
                price=data["price"],
                invested=data["invested"],
                percentage=(
                    (data["value"] / total_value * Decimal("100"))
                    if total_value > Decimal("0")
                    else Decimal("0")
                ),
            )
            for asset_key, data in day["positions"].items()
        ] or None
        result.append(
            AccountHistorySnapshotResponse(
                snapshot_date=d,
                total_value=total_value,
                total_invested=day["total_invested"],
                daily_pnl=round(daily_pnl, 2) if daily_pnl is not None else None,
                all_time_pnl=round(total_value - day["total_invested"], 2),
                positions=positions,
            )
        )

    return result