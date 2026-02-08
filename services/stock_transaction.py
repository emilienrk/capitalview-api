"""Stock transaction services."""

from decimal import Decimal
from datetime import datetime
from typing import List, Optional

from sqlmodel import Session, select

from models import StockAccount, StockTransaction, StockTransactionType
from dtos import (
    StockTransactionCreate, 
    StockTransactionUpdate, 
    TransactionResponse,
    PositionResponse,
    AccountSummaryResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_market_info
from services.stock_account import _map_account_to_response


def _decrypt_transaction(tx: StockTransaction, master_key: str) -> TransactionResponse:
    """Decrypt StockTransaction and calculate totals."""
    ticker = decrypt_data(tx.ticker_enc, master_key)
    type_str = decrypt_data(tx.type_enc, master_key)
    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
    price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
    fees = Decimal(decrypt_data(tx.fees_enc, master_key))
    
    # Decrypt and parse executed_at
    exec_at_str = decrypt_data(tx.executed_at_enc, master_key)
    try:
        executed_at = datetime.fromisoformat(exec_at_str)
    except ValueError:
        # Fallback if parsing fails (should not happen if consistent)
        executed_at = tx.created_at

    # Calculate totals
    total_cost = (amount * price) + fees
    fees_pct = (fees / total_cost * 100) if total_cost > 0 else Decimal("0")

    return TransactionResponse(
        id=tx.id,
        ticker=ticker,
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
    master_key: str
) -> TransactionResponse:
    """Create a new encrypted stock transaction."""
    account_bidx = hash_index(str(data.account_id), master_key)
    
    ticker_enc = encrypt_data(data.ticker.upper(), master_key)
    type_enc = encrypt_data(data.type.value, master_key)
    amount_enc = encrypt_data(str(data.amount), master_key)
    price_enc = encrypt_data(str(data.price_per_unit), master_key)
    fees_enc = encrypt_data(str(data.fees), master_key)
    exec_at_enc = encrypt_data(data.executed_at.isoformat(), master_key)
    
    notes_enc = None
    if data.notes:
        notes_enc = encrypt_data(data.notes, master_key)
        
    exchange_enc = encrypt_data(data.exchange or "", master_key)

    transaction = StockTransaction(
        account_id_bidx=account_bidx,
        ticker_enc=ticker_enc,
        exchange_enc=exchange_enc,
        type_enc=type_enc,
        amount_enc=amount_enc,
        price_per_unit_enc=price_enc,
        fees_enc=fees_enc,
        executed_at_enc=exec_at_enc,
        notes_enc=notes_enc
    )
    
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    
    return _decrypt_transaction(transaction, master_key)


def get_stock_transaction(
    session: Session,
    transaction_id: int,
    master_key: str
) -> Optional[TransactionResponse]:
    """Get a single transaction by ID."""
    transaction = session.get(StockTransaction, transaction_id)
    if not transaction:
        return None
    return _decrypt_transaction(transaction, master_key)


def update_stock_transaction(
    session: Session,
    transaction: StockTransaction,
    data: StockTransactionUpdate,
    master_key: str
) -> TransactionResponse:
    """Update an existing stock transaction."""
    if data.ticker is not None:
        transaction.ticker_enc = encrypt_data(data.ticker.upper(), master_key)
    
    if data.exchange is not None:
        transaction.exchange_enc = encrypt_data(data.exchange, master_key)
        
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
    
    return _decrypt_transaction(transaction, master_key)


def delete_stock_transaction(
    session: Session,
    transaction_id: int
) -> bool:
    """Delete a transaction."""
    transaction = session.get(StockTransaction, transaction_id)
    if not transaction:
        return False
        
    session.delete(transaction)
    session.commit()
    return True


def get_account_transactions(
    session: Session,
    account_id: int,
    master_key: str
) -> List[TransactionResponse]:
    """Get all transactions for a specific account."""
    account_bidx = hash_index(str(account_id), master_key)
    
    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
    ).all()
    
    return [_decrypt_transaction(tx, master_key) for tx in transactions]


def get_stock_account_summary(
    session: Session, 
    account: StockAccount, 
    master_key: str
) -> AccountSummaryResponse:
    """Get summary for a stock account with positions."""
    acc_resp = _map_account_to_response(account, master_key)
    
    transactions = get_account_transactions(session, account.id, master_key)
    
    positions_map: dict[str, dict] = {}
    
    for tx in transactions:
        ticker = tx.ticker
        if ticker not in positions_map:
            positions_map[ticker] = {
                "ticker": ticker,
                "total_amount": Decimal("0"),
                "total_cost": Decimal("0"),
                "total_fees": Decimal("0"),
            }
        
        if tx.type in ("BUY", "DIVIDEND", "DEPOSIT"):
            positions_map[ticker]["total_amount"] += tx.amount
            positions_map[ticker]["total_cost"] += (tx.amount * tx.price_per_unit)
        else:  # SELL
            positions_map[ticker]["total_amount"] -= tx.amount
            positions_map[ticker]["total_cost"] -= (tx.amount * tx.price_per_unit)
            
        positions_map[ticker]["total_fees"] += tx.fees

    positions = []
    for ticker, data in positions_map.items():
        if data["total_amount"] <= 0:
            continue
            
        total_invested = data["total_cost"] + data["total_fees"]
        avg_price = data["total_cost"] / data["total_amount"] if data["total_amount"] > 0 else Decimal("0")
        fees_pct = (data["total_fees"] / total_invested * 100) if total_invested > 0 else Decimal("0")
        
        # Market Price Fetching (Cleartext service)
        name, current_price = get_market_info(session, ticker)
        
        current_value = None
        profit_loss = None
        profit_loss_pct = None
        
        if current_price:
            current_value = data["total_amount"] * current_price
            profit_loss = current_value - total_invested
            profit_loss_pct = (profit_loss / total_invested * 100) if total_invested > 0 else Decimal("0")
        
        positions.append(PositionResponse(
            ticker=ticker,
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
        account_type=acc_resp.account_type.value,
        total_invested=round(total_invested_acc, 2),
        total_fees=round(total_fees_acc, 2),
        current_value=round(current_value_acc, 2) if current_value_acc else None,
        profit_loss=round(profit_loss_acc, 2) if profit_loss_acc else None,
        profit_loss_percentage=round(profit_loss_pct_acc, 2) if profit_loss_pct_acc else None,
        positions=positions
    )
