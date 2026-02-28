"""Asset schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class AssetCreate(BaseModel):
    """Create a personal asset. At least one of purchase_price or estimated_value must be provided."""
    name: str
    description: Optional[str] = None
    category: str
    purchase_price: Optional[Decimal] = None
    estimated_value: Optional[Decimal] = None
    currency: str = "EUR"
    acquisition_date: Optional[str] = None

    @model_validator(mode="after")
    def at_least_one_price(self) -> "AssetCreate":
        if self.purchase_price is None and self.estimated_value is None:
            raise ValueError("Au moins un prix (achat ou estimé) est requis")
        return self


class AssetUpdate(BaseModel):
    """Update a personal asset."""
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    purchase_price: Optional[Decimal] = None
    estimated_value: Optional[Decimal] = Field(None, ge=0)
    currency: Optional[str] = None
    acquisition_date: Optional[str] = None


class AssetSell(BaseModel):
    """Mark an asset as sold."""
    sold_price: Decimal = Field(ge=0)
    sold_at: str  # ISO date string


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
    sold_price: Optional[Decimal] = None
    sold_at: Optional[str] = None
    created_at: datetime
    updated_at: datetime


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
