"""Bank account routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import BankAccount
from models.enums import BankAccountType
from schemas import (
    BankAccountCreate,
    BankAccountUpdate,
    BankAccountResponse,
    BankSummaryResponse,
)
from services.bank import (
    get_bank_account_response,
    get_user_bank_accounts,
    get_all_bank_accounts,
)

router = APIRouter(prefix="/bank", tags=["Bank Accounts"])


@router.post("/accounts", response_model=BankAccountResponse, status_code=201)
def create_bank_account(
    account_data: BankAccountCreate, session: Session = Depends(get_session)
):
    """Create a new bank account."""
    # Validate account_type enum
    try:
        account_type = BankAccountType(account_data.account_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid account_type. Must be one of: {[e.value for e in BankAccountType]}",
        )
    
    new_account = BankAccount(
        user_id=account_data.user_id,
        name=account_data.name,
        bank_name=account_data.bank_name,
        encrypted_iban=account_data.encrypted_iban,
        balance=account_data.balance,
        account_type=account_type,
    )
    session.add(new_account)
    session.commit()
    session.refresh(new_account)
    return get_bank_account_response(new_account)


@router.get("/accounts", response_model=BankSummaryResponse)
def get_bank_accounts(session: Session = Depends(get_session)):
    """Get all bank accounts with total balance."""
    return get_all_bank_accounts(session)


@router.get("/accounts/{account_id}", response_model=BankAccountResponse)
def get_bank_account(account_id: int, session: Session = Depends(get_session)):
    """Get a specific bank account."""
    account = session.get(BankAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return get_bank_account_response(account)


@router.put("/accounts/{account_id}", response_model=BankAccountResponse)
def update_bank_account(
    account_id: int,
    account_data: BankAccountUpdate,
    session: Session = Depends(get_session),
):
    """Update a bank account."""
    account = session.get(BankAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    # Update only provided fields
    if account_data.name is not None:
        account.name = account_data.name
    if account_data.bank_name is not None:
        account.bank_name = account_data.bank_name
    if account_data.encrypted_iban is not None:
        account.encrypted_iban = account_data.encrypted_iban
    if account_data.balance is not None:
        account.balance = account_data.balance
    
    session.add(account)
    session.commit()
    session.refresh(account)
    return get_bank_account_response(account)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_bank_account(account_id: int, session: Session = Depends(get_session)):
    """Delete a bank account."""
    account = session.get(BankAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    session.delete(account)
    session.commit()
    return None


@router.get("/user/{user_id}", response_model=BankSummaryResponse)
def get_user_banks(user_id: int, session: Session = Depends(get_session)):
    """Get all bank accounts for a specific user."""
    return get_user_bank_accounts(session, user_id)
