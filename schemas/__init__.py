"""Schemas for API responses."""

from decimal import Decimal
from datetime import datetime, date
from typing import Optional

from pydantic import BaseModel


# ============== BANK SCHEMAS ==============

class BankAccountCreate(BaseModel):
    """Create a bank account."""
    user_id: int
    name: str
    account_type: str
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None
    balance: Decimal = Decimal("0")


class BankAccountUpdate(BaseModel):
    """Update a bank account."""
    name: Optional[str] = None
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None
    balance: Optional[Decimal] = None


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

class CashflowCreate(BaseModel):
    """Create a cashflow."""
    user_id: int
    name: str
    flow_type: str
    category: str
    amount: Decimal
    frequency: str
    transaction_date: date


class CashflowUpdate(BaseModel):
    """Update a cashflow."""
    name: Optional[str] = None
    category: Optional[str] = None
    amount: Optional[Decimal] = None
    frequency: Optional[str] = None
    transaction_date: Optional[date] = None


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
    total_amount: Decimal
    average_buy_price: Decimal
    total_invested: Decimal
    total_fees: Decimal
    fees_percentage: Decimal
    
    current_price: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[Decimal] = None


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


# ============== STOCK ACCOUNT CRUD SCHEMAS ==============

class StockAccountCreate(BaseModel):
    """Create a stock account."""
    user_id: int
    name: str
    account_type: str  # PEA, CTO, PEA_PME
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None


class StockAccountUpdate(BaseModel):
    """Update a stock account."""
    name: Optional[str] = None
    bank_name: Optional[str] = None
    encrypted_iban: Optional[str] = None


class StockAccountBasicResponse(BaseModel):
    """Basic stock account response (without positions)."""
    id: int
    user_id: int
    name: str
    account_type: str
    bank_name: Optional[str] = None
    created_at: datetime


# ============== STOCK TRANSACTION CRUD SCHEMAS ==============

class StockTransactionCreate(BaseModel):
    """Create a stock transaction."""
    account_id: int
    ticker: str
    exchange: Optional[str] = None
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal = Decimal("0")
    executed_at: datetime


class StockTransactionUpdate(BaseModel):
    """Update a stock transaction."""
    ticker: Optional[str] = None
    exchange: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[Decimal] = None
    price_per_unit: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    executed_at: Optional[datetime] = None


class StockTransactionBasicResponse(BaseModel):
    """Basic stock transaction response."""
    id: int
    account_id: int
    ticker: str
    exchange: Optional[str] = None
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    executed_at: datetime


# ============== CRYPTO ACCOUNT CRUD SCHEMAS ==============

class CryptoAccountCreate(BaseModel):
    """Create a crypto account."""
    user_id: int
    name: str
    wallet_name: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountUpdate(BaseModel):
    """Update a crypto account."""
    name: Optional[str] = None
    wallet_name: Optional[str] = None
    public_address: Optional[str] = None


class CryptoAccountBasicResponse(BaseModel):
    """Basic crypto account response (without positions)."""
    id: int
    user_id: int
    name: str
    wallet_name: Optional[str] = None
    public_address: Optional[str] = None
    created_at: datetime


# ============== CRYPTO TRANSACTION CRUD SCHEMAS ==============

class CryptoTransactionCreate(BaseModel):
    """Create a crypto transaction."""
    account_id: int
    ticker: str
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal = Decimal("0")
    fees_ticker: Optional[str] = None
    executed_at: datetime


class CryptoTransactionUpdate(BaseModel):
    """Update a crypto transaction."""
    ticker: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[Decimal] = None
    price_per_unit: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    fees_ticker: Optional[str] = None
    executed_at: Optional[datetime] = None


class CryptoTransactionBasicResponse(BaseModel):
    """Basic crypto transaction response."""
    id: int
    account_id: int
    ticker: str
    type: str
    amount: Decimal
    price_per_unit: Decimal
    fees: Decimal
    fees_ticker: Optional[str] = None
    executed_at: datetime


# ============== NOTE CRUD SCHEMAS ==============

class NoteCreate(BaseModel):
    """Create a note."""
    user_id: int
    name: str
    description: Optional[str] = None


class NoteUpdate(BaseModel):
    """Update a note."""
    name: Optional[str] = None
    description: Optional[str] = None


class NoteResponse(BaseModel):
    """Note response."""
    model_config = {"from_attributes": True}
    
    id: int
    user_id: int
    name: str
    description: Optional[str] = None
