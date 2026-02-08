"""Crypto transaction services."""

from decimal import Decimal
from datetime import datetime
from typing import List, Optional

from sqlmodel import Session, select

from models import CryptoAccount, CryptoTransaction, CryptoTransactionType
from dtos import (
    CryptoTransactionCreate, 
    CryptoTransactionUpdate, 
    CryptoTransactionBasicResponse, # Needed?
    TransactionResponse, # Shared DTO
    PositionResponse,
    AccountSummaryResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.market import get_market_info, get_market_price
from services.crypto_account import _map_account_to_response


def _decrypt_transaction(tx: CryptoTransaction, master_key: str, session: Session) -> TransactionResponse:
    """Decrypt CryptoTransaction and calculate totals."""
    ticker = decrypt_data(tx.ticker_enc, master_key)
    type_str = decrypt_data(tx.type_enc, master_key)
    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
    price = Decimal(decrypt_data(tx.price_per_unit_enc, master_key))
    fees = Decimal(decrypt_data(tx.fees_enc, master_key))
    
    fees_ticker = None
    if tx.fees_ticker_enc:
        fees_ticker = decrypt_data(tx.fees_ticker_enc, master_key)
    
    # Decrypt executed_at
    exec_at_str = decrypt_data(tx.executed_at_enc, master_key)
    try:
        executed_at = datetime.fromisoformat(exec_at_str)
    except ValueError:
        executed_at = tx.created_at

    # Calculate Fees in EUR
    fees_in_eur = fees
    actual_fees_ticker = fees_ticker or ticker
    
    if actual_fees_ticker != "EUR":
        fees_price = get_market_price(session, actual_fees_ticker)
        if fees_price:
            fees_in_eur = fees * fees_price
        else:
            pass

    # Calculate totals
    total_cost = (amount * price)
    total_cost_with_fees = total_cost + fees_in_eur
    fees_pct = (fees_in_eur / total_cost_with_fees * 100) if total_cost_with_fees > 0 else Decimal("0")

    return TransactionResponse(
        id=tx.uuid,
        ticker=ticker,
        type=type_str,
        amount=amount,
        price_per_unit=price,
        fees=fees_in_eur, # Returning converted fees
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
    account_bidx = hash_index(data.account_id, master_key)
    
    ticker_enc = encrypt_data(data.ticker.upper(), master_key)
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
        
    fees_ticker_enc = None
    if data.fees_ticker:
        fees_ticker_enc = encrypt_data(data.fees_ticker, master_key)

    transaction = CryptoTransaction(
        account_id_bidx=account_bidx,
        ticker_enc=ticker_enc,
        type_enc=type_enc,
        amount_enc=amount_enc,
        price_per_unit_enc=price_enc,
        fees_enc=fees_enc,
        executed_at_enc=exec_at_enc,
        notes_enc=notes_enc,
        tx_hash_enc=tx_hash_enc,
        fees_ticker_enc=fees_ticker_enc
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
    if data.ticker is not None:
        transaction.ticker_enc = encrypt_data(data.ticker.upper(), master_key)
        
    if data.type is not None:
        transaction.type_enc = encrypt_data(data.type.value, master_key)
        
    if data.amount is not None:
        transaction.amount_enc = encrypt_data(str(data.amount), master_key)
        
    if data.price_per_unit is not None:
        transaction.price_per_unit_enc = encrypt_data(str(data.price_per_unit), master_key)
        
    if data.fees is not None:
        transaction.fees_enc = encrypt_data(str(data.fees), master_key)
        
    if data.fees_ticker is not None:
        transaction.fees_ticker_enc = encrypt_data(data.fees_ticker, master_key)

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
    
    return [_decrypt_transaction(tx, master_key, session) for tx in transactions]


def get_crypto_account_summary(
    session: Session, 
    account: CryptoAccount, 
    master_key: str
) -> AccountSummaryResponse:
    """Get summary for a crypto account with positions."""
    acc_resp = _map_account_to_response(account, master_key)
    
    # 1. Get and Sort Transactions
    transactions = get_account_transactions(session, account.uuid, master_key)
    transactions.sort(key=lambda x: x.executed_at)
    
    positions_map: dict[str, dict] = {}
    
    # 2. Process Transactions (PRU Calculation)
    for tx in transactions:
        ticker = tx.ticker
        if ticker not in positions_map:
            positions_map[ticker] = {
                "ticker": ticker,
                "total_amount": Decimal("0"),
                "total_cost": Decimal("0"), # Weighted Cost Basis
                "total_fees": Decimal("0"),
            }
        
        pos = positions_map[ticker]
        
        # BUY Logic
        if tx.type in ("BUY", "STAKING", "SWAP"):
            # Cost Basis Increase = (Amount * Price) + Fees
            # Note: tx.fees is already in EUR in the response object
            cost_increase = (tx.amount * tx.price_per_unit) + tx.fees
            pos["total_amount"] += tx.amount
            pos["total_cost"] += cost_increase
            
        # SELL Logic
        else: # SELL
            if pos["total_amount"] > 0:
                # Reduce Cost Proportinally (Weighted Average)
                fraction = tx.amount / pos["total_amount"]
                if fraction > 1: fraction = Decimal("1")
                
                cost_removed = pos["total_cost"] * fraction
                
                pos["total_amount"] -= tx.amount
                pos["total_cost"] -= cost_removed
                
                if pos["total_amount"] < 0: pos["total_amount"] = Decimal("0")
                if pos["total_cost"] < 0: pos["total_cost"] = Decimal("0")
        
        pos["total_fees"] += tx.fees

    # 3. Finalize Positions
    positions = []
    for ticker, data in positions_map.items():
        if data["total_amount"] <= 0:
            continue
            
        total_invested = data["total_cost"]
        avg_price = total_invested / data["total_amount"] if data["total_amount"] > 0 else Decimal("0")
        
        fees_pct = Decimal("0")
        if total_invested > 0:
             fees_pct = (data["total_fees"] / total_invested * 100)
        
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
        account_type="CRYPTO",
        total_invested=round(total_invested_acc, 2),
        total_fees=round(total_fees_acc, 2),
        current_value=round(current_value_acc, 2) if current_value_acc else None,
        profit_loss=round(profit_loss_acc, 2) if profit_loss_acc else None,
        profit_loss_percentage=round(profit_loss_pct_acc, 2) if profit_loss_pct_acc else None,
        positions=positions
    )