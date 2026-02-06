"""Crypto accounts and transactions CRUD routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import CryptoAccount, CryptoTransaction, User
from services.auth import get_current_user
from models.enums import CryptoTransactionType
from schemas import (
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
from services.crypto import (
    get_crypto_account_summary,
    calculate_crypto_transaction,
)

router = APIRouter(prefix="/crypto", tags=["Crypto"])


# ============== ACCOUNTS ==============

@router.post("/accounts", response_model=CryptoAccountBasicResponse, status_code=201)
def create_crypto_account(
    data: CryptoAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Create a new crypto account/wallet."""
    account = CryptoAccount(
        user_id=current_user.id,
        name=data.name,
        wallet_name=data.wallet_name,
        public_address=data.public_address,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return CryptoAccountBasicResponse(
        id=account.id,
        name=account.name,
        wallet_name=account.wallet_name,
        public_address=account.public_address,
        created_at=account.created_at,
    )


@router.get("/accounts", response_model=list[CryptoAccountBasicResponse])
def list_crypto_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """List all crypto accounts for current user."""
    accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_id == current_user.id)
    ).all()
    return [
        CryptoAccountBasicResponse(
            id=acc.id,
            name=acc.name,
            wallet_name=acc.wallet_name,
            public_address=acc.public_address,
            created_at=acc.created_at,
        )
        for acc in accounts
    ]


@router.get("/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_crypto_account(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get a crypto account with positions and calculated values."""
    account = session.get(CryptoAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return get_crypto_account_summary(session, account)


@router.put("/accounts/{account_id}", response_model=CryptoAccountBasicResponse)
def update_crypto_account(
    account_id: int,
    data: CryptoAccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Update a crypto account."""
    account = session.get(CryptoAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if data.name is not None:
        account.name = data.name
    if data.wallet_name is not None:
        account.wallet_name = data.wallet_name
    if data.public_address is not None:
        account.public_address = data.public_address
    
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return CryptoAccountBasicResponse(
        id=account.id,
        name=account.name,
        wallet_name=account.wallet_name,
        public_address=account.public_address,
        created_at=account.created_at,
    )


@router.delete("/accounts/{account_id}", status_code=204)
def delete_crypto_account(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Delete a crypto account and all its transactions."""
    account = session.get(CryptoAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    session.delete(account)
    session.commit()
    return None


# ============== TRANSACTIONS ==============

@router.post("/transactions", response_model=CryptoTransactionBasicResponse, status_code=201)
def create_crypto_transaction(
    data: CryptoTransactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Create a new crypto transaction."""
    # Validate account exists
    account = session.get(CryptoAccount, data.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Validate transaction type enum
    try:
        tx_type = CryptoTransactionType(data.type)
    except ValueError:
        valid_types = [t.value for t in CryptoTransactionType]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type. Must be one of: {valid_types}"
        )
    
    transaction = CryptoTransaction(
        account_id=data.account_id,
        ticker=data.ticker.upper(),
        type=tx_type,
        amount=data.amount,
        price_per_unit=data.price_per_unit,
        fees=data.fees,
        fees_ticker=data.fees_ticker,
        executed_at=data.executed_at,
    )
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    
    return CryptoTransactionBasicResponse(
        id=transaction.id,
        account_id=transaction.account_id,
        ticker=transaction.ticker,
        type=transaction.type.value,
        amount=transaction.amount,
        price_per_unit=transaction.price_per_unit,
        fees=transaction.fees,
        fees_ticker=transaction.fees_ticker,
        executed_at=transaction.executed_at,
    )


@router.get("/transactions", response_model=list[TransactionResponse])
def list_crypto_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """List all crypto transactions for current user (history)."""

    user_account_ids = [
        acc.id for acc in session.exec(
            select(CryptoAccount).where(CryptoAccount.user_id == current_user.id)
        ).all()
    ]
    
    transactions = session.exec(
        select(CryptoTransaction).where(CryptoTransaction.account_id.in_(user_account_ids))
    ).all()
    return [calculate_crypto_transaction(tx, session) for tx in transactions]


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
def get_crypto_transaction(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get a specific crypto transaction."""
    transaction = session.get(CryptoTransaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    account = session.get(CryptoAccount, transaction.account_id)
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return calculate_crypto_transaction(transaction, session)


@router.put("/transactions/{transaction_id}", response_model=CryptoTransactionBasicResponse)
def update_crypto_transaction(
    transaction_id: int,
    data: CryptoTransactionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Update a crypto transaction."""
    transaction = session.get(CryptoTransaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    account = session.get(CryptoAccount, transaction.account_id)
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if data.ticker is not None:
        transaction.ticker = data.ticker.upper()
    if data.type is not None:
        try:
            transaction.type = CryptoTransactionType(data.type)
        except ValueError:
            valid_types = [t.value for t in CryptoTransactionType]
            raise HTTPException(
                status_code=400,
                detail=f"Invalid type. Must be one of: {valid_types}"
            )
    if data.amount is not None:
        transaction.amount = data.amount
    if data.price_per_unit is not None:
        transaction.price_per_unit = data.price_per_unit
    if data.fees is not None:
        transaction.fees = data.fees
    if data.fees_ticker is not None:
        transaction.fees_ticker = data.fees_ticker
    if data.executed_at is not None:
        transaction.executed_at = data.executed_at
    
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    
    return CryptoTransactionBasicResponse(
        id=transaction.id,
        account_id=transaction.account_id,
        ticker=transaction.ticker,
        type=transaction.type.value,
        amount=transaction.amount,
        price_per_unit=transaction.price_per_unit,
        fees=transaction.fees,
        fees_ticker=transaction.fees_ticker,
        executed_at=transaction.executed_at,
    )


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_crypto_transaction(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Delete a crypto transaction."""
    transaction = session.get(CryptoTransaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    account = session.get(CryptoAccount, transaction.account_id)
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    session.delete(transaction)
    session.commit()
    return None


@router.get("/transactions/account/{account_id}", response_model=list[TransactionResponse])
def get_account_transactions(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get all transactions for a specific account."""

    account = session.get(CryptoAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    transactions = session.exec(
        select(CryptoTransaction).where(CryptoTransaction.account_id == account_id)
    ).all()
    return [calculate_crypto_transaction(tx, session) for tx in transactions]


@router.post("/transactions/bulk", response_model=CryptoBulkImportResponse, status_code=201)
def bulk_import_crypto_transactions(
    data: CryptoBulkImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Bulk import multiple crypto transactions for a given account."""
    # Validate account exists and belongs to user
    account = session.get(CryptoAccount, data.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if not data.transactions:
        raise HTTPException(status_code=400, detail="No transactions provided")

    created: list[CryptoTransaction] = []
    for tx_data in data.transactions:
        # Validate transaction type enum
        try:
            tx_type = CryptoTransactionType(tx_data.type)
        except ValueError:
            valid_types = [t.value for t in CryptoTransactionType]
            raise HTTPException(
                status_code=400,
                detail=f"Invalid type '{tx_data.type}' for ticker '{tx_data.ticker}'. Must be one of: {valid_types}"
            )

        transaction = CryptoTransaction(
            account_id=data.account_id,
            ticker=tx_data.ticker.upper(),
            type=tx_type,
            amount=tx_data.amount,
            price_per_unit=tx_data.price_per_unit,
            fees=tx_data.fees,
            fees_ticker=tx_data.fees_ticker,
            executed_at=tx_data.executed_at,
        )
        session.add(transaction)
        created.append(transaction)

    session.commit()
    for tx in created:
        session.refresh(tx)

    return CryptoBulkImportResponse(
        imported_count=len(created),
        transactions=[
            CryptoTransactionBasicResponse(
                id=tx.id,
                account_id=tx.account_id,
                ticker=tx.ticker,
                type=tx.type.value,
                amount=tx.amount,
                price_per_unit=tx.price_per_unit,
                fees=tx.fees,
                fees_ticker=tx.fees_ticker,
                executed_at=tx.executed_at,
            )
            for tx in created
        ],
    )
