"""Crypto transaction services."""

from decimal import Decimal
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Session, select

from models import CryptoAccount, CryptoTransaction
from models.market import MarketPrice
from dtos import (
    CryptoTransactionCreate, 
    CryptoTransactionUpdate, 
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_crypto_info, get_crypto_price
from services.crypto_account import _map_account_to_response


def _decrypt_transaction(tx: CryptoTransaction, master_key: str, session: Session) -> TransactionResponse:
    """Decrypt CryptoTransaction and calculate totals."""
    symbol = decrypt_data(tx.symbol_enc, master_key)
    type_str = decrypt_data(tx.type_enc, master_key)
    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
    price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
    fees = Decimal(decrypt_data(tx.fees_enc, master_key))
    exec_at_str = decrypt_data(tx.executed_at_enc, master_key)
    try:
        executed_at = datetime.fromisoformat(exec_at_str)
    except ValueError:
        executed_at = tx.created_at
    
    if tx.fees_symbol_enc:
        fees_symbol = decrypt_data(tx.fees_symbol_enc, master_key)
    else:
        fees_symbol = symbol
    
    fees_in_eur = fees
    actual_fees_symbol = fees_symbol or symbol
    
    if actual_fees_symbol != "EUR":
        fees_price = get_crypto_price(session, actual_fees_symbol)
        if fees_price:
            fees_in_eur = fees * fees_price
        else:
             if actual_fees_symbol == "EUR":
                 fees_in_eur = fees

    total_cost = (amount * price)
    total_cost_with_fees = total_cost + fees_in_eur
    fees_pct = (fees_in_eur / total_cost_with_fees * 100) if total_cost_with_fees > 0 else Decimal("0")

    return TransactionResponse(
        id=tx.uuid,
        symbol=symbol,
        isin=symbol,
        type=type_str,
        amount=amount,
        price_per_unit=price,
        fees=fees_in_eur,
        executed_at=executed_at,
        total_cost=total_cost_with_fees,
        fees_percentage=round(fees_pct, 2),
    )


def create_crypto_transaction(
    session: Session,
    data: CryptoTransactionCreate,
    master_key: str
) -> TransactionResponse:
    """Create a new encrypted crypto transaction."""
    if data.name and data.symbol:
        market_price = session.exec(
            select(MarketPrice).where(MarketPrice.isin == data.symbol.upper())
        ).first()
        
        if market_price:
            if not market_price.name:
                market_price.name = data.name
                session.add(market_price)
        else:
            market_price = MarketPrice(
                isin=data.symbol.upper(),
                symbol=data.symbol.upper(),
                name=data.name,
                current_price=Decimal("0"),
                currency="EUR",
                last_updated=datetime(2000, 1, 1, tzinfo=timezone.utc)
            )
            session.add(market_price)

    account_bidx = hash_index(data.account_id, master_key)
    
    if data.fees and data.fees > 0 and data.fees_symbol and data.fees_symbol != "EUR":
        fees_rate = Decimal("0")
        
        if data.fees_symbol == data.symbol:
            fees_rate = data.price_per_unit
        
        else:
            market_rate = get_crypto_price(session, data.fees_symbol)
            if market_rate:
                fees_rate = market_rate
        
        if fees_rate > 0:
            data.fees = data.fees * fees_rate
            data.fees_symbol = "EUR"

    symbol_enc = encrypt_data(data.symbol.upper(), master_key)
    type_enc = encrypt_data(data.type.value, master_key)
    amount_enc = encrypt_data(str(data.amount), master_key)
    price_enc = encrypt_data(str(data.price_per_unit), master_key)
    fees_enc = encrypt_data(str(data.fees), master_key)
    exec_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
    
    notes_enc = None
    if data.notes:
        notes_enc = encrypt_data(data.notes, master_key)
        
    tx_hash_enc = None
    if data.tx_hash:
        tx_hash_enc = encrypt_data(data.tx_hash, master_key)
        
    fees_symbol_enc = None
    if data.fees_symbol:
        fees_symbol_enc = encrypt_data(data.fees_symbol, master_key)

    transaction = CryptoTransaction(
        account_id_bidx=account_bidx,
        symbol_enc=symbol_enc,
        type_enc=type_enc,
        amount_enc=amount_enc,
        price_per_unit_enc=price_enc,
        fees_enc=fees_enc,
        executed_at_enc=exec_at_enc,
        notes_enc=notes_enc,
        tx_hash_enc=tx_hash_enc,
        fees_symbol_enc=fees_symbol_enc
    )
    
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    
    return _decrypt_transaction(transaction, master_key, session)


def get_crypto_transaction(
    session: Session,
    transaction_uuid: str,
    master_key: str
) -> Optional[TransactionResponse]:
    """Get a single transaction by ID."""
    transaction = session.get(CryptoTransaction, transaction_uuid)
    if not transaction:
        return None
    return _decrypt_transaction(transaction, master_key, session)


def update_crypto_transaction(
    session: Session,
    transaction: CryptoTransaction,
    data: CryptoTransactionUpdate,
    master_key: str
) -> TransactionResponse:
    """Update an existing crypto transaction."""
    if data.fees is not None and data.fees > 0 and data.fees_symbol and data.fees_symbol != "EUR":
        fees_rate = Decimal("0")
        target_symbol = data.symbol if data.symbol else None
        
        if target_symbol and data.fees_symbol == target_symbol and data.price_per_unit:
             fees_rate = data.price_per_unit
        else:
            market_rate = get_crypto_price(session, data.fees_symbol)
            if market_rate:
                fees_rate = market_rate
        
        if fees_rate > 0:
            data.fees = data.fees * fees_rate
            data.fees_symbol = "EUR"

    if data.name and data.symbol:
        market_price = session.exec(
            select(MarketPrice).where(MarketPrice.isin == data.symbol.upper())
        ).first()
        
        if market_price:
             if not market_price.name:
                market_price.name = data.name
                session.add(market_price)
        else:
            market_price = MarketPrice(
                isin=data.symbol.upper(),
                symbol=data.symbol.upper(),
                name=data.name,
                current_price=Decimal("0"),
                currency="EUR",
                last_updated=datetime(2000, 1, 1, tzinfo=timezone.utc)
            )
            session.add(market_price)

    if data.symbol is not None:
        transaction.symbol_enc = encrypt_data(data.symbol.upper(), master_key)
        
    if data.type is not None:
        transaction.type_enc = encrypt_data(data.type.value, master_key)
        
    if data.amount is not None:
        transaction.amount_enc = encrypt_data(str(data.amount), master_key)
        
    if data.price_per_unit is not None:
        transaction.price_per_unit_enc = encrypt_data(str(data.price_per_unit), master_key)
        
    if data.fees is not None:
        transaction.fees_enc = encrypt_data(str(data.fees), master_key)
        
    if data.fees_symbol is not None:
        transaction.fees_symbol_enc = encrypt_data(data.fees_symbol, master_key)

    if data.executed_at is not None:
        transaction.executed_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
        
    if data.notes is not None:
        transaction.notes_enc = encrypt_data(data.notes, master_key)
        
    if data.tx_hash is not None:
        transaction.tx_hash_enc = encrypt_data(data.tx_hash, master_key)
        
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    
    return _decrypt_transaction(transaction, master_key, session)


def delete_crypto_transaction(
    session: Session,
    transaction_uuid: str
) -> bool:
    """Delete a transaction."""
    transaction = session.get(CryptoTransaction, transaction_uuid)
    if not transaction:
        return False
        
    session.delete(transaction)
    session.commit()
    return True


def get_account_transactions(
    session: Session,
    account_uuid: str,
    master_key: str
) -> List[TransactionResponse]:
    """Get all transactions for a specific account."""
    account_bidx = hash_index(account_uuid, master_key)
    
    transactions = session.exec(
        select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == account_bidx)
    ).all()
    
    decoded_transactions = [_decrypt_transaction(tx, master_key, session) for tx in transactions]
    
    symbols = {tx.symbol for tx in decoded_transactions if tx.symbol}
    
    market_map = {}
    if symbols:
        market_prices = session.exec(
            select(MarketPrice).where(MarketPrice.isin.in_(symbols))
        ).all()
        for mp in market_prices:
            market_map[mp.isin] = {"name": mp.name, "current_price": mp.current_price}
            
    for tx in decoded_transactions:
        key = tx.symbol
        if key in market_map:
            tx.name = market_map[key]["name"]
            tx.current_price = market_map[key]["current_price"]
            
            if tx.current_price and tx.amount:
                 tx.current_value = tx.amount * tx.current_price
                 
                 if tx.total_cost:
                    tx.profit_loss = tx.current_value - tx.total_cost
                    if tx.total_cost > 0:
                        tx.profit_loss_percentage = (tx.profit_loss / tx.total_cost) * 100

    return decoded_transactions


def get_crypto_account_summary(
    session: Session, 
    account: CryptoAccount, 
    master_key: str
) -> AccountSummaryResponse:
    """Get summary for a crypto account with positions."""
    acc_resp = _map_account_to_response(account, master_key)
    
    transactions = get_account_transactions(session, account.uuid, master_key)
    transactions.sort(key=lambda x: x.executed_at)
    
    positions_map: dict[str, dict] = {}
    
    for tx in transactions:
        symbol = tx.symbol
        if symbol not in positions_map:
            positions_map[symbol] = {
                "symbol": symbol,
                "total_amount": Decimal("0"),
                "total_cost": Decimal("0"),
                "total_fees": Decimal("0"),
            }
        
        pos = positions_map[symbol]
        
        if tx.type in ("BUY", "STAKING", "SWAP"):
            cost_increase = (tx.amount * tx.price_per_unit) + tx.fees
            pos["total_amount"] += tx.amount
            pos["total_cost"] += cost_increase
            
        else:
            if pos["total_amount"] > 0:
                fraction = tx.amount / pos["total_amount"]
                if fraction > 1: fraction = Decimal("1")
                
                cost_removed = pos["total_cost"] * fraction
                
                pos["total_amount"] -= tx.amount
                pos["total_cost"] -= cost_removed
                
                if pos["total_amount"] < 0: pos["total_amount"] = Decimal("0")
                if pos["total_cost"] < 0: pos["total_cost"] = Decimal("0")
        
        pos["total_fees"] += tx.fees

    positions = []
    for symbol, data in positions_map.items():
        if data["total_amount"] <= 0:
            continue
            
        total_invested = data["total_cost"]
        avg_price = total_invested / data["total_amount"] if data["total_amount"] > 0 else Decimal("0")
        
        fees_pct = Decimal("0")
        if total_invested > 0:
             fees_pct = (data["total_fees"] / total_invested * 100)
        
        name, current_price = get_crypto_info(session, symbol)
        
        current_value = None
        profit_loss = None
        profit_loss_pct = None
        
        if current_price:
            current_value = data["total_amount"] * current_price
            profit_loss = current_value - total_invested
            profit_loss_pct = (profit_loss / total_invested * 100) if total_invested > 0 else Decimal("0")
        
        positions.append(PositionResponse(
            symbol=symbol,
            isin=symbol,
            name=name,
            total_amount=data["total_amount"],
            average_buy_price=round(avg_price, 4),
            total_invested=round(total_invested, 2),
            total_fees=round(data["total_fees"], 2),
            fees_percentage=round(fees_pct, 2),
            current_price=current_price,
            current_value=round(current_value, 2) if current_value else None,
            profit_loss=round(profit_loss, 2) if profit_loss else None,
            profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None
        ))

    total_invested_acc = sum(p.total_invested for p in positions)
    total_fees_acc = sum(p.total_fees for p in positions)
    current_value_acc = sum(p.current_value for p in positions if p.current_value)
    
    profit_loss_acc = None
    profit_loss_pct_acc = None
    
    if current_value_acc:
        profit_loss_acc = current_value_acc - total_invested_acc
        if total_invested_acc > 0:
            profit_loss_pct_acc = (profit_loss_acc / total_invested_acc * 100)

    return AccountSummaryResponse(
        account_id=acc_resp.id,
        account_name=acc_resp.name,
        account_type="CRYPTO",
        total_invested=round(total_invested_acc, 2),
        total_fees=round(total_fees_acc, 2),
        current_value=round(current_value_acc, 2) if current_value_acc else None,
        profit_loss=round(profit_loss_acc, 2) if profit_loss_acc else None,
        profit_loss_percentage=round(profit_loss_pct_acc, 2) if profit_loss_pct_acc else None,
        positions=positions
    )