"""Bank account service."""

from decimal import Decimal

from sqlmodel import Session, select

from models import BankAccount
from dtos import BankAccountResponse, BankSummaryResponse


def get_bank_account_response(account: BankAccount) -> BankAccountResponse:
    """Convert a BankAccount to a response."""
    return BankAccountResponse(
        id=account.id,
        name=account.name,
        bank_name=account.bank_name,
        balance=account.balance,
        account_type=account.account_type.value,
        updated_at=account.updated_at,
    )


def get_user_bank_accounts(session: Session, user_id: int) -> BankSummaryResponse:
    """Get all bank accounts for a user with total balance."""
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_hash == hash_index(id))
    ).all()
    
    account_responses = [get_bank_account_response(acc) for acc in accounts]
    total_balance = sum(acc.balance for acc in account_responses)
    
    return BankSummaryResponse(
        total_balance=total_balance,
        accounts=account_responses,
    )


def get_all_bank_accounts(session: Session) -> BankSummaryResponse:
    """Get all bank accounts with total balance."""
    accounts = session.exec(select(BankAccount)).all()
    
    account_responses = [get_bank_account_response(acc) for acc in accounts]
    total_balance = sum(acc.balance for acc in account_responses)
    
    return BankSummaryResponse(
        total_balance=total_balance,
        accounts=account_responses,
    )
