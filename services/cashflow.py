"""Cashflow service."""

from decimal import Decimal
from collections import defaultdict

from sqlmodel import Session, select

from models import Cashflow
from models.enums import FlowType, Frequency
from dtos import (
    CashflowResponse,
    CashflowCategoryResponse,
    CashflowSummaryResponse,
    CashflowBalanceResponse,
)


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


def get_cashflow_response(cashflow: Cashflow) -> CashflowResponse:
    """Convert a Cashflow to a response."""
    return CashflowResponse(
        id=cashflow.id,
        name=cashflow.name,
        flow_type=cashflow.flow_type.value,
        category=cashflow.category,
        amount=cashflow.amount,
        frequency=cashflow.frequency.value,
        transaction_date=cashflow.transaction_date,
        monthly_amount=get_monthly_amount(cashflow.amount, cashflow.frequency),
    )


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


def get_cashflows_by_type(
    session: Session, 
    user_id: int, 
    flow_type: FlowType
) -> CashflowSummaryResponse:
    """Get all cashflows of a specific type for a user."""
    cashflows = session.exec(
        select(Cashflow)
        .where(Cashflow.user_id == user_id)
        .where(Cashflow.flow_type == flow_type)
    ).all()
    
    cashflow_responses = [get_cashflow_response(cf) for cf in cashflows]
    categories = aggregate_by_category(cashflow_responses)
    
    total_amount = sum(cf.amount for cf in cashflow_responses)
    monthly_total = sum(cf.monthly_amount for cf in cashflow_responses)
    
    return CashflowSummaryResponse(
        flow_type=flow_type.value,
        total_amount=total_amount,
        monthly_total=monthly_total,
        categories=categories,
    )


def get_user_inflows(session: Session, user_id: int) -> CashflowSummaryResponse:
    """Get all income for a user."""
    return get_cashflows_by_type(session, user_id, FlowType.INFLOW)


def get_user_outflows(session: Session, user_id: int) -> CashflowSummaryResponse:
    """Get all expenses for a user."""
    return get_cashflows_by_type(session, user_id, FlowType.OUTFLOW)


def get_user_cashflow_balance(session: Session, user_id: int) -> CashflowBalanceResponse:
    """Get the complete cashflow balance for a user."""
    inflows = get_user_inflows(session, user_id)
    outflows = get_user_outflows(session, user_id)
    
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
