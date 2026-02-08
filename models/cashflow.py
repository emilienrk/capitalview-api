"""Cashflow model (income and expenses)."""

from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT


class Cashflow(SQLModel, table=True):
    """Merged Income and Expenses table."""
    __tablename__ = "cashflows"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    flow_type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    category_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    frequency_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    transaction_date_enc: str = Field(sa_column=Column(TEXT, nullable=False))

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