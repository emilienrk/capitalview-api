"""
MarketPrice model (cache for API prices).
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlmodel import Field, SQLModel


class MarketPrice(SQLModel, table=True):
    """Cache for API prices (Asset Price)."""
    __tablename__ = "market_prices"

    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(unique=True, index=True)
    name: Optional[str] = Field(default=None)
    current_price: Decimal = Field(max_digits=15, decimal_places=4, nullable=False)
    currency: str = Field(default="EUR")
    last_updated: datetime = Field(default_factory=datetime.now(timezone.utc))
