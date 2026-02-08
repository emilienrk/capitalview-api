"""Crypto account services."""

from typing import List, Optional

from sqlmodel import Session, select

from models import CryptoAccount, CryptoTransaction
from dtos import (
    CryptoAccountCreate, 
    CryptoAccountUpdate, 
    CryptoAccountBasicResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_account_to_response(account: CryptoAccount, master_key: str) -> CryptoAccountBasicResponse:
    """Decrypt and map CryptoAccount to basic response."""
    name = decrypt_data(account.name_enc, master_key)
    
    platform = None
    if account.platform_enc:
        platform = decrypt_data(account.platform_enc, master_key)
        
    address = None
    if account.public_address_enc:
        address = decrypt_data(account.public_address_enc, master_key)

    return CryptoAccountBasicResponse(
        id=account.id,
        name=name,
        platform=platform,
        public_address=address,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def create_crypto_account(
    session: Session, 
    data: CryptoAccountCreate, 
    user_uuid: str, 
    master_key: str
) -> CryptoAccountBasicResponse:
    """Create a new encrypted crypto account."""
    user_bidx = hash_index(user_uuid, master_key)
    
    name_enc = encrypt_data(data.name, master_key)
    
    platform_enc = None
    if data.platform:
        platform_enc = encrypt_data(data.platform, master_key)
        
    address_enc = None
    if data.public_address:
        address_enc = encrypt_data(data.public_address, master_key)
        
    account = CryptoAccount(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        platform_enc=platform_enc,
        public_address_enc=address_enc,
    )
    
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_account_to_response(account, master_key)


def get_user_crypto_accounts(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> List[CryptoAccountBasicResponse]:
    """List all crypto accounts for a user."""
    user_bidx = hash_index(user_uuid, master_key)
    
    accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    return [_map_account_to_response(acc, master_key) for acc in accounts]


def get_crypto_account(
    session: Session,
    account_id: int,
    user_uuid: str,
    master_key: str
) -> Optional[CryptoAccountBasicResponse]:
    """Get a single crypto account if it belongs to the user."""
    account = session.get(CryptoAccount, account_id)
    if not account:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if account.user_uuid_bidx != user_bidx:
        return None
        
    return _map_account_to_response(account, master_key)


def update_crypto_account(
    session: Session,
    account: CryptoAccount,
    data: CryptoAccountUpdate,
    master_key: str
) -> CryptoAccountBasicResponse:
    """Update an existing crypto account."""
    if data.name is not None:
        account.name_enc = encrypt_data(data.name, master_key)
        
    if data.platform is not None:
        account.platform_enc = encrypt_data(data.platform, master_key)
        
    if data.public_address is not None:
        account.public_address_enc = encrypt_data(data.public_address, master_key)
        
    session.add(account)
    session.commit()
    session.refresh(account)
    
    return _map_account_to_response(account, master_key)


def delete_crypto_account(
    session: Session,
    account_id: int,
    master_key: str
) -> bool:
    """
    Delete a crypto account and all its transactions.
    
    Args:
        session: Database session
        account_id: Account ID to delete
        master_key: Used to calculate blind index for deleting transactions
    
    Returns:
        True if deleted, False if not found
    """
    account = session.get(CryptoAccount, account_id)
    if not account:
        return False

    # Manual cascade delete for transactions
    account_bidx = hash_index(str(account_id), master_key)
    
    transactions = session.exec(
        select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == account_bidx)
    ).all()
    
    for tx in transactions:
        session.delete(tx)
        
    session.delete(account)
    session.commit()
    return True
