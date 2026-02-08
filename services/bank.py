"""Bank account service."""

from decimal import Decimal
from typing import Optional

from sqlmodel import Session, select

from models import BankAccount, BankAccountType
from dtos import BankAccountCreate, BankAccountUpdate, BankAccountResponse, BankSummaryResponse
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_to_response(account: BankAccount, master_key: str) -> BankAccountResponse:
    """Decrypt and map a BankAccount to a response DTO."""
    name = decrypt_data(account.name_enc, master_key)
    balance_str = decrypt_data(account.balance_enc, master_key)
    type_str = decrypt_data(account.account_type_enc, master_key)
    
    inst_name = None
    if account.institution_name_enc:
        inst_name = decrypt_data(account.institution_name_enc, master_key)
        
    identifier = None
    if account.identifier_enc:
        identifier = decrypt_data(account.identifier_enc, master_key)

    return BankAccountResponse(
        id=account.uuid,
        name=name,
        balance=Decimal(balance_str),
        account_type=BankAccountType(type_str),
        institution_name=inst_name,
        identifier=identifier,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def create_bank_account(
    session: Session, 
    data: BankAccountCreate, 
    user_uuid: str, 
    master_key: str
) -> BankAccountResponse:
    """Create a new encrypted bank account."""
    user_bidx = hash_index(user_uuid, master_key)
    
    name_enc = encrypt_data(data.name, master_key)
    balance_enc = encrypt_data(str(data.balance), master_key)
    type_enc = encrypt_data(data.account_type.value, master_key)
    
    inst_enc = None
    if data.institution_name:
        inst_enc = encrypt_data(data.institution_name, master_key)
        
    ident_enc = None
    if data.identifier:
        ident_enc = encrypt_data(data.identifier, master_key)
        
    account = BankAccount(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        balance_enc=balance_enc,
        account_type_enc=type_enc,
        institution_name_enc=inst_enc,
        identifier_enc=ident_enc,
    )
    
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_to_response(account, master_key)


def update_bank_account(
    session: Session,
    account: BankAccount,
    data: BankAccountUpdate,
    master_key: str
) -> BankAccountResponse:
    """Update an existing bank account."""
    if data.name is not None:
        account.name_enc = encrypt_data(data.name, master_key)
        
    if data.balance is not None:
        account.balance_enc = encrypt_data(str(data.balance), master_key)
        
    if data.institution_name is not None:
        account.institution_name_enc = encrypt_data(data.institution_name, master_key)
        
    if data.identifier is not None:
        account.identifier_enc = encrypt_data(data.identifier, master_key)
        
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_to_response(account, master_key)


def delete_bank_account(
    session: Session,
    account_uuid: str
) -> bool:
    """Delete a bank account."""
    account = session.get(BankAccount, account_uuid)
    if not account:
        return False
        
    session.delete(account)
    session.commit()
    return True


def get_user_bank_accounts(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> BankSummaryResponse:
    """Get all bank accounts for a user."""
    user_bidx = hash_index(user_uuid, master_key)
    
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    responses = [_map_to_response(acc, master_key) for acc in accounts]
    total_balance = sum(acc.balance for acc in responses)
    
    return BankSummaryResponse(
        total_balance=total_balance,
        accounts=responses
    )


def get_bank_account(
    session: Session,
    account_uuid: str,
    user_uuid: str,
    master_key: str
) -> Optional[BankAccountResponse]:
    """Get a single bank account if it belongs to the user."""
    account = session.get(BankAccount, account_uuid)
    if not account:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if account.user_uuid_bidx != user_bidx:
        return None
        
    return _map_to_response(account, master_key)
