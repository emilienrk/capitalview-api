"""
MarketAsset and MarketPriceHistory models.

MarketAsset holds asset metadata (isin, symbol, name, currency …).
MarketPriceHistory stores one price per asset per day.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel, UniqueConstraint


class MarketAsset(SQLModel, table=True):
    """Reference data for a tracked market instrument."""
    __tablename__ = "market_assets"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    isin: str = Field(index=True, unique=True, default=None)
    symbol: Optional[str] = Field(index=True)
    exchange: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    sector: Optional[str] = Field(default=None)
    currency: str = Field(default="USD")
    asset_type: Optional[str] = Field(default=None)


class MarketPriceHistory(SQLModel, table=True):
    """One price row per asset per calendar day."""
    __tablename__ = "market_price_history"
    __table_args__ = (
        UniqueConstraint("market_asset_id", "date", name="uq_market_price_history_asset_date"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    market_asset_id: int = Field(foreign_key="market_assets.id", index=True, nullable=False)
    price: Decimal = Field(max_digits=20, decimal_places=8, nullable=False)
    price_date: date = Field(
        sa_column=Column("date", sa.Date, nullable=False),
    )
    created_at: datetime = Field(
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        )
    )
    updated_at: datetime = Field(
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        )
    )


# Backward-compat alias used during transition
MarketPrice = MarketAsset
