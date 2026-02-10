import pytest
from decimal import Decimal
from sqlmodel import Session

from services.stock_account import (
    create_stock_account,
    get_user_stock_accounts,
    get_stock_account,
    update_stock_account,
    delete_stock_account,
)
from dtos.stock import StockAccountCreate, StockAccountUpdate
from models.enums import StockAccountType
from models.stock import StockAccount
from services.encryption import hash_index


def test_create_stock_account(session: Session, master_key: str):
    data = StockAccountCreate(
        name="My PEA",
        account_type=StockAccountType.PEA,
        institution_name="Bank of France",
        identifier="FR123456"
    )
    user_uuid = "user_1"
    resp = create_stock_account(session, data, user_uuid, master_key)
    assert resp.name == "My PEA"
    assert resp.account_type == StockAccountType.PEA
    assert resp.institution_name == "Bank of France"
    assert resp.identifier == "FR123456"
    acc_db = session.get(StockAccount, resp.id)
    assert acc_db is not None
    assert acc_db.name_enc != "My PEA"
    assert acc_db.user_uuid_bidx == hash_index(user_uuid, master_key)


def test_get_user_stock_accounts(session: Session, master_key: str):
    user_1 = "user_1"
    user_2 = "user_2"
    create_stock_account(session, StockAccountCreate(name="U1 A1", account_type=StockAccountType.PEA), user_1, master_key)
    create_stock_account(session, StockAccountCreate(name="U1 A2", account_type=StockAccountType.CTO), user_1, master_key)
    create_stock_account(session, StockAccountCreate(name="U2 A1", account_type=StockAccountType.PEA), user_2, master_key)
    accounts_u1 = get_user_stock_accounts(session, user_1, master_key)
    assert len(accounts_u1) == 2
    names_u1 = {a.name for a in accounts_u1}
    assert names_u1 == {"U1 A1", "U1 A2"}
    accounts_u2 = get_user_stock_accounts(session, user_2, master_key)
    assert len(accounts_u2) == 1
    assert accounts_u2[0].name == "U2 A1"


def test_get_stock_account(session: Session, master_key: str):
    user_uuid = "user_1"
    data = StockAccountCreate(name="My Account", account_type=StockAccountType.PEA)
    created = create_stock_account(session, data, user_uuid, master_key)
    fetched = get_stock_account(session, created.id, user_uuid, master_key)
    assert fetched is not None
    assert fetched.id == created.id
    fetched_wrong = get_stock_account(session, created.id, "user_2", master_key)
    assert fetched_wrong is None
    assert get_stock_account(session, "non_existent", user_uuid, master_key) is None


def test_update_stock_account(session: Session, master_key: str):
    user_uuid = "user_1"
    data = StockAccountCreate(name="Old Name", account_type=StockAccountType.PEA)
    created = create_stock_account(session, data, user_uuid, master_key)
    acc_db = session.get(StockAccount, created.id)
    update_data = StockAccountUpdate(
        name="New Name",
        institution_name="New Bank",
        identifier="New ID"
    )
    updated = update_stock_account(session, acc_db, update_data, master_key)
    assert updated.name == "New Name"
    assert updated.institution_name == "New Bank"
    assert updated.identifier == "New ID"
    session.refresh(acc_db)
    fetched = get_stock_account(session, created.id, user_uuid, master_key)
    assert fetched.name == "New Name"
    assert fetched.identifier == "New ID"


def test_delete_stock_account(session: Session, master_key: str):
    user_uuid = "user_1"
    data = StockAccountCreate(name="To Delete", account_type=StockAccountType.PEA)
    created = create_stock_account(session, data, user_uuid, master_key)
    assert delete_stock_account(session, created.id, master_key) is True
    assert session.get(StockAccount, created.id) is None
    assert delete_stock_account(session, "non_existent", master_key) is False
    acc_tx = create_stock_account(session, StockAccountCreate(name="With Tx", account_type=StockAccountType.CTO), user_uuid, master_key)
    from services.stock_transaction import create_stock_transaction
    from dtos.stock import StockTransactionCreate
    from models.enums import StockTransactionType
    from datetime import datetime
    tx_data = StockTransactionCreate(
        account_id=acc_tx.id,
        symbol="TEST",
        type=StockTransactionType.BUY,
        amount=Decimal(1),
        price_per_unit=Decimal(1),
        executed_at=datetime.now()
    )
    tx = create_stock_transaction(session, tx_data, master_key)
    assert delete_stock_account(session, acc_tx.id, master_key) is True
    assert session.get(StockAccount, acc_tx.id) is None
    from models.stock import StockTransaction
    assert session.get(StockTransaction, tx.id) is None
