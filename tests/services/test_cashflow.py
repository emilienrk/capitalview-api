import pytest
from decimal import Decimal
from datetime import date, datetime
from sqlmodel import Session

from services.cashflow import (
    create_cashflow,
    get_cashflow,
    update_cashflow,
    delete_cashflow,
    get_all_user_cashflows,
    get_user_inflows,
    get_user_outflows,
    get_user_cashflow_balance,
    get_monthly_amount
)
from dtos.cashflow import CashflowCreate, CashflowUpdate
from models.enums import FlowType, Frequency, BankAccountType
from models.cashflow import Cashflow
from models.bank import BankAccount
from services.bank import create_bank_account
from dtos.bank import BankAccountCreate
from services.encryption import hash_index, decrypt_data, encrypt_data


def test_get_monthly_amount():
    assert get_monthly_amount(Decimal("100"), Frequency.MONTHLY) == Decimal("100")
    assert get_monthly_amount(Decimal("100"), Frequency.YEARLY) == Decimal("100") / Decimal("12")
    assert get_monthly_amount(Decimal("100"), Frequency.WEEKLY) == Decimal("100") * Decimal("4.33")
    assert get_monthly_amount(Decimal("100"), Frequency.DAILY) == Decimal("3000")
    assert get_monthly_amount(Decimal("100"), Frequency.ONCE) == Decimal("0")


def test_create_cashflow(session: Session, master_key: str):
    data = CashflowCreate(
        name="Salary",
        flow_type=FlowType.INFLOW,
        category="Work",
        amount=Decimal("5000"),
        frequency=Frequency.MONTHLY,
        transaction_date=date(2023, 1, 1)
    )
    user_uuid = "user_cf_1"
    cf = create_cashflow(session, data, user_uuid, master_key)
    assert cf.name == "Salary"
    assert cf.amount == Decimal("5000")
    assert cf.monthly_amount == Decimal("5000")
    assert cf.id is not None
    db_cf = session.get(Cashflow, cf.id)
    assert db_cf is not None
    assert db_cf.user_uuid_bidx == hash_index(user_uuid, master_key)
    assert decrypt_data(db_cf.name_enc, master_key) == "Salary"


def test_get_cashflow(session: Session, master_key: str):
    user_uuid = "user_cf_2"
    data = CashflowCreate(
        name="Rent",
        flow_type=FlowType.OUTFLOW,
        category="Housing",
        amount=Decimal("1500"),
        frequency=Frequency.MONTHLY,
        transaction_date=date(2023, 1, 1)
    )
    created = create_cashflow(session, data, user_uuid, master_key)
    fetched = get_cashflow(session, created.id, user_uuid, master_key)
    assert fetched.name == "Rent"
    assert get_cashflow(session, "non_existent", user_uuid, master_key) is None
    assert get_cashflow(session, created.id, "other_user", master_key) is None


def test_update_cashflow(session: Session, master_key: str):
    user_uuid = "user_cf_3"
    data = CashflowCreate(
        name="Gym",
        flow_type=FlowType.OUTFLOW,
        category="Health",
        amount=Decimal("50"),
        frequency=Frequency.MONTHLY,
        transaction_date=date(2023, 1, 1)
    )
    cf = create_cashflow(session, data, user_uuid, master_key)
    db_cf = session.get(Cashflow, cf.id)
    update_data = CashflowUpdate(name="Gym & Spa", amount=Decimal("80"), frequency=Frequency.YEARLY)
    updated = update_cashflow(session, db_cf, update_data, master_key, user_uuid)
    assert updated.name == "Gym & Spa"
    assert updated.amount == Decimal("80")
    assert updated.frequency == Frequency.YEARLY.value
    assert abs(updated.monthly_amount - (Decimal("80") / Decimal("12"))) < Decimal("0.000000001")


def test_delete_cashflow(session: Session, master_key: str):
    user_uuid = "user_cf_4"
    data = CashflowCreate(
        name="Netflix",
        flow_type=FlowType.OUTFLOW,
        category="Subs",
        amount=Decimal("15"),
        frequency=Frequency.MONTHLY,
        transaction_date=date(2023, 1, 1)
    )
    cf = create_cashflow(session, data, user_uuid, master_key)
    assert delete_cashflow(session, cf.id) is True
    assert session.get(Cashflow, cf.id) is None
    assert delete_cashflow(session, "non_existent") is False


def test_aggregation_and_balance(session: Session, master_key: str):
    user_uuid = "user_balance"
    create_cashflow(session, CashflowCreate(name="Salary", flow_type=FlowType.INFLOW, category="Job", amount=Decimal("3000"), frequency=Frequency.MONTHLY, transaction_date=date.today()), user_uuid, master_key)
    create_cashflow(session, CashflowCreate(name="Freelance", flow_type=FlowType.INFLOW, category="Job", amount=Decimal("12000"), frequency=Frequency.YEARLY, transaction_date=date.today()), user_uuid, master_key)
    create_cashflow(session, CashflowCreate(name="Rent", flow_type=FlowType.OUTFLOW, category="Housing", amount=Decimal("1000"), frequency=Frequency.MONTHLY, transaction_date=date.today()), user_uuid, master_key)
    create_cashflow(session, CashflowCreate(name="Groceries", flow_type=FlowType.OUTFLOW, category="Food", amount=Decimal("500"), frequency=Frequency.MONTHLY, transaction_date=date.today()), user_uuid, master_key)
    create_cashflow(session, CashflowCreate(name="Secret", flow_type=FlowType.INFLOW, category="Spy", amount=Decimal("100000"), frequency=Frequency.MONTHLY, transaction_date=date.today()), "other_agent", master_key)
    inflows = get_user_inflows(session, user_uuid, master_key)
    assert inflows.flow_type == FlowType.INFLOW.value
    assert inflows.total_amount == Decimal("15000")
    assert inflows.monthly_total == Decimal("4000")
    assert len(inflows.categories) == 1
    assert inflows.categories[0].category == "Job"
    outflows = get_user_outflows(session, user_uuid, master_key)
    assert outflows.total_amount == Decimal("1500")
    assert outflows.monthly_total == Decimal("1500")
    assert len(outflows.categories) == 2
    balance = get_user_cashflow_balance(session, user_uuid, master_key)
    assert balance.total_inflows == Decimal("15000")
    assert balance.total_outflows == Decimal("1500")
    assert balance.net_balance == Decimal("13500")
    assert balance.monthly_balance == Decimal("2500")
    assert balance.savings_rate == (Decimal("2500") / Decimal("4000")) * 100


def test_get_all_user_cashflows(session: Session, master_key: str):
    user_uuid = "user_list_all"
    create_cashflow(session, CashflowCreate(name="A", flow_type=FlowType.INFLOW, category="C", amount=Decimal(1), frequency=Frequency.ONCE, transaction_date=date.today()), user_uuid, master_key)
    result = get_all_user_cashflows(session, user_uuid, master_key)
    assert len(result) == 1
    assert result[0].name == "A"


# ─── bank_account_id link (blind index) ──────────────────────


def test_create_cashflow_with_bank_account_id(session: Session, master_key: str):
    user_uuid = "user_cf_bank_link"
    # Create a real bank account first
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Main", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    cf = create_cashflow(
        session,
        CashflowCreate(
            name="Rent",
            flow_type=FlowType.OUTFLOW,
            category="housing",
            amount=Decimal("800"),
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 5),
            bank_account_id=acc.id,
        ),
        user_uuid,
        master_key,
    )
    assert cf.bank_account_id == acc.id
    # Verify blind index stored in DB (not the plain UUID)
    db_cf = session.get(Cashflow, cf.id)
    assert db_cf.bank_account_uuid_bidx is not None
    assert db_cf.bank_account_uuid_bidx != acc.id
    assert db_cf.bank_account_uuid_bidx == hash_index(acc.id, master_key)


def test_create_cashflow_without_bank_account_id(session: Session, master_key: str):
    user_uuid = "user_cf_no_bank"
    cf = create_cashflow(
        session,
        CashflowCreate(
            name="Income",
            flow_type=FlowType.INFLOW,
            category="work",
            amount=Decimal("3000"),
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 28),
            bank_account_id=None,
        ),
        user_uuid,
        master_key,
    )
    assert cf.bank_account_id is None
    db_cf = session.get(Cashflow, cf.id)
    assert db_cf.bank_account_uuid_bidx is None


def test_get_cashflow_resolves_bank_account_id(session: Session, master_key: str):
    user_uuid = "user_cf_resolve"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Savings", balance=Decimal("0"), account_type=BankAccountType.SAVINGS),
        user_uuid,
        master_key,
    )
    cf = create_cashflow(
        session,
        CashflowCreate(
            name="Transfer",
            flow_type=FlowType.OUTFLOW,
            category="transfer",
            amount=Decimal("200"),
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 1),
            bank_account_id=acc.id,
        ),
        user_uuid,
        master_key,
    )
    fetched = get_cashflow(session, cf.id, user_uuid, master_key)
    assert fetched.bank_account_id == acc.id


def test_update_cashflow_links_bank_account(session: Session, master_key: str):
    user_uuid = "user_cf_update_link"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Checking", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    cf = create_cashflow(
        session,
        CashflowCreate(
            name="Netflix",
            flow_type=FlowType.OUTFLOW,
            category="leisure",
            amount=Decimal("15"),
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 12),
        ),
        user_uuid,
        master_key,
    )
    assert cf.bank_account_id is None

    db_cf = session.get(Cashflow, cf.id)
    updated = update_cashflow(
        session, db_cf,
        CashflowUpdate(bank_account_id=acc.id),
        master_key,
        user_uuid,
    )
    assert updated.bank_account_id == acc.id


def test_update_cashflow_unlinks_bank_account(session: Session, master_key: str):
    user_uuid = "user_cf_unlink"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Checking", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    cf = create_cashflow(
        session,
        CashflowCreate(
            name="Spotify",
            flow_type=FlowType.OUTFLOW,
            category="music",
            amount=Decimal("10"),
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 20),
            bank_account_id=acc.id,
        ),
        user_uuid,
        master_key,
    )
    assert cf.bank_account_id == acc.id

    db_cf = session.get(Cashflow, cf.id)
    updated = update_cashflow(
        session, db_cf,
        CashflowUpdate(bank_account_id=""),  # Empty string = unlink
        master_key,
        user_uuid,
    )
    assert updated.bank_account_id is None


def test_bank_account_id_not_exposed_across_users(session: Session, master_key: str):
    """A cashflow linked to user_A should not expose a bank account UUID to user_B."""
    user_a = "user_isolation_a"
    user_b = "user_isolation_b"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Private", balance=Decimal("0"), account_type=BankAccountType.CHECKING),
        user_a,
        master_key,
    )
    cf = create_cashflow(
        session,
        CashflowCreate(
            name="Private CF",
            flow_type=FlowType.OUTFLOW,
            category="private",
            amount=Decimal("100"),
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 1),
            bank_account_id=acc.id,
        ),
        user_a,
        master_key,
    )
    # user_B cannot access user_A's cashflow at all
    assert get_cashflow(session, cf.id, user_b, master_key) is None
