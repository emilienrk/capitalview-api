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
    BankHistoryImportRequest,
)
from services.bank import (
    create_bank_account,
    get_bank_account,
    get_user_bank_accounts,
    update_bank_account,
    delete_bank_account,
    get_bank_account_history,
    get_all_bank_accounts_history,
    delete_bank_account_history,
    import_bank_account_history,
)
from dtos.transaction import AccountHistorySnapshotResponse

router = APIRouter(prefix="/bank", tags=["Bank Accounts"])


@router.post("/accounts", response_model=BankAccountResponse, status_code=201)
def create_account(
    account_data: BankAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Create a new bank account."""
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


@router.get("/history", response_model=list[AccountHistorySnapshotResponse])
def get_all_history(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get aggregated historical snapshots across all bank accounts."""
    return get_all_bank_accounts_history(session, current_user.uuid, master_key)


@router.delete("/accounts/{account_id}/history", status_code=204)
def delete_account_history(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Delete all historical snapshots for a bank account."""
    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    delete_bank_account_history(session, account_id, master_key)


@router.post("/accounts/{account_id}/history/import", status_code=200)
def import_account_history(
    account_id: str,
    payload: BankHistoryImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
) -> dict:
    """Import historical balance snapshots for a bank account.

    Entries: a list of {snapshot_date, value} pairs.
    When overwrite=True, all existing history is deleted before import.
    When overwrite=False (default), existing rows are preserved.
    """
    from models import BankAccount as BankAccountModel

    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")

    account = session.get(BankAccountModel, account_id)
    count = import_bank_account_history(
        session, account, payload.entries, master_key, overwrite=payload.overwrite
    )
    return {"inserted": count}


@router.get("/accounts/{account_id}/history", response_model=list[AccountHistorySnapshotResponse])
def get_account_history(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get historical daily snapshots for a bank account."""
    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    return get_bank_account_history(session, account_id, master_key)


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
    existing = get_bank_account(session, account_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return delete_bank_account(session, account_id, master_key)
