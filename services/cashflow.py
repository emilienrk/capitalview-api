"""Cashflow service."""

import calendar
from decimal import Decimal
from datetime import date, timedelta
from collections import defaultdict

from sqlmodel import Session, select

from models import Cashflow
from models.enums import FlowType, Frequency
from dtos import (
    CashflowCreate,
    CashflowUpdate,
    CashflowResponse,
    CashflowCategoryResponse,
    CashflowSummaryResponse,
    CashflowBalanceResponse,
)
from services.encryption import encrypt_data, decrypt_data, hash_index


def get_monthly_amount(amount: Decimal, frequency: Frequency) -> Decimal:
    """Convert amount to monthly equivalent based on frequency."""
    multipliers = {
        Frequency.ONCE: Decimal("0"),
        Frequency.DAILY: Decimal("30"),
        Frequency.WEEKLY: Decimal("4.33"),  # 52 weeks / 12 months
        Frequency.MONTHLY: Decimal("1"),
        Frequency.YEARLY: Decimal("1") / Decimal("12"),
    }
    return amount * multipliers.get(frequency, Decimal("1"))


def _map_cashflow_to_response(
    cashflow: Cashflow,
    master_key: str,
    bank_bidx_map: dict | None = None,
) -> CashflowResponse:
    """Decrypt and map Cashflow to response DTO.

    bank_bidx_map: optional dict of {bank_account_uuid_bidx -> bank_account_uuid},
    used to resolve the linked bank account UUID from its blind index.
    """
    name = decrypt_data(cashflow.name_enc, master_key)
    flow_type_str = decrypt_data(cashflow.flow_type_enc, master_key)
    category = decrypt_data(cashflow.category_enc, master_key)
    amount_str = decrypt_data(cashflow.amount_enc, master_key)
    frequency_str = decrypt_data(cashflow.frequency_enc, master_key)
    date_str = decrypt_data(cashflow.transaction_date_enc, master_key)
    
    amount = Decimal(amount_str)
    frequency = Frequency(frequency_str)
    flow_type = FlowType(flow_type_str)
    transaction_date = date.fromisoformat(date_str)

    bank_account_id = None
    if cashflow.bank_account_uuid_bidx and bank_bidx_map:
        bank_account_id = bank_bidx_map.get(cashflow.bank_account_uuid_bidx)

    return CashflowResponse(
        id=cashflow.uuid,
        name=name,
        flow_type=flow_type.value,
        category=category,
        amount=amount,
        frequency=frequency.value,
        transaction_date=transaction_date,
        monthly_amount=get_monthly_amount(amount, frequency),
        bank_account_id=bank_account_id,
        created_at=cashflow.created_at,
        updated_at=cashflow.updated_at,
    )


def create_cashflow(
    session: Session, 
    data: CashflowCreate, 
    user_uuid: str, 
    master_key: str
) -> CashflowResponse:
    """Create a new encrypted cashflow."""
    user_bidx = hash_index(user_uuid, master_key)
    
    name_enc = encrypt_data(data.name, master_key)
    flow_type_enc = encrypt_data(data.flow_type.value, master_key)
    category_enc = encrypt_data(data.category, master_key)
    amount_enc = encrypt_data(str(data.amount), master_key)
    frequency_enc = encrypt_data(data.frequency.value, master_key)
    date_enc = encrypt_data(data.transaction_date.isoformat(), master_key)
    bank_acc_bidx = hash_index(data.bank_account_id, master_key) if data.bank_account_id else None
    
    cashflow = Cashflow(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        flow_type_enc=flow_type_enc,
        category_enc=category_enc,
        amount_enc=amount_enc,
        frequency_enc=frequency_enc,
        transaction_date_enc=date_enc,
        bank_account_uuid_bidx=bank_acc_bidx,
    )
    
    session.add(cashflow)
    session.commit()
    session.refresh(cashflow)
    
    bank_bidx_map = _build_bank_bidx_map(session, user_uuid, master_key)
    return _map_cashflow_to_response(cashflow, master_key, bank_bidx_map)


def update_cashflow(
    session: Session,
    cashflow: Cashflow,
    data: CashflowUpdate,
    master_key: str,
    user_uuid: str,
) -> CashflowResponse:
    """Update an existing cashflow."""
    if data.name is not None:
        cashflow.name_enc = encrypt_data(data.name, master_key)
        
    if data.flow_type is not None:
        cashflow.flow_type_enc = encrypt_data(data.flow_type.value, master_key)
        
    if data.category is not None:
        cashflow.category_enc = encrypt_data(data.category, master_key)
        
    if data.amount is not None:
        cashflow.amount_enc = encrypt_data(str(data.amount), master_key)
        
    if data.frequency is not None:
        cashflow.frequency_enc = encrypt_data(data.frequency.value, master_key)
        
    if data.transaction_date is not None:
        cashflow.transaction_date_enc = encrypt_data(data.transaction_date.isoformat(), master_key)

    if data.bank_account_id is not None:
        # Empty string means unlinking the account
        cashflow.bank_account_uuid_bidx = hash_index(data.bank_account_id, master_key) if data.bank_account_id else None
        
    session.add(cashflow)
    session.commit()
    session.refresh(cashflow)
    
    bank_bidx_map = _build_bank_bidx_map(session, user_uuid, master_key)
    return _map_cashflow_to_response(cashflow, master_key, bank_bidx_map)


def delete_cashflow(
    session: Session,
    cashflow_uuid: str
) -> bool:
    """Delete a cashflow."""
    cashflow = session.get(Cashflow, cashflow_uuid)
    if not cashflow:
        return False
        
    session.delete(cashflow)
    session.commit()
    return True


def get_cashflow(
    session: Session,
    cashflow_uuid: str,
    user_uuid: str,
    master_key: str
) -> CashflowResponse | None:
    """Get a single cashflow."""
    cashflow = session.get(Cashflow, cashflow_uuid)
    if not cashflow:
        return None
        
    user_bidx = hash_index(user_uuid, master_key)
    if cashflow.user_uuid_bidx != user_bidx:
        return None

    bank_bidx_map = _build_bank_bidx_map(session, user_uuid, master_key)
    return _map_cashflow_to_response(cashflow, master_key, bank_bidx_map)


def aggregate_by_category(cashflows: list[CashflowResponse]) -> list[CashflowCategoryResponse]:
    """Group cashflows by category."""
    categories: dict[str, list[CashflowResponse]] = defaultdict(list)
    
    for cf in cashflows:
        categories[cf.category].append(cf)
    
    result = []
    for category, items in sorted(categories.items()):
        total_amount = sum(item.amount for item in items)
        monthly_total = sum(item.monthly_amount for item in items)
        result.append(CashflowCategoryResponse(
            category=category,
            total_amount=total_amount,
            monthly_total=monthly_total,
            count=len(items),
            items=items,
        ))
    
    return result


def _build_bank_bidx_map(session: Session, user_uuid: str, master_key: str) -> dict:
    """Build a map of {bank_account_uuid_bidx -> bank_account.uuid} for a user."""
    from models import BankAccount
    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()
    return {hash_index(acc.uuid, master_key): acc.uuid for acc in accounts}


def get_all_user_cashflows(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> list[CashflowResponse]:
    """Get all cashflows for a user, decrypted."""
    user_bidx = hash_index(user_uuid, master_key)
    cashflows = session.exec(
        select(Cashflow).where(Cashflow.user_uuid_bidx == user_bidx)
    ).all()
    bank_bidx_map = _build_bank_bidx_map(session, user_uuid, master_key)
    return [_map_cashflow_to_response(cf, master_key, bank_bidx_map) for cf in cashflows]


def get_cashflows_by_type(
    session: Session, 
    user_uuid: str, 
    master_key: str,
    flow_type: FlowType
) -> CashflowSummaryResponse:
    """Get all cashflows of a specific type for a user."""
    # Since we can't filter by encrypted type in DB easily without blind index for type,
    # we fetch all and filter in memory.
    all_cashflows = get_all_user_cashflows(session, user_uuid, master_key)
    
    filtered = [cf for cf in all_cashflows if cf.flow_type == flow_type.value]
    
    categories = aggregate_by_category(filtered)
    
    total_amount = sum(cf.amount for cf in filtered)
    monthly_total = sum(cf.monthly_amount for cf in filtered)
    
    return CashflowSummaryResponse(
        flow_type=flow_type.value,
        total_amount=total_amount,
        monthly_total=monthly_total,
        categories=categories,
    )


def get_user_inflows(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> CashflowSummaryResponse:
    """Get all income for a user."""
    return get_cashflows_by_type(session, user_uuid, master_key, FlowType.INFLOW)


def get_user_outflows(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> CashflowSummaryResponse:
    """Get all expenses for a user."""
    return get_cashflows_by_type(session, user_uuid, master_key, FlowType.OUTFLOW)


def get_user_cashflow_balance(
    session: Session, 
    user_uuid: str, 
    master_key: str
) -> CashflowBalanceResponse:
    """Get the complete cashflow balance for a user."""
    inflows = get_cashflows_by_type(session, user_uuid, master_key, FlowType.INFLOW)
    outflows = get_cashflows_by_type(session, user_uuid, master_key, FlowType.OUTFLOW)
    
    net_balance = inflows.total_amount - outflows.total_amount
    monthly_balance = inflows.monthly_total - outflows.monthly_total

    
    # Calculate savings rate
    savings_rate = None
    if inflows.monthly_total > 0:
        savings_rate = (monthly_balance / inflows.monthly_total) * Decimal("100")
    
    return CashflowBalanceResponse(
        total_inflows=inflows.total_amount,
        monthly_inflows=inflows.monthly_total,
        total_outflows=outflows.total_amount,
        monthly_outflows=outflows.monthly_total,
        net_balance=net_balance,
        monthly_balance=monthly_balance,
        savings_rate=savings_rate,
        inflows=inflows,
        outflows=outflows,
    )


def get_cashflow_occurrences(
    cf: CashflowResponse,
    from_date: date,
    to_date: date,
) -> list[date]:
    """Return all firing dates of a cashflow in the half-open interval (from_date, to_date].

    from_date is exclusive (last processed date).
    to_date is inclusive (today).
    """
    if from_date >= to_date:
        return []

    reference = cf.transaction_date
    frequency = Frequency(cf.frequency)
    occurrences: list[date] = []

    if frequency == Frequency.ONCE:
        if from_date < reference <= to_date:
            occurrences.append(reference)
        return occurrences

    current = reference

    if frequency == Frequency.DAILY:
        if current <= from_date:
            # Jump forward to the first day strictly after from_date
            delta = (from_date - current).days + 1
            current = current + timedelta(days=delta)
        while current <= to_date:
            occurrences.append(current)
            current += timedelta(days=1)

    elif frequency == Frequency.WEEKLY:
        if current <= from_date:
            delta = (from_date - current).days
            weeks = delta // 7 + 1
            current = current + timedelta(weeks=weeks)
        while current <= to_date:
            occurrences.append(current)
            current += timedelta(weeks=1)

    elif frequency == Frequency.MONTHLY:
        anchor_day = reference.day
        # Advance month by month until current is strictly after from_date
        while current <= from_date:
            next_month = current.month % 12 + 1
            next_year = current.year + (1 if current.month == 12 else 0)
            last_day = calendar.monthrange(next_year, next_month)[1]
            current = date(next_year, next_month, min(anchor_day, last_day))
        while current <= to_date:
            occurrences.append(current)
            next_month = current.month % 12 + 1
            next_year = current.year + (1 if current.month == 12 else 0)
            last_day = calendar.monthrange(next_year, next_month)[1]
            current = date(next_year, next_month, min(anchor_day, last_day))

    elif frequency == Frequency.YEARLY:
        anchor_month = reference.month
        anchor_day = reference.day
        while current <= from_date:
            try:
                current = date(current.year + 1, anchor_month, anchor_day)
            except ValueError:
                # Feb 29 on a non-leap year → Feb 28
                current = date(current.year + 1, anchor_month, 28)
        while current <= to_date:
            occurrences.append(current)
            try:
                current = date(current.year + 1, anchor_month, anchor_day)
            except ValueError:
                current = date(current.year + 1, anchor_month, 28)

    return occurrences