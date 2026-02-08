"""Stock account services."""

from typing import List, Optional

from sqlmodel import Session, select

from models import StockAccount, StockAccountType, StockTransaction
from dtos import (
    StockAccountCreate, 
    StockAccountUpdate, 
    StockAccountBasicResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_account_to_response(account: StockAccount, master_key: str) -> StockAccountBasicResponse:
    """Decrypt and map StockAccount to basic response."""
    name = decrypt_data(account.name_enc, master_key)
    type_str = decrypt_data(account.account_type_enc, master_key)
    
    inst_name = None
    if account.institution_name_enc:
        inst_name = decrypt_data(account.institution_name_enc, master_key)
        
    ident = None
    if account.identifier_enc:
        ident = decrypt_data(account.identifier_enc, master_key)

    return StockAccountBasicResponse(
        id=account.id,
        name=name,
        account_type=StockAccountType(type_str),
        institution_name=inst_name,
        identifier=ident,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def create_stock_account(
    session: Session, 
    data: StockAccountCreate, 
    user_uuid: str, 
    master_key: str
) -> StockAccountBasicResponse:
    """Create a new encrypted stock account."""
    user_bidx = hash_index(user_uuid, master_key)
    
    name_enc = encrypt_data(data.name, master_key)
    type_enc = encrypt_data(data.account_type.value, master_key)
    
    inst_enc = None
    if data.institution_name:
        inst_enc = encrypt_data(data.institution_name, master_key)
        
    ident_enc = None
    if data.identifier:
        ident_enc = encrypt_data(data.identifier, master_key)
        
    account = StockAccount(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        account_type_enc=type_enc,
        institution_name_enc=inst_enc,
        identifier_enc=ident_enc,
    )
    
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_account_to_response(account, master_key)


def get_user_stock_accounts(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> List[StockAccountBasicResponse]:
    """List all stock accounts for a user."""
    user_bidx = hash_index(user_uuid, master_key)
    
    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    return [_map_account_to_response(acc, master_key) for acc in accounts]


def get_stock_account(
    session: Session,
    account_id: int,
    user_uuid: str,
    master_key: str
) -> Optional[StockAccountBasicResponse]:
    """Get a single stock account if it belongs to the user."""
    account = session.get(StockAccount, account_id)
    if not account:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if account.user_uuid_bidx != user_bidx:
        return None
        
    return _map_account_to_response(account, master_key)


def update_stock_account(
    session: Session,
    account: StockAccount,
    data: StockAccountUpdate,
    master_key: str
) -> StockAccountBasicResponse:
    """Update an existing stock account."""
    if data.name is not None:
        account.name_enc = encrypt_data(data.name, master_key)
        
    if data.institution_name is not None:
        account.institution_name_enc = encrypt_data(data.institution_name, master_key)
        
    if data.identifier is not None:
        account.identifier_enc = encrypt_data(data.identifier, master_key)
        
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_account_to_response(account, master_key)


def delete_stock_account(
    session: Session,
    account_id: int,
    master_key: str
) -> bool:
    """
    Delete a stock account and all its transactions.
    
    Args:
        session: Database session
        account_id: Account ID to delete
        master_key: Used to calculate blind index for deleting transactions
    
    Returns:
        True if deleted, False if not found (though usually caller checks existence)
    """
    account = session.get(StockAccount, account_id)
    if not account:
        return False

    # Manual cascade delete for transactions (Overkill privacy mode: no FK)
    # We must find transactions by account_id_bidx
    account_bidx = hash_index(str(account_id), master_key)
    
    transactions = session.exec(
        select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
    ).all()
    
    for tx in transactions:
        session.delete(tx)
        
    session.delete(account)
    session.commit()
    return True
