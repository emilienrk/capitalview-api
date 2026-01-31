"""
User and UserSettings models.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel
import sqlalchemy as sa
from sqlmodel import Column

if TYPE_CHECKING:
    from .bank import BankAccount
    from .cashflow import Cashflow
    from .crypto import CryptoAccount
    from .note import Note
    from .stock import StockAccount


class User(SQLModel, table=True):
    """Central user table."""
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(nullable=False)
    email: str = Field(nullable=False, unique=True, index=True)
    password_hash: str = Field(nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    last_login: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )

    # Relationships
    settings: Optional["UserSettings"] = Relationship(back_populates="user")
    bank_accounts: list["BankAccount"] = Relationship(back_populates="user")
    stock_accounts: list["StockAccount"] = Relationship(back_populates="user")
    crypto_accounts: list["CryptoAccount"] = Relationship(back_populates="user")
    cashflows: list["Cashflow"] = Relationship(back_populates="user")
    notes: list["Note"] = Relationship(back_populates="user")


class UserSettings(SQLModel, table=True):
    """Simulation constants per user (inflation, tax rates)."""
    __tablename__ = "user_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", unique=True)
    objectives: Optional[list] = Field(
        default=None,
        sa_column=Column(sa.JSON),
        description='List of objectives: [{"name": str, "percentage": float}]',
    )
    flat_tax_rate: Decimal = Field(default=Decimal("0.30"), max_digits=5, decimal_places=4)
    tax_pea_rate: Decimal = Field(default=Decimal("0.172"), max_digits=5, decimal_places=4)
    yield_expectation: Decimal = Field(default=Decimal("0.05"), max_digits=5, decimal_places=4)
    inflation_rate: Decimal = Field(default=Decimal("0.02"), max_digits=5, decimal_places=4)

    # Relationships
    user: Optional[User] = Relationship(back_populates="settings")


class RefreshToken(SQLModel, table=True):
    """Refresh tokens for JWT authentication."""
    __tablename__ = "refresh_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    token: str = Field(nullable=False, unique=True, index=True)
    expires_at: datetime = Field(nullable=False)
    revoked: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
