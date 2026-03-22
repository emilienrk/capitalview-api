import uuid as uuid_mod
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from models.account_history import AccountHistory
from models.bank import BankAccount
from models.enums import AccountCategory, BankAccountType
from dtos.bank import BankAccountCreate, BankAccountUpdate, BankHistoryEntry
from services.bank import (
    create_bank_account,
    delete_bank_account,
    delete_bank_account_history,
    get_bank_account,
    get_user_bank_accounts,
    import_bank_account_history,
    update_bank_account,
)
from services.encryption import decrypt_data, encrypt_data, hash_index


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_pg_insert(monkeypatch):
    """Replace pg_insert (PostgreSQL-specific) with a plain SA insert for SQLite tests."""
    import sqlalchemy as sa

    def _fake(table):
        class _Stmt:
            def values(self, rows):
                self._rows = rows
                return self

            def on_conflict_do_nothing(self, **kwargs):
                return sa.insert(table).values(self._rows)

        return _Stmt()

    monkeypatch.setattr("services.bank.pg_insert", _fake)


def _get_history_rows(session: Session, account_id_bidx: str) -> list[AccountHistory]:
    return session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .order_by(AccountHistory.snapshot_date)
    ).all()


def _value_on_date(rows: list[AccountHistory], d: date, master_key: str) -> Decimal:
    for row in rows:
        if row.snapshot_date == d:
            return Decimal(decrypt_data(row.total_value_enc, master_key))
    raise KeyError(f"No history row for {d}")


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


# ---------------------------------------------------------------------------
# History tests
# ---------------------------------------------------------------------------


def test_delete_bank_account_history(session: Session, master_key: str):
    """Deleting history removes all rows and returns the deleted count."""
    user_uuid = "user_del_hist"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Del Hist", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    account_id_bidx = hash_index(acc.id, master_key)
    user_bidx = hash_index(user_uuid, master_key)

    for d in [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)]:
        session.add(AccountHistory(
            uuid=str(uuid_mod.uuid4()),
            user_uuid_bidx=user_bidx,
            account_id_bidx=account_id_bidx,
            account_type=AccountCategory.BANK,
            snapshot_date=d,
            total_value_enc=encrypt_data("1000", master_key),
            total_invested_enc=encrypt_data("1000", master_key),
        ))
    session.commit()

    assert len(_get_history_rows(session, account_id_bidx)) == 3
    deleted = delete_bank_account_history(session, acc.id, master_key)
    assert deleted == 3
    assert len(_get_history_rows(session, account_id_bidx)) == 0


def test_import_bank_account_history_empty_returns_zero(session: Session, master_key: str, sqlite_pg_insert):
    """Importing an empty list does nothing and returns 0."""
    user_uuid = "user_import_empty"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Empty Import", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    db_acc = session.get(BankAccount, acc.id)
    assert import_bank_account_history(session, db_acc, [], master_key) == 0


def test_import_bank_account_history_fills_gaps(session: Session, master_key: str, sqlite_pg_insert):
    """Gaps between known entries are forward-filled with the last known value."""
    user_uuid = "user_import_gaps"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Gap Fill", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    db_acc = session.get(BankAccount, acc.id)
    account_id_bidx = hash_index(acc.id, master_key)

    entries = [
        BankHistoryEntry(snapshot_date=date(2025, 1, 1), value=Decimal("1000")),
        BankHistoryEntry(snapshot_date=date(2025, 1, 5), value=Decimal("2000")),
    ]
    count = import_bank_account_history(session, db_acc, entries, master_key)
    assert count > 0

    rows = _get_history_rows(session, account_id_bidx)
    # Jan 1 entry
    assert _value_on_date(rows, date(2025, 1, 1), master_key) == Decimal("1000")
    # Jan 2-4: forward-filled from Jan 1
    assert _value_on_date(rows, date(2025, 1, 2), master_key) == Decimal("1000")
    assert _value_on_date(rows, date(2025, 1, 4), master_key) == Decimal("1000")
    # Jan 5 entry
    assert _value_on_date(rows, date(2025, 1, 5), master_key) == Decimal("2000")
    # Jan 6+: forward-filled from Jan 5
    assert _value_on_date(rows, date(2025, 1, 6), master_key) == Decimal("2000")


def test_import_bank_account_history_zeros_before_first_entry(session: Session, master_key: str, sqlite_pg_insert):
    """Days between account creation and first entry are set to 0."""
    user_uuid = "user_import_zeros"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Zeros Before", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    # Backdate account creation so fill_start < first_entry_date
    db_acc = session.get(BankAccount, acc.id)
    db_acc.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    session.add(db_acc)
    session.commit()
    session.refresh(db_acc)

    account_id_bidx = hash_index(acc.id, master_key)
    entries = [BankHistoryEntry(snapshot_date=date(2025, 1, 10), value=Decimal("500"))]
    import_bank_account_history(session, db_acc, entries, master_key)

    rows = _get_history_rows(session, account_id_bidx)
    assert _value_on_date(rows, date(2025, 1, 1), master_key) == Decimal("0")
    assert _value_on_date(rows, date(2025, 1, 9), master_key) == Decimal("0")
    assert _value_on_date(rows, date(2025, 1, 10), master_key) == Decimal("500")
    assert _value_on_date(rows, date(2025, 1, 11), master_key) == Decimal("500")


def test_import_bank_account_history_entries_before_account_creation(session: Session, master_key: str, sqlite_pg_insert):
    """Entries dated before account creation extend fill_start backwards."""
    user_uuid = "user_import_before"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Before Creation", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    # Account created today; entry is from 2024 — fill_start must go back to 2024
    db_acc = session.get(BankAccount, acc.id)
    account_id_bidx = hash_index(acc.id, master_key)

    entries = [BankHistoryEntry(snapshot_date=date(2024, 6, 1), value=Decimal("3000"))]
    import_bank_account_history(session, db_acc, entries, master_key)

    rows = _get_history_rows(session, account_id_bidx)
    assert _value_on_date(rows, date(2024, 6, 1), master_key) == Decimal("3000")
    assert _value_on_date(rows, date(2024, 6, 2), master_key) == Decimal("3000")


def test_import_bank_account_history_overwrite(session: Session, master_key: str, sqlite_pg_insert):
    """overwrite=True deletes existing rows then inserts fresh ones."""
    user_uuid = "user_import_ow"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Overwrite", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    db_acc = session.get(BankAccount, acc.id)
    account_id_bidx = hash_index(acc.id, master_key)
    user_bidx = hash_index(user_uuid, master_key)

    # Insert stale rows that overwrite should clear
    for d in [date(2025, 3, 1), date(2025, 3, 2)]:
        session.add(AccountHistory(
            uuid=str(uuid_mod.uuid4()),
            user_uuid_bidx=user_bidx,
            account_id_bidx=account_id_bidx,
            account_type=AccountCategory.BANK,
            snapshot_date=d,
            total_value_enc=encrypt_data("9999", master_key),
            total_invested_enc=encrypt_data("9999", master_key),
        ))
    session.commit()

    entries = [BankHistoryEntry(snapshot_date=date(2025, 3, 1), value=Decimal("1234"))]
    import_bank_account_history(session, db_acc, entries, master_key, overwrite=True)

    rows = _get_history_rows(session, account_id_bidx)
    # Old value (9999) must be replaced with new (1234)
    assert _value_on_date(rows, date(2025, 3, 1), master_key) == Decimal("1234")
    assert _value_on_date(rows, date(2025, 3, 2), master_key) == Decimal("1234")
