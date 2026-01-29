"""Portfolio routes."""

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from database import get_session
from models import (
    StockAccount, StockTransaction,
    CryptoAccount, CryptoTransaction
)
from schemas import TransactionResponse, AccountSummaryResponse, PortfolioResponse
from services.portfolio import (
    calculate_transaction,
    get_market_price,
    get_stock_account_summary,
    get_crypto_account_summary,
    get_user_portfolio
)

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])


# ============== TRANSACTIONS ==============

@router.get("/stocks/transactions", response_model=list[TransactionResponse])
def get_all_stock_transactions(session: Session = Depends(get_session)):
    """Get all stock transactions with calculated fields."""
    transactions = session.exec(select(StockTransaction)).all()
    
    result = []
    for tx in transactions:
        current_price = get_market_price(session, tx.ticker)
        result.append(calculate_transaction(tx, current_price))
    
    return result


@router.get("/crypto/transactions", response_model=list[TransactionResponse])
def get_all_crypto_transactions(session: Session = Depends(get_session)):
    """Get all crypto transactions with calculated fields."""
    transactions = session.exec(select(CryptoTransaction)).all()
    
    result = []
    for tx in transactions:
        current_price = get_market_price(session, tx.ticker)
        result.append(calculate_transaction(tx, current_price))
    
    return result


# ============== ACCOUNTS ==============

@router.get("/stocks/accounts", response_model=list[AccountSummaryResponse])
def get_stock_accounts(session: Session = Depends(get_session)):
    """Get all stock accounts with positions and calculated values."""
    accounts = session.exec(select(StockAccount)).all()
    return [get_stock_account_summary(session, acc) for acc in accounts]


@router.get("/stocks/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_stock_account(account_id: int, session: Session = Depends(get_session)):
    """Get a specific stock account with positions."""
    account = session.get(StockAccount, account_id)
    if not account:
        return {"error": "Account not found"}
    return get_stock_account_summary(session, account)


@router.get("/crypto/accounts", response_model=list[AccountSummaryResponse])
def get_crypto_accounts(session: Session = Depends(get_session)):
    """Get all crypto accounts with positions and calculated values."""
    accounts = session.exec(select(CryptoAccount)).all()
    return [get_crypto_account_summary(session, acc) for acc in accounts]


@router.get("/crypto/accounts/{account_id}", response_model=AccountSummaryResponse)
def get_crypto_account(account_id: int, session: Session = Depends(get_session)):
    """Get a specific crypto account with positions."""
    account = session.get(CryptoAccount, account_id)
    if not account:
        return {"error": "Account not found"}
    return get_crypto_account_summary(session, account)


# ============== PORTFOLIO ==============

@router.get("/user/{user_id}", response_model=PortfolioResponse)
def get_portfolio(user_id: int, session: Session = Depends(get_session)):
    """Get complete portfolio for a user with all accounts and positions."""
    return get_user_portfolio(session, user_id)
