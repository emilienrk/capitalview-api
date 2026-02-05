"""Cashflow schemas."""

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from models.enums import FlowType, Frequency


class CashflowCreate(BaseModel):
    """Create a cashflow."""
    name: str
    flow_type: FlowType
    category: str
    amount: Decimal
    frequency: Frequency
    transaction_date: date


class CashflowUpdate(BaseModel):
    """Update a cashflow."""
    name: Optional[str] = None
    category: Optional[str] = None
    amount: Optional[Decimal] = None
    frequency: Optional[Frequency] = None
    transaction_date: Optional[date] = None


class CashflowResponse(BaseModel):
    """Single cashflow response."""
    id: int
    name: str
    flow_type: FlowType
    category: str
    amount: Decimal
    frequency: Frequency
    transaction_date: date

    monthly_amount: Decimal  # Amount normalized to monthly


class CashflowCategoryResponse(BaseModel):
    """Cashflows grouped by category."""
    category: str
    total_amount: Decimal
    monthly_total: Decimal
    count: int
    items: list[CashflowResponse]


class CashflowSummaryResponse(BaseModel):
    """Summary of cashflows (inflows or outflows)."""
    flow_type: FlowType
    total_amount: Decimal
    monthly_total: Decimal
    categories: list[CashflowCategoryResponse]


class CashflowBalanceResponse(BaseModel):
    """Balance between inflows and outflows."""
    total_inflows: Decimal
    monthly_inflows: Decimal
    total_outflows: Decimal
    monthly_outflows: Decimal
    net_balance: Decimal
    monthly_balance: Decimal
    savings_rate: Optional[Decimal] = None  # (inflows - outflows) / inflows * 100
    inflows: CashflowSummaryResponse
    outflows: CashflowSummaryResponse
