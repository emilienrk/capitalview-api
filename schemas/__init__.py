"""Schemas for API responses."""

from decimal import Decimal
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


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
