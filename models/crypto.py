"""
CryptoAccount and CryptoTransaction models.
"""
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from sqlmodel import Column, Enum, Field, Relationship, SQLModel

from .enums import CryptoTransactionType

if TYPE_CHECKING:
    from .user import User


class CryptoAccount(SQLModel, table=True):
    """Crypto wallets and exchanges."""
    __tablename__ = "crypto_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    name: str = Field(nullable=False)
    wallet_name: Optional[str] = Field(default=None)
    public_address: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )

    # Relationships
    user: Optional["User"] = Relationship(back_populates="crypto_accounts")
    transactions: list["CryptoTransaction"] = Relationship(back_populates="account")


class CryptoTransaction(SQLModel, table=True):
    """History of buy/sell for crypto."""
    __tablename__ = "crypto_transactions"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="crypto_accounts.id")
    ticker: str = Field(index=True)
    type: CryptoTransactionType = Field(
        sa_column=Column(Enum(CryptoTransactionType), nullable=False)
    )
    amount: Decimal = Field(max_digits=24, decimal_places=18, nullable=False)
    price_per_unit: Decimal = Field(max_digits=15, decimal_places=4, nullable=False)
    fees: Decimal = Field(default=Decimal("0"), max_digits=15, decimal_places=8)
    fees_ticker: Optional[str] = Field(default=None)
    executed_at: datetime = Field(nullable=False)

    # Relationships
    account: Optional[CryptoAccount] = Relationship(back_populates="transactions")
