"""
User and UserSettings models.
"""
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, TEXT
from datetime import date, datetime, timezone
from decimal import Decimal
import sqlalchemy as sa


class User(SQLModel, table=True):
    """Central user table."""
    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default=None, primary_key=True)
    auth_salt: str = Field(sa_column=Column(TEXT, nullable=False))
    username: str = Field(nullable=False, unique=True, index=True)
    email: str = Field(nullable=False, unique=True, index=True)
    password_hash: str = Field(nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    last_login: Optional[datetime] = Field(default=None)
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


class UserSettings(SQLModel, table=True):
    """Simulation constants per user (inflation, tax rates)."""
    __tablename__ = "user_settings"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(index=True, unique=True) 
    objectives_enc: Optional[str] = Field(sa_column=Column(TEXT))
    theme: str = Field(default="system", nullable=False)
    dashboard_layout_enc: Optional[str] = Field(sa_column=Column(TEXT))
    flat_tax_rate: Decimal = Field(default=Decimal("0.30"), max_digits=5, decimal_places=4)
    tax_pea_rate: Decimal = Field(default=Decimal("0.172"), max_digits=5, decimal_places=4)
    yield_expectation: Decimal = Field(default=Decimal("0.05"), max_digits=5, decimal_places=4)
    inflation_rate: Decimal = Field(default=Decimal("0.02"), max_digits=5, decimal_places=4)
    crypto_module_enabled: bool = Field(default=False, nullable=False)
    crypto_mode: str = Field(default="SINGLE", nullable=False)
    # Manual USD→EUR rate override (None = use auto-fetched rate)
    usd_eur_rate: Optional[Decimal] = Field(
        default=None,
        max_digits=10,
        decimal_places=6,
        sa_column=Column(sa.Numeric(10, 6), nullable=True),
    )

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


class RefreshToken(SQLModel, table=True):
    """Refresh tokens for JWT authentication."""
    __tablename__ = "refresh_tokens"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    user_uuid: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    token: str = Field(nullable=False, unique=True, index=True)
    expires_at: datetime = Field(nullable=False)
    revoked: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
