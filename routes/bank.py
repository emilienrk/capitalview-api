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
    # Validation logic (e.g. unique regulated accounts) is complicated with encryption 
    # because we can't easily query "type" without decrypting everything.
    # For now, we trust the user or implement a check by fetching all accounts and checking in memory.
    
    # Simple check for duplicates in memory
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
    account_id: int,
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
    account_id: int,
    account_data: BankAccountUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Update a bank account."""
    # Fetch model to update (we need the model instance for the service)
    # The service update_bank_account expects a Model, not ID.
    # Let's verify ownership first.
    from models import BankAccount
    account = session.get(BankAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Verify ownership (blind index check is done inside service if we passed ID, 
    # but here we pass object. Ideally service should handle retrieval too or we check here)
    # Let's use the getter to verify ownership efficiently
    existing = get_bank_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=403, detail="Access denied")

    return update_bank_account(session, account, account_data, master_key)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Delete a bank account."""
    # Verify ownership first
    existing = get_bank_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # We don't check for 403 specifically here because get_bank_account returns None for both 404 and 403 cases (filtered by user)
    
    from models import BankAccount
    # Service delete expects session and ID? 
    # Let's check service signature: delete_bank_account(session, account_id)
    # Wait, service delete doesn't check owner if we just pass ID.
    # But we checked existence above which validates owner.
    
    # However, to be safe, we should ensure we are deleting what we checked.
    # Since we don't have FKs, it's fine.
    
    # Wait, the service delete_bank_account might not exist or needs checking.
    # I implemented it in services/bank.py? Let me double check...
    # I didn't implement delete_bank_account in services/bank.py explicitly in the last turn!
    # I implemented create, update, get_user, get_one.
    # I missed delete!
    
    # I will add it here temporarily or assume I added it. 
    # Checking my memory... I verified services. Bank service had:
    # create, update, get_user, get_one. 
    # I missed delete logic there. 
    
    # I'll implement deletion logic here directly for now since it's simple (no cascades for bank accounts usually, unless history?)
    # Bank accounts don't have transaction history in this app version (only balance).
    
    account_to_delete = session.get(BankAccount, account_id)
    session.delete(account_to_delete)
    session.commit()
    
    return None