"""Cashflow service."""

import calendar
from decimal import Decimal
from datetime import date, timedelta
from collections import defaultdict

from typing import NamedTuple

from sqlmodel import Session, select

from models import Cashflow
from models.currency import BASE_CURRENCY
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
from services.market import get_exchange_rate, has_exchange_rate


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


class LinkedAccount(NamedTuple):
    """What a cashflow needs to know about the account it is attached to."""
    uuid: str
    currency: str


def _map_cashflow_to_response(
    cashflow: Cashflow,
    master_key: str,
    bank_bidx_map: dict[str, LinkedAccount] | None = None,
) -> CashflowResponse:
    """Decrypt and map Cashflow to response DTO.

    bank_bidx_map: optional dict of {bank_account_uuid_bidx -> LinkedAccount},
    used to resolve the linked bank account from its blind index.
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

    # A flow is denominated by the account it hits: the bank posts what it
    # actually moved, in its own currency, and that figure is the one applied to
    # the balance. Unattached, there is no such account — euros, like every
    # other aggregate. See docs/currencies.md.
    linked = (
        bank_bidx_map.get(cashflow.bank_account_uuid_bidx)
        if cashflow.bank_account_uuid_bidx and bank_bidx_map
        else None
    )
    bank_account_id = linked.uuid if linked else None
    currency = linked.currency if linked else BASE_CURRENCY

    is_active = True
    if cashflow.is_active_enc:
        is_active = decrypt_data(cashflow.is_active_enc, master_key) == "true"

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
        currency=currency,
        is_active=is_active,
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
    is_active_enc = encrypt_data("true" if data.is_active else "false", master_key)

    cashflow = Cashflow(
        user_uuid_bidx=user_bidx,
        name_enc=name_enc,
        flow_type_enc=flow_type_enc,
        category_enc=category_enc,
        amount_enc=amount_enc,
        frequency_enc=frequency_enc,
        transaction_date_enc=date_enc,
        bank_account_uuid_bidx=bank_acc_bidx,
        is_active_enc=is_active_enc,
    )
    
    session.add(cashflow)
    session.commit()
    session.refresh(cashflow)
    
    bank_bidx_map = build_bank_bidx_map(session, user_uuid, master_key)
    response = _map_cashflow_to_response(cashflow, master_key, bank_bidx_map)
    return _fill_euro_amounts(session, [response])[0]


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

    if data.is_active is not None:
        cashflow.is_active_enc = encrypt_data("true" if data.is_active else "false", master_key)

    session.add(cashflow)
    session.commit()
    session.refresh(cashflow)
    
    bank_bidx_map = build_bank_bidx_map(session, user_uuid, master_key)
    response = _map_cashflow_to_response(cashflow, master_key, bank_bidx_map)
    return _fill_euro_amounts(session, [response])[0]


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

    bank_bidx_map = build_bank_bidx_map(session, user_uuid, master_key)
    response = _map_cashflow_to_response(cashflow, master_key, bank_bidx_map)
    return _fill_euro_amounts(session, [response])[0]


def _euro_rates(session: Session, cashflows: list[CashflowResponse]) -> dict[str, Decimal]:
    """The euro rate of each currency in play; absent when none is published.

    Totals cross accounts, so they cross currencies, and adding the raw figures
    would total francs with euros.

    A currency stays out of the map rather than converting at 1:
    `get_exchange_rate` answers 1 both for a rate that genuinely is 1 and for a
    currency it knows nothing about, so a total built on it would be wrong with
    nothing marking it as wrong. `services.bank._total_in_base_currency` makes
    the same call for the same reason.

    Today's rate, not each flow's own date: these are standing declarations,
    not dated movements — "what does my month look like" is a question about
    now.
    """
    rates: dict[str, Decimal] = {}
    for cf in cashflows:
        if cf.currency in rates:
            continue
        if has_exchange_rate(session, cf.currency):
            rates[cf.currency] = get_exchange_rate(session, cf.currency, BASE_CURRENCY)
    return rates


def _fill_euro_amounts(
    session: Session, cashflows: list[CashflowResponse]
) -> list[CashflowResponse]:
    """Set each flow's euro equivalent, so callers can add them up themselves.

    The web app aggregates client-side — a Sankey diagram needs a value per
    flow, not just the totals — and it holds no exchange rates. Without this it
    would sum francs with euros, exactly what the totals below refuse to do.
    """
    rates = _euro_rates(session, cashflows)
    for cf in cashflows:
        rate = rates.get(cf.currency)
        cf.monthly_amount_eur = cf.monthly_amount * rate if rate is not None else None
    return cashflows


def _total_in_euros(
    cashflows: list[CashflowResponse],
    rates: dict[str, Decimal],
    field: str,
) -> Decimal | None:
    """Sum one amount field across currencies, in euros.

    One unconvertible flow drops the whole total: a sum over the rest would
    read as the total it is not.
    """
    total = Decimal("0")
    for cf in cashflows:
        rate = rates.get(cf.currency)
        if rate is None:
            return None
        total += getattr(cf, field) * rate
    return total


def _difference(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    """Subtract two totals, unless either is unavailable."""
    if left is None or right is None:
        return None
    return left - right


def aggregate_by_category(
    cashflows: list[CashflowResponse],
    rates: dict[str, Decimal],
) -> list[CashflowCategoryResponse]:
    """Group cashflows by category, with each category totalled in euros."""
    categories: dict[str, list[CashflowResponse]] = defaultdict(list)
    
    for cf in cashflows:
        categories[cf.category].append(cf)
    
    result = []
    for category, items in sorted(categories.items()):
        result.append(CashflowCategoryResponse(
            category=category,
            total_amount=_total_in_euros(items, rates, "amount"),
            monthly_total=_total_in_euros(items, rates, "monthly_amount"),
            count=len(items),
            items=items,
        ))
    
    return result


def build_bank_bidx_map(
    session: Session, user_uuid: str, master_key: str
) -> dict[str, LinkedAccount]:
    """Build a map of {bank_account_uuid_bidx -> LinkedAccount} for a user."""
    # Lazy, like the mirror import in services.bank: the two modules need each
    # other and neither can be the one loaded second.
    from models import BankAccount
    from services.bank import account_currency

    user_bidx = hash_index(user_uuid, master_key)
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()
    return {
        hash_index(acc.uuid, master_key): LinkedAccount(
            acc.uuid, account_currency(acc, master_key)
        )
        for acc in accounts
    }


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
    bank_bidx_map = build_bank_bidx_map(session, user_uuid, master_key)
    responses = [_map_cashflow_to_response(cf, master_key, bank_bidx_map) for cf in cashflows]
    return _fill_euro_amounts(session, responses)


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

    rates = _euro_rates(session, filtered)

    return CashflowSummaryResponse(
        flow_type=flow_type.value,
        total_amount=_total_in_euros(filtered, rates, "amount"),
        monthly_total=_total_in_euros(filtered, rates, "monthly_amount"),
        categories=aggregate_by_category(filtered, rates),
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

    # One unconvertible side takes the balance with it: a net worked out from
    # only half the flows would read as a real figure.
    net_balance = _difference(inflows.total_amount, outflows.total_amount)
    monthly_balance = _difference(inflows.monthly_total, outflows.monthly_total)

    # Calculate savings rate
    savings_rate = None
    if monthly_balance is not None and inflows.monthly_total:
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