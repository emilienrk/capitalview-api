"""Cashflow routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import Cashflow
from schemas import (
    CashflowResponse,
    CashflowSummaryResponse,
    CashflowBalanceResponse,
)
from services.cashflow import (
    get_cashflow_response,
    get_user_inflows,
    get_user_outflows,
    get_user_cashflow_balance,
)

router = APIRouter(prefix="/cashflow", tags=["Cashflows"])


# ============== INFLOWS (Income) ==============

@router.get("/user/{user_id}/inflows", response_model=CashflowSummaryResponse)
def get_inflows(user_id: int, session: Session = Depends(get_session)):
    """Get all income/inflows for a user, grouped by category."""
    return get_user_inflows(session, user_id)


# ============== OUTFLOWS (Expenses) ==============

@router.get("/user/{user_id}/outflows", response_model=CashflowSummaryResponse)
def get_outflows(user_id: int, session: Session = Depends(get_session)):
    """Get all expenses/outflows for a user, grouped by category."""
    return get_user_outflows(session, user_id)


# ============== BALANCE (Total) ==============

@router.get("/user/{user_id}/balance", response_model=CashflowBalanceResponse)
def get_balance(user_id: int, session: Session = Depends(get_session)):
    """
    Get complete cashflow balance for a user.
    
    Returns:
        - Total inflows and outflows
        - Monthly equivalents
        - Net balance
        - Savings rate (% of income saved)
        - Breakdown by category
    """
    return get_user_cashflow_balance(session, user_id)


# ============== INDIVIDUAL CASHFLOW ==============

@router.get("/{cashflow_id}", response_model=CashflowResponse)
def get_cashflow(cashflow_id: int, session: Session = Depends(get_session)):
    """Get a specific cashflow entry."""
    cashflow = session.get(Cashflow, cashflow_id)
    if not cashflow:
        raise HTTPException(status_code=404, detail="Cashflow not found")
    return get_cashflow_response(cashflow)
