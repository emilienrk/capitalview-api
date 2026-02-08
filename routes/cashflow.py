"""Cashflow routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import User, Cashflow
from services.auth import get_current_user, get_master_key
from models.enums import FlowType
from dtos import (
    CashflowCreate,
    CashflowUpdate,
    CashflowResponse,
    CashflowSummaryResponse,
    CashflowBalanceResponse,
)
from services.cashflow import (
    create_cashflow,
    update_cashflow,
    delete_cashflow,
    get_cashflow,
    get_all_user_cashflows,
    get_cashflows_by_type,
    get_user_cashflow_balance
)

router = APIRouter(prefix="/cashflow", tags=["Cashflows"])


@router.post("", response_model=CashflowResponse, status_code=201)
def create_entry(
    cashflow_data: CashflowCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new cashflow entry."""
    return create_cashflow(session, cashflow_data, current_user.uuid, master_key)


@router.get("", response_model=list[CashflowResponse])
def get_all(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all cashflow entries for current user."""
    return get_all_user_cashflows(session, current_user.uuid, master_key)


@router.get("/me/inflows", response_model=CashflowSummaryResponse)
def get_inflows(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all income/inflows for current authenticated user, grouped by category."""
    return get_cashflows_by_type(session, current_user.uuid, master_key, FlowType.INFLOW)


@router.get("/me/outflows", response_model=CashflowSummaryResponse)
def get_outflows(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all expenses/outflows for current authenticated user, grouped by category."""
    return get_cashflows_by_type(session, current_user.uuid, master_key, FlowType.OUTFLOW)


@router.get("/me/balance", response_model=CashflowBalanceResponse)
def get_balance(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get complete cashflow balance for current authenticated user."""
    return get_user_cashflow_balance(session, current_user.uuid, master_key)


@router.get("/{cashflow_id}", response_model=CashflowResponse)
def get_entry(
    cashflow_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a specific cashflow entry."""
    cashflow = get_cashflow(session, cashflow_id, current_user.uuid, master_key)
    if not cashflow:
        raise HTTPException(status_code=404, detail="Cashflow not found")
    return cashflow


@router.put("/{cashflow_id}", response_model=CashflowResponse)
def update_entry(
    cashflow_id: str,
    cashflow_data: CashflowUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Update a cashflow entry."""
    # Verify ownership
    existing = get_cashflow(session, cashflow_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Cashflow not found")
    
    cashflow_model = session.get(Cashflow, cashflow_id)
    return update_cashflow(session, cashflow_model, cashflow_data, master_key)


@router.delete("/{cashflow_id}", status_code=204)
def delete_entry(
    cashflow_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a cashflow entry."""
    # Verify ownership
    existing = get_cashflow(session, cashflow_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Cashflow not found")
        
    delete_cashflow(session, cashflow_id)
    return None
