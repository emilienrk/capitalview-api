"""
MarketPrice model (cache for API prices).
"""
from datetime import datetime
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel


class MarketPrice(SQLModel, table=True):
    """Cache for API prices (Asset Price)."""
    __tablename__ = "market_prices"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(unique=True, index=True)
    name: Optional[str] = Field(default=None)
    current_price: Decimal = Field(max_digits=15, decimal_places=4, nullable=False)
    currency: str = Field(default="EUR")
    last_updated: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
