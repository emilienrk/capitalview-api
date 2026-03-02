"""
Community profile and position models.

Allows users to share a public view of their portfolio PnL (%)
without exposing amounts, quantities, or PRU in cleartext.
"""
from typing import Optional
from datetime import datetime

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, TEXT
import sqlalchemy as sa


class CommunityProfile(SQLModel, table=True):
    """One-to-one link between a user and their public community profile.

    Uses user_id directly as the PK (one profile per user, or none).
    """
    __tablename__ = "community_profiles"
    __table_args__ = {"extend_existing": True}

    user_id: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    is_active: bool = Field(default=False, nullable=False)
    display_name: Optional[str] = Field(default=None, sa_column=Column(sa.String(100), nullable=True))
    bio: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))

    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    updated_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


class CommunityPosition(SQLModel, table=True):
    """
    A single asset line shared on a community profile.

    symbol_encrypted / pru_encrypted are encrypted with COMMUNITY_ENCRYPTION_KEY
    (server-side AES-256-GCM) so the server can decrypt them when another
    authenticated user views the profile.
    """
    __tablename__ = "community_positions"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_user_id: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("community_profiles.user_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    asset_type: str = Field(nullable=False)  # "CRYPTO" or "STOCK"
    symbol_encrypted: str = Field(sa_column=Column(TEXT, nullable=False))
    pru_encrypted: str = Field(sa_column=Column(TEXT, nullable=False))

    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    updated_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
