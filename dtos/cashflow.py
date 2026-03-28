"""Cashflow schemas."""

from datetime import date, datetime
from decimal import Decimal

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
    bank_account_id: str | None = None


class CashflowUpdate(BaseModel):
    """Update a cashflow."""
    name: str | None = None
    flow_type: FlowType | None = None
    category: str | None = None
    amount: Decimal | None = None
    frequency: Frequency | None = None
    transaction_date: date | None = None
    bank_account_id: str | None = None


class CashflowResponse(BaseModel):
    """Single cashflow response."""
    id: str
    name: str
    flow_type: FlowType
    category: str
    amount: Decimal
    frequency: Frequency
    transaction_date: date
    created_at: datetime
    updated_at: datetime

    monthly_amount: Decimal  # Amount normalized to monthly
    bank_account_id: str | None = None  # Linked bank account UUID


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
    savings_rate: Decimal | None = None  # (inflows - outflows) / inflows * 100
    inflows: CashflowSummaryResponse
    outflows: CashflowSummaryResponse
