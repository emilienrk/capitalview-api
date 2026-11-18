"""
FundExposure: what a market line is exposed to — sectors, countries — and its
annual fee.

Public reference data, like the price history: nothing about a user, so nothing
encrypted. One row per weight, so a source that knows only the countries of a
fund adds countries without having to invent sectors.

`origin` says who wrote the row. Today only the hand-kept file does
(data/fund_exposure.toml, synced at startup); an automatic provider would write
the same rows under its own origin, and the reader prefers the file.
"""
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Column, Field, SQLModel, UniqueConstraint


class FundExposure(SQLModel, table=True):
    __tablename__ = "fund_exposures"
    __table_args__ = (
        UniqueConstraint("asset_key", "origin", "kind", "label", name="uq_fund_exposures_row"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    asset_key: str = Field(index=True)
    origin: str = Field(sa_column=Column(sa.String(20), nullable=False))
    # "sector", "country" or "ter". A fee row has an empty label.
    kind: str = Field(sa_column=Column(sa.String(10), nullable=False))
    label: str = Field(default="", sa_column=Column(sa.String(40), nullable=False, server_default=""))
    weight: Decimal = Field(max_digits=10, decimal_places=6, nullable=False)
    as_of: date = Field(nullable=False)
    source: str = Field(sa_column=Column(sa.Text, nullable=False))
    updated_at: datetime = Field(
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        )
    )
