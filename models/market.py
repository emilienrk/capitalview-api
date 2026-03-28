"""
MarketAsset and MarketPriceHistory models.

MarketAsset holds asset metadata (isin, symbol, name, currency …).
MarketPriceHistory stores one price per asset per day.
"""
from datetime import date, datetime
from decimal import Decimal
import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel, UniqueConstraint


from models.enums import AssetType


class MarketAsset(SQLModel, table=True):
    """Reference data for a tracked market instrument."""
    __tablename__ = "market_assets"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    isin: str = Field(index=True, unique=True, default=None)
    symbol: str | None = Field(index=True)
    exchange: str | None = Field(default=None)
    name: str | None = Field(default=None)
    sector: str | None = Field(default=None)
    asset_type: AssetType | None = Field(default=None, index=True)


class MarketPriceHistory(SQLModel, table=True):
    """One price row per asset per calendar day."""
    __tablename__ = "market_price_history"
    __table_args__ = (
        UniqueConstraint("market_asset_id", "date", name="uq_market_price_history_asset_date"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    market_asset_id: int = Field(
        sa_column=Column(
            sa.Integer,
            sa.ForeignKey("market_assets.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
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
