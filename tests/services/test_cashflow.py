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
from models.enums import FlowType, Frequency
from models.cashflow import Cashflow
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
    
    # Check DB
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
    
    # 1. Success
    fetched = get_cashflow(session, created.id, user_uuid, master_key)
    assert fetched.name == "Rent"
    
    # 2. Not Found
    assert get_cashflow(session, "non_existent", user_uuid, master_key) is None
    
    # 3. Wrong User
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
    
    # Fetch DB object
    db_cf = session.get(Cashflow, cf.id)
    
    update_data = CashflowUpdate(
        name="Gym & Spa",
        amount=Decimal("80"),
        frequency=Frequency.YEARLY # Changed frequency
    )
    
    updated = update_cashflow(session, db_cf, update_data, master_key)
    
    assert updated.name == "Gym & Spa"
    assert updated.amount == Decimal("80")
    assert updated.frequency == Frequency.YEARLY.value
    # 80 / 12 = 6.6666...
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
    
    # 1. Salary (Inflow Monthly)
    create_cashflow(session, CashflowCreate(
        name="Salary", flow_type=FlowType.INFLOW, category="Job", amount=Decimal("3000"), frequency=Frequency.MONTHLY, transaction_date=date.today()
    ), user_uuid, master_key)
    
    # 2. Freelance (Inflow Yearly) -> 12000 / 12 = 1000/mo
    create_cashflow(session, CashflowCreate(
        name="Freelance", flow_type=FlowType.INFLOW, category="Job", amount=Decimal("12000"), frequency=Frequency.YEARLY, transaction_date=date.today()
    ), user_uuid, master_key)
    
    # 3. Rent (Outflow Monthly)
    create_cashflow(session, CashflowCreate(
        name="Rent", flow_type=FlowType.OUTFLOW, category="Housing", amount=Decimal("1000"), frequency=Frequency.MONTHLY, transaction_date=date.today()
    ), user_uuid, master_key)
    
    # 4. Food (Outflow Monthly)
    create_cashflow(session, CashflowCreate(
        name="Groceries", flow_type=FlowType.OUTFLOW, category="Food", amount=Decimal("500"), frequency=Frequency.MONTHLY, transaction_date=date.today()
    ), user_uuid, master_key)
    
    # 5. Other User Data (Should be ignored)
    create_cashflow(session, CashflowCreate(
        name="Secret", flow_type=FlowType.INFLOW, category="Spy", amount=Decimal("100000"), frequency=Frequency.MONTHLY, transaction_date=date.today()
    ), "other_agent", master_key)
    
    # Test Inflows
    inflows = get_user_inflows(session, user_uuid, master_key)
    assert inflows.flow_type == FlowType.INFLOW.value
    assert inflows.total_amount == Decimal("15000") # 3000 + 12000
    assert inflows.monthly_total == Decimal("4000") # 3000 + 1000
    assert len(inflows.categories) == 1
    assert inflows.categories[0].category == "Job"
    
    # Test Outflows
    outflows = get_user_outflows(session, user_uuid, master_key)
    assert outflows.total_amount == Decimal("1500") # 1000 + 500
    assert outflows.monthly_total == Decimal("1500")
    assert len(outflows.categories) == 2
    
    # Test Balance
    balance = get_user_cashflow_balance(session, user_uuid, master_key)
    assert balance.total_inflows == Decimal("15000")
    assert balance.total_outflows == Decimal("1500")
    assert balance.net_balance == Decimal("13500")
    assert balance.monthly_balance == Decimal("2500") # 4000 - 1500
    assert balance.savings_rate == (Decimal("2500") / Decimal("4000")) * 100


def test_get_all_user_cashflows(session: Session, master_key: str):
    user_uuid = "user_list_all"
    create_cashflow(session, CashflowCreate(
        name="A", flow_type=FlowType.INFLOW, category="C", amount=Decimal(1), frequency=Frequency.ONCE, transaction_date=date.today()
    ), user_uuid, master_key)
    
    result = get_all_user_cashflows(session, user_uuid, master_key)
    assert len(result) == 1
    assert result[0].name == "A"
