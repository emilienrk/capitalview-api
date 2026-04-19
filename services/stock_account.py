"""Stock account services."""

import json
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Session, select

from models import StockAccount, StockAccountType, StockTransaction
from models.account_history import AccountHistory
from dtos import (
    StockAccountCreate,
    StockAccountUpdate,
    StockAccountBasicResponse,
)
from dtos.transaction import (
    AccountHistoryPosition,
    AccountHistorySnapshotResponse,
)
from services.stock_transaction import get_account_transactions, get_stock_account_summary
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_account_to_response(account: StockAccount, master_key: str) -> StockAccountBasicResponse:
    """Decrypt and map StockAccount to basic response."""
    name = decrypt_data(account.name_enc, master_key)
    type_str = decrypt_data(account.account_type_enc, master_key)
    
    inst_name = None
    if account.institution_name_enc:
        inst_name = decrypt_data(account.institution_name_enc, master_key)
        
    ident = None
    if account.identifier_enc:
        ident = decrypt_data(account.identifier_enc, master_key)

    return StockAccountBasicResponse(
        id=account.uuid,
        name=name,
        account_type=StockAccountType(type_str),
        institution_name=inst_name,
        identifier=ident,
        opened_at=account.opened_at,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def create_stock_account(
    session: Session, 
    data: StockAccountCreate, 
    user_uuid: str, 
    master_key: str
) -> StockAccountBasicResponse:
    """Create a new encrypted stock account."""
    user_bidx = hash_index(user_uuid, master_key)
    
    name_enc = encrypt_data(data.name, master_key)
    type_enc = encrypt_data(data.account_type.value, master_key)
    
    inst_enc = None
    if data.institution_name:
        inst_enc = encrypt_data(data.institution_name, master_key)
        
    ident_enc = None
    if data.identifier:
        ident_enc = encrypt_data(data.identifier, master_key)
        
    account = StockAccount(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        account_type_enc=type_enc,
        institution_name_enc=inst_enc,
        identifier_enc=ident_enc,
        opened_at=data.opened_at,
    )
    
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_account_to_response(account, master_key)


def get_user_stock_accounts(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> list[StockAccountBasicResponse]:
    """List all stock accounts for a user."""
    user_bidx = hash_index(user_uuid, master_key)
    
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    return [_map_account_to_response(acc, master_key) for acc in accounts]


def get_stock_account(
    session: Session,
    account_uuid: str,
    user_uuid: str,
    master_key: str
) -> StockAccountBasicResponse | None:
    """Get a single stock account if it belongs to the user."""
    account = session.get(StockAccount, account_uuid)
    if not account:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if account.user_uuid_bidx != user_bidx:
        return None
        
    return _map_account_to_response(account, master_key)


def update_stock_account(
    session: Session,
    account: StockAccount,
    data: StockAccountUpdate,
    master_key: str
) -> StockAccountBasicResponse:
    """Update an existing stock account."""
    if data.name is not None:
        account.name_enc = encrypt_data(data.name, master_key)
        
    if data.institution_name is not None:
        account.institution_name_enc = encrypt_data(data.institution_name, master_key)
        
    if data.identifier is not None:
        account.identifier_enc = encrypt_data(data.identifier, master_key)

    if data.opened_at is not None:
        account.opened_at = data.opened_at
        
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_account_to_response(account, master_key)


def delete_stock_account(
    session: Session,
    account_uuid: str,
    master_key: str
) -> bool:
    """
    Delete a stock account and all its transactions.
    """
    account = session.get(StockAccount, account_uuid)
    if not account:
        return False

    # Cascade delete for transactions
    account_bidx = hash_index(account_uuid, master_key)
    
    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
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
    account = session.get(StockAccount, account_uuid)
    if not account:
        return None

    transactions = get_account_transactions(session, account_uuid, master_key)
    summary = get_stock_account_summary(session, transactions, db_only=True)
    if summary.current_value is None:
        return None

    total_value = Decimal(summary.current_value)
    total_invested = Decimal(summary.total_invested)
    total_deposits = Decimal(summary.total_deposits)
    total_withdrawals = Decimal(summary.total_withdrawals)

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
        total_deposits=total_deposits,
        total_withdrawals=total_withdrawals,
        total_fees=Decimal(summary.total_fees),
        total_dividends=Decimal(summary.total_dividends),
        daily_pnl=None,
        cumulative_pnl=round(
            Decimal(summary.profit_loss)
            if summary.profit_loss is not None
            else (total_value - total_deposits + total_withdrawals),
            2,
        ),
        positions=positions or None,
    )


def get_stock_account_history(
    session: Session,
    account_uuid: str,
    master_key: str,
    include_current: bool = True,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[AccountHistorySnapshotResponse]:
    """Return decrypted daily snapshots for a stock account, ordered by date."""
    account_id_bidx = hash_index(account_uuid, master_key)
    today = date.today()

    query = select(AccountHistory).where(AccountHistory.account_id_bidx == account_id_bidx)
    if start_date:
        query = query.where(AccountHistory.snapshot_date >= start_date)
    if end_date:
        query = query.where(AccountHistory.snapshot_date <= end_date)

    rows = session.exec(query.order_by(AccountHistory.snapshot_date)).all()

    result: list[AccountHistorySnapshotResponse] = []
    for row in rows:
        total_value = Decimal(decrypt_data(row.total_value_enc, master_key))
        total_invested = Decimal(decrypt_data(row.total_invested_enc, master_key))
        total_deposits = (
            Decimal(decrypt_data(row.total_deposits_enc, master_key))
            if row.total_deposits_enc
            else Decimal("0")
        )
        total_withdrawals = (
            Decimal(decrypt_data(row.total_withdrawals_enc, master_key))
            if row.total_withdrawals_enc
            else Decimal("0")
        )
        daily_pnl = (
            Decimal(decrypt_data(row.daily_pnl_enc, master_key))
            if row.daily_pnl_enc
            else None
        )
        cumulative_pnl = (
            Decimal(decrypt_data(row.cumulative_pnl_enc, master_key))
            if row.cumulative_pnl_enc
            else None
        )
        total_fees = (
            Decimal(decrypt_data(row.total_fees_enc, master_key))
            if row.total_fees_enc
            else None
        )
        total_dividends = (
            Decimal(decrypt_data(row.total_dividends_enc, master_key))
            if row.total_dividends_enc
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
                total_deposits=total_deposits,
                total_withdrawals=total_withdrawals,
                total_fees=total_fees,
                total_dividends=total_dividends,
                daily_pnl=daily_pnl,
                cumulative_pnl=cumulative_pnl,
                positions=positions,
            )
        )

    if include_current and (not end_date or end_date >= today):
        current_snapshot = _build_current_account_snapshot(session, account_uuid, master_key)
        if current_snapshot is not None:
            result = [snap for snap in result if snap.snapshot_date != today]
            result.append(current_snapshot)
    else:
        result = [snap for snap in result if snap.snapshot_date != today]

    result.sort(key=lambda snap: snap.snapshot_date)

    if include_current and len(result) >= 2:
        last_snap = result[-1]
        prev_snap = result[-2]
        if (
            last_snap.snapshot_date == today
            and last_snap.daily_pnl is None
            and last_snap.cumulative_pnl is not None
            and prev_snap.cumulative_pnl is not None
        ):
            last_snap.daily_pnl = round(last_snap.cumulative_pnl - prev_snap.cumulative_pnl, 2)

    return result


def get_all_stock_accounts_history(
    session: Session,
    user_uuid: str,
    master_key: str,
    include_current: bool = True,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[AccountHistorySnapshotResponse]:
    """
    Aggregate daily snapshots across all stock accounts for a user.
    For each date, total_value and total_invested are summed; positions
    are merged by asset_key (quantities and values summed, price kept from
    the last account that had a price for that asset_key).
    """
    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    # Collect per-account snapshots then aggregate by date.
    # Keep daily_pnl as the sum of per-account daily_pnl values to preserve
    # external-flow neutralization already applied at account snapshot level.
    # date -> {"total_value": Decimal, 
    #          "total_invested": Decimal,
    #          "total_deposits": Decimal,
    #          "total_withdrawals": Decimal,
    #          "total_fees": Decimal,
    #          "total_dividends": Decimal,
    #          "daily_pnl": Decimal,
    #          "cumulative_pnl": Decimal,
    #          "positions": {asset_key: {"quantity", "value", "price", "invested"}}}
    aggregated: dict = {}

    for acc in accounts:
        for snap in get_stock_account_history(session, acc.uuid, master_key, include_current, start_date, end_date):
            d = snap.snapshot_date
            if d not in aggregated:
                aggregated[d] = {
                    "total_value": Decimal("0"),
                    "total_invested": Decimal("0"),
                    "total_deposits": Decimal("0"),
                    "total_withdrawals": Decimal("0"),
                    "total_fees": Decimal("0"),
                    "total_dividends": Decimal("0"),
                    "daily_pnl": Decimal("0"),
                    "cumulative_pnl": Decimal("0"),
                    "has_cumulative_pnl": False,
                    "positions": {},
                }
            aggregated[d]["total_value"] += snap.total_value
            aggregated[d]["total_invested"] += snap.total_invested
            aggregated[d]["total_deposits"] += snap.total_deposits
            aggregated[d]["total_withdrawals"] += snap.total_withdrawals
            if snap.total_fees is not None:
                aggregated[d]["total_fees"] += snap.total_fees
            if snap.total_dividends is not None:
                aggregated[d]["total_dividends"] += snap.total_dividends
            if snap.daily_pnl is not None:
                aggregated[d]["daily_pnl"] += snap.daily_pnl
            if snap.cumulative_pnl is not None:
                aggregated[d]["cumulative_pnl"] += snap.cumulative_pnl
                aggregated[d]["has_cumulative_pnl"] = True

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
        daily_pnl = day["daily_pnl"]  # Keep raw sum of daily_pnl

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
                total_deposits=day["total_deposits"],
                total_withdrawals=day["total_withdrawals"],
                total_fees=round(day["total_fees"], 2),
                total_dividends=round(day["total_dividends"], 2),
                daily_pnl=round(daily_pnl, 2) if index > 0 else None,
                cumulative_pnl=(
                    round(day["cumulative_pnl"], 2)
                    if day["has_cumulative_pnl"]
                    else None
                ),
                positions=positions,
            )
        )

    return result