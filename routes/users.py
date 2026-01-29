"""User routes - global portfolio view."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import User
from schemas import PortfolioResponse
from services.stocks import get_user_stock_summary
from services.crypto import get_user_crypto_summary

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/{user_id}/portfolio", response_model=PortfolioResponse)
def get_user_portfolio(user_id: int, session: Session = Depends(get_session)):
    """
    Get complete portfolio for a user.
    
    Aggregates all stock and crypto accounts with:
    - Total invested amount
    - Total fees
    - Current value
    - Profit/Loss
    - Performance percentage
    """
    # Verify user exists
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get all account summaries
    stock_accounts = get_user_stock_summary(session, user_id)
    crypto_accounts = get_user_crypto_summary(session, user_id)
    
    accounts = stock_accounts + crypto_accounts
    
    # Aggregate totals
    total_invested = sum(a.total_invested for a in accounts)
    total_fees = sum(a.total_fees for a in accounts)
    current_value = sum(a.current_value for a in accounts if a.current_value)
    profit_loss = current_value - total_invested if current_value else None
    profit_loss_pct = (profit_loss / total_invested * 100) if profit_loss and total_invested > 0 else None
    
    return PortfolioResponse(
        total_invested=round(total_invested, 2),
        total_fees=round(total_fees, 2),
        current_value=round(current_value, 2) if current_value else None,
        profit_loss=round(profit_loss, 2) if profit_loss else None,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None,
        accounts=accounts
    )
