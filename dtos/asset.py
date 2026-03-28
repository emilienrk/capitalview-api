"""Asset schemas."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator


class AssetCreate(BaseModel):
    """Create a personal asset. At least one of purchase_price or estimated_value must be provided."""
    name: str
    description: str | None = None
    category: str
    purchase_price: Decimal | None = None
    estimated_value: Decimal | None = None
    currency: str = "EUR"
    acquisition_date: str | None = None

    @field_validator("acquisition_date", mode="before")
    @classmethod
    def validate_acquisition_date(cls, v: str | None) -> str | None:
        """Validate that acquisition_date is in ISO format (YYYY-MM-DD)."""
        if v is None or v == "":
            return None
        try:
            # Try to parse as ISO date (YYYY-MM-DD)
            datetime.fromisoformat(v).date()
            return v
        except (ValueError, TypeError):
            raise ValueError("acquisition_date doit être au format ISO (YYYY-MM-DD)")

    @model_validator(mode="after")
    def at_least_one_price(self) -> "AssetCreate":
        if self.purchase_price is None and self.estimated_value is None:
            raise ValueError("Au moins un prix (achat ou estimé) est requis")
        return self


class AssetUpdate(BaseModel):
    """Update a personal asset."""
    name: str | None = None
    description: str | None = None
    category: str | None = None
    purchase_price: Decimal | None = None
    currency: str | None = None
    acquisition_date: str | None = None


class AssetSell(BaseModel):
    """Mark an asset as sold."""
    sold_price: Decimal = Field(ge=0)
    sold_at: str  # ISO date string


class AssetResponse(BaseModel):
    """Personal asset response."""
    id: str
    name: str
    description: str | None = None
    category: str
    purchase_price: Decimal | None = None
    estimated_value: Decimal
    currency: str
    acquisition_date: str | None = None
    profit_loss: Decimal | None = None
    sold_price: Decimal | None = None
    sold_at: str | None = None
    last_valuation_date: str | None = None
    created_at: datetime
    updated_at: datetime


class AssetValuationCreate(BaseModel):
    """Create a valuation entry."""
    estimated_value: Decimal = Field(ge=0)
    note: str | None = None
    valued_at: str  # ISO date string


class AssetValuationUpdate(BaseModel):
    """Update a valuation entry."""
    estimated_value: Decimal | None = Field(None, ge=0)
    note: str | None = None
    valued_at: str | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "AssetValuationUpdate":
        if not self.model_fields_set:
            raise ValueError("Au moins un champ doit être fourni")
        return self


class AssetValuationResponse(BaseModel):
    """Valuation history entry response."""
    id: str
    asset_id: str
    estimated_value: Decimal
    note: str | None = None
    valued_at: str
    source: str | None = None
    created_at: datetime
    updated_at: datetime


class AssetCategorySummary(BaseModel):
    """Summary for a single category."""
    category: str
    count: int
    total_estimated_value: Decimal


class AssetSummaryResponse(BaseModel):
    """Summary of all personal assets."""
    total_estimated_value: Decimal
    total_purchase_price: Decimal
    total_profit_loss: Decimal | None = None
    asset_count: int
    categories: list[AssetCategorySummary]
    assets: list[AssetResponse]
