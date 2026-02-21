"""
Asset and AssetValuation models.
Track personal possessions with estimated values and valuation history.
"""
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT
import uuid


class Asset(SQLModel, table=True):
    """Personal asset (non-market-traded possessions)."""
    __tablename__ = "assets"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    description_enc: Optional[str] = Field(sa_column=Column(TEXT))
    category_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    purchase_price_enc: Optional[str] = Field(sa_column=Column(TEXT))
    estimated_value_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    currency: str = Field(default="EUR", sa_column=Column(TEXT, nullable=False, server_default="EUR"))
    acquisition_date_enc: Optional[str] = Field(sa_column=Column(TEXT))

    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
    updated_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        )
    )


class AssetValuation(SQLModel, table=True):
    """Historical valuation entry for an asset."""
    __tablename__ = "asset_valuations"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    asset_uuid: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    estimated_value_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    note_enc: Optional[str] = Field(sa_column=Column(TEXT))
    valued_at_enc: str = Field(sa_column=Column(TEXT, nullable=False))

    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
