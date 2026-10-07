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
    BankEntryRequest,
    BankEntryResponse,
    SavingsInterestResponse,
)
from services.bank import (
    LedgerBalanceError,
    LinkedAccountFieldLockedError,
    UnconvertibleCurrencyError,
    create_bank_account,
    get_bank_account,
    get_user_bank_accounts,
    update_bank_account,
    delete_bank_account,
    get_bank_account_history,
    get_all_bank_accounts_history,
    delete_bank_account_history,
    import_bank_account_history,
    confirm_up_to_date,
)
from dtos.transaction import AccountHistorySnapshotResponse
from services.savings_interest import get_user_savings_interest

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

    try:
        return create_bank_account(session, account_data, current_user.uuid, master_key)
    except UnconvertibleCurrencyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/accounts", response_model=BankSummaryResponse)
def get_accounts(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """Get all bank accounts with total balance for current user."""
    return get_user_bank_accounts(session, current_user.uuid, master_key)


@router.get("/interest", response_model=list[SavingsInterestResponse])
def get_interest(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """This year's interest on every savings account that has a rate."""
    return get_user_savings_interest(session, current_user.uuid, master_key)


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
    On an account no bank feeds, each becomes an adjustment operation. On a
    linked one, overwrite=True deletes all existing history before import;
    overwrite=False (default) preserves existing rows.
    """
    from models import BankAccount as BankAccountModel

    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")

    from services.bank_ledger import import_balance_points, is_synced

    account = session.get(BankAccountModel, account_id)
    if not is_synced(session, account, master_key):
        # The operations are this account's truth: each balance becomes the
        # adjustment that agrees with it (docs/bank-ledger.md).
        return {"inserted": import_balance_points(session, account, payload.entries, master_key)}
    count = import_bank_account_history(
        session, account, payload.entries, master_key, overwrite=payload.overwrite
    )
    return {"inserted": count}


@router.post("/accounts/{account_id}/up-to-date", status_code=204)
def confirm_account_up_to_date(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
) -> None:
    """Vouch that nothing happened on the account since its last operation."""
    from models import BankAccount as BankAccountModel

    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    confirm_up_to_date(session, session.get(BankAccountModel, account_id))


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

    try:
        return update_bank_account(session, account, account_data, master_key)
    except UnconvertibleCurrencyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (LinkedAccountFieldLockedError, LedgerBalanceError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        # Interest terms the account cannot carry once merged with its own.
        raise HTTPException(status_code=400, detail=str(exc))


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


@router.post("/accounts/{account_id}/entries", response_model=BankEntryResponse)
def add_account_entry(
    account_id: str,
    entry: BankEntryRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    dry_run: bool = False,
):
    """An operation typed by hand, or a balance read on a statement, on an
    account no bank feeds (docs/bank-ledger.md)."""
    from models import BankAccount as BankAccountModel
    from services.bank_ledger import SyncedAccountError, add_entry

    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        return add_entry(session, session.get(BankAccountModel, account_id), entry, master_key, dry_run)
    except SyncedAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
) -> None:
    """Delete one operation, then rebuild the balance it was part of."""
    from sqlmodel import select

    from models import BankAccount as BankAccountModel
    from models.banking import BankTransaction
    from services.bank_ledger import OperationNotDeletableError, delete_operation
    from services.encryption import hash_index

    row = session.get(BankTransaction, transaction_id)
    account = None
    if row is not None:
        account = next(
            (
                acc for acc in session.exec(
                    select(BankAccountModel).where(
                        BankAccountModel.user_uuid_bidx == hash_index(current_user.uuid, master_key)
                    )
                ).all()
                if hash_index(acc.uuid, master_key) == row.account_id_bidx
            ),
            None,
        )
    if account is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    try:
        delete_operation(session, account, row, master_key)
    except OperationNotDeletableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
