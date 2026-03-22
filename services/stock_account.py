"""Stock account services."""

import json
from decimal import Decimal
from typing import List, Optional

from sqlmodel import Session, select

from models import StockAccount, StockAccountType, StockTransaction
from models.account_history import AccountHistory
from dtos import (
    StockAccountCreate,
    StockAccountUpdate,
    StockAccountBasicResponse,
)
from dtos.transaction import AccountHistoryPosition, AccountHistorySnapshotResponse
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
) -> List[StockAccountBasicResponse]:
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
) -> Optional[StockAccountBasicResponse]:
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
        
    session.delete(account)
    session.commit()
    return True


def get_stock_account_history(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> list[AccountHistorySnapshotResponse]:
    """Return decrypted daily snapshots for a stock account, ordered by date."""
    account_id_bidx = hash_index(account_uuid, master_key)

    rows = session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .order_by(AccountHistory.snapshot_date)
    ).all()

    result = []
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

        result.append(
            AccountHistorySnapshotResponse(
                snapshot_date=row.snapshot_date,
                total_value=total_value,
                total_invested=total_invested,
                daily_pnl=daily_pnl,
                positions=positions,
            )
        )

    return result


def get_all_stock_accounts_history(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> list[AccountHistorySnapshotResponse]:
    """
    Aggregate daily snapshots across all stock accounts for a user.
    For each date, total_value and total_invested are summed; positions
    are merged by symbol (quantities and values summed, price kept from
    the last account that had a price for that symbol).
    """
    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    # Collect per-account snapshots then aggregate by date
    # date -> {"total_value": Decimal, "total_invested": Decimal,
    #          "positions": {symbol: {"quantity", "value", "price", "invested"}}}
    aggregated: dict = {}

    for acc in accounts:
        for snap in get_stock_account_history(session, acc.uuid, master_key):
            d = snap.snapshot_date
            if d not in aggregated:
                aggregated[d] = {
                    "total_value": Decimal("0"),
                    "total_invested": Decimal("0"),
                    "positions": {},
                }
            aggregated[d]["total_value"] += snap.total_value
            aggregated[d]["total_invested"] += snap.total_invested

            for pos in (snap.positions or []):
                sym = pos.symbol
                if sym not in aggregated[d]["positions"]:
                    aggregated[d]["positions"][sym] = {
                        "quantity": Decimal("0"),
                        "value": Decimal("0"),
                        "price": None,
                        "invested": Decimal("0"),
                    }
                aggregated[d]["positions"][sym]["quantity"] += pos.quantity
                aggregated[d]["positions"][sym]["value"] += pos.value
                aggregated[d]["positions"][sym]["invested"] += pos.invested
                if pos.price is not None:
                    aggregated[d]["positions"][sym]["price"] = pos.price

    result = []
    prev_value: Decimal | None = None
    for d in sorted(aggregated):
        day = aggregated[d]
        total_value = day["total_value"]
        daily_pnl = (total_value - prev_value) if prev_value is not None else None
        positions = [
            AccountHistoryPosition(
                symbol=sym,
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
            for sym, data in day["positions"].items()
        ] or None
        result.append(
            AccountHistorySnapshotResponse(
                snapshot_date=d,
                total_value=total_value,
                total_invested=day["total_invested"],
                daily_pnl=round(daily_pnl, 2) if daily_pnl is not None else None,
                positions=positions,
            )
        )
        prev_value = total_value

    return result