"""Cashflow routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import Cashflow, User
from services.auth import get_current_user
from models.enums import FlowType, Frequency
from schemas import (
    CashflowCreate,
    CashflowUpdate,
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


@router.post("", response_model=CashflowResponse, status_code=201)
def create_cashflow(
    cashflow_data: CashflowCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Create a new cashflow entry."""
    # Validate enums
    try:
        flow_type = FlowType(cashflow_data.flow_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid flow_type. Must be one of: {[e.value for e in FlowType]}",
        )
    
    try:
        frequency = Frequency(cashflow_data.frequency)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid frequency. Must be one of: {[e.value for e in Frequency]}",
        )
    
    new_cashflow = Cashflow(
        user_id=current_user.id,
        name=cashflow_data.name,
        flow_type=flow_type,
        category=cashflow_data.category,
        amount=cashflow_data.amount,
        frequency=frequency,
        transaction_date=cashflow_data.transaction_date,
    )
    session.add(new_cashflow)
    session.commit()
    session.refresh(new_cashflow)
    return get_cashflow_response(new_cashflow)


@router.get("", response_model=list[CashflowResponse])
def get_all_cashflows(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get all cashflow entries for current user."""
    cashflows = session.exec(
        select(Cashflow).where(Cashflow.user_id == current_user.id)
    ).all()
    return [get_cashflow_response(cf) for cf in cashflows]


# NOTE: /me/* routes must be defined BEFORE /{cashflow_id} to avoid FastAPI matching "me" as an integer
@router.get("/me/inflows", response_model=CashflowSummaryResponse)
def get_my_inflows(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get all income/inflows for current authenticated user, grouped by category."""
    return get_user_inflows(session, current_user.id)


@router.get("/me/outflows", response_model=CashflowSummaryResponse)
def get_my_outflows(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get all expenses/outflows for current authenticated user, grouped by category."""
    return get_user_outflows(session, current_user.id)


@router.get("/me/balance", response_model=CashflowBalanceResponse)
def get_my_balance(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """
    Get complete cashflow balance for current authenticated user.
    
    Returns:
        - Total inflows and outflows
        - Monthly equivalents
        - Net balance
        - Savings rate (% of income saved)
        - Breakdown by category
    """
    return get_user_cashflow_balance(session, current_user.id)


@router.get("/{cashflow_id}", response_model=CashflowResponse)
def get_cashflow(
    cashflow_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Get a specific cashflow entry."""
    cashflow = session.get(Cashflow, cashflow_id)
    if not cashflow:
        raise HTTPException(status_code=404, detail="Cashflow not found")
    
    if cashflow.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return get_cashflow_response(cashflow)


@router.put("/{cashflow_id}", response_model=CashflowResponse)
def update_cashflow(
    cashflow_id: int,
    cashflow_data: CashflowUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Update a cashflow entry."""
    cashflow = session.get(Cashflow, cashflow_id)
    if not cashflow:
        raise HTTPException(status_code=404, detail="Cashflow not found")
    
    if cashflow.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if cashflow_data.name is not None:
        cashflow.name = cashflow_data.name
    if cashflow_data.category is not None:
        cashflow.category = cashflow_data.category
    if cashflow_data.amount is not None:
        cashflow.amount = cashflow_data.amount
    if cashflow_data.frequency is not None:
        try:
            cashflow.frequency = Frequency(cashflow_data.frequency)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid frequency. Must be one of: {[e.value for e in Frequency]}",
            )
    if cashflow_data.transaction_date is not None:
        cashflow.transaction_date = cashflow_data.transaction_date
    
    session.add(cashflow)
    session.commit()
    session.refresh(cashflow)
    return get_cashflow_response(cashflow)


@router.delete("/{cashflow_id}", status_code=204)
def delete_cashflow(
    cashflow_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """Delete a cashflow entry."""
    cashflow = session.get(Cashflow, cashflow_id)
    if not cashflow:
        raise HTTPException(status_code=404, detail="Cashflow not found")
    
    if cashflow.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    session.delete(cashflow)
    session.commit()
    return None
