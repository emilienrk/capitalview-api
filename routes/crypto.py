"""Crypto accounts and transactions CRUD routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import User, CryptoAccount, CryptoTransaction
from services.auth import get_current_user, get_master_key
from models.enums import CryptoTransactionType
from dtos import (
    CryptoAccountCreate,
    CryptoAccountUpdate,
    CryptoAccountBasicResponse,
    CryptoBulkImportRequest,
    CryptoBulkImportResponse,
    CryptoTransactionCreate,
    CryptoTransactionUpdate,
    CryptoTransactionBasicResponse,
    AccountSummaryResponse,
    TransactionResponse,
)
from services.crypto_account import (
    create_crypto_account,
    get_crypto_account,
    get_user_crypto_accounts,
    update_crypto_account,
    delete_crypto_account
)
from services.crypto_transaction import (
    create_crypto_transaction,
    get_crypto_transaction,
    get_account_transactions,
    update_crypto_transaction,
    delete_crypto_transaction,
    get_crypto_account_summary
)

router = APIRouter(prefix="/crypto", tags=["Crypto"])


# ============== ACCOUNTS ==============

@router.post("/accounts", response_model=CryptoAccountBasicResponse, status_code=201)
def create_account(
    data: CryptoAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new crypto account/wallet."""
    return create_crypto_account(session, data, current_user.uuid, master_key)


@router.get("/accounts", response_model=list[CryptoAccountBasicResponse])
def list_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """List all crypto accounts for current user."""
    return get_user_crypto_accounts(session, current_user.uuid, master_key)


@router.get("/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a crypto account with positions and calculated values."""
    # Verify ownership
    account_basic = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not account_basic:
        raise HTTPException(status_code=404, detail="Account not found")
        
    # We need the model for the summary service
    account_model = session.get(CryptoAccount, account_id)
    
    return get_crypto_account_summary(session, account_model, master_key)


@router.put("/accounts/{account_id}", response_model=CryptoAccountBasicResponse)
def update_account(
    account_id: str,
    data: CryptoAccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a crypto account."""
    # Verify ownership
    existing = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
        
    account_model = session.get(CryptoAccount, account_id)
    return update_crypto_account(session, account_model, data, master_key)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a crypto account and all its transactions."""
    # Verify ownership
    existing = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
        
    delete_crypto_account(session, account_id, master_key)
    return None


# ============== TRANSACTIONS ==============

@router.post("/transactions", response_model=CryptoTransactionBasicResponse, status_code=201)
def create_transaction(
    data: CryptoTransactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new crypto transaction."""
    # Verify account ownership
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    resp = create_crypto_transaction(session, data, master_key)
    
    return CryptoTransactionBasicResponse(
        id=resp.id,
        account_id=data.account_id,
        ticker=resp.ticker,
        type=data.type,
        amount=resp.amount,
        price_per_unit=resp.price_per_unit,
        fees=data.fees, # Original fees
        fees_ticker=data.fees_ticker,
        executed_at=resp.executed_at,
        notes=data.notes,
        tx_hash=data.tx_hash
    )


@router.get("/transactions", response_model=list[TransactionResponse])
def list_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """List all crypto transactions for current user (history)."""
    # 1. Get all user accounts
    accounts = get_user_crypto_accounts(session, current_user.uuid, master_key)
    
    # 2. Get transactions for each account
    all_transactions = []
    for acc in accounts:
        txs = get_account_transactions(session, acc.id, master_key)
        all_transactions.extend(txs)
        
    # Sort by date desc
    all_transactions.sort(key=lambda x: x.executed_at, reverse=True)
    
    return all_transactions


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
def get_transaction(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a specific crypto transaction."""
    transaction = get_crypto_transaction(session, transaction_id, master_key)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    # Implicit ownership check via decryption success
    return transaction


@router.put("/transactions/{transaction_id}", response_model=CryptoTransactionBasicResponse)
def update_transaction(
    transaction_id: str,
    data: CryptoTransactionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Update a crypto transaction."""
    tx_model = session.get(CryptoTransaction, transaction_id)
    if not tx_model:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    try:
        resp = update_crypto_transaction(session, tx_model, data, master_key)
        
        return CryptoTransactionBasicResponse(
            id=resp.id,
            account_id="unknown", # We don't have account_id easily accessible
            ticker=resp.ticker,
            type=CryptoTransactionType.BUY, # Placeholder
            amount=resp.amount,
            price_per_unit=resp.price_per_unit,
            fees=resp.fees,
            fees_ticker=None,
            executed_at=resp.executed_at,
            notes=None,
            tx_hash=None
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Access denied")


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a crypto transaction."""
    # Implicit ownership check
    tx = get_crypto_transaction(session, transaction_id, master_key)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    delete_crypto_transaction(session, transaction_id)
    return None


@router.get("/transactions/account/{account_id}", response_model=list[TransactionResponse])
def get_transactions_by_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all transactions for a specific account."""
    # Verify account
    account = get_crypto_account(session, account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return get_account_transactions(session, account_id, master_key)


@router.post("/transactions/bulk", response_model=CryptoBulkImportResponse, status_code=201)
def bulk_import_transactions(
    data: CryptoBulkImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Bulk import multiple crypto transactions."""
    # Verify account
    account = get_crypto_account(session, data.account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    created_responses = []
    
    for item in data.transactions:
        create_dto = CryptoTransactionCreate(
            account_id=data.account_id,
            ticker=item.ticker,
            type=item.type,
            amount=item.amount,
            price_per_unit=item.price_per_unit,
            fees=item.fees,
            fees_ticker=item.fees_ticker,
            executed_at=item.executed_at,
            notes=item.notes,
            tx_hash=item.tx_hash
        )
        
        resp = create_crypto_transaction(session, create_dto, master_key)
        
        basic = CryptoTransactionBasicResponse(
            id=resp.id,
            account_id=data.account_id,
            ticker=resp.ticker,
            type=item.type,
            amount=resp.amount,
            price_per_unit=resp.price_per_unit,
            fees=item.fees, 
            fees_ticker=item.fees_ticker,
            executed_at=resp.executed_at,
            notes=item.notes,
            tx_hash=item.tx_hash
        )
        created_responses.append(basic)

    return CryptoBulkImportResponse(
        imported_count=len(created_responses),
        transactions=created_responses
    )
