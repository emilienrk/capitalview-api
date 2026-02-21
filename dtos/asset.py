"""Asset schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


# ============== ASSET CRUD ==============

class AssetCreate(BaseModel):
    """Create a personal asset."""
    name: str
    description: Optional[str] = None
    category: str
    purchase_price: Optional[Decimal] = None
    estimated_value: Decimal = Field(ge=0)
    currency: str = "EUR"
    acquisition_date: Optional[str] = None


class AssetUpdate(BaseModel):
    """Update a personal asset."""
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    purchase_price: Optional[Decimal] = None
    estimated_value: Optional[Decimal] = Field(None, ge=0)
    currency: Optional[str] = None
    acquisition_date: Optional[str] = None


class AssetResponse(BaseModel):
    """Personal asset response."""
    id: str
    name: str
    description: Optional[str] = None
    category: str
    purchase_price: Optional[Decimal] = None
    estimated_value: Decimal
    currency: str
    acquisition_date: Optional[str] = None
    profit_loss: Optional[Decimal] = None
    profit_loss_percentage: Optional[float] = None
    created_at: datetime
    updated_at: datetime


# ============== VALUATION HISTORY ==============

class AssetValuationCreate(BaseModel):
    """Create a valuation entry."""
    estimated_value: Decimal = Field(ge=0)
    note: Optional[str] = None
    valued_at: str  # ISO date string


class AssetValuationResponse(BaseModel):
    """Valuation history entry response."""
    id: str
    asset_id: str
    estimated_value: Decimal
    note: Optional[str] = None
    valued_at: str
    created_at: datetime


# ============== SUMMARY ==============

class AssetCategorySummary(BaseModel):
    """Summary for a single category."""
    category: str
    count: int
    total_estimated_value: Decimal


class AssetSummaryResponse(BaseModel):
    """Summary of all personal assets."""
    total_estimated_value: Decimal
    total_purchase_price: Decimal
    total_profit_loss: Optional[Decimal] = None
    asset_count: int
    categories: list[AssetCategorySummary]
    assets: list[AssetResponse]
