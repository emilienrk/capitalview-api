"""Stock transaction services."""

from decimal import Decimal
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Session, select

from models import StockAccount, StockTransaction
from models.market import MarketPrice
from models.enums import AssetType
from dtos import (
    StockTransactionCreate,
    StockTransactionUpdate,
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_stock_info
from services.market_data import market_data_manager
from services.stock_account import _map_account_to_response


def _decrypt_transaction(tx: StockTransaction, master_key: str) -> TransactionResponse:
    """Decrypt a StockTransaction and return a response with calculated totals."""
    isin = decrypt_data(tx.isin_enc, master_key)
    type_str = decrypt_data(tx.type_enc, master_key)
    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
    price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
    fees = Decimal(decrypt_data(tx.fees_enc, master_key))
    exec_at_str = decrypt_data(tx.executed_at_enc, master_key)
    try:
        executed_at = datetime.fromisoformat(exec_at_str)
    except ValueError:
        executed_at = tx.created_at

    total_cost = (amount * price) + fees
    fees_pct = (fees / total_cost * 100) if total_cost > 0 else Decimal("0")

    return TransactionResponse(
        id=tx.uuid,
        isin=isin,
        symbol=None,
        name=None,
        exchange=None,
        type=type_str,
        amount=amount,
        price_per_unit=price,
        fees=fees,
        executed_at=executed_at,
        total_cost=total_cost,
        fees_percentage=round(fees_pct, 2),
    )


def create_stock_transaction(
    session: Session,
    data: StockTransactionCreate,
    master_key: str,
) -> TransactionResponse:
    """Create a new encrypted stock transaction."""
    if data.isin:
        data.isin = data.isin.strip()
    if data.symbol:
        data.symbol = data.symbol.strip()

    account_bidx = hash_index(data.account_id, master_key)
    isin_enc = encrypt_data(data.isin, master_key)
    type_enc = encrypt_data(data.type.value, master_key)
    amount_enc = encrypt_data(str(data.amount), master_key)
    price_enc = encrypt_data(str(data.price_per_unit), master_key)
    fees_enc = encrypt_data(str(data.fees), master_key)
    exec_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
    

    mp = session.exec(select(MarketPrice).where(MarketPrice.isin == data.isin)).first()
    if not mp:
        market_info = None
        
        if data.symbol:
            market_info = market_data_manager.get_info(data.symbol, AssetType.STOCK)
        
        if not market_info:
            results = market_data_manager.search(data.isin, AssetType.STOCK)
            if results:
                res = results[0]
                market_info = market_data_manager.get_info(res["symbol"], AssetType.STOCK)
                if not market_info:
                    market_info = {
                        "name": res.get("name"),
                        "symbol": res.get("symbol"),
                        "currency": res.get("currency", "EUR"),
                        "price": Decimal("0"),
                        "exchange": res.get("exchange")
                    }

        if not market_info:
            market_info = {
                "symbol": data.symbol,
                "name": data.name or data.symbol,
                "exchange": data.exchange,
                "price": Decimal("0"),
                "currency": "EUR"
            }

        mp = MarketPrice(
            isin=data.isin,
            symbol=market_info.get("symbol") or data.symbol,
            name=market_info.get("name") or data.name,
            exchange=market_info.get("exchange") or data.exchange,
            current_price=market_info.get("price") or Decimal("0"),
            currency=market_info.get("currency") or "EUR",
            last_updated=datetime(2000, 1, 1, tzinfo=timezone.utc)
        )
        session.add(mp)
        session.commit()
    else:
        if data.symbol and not mp.symbol:
            mp.symbol = data.symbol
            session.add(mp)
            session.commit()

    notes_enc = None
    if data.notes:
        notes_enc = encrypt_data(data.notes, master_key)

    transaction = StockTransaction(
        account_id_bidx=account_bidx,
        isin_enc=isin_enc,
        type_enc=type_enc,
        amount_enc=amount_enc,
        price_per_unit_enc=price_enc,
        fees_enc=fees_enc,
        executed_at_enc=exec_at_enc,
        notes_enc=notes_enc,
    )

    session.add(transaction)
    session.commit()
    session.refresh(transaction)

    resp = _decrypt_transaction(transaction, master_key)
    if resp.isin:
        if data.symbol:
            resp.symbol = data.symbol
        if data.exchange:
            resp.exchange = data.exchange
        if data.name:
            resp.name = data.name
        
        if not resp.symbol or not resp.exchange or not resp.name:
             mp = session.exec(select(MarketPrice).where(MarketPrice.isin == resp.isin)).first()
             if mp:
                 if not resp.symbol: resp.symbol = mp.symbol
                 if not resp.exchange: resp.exchange = mp.exchange
                 if not resp.name: resp.name = mp.name
    return resp


def get_stock_transaction(
    session: Session,
    transaction_uuid: str,
    master_key: str,
) -> Optional[TransactionResponse]:
    """Get a single transaction by UUID."""
    transaction = session.get(StockTransaction, transaction_uuid)
    if not transaction:
        return None
    resp = _decrypt_transaction(transaction, master_key)
    
    mp = session.exec(select(MarketPrice).where(MarketPrice.isin == resp.isin)).first()
    if mp:
        resp.symbol = mp.symbol
        resp.exchange = mp.exchange
        resp.name = mp.name
            
    return resp


def update_stock_transaction(
    session: Session,
    transaction: StockTransaction,
    data: StockTransactionUpdate,
    master_key: str,
) -> TransactionResponse:
    """Update an existing stock transaction (only provided fields)."""
    if data.isin:
        data.isin = data.isin.strip()
    if data.symbol:
        data.symbol = data.symbol.strip()

    if data.isin is not None:
        transaction.isin_enc = encrypt_data(data.isin, master_key) if data.isin else None
        
    if data.type is not None:
        transaction.type_enc = encrypt_data(data.type.value, master_key)
    if data.amount is not None:
        transaction.amount_enc = encrypt_data(str(data.amount), master_key)
    if data.price_per_unit is not None:
        transaction.price_per_unit_enc = encrypt_data(str(data.price_per_unit), master_key)
    if data.fees is not None:
        transaction.fees_enc = encrypt_data(str(data.fees), master_key)
    if data.executed_at is not None:
        transaction.executed_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
    if data.notes is not None:
        transaction.notes_enc = encrypt_data(data.notes, master_key)

    session.add(transaction)
    session.commit()
    session.refresh(transaction)

    resp = _decrypt_transaction(transaction, master_key)
    if resp.isin:
        mp = session.exec(select(MarketPrice).where(MarketPrice.isin == resp.isin)).first()
        if mp:
            resp.symbol = mp.symbol
            resp.exchange = mp.exchange
            resp.name = mp.name
            
    return resp


def delete_stock_transaction(session: Session, transaction_uuid: str) -> bool:
    """Delete a transaction by UUID."""
    transaction = session.get(StockTransaction, transaction_uuid)
    if not transaction:
        return False

    session.delete(transaction)
    session.commit()
    return True


def get_account_transactions(
    session: Session,
    account_uuid: str,
    master_key: str,
) -> List[TransactionResponse]:
    """Get all transactions for a specific account with enriched market data."""
    account_bidx = hash_index(account_uuid, master_key)

    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
    ).all()

    decoded_transactions = [_decrypt_transaction(tx, master_key) for tx in transactions]
    
    # Use ISIN for reliable market data lookup
    isins = {tx.isin for tx in decoded_transactions if tx.isin}
    
    market_map = {}
    if isins:
        market_prices = session.exec(
            select(MarketPrice).where(MarketPrice.isin.in_(isins))
        ).all()
        for mp in market_prices:
            market_map[mp.isin] = {
                "name": mp.name, 
                "symbol": mp.symbol, 
                "exchange": mp.exchange
            }
            
    for tx in decoded_transactions:
        # Prioritize locally stored name, fallback to market map
        if tx.isin and tx.isin in market_map:
            if not tx.name:
                tx.name = market_map[tx.isin]["name"]
            if not tx.symbol:
                tx.symbol = market_map[tx.isin]["symbol"]
            if not tx.exchange:
                tx.exchange = market_map[tx.isin]["exchange"]
                
    return decoded_transactions


def get_stock_account_summary(
    session: Session,
    account: StockAccount,
    master_key: str,
) -> AccountSummaryResponse:
    """Build a full account summary with positions and P&L."""
    acc_resp = _map_account_to_response(account, master_key)

    transactions = get_account_transactions(session, account.uuid, master_key)
    transactions.sort(key=lambda x: x.executed_at)

    positions_map: dict[str, dict] = {}

    for tx in transactions:
        position_key = tx.isin
        
        if position_key not in positions_map:
            positions_map[position_key] = {
                "isin": tx.isin,
                "symbol": tx.symbol,
                "name": tx.name,
                "exchange": tx.exchange,
                "total_amount": Decimal("0"),
                "total_cost": Decimal("0"),
                "total_fees": Decimal("0"),
            }

        pos = positions_map[position_key]
        
        if tx.symbol and not pos["symbol"]:
            pos["symbol"] = tx.symbol
        if tx.name and not pos["name"]:
            pos["name"] = tx.name
        if tx.exchange and not pos["exchange"]:
            pos["exchange"] = tx.exchange
        if tx.isin and not pos["isin"]:
            pos["isin"] = tx.isin

        if tx.type in ("BUY", "DIVIDEND", "DEPOSIT"):
            pos["total_amount"] += tx.amount
            pos["total_cost"] += (tx.amount * tx.price_per_unit) + tx.fees

        elif tx.type == "SELL" and pos["total_amount"] > 0:
            fraction = min(tx.amount / pos["total_amount"], Decimal("1"))
            pos["total_amount"] -= tx.amount
            pos["total_cost"] -= pos["total_cost"] * fraction
            pos["total_amount"] = max(pos["total_amount"], Decimal("0"))
            pos["total_cost"] = max(pos["total_cost"], Decimal("0"))

        pos["total_fees"] += tx.fees

    positions: list[PositionResponse] = []
    for position_key, data in positions_map.items():
        if data["total_amount"] <= 0:
            continue
            
        isin = data.get("isin")
        symbol = data.get("symbol")
        name = data.get("name")
        exchange = data.get("exchange")

        total_invested = data["total_cost"]
        avg_price = total_invested / data["total_amount"] if data["total_amount"] > 0 else Decimal("0")
        fees_pct = (data["total_fees"] / total_invested * 100) if total_invested > 0 else Decimal("0")
        
        current_price = None
        market_name = None
        
        if isin:
            market_name, current_price = get_stock_info(session, isin)
        
        final_name = market_name if market_name else name

        current_value = None
        profit_loss = None
        profit_loss_pct = None

        if current_price is not None:
            current_value = data["total_amount"] * current_price
            profit_loss = current_value - total_invested
            profit_loss_pct = (profit_loss / total_invested * 100) if total_invested > 0 else Decimal("0")

        positions.append(PositionResponse(
            symbol=symbol or position_key,
            exchange=exchange,
            name=final_name,
            isin=isin,
            total_amount=data["total_amount"],
            average_buy_price=round(avg_price, 4),
            total_invested=round(total_invested, 2),
            total_fees=round(data["total_fees"], 2),
            fees_percentage=round(fees_pct, 2),
            current_price=current_price,
            current_value=round(current_value, 2) if current_value is not None else None,
            profit_loss=round(profit_loss, 2) if profit_loss is not None else None,
            profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct is not None else None,
        ))

    total_invested_acc = sum(p.total_invested for p in positions)
    total_fees_acc = sum(p.total_fees for p in positions)
    current_value_acc = sum(p.current_value for p in positions if p.current_value is not None)

    profit_loss_acc = None
    profit_loss_pct_acc = None

    if any(p.current_value is not None for p in positions):
        profit_loss_acc = current_value_acc - total_invested_acc
        if total_invested_acc > 0:
            profit_loss_pct_acc = (profit_loss_acc / total_invested_acc * 100)

    return AccountSummaryResponse(
        account_id=acc_resp.id,
        account_name=acc_resp.name,
        account_type=acc_resp.account_type.value,
        total_invested=round(total_invested_acc, 2),
        total_fees=round(total_fees_acc, 2),
        current_value=round(current_value_acc, 2) if current_value_acc is not None else None,
        profit_loss=round(profit_loss_acc, 2) if profit_loss_acc is not None else None,
        profit_loss_percentage=round(profit_loss_pct_acc, 2) if profit_loss_pct_acc is not None else None,
        positions=positions,
    )
