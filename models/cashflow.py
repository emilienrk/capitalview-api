"""Cashflow model (income and expenses)."""

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlmodel import Column, Enum, Field, Relationship, SQLModel

from .enums import FlowType, Frequency

if TYPE_CHECKING:
    from .user import User


class Cashflow(SQLModel, table=True):
    """Merged Income and Expenses table."""
    __tablename__ = "cashflows"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    name: str = Field(nullable=False)
    flow_type: FlowType = Field(sa_column=Column(Enum(FlowType), nullable=False))
    category: str = Field(nullable=False)
    amount: Decimal = Field(max_digits=15, decimal_places=2, nullable=False)
    frequency: Frequency = Field(
        sa_column=Column(Enum(Frequency), nullable=False, default=Frequency.ONCE)
    )
    transaction_date: datetime.date = Field(nullable=False)

    # Relationships
    user: Optional["User"] = Relationship(back_populates="cashflows")
