"""Bank account routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import User
from services.auth import get_current_user, get_master_key
from dtos import (
    BankAccountCreate,
    BankAccountUpdate,
    BankAccountResponse,
    BankSummaryResponse,
)
from services.bank import (
    create_bank_account,
    get_bank_account,
    get_user_bank_accounts,
    update_bank_account,
    delete_bank_account as service_delete_bank_account
)

router = APIRouter(prefix="/bank", tags=["Bank Accounts"])


@router.post("/accounts", response_model=BankAccountResponse, status_code=201)
def create_account(
    account_data: BankAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new bank account."""
    # Check for duplicates in memory
    user_accounts = get_user_bank_accounts(session, current_user.uuid, master_key)
    
    unique_types = {
        "LIVRET_A", "LIVRET_DEVE", "LEP", "LDD", "PEL", "CEL"
    }
    
    if account_data.account_type.value in unique_types:
        for acc in user_accounts.accounts:
            if acc.account_type.value == account_data.account_type.value:
                raise HTTPException(
                    status_code=400,
                    detail=f"You already have a {account_data.account_type.value} account."
                )

    return create_bank_account(session, account_data, current_user.uuid, master_key)


@router.get("/accounts", response_model=BankSummaryResponse)
def get_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all bank accounts with total balance for current user."""
    return get_user_bank_accounts(session, current_user.uuid, master_key)


@router.get("/accounts/{account_id}", response_model=BankAccountResponse)
def get_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get a specific bank account."""
    account = get_bank_account(session, account_id, current_user.uuid, master_key)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.put("/accounts/{account_id}", response_model=BankAccountResponse)
def update_account(
    account_id: str,
    account_data: BankAccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Update a bank account."""
    from models import BankAccount
    account = session.get(BankAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Verify ownership
    existing = get_bank_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=403, detail="Access denied")

    return update_bank_account(session, account, account_data, master_key)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a bank account."""
    # Verify ownership first
    existing = get_bank_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
    
    from models import BankAccount
    account_to_delete = session.get(BankAccount, account_id)
    session.delete(account_to_delete)
    session.commit()
    
    return None
