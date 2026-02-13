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
    isin: str = Field(index=True, unique=True, default=None)
    symbol: Optional[str] = Field(index=True)
    exchange: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    sector: Optional[str] = Field(default=None)
    current_price: Decimal = Field(max_digits=20, decimal_places=8, nullable=False)
    currency: str = Field(default="EUR")
    last_updated: datetime = Field(
        sa_column=Column(
            sa.DateTime(timezone=True), 
            server_default=sa.func.now(), 
            onupdate=sa.func.now(),
            nullable=False
        )
    )
