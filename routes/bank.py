"""Bank account routes."""

from datetime import date
from decimal import Decimal
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
    BankBalanceResponse,
    BankEntryResponse,
    SavingsInterestResponse,
)
from services.bank import (
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
    confirm_up_to_date,
)
from dtos.banking import BankEntryCreate, BankTransactionItem
from dtos.transaction import AccountHistorySnapshotResponse
from services.banking.transfer_decisions import TransactionNotFoundError
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
    except LinkedAccountFieldLockedError as exc:
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
    entry: BankEntryCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """An operation typed by hand on an account no bank feeds (docs/bank-ledger.md),
    filed as the user said (docs/bank-sorting.md, R56)."""
    from models import BankAccount as BankAccountModel
    from models.banking import BankTransaction
    from services.bank_ledger import SyncedAccountError, add_entry, delete_operation
    from services.banking.flows import file_entry
    from services.banking.transfer_decisions import DecisionError

    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    account = session.get(BankAccountModel, account_id)
    try:
        created = add_entry(session, account, entry, master_key)
    except SyncedAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        file_entry(session, current_user.uuid, master_key, account_id, created.id, entry)
    except (DecisionError, TransactionNotFoundError) as exc:
        # All or nothing: an entry the user filed is not left unfiled.
        delete_operation(session, account, session.get(BankTransaction, created.id), master_key)
        raise HTTPException(status_code=400, detail=str(exc) or "Cette opération ne peut pas être appariée.")
    return created


@router.get("/accounts/{account_id}/counterparts", response_model=list[BankTransactionItem])
def get_entry_counterparts(
    account_id: str,
    day: date,
    amount: Decimal,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    editing: str | None = None,
):
    """What an entry about to be typed here could pair with (docs/bank-sorting.md, R56);
    `editing`, the entry it replaces."""
    from services.banking.flows import list_entry_counterparts

    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    return list_entry_counterparts(session, current_user.uuid, master_key, account_id, day, amount, editing)


@router.get("/accounts/{account_id}/balance", response_model=BankBalanceResponse)
def get_account_balance(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    day: date | None = None,
):
    """What the operations of an account no bank feeds add up to, at the end of
    `day` (today by default) and today."""
    from models import BankAccount as BankAccountModel
    from services.bank_ledger import SyncedAccountError, balance_on

    if not get_bank_account(session, account_id, current_user.uuid, master_key):
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        return balance_on(session, session.get(BankAccountModel, account_id), day or date.today(), master_key)
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
