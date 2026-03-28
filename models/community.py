"""
Community profile, follow, and position models.

Allows users to share a public view of their portfolio PnL (%)
without exposing amounts, quantities, or PRU in cleartext.

Privacy modes:
- is_private=False: profile appears in search results freely.
- is_private=True: profile appears only when the exact username is searched.
  Investments are visible only if both users follow each other (mutual follow).
"""
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
            index=True,
        )
    )
    is_active: bool = Field(default=False, nullable=False)
    is_private: bool = Field(default=True, nullable=False)
    display_name: str | None = Field(default=None, sa_column=Column(sa.String(100), nullable=True))
    bio: str | None = Field(default=None, sa_column=Column(sa.Text, nullable=True))
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


class CommunityFollow(SQLModel, table=True):
    """Directed follow relationship between two users.

    follower_id → following_id.  Mutual follow is required to view a
    private profile's investments.
    """
    __tablename__ = "community_follows"
    __table_args__ = (
        sa.UniqueConstraint("follower_id", "following_id", name="uq_follow_pair"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    follower_id: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    following_id: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
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

    id: int | None = Field(default=None, primary_key=True)
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


class CommunityPick(SQLModel, table=True):
    """A 'like' / pick on a stock or crypto asset.

    Users can publicly like assets with an optional comment and target price.
    One pick per user per (symbol, asset_type) — enforced by unique constraint.
    """
    __tablename__ = "community_picks"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "symbol", "asset_type", name="uq_user_pick"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    symbol: str = Field(sa_column=Column(sa.String(30), nullable=False))
    asset_type: str = Field(sa_column=Column(sa.String(10), nullable=False))
    comment: str | None = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    target_price: float | None = Field(default=None, sa_column=Column(sa.Float, nullable=True))
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
