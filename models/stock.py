"""   
StockAccount and StockTransaction models.
"""
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from sqlmodel import Column, Enum, Field, Relationship, SQLModel

from .enums import StockAccountType, StockTransactionType

if TYPE_CHECKING:
    from .user import User


class StockAccount(SQLModel, table=True):
    """Investment accounts (PEA, CTO)."""
    __tablename__ = "stock_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    name: str = Field(nullable=False)
    bank_name: Optional[str] = Field(default=None)
    encrypted_iban: Optional[str] = Field(default=None)
    account_type: StockAccountType = Field(
        sa_column=Column(Enum(StockAccountType), nullable=False)
    )
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )

    # Relationships
    user: Optional["User"] = Relationship(back_populates="stock_accounts")
    transactions: list["StockTransaction"] = Relationship(back_populates="account")


class StockTransaction(SQLModel, table=True):
    """History of buy/sell for stocks."""
    __tablename__ = "stock_transactions"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="stock_accounts.id")
    ticker: str = Field(index=True)
    exchange: Optional[str] = Field(default=None)
    type: StockTransactionType = Field(
        sa_column=Column(Enum(StockTransactionType), nullable=False)
    )
    amount: Decimal = Field(max_digits=15, decimal_places=6, nullable=False)
    price_per_unit: Decimal = Field(max_digits=15, decimal_places=4, nullable=False)
    fees: Decimal = Field(default=Decimal("0"), max_digits=15, decimal_places=2)
    executed_at: datetime = Field(nullable=False)

    # Relationships
    account: Optional[StockAccount] = Relationship(back_populates="transactions")
