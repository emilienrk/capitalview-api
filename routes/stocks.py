"""Stock accounts and transactions CRUD routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import StockAccount, StockTransaction, User
from services.auth import get_current_user
from models.enums import StockAccountType, StockTransactionType
from schemas import (
    StockAccountCreate,
    StockAccountUpdate,
    StockAccountBasicResponse,
    StockTransactionCreate,
    StockTransactionUpdate,
    StockTransactionBasicResponse,
    AccountSummaryResponse,
    TransactionResponse,
)
from services.stocks import (
    get_stock_account_summary,
    calculate_stock_transaction,
)

router = APIRouter(prefix="/stocks", tags=["Stocks"])


# ============== ACCOUNTS ==============

@router.post("/accounts", response_model=StockAccountBasicResponse, status_code=201)
def create_stock_account(
    data: StockAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Create a new stock account."""

    # Validate account_type enum
    try:
        account_type = StockAccountType(data.account_type)
    except ValueError:
        valid_types = [t.value for t in StockAccountType]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid account_type. Must be one of: {valid_types}"
        )
    
    account = StockAccount(
        user_id=current_user.id,
        name=data.name,
        account_type=account_type,
        bank_name=data.bank_name,
        encrypted_iban=data.encrypted_iban,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return StockAccountBasicResponse(
        id=account.id,
        name=account.name,
        account_type=account.account_type.value,
        bank_name=account.bank_name,
        created_at=account.created_at,
    )


@router.get("/accounts", response_model=list[StockAccountBasicResponse])
def list_stock_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """List all stock accounts (basic info)."""
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_id == current_user.id)
    ).all()
    return [
        StockAccountBasicResponse(
            id=acc.id,
            name=acc.name,
            account_type=acc.account_type.value,
            bank_name=acc.bank_name,
            created_at=acc.created_at,
        )
        for acc in accounts
    ]


@router.get("/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_stock_account(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get a stock account with positions and calculated values."""
    account = session.get(StockAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return get_stock_account_summary(session, account)


@router.put("/accounts/{account_id}", response_model=StockAccountBasicResponse)
def update_stock_account(
    account_id: int,
    data: StockAccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Update a stock account."""
    account = session.get(StockAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if data.name is not None:
        account.name = data.name
    if data.bank_name is not None:
        account.bank_name = data.bank_name
    if data.encrypted_iban is not None:
        account.encrypted_iban = data.encrypted_iban
    
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return StockAccountBasicResponse(
        id=account.id,
        name=account.name,
        account_type=account.account_type.value,
        bank_name=account.bank_name,
        created_at=account.created_at,
    )


@router.delete("/accounts/{account_id}", status_code=204)
def delete_stock_account(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Delete a stock account and all its transactions."""
    account = session.get(StockAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    session.delete(account)
    session.commit()
    return None


@router.get("/accounts/me", response_model=list[StockAccountBasicResponse])
def get_my_stock_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get all stock accounts for current authenticated user."""
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_id == current_user.id)
    ).all()
    return [
        StockAccountBasicResponse(
            id=acc.id,
            name=acc.name,
            account_type=acc.account_type.value,
            bank_name=acc.bank_name,
            created_at=acc.created_at,
        )
        for acc in accounts
    ]

# ============== TRANSACTIONS ==============

@router.post("/transactions", response_model=StockTransactionBasicResponse, status_code=201)
def create_stock_transaction(
    data: StockTransactionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Create a new stock transaction."""
    # Validate account exists
    account = session.get(StockAccount, data.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Validate transaction type enum
    try:
        tx_type = StockTransactionType(data.type)
    except ValueError:
        valid_types = [t.value for t in StockTransactionType]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type. Must be one of: {valid_types}"
        )
    
    transaction = StockTransaction(
        account_id=data.account_id,
        ticker=data.ticker.upper(),
        exchange=data.exchange,
        type=tx_type,
        amount=data.amount,
        price_per_unit=data.price_per_unit,
        fees=data.fees,
        executed_at=data.executed_at,
    )
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    
    return StockTransactionBasicResponse(
        id=transaction.id,
        account_id=transaction.account_id,
        ticker=transaction.ticker,
        exchange=transaction.exchange,
        type=transaction.type.value,
        amount=transaction.amount,
        price_per_unit=transaction.price_per_unit,
        fees=transaction.fees,
        executed_at=transaction.executed_at,
    )


@router.get("/transactions", response_model=list[TransactionResponse])
def list_stock_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """List all stock transactions for current user (history)."""
    # Get all accounts for current user
    user_account_ids = [
        acc.id for acc in session.exec(
            select(StockAccount).where(StockAccount.user_id == current_user.id)
        ).all()
    ]
    
    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id.in_(user_account_ids))
    ).all()
    return [calculate_stock_transaction(tx) for tx in transactions]


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
def get_stock_transaction(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get a specific stock transaction."""
    transaction = session.get(StockTransaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    account = session.get(StockAccount, transaction.account_id)
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return calculate_stock_transaction(transaction)


@router.put("/transactions/{transaction_id}", response_model=StockTransactionBasicResponse)
def update_stock_transaction(
    transaction_id: int,
    data: StockTransactionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Update a stock transaction."""
    transaction = session.get(StockTransaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    account = session.get(StockAccount, transaction.account_id)
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if data.ticker is not None:
        transaction.ticker = data.ticker.upper()
    if data.exchange is not None:
        transaction.exchange = data.exchange
    if data.type is not None:
        try:
            transaction.type = StockTransactionType(data.type)
        except ValueError:
            valid_types = [t.value for t in StockTransactionType]
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
    if data.executed_at is not None:
        transaction.executed_at = data.executed_at
    
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    
    return StockTransactionBasicResponse(
        id=transaction.id,
        account_id=transaction.account_id,
        ticker=transaction.ticker,
        exchange=transaction.exchange,
        type=transaction.type.value,
        amount=transaction.amount,
        price_per_unit=transaction.price_per_unit,
        fees=transaction.fees,
        executed_at=transaction.executed_at,
    )


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_stock_transaction(
    transaction_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Delete a stock transaction."""
    transaction = session.get(StockTransaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    
    account = session.get(StockAccount, transaction.account_id)
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
    account = session.get(StockAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id == account_id)
    ).all()
    return [calculate_stock_transaction(tx) for tx in transactions]
