"""Dashboard routes - Personal portfolio overview."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from database import get_session
from models import User, StockAccount, CryptoAccount
from dtos import PortfolioResponse
from services.auth import get_current_user, get_master_key
from services.encryption import hash_index
from services.exchange_rate import get_exchange_rate, convert_account_to_eur
from services.stock_transaction import get_stock_account_summary
from services.crypto_transaction import get_crypto_account_summary

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/exchange-rate")
def get_rate(
    current_user: Annotated[User, Depends(get_current_user)],
    from_currency: str = "USD",
    to_currency: str = "EUR",
) -> dict:
    """Return a cached exchange rate (e.g. USD→EUR) for frontend use."""
    rate = get_exchange_rate(from_currency, to_currency)
    return {
        "from": from_currency,
        "to": to_currency,
        "rate": float(rate),
    }


@router.get("/portfolio", response_model=PortfolioResponse)
def get_my_portfolio(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """
    Get complete portfolio for authenticated user.
    
    Aggregates all stock and crypto accounts with:
    - Total invested amount
    - Total fees
    - Current value
    - Profit/Loss
    - Performance percentage
    """
    user_bidx = hash_index(current_user.uuid, master_key)
    
    stock_models = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    crypto_models = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    accounts = []
    
    for acc in stock_models:
        summary = get_stock_account_summary(session, acc, master_key)
        accounts.append(summary)
    
    # Convert crypto accounts from USD to EUR for portfolio aggregation
    usd_eur_rate = get_exchange_rate("USD", "EUR")
    for acc in crypto_models:
        summary = get_crypto_account_summary(session, acc, master_key)
        summary_eur = convert_account_to_eur(summary, usd_eur_rate)
        accounts.append(summary_eur)
    
    total_invested = sum(a.total_invested for a in accounts)
    total_fees = sum(a.total_fees for a in accounts)
    current_value = sum(a.current_value for a in accounts if a.current_value)
    
    profit_loss = None
    profit_loss_pct = None
    
    if current_value > 0 or total_invested > 0:
        profit_loss = current_value - total_invested
        if total_invested > 0:
            profit_loss_pct = (profit_loss / total_invested * 100)
    
    return PortfolioResponse(
        total_invested=round(total_invested, 2),
        total_fees=round(total_fees, 2),
        current_value=round(current_value, 2) if current_value else None,
        profit_loss=round(profit_loss, 2) if profit_loss else None,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None,
        accounts=accounts
    )