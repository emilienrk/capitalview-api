import pytest
from sqlmodel import Session, select
from datetime import datetime

from services.crypto_account import (
    create_crypto_account,
    get_user_crypto_accounts,
    get_crypto_account,
    update_crypto_account,
    delete_crypto_account,
)
from services.crypto_transaction import create_crypto_transaction
from dtos.crypto import CryptoAccountCreate, CryptoAccountUpdate, CryptoTransactionCreate
from models.crypto import CryptoAccount, CryptoTransaction
from models.enums import CryptoTransactionType
from services.encryption import hash_index, decrypt_data
from decimal import Decimal

def test_create_crypto_account(session: Session, master_key: str):
    data = CryptoAccountCreate(
        name="My Ledger",
        platform="Ledger Live",
        public_address="0x123abc"
    )
    user_uuid = "user_create_test"
    
    account = create_crypto_account(session, data, user_uuid, master_key)
    
    assert account.name == "My Ledger"
    assert account.platform == "Ledger Live"
    assert account.public_address == "0x123abc"
    assert account.id is not None
    assert isinstance(account.created_at, datetime)
    
    # Verify DB storage
    db_acc = session.get(CryptoAccount, account.id)
    assert db_acc is not None
    assert db_acc.user_uuid_bidx == hash_index(user_uuid, master_key)
    assert decrypt_data(db_acc.name_enc, master_key) == "My Ledger"


def test_create_crypto_account_minimal(session: Session, master_key: str):
    """Test creating account with optional fields missing."""
    data = CryptoAccountCreate(name="Simple Wallet")
    user_uuid = "user_minimal"
    
    account = create_crypto_account(session, data, user_uuid, master_key)
    
    assert account.name == "Simple Wallet"
    assert account.platform is None
    assert account.public_address is None


def test_get_user_crypto_accounts(session: Session, master_key: str):
    user_uuid = "user_list"
    other_user = "user_other"
    
    # Create 2 accounts for user_list
    create_crypto_account(session, CryptoAccountCreate(name="Acc 1"), user_uuid, master_key)
    create_crypto_account(session, CryptoAccountCreate(name="Acc 2"), user_uuid, master_key)
    
    # Create 1 account for other_user
    create_crypto_account(session, CryptoAccountCreate(name="Other Acc"), other_user, master_key)
    
    accounts = get_user_crypto_accounts(session, user_uuid, master_key)
    assert len(accounts) == 2
    assert {a.name for a in accounts} == {"Acc 1", "Acc 2"}


def test_get_crypto_account(session: Session, master_key: str):
    user_uuid = "user_get"
    acc = create_crypto_account(session, CryptoAccountCreate(name="Target"), user_uuid, master_key)
    
    # 1. Success
    fetched = get_crypto_account(session, acc.id, user_uuid, master_key)
    assert fetched is not None
    assert fetched.id == acc.id
    assert fetched.name == "Target"
    
    # 2. Not Found
    assert get_crypto_account(session, "non_existent", user_uuid, master_key) is None
    
    # 3. Wrong User (Access Denied logic simulation)
    assert get_crypto_account(session, acc.id, "other_user", master_key) is None


def test_update_crypto_account(session: Session, master_key: str):
    user_uuid = "user_update"
    acc_resp = create_crypto_account(session, CryptoAccountCreate(name="Old Name", platform="Old Plat"), user_uuid, master_key)
    
    # Get DB object to pass to update function (simulating route logic which fetches it first)
    # The service function signature is update_crypto_account(session, account: CryptoAccount, data: ..., master_key)
    acc_db = session.get(CryptoAccount, acc_resp.id)
    
    update_data = CryptoAccountUpdate(
        name="New Name",
        public_address="0xNewAddr"
    )
    
    updated = update_crypto_account(session, acc_db, update_data, master_key)
    
    assert updated.name == "New Name"
    assert updated.platform == "Old Plat" # Unchanged
    assert updated.public_address == "0xNewAddr" # Changed/Added
    
    # Verify DB
    session.refresh(acc_db)
    assert decrypt_data(acc_db.name_enc, master_key) == "New Name"


def test_delete_crypto_account_cascade(session: Session, master_key: str):
    """Test that deleting an account also deletes its transactions."""
    user_uuid = "user_delete"
    acc = create_crypto_account(session, CryptoAccountCreate(name="To Delete"), user_uuid, master_key)
    
    # Add a transaction
    tx_data = CryptoTransactionCreate(
        account_id=acc.id,
        ticker="BTC",
        type=CryptoTransactionType.BUY,
        amount=Decimal("1"),
        price_per_unit=Decimal("100"),
        fees=Decimal("1"),
        executed_at=datetime.now()
    )
    create_crypto_transaction(session, tx_data, master_key)
    
    # Verify transaction exists
    acc_bidx = hash_index(acc.id, master_key)
    txs = session.exec(select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == acc_bidx)).all()
    assert len(txs) == 1
    
    # Delete Account
    result = delete_crypto_account(session, acc.id, master_key)
    assert result is True
    
    # Verify Account Gone
    assert session.get(CryptoAccount, acc.id) is None
    
    # Verify Transaction Gone
    txs_after = session.exec(select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == acc_bidx)).all()
    assert len(txs_after) == 0

def test_delete_crypto_account_not_found(session: Session, master_key: str):
    assert delete_crypto_account(session, "non_existent", master_key) is False
