import pytest
from decimal import Decimal
from sqlmodel import Session

from services.bank import (
    create_bank_account,
    get_user_bank_accounts,
    get_bank_account,
    update_bank_account,
    delete_bank_account,
)
from dtos.bank import BankAccountCreate, BankAccountUpdate
from models.enums import BankAccountType
from models.bank import BankAccount
from services.encryption import hash_index


def test_create_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    data = BankAccountCreate(
        name="Main Checking",
        balance=Decimal("1500.50"),
        account_type=BankAccountType.CHECKING,
        institution_name="Big Bank",
        identifier="FR76"
    )
    resp = create_bank_account(session, data, user_uuid, master_key)
    assert resp.name == "Main Checking"
    assert resp.balance == Decimal("1500.50")
    assert resp.account_type == BankAccountType.CHECKING
    assert resp.institution_name == "Big Bank"
    assert resp.identifier == "FR76"
    db_acc = session.get(BankAccount, resp.id)
    assert db_acc is not None
    assert db_acc.user_uuid_bidx == hash_index(user_uuid, master_key)
    assert db_acc.balance_enc != "1500.50"


def test_get_user_bank_accounts(session: Session, master_key: str):
    user_uuid = "user_1"
    create_bank_account(session, BankAccountCreate(name="Acc 1", balance=Decimal("100"), account_type=BankAccountType.CHECKING), user_uuid, master_key)
    create_bank_account(session, BankAccountCreate(name="Acc 2", balance=Decimal("200"), account_type=BankAccountType.SAVINGS), user_uuid, master_key)
    summary = get_user_bank_accounts(session, user_uuid, master_key)
    assert len(summary.accounts) == 2
    assert summary.total_balance == Decimal("300")


def test_get_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_bank_account(session, BankAccountCreate(name="My Acc", balance=Decimal("0"), account_type=BankAccountType.CHECKING), user_uuid, master_key)
    fetched = get_bank_account(session, created.id, user_uuid, master_key)
    assert fetched.name == "My Acc"
    assert get_bank_account(session, created.id, "user_2", master_key) is None
    assert get_bank_account(session, "non_existent", user_uuid, master_key) is None


def test_update_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_bank_account(session, BankAccountCreate(name="Old Name", balance=Decimal("100"), account_type=BankAccountType.CHECKING), user_uuid, master_key)
    db_acc = session.get(BankAccount, created.id)
    updated = update_bank_account(session, db_acc, BankAccountUpdate(name="New Name", balance=Decimal("500"), institution_name="New Inst", identifier="New ID"), master_key)
    assert updated.name == "New Name"
    assert updated.balance == Decimal("500")
    assert updated.institution_name == "New Inst"
    assert updated.identifier == "New ID"


def test_delete_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_bank_account(session, BankAccountCreate(name="Del", balance=Decimal("0"), account_type=BankAccountType.CHECKING), user_uuid, master_key)
    assert delete_bank_account(session, created.id) is True
    assert session.get(BankAccount, created.id) is None
    assert delete_bank_account(session, "non_existent") is False
