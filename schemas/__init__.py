"""Schemas for API responses."""

from decimal import Decimal
from datetime import datetime, date
from typing import Optional

from pydantic import BaseModel


# ============== BANK SCHEMAS ==============

class BankAccountResponse(BaseModel):
    """Bank account response."""
    id: int
    user_id: int
    name: str
    bank_name: Optional[str] = None
    balance: Decimal
    account_type: str
    updated_at: datetime


class BankSummaryResponse(BaseModel):
    """Summary of all bank accounts."""
    total_balance: Decimal
    accounts: list[BankAccountResponse]


# ============== CASHFLOW SCHEMAS ==============

class CashflowResponse(BaseModel):
    """Single cashflow response."""
    id: int
    user_id: int
    name: str
    flow_type: str
    category: str
    amount: Decimal
    frequency: str
    transaction_date: date
    
    # Calculated for monthly projection
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
    flow_type: str
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


# ============== TRANSACTION SCHEMAS ==============

class TransactionResponse(BaseModel):
    """Base transaction response with calculated fields."""
    id: int
    ticker: str
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: datetime
    
    # Calculated fields
    total_cost: Decimal  # amount * price_per_unit + fees
    fees_percentage: Decimal  # fees / total_cost * 100
    current_price: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None


class PositionResponse(BaseModel):
    """Aggregated position for a single asset."""
    ticker: str
    name: Optional[str] = None
    total_amount: Decimal  # Quantité totale
    average_buy_price: Decimal  # PRU (Prix de Revient Unitaire)
    total_invested: Decimal  # Montant total investi
    total_fees: Decimal  # Frais totaux
    fees_percentage: Decimal  # Frais en %
    
    # Current values (from MarketPrice)
    current_price: Optional[Decimal] = None  # Cours actuel
    current_value: Optional[Decimal] = None  # Valeur totale actuelle
    profit_loss: Optional[Decimal] = None  # Plus/moins value en €
    profit_loss_percentage: Optional[Decimal] = None  # Performance en %


class AccountSummaryResponse(BaseModel):
    """Summary of an account with all positions."""
    account_id: int
    account_name: str
    account_type: str
    total_invested: Decimal
    total_fees: Decimal
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None
    positions: list[PositionResponse]


class PortfolioResponse(BaseModel):
    """Global portfolio summary."""
    total_invested: Decimal
    total_fees: Decimal
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None
    accounts: list[AccountSummaryResponse]
