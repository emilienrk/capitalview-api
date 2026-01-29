"""
BankAccount model (standard bank accounts).
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from sqlmodel import Column, Enum, Field, Relationship, SQLModel

from .enums import BankAccountType

if TYPE_CHECKING:
    from .user import User


class BankAccount(SQLModel, table=True):
    """Standard bank accounts (Cash, Savings)."""
    __tablename__ = "bank_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    name: str = Field(nullable=False)
    bank_name: Optional[str] = Field(default=None)
    encrypted_iban: Optional[str] = Field(default=None)
    balance: Decimal = Field(default=Decimal("0"), max_digits=15, decimal_places=2)
    account_type: BankAccountType = Field(sa_column=Column(Enum(BankAccountType), nullable=False))
    updated_at: datetime = Field(
        sa_column=Column(
            sa.DateTime,
            default=datetime.now(timezone.utc),
            onupdate=datetime.now(timezone.utc),
            nullable=False,
        )
    )

    # Relationships
    user: Optional["User"] = Relationship(back_populates="bank_accounts")
