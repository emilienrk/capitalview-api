"""Bank account routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import BankAccount
from schemas import BankAccountResponse, BankSummaryResponse
from services.bank import (
    get_bank_account_response,
    get_user_bank_accounts,
    get_all_bank_accounts,
)

router = APIRouter(prefix="/bank", tags=["Bank Accounts"])


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


@router.get("/user/{user_id}", response_model=BankSummaryResponse)
def get_user_banks(user_id: int, session: Session = Depends(get_session)):
    """Get all bank accounts for a specific user."""
    return get_user_bank_accounts(session, user_id)
